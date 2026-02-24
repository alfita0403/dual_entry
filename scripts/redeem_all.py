#!/usr/bin/env python3
"""
Redeem All Script - Redeem all resolved Polymarket positions

Usage:
    python scripts/redeem_all.py

    # Or with custom RPC:
    python scripts/redeem_all.py --rpc-url https://polygon-rpc.com

    # Dry run (just show positions without redeeming):
    python scripts/redeem_all.py --dry-run
"""

import argparse
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from src.redeemer import Redeemer, create_redeemer_from_env


def main():
    parser = argparse.ArgumentParser(
        description="Redeem all resolved Polymarket positions"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show positions without redeeming",
    )
    parser.add_argument(
        "--rpc-url",
        type=str,
        default=None,
        help="Polygon RPC URL (default: POLY_RPC_URL env or https://polygon-rpc.com)",
    )
    parser.add_argument(
        "--proxy-address",
        type=str,
        default=None,
        help="Proxy wallet address (default: POLY_SAFE_ADDRESS env)",
    )
    parser.add_argument(
        "--private-key",
        type=str,
        default=None,
        help="Private key (default: POLY_PRIVATE_KEY env)",
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

    print(f"Proxy address: {proxy_address}")
    print(f"RPC URL: {rpc_url}")
    print()

    try:
        redeemer = Redeemer(
            private_key=private_key,
            proxy_address=proxy_address,
            rpc_url=rpc_url,
        )
    except Exception as e:
        print(f"ERROR: Failed to initialize redeemer: {e}")
        sys.exit(1)

    print("Fetching redeemable positions...")
    positions = redeemer.get_redeemable_positions(proxy_address)

    if not positions:
        print("No redeemable positions found.")
        return

    print(f"Found {len(positions)} redeemable positions:")
    print()

    grouped = redeemer.group_by_condition(positions)
    total_value = 0.0

    for condition_id, pos_list in grouped.items():
        pos = pos_list[0]
        value = sum(p.size for p in pos_list)
        total_value += value
        print(f"  - {pos.title}")
        print(f"    Condition: {condition_id[:20]}...")
        print(f"    Size: {value:.4f} shares (${value:.2f})")
        print(f"    Neg Risk: {pos.neg_risk}")
        print(f"    Slug: {pos.slug}")
        print()

    print(f"Total value: ${total_value:.2f}")
    print()

    if args.dry_run:
        print("Dry run mode - not redeeming positions.")
        return

    print("Starting redemption...")
    print()

    try:
        results = redeemer.redeem_all(proxy_address)

        success_count = sum(1 for r in results if r.get("status") == "success")
        error_count = sum(1 for r in results if r.get("status") == "error")

        print()
        print(f"Redemption complete: {success_count} succeeded, {error_count} failed")

    except Exception as e:
        print(f"ERROR during redemption: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
