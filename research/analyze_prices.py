"""Price Analyzer for Polymarket stat-arb research.

Designed for large datasets (24h+ of data, 288+ cycles).
Analyzes data collected by data_collector.py to find statistical arbitrage
opportunities between BTC, ETH, SOL, XRP 5-minute Up/Down markets.

Generates:
  1. Sample cycles + average cycle with confidence bands
  2. Dispersion analysis with percentile bands
  3. Pair spread evolution (BTC vs altcoins) with bands
  4. Correlation heatmap of price changes
  5. Divergence signal scanner with reversion histograms
  6. Time-of-day patterns
  7. Expected P&L backtest of divergence signals

Usage:
    python strategies/analyze_prices.py                              # all CSVs
    python strategies/analyze_prices.py data/prices_2026-02-28.csv   # one file
    python strategies/analyze_prices.py --min-spread 0.10            # stricter threshold
    python strategies/analyze_prices.py --no-show                    # save PNGs only
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# ── Constants ────────────────────────────────────────────────

COINS = ["BTC", "ETH", "SOL", "XRP"]
ASK_COLS = [f"{c.lower()}_up_ask" for c in COINS]
BID_COLS = [f"{c.lower()}_up_bid" for c in COINS]
COIN_COLORS = {"BTC": "#F7931A", "ETH": "#627EEA", "SOL": "#9945FF", "XRP": "#23292F"}


# ── Data Loading ─────────────────────────────────────────────


def load_data(paths: List[str]) -> pd.DataFrame:
    """Load and concatenate CSV files."""
    frames = []
    for p in paths:
        df = pd.read_csv(p, parse_dates=["timestamp", "cycle_start"])
        frames.append(df)

    df = pd.concat(frames, ignore_index=True)
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)

    # Computed columns
    df["group_mean"] = df[ASK_COLS].mean(axis=1)
    df["dispersion"] = df[ASK_COLS].std(axis=1)
    df["ask_range"] = df[ASK_COLS].max(axis=1) - df[ASK_COLS].min(axis=1)
    df["hour"] = df["cycle_start"].dt.hour

    return df


def get_cycles(df: pd.DataFrame, min_rows: int = 20) -> List[pd.DataFrame]:
    """Split into per-cycle DataFrames, filtering tiny fragments."""
    cycles = []
    for _, group in df.groupby("cycle_start"):
        if len(group) >= min_rows:
            cycles.append(group.reset_index(drop=True))
    return cycles


# ── Outcome Determination ────────────────────────────────────


def determine_coin_outcomes(cycle: pd.DataFrame) -> Dict[str, Optional[str]]:
    """Determine per-COIN outcome from late-cycle convergence.

    Each coin resolves independently (BTC can go UP while SOL goes DOWN).
    Returns dict like {"BTC": "UP", "ETH": "DOWN", "SOL": None, "XRP": "DOWN"}.
    """
    late = cycle[cycle["seconds_elapsed"] >= 280]
    if len(late) < 3:
        return {c: None for c in COINS}

    outcomes = {}
    for coin in COINS:
        ask_col = f"{coin.lower()}_up_ask"
        avg_ask = late[ask_col].mean()
        if avg_ask >= 0.70:
            outcomes[coin] = "UP"
        elif avg_ask <= 0.30:
            outcomes[coin] = "DOWN"
        else:
            outcomes[coin] = None
    return outcomes


# ── Signal Detection ─────────────────────────────────────────


def find_divergence_signals(
    cycle: pd.DataFrame,
    coin_outcomes: Dict[str, Optional[str]],
    min_spread: float = 0.08,
    cooldown: int = 10,
) -> List[Dict]:
    """Find moments where one coin diverges from the group mean.

    Tracks reversion at +30s and +60s, plus whether that coin's UP won.
    """
    signals = []
    last_signal_t = -cooldown
    early = cycle[cycle["early"] == True]

    for idx, row in early.iterrows():
        t = row["seconds_elapsed"]
        if t - last_signal_t < cooldown:
            continue

        mean_ask = row["group_mean"]

        # Find the most divergent coin (cheapest relative to mean)
        best_coin, best_dev = None, 0.0
        for coin in COINS:
            ask_col = f"{coin.lower()}_up_ask"
            dev = mean_ask - row[ask_col]
            if dev > best_dev:
                best_coin, best_dev = coin, dev

        if best_coin is None or best_dev < min_spread:
            continue

        ask_col = f"{best_coin.lower()}_up_ask"
        coin_ask = row[ask_col]

        # Track reversion
        f30 = cycle[(cycle["seconds_elapsed"] >= t + 25) &
                     (cycle["seconds_elapsed"] <= t + 35)]
        f60 = cycle[(cycle["seconds_elapsed"] >= t + 55) &
                     (cycle["seconds_elapsed"] <= t + 65)]

        ask30 = f30[ask_col].mean() if len(f30) > 0 else None
        ask60 = f60[ask_col].mean() if len(f60) > 0 else None

        # Coin outcome for P&L
        outcome = coin_outcomes.get(best_coin)

        signals.append({
            "cycle_start": row["cycle_start"],
            "hour": row["cycle_start"].hour,
            "seconds_elapsed": t,
            "coin": best_coin,
            "coin_ask": coin_ask,
            "group_mean": mean_ask,
            "deviation": best_dev,
            "ask_after_30": ask30,
            "ask_after_60": ask60,
            "reversion_30": (ask30 - coin_ask) if ask30 is not None else None,
            "reversion_60": (ask60 - coin_ask) if ask60 is not None else None,
            "coin_outcome": outcome,
            # P&L if we bought at coin_ask: win = 1 - ask, lose = -ask
            "pnl": (1.0 - coin_ask) if outcome == "UP" else (
                -coin_ask if outcome == "DOWN" else None
            ),
        })
        last_signal_t = t

    return signals


def compute_correlation(df: pd.DataFrame) -> pd.DataFrame:
    """Correlation matrix of ask price CHANGES during early window."""
    early = df[df["early"] == True].copy()
    changes = pd.DataFrame()
    for coin in COINS:
        changes[coin] = early[f"{coin.lower()}_up_ask"].diff()
    return changes.corr()


# ── Trade Simulation (TP + Timeout) ──────────────────────────


def simulate_exit(
    cycle: pd.DataFrame, t_entry: int, coin: str,
    ask_entry: float, target: float, timeout: int,
    coin_outcome: Optional[str],
) -> Dict:
    """Simulate a single trade exit using bid data.

    Priority: Take Profit > Timeout exit > Hold to expiry.
    """
    bid_col = f"{coin.lower()}_up_bid"
    max_t = min(t_entry + timeout, 300)

    # Look for take profit (vectorized: first bid >= entry + target)
    future = cycle[(cycle["seconds_elapsed"] > t_entry) &
                   (cycle["seconds_elapsed"] <= max_t)]
    if len(future) > 0:
        tp_mask = future[bid_col] >= ask_entry + target
        tp_hits = future[tp_mask]
        if len(tp_hits) > 0:
            first = tp_hits.iloc[0]
            return {
                "exit_type": "TP",
                "exit_price": float(first[bid_col]),
                "pnl": float(first[bid_col]) - ask_entry,
                "hold_time": int(first["seconds_elapsed"]) - t_entry,
            }

    # Timeout: sell at last available bid before max_t
    if max_t < 300 and len(future) > 0:
        bid = float(future[bid_col].iloc[-1])
        return {
            "exit_type": "TIMEOUT",
            "exit_price": bid,
            "pnl": bid - ask_entry,
            "hold_time": timeout,
        }

    # Hold to expiry
    if coin_outcome == "UP":
        return {"exit_type": "EXPIRY_WIN", "exit_price": 1.0,
                "pnl": 1.0 - ask_entry, "hold_time": 300 - t_entry}
    elif coin_outcome == "DOWN":
        return {"exit_type": "EXPIRY_LOSS", "exit_price": 0.0,
                "pnl": -ask_entry, "hold_time": 300 - t_entry}
    return {"exit_type": "UNKNOWN", "exit_price": None,
            "pnl": None, "hold_time": None}


def sweep_parameters(
    cycles: List[pd.DataFrame],
    all_outcomes: List[Dict[str, Optional[str]]],
    cooldown: int = 10,
) -> pd.DataFrame:
    """Test all parameter combinations and return ranked results."""
    SPREADS = [0.05, 0.06, 0.08, 0.10, 0.12, 0.15]
    TARGETS = [0.01, 0.02, 0.03, 0.05, 0.08]
    TIMEOUTS = [20, 30, 45, 60, 90, 120]

    results = []

    for min_spread in SPREADS:
        # Find signals once per min_spread
        all_signals = []
        for i, (cycle, outcomes) in enumerate(zip(cycles, all_outcomes)):
            sigs = find_divergence_signals(cycle, outcomes, min_spread, cooldown)
            for sig in sigs:
                sig["_cycle_idx"] = i
            all_signals.extend(sigs)

        if not all_signals:
            continue

        for target in TARGETS:
            for timeout in TIMEOUTS:
                trades = []
                for sig in all_signals:
                    cycle = cycles[sig["_cycle_idx"]]
                    result = simulate_exit(
                        cycle, sig["seconds_elapsed"], sig["coin"],
                        sig["coin_ask"], target, timeout, sig["coin_outcome"],
                    )
                    if result["pnl"] is not None:
                        trades.append(result)

                if len(trades) < 3:
                    continue

                pnls = [t["pnl"] for t in trades]
                wins = [p for p in pnls if p > 0]
                tp_n = sum(1 for t in trades if t["exit_type"] == "TP")
                to_n = sum(1 for t in trades if t["exit_type"] == "TIMEOUT")

                results.append({
                    "min_spread": min_spread,
                    "target": target,
                    "timeout": timeout,
                    "trades": len(trades),
                    "win_rate": len(wins) / len(trades),
                    "avg_pnl": float(np.mean(pnls)),
                    "total_pnl": float(np.sum(pnls)),
                    "avg_hold": float(np.mean([t["hold_time"] for t in trades])),
                    "tp_pct": tp_n / len(trades),
                    "timeout_pct": to_n / len(trades),
                    "expiry_pct": 1 - (tp_n + to_n) / len(trades),
                })

    return pd.DataFrame(results)


# ── Aggregation helpers (vectorized, no iterrows) ────────────


def aggregate_by_elapsed(cycles: List[pd.DataFrame], col: str,
                         early_only: bool = False) -> pd.DataFrame:
    """Aggregate a column across cycles by seconds_elapsed.

    Returns DataFrame with columns: seconds_elapsed, mean, p10, p25, p75, p90.
    """
    all_data = pd.concat(cycles, ignore_index=True)
    if early_only:
        all_data = all_data[all_data["early"] == True]

    grouped = all_data.groupby("seconds_elapsed")[col].agg(
        ["mean", "std", "count",
         lambda x: x.quantile(0.10),
         lambda x: x.quantile(0.25),
         lambda x: x.quantile(0.75),
         lambda x: x.quantile(0.90)]
    )
    grouped.columns = ["mean", "std", "count", "p10", "p25", "p75", "p90"]
    grouped = grouped[grouped["count"] >= 3]  # need at least 3 cycles per second
    return grouped.reset_index()


# ── Plotting ─────────────────────────────────────────────────


def plot_cycles_overview(cycles: List[pd.DataFrame],
                         all_outcomes: List[Dict[str, Optional[str]]],
                         save_dir: str) -> None:
    """Top: 6 sample cycles. Bottom: average cycle with confidence bands."""
    n_cycles = len(cycles)

    # Pick 6 evenly spaced cycles
    if n_cycles <= 6:
        sample_idx = list(range(n_cycles))
    else:
        sample_idx = [int(i * (n_cycles - 1) / 5) for i in range(6)]

    n_samples = len(sample_idx)
    fig, axes = plt.subplots(2, 3, figsize=(18, 10), squeeze=False)
    fig.suptitle(f"Price Evolution - {n_cycles} cycles total", fontsize=14,
                 fontweight="bold")

    # Top row: sample cycles
    for i, ci in enumerate(sample_idx[:3]):
        ax = axes[0][i]
        cycle = cycles[ci]
        outcome = all_outcomes[ci]
        t = cycle["seconds_elapsed"]

        for coin in COINS:
            ax.plot(t, cycle[f"{coin.lower()}_up_ask"], color=COIN_COLORS[coin],
                    label=coin, linewidth=1.2, alpha=0.85)

        ax.axvline(x=120, color="gray", linestyle="--", alpha=0.5, linewidth=0.8)
        cycle_time = cycle["cycle_start"].iloc[0]
        # Build outcome label
        ups = [c for c in COINS if outcome.get(c) == "UP"]
        downs = [c for c in COINS if outcome.get(c) == "DOWN"]
        lbl = cycle_time.strftime("%H:%M")
        if ups:
            lbl += f" [UP: {','.join(ups)}]"
        elif downs:
            lbl += " [DOWN]"
        ax.set_title(lbl, fontsize=9)
        ax.set_ylim(-0.02, 1.02)
        ax.legend(fontsize=7, loc="upper right")
        ax.grid(True, alpha=0.3)

    # Bottom row: average cycle with bands (one subplot per coin)
    # Actually, put all coins on one chart with bands
    for i in range(3):
        ax = axes[1][i]

    # Use bottom-left for average cycle
    ax_avg = axes[1][0]
    for coin in COINS:
        col = f"{coin.lower()}_up_ask"
        agg = aggregate_by_elapsed(cycles, col)
        if len(agg) == 0:
            continue
        ax_avg.plot(agg["seconds_elapsed"], agg["mean"], color=COIN_COLORS[coin],
                    label=coin, linewidth=1.5)
        ax_avg.fill_between(agg["seconds_elapsed"], agg["p25"], agg["p75"],
                            color=COIN_COLORS[coin], alpha=0.1)

    ax_avg.axvline(x=120, color="gray", linestyle="--", alpha=0.5)
    ax_avg.set_title(f"Average Cycle (P25-P75 band, n={n_cycles})", fontsize=10)
    ax_avg.set_xlabel("seconds")
    ax_avg.set_ylabel("UP ask")
    if ax_avg.get_legend_handles_labels()[1]:
        ax_avg.legend(fontsize=7)
    ax_avg.grid(True, alpha=0.3)

    # Bottom-center: average dispersion
    ax_disp = axes[1][1]
    agg_d = aggregate_by_elapsed(cycles, "dispersion")
    if len(agg_d) > 0:
        ax_disp.plot(agg_d["seconds_elapsed"], agg_d["mean"], color="red", linewidth=2)
        ax_disp.fill_between(agg_d["seconds_elapsed"], agg_d["p25"], agg_d["p75"],
                             color="red", alpha=0.15, label="P25-P75")
        ax_disp.fill_between(agg_d["seconds_elapsed"], agg_d["p10"], agg_d["p90"],
                             color="red", alpha=0.05, label="P10-P90")
    ax_disp.axvline(x=120, color="gray", linestyle="--", alpha=0.5)
    ax_disp.set_title("Average Dispersion", fontsize=10)
    ax_disp.set_xlabel("seconds")
    ax_disp.set_ylabel("std dev")
    if ax_disp.get_legend_handles_labels()[1]:
        ax_disp.legend(fontsize=7)
    ax_disp.grid(True, alpha=0.3)

    # Bottom-right: outcome distribution pie/bar
    ax_out = axes[1][2]
    all_coin_outcomes = []
    for oc in all_outcomes:
        for coin in COINS:
            if oc.get(coin) is not None:
                all_coin_outcomes.append(oc[coin])
    if all_coin_outcomes:
        up_count = all_coin_outcomes.count("UP")
        down_count = all_coin_outcomes.count("DOWN")
        total = up_count + down_count
        ax_out.bar(["UP", "DOWN"], [up_count, down_count],
                   color=["#2ecc71", "#e74c3c"], alpha=0.8)
        ax_out.set_title(f"Outcomes (n={total} coin-cycles)", fontsize=10)
        ax_out.set_ylabel("count")
        for j, v in enumerate([up_count, down_count]):
            pct = 100 * v / total if total > 0 else 0
            ax_out.text(j, v + 0.5, f"{v} ({pct:.0f}%)", ha="center", fontsize=9)
    else:
        ax_out.text(0.5, 0.5, "No resolved cycles", ha="center", va="center",
                    transform=ax_out.transAxes, fontsize=12)
    ax_out.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(os.path.join(save_dir, "1_cycles_overview.png"), dpi=150)


def plot_pair_spreads(cycles: List[pd.DataFrame], save_dir: str) -> None:
    """BTC vs altcoin spreads with percentile bands (no individual lines)."""
    alt_coins = ["ETH", "SOL", "XRP"]
    fig, axes = plt.subplots(1, 3, figsize=(16, 5))
    fig.suptitle("Pair Spreads: BTC ask - Altcoin ask (early window)",
                 fontsize=14, fontweight="bold")

    # Pre-compute spread columns
    all_early = pd.concat([c[c["early"] == True] for c in cycles], ignore_index=True)

    for ax, alt in zip(axes, alt_coins):
        col_name = f"_spread_{alt.lower()}"
        all_early[col_name] = all_early["btc_up_ask"] - all_early[f"{alt.lower()}_up_ask"]

        grouped = all_early.groupby("seconds_elapsed")[col_name].agg(
            ["mean",
             lambda x: x.quantile(0.25),
             lambda x: x.quantile(0.75)]
        )
        grouped.columns = ["mean", "p25", "p75"]
        grouped = grouped.reset_index()

        ax.plot(grouped["seconds_elapsed"], grouped["mean"],
                color=COIN_COLORS[alt], linewidth=2, label="Mean")
        ax.fill_between(grouped["seconds_elapsed"], grouped["p25"], grouped["p75"],
                        color=COIN_COLORS[alt], alpha=0.2, label="P25-P75")
        ax.axhline(y=0, color="black", linewidth=0.5)
        ax.set_xlabel("seconds elapsed")
        ax.set_ylabel("spread")
        ax.set_title(f"BTC - {alt}", fontsize=11)
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(os.path.join(save_dir, "2_pair_spreads.png"), dpi=150)


def plot_correlation(corr: pd.DataFrame, save_dir: str) -> None:
    """Correlation heatmap."""
    fig, ax = plt.subplots(figsize=(6, 5))
    fig.suptitle("Correlation of Ask Price Changes (early window)",
                 fontsize=14, fontweight="bold")

    im = ax.imshow(corr.values, cmap="RdYlGn", vmin=-1, vmax=1)
    ax.set_xticks(range(len(COINS)))
    ax.set_yticks(range(len(COINS)))
    ax.set_xticklabels(COINS)
    ax.set_yticklabels(COINS)

    for i in range(len(COINS)):
        for j in range(len(COINS)):
            val = corr.values[i, j]
            color = "white" if abs(val) > 0.5 else "black"
            ax.text(j, i, f"{val:.2f}", ha="center", va="center",
                    color=color, fontsize=12, fontweight="bold")

    plt.colorbar(im, ax=ax, shrink=0.8)
    plt.tight_layout()
    plt.savefig(os.path.join(save_dir, "3_correlation.png"), dpi=150)


def plot_signals(signals: List[Dict], save_dir: str) -> None:
    """Scatter of deviation vs reversion + histogram of outcomes."""
    if not signals:
        return

    sig_df = pd.DataFrame(signals)
    fig, axes = plt.subplots(1, 3, figsize=(18, 5))
    fig.suptitle(f"Divergence Signals (n={len(sig_df)})", fontsize=14,
                 fontweight="bold")

    # Left: scatter deviation vs 30s reversion
    ax1 = axes[0]
    has_30 = sig_df.dropna(subset=["reversion_30"])
    if len(has_30) > 0:
        colors = [COIN_COLORS[c] for c in has_30["coin"]]
        ax1.scatter(has_30["deviation"], has_30["reversion_30"],
                    c=colors, s=30, alpha=0.5, edgecolors="none")
        ax1.axhline(y=0, color="black", linewidth=0.5)

        # Trend line
        if len(has_30) >= 5:
            z = np.polyfit(has_30["deviation"], has_30["reversion_30"], 1)
            x_line = np.linspace(has_30["deviation"].min(), has_30["deviation"].max(), 50)
            ax1.plot(x_line, np.polyval(z, x_line), "r--", linewidth=1.5,
                     label=f"trend (slope={z[0]:.2f})")
            ax1.legend(fontsize=8)

    ax1.set_xlabel("deviation from group mean")
    ax1.set_ylabel("price change after 30s")
    ax1.set_title("Deviation vs 30s Reversion")
    ax1.grid(True, alpha=0.3)

    # Center: histogram of 30s reversion
    ax2 = axes[1]
    if len(has_30) > 0:
        pos = has_30[has_30["reversion_30"] > 0]["reversion_30"]
        neg = has_30[has_30["reversion_30"] <= 0]["reversion_30"]
        bins = np.linspace(has_30["reversion_30"].min(), has_30["reversion_30"].max(), 25)
        if len(pos) > 0:
            ax2.hist(pos, bins=bins, alpha=0.7, color="#2ecc71", label=f"Reverted ({len(pos)})")
        if len(neg) > 0:
            ax2.hist(neg, bins=bins, alpha=0.7, color="#e74c3c", label=f"Didn't ({len(neg)})")
        pct = 100 * len(pos) / len(has_30)
        ax2.set_title(f"30s Reversion Distribution ({pct:.0f}% positive)", fontsize=10)
        ax2.legend(fontsize=8)
    ax2.set_xlabel("price change after 30s")
    ax2.set_ylabel("count")
    ax2.grid(True, alpha=0.3)

    # Right: P&L histogram (if outcomes available)
    ax3 = axes[2]
    has_pnl = sig_df.dropna(subset=["pnl"])
    if len(has_pnl) > 0:
        wins = has_pnl[has_pnl["pnl"] > 0]["pnl"]
        losses = has_pnl[has_pnl["pnl"] <= 0]["pnl"]
        all_pnl = has_pnl["pnl"]
        bins = np.linspace(all_pnl.min(), all_pnl.max(), 25)
        if len(wins) > 0:
            ax3.hist(wins, bins=bins, alpha=0.7, color="#2ecc71",
                     label=f"Wins ({len(wins)})")
        if len(losses) > 0:
            ax3.hist(losses, bins=bins, alpha=0.7, color="#e74c3c",
                     label=f"Losses ({len(losses)})")
        avg_pnl = all_pnl.mean()
        ax3.axvline(x=avg_pnl, color="black", linestyle="--", linewidth=1.5)
        ax3.set_title(f"Signal P&L (avg={avg_pnl:+.3f}/trade, n={len(has_pnl)})",
                      fontsize=10)
        ax3.legend(fontsize=8)
    else:
        ax3.text(0.5, 0.5, "No resolved signals\n(need full cycles)",
                 ha="center", va="center", transform=ax3.transAxes, fontsize=11)
    ax3.set_xlabel("P&L per $1 bet")
    ax3.set_ylabel("count")
    ax3.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(os.path.join(save_dir, "4_signals.png"), dpi=150)


def plot_time_of_day(df: pd.DataFrame, signals: List[Dict],
                     all_outcomes: List[Dict[str, Optional[str]]],
                     cycles: List[pd.DataFrame],
                     save_dir: str) -> None:
    """Time-of-day patterns: dispersion, signal frequency, win rate by hour."""
    fig, axes = plt.subplots(1, 3, figsize=(18, 5))
    fig.suptitle("Time-of-Day Analysis (UTC)", fontsize=14, fontweight="bold")

    early = df[df["early"] == True]

    # Left: average dispersion by hour
    ax1 = axes[0]
    if len(early) > 0:
        hourly_disp = early.groupby("hour")["dispersion"].agg(["mean", "std", "count"])
        hours_with_data = hourly_disp[hourly_disp["count"] >= 10]
        if len(hours_with_data) > 0:
            ax1.bar(hours_with_data.index, hours_with_data["mean"],
                    yerr=hours_with_data["std"] / np.sqrt(hours_with_data["count"]),
                    color="steelblue", alpha=0.7, capsize=3)
    ax1.set_xlabel("hour (UTC)")
    ax1.set_ylabel("avg dispersion")
    ax1.set_title("Dispersion by Hour")
    ax1.set_xticks(range(0, 24))
    ax1.tick_params(axis="x", labelsize=7)
    ax1.grid(True, alpha=0.3)

    # Center: signal frequency by hour
    ax2 = axes[1]
    if signals:
        sig_df = pd.DataFrame(signals)
        hourly_sigs = sig_df.groupby("hour").size()
        # Normalize by cycles per hour
        cycle_hours = pd.Series([c["cycle_start"].iloc[0].hour for c in cycles])
        cycles_per_hour = cycle_hours.value_counts().sort_index()
        # Signals per cycle by hour
        sig_rate = pd.Series(dtype=float)
        for h in range(24):
            n_sigs = hourly_sigs.get(h, 0)
            n_cycles = cycles_per_hour.get(h, 0)
            if n_cycles > 0:
                sig_rate[h] = n_sigs / n_cycles
        if len(sig_rate) > 0:
            ax2.bar(sig_rate.index, sig_rate.values, color="coral", alpha=0.7)
    ax2.set_xlabel("hour (UTC)")
    ax2.set_ylabel("signals per cycle")
    ax2.set_title("Signal Frequency by Hour")
    ax2.set_xticks(range(0, 24))
    ax2.tick_params(axis="x", labelsize=7)
    ax2.grid(True, alpha=0.3)

    # Right: win rate by hour (signals that resulted in UP win for cheap coin)
    ax3 = axes[2]
    if signals:
        sig_df = pd.DataFrame(signals)
        has_outcome = sig_df.dropna(subset=["coin_outcome"])
        if len(has_outcome) > 0:
            hourly_wr = has_outcome.groupby("hour").apply(
                lambda g: (g["coin_outcome"] == "UP").mean(),
                include_groups=False,
            )
            hourly_n = has_outcome.groupby("hour").size()
            if len(hourly_wr) > 0:
                colors = ["#2ecc71" if wr > 0.5 else "#e74c3c" for wr in hourly_wr]
                bars = ax3.bar(hourly_wr.index, hourly_wr.values, color=colors, alpha=0.7)
                ax3.axhline(y=0.5, color="black", linestyle="--", linewidth=0.8)
                for h, wr in hourly_wr.items():
                    n = hourly_n.get(h, 0)
                    ax3.text(h, wr + 0.02, f"n={n}", ha="center", fontsize=6)
    ax3.set_xlabel("hour (UTC)")
    ax3.set_ylabel("win rate (cheap coin UP)")
    ax3.set_title("Signal Win Rate by Hour")
    ax3.set_ylim(0, 1.05)
    ax3.set_xticks(range(0, 24))
    ax3.tick_params(axis="x", labelsize=7)
    ax3.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(os.path.join(save_dir, "5_time_of_day.png"), dpi=150)


def plot_backtest(sweep_df: pd.DataFrame, save_dir: str) -> None:
    """Plot parameter sweep results: heatmaps + best combo cumulative P&L."""
    if len(sweep_df) == 0:
        return

    profitable = sweep_df[sweep_df["avg_pnl"] > 0]
    best = sweep_df.loc[sweep_df["total_pnl"].idxmax()] if len(sweep_df) > 0 else None

    fig, axes = plt.subplots(2, 3, figsize=(18, 10))
    fig.suptitle("Take-Profit Backtest - Parameter Sweep", fontsize=14,
                 fontweight="bold")

    # Top-left: heatmap target vs timeout (best min_spread)
    ax = axes[0][0]
    if best is not None:
        best_spread = best["min_spread"]
        subset = sweep_df[sweep_df["min_spread"] == best_spread]
        pivot = subset.pivot_table(values="avg_pnl", index="target",
                                   columns="timeout", aggfunc="first")
        if len(pivot) > 0:
            im = ax.imshow(pivot.values, cmap="RdYlGn", aspect="auto",
                           vmin=-0.1, vmax=0.1)
            ax.set_xticks(range(len(pivot.columns)))
            ax.set_xticklabels([f"{int(c)}s" for c in pivot.columns], fontsize=8)
            ax.set_yticks(range(len(pivot.index)))
            ax.set_yticklabels([f"{v:.2f}" for v in pivot.index], fontsize=8)
            for i in range(len(pivot.index)):
                for j in range(len(pivot.columns)):
                    val = pivot.values[i, j]
                    if not np.isnan(val):
                        color = "white" if abs(val) > 0.05 else "black"
                        ax.text(j, i, f"{val:+.3f}", ha="center", va="center",
                                fontsize=7, color=color)
            plt.colorbar(im, ax=ax, shrink=0.8)
    ax.set_xlabel("timeout")
    ax.set_ylabel("target")
    ax.set_title(f"Avg P&L (min_spread={best_spread:.2f})" if best is not None else "")

    # Top-center: avg_pnl by min_spread (aggregated)
    ax = axes[0][1]
    by_spread = sweep_df.groupby("min_spread")["avg_pnl"].agg(["mean", "max", "count"])
    if len(by_spread) > 0:
        colors = ["#2ecc71" if m > 0 else "#e74c3c" for m in by_spread["max"]]
        ax.bar(range(len(by_spread)), by_spread["max"], color=colors, alpha=0.7)
        ax.set_xticks(range(len(by_spread)))
        ax.set_xticklabels([f"{s:.2f}" for s in by_spread.index], fontsize=8)
        for i, (_, row) in enumerate(by_spread.iterrows()):
            ax.text(i, row["max"] + 0.002, f"best:{row['max']:+.3f}",
                    ha="center", fontsize=6)
    ax.axhline(y=0, color="black", linewidth=0.5)
    ax.set_xlabel("min_spread")
    ax.set_ylabel("best avg P&L")
    ax.set_title("Best P&L per Spread Threshold")
    ax.grid(True, alpha=0.3)

    # Top-right: trades count by min_spread
    ax = axes[0][2]
    by_spread_trades = sweep_df.groupby("min_spread")["trades"].first()
    if len(by_spread_trades) > 0:
        ax.bar(range(len(by_spread_trades)), by_spread_trades.values,
               color="steelblue", alpha=0.7)
        ax.set_xticks(range(len(by_spread_trades)))
        ax.set_xticklabels([f"{s:.2f}" for s in by_spread_trades.index], fontsize=8)
        for i, v in enumerate(by_spread_trades.values):
            ax.text(i, v + 0.5, str(int(v)), ha="center", fontsize=7)
    ax.set_xlabel("min_spread")
    ax.set_ylabel("# trades")
    ax.set_title("Trade Count by Spread Threshold")
    ax.grid(True, alpha=0.3)

    # Bottom-left: exit type breakdown for best combo
    ax = axes[1][0]
    if best is not None:
        labels = ["Take Profit", "Timeout", "Expiry"]
        sizes = [best["tp_pct"], best["timeout_pct"], best["expiry_pct"]]
        colors_pie = ["#2ecc71", "#f39c12", "#e74c3c"]
        wedges, texts, autotexts = ax.pie(
            sizes, labels=labels, colors=colors_pie, autopct="%1.0f%%",
            startangle=90, textprops={"fontsize": 9})
        ax.set_title(f"Exit Types (best combo, n={int(best['trades'])})", fontsize=10)

    # Bottom-center: win rate vs avg_pnl scatter (all combos)
    ax = axes[1][1]
    if len(sweep_df) > 0:
        sc = ax.scatter(sweep_df["win_rate"], sweep_df["avg_pnl"],
                        c=sweep_df["min_spread"], cmap="viridis",
                        s=30, alpha=0.6, edgecolors="none")
        ax.axhline(y=0, color="black", linewidth=0.5)
        if best is not None:
            ax.scatter([best["win_rate"]], [best["avg_pnl"]],
                       c="red", s=120, marker="*", zorder=5, label="Best")
            ax.legend(fontsize=8)
        plt.colorbar(sc, ax=ax, label="min_spread", shrink=0.8)
    ax.set_xlabel("win rate")
    ax.set_ylabel("avg P&L per $1")
    ax.set_title("All Combos: Win Rate vs P&L")
    ax.grid(True, alpha=0.3)

    # Bottom-right: top 10 combos bar chart
    ax = axes[1][2]
    top10 = sweep_df.nlargest(10, "total_pnl")
    if len(top10) > 0:
        labels = [f"s{r['min_spread']:.2f}\nt{r['target']:.2f}\n{int(r['timeout'])}s"
                  for _, r in top10.iterrows()]
        colors = ["#2ecc71" if p > 0 else "#e74c3c" for p in top10["total_pnl"]]
        ax.barh(range(len(top10)), top10["total_pnl"], color=colors, alpha=0.7)
        ax.set_yticks(range(len(top10)))
        ax.set_yticklabels(labels, fontsize=6)
        for i, (_, row) in enumerate(top10.iterrows()):
            ax.text(row["total_pnl"] + 0.05, i,
                    f"{row['total_pnl']:+.2f} ({int(row['trades'])}t)",
                    va="center", fontsize=7)
    ax.axvline(x=0, color="black", linewidth=0.5)
    ax.set_xlabel("total P&L")
    ax.set_title("Top 10 Combos by Total P&L")
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(os.path.join(save_dir, "6_backtest.png"), dpi=150)


# ── Console Report ───────────────────────────────────────────


def print_report(
    df: pd.DataFrame,
    cycles: List[pd.DataFrame],
    all_outcomes: List[Dict[str, Optional[str]]],
    signals: List[Dict],
    corr: pd.DataFrame,
    sweep_df: Optional[pd.DataFrame] = None,
) -> None:
    """Print comprehensive summary to console."""
    print("\n" + "=" * 70)
    print("  POLYMARKET STAT-ARB ANALYSIS REPORT")
    print("=" * 70)

    # Data summary
    date_min = df["timestamp"].min()
    date_max = df["timestamp"].max()
    n_hours = (date_max - date_min).total_seconds() / 3600
    print(f"\n  Data:     {len(df):,} rows | {len(cycles)} cycles | {n_hours:.1f} hours")
    print(f"  Range:    {date_min} -> {date_max}")

    # Per-coin outcomes
    resolved = 0
    coin_up = {c: 0 for c in COINS}
    coin_down = {c: 0 for c in COINS}
    for oc in all_outcomes:
        for coin in COINS:
            if oc[coin] == "UP":
                coin_up[coin] += 1
                resolved += 1
            elif oc[coin] == "DOWN":
                coin_down[coin] += 1
                resolved += 1

    if resolved > 0:
        print(f"\n  Outcomes ({resolved} coin-cycles resolved):")
        for coin in COINS:
            total = coin_up[coin] + coin_down[coin]
            if total > 0:
                up_pct = 100 * coin_up[coin] / total
                print(f"    {coin:>4}: UP {coin_up[coin]:>3} ({up_pct:4.1f}%) | "
                      f"DOWN {coin_down[coin]:>3} ({100-up_pct:4.1f}%) | "
                      f"total {total}")
    else:
        print("\n  Outcomes: No resolved cycles (need full 5-min data with t>=280)")

    # Dispersion
    early = df[df["early"] == True]
    if len(early) > 0:
        avg_d = early["dispersion"].mean()
        p50_d = early["dispersion"].median()
        p90_d = early["dispersion"].quantile(0.90)
        p99_d = early["dispersion"].quantile(0.99)
        print(f"\n  Dispersion (early window):")
        print(f"    Mean: {avg_d:.4f} | Median: {p50_d:.4f} | "
              f"P90: {p90_d:.4f} | P99: {p99_d:.4f}")

    # Correlation
    print(f"\n  Correlation (ask changes, early window):")
    pairs = []
    for i, c1 in enumerate(COINS):
        for j, c2 in enumerate(COINS):
            if j > i:
                pairs.append((corr.loc[c1, c2], c1, c2))
    pairs.sort(key=lambda x: abs(x[0]), reverse=True)
    for val, c1, c2 in pairs:
        bar = "#" * int(abs(val) * 20)
        print(f"    {c1}-{c2}: {val:+.3f}  {bar}")

    # Signals
    print(f"\n  {'='*50}")
    print(f"  DIVERGENCE SIGNALS: {len(signals)} detected")
    print(f"  {'='*50}")

    if not signals:
        print("  (none - try lowering --min-spread)")
        print("\n" + "=" * 70)
        return

    sig_df = pd.DataFrame(signals)

    # Reversion stats
    has_30 = sig_df.dropna(subset=["reversion_30"])
    has_60 = sig_df.dropna(subset=["reversion_60"])

    if len(has_30) > 0:
        rev30_pct = 100 * (has_30["reversion_30"] > 0).mean()
        avg30 = has_30["reversion_30"].mean()
        print(f"\n  30s reversion: {rev30_pct:.1f}% positive "
              f"(n={len(has_30)}) | avg: {avg30:+.4f}")

    if len(has_60) > 0:
        rev60_pct = 100 * (has_60["reversion_60"] > 0).mean()
        avg60 = has_60["reversion_60"].mean()
        print(f"  60s reversion: {rev60_pct:.1f}% positive "
              f"(n={len(has_60)}) | avg: {avg60:+.4f}")

    # Per-coin signal breakdown
    print(f"\n  Signals by coin:")
    for coin in COINS:
        cs = sig_df[sig_df["coin"] == coin]
        if len(cs) > 0:
            avg_dev = cs["deviation"].mean()
            avg_ask = cs["coin_ask"].mean()
            print(f"    {coin:>4}: {len(cs):>4} signals | "
                  f"avg dev: {avg_dev:.3f} | avg ask: {avg_ask:.2f}")

    # P&L summary
    has_pnl = sig_df.dropna(subset=["pnl"])
    if len(has_pnl) > 0:
        wins = has_pnl[has_pnl["pnl"] > 0]
        losses = has_pnl[has_pnl["pnl"] <= 0]
        avg_pnl = has_pnl["pnl"].mean()
        total_pnl = has_pnl["pnl"].sum()
        win_rate = len(wins) / len(has_pnl)
        avg_win = wins["pnl"].mean() if len(wins) > 0 else 0
        avg_loss = losses["pnl"].mean() if len(losses) > 0 else 0

        print(f"\n  {'='*50}")
        print(f"  P&L BACKTEST (buy cheap coin at ask, $1 per trade)")
        print(f"  {'='*50}")
        print(f"  Trades:     {len(has_pnl)}")
        print(f"  Win rate:   {100*win_rate:.1f}% "
              f"({len(wins)}W / {len(losses)}L)")
        print(f"  Avg win:    {avg_win:+.3f}")
        print(f"  Avg loss:   {avg_loss:+.3f}")
        print(f"  Avg P&L:    {avg_pnl:+.4f} per trade")
        print(f"  Total P&L:  {total_pnl:+.2f} (on {len(has_pnl)} trades)")

        # Breakeven analysis
        avg_ask = has_pnl["coin_ask"].mean()
        be_wr = avg_ask  # breakeven win rate = buy price
        print(f"\n  Avg buy price:      {avg_ask:.3f}")
        print(f"  Breakeven win rate: {100*be_wr:.1f}%")
        print(f"  Actual win rate:    {100*win_rate:.1f}%")
        edge = win_rate - be_wr
        print(f"  Edge:               {100*edge:+.1f}pp")
    else:
        print(f"\n  P&L: No resolved signals (need cycles with t>=280 data)")

    # ── Sweep results (TP backtest) ──
    if sweep_df is not None and len(sweep_df) > 0:
        profitable = sweep_df[sweep_df["avg_pnl"] > 0]
        best = sweep_df.loc[sweep_df["total_pnl"].idxmax()]

        print(f"\n  {'='*50}")
        print(f"  TAKE-PROFIT BACKTEST (parameter sweep)")
        print(f"  {'='*50}")
        print(f"  Combos tested:  {len(sweep_df)}")
        print(f"  Profitable:     {len(profitable)}")

        # Best combo details
        print(f"\n  BEST COMBO (by total P&L):")
        print(f"    min_spread:   {best['min_spread']:.2f}")
        print(f"    TP target:    {best['target']:.2f}")
        print(f"    timeout:      {int(best['timeout'])}s")
        print(f"    trades:       {int(best['trades'])}")
        print(f"    win rate:     {100*best['win_rate']:.1f}%")
        print(f"    avg P&L:      {best['avg_pnl']:+.4f} per $1")
        print(f"    total P&L:    {best['total_pnl']:+.2f}")
        print(f"    avg hold:     {best['avg_hold']:.0f}s")
        print(f"    exit types:   TP {100*best['tp_pct']:.0f}% | "
              f"Timeout {100*best['timeout_pct']:.0f}% | "
              f"Expiry {100*best['expiry_pct']:.0f}%")

        # Top 10 combos table
        top10 = sweep_df.nlargest(10, "total_pnl")
        print(f"\n  Top 10 combos by total P&L:")
        print(f"  {'Spread':>6} {'Target':>6} {'T/O':>5} "
              f"{'Trades':>6} {'WR':>6} {'AvgPnL':>8} {'TotPnL':>8} "
              f"{'TP%':>5} {'Hold':>5}")
        print(f"  {'-'*6} {'-'*6} {'-'*5} "
              f"{'-'*6} {'-'*6} {'-'*8} {'-'*8} "
              f"{'-'*5} {'-'*5}")
        for _, row in top10.iterrows():
            print(f"  {row['min_spread']:>6.2f} {row['target']:>6.2f} "
                  f"{int(row['timeout']):>4}s "
                  f"{int(row['trades']):>6} {100*row['win_rate']:>5.1f}% "
                  f"{row['avg_pnl']:>+8.4f} {row['total_pnl']:>+8.2f} "
                  f"{100*row['tp_pct']:>4.0f}% {row['avg_hold']:>4.0f}s")

        # Also show if ANY combo beats hold-to-expiry
        sig_df_local = pd.DataFrame(signals) if signals else pd.DataFrame()
        has_pnl_local = sig_df_local.dropna(subset=["pnl"]) if len(sig_df_local) > 0 else pd.DataFrame()
        if len(has_pnl_local) > 0:
            hold_avg = has_pnl_local["pnl"].mean()
            tp_improvement = best["avg_pnl"] - hold_avg
            print(f"\n  vs Hold-to-Expiry:")
            print(f"    Hold avg P&L:   {hold_avg:+.4f}")
            print(f"    Best TP avg:    {best['avg_pnl']:+.4f}")
            print(f"    Improvement:    {tp_improvement:+.4f} per trade")

    # Top signals table (limit to 20)
    show_n = min(len(signals), 20)
    if show_n > 0 and len(signals) > 20:
        print(f"\n  Top {show_n} signals by deviation (of {len(signals)} total):")
    elif show_n > 0:
        print(f"\n  All {show_n} signals:")
    sig_sorted = sig_df.sort_values("deviation", ascending=False).head(show_n)
    print(f"  {'Time':>6} {'Coin':>4} {'Ask':>6} {'Mean':>6} "
          f"{'Dev':>6} {'30s':>7} {'60s':>7} {'P&L':>7}")
    print(f"  {'-'*6} {'-'*4} {'-'*6} {'-'*6} "
          f"{'-'*6} {'-'*7} {'-'*7} {'-'*7}")
    for _, sig in sig_sorted.iterrows():
        t = sig["seconds_elapsed"]
        r30 = f"{sig['reversion_30']:+.3f}" if pd.notna(sig["reversion_30"]) else "   n/a"
        r60 = f"{sig['reversion_60']:+.3f}" if pd.notna(sig["reversion_60"]) else "   n/a"
        pnl = f"{sig['pnl']:+.3f}" if pd.notna(sig["pnl"]) else "   n/a"
        print(f"  {t:>5}s {sig['coin']:>4} {sig['coin_ask']:>6.2f} "
              f"{sig['group_mean']:>6.2f} {sig['deviation']:>6.3f} "
              f"{r30:>7} {r60:>7} {pnl:>7}")

    print("\n" + "=" * 70)


# ── CLI ──────────────────────────────────────────────────────


def parse_args():
    parser = argparse.ArgumentParser(
        description="Analyze price snapshots for stat-arb opportunities"
    )
    parser.add_argument(
        "files", nargs="*", default=None,
        help="CSV file(s). Default: all prices_*.csv in data/",
    )
    parser.add_argument(
        "--min-spread", type=float, default=0.08,
        help="Min deviation to trigger signal (default: 0.08)",
    )
    parser.add_argument(
        "--cooldown", type=int, default=10,
        help="Min seconds between signals (default: 10)",
    )
    parser.add_argument(
        "--output-dir", default="reports",
        help="Directory for PNG plots (default: reports/)",
    )
    parser.add_argument(
        "--no-show", action="store_true",
        help="Save PNGs only, don't open interactive plots",
    )
    return parser.parse_args()


# ── Main ─────────────────────────────────────────────────────


def main():
    args = parse_args()

    # Resolve input files
    if args.files:
        csv_files = args.files
    else:
        data_dir = Path("data")
        csv_files = sorted(data_dir.glob("prices_*.csv"))
        if not csv_files:
            print("No CSV files found in data/. Run data_collector.py first.")
            sys.exit(1)
        csv_files = [str(f) for f in csv_files]

    print(f"Loading {len(csv_files)} file(s)...")
    df = load_data(csv_files)
    print(f"Loaded {len(df):,} rows")

    # Split into cycles
    cycles = get_cycles(df, min_rows=20)
    if not cycles:
        print("Not enough data (need cycles with 20+ rows).")
        sys.exit(1)

    # Per-coin outcomes
    all_outcomes = [determine_coin_outcomes(c) for c in cycles]
    resolved_cycles = sum(
        1 for oc in all_outcomes if any(v is not None for v in oc.values())
    )
    print(f"Found {len(cycles)} cycles ({resolved_cycles} with known outcome)")

    # Find divergence signals
    all_signals = []
    for cycle, outcomes in zip(cycles, all_outcomes):
        sigs = find_divergence_signals(
            cycle, outcomes,
            min_spread=args.min_spread,
            cooldown=args.cooldown,
        )
        all_signals.extend(sigs)
    print(f"Detected {len(all_signals)} divergence signals")

    # Parameter sweep (TP backtest)
    print("Running parameter sweep (TP backtest)...")
    sweep_df = sweep_parameters(cycles, all_outcomes, cooldown=args.cooldown)
    if len(sweep_df) > 0:
        profitable = sweep_df[sweep_df["avg_pnl"] > 0]
        print(f"Tested {len(sweep_df)} combos | "
              f"{len(profitable)} profitable")
    else:
        print("No parameter combos produced enough trades")

    # Correlation
    corr = compute_correlation(df)

    # Console report
    print_report(df, cycles, all_outcomes, all_signals, corr, sweep_df)

    # Plots
    save_dir = args.output_dir
    Path(save_dir).mkdir(parents=True, exist_ok=True)

    print(f"\nGenerating plots in {save_dir}/...")
    plot_cycles_overview(cycles, all_outcomes, save_dir)
    plot_pair_spreads(cycles, save_dir)
    plot_correlation(corr, save_dir)
    plot_signals(all_signals, save_dir)
    plot_time_of_day(df, all_signals, all_outcomes, cycles, save_dir)
    plot_backtest(sweep_df, save_dir)

    print(f"Saved 6 plots to {save_dir}/")

    if not args.no_show:
        plt.show()


if __name__ == "__main__":
    main()
