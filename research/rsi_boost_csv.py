"""
RSI boost test on CSV (real prices).
Base $5, boost to $8 on UP bets when ETH RSI(7) < threshold.
"""

import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from pathlib import Path

from backtest import (
    load_csvs,
    csv_build_cycles,
    find_trades_csv,
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
    {"pattern": "DDDDD", "side": "UP", "coins": ["ETH"], "max_ask": 0.60},
    {"pattern": "DDDD", "side": "UP", "coins": ["ETH"], "max_ask": 0.60},
    {"pattern": "UUUU", "side": "DOWN", "coins": ["ETH"], "max_ask": 0.60},
    {"pattern": "UUU", "side": "DOWN", "coins": ["ETH"], "max_ask": 0.60},
    {"pattern": "DDD", "side": "UP", "coins": ["ETH"], "max_ask": 0.60},
]

RSI_PERIOD = 7
TF_SECONDS = 300
BASE_SIZE = 5.0
BOOST_SIZE = 8.0


def get_rsi_at_ts(rsi_lookup, ts):
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
    print("  RSI BOOST - CSV (real prices)")
    print(f"  Base size: ${BASE_SIZE}  |  Boost size: ${BOOST_SIZE}")
    print(f"  Boost: UP bets when RSI < threshold")
    print("=" * 80)

    # Load CSV
    print("\n  Loading CSV data...")
    raw = load_csvs()
    print("  Building cycles...")
    csv_seqs = csv_build_cycles(raw)

    # Get trades with real prices
    trades_df = find_trades_csv(csv_seqs, RULES, size=BASE_SIZE, fee=0.0)
    print(f"  {len(trades_df)} trades (max_ask=0.60, real entry prices)")

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

    # Attach RSI to trades
    trades_df["rsi"] = trades_df["start_ts"].apply(
        lambda ts: get_rsi_at_ts(rsi_lookup, ts)
    )
    valid = trades_df[trades_df["rsi"].notna()].copy()
    print(f"  {len(valid)}/{len(trades_df)} trades with RSI")

    # Flat baseline (all $5)
    flat_pnl = valid["pnl"].sum()
    flat_wr = valid["hit"].mean() * 100
    print(
        f"\n  Flat baseline: {len(valid)} trades, WR={flat_wr:.1f}%, PnL=${flat_pnl:+.2f}"
    )

    # Test thresholds
    thresholds = [25, 30, 35, 40, 45, 50]

    print(
        f"\n  {'Thresh':>7} {'Boosted':>8} {'BstWR':>6} {'Flat PnL':>10} {'Boost PnL':>11} {'Improv':>9} {'$/day':>7}"
    )
    print(f"  {'-' * 7} {'-' * 8} {'-' * 6} {'-' * 10} {'-' * 11} {'-' * 9} {'-' * 7}")

    results = []
    for thresh in thresholds:
        pnl_boost = 0.0
        boosted_n = 0
        boosted_wins = 0

        for _, row in valid.iterrows():
            rsi = row["rsi"]
            side = row["side"]
            hit = row["hit"]
            entry_ask = row["entry_ask"]

            # Only boost UP bets when RSI < threshold
            boost = side == "UP" and rsi < thresh
            size = BOOST_SIZE if boost else BASE_SIZE

            if hit:
                pnl = (1.0 - entry_ask) * size
            else:
                pnl = -entry_ask * size

            pnl_boost += pnl
            if boost:
                boosted_n += 1
                if hit:
                    boosted_wins += 1

        imp = pnl_boost - flat_pnl
        bwr = boosted_wins / boosted_n * 100 if boosted_n > 0 else 0
        days = 5
        results.append(
            {
                "thresh": thresh,
                "pnl": pnl_boost,
                "imp": imp,
                "bn": boosted_n,
                "bwr": bwr,
            }
        )

        print(
            f"  RSI<{thresh:>2}   {boosted_n:>7}  {bwr:>5.1f}%  "
            f"${flat_pnl:>+9.2f} ${pnl_boost:>+10.2f} ${imp:>+8.2f} ${imp / days:>+6.2f}"
        )

    # Per-pattern detail for best threshold
    best = max(results, key=lambda r: r["imp"])
    thresh = best["thresh"]

    print(f"\n{'=' * 80}")
    print(f"  BEST: RSI<{thresh} (boost {best['bn']} UP trades to ${BOOST_SIZE})")
    print(
        f"  Flat: ${flat_pnl:+.2f} -> Boosted: ${best['pnl']:+.2f} ({best['imp']:+.2f})"
    )
    print(f"{'=' * 80}")

    # Per-pattern breakdown at best threshold
    print(f"\n  Per-pattern detail (RSI<{thresh}, UP bets boosted to ${BOOST_SIZE}):")
    print(
        f"  {'Pattern':>8} {'Side':>5} {'N':>5} {'Bst':>4} {'WR':>6} {'FlatPnL':>9} {'BstPnL':>9} {'BstWR':>6}"
    )
    print(
        f"  {'-' * 8} {'-' * 5} {'-' * 5} {'-' * 4} {'-' * 6} {'-' * 9} {'-' * 9} {'-' * 6}"
    )

    for pat in ["DDDDD", "DDDD", "DDD", "UUUU", "UUU"]:
        pv = valid[valid["pattern"] == pat]
        if len(pv) == 0:
            continue
        side = pv["side"].iloc[0]
        wr = pv["hit"].mean() * 100
        flat_p = pv["pnl"].sum()

        boost_p = 0.0
        bn = 0
        bw = 0
        for _, row in pv.iterrows():
            boost = side == "UP" and row["rsi"] < thresh
            size = BOOST_SIZE if boost else BASE_SIZE
            if row["hit"]:
                boost_p += (1.0 - row["entry_ask"]) * size
            else:
                boost_p += -row["entry_ask"] * size
            if boost:
                bn += 1
                if row["hit"]:
                    bw += 1

        bwr = bw / bn * 100 if bn > 0 else 0
        print(
            f"  {pat:>8} {side:>5} {len(pv):>5} {bn:>4} {wr:>5.1f}%  "
            f"${flat_p:>+8.2f} ${boost_p:>+8.2f} {bwr:>5.1f}%"
        )

    # Equity curve comparison
    if HAS_MPL:
        OUTPUT_DIR.mkdir(exist_ok=True)
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))
        fig.suptitle(
            f"RSI Boost CSV - $5 base / ${BOOST_SIZE} on UP when RSI<{thresh}",
            fontsize=13,
            fontweight="bold",
        )

        # Equity curves
        sorted_trades = valid.sort_values("start_ts")
        cum_flat = sorted_trades["pnl"].cumsum()

        cum_boost = []
        running = 0
        for _, row in sorted_trades.iterrows():
            boost = row["side"] == "UP" and row["rsi"] < thresh
            size = BOOST_SIZE if boost else BASE_SIZE
            if row["hit"]:
                pnl = (1.0 - row["entry_ask"]) * size
            else:
                pnl = -row["entry_ask"] * size
            running += pnl
            cum_boost.append(running)

        ax1.plot(
            range(len(cum_flat)),
            cum_flat.values,
            label=f"Flat ${BASE_SIZE}",
            color="blue",
            alpha=0.8,
        )
        ax1.plot(
            range(len(cum_boost)),
            cum_boost,
            label=f"RSI boost ${BOOST_SIZE}",
            color="green",
            alpha=0.8,
        )
        ax1.axhline(y=0, color="black", linewidth=0.5)
        ax1.set_xlabel("Trade #")
        ax1.set_ylabel("Cumulative PnL ($)")
        ax1.set_title("Equity Curve: Flat vs RSI Boost")
        ax1.legend()
        ax1.grid(True, alpha=0.3)

        # RSI distribution of boosted trades
        up_trades = valid[valid["side"] == "UP"]
        boosted = up_trades[up_trades["rsi"] < thresh]
        not_boosted = up_trades[up_trades["rsi"] >= thresh]

        ax2.hist(
            boosted["rsi"],
            bins=20,
            alpha=0.6,
            color="green",
            label=f"Boosted (N={len(boosted)}, WR={boosted['hit'].mean() * 100:.1f}%)",
        )
        ax2.hist(
            not_boosted["rsi"],
            bins=20,
            alpha=0.6,
            color="gray",
            label=f"Normal (N={len(not_boosted)}, WR={not_boosted['hit'].mean() * 100:.1f}%)",
        )
        ax2.set_xlabel("RSI(7)")
        ax2.set_ylabel("Count")
        ax2.set_title("RSI of UP Bets: Boosted vs Normal")
        ax2.legend()

        plt.tight_layout()
        path = OUTPUT_DIR / "rsi_boost_csv.png"
        plt.savefig(path, dpi=150)
        print(f"\n  Plot saved: {path}")

    print("\n  Done!")


if __name__ == "__main__":
    main()
