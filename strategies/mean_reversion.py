"""
Mean-Reversion Strategy - 5-Minute Up/Down Crypto Markets

Config-driven multi-pattern strategy based on Telonex 74-day backtest.
Each rule specifies: pattern, side, coins, and per-rule max_ask.

Key design decisions (from research/full_pattern_scan.py):
  - Only UP-streak -> DOWN mean-reversion patterns are significant
  - Longer streaks have higher WR but lower frequency
  - ETH has the strongest edge (UUUU: 57.4%, UUU: 55.8%)
  - Per-rule max_ask ensures each pattern is +EV at its entry price
  - Rules are priority-ordered: longest pattern first per coin

Entry mechanism (GTC limit orders):
  - At cycle detection (~t=3s), place GTC limit BUY at max_ask price
  - Order rests on book; may fill as maker (0% fee) vs taker (1.5%)
  - After cancel_timeout (default 10s), cancel all unfilled orders
  - Any filled amount is held to market resolution

Config file: mean_reversion.yaml (same directory)

Usage:
    python strategies/mean_reversion.py
    python strategies/mean_reversion.py --dry-run
    python strategies/mean_reversion.py --config path/to/config.yaml
    python strategies/mean_reversion.py --dry-run --name "test1"
"""

import argparse
import asyncio
import concurrent.futures
import enum
import json
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Path & env setup
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

logging.getLogger("src.websocket_client").setLevel(logging.WARNING)

from lib.market_manager import MarketInfo, MarketManager  # noqa: E402
from lib.console import Colors, format_countdown  # noqa: E402
from src.client import ClobClient  # noqa: E402
from src.config import Config  # noqa: E402
from src.gamma_client import GammaClient  # noqa: E402
from src.signer import Order, OrderSigner  # noqa: E402
from src.websocket_client import OrderbookSnapshot  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COINS: List[str] = ["BTC", "ETH", "SOL", "XRP"]

TRADE_LOG_FILE = Path(__file__).resolve().parent.parent / "meanrev_trades.txt"
DEFAULT_CONFIG = Path(__file__).resolve().parent / "mean_reversion.yaml"

# Outcome inference thresholds (applied at t >= INFERENCE_TIME)
# Last 5 seconds of the 300s cycle — prices are near-settled by then
INFERENCE_TIME = 295       # seconds into cycle to read outcome
UP_THRESHOLD = 0.95        # UP ask > this => outcome is UP (near-certain)
DOWN_THRESHOLD = 0.05      # UP ask < this => outcome is DOWN (near-certain)

# Max ask price filter — don't buy when ask is too expensive (bad risk/reward)
DEFAULT_MAX_ASK = 0.60     # only enter if ask <= this

# How long after cycle start to look for pattern-match entries
# (overridden by YAML config; these are fallback defaults)
ENTRY_WINDOW_START = 5     # seconds after cycle start
ENTRY_WINDOW_END = 10      # seconds — conservative to avoid adverse prices

# Dry-run simulation penalties (match stat_arb.py)
SIM_ENTRY_SLIP = 0.01
SIM_FEE_RATE = 0.015       # 1.5% one-way taker fee


# ---------------------------------------------------------------------------
# Pattern definitions (unused — kept for reference, config comes from YAML)
# ---------------------------------------------------------------------------
BUILTIN_PATTERNS: Dict[str, Tuple[str, str]] = {
    "UUUU": ("UUUU", "DOWN"),
    "UUU":  ("UUU",  "DOWN"),
    "UU":   ("UU",   "DOWN"),
}


# ---------------------------------------------------------------------------
# TUI-aware logging
# ---------------------------------------------------------------------------
_log_buffer: list = []
_tui_active = False


def ts_now() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(msg: str, level: str = "info") -> None:
    colors = {
        "info": "\033[0m",
        "success": "\033[92m",
        "warning": "\033[93m",
        "error": "\033[91m",
        "trade": "\033[96m",
    }
    reset = "\033[0m"
    color = colors.get(level, colors["info"])
    line = f"  {color}[{ts_now()}] {msg}{reset}"
    if _tui_active:
        _log_buffer.append(line)
        if len(_log_buffer) > 24:
            _log_buffer.pop(0)
    else:
        print(line)


# ===================================================================
# Persistent trade log
# ===================================================================
@dataclass
class PositionRecord:
    coin: str
    side: str                 # "up" or "down"
    fill_price: float
    fill_size: float
    fill_time: float
    market_slug: str
    order_id: str = ""
    cost: float = 0.0
    pattern: str = ""         # which pattern triggered this
    # Resolution
    resolved: bool = False
    won: Optional[bool] = None
    payout: float = 0.0
    pnl: Optional[float] = None
    exit_type: Optional[str] = None   # "EXPIRY_WIN" or "EXPIRY_LOSS"
    exit_time: Optional[float] = None


@dataclass
class PendingOrder:
    """Tracks a GTC limit order waiting for fill or cancellation."""
    coin: str
    side: str                     # "up" or "down"
    token_id: str
    order_id: str
    limit_price: float            # the max_ask we placed at
    size: float
    placed_at: float              # time.time() when placed
    market_slug: str
    pattern: str
    neg_risk: bool = False
    tick_size: str = "0.01"


def _append_trade_log(
    pos: PositionRecord,
    cfg: "SequenceConfig",
    outcome: str = "PENDING",
    log_file: Optional[Path] = None,
) -> None:
    target = log_file or TRADE_LOG_FILE
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    now_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    pnl_str = f"${pos.pnl:+.4f}" if pos.pnl is not None else "--"
    line = (
        f"{now_utc} | {now_local} | "
        f"order_id={pos.order_id} | "
        f"market={pos.market_slug} | coin={pos.coin} | side={pos.side.upper()} | "
        f"entry={pos.fill_price:.4f} | "
        f"size={pos.fill_size:.4f} | pnl={pnl_str} | "
        f"pattern={pos.pattern} | "
        f"dry_run={cfg.dry_run} | outcome={outcome}"
    )
    try:
        with open(target, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:
        log(f"Trade log write error: {exc}", "warning")


def _update_trade_log_outcome(
    order_id: str, market_slug: str, coin: str, side: str, outcome: str,
    log_file: Optional[Path] = None,
) -> None:
    target = log_file or TRADE_LOG_FILE
    try:
        if not target.exists():
            return
        lines = target.read_text(encoding="utf-8").splitlines()
        updated = []
        for line in lines:
            matched = False
            if order_id and f"order_id={order_id}" in line:
                matched = True
            elif (
                f"market={market_slug}" in line
                and f"coin={coin}" in line
                and f"side={side.upper()}" in line
            ):
                matched = True
            if matched and "outcome=PENDING" in line:
                line = line.replace("outcome=PENDING", f"outcome={outcome}")
            updated.append(line)
        target.write_text("\n".join(updated) + "\n", encoding="utf-8")
    except Exception:
        pass


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _streak_length(history, direction: str) -> int:
    """Count consecutive occurrences of `direction` at the end of history."""
    count = 0
    for ch in reversed(history):
        if ch == direction:
            count += 1
        else:
            break
    return count


# ===================================================================
# Configuration
# ===================================================================
@dataclass
class PatternRule:
    """One pattern->action rule with per-rule max_ask and coin filter."""
    pattern: str                        # e.g. "UUUU"
    buy_side: str                       # "up" or "down"
    max_ask: float = DEFAULT_MAX_ASK    # per-rule max ask
    coins: Optional[List[str]] = None   # coins this rule applies to (None = all)
    priority: int = 0                   # lower = higher priority


@dataclass
class SequenceConfig:
    rules: List[PatternRule]
    size: float = 5.0
    slippage: float = 0.03             # legacy (unused with GTC limits)
    max_ask: float = DEFAULT_MAX_ASK   # global fallback max ask
    max_trades_per_cycle: int = 4      # one per coin
    dry_run: bool = False
    market_check_interval: float = 5.0
    name: str = ""
    coins: Optional[List[str]] = None  # global coin filter (None = all)
    kelly: float = 0.0
    entry_window_start: int = ENTRY_WINDOW_START
    entry_window_end: int = ENTRY_WINDOW_END
    cancel_timeout: float = 10.0       # seconds after placing GTC to cancel unfilled

    @classmethod
    def from_yaml(cls, path: str, dry_run: bool = False,
                  name: str = "") -> "SequenceConfig":
        """Load config from YAML file."""
        with open(path) as f:
            raw = yaml.safe_load(f)

        rules = []
        for i, r in enumerate(raw.get("rules", [])):
            pattern = r["pattern"].upper()
            side = r["side"].lower()
            rule_coins = [c.upper() for c in r["coins"]] if "coins" in r else None
            rule_max_ask = float(r.get("max_ask", DEFAULT_MAX_ASK))
            rules.append(PatternRule(
                pattern=pattern,
                buy_side=side,
                max_ask=rule_max_ask,
                coins=rule_coins,
                priority=r.get("priority", i),
            ))

        # Sort rules: longest pattern first (highest priority for hierarchy),
        # then by explicit priority
        rules.sort(key=lambda r: (-len(r.pattern), r.priority))

        cfg = cls(
            rules=rules,
            size=float(raw.get("size", 5.0)),
            slippage=float(raw.get("slippage", 0.03)),
            max_ask=float(raw.get("max_ask", DEFAULT_MAX_ASK)),
            max_trades_per_cycle=int(raw.get("max_trades_per_cycle", 4)),
            dry_run=dry_run,
            market_check_interval=float(raw.get("market_check_interval", 5.0)),
            name=name,
            entry_window_start=int(raw.get("entry_window_start", ENTRY_WINDOW_START)),
            entry_window_end=int(raw.get("entry_window_end", ENTRY_WINDOW_END)),
            cancel_timeout=float(raw.get("cancel_timeout", 10.0)),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.size < 5:
            raise ValueError(f"size must be >= 5, got {self.size}")
        if not self.rules:
            raise ValueError("At least one pattern rule is required")
        for rule in self.rules:
            if not all(c in "UD" for c in rule.pattern):
                raise ValueError(
                    f"Pattern must contain only U/D, got '{rule.pattern}'"
                )
            if rule.buy_side not in ("up", "down"):
                raise ValueError(
                    f"buy_side must be 'up' or 'down', got '{rule.buy_side}'"
                )
            if not 0.30 <= rule.max_ask <= 0.90:
                raise ValueError(
                    f"max_ask must be 0.30-0.90, got {rule.max_ask}"
                )


# ===================================================================
# State machine
# ===================================================================
class CycleState(enum.Enum):
    WAITING_MARKET = "WAITING_MARKET"
    OBSERVING = "OBSERVING"       # Watching cycle, inferring outcomes at end
    ENTRY_WINDOW = "ENTRY_WINDOW" # New cycle, checking for pattern matches
    PENDING_ORDERS = "PENDING"    # GTC limits placed, waiting for fills/cancel
    TRADED = "TRADED"             # Bought, holding to resolution
    DONE = "DONE"


# ===================================================================
# Strategy
# ===================================================================
class SequenceStrategy:
    """Pattern-based autocorrelation strategy."""

    def __init__(
        self,
        cfg: SequenceConfig,
        bot_config: Config,
        signer: OrderSigner,
        clob: ClobClient,
    ):
        self.cfg = cfg
        self.bot_config = bot_config
        self.signer = signer
        self.clob = clob

        # Log file
        if cfg.dry_run and cfg.name:
            self.log_file = TRADE_LOG_FILE.parent / f"sequence_sim_{cfg.name}.txt"
        else:
            self.log_file = TRADE_LOG_FILE

        # 4 MarketManagers
        self.managers: Dict[str, MarketManager] = {}
        for coin in COINS:
            self.managers[coin] = MarketManager(
                coin=coin,
                market_check_interval=cfg.market_check_interval,
                auto_switch_market=True,
                interval="5m",
            )

        # Cycle state
        self.cycle_state = CycleState.WAITING_MARKET
        self._cycle_ts: Optional[int] = None
        self._cycle_start_ts: float = 0.0

        # Rolling outcome history per coin: 'U' or 'D'
        # maxlen=6 supports patterns up to length 6
        self._outcome_history: Dict[str, deque] = {
            c: deque(maxlen=6) for c in COINS
        }
        # Track which cycle_ts each coin's last inference was from
        # (prevent double-recording from same cycle)
        self._last_inference_ts: Dict[str, Optional[int]] = {c: None for c in COINS}

        # Track which coins already traded this cycle
        self._traded_coins: Set[str] = set()

        # Per-coin orderbook caches
        self._best_asks: Dict[str, Dict[str, float]] = {
            c: {"up": 1.0, "down": 1.0} for c in COINS
        }
        self._best_bids: Dict[str, Dict[str, float]] = {
            c: {"up": 0.0, "down": 0.0} for c in COINS
        }
        self._coin_markets: Dict[str, Optional[MarketInfo]] = {
            c: None for c in COINS
        }

        # Fee cache
        self._fee_rate_cache: Dict[str, int] = {}

        # Dedicated CLOB thread
        self._clob_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="clob-hot"
        )

        # Book event (WS callback signals this)
        self._book_event: asyncio.Event = asyncio.Event()

        # Pending GTC limit orders (placed, waiting for fill/cancel)
        self._pending_orders: List[PendingOrder] = []
        self._orders_placed_ts: float = 0.0  # when GTC orders were placed this cycle

        # Positions
        self._current_positions: List[PositionRecord] = []
        self._all_positions: List[PositionRecord] = []

        # Resolution tasks
        self._resolution_tasks: List[asyncio.Task] = []
        self._scheduled_slugs: Set[str] = set()

        # Session stats
        self.cycles_seen: int = 0
        self.total_orders_placed: int = 0
        self.total_fills: int = 0
        self.total_wins: int = 0
        self.total_losses: int = 0
        self.total_resolved: int = 0
        self.session_pnl: float = 0.0
        self.total_spent: float = 0.0
        self.total_received: float = 0.0
        self.total_shares: float = 0.0

        # Per-coin stats
        self.coin_wins: Dict[str, int] = {c: 0 for c in COINS}
        self.coin_losses: Dict[str, int] = {c: 0 for c in COINS}
        self.coin_resolved: Dict[str, int] = {c: 0 for c in COINS}

        # Per-pattern stats
        self.pattern_wins: Dict[str, int] = {}
        self.pattern_losses: Dict[str, int] = {}
        self.pattern_fills: Dict[str, int] = {}
        for rule in cfg.rules:
            self.pattern_wins[rule.pattern] = 0
            self.pattern_losses[rule.pattern] = 0
            self.pattern_fills[rule.pattern] = 0

        self._load_stats_from_log()

        # Sweep + heartbeat
        self._session_start: float = time.time()
        self._last_heartbeat_ts: float = 0.0
        self._last_sweep_ts: float = 0.0
        self._sweep_task: Optional[asyncio.Task] = None
        self._last_done_poll: float = 0.0
        self._last_task_cleanup: float = 0.0
        self._last_render_ts: float = 0.0
        self._ticks_total: int = 0
        self._ticks_window: int = 0
        self._last_tick_ts: float = 0.0
        self._status_window_start: float = time.time()

    # ------------------------------------------------------------------
    # Restore stats from trade log
    # ------------------------------------------------------------------
    def _load_stats_from_log(self) -> None:
        if not self.log_file.exists():
            return
        try:
            for line in self.log_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                fields = {}
                for part in line.split("|"):
                    part = part.strip()
                    if "=" in part:
                        k, v = part.split("=", 1)
                        fields[k.strip()] = v.strip()

                size = _to_float(fields.get("size", "0"))
                entry = _to_float(fields.get("entry", "0"))
                cost = entry * size if entry > 0 and size > 0 else 0.0
                outcome = fields.get("outcome", "")
                pattern = fields.get("pattern", "")
                coin = fields.get("coin", "").upper()

                self.total_fills += 1
                self.total_spent += cost
                self.total_shares += size

                if pattern in self.pattern_fills:
                    self.pattern_fills[pattern] += 1

                if outcome.startswith("WIN"):
                    self.total_wins += 1
                    self.total_resolved += 1
                    if coin in self.coin_wins:
                        self.coin_wins[coin] += 1
                        self.coin_resolved[coin] += 1
                    if pattern in self.pattern_wins:
                        self.pattern_wins[pattern] += 1
                    profit_str = outcome.replace("WIN +$", "").replace("WIN +", "")
                    self.session_pnl += _to_float(profit_str)
                    self.total_received += size

                elif outcome.startswith("LOSS"):
                    self.total_losses += 1
                    self.total_resolved += 1
                    if coin in self.coin_losses:
                        self.coin_losses[coin] += 1
                        self.coin_resolved[coin] += 1
                    if pattern in self.pattern_losses:
                        self.pattern_losses[pattern] += 1
                    self.session_pnl -= cost
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def run(self) -> None:
        global _tui_active

        log("Sequence Pattern Strategy started (4-coin WebSocket)", "success")
        log(f"  rules:    {', '.join(f'{r.pattern}->BUY {r.buy_side.upper()}' for r in self.cfg.rules)}")
        log(f"  size:     {self.cfg.size} shares")
        if self.cfg.kelly > 0:
            log(f"  kelly:    {self.cfg.kelly} (streak scaling enabled)")
        log(f"  max_ask:  {self.cfg.max_ask:.2f}")
        log(f"  infer:    t>={INFERENCE_TIME}s, UP>{UP_THRESHOLD}, DOWN<{DOWN_THRESHOLD}")
        log(f"  dry_run:  {self.cfg.dry_run}")
        log(f"  log:      {self.log_file}")
        print()

        try:
            await self._start_all_managers()
            _tui_active = True

            while True:
                try:
                    await self._tick()
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    log(f"[tick error] {exc}", "error")
                await asyncio.sleep(0.1)

        except KeyboardInterrupt:
            pass
        finally:
            _tui_active = False
            await self._cleanup()
            self._print_summary()

    # ------------------------------------------------------------------
    # Manager setup
    # ------------------------------------------------------------------
    async def _start_all_managers(self) -> None:
        log("Starting 4 coin managers (BTC, ETH, SOL, XRP)...", "info")

        for coin in COINS:
            mgr = self.managers[coin]

            mgr.on_market_change(
                lambda old, new, c=coin: self._on_market_change(c, old, new)
            )
            mgr.on_book_update(
                lambda snap, c=coin: self._on_book_update(c, snap)
            )

            attempts = 0
            while True:
                started = await mgr.start()
                if started:
                    break
                attempts += 1
                if attempts >= 5:
                    log(f"  {coin}: no active 5m market after 5 tries", "error")
                    break
                log(f"  {coin}: retrying in 2s...", "warning")
                await asyncio.sleep(2)

            if mgr.current_market:
                self._coin_markets[coin] = mgr.current_market
                log(f"  {coin}: {mgr.current_market.slug}", "success")
            else:
                log(f"  {coin}: no market yet", "warning")

        await asyncio.sleep(1.5)

        connected = sum(1 for m in self.managers.values() if m.is_connected)
        log(
            f"WebSocket connections: {connected}/4",
            "success" if connected == 4 else "warning",
        )

        # Enter cycle from first discovered market
        for coin in COINS:
            market = self._coin_markets.get(coin)
            if market:
                self._maybe_enter_cycle(coin, market)
                break

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------
    def _on_market_change(self, coin: str, old_slug: str, new_slug: str) -> None:
        mgr = self.managers[coin]
        market = mgr.current_market
        if market:
            self._coin_markets[coin] = market
            # Invalidate stale orderbook data
            self._best_asks[coin] = {"up": 1.0, "down": 1.0}
            self._best_bids[coin] = {"up": 0.0, "down": 0.0}
            log(f"{coin} -> {new_slug}", "info")
            self._maybe_enter_cycle(coin, market)

            if old_slug:
                self._schedule_resolution(coin, old_slug)

    def _on_book_update(self, coin: str, snapshot: OrderbookSnapshot) -> None:
        self._ticks_total += 1
        self._ticks_window += 1
        self._last_tick_ts = time.time()

        market = self._coin_markets.get(coin)
        if not market:
            return

        # Reject stale data from previous cycle's market
        if self._cycle_ts is not None:
            ms = market.start_timestamp()
            if ms is not None and ms != self._cycle_ts:
                return

        try:
            asset_id = snapshot.asset_id
            for side in ("up", "down"):
                if market.token_ids.get(side) == asset_id:
                    asks = snapshot.asks
                    best_ask = asks[0].price if asks else 1.0
                    self._best_asks[coin][side] = best_ask

                    bids = snapshot.bids
                    best_bid = bids[0].price if bids else 0.0
                    self._best_bids[coin][side] = best_bid

                    self._book_event.set()
                    break
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Cycle management
    # ------------------------------------------------------------------
    def _maybe_enter_cycle(self, coin: str, market: MarketInfo) -> None:
        market_start = market.start_timestamp()
        if market_start is None:
            return

        # Same cycle — just update coin market
        if self._cycle_ts == market_start:
            return

        # --- New cycle ---
        # Before entering new cycle, try to infer outcomes from old cycle
        self._try_infer_outcomes_from_prices()

        # Schedule resolution for any positions from old cycle
        if self.cycle_state in (CycleState.OBSERVING, CycleState.ENTRY_WINDOW,
                                CycleState.PENDING_ORDERS, CycleState.TRADED):
            self._transition_to_done()

        self._cycle_ts = market_start
        self._cycle_start_ts = float(market_start)
        self._traded_coins.clear()
        self._current_positions.clear()
        self._pending_orders.clear()
        self._orders_placed_ts = 0.0
        self._fee_rate_cache.clear()
        self.cycles_seen += 1

        # Reset orderbook caches
        self._best_asks = {c: {"up": 1.0, "down": 1.0} for c in COINS}
        self._best_bids = {c: {"up": 0.0, "down": 0.0} for c in COINS}

        now = time.time()
        cycle_age = now - self._cycle_start_ts

        # Check if we have pattern matches for this new cycle
        matches = self._find_pattern_matches()

        if matches:
            match_strs = [f"{c}:{r.pattern}->BUY {r.buy_side.upper()}" for c, r in matches]
            log(
                f"NEW CYCLE #{self.cycles_seen}: "
                f"MATCHES: {', '.join(match_strs)}  age={cycle_age:.0f}s",
                "trade",
            )
            self.cycle_state = CycleState.ENTRY_WINDOW

            # Pre-warm CLOB + pre-fetch fees
            loop = asyncio.get_running_loop()
            loop.create_task(self._prewarm_clob())
            for c, rule in matches:
                m = self._coin_markets.get(c)
                if m:
                    tid = m.token_ids.get(rule.buy_side, "")
                    if tid and tid not in self._fee_rate_cache:
                        loop.create_task(self._prefetch_fee(tid))
        else:
            log(
                f"NEW CYCLE #{self.cycles_seen}: no pattern matches. Observing.",
                "info",
            )
            self.cycle_state = CycleState.OBSERVING

        # Log outcome history
        for c in COINS:
            hist = "".join(self._outcome_history[c])
            if hist:
                log(f"  {c} history: [{hist}]", "info")

    def _transition_to_done(self) -> None:
        self.cycle_state = CycleState.DONE

        # Cancel any lingering pending orders (edge case: cycle ended before timeout)
        if self._pending_orders:
            order_ids = [p.order_id for p in self._pending_orders if p.order_id]
            if order_ids:
                try:
                    self.clob.cancel_orders(order_ids)
                except Exception:
                    pass
            self._pending_orders.clear()

        # Schedule resolution for all unique slugs from current positions
        seen_slugs: Set[str] = set()
        for pos in self._current_positions:
            if pos.market_slug and pos.market_slug not in seen_slugs:
                seen_slugs.add(pos.market_slug)
                self._schedule_resolution_all(pos.market_slug)

    # ------------------------------------------------------------------
    # Outcome inference from WS prices
    # ------------------------------------------------------------------
    def _try_infer_outcomes_from_prices(self) -> None:
        """At end of cycle, infer outcome from UP ask prices.

        Called just before entering a new cycle.  For each coin, if the
        current UP ask is extreme enough (> UP_THRESHOLD or < DOWN_THRESHOLD),
        record the outcome.
        """
        if self._cycle_ts is None:
            return

        for coin in COINS:
            # Don't double-record for same cycle
            if self._last_inference_ts[coin] == self._cycle_ts:
                continue

            up_ask = self._best_asks[coin]["up"]

            if up_ask > UP_THRESHOLD:
                self._outcome_history[coin].append("U")
                self._last_inference_ts[coin] = self._cycle_ts
                log(f"  {coin} outcome inferred: UP (ask={up_ask:.3f})", "trade")
            elif up_ask < DOWN_THRESHOLD:
                self._outcome_history[coin].append("D")
                self._last_inference_ts[coin] = self._cycle_ts
                log(f"  {coin} outcome inferred: DOWN (ask={up_ask:.3f})", "trade")
            else:
                # Ambiguous — don't record (breaks pattern chains, which is correct;
                # we only want high-confidence sequences)
                log(
                    f"  {coin} outcome AMBIGUOUS (ask={up_ask:.3f}), not recorded",
                    "warning",
                )

    # ------------------------------------------------------------------
    # Pattern matching
    # ------------------------------------------------------------------
    def _find_pattern_matches(self) -> List[Tuple[str, PatternRule]]:
        """Check all coins against all rules with per-rule coin filters.

        Rules are pre-sorted longest-pattern-first, so the first match per
        coin is always the highest-priority (longest) pattern. This ensures
        that if UUUU and UUU both match, UUUU wins (higher WR, higher max_ask).
        """
        matches = []
        active = self.cfg.coins if self.cfg.coins else COINS
        for coin in active:
            hist = list(self._outcome_history[coin])
            if not hist:
                continue

            for rule in self.cfg.rules:
                # Check if this rule applies to this coin
                if rule.coins and coin not in rule.coins:
                    continue

                pattern_len = len(rule.pattern)
                if len(hist) < pattern_len:
                    continue

                recent = "".join(hist[-pattern_len:])
                if recent == rule.pattern:
                    matches.append((coin, rule))
                    break  # First matching rule wins per coin

        return matches

    # ------------------------------------------------------------------
    # Entry execution
    # ------------------------------------------------------------------
    async def _try_enter_trades(self) -> None:
        """Place GTC limit orders at max_ask for all pattern matches.

        Orders are placed immediately and tracked in _pending_orders.
        After cancel_timeout seconds, unfilled orders are cancelled.
        Fills (immediate or partial) are recorded as positions.
        """
        matches = self._find_pattern_matches()
        placed_any = False

        for coin, rule in matches:
            if coin in self._traded_coins:
                continue
            if (len(self._current_positions) + len(self._pending_orders)
                    >= self.cfg.max_trades_per_cycle):
                break

            market = self._coin_markets.get(coin)
            if not market:
                continue

            buy_side = rule.buy_side
            token_id = market.token_ids.get(buy_side, "")
            if not token_id:
                continue

            # Use rule.max_ask as the limit price (no slippage needed)
            limit_price = rule.max_ask
            trade_size = self.cfg.size

            hist_str = "".join(self._outcome_history[coin])
            current_ask = self._best_asks[coin][buy_side]
            log(
                f"SIGNAL: {coin} [{hist_str}] -> {rule.pattern} -> "
                f"GTC LIMIT BUY {buy_side.upper()} @ {limit_price:.3f} "
                f"(ask={current_ask:.3f})",
                "trade",
            )

            if self.cfg.dry_run:
                tracker = self._simulate_buy(
                    coin, buy_side, current_ask, market, rule.pattern, trade_size,
                )
                if tracker:
                    self._traded_coins.add(coin)
                    placed_any = True
            else:
                result = await self._submit_limit_buy(
                    coin, buy_side, token_id, market, limit_price,
                    rule.pattern, trade_size,
                )
                if result is not None:
                    placed_any = True
                    # result is either a PositionRecord (immediate fill)
                    # or a PendingOrder (resting on book)
                    if isinstance(result, PositionRecord):
                        self._traded_coins.add(coin)
                    # PendingOrder is already in self._pending_orders

        if placed_any and not self.cfg.dry_run and self._pending_orders:
            self._orders_placed_ts = time.time()
            self.cycle_state = CycleState.PENDING_ORDERS
            log(
                f"Placed {len(self._pending_orders)} GTC limit(s). "
                f"Cancel timeout: {self.cfg.cancel_timeout:.0f}s.",
                "trade",
            )

    def _simulate_buy(
        self, coin: str, side: str, ask_price: float, market: MarketInfo, pattern: str,
        trade_size: Optional[float] = None,
    ) -> Optional[PositionRecord]:
        """Simulate a fill for dry-run mode."""
        sim_price = ask_price + SIM_ENTRY_SLIP
        sim_size = trade_size if trade_size is not None else self.cfg.size
        cost = sim_price * sim_size

        pos = PositionRecord(
            coin=coin,
            side=side,
            fill_price=sim_price,
            fill_size=sim_size,
            fill_time=time.time(),
            market_slug=market.slug,
            order_id=f"SIM-{coin}-{int(time.time())}",
            cost=cost,
            pattern=pattern,
        )
        self._current_positions.append(pos)
        self._all_positions.append(pos)
        self.total_fills += 1
        self.total_orders_placed += 1
        self.total_spent += cost
        self.total_shares += sim_size
        if pattern in self.pattern_fills:
            self.pattern_fills[pattern] += 1

        _append_trade_log(pos, self.cfg, outcome="PENDING", log_file=self.log_file)
        log(
            f"SIM FILL {coin}-{side.upper()} @ {sim_price:.4f} x{sim_size:.2f} "
            f"(pattern={pattern})",
            "success",
        )
        return pos

    async def _submit_limit_buy(
        self,
        coin: str,
        side: str,
        token_id: str,
        market: MarketInfo,
        limit_price: float,
        pattern: str,
        trade_size: Optional[float] = None,
    ) -> Optional[Any]:
        """Submit a GTC limit BUY to the CLOB.

        Returns:
            PositionRecord if immediately matched (full fill).
            PendingOrder if order is resting on the book (live).
            None on error.
        """
        actual_size = trade_size if trade_size is not None else self.cfg.size
        label = f"{coin}-{side.upper()}"
        try:
            # Fee
            fee_rate_bps = self._fee_rate_cache.get(token_id)
            if fee_rate_bps is None:
                fee_rate_bps = await asyncio.to_thread(
                    self.clob.get_fee_rate_bps, token_id
                )
                self._fee_rate_cache[token_id] = fee_rate_bps

            order = Order(
                token_id=token_id,
                price=limit_price,
                size=actual_size,
                side="BUY",
                funder=self.bot_config.safe_address,
                fee_rate_bps=fee_rate_bps,
                signature_type=self.bot_config.clob.signature_type,
                neg_risk=market.neg_risk,
                tick_size=market.tick_size,
            )

            # Sign + POST as GTC on dedicated thread
            t_start = time.perf_counter()
            loop = asyncio.get_running_loop()
            response, t_sign_us, t_post_us = await loop.run_in_executor(
                self._clob_executor,
                self._sync_sign_and_post, order, "GTC",
            )
            t_total_us = (time.perf_counter() - t_start) * 1_000_000

            timing = f"[sign={t_sign_us:.0f}us post={t_post_us:.0f}us total={t_total_us:.0f}us]"

            if not response.get("success", False):
                error = response.get("errorMsg", "unknown")
                log(f"GTC FAIL {label}: {error} {timing}", "error")
                return None

            order_id = (
                response.get("orderID")
                or response.get("orderId")
                or response.get("order_id")
                or ""
            )

            status = str(response.get("status", "")).lower()
            self.total_orders_placed += 1

            if status in {"matched", "filled", "executed", "complete", "completed"}:
                # Immediately filled — record as position
                taking = _to_float(response.get("takingAmount", 0))
                making = _to_float(response.get("makingAmount", 0))
                fp = making / max(taking, 1e-12) if taking > 0 else limit_price
                fill_size = taking if taking > 0 else actual_size
                cost = fp * fill_size

                pos = PositionRecord(
                    coin=coin,
                    side=side,
                    fill_price=fp,
                    fill_size=fill_size,
                    fill_time=time.time(),
                    market_slug=market.slug,
                    order_id=order_id,
                    cost=cost,
                    pattern=pattern,
                )
                self._current_positions.append(pos)
                self._all_positions.append(pos)
                self.total_fills += 1
                self.total_spent += cost
                self.total_shares += fill_size
                if pattern in self.pattern_fills:
                    self.pattern_fills[pattern] += 1

                _append_trade_log(pos, self.cfg, outcome="PENDING", log_file=self.log_file)
                log(
                    f"GTC IMMEDIATE FILL {label} @ {fp:.4f} x{fill_size:.2f} "
                    f"(pattern={pattern}) {timing}",
                    "success",
                )
                return pos

            if status == "live":
                # Resting on book — track as pending
                pending = PendingOrder(
                    coin=coin,
                    side=side,
                    token_id=token_id,
                    order_id=order_id,
                    limit_price=limit_price,
                    size=actual_size,
                    placed_at=time.time(),
                    market_slug=market.slug,
                    pattern=pattern,
                    neg_risk=market.neg_risk,
                    tick_size=market.tick_size,
                )
                self._pending_orders.append(pending)
                log(
                    f"GTC LIVE {label} @ {limit_price:.3f} x{actual_size:.2f} "
                    f"id={order_id[:16]}... {timing}",
                    "trade",
                )
                return pending

            # Unexpected status (delayed, unmatched, etc.)
            log(
                f"GTC UNEXPECTED {label}: status={status} id={order_id[:16]}... {timing}",
                "warning",
            )
            # Still track it as pending if we got an order_id
            if order_id:
                pending = PendingOrder(
                    coin=coin,
                    side=side,
                    token_id=token_id,
                    order_id=order_id,
                    limit_price=limit_price,
                    size=actual_size,
                    placed_at=time.time(),
                    market_slug=market.slug,
                    pattern=pattern,
                    neg_risk=market.neg_risk,
                    tick_size=market.tick_size,
                )
                self._pending_orders.append(pending)
                return pending
            return None

        except Exception as exc:
            log(f"GTC ERR {label}: {exc}", "error")
            return None

    async def _cancel_and_settle_pending(self) -> None:
        """Cancel all unfilled GTC orders and record any fills as positions.

        Called after cancel_timeout expires. For each pending order:
        1. Poll get_order to check if it was (partially) filled
        2. Cancel whatever remains unfilled
        3. Record any filled amount as a position
        """
        if not self._pending_orders:
            return

        pending = list(self._pending_orders)
        self._pending_orders.clear()

        # Batch cancel all order IDs first (fast, single API call)
        order_ids = [p.order_id for p in pending if p.order_id]
        if order_ids:
            try:
                cancel_result = await asyncio.to_thread(
                    self.clob.cancel_orders, order_ids
                )
                cancelled = cancel_result.get("canceled", [])
                not_cancelled = cancel_result.get("not_canceled", {})
                log(
                    f"Cancelled {len(cancelled)}/{len(order_ids)} orders"
                    + (f" (not_cancelled: {not_cancelled})" if not_cancelled else ""),
                    "info",
                )
            except Exception as exc:
                log(f"Cancel batch error: {exc}", "error")

        # Now poll each order for fill status
        for p in pending:
            if not p.order_id:
                continue
            try:
                order_data = await asyncio.to_thread(
                    self.clob.get_order, p.order_id
                )
                size_matched = _to_float(order_data.get("size_matched", 0))
                original_size = _to_float(order_data.get("original_size", p.size))
                price = _to_float(order_data.get("price", p.limit_price))

                label = f"{p.coin}-{p.side.upper()}"

                if size_matched > 0:
                    # Got a (partial) fill — record as position
                    fill_price = price  # limit order fills at limit price or better
                    cost = fill_price * size_matched

                    pos = PositionRecord(
                        coin=p.coin,
                        side=p.side,
                        fill_price=fill_price,
                        fill_size=size_matched,
                        fill_time=time.time(),
                        market_slug=p.market_slug,
                        order_id=p.order_id,
                        cost=cost,
                        pattern=p.pattern,
                    )
                    self._current_positions.append(pos)
                    self._all_positions.append(pos)
                    self.total_fills += 1
                    self.total_spent += cost
                    self.total_shares += size_matched
                    if p.pattern in self.pattern_fills:
                        self.pattern_fills[p.pattern] += 1
                    self._traded_coins.add(p.coin)

                    _append_trade_log(pos, self.cfg, outcome="PENDING", log_file=self.log_file)

                    partial = "" if size_matched >= original_size else " (PARTIAL)"
                    log(
                        f"GTC FILLED{partial} {label} @ {fill_price:.4f} "
                        f"x{size_matched:.2f}/{original_size:.2f} "
                        f"(pattern={p.pattern})",
                        "success",
                    )
                else:
                    log(
                        f"GTC NO FILL {label} @ {p.limit_price:.3f} "
                        f"(pattern={p.pattern}) — cancelled",
                        "warning",
                    )

            except Exception as exc:
                log(f"Poll order {p.order_id[:16]}... error: {exc}", "error")

    def _sync_sign_and_post(
        self, order: Order, order_type: str = "GTC"
    ) -> Tuple[Dict[str, Any], float, float]:
        """Sign + POST on the dedicated executor thread."""
        prev_t, prev_r = self.clob.timeout, self.clob.retry_count
        self.clob.timeout = 5
        self.clob.retry_count = 1
        try:
            t0 = time.perf_counter()
            signed = self.signer.sign_order(order)
            t_sign = (time.perf_counter() - t0) * 1_000_000

            t1 = time.perf_counter()
            response = self.clob.post_order(signed, order_type)
            t_post = (time.perf_counter() - t1) * 1_000_000

            return response, t_sign, t_post
        finally:
            self.clob.timeout = prev_t
            self.clob.retry_count = prev_r

    async def _prewarm_clob(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                self._clob_executor,
                lambda: self.clob._request("GET", "/time"),
            )
        except Exception:
            pass

    async def _prefetch_fee(self, token_id: str) -> None:
        try:
            fee = await asyncio.to_thread(self.clob.get_fee_rate_bps, token_id)
            self._fee_rate_cache[token_id] = fee
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Resolution tracking
    # ------------------------------------------------------------------
    def _schedule_resolution(self, coin: str, old_slug: str) -> None:
        parts = old_slug.rsplit("-", 1)
        if len(parts) == 2:
            ts_suffix = parts[1]
            for c in COINS:
                slug = f"{c.lower()}-updown-5m-{ts_suffix}"
                self._schedule_resolution_all(slug)
        else:
            self._schedule_resolution_all(old_slug)

    def _schedule_resolution_all(self, slug: str) -> None:
        if slug in self._scheduled_slugs:
            return
        self._scheduled_slugs.add(slug)

        task = asyncio.get_running_loop().create_task(
            self._check_resolution_for_slug(slug)
        )
        self._resolution_tasks.append(task)

    async def _check_resolution_for_slug(self, old_slug: str) -> None:
        """Check if a finished market has resolved and record win/loss."""
        positions = [
            p for p in self._all_positions
            if p.market_slug == old_slug and not p.resolved
        ]
        if not positions:
            return

        delays = [10, 10, 15, 15, 20, 30, 30, 45, 60, 60]
        gamma = GammaClient()
        winner: Optional[str] = None

        for attempt, delay in enumerate(delays):
            await asyncio.sleep(delay)

            try:
                market_data = await asyncio.to_thread(
                    gamma.get_market_by_slug, old_slug
                )
                if not market_data:
                    continue
                if not market_data.get("closed", False):
                    continue

                raw_prices = market_data.get("outcomePrices", "[]")
                raw_outcomes = market_data.get("outcomes", "[]")
                prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes

                for idx, price in enumerate(prices):
                    if str(price) == "1" and idx < len(outcomes):
                        winner = str(outcomes[idx]).lower()
                        break

                if winner:
                    log(
                        f"Resolved: {old_slug} -> {winner.upper()} (attempt {attempt + 1})",
                        "info",
                    )
                    break

            except Exception as exc:
                if attempt == len(delays) - 1:
                    log(f"Resolve error ({old_slug}): {exc}", "error")

        if winner is None:
            log(f"Resolve: {old_slug} not closed after {len(delays)} attempts", "warning")
            return

        self._apply_resolution(positions, winner)

    def _apply_resolution(self, positions: List[PositionRecord], winner: str) -> None:
        for pos in positions:
            if pos.resolved:
                continue
            pos.resolved = True
            self.total_resolved += 1

            effective_cost = pos.cost
            if self.cfg.dry_run:
                effective_cost = pos.cost * (1.0 + SIM_FEE_RATE)

            coin_key = pos.coin.upper()
            pattern = pos.pattern

            if coin_key in self.coin_resolved:
                self.coin_resolved[coin_key] += 1

            if pos.side == winner:
                pos.won = True
                pos.payout = pos.fill_size * 1.0
                profit = pos.payout - effective_cost
                pos.pnl = profit
                pos.exit_type = "EXPIRY_WIN"
                pos.exit_time = time.time()
                self.total_wins += 1
                if coin_key in self.coin_wins:
                    self.coin_wins[coin_key] += 1
                if pattern in self.pattern_wins:
                    self.pattern_wins[pattern] += 1
                self.session_pnl += profit
                self.total_received += pos.payout
                outcome_str = f"WIN +${profit:.4f}"
                log(
                    f"WIN  {pos.coin}-{pos.side.upper()} @{pos.fill_price:.2f} "
                    f"-> +${profit:.4f} (pattern={pattern})",
                    "success",
                )
            else:
                pos.won = False
                pos.payout = 0.0
                pos.pnl = -effective_cost
                pos.exit_type = "EXPIRY_LOSS"
                pos.exit_time = time.time()
                self.total_losses += 1
                if coin_key in self.coin_losses:
                    self.coin_losses[coin_key] += 1
                if pattern in self.pattern_losses:
                    self.pattern_losses[pattern] += 1
                self.session_pnl -= effective_cost
                outcome_str = f"LOSS -${effective_cost:.4f}"
                log(
                    f"LOSS {pos.coin}-{pos.side.upper()} @{pos.fill_price:.2f} "
                    f"-> -${effective_cost:.4f} (pattern={pattern})",
                    "error",
                )

            _update_trade_log_outcome(
                pos.order_id, pos.market_slug, pos.coin, pos.side, outcome_str,
                log_file=self.log_file,
            )

    # ------------------------------------------------------------------
    # Sweep pending
    # ------------------------------------------------------------------
    async def _sweep_pending(self) -> None:
        """Scan unresolved positions and orphaned log entries."""
        pending: Dict[str, List[PositionRecord]] = {}
        in_memory_keys: Set[str] = set()
        for pos in self._all_positions:
            key = pos.order_id or f"{pos.market_slug}|{pos.coin}|{pos.side}"
            in_memory_keys.add(key)
            if not pos.resolved:
                pending.setdefault(pos.market_slug, []).append(pos)

        # Orphaned entries in log
        orphaned: Dict[str, List[Dict[str, str]]] = {}
        try:
            if self.log_file.exists():
                for line in self.log_file.read_text(encoding="utf-8").splitlines():
                    if "outcome=PENDING" not in line:
                        continue
                    fields: Dict[str, str] = {}
                    for part in line.split("|"):
                        part = part.strip()
                        if "=" in part:
                            k, v = part.split("=", 1)
                            fields[k.strip()] = v.strip()
                    oid = fields.get("order_id", "")
                    slug = fields.get("market", "")
                    if not slug:
                        continue
                    if oid and oid in in_memory_keys:
                        continue
                    if not oid:
                        coin = fields.get("coin", "")
                        side = fields.get("side", "").lower()
                        if f"{slug}|{coin}|{side}" in in_memory_keys:
                            continue
                    orphaned.setdefault(slug, []).append(fields)
        except Exception:
            pass

        if not pending and not orphaned:
            return

        all_slugs = set(pending.keys()) | set(orphaned.keys())
        gamma = GammaClient()

        for slug in all_slugs:
            try:
                market_data = await asyncio.to_thread(gamma.get_market_by_slug, slug)
                if not market_data or not market_data.get("closed", False):
                    continue

                raw_prices = market_data.get("outcomePrices", "[]")
                raw_outcomes = market_data.get("outcomes", "[]")
                prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes

                winner = None
                for idx, price in enumerate(prices):
                    if str(price) == "1" and idx < len(outcomes):
                        winner = str(outcomes[idx]).lower()
                        break
                if not winner:
                    continue

                if slug in pending:
                    log(f"[sweep] Resolved: {slug} -> {winner.upper()}", "info")
                    self._apply_resolution(pending[slug], winner)

                if slug in orphaned:
                    for entry in orphaned[slug]:
                        oid = entry.get("order_id", "")
                        coin = entry.get("coin", "?")
                        side = entry.get("side", "?").lower()
                        entry_price = _to_float(entry.get("entry", "0"))
                        fill_size = _to_float(entry.get("size", "0"))
                        cost = entry_price * fill_size
                        pattern = entry.get("pattern", "?")

                        is_win = (side == winner)
                        coin_key = coin.upper()
                        if is_win:
                            payout = fill_size
                            profit = payout - cost
                            outcome_str = f"WIN +${profit:.4f}"
                            self.total_wins += 1
                            if coin_key in self.coin_wins:
                                self.coin_wins[coin_key] += 1
                            self.session_pnl += profit
                            self.total_received += payout
                        else:
                            outcome_str = f"LOSS -${cost:.4f}"
                            self.total_losses += 1
                            if coin_key in self.coin_losses:
                                self.coin_losses[coin_key] += 1
                            self.session_pnl -= cost

                        self.total_resolved += 1
                        if coin_key in self.coin_resolved:
                            self.coin_resolved[coin_key] += 1
                        _update_trade_log_outcome(
                            oid, slug, coin, side, outcome_str,
                            log_file=self.log_file,
                        )
            except Exception as exc:
                log(f"[sweep] error ({slug}): {exc}", "warning")

    # ------------------------------------------------------------------
    # Main tick
    # ------------------------------------------------------------------
    async def _tick(self) -> None:
        now = time.time()

        # --- Outcome inference: at t >= 280s of current cycle, read prices ---
        if self._cycle_start_ts > 0:
            cycle_age = now - self._cycle_start_ts
            if cycle_age >= INFERENCE_TIME:
                self._try_infer_outcomes_from_prices()

        # --- Entry window: place GTC limit orders once ---
        if self.cycle_state == CycleState.ENTRY_WINDOW:
            cycle_age = now - self._cycle_start_ts
            ew_start = self.cfg.entry_window_start
            ew_end = self.cfg.entry_window_end
            if ew_start <= cycle_age <= ew_end:
                await self._try_enter_trades()
                # _try_enter_trades transitions to PENDING_ORDERS if GTC orders resting
                # If all orders filled immediately (or dry-run), transition to TRADED:
                if self.cycle_state == CycleState.ENTRY_WINDOW and self._traded_coins:
                    self.cycle_state = CycleState.TRADED
                    log(
                        f"Traded {len(self._traded_coins)} coin(s): "
                        f"{', '.join(sorted(self._traded_coins))}. Holding to resolution.",
                        "trade",
                    )
            elif cycle_age > ew_end:
                # Window expired with no trades placed
                if not self._traded_coins and not self._pending_orders:
                    log("Entry window expired, no trades executed.", "warning")
                    self.cycle_state = CycleState.OBSERVING

        # --- Pending orders: wait for cancel_timeout, then cancel + settle ---
        if self.cycle_state == CycleState.PENDING_ORDERS:
            elapsed = now - self._orders_placed_ts
            if elapsed >= self.cfg.cancel_timeout:
                await self._cancel_and_settle_pending()
                if self._traded_coins:
                    self.cycle_state = CycleState.TRADED
                    log(
                        f"Traded {len(self._traded_coins)} coin(s): "
                        f"{', '.join(sorted(self._traded_coins))}. Holding to resolution.",
                        "trade",
                    )
                else:
                    log("All GTC orders cancelled, no fills.", "warning")
                    self.cycle_state = CycleState.OBSERVING

        # --- Check if all markets ended ---
        if self.cycle_state in (CycleState.OBSERVING, CycleState.ENTRY_WINDOW,
                                CycleState.PENDING_ORDERS, CycleState.TRADED):
            all_ended = all(
                m.has_ended() for c in COINS
                if (m := self._coin_markets.get(c)) is not None
            ) and any(self._coin_markets.get(c) for c in COINS)
            if all_ended and self.cycle_state != CycleState.DONE:
                log("All markets ended. Cycle complete.", "info")
                self._transition_to_done()

        # --- Poll for new markets when DONE ---
        if self.cycle_state == CycleState.DONE:
            if now - self._last_done_poll >= 3.0:
                self._last_done_poll = now
                for coin in COINS:
                    mgr = self.managers.get(coin)
                    if not mgr or not mgr.current_market:
                        continue
                    market = mgr.current_market
                    ms = market.start_timestamp()
                    if ms is not None:
                        self._coin_markets[coin] = market
                        if ms != self._cycle_ts:
                            log(f"[poll] New market detected via {coin}", "info")
                            self._maybe_enter_cycle(coin, market)
                            break

        # --- Sweep pending (every 2 min) ---
        if now - self._last_sweep_ts >= 120.0:
            self._last_sweep_ts = now
            if self._sweep_task is None or self._sweep_task.done():
                self._sweep_task = asyncio.get_running_loop().create_task(
                    self._sweep_pending()
                )

        # --- Task cleanup (every 30s) ---
        if now - self._last_task_cleanup >= 30.0:
            self._last_task_cleanup = now
            self._resolution_tasks = [t for t in self._resolution_tasks if not t.done()]

        # --- Heartbeat (every 5 min) ---
        if now - self._last_heartbeat_ts >= 300.0:
            self._last_heartbeat_ts = now
            uptime_h = (now - self._session_start) / 3600
            connected = sum(1 for m in self.managers.values() if m.is_connected)
            log(
                f"[heartbeat] up={uptime_h:.1f}h  WS={connected}/4  "
                f"cycles={self.cycles_seen}  fills={self.total_fills}  "
                f"pnl=${self.session_pnl:+.2f}",
                "info",
            )
            if len(self._all_positions) > 2000:
                trimmed = [p for p in self._all_positions if not p.resolved]
                self._all_positions = trimmed

        # --- TUI render ---
        render_interval = 0.5 if _tui_active else 2.0
        if now - self._last_render_ts >= render_interval:
            self._render_tui()
            self._last_render_ts = now
            self._ticks_window = 0
            self._status_window_start = now

    # ------------------------------------------------------------------
    # TUI
    # ------------------------------------------------------------------
    def _render_tui(self) -> None:
        G = Colors.GREEN
        R = Colors.RED
        Y = Colors.YELLOW
        C = Colors.CYAN
        B = Colors.BOLD
        D = Colors.DIM
        X = Colors.RESET
        M = Colors.MAGENTA
        W = 72

        lines: list[str] = []

        def hsep() -> None:
            lines.append(f" {C}{'_' * W}{X}")

        # --- Header ---
        connected = sum(1 for m in self.managers.values() if m.is_connected)
        ws_c = G if connected == 4 else (Y if connected > 0 else R)

        countdown = f"{D}--:--{X}"
        for mgr in self.managers.values():
            if mgr.current_market:
                cd = mgr.current_market.get_countdown()
                if cd and cd[0] >= 0:
                    countdown = format_countdown(cd[0], cd[1])
                    break

        state_map = {
            CycleState.OBSERVING: (D, "OBSERVE"),
            CycleState.ENTRY_WINDOW: (Y, "ENTRY"),
            CycleState.PENDING_ORDERS: (Y, "PENDING"),
            CycleState.TRADED: (G, "HOLDING"),
            CycleState.WAITING_MARKET: (D, "WAIT"),
            CycleState.DONE: (D, "IDLE"),
        }
        sc, st = state_map.get(self.cycle_state, (D, "?"))

        up_s = time.time() - self._session_start
        up_h, up_m = int(up_s // 3600), int((up_s % 3600) // 60)
        up_str = f"{up_h}h{up_m:02d}m" if up_h else f"{up_m}m"

        if self.cfg.dry_run and self.cfg.name:
            dry = f" {Y}[SIM: {self.cfg.name}]{X}"
        elif self.cfg.dry_run:
            dry = f" {R}[DRY]{X}"
        else:
            dry = ""

        rules_str = ", ".join(f"{r.pattern}->{r.buy_side[0].upper()}" for r in self.cfg.rules)

        lines.append("")
        lines.append(
            f"  {M}{B}SEQUENCE{X}{dry}"
            f"   {ws_c}ws:{connected}/4{X}"
            f"   {countdown}"
            f"   {sc}{B}{st}{X}"
            f"   {D}{up_str}{X}"
        )
        kelly_str = f"  kelly={self.cfg.kelly}" if self.cfg.kelly > 0 else ""
        lines.append(
            f"  {D}rules: {rules_str}  size={self.cfg.size}  "
            f"max_ask={self.cfg.max_ask:.2f}{kelly_str}  cycle #{self.cycles_seen}{X}"
        )
        hsep()

        # --- Outcome history ---
        lines.append(f"  {B}Outcome History{X}")
        for coin in COINS:
            hist = list(self._outcome_history[coin])
            hist_str = " ".join(hist) if hist else "--"
            # Highlight if pattern matches
            matches = []
            for rule in self.cfg.rules:
                plen = len(rule.pattern)
                if len(hist) >= plen and "".join(hist[-plen:]) == rule.pattern:
                    matches.append(f"{rule.pattern}->{rule.buy_side[0].upper()}")

            match_tag = ""
            if matches:
                match_tag = f"  {G}{B}MATCH: {', '.join(matches)}{X}"

            lines.append(f"    {B}{coin:>4}{X}  [{hist_str}]{match_tag}")
        hsep()

        # --- Price grid ---
        lines.append(
            f"  {D}{'':>5}    {'UP ask':>8}    {'DOWN ask':>8}{X}"
        )
        for coin in COINS:
            ua = self._best_asks[coin]["up"]
            da = self._best_asks[coin]["down"]
            lines.append(f"  {B}{coin:>5}{X}    {D}{ua:>8.4f}{X}    {D}{da:>8.4f}{X}")
        hsep()

        # --- Current positions ---
        if self._current_positions:
            lines.append(f"  {B}Positions (this cycle){X}")
            for pos in self._current_positions:
                tag = f"{D}pending{X}"
                if pos.resolved:
                    if pos.won:
                        tag = f"{G}WIN +${pos.pnl or 0:.2f}{X}"
                    else:
                        tag = f"{R}LOSS -${abs(pos.pnl or 0):.2f}{X}"
                lines.append(
                    f"    {B}{pos.coin}{X}-{pos.side.upper()}"
                    f"  @{pos.fill_price:.3f} x{pos.fill_size:.1f}"
                    f"  [{pos.pattern}] {tag}"
                )
        else:
            lines.append(f"  positions: {D}none{X}")
        hsep()

        # --- Stats ---
        pnl_c = G if self.session_pnl >= 0 else R
        wr = (
            f"{(self.total_wins / self.total_resolved) * 100:.0f}%"
            if self.total_resolved > 0 else "--"
        )
        lines.append(
            f"  {B}{self.total_fills}{X} fills"
            f"   {G}{self.total_wins}W{X}/{R}{self.total_losses}L{X}"
            f"   win:{B}{wr}{X}"
            f"   pnl:{pnl_c}{B}${self.session_pnl:+.2f}{X}"
        )

        # Per-pattern breakdown
        pattern_parts = []
        for rule in self.cfg.rules:
            p = rule.pattern
            pw = self.pattern_wins.get(p, 0)
            pl = self.pattern_losses.get(p, 0)
            pf = self.pattern_fills.get(p, 0)
            pr = pw + pl
            pwr = f"{(pw / pr) * 100:.0f}%" if pr > 0 else "--"
            pattern_parts.append(f"{p}: {G}{pw}W{X}/{R}{pl}L{X}={pwr}")
        if pattern_parts:
            lines.append(f"  {D}patterns:{X} " + "  ".join(pattern_parts))

        # Per-coin breakdown
        coin_parts = []
        for coin in COINS:
            cw = self.coin_wins[coin]
            cl = self.coin_losses[coin]
            cr = self.coin_resolved[coin]
            cwr = f"{(cw / cr) * 100:.0f}%" if cr > 0 else "--"
            coin_parts.append(f"{B}{coin}{X} {G}{cw}W{X}/{R}{cl}L{X}={cwr}")
        lines.append(f"  {D}|{X} " + f"  {D}|{X} ".join(coin_parts))
        hsep()

        # --- Trade history (last 8) ---
        lines.append(f"  {B}Trades{X}")
        recent = self._all_positions[-8:] if self._all_positions else []
        if recent:
            for p in reversed(recent):
                ts = datetime.fromtimestamp(p.fill_time).strftime("%H:%M")
                tag = f"{D}...{X}"
                if p.resolved:
                    if p.won:
                        profit = (p.payout or 0) - p.cost
                        tag = f"{G}{B}WIN{X} {G}+${profit:.2f}{X}"
                    else:
                        tag = f"{R}LOSS{X} {R}-${p.cost:.2f}{X}"
                lines.append(
                    f"  {D}{ts}{X}"
                    f"  {B}{p.coin}{X}-{p.side.upper():<4}"
                    f"  {C}@{p.fill_price:.3f}{X} x{p.fill_size:.1f}"
                    f"  [{p.pattern}] {tag}"
                )
        else:
            lines.append(f"  {D}waiting for pattern signals...{X}")
        hsep()

        # --- Events ---
        lines.append(f"  {B}Events{X}")
        evts = _log_buffer[-6:] if _log_buffer else []
        if evts:
            for msg in evts:
                lines.append(msg)
        else:
            lines.append(f"  {D}starting up...{X}")
        hsep()
        lines.append("")

        print("\033[H\033[J" + "\n".join(lines), flush=True)

    # ------------------------------------------------------------------
    # Cleanup & summary
    # ------------------------------------------------------------------
    async def _cleanup(self) -> None:
        for task in self._resolution_tasks:
            if not task.done():
                task.cancel()
        for mgr in self.managers.values():
            try:
                await mgr.stop()
            except Exception:
                pass

    def _print_summary(self) -> None:
        print()
        print("=" * 60)
        print("  SEQUENCE PATTERN - SESSION SUMMARY")
        print("=" * 60)
        rules_str = ", ".join(
            f"{r.pattern}->BUY {r.buy_side.upper()}" for r in self.cfg.rules
        )
        print(f"  Rules:         {rules_str}")
        print(f"  Size:          {self.cfg.size}")
        print(f"  Dry run:       {self.cfg.dry_run}")
        print(f"  Cycles seen:   {self.cycles_seen}")
        print(f"  Total fills:   {self.total_fills}")
        print(
            f"  Resolved:      {self.total_resolved}"
            f"  ({self.total_wins}W / {self.total_losses}L)"
        )
        print(f"  Total spent:   ${self.total_spent:.4f}")
        print(f"  Total received:${self.total_received:.4f}")
        print(f"  Session PnL:   ${self.session_pnl:+.4f}")

        if self.total_resolved > 0:
            wr = (self.total_wins / self.total_resolved) * 100
            print(f"  Win rate:      {wr:.1f}%")

        # Pattern breakdown
        print()
        print("  Per-pattern breakdown:")
        for rule in self.cfg.rules:
            p = rule.pattern
            pw = self.pattern_wins.get(p, 0)
            pl = self.pattern_losses.get(p, 0)
            pr = pw + pl
            pwr = f"{(pw / pr) * 100:.1f}%" if pr > 0 else "--"
            print(f"    {p} -> BUY {rule.buy_side.upper()}: {pw}W / {pl}L (win rate: {pwr})")

        # Coin breakdown
        print()
        print("  Per-coin breakdown:")
        for coin in COINS:
            cw = self.coin_wins[coin]
            cl = self.coin_losses[coin]
            cr = self.coin_resolved[coin]
            cwr = f"{(cw / cr) * 100:.1f}%" if cr > 0 else "--"
            print(f"    {coin:>4}: {cw}W / {cl}L  (win rate: {cwr})")

        # Outcome history
        print()
        print("  Final outcome history:")
        for coin in COINS:
            hist = " ".join(self._outcome_history[coin])
            print(f"    {coin:>4}: [{hist}]")

        if self._all_positions:
            print()
            print("  All fills:")
            for p in self._all_positions:
                res = ""
                if p.resolved:
                    if p.won:
                        profit = (p.payout or 0) - p.cost
                        res = f"  WIN +${profit:.4f}"
                    else:
                        res = f"  LOSS -${p.cost:.4f}"
                ts = datetime.fromtimestamp(p.fill_time).strftime("%H:%M:%S")
                print(
                    f"    {ts}  {p.coin}-{p.side.upper():>4}"
                    f"  @{p.fill_price:.4f} x{p.fill_size:.2f}"
                    f"  cost=${p.cost:.4f}  [{p.pattern}]{res}"
                )

        print("=" * 60)
        print(f"  Trade log: {self.log_file}")
        print("=" * 60)


# ===================================================================
# Component builder
# ===================================================================
def build_components() -> Tuple[Config, OrderSigner, ClobClient]:
    config = Config.from_env()

    private_key = os.environ.get("POLY_PRIVATE_KEY", "")
    if not private_key:
        print("ERROR: POLY_PRIVATE_KEY is not set")
        raise SystemExit(1)

    signer = OrderSigner(private_key, chain_id=config.clob.chain_id)

    clob = ClobClient(
        host=config.clob.host,
        chain_id=config.clob.chain_id,
        signature_type=config.clob.signature_type,
        funder=config.safe_address,
        signer_address=signer.address,
        builder_creds=config.builder,
    )

    api_creds = clob.create_or_derive_api_key(signer)
    clob.set_api_creds(api_creds)

    return config, signer, clob


# ===================================================================
# CLI
# ===================================================================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mean-Reversion: config-driven multi-pattern strategy for 5-min Up/Down markets"
    )
    parser.add_argument(
        "--config", type=str, default=str(DEFAULT_CONFIG),
        help=f"Path to YAML config file (default: {DEFAULT_CONFIG.name})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate without placing real orders",
    )
    parser.add_argument(
        "--name", type=str, default="",
        help="Instance name for sim log files",
    )
    args = parser.parse_args()

    # Load config from YAML
    config_path = args.config
    if not Path(config_path).exists():
        print(f"ERROR: Config file not found: {config_path}")
        raise SystemExit(1)

    name = args.name
    if not name and args.dry_run:
        name = "meanrev_sim"

    cfg = SequenceConfig.from_yaml(config_path, dry_run=args.dry_run, name=name)

    print()
    log("=" * 60, "info")
    log("  MEAN-REVERSION STRATEGY", "info")
    log(f"  Config: {config_path}", "info")
    log("=" * 60, "info")
    print()

    log("Initializing components...", "info")
    bot_config, signer, clob = build_components()
    log(f"  EOA:   {signer.address}", "info")
    log(f"  Proxy: {bot_config.safe_address}", "info")
    log(f"  Sig:   type {bot_config.clob.signature_type}", "info")
    if cfg.dry_run:
        log(f"  Mode:  SIM [{cfg.name}]", "info")
    print()

    log(f"Entry window: {cfg.entry_window_start}-{cfg.entry_window_end}s", "info")
    log(f"Cancel timeout: {cfg.cancel_timeout:.0f}s (GTC limit orders)", "info")
    log(f"Size: ${cfg.size:.0f} flat (no Kelly)", "info")
    log(f"Order type: GTC limit at max_ask (maker-friendly)", "info")
    print()

    log("Rules (priority order, longest pattern first):", "info")
    for rule in cfg.rules:
        coins_str = ",".join(rule.coins) if rule.coins else "ALL"
        log(
            f"  {rule.pattern} -> BUY {rule.buy_side.upper()}  "
            f"max_ask=${rule.max_ask:.2f}  coins=[{coins_str}]",
            "info",
        )
    print()

    strategy = SequenceStrategy(cfg, bot_config, signer, clob)
    asyncio.run(strategy.run())


if __name__ == "__main__":
    main()
