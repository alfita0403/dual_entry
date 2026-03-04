"""
RSI boost test on CSV - BOTH sides.
  UP bets: boost when RSI < threshold (oversold -> bounce)
  DOWN bets: boost when RSI > threshold (overbought -> drop)
Base $5, boost to $8.
"""

import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
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
    for c in [last_completed, last_completed - TF_SECONDS]:
        if c in rsi_lookup:
            v = rsi_lookup[c]
            if np.isfinite(v):
                return v
    return np.nan


def run_boost(valid, rsi_lookup, up_thresh, dn_thresh):
    """Run one config. Returns dict with results."""
    pnl = 0.0
    bn_up = bw_up = bn_dn = bw_dn = 0

    for _, row in valid.iterrows():
        rsi = row["rsi"]
        side = row["side"]
        hit = row["hit"]
        ea = row["entry_ask"]

        boost = False
        if side == "UP" and up_thresh is not None and rsi < up_thresh:
            boost = True
        elif side == "DOWN" and dn_thresh is not None and rsi > dn_thresh:
            boost = True

        size = BOOST_SIZE if boost else BASE_SIZE
        pnl += ((1.0 - ea) * size) if hit else (-ea * size)

        if boost and side == "UP":
            bn_up += 1
            if hit:
                bw_up += 1
        elif boost and side == "DOWN":
            bn_dn += 1
            if hit:
                bw_dn += 1

    return {
        "pnl": pnl,
        "bn_up": bn_up,
        "bw_up": bw_up,
        "bn_dn": bn_dn,
        "bw_dn": bw_dn,
    }


def main():
    print("=" * 80)
    print(f"  RSI BOOST BOTH SIDES - CSV (real prices)")
    print(f"  Base ${BASE_SIZE} -> ${BOOST_SIZE}")
    print(f"  UP bets boost when RSI LOW  |  DOWN bets boost when RSI HIGH")
    print("=" * 80)

    # Load
    print("\n  Loading CSV...")
    raw = load_csvs()
    csv_seqs = csv_build_cycles(raw)
    trades_df = find_trades_csv(csv_seqs, RULES, size=BASE_SIZE, fee=0.0)
    print(f"  {len(trades_df)} trades")

    ts_min, ts_max = int(trades_df["start_ts"].min()), int(trades_df["start_ts"].max())
    kl = fetch_klines(
        "ETHUSDT", "5m", (ts_min - 50 * TF_SECONDS) * 1000, (ts_max + 600) * 1000
    )
    kl["rsi"] = compute_rsi(kl["close"].values, RSI_PERIOD)
    kl["ts_s"] = (kl["open_time_ms"] / 1000).astype(int)
    rsi_lookup = {
        int(r["ts_s"]): r["rsi"] for _, r in kl.iterrows() if np.isfinite(r["rsi"])
    }

    trades_df["rsi"] = trades_df["start_ts"].apply(
        lambda ts: get_rsi_at_ts(rsi_lookup, ts)
    )
    valid = trades_df[trades_df["rsi"].notna()].copy()

    flat_pnl = valid["pnl"].sum()
    n_up = len(valid[valid["side"] == "UP"])
    n_dn = len(valid[valid["side"] == "DOWN"])
    print(f"  {len(valid)} trades ({n_up} UP, {n_dn} DOWN), Flat PnL=${flat_pnl:+.2f}")

    # ===================================================================
    # SECTION 1: UP bets only (RSI < thresh)
    # ===================================================================
    print(f"\n{'=' * 80}")
    print("  UP BETS: boost to $8 when RSI < threshold")
    print(f"{'=' * 80}")
    thresholds = [25, 30, 35, 40, 45, 50, 55, 60]
    print(f"  {'Thresh':>7} {'Boosted':>8} {'BstWR':>7} {'PnL':>10} {'Improv':>9}")
    print(f"  {'-' * 7} {'-' * 8} {'-' * 7} {'-' * 10} {'-' * 9}")
    for t in thresholds:
        r = run_boost(valid, rsi_lookup, up_thresh=t, dn_thresh=None)
        bwr = r["bw_up"] / r["bn_up"] * 100 if r["bn_up"] else 0
        imp = r["pnl"] - flat_pnl
        print(
            f"  RSI<{t:>2}   {r['bn_up']:>7}  {bwr:>5.1f}%  ${r['pnl']:>+9.2f} ${imp:>+8.2f}"
        )

    # ===================================================================
    # SECTION 2: DOWN bets only (RSI > thresh)
    # ===================================================================
    print(f"\n{'=' * 80}")
    print("  DOWN BETS: boost to $8 when RSI > threshold")
    print(f"{'=' * 80}")
    print(f"  {'Thresh':>7} {'Boosted':>8} {'BstWR':>7} {'PnL':>10} {'Improv':>9}")
    print(f"  {'-' * 7} {'-' * 8} {'-' * 7} {'-' * 10} {'-' * 9}")
    for t in [45, 50, 55, 60, 65, 70, 75, 80]:
        r = run_boost(valid, rsi_lookup, up_thresh=None, dn_thresh=t)
        bwr = r["bw_dn"] / r["bn_dn"] * 100 if r["bn_dn"] else 0
        imp = r["pnl"] - flat_pnl
        print(
            f"  RSI>{t:>2}   {r['bn_dn']:>7}  {bwr:>5.1f}%  ${r['pnl']:>+9.2f} ${imp:>+8.2f}"
        )

    # ===================================================================
    # SECTION 3: Both sides combined
    # ===================================================================
    print(f"\n{'=' * 80}")
    print("  BOTH SIDES: UP boost RSI<X + DOWN boost RSI>Y")
    print(f"{'=' * 80}")
    up_options = [30, 40, 45, 50]
    dn_options = [50, 55, 60, 70]
    print(
        f"  {'UP<':>5} {'DN>':>5} {'BstUP':>6} {'UPWR':>6} {'BstDN':>6} {'DNWR':>6} {'PnL':>10} {'Improv':>9}"
    )
    print(
        f"  {'-' * 5} {'-' * 5} {'-' * 6} {'-' * 6} {'-' * 6} {'-' * 6} {'-' * 10} {'-' * 9}"
    )

    best_imp = -999
    best_cfg = None
    for ut in up_options:
        for dt in dn_options:
            r = run_boost(valid, rsi_lookup, up_thresh=ut, dn_thresh=dt)
            uwr = r["bw_up"] / r["bn_up"] * 100 if r["bn_up"] else 0
            dwr = r["bw_dn"] / r["bn_dn"] * 100 if r["bn_dn"] else 0
            imp = r["pnl"] - flat_pnl
            if imp > best_imp:
                best_imp = imp
                best_cfg = (ut, dt, r)
            print(
                f"    {ut:>2}   {dt:>3}  {r['bn_up']:>5}  {uwr:>5.1f}% {r['bn_dn']:>5}  {dwr:>5.1f}%  "
                f"${r['pnl']:>+9.2f} ${imp:>+8.2f}"
            )

    ut, dt, r = best_cfg
    print(f"\n  BEST: UP boost RSI<{ut}, DOWN boost RSI>{dt}")
    print(
        f"  Flat: ${flat_pnl:+.2f} -> Boosted: ${r['pnl']:+.2f} (improvement: ${best_imp:+.2f})"
    )

    # ===================================================================
    # Per-pattern at best config
    # ===================================================================
    print(f"\n  Per-pattern (UP<{ut}, DN>{dt}):")
    print(
        f"  {'Pattern':>8} {'Side':>5} {'N':>4} {'Bst':>4} {'WR':>6} {'BstWR':>6} {'FlatPnL':>9} {'BstPnL':>9}"
    )
    print(
        f"  {'-' * 8} {'-' * 5} {'-' * 4} {'-' * 4} {'-' * 6} {'-' * 6} {'-' * 9} {'-' * 9}"
    )

    for pat in ["DDDDD", "DDDD", "DDD", "UUUU", "UUU"]:
        pv = valid[valid["pattern"] == pat]
        if len(pv) == 0:
            continue
        side = pv["side"].iloc[0]
        wr = pv["hit"].mean() * 100
        flat_p = pv["pnl"].sum()

        boost_p = 0.0
        bn = bw = 0
        for _, row in pv.iterrows():
            boost = False
            if side == "UP" and row["rsi"] < ut:
                boost = True
            elif side == "DOWN" and row["rsi"] > dt:
                boost = True
            size = BOOST_SIZE if boost else BASE_SIZE
            boost_p += (
                ((1.0 - row["entry_ask"]) * size)
                if row["hit"]
                else (-row["entry_ask"] * size)
            )
            if boost:
                bn += 1
                if row["hit"]:
                    bw += 1

        bwr = bw / bn * 100 if bn else 0
        print(
            f"  {pat:>8} {side:>5} {len(pv):>4} {bn:>4} {wr:>5.1f}% {bwr:>5.1f}%  "
            f"${flat_p:>+8.2f} ${boost_p:>+8.2f}"
        )

    # Plot
    if HAS_MPL:
        OUTPUT_DIR.mkdir(exist_ok=True)
        fig, axes = plt.subplots(1, 3, figsize=(18, 6))
        fig.suptitle(
            f"RSI Boost CSV - ${BASE_SIZE} base / ${BOOST_SIZE} boost",
            fontsize=13,
            fontweight="bold",
        )

        # Equity curve at best config
        ax = axes[0]
        sorted_t = valid.sort_values("start_ts")
        cum_flat = sorted_t["pnl"].cumsum()
        cum_boost = []
        running = 0
        for _, row in sorted_t.iterrows():
            boost = False
            if row["side"] == "UP" and row["rsi"] < ut:
                boost = True
            elif row["side"] == "DOWN" and row["rsi"] > dt:
                boost = True
            size = BOOST_SIZE if boost else BASE_SIZE
            pnl = (
                ((1.0 - row["entry_ask"]) * size)
                if row["hit"]
                else (-row["entry_ask"] * size)
            )
            running += pnl
            cum_boost.append(running)

        ax.plot(
            range(len(cum_flat)),
            cum_flat.values,
            label=f"Flat ${BASE_SIZE}",
            color="blue",
        )
        ax.plot(
            range(len(cum_boost)),
            cum_boost,
            label=f"Boost ${BOOST_SIZE}",
            color="green",
        )
        ax.axhline(y=0, color="black", linewidth=0.5)
        ax.set_title(f"Equity: Flat vs Boost (UP<{ut}, DN>{dt})")
        ax.set_xlabel("Trade #")
        ax.set_ylabel("PnL ($)")
        ax.legend()
        ax.grid(True, alpha=0.3)

        # UP bets WR by RSI bucket
        ax = axes[1]
        up = valid[valid["side"] == "UP"].copy()
        bins = list(range(0, 101, 10))
        up["rsi_bin"] = pd.cut(up["rsi"], bins=bins)
        wr_up = up.groupby("rsi_bin", observed=True)["hit"].agg(["mean", "count"])
        wr_up = wr_up[wr_up["count"] >= 3]
        x = [f"{int(b.left)}-{int(b.right)}" for b in wr_up.index]
        colors = ["green" if b.right <= ut else "gray" for b in wr_up.index]
        ax.bar(range(len(x)), wr_up["mean"] * 100, color=colors, alpha=0.8)
        ax.set_xticks(range(len(x)))
        ax.set_xticklabels(x, rotation=45, fontsize=8)
        for i, (_, row) in enumerate(wr_up.iterrows()):
            ax.text(
                i,
                row["mean"] * 100 + 1,
                f"n={int(row['count'])}",
                ha="center",
                fontsize=7,
            )
        ax.axhline(y=50, color="red", linestyle="--", alpha=0.5)
        ax.set_title("UP bets WR by RSI (green=boosted)")
        ax.set_ylabel("Win Rate %")

        # DOWN bets WR by RSI bucket
        ax = axes[2]
        dn = valid[valid["side"] == "DOWN"].copy()
        dn["rsi_bin"] = pd.cut(dn["rsi"], bins=bins)
        wr_dn = dn.groupby("rsi_bin", observed=True)["hit"].agg(["mean", "count"])
        wr_dn = wr_dn[wr_dn["count"] >= 3]
        x = [f"{int(b.left)}-{int(b.right)}" for b in wr_dn.index]
        colors = ["red" if b.left >= dt else "gray" for b in wr_dn.index]
        ax.bar(range(len(x)), wr_dn["mean"] * 100, color=colors, alpha=0.8)
        ax.set_xticks(range(len(x)))
        ax.set_xticklabels(x, rotation=45, fontsize=8)
        for i, (_, row) in enumerate(wr_dn.iterrows()):
            ax.text(
                i,
                row["mean"] * 100 + 1,
                f"n={int(row['count'])}",
                ha="center",
                fontsize=7,
            )
        ax.axhline(y=50, color="red", linestyle="--", alpha=0.5)
        ax.set_title("DOWN bets WR by RSI (red=boosted)")
        ax.set_ylabel("Win Rate %")

        plt.tight_layout()
        path = OUTPUT_DIR / "rsi_boost_csv_both.png"
        plt.savefig(path, dpi=150)
        print(f"\n  Plot saved: {path}")

    print("\n  Done!")


if __name__ == "__main__":
    main()
