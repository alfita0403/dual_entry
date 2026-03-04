"""
RSI significance test on Telonex dataset.

Question: does ETH RSI(7, 5m) add predictive value to our 5 pattern signals?

Method (anti-overfit):
  1. Compute ETH RSI(7) at the time of each pattern match (last COMPLETED candle)
  2. Split trades into terciles (low/mid/high RSI) - NOT arbitrary thresholds
  3. Compare WR across terciles - chi-squared test for independence
  4. Also test: does RSI direction matter? (UP bets want low RSI, DOWN bets want high RSI)
  5. ONE test per analysis = no multiple comparison inflation

If RSI is irrelevant, WR should be ~equal across terciles.
If RSI helps, WR should vary systematically.
"""

import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from scipy.stats import chi2_contingency, mannwhitneyu
from pathlib import Path

from backtest import (
    load_telonex,
    build_sequences,
    find_trades,
    fetch_klines,
    compute_rsi,
    OUTPUT_DIR,
)

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    HAS_MPL = True
except ImportError:
    HAS_MPL = False

# 5 ETH patterns at max_ask=0.60 (same as live)
RULES = [
    {"pattern": "DDDDD", "side": "UP", "coins": ["ETH"], "max_ask": 0.60},
    {"pattern": "DDDD", "side": "UP", "coins": ["ETH"], "max_ask": 0.60},
    {"pattern": "UUUU", "side": "DOWN", "coins": ["ETH"], "max_ask": 0.60},
    {"pattern": "UUU", "side": "DOWN", "coins": ["ETH"], "max_ask": 0.60},
    {"pattern": "DDD", "side": "UP", "coins": ["ETH"], "max_ask": 0.60},
]

RSI_PERIOD = 7
RSI_TIMEFRAME = "5m"
TF_SECONDS = 300  # 5m = 300s


def get_rsi_at_timestamp(rsi_lookup, ts):
    """Get RSI value for the last COMPLETED candle before timestamp ts."""
    snapped = (ts // TF_SECONDS) * TF_SECONDS
    last_completed = snapped - TF_SECONDS
    for candidate in [last_completed, last_completed - TF_SECONDS]:
        if candidate in rsi_lookup:
            v = rsi_lookup[candidate]
            if np.isfinite(v):
                return v
    return np.nan


def main():
    print("=" * 80)
    print("  RSI SIGNIFICANCE TEST - ETH RSI(7, 5m) on Telonex 74-day dataset")
    print("=" * 80)

    # --- Load Telonex trades ---
    print("\n  Loading Telonex data...")
    tdf = load_telonex()
    seqs = build_sequences(tdf)
    trades_df = find_trades(seqs, RULES, size=5.0, fee=0.0)
    print(f"  {len(trades_df)} trades from 5 ETH patterns")

    # --- Fetch ETH klines and compute RSI ---
    ts_min = int(trades_df["start_ts"].min())
    ts_max = int(trades_df["start_ts"].max())
    warmup_s = (RSI_PERIOD + 5) * TF_SECONDS
    start_ms = (ts_min - warmup_s) * 1000
    end_ms = (ts_max + 600) * 1000

    print(f"  Fetching ETHUSDT {RSI_TIMEFRAME} klines...")
    kl = fetch_klines("ETHUSDT", RSI_TIMEFRAME, start_ms, end_ms)
    print(f"  {len(kl)} klines loaded")

    # Compute RSI
    close = kl["close"].values
    rsi_arr = compute_rsi(close, RSI_PERIOD)
    kl["rsi"] = rsi_arr
    kl["ts_s"] = (kl["open_time_ms"] / 1000).astype(int)

    # Build lookup: ts_s -> rsi
    rsi_lookup = {}
    for _, row in kl.iterrows():
        if np.isfinite(row["rsi"]):
            rsi_lookup[int(row["ts_s"])] = row["rsi"]

    # --- Attach RSI to each trade ---
    trades_df["rsi"] = trades_df["start_ts"].apply(
        lambda ts: get_rsi_at_timestamp(rsi_lookup, ts)
    )
    valid = trades_df[trades_df["rsi"].notna()].copy()
    print(
        f"  {len(valid)}/{len(trades_df)} trades have valid RSI ({len(trades_df) - len(valid)} missing)"
    )

    if len(valid) < 100:
        print("  ERROR: too few trades with RSI data")
        return

    # =====================================================================
    # TEST 1: Tercile analysis (does WR vary with RSI level?)
    # =====================================================================
    print(f"\n{'=' * 80}")
    print("  TEST 1: RSI Terciles - does WR vary with RSI level?")
    print(f"{'=' * 80}")

    valid["rsi_tercile"] = pd.qcut(valid["rsi"], 3, labels=["Low", "Mid", "High"])

    print(f"\n  RSI ranges:")
    for t in ["Low", "Mid", "High"]:
        subset = valid[valid["rsi_tercile"] == t]
        print(
            f"    {t:>4}: RSI {subset['rsi'].min():.1f} - {subset['rsi'].max():.1f}  (N={len(subset)})"
        )

    print(f"\n  {'Tercile':>8} {'N':>6} {'Wins':>6} {'WR':>7} {'PnL':>10}")
    print(f"  {'-' * 8} {'-' * 6} {'-' * 6} {'-' * 7} {'-' * 10}")

    contingency = []
    for t in ["Low", "Mid", "High"]:
        subset = valid[valid["rsi_tercile"] == t]
        n = len(subset)
        wins = int(subset["hit"].sum())
        losses = n - wins
        wr = wins / n * 100 if n > 0 else 0
        pnl = subset["pnl"].sum()
        contingency.append([wins, losses])
        sign = "+" if pnl >= 0 else ""
        print(f"  {t:>8} {n:>6} {wins:>6} {wr:>6.1f}% ${sign}{pnl:>8.2f}")

    chi2, p_chi2, dof, _ = chi2_contingency(contingency)
    print(f"\n  Chi-squared test: chi2={chi2:.3f}, dof={dof}, p={p_chi2:.6f}")
    if p_chi2 < 0.05:
        print("  >>> SIGNIFICANT (p < 0.05): WR depends on RSI level")
    else:
        print("  >>> NOT significant (p >= 0.05): WR does NOT depend on RSI level")

    # =====================================================================
    # TEST 2: Directional - does RSI help per side?
    # =====================================================================
    print(f"\n{'=' * 80}")
    print("  TEST 2: Directional - RSI aligned vs misaligned with bet side")
    print("  (UP bets want low RSI = oversold, DOWN bets want high RSI = overbought)")
    print(f"{'=' * 80}")

    median_rsi = valid["rsi"].median()
    print(f"\n  Median RSI: {median_rsi:.1f}")

    # "Aligned" = RSI confirms the mean-reversion thesis
    # UP bet + low RSI (oversold -> expect bounce) = aligned
    # DOWN bet + high RSI (overbought -> expect drop) = aligned
    valid["aligned"] = ((valid["side"] == "UP") & (valid["rsi"] < median_rsi)) | (
        (valid["side"] == "DOWN") & (valid["rsi"] >= median_rsi)
    )

    for label, mask in [
        ("Aligned", valid["aligned"]),
        ("Misaligned", ~valid["aligned"]),
    ]:
        subset = valid[mask]
        n = len(subset)
        wins = int(subset["hit"].sum())
        wr = wins / n * 100 if n > 0 else 0
        pnl = subset["pnl"].sum()
        sign = "+" if pnl >= 0 else ""
        print(f"  {label:>12}: N={n:>5}, WR={wr:.1f}%, PnL=${sign}{pnl:.2f}")

    aligned_wr = valid[valid["aligned"]]["hit"]
    misaligned_wr = valid[~valid["aligned"]]["hit"]
    if len(aligned_wr) > 10 and len(misaligned_wr) > 10:
        stat, p_mw = mannwhitneyu(aligned_wr, misaligned_wr, alternative="greater")
        print(
            f"\n  Mann-Whitney U test (aligned > misaligned): U={stat:.0f}, p={p_mw:.6f}"
        )
        if p_mw < 0.05:
            print("  >>> SIGNIFICANT: RSI alignment improves WR")
        else:
            print("  >>> NOT significant: RSI alignment does NOT improve WR")

    # =====================================================================
    # TEST 3: Per-pattern breakdown
    # =====================================================================
    print(f"\n{'=' * 80}")
    print("  TEST 3: Per-pattern RSI tercile breakdown")
    print(f"{'=' * 80}")

    for pat_name in ["DDDDD", "DDDD", "DDD", "UUUU", "UUU"]:
        pat_trades = valid[valid["pattern"] == pat_name]
        if len(pat_trades) < 30:
            print(f"\n  {pat_name}: too few trades ({len(pat_trades)})")
            continue

        side = pat_trades["side"].iloc[0]
        pat_trades = pat_trades.copy()
        pat_trades["rsi_tercile"] = pd.qcut(
            pat_trades["rsi"], 3, labels=["Low", "Mid", "High"], duplicates="drop"
        )

        print(f"\n  {pat_name} -> {side} (N={len(pat_trades)}):")
        print(f"  {'Tercile':>8} {'N':>5} {'WR':>7} {'PnL':>9}")

        ct = []
        for t in ["Low", "Mid", "High"]:
            s = pat_trades[pat_trades["rsi_tercile"] == t]
            n = len(s)
            if n == 0:
                continue
            wins = int(s["hit"].sum())
            wr = wins / n * 100
            pnl = s["pnl"].sum()
            ct.append([wins, n - wins])
            sign = "+" if pnl >= 0 else ""
            # Mark the "theory-favored" tercile
            star = ""
            if side == "UP" and t == "Low":
                star = " <-- theory favors"
            elif side == "DOWN" and t == "High":
                star = " <-- theory favors"
            print(f"  {t:>8} {n:>5} {wr:>6.1f}% ${sign}{pnl:>7.2f}{star}")

        if len(ct) >= 2:
            chi2_p, p_p, _, _ = chi2_contingency(ct)
            print(f"    chi2={chi2_p:.2f}, p={p_p:.4f} {'*' if p_p < 0.05 else ''}")

    # =====================================================================
    # TEST 4: Continuous correlation (RSI vs outcome)
    # =====================================================================
    print(f"\n{'=' * 80}")
    print("  TEST 4: Point-biserial correlation (RSI vs hit)")
    print(f"{'=' * 80}")

    from scipy.stats import pointbiserialr

    # All trades
    r_all, p_all = pointbiserialr(valid["hit"].astype(int), valid["rsi"])
    print(f"\n  All trades:    r={r_all:.4f}, p={p_all:.4f}")

    # UP bets only (expect negative r: lower RSI -> more wins)
    up = valid[valid["side"] == "UP"]
    if len(up) > 30:
        r_up, p_up = pointbiserialr(up["hit"].astype(int), up["rsi"])
        print(
            f"  UP bets only:  r={r_up:.4f}, p={p_up:.4f}  (expect r < 0 if RSI helps)"
        )

    # DOWN bets only (expect positive r: higher RSI -> more wins)
    dn = valid[valid["side"] == "DOWN"]
    if len(dn) > 30:
        r_dn, p_dn = pointbiserialr(dn["hit"].astype(int), dn["rsi"])
        print(
            f"  DOWN bets only: r={r_dn:.4f}, p={p_dn:.4f}  (expect r > 0 if RSI helps)"
        )

    # =====================================================================
    # PLOT
    # =====================================================================
    if HAS_MPL:
        OUTPUT_DIR.mkdir(exist_ok=True)
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))
        fig.suptitle(
            "RSI Significance Test - ETH RSI(7, 5m) on Telonex 74d",
            fontsize=13,
            fontweight="bold",
        )

        # 1: RSI distribution of wins vs losses
        ax = axes[0][0]
        wins_rsi = valid[valid["hit"]]["rsi"]
        loss_rsi = valid[~valid["hit"]]["rsi"]
        ax.hist(
            wins_rsi,
            bins=30,
            alpha=0.6,
            label=f"Wins (N={len(wins_rsi)})",
            color="green",
        )
        ax.hist(
            loss_rsi,
            bins=30,
            alpha=0.6,
            label=f"Losses (N={len(loss_rsi)})",
            color="red",
        )
        ax.set_xlabel("RSI(7)")
        ax.set_ylabel("Count")
        ax.set_title("RSI Distribution: Wins vs Losses")
        ax.legend()

        # 2: WR by RSI bucket (10-unit bins)
        ax = axes[0][1]
        bins = list(range(0, 101, 10))
        valid["rsi_bin"] = pd.cut(valid["rsi"], bins=bins)
        wr_by_bin = valid.groupby("rsi_bin", observed=True)["hit"].agg(
            ["mean", "count"]
        )
        wr_by_bin = wr_by_bin[wr_by_bin["count"] >= 10]  # min 10 trades per bin
        x_labels = [f"{int(b.left)}-{int(b.right)}" for b in wr_by_bin.index]
        colors = [
            "green" if w > 0.557 else "red" if w < 0.5 else "gray"
            for w in wr_by_bin["mean"]
        ]
        bars = ax.bar(
            range(len(x_labels)), wr_by_bin["mean"] * 100, color=colors, alpha=0.8
        )
        ax.set_xticks(range(len(x_labels)))
        ax.set_xticklabels(x_labels, rotation=45, fontsize=8)
        ax.axhline(
            y=55.7, color="blue", linestyle="--", alpha=0.5, label="Overall WR (55.7%)"
        )
        ax.set_ylabel("Win Rate %")
        ax.set_title("WR by RSI Bucket (10-unit bins)")
        ax.legend()
        # Add trade count on bars
        for i, (_, row) in enumerate(wr_by_bin.iterrows()):
            ax.text(
                i,
                row["mean"] * 100 + 0.5,
                f"n={int(row['count'])}",
                ha="center",
                fontsize=7,
            )

        # 3: WR by RSI for UP bets vs DOWN bets
        ax = axes[1][0]
        for side, color, marker in [("UP", "blue", "o"), ("DOWN", "red", "s")]:
            s = valid[valid["side"] == side].copy()
            s["rsi_bin"] = pd.cut(s["rsi"], bins=bins)
            wr_s = s.groupby("rsi_bin", observed=True)["hit"].agg(["mean", "count"])
            wr_s = wr_s[wr_s["count"] >= 10]
            x = [int(b.mid) for b in wr_s.index]
            ax.plot(
                x,
                wr_s["mean"] * 100,
                color=color,
                marker=marker,
                label=f"{side} bets",
                markersize=5,
            )
        ax.axhline(y=55.7, color="gray", linestyle="--", alpha=0.5)
        ax.set_xlabel("RSI(7)")
        ax.set_ylabel("Win Rate %")
        ax.set_title("WR by RSI: UP bets vs DOWN bets")
        ax.legend()
        ax.grid(True, alpha=0.3)

        # 4: Cumulative PnL by RSI tercile
        ax = axes[1][1]
        for t, color in [("Low", "blue"), ("Mid", "gray"), ("High", "red")]:
            s = valid[valid["rsi_tercile"] == t].sort_values("start_ts")
            cum_pnl = s["pnl"].cumsum()
            ax.plot(
                range(len(cum_pnl)), cum_pnl, label=f"RSI {t}", color=color, alpha=0.8
            )
        ax.set_xlabel("Trade #")
        ax.set_ylabel("Cumulative PnL ($)")
        ax.set_title("Equity Curve by RSI Tercile")
        ax.legend()
        ax.grid(True, alpha=0.3)

        plt.tight_layout()
        path = OUTPUT_DIR / "rsi_significance_telonex.png"
        plt.savefig(path, dpi=150)
        print(f"\n  Plot saved: {path}")

    # =====================================================================
    # SUMMARY
    # =====================================================================
    print(f"\n{'=' * 80}")
    print("  SUMMARY")
    print(f"{'=' * 80}")
    print(f"  Dataset: Telonex 74 days, {len(valid)} ETH trades with RSI data")
    print(f"  RSI: ETH RSI(7, 5m) from Binance klines (last completed candle)")
    print(f"  Overall WR: {valid['hit'].mean() * 100:.1f}%")
    print(
        f"  Chi-squared (terciles): p={p_chi2:.4f} {'- SIGNIFICANT' if p_chi2 < 0.05 else '- not significant'}"
    )
    if len(aligned_wr) > 10 and len(misaligned_wr) > 10:
        print(
            f"  Mann-Whitney (aligned): p={p_mw:.4f} {'- SIGNIFICANT' if p_mw < 0.05 else '- not significant'}"
        )
    print(f"  Point-biserial (all):   r={r_all:.4f}, p={p_all:.4f}")
    if len(up) > 30:
        print(f"  Point-biserial (UP):    r={r_up:.4f}, p={p_up:.4f}")
    if len(dn) > 30:
        print(f"  Point-biserial (DOWN):  r={r_dn:.4f}, p={p_dn:.4f}")

    if p_chi2 < 0.05:
        print("\n  CONCLUSION: RSI has a statistically significant effect on WR.")
        print("  Recommend further investigation with CSV real prices.")
    else:
        print("\n  CONCLUSION: RSI does NOT significantly affect WR.")
        print("  Keeping RSI filter disabled is the correct decision.")

    print(f"{'=' * 80}")


if __name__ == "__main__":
    main()
