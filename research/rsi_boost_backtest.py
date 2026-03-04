"""
RSI boost backtest: compare flat size vs boosted size when RSI confirms.

Logic:
  - UP bets (DDD, DDDD, DDDDD): boost when RSI < threshold (oversold)
  - DOWN bets (UUU, UUUU): boost when RSI > (100-threshold) (overbought)
  - Never skip trades - just increase size on high-conviction setups

Tests multiple boost levels and thresholds on Telonex 74-day dataset.
"""

import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
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

RULES = [
    {"pattern": "DDDDD", "side": "UP", "coins": ["ETH"], "max_ask": 0.55},
    {"pattern": "DDDD", "side": "UP", "coins": ["ETH"], "max_ask": 0.55},
    {"pattern": "UUUU", "side": "DOWN", "coins": ["ETH"], "max_ask": 0.55},
    {"pattern": "UUU", "side": "DOWN", "coins": ["ETH"], "max_ask": 0.55},
    {"pattern": "DDD", "side": "UP", "coins": ["ETH"], "max_ask": 0.55},
]

RSI_PERIOD = 7
TF_SECONDS = 300
BASE_SIZE = 5.0


def get_rsi_at_ts(rsi_lookup, ts):
    snapped = (ts // TF_SECONDS) * TF_SECONDS
    last_completed = snapped - TF_SECONDS
    for candidate in [last_completed, last_completed - TF_SECONDS]:
        if candidate in rsi_lookup:
            v = rsi_lookup[candidate]
            if np.isfinite(v):
                return v
    return np.nan


def simulate_boost(trades_df, rsi_lookup, rsi_thresh, boost_mult):
    """Simulate PnL with RSI-based size boosting.

    - UP bets: boost if RSI < rsi_thresh
    - DOWN bets: boost if RSI > (100 - rsi_thresh)
    - All other trades: base size
    """
    total_pnl = 0.0
    total_pnl_flat = 0.0
    boosted_count = 0
    total_count = 0
    boosted_wins = 0
    boosted_total = 0

    for _, row in trades_df.iterrows():
        rsi = get_rsi_at_ts(rsi_lookup, row["start_ts"])
        side = row["side"]
        hit = row["hit"]
        ma = row["max_ask"]

        # Flat PnL (baseline)
        if hit:
            flat_pnl = (1.0 - ma) * BASE_SIZE
        else:
            flat_pnl = -ma * BASE_SIZE
        total_pnl_flat += flat_pnl

        # Boosted PnL
        boost = False
        if not np.isnan(rsi):
            if side == "UP" and rsi < rsi_thresh:
                boost = True
            elif side == "DOWN" and rsi > (100 - rsi_thresh):
                boost = True

        size = BASE_SIZE * boost_mult if boost else BASE_SIZE
        if hit:
            pnl = (1.0 - ma) * size
        else:
            pnl = -ma * size

        total_pnl += pnl
        total_count += 1
        if boost:
            boosted_count += 1
            boosted_total += 1
            if hit:
                boosted_wins += 1

    boosted_wr = boosted_wins / boosted_total * 100 if boosted_total > 0 else 0
    return {
        "pnl_boost": total_pnl,
        "pnl_flat": total_pnl_flat,
        "improvement": total_pnl - total_pnl_flat,
        "boosted_trades": boosted_count,
        "boosted_wr": boosted_wr,
        "total": total_count,
        "pct_boosted": boosted_count / total_count * 100 if total_count else 0,
    }


def main():
    print("=" * 80)
    print("  RSI BOOST BACKTEST - Telonex 74 days")
    print("=" * 80)

    # Load data
    print("\n  Loading Telonex...")
    tdf = load_telonex()
    seqs = build_sequences(tdf)
    trades_df = find_trades(seqs, RULES, size=BASE_SIZE, fee=0.0)
    print(f"  {len(trades_df)} trades")

    # Fetch ETH klines
    ts_min, ts_max = int(trades_df["start_ts"].min()), int(trades_df["start_ts"].max())
    warmup_s = 50 * TF_SECONDS
    kl = fetch_klines(
        "ETHUSDT", "5m", (ts_min - warmup_s) * 1000, (ts_max + 600) * 1000
    )
    rsi_arr = compute_rsi(kl["close"].values, RSI_PERIOD)
    kl["rsi"] = rsi_arr
    kl["ts_s"] = (kl["open_time_ms"] / 1000).astype(int)
    rsi_lookup = {
        int(r["ts_s"]): r["rsi"] for _, r in kl.iterrows() if np.isfinite(r["rsi"])
    }
    print(f"  {len(rsi_lookup)} RSI values")

    # Flat baseline
    flat_pnl = trades_df["pnl"].sum()
    print(f"\n  Flat baseline ($5 all trades): ${flat_pnl:+.2f}")

    # Sweep: thresholds x boost multipliers
    thresholds = [30, 35, 40, 45, 50]
    boosts = [1.5, 2.0, 2.5, 3.0]

    print(
        f"\n  {'Thresh':>7} {'Boost':>6} {'Boosted':>8} {'%Boost':>7} {'BstWR':>6} {'PnL Flat':>10} {'PnL Boost':>11} {'Improv':>9}"
    )
    print(
        f"  {'-' * 7} {'-' * 6} {'-' * 8} {'-' * 7} {'-' * 6} {'-' * 10} {'-' * 11} {'-' * 9}"
    )

    results = []
    for thresh in thresholds:
        for mult in boosts:
            r = simulate_boost(trades_df, rsi_lookup, thresh, mult)
            results.append({"thresh": thresh, "mult": mult, **r})
            sign_b = "+" if r["pnl_boost"] >= 0 else ""
            sign_i = "+" if r["improvement"] >= 0 else ""
            print(
                f"  RSI<{thresh:>2}   x{mult:.1f}   {r['boosted_trades']:>7}  "
                f"{r['pct_boosted']:>5.1f}%  {r['boosted_wr']:>5.1f}%  "
                f"${flat_pnl:>+9.2f} ${sign_b}{r['pnl_boost']:>9.2f} ${sign_i}{r['improvement']:>7.2f}"
            )

    # Find best
    best = max(results, key=lambda r: r["improvement"])
    print(f"\n  BEST: RSI<{best['thresh']} with x{best['mult']:.1f} boost")
    print(
        f"    Boosted {best['boosted_trades']}/{best['total']} trades ({best['pct_boosted']:.1f}%)"
    )
    print(f"    Boosted WR: {best['boosted_wr']:.1f}%")
    print(f"    PnL improvement: ${best['improvement']:+.2f}")
    print(f"    Flat: ${best['pnl_flat']:+.2f} -> Boosted: ${best['pnl_boost']:+.2f}")

    # Also test: only boost UP bets (where RSI signal is strongest)
    print(f"\n{'=' * 80}")
    print("  UP-ONLY BOOST (RSI signal strongest for UP bets)")
    print(f"{'=' * 80}")

    print(
        f"\n  {'Thresh':>7} {'Boost':>6} {'Boosted':>8} {'BstWR':>6} {'PnL Boost':>11} {'Improv':>9}"
    )
    print(f"  {'-' * 7} {'-' * 6} {'-' * 8} {'-' * 6} {'-' * 11} {'-' * 9}")

    up_results = []
    for thresh in thresholds:
        for mult in boosts:
            # Only boost UP bets
            total_pnl = 0.0
            boosted_n = 0
            boosted_wins = 0
            for _, row in trades_df.iterrows():
                rsi = get_rsi_at_ts(rsi_lookup, row["start_ts"])
                side = row["side"]
                hit = row["hit"]
                ma = row["max_ask"]

                boost = False
                if side == "UP" and not np.isnan(rsi) and rsi < thresh:
                    boost = True

                size = BASE_SIZE * mult if boost else BASE_SIZE
                pnl = ((1.0 - ma) * size) if hit else (-ma * size)
                total_pnl += pnl
                if boost:
                    boosted_n += 1
                    if hit:
                        boosted_wins += 1

            imp = total_pnl - flat_pnl
            bwr = boosted_wins / boosted_n * 100 if boosted_n else 0
            up_results.append(
                {
                    "thresh": thresh,
                    "mult": mult,
                    "pnl": total_pnl,
                    "imp": imp,
                    "bn": boosted_n,
                    "bwr": bwr,
                }
            )
            sign_b = "+" if total_pnl >= 0 else ""
            sign_i = "+" if imp >= 0 else ""
            print(
                f"  RSI<{thresh:>2}   x{mult:.1f}   {boosted_n:>7}  {bwr:>5.1f}%  "
                f"${sign_b}{total_pnl:>9.2f} ${sign_i}{imp:>7.2f}"
            )

    best_up = max(up_results, key=lambda r: r["imp"])
    print(f"\n  BEST UP-ONLY: RSI<{best_up['thresh']} with x{best_up['mult']:.1f}")
    print(f"    Boosted {best_up['bn']} UP trades, WR={best_up['bwr']:.1f}%")
    print(f"    PnL: ${best_up['pnl']:+.2f} (improvement: ${best_up['imp']:+.2f})")

    # Plot
    if HAS_MPL:
        OUTPUT_DIR.mkdir(exist_ok=True)
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))
        fig.suptitle("RSI Boost Backtest - Telonex 74d", fontsize=13, fontweight="bold")

        # Heatmap: threshold vs multiplier (both-side boost)
        imp_matrix = np.zeros((len(thresholds), len(boosts)))
        for r in results:
            i = thresholds.index(r["thresh"])
            j = boosts.index(r["mult"])
            imp_matrix[i][j] = r["improvement"]

        im = ax1.imshow(imp_matrix, aspect="auto", cmap="RdYlGn")
        ax1.set_xticks(range(len(boosts)))
        ax1.set_xticklabels([f"x{b}" for b in boosts])
        ax1.set_yticks(range(len(thresholds)))
        ax1.set_yticklabels([f"RSI<{t}" for t in thresholds])
        ax1.set_title("PnL Improvement ($) - Both sides")
        for i in range(len(thresholds)):
            for j in range(len(boosts)):
                ax1.text(
                    j,
                    i,
                    f"${imp_matrix[i][j]:+.0f}",
                    ha="center",
                    va="center",
                    fontsize=9,
                )
        plt.colorbar(im, ax=ax1)

        # Heatmap: UP-only
        imp_up = np.zeros((len(thresholds), len(boosts)))
        for r in up_results:
            i = thresholds.index(r["thresh"])
            j = boosts.index(r["mult"])
            imp_up[i][j] = r["imp"]

        im2 = ax2.imshow(imp_up, aspect="auto", cmap="RdYlGn")
        ax2.set_xticks(range(len(boosts)))
        ax2.set_xticklabels([f"x{b}" for b in boosts])
        ax2.set_yticks(range(len(thresholds)))
        ax2.set_yticklabels([f"RSI<{t}" for t in thresholds])
        ax2.set_title("PnL Improvement ($) - UP bets only")
        for i in range(len(thresholds)):
            for j in range(len(boosts)):
                ax2.text(
                    j, i, f"${imp_up[i][j]:+.0f}", ha="center", va="center", fontsize=9
                )
        plt.colorbar(im2, ax=ax2)

        plt.tight_layout()
        path = OUTPUT_DIR / "rsi_boost_heatmap.png"
        plt.savefig(path, dpi=150)
        print(f"\n  Plot saved: {path}")

    print(f"\n{'=' * 80}")
    print("  Done!")


if __name__ == "__main__":
    main()
