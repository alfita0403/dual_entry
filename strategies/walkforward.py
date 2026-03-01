"""Walk-Forward Out-of-Sample Test.

Train on day 1 (Feb 28), test on day 2 (Mar 1).
Day 2 NEVER participates in parameter selection.

If strategies profitable on day 1 are ALSO profitable on day 2,
the edge is likely real (not overfit).
"""

import sys, time
from pathlib import Path
import numpy as np
import pandas as pd

# Reuse everything from research_v2
from research_v2 import (
    load_data, get_cycles, determine_outcomes,
    find_signals_statarb_up, find_signals_statarb_dn,
    find_signals_cheap, simulate, bankroll, COINS,
)

BET = 10.0
FEE = 0.01
START = 100.0
ENTRY_DELAY = 5  # seconds: on-chain settlement before GTC is placed


def run_grid(cycles, outcomes, label=""):
    """Run full parameter grid, return dict of {key: {trades, stats}}."""
    results = {}

    # UP grid
    for sp in [0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.12, 0.14, 0.16, 0.18, 0.20]:
        signals = find_signals_statarb_up(cycles, outcomes, sp, 300)
        for w in [30, 45, 60, 90, 120, 150, 180]:
            w_sigs = [s for s in signals if s["t"] <= w]
            for tp in [0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12, 0.15]:
                for to in [15, 20, 30, 45, 60, 90, 300]:
                    trades = simulate(w_sigs, tp, to, FEE, ENTRY_DELAY)
                    if not trades:
                        continue
                    pnls = [t["pnl"] for t in trades]
                    sim = bankroll(trades, START, BET)
                    key = f"UP s={sp:.2f} w={w} tp={tp:.2f} to={to}s"
                    results[key] = {
                        "trades": trades,
                        "n": len(trades),
                        "avg_pnl": np.mean(pnls),
                        "wr": sum(1 for p in pnls if p > 0) / len(pnls),
                        "final": sim["final"],
                        "profit": sim["profit"],
                        "max_dd": sim["max_dd"],
                        "pf": sim["pf"],
                    }
    return results


def main():
    train_file = "data/prices_2026-02-28.csv"
    test_file = "data/prices_2026-03-01.csv"

    if not Path(train_file).exists() or not Path(test_file).exists():
        print("Need both data files"); sys.exit(1)

    # ── TRAIN: day 1 ──────────────────────────────────────────
    print("=" * 100)
    print("  WALK-FORWARD OUT-OF-SAMPLE TEST")
    print("  Train: Feb 28  |  Test: Mar 1 (NEVER seen during optimization)")
    print("=" * 100)

    print("\nLoading TRAIN data (Feb 28)...")
    df_train = load_data([train_file])
    c_train = get_cycles(df_train)
    o_train = [determine_outcomes(c) for c in c_train]
    print(f"  {len(df_train):,} rows | {len(c_train)} cycles")

    print("Running grid on TRAIN...")
    t0 = time.time()
    train_results = run_grid(c_train, o_train, "TRAIN")
    print(f"  {len(train_results)} strategies evaluated in {time.time()-t0:.1f}s")

    # Filter: profitable on train
    profitable_train = {k: v for k, v in train_results.items() if v["profit"] > 0}
    print(f"  {len(profitable_train)} profitable on TRAIN")

    if not profitable_train:
        print("Nothing profitable on train data."); return

    # Sort by edge
    sorted_train = sorted(profitable_train.items(), key=lambda x: -x[1]["avg_pnl"])

    # ── TEST: day 2 ───────────────────────────────────────────
    print(f"\nLoading TEST data (Mar 1)...")
    df_test = load_data([test_file])
    c_test = get_cycles(df_test)
    o_test = [determine_outcomes(c) for c in c_test]
    print(f"  {len(df_test):,} rows | {len(c_test)} cycles")

    print("Running SAME strategies on TEST (out-of-sample)...")
    t0 = time.time()
    test_results = run_grid(c_test, o_test, "TEST")
    print(f"  Done in {time.time()-t0:.1f}s")

    # ── COMPARISON ────────────────────────────────────────────
    print(f"\n{'=' * 100}")
    print(f"  RESULTS: {len(profitable_train)} strategies profitable on TRAIN")
    print(f"{'=' * 100}")

    # Count how many train-profitable are also test-profitable
    both_profitable = 0
    test_profitable_only = 0
    train_only = 0

    rows = []
    for key, tr in sorted_train:
        te = test_results.get(key)
        if te:
            rows.append({
                "key": key,
                "train_edge": tr["avg_pnl"],
                "train_n": tr["n"],
                "train_wr": tr["wr"],
                "train_pf": tr["pf"],
                "train_final": tr["final"],
                "train_dd": tr["max_dd"],
                "test_edge": te["avg_pnl"],
                "test_n": te["n"],
                "test_wr": te["wr"],
                "test_pf": te["pf"],
                "test_final": te["final"],
                "test_dd": te["max_dd"],
            })
            if te["profit"] > 0:
                both_profitable += 1
            else:
                train_only += 1
        else:
            rows.append({
                "key": key,
                "train_edge": tr["avg_pnl"],
                "train_n": tr["n"],
                "train_wr": tr["wr"],
                "train_pf": tr["pf"],
                "train_final": tr["final"],
                "train_dd": tr["max_dd"],
                "test_edge": 0, "test_n": 0, "test_wr": 0,
                "test_pf": 0, "test_final": START, "test_dd": 0,
            })
            train_only += 1

    survival_rate = both_profitable / len(profitable_train) * 100

    print(f"\n  Profitable on TRAIN:              {len(profitable_train)}")
    print(f"  Also profitable on TEST (OOS):    {both_profitable}")
    print(f"  SURVIVAL RATE:                    {survival_rate:.1f}%")
    print(f"\n  If survival >> 50%: REAL EDGE")
    print(f"  If survival ~50%: COIN FLIP (no edge)")
    print(f"  If survival << 50%: OVERFIT")

    # Top 20 by train edge with OOS results
    print(f"\n  {'-' * 100}")
    print(f"  Top 20 by TRAIN edge -> OUT-OF-SAMPLE results")
    print(f"  {'-' * 100}")
    hdr = (f"  {'#':>2} {'TRAIN':>43} {'|':>1} {'TEST (OOS)':>43}  Params")
    print(hdr)
    print(f"     {'Edge':>6} {'$Final':>7} {'WR':>5} {'PF':>5} {'DD':>5} {'N':>4}"
          f"  | {'Edge':>6} {'$Final':>7} {'WR':>5} {'PF':>5} {'DD':>5} {'N':>4}")

    for i, r in enumerate(rows[:20], 1):
        oos_mark = "OK" if r["test_final"] > START else "XX"
        print(f"  {i:>2}"
              f" {r['train_edge']:>+5.3f} ${r['train_final']:>6.1f}"
              f" {100*r['train_wr']:>4.0f}% {r['train_pf']:>5.2f}"
              f" {r['train_dd']:>4.1f}% {r['train_n']:>4}"
              f"  | {r['test_edge']:>+5.3f} ${r['test_final']:>6.1f}"
              f" {100*r['test_wr']:>4.0f}% {r['test_pf']:>5.2f}"
              f" {r['test_dd']:>4.1f}% {r['test_n']:>4}"
              f"  {oos_mark} {r['key']}")

    # Show strategies profitable on BOTH, sorted by test edge
    both = [r for r in rows if r["test_final"] > START]
    if both:
        both.sort(key=lambda x: -x["test_edge"])
        print(f"\n  {'-' * 100}")
        print(f"  SURVIVED OOS ({len(both)}): sorted by TEST edge")
        print(f"  {'-' * 100}")
        print(f"     {'Edge':>6} {'$Final':>7} {'WR':>5} {'PF':>5} {'DD':>5} {'N':>4}"
              f"  | {'Edge':>6} {'$Final':>7} {'WR':>5} {'PF':>5} {'DD':>5} {'N':>4}")
        for i, r in enumerate(both[:20], 1):
            print(f"  {i:>2}"
                  f" {r['train_edge']:>+5.3f} ${r['train_final']:>6.1f}"
                  f" {100*r['train_wr']:>4.0f}% {r['train_pf']:>5.2f}"
                  f" {r['train_dd']:>4.1f}% {r['train_n']:>4}"
                  f"  | {r['test_edge']:>+5.3f} ${r['test_final']:>6.1f}"
                  f" {100*r['test_wr']:>4.0f}% {r['test_pf']:>5.02f}"
                  f" {r['test_dd']:>4.1f}% {r['test_n']:>4}"
                  f"    {r['key']}")

    print(f"\n{'=' * 100}")


if __name__ == "__main__":
    main()
