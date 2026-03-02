"""Autocorrelation Analysis — Do previous cycle outcomes predict the next?

Two-layer analysis:
  Layer 1: Does P(UP_t | sequence of previous outcomes) differ from P(UP) unconditional?
  Layer 2: Even if it does, is the edge already priced into the opening ask?

If P(UP | last 3 DOWN) = 60% but ask opens at $0.60 -> no edge (priced in).
If P(UP | last 3 DOWN) = 60% but ask opens at $0.45 -> potential edge of 15pp.

Statistical tests:
  - Binomial test for each conditional vs unconditional base rate
  - Bootstrap 95% CI on conditional probabilities
  - Fisher exact test for independence (2×2 contingency)

Outcome resolution:
  Default: Gamma API (ground truth — actual market resolutions)
  --legacy: Old CSV-based inference (0.70/0.30 thresholds, t>=280) — KNOWN INACCURATE

Usage:
    python research/autocorrelation.py data/prices_*.csv
    python research/autocorrelation.py data/prices_2026-03-01.csv --legacy
"""

import argparse
import os
import sys
from pathlib import Path
from itertools import product
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
from scipy import stats as sp_stats

# Path setup
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research_v2 import load_data, get_cycles, determine_outcomes, COINS
from backtest_patterns import resolve_all_outcomes

N_BOOT = 10000


# ── Data preparation ────────────────────────────────────────────

def build_outcome_series(cycles, outcomes):
    """Build per-coin and aggregate outcome time series.

    Returns:
        per_coin: dict[coin] -> list of (outcome_int, open_ask, cycle_start)
            outcome_int: 1 = UP, 0 = DOWN, None = unresolved
            open_ask: UP ask at t=5s (first reliable price)
        majority: list of (outcome_int, avg_open_ask)
            majority = 1 if 3+ coins went UP, else 0
    """
    per_coin = {c: [] for c in COINS}
    majority = []

    for cycle, oc in zip(cycles, outcomes):
        t_arr = cycle["seconds_elapsed"].values
        # Get price at t~5s (first reliable quote after cycle open)
        idx_5 = np.searchsorted(t_arr, 5)
        if idx_5 >= len(cycle):
            idx_5 = 0
        row = cycle.iloc[idx_5]
        cs = cycle["cycle_start"].iloc[0]

        coin_ints = []
        open_asks = []
        for c in COINS:
            o = oc.get(c)
            if o == "UP":
                val = 1
            elif o == "DOWN":
                val = 0
            else:
                val = None
            ask = row[f"{c.lower()}_up_ask"]
            per_coin[c].append((val, ask, cs))
            coin_ints.append(val)
            open_asks.append(ask)

        # Majority outcome (3+ coins same direction)
        valid = [v for v in coin_ints if v is not None]
        if len(valid) >= 3:
            up_count = sum(valid)
            maj = 1 if up_count >= 3 else 0  # could be 2-2, treat as DOWN
            majority.append((maj, np.mean(open_asks)))
        else:
            majority.append((None, np.mean(open_asks)))

    return per_coin, majority


# ── Statistical helpers ─────────────────────────────────────────

def bootstrap_ci(successes, total, n_boot=N_BOOT):
    """Bootstrap 95% CI on a proportion."""
    if total < 3:
        return (0.0, 1.0)
    arr = np.zeros(total)
    arr[:successes] = 1
    rng = np.random.default_rng(42)
    boot = rng.choice(arr, size=(n_boot, total), replace=True)
    props = boot.mean(axis=1)
    return (float(np.percentile(props, 2.5)), float(np.percentile(props, 97.5)))


def binomial_pvalue(k, n, p0):
    """Two-sided binomial test: H0: P = p0."""
    if n < 1:
        return 1.0
    result = sp_stats.binomtest(k, n, p0, alternative='two-sided')
    return float(result.pvalue)


# ── Layer 1: Conditional probabilities ──────────────────────────

def analyze_conditional(series, max_lag=4):
    """Compute P(UP_t | sequence of previous outcomes).

    Args:
        series: list of (outcome_int, open_ask, ...) where outcome_int in {0, 1, None}
        max_lag: maximum sequence length to look back

    Returns:
        results: list of dicts with condition, counts, probabilities, tests
    """
    # Extract outcome sequence (drop Nones for sequential analysis)
    filtered = [(v, ask) for v, ask, *_ in series if v is not None]
    outcomes = [v for v, _ in filtered]
    asks = [a for _, a in filtered]
    n = len(outcomes)

    if n < 10:
        return []

    # Unconditional base rate
    base_up = sum(outcomes) / n
    results = [{"condition": "Unconditional", "pattern": "-",
                "n": n, "up": sum(outcomes), "p_up": base_up,
                "ci_lo": bootstrap_ci(sum(outcomes), n)[0],
                "ci_hi": bootstrap_ci(sum(outcomes), n)[1],
                "p_val": 1.0, "avg_ask": np.mean(asks),
                "edge": 0.0}]

    # For each lag depth (1, 2, 3, ...)
    for lag in range(1, max_lag + 1):
        # Generate all possible patterns of length `lag`
        patterns = list(product([0, 1], repeat=lag))
        for pattern in patterns:
            pat_str = "".join("U" if p == 1 else "D" for p in pattern)
            # Find indices where the previous `lag` outcomes match pattern
            up_count = 0
            total = 0
            ask_sum = 0.0
            for i in range(lag, n):
                prev = tuple(outcomes[i - lag:i])
                if prev == pattern:
                    total += 1
                    up_count += outcomes[i]
                    ask_sum += asks[i]

            if total < 3:
                continue

            p_up = up_count / total
            avg_ask = ask_sum / total
            ci_lo, ci_hi = bootstrap_ci(up_count, total)
            p_val = binomial_pvalue(up_count, total, base_up)
            # Edge = actual P(UP) - implied P(UP) from ask
            # If you buy UP at ask, you need P(UP) > ask to profit
            edge = p_up - avg_ask

            results.append({
                "condition": f"prev {lag}={pat_str}",
                "pattern": pat_str,
                "n": total,
                "up": up_count,
                "p_up": p_up,
                "ci_lo": ci_lo,
                "ci_hi": ci_hi,
                "p_val": p_val,
                "avg_ask": avg_ask,
                "edge": edge,
            })

    return results


# ── Streak analysis ─────────────────────────────────────────────

def analyze_streaks(series, max_streak=6):
    """After N consecutive UPs or DOWNs, what happens next?"""
    filtered = [(v, ask) for v, ask, *_ in series if v is not None]
    outcomes = [v for v, _ in filtered]
    asks = [a for _, a in filtered]
    n = len(outcomes)

    if n < 10:
        return []

    base_up = sum(outcomes) / n
    results = []

    for direction in [1, 0]:  # 1=UP streak, 0=DOWN streak
        d_str = "UP" if direction == 1 else "DOWN"
        for streak_len in range(1, max_streak + 1):
            up_after = 0
            total = 0
            ask_sum = 0.0
            for i in range(streak_len, n):
                # Check if previous streak_len outcomes are all `direction`
                if all(outcomes[i - j - 1] == direction for j in range(streak_len)):
                    # And the one before (if exists) is NOT `direction` (pure streak start)
                    # Actually, let's count "at least N consecutive" for simplicity
                    total += 1
                    up_after += outcomes[i]
                    ask_sum += asks[i]

            if total < 3:
                continue

            p_up = up_after / total
            avg_ask = ask_sum / total
            ci_lo, ci_hi = bootstrap_ci(up_after, total)
            p_val = binomial_pvalue(up_after, total, base_up)
            edge = p_up - avg_ask

            results.append({
                "streak": f"{streak_len}+ {d_str}",
                "n": total,
                "up": up_after,
                "p_up": p_up,
                "ci_lo": ci_lo,
                "ci_hi": ci_hi,
                "p_val": p_val,
                "avg_ask": avg_ask,
                "edge": edge,
            })

    return results


# ── Cross-coin lead-lag ─────────────────────────────────────────

def analyze_cross_coin(per_coin):
    """Does coin X's outcome predict coin Y's NEXT outcome?"""
    results = []
    base_rates = {}
    for c in COINS:
        vals = [v for v, _, _ in per_coin[c] if v is not None]
        base_rates[c] = sum(vals) / len(vals) if vals else 0.5

    for leader in COINS:
        for follower in COINS:
            if leader == follower:
                continue
            l_series = per_coin[leader]
            f_series = per_coin[follower]
            n = min(len(l_series), len(f_series))

            # When leader went UP in cycle t, what does follower do in cycle t+1?
            for leader_dir in [1, 0]:
                d_str = "UP" if leader_dir == 1 else "DN"
                up_count = 0
                total = 0
                ask_sum = 0.0
                for i in range(1, n):
                    l_val = l_series[i - 1][0]
                    f_val = f_series[i][0]
                    f_ask = f_series[i][1]
                    if l_val == leader_dir and f_val is not None:
                        total += 1
                        up_count += f_val
                        ask_sum += f_ask

                if total < 5:
                    continue

                p_up = up_count / total
                avg_ask = ask_sum / total
                base = base_rates[follower]
                p_val = binomial_pvalue(up_count, total, base)
                edge = p_up - avg_ask

                results.append({
                    "signal": f"{leader} {d_str} -> {follower}",
                    "n": total,
                    "up": up_count,
                    "p_up": p_up,
                    "base": base,
                    "p_val": p_val,
                    "avg_ask": avg_ask,
                    "edge": edge,
                })

    return results


# ── Runs test for randomness ───────────────────────────────────

def runs_test(outcomes):
    """Wald-Wolfowitz runs test: is the sequence random?
    Fewer runs than expected -> clustering (momentum).
    More runs than expected -> mean reversion.
    """
    vals = [v for v in outcomes if v is not None]
    n = len(vals)
    if n < 20:
        return None

    n1 = sum(vals)       # number of UPs
    n0 = n - n1          # number of DOWNs
    if n0 == 0 or n1 == 0:
        return None

    # Count runs
    runs = 1
    for i in range(1, n):
        if vals[i] != vals[i - 1]:
            runs += 1

    # Expected runs and variance under H0 (random)
    e_runs = 1 + (2 * n0 * n1) / n
    var_runs = (2 * n0 * n1 * (2 * n0 * n1 - n)) / (n * n * (n - 1))
    if var_runs <= 0:
        return None

    z = (runs - e_runs) / np.sqrt(var_runs)
    p_val = 2 * (1 - sp_stats.norm.cdf(abs(z)))

    return {
        "n": n, "n_up": n1, "n_down": n0,
        "runs": runs, "expected_runs": e_runs,
        "z": z, "p_val": p_val,
        "interpretation": "clustering" if z < -1.96 else ("mean-reverting" if z > 1.96 else "random"),
    }


# ── Main ────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Autocorrelation analysis on 5-min crypto market outcomes."
    )
    parser.add_argument(
        "csv_files", nargs="*",
        help="CSV data files. If omitted, uses data/prices_*.csv",
    )
    parser.add_argument(
        "--legacy", action="store_true",
        help="Use old CSV-based inference (0.70/0.30 thresholds). KNOWN INACCURATE.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.csv_files:
        csv_files = args.csv_files
    else:
        csv_files = sorted(str(f) for f in Path("data").glob("prices_*.csv"))
    if not csv_files:
        print("No data files found."); sys.exit(1)

    mode = "LEGACY (CSV 0.70/0.30)" if args.legacy else "GAMMA API (ground truth)"
    print(f"Loading data...  [Inference: {mode}]")
    df = load_data(csv_files)
    cycles = get_cycles(df)

    if args.legacy:
        outcomes = [determine_outcomes(c) for c in cycles]
    else:
        print("  Resolving outcomes via Gamma API (cached after first run)...")
        outcomes = resolve_all_outcomes(cycles)

    n_resolved = sum(1 for o in outcomes if any(v is not None for v in o.values()))
    print(f"  {len(df):,} rows | {len(cycles)} cycles | {n_resolved} resolved\n")

    per_coin, majority = build_outcome_series(cycles, outcomes)

    W = 95

    # ════════════════════════════════════════════════════════════
    # SECTION 1: RUNS TEST (is the sequence random?)
    # ════════════════════════════════════════════════════════════
    print("=" * W)
    print("  SECTION 1: RUNS TEST — Is the outcome sequence random?")
    print("=" * W)
    print("  Wald-Wolfowitz: fewer runs = momentum/clustering, more runs = mean-reversion\n")

    for c in COINS:
        vals = [v for v, _, _ in per_coin[c] if v is not None]
        rt = runs_test(vals)
        if rt:
            print(f"  {c:>4}: n={rt['n']:>3}  UP={rt['n_up']:>3}  DN={rt['n_down']:>3}"
                  f"  runs={rt['runs']:>3}  expected={rt['expected_runs']:.1f}"
                  f"  z={rt['z']:>+.2f}  p={rt['p_val']:.4f}  => {rt['interpretation']}")

    # Majority (market-level)
    maj_vals = [v for v, _ in majority if v is not None]
    rt_maj = runs_test(maj_vals)
    if rt_maj:
        print(f"  {'MAJ':>4}: n={rt_maj['n']:>3}  UP={rt_maj['n_up']:>3}  DN={rt_maj['n_down']:>3}"
              f"  runs={rt_maj['runs']:>3}  expected={rt_maj['expected_runs']:.1f}"
              f"  z={rt_maj['z']:>+.2f}  p={rt_maj['p_val']:.4f}  => {rt_maj['interpretation']}")

    # ════════════════════════════════════════════════════════════
    # SECTION 2: CONDITIONAL PROBABILITIES (per coin)
    # ════════════════════════════════════════════════════════════
    print(f"\n{'=' * W}")
    print("  SECTION 2: CONDITIONAL PROBABILITIES — P(UP_t | previous outcomes)")
    print("  Layer 1: does the conditional differ from unconditional?")
    print("  Layer 2: is any difference already priced into the opening ask?")
    print("=" * W)

    for c in COINS:
        print(f"\n  -- {c} --")
        results = analyze_conditional(per_coin[c], max_lag=3)
        if not results:
            print("    Insufficient data")
            continue
        print(f"  {'Condition':<18} {'N':>4} {'UP':>3} {'P(UP)':>7} {'95% CI':>15}"
              f"  {'p-val':>6}  {'Avg Ask':>7} {'Edge':>7}  Note")
        print(f"  {'-' * 90}")
        for r in results:
            sig = ""
            if r["p_val"] < 0.05 and r["condition"] != "Unconditional":
                sig = " *"
            if r["p_val"] < 0.01 and r["condition"] != "Unconditional":
                sig = " **"
            edge_note = ""
            if r["condition"] != "Unconditional":
                if r["edge"] > 0.05:
                    edge_note = " << POTENTIAL EDGE"
                elif r["edge"] < -0.05:
                    edge_note = " << priced in (overpriced)"
            print(f"  {r['condition']:<18} {r['n']:>4} {r['up']:>3}"
                  f" {r['p_up']:>6.1%} [{r['ci_lo']:>5.1%},{r['ci_hi']:>5.1%}]"
                  f"  {r['p_val']:>6.4f}  {r['avg_ask']:>7.3f} {r['edge']:>+6.1%}{sig}{edge_note}")

    # ════════════════════════════════════════════════════════════
    # SECTION 3: MAJORITY OUTCOME (aggregate market direction)
    # ════════════════════════════════════════════════════════════
    print(f"\n{'=' * W}")
    print("  SECTION 3: MAJORITY OUTCOME — Market-level autocorrelation")
    print("  Majority = 3+ of 4 coins resolve same direction")
    print("=" * W)

    maj_series = [(v, ask, None) for v, ask in majority]
    results = analyze_conditional(maj_series, max_lag=3)
    if results:
        print(f"\n  {'Condition':<18} {'N':>4} {'UP':>3} {'P(UP)':>7} {'95% CI':>15}"
              f"  {'p-val':>6}  {'Avg Ask':>7} {'Edge':>7}  Note")
        print(f"  {'-' * 90}")
        for r in results:
            sig = ""
            if r["p_val"] < 0.05 and r["condition"] != "Unconditional":
                sig = " *"
            if r["p_val"] < 0.01 and r["condition"] != "Unconditional":
                sig = " **"
            edge_note = ""
            if r["condition"] != "Unconditional":
                if r["edge"] > 0.05:
                    edge_note = " << POTENTIAL EDGE"
                elif r["edge"] < -0.05:
                    edge_note = " << priced in (overpriced)"
            print(f"  {r['condition']:<18} {r['n']:>4} {r['up']:>3}"
                  f" {r['p_up']:>6.1%} [{r['ci_lo']:>5.1%},{r['ci_hi']:>5.1%}]"
                  f"  {r['p_val']:>6.4f}  {r['avg_ask']:>7.3f} {r['edge']:>+6.1%}{sig}{edge_note}")

    # ════════════════════════════════════════════════════════════
    # SECTION 4: STREAK ANALYSIS
    # ════════════════════════════════════════════════════════════
    print(f"\n{'=' * W}")
    print("  SECTION 4: STREAK ANALYSIS — After N consecutive same-direction outcomes")
    print("=" * W)

    for c in COINS:
        streaks = analyze_streaks(per_coin[c], max_streak=5)
        if not streaks:
            continue
        print(f"\n  -- {c} --")
        print(f"  {'Streak':<12} {'N':>4} {'UP':>3} {'P(UP)':>7} {'95% CI':>15}"
              f"  {'p-val':>6}  {'Avg Ask':>7} {'Edge':>7}")
        print(f"  {'-' * 75}")
        for r in streaks:
            sig = " *" if r["p_val"] < 0.05 else ""
            print(f"  {r['streak']:<12} {r['n']:>4} {r['up']:>3}"
                  f" {r['p_up']:>6.1%} [{r['ci_lo']:>5.1%},{r['ci_hi']:>5.1%}]"
                  f"  {r['p_val']:>6.4f}  {r['avg_ask']:>7.3f} {r['edge']:>+6.1%}{sig}")

    # Majority streaks
    print(f"\n  -- MAJORITY --")
    streaks = analyze_streaks(maj_series, max_streak=5)
    if streaks:
        print(f"  {'Streak':<12} {'N':>4} {'UP':>3} {'P(UP)':>7} {'95% CI':>15}"
              f"  {'p-val':>6}  {'Avg Ask':>7} {'Edge':>7}")
        print(f"  {'-' * 75}")
        for r in streaks:
            sig = " *" if r["p_val"] < 0.05 else ""
            print(f"  {r['streak']:<12} {r['n']:>4} {r['up']:>3}"
                  f" {r['p_up']:>6.1%} [{r['ci_lo']:>5.1%},{r['ci_hi']:>5.1%}]"
                  f"  {r['p_val']:>6.4f}  {r['avg_ask']:>7.3f} {r['edge']:>+6.1%}{sig}")

    # ════════════════════════════════════════════════════════════
    # SECTION 5: CROSS-COIN LEAD-LAG
    # ════════════════════════════════════════════════════════════
    print(f"\n{'=' * W}")
    print("  SECTION 5: CROSS-COIN — Does coin X's outcome predict coin Y next cycle?")
    print("=" * W)

    cross = analyze_cross_coin(per_coin)
    if cross:
        # Sort by abs(edge) descending
        cross.sort(key=lambda x: abs(x["edge"]), reverse=True)
        print(f"\n  {'Signal':<20} {'N':>4} {'UP':>3} {'P(UP)':>7} {'Base':>6}"
              f"  {'p-val':>6}  {'Ask':>5} {'Edge':>7}")
        print(f"  {'-' * 75}")
        for r in cross[:20]:  # Top 20 by abs(edge)
            sig = " *" if r["p_val"] < 0.05 else ""
            print(f"  {r['signal']:<20} {r['n']:>4} {r['up']:>3}"
                  f" {r['p_up']:>6.1%} {r['base']:>5.1%}"
                  f"  {r['p_val']:>6.4f}  {r['avg_ask']:>5.3f} {r['edge']:>+6.1%}{sig}")

    # ════════════════════════════════════════════════════════════
    # SECTION 6: SUMMARY & INTERPRETATION
    # ════════════════════════════════════════════════════════════
    print(f"\n{'=' * W}")
    print("  SECTION 6: SUMMARY")
    print("=" * W)

    # Collect all results with p < 0.05 (before Bonferroni)
    all_tests = []
    for c in COINS:
        for r in analyze_conditional(per_coin[c], max_lag=3):
            if r["condition"] != "Unconditional":
                r["source"] = c
                all_tests.append(r)
    for r in analyze_conditional(maj_series, max_lag=3):
        if r["condition"] != "Unconditional":
            r["source"] = "MAJ"
            all_tests.append(r)

    n_tests = len(all_tests)
    bonferroni = 0.05 / n_tests if n_tests > 0 else 0.05
    sig_nominal = [r for r in all_tests if r["p_val"] < 0.05]
    sig_bonf = [r for r in all_tests if r["p_val"] < bonferroni]

    print(f"\n  Total conditional tests run: {n_tests}")
    print(f"  Bonferroni-corrected alpha: {bonferroni:.5f}")
    print(f"  Significant at nominal p<0.05: {len(sig_nominal)}")
    print(f"  Significant after Bonferroni: {len(sig_bonf)}")

    if sig_nominal:
        print(f"\n  Nominally significant results (p < 0.05):")
        for r in sorted(sig_nominal, key=lambda x: x["p_val"]):
            print(f"    {r['source']:>4} {r['condition']:<18}"
                  f" P(UP)={r['p_up']:.1%} (n={r['n']})"
                  f"  ask={r['avg_ask']:.3f}  edge={r['edge']:+.1%}"
                  f"  p={r['p_val']:.4f}")
            if r["p_val"] < bonferroni:
                print(f"         ^^^ SURVIVES Bonferroni correction ^^^")

    if not sig_bonf:
        print(f"\n  CONCLUSION: No statistically significant autocorrelation found")
        print(f"  after correcting for {n_tests} multiple comparisons.")
        print(f"  The outcome sequence is consistent with independence (random walk).")
    else:
        print(f"\n  CONCLUSION: {len(sig_bonf)} pattern(s) survived Bonferroni correction.")
        print(f"  Check if the edge column shows opportunity after accounting for ask spread.")

    # Final: expected vs observed
    print(f"\n  Edge interpretation:")
    print(f"    Edge > 0: actual P(UP) > market-implied P(UP) => BUY UP has +EV")
    print(f"    Edge < 0: actual P(UP) < market-implied P(UP) => BUY DOWN has +EV")
    print(f"    Edge ~ 0: market is correctly priced => no opportunity")

    edges_with_edge = [r for r in all_tests if abs(r["edge"]) > 0.10 and r["n"] >= 10]
    if edges_with_edge:
        print(f"\n  Patterns with |edge| > 10pp AND n >= 10:")
        for r in sorted(edges_with_edge, key=lambda x: -abs(x["edge"])):
            direction = "BUY UP" if r["edge"] > 0 else "BUY DOWN"
            print(f"    {r['source']:>4} {r['condition']:<18}"
                  f" P(UP)={r['p_up']:.1%}  ask={r['avg_ask']:.3f}"
                  f"  edge={r['edge']:+.1%}  n={r['n']}  p={r['p_val']:.4f}"
                  f"  => {direction}")
    else:
        print(f"\n  No patterns found with |edge| > 10pp and sufficient sample size.")

    print(f"\n{'=' * W}")


if __name__ == "__main__":
    main()
