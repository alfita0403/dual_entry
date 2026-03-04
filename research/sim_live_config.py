"""
Simulate the EXACT live config on CSV data with RSI boost.

Config: 5 ETH patterns, max_ask=0.60, RSI(7)@5m boost
  UP bets:   base $5, boost $8 when RSI < 35
  DOWN bets: base $5, boost $8 when RSI > 55

Outputs equity curve PNG + trade-by-trade log.
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

# === EXACT LIVE CONFIG ===
RULES = [
    {"pattern": "DDDDD", "side": "UP", "coins": ["ETH"], "max_ask": 0.60},
    {"pattern": "DDDD", "side": "UP", "coins": ["ETH"], "max_ask": 0.60},
    {"pattern": "DDD", "side": "UP", "coins": ["ETH"], "max_ask": 0.60},
    {"pattern": "UUUU", "side": "DOWN", "coins": ["ETH"], "max_ask": 0.60},
    {"pattern": "UUU", "side": "DOWN", "coins": ["ETH"], "max_ask": 0.60},
]
BASE_SIZE = 5.0
BOOST_SIZE = 8.0
UP_THRESH = 35.0
DN_THRESH = 55.0
RSI_PERIOD = 7
TF = 300


def get_rsi(rsi_lookup, ts):
    snapped = (ts // TF) * TF
    last = snapped - TF
    for c in [last, last - TF]:
        if c in rsi_lookup:
            v = rsi_lookup[c]
            if np.isfinite(v):
                return v
    return np.nan


def main():
    print("=" * 70)
    print("  LIVE CONFIG SIMULATION - CSV real prices")
    print("  RSI(7) boost: UP<35 -> $8, DN>55 -> $8, base $5")
    print("  max_ask=0.60, ETH-only")
    print("=" * 70)

    print("\n  Loading CSV...")
    raw = load_csvs()
    seqs = csv_build_cycles(raw)
    trades_df = find_trades_csv(seqs, RULES, size=BASE_SIZE, fee=0.0)
    print(f"  {len(trades_df)} trades")
    if trades_df.empty:
        print("  No trades found!")
        return

    up_n = len(trades_df[trades_df["side"] == "UP"])
    dn_n = len(trades_df[trades_df["side"] == "DOWN"])
    print(f"  UP={up_n}, DOWN={dn_n}")

    # Fetch ETHUSDT RSI
    ts_min = int(trades_df["start_ts"].min())
    ts_max = int(trades_df["start_ts"].max())
    warmup = 50 * TF
    kl = fetch_klines("ETHUSDT", "5m", (ts_min - warmup) * 1000, (ts_max + 600) * 1000)
    rsi_arr = compute_rsi(kl["close"].values, RSI_PERIOD)
    kl["rsi"] = rsi_arr
    kl["ts_s"] = (kl["open_time_ms"] / 1000).astype(int)
    rsi_lookup = {
        int(r["ts_s"]): r["rsi"] for _, r in kl.iterrows() if np.isfinite(r["rsi"])
    }
    print(f"  {len(rsi_lookup)} RSI values")

    # Simulate
    flat_equity = [0.0]
    boost_equity = [0.0]
    boosted_flags = []
    rsi_values = []

    for _, row in trades_df.iterrows():
        rsi = get_rsi(rsi_lookup, row["start_ts"])
        side = row["side"]
        hit = row["hit"]
        entry = row["entry_ask"]

        flat_pnl = ((1.0 - entry) * BASE_SIZE) if hit else (-entry * BASE_SIZE)

        boost = False
        if not np.isnan(rsi):
            if side == "UP" and rsi < UP_THRESH:
                boost = True
            elif side == "DOWN" and rsi > DN_THRESH:
                boost = True

        size = BOOST_SIZE if boost else BASE_SIZE
        boost_pnl = ((1.0 - entry) * size) if hit else (-entry * size)

        flat_equity.append(flat_equity[-1] + flat_pnl)
        boost_equity.append(boost_equity[-1] + boost_pnl)
        boosted_flags.append(boost)
        rsi_values.append(rsi)

    # Stats
    n = len(trades_df)
    wins = int(trades_df["hit"].sum())
    wr = wins / n * 100
    flat_final = flat_equity[-1]
    boost_final = boost_equity[-1]
    n_boosted = sum(boosted_flags)
    boosted_wins = sum(
        1 for i, b in enumerate(boosted_flags) if b and trades_df.iloc[i]["hit"]
    )
    boosted_wr = boosted_wins / n_boosted * 100 if n_boosted else 0
    avg_entry = trades_df["entry_ask"].mean()

    print(f"\n  Results:")
    print(f"    Trades:      {n} ({up_n} UP, {dn_n} DOWN)")
    print(f"    WR:          {wr:.1f}% ({wins}W/{n - wins}L)")
    print(f"    Avg entry:   ${avg_entry:.4f}")
    print(f"    Flat $5:     ${flat_final:+.2f}")
    print(
        f"    Boost:       ${boost_final:+.2f} ({n_boosted} boosted, {boosted_wr:.1f}% WR)"
    )
    print(f"    Improvement: ${boost_final - flat_final:+.2f}")

    # Per-day
    print(f"\n  Per-day:")
    for date in sorted(trades_df["date"].unique()):
        day = trades_df[trades_df["date"] == date]
        dw = int(day["hit"].sum())
        dn = len(day)
        dwr = dw / dn * 100 if dn else 0
        dpnl = day["pnl"].sum()
        print(f"    {date}: {dn} trades, {dwr:.0f}% WR, ${dpnl:+.2f}")

    # Trade log
    print(f"\n  Trades ([B]=boosted):")
    for i in range(n):
        row = trades_df.iloc[i]
        rsi = rsi_values[i]
        b = "B" if boosted_flags[i] else " "
        w = "W" if row["hit"] else "L"
        rsi_s = f"RSI={rsi:.0f}" if not np.isnan(rsi) else "RSI=--"
        pnl_val = boost_equity[i + 1] - boost_equity[i]
        print(
            f"    {i + 1:>3}. [{b}] {row['coin']} {row['pattern']}->{row['side']}"
            f" @{row['entry_ask']:.3f} {w}  {rsi_s}  pnl=${pnl_val:+.2f}"
            f"  eq=${boost_equity[i + 1]:+.2f}"
        )

    # Plot
    if HAS_MPL:
        OUTPUT_DIR.mkdir(exist_ok=True)
        fig, (ax1, ax2) = plt.subplots(
            2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]}
        )
        fig.suptitle(
            f"Live Config Simulation - CSV {len(trades_df['date'].unique())} days,"
            f" {n} trades\n"
            f"ETH-only, max_ask=0.60, RSI(7) boost UP<35/DN>55 ($5->$8)",
            fontsize=12,
            fontweight="bold",
        )

        x = range(len(flat_equity))
        ax1.plot(
            x,
            flat_equity,
            "b-",
            alpha=0.5,
            linewidth=1,
            label=f"Flat $5: ${flat_final:+.2f}",
        )
        ax1.plot(
            x,
            boost_equity,
            "g-",
            linewidth=2,
            label=f"RSI Boost: ${boost_final:+.2f}",
        )
        ax1.axhline(0, color="gray", ls="--", alpha=0.3)

        for i, b in enumerate(boosted_flags):
            if b:
                c = "lime" if boost_equity[i + 1] > boost_equity[i] else "red"
                ax1.scatter(
                    i + 1, boost_equity[i + 1], color=c, s=30, zorder=5, alpha=0.7
                )

        ax1.set_ylabel("Equity ($)")
        ax1.legend(loc="upper left", fontsize=10)
        ax1.grid(True, alpha=0.2)

        rsi_x = range(1, len(rsi_values) + 1)
        colors = []
        for i, r in enumerate(rsi_values):
            if np.isnan(r):
                colors.append("gray")
            elif (trades_df.iloc[i]["side"] == "UP" and r < UP_THRESH) or (
                trades_df.iloc[i]["side"] == "DOWN" and r > DN_THRESH
            ):
                colors.append("green")
            else:
                colors.append("gray")
        ax2.bar(
            rsi_x,
            [r if not np.isnan(r) else 0 for r in rsi_values],
            color=colors,
            alpha=0.6,
        )
        ax2.axhline(
            UP_THRESH,
            color="blue",
            ls="--",
            alpha=0.5,
            label=f"UP boost < {UP_THRESH:.0f}",
        )
        ax2.axhline(
            DN_THRESH,
            color="red",
            ls="--",
            alpha=0.5,
            label=f"DN boost > {DN_THRESH:.0f}",
        )
        ax2.set_ylabel("RSI(7)")
        ax2.set_xlabel("Trade #")
        ax2.legend(fontsize=8)
        ax2.set_ylim(0, 100)
        ax2.grid(True, alpha=0.2)

        plt.tight_layout()
        path = OUTPUT_DIR / "live_config_csv_simulation.png"
        plt.savefig(path, dpi=150)
        print(f"\n  Plot saved: {path}")

    print(f"\n{'=' * 70}")
    print("  Done!")


if __name__ == "__main__":
    main()
