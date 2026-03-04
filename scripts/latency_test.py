"""
Latency test: measure round-trip time for limit orders on a sports market.
Places an order at --initial-price, then every 5s cancels and replaces
at +$0.01 until --price ceiling. Prints full API responses and latency breakdown.

Usage:
    python scripts/latency_test.py --price 0.10 --size 5
    python scripts/latency_test.py --initial-price 0.01 --price 0.10 --size 5
"""

import argparse
import json
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from src.config import Config
from src.client import ClobClient
from src.signer import OrderSigner, Order


# ── Market config ──────────────────────────────────────────────
# CS2: PARIVISION vs Legacy (BO3) — Match Winner
# Outcome: Legacy (LGC)
TOKEN_ID = "94357681239221786511279838648751468183436463474725725372604117147638918083180"
NEG_RISK = False
TICK_SIZE = "0.01"


def pp(label, data):
    """Pretty-print any API response."""
    print(f"\n  -- {label} --")
    if isinstance(data, dict):
        for k, v in data.items():
            print(f"    {k}: {json.dumps(v, default=str) if isinstance(v, (dict, list)) else v}")
    else:
        print(f"    {data}")


def sign_and_post(signer, clob, config, price, size, fee_rate_bps, side="BUY", order_type="GTC"):
    """Sign + POST an order. Returns (response, t_sign_ms, t_post_ms)."""
    order = Order(
        token_id=TOKEN_ID,
        price=price,
        size=size,
        side=side,
        funder=config.safe_address,
        fee_rate_bps=fee_rate_bps,
        signature_type=config.clob.signature_type,
        neg_risk=NEG_RISK,
        tick_size=TICK_SIZE,
    )

    t0 = time.perf_counter()
    signed = signer.sign_order(order)
    t_sign = (time.perf_counter() - t0) * 1000

    t1 = time.perf_counter()
    response = clob.post_order(signed, order_type)
    t_post = (time.perf_counter() - t1) * 1000

    return response, signed, t_sign, t_post


def cancel(clob, order_id):
    """Cancel an order. Returns (response, t_cancel_ms)."""
    t0 = time.perf_counter()
    resp = clob.cancel_order(order_id)
    t_cancel = (time.perf_counter() - t0) * 1000
    return resp, t_cancel


def main():
    parser = argparse.ArgumentParser(description="Latency test for Polymarket limit orders")
    parser.add_argument("--price", type=float, required=True,
                        help="Final/ceiling price (e.g. 0.10)")
    parser.add_argument("--size", type=float, required=True,
                        help="Number of shares (e.g. 5)")
    parser.add_argument("--initial-price", type=float, default=None,
                        help="Start at this price and climb +0.01 every 5s up to --price")
    args = parser.parse_args()

    use_ladder = args.initial_price is not None
    if use_ladder and args.initial_price >= args.price:
        print("ERROR: --initial-price must be less than --price")
        return

    # ── Setup ──────────────────────────────────────────────────
    config = Config.load_with_env("config.yaml")
    private_key = os.environ.get("POLY_PRIVATE_KEY", "")
    if not private_key:
        print("ERROR: POLY_PRIVATE_KEY not set")
        return

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

    print("=" * 64)
    print("  LATENCY TEST — Sports Market Order")
    print("=" * 64)
    print(f"  Market:  CS2 PARIVISION vs Legacy (BO3)")
    print(f"  Side:    SELL LGC (FOK market sell)")
    if use_ladder:
        print(f"  Ladder:  ${args.initial_price:.2f} -> ${args.price:.2f} (+$0.01 / 5s)")
    else:
        print(f"  Price:   ${args.price:.2f}")
    print(f"  Size:    {args.size} shares")
    print(f"  Token:   {TOKEN_ID[:20]}...")
    print(f"  EOA:     {signer.address}")
    print(f"  Proxy:   {config.safe_address}")
    print(f"  SigType: {config.clob.signature_type}")
    print("=" * 64)

    # ── Fee rate lookup ────────────────────────────────────────
    t0 = time.perf_counter()
    fee_rate_bps = clob.get_fee_rate_bps(TOKEN_ID)
    t_fee = (time.perf_counter() - t0) * 1000
    print(f"\n  [0] Fee rate lookup:   {fee_rate_bps} bps  ({t_fee:.1f} ms)")

    # ── Collect all latency measurements ───────────────────────
    steps = []  # list of dicts with step info

    if not use_ladder:
        # Market sell mode: FOK at low price to sell immediately
        sell_price = args.price
        t_total_start = time.perf_counter()
        resp, signed, t_sign, t_post = sign_and_post(
            signer, clob, config, sell_price, args.size, fee_rate_bps,
            side="SELL", order_type="FOK",
        )
        t_total = (time.perf_counter() - t_total_start) * 1000
        print(f"\n  [1] MARKET SELL (FOK)")
        print(f"      Sign:  {t_sign:.2f} ms")
        print(f"      POST:  {t_post:.2f} ms")
        print(f"      TOTAL: {t_total:.2f} ms")
        pp("Signed order", signed)
        pp("POST /order response", resp)
        steps.append({"price": sell_price, "sign": t_sign, "post": t_post,
                       "cancel": 0.0, "status": resp.get("status", "?")})
    else:
        # Ladder mode: climb from initial_price to price
        current_price = args.initial_price
        step_num = 0
        current_order_id = None

        while current_price <= args.price + 0.001:  # float tolerance
            step_num += 1
            price = round(current_price, 2)

            # Cancel previous order if exists
            t_cancel_ms = 0.0
            if current_order_id:
                cancel_resp, t_cancel_ms = cancel(clob, current_order_id)
                current_order_id = None
                print(f"  [{step_num}] Cancel prev: {t_cancel_ms:.2f} ms")
                pp(f"DELETE response (step {step_num})", cancel_resp)

            # Place new order
            resp, signed, t_sign, t_post = sign_and_post(
                signer, clob, config, price, args.size, fee_rate_bps,
            )
            total = t_cancel_ms + t_sign + t_post
            print(f"\n  [{step_num}] @ ${price:.2f}  sign={t_sign:.1f}ms  post={t_post:.1f}ms  cancel={t_cancel_ms:.1f}ms  total={total:.1f}ms")
            pp(f"POST response (step {step_num}, ${price:.2f})", resp)

            status = resp.get("status", "?")
            steps.append({"price": price, "sign": t_sign, "post": t_post,
                           "cancel": t_cancel_ms, "status": status})

            current_order_id = resp.get("orderID", "")

            # If this is the last step (ceiling reached), wait 10s then cancel
            if price >= args.price - 0.001:
                if current_order_id:
                    print(f"\n  Ceiling ${price:.2f} reached. Waiting 10s...")
                    time.sleep(10)
                    cancel_resp, t_cancel_ms = cancel(clob, current_order_id)
                    print(f"  [final] Cancel: {t_cancel_ms:.2f} ms")
                    pp("DELETE final response", cancel_resp)
                    steps[-1]["cancel"] = t_cancel_ms
                break

            # Wait 5s before next step
            print(f"  Waiting 5s before next step...")
            time.sleep(5)
            current_price += 0.01

    # ── Summary ────────────────────────────────────────────────
    print("\n" + "=" * 64)
    print("  LATENCY BREAKDOWN")
    print("=" * 64)
    print(f"  {'Step':>4}  {'Price':>6}  {'Sign':>9}  {'POST':>9}  {'Cancel':>9}  {'Total':>9}  Status")
    print(f"  {'----':>4}  {'------':>6}  {'---------':>9}  {'---------':>9}  {'---------':>9}  {'---------':>9}  ------")
    for i, s in enumerate(steps, 1):
        total = s["sign"] + s["post"] + s["cancel"]
        print(f"  {i:4d}  ${s['price']:.2f}  {s['sign']:8.2f}ms  {s['post']:8.2f}ms  {s['cancel']:8.2f}ms  {total:8.2f}ms  {s['status']}")

    if len(steps) > 1:
        avg_sign = sum(s["sign"] for s in steps) / len(steps)
        avg_post = sum(s["post"] for s in steps) / len(steps)
        avg_cancel = sum(s["cancel"] for s in steps if s["cancel"] > 0)
        n_cancels = sum(1 for s in steps if s["cancel"] > 0)
        avg_cancel = avg_cancel / n_cancels if n_cancels else 0
        print(f"\n  Averages:")
        print(f"    Sign:   {avg_sign:8.2f} ms")
        print(f"    POST:   {avg_post:8.2f} ms")
        if n_cancels:
            print(f"    Cancel: {avg_cancel:8.2f} ms  ({n_cancels} samples)")

    print(f"\n  Fee rate lookup: {t_fee:.2f} ms")
    print("=" * 64)


if __name__ == "__main__":
    main()
