"""
Correlation Hunter - Multi-Coin 5-Minute Markets

Monitors all 4 coins (BTC, ETH, SOL, XRP) simultaneously on their 5-minute
Up/Down markets.  Bets on cross-market correlation: when BTC's ask on one
side drops to the threshold, scans the other 3 coins for the OPPOSITE side
also hitting the threshold, then buys both as a pair.

Correlation thesis:
    All 4 coins tend to resolve on the same side.  Buying BTC UP at $0.20
    + SOL DOWN at $0.20 = $0.40 cost.  If both go UP or both go DOWN,
    exactly one wins = $1.00 payout = $0.60 profit.  Only lose if they
    go opposite directions.

Trigger logic:
    - Trigger coin: BTC only.  Monitor BTC UP and DOWN asks.
    - Trigger condition: When BTC UP or DOWN ask <= price threshold.
    - Scanning: While BTC is triggered, scan ETH/SOL/XRP for the
      OPPOSITE side at <= threshold.
    - Scanning is dynamic: if BTC goes back above threshold, stop
      scanning.  If it comes back, resume.
    - Pair execution: When a follower also triggers, buy BOTH (BTC side
      + follower opposite side) simultaneously.
    - BTC is bought once per cycle (first pair locks the side).
    - Followers can accumulate: if BTC UP + XRP DOWN bought, then later
      ETH DOWN triggers -> buy ETH DOWN too (BTC already bought).

Trade log:
    Every fill is appended to ``correlation_trades.txt`` (never overwritten).
    Each line contains timestamp, market, coin, side, price, size, cost,
    config params, and outcome (when resolved).

Usage:
    python strategies/correlation.py --dry-run
    python strategies/correlation.py --window 60 --price 0.20 --size 5
    python strategies/correlation.py --window 120 --price 0.20 --size 10
"""

import argparse
import asyncio
import enum
import json
import logging
import os
import sys
import time
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
from lib.console import Colors, format_countdown, StatusDisplay  # noqa: E402
from src.client import ClobClient  # noqa: E402
from src.config import Config  # noqa: E402
from src.gamma_client import GammaClient  # noqa: E402
from src.signer import Order, OrderSigner  # noqa: E402
from src.websocket_client import OrderbookSnapshot  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COINS: List[str] = ["BTC", "ETH", "SOL", "XRP"]
FOLLOWER_COINS: List[str] = ["ETH", "SOL", "XRP"]

TRADE_LOG_FILE = Path(__file__).resolve().parent.parent / "correlation_trades.txt"

# ---------------------------------------------------------------------------
# TUI-aware logging (same pattern as dual_entry)
# ---------------------------------------------------------------------------
_log_buffer: list = []
_tui_active = False


def ts_now() -> str:
    return datetime.now().strftime("%H:%M:%S")


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
def _append_trade_log(
    pos: "PositionRecord",
    cfg: "CorrelationConfig",
    outcome: str = "PENDING",
    log_file: Optional[Path] = None,
) -> None:
    """Append one line per fill to the persistent trade log file."""
    target = log_file or TRADE_LOG_FILE
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    now_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = (
        f"{now_utc} | {now_local} | "
        f"order_id={pos.order_id} | "
        f"market={pos.market_slug} | coin={pos.coin} | side={pos.side.upper()} | "
        f"price={pos.fill_price:.4f} | size={pos.fill_size:.1f} | "
        f"cost=${pos.cost:.4f} | "
        f"window={cfg.window:.0f}s | cfg_price={cfg.price} | cfg_size={cfg.size} | "
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
    """Update the outcome field for a specific trade in the log file.

    Matches by order_id first (unique key), falls back to market+coin+side.
    """
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


# ===================================================================
# Configuration
# ===================================================================
@dataclass
class CorrelationConfig:
    """Configuration for the Correlation Hunter strategy."""

    window: float = 60.0  # seconds from market birth
    price: float = 0.20  # max buy price (limit) -- trigger threshold
    size: float = 5.0  # shares per order
    dry_run: bool = False
    market_check_interval: float = 5.0
    name: str = ""  # instance identifier (auto-generated if empty)

    def validate(self) -> None:
        if not 0.01 <= self.price <= 0.99:
            raise ValueError(f"price must be 0.01-0.99, got {self.price}")
        if not 1.0 <= self.window <= 300.0:
            raise ValueError(f"window must be 1-300 seconds, got {self.window}")
        if self.size < 5:
            raise ValueError(f"size must be >= 5, got {self.size}")


# ===================================================================
# State machine
# ===================================================================
class CycleState(enum.Enum):
    WAITING_MARKET = "WAITING_MARKET"
    ACTIVE = "ACTIVE"
    HOLDING = "HOLDING"
    DONE = "DONE"


# ===================================================================
# Data classes
# ===================================================================
@dataclass
class OrderTracker:
    coin: str
    side: str
    token_id: str
    order_id: str
    price: float
    size: float
    placed_at: float
    market_slug: str = ""
    filled: bool = False
    fill_price: float = 0.0
    fill_size: float = 0.0
    fill_time: float = 0.0
    cancelled: bool = False


@dataclass
class PositionRecord:
    coin: str
    side: str
    fill_price: float
    fill_size: float
    fill_time: float
    market_slug: str
    order_id: str = ""
    cost: float = 0.0
    resolved: bool = False
    won: bool = False
    payout: float = 0.0


# ===================================================================
# Strategy
# ===================================================================
class CorrelationStrategy:
    """Buy correlated pairs across 4 coins, hold to expiry."""

    def __init__(
        self,
        cfg: CorrelationConfig,
        bot_config: Config,
        signer: OrderSigner,
        clob: ClobClient,
    ):
        self.cfg = cfg
        self.bot_config = bot_config
        self.signer = signer
        self.clob = clob

        # Per-instance log file: sim instances get their own file
        if cfg.dry_run and cfg.name:
            self.log_file = (
                TRADE_LOG_FILE.parent / f"correlation_sim_{cfg.name}.txt"
            )
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
        self._cycle_deadline: float = 0.0
        self._coins_entered: Set[str] = set()

        # Orders (filled orders tracked here)
        self._orders: Dict[str, OrderTracker] = {}
        self._orders_placed_this_cycle: int = 0

        # Positions
        self._current_positions: List[PositionRecord] = []
        self._all_positions: List[PositionRecord] = []

        # Session stats (restored from trade log on startup)
        self.cycles_seen: int = 0
        self.total_orders_placed: int = 0
        self.total_fills: int = 0
        self.total_wins: int = 0
        self.total_losses: int = 0
        self.total_resolved: int = 0
        self.session_pnl: float = 0.0
        self.total_spent: float = 0.0

        # Per-coin stats
        self.coin_wins: Dict[str, int] = {c: 0 for c in COINS}
        self.coin_losses: Dict[str, int] = {c: 0 for c in COINS}
        self.coin_resolved: Dict[str, int] = {c: 0 for c in COINS}

        self._load_stats_from_log()

        # Per-coin caches
        self._best_asks: Dict[str, Dict[str, float]] = {
            c: {"up": 1.0, "down": 1.0} for c in COINS
        }
        self._coin_markets: Dict[str, Optional[MarketInfo]] = {
            c: None for c in COINS
        }

        # Fee cache
        self._fee_rate_cache: Dict[str, int] = {}

        # --- Correlation state ---
        self._btc_locked_side: Optional[str] = None  # "up" or "down" once BTC bought
        self._btc_bought: bool = False
        self._followers_bought: Set[str] = set()  # e.g. {"ETH", "SOL"}
        self._scanning: bool = False
        self._current_trigger_side: Optional[str] = None  # BTC side that triggered

        # TUI
        self._last_render_ts: float = 0.0
        self._ticks_total: int = 0
        self._ticks_window: int = 0
        self._last_tick_ts: float = 0.0
        self._status_window_start: float = time.time()

        # Tasks
        self._fill_watcher_task: Optional[asyncio.Task] = None
        self._resolution_tasks: List[asyncio.Task] = []

        # 24h stability
        self._session_start: float = time.time()
        self._last_heartbeat_ts: float = 0.0
        self._last_done_poll: float = 0.0
        self._last_task_cleanup: float = 0.0
        self._last_sweep_ts: float = 0.0
        self._sweep_task: Optional[asyncio.Task] = None
        self._ws_reconnect_count: int = 0
        self._scheduled_slugs: Set[str] = set()

    # ------------------------------------------------------------------
    # Restore stats from trade log
    # ------------------------------------------------------------------
    def _load_stats_from_log(self) -> None:
        """Read trade log and restore cumulative stats."""
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

                cost_str = fields.get("cost", "0").lstrip("$")
                cost = _to_float(cost_str)
                outcome = fields.get("outcome", "")

                coin = fields.get("coin", "").upper()

                self.total_fills += 1
                self.total_spent += cost
                # PnL and W/L only change on resolution.

                if outcome.startswith("WIN"):
                    self.total_wins += 1
                    self.total_resolved += 1
                    if coin in self.coin_wins:
                        self.coin_wins[coin] += 1
                        self.coin_resolved[coin] += 1
                    profit_str = outcome.replace("WIN +$", "").replace("WIN +", "")
                    self.session_pnl += _to_float(profit_str)
                elif outcome.startswith("LOSS"):
                    self.total_losses += 1
                    self.total_resolved += 1
                    if coin in self.coin_losses:
                        self.coin_losses[coin] += 1
                        self.coin_resolved[coin] += 1
                    self.session_pnl -= cost
        except Exception:
            pass  # If log is corrupted, start fresh

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def run(self) -> None:
        global _tui_active

        log("Correlation Hunter Strategy started (4-coin WebSocket)", "success")
        log(f"  window:  {self.cfg.window:.0f}s")
        log(f"  price:   {self.cfg.price}")
        log(f"  size:    {self.cfg.size} shares")
        log(f"  dry_run: {self.cfg.dry_run}")
        log(f"  log:     {self.log_file}")
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
            # Invalidate stale orderbook data from old market
            self._best_asks[coin] = {"up": 1.0, "down": 1.0}
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

        try:
            asset_id = snapshot.asset_id
            for side in ("up", "down"):
                if market.token_ids.get(side) == asset_id:
                    asks = snapshot.asks
                    best = min(float(a.price) for a in asks) if asks else 1.0
                    self._best_asks[coin][side] = best
                    break
        except Exception:
            pass  # Malformed orderbook data; ignore and wait for next update

    # ------------------------------------------------------------------
    # Cycle management
    # ------------------------------------------------------------------
    def _maybe_enter_cycle(self, coin: str, market: MarketInfo) -> None:
        market_start = market.start_timestamp()
        if market_start is None:
            return

        now = time.time()
        deadline = float(market_start) + self.cfg.window

        # Same cycle -- just register coin
        if self._cycle_ts == market_start:
            if coin not in self._coins_entered:
                self._coins_entered.add(coin)
            return

        # --- New cycle ---
        if self.cycle_state in (CycleState.ACTIVE, CycleState.HOLDING):
            self._transition_to_done()

        self._cycle_ts = market_start
        self._cycle_start_ts = float(market_start)
        self._cycle_deadline = deadline
        self._orders.clear()
        self._current_positions.clear()
        self._orders_placed_this_cycle = 0
        self._coins_entered.clear()
        self.cycles_seen += 1

        # Reset all orderbook caches
        self._best_asks = {c: {"up": 1.0, "down": 1.0} for c in COINS}
        # Clear fee cache — token IDs change every cycle
        self._fee_rate_cache.clear()

        # --- Reset correlation state ---
        self._btc_locked_side = None
        self._btc_bought = False
        self._followers_bought.clear()
        self._scanning = False
        self._current_trigger_side = None

        if now >= deadline:
            market_age = now - self._cycle_start_ts
            log(
                f"Market age {market_age:.0f}s > window {self.cfg.window:.0f}s. Skip.",
                "warning",
            )
            self.cycle_state = CycleState.DONE
            return

        remaining = max(0.0, deadline - now)
        log(
            f"NEW CYCLE #{self.cycles_seen}: "
            f"window={self.cfg.window:.0f}s  rem={remaining:.0f}s  "
            f"price<={self.cfg.price}",
            "trade",
        )
        self.cycle_state = CycleState.ACTIVE

        # Register all coins that share this market start
        for c in COINS:
            m = self._coin_markets.get(c)
            if m and m.start_timestamp() == market_start:
                self._coins_entered.add(c)

        # Start the watcher (no orders placed yet -- watcher handles everything)
        if self._fill_watcher_task and not self._fill_watcher_task.done():
            self._fill_watcher_task.cancel()
        self._fill_watcher_task = asyncio.get_running_loop().create_task(
            self._watch_and_trade()
        )

    def _transition_to_done(self) -> None:
        """Transition to DONE and schedule resolution."""
        self.cycle_state = CycleState.DONE

        # Stop watcher
        if self._fill_watcher_task and not self._fill_watcher_task.done():
            self._fill_watcher_task.cancel()

        # Mark remaining orders as cancelled locally
        for t in self._orders.values():
            if not t.filled and not t.cancelled:
                t.cancelled = True

        # Schedule resolution for ALL unique slugs in current positions
        seen_slugs: Set[str] = set()
        for pos in self._current_positions:
            if pos.market_slug and pos.market_slug not in seen_slugs:
                seen_slugs.add(pos.market_slug)
                self._schedule_resolution_all(pos.market_slug)

    # ------------------------------------------------------------------
    # Core: watch orderbooks and execute correlation pairs
    # ------------------------------------------------------------------
    async def _watch_and_trade(self) -> None:
        """Main trading loop for the correlation strategy.

        Monitors BTC asks. When BTC UP or DOWN <= threshold, starts
        scanning followers for the OPPOSITE side. Executes pairs.
        """
        while self.cycle_state == CycleState.ACTIVE:
            now = time.time()

            # Window expired check
            if now >= self._cycle_deadline:
                await asyncio.sleep(0.1)
                continue

            # --- BTC trigger detection ---
            btc_up_ask = self._best_asks.get("BTC", {}).get("up", 1.0)
            btc_down_ask = self._best_asks.get("BTC", {}).get("down", 1.0)

            if self._btc_bought:
                # BTC already bought -- only scan for more followers
                if not self._btc_locked_side:
                    await asyncio.sleep(0.5)
                    continue
                follower_side = "down" if self._btc_locked_side == "up" else "up"
                self._scanning = True
                self._current_trigger_side = self._btc_locked_side

                for follower_coin in FOLLOWER_COINS:
                    if follower_coin in self._followers_bought:
                        continue
                    if follower_coin not in self._coins_entered:
                        continue
                    follower_ask = self._best_asks.get(follower_coin, {}).get(
                        follower_side, 1.0
                    )
                    if follower_ask <= self.cfg.price:
                        # Execute follower only (BTC already bought)
                        await self._execute_follower(
                            follower_coin, follower_side, follower_ask
                        )
            else:
                # BTC not yet bought -- detect trigger
                btc_trigger_side: Optional[str] = None
                btc_trigger_ask: float = 1.0

                if btc_up_ask <= self.cfg.price:
                    btc_trigger_side = "up"
                    btc_trigger_ask = btc_up_ask
                elif btc_down_ask <= self.cfg.price:
                    btc_trigger_side = "down"
                    btc_trigger_ask = btc_down_ask

                if btc_trigger_side is not None:
                    self._scanning = True
                    self._current_trigger_side = btc_trigger_side
                    follower_side = (
                        "down" if btc_trigger_side == "up" else "up"
                    )

                    # Scan followers for opposite side
                    for follower_coin in FOLLOWER_COINS:
                        if follower_coin in self._followers_bought:
                            continue
                        if follower_coin not in self._coins_entered:
                            continue
                        follower_ask = self._best_asks.get(
                            follower_coin, {}
                        ).get(follower_side, 1.0)
                        if follower_ask <= self.cfg.price:
                            # Execute pair: BTC + follower simultaneously
                            await self._execute_pair(
                                btc_trigger_side,
                                btc_trigger_ask,
                                follower_coin,
                                follower_side,
                                follower_ask,
                            )
                            # After first pair, BTC is locked
                            break  # Re-enter loop to check for more followers
                else:
                    # BTC not triggered -- stop scanning
                    self._scanning = False
                    self._current_trigger_side = None

            await asyncio.sleep(0.5)

    # ------------------------------------------------------------------
    # Pair execution
    # ------------------------------------------------------------------
    async def _execute_pair(
        self,
        btc_side: str,
        btc_ask: float,
        follower_coin: str,
        follower_side: str,
        follower_ask: float,
    ) -> None:
        """Execute a BTC + follower pair simultaneously.

        In dry-run: instant sim fill at limit price.
        In live: submit GTC orders via asyncio.gather.
        """
        log(
            f"PAIR: BTC-{btc_side.upper()} @ {btc_ask:.4f}"
            f" + {follower_coin}-{follower_side.upper()} @ {follower_ask:.4f}",
            "trade",
        )

        if self.cfg.dry_run:
            btc_tracker = self._record_sim_fill("BTC", btc_side)
            follower_tracker = self._record_sim_fill(
                follower_coin, follower_side
            )
            if btc_tracker:
                self._btc_bought = True
                self._btc_locked_side = btc_side
                log(
                    f"[SIM] FILLED BTC-{btc_side.upper()} @ {self.cfg.price:.4f}",
                    "success",
                )
            if follower_tracker:
                self._followers_bought.add(follower_coin)
                log(
                    f"[SIM] FILLED {follower_coin}-{follower_side.upper()}"
                    f" @ {self.cfg.price:.4f}",
                    "success",
                )
        else:
            # Live: submit both orders simultaneously
            btc_market = self._coin_markets.get("BTC")
            follower_market = self._coin_markets.get(follower_coin)
            if not btc_market or not follower_market:
                log("PAIR ABORT: missing market info", "error")
                return

            btc_token = btc_market.token_ids.get(btc_side, "")
            follower_token = follower_market.token_ids.get(follower_side, "")
            if not btc_token or not follower_token:
                log("PAIR ABORT: missing token IDs", "error")
                return

            results = await asyncio.gather(
                self._submit_live_order("BTC", btc_side, btc_token, btc_market),
                self._submit_live_order(
                    follower_coin, follower_side, follower_token, follower_market
                ),
                return_exceptions=True,
            )

            btc_result = results[0]
            follower_result = results[1]

            if isinstance(btc_result, Exception):
                log(f"PAIR BTC order error: {btc_result}", "error")
                btc_result = None
            if isinstance(follower_result, Exception):
                log(
                    f"PAIR {follower_coin} order error: {follower_result}",
                    "error",
                )
                follower_result = None

            if btc_result:
                self._btc_bought = True
                self._btc_locked_side = btc_side
            if follower_result:
                self._followers_bought.add(follower_coin)

    async def _execute_follower(
        self,
        follower_coin: str,
        follower_side: str,
        follower_ask: float,
    ) -> None:
        """Execute a follower order only (BTC already bought)."""
        log(
            f"FOLLOWER: {follower_coin}-{follower_side.upper()}"
            f" @ {follower_ask:.4f}"
            f" (BTC-{self._btc_locked_side.upper() if self._btc_locked_side else '?'}"
            f" already bought)",
            "trade",
        )

        if self.cfg.dry_run:
            tracker = self._record_sim_fill(follower_coin, follower_side)
            if tracker:
                self._followers_bought.add(follower_coin)
                log(
                    f"[SIM] FILLED {follower_coin}-{follower_side.upper()}"
                    f" @ {self.cfg.price:.4f}",
                    "success",
                )
        else:
            follower_market = self._coin_markets.get(follower_coin)
            if not follower_market:
                log(f"FOLLOWER ABORT: no market for {follower_coin}", "error")
                return
            follower_token = follower_market.token_ids.get(follower_side, "")
            if not follower_token:
                log(
                    f"FOLLOWER ABORT: no token for"
                    f" {follower_coin}-{follower_side}",
                    "error",
                )
                return

            result = await self._submit_live_order(
                follower_coin, follower_side, follower_token, follower_market
            )
            if result:
                self._followers_bought.add(follower_coin)

    # ------------------------------------------------------------------
    # Sim fill
    # ------------------------------------------------------------------
    def _record_sim_fill(
        self, coin: str, side: str
    ) -> Optional[OrderTracker]:
        """Create an OrderTracker, mark it filled, and record the fill."""
        market = self._coin_markets.get(coin)
        if not market:
            return None

        token_id = market.token_ids.get(side, "")
        order_id = f"corr-{coin}-{side}-{int(time.time() * 1000)}"

        tracker = OrderTracker(
            coin=coin,
            side=side,
            token_id=token_id,
            order_id=order_id,
            price=self.cfg.price,
            size=self.cfg.size,
            placed_at=time.time(),
            market_slug=market.slug,
            filled=True,
            fill_price=self.cfg.price,  # sim fill at limit price
            fill_size=self.cfg.size,
            fill_time=time.time(),
        )
        self._orders[order_id] = tracker
        self._orders_placed_this_cycle += 1
        self.total_orders_placed += 1
        self._record_fill(tracker)
        return tracker

    # ------------------------------------------------------------------
    # Live order submission
    # ------------------------------------------------------------------
    async def _submit_live_order(
        self,
        coin: str,
        side: str,
        token_id: str,
        market: MarketInfo,
    ) -> Optional[OrderTracker]:
        """Submit a real GTC limit BUY to the CLOB.

        Returns the filled OrderTracker on immediate fill.  If the order
        rests (ask moved away from threshold between check and submit),
        it is cancelled immediately and None is returned.  The watcher
        loop will retry on the next iteration if conditions still hold.
        """
        label = f"{coin}-{side.upper()}"
        try:
            fee_rate_bps = self._fee_rate_cache.get(token_id)
            if fee_rate_bps is None:
                fee_rate_bps = await asyncio.to_thread(
                    self.clob.get_fee_rate_bps, token_id
                )
                self._fee_rate_cache[token_id] = fee_rate_bps

            order = Order(
                token_id=token_id,
                price=self.cfg.price,
                size=self.cfg.size,
                side="BUY",
                funder=self.bot_config.safe_address,
                fee_rate_bps=fee_rate_bps,
                signature_type=self.bot_config.clob.signature_type,
                neg_risk=market.neg_risk,
                tick_size=market.tick_size,
            )
            signed = self.signer.sign_order(order)
            response = await asyncio.to_thread(self.clob.post_order, signed, "GTC")

            if not response.get("success", False):
                error = response.get("errorMsg", "unknown")
                log(f"SUBMIT FAIL {label}: {error}", "error")
                return None

            order_id = (
                response.get("orderID")
                or response.get("orderId")
                or response.get("order_id")
                or ""
            )

            # Check if it filled immediately
            status = str(response.get("status", "")).lower()
            tracker = OrderTracker(
                coin=coin,
                side=side,
                token_id=token_id,
                order_id=order_id,
                price=self.cfg.price,
                size=self.cfg.size,
                placed_at=time.time(),
                market_slug=market.slug,
            )

            if status in {"matched", "filled", "executed", "complete", "completed"}:
                taking = _to_float(response.get("takingAmount", 0))
                making = _to_float(response.get("makingAmount", 0))
                fp = making / max(taking, 1e-12) if taking > 0 else self.cfg.price
                if fp > self.cfg.price:
                    fp = self.cfg.price
                tracker.filled = True
                tracker.fill_price = fp
                tracker.fill_size = min(
                    taking if taking > 0 else self.cfg.size,
                    self.cfg.size,
                )
                tracker.fill_time = time.time()
                self._orders[order_id] = tracker
                self._orders_placed_this_cycle += 1
                self.total_orders_placed += 1
                self._record_fill(tracker)
                log(f"FILLED {label}: {tracker.fill_size:.1f} @ {fp:.4f}", "success")
                return tracker

            # --- Order is resting (ask moved away from threshold) ---
            # Cancel immediately -- the correlation window is tight and
            # we don't want resting orders accumulating on the book.
            self._orders[order_id] = tracker
            self._orders_placed_this_cycle += 1
            self.total_orders_placed += 1
            log(f"RESTING {label}: cancelling (ask moved)", "warning")
            try:
                await asyncio.to_thread(self.clob.cancel_order, order_id)
                tracker.cancelled = True
            except Exception:
                pass  # Will be cleaned up by _cancel_unfilled_orders at window expiry
            return None
        except Exception as exc:
            log(f"SUBMIT ERR {label}: {exc}", "error")
            return None

    # ------------------------------------------------------------------
    # Position tracking
    # ------------------------------------------------------------------
    def _record_fill(self, tracker: OrderTracker) -> None:
        self.total_fills += 1
        cost = tracker.fill_size * tracker.fill_price
        self.total_spent += cost
        # PnL and W/L only change on resolution, not on fill.

        pos = PositionRecord(
            coin=tracker.coin,
            side=tracker.side,
            fill_price=tracker.fill_price,
            fill_size=tracker.fill_size,
            fill_time=tracker.fill_time,
            market_slug=tracker.market_slug,
            order_id=tracker.order_id,
            cost=cost,
        )
        self._current_positions.append(pos)
        self._all_positions.append(pos)

        # Append to persistent trade log
        _append_trade_log(pos, self.cfg, outcome="PENDING", log_file=self.log_file)

    # ------------------------------------------------------------------
    # Cancellation (simplified -- no up_mode cancel-opposite)
    # ------------------------------------------------------------------
    async def _cancel_unfilled_orders(self) -> None:
        to_cancel = [
            (oid, t)
            for oid, t in self._orders.items()
            if not t.filled and not t.cancelled
        ]
        if not to_cancel:
            return

        count = 0
        for order_id, tracker in to_cancel:
            if self.cfg.dry_run:
                tracker.cancelled = True
                count += 1
                continue
            try:
                await asyncio.to_thread(self.clob.cancel_order, order_id)
                tracker.cancelled = True
                count += 1
            except Exception as exc:
                log(f"Cancel err {tracker.coin}-{tracker.side}: {exc}", "warning")

        if count > 0:
            log(f"Cancelled {count} unfilled order(s)", "warning")

    # ------------------------------------------------------------------
    # Resolution tracking
    # ------------------------------------------------------------------
    def _schedule_resolution(self, coin: str, old_slug: str) -> None:
        """Legacy per-coin entry point. Delegates to _schedule_resolution_all."""
        parts = old_slug.rsplit("-", 1)
        if len(parts) == 2:
            ts_suffix = parts[1]
            for c in COINS:
                slug = f"{c.lower()}-updown-5m-{ts_suffix}"
                self._schedule_resolution_all(slug)
        else:
            self._schedule_resolution_all(old_slug)

    def _schedule_resolution_all(self, slug: str) -> None:
        """Schedule resolution for a market slug (covers all coins/sides)."""
        if slug in self._scheduled_slugs:
            return
        self._scheduled_slugs.add(slug)
        if len(self._scheduled_slugs) > 500:
            self._scheduled_slugs.clear()

        task = asyncio.get_running_loop().create_task(
            self._check_resolution_for_slug(slug)
        )
        self._resolution_tasks.append(task)

    async def _check_resolution_for_slug(self, old_slug: str) -> None:
        """Check if a finished market has resolved and record win/loss."""
        positions = [
            p
            for p in self._all_positions
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
                        f"Resolved: {old_slug} -> {winner.upper()}"
                        f" (closed, attempt {attempt + 1})",
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
        """Apply win/loss outcome to a list of positions."""
        for pos in positions:
            if pos.resolved:
                continue
            pos.resolved = True
            self.total_resolved += 1
            coin_key = pos.coin.upper()
            if coin_key in self.coin_resolved:
                self.coin_resolved[coin_key] += 1
            if pos.side == winner:
                pos.won = True
                pos.payout = pos.fill_size * 1.0
                profit = pos.payout - pos.cost
                self.total_wins += 1
                if coin_key in self.coin_wins:
                    self.coin_wins[coin_key] += 1
                self.session_pnl += profit
                outcome_str = f"WIN +${profit:.4f}"
                log(
                    f"WIN  {pos.coin}-{pos.side.upper()} "
                    f"@{pos.fill_price:.2f} -> +${profit:.4f}",
                    "success",
                )
            else:
                pos.won = False
                pos.payout = 0.0
                self.total_losses += 1
                if coin_key in self.coin_losses:
                    self.coin_losses[coin_key] += 1
                self.session_pnl -= pos.cost
                outcome_str = f"LOSS -${pos.cost:.4f}"
                log(
                    f"LOSS {pos.coin}-{pos.side.upper()} "
                    f"@{pos.fill_price:.2f} -> -${pos.cost:.4f}",
                    "error",
                )

            # Update persistent trade log
            _update_trade_log_outcome(
                pos.order_id, pos.market_slug, pos.coin, pos.side, outcome_str,
                log_file=self.log_file,
            )

    # ------------------------------------------------------------------
    # Periodic sweep: resolve any PENDING positions
    # ------------------------------------------------------------------
    async def _sweep_pending(self) -> None:
        """Scan all unresolved positions and try to resolve via Gamma API.

        Two phases:
        1. In-memory: scan ``_all_positions`` for unresolved PositionRecords.
        2. Log-file:  scan ``self.log_file`` for ``outcome=PENDING`` lines
           that have *no* matching in-memory record (orphaned after restart).
        """
        # --- Phase 1: in-memory positions ---
        pending: Dict[str, List[PositionRecord]] = {}
        in_memory_keys: Set[str] = set()
        for pos in self._all_positions:
            if pos.order_id:
                in_memory_keys.add(pos.order_id)
            else:
                in_memory_keys.add(
                    f"{pos.market_slug}|{pos.coin}|{pos.side}"
                )
            if not pos.resolved:
                pending.setdefault(pos.market_slug, []).append(pos)

        # --- Phase 2: orphaned PENDING entries in log file ---
        orphaned_pending: Dict[str, List[Dict[str, str]]] = {}
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
                    orphaned_pending.setdefault(slug, []).append(fields)
        except Exception as exc:
            log(f"[sweep] log scan error: {exc}", "warning")

        if not pending and not orphaned_pending:
            return

        all_slugs = set(pending.keys()) | set(orphaned_pending.keys())

        gamma = GammaClient()
        for slug in all_slugs:
            try:
                market_data = await asyncio.to_thread(
                    gamma.get_market_by_slug, slug
                )
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

                # Resolve in-memory positions (Phase 1)
                if slug in pending:
                    log(f"[sweep] Resolved: {slug} -> {winner.upper()}", "info")
                    self._apply_resolution(pending[slug], winner)

                # Resolve orphaned log-file entries (Phase 2)
                if slug in orphaned_pending:
                    for entry in orphaned_pending[slug]:
                        oid = entry.get("order_id", "")
                        coin = entry.get("coin", "?")
                        side = entry.get("side", "?").lower()
                        cost_str = entry.get("cost", "0").lstrip("$")
                        cost = _to_float(cost_str)
                        size_str = entry.get("size", "0")
                        fill_size = _to_float(size_str)

                        is_win = (side == winner)

                        coin_key = coin.upper()
                        if is_win:
                            payout = fill_size * 1.0
                            profit = payout - cost
                            outcome_str = f"WIN +${profit:.4f}"
                            self.total_wins += 1
                            if coin_key in self.coin_wins:
                                self.coin_wins[coin_key] += 1
                            self.session_pnl += profit
                            log(
                                f"[sweep-orphan] WIN  {coin}-{side.upper()} "
                                f"@${cost/fill_size if fill_size else 0:.2f} "
                                f"-> +${profit:.4f}",
                                "success",
                            )
                        else:
                            outcome_str = f"LOSS -${cost:.4f}"
                            self.total_losses += 1
                            if coin_key in self.coin_losses:
                                self.coin_losses[coin_key] += 1
                            self.session_pnl -= cost
                            log(
                                f"[sweep-orphan] LOSS {coin}-{side.upper()} "
                                f"@${cost/fill_size if fill_size else 0:.2f} "
                                f"-> -${cost:.4f}",
                                "error",
                            )

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

        # Check if all active markets ended
        if self.cycle_state in (CycleState.ACTIVE, CycleState.HOLDING):
            all_ended = True
            for coin in COINS:
                m = self._coin_markets.get(coin)
                if m and not m.has_ended():
                    all_ended = False
                    break
            if all_ended and any(self._coin_markets.get(c) for c in COINS):
                if self.cycle_state != CycleState.DONE:
                    log("All markets ended. Cycle complete.", "info")
                    self._transition_to_done()

        # Window expiry
        if (
            self.cycle_state == CycleState.ACTIVE
            and self._cycle_deadline > 0
            and now >= self._cycle_deadline
        ):
            log(
                f"Window expired ({self.cfg.window:.0f}s). Moving to HOLD.",
                "warning",
            )
            await self._cancel_unfilled_orders()
            self.cycle_state = CycleState.HOLDING
            fills = len(self._current_positions)
            log(f"HOLDING {fills} position(s) to expiry.", "trade")

            if self._fill_watcher_task and not self._fill_watcher_task.done():
                self._fill_watcher_task.cancel()

        # --- Belt-and-suspenders: actively poll for new markets when DONE ---
        if self.cycle_state == CycleState.DONE:
            if now - self._last_done_poll >= 3.0:
                self._last_done_poll = now
                new_market_coin = None
                for coin in COINS:
                    mgr = self.managers.get(coin)
                    if not mgr or not mgr.current_market:
                        continue
                    market = mgr.current_market
                    ms = market.start_timestamp()
                    if ms is not None:
                        self._coin_markets[coin] = market
                        if ms != self._cycle_ts and new_market_coin is None:
                            new_market_coin = coin
                if new_market_coin:
                    m = self._coin_markets.get(new_market_coin)
                    if m:
                        log(f"[poll] New market detected via {new_market_coin}", "info")
                        self._maybe_enter_cycle(new_market_coin, m)

        # --- Periodic sweep: resolve PENDING positions (every 2 min) ---
        if now - self._last_sweep_ts >= 120.0:
            self._last_sweep_ts = now
            if self._sweep_task is None or self._sweep_task.done():
                self._sweep_task = asyncio.get_running_loop().create_task(
                    self._sweep_pending()
                )

        # --- Periodic cleanup of completed resolution tasks ---
        if now - self._last_task_cleanup >= 30.0:
            self._last_task_cleanup = now
            self._resolution_tasks = [
                t for t in self._resolution_tasks if not t.done()
            ]

        # --- Periodic heartbeat + WS health check (every 5 min) ---
        if now - self._last_heartbeat_ts >= 300.0:
            self._last_heartbeat_ts = now
            uptime_h = (now - self._session_start) / 3600
            connected = sum(1 for m in self.managers.values() if m.is_connected)
            pending_res = len(self._resolution_tasks)
            log(
                f"[heartbeat] up={uptime_h:.1f}h  WS={connected}/4  "
                f"cycles={self.cycles_seen}  fills={self.total_fills}  "
                f"pnl=${self.session_pnl:+.2f}  res_tasks={pending_res}",
                "info",
            )
            if len(self._all_positions) > 2000:
                trimmed: List[PositionRecord] = []
                to_drop = len(self._all_positions) - 2000
                dropped = 0
                for p in self._all_positions:
                    if dropped < to_drop and p.resolved:
                        dropped += 1
                        continue
                    trimmed.append(p)
                self._all_positions = trimmed

        # TUI
        render_interval = 0.5 if _tui_active else 2.0
        if now - self._last_render_ts >= render_interval:
            elapsed = max(now - self._status_window_start, 1e-6)
            tick_rate = self._ticks_window / elapsed
            since_last = now - self._last_tick_ts if self._last_tick_ts else 0.0

            if _tui_active:
                self._render_tui(tick_rate, since_last)

            self._last_render_ts = now
            self._ticks_window = 0
            self._status_window_start = now

    # ------------------------------------------------------------------
    # TUI
    # ------------------------------------------------------------------
    def _render_tui(self, tick_rate: float, since_last: float) -> None:
        G = Colors.GREEN
        R = Colors.RED
        Y = Colors.YELLOW
        C = Colors.CYAN
        B = Colors.BOLD
        D = Colors.DIM
        X = Colors.RESET
        M = Colors.MAGENTA if hasattr(Colors, "MAGENTA") else "\033[95m"
        W = 68

        lines: list[str] = []

        def sep() -> None:
            lines.append(f" {D}{'.' * W}{X}")

        def hsep() -> None:
            lines.append(f" {C}{'_' * W}{X}")

        # --- Header bar ---
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
            CycleState.ACTIVE: (Y, "ACTIVE"),
            CycleState.HOLDING: (C, "HOLD"),
            CycleState.WAITING_MARKET: (D, "WAIT"),
            CycleState.DONE: (D, "IDLE"),
        }
        sc, st = state_map.get(self.cycle_state, (D, "?"))
        if self.cycle_state == CycleState.ACTIVE:
            rem = max(0.0, self._cycle_deadline - time.time())
            st = f"SCAN {rem:.0f}s"

        up_s = time.time() - self._session_start
        up_h, up_m = int(up_s // 3600), int((up_s % 3600) // 60)
        up_str = f"{up_h}h{up_m:02d}m" if up_h else f"{up_m}m"
        if self.cfg.dry_run and self.cfg.name:
            dry = f" {Y}[SIM: {self.cfg.name}]{X}"
        elif self.cfg.dry_run:
            dry = f" {R}[DRY]{X}"
        else:
            dry = ""

        lines.append("")
        lines.append(
            f"  {M}{B}CORRELATION HUNTER{X}{dry}"
            f"      {ws_c}ws:{connected}/4{X}"
            f"   {countdown}"
            f"   {sc}{B}{st}{X}"
            f"   {D}{up_str}{X}"
        )
        lines.append(
            f"  {D}threshold {self.cfg.price}  window {self.cfg.window:.0f}s"
            f"  size {self.cfg.size:.0f}  cycle #{self.cycles_seen}{X}"
        )
        hsep()

        # --- Correlation status ---
        if self._btc_bought and self._btc_locked_side:
            btc_tag = f"{G}{B}LOCKED {self._btc_locked_side.upper()}{X}"
            follower_side = "DOWN" if self._btc_locked_side == "up" else "UP"
            scan_tag = f"{Y}scanning {follower_side}{X}"
        elif self._scanning and self._current_trigger_side:
            btc_tag = f"{Y}{B}TRIGGER {self._current_trigger_side.upper()}{X}"
            follower_side = "DOWN" if self._current_trigger_side == "up" else "UP"
            scan_tag = f"{Y}scanning {follower_side}{X}"
        else:
            btc_tag = f"{D}watching{X}"
            scan_tag = f"{D}--{X}"

        bought_list = ", ".join(sorted(self._followers_bought)) if self._followers_bought else "--"
        lines.append(
            f"  BTC: {btc_tag}    followers: {scan_tag}    bought: {B}{bought_list}{X}"
        )
        hsep()

        # --- 8 prices ---
        lines.append(
            f"  {D}{'':>5}    {'UP':>8}    {'DOWN':>8}"
            f"      {'UP':>8}    {'DOWN':>8}{X}"
        )
        lines.append(
            f"  {D}{'':>5}    {'ask':>8}    {'ask':>8}"
            f"      {'order':>8}    {'order':>8}{X}"
        )

        for coin in COINS:
            ua = self._best_asks[coin]["up"]
            da = self._best_asks[coin]["down"]

            # Highlight logic for correlation:
            # BTC: highlight the side that's <= threshold (trigger side)
            # Followers: highlight the OPPOSITE side of BTC trigger
            if coin == "BTC":
                uc = f"{G}{B}" if ua <= self.cfg.price else D
                dc = f"{G}{B}" if da <= self.cfg.price else D
            else:
                # Followers: highlight the scan side (opposite of BTC trigger)
                if self._current_trigger_side == "up":
                    # BTC UP triggered -> scan follower DOWN
                    uc = D
                    dc = f"{G}{B}" if da <= self.cfg.price else D
                elif self._current_trigger_side == "down":
                    # BTC DOWN triggered -> scan follower UP
                    uc = f"{G}{B}" if ua <= self.cfg.price else D
                    dc = D
                else:
                    uc = D
                    dc = D

            us = self._order_status_str(coin, "up")
            ds = self._order_status_str(coin, "down")
            lines.append(
                f"  {B}{coin:>5}{X}"
                f"    {uc}{ua:>8.4f}{X}    {dc}{da:>8.4f}{X}"
                f"      {us:>8}    {ds:>8}"
            )
        hsep()

        # --- Stats ---
        pnl_c = G if self.session_pnl >= 0 else R
        wr = (
            f"{(self.total_wins / self.total_resolved) * 100:.0f}%"
            if self.total_resolved > 0 else "--"
        )
        pairs_count = len(self._followers_bought)
        lines.append(
            f"  {B}{self.total_fills}{X} fills"
            f"   {B}{pairs_count}{X} pairs"
            f"   {G}{self.total_wins}W{X}/{R}{self.total_losses}L{X}"
            f"   win:{B}{wr}{X}"
            f"   pnl:{pnl_c}{B}${self.session_pnl:+.2f}{X}"
            f"   spent:{D}${self.total_spent:.2f}{X}"
        )

        # Per-coin W/L row
        coin_parts: list[str] = []
        for coin in COINS:
            cw = self.coin_wins[coin]
            cl = self.coin_losses[coin]
            cr = self.coin_resolved[coin]
            cwr = f"{(cw / cr) * 100:.0f}%" if cr > 0 else "--"
            coin_parts.append(
                f"{B}{coin}{X} {G}{cw}W{X}/{R}{cl}L{X}={cwr}"
            )
        lines.append(f"  {D}|{X} " + f"  {D}|{X} ".join(coin_parts))
        hsep()

        # --- Trade history (last 6 fills) ---
        lines.append(f"  {B}Trades{X}")
        recent = self._all_positions[-6:] if self._all_positions else []
        if recent:
            for p in reversed(recent):
                ts = datetime.fromtimestamp(p.fill_time).strftime("%H:%M")
                tag = f"{D}...{X}"
                if p.resolved:
                    if p.won:
                        profit = p.payout - p.cost
                        tag = f"{G}{B}WIN{X} {G}+${profit:.2f}{X}"
                    else:
                        tag = f"{R}LOSS{X} {R}-${p.cost:.2f}{X}"
                lines.append(
                    f"  {D}{ts}{X}"
                    f"  {B}{p.coin}{X}-{p.side.upper():<4}"
                    f"  {C}@{p.fill_price:.2f}{X} x{p.fill_size:.0f}"
                    f"  {tag}"
                )
        else:
            lines.append(f"  {D}waiting for pairs...{X}")
        hsep()

        # --- Events (last 6) ---
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

    def _order_status_str(self, coin: str, side: str) -> str:
        G = Colors.GREEN
        Y = Colors.YELLOW
        D = Colors.DIM
        X = Colors.RESET
        for t in self._orders.values():
            if t.coin == coin and t.side == side:
                if t.filled:
                    return f"{G}FILL@{t.fill_price:.2f}{X}"
                if t.cancelled:
                    return f"{D}canc{X}"
                return f"{Y}resting{X}"
        return f"{D}--{X}"

    # ------------------------------------------------------------------
    # Cleanup & summary
    # ------------------------------------------------------------------
    async def _cleanup(self) -> None:
        if self._fill_watcher_task and not self._fill_watcher_task.done():
            self._fill_watcher_task.cancel()
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
        print("  CORRELATION HUNTER - SESSION SUMMARY")
        print("=" * 60)
        print(
            f"  Config:        window={self.cfg.window:.0f}s"
            f"  price={self.cfg.price}  size={self.cfg.size}"
        )
        print(f"  Dry run:       {self.cfg.dry_run}")
        print(f"  Cycles seen:   {self.cycles_seen}")
        print(f"  Orders placed: {self.total_orders_placed}")
        print(f"  Total fills:   {self.total_fills}")
        print(
            f"  Resolved:      {self.total_resolved}"
            f"  ({self.total_wins}W / {self.total_losses}L)"
        )
        print(f"  Total spent:   ${self.total_spent:.4f}")
        print(f"  Session PnL:   ${self.session_pnl:+.4f}")

        if self.total_fills > 0:
            wr = (self.total_wins / self.total_resolved) * 100 if self.total_resolved > 0 else 0.0
            print(f"  Win rate:      {wr:.1f}%")
            # Correlation pairs: cost is 2x price, payout is $1 if correlated
            pair_cost = self.cfg.price * 2
            implied_corr = pair_cost * 100
            print(f"  Pair cost:     ${pair_cost:.2f} (implied break-even: {implied_corr:.0f}% correlation)")

            print()
            print("  Per-coin breakdown:")
            for coin in COINS:
                cw = self.coin_wins[coin]
                cl = self.coin_losses[coin]
                cr = self.coin_resolved[coin]
                cwr = f"{(cw / cr) * 100:.1f}%" if cr > 0 else "--"
                print(f"    {coin:>4}:  {cw}W / {cl}L  (win rate: {cwr})")

        if self._all_positions:
            print()
            print("  All fills:")
            for p in self._all_positions:
                res = ""
                if p.resolved:
                    res = f"  {'WIN' if p.won else 'LOSS'}"
                ts = datetime.fromtimestamp(p.fill_time).strftime("%H:%M:%S")
                print(
                    f"    {ts}  {p.coin}-{p.side.upper():>4}"
                    f"  @{p.fill_price:.4f}  x{p.fill_size:.0f}"
                    f"  cost=${p.cost:.4f}{res}"
                )

        print("=" * 60)
        print(f"  Trade log: {self.log_file}")
        print("=" * 60)


# ===================================================================
# Helpers
# ===================================================================
def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


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
        description=(
            "Correlation Hunter: bet on cross-market correlation across 4 coins"
        )
    )
    parser.add_argument(
        "--window", type=float, default=60.0,
        help="Seconds from market birth to allow trading (1-300, default: 60)",
    )
    parser.add_argument(
        "--price", type=float, default=0.20,
        help="Threshold price for trigger/scan (default: 0.20)",
    )
    parser.add_argument(
        "--size", type=float, default=5.0,
        help="Shares per order (min 5, default: 5)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate without placing real orders",
    )
    parser.add_argument(
        "--name", type=str, default="",
        help="Instance name (auto-generated from config if empty)",
    )
    parser.add_argument(
        "--market-check-interval", type=float, default=5.0,
        help="Seconds between market discovery checks (default: 5)",
    )
    args = parser.parse_args()

    # Auto-generate instance name from config when in dry-run
    name = args.name
    if not name and args.dry_run:
        name = f"w{int(args.window)}_p{args.price}_s{int(args.size)}"

    cfg = CorrelationConfig(
        window=args.window,
        price=args.price,
        size=args.size,
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

    strategy = CorrelationStrategy(cfg, bot_config, signer, clob)
    asyncio.run(strategy.run())


if __name__ == "__main__":
    main()
