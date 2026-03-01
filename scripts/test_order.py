"""
Quick diagnostic: derive API key and try a tiny FOK order.
Tests the full auth chain: L1 -> L2 -> HMAC -> POST /order.

Usage:
    python scripts/test_order.py
    python scripts/test_order.py --dry  # just test auth, don't place order
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from src.config import Config
from src.signer import Order, OrderSigner
from src.client import ClobClient
from src.gamma_client import GammaClient


def main():
    parser = argparse.ArgumentParser(description="Test CLOB auth + order submission")
    parser.add_argument("--dry", action="store_true", help="Only test auth, don't place order")
    args = parser.parse_args()

    config = Config.from_env()
    private_key = os.environ.get("POLY_PRIVATE_KEY", "")
    if not private_key:
        print("ERROR: POLY_PRIVATE_KEY not set")
        return

    signer = OrderSigner(private_key, chain_id=config.clob.chain_id)
    print(f"EOA:    {signer.address}")
    print(f"Proxy:  {config.safe_address}")
    print(f"Chain:  {config.clob.chain_id}")
    print(f"Host:   {config.clob.host}")
    print()

    clob = ClobClient(
        host=config.clob.host,
        chain_id=config.clob.chain_id,
        signature_type=config.clob.signature_type,
        funder=config.safe_address,
        signer_address=signer.address,
        builder_creds=config.builder,
    )

    # Step 1: Check server time vs local time
    print("--- Clock check ---")
    try:
        server_time = clob._request("GET", "/time")
        local_time = int(time.time())
        print(f"Server time: {server_time}")
        print(f"Local time:  {local_time}")
        if isinstance(server_time, (int, float)):
            drift = abs(local_time - int(server_time))
            print(f"Drift:       {drift}s {'OK' if drift < 5 else 'WARNING - too much drift!'}")
    except Exception as e:
        print(f"GET /time failed: {e}")
    print()

    # Step 2: Derive API key (NOT create - to avoid invalidating other bots)
    print("--- API key derivation ---")
    try:
        creds = clob.derive_api_key(signer, nonce=0)
        clob.set_api_creds(creds)
        print(f"API key:    {creds.api_key[:16]}...")
        print(f"Passphrase: {creds.passphrase[:8]}...")
        print("Derive OK")
    except Exception as e:
        print(f"derive_api_key failed: {e}")
        print("Trying create_api_key instead...")
        try:
            creds = clob.create_api_key(signer, nonce=0)
            clob.set_api_creds(creds)
            print(f"API key:    {creds.api_key[:16]}...")
            print("Create OK (NOTE: this invalidated any previous key!)")
        except Exception as e2:
            print(f"create_api_key also failed: {e2}")
            return
    print()

    if args.dry:
        print("--- Dry mode: skipping order test ---")
        print("Auth is working!")
        return

    # Step 3: Find an active market and try a tiny order
    print("--- Finding active market ---")
    gamma = GammaClient()
    market = None
    for coin in ["BTC", "ETH", "SOL", "XRP"]:
        try:
            m = gamma.get_current_5m_market(coin)
            if m:
                market = m
                print(f"Found: {coin} -> {m.get('slug', '?')}")
                break
        except Exception as e:
            print(f"  {coin}: {e}")
    if not market:
        print("No active 5m markets found")
        return
    slug = market.get("slug", "?")
    raw_tokens = market.get("clobTokenIds", "[]")
    tokens = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
    print(f"Market: {slug}")
    print(f"Tokens: {tokens[0][:20]}... , {tokens[1][:20]}...")
    if len(tokens) < 2:
        print("Not enough tokens")
        return
    print()

    # Step 4: Place a tiny FOK order (will likely be killed = no risk)
    # Buy UP at $0.02 — way below market so FOK will be killed, but above min size ($1)
    token_id = tokens[0]  # UP token
    print(f"--- Submitting test FOK order (UP @ $0.02 x55, will be killed) ---")
    try:
        neg_risk = market.get("negRisk", False)
        fee_rate = clob.get_fee_rate_bps(token_id)
        print(f"Fee rate: {fee_rate} bps")

        order = Order(
            token_id=token_id,
            price=0.02,
            size=55.0,
            side="BUY",
            funder=config.safe_address,
            fee_rate_bps=fee_rate,
            signature_type=config.clob.signature_type,
            neg_risk=neg_risk,
        )

        signed = signer.sign_order(order)
        response = clob.post_order(signed, "FOK")
        print(f"Response: {response}")

        if response.get("success"):
            print("ORDER ACCEPTED (probably killed due to $0.01 price)")
        else:
            error = response.get("errorMsg", "unknown")
            print(f"ORDER REJECTED: {error}")

    except Exception as e:
        print(f"Order error: {e}")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
