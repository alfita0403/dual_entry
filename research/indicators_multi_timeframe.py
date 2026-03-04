"""
indicators_multi_timeframe.py - Multi-timeframe technical indicator analysis.

Tests Bollinger Bands, MACD, Stochastic, and Volume indicators at 1m, 5m, 15m,
1h, 4h timeframes as FILTERS on the 6-rule YAML mean-reversion strategy.

For each indicator x timeframe combination:
  - Run the full 6-rule YAML backtest WITH and WITHOUT the filter
  - Report: trades, WR, daily PnL, improvement vs baseline

Also tests COMBINATIONS of best filters, including with RSI(14) 5m.

Data:
  - Telonex: 60,605 markets (59,423 settled), Dec 18 2025 - Mar 1 2026
  - Binance BTCUSDT klines for 1m, 5m, 15m, 1h, 4h

PnL model: 0% fee, $5 size, buy at max_ask per rule.
  Win: payout $5 (shares=1), cost=ask*$5 -> PnL = $5 - ask*$5 = $5*(1-ask)
  Loss: PnL = -ask*$5

Usage:
    python research/indicators_multi_timeframe.py
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
from scipy.stats import fisher_exact

warnings.filterwarnings("ignore", category=FutureWarning)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATA_FILE = Path(__file__).parent.parent / "data" / "telonex_updown_5m.parquet"
KLINE_CACHE_FILE = Path(__file__).parent / "binance_multi_tf_klines_cache.json"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

COINS = ["BTC", "ETH", "SOL", "XRP"]
SIZE = 5.0  # $5 per trade
FEE_RATE = 0.0  # 0% fee as specified

# Live YAML rules (from strategies/mean_reversion.yaml)
RULES = [
    {"pattern": "UUUUU", "side": "DOWN", "coins": ["ETH"],              "max_ask": 0.54},
    {"pattern": "UUUUU", "side": "DOWN", "coins": ["BTC", "SOL", "XRP"], "max_ask": 0.51},
    {"pattern": "UUUU",  "side": "DOWN", "coins": ["ETH"],              "max_ask": 0.54},
    {"pattern": "UUUU",  "side": "DOWN", "coins": ["BTC", "SOL", "XRP"], "max_ask": 0.51},
    {"pattern": "UDUU",  "side": "DOWN", "coins": ["BTC"],              "max_ask": 0.51},
    {"pattern": "UUU",   "side": "DOWN", "coins": ["BTC", "ETH", "SOL", "XRP"], "max_ask": 0.51},
]

TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h"]

# Timeframe in seconds for matching
TF_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
}

# Bonferroni: 4 indicators x 5 timeframes x ~2 directions = 40 tests
BONFERRONI_ALPHA = 0.05 / 40


# ---------------------------------------------------------------------------
# 1. Load Telonex data
# ---------------------------------------------------------------------------
def load_telonex() -> Tuple[pd.DataFrame, Dict]:
    """Load Telonex data and build per-coin outcome sequences."""
    print(f"Loading {DATA_FILE}...")
    df = pd.read_parquet(DATA_FILE)
    print(f"  Total markets: {len(df):,}")

    df = df[df["result_id"].isin(["0", "1"])].copy()
    print(f"  Settled markets: {len(df):,}")

    df["coin"] = df["slug"].str.extract(r"^(\w+)-updown-5m-")[0].str.upper()
    df["unix_ts"] = df["slug"].str.extract(r"-(\d+)$")[0].astype(float).astype(int)
    df["outcome"] = df["result_id"].map({"0": "U", "1": "D"})
    df["dt"] = pd.to_datetime(df["unix_ts"], unit="s", utc=True)
    df["date"] = df["dt"].dt.date
    df = df.dropna(subset=["coin"])
    df = df.sort_values(["coin", "unix_ts"]).reset_index(drop=True)

    for coin in COINS:
        cnt = (df["coin"] == coin).sum()
        print(f"    {coin}: {cnt:,} markets")

    ts_min = df["unix_ts"].min()
    ts_max = df["unix_ts"].max()
    dt_min = datetime.fromtimestamp(ts_min, tz=timezone.utc)
    dt_max = datetime.fromtimestamp(ts_max, tz=timezone.utc)
    print(f"  Date range: {dt_min:%Y-%m-%d %H:%M} to {dt_max:%Y-%m-%d %H:%M}")

    # Build per-coin sequences
    coin_seqs = {}
    for coin in COINS:
        cdf = df[df["coin"] == coin].sort_values("unix_ts").reset_index(drop=True)
        coin_seqs[coin] = {
            "outcomes": cdf["outcome"].values,
            "dates": cdf["date"].values,
            "unix_ts": cdf["unix_ts"].values,
            "n": len(cdf),
        }

    return df, coin_seqs


# ---------------------------------------------------------------------------
# 2. Fetch Binance klines with caching
# ---------------------------------------------------------------------------
def fetch_binance_klines(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> pd.DataFrame:
    """Fetch klines from Binance with local file caching."""
    cache = {}
    cache_key = f"{symbol}_{interval}_{start_ms}_{end_ms}"

    if KLINE_CACHE_FILE.exists():
        with open(KLINE_CACHE_FILE) as f:
            cache = json.load(f)
        if cache_key in cache:
            df = pd.DataFrame(cache[cache_key])
            df["open_time_ms"] = df["open_time"]
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            print(f"    Cache hit ({interval}): {len(df):,} klines")
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
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            print(f"      Error fetching: {e}, retrying in 2s...")
            time_mod.sleep(2)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
            except Exception as e2:
                print(f"      Retry failed: {e2}, skipping remaining")
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
        if len(all_klines) % 10000 == 0:
            print(f"      fetched {len(all_klines):,} klines so far...")
        time_mod.sleep(0.12)

    # Cache
    cache[cache_key] = all_klines
    with open(KLINE_CACHE_FILE, "w") as f:
        json.dump(cache, f)
    print(f"    Total ({interval}): {len(all_klines):,} klines fetched and cached")

    df = pd.DataFrame(all_klines)
    if len(df) == 0:
        return df
    df["open_time_ms"] = df["open_time"]
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


# ---------------------------------------------------------------------------
# 3. Compute indicators on a kline DataFrame
# ---------------------------------------------------------------------------
def compute_rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder's smoothed RSI."""
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


def compute_indicators(klines: pd.DataFrame) -> pd.DataFrame:
    """Compute BB, MACD, Stochastic, Volume ratio, and RSI on kline data."""
    df = klines.copy()
    df = df.sort_values("open_time").reset_index(drop=True)

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    volume = df["volume"].values
    n = len(close)

    # --- RSI(14) ---
    df["rsi_14"] = compute_rsi(close, 14)

    # --- Bollinger Bands (20, 2) ---
    bb_period = 20
    bb_std_mult = 2.0
    sma_20 = pd.Series(close).rolling(bb_period, min_periods=bb_period).mean().values
    std_20 = pd.Series(close).rolling(bb_period, min_periods=bb_period).std().values
    upper_bb = sma_20 + bb_std_mult * std_20
    lower_bb = sma_20 - bb_std_mult * std_20
    # BB %B: (close - lower) / (upper - lower)
    bb_pctb = np.full(n, np.nan)
    bb_pos = np.full(n, np.nan)
    for i in range(n):
        if not np.isnan(sma_20[i]) and (upper_bb[i] - lower_bb[i]) > 0:
            bb_pctb[i] = (close[i] - lower_bb[i]) / (upper_bb[i] - lower_bb[i])
            if close[i] > upper_bb[i]:
                bb_pos[i] = 1   # above upper
            elif close[i] < lower_bb[i]:
                bb_pos[i] = -1  # below lower
            else:
                bb_pos[i] = 0   # inside
    df["bb_pctb"] = bb_pctb
    df["bb_position"] = bb_pos

    # --- MACD (12, 26, 9) ---
    ema_12 = pd.Series(close).ewm(span=12, adjust=False).mean().values
    ema_26 = pd.Series(close).ewm(span=26, adjust=False).mean().values
    macd_line = ema_12 - ema_26
    signal_line = pd.Series(macd_line).ewm(span=9, adjust=False).mean().values
    macd_hist = macd_line - signal_line
    macd_signal = np.full(n, np.nan)
    for i in range(34, n):
        if macd_line[i] > signal_line[i]:
            macd_signal[i] = 1  # bullish
        else:
            macd_signal[i] = -1  # bearish
    df["macd_signal"] = macd_signal
    df["macd_hist"] = macd_hist

    # --- Stochastic %K(14, 3) ---
    stoch_period = 14
    stoch_smooth = 3
    raw_k = np.full(n, np.nan)
    for i in range(stoch_period - 1, n):
        h_max = np.max(high[i - stoch_period + 1:i + 1])
        l_min = np.min(low[i - stoch_period + 1:i + 1])
        if h_max != l_min:
            raw_k[i] = ((close[i] - l_min) / (h_max - l_min)) * 100.0
        else:
            raw_k[i] = 50.0
    stoch_k = pd.Series(raw_k).rolling(stoch_smooth, min_periods=stoch_smooth).mean().values
    df["stoch_k"] = stoch_k

    # --- Volume Ratio: current / 20-SMA ---
    vol_sma_20 = pd.Series(volume).rolling(20, min_periods=20).mean().values
    vol_ratio = np.full(n, np.nan)
    for i in range(n):
        if not np.isnan(vol_sma_20[i]) and vol_sma_20[i] > 0:
            vol_ratio[i] = volume[i] / vol_sma_20[i]
    df["vol_ratio"] = vol_ratio

    # open_time in seconds for matching
    df["open_time_s"] = (df["open_time_ms"] / 1000).astype(int)

    return df


# ---------------------------------------------------------------------------
# 4. Build timestamp -> indicator lookup per timeframe
# ---------------------------------------------------------------------------
def build_indicator_lookup(klines_ind: pd.DataFrame) -> Dict[int, dict]:
    """Build dict mapping open_time_s -> indicator dict for fast lookup."""
    lookup = {}
    cols = ["rsi_14", "bb_position", "bb_pctb", "macd_signal", "macd_hist",
            "stoch_k", "vol_ratio"]
    for idx, row in klines_ind.iterrows():
        ts = int(row["open_time_s"])
        rec = {}
        for col in cols:
            val = row.get(col, np.nan)
            if isinstance(val, (int, float, np.integer, np.floating)) and np.isfinite(val):
                rec[col] = float(val)
            else:
                rec[col] = np.nan
        lookup[ts] = rec
    return lookup


def find_nearest_kline(lookup: Dict[int, dict], ts: int, tf_seconds: int) -> Optional[dict]:
    """Find the kline that was active at timestamp ts.

    For a given timeframe, the candle open at time T covers [T, T+tf_seconds).
    We want the candle whose open_time <= ts < open_time + tf_seconds.
    Align ts down to the nearest candle boundary.
    """
    # Align to candle boundary
    aligned = (ts // tf_seconds) * tf_seconds
    rec = lookup.get(aligned)
    if rec is not None:
        return rec
    # Try nearby alignments (clock might be slightly off)
    for offset in [-tf_seconds, tf_seconds, -2 * tf_seconds, 2 * tf_seconds]:
        rec = lookup.get(aligned + offset)
        if rec is not None:
            return rec
    return None


# ---------------------------------------------------------------------------
# 5. YAML rule trade finder (same as edge_stability_telonex.py)
# ---------------------------------------------------------------------------
def find_yaml_rule_trades(coin_seqs: Dict, rules: List[dict]) -> List[dict]:
    """Simulate the live YAML rules with priority ordering."""
    all_trades = []
    for coin, data in coin_seqs.items():
        outcomes = data["outcomes"]
        dates = data["dates"]
        unix_ts = data["unix_ts"]
        n = data["n"]

        for i in range(1, n):
            for rule in rules:
                if coin not in rule["coins"]:
                    continue
                pat = list(rule["pattern"])
                plen = len(pat)
                if i < plen:
                    continue
                match = True
                for j in range(plen):
                    if outcomes[i - plen + j] != pat[j]:
                        match = False
                        break
                if not match:
                    continue
                side = rule["side"]
                max_ask = rule["max_ask"]
                actual = outcomes[i]
                hit = 1 if (side == "DOWN" and actual == "D") or \
                           (side == "UP" and actual == "U") else 0
                all_trades.append({
                    "coin": coin,
                    "idx": i,
                    "outcome": actual,
                    "date": dates[i],
                    "unix_ts": int(unix_ts[i]),
                    "pattern": rule["pattern"],
                    "side": side,
                    "max_ask": max_ask,
                    "hit": hit,
                })
                break  # first matching rule per coin per position
    return all_trades


# ---------------------------------------------------------------------------
# 6. PnL computation (0% fee, $5 size)
# ---------------------------------------------------------------------------
def compute_pnl(hit: int, max_ask: float) -> float:
    """Compute PnL for a single trade. 0% fee, $5 size."""
    if hit:
        # Win: buy at ask, payout $1/share -> profit = (1 - ask) * size
        return SIZE * (1.0 - max_ask)
    else:
        # Loss: lose cost = ask * size
        return -SIZE * max_ask


# ---------------------------------------------------------------------------
# 7. Filter definitions
# ---------------------------------------------------------------------------
FILTERS = {
    "bb_above_upper": {
        "label": "BB above upper band (skip)",
        "indicator": "bb_position",
        "condition": lambda val: val == 1,  # True = skip this trade
        "description": "Skip when price > upper Bollinger Band",
    },
    "bb_below_lower": {
        "label": "BB below lower band (skip)",
        "indicator": "bb_position",
        "condition": lambda val: val == -1,
        "description": "Skip when price < lower Bollinger Band",
    },
    "macd_bullish": {
        "label": "MACD bullish (skip)",
        "indicator": "macd_signal",
        "condition": lambda val: val == 1,
        "description": "Skip when MACD > signal (bullish)",
    },
    "macd_bearish": {
        "label": "MACD bearish (skip)",
        "indicator": "macd_signal",
        "condition": lambda val: val == -1,
        "description": "Skip when MACD < signal (bearish)",
    },
    "stoch_overbought": {
        "label": "Stochastic > 80 (skip)",
        "indicator": "stoch_k",
        "condition": lambda val: val > 80,
        "description": "Skip when %K > 80 (overbought)",
    },
    "stoch_oversold": {
        "label": "Stochastic < 20 (skip)",
        "indicator": "stoch_k",
        "condition": lambda val: val < 20,
        "description": "Skip when %K < 20 (oversold)",
    },
    "vol_high": {
        "label": "Volume > 1.5x avg (skip)",
        "indicator": "vol_ratio",
        "condition": lambda val: val > 1.5,
        "description": "Skip when volume > 1.5x 20-SMA",
    },
    "vol_low": {
        "label": "Volume < 0.5x avg (skip)",
        "indicator": "vol_ratio",
        "condition": lambda val: val < 0.5,
        "description": "Skip when volume < 0.5x 20-SMA",
    },
    "rsi_above_60": {
        "label": "RSI > 60 (skip)",
        "indicator": "rsi_14",
        "condition": lambda val: val > 60,
        "description": "Skip when RSI(14) > 60",
    },
    "rsi_above_70": {
        "label": "RSI > 70 (skip)",
        "indicator": "rsi_14",
        "condition": lambda val: val > 70,
        "description": "Skip when RSI(14) > 70",
    },
}


# ---------------------------------------------------------------------------
# 8. Apply filter and compute stats
# ---------------------------------------------------------------------------
def apply_filter(
    trades_df: pd.DataFrame,
    indicator_col: str,
    skip_condition,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Apply a filter to trades. Returns (kept_trades, skipped_trades).

    skip_condition: function(value) -> bool. If True, skip the trade.
    """
    valid = trades_df[trades_df[indicator_col].notna()].copy()
    skip_mask = valid[indicator_col].apply(skip_condition)
    kept = valid[~skip_mask]
    skipped = valid[skip_mask]
    return kept, skipped


def compute_stats(trades_df: pd.DataFrame) -> dict:
    """Compute summary stats for a set of trades."""
    n = len(trades_df)
    if n == 0:
        return {"n": 0, "wins": 0, "wr": 0, "total_pnl": 0, "daily_pnl": 0, "n_days": 0}

    wins = int(trades_df["hit"].sum())
    wr = wins / n
    total_pnl = trades_df["pnl"].sum()

    # Daily PnL
    n_days = trades_df["date"].nunique()
    daily_pnl = total_pnl / n_days if n_days > 0 else 0

    return {
        "n": n,
        "wins": wins,
        "wr": wr,
        "total_pnl": total_pnl,
        "daily_pnl": daily_pnl,
        "n_days": n_days,
    }


# ---------------------------------------------------------------------------
# 9. Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 90)
    print("MULTI-TIMEFRAME INDICATOR ANALYSIS")
    print("Indicators: Bollinger Bands, MACD, Stochastic, Volume, RSI")
    print("Timeframes: 1m, 5m, 15m, 1h, 4h")
    print("Strategy: 6-rule YAML mean-reversion (0% fee, $5 size)")
    print("=" * 90)

    # ----- Load Telonex -----
    tdf, coin_seqs = load_telonex()

    # ----- Find all YAML rule trades -----
    print("\nFinding YAML rule trades...")
    trades_list = find_yaml_rule_trades(coin_seqs, RULES)
    trades_df = pd.DataFrame(trades_list)
    trades_df["pnl"] = trades_df.apply(
        lambda r: compute_pnl(r["hit"], r["max_ask"]), axis=1
    )
    trades_df = trades_df.sort_values("unix_ts").reset_index(drop=True)

    baseline_stats = compute_stats(trades_df)
    print(f"\nBASELINE (no filter):")
    print(f"  Trades: {baseline_stats['n']:,}")
    print(f"  Win rate: {baseline_stats['wr']:.4f} ({baseline_stats['wr']:.1%})")
    print(f"  Total PnL: ${baseline_stats['total_pnl']:.2f}")
    print(f"  Daily PnL: ${baseline_stats['daily_pnl']:.2f}")
    print(f"  Days: {baseline_stats['n_days']}")

    # ----- Fetch Binance klines for all timeframes -----
    ts_min = int(tdf["unix_ts"].min())
    ts_max = int(tdf["unix_ts"].max())
    # Extra warmup: 200 periods of the largest timeframe (4h = 14400s)
    warmup_s = 200 * 14400
    start_ms = int((ts_min - warmup_s) * 1000)
    end_ms = int((ts_max + 600) * 1000)

    print(f"\nFetching Binance BTCUSDT klines for all timeframes...")
    tf_lookups = {}
    tf_klines = {}

    for tf in TIMEFRAMES:
        print(f"\n  Timeframe: {tf}")
        klines = fetch_binance_klines("BTCUSDT", tf, start_ms, end_ms)
        if len(klines) == 0:
            print(f"    WARNING: No klines for {tf}")
            continue

        klines_ind = compute_indicators(klines)
        tf_klines[tf] = klines_ind

        # Report coverage
        for col in ["rsi_14", "bb_position", "macd_signal", "stoch_k", "vol_ratio"]:
            valid = klines_ind[col].notna().sum()
            pct = valid / len(klines_ind) if len(klines_ind) > 0 else 0
            print(f"      {col}: {valid:,} / {len(klines_ind):,} ({pct:.1%})")

        lookup = build_indicator_lookup(klines_ind)
        tf_lookups[tf] = lookup
        print(f"    Lookup size: {len(lookup):,}")

    # ----- Enrich trades with indicator values for each timeframe -----
    print(f"\nEnriching trades with indicators from all timeframes...")

    indicator_cols = ["rsi_14", "bb_position", "bb_pctb", "macd_signal",
                      "macd_hist", "stoch_k", "vol_ratio"]

    for tf in TIMEFRAMES:
        if tf not in tf_lookups:
            continue
        lookup = tf_lookups[tf]
        tf_secs = TF_SECONDS[tf]

        for col in indicator_cols:
            col_name = f"{col}_{tf}"
            vals = []
            for _, row in trades_df.iterrows():
                ts = int(row["unix_ts"])
                kline = find_nearest_kline(lookup, ts, tf_secs)
                if kline is not None and not np.isnan(kline.get(col, np.nan)):
                    vals.append(kline[col])
                else:
                    vals.append(np.nan)
            trades_df[col_name] = vals

        matched = trades_df[f"rsi_14_{tf}"].notna().sum()
        print(f"  {tf}: {matched:,} / {len(trades_df):,} trades matched "
              f"({matched / len(trades_df):.1%})")

    # ===================================================================
    # SECTION 1: INDIVIDUAL INDICATOR x TIMEFRAME ANALYSIS
    # ===================================================================
    print("\n\n" + "=" * 90)
    print("SECTION 1: INDIVIDUAL INDICATOR x TIMEFRAME FILTER RESULTS")
    print("=" * 90)

    # Collect all results for ranking
    all_results = []

    # Header
    print(f"\n{'Filter':<35} {'TF':<5} {'Kept':>6} {'Skipped':>7} "
          f"{'WR':>7} {'dWR':>7} {'DailyPnL':>10} {'dDaily':>8} "
          f"{'p-value':>10} {'Sig':>8}")
    print("-" * 115)

    # Print baseline row
    print(f"{'BASELINE (no filter)':<35} {'--':<5} "
          f"{baseline_stats['n']:>6,} {'--':>7} "
          f"{baseline_stats['wr']:>6.1%} {'--':>7} "
          f"${baseline_stats['daily_pnl']:>9.2f} {'--':>8} "
          f"{'--':>10} {'--':>8}")
    print("-" * 115)

    for filter_key, filt in FILTERS.items():
        for tf in TIMEFRAMES:
            ind_col = f"{filt['indicator']}_{tf}"
            if ind_col not in trades_df.columns:
                continue

            kept, skipped = apply_filter(
                trades_df, ind_col, filt["condition"]
            )
            if len(kept) < 20:
                continue

            kept_stats = compute_stats(kept)
            delta_wr = kept_stats["wr"] - baseline_stats["wr"]
            delta_daily = kept_stats["daily_pnl"] - baseline_stats["daily_pnl"]

            # Fisher exact: kept vs skipped
            if len(skipped) >= 5:
                sk_wins = int(skipped["hit"].sum())
                table = [
                    [kept_stats["wins"], len(kept) - kept_stats["wins"]],
                    [sk_wins, len(skipped) - sk_wins],
                ]
                _, p_fisher = fisher_exact(table, alternative="two-sided")
            else:
                p_fisher = 1.0

            sig = ""
            if p_fisher < BONFERRONI_ALPHA:
                sig = "BONF***"
            elif p_fisher < 0.05:
                sig = "p<.05*"

            print(f"{filt['label']:<35} {tf:<5} "
                  f"{len(kept):>6,} {len(skipped):>7,} "
                  f"{kept_stats['wr']:>6.1%} {delta_wr:>+6.1%} "
                  f"${kept_stats['daily_pnl']:>9.2f} ${delta_daily:>+7.2f} "
                  f"{p_fisher:>10.5f} {sig:>8}")

            all_results.append({
                "filter": filter_key,
                "filter_label": filt["label"],
                "timeframe": tf,
                "indicator_col": ind_col,
                "n_kept": len(kept),
                "n_skipped": len(skipped),
                "wr_kept": kept_stats["wr"],
                "delta_wr": delta_wr,
                "daily_pnl": kept_stats["daily_pnl"],
                "delta_daily": delta_daily,
                "total_pnl": kept_stats["total_pnl"],
                "p_fisher": p_fisher,
                "sig_bonf": p_fisher < BONFERRONI_ALPHA,
                "sig_05": p_fisher < 0.05,
                "wr_skipped": int(skipped["hit"].sum()) / len(skipped) if len(skipped) > 0 else 0,
            })

    # ===================================================================
    # SECTION 2: RANKING BY IMPROVEMENT
    # ===================================================================
    print("\n\n" + "=" * 90)
    print("SECTION 2: TOP FILTERS RANKED BY DAILY PnL IMPROVEMENT")
    print("=" * 90)

    results_sorted = sorted(all_results, key=lambda x: x["delta_daily"], reverse=True)

    print(f"\n{'Rank':>4} {'Filter':<35} {'TF':<5} {'Kept':>6} "
          f"{'WR':>7} {'dWR':>7} {'DailyPnL':>10} {'dDaily':>8} "
          f"{'p-val':>10} {'Sig':>8}")
    print("-" * 115)

    for rank, r in enumerate(results_sorted[:25], 1):
        sig = "BONF***" if r["sig_bonf"] else ("p<.05*" if r["sig_05"] else "")
        print(f"{rank:>4} {r['filter_label']:<35} {r['timeframe']:<5} "
              f"{r['n_kept']:>6,} "
              f"{r['wr_kept']:>6.1%} {r['delta_wr']:>+6.1%} "
              f"${r['daily_pnl']:>9.2f} ${r['delta_daily']:>+7.2f} "
              f"{r['p_fisher']:>10.5f} {sig:>8}")

    # ===================================================================
    # SECTION 2b: RANKING BY P-VALUE (statistical significance)
    # ===================================================================
    print("\n\n" + "=" * 90)
    print("SECTION 2b: TOP FILTERS RANKED BY STATISTICAL SIGNIFICANCE")
    print("=" * 90)

    results_by_p = sorted(all_results, key=lambda x: x["p_fisher"])

    print(f"\n{'Rank':>4} {'Filter':<35} {'TF':<5} {'Kept':>6} {'Skip':>6} "
          f"{'WR_kept':>8} {'WR_skip':>8} {'dWR':>7} "
          f"{'p-val':>10} {'Sig':>8}")
    print("-" * 115)

    for rank, r in enumerate(results_by_p[:25], 1):
        sig = "BONF***" if r["sig_bonf"] else ("p<.05*" if r["sig_05"] else "")
        print(f"{rank:>4} {r['filter_label']:<35} {r['timeframe']:<5} "
              f"{r['n_kept']:>6,} {r['n_skipped']:>6,} "
              f"{r['wr_kept']:>7.1%} {r['wr_skipped']:>7.1%} {r['delta_wr']:>+6.1%} "
              f"{r['p_fisher']:>10.5f} {sig:>8}")

    # ===================================================================
    # SECTION 3: SUMMARY TABLE (indicator x timeframe)
    # ===================================================================
    print("\n\n" + "=" * 90)
    print("SECTION 3: SUMMARY TABLE - WR IMPROVEMENT BY INDICATOR x TIMEFRAME")
    print("=" * 90)

    # Group by core indicator (not direction) and show the best direction
    core_indicators = {
        "Bollinger Bands": ["bb_above_upper", "bb_below_lower"],
        "MACD": ["macd_bullish", "macd_bearish"],
        "Stochastic": ["stoch_overbought", "stoch_oversold"],
        "Volume Ratio": ["vol_high", "vol_low"],
        "RSI(14)": ["rsi_above_60", "rsi_above_70"],
    }

    print(f"\n{'Indicator':<20} {'Best Filter':<30} ", end="")
    for tf in TIMEFRAMES:
        print(f"{'|  ' + tf:>10}", end="")
    print(f"{'| Best TF':>12}")
    print("-" * 110)

    for ind_name, filter_keys in core_indicators.items():
        # For each TF, find the best filter direction
        best_per_tf = {}
        for tf in TIMEFRAMES:
            best_r = None
            for fk in filter_keys:
                matching = [r for r in all_results
                            if r["filter"] == fk and r["timeframe"] == tf]
                if matching:
                    r = matching[0]
                    if best_r is None or r["delta_wr"] > best_r["delta_wr"]:
                        best_r = r
            best_per_tf[tf] = best_r

        # Find overall best
        all_for_ind = [r for r in all_results if r["filter"] in filter_keys]
        if not all_for_ind:
            continue

        best_overall = max(all_for_ind, key=lambda x: x["delta_wr"])

        print(f"{ind_name:<20} {best_overall['filter_label']:<30} ", end="")
        for tf in TIMEFRAMES:
            r = best_per_tf.get(tf)
            if r:
                sig_mark = "*" if r["sig_05"] else " "
                sig_mark = "**" if r["sig_bonf"] else sig_mark
                print(f"| {r['delta_wr']:>+5.1%}{sig_mark:<2}", end="")
            else:
                print(f"|    {'--':>5}", end="")

        print(f"| {best_overall['timeframe']:>5} {best_overall['delta_wr']:>+5.1%}")

    # ===================================================================
    # SECTION 4: COMBINATION TESTS
    # ===================================================================
    print("\n\n" + "=" * 90)
    print("SECTION 4: COMBINATION TESTS")
    print("Testing the most promising filter combinations")
    print("=" * 90)

    # Get the top positive-delta filters (skipping trades improves WR)
    top_filters = [r for r in results_sorted if r["delta_wr"] > 0][:10]

    # Pre-defined combos to test
    combo_specs = []

    # Combo 1: RSI(14) 5m > 60 + BB above upper (any TF)
    for tf in TIMEFRAMES:
        combo_specs.append({
            "name": f"RSI(14)_5m>60 + BB_above_upper_{tf}",
            "filters": [
                ("rsi_14_5m", lambda v: v > 60),
                (f"bb_position_{tf}", lambda v: v == 1),
            ],
        })

    # Combo 2: RSI(14) 5m > 60 + MACD bullish (any TF)
    for tf in TIMEFRAMES:
        combo_specs.append({
            "name": f"RSI(14)_5m>60 + MACD_bullish_{tf}",
            "filters": [
                ("rsi_14_5m", lambda v: v > 60),
                (f"macd_signal_{tf}", lambda v: v == 1),
            ],
        })

    # Combo 3: Best single filters combined (top 2)
    if len(top_filters) >= 2:
        for i in range(min(3, len(top_filters))):
            for j in range(i + 1, min(5, len(top_filters))):
                f1 = top_filters[i]
                f2 = top_filters[j]
                # skip if same indicator
                if f1["filter"].split("_")[0] == f2["filter"].split("_")[0]:
                    continue
                filt1 = FILTERS[f1["filter"]]
                filt2 = FILTERS[f2["filter"]]
                combo_specs.append({
                    "name": f"{f1['filter_label']}_{f1['timeframe']} + {f2['filter_label']}_{f2['timeframe']}",
                    "filters": [
                        (f1["indicator_col"], filt1["condition"]),
                        (f2["indicator_col"], filt2["condition"]),
                    ],
                })

    # Combo 4: RSI > 70 5m + Stochastic > 80 (various TFs)
    for tf in TIMEFRAMES:
        combo_specs.append({
            "name": f"RSI(14)_5m>70 + Stoch>80_{tf}",
            "filters": [
                ("rsi_14_5m", lambda v: v > 70),
                (f"stoch_k_{tf}", lambda v: v > 80),
            ],
        })

    # Triple combo: RSI > 60 5m + MACD bullish 5m + BB above upper 5m
    combo_specs.append({
        "name": "RSI>60_5m + MACD_bull_5m + BB_above_5m",
        "filters": [
            ("rsi_14_5m", lambda v: v > 60),
            ("macd_signal_5m", lambda v: v == 1),
            ("bb_position_5m", lambda v: v == 1),
        ],
    })

    # Triple combo: RSI > 60 5m + MACD bullish 1h + Stoch > 80 15m
    combo_specs.append({
        "name": "RSI>60_5m + MACD_bull_1h + Stoch>80_15m",
        "filters": [
            ("rsi_14_5m", lambda v: v > 60),
            ("macd_signal_1h", lambda v: v == 1),
            ("stoch_k_15m", lambda v: v > 80),
        ],
    })

    combo_results = []

    for spec in combo_specs:
        # Apply all filters: skip trade if ANY filter says skip
        valid_mask = pd.Series([True] * len(trades_df), index=trades_df.index)
        skip_mask = pd.Series([False] * len(trades_df), index=trades_df.index)
        all_valid = True

        for col, cond in spec["filters"]:
            if col not in trades_df.columns:
                all_valid = False
                break
            col_valid = trades_df[col].notna()
            valid_mask = valid_mask & col_valid
            col_skip = trades_df[col].apply(
                lambda v: cond(v) if pd.notna(v) else False
            )
            skip_mask = skip_mask | col_skip

        if not all_valid:
            continue

        # Only consider rows where all indicator columns are valid
        combined_skip = skip_mask & valid_mask
        kept = trades_df[valid_mask & ~combined_skip]
        skipped = trades_df[valid_mask & combined_skip]

        if len(kept) < 20:
            continue

        kept_stats = compute_stats(kept)
        delta_wr = kept_stats["wr"] - baseline_stats["wr"]
        delta_daily = kept_stats["daily_pnl"] - baseline_stats["daily_pnl"]

        # Fisher exact
        if len(skipped) >= 5:
            sk_wins = int(skipped["hit"].sum())
            table = [
                [kept_stats["wins"], len(kept) - kept_stats["wins"]],
                [sk_wins, len(skipped) - sk_wins],
            ]
            _, p_fisher = fisher_exact(table, alternative="two-sided")
        else:
            p_fisher = 1.0

        combo_results.append({
            "name": spec["name"],
            "n_kept": len(kept),
            "n_skipped": len(skipped),
            "wr_kept": kept_stats["wr"],
            "wr_skipped": int(skipped["hit"].sum()) / len(skipped) if len(skipped) > 0 else 0,
            "delta_wr": delta_wr,
            "daily_pnl": kept_stats["daily_pnl"],
            "delta_daily": delta_daily,
            "total_pnl": kept_stats["total_pnl"],
            "p_fisher": p_fisher,
        })

    # Sort by daily PnL improvement
    combo_results.sort(key=lambda x: x["delta_daily"], reverse=True)

    print(f"\n{'Rank':>4} {'Combination':<55} {'Kept':>6} {'Skip':>6} "
          f"{'WR':>7} {'dWR':>7} {'DailyPnL':>10} {'p-val':>10}")
    print("-" * 125)

    for rank, cr in enumerate(combo_results[:30], 1):
        sig = ""
        if cr["p_fisher"] < BONFERRONI_ALPHA:
            sig = " BONF***"
        elif cr["p_fisher"] < 0.05:
            sig = " p<.05*"

        print(f"{rank:>4} {cr['name']:<55} "
              f"{cr['n_kept']:>6,} {cr['n_skipped']:>6,} "
              f"{cr['wr_kept']:>6.1%} {cr['delta_wr']:>+6.1%} "
              f"${cr['daily_pnl']:>9.2f} "
              f"{cr['p_fisher']:>10.5f}{sig}")

    # ===================================================================
    # SECTION 5: SKIPPED TRADE ANALYSIS
    # ===================================================================
    print("\n\n" + "=" * 90)
    print("SECTION 5: WHAT HAPPENS TO SKIPPED TRADES?")
    print("(If skipped trades have LOW WR, the filter is working correctly)")
    print("=" * 90)

    # Show WR of skipped trades for top filters
    print(f"\n{'Filter':<35} {'TF':<5} {'Kept_WR':>8} {'Skip_WR':>8} "
          f"{'Skip_N':>7} {'Spread':>8} {'Verdict':>20}")
    print("-" * 100)

    for r in results_by_p[:20]:
        if r["n_skipped"] < 10:
            continue
        spread = r["wr_kept"] - r["wr_skipped"]
        verdict = ""
        if spread > 0.03 and r["p_fisher"] < 0.05:
            verdict = "FILTER WORKS"
        elif spread > 0.01:
            verdict = "Modest effect"
        elif spread < -0.01:
            verdict = "WRONG DIRECTION"
        else:
            verdict = "No clear effect"

        print(f"{r['filter_label']:<35} {r['timeframe']:<5} "
              f"{r['wr_kept']:>7.1%} {r['wr_skipped']:>7.1%} "
              f"{r['n_skipped']:>7,} {spread:>+7.1%} {verdict:>20}")

    # ===================================================================
    # SECTION 6: PER-TIMEFRAME BEST INDICATOR
    # ===================================================================
    print("\n\n" + "=" * 90)
    print("SECTION 6: BEST INDICATOR PER TIMEFRAME")
    print("=" * 90)

    for tf in TIMEFRAMES:
        tf_results = [r for r in all_results if r["timeframe"] == tf]
        if not tf_results:
            continue

        best = max(tf_results, key=lambda x: x["delta_wr"])
        most_sig = min(tf_results, key=lambda x: x["p_fisher"])

        print(f"\n  {tf}:")
        print(f"    Best WR improvement: {best['filter_label']} "
              f"(dWR={best['delta_wr']:+.1%}, p={best['p_fisher']:.5f})")
        print(f"    Most significant:    {most_sig['filter_label']} "
              f"(dWR={most_sig['delta_wr']:+.1%}, p={most_sig['p_fisher']:.5f})")

    # ===================================================================
    # FINAL VERDICT
    # ===================================================================
    print("\n\n" + "=" * 90)
    print("FINAL VERDICT")
    print("=" * 90)

    n_bonf = sum(1 for r in all_results if r["sig_bonf"])
    n_05 = sum(1 for r in all_results if r["sig_05"])
    n_total = len(all_results)

    print(f"\n  Total filter x timeframe tests: {n_total}")
    print(f"  Significant at Bonferroni (p < {BONFERRONI_ALPHA:.4f}): {n_bonf}")
    print(f"  Significant at p < 0.05 (uncorrected): {n_05}")
    print(f"  Expected false positives at p<0.05: ~{n_total * 0.05:.1f}")

    # Summarize each indicator
    print(f"\n  Per-indicator summary:")
    for ind_name, filter_keys in core_indicators.items():
        ind_results = [r for r in all_results if r["filter"] in filter_keys]
        if not ind_results:
            print(f"    {ind_name}: NO DATA")
            continue

        best = max(ind_results, key=lambda x: x["delta_wr"])
        most_sig = min(ind_results, key=lambda x: x["p_fisher"])

        if most_sig["sig_bonf"]:
            verdict = f"SIGNIFICANT (Bonferroni)"
        elif most_sig["sig_05"]:
            verdict = f"Marginal (p<0.05)"
        else:
            verdict = f"NOT significant"

        print(f"    {ind_name}:")
        print(f"      Best: {best['filter_label']} @ {best['timeframe']} "
              f"-> dWR={best['delta_wr']:>+.1%}, dDaily=${best['delta_daily']:>+.2f}")
        print(f"      Most sig: {most_sig['filter_label']} @ {most_sig['timeframe']} "
              f"-> p={most_sig['p_fisher']:.5f} [{verdict}]")

    # Best combination
    if combo_results:
        best_combo = combo_results[0]
        print(f"\n  Best combination:")
        print(f"    {best_combo['name']}")
        print(f"    WR: {best_combo['wr_kept']:.1%} (delta={best_combo['delta_wr']:>+.1%})")
        print(f"    Daily PnL: ${best_combo['daily_pnl']:.2f} "
              f"(delta=${best_combo['delta_daily']:>+.2f})")
        print(f"    p-value: {best_combo['p_fisher']:.5f}")

    print(f"\n  {'='*80}")
    print(f"  INTERPRETATION:")
    print(f"  - Filters that SKIP overbought/bullish signals when betting DOWN")
    print(f"    should improve WR by avoiding counter-trend trades.")
    print(f"  - If NO filter shows Bonferroni significance, the indicators do")
    print(f"    not add value beyond the streak pattern alone.")
    print(f"  - If significance exists at multiple timeframes for the same")
    print(f"    indicator, the effect is robust (not timeframe-dependent).")
    print(f"  {'='*80}")

    print("\nDone.")


if __name__ == "__main__":
    main()
