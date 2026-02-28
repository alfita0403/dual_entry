#!/usr/bin/env python3
"""
Redeem Last Loop - Harvests prizes from the last N markets every 5 minutes.

Only redeems the most RECENT markets (sorted by date in title).

Usage:
    python redeem_last_loop.py

    # Dry run (show positions without redeeming):
    python redeem_last_loop.py --dry-run

    # Custom interval and number of recent markets:
    python redeem_last_loop.py --interval 300 --last 5
"""

import argparse
import re
import sys
import os
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from src.redeemer import Redeemer


def parse_market_datetime(title: str) -> datetime:
    """
    Parse date from market title like:
    'Bitcoin Up or Down - February 28, 4:30AM-4:35AM ET'
    Returns datetime for sorting. Falls back to epoch if unparseable.
    """
    match = re.search(
        r"(\w+ \d{1,2}),\s*(\d{1,2}:\d{2}(?:AM|PM))", title
    )
    if not match:
        return datetime(2000, 1, 1)

    date_part = match.group(1)  # "February 28"
    time_part = match.group(2)  # "4:30AM"

    try:
        # Use current year
        dt = datetime.strptime(
            f"{date_part} 2026 {time_part}", "%B %d %Y %I:%M%p"
        )
        return dt
    except ValueError:
        return datetime(2000, 1, 1)


def run_redeem_cycle(redeemer: Redeemer, proxy_address: str, last_n: int, dry_run: bool) -> int:
    """Run one redemption cycle. Returns number of successful redemptions."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"  Redeem cycle @ {now}")
    print(f"{'='*60}")

    # Reset nonce tracking at start of each cycle
    redeemer._next_nonce = None

    try:
        positions = redeemer.get_redeemable_positions(proxy_address)
    except Exception as e:
        print(f"  ERROR fetching positions: {e}")
        return 0

    if not positions:
        print("  No redeemable positions found. Waiting...")
        return 0

    grouped = redeemer.group_by_condition(positions)

    # Sort by date parsed from title (most recent first), take last N
    def sort_key(item):
        condition_id, pos_list = item
        return parse_market_datetime(pos_list[0].title)

    sorted_markets = sorted(grouped.items(), key=sort_key, reverse=True)[:last_n]

    total_value = 0.0
    print(f"\n  {len(grouped)} redeemable total, showing last {last_n}:\n")

    for condition_id, pos_list in sorted_markets:
        pos = pos_list[0]
        value = sum(p.size for p in pos_list)
        total_value += value
        dt = parse_market_datetime(pos.title)
        print(f"    - {pos.title}")
        print(f"      Size: {value:.4f} shares (${value:.2f})")

    print(f"\n  Value to redeem: ${total_value:.2f}")

    if dry_run:
        print("\n  [DRY RUN] Skipping redemption.")
        return 0

    print("\n  Redeeming...")
    success_count = 0
    total = len(sorted_markets)

    for condition_id, pos_list in sorted_markets:
        pos = pos_list[0]

        try:
            tx_hash = redeemer.redeem_position(condition_id, pos.neg_risk)
            print(f"    OK  {pos.title}")
            print(f"        tx: {tx_hash[:30]}...")
            success_count += 1
        except Exception as e:
            error_msg = str(e)
            if "execution reverted" in error_msg.lower():
                print(f"    --  {pos.title} (already redeemed or not ready)")
            else:
                print(f"    ERR {pos.title}: {error_msg[:100]}")

    print(f"\n  Cycle done: {success_count}/{total} redeemed")
    return success_count


def main():
    parser = argparse.ArgumentParser(
        description="Loop that redeems last N market positions every X minutes"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show positions without redeeming, then exit",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Seconds between cycles (default: 300 = 5 minutes)",
    )
    parser.add_argument(
        "--last",
        type=int,
        default=5,
        help="Number of most recent markets to redeem (default: 5)",
    )
    parser.add_argument(
        "--rpc-url",
        type=str,
        default=None,
        help="Polygon RPC URL",
    )
    parser.add_argument(
        "--proxy-address",
        type=str,
        default=None,
        help="Proxy wallet address",
    )
    parser.add_argument(
        "--private-key",
        type=str,
        default=None,
        help="Private key",
    )

    args = parser.parse_args()

    private_key = args.private_key or os.environ.get("POLY_PRIVATE_KEY", "")
    proxy_address = args.proxy_address or os.environ.get("POLY_SAFE_ADDRESS", "")
    rpc_url = args.rpc_url or os.environ.get("POLY_RPC_URL", "https://polygon-rpc.com")

    if not private_key:
        print("ERROR: POLY_PRIVATE_KEY is required (set env or --private-key)")
        sys.exit(1)
    if not proxy_address:
        print("ERROR: POLY_SAFE_ADDRESS is required (set env or --proxy-address)")
        sys.exit(1)

    print(f"Redeem Last Loop")
    print(f"  Proxy:    {proxy_address}")
    print(f"  RPC:      {rpc_url}")
    print(f"  Interval: {args.interval}s ({args.interval // 60}m)")
    print(f"  Last N:   {args.last} most recent markets")

    try:
        redeemer = Redeemer(
            private_key=private_key,
            proxy_address=proxy_address,
            rpc_url=rpc_url,
        )
        print(f"  Status:   Connected to Polygon")
    except Exception as e:
        print(f"ERROR: Failed to initialize redeemer: {e}")
        sys.exit(1)

    if args.dry_run:
        run_redeem_cycle(redeemer, proxy_address, args.last, dry_run=True)
        return

    total_redeemed = 0
    cycle_count = 0

    print(f"\n  Starting loop (Ctrl+C to stop)...")

    try:
        while True:
            cycle_count += 1
            redeemed = run_redeem_cycle(redeemer, proxy_address, args.last, dry_run=False)
            total_redeemed += redeemed

            minutes = args.interval // 60
            seconds = args.interval % 60
            wait_str = f"{minutes}m{seconds}s" if seconds else f"{minutes}m"
            print(f"\n  Next cycle in {wait_str}... (total redeemed: {total_redeemed})")

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print(f"\n\nStopped. {cycle_count} cycles, {total_redeemed} total redemptions.")


if __name__ == "__main__":
    main()
