# Research Findings — Polymarket 5-Min Up/Down Markets

> **Last updated**: 2026-03-01
> **Data**: 72,613 rows | 255 cycles | 225 resolved | ~2 days (Feb 28 + Mar 1)
> **Coins**: BTC, ETH, SOL, XRP
> **Market type**: Binary 5-minute Up/Down (ERC-1155 tokens on Polygon)

---

## Table of Contents

1. [Market Structure](#1-market-structure)
2. [Data Collection](#2-data-collection)
3. [Base Rates -- The Ground Truth](#3-base-rates--the-ground-truth)
4. [Research Methodology Evolution](#4-research-methodology-evolution)
5. [Strategies That DO NOT Work](#5-strategies-that-do-not-work)
6. [The Overfitting Problem](#6-the-overfitting-problem)
7. [Live Trading Results](#7-live-trading-results)
8. [Execution & Infrastructure Findings](#8-execution--infrastructure-findings)
9. [Polymarket Mechanics Discoveries](#9-polymarket-mechanics-discoveries)
10. [Autocorrelation Analysis](#10-autocorrelation-analysis-outcome-sequence-dependence)
11. [Possible Opportunities to Investigate](#11-possible-opportunities-to-investigate)
12. [Statistical Framework](#12-statistical-framework)
13. [Data Requirements for Future Research](#13-data-requirements-for-future-research)
14. [File Reference](#14-file-reference)

---

## 1. Market Structure

Polymarket offers 5-minute binary markets on crypto price movement. Each cycle:

- A new market opens (e.g., "Will BTC go up in the next 5 minutes?")
- Two tokens are created: **UP** and **DOWN** (ERC-1155 on Polygon)
- Prices range from $0.01 to $0.99 (probability-weighted)
- At resolution: winning token pays $1, losing token pays $0
- Markets run continuously, one cycle every 5 minutes

**Key structural features:**
- Market maker sets the initial odds (typically ~52-53% for UP at open)
- Bid-ask spread is ~2-5 cents on each side
- Liquidity is thin — a few hundred dollars on each level
- UP + DOWN asks > $1.00 (this is the market maker's vig)
- High cross-coin correlation (0.788) — all 4 coins tend to resolve the same direction

---

## 2. Data Collection

**Script**: `scripts/data_collector.py` (running 24/7 on Hetzner server in Ireland)

- Samples orderbook every ~1 second via WebSocket
- Records: timestamp, cycle_start, seconds_elapsed, and for each coin: `{coin}_up_ask`, `{coin}_up_bid`, `{coin}_down_ask`, `{coin}_down_bid`
- Data files: `data/prices_2026-02-28.csv` (15,927 rows, 57 cycles) and `data/prices_2026-03-01.csv` (56,687 rows, ~198 cycles)
- Server continues collecting while we analyze locally

---

## 3. Base Rates — The Ground Truth

These are the most important numbers in this entire document. Every strategy must be evaluated against these baselines.

### 3.1 UP Win Rates (P(UP wins))

| Coin | P(UP wins) | N (resolved) | Avg ask @t=10s |
|------|-----------|-------------|----------------|
| BTC  | 45.0%     | 200         | 0.525          |
| ETH  | 42.5%     | 207         | 0.528          |
| SOL  | 43.3%     | 201         | 0.525          |
| XRP  | 43.0%     | 200         | 0.527          |

**Interpretation**: UP wins less than 50% of the time, but the market prices UP at ~52.5 cents. This means:
- The market maker charges a premium (vig) of ~7-10% over fair value
- Buying UP randomly at the ask has **negative expected value**
- You pay $0.525 for something that pays $1 only 43% of the time → E[payout] = $0.43, loss of $0.095/trade

### 3.2 Cheap Shares

| Condition | P(UP wins) | N |
|-----------|-----------|---|
| UP ask < $0.25 | 16.6% | 193 |

**Interpretation**: When UP is cheap, it's cheap for a reason. The market is almost always right. The payout asymmetry ($0.25 risk for $1 reward) does NOT compensate — you'd need >25% win rate to break even, but actual is 16.6%.

### 3.3 Cross-Coin Correlation

**Outcome correlation: 0.788** (high)

All 4 coins tend to resolve the same direction. This means:
- Diversifying across coins provides almost no diversification benefit
- A strategy that buys 2+ coins simultaneously is making correlated bets
- Cross-coin divergence (stat-arb) relies on temporary decorrelation, which does occur but is unstable

### 3.4 Implied vs Actual Probabilities

| Metric | Value |
|--------|-------|
| Market-implied P(UP) at open | ~52.5% |
| Actual P(UP) observed | ~43.5% |
| Market maker overcharge | ~9 percentage points |
| Break-even required WR at ask=0.525 | 52.5% + fees |

**The market has a systematic anti-UP bias.** DOWN wins more often than UP in our sample. This could be:
1. A genuine statistical bias in 5-min crypto returns
2. Sample-specific (only 2 days of data)
3. Market maker correctly pricing downside risk premium

---

## 4. Research Methodology Evolution

### 4.1 Research v1 (Grid Search + 6-fold CV)

**File**: `research/research_v1.py`
**Method**: Test 4,536 parameter combinations across 6 cross-validation fold schemes. Strategy must be profitable on ALL 6 folds.
**Data**: 57 cycles (Feb 28 only)
**Result**: 119 strategies survived. Best: `spread=0.12, window=60, tp=0.15, timeout=30` → $100→$114.85

**Problem discovered later**: Adding 27 more cycles of data (Feb 28 → Mar 1) caused the number of survivors to drop from 119 to far fewer. Parameters shifted completely.

### 4.2 Research v2 (Multi-Family Grid + Worst-Case Fees)

**File**: `research/research_v2.py`
**Method**: 3 strategy families (stat-arb UP, stat-arb DOWN, cheap quote), 8,000+ combinations, 14-fold CV. Added worst-case assumptions: fee=$0.02, slippage=$0.01, entry_delay=5s.
**Data**: 255 cycles
**Result**: With worst-case fees, very few strategies survived all 14 folds.

**Key problems**:
- Grid search over 8,000+ combinations with 255 data points is pure overfitting
- With Bonferroni correction for 8,000 tests, alpha per test = 0.05/8000 = 0.00000625 — nothing could survive
- The "survivors" were artifacts of multiple comparisons, not real edges
- Parameter instability between 228 and 255 cycles proved this definitively

### 4.3 Walk-Forward OOS Test

**File**: `research/walkforward.py`
**Method**: Train on Feb 28 (57 cycles), test on Mar 1 (198 cycles). Day 2 NEVER participates in parameter selection.
**Result**: Survival rate of train-profitable strategies on test data was roughly coin-flip level (~50%), confirming no real edge.

### 4.4 Research v3 (Hypothesis-Driven, Current)

**File**: `research/research_v3.py`
**Method**: Fundamental change — NO grid search. 7 distinct hypotheses with fixed parameters derived from economic theory. Each has a causal "WHY" explanation.
**Statistical framework**:
- Bootstrap 95% CI on expected edge (10,000 resamples)
- Permutation test (sign-flip) for p-values (5,000 permutations)
- Bonferroni correction: α = 0.05/7 = 0.0071
- 60/40 chronological train/test split
**Data**: 255 cycles
**Result**: **ZERO strategies reached statistical significance.**

---

## 5. Strategies That DO NOT Work

### 5.1 H1: Stat-Arb Divergence (Cross-Sectional Mean Reversion)

**Thesis**: When one coin's UP ask diverges below the group mean of all 4 coins, it's a temporary mispricing. Buy the divergent coin and take profit when it reverts.

**Parameters**: spread=0.15, window=60s, take-profit=0.15, timeout=20s

**Results**:
| Metric | Full | Train | Test (OOS) |
|--------|------|-------|------------|
| N trades | 33 | 28 | 5 |
| Edge | +$0.0106 | +$0.0200 | **-$0.0420** |
| Win rate | 42% | 43% | 40% |
| p-value | 0.2674 | — | 0.7478 |
| 95% CI | [-0.023, +0.042] | — | — |

**Verdict**: DEAD. The only strategy with a positive full-dataset edge, but:
- Edge is not statistically significant (p=0.27, need p<0.007)
- CI includes zero — could easily be negative
- OOS edge is negative (-$0.042)
- Only 33 trades in 255 cycles — far too few for confidence
- TP hit rate: 10/33 (30%), timeout: 23/33 (70%)

**Why it fails**: The divergence is real (coins do temporarily decorrelate), but the fee+slippage cost ($0.03/trade) is larger than the typical reversion profit. The edge, if it exists, is thinner than transaction costs.

### 5.2 H2: Cheap Shares (Buy Low, Hold to Expiry)

**Thesis**: When UP ask < $0.25, the asymmetric payout ($0.25 risk, $1 reward) creates positive EV if actual P(UP) > implied.

**Parameters**: max_price=0.25, hold to resolution (no TP, no timeout sell)

**Results**:
| Metric | Full | Train | Test (OOS) |
|--------|------|-------|------------|
| N trades | 85 | 54 | 31 |
| Edge | **-$0.0913** | -$0.1076 | -$0.0629 |
| Win rate | **16%** | 15% | 19% |
| p-value | 0.9870 | — | 0.8086 |

**Verdict**: DEAD. When UP is cheap, it's cheap for a very good reason. P(UP wins | ask<0.25) = 16.6%, far below the 25% needed to break even. You'd lose $0.09 per dollar bet. This is a liquidity trap — the market maker is correct.

### 5.3 H3: Early Momentum (Buy the Leader)

**Thesis**: The coin whose UP ask rose most in the first 20 seconds will continue rising (momentum continuation from informed trading).

**Parameters**: lookback=20s, min_move=0.05, take-profit=0.10, timeout=30s

**Results**:
| Metric | Full | Train | Test (OOS) |
|--------|------|-------|------------|
| N trades | 124 | 82 | 42 |
| Edge | **-$0.0590** | -$0.0605 | -$0.0562 |
| Win rate | **37%** | 38% | 36% |
| p-value | 1.0000 | — | 0.9996 |

**Verdict**: DEAD. p=1.0 — literally worse than random. Momentum does not carry in these 5-minute markets. The early price rise is already priced in by the market maker. Buying after a 5-cent move means paying inflated prices for no continuation. TP hit only 17/124 (14%).

### 5.4 H4: Early Dip Reversal (Buy the Dip)

**Thesis**: The coin whose UP ask dropped most in the first 20 seconds is oversold due to noise (a large sell, a MM pulling quotes). Buy the dip for mean reversion.

**Parameters**: lookback=20s, min_drop=0.05, take-profit=0.10, timeout=30s

**Results**:
| Metric | Full | Train | Test (OOS) |
|--------|------|-------|------------|
| N trades | 109 | 70 | 39 |
| Edge | **-$0.0602** | -$0.0544 | -$0.0705 |
| Win rate | **30%** | 34% | 23% |
| p-value | 1.0000 | — | 1.0000 |

**Verdict**: DEAD. Even worse than momentum. When price drops, it stays down. The "dip" is signal, not noise — the market maker is adjusting to real information. Buying the dip loses $0.06 per dollar bet. OOS is even worse (edge -$0.07).

### 5.5 H5: BTC Lead-Lag (BTC Leads, Alts Follow)

**Thesis**: BTC is the price leader. When BTC moves, altcoins follow with a delay. Buy the most lagging altcoin on the same side as BTC.

**Parameters**: leader_window=15s, min_leader_move=0.05, take-profit=0.10, timeout=20s

**Results**:
| Metric | Full | Train | Test (OOS) |
|--------|------|-------|------------|
| N trades | 76 | 52 | 24 |
| Edge | **-$0.0508** | -$0.0492 | -$0.0542 |
| Win rate | **33%** | 31% | 38% |
| p-value | 1.0000 | — | 0.9884 |

**Verdict**: DEAD. The lead-lag effect either doesn't exist in these markets, or the lag is shorter than our entry delay (5s on-chain settlement). By the time we can place a GTC sell, the altcoin has already adjusted. Note: this only tested BTC-UP signals (DOWN direction was skipped in this implementation).

### 5.6 H6: Bid Momentum (Order Flow Signal)

**Thesis**: Rising bids = active buying pressure. When bid rises >3 cents in 10 seconds, it signals genuine demand and predicts continued upward movement.

**Parameters**: lookback=10s, min_rise=0.03, take-profit=0.10, timeout=20s, window=90s

**Results**:
| Metric | Full | Train | Test (OOS) |
|--------|------|-------|------------|
| N trades | **253** | 151 | 102 |
| Edge | **-$0.0428** | -$0.0440 | -$0.0409 |
| Win rate | **35%** | 36% | 33% |
| p-value | 1.0000 | — | 1.0000 |

**Verdict**: DEAD. The highest-frequency strategy (253 trades) is consistently negative. Bid rises in these thin markets are noise, not signal. The market maker absorbs the buying pressure and re-centers. TP hit only 34/253 (13%). Very consistent losses across train and test — this is reliably bad.

### 5.7 H7: Previous-Cycle Continuation (Autocorrelation)

**Thesis**: If a coin went UP in the last cycle, there's residual momentum into the next cycle (trending markets, herding behavior).

**Parameters**: entry at t=5s, take-profit=0.10, timeout=30s

**Results**:
| Metric | Full | Train | Test (OOS) |
|--------|------|-------|------------|
| N trades | 131 | 74 | 56 |
| Edge | **-$0.0382** | -$0.0409 | -$0.0366 |
| Win rate | **42%** | 42% | 41% |
| p-value | 0.9998 | — | 0.9932 |

**Verdict**: DEAD. No autocorrelation in 5-minute crypto outcomes. Previous cycle results do not predict the next cycle. The "best" win rate among losers (42%) is still below the ~52.5% needed to overcome the ask spread.

### 5.8 Summary: Exit Type Analysis

| Strategy | TP hits | Timeout | Expiry | Avg hold |
|----------|---------|---------|--------|----------|
| H1: Stat-arb | 10/33 (30%) | 23/33 | 0/33 | 16s |
| H2: Cheap shares | 0/85 (0%) | 0/85 | 85/85 | 237s |
| H3: Momentum | 17/124 (14%) | 107/124 | 0/124 | 28s |
| H4: Dip reversal | 19/109 (17%) | 90/109 | 0/109 | 28s |
| H5: Lead-lag | 15/76 (20%) | 61/76 | 0/76 | 18s |
| H6: Bid momentum | 34/253 (13%) | 219/253 | 0/253 | 19s |
| H7: Autocorrelation | 43/131 (33%) | 88/131 | 0/131 | 25s |

**Pattern**: The vast majority of trades exit at timeout, not at take-profit. This means price typically moves AGAINST the position, not toward the target. The market is efficient at these timescales.

---

## 6. The Overfitting Problem

This is the most important lesson from the entire project.

### 6.1 What Happened

1. **With 57 cycles (Feb 28)**: Grid search over 4,536 parameter combos found 119 "winners" that survived 6-fold CV. Best: spread=0.12, tp=0.15, timeout=30 → $100→$114.85. We deployed this live.

2. **With 228 cycles**: 7 strategies survived 14-fold CV. "expire" strategy dominated (88% WR, N=17).

3. **With 255 cycles (27 more)**: Only 4 survived. The "expire" strategy was DEAD. Parameters shifted completely.

### 6.2 Why Grid Search Fails Here

- **Multiple comparisons**: Testing 8,000+ parameter combos on 255 data points guarantees false positives. Even at α=0.05, you'd expect 400 false positives by chance.
- **Bonferroni correction kills everything**: With 8,000 tests, corrected α = 0.05/8000 = 6.25×10⁻⁶. No strategy has enough statistical power at N=33 trades.
- **Small sample, high noise**: Binary outcomes (win/lose) with base rate ~43% and only 33-253 trades per strategy. A single trade flipping changes the win rate by 0.4-3%.
- **Parameter instability**: The "best" parameters on 57 cycles (spread=0.12) became marginal on 255 cycles (minimum viable spread≥0.16). If parameters shift with every new day of data, there is no stable edge.

### 6.3 The Correct Interpretation

The grid search approach was equivalent to running a random number generator 8,000 times and declaring "the one that rolled highest" as your strategy. It tells you about noise, not signal. Research v3 fixed this by testing only 7 pre-specified hypotheses, but even then, nothing survived.

---

## 7. Live Trading Results

### 7.1 Session 1 (6 trades)

| Metric | Value |
|--------|-------|
| Strategy | Stat-arb divergence, spread=0.10, tp=0.15, timeout=60 |
| Trades | 6 |
| W/L | 2W / 4L |
| PnL | **-$5.67** |

### 7.2 Session 2 (11 trades)

| Metric | Value |
|--------|-------|
| Strategy | Stat-arb divergence, spread=0.10, tp=0.15, timeout=60 |
| Trades | 11 |
| W/L | 4W / 7L |
| PnL | **~-$10** |
| Key losses | SOL crashed 0.50→0.14 and 0.46→0.09 during timeout hold |

### 7.3 Total Live PnL

**~-$18 across all sessions**

### 7.4 Post-Mortem

1. **spread=0.10 was too low** — later research showed minimum viable is ≥0.16, which reduces signal frequency to near-zero
2. **timeout=60 was too long** — holding for 60 seconds exposed the position to mean-reverting-against-us moves (SOL collapse)
3. **The strategy was chosen from overfit grid search results** — the parameters that "worked" on 57 cycles were not robust

---

## 8. Execution & Infrastructure Findings

### 8.1 Latency Budget (Ireland Server → Polymarket)

| Component | Before | After Optimization |
|-----------|--------|--------------------|
| Fee rate HTTP | 57ms | ~0ms (cache hit) |
| EIP-712 signing | 67ms | 17ms (cached signer) |
| HTTP POST to CLOB | 340ms | 340ms (irreducible) |
| **Total signal→order** | **457ms** | **361ms** |

The POST to CLOB is ~340ms from Ireland and is irreducible without co-location or a different HTTP library.

### 8.2 HFT Optimizations Applied

1. **Dedicated single-thread executor** for CLOB API calls (guaranteed TCP keep-alive)
2. **Combined sign + POST** in one thread call (event loop never blocked by ECDSA signing)
3. **Pre-warm TLS connection** at cycle start (no cold TLS handshake on first BUY)
4. **Pre-decoded HMAC base64 secrets** at init (eliminated per-request base64 decode)
5. **Single JSON serialization** (eliminated duplicate json.dumps in post_order)
6. **Millisecond timestamps** in logs for latency diagnosis
7. **Event-driven execution** via asyncio.Event (WS tick → signal in <1ms, not 500ms polling)

### 8.3 Best-Case Signal→Order Latency

```
WS tick age:    50-170ms (stale by the time we receive it)
Signal detect:  ~23-42μs (O(1) dict lookups)
Sign + POST:    ~360ms
Total E2E:      ~410-530ms
```

### 8.4 Implication for Strategies

At 400-500ms latency, any edge that relies on sub-second price changes is inaccessible. The market maker likely has <10ms latency. Our strategies must rely on signals that persist for at least 5+ seconds to be actionable.

---

## 9. Polymarket Mechanics Discoveries

### 9.1 Fee Structure
- **Buy fee**: ~1.5% deducted FROM SHARES (not from USDC spent)
- **Sell fee**: None
- This means you receive fewer shares than your USDC would suggest
- Net effect: ~$0.007-0.01 per $1 traded (our worst-case model uses $0.02)

### 9.2 CLOB Behavior
- **CLOB truncates sell sizes** to 2 decimal places (not 6)
- **FOK can fail** on low liquidity with error: "order couldn't be fully filled"
- **Settlement delay**: 2-5 seconds on Polygon before shares are available to sell
- **On-chain balance check**: CLOB validates `balanceOf()` for SELL orders — cannot sell before settlement
- **GTC sells can get price improvement** — sometimes sell above limit price

### 9.3 POST /order Response is UNRELIABLE

The POST response `takingAmount` field is sometimes wrong. Observed: POST returned 5.65 shares but actual fill was 5.57. **Always verify** via `GET /data/order/{id}` and `GET /data/trades`.

### 9.4 Complement Match Pricing (CRITICAL BUG FOUND)

In binary markets, a BUY UP order can match against a BUY DOWN order (complement/mint match). When this happens:
- `maker_orders[].price` returns the DOWN price (e.g., 0.09), not the UP price (0.91)
- Real cost per share = `1 - maker_price`
- Bot initially reported fills at $0.09 when actual cost was $0.91

**Fix**: Compare `maker_orders[].asset_id` with our `token_id`. If different → complement match → `execution_price = 1 - maker_price`.

### 9.5 Shares Discrepancy

Bot shows `size_matched` from API (gross shares before fee). Polymarket UI shows net shares after taker fee deduction. Difference is ~1.5%. Both are "correct" from their perspective.

---

## 10. Autocorrelation Analysis (Outcome Sequence Dependence)

**Script**: `research/autocorrelation.py`
**Question**: Do previous cycle outcomes predict the next cycle? And if so, is the edge already priced into the opening ask?
**Data**: 255 cycles, 225 resolved, per-coin + majority (3+ of 4 coins same direction)

### 10.1 Runs Test — Is the Sequence Random?

Wald-Wolfowitz runs test: fewer runs than expected = clustering/momentum, more = mean-reversion.

| Series | N | UP | DN | Runs | Expected | z | p-value | Verdict |
|--------|---|----|----|------|----------|---|---------|---------|
| BTC | 200 | 90 | 110 | 115 | 100.0 | **+2.15** | **0.032** | **Mean-reverting** |
| ETH | 207 | 88 | 119 | 109 | 102.2 | +0.97 | 0.331 | Random |
| SOL | 201 | 87 | 114 | 101 | 99.7 | +0.19 | 0.850 | Random |
| XRP | 200 | 86 | 114 | 95 | 99.0 | -0.58 | 0.559 | Random |
| MAJ | 206 | 75 | 131 | 99 | 96.4 | +0.39 | 0.694 | Random |

**Key finding**: BTC is the only coin with statistically significant non-randomness (p=0.032). It has MORE runs than expected, meaning BTC tends to **alternate** UP/DOWN more than a random coin flip would. ETH, SOL, XRP, and the majority aggregate are all consistent with independence.

**Caveat**: BTC's p=0.032 would not survive Bonferroni correction across 5 tests (corrected alpha = 0.01). Marginal at best.

### 10.2 Conditional Probabilities — P(UP_t | previous outcomes)

Two-layer analysis:
- **Layer 1**: Does the conditional probability differ from the unconditional base rate?
- **Layer 2**: Even if it does, is it already reflected in the opening ask price? (Edge = P(UP actual) - ask implied)

#### 10.2.1 BTC Conditional Probabilities

| Condition | N | P(UP) | 95% CI | p-val | Avg Ask | Edge | Note |
|-----------|---|-------|--------|-------|---------|------|------|
| Unconditional | 200 | 45.0% | [38.0%, 52.0%] | — | 0.520 | — | |
| prev 1=D | 109 | 52.3% | [43.1%, 61.5%] | 0.148 | 0.539 | -1.6pp | |
| prev 1=U | 90 | 36.7% | [26.7%, 46.7%] | 0.138 | 0.493 | -12.6pp | Overpriced |
| prev 2=DD | 51 | 56.9% | [43.1%, 70.6%] | 0.093 | 0.549 | +1.9pp | |
| prev 2=UU | 33 | 27.3% | [12.1%, 42.4%] | 0.053 | 0.472 | -19.9pp | Overpriced |
| **prev 3=DDD** | **22** | **72.7%** | [54.5%, 90.9%] | **0.010** | **0.555** | **+17.3pp** | **Potential edge** |
| prev 3=UUU | 9 | 22.2% | [0.0%, 55.6%] | 0.199 | 0.458 | -23.6pp | Overpriced |

**BTC mean-reversion pattern**: After 3 consecutive DOWNs, BTC UP wins 72.7% of the time. The market maker prices the ask at only $0.555 (implying 55.5% UP), leaving a potential +17.3pp edge. After 2 consecutive UPs, BTC UP drops to 27.3% — the market prices at $0.472 but reality is even lower.

#### 10.2.2 Other Coins — DDD Pattern Consistency

The DDD reversal pattern appears across ALL coins, not just BTC:

| Coin | P(UP | DDD) | N | Ask | Edge |
|------|-------------|---|-----|------|
| BTC | **72.7%** | 22 | 0.555 | +17.3pp |
| SOL | 60.7% | 28 | 0.526 | +8.1pp |
| XRP | 60.0% | 30 | 0.556 | +4.4pp |
| ETH | 58.1% | 31 | 0.538 | +4.2pp |
| MAJ (3+/4) | **59.5%** | 42 | 0.539 | +5.6pp |

The consistency across coins strengthens the hypothesis. This is NOT a single-coin anomaly.

#### 10.2.3 Majority (Market-Level) Conditional Probabilities

| Condition | N | P(UP) | p-val | Ask | Edge | Note |
|-----------|---|-------|-------|-----|------|------|
| Unconditional | 206 | 36.4% | — | 0.521 | — | |
| prev 2=UD | 49 | 22.4% | 0.053 | 0.516 | -29.2pp | Strong DOWN signal |
| **prev 3=DDD** | **42** | **59.5%** | **0.003** | **0.539** | **+5.6pp** | **Lowest p-value** |
| prev 3=DUD | 31 | 19.4% | 0.061 | 0.512 | -31.8pp | Strong DOWN signal |

**MAJ prev 3=DDD has the lowest p-value of all tests: 0.003.** This is the closest to surviving Bonferroni (would need p < 0.00071 for 70 tests). With more data, this could become significant.

### 10.3 Streak Analysis

After N consecutive same-direction outcomes:

#### BTC Streaks
| Streak | N | P(UP next) | Ask | Edge |
|--------|---|-----------|-----|------|
| 1+ UP | 90 | 36.7% | 0.493 | -12.6pp |
| 2+ UP | 33 | 27.3% | 0.472 | -19.9pp |
| **3+ DOWN** | **22** | **72.7%** | **0.555** | **+17.3pp** |
| 4+ DOWN | 6 | 50.0% | 0.523 | -2.3pp |

#### SOL Streaks (notable)
| Streak | N | P(UP next) | Ask | Edge |
|--------|---|-----------|-----|------|
| 3+ DOWN | 28 | 60.7% | 0.526 | +8.1pp |
| **4+ DOWN** | **11** | **72.7%** | **0.558** | **+16.9pp** |
| 5+ DOWN | 3 | 100% | 0.557 | +44.3pp |
| 4+ UP | 6 | **0.0%** | 0.522 | -52.2pp |

**Observation**: Long DOWN streaks (3+) predict UP reversal across coins. Long UP streaks predict DOWN continuation. The market maker does NOT adjust asks proportionally — the ask remains ~0.52-0.56 regardless of streak length.

### 10.4 Cross-Coin Lead-Lag (Cycle-to-Cycle)

Does coin X going DOWN in cycle t predict coin Y going UP in cycle t+1?

Top results sorted by absolute edge:

| Signal | N | P(UP next) | Base | p-val | Ask | Edge |
|--------|---|-----------|------|-------|-----|------|
| BTC UP -> ETH | 74 | 39.2% | 42.5% | 0.639 | 0.511 | -11.9pp |
| BTC UP -> SOL | 71 | 39.4% | 43.3% | 0.551 | 0.506 | -11.2pp |
| BTC DN -> ETH | 89 | 52.8% | 42.5% | 0.054 | 0.535 | -0.7pp |
| BTC DN -> SOL | 87 | 51.7% | 43.3% | 0.130 | 0.536 | -1.9pp |
| ETH DN -> SOL | 95 | 49.5% | 43.3% | 0.255 | 0.532 | -3.7pp |

**Pattern**: When BTC or ETH go DOWN, the NEXT cycle's UP probability for other coins increases to ~50-53%. However, the market also adjusts the ask upward (0.53-0.54), nearly eliminating the edge. Cross-coin lead-lag at the cycle level is close to zero after pricing.

### 10.5 Autocorrelation Summary

**Total conditional tests run**: 70
**Bonferroni-corrected alpha**: 0.00071
**Significant at nominal p<0.05**: 5
**Significant after Bonferroni**: 0

| Rank | Pattern | P(UP) | N | Ask | Edge | p-value |
|------|---------|-------|---|-----|------|---------|
| 1 | MAJ prev 3=DDD | 59.5% | 42 | 0.539 | +5.6pp | **0.003** |
| 2 | BTC prev 3=DDD | 72.7% | 22 | 0.555 | +17.3pp | **0.010** |
| 3 | XRP prev 3=DUD | 16.7% | 24 | 0.511 | -34.5pp | **0.012** |
| 4 | XRP prev 2=UD | 25.5% | 47 | 0.514 | -25.9pp | **0.018** |
| 5 | ETH prev 3=DUU | 19.0% | 21 | 0.503 | -31.2pp | **0.044** |

**CONCLUSION**: No pattern survives Bonferroni correction with 255 cycles. However, two patterns show consistent, partially-unpriced effects worth monitoring with more data:

1. **DDD reversal (BUY UP)**: After 3 consecutive DOWNs, UP wins 60-73% across all coins. The market maker only adjusts asks to ~55%, leaving +5 to +17pp of unpriced edge. Needs 200+ DDD occurrences to confirm.

2. **UD continuation (BUY DOWN)**: After UP-then-DOWN ("dead cat bounce"), next cycle is DOWN 75-78% of the time. The market maker keeps asks at ~51%, implying 49% DOWN, but reality is 75%. Potential -25 to -30pp mispricing.

**Why the market maker might not price this**: The MM likely uses a static pricing model based on real-time crypto spot volatility (from Binance/Coinbase feeds), not on Polymarket's own outcome history. If the autocorrelation is endogenous to Polymarket's microstructure, it would be invisible to a volatility-based pricer.

---

## 11. Possible Opportunities to Investigate

These are ideas that have NOT been tested or have shown partial promise. None are confirmed edges.

### 11.1 DDD Reversal Strategy (HIGHEST PRIORITY)

**Status**: Partially validated in autocorrelation analysis. Needs more data.

**Hypothesis**: After 3 consecutive DOWN resolutions for a coin (or majority of coins), buy UP on the next cycle. The market maker does not adjust asks to reflect the ~60-73% reversal probability.

**What we know**:
- P(UP | 3 consecutive DOWN) = 60-73% across all coins
- Market ask at cycle open after DDD: ~$0.53-0.56 (implies 53-56% UP)
- Unpriced edge: +5 to +17 percentage points
- MAJ DDD p-value: 0.003 (closest to Bonferroni significance)
- Frequency: ~10-15 DDD events per day (rough estimate from data)

**What's needed**:
- 200+ DDD occurrences to confirm with statistical power (currently 22-42)
- At ~10-15/day, need ~2 weeks more data
- Verify the pattern holds in train/test split with larger dataset
- Simulate with realistic fees/slippage to confirm edge survives transaction costs

**Implementation**: Simple — check last 3 resolutions for each coin before cycle opens. If all 3 were DOWN, buy UP at market open. Hold to resolution (no TP/timeout needed since the edge is in resolution probability).

### 11.2 UD Continuation / Dead Cat Bounce (HIGH PRIORITY)

**Status**: Partially validated. Needs more data.

**Hypothesis**: After UP followed by DOWN ("dead cat bounce"), the next cycle resolves DOWN 75-78% of the time. Buy DOWN token.

**What we know**:
- P(UP | prev UD) = 22-34% depending on coin (i.e., P(DOWN) = 66-78%)
- Market ask for UP: ~$0.51-0.52 (implies ~48-49% DOWN)
- Unpriced DOWN edge: +17 to +29 percentage points
- XRP prev 2=UD: p=0.018, N=47

**Implementation**: Check last 2 resolutions. If pattern is UP then DOWN, buy DOWN token at cycle open.

### 11.3 DOWN-Side Trading (HIGH PRIORITY)

**Rationale**: If UP wins only 43% of the time, DOWN wins 57%. We have only tested buying UP tokens. Buying DOWN tokens when they're cheap could be the directional bet to make.

**What to test**:
- Base rate: P(DOWN wins) = ~57% -- already above break-even if DOWN ask ~ $0.52-0.53
- Stat-arb on DOWN asks: same mean-reversion logic but on the side that resolves more often
- BTC lead-lag with DOWN signals (H5 skipped DOWN direction entirely)
- "Expensive UP" -> buy DOWN: when UP ask > $0.75, market says >75% UP chance, but actual is ~43%

**Caveat**: The market maker prices DOWN tokens too, so DOWN asks will already reflect the higher win probability. Need to verify that DOWN token pricing has the same vig structure.

### 11.4 Contrarian / Fade the Crowd

**Rationale**: The systematic anti-UP bias suggests the crowd overweights UP outcomes. A strategy that systematically bets against popular consensus might have edge.

**What to test**:
- When all 4 coins have UP ask > 0.55 (crowd bullish), buy DOWN on all
- Measure: do correlated bullish signals predict contrarian outcomes?

### 11.5 Market-Making (Provide Liquidity)

**Rationale**: Instead of taking directional bets, provide liquidity by placing limit orders on both sides. Capture the bid-ask spread without directional exposure.

**Challenges**:
- Requires managing inventory (delta-neutral positioning)
- Needs much faster execution (sub-100ms) to avoid adverse selection
- Polymarket's market maker already dominates this niche
- Our 340ms latency is probably too slow

### 11.6 Time-of-Day / Volatility Regimes

**Rationale**: Crypto markets have different behaviors at different times (US session vs Asia session, weekend vs weekday). The edge might exist only in specific regimes.

**What to test**:
- Segment data by hour-of-day, day-of-week
- Compare base rates across regimes
- Look for periods where market maker is less active (wider spreads = more opportunity)

**Caveat**: Splitting 255 cycles by time regime further reduces sample size. Need 1000+ cycles.

### 11.7 Orderbook Depth / Imbalance Signals

**Rationale**: We currently only use best ask/bid. The full orderbook depth might contain information about order flow direction.

**What to test**:
- Bid-heavy vs ask-heavy orderbook → predicts direction?
- Large resting orders ("icebergs") as directional signals
- Orderbook imbalance ratio as entry filter

**Limitation**: Current data collector only records best bid/ask. Would need to collect full depth.

### 11.8 Longer Holding Periods (TP-or-Expire on SELECT Trades)

**Rationale**: H1 (stat-arb) showed slight positive edge when averaged across all trades, but lost on timeout exits. If we could identify WHICH divergence signals are high-quality and hold only those to expiry, might capture the $1 resolution payout.

**What to test**:
- Filter stat-arb signals by divergence magnitude (spread > 0.20? 0.25?)
- Hold filtered signals to resolution instead of using timeout
- Requires much larger sample to test (N=33 is too few to sub-filter)

### 11.9 Cross-Market Arbitrage

**Rationale**: Polymarket's 5-min markets may be priced relative to external exchanges (Binance, Coinbase). If external price moves predict Polymarket resolution, we can front-run the Polymarket market maker.

**What to test**:
- Collect simultaneous Binance 1-second klines alongside Polymarket orderbook
- Measure lag between Binance move and Polymarket price adjustment
- If lag > 5 seconds (our entry delay), there's a window

**Challenge**: Requires additional data collection infrastructure. The market maker likely already does this.

### 11.10 Yield from Providing Limit Orders (Passive Strategy)

**Rationale**: Instead of crossing the spread (FOK/market orders), place limit orders at favorable prices and wait for fills. If filled at bid (e.g., $0.48 for UP), the implied probability breakeven drops to 48%, closer to actual 43% — still unprofitable, but closer.

**What to test**:
- Place GTC limit buys below current ask
- Measure fill rate and win rate of filled orders
- Compare with FOK crossing-the-spread approach

---

## 12. Statistical Framework

### 12.1 Worst-Case Trading Assumptions

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Fee | $0.02/trade | Conservative; real is ~$0.007-0.01 |
| Slippage | $0.01/trade | FOK fills 1-2 ticks above displayed ask |
| Entry delay | 5 seconds | Polygon settlement before GTC sell can be placed |
| TP exit | At limit price only | No price improvement assumed |
| Bet size | $10/trade | Matches live trading size |

### 12.2 Statistical Tests Used (Research v3)

| Test | Purpose |
|------|---------|
| Bootstrap 95% CI | Confidence interval on mean edge (10,000 resamples) |
| Permutation p-value | One-sided sign-flip test: H₀: E[pnl] ≤ 0 (5,000 permutations) |
| Bonferroni correction | Adjusted α = 0.05/7 = 0.0071 for 7 hypotheses |
| Chronological split | 60% train / 40% test, preserving time order |

### 12.3 Required Sample Size Estimates

For detecting a thin edge of $0.01/trade with binary outcomes:
- Standard deviation of PnL per trade: ~$0.15-0.25 (depending on strategy)
- Effect size d = 0.01/0.20 = 0.05
- For 80% power at α=0.007 (Bonferroni): **N ≈ 4,500 trades**
- At current signal frequency (33-253 trades per 255 cycles): **need 2,000-17,000 cycles**
- At ~288 cycles/day: **7-60 days of data collection**

If the edge is truly $0.01/trade, it would take 2+ weeks of data to detect it statistically. If the edge is zero (which the data suggests), no amount of data will find it.

---

## 13. Data Requirements for Future Research

### 13.1 Current Data Status

| Metric | Value |
|--------|-------|
| Total cycles | ~255 (growing on server) |
| Total rows | 72,613 |
| Days of collection | 2 (Feb 28-Mar 1) |
| Collection rate | ~288 cycles/day |

### 13.2 What's Needed

| Goal | Cycles needed | Days |
|------|--------------|------|
| Confirm DDD reversal pattern | ~200 DDD events (~500-1000 cycles) | ~2-4 |
| Confirm UD continuation pattern | ~200 UD events (~500 cycles) | ~2 |
| Detect $0.03 edge (moderate) | ~500 | ~2 |
| Detect $0.01 edge (thin) | ~4,500 | ~16 |
| Regime analysis (time-of-day) | ~2,000 | ~7 |
| DOWN-side strategy test | ~500 | ~2 |
| Robust walk-forward (5+ windows) | ~1,500 | ~5 |

### 13.3 Data Collector Status

- Running 24/7 on Hetzner server (`screen -r data_col`)
- Writes to `data/prices_YYYY-MM-DD.csv`
- Server is in Ireland (close to Polymarket infrastructure)
- Currently captures: best bid/ask only (no depth)

---

## 14. File Reference

### Research Scripts

| File | Purpose | Status |
|------|---------|--------|
| `research/autocorrelation.py` | Outcome sequence dependence, conditional probs, streaks | DDD reversal + UD patterns found |
| `research/research_v3.py` | Hypothesis-driven analysis, 7 strategies, bootstrap/permutation | All results negative |
| `research/research_v2.py` | Multi-family grid search, 14-fold CV, worst-case fees | SUPERSEDED by v3 |
| `research/research_v1.py` | Original grid search, 6-fold CV | SUPERSEDED |
| `research/walkforward.py` | Walk-forward OOS test (train Feb 28 / test Mar 1) | ~50% survival (no edge) |
| `research/analyze_prices.py` | Full analysis with 6 plots + console report | REFERENCE |

### Trading Strategies (live/dry-run)

| File | Purpose | Status |
|------|---------|--------|
| `strategies/stat_arb.py` | Stat-arb divergence bot | HFT-optimized, -$18 live PnL |
| `strategies/btc_signal.py` | BTC lead-lag signal bot | DO NOT MODIFY |
| `strategies/correlation.py` | Correlation hunter | DO NOT MODIFY |

### Scripts (utilities)

| File | Purpose |
|------|---------|
| `scripts/data_collector.py` | 24/7 price data collection (RUNNING on server) |
| `scripts/stress_test.py` | Stress test v1 |
| `scripts/stress_test_v2.py` | Stress test v2 |

### Infrastructure

| File | Purpose |
|------|---------|
| `src/client.py` | ClobClient (HMAC auth, order submission) |
| `src/signer.py` | EIP-712 signing (cached signer/builder) |
| `src/websocket_client.py` | WebSocket client, OrderbookSnapshot |
| `src/http.py` | Thread-local requests.Session |
| `src/config.py` | Config loading (env/YAML) |
| `lib/market_manager.py` | Market discovery via Gamma API |

---

## Appendix A: Strategy Parameter Summary

| Strategy | Parameters | Source | Edge | p-value | Verdict |
|----------|-----------|--------|------|---------|---------|
| H1: Stat-arb | spread=0.15, w=60, tp=0.15, to=20 | ~1.5σ of dispersion | +0.011 | 0.267 | NOT SIGNIFICANT |
| H2: Cheap shares | max=0.25, hold to expire | Payout asymmetry | -0.091 | 0.987 | NEGATIVE EDGE |
| H3: Momentum | lb=20s, move=0.05, tp=0.10, to=30 | Crypto momentum factor | -0.059 | 1.000 | NEGATIVE EDGE |
| H4: Dip reversal | lb=20s, drop=0.05, tp=0.10, to=30 | Mean reversion | -0.060 | 1.000 | NEGATIVE EDGE |
| H5: Lead-lag | lw=15s, move=0.05, tp=0.10, to=20 | Info propagation | -0.051 | 1.000 | NEGATIVE EDGE |
| H6: Bid momentum | lb=10s, rise=0.03, tp=0.10, to=20 | Order flow | -0.043 | 1.000 | NEGATIVE EDGE |
| H7: Autocorrelation | entry=5s, tp=0.10, to=30 | Trend persistence | -0.038 | 1.000 | NEGATIVE EDGE |

## Appendix B: Wallet & Infrastructure

| Item | Value |
|------|-------|
| EOA signer | `0xB968A1d3957fE8B73ad7B8b1B91B06007053e8Ef` |
| Safe/funder | `0xb4d250F58C26840E09723a83cE9c8149Aa32cE99` |
| Signature type | 1 (POLY_PROXY) |
| Server | Hetzner Ireland, `root@38.180.21.165` |
| Server code path | `~/dual_entry` |
| Python server | 3.10 |
| Python local | 3.14 |

---

*This document will be updated as new data is collected and new hypotheses are tested.*
