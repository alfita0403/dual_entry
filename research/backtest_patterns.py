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
import json
import sys
import os
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
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

# Outcome inference constants
INFERENCE_START = 295      # seconds into cycle
UP_THRESHOLD = 0.95        # UP ask > this => UP
DOWN_THRESHOLD = 0.05      # UP ask < this => DOWN

# Smart hybrid: prices in this range are "genuinely ambiguous" — both CSV and
# live WS are unlikely to resolve clearly. Calibrated against live trade log.
AMBIG_LOW = 0.25           # below this: borderline, use Gamma
AMBIG_HIGH = 0.75          # above this: borderline, use Gamma


def determine_outcomes_legacy(cycle: pd.DataFrame) -> Dict[str, Optional[str]]:
    """LEGACY: Determine cycle outcomes using strict thresholds on last 5 seconds.

    This method is known to diverge from live execution because CSV snapshots
    (1/sec) can differ from real-time WebSocket prices at the threshold boundary.
    Use --legacy-inference to enable.
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


def determine_history_outcomes(
    cycle: pd.DataFrame,
    gamma_outcomes: Dict[str, Optional[str]],
) -> Dict[str, Optional[str]]:
    """Smart hybrid: determine which outcomes to record in pattern history.

    Replicates the live bot's behavior:
      - CSV clearly extreme (>0.95 or <0.05): record from CSV
      - CSV no data at t>=295: use Gamma (data gap, live WS had data)
      - CSV borderline (0.05-0.25 or 0.75-0.95): use Gamma (WS likely saw extreme)
      - CSV genuinely ambiguous (0.25-0.75): DON'T record (live bot also ambiguous)

    This is used ONLY for building pattern history chains. Trade win/loss
    always uses Gamma resolution (ground truth).
    """
    late = cycle[cycle["seconds_elapsed"] >= INFERENCE_START]
    out: Dict[str, Optional[str]] = {}

    for c in COINS:
        cl = c.lower()

        if len(late) < 1:
            # No CSV data at end of cycle -> use Gamma (data gap)
            out[c] = gamma_outcomes.get(c)
            continue

        last_val = late[f"{cl}_up_ask"].iloc[-1]

        if last_val > UP_THRESHOLD:
            out[c] = "UP"
        elif last_val < DOWN_THRESHOLD:
            out[c] = "DOWN"
        elif last_val <= AMBIG_LOW or last_val >= AMBIG_HIGH:
            # Borderline: WS probably caught extreme -> use Gamma
            out[c] = gamma_outcomes.get(c)
        else:
            # Genuinely ambiguous (0.25-0.75): live bot also couldn't determine
            out[c] = None

    return out


# ---------------------------------------------------------------------------
# Gamma API resolution (ground truth — same as live bot)
# ---------------------------------------------------------------------------
GAMMA_API = "https://gamma-api.polymarket.com/markets/slug"
CACHE_FILE = Path(__file__).parent / "resolution_cache.json"
COIN_SLUG = {"BTC": "btc", "ETH": "eth", "SOL": "sol", "XRP": "xrp"}
API_DELAY = 0.1  # seconds between API calls (be nice to the server)


def _load_cache() -> Dict[str, str]:
    """Load resolution cache from disk."""
    if CACHE_FILE.exists():
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}


def _save_cache(cache: Dict[str, str]) -> None:
    """Persist resolution cache to disk."""
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def _ts_to_unix(ts: pd.Timestamp) -> int:
    """Convert tz-naive UTC Timestamp to Unix seconds."""
    return int(ts.timestamp())


def _resolve_slug(slug: str) -> Optional[str]:
    """Call Gamma API for one market slug. Returns 'UP', 'DOWN', or None."""
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


def resolve_all_outcomes(
    cycles: list,
    verbose: bool = True,
) -> List[Dict[str, Optional[str]]]:
    """Resolve all cycle outcomes via Gamma API with local caching.

    First run fetches from API (~0.1s per market, ~100s for 255 cycles x 4 coins).
    Subsequent runs are instant (reads from resolution_cache.json).
    """
    cache = _load_cache()
    all_outcomes: List[Dict[str, Optional[str]]] = []
    api_calls = 0
    cache_hits = 0

    for i, cycle in enumerate(cycles):
        ts = cycle["cycle_start"].iloc[0]
        unix = _ts_to_unix(ts)
        outcomes: Dict[str, Optional[str]] = {}

        for coin in COINS:
            slug = f"{COIN_SLUG[coin]}-updown-5m-{unix}"
            if slug in cache:
                val = cache[slug]
                outcomes[coin] = val if val in ("UP", "DOWN") else None
                cache_hits += 1
            else:
                result = _resolve_slug(slug)
                cache[slug] = result if result else "UNKNOWN"
                outcomes[coin] = result
                api_calls += 1
                if api_calls % 100 == 0:
                    _save_cache(cache)  # periodic save
                time.sleep(API_DELAY)

        all_outcomes.append(outcomes)

        if verbose and api_calls > 0 and (i + 1) % 25 == 0:
            print(f"    Resolved {i+1}/{len(cycles)} cycles ({api_calls} API calls)...")

    _save_cache(cache)
    if verbose:
        total = len(cycles) * len(COINS)
        print(
            f"    Resolution: {len(cycles)} cycles | "
            f"{cache_hits} cached, {api_calls} API calls | "
            f"{len(cache)} total entries in cache"
        )
    return all_outcomes


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
    coins: Optional[List[str]] = None  # None = all coins
    max_ask: Optional[float] = None    # None = use global default


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


def load_yaml_rules(yaml_path: str) -> Tuple[List[PatternRule], Dict[str, Any]]:
    """Load pattern rules from a mean_reversion YAML config.

    Returns (rules, config_dict) where config_dict has entry_window, size, etc.
    """
    with open(yaml_path) as f:
        cfg = yaml.safe_load(f)

    rules: List[PatternRule] = []
    for r in cfg.get("rules", []):
        pattern = r["pattern"].replace("U", "U").replace("D", "D")
        side_raw = r.get("side", "DOWN").upper()
        buy_side = side_raw  # "DOWN" or "UP"
        coins = [c.upper() for c in r.get("coins", [])] or None
        max_ask = r.get("max_ask")
        rules.append(PatternRule(pattern, buy_side, coins, max_ask))

    return rules, cfg


# ---------------------------------------------------------------------------
# Core backtest logic
# ---------------------------------------------------------------------------
def get_entry_price(
    cycle: pd.DataFrame, coin: str, side: str, entry_time: int = ENTRY_TIME,
) -> Optional[float]:
    """Get the ask price for a coin/side at entry time.

    Uses a tight 3-second window around entry_time to get realistic fill price.

    For UP: reads {coin}_up_ask directly.
    For DOWN: derives from UP bid -> DOWN ask = 1 - UP bid.
    """
    entry_rows = cycle[
        (cycle["seconds_elapsed"] >= entry_time) &
        (cycle["seconds_elapsed"] <= entry_time + 3)
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


def _streak_length(history: deque, direction: str) -> int:
    """Count consecutive occurrences of `direction` at the end of history.

    E.g. history = ['D','U','U','U'], direction='U' -> 3
    """
    count = 0
    for ch in reversed(history):
        if ch == direction:
            count += 1
        else:
            break
    return count


def run_backtest(
    csv_paths: List[str],
    rules: List[PatternRule],
    size: float = 5.0,
    initial_balance: float = 100.0,
    max_trades_per_cycle: int = 4,
    max_ask: float = DEFAULT_MAX_ASK,
    use_gamma: bool = True,
    coins_filter: Optional[List[str]] = None,
    kelly_scale: float = 0.0,
    entry_time: int = ENTRY_TIME,
) -> BacktestResult:
    """Run pattern backtest on historical CSV data.

    Two-phase outcome resolution (when use_gamma=True):
      1. TRADE OUTCOMES (win/loss): Always Gamma API — ground truth resolution.
      2. PATTERN HISTORY: Smart hybrid — CSV for clear prices, Gamma for
         borderline/missing data, skip genuinely ambiguous (0.25-0.75 zone).
         This replicates the live bot's WS-based inference behavior.
    """

    # Load and process data
    df = load_data(csv_paths)
    cycles = get_cycles(df, min_rows=20)

    # Phase 1: Resolve all outcomes via Gamma API (for trade win/loss)
    if use_gamma:
        all_outcomes_gamma = resolve_all_outcomes(cycles)
    else:
        all_outcomes_gamma = [determine_outcomes_legacy(c) for c in cycles]

    # Phase 2: Determine history outcomes (for pattern chain building)
    # When use_gamma=True, uses smart hybrid; otherwise uses same as legacy
    if use_gamma:
        all_outcomes_history = [
            determine_history_outcomes(cycle, gamma)
            for cycle, gamma in zip(cycles, all_outcomes_gamma)
        ]
    else:
        all_outcomes_history = all_outcomes_gamma  # legacy: same for both

    # Coin filter
    active_coins = [c.upper() for c in coins_filter] if coins_filter else list(COINS)

    # State: rolling history per coin
    max_pattern_len = max(len(r.pattern) for r in rules)
    history: Dict[str, deque] = {c: deque(maxlen=max_pattern_len + 1) for c in COINS}

    balance = initial_balance
    equity_curve = [balance]
    trades: List[Trade] = []
    peak_balance = balance
    max_dd = 0.0
    cycles_with_signal = 0

    for cycle_idx, (cycle, trade_outcomes, hist_outcomes) in enumerate(
        zip(cycles, all_outcomes_gamma, all_outcomes_history)
    ):
        # --- Check for pattern matches BEFORE recording this cycle's outcome ---
        # (We match on history from PREVIOUS cycles, then buy in THIS cycle)
        matched_coins: set = set()
        cycle_trades: List[Tuple[str, PatternRule]] = []

        for coin in active_coins:
            hist = list(history[coin])
            if not hist:
                continue
            for rule in rules:
                # Per-coin filter: skip if rule restricts to specific coins
                if rule.coins and coin not in rule.coins:
                    continue
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

            # Get this cycle's outcome for the coin (Gamma = ground truth)
            coin_outcome = trade_outcomes.get(coin)
            if coin_outcome is None:
                continue  # unresolved market, skip

            # Get entry price
            entry_price = get_entry_price(cycle, coin, rule.buy_side, entry_time)
            if entry_price is None:
                continue

            # Max ask filter: per-rule max_ask overrides global
            effective_max_ask = rule.max_ask if rule.max_ask is not None else max_ask
            if entry_price > effective_max_ask:
                continue

            # Kelly scaling: increase size based on streak length
            trade_size = size
            if kelly_scale > 0:
                # Detect streak: how many consecutive same-direction outcomes
                # For UU->DOWN: the pattern ends in 'U', streak of U's matters
                # For DDD->UP: the pattern ends in 'D', streak of D's matters
                last_char = rule.pattern[-1]  # 'U' or 'D'
                streak = _streak_length(history[coin], last_char)
                pat_len = len(rule.pattern)
                extra = max(0, streak - pat_len)  # extra beyond pattern
                # Scale: base * (1 + kelly_scale * extra)
                # kelly_scale=0.5 -> UU:1x, UUU:1.5x, UUUU:2x, UUUUU:2.5x
                trade_size = size * (1.0 + kelly_scale * extra)

            # Cost calculation
            fill_price = entry_price + SLIPPAGE
            cost = fill_price * trade_size

            # Check if we can afford this trade
            if cost > balance:
                continue

            # Determine win/loss
            won = (rule.buy_side == coin_outcome)
            if won:
                # Fee is deducted from shares received on entry
                effective_shares = trade_size * (1.0 - FEE_RATE)
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
                size=trade_size,
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

        # --- Record this cycle's outcomes into HISTORY (smart hybrid) ---
        for coin in COINS:
            outcome = hist_outcomes.get(coin)
            if outcome == "UP":
                history[coin].append("U")
            elif outcome == "DOWN":
                history[coin].append("D")
            # None: don't record (breaks chains — matches live bot behavior)

    # --- Compute stats ---
    total_trades = len(trades)
    wins = sum(1 for t in trades if t.won)
    losses = total_trades - wins
    win_rate = wins / total_trades if total_trades > 0 else 0.0
    total_pnl = balance - initial_balance
    avg_pnl = total_pnl / total_trades if total_trades > 0 else 0.0
    max_dd_pct = (max_dd / peak_balance * 100) if peak_balance > 0 else 0.0

    # Per-rule breakdown (unique key per rule to handle same pattern with different coins)
    per_pattern: Dict[str, Dict[str, Any]] = {}
    for rule in rules:
        rule_coins = set(rule.coins) if rule.coins else set(COINS)
        coins_tag = ",".join(sorted(rule_coins))
        key = f"{rule.pattern}[{coins_tag}]"
        pt = [
            t for t in trades
            if t.pattern == rule.pattern and t.coin in rule_coins
        ]
        pw = sum(1 for t in pt if t.won)
        pl = len(pt) - pw
        ppnl = sum(t.pnl for t in pt)
        per_pattern[key] = {
            "side": rule.buy_side,
            "trades": len(pt),
            "wins": pw,
            "losses": pl,
            "win_rate": pw / len(pt) if pt else 0.0,
            "total_pnl": ppnl,
            "avg_pnl": ppnl / len(pt) if pt else 0.0,
            "max_ask": rule.max_ask,
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

    # Per-pattern/rule
    if result.per_pattern:
        print()
        print(f"  --- Per Rule ---")
        print(f"  {'Rule':<24} {'Side':<6} {'Trades':>6} {'Wins':>5} {'Loss':>5} {'WR':>6} {'PnL':>10} {'Avg':>10}")
        print(f"  {'-'*24} {'-'*6} {'-'*6} {'-'*5} {'-'*5} {'-'*6} {'-'*10} {'-'*10}")
        for pattern, stats in result.per_pattern.items():
            print(
                f"  {pattern:<24} {stats['side']:<6} {stats['trades']:>6} "
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
        "--yaml", type=str, default=None,
        help=(
            "Load rules from a YAML config file (e.g. strategies/mean_reversion.yaml). "
            "Overrides --pattern/--side/--all-patterns. Supports per-coin and per-rule max_ask."
        ),
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
        "--kelly", type=float, default=0.0,
        help=(
            "Kelly-scaled betting: increase stake by this factor for each "
            "extra streak beyond pattern length. E.g. --kelly 0.5 with UU: "
            "UU=1x, UUU=1.5x, UUUU=2x, UUUUU=2.5x. 0=disabled (default)."
        ),
    )
    parser.add_argument(
        "--no-plot", action="store_true",
        help="Skip equity curve plot",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output path for equity curve PNG (default: auto-generated)",
    )
    parser.add_argument(
        "--coins", type=str, default=None,
        help=(
            "Comma-separated list of coins to trade (e.g. BTC,ETH). "
            "If omitted, trades all 4 coins."
        ),
    )
    parser.add_argument(
        "--legacy-inference", action="store_true",
        help=(
            "Use CSV-based threshold inference instead of Gamma API resolution. "
            "WARNING: known to diverge from live execution on borderline cycles."
        ),
    )

    args = parser.parse_args()

    # Validate CSV files exist
    for f in args.csv_files:
        if not Path(f).exists():
            print(f"ERROR: File not found: {f}")
            sys.exit(1)

    # Parse rules
    yaml_cfg = None
    if args.yaml:
        yaml_path = Path(args.yaml)
        if not yaml_path.exists():
            print(f"ERROR: YAML file not found: {args.yaml}")
            sys.exit(1)
        rules, yaml_cfg = load_yaml_rules(args.yaml)
        print(f"\n  Loaded {len(rules)} rules from {args.yaml}")
        for r in rules:
            coins_str = ",".join(r.coins) if r.coins else "ALL"
            ask_str = f"max_ask={r.max_ask}" if r.max_ask else ""
            print(f"    {r.pattern} -> BUY {r.buy_side}  [{coins_str}]  {ask_str}")
        # Use YAML size if not overridden on CLI
        if yaml_cfg and args.size == 5.0 and "size" in yaml_cfg:
            args.size = yaml_cfg["size"]
    elif args.all_patterns:
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

    use_gamma = not args.legacy_inference

    # Parse coin filter
    coins_filter = None
    if args.coins:
        coins_filter = [c.strip().upper() for c in args.coins.split(",")]
        print(f"  Coin filter: {', '.join(coins_filter)}")

    if args.kelly > 0:
        print(f"  Kelly scaling: {args.kelly} (UU=1x, UUU={1+args.kelly:.1f}x, UUUU={1+2*args.kelly:.1f}x, ...)")

    # Entry time: use YAML config or default
    entry_time = ENTRY_TIME
    if yaml_cfg and "entry_window_start" in yaml_cfg:
        entry_time = yaml_cfg["entry_window_start"]
        print(f"  Entry time: t={entry_time}s (from YAML)")

    # Run backtest
    result = run_backtest(
        csv_paths=args.csv_files,
        rules=rules,
        size=args.size,
        initial_balance=args.balance,
        max_trades_per_cycle=args.max_trades,
        max_ask=args.max_ask,
        use_gamma=use_gamma,
        coins_filter=coins_filter,
        kelly_scale=args.kelly,
        entry_time=entry_time,
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
