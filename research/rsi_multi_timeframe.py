"""
rsi_multi_timeframe.py - Comprehensive RSI multi-timeframe analysis.

Tests whether 5m RSI(14) is truly the best RSI configuration for filtering
mean-reversion trades, or if other timeframes / periods perform better.

Analysis:
  1. Download Binance BTCUSDT klines for 8 timeframes: 1m, 3m, 5m, 15m, 30m, 1h, 4h, 1d
  2. For each timeframe, compute RSI(14) and match to Telonex cycle timestamps
  3. Test NEGATIVE filters (skip when RSI > threshold): 50, 55, 60, 65, 70
  4. Test POSITIVE filters (only trade when RSI < threshold): 30, 40, 50
  5. Find optimal combination: which timeframe + threshold maximizes daily PnL
  6. Test RSI period variations: RSI(7), RSI(14), RSI(21), RSI(28) on best timeframe
  7. Output summary tables

Uses Telonex 59,423 settled markets (Dec 18 2025 - Mar 1 2026).
Applies all 6 YAML rules with priority ordering, 0% maker fee, $5 size.

Usage:
    python research/rsi_multi_timeframe.py
"""

import json
import re
import sys
import time as time_mod
import urllib.request
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).parent.parent / "data"
TELONEX_FILE = DATA_DIR / "telonex_updown_5m.parquet"
CACHE_DIR = Path(__file__).parent / "binance_kline_cache"
BINANCE_URL = "https://api.binance.com/api/v3/klines"

COINS = ["BTC", "ETH", "SOL", "XRP"]
SIZE = 5.0
FEE_RATE = 0.0  # 0% maker fee on Builder

# All 6 YAML rules (same as live + backtest_realdata.py)
RULES = [
    {"pattern": "UUUUU", "side": "DOWN", "coins": ["ETH"],              "max_ask": 0.54},
    {"pattern": "UUUUU", "side": "DOWN", "coins": ["BTC","SOL","XRP"],  "max_ask": 0.51},
    {"pattern": "UUUU",  "side": "DOWN", "coins": ["ETH"],              "max_ask": 0.54},
    {"pattern": "UUUU",  "side": "DOWN", "coins": ["BTC","SOL","XRP"],  "max_ask": 0.51},
    {"pattern": "UDUU",  "side": "DOWN", "coins": ["BTC"],              "max_ask": 0.51},
    {"pattern": "UUU",   "side": "DOWN", "coins": ["BTC","ETH","SOL","XRP"], "max_ask": 0.51},
]

# Timeframes to test
TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"]

# RSI periods to test on best timeframe
RSI_PERIODS = [7, 14, 21, 28]

# Filter thresholds
NEGATIVE_THRESHOLDS = [50, 55, 60, 65, 70]   # skip when RSI > threshold
POSITIVE_THRESHOLDS = [30, 40, 50]            # only trade when RSI < threshold

# Interval durations in seconds (for matching timestamps to candles)
INTERVAL_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


# ---------------------------------------------------------------------------
# 1. Load Telonex data
# ---------------------------------------------------------------------------
def load_telonex() -> Tuple[pd.DataFrame, dict]:
    """Load and prepare the Telonex dataset. Returns (df, coin_seqs)."""
    print(f"Loading {TELONEX_FILE}...")
    df = pd.read_parquet(TELONEX_FILE)
    df = df[df["result_id"].isin(["0", "1"])].copy()
    print(f"  Settled markets: {len(df):,}")

    # Parse coin and timestamp from slug
    def parse_slug(slug):
        m = re.match(r"(\w+)-updown-5m-(\d+)", slug)
        if m:
            return m.group(1).upper(), int(m.group(2))
        return None, None

    df["coin"], df["start_ts"] = zip(*df["slug"].apply(parse_slug))
    df = df.dropna(subset=["coin"])
    df["outcome"] = df["result_id"].map({"0": "U", "1": "D"})
    df["date"] = pd.to_datetime(df["start_ts"], unit="s", utc=True).dt.date
    df = df.sort_values(["coin", "start_ts"]).reset_index(drop=True)

    ts_min = df["start_ts"].min()
    ts_max = df["start_ts"].max()
    dt_min = datetime.fromtimestamp(ts_min, tz=timezone.utc)
    dt_max = datetime.fromtimestamp(ts_max, tz=timezone.utc)
    n_days = (df["date"].max() - df["date"].min()).days + 1
    print(f"  Date range: {dt_min:%Y-%m-%d} to {dt_max:%Y-%m-%d} ({n_days} days)")

    for coin in COINS:
        cnt = len(df[df["coin"] == coin])
        print(f"    {coin}: {cnt:,} markets")

    # Build per-coin outcome sequences
    coin_seqs = {}
    for coin in COINS:
        cdf = df[df["coin"] == coin].sort_values("start_ts").reset_index(drop=True)
        coin_seqs[coin] = {
            "outcomes": cdf["outcome"].values,
            "dates": cdf["date"].values,
            "unix_ts": cdf["start_ts"].values.astype(int),
            "n": len(cdf),
        }

    return df, coin_seqs


# ---------------------------------------------------------------------------
# 2. Build rule-based trades (all 6 YAML rules with priority)
# ---------------------------------------------------------------------------
def find_yaml_rule_trades(coin_seqs: dict) -> pd.DataFrame:
    """Simulate the live YAML rules with priority ordering."""
    all_trades = []
    for coin, data in coin_seqs.items():
        outcomes = data["outcomes"]
        dates = data["dates"]
        unix_ts = data["unix_ts"]
        n = data["n"]

        for i in range(1, n):
            for rule in RULES:
                if coin not in rule["coins"]:
                    continue
                pat = list(rule["pattern"])
                plen = len(pat)
                if i < plen:
                    continue
                # Check pattern match
                match = True
                for j in range(plen):
                    if outcomes[i - plen + j] != pat[j]:
                        match = False
                        break
                if not match:
                    continue
                # Rule matches
                side = rule["side"]
                max_ask = rule["max_ask"]
                actual = outcomes[i]
                hit = 1 if (side == "DOWN" and actual == "D") else 0

                # PnL with 0% fee
                if hit:
                    pnl = (1.0 - max_ask) * SIZE
                else:
                    pnl = -max_ask * SIZE

                all_trades.append({
                    "coin": coin,
                    "idx": i,
                    "outcome": actual,
                    "date": dates[i],
                    "pattern": rule["pattern"],
                    "side": side,
                    "max_ask": max_ask,
                    "hit": hit,
                    "pnl": pnl,
                    "unix_ts": int(unix_ts[i]),
                })
                break  # first matching rule wins

    trades_df = pd.DataFrame(all_trades)
    trades_df = trades_df.sort_values("unix_ts").reset_index(drop=True)
    return trades_df


# ---------------------------------------------------------------------------
# 3. Download Binance klines with per-timeframe caching
# ---------------------------------------------------------------------------
def download_binance_klines(
    symbol: str,
    interval: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """Download Binance klines via REST API with local JSON cache."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"{symbol}_{interval}_{start_date}_{end_date}.json"

    if cache_file.exists():
        with open(cache_file) as f:
            all_klines = json.load(f)
        print(f"    {interval}: cache hit, {len(all_klines):,} klines")
    else:
        start_ts = int(pd.Timestamp(start_date, tz="UTC").timestamp() * 1000)
        end_ts = int(pd.Timestamp(end_date, tz="UTC").timestamp() * 1000)

        all_klines = []
        current_start = start_ts

        while current_start < end_ts:
            url = (
                f"{BINANCE_URL}?symbol={symbol}&interval={interval}"
                f"&startTime={current_start}&endTime={end_ts}&limit=1000"
            )
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
            except Exception as e:
                print(f"    WARNING: Binance API error ({interval}): {e}")
                break

            if not data:
                break

            for k in data:
                all_klines.append({
                    "open_time_ms": k[0],
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "close_time_ms": k[6],
                })

            last_close = data[-1][6]
            if last_close <= current_start:
                break
            current_start = last_close + 1

            time_mod.sleep(0.12)  # rate limit

        # Save cache
        with open(cache_file, "w") as f:
            json.dump(all_klines, f)
        print(f"    {interval}: downloaded {len(all_klines):,} klines")

    if not all_klines:
        return pd.DataFrame()

    df = pd.DataFrame(all_klines)
    df["open_time_s"] = (df["open_time_ms"] / 1000).astype(int)
    df["close"] = df["close"].astype(float)
    df = df.sort_values("open_time_ms").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# 4. Compute RSI with configurable period
# ---------------------------------------------------------------------------
def compute_rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Compute RSI using Wilder's smoothed average gain/loss."""
    n = len(closes)
    delta = np.diff(closes, prepend=closes[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)

    rsi = np.full(n, np.nan)
    if n <= period:
        return rsi

    avg_gain = np.full(n, np.nan)
    avg_loss = np.full(n, np.nan)

    avg_gain[period] = np.mean(gain[1:period + 1])
    avg_loss[period] = np.mean(loss[1:period + 1])

    for i in range(period + 1, n):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i]) / period

    for i in range(period, n):
        if avg_loss[i] == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gain[i] / avg_loss[i]
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))

    return rsi


# ---------------------------------------------------------------------------
# 5. Match trade timestamps to RSI values for a given timeframe
# ---------------------------------------------------------------------------
def match_trades_to_rsi(
    trades_df: pd.DataFrame,
    klines_df: pd.DataFrame,
    interval: str,
    rsi_values: np.ndarray,
) -> np.ndarray:
    """For each trade, find the RSI from the kline that was ACTIVE at trade time.

    For timeframe X, the candle active at timestamp T is the one whose
    open_time <= T < open_time + interval_duration.
    We use binary search for efficiency.
    """
    trade_ts = trades_df["unix_ts"].values.astype(int)
    kline_open_ts = klines_df["open_time_s"].values.astype(int)
    interval_s = INTERVAL_SECONDS[interval]
    n_trades = len(trade_ts)
    matched_rsi = np.full(n_trades, np.nan)

    for i in range(n_trades):
        ts = trade_ts[i]
        # Binary search: find largest kline_open_ts <= ts
        idx = np.searchsorted(kline_open_ts, ts, side="right") - 1
        if idx < 0:
            continue
        # Verify the candle contains this timestamp
        candle_start = kline_open_ts[idx]
        if ts >= candle_start and ts < candle_start + interval_s:
            matched_rsi[i] = rsi_values[idx]
        elif idx + 1 < len(kline_open_ts):
            # Try next candle (edge case for daily candles starting at midnight)
            candle_start_next = kline_open_ts[idx + 1]
            if ts >= candle_start_next and ts < candle_start_next + interval_s:
                matched_rsi[i] = rsi_values[idx + 1]
            else:
                # Use the closest candle anyway (for daily the open might be offset)
                matched_rsi[i] = rsi_values[idx]
        else:
            # Use the closest candle
            matched_rsi[i] = rsi_values[idx]

    return matched_rsi


# ---------------------------------------------------------------------------
# 6. Evaluate a filter on trades
# ---------------------------------------------------------------------------
def evaluate_filter(
    trades_df: pd.DataFrame,
    rsi_col: str,
    filter_type: str,  # "negative" or "positive"
    threshold: float,
    n_days: int,
) -> dict:
    """Evaluate a filter and return metrics.

    negative: skip trades where RSI > threshold (keep RSI <= threshold)
    positive: only trade when RSI < threshold (keep RSI < threshold)
    """
    valid = trades_df.dropna(subset=[rsi_col])
    n_valid = len(valid)
    if n_valid == 0:
        return None

    if filter_type == "negative":
        kept = valid[valid[rsi_col] <= threshold]
        label = f"skip RSI>{threshold}"
    else:  # positive
        kept = valid[valid[rsi_col] < threshold]
        label = f"only RSI<{threshold}"

    n_kept = len(kept)
    n_removed = n_valid - n_kept
    if n_kept == 0:
        return None

    wins = int(kept["hit"].sum())
    wr = wins / n_kept
    total_pnl = kept["pnl"].sum()
    daily_pnl = total_pnl / n_days
    avg_pnl = total_pnl / n_kept

    # Also compute WR and PnL of removed trades
    removed = valid[~valid.index.isin(kept.index)]
    removed_wins = int(removed["hit"].sum()) if len(removed) > 0 else 0
    removed_wr = removed_wins / len(removed) if len(removed) > 0 else 0
    removed_pnl = removed["pnl"].sum() if len(removed) > 0 else 0

    return {
        "filter_type": filter_type,
        "threshold": threshold,
        "label": label,
        "n_valid": n_valid,
        "n_kept": n_kept,
        "n_removed": n_removed,
        "pct_removed": n_removed / n_valid * 100,
        "wins": wins,
        "wr": wr,
        "total_pnl": total_pnl,
        "daily_pnl": daily_pnl,
        "avg_pnl": avg_pnl,
        "removed_wr": removed_wr,
        "removed_pnl": removed_pnl,
    }


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    print("=" * 90)
    print("  RSI MULTI-TIMEFRAME ANALYSIS")
    print("  Testing 8 timeframes x multiple thresholds x 4 RSI periods")
    print("  Telonex 59,423 settled markets | 6 YAML rules | 0% fee | $5 size")
    print("=" * 90)

    # ---- Step 1: Load Telonex data ----
    tdf, coin_seqs = load_telonex()

    # ---- Step 2: Build trades from YAML rules ----
    print(f"\nBuilding trades from 6 YAML rules...")
    trades_df = find_yaml_rule_trades(coin_seqs)
    n_trades = len(trades_df)
    n_wins = int(trades_df["hit"].sum())
    n_days = (trades_df["date"].max() - trades_df["date"].min()).days + 1
    baseline_wr = n_wins / n_trades
    baseline_pnl = trades_df["pnl"].sum()
    baseline_daily = baseline_pnl / n_days

    print(f"  Total trades: {n_trades:,}")
    print(f"  Wins: {n_wins:,} / Losses: {n_trades - n_wins:,}")
    print(f"  Baseline WR: {baseline_wr:.4f} ({baseline_wr:.1%})")
    print(f"  Baseline PnL: ${baseline_pnl:.2f}")
    print(f"  Baseline daily PnL: ${baseline_daily:.2f}/day")
    print(f"  Days: {n_days}")

    # ---- Step 3: Download Binance klines for all timeframes ----
    print(f"\nDownloading Binance BTCUSDT klines for {len(TIMEFRAMES)} timeframes...")
    # Need warmup: start earlier for higher timeframes
    # For 1d RSI(28), need 28 daily candles = 28 days before Dec 18
    start_date = "2025-11-15"  # plenty of warmup for all timeframes
    end_date = "2026-03-02"

    klines_by_tf = {}
    for tf in TIMEFRAMES:
        kdf = download_binance_klines("BTCUSDT", tf, start_date, end_date)
        if len(kdf) > 0:
            klines_by_tf[tf] = kdf
        else:
            print(f"    WARNING: No data for {tf}")

    # ---- Step 4: Compute RSI(14) for each timeframe and match to trades ----
    print(f"\nComputing RSI(14) and matching to {n_trades:,} trades...")

    rsi_by_tf = {}  # tf -> column name in trades_df
    for tf in TIMEFRAMES:
        if tf not in klines_by_tf:
            continue
        kdf = klines_by_tf[tf]
        rsi_vals = compute_rsi(kdf["close"].values, period=14)

        col_name = f"rsi14_{tf}"
        trades_df[col_name] = match_trades_to_rsi(trades_df, kdf, tf, rsi_vals)
        n_matched = trades_df[col_name].notna().sum()
        print(f"    {tf}: {n_matched:,} / {n_trades:,} matched ({n_matched/n_trades:.1%})")
        rsi_by_tf[tf] = col_name

    # ====================================================================
    # SECTION 1: NEGATIVE FILTER (skip when RSI > threshold)
    # ====================================================================
    print("\n\n" + "=" * 90)
    print("  SECTION 1: NEGATIVE FILTER — Skip trades when RSI(14) > threshold")
    print("  (Removes trades during overbought/bullish conditions)")
    print("=" * 90)

    neg_results = []

    print(f"\n  {'Timeframe':<10} {'Threshold':>9} {'Trades':>7} {'Removed':>8} {'%Rem':>6} "
          f"{'WR':>7} {'TotalPnL':>10} {'DailyPnL':>9} {'RemovedWR':>10}")
    print(f"  {'-'*10} {'-'*9} {'-'*7} {'-'*8} {'-'*6} "
          f"{'-'*7} {'-'*10} {'-'*9} {'-'*10}")

    # Baseline row
    print(f"  {'BASELINE':<10} {'--':>9} {n_trades:>7} {'--':>8} {'--':>6} "
          f"{baseline_wr:>6.1%} ${baseline_pnl:>+9.2f} ${baseline_daily:>+8.2f} {'--':>10}")
    print()

    for tf in TIMEFRAMES:
        if tf not in rsi_by_tf:
            continue
        col = rsi_by_tf[tf]
        for thresh in NEGATIVE_THRESHOLDS:
            r = evaluate_filter(trades_df, col, "negative", thresh, n_days)
            if r is None:
                continue
            r["timeframe"] = tf
            r["rsi_period"] = 14
            neg_results.append(r)

            marker = " ***" if r["daily_pnl"] > baseline_daily * 1.1 else ""
            print(f"  {tf:<10} {thresh:>9} {r['n_kept']:>7} {r['n_removed']:>8} "
                  f"{r['pct_removed']:>5.1f}% {r['wr']:>6.1%} "
                  f"${r['total_pnl']:>+9.2f} ${r['daily_pnl']:>+8.2f} "
                  f"{r['removed_wr']:>9.1%}{marker}")
        print()

    # ====================================================================
    # SECTION 2: POSITIVE FILTER (only trade when RSI < threshold)
    # ====================================================================
    print("\n" + "=" * 90)
    print("  SECTION 2: POSITIVE FILTER — Only trade when RSI(14) < threshold")
    print("  (Only trades during oversold/bearish conditions)")
    print("=" * 90)

    pos_results = []

    print(f"\n  {'Timeframe':<10} {'Threshold':>9} {'Trades':>7} {'Removed':>8} {'%Rem':>6} "
          f"{'WR':>7} {'TotalPnL':>10} {'DailyPnL':>9} {'RemovedWR':>10}")
    print(f"  {'-'*10} {'-'*9} {'-'*7} {'-'*8} {'-'*6} "
          f"{'-'*7} {'-'*10} {'-'*9} {'-'*10}")

    print(f"  {'BASELINE':<10} {'--':>9} {n_trades:>7} {'--':>8} {'--':>6} "
          f"{baseline_wr:>6.1%} ${baseline_pnl:>+9.2f} ${baseline_daily:>+8.2f} {'--':>10}")
    print()

    for tf in TIMEFRAMES:
        if tf not in rsi_by_tf:
            continue
        col = rsi_by_tf[tf]
        for thresh in POSITIVE_THRESHOLDS:
            r = evaluate_filter(trades_df, col, "positive", thresh, n_days)
            if r is None:
                continue
            r["timeframe"] = tf
            r["rsi_period"] = 14
            pos_results.append(r)

            marker = " ***" if r["daily_pnl"] > baseline_daily * 1.1 else ""
            print(f"  {tf:<10} {thresh:>9} {r['n_kept']:>7} {r['n_removed']:>8} "
                  f"{r['pct_removed']:>5.1f}% {r['wr']:>6.1%} "
                  f"${r['total_pnl']:>+9.2f} ${r['daily_pnl']:>+8.2f} "
                  f"{r['removed_wr']:>9.1%}{marker}")
        print()

    # ====================================================================
    # SECTION 3: RANKING — Best configurations by daily PnL
    # ====================================================================
    print("\n" + "=" * 90)
    print("  SECTION 3: RANKING — All configs sorted by daily PnL")
    print("=" * 90)

    all_results = neg_results + pos_results
    all_results.sort(key=lambda x: x["daily_pnl"], reverse=True)

    print(f"\n  {'Rank':>4} {'TF':<6} {'Filter':<18} {'Trades':>7} {'%Rem':>6} "
          f"{'WR':>7} {'TotalPnL':>10} {'DailyPnL':>9} {'RemWR':>7}")
    print(f"  {'-'*4} {'-'*6} {'-'*18} {'-'*7} {'-'*6} "
          f"{'-'*7} {'-'*10} {'-'*9} {'-'*7}")

    # Add baseline as rank 0
    print(f"  {'--':>4} {'--':<6} {'BASELINE (no RSI)':<18} {n_trades:>7} {'--':>6} "
          f"{baseline_wr:>6.1%} ${baseline_pnl:>+9.2f} ${baseline_daily:>+8.2f} {'--':>7}")

    for rank, r in enumerate(all_results[:25], 1):
        print(f"  {rank:>4} {r['timeframe']:<6} {r['label']:<18} {r['n_kept']:>7} "
              f"{r['pct_removed']:>5.1f}% {r['wr']:>6.1%} "
              f"${r['total_pnl']:>+9.2f} ${r['daily_pnl']:>+8.2f} "
              f"{r['removed_wr']:>6.1%}")

    # Identify best from Section 1-2
    if all_results:
        best_overall = all_results[0]
        best_tf = best_overall["timeframe"]
        print(f"\n  >>> BEST CONFIG (RSI period=14): {best_tf} | {best_overall['label']} <<<")
        print(f"      Trades: {best_overall['n_kept']:,}, WR: {best_overall['wr']:.1%}, "
              f"Daily PnL: ${best_overall['daily_pnl']:.2f}/day")
    else:
        best_tf = "5m"  # default

    # ====================================================================
    # SECTION 4: RSI PERIOD SWEEP on best timeframe
    # ====================================================================
    print("\n\n" + "=" * 90)
    print(f"  SECTION 4: RSI PERIOD SWEEP on best timeframe ({best_tf})")
    print(f"  Testing RSI(7), RSI(14), RSI(21), RSI(28)")
    print("=" * 90)

    if best_tf not in klines_by_tf:
        print(f"  ERROR: No kline data for {best_tf}")
    else:
        kdf = klines_by_tf[best_tf]

        period_results = []

        for period in RSI_PERIODS:
            rsi_vals = compute_rsi(kdf["close"].values, period=period)
            col = f"rsi{period}_{best_tf}"
            trades_df[col] = match_trades_to_rsi(trades_df, kdf, best_tf, rsi_vals)
            n_matched = trades_df[col].notna().sum()
            print(f"  RSI({period}) on {best_tf}: {n_matched:,} matched")

            # Test all filter types
            for thresh in NEGATIVE_THRESHOLDS:
                r = evaluate_filter(trades_df, col, "negative", thresh, n_days)
                if r:
                    r["timeframe"] = best_tf
                    r["rsi_period"] = period
                    period_results.append(r)

            for thresh in POSITIVE_THRESHOLDS:
                r = evaluate_filter(trades_df, col, "positive", thresh, n_days)
                if r:
                    r["timeframe"] = best_tf
                    r["rsi_period"] = period
                    period_results.append(r)

        period_results.sort(key=lambda x: x["daily_pnl"], reverse=True)

        print(f"\n  {'Rank':>4} {'Period':>6} {'Filter':<18} {'Trades':>7} {'%Rem':>6} "
              f"{'WR':>7} {'TotalPnL':>10} {'DailyPnL':>9} {'RemWR':>7}")
        print(f"  {'-'*4} {'-'*6} {'-'*18} {'-'*7} {'-'*6} "
              f"{'-'*7} {'-'*10} {'-'*9} {'-'*7}")

        print(f"  {'--':>4} {'--':>6} {'BASELINE':<18} {n_trades:>7} {'--':>6} "
              f"{baseline_wr:>6.1%} ${baseline_pnl:>+9.2f} ${baseline_daily:>+8.2f} {'--':>7}")

        for rank, r in enumerate(period_results[:20], 1):
            print(f"  {rank:>4} {r['rsi_period']:>6} {r['label']:<18} {r['n_kept']:>7} "
                  f"{r['pct_removed']:>5.1f}% {r['wr']:>6.1%} "
                  f"${r['total_pnl']:>+9.2f} ${r['daily_pnl']:>+8.2f} "
                  f"{r['removed_wr']:>6.1%}")

    # ====================================================================
    # SECTION 5: ALSO TEST ALL TFs with other RSI periods
    # ====================================================================
    print("\n\n" + "=" * 90)
    print("  SECTION 5: FULL GRID — All timeframes x RSI periods x thresholds")
    print("  (Only negative filters for brevity — skip when RSI > threshold)")
    print("=" * 90)

    full_grid = []

    for tf in TIMEFRAMES:
        if tf not in klines_by_tf:
            continue
        kdf = klines_by_tf[tf]

        for period in RSI_PERIODS:
            rsi_vals = compute_rsi(kdf["close"].values, period=period)
            col = f"rsi{period}_{tf}_full"
            trades_df[col] = match_trades_to_rsi(trades_df, kdf, tf, rsi_vals)

            for thresh in NEGATIVE_THRESHOLDS:
                r = evaluate_filter(trades_df, col, "negative", thresh, n_days)
                if r:
                    r["timeframe"] = tf
                    r["rsi_period"] = period
                    full_grid.append(r)

    full_grid.sort(key=lambda x: x["daily_pnl"], reverse=True)

    print(f"\n  TOP 30 CONFIGURATIONS (negative filter, sorted by daily PnL):")
    print(f"\n  {'Rank':>4} {'TF':<6} {'RSI':>5} {'Thresh':>6} {'Trades':>7} {'%Rem':>6} "
          f"{'WR':>7} {'TotalPnL':>10} {'DailyPnL':>9} {'RemWR':>7}")
    print(f"  {'-'*4} {'-'*6} {'-'*5} {'-'*6} {'-'*7} {'-'*6} "
          f"{'-'*7} {'-'*10} {'-'*9} {'-'*7}")

    print(f"  {'--':>4} {'--':<6} {'--':>5} {'--':>6} {n_trades:>7} {'--':>6} "
          f"{baseline_wr:>6.1%} ${baseline_pnl:>+9.2f} ${baseline_daily:>+8.2f} {'--':>7}")

    for rank, r in enumerate(full_grid[:30], 1):
        print(f"  {rank:>4} {r['timeframe']:<6} {r['rsi_period']:>5} {r['threshold']:>6} "
              f"{r['n_kept']:>7} {r['pct_removed']:>5.1f}% {r['wr']:>6.1%} "
              f"${r['total_pnl']:>+9.2f} ${r['daily_pnl']:>+8.2f} "
              f"{r['removed_wr']:>6.1%}")

    # ====================================================================
    # SECTION 6: SUMMARY TABLE — Timeframe x Threshold heatmap (RSI 14)
    # ====================================================================
    print("\n\n" + "=" * 90)
    print("  SECTION 6: HEATMAP — Daily PnL by Timeframe x Threshold (RSI 14, negative filter)")
    print("=" * 90)

    # Build lookup
    neg_lookup = {}
    for r in neg_results:
        neg_lookup[(r["timeframe"], r["threshold"])] = r

    print(f"\n  {'TF':<8}", end="")
    for thresh in NEGATIVE_THRESHOLDS:
        print(f"  {'RSI>' + str(thresh):>10}", end="")
    print(f"  {'Best':>10}")
    print(f"  {'-'*8}", end="")
    for _ in NEGATIVE_THRESHOLDS:
        print(f"  {'-'*10}", end="")
    print(f"  {'-'*10}")

    for tf in TIMEFRAMES:
        print(f"  {tf:<8}", end="")
        best_daily_for_tf = -999
        for thresh in NEGATIVE_THRESHOLDS:
            key = (tf, thresh)
            if key in neg_lookup:
                r = neg_lookup[key]
                val = r["daily_pnl"]
                best_daily_for_tf = max(best_daily_for_tf, val)
                marker = "*" if val > baseline_daily * 1.05 else " "
                print(f"  ${val:>+8.2f}{marker}", end="")
            else:
                print(f"  {'--':>10}", end="")
        if best_daily_for_tf > -999:
            print(f"  ${best_daily_for_tf:>+8.2f}")
        else:
            print(f"  {'--':>10}")

    print(f"\n  Baseline daily PnL: ${baseline_daily:.2f}/day")

    # ====================================================================
    # SECTION 7: WR HEATMAP
    # ====================================================================
    print(f"\n  WR HEATMAP — by Timeframe x Threshold (RSI 14, negative filter)")
    print(f"  {'TF':<8}", end="")
    for thresh in NEGATIVE_THRESHOLDS:
        print(f"  {'RSI>' + str(thresh):>10}", end="")
    print()
    print(f"  {'-'*8}", end="")
    for _ in NEGATIVE_THRESHOLDS:
        print(f"  {'-'*10}", end="")
    print()

    for tf in TIMEFRAMES:
        print(f"  {tf:<8}", end="")
        for thresh in NEGATIVE_THRESHOLDS:
            key = (tf, thresh)
            if key in neg_lookup:
                r = neg_lookup[key]
                print(f"  {r['wr']:>9.1%}", end="")
            else:
                print(f"  {'--':>10}", end="")
        print()

    print(f"\n  Baseline WR: {baseline_wr:.1%}")

    # ====================================================================
    # SECTION 8: MONOTONIC RSI RELATIONSHIP (WR by RSI decile, each TF)
    # ====================================================================
    print("\n\n" + "=" * 90)
    print("  SECTION 8: RSI DECILE ANALYSIS — WR by RSI(14) range for each timeframe")
    print("  (Verifying the monotonic relationship holds across timeframes)")
    print("=" * 90)

    rsi_bins = [(0, 20), (20, 30), (30, 40), (40, 50), (50, 60),
                (60, 70), (70, 80), (80, 100)]

    for tf in TIMEFRAMES:
        if tf not in rsi_by_tf:
            continue
        col = rsi_by_tf[tf]
        valid = trades_df.dropna(subset=[col])
        if len(valid) < 100:
            continue

        print(f"\n  {tf} RSI(14) — {len(valid):,} trades with valid RSI:")
        print(f"    {'RSI Range':<12} {'Trades':>7} {'Wins':>6} {'WR':>7} {'PnL':>10} {'AvgPnL':>8}")
        print(f"    {'-'*12} {'-'*7} {'-'*6} {'-'*7} {'-'*10} {'-'*8}")

        for lo, hi in rsi_bins:
            sub = valid[(valid[col] >= lo) & (valid[col] < hi)]
            if len(sub) == 0:
                continue
            n = len(sub)
            w = int(sub["hit"].sum())
            wr = w / n
            pnl = sub["pnl"].sum()
            avg = pnl / n
            bar = "#" * int(wr * 40)
            print(f"    [{lo:>2}-{hi:>3}) {n:>7} {w:>6} {wr:>6.1%} ${pnl:>+9.2f} ${avg:>+7.4f}  {bar}")

    # ====================================================================
    # SECTION 9: FINAL VERDICT
    # ====================================================================
    print("\n\n" + "=" * 90)
    print("  FINAL VERDICT")
    print("=" * 90)

    # Find overall best from full grid
    if full_grid:
        best = full_grid[0]
        print(f"\n  BEST NEGATIVE FILTER (from full grid):")
        print(f"    Timeframe:    {best['timeframe']}")
        print(f"    RSI Period:   {best['rsi_period']}")
        print(f"    Threshold:    skip when RSI > {best['threshold']}")
        print(f"    Trades kept:  {best['n_kept']:,} / {best['n_valid']:,} "
              f"({best['pct_removed']:.1f}% removed)")
        print(f"    Win Rate:     {best['wr']:.1%} (baseline: {baseline_wr:.1%}, "
              f"delta: {best['wr'] - baseline_wr:+.1%})")
        print(f"    Total PnL:    ${best['total_pnl']:.2f}")
        print(f"    Daily PnL:    ${best['daily_pnl']:.2f}/day "
              f"(baseline: ${baseline_daily:.2f}/day)")
        pnl_improvement = best['daily_pnl'] - baseline_daily
        pct_improvement = (best['daily_pnl'] / baseline_daily - 1) * 100 if baseline_daily > 0 else 0
        print(f"    Improvement:  ${pnl_improvement:+.2f}/day ({pct_improvement:+.1f}%)")
        print(f"    Removed WR:   {best['removed_wr']:.1%}")

    # Find best positive filter
    pos_all = [r for r in (neg_results + pos_results) if r["filter_type"] == "positive"]
    if pos_all:
        pos_all.sort(key=lambda x: x["daily_pnl"], reverse=True)
        best_pos = pos_all[0]
        print(f"\n  BEST POSITIVE FILTER:")
        print(f"    Timeframe:    {best_pos['timeframe']}")
        print(f"    RSI Period:   14")
        print(f"    Threshold:    only trade when RSI < {best_pos['threshold']}")
        print(f"    Trades kept:  {best_pos['n_kept']:,} (from {best_pos['n_valid']:,})")
        print(f"    Win Rate:     {best_pos['wr']:.1%}")
        print(f"    Daily PnL:    ${best_pos['daily_pnl']:.2f}/day")

    # Compare 5m RSI(14) > 60 specifically
    print(f"\n  COMPARISON — Is 5m RSI(14) > 60 the best?")
    ref_key = ("5m", 60)
    if ref_key in neg_lookup:
        ref = neg_lookup[ref_key]
        print(f"    5m RSI(14) skip>60: {ref['n_kept']:,} trades, "
              f"WR={ref['wr']:.1%}, daily=${ref['daily_pnl']:.2f}/day")
    if full_grid:
        print(f"    Best overall:       {best['timeframe']} RSI({best['rsi_period']}) "
              f"skip>{best['threshold']}: {best['n_kept']:,} trades, "
              f"WR={best['wr']:.1%}, daily=${best['daily_pnl']:.2f}/day")
        if best["timeframe"] == "5m" and best["rsi_period"] == 14 and best["threshold"] == 60:
            print(f"\n    ANSWER: YES, 5m RSI(14) > 60 IS the best filter!")
        else:
            print(f"\n    ANSWER: NO, {best['timeframe']} RSI({best['rsi_period']}) > "
                  f"{best['threshold']} is BETTER than 5m RSI(14) > 60!")

    # ====================================================================
    # SECTION 10: PRACTICAL IMPLEMENTATION NOTES
    # ====================================================================
    print(f"\n\n  {'='*70}")
    print(f"  IMPLEMENTATION NOTES")
    print(f"  {'='*70}")
    print(f"""
  1. LATENCY: Higher timeframes (1h, 4h, 1d) are more stable — RSI changes
     less frequently, so the filter can be checked less often.

  2. ROBUSTNESS: If multiple timeframes give similar results, prefer the
     higher timeframe (less noise, more reliable signal).

  3. COMBINATION: Could combine e.g. 5m RSI < 60 AND 1h RSI < 70 for
     a layered filter. This was not tested here but worth exploring.

  4. The negative filter (skip when RSI > X) is operationally simpler than
     the positive filter (only trade when RSI < X), because it only skips
     a fraction of trades rather than waiting for specific conditions.
""")

    print("=" * 90)
    print("  Done.")
    print("=" * 90)


if __name__ == "__main__":
    main()
