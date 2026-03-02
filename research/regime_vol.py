"""
Volatility & Time Regime Analysis for UU->DOWN on BTC

Uses Binance 1-minute klines to compute REAL volatility and trend metrics,
then cross-references with Polymarket UU->DOWN trade outcomes.

Tests whether the pattern works better under specific conditions:
  1. Hour of day (6 x 4-hour sessions)
  2. Realized volatility (rolling 1h vol from Binance 1m returns)
  3. Trend regime (1h BTC return: bullish vs bearish vs flat)
  4. Volume regime (Binance BTC volume in preceding hour)
  5. Combined: low-vol + specific hours
  6. Intra-cycle volatility (Binance 1m vol within the 5-min window)

Usage:
    python research/regime_vol.py data/prices_2026-02-28.csv data/prices_2026-03-01.csv data/prices_2026-03-02.csv
    python research/regime_vol.py data/prices_*.csv --coins BTC
"""

import argparse
import json
import sys
import os
import time as time_mod
import urllib.request
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import stats

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research_v2 import load_data, get_cycles, COINS, UP_ASK
from backtest_patterns import (
    resolve_all_outcomes,
    determine_history_outcomes,
    get_entry_price,
    SLIPPAGE, FEE_RATE, DEFAULT_MAX_ASK,
    INFERENCE_START, UP_THRESHOLD, DOWN_THRESHOLD,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PATTERN = "UU"
BUY_SIDE = "DOWN"
MAX_ASK = DEFAULT_MAX_ASK
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
KLINE_CACHE_FILE = Path(__file__).parent / "binance_klines_cache.json"


# ---------------------------------------------------------------------------
# Binance data fetching
# ---------------------------------------------------------------------------
def fetch_binance_klines(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> pd.DataFrame:
    """Fetch 1m klines from Binance, with local caching."""
    cache = {}
    cache_key = f"{symbol}_{interval}_{start_ms}_{end_ms}"

    if KLINE_CACHE_FILE.exists():
        with open(KLINE_CACHE_FILE) as f:
            cache = json.load(f)
        if cache_key in cache:
            df = pd.DataFrame(cache[cache_key])
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            print(f"    Binance cache hit: {len(df)} klines")
            return df

    print(f"    Fetching {symbol} {interval} klines from Binance...")
    all_klines = []
    current_start = start_ms

    while current_start < end_ms:
        url = (
            f"{BINANCE_KLINES_URL}?symbol={symbol}&interval={interval}"
            f"&startTime={current_start}&endTime={end_ms}&limit=1000"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

        if not data:
            break

        for k in data:
            all_klines.append({
                "open_time": k[0],
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "close_time": k[6],
                "quote_volume": float(k[7]),
                "trades": int(k[8]),
            })

        current_start = data[-1][6] + 1  # close_time + 1ms
        print(f"      fetched {len(all_klines)} klines so far...")
        time_mod.sleep(0.15)  # rate limit

    # Cache
    cache[cache_key] = all_klines
    with open(KLINE_CACHE_FILE, "w") as f:
        json.dump(cache, f)
    print(f"    Total: {len(all_klines)} klines cached")

    df = pd.DataFrame(all_klines)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


def compute_binance_features(klines: pd.DataFrame) -> pd.DataFrame:
    """Compute rolling features from 1m klines."""
    df = klines.copy()
    df = df.sort_values("open_time").reset_index(drop=True)

    # 1-min log returns
    df["log_ret"] = np.log(df["close"] / df["open"])

    # Rolling 1-hour realized volatility (std of 1m returns, annualized doesn't matter)
    df["vol_1h"] = df["log_ret"].rolling(60, min_periods=30).std()

    # Rolling 1-hour return (trend)
    df["ret_1h"] = df["close"].pct_change(60)

    # Rolling 1-hour volume
    df["vol_sum_1h"] = df["quote_volume"].rolling(60, min_periods=30).sum()

    # Rolling 1-hour trade count
    df["trades_1h"] = df["trades"].rolling(60, min_periods=30).sum()

    return df


def match_cycle_to_klines(
    cycle_start: pd.Timestamp,
    klines: pd.DataFrame,
) -> Optional[pd.Series]:
    """Find the kline row closest to cycle_start (within 2 min tolerance)."""
    # Make cycle_start tz-aware UTC if needed
    if cycle_start.tzinfo is None:
        cycle_start = cycle_start.tz_localize("UTC")

    diffs = (klines["open_time"] - cycle_start).abs()
    min_idx = diffs.idxmin()
    min_diff = diffs[min_idx]

    if min_diff > pd.Timedelta(minutes=2):
        return None
    return klines.loc[min_idx]


def get_intra_cycle_vol(
    cycle_start: pd.Timestamp,
    klines: pd.DataFrame,
) -> Optional[float]:
    """Get the realized vol of the 5 klines within the 5-min cycle."""
    if cycle_start.tzinfo is None:
        cycle_start = cycle_start.tz_localize("UTC")
    cycle_end = cycle_start + pd.Timedelta(minutes=5)
    mask = (klines["open_time"] >= cycle_start) & (klines["open_time"] < cycle_end)
    intra = klines[mask]
    if len(intra) < 3:
        return None
    return intra["log_ret"].std()


# ---------------------------------------------------------------------------
# Trade with Binance features
# ---------------------------------------------------------------------------
class VolTrade:
    """A UU->DOWN trade with Binance-derived regime features."""
    def __init__(self):
        self.cycle_idx: int = 0
        self.cycle_start: pd.Timestamp = pd.NaT
        self.coin: str = ""
        self.won: bool = False
        self.pnl: float = 0.0
        self.entry_price: float = 0.0
        # Binance features
        self.hour: int = 0
        self.vol_1h: float = 0.0         # 1h realized vol
        self.ret_1h: float = 0.0          # 1h return (trend)
        self.volume_1h: float = 0.0       # 1h quote volume
        self.trades_1h: int = 0           # 1h trade count
        self.intra_vol: float = 0.0       # intra-cycle vol
        self.btc_price: float = 0.0       # BTC price at cycle start


# ---------------------------------------------------------------------------
# Build trades
# ---------------------------------------------------------------------------
def build_trades(
    csv_paths: List[str],
    klines: pd.DataFrame,
    coins_filter: Optional[List[str]] = None,
) -> List[VolTrade]:
    """Run UU->DOWN backtest on BTC and attach Binance features."""
    df = load_data(csv_paths)
    cycles = get_cycles(df, min_rows=20)

    all_gamma = resolve_all_outcomes(cycles)
    all_history = [
        determine_history_outcomes(cycle, gamma)
        for cycle, gamma in zip(cycles, all_gamma)
    ]

    active_coins = [c.upper() for c in coins_filter] if coins_filter else ["BTC"]
    history: Dict[str, deque] = {c: deque(maxlen=10) for c in COINS}

    klines_feat = compute_binance_features(klines)
    trades: List[VolTrade] = []

    for cycle_idx, (cycle, trade_outcomes, hist_outcomes) in enumerate(
        zip(cycles, all_gamma, all_history)
    ):
        cycle_start_ts = cycle["cycle_start"].iloc[0]
        hour = cycle_start_ts.hour

        # Match to Binance kline
        kline_match = match_cycle_to_klines(cycle_start_ts, klines_feat)

        for coin in active_coins:
            hist = list(history[coin])
            if len(hist) < 2:
                continue
            recent = "".join(hist[-2:])
            if recent != PATTERN:
                continue

            coin_outcome = trade_outcomes.get(coin)
            if coin_outcome is None:
                continue

            entry_price = get_entry_price(cycle, coin, BUY_SIDE)
            if entry_price is None or entry_price > MAX_ASK:
                continue

            fill_price = entry_price + SLIPPAGE
            cost = fill_price * 5.0
            won = (BUY_SIDE == coin_outcome)
            if won:
                pnl = 5.0 * (1.0 - FEE_RATE) - cost
            else:
                pnl = -cost

            t = VolTrade()
            t.cycle_idx = cycle_idx
            t.cycle_start = cycle_start_ts
            t.coin = coin
            t.won = won
            t.pnl = pnl
            t.entry_price = entry_price
            t.hour = hour

            if kline_match is not None:
                t.vol_1h = kline_match.get("vol_1h", 0.0)
                t.ret_1h = kline_match.get("ret_1h", 0.0)
                t.volume_1h = kline_match.get("vol_sum_1h", 0.0)
                t.trades_1h = int(kline_match.get("trades_1h", 0))
                t.btc_price = kline_match.get("close", 0.0)

                if not np.isfinite(t.vol_1h):
                    t.vol_1h = 0.0
                if not np.isfinite(t.ret_1h):
                    t.ret_1h = 0.0

            intra = get_intra_cycle_vol(cycle_start_ts, klines_feat)
            t.intra_vol = intra if intra is not None and np.isfinite(intra) else 0.0

            trades.append(t)

        # Update history
        for coin in COINS:
            outcome = hist_outcomes.get(coin)
            if outcome == "UP":
                history[coin].append("U")
            elif outcome == "DOWN":
                history[coin].append("D")

    return trades


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------
def print_bin_table(
    trades: List[VolTrade],
    title: str,
    bin_func,
    min_n: int = 5,
) -> None:
    """Generic bin analysis with binomial test."""
    bins: Dict[str, List[VolTrade]] = {}
    for t in trades:
        b = bin_func(t)
        if b is None:
            continue
        bins.setdefault(b, []).append(t)

    if not bins:
        print(f"  No data for {title}")
        return

    overall_wr = sum(1 for t in trades if t.won) / len(trades) if trades else 0.5

    print(f"\n  --- {title} ---")
    print(f"  {'Bin':<28} {'N':>4} {'W':>3} {'L':>3} {'WR':>6} "
          f"{'PnL':>9} {'Avg':>9} {'vs avg':>7} {'p-val':>7}")
    print(f"  {'-'*28} {'-'*4} {'-'*3} {'-'*3} {'-'*6} "
          f"{'-'*9} {'-'*9} {'-'*7} {'-'*7}")

    for b in sorted(bins.keys()):
        bt = bins[b]
        n = len(bt)
        if n < min_n:
            continue
        w = sum(1 for t in bt if t.won)
        l = n - w
        wr = w / n
        pnl = sum(t.pnl for t in bt)
        avg = pnl / n
        delta = wr - overall_wr
        p_val = stats.binomtest(w, n, overall_wr).pvalue

        sig = ""
        if p_val < 0.01:
            sig = " **"
        elif p_val < 0.05:
            sig = " *"

        print(f"  {b:<28} {n:>4} {w:>3} {l:>3} {wr:>5.0%} "
              f"${pnl:>+8.2f} ${avg:>+8.4f} {delta:>+6.1%} {p_val:>6.3f}{sig}")


# ---------------------------------------------------------------------------
# Main analyses
# ---------------------------------------------------------------------------
def analyze_hour_of_day(trades: List[VolTrade]) -> None:
    """4-hour session analysis."""
    def session_bin(t):
        h = t.hour
        if 0 <= h < 4:
            return "00-04 UTC (Asia night)"
        elif 4 <= h < 8:
            return "04-08 UTC (Asia morning)"
        elif 8 <= h < 12:
            return "08-12 UTC (EU morning)"
        elif 12 <= h < 16:
            return "12-16 UTC (EU afternoon)"
        elif 16 <= h < 20:
            return "16-20 UTC (US afternoon)"
        else:
            return "20-24 UTC (US evening)"

    print_bin_table(trades, "Session (4-hour blocks)", session_bin)

    # Also by individual hour where we have enough data
    print_bin_table(trades, "Hour of Day (individual)", lambda t: f"{t.hour:02d}:00 UTC", min_n=3)


def analyze_volatility(trades: List[VolTrade]) -> None:
    """Realized vol regime from Binance."""
    vols = [t.vol_1h for t in trades if t.vol_1h > 0]
    if not vols:
        print("  No volatility data")
        return

    q33 = np.percentile(vols, 33)
    q67 = np.percentile(vols, 67)
    print(f"\n  1h Realized Vol percentiles: p33={q33:.6f} p67={q67:.6f}")

    def vol_bin(t):
        if t.vol_1h <= 0:
            return None
        if t.vol_1h < q33:
            return f"a) Low vol   (<{q33:.5f})"
        elif t.vol_1h < q67:
            return f"b) Med vol   ({q33:.5f}-{q67:.5f})"
        else:
            return f"c) High vol  (>{q67:.5f})"

    print_bin_table(trades, "1h Realized Volatility (Binance)", vol_bin)

    # Also absolute vol bins
    def abs_vol_bin(t):
        if t.vol_1h <= 0:
            return None
        v = t.vol_1h
        if v < 0.0005:
            return "a) Very low  (<0.05%)"
        elif v < 0.001:
            return "b) Low       (0.05-0.10%)"
        elif v < 0.002:
            return "c) Medium    (0.10-0.20%)"
        else:
            return "d) High      (>0.20%)"

    print_bin_table(trades, "1h Realized Vol (absolute bins)", abs_vol_bin)


def analyze_trend(trades: List[VolTrade]) -> None:
    """1h trend regime from Binance."""
    rets = [t.ret_1h for t in trades if t.ret_1h != 0]
    if not rets:
        print("  No trend data")
        return

    def trend_bin(t):
        r = t.ret_1h
        if r == 0:
            return None
        if r < -0.005:
            return "a) Strong down (<-0.5%)"
        elif r < -0.001:
            return "b) Mild down   (-0.5% to -0.1%)"
        elif r < 0.001:
            return "c) Flat        (-0.1% to +0.1%)"
        elif r < 0.005:
            return "d) Mild up     (+0.1% to +0.5%)"
        else:
            return "e) Strong up   (>+0.5%)"

    print_bin_table(trades, "1h BTC Trend (Binance)", trend_bin)


def analyze_volume(trades: List[VolTrade]) -> None:
    """Volume regime from Binance."""
    volumes = [t.volume_1h for t in trades if t.volume_1h > 0]
    if not volumes:
        print("  No volume data")
        return

    q33 = np.percentile(volumes, 33)
    q67 = np.percentile(volumes, 67)

    def vol_bin(t):
        if t.volume_1h <= 0:
            return None
        if t.volume_1h < q33:
            return f"a) Low volume"
        elif t.volume_1h < q67:
            return f"b) Med volume"
        else:
            return f"c) High volume"

    print_bin_table(trades, "1h Binance Quote Volume", vol_bin)


def analyze_combined(trades: List[VolTrade]) -> None:
    """Combined: vol + session."""
    vols = [t.vol_1h for t in trades if t.vol_1h > 0]
    if not vols:
        return

    vol_median = np.median(vols)

    def combined_bin(t):
        if t.vol_1h <= 0:
            return None
        vol_label = "LowVol" if t.vol_1h < vol_median else "HighVol"
        h = t.hour
        if 0 <= h < 8:
            session = "Asia"
        elif 8 <= h < 16:
            session = "EU"
        else:
            session = "US"
        return f"{vol_label} + {session}"

    print_bin_table(trades, "Volatility x Session (combined)", combined_bin, min_n=3)


def analyze_intra_cycle(trades: List[VolTrade]) -> None:
    """Intra-cycle volatility from Binance 1m klines."""
    ivols = [t.intra_vol for t in trades if t.intra_vol > 0]
    if not ivols:
        return

    median_iv = np.median(ivols)

    def iv_bin(t):
        if t.intra_vol <= 0:
            return None
        if t.intra_vol < median_iv:
            return "a) Low intra-cycle vol"
        else:
            return "b) High intra-cycle vol"

    print_bin_table(trades, "Intra-Cycle Vol (Binance 1m within 5min)", iv_bin)


# ---------------------------------------------------------------------------
# Summary: best and worst conditions
# ---------------------------------------------------------------------------
def summary_best_worst(trades: List[VolTrade]) -> None:
    """Print the best and worst conditions found."""
    print(f"\n  --- BEST & WORST CONDITIONS (all with n >= 5) ---")

    vols = [t.vol_1h for t in trades if t.vol_1h > 0]
    vol_median = np.median(vols) if vols else 0

    conditions = []

    # Test each hour
    for h in range(24):
        ht = [t for t in trades if t.hour == h]
        if len(ht) >= 5:
            w = sum(1 for t in ht if t.won)
            conditions.append((f"Hour {h:02d}:00", len(ht), w, w / len(ht),
                               sum(t.pnl for t in ht)))

    # Test vol regimes
    for label, filt in [
        ("Low vol (< median)", lambda t: 0 < t.vol_1h < vol_median),
        ("High vol (>= median)", lambda t: t.vol_1h >= vol_median),
    ]:
        ft = [t for t in trades if filt(t)]
        if len(ft) >= 5:
            w = sum(1 for t in ft if t.won)
            conditions.append((label, len(ft), w, w / len(ft),
                               sum(t.pnl for t in ft)))

    # Test trend
    for label, lo, hi in [
        ("BTC falling (1h ret < -0.1%)", -999, -0.001),
        ("BTC flat", -0.001, 0.001),
        ("BTC rising (1h ret > +0.1%)", 0.001, 999),
    ]:
        ft = [t for t in trades if lo <= t.ret_1h < hi]
        if len(ft) >= 5:
            w = sum(1 for t in ft if t.won)
            conditions.append((label, len(ft), w, w / len(ft),
                               sum(t.pnl for t in ft)))

    if not conditions:
        return

    # Sort by WR
    conditions.sort(key=lambda x: x[3], reverse=True)

    overall_wr = sum(1 for t in trades if t.won) / len(trades)

    print(f"\n  {'Condition':<32} {'N':>4} {'WR':>6} {'PnL':>9} {'vs avg':>7}")
    print(f"  {'-'*32} {'-'*4} {'-'*6} {'-'*9} {'-'*7}")
    for label, n, w, wr, pnl in conditions[:5]:
        print(f"  {label:<32} {n:>4} {wr:>5.0%} ${pnl:>+8.2f} {wr - overall_wr:>+6.1%}  {'BEST' if wr > overall_wr + 0.10 else ''}")
    print(f"  {'...'}")
    for label, n, w, wr, pnl in conditions[-3:]:
        print(f"  {label:<32} {n:>4} {wr:>5.0%} ${pnl:>+8.2f} {wr - overall_wr:>+6.1%}  {'WORST' if wr < overall_wr - 0.10 else ''}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Volatility & time regime analysis for UU->DOWN using Binance data"
    )
    parser.add_argument("csv_files", nargs="+", help="CSV price data files")
    parser.add_argument("--coins", type=str, default="BTC",
                        help="Coin filter (default: BTC)")
    args = parser.parse_args()

    for f in args.csv_files:
        if not Path(f).exists():
            print(f"ERROR: File not found: {f}")
            sys.exit(1)

    coins_filter = [c.strip().upper() for c in args.coins.split(",")]
    coin_label = ",".join(coins_filter)

    # Determine date range from CSVs
    print(f"\n  Loading Polymarket data...")
    all_df = pd.concat([pd.read_csv(f, parse_dates=["timestamp"]) for f in args.csv_files])
    start_ts = all_df["timestamp"].min()
    end_ts = all_df["timestamp"].max()

    # Convert to ms for Binance API (with 2h buffer for rolling features)
    start_ms = int((start_ts - pd.Timedelta(hours=2)).timestamp() * 1000)
    end_ms = int((end_ts + pd.Timedelta(minutes=10)).timestamp() * 1000)

    print(f"  Polymarket range: {start_ts} -> {end_ts}")
    print(f"  Fetching Binance BTCUSDT 1m klines...")
    klines = fetch_binance_klines("BTCUSDT", "1m", start_ms, end_ms)
    print(f"  Binance klines: {len(klines)} rows")

    print(f"\n  Building UU->DOWN trades for {coin_label}...")
    trades = build_trades(args.csv_files, klines, coins_filter)

    if not trades:
        print("  No trades found!")
        return

    total_w = sum(1 for t in trades if t.won)
    total_pnl = sum(t.pnl for t in trades)
    print(f"  {len(trades)} trades | {total_w}W/{len(trades)-total_w}L | "
          f"WR={total_w/len(trades):.1%} | PnL=${total_pnl:+.2f}")

    print("\n" + "=" * 78)
    print(f"  VOLATILITY & TIME REGIME ANALYSIS: UU->DOWN on {coin_label}")
    print(f"  Binance BTCUSDT 1-minute klines as regime indicators")
    print("=" * 78)

    # 1. Hour of day
    analyze_hour_of_day(trades)

    # 2. Realized volatility
    analyze_volatility(trades)

    # 3. Trend
    analyze_trend(trades)

    # 4. Volume
    analyze_volume(trades)

    # 5. Intra-cycle vol
    analyze_intra_cycle(trades)

    # 6. Combined
    analyze_combined(trades)

    # 7. Summary
    summary_best_worst(trades)

    print("\n" + "=" * 78)
    print("  INTERPRETATION")
    print("=" * 78)
    print("""
  This analysis tests YOUR hypothesis: UU->DOWN works better during
  low-volatility, low-volume periods (e.g., Asian session overnight)
  because the market is more mean-reverting when there are fewer
  fundamental/news-driven moves.

  Key metrics:
  - 'vs avg': WR difference from overall average. Positive = better.
  - 'p-val': binomial test vs overall WR. < 0.05 = significant.

  If low-vol + Asia session has significantly higher WR:
  -> Implement as a time/vol filter in the live bot
  -> Only trade during favorable regimes

  If no significant difference:
  -> The pattern is regime-independent (or sample too small to tell)
    """)
    print("=" * 78)


if __name__ == "__main__":
    main()
