"""
Cheap Quote Hunter - Multi-Coin 5-Minute Markets

Monitors all 4 coins (BTC, ETH, SOL, XRP) simultaneously on their 5-minute
Up/Down markets.  Places GTC limit BUY orders at a configurable price
threshold during a configurable window from market birth.  Holds filled
positions to expiry and automatically tracks win/loss outcomes.

Purpose:
    Frequentist inference -- does a 5-cent quote actually win ~5 % of
    the time, or more, or less?

Order approach:
    GTC limit BUY orders placed as soon as each market starts (up to 8
    orders per cycle: UP + DOWN for each coin).  Cancelled when the
    entry window expires.  This guarantees the configured price (limit
    orders never pay more) and eliminates latency risk (the exchange
    matches the order, not the bot).

Trade log:
    Every fill is appended to ``cheap_quote_trades.txt`` (never overwritten).
    Each line contains timestamp, market, coin, side, price, size, cost,
    config params, and outcome (when resolved).

Usage:
    python strategies/cheap_quote.py --dry-run
    python strategies/cheap_quote.py --window 60 --price 0.05 --size 5
    python strategies/cheap_quote.py --window 120 --price 0.20 --size 5
"""

import argparse
import asyncio
import enum
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
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

TRADE_LOG_FILE = Path(__file__).resolve().parent.parent / "cheap_quote_trades.txt"

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
    cfg: "CheapQuoteConfig",
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
class CheapQuoteConfig:
    """Configuration for the Cheap Quote strategy."""

    window: float = 60.0  # seconds from market birth
    price: float = 0.05  # max buy price (limit)
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
class CheapQuoteStrategy:
    """Buy cheap quotes across 4 coins, hold to expiry."""

    def __init__(
        self,
        cfg: CheapQuoteConfig,
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
                TRADE_LOG_FILE.parent / f"cheap_quote_sim_{cfg.name}.txt"
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

        # Orders
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

                self.total_fills += 1
                self.total_losses += 1  # assume loss
                self.total_spent += cost
                # Assume loss immediately (deduct cost)
                self.session_pnl -= cost

                if outcome.startswith("WIN"):
                    self.total_wins += 1
                    self.total_losses -= 1  # flip from assumed loss to win
                    self.total_resolved += 1
                    # WIN +$X.XX -> profit = payout - cost, so payout = profit + cost
                    profit_str = outcome.replace("WIN +$", "").replace("WIN +", "")
                    payout = _to_float(profit_str) + cost
                    self.session_pnl += payout
                elif outcome.startswith("LOSS"):
                    # Already counted as loss; just mark resolved
                    self.total_resolved += 1
        except Exception:
            pass  # If log is corrupted, start fresh

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def run(self) -> None:
        global _tui_active

        log("Cheap Quote Strategy started (4-coin WebSocket)", "success")
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
            # Invalidate stale orderbook data from old market so the
            # fill-watcher doesn't see e.g. 0.01 from the resolved side.
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

        # Same cycle -- add coin if needed
        if self._cycle_ts == market_start:
            if coin not in self._coins_entered and now < deadline:
                self._coins_entered.add(coin)
                asyncio.get_running_loop().create_task(
                    self._place_orders_for_coin(coin, market)
                )
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
        # Reset all orderbook caches — old market quotes are invalid for
        # the new cycle.  Fills won't trigger until fresh WS data arrives.
        self._best_asks = {c: {"up": 1.0, "down": 1.0} for c in COINS}

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

        for c in COINS:
            m = self._coin_markets.get(c)
            if m and m.start_timestamp() == market_start:
                self._coins_entered.add(c)
                asyncio.get_running_loop().create_task(
                    self._place_orders_for_coin(c, m)
                )

        # Fill watcher
        if self._fill_watcher_task and not self._fill_watcher_task.done():
            self._fill_watcher_task.cancel()
        self._fill_watcher_task = asyncio.get_running_loop().create_task(
            self._watch_fills()
        )

    def _transition_to_done(self) -> None:
        """Transition to DONE and schedule resolution.

        Order cancellation is handled by the ACTIVE->HOLDING transition
        in _tick (which uses async cancel). This method only marks
        remaining unfilled orders as cancelled locally and schedules
        resolution for any fills from this cycle.
        """
        self.cycle_state = CycleState.DONE

        # Stop fill watcher
        if self._fill_watcher_task and not self._fill_watcher_task.done():
            self._fill_watcher_task.cancel()

        # Mark remaining orders as cancelled locally (the async cancel
        # in _tick already sent cancel requests to CLOB if we came
        # through ACTIVE->HOLDING->DONE path; for direct ACTIVE->DONE
        # transitions we mark them here and they'll expire on the CLOB
        # when the market ends).
        for t in self._orders.values():
            if not t.filled and not t.cancelled:
                t.cancelled = True

        # Schedule resolution using the market_slug stored on the
        # positions themselves (immune to _coin_markets race).
        if self._current_positions:
            slug = self._current_positions[0].market_slug
            if slug:
                self._schedule_resolution_all(slug)

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------
    async def _place_orders_for_coin(self, coin: str, market: MarketInfo) -> None:
        for side in ("up", "down"):
            token_id = market.token_ids.get(side, "")
            if not token_id:
                log(f"{coin}-{side.upper()}: no token ID", "warning")
                continue
            await self._place_single_order(coin, side, token_id, market)

    async def _place_single_order(
        self, coin: str, side: str, token_id: str, market: MarketInfo
    ) -> None:
        label = f"{coin}-{side.upper()}"

        # Dry-run
        if self.cfg.dry_run:
            order_id = f"dry-{coin}-{side}-{int(time.time() * 1000)}"
            log(f"[DRY] BUY {label}: {self.cfg.size} @ {self.cfg.price}", "trade")
            tracker = OrderTracker(
                coin=coin, side=side, token_id=token_id,
                order_id=order_id, price=self.cfg.price,
                size=self.cfg.size, placed_at=time.time(),
                market_slug=market.slug,
            )
            self._orders[order_id] = tracker
            self._orders_placed_this_cycle += 1
            self.total_orders_placed += 1
            return

        # Live
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

            success = bool(response.get("success", False))
            if not success:
                error = response.get("errorMsg", "unknown")
                log(f"FAIL {label}: {error}", "error")
                return

            order_id = (
                response.get("orderID")
                or response.get("orderId")
                or response.get("order_id")
                or ""
            )
            status = str(response.get("status", "")).lower()

            tracker = OrderTracker(
                coin=coin, side=side, token_id=token_id,
                order_id=order_id, price=self.cfg.price,
                size=self.cfg.size, placed_at=time.time(),
                market_slug=market.slug,
            )

            if status in {"matched", "filled", "executed", "complete", "completed"}:
                taking = _to_float(response.get("takingAmount", 0))
                making = _to_float(response.get("makingAmount", 0))
                fp = making / max(taking, 1e-12) if taking > 0 else self.cfg.price
                # Sanity: limit BUY never fills above our limit price
                if fp > self.cfg.price:
                    fp = self.cfg.price
                tracker.filled = True
                tracker.fill_price = fp
                # Cap at our order size
                tracker.fill_size = min(
                    taking if taking > 0 else self.cfg.size,
                    self.cfg.size,
                )
                tracker.fill_time = time.time()
                self._record_fill(tracker)
                log(f"FILLED {label}: {tracker.fill_size:.1f} @ {fp:.4f}", "success")
            else:
                sid = order_id[:12] + "..." if len(order_id) > 12 else order_id
                log(f"PLACED {label}: {self.cfg.size} @ {self.cfg.price} id={sid}", "success")

            self._orders[order_id] = tracker
            self._orders_placed_this_cycle += 1
            self.total_orders_placed += 1

        except Exception as exc:
            log(f"ERROR {label}: {exc}", "error")

    # ------------------------------------------------------------------
    # Fill watcher
    # ------------------------------------------------------------------
    async def _watch_fills(self) -> None:
        while self.cycle_state == CycleState.ACTIVE:
            unfilled = [
                (oid, t)
                for oid, t in self._orders.items()
                if not t.filled and not t.cancelled
            ]
            if not unfilled:
                await asyncio.sleep(0.5)
                continue

            for order_id, tracker in unfilled:
                if self.cfg.dry_run:
                    ask = self._best_asks.get(tracker.coin, {}).get(tracker.side, 1.0)
                    if ask <= self.cfg.price:
                        tracker.filled = True
                        tracker.fill_price = self.cfg.price  # fill at limit price
                        tracker.fill_size = self.cfg.size
                        tracker.fill_time = time.time()
                        self._record_fill(tracker)
                        log(
                            f"[SIM] FILLED {tracker.coin}-{tracker.side.upper()} "
                            f"@ {self.cfg.price:.4f}",
                            "success",
                        )
                    continue

                try:
                    filled, closed, status, size_matched, avg_price = (
                        await asyncio.to_thread(
                            self._check_order_filled_sync,
                            order_id,
                            tracker.size,
                        )
                    )
                    if filled:
                        tracker.filled = True
                        # Cap fill_size at our order size (API may report
                        # aggregate market volume, not just our order).
                        tracker.fill_size = min(
                            size_matched if size_matched > 0 else tracker.size,
                            tracker.size,
                        )
                        # Limit BUY never fills above limit price.  If the API
                        # reports a higher price it's reading the wrong field.
                        if avg_price > 0 and avg_price <= tracker.price:
                            tracker.fill_price = avg_price
                        else:
                            tracker.fill_price = tracker.price
                        tracker.fill_time = time.time()
                        self._record_fill(tracker)
                        label = f"{tracker.coin}-{tracker.side.upper()}"
                        log(
                            f"FILLED {label}: {tracker.fill_size:.1f} "
                            f"@ {tracker.fill_price:.4f}",
                            "success",
                        )
                    elif closed:
                        tracker.cancelled = True
                except Exception as exc:
                    log(f"Fill check err {tracker.coin}-{tracker.side}: {exc}", "warning")

            await asyncio.sleep(0.5)

    def _check_order_filled_sync(
        self, order_id: str, expected_size: float
    ) -> Tuple[bool, bool, str, float, float]:
        try:
            payload = self.clob.get_order(order_id)
            if payload is None:
                return False, True, "missing", 0.0, 0.0

            order_data = (
                payload.get("order", payload) if isinstance(payload, dict) else {}
            )
            status = str(order_data.get("status", "")).lower()
            size_matched = _to_float(
                order_data.get("size_matched", order_data.get("sizeMatched", 0))
            )
            original_size = _to_float(
                order_data.get("original_size", order_data.get("originalSize", 0))
            )
            limit_price = _to_float(order_data.get("price", 0.0))
            avg_price = limit_price if limit_price > 0 else self.cfg.price

            associate_trades = order_data.get("associate_trades", [])
            if isinstance(associate_trades, list) and associate_trades:
                total_sz, total_cost = 0.0, 0.0
                for tid in associate_trades:
                    trade = self.clob.get_trade(str(tid))
                    if trade:
                        tsz = _to_float(trade.get("size", 0))
                        tpx = _to_float(trade.get("price", 0))
                        if tsz > 0:
                            total_sz += tsz
                            total_cost += tsz * tpx
                if total_sz > 0:
                    size_matched = max(size_matched, total_sz)
                    avg_price = total_cost / total_sz

            filled_by_status = status in {
                "matched", "filled", "executed", "complete", "completed",
            }
            filled_by_size = (
                original_size > 0 and size_matched >= original_size - 1e-9
            )
            filled_by_expected = size_matched >= expected_size - 1e-9
            filled = filled_by_status or filled_by_size or filled_by_expected

            closed = status in {
                "canceled", "cancelled", "expired", "failed", "rejected",
            }
            return filled, closed, status, size_matched, avg_price
        except Exception:
            return False, False, "error", 0.0, 0.0

    # ------------------------------------------------------------------
    # Position tracking
    # ------------------------------------------------------------------
    def _record_fill(self, tracker: OrderTracker) -> None:
        self.total_fills += 1
        self.total_losses += 1  # assume loss immediately
        cost = tracker.fill_size * tracker.fill_price
        self.total_spent += cost
        self.session_pnl -= cost  # assume loss immediately

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
    # Cancellation
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
                # Leave cancelled=False so fill watcher can still detect it

        if count > 0:
            log(f"Cancelled {count} unfilled order(s)", "warning")

    # ------------------------------------------------------------------
    # Resolution tracking
    # ------------------------------------------------------------------
    def _schedule_resolution(self, coin: str, old_slug: str) -> None:
        """Legacy per-coin entry point. Delegates to _schedule_resolution_all."""
        # Build full slug set: all 4 coins share the same timestamp suffix
        # e.g. btc-updown-5m-1772144400 -> extract 1772144400
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
        # Cap the set to prevent unbounded growth
        if len(self._scheduled_slugs) > 500:
            self._scheduled_slugs = set(list(self._scheduled_slugs)[-200:])

        task = asyncio.get_running_loop().create_task(
            self._check_resolution_for_slug(slug)
        )
        self._resolution_tasks.append(task)

    async def _check_resolution_for_slug(self, old_slug: str) -> None:
        """Check if a finished market has resolved and record win/loss.

        Resolves ALL positions for this slug (any coin, any side).

        Uses definitive Gamma API fields:
          - closed == True  -> market is settled
          - outcomePrices has "1" and "0" -> the outcome with "1" won

        Retries up to 10 times over ~5 min to wait for Polymarket to
        officially close the market and set final prices.
        """
        positions = [
            p
            for p in self._all_positions
            if p.market_slug == old_slug and not p.resolved
        ]
        if not positions:
            return

        # Retry schedule: total ~5 min (5m markets resolve within 1-2 min)
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

                # Only trust the result when Polymarket explicitly closes
                if not market_data.get("closed", False):
                    continue  # Not closed yet, keep waiting

                # Parse outcomePrices and outcomes from raw Gamma response
                raw_prices = market_data.get("outcomePrices", "[]")
                raw_outcomes = market_data.get("outcomes", "[]")
                prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes

                # Find the outcome with price == "1" (the winner)
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
            if pos.side == winner:
                pos.won = True
                pos.payout = pos.fill_size * 1.0
                profit = pos.payout - pos.cost
                self.total_wins += 1
                self.total_losses -= 1  # flip from assumed loss to win
                # Cost was already deducted on fill; add back full payout
                self.session_pnl += pos.payout
                outcome_str = f"WIN +${profit:.4f}"
                log(
                    f"WIN  {pos.coin}-{pos.side.upper()} "
                    f"@{pos.fill_price:.2f} -> +${profit:.4f}",
                    "success",
                )
            else:
                pos.won = False
                pos.payout = 0.0
                # Already counted as loss on fill; nothing to change
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

        Groups positions by market_slug, makes ONE API call per slug.
        If market is closed, resolves immediately.  If not, skips (retried
        next sweep).
        """
        # --- Phase 1: in-memory positions ---
        pending: Dict[str, List[PositionRecord]] = {}
        in_memory_order_ids: Set[str] = set()
        for pos in self._all_positions:
            if pos.order_id:
                in_memory_order_ids.add(pos.order_id)
            if not pos.resolved:
                pending.setdefault(pos.market_slug, []).append(pos)

        # --- Phase 2: orphaned PENDING entries in log file ---
        # These exist when the process restarted after fills were recorded
        # but before they were resolved.  _load_stats_from_log restores
        # aggregate stats (treating PENDING as a loss), but does NOT
        # recreate PositionRecord objects.  We must resolve them here.
        orphaned_pending: Dict[str, List[Dict[str, str]]] = {}
        try:
            if self.log_file.exists():
                for line in self.log_file.read_text(encoding="utf-8").splitlines():
                    if "outcome=PENDING" not in line:
                        continue
                    # Parse key=value fields
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
                    # Skip if already tracked in memory (Phase 1 handles it)
                    if oid and oid in in_memory_order_ids:
                        continue
                    orphaned_pending.setdefault(slug, []).append(fields)
        except Exception as exc:
            log(f"[sweep] log scan error: {exc}", "warning")

        if not pending and not orphaned_pending:
            return

        # Collect all slugs we need to query
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

                        if is_win:
                            payout = fill_size * 1.0
                            profit = payout - cost
                            outcome_str = f"WIN +${profit:.4f}"
                            self.total_wins += 1
                            self.total_losses -= 1
                            self.session_pnl += payout
                            log(
                                f"[sweep-orphan] WIN  {coin}-{side.upper()} "
                                f"@${cost/fill_size if fill_size else 0:.2f} "
                                f"-> +${profit:.4f}",
                                "success",
                            )
                        else:
                            outcome_str = f"LOSS -${cost:.4f}"
                            # Already counted as loss on restore; nothing to change
                            log(
                                f"[sweep-orphan] LOSS {coin}-{side.upper()} "
                                f"@${cost/fill_size if fill_size else 0:.2f} "
                                f"-> -${cost:.4f}",
                                "error",
                            )

                        self.total_resolved += 1
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
                f"Window expired ({self.cfg.window:.0f}s). Cancelling.",
                "warning",
            )
            await self._cancel_unfilled_orders()
            self.cycle_state = CycleState.HOLDING
            fills = len(self._current_positions)
            log(f"HOLDING {fills} position(s) to expiry.", "trade")

            if self._fill_watcher_task and not self._fill_watcher_task.done():
                self._fill_watcher_task.cancel()

        # --- Belt-and-suspenders: actively poll for new markets when DONE ---
        # The MarketManager callbacks should handle this, but if the callback
        # was missed (exception, race condition, etc.) we detect it here.
        if self.cycle_state == CycleState.DONE:
            if now - self._last_done_poll >= 3.0:
                self._last_done_poll = now
                new_market_coin = None
                # Update _coin_markets for ALL coins first
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
                # Then enter cycle if a new market was found
                if new_market_coin:
                    m = self._coin_markets.get(new_market_coin)
                    if m:
                        log(f"[poll] New market detected via {new_market_coin}", "info")
                        self._maybe_enter_cycle(new_market_coin, m)

        # --- Periodic sweep: resolve PENDING positions (every 2 min) ---
        if now - self._last_sweep_ts >= 120.0:
            self._last_sweep_ts = now
            asyncio.get_running_loop().create_task(self._sweep_pending())

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
            # Cap _all_positions to prevent unbounded memory growth:
            # keep only the last 2000 entries (enough for ~16 hours at max fill rate)
            if len(self._all_positions) > 2000:
                self._all_positions = self._all_positions[-2000:]

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
            st = f"BUY {rem:.0f}s"

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
            f"  {C}{B}CHEAP QUOTE HUNTER{X}{dry}"
            f"      {ws_c}ws:{connected}/4{X}"
            f"   {countdown}"
            f"   {sc}{B}{st}{X}"
            f"   {D}{up_str}{X}"
        )
        lines.append(
            f"  {D}limit {self.cfg.price}  window {self.cfg.window:.0f}s"
            f"  size {self.cfg.size:.0f}  cycle #{self.cycles_seen}{X}"
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
            # Highlight prices at or below our limit
            uc = f"{G}{B}" if ua <= self.cfg.price else D
            dc = f"{G}{B}" if da <= self.cfg.price else D
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
            f"{(self.total_wins / self.total_fills) * 100:.0f}%"
            if self.total_fills > 0 else "--"
        )
        lines.append(
            f"  {B}{self.total_fills}{X} fills"
            f"   {G}{self.total_wins}W{X}/{R}{self.total_losses}L{X}"
            f"   win:{B}{wr}{X}"
            f"   pnl:{pnl_c}{B}${self.session_pnl:+.2f}{X}"
            f"   spent:{D}${self.total_spent:.2f}{X}"
        )
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
            lines.append(f"  {D}waiting for fills...{X}")
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
        print("  CHEAP QUOTE HUNTER - SESSION SUMMARY")
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
            wr = (self.total_wins / self.total_fills) * 100
            implied = self.cfg.price * 100
            print(f"  Win rate:      {wr:.1f}%  (implied: {implied:.1f}%)")
            edge = wr - implied
            tag = "POSITIVE" if edge > 0 else "NEGATIVE" if edge < 0 else "NEUTRAL"
            print(f"  Edge:          {edge:+.1f}pp  ({tag})")

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
            "Cheap Quote Hunter: buy cheap quotes across 4 coins, hold to expiry"
        )
    )
    parser.add_argument(
        "--window", type=float, default=60.0,
        help="Seconds from market birth to allow trading (1-300, default: 60)",
    )
    parser.add_argument(
        "--price", type=float, default=0.05,
        help="Maximum buy price (default: 0.05)",
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

    cfg = CheapQuoteConfig(
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

    strategy = CheapQuoteStrategy(cfg, bot_config, signer, clob)
    asyncio.run(strategy.run())


if __name__ == "__main__":
    main()
