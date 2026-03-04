"""
Sports market delay probe — sequential, one by one.

Tests every avenue to bypass "delayed" on marketable sports orders.
Each test runs individually with a pause between them.

On-chain bypass: IMPOSSIBLE (fillOrder is onlyOperator on CTFExchange).

Usage:
    python scripts/delay_probe.py
"""

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


# ── Market: CS2 PARIVISION vs Legacy (BO3) ────────────────────
TOKEN_PRV = "986842377446719116885910390175677023212328739601747171202293905425942261864"
TOKEN_LGC = "94357681239221786511279838648751468183436463474725725372604117147638918083180"
LABEL_PRV = "PARIVISION"
LABEL_LGC = "Legacy"
NEG_RISK = False
TICK_SIZE = "0.01"
SIZE = 5.0

# Target: BUY Legacy
TARGET_TOKEN = TOKEN_LGC
TARGET_LABEL = LABEL_LGC
TARGET_FEE = 0
COMP_TOKEN = TOKEN_PRV
COMP_LABEL = LABEL_PRV
COMP_FEE = 0


def pp(data):
    if isinstance(data, dict):
        for k, v in data.items():
            val = json.dumps(v, default=str) if isinstance(v, (dict, list)) else v
            print(f"      {k}: {val}")
    else:
        print(f"      {data}")


def read_book(clob, token_id):
    book = clob.get_order_book(token_id)
    bids = sorted(book.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
    asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
    best_bid = float(bids[0]["price"]) if bids else 0.0
    best_ask = float(asks[0]["price"]) if asks else 1.0
    bid_size = float(bids[0]["size"]) if bids else 0.0
    ask_size = float(asks[0]["size"]) if asks else 0.0
    return best_bid, best_ask, bid_size, ask_size


def sign_order_raw(signer, config, token_id, price, size, side, fee_bps, expiration=0, nonce=0):
    order = Order(
        token_id=token_id, price=price, size=size, side=side,
        funder=config.safe_address, fee_rate_bps=fee_bps,
        signature_type=config.clob.signature_type,
        neg_risk=NEG_RISK, tick_size=TICK_SIZE,
        expiration=expiration, nonce=nonce,
    )
    return signer.sign_order(order)


def post_order_raw(clob, signed, order_type, extra_body=None):
    """POST via raw HTTP to support extra fields. Returns (resp_dict, time_ms)."""
    owner = ""
    if clob.api_creds and clob.api_creds.api_key:
        owner = clob.api_creds.api_key

    body = {"order": signed, "owner": owner, "orderType": order_type}
    if extra_body:
        body.update(extra_body)

    body_json = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    headers = clob._build_headers("POST", "/order", body_json)

    t0 = time.perf_counter()
    resp_raw = clob.session.post(
        f"{clob.host}/order", headers=headers,
        data=body_json.encode("utf-8"), timeout=15,
    )
    t_ms = (time.perf_counter() - t0) * 1000

    try:
        resp = resp_raw.json()
    except Exception:
        resp = {"http_status": resp_raw.status_code, "raw": resp_raw.text[:500]}
    return resp, t_ms


def post_batch_raw(clob, orders_with_types):
    """POST /orders batch. Returns (resp, time_ms)."""
    owner = ""
    if clob.api_creds and clob.api_creds.api_key:
        owner = clob.api_creds.api_key

    payload = [{"order": s, "owner": owner, "orderType": ot} for s, ot in orders_with_types]
    body_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    headers = clob._build_headers("POST", "/orders", body_json)

    t0 = time.perf_counter()
    resp_raw = clob.session.post(
        f"{clob.host}/orders", headers=headers,
        data=body_json.encode("utf-8"), timeout=15,
    )
    t_ms = (time.perf_counter() - t0) * 1000

    try:
        resp = resp_raw.json()
    except Exception:
        resp = {"http_status": resp_raw.status_code, "raw": resp_raw.text[:500]}
    return resp, t_ms


def cancel_safe(clob, order_id):
    if order_id:
        try:
            clob.cancel_order(order_id)
        except Exception:
            pass


def run_test(test_id, description, clob, signer, config,
             token_id, price, size, side, order_type, fee_bps,
             expiration=0, nonce=0, extra_body=None, is_batch=False,
             batch_items=None):
    """Run a single test, print result, auto-cancel if delayed. Returns True if matched."""

    print(f"\n{'='*70}")
    print(f"  TEST {test_id}: {description}")
    print(f"  {side} {size}x @ ${price:.3f}  type={order_type}  token={token_id[:15]}...")
    if extra_body:
        print(f"  extra: {extra_body}")
    print(f"{'='*70}")

    if is_batch and batch_items:
        resp, t_ms = post_batch_raw(clob, batch_items)
        print(f"  Response ({t_ms:.0f}ms):")
        pp(resp)
        # Handle batch response (list)
        if isinstance(resp, list):
            for r in resp:
                oid = r.get("orderID", "")
                st = r.get("status", "")
                if st == "delayed" and oid:
                    cancel_safe(clob, oid)
                    print(f"  >>> DELAYED -> auto-cancelled {oid[:20]}...")
                elif st == "matched":
                    print(f"  *** MATCHED! {oid[:20]}...")
                elif st in ("live", "unmatched") and oid:
                    cancel_safe(clob, oid)
                    print(f"      live -> cancelled")
        input("  Press ENTER for next test...")
        return False

    signed = sign_order_raw(signer, config, token_id, price, size, side, fee_bps, expiration, nonce)
    resp, t_ms = post_order_raw(clob, signed, order_type, extra_body)

    status = resp.get("status", "")
    error_msg = resp.get("errorMsg", "")
    order_id = resp.get("orderID", "")
    display = status if status else error_msg if error_msg else "???"

    if status == "matched":
        print(f"\n  *** MATCHED! *** ({t_ms:.0f}ms)  BYPASS FOUND!")
        pp(resp)
        input("  Press ENTER for next test...")
        return True
    elif status == "delayed":
        print(f"\n  >>> DELAYED ({t_ms:.0f}ms) -> cancelling...")
        pp(resp)
        cancel_safe(clob, order_id)
        print(f"      cancelled.")
    elif status == "live":
        print(f"\n      LIVE ({t_ms:.0f}ms) -> non-marketable, cancelling...")
        pp(resp)
        cancel_safe(clob, order_id)
    elif status == "unmatched":
        print(f"\n      UNMATCHED ({t_ms:.0f}ms) -> cancelling...")
        pp(resp)
        cancel_safe(clob, order_id)
    else:
        print(f"\n      {display} ({t_ms:.0f}ms)")
        pp(resp)
        if order_id:
            cancel_safe(clob, order_id)

    input("  Press ENTER for next test...")
    return False


def main():
    global TARGET_FEE, COMP_FEE

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

    print("=" * 70)
    print("  DELAY BYPASS PROBE — CS2 PARIVISION vs Legacy")
    print("  Target: BUY Legacy")
    print("  Each test runs one at a time. ENTER to advance.")
    print("  Delayed orders auto-cancelled. No money spent unless MATCHED.")
    print("=" * 70)

    # Read books
    bid_t, ask_t, bid_sz_t, ask_sz_t = read_book(clob, TARGET_TOKEN)
    bid_c, ask_c, bid_sz_c, ask_sz_c = read_book(clob, COMP_TOKEN)
    TARGET_FEE = clob.get_fee_rate_bps(TARGET_TOKEN)
    COMP_FEE = clob.get_fee_rate_bps(COMP_TOKEN)

    print(f"\n  {TARGET_LABEL}: bid={bid_t:.3f}({bid_sz_t:.0f})  ask={ask_t:.3f}({ask_sz_t:.0f})  fee={TARGET_FEE}bps")
    print(f"  {COMP_LABEL}:  bid={bid_c:.3f}({bid_sz_c:.0f})  ask={ask_c:.3f}({ask_sz_c:.0f})  fee={COMP_FEE}bps")
    print()
    input("  Press ENTER to start tests...")

    T = run_test  # shorthand

    # ── A: BASELINE (non-marketable, should be "live") ────────
    T("A1", "GTC far below ask (baseline, expect live)",
      clob, signer, config, TARGET_TOKEN, max(round(ask_t - 0.10, 2), 0.01),
      SIZE, "BUY", "GTC", TARGET_FEE)

    # ── B: ORDER TYPES at best ask ───────────────────────────
    # Refresh book before marketable tests
    bid_t, ask_t, _, _ = read_book(clob, TARGET_TOKEN)

    T("B1", "GTC at best ask (expect delayed)",
      clob, signer, config, TARGET_TOKEN, ask_t,
      SIZE, "BUY", "GTC", TARGET_FEE)

    T("B2", "FOK at best ask",
      clob, signer, config, TARGET_TOKEN, ask_t,
      SIZE, "BUY", "FOK", TARGET_FEE)

    T("B3", "FAK at best ask",
      clob, signer, config, TARGET_TOKEN, ask_t,
      SIZE, "BUY", "FAK", TARGET_FEE)

    T("B4", "GTD at best ask (exp 90s)",
      clob, signer, config, TARGET_TOKEN, ask_t,
      SIZE, "BUY", "GTD", TARGET_FEE, expiration=int(time.time()) + 90)

    T("B5", "GTD at best ask (min exp ~65s)",
      clob, signer, config, TARGET_TOKEN, ask_t,
      SIZE, "BUY", "GTD", TARGET_FEE, expiration=int(time.time()) + 65)

    T("B6", "GTC above ask (+0.03)",
      clob, signer, config, TARGET_TOKEN, min(round(ask_t + 0.03, 2), 0.99),
      SIZE, "BUY", "GTC", TARGET_FEE)

    # ── C: SELL COMPLEMENT (same exposure, different book) ───
    bid_c, ask_c, _, _ = read_book(clob, COMP_TOKEN)

    T("C1", f"SELL {COMP_LABEL} GTC at bid (marketable)",
      clob, signer, config, COMP_TOKEN, bid_c,
      SIZE, "SELL", "GTC", COMP_FEE)

    T("C2", f"SELL {COMP_LABEL} FOK at bid",
      clob, signer, config, COMP_TOKEN, bid_c,
      SIZE, "SELL", "FOK", COMP_FEE)

    T("C3", f"SELL {COMP_LABEL} FAK at bid",
      clob, signer, config, COMP_TOKEN, bid_c,
      SIZE, "SELL", "FAK", COMP_FEE)

    T("C4", f"SELL {COMP_LABEL} GTC below bid (-0.03)",
      clob, signer, config, COMP_TOKEN, max(round(bid_c - 0.03, 2), 0.01),
      SIZE, "SELL", "GTC", COMP_FEE)

    # ── D: SIZE VARIATIONS ───────────────────────────────────
    bid_t, ask_t, _, _ = read_book(clob, TARGET_TOKEN)

    T("D1", "GTC at ask, size=1",
      clob, signer, config, TARGET_TOKEN, ask_t,
      1.0, "BUY", "GTC", TARGET_FEE)

    T("D2", "FAK at ask, size=1",
      clob, signer, config, TARGET_TOKEN, ask_t,
      1.0, "BUY", "FAK", TARGET_FEE)

    T("D3", "GTC at ask, size=0.1",
      clob, signer, config, TARGET_TOKEN, ask_t,
      0.1, "BUY", "GTC", TARGET_FEE)

    # ── E: RAW HTTP — undocumented fields ────────────────────
    bid_t, ask_t, _, _ = read_book(clob, TARGET_TOKEN)

    T("E1", "GTC at ask + postOnly=true",
      clob, signer, config, TARGET_TOKEN, ask_t,
      SIZE, "BUY", "GTC", TARGET_FEE, extra_body={"postOnly": True})

    T("E2", "GTC at ask + immediateOrCancel=true",
      clob, signer, config, TARGET_TOKEN, ask_t,
      SIZE, "BUY", "GTC", TARGET_FEE, extra_body={"immediateOrCancel": True})

    T("E3", "GTC at ask + skipDelay=true",
      clob, signer, config, TARGET_TOKEN, ask_t,
      SIZE, "BUY", "GTC", TARGET_FEE, extra_body={"skipDelay": True})

    T("E4", "GTC at ask + matchOnly=true",
      clob, signer, config, TARGET_TOKEN, ask_t,
      SIZE, "BUY", "GTC", TARGET_FEE, extra_body={"matchOnly": True})

    T("E5", "orderType='MARKET'",
      clob, signer, config, TARGET_TOKEN, ask_t,
      SIZE, "BUY", "MARKET", TARGET_FEE)

    T("E6", "orderType='IOC'",
      clob, signer, config, TARGET_TOKEN, ask_t,
      SIZE, "BUY", "IOC", TARGET_FEE)

    # ── F: EDGE CASES ────────────────────────────────────────
    bid_t, ask_t, _, _ = read_book(clob, TARGET_TOKEN)

    T("F1", "GTC at ask, nonce=42",
      clob, signer, config, TARGET_TOKEN, ask_t,
      SIZE, "BUY", "GTC", TARGET_FEE, nonce=42)

    T("F2", "GTC at ask, feeRate=0",
      clob, signer, config, TARGET_TOKEN, ask_t,
      SIZE, "BUY", "GTC", 0)

    T("F3", "GTC at ask, exp=1 (already expired)",
      clob, signer, config, TARGET_TOKEN, ask_t,
      SIZE, "BUY", "GTC", TARGET_FEE, expiration=1)

    # ── G: BATCH endpoint ────────────────────────────────────
    bid_t, ask_t, _, _ = read_book(clob, TARGET_TOKEN)

    s1 = sign_order_raw(signer, config, TARGET_TOKEN, ask_t, SIZE, "BUY", TARGET_FEE)
    T("G1", "BATCH: 1x GTC at ask",
      clob, signer, config, TARGET_TOKEN, ask_t, SIZE, "BUY", "GTC", TARGET_FEE,
      is_batch=True, batch_items=[(s1, "GTC")])

    s2 = sign_order_raw(signer, config, TARGET_TOKEN, ask_t, SIZE, "BUY", TARGET_FEE)
    T("G2", "BATCH: 1x FAK at ask",
      clob, signer, config, TARGET_TOKEN, ask_t, SIZE, "BUY", "FAK", TARGET_FEE,
      is_batch=True, batch_items=[(s2, "FAK")])

    # Batch: non-marketable + marketable together
    s3a = sign_order_raw(signer, config, TARGET_TOKEN, max(round(ask_t - 0.10, 2), 0.01), SIZE, "BUY", TARGET_FEE)
    s3b = sign_order_raw(signer, config, TARGET_TOKEN, ask_t, SIZE, "BUY", TARGET_FEE)
    T("G3", "BATCH: safe + marketable GTC together",
      clob, signer, config, TARGET_TOKEN, ask_t, SIZE, "BUY", "GTC", TARGET_FEE,
      is_batch=True, batch_items=[(s3a, "GTC"), (s3b, "GTC")])

    # ── H: BUY the other token (delay per-token?) ────────────
    bid_c, ask_c, _, _ = read_book(clob, COMP_TOKEN)

    T("H1", f"BUY {COMP_LABEL} GTC at ask",
      clob, signer, config, COMP_TOKEN, ask_c,
      SIZE, "BUY", "GTC", COMP_FEE)

    T("H2", f"BUY {COMP_LABEL} FAK at ask",
      clob, signer, config, COMP_TOKEN, ask_c,
      SIZE, "BUY", "FAK", COMP_FEE)

    print(f"\n{'='*70}")
    print("  ALL TESTS COMPLETE")
    print("  If no *** MATCHED appeared, the delay cannot be bypassed via API.")
    print("  fillOrder on-chain is onlyOperator — no contract bypass either.")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
