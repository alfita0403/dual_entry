"""Price Analyzer for Polymarket stat-arb research.

Analyzes data collected by data_collector.py to find statistical arbitrage
opportunities between BTC, ETH, SOL, XRP 5-minute Up/Down markets.

Generates:
  - Per-cycle price overlay plots
  - Dispersion analysis (std dev across coins over time)
  - Pair spread charts (BTC vs each altcoin)
  - Divergence signal scanner with mean-reversion tracking
  - Correlation heatmap
  - Console summary with actionable stats

Usage:
    python strategies/analyze_prices.py                              # all CSVs
    python strategies/analyze_prices.py data/prices_2026-02-28.csv   # one file
    python strategies/analyze_prices.py --min-spread 0.08            # custom threshold
    python strategies/analyze_prices.py --no-show                    # save PNGs only
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

# ── Constants ────────────────────────────────────────────────

COINS = ["BTC", "ETH", "SOL", "XRP"]
ASK_COLS = [f"{c.lower()}_up_ask" for c in COINS]
BID_COLS = [f"{c.lower()}_up_bid" for c in COINS]
COIN_COLORS = {"BTC": "#F7931A", "ETH": "#627EEA", "SOL": "#9945FF", "XRP": "#23292F"}


# ── Data Loading ─────────────────────────────────────────────


def load_data(paths: List[str]) -> pd.DataFrame:
    """Load and concatenate CSV files into a single DataFrame."""
    frames = []
    for p in paths:
        df = pd.read_csv(p, parse_dates=["timestamp", "cycle_start"])
        frames.append(df)

    df = pd.concat(frames, ignore_index=True)
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)

    # Computed columns
    for coin in COINS:
        ask = f"{coin.lower()}_up_ask"
        bid = f"{coin.lower()}_up_bid"
        df[f"{coin.lower()}_mid"] = (df[ask] + df[bid]) / 2

    mid_cols = [f"{c.lower()}_mid" for c in COINS]
    df["group_mean"] = df[ASK_COLS].mean(axis=1)
    df["dispersion"] = df[ASK_COLS].std(axis=1)
    df["range"] = df[ASK_COLS].max(axis=1) - df[ASK_COLS].min(axis=1)

    return df


def get_cycles(df: pd.DataFrame, min_rows: int = 20) -> List[pd.DataFrame]:
    """Split DataFrame into per-cycle DataFrames, filtering tiny fragments."""
    cycles = []
    for _, group in df.groupby("cycle_start"):
        if len(group) >= min_rows:
            cycles.append(group.reset_index(drop=True))
    return cycles


def determine_outcome(cycle: pd.DataFrame) -> Optional[str]:
    """Determine cycle outcome from late-cycle convergence.

    Returns 'UP' if asks converge to >0.5, 'DOWN' if <0.5, None if unknown.
    """
    late = cycle[cycle["seconds_elapsed"] >= 280]
    if len(late) < 3:
        return None

    avg_ask = late[ASK_COLS].mean().mean()
    if avg_ask >= 0.7:
        return "UP"
    elif avg_ask <= 0.3:
        return "DOWN"
    return None


# ── Analysis Functions ───────────────────────────────────────


def find_divergence_signals(
    cycle: pd.DataFrame,
    min_spread: float = 0.08,
    early_only: bool = True,
    cooldown: int = 10,
) -> List[Dict]:
    """Find moments where one coin diverges significantly from the group.

    A signal fires when:
      - A coin's ask is at least `min_spread` below the group mean
      - We're in the early window (if early_only=True)
      - At least `cooldown` seconds since last signal

    Returns list of signal dicts with reversion tracking.
    """
    signals = []
    last_signal_t = -cooldown

    data = cycle[cycle["early"] == True] if early_only else cycle

    for idx, row in data.iterrows():
        t = row["seconds_elapsed"]
        if t - last_signal_t < cooldown:
            continue

        mean_ask = row["group_mean"]
        for coin in COINS:
            ask_col = f"{coin.lower()}_up_ask"
            coin_ask = row[ask_col]
            deviation = mean_ask - coin_ask

            if deviation >= min_spread:
                # Track what happens next
                future_30 = cycle[
                    (cycle["seconds_elapsed"] >= t + 25)
                    & (cycle["seconds_elapsed"] <= t + 35)
                ]
                future_60 = cycle[
                    (cycle["seconds_elapsed"] >= t + 55)
                    & (cycle["seconds_elapsed"] <= t + 65)
                ]

                ask_after_30 = future_30[ask_col].mean() if len(future_30) > 0 else None
                ask_after_60 = future_60[ask_col].mean() if len(future_60) > 0 else None

                reversion_30 = (
                    (ask_after_30 - coin_ask) if ask_after_30 is not None else None
                )
                reversion_60 = (
                    (ask_after_60 - coin_ask) if ask_after_60 is not None else None
                )

                signals.append({
                    "cycle_start": row["cycle_start"],
                    "seconds_elapsed": t,
                    "coin": coin,
                    "coin_ask": coin_ask,
                    "group_mean": mean_ask,
                    "deviation": deviation,
                    "ask_after_30": ask_after_30,
                    "ask_after_60": ask_after_60,
                    "reversion_30": reversion_30,
                    "reversion_60": reversion_60,
                })
                last_signal_t = t
                break  # one signal per timestamp

    return signals


def compute_pair_spreads(df: pd.DataFrame) -> pd.DataFrame:
    """Compute BTC ask minus each altcoin ask."""
    spreads = df[["timestamp", "cycle_start", "seconds_elapsed", "early"]].copy()
    btc_ask = df["btc_up_ask"]
    for coin in ["ETH", "SOL", "XRP"]:
        spreads[f"btc_vs_{coin.lower()}"] = btc_ask - df[f"{coin.lower()}_up_ask"]
    return spreads


def compute_correlation(df: pd.DataFrame, early_only: bool = True) -> pd.DataFrame:
    """Compute correlation matrix of ask price CHANGES during early window."""
    data = df[df["early"] == True] if early_only else df

    changes = pd.DataFrame()
    for coin in COINS:
        ask_col = f"{coin.lower()}_up_ask"
        changes[coin] = data[ask_col].diff()

    return changes.corr()


# ── Plotting ─────────────────────────────────────────────────


def plot_cycles_overview(cycles: List[pd.DataFrame], outcomes: List[Optional[str]],
                         save_dir: str) -> None:
    """Plot price evolution for recent cycles (up to 6)."""
    recent = cycles[-6:]
    recent_outcomes = outcomes[-6:]
    n = len(recent)
    if n == 0:
        return

    cols = min(n, 3)
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(6 * cols, 4 * rows), squeeze=False)
    fig.suptitle("Price Evolution per Cycle (UP asks)", fontsize=14, fontweight="bold")

    for i, (cycle, outcome) in enumerate(zip(recent, recent_outcomes)):
        ax = axes[i // cols][i % cols]
        t = cycle["seconds_elapsed"]

        for coin in COINS:
            ask_col = f"{coin.lower()}_up_ask"
            ax.plot(t, cycle[ask_col], color=COIN_COLORS[coin], label=coin,
                    linewidth=1.2, alpha=0.85)

        # Early window boundary
        ax.axvline(x=120, color="gray", linestyle="--", alpha=0.5, linewidth=0.8)
        ax.text(122, 0.95, "early", fontsize=7, color="gray", alpha=0.7)

        cycle_time = cycle["cycle_start"].iloc[0]
        title = cycle_time.strftime("%H:%M")
        if outcome:
            title += f" [{outcome} won]"
        ax.set_title(title, fontsize=10)
        ax.set_xlabel("seconds", fontsize=8)
        ax.set_ylabel("UP ask", fontsize=8)
        ax.set_ylim(-0.02, 1.02)
        ax.legend(fontsize=7, loc="upper right")
        ax.grid(True, alpha=0.3)

    # Hide unused subplots
    for i in range(n, rows * cols):
        axes[i // cols][i % cols].set_visible(False)

    plt.tight_layout()
    plt.savefig(os.path.join(save_dir, "1_cycles_overview.png"), dpi=150)


def plot_dispersion(cycles: List[pd.DataFrame], save_dir: str) -> None:
    """Plot dispersion (std dev across 4 coins) over cycle time."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle("Dispersion Analysis (std dev of 4 coins' asks)", fontsize=14,
                 fontweight="bold")

    # Left: individual cycles as faint lines + average
    all_disp = {}
    for cycle in cycles:
        for _, row in cycle.iterrows():
            t = int(row["seconds_elapsed"])
            if t not in all_disp:
                all_disp[t] = []
            all_disp[t].append(row["dispersion"])

        ax1.plot(cycle["seconds_elapsed"], cycle["dispersion"],
                 color="steelblue", alpha=0.2, linewidth=0.8)

    # Average dispersion curve
    avg_t = sorted(all_disp.keys())
    avg_d = [np.mean(all_disp[t]) for t in avg_t]
    ax1.plot(avg_t, avg_d, color="red", linewidth=2, label="Average")
    ax1.axvline(x=120, color="gray", linestyle="--", alpha=0.5)
    ax1.set_xlabel("seconds elapsed")
    ax1.set_ylabel("dispersion (std dev)")
    ax1.set_title("Dispersion Over Cycle Time")
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    # Right: dispersion distribution (early vs late)
    early_data = pd.concat([c[c["early"] == True] for c in cycles])
    late_data = pd.concat([c[c["early"] == False] for c in cycles]) if any(
        (c["early"] == False).any() for c in cycles
    ) else pd.DataFrame()

    if len(early_data) > 0:
        ax2.hist(early_data["dispersion"], bins=30, alpha=0.6, color="steelblue",
                 label=f"Early (n={len(early_data)})", density=True)
    if len(late_data) > 0:
        ax2.hist(late_data["dispersion"], bins=30, alpha=0.6, color="coral",
                 label=f"Late (n={len(late_data)})", density=True)
    ax2.set_xlabel("dispersion (std dev)")
    ax2.set_ylabel("density")
    ax2.set_title("Distribution: Early vs Late")
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(os.path.join(save_dir, "2_dispersion.png"), dpi=150)


def plot_pair_spreads(cycles: List[pd.DataFrame], save_dir: str) -> None:
    """Plot BTC vs altcoin spreads over cycle time."""
    alt_coins = ["ETH", "SOL", "XRP"]
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    fig.suptitle("Pair Spreads: BTC ask - Altcoin ask (early window)",
                 fontsize=14, fontweight="bold")

    for ax, alt in zip(axes, alt_coins):
        spread_col = f"btc_vs_{alt.lower()}"

        for cycle in cycles:
            early = cycle[cycle["early"] == True]
            if len(early) == 0:
                continue
            spreads = early["btc_up_ask"] - early[f"{alt.lower()}_up_ask"]
            ax.plot(early["seconds_elapsed"], spreads,
                    alpha=0.3, linewidth=0.8, color=COIN_COLORS[alt])

        # Average spread
        all_spreads = {}
        for cycle in cycles:
            early = cycle[cycle["early"] == True]
            for _, row in early.iterrows():
                t = int(row["seconds_elapsed"])
                spread = row["btc_up_ask"] - row[f"{alt.lower()}_up_ask"]
                if t not in all_spreads:
                    all_spreads[t] = []
                all_spreads[t].append(spread)

        if all_spreads:
            avg_t = sorted(all_spreads.keys())
            avg_s = [np.mean(all_spreads[t]) for t in avg_t]
            ax.plot(avg_t, avg_s, color="red", linewidth=2, label="Average")

        ax.axhline(y=0, color="black", linewidth=0.5)
        ax.set_xlabel("seconds elapsed")
        ax.set_ylabel("spread")
        ax.set_title(f"BTC - {alt}", fontsize=11)
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(os.path.join(save_dir, "3_pair_spreads.png"), dpi=150)


def plot_correlation(corr: pd.DataFrame, save_dir: str) -> None:
    """Plot correlation heatmap of ask price changes."""
    fig, ax = plt.subplots(figsize=(6, 5))
    fig.suptitle("Correlation of Ask Price Changes (early window)",
                 fontsize=14, fontweight="bold")

    im = ax.imshow(corr.values, cmap="RdYlGn", vmin=-1, vmax=1)

    ax.set_xticks(range(len(COINS)))
    ax.set_yticks(range(len(COINS)))
    ax.set_xticklabels(COINS)
    ax.set_yticklabels(COINS)

    # Annotate cells
    for i in range(len(COINS)):
        for j in range(len(COINS)):
            val = corr.values[i, j]
            color = "white" if abs(val) > 0.5 else "black"
            ax.text(j, i, f"{val:.2f}", ha="center", va="center",
                    color=color, fontsize=12, fontweight="bold")

    plt.colorbar(im, ax=ax, shrink=0.8)
    plt.tight_layout()
    plt.savefig(os.path.join(save_dir, "4_correlation.png"), dpi=150)


def plot_signals(signals: List[Dict], save_dir: str) -> None:
    """Plot divergence signals and their reversion outcomes."""
    if not signals:
        return

    sig_df = pd.DataFrame(signals)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle("Divergence Signal Scanner", fontsize=14, fontweight="bold")

    # Left: deviation size vs reversion at +30s
    has_rev30 = sig_df.dropna(subset=["reversion_30"])
    if len(has_rev30) > 0:
        colors = [COIN_COLORS[c] for c in has_rev30["coin"]]
        ax1.scatter(has_rev30["deviation"], has_rev30["reversion_30"],
                    c=colors, s=60, alpha=0.7, edgecolors="black", linewidth=0.5)
        ax1.axhline(y=0, color="black", linewidth=0.5)

        # Add coin labels
        for _, row in has_rev30.iterrows():
            ax1.annotate(row["coin"], (row["deviation"], row["reversion_30"]),
                         fontsize=7, alpha=0.7,
                         xytext=(3, 3), textcoords="offset points")

    ax1.set_xlabel("deviation from group mean")
    ax1.set_ylabel("price change after 30s")
    ax1.set_title("Deviation vs 30s Reversion")
    ax1.grid(True, alpha=0.3)

    # Right: deviation size vs reversion at +60s
    has_rev60 = sig_df.dropna(subset=["reversion_60"])
    if len(has_rev60) > 0:
        colors = [COIN_COLORS[c] for c in has_rev60["coin"]]
        ax2.scatter(has_rev60["deviation"], has_rev60["reversion_60"],
                    c=colors, s=60, alpha=0.7, edgecolors="black", linewidth=0.5)
        ax2.axhline(y=0, color="black", linewidth=0.5)

        for _, row in has_rev60.iterrows():
            ax2.annotate(row["coin"], (row["deviation"], row["reversion_60"]),
                         fontsize=7, alpha=0.7,
                         xytext=(3, 3), textcoords="offset points")

    ax2.set_xlabel("deviation from group mean")
    ax2.set_ylabel("price change after 60s")
    ax2.set_title("Deviation vs 60s Reversion")
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(os.path.join(save_dir, "5_signals.png"), dpi=150)


# ── Console Report ───────────────────────────────────────────


def print_report(
    df: pd.DataFrame,
    cycles: List[pd.DataFrame],
    outcomes: List[Optional[str]],
    signals: List[Dict],
    corr: pd.DataFrame,
) -> None:
    """Print summary report to console."""
    print("\n" + "=" * 65)
    print("  POLYMARKET STAT-ARB ANALYSIS REPORT")
    print("=" * 65)

    # Data summary
    date_range = f"{df['timestamp'].min()} -> {df['timestamp'].max()}"
    print(f"\n  Data:     {len(df)} rows | {len(cycles)} cycles")
    print(f"  Range:    {date_range}")

    # Outcomes
    known = [(i, o) for i, o in enumerate(outcomes) if o is not None]
    if known:
        up_wins = sum(1 for _, o in known if o == "UP")
        down_wins = sum(1 for _, o in known if o == "DOWN")
        print(f"  Outcomes: {len(known)} resolved | UP={up_wins} DOWN={down_wins}")
    else:
        print("  Outcomes: No resolved cycles yet (need full 5-min data)")

    # Average prices early window
    early = df[df["early"] == True]
    if len(early) > 0:
        print(f"\n  Average asks (early window, t<=120):")
        for coin in COINS:
            ask_col = f"{coin.lower()}_up_ask"
            avg = early[ask_col].mean()
            std = early[ask_col].std()
            print(f"    {coin:>4}: {avg:.3f} +/- {std:.3f}")

    # Dispersion stats
    if len(early) > 0:
        avg_disp = early["dispersion"].mean()
        max_disp = early["dispersion"].max()
        p90_disp = early["dispersion"].quantile(0.9)
        print(f"\n  Dispersion (early window):")
        print(f"    Average: {avg_disp:.4f}")
        print(f"    P90:     {p90_disp:.4f}")
        print(f"    Max:     {max_disp:.4f}")

    # Correlation
    print(f"\n  Correlation of ask changes (early window):")
    for i, c1 in enumerate(COINS):
        for j, c2 in enumerate(COINS):
            if j > i:
                print(f"    {c1}-{c2}: {corr.loc[c1, c2]:.3f}")

    # Divergence signals
    print(f"\n  Divergence signals: {len(signals)} detected")
    if signals:
        sig_df = pd.DataFrame(signals)

        # Mean reversion stats
        has_30 = sig_df.dropna(subset=["reversion_30"])
        has_60 = sig_df.dropna(subset=["reversion_60"])

        if len(has_30) > 0:
            reverted_30 = (has_30["reversion_30"] > 0).sum()
            avg_rev_30 = has_30["reversion_30"].mean()
            print(f"    30s reversion: {reverted_30}/{len(has_30)} "
                  f"({100*reverted_30/len(has_30):.0f}%) | "
                  f"avg change: {avg_rev_30:+.4f}")

        if len(has_60) > 0:
            reverted_60 = (has_60["reversion_60"] > 0).sum()
            avg_rev_60 = has_60["reversion_60"].mean()
            print(f"    60s reversion: {reverted_60}/{len(has_60)} "
                  f"({100*reverted_60/len(has_60):.0f}%) | "
                  f"avg change: {avg_rev_60:+.4f}")

        # Per-coin breakdown
        print(f"\n  Signals by coin:")
        for coin in COINS:
            coin_sigs = sig_df[sig_df["coin"] == coin]
            if len(coin_sigs) > 0:
                avg_dev = coin_sigs["deviation"].mean()
                print(f"    {coin:>4}: {len(coin_sigs)} signals | "
                      f"avg deviation: {avg_dev:.3f}")

        # Detail table
        print(f"\n  Signal Detail:")
        print(f"  {'Time':>6} {'Coin':>4} {'Ask':>6} {'Mean':>6} "
              f"{'Dev':>6} {'30s':>7} {'60s':>7}")
        print(f"  {'-'*6} {'-'*4} {'-'*6} {'-'*6} "
              f"{'-'*6} {'-'*7} {'-'*7}")
        for sig in signals:
            t = sig["seconds_elapsed"]
            rev30 = f"{sig['reversion_30']:+.3f}" if sig["reversion_30"] is not None else "   n/a"
            rev60 = f"{sig['reversion_60']:+.3f}" if sig["reversion_60"] is not None else "   n/a"
            print(f"  {t:>5}s {sig['coin']:>4} {sig['coin_ask']:>6.2f} "
                  f"{sig['group_mean']:>6.2f} {sig['deviation']:>6.3f} "
                  f"{rev30:>7} {rev60:>7}")

    print("\n" + "=" * 65)


# ── CLI ──────────────────────────────────────────────────────


def parse_args():
    parser = argparse.ArgumentParser(
        description="Analyze price snapshots for stat-arb opportunities"
    )
    parser.add_argument(
        "files", nargs="*", default=None,
        help="CSV file(s) to analyze. Default: all prices_*.csv in data/",
    )
    parser.add_argument(
        "--min-spread", type=float, default=0.08,
        help="Min deviation from group mean to trigger signal (default: 0.08)",
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
    print(f"Loaded {len(df)} rows")

    # Split into cycles
    cycles = get_cycles(df, min_rows=20)
    if not cycles:
        print("Not enough data for analysis (need cycles with 20+ rows).")
        sys.exit(1)

    outcomes = [determine_outcome(c) for c in cycles]
    print(f"Found {len(cycles)} cycles ({sum(o is not None for o in outcomes)} with known outcome)")

    # Find divergence signals
    all_signals = []
    for cycle in cycles:
        sigs = find_divergence_signals(
            cycle,
            min_spread=args.min_spread,
            cooldown=args.cooldown,
        )
        all_signals.extend(sigs)

    # Correlation
    corr = compute_correlation(df, early_only=True)

    # Console report
    print_report(df, cycles, outcomes, all_signals, corr)

    # Plots
    save_dir = args.output_dir
    Path(save_dir).mkdir(parents=True, exist_ok=True)

    print(f"\nGenerating plots in {save_dir}/...")
    plot_cycles_overview(cycles, outcomes, save_dir)
    plot_dispersion(cycles, save_dir)
    plot_pair_spreads(cycles, save_dir)
    plot_correlation(corr, save_dir)
    plot_signals(all_signals, save_dir)

    print(f"Saved 5 plots to {save_dir}/")

    if not args.no_show:
        plt.show()


if __name__ == "__main__":
    main()
