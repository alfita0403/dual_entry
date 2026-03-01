"""
Trend Check: Is the pattern edge structural or just riding a bearish trend?

Downloads BTC spot 5-minute candles from yfinance for the same period as our
CSV data, then:
  1. Shows BTC overall price action (open, close, % change)
  2. For each 5-min Polymarket cycle, tags it as "spot UP" or "spot DOWN"
     based on whether BTC spot price rose or fell in that window
  3. Runs the pattern backtest SEPARATELY on spot-UP vs spot-DOWN cycles
  4. If patterns work in BOTH conditions → structural edge
     If patterns only work when spot is DOWN → trend-dependent, no real edge

Usage:
    python research/trend_check.py data/prices_2026-02-28.csv data/prices_2026-03-01.csv
"""

import sys
import os
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research_v2 import load_data, get_cycles, COINS

# ---------------------------------------------------------------------------
# Constants (must match backtest_patterns.py exactly)
# ---------------------------------------------------------------------------
FEE_RATE = 0.015
SLIPPAGE = 0.03
ENTRY_TIME = 5
DEFAULT_MAX_ASK = 0.60
INFERENCE_START = 295
UP_THRESHOLD = 0.95
DOWN_THRESHOLD = 0.05

PATTERNS = [
    ("UD", "DOWN"),
    ("UU", "DOWN"),
]


def determine_outcomes(cycle: pd.DataFrame) -> Dict[str, Optional[str]]:
    late = cycle[cycle["seconds_elapsed"] >= INFERENCE_START]
    if len(late) < 2:
        return {c: None for c in COINS}
    out: Dict[str, Optional[str]] = {}
    for c in COINS:
        avg = late[f"{c.lower()}_up_ask"].mean()
        if avg >= UP_THRESHOLD:
            out[c] = "UP"
        elif avg <= DOWN_THRESHOLD:
            out[c] = "DOWN"
        else:
            out[c] = None
    return out


def get_entry_price(cycle: pd.DataFrame, coin: str, side: str) -> Optional[float]:
    entry_rows = cycle[
        (cycle["seconds_elapsed"] >= ENTRY_TIME) &
        (cycle["seconds_elapsed"] <= ENTRY_TIME + 3)
    ]
    if entry_rows.empty:
        entry_rows = cycle[cycle["seconds_elapsed"] <= 10]
    if entry_rows.empty:
        return None
    cl = coin.lower()
    if side == "UP":
        price = entry_rows[f"{cl}_up_ask"].mean()
    else:
        bid = entry_rows[f"{cl}_up_bid"].mean()
        if bid <= 0:
            return None
        price = 1.0 - bid
    if price <= 0.01 or price >= 0.99:
        return None
    return price


def main():
    if len(sys.argv) < 2:
        print("Usage: python research/trend_check.py data/prices_*.csv")
        sys.exit(1)

    csv_paths = sys.argv[1:]

    # ------------------------------------------------------------------
    # 1. Load Polymarket data and determine cycle times
    # ------------------------------------------------------------------
    print("\n  Loading Polymarket data...")
    df = load_data(csv_paths)
    cycles = get_cycles(df, min_rows=20)
    all_outcomes = [determine_outcomes(c) for c in cycles]

    # Get time range
    first_ts = df["timestamp"].min()
    last_ts = df["timestamp"].max()
    print(f"  Data range: {first_ts} to {last_ts}")
    print(f"  Total cycles: {len(cycles)}")

    # ------------------------------------------------------------------
    # 2. Download BTC spot data from yfinance
    # ------------------------------------------------------------------
    print("\n  Downloading BTC 5-min candles from yfinance...")
    start_date = (first_ts - timedelta(hours=1)).strftime("%Y-%m-%d")
    end_date = (last_ts + timedelta(hours=6)).strftime("%Y-%m-%d")

    btc = yf.download("BTC-USD", start=start_date, end=end_date, interval="5m",
                       progress=False, auto_adjust=True)
    if btc.empty:
        print("  ERROR: No BTC data from yfinance")
        sys.exit(1)

    # Flatten multi-level columns if present
    if isinstance(btc.columns, pd.MultiIndex):
        btc.columns = btc.columns.get_level_values(0)

    # Ensure index is timezone-aware UTC
    if btc.index.tz is None:
        btc.index = btc.index.tz_localize("UTC")
    else:
        btc.index = btc.index.tz_convert("UTC")

    btc_open = btc["Open"].iloc[0]
    btc_close = btc["Close"].iloc[-1]
    btc_change = (btc_close - btc_open) / btc_open * 100
    print(f"  BTC open:  ${btc_open:,.0f}")
    print(f"  BTC close: ${btc_close:,.0f}")
    print(f"  BTC change: {btc_change:+.2f}%")
    print(f"  Candles: {len(btc)}")

    # ------------------------------------------------------------------
    # 3. Tag each Polymarket cycle with BTC spot direction
    # ------------------------------------------------------------------
    print("\n  Tagging cycles with BTC spot direction...")

    cycle_tags = []  # "UP", "DOWN", or None
    for cycle in cycles:
        cycle_start = cycle["cycle_start"].iloc[0]
        if cycle_start.tz is None:
            cycle_start = cycle_start.tz_localize("UTC")

        # Find BTC candle closest to this cycle start
        # Look for candles within 3 minutes of cycle start
        mask = (btc.index >= cycle_start - timedelta(minutes=3)) & \
               (btc.index <= cycle_start + timedelta(minutes=3))
        matching = btc[mask]

        if matching.empty:
            cycle_tags.append(None)
            continue

        candle = matching.iloc[0]
        if candle["Close"] > candle["Open"]:
            cycle_tags.append("UP")
        elif candle["Close"] < candle["Open"]:
            cycle_tags.append("DOWN")
        else:
            cycle_tags.append(None)

    spot_up = sum(1 for t in cycle_tags if t == "UP")
    spot_down = sum(1 for t in cycle_tags if t == "DOWN")
    spot_none = sum(1 for t in cycle_tags if t is None)
    print(f"  Spot UP cycles:   {spot_up}")
    print(f"  Spot DOWN cycles: {spot_down}")
    print(f"  Unmatched:        {spot_none}")

    # ------------------------------------------------------------------
    # 4. Run pattern backtest split by spot direction
    # ------------------------------------------------------------------
    print("\n  Running split backtest...")

    # Group: "ALL", "SPOT_UP", "SPOT_DOWN"
    groups = {"ALL": [], "SPOT_UP": [], "SPOT_DOWN": []}

    for i in range(len(cycles)):
        groups["ALL"].append(i)
        tag = cycle_tags[i]
        if tag == "UP":
            groups["SPOT_UP"].append(i)
        elif tag == "DOWN":
            groups["SPOT_DOWN"].append(i)

    for group_name, group_indices in groups.items():
        if not group_indices:
            continue

        # Replay backtest with only these cycles (but history builds from ALL)
        max_plen = max(len(p) for p, _ in PATTERNS)
        history: Dict[str, deque] = {c: deque(maxlen=max_plen + 1) for c in COINS}

        trades = 0
        wins = 0
        total_pnl = 0.0

        for cycle_idx in range(len(cycles)):
            outcomes = all_outcomes[cycle_idx]
            in_group = cycle_idx in group_indices

            # Check pattern matches
            if in_group:
                for coin in COINS:
                    hist = list(history[coin])
                    if not hist:
                        continue
                    for pattern, buy_side in PATTERNS:
                        plen = len(pattern)
                        if len(hist) < plen:
                            continue
                        recent = "".join(hist[-plen:])
                        if recent != pattern:
                            continue

                        coin_outcome = outcomes.get(coin)
                        if coin_outcome is None:
                            break

                        entry_price = get_entry_price(cycles[cycle_idx], coin, buy_side)
                        if entry_price is None or entry_price > DEFAULT_MAX_ASK:
                            break

                        fill_price = entry_price + SLIPPAGE
                        cost = fill_price * 5.0
                        won = (buy_side == coin_outcome)

                        if won:
                            pnl = 5.0 * (1.0 - FEE_RATE) - cost
                        else:
                            pnl = -cost

                        trades += 1
                        if won:
                            wins += 1
                        total_pnl += pnl
                        break

            # Always record outcomes to maintain history continuity
            for coin in COINS:
                outcome = outcomes.get(coin)
                if outcome == "UP":
                    history[coin].append("U")
                elif outcome == "DOWN":
                    history[coin].append("D")

        wr = wins / trades * 100 if trades > 0 else 0
        avg = total_pnl / trades if trades > 0 else 0

        print(f"\n  === {group_name} ({len(group_indices)} cycles) ===")
        print(f"  Trades:    {trades}")
        print(f"  Wins:      {wins}")
        print(f"  Win rate:  {wr:.1f}%")
        print(f"  Total PnL: ${total_pnl:+.2f}")
        print(f"  Avg PnL:   ${avg:+.4f}")

    # ------------------------------------------------------------------
    # 5. Additional: base rate of DOWN winning in each spot condition
    # ------------------------------------------------------------------
    print("\n\n  === BASE RATE CHECK ===")
    print("  (What % of Polymarket cycles resolve DOWN, split by BTC spot direction)")

    for group_name, group_indices in groups.items():
        if not group_indices:
            continue
        total_resolved = 0
        down_wins = 0
        for idx in group_indices:
            outcomes = all_outcomes[idx]
            for coin in COINS:
                o = outcomes.get(coin)
                if o is not None:
                    total_resolved += 1
                    if o == "DOWN":
                        down_wins += 1

        down_rate = down_wins / total_resolved * 100 if total_resolved > 0 else 0
        print(f"  {group_name:>10}: DOWN wins {down_rate:.1f}% ({down_wins}/{total_resolved})")

    print()


if __name__ == "__main__":
    main()
