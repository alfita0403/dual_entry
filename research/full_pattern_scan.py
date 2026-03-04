"""
Full Pattern Scan - exhaustive search across all coins, all patterns, both sides.

Uses Telonex 74-day dataset (60,605 markets) to find every statistically
significant pattern/coin combination and compute the max entry price (max_ask)
at which each is profitable.

Output: ranked table of all viable strategies sorted by expected profit.

Usage:
    python research/full_pattern_scan.py
    python research/full_pattern_scan.py --max-len 7
    python research/full_pattern_scan.py --min-trades 200
"""

import argparse
import sys
from itertools import product
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import stats

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATA_FILE = Path(__file__).parent.parent / "data" / "telonex_updown_5m.parquet"
COINS = ["BTC", "ETH", "SOL", "XRP"]
FEE_RATE = 0.015       # 1.5% taker fee on shares
SLIPPAGE = 0.03        # $0.03 slippage on entry


def load_data(from_date: Optional[str] = None, to_date: Optional[str] = None):
    df = pd.read_parquet(DATA_FILE)
    df["coin"] = df["slug"].str.extract(r"^(\w+)-updown-5m-")[0].str.upper()
    df["unix_ts"] = df["slug"].str.extract(r"-(\d+)$")[0].astype(float)
    df["datetime"] = pd.to_datetime(df["unix_ts"], unit="s", utc=True)
    df["outcome"] = df["result_id"].map({"0": "UP", "1": "DOWN"})

    if from_date:
        df = df[df["datetime"] >= pd.Timestamp(from_date, tz="UTC")]
    if to_date:
        df = df[df["datetime"] <= pd.Timestamp(to_date, tz="UTC")]

    df = df.sort_values(["coin", "unix_ts"]).reset_index(drop=True)
    return df


def infer_n_days(df: pd.DataFrame) -> float:
    if df.empty:
        return 1.0
    span_days = (df["datetime"].max() - df["datetime"].min()).total_seconds() / 86400.0
    return max(1.0, span_days)


def build_coin_data(df: pd.DataFrame) -> Dict[str, Dict[str, object]]:
    coin_data: Dict[str, Dict[str, object]] = {}
    for coin in COINS:
        cdf = df[df["coin"] == coin].sort_values("unix_ts")
        outcomes = cdf["outcome"].tolist()
        ud = ["U" if o == "UP" else "D" if o == "DOWN" else None for o in outcomes]
        valid = [o for o in outcomes if o is not None]
        if not valid:
            continue
        base_down = sum(1 for o in valid if o == "DOWN") / len(valid)
        base_up = 1 - base_down
        n_cycles = len(cdf)
        coin_data[coin] = {
            "ud": ud,
            "base_down": base_down,
            "base_up": base_up,
            "n_cycles": n_cycles,
        }
    return coin_data


def test_min_streak(ud_seq: list, streak_dir: str, min_streak: int, buy_side_ud: str) -> Tuple[int, int]:
    trades, wins = 0, 0
    for i in range(min_streak, len(ud_seq)):
        if ud_seq[i] is None:
            continue
        ok = True
        for j in range(1, min_streak + 1):
            if i - j < 0 or ud_seq[i - j] is None or ud_seq[i - j] != streak_dir:
                ok = False
                break
        if not ok:
            continue
        trades += 1
        if ud_seq[i] == buy_side_ud:
            wins += 1
    return trades, wins


def test_pattern_coin(ud_seq: list, pattern: str, buy_side: str) -> Tuple[int, int]:
    """Count trades and wins for pattern -> buy_side."""
    p_len = len(pattern)
    trades, wins = 0, 0
    for i in range(p_len, len(ud_seq)):
        if ud_seq[i] is None:
            continue
        window = ud_seq[i - p_len:i]
        if any(o is None for o in window):
            continue
        if "".join(window) == pattern:
            trades += 1
            if ud_seq[i] == buy_side:
                wins += 1
    return trades, wins


def compute_metrics(trades: int, wins: int, base_rate: float, n_days: float):
    """Compute WR, break-even price, significance, frequency."""
    if trades == 0:
        return None

    wr = wins / trades
    delta = wr - base_rate

    # Statistical test
    bt = stats.binomtest(wins, trades, base_rate, alternative="greater")
    p_val = bt.pvalue

    # z-score
    se = np.sqrt(base_rate * (1 - base_rate) / trades)
    z = (wr - base_rate) / se if se > 0 else 0

    # Break-even entry price: EV = WR * (payout - entry) + (1-WR) * (-entry) = 0
    #   payout = 1.0 * (1 - FEE_RATE) = 0.985
    #   EV = WR * 0.985 - entry = 0
    #   => entry_breakeven = WR * 0.985
    payout = 1.0 * (1 - FEE_RATE)
    be_entry = wr * payout
    max_ask = be_entry - SLIPPAGE  # max ask price you can pay

    # Expected value at different entry prices
    # At ask = 0.50: EV = WR * (0.985 - 0.50 - slippage) + (1-WR) * (-0.50 - slippage)
    ev_at_50 = wr * (payout - 0.50 - SLIPPAGE) + (1 - wr) * (-0.50 - SLIPPAGE)
    ev_at_48 = wr * (payout - 0.48 - SLIPPAGE) + (1 - wr) * (-0.48 - SLIPPAGE)

    # Frequency
    trades_per_day = trades / n_days

    return {
        "trades": trades,
        "wins": wins,
        "wr": wr,
        "base": base_rate,
        "delta": delta,
        "z": z,
        "p": p_val,
        "be_entry": be_entry,
        "max_ask": max_ask,
        "ev_at_50": ev_at_50,
        "ev_at_48": ev_at_48,
        "trades_per_day": trades_per_day,
    }


def eval_strategy_oos(
    row: dict,
    coin_data_oos: Dict[str, Dict[str, object]],
    n_days_oos: float,
) -> Optional[dict]:
    """Evaluate one in-sample strategy row on OOS data without re-fitting."""
    buy_side_ud = "D" if row["side"] == "DOWN" else "U"

    if row["scope"] == "single":
        coin = row["coin"]
        if coin not in coin_data_oos:
            return None
        cd = coin_data_oos[coin]
        base = float(cd["base_down"] if buy_side_ud == "D" else cd["base_up"])
        t, w = test_pattern_coin(cd["ud"], row["pattern"], buy_side_ud)
        if t == 0:
            return None
        m = compute_metrics(t, w, base, n_days_oos)
        return m

    if row["scope"] == "combined":
        total_t, total_w = 0, 0
        bases = []
        for coin in COINS:
            cd = coin_data_oos.get(coin)
            if cd is None:
                continue
            base = float(cd["base_down"] if buy_side_ud == "D" else cd["base_up"])
            bases.append(base)
            t, w = test_pattern_coin(cd["ud"], row["pattern"], buy_side_ud)
            total_t += t
            total_w += w

        if total_t == 0 or not bases:
            return None
        avg_base = float(np.mean(bases))
        return compute_metrics(total_t, total_w, avg_base, n_days_oos)

    if row["scope"] == "min_streak":
        pattern = str(row["pattern"])
        streak_dir = pattern[0]
        min_streak = len(pattern.replace("+", ""))
        coin_label = row["coin"]
        coins_to_scan = COINS if coin_label == "ALL" else [coin_label]
        total_t, total_w = 0, 0
        bases = []

        for coin in coins_to_scan:
            cd = coin_data_oos.get(coin)
            if cd is None:
                continue
            base = float(cd["base_down"] if buy_side_ud == "D" else cd["base_up"])
            bases.append(base)
            t, w = test_min_streak(cd["ud"], streak_dir, min_streak, buy_side_ud)
            total_t += t
            total_w += w

        if total_t == 0 or not bases:
            return None
        avg_base = float(np.mean(bases))
        return compute_metrics(total_t, total_w, avg_base, n_days_oos)

    return None


def main():
    parser = argparse.ArgumentParser(description="Exhaustive pattern scan")
    parser.add_argument("--max-len", type=int, default=6,
                        help="Max pattern length (default 6)")
    parser.add_argument("--min-trades", type=int, default=100,
                        help="Min trades to report (default 100)")
    parser.add_argument("--top", type=int, default=60,
                        help="Show top N results (default 60)")
    parser.add_argument("--from-date", type=str, default=None,
                        help="UTC date filter start, e.g. 2026-01-01")
    parser.add_argument("--to-date", type=str, default=None,
                        help="UTC date filter end, e.g. 2026-03-01")
    parser.add_argument("--oos-days", type=int, default=0,
                        help="Reserve last N days for out-of-sample validation")
    parser.add_argument("--oos-min-trades", type=int, default=30,
                        help="Min OOS trades to report a strategy (default 30)")
    args = parser.parse_args()

    print("Loading Telonex data...")
    full_df = load_data(args.from_date, args.to_date)
    if full_df.empty:
        print("No data after filters. Exiting.")
        sys.exit(1)

    oos_df = None
    if args.oos_days > 0:
        cutoff = full_df["datetime"].max() - pd.Timedelta(days=args.oos_days)
        df = full_df[full_df["datetime"] < cutoff].copy()
        oos_df = full_df[full_df["datetime"] >= cutoff].copy()
        if df.empty or oos_df.empty:
            print("OOS split invalid (train or OOS empty). Use fewer --oos-days.")
            sys.exit(1)
        print(
            f"  Train: {df['datetime'].min()} -> {df['datetime'].max()} ({len(df):,} rows)"
        )
        print(
            f"  OOS:   {oos_df['datetime'].min()} -> {oos_df['datetime'].max()} ({len(oos_df):,} rows)"
        )
    else:
        df = full_df

    n_days = infer_n_days(df)

    # Precompute per-coin sequences and base rates
    coin_data = build_coin_data(df)
    for coin in COINS:
        if coin not in coin_data:
            continue
        cd = coin_data[coin]
        base_down = float(cd["base_down"])
        base_up = float(cd["base_up"])
        n_cycles = int(cd["n_cycles"])
        print(f"  {coin}: {n_cycles:,} cycles, base DOWN={base_down:.1%}, base UP={base_up:.1%}")

    # =========================================================================
    # SCAN 1: Per-coin patterns (each coin individually)
    # =========================================================================
    print(f"\nScanning patterns length 2-{args.max_len} x {len(COINS)} coins x 2 sides...")
    all_results = []

    for p_len in range(2, args.max_len + 1):
        patterns = ["".join(bits) for bits in product("UD", repeat=p_len)]
        for pattern in patterns:
            for buy_side_ud, buy_side_name in [("D", "DOWN"), ("U", "UP")]:
                # Per coin
                for coin in COINS:
                    cd = coin_data[coin]
                    base = cd["base_down"] if buy_side_ud == "D" else cd["base_up"]
                    t, w = test_pattern_coin(cd["ud"], pattern, buy_side_ud)
                    if t < args.min_trades:
                        continue
                    m = compute_metrics(t, w, float(base), n_days)
                    if m is None:
                        continue
                    m["pattern"] = pattern
                    m["side"] = buy_side_name
                    m["coin"] = coin
                    m["scope"] = "single"
                    all_results.append(m)

                # All coins combined
                total_t, total_w = 0, 0
                bases = []
                for coin in COINS:
                    cd = coin_data[coin]
                    base = cd["base_down"] if buy_side_ud == "D" else cd["base_up"]
                    bases.append(base)
                    t, w = test_pattern_coin(cd["ud"], pattern, buy_side_ud)
                    total_t += t
                    total_w += w

                if total_t < args.min_trades:
                    continue
                avg_base = float(np.mean(bases))
                m = compute_metrics(total_t, total_w, avg_base, n_days)
                if m is None:
                    continue
                m["pattern"] = pattern
                m["side"] = buy_side_name
                m["coin"] = "ALL"
                m["scope"] = "combined"
                all_results.append(m)

        print(f"  Length {p_len}: done ({len(all_results)} results so far)")

    # =========================================================================
    # SCAN 2: "Minimum streak" patterns (streak >= N, not exact)
    # =========================================================================
    print("\nScanning minimum-streak patterns (streak >= N)...")
    for min_streak in range(2, args.max_len + 1):
        for streak_dir, buy_side_ud, buy_side_name in [("U", "D", "DOWN"), ("D", "U", "UP")]:
            for coin_label in COINS + ["ALL"]:
                coins_to_scan = COINS if coin_label == "ALL" else [coin_label]
                total_t, total_w = 0, 0
                bases = []

                for coin in coins_to_scan:
                    cd = coin_data[coin]
                    base = cd["base_down"] if buy_side_ud == "D" else cd["base_up"]
                    bases.append(base)
                    t, w = test_min_streak(cd["ud"], streak_dir, min_streak, buy_side_ud)
                    total_t += t
                    total_w += w

                if total_t < args.min_trades:
                    continue
                avg_base = float(np.mean(bases))
                m = compute_metrics(total_t, total_w, avg_base, n_days)
                if m is None:
                    continue
                m["pattern"] = f"{streak_dir * min_streak}+"
                m["side"] = buy_side_name
                m["coin"] = coin_label
                m["scope"] = "min_streak"
                all_results.append(m)

    # =========================================================================
    # Count total tests for Bonferroni
    # =========================================================================
    n_tests = len(all_results)
    bonf_alpha = 0.05 / n_tests if n_tests > 0 else 0.05

    print(f"\nTotal tests: {n_tests}")
    print(f"Bonferroni alpha: {bonf_alpha:.6f}")

    # Tag significance
    for r in all_results:
        r["bonf_sig"] = r["p"] < bonf_alpha
        r["nom_sig"] = r["p"] < 0.05

    # =========================================================================
    # OUTPUT 1: Everything significant, sorted by max_ask (most profitable first)
    # =========================================================================
    sig = [r for r in all_results if r["bonf_sig"]]
    sig.sort(key=lambda x: -x["max_ask"])

    print(f"\n{'=' * 110}")
    print(f"  ALL BONFERRONI-SIGNIFICANT RESULTS ({len(sig)} / {n_tests})")
    print(f"  Sorted by max_ask (highest = most room for profit)")
    print(f"{'=' * 110}")
    print(f"  {'Coin':<5} {'Pattern':<8} {'Side':<5} {'N':>6} {'WR':>6} {'Base':>5} "
          f"{'Delta':>6} {'z':>6} {'p-val':>10} {'MaxAsk':>7} {'EV@.48':>7} {'EV@.50':>7} {'Tr/day':>6}")
    print(f"  {'-'*5} {'-'*8} {'-'*5} {'-'*6} {'-'*6} {'-'*5} "
          f"{'-'*6} {'-'*6} {'-'*10} {'-'*7} {'-'*7} {'-'*7} {'-'*6}")

    for r in sig:
        ev48_str = f"${r['ev_at_48']:+.3f}" if abs(r['ev_at_48']) < 1 else f"${r['ev_at_48']:+.2f}"
        ev50_str = f"${r['ev_at_50']:+.3f}" if abs(r['ev_at_50']) < 1 else f"${r['ev_at_50']:+.2f}"
        print(f"  {r['coin']:<5} {r['pattern']:<8} {r['side']:<5} {r['trades']:>6,} {r['wr']:>5.1%} "
              f"{r['base']:>4.1%} {r['delta']:>+5.1%} {r['z']:>+5.1f} {r['p']:>10.2e} "
              f"${r['max_ask']:>.3f} {ev48_str:>7} {ev50_str:>7} {r['trades_per_day']:>5.1f}")

    # =========================================================================
    # OUTPUT 2: Top strategies by EV at ask=$0.50 (realistic entry)
    # =========================================================================
    profitable_50 = [r for r in all_results if r["bonf_sig"] and r["ev_at_50"] > 0]
    profitable_50.sort(key=lambda x: -x["ev_at_50"])

    print(f"\n{'=' * 90}")
    print(f"  PROFITABLE AT ASK=$0.50 (after slippage+fees) - {len(profitable_50)} strategies")
    print(f"{'=' * 90}")

    if profitable_50:
        print(f"  {'Coin':<5} {'Pattern':<8} {'Side':<5} {'N':>6} {'WR':>6} "
              f"{'MaxAsk':>7} {'EV/trade':>9} {'$/day':>7} {'Tr/day':>6}")
        print(f"  {'-'*5} {'-'*8} {'-'*5} {'-'*6} {'-'*6} "
              f"{'-'*7} {'-'*9} {'-'*7} {'-'*6}")
        for r in profitable_50:
            daily_ev = r["ev_at_50"] * r["trades_per_day"]
            print(f"  {r['coin']:<5} {r['pattern']:<8} {r['side']:<5} {r['trades']:>6,} {r['wr']:>5.1%} "
                  f"${r['max_ask']:>.3f} ${r['ev_at_50']:>+7.4f} ${daily_ev:>+6.3f} {r['trades_per_day']:>5.1f}")
    else:
        print("  NONE - no strategy is profitable at ask=$0.50 after Bonferroni correction.")

    # =========================================================================
    # OUTPUT 3: Top strategies by EV at ask=$0.48
    # =========================================================================
    profitable_48 = [r for r in all_results if r["bonf_sig"] and r["ev_at_48"] > 0]
    profitable_48.sort(key=lambda x: -x["ev_at_48"])

    print(f"\n{'=' * 90}")
    print(f"  PROFITABLE AT ASK=$0.48 (optimistic entry) - {len(profitable_48)} strategies")
    print(f"{'=' * 90}")

    if profitable_48:
        print(f"  {'Coin':<5} {'Pattern':<8} {'Side':<5} {'N':>6} {'WR':>6} "
              f"{'MaxAsk':>7} {'EV/trade':>9} {'$/day':>7} {'Tr/day':>6}")
        print(f"  {'-'*5} {'-'*8} {'-'*5} {'-'*6} {'-'*6} "
              f"{'-'*7} {'-'*9} {'-'*7} {'-'*6}")
        for r in profitable_48:
            daily_ev = r["ev_at_48"] * r["trades_per_day"]
            print(f"  {r['coin']:<5} {r['pattern']:<8} {r['side']:<5} {r['trades']:>6,} {r['wr']:>5.1%} "
                  f"${r['max_ask']:>.3f} ${r['ev_at_48']:>+7.4f} ${daily_ev:>+6.3f} {r['trades_per_day']:>5.1f}")
    else:
        print("  NONE.")

    # =========================================================================
    # OUTPUT 4: Strategy configurator - what max_ask to use for each
    # =========================================================================
    # Only show Bonferroni-significant with max_ask > 0.45
    viable = [r for r in all_results if r["bonf_sig"] and r["max_ask"] > 0.45]
    viable.sort(key=lambda x: (-x["max_ask"], -x["trades"]))

    print(f"\n{'=' * 100}")
    print(f"  STRATEGY CONFIGURATOR: Bonferroni-significant with max_ask > $0.45")
    print(f"  Use these max_ask values in the live bot to ensure +EV")
    print(f"{'=' * 100}")
    print(f"  {'Coin':<5} {'Pattern':<8} {'Side':<5} {'N':>6} {'WR':>6} "
          f"{'MaxAsk':>7} {'Frequency':>12} {'Note'}")
    print(f"  {'-'*5} {'-'*8} {'-'*5} {'-'*6} {'-'*6} "
          f"{'-'*7} {'-'*12} {'-'*30}")

    for r in viable:
        freq = f"{r['trades_per_day']:.1f}/day"
        note = ""
        if r["max_ask"] >= 0.53:
            note = "STRONG"
        elif r["max_ask"] >= 0.50:
            note = "viable"
        elif r["max_ask"] >= 0.48:
            note = "tight"
        else:
            note = "marginal"
        print(f"  {r['coin']:<5} {r['pattern']:<8} {r['side']:<5} {r['trades']:>6,} {r['wr']:>5.1%} "
              f"${r['max_ask']:>.3f} {freq:>12} {note}")

    # =========================================================================
    # OUTPUT 5: Out-of-sample validation (optional)
    # =========================================================================
    survivors_oos = []
    if oos_df is not None:
        n_days_oos = infer_n_days(oos_df)
        coin_data_oos = build_coin_data(oos_df)
        oos_eval = []

        for row in sig:
            oos_m = eval_strategy_oos(row, coin_data_oos, n_days_oos)
            if oos_m is None:
                continue
            out = dict(row)
            out["oos_trades"] = oos_m["trades"]
            out["oos_wins"] = oos_m["wins"]
            out["oos_wr"] = oos_m["wr"]
            out["oos_base"] = oos_m["base"]
            out["oos_delta"] = oos_m["delta"]
            out["oos_p"] = oos_m["p"]
            out["oos_max_ask"] = oos_m["max_ask"]
            out["oos_ev_at_50"] = oos_m["ev_at_50"]
            out["oos_ev_at_48"] = oos_m["ev_at_48"]
            oos_eval.append(out)

        survivors_oos = [
            r for r in oos_eval
            if r["oos_trades"] >= args.oos_min_trades
            and r["oos_wr"] > r["oos_base"]
            and r["oos_ev_at_50"] > 0
        ]
        survivors_oos.sort(key=lambda x: -x["oos_ev_at_50"])

        print(f"\n{'=' * 110}")
        print(f"  OOS VALIDATION (last {args.oos_days} days holdout)")
        print(f"  In-sample Bonferroni-significant tested on unseen data")
        print(f"{'=' * 110}")
        print(f"  OOS candidates tested: {len(oos_eval)}")
        print(f"  OOS survivors (WR>base, EV@0.50>0, N>={args.oos_min_trades}): {len(survivors_oos)}")

        if survivors_oos:
            print(f"  {'Coin':<5} {'Pattern':<8} {'Side':<5} {'N_OOS':>7} {'WR_OOS':>7} {'Base':>5} "
                  f"{'Delta':>6} {'EV@.50':>8} {'MaxAsk':>7}")
            print(f"  {'-'*5} {'-'*8} {'-'*5} {'-'*7} {'-'*7} {'-'*5} {'-'*6} {'-'*8} {'-'*7}")
            for r in survivors_oos:
                print(
                    f"  {r['coin']:<5} {r['pattern']:<8} {r['side']:<5} "
                    f"{r['oos_trades']:>7,} {r['oos_wr']:>6.1%} {r['oos_base']:>4.1%} "
                    f"{r['oos_delta']:>+5.1%} ${r['oos_ev_at_50']:>+7.4f} ${r['oos_max_ask']:>.3f}"
                )
        else:
            print("  No OOS survivors under the current thresholds.")

    # =========================================================================
    # OUTPUT 6: Summary statistics
    # =========================================================================
    print(f"\n{'=' * 70}")
    print(f"  SUMMARY")
    print(f"{'=' * 70}")
    print(f"  Total tests run:             {n_tests}")
    print(f"  Bonferroni alpha:            {bonf_alpha:.6f}")
    print(f"  Bonferroni significant:      {len(sig)}")
    print(f"  Profitable at ask=$0.50:     {len(profitable_50)}")
    print(f"  Profitable at ask=$0.48:     {len(profitable_48)}")
    print(f"  Max_ask > $0.50:             {len([r for r in sig if r['max_ask'] > 0.50])}")
    print(f"  Max_ask > $0.53:             {len([r for r in sig if r['max_ask'] > 0.53])}")
    if oos_df is not None:
        print(f"  OOS survivors:               {len(survivors_oos)}")

    # Best single strategy
    if sig:
        best = max(sig, key=lambda x: x["max_ask"])
        print(f"\n  BEST STRATEGY: {best['coin']} {best['pattern']}->{best['side']}")
        print(f"    WR={best['wr']:.1%}, N={best['trades']:,}, max_ask=${best['max_ask']:.3f}")
        print(f"    {best['trades_per_day']:.1f} trades/day")
        print(f"    At $5/trade, max_ask=${best['max_ask']:.2f}: "
              f"EV ~ ${best['wr'] * 0.985 * 5 - best['max_ask'] * 5:.2f}/trade")


if __name__ == "__main__":
    main()
