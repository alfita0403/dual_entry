"""
Regime Analysis - Can we detect favorable vs unfavorable conditions for UU→DOWN?

Tests whether any observable feature at entry time predicts trade success.
Features tested:
  1. Time of day (hourly buckets)
  2. Cross-coin unanimity (how many coins went UP in previous cycle)
  3. Entry ask price level (cheap vs expensive entries)
  4. Trailing outcome volatility (mixing vs trending in recent history)
  5. Intra-cycle price range (how much price moved within the cycle)
  6. Day of data (temporal stability of the edge)

For each feature, splits trades into bins and tests:
  - Win rate per bin
  - Statistical significance (chi-squared / Fisher's exact)
  - Average PnL per bin

Usage:
    python research/regime_analysis.py data/prices_2026-02-28.csv data/prices_2026-03-01.csv data/prices_2026-03-02.csv
    python research/regime_analysis.py data/prices_*.csv --coins BTC,ETH
    python research/regime_analysis.py data/prices_*.csv --coins BTC
"""

import argparse
import json
import sys
import os
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


# ---------------------------------------------------------------------------
# Trade with features
# ---------------------------------------------------------------------------
class FeatureTrade:
    """A trade with associated features for regime analysis."""
    def __init__(self):
        self.cycle_idx: int = 0
        self.cycle_start: str = ""
        self.coin: str = ""
        self.entry_price: float = 0.0
        self.won: bool = False
        self.pnl: float = 0.0
        # Features
        self.hour: int = 0
        self.cross_coin_ups: int = 0       # how many coins went UP in prev cycle
        self.trailing_mixing: float = 0.0   # fraction of alternations in last N outcomes
        self.ask_level: float = 0.0         # entry ask price
        self.intra_vol: float = 0.0         # intra-cycle price std
        self.day_label: str = ""            # date string
        self.streak_len: int = 0            # current UP streak length
        self.price_trend: float = 0.0       # average UP ask over last 3 cycles minus current


# ---------------------------------------------------------------------------
# Build trades with features
# ---------------------------------------------------------------------------
def build_trades_with_features(
    csv_paths: List[str],
    coins_filter: Optional[List[str]] = None,
) -> List[FeatureTrade]:
    """Run UU→DOWN backtest and attach features to each trade."""
    df = load_data(csv_paths)
    cycles = get_cycles(df, min_rows=20)
    
    # Resolve outcomes
    all_gamma = resolve_all_outcomes(cycles)
    all_history = [
        determine_history_outcomes(cycle, gamma)
        for cycle, gamma in zip(cycles, all_gamma)
    ]
    
    active_coins = [c.upper() for c in coins_filter] if coins_filter else list(COINS)
    
    # State
    history: Dict[str, deque] = {c: deque(maxlen=10) for c in COINS}
    # Track previous cycle outcomes for cross-coin feature
    prev_cycle_outcomes: Dict[str, Optional[str]] = {c: None for c in COINS}
    
    trades: List[FeatureTrade] = []
    
    for cycle_idx, (cycle, trade_outcomes, hist_outcomes) in enumerate(
        zip(cycles, all_gamma, all_history)
    ):
        # --- Feature: cross-coin ups (from PREVIOUS cycle) ---
        cross_ups = sum(1 for c in COINS if prev_cycle_outcomes.get(c) == "UP")
        
        # --- Feature: time of day ---
        cycle_start_ts = cycle["cycle_start"].iloc[0]
        hour = cycle_start_ts.hour
        day_label = str(cycle_start_ts.date())
        
        # --- Check pattern matches ---
        for coin in active_coins:
            hist = list(history[coin])
            if len(hist) < 2:
                continue
            recent = "".join(hist[-2:])
            if recent != PATTERN:
                continue
            
            # Get outcome
            coin_outcome = trade_outcomes.get(coin)
            if coin_outcome is None:
                continue
            
            # Get entry price
            entry_price = get_entry_price(cycle, coin, BUY_SIDE)
            if entry_price is None:
                continue
            if entry_price > MAX_ASK:
                continue
            
            # Cost and PnL
            fill_price = entry_price + SLIPPAGE
            cost = fill_price * 5.0  # flat $5
            won = (BUY_SIDE == coin_outcome)
            if won:
                effective_shares = 5.0 * (1.0 - FEE_RATE)
                pnl = effective_shares - cost
            else:
                pnl = -cost
            
            # --- Feature: streak length ---
            streak = 0
            for ch in reversed(hist):
                if ch == "U":
                    streak += 1
                else:
                    break
            
            # --- Feature: trailing mixing (alternation rate in last 6 outcomes) ---
            mixing = 0.0
            if len(hist) >= 3:
                alternations = sum(1 for i in range(1, len(hist)) if hist[i] != hist[i-1])
                mixing = alternations / (len(hist) - 1)
            
            # --- Feature: intra-cycle volatility ---
            cl = coin.lower()
            up_ask_col = f"{cl}_up_ask"
            early = cycle[cycle["seconds_elapsed"] <= 60]
            intra_vol = early[up_ask_col].std() if len(early) > 5 else 0.0
            
            # --- Feature: price trend (avg UP ask last 3 cycles vs current) ---
            price_trend = 0.0
            if cycle_idx >= 3:
                recent_asks = []
                for prev_i in range(max(0, cycle_idx - 3), cycle_idx):
                    prev_cycle = cycles[prev_i]
                    prev_early = prev_cycle[prev_cycle["seconds_elapsed"] <= 30]
                    if len(prev_early) > 0:
                        recent_asks.append(prev_early[up_ask_col].mean())
                current_early = cycle[cycle["seconds_elapsed"] <= 30]
                if recent_asks and len(current_early) > 0:
                    current_ask = current_early[up_ask_col].mean()
                    price_trend = current_ask - np.mean(recent_asks)
            
            # Build trade
            t = FeatureTrade()
            t.cycle_idx = cycle_idx
            t.cycle_start = str(cycle_start_ts)
            t.coin = coin
            t.entry_price = entry_price
            t.won = won
            t.pnl = pnl
            t.hour = hour
            t.cross_coin_ups = cross_ups
            t.trailing_mixing = mixing
            t.ask_level = entry_price
            t.intra_vol = intra_vol if not np.isnan(intra_vol) else 0.0
            t.day_label = day_label
            t.streak_len = streak
            t.price_trend = price_trend
            trades.append(t)
        
        # --- Update history and prev outcomes ---
        for coin in COINS:
            outcome = hist_outcomes.get(coin)
            if outcome == "UP":
                history[coin].append("U")
            elif outcome == "DOWN":
                history[coin].append("D")
            prev_cycle_outcomes[coin] = trade_outcomes.get(coin)
    
    return trades


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------
def analyze_bins(
    trades: List[FeatureTrade],
    feature_name: str,
    bin_func,
    min_trades: int = 10,
) -> None:
    """Analyze win rate by bins defined by bin_func."""
    # Group trades by bin
    bins: Dict[str, List[FeatureTrade]] = {}
    for t in trades:
        b = bin_func(t)
        if b is None:
            continue
        bins.setdefault(b, []).append(t)
    
    if not bins:
        print(f"  No data for {feature_name}")
        return
    
    print(f"\n  --- {feature_name} ---")
    print(f"  {'Bin':<20} {'Trades':>6} {'Wins':>5} {'Loss':>5} {'WR':>6} {'PnL':>10} {'Avg':>10} {'p-val':>8}")
    print(f"  {'-'*20} {'-'*6} {'-'*5} {'-'*5} {'-'*6} {'-'*10} {'-'*10} {'-'*8}")
    
    overall_wr = sum(1 for t in trades if t.won) / len(trades) if trades else 0.5
    
    sorted_bins = sorted(bins.keys())
    for b in sorted_bins:
        bt = bins[b]
        n = len(bt)
        if n < min_trades:
            continue
        w = sum(1 for t in bt if t.won)
        l = n - w
        wr = w / n
        pnl = sum(t.pnl for t in bt)
        avg = pnl / n
        
        # Binomial test: is this bin's WR different from overall?
        p_val = stats.binomtest(w, n, overall_wr).pvalue if n >= 5 else 1.0
        
        sig = ""
        if p_val < 0.01:
            sig = " **"
        elif p_val < 0.05:
            sig = " *"
        
        print(
            f"  {b:<20} {n:>6} {w:>5} {l:>5} {wr:>5.0%} "
            f"${pnl:>+9.2f} ${avg:>+9.4f} {p_val:>7.4f}{sig}"
        )


def rolling_wr_analysis(trades: List[FeatureTrade], window: int = 20) -> None:
    """Analyze if trailing win rate predicts future success."""
    print(f"\n  --- Trailing WR (window={window}) vs Next Trade ---")
    
    if len(trades) < window + 10:
        print(f"  Not enough trades (need {window + 10}, have {len(trades)})")
        return
    
    # Compute trailing WR for each trade
    trailing_wrs = []
    for i in range(window, len(trades)):
        prev_wins = sum(1 for t in trades[i-window:i] if t.won)
        trailing_wr = prev_wins / window
        trailing_wrs.append((trailing_wr, trades[i].won, trades[i].pnl))
    
    # Bin by trailing WR
    bins = {"WR<45%": [], "45-55%": [], "55-65%": [], "WR>65%": []}
    for wr, won, pnl in trailing_wrs:
        if wr < 0.45:
            bins["WR<45%"].append((won, pnl))
        elif wr < 0.55:
            bins["45-55%"].append((won, pnl))
        elif wr < 0.65:
            bins["55-65%"].append((won, pnl))
        else:
            bins["WR>65%"].append((won, pnl))
    
    print(f"  {'Trailing WR':<15} {'Trades':>6} {'NextWR':>7} {'Avg PnL':>10} {'Interpretation'}")
    print(f"  {'-'*15} {'-'*6} {'-'*7} {'-'*10} {'-'*30}")
    
    for label in ["WR<45%", "45-55%", "55-65%", "WR>65%"]:
        bt = bins[label]
        if len(bt) < 5:
            continue
        n = len(bt)
        w = sum(1 for won, _ in bt if won)
        wr = w / n
        avg_pnl = sum(pnl for _, pnl in bt) / n
        
        # Is the trailing WR predictive?
        if label == "WR<45%":
            interp = "Cold streak -> ?"
        elif label == "WR>65%":
            interp = "Hot streak -> ?"
        else:
            interp = "Normal"
        
        print(f"  {label:<15} {n:>6} {wr:>6.0%} ${avg_pnl:>+9.4f} {interp}")


def temporal_stability(trades: List[FeatureTrade]) -> None:
    """Check if the edge is stable across time periods."""
    print(f"\n  --- Temporal Stability (by day) ---")
    
    days: Dict[str, List[FeatureTrade]] = {}
    for t in trades:
        days.setdefault(t.day_label, []).append(t)
    
    print(f"  {'Day':<12} {'Trades':>6} {'Wins':>5} {'WR':>6} {'PnL':>10} {'Avg':>10}")
    print(f"  {'-'*12} {'-'*6} {'-'*5} {'-'*6} {'-'*10} {'-'*10}")
    
    for day in sorted(days.keys()):
        dt = days[day]
        n = len(dt)
        w = sum(1 for t in dt if t.won)
        wr = w / n if n > 0 else 0
        pnl = sum(t.pnl for t in dt)
        avg = pnl / n if n > 0 else 0
        print(f"  {day:<12} {n:>6} {w:>5} {wr:>5.0%} ${pnl:>+9.2f} ${avg:>+9.4f}")


def streak_context_analysis(trades: List[FeatureTrade]) -> None:
    """Analyze: does the UP streak length at entry affect outcome?"""
    print(f"\n  --- Streak Length at Entry (current UP streak) ---")
    
    bins: Dict[int, List[FeatureTrade]] = {}
    for t in trades:
        bins.setdefault(t.streak_len, []).append(t)
    
    print(f"  {'Streak':>8} {'Trades':>6} {'WR':>6} {'PnL':>10} {'Avg':>10} {'Note'}")
    print(f"  {'-'*8} {'-'*6} {'-'*6} {'-'*10} {'-'*10} {'-'*20}")
    
    for s in sorted(bins.keys()):
        bt = bins[s]
        n = len(bt)
        if n < 3:
            continue
        w = sum(1 for t in bt if t.won)
        wr = w / n
        pnl = sum(t.pnl for t in bt)
        avg = pnl / n
        
        note = ""
        if s == 2:
            note = "= UU (base entry)"
        elif s == 3:
            note = "= UUU (Kelly 1.5x)"
        elif s >= 4:
            note = f"= {'U'*s} (Kelly {1+0.5*(s-2):.1f}x)"
        
        print(f"  {s:>8} {n:>6} {wr:>5.0%} ${pnl:>+9.2f} ${avg:>+9.4f} {note}")


def per_coin_per_feature(trades: List[FeatureTrade]) -> None:
    """Per-coin breakdown of key features."""
    coins = sorted(set(t.coin for t in trades))
    
    for coin in coins:
        ct = [t for t in trades if t.coin == coin]
        if not ct:
            continue
        
        print(f"\n  === {coin} ({len(ct)} trades, "
              f"{sum(1 for t in ct if t.won)}W/{sum(1 for t in ct if not t.won)}L, "
              f"{sum(1 for t in ct if t.won)/len(ct):.0%} WR) ===")
        
        # Streak analysis per coin
        streak_bins: Dict[int, List[FeatureTrade]] = {}
        for t in ct:
            streak_bins.setdefault(t.streak_len, []).append(t)
        
        print(f"  {'Streak':>8} {'N':>4} {'WR':>6} {'PnL':>8}")
        for s in sorted(streak_bins.keys()):
            bt = streak_bins[s]
            n = len(bt)
            if n < 2:
                continue
            w = sum(1 for t in bt if t.won)
            wr = w / n
            pnl = sum(t.pnl for t in bt)
            print(f"  {s:>8} {n:>4} {wr:>5.0%} ${pnl:>+7.2f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Regime analysis for UU→DOWN strategy")
    parser.add_argument("csv_files", nargs="+", help="CSV price data files")
    parser.add_argument("--coins", type=str, default=None,
                        help="Comma-separated coin filter (e.g. BTC,ETH)")
    args = parser.parse_args()
    
    for f in args.csv_files:
        if not Path(f).exists():
            print(f"ERROR: File not found: {f}")
            sys.exit(1)
    
    coins_filter = None
    if args.coins:
        coins_filter = [c.strip().upper() for c in args.coins.split(",")]
    
    coin_label = ",".join(coins_filter) if coins_filter else "ALL"
    print(f"\n  Building trades for UU->DOWN on {coin_label}...")
    trades = build_trades_with_features(args.csv_files, coins_filter)
    
    if not trades:
        print("  No trades found!")
        return
    
    total_w = sum(1 for t in trades if t.won)
    total_l = len(trades) - total_w
    total_pnl = sum(t.pnl for t in trades)
    print(f"  {len(trades)} trades | {total_w}W/{total_l}L | "
          f"WR={total_w/len(trades):.1%} | PnL=${total_pnl:+.2f}")
    
    print("\n" + "=" * 75)
    print("  REGIME ANALYSIS: UU->DOWN")
    print("=" * 75)
    
    # 1. Temporal stability
    temporal_stability(trades)
    
    # 2. Time of day
    analyze_bins(trades, "Time of Day (hour UTC)", lambda t: f"{t.hour:02d}:00")
    
    # 3. Cross-coin unanimity
    analyze_bins(trades, "Cross-Coin UPs (prev cycle)",
                 lambda t: f"{t.cross_coin_ups} coins UP")
    
    # 4. Entry ask price
    def ask_bin(t):
        if t.ask_level < 0.48:
            return "a) <0.48 (cheap)"
        elif t.ask_level < 0.52:
            return "b) 0.48-0.52"
        elif t.ask_level < 0.56:
            return "c) 0.52-0.56"
        else:
            return "d) 0.56-0.60 (expensive)"
    analyze_bins(trades, "Entry Ask Price", ask_bin)
    
    # 5. Trailing mixing (alternation rate)
    def mixing_bin(t):
        if t.trailing_mixing < 0.40:
            return "a) <40% (trending)"
        elif t.trailing_mixing < 0.60:
            return "b) 40-60% (normal)"
        else:
            return "c) >60% (choppy)"
    analyze_bins(trades, "Trailing Alternation Rate", mixing_bin)
    
    # 6. Intra-cycle volatility
    def vol_bin(t):
        if t.intra_vol < 0.02:
            return "a) Low vol (<0.02)"
        elif t.intra_vol < 0.05:
            return "b) Med vol (0.02-0.05)"
        else:
            return "c) High vol (>0.05)"
    analyze_bins(trades, "Intra-Cycle Volatility", vol_bin)
    
    # 7. Price trend
    def trend_bin(t):
        if t.price_trend < -0.03:
            return "a) Falling (< -3%)"
        elif t.price_trend < 0.03:
            return "b) Flat (-3% to +3%)"
        else:
            return "c) Rising (> +3%)"
    analyze_bins(trades, "Price Trend (3-cycle)", trend_bin)
    
    # 8. Streak length at entry
    streak_context_analysis(trades)
    
    # 9. Rolling WR
    rolling_wr_analysis(trades, window=15)
    
    # 10. Per-coin breakdown with features
    per_coin_per_feature(trades)
    
    print("\n" + "=" * 75)
    print("  INTERPRETATION GUIDE")
    print("=" * 75)
    print("""
  - p-val < 0.05*: This bin's WR is significantly different from average.
    A low WR bin with p<0.05 means: AVOID trading in this condition.
    A high WR bin with p<0.05 means: this condition HELPS the strategy.
  
  - Cross-coin UPs: If all 4 coins went UP last cycle (macro move), 
    mean-reversion may not apply (the move is fundamental, not noise).
  
  - Trailing WR: If the strategy has been losing recently, does it
    recover (mean-reversion of the strategy itself) or keep losing
    (regime change)?
  
  - Streak length: After UU (streak=2) vs UUU (streak=3) vs UUUU (streak=4).
    If WR drops at longer streaks, Kelly scaling is HARMFUL.
  
  - Time of day: Some hours may have more trending behavior (e.g., 
    US/EU market opens causing macro moves).
    """)
    print("=" * 75)


if __name__ == "__main__":
    main()
