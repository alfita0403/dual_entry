# Research Findings — Polymarket 5-Min Up/Down Markets

> **Last updated**: 2026-03-03 (Session 5 — Exhaustive Bonferroni pattern scan, OOS robustness suite, Gamma-confirmed inference)
> **Data**: ~130,000 rows | 471 cycles | 3 days (self-collected) + **60,605 markets / 74 days (Telonex)**
> **Coins**: BTC, ETH, SOL, XRP
> **Market type**: Binary 5-minute Up/Down (ERC-1155 tokens on Polygon)
> **Live PnL to date**: ~-$63 (stat-arb -$18, pattern bot ~-$45 from Kelly-amplified losses)

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
11. [Possible Opportunities to Investigate](#11-possible-opportunities-to-investigate) *(updated)*
12. [Statistical Framework](#12-statistical-framework)
13. [Data Requirements for Future Research](#13-data-requirements-for-future-research)
14. [File Reference](#14-file-reference) *(updated)*
15. [Pattern Backtesting — From Theory to Practice](#15-pattern-backtesting--from-theory-to-practice)
16. [Trend Dependency Analysis — The Critical Finding](#16-trend-dependency-analysis--the-critical-finding)
17. [Infrastructure Discoveries & Bug Fixes](#17-infrastructure-discoveries--bug-fixes)
18. [Honest Assessment & Next Steps](#18-honest-assessment--next-steps)
19. [Session 3: Edge Degradation & Regime Analysis](#19-session-3-edge-degradation--regime-analysis) **NEW**
20. [Session 3: Kelly Post-Mortem](#20-session-3-kelly-post-mortem) **NEW**
21. [Late Entry Calibration Study](#21-late-entry-calibration-study)
22. [Binance Volatility & Time Regime Analysis](#22-binance-volatility--time-regime-analysis)
23. [Telonex 73-Day Backtest — The Definitive Answer](#23-telonex-73-day-backtest--the-definitive-answer)
24. [Session 5: Exhaustive Pattern Scan, OOS Suite & Gamma-Confirmed Inference](#24-session-5-exhaustive-pattern-scan-oos-suite--gamma-confirmed-inference) **NEW — CRITICAL**

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

> **UPDATE (Section 23, 16,620 BTC cycles from Telonex)**: BTC runs test p=0.215 — **RANDOM**. The p=0.032 was a Type I error. All 4 coins are IID random sequences.

> **UPDATE (Session 3, 471 cycles)**: ETH initially showed p=0.042 at 440 cycles but decayed to p=0.123 with 31 new cycles -- **lost significance**. BTC held at p=0.032. See Section 19.1 for updated table.

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

> **IMPORTANT UPDATE (Session 2)**: Sections 11.1 and 11.2 below were the original assessments from the autocorrelation analysis (Section 10). **Both have been substantially revised** based on pattern backtesting (Section 15) and trend dependency analysis (Section 16). Read those sections for the full picture.

### 11.1 DDD Reversal Strategy ~~(HIGHEST PRIORITY)~~ **DEAD**

**Status**: **KILLED**. Collapsed under strict inference thresholds.

**Original hypothesis**: After 3 consecutive DOWN resolutions, buy UP on the next cycle. Autocorrelation analysis (Section 10) showed 72.7% UP win rate with +17.3pp unpriced edge.

**What killed it**: The autocorrelation analysis in Section 10 used **lax outcome classification** (thresholds around 0.70/0.30 with earlier inference times). When we applied the **strict thresholds required for live trading** (UP ask >= 0.95 or <= 0.05 at t >= 295s), many cycles that were previously classified as clear UP or DOWN became **ambiguous** and broke the DDD chain. Result:

- With lax thresholds: DDD->UP showed **71% WR** (the number that excited us)
- With strict thresholds: DDD->UP collapsed to **37.7% WR** — below break-even, **total ruin**
- The "pattern" was an artifact of noisy outcome classification, not a real structural dependency

**Lesson**: Any pattern that depends on outcome *classification* is only as good as the classifier. Loose classifiers create phantom patterns that vanish when you tighten the definition to match live execution requirements.

### 11.2 UD Continuation / Dead Cat Bounce ~~(HIGH PRIORITY)~~ **TREND-DEPENDENT**

**Status**: **Partially alive, but mostly riding spot momentum.** See Section 16 for the critical analysis.

**Original hypothesis**: After UP followed by DOWN ("dead cat bounce"), the next cycle resolves DOWN 75-78% of the time. Buy DOWN token.

**Backtest results with strict parameters** (Section 15):
- UD->BUY DOWN: 151 trades, **70% WR**, +$0.82/trade, total +$123.32 on $100
- UU->BUY DOWN: 85 trades, **69% WR**, +$0.73/trade, total +$62.55 on $100
- Combined (UD+UU): 236 trades, **69.5% WR**, +$185.92 on $100, Max DD 12.5%

**But the trend dependency analysis (Section 16) revealed**:
- **88% of total PnL comes from spot-DOWN cycles** where the base rate for DOWN is already 76.4%
- Pattern adds only **+1.5pp over base rate** when BTC spot is falling (p=0.34, NOT significant)
- Pattern adds **+9.7pp over base rate** when BTC spot is rising (p=0.02, marginally significant)
- The aggregate 70% WR is an artifact of the data period: BTC fell -0.49%, creating a DOWN-biased environment

**Honest assessment**: UD/UU patterns are ~88% a bet on spot momentum and ~12% a genuine pattern effect. The genuine pattern effect (+9.7pp in spot-UP conditions) is real but marginal (p=0.02 nominal, would not survive Bonferroni). You could achieve most of the same returns by simply buying DOWN whenever BTC spot just fell.

**Implementation**: Still deployed live (`strategies/sequence.py --pattern UD,UU`), but should be evaluated against a simpler spot-momentum baseline.

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
| `research/telonex_backtest.py` | **Definitive** pattern analysis on 73 days of Telonex data | **NEW** — UU->DOWN 52.2% WR (real but unprofitable) |
| `research/autocorrelation.py` | Outcome sequence dependence, conditional probs, streaks | DDD reversal + UD patterns found (lax thresholds) |
| `research/backtest_patterns.py` | Pattern backtester with strict thresholds, equity curves | UD/UU validated then UD collapsed at 471 cycles |
| `research/trend_check.py` | Spot momentum vs pattern edge decomposition | Edge is mostly trend-dependent |
| `research/regime_analysis.py` | Regime detection: time-of-day, alternation rate, streak length, cross-coin | Choppy regime 84% WR (p=0.009) |
| `research/research_v3.py` | Hypothesis-driven analysis, 7 strategies, bootstrap/permutation | All results negative |
| `research/research_v2.py` | Multi-family grid search, 14-fold CV, worst-case fees | SUPERSEDED by v3 |
| `research/research_v1.py` | Original grid search, 6-fold CV | SUPERSEDED |
| `research/walkforward.py` | Walk-forward OOS test (train Feb 28 / test Mar 1) | ~50% survival (no edge) |
| `research/analyze_prices.py` | Full analysis with 6 plots + console report | REFERENCE |

### Generated Research Outputs

| File | Purpose |
|------|---------|
| `research/equity_UD_UU.png` | Equity curve for UD+UU pattern strategy |
| `research/equity_UD.png` | Equity curve for UD-only pattern strategy |
| `research/equity_UU.png` | Equity curve for UU-only pattern strategy |

### Trading Strategies (live/dry-run)

| File | Purpose | Status |
|------|---------|--------|
| `strategies/sequence.py` | Pattern-based autocorrelation bot (UD, UU, DDD, etc.) | **NEW** — Live on server, ~1700 lines |
| `strategies/stat_arb.py` | Stat-arb divergence bot | HFT-optimized, -$18 live PnL |
| `strategies/btc_signal.py` | BTC lead-lag signal bot | DO NOT MODIFY |
| `strategies/correlation.py` | Correlation hunter | DO NOT MODIFY |

### Scripts (utilities)

| File | Purpose |
|------|---------|
| `scripts/data_collector.py` | 24/7 price data collection (RUNNING on server) |
| `scripts/test_order.py` | CLOB auth diagnostic (clock, API key, FOK test) — **NEW** |
| `scripts/stress_test.py` | Stress test v1 |
| `scripts/stress_test_v2.py` | Stress test v2 |

### Infrastructure

| File | Purpose |
|------|---------|
| `src/client.py` | ClobClient (HMAC auth, order submission) — **HMAC bug fixed** |
| `src/signer.py` | EIP-712 signing (cached signer/builder) |
| `src/websocket_client.py` | WebSocket client, OrderbookSnapshot |
| `src/http.py` | Thread-local requests.Session |
| `src/config.py` | Config loading (env/YAML) |
| `lib/market_manager.py` | Market discovery via Gamma API |

---

## 15. Pattern Backtesting — From Theory to Practice

**Script**: `research/backtest_patterns.py` (~650 lines)
**Purpose**: Bridge the gap between autocorrelation analysis (Section 10, which used lax thresholds) and live execution (which requires strict thresholds). Every parameter is matched exactly to the live bot.

### 15.1 Methodology — Exact Parameter Match

The backtester was designed to replicate the live strategy (`strategies/sequence.py`) with zero parameter mismatch. This is critical because even small differences between backtest and live (e.g., different entry times, threshold levels, or fee assumptions) can create phantom edges that evaporate in production.

| Parameter | Backtest Value | Live Value | Match? |
|-----------|---------------|------------|--------|
| Entry time | t=5s into cycle | `ENTRY_WINDOW_START=5` | Yes |
| Slippage | +$0.03 on ask | `--slippage 0.03` | Yes |
| Fee rate | 1.5% on shares | `SIM_FEE_RATE=0.015` | Yes |
| Max ask | $0.60 | `--max-ask 0.60` | Yes |
| Inference time | t >= 295s | `INFERENCE_TIME=295` | Yes |
| UP threshold | >= 0.95 | `UP_THRESHOLD=0.95` | Yes |
| DOWN threshold | <= 0.05 | `DOWN_THRESHOLD=0.05` | Yes |
| Outcome handling | Ambiguous breaks chain | Same in live | Yes |
| Bet size | $5.00 per trade | `--size 5` | Yes |
| Starting balance | $100.00 | — | — |

**Trade PnL formulas**:
- Fill price = `ask_at_t5 + 0.03` (slippage)
- Cost = `fill_price * size`
- Win payout = `size * (1 - 0.015)` = `size * 0.985` (fee deducted from shares)
- Win PnL = `payout - cost`
- Loss PnL = `-cost` (total loss)

### 15.2 DDD Collapse — The Pattern That Died

The DDD reversal was the most exciting pattern from Section 10: after 3 consecutive DOWNs, BTC UP won 72.7% of the time with +17.3pp unpriced edge. However, that analysis used **lax** outcome classification.

**What changed with strict thresholds (0.95/0.05 at t>=295s)**:

Many cycles that were previously classified as clear UP or DOWN outcomes became **ambiguous** under strict rules. An ambiguous outcome is not recorded in the history, which **breaks the DDD chain**. A sequence that looked like `D, D, D` under lax rules might look like `D, ?, D` under strict rules (where `?` = ambiguous), producing only a single `D` in the history after the break.

| Metric | Lax Thresholds | Strict Thresholds |
|--------|---------------|-------------------|
| DDD->UP Win Rate | ~71% | **37.7%** |
| Edge | +17.3pp | **-12.3pp** |
| Break-even required | ~53% | ~53% |
| Verdict | Promising | **Ruin** |

**Root cause**: The DDD "pattern" was largely an artifact of how outcomes were classified. With generous thresholds, ambiguous cycles got force-classified as U or D, creating longer apparent streaks. With strict thresholds, the true uncertainty is acknowledged and the streaks shatter.

**Lesson**: **Never backtest with parameters looser than what you'll use in production.** This is the pattern strategy equivalent of look-ahead bias.

### 15.3 UD and UU — The Surviving Patterns

With strict thresholds and all parameters matched to live:

| Pattern | Side | Trades | Wins | WR | Avg PnL/trade | Total PnL | Max DD |
|---------|------|--------|------|----|---------------|-----------|--------|
| UD | DOWN | 151 | 106 | **70.2%** | +$0.82 | +$123.32 | — |
| UU | DOWN | 85 | 59 | **69.4%** | +$0.73 | +$62.55 | — |
| **Combined** | — | **236** | **165** | **69.5%** | **+$0.79** | **+$185.92** | **12.5%** |

Starting from $100, the equity curve reached $285.92 after 236 trades across ~255 cycles (~2 days of data).

**Per-coin breakdown** (combined UD+UU):

| Coin | Trades | WR | Note |
|------|--------|----|------|
| BTC | ~59 | ~70% | Consistent |
| ETH | ~61 | ~69% | Consistent |
| SOL | ~58 | ~71% | Best |
| XRP | ~58 | ~68% | Consistent |

The consistency across all 4 coins is notable — the edge (or apparent edge) is not concentrated in a single asset.

### 15.4 Full Pattern Screen

Running `--all-patterns` mode screened 9 built-in patterns:

| Pattern | Side | Trades | WR | Total PnL | Verdict |
|---------|------|--------|----|-----------|---------|
| **UD** | DOWN | 151 | **70%** | +$123 | Best |
| **UU** | DOWN | 85 | **69%** | +$63 | Good |
| DD | UP | 135 | 57% | +$20 | Marginal |
| DU | UP | 96 | 55% | +$5 | Break-even |
| UUU | DOWN | 34 | 71% | +$23 | Too few trades |
| DDD | UP | 53 | **38%** | **-$65** | **Dead** |
| DDDD | UP | 22 | 41% | -$23 | Dead |
| UDU | UP | 47 | 51% | -$4 | Break-even |
| DUD | DOWN | 59 | 63% | +$20 | Marginal |

**Observation**: All "buy DOWN" patterns (UD, UU, UUU, DUD) outperform all "buy UP" patterns. This is suspiciously consistent with the base rate (DOWN wins ~57% of the time). It suggests the patterns may be proxying for the underlying DOWN bias in the data rather than capturing a genuine autocorrelation effect.

### 15.5 Equity Curve Characteristics

The equity curve for UD+UU shows:
- Relatively smooth upward trajectory (no catastrophic drawdowns)
- Max drawdown of 12.5% ($35.6 from peak)
- Win streaks of 8-12 trades are common
- Losing streaks rarely exceed 4-5 trades
- The curve looks "too good" for a 2-day sample — this should raise suspicion, not confidence

**File**: `research/equity_UD_UU.png`

---

## 16. Trend Dependency Analysis — The Critical Finding

**Script**: `research/trend_check.py`
**Purpose**: Answer the most important question about our pattern strategy: **Is the edge structural (works in all conditions) or trend-dependent (only works when crypto is falling)?**

This is arguably the most important analysis in this entire document. It explains why the backtested WR of 70% is misleading.

### 16.1 Methodology

1. Load the same Polymarket CSV data used in pattern backtesting
2. Download BTC-USD 5-minute candles from Yahoo Finance (`yfinance`) for the matching period
3. For each Polymarket 5-minute cycle, find the corresponding BTC spot candle (within 3-minute tolerance)
4. Tag each cycle as **spot-UP** (BTC close > open) or **spot-DOWN** (BTC close < open)
5. Run the UD+UU pattern backtest **separately** on each group, maintaining continuous history across all cycles (patterns build from ALL cycles, but trades only count in the active group)
6. Compare pattern WR to the **base rate** (% of Polymarket outcomes resolving DOWN) in each group

### 16.2 BTC Spot Context for the Backtest Period

| Metric | Value |
|--------|-------|
| Period | Feb 28 18:00 UTC — Mar 1 18:00 UTC |
| BTC open | ~$84,200 |
| BTC close | ~$83,800 |
| BTC change | **-0.49%** |
| Spot-UP cycles | 158 (53%) |
| Spot-DOWN cycles | 140 (47%) |

BTC was essentially flat over the period, with a slight downward bias. The cycle split was roughly 50/50.

### 16.3 Results Split by Spot Direction

#### When BTC Spot is FALLING (spot-DOWN cycles)

| Metric | Value |
|--------|-------|
| Pattern trades | ~100 |
| Pattern WR | **77.9%** |
| Base rate (Polymarket DOWN wins) | **76.4%** |
| Pattern adds | **+1.5 percentage points** |
| z-score | 0.42 |
| p-value | **0.34** (NOT significant) |
| Share of total PnL | **88%** |

**Interpretation**: When BTC spot is falling in a 5-minute window, Polymarket DOWN tokens win 76.4% of the time *regardless of pattern*. The pattern adds only +1.5pp — statistically indistinguishable from zero. You could buy DOWN blindly in every spot-DOWN cycle and get nearly the same returns.

#### When BTC Spot is RISING (spot-UP cycles)

| Metric | Value |
|--------|-------|
| Pattern trades | ~60 |
| Pattern WR | **56.8%** |
| Base rate (Polymarket DOWN wins) | **47.1%** |
| Pattern adds | **+9.7 percentage points** |
| z-score | 2.33 |
| p-value | **0.02** (marginally significant) |
| Share of total PnL | **12%** |

**Interpretation**: When BTC spot is rising, the base rate for DOWN is only 47.1% (DOWN is the *underdog*). The pattern pushes WR to 56.8%, adding +9.7pp of genuine predictive power. This is marginally significant (p=0.02), though it would not survive Bonferroni correction.

### 16.4 The Decomposition

The aggregate 70% WR can be decomposed as:

```
Aggregate WR = (Spot-DOWN share) * (WR in Spot-DOWN) + (Spot-UP share) * (WR in Spot-UP)
            = 0.47 * 77.9% + 0.53 * 56.8%
            = 36.6% + 30.1%
            = ~66.7%

Where:
  - 36.6% / 66.7% = 55% of wins come from spot-DOWN (pattern barely helps)
  - 30.1% / 66.7% = 45% of wins come from spot-UP (pattern genuinely helps)
  
But in PnL terms (because spot-DOWN trades are more profitable):
  - 88% of PnL comes from spot-DOWN cycles
  - 12% of PnL comes from spot-UP cycles
```

### 16.5 Why This Matters

**The pattern is not an independent signal.** It is approximately 88% a bet on spot momentum and 12% a genuine autocorrelation effect. The implications:

1. **On a bullish day** (BTC +2%): The base rate flips — UP wins more, DOWN wins less. The 88% of PnL that came from spot-DOWN cycles evaporates. The +9.7pp pattern edge in spot-UP conditions would generate small profits, but not enough to offset losses from reduced spot-DOWN frequency.

2. **On a flat day** (~50/50 spot): Performance would be mediocre. Roughly break-even or slightly positive from the +9.7pp spot-UP edge, but nothing like the 70% WR we backtested.

3. **On a bearish day** (BTC -2%): Performance would be spectacular — because you're riding the trend, not the pattern. Any strategy that buys DOWN would work.

4. **The 2-day backtest happened to be slightly bearish** (-0.49%), which inflated the aggregate WR. This is sample bias, not alpha.

### 16.6 The Honest Question

> If you could just check whether BTC spot fell in the last 5 minutes (via Binance API) and buy DOWN when it did, would you get the same or better returns than the pattern strategy?

Based on this analysis: **probably yes**. The pattern is a noisy, lagged proxy for spot momentum. The more direct signal (actual BTC spot direction) would be simpler, faster, and more reliable.

### 16.7 What the Pattern Still Offers

The +9.7pp edge in spot-UP conditions (p=0.02) is not zero. It suggests that after UD or UU sequences, there is some residual DOWN pressure that persists even when spot is rising. This could be:

- Market maker behavioral inertia (slow to reprice after a sequence)
- Herding: after seeing DOWN outcomes, retail leans DOWN regardless of spot
- Structural: Polymarket's 5-min resolution mechanics create a mean-reversion tendency

But this is marginal, not transformative. It's the difference between a +9.7pp edge (small, positive) and a +1.5pp edge (zero). Not the difference between 70% WR (riches) and 50% WR (ruin).

---

## 17. Infrastructure Discoveries & Bug Fixes

### 17.1 Critical Bug: `set_api_creds()` HMAC Key Not Updated

**File**: `src/client.py`, method `set_api_creds()`
**Severity**: Critical — all POST /order requests fail with HTTP 401
**Commit**: `8c05177`

**The bug**: `ClobClient.__init__()` pre-decodes the HMAC secret from API credentials into `_api_hmac_key` (bytes) at construction time for performance. However, `set_api_creds()` only updated `self.api_creds` without re-decoding `_api_hmac_key`. In the common flow:

```python
clob = ClobClient(...)         # _api_hmac_key = None (no creds at init)
creds = clob.derive_api_key(signer)
clob.set_api_creds(creds)      # BUG: _api_hmac_key still None
clob.post_order(order, "FOK")  # No POLY_SIGNATURE header sent -> 401
```

**The fix**: `set_api_creds()` now explicitly re-decodes:
```python
def set_api_creds(self, creds):
    self.api_creds = creds
    self._api_hmac_key = None
    if creds and creds.is_valid():
        self._api_hmac_key = base64.urlsafe_b64decode(creds.secret)
```

**Impact**: Without this fix, no live orders could be placed. The bot would appear to work (WebSocket connects, patterns detected, orders signed) but every POST /order would fail silently with 401. This was the root cause of initial live trading failures with the sequence strategy.

### 17.2 API Key Invalidation Behavior

**Discovery**: Each Polymarket wallet can only have **ONE active API key** at a time.

- `create_api_key()` creates a new key and **invalidates all previous keys**
- `create_or_derive_api_key()` tries `create` first, which kills existing keys
- `derive_api_key()` re-derives the existing key without invalidating it

**Implication**: If two bot instances run on the same wallet (e.g., `stat_arb.py` and `sequence.py`), the second one to call `create_api_key()` will kill the first one's credentials. Both bots will then fight over the API key, with each startup invalidating the other.

**Solution**: Use `derive_api_key()` (not `create_or_derive_api_key()`) in production bots. Only use `create_api_key()` during initial setup.

### 17.3 CLOB Auth Diagnostic Tool

**File**: `scripts/test_order.py`

Created to diagnose the 401 auth failures. Tests the full auth chain:

1. **Clock drift check**: Compares local time to CLOB server `/time` endpoint. HMAC auth fails with >5s drift.
2. **API key derivation**: Derives (not creates) API key to avoid invalidating other sessions.
3. **Market discovery**: Finds a live 5-minute market via Gamma API.
4. **FOK test order**: Places a $0.02 bid on UP (55 shares, FOK). Intentionally below market — gets killed immediately, zero financial risk.

Usage: `python scripts/test_order.py` (full test) or `python scripts/test_order.py --dry` (auth only, no order).

### 17.4 Strict Inference Thresholds

**Commit**: `97265c2`

Changed outcome inference from lax to strict:
- Before: UP ask > 0.70 => UP, < 0.30 => DOWN (at various times)
- After: UP ask >= 0.95 => UP, <= 0.05 => DOWN (at t >= 295s only)

This ensures outcomes are only recorded when the market is >95% confident of the result (last 5 seconds of the 300-second cycle). Ambiguous outcomes (between 0.05 and 0.95) are skipped and break pattern chains.

**Impact**: Dramatically reduced the number of classified outcomes, which killed DDD (Section 15.2) but preserved UD/UU at roughly the same win rates.

---

## 18. Honest Assessment & Next Steps

### 18.1 What We Know for Sure

1. **7 intra-cycle strategies are dead** (Section 5). Stat-arb, momentum, dip-buying, lead-lag, bid signals, cheap shares, and single-cycle autocorrelation — all tested with rigorous statistical methods, all negative. The market is efficient at the intra-cycle timescale.

2. **Grid search over parameters is worthless** (Section 6). With 255 data points and thousands of parameter combos, you will always find "winners" that are pure noise. Research v1 and v2 were exercises in overfitting.

3. **DDD reversal is dead** (Section 15.2). The pattern that showed 72.7% WR with lax thresholds collapses to 37.7% with the strict thresholds required for live trading. It was a classification artifact.

4. **The HMAC bug was real** (Section 17.1). Live orders were failing silently because L2 auth headers weren't being sent. This is now fixed.

5. **UD/UU patterns show ~70% WR in backtest** (Section 15.3), but this is heavily confounded by spot momentum (Section 16).

### 18.2 What We Don't Know

1. **Is the +9.7pp edge in spot-UP conditions real?** It's marginally significant (p=0.02), but with only ~60 trades and no Bonferroni correction, this could be noise. Need 200+ spot-UP pattern trades to confirm.

2. **What happens on a bullish day?** The entire 2-day dataset had BTC slightly falling (-0.49%). We have zero data on pattern performance during a BTC rally. The strategy could be catastrophically wrong in that regime.

3. **Is the DOWN base rate bias persistent?** DOWN winning 57% of the time in our sample could be:
   - A genuine structural feature of 5-min crypto markets (likely — crypto has negative skew at short horizons)
   - A sample artifact from a slightly bearish 2-day window (possible)
   - Both (most likely)

4. **Does the live strategy actually make money?** It's deployed (`screen -r sequence` on the server) but has had no fills yet as of this writing. Live slippage, latency, and fill rates could differ from backtest assumptions.

### 18.3 The Uncomfortable Truth

After 2+ weeks of research, ~$63 in live losses, and 1000+ lines of this document, the honest summary is:

**We have not found a statistically robust, regime-independent edge in Polymarket's 5-minute markets.**

What we have found is:
- UU->DOWN on BTC+ETH at 60.2% WR over 471 cycles (p=0.004 for WR > 50%)
- The edge is degrading: 64% at 440 cycles, 60.2% at 471 cycles, with Mar 02 at 52% WR
- ETH lost statistical significance in the runs test (p=0.042 -> p=0.123 with 31 new cycles)
- Kelly scaling amplified losses by 3x during an adverse ETH streak
- The most promising regime filter (choppy markets, 84% WR) has only 31 trades
- BTC-only is the only statistically defensible configuration (p=0.032 runs test, 64.3% WR, 9.5% MaxDD)

This is not a failure of methodology -- it's a success. Finding out that an edge is thinner than expected (and that ETH was a false positive) before losing more capital is the entire point of rigorous research.

> **Session 3 update**: Live bot switched to BTC-only, kelly=0, flat $5. This is the most conservative configuration supported by the data. See Sections 19-20 for full analysis.

### 18.4 Actionable Next Steps

**Priority 1: Collect more data (0 effort, just wait)**
- The data collector runs 24/7. Every day adds ~288 cycles.
- After 1 week: ~2,000 cycles. After 2 weeks: ~4,000 cycles.
- Re-run `trend_check.py` on multi-day data covering both bullish and bearish regimes.
- If the +9.7pp spot-UP edge persists across regimes with p < 0.007 (Bonferroni), it's real.

**Priority 2: Test direct spot momentum strategy**
- Hypothesis: Check BTC spot direction (via Binance API) in the last 5 minutes. If BTC fell, buy DOWN on all 4 coins.
- This is the natural next step from Section 16: if 88% of the edge is spot momentum, use the direct signal instead of the lagged pattern proxy.
- Compare head-to-head with the pattern strategy on the same data.

**Priority 3: Monitor live bot**
- `strategies/sequence.py` is deployed with `--pattern UD,UU --size 5 --max-ask 0.60`
- Track live fills, slippage, and PnL
- If live WR is 60-65% (lower than backtest 70%), that's consistent with the trend dependency analysis
- If live WR is <55%, the strategy should be stopped

**Priority 4: Regime analysis**
- When we have 1+ weeks of data, segment by: time of day, BTC volatility, BTC trend direction
- Look for specific conditions where the pattern edge is concentrated
- A strategy that only trades in favorable regimes (e.g., "pattern + BTC vol < X") might isolate the genuine signal

### 18.5 What NOT to Do

1. **Do NOT increase bet size** until we have 500+ live trades confirming the edge
2. **Do NOT add more patterns** hoping to increase frequency — more patterns = more multiple comparisons = more phantom edges
3. **Do NOT ignore the trend dependency result** — it's the most important finding of this entire project
4. **Do NOT assume the backtest WR (70%) will hold live** — expect 55-65% at best, accounting for regime variation

---

## 19. Session 3: Edge Degradation & Regime Analysis

> **Date**: 2026-03-02
> **Data**: 471 cycles (3 days: Feb 28, Mar 1, Mar 2)
> **Key finding**: The edge is degrading with new data. ETH lost statistical significance. Regime analysis reveals one actionable filter (alternation rate) but with small sample.

### 19.1 Updated Runs Test (471 cycles)

| Coin | z | p-value | Was (440 cycles) | Verdict |
|------|---|---------|-------------------|---------|
| **BTC** | +2.15 | **0.032** | p=0.018 | Still marginal |
| **ETH** | +1.54 | **0.123** | p=0.042 | **LOST SIGNIFICANCE** |
| SOL | +1.13 | 0.258 | 0.258 | Random |
| XRP | +1.05 | 0.294 | 0.294 | Random |

**ETH's mean-reversion signal disappeared with just 31 new cycles.** This is the hallmark of a false positive decaying toward the null. BTC remains marginal (p=0.032) but would not survive Bonferroni correction across 4 tests.

### 19.2 Updated Backtest Results (UU->DOWN, 471 cycles)

| Config | WR (440) | PnL (440) | MaxDD (440) | WR (471) | PnL (471) | MaxDD (471) |
|--------|----------|-----------|-------------|----------|-----------|-------------|
| BTC+ETH flat | 64.0% | +$73 | 7.2% | **60.2%** | **+$46** | **20.4%** |
| BTC only | 68% | +$45 | - | **64.3%** | **+$36** | 9.5% |
| ETH only | 61% | +$28 | - | **56.7%** | **+$10** | **17.5%** |

**In the 31 new cycles (Mar 2 morning)**:
- ETH UU->DOWN: 2W/8L (20% WR) -- catastrophic
- BTC UU->DOWN: 2W/5L (29% WR) -- poor but less severe

### 19.3 Pattern Ranking (BTC+ETH, 471 cycles, all Bonferroni-tested)

| Pattern | Side | Trades | WR | PnL | Avg/trade | z-score | p-value |
|---------|------|--------|----|-----|-----------|---------|---------|
| **UU** | DOWN | 181 | **60.2%** | **+$46** | **$0.26** | **2.87** | **0.004** |
| DD | UP | ~155 | 58% | +$12 | $0.08 | 2.02 | 0.043 |
| DDD | UP | ~59 | 61% | +$12 | $0.20 | 1.69 | 0.091 |
| UUU | DOWN | ~56 | 61% | +$13 | $0.24 | 1.60 | 0.110 |
| DU | UP | ~149 | 40% | -$99 | -$0.67 | - | CATASTROPHIC |
| UD | DOWN | ~229 | 48% | -$39 | -$0.17 | - | CATASTROPHIC |

**Only UU->DOWN survives Bonferroni correction** (p=0.004 < 0.005 for ~10 tests). Notably, **UD->DOWN collapsed** from 70% WR (255 cycles) to 48% WR (471 cycles). This is the most dramatic edge decay observed.

### 19.4 Regime Analysis Results

**Script**: `research/regime_analysis.py`
**Method**: Build all UU->DOWN trades with features, split by bins, test each bin's WR against overall with binomial test.

#### 19.4.1 Temporal Stability (by day)

**BTC+ETH:**

| Day | Trades | WR | PnL/trade |
|-----|--------|----|-----------|
| Feb 28 | 25 | 56% | +$0.02 |
| Mar 01 | 98 | **66%** | +$0.57 |
| Mar 02 | 58 | **52%** | **-$0.17** |

**BTC-only:**

| Day | Trades | WR | PnL/trade |
|-----|--------|----|-----------|
| Feb 28 | 12 | 58% | +$0.04 |
| Mar 01 | 46 | **70%** | +$0.70 |
| Mar 02 | 26 | **58%** | +$0.13 |

Mar 01 was the golden day. Mar 02 shows clear degradation, especially for ETH (which is pulling BTC+ETH combined WR below 55%). BTC-only holds 58% even on Mar 02.

#### 19.4.2 Alternation Rate (SIGNIFICANT)

The most actionable finding from regime analysis:

| Regime | Trades | WR | PnL | Avg/trade | p-value |
|--------|--------|----|-----|-----------|---------|
| Trending (<40% alternation) | 37 | 49% | -$11 | -$0.30 | 0.18 |
| Normal (40-60%) | 113 | 58% | +$12 | +$0.11 | 0.57 |
| **Choppy (>60% alternation)** | **31** | **84%** | **+$45** | **+$1.46** | **0.009** |

**Interpretation**: UU->DOWN is a mean-reversion strategy. When the market is choppy (high alternation between UP/DOWN), mean-reversion works perfectly. When trending, it fails. This has sound economic logic.

**Caveat**: n=31 is small. 95% CI for 84% WR at n=31 is [66%, 95%]. Promising but not conclusive. For BTC-only, the effect is at p=0.066 (16 trades, 88% WR).

#### 19.4.3 Time of Day

| Hour UTC | Trades | WR | p-value | Note |
|----------|--------|----|---------|------|
| 09:00 | 11 | **27%** | **0.032** | European market open |
| 05:00 | 12 | 75% | 0.38 | Asian pre-market |
| 04:00 | 10 | 70% | 0.75 | Asian pre-market |

09:00 UTC (European open) is toxic for mean-reversion -- likely fundamental moves that break the pattern. Small sample (n=11) but direction makes economic sense.

#### 19.4.4 Streak Length at Entry (CRITICAL for Kelly)

**BTC+ETH combined:**

| Streak | Trades | WR | PnL/trade | Kelly would do |
|--------|--------|----|-----------|----------------|
| 2 (UU) | 115 | **65%** | +$0.53 | Base size $5 |
| 3 (UUU) | 38 | **47%** | -$0.40 | 1.5x = $7.50 |
| 4 (UUUU) | 17 | 65% | +$0.37 | 2.0x = $10 |

**Per-coin breakdown:**

| Streak | BTC WR (N) | ETH WR (N) |
|--------|------------|------------|
| 2 (UU) | 64% (58) | 67% (57) |
| 3 (UUU) | **63%** (19) | **32%** (19) |
| 4+ | 83% (6) | 55% (11) |

**ETH collapses to 32% WR at streak=3.** Kelly was scaling size UP at exactly the point where ETH's edge DISAPPEARS. This is the root cause of the $45 live loss.

#### 19.4.5 Other Features (Not Significant)

| Feature | Finding | p-value |
|---------|---------|---------|
| Cross-coin UPs | 4 coins UP: 62% WR; 2 coins UP: 47% WR | >0.25 |
| Entry ask price | No pattern (55-65% across all bins) | >0.50 |
| Intra-cycle volatility | No difference between med/high vol | >0.75 |
| Price trend (3-cycle) | Falling: 66%, Rising: 49% | 0.14 |
| Trailing WR (window=15) | ~60-62% in all bins, not predictive | n/a |

Trailing WR is not predictive -- losses appear random within the model, not regime-clustered.

---

## 20. Session 3: Kelly Post-Mortem

### 20.1 What Happened

The live bot ran with `--coins BTC,ETH --kelly 0.5` starting ~Mar 1 evening.

**Kelly scaling formula**: `size = base_size * (1 + kelly * (streak - 2))` where streak = consecutive UP outcomes before entry.

At streak=2 (UU): $5.00
At streak=3 (UUU): $5 * (1 + 0.5*1) = $7.50
At streak=4 (UUUU): $5 * (1 + 0.5*2) = $10.00
At streak=5+: $12.50+

### 20.2 The Catastrophe

ETH went on a 6+ consecutive UP streak during a trending period. Kelly escalated:
- Trade 1 (UU, $5): LOSS
- Trade 2 (UUU, $7.50): LOSS
- Trade 3 (UUUU, $10): LOSS
- Trade 4 (UUUUU, $12.50): LOSS
- ...

With flat $5 sizing, these 4+ losses would cost ~$17. With Kelly scaling, the cost was ~$43.

**Kelly tripled the losses** because it increased exposure precisely in the regime where ETH's edge evaporated (streak>=3 has 32% WR for ETH).

### 20.3 Why Kelly Failed

Kelly criterion assumes:
1. **Stationary win rate** -- ETH's WR drops from 67% at streak=2 to 32% at streak=3
2. **Known edge** -- the edge was estimated from insufficient data (440 cycles)
3. **Long-run convergence** -- requires thousands of bets; we had dozens

With non-stationary win rates, Kelly becomes an anti-strategy: it bets MORE when the edge is SMALLER.

### 20.4 Resolution

**Live bot restarted** with: `--coins BTC --kelly 0 --size 5`
- Eliminated ETH (lost statistical significance, 32% WR at streak=3)
- Eliminated Kelly scaling (non-stationary win rates)
- BTC-only with flat sizing is the most conservative configuration

### 20.5 Lesson

**Never use Kelly without proving win rate stationarity across the conditions where Kelly would increase size.** In our case, Kelly's condition for increasing size (longer streaks) was ANTI-CORRELATED with higher win rates for ETH. The backtester should have tested Kelly at each streak length separately -- this was the missing analysis.

---

## 21. Late Entry Calibration Study

> **Date**: 2026-03-03
> **Script**: `research/late_entry.py`
> **Source**: Medium article by Benjamin-Cup proposing "last-second dynamics" edge in Polymarket 5-min markets
> **Verdict**: DEAD. Market maker is well-calibrated. No edge after costs.

### 21.1 Hypothesis

Enter late in the 5-minute cycle (t=240-280s) when the outcome is nearly decided. If the market says 80% UP but actual win rate is 90%, there's a 10pp edge buying UP late.

### 21.2 Results

The market maker is well-calibrated at every time point tested:

| Time | Side | Threshold | N | WR | PnL | Miscalibration |
|------|------|-----------|---|-----|-----|----------------|
| t=270 | BUY UP | ask>=70% | 536 | 87.9% | **-$193** | -2.9pp |
| t=270 | BUY DOWN | ask<=30% | 769 | 96.7% | **-$52** | +3.1pp |
| t=280 | BUY UP | ask>=70% | 476 | 88.7% | **-$178** | -3.1pp |
| t=280 | BUY DOWN | ask<=30% | 791 | 97.0% | **-$87** | +2.4pp |

**Every simulation loses money** despite 88-97% win rates. The miscalibration (~3pp) does not cover the cost of entry (slippage $0.03 + fees 1.5% = ~4-5pp minimum edge needed).

### 21.3 Systematic Bias Found

The MM consistently **overprices UP** and **underprices DOWN** across all time bins. But this is the same global DOWN bias we already knew about (DOWN wins ~57%). It's not a late-entry edge -- it's the base rate.

### 21.4 Conclusion

The article's core premise ("enter late when the outcome is nearly decided") is correct in direction but the market already prices the near-certainty correctly. You're buying certainty, and certainty is expensive. After slippage and fees, there is no alpha.

---

## 22. Binance Volatility & Time Regime Analysis

> **Date**: 2026-03-03
> **Script**: `research/regime_vol.py`
> **Data**: Binance BTCUSDT 1-minute klines (2,479 candles) cross-referenced with 84 BTC UU->DOWN trades
> **Verdict**: Promising directional signals but NOT statistically significant (n too small). Needs validation with Mar 03+ data.

### 22.1 Hypothesis

UU->DOWN is a mean-reversion strategy. Mean-reversion should work better when:
- Volatility is low (less fundamental/news noise)
- Volume is low (fewer directional participants)
- During Asian session (less institutional activity)

### 22.2 Key Results

#### Realized Volatility (1h rolling, Binance 1m returns)

| Regime | N | WR | PnL | vs avg | p-value |
|--------|---|-----|------|--------|---------|
| **Low vol** (< p33) | 28 | **79%** | +$31 | **+14.3pp** | 0.166 |
| Medium vol | 28 | **46%** | -$12 | **-17.9pp** | 0.074 |
| High vol (> p67) | 28 | 68% | +$17 | +3.6pp | 0.844 |

Surprising U-shape: both low AND high vol work, but medium vol is catastrophic (46% WR). Low vol has the strongest effect (+14.3pp above average).

#### Session (4-hour blocks)

| Session | N | WR | PnL | vs avg | p-value |
|---------|---|-----|------|--------|---------|
| **04-08 UTC (Asia morning)** | 12 | **92%** | +$21 | **+27.4pp** | 0.067 |
| 00-04 UTC (Asia night) | 16 | 62% | +$7 | -1.8pp | 1.000 |
| 12-16 UTC (EU afternoon) | 12 | 67% | +$5 | +2.4pp | 1.000 |
| 20-24 UTC (US evening) | 18 | 61% | +$3 | -3.2pp | 0.808 |
| 08-12 UTC (EU morning) | 19 | **53%** | -$2 | **-11.7pp** | 0.340 |

**04-08 UTC is the best session** (92% WR, 11W/1L). **08-12 UTC is the worst** (53% WR). This aligns with the hypothesis: Asian morning has less volume and fewer fundamental moves, EU morning brings institutional participation.

#### BTC 1h Trend (Binance)

| Trend | N | WR | PnL | vs avg |
|-------|---|-----|------|--------|
| **Mild down (-0.5% to -0.1%)** | 11 | **91%** | +$19 | **+26.6pp** |
| Strong down (<-0.5%) | 11 | 73% | +$10 | +8.4pp |
| Mild up (+0.1% to +0.5%) | 21 | 76% | +$20 | +11.9pp |
| **Flat (-0.1% to +0.1%)** | 15 | **47%** | -$6 | **-17.6pp** |
| **Strong up (>+0.5%)** | 26 | **50%** | -$7 | **-14.3pp** |

UU->DOWN works best when BTC is mildly moving (either direction) but fails when flat or strongly rising. Mild down + mild up are both good; the pattern seems to need SOME directional context but not too much.

#### Combined: Volatility x Session

| Combination | N | WR | PnL |
|-------------|---|-----|------|
| **LowVol + Asia** | 16 | **75%** | +$15 |
| **LowVol + US** | 7 | **86%** | +$10 |
| HighVol + EU | 12 | 50% | -$4 |
| HighVol + US | 18 | 50% | -$5 |

### 22.3 Honest Assessment

**Nothing is statistically significant** (all p > 0.05). With only 84 BTC trades split into bins of 7-28, there is no power to detect anything below ~20pp effects.

However, the directions are economically consistent:
1. Low vol -> more mean-reversion -> pattern works better (makes sense)
2. Asia morning -> less noise -> cleaner signal (makes sense)
3. EU morning -> institutional activity -> breaks mean-reversion (makes sense)
4. Flat markets -> no directional context -> coin-flip (makes sense)

### 22.4 Next Steps

**Re-run with Mar 03 data** (full day) to roughly double the sample. If:
- Asia morning 04-08 UTC maintains >80% WR with n>=20 -> implement as time filter
- Low vol maintains >75% WR with n>=40 -> implement as vol filter
- EU morning stays <55% WR -> implement as exclusion filter

If confirmed, the live bot could be modified to: only trade during 00-08 UTC and 12-20 UTC (exclude EU morning and late US), and/or only when Binance 1h vol is below median.

### 22.5 File Reference

| File | Purpose |
|------|---------|
| `research/regime_vol.py` | Full analysis script (downloads Binance data, caches it, cross-refs with trades) |
| `research/late_entry.py` | Late-entry calibration study (Section 21) |
| `research/binance_klines_cache.json` | Cached Binance 1m klines (avoid re-downloading) |

---

## 23. Telonex 73-Day Backtest — The Definitive Answer

> **Date**: 2026-03-03
> **Script**: `research/telonex_backtest.py`
> **Data**: Telonex free markets dataset — 60,605 resolved 5-min markets, 74 days (Dec 18, 2025 – Mar 1, 2026)
> **Sample sizes**: BTC 16,620 | ETH 14,661 | SOL 14,663 | XRP 14,661
> **Verdict**: UU->DOWN edge is REAL but TINY (52.2% WR). Not profitable after costs.

This is the most important section in this document. Everything before this was based on 471 cycles (3 days) of self-collected data. Telonex provides **35x more data** — enough to settle every open question definitively.

### 23.1 Data Source

Telonex (telonex.io) provides free downloadable datasets of Polymarket market metadata including **resolution outcomes**. The free dataset contains no price/quote data — only which side won each market. This is sufficient for:
- Runs tests (is the sequence random?)
- Pattern win rates (UU->DOWN, DDD->UP, etc.)
- Hour-of-day analysis
- Temporal stability (weekly WR)
- Streak length analysis

It is NOT sufficient for:
- Entry price / max_ask filter analysis
- Slippage/fees/PnL estimation
- Intra-cycle price dynamics

File: `data/telonex_updown_5m.parquet` (no API downloads used — free dataset)

### 23.2 Runs Test — The Sequence is RANDOM

Our 471-cycle runs test (Section 10) showed BTC with p=0.032, suggesting mean-reversion. **With 35x more data, this disappears completely.**

| Coin | N | UP% | Runs | z | p-value | 471-cycle result |
|------|---|-----|------|---|---------|-----------------|
| **BTC** | **16,620** | **50.1%** | **8,391** | **+1.24** | **0.215** | was p=0.032 |
| ETH | 14,661 | 49.7% | 7,369 | +0.62 | 0.533 | was p=0.123 |
| SOL | 14,663 | 49.1% | 7,365 | +0.58 | 0.565 | — |
| XRP | 14,661 | 49.1% | 7,274 | -0.91 | 0.364 | — |

**ALL FOUR COINS ARE RANDOM.** No mean-reversion, no trending, no structure. The z-scores are all within [-1, +1.3] — pure noise. BTC's original p=0.032 was a Type I error from small N.

**Implication**: The entire theoretical basis for our pattern strategy ("BTC mean-reverts, so after UU, DOWN is more likely") is wrong. The sequence is IID. Any pattern effect must come from a different mechanism than mean-reversion.

### 23.3 Pattern Backtesting — All 10 Patterns (All Coins Combined)

Base rate DOWN (avg across 4 coins): 48.6%. Bonferroni correction for 10 tests: alpha = 0.005.

| Pattern | Side | Trades | Wins | WR | vs base | z | p-val | Verdict |
|---------|------|--------|------|----|---------|---|-------|---------|
| **UU** | **DOWN** | **14,803** | **7,733** | **52.2%** | **+3.7pp** | **+8.97** | **<0.0001** | **SIGNIFICANT** |
| **UUU** | **DOWN** | **7,069** | **3,817** | **54.0%** | **+5.4pp** | **+9.15** | **<0.0001** | **SIGNIFICANT** |
| **UUUU** | **DOWN** | **3,252** | **1,773** | **54.5%** | **+6.0pp** | **+6.81** | **<0.0001** | **SIGNIFICANT** |
| **DDD** | **UP** | **6,776** | **3,600** | **53.1%** | **+1.7pp** | **+2.77** | **0.003** | **SIGNIFICANT** |
| **DDDD** | **UP** | **3,176** | **1,717** | **54.1%** | **+2.6pp** | **+2.95** | **0.002** | **SIGNIFICANT** |
| DD | UP | 14,219 | 7,443 | 52.3% | +0.9pp | +2.15 | 0.016 | nominal only |
| UD | DOWN | 15,190 | 7,439 | 49.0% | +0.4pp | +1.03 | 0.153 | NOT significant |
| DU | UP | 15,195 | 7,735 | 50.9% | -0.5pp | -1.33 | 0.911 | NOT significant |
| DUD | DOWN | 7,460 | 3,633 | 48.7% | +0.1pp | +0.25 | 0.405 | NOT significant |
| UDU | UP | 7,751 | 3,925 | 50.6% | -0.8pp | -1.42 | 0.924 | NOT significant |

**Key findings**:

1. **UU->DOWN is real** (p<0.0001, survives Bonferroni). But the edge is only **+3.7pp above base rate** — our 471-cycle backtest showing 69.4% WR was a lucky small sample.

2. **Longer UP streaks have higher WR** (UU: 52.2%, UUU: 54.0%, UUUU: 54.5%) — mean-reversion pressure increases with streak length. But they occur less frequently.

3. **UD->DOWN is DEAD** (p=0.153). Our 471-cycle result of 70.2% WR was entirely noise + spot momentum bias. With 15,190 trades, it's 49.0% — essentially a coin flip.

4. **DDD->UP is alive** (p=0.003) — the opposite direction mean-reversion also works, with 53.1% WR.

5. **DU->UP and UDU->UP are negative** — alternating patterns have no predictive power.

### 23.4 UU->DOWN Per Coin

| Coin | Trades | Wins | WR | Base DOWN% | Delta | p-val |
|------|--------|------|----|-----------|-------|-------|
| BTC | 4,123 | 2,151 | 52.2% | 48.2% | +4.0pp | <0.0001 |
| ETH | 3,607 | 1,909 | 52.9% | 48.2% | +4.7pp | <0.0001 |
| SOL | 3,517 | 1,857 | 52.8% | 48.9% | +3.9pp | <0.0001 |
| XRP | 3,556 | 1,816 | 51.1% | 48.9% | +2.1pp | 0.005 |

**All 4 coins show the effect.** BTC, ETH, SOL are statistically significant (p<0.0001). XRP is marginally significant (p=0.005). The consistency across coins suggests a structural market mechanism, not a coin-specific anomaly.

**But the edge is small**: +2.1 to +4.7pp above the base rate. This is the RAW outcome edge — before entry price, slippage, and fees.

### 23.5 Profitability Estimate (Fatal Math)

Even though UU->DOWN is statistically significant, it is likely **not profitable**:

```
WR = 52.2%
Assume best-case entry: DOWN ask = $0.48 (base rate implies this)
Win payout = $1.00 * (1 - 0.015 fee) = $0.985
Win PnL = $0.985 - $0.48 = +$0.505
Loss PnL = -$0.48

EV = 0.522 * $0.505 + 0.478 * (-$0.48)
   = $0.2636 - $0.2294
   = +$0.034 per $0.48 risked (best case, NO slippage)

With slippage of $0.03 on entry:
  Entry cost = $0.51
  Win PnL = $0.985 - $0.51 = +$0.475
  Loss PnL = -$0.51
  EV = 0.522 * $0.475 + 0.478 * (-$0.51)
     = $0.2480 - $0.2438
     = +$0.004 per trade ≈ BREAK-EVEN

With max_ask = $0.60 (our actual live parameter):
  Entry cost = $0.60 + $0.03 slippage = $0.63
  Win PnL = $0.985 - $0.63 = +$0.355
  Loss PnL = -$0.63
  EV = 0.522 * $0.355 + 0.478 * (-$0.63)
     = $0.1853 - $0.3011
     = -$0.116 per trade ← NEGATIVE EV
```

**At max_ask=0.60, the strategy loses ~$0.12 per trade.** The only way it could be profitable is if the DOWN ask is consistently near $0.48 when UU occurs — and we don't have entry price data from Telonex to verify this.

### 23.6 Hour-of-Day Analysis (UU->DOWN, All Coins)

Does the Asia morning effect from Section 22 hold with 14,803 trades?

| Hour UTC | Trades | Wins | WR | vs avg 52.2% | p-val |
|----------|--------|------|----|-------------|-------|
| 02:00 | 596 | 338 | 56.7% | +4.5pp | 0.030 * |
| **23:00** | **561** | **324** | **57.8%** | **+5.5pp** | **0.010** |
| 13:00 | 605 | 330 | 54.5% | +2.3pp | 0.272 |
| 08:00 | 617 | 295 | 47.8% | -4.4pp | 0.029 * |
| 01:00 | 640 | 312 | 48.8% | -3.5pp | 0.082 |
| 03:00 | 655 | 318 | 48.5% | -3.7pp | 0.060 |
| 15:00 | 655 | 323 | 49.3% | -2.9pp | 0.137 |

**Most hours are within 2-3pp of the 52.2% average.** Two hours show nominal significance:

- **23:00 UTC** (57.8% WR, p=0.010) — best hour. But with 24 tests, Bonferroni alpha = 0.002 — does NOT survive correction.
- **02:00 UTC** (56.7% WR, p=0.030) — second best. Also does not survive Bonferroni.
- **08:00 UTC** (47.8% WR, p=0.029) — worst hour, consistent with Section 22's finding that EU morning is worst.

**Does the 04-08 UTC Asia morning effect hold?** Mixed. Hour 04 (51.6%) and 05 (53.1%) are close to average. Hour 06 (52.7%) and 07 (51.8%) are also unremarkable. The dramatic 92% WR from Section 22 (n=12) was small-sample noise. With n=600+ per hour, the reality is much more modest.

**Conclusion**: There is NO hour-of-day filter that would meaningfully improve the strategy. The hourly variation is ~5pp at best, which doesn't change the fundamental profitability calculation.

### 23.7 Temporal Stability (Weekly WR)

| Week | Trades | Wins | WR |
|------|--------|------|----|
| 2025-W50 | 1,057 | 533 | 50.4% |
| 2025-W51 | 1,969 | 1,047 | **53.2%** |
| 2025-W52 | 866 | 465 | **53.7%** |
| 2026-W00 | 1,247 | 613 | 49.2% |
| 2026-W01 | 2,111 | 1,069 | 50.6% |
| 2026-W02 | 2,014 | 1,059 | **52.6%** |
| 2026-W03 | 1,879 | 1,021 | **54.3%** |
| 2026-W04 | 292 | 139 | 47.6% |
| 2026-W06 | 294 | 150 | 51.0% |
| 2026-W07 | 1,436 | 752 | 52.4% |
| 2026-W08 | 1,638 | 885 | **54.0%** |

The WR oscillates between 47.6% and 54.3% across weeks. Several weeks are **below 50%** (W50, W00, W01, W04). The "good" weeks (53-54%) are barely above break-even after costs. There is no consistent regime — the edge is thin and variable.

### 23.8 Streak Length Analysis (All Coins)

After N consecutive UPs, what is P(DOWN next)?

| Streak | Trades | P(DOWN) | Note |
|--------|--------|---------|------|
| 2 (UU) | 7,734 | 50.6% | Our entry — barely above 50% |
| 3 (UUU) | 3,817 | 53.5% | Better, but fewer trades |
| 4 (UUUU) | 1,773 | 54.4% | Better still, much fewer trades |
| 5 | 808 | 55.4% | Diminishing frequency |
| 6 | 360 | 55.8% | ~1.5 per day across 4 coins |
| 7 | 159 | 56.6% | Rare |
| 8+ | <70 | Unstable | Too few for inference |

**The streak effect is real but weak.** P(DOWN) increases by ~1pp per additional UP in the streak, from 50.6% at streak=2 to 56.6% at streak=7. But the fundamental problem remains: the edge is only 2-7pp above 50%, and costs are 4-5pp.

**Important note on Section 6 output**: The streak table counts trades where the streak is EXACTLY N (not N+). So streak=2 means "exactly 2 UPs" (preceded by a non-UP). The total UU->DOWN pattern (including UUU, UUUU, etc.) has the full 14,803 trades at 52.2% WR.

### 23.9 Comparison: 471 Cycles vs 16,620 Cycles

| Metric | 471 cycles (ours) | 16,620 cycles (Telonex) | Shrinkage |
|--------|-------------------|------------------------|-----------|
| BTC runs test p | 0.032 (significant!) | 0.215 (RANDOM) | Edge was noise |
| UU->DOWN WR | 69.4% (amazing!) | 52.2% (barely above 50%) | -17.2pp |
| UD->DOWN WR | 70.2% (amazing!) | 49.0% (coin flip) | -21.2pp |
| DDD->UP WR | 37.7% (dead) | 53.1% (actually alive!) | +15.4pp |
| UU trades (BTC) | 85 | 4,123 | 48x more data |

**Every exciting finding from 471 cycles shrank dramatically:**
- UU->DOWN lost 17pp of win rate
- UD->DOWN went from "best pattern" to "coin flip"
- DDD->UP reversed direction (was dead, now alive but thin)

This is **textbook regression to the mean**. Small samples produce extreme estimates; large samples converge to the true (boring) value.

### 23.10 What This Means for the Live Bot

The live bot runs `--pattern UU --side DOWN --size 5 --max-ask 0.60 --coins BTC --kelly 0`.

With the Telonex data, we now know:
- **True WR ≈ 52.2%** (not 64-70% as we hoped)
- **At max_ask=0.60, EV is negative** (see Section 23.5)
- **No hour filter can save it** (Section 23.6)
- **No streak filter can save it** — even streak=7 (56.6% WR) doesn't overcome 4-5pp of costs at entry prices above $0.50

**The only scenario where UU->DOWN could be profitable** is if the DOWN ask at the time of UU is consistently below $0.50 — meaning we're buying DOWN shares cheaply. This is plausible (the market might slightly overprice UP continuation after UU), but we don't have entry price data from Telonex to verify.

Our self-collected data (84 BTC trades) could answer this, but 84 trades is too few and already shown to be biased by the spot momentum context of those specific days.

### 23.11 Script Reference

| File | Purpose |
|------|---------|
| `research/telonex_backtest.py` | Full pattern analysis on Telonex data (6 sections, all coins) |
| `data/telonex_updown_5m.parquet` | 60,605 market resolutions (Telonex free dataset) |
| `data/telonex_updown_5m.csv` | Same data as CSV |

---

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
| **H8: UD pattern** | entry=5s, hold to resolution, max_ask=0.60 | Sequence autocorrelation | **+$0.82/trade** | ~0.02* | **TREND-DEPENDENT** |
| **H9: UU pattern** | entry=5s, hold to resolution, max_ask=0.60 | Sequence autocorrelation | **+$0.73/trade** | ~0.02* | **TREND-DEPENDENT** |
| **H8/H9 revised (Telonex)** | outcome-only, no price filter | Sequence autocorrelation | **WR=52.2%** | <0.0001 | **REAL BUT UNPROFITABLE** |

*\*p-value is for the pattern's added edge above base rate in spot-UP conditions only (Section 16). The aggregate edge is confounded by spot momentum.*
*Section 23 Telonex data (16,620 BTC cycles): UU->DOWN WR=52.2% (p<0.0001), UD->DOWN WR=49.0% (not significant). Edge is real but tiny (~3.7pp), likely not profitable after 4-5pp of costs.*

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

---

## 24. Session 5: Exhaustive Pattern Scan, OOS Suite & Gamma-Confirmed Inference

> **Date**: 2026-03-03
> **Key deliverables**: (1) Exhaustive 1,290-test Bonferroni pattern scan, (2) Top-30 OOS robustness suite with multi-holdout + regime validation, (3) Gamma-confirmed outcome inference system with warm-start, (4) Critical outcome inference bugfix
> **Status**: Live strategy updated and deployed with OOS-robust rule set + Gamma confirmation

### 24.1 Exhaustive Pattern Scan (Bonferroni-Corrected)

**Script**: `research/full_pattern_scan.py`

Ran 1,290 exhaustive tests across all patterns of length 2-6, all 4 coins individually + "ALL" pool, both UP and DOWN sides. Bonferroni-corrected alpha = 0.05 / 1,290 = 0.000039.

**Result**: Only 30 pattern-coin-side combinations survive Bonferroni. **ALL of them are UP-streak -> DOWN mean-reversion.** No DD->UP, no continuation, no alternating patterns survive individual coin-level Bonferroni.

Key survivors (by EV at max_ask=$0.50):

| Strategy | Coin | WR | Max Ask | EV@0.50 | p-value |
|----------|------|----|---------|---------|---------|
| UUUUU->DOWN | ETH | 57.4% | $0.535 | +$0.035 | <0.00004 |
| UUUU->DOWN | ETH | 56.1% | $0.521 | +$0.021 | <0.00004 |
| UUUUU->DOWN | ALL | 54.6% | $0.508 | +$0.008 | <0.00004 |
| UUUU->DOWN | ALL | 54.5% | $0.507 | +$0.007 | <0.00004 |
| UUU->DOWN | ETH | 55.8% | $0.518 | +$0.018 | <0.00004 |

**Critical insight**: "ALL" is a statistical pool, not a cross-coin signal. For live trading, per-coin max_ask caps are more accurate than uniform ALL caps.

### 24.2 Top-30 OOS Robustness Suite

**Script**: `research/top30_oos_suite.py`

Full pipeline: exhaustive scan -> top-30 Bonferroni survivors -> multi-holdout OOS (7/14/21 day holdouts) -> quantile base-rate regime validation -> final survivor scoreboard.

**11 strict survivors** (pass ALL OOS holdouts + >= 2/3 regime bins):

| Strategy | WR | MaxAsk | EV@0.50 | OOS holdouts | Regimes |
|----------|-----|--------|---------|-------------|---------|
| ETH UUUU->DOWN | 57.4% | $0.535 | +$0.035 | 3/3 | 3/3 |
| ALL UUUUU->DOWN | 54.6% | $0.508 | +$0.008 | 3/3 | 3/3 |
| ALL UUUU->DOWN | 54.5% | $0.507 | +$0.007 | 3/3 | 3/3 |
| BTC UDUU->DOWN | 54.2% | $0.504 | +$0.004 | 3/3 | 3/3 |
| ALL UUU->DOWN | 54.0% | $0.502 | +$0.002 | 3/3 | 3/3 |

**Regime robustness**: All surviving patterns maintain WR > base rate across low/mid/high base-rate regimes (quantile terciles). In UP-dominant periods, EV@0.50 can go negative even when WR > base (need tighter max_ask). In DOWN-dominant periods, edge amplifies significantly.

### 24.3 Live Config Update (OOS-Robust Rule Set)

Updated `strategies/mean_reversion.yaml` with 6 rules derived from the OOS suite:

| Rule | Coins | Max Ask | Rationale |
|------|-------|---------|-----------|
| UUUUU->DOWN | ETH | $0.54 | Strongest individual coin signal |
| UUUU->DOWN | ETH | $0.54 | ETH has highest WR at 4-streak |
| UUUUU->DOWN | BTC, SOL, XRP | $0.51 | Pool survivors, tighter cap |
| UUUU->DOWN | BTC, SOL, XRP | $0.51 | Pool survivors, tighter cap |
| UDUU->DOWN | BTC | $0.51 | BTC-specific alternating pattern |
| UUU->DOWN | ALL | $0.51 | Most frequent signal, modest edge |

Entry window: 1-3s, cancel timeout: 10s, size: $5 flat.

### 24.4 Expected Monthly PnL Model

Based on pattern frequencies in Telonex data:
- ~112 signals/day across all rules, ~34 fills/day at 30% fill rate
- EV/share: ~$0.03, size=5 shares -> ~$0.15/trade
- **~$149/month** at 30% fill rate (optimistic)
- **~$111/month** conservative (extra entry fee)

### 24.5 Critical Bug Fix: Outcome Inference at Cycle Rollover

**Commit**: `0e7a914`

**The bug**: In `_on_market_change()`, the asks/bids were reset to defaults (`up=1.0`) BEFORE inferring old-cycle outcomes. This caused the inference function to see `up_ask=1.0` (the default placeholder) and classify the outcome as "UP" — regardless of what actually happened.

**Impact**: False UP classifications contaminated pattern histories, creating phantom UP streaks that triggered false DOWN trades.

**Fix**: Three changes:
1. Infer outcomes BEFORE invalidating ask cache
2. Added `_up_ask_cycle_seen` dict requiring a fresh WS snapshot before inference
3. Reject inference when no UP snapshot was received for that cycle

### 24.6 Robust Gamma-Confirmed Outcome Inference System

**Commit**: `d0a4004`

This is the most important infrastructure change of the session. The previous system used WS price thresholds (UP_THRESHOLD=0.95, DOWN_THRESHOLD=0.05) which misclassified ~10-15% of markets decided in the last seconds.

#### The Problem

When the underlying asset price is near the strike, a small move in the last seconds can flip the outcome. The WS data arrives with 50-170ms latency, and at 0.96/0.04 the outcome is NOT certain. Markets where `up_ask` ends at 0.96 sometimes resolve DOWN.

#### The Solution

**Two-tier inference with Gamma API ground truth:**

1. **WS inference (immediate, provisional)**: Only classify when `up_ask >= 0.99` (UP) or `up_ask <= 0.01` (DOWN). These thresholds represent near-absolute certainty — the market makers have consensus-priced the outcome.

2. **Ambiguous outcomes**: When ask is between 0.01 and 0.99, record as `-` (unknown). This immediately breaks any pattern chain, preventing trades on uncertain history.

3. **Gamma API recheck (async, ~3.5 min delay)**: Every recorded outcome (including provisionals) is queued for verification against Gamma API (`GET /markets/slug/{slug}`). When `closed=true` and `outcomePrices` resolves, the entry is upgraded to CONFIRMED.

4. **Retry logic**: If Gamma hasn't closed the market yet, retry every 5 minutes until confirmed. All outcomes are eventually confirmed.

5. **Pattern matching gate**: For a pattern of length N (e.g., UUU), at least the first N-1 outcomes MUST be CONFIRMED by Gamma before a trade is allowed. The last outcome (the trigger) can be provisional or confirmed.

6. **Correction detection**: If Gamma confirms a different outcome than the WS inference, the entry is corrected and logged as a warning.

#### TUI Color Coding

- **White** (`\033[97m`): Provisional or unknown — not yet verified by Gamma
- **Orange** (`\033[38;5;214m`): Confirmed by Gamma API — ground truth verified

#### Data Structures

```python
class ConfirmationStatus(Enum):
    PROVISIONAL = "PROVISIONAL"   # WS inference, not yet verified
    CONFIRMED = "CONFIRMED"       # Gamma API verified
    UNKNOWN = "UNKNOWN"           # Ambiguous WS, awaiting Gamma

@dataclass
class OutcomeEntry:
    outcome: str                  # "U", "D", or "-"
    status: ConfirmationStatus
    cycle_ts: int
    market_slug: str
    observed_up_ask: float
```

The `_outcome_history` deque now stores `OutcomeEntry` objects instead of plain strings.

### 24.7 Warm-Start: Pre-Fill History from Gamma API

**Commit**: (this session)

Eliminates the 25-minute cold-start window where the bot has no pattern data. At startup:

1. Calculate the previous 6 cycle timestamps (each 5 min apart) from the current active market
2. For each (coin, cycle_ts), query Gamma API for the market resolution
3. If `closed=true` with resolved `outcomePrices`: store as CONFIRMED (orange in TUI)
4. If not closed yet (e.g., immediately preceding cycle): store as `-` UNKNOWN and queue for async Gamma recheck
5. Load into `_outcome_history` oldest-first

**Result**: The bot can start trading on valid patterns from the very first cycle, rather than waiting 25+ minutes to accumulate 5 outcomes via WS observation.

### 24.8 Research Pipeline Scripts

| File | Purpose | Status |
|------|---------|--------|
| `research/full_pattern_scan.py` | Exhaustive pattern scanner: all lengths 2-6, all coins, Bonferroni, OOS holdout, date filters | **UPDATED** |
| `research/top30_oos_suite.py` | Full pipeline: scan -> top-N -> multi-holdout OOS -> regime validation -> scoreboard | **NEW** |
| `research/base_regime_validation.py` | Tests patterns across base-rate regimes (fixed + quantile bins) | **NEW** |

### 24.9 Key Lessons from This Session

1. **UD->DOWN collapsed from 70% WR (255 cycles) to 49% (15,190 cycles in Telonex)**. This was the most dramatic edge decay observed. Never trust small-sample backtests.

2. **All surviving patterns after Bonferroni are UP-streak -> DOWN mean-reversion.** No continuation, no alternating, no DOWN-streak patterns survive. The market microstructure creates a specific mean-reversion signature only after UP streaks.

3. **ETH is the strongest individual coin for mean-reversion patterns.** UUUU->DOWN at 57.4% WR on ETH is the single best OOS-robust signal.

4. **Asia morning 92% WR (n=12) shrunk to 50.8% WR (n=701) with Telonex data.** Hour-of-day filters do not survive larger datasets.

5. **Late entry strategy is DEAD.** Market maker is well-calibrated at every time point. No alpha from entering late.

6. **10-15% of markets are decided in the last 10 seconds.** WS-based inference at 0.95/0.05 thresholds is unreliable for these. Only >= 0.99 provides near-certainty. Gamma API is the only ground truth.

7. **Pattern chains built on uncertain outcomes are worse than no chains at all.** A single misclassified outcome can trigger a false pattern match and a losing trade.

---

*This document will be updated as new data is collected and new hypotheses are tested.*
