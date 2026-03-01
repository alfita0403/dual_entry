"""
Pattern Backtester - Backtest autocorrelation patterns on historical CSV data.

Tests pattern-based strategies (DDD->BUY UP, UD->BUY DOWN, etc.) against
collected price data and produces:
  - Total trades, win rate, avg PnL per trade
  - Starting balance, ending balance, net profit
  - Max drawdown (peak-to-trough on equity curve)
  - Equity curve plot (saved as PNG)
  - Per-coin and per-pattern breakdowns
  - Trade list (optional)

Usage:
    python research/backtest_patterns.py data/prices_2026-02-28.csv
    python research/backtest_patterns.py data/prices_2026-02-28.csv data/prices_2026-03-01.csv
    python research/backtest_patterns.py data/prices_*.csv --pattern DDD,UD --size 5
    python research/backtest_patterns.py data/prices_*.csv --pattern DDD --side UP --size 10
    python research/backtest_patterns.py data/prices_*.csv --all-patterns
    python research/backtest_patterns.py data/prices_*.csv --trades  # print every trade
"""

import argparse
import sys
import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Path setup (so we can import from research/)
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research_v2 import load_data, get_cycles, COINS, UP_ASK

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Fee: ~1.5% taker fee on entry (Polymarket charges on shares received)
# No fee on win payout (settlement), full loss on loss.
FEE_RATE = 0.015

# Slippage: matches live strategy --slippage 0.03 (FOK limit above best ask)
SLIPPAGE = 0.03

# Entry timing: matches live strategy ENTRY_WINDOW_START = 5
ENTRY_TIME = 5

# Max ask price: don't enter when ask is too expensive (bad risk/reward)
DEFAULT_MAX_ASK = 0.60

# Outcome inference: last 5 seconds, strict thresholds (matches live strategy)
INFERENCE_START = 295      # seconds into cycle
UP_THRESHOLD = 0.95        # UP ask > this => UP
DOWN_THRESHOLD = 0.05      # UP ask < this => DOWN


def determine_outcomes(cycle: pd.DataFrame) -> Dict[str, Optional[str]]:
    """Determine cycle outcomes using strict thresholds on last 5 seconds.

    Matches the live strategy (sequence.py) exactly:
    t >= 295s, UP ask > 0.95 => UP, UP ask < 0.05 => DOWN, else None.
    """
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


# Built-in pattern defaults
BUILTIN_PATTERNS: Dict[str, str] = {
    "DDD":  "UP",
    "UD":   "DOWN",
    "UUU":  "DOWN",
    "DU":   "UP",
    "DDDD": "UP",
    "DD":   "UP",
    "UU":   "DOWN",
    "UDU":  "UP",
    "DUD":  "DOWN",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class PatternRule:
    pattern: str
    buy_side: str  # "UP" or "DOWN"


@dataclass
class Trade:
    cycle_idx: int
    cycle_start: str
    coin: str
    pattern: str
    buy_side: str       # "UP" or "DOWN"
    entry_price: float  # ask price at entry (before slippage)
    cost: float         # actual cost = (entry_price + slippage) * size * (1 + fee)
    size: float
    outcome: str        # "UP" or "DOWN" (actual resolution)
    won: bool
    pnl: float          # size * 1.0 - cost if won, -cost if lost
    balance_after: float = 0.0


@dataclass
class BacktestResult:
    rules: List[PatternRule]
    size: float
    initial_balance: float
    final_balance: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_pnl: float
    total_pnl: float
    max_drawdown: float
    max_drawdown_pct: float
    max_ask: float = DEFAULT_MAX_ASK
    # Breakdowns
    per_pattern: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    per_coin: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Equity curve
    equity_curve: List[float] = field(default_factory=list)
    trades: List[Trade] = field(default_factory=list)
    # Data info
    total_cycles: int = 0
    cycles_with_signal: int = 0


# ---------------------------------------------------------------------------
# Core backtest logic
# ---------------------------------------------------------------------------
def get_entry_price(cycle: pd.DataFrame, coin: str, side: str) -> Optional[float]:
    """Get the ask price for a coin/side at entry time (t=5-8s into cycle).

    Matches live strategy: ENTRY_WINDOW_START=5, bot enters on first tick.
    Uses a tight 3-second window around ENTRY_TIME to get realistic fill price.

    For UP: reads {coin}_up_ask directly.
    For DOWN: derives from UP bid -> DOWN ask = 1 - UP bid.
    """
    # Tight window: t=5 to t=8 (matches live bot entering at first opportunity)
    entry_rows = cycle[
        (cycle["seconds_elapsed"] >= ENTRY_TIME) &
        (cycle["seconds_elapsed"] <= ENTRY_TIME + 3)
    ]
    if entry_rows.empty:
        # Fallback: first 10 seconds
        entry_rows = cycle[cycle["seconds_elapsed"] <= 10]
    if entry_rows.empty:
        return None

    cl = coin.lower()
    if side == "UP":
        col = f"{cl}_up_ask"
        price = entry_rows[col].mean()
    else:
        # DOWN ask ~ 1 - UP bid
        col = f"{cl}_up_bid"
        bid = entry_rows[col].mean()
        if bid <= 0:
            return None
        price = 1.0 - bid

    if price <= 0.01 or price >= 0.99:
        return None
    return price


def run_backtest(
    csv_paths: List[str],
    rules: List[PatternRule],
    size: float = 5.0,
    initial_balance: float = 100.0,
    max_trades_per_cycle: int = 4,
    max_ask: float = DEFAULT_MAX_ASK,
) -> BacktestResult:
    """Run pattern backtest on historical CSV data."""

    # Load and process data
    df = load_data(csv_paths)
    cycles = get_cycles(df, min_rows=20)

    # Determine outcomes for all cycles
    all_outcomes = [determine_outcomes(c) for c in cycles]

    # State: rolling history per coin
    max_pattern_len = max(len(r.pattern) for r in rules)
    history: Dict[str, deque] = {c: deque(maxlen=max_pattern_len + 1) for c in COINS}

    balance = initial_balance
    equity_curve = [balance]
    trades: List[Trade] = []
    peak_balance = balance
    max_dd = 0.0
    cycles_with_signal = 0

    for cycle_idx, (cycle, outcomes) in enumerate(zip(cycles, all_outcomes)):
        # --- Check for pattern matches BEFORE recording this cycle's outcome ---
        # (We match on history from PREVIOUS cycles, then buy in THIS cycle)
        matched_coins: set = set()
        cycle_trades: List[Tuple[str, PatternRule]] = []

        for coin in COINS:
            hist = list(history[coin])
            if not hist:
                continue
            for rule in rules:
                plen = len(rule.pattern)
                if len(hist) < plen:
                    continue
                recent = "".join(hist[-plen:])
                if recent == rule.pattern:
                    cycle_trades.append((coin, rule))
                    matched_coins.add(coin)
                    break  # first matching rule per coin

        # --- Execute trades for this cycle ---
        if cycle_trades:
            cycles_with_signal += 1

        trades_this_cycle = 0
        for coin, rule in cycle_trades:
            if trades_this_cycle >= max_trades_per_cycle:
                break

            # Get this cycle's outcome for the coin
            coin_outcome = outcomes.get(coin)
            if coin_outcome is None:
                continue  # ambiguous outcome, skip

            # Get entry price
            entry_price = get_entry_price(cycle, coin, rule.buy_side)
            if entry_price is None:
                continue

            # Max ask filter: skip expensive entries with bad risk/reward
            if entry_price > max_ask:
                continue

            # Cost calculation
            fill_price = entry_price + SLIPPAGE
            cost = fill_price * size

            # Check if we can afford this trade
            if cost > balance:
                continue

            # Determine win/loss
            won = (rule.buy_side == coin_outcome)
            if won:
                payout = size * 1.0  # binary: win pays $1 per share
                # Fee is deducted from shares received on entry
                # Effective shares = size * (1 - FEE_RATE) but payout is per
                # original size. Actually in Polymarket, fee reduces shares:
                # you pay cost but receive size * (1-fee) shares.
                # If won: payout = size * (1-fee) * 1.0
                effective_shares = size * (1.0 - FEE_RATE)
                payout = effective_shares * 1.0
                pnl = payout - cost
            else:
                pnl = -cost

            balance += pnl

            trade = Trade(
                cycle_idx=cycle_idx,
                cycle_start=str(cycle["cycle_start"].iloc[0]),
                coin=coin,
                pattern=rule.pattern,
                buy_side=rule.buy_side,
                entry_price=entry_price,
                cost=cost,
                size=size,
                outcome=coin_outcome,
                won=won,
                pnl=pnl,
                balance_after=balance,
            )
            trades.append(trade)
            trades_this_cycle += 1

        equity_curve.append(balance)

        # Update peak and drawdown
        if balance > peak_balance:
            peak_balance = balance
        dd = peak_balance - balance
        if dd > max_dd:
            max_dd = dd

        # --- Record this cycle's outcomes into history ---
        for coin in COINS:
            outcome = outcomes.get(coin)
            if outcome == "UP":
                history[coin].append("U")
            elif outcome == "DOWN":
                history[coin].append("D")
            # None: don't record (breaks chains intentionally)

    # --- Compute stats ---
    total_trades = len(trades)
    wins = sum(1 for t in trades if t.won)
    losses = total_trades - wins
    win_rate = wins / total_trades if total_trades > 0 else 0.0
    total_pnl = balance - initial_balance
    avg_pnl = total_pnl / total_trades if total_trades > 0 else 0.0
    max_dd_pct = (max_dd / peak_balance * 100) if peak_balance > 0 else 0.0

    # Per-pattern breakdown
    per_pattern: Dict[str, Dict[str, Any]] = {}
    for rule in rules:
        p = rule.pattern
        pt = [t for t in trades if t.pattern == p]
        pw = sum(1 for t in pt if t.won)
        pl = len(pt) - pw
        ppnl = sum(t.pnl for t in pt)
        per_pattern[p] = {
            "side": rule.buy_side,
            "trades": len(pt),
            "wins": pw,
            "losses": pl,
            "win_rate": pw / len(pt) if pt else 0.0,
            "total_pnl": ppnl,
            "avg_pnl": ppnl / len(pt) if pt else 0.0,
        }

    # Per-coin breakdown
    per_coin: Dict[str, Dict[str, Any]] = {}
    for coin in COINS:
        ct = [t for t in trades if t.coin == coin]
        cw = sum(1 for t in ct if t.won)
        cl = len(ct) - cw
        cpnl = sum(t.pnl for t in ct)
        per_coin[coin] = {
            "trades": len(ct),
            "wins": cw,
            "losses": cl,
            "win_rate": cw / len(ct) if ct else 0.0,
            "total_pnl": cpnl,
            "avg_pnl": cpnl / len(ct) if ct else 0.0,
        }

    return BacktestResult(
        rules=rules,
        size=size,
        initial_balance=initial_balance,
        final_balance=balance,
        total_trades=total_trades,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        avg_pnl=avg_pnl,
        total_pnl=total_pnl,
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct,
        max_ask=max_ask,
        per_pattern=per_pattern,
        per_coin=per_coin,
        equity_curve=equity_curve,
        trades=trades,
        total_cycles=len(cycles),
        cycles_with_signal=cycles_with_signal,
    )


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------
def print_results(result: BacktestResult, show_trades: bool = False) -> None:
    """Print backtest results to console."""
    rules_str = ", ".join(f"{r.pattern}->BUY {r.buy_side}" for r in result.rules)

    print()
    print("=" * 70)
    print("  PATTERN BACKTEST RESULTS")
    print("=" * 70)
    print(f"  Rules:            {rules_str}")
    print(f"  Size per trade:   ${result.size:.2f}")
    print(f"  Max ask:          {result.max_ask:.2f}")
    print(f"  Fee rate:         {FEE_RATE:.1%}")
    print(f"  Slippage:         ${SLIPPAGE:.2f}")
    print(f"  Inference:        t>={INFERENCE_START}s, UP>{UP_THRESHOLD}, DOWN<{DOWN_THRESHOLD}")
    print(f"  Total cycles:     {result.total_cycles}")
    print(f"  Cycles w/ signal: {result.cycles_with_signal}")
    print()
    print(f"  --- P&L ---")
    print(f"  Initial balance:  ${result.initial_balance:.2f}")
    print(f"  Final balance:    ${result.final_balance:.2f}")
    print(f"  Net profit:       ${result.total_pnl:+.2f}")
    print(f"  ROI:              {result.total_pnl / result.initial_balance * 100:+.1f}%")
    print()
    print(f"  --- Trades ---")
    print(f"  Total trades:     {result.total_trades}")
    print(f"  Wins:             {result.wins}")
    print(f"  Losses:           {result.losses}")
    print(f"  Win rate:         {result.win_rate:.1%}")
    print(f"  Avg PnL/trade:    ${result.avg_pnl:+.4f}")
    print()
    print(f"  --- Risk ---")
    print(f"  Max drawdown:     ${result.max_drawdown:.2f}")
    print(f"  Max drawdown %:   {result.max_drawdown_pct:.1f}%")

    # Per-pattern
    if result.per_pattern:
        print()
        print(f"  --- Per Pattern ---")
        print(f"  {'Pattern':<8} {'Side':<6} {'Trades':>6} {'Wins':>5} {'Loss':>5} {'WR':>6} {'PnL':>10} {'Avg':>10}")
        print(f"  {'-'*8} {'-'*6} {'-'*6} {'-'*5} {'-'*5} {'-'*6} {'-'*10} {'-'*10}")
        for pattern, stats in result.per_pattern.items():
            print(
                f"  {pattern:<8} {stats['side']:<6} {stats['trades']:>6} "
                f"{stats['wins']:>5} {stats['losses']:>5} "
                f"{stats['win_rate']:>5.0%} "
                f"${stats['total_pnl']:>+9.2f} "
                f"${stats['avg_pnl']:>+9.4f}"
            )

    # Per-coin
    if result.per_coin:
        print()
        print(f"  --- Per Coin ---")
        print(f"  {'Coin':<6} {'Trades':>6} {'Wins':>5} {'Loss':>5} {'WR':>6} {'PnL':>10} {'Avg':>10}")
        print(f"  {'-'*6} {'-'*6} {'-'*5} {'-'*5} {'-'*6} {'-'*10} {'-'*10}")
        for coin, stats in result.per_coin.items():
            if stats["trades"] == 0:
                continue
            print(
                f"  {coin:<6} {stats['trades']:>6} "
                f"{stats['wins']:>5} {stats['losses']:>5} "
                f"{stats['win_rate']:>5.0%} "
                f"${stats['total_pnl']:>+9.2f} "
                f"${stats['avg_pnl']:>+9.4f}"
            )

    # Trade list
    if show_trades and result.trades:
        print()
        print(f"  --- Trade List ---")
        print(
            f"  {'#':>3} {'Cycle':<22} {'Coin':<5} {'Pat':<5} "
            f"{'Side':<5} {'Ask':>6} {'Cost':>7} {'Out':>5} {'W/L':>4} "
            f"{'PnL':>8} {'Bal':>9}"
        )
        print(f"  {'-'*3} {'-'*22} {'-'*5} {'-'*5} {'-'*5} {'-'*6} {'-'*7} {'-'*5} {'-'*4} {'-'*8} {'-'*9}")
        for i, t in enumerate(result.trades):
            wl = "W" if t.won else "L"
            print(
                f"  {i+1:>3} {t.cycle_start:<22} {t.coin:<5} {t.pattern:<5} "
                f"{t.buy_side:<5} {t.entry_price:>5.3f} "
                f"${t.cost:>6.2f} {t.outcome:>5} {wl:>4} "
                f"${t.pnl:>+7.2f} ${t.balance_after:>8.2f}"
            )

    print()
    print("=" * 70)


def save_equity_curve(result: BacktestResult, output_path: str) -> None:
    """Save equity curve as PNG."""
    try:
        import matplotlib
        matplotlib.use("Agg")  # non-interactive backend
        import matplotlib.pyplot as plt
    except ImportError:
        print("  [!] matplotlib not installed, skipping equity curve plot")
        return

    fig, ax = plt.subplots(figsize=(12, 5))

    x = range(len(result.equity_curve))
    ax.plot(x, result.equity_curve, linewidth=1.2, color="#2196F3")
    ax.axhline(
        y=result.initial_balance, color="gray", linestyle="--",
        linewidth=0.8, alpha=0.6, label=f"Initial: ${result.initial_balance:.0f}",
    )
    ax.fill_between(
        x, result.initial_balance, result.equity_curve,
        where=[v >= result.initial_balance for v in result.equity_curve],
        alpha=0.15, color="green",
    )
    ax.fill_between(
        x, result.initial_balance, result.equity_curve,
        where=[v < result.initial_balance for v in result.equity_curve],
        alpha=0.15, color="red",
    )

    # Mark trades on the equity curve
    for trade in result.trades:
        # trade index in equity curve is cycle_idx + 1 (curve starts at initial)
        idx = trade.cycle_idx + 1
        if idx < len(result.equity_curve):
            color = "green" if trade.won else "red"
            ax.scatter(idx, result.equity_curve[idx], color=color, s=15, zorder=5, alpha=0.7)

    rules_str = ", ".join(f"{r.pattern}->{r.buy_side}" for r in result.rules)
    ax.set_title(
        f"Equity Curve: {rules_str}  |  "
        f"{result.total_trades} trades  |  "
        f"WR: {result.win_rate:.0%}  |  "
        f"PnL: ${result.total_pnl:+.2f}  |  "
        f"MaxDD: ${result.max_drawdown:.2f} ({result.max_drawdown_pct:.1f}%)",
        fontsize=10,
    )
    ax.set_xlabel("Cycle #")
    ax.set_ylabel("Balance ($)")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()
    print(f"  Equity curve saved: {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest autocorrelation patterns on historical price CSV data"
    )
    parser.add_argument(
        "csv_files", nargs="+",
        help="CSV file(s) with price data (e.g. data/prices_2026-02-28.csv)",
    )
    parser.add_argument(
        "--pattern", type=str, default="DDD,UD",
        help=(
            "Pattern(s) to test, comma-separated. "
            "Default: DDD,UD. Use --all-patterns to test all built-in patterns."
        ),
    )
    parser.add_argument(
        "--side", type=str, default=None,
        help=(
            "Side(s) to buy, comma-separated. Must match --pattern length. "
            "If omitted, uses built-in defaults."
        ),
    )
    parser.add_argument(
        "--all-patterns", action="store_true",
        help="Test ALL built-in patterns (DDD, UD, UUU, DU, DDDD, DD, UU, UDU, DUD)",
    )
    parser.add_argument(
        "--size", type=float, default=5.0,
        help="Shares per trade (default: 5)",
    )
    parser.add_argument(
        "--max-ask", type=float, default=DEFAULT_MAX_ASK,
        help=f"Max ask price to enter (default: {DEFAULT_MAX_ASK}). Rejects expensive entries.",
    )
    parser.add_argument(
        "--balance", type=float, default=100.0,
        help="Starting balance in USD (default: 100)",
    )
    parser.add_argument(
        "--max-trades", type=int, default=4,
        help="Max trades per cycle (default: 4)",
    )
    parser.add_argument(
        "--trades", action="store_true",
        help="Print every individual trade",
    )
    parser.add_argument(
        "--no-plot", action="store_true",
        help="Skip equity curve plot",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output path for equity curve PNG (default: auto-generated)",
    )

    args = parser.parse_args()

    # Validate CSV files exist
    for f in args.csv_files:
        if not Path(f).exists():
            print(f"ERROR: File not found: {f}")
            sys.exit(1)

    # Parse rules
    if args.all_patterns:
        rules = [PatternRule(p, s) for p, s in BUILTIN_PATTERNS.items()]
    else:
        patterns = [p.strip().upper() for p in args.pattern.split(",")]
        if args.side:
            sides = [s.strip().upper() for s in args.side.split(",")]
            if len(sides) != len(patterns):
                print(f"ERROR: --side has {len(sides)} entries but --pattern has {len(patterns)}")
                sys.exit(1)
        else:
            sides = []
            for p in patterns:
                if p in BUILTIN_PATTERNS:
                    sides.append(BUILTIN_PATTERNS[p])
                else:
                    print(
                        f"ERROR: No built-in default side for pattern '{p}'. "
                        f"Use --side to specify. Known: {list(BUILTIN_PATTERNS.keys())}"
                    )
                    sys.exit(1)
        rules = [PatternRule(p, s) for p, s in zip(patterns, sides)]

    print(f"\n  Loading {len(args.csv_files)} CSV file(s)...")

    # Run backtest
    result = run_backtest(
        csv_paths=args.csv_files,
        rules=rules,
        size=args.size,
        initial_balance=args.balance,
        max_trades_per_cycle=args.max_trades,
        max_ask=args.max_ask,
    )

    # Print results
    print_results(result, show_trades=args.trades)

    # Equity curve
    if not args.no_plot:
        if args.output:
            plot_path = args.output
        else:
            pattern_tag = "_".join(r.pattern for r in rules)
            plot_path = f"research/equity_{pattern_tag}.png"
        save_equity_curve(result, plot_path)


if __name__ == "__main__":
    main()
