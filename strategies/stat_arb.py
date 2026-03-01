"""
Stat-Arb Divergence Strategy - 5-Minute Up/Down Crypto Markets

Monitors all 4 coins (BTC, ETH, SOL, XRP) simultaneously on their 5-minute
Up/Down markets.  Computes a group mean of UP ask prices and buys the coin
whose UP ask is furthest below the group mean — a classic mean-reversion /
statistical arbitrage approach.

Thesis:
    In 5-minute crypto Up/Down markets, the four coins typically move in
    tandem.  When one coin's UP ask diverges significantly below the group
    mean, the market is temporarily mispricing that coin.  Buying the
    cheapest UP token and exiting via take-profit (or timeout) captures
    the reversion to the mean.

Trigger logic:
    - Compute group_mean = mean of all 4 UP ask prices.
    - Find the coin whose UP ask is furthest below the group mean.
    - If deviation >= spread threshold AND within window AND cooldown passed:
      BUY that coin's UP token via FOK.
    - After buying, monitor the bid price:
        * If bid >= entry_ask + target: SELL (take profit).
        * If time since entry >= timeout: SELL at current bid (timeout exit).
        * If market ends before either: hold to resolution (fallback).
    - One trade at a time per cycle.

Trade log:
    Every fill is appended to ``stat_arb_trades.txt`` (never overwritten).
    Each line contains timestamp, market, coin, side, entry, exit, exit_type,
    size, PnL, hold time, deviation, and config params.

Usage:
    python strategies/stat_arb.py --dry-run
    python strategies/stat_arb.py --spread 0.12 --target 0.15 --window 60
    python strategies/stat_arb.py --spread 0.08 --target 0.10 --timeout 20 --size 10
"""

import argparse
import asyncio
import concurrent.futures
import enum
import json
import logging
import math
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
from web3 import Web3  # noqa: E402

# CTF (Conditional Token Framework) contract on Polygon — holds ERC-1155 shares
_CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_CTF_BALANCE_ABI = [{"inputs": [{"name": "account", "type": "address"},
                                 {"name": "id", "type": "uint256"}],
                      "name": "balanceOf",
                      "outputs": [{"name": "", "type": "uint256"}],
                      "stateMutability": "view", "type": "function"}]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COINS: List[str] = ["BTC", "ETH", "SOL", "XRP"]

TRADE_LOG_FILE = Path(__file__).resolve().parent.parent / "stat_arb_trades.txt"

# ---------------------------------------------------------------------------
# Dry-run simulation realism
# ---------------------------------------------------------------------------
# These penalties match the stress-test "Realistic" scenario so the dry-run
# PnL closely approximates what we'd see in live trading.
#
# Entry:  +1c  — simulates worse fill due to ~360ms latency + orderbook depth.
# Exit:   +2c  — exit slippage is worse (TP race, expiry crowd, thinner bids).
# Fee:    1%/side (~2% RT) — conservative estimate for Polymarket taker fees.
SIM_ENTRY_SLIP = 0.01   # 1 cent worse than best ask
SIM_EXIT_SLIP  = 0.02   # 2 cents worse than best bid (matches stress-test "Realistic")
SIM_FEE_RATE   = 0.01   # 1% per side (~2% round-trip, matches stress-test "Realistic")


# ---------------------------------------------------------------------------
# TUI-aware logging (same pattern as btc_signal)
# ---------------------------------------------------------------------------
_log_buffer: list = []
_tui_active = False


def ts_now() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]  # HH:MM:SS.mmm


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
    cfg: "StatArbConfig",
    outcome: str = "PENDING",
    log_file: Optional[Path] = None,
) -> None:
    """Append one line per fill to the persistent trade log file."""
    target = log_file or TRADE_LOG_FILE
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    now_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    exit_price_str = f"{pos.exit_price:.4f}" if pos.exit_price is not None else "--"
    exit_type_str = pos.exit_type or "--"
    pnl_str = f"${pos.pnl:+.4f}" if pos.pnl is not None else "--"
    hold_str = f"{int(pos.exit_time - pos.fill_time)}s" if pos.exit_time else "--"

    line = (
        f"{now_utc} | {now_local} | "
        f"order_id={pos.order_id} | "
        f"market={pos.market_slug} | coin={pos.coin} | side={pos.side.upper()} | "
        f"entry={pos.fill_price:.4f} | exit={exit_price_str} | "
        f"exit_type={exit_type_str} | "
        f"size={pos.fill_size:.4f} | pnl={pnl_str} | "
        f"hold={hold_str} | "
        f"dev={pos.deviation:.4f} | "
        f"spread={cfg.spread} | target={cfg.target} | "
        f"window={cfg.window:.0f}s | "
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


def _update_trade_log_exit(
    order_id: str,
    exit_price: float,
    exit_type: str,
    pnl: float,
    hold_secs: int,
    log_file: Optional[Path] = None,
) -> None:
    """Update exit fields for a trade that exited early (TP/timeout)."""
    target = log_file or TRADE_LOG_FILE
    try:
        if not target.exists():
            return
        lines = target.read_text(encoding="utf-8").splitlines()
        updated = []
        for line in lines:
            if order_id and f"order_id={order_id}" in line:
                # Replace placeholder exit fields
                line = line.replace("exit=--", f"exit={exit_price:.4f}")
                line = line.replace("exit_type=--", f"exit_type={exit_type}")
                line = line.replace("pnl=--", f"pnl=${pnl:+.4f}")
                line = line.replace("hold=--", f"hold={hold_secs}s")
                # Update outcome from PENDING to CLOSED
                line = line.replace("outcome=PENDING", "outcome=CLOSED")
            updated.append(line)
        target.write_text("\n".join(updated) + "\n", encoding="utf-8")
    except Exception:
        pass


# ===================================================================
# Configuration
# ===================================================================
@dataclass
class StatArbConfig:
    """Configuration for the Stat-Arb Divergence strategy."""

    window: float = 60.0      # seconds from market birth to allow entry
    spread: float = 0.12      # min deviation from group mean to trigger
    target: float = 0.15      # take-profit target above entry ask
    timeout: int = 30          # seconds to hold before timeout exit
    size: float = 5.0          # shares per order
    slippage: float = 0.03     # FOK slippage buffer
    cooldown: int = 10         # min seconds between signals in same cycle
    dry_run: bool = False
    market_check_interval: float = 5.0
    name: str = ""             # instance identifier

    def validate(self) -> None:
        if not 1.0 <= self.window <= 300.0:
            raise ValueError(f"window must be 1-300 seconds, got {self.window}")
        if not 0.03 <= self.spread <= 0.50:
            raise ValueError(f"spread must be 0.03-0.50, got {self.spread}")
        if not 0.01 <= self.target <= 0.50:
            raise ValueError(f"target must be 0.01-0.50, got {self.target}")
        if not 5 <= self.timeout <= 300:
            raise ValueError(f"timeout must be 5-300 seconds, got {self.timeout}")
        if self.size < 5:
            raise ValueError(f"size must be >= 5, got {self.size}")
        if not 0.01 <= self.slippage <= 0.20:
            raise ValueError(f"slippage must be 0.01-0.20, got {self.slippage}")
        if not 1 <= self.cooldown <= 300:
            raise ValueError(f"cooldown must be 1-300, got {self.cooldown}")


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
    # Exit tracking
    exit_price: Optional[float] = None
    exit_type: Optional[str] = None   # "TP", "TIMEOUT", "EXPIRY_WIN", "EXPIRY_LOSS"
    exit_time: Optional[float] = None
    pnl: Optional[float] = None
    deviation: float = 0.0
    # Resolution fallback
    resolved: bool = False
    won: Optional[bool] = None
    payout: float = 0.0
    sell_failed: bool = False  # True after sell attempt fails (hold to expiry)
    wallet_size: Optional[float] = None  # Exact shares in wallet (from balance API)


# ===================================================================
# Strategy
# ===================================================================
class StatArbStrategy:
    """Buy cheapest divergent coin UP token, exit via TP or timeout."""

    def __init__(
        self,
        cfg: StatArbConfig,
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
                TRADE_LOG_FILE.parent / f"stat_arb_sim_{cfg.name}.txt"
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

        # Active position (one at a time per cycle)
        self._position: Optional[PositionRecord] = None
        self._holding_position: bool = False
        self._trade_executed: bool = False

        # GTC TP limit sell tracking
        self._tp_order_id: Optional[str] = None
        self._tp_gtc_active: bool = False
        self._last_tp_poll: float = 0.0

        # On-chain balance query (CTF balanceOf)
        rpc_url = os.environ.get("POLY_RPC_URL", "https://polygon.publicnode.com")
        self._w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 5}))
        self._ctf = self._w3.eth.contract(
            address=Web3.to_checksum_address(_CTF_ADDRESS), abi=_CTF_BALANCE_ABI,
        )
        self._safe_checksum = Web3.to_checksum_address(bot_config.safe_address)

        # Signal state
        self._last_signal_t: float = 0.0
        self._last_signal_coin: Optional[str] = None
        self._last_signal_dev: float = 0.0
        self._last_group_mean: float = 0.0

        # Session stats (restored from trade log on startup)
        self.cycles_seen: int = 0
        self.total_orders_placed: int = 0
        self.total_fills: int = 0
        self.total_wins: int = 0
        self.total_losses: int = 0
        self.total_resolved: int = 0
        self.session_pnl: float = 0.0
        self.total_spent: float = 0.0
        self.total_received: float = 0.0   # sell proceeds + win payouts
        self.total_shares: float = 0.0
        self.total_tp: int = 0
        self.total_timeout: int = 0

        # Per-coin stats
        self.coin_wins: Dict[str, int] = {c: 0 for c in COINS}
        self.coin_losses: Dict[str, int] = {c: 0 for c in COINS}
        self.coin_resolved: Dict[str, int] = {c: 0 for c in COINS}

        self._load_stats_from_log()

        # Per-coin orderbook caches
        self._best_asks: Dict[str, Dict[str, float]] = {
            c: {"up": 1.0, "down": 1.0} for c in COINS
        }
        self._best_bids: Dict[str, Dict[str, float]] = {
            c: {"up": 0.0, "down": 0.0} for c in COINS
        }
        # Polymarket server timestamps for each book update (seconds)
        self._book_pm_ts: Dict[str, Dict[str, float]] = {
            c: {"up": 0.0, "down": 0.0} for c in COINS
        }
        self._coin_markets: Dict[str, Optional[MarketInfo]] = {
            c: None for c in COINS
        }

        # Fee cache
        self._fee_rate_cache: Dict[str, int] = {}

        # Dedicated single-thread executor for CLOB API calls.
        # Guarantees: 1) session/TCP reuse (same thread = same keep-alive),
        #             2) sign + POST in one call (zero event-loop blocking).
        self._clob_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="clob-hot"
        )

        # Event-driven: WS callback signals this when any book updates
        self._book_event: asyncio.Event = asyncio.Event()

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

                size = _to_float(fields.get("size", "0"))
                entry = _to_float(fields.get("entry", "0"))
                cost = entry * size if entry > 0 and size > 0 else 0.0
                outcome = fields.get("outcome", "")
                exit_type = fields.get("exit_type", "")
                pnl_str = fields.get("pnl", "").lstrip("$").lstrip("+")

                self.total_fills += 1
                self.total_spent += cost
                self.total_shares += size

                coin = fields.get("coin", "").upper()

                if outcome == "CLOSED":
                    # Early exit (TP or TIMEOUT)
                    pnl_val = _to_float(pnl_str)
                    self.session_pnl += pnl_val
                    # Restore sell proceeds
                    exit_price_val = _to_float(fields.get("exit", "0"))
                    if exit_price_val > 0 and size > 0:
                        self.total_received += exit_price_val * size
                    if exit_type == "TP":
                        self.total_tp += 1
                        self.total_wins += 1
                        self.total_resolved += 1
                    elif exit_type == "TIMEOUT":
                        self.total_timeout += 1
                        self.total_resolved += 1
                        if pnl_val >= 0:
                            self.total_wins += 1
                        else:
                            self.total_losses += 1
                    if coin in self.coin_resolved:
                        self.coin_resolved[coin] += 1
                        if pnl_val >= 0 and coin in self.coin_wins:
                            self.coin_wins[coin] += 1
                        elif pnl_val < 0 and coin in self.coin_losses:
                            self.coin_losses[coin] += 1

                elif outcome.startswith("WIN"):
                    self.total_wins += 1
                    self.total_resolved += 1
                    if coin in self.coin_wins:
                        self.coin_wins[coin] += 1
                        self.coin_resolved[coin] += 1
                    profit_str = outcome.replace("WIN +$", "").replace("WIN +", "")
                    self.session_pnl += _to_float(profit_str)
                    self.total_received += size  # payout = size * 1.0

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

        log("Stat-Arb Divergence Strategy started (4-coin WebSocket)", "success")
        log(f"  window:   {self.cfg.window:.0f}s")
        log(f"  spread:   >={self.cfg.spread} (min deviation from group mean)")
        log(f"  target:   {self.cfg.target} (take-profit above entry)")
        log(f"  timeout:  {self.cfg.timeout}s")
        log(f"  size:     {self.cfg.size} shares")
        log(f"  cooldown: {self.cfg.cooldown}s")
        log(f"  dry_run:  {self.cfg.dry_run}")
        if self.cfg.dry_run:
            log(f"  sim_adj:  entry+{SIM_ENTRY_SLIP:.2f} exit-{SIM_EXIT_SLIP:.2f}"
                f" fee={SIM_FEE_RATE:.1%}/side")
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
            # Invalidate stale orderbook data from old market
            self._best_asks[coin] = {"up": 1.0, "down": 1.0}
            self._best_bids[coin] = {"up": 0.0, "down": 0.0}
            self._book_pm_ts[coin] = {"up": 0.0, "down": 0.0}
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

        # Reject stale data from previous cycle's market.
        # After a new cycle starts, _cycle_ts is set to the new market's
        # start timestamp.  Any coin whose _coin_markets still points to
        # the old market will have a different start_timestamp and be
        # rejected here — preventing stale prices from leaking through.
        if self._cycle_ts is not None:
            ms = market.start_timestamp()
            if ms is not None and ms != self._cycle_ts:
                return

        try:
            asset_id = snapshot.asset_id
            for side in ("up", "down"):
                if market.token_ids.get(side) == asset_id:
                    # Best ask
                    asks = snapshot.asks
                    best_ask = asks[0].price if asks else 1.0
                    self._best_asks[coin][side] = best_ask

                    # Best bid (needed for TP monitoring)
                    bids = snapshot.bids
                    best_bid = bids[0].price if bids else 0.0
                    self._best_bids[coin][side] = best_bid

                    # Store Polymarket's server timestamp (seconds)
                    pm_ts = snapshot.timestamp
                    if pm_ts > 1e12:
                        pm_ts = pm_ts / 1000.0
                    self._book_pm_ts[coin][side] = float(pm_ts)

                    # Wake the trading loop instantly
                    self._book_event.set()
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
        self._best_bids = {c: {"up": 0.0, "down": 0.0} for c in COINS}
        self._book_pm_ts = {c: {"up": 0.0, "down": 0.0} for c in COINS}
        # Clear fee cache — token IDs change every cycle
        self._fee_rate_cache.clear()

        # --- Reset signal/position state ---
        self._trade_executed = False
        self._holding_position = False
        self._position = None
        self._tp_order_id = None
        self._tp_gtc_active = False
        self._last_tp_poll = 0.0
        self._last_signal_t = 0.0
        self._last_signal_coin = None
        self._last_signal_dev = 0.0
        self._last_group_mean = 0.0

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
            f"spread>={self.cfg.spread}  target={self.cfg.target}  "
            f"timeout={self.cfg.timeout}s",
            "trade",
        )
        self.cycle_state = CycleState.ACTIVE

        # Register all coins that share this market start
        for c in COINS:
            m = self._coin_markets.get(c)
            if m and m.start_timestamp() == market_start:
                self._coins_entered.add(c)

        # Pre-fetch fee rates for all known tokens
        loop = asyncio.get_running_loop()
        for c in self._coins_entered:
            m = self._coin_markets.get(c)
            if not m:
                continue
            for s in ("up", "down"):
                tid = m.token_ids.get(s, "")
                if tid and tid not in self._fee_rate_cache:
                    loop.create_task(self._prefetch_fee(tid))

        # Pre-warm HTTPS connection on dedicated CLOB thread
        # (TCP + TLS handshake done BEFORE first hot-path BUY)
        loop.create_task(self._prewarm_clob())

        # Start the watcher
        if self._fill_watcher_task and not self._fill_watcher_task.done():
            self._fill_watcher_task.cancel()
        self._fill_watcher_task = loop.create_task(
            self._watch_and_trade()
        )

    def _transition_to_done(self) -> None:
        """Transition to DONE and schedule resolution."""
        self.cycle_state = CycleState.DONE

        # Cancel any resting GTC TP order (fire-and-forget)
        if self._tp_gtc_active and self._tp_order_id:
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(self._cancel_tp_order())
            except RuntimeError:
                pass  # no running loop — best-effort
        self._tp_order_id = None
        self._tp_gtc_active = False

        # Stop watcher
        if self._fill_watcher_task and not self._fill_watcher_task.done():
            self._fill_watcher_task.cancel()

        # Mark remaining orders as cancelled locally
        for t in self._orders.values():
            if not t.filled and not t.cancelled:
                t.cancelled = True

        # If holding a position that hasn't exited, schedule resolution
        if self._position and self._position.exit_type is None:
            self._holding_position = False
            # Will be resolved via _schedule_resolution_all

        # Schedule resolution for ALL unique slugs in current positions
        seen_slugs: Set[str] = set()
        for pos in self._current_positions:
            if pos.market_slug and pos.market_slug not in seen_slugs:
                seen_slugs.add(pos.market_slug)
                self._schedule_resolution_all(pos.market_slug)

    # ------------------------------------------------------------------
    # Fee pre-fetch (runs as background task at cycle start)
    # ------------------------------------------------------------------
    async def _prefetch_fee(self, token_id: str) -> None:
        """Fetch fee rate in the background so the hot path has a cache hit."""
        try:
            fee = await asyncio.to_thread(self.clob.get_fee_rate_bps, token_id)
            self._fee_rate_cache[token_id] = fee
        except Exception:
            pass  # Will fall back to on-demand fetch if this fails

    # ------------------------------------------------------------------
    # Core: watch for divergence signal, monitor TP/timeout
    # ------------------------------------------------------------------
    async def _watch_and_trade(self) -> None:
        """Main trading loop for the stat-arb strategy.

        Event-driven: wakes on every WebSocket book update via
        ``_book_event`` (<1 ms latency) instead of polling with sleep.
        Computes group mean of UP asks, finds cheapest divergent coin,
        and buys via FOK. After buying, monitors bid for TP or timeout.
        One trade per 5-minute cycle.
        """
        while self.cycle_state == CycleState.ACTIVE:
            # Wait for the next book update (any coin).
            # Timeout at 2s so deadline/TUI checks still run even
            # if no WS ticks arrive.
            try:
                await asyncio.wait_for(self._book_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            self._book_event.clear()

            now = time.time()

            # If we have a position, monitor TP/timeout REGARDLESS of
            # whether the entry window has expired.  The window only
            # controls when NEW entries are allowed — once we're in a
            # position, TP/timeout monitoring must continue until exit.
            if self._holding_position:
                await self._check_tp_exit(now)
                continue

            # If already traded this cycle (after TP/timeout/expiry), stop
            if self._trade_executed:
                continue

            # Window expired — no more entries allowed
            if now >= self._cycle_deadline:
                continue

            # --- Signal detection: group mean divergence ---
            asks: Dict[str, float] = {}
            for coin in COINS:
                ask = self._best_asks[coin]["up"]
                if ask > 0.90 or ask < 0.10:
                    continue  # stale / extreme — no legit price is <0.10 or >0.90 in first 30s
                asks[coin] = ask

            if len(asks) < 4:
                continue  # need all 4 coins for group mean

            group_mean = sum(asks.values()) / len(asks)

            # Find cheapest coin (biggest deviation below mean)
            best_coin: Optional[str] = None
            best_dev: float = 0.0
            for coin, ask in asks.items():
                dev = group_mean - ask
                if dev > best_dev:
                    best_coin = coin
                    best_dev = dev

            if best_coin is None or best_dev < self.cfg.spread:
                continue

            # Cooldown check
            if now - self._last_signal_t < self.cfg.cooldown:
                continue

            # Store signal info for TUI
            self._last_signal_coin = best_coin
            self._last_signal_dev = best_dev
            self._last_group_mean = group_mean
            self._last_signal_t = now

            # Execute buy
            entry_ask = asks[best_coin]
            buy_price = min(round(entry_ask + self.cfg.slippage, 2), 0.99)
            buy_price = max(buy_price, entry_ask)  # never below ask

            await self._execute_buy(
                best_coin, "up", buy_price,
                ask_price=entry_ask,
                deviation=best_dev,
                group_mean=group_mean,
            )

    # ------------------------------------------------------------------
    # Take-profit / timeout exit monitoring
    # ------------------------------------------------------------------
    async def _check_tp_exit(self, now: float) -> None:
        """Check if current position should exit via TP or timeout."""
        if not self._position:
            return

        pos = self._position
        # If sell already failed, stop trying — hold to expiry
        if pos.sell_failed:
            return

        bid = self._best_bids[pos.coin]["up"]
        elapsed = now - pos.fill_time
        tp_price = pos.fill_price + self.cfg.target

        # --- TP detection ---
        if not self.cfg.dry_run and self._tp_gtc_active:
            # Live with GTC: poll order status every 3s
            if (now - self._last_tp_poll) >= 3.0:
                self._last_tp_poll = now
                filled = await self._poll_tp_order()
                if filled:
                    await self._record_gtc_tp_fill(pos, filled)
                    return
        elif not self.cfg.dry_run and not self._tp_gtc_active:
            # Live without GTC (placement failed): fall back to FOK on bid
            if bid >= tp_price:
                log(
                    f"$ TP HIT: {pos.coin}-UP bid={bid:.4f} >= {tp_price:.4f}"
                    f" (+${(bid - pos.fill_price) * pos.fill_size:.4f},"
                    f" {int(elapsed)}s)",
                    "trade",
                )
                await self._execute_exit(pos, bid, "TP")
                return
        else:
            # Dry-run: simulate TP when bid >= target
            if bid >= tp_price:
                log(
                    f"$ TP HIT: {pos.coin}-UP bid={bid:.4f} >= {tp_price:.4f}"
                    f" (+${(bid - pos.fill_price) * pos.fill_size:.4f},"
                    f" {int(elapsed)}s)",
                    "trade",
                )
                await self._execute_exit(pos, bid, "TP")
                return

        # --- Timeout ---
        if elapsed >= self.cfg.timeout:
            pnl = (bid - pos.fill_price) * pos.fill_size
            log(
                f"$ TIMEOUT: {pos.coin}-UP bid={bid:.4f} after {int(elapsed)}s"
                f" ({'+' if pnl >= 0 else ''}{pnl:.4f})",
                "trade",
            )
            # Cancel resting GTC before timeout sell
            if self._tp_gtc_active:
                await self._cancel_tp_order()
            await self._execute_exit(pos, bid, "TIMEOUT")
            return

    # ------------------------------------------------------------------
    # Exit execution (TP or timeout)
    # ------------------------------------------------------------------
    async def _execute_exit(
        self, pos: PositionRecord, bid_price: float, exit_type: str
    ) -> None:
        """Execute a sell at the given bid price (TP or timeout exit)."""
        pnl = (bid_price - pos.fill_price) * pos.fill_size
        hold_secs = int(time.time() - pos.fill_time)

        if self.cfg.dry_run:
            # Simulate sell with realism penalties:
            # - Exit slippage: sell at bid - SIM_EXIT_SLIP (latency + matching)
            # - Taker fees on both entry and exit sides
            sim_exit = round(max(bid_price - SIM_EXIT_SLIP, 0.01), 4)
            entry_fee = pos.fill_price * pos.fill_size * SIM_FEE_RATE
            exit_fee = sim_exit * pos.fill_size * SIM_FEE_RATE
            sim_pnl = (sim_exit - pos.fill_price) * pos.fill_size - entry_fee - exit_fee

            pos.exit_price = sim_exit
            pos.exit_type = exit_type
            pos.exit_time = time.time()
            pos.pnl = sim_pnl
            pos.resolved = True
            pos.won = sim_pnl >= 0

            self._holding_position = False
            self._trade_executed = True
            self._position = None

            # Update stats
            self.total_resolved += 1
            self.session_pnl += sim_pnl
            self.total_received += sim_exit * pos.fill_size
            if exit_type == "TP":
                self.total_tp += 1
                if sim_pnl >= 0:
                    self.total_wins += 1
                else:
                    # TP triggered but fees/slippage ate the profit
                    self.total_losses += 1
            elif exit_type == "TIMEOUT":
                self.total_timeout += 1
                if sim_pnl >= 0:
                    self.total_wins += 1
                else:
                    self.total_losses += 1

            coin_key = pos.coin.upper()
            if coin_key in self.coin_resolved:
                self.coin_resolved[coin_key] += 1
            if sim_pnl >= 0 and coin_key in self.coin_wins:
                self.coin_wins[coin_key] += 1
            elif sim_pnl < 0 and coin_key in self.coin_losses:
                self.coin_losses[coin_key] += 1

            log(
                f"[SIM] SELL {pos.coin}-UP bid={bid_price:.4f}"
                f" sim_exit={sim_exit:.4f}"
                f" exit={exit_type} pnl=${sim_pnl:+.4f}"
                f" (fees=${entry_fee + exit_fee:.3f})"
                f" hold={hold_secs}s",
                "success" if sim_pnl >= 0 else "warning",
            )

            # Update persistent log
            _update_trade_log_exit(
                pos.order_id, sim_exit, exit_type, sim_pnl, hold_secs,
                log_file=self.log_file,
            )
        else:
            # Live sell: submit SELL FOK order
            market = self._coin_markets.get(pos.coin)
            if not market:
                log(f"EXIT ABORT: no market for {pos.coin}", "error")
                return

            token_id = market.token_ids.get("up", "")
            if not token_id:
                log(f"EXIT ABORT: no UP token for {pos.coin}", "error")
                return

            # Sell price: we accept slightly below bid for guaranteed fill
            sell_price = max(round(bid_price - self.cfg.slippage, 2), 0.01)

            sell_result = await self._submit_sell_order(
                pos, token_id, market, sell_price
            )

            if sell_result:
                actual_exit_price = sell_result.fill_price
                actual_pnl = (actual_exit_price - pos.fill_price) * sell_result.fill_size

                pos.exit_price = actual_exit_price
                pos.exit_type = exit_type
                pos.exit_time = time.time()
                pos.pnl = actual_pnl
                pos.resolved = True
                pos.won = actual_pnl >= 0

                self._holding_position = False
                self._trade_executed = True
                self._position = None

                # Update stats
                self.total_resolved += 1
                self.session_pnl += actual_pnl
                self.total_received += actual_exit_price * sell_result.fill_size
                if exit_type == "TP":
                    self.total_tp += 1
                    self.total_wins += 1
                elif exit_type == "TIMEOUT":
                    self.total_timeout += 1
                    if actual_pnl >= 0:
                        self.total_wins += 1
                    else:
                        self.total_losses += 1

                coin_key = pos.coin.upper()
                if coin_key in self.coin_resolved:
                    self.coin_resolved[coin_key] += 1
                if actual_pnl >= 0 and coin_key in self.coin_wins:
                    self.coin_wins[coin_key] += 1
                elif actual_pnl < 0 and coin_key in self.coin_losses:
                    self.coin_losses[coin_key] += 1

                hold_secs = int(pos.exit_time - pos.fill_time)
                log(
                    f"SELL FILLED {pos.coin}-UP @{actual_exit_price:.4f}"
                    f" exit={exit_type} pnl=${actual_pnl:+.4f}"
                    f" hold={hold_secs}s",
                    "success" if actual_pnl >= 0 else "warning",
                )

                _update_trade_log_exit(
                    pos.order_id, actual_exit_price, exit_type,
                    actual_pnl, hold_secs, log_file=self.log_file,
                )
            else:
                # Sell failed -- stop retrying, hold to expiry for resolution
                pos.sell_failed = True
                self._holding_position = False
                self._trade_executed = True
                log(
                    f"SELL FAILED {pos.coin}-UP -- holding to expiry",
                    "error",
                )

    # ------------------------------------------------------------------
    # Low-level FOK sell (single attempt)
    # ------------------------------------------------------------------
    async def _try_sell_fok(
        self,
        token_id: str,
        market: MarketInfo,
        sell_price: float,
        sell_size: float,
        fee_rate_bps: int,
        label: str,
    ) -> Optional[Dict[str, Any]]:
        """Send one FOK SELL to the CLOB.

        Returns response dict on success/expected failure, or None on
        unexpected error.  HTTP 400 "balance" errors are returned as
        {"success": False, "errorMsg": ...} so callers can retry.
        """
        try:
            order = Order(
                token_id=token_id,
                price=sell_price,
                size=sell_size,
                side="SELL",
                funder=self.bot_config.safe_address,
                fee_rate_bps=fee_rate_bps,
                signature_type=self.bot_config.clob.signature_type,
                neg_risk=market.neg_risk,
                tick_size=market.tick_size,
            )
            signed = self.signer.sign_order(order)

            prev_timeout, prev_retry = self.clob.timeout, self.clob.retry_count
            self.clob.timeout = 5
            self.clob.retry_count = 1
            try:
                response = await asyncio.to_thread(
                    self.clob.post_order, signed, "FOK"
                )
            finally:
                self.clob.timeout = prev_timeout
                self.clob.retry_count = prev_retry

            return response
        except Exception as exc:
            err = str(exc).lower()
            if "balance" in err or "allowance" in err:
                # Return as dict so retry loop can handle it
                return {"success": False, "errorMsg": str(exc)}
            log(f"SELL FOK ERR {label}: {exc}", "error")
            return None

    # ------------------------------------------------------------------
    # Sell order submission (FOK)
    # ------------------------------------------------------------------
    async def _submit_sell_order(
        self,
        pos: PositionRecord,
        token_id: str,
        market: MarketInfo,
        sell_price: float,
    ) -> Optional[OrderTracker]:
        """Submit a FOK SELL order to the CLOB.

        Uses wallet_size (exact balance from API) when available.
        Falls back to re-querying balance, then to fill_size if all
        else fails.
        """
        label = f"{pos.coin}-UP-SELL"
        try:
            fee_rate_bps = self._fee_rate_cache.get(token_id)
            if fee_rate_bps is None:
                fee_rate_bps = await asyncio.to_thread(
                    self.clob.get_fee_rate_bps, token_id
                )
                self._fee_rate_cache[token_id] = fee_rate_bps

            # Best source: wallet_size from on-chain balanceOf
            # Fallback: re-query now, then retry with decreasing %
            raw_size = pos.wallet_size
            if not raw_size:
                await self._query_wallet_balance(token_id)
                raw_size = pos.wallet_size

            if raw_size:
                # Exact balance known — single attempt, 6-decimal precision
                size_attempts = [raw_size]
            else:
                # Balance unknown — retry with decreasing sizes
                size_attempts = [pos.fill_size * f for f in
                                 [1.00, 0.99, 0.98, 0.97, 0.96, 0.95, 0.94, 0.93, 0.92]]

            response = None
            sell_size = 0.0
            for attempt_i, attempt_raw in enumerate(size_attempts):
                sell_size = round(attempt_raw, 6)
                if attempt_i > 0:
                    log(f"SELL retry #{attempt_i}: size={sell_size:.6f}", "info")

                response = await self._try_sell_fok(
                    token_id, market, sell_price, sell_size, fee_rate_bps, label,
                )

                if response is None:
                    return None  # exception

                if response.get("success", False):
                    break  # success

                error = response.get("errorMsg", "unknown")
                if "balance" in error.lower() and attempt_i < len(size_attempts) - 1:
                    continue  # retry smaller
                log(f"SELL FOK FAIL {label}: {error}", "error")
                return None
            else:
                log(f"SELL {label}: all size attempts failed", "error")
                return None

            if not response or not response.get("success", False):
                return None

            order_id = (
                response.get("orderID")
                or response.get("orderId")
                or response.get("order_id")
                or ""
            )

            status = str(response.get("status", "")).lower()
            tracker = OrderTracker(
                coin=pos.coin,
                side="up",
                token_id=token_id,
                order_id=order_id,
                price=sell_price,
                size=sell_size,
                placed_at=time.time(),
                market_slug=market.slug,
            )

            if status in {"matched", "filled", "executed", "complete", "completed"}:
                # Verify fill
                taking = 0.0
                making = 0.0
                if order_id:
                    verified = await self._verify_fill(
                        order_id, sell_price, token_id
                    )
                    if verified:
                        taking, making = verified
                # Fallback to POST response
                if taking <= 0:
                    taking = _to_float(response.get("takingAmount", 0))
                    making = _to_float(response.get("makingAmount", 0))

                fp = making / max(taking, 1e-12) if taking > 0 else sell_price
                tracker.filled = True
                tracker.fill_price = fp
                tracker.fill_size = taking if taking > 0 else sell_size
                tracker.fill_time = time.time()
                self._orders[order_id] = tracker
                return tracker

            # FOK: if not filled, it's killed
            log(f"SELL FOK KILLED {label}: ask moved", "warning")
            tracker.cancelled = True
            self._orders[order_id] = tracker
            return None

        except Exception as exc:
            log(f"SELL FOK ERR {label}: {exc}", "error")
            return None

    # ------------------------------------------------------------------
    # Wallet balance query — on-chain CTF balanceOf (ground truth)
    # ------------------------------------------------------------------
    async def _query_wallet_balance(self, token_id: str) -> None:
        """Query exact on-chain token balance via CTF balanceOf.

        On-chain settlement takes ~2-4s after CLOB fill confirmation.
        Polls every 0.5s to minimize delay before placing GTC sell.
        """
        pos = self._position
        if not pos or self.cfg.dry_run:
            return
        t0 = time.monotonic()
        for attempt in range(12):  # 12 * 0.5s = 6s max
            if attempt > 0:
                await asyncio.sleep(0.5)
            try:
                raw = await asyncio.to_thread(
                    self._ctf.functions.balanceOf(
                        self._safe_checksum, int(token_id),
                    ).call,
                )
                balance = raw / 1e6  # CTF tokens use 6 decimals
                if balance > 0:
                    elapsed = time.monotonic() - t0
                    pos.wallet_size = balance
                    log(
                        f"On-chain balance: {balance:.6f} shares"
                        f" (fill_size={pos.fill_size:.6f},"
                        f" fee={pos.fill_size - balance:+.6f},"
                        f" settled in {elapsed:.1f}s)",
                        "info",
                    )
                    return
            except Exception as exc:
                log(f"On-chain query err (attempt {attempt}): {exc}", "warning")
        log("On-chain balance still 0 after 6s — sell will retry with decreasing sizes", "warning")

    # ------------------------------------------------------------------
    # GTC limit sell at TP price (maker = 0% fee)
    # ------------------------------------------------------------------
    async def _place_tp_gtc_sell(
        self, token_id: str, market: MarketInfo,
        size_override: Optional[float] = None,
    ) -> None:
        """Place a resting GTC limit sell at fill_price + target.

        This sits on the book as a maker order (0% fee).  If it fills,
        we get the TP exit for free.  If timeout expires first, we
        cancel and FOK sell at market.

        Args:
            size_override: If set, use this exact size (for provisional
                orders before on-chain balance is known).
        """
        pos = self._position
        if not pos:
            return

        tp_price = round(pos.fill_price + self.cfg.target, 2)
        label = f"{pos.coin}-UP-GTC-TP"

        try:
            fee_rate_bps = self._fee_rate_cache.get(token_id)
            if fee_rate_bps is None:
                fee_rate_bps = await asyncio.to_thread(
                    self.clob.get_fee_rate_bps, token_id
                )
                self._fee_rate_cache[token_id] = fee_rate_bps

            if size_override:
                size_attempts = [size_override]
            elif pos.wallet_size:
                size_attempts = [pos.wallet_size]
            else:
                size_attempts = [pos.fill_size * f for f in
                                 [1.00, 0.99, 0.98, 0.97, 0.96, 0.95, 0.94, 0.93, 0.92]]

            for attempt_i, raw_size in enumerate(size_attempts):
                sell_size = round(raw_size, 6)

                order = Order(
                    token_id=token_id,
                    price=tp_price,
                    size=sell_size,
                    side="SELL",
                    funder=self.bot_config.safe_address,
                    fee_rate_bps=fee_rate_bps,
                    signature_type=self.bot_config.clob.signature_type,
                    neg_risk=market.neg_risk,
                    tick_size=market.tick_size,
                )
                signed = self.signer.sign_order(order)

                try:
                    response = await asyncio.to_thread(
                        self.clob.post_order, signed, "GTC"
                    )
                except Exception as exc:
                    err = str(exc).lower()
                    if ("balance" in err or "allowance" in err) \
                            and attempt_i < len(size_attempts) - 1:
                        log(f"GTC retry #{attempt_i+1}: {exc}", "info")
                        continue
                    log(f"GTC TP ERR {label}: {exc}", "warning")
                    return  # non-balance error, give up

                if response.get("success", False):
                    oid = (response.get("orderID")
                           or response.get("orderId")
                           or response.get("order_id") or "")
                    self._tp_order_id = oid
                    self._tp_gtc_active = True
                    self._last_tp_poll = time.time()
                    log(
                        f"GTC TP placed: {label} @{tp_price:.2f}"
                        f" x{sell_size:.6f} id={oid[:12]}",
                        "success",
                    )
                    return

                error = response.get("errorMsg", "unknown")
                if "balance" in error.lower() and attempt_i < len(size_attempts) - 1:
                    continue  # retry smaller
                log(f"GTC TP FAIL {label}: {error}", "warning")
                break

            # GTC placement failed — will fall back to FOK on bid detection
            log(f"GTC TP not placed — using FOK fallback", "warning")

        except Exception as exc:
            log(f"GTC TP ERR {label}: {exc}", "warning")

    async def _poll_tp_order(self) -> Optional[Dict[str, Any]]:
        """Check if the resting GTC TP order has filled."""
        if not self._tp_order_id:
            return None
        try:
            order_data = await asyncio.to_thread(
                self.clob.get_order, self._tp_order_id
            )
            if not order_data:
                return None
            if isinstance(order_data, dict) and "order" in order_data:
                order_data = order_data["order"]

            status = str(order_data.get("status", "")).lower()
            if status in ("matched", "filled", "executed", "complete", "completed"):
                size_matched = _to_float(
                    order_data.get("size_matched")
                    or order_data.get("sizeMatched") or 0
                )
                # Try to extract actual execution price from order data
                making = _to_float(
                    order_data.get("associate_trades_making")
                    or order_data.get("makingAmount") or 0
                )
                taking = _to_float(
                    order_data.get("associate_trades_taking")
                    or order_data.get("takingAmount") or 0
                )
                # For a SELL: making = shares given, taking = USDC received
                # exec_price = taking / making (USDC per share)
                exec_price = (taking / making) if making > 0 else 0.0
                return {
                    "size": size_matched if size_matched > 0 else 0,
                    "exec_price": exec_price,
                    "order_data": order_data,
                }
            return None
        except Exception as exc:
            log(f"GTC poll err: {exc}", "warning")
            return None

    async def _record_gtc_tp_fill(
        self, pos: PositionRecord, fill_info: Dict[str, Any],
    ) -> None:
        """Record a successful GTC TP fill (maker = 0% fee)."""
        tp_price = round(pos.fill_price + self.cfg.target, 2)
        fill_size = fill_info.get("size", 0) or pos.fill_size
        # Use actual execution price if available (price improvement),
        # otherwise fall back to TP limit price.
        exec_price = fill_info.get("exec_price", 0)
        if exec_price > 0:
            exit_price = exec_price
        else:
            exit_price = tp_price
        # Maker order: no taker fee on exit
        pnl = (exit_price - pos.fill_price) * fill_size
        hold_secs = int(time.time() - pos.fill_time)

        pos.exit_price = exit_price
        pos.exit_type = "TP"
        pos.exit_time = time.time()
        pos.pnl = pnl
        pos.resolved = True
        pos.won = pnl >= 0

        self._holding_position = False
        self._trade_executed = True
        self._position = None
        self._tp_order_id = None
        self._tp_gtc_active = False

        self.total_resolved += 1
        self.session_pnl += pnl
        self.total_received += exit_price * fill_size
        self.total_tp += 1
        if pnl >= 0:
            self.total_wins += 1
        else:
            self.total_losses += 1

        coin_key = pos.coin.upper()
        if coin_key in self.coin_resolved:
            self.coin_resolved[coin_key] += 1
        if pnl >= 0 and coin_key in self.coin_wins:
            self.coin_wins[coin_key] += 1
        elif pnl < 0 and coin_key in self.coin_losses:
            self.coin_losses[coin_key] += 1

        price_note = ""
        if exec_price > 0 and abs(exec_price - tp_price) > 0.005:
            price_note = f" (limit={tp_price:.2f} exec={exit_price:.4f})"
        log(
            f"GTC TP FILLED {pos.coin}-UP @{exit_price:.4f}"
            f" pnl=${pnl:+.4f} hold={hold_secs}s (maker 0% fee){price_note}",
            "success" if pnl >= 0 else "warning",
        )

        _update_trade_log_exit(
            pos.order_id, exit_price, "TP", pnl, hold_secs,
            log_file=self.log_file,
        )

    async def _cancel_tp_order(self) -> None:
        """Cancel the resting GTC TP order."""
        if not self._tp_order_id:
            return
        try:
            await asyncio.to_thread(
                self.clob.cancel_order, self._tp_order_id
            )
            log(f"GTC TP cancelled: {self._tp_order_id[:12]}", "info")
        except Exception as exc:
            # Cancel might fail if already filled — check status
            log(f"GTC cancel err: {exc}", "warning")
            filled = await self._poll_tp_order()
            if filled and self._position:
                await self._record_gtc_tp_fill(self._position, filled)
        self._tp_order_id = None
        self._tp_gtc_active = False

    # ------------------------------------------------------------------
    # Buy trade execution
    # ------------------------------------------------------------------
    async def _execute_buy(
        self,
        coin: str,
        side: str,
        buy_price: float,
        *,
        ask_price: Optional[float] = None,
        deviation: float = 0.0,
        group_mean: float = 0.0,
    ) -> None:
        """Execute a single BUY trade: cheapest divergent coin via FOK."""
        if ask_price is None:
            ask_price = buy_price

        log(
            f"$ BUY: {coin}-{side.upper()} @{ask_price:.4f}"
            f" dev={deviation:.3f} mean={group_mean:.3f}"
            f" limit={buy_price:.4f}",
            "trade",
        )

        if self.cfg.dry_run:
            # Sim fills at ask + entry penalty (conservative)
            tracker = self._record_sim_fill(
                coin, side, ask_price, deviation=deviation
            )
            if tracker:
                log(
                    f"[SIM] FILLED {coin}-{side.upper()}"
                    f" ask={ask_price:.4f}"
                    f" sim_entry={tracker.fill_price:.4f}"
                    f" (+{SIM_ENTRY_SLIP:.2f} slip)",
                    "success",
                )
                # Enter holding state for TP/timeout monitoring
                self._holding_position = True
        else:
            market = self._coin_markets.get(coin)
            if not market:
                log(f"BUY ABORT: no market for {coin}", "error")
                return
            token_id = market.token_ids.get(side, "")
            if not token_id:
                log(f"BUY ABORT: no token for {coin}-{side}", "error")
                return

            result = await self._submit_live_buy(
                coin, side, token_id, market, buy_price,
                deviation=deviation,
            )
            if result:
                # Enter holding state for TP/timeout monitoring
                self._holding_position = True
                # Wait for on-chain settlement then place GTC immediately.
                # CLOB validates on-chain balance for sells — cannot place
                # before settlement (HTTP 400 "not enough balance").
                await self._query_wallet_balance(token_id)
                await self._place_tp_gtc_sell(token_id, market)
            else:
                # FOK missed -- mark cycle as attempted
                self._trade_executed = True

    # ------------------------------------------------------------------
    # Sim fill
    # ------------------------------------------------------------------
    def _record_sim_fill(
        self, coin: str, side: str, buy_price: float,
        deviation: float = 0.0,
    ) -> Optional[OrderTracker]:
        """Create an OrderTracker, mark it filled, and record the fill.

        Applies simulation penalties to make dry-run conservative:
        - Entry slippage: fills at ask + SIM_ENTRY_SLIP (latency + depth)
        - Fee: included in cost via _record_fill
        """
        market = self._coin_markets.get(coin)
        if not market:
            return None

        token_id = market.token_ids.get(side, "")
        order_id = f"arb-{coin}-{side}-{int(time.time() * 1000)}"

        # Penalize entry: real fills are worse than best ask due to
        # ~360ms latency and orderbook depth consumption
        sim_price = round(buy_price + SIM_ENTRY_SLIP, 4)

        tracker = OrderTracker(
            coin=coin,
            side=side,
            token_id=token_id,
            order_id=order_id,
            price=buy_price,
            size=self.cfg.size,
            placed_at=time.time(),
            market_slug=market.slug,
            filled=True,
            fill_price=sim_price,
            fill_size=self.cfg.size,
            fill_time=time.time(),
        )
        self._orders[order_id] = tracker
        self._orders_placed_this_cycle += 1
        self.total_orders_placed += 1
        self._record_fill(tracker, deviation=deviation)
        return tracker

    # ------------------------------------------------------------------
    # Post-fill verification (real size & price from CLOB API)
    # ------------------------------------------------------------------
    async def _verify_fill(
        self,
        order_id: str,
        price: float,
        token_id: str = "",
    ) -> Optional[Tuple[float, float]]:
        """Query CLOB API for real fill data after a FOK fill.

        Returns:
            ``(taking, making)`` if verified — *taking* = shares matched,
            *making* = total USDC cost (shares x avg fill price).
            ``None`` if the data is unavailable.
        """
        try:
            await asyncio.sleep(0.5)

            order_data = await asyncio.to_thread(
                self.clob.get_order, order_id
            )
            if not order_data:
                return None

            if isinstance(order_data, dict) and "order" in order_data:
                order_data = order_data["order"]

            size_matched = _to_float(
                order_data.get("size_matched")
                or order_data.get("sizeMatched")
                or 0
            )

            # Real execution prices from trades
            associate_trades = order_data.get("associate_trades") or []
            trade_ids: List[str] = []
            if isinstance(associate_trades, list):
                trade_ids = [str(tid) for tid in associate_trades if tid]

            real_size = 0.0
            real_cost = 0.0
            for tid in trade_ids[:5]:
                try:
                    trade = await asyncio.to_thread(
                        self.clob.get_trade, tid
                    )
                    if not trade:
                        continue
                    makers = trade.get("maker_orders") or []
                    if makers:
                        for mo in makers:
                            mp = _to_float(mo.get("price", 0))
                            ma = _to_float(mo.get("matched_amount", 0))
                            if mp > 0 and ma > 0:
                                # Detect complement match
                                is_complement = False
                                maker_asset = mo.get("asset_id", "")
                                if maker_asset and token_id:
                                    is_complement = (maker_asset != token_id)
                                elif price > 0:
                                    d_direct = abs(mp - price)
                                    d_compl = abs(mp - (1.0 - price))
                                    is_complement = d_compl < d_direct

                                exec_price = (1.0 - mp) if is_complement else mp
                                real_size += ma
                                real_cost += ma * exec_price

                                if is_complement:
                                    log(
                                        f"[verify] complement match:"
                                        f" maker_price={mp:.4f}"
                                        f" -> exec_price={exec_price:.4f}",
                                        "info",
                                    )
                    else:
                        ts = _to_float(trade.get("size", 0))
                        tp = _to_float(trade.get("price", 0))
                        if ts > 0 and tp > 0:
                            real_size += ts
                            real_cost += ts * tp
                except Exception:
                    continue

            taking = size_matched if size_matched > 0 else real_size
            if taking <= 0:
                return None

            if real_cost > 0 and real_size > 0:
                avg_price = real_cost / real_size
                making = taking * avg_price
            else:
                making = price * taking

            avg = making / taking if taking > 0 else price
            log(
                f"[verify] size={taking:.4f} avg_price={avg:.4f} "
                f"(order sm={size_matched:.4f}, trades={len(trade_ids)})",
                "info",
            )
            return (taking, making)

        except Exception as exc:
            log(f"[verify] err: {exc}", "warning")
            return None

    # ------------------------------------------------------------------
    # Hot-path: sign + POST in a single thread call (zero event-loop block)
    # ------------------------------------------------------------------
    def _sync_sign_and_post(
        self, order: Order, order_type: str = "FOK"
    ) -> Tuple[Dict[str, Any], float, float]:
        """Sign an order and POST it to the CLOB in one synchronous call.

        Runs on the dedicated ``_clob_executor`` thread so the asyncio
        event loop is NEVER blocked by ECDSA signing or HMAC computation.

        Returns:
            (response_dict, sign_microseconds, post_microseconds)
        """
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
        """Pre-warm the HTTPS connection on the dedicated CLOB thread.

        Makes a lightweight GET /time so the TCP + TLS handshake is
        already done before the first hot-path BUY of the cycle.
        """
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                self._clob_executor,
                lambda: self.clob._request("GET", "/time"),
            )
        except Exception:
            pass  # best-effort warm-up

    # ------------------------------------------------------------------
    # Live BUY order submission (FOK)
    # ------------------------------------------------------------------
    async def _submit_live_buy(
        self,
        coin: str,
        side: str,
        token_id: str,
        market: MarketInfo,
        buy_price: float,
        deviation: float = 0.0,
    ) -> Optional[OrderTracker]:
        """Submit a FOK limit BUY to the CLOB."""
        label = f"{coin}-{side.upper()}"
        try:
            t_fee_start = time.perf_counter()
            fee_rate_bps = self._fee_rate_cache.get(token_id)
            fee_cached = fee_rate_bps is not None
            if fee_rate_bps is None:
                fee_rate_bps = await asyncio.to_thread(
                    self.clob.get_fee_rate_bps, token_id
                )
                self._fee_rate_cache[token_id] = fee_rate_bps
            t_fee_us = (time.perf_counter() - t_fee_start) * 1_000_000

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

            # Hot path: sign + POST in a single thread call on the
            # dedicated executor.  Event loop stays unblocked the whole time.
            t_hot_start = time.perf_counter()
            loop = asyncio.get_running_loop()
            response, t_sign_us, t_post_us = await loop.run_in_executor(
                self._clob_executor,
                self._sync_sign_and_post, order, "FOK",
            )
            t_hot_us = (time.perf_counter() - t_hot_start) * 1_000_000

            timing = (
                f"[fee={'hit' if fee_cached else 'MISS'}={t_fee_us:.0f}us"
                f" sign={t_sign_us:.0f}us"
                f" post={t_post_us:.0f}us"
                f" hot={t_hot_us:.0f}us]"
            )

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
            tracker = OrderTracker(
                coin=coin,
                side=side,
                token_id=token_id,
                order_id=order_id,
                price=buy_price,
                size=self.cfg.size,
                placed_at=time.time(),
                market_slug=market.slug,
            )

            if status in {"matched", "filled", "executed", "complete", "completed"}:
                # Verify fill
                t_verify_start = time.perf_counter()
                taking = 0.0
                making = 0.0
                if order_id:
                    verified = await self._verify_fill(order_id, buy_price, token_id)
                    if verified:
                        taking, making = verified
                # Fallback to POST response
                if taking <= 0:
                    taking = _to_float(response.get("takingAmount", 0))
                    making = _to_float(response.get("makingAmount", 0))
                t_verify_us = (time.perf_counter() - t_verify_start) * 1_000_000

                fp = making / max(taking, 1e-12) if taking > 0 else buy_price
                tracker.filled = True
                tracker.fill_price = fp
                tracker.fill_size = taking if taking > 0 else self.cfg.size
                tracker.fill_time = time.time()
                self._orders[order_id] = tracker
                self._orders_placed_this_cycle += 1
                self.total_orders_placed += 1
                self._record_fill(tracker, deviation=deviation)
                log(
                    f"FILLED {label}: {tracker.fill_size:.2f} @ {fp:.4f}"
                    f" {timing} [verify={t_verify_us:.0f}us]",
                    "success",
                )
                return tracker

            # FOK: if not filled, it's killed
            log(
                f"FOK KILLED {label}: order not filled (ask moved) {timing}",
                "warning",
            )
            self._orders[order_id] = tracker
            self._orders_placed_this_cycle += 1
            self.total_orders_placed += 1
            tracker.cancelled = True
            return None

        except Exception as exc:
            log(f"FOK ERR {label}: {exc}", "error")
            return None

    # ------------------------------------------------------------------
    # Position tracking
    # ------------------------------------------------------------------
    def _record_fill(
        self, tracker: OrderTracker, deviation: float = 0.0,
    ) -> None:
        self.total_fills += 1
        cost = tracker.fill_size * tracker.fill_price
        self.total_spent += cost
        self.total_shares += tracker.fill_size

        pos = PositionRecord(
            coin=tracker.coin,
            side=tracker.side,
            fill_price=tracker.fill_price,
            fill_size=tracker.fill_size,
            fill_time=tracker.fill_time,
            market_slug=tracker.market_slug,
            order_id=tracker.order_id,
            cost=cost,
            deviation=deviation,
        )
        self._current_positions.append(pos)
        self._all_positions.append(pos)
        self._position = pos

        # Append to persistent trade log
        _append_trade_log(pos, self.cfg, outcome="PENDING", log_file=self.log_file)

    # ------------------------------------------------------------------
    # Cancellation (kept for safety -- FOK shouldn't leave resting orders)
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
        """Apply win/loss outcome to positions that were held to expiry.

        Only applies to positions that haven't already been resolved via
        TP or timeout exit. Positions that exited early are already resolved.

        In dry-run mode, deducts taker fee from cost (entry side) so PnL
        reflects real-world execution.
        """
        for pos in positions:
            if pos.resolved:
                continue
            pos.resolved = True
            self.total_resolved += 1
            # In dry-run, the entry cost didn't include fees — add them now
            effective_cost = pos.cost
            if self.cfg.dry_run:
                effective_cost = pos.cost * (1.0 + SIM_FEE_RATE)
            coin_key = pos.coin.upper()
            if coin_key in self.coin_resolved:
                self.coin_resolved[coin_key] += 1
            if pos.side == winner:
                pos.won = True
                pos.payout = pos.fill_size * 1.0
                profit = pos.payout - effective_cost
                pos.pnl = profit
                pos.exit_type = "EXPIRY_WIN"
                pos.exit_time = time.time()
                pos.exit_price = 1.0
                self.total_wins += 1
                if coin_key in self.coin_wins:
                    self.coin_wins[coin_key] += 1
                self.session_pnl += profit
                self.total_received += pos.payout
                outcome_str = f"WIN +${profit:.4f}"
                log(
                    f"WIN  {pos.coin}-{pos.side.upper()} "
                    f"@{pos.fill_price:.2f} -> +${profit:.4f}",
                    "success",
                )
            else:
                pos.won = False
                pos.payout = 0.0
                pos.pnl = -effective_cost
                pos.exit_type = "EXPIRY_LOSS"
                pos.exit_time = time.time()
                pos.exit_price = 0.0
                self.total_losses += 1
                if coin_key in self.coin_losses:
                    self.coin_losses[coin_key] += 1
                self.session_pnl -= effective_cost
                outcome_str = f"LOSS -${effective_cost:.4f}"
                log(
                    f"LOSS {pos.coin}-{pos.side.upper()} "
                    f"@{pos.fill_price:.2f} -> -${effective_cost:.4f}",
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
                        entry_price_str = entry.get("entry", "0")
                        entry_price = _to_float(entry_price_str)
                        size_str = entry.get("size", "0")
                        fill_size = _to_float(size_str)
                        cost = entry_price * fill_size

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
                            self.total_received += payout
                            log(
                                f"[sweep-orphan] WIN  {coin}-{side.upper()} "
                                f"@${entry_price:.2f} "
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
                                f"@${entry_price:.2f} "
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
            # If we're holding a position, keep monitoring until TP/timeout
            if self._holding_position and self._position:
                # Still within the cycle, just past the entry window.
                # Keep monitoring TP/timeout in the watcher task.
                pass
            else:
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
        M = Colors.MAGENTA
        W = 72

        lines: list[str] = []

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
            if self._holding_position:
                elapsed = time.time() - self._position.fill_time if self._position else 0
                st = f"HOLD {int(elapsed)}s/{self.cfg.timeout}s"
            elif self._trade_executed:
                st = f"DONE {rem:.0f}s"
            else:
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
            f"  {M}{B}STAT-ARB{X}{dry}"
            f"          {ws_c}ws:{connected}/4{X}"
            f"   {countdown}"
            f"   {sc}{B}{st}{X}"
            f"   {D}{up_str}{X}"
        )
        lines.append(
            f"  {D}spread>={self.cfg.spread}  window {self.cfg.window:.0f}s"
            f"  target {self.cfg.target}  timeout {self.cfg.timeout}s"
            f"  cycle #{self.cycles_seen}{X}"
        )
        hsep()

        # --- Signal + trade status ---
        if self._holding_position and self._position:
            pos = self._position
            bid = self._best_bids[pos.coin]["up"]
            current_pnl = (bid - pos.fill_price) * pos.fill_size
            tp_price = pos.fill_price + self.cfg.target
            elapsed = time.time() - pos.fill_time
            signal_str = (
                f"{G}{B}{pos.coin}{X}  dev={pos.deviation:.3f}"
                f"  mean={self._last_group_mean:.2f}"
                f"  ask={pos.fill_price:.2f}"
            )
            trade_str = f"{G}{B}{pos.coin}-UP @{pos.fill_price:.2f}{X}"
            lines.append(
                f"  signal: {signal_str}  {D}|{X}  trade: {trade_str}"
            )
        elif self._trade_executed and self._position:
            pos = self._position
            signal_str = (
                f"{D}{pos.coin}  dev={pos.deviation:.3f}"
                f"  mean={self._last_group_mean:.2f}"
                f"  ask={pos.fill_price:.2f}{X}"
            )
            exit_tag = pos.exit_type or "?"
            pnl_val = pos.pnl or 0.0
            pnl_c = G if pnl_val >= 0 else R
            trade_str = (
                f"{D}{pos.coin}-UP @{pos.fill_price:.2f}"
                f" -> {exit_tag} {pnl_c}${pnl_val:+.2f}{X}"
            )
            lines.append(
                f"  signal: {signal_str}  {D}|{X}  trade: {trade_str}"
            )
        elif self._last_signal_coin:
            signal_str = (
                f"{Y}{B}{self._last_signal_coin}{X}"
                f"  dev={self._last_signal_dev:.3f}"
                f"  mean={self._last_group_mean:.2f}"
            )
            lines.append(
                f"  signal: {signal_str}  {D}|{X}  trade: {D}--{X}"
            )
        else:
            lines.append(
                f"  signal: {D}watching{X}  {D}|{X}  trade: {D}--{X}"
            )
        hsep()

        # --- Price grid: UP ask, DOWN ask, UP bid ---
        lines.append(
            f"  {D}{'':>5}    {'UP ask':>8}    {'DOWN ask':>8}"
            f"      {'UP bid':>8}{X}"
        )

        # Find cheapest UP ask for highlight
        cheapest_coin = None
        cheapest_ask = 1.0
        for coin in COINS:
            ua = self._best_asks[coin]["up"]
            if ua < cheapest_ask:
                cheapest_ask = ua
                cheapest_coin = coin

        for coin in COINS:
            ua = self._best_asks[coin]["up"]
            da = self._best_asks[coin]["down"]
            ub = self._best_bids[coin]["up"]

            # Highlight cheapest coin
            if coin == cheapest_coin and ua < 1.0:
                uc = f"{C}{B}"
                marker = f"  {C}<< cheapest{X}"
            else:
                uc = D
                marker = ""

            lines.append(
                f"  {B}{coin:>5}{X}"
                f"    {uc}{ua:>8.4f}{X}    {D}{da:>8.4f}{X}"
                f"      {D}{ub:>8.4f}{X}{marker}"
            )
        hsep()

        # --- Position monitor ---
        if self._holding_position and self._position:
            pos = self._position
            bid = self._best_bids[pos.coin]["up"]
            current_pnl = (bid - pos.fill_price) * pos.fill_size
            tp_price = pos.fill_price + self.cfg.target
            elapsed = time.time() - pos.fill_time
            pnl_c = G if current_pnl >= 0 else R
            lines.append(
                f"  position: {B}{pos.coin}-UP{X}"
                f" @{pos.fill_price:.2f}"
                f"  bid={C}{bid:.4f}{X}"
                f"  pnl:{pnl_c}${current_pnl:+.2f}{X}"
                f"  {'GTC' if self._tp_gtc_active else 'TP'}@{tp_price:.2f}"
                f"  hold={int(elapsed)}s/{self.cfg.timeout}s"
            )
        else:
            lines.append(f"  position: {D}none{X}")
        hsep()

        # --- Stats ---
        pnl_c = G if self.session_pnl >= 0 else R
        wr = (
            f"{(self.total_wins / self.total_resolved) * 100:.0f}%"
            if self.total_resolved > 0 else "--"
        )
        net_deployed = self.total_spent - self.total_received
        lines.append(
            f"  {B}{self.total_fills}{X} fills"
            f"   {G}{self.total_wins}W{X}/{R}{self.total_losses}L{X}"
            f"   win:{B}{wr}{X}"
            f"   pnl:{pnl_c}{B}${self.session_pnl:+.2f}{X}"
            f"   net:{D}${net_deployed:.2f}{X}"
        )

        # Exit type breakdown
        lines.append(
            f"  {D}TP:{self.total_tp}  TIMEOUT:{self.total_timeout}"
            f"  EXPIRY:{self.total_resolved - self.total_tp - self.total_timeout}{X}"
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
                    if p.exit_type == "TP":
                        pnl_val = p.pnl or 0.0
                        hold = int(p.exit_time - p.fill_time) if p.exit_time else 0
                        pnl_c_tag = G if pnl_val >= 0 else R
                        tag = f"{G}{B}TP{X} {pnl_c_tag}${pnl_val:+.2f}{X} ({hold}s)"
                    elif p.exit_type == "TIMEOUT":
                        pnl_val = p.pnl or 0.0
                        hold = int(p.exit_time - p.fill_time) if p.exit_time else 0
                        pnl_c_tag = G if pnl_val >= 0 else R
                        tag = f"{Y}TIMEOUT{X} {pnl_c_tag}${pnl_val:+.2f}{X} ({hold}s)"
                    elif p.won:
                        profit = (p.payout or 0) - p.cost
                        tag = f"{G}{B}WIN{X} {G}+${profit:.2f}{X}"
                    else:
                        tag = f"{R}LOSS{X} {R}-${p.cost:.2f}{X}"
                lines.append(
                    f"  {D}{ts}{X}"
                    f"  {B}{p.coin}{X}-{p.side.upper():<4}"
                    f"  {C}@{p.fill_price:.2f}{X} x{p.fill_size:.2f}"
                    f"  {tag}"
                )
        else:
            lines.append(f"  {D}waiting for divergence signal...{X}")
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
        # Cancel any resting GTC TP order
        if self._tp_gtc_active and self._tp_order_id:
            try:
                await self._cancel_tp_order()
            except Exception as exc:
                log(f"Cleanup GTC cancel err: {exc}", "warning")
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
        print("  STAT-ARB DIVERGENCE - SESSION SUMMARY")
        print("=" * 60)
        print(
            f"  Config:        window={self.cfg.window:.0f}s"
            f"  spread>={self.cfg.spread}  target={self.cfg.target}"
            f"  timeout={self.cfg.timeout}s"
        )
        print(
            f"                 size={self.cfg.size}"
            f"  slippage={self.cfg.slippage:.2f}"
            f"  cooldown={self.cfg.cooldown}s"
        )
        print(f"  Dry run:       {self.cfg.dry_run}")
        print(f"  Cycles seen:   {self.cycles_seen}")
        print(f"  Orders placed: {self.total_orders_placed}")
        print(f"  Total fills:   {self.total_fills}")
        print(
            f"  Resolved:      {self.total_resolved}"
            f"  ({self.total_wins}W / {self.total_losses}L)"
        )
        print(
            f"  Exit types:    TP={self.total_tp}"
            f"  TIMEOUT={self.total_timeout}"
            f"  EXPIRY={self.total_resolved - self.total_tp - self.total_timeout}"
        )
        print(f"  Total spent:   ${self.total_spent:.4f}")
        print(f"  Total received:${self.total_received:.4f}")
        print(f"  Net deployed:  ${self.total_spent - self.total_received:.4f}")
        print(f"  Session PnL:   ${self.session_pnl:+.4f}")

        if self.total_fills > 0:
            wr = (self.total_wins / self.total_resolved) * 100 if self.total_resolved > 0 else 0.0
            print(f"  Win rate:      {wr:.1f}%")
            avg_price = self.total_spent / self.total_shares if self.total_shares > 0 else 0.0
            print(f"  Avg buy price: {avg_price:.4f}")

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
                    if p.exit_type in ("TP", "TIMEOUT"):
                        pnl_val = p.pnl or 0.0
                        hold = int(p.exit_time - p.fill_time) if p.exit_time else 0
                        res = f"  {p.exit_type} ${pnl_val:+.4f} ({hold}s)"
                    elif p.won:
                        profit = (p.payout or 0) - p.cost
                        res = f"  WIN +${profit:.4f}"
                    else:
                        res = f"  LOSS -${p.cost:.4f}"
                ts = datetime.fromtimestamp(p.fill_time).strftime("%H:%M:%S")
                print(
                    f"    {ts}  {p.coin}-{p.side.upper():>4}"
                    f"  @{p.fill_price:.4f}  x{p.fill_size:.2f}"
                    f"  cost=${p.cost:.4f}  dev={p.deviation:.3f}{res}"
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
            "Stat-Arb Divergence: mean-reversion across 4 crypto 5-min markets"
        )
    )
    parser.add_argument(
        "--window", type=float, default=60.0,
        help="Seconds from market birth to allow entry (1-300, default: 60)",
    )
    parser.add_argument(
        "--spread", type=float, default=0.12,
        help="Min deviation from group mean to trigger (0.03-0.50, default: 0.12)",
    )
    parser.add_argument(
        "--target", type=float, default=0.15,
        help="Take-profit target above entry ask (0.01-0.50, default: 0.15)",
    )
    parser.add_argument(
        "--timeout", type=int, default=30,
        help="Seconds to hold before timeout exit (5-300, default: 30)",
    )
    parser.add_argument(
        "--size", type=float, default=5.0,
        help="Shares per order (min 5, default: 5)",
    )
    parser.add_argument(
        "--slippage", type=float, default=0.03,
        help="FOK slippage buffer (0.01-0.20, default: 0.03)",
    )
    parser.add_argument(
        "--cooldown", type=int, default=10,
        help="Min seconds between signals in same cycle (default: 10)",
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
        name = f"s{args.spread}_t{args.target}_w{int(args.window)}"

    cfg = StatArbConfig(
        window=args.window,
        spread=args.spread,
        target=args.target,
        timeout=args.timeout,
        size=args.size,
        slippage=args.slippage,
        cooldown=args.cooldown,
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

    strategy = StatArbStrategy(cfg, bot_config, signer, clob)
    asyncio.run(strategy.run())


if __name__ == "__main__":
    main()
