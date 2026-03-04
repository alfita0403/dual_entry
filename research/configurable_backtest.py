"""
configurable_backtest.py - Configurable backtester with indicator filters and equity curve.

Edit the CONFIGURATION section at the top to test different strategies.
Generates an equity curve plot saved as research/equity_curve.png.

Usage:
    python research/configurable_backtest.py

Data sources:
    - Telonex 74-day parquet (59,423 settled markets, Dec 18 2025 - Feb 28 2026)
    - Binance BTCUSDT klines (downloaded and cached automatically)

Dependencies:
    pip install pandas numpy matplotlib
    (scipy is optional, used only if installed)
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
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    import matplotlib
    matplotlib.use("Agg")  # Non-interactive backend for PNG output
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False
    print("WARNING: matplotlib not installed. Run: pip install matplotlib")
    print("         Equity curve plot will be skipped.")

warnings.filterwarnings("ignore", category=FutureWarning)


# ============================================================================
# CONFIGURATION  (edit this section to test different strategies)
# ============================================================================

SIZE = 5.0          # Trade size in USDC tokens per trade
FEE_RATE = 0.0      # Maker fee rate (0% on Builder Program, 0.015 for taker)

# Pattern rules: when the recent outcome sequence matches "pattern",
# bet on "side" for coins in "coins", but only if the ask <= max_ask.
# Rules are checked top-to-bottom; first matching rule wins (priority order).
RULES = [
    {"pattern": "UUUUU", "side": "DOWN", "coins": ["ETH"],              "max_ask": 0.54},
    {"pattern": "UUUUU", "side": "DOWN", "coins": ["BTC","SOL","XRP"],  "max_ask": 0.51},
    {"pattern": "UUUU",  "side": "DOWN", "coins": ["ETH"],              "max_ask": 0.54},
    {"pattern": "UUUU",  "side": "DOWN", "coins": ["BTC","SOL","XRP"],  "max_ask": 0.51},
    {"pattern": "UDUU",  "side": "DOWN", "coins": ["BTC"],              "max_ask": 0.51},
    {"pattern": "UUU",   "side": "DOWN", "coins": ["BTC","ETH","SOL","XRP"], "max_ask": 0.51},
]

# Indicator filters: each filter blocks a trade if the condition is NOT met.
# Set to empty list [] to disable all filters (baseline).
#
# Supported indicators:
#   "RSI"          - RSI(period) on BTC, default period=14
#   "BB_UPPER"     - Bollinger Band upper distance (close - upper_bb), period=20, std=2
#   "BB_LOWER"     - Bollinger Band lower distance (close - lower_bb)
#   "MACD_HIST"    - MACD histogram (MACD line - signal line), params 12/26/9
#   "STOCH_K"      - Stochastic %K(period=14, smooth=3)
#   "VOL_RATIO"    - Volume / 20-period SMA volume
#   "ATR_PCT"      - ATR(period=14) as percentage of price
#
# Each filter dict:
#   indicator  - one of the above indicator names
#   period     - indicator period (default depends on indicator)
#   timeframe  - Binance kline interval: "5m", "15m", "1h", "4h", "1d"
#   condition  - "<" or ">"
#   threshold  - numeric threshold
#
# Example: skip trades when RSI(14) on 5m > 60
FILTERS = [
    {"indicator": "RSI", "period": 14, "timeframe": "5m", "condition": "<", "threshold": 60},
    # {"indicator": "BB_UPPER", "period": 20, "timeframe": "5m", "condition": "<", "threshold": 0},
    # {"indicator": "STOCH_K", "period": 14, "timeframe": "5m", "condition": "<", "threshold": 80},
    # {"indicator": "MACD_HIST", "timeframe": "5m", "condition": "<", "threshold": 0},
    # {"indicator": "VOL_RATIO", "timeframe": "5m", "condition": "<", "threshold": 2.0},
    # {"indicator": "ATR_PCT", "period": 14, "timeframe": "5m", "condition": ">", "threshold": 0.05},
]

# Data source toggle
USE_TELONEX = True   # True: use Telonex 74-day parquet; False: use CSV files only

# ============================================================================
# END CONFIGURATION
# ============================================================================


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
TELONEX_FILE = DATA_DIR / "telonex_updown_5m.parquet"
KLINE_CACHE_FILE = SCRIPT_DIR / "binance_klines_cache_configurable.json"
EQUITY_CURVE_FILE = SCRIPT_DIR / "equity_curve.png"
COINS = ["BTC", "ETH", "SOL", "XRP"]


# ---------------------------------------------------------------------------
# 1. Binance kline fetching with caching
# ---------------------------------------------------------------------------
def fetch_binance_klines(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> pd.DataFrame:
    """Fetch klines from Binance REST API with local JSON file caching."""
    cache = {}
    cache_key = f"{symbol}_{interval}_{start_ms}_{end_ms}"

    if KLINE_CACHE_FILE.exists():
        with open(KLINE_CACHE_FILE) as f:
            cache = json.load(f)
        if cache_key in cache:
            df = pd.DataFrame(cache[cache_key])
            df["open_time_ms"] = df["open_time"]
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            print(f"  [cache hit] {symbol} {interval}: {len(df):,} klines")
            return df

    print(f"  Downloading {symbol} {interval} klines from Binance...")
    base_url = "https://api.binance.com/api/v3/klines"
    all_klines = []
    current_start = start_ms

    while current_start < end_ms:
        url = (
            f"{base_url}?symbol={symbol}&interval={interval}"
            f"&startTime={current_start}&endTime={end_ms}&limit=1000"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            print(f"  WARNING: Binance API error: {e}")
            break

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
        if len(all_klines) % 5000 == 0:
            print(f"    fetched {len(all_klines):,} klines so far...")
        time_mod.sleep(0.12)  # respect rate limit

    if not all_klines:
        print("  ERROR: No klines downloaded")
        return pd.DataFrame()

    # Save to cache
    cache[cache_key] = all_klines
    with open(KLINE_CACHE_FILE, "w") as f:
        json.dump(cache, f)
    print(f"  Downloaded and cached {len(all_klines):,} klines")

    df = pd.DataFrame(all_klines)
    df["open_time_ms"] = df["open_time"]
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


# ---------------------------------------------------------------------------
# 2. Technical indicator computation
# ---------------------------------------------------------------------------
def compute_rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """Compute RSI using Wilder's smoothing method."""
    n = len(close)
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)

    avg_gain = np.full(n, np.nan)
    avg_loss = np.full(n, np.nan)
    rsi = np.full(n, np.nan)

    if n > period:
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


def compute_bollinger_bands(
    close: np.ndarray, period: int = 20, num_std: float = 2.0
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute Bollinger Bands. Returns (sma, upper_bb, lower_bb)."""
    sma = pd.Series(close).rolling(period, min_periods=period).mean().values
    std = pd.Series(close).rolling(period, min_periods=period).std().values
    upper = sma + num_std * std
    lower = sma - num_std * std
    return sma, upper, lower


def compute_macd(
    close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute MACD line, signal line, histogram."""
    ema_fast = pd.Series(close).ewm(span=fast, adjust=False).mean().values
    ema_slow = pd.Series(close).ewm(span=slow, adjust=False).mean().values
    macd_line = ema_fast - ema_slow
    signal_line = pd.Series(macd_line).ewm(span=signal, adjust=False).mean().values
    histogram = macd_line - signal_line
    # Mark first slow+signal-1 periods as NaN (warmup)
    warmup = slow + signal - 1
    histogram[:warmup] = np.nan
    macd_line[:warmup] = np.nan
    signal_line[:warmup] = np.nan
    return macd_line, signal_line, histogram


def compute_stochastic(
    close: np.ndarray, high: np.ndarray, low: np.ndarray,
    period: int = 14, smooth: int = 3
) -> np.ndarray:
    """Compute Stochastic %K (smoothed)."""
    n = len(close)
    raw_k = np.full(n, np.nan)
    for i in range(period - 1, n):
        h_max = np.max(high[i - period + 1:i + 1])
        l_min = np.min(low[i - period + 1:i + 1])
        if h_max != l_min:
            raw_k[i] = ((close[i] - l_min) / (h_max - l_min)) * 100.0
        else:
            raw_k[i] = 50.0
    stoch_k = pd.Series(raw_k).rolling(smooth, min_periods=smooth).mean().values
    return stoch_k


def compute_atr(
    close: np.ndarray, high: np.ndarray, low: np.ndarray, period: int = 14
) -> np.ndarray:
    """Compute ATR as percentage of price (Wilder's smoothing)."""
    n = len(close)
    tr = np.full(n, np.nan)
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    atr = np.full(n, np.nan)
    if n > period + 1:
        atr[period] = np.nanmean(tr[1:period + 1])
        for i in range(period + 1, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    # Normalize as % of price
    atr_pct = np.full(n, np.nan)
    for i in range(n):
        if not np.isnan(atr[i]) and close[i] > 0:
            atr_pct[i] = (atr[i] / close[i]) * 100.0
    return atr_pct


def compute_volume_ratio(volume: np.ndarray, period: int = 20) -> np.ndarray:
    """Compute volume / SMA(period) of volume."""
    vol_sma = pd.Series(volume).rolling(period, min_periods=period).mean().values
    n = len(volume)
    ratio = np.full(n, np.nan)
    for i in range(n):
        if not np.isnan(vol_sma[i]) and vol_sma[i] > 0:
            ratio[i] = volume[i] / vol_sma[i]
    return ratio


def compute_all_indicators(klines: pd.DataFrame, filters: List[dict]) -> pd.DataFrame:
    """Compute all indicators needed by the given filters on a kline DataFrame."""
    df = klines.copy().sort_values("open_time").reset_index(drop=True)
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    volume = df["volume"].values

    # Determine which indicators are needed
    needed = set(f["indicator"] for f in filters)

    if "RSI" in needed:
        periods = set(f.get("period", 14) for f in filters if f["indicator"] == "RSI")
        for p in periods:
            df[f"RSI_{p}"] = compute_rsi(close, period=p)

    if "BB_UPPER" in needed or "BB_LOWER" in needed:
        periods = set(
            f.get("period", 20)
            for f in filters
            if f["indicator"] in ("BB_UPPER", "BB_LOWER")
        )
        for p in periods:
            sma, upper, lower = compute_bollinger_bands(close, period=p)
            df[f"BB_UPPER_{p}"] = close - upper   # negative = below upper band
            df[f"BB_LOWER_{p}"] = close - lower    # positive = above lower band

    if "MACD_HIST" in needed:
        macd_line, signal_line, histogram = compute_macd(close)
        df["MACD_HIST"] = histogram

    if "STOCH_K" in needed:
        periods = set(f.get("period", 14) for f in filters if f["indicator"] == "STOCH_K")
        for p in periods:
            df[f"STOCH_K_{p}"] = compute_stochastic(close, high, low, period=p)

    if "ATR_PCT" in needed:
        periods = set(f.get("period", 14) for f in filters if f["indicator"] == "ATR_PCT")
        for p in periods:
            df[f"ATR_PCT_{p}"] = compute_atr(close, high, low, period=p)

    if "VOL_RATIO" in needed:
        df["VOL_RATIO"] = compute_volume_ratio(volume)

    # Build open_time_s for timestamp matching
    df["open_time_s"] = (df["open_time_ms"] / 1000).astype(int)

    return df


def get_indicator_column(filt: dict) -> str:
    """Return the DataFrame column name for a given filter config."""
    ind = filt["indicator"]
    period = filt.get("period")
    if ind == "RSI":
        return f"RSI_{period or 14}"
    elif ind == "BB_UPPER":
        return f"BB_UPPER_{period or 20}"
    elif ind == "BB_LOWER":
        return f"BB_LOWER_{period or 20}"
    elif ind == "MACD_HIST":
        return "MACD_HIST"
    elif ind == "STOCH_K":
        return f"STOCH_K_{period or 14}"
    elif ind == "ATR_PCT":
        return f"ATR_PCT_{period or 14}"
    elif ind == "VOL_RATIO":
        return "VOL_RATIO"
    else:
        raise ValueError(f"Unknown indicator: {ind}")


def filter_description(filt: dict) -> str:
    """Human-readable description of a filter."""
    ind = filt["indicator"]
    period = filt.get("period")
    tf = filt.get("timeframe", "5m")
    cond = filt["condition"]
    thresh = filt["threshold"]
    period_str = f"({period})" if period else ""
    return f"{ind}{period_str} {tf} {cond} {thresh}"


# ---------------------------------------------------------------------------
# 3. Telonex data loading
# ---------------------------------------------------------------------------
def load_telonex() -> pd.DataFrame:
    """Load the Telonex 74-day parquet dataset."""
    if not TELONEX_FILE.exists():
        print(f"ERROR: Telonex file not found at {TELONEX_FILE}")
        sys.exit(1)

    df = pd.read_parquet(TELONEX_FILE)
    df = df[df["result_id"].isin(["0", "1"])].copy()

    # Parse coin and timestamp from slug
    df["coin"] = df["slug"].str.extract(r"^(\w+)-updown-5m-")[0].str.upper()
    df["start_ts"] = df["slug"].str.extract(r"-(\d+)$")[0].astype(float).astype(int)
    df["outcome"] = df["result_id"].map({"0": "U", "1": "D"})
    df["start_dt"] = pd.to_datetime(df["start_ts"], unit="s", utc=True)
    df["date"] = df["start_dt"].dt.date
    df = df.sort_values(["coin", "start_ts"]).reset_index(drop=True)

    return df


# ---------------------------------------------------------------------------
# 4. CSV data loading (fallback)
# ---------------------------------------------------------------------------
def load_csvs() -> Optional[pd.DataFrame]:
    """Load data/prices_*.csv files."""
    import glob
    csv_pattern = str(DATA_DIR / "prices_*.csv")
    files = sorted(glob.glob(csv_pattern))
    if not files:
        print("ERROR: No CSV files found matching data/prices_*.csv")
        return None

    frames = []
    for f in files:
        frame = pd.read_csv(f, parse_dates=["timestamp", "cycle_start"])
        frames.append(frame)
        print(f"  Loaded {f}: {len(frame):,} rows")

    df = pd.concat(frames, ignore_index=True).sort_values("timestamp").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# 5. Build outcome sequences and find pattern trades
# ---------------------------------------------------------------------------
def build_coin_sequences(telonex_df: pd.DataFrame) -> Dict[str, dict]:
    """Build per-coin outcome sequences from Telonex data."""
    coin_seqs = {}
    for coin in COINS:
        cdf = telonex_df[telonex_df["coin"] == coin].sort_values("start_ts").reset_index(drop=True)
        coin_seqs[coin] = {
            "outcomes": cdf["outcome"].values,
            "dates": cdf["date"].values,
            "start_ts": cdf["start_ts"].values,
            "n": len(cdf),
        }
    return coin_seqs


def find_pattern_trades(
    coin_seqs: Dict[str, dict],
    rules: List[dict],
) -> pd.DataFrame:
    """Find all pattern-matched trades using priority rule ordering.

    For each coin's outcome sequence, at each position, check rules top-to-bottom.
    First matching rule fires (priority ordering).
    """
    all_trades = []

    for coin, data in coin_seqs.items():
        outcomes = data["outcomes"]
        dates = data["dates"]
        start_ts = data["start_ts"]
        n = data["n"]

        for i in range(1, n):
            for rule in rules:
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
                hit = (side == "DOWN" and actual == "D") or (side == "UP" and actual == "U")

                if hit:
                    pnl = (1.0 - max_ask) * SIZE - FEE_RATE * SIZE
                else:
                    pnl = -max_ask * SIZE - FEE_RATE * SIZE

                all_trades.append({
                    "coin": coin,
                    "date": dates[i],
                    "start_ts": int(start_ts[i]),
                    "pattern": rule["pattern"],
                    "side": side,
                    "max_ask": max_ask,
                    "outcome": actual,
                    "hit": hit,
                    "pnl": pnl,
                })
                break  # first matching rule wins

    if not all_trades:
        return pd.DataFrame()

    df = pd.DataFrame(all_trades)
    df = df.sort_values("start_ts").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# 6. Apply indicator filters to trades
# ---------------------------------------------------------------------------
def build_indicator_lookup(klines_ind: pd.DataFrame) -> Dict[int, dict]:
    """Build a dict mapping open_time_s -> row dict for fast timestamp lookup."""
    lookup = {}
    cols = [c for c in klines_ind.columns if c not in ("open_time", "open_time_ms")]
    for _, row in klines_ind.iterrows():
        ts = int(row["open_time_s"])
        lookup[ts] = {c: row[c] for c in cols}
    return lookup


def get_indicator_value(
    lookup: Dict[int, dict], ts: int, column: str
) -> float:
    """Get an indicator value at a given timestamp, trying exact match then +/-300s."""
    row = lookup.get(ts)
    if row is not None:
        val = row.get(column, np.nan)
        if isinstance(val, (int, float, np.integer, np.floating)) and np.isfinite(val):
            return float(val)

    # Try nearby timestamps (5-min offsets)
    for offset in [-300, 300, -600, 600]:
        row = lookup.get(ts + offset)
        if row is not None:
            val = row.get(column, np.nan)
            if isinstance(val, (int, float, np.integer, np.floating)) and np.isfinite(val):
                return float(val)
    return np.nan


def apply_filters(
    trades_df: pd.DataFrame,
    filters: List[dict],
    indicator_lookups: Dict[str, Dict[int, dict]],
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Apply indicator filters to trades.

    Returns (filtered_trades, all_trades_with_filter_data).
    The all_trades_with_filter_data df has extra columns: each filter's indicator
    value and a 'blocked' boolean.
    """
    if trades_df.empty:
        return trades_df.copy(), trades_df.copy()

    df = trades_df.copy()

    # Add indicator values for each filter
    for i, filt in enumerate(filters):
        tf = filt.get("timeframe", "5m")
        col = get_indicator_column(filt)
        col_name = f"filter_{i}_{filt['indicator']}"

        lookup = indicator_lookups.get(tf, {})
        df[col_name] = df["start_ts"].apply(
            lambda ts, lk=lookup, c=col: get_indicator_value(lk, ts, c)
        )

    # Determine which trades pass ALL filters
    df["blocked"] = False
    df["block_reason"] = ""

    for i, filt in enumerate(filters):
        col_name = f"filter_{i}_{filt['indicator']}"
        cond = filt["condition"]
        thresh = filt["threshold"]

        if cond == "<":
            # Trade is allowed if indicator < threshold; blocked if >= threshold
            blocked_mask = df[col_name] >= thresh
        elif cond == ">":
            # Trade is allowed if indicator > threshold; blocked if <= threshold
            blocked_mask = df[col_name] <= thresh
        else:
            raise ValueError(f"Unknown condition: {cond}")

        # Also block if indicator value is NaN (can't verify condition)
        nan_mask = df[col_name].isna()

        newly_blocked = blocked_mask & ~df["blocked"]
        df.loc[newly_blocked, "block_reason"] = filter_description(filt)
        df["blocked"] = df["blocked"] | blocked_mask | nan_mask

    filtered = df[~df["blocked"]].copy()
    return filtered, df


# ---------------------------------------------------------------------------
# 7. Reporting
# ---------------------------------------------------------------------------
def print_summary(trades_df: pd.DataFrame, label: str):
    """Print trade summary statistics."""
    if trades_df.empty:
        print(f"\n  {label}: No trades")
        return

    total = len(trades_df)
    wins = trades_df["hit"].sum()
    wr = wins / total if total > 0 else 0
    total_pnl = trades_df["pnl"].sum()
    avg_pnl = trades_df["pnl"].mean()

    # Compute daily stats
    daily = trades_df.groupby("date").agg(
        n_trades=("pnl", "count"),
        daily_pnl=("pnl", "sum"),
        daily_wr=("hit", "mean"),
    ).reset_index().sort_values("date")
    daily["cum_pnl"] = daily["daily_pnl"].cumsum()
    n_days = len(daily)
    avg_daily_pnl = daily["daily_pnl"].mean() if n_days > 0 else 0
    positive_days = (daily["daily_pnl"] > 0).sum()
    max_drawdown = compute_max_drawdown(trades_df["pnl"].values)

    print(f"\n  {label}")
    print(f"  {'='*65}")
    print(f"  Total trades:     {total:,}")
    print(f"  Wins:             {wins:,} ({wr*100:.1f}%)")
    print(f"  Losses:           {total - wins:,}")
    print(f"  Total PnL:        ${total_pnl:+.2f}")
    print(f"  Avg PnL/trade:    ${avg_pnl:+.4f}")
    print(f"  Avg daily PnL:    ${avg_daily_pnl:+.2f}/day ({n_days} days)")
    print(f"  Positive days:    {positive_days}/{n_days} ({positive_days/n_days*100:.0f}%)" if n_days > 0 else "")
    print(f"  Max drawdown:     ${max_drawdown:.2f}")


def compute_max_drawdown(pnl_series: np.ndarray) -> float:
    """Compute maximum drawdown from a PnL series."""
    cum = np.cumsum(pnl_series)
    peak = np.maximum.accumulate(cum)
    drawdown = peak - cum
    return float(np.max(drawdown)) if len(drawdown) > 0 else 0.0


def print_per_pattern_breakdown(trades_df: pd.DataFrame, rules: List[dict]):
    """Print per-pattern trade breakdown."""
    if trades_df.empty:
        return

    print(f"\n  Per-Pattern Breakdown:")
    print(f"  {'Pattern':<10} {'Coins':<18} {'Trades':>6} {'Wins':>5} {'WR':>7} {'PnL':>10} {'Avg':>8}")
    print(f"  {'-'*10} {'-'*18} {'-'*6} {'-'*5} {'-'*7} {'-'*10} {'-'*8}")

    for rule in rules:
        coins_str = ",".join(rule["coins"])
        mask = (trades_df["pattern"] == rule["pattern"]) & (trades_df["coin"].isin(rule["coins"]))
        sub = trades_df[mask]
        if len(sub) == 0:
            print(f"  {rule['pattern']:<10} {coins_str:<18} {'--':>6}")
            continue
        n = len(sub)
        w = sub["hit"].sum()
        wr_ = w / n
        pnl = sub["pnl"].sum()
        avg = sub["pnl"].mean()
        print(f"  {rule['pattern']:<10} {coins_str:<18} {n:>6} {w:>5} {wr_:>6.1%} ${pnl:>+9.2f} ${avg:>+7.4f}")


def print_per_coin_breakdown(trades_df: pd.DataFrame):
    """Print per-coin trade breakdown."""
    if trades_df.empty:
        return

    print(f"\n  Per-Coin Breakdown:")
    print(f"  {'Coin':<6} {'Trades':>6} {'Wins':>5} {'WR':>7} {'PnL':>10}")
    print(f"  {'-'*6} {'-'*6} {'-'*5} {'-'*7} {'-'*10}")
    for coin in COINS:
        sub = trades_df[trades_df["coin"] == coin]
        if len(sub) == 0:
            continue
        n = len(sub)
        w = sub["hit"].sum()
        wr_ = w / n
        pnl = sub["pnl"].sum()
        print(f"  {coin:<6} {n:>6} {w:>5} {wr_:>6.1%} ${pnl:>+9.2f}")


def print_daily_trajectory(trades_df: pd.DataFrame, compact: bool = True):
    """Print daily PnL trajectory."""
    if trades_df.empty:
        return

    daily = trades_df.groupby("date").agg(
        n_trades=("pnl", "count"),
        daily_pnl=("pnl", "sum"),
        daily_wr=("hit", "mean"),
    ).reset_index().sort_values("date")
    daily["cum_pnl"] = daily["daily_pnl"].cumsum()

    if compact:
        print(f"\n  Daily Trajectory (weekly snapshots):")
    else:
        print(f"\n  Daily Trajectory:")
    print(f"  {'Date':<12} {'Trades':>6} {'DailyPnL':>9} {'CumPnL':>9} {'WR':>6}")
    print(f"  {'-'*12} {'-'*6} {'-'*9} {'-'*9} {'-'*6}")

    for i, (_, r) in enumerate(daily.iterrows()):
        if compact and (i % 7 != 0 and i != len(daily) - 1):
            continue
        print(f"  {str(r['date']):<12} {r['n_trades']:>6} "
              f"${r['daily_pnl']:>+8.2f} ${r['cum_pnl']:>+8.2f} {r['daily_wr']:>5.1%}")


def print_filter_impact(all_trades_df: pd.DataFrame, filters: List[dict]):
    """Print statistics about which trades were filtered out and their quality."""
    if all_trades_df.empty or not filters:
        return

    blocked = all_trades_df[all_trades_df["blocked"]]
    passed = all_trades_df[~all_trades_df["blocked"]]

    total = len(all_trades_df)
    n_blocked = len(blocked)
    n_passed = len(passed)

    print(f"\n  Filter Impact:")
    print(f"  {'='*65}")
    print(f"  Total potential trades:   {total:,}")
    print(f"  Trades passed filters:    {n_passed:,} ({n_passed/total*100:.1f}%)")
    print(f"  Trades blocked:           {n_blocked:,} ({n_blocked/total*100:.1f}%)")

    if n_blocked > 0:
        blocked_wins = blocked["hit"].sum()
        blocked_wr = blocked_wins / n_blocked
        blocked_pnl = blocked["pnl"].sum()
        print(f"\n  Blocked trades quality:")
        print(f"    Wins: {blocked_wins:,}/{n_blocked:,} ({blocked_wr*100:.1f}%)")
        print(f"    PnL if taken: ${blocked_pnl:+.2f} (avg ${blocked_pnl/n_blocked:+.4f})")
        if blocked_wr < 0.5:
            print(f"    -> Blocked trades were LOSING on average -> filters HELP")
        else:
            print(f"    -> Blocked trades were WINNING on average -> filters HURT")

    if n_passed > 0:
        passed_wins = passed["hit"].sum()
        passed_wr = passed_wins / n_passed
        passed_pnl = passed["pnl"].sum()
        print(f"\n  Passed trades quality:")
        print(f"    Wins: {passed_wins:,}/{n_passed:,} ({passed_wr*100:.1f}%)")
        print(f"    PnL: ${passed_pnl:+.2f} (avg ${passed_pnl/n_passed:+.4f})")

    # Per-filter breakdown
    if len(filters) > 1:
        print(f"\n  Per-Filter Block Counts:")
        for i, filt in enumerate(filters):
            desc = filter_description(filt)
            blocked_by = all_trades_df[all_trades_df["block_reason"] == desc]
            print(f"    {desc}: blocked {len(blocked_by):,} trades (first blocker)")


# ---------------------------------------------------------------------------
# 8. Equity curve plotting
# ---------------------------------------------------------------------------
def plot_equity_curve(
    filtered_trades: pd.DataFrame,
    baseline_trades: Optional[pd.DataFrame],
    filters: List[dict],
    output_path: Path,
):
    """Generate and save equity curve plot."""
    if not HAS_MATPLOTLIB:
        print("  Skipping plot (matplotlib not installed)")
        return

    fig, ax = plt.subplots(figsize=(14, 7))

    # Plot baseline (no filters) if filters are active
    if baseline_trades is not None and len(filters) > 0 and not baseline_trades.empty:
        baseline_daily = baseline_trades.groupby("date")["pnl"].sum().reset_index()
        baseline_daily = baseline_daily.sort_values("date")
        baseline_daily["cum_pnl"] = baseline_daily["pnl"].cumsum()
        baseline_dates = pd.to_datetime(baseline_daily["date"])

        ax.plot(
            baseline_dates,
            baseline_daily["cum_pnl"],
            color="#BBBBBB",
            linewidth=1.5,
            linestyle="--",
            label=f"No filters (baseline)",
            alpha=0.8,
        )
        ax.fill_between(
            baseline_dates,
            0,
            baseline_daily["cum_pnl"],
            alpha=0.05,
            color="gray",
        )

    # Plot filtered equity curve
    if not filtered_trades.empty:
        daily = filtered_trades.groupby("date")["pnl"].sum().reset_index()
        daily = daily.sort_values("date")
        daily["cum_pnl"] = daily["pnl"].cumsum()
        dates = pd.to_datetime(daily["date"])

        color = "#2196F3" if len(filters) > 0 else "#4CAF50"
        label_parts = [filter_description(f) for f in filters]
        label = "With filters: " + ", ".join(label_parts) if label_parts else "Strategy PnL"

        ax.plot(
            dates,
            daily["cum_pnl"],
            color=color,
            linewidth=2.0,
            label=label,
        )
        ax.fill_between(
            dates,
            0,
            daily["cum_pnl"],
            alpha=0.15,
            color=color,
        )

    # Formatting
    ax.axhline(y=0, color="black", linewidth=0.5, linestyle="-")
    ax.set_xlabel("Date", fontsize=12)
    ax.set_ylabel("Cumulative PnL ($)", fontsize=12)

    # Build title
    n_rules = len(RULES)
    filter_summary = ", ".join(filter_description(f) for f in filters) if filters else "No filters"
    title = f"Equity Curve | {n_rules} rules | ${SIZE:.0f}/trade | Fee={FEE_RATE*100:.1f}%"
    ax.set_title(title, fontsize=13, fontweight="bold")

    # Subtitle with filter info
    subtitle = f"Filters: {filter_summary}"
    if not filtered_trades.empty:
        total_pnl = filtered_trades["pnl"].sum()
        total_trades = len(filtered_trades)
        wr = filtered_trades["hit"].mean() * 100
        subtitle += f" | {total_trades} trades | WR={wr:.1f}% | PnL=${total_pnl:+.2f}"
    ax.text(
        0.5, 1.02, subtitle,
        transform=ax.transAxes,
        fontsize=9,
        ha="center",
        va="bottom",
        color="gray",
    )

    ax.legend(loc="upper left", fontsize=10)
    ax.grid(True, alpha=0.3)

    # Date formatting
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
    fig.autofmt_xdate(rotation=45)

    plt.tight_layout()
    fig.savefig(str(output_path), dpi=150, bbox_inches="tight")
    plt.close(fig)

    print(f"\nEquity curve saved to: {output_path}")


# ---------------------------------------------------------------------------
# 9. Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 80)
    print("  CONFIGURABLE BACKTEST")
    print(f"  Size=${SIZE:.0f} | Fee={FEE_RATE*100:.1f}% | "
          f"Rules={len(RULES)} | Filters={len(FILTERS)}")
    print("=" * 80)

    # --- Print active configuration ---
    print(f"\n  Active Rules:")
    for i, rule in enumerate(RULES):
        coins_str = ",".join(rule["coins"])
        print(f"    {i+1}. {rule['pattern']} -> {rule['side']} | "
              f"coins=[{coins_str}] | max_ask={rule['max_ask']}")

    if FILTERS:
        print(f"\n  Active Filters:")
        for i, filt in enumerate(FILTERS):
            print(f"    {i+1}. {filter_description(filt)}")
    else:
        print(f"\n  Filters: NONE (baseline mode)")

    # ===================================================================
    # STEP 1: Load data
    # ===================================================================
    if USE_TELONEX:
        print(f"\n{'='*80}")
        print("STEP 1: LOADING TELONEX DATA")
        print(f"{'='*80}")
        telonex_df = load_telonex()
        print(f"  Loaded {len(telonex_df):,} settled markets")
        print(f"  Date range: {telonex_df['date'].min()} to {telonex_df['date'].max()}")
        for coin in COINS:
            n = len(telonex_df[telonex_df["coin"] == coin])
            print(f"    {coin}: {n:,} cycles")

        coin_seqs = build_coin_sequences(telonex_df)
    else:
        print(f"\n{'='*80}")
        print("STEP 1: LOADING CSV DATA")
        print(f"{'='*80}")
        csv_df = load_csvs()
        if csv_df is None:
            sys.exit(1)
        print("  NOTE: CSV mode not implemented in this version. Use USE_TELONEX=True.")
        sys.exit(1)

    # ===================================================================
    # STEP 2: Find all pattern trades (no filters yet)
    # ===================================================================
    print(f"\n{'='*80}")
    print("STEP 2: FINDING PATTERN TRADES")
    print(f"{'='*80}")

    all_trades = find_pattern_trades(coin_seqs, RULES)
    print(f"  Found {len(all_trades):,} pattern-matched trades")

    if all_trades.empty:
        print("  ERROR: No trades found. Check your rules.")
        sys.exit(1)

    # ===================================================================
    # STEP 3: Download Binance klines and compute indicators
    # ===================================================================
    indicator_lookups: Dict[str, Dict[int, dict]] = {}

    if FILTERS:
        print(f"\n{'='*80}")
        print("STEP 3: DOWNLOADING BINANCE DATA & COMPUTING INDICATORS")
        print(f"{'='*80}")

        # Determine unique timeframes needed
        timeframes = set(f.get("timeframe", "5m") for f in FILTERS)
        print(f"  Timeframes needed: {sorted(timeframes)}")

        # Date range from data (with warmup buffer)
        ts_min = int(all_trades["start_ts"].min())
        ts_max = int(all_trades["start_ts"].max())
        warmup_s = 200 * 5 * 60  # 200 x 5min candles warmup for indicators
        start_ms = int((ts_min - warmup_s) * 1000)
        end_ms = int((ts_max + 600) * 1000)

        for tf in sorted(timeframes):
            print(f"\n  --- {tf} timeframe ---")
            klines = fetch_binance_klines("BTCUSDT", tf, start_ms, end_ms)
            if klines.empty:
                print(f"  WARNING: No klines for {tf}, filters using this timeframe will block all trades")
                continue

            # Compute indicators for this timeframe's filters
            tf_filters = [f for f in FILTERS if f.get("timeframe", "5m") == tf]
            klines_ind = compute_all_indicators(klines, tf_filters)
            print(f"  Computed indicators on {len(klines_ind):,} klines")

            # Build lookup
            lookup = build_indicator_lookup(klines_ind)
            indicator_lookups[tf] = lookup
            print(f"  Lookup size: {len(lookup):,} timestamps")

            # Print indicator stats
            for filt in tf_filters:
                col = get_indicator_column(filt)
                values = [
                    row.get(col, np.nan) for row in lookup.values()
                    if isinstance(row.get(col, np.nan), (int, float))
                    and np.isfinite(row.get(col, np.nan))
                ]
                if values:
                    arr = np.array(values)
                    print(f"    {col}: mean={np.mean(arr):.2f}, "
                          f"median={np.median(arr):.2f}, "
                          f"min={np.min(arr):.2f}, max={np.max(arr):.2f}")
    else:
        print(f"\n  (No filters configured, skipping Binance data download)")

    # ===================================================================
    # STEP 4: Apply filters
    # ===================================================================
    print(f"\n{'='*80}")
    print("STEP 4: APPLYING FILTERS")
    print(f"{'='*80}")

    if FILTERS:
        filtered_trades, all_trades_annotated = apply_filters(
            all_trades, FILTERS, indicator_lookups
        )
        print(f"  Before filters: {len(all_trades):,} trades")
        print(f"  After filters:  {len(filtered_trades):,} trades")
        print(f"  Filtered out:   {len(all_trades) - len(filtered_trades):,} trades")
    else:
        filtered_trades = all_trades.copy()
        all_trades_annotated = all_trades.copy()
        all_trades_annotated["blocked"] = False
        all_trades_annotated["block_reason"] = ""
        print(f"  No filters active. All {len(filtered_trades):,} trades included.")

    # ===================================================================
    # STEP 5: Report results
    # ===================================================================
    print(f"\n{'='*80}")
    print("STEP 5: RESULTS")
    print(f"{'='*80}")

    # Baseline summary (no filters)
    if FILTERS:
        print_summary(all_trades, "BASELINE (no filters)")
        print_summary(filtered_trades, "WITH FILTERS")
    else:
        print_summary(filtered_trades, "STRATEGY RESULTS")

    print_per_pattern_breakdown(filtered_trades, RULES)
    print_per_coin_breakdown(filtered_trades)
    print_daily_trajectory(filtered_trades, compact=True)

    if FILTERS:
        print_filter_impact(all_trades_annotated, FILTERS)

    # ===================================================================
    # STEP 6: Plot equity curve
    # ===================================================================
    print(f"\n{'='*80}")
    print("STEP 6: EQUITY CURVE")
    print(f"{'='*80}")

    baseline_for_plot = all_trades if FILTERS else None
    plot_equity_curve(filtered_trades, baseline_for_plot, FILTERS, EQUITY_CURVE_FILE)

    # Final summary
    print(f"\n{'='*80}")
    print("  DONE")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()
