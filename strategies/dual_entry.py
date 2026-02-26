"""
Dual Entry Strategy - BTC 5-Minute Markets (WebSocket)

Scans the UP and DOWN order books for cheap entries using real-time CLOB
WebSocket updates (tick-by-tick). No REST polling is used for price decisions.

Strategy Logic:
    1. Wait for a new BTC 5m market to go live.
    2. LEG 1: Monitor best ask of UP and DOWN.
       If either ask <= entry_price within entry_window (counted from market birth,
       not from bot start), place a limit BUY.
    3. Wait until LEG 1 is filled.
    4. LEG 2: Monitor best ask of the opposite side.
       If ask <= second_price (= total_tickets - avg_fill_price_leg1), place limit BUY.
    5. Wait until LEG 2 is filled, then market is complete.
    6. When market rotates, repeat on the next 5m market.
"""

import argparse
import asyncio
import enum
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Set, Tuple, List

from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

logging.getLogger("src.websocket_client").setLevel(logging.WARNING)

from lib.market_manager import MarketInfo, MarketManager
from src.client import ClobClient
from src.config import Config
from src.merger import Merger
from src.signer import Order, OrderSigner
from src.websocket_client import OrderbookSnapshot
from lib.console import Colors, format_countdown, StatusDisplay


@dataclass
class DualEntryConfig:
    """Configuration for the dual entry strategy."""

    entry_price: float = 0.48
    total_tickets: float = 0.90
    entry_window: float = 30.0  # Seconds from market birth for LEG1
    size: float = 5.0
    coin: str = "BTC"
    dry_run: bool = False
    market_check_interval: float = 5.0

    @property
    def second_price(self) -> float:
        return round(self.total_tickets - self.entry_price, 4)

    def validate(self) -> None:
        if not 0.01 <= self.entry_price <= 0.99:
            raise ValueError(
                f"entry_price must be between 0.01 and 0.99, got {self.entry_price}"
            )
        if not 0.02 <= self.total_tickets < 1.00:
            raise ValueError(
                f"total_tickets must be between 0.02 and 0.99, got {self.total_tickets}"
            )
        if self.second_price <= 0:
            raise ValueError(
                f"total_tickets ({self.total_tickets}) must be greater than entry_price ({self.entry_price})"
            )
        if self.size < 5:
            raise ValueError(f"size must be >= 5 for 5m markets, got {self.size}")


class StrategyState(enum.Enum):
    """Dual-entry state machine."""

    WAITING_MARKET = "WAITING_MARKET"
    SCANNING_LEG1 = "SCANNING_LEG1"
    WAITING_LEG1_FILL = "WAITING_LEG1_FILL"
    SCANNING_LEG2 = "SCANNING_LEG2"
    WAITING_LEG2_FILL = "WAITING_LEG2_FILL"
    DONE = "DONE"


def ts_now() -> str:
    return datetime.now().strftime("%H:%M:%S")


_log_buffer_ref: list = []
_tui_active = False


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
        _log_buffer_ref.append(line)
        if len(_log_buffer_ref) > 20:
            _log_buffer_ref.pop(0)
    else:
        print(line)


class DualEntryStrategy:
    """WebSocket-based dual-entry strategy for 5-minute markets."""

    def __init__(
        self,
        config: DualEntryConfig,
        bot_config: Config,
        signer: OrderSigner,
        clob: ClobClient,
        merger: Optional[Merger] = None,
    ):
        self.cfg = config
        self.bot_config = bot_config
        self.signer = signer
        self.clob = clob
        self.merger = merger

        self.manager = MarketManager(
            coin=config.coin,
            market_check_interval=config.market_check_interval,
            auto_switch_market=True,
            interval="5m",
        )

        self.state = StrategyState.WAITING_MARKET
        self._current_market_slug: Optional[str] = None
        self._leg1_side: Optional[str] = None
        self._leg1_start_ts: float = 0.0
        self._leg1_deadline: float = 0.0

        self._best_asks: Dict[str, float] = {"up": 1.0, "down": 1.0}
        self._leg1_inflight = False
        self._leg2_inflight = False
        self._leg1_next_attempt_ts = 0.0
        self._leg2_next_attempt_ts = 0.0
        self._leg1_error_count = 0
        self._leg2_error_count = 0

        self._watch_leg1_task: Optional[asyncio.Task] = None
        self._watch_leg2_task: Optional[asyncio.Task] = None
        self._market_prepare_task: Optional[asyncio.Task] = None
        self._background_tasks: Set[asyncio.Task] = set()

        self._open_order_ids_by_market: Dict[str, Set[str]] = {}
        self._last_status_log_ts = 0.0
        self._fee_rate_cache: Dict[str, int] = {}
        self._trade_cache: Dict[str, Dict[str, Any]] = {}
        self._leg1_filled_size = 0.0
        self._leg1_cost = 0.0
        self._leg1_avg_price = 0.0
        self._leg2_filled_size = 0.0
        self._leg2_cost = 0.0
        self._leg2_target_size = 0.0
        self._leg2_target_price = 0.0
        self._leg1_token_id: Optional[str] = None
        self._leg2_token_id: Optional[str] = None
        self._current_market_fee_bps = 0
        self._hedge_announced = False
        self._last_hedged_size = 0.0
        self._last_pnl = 0.0
        self._ticks_total = 0
        self._ticks_market = 0
        self._ticks_in_status_window = 0
        self._last_tick_rx_ts = 0.0
        self._last_tick_ws_ts: Optional[int] = None

        self.markets_seen = 0
        self.leg1_entries = 0
        self.leg2_entries = 0
        self.total_spent = 0.0
        self._session_pnl = 0.0
        self._unhedged_loss = 0.0

        self._leg1_fill_ts: float = 0.0
        self._leg2_fill_ts: float = 0.0
        self._leg1_real_size: float = 0.0
        self._leg2_real_size: float = 0.0
        self._leg1_fill_datetime: str = ""
        self._leg2_fill_datetime: str = ""

        self._merge_completed = False
        self._merge_tx_hash: Optional[str] = None
        self.merges_done = 0

    async def run(self) -> None:
        """Main strategy loop."""
        global _tui_active
        log("Dual Entry Strategy started (WebSocket tick-by-tick)", "success")
        log(f"  coin:          {self.cfg.coin}")
        log(f"  entry_price:   {self.cfg.entry_price}")
        log(f"  total_tickets: {self.cfg.total_tickets}")
        log(f"  second_price:  {self.cfg.second_price}")
        log(f"  entry_window:  {self.cfg.entry_window}s")
        log(f"  size:          {self.cfg.size} shares")
        log(f"  dry_run:       {self.cfg.dry_run}")
        print()

        self._register_callbacks()

        try:
            await self._start_manager_with_retry()

            if self.manager.current_market:
                self._enter_market(self.manager.current_market)

            _tui_active = True
            while True:
                await self._timer_tick()
                await asyncio.sleep(0.1)

        except KeyboardInterrupt:
            _tui_active = False
            log("Strategy stopped by user", "warning")
        finally:
            _tui_active = False
            await self._shutdown_background_tasks()
            await self.manager.stop()
            self._print_summary()

    def _register_callbacks(self) -> None:
        @self.manager.on_book_update
        async def on_book(snapshot: OrderbookSnapshot):  # pyright: ignore[reportUnusedFunction]
            await self._on_book_update(snapshot)

        @self.manager.on_market_change
        def on_market_change(old_slug: str, new_slug: str):  # pyright: ignore[reportUnusedFunction]
            self._on_market_change(old_slug, new_slug)

        @self.manager.on_connect
        def on_connect():  # pyright: ignore[reportUnusedFunction]
            log("WebSocket connected", "success")

        @self.manager.on_disconnect
        def on_disconnect():  # pyright: ignore[reportUnusedFunction]
            log("WebSocket disconnected", "warning")

    async def _start_manager_with_retry(self) -> None:
        log("Discovering active 5m market...", "info")

        while True:
            started = await self.manager.start()
            if started:
                break
            log("No active 5m market found yet, retrying in 3s...", "warning")
            await asyncio.sleep(3)

        if self.manager.current_market:
            log(f"Connected to market: {self.manager.current_market.slug}", "success")

        if await self.manager.wait_for_data(timeout=10.0):
            log("Receiving live orderbook data", "success")
        else:
            log("No initial WS book data yet, continuing...", "warning")

    async def _timer_tick(self) -> None:
        market = self.manager.current_market
        if not market:
            self.state = StrategyState.WAITING_MARKET
            return

        if market.slug != self._current_market_slug:
            self._enter_market(market)

        if market.has_ended() and self.state not in (
            StrategyState.DONE,
            StrategyState.WAITING_MARKET,
        ):
            if self._leg2_filled_size > 0:
                self._announce_hedge_result()
            elif self._leg1_filled_size > 0:
                loss = self._leg1_cost
                self._unhedged_loss += loss
                self._session_pnl -= loss
                log(
                    f"Market ended UNHEDGED. LEG1={self._leg1_filled_size:.4f} @ {self._leg1_avg_price:.4f}, "
                    f"cost=${loss:.4f} LOST",
                    "error",
                )
            log("Market ended. Waiting for next 5m market...", "warning")
            self.state = StrategyState.DONE

        if (
            self.state == StrategyState.SCANNING_LEG1
            and time.time() >= self._leg1_deadline
        ):
            log(
                f"LEG 1 window expired ({self.cfg.entry_window:.0f}s). Skipping this market.",
                "warning",
            )
            self.state = StrategyState.DONE

        now = time.time()
        render_interval = 0.5 if _tui_active else 2.0
        elapsed = (
            now - self._last_status_log_ts
            if self._last_status_log_ts > 0
            else render_interval
        )
        if elapsed >= render_interval:
            ticks = self._ticks_in_status_window
            tick_rate = ticks / max(elapsed, 1e-6)
            since_last_tick = (
                now - self._last_tick_rx_ts if self._last_tick_rx_ts > 0 else -1.0
            )
            self._last_status_log_ts = now
            self._ticks_in_status_window = 0
            if _tui_active:
                self._render_tui(market, ticks, tick_rate, since_last_tick)
            else:
                self._log_status_line(market, ticks, tick_rate, since_last_tick)

    async def _on_book_update(self, snapshot: OrderbookSnapshot) -> None:
        market = self.manager.current_market
        if not market:
            return

        if snapshot.asset_id not in market.token_ids.values():
            return

        self._ticks_total += 1
        self._ticks_market += 1
        self._ticks_in_status_window += 1
        self._last_tick_rx_ts = time.time()
        self._last_tick_ws_ts = snapshot.timestamp

        # Refresh both sides from MarketManager cache (fed by WebSocket ticks).
        # This is more robust around market switches than updating only one side.
        self._best_asks["up"] = self.manager.get_best_ask("up")
        self._best_asks["down"] = self.manager.get_best_ask("down")

        if self.state == StrategyState.SCANNING_LEG1:
            await self._maybe_trigger_leg1()
        elif self.state == StrategyState.SCANNING_LEG2:
            await self._maybe_trigger_leg2()

    def _asset_side(self, asset_id: str) -> Optional[str]:
        market = self.manager.current_market
        if not market:
            return None

        for side, token_id in market.token_ids.items():
            if token_id == asset_id:
                return side
        return None

    async def _maybe_trigger_leg1(self) -> None:
        if self._leg1_inflight or self.state != StrategyState.SCANNING_LEG1:
            return

        now = time.time()
        if self._leg1_start_ts and now < self._leg1_start_ts:
            return
        if self._leg1_deadline and now >= self._leg1_deadline:
            return
        if now < self._leg1_next_attempt_ts:
            return

        up_ask = self._best_asks.get("up", 1.0)
        down_ask = self._best_asks.get("down", 1.0)

        candidates = []
        if up_ask <= self.cfg.entry_price:
            candidates.append(("up", up_ask))
        if down_ask <= self.cfg.entry_price:
            candidates.append(("down", down_ask))

        if not candidates:
            return

        side, ask = min(candidates, key=lambda item: item[1])
        log(
            f"LEG 1 trigger: {side.upper()} ask={ask:.4f} <= {self.cfg.entry_price}",
            "trade",
        )

        self._leg1_inflight = True
        self._spawn_task(self._execute_leg1(side, self._current_market_slug))

    async def _execute_leg1(self, side: str, market_slug: Optional[str]) -> None:
        try:
            market = self.manager.current_market
            if not market or market.slug != market_slug:
                return

            token_id = market.token_ids.get(side, "")
            if not token_id:
                log(f"LEG 1 missing token ID for {side}", "error")
                return

            self._leg1_side = side

            response = await self._submit_buy_order(
                token_id=token_id,
                price=self.cfg.entry_price,
                size=self.cfg.size,
                side_label=f"LEG1 {side.upper()}",
                neg_risk=market.neg_risk,
                tick_size=market.tick_size,
            )
            if not response:
                self._leg1_error_count += 1
                cooldown = min(8.0, 2.0 ** min(self._leg1_error_count, 3))
                self._leg1_next_attempt_ts = time.time() + cooldown

                if self._leg1_error_count >= 3:
                    log(
                        "LEG 1 disabled for this market after repeated order errors.",
                        "warning",
                    )
                    self.state = StrategyState.DONE
                else:
                    log(f"LEG 1 order failed. Retrying in {cooldown:.0f}s", "warning")
                return

            order_id = self._extract_order_id(response)
            filled_now = self._is_order_filled_response(response, self.cfg.size)
            immediate_size, immediate_price = self._extract_post_fill_metrics(
                response,
                fallback_price=self.cfg.entry_price,
            )

            if filled_now:
                self._leg1_error_count = 0
                matched_size = immediate_size if immediate_size > 0 else self.cfg.size
                matched_price = (
                    immediate_price if immediate_price > 0 else self.cfg.entry_price
                )
                self._activate_leg2_from_leg1(
                    matched_size=matched_size,
                    avg_price=matched_price,
                    leg1_token_id=token_id,
                    tick_size=market.tick_size,
                    source="immediate",
                )
                return

            if not order_id:
                log(
                    "LEG 1 order has no ID. Cannot track fill; skipping market.",
                    "error",
                )
                self.state = StrategyState.DONE
                return

            self._remember_open_order(market.slug, order_id)
            self.state = StrategyState.WAITING_LEG1_FILL
            log("LEG 1 resting. Waiting for fill before LEG 2...", "trade")

            self._watch_leg1_task = self._spawn_task(
                self._watch_leg1_fill(order_id, market.slug)
            )

        finally:
            self._leg1_inflight = False

    async def _watch_leg1_fill(self, order_id: str, market_slug: str) -> None:
        while True:
            if self.state != StrategyState.WAITING_LEG1_FILL:
                return

            market = self.manager.current_market
            if not market or market.slug != market_slug:
                return

            if market.has_ended():
                log("Market ended before LEG 1 fill.", "warning")
                self.state = StrategyState.DONE
                return

            filled, closed, status, size_matched, avg_price = await asyncio.to_thread(
                self._check_order_filled_sync,
                order_id,
                self.cfg.size,
            )

            if filled:
                self._forget_open_order(market_slug, order_id)
                matched_size = size_matched if size_matched > 0 else self.cfg.size
                matched_price = avg_price if avg_price > 0 else self.cfg.entry_price
                self._activate_leg2_from_leg1(
                    matched_size=matched_size,
                    avg_price=matched_price,
                    leg1_token_id=self.manager.current_market.token_ids.get(
                        self._leg1_side or "", ""
                    )
                    if self.manager.current_market
                    else None,
                    tick_size=market.tick_size,
                    source=f"fill-confirmed:{status}",
                )
                return

            if closed:
                self._forget_open_order(market_slug, order_id)
                if size_matched > 0:
                    matched_price = avg_price if avg_price > 0 else self.cfg.entry_price
                    self._activate_leg2_from_leg1(
                        matched_size=size_matched,
                        avg_price=matched_price,
                        leg1_token_id=self.manager.current_market.token_ids.get(
                            self._leg1_side or "", ""
                        )
                        if self.manager.current_market
                        else None,
                        tick_size=market.tick_size,
                        source=f"partial-closed:{status}",
                    )
                else:
                    log(f"LEG 1 closed without fill (status={status}).", "warning")
                    self.state = StrategyState.DONE
                return

            await asyncio.sleep(0.35)

    async def _maybe_trigger_leg2(self) -> None:
        if self._leg2_inflight or self.state != StrategyState.SCANNING_LEG2:
            return

        if time.time() < self._leg2_next_attempt_ts:
            return

        if self._leg1_side not in ("up", "down"):
            return

        if self._leg2_target_size <= 0:
            return

        if self._leg2_target_price <= 0:
            return

        opposite = "down" if self._leg1_side == "up" else "up"
        ask = self._best_asks.get(opposite, 1.0)

        if ask > self._leg2_target_price:
            return

        log(
            f"LEG 2 trigger: {opposite.upper()} ask={ask:.4f} <= {self._leg2_target_price:.4f} "
            f"for size {self._leg2_target_size:.4f}",
            "trade",
        )

        self._leg2_inflight = True
        self._spawn_task(self._execute_leg2(opposite, self._current_market_slug))

    async def _execute_leg2(self, side: str, market_slug: Optional[str]) -> None:
        try:
            market = self.manager.current_market
            if not market or market.slug != market_slug:
                return

            token_id = market.token_ids.get(side, "")
            if not token_id:
                log(f"LEG 2 missing token ID for {side}", "error")
                return

            expected_size = self._leg2_target_size
            if expected_size <= 0:
                return

            response = await self._submit_buy_order(
                token_id=token_id,
                price=self._leg2_target_price,
                size=expected_size,
                side_label=f"LEG2 {side.upper()}",
                neg_risk=market.neg_risk,
                tick_size=market.tick_size,
            )
            if not response:
                self._leg2_error_count += 1
                cooldown = min(8.0, 2.0 ** min(self._leg2_error_count, 3))
                self._leg2_next_attempt_ts = time.time() + cooldown

                if self._leg2_error_count >= 3:
                    log(
                        "LEG 2 disabled for this market after repeated order errors.",
                        "warning",
                    )
                    self.state = StrategyState.DONE
                else:
                    log(f"LEG 2 order failed. Retrying in {cooldown:.0f}s", "warning")
                return

            order_id = self._extract_order_id(response)
            filled_now = self._is_order_filled_response(response, expected_size)
            immediate_size, immediate_price = self._extract_post_fill_metrics(
                response,
                fallback_price=self._leg2_target_price,
            )

            if filled_now:
                self._leg2_error_count = 0
                matched_size = immediate_size if immediate_size > 0 else expected_size
                matched_price = (
                    immediate_price if immediate_price > 0 else self._leg2_target_price
                )
                self._record_leg2_fill(matched_size, matched_price, self._leg2_token_id)
                self._leg2_target_size = max(0.0, self._leg2_target_size - matched_size)

                if self._leg2_target_size <= 1e-9:
                    self._announce_hedge_result()
                    self.state = StrategyState.DONE
                else:
                    self.state = StrategyState.SCANNING_LEG2
                    log(
                        f"LEG 2 partial immediate fill: {matched_size:.4f}. "
                        f"Remaining {self._leg2_target_size:.4f}",
                        "warning",
                    )
                return

            if not order_id:
                log("LEG 2 order has no ID. Cannot track fill.", "warning")
                self._leg2_next_attempt_ts = time.time() + 2.0
                self.state = StrategyState.SCANNING_LEG2
                return

            self._remember_open_order(market.slug, order_id)
            self.state = StrategyState.WAITING_LEG2_FILL
            log("LEG 2 resting. Waiting fill confirmation...", "trade")

            self._watch_leg2_task = self._spawn_task(
                self._watch_leg2_fill(order_id, market.slug, expected_size)
            )

        finally:
            self._leg2_inflight = False

    async def _watch_leg2_fill(
        self, order_id: str, market_slug: str, expected_size: float
    ) -> None:
        while True:
            if self.state != StrategyState.WAITING_LEG2_FILL:
                return

            market = self.manager.current_market
            if not market or market.slug != market_slug:
                return

            if market.has_ended():
                log("Market ended before LEG 2 fill.", "warning")
                self.state = StrategyState.DONE
                return

            filled, closed, status, size_matched, avg_price = await asyncio.to_thread(
                self._check_order_filled_sync,
                order_id,
                expected_size,
            )

            if filled:
                self._forget_open_order(market_slug, order_id)
                matched_size = size_matched if size_matched > 0 else expected_size
                fill_price = avg_price if avg_price > 0 else self._leg2_target_price
                self._record_leg2_fill(matched_size, fill_price, self._leg2_token_id)

                self._leg2_target_size = max(0.0, self._leg2_target_size - matched_size)
                if self._leg2_target_size <= 1e-9:
                    log(f"LEG 2 fill confirmed ({status}).", "success")
                    self._announce_hedge_result()
                    self.state = StrategyState.DONE
                else:
                    log(
                        f"LEG 2 partial fill confirmed ({status}) size={matched_size:.4f}. "
                        f"Remaining {self._leg2_target_size:.4f}",
                        "warning",
                    )
                    self.state = StrategyState.SCANNING_LEG2
                return

            if closed:
                self._forget_open_order(market_slug, order_id)

                if size_matched > 0:
                    fill_price = avg_price if avg_price > 0 else self._leg2_target_price
                    self._record_leg2_fill(
                        size_matched, fill_price, self._leg2_token_id
                    )

                remaining = max(0.0, expected_size - max(0.0, size_matched))
                self._leg2_target_size = max(
                    0.0, self._leg2_target_size - max(0.0, size_matched)
                )

                if self._leg2_target_size <= 1e-9:
                    self._announce_hedge_result()
                    self.state = StrategyState.DONE
                elif market.has_ended():
                    log(
                        f"LEG 2 closed at market end (status={status}). "
                        f"Remaining unhedged size {self._leg2_target_size:.4f}",
                        "warning",
                    )
                    self.state = StrategyState.DONE
                else:
                    log(
                        f"LEG 2 closed ({status}). Retrying remaining size "
                        f"{remaining:.4f} (total remaining {self._leg2_target_size:.4f})",
                        "warning",
                    )
                    self._leg2_next_attempt_ts = time.time() + 1.5
                    self.state = StrategyState.SCANNING_LEG2
                return

            await asyncio.sleep(0.35)

    async def _submit_buy_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side_label: str,
        neg_risk: bool,
        tick_size: str,
    ) -> Optional[Dict[str, Any]]:
        if self.cfg.dry_run:
            log(
                f"[DRY RUN] BUY {side_label}: {size} shares @ {price}",
                "trade",
            )
            return {
                "success": True,
                "status": "matched",
                "takingAmount": str(size),
                "makingAmount": str(round(size * price, 8)),
                "orderID": f"dry-run-{int(time.time() * 1000)}",
            }

        try:
            fee_rate_bps = self._fee_rate_cache.get(token_id)
            if fee_rate_bps is None:
                fee_rate_bps = await asyncio.to_thread(
                    self.clob.get_fee_rate_bps, token_id
                )
                self._fee_rate_cache[token_id] = fee_rate_bps

            order = Order(
                token_id=token_id,
                price=price,
                size=size,
                side="BUY",
                funder=self.bot_config.safe_address,
                fee_rate_bps=fee_rate_bps,
                signature_type=self.bot_config.clob.signature_type,
                neg_risk=neg_risk,
                tick_size=tick_size,
            )

            signed = self.signer.sign_order(order)
            response = await asyncio.to_thread(self.clob.post_order, signed, "GTC")

            success = bool(response.get("success", False))
            if not success:
                error = response.get("errorMsg", "unknown")
                log(f"ORDER FAILED {side_label}: {error}", "error")
                return None

            order_id = self._extract_order_id(response) or "?"
            status = str(response.get("status", "")).lower() or "unknown"
            log(
                f"ORDER PLACED {side_label}: {size} @ {price} "
                f"fee_bps={fee_rate_bps} id={order_id[:16]}... status={status}",
                "success",
            )
            return response

        except Exception as exc:
            log(f"ORDER ERROR {side_label}: {exc}", "error")
            return None

    @staticmethod
    def _extract_order_id(response: Dict[str, Any]) -> Optional[str]:
        order_id = response.get("orderID") or response.get("orderId")
        if isinstance(order_id, str) and order_id:
            return order_id
        return None

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    def _is_order_filled_response(
        self, response: Dict[str, Any], expected_size: float
    ) -> bool:
        status = str(response.get("status", "")).lower()
        if status in {"matched", "filled", "executed", "complete", "completed"}:
            return True

        size_matched = self._to_float(
            response.get("size_matched", response.get("sizeMatched", 0))
        )
        return size_matched >= expected_size - 1e-9

    def _extract_post_fill_metrics(
        self, response: Dict[str, Any], fallback_price: float
    ) -> Tuple[float, float]:
        """Extract immediate matched size and average fill price from post_order response."""
        taking_amount = self._to_float(response.get("takingAmount", 0.0))
        making_amount = self._to_float(response.get("makingAmount", 0.0))

        if taking_amount > 0 and making_amount > 0:
            avg_price = making_amount / max(taking_amount, 1e-12)
            return taking_amount, avg_price

        size_matched = self._to_float(
            response.get("size_matched", response.get("sizeMatched", 0.0))
        )
        if size_matched > 0:
            return size_matched, fallback_price

        return 0.0, fallback_price

    def _get_trade_cached_sync(self, trade_id: str) -> Optional[Dict[str, Any]]:
        if trade_id in self._trade_cache:
            return self._trade_cache[trade_id]
        trade = self.clob.get_trade(trade_id)
        if trade:
            self._trade_cache[trade_id] = trade
        return trade

    def _estimate_avg_fill_from_trade_ids_sync(
        self, trade_ids: List[str], fallback_price: float
    ) -> Tuple[float, float]:
        total_size = 0.0
        total_cost = 0.0

        for trade_id in trade_ids:
            trade = self._get_trade_cached_sync(trade_id)
            if not trade:
                continue
            trade_size = self._to_float(trade.get("size", 0.0))
            trade_price = self._to_float(trade.get("price", fallback_price))
            if trade_size <= 0:
                continue
            total_size += trade_size
            total_cost += trade_size * trade_price

        if total_size <= 0:
            return 0.0, fallback_price

        return total_size, total_cost / total_size

    def _lookup_order_trades_fallback_sync(self, order_id: str) -> Tuple[float, float]:
        """
        Fallback lookup for closed/missing orders by scanning recent user trades.

        Returns:
            (matched_size, avg_price)
        """
        try:
            recent_trades = self.clob.get_trades(
                maker_address=self.bot_config.safe_address,
                after=int(time.time()) - 900,
                max_pages=3,
            )
        except Exception:
            return 0.0, 0.0

        total_size = 0.0
        total_cost = 0.0
        for trade in recent_trades:
            if not isinstance(trade, dict):
                continue
            if str(trade.get("taker_order_id", "")) != order_id:
                continue
            trade_size = self._to_float(trade.get("size", 0.0))
            trade_price = self._to_float(trade.get("price", 0.0))
            if trade_size <= 0:
                continue
            total_size += trade_size
            total_cost += trade_size * trade_price

        if total_size <= 0:
            return 0.0, 0.0

        return total_size, total_cost / total_size

    def _check_order_filled_sync(
        self, order_id: str, expected_size: float
    ) -> Tuple[bool, bool, str, float, float]:
        try:
            payload = self.clob.get_order(order_id)

            if payload is None:
                fallback_size, fallback_avg = self._lookup_order_trades_fallback_sync(
                    order_id
                )
                filled = fallback_size >= expected_size - 1e-9 and fallback_size > 0
                return filled, True, "missing", fallback_size, fallback_avg

            order_data = (
                payload.get("order", payload) if isinstance(payload, dict) else {}
            )

            status = str(order_data.get("status", "")).lower()
            size_matched = self._to_float(
                order_data.get("size_matched", order_data.get("sizeMatched", 0))
            )
            original_size = self._to_float(
                order_data.get("original_size", order_data.get("originalSize", 0))
            )
            limit_price = self._to_float(order_data.get("price", 0.0), 0.0)

            associate_trades = order_data.get("associate_trades", [])
            trade_ids: List[str] = []
            if isinstance(associate_trades, list):
                trade_ids = [str(tid) for tid in associate_trades if tid]

            trades_size, trades_avg_price = self._estimate_avg_fill_from_trade_ids_sync(
                trade_ids,
                fallback_price=limit_price if limit_price > 0 else 0.5,
            )

            if trades_size > 0:
                size_matched = max(size_matched, trades_size)
                avg_fill_price = trades_avg_price
            else:
                avg_fill_price = limit_price if limit_price > 0 else 0.5

            filled_by_status = status in {
                "matched",
                "filled",
                "executed",
                "complete",
                "completed",
            }
            filled_by_size = original_size > 0 and size_matched >= original_size - 1e-9
            filled_by_expected = size_matched >= expected_size - 1e-9

            filled = filled_by_status or filled_by_size or filled_by_expected
            closed = status in {
                "canceled",
                "cancelled",
                "expired",
                "failed",
                "rejected",
            }

            return filled, closed, status, size_matched, avg_fill_price
        except Exception:
            return False, False, "unknown", 0.0, 0.0

    def _round_down_to_tick(self, value: float, tick_size: str) -> float:
        tick = self._to_float(tick_size, 0.01)
        if tick <= 0:
            tick = 0.01
        steps = int(value / tick + 1e-9)
        return round(steps * tick, 6)

    def _record_leg1_fill(
        self, size: float, avg_price: float, token_id: Optional[str]
    ) -> None:
        if size <= 0:
            return

        was_unset = self._leg1_filled_size <= 0

        if self._leg1_filled_size > 0:
            self.total_spent -= self._leg1_cost

        self._leg1_filled_size = size
        self._leg1_cost = size * avg_price
        self._leg1_avg_price = avg_price
        self._leg1_token_id = token_id
        self.total_spent += self._leg1_cost

        fee_bps = self._fee_rate_cache.get(token_id or "", self._current_market_fee_bps)
        self._leg1_real_size = size * (1 - fee_bps / 10000)
        self._leg1_fill_ts = time.time()
        self._leg1_fill_datetime = datetime.now().strftime("%H:%M:%S")

        if was_unset:
            self.leg1_entries += 1

    def _record_leg2_fill(
        self, size: float, avg_price: float, token_id: Optional[str] = None
    ) -> None:
        if size <= 0:
            return

        self._leg2_filled_size += size
        self._leg2_cost += size * avg_price
        self.total_spent += size * avg_price

        fee_bps = self._fee_rate_cache.get(token_id or "", self._current_market_fee_bps)
        self._leg2_real_size = size * (1 - fee_bps / 10000)
        self._leg2_fill_ts = time.time()
        self._leg2_fill_datetime = datetime.now().strftime("%H:%M:%S")

    def _activate_leg2_from_leg1(
        self,
        matched_size: float,
        avg_price: float,
        leg1_token_id: Optional[str],
        tick_size: str,
        source: str,
    ) -> None:
        if matched_size <= 0:
            self.state = StrategyState.DONE
            return

        self._record_leg1_fill(matched_size, avg_price, leg1_token_id)

        raw_second_price = self.cfg.total_tickets - avg_price
        if raw_second_price <= 0:
            log(
                f"LEG 1 avg fill {avg_price:.4f} already exceeds total_tickets {self.cfg.total_tickets:.4f}. "
                "Skipping LEG 2 for this market.",
                "warning",
            )
            self.state = StrategyState.DONE
            return

        second_price = self._round_down_to_tick(raw_second_price, tick_size)
        second_price = max(self._to_float(tick_size, 0.01), min(0.99, second_price))

        self._leg2_target_price = second_price
        self._leg2_target_size = matched_size
        self._leg2_next_attempt_ts = 0.0

        opposite = "down" if self._leg1_side == "up" else "up"
        if self.manager.current_market:
            self._leg2_token_id = self.manager.current_market.token_ids.get(opposite)

        leg1_fee_bps = self._fee_rate_cache.get(
            self._leg1_token_id or "", self._current_market_fee_bps
        )
        leg2_fee_bps = self._fee_rate_cache.get(
            self._leg2_token_id or "", self._current_market_fee_bps
        )

        # Estimate PnL if leg2 fills at target price
        est_total_cost = matched_size * (avg_price + second_price)
        est_real_leg1 = matched_size * (1 - leg1_fee_bps / 10000)
        est_real_leg2 = matched_size * (1 - leg2_fee_bps / 10000)
        est_merge = min(est_real_leg1, est_real_leg2)
        est_pnl = est_merge - est_total_cost

        self.state = StrategyState.SCANNING_LEG2
        log(
            f"LEG 1 fill ({source}) avg={avg_price:.4f}, size={matched_size:.4f}. "
            f"LEG 2 target {opposite.upper()} ask <= {second_price:.4f}",
            "success",
        )
        log(
            f"Est PnL=${est_pnl:+.4f} (cost=${est_total_cost:.4f}, "
            f"merge={est_merge:.4f} shares, fee_bps L1={leg1_fee_bps} L2={leg2_fee_bps})",
            "info",
        )

    def _announce_hedge_result(self) -> None:
        if self._hedge_announced:
            return

        hedged_size = min(self._leg1_filled_size, self._leg2_filled_size)
        if hedged_size <= 0:
            log("No hedged size completed.", "warning")
            return

        # --- Correct PnL ---
        # total_cost = USDC actually spent on both legs
        total_cost = self._leg1_cost + self._leg2_cost

        # Real shares received after fees
        real_size_leg1 = (
            self._leg1_real_size if self._leg1_real_size > 0 else self._leg1_filled_size
        )
        real_size_leg2 = (
            self._leg2_real_size if self._leg2_real_size > 0 else self._leg2_filled_size
        )
        real_hedged = min(real_size_leg1, real_size_leg2)

        # Merge payout: each merged pair returns $1 USDC
        merge_payout = real_hedged * 1.0
        pnl = merge_payout - total_cost

        self._last_hedged_size = hedged_size
        self._last_pnl = pnl

        if self._leg2_filled_size + 1e-9 >= self._leg1_filled_size:
            self.leg2_entries += 1
            self._hedge_announced = True
            self._session_pnl += pnl
            log(
                f"DUAL ENTRY HEDGED  cost=${total_cost:.4f}, "
                f"merge={real_hedged:.4f} shares -> ${merge_payout:.4f}, "
                f"PnL=${pnl:+.4f}",
                "success",
            )
            # Auto-merge: convert both sides back into USDC immediately
            self._spawn_task(self._auto_merge(real_hedged))
        else:
            residual = max(0.0, self._leg1_filled_size - self._leg2_filled_size)
            log(
                f"Partial hedge. hedged={hedged_size:.4f}, residual={residual:.4f}, "
                f"cost=${total_cost:.4f}, est_merge=${merge_payout:.4f}, PnL=${pnl:+.4f}",
                "warning",
            )

    async def _auto_merge(self, merge_shares: float) -> None:
        """Merge both outcome tokens back into USDC after a successful hedge."""
        market = self.manager.current_market
        if not market:
            log("Merge: no active market", "warning")
            return

        condition_id = market.condition_id
        if not condition_id:
            log("Merge: no condition_id available, skip", "warning")
            return

        if self.cfg.dry_run:
            log(
                f"[DRY RUN] MERGE {merge_shares:.4f} shares -> "
                f"${merge_shares:.4f} USDC (condition={condition_id[:16]}...)",
                "trade",
            )
            self._merge_completed = True
            self._merge_tx_hash = "dry-run"
            self.merges_done += 1
            return

        if not self.merger:
            log("Merge: no Merger instance configured, skip", "warning")
            return

        try:
            log(
                f"Merging {merge_shares:.4f} shares -> USDC "
                f"(condition={condition_id[:16]}...)",
                "trade",
            )
            tx_hash = await asyncio.to_thread(
                self.merger.merge,
                condition_id=condition_id,
                shares=merge_shares,
                neg_risk=market.neg_risk,
            )
            self._merge_completed = True
            self._merge_tx_hash = tx_hash
            self.merges_done += 1
            log(f"MERGE OK: {tx_hash[:20]}...", "success")
        except Exception as exc:
            log(f"MERGE FAILED: {exc}", "error")

    async def _reconcile_open_orders_for_market(self, market: MarketInfo) -> None:
        if self.cfg.dry_run:
            return

        try:
            open_orders = await asyncio.to_thread(self.clob.get_open_orders)
        except Exception as exc:
            log(f"Could not fetch open orders for reconciliation: {exc}", "warning")
            return

        token_ids = set(market.token_ids.values())
        to_cancel: List[str] = []
        for order in open_orders:
            if not isinstance(order, dict):
                continue
            asset_id = str(order.get("asset_id", order.get("assetId", "")))
            order_id = str(order.get("id", order.get("orderID", "")))
            if asset_id in token_ids and order_id:
                to_cancel.append(order_id)

        if not to_cancel:
            return

        log(
            f"Reconciling: canceling {len(to_cancel)} pre-existing open order(s) in current market",
            "warning",
        )
        for order_id in to_cancel:
            try:
                await asyncio.to_thread(self.clob.cancel_order, order_id)
                log(f"  canceled reconciled order {order_id[:16]}...", "success")
            except Exception as exc:
                log(
                    f"  failed to cancel reconciled order {order_id[:16]}...: {exc}",
                    "warning",
                )

    async def _prepare_market_for_trading(self, market_slug: str) -> None:
        market = self.manager.current_market
        if not market or market.slug != market_slug:
            return

        await self._reconcile_open_orders_for_market(market)

        market = self.manager.current_market
        if not market or market.slug != market_slug:
            return

        # Prefetch fee rates for both sides when possible.
        fee_rates: List[int] = []
        for token_id in market.token_ids.values():
            if token_id not in self._fee_rate_cache:
                try:
                    fee = await asyncio.to_thread(self.clob.get_fee_rate_bps, token_id)
                    self._fee_rate_cache[token_id] = fee
                except Exception:
                    pass
            if token_id in self._fee_rate_cache:
                fee_rates.append(self._fee_rate_cache[token_id])
        self._current_market_fee_bps = max(fee_rates) if fee_rates else 0

        now = time.time()
        if now >= self._leg1_deadline:
            market_age = now - self._leg1_start_ts
            self.state = StrategyState.DONE
            log(
                f"  Market age {market_age:.0f}s is outside LEG 1 window "
                f"({self.cfg.entry_window:.0f}s from market birth). Waiting next market.",
                "warning",
            )
            return

        self.state = StrategyState.SCANNING_LEG1

        if now < self._leg1_start_ts:
            wait_secs = self._leg1_start_ts - now
            log(
                f"  Market not started yet. Waiting {wait_secs:.1f}s for birth.",
                "info",
            )

        remaining = max(0.0, self._leg1_deadline - now)
        log(
            f"  LEG 1 scanning ask <= {self.cfg.entry_price} for first "
            f"{self.cfg.entry_window:.0f}s after market birth "
            f"(remaining now: {remaining:.1f}s)",
            "trade",
        )

    def _on_market_change(self, old_slug: str, new_slug: str) -> None:
        log(f"Market changed: {old_slug} -> {new_slug}", "warning")

        if old_slug:
            self._spawn_task(self._cancel_stale_orders(old_slug))

        if self.manager.current_market:
            self._enter_market(self.manager.current_market)

    def _enter_market(self, market: MarketInfo) -> None:
        if (
            market.slug == self._current_market_slug
            and self.state != StrategyState.DONE
        ):
            return

        self._cancel_watch_tasks()

        self._current_market_slug = market.slug
        self._leg1_side = None

        now = time.time()
        market_start = market.start_timestamp()
        self._leg1_start_ts = float(market_start) if market_start is not None else now
        self._leg1_deadline = self._leg1_start_ts + self.cfg.entry_window

        self._best_asks = {"up": 1.0, "down": 1.0}
        self._leg1_inflight = False
        self._leg2_inflight = False
        self._leg1_next_attempt_ts = 0.0
        self._leg2_next_attempt_ts = 0.0
        self._leg1_error_count = 0
        self._leg2_error_count = 0
        self._ticks_market = 0
        self._leg1_filled_size = 0.0
        self._leg1_cost = 0.0
        self._leg1_avg_price = 0.0
        self._leg2_filled_size = 0.0
        self._leg2_cost = 0.0
        self._leg2_target_size = 0.0
        self._leg2_target_price = 0.0
        self._leg1_token_id = None
        self._leg2_token_id = None
        self._current_market_fee_bps = 0
        self._hedge_announced = False
        self._trade_cache.clear()
        self._leg1_fill_ts = 0.0
        self._leg2_fill_ts = 0.0
        self._leg1_real_size = 0.0
        self._leg2_real_size = 0.0
        self._leg1_fill_datetime = ""
        self._leg2_fill_datetime = ""
        self._merge_completed = False
        self._merge_tx_hash = None

        self.markets_seen += 1

        log(f"New market: {market.slug}", "success")
        log(f"  question: {market.question}")
        log(f"  tick_size: {market.tick_size}  neg_risk: {market.neg_risk}")

        self.state = StrategyState.WAITING_MARKET
        self._market_prepare_task = self._spawn_task(
            self._prepare_market_for_trading(market.slug)
        )

    def _cancel_watch_tasks(self) -> None:
        for task in [
            self._watch_leg1_task,
            self._watch_leg2_task,
            self._market_prepare_task,
        ]:
            if task is not None and not task.done():
                task.cancel()
        self._watch_leg1_task = None
        self._watch_leg2_task = None
        self._market_prepare_task = None

    def _remember_open_order(self, market_slug: str, order_id: str) -> None:
        if market_slug not in self._open_order_ids_by_market:
            self._open_order_ids_by_market[market_slug] = set()
        self._open_order_ids_by_market[market_slug].add(order_id)

    def _forget_open_order(self, market_slug: str, order_id: str) -> None:
        order_ids = self._open_order_ids_by_market.get(market_slug)
        if not order_ids:
            return
        order_ids.discard(order_id)
        if not order_ids:
            self._open_order_ids_by_market.pop(market_slug, None)

    async def _cancel_stale_orders(self, market_slug: str) -> None:
        order_ids = list(self._open_order_ids_by_market.pop(market_slug, set()))
        if not order_ids or self.cfg.dry_run:
            return

        log(f"Canceling {len(order_ids)} stale order(s) from {market_slug}", "warning")
        for order_id in order_ids:
            try:
                await asyncio.to_thread(self.clob.cancel_order, order_id)
                log(f"  canceled stale order {order_id[:16]}...", "success")
            except Exception as exc:
                log(
                    f"  failed to cancel stale order {order_id[:16]}...: {exc}",
                    "warning",
                )

    def _spawn_task(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def _cleanup(done_task: asyncio.Task) -> None:
            self._background_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                log(f"Background task error: {exc}", "error")

        task.add_done_callback(_cleanup)
        return task

    async def _shutdown_background_tasks(self) -> None:
        self._cancel_watch_tasks()

        pending = [task for task in self._background_tasks if not task.done()]
        for task in pending:
            task.cancel()

        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        self._background_tasks.clear()

    def _log_status_line(
        self,
        market: MarketInfo,
        ticks_window: int,
        tick_rate: float,
        seconds_since_last_tick: float,
    ) -> None:
        countdown = market.get_countdown_str()
        up_ask = self._best_asks.get("up", 1.0)
        down_ask = self._best_asks.get("down", 1.0)
        ws_state = "WS" if self.manager.is_connected else "DISC"
        last_tick_part = (
            f"last_tick={seconds_since_last_tick:.2f}s"
            if seconds_since_last_tick >= 0
            else "last_tick=NA"
        )
        ws_ts_part = (
            f"ws_ts={self._last_tick_ws_ts}" if self._last_tick_ws_ts else "ws_ts=NA"
        )

        log(
            f"[{ws_state}] {countdown} | state={self.state.value} | "
            f"up_ask={up_ask:.4f} down_ask={down_ask:.4f} | "
            f"ticks={ticks_window} ({tick_rate:.1f}/s) {last_tick_part} {ws_ts_part}",
            "info",
        )

    def _format_leg1_status(self) -> str:
        """Format LEG 1 status line for TUI."""
        G, Y, C, D, X = (
            Colors.GREEN,
            Colors.YELLOW,
            Colors.CYAN,
            Colors.DIM,
            Colors.RESET,
        )
        if self._leg1_filled_size > 0:
            side = (self._leg1_side or "?").upper()
            real_size = (
                self._leg1_real_size
                if self._leg1_real_size > 0
                else self._leg1_filled_size
            )
            ts_str = f" @{self._leg1_fill_datetime}" if self._leg1_fill_datetime else ""
            return (
                f"{G}{side:4} FILLED{X}  "
                f"{real_size:.2f} shares @ {self._leg1_avg_price:.4f}  "
                f"cost ${self._leg1_cost:.2f}{ts_str}"
            )
        if self.state == StrategyState.WAITING_LEG1_FILL:
            side = (self._leg1_side or "?").upper()
            return f"{Y}{side:4} RESTING{X}  limit @ {self.cfg.entry_price}"
        if self.state == StrategyState.SCANNING_LEG1:
            remaining = max(0.0, self._leg1_deadline - time.time())
            return f"{C}     SCANNING{X}  ask <= {self.cfg.entry_price}  ({remaining:.0f}s left)"
        return f"{D}     --{X}"

    def _format_leg2_status(self) -> str:
        """Format LEG 2 status line for TUI."""
        G, Y, C, D, X = (
            Colors.GREEN,
            Colors.YELLOW,
            Colors.CYAN,
            Colors.DIM,
            Colors.RESET,
        )
        opposite = "?"
        if self._leg1_side:
            opposite = "DOWN" if self._leg1_side == "up" else "UP"
        if self._leg2_filled_size > 0 and self._leg2_target_size <= 1e-9:
            avg = self._leg2_cost / max(self._leg2_filled_size, 1e-12)
            real_size = (
                self._leg2_real_size
                if self._leg2_real_size > 0
                else self._leg2_filled_size
            )
            ts_str = f" @{self._leg2_fill_datetime}" if self._leg2_fill_datetime else ""
            return (
                f"{G}{opposite:4} FILLED{X}  "
                f"{real_size:.2f} shares @ {avg:.4f}  "
                f"cost ${self._leg2_cost:.2f}{ts_str}"
            )
        if self.state == StrategyState.WAITING_LEG2_FILL:
            return (
                f"{Y}{opposite:4} RESTING{X}  limit @ {self._leg2_target_price:.4f}"
                f"  for {self._leg2_target_size:.2f} shares"
            )
        if self.state == StrategyState.SCANNING_LEG2:
            return (
                f"{C}{opposite:4} SCANNING{X}  ask <= {self._leg2_target_price:.4f}"
                f"  for {self._leg2_target_size:.2f} shares"
            )
        return f"{D}     --{X}"

    def _render_tui(
        self,
        market: MarketInfo,
        ticks_window: int,
        tick_rate: float,
        since_last_tick: float,
    ) -> None:
        """Render full-screen TUI with strategy state, legs, PnL, and events."""
        G = Colors.GREEN
        R = Colors.RED
        C = Colors.CYAN
        B = Colors.BOLD
        D = Colors.DIM
        X = Colors.RESET

        d = StatusDisplay(width=78)

        # --- Header ---
        ws_icon = f"{G}WS{X}" if self.manager.is_connected else f"{R}DC{X}"
        try:
            mins, secs = market.get_countdown()
            countdown = format_countdown(mins, secs)
        except Exception:
            countdown = market.get_countdown_str()

        state_colors = {
            StrategyState.WAITING_MARKET: Colors.YELLOW,
            StrategyState.SCANNING_LEG1: C,
            StrategyState.WAITING_LEG1_FILL: Colors.YELLOW,
            StrategyState.SCANNING_LEG2: C,
            StrategyState.WAITING_LEG2_FILL: Colors.YELLOW,
            StrategyState.DONE: G if self._hedge_announced else D,
        }
        sc = state_colors.get(self.state, X)

        d.add_bold_separator()
        d.add_line(
            f" {B}[{self.cfg.coin}]{X} [{ws_icon}] Dual Entry"
            f"          {countdown}    {sc}{self.state.value}{X}"
        )
        d.add_bold_separator()

        # --- Params ---
        d.add_line(
            f" entry <= {self.cfg.entry_price}  |  tickets <= {self.cfg.total_tickets}"
            f"  |  size: {self.cfg.size:.0f}  |  window: {self.cfg.entry_window:.0f}s"
        )
        d.add_separator()

        # --- Best asks ---
        up_ask = self._best_asks.get("up", 1.0)
        down_ask = self._best_asks.get("down", 1.0)
        up_mark, down_mark = "", ""

        if self.state in (StrategyState.SCANNING_LEG1, StrategyState.WAITING_MARKET):
            if up_ask <= self.cfg.entry_price:
                up_mark = f" {G}<= entry{X}"
            if down_ask <= self.cfg.entry_price:
                down_mark = f" {G}<= entry{X}"
        elif self.state in (
            StrategyState.SCANNING_LEG2,
            StrategyState.WAITING_LEG2_FILL,
        ):
            opp = "down" if self._leg1_side == "up" else "up"
            if self._best_asks.get(opp, 1.0) <= self._leg2_target_price:
                if opp == "up":
                    up_mark = f" {G}<= target{X}"
                else:
                    down_mark = f" {G}<= target{X}"

        d.add_line(
            f"       {G}UP{X}  ask: {up_ask:.4f}{up_mark}"
            f"               {R}DOWN{X}  ask: {down_ask:.4f}{down_mark}"
        )
        d.add_separator()

        # --- Legs ---
        d.add_line(f" LEG 1 | {self._format_leg1_status()}")
        d.add_line(f" LEG 2 | {self._format_leg2_status()}")
        if self._hedge_announced:
            merge_status = ""
            if self._merge_completed:
                merge_status = f"  |  {G}MERGED{X}"
            pc = G if self._last_pnl > 0 else R if self._last_pnl < 0 else D
            d.add_line(
                f"        {G}HEDGED{X}  PnL: {pc}${self._last_pnl:+.4f}{X}{merge_status}"
            )
        d.add_separator()

        # --- Session ---
        sc = G if self._session_pnl > 0 else R if self._session_pnl < 0 else D
        d.add_line(
            f" Session: {self.markets_seen} mkts  {self.leg2_entries} hedges"
            f"  {self.merges_done} merges"
            f"  |  PnL: {sc}${self._session_pnl:+.4f}{X}"
            f"  |  Spent: ${self.total_spent:.2f}"
        )
        d.add_separator()

        # --- Feed ---
        last_str = f"{since_last_tick:.2f}s" if since_last_tick >= 0 else "N/A"
        d.add_line(
            f" Feed: {ticks_window} ticks ({tick_rate:.1f}/s) last={last_str}"
            f"  |  market: {self._ticks_market}  total: {self._ticks_total}"
        )
        d.add_separator()

        # --- Events ---
        d.add_line(f" {B}Events (last 2 markets):{X}")
        if _log_buffer_ref:
            for msg in _log_buffer_ref[-12:]:
                d.add_line(msg)
        else:
            d.add_line(f"  {D}(waiting for events...){X}")
        d.add_bold_separator()

        d.render(in_place=True)

    def _print_summary(self) -> None:
        print()
        log("=" * 52)
        log("Session Summary")
        log(f"  Markets seen:  {self.markets_seen}")
        log(f"  LEG1 fills:    {self.leg1_entries}")
        log(f"  LEG2 fills:    {self.leg2_entries}")
        log(f"  Merges:        {self.merges_done}")
        log(f"  Total spent:   ${self.total_spent:.2f}")
        log(f"  Ticks (total): {self._ticks_total}")
        if self._leg1_filled_size > 0 or self._leg2_filled_size > 0:
            log(
                f"  Last market sizes: L1={self._leg1_filled_size:.4f} "
                f"L2={self._leg2_filled_size:.4f}"
            )
        if self._last_hedged_size > 0:
            log(
                f"  Last hedge: size={self._last_hedged_size:.4f} "
                f"PnL=${self._last_pnl:+.4f}",
                "success",
            )
        if self._session_pnl != 0:
            log(
                f"  Session PnL: ${self._session_pnl:+.4f}",
                "success",
            )
        log("=" * 52)


def build_components() -> Tuple[Config, OrderSigner, ClobClient]:
    """Build all required components from environment variables."""
    config = Config.from_env()

    private_key = os.environ.get("POLY_PRIVATE_KEY", "")
    if not private_key:
        print("ERROR: POLY_PRIVATE_KEY is not set in environment")
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dual Entry Strategy for Polymarket 5m markets (WebSocket)"
    )
    parser.add_argument(
        "--entry-price", type=float, default=0.48, help="LEG1 ask threshold"
    )
    parser.add_argument(
        "--total-tickets", type=float, default=0.90, help="LEG1 + LEG2 max total"
    )
    parser.add_argument(
        "--entry-window", type=float, default=30.0, help="Seconds for LEG1 scan"
    )
    parser.add_argument(
        "--size", type=float, default=5.0, help="Shares per order (>=5)"
    )
    parser.add_argument(
        "--coin", type=str, default="BTC", help="Coin symbol (default BTC)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Do not place real orders"
    )
    parser.add_argument(
        "--market-check-interval",
        type=float,
        default=5.0,
        help="Seconds between market discovery checks",
    )
    args = parser.parse_args()

    strategy_cfg = DualEntryConfig(
        entry_price=args.entry_price,
        total_tickets=args.total_tickets,
        entry_window=args.entry_window,
        size=args.size,
        coin=args.coin.upper(),
        dry_run=args.dry_run,
        market_check_interval=args.market_check_interval,
    )
    strategy_cfg.validate()

    print()
    log("Initializing components...", "info")
    bot_config, signer, clob = build_components()
    log(f"  EOA:   {signer.address}", "info")
    log(f"  Proxy: {bot_config.safe_address}", "info")
    log(f"  Sig:   type {bot_config.clob.signature_type}", "info")

    # Initialize merger for auto-merge after hedge
    merger = None
    if not args.dry_run:
        try:
            private_key = os.environ.get("POLY_PRIVATE_KEY", "")
            rpc_url = os.environ.get("POLY_RPC_URL", "https://polygon-rpc.com")
            merger = Merger(
                private_key=private_key,
                proxy_address=bot_config.safe_address,
                rpc_url=rpc_url,
            )
            log("  Merger: ready (auto-merge after hedge)", "info")
        except Exception as exc:
            log(f"  Merger: disabled ({exc})", "warning")
    else:
        log("  Merger: dry-run mode (simulated)", "info")
    print()

    strategy = DualEntryStrategy(
        config=strategy_cfg,
        bot_config=bot_config,
        signer=signer,
        clob=clob,
        merger=merger,
    )

    asyncio.run(strategy.run())


if __name__ == "__main__":
    main()
