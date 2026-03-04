"""Top-30 strategy robustness suite.

Workflow:
1) Run exhaustive pattern scan on selected date range.
2) Keep top N Bonferroni-significant strategies by max_ask.
3) Validate each strategy in multiple out-of-sample holdouts.
4) Validate each strategy across base-rate regimes (quantile bins).

This script is intentionally conservative to avoid overfitting.

Usage:
    python research/top30_oos_suite.py
    python research/top30_oos_suite.py --top 30 --min-trades 100
    python research/top30_oos_suite.py --holdouts 7,14,21 --min-oos-trades 30
"""

from __future__ import annotations

import argparse
from itertools import product
from typing import Dict, List, Optional, Tuple

import pandas as pd

from base_regime_validation import (
    PatternSpec,
    add_regimes,
    build_sequences,
    daily_base_down,
    eval_regime,
    extract_events,
)
from full_pattern_scan import (
    COINS,
    build_coin_data,
    compute_metrics,
    eval_strategy_oos,
    infer_n_days,
    load_data,
    test_min_streak,
    test_pattern_coin,
)


def parse_holdouts(text: str) -> List[int]:
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    return sorted(set(out))


def run_scan(df: pd.DataFrame, max_len: int, min_trades: int) -> Tuple[List[dict], float]:
    n_days = infer_n_days(df)
    coin_data = build_coin_data(df)
    all_results: List[dict] = []

    # Exact patterns by length
    for p_len in range(2, max_len + 1):
        patterns = ["".join(bits) for bits in product("UD", repeat=p_len)]
        for pattern in patterns:
            for buy_side_ud, buy_side_name in [("D", "DOWN"), ("U", "UP")]:
                # Per coin
                for coin in COINS:
                    cd = coin_data.get(coin)
                    if cd is None:
                        continue
                    base = float(cd["base_down"] if buy_side_ud == "D" else cd["base_up"])
                    t, w = test_pattern_coin(cd["ud"], pattern, buy_side_ud)
                    if t < min_trades:
                        continue
                    m = compute_metrics(t, w, base, n_days)
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
                    cd = coin_data.get(coin)
                    if cd is None:
                        continue
                    base = float(cd["base_down"] if buy_side_ud == "D" else cd["base_up"])
                    bases.append(base)
                    t, w = test_pattern_coin(cd["ud"], pattern, buy_side_ud)
                    total_t += t
                    total_w += w

                if total_t < min_trades or not bases:
                    continue
                avg_base = float(sum(bases) / len(bases))
                m = compute_metrics(total_t, total_w, avg_base, n_days)
                if m is None:
                    continue
                m["pattern"] = pattern
                m["side"] = buy_side_name
                m["coin"] = "ALL"
                m["scope"] = "combined"
                all_results.append(m)

    # Min-streak patterns
    for min_streak in range(2, max_len + 1):
        for streak_dir, buy_side_ud, buy_side_name in [("U", "D", "DOWN"), ("D", "U", "UP")]:
            for coin_label in COINS + ["ALL"]:
                coins_to_scan = COINS if coin_label == "ALL" else [coin_label]
                total_t, total_w = 0, 0
                bases = []

                for coin in coins_to_scan:
                    cd = coin_data.get(coin)
                    if cd is None:
                        continue
                    base = float(cd["base_down"] if buy_side_ud == "D" else cd["base_up"])
                    bases.append(base)
                    t, w = test_min_streak(cd["ud"], streak_dir, min_streak, buy_side_ud)
                    total_t += t
                    total_w += w

                if total_t < min_trades or not bases:
                    continue
                avg_base = float(sum(bases) / len(bases))
                m = compute_metrics(total_t, total_w, avg_base, n_days)
                if m is None:
                    continue
                m["pattern"] = f"{streak_dir * min_streak}+"
                m["side"] = buy_side_name
                m["coin"] = coin_label
                m["scope"] = "min_streak"
                all_results.append(m)

    n_tests = len(all_results)
    bonf_alpha = 0.05 / n_tests if n_tests > 0 else 0.05
    for r in all_results:
        r["bonf_sig"] = bool(r["p"] < bonf_alpha)

    return all_results, bonf_alpha


def evaluate_holdouts(
    df: pd.DataFrame,
    strategies: List[dict],
    holdouts: List[int],
    min_oos_trades: int,
) -> Dict[int, Dict[str, dict]]:
    out: Dict[int, Dict[str, dict]] = {}
    dt_max = df["datetime"].max()

    for days in holdouts:
        cutoff = dt_max - pd.Timedelta(days=days)
        oos_df = df[df["datetime"] >= cutoff].copy()
        if oos_df.empty:
            out[days] = {}
            continue

        oos_coin = build_coin_data(oos_df)
        n_days_oos = infer_n_days(oos_df)
        result_map: Dict[str, dict] = {}

        for s in strategies:
            key = f"{s['coin']}|{s['pattern']}|{s['side']}|{s['scope']}"
            m = eval_strategy_oos(s, oos_coin, n_days_oos)
            if m is None:
                result_map[key] = {
                    "trades": 0,
                    "wr": 0.0,
                    "base": 0.0,
                    "delta": 0.0,
                    "p": 1.0,
                    "ev_at_50": -1.0,
                    "pass_soft": False,
                    "pass_strict": False,
                }
                continue

            pass_soft = (
                m["trades"] >= min_oos_trades
                and m["wr"] > m["base"]
                and m["ev_at_50"] > 0
            )
            pass_strict = pass_soft and (float(m["p"]) < 0.05)

            result_map[key] = {
                "trades": int(m["trades"]),
                "wr": float(m["wr"]),
                "base": float(m["base"]),
                "delta": float(m["delta"]),
                "p": float(m["p"]),
                "ev_at_50": float(m["ev_at_50"]),
                "max_ask": float(m["max_ask"]),
                "pass_soft": bool(pass_soft),
                "pass_strict": bool(pass_strict),
            }

        out[days] = result_map

    return out


def evaluate_quantile_regimes(
    df: pd.DataFrame,
    strategies: List[dict],
    min_regime_trades: int,
) -> Dict[str, dict]:
    df_local = df.copy()
    if "date" not in df_local.columns:
        df_local["date"] = df_local["datetime"].dt.date

    seq = build_sequences(df_local)
    out: Dict[str, dict] = {}

    for s in strategies:
        key = f"{s['coin']}|{s['pattern']}|{s['side']}|{s['scope']}"
        scope = "single" if s["scope"] == "single" else "all"
        coin = s["coin"] if scope == "single" else "ALL"
        spec = PatternSpec(
            name=f"{s['coin']} {s['pattern']}->{s['side']}",
            scope=scope,
            coin=coin,
            pattern=s["pattern"],
            side=s["side"],
        )

        events = extract_events(seq, spec)
        daily_raw = daily_base_down(df_local, spec)
        min_day_markets = 900 if spec.scope == "all" else 220
        daily_raw = daily_raw[daily_raw["n_markets"] >= min_day_markets].copy()
        if daily_raw.empty:
            out[key] = {"passes": 0, "total": 0, "details": {}}
            continue

        daily = add_regimes(daily_raw)
        details: Dict[str, dict] = {}
        passes = 0
        total = 0

        for regime in ["low_down", "mid", "high_down"]:
            r = eval_regime(df_local, events, daily, spec, spec.side, "q_regime", regime)
            if r is None:
                continue
            total += 1
            pass_sig = (
                r["trades"] >= min_regime_trades
                and r["wr"] > r["base"]
                and r["p"] < 0.05
            )
            if pass_sig:
                passes += 1
            details[regime] = {
                "trades": int(r["trades"]),
                "wr": float(r["wr"]),
                "base": float(r["base"]),
                "delta": float(r["delta"]),
                "p": float(r["p"]),
                "ev_at_50": float(r["ev_at_50"]),
                "pass": bool(pass_sig),
            }

        out[key] = {"passes": passes, "total": total, "details": details}

    return out


def fmt_pct(x: float) -> str:
    return f"{x*100:.1f}%"


def main() -> None:
    parser = argparse.ArgumentParser(description="Top-30 OOS robustness suite")
    parser.add_argument("--max-len", type=int, default=6)
    parser.add_argument("--min-trades", type=int, default=100)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--holdouts", type=str, default="7,14,21")
    parser.add_argument("--min-oos-trades", type=int, default=30)
    parser.add_argument("--min-regime-trades", type=int, default=120)
    parser.add_argument("--from-date", type=str, default=None)
    parser.add_argument("--to-date", type=str, default=None)
    args = parser.parse_args()

    holdouts = parse_holdouts(args.holdouts)
    df = load_data(args.from_date, args.to_date)
    if df.empty:
        print("No data after filters.")
        return

    print("Running exhaustive scan...")
    all_results, bonf_alpha = run_scan(df, args.max_len, args.min_trades)
    sig = [r for r in all_results if r["bonf_sig"]]
    sig.sort(key=lambda x: (-x["max_ask"], -x["trades"]))
    top = sig[: args.top]

    print(f"  Total tests: {len(all_results)}")
    print(f"  Bonferroni alpha: {bonf_alpha:.6f}")
    print(f"  Significant: {len(sig)}")
    print(f"  Top selected: {len(top)}")

    print("\nTop strategies (in-sample):")
    print(
        f"  {'#':>2} {'Coin':<5} {'Pattern':<8} {'Side':<5} {'N':>6} {'WR':>6} {'Base':>6} {'MaxAsk':>7} {'EV@.50':>8}"
    )
    for i, r in enumerate(top, 1):
        print(
            f"  {i:>2} {r['coin']:<5} {r['pattern']:<8} {r['side']:<5} {r['trades']:>6,} "
            f"{fmt_pct(r['wr']):>6} {fmt_pct(r['base']):>6} ${r['max_ask']:.3f} ${r['ev_at_50']:+.4f}"
        )

    holdout_eval = evaluate_holdouts(df, top, holdouts, args.min_oos_trades)
    regime_eval = evaluate_quantile_regimes(df, top, args.min_regime_trades)

    print("\nOOS scoreboard:")
    print(
        f"  {'Coin':<5} {'Pattern':<8} {'Side':<5} "
        + " ".join([f"H{d:>2}" for d in holdouts])
        + "  Regimes"
    )

    final_rows = []
    for r in top:
        key = f"{r['coin']}|{r['pattern']}|{r['side']}|{r['scope']}"
        oos_marks = []
        strict_passes = 0
        for d in holdouts:
            m = holdout_eval.get(d, {}).get(key)
            ok = bool(m and m["pass_strict"])
            strict_passes += int(ok)
            oos_marks.append("OK" if ok else "--")

        reg = regime_eval.get(key, {"passes": 0, "total": 0})
        reg_passes = int(reg["passes"])
        reg_total = int(reg["total"])

        print(
            f"  {r['coin']:<5} {r['pattern']:<8} {r['side']:<5} "
            + " ".join([f"{m:>3}" for m in oos_marks])
            + f"   {reg_passes}/{reg_total}"
        )

        final_rows.append(
            {
                "coin": r["coin"],
                "pattern": r["pattern"],
                "side": r["side"],
                "scope": r["scope"],
                "in_wr": float(r["wr"]),
                "in_base": float(r["base"]),
                "in_max_ask": float(r["max_ask"]),
                "in_ev50": float(r["ev_at_50"]),
                "oos_passes": strict_passes,
                "oos_total": len(holdouts),
                "reg_passes": reg_passes,
                "reg_total": reg_total,
            }
        )

    # Final survivors: strict in all OOS holdouts and >=2/3 quantile regimes
    survivors = [
        x for x in final_rows
        if x["oos_passes"] == x["oos_total"]
        and x["reg_total"] >= 3
        and x["reg_passes"] >= 2
    ]
    survivors.sort(key=lambda x: (-x["in_ev50"], -x["in_max_ask"]))

    print("\nFinal survivors (strict):")
    if not survivors:
        print("  None pass all OOS holdouts with regime consistency threshold.")
    else:
        print(
            f"  {'Coin':<5} {'Pattern':<8} {'Side':<5} {'WR':>6} {'Base':>6} {'MaxAsk':>7} {'EV@.50':>8} {'OOS':>5} {'Reg':>5}"
        )
        for s in survivors:
            print(
                f"  {s['coin']:<5} {s['pattern']:<8} {s['side']:<5} "
                f"{fmt_pct(s['in_wr']):>6} {fmt_pct(s['in_base']):>6} ${s['in_max_ask']:.3f} "
                f"${s['in_ev50']:+.4f} {s['oos_passes']}/{s['oos_total']:>2} {s['reg_passes']}/{s['reg_total']:>2}"
            )

        print("\nSurvivor detailed metrics:")
        for s in survivors:
            key = f"{s['coin']}|{s['pattern']}|{s['side']}|{s['scope']}"
            print(f"  {s['coin']} {s['pattern']}->{s['side']} ({s['scope']})")
            for d in holdouts:
                m = holdout_eval.get(d, {}).get(key)
                if not m or m["trades"] == 0:
                    print(f"    H{d}: no trades")
                    continue
                print(
                    f"    H{d}: N={m['trades']:,} WR={fmt_pct(m['wr'])} Base={fmt_pct(m['base'])} "
                    f"Delta={fmt_pct(m['delta'])} p={m['p']:.2e} EV@0.50=${m['ev_at_50']:+.4f} "
                    f"MaxAsk=${m.get('max_ask', 0.0):.3f}"
                )

            reg = regime_eval.get(key, {"details": {}})
            for regime in ["low_down", "mid", "high_down"]:
                rd = reg["details"].get(regime)
                if not rd:
                    print(f"    {regime}: no data")
                    continue
                print(
                    f"    {regime}: N={rd['trades']:,} WR={fmt_pct(rd['wr'])} Base={fmt_pct(rd['base'])} "
                    f"Delta={fmt_pct(rd['delta'])} p={rd['p']:.2e} EV@0.50=${rd['ev_at_50']:+.4f} "
                    f"pass={'YES' if rd['pass'] else 'NO'}"
                )


if __name__ == "__main__":
    main()
