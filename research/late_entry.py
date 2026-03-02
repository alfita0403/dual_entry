"""
Late Entry Calibration Study - Is the Polymarket market maker well-calibrated
in the final seconds of 5-minute cycles?

Core question: At time T within the cycle, if the UP ask = X, does UP actually
win X% of the time? If UP wins MORE than X%, the market is under-pricing UP
and there's an edge buying UP late. If LESS, there's an edge buying DOWN late.

Tests:
  1. Calibration curve: at t=240,250,260,270,280s, bin by ask price, compare
     implied probability vs actual win rate
  2. Late entry simulation: if at time T the ask strongly favors one side
     (>0.70 or <0.30), buy that side. PnL after slippage/fees.
  3. Edge by entry time: how does the edge change as you enter later?
  4. Per-coin calibration breakdown

Usage:
    python research/late_entry.py data/prices_2026-02-28.csv data/prices_2026-03-01.csv
    python research/late_entry.py data/prices_*.csv --coins BTC,ETH
"""

import argparse
import sys
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research_v2 import load_data, get_cycles, COINS, UP_ASK
from backtest_patterns import resolve_all_outcomes

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
FEE_RATE = 0.015
SLIPPAGE = 0.03

# Times to sample (seconds into cycle)
SAMPLE_TIMES = [180, 210, 240, 250, 260, 270, 280, 290]

# Probability bins for calibration
PROB_BINS = [
    (0.00, 0.10, "0-10%  (strong DOWN)"),
    (0.10, 0.20, "10-20% (DOWN likely)"),
    (0.20, 0.30, "20-30% (DOWN lean)"),
    (0.30, 0.40, "30-40% (slight DOWN)"),
    (0.40, 0.50, "40-50% (toss-up DOWN)"),
    (0.50, 0.60, "50-60% (toss-up UP)"),
    (0.60, 0.70, "60-70% (slight UP)"),
    (0.70, 0.80, "70-80% (UP lean)"),
    (0.80, 0.90, "80-90% (UP likely)"),
    (0.90, 1.00, "90-100%(strong UP)"),
]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
class LateObs:
    """One observation: a coin at a specific time in a cycle."""
    def __init__(self):
        self.cycle_idx: int = 0
        self.coin: str = ""
        self.time_s: int = 0
        self.up_ask: float = 0.0       # market-implied P(UP)
        self.actual_up: Optional[bool] = None  # did UP actually win?


# ---------------------------------------------------------------------------
# Build observations
# ---------------------------------------------------------------------------
def build_observations(
    csv_paths: List[str],
    coins_filter: Optional[List[str]] = None,
) -> List[LateObs]:
    """For each cycle, coin, and sample time, record the UP ask and outcome."""
    df = load_data(csv_paths)
    cycles = get_cycles(df, min_rows=20)

    # Resolve outcomes via Gamma API
    all_outcomes = resolve_all_outcomes(cycles)

    active_coins = [c.upper() for c in coins_filter] if coins_filter else list(COINS)

    observations: List[LateObs] = []

    for cycle_idx, (cycle, outcomes) in enumerate(zip(cycles, all_outcomes)):
        for coin in active_coins:
            outcome = outcomes.get(coin)
            if outcome is None:
                continue
            actual_up = (outcome == "UP")

            cl = coin.lower()
            up_ask_col = f"{cl}_up_ask"

            for t in SAMPLE_TIMES:
                # Get the UP ask at approximately time t
                # Use a small window around t to handle missing seconds
                window = cycle[
                    (cycle["seconds_elapsed"] >= t - 2) &
                    (cycle["seconds_elapsed"] <= t + 2)
                ]
                if len(window) == 0:
                    continue

                # Take the value closest to t
                closest_idx = (window["seconds_elapsed"] - t).abs().idxmin()
                ask_val = window.loc[closest_idx, up_ask_col]

                if pd.isna(ask_val) or ask_val <= 0 or ask_val >= 1:
                    continue

                obs = LateObs()
                obs.cycle_idx = cycle_idx
                obs.coin = coin
                obs.time_s = t
                obs.up_ask = ask_val
                obs.actual_up = actual_up
                observations.append(obs)

    return observations


# ---------------------------------------------------------------------------
# Analysis 1: Calibration curve
# ---------------------------------------------------------------------------
def calibration_analysis(obs: List[LateObs], time_filter: Optional[int] = None) -> None:
    """Compare implied probability (ask) vs actual win rate."""
    filtered = obs if time_filter is None else [o for o in obs if o.time_s == time_filter]

    if not filtered:
        print(f"  No data for t={time_filter}")
        return

    time_label = f"t={time_filter}s" if time_filter else "all times"
    print(f"\n  --- Calibration at {time_label} ({len(filtered)} observations) ---")
    print(f"  {'Implied P(UP)':<22} {'N':>5} {'Actual UP%':>10} {'Implied%':>9} "
          f"{'Miscal':>8} {'Edge side':>10} {'p-val':>7}")
    print(f"  {'-'*22} {'-'*5} {'-'*10} {'-'*9} {'-'*8} {'-'*10} {'-'*7}")

    from scipy import stats

    for lo, hi, label in PROB_BINS:
        in_bin = [o for o in filtered if lo <= o.up_ask < hi]
        if len(in_bin) < 5:
            continue

        n = len(in_bin)
        up_wins = sum(1 for o in in_bin if o.actual_up)
        actual_rate = up_wins / n
        implied_rate = np.mean([o.up_ask for o in in_bin])
        miscal = actual_rate - implied_rate  # positive = UP wins more than market says

        # Binomial test: is actual rate different from implied?
        p_val = stats.binomtest(up_wins, n, implied_rate).pvalue

        if miscal > 0.02:
            edge = "BUY UP"
        elif miscal < -0.02:
            edge = "BUY DOWN"
        else:
            edge = "none"

        sig = ""
        if p_val < 0.01:
            sig = " **"
        elif p_val < 0.05:
            sig = " *"

        print(
            f"  {label:<22} {n:>5} {actual_rate:>9.1%} {implied_rate:>8.1%} "
            f"{miscal:>+7.1%} {edge:>10} {p_val:>6.3f}{sig}"
        )


# ---------------------------------------------------------------------------
# Analysis 2: Late entry simulation
# ---------------------------------------------------------------------------
def simulate_late_entry(
    obs: List[LateObs],
    entry_time: int,
    threshold: float = 0.70,
    size: float = 5.0,
) -> None:
    """Simulate: at entry_time, if ask > threshold buy UP, if ask < (1-threshold) buy DOWN."""
    filtered = [o for o in obs if o.time_s == entry_time]
    if not filtered:
        print(f"  No data at t={entry_time}")
        return

    print(f"\n  --- Late Entry Simulation at t={entry_time}s "
          f"(threshold={threshold:.0%}) ---")

    # Buy UP when ask > threshold
    up_candidates = [o for o in filtered if o.up_ask >= threshold]
    # Buy DOWN when ask < (1-threshold), i.e., DOWN implied >= threshold
    down_candidates = [o for o in filtered if o.up_ask <= (1.0 - threshold)]
    # Skip middle (no trade)
    skip = [o for o in filtered if (1.0 - threshold) < o.up_ask < threshold]

    for label, candidates, buy_up in [
        ("BUY UP  (ask >= {:.0%})".format(threshold), up_candidates, True),
        ("BUY DOWN (ask <= {:.0%})".format(1-threshold), down_candidates, False),
    ]:
        if not candidates:
            print(f"  {label}: 0 trades")
            continue

        wins = 0
        total_pnl = 0.0
        for o in candidates:
            if buy_up:
                entry_price = o.up_ask + SLIPPAGE
                won = o.actual_up
            else:
                entry_price = (1.0 - o.up_ask) + SLIPPAGE
                won = not o.actual_up

            cost = entry_price * size
            if won:
                payout = size * (1.0 - FEE_RATE)
                pnl = payout - cost
                wins += 1
            else:
                pnl = -cost
            total_pnl += pnl

        n = len(candidates)
        wr = wins / n
        avg = total_pnl / n

        print(f"  {label}: {n} trades | {wins}W/{n-wins}L | "
              f"WR={wr:.1%} | PnL=${total_pnl:+.2f} | Avg=${avg:+.4f}")

    print(f"  (skipped {len(skip)} cycles where {1-threshold:.0%} < ask < {threshold:.0%})")


# ---------------------------------------------------------------------------
# Analysis 3: Edge by entry time
# ---------------------------------------------------------------------------
def edge_by_time(obs: List[LateObs], size: float = 5.0) -> None:
    """For each sample time, find the best entry and its edge."""
    print(f"\n  --- Edge by Entry Time (best directional bet per time) ---")
    print(f"  {'Time':>6} {'Strategy':<30} {'N':>5} {'WR':>6} {'PnL':>10} "
          f"{'Avg':>10} {'Miscal':>8}")
    print(f"  {'-'*6} {'-'*30} {'-'*5} {'-'*6} {'-'*10} {'-'*10} {'-'*8}")

    for t in SAMPLE_TIMES:
        t_obs = [o for o in obs if o.time_s == t]
        if not t_obs:
            continue

        # Strong UP (ask >= 0.70): buy UP
        strong_up = [o for o in t_obs if o.up_ask >= 0.70]
        # Strong DOWN (ask <= 0.30): buy DOWN
        strong_dn = [o for o in t_obs if o.up_ask <= 0.30]

        best_label = ""
        best_n = 0
        best_wr = 0.0
        best_pnl = 0.0
        best_miscal = 0.0

        for label, cands, buy_up in [
            ("UP when ask>=0.70", strong_up, True),
            ("DOWN when ask<=0.30", strong_dn, False),
        ]:
            if len(cands) < 5:
                continue
            wins = 0
            pnl = 0.0
            for o in cands:
                if buy_up:
                    entry = o.up_ask + SLIPPAGE
                    won = o.actual_up
                else:
                    entry = (1.0 - o.up_ask) + SLIPPAGE
                    won = not o.actual_up
                cost = entry * size
                if won:
                    pnl += size * (1.0 - FEE_RATE) - cost
                    wins += 1
                else:
                    pnl -= cost

            n = len(cands)
            wr = wins / n
            avg = pnl / n

            if buy_up:
                implied = np.mean([o.up_ask for o in cands])
                actual = wins / n
            else:
                implied = np.mean([1.0 - o.up_ask for o in cands])
                actual = wins / n
            miscal = actual - implied

            if abs(pnl) > abs(best_pnl) or best_n == 0:
                best_label = label
                best_n = n
                best_wr = wr
                best_pnl = pnl
                best_miscal = miscal

        if best_n > 0:
            avg = best_pnl / best_n
            print(f"  t={t:>3}s {best_label:<30} {best_n:>5} {best_wr:>5.0%} "
                  f"${best_pnl:>+9.2f} ${avg:>+9.4f} {best_miscal:>+7.1%}")


# ---------------------------------------------------------------------------
# Analysis 4: Aggressive thresholds
# ---------------------------------------------------------------------------
def threshold_sweep(obs: List[LateObs], entry_time: int = 270, size: float = 5.0) -> None:
    """Sweep different confidence thresholds at a fixed entry time."""
    filtered = [o for o in obs if o.time_s == entry_time]
    if not filtered:
        return

    print(f"\n  --- Threshold Sweep at t={entry_time}s ---")
    print(f"  {'Threshold':<12} {'Side':<8} {'N':>5} {'WR':>6} {'PnL':>10} "
          f"{'Avg':>10} {'Implied':>8} {'Actual':>8} {'Miscal':>8}")
    print(f"  {'-'*12} {'-'*8} {'-'*5} {'-'*6} {'-'*10} "
          f"{'-'*10} {'-'*8} {'-'*8} {'-'*8}")

    for thresh in [0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]:
        for buy_up in [True, False]:
            if buy_up:
                cands = [o for o in filtered if o.up_ask >= thresh]
                side = "UP"
            else:
                cands = [o for o in filtered if o.up_ask <= (1.0 - thresh)]
                side = "DOWN"

            if len(cands) < 5:
                continue

            wins = 0
            pnl = 0.0
            for o in cands:
                if buy_up:
                    entry = o.up_ask + SLIPPAGE
                    won = o.actual_up
                else:
                    entry = (1.0 - o.up_ask) + SLIPPAGE
                    won = not o.actual_up
                cost = entry * size
                if won:
                    pnl += size * (1.0 - FEE_RATE) - cost
                    wins += 1
                else:
                    pnl -= cost

            n = len(cands)
            wr = wins / n
            avg = pnl / n
            if buy_up:
                implied = np.mean([o.up_ask for o in cands])
            else:
                implied = np.mean([1.0 - o.up_ask for o in cands])
            actual = wr
            miscal = actual - implied

            print(f"  >={thresh:.0%}{'':>5} {side:<8} {n:>5} {wr:>5.0%} "
                  f"${pnl:>+9.2f} ${avg:>+9.4f} {implied:>7.1%} "
                  f"{actual:>7.1%} {miscal:>+7.1%}")


# ---------------------------------------------------------------------------
# Analysis 5: Per-coin breakdown
# ---------------------------------------------------------------------------
def per_coin_calibration(obs: List[LateObs], entry_time: int = 270) -> None:
    """Per-coin calibration at a fixed time."""
    filtered = [o for o in obs if o.time_s == entry_time]
    coins = sorted(set(o.coin for o in filtered))

    print(f"\n  --- Per-Coin Calibration at t={entry_time}s ---")

    for coin in coins:
        coin_obs = [o for o in filtered if o.coin == coin]
        if len(coin_obs) < 10:
            continue

        # Strong signals only
        strong_up = [o for o in coin_obs if o.up_ask >= 0.70]
        strong_dn = [o for o in coin_obs if o.up_ask <= 0.30]

        print(f"\n  {coin}:")
        for label, cands, check_up in [
            ("  ask>=0.70 (buy UP)", strong_up, True),
            ("  ask<=0.30 (buy DN)", strong_dn, False),
        ]:
            if len(cands) < 3:
                print(f"  {label}: <3 trades, skip")
                continue
            n = len(cands)
            if check_up:
                wins = sum(1 for o in cands if o.actual_up)
                implied = np.mean([o.up_ask for o in cands])
            else:
                wins = sum(1 for o in cands if not o.actual_up)
                implied = np.mean([1.0 - o.up_ask for o in cands])
            actual = wins / n
            miscal = actual - implied
            print(f"  {label}: n={n} | actual={actual:.1%} | "
                  f"implied={implied:.1%} | miscal={miscal:+.1%}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Late entry calibration study for Polymarket 5-min markets"
    )
    parser.add_argument("csv_files", nargs="+", help="CSV price data files")
    parser.add_argument("--coins", type=str, default=None,
                        help="Comma-separated coin filter (e.g. BTC,ETH)")
    args = parser.parse_args()

    for f in args.csv_files:
        if not Path(f).exists():
            print(f"ERROR: File not found: {f}")
            sys.exit(1)

    coins_filter = None
    if args.coins:
        coins_filter = [c.strip().upper() for c in args.coins.split(",")]

    coin_label = ",".join(coins_filter) if coins_filter else "ALL"
    print(f"\n  Building observations for {coin_label}...")
    obs = build_observations(args.csv_files, coins_filter)

    if not obs:
        print("  No observations found!")
        return

    print(f"  {len(obs)} observations across {len(set(o.cycle_idx for o in obs))} cycles")

    print("\n" + "=" * 78)
    print("  LATE ENTRY CALIBRATION STUDY")
    print("=" * 78)

    # 1. Calibration at key times
    for t in [240, 260, 270, 280]:
        calibration_analysis(obs, time_filter=t)

    # 2. Late entry simulation at different times
    for t in [240, 260, 270, 280]:
        simulate_late_entry(obs, entry_time=t, threshold=0.70)

    # 3. Edge by entry time
    edge_by_time(obs)

    # 4. Threshold sweep at t=270
    threshold_sweep(obs, entry_time=270)

    # 5. Per-coin breakdown
    per_coin_calibration(obs, entry_time=270)

    print("\n" + "=" * 78)
    print("  INTERPRETATION")
    print("=" * 78)
    print("""
  Miscalibration (Miscal) = Actual win rate - Market implied probability

  Miscal > 0: Market UNDERPRICES this side. Edge exists buying this side late.
  Miscal ~ 0: Market is well-calibrated. No edge.
  Miscal < 0: Market OVERPRICES this side. Edge exists betting AGAINST.

  Key question: Does miscalibration INCREASE as entry time gets later?
  If yes: the MM doesn't update fast enough in final seconds -> exploitable.
  If no:  the MM is well-calibrated throughout -> no late-entry edge.

  For a trade to be profitable after costs:
    - Buy UP at ask X: need actual P(UP) > X + slippage + fees
    - Slippage = 0.03, fees ~1.5% on payout
    - Minimum edge needed: ~4-5 percentage points above implied
    """)
    print("=" * 78)


if __name__ == "__main__":
    main()
