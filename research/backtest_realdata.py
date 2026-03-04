"""
backtest_realdata.py - Comprehensive backtest on real collected data + Telonex 74-day data.

TASK 1: Backtest 6 YAML rules on all CSV files (data/prices_*.csv, 5 days)
TASK 2: Backtest same rules on Telonex 74-day parquet (corrected 0% maker fee)
TASK 3: RSI filter evaluation (Binance BTCUSDT 5m klines, RSI > 60 / > 70)

Key corrections from edge_stability_telonex.py:
  - FEE = 0% (maker on Builder Program), NOT 1.5% taker
  - Entry price: DOWN ask at t=1-3s of new cycle
  - PnL: win = (1 - entry_ask) * size, loss = -entry_ask * size

Usage:
    python research/backtest_realdata.py
"""

import json
import sys
import os
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.stats import binomtest

warnings.filterwarnings("ignore", category=FutureWarning)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).parent.parent / "data"
TELONEX_FILE = DATA_DIR / "telonex_updown_5m.parquet"
COINS = ["BTC", "ETH", "SOL", "XRP"]
SIZE = 5.0   # $5 per trade
FEE_RATE = 0.0  # 0% maker fee on Builder

# Live YAML rules
RULES = [
    {"pattern": "UUUUU", "side": "DOWN", "coins": ["ETH"],              "max_ask": 0.54},
    {"pattern": "UUUUU", "side": "DOWN", "coins": ["BTC","SOL","XRP"],  "max_ask": 0.51},
    {"pattern": "UUUU",  "side": "DOWN", "coins": ["ETH"],              "max_ask": 0.54},
    {"pattern": "UUUU",  "side": "DOWN", "coins": ["BTC","SOL","XRP"],  "max_ask": 0.51},
    {"pattern": "UDUU",  "side": "DOWN", "coins": ["BTC"],              "max_ask": 0.51},
    {"pattern": "UUU",   "side": "DOWN", "coins": ["BTC","ETH","SOL","XRP"], "max_ask": 0.51},
]

# Outcome inference from CSV
OUTCOME_THRESHOLD_UP = 0.99    # UP ask >= 0.99 => U
OUTCOME_THRESHOLD_DOWN = 0.01  # UP ask <= 0.01 => D
OUTCOME_WINDOW_START = 295     # seconds into cycle for outcome determination
OUTCOME_WINDOW_END = 299

# Entry timing
ENTRY_SEC_START = 1
ENTRY_SEC_END = 3


# ============================================================================
# TASK 1: CSV-based backtest
# ============================================================================
def load_all_csvs() -> pd.DataFrame:
    """Load all data/prices_*.csv files into one DataFrame."""
    import glob
    csv_pattern = str(DATA_DIR / "prices_*.csv")
    files = sorted(glob.glob(csv_pattern))
    if not files:
        print("ERROR: No CSV files found matching data/prices_*.csv")
        sys.exit(1)

    frames = []
    for f in files:
        df = pd.read_csv(f, parse_dates=["timestamp", "cycle_start"])
        frames.append(df)
        print(f"  Loaded {f}: {len(df):,} rows")

    df = pd.concat(frames, ignore_index=True).sort_values("timestamp").reset_index(drop=True)
    return df


def get_cycles(df: pd.DataFrame, min_rows: int = 20) -> list:
    """Group by cycle_start, return list of DataFrames."""
    return [g.reset_index(drop=True) for _, g in df.groupby("cycle_start") if len(g) >= min_rows]


def determine_outcome_csv(cycle: pd.DataFrame, coin: str) -> Optional[str]:
    """Determine outcome for a coin in a cycle using last 5 seconds of CSV data.

    At t=295-299, if UP ask >= 0.99 => U, if UP ask <= 0.01 => D, else None (ambiguous).
    """
    late = cycle[
        (cycle["seconds_elapsed"] >= OUTCOME_WINDOW_START) &
        (cycle["seconds_elapsed"] <= OUTCOME_WINDOW_END)
    ]
    if len(late) < 1:
        return None

    col = f"{coin.lower()}_up_ask"
    avg = late[col].mean()

    if avg >= OUTCOME_THRESHOLD_UP:
        return "U"
    elif avg <= OUTCOME_THRESHOLD_DOWN:
        return "D"
    else:
        return None  # ambiguous


def get_entry_down_ask(cycle: pd.DataFrame, coin: str) -> Optional[float]:
    """Get DOWN ask price at t=1-3s of the cycle.

    DOWN ask = 1 - UP bid.
    """
    entry_rows = cycle[
        (cycle["seconds_elapsed"] >= ENTRY_SEC_START) &
        (cycle["seconds_elapsed"] <= ENTRY_SEC_END)
    ]
    if entry_rows.empty:
        # Fallback: first 5 seconds
        entry_rows = cycle[cycle["seconds_elapsed"] <= 5]
    if entry_rows.empty:
        return None

    col = f"{coin.lower()}_up_bid"
    up_bid = entry_rows[col].mean()
    down_ask = 1.0 - up_bid

    if down_ask <= 0.01 or down_ask >= 0.99:
        return None
    return down_ask


def backtest_csv():
    """Run backtest on CSV data (Task 1)."""
    print("=" * 80)
    print("TASK 1: BACKTEST ON REAL CSV DATA (data/prices_*.csv)")
    print("=" * 80)

    df = load_all_csvs()
    cycles = get_cycles(df, min_rows=20)
    print(f"\n  Total cycles: {len(cycles):,}")

    # Build per-coin outcome sequences and entry prices
    # We need cycle-aligned data: for each cycle, outcome per coin + entry price
    coin_histories = {c: [] for c in COINS}  # List of (outcome, date)
    coin_entry_prices = {c: [] for c in COINS}  # List of (down_ask, date)

    for cycle in cycles:
        cycle_start = cycle["cycle_start"].iloc[0]
        date = pd.Timestamp(cycle_start).date() if hasattr(pd.Timestamp(cycle_start), 'date') else cycle_start

        for coin in COINS:
            outcome = determine_outcome_csv(cycle, coin)
            entry_price = get_entry_down_ask(cycle, coin)
            coin_histories[coin].append({
                "outcome": outcome,
                "date": date,
                "cycle_start": cycle_start,
                "entry_down_ask": entry_price,
            })

    # Print outcome stats
    print(f"\n  Outcome determination (UP ask >= {OUTCOME_THRESHOLD_UP}, <= {OUTCOME_THRESHOLD_DOWN}):")
    for coin in COINS:
        outcomes = [h["outcome"] for h in coin_histories[coin]]
        n_u = sum(1 for o in outcomes if o == "U")
        n_d = sum(1 for o in outcomes if o == "D")
        n_none = sum(1 for o in outcomes if o is None)
        total = len(outcomes)
        print(f"    {coin}: {total} cycles | U={n_u} ({n_u/total*100:.1f}%) | "
              f"D={n_d} ({n_d/total*100:.1f}%) | ambiguous={n_none} ({n_none/total*100:.1f}%)")

    # Now run the pattern matching with YAML rules
    all_trades = []

    for coin in COINS:
        hist = coin_histories[coin]
        n = len(hist)

        for i in range(1, n):
            # Current cycle outcome (for trade resolution)
            current = hist[i]
            if current["outcome"] is None:
                continue

            # Check rules (priority order: longest pattern first as in YAML)
            for rule in RULES:
                if coin not in rule["coins"]:
                    continue

                pat = list(rule["pattern"])
                plen = len(pat)
                if i < plen:
                    continue

                # Check if prior plen outcomes match pattern
                match = True
                for j in range(plen):
                    prev_outcome = hist[i - plen + j]["outcome"]
                    if prev_outcome != pat[j]:
                        match = False
                        break
                if not match:
                    continue

                # Pattern matches! Check entry price filter
                entry_ask = current["entry_down_ask"]
                if entry_ask is None:
                    break  # no price data, skip this cycle for this coin

                if entry_ask > rule["max_ask"]:
                    break  # too expensive

                # Simulate trade
                won = current["outcome"] == "D"
                if won:
                    pnl = (1.0 - entry_ask) * SIZE - FEE_RATE * SIZE
                else:
                    pnl = -entry_ask * SIZE - FEE_RATE * SIZE

                all_trades.append({
                    "coin": coin,
                    "pattern": rule["pattern"],
                    "side": rule["side"],
                    "max_ask": rule["max_ask"],
                    "entry_ask": entry_ask,
                    "outcome": current["outcome"],
                    "won": won,
                    "pnl": pnl,
                    "date": current["date"],
                    "cycle_start": current["cycle_start"],
                })
                break  # first matching rule wins

    trades_df = pd.DataFrame(all_trades) if all_trades else pd.DataFrame()

    if trades_df.empty:
        print("\n  WARNING: No trades found! Data may not have enough cycles or clear outcomes.")
        print("  This is expected with strict 0.99/0.01 thresholds on 5 days of data.")
        print("  CSV outcome determination is very conservative.")
        return trades_df

    # Print results
    print(f"\n  RESULTS (Fee={FEE_RATE*100:.1f}%, Size=${SIZE:.0f}):")
    print(f"  {'='*70}")
    total = len(trades_df)
    wins = trades_df["won"].sum()
    wr = wins / total if total > 0 else 0
    total_pnl = trades_df["pnl"].sum()
    avg_pnl = trades_df["pnl"].mean()

    print(f"  Total trades:   {total}")
    print(f"  Wins:           {wins} ({wr*100:.1f}%)")
    print(f"  Losses:         {total - wins}")
    print(f"  Total PnL:      ${total_pnl:.2f}")
    print(f"  Avg PnL/trade:  ${avg_pnl:.4f}")

    # Per-pattern breakdown
    print(f"\n  Per-Pattern Breakdown:")
    print(f"  {'Pattern':<10} {'Coins':<18} {'Trades':>6} {'Wins':>5} {'WR':>6} {'PnL':>10} {'Avg':>8}")
    print(f"  {'-'*10} {'-'*18} {'-'*6} {'-'*5} {'-'*6} {'-'*10} {'-'*8}")

    for rule in RULES:
        coins_str = ",".join(rule["coins"])
        mask = (trades_df["pattern"] == rule["pattern"]) & (trades_df["coin"].isin(rule["coins"]))
        sub = trades_df[mask]
        if len(sub) == 0:
            print(f"  {rule['pattern']:<10} {coins_str:<18} {'--':>6}")
            continue
        n = len(sub)
        w = sub["won"].sum()
        wr_ = w / n
        pnl = sub["pnl"].sum()
        avg = sub["pnl"].mean()
        print(f"  {rule['pattern']:<10} {coins_str:<18} {n:>6} {w:>5} {wr_:>5.1%} ${pnl:>+9.2f} ${avg:>+7.4f}")

    # Per-coin breakdown
    print(f"\n  Per-Coin Breakdown:")
    print(f"  {'Coin':<6} {'Trades':>6} {'Wins':>5} {'WR':>6} {'PnL':>10}")
    print(f"  {'-'*6} {'-'*6} {'-'*5} {'-'*6} {'-'*10}")
    for coin in COINS:
        sub = trades_df[trades_df["coin"] == coin]
        if len(sub) == 0:
            continue
        n = len(sub)
        w = sub["won"].sum()
        wr_ = w / n
        pnl = sub["pnl"].sum()
        print(f"  {coin:<6} {n:>6} {w:>5} {wr_:>5.1%} ${pnl:>+9.2f}")

    # Daily PnL
    if not trades_df.empty:
        daily = trades_df.groupby("date").agg(
            n_trades=("pnl", "count"),
            daily_pnl=("pnl", "sum"),
            daily_wr=("won", "mean"),
        ).reset_index()
        daily = daily.sort_values("date")
        daily["cum_pnl"] = daily["daily_pnl"].cumsum()

        n_days = len(daily)
        avg_daily_pnl = daily["daily_pnl"].mean()

        print(f"\n  Daily PnL:")
        print(f"  {'Date':<12} {'Trades':>6} {'PnL':>10} {'CumPnL':>10} {'WR':>6}")
        print(f"  {'-'*12} {'-'*6} {'-'*10} {'-'*10} {'-'*6}")
        for _, r in daily.iterrows():
            print(f"  {str(r['date']):<12} {r['n_trades']:>6} "
                  f"${r['daily_pnl']:>+9.2f} ${r['cum_pnl']:>+9.2f} {r['daily_wr']:>5.1%}")
        print(f"\n  Avg daily PnL: ${avg_daily_pnl:.2f}/day")

    return trades_df


# ============================================================================
# TASK 2: Telonex 74-day backtest with 0% fee
# ============================================================================
def backtest_telonex():
    """Run backtest on Telonex 74-day data with corrected 0% fee."""
    print("\n\n" + "=" * 80)
    print("TASK 2: TELONEX 74-DAY BACKTEST (Corrected 0% Maker Fee)")
    print("=" * 80)

    if not TELONEX_FILE.exists():
        print(f"  ERROR: {TELONEX_FILE} not found")
        return None

    df = pd.read_parquet(TELONEX_FILE)
    df = df[df["result_id"].isin(["0", "1"])].copy()

    # Parse coin and timestamp from slug
    df["coin"] = df["slug"].str.extract(r"^(\w+)-updown-5m-")[0].str.upper()
    df["unix_ts"] = df["slug"].str.extract(r"-(\d+)$")[0].astype(float)
    df["dt"] = pd.to_datetime(df["unix_ts"], unit="s", utc=True)
    df["outcome"] = df["result_id"].map({"0": "U", "1": "D"})
    df["date"] = df["dt"].dt.date
    df = df.sort_values(["coin", "unix_ts"]).reset_index(drop=True)

    print(f"\n  Loaded {len(df):,} settled markets")
    print(f"  Date range: {df['date'].min()} to {df['date'].max()}")
    n_days = (df['date'].max() - df['date'].min()).days + 1
    print(f"  Span: {n_days} days")

    # Build per-coin outcome sequences
    coin_seqs = {}
    for coin in COINS:
        cdf = df[df["coin"] == coin].sort_values("unix_ts").reset_index(drop=True)
        coin_seqs[coin] = {
            "outcomes": cdf["outcome"].values,
            "dates": cdf["date"].values,
            "unix_ts": cdf["unix_ts"].values,
            "n": len(cdf),
        }
        print(f"    {coin}: {len(cdf):,} cycles")

    # Find all rule trades with priority ordering
    all_trades = find_yaml_rule_trades(coin_seqs, RULES)
    trades_df = pd.DataFrame(all_trades)
    trades_df = trades_df.sort_values("date").reset_index(drop=True)

    # ---- Compute PnL with CORRECTED 0% fee ----
    # Telonex data doesn't have order book prices, so we use max_ask as entry price
    # (conservative: assume we always pay max_ask when entering)
    def compute_pnl_corrected(row):
        ask = row["max_ask"]
        if row["hit"]:
            # Win: paid ask per share, payout $1 per share
            pnl = (1.0 - ask) * SIZE - FEE_RATE * SIZE  # FEE_RATE = 0
        else:
            # Loss: lose what we paid
            pnl = -ask * SIZE - FEE_RATE * SIZE
        return pnl

    trades_df["pnl"] = trades_df.apply(compute_pnl_corrected, axis=1)
    trades_df["cum_pnl"] = trades_df["pnl"].cumsum()

    # --- Also compute what the OLD 1.5% fee would give ---
    old_fee = 0.015
    def compute_pnl_old(row):
        ask = row["max_ask"]
        if row["hit"]:
            shares = SIZE / ask
            pnl = shares * (1.0 - ask) - SIZE * old_fee
        else:
            pnl = -SIZE - SIZE * old_fee
        return pnl

    trades_df["pnl_old_fee"] = trades_df.apply(compute_pnl_old, axis=1)

    total = len(trades_df)
    wins = trades_df["hit"].sum()
    wr = wins / total
    total_pnl = trades_df["pnl"].sum()
    total_pnl_old = trades_df["pnl_old_fee"].sum()
    avg_pnl = trades_df["pnl"].mean()

    print(f"\n  RESULTS:")
    print(f"  {'='*70}")
    print(f"  Total trades:         {total:,}")
    print(f"  Wins:                 {wins:,} ({wr*100:.1f}%)")
    print(f"  Losses:               {total - wins:,}")
    print(f"")
    print(f"  >>> CORRECTED (0% maker fee) <<<")
    print(f"  Total PnL:            ${total_pnl:.2f}")
    print(f"  Avg PnL/trade:        ${avg_pnl:.4f}")
    print(f"  Avg daily PnL:        ${total_pnl / n_days:.2f}/day")
    print(f"")
    print(f"  --- OLD (1.5% taker fee, WRONG) ---")
    print(f"  Total PnL (old):      ${total_pnl_old:.2f}")
    print(f"  Avg daily PnL (old):  ${total_pnl_old / n_days:.2f}/day")
    print(f"  Fee correction impact: ${total_pnl - total_pnl_old:+.2f}")

    # Per-pattern breakdown
    print(f"\n  Per-Pattern Breakdown (0% fee):")
    print(f"  {'Pattern':<10} {'Coins':<18} {'Trades':>6} {'Wins':>5} {'WR':>7} {'PnL':>10} {'Avg':>8}")
    print(f"  {'-'*10} {'-'*18} {'-'*6} {'-'*5} {'-'*7} {'-'*10} {'-'*8}")

    for rule in RULES:
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

        # Binomial test vs 50%
        bt = binomtest(w, n, 0.5, alternative="greater")
        sig = f"p={bt.pvalue:.4f}"
        if bt.pvalue < 0.05:
            sig += " *"
        if bt.pvalue < 0.01:
            sig = sig.replace("*", "**")

        print(f"  {rule['pattern']:<10} {coins_str:<18} {n:>6} {w:>5} {wr_:>6.1%} "
              f"${pnl:>+9.2f} ${avg:>+7.4f}  {sig}")

    # Per-coin breakdown
    print(f"\n  Per-Coin Breakdown (0% fee):")
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

    # Best single pattern: ETH UUUU->DOWN
    print(f"\n  >>> BEST SINGLE PATTERN: ETH UUUU->DOWN <<<")
    mask_best = (trades_df["pattern"] == "UUUU") & (trades_df["coin"] == "ETH")
    best = trades_df[mask_best]
    if len(best) > 0:
        n = len(best)
        w = best["hit"].sum()
        wr_ = w / n
        pnl = best["pnl"].sum()
        bt_onesided = binomtest(w, n, 0.5, alternative="greater")
        bt_twosided = binomtest(w, n, 0.5)
        ci = bt_twosided.proportion_ci(confidence_level=0.95)
        print(f"  Trades: {n}, Wins: {w}, WR: {wr_*100:.1f}%")
        print(f"  95% CI (two-sided): [{ci.low*100:.1f}%, {ci.high*100:.1f}%]")
        print(f"  PnL: ${pnl:.2f} (avg ${pnl/n:.4f}/trade)")
        print(f"  Binomial p-value (one-sided): {bt_onesided.pvalue:.6f}")

    # Daily PnL trajectory
    daily = trades_df.groupby("date").agg(
        n_trades=("pnl", "count"),
        daily_pnl=("pnl", "sum"),
        daily_wr=("hit", "mean"),
    ).reset_index()
    daily = daily.sort_values("date").reset_index(drop=True)
    daily["cum_pnl"] = daily["daily_pnl"].cumsum()

    avg_daily_pnl = daily["daily_pnl"].mean()
    median_daily_pnl = daily["daily_pnl"].median()
    positive_days = (daily["daily_pnl"] > 0).sum()

    print(f"\n  Daily PnL Summary:")
    print(f"  Avg daily PnL:    ${avg_daily_pnl:.2f}")
    print(f"  Median daily PnL: ${median_daily_pnl:.2f}")
    print(f"  Positive days:    {positive_days}/{len(daily)} ({positive_days/len(daily)*100:.1f}%)")
    print(f"  Total PnL:        ${daily['daily_pnl'].sum():.2f}")

    # Print compact daily trajectory (every 7 days)
    print(f"\n  Daily Trajectory (weekly snapshots):")
    print(f"  {'Date':<12} {'Trades':>6} {'DailyPnL':>9} {'CumPnL':>9} {'WR':>6}")
    print(f"  {'-'*12} {'-'*6} {'-'*9} {'-'*9} {'-'*6}")
    for i, (_, r) in enumerate(daily.iterrows()):
        if i % 7 == 0 or i == len(daily) - 1:
            print(f"  {str(r['date']):<12} {r['n_trades']:>6} "
                  f"${r['daily_pnl']:>+8.2f} ${r['cum_pnl']:>+8.2f} {r['daily_wr']:>5.1%}")

    return trades_df, coin_seqs


def find_yaml_rule_trades(coin_seqs, rules):
    """Simulate the live YAML rules with priority ordering.

    For each coin's sequence, at each position check rules top-to-bottom.
    First matching rule fires.
    """
    all_trades = []
    for coin, data in coin_seqs.items():
        outcomes = data["outcomes"]
        dates = data["dates"]
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
                hit = 1 if (side == "DOWN" and actual == "D") or \
                           (side == "UP" and actual == "U") else 0
                all_trades.append({
                    "coin": coin,
                    "idx": i,
                    "outcome": actual,
                    "date": dates[i],
                    "pattern": rule["pattern"],
                    "side": side,
                    "max_ask": max_ask,
                    "hit": hit,
                })
                break  # first matching rule wins
    return all_trades


# ============================================================================
# TASK 3: RSI Filter Evaluation
# ============================================================================
def compute_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """Compute RSI(period) from a close price series."""
    delta = closes.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    # Use exponential weighted mean (Wilder's smoothing)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def download_binance_klines(
    symbol: str = "BTCUSDT",
    interval: str = "5m",
    start_date: str = "2025-12-18",
    end_date: str = "2026-03-02",
) -> pd.DataFrame:
    """Download Binance klines via REST API (no auth needed)."""
    import urllib.request

    base_url = "https://api.binance.com/api/v3/klines"
    start_ts = int(pd.Timestamp(start_date, tz="UTC").timestamp() * 1000)
    end_ts = int(pd.Timestamp(end_date, tz="UTC").timestamp() * 1000)

    all_klines = []
    current_start = start_ts
    batch = 0

    print(f"\n  Downloading Binance {symbol} {interval} klines...")
    print(f"  Period: {start_date} to {end_date}")

    while current_start < end_ts:
        params = f"symbol={symbol}&interval={interval}&startTime={current_start}&endTime={end_ts}&limit=1000"
        url = f"{base_url}?{params}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            print(f"  WARNING: Binance API error: {e}")
            break

        if not data:
            break

        all_klines.extend(data)
        batch += 1
        last_ts = data[-1][0]

        if last_ts <= current_start:
            break
        current_start = last_ts + 1

        if batch % 5 == 0:
            print(f"    Batch {batch}: {len(all_klines):,} klines so far...")

    if not all_klines:
        print("  ERROR: No klines downloaded")
        return pd.DataFrame()

    # Parse klines
    df = pd.DataFrame(all_klines, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "num_trades", "taker_buy_base",
        "taker_buy_quote", "ignore"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    df["close"] = df["close"].astype(float)
    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["volume"] = df["volume"].astype(float)

    # Drop duplicates
    df = df.drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)

    print(f"  Downloaded {len(df):,} klines")
    print(f"  Date range: {df['open_time'].min()} to {df['open_time'].max()}")

    return df


def evaluate_rsi_filter(coin_seqs, telonex_trades_df):
    """Evaluate RSI > 60 and RSI > 70 filters on the Telonex backtest."""
    print("\n\n" + "=" * 80)
    print("TASK 3: RSI FILTER EVALUATION")
    print("=" * 80)

    # Download Binance klines for the Telonex period
    klines = download_binance_klines(
        symbol="BTCUSDT",
        interval="5m",
        start_date="2025-12-15",  # a few days before Telonex start for RSI warmup
        end_date="2026-03-02",
    )

    if klines.empty:
        print("  Cannot evaluate RSI filter without Binance data")
        return

    # Compute RSI(14)
    klines["rsi_14"] = compute_rsi(klines["close"], period=14)
    print(f"\n  RSI(14) stats:")
    print(f"    Mean:   {klines['rsi_14'].mean():.1f}")
    print(f"    Median: {klines['rsi_14'].median():.1f}")
    print(f"    Min:    {klines['rsi_14'].min():.1f}")
    print(f"    Max:    {klines['rsi_14'].max():.1f}")
    print(f"    % > 60: {(klines['rsi_14'] > 60).mean()*100:.1f}%")
    print(f"    % > 70: {(klines['rsi_14'] > 70).mean()*100:.1f}%")

    # The Telonex trades have unix timestamps. We need to map each trade to
    # the corresponding Binance 5m candle to get the RSI at trade time.
    # The Telonex trade's unix_ts corresponds to the market open time (cycle start).

    # Get all trade unix timestamps from coin_seqs
    # We need to rebuild trades with unix_ts attached
    all_trades_with_ts = []
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
                match = True
                for j in range(plen):
                    if outcomes[i - plen + j] != pat[j]:
                        match = False
                        break
                if not match:
                    continue
                side = rule["side"]
                actual = outcomes[i]
                hit = 1 if (side == "DOWN" and actual == "D") else 0
                max_ask = rule["max_ask"]

                # PnL with 0% fee
                if hit:
                    pnl = (1.0 - max_ask) * SIZE
                else:
                    pnl = -max_ask * SIZE

                all_trades_with_ts.append({
                    "coin": coin,
                    "idx": i,
                    "outcome": actual,
                    "date": dates[i],
                    "pattern": rule["pattern"],
                    "side": side,
                    "max_ask": max_ask,
                    "hit": hit,
                    "pnl": pnl,
                    "unix_ts": unix_ts[i],
                })
                break

    trades_ts_df = pd.DataFrame(all_trades_with_ts)
    trades_ts_df["trade_dt"] = pd.to_datetime(trades_ts_df["unix_ts"], unit="s", utc=True)

    # Map each trade to nearest Binance 5m candle
    # Binance candle open_time should match the Polymarket cycle start
    # We'll find the closest candle by matching open_time
    klines_sorted = klines.sort_values("open_time").reset_index(drop=True)

    def get_rsi_for_timestamp(ts):
        """Get RSI at the 5m candle containing this timestamp."""
        # Find the candle whose open_time is <= ts and close_time >= ts
        mask = klines_sorted["open_time"] <= ts
        if not mask.any():
            return np.nan
        idx = mask.values.nonzero()[0][-1]
        return klines_sorted.iloc[idx]["rsi_14"]

    print(f"\n  Mapping {len(trades_ts_df):,} trades to RSI values...")
    trades_ts_df["rsi"] = trades_ts_df["trade_dt"].apply(get_rsi_for_timestamp)

    # Drop trades where RSI is NaN (warmup period)
    valid = trades_ts_df.dropna(subset=["rsi"])
    print(f"  Trades with valid RSI: {len(valid):,} / {len(trades_ts_df):,}")

    # Baseline: all trades (no filter)
    baseline_n = len(valid)
    baseline_wins = valid["hit"].sum()
    baseline_wr = baseline_wins / baseline_n if baseline_n > 0 else 0
    baseline_pnl = valid["pnl"].sum()

    print(f"\n  {'='*75}")
    print(f"  RSI FILTER COMPARISON")
    print(f"  {'='*75}")
    print(f"  {'Filter':<25} {'Trades':>7} {'Removed':>8} {'Wins':>6} {'WR':>7} "
          f"{'PnL':>10} {'Avg PnL':>8} {'Daily$':>7}")
    print(f"  {'-'*25} {'-'*7} {'-'*8} {'-'*6} {'-'*7} {'-'*10} {'-'*8} {'-'*7}")

    n_days = (valid["date"].max() - valid["date"].min()).days + 1

    # No filter (baseline)
    print(f"  {'No filter (baseline)':<25} {baseline_n:>7} {'--':>8} {baseline_wins:>6} "
          f"{baseline_wr:>6.1%} ${baseline_pnl:>+9.2f} ${baseline_pnl/baseline_n:>+7.4f} "
          f"${baseline_pnl/n_days:>+6.2f}")

    # RSI > 60 filter: skip trades where RSI > 60
    for threshold_name, threshold in [("RSI > 60 (skip)", 60), ("RSI > 70 (skip)", 70)]:
        filtered = valid[valid["rsi"] <= threshold]
        removed = valid[valid["rsi"] > threshold]
        n_filt = len(filtered)
        n_removed = len(removed)
        wins_filt = filtered["hit"].sum()
        wr_filt = wins_filt / n_filt if n_filt > 0 else 0
        pnl_filt = filtered["pnl"].sum()

        # Also show stats for removed trades
        removed_wins = removed["hit"].sum()
        removed_wr = removed_wins / n_removed if n_removed > 0 else 0

        print(f"  {threshold_name:<25} {n_filt:>7} {n_removed:>8} {wins_filt:>6} "
              f"{wr_filt:>6.1%} ${pnl_filt:>+9.2f} ${pnl_filt/n_filt if n_filt > 0 else 0:>+7.4f} "
              f"${pnl_filt/n_days:>+6.2f}")

    # Detailed analysis of removed trades
    print(f"\n  Analysis of trades REMOVED by each filter:")
    for threshold_name, threshold in [("RSI > 60", 60), ("RSI > 70", 70)]:
        removed = valid[valid["rsi"] > threshold]
        if len(removed) == 0:
            print(f"    {threshold_name}: 0 trades removed")
            continue
        n_r = len(removed)
        w_r = removed["hit"].sum()
        wr_r = w_r / n_r
        pnl_r = removed["pnl"].sum()
        print(f"    {threshold_name}: {n_r} trades removed, "
              f"WR={wr_r*100:.1f}%, PnL=${pnl_r:.2f} (avg ${pnl_r/n_r:.4f})")
        if wr_r < 0.5:
            print(f"      -> These are LOSING trades on average -> filter HELPS")
        else:
            print(f"      -> These are WINNING trades on average -> filter HURTS")

    # Per-coin RSI filter impact
    print(f"\n  RSI > 60 Filter Impact Per Coin:")
    print(f"  {'Coin':<6} {'Base N':>7} {'Base WR':>8} {'Filt N':>7} {'Filt WR':>8} "
          f"{'Removed':>8} {'Rem WR':>8}")
    print(f"  {'-'*6} {'-'*7} {'-'*8} {'-'*7} {'-'*8} {'-'*8} {'-'*8}")
    for coin in COINS:
        base_coin = valid[valid["coin"] == coin]
        filt_coin = valid[(valid["coin"] == coin) & (valid["rsi"] <= 60)]
        rem_coin = valid[(valid["coin"] == coin) & (valid["rsi"] > 60)]
        if len(base_coin) == 0:
            continue
        b_n, b_w = len(base_coin), base_coin["hit"].sum()
        f_n, f_w = len(filt_coin), filt_coin["hit"].sum()
        r_n, r_w = len(rem_coin), rem_coin["hit"].sum()
        print(f"  {coin:<6} {b_n:>7} {b_w/b_n:>7.1%} {f_n:>7} "
              f"{f_w/f_n if f_n > 0 else 0:>7.1%} {r_n:>8} "
              f"{r_w/r_n if r_n > 0 else 0:>7.1%}")

    # RSI distribution at trade time
    print(f"\n  RSI Distribution at Trade Entry:")
    rsi_bins = [(0, 30), (30, 40), (40, 50), (50, 60), (60, 70), (70, 80), (80, 100)]
    print(f"  {'RSI Range':<12} {'Trades':>7} {'Wins':>5} {'WR':>7} {'PnL':>10}")
    print(f"  {'-'*12} {'-'*7} {'-'*5} {'-'*7} {'-'*10}")
    for lo, hi in rsi_bins:
        sub = valid[(valid["rsi"] >= lo) & (valid["rsi"] < hi)]
        if len(sub) == 0:
            continue
        n = len(sub)
        w = sub["hit"].sum()
        wr_ = w / n
        pnl = sub["pnl"].sum()
        marker = " <-- filter zone" if lo >= 60 else ""
        print(f"  [{lo:>2}-{hi:>2})    {n:>7} {w:>5} {wr_:>6.1%} ${pnl:>+9.2f}{marker}")


# ============================================================================
# Main
# ============================================================================
if __name__ == "__main__":
    print("=" * 80)
    print("  COMPREHENSIVE BACKTEST: 6 YAML Rules, 0% Maker Fee")
    print("  Date: 2026-03-04")
    print("=" * 80)

    # TASK 1: CSV backtest
    csv_trades = backtest_csv()

    # TASK 2: Telonex backtest
    result = backtest_telonex()
    if result is not None:
        telonex_trades, coin_seqs = result

        # TASK 3: RSI filter
        evaluate_rsi_filter(coin_seqs, telonex_trades)

    print("\n\n" + "=" * 80)
    print("  SUMMARY")
    print("=" * 80)
    print(f"""
  KEY FINDINGS:
  1. Fee correction: 0% maker fee (Builder) vs 1.5% taker fee
     - Every trade saves $0.075 in fees ($5 * 1.5%)
     - Over 74 days this compounds significantly

  2. The best single pattern is ETH UUUU->DOWN
     - See detailed stats above

  3. RSI filter analysis:
     - RSI > 60: More aggressive, removes more trades
     - RSI > 70: Less aggressive, surgical removal
     - See comparison table above for which improves PnL

  NOTE: CSV backtest uses strict outcome thresholds (0.99/0.01)
  which may result in fewer trades than Telonex (which uses
  Gamma API resolution). The Telonex backtest is more comprehensive.
""")
    print("=" * 80)
    print("Done.")
