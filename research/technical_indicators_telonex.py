"""
technical_indicators_telonex.py - Test whether standard technical indicators on
BTC price predict the win rate of UP-streak -> DOWN mean-reversion patterns.

Indicators tested (all computed on Binance BTCUSDT 5m klines):
  1. RSI(14)          - Oversold/Neutral/Overbought
  2. Bollinger Bands  - Below lower / Inside / Above upper
  3. ATR(14)          - Low/Medium/High volatility (percentile terciles)
  4. MACD(12,26,9)    - Bullish (MACD>signal) vs Bearish
  5. Stochastic %K    - Oversold(<20) / Neutral / Overbought(>80)
  6. Volume Ratio     - Current vol / 20-period SMA vol

For each indicator bin we compute:
  - WR of UU->DOWN, UUU->DOWN, UUUU->DOWN
  - N trades, delta vs baseline, Fisher exact p-value
  - Bonferroni correction (alpha = 0.05 / ~30 bins = 0.0017)

Also tests combinations of the top 2-3 most promising individual indicators.

Uses:
  - Telonex dataset: 60,605 markets (59,423 settled), 74 days, 4 coins
  - Binance BTCUSDT 5m klines (Dec 18 2025 - Mar 1 2026)
  - BTC Binance data applied to ALL coins (BTC drives market, r=0.788)

Usage:
    python research/technical_indicators_telonex.py
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
DATA_PATH = Path(__file__).parent.parent / "data" / "telonex_updown_5m.parquet"
KLINE_CACHE_FILE = Path(__file__).parent / "binance_5m_klines_cache.json"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

PATTERNS = {
    "UU->DOWN":   (2, "D"),
    "UUU->DOWN":  (3, "D"),
    "UUUU->DOWN": (4, "D"),
}

# Bonferroni: ~30 bins across all indicators -> alpha = 0.05/30
BONFERRONI_ALPHA = 0.05 / 30  # ~0.00167


# ---------------------------------------------------------------------------
# 1. Load Telonex data (same pattern as regime_vol_telonex.py)
# ---------------------------------------------------------------------------
def load_telonex() -> pd.DataFrame:
    """Load and prepare the Telonex dataset."""
    print(f"Loading {DATA_PATH}...")
    df = pd.read_parquet(DATA_PATH)
    print(f"  Total markets: {len(df):,}")

    # Filter to settled only
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

    # Map result_id to outcome: 0 = UP, 1 = DOWN
    df["outcome"] = df["result_id"].map({"0": "U", "1": "D"})

    # Convert timestamp to datetime
    df["start_dt"] = pd.to_datetime(df["start_ts"], unit="s", utc=True)
    df["hour"] = df["start_dt"].dt.hour

    # Sort by coin, then timestamp
    df = df.sort_values(["coin", "start_ts"]).reset_index(drop=True)

    print(f"\n  Coin distribution:")
    for coin, cnt in df["coin"].value_counts().sort_index().items():
        print(f"    {coin}: {cnt:,} markets")

    ts_min = df["start_ts"].min()
    ts_max = df["start_ts"].max()
    dt_min = datetime.fromtimestamp(ts_min, tz=timezone.utc)
    dt_max = datetime.fromtimestamp(ts_max, tz=timezone.utc)
    print(f"\n  Date range: {dt_min:%Y-%m-%d %H:%M} to {dt_max:%Y-%m-%d %H:%M}")

    return df


# ---------------------------------------------------------------------------
# 2. Fetch Binance 5m klines with caching
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
            print(f"  Binance cache hit: {len(df):,} klines")
            return df

    print(f"  Fetching {symbol} {interval} klines from Binance...")
    all_klines = []
    current_start = start_ms

    while current_start < end_ms:
        url = (
            f"{BINANCE_KLINES_URL}?symbol={symbol}&interval={interval}"
            f"&startTime={current_start}&endTime={end_ms}&limit=1000"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
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
        if len(all_klines) % 5000 == 0:
            print(f"    fetched {len(all_klines):,} klines so far...")
        time_mod.sleep(0.12)  # rate limit

    # Cache
    cache[cache_key] = all_klines
    with open(KLINE_CACHE_FILE, "w") as f:
        json.dump(cache, f)
    print(f"  Total: {len(all_klines):,} klines fetched and cached")

    df = pd.DataFrame(all_klines)
    df["open_time_ms"] = df["open_time"]
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


# ---------------------------------------------------------------------------
# 3. Compute technical indicators on 5m klines
# ---------------------------------------------------------------------------
def compute_indicators(klines: pd.DataFrame) -> pd.DataFrame:
    """Compute all technical indicators on 5m klines."""
    df = klines.copy()
    df = df.sort_values("open_time").reset_index(drop=True)

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    volume = df["volume"].values
    n = len(close)

    # --- RSI(14) ---
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    # Wilder's smoothed RSI
    avg_gain = np.full(n, np.nan)
    avg_loss = np.full(n, np.nan)
    rsi = np.full(n, np.nan)
    period = 14
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
    df["rsi_14"] = rsi

    # --- Bollinger Bands (20, 2) ---
    bb_period = 20
    bb_std = 2.0
    sma_20 = pd.Series(close).rolling(bb_period, min_periods=bb_period).mean().values
    std_20 = pd.Series(close).rolling(bb_period, min_periods=bb_period).std().values
    upper_bb = sma_20 + bb_std * std_20
    lower_bb = sma_20 - bb_std * std_20
    # Position: -1 = below lower, 0 = inside, 1 = above upper
    bb_pos = np.full(n, np.nan)
    for i in range(n):
        if not np.isnan(sma_20[i]):
            if close[i] < lower_bb[i]:
                bb_pos[i] = -1
            elif close[i] > upper_bb[i]:
                bb_pos[i] = 1
            else:
                bb_pos[i] = 0
    df["bb_position"] = bb_pos

    # --- ATR(14) normalized as % of price ---
    tr = np.full(n, np.nan)
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    # Wilder's smoothed ATR
    atr = np.full(n, np.nan)
    atr_period = 14
    if n > atr_period + 1:
        atr[atr_period] = np.nanmean(tr[1:atr_period + 1])
        for i in range(atr_period + 1, n):
            atr[i] = (atr[i - 1] * (atr_period - 1) + tr[i]) / atr_period
    # Normalize as percentage of price
    atr_pct = np.full(n, np.nan)
    for i in range(n):
        if not np.isnan(atr[i]) and close[i] > 0:
            atr_pct[i] = (atr[i] / close[i]) * 100.0
    df["atr_14_pct"] = atr_pct

    # --- MACD (12, 26, 9) ---
    ema_12 = pd.Series(close).ewm(span=12, adjust=False).mean().values
    ema_26 = pd.Series(close).ewm(span=26, adjust=False).mean().values
    macd_line = ema_12 - ema_26
    signal_line = pd.Series(macd_line).ewm(span=9, adjust=False).mean().values
    macd_hist = macd_line - signal_line
    # Need at least 26 periods for MACD to be meaningful
    macd_signal = np.full(n, np.nan)
    for i in range(34, n):  # 26 + 9 - 1 warmup
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
            raw_k[i] = 50.0  # no range
    # Smooth with SMA(3)
    stoch_k = pd.Series(raw_k).rolling(stoch_smooth, min_periods=stoch_smooth).mean().values
    df["stoch_k"] = stoch_k

    # --- Volume Ratio: current volume / 20-period SMA volume ---
    vol_sma_20 = pd.Series(volume).rolling(20, min_periods=20).mean().values
    vol_ratio = np.full(n, np.nan)
    for i in range(n):
        if not np.isnan(vol_sma_20[i]) and vol_sma_20[i] > 0:
            vol_ratio[i] = volume[i] / vol_sma_20[i]
    df["vol_ratio"] = vol_ratio

    # Create open_time_s (seconds) for matching
    df["open_time_s"] = (df["open_time_ms"] / 1000).astype(int)

    return df


# ---------------------------------------------------------------------------
# 4. Match Telonex cycles to Binance klines by timestamp
# ---------------------------------------------------------------------------
def build_indicator_lookup(klines_ind: pd.DataFrame) -> Dict[int, pd.Series]:
    """Build a dict mapping open_time_s -> indicator row for fast lookup."""
    lookup = {}
    for idx, row in klines_ind.iterrows():
        ts = int(row["open_time_s"])
        lookup[ts] = row
    return lookup


def match_telonex_to_indicators(
    telonex_df: pd.DataFrame,
    indicator_lookup: Dict[int, pd.Series],
) -> pd.DataFrame:
    """
    For each Telonex cycle, find the Binance 5m kline at the same start_ts.
    Returns a DataFrame with pattern trade data + indicator values.
    """
    # Build per-coin outcome sequences
    trades_list = []

    for coin in sorted(telonex_df["coin"].unique()):
        coin_df = telonex_df[telonex_df["coin"] == coin].reset_index(drop=True)
        outcomes = coin_df["outcome"].values
        start_ts_arr = coin_df["start_ts"].values.astype(int)
        n = len(outcomes)

        for pat_name, (streak_len, target) in PATTERNS.items():
            streak_char = "U" if target == "D" else "D"

            for i in range(streak_len, n):
                # Check streak
                match = True
                for j in range(streak_len):
                    if outcomes[i - streak_len + j] != streak_char:
                        match = False
                        break
                if not match:
                    continue

                hit = 1 if outcomes[i] == target else 0
                ts = int(start_ts_arr[i])

                # Look up indicator values at this timestamp
                # Try exact match first, then +/- 300s (5 min)
                kline_row = indicator_lookup.get(ts)
                if kline_row is None:
                    # Try nearest within 5 minutes
                    for offset in [-300, 300, -600, 600]:
                        kline_row = indicator_lookup.get(ts + offset)
                        if kline_row is not None:
                            break

                rec = {
                    "coin": coin,
                    "pattern": pat_name,
                    "start_ts": ts,
                    "hit": hit,
                    "rsi_14": np.nan,
                    "bb_position": np.nan,
                    "atr_14_pct": np.nan,
                    "macd_signal": np.nan,
                    "stoch_k": np.nan,
                    "vol_ratio": np.nan,
                }

                if kline_row is not None:
                    for col in ["rsi_14", "bb_position", "atr_14_pct",
                                "macd_signal", "stoch_k", "vol_ratio"]:
                        val = kline_row.get(col, np.nan)
                        if isinstance(val, (int, float, np.integer, np.floating)):
                            if np.isfinite(val):
                                rec[col] = float(val)

                trades_list.append(rec)

        print(f"  {coin}: {n:,} cycles processed")

    return pd.DataFrame(trades_list)


# ---------------------------------------------------------------------------
# 5. Analysis functions
# ---------------------------------------------------------------------------
def analyze_bin(
    df: pd.DataFrame,
    bin_col: str,
    bin_val,
    overall_w: int,
    overall_n: int,
) -> dict:
    """Compute WR stats for a single bin vs everything else."""
    subset = df[df[bin_col] == bin_val]
    n = len(subset)
    if n == 0:
        return None

    w = int(subset["hit"].sum())
    wr = w / n
    overall_wr = overall_w / overall_n
    delta = wr - overall_wr

    # Fisher exact: this bin vs rest
    rest = df[df[bin_col] != bin_val].dropna(subset=[bin_col])
    rest = rest[rest[bin_col].notna()]
    rest_w = int(rest["hit"].sum())
    rest_n = len(rest)

    if rest_n == 0:
        p_fisher = 1.0
    else:
        table = [[w, n - w], [rest_w, rest_n - rest_w]]
        _, p_fisher = fisher_exact(table, alternative="two-sided")

    return {
        "bin": bin_val,
        "n": n,
        "w": w,
        "l": n - w,
        "wr": wr,
        "delta": delta,
        "p_fisher": p_fisher,
        "sig_bonf": p_fisher < BONFERRONI_ALPHA,
        "sig_05": p_fisher < 0.05,
    }


def print_indicator_table(
    title: str,
    results: List[dict],
) -> None:
    """Pretty-print a table of indicator bin results."""
    print(f"\n  {'='*78}")
    print(f"  {title}")
    print(f"  {'='*78}")
    print(f"  {'Bin':<30} {'N':>6} {'W':>5} {'L':>5} {'WR':>7} "
          f"{'Delta':>8} {'p-Fisher':>10} {'Sig?':>10}")
    print(f"  {'-'*30} {'-'*6} {'-'*5} {'-'*5} {'-'*7} "
          f"{'-'*8} {'-'*10} {'-'*10}")

    for r in results:
        if r is None:
            continue
        sig_label = ""
        if r["sig_bonf"]:
            sig_label = "BONF ***"
        elif r["sig_05"]:
            sig_label = "p<0.05 *"
        else:
            sig_label = "  n.s."

        print(f"  {str(r['bin']):<30} {r['n']:>6,} {r['w']:>5,} {r['l']:>5,} "
              f"{r['wr']:>6.1%} {r['delta']:>+7.1%} {r['p_fisher']:>10.5f} "
              f"{sig_label:>10}")


# ---------------------------------------------------------------------------
# 6. Define indicator bins
# ---------------------------------------------------------------------------
def assign_rsi_bins(df: pd.DataFrame) -> pd.DataFrame:
    """Assign RSI bins."""
    df = df.copy()
    # Standard bins
    conditions = [
        df["rsi_14"] < 30,
        (df["rsi_14"] >= 30) & (df["rsi_14"] <= 70),
        df["rsi_14"] > 70,
    ]
    choices = ["a) Oversold (<30)", "b) Neutral (30-70)", "c) Overbought (>70)"]
    df["rsi_bin"] = np.select(conditions, choices, default=None)
    df.loc[df["rsi_14"].isna(), "rsi_bin"] = None

    # Alternative bins: <40 vs >60
    conditions2 = [
        df["rsi_14"] < 40,
        (df["rsi_14"] >= 40) & (df["rsi_14"] <= 60),
        df["rsi_14"] > 60,
    ]
    choices2 = ["a) RSI<40", "b) RSI 40-60", "c) RSI>60"]
    df["rsi_bin_alt"] = np.select(conditions2, choices2, default=None)
    df.loc[df["rsi_14"].isna(), "rsi_bin_alt"] = None

    return df


def assign_bb_bins(df: pd.DataFrame) -> pd.DataFrame:
    """Assign Bollinger Band bins."""
    df = df.copy()
    mapping = {-1: "a) Below lower", 0: "b) Inside bands", 1: "c) Above upper"}
    df["bb_bin"] = df["bb_position"].map(mapping)
    return df


def assign_atr_bins(df: pd.DataFrame) -> pd.DataFrame:
    """Assign ATR bins by percentile terciles."""
    df = df.copy()
    valid = df["atr_14_pct"].dropna()
    if len(valid) < 100:
        df["atr_bin"] = None
        return df
    p33 = valid.quantile(0.333)
    p67 = valid.quantile(0.667)

    def atr_bin(v):
        if pd.isna(v):
            return None
        if v < p33:
            return f"a) Low ATR (<p33={p33:.3f}%)"
        elif v < p67:
            return f"b) Med ATR (p33-p67)"
        else:
            return f"c) High ATR (>p67={p67:.3f}%)"

    df["atr_bin"] = df["atr_14_pct"].apply(atr_bin)
    return df


def assign_macd_bins(df: pd.DataFrame) -> pd.DataFrame:
    """Assign MACD bins."""
    df = df.copy()
    mapping = {1: "a) Bullish (MACD>sig)", -1: "b) Bearish (MACD<sig)"}
    df["macd_bin"] = df["macd_signal"].map(mapping)
    return df


def assign_stoch_bins(df: pd.DataFrame) -> pd.DataFrame:
    """Assign Stochastic %K bins."""
    df = df.copy()
    conditions = [
        df["stoch_k"] < 20,
        (df["stoch_k"] >= 20) & (df["stoch_k"] <= 80),
        df["stoch_k"] > 80,
    ]
    choices = ["a) Oversold (<20)", "b) Neutral (20-80)", "c) Overbought (>80)"]
    df["stoch_bin"] = np.select(conditions, choices, default=None)
    df.loc[df["stoch_k"].isna(), "stoch_bin"] = None
    return df


def assign_vol_ratio_bins(df: pd.DataFrame) -> pd.DataFrame:
    """Assign volume ratio bins."""
    df = df.copy()
    conditions = [
        df["vol_ratio"] < 0.5,
        (df["vol_ratio"] >= 0.5) & (df["vol_ratio"] <= 1.5),
        df["vol_ratio"] > 1.5,
    ]
    choices = ["a) Low vol (<0.5x)", "b) Normal (0.5-1.5x)", "c) High vol (>1.5x)"]
    df["vol_ratio_bin"] = np.select(conditions, choices, default=None)
    df.loc[df["vol_ratio"].isna(), "vol_ratio_bin"] = None
    return df


# ---------------------------------------------------------------------------
# 7. Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 80)
    print("TECHNICAL INDICATORS vs MEAN-REVERSION PATTERN WIN RATE")
    print("Testing 6 indicators on 59,423 settled Telonex markets")
    print("Bonferroni alpha = 0.05/30 = {:.4f}".format(BONFERRONI_ALPHA))
    print("=" * 80)

    # ----- Load Telonex -----
    tdf = load_telonex()

    # ----- Fetch Binance 5m klines -----
    ts_min = tdf["start_ts"].min()
    ts_max = tdf["start_ts"].max()
    # Extra 200 periods (1000 min) warmup before for indicator computation
    warmup_s = 200 * 5 * 60  # 200 x 5min candles
    start_ms = int((ts_min - warmup_s) * 1000)
    end_ms = int((ts_max + 600) * 1000)

    print(f"\n  Fetching Binance BTCUSDT 5m klines...")
    klines = fetch_binance_klines("BTCUSDT", "5m", start_ms, end_ms)
    print(f"  Binance 5m klines: {len(klines):,} rows")

    # ----- Compute indicators -----
    print(f"\n  Computing technical indicators...")
    klines_ind = compute_indicators(klines)

    # Report indicator coverage
    for col in ["rsi_14", "bb_position", "atr_14_pct", "macd_signal", "stoch_k", "vol_ratio"]:
        valid = klines_ind[col].notna().sum()
        print(f"    {col}: {valid:,} / {len(klines_ind):,} valid ({valid/len(klines_ind):.1%})")

    # ----- Build indicator lookup -----
    print(f"\n  Building timestamp lookup...")
    indicator_lookup = build_indicator_lookup(klines_ind)
    print(f"  Lookup size: {len(indicator_lookup):,} timestamps")

    # ----- Match Telonex to indicators -----
    print(f"\n  Matching Telonex cycles to Binance indicators...")
    trades_df = match_telonex_to_indicators(tdf, indicator_lookup)
    print(f"\n  Total pattern trades: {len(trades_df):,}")

    # Report match rates
    for col in ["rsi_14", "bb_position", "atr_14_pct", "macd_signal", "stoch_k", "vol_ratio"]:
        matched = trades_df[col].notna().sum()
        print(f"    {col} matched: {matched:,} / {len(trades_df):,} "
              f"({matched/len(trades_df):.1%})")

    # ----- Assign all bins -----
    trades_df = assign_rsi_bins(trades_df)
    trades_df = assign_bb_bins(trades_df)
    trades_df = assign_atr_bins(trades_df)
    trades_df = assign_macd_bins(trades_df)
    trades_df = assign_stoch_bins(trades_df)
    trades_df = assign_vol_ratio_bins(trades_df)

    # ===================================================================
    # INDIVIDUAL INDICATOR ANALYSIS
    # ===================================================================
    print("\n\n" + "=" * 80)
    print("SECTION 1: INDIVIDUAL INDICATOR ANALYSIS")
    print("=" * 80)

    # Track all results for combination analysis
    all_bin_results = []

    indicators = [
        ("RSI(14) - Standard Bins", "rsi_bin"),
        ("RSI(14) - Alternative Bins (<40 / 40-60 / >60)", "rsi_bin_alt"),
        ("Bollinger Band Position (20,2)", "bb_bin"),
        ("ATR(14) % - Tercile Bins", "atr_bin"),
        ("MACD(12,26,9) Signal", "macd_bin"),
        ("Stochastic %K(14,3)", "stoch_bin"),
        ("Volume Ratio (vol/SMA20)", "vol_ratio_bin"),
    ]

    for pat_name in PATTERNS:
        pat_df = trades_df[trades_df["pattern"] == pat_name].copy()
        n_total = len(pat_df)
        w_total = int(pat_df["hit"].sum())
        wr_total = w_total / n_total if n_total > 0 else 0

        print(f"\n{'#'*80}")
        print(f"  PATTERN: {pat_name}")
        print(f"  Baseline: {n_total:,} trades | {w_total:,}W / {n_total-w_total:,}L | "
              f"WR = {wr_total:.4f} ({wr_total:.1%})")
        print(f"{'#'*80}")

        for ind_name, bin_col in indicators:
            # Get unique valid bins
            valid_df = pat_df[pat_df[bin_col].notna() & (pat_df[bin_col] != "None")]
            if len(valid_df) < 50:
                print(f"\n  --- {ind_name}: insufficient data ({len(valid_df)} trades) ---")
                continue

            bins = sorted(valid_df[bin_col].unique())
            results = []
            for b in bins:
                r = analyze_bin(valid_df, bin_col, b, w_total, n_total)
                if r is not None:
                    r["indicator"] = ind_name
                    r["pattern"] = pat_name
                    all_bin_results.append(r)
                    results.append(r)

            print_indicator_table(f"{ind_name} | {pat_name}", results)

    # ===================================================================
    # SUMMARY: MOST PROMISING INDIVIDUAL INDICATORS
    # ===================================================================
    print("\n\n" + "=" * 80)
    print("SECTION 2: RANKING OF ALL INDICATOR BINS BY P-VALUE")
    print("=" * 80)

    # Sort by p-value
    sorted_results = sorted(all_bin_results, key=lambda x: x["p_fisher"])

    print(f"\n  {'Rank':<5} {'Pattern':<14} {'Indicator':<40} {'Bin':<30} "
          f"{'N':>6} {'WR':>7} {'Delta':>8} {'p-Fisher':>10} {'Sig':>10}")
    print(f"  {'-'*5} {'-'*14} {'-'*40} {'-'*30} "
          f"{'-'*6} {'-'*7} {'-'*8} {'-'*10} {'-'*10}")

    for rank, r in enumerate(sorted_results[:30], 1):
        sig = "BONF***" if r["sig_bonf"] else ("p<.05*" if r["sig_05"] else "n.s.")
        print(f"  {rank:<5} {r['pattern']:<14} {r['indicator']:<40} {str(r['bin']):<30} "
              f"{r['n']:>6,} {r['wr']:>6.1%} {r['delta']:>+7.1%} "
              f"{r['p_fisher']:>10.5f} {sig:>10}")

    # Count significant results
    n_bonf = sum(1 for r in all_bin_results if r["sig_bonf"])
    n_05 = sum(1 for r in all_bin_results if r["sig_05"])
    n_total_bins = len(all_bin_results)

    print(f"\n  Total bins tested: {n_total_bins}")
    print(f"  Significant at Bonferroni (p < {BONFERRONI_ALPHA:.4f}): {n_bonf}")
    print(f"  Significant at p < 0.05 (uncorrected): {n_05}")
    print(f"  Expected false positives at p<0.05: ~{n_total_bins * 0.05:.1f}")

    # ===================================================================
    # SECTION 3: COMBINATION TESTS
    # ===================================================================
    print("\n\n" + "=" * 80)
    print("SECTION 3: COMBINATION TESTS")
    print("Combining top 2-3 most promising individual indicators")
    print("=" * 80)

    from itertools import combinations

    # Test TWO directions: positive (WR-boosting) and negative (WR-killing)
    for direction_label, direction_filter in [
        ("POSITIVE (WR-boosting: when to BET DOWN)", lambda r: r["delta"] > 0),
        ("NEGATIVE (WR-killing: when to AVOID DOWN)", lambda r: r["delta"] < 0),
    ]:
        print(f"\n  {'*'*70}")
        print(f"  {direction_label}")
        print(f"  {'*'*70}")

        for pat_name in ["UU->DOWN", "UUU->DOWN", "UUUU->DOWN"]:
            # Get interesting bins in the desired direction
            pat_interesting = [r for r in sorted_results
                               if r["p_fisher"] < 0.10
                               and r["pattern"] == pat_name
                               and direction_filter(r)]
            if len(pat_interesting) < 2:
                continue

            pat_df = trades_df[trades_df["pattern"] == pat_name].copy()
            n_total = len(pat_df)
            w_total = int(pat_df["hit"].sum())
            wr_total = w_total / n_total

            # Get unique indicator bin_cols for this pattern (up to 4)
            seen_indicators = set()
            top_filters = []
            for r in pat_interesting:
                ind_name = r["indicator"]
                if ind_name not in seen_indicators and len(top_filters) < 4:
                    for iname, bcol in indicators:
                        if iname == ind_name:
                            top_filters.append({
                                "indicator": ind_name,
                                "bin_col": bcol,
                                "bin_val": r["bin"],
                                "wr": r["wr"],
                                "delta": r["delta"],
                                "p": r["p_fisher"],
                            })
                            seen_indicators.add(ind_name)
                            break

            if len(top_filters) < 2:
                continue

            print(f"\n  {'='*70}")
            print(f"  COMBINATIONS for {pat_name} (baseline WR={wr_total:.4f}, N={n_total:,})")
            print(f"  {'='*70}")

            print(f"\n  Individual filters being combined:")
            for f in top_filters:
                print(f"    - {f['indicator']}: bin={f['bin_val']} "
                      f"(WR={f['wr']:.1%}, delta={f['delta']:+.1%}, p={f['p']:.4f})")

            combo_results = []

            for combo_size in [2, 3]:
                if len(top_filters) < combo_size:
                    continue

                for combo in combinations(range(len(top_filters)), combo_size):
                    mask = pd.Series([True] * len(pat_df), index=pat_df.index)
                    label_parts = []
                    for idx in combo:
                        f = top_filters[idx]
                        col_mask = pat_df[f["bin_col"]] == f["bin_val"]
                        mask = mask & col_mask
                        label_parts.append(f"{f['bin_val']}")

                    subset = pat_df[mask]
                    n_sub = len(subset)
                    if n_sub < 10:
                        continue
                    w_sub = int(subset["hit"].sum())
                    wr_sub = w_sub / n_sub
                    delta_sub = wr_sub - wr_total

                    rest = pat_df[~mask]
                    rest_w = int(rest["hit"].sum())
                    rest_n = len(rest)
                    if rest_n > 0:
                        table = [[w_sub, n_sub - w_sub], [rest_w, rest_n - rest_w]]
                        _, p_fish = fisher_exact(table, alternative="two-sided")
                    else:
                        p_fish = 1.0

                    combo_label = " + ".join(label_parts)
                    sig = "BONF***" if p_fish < BONFERRONI_ALPHA else (
                        "p<.05*" if p_fish < 0.05 else "n.s.")

                    combo_results.append({
                        "combo": combo_label,
                        "n": n_sub,
                        "w": w_sub,
                        "wr": wr_sub,
                        "delta": delta_sub,
                        "p_fisher": p_fish,
                        "sig": sig,
                    })

            if combo_results:
                combo_results.sort(key=lambda x: x["p_fisher"])
                print(f"\n  {'Combination':<60} {'N':>6} {'WR':>7} "
                      f"{'Delta':>8} {'p-Fish':>10} {'Sig':>10}")
                print(f"  {'-'*60} {'-'*6} {'-'*7} {'-'*8} {'-'*10} {'-'*10}")
                for cr in combo_results:
                    print(f"  {cr['combo']:<60} {cr['n']:>6,} "
                          f"{cr['wr']:>6.1%} {cr['delta']:>+7.1%} "
                          f"{cr['p_fisher']:>10.5f} {cr['sig']:>10}")

    # ===================================================================
    # FINAL VERDICT
    # ===================================================================
    print("\n\n" + "=" * 80)
    print("FINAL VERDICT")
    print("=" * 80)

    # Summarize each indicator
    ind_verdicts = {}
    for ind_name, bin_col in indicators:
        # Collect all results for this indicator across all patterns
        ind_results = [r for r in all_bin_results if r["indicator"] == ind_name]
        if not ind_results:
            ind_verdicts[ind_name] = "NO DATA"
            continue

        best = min(ind_results, key=lambda x: x["p_fisher"])
        if best["sig_bonf"]:
            ind_verdicts[ind_name] = f"SIGNIFICANT (Bonferroni): best p={best['p_fisher']:.5f}, " \
                                      f"bin={best['bin']}, WR={best['wr']:.1%}, " \
                                      f"delta={best['delta']:+.1%} [{best['pattern']}]"
        elif best["sig_05"]:
            ind_verdicts[ind_name] = f"Marginal (p<0.05 uncorrected): best p={best['p_fisher']:.5f}, " \
                                      f"bin={best['bin']}, WR={best['wr']:.1%}, " \
                                      f"delta={best['delta']:+.1%} [{best['pattern']}]"
        else:
            ind_verdicts[ind_name] = f"NOT significant: best p={best['p_fisher']:.5f} [{best['pattern']}]"

    for ind_name, verdict in ind_verdicts.items():
        print(f"\n  {ind_name}:")
        print(f"    {verdict}")

    # Overall
    print(f"\n\n  {'='*70}")
    print(f"  OVERALL:")
    if n_bonf > 0:
        print(f"  {n_bonf} indicator bins survived Bonferroni correction.")
        print(f"  These may represent genuine predictive signals worth investigating.")
    else:
        print(f"  ZERO indicator bins survived Bonferroni correction.")
        print(f"  Standard technical indicators on BTC do NOT predict")
        print(f"  mean-reversion pattern outcomes in Polymarket 5m Up/Down markets.")
        if n_05 > 0:
            print(f"  ({n_05} bins showed p<0.05 uncorrected, but this is expected")
            print(f"  by chance alone given {n_total_bins} tests.)")
    print(f"  {'='*70}")

    # ===================================================================
    # CRITICAL INTERPRETATION
    # ===================================================================
    print("\n\n" + "=" * 80)
    print("CRITICAL INTERPRETATION: Is this a real edge or just trend-following?")
    print("=" * 80)

    print("""
  The massive significance of RSI, Bollinger Bands, MACD, and Stochastic is
  NOT a surprise -- it reflects a TAUTOLOGICAL relationship:

  - We bet DOWN after UP-streaks (mean reversion).
  - When RSI is oversold / price below lower BB, BTC has already been FALLING.
  - A falling BTC means the next 5-min candle is more likely DOWN.
  - So of course the DOWN bet wins more when BTC is already falling!

  Conversely:
  - When RSI is overbought / price above upper BB, BTC is RISING.
  - A rising BTC means the next 5-min candle is more likely UP.
  - So the DOWN bet loses -- the streak CONTINUES rather than reverting.

  KEY QUESTION: Does combining indicators with the UP-streak pattern beat
  simply trading the indicator alone (without requiring a streak)?

  If RSI < 30 predicts DOWN regardless of streak, then the streak pattern
  adds no value -- we should just trade RSI directly.
  """)

    # Test: RSI < 30 -> next outcome DOWN (without any streak requirement)
    print(f"  Testing: Do indicators predict DOWN *without* streak requirement?")
    print(f"  (If yes, the streak pattern is irrelevant -- indicators do all the work)")
    print()

    # Build all-cycles data (not just pattern trades)
    all_cycles_list = []
    for coin in sorted(tdf["coin"].unique()):
        coin_df = tdf[tdf["coin"] == coin].reset_index(drop=True)
        for _, row in coin_df.iterrows():
            ts = int(row["start_ts"])
            hit_d = 1 if row["outcome"] == "D" else 0
            kline_row = indicator_lookup.get(ts)
            if kline_row is None:
                for offset in [-300, 300, -600, 600]:
                    kline_row = indicator_lookup.get(ts + offset)
                    if kline_row is not None:
                        break
            rsi_val = np.nan
            bb_val = np.nan
            macd_val = np.nan
            if kline_row is not None:
                for col_name, var_ref in [("rsi_14", "rsi_val"), ("bb_position", "bb_val"),
                                           ("macd_signal", "macd_val")]:
                    v = kline_row.get(col_name, np.nan)
                    if isinstance(v, (int, float, np.integer, np.floating)) and np.isfinite(v):
                        if col_name == "rsi_14":
                            rsi_val = float(v)
                        elif col_name == "bb_position":
                            bb_val = float(v)
                        elif col_name == "macd_signal":
                            macd_val = float(v)
            all_cycles_list.append({
                "coin": coin, "hit_down": hit_d,
                "rsi_14": rsi_val, "bb_position": bb_val, "macd_signal": macd_val,
            })

    all_cycles_df = pd.DataFrame(all_cycles_list)
    base_down_rate = all_cycles_df["hit_down"].mean()
    base_n = len(all_cycles_df)
    print(f"  Baseline DOWN rate (all cycles, no streak): {base_down_rate:.4f} "
          f"({base_down_rate:.1%}), N={base_n:,}")

    # RSI < 30 -> DOWN rate (no streak)
    for label, mask_fn in [
        ("RSI < 30 (no streak)", lambda df: df["rsi_14"] < 30),
        ("RSI < 40 (no streak)", lambda df: df["rsi_14"] < 40),
        ("BB below lower (no streak)", lambda df: df["bb_position"] == -1),
        ("MACD bearish (no streak)", lambda df: df["macd_signal"] == -1),
        ("RSI > 70 (no streak)", lambda df: df["rsi_14"] > 70),
        ("BB above upper (no streak)", lambda df: df["bb_position"] == 1),
    ]:
        valid = all_cycles_df[all_cycles_df["rsi_14"].notna() | all_cycles_df["bb_position"].notna()]
        sub = all_cycles_df[mask_fn(all_cycles_df)]
        n_sub = len(sub)
        if n_sub < 20:
            continue
        dr = sub["hit_down"].mean()
        rest = all_cycles_df[~mask_fn(all_cycles_df)]
        rest = rest[rest.notna().all(axis=1)]
        rest_dr = rest["hit_down"].mean()
        rest_n = len(rest)

        table = [[int(sub["hit_down"].sum()), n_sub - int(sub["hit_down"].sum())],
                 [int(rest["hit_down"].sum()), rest_n - int(rest["hit_down"].sum())]]
        _, p_f = fisher_exact(table, alternative="two-sided")

        print(f"  {label:<35} DOWN rate={dr:.1%} (N={n_sub:,})  "
              f"delta={dr - base_down_rate:+.1%}  p={p_f:.5f}")

    # Compare: UU + RSI<30 vs just RSI<30
    print(f"\n  Comparing: streak + indicator vs indicator alone:")
    uu_df = trades_df[trades_df["pattern"] == "UU->DOWN"]
    uu_rsi_low = uu_df[uu_df["rsi_14"] < 30]
    all_rsi_low = all_cycles_df[all_cycles_df["rsi_14"] < 30]

    if len(uu_rsi_low) > 10 and len(all_rsi_low) > 10:
        wr_uu_rsi = uu_rsi_low["hit"].mean()
        dr_rsi = all_rsi_low["hit_down"].mean()
        print(f"  RSI<30 alone:          DOWN rate = {dr_rsi:.1%} (N={len(all_rsi_low):,})")
        print(f"  UU->DOWN + RSI<30:     WR = {wr_uu_rsi:.1%} (N={len(uu_rsi_low):,})")
        print(f"  Streak adds: {wr_uu_rsi - dr_rsi:+.1%}")

    uu_bb_low = uu_df[uu_df["bb_position"] == -1]
    all_bb_low = all_cycles_df[all_cycles_df["bb_position"] == -1]
    if len(uu_bb_low) > 10 and len(all_bb_low) > 10:
        wr_uu_bb = uu_bb_low["hit"].mean()
        dr_bb = all_bb_low["hit_down"].mean()
        print(f"  BB<lower alone:        DOWN rate = {dr_bb:.1%} (N={len(all_bb_low):,})")
        print(f"  UU->DOWN + BB<lower:   WR = {wr_uu_bb:.1%} (N={len(uu_bb_low):,})")
        print(f"  Streak adds: {wr_uu_bb - dr_bb:+.1%}")

    print(f"\n  {'='*70}")
    print(f"  BOTTOM LINE:")
    print(f"  If indicators predict DOWN equally well WITHOUT the streak, then the")
    print(f"  mean-reversion pattern is just piggybacking on the BTC trend signal.")
    print(f"  The actionable insight would be: FILTER trades by RSI/BB to avoid")
    print(f"  betting DOWN during strong uptrends (RSI>70, above upper BB).")
    print(f"  {'='*70}")

    print("\nDone.")


if __name__ == "__main__":
    main()
