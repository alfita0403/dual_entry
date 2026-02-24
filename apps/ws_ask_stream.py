#!/usr/bin/env python3
"""
WebSocket Ask Stream Viewer

Prints best ask updates from Polymarket CLOB WebSocket in real time.
Useful to compare terminal stream vs browser UI.

Usage:
    python apps/ws_ask_stream.py --coin BTC --interval 5m
    python apps/ws_ask_stream.py --coin BTC --interval 5m --throttle-ms 0
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.market_manager import MarketManager


def ts_now() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Real-time best ask stream from CLOB WebSocket"
    )
    parser.add_argument(
        "--coin", type=str, default="BTC", choices=["BTC", "ETH", "SOL", "XRP"]
    )
    parser.add_argument("--interval", type=str, default="5m", choices=["5m", "15m"])
    parser.add_argument(
        "--throttle-ms",
        type=int,
        default=0,
        help="Minimum ms between printed lines (0 = every tick)",
    )
    args = parser.parse_args()

    logging.getLogger("src.websocket_client").setLevel(logging.WARNING)

    manager = MarketManager(
        coin=args.coin,
        interval=args.interval,
        market_check_interval=3.0,
        auto_switch_market=True,
    )

    last_print = 0.0

    @manager.on_market_change
    def on_market_change(old_slug: str, new_slug: str):  # pyright: ignore[reportUnusedFunction]
        print(f"\n[{ts_now()}] MARKET CHANGE: {old_slug} -> {new_slug}", flush=True)

    @manager.on_connect
    def on_connect():  # pyright: ignore[reportUnusedFunction]
        print(f"[{ts_now()}] WS connected", flush=True)

    @manager.on_disconnect
    def on_disconnect():  # pyright: ignore[reportUnusedFunction]
        print(f"[{ts_now()}] WS disconnected", flush=True)

    @manager.on_book_update
    async def on_book(snapshot):  # pyright: ignore[reportUnusedFunction]
        nonlocal last_print

        now = time.time()
        if args.throttle_ms > 0 and (now - last_print) * 1000 < args.throttle_ms:
            return

        market = manager.current_market
        if not market:
            return

        side = "?"
        for s, token_id in market.token_ids.items():
            if token_id == snapshot.asset_id:
                side = s.upper()
                break

        # Ignore ticks from assets that don't match current market token IDs.
        # These can appear briefly right after market subscription switches.
        if side == "?":
            return

        last_print = now
        print(
            f"[{ts_now()}] slug={market.slug} side={side} "
            f"best_bid={snapshot.best_bid:.4f} best_ask={snapshot.best_ask:.4f} "
            f"mid={snapshot.mid_price:.4f} ws_ts={snapshot.timestamp}",
            flush=True,
        )

    print(f"Starting stream for {args.coin} {args.interval} (Ctrl+C to stop)")

    started = await manager.start()
    if not started:
        print("No active market found.")
        return

    await manager.wait_for_data(timeout=10.0)

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        await manager.stop()


if __name__ == "__main__":
    asyncio.run(main())
