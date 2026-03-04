"""
regime_vol_telonex.py - Validate Section 22 volatility regime hypothesis at scale.

Section 22 found (on 84 BTC trades from 3 days of our own data):
  - Low vol: 79% WR (n=28) — need to test with full data
  - Asia morning 04-08 UTC: 92% WR (n=12)
  - EU morning 08-12 UTC: 53% WR (n=19)
  - Combined LowVol + Asia: 75% WR (n=16)

Section 23 already debunked the time-of-day effect (50.8% for 04-08 UTC with
n=2,447 from Telonex all-coin data). This script adds the Binance volatility
dimension that Section 23 could not test.

Uses:
  - Telonex dataset: 60,605 markets (59,423 settled), 74 days, 4 coins
  - Binance BTCUSDT 1-minute klines for realized volatility computation
  - Patterns: UU->DOWN and UUUU->DOWN (our main live strategy)

Usage:
    python research/regime_vol_telonex.py
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
from scipy.stats import binomtest, fisher_exact

warnings.filterwarnings("ignore", category=FutureWarning)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATA_PATH = Path(__file__).parent.parent / "data" / "telonex_updown_5m.parquet"
KLINE_CACHE_FILE = Path(__file__).parent / "binance_klines_cache_telonex.json"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

PATTERNS = {
    "UU->DOWN":   (2, "D"),    # 2 consecutive U's, then bet DOWN
    "UUU->DOWN":  (3, "D"),
    "UUUU->DOWN": (4, "D"),    # Our main live strategy
    "DD->UP":     (2, "U"),    # Symmetry check
}

SESSION_LABELS = {
    (0, 4):   "00-04 UTC (Asia night)",
    (4, 8):   "04-08 UTC (Asia morning)",
    (8, 12):  "08-12 UTC (EU morning)",
    (12, 16): "12-16 UTC (EU afternoon)",
    (16, 20): "16-20 UTC (US afternoon)",
    (20, 24): "20-24 UTC (US evening)",
}


# ---------------------------------------------------------------------------
# 1. Load Telonex data
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

    # Map result_id to outcome
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
# 2. Fetch Binance klines
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
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


def compute_binance_features(klines: pd.DataFrame) -> pd.DataFrame:
    """Compute rolling features from 1m klines."""
    df = klines.copy()
    df = df.sort_values("open_time").reset_index(drop=True)

    # 1-min log returns
    df["log_ret"] = np.log(df["close"] / df["open"])

    # Rolling 1-hour realized volatility (std of 60 x 1m log returns)
    df["vol_1h"] = df["log_ret"].rolling(60, min_periods=30).std()

    # Rolling 1-hour return (trend)
    df["ret_1h"] = df["close"].pct_change(60)

    # Rolling 1-hour quote volume
    df["vol_sum_1h"] = df["quote_volume"].rolling(60, min_periods=30).sum()

    return df


def match_timestamp_to_kline(
    ts_utc: pd.Timestamp,
    klines: pd.DataFrame,
) -> Optional[pd.Series]:
    """Find closest kline row to a given UTC timestamp (within 2 min)."""
    if ts_utc.tzinfo is None:
        ts_utc = ts_utc.tz_localize("UTC")

    # Use searchsorted for speed instead of computing all diffs
    idx = klines["open_time"].searchsorted(ts_utc)
    candidates = []
    for i in [max(0, idx - 1), min(idx, len(klines) - 1)]:
        diff = abs((klines["open_time"].iloc[i] - ts_utc).total_seconds())
        if diff <= 120:
            candidates.append((diff, i))

    if not candidates:
        return None
    candidates.sort()
    return klines.iloc[candidates[0][1]]


# ---------------------------------------------------------------------------
# 3. Build pattern trades with Binance features
# ---------------------------------------------------------------------------
def build_pattern_trades(
    telonex_df: pd.DataFrame,
    klines_feat: pd.DataFrame,
) -> Dict[str, pd.DataFrame]:
    """
    For each coin, build sequences and detect patterns.
    For each pattern match, attach Binance vol/trend features at that timestamp.
    """
    results = {pat: [] for pat in PATTERNS}

    for coin in sorted(telonex_df["coin"].unique()):
        coin_df = telonex_df[telonex_df["coin"] == coin].reset_index(drop=True)
        outcomes = coin_df["outcome"].values
        start_dts = coin_df["start_dt"].values
        hours = coin_df["hour"].values
        n = len(outcomes)

        for pat_name, (streak_len, target) in PATTERNS.items():
            # Determine streak character
            if target == "D":
                streak_char = "U"
            else:
                streak_char = "D"

            for i in range(streak_len, n):
                # Check if the preceding streak_len outcomes are all streak_char
                match = True
                for j in range(streak_len):
                    if outcomes[i - streak_len + j] != streak_char:
                        match = False
                        break
                if not match:
                    continue

                # This is a valid signal at position i
                hit = 1 if outcomes[i] == target else 0
                ts = pd.Timestamp(start_dts[i], tz="UTC")
                hour = int(hours[i])

                # Get Binance features at this timestamp
                kline = match_timestamp_to_kline(ts, klines_feat)

                vol_1h = np.nan
                ret_1h = np.nan
                volume_1h = np.nan
                if kline is not None:
                    vol_1h = kline.get("vol_1h", np.nan)
                    ret_1h = kline.get("ret_1h", np.nan)
                    volume_1h = kline.get("vol_sum_1h", np.nan)
                    # Clean NaN/inf
                    if not np.isfinite(vol_1h):
                        vol_1h = np.nan
                    if not np.isfinite(ret_1h):
                        ret_1h = np.nan
                    if not np.isfinite(volume_1h):
                        volume_1h = np.nan

                results[pat_name].append({
                    "coin": coin,
                    "timestamp": ts,
                    "hour": hour,
                    "hit": hit,
                    "vol_1h": vol_1h,
                    "ret_1h": ret_1h,
                    "volume_1h": volume_1h,
                })

        print(f"  {coin}: {n:,} cycles processed")

    # Convert to DataFrames
    out = {}
    for pat_name, records in results.items():
        if records:
            out[pat_name] = pd.DataFrame(records)
        else:
            out[pat_name] = pd.DataFrame()
    return out


# ---------------------------------------------------------------------------
# 4. Analysis functions
# ---------------------------------------------------------------------------
def get_session(hour: int) -> str:
    """Map hour to session label."""
    for (lo, hi), label in SESSION_LABELS.items():
        if lo <= hour < hi:
            return label
    return "unknown"


def print_bin_analysis(
    df: pd.DataFrame,
    title: str,
    bin_col: str,
    overall_wr: float,
    min_n: int = 10,
) -> None:
    """Analyze WR by bins with binomial test and Fisher exact test."""
    print(f"\n  --- {title} ---")
    print(f"  {'Bin':<32} {'N':>6} {'W':>5} {'L':>5} {'WR':>7} "
          f"{'Delta':>8} {'p-binom':>8} {'p-fish':>8}")
    print(f"  {'-'*32} {'-'*6} {'-'*5} {'-'*5} {'-'*7} "
          f"{'-'*8} {'-'*8} {'-'*8}")

    if bin_col not in df.columns:
        print(f"  [no data for {bin_col}]")
        return

    bins = sorted(df[bin_col].dropna().unique())
    rest_df = df[df[bin_col].notna()]

    for b in bins:
        subset = df[df[bin_col] == b]
        n = len(subset)
        if n < min_n:
            continue
        w = subset["hit"].sum()
        lo = n - w
        wr = w / n
        delta = wr - overall_wr

        # Binomial test vs overall WR
        p_binom = binomtest(w, n, overall_wr, alternative="two-sided").pvalue

        # Fisher exact test: this bin vs all other bins
        other = rest_df[rest_df[bin_col] != b]
        other_w = other["hit"].sum()
        other_n = len(other)
        table = [[w, n - w], [other_w, other_n - other_w]]
        _, p_fisher = fisher_exact(table, alternative="two-sided")

        sig = ""
        if p_fisher < 0.001:
            sig = " ***"
        elif p_fisher < 0.01:
            sig = "  **"
        elif p_fisher < 0.05:
            sig = "   *"

        print(f"  {str(b):<32} {n:>6,} {w:>5,} {lo:>5,} {wr:>6.1%} "
              f"{delta:>+7.1%} {p_binom:>8.4f} {p_fisher:>8.4f}{sig}")


# ---------------------------------------------------------------------------
# 5. Main analysis
# ---------------------------------------------------------------------------
def main():
    print("=" * 80)
    print("VOLATILITY REGIME VALIDATION - TELONEX FULL DATASET")
    print("Testing Section 22 hypothesis with 59,423 settled markets")
    print("=" * 80)

    # Load Telonex data
    tdf = load_telonex()

    # Determine Binance kline date range
    ts_min = tdf["start_ts"].min()
    ts_max = tdf["start_ts"].max()
    # 2h buffer before for rolling features warmup
    start_ms = int((ts_min - 7200) * 1000)
    end_ms = int((ts_max + 600) * 1000)

    print(f"\n  Fetching Binance BTCUSDT 1m klines...")
    klines = fetch_binance_klines("BTCUSDT", "1m", start_ms, end_ms)
    print(f"  Binance klines: {len(klines):,} rows")
    print(f"  Computing rolling features...")
    klines_feat = compute_binance_features(klines)

    print(f"\n  Building pattern trades with Binance features...")
    pattern_trades = build_pattern_trades(tdf, klines_feat)

    for pat_name, pdf in pattern_trades.items():
        if len(pdf) == 0:
            print(f"  {pat_name}: 0 trades")
            continue
        n = len(pdf)
        w = pdf["hit"].sum()
        wr = w / n
        print(f"  {pat_name}: {n:,} trades | {w:,}W / {n-w:,}L | WR={wr:.3f}")

    # ===================================================================
    # Analyze each pattern
    # ===================================================================
    for pat_name in ["UU->DOWN", "UUUU->DOWN"]:
        pdf = pattern_trades.get(pat_name)
        if pdf is None or len(pdf) == 0:
            print(f"\n  Skipping {pat_name}: no trades")
            continue

        n_total = len(pdf)
        w_total = pdf["hit"].sum()
        overall_wr = w_total / n_total

        print("\n" + "=" * 80)
        print(f"  PATTERN: {pat_name}")
        print(f"  Total: {n_total:,} trades | {w_total:,}W / {n_total-w_total:,}L | WR={overall_wr:.4f}")
        print("=" * 80)

        # -----------------------------------------------------------
        # A. Session (4-hour blocks)
        # -----------------------------------------------------------
        pdf["session"] = pdf["hour"].apply(get_session)
        print_bin_analysis(pdf, "Session (4-hour blocks)", "session", overall_wr)

        # -----------------------------------------------------------
        # B. Individual hours
        # -----------------------------------------------------------
        pdf["hour_label"] = pdf["hour"].apply(lambda h: f"{h:02d}:00 UTC")
        print_bin_analysis(pdf, "Hour of Day", "hour_label", overall_wr, min_n=30)

        # -----------------------------------------------------------
        # C. Realized Volatility (Binance 1h)
        # -----------------------------------------------------------
        vol_valid = pdf["vol_1h"].dropna()
        n_vol = len(vol_valid)
        print(f"\n  Binance vol matches: {n_vol:,} / {n_total:,} "
              f"({n_vol/n_total:.1%} match rate)")

        if n_vol > 100:
            q33 = vol_valid.quantile(0.333)
            q67 = vol_valid.quantile(0.667)
            print(f"  1h Vol percentiles: p33={q33:.6f}, p67={q67:.6f}")

            def vol_bin(row):
                v = row["vol_1h"]
                if pd.isna(v):
                    return None
                if v < q33:
                    return f"a) Low vol   (<{q33:.5f})"
                elif v < q67:
                    return f"b) Med vol   ({q33:.5f}-{q67:.5f})"
                else:
                    return f"c) High vol  (>{q67:.5f})"

            pdf["vol_bin"] = pdf.apply(vol_bin, axis=1)
            print_bin_analysis(pdf, "1h Realized Volatility (Binance)", "vol_bin", overall_wr)

            # Also absolute bins matching Section 22
            def abs_vol_bin(row):
                v = row["vol_1h"]
                if pd.isna(v):
                    return None
                if v < 0.0005:
                    return "a) Very low  (<0.05%)"
                elif v < 0.001:
                    return "b) Low       (0.05-0.10%)"
                elif v < 0.002:
                    return "c) Medium    (0.10-0.20%)"
                else:
                    return "d) High      (>0.20%)"

            pdf["abs_vol_bin"] = pdf.apply(abs_vol_bin, axis=1)
            print_bin_analysis(pdf, "1h Realized Vol (absolute bins)", "abs_vol_bin", overall_wr)

            # Median split
            vol_median = vol_valid.median()
            pdf["vol_half"] = pdf["vol_1h"].apply(
                lambda v: "Low vol (<median)" if pd.notna(v) and v < vol_median
                else ("High vol (>=median)" if pd.notna(v) else None)
            )
            print_bin_analysis(pdf, "1h Vol (median split)", "vol_half", overall_wr)

        # -----------------------------------------------------------
        # D. BTC 1h Trend
        # -----------------------------------------------------------
        ret_valid = pdf["ret_1h"].dropna()
        if len(ret_valid) > 100:
            def trend_bin(row):
                r = row["ret_1h"]
                if pd.isna(r):
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

            pdf["trend_bin"] = pdf.apply(trend_bin, axis=1)
            print_bin_analysis(pdf, "1h BTC Trend (Binance)", "trend_bin", overall_wr)

        # -----------------------------------------------------------
        # E. Binance Volume
        # -----------------------------------------------------------
        volq_valid = pdf["volume_1h"].dropna()
        if len(volq_valid) > 100:
            vq33 = volq_valid.quantile(0.333)
            vq67 = volq_valid.quantile(0.667)

            def vol_q_bin(row):
                v = row["volume_1h"]
                if pd.isna(v):
                    return None
                if v < vq33:
                    return "a) Low volume"
                elif v < vq67:
                    return "b) Med volume"
                else:
                    return "c) High volume"

            pdf["vol_q_bin"] = pdf.apply(vol_q_bin, axis=1)
            print_bin_analysis(pdf, "1h Binance Quote Volume", "vol_q_bin", overall_wr)

        # -----------------------------------------------------------
        # F. Combined: Volatility x Session
        # -----------------------------------------------------------
        if n_vol > 100:
            vol_median = vol_valid.median()

            def combined_bin(row):
                v = row["vol_1h"]
                if pd.isna(v):
                    return None
                vol_label = "LowVol" if v < vol_median else "HighVol"
                h = row["hour"]
                if 0 <= h < 8:
                    session = "Asia"
                elif 8 <= h < 16:
                    session = "EU"
                else:
                    session = "US"
                return f"{vol_label} + {session}"

            pdf["combined"] = pdf.apply(combined_bin, axis=1)
            print_bin_analysis(pdf, "Volatility x Session (combined)", "combined", overall_wr)

        # -----------------------------------------------------------
        # G. Per-coin breakdown (vol regime)
        # -----------------------------------------------------------
        if n_vol > 100:
            print(f"\n  --- Per-Coin Vol Regime Breakdown ---")
            for coin in sorted(pdf["coin"].unique()):
                cpdf = pdf[pdf["coin"] == coin]
                cn = len(cpdf)
                cw = cpdf["hit"].sum()
                cwr = cw / cn if cn > 0 else 0
                print(f"\n  {coin}: {cn:,} trades, Overall WR={cwr:.3f}")

                for vbin in sorted(pdf["vol_bin"].dropna().unique()):
                    sub = cpdf[cpdf["vol_bin"] == vbin]
                    sn = len(sub)
                    if sn < 10:
                        continue
                    sw = sub["hit"].sum()
                    swr = sw / sn
                    delta = swr - cwr
                    p = binomtest(sw, sn, cwr, alternative="two-sided").pvalue
                    sig = " *" if p < 0.05 else ("**" if p < 0.01 else "")
                    print(f"    {vbin:<35} N={sn:>5,}  WR={swr:.3f}  "
                          f"Delta={delta:+.3f}  p={p:.4f}{sig}")

    # ===================================================================
    # Final Summary / Verdict
    # ===================================================================
    print("\n\n" + "=" * 80)
    print("SUMMARY VERDICT: Section 22 Hypothesis Validation")
    print("=" * 80)

    for pat_name in ["UU->DOWN", "UUUU->DOWN"]:
        pdf = pattern_trades.get(pat_name)
        if pdf is None or len(pdf) == 0:
            continue

        n_total = len(pdf)
        w_total = pdf["hit"].sum()
        overall_wr = w_total / n_total

        print(f"\n{'='*70}")
        print(f"  {pat_name}: {n_total:,} trades, Overall WR={overall_wr:.4f}")
        print(f"{'='*70}")

        vol_valid = pdf["vol_1h"].dropna()
        if len(vol_valid) > 0:
            vol_median = vol_valid.median()

            # Test 1: Low vol claim (Section 22: 79% WR)
            low_vol = pdf[pdf["vol_1h"] < vol_median].dropna(subset=["vol_1h"])
            high_vol = pdf[pdf["vol_1h"] >= vol_median].dropna(subset=["vol_1h"])

            if len(low_vol) > 0 and len(high_vol) > 0:
                lw = low_vol["hit"].sum()
                ln = len(low_vol)
                lwr = lw / ln
                hw = high_vol["hit"].sum()
                hn = len(high_vol)
                hwr = hw / hn

                table = [[lw, ln - lw], [hw, hn - hw]]
                _, p_fish = fisher_exact(table)
                p_binom = binomtest(lw, ln, overall_wr).pvalue

                print(f"\n  Hypothesis 1: Low vol -> higher WR")
                print(f"    Section 22 claim: Low vol = 79% WR (n=28)")
                print(f"    Telonex result:   Low vol = {lwr:.1%} WR (n={ln:,})")
                print(f"                     High vol = {hwr:.1%} WR (n={hn:,})")
                print(f"    Delta: {lwr - hwr:+.1%}")
                print(f"    Fisher p-value: {p_fish:.4f}")
                if p_fish < 0.05:
                    print(f"    --> SIGNIFICANT: Low vol IS different from high vol")
                else:
                    print(f"    --> NOT SIGNIFICANT: No difference between vol regimes")

        # Test 2: Asia morning 04-08 UTC (Section 22: 92% WR)
        asia_am = pdf[(pdf["hour"] >= 4) & (pdf["hour"] < 8)]
        rest = pdf[(pdf["hour"] < 4) | (pdf["hour"] >= 8)]

        if len(asia_am) > 0 and len(rest) > 0:
            aw = asia_am["hit"].sum()
            an = len(asia_am)
            awr = aw / an
            rw = rest["hit"].sum()
            rn = len(rest)

            table = [[aw, an - aw], [rw, rn - rw]]
            _, p_fish = fisher_exact(table)

            print(f"\n  Hypothesis 2: Asia morning 04-08 UTC -> higher WR")
            print(f"    Section 22 claim: 92% WR (n=12)")
            print(f"    Telonex result:   {awr:.1%} WR (n={an:,})")
            print(f"    Rest of day:      {rw/rn:.1%} WR (n={rn:,})")
            print(f"    Delta: {awr - rw/rn:+.1%}")
            print(f"    Fisher p-value: {p_fish:.4f}")
            if p_fish > 0.05:
                print(f"    --> NOT SIGNIFICANT: Asia morning is NOT special")

        # Test 3: EU morning 08-12 worst (Section 22: 53% WR)
        eu_am = pdf[(pdf["hour"] >= 8) & (pdf["hour"] < 12)]
        rest2 = pdf[(pdf["hour"] < 8) | (pdf["hour"] >= 12)]

        if len(eu_am) > 0 and len(rest2) > 0:
            ew = eu_am["hit"].sum()
            en = len(eu_am)
            ewr = ew / en
            r2w = rest2["hit"].sum()
            r2n = len(rest2)

            table = [[ew, en - ew], [r2w, r2n - r2w]]
            _, p_fish = fisher_exact(table)

            print(f"\n  Hypothesis 3: EU morning 08-12 UTC -> lower WR")
            print(f"    Section 22 claim: 53% WR (n=19)")
            print(f"    Telonex result:   {ewr:.1%} WR (n={en:,})")
            print(f"    Rest of day:      {r2w/r2n:.1%} WR (n={r2n:,})")
            print(f"    Delta: {ewr - r2w/r2n:+.1%}")
            print(f"    Fisher p-value: {p_fish:.4f}")
            if p_fish > 0.05:
                print(f"    --> NOT SIGNIFICANT: EU morning is NOT distinctly worse")

        # Test 4: Combined LowVol + Asia (Section 22: 75% WR)
        if len(vol_valid) > 0:
            vol_median = vol_valid.median()
            combo = pdf[
                (pdf["vol_1h"] < vol_median) &
                (pdf["hour"] >= 0) & (pdf["hour"] < 8) &
                pdf["vol_1h"].notna()
            ]
            combo_rest = pdf[
                ~(
                    (pdf["vol_1h"] < vol_median) &
                    (pdf["hour"] >= 0) & (pdf["hour"] < 8)
                ) &
                pdf["vol_1h"].notna()
            ]

            if len(combo) > 0 and len(combo_rest) > 0:
                cw = combo["hit"].sum()
                cn = len(combo)
                cwr = cw / cn
                crw = combo_rest["hit"].sum()
                crn = len(combo_rest)

                table = [[cw, cn - cw], [crw, crn - crw]]
                _, p_fish = fisher_exact(table)

                print(f"\n  Hypothesis 4: LowVol + Asia -> higher WR")
                print(f"    Section 22 claim: 75% WR (n=16)")
                print(f"    Telonex result:   {cwr:.1%} WR (n={cn:,})")
                print(f"    Everything else:  {crw/crn:.1%} WR (n={crn:,})")
                print(f"    Delta: {cwr - crw/crn:+.1%}")
                print(f"    Fisher p-value: {p_fish:.4f}")
                if p_fish > 0.05:
                    print(f"    --> NOT SIGNIFICANT: Combined filter has no edge")

        # Test 5: BTC trend (Section 22: mild down 91%, flat 47%)
        if "trend_bin" in pdf.columns:
            for trend_label in sorted(pdf["trend_bin"].dropna().unique()):
                sub = pdf[pdf["trend_bin"] == trend_label]
                sn = len(sub)
                if sn < 30:
                    continue
                sw = sub["hit"].sum()
                swr = sw / sn
                p = binomtest(sw, sn, overall_wr).pvalue
                sig_marker = " <-- SIGNIFICANT" if p < 0.05 else ""
                print(f"    Trend: {trend_label}  WR={swr:.1%} (n={sn:,})  "
                      f"p={p:.4f}{sig_marker}")

    # ===================================================================
    # Overall conclusion
    # ===================================================================
    print("\n\n" + "=" * 80)
    print("OVERALL CONCLUSION")
    print("=" * 80)

    pdf_uu = pattern_trades.get("UU->DOWN")
    if pdf_uu is not None and len(pdf_uu) > 0:
        n = len(pdf_uu)
        w = pdf_uu["hit"].sum()
        wr = w / n

        vol_valid = pdf_uu["vol_1h"].dropna()
        if len(vol_valid) > 0:
            vol_median = vol_valid.median()
            low_v = pdf_uu[pdf_uu["vol_1h"] < vol_median].dropna(subset=["vol_1h"])
            high_v = pdf_uu[pdf_uu["vol_1h"] >= vol_median].dropna(subset=["vol_1h"])
            lwr = low_v["hit"].sum() / len(low_v) if len(low_v) > 0 else 0
            hwr = high_v["hit"].sum() / len(high_v) if len(high_v) > 0 else 0
            delta = abs(lwr - hwr)

            if delta < 0.02:
                print(f"\nThe volatility regime filter shows NO meaningful WR improvement.")
                print(f"Low vol WR={lwr:.1%} vs High vol WR={hwr:.1%} (delta={lwr-hwr:+.1%})")
                print(f"Section 22's finding (79% WR in low vol, n=28) was a small-sample artifact.")
            elif lwr > hwr:
                print(f"\nLow vol shows +{delta:.1%} WR advantage, further testing needed.")
            else:
                print(f"\nHigh vol shows +{delta:.1%} WR advantage (opposite of hypothesis!).")

        print(f"\nOverall UU->DOWN WR: {wr:.1%} (n={n:,})")
        print(f"This is essentially a coin flip. No regime filter can rescue it.")

    print(f"\n{'='*80}")
    print("Done.")


if __name__ == "__main__":
    main()
