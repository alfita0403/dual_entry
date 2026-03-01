"""
Sequence Pattern Strategy - 5-Minute Up/Down Crypto Markets

Exploits autocorrelation patterns discovered in research:
  - DDD reversal: after 3 consecutive DOWN outcomes, buy UP  (~60-73% win)
  - UD continuation: after UP-then-DOWN, buy DOWN  (~66-78% win)

The market maker does NOT reprice after streaks, so these represent
unpriced edges of +5 to +17 percentage points above the implied
probability from the ask price.

Outcome inference:
  Each 5-minute cycle is 300 seconds.  At t >= 280s, the UP ask price
  reveals the outcome with near-certainty:
    UP ask < 0.15  =>  outcome is DOWN  (99%+ certain)
    UP ask > 0.85  =>  outcome is UP    (99%+ certain)
  We record this into a rolling deque per coin.

Pattern matching:
  At the START of a new cycle (t = 5-15s), check each coin's history.
  If the most recent outcomes match the configured pattern, fire a FOK
  BUY for the configured side.  Hold to resolution (no TP/timeout).

Usage:
    python strategies/sequence.py --pattern DDD --side UP --size 5
    python strategies/sequence.py --pattern UD --side DOWN --size 5
    python strategies/sequence.py --dry-run --pattern DDD --side UP --name "ddd_test"
    python strategies/sequence.py --pattern DDD,UD --side UP,DOWN --size 10
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

TRADE_LOG_FILE = Path(__file__).resolve().parent.parent / "sequence_trades.txt"

# Outcome inference thresholds (applied at t >= INFERENCE_TIME)
# Last 5 seconds of the 300s cycle — prices are near-settled by then
INFERENCE_TIME = 295       # seconds into cycle to read outcome
UP_THRESHOLD = 0.95        # UP ask > this => outcome is UP (near-certain)
DOWN_THRESHOLD = 0.05      # UP ask < this => outcome is DOWN (near-certain)

# Max ask price filter — don't buy when ask is too expensive (bad risk/reward)
DEFAULT_MAX_ASK = 0.60     # only enter if ask <= this

# How long after cycle start to look for pattern-match entries
ENTRY_WINDOW_START = 5     # seconds after cycle start
ENTRY_WINDOW_END = 30      # seconds after cycle start

# Dry-run simulation penalties (match stat_arb.py)
SIM_ENTRY_SLIP = 0.01
SIM_FEE_RATE = 0.015       # 1.5% one-way taker fee


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------
# Each pattern is a tuple of (name, sequence_to_match, side_to_buy)
# sequence_to_match is a string like "DDD" meaning last 3 outcomes were D,D,D
BUILTIN_PATTERNS: Dict[str, Tuple[str, str]] = {
    "DDD":  ("DDD",  "UP"),     # 3 consecutive DOWN => buy UP (reversal)
    "UD":   ("UD",   "DOWN"),   # UP then DOWN => buy DOWN (continuation)
    "UU":   ("UU",   "DOWN"),   # 2 consecutive UP => buy DOWN (reversal)
    "UUU":  ("UUU",  "DOWN"),   # 3 consecutive UP => buy DOWN (reversal)
    "DU":   ("DU",   "UP"),     # DOWN then UP => buy UP (continuation)
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


# ===================================================================
# Configuration
# ===================================================================
@dataclass
class PatternRule:
    """One pattern->action rule."""
    pattern: str          # e.g. "DDD"
    buy_side: str         # "up" or "down"


@dataclass
class SequenceConfig:
    rules: List[PatternRule]
    size: float = 5.0
    slippage: float = 0.03
    max_ask: float = DEFAULT_MAX_ASK  # max ask price to enter
    max_trades_per_cycle: int = 4   # one per coin
    dry_run: bool = False
    market_check_interval: float = 5.0
    name: str = ""

    def validate(self) -> None:
        if self.size < 5:
            raise ValueError(f"size must be >= 5, got {self.size}")
        if not 0.30 <= self.max_ask <= 0.90:
            raise ValueError(f"max_ask must be 0.30-0.90, got {self.max_ask}")
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


# ===================================================================
# State machine
# ===================================================================
class CycleState(enum.Enum):
    WAITING_MARKET = "WAITING_MARKET"
    OBSERVING = "OBSERVING"       # Watching cycle, inferring outcomes at end
    ENTRY_WINDOW = "ENTRY_WINDOW" # New cycle, checking for pattern matches
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
        # maxlen=5 supports patterns up to length 5
        self._outcome_history: Dict[str, deque] = {
            c: deque(maxlen=5) for c in COINS
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
        if self.cycle_state in (CycleState.OBSERVING, CycleState.ENTRY_WINDOW, CycleState.TRADED):
            self._transition_to_done()

        self._cycle_ts = market_start
        self._cycle_start_ts = float(market_start)
        self._traded_coins.clear()
        self._current_positions.clear()
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
        """Check all coins against all rules. Return list of (coin, rule) matches."""
        matches = []
        for coin in COINS:
            hist = list(self._outcome_history[coin])
            if not hist:
                continue

            for rule in self.cfg.rules:
                pattern_len = len(rule.pattern)
                if len(hist) < pattern_len:
                    continue

                # Check if the last N outcomes match the pattern
                recent = "".join(hist[-pattern_len:])
                if recent == rule.pattern:
                    matches.append((coin, rule))
                    break  # First matching rule wins per coin

        return matches

    # ------------------------------------------------------------------
    # Entry execution
    # ------------------------------------------------------------------
    async def _try_enter_trades(self) -> None:
        """Called during ENTRY_WINDOW to fire FOK buys on pattern matches."""
        matches = self._find_pattern_matches()

        for coin, rule in matches:
            if coin in self._traded_coins:
                continue
            if len(self._current_positions) >= self.cfg.max_trades_per_cycle:
                break

            market = self._coin_markets.get(coin)
            if not market:
                continue

            buy_side = rule.buy_side
            token_id = market.token_ids.get(buy_side, "")
            if not token_id:
                continue

            buy_price = self._best_asks[coin][buy_side]
            if buy_price <= 0.01 or buy_price > self.cfg.max_ask:
                log(
                    f"Skip {coin}: {buy_side} ask={buy_price:.3f} "
                    f"(max_ask={self.cfg.max_ask:.2f})",
                    "warning",
                )
                continue

            # Add slippage for FOK
            fok_price = min(buy_price + self.cfg.slippage, 0.99)

            hist_str = "".join(self._outcome_history[coin])
            log(
                f"SIGNAL: {coin} [{hist_str}] -> {rule.pattern} -> BUY {buy_side.upper()} "
                f"@ {buy_price:.3f} (FOK @ {fok_price:.3f})",
                "trade",
            )

            if self.cfg.dry_run:
                tracker = self._simulate_buy(coin, buy_side, buy_price, market, rule.pattern)
            else:
                tracker = await self._submit_live_buy(
                    coin, buy_side, token_id, market, fok_price, rule.pattern
                )

            if tracker:
                self._traded_coins.add(coin)

    def _simulate_buy(
        self, coin: str, side: str, ask_price: float, market: MarketInfo, pattern: str,
    ) -> Optional[PositionRecord]:
        """Simulate a fill for dry-run mode."""
        sim_price = ask_price + SIM_ENTRY_SLIP
        sim_size = self.cfg.size
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

    async def _submit_live_buy(
        self,
        coin: str,
        side: str,
        token_id: str,
        market: MarketInfo,
        buy_price: float,
        pattern: str,
    ) -> Optional[PositionRecord]:
        """Submit a FOK limit BUY to the CLOB."""
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
                price=buy_price,
                size=self.cfg.size,
                side="BUY",
                funder=self.bot_config.safe_address,
                fee_rate_bps=fee_rate_bps,
                signature_type=self.bot_config.clob.signature_type,
                neg_risk=market.neg_risk,
                tick_size=market.tick_size,
            )

            # Hot path: sign + POST on dedicated thread
            t_start = time.perf_counter()
            loop = asyncio.get_running_loop()
            response, t_sign_us, t_post_us = await loop.run_in_executor(
                self._clob_executor,
                self._sync_sign_and_post, order, "FOK",
            )
            t_total_us = (time.perf_counter() - t_start) * 1_000_000

            timing = f"[sign={t_sign_us:.0f}us post={t_post_us:.0f}us total={t_total_us:.0f}us]"

            if not response.get("success", False):
                error = response.get("errorMsg", "unknown")
                log(f"FOK FAIL {label}: {error} {timing}", "error")
                return None

            order_id = (
                response.get("orderID")
                or response.get("orderId")
                or response.get("order_id")
                or ""
            )

            status = str(response.get("status", "")).lower()

            if status in {"matched", "filled", "executed", "complete", "completed"}:
                taking = _to_float(response.get("takingAmount", 0))
                making = _to_float(response.get("makingAmount", 0))
                fp = making / max(taking, 1e-12) if taking > 0 else buy_price
                fill_size = taking if taking > 0 else self.cfg.size
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
                self.total_orders_placed += 1
                self.total_spent += cost
                self.total_shares += fill_size
                if pattern in self.pattern_fills:
                    self.pattern_fills[pattern] += 1

                _append_trade_log(pos, self.cfg, outcome="PENDING", log_file=self.log_file)
                log(
                    f"FILLED {label} @ {fp:.4f} x{fill_size:.2f} "
                    f"(pattern={pattern}) {timing}",
                    "success",
                )
                return pos

            # FOK not filled = killed
            log(f"FOK KILLED {label}: order not filled {timing}", "warning")
            self.total_orders_placed += 1
            return None

        except Exception as exc:
            log(f"FOK ERR {label}: {exc}", "error")
            return None

    def _sync_sign_and_post(
        self, order: Order, order_type: str = "FOK"
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

        # --- Entry window: fire trades in first 5-30s of cycle ---
        if self.cycle_state == CycleState.ENTRY_WINDOW:
            cycle_age = now - self._cycle_start_ts
            if ENTRY_WINDOW_START <= cycle_age <= ENTRY_WINDOW_END:
                await self._try_enter_trades()
                # If we traded at least once, transition
                if self._traded_coins:
                    self.cycle_state = CycleState.TRADED
                    log(
                        f"Traded {len(self._traded_coins)} coin(s): "
                        f"{', '.join(sorted(self._traded_coins))}. Holding to resolution.",
                        "trade",
                    )
            elif cycle_age > ENTRY_WINDOW_END:
                # Window expired with no trades
                if not self._traded_coins:
                    log("Entry window expired, no trades executed.", "warning")
                    self.cycle_state = CycleState.OBSERVING

        # --- Check if all markets ended ---
        if self.cycle_state in (CycleState.OBSERVING, CycleState.ENTRY_WINDOW, CycleState.TRADED):
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
        lines.append(
            f"  {D}rules: {rules_str}  size={self.cfg.size}  "
            f"max_ask={self.cfg.max_ask:.2f}  cycle #{self.cycles_seen}{X}"
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
        description="Sequence Pattern: exploit autocorrelation in 5-min Up/Down markets"
    )
    parser.add_argument(
        "--pattern", type=str, required=True,
        help=(
            "Outcome pattern(s) to match. Comma-separated for multiple. "
            "Each char is U(p) or D(own). Examples: DDD, UD, UUU, DDD,UD"
        ),
    )
    parser.add_argument(
        "--side", type=str, default=None,
        help=(
            "Side(s) to buy on match. Comma-separated, same length as --pattern. "
            "If omitted, uses built-in defaults (DDD->UP, UD->DOWN, etc). "
            "Examples: UP, DOWN, UP,DOWN"
        ),
    )
    parser.add_argument(
        "--size", type=float, default=5.0,
        help="Shares per order (min 5, default: 5)",
    )
    parser.add_argument(
        "--slippage", type=float, default=0.03,
        help="FOK slippage buffer (default: 0.03)",
    )
    parser.add_argument(
        "--max-ask", type=float, default=DEFAULT_MAX_ASK,
        help=f"Max ask price to enter (default: {DEFAULT_MAX_ASK}). Rejects expensive entries with bad risk/reward.",
    )
    parser.add_argument(
        "--max-trades", type=int, default=4,
        help="Max trades per cycle (default: 4 = one per coin)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate without placing real orders",
    )
    parser.add_argument(
        "--name", type=str, default="",
        help="Instance name for sim log files",
    )
    parser.add_argument(
        "--market-check-interval", type=float, default=5.0,
        help="Seconds between market discovery checks (default: 5)",
    )
    args = parser.parse_args()

    # Parse patterns and sides
    patterns = [p.strip().upper() for p in args.pattern.split(",")]

    if args.side:
        sides = [s.strip().lower() for s in args.side.split(",")]
        if len(sides) != len(patterns):
            print(f"ERROR: --side has {len(sides)} entries but --pattern has {len(patterns)}")
            raise SystemExit(1)
    else:
        # Use built-in defaults
        sides = []
        for p in patterns:
            if p in BUILTIN_PATTERNS:
                sides.append(BUILTIN_PATTERNS[p][1].lower())
            else:
                print(
                    f"ERROR: No built-in default side for pattern '{p}'. "
                    f"Use --side to specify. Known patterns: {list(BUILTIN_PATTERNS.keys())}"
                )
                raise SystemExit(1)

    rules = [PatternRule(pattern=p, buy_side=s) for p, s in zip(patterns, sides)]

    name = args.name
    if not name and args.dry_run:
        name = "_".join(f"{r.pattern}{r.buy_side[0]}" for r in rules)

    cfg = SequenceConfig(
        rules=rules,
        size=args.size,
        slippage=args.slippage,
        max_ask=args.max_ask,
        max_trades_per_cycle=args.max_trades,
        dry_run=args.dry_run,
        market_check_interval=args.market_check_interval,
        name=name,
    )
    cfg.validate()

    print()
    log("Initializing components...", "info")
    bot_config, signer, clob = build_components()
    log(f"  EOA:   {signer.address}", "info")
    log(f"  Proxy: {bot_config.safe_address}", "info")
    log(f"  Sig:   type {bot_config.clob.signature_type}", "info")
    if cfg.dry_run:
        log(f"  Mode:  SIM [{cfg.name}]", "info")
    print()

    log("Rules:", "info")
    for rule in cfg.rules:
        default_tag = ""
        if rule.pattern in BUILTIN_PATTERNS:
            _, default_side = BUILTIN_PATTERNS[rule.pattern]
            if rule.buy_side == default_side.lower():
                default_tag = " (built-in)"
        log(
            f"  {rule.pattern} -> BUY {rule.buy_side.upper()}{default_tag}",
            "info",
        )
    print()

    strategy = SequenceStrategy(cfg, bot_config, signer, clob)
    asyncio.run(strategy.run())


if __name__ == "__main__":
    main()
