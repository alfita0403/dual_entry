# Strategy Development Log

Complete context of all trading strategies developed for Polymarket 5-minute Up/Down crypto markets. This document captures the full development history, discoveries, bugs found, and lessons learned.

---

## Project Overview

A Python trading bot for Polymarket's 5-minute Up/Down binary markets on 4 coins: BTC, ETH, SOL, XRP. Uses the Builder Program for gasless transactions (EIP-712 signing). Deployed on a Hetzner server in Ireland (`38.180.21.165`).

**Infrastructure:**
- Server: Hetzner (Ireland), user `root`, code in `~/dual_entry`, venv at `~/dual_entry/venv/bin/activate`, Python 3.10
- Local dev: `C:\Users\jorge\Desktop\dual_entry`, Python 3.14
- 4 coins: BTC, ETH, SOL, XRP — all 5-minute markets via MarketManager
- WebSocket connections for real-time orderbook data (4 simultaneous)
- Gamma API for market resolution (closed status + outcomePrices)

---

## Strategies Developed

### 1. Signal Hunter (`strategies/btc_signal.py`) — PRIMARY

**Thesis:** BTC leads, followers lag. When BTC's ask on one side hits a high-confidence threshold, the cheaper follower coins haven't adjusted yet. Buy the cheapest follower on the same side for positive expected value.

**Mechanism:**
- Signal coin: BTC only (never traded)
- Signal condition: BTC UP or DOWN ask >= `--price` threshold (e.g., 0.80)
- Action: Buy the CHEAPEST follower (ETH/SOL/XRP) on the SAME side
- Follower must be cheaper than BTC on the signal side
- Order type: FOK (Fill Or Kill) with slippage buffer
- One trade per 5-minute cycle — no retries after first attempt
- `buy_limit = min(ask + slippage, btc_ask - 0.01)` — never pay within 1c of BTC

**CLI:**
```bash
python strategies/btc_signal.py --window 60 --price 0.80 --size 5 --slippage 0.03
python strategies/btc_signal.py --dry-run --price 0.90 --window 30 --name "p90_w30"
```

**Flags:** `--window` (1-300s, default 60), `--price` (0.51-0.99, default 0.80), `--size` (min 5), `--slippage` (0.01-0.20, default 0.03), `--dry-run`, `--name`, `--market-check-interval`

### 2. Cheap Quote (`strategies/cheap_quote.py`)

Buys the cheapest available quote across all coins/sides. Simpler strategy, less directional thesis.

### 3. Correlation Hunter (`strategies/correlation.py`)

Cross-coin correlation strategy. **DO NOT MODIFY** — kept as reference.

---

## Signal Hunter Development History

### Commit Timeline (oldest to newest)

| Commit | Description |
|--------|-------------|
| `74667f0` | Initial Signal Hunter: BTC-guided FOK buys on cheapest follower |
| `74d3603` | Fix stale-data guard in cheap_quote, fix signal hunter fill price and follower<BTC check |
| `4d9ae75` | Audit fix: stale-data guard AND→OR, defensive TUI guard for _bought_price |
| `63c2d01` | Per-side stale guard so BTC UP signal fires without waiting for DOWN data |
| `d95a84d` | Verify real fill price/size via CLOB order+trades API after FOK fill |
| `9df319b` | Slippage buffer + one-attempt FOK, remove fill_size cap |
| `db5f434` | Event-driven execution (<1ms latency) instead of 500ms polling loop |
| `19e97aa` | Pre-fetch fee rates at cycle start to eliminate HTTP blocking in hot path |
| `d21a1af` | Hot path micro-optimizations: cache signer/builder, index-0 ask, FOK timeout 5s |
| `cc1a6c5` | Instrument hot path with perf_counter microsecond timing |
| `915fa2e` | Add e2e timing from Polymarket server timestamp to POST response |
| `3c80574` | Always verify fill via CLOB API (POST takingAmount unreliable) |
| `029791c` | Fix avg buy price across restarts by tracking total_shares persistently |
| `938b12c` | Fix verify_fill complement match: maker_price was DOWN price, real cost = 1 - maker_price |

---

## Discoveries & Bugs Found

### Discovery 1: POST /order response is UNRELIABLE

**Observed:** POST returned `takingAmount=5.65` but Polymarket actually filled `5.57` shares.

**Fix:** `_verify_fill` is called ALWAYS after every fill. It queries:
- `GET /data/order/{id}` → `size_matched` (real shares)
- `GET /data/trades` → `maker_orders[].price` × `matched_amount` (real execution price)

POST data is only used as fallback if verify fails.

### Discovery 2: Complement match pricing (CRITICAL)

**Observed:** Bot reported fill at $0.09 when Polymarket charged $0.91. Cost showed $0.45 instead of real $4.59.

**Root cause:** In binary markets, a BUY UP order can match against a BUY DOWN order (complement/mint match). When this happens, `maker_orders[].price` returns the DOWN price (0.09), not the UP price (0.91). The real cost per share is `1 - maker_price`.

**Fix (commit `938b12c`):**
1. Compare `maker_orders[].asset_id` with our `token_id`
2. If different → complement match → execution price = `1 - maker_price`
3. If no `asset_id` available → heuristic: if `maker_price` is closer to `1 - buy_price` than to `buy_price`, it's complement

**Verification:** After fix, bot reports `exec_price=0.7200` for a trade where PM shows avg 72¢. Correct.

### Discovery 3: CLOB API field names

- `GET /data/order/{id}`: `size_matched`, `original_size`, `price` (limit), `associate_trades` (trade IDs)
- `GET /data/trades`: `price` (taker limit price — NOT execution price!), `size`, `maker_orders[].price` (maker's order price — may be complement!), `maker_orders[].matched_amount`, `maker_orders[].asset_id` (maker's token)

### Discovery 4: `sign_order` was ~67ms on server (Python 3.10)

Creating `ClobSigner` + `ClobOrderBuilder` each call was expensive. Fixed by caching both in `OrderSigner.__init__` and reusing via `_builder_cache` keyed by `(sig_type, funder)`. After fix: ~17ms.

### Discovery 5: Fee rate HTTP in hot path was ~57ms

`get_fee_rate_bps` was called on-demand during order submission. Fixed by pre-fetching all token fees as background tasks at cycle start (`_prefetch_fee`). After fix: ~1us (cache hit).

### Discovery 6: HTTP POST to CLOB is ~340ms from Ireland server

This is pure network latency and is irreducible in Python from the Ireland server. The total observed latency evolution:
- Before optimizations: ~457ms (fee MISS + slow sign + POST)
- After optimizations: ~361ms (fee hit + cached sign + POST)
- POST dominates at ~340ms — would need co-location or different HTTP library to reduce further

### Discovery 7: Polymarket WS `timestamp` field

Stored as `int` in `OrderbookSnapshot.timestamp`. May be seconds or milliseconds — code auto-detects (`>1e12` = millis, convert to seconds). Used to compute true E2E latency from PM's server clock.

### Discovery 8: Per-side stale guard bug

Old guard `if btc_up_ask == 1.0 or btc_down_ask == 1.0: continue` blocked ALL trading until BOTH BTC sides had real WS data. Fixed to per-side `!= 1.0` check — signal fires as soon as that specific side has data.

### Discovery 9: FOK retry loop destroyed edge

When FOK failed at exact ask, bot retried on next loop iteration at higher ask. Observed: filled at $0.81 instead of $0.60. Fixed: one-attempt-only + slippage buffer. `_trade_executed = True` set unconditionally after attempt.

### Discovery 10: `fill_size` was incorrectly capped

`min(taking, cfg.size)` was wrong — PM can fill slightly more than requested due to fee mechanics. Cap removed.

### Discovery 11: `OrderbookSnapshot.asks` are pre-sorted

`from_message()` sorts asks ascending. Using `asks[0].price` instead of `min()` saves iterating all levels.

### Discovery 12: Shares discrepancy (bot vs PM UI)

Bot shows `size_matched` from API (gross shares before fee). PM UI shows net shares after taker fee deduction. Difference is small (~1%) and equals the taker fee in shares. Both are "correct" from their perspective.

---

## Performance Optimizations (Hot Path)

### Before (observed on server, commit ~`d95a84d`):
```
fee:   56,618 us  (57ms)  — HTTP call to /fee-rate
sign:  67,004 us  (67ms)  — creating ClobSigner + ClobOrderBuilder each call
post: 333,624 us (334ms)  — HTTP POST to CLOB (network)
total: 457,410 us (457ms)
```

### After (observed on server, commit `938b12c`):
```
fee:        1 us  ( 0ms)  — cache hit (pre-fetched at cycle start)
sign:  17,156 us  (17ms)  — cached signer + builder
post: 343,527 us (344ms)  — HTTP POST to CLOB (network, irreducible)
total: 360,806 us (361ms)
```

### Optimizations applied:
1. **Event-driven execution**: `asyncio.Event` wakes trading loop on WS tick (<1ms) instead of `sleep(0.5)` (500ms)
2. **Fee rate pre-fetch**: Background tasks at cycle start, cache hit on hot path
3. **Signer/builder cache**: `ClobSigner` in `__init__`, `ClobOrderBuilder` in `_builder_cache` dict
4. **`asks[0].price`**: O(1) index vs `min()` over all levels
5. **FOK timeout**: `timeout=5s`, `retry_count=1` (temporarily set, restored after POST)
6. **Microsecond timing**: `time.perf_counter()` at every stage for instrumentation
7. **E2E timing**: Polymarket server timestamp → POST completion for real-world latency

### Signal detection latency:
```
detect: ~23-42 us  — from WS tick to signal fire
ws_lag: ~50-170ms  — age of PM's tick when we process it
```

---

## Architecture of Signal Hunter

### Hot Path (signal → order):
```
WS tick arrives
  → _on_book_update (O(1): asks[0].price, dict set, event.set())
  → _book_event wakes _watch_and_trade (<1ms)
  → Signal detection (dict lookups, ~42us)
  → Slippage calc (O(1))
  → _execute_signal_trade
    → _submit_live_order
      → Fee cache lookup (O(1), ~1us)
      → sign_order (cached builder, ~17ms)
      → POST /order via asyncio.to_thread (~340ms)
      → _verify_fill (after POST, ~600ms, doesn't affect e2e)
```

### Key Data Structures:
- `_best_asks: Dict[coin, Dict[side, float]]` — latest best ask per coin/side
- `_book_pm_ts: Dict[coin, Dict[side, float]]` — Polymarket server timestamp per book update
- `_book_event: asyncio.Event` — wakes trading loop on any book tick
- `_fee_rate_cache: Dict[token_id, int]` — pre-fetched fee rates
- `_builder_cache: Dict[tuple, ClobOrderBuilder]` — cached order builders in signer

### Cycle Lifecycle:
```
WAITING_MARKET → ACTIVE (new 5m market detected, window starts)
                   ↓ window expires
                HOLDING (positions held to resolution)
                   ↓ all markets ended
                  DONE (resolution scheduled, waiting for next cycle)
                   ↓ new market detected
                ACTIVE ...
```

### PnL Model:
- Fills only affect `total_fills`, `total_spent`, `total_shares`
- Win/Loss/PnL only change on RESOLUTION (Gamma API `closed==True`, `outcomePrices` where winner = `"1"`)
- PENDING positions have no PnL impact
- Win rate = wins / total_resolved (excludes pending)
- Periodic sweep every 2 min resolves both in-memory and log-file orphaned PENDING positions

---

## Trade Log Format

File: `signal_trades.txt` (on server, never overwritten, append-only)

```
2026-02-28 12:16:49 UTC | 2026-02-28 12:16:49 | order_id=xxx | market=sol-updown-5m-1772280900 | coin=SOL | side=UP | price=0.4600 | size=5.0549 | cost=$3.6395 | window=180s | cfg_price=0.8 | cfg_size=5 | dry_run=False | outcome=PENDING
```

Outcome is updated in-place to `WIN +$X.XXXX` or `LOSS -$X.XXXX` when resolved.

---

## Dry-Run Testing Setup

Multiple configs can run simultaneously in screen sessions:

```bash
screen -dmS btc_sig-2 bash -c 'source venv/bin/activate && python strategies/btc_signal.py --dry-run --price 0.90 --window 30 --name "p90_w30"'
screen -dmS btc_sig-3 bash -c 'source venv/bin/activate && python strategies/btc_signal.py --dry-run --price 0.85 --window 30 --name "p85_w30"'
screen -dmS btc_sig-4 bash -c 'source venv/bin/activate && python strategies/btc_signal.py --dry-run --price 0.80 --window 30 --name "p80_w30"'
screen -dmS btc_sig-5 bash -c 'source venv/bin/activate && python strategies/btc_signal.py --dry-run --price 0.75 --window 30 --name "p75_w30"'
screen -dmS btc_sig-6 bash -c 'source venv/bin/activate && python strategies/btc_signal.py --dry-run --price 0.70 --window 30 --name "p70_w30"'
```

Each generates its own log file. After 24h, compare win rates across configs to find optimal parameters before going live.

---

## Real Trading Results (Feb 28, 2026)

### Session 1 (old code, pre-optimizations):
- SOL-UP: 5.57 shares @ $0.46, cost $2.56 → **WIN** (+$2.95, +115%)
- Bot showed wrong numbers (5.65 shares, $2.60) — fixed by verify_fill

### Session 2 (with complement fix):
- XRP-DOWN: 5.20 shares @ $0.76, cost $3.86 → pending
- ETH-DOWN: 5.42 shares @ $0.72, cost $3.86 → pending

### Key Observation:
At `--price 0.80`, followers are often already expensive (0.70-0.76). Edge is thin — need >76% accuracy to profit at those levels. Higher `--price` (0.90+) with shorter `--window` (30s) may find better asymmetry where followers haven't adjusted yet.

---

## Known Limitations / Not Yet Done

1. **Taker fee not in cost calculation**: Bot shows gross shares/cost. PM shows net (after fee). Difference is ~1%. Not yet incorporated into PnL.
2. **334ms POST latency from Ireland**: Irreducible with Python `requests` via `asyncio.to_thread`. Would need co-location or async HTTP library (httpx/aiohttp).
3. **Port to Rust/C++/TypeScript**: Planned after Python version is validated with winning configs.
4. **Same optimizations not applied to cheap_quote.py/correlation.py**: Only btc_signal.py was optimized.
5. **Redeem loop**: Separate script `python scripts/redeem_last_loop.py --last 20 --interval 300` handles USDC redemption.

---

## Key Files Reference

| File | Lines | Purpose |
|------|-------|---------|
| `strategies/btc_signal.py` | ~1960 | Signal Hunter strategy (PRIMARY) |
| `strategies/cheap_quote.py` | ~1797 | Cheap Quote strategy |
| `strategies/correlation.py` | ~1771 | Correlation Hunter (DO NOT MODIFY) |
| `src/signer.py` | ~334 | EIP-712 signing with cached signer/builder |
| `src/client.py` | ~893 | CLOB + Relayer API client |
| `src/websocket_client.py` | ~786 | WS client, OrderbookSnapshot |
| `lib/market_manager.py` | ~571 | MarketManager, market discovery |
| `CLAUDE.md` | — | Claude Code instructions and project context |
| `signal_trades.txt` | — | Persistent trade log (server only) |
