"""Validate robust patterns across base-rate regimes.

Goal:
    Test whether OOS-surviving patterns remain better than baseline in
    different market environments (UP-dominant vs DOWN-dominant).

This is a robustness check, not a parameter search.

Usage:
    python research/base_regime_validation.py
    python research/base_regime_validation.py --min-trades 100
    python research/base_regime_validation.py --from-date 2026-01-01 --to-date 2026-03-01
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from scipy import stats


DATA_FILE = Path(__file__).parent.parent / "data" / "telonex_updown_5m.parquet"
COINS = ["BTC", "ETH", "SOL", "XRP"]
FEE_RATE = 0.015
SLIPPAGE = 0.03


@dataclass(frozen=True)
class PatternSpec:
    name: str
    scope: str  # "single" or "all"
    coin: str
    pattern: str
    side: str   # "DOWN" or "UP"


# Unique OOS-surviving families from full_pattern_scan holdout test
PATTERNS: List[PatternSpec] = [
    PatternSpec("ETH UUU->DOWN", "single", "ETH", "UUU", "DOWN"),
    PatternSpec("ALL DUUU->DOWN", "all", "ALL", "DUUU", "DOWN"),
    PatternSpec("ALL UUU->DOWN", "all", "ALL", "UUU", "DOWN"),
    PatternSpec("ALL UUUU->DOWN", "all", "ALL", "UUUU", "DOWN"),
]


def load_data(from_date: Optional[str], to_date: Optional[str]) -> pd.DataFrame:
    df = pd.read_parquet(DATA_FILE)
    df["coin"] = df["slug"].str.extract(r"^(\w+)-updown-5m-")[0].str.upper()
    df["unix_ts"] = df["slug"].str.extract(r"-(\d+)$")[0].astype(float)
    df["datetime"] = pd.to_datetime(df["unix_ts"], unit="s", utc=True)
    df["date"] = df["datetime"].dt.date
    df["outcome"] = df["result_id"].map({"0": "UP", "1": "DOWN"})
    df = df[df["coin"].isin(COINS)].copy()

    if from_date:
        df = df[df["datetime"] >= pd.Timestamp(from_date, tz="UTC")]
    if to_date:
        df = df[df["datetime"] <= pd.Timestamp(to_date, tz="UTC")]

    return df.sort_values(["coin", "unix_ts"]).reset_index(drop=True)


def build_sequences(df: pd.DataFrame) -> Dict[str, List[dict]]:
    sequences: Dict[str, List[dict]] = {}
    for coin in COINS:
        cdf = df[df["coin"] == coin].sort_values("unix_ts")
        seq: List[dict] = []
        for row in cdf.itertuples(index=False):
            if row.outcome not in {"UP", "DOWN"}:
                continue
            seq.append(
                {
                    "ud": "U" if row.outcome == "UP" else "D",
                    "date": row.date,
                }
            )
        sequences[coin] = seq
    return sequences


def extract_events(sequences: Dict[str, List[dict]], spec: PatternSpec) -> pd.DataFrame:
    side_ud = "D" if spec.side == "DOWN" else "U"
    coins = [spec.coin] if spec.scope == "single" else COINS
    p_len = len(spec.pattern)

    rows = []
    for coin in coins:
        seq = sequences.get(coin, [])
        for i in range(p_len, len(seq)):
            window = "".join(seq[j]["ud"] for j in range(i - p_len, i))
            if window != spec.pattern:
                continue
            win = 1 if seq[i]["ud"] == side_ud else 0
            rows.append({"coin": coin, "date": seq[i]["date"], "win": win})

    return pd.DataFrame(rows)


def daily_base_down(df: pd.DataFrame, spec: PatternSpec) -> pd.DataFrame:
    scope_df = df if spec.scope == "all" else df[df["coin"] == spec.coin]
    grp = scope_df.groupby("date")["outcome"]
    out = grp.apply(lambda s: (s == "DOWN").mean()).reset_index(name="base_down")
    out["n_markets"] = grp.size().values
    return out


def add_regimes(daily: pd.DataFrame) -> pd.DataFrame:
    out = daily.copy()

    # Fixed regime thresholds (user-intuitive 45/55 split)
    out["fixed_regime"] = "neutral"
    out.loc[out["base_down"] <= 0.45, "fixed_regime"] = "up_dominant"
    out.loc[out["base_down"] >= 0.55, "fixed_regime"] = "down_dominant"

    # Quantile regimes (balanced sample sizes)
    q1, q2 = out["base_down"].quantile([1 / 3, 2 / 3]).tolist()
    out["q_regime"] = "mid"
    out.loc[out["base_down"] <= q1, "q_regime"] = "low_down"
    out.loc[out["base_down"] >= q2, "q_regime"] = "high_down"

    return out


def eval_regime(
    df: pd.DataFrame,
    events: pd.DataFrame,
    daily: pd.DataFrame,
    spec: PatternSpec,
    side: str,
    regime_col: str,
    regime_label: str,
) -> Optional[dict]:
    days = set(daily[daily[regime_col] == regime_label]["date"].tolist())
    if not days:
        return None

    if events.empty:
        return None

    ev = events[events["date"].isin(days)]
    trades = int(len(ev))
    if trades == 0:
        return None
    wins = int(ev["win"].sum())
    wr = wins / trades

    scope_df = df if spec.scope == "all" else df[df["coin"] == spec.coin]
    scope_df = scope_df[scope_df["date"].isin(days)]
    if scope_df.empty:
        return None

    base_down = float((scope_df["outcome"] == "DOWN").mean())
    base = base_down if side == "DOWN" else 1.0 - base_down
    delta = wr - base
    pval = stats.binomtest(wins, trades, base, alternative="greater").pvalue

    payout = 1.0 * (1.0 - FEE_RATE)
    max_ask = wr * payout - SLIPPAGE
    ev_at_50 = wr * (payout - 0.50 - SLIPPAGE) + (1.0 - wr) * (-0.50 - SLIPPAGE)

    return {
        "regime": regime_label,
        "days": int(len(days)),
        "trades": trades,
        "wins": wins,
        "wr": wr,
        "base": base,
        "delta": delta,
        "p": float(pval),
        "max_ask": max_ask,
        "ev_at_50": ev_at_50,
    }


def print_section(title: str, rows: List[dict], min_trades: int) -> None:
    print("\n" + "=" * 112)
    print(f"  {title}")
    print("=" * 112)
    print(
        "  "
        f"{'Pattern':<18} {'Regime':<14} {'Days':>5} {'N':>6} {'WR':>7} {'Base':>7} "
        f"{'Delta':>7} {'p-val':>10} {'EV@.50':>8} {'MaxAsk':>7}"
    )
    print(
        "  "
        f"{'-'*18} {'-'*14} {'-'*5} {'-'*6} {'-'*7} {'-'*7} {'-'*7} {'-'*10} {'-'*8} {'-'*7}"
    )

    shown = 0
    for r in rows:
        if r["trades"] < min_trades:
            continue
        shown += 1
        print(
            "  "
            f"{r['pattern']:<18} {r['regime']:<14} {r['days']:>5} {r['trades']:>6,} "
            f"{r['wr']:>6.1%} {r['base']:>6.1%} {r['delta']:>+6.1%} {r['p']:>10.2e} "
            f"${r['ev_at_50']:>+7.4f} ${r['max_ask']:>.3f}"
        )

    if shown == 0:
        print(f"  No rows with N >= {min_trades}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Base-rate regime robustness test")
    parser.add_argument("--from-date", type=str, default=None)
    parser.add_argument("--to-date", type=str, default=None)
    parser.add_argument("--min-trades", type=int, default=120)
    parser.add_argument("--min-day-markets-all", type=int, default=900)
    parser.add_argument("--min-day-markets-single", type=int, default=220)
    args = parser.parse_args()

    df = load_data(args.from_date, args.to_date)
    if df.empty:
        print("No data after filters.")
        return

    print("Loaded data")
    print(f"  Rows: {len(df):,}")
    print(f"  Range: {df['datetime'].min()} -> {df['datetime'].max()}")

    seq = build_sequences(df)

    fixed_rows: List[dict] = []
    quant_rows: List[dict] = []

    for spec in PATTERNS:
        events = extract_events(seq, spec)
        daily_raw = daily_base_down(df, spec)
        min_day_markets = (
            args.min_day_markets_all if spec.scope == "all" else args.min_day_markets_single
        )
        daily_raw = daily_raw[daily_raw["n_markets"] >= min_day_markets].copy()
        if daily_raw.empty:
            continue
        daily = add_regimes(daily_raw)

        for regime in ["up_dominant", "neutral", "down_dominant"]:
            res = eval_regime(df, events, daily, spec, spec.side, "fixed_regime", regime)
            if res is None:
                continue
            res["pattern"] = spec.name
            fixed_rows.append(res)

        for regime in ["low_down", "mid", "high_down"]:
            res = eval_regime(df, events, daily, spec, spec.side, "q_regime", regime)
            if res is None:
                continue
            res["pattern"] = spec.name
            quant_rows.append(res)

    print_section("FIXED REGIMES (base DOWN <=45%, 45-55%, >=55%)", fixed_rows, args.min_trades)
    print_section("QUANTILE REGIMES (balanced by day count)", quant_rows, args.min_trades)

    print("\nDone.")


if __name__ == "__main__":
    main()
