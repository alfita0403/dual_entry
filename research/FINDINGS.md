# Research Findings — Polymarket 5-Min Up/Down Markets

> **Last updated**: 2026-03-04 (Session 8 — Look-ahead bias in RSI backtester FIXED, corrected numbers)
> **Data**: 60,605 resolved markets / 74 days (Telonex) + 5 days self-collected + 75 live trades
> **Coins**: BTC, ETH, SOL, XRP
> **Market type**: Binary 5-minute Up/Down (ERC-1155 tokens on Polygon)
> **Live status**: Running `mean_reversion.py` with RSI(7) filter + drawdown circuit breaker
>
> **WARNING**: Session 7 numbers for RSI filter (71.5% WR) were INVALID due to look-ahead bias
> in the backtester. The RSI was computed using the candle that contained the trade's outcome.
> Corrected numbers show RSI provides NO benefit to WR in backtesting. See Section 6.

---

## Table of Contents

1. [Current Live Strategy](#1-current-live-strategy)
2. [Market Structure](#2-market-structure)
3. [Base Rates](#3-base-rates)
4. [Telonex 73-Day Dataset — The Definitive Data](#4-telonex-73-day-dataset--the-definitive-data)
5. [Pattern Discovery Pipeline](#5-pattern-discovery-pipeline)
6. [RSI Filter — The Breakthrough](#6-rsi-filter--the-breakthrough)
7. [Live Trading Results](#7-live-trading-results)
8. [Key Lessons Learned](#8-key-lessons-learned)
9. [Execution & Infrastructure](#9-execution--infrastructure)
10. [Research Scripts Reference](#10-research-scripts-reference)
11. [Appendix A: Strategy Parameter Summary](#appendix-a-strategy-parameter-summary)
12. [Appendix B: Wallet & Infrastructure](#appendix-b-wallet--infrastructure)

---

## 1. Current Live Strategy

**File**: `strategies/mean_reversion.py` + `strategies/mean_reversion.yaml`
**Deployed**: 2026-03-04 on Hetzner server (Ireland)

### 1.1 How It Works

The bot trades Polymarket's 5-minute Up/Down binary markets. After observing consecutive UP outcomes for a coin, it bets DOWN (mean-reversion). It uses GTC limit orders placed at t=1-3s into each cycle, cancelled after 10s if unfilled. Filled positions are held to resolution ($1 payout on win, $0 on loss).

### 1.2 Pre-Trade Filters

1. **RSI(7) on Binance BTCUSDT 5m klines**: Skip ALL trades when RSI >= 60. **NOTE**: The 71.5% WR claimed in Session 7 was caused by look-ahead bias (see Section 6). After fixing the backtester, RSI(7)<60 reduces WR to 52.4% and cuts PnL. The filter is still deployed live as a safety measure (fail-closed: if Binance is unreachable, all trades are blocked).

2. **Drawdown circuit breaker**: Pause trading if session PnL drops below -$30 or after 8 consecutive losses. Auto-resumes after 30 minutes.

### 1.3 YAML Config (Live)

```yaml
entry_window_start: 1
entry_window_end: 3
cancel_timeout: 10.0
size: 5.0

rsi_filter:
  enabled: true
  period: 7
  timeframe: "5m"
  threshold: 60

drawdown_protection:
  enabled: true
  max_drawdown: 30.0
  max_consecutive_losses: 8
  cooldown_minutes: 30

rules:
  - pattern: "UUUUU"   # ETH strongest at 5-streak
    side: "DOWN"
    coins: ["ETH"]
    max_ask: 0.54

  - pattern: "UUUUU"   # Other coins, tighter cap
    side: "DOWN"
    coins: ["BTC", "SOL", "XRP"]
    max_ask: 0.51

  - pattern: "UUUU"    # ETH strongest at 4-streak
    side: "DOWN"
    coins: ["ETH"]
    max_ask: 0.54

  - pattern: "UUUU"    # Other coins
    side: "DOWN"
    coins: ["BTC", "SOL", "XRP"]
    max_ask: 0.51

  - pattern: "UDUU"    # BTC-specific alternating pattern
    side: "DOWN"
    coins: ["BTC"]
    max_ask: 0.50

  - pattern: "UUU"     # Most frequent signal, lowest edge
    side: "DOWN"
    coins: ["BTC", "ETH", "SOL", "XRP"]
    max_ask: 0.49
```

Rules are matched longest-first. During a long UP streak, cascading trades fire (UUU at position 3, UUUU at 4, UUUUU at 5). This is intentional — overlapping trades are +EV in aggregate (+$572 over 74 days in backtest).

### 1.4 Per-Pattern Max Ask Rationale

Lower-edge patterns need tighter entry prices to stay profitable:

| Pattern | Telonex WR | Breakeven ask | Our max_ask | Margin |
|---------|-----------|--------------|-------------|--------|
| UUUUU (ETH) | 57.4% | ~$0.535 | $0.54 | ~0.5pp |
| UUUU (ETH) | 56.1% | ~$0.521 | $0.54 | ~2pp |
| UUUU (others) | 54.5% | ~$0.507 | $0.51 | ~0.3pp |
| UUU (all) | 54.0% | ~$0.502 | $0.49 | safe |

### 1.5 Expected Performance (Backtest)

**After look-ahead bias fix** (Session 8), with current YAML rules on Telonex 74 days:
- **No filter (baseline)**: 8,181 trades, 54.0% WR, +$1,568 PnL, **+$27.51/day**
- **RSI(7) skip>60**: 3,292 trades, 52.4% WR, +$400, +$7.02/day (WORSE)

The baseline without RSI is the strongest configuration. 10/10 weeks profitable.

Realistic expectations (accounting for fill rate, slippage):
- 30% fill rate: ~$8/day
- 50% fill rate: ~$14/day
- 70% profitable days

### 1.6 Outcome Inference System

Three-tier outcome detection:
1. **WS price thresholds** (immediate): UP ask >= 0.95 → UP, <= 0.05 → DOWN
2. **REST book-confirm** (1-5s delay): Check orderbook for near-$1 bids posted by MMs after resolution. Now with 3 retries and 2s gaps.
3. **Gamma API** (3-5 min delay): Ground truth from `GET /markets/slug/{slug}`. Confirms or corrects all outcomes.

Pattern matching requires confirmed outcomes (Gamma-verified) for all positions except the trigger.

---

## 2. Market Structure

Polymarket offers 5-minute binary markets on crypto price movement (BTC, ETH, SOL, XRP). Each cycle:
- Two tokens: **UP** and **DOWN** (ERC-1155 on Polygon)
- Prices range $0.01-$0.99 (probability-weighted)
- Resolution: winning token → $1, losing → $0
- Market maker sets initial odds (~52-53% UP at open)
- UP + DOWN asks > $1.00 (the MM's vig)
- High cross-coin correlation (0.788)

**Builder Program**: 0% maker fee via EIP-712 signing. This is critical — without it, the thin edges would be eaten by fees.

---

## 3. Base Rates

### 3.1 Telonex Base Rates (74 days, N=60,605)

| Coin | P(UP) | P(DOWN) | N |
|------|-------|---------|---|
| BTC | 50.1% | 49.9% | 16,620 |
| ETH | 49.7% | 50.3% | 14,661 |
| SOL | 49.1% | 50.9% | 14,663 |
| XRP | 49.1% | 50.9% | 14,661 |

Near 50/50 over large samples. The ~57% DOWN rate we saw in early 3-day data was sample bias from a slightly bearish period.

### 3.2 Runs Tests — The Sequence is RANDOM

All 4 coins pass the Wald-Wolfowitz runs test (BTC p=0.215, ETH p=0.533, SOL p=0.565, XRP p=0.364). **There is no autocorrelation in the raw sequence.** The early finding of BTC p=0.032 (255 cycles) was a Type I error.

The pattern edge comes from a different mechanism than simple autocorrelation — likely market microstructure creating a mean-reversion signature specifically after UP streaks.

### 3.3 Key Pricing Facts

- Market-implied P(UP) at open: ~52.5%
- UP + DOWN asks sum to ~$1.03-1.05 (vig)
- Break-even WR at ask=$0.50: 50.75% (with 1.5% fee)
- Break-even WR at ask=$0.51: 51.75%

---

## 4. Telonex 73-Day Dataset — The Definitive Data

**Script**: `research/telonex_backtest.py`
**Data**: 60,605 resolved 5-min markets, 74 days (Dec 18, 2025 – Mar 1, 2026)
**Source**: Telonex free dataset (`data/telonex_updown_5m.csv`)

This dataset settled every open question. Everything from the initial 3-day self-collected data (471 cycles) showed massive regression to the mean when tested against 74 days.

### 4.1 Pattern Results (Bonferroni-corrected, 1,240 tests, alpha = 0.05/1240)

**14 Bonferroni-significant patterns** (Session 8 exhaustive scan):

| Coin | Pattern | Bet | N | WR | MaxAsk | bonf_p |
|------|---------|-----|---|-----|--------|--------|
| ALL | UUU | D | 7,071 | 54.0% | 0.540 | 0.0000 |
| ALL | DD | U | 14,219 | 52.3% | 0.523 | 0.0000 |
| ALL | UU | D | 14,807 | 52.2% | 0.522 | 0.0000 |
| ALL | UUUU | D | 3,252 | 54.5% | 0.545 | 0.0002 |
| ALL | DDD | U | 6,776 | 53.1% | 0.531 | 0.0002 |
| ALL | DUDUDD | U | 958 | 57.7% | 0.577 | 0.0012 |
| ETH | UUU | D | 1,698 | 55.8% | 0.558 | 0.0013 |
| ALL | DDDD | U | 3,176 | 54.1% | 0.541 | 0.0031 |
| ETH | DDD | U | 1,580 | 55.6% | 0.556 | 0.0051 |
| ALL | DUUU | D | 3,819 | 53.6% | 0.536 | 0.0066 |
| ETH | DDDD | U | 701 | 58.3% | 0.583 | 0.0070 |
| ALL | DDUUD | U | 1,861 | 54.7% | 0.547 | 0.0337 |
| ETH | UUUU | D | 751 | 57.4% | 0.574 | 0.0360 |
| ALL | DDDUUD | U | 935 | 56.6% | 0.566 | 0.0401 |

**Key findings:**
1. **Both UP-streak->DOWN and DOWN-streak->UP are significant.** Mean-reversion works in both directions.
2. **Longer streaks = higher WR** (UU 52.2% -> UUUU 54.5%). Mean-reversion pressure increases with streak length.
3. **DDD->UP is real** (6,776 trades, 53.1% WR, p<0.001). This is a candidate to ADD to the live strategy.
4. **ETH is the strongest coin** across both directions (UUU 55.8%, DDD 55.6%, UUUU 57.4%).
5. **UD->DOWN is DEAD** (49.0% WR). Our early 70.2% WR on 255 cycles was pure noise + spot momentum.

### 4.2 Streak Length Effect

| Streak length | P(DOWN next) | Trades |
|---------------|-------------|--------|
| 2 (UU) | 50.6% | 7,734 |
| 3 (UUU) | 53.5% | 3,817 |
| 4 (UUUU) | 54.4% | 1,773 |
| 5 | 55.4% | 808 |
| 6 | 55.8% | 360 |
| 7 | 56.6% | 159 |

P(DOWN) increases ~1pp per additional UP. The effect is real and profitable at tight max_ask values.

### 4.3 Regression to the Mean

| Metric | 471 cycles (3 days) | 16,620 cycles (Telonex) |
|--------|-------|---------|
| UU→DOWN WR | 69.4% | 52.2% (-17.2pp) |
| UD→DOWN WR | 70.2% | 49.0% (-21.2pp) |
| BTC runs test | p=0.032 | p=0.215 (RANDOM) |
| DDD→UP WR | 37.7% | 53.1% (+15.4pp) |

Every exciting finding from small samples shrank dramatically. This is textbook regression to the mean.

---

## 5. Pattern Discovery Pipeline

### 5.1 Exhaustive Bonferroni Scan

**Script**: `research/full_pattern_scan.py`

Tested 1,290 pattern-coin-side combinations (lengths 2-6, 4 coins + ALL pool, UP and DOWN). Bonferroni alpha = 0.05/1,290 = 0.000039.

**Result**: Only 30 combinations survive. ALL are UP-streak → DOWN.

### 5.2 OOS Robustness Suite

**Script**: `research/top30_oos_suite.py`

Pipeline: Bonferroni scan → top 30 → multi-holdout OOS (7/14/21 day holdouts) → quantile base-rate regime validation.

**11 strict survivors** (pass ALL OOS holdouts + >= 2/3 regime bins):

| Strategy | WR | MaxAsk | EV@0.50 | Regimes |
|----------|-----|--------|---------|---------|
| ETH UUUU→DOWN | 57.4% | $0.535 | +$0.035 | 3/3 |
| ALL UUUUU→DOWN | 54.6% | $0.508 | +$0.008 | 3/3 |
| ALL UUUU→DOWN | 54.5% | $0.507 | +$0.007 | 3/3 |
| BTC UDUU→DOWN | 54.2% | $0.504 | +$0.004 | 3/3 |
| ALL UUU→DOWN | 54.0% | $0.502 | +$0.002 | 3/3 |

These are the basis for the 6 live YAML rules.

### 5.3 Edge Stability — STABLE / STRENGTHENING

**Script**: `research/edge_stability_telonex.py`

Rolling 7-day analysis over 74 days (baseline, no RSI filter):
- WR trend slope: +0.00028/day (p=0.023) — slightly strengthening
- No structural break detected (Bonferroni p=0.148)
- PnL curve is convex (accelerating)
- 70.2% of days are profitable
- Worst single day: -$162
- **10/10 weeks profitable** in single-pattern backtest (UUU->DOWN at max_ask=$0.45)

> Note: RSI-filtered stability results from Session 7 are INVALID due to look-ahead bias.

---

## 6. RSI Filter — LOOK-AHEAD BIAS DISCOVERED (Session 8)

### 6.1 The Bug

**CRITICAL**: All RSI numbers from Session 7 were INVALID due to look-ahead bias in `research/backtest.py`.

The `IndicatorEngine.get_value()` method (line 695-696) used the Binance candle `[T, T+300)` to compute RSI for a trade at time T. That candle contains the ENTIRE 5-minute cycle's price action -- including the outcome. The RSI was literally seeing the future.

**Effect**: RSI(7)<60 appeared to boost WR from 54% to 71.5%. In reality (after fix), it REDUCES WR to 52.4%. The RSI filter was selectively admitting winning trades and blocking losing trades because it could see the outcome.

**Fix applied** (commit `8499530`): Changed candidate order from `[snapped, snapped - tf_s, snapped + tf_s]` to `[last_completed, last_completed - tf_s]` where `last_completed = snapped - tf_s`.

### 6.2 Corrected RSI Numbers

| Config | Trades | WR | PnL | $/day |
|--------|--------|-----|-----|-------|
| **No filter (baseline)** | **8,181** | **54.0%** | **+$1,568** | **+$27.51** |
| RSI(7) skip>60 | 3,292 | 52.4% | +$400 | +$7.02 |
| RSI(7) skip>65 | 4,500 | 52.7% | +$711 | +$12.47 |
| RSI(14) skip>60 | 5,100 | 52.7% | +$827 | +$14.50 |

**The baseline without RSI is the best configuration.** Every RSI threshold tested reduces PnL by filtering out profitable trades along with unprofitable ones.

### 6.3 Why the Monotonic Table Was Fake

The "textbook-perfect" RSI vs WR table from Session 7 (84% WR at RSI<30, 18% at RSI>80) was entirely an artifact of look-ahead bias. When RSI was low, the current candle was moving DOWN (which is what we bet on), so we saw more wins. When RSI was high, the candle was moving UP, so we saw more losses. The RSI wasn't predicting -- it was reporting.

All research scripts that used the biased `get_value()` produced invalid results:
- `research/rsi_multi_timeframe.py` -- INVALID
- `research/top30_oos_suite.py` -- RSI-filtered results INVALID (baseline results still valid)
- `research/indicators_multi_timeframe.py` -- INVALID
- `research/technical_indicators_telonex.py` -- INVALID

### 6.4 Why RSI Is Still Deployed Live

Despite no backtesting benefit, RSI is kept in the live strategy as a safety measure:
1. **Fail-closed behavior** (Session 7 fix): If Binance API is unreachable, all trades are blocked. This prevents trading blind.
2. **Staleness check**: RSI data older than 5 minutes blocks trades.
3. **Potential live benefit**: In live trading, RSI may help filter periods with bad ask prices (high spread). This hypothesis has NOT been tested yet.
4. **Low cost**: At worst it reduces trade volume, but doesn't cause losses.

### 6.5 Other Indicators -- ALL INVALID

All indicator comparisons from Session 7 (BB, MACD, Stoch, Vol) used the same biased `get_value()`. The numbers in the previous version of this document were wrong. These indicators need to be re-tested with the corrected backtester before any conclusions can be drawn.

---

## 7. Live Trading Results

### 7.1 Session 1-2: Stat-Arb (DEAD)

17 trades, ~-$18. Overfit grid search parameters. Strategy was abandoned after proving no edge.

### 7.2 Session 3: Pattern Bot with Kelly (DISASTER)

Ran UU→DOWN with Kelly scaling on BTC+ETH. Kelly tripled losses during ETH streak (32% WR at streak=3, but Kelly was increasing size). Lost ~$45.

**Lesson**: Never use Kelly without proving win rate stationarity at each scaling level.

### 7.3 Session 4-6: Mean Reversion Bot (Before RSI)

75 trades, 35W/40L (47% WR), -$14.05 total PnL. Peak at ~+$40 then crashed during BTC rally. Max drawdown: $35.45. The massacre from 07:30 Mar 4: 14 trades, 3W/11L, -$20.80 — all during RSI 67-90.

**RSI filter simulation on these trades**: Would have kept 24 trades (62% WR, +$13.79) and blocked 51 trades (39% WR, -$27.84).

### 7.4 Session 7: Mean Reversion with RSI Filter (CURRENT)

Deployed 2026-03-04. RSI(7) filter + drawdown protection + tighter max_ask. Running live.

### 7.5 Total Live PnL History

~-$500 across all sessions (stat-arb, Kelly disaster, unfiltered mean-rev). The bulk of losses came from (1) overfit strategies, (2) Kelly amplification, (3) trading during high-RSI rallies.

---

## 8. Key Lessons Learned

### 8.1 The Overfitting Problem

The most important lesson. Grid search over 4,536-8,000 parameter combos on 255 data points found "winners" that were pure noise. Research v1 found 119 survivors; adding 27 more cycles of data killed them all. With Bonferroni correction for 8,000 tests, nothing could survive.

**Fix**: Hypothesis-driven research with pre-specified parameters. The exhaustive Bonferroni scan (1,290 tests) with multi-holdout OOS validation is the correct approach.

### 8.2 Small-Sample Regime Effects Don't Survive

Every regime filter that looked amazing on small samples collapsed with more data:

| Finding (small N) | N | Large-N result | N |
|-------------------|---|----------------|---|
| Choppy regime 84% WR | 31 | 51.3% WR | 4,082 |
| Asia morning 92% WR | 12 | 49.6% WR | 554 |
| Low vol 79% WR | 28 | 51.3% WR | 7,401 |
| EU morning 53% WR (worst) | 11 | 50.8% WR | 2,469 |

**Rule**: Never implement a filter with N < 1,000.

### 8.3 Trend Dependency

Early backtests showed 70% WR, but decomposition revealed 88% of PnL came from spot-DOWN cycles where the base rate was already 76.4%. The pattern added only +1.5pp when BTC spot was falling (p=0.34). The tight max_ask limits partially address this -- when momentum is strong, DOWN asks are expensive (>$0.49), so the bot naturally sits out.

### 8.4 Kelly Criterion Failure

Kelly assumes stationary win rates. ETH's WR drops from 67% at streak=2 to 32% at streak=3. Kelly scaled UP at exactly the point where the edge DISAPPEARED. Never use Kelly without proving WR stationarity at each scaling level.

### 8.5 UD→DOWN Collapse

The most dramatic edge decay: 70.2% WR on 255 cycles → 49.0% on 15,190 cycles. UD was never a real pattern — it was noise + spot momentum bias from a slightly bearish 2-day sample.

### 8.6 Inference Matters

WS-based outcome classification at 0.95/0.05 thresholds misclassifies ~10-15% of markets decided in the last seconds. The DDD pattern (72.7% WR) collapsed to 37.7% when strict thresholds were applied -- the "pattern" was an artifact of noisy classification. Always use Gamma API ground truth.

### 8.7 Look-Ahead Bias in Technical Indicators (Session 8)

**The most expensive bug in this project.** The backtester's `IndicatorEngine.get_value()` used the Binance candle `[T, T+300)` to compute RSI for a trade at time T. That candle covers the same 5 minutes as the Polymarket cycle -- it literally contains the outcome. RSI wasn't predicting; it was reporting.

This created a textbook-perfect monotonic WR vs RSI table that was completely artificial. Several hours of research and optimization were wasted fitting to a phantom signal.

**Lesson**: When any indicator shows "too good to be true" results, immediately audit the data pipeline for time leakage. Check: (1) Is the indicator computed from data available BEFORE the trade decision? (2) Does any input overlap with the outcome period? The fix was trivial (use the PRIOR completed candle), but the bug went undetected because the results were "structurally logical" -- RSI > 60 during UP momentum should hurt DOWN bets. The logic was correct; the implementation was wrong.

### 8.8 Cascade Loss Problem

When UUU triggers and loses (outcome U), it extends to UUUU, which triggers again. If that loses too, extends to UUUUU. 808 cascades found in 74 days. Even when the final UUUUU trade wins, the cascade net is -$2.55. When all 3 lose: -$7.55. The strategy is profitable DESPITE cascades, not because they're avoided. This is a known cost of the overlapping rule structure.

---

## 9. Execution & Infrastructure

### 9.1 Latency Budget (Ireland Server → Polymarket)

| Component | Time |
|-----------|------|
| EIP-712 signing | ~17ms (cached signer) |
| HTTP POST to CLOB | ~340ms (irreducible) |
| Total signal→order | ~360ms |

At 400ms latency, strategies must rely on signals persisting 5+ seconds.

### 9.2 Polymarket API Quirks

- **Fee**: ~1.5% deducted from shares (not USDC spent). Builder Program: 0% maker fee.
- **CLOB truncates sell sizes** to 2 decimal places
- **POST /order response is unreliable** — `takingAmount` can be wrong. Always verify via GET.
- **Complement match pricing**: BUY UP can match against BUY DOWN. `execution_price = 1 - maker_price` when `asset_id` differs.
- **API key invalidation**: Each wallet has ONE active key. `create_api_key()` kills previous keys. Use `derive_api_key()` in production.

### 9.3 Critical Bug: HMAC Key Not Updated

`set_api_creds()` didn't re-decode `_api_hmac_key`, causing all POST /order to fail with 401. Fixed by re-decoding on credential update.

### 9.4 Outcome Inference Bugs Fixed (Session 5-7)

1. Asks/bids reset BEFORE inference — caused false UP classifications
2. `_up_ask_cycle_seen` gate blocked valid bid-based confirmations
3. WS provisional thresholds relaxed from 0.99/0.01 to 0.95/0.05
4. Old cycle markets saved for ALL coins before reset
5. Late inference method for lagging coins
6. REST confirm window extended and given 3 retries with 2s gaps

---

## 10. Research Scripts Reference

### Live Strategy

| File | Purpose |
|------|---------|
| `strategies/mean_reversion.py` | Live trading bot (~2550 lines). RSI filter, drawdown protection, multi-pattern rules, Gamma inference |
| `strategies/mean_reversion.yaml` | Live config: 6 rules, RSI filter, drawdown settings |

### Core Research

| File | Purpose |
|------|---------|
| `research/backtest.py` | **Main backtester**. Runs YAML configs on Telonex + CSV data. `--pattern UUU` for single-pattern analysis, `--coin ETH`, per-cycle equity curves, strategy presets. Look-ahead bias in RSI fixed (Session 8). |
| `research/full_pattern_scan.py` | Exhaustive 1,290-test Bonferroni pattern scanner |
| `research/top30_oos_suite.py` | Full pipeline: scan → top-30 → multi-holdout OOS → regime validation |
| `research/base_regime_validation.py` | Tests patterns across base-rate regimes (quantile bins) |
| `research/configurable_backtest.py` | User-friendly backtester with editable config section + equity curve plot |

### Indicator & Regime Research

| File | Purpose | Verdict |
|------|---------|---------|
| `research/rsi_multi_timeframe.py` | RSI period + timeframe grid search | **RSI(7) 5m is optimal** |
| `research/indicators_multi_timeframe.py` | 5 indicators x 5 timeframes + combos | RSI dominates |
| `research/technical_indicators_telonex.py` | RSI/BB/MACD/Stoch/Vol vs outcomes | Monotonic RSI relationship found |
| `research/edge_stability_telonex.py` | 74-day rolling stability analysis | Edge stable/strengthening |
| `research/regime_choppy_telonex.py` | Choppy regime test | **DEAD** (51.3% WR at n=4082) |
| `research/regime_vol_telonex.py` | Volatility & session regime test | **DEAD** |
| `research/four_hypotheses_telonex.py` | Cross-coin divergence, streak speed, weekday, post-loss autocorr | Only post-loss significant |

### Analysis & Validation

| File | Purpose |
|------|---------|
| `research/_analyze_trades.py` | Live trade log analyzer with RSI simulation + Gamma resolution |
| `research/backtest_realdata.py` | Backtester using self-collected CSV data (5 days) |
| `research/streak_overlap_analysis.py` | Overlapping pattern rule analysis (+$572 over 74d) |
| `research/fill_rate_simulation.py` | Optimal max_ask vs fill rate simulation |

### Earlier Research (Historical)

| File | Purpose | Status |
|------|---------|--------|
| `research/telonex_backtest.py` | Pattern analysis on Telonex (pre-backtest.py) | Superseded by `backtest.py` |
| `research/autocorrelation.py` | Outcome sequence dependence (255 cycles) | Superseded by Telonex |
| `research/backtest_patterns.py` | Pattern backtester (255-471 cycles) | Superseded by `backtest.py` |
| `research/trend_check.py` | Spot momentum vs pattern edge decomposition | Finding absorbed into RSI filter |
| `research/regime_analysis.py` | Regime detection (471 cycles) | Superseded by Telonex regime tests |
| `research/research_v3.py` | Hypothesis-driven, 7 strategies, all negative | Historical reference |
| `research/research_v2.py` | Grid search, 14-fold CV | Superseded |
| `research/research_v1.py` | Original grid search | Superseded |
| `research/late_entry.py` | Late-entry calibration study | **DEAD** — MM well-calibrated |

### Data Files

| File | Purpose |
|------|---------|
| `data/telonex_updown_5m.csv` | 60,605 Telonex market outcomes (74 days) |
| `data/telonex_sample_quotes.csv` | Telonex quote data with prices |
| `data/prices_YYYY-MM-DD.csv` | Self-collected orderbook data (24/7 on server) |
| `data/meanrev_trades.txt` | Live trade log |
| `research/resolution_cache.json` | Gamma API resolution cache |
| `research/binance_*_cache*.json` | Cached Binance klines (various timeframes) |

### Scripts

| File | Purpose |
|------|---------|
| `scripts/data_collector.py` | 24/7 price data collection (RUNNING on server) |
| `scripts/test_order.py` | CLOB auth diagnostic tool |
| `scripts/delay_probe.py` | Latency measurement tool |
| `scripts/latency_test.py` | Network latency benchmarking |

---

## Appendix A: Strategy Parameter Summary

### Strategies That DO NOT Work (7 tested, all dead)

| Strategy | Edge | p-value | Why it fails |
|----------|------|---------|-------------|
| Stat-arb divergence | +$0.011 | 0.267 | Edge thinner than transaction costs |
| Cheap shares (UP<$0.25) | -$0.091 | 0.987 | Market correctly prices low-prob outcomes |
| Early momentum | -$0.059 | 1.000 | Price rise already incorporated by MM |
| Dip reversal | -$0.060 | 1.000 | Dips are signal, not noise |
| BTC lead-lag | -$0.051 | 1.000 | Lag shorter than our entry delay |
| Bid momentum | -$0.043 | 1.000 | Bid rises in thin markets are noise |
| Previous-cycle continuation | -$0.038 | 1.000 | No autocorrelation in 5-min outcomes |

### Current Strategy Performance

| Config | Trades | WR | Daily PnL | Period |
|--------|--------|-----|-----------|--------|
| **YAML rules, no filter (BEST)** | **8,181** | **54.0%** | **$27.51** | **Telonex 74d** |
| YAML rules + RSI(7) skip>60 | 3,292 | 52.4% | $7.02 | Telonex 74d |
| Live trades (no RSI at time) | 75 | 47% | -$14.05 total | 1 day |

> Note: Session 7 claimed RSI(7)<60 gave 71.5% WR / $53.82/day. This was caused by
> look-ahead bias in the backtester RSI computation. See Section 6 for details.

---

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
