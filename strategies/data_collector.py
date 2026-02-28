"""Data Collector for Polymarket 5-minute Up/Down markets.

Captures best ask and bid (UP side) for BTC, ETH, SOL, XRP every second.
Outputs to daily CSV files for statistical arbitrage analysis.

Usage:
    python strategies/data_collector.py
    python strategies/data_collector.py --early-window 150 --skip 3
    python strategies/data_collector.py --output-dir ./my_data
"""

import argparse
import asyncio
import csv
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

# ── Path setup ───────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

# Suppress noisy logs
logging.getLogger("src.websocket_client").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

from lib.market_manager import MarketManager  # noqa: E402
from src.websocket_client import OrderbookSnapshot  # noqa: E402

# ── Constants ────────────────────────────────────────────────

COINS = ["BTC", "ETH", "SOL", "XRP"]

CSV_HEADERS = [
    "timestamp",
    "cycle_start",
    "seconds_elapsed",
    "early",
    "btc_up_ask",
    "btc_up_bid",
    "eth_up_ask",
    "eth_up_bid",
    "sol_up_ask",
    "sol_up_bid",
    "xrp_up_ask",
    "xrp_up_bid",
]


# ── Config ───────────────────────────────────────────────────


@dataclass
class CollectorConfig:
    output_dir: str = "data"
    skip: int = 5  # skip first N seconds of each cycle
    early_window: int = 120  # seconds considered "early"
    interval: float = 1.0  # sampling interval in seconds


# ── Data Collector ───────────────────────────────────────────


class DataCollector:
    def __init__(self, config: CollectorConfig):
        self.config = config

        # One MarketManager per coin
        self.managers: Dict[str, MarketManager] = {}
        for coin in COINS:
            self.managers[coin] = MarketManager(
                coin=coin,
                market_check_interval=5.0,
                auto_switch_market=True,
                interval="5m",
            )

        # Price cache — UP side only
        self._best_asks: Dict[str, float] = {c: 1.0 for c in COINS}
        self._best_bids: Dict[str, float] = {c: 0.0 for c in COINS}

        # Cycle tracking
        self._cycle_start_ts: Optional[float] = None
        self._cycle_start_str: str = ""
        self._cycle_rows: int = 0

        # CSV state
        self._csv_file = None
        self._csv_writer = None
        self._current_date: str = ""
        self._total_rows_today: int = 0
        self._cycles_today: int = 0

        # Running flag
        self._running: bool = False

    # ── Callbacks ────────────────────────────────────────────

    def _on_book_update(self, coin: str, snapshot: OrderbookSnapshot) -> None:
        """Update cached prices when WS delivers a new orderbook."""
        market = self.managers[coin].current_market
        if not market:
            return

        # Only care about the UP side
        up_token = market.token_ids.get("up")
        if snapshot.asset_id == up_token:
            self._best_asks[coin] = (
                snapshot.asks[0].price if snapshot.asks else 1.0
            )
            self._best_bids[coin] = (
                snapshot.bids[0].price if snapshot.bids else 0.0
            )

    def _on_market_change(self, coin: str, old_slug: str, new_slug: str) -> None:
        """Detect new 5-minute cycle."""
        market = self.managers[coin].current_market
        if not market:
            return

        start_ts = market.start_timestamp()
        if not start_ts:
            return

        # Same cycle — nothing to do
        if start_ts == self._cycle_start_ts:
            return

        # Log end of previous cycle
        if self._cycle_start_ts is not None and self._cycle_rows > 0:
            self._log(f"Cycle ended — {self._cycle_rows} rows written")

        # New cycle
        self._cycle_start_ts = float(start_ts)
        self._cycle_start_str = datetime.fromtimestamp(
            start_ts, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
        self._cycle_rows = 0
        self._cycles_today += 1

        # Reset prices to defaults (sentinel values)
        self._best_asks = {c: 1.0 for c in COINS}
        self._best_bids = {c: 0.0 for c in COINS}

        self._log(f"New cycle started ({self._cycle_start_str})")

    # ── CSV management ───────────────────────────────────────

    def _ensure_csv(self) -> None:
        """Open/rotate CSV file based on current UTC date."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if today == self._current_date and self._csv_writer is not None:
            return

        # Close old file
        if self._csv_file is not None:
            self._log(
                f"Day ended — {self._total_rows_today} rows, "
                f"{self._cycles_today} cycles"
            )
            self._csv_file.close()

        # Ensure output directory exists
        Path(self.config.output_dir).mkdir(parents=True, exist_ok=True)

        filepath = os.path.join(self.config.output_dir, f"prices_{today}.csv")
        write_header = (
            not os.path.exists(filepath) or os.path.getsize(filepath) == 0
        )

        self._csv_file = open(filepath, "a", newline="", encoding="utf-8")
        self._csv_writer = csv.writer(self._csv_file)

        if write_header:
            self._csv_writer.writerow(CSV_HEADERS)
            self._csv_file.flush()

        self._current_date = today
        self._total_rows_today = 0
        self._cycles_today = 0

        self._log(f"Writing to {filepath}")

    def _write_row(self, elapsed: float) -> None:
        """Write one snapshot row to CSV."""
        # Skip if no real WS data yet (all asks still at sentinel)
        if all(self._best_asks[c] == 1.0 for c in COINS):
            return

        self._ensure_csv()

        elapsed_int = int(elapsed)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        early = "True" if elapsed_int <= self.config.early_window else "False"

        row = [
            now_str,
            self._cycle_start_str,
            str(elapsed_int),
            early,
        ]
        for coin in COINS:
            row.append(f"{self._best_asks[coin]:.4f}")
            row.append(f"{self._best_bids[coin]:.4f}")

        self._csv_writer.writerow(row)
        self._csv_file.flush()
        self._cycle_rows += 1
        self._total_rows_today += 1

    # ── Startup ──────────────────────────────────────────────

    async def _start_managers(self) -> None:
        """Start all 4 MarketManagers with retry."""
        for coin in COINS:
            mgr = self.managers[coin]

            # Register callbacks BEFORE start (c=coin captures loop var)
            mgr.on_book_update(
                lambda snap, c=coin: self._on_book_update(c, snap)
            )
            mgr.on_market_change(
                lambda old, new, c=coin: self._on_market_change(c, old, new)
            )

            attempts = 0
            while True:
                try:
                    started = await mgr.start()
                    if started:
                        self._log(f"{coin} manager started")
                        break
                except Exception as e:
                    self._log(f"{coin} start error: {e}")

                attempts += 1
                if attempts >= 10:
                    self._log(
                        f"{coin} failed after {attempts} attempts — "
                        f"will keep retrying in background"
                    )
                    break
                await asyncio.sleep(2)

            # If started mid-cycle, init cycle from current market
            if mgr.current_market and self._cycle_start_ts is None:
                start_ts = mgr.current_market.start_timestamp()
                if start_ts:
                    self._cycle_start_ts = float(start_ts)
                    self._cycle_start_str = datetime.fromtimestamp(
                        start_ts, tz=timezone.utc
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    self._cycles_today += 1
                    self._log(f"Joined mid-cycle ({self._cycle_start_str})")

    # ── Main loop ────────────────────────────────────────────

    async def run(self) -> None:
        """Main entry point. Runs forever collecting data."""
        self._running = True

        self._log("Starting data collector...")
        self._log(
            f"Config: skip={self.config.skip}s, "
            f"early_window={self.config.early_window}s, "
            f"interval={self.config.interval}s"
        )
        self._log(f"Output: {os.path.abspath(self.config.output_dir)}/")

        await self._start_managers()

        self._log("All managers started — collecting data")
        self._log(
            "Prices: ask/bid for UP side | "
            "1.0000/0.0000 = no data yet"
        )

        last_summary = time.time()

        while self._running:
            try:
                if self._cycle_start_ts is not None:
                    now = time.time()
                    elapsed = now - self._cycle_start_ts

                    if self.config.skip <= elapsed <= 300:
                        self._write_row(elapsed)

                # Periodic summary every 30 minutes
                if time.time() - last_summary >= 1800:
                    self._log(
                        f"Status: {self._total_rows_today} rows today | "
                        f"{self._cycles_today} cycles | "
                        f"asks=[BTC:{self._best_asks['BTC']:.2f} "
                        f"ETH:{self._best_asks['ETH']:.2f} "
                        f"SOL:{self._best_asks['SOL']:.2f} "
                        f"XRP:{self._best_asks['XRP']:.2f}]"
                    )
                    last_summary = time.time()

            except Exception as e:
                self._log(f"Error in sampling loop: {e}")

            await asyncio.sleep(self.config.interval)

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False
        self._log("Shutting down...")

        for coin in COINS:
            try:
                await self.managers[coin].stop()
            except Exception:
                pass

        if self._csv_file:
            self._csv_file.close()

        self._log(f"Stopped. {self._total_rows_today} rows written today.")

    # ── Logging ──────────────────────────────────────────────

    @staticmethod
    def _log(msg: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)


# ── CLI ──────────────────────────────────────────────────────


def parse_args() -> CollectorConfig:
    parser = argparse.ArgumentParser(
        description="Collect 5-min market price snapshots for stat-arb analysis"
    )
    parser.add_argument(
        "--output-dir",
        default="data",
        help="Directory for CSV files (default: data/)",
    )
    parser.add_argument(
        "--skip",
        type=int,
        default=5,
        help="Skip first N seconds of each cycle (default: 5)",
    )
    parser.add_argument(
        "--early-window",
        type=int,
        default=120,
        help="Seconds considered 'early' for boolean flag (default: 120)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Sampling interval in seconds (default: 1.0)",
    )

    args = parser.parse_args()
    return CollectorConfig(
        output_dir=args.output_dir,
        skip=args.skip,
        early_window=args.early_window,
        interval=args.interval,
    )


# ── Entry point ──────────────────────────────────────────────


async def _main():
    config = parse_args()
    collector = DataCollector(config)

    try:
        await collector.run()
    except KeyboardInterrupt:
        pass
    finally:
        await collector.stop()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\n[--:--:--] Interrupted by user.", flush=True)
