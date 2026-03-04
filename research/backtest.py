"""
backtest.py — One-command backtester with equity curve comparison.

Two data modes:
  --csv   : Use your own scraped 1-second CSVs (data/prices_*.csv)
            Real entry prices, real max_ask filter, real outcomes.
  (default): Use Telonex 74-day parquet (outcomes only, assumes entry at max_ask)

Usage:
    # Preset comparison mode (multiple configs on one chart)
    python research/backtest.py --csv                      # CSV mode, default preset
    python research/backtest.py --csv --preset rsi7        # CSV mode, RSI sweep
    python research/backtest.py --csv --preset maxask      # CSV mode, max_ask sweep
    python research/backtest.py --csv --preset indicators  # CSV mode, all indicators
    python research/backtest.py --csv --preset all         # CSV mode, everything

    python research/backtest.py                            # Telonex mode (74 days)
    python research/backtest.py --preset all               # Telonex, everything

    python research/backtest.py --custom                   # custom config (edit top of file)
    python research/backtest.py --list                     # list presets

    # Single-pattern backtest mode (max_ask sweep + per-coin + weekly stability)
    python research/backtest.py --pattern UUU              # All coins, auto side (DOWN)
    python research/backtest.py --pattern UUU --coin ETH   # ETH only
    python research/backtest.py --pattern DDD              # Auto side: bet UP
    python research/backtest.py --pattern UUU --max-ask 0.49  # Fixed max_ask + RSI comparison
    python research/backtest.py --pattern DUDUDD --csv     # CSV data
"""

import json
import sys
import time as time_mod
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    HAS_MPL = True
except ImportError:
    HAS_MPL = False
    print("WARNING: pip install matplotlib  (equity curve will be skipped)")

warnings.filterwarnings("ignore")

# ============================================================================
# CUSTOM CONFIG — edit this section to test your own strategy
# ============================================================================
CUSTOM_LABEL = "Top 7 Bonferroni (no filter)"

CUSTOM_RULES = [
    # --- Longest patterns first (first match wins) ---
    # #2: DUDUDD->UP ALL (57.7% WR, N=958, bonf_p=0.0012)
    {
        "pattern": "DUDUDD",
        "side": "UP",
        "coins": ["BTC", "ETH", "SOL", "XRP"],
        "max_ask": 0.51,
    },
    # #13: DDDDD->UP ETH (60.3% WR, N=292, p=0.000266)
    {"pattern": "DDDDD", "side": "UP", "coins": ["ETH"], "max_ask": 0.51},
    # #1: DDDD->UP ETH (58.3% WR, N=701, bonf_p=0.0070)
    {"pattern": "DDDD", "side": "UP", "coins": ["ETH"], "max_ask": 0.51},
    # #6: UUUU->DOWN ETH (57.4% WR, N=751, bonf_p=0.036)
    {"pattern": "UUUU", "side": "DOWN", "coins": ["ETH"], "max_ask": 0.51},
    # #6: UUUU->DOWN others (54.5% WR)
    {
        "pattern": "UUUU",
        "side": "DOWN",
        "coins": ["BTC", "SOL", "XRP"],
        "max_ask": 0.51,
    },
    # #3: UUU->DOWN ETH (55.8% WR, N=1698, bonf_p=0.0013)
    {"pattern": "UUU", "side": "DOWN", "coins": ["ETH"], "max_ask": 0.51},
    # #5: UUU->DOWN others (54.0% WR, N=5373)
    {"pattern": "UUU", "side": "DOWN", "coins": ["BTC", "SOL", "XRP"], "max_ask": 0.51},
    # #4: DDD->UP ETH (55.6% WR, N=1580, bonf_p=0.0051)
    {"pattern": "DDD", "side": "UP", "coins": ["ETH"], "max_ask": 0.51},
]

CUSTOM_FILTERS = []  # No filters — RSI proven useless after look-ahead fix

CUSTOM_SIZE = 5.0
CUSTOM_FEE = 0.0  # 0% maker on Builder Program
# ============================================================================
# END CUSTOM CONFIG
# ============================================================================

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
TELONEX_FILE = DATA_DIR / "telonex_updown_5m.parquet"
KLINE_CACHE = SCRIPT_DIR / "binance_klines_cache_bt.json"
OUTPUT_DIR = SCRIPT_DIR / "pngs"
COINS = ["BTC", "ETH", "SOL", "XRP"]

# Default rules (from mean_reversion.yaml — must stay in sync!)
DEFAULT_RULES = [
    {"pattern": "UUUUU", "side": "DOWN", "coins": ["ETH"], "max_ask": 0.54},
    {
        "pattern": "UUUUU",
        "side": "DOWN",
        "coins": ["BTC", "SOL", "XRP"],
        "max_ask": 0.51,
    },
    {"pattern": "UUUU", "side": "DOWN", "coins": ["ETH"], "max_ask": 0.54},
    {
        "pattern": "UUUU",
        "side": "DOWN",
        "coins": ["BTC", "SOL", "XRP"],
        "max_ask": 0.51,
    },
    {"pattern": "UDUU", "side": "DOWN", "coins": ["BTC"], "max_ask": 0.50},
    {
        "pattern": "UUU",
        "side": "DOWN",
        "coins": ["BTC", "ETH", "SOL", "XRP"],
        "max_ask": 0.49,
    },
]


# ---------------------------------------------------------------------------
# Technical indicators
# ---------------------------------------------------------------------------
def compute_rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """Compute RSI — identical algorithm to live _compute_rsi() in mean_reversion.py.

    Uses np.diff (no prepend) so delta has length n-1.  First seed uses
    mean of first `period` deltas, then Wilder smoothing from there.
    Returns array of length n; indices < period are NaN.
    """
    n = len(close)
    rsi = np.full(n, np.nan)
    if n < period + 1:
        return rsi
    delta = np.diff(close)  # length n-1
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    # Seed: mean of first `period` deltas (indices 0..period-1)
    avg_gain = np.mean(gain[:period])
    avg_loss = np.mean(loss[:period])
    # Wilder smoothing from index `period` onward
    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
    # To build the full array, re-run and store at each step.
    # Index mapping: delta[j] corresponds to close[j+1], so RSI at close[j+1]
    # is computed from deltas up to j.  The first valid RSI is at close[period].
    avg_g = np.mean(gain[:period])
    avg_l = np.mean(loss[:period])
    if avg_l == 0:
        rsi[period] = 100.0
    else:
        rsi[period] = 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    for i in range(period, len(delta)):
        avg_g = (avg_g * (period - 1) + gain[i]) / period
        avg_l = (avg_l * (period - 1) + loss[i]) / period
        if avg_l == 0:
            rsi[i + 1] = 100.0
        else:
            rsi[i + 1] = 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return rsi


def compute_bb(close: np.ndarray, period: int = 20, nstd: float = 2.0):
    sma = pd.Series(close).rolling(period, min_periods=period).mean().values
    std = pd.Series(close).rolling(period, min_periods=period).std().values
    return sma, sma + nstd * std, sma - nstd * std


def compute_macd(close: np.ndarray, fast=12, slow=26, signal=9):
    ef = pd.Series(close).ewm(span=fast, adjust=False).mean().values
    es = pd.Series(close).ewm(span=slow, adjust=False).mean().values
    ml = ef - es
    sl = pd.Series(ml).ewm(span=signal, adjust=False).mean().values
    hist = ml - sl
    warmup = slow + signal - 1
    hist[:warmup] = np.nan
    return hist


def compute_stoch(close, high, low, period=14, smooth=3):
    n = len(close)
    raw = np.full(n, np.nan)
    for i in range(period - 1, n):
        hh = np.max(high[i - period + 1 : i + 1])
        ll = np.min(low[i - period + 1 : i + 1])
        raw[i] = ((close[i] - ll) / (hh - ll)) * 100 if hh != ll else 50.0
    return pd.Series(raw).rolling(smooth, min_periods=smooth).mean().values


def compute_vol_ratio(volume, period=20):
    sma = pd.Series(volume).rolling(period, min_periods=period).mean().values
    r = np.full(len(volume), np.nan)
    for i in range(len(volume)):
        if not np.isnan(sma[i]) and sma[i] > 0:
            r[i] = volume[i] / sma[i]
    return r


# ---------------------------------------------------------------------------
# Binance kline fetcher with cache
# ---------------------------------------------------------------------------
def fetch_klines(
    symbol: str, interval: str, start_ms: int, end_ms: int
) -> pd.DataFrame:
    cache = {}
    key = f"{symbol}_{interval}_{start_ms}_{end_ms}"
    if KLINE_CACHE.exists():
        with open(KLINE_CACHE) as f:
            cache = json.load(f)
        if key in cache:
            df = pd.DataFrame(cache[key])
            df["open_time_ms"] = df["open_time"]
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            return df

    print(f"  Downloading {symbol} {interval} from Binance...")
    url_base = "https://api.binance.com/api/v3/klines"
    import urllib.request

    rows = []
    cur = start_ms
    while cur < end_ms:
        url = f"{url_base}?symbol={symbol}&interval={interval}&startTime={cur}&endTime={end_ms}&limit=1000"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            print(f"  WARNING: {e}")
            break
        if not data:
            break
        for k in data:
            rows.append(
                {
                    "open_time": k[0],
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                }
            )
        cur = data[-1][6] + 1
        if len(rows) % 5000 < 1000:
            print(f"    {len(rows):,} klines...")
        time_mod.sleep(0.12)
    if not rows:
        return pd.DataFrame()
    cache[key] = rows
    with open(KLINE_CACHE, "w") as f:
        json.dump(cache, f)
    print(f"  Cached {len(rows):,} klines")
    df = pd.DataFrame(rows)
    df["open_time_ms"] = df["open_time"]
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


# ---------------------------------------------------------------------------
# CSV loader (real 1-second scraped data)
# ---------------------------------------------------------------------------
ENTRY_WINDOW = (5, 8)  # seconds into cycle for entry price (data starts at t=5)
RESOLUTION_CACHE = SCRIPT_DIR / "resolution_cache.json"
GAMMA_API = "https://gamma-api.polymarket.com/markets/slug"
COIN_SLUG = {"BTC": "btc", "ETH": "eth", "SOL": "sol", "XRP": "xrp"}
API_DELAY = 0.1  # seconds between Gamma API calls


def load_csvs() -> pd.DataFrame:
    """Load all data/prices_*.csv files."""
    import glob

    files = sorted(glob.glob(str(DATA_DIR / "prices_*.csv")))
    if not files:
        print("  ERROR: No CSV files found in data/prices_*.csv")
        sys.exit(1)
    frames = []
    for f in files:
        df = pd.read_csv(f, parse_dates=["timestamp", "cycle_start"])
        frames.append(df)
        print(
            f"    {Path(f).name}: {len(df):,} rows, {df['cycle_start'].nunique()} cycles"
        )
    return (
        pd.concat(frames, ignore_index=True)
        .sort_values("timestamp")
        .reset_index(drop=True)
    )


def _load_resolution_cache() -> Dict[str, str]:
    if RESOLUTION_CACHE.exists():
        with open(RESOLUTION_CACHE) as f:
            return json.load(f)
    return {}


def _save_resolution_cache(cache: Dict[str, str]):
    with open(RESOLUTION_CACHE, "w") as f:
        json.dump(cache, f, indent=2)


def _resolve_slug_gamma(slug: str) -> Optional[str]:
    """Call Gamma API for one market slug. Returns 'UP', 'DOWN', or None."""
    import urllib.request

    url = f"{GAMMA_API}/{slug}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if not data.get("closed"):
            return None
        outcomes = json.loads(data.get("outcomes", "[]"))
        prices = json.loads(data.get("outcomePrices", "[]"))
        for i, p in enumerate(prices):
            if p == "1":
                return outcomes[i].upper()  # "Up" -> "UP", "Down" -> "DOWN"
        return None
    except Exception:
        return None


def csv_build_cycles(raw: pd.DataFrame):
    """Build per-coin outcome sequences + entry prices from CSV data.

    Outcomes are resolved via Gamma API (100% ground truth), cached locally.
    Entry prices are real DOWN asks at t=5-8s of each cycle.
    """
    cycle_groups = list(raw.groupby("cycle_start"))
    cycle_groups.sort(key=lambda x: x[0])

    cache = _load_resolution_cache()
    coin_data = {
        c: {"outcomes": [], "dates": [], "start_ts": [], "entry_ask": []} for c in COINS
    }

    api_calls = 0
    cache_hits = 0
    n_resolved = 0
    n_unknown = 0

    for idx, (cycle_start, g) in enumerate(cycle_groups):
        if len(g) < 10:
            continue

        cycle_ts = int(pd.Timestamp(cycle_start).timestamp())
        cycle_date = pd.Timestamp(cycle_start).date()

        # Entry prices from CSV: t=5-8
        entry = g[
            (g["seconds_elapsed"] >= ENTRY_WINDOW[0])
            & (g["seconds_elapsed"] <= ENTRY_WINDOW[1])
        ]

        for coin in COINS:
            cl = coin.lower()
            slug = f"{COIN_SLUG[coin]}-updown-5m-{cycle_ts}"

            # --- Resolve outcome via Gamma API (cached) ---
            if slug in cache:
                val = cache[slug]
                cache_hits += 1
            else:
                val = _resolve_slug_gamma(slug)
                cache[slug] = val if val else "UNKNOWN"
                api_calls += 1
                if api_calls % 100 == 0:
                    _save_resolution_cache(cache)
                    if api_calls % 100 == 0:
                        print(
                            f"    {api_calls} API calls, {idx + 1}/{len(cycle_groups)} cycles..."
                        )
                time_mod.sleep(API_DELAY)

            if val in ("UP", "Up"):
                outcome = "U"
                n_resolved += 1
            elif val in ("DOWN", "Down"):
                outcome = "D"
                n_resolved += 1
            else:
                outcome = None
                n_unknown += 1

            # --- Entry price: DOWN ask = 1 - UP bid ---
            entry_ask = np.nan
            if len(entry) >= 1:
                up_bid_col = f"{cl}_up_bid"
                avg_up_bid = entry[up_bid_col].mean()
                down_ask = 1.0 - avg_up_bid
                if 0.01 < down_ask < 0.99:
                    entry_ask = round(down_ask, 4)

            coin_data[coin]["outcomes"].append(outcome)
            coin_data[coin]["dates"].append(cycle_date)
            coin_data[coin]["start_ts"].append(cycle_ts)
            coin_data[coin]["entry_ask"].append(entry_ask)

    _save_resolution_cache(cache)
    total = n_resolved + n_unknown
    print(f"  Gamma API: {cache_hits} cached, {api_calls} new calls")
    print(
        f"  Outcomes: {n_resolved:,} resolved, {n_unknown:,} unknown "
        f"({n_resolved / total * 100:.0f}% resolved)"
        if total
        else ""
    )

    seqs = {}
    for coin in COINS:
        d = coin_data[coin]
        seqs[coin] = {
            "outcomes": d["outcomes"],
            "dates": d["dates"],
            "start_ts": d["start_ts"],
            "entry_ask": d["entry_ask"],
            "n": len(d["outcomes"]),
        }
    return seqs


def find_trades_csv(seqs, rules, size=5.0, fee=0.0):
    """Find trades using CSV data with REAL entry prices.

    Key differences from Telonex mode:
    - Uses actual DOWN ask at t=5-8 as entry price (not max_ask)
    - Applies max_ask filter against real price
    - PnL uses real entry price (price improvement if ask < max_ask)
    - None outcomes break pattern chains
    """
    trades = []
    for coin, d in seqs.items():
        outcomes = d["outcomes"]
        dates = d["dates"]
        ts_arr = d["start_ts"]
        asks = d["entry_ask"]
        n = d["n"]

        for i in range(1, n):
            if outcomes[i] is None:
                continue  # can't resolve this cycle

            for rule in rules:
                if coin not in rule["coins"]:
                    continue
                pat = list(rule["pattern"])
                pl = len(pat)
                if i < pl:
                    continue

                # Check pattern: all prior outcomes must be non-None and match
                match = True
                for j in range(pl):
                    prev = outcomes[i - pl + j]
                    if prev is None or prev != pat[j]:
                        match = False
                        break
                if not match:
                    continue

                # Get real entry price
                entry_ask = asks[i]
                if np.isnan(entry_ask):
                    break  # no price data, skip

                # max_ask filter: would we place the order?
                if entry_ask > rule["max_ask"]:
                    break  # too expensive, skip (first matching rule, so break)

                # Trade executes at real entry_ask (price improvement!)
                side = rule["side"]
                actual = outcomes[i]
                hit = (side == "DOWN" and actual == "D") or (
                    side == "UP" and actual == "U"
                )

                if hit:
                    pnl = (1.0 - entry_ask) * size - fee * size
                else:
                    pnl = -entry_ask * size - fee * size

                trades.append(
                    {
                        "coin": coin,
                        "date": dates[i],
                        "start_ts": int(ts_arr[i]),
                        "pattern": rule["pattern"],
                        "side": side,
                        "max_ask": rule["max_ask"],
                        "entry_ask": entry_ask,
                        "outcome": actual,
                        "hit": hit,
                        "pnl": pnl,
                    }
                )
                break  # first matching rule wins

    if not trades:
        return pd.DataFrame()
    df = pd.DataFrame(trades).sort_values("start_ts").reset_index(drop=True)
    # Assign cycle_idx: unique cycle timestamps in order
    unique_ts = sorted(df["start_ts"].unique())
    ts_to_idx = {ts: idx for idx, ts in enumerate(unique_ts)}
    df["cycle_idx"] = df["start_ts"].map(ts_to_idx)
    return df


# ---------------------------------------------------------------------------
# Telonex loader
# ---------------------------------------------------------------------------
def load_telonex():
    df = pd.read_parquet(TELONEX_FILE)
    df = df[df["result_id"].isin(["0", "1"])].copy()
    df["coin"] = df["slug"].str.extract(r"^(\w+)-updown-5m-")[0].str.upper()
    df["start_ts"] = df["slug"].str.extract(r"-(\d+)$")[0].astype(float).astype(int)
    df["outcome"] = df["result_id"].map({"0": "U", "1": "D"})
    df["start_dt"] = pd.to_datetime(df["start_ts"], unit="s", utc=True)
    df["date"] = df["start_dt"].dt.date
    df = df.sort_values(["coin", "start_ts"]).reset_index(drop=True)
    return df


def build_sequences(tdf):
    seqs = {}
    for coin in COINS:
        c = tdf[tdf["coin"] == coin].sort_values("start_ts").reset_index(drop=True)
        seqs[coin] = {
            "outcomes": c["outcome"].values,
            "dates": c["date"].values,
            "start_ts": c["start_ts"].values,
            "n": len(c),
        }
    return seqs


# ---------------------------------------------------------------------------
# Pattern matching engine
# ---------------------------------------------------------------------------
def find_trades(seqs, rules, size=5.0, fee=0.0):
    trades = []
    for coin, d in seqs.items():
        out, dates, ts, n = d["outcomes"], d["dates"], d["start_ts"], d["n"]
        for i in range(1, n):
            for rule in rules:
                if coin not in rule["coins"]:
                    continue
                pat = list(rule["pattern"])
                pl = len(pat)
                if i < pl:
                    continue
                if any(out[i - pl + j] != pat[j] for j in range(pl)):
                    continue
                side, ma = rule["side"], rule["max_ask"]
                actual = out[i]
                hit = (side == "DOWN" and actual == "D") or (
                    side == "UP" and actual == "U"
                )
                pnl = (
                    ((1.0 - ma) * size - fee * size)
                    if hit
                    else (-ma * size - fee * size)
                )
                trades.append(
                    {
                        "coin": coin,
                        "date": dates[i],
                        "start_ts": int(ts[i]),
                        "pattern": rule["pattern"],
                        "side": side,
                        "max_ask": ma,
                        "outcome": actual,
                        "hit": hit,
                        "pnl": pnl,
                    }
                )
                break
    if not trades:
        return pd.DataFrame()
    df = pd.DataFrame(trades).sort_values("start_ts").reset_index(drop=True)
    unique_ts = sorted(df["start_ts"].unique())
    ts_to_idx = {ts: idx for idx, ts in enumerate(unique_ts)}
    df["cycle_idx"] = df["start_ts"].map(ts_to_idx)
    return df


# ---------------------------------------------------------------------------
# Indicator filter engine
# ---------------------------------------------------------------------------
class IndicatorEngine:
    """Fetches klines, computes indicators, and provides fast lookup."""

    def __init__(self, ts_min: int, ts_max: int):
        self.ts_min = ts_min
        self.ts_max = ts_max
        self._lookups: Dict[str, Dict[int, dict]] = {}  # tf -> {ts_s -> {col: val}}
        self._klines: Dict[str, pd.DataFrame] = {}  # tf -> raw klines df
        self._computed: Dict[str, set] = {}  # tf -> set of computed column names

    def _fetch_klines(self, tf: str) -> pd.DataFrame:
        if tf in self._klines:
            return self._klines[tf]
        warmup_s = 200 * 300  # 200 candles * 5min
        start_ms = int((self.ts_min - warmup_s) * 1000)
        end_ms = int((self.ts_max + 600) * 1000)
        kl = fetch_klines("BTCUSDT", tf, start_ms, end_ms)
        if not kl.empty:
            kl = kl.sort_values("open_time").reset_index(drop=True)
            kl["ts_s"] = (kl["open_time_ms"] / 1000).astype(int)
        self._klines[tf] = kl
        return kl

    def ensure_timeframe(self, tf: str, filters: List[dict]):
        kl = self._fetch_klines(tf)
        if kl.empty:
            self._lookups[tf] = {}
            return

        if tf not in self._computed:
            self._computed[tf] = set()

        close = kl["close"].values
        high = kl["high"].values
        low = kl["low"].values
        vol = kl["volume"].values
        tf_filters = [f for f in filters if f.get("timeframe", "5m") == tf]
        new_cols = False

        for filt in tf_filters:
            col = _col_name(filt)
            if col in self._computed[tf]:
                continue  # already computed
            ind = filt["indicator"]
            p = filt.get("period")
            if ind == "RSI":
                kl[col] = compute_rsi(close, p or 14)
            elif ind == "BB_UPPER":
                _, upper, _ = compute_bb(close, p or 20)
                kl[col] = close - upper
            elif ind == "BB_LOWER":
                _, _, lower = compute_bb(close, p or 20)
                kl[col] = close - lower
            elif ind == "MACD_HIST":
                kl[col] = compute_macd(close)
            elif ind == "STOCH_K":
                kl[col] = compute_stoch(close, high, low, p or 14)
            elif ind == "VOL_RATIO":
                kl[col] = compute_vol_ratio(vol)
            self._computed[tf].add(col)
            new_cols = True

        if new_cols or tf not in self._lookups:
            # Rebuild lookup with all columns
            lookup = {}
            for _, row in kl.iterrows():
                lookup[int(row["ts_s"])] = row.to_dict()
            self._lookups[tf] = lookup

    # Timeframe string -> seconds mapping for kline boundary snapping
    _TF_SECONDS = {
        "1m": 60,
        "3m": 180,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "4h": 14400,
        "1d": 86400,
    }

    def get_value(self, tf: str, ts: int, col: str) -> float:
        """Look up indicator value for the most recent COMPLETED candle.

        Snaps the query timestamp to the nearest kline boundary, then
        checks the current and one prior candle.  This is more robust
        than fixed offsets when cycle timestamps don't exactly align
        with Binance kline open times.
        """
        lk = self._lookups.get(tf, {})
        if not lk:
            return np.nan
        tf_s = self._TF_SECONDS.get(tf, 300)
        # Snap to the kline boundary at or before this timestamp.
        # The candle at `snapped` opens at T and covers [T, T+tf_s) — it is
        # NOT yet complete at trade decision time.  The last COMPLETED candle
        # opened at snapped - tf_s and closed at snapped.
        snapped = (ts // tf_s) * tf_s
        last_completed = snapped - tf_s
        for candidate in [last_completed, last_completed - tf_s]:
            row = lk.get(candidate)
            if row is not None:
                v = row.get(col, np.nan)
                if isinstance(v, (int, float, np.integer, np.floating)) and np.isfinite(
                    v
                ):
                    return float(v)
        return np.nan


def _col_name(f):
    ind = f["indicator"]
    p = f.get("period")
    if ind == "RSI":
        return f"RSI_{p or 14}"
    if ind == "BB_UPPER":
        return f"BB_UPPER_{p or 20}"
    if ind == "BB_LOWER":
        return f"BB_LOWER_{p or 20}"
    if ind == "MACD_HIST":
        return "MACD_HIST"
    if ind == "STOCH_K":
        return f"STOCH_K_{p or 14}"
    if ind == "VOL_RATIO":
        return "VOL_RATIO"
    return ind


def apply_filters(trades_df, filters, engine):
    if trades_df.empty or not filters:
        return trades_df.copy()
    for tf in set(f.get("timeframe", "5m") for f in filters):
        engine.ensure_timeframe(tf, filters)
    df = trades_df.copy()
    df["_pass"] = True
    for filt in filters:
        tf = filt.get("timeframe", "5m")
        col = _col_name(filt)
        vals = df["start_ts"].apply(lambda ts: engine.get_value(tf, ts, col))
        if filt["condition"] == "<":
            df["_pass"] = df["_pass"] & (vals < filt["threshold"])
        else:
            df["_pass"] = df["_pass"] & (vals > filt["threshold"])
        df["_pass"] = df["_pass"] & vals.notna()
    return df[df["_pass"]].drop(columns=["_pass"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------
def stats(df):
    if df.empty:
        return {
            "n": 0,
            "wr": 0,
            "pnl": 0,
            "daily": 0,
            "dd": 0,
            "days": 0,
            "pos_days": 0,
        }
    n = len(df)
    wins = df["hit"].sum()
    pnl = df["pnl"].sum()
    daily = df.groupby("date")["pnl"].sum()
    cum = np.cumsum(df["pnl"].values)
    dd = float(np.max(np.maximum.accumulate(cum) - cum)) if len(cum) else 0
    ndays = len(daily)
    return {
        "n": n,
        "wr": wins / n,
        "pnl": pnl,
        "daily": daily.mean() if ndays else 0,
        "dd": dd,
        "days": ndays,
        "pos_days": (daily > 0).sum(),
    }


def print_stats(label, s):
    print(f"  {label}")
    print(
        f"    Trades: {s['n']:,}  |  WR: {s['wr'] * 100:.1f}%  |  PnL: ${s['pnl']:+,.2f}"
    )
    print(
        f"    Daily: ${s['daily']:+.2f}/day  |  Max DD: ${s['dd']:.2f}  |  "
        f"Profitable days: {s['pos_days']}/{s['days']}"
    )


# ---------------------------------------------------------------------------
# Equity curve plotter
# ---------------------------------------------------------------------------
COLORS = [
    "#2196F3",
    "#4CAF50",
    "#FF9800",
    "#E91E63",
    "#9C27B0",
    "#00BCD4",
    "#795548",
    "#607D8B",
]


def _esc(s: str) -> str:
    """Escape $ for matplotlib labels (prevents mathtext parsing)."""
    return s.replace("$", "\\$")


def plot_curves(
    results: List[Tuple[str, pd.DataFrame]], output_file: Path, title: str = ""
):
    if not HAS_MPL:
        print("  (matplotlib not installed, skipping plot)")
        return

    output_file = Path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Determine max cycle count across all configs (for shared X axis)
    max_cycles = 0
    for _, tdf in results:
        if not tdf.empty and "cycle_idx" in tdf.columns:
            max_cycles = max(max_cycles, int(tdf["cycle_idx"].max()) + 1)

    use_cycles = max_cycles > 0

    if use_cycles:
        # --- Per-cycle equity curve (X = cycle #, Y = cumulative PnL) ---
        fig, ax = plt.subplots(figsize=(14, 6))

        for i, (label, tdf) in enumerate(results):
            if tdf.empty:
                continue
            # Build per-cycle cumulative PnL
            cycle_pnl = tdf.groupby("cycle_idx")["pnl"].sum()
            # Fill all cycles (even those with no trades = 0 pnl)
            all_cycles = pd.Series(0.0, index=range(max_cycles))
            all_cycles.update(cycle_pnl)
            cum_pnl = all_cycles.cumsum()

            color = COLORS[i % len(COLORS)]
            s = stats(tdf)
            short = _esc(
                f"{label} ({s['n']:,}tr, {s['wr'] * 100:.0f}%WR, ${s['pnl']:+.1f})"
            )
            ax.plot(
                cum_pnl.index,
                cum_pnl.values,
                color=color,
                linewidth=2.0 if i == 0 else 1.5,
                linestyle="-" if i < 4 else "--",
                label=short,
                alpha=0.9,
            )

        ax.axhline(0, color="black", lw=0.5)
        ax.fill_between(
            range(max_cycles), 0, [0] * max_cycles, alpha=0
        )  # dummy for layout
        ax.set_xlabel("Cycle #", fontsize=12)
        ax.set_ylabel("Cumulative PnL ($)", fontsize=12)
        ax.set_title(title or "Backtest Equity Curves", fontsize=13, fontweight="bold")
        ax.legend(loc="upper left", fontsize=9)
        ax.grid(True, alpha=0.3)

        plt.tight_layout()
        fig.savefig(str(output_file), dpi=150, bbox_inches="tight")
        plt.close(fig)
    else:
        # --- Fallback: daily aggregation (for Telonex data without cycle_idx) ---
        fig, axes = plt.subplots(
            2, 1, figsize=(14, 10), gridspec_kw={"height_ratios": [3, 1]}
        )
        ax_eq = axes[0]
        ax_wr = axes[1]

        for i, (label, tdf) in enumerate(results):
            if tdf.empty:
                continue
            daily = (
                tdf.groupby("date")
                .agg(pnl=("pnl", "sum"), wr=("hit", "mean"), n=("pnl", "count"))
                .reset_index()
            )
            daily = daily.sort_values("date")
            daily["cum_pnl"] = daily["pnl"].cumsum()
            dates = pd.to_datetime(daily["date"])
            color = COLORS[i % len(COLORS)]
            s = stats(tdf)
            short = _esc(
                f"{label} ({s['n']:,}tr, {s['wr'] * 100:.0f}%WR, ${s['daily']:+.1f}/d)"
            )
            ax_eq.plot(
                dates,
                daily["cum_pnl"],
                color=color,
                linewidth=2.0 if i == 0 else 1.5,
                linestyle="-" if i < 4 else "--",
                label=short,
                alpha=0.9,
            )
            rolling_wr = daily["wr"].rolling(7, min_periods=3).mean()
            ax_wr.plot(dates, rolling_wr * 100, color=color, linewidth=1.2, alpha=0.7)

        ax_eq.axhline(0, color="black", lw=0.5)
        ax_eq.set_ylabel("Cumulative PnL ($)", fontsize=12)
        ax_eq.set_title(
            title or "Backtest Comparison — Telonex 74 days",
            fontsize=13,
            fontweight="bold",
        )
        ax_eq.legend(loc="upper left", fontsize=8)
        ax_eq.grid(True, alpha=0.3)

        ax_wr.axhline(50, color="black", lw=0.5, ls="--")
        ax_wr.set_ylabel("7-day Rolling WR %", fontsize=10)
        ax_wr.set_xlabel("Date", fontsize=11)
        ax_wr.grid(True, alpha=0.3)
        ax_wr.set_ylim(20, 85)

        for ax in axes:
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
            ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
        fig.autofmt_xdate(rotation=45)
        plt.tight_layout()
        fig.savefig(str(output_file), dpi=150, bbox_inches="tight")
        plt.close(fig)

    print(f"\n  Plot saved: {output_file}")


# ---------------------------------------------------------------------------
# Preset definitions
# ---------------------------------------------------------------------------
def make_rules_with_maxask(ma):
    """Clone DEFAULT_RULES overriding all max_ask to a single value."""
    return [dict(r, max_ask=ma) for r in DEFAULT_RULES]


def make_rules(uuu=0.49, uuuu=0.51, uuuuu=0.51, uduu=0.50, eth_bonus=0.03):
    """Build rule set with per-pattern max_ask.

    Args:
        uuu:       max_ask for UUU (all coins)
        uuuu:      max_ask for UUUU (non-ETH)
        uuuuu:     max_ask for UUUUU (non-ETH)
        uduu:      max_ask for UDUU (BTC only)
        eth_bonus: extra max_ask for ETH on UUUU/UUUUU (default +$0.03)
    """
    return [
        {
            "pattern": "UUUUU",
            "side": "DOWN",
            "coins": ["ETH"],
            "max_ask": uuuuu + eth_bonus,
        },
        {
            "pattern": "UUUUU",
            "side": "DOWN",
            "coins": ["BTC", "SOL", "XRP"],
            "max_ask": uuuuu,
        },
        {
            "pattern": "UUUU",
            "side": "DOWN",
            "coins": ["ETH"],
            "max_ask": uuuu + eth_bonus,
        },
        {
            "pattern": "UUUU",
            "side": "DOWN",
            "coins": ["BTC", "SOL", "XRP"],
            "max_ask": uuuu,
        },
        {"pattern": "UDUU", "side": "DOWN", "coins": ["BTC"], "max_ask": uduu},
        {
            "pattern": "UUU",
            "side": "DOWN",
            "coins": ["BTC", "ETH", "SOL", "XRP"],
            "max_ask": uuu,
        },
    ]


# Named strategies with per-pattern max_ask tuning
# Based on Telonex WR data:
#   UUU:  54.0% WR → breakeven ~$0.502
#   UUUU: 54.5% WR → breakeven ~$0.507
#   UUUUU:54.6% WR → breakeven ~$0.508
#   ETH UUUU: 57.4% WR → breakeven ~$0.535
STRATEGY_CURRENT = DEFAULT_RULES  # UUU=0.49, UUUU=0.51, UDUU=0.50, ETH+0.03

STRATEGY_TIGHT = make_rules(  # Tighter UUU, keep rest
    uuu=0.49,
    uuuu=0.51,
    uuuuu=0.51,
    uduu=0.50,
    eth_bonus=0.03,
)

STRATEGY_AGGRESSIVE = make_rules(  # Wider for longer streaks
    uuu=0.49,
    uuuu=0.53,
    uuuuu=0.55,
    uduu=0.51,
    eth_bonus=0.04,
)

STRATEGY_CONSERVATIVE = make_rules(  # Only enter at good prices
    uuu=0.48,
    uuuu=0.49,
    uuuuu=0.50,
    uduu=0.49,
    eth_bonus=0.03,
)

STRATEGY_NO_UUU = [  # Drop UUU entirely (lowest edge), keep UUUU+
    {"pattern": "UUUUU", "side": "DOWN", "coins": ["ETH"], "max_ask": 0.55},
    {
        "pattern": "UUUUU",
        "side": "DOWN",
        "coins": ["BTC", "SOL", "XRP"],
        "max_ask": 0.52,
    },
    {"pattern": "UUUU", "side": "DOWN", "coins": ["ETH"], "max_ask": 0.55},
    {
        "pattern": "UUUU",
        "side": "DOWN",
        "coins": ["BTC", "SOL", "XRP"],
        "max_ask": 0.52,
    },
    {"pattern": "UDUU", "side": "DOWN", "coins": ["BTC"], "max_ask": 0.51},
]

RSI7_60 = [
    {
        "indicator": "RSI",
        "period": 7,
        "timeframe": "5m",
        "condition": "<",
        "threshold": 60,
    }
]
RSI7_65 = [
    {
        "indicator": "RSI",
        "period": 7,
        "timeframe": "5m",
        "condition": "<",
        "threshold": 65,
    }
]
RSI14_60 = [
    {
        "indicator": "RSI",
        "period": 14,
        "timeframe": "5m",
        "condition": "<",
        "threshold": 60,
    }
]


PRESETS = {
    "default": {
        "title": "Strategy Comparison: Baseline vs RSI Filters",
        "configs": [
            ("Baseline (no filter)", DEFAULT_RULES, []),
            ("RSI(14) 5m skip>60", DEFAULT_RULES, RSI14_60),
            ("RSI(7) 5m skip>60", DEFAULT_RULES, RSI7_60),
            ("RSI(7) 5m skip>65", DEFAULT_RULES, RSI7_65),
        ],
    },
    "strategy": {
        "title": "Per-Pattern Max Ask Strategies (with RSI(7)<60)",
        "configs": [
            ("Current YAML", STRATEGY_CURRENT, RSI7_60),
            ("Tight (UUU=49)", STRATEGY_TIGHT, RSI7_60),
            ("Aggressive (UUUU=53)", STRATEGY_AGGRESSIVE, RSI7_60),
            ("Conservative (all tight)", STRATEGY_CONSERVATIVE, RSI7_60),
            ("No UUU (UUUU+ only)", STRATEGY_NO_UUU, RSI7_60),
        ],
    },
    "strategy-raw": {
        "title": "Per-Pattern Max Ask Strategies (NO RSI filter)",
        "configs": [
            ("Current YAML", STRATEGY_CURRENT, []),
            ("Tight (UUU=49)", STRATEGY_TIGHT, []),
            ("Aggressive (UUUU=53)", STRATEGY_AGGRESSIVE, []),
            ("Conservative (all tight)", STRATEGY_CONSERVATIVE, []),
            ("No UUU (UUUU+ only)", STRATEGY_NO_UUU, []),
        ],
    },
    "rsi7": {
        "title": "RSI(7) Threshold Sweep",
        "configs": [
            ("Baseline", DEFAULT_RULES, []),
            (
                "RSI(7) skip>55",
                DEFAULT_RULES,
                [
                    {
                        "indicator": "RSI",
                        "period": 7,
                        "timeframe": "5m",
                        "condition": "<",
                        "threshold": 55,
                    }
                ],
            ),
            ("RSI(7) skip>60", DEFAULT_RULES, RSI7_60),
            ("RSI(7) skip>65", DEFAULT_RULES, RSI7_65),
            (
                "RSI(7) skip>70",
                DEFAULT_RULES,
                [
                    {
                        "indicator": "RSI",
                        "period": 7,
                        "timeframe": "5m",
                        "condition": "<",
                        "threshold": 70,
                    }
                ],
            ),
        ],
    },
    "maxask": {
        "title": "Max Ask Sweep (flat, all patterns same)",
        "configs": [
            ("max_ask=$0.45", make_rules_with_maxask(0.45), []),
            ("max_ask=$0.47", make_rules_with_maxask(0.47), []),
            ("max_ask=$0.49", make_rules_with_maxask(0.49), []),
            ("max_ask=$0.51 (current)", DEFAULT_RULES, []),
            ("max_ask=$0.54", make_rules_with_maxask(0.54), []),
            ("max_ask=$0.60", make_rules_with_maxask(0.60), []),
        ],
    },
    "indicators": {
        "title": "Indicator Comparison (all 5m)",
        "configs": [
            ("Baseline", DEFAULT_RULES, []),
            ("RSI(7) skip>60", DEFAULT_RULES, RSI7_60),
            (
                "BB above upper skip",
                DEFAULT_RULES,
                [
                    {
                        "indicator": "BB_UPPER",
                        "period": 20,
                        "timeframe": "5m",
                        "condition": "<",
                        "threshold": 0,
                    }
                ],
            ),
            (
                "MACD bullish skip",
                DEFAULT_RULES,
                [
                    {
                        "indicator": "MACD_HIST",
                        "timeframe": "5m",
                        "condition": "<",
                        "threshold": 0,
                    }
                ],
            ),
            (
                "Stoch >80 skip",
                DEFAULT_RULES,
                [
                    {
                        "indicator": "STOCH_K",
                        "period": 14,
                        "timeframe": "5m",
                        "condition": "<",
                        "threshold": 80,
                    }
                ],
            ),
            (
                "RSI(7)<60 + BB<upper",
                DEFAULT_RULES,
                [
                    {
                        "indicator": "RSI",
                        "period": 7,
                        "timeframe": "5m",
                        "condition": "<",
                        "threshold": 60,
                    },
                    {
                        "indicator": "BB_UPPER",
                        "period": 20,
                        "timeframe": "5m",
                        "condition": "<",
                        "threshold": 0,
                    },
                ],
            ),
        ],
    },
    "all": {
        "title": "Best of Each: Strategy x Filter",
        "configs": [
            ("Current (no filter)", STRATEGY_CURRENT, []),
            ("Current + RSI(7)<60", STRATEGY_CURRENT, RSI7_60),
            ("Tight + RSI(7)<60", STRATEGY_TIGHT, RSI7_60),
            ("Aggressive + RSI(7)<60", STRATEGY_AGGRESSIVE, RSI7_60),
            ("No UUU + RSI(7)<60", STRATEGY_NO_UUU, RSI7_60),
            ("Tight + RSI(7)<65", STRATEGY_TIGHT, RSI7_65),
        ],
    },
}


# ---------------------------------------------------------------------------
# Single-pattern backtest mode
# ---------------------------------------------------------------------------
def _run_pattern_mode(args):
    """Run a detailed backtest for a single pattern.

    Usage:
        python research/backtest.py --pattern UUU                      # All coins, auto side, max_ask sweep
        python research/backtest.py --pattern UUU --coin ETH           # ETH only
        python research/backtest.py --pattern UUU --max-ask 0.49       # Fixed max_ask
        python research/backtest.py --pattern DDD                      # Auto-detects: bet UP
        python research/backtest.py --pattern DUDUDD --coin ALL        # Explicit ALL coins
        python research/backtest.py --pattern UUU --csv                # Use CSV data
    """
    from scipy.stats import binomtest

    pattern = args.pattern.upper()
    use_csv = args.csv
    mode_str = "CSV (real prices)" if use_csv else "Telonex (74 days)"
    size = args.size
    fee = args.fee

    # Auto-detect bet side: bet AGAINST the last character (reversal)
    last_char = pattern[-1]
    side = "DOWN" if last_char == "U" else "UP"

    # Coin filter
    if args.coin and args.coin.upper() != "ALL":
        coins = [args.coin.upper()]
        if coins[0] not in COINS:
            print(f"  ERROR: Unknown coin '{coins[0]}'. Must be one of {COINS}")
            return
    else:
        coins = COINS
    coins_str = ",".join(coins) if len(coins) < 4 else "ALL"

    print("=" * 80)
    print(f"  SINGLE PATTERN BACKTEST — {mode_str}")
    print(f"  Pattern: {pattern} -> Bet {side}  |  Coins: {coins_str}")
    print("=" * 80)

    # Load data
    if use_csv:
        print(f"\n  Loading CSV data...")
        raw = load_csvs()
        seqs = csv_build_cycles(raw)
        all_ts = []
        for c in COINS:
            all_ts.extend(seqs[c]["start_ts"])
        ts_min, ts_max = min(all_ts), max(all_ts)
        trade_finder = find_trades_csv
    else:
        print(f"\n  Loading Telonex data...")
        tdf = load_telonex()
        seqs = build_sequences(tdf)
        n_markets = len(tdf)
        date_min, date_max = tdf["date"].min(), tdf["date"].max()
        ndays_total = (date_max - date_min).days + 1
        print(
            f"  {n_markets:,} markets | {date_min} to {date_max} ({ndays_total} days)"
        )
        ts_min = int(tdf["start_ts"].min())
        ts_max = int(tdf["start_ts"].max())
        trade_finder = find_trades

    engine = IndicatorEngine(ts_min, ts_max)

    # --- Max ask sweep ---
    if args.max_ask is not None:
        # Single max_ask: show baseline + RSI variants
        ma_values = [args.max_ask]
        show_rsi_comparison = not args.no_rsi
    else:
        # Sweep: find the profitable range
        ma_values = [0.45, 0.47, 0.49, 0.50, 0.51, 0.52, 0.53, 0.54, 0.55, 0.57, 0.60]
        show_rsi_comparison = False

    print(
        f"\n  {'MaxAsk':>8} {'Trades':>7} {'Wins':>6} {'WR':>6} {'PnL':>10} {'$/day':>8} {'MaxDD':>8} {'p-value':>10} {'Sig?':>5}"
    )
    print(
        f"  {'-' * 8} {'-' * 7} {'-' * 6} {'-' * 6} {'-' * 10} {'-' * 8} {'-' * 8} {'-' * 10} {'-' * 5}"
    )

    best_pnl = -9999
    best_ma = None
    best_trades_df = None

    for ma in ma_values:
        rules = [{"pattern": pattern, "side": side, "coins": coins, "max_ask": ma}]
        trades = trade_finder(seqs, rules, size=size, fee=fee)
        s = stats(trades)
        n, wr = s["n"], s["wr"]

        # Statistical significance via binomial test
        if n > 0:
            wins = int(trades["hit"].sum())
            bt = binomtest(wins, n, 0.5, alternative="greater")
            pval = bt.pvalue
            sig = (
                "***"
                if pval < 0.001
                else "**"
                if pval < 0.01
                else "*"
                if pval < 0.05
                else ""
            )
        else:
            pval = 1.0
            sig = ""

        marker = " <--" if s["pnl"] > best_pnl and n > 0 else ""
        print(
            f"  ${ma:.2f}   {n:>7,} {int(n * wr):>6} {wr * 100:>5.1f}% ${s['pnl']:>+9.2f} "
            f"${s['daily']:>+7.2f} ${s['dd']:>7.2f}  {pval:>9.6f} {sig:>5}{marker}"
        )

        if n > 0 and s["pnl"] > best_pnl:
            best_pnl = s["pnl"]
            best_ma = ma
            best_trades_df = trades

    # --- Per-coin breakdown at best max_ask ---
    if best_trades_df is not None and not best_trades_df.empty:
        print(f"\n  Best max_ask: ${best_ma:.2f}")

        if len(coins) > 1:
            print(f"\n  Per-coin breakdown (max_ask=${best_ma:.2f}):")
            print(
                f"  {'Coin':<6} {'Trades':>7} {'Wins':>6} {'WR':>6} {'PnL':>10} {'p-value':>10}"
            )
            print(f"  {'-' * 6} {'-' * 7} {'-' * 6} {'-' * 6} {'-' * 10} {'-' * 10}")
            for coin in coins:
                sub = best_trades_df[best_trades_df["coin"] == coin]
                if sub.empty:
                    continue
                n = len(sub)
                w = int(sub["hit"].sum())
                wr = w / n
                pnl = sub["pnl"].sum()
                bt = binomtest(w, n, 0.5, alternative="greater")
                sig = (
                    "***"
                    if bt.pvalue < 0.001
                    else "**"
                    if bt.pvalue < 0.01
                    else "*"
                    if bt.pvalue < 0.05
                    else ""
                )
                print(
                    f"  {coin:<6} {n:>7} {w:>6} {wr * 100:>5.1f}% ${pnl:>+9.2f}  {bt.pvalue:>9.6f} {sig}"
                )

        # --- Weekly stability ---
        daily = (
            best_trades_df.groupby("date")
            .agg(pnl=("pnl", "sum"), n=("pnl", "count"), wins=("hit", "sum"))
            .reset_index()
        )
        daily["date"] = pd.to_datetime(daily["date"])
        daily = daily.sort_values("date")

        # Group by week
        daily["week"] = daily["date"].dt.isocalendar().week.astype(int)
        daily["year"] = daily["date"].dt.isocalendar().year.astype(int)
        weekly = (
            daily.groupby(["year", "week"])
            .agg(
                pnl=("pnl", "sum"),
                n=("n", "sum"),
                wins=("wins", "sum"),
                days=("date", "count"),
            )
            .reset_index()
        )

        if len(weekly) >= 3:
            print(f"\n  Weekly stability (max_ask=${best_ma:.2f}):")
            print(f"  {'Week':>8} {'Trades':>7} {'WR':>6} {'PnL':>10} {'Green?':>7}")
            print(f"  {'-' * 8} {'-' * 7} {'-' * 6} {'-' * 10} {'-' * 7}")
            green_weeks = 0
            for _, row in weekly.iterrows():
                n = int(row["n"])
                w = int(row["wins"])
                wr = w / n if n > 0 else 0
                pnl = row["pnl"]
                green = "YES" if pnl > 0 else "no"
                if pnl > 0:
                    green_weeks += 1
                print(
                    f"  W{int(row['week']):02d}     {n:>7} {wr * 100:>5.1f}% ${pnl:>+9.2f} {green:>7}"
                )
            print(
                f"\n  Green weeks: {green_weeks}/{len(weekly)} ({green_weeks / len(weekly) * 100:.0f}%)"
            )

        # --- RSI comparison (if applicable and not --no-rsi) ---
        if show_rsi_comparison and args.max_ask is not None:
            ma = args.max_ask
            rules = [{"pattern": pattern, "side": side, "coins": coins, "max_ask": ma}]
            base_trades = trade_finder(seqs, rules, size=size, fee=fee)

            rsi_configs = [
                ("No filter", []),
                (
                    "RSI(7)<55",
                    [
                        {
                            "indicator": "RSI",
                            "period": 7,
                            "timeframe": "5m",
                            "condition": "<",
                            "threshold": 55,
                        }
                    ],
                ),
                ("RSI(7)<60", RSI7_60),
                ("RSI(7)<65", RSI7_65),
                ("RSI(14)<60", RSI14_60),
            ]

            print(f"\n  RSI filter comparison (max_ask=${ma:.2f}):")
            print(f"  {'Filter':<16} {'Trades':>7} {'WR':>6} {'PnL':>10} {'$/day':>8}")
            print(f"  {'-' * 16} {'-' * 7} {'-' * 6} {'-' * 10} {'-' * 8}")

            for flabel, filt in rsi_configs:
                if filt:
                    ft = apply_filters(base_trades, filt, engine)
                else:
                    ft = base_trades
                s = stats(ft)
                print(
                    f"  {flabel:<16} {s['n']:>7,} {s['wr'] * 100:>5.1f}% ${s['pnl']:>+9.2f} ${s['daily']:>+7.2f}"
                )

    # --- Plot equity curve ---
    if best_trades_df is not None and not best_trades_df.empty and HAS_MPL:
        out = (
            Path(args.output)
            if args.output
            else OUTPUT_DIR / f"equity_pattern_{pattern}_{coins_str}.png"
        )
        coin_tag = f" ({coins_str})" if len(coins) < 4 else ""
        title = f"Pattern {pattern} -> {side}{coin_tag} -- max_ask=${best_ma:.2f} ({mode_str})"
        plot_curves(
            [(f"{pattern}->{side} ma=${best_ma:.2f}", best_trades_df)], out, title
        )

    print(f"\n  Done!")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Backtest with equity curve comparison"
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Use real 1-second CSV data (data/prices_*.csv)",
    )
    parser.add_argument(
        "--preset",
        default="default",
        help="Preset to run: default, rsi7, maxask, indicators, all",
    )
    parser.add_argument(
        "--custom",
        action="store_true",
        help="Run only the custom config defined at top of file",
    )
    parser.add_argument("--list", action="store_true", help="List available presets")
    parser.add_argument(
        "--size", type=float, default=5.0, help="Trade size (default $5)"
    )
    parser.add_argument("--fee", type=float, default=0.0, help="Fee rate (default 0%%)")
    parser.add_argument("--output", type=str, default=None, help="Output PNG path")
    parser.add_argument(
        "--max-ask",
        type=float,
        default=None,
        help="Override max_ask for ALL rules (e.g. --max-ask 0.60). Applies RSI(7)<60 by default.",
    )
    parser.add_argument(
        "--no-rsi",
        action="store_true",
        help="Disable RSI filter (only with --max-ask)",
    )
    parser.add_argument(
        "--pattern",
        type=str,
        default=None,
        help="Single pattern to test, e.g. UUU, DDD, UUUU. Auto-detects bet side (reversal).",
    )
    parser.add_argument(
        "--coin",
        type=str,
        default=None,
        help="Single coin to test (BTC, ETH, SOL, XRP). Default: ALL coins.",
    )
    args = parser.parse_args()

    if args.list:
        print("Available presets:")
        for name, preset in PRESETS.items():
            configs = preset["configs"]
            print(f"  --preset {name:12s}  {preset['title']}  ({len(configs)} curves)")
        print(f"\n  --custom              Run the CUSTOM_* config at top of file")
        print(
            f"\n  --pattern UUU         Single-pattern backtest (max_ask sweep, per-coin, weekly)"
        )
        print(
            f"  --pattern UUU --coin ETH --max-ask 0.49   (with coin filter and RSI comparison)"
        )
        print(f"\n  --csv                 Use real CSV data instead of Telonex")
        return

    # --pattern mode: single-pattern backtest with detailed output
    if args.pattern is not None:
        _run_pattern_mode(args)
        return

    use_csv = args.csv
    mode_str = "CSV (real prices)" if use_csv else "Telonex (74 days)"

    # Load data
    print("=" * 80)
    print(f"  BACKTEST ENGINE — {mode_str}")
    print("=" * 80)

    if use_csv:
        print(f"\n  Loading CSV data (data/prices_*.csv)...")
        print(f"  Outcomes: Gamma API (ground truth, cached in resolution_cache.json)")
        raw = load_csvs()
        total_cycles = raw["cycle_start"].nunique()
        print(f"  Total: {len(raw):,} rows, {total_cycles:,} cycles")
        print(f"\n  Building outcome sequences...")
        seqs = csv_build_cycles(raw)
        for c in COINS:
            det = sum(1 for o in seqs[c]["outcomes"] if o is not None)
            print(f"    {c}: {seqs[c]['n']:,} cycles ({det} with clear outcome)")
        # ts range for indicator engine
        all_ts = []
        for c in COINS:
            all_ts.extend(seqs[c]["start_ts"])
        ts_min, ts_max = min(all_ts), max(all_ts)
    else:
        print(f"\n  Loading Telonex data...")
        tdf = load_telonex()
        seqs = build_sequences(tdf)
        n_markets = len(tdf)
        date_min, date_max = tdf["date"].min(), tdf["date"].max()
        print(f"  {n_markets:,} markets | {date_min} to {date_max}")
        for c in COINS:
            print(f"    {c}: {seqs[c]['n']:,}")
        ts_min = int(tdf["start_ts"].min())
        ts_max = int(tdf["start_ts"].max())

    engine = IndicatorEngine(ts_min, ts_max)

    # --max-ask override: multiple curves at that max_ask with different filters
    if args.max_ask is not None:
        ma = args.max_ask
        rules_ma = make_rules_with_maxask(ma)
        configs = [
            (f"No filter (baseline)", rules_ma, []),
            (f"RSI(7)<60", rules_ma, RSI7_60),
            (f"RSI(7)<65", rules_ma, RSI7_65),
            (
                f"RSI(7)<55",
                rules_ma,
                [
                    {
                        "indicator": "RSI",
                        "period": 7,
                        "timeframe": "5m",
                        "condition": "<",
                        "threshold": 55,
                    },
                ],
            ),
            (
                f"RSI(14)<60",
                rules_ma,
                [
                    {
                        "indicator": "RSI",
                        "period": 14,
                        "timeframe": "5m",
                        "condition": "<",
                        "threshold": 60,
                    },
                ],
            ),
        ]
        title = f"Backtest max_ask={ma:.2f} — Filter Comparison ({mode_str})"
        preset_name = f"csv_maxask_{ma:.2f}" if use_csv else f"maxask_{ma:.2f}"
        size = args.size
        fee = args.fee
    elif args.custom:
        configs = [
            ("Baseline (no filter)", DEFAULT_RULES, []),
            (CUSTOM_LABEL, CUSTOM_RULES, CUSTOM_FILTERS),
        ]
        # In custom mode, use CUSTOM_SIZE/CUSTOM_FEE from the config section
        # at the top of this file.  CLI --size/--fee override if explicitly set.
        size = CUSTOM_SIZE
        fee = CUSTOM_FEE
        title = f"Custom Backtest ({mode_str}) | ${size}/trade"
        preset_name = "csv_custom" if use_csv else "custom"
    else:
        preset = PRESETS.get(args.preset)
        if not preset:
            print(
                f"  ERROR: Unknown preset '{args.preset}'. Use --list to see options."
            )
            return
        configs = preset["configs"]
        title = f"{preset['title']} ({mode_str})"
        preset_name = f"csv_{args.preset}" if use_csv else args.preset
        size = args.size
        fee = args.fee

    # Choose the right trade finder
    trade_finder = find_trades_csv if use_csv else find_trades

    # Run all configs
    results = []
    print(f"\n  Running {len(configs)} configurations...\n")

    for label, rules, filters in configs:
        print(f"  --- {label} ---")
        # Show per-pattern max_ask for this config
        seen = set()
        for r in rules:
            coins_str = ",".join(r["coins"]) if len(r["coins"]) < 4 else "ALL"
            key = (r["pattern"], coins_str, r["max_ask"])
            if key not in seen:
                print(
                    f"    {r['pattern']:6} [{coins_str:10}] max_ask=${r['max_ask']:.2f}"
                )
                seen.add(key)
        if filters:
            for f in filters:
                print(
                    f"    filter: {f['indicator']}({f.get('period', '')}) {f['condition']} {f['threshold']}"
                )
        trades = trade_finder(seqs, rules, size=size, fee=fee)
        if filters and not trades.empty:
            trades = apply_filters(trades, filters, engine)
        s = stats(trades)
        print_stats(label, s)
        results.append((label, trades))
        print()

    # ---- Align cycle indices across all configs (shared X axis) ----
    # Without this, each config has its own cycle numbering and the plot
    # X axis is misleading (cycle #50 for config A != cycle #50 for config B).
    all_timestamps: set = set()
    for _, tdf in results:
        if not tdf.empty:
            all_timestamps.update(int(ts) for ts in tdf["start_ts"].values)
    if all_timestamps:
        global_ts_sorted = sorted(all_timestamps)
        global_ts_to_idx = {ts: idx for idx, ts in enumerate(global_ts_sorted)}
        aligned_results = []
        for label, tdf in results:
            if not tdf.empty:
                tdf = tdf.copy()
                tdf["cycle_idx"] = tdf["start_ts"].astype(int).map(global_ts_to_idx)
            aligned_results.append((label, tdf))
        results = aligned_results

    # Summary table
    print("=" * 80)
    print("  COMPARISON TABLE")
    print("=" * 80)
    print(
        f"  {'Config':<30} {'Trades':>7} {'WR':>6} {'PnL':>10} {'$/day':>8} {'MaxDD':>8} {'ProfDays':>9}"
    )
    print(f"  {'-' * 30} {'-' * 7} {'-' * 6} {'-' * 10} {'-' * 8} {'-' * 8} {'-' * 9}")
    for label, tdf in results:
        s = stats(tdf)
        pd_str = f"{s['pos_days']}/{s['days']}" if s["days"] else "—"
        print(
            f"  {label:<30} {s['n']:>7,} {s['wr'] * 100:>5.1f}% ${s['pnl']:>+9.2f} "
            f"${s['daily']:>+7.2f} ${s['dd']:>7.2f} {pd_str:>9}"
        )

    # Per-pattern breakdown for best config
    best_label, best_trades = max(
        results, key=lambda x: stats(x[1])["daily"] if not x[1].empty else -999
    )
    if not best_trades.empty:
        print(f"\n  Best config: {best_label}")

        has_entry_ask = "entry_ask" in best_trades.columns
        if has_entry_ask:
            print(f"\n  Entry price stats (real DOWN ask at t=5-8s):")
            print(
                f"  {'Pattern':<8} {'Trades':>7} {'WR':>6} {'PnL':>10} {'AvgAsk':>8} {'MinAsk':>8} {'MaxAsk':>8}"
            )
            print(
                f"  {'-' * 8} {'-' * 7} {'-' * 6} {'-' * 10} {'-' * 8} {'-' * 8} {'-' * 8}"
            )
            for pat in sorted(best_trades["pattern"].unique()):
                sub = best_trades[best_trades["pattern"] == pat]
                n = len(sub)
                w = sub["hit"].sum()
                ea = sub["entry_ask"]
                print(
                    f"  {pat:<8} {n:>7} {w / n * 100:>5.1f}% ${sub['pnl'].sum():>+9.2f} "
                    f"${ea.mean():>7.3f} ${ea.min():>7.3f} ${ea.max():>7.3f}"
                )
        else:
            print(f"\n  Per-pattern breakdown:")
            print(f"  {'Pattern':<8} {'Trades':>7} {'WR':>6} {'PnL':>10}")
            print(f"  {'-' * 8} {'-' * 7} {'-' * 6} {'-' * 10}")
            for pat in sorted(best_trades["pattern"].unique()):
                sub = best_trades[best_trades["pattern"] == pat]
                n = len(sub)
                w = sub["hit"].sum()
                print(
                    f"  {pat:<8} {n:>7} {w / n * 100:>5.1f}% ${sub['pnl'].sum():>+9.2f}"
                )

        print(f"\n  Per-coin breakdown:")
        print(f"  {'Coin':<6} {'Trades':>7} {'WR':>6} {'PnL':>10}")
        print(f"  {'-' * 6} {'-' * 7} {'-' * 6} {'-' * 10}")
        for coin in COINS:
            sub = best_trades[best_trades["coin"] == coin]
            if sub.empty:
                continue
            n = len(sub)
            w = sub["hit"].sum()
            print(f"  {coin:<6} {n:>7} {w / n * 100:>5.1f}% ${sub['pnl'].sum():>+9.2f}")

    # Plot
    out = Path(args.output) if args.output else OUTPUT_DIR / f"equity_{preset_name}.png"
    plot_curves(results, out, title)

    print(f"\n  Done! Open the plot: {out}")


if __name__ == "__main__":
    main()
