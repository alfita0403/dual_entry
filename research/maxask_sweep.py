"""
max_ask sweep: test all max_ask from 0.40 to 0.80 on CSV and Telonex.
5 ETH-only patterns, combined PnL at each level.
Saves PNGs to research/pngs/
"""

import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from pathlib import Path

# Import backtest functions
from backtest import (
    load_csvs,
    csv_build_cycles,
    find_trades_csv,
    load_telonex,
    build_sequences,
    find_trades,
    OUTPUT_DIR,
)

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    HAS_MPL = True
except ImportError:
    HAS_MPL = False

# The 5 ETH patterns
BASE_RULES = [
    {"pattern": "DDDDD", "side": "UP", "coins": ["ETH"]},
    {"pattern": "DDDD", "side": "UP", "coins": ["ETH"]},
    {"pattern": "UUUU", "side": "DOWN", "coins": ["ETH"]},
    {"pattern": "UUU", "side": "DOWN", "coins": ["ETH"]},
    {"pattern": "DDD", "side": "UP", "coins": ["ETH"]},
]

MAX_ASK_VALUES = np.arange(0.40, 0.81, 0.01)
MAX_ASK_VALUES = np.round(MAX_ASK_VALUES, 2)

SIZE = 5.0


def make_rules(max_ask):
    return [{**r, "max_ask": max_ask} for r in BASE_RULES]


def sweep(mode, seqs):
    """Run sweep, return list of dicts with results."""
    results = []
    find_fn = find_trades_csv if mode == "csv" else find_trades

    for ma in MAX_ASK_VALUES:
        rules = make_rules(ma)
        df = find_fn(seqs, rules, size=SIZE, fee=0.0)

        if len(df) == 0:
            results.append(
                {
                    "max_ask": ma,
                    "trades": 0,
                    "wins": 0,
                    "wr": 0,
                    "pnl": 0,
                    "avg_entry": 0,
                }
            )
            continue

        trades = len(df)
        wins = int(df["hit"].sum())
        wr = wins / trades * 100
        pnl = df["pnl"].sum()
        avg_entry = df["entry_ask"].mean() if "entry_ask" in df.columns else ma

        results.append(
            {
                "max_ask": ma,
                "trades": trades,
                "wins": wins,
                "wr": wr,
                "pnl": pnl,
                "avg_entry": avg_entry,
            }
        )

    return results


def print_table(results, title):
    print(f"\n{'=' * 80}")
    print(f"  {title}")
    print(f"{'=' * 80}")
    print(
        f"  {'MaxAsk':>8} {'Trades':>7} {'Wins':>6} {'WR':>7} {'PnL':>11} {'AvgEntry':>9}  Note"
    )
    print(f"  {'-' * 8} {'-' * 7} {'-' * 6} {'-' * 7} {'-' * 11} {'-' * 9}  {'-' * 12}")

    best_pnl = max(r["pnl"] for r in results)

    for r in results:
        if r["trades"] == 0:
            print(f"  ${r['max_ask']:.2f}         0      0   0.0% $      0.00     --")
            continue
        marker = "  <-- BEST" if r["pnl"] == best_pnl and r["pnl"] > 0 else ""
        sign = "+" if r["pnl"] >= 0 else ""
        print(
            f"  ${r['max_ask']:.2f}   {r['trades']:>7} {r['wins']:>6} "
            f"{r['wr']:>6.1f}% ${sign}{r['pnl']:>9.2f} "
            f"${r['avg_entry']:.3f}{marker}"
        )


def plot_sweep(csv_results, telonex_results):
    if not HAS_MPL:
        print("\n  No matplotlib - skipping plots")
        return

    OUTPUT_DIR.mkdir(exist_ok=True)

    fig, axes = plt.subplots(2, 2, figsize=(16, 12))
    fig.suptitle(
        "max_ask Sweep: 5 ETH Patterns (0.40 - 0.80)", fontsize=14, fontweight="bold"
    )

    for idx, (results, label) in enumerate(
        [(csv_results, "CSV (5d, real prices)"), (telonex_results, "Telonex (74d)")]
    ):
        ma_vals = [r["max_ask"] for r in results]
        pnl_vals = [r["pnl"] for r in results]
        wr_vals = [r["wr"] for r in results]
        trade_vals = [r["trades"] for r in results]

        # PnL plot
        ax = axes[0][idx]
        colors = ["green" if p >= 0 else "red" for p in pnl_vals]
        ax.bar(ma_vals, pnl_vals, width=0.008, color=colors, alpha=0.8)
        ax.axhline(y=0, color="black", linewidth=0.5)
        best_idx = np.argmax(pnl_vals)
        ax.axvline(
            x=ma_vals[best_idx],
            color="blue",
            linestyle="--",
            alpha=0.5,
            label=f"Best: ${ma_vals[best_idx]:.2f} (${pnl_vals[best_idx]:+.0f})",
        )
        ax.set_title(f"PnL - {label}")
        ax.set_xlabel("max_ask")
        ax.set_ylabel("PnL ($)")
        ax.legend()
        ax.grid(True, alpha=0.3)

        # WR + trades plot
        ax2 = axes[1][idx]
        color1 = "tab:blue"
        ax2.plot(
            ma_vals, wr_vals, color=color1, marker=".", markersize=3, label="Win Rate %"
        )
        # Breakeven line (WR = max_ask * 100)
        be_line = [ma * 100 for ma in ma_vals]
        ax2.plot(
            ma_vals,
            be_line,
            color="red",
            linestyle="--",
            alpha=0.7,
            label="Breakeven WR",
        )
        ax2.set_xlabel("max_ask")
        ax2.set_ylabel("Win Rate %", color=color1)
        ax2.tick_params(axis="y", labelcolor=color1)
        ax2.set_ylim(40, 70)
        ax2.legend(loc="upper left")
        ax2.grid(True, alpha=0.3)
        ax2.set_title(f"WR vs Breakeven - {label}")

        # Trade count on secondary axis
        ax3 = ax2.twinx()
        ax3.bar(
            ma_vals, trade_vals, width=0.008, alpha=0.2, color="gray", label="Trades"
        )
        ax3.set_ylabel("Trade count", color="gray")
        ax3.tick_params(axis="y", labelcolor="gray")

    plt.tight_layout()
    path = OUTPUT_DIR / "maxask_sweep_combined.png"
    plt.savefig(path, dpi=150)
    print(f"\n  Combined plot saved: {path}")

    # Individual CSV plot (larger)
    fig2, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6))
    fig2.suptitle("CSV max_ask Sweep - 5 ETH Patterns", fontsize=14, fontweight="bold")

    ma_vals = [r["max_ask"] for r in csv_results]
    pnl_vals = [r["pnl"] for r in csv_results]
    wr_vals = [r["wr"] for r in csv_results]
    avg_entries = [r["avg_entry"] for r in csv_results if r["trades"] > 0]
    ma_with_trades = [r["max_ask"] for r in csv_results if r["trades"] > 0]

    colors = ["green" if p >= 0 else "red" for p in pnl_vals]
    ax1.bar(ma_vals, pnl_vals, width=0.008, color=colors, alpha=0.8)
    ax1.axhline(y=0, color="black", linewidth=0.5)
    best_idx = np.argmax(pnl_vals)
    ax1.axvline(x=ma_vals[best_idx], color="blue", linestyle="--", alpha=0.5)
    ax1.set_title(
        f"PnL by max_ask (best: ${ma_vals[best_idx]:.2f} -> ${pnl_vals[best_idx]:+.1f})"
    )
    ax1.set_xlabel("max_ask")
    ax1.set_ylabel("PnL ($)")
    ax1.grid(True, alpha=0.3)

    ax2.plot(
        ma_with_trades,
        avg_entries,
        color="purple",
        marker="o",
        markersize=3,
        label="Avg Entry Price",
    )
    ax2.plot(
        ma_vals,
        ma_vals,
        color="red",
        linestyle="--",
        alpha=0.5,
        label="max_ask = entry (worst case)",
    )
    ax2.set_title("Average Entry Price vs max_ask")
    ax2.set_xlabel("max_ask")
    ax2.set_ylabel("Avg Entry Price ($)")
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    path2 = OUTPUT_DIR / "maxask_sweep_csv_detail.png"
    plt.savefig(path2, dpi=150)
    print(f"  CSV detail plot saved: {path2}")


def main():
    # --- CSV ---
    print("  Loading CSV data...")
    raw = load_csvs()
    print("  Building cycles...")
    csv_seqs = csv_build_cycles(raw)
    print("  Running CSV sweep (0.40 -> 0.80)...")
    csv_results = sweep("csv", csv_seqs)
    print_table(csv_results, "CSV (5 days, real prices) - 5 ETH patterns combined")

    # --- Telonex ---
    print("\n  Loading Telonex data...")
    tdf = load_telonex()
    tel_seqs = build_sequences(tdf)
    print("  Running Telonex sweep (0.40 -> 0.80)...")
    tel_results = sweep("telonex", tel_seqs)
    print_table(
        tel_results, "Telonex (74 days, entry=max_ask) - 5 ETH patterns combined"
    )

    # --- Best ---
    csv_best = max(csv_results, key=lambda r: r["pnl"])
    tel_best = max(tel_results, key=lambda r: r["pnl"])
    print(f"\n{'=' * 80}")
    print(f"  OPTIMAL max_ask:")
    print(
        f"    CSV:     ${csv_best['max_ask']:.2f}  ->  {csv_best['trades']} trades, {csv_best['wr']:.1f}% WR, ${csv_best['pnl']:+.2f}"
    )
    print(
        f"    Telonex: ${tel_best['max_ask']:.2f}  ->  {tel_best['trades']} trades, {tel_best['wr']:.1f}% WR, ${tel_best['pnl']:+.2f}"
    )

    # Find max_ask range where BOTH are profitable
    both_profitable = [
        ma
        for ma in MAX_ASK_VALUES
        if any(r["max_ask"] == ma and r["pnl"] > 0 for r in csv_results)
        and any(r["max_ask"] == ma and r["pnl"] > 0 for r in tel_results)
    ]
    if both_profitable:
        # Best combined = max sum of normalized PnLs
        best_combined = None
        best_combined_score = -999
        for ma in both_profitable:
            csv_pnl = next(r["pnl"] for r in csv_results if r["max_ask"] == ma)
            tel_pnl = next(r["pnl"] for r in tel_results if r["max_ask"] == ma)
            # Normalize by max PnL in each dataset
            score = csv_pnl / csv_best["pnl"] + tel_pnl / tel_best["pnl"]
            if score > best_combined_score:
                best_combined_score = score
                best_combined = ma
        csv_r = next(r for r in csv_results if r["max_ask"] == best_combined)
        tel_r = next(r for r in tel_results if r["max_ask"] == best_combined)
        print(f"\n    BEST COMPROMISE (profitable on both):")
        print(f"    max_ask=${best_combined:.2f}")
        print(
            f"      CSV:     {csv_r['trades']} trades, {csv_r['wr']:.1f}% WR, ${csv_r['pnl']:+.2f}"
        )
        print(
            f"      Telonex: {tel_r['trades']} trades, {tel_r['wr']:.1f}% WR, ${tel_r['pnl']:+.2f}"
        )
    else:
        print(
            f"\n    WARNING: No max_ask is profitable on BOTH datasets simultaneously"
        )

    print(f"{'=' * 80}")

    # --- Plots ---
    plot_sweep(csv_results, tel_results)
    print("\n  Done!")


if __name__ == "__main__":
    main()
