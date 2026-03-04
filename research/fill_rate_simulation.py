"""
fill_rate_simulation.py - Simulate fill rates and expected PnL at different max_ask prices.

Key question: the live strategy places GTC limit BUY orders for DOWN tokens at
max_ask price (currently $0.51 for most rules, $0.54 for ETH). What happens if
we raise max_ask to $0.52, $0.53, etc.?

Higher max_ask = more fills (higher throughput) but lower EV per trade.
We need to find the OPTIMAL max_ask that maximizes EV per opportunity.

Data: telonex_updown_5m.parquet (60,605 resolved 5-min markets, 74 days)
      telonex_sample_quotes_incycle.parquet (18K quote snapshots for one market)

Since we have outcomes for all 60K markets but ask prices for only one sample
market, we model the DOWN ask distribution at cycle start from:
  1. The binary market relationship: DOWN_ask ~ 1 - UP_ask
  2. Empirical observation: after UP streaks, DOWN is typically priced 0.48-0.55
  3. The sample quote data to calibrate the spread distribution

Approach:
  - For each pattern (UUU, UUUU, UUUUU), compute the unconditional WR from data
  - Model DOWN ask at entry as a distribution (the market-maker's price)
  - For each max_ask threshold, compute fill rate, conditional WR, and EV
  - Find the optimal max_ask that maximizes EV per opportunity

Usage:
    python research/fill_rate_simulation.py
"""

import re
import sys
import warnings
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from scipy.stats import binomtest, norm

warnings.filterwarnings("ignore", category=FutureWarning)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATA_FILE = Path(__file__).parent.parent / "data" / "telonex_updown_5m.parquet"
QUOTES_FILE = Path(__file__).parent.parent / "data" / "telonex_sample_quotes_incycle.parquet"

MAX_ASK_THRESHOLDS = [
    0.44, 0.45, 0.46, 0.47, 0.48, 0.49, 0.50, 0.51,
    0.52, 0.53, 0.54, 0.55, 0.56, 0.58, 0.60,
]
COINS = ["BTC", "ETH", "SOL", "XRP"]

# Estimated pattern frequency per day across 4 coins (from data analysis)
# We'll compute these from the data itself
DATASET_DAYS = 74  # approximate span of the Telonex dataset


# ---------------------------------------------------------------------------
# 1. Load and prepare outcome data
# ---------------------------------------------------------------------------
def load_outcome_data() -> pd.DataFrame:
    """Load Telonex resolved markets."""
    if not DATA_FILE.exists():
        print(f"ERROR: {DATA_FILE} not found.")
        sys.exit(1)

    df = pd.read_parquet(DATA_FILE)
    df = df[df["result_id"].isin(["0", "1"])].copy()

    df["coin"] = df["slug"].str.extract(r"^(\w+)-updown-5m-")[0].str.upper()
    df["unix_ts"] = df["slug"].str.extract(r"-(\d+)$")[0].astype(float)
    df["outcome"] = df["result_id"].map({"0": "U", "1": "D"})

    df = df.dropna(subset=["coin"])
    return df.sort_values(["coin", "unix_ts"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 2. Analyze quote data to model DOWN ask distribution
# ---------------------------------------------------------------------------
def analyze_quote_distribution():
    """Analyze the sample quotes to understand DOWN ask price behavior."""
    if not QUOTES_FILE.exists():
        print(f"WARNING: {QUOTES_FILE} not found. Using theoretical model only.")
        return None

    df = pd.read_parquet(QUOTES_FILE)

    # Focus on in-cycle quotes (secs >= 0)
    in_cycle = df[df["secs"] >= 0].copy()

    # Focus on early-cycle quotes (0-10s) where our bot would place orders
    early = in_cycle[(in_cycle["secs"] >= 0) & (in_cycle["secs"] <= 10)]

    print("=" * 80)
    print("QUOTE DATA ANALYSIS (calibrating DOWN ask distribution)")
    print("=" * 80)
    print(f"\nData source: {QUOTES_FILE}")
    print(f"Total quotes: {len(df):,}")
    print(f"In-cycle quotes (secs >= 0): {len(in_cycle):,}")
    print(f"Early-cycle quotes (0-10s): {len(early):,}")

    print(f"\nColumns: {df.columns.tolist()}")

    print(f"\nDOWN ask price stats (in-cycle):")
    ask_vals = in_cycle["ask"].dropna()
    print(f"  Mean:   {ask_vals.mean():.4f}")
    print(f"  Median: {ask_vals.median():.4f}")
    print(f"  Std:    {ask_vals.std():.4f}")
    print(f"  Min:    {ask_vals.min():.4f}")
    print(f"  Max:    {ask_vals.max():.4f}")

    if len(early) > 0:
        early_ask = early["ask"].dropna()
        print(f"\nDOWN ask price stats (0-10s):")
        print(f"  Mean:   {early_ask.mean():.4f}")
        print(f"  Median: {early_ask.median():.4f}")
        print(f"  Std:    {early_ask.std():.4f}")

    # Distribution of in-cycle ask prices
    print(f"\nDOWN ask distribution (in-cycle):")
    bins = [0.0, 0.40, 0.44, 0.46, 0.48, 0.49, 0.50, 0.51, 0.52,
            0.53, 0.54, 0.55, 0.56, 0.58, 0.60, 0.70, 1.00]
    for i in range(len(bins) - 1):
        lo, hi = bins[i], bins[i + 1]
        cnt = ((ask_vals >= lo) & (ask_vals < hi)).sum()
        pct = cnt / len(ask_vals) * 100
        bar = "#" * int(pct / 2)
        print(f"  [{lo:.2f}, {hi:.2f}): {cnt:>5} ({pct:5.1f}%)  {bar}")

    # Also load CSV price data for calibration
    csv_files = list((Path(__file__).parent.parent / "data").glob("prices_*.csv"))
    if csv_files:
        csv_dfs = [pd.read_csv(f) for f in csv_files]
        csv_df = pd.concat(csv_dfs, ignore_index=True)
        csv_early = csv_df[
            (csv_df["seconds_elapsed"] >= 1) & (csv_df["seconds_elapsed"] <= 5)
        ]
        print(f"\n--- CSV PRICE DATA CALIBRATION ({len(csv_files)} files) ---")
        print(f"Early-cycle rows (1-5s): {len(csv_early):,}")
        for coin in ["btc", "eth", "sol", "xrp"]:
            col = f"{coin}_up_ask"
            if col in csv_early.columns:
                up_ask = csv_early[col].dropna()
                down_est = 1.0 - up_ask
                print(f"  {coin.upper()} DOWN ask (1 - UP_ask):"
                      f" mean={down_est.mean():.3f},"
                      f" median={down_est.median():.3f},"
                      f" std={down_est.std():.3f},"
                      f" range=[{down_est.min():.2f}, {down_est.max():.2f}]")

    return in_cycle


# ---------------------------------------------------------------------------
# 3. Extract pattern signals with outcomes
# ---------------------------------------------------------------------------
def extract_pattern_signals(df: pd.DataFrame, patterns: Dict[str, int]) -> Dict:
    """Extract all pattern occurrences with their outcomes.

    Args:
        df: DataFrame with 'coin', 'unix_ts', 'outcome' columns
        patterns: dict of pattern_name -> prefix_length (e.g., {"UUU": 3})

    Returns:
        dict of pattern_name -> list of (coin, idx, is_down_win)
    """
    signals = {pat: [] for pat in patterns}

    for coin in sorted(df["coin"].unique()):
        cdf = df[df["coin"] == coin].reset_index(drop=True)
        outcomes = cdf["outcome"].values
        n = len(outcomes)

        for pat_name, prefix_len in patterns.items():
            prefix = ["U"] * prefix_len
            for i in range(prefix_len, n):
                # Check if the last prefix_len outcomes before position i are all U
                match = True
                for j in range(prefix_len):
                    if outcomes[i - prefix_len + j] != "U":
                        match = False
                        break
                if not match:
                    continue
                # Record whether position i was DOWN (the target)
                is_down = 1 if outcomes[i] == "D" else 0
                signals[pat_name].append((coin, i, is_down))

    return signals


# ---------------------------------------------------------------------------
# 4. Model DOWN ask price distribution after UP streaks
# ---------------------------------------------------------------------------
def model_down_ask_after_streaks(streak_len: int) -> Tuple[float, float]:
    """Model the DOWN ask price distribution after an UP streak.

    CALIBRATION from empirical data (2 days of collected CSV prices):
    - Overall DOWN ask at cycle start (1-5s): mean=0.495, median=0.49, std=0.065
    - This is across ALL cycles (not conditioned on prior outcomes)
    - After UP streaks, market makers price DOWN cheaper (lower ask)
      because the crowd assumes momentum will continue

    We estimate the shift per additional UP in the streak:
    - The unconditional DOWN ask ~ N(0.495, 0.065) at cycle start
    - After 3 consecutive UPs: DOWN ask shifts ~2c lower (crowd expects UP)
    - After 4 consecutive UPs: DOWN ask shifts ~3c lower
    - After 5 consecutive UPs: DOWN ask shifts ~5c lower

    The spread/liquidity also tightens after streaks (smaller sigma)
    because market makers have higher conviction on the direction.

    We model DOWN_ask ~ Normal(mu, sigma) truncated to [0.20, 0.80].
    """
    # Calibrated from: 1 - UP_ask at cycle start (1-5s) across BTC/ETH/SOL/XRP
    # Base: mean=0.495, std=0.065 (unconditional)
    # After UP streaks, DOWN gets cheaper (lower mu) and spread tightens
    if streak_len <= 2:
        mu, sigma = 0.495, 0.065
    elif streak_len == 3:
        mu, sigma = 0.475, 0.060  # ~2c lower than unconditional
    elif streak_len == 4:
        mu, sigma = 0.465, 0.055  # ~3c lower
    elif streak_len >= 5:
        mu, sigma = 0.450, 0.050  # ~4.5c lower
    return mu, sigma


def simulate_fill_probabilities(
    mu: float, sigma: float, thresholds: List[float],
    n_samples: int = 100_000,
) -> Dict[float, float]:
    """Simulate DOWN ask prices and compute P(ask <= threshold) for each threshold.

    Returns dict of threshold -> fill_probability.
    """
    np.random.seed(42)
    asks = np.random.normal(mu, sigma, n_samples)
    asks = np.clip(asks, 0.20, 0.80)

    fill_probs = {}
    for t in thresholds:
        fill_probs[t] = (asks <= t).mean()

    return fill_probs


# ---------------------------------------------------------------------------
# 5. Core analysis: EV computation at different max_ask levels
# ---------------------------------------------------------------------------
def analyze_pattern_ev(
    pat_name: str,
    signals: List[Tuple],
    streak_len: int,
    thresholds: List[float],
    total_days: float,
) -> pd.DataFrame:
    """Compute EV per trade, EV per opportunity, and monthly PnL for a pattern.

    The key insight: we model two independent aspects:
      1. Fill probability: P(DOWN_ask <= max_ask) - from the ask distribution model
      2. Win rate: P(DOWN wins | we bet DOWN after this pattern) - from outcome data

    The WR is the UNCONDITIONAL WR for the pattern (same regardless of entry price).
    The EV per trade depends on the entry price (max_ask).
    The EV per opportunity = fill_rate * EV_per_trade.

    We also compute a conservative scenario where WR degrades slightly at higher
    max_ask (adverse selection: when ask is high, market may be pricing in info).
    """
    n_total = len(signals)
    n_wins = sum(hit for _, _, hit in signals)
    wr_unconditional = n_wins / n_total if n_total > 0 else 0.5

    # Signal frequency
    signals_per_day = n_total / total_days if total_days > 0 else 0
    signals_per_month = signals_per_day * 30.44  # average days per month

    # Model ask distribution
    mu, sigma = model_down_ask_after_streaks(streak_len)
    fill_probs = simulate_fill_probabilities(mu, sigma, thresholds)

    # Per-coin breakdown for WR
    coin_wrs = {}
    for coin in COINS:
        coin_signals = [(c, i, h) for c, i, h in signals if c == coin]
        if len(coin_signals) > 0:
            coin_wrs[coin] = sum(h for _, _, h in coin_signals) / len(coin_signals)

    rows = []
    for max_ask in thresholds:
        fill_rate = fill_probs[max_ask]

        # --- Base case: WR is constant regardless of entry price ---
        wr = wr_unconditional

        # EV per filled trade (worst-case: fill exactly at max_ask)
        # Win: receive $1.00, paid max_ask -> profit = 1 - max_ask
        # Lose: paid max_ask, receive $0 -> loss = max_ask
        ev_per_trade = wr * (1.0 - max_ask) - (1.0 - wr) * max_ask

        # EV per signal opportunity = P(fill) * EV(if filled)
        ev_per_opp = fill_rate * ev_per_trade

        # Monthly projections
        monthly_opps = signals_per_month
        monthly_fills = monthly_opps * fill_rate
        monthly_ev = monthly_opps * ev_per_opp

        # --- Conservative case: WR degrades at higher entry prices ---
        # Rationale: adverse selection - when DOWN ask is high, market
        # may be pricing in real information (DOWN is actually likely)
        # Model: WR drops 0.5% per cent above 0.50
        wr_conservative = wr - max(0, (max_ask - 0.50)) * 0.5
        wr_conservative = max(wr_conservative, 0.50)  # floor at 50%
        ev_conservative = wr_conservative * (1.0 - max_ask) - (1.0 - wr_conservative) * max_ask
        ev_opp_conservative = fill_rate * ev_conservative

        # --- Optimistic case: WR is slightly better at lower ask ---
        # When DOWN is cheap (ask < 0.50), market underprices it after UP streaks
        wr_optimistic = wr + max(0, (0.50 - max_ask)) * 0.3
        wr_optimistic = min(wr_optimistic, 0.65)  # cap
        ev_optimistic = wr_optimistic * (1.0 - max_ask) - (1.0 - wr_optimistic) * max_ask
        ev_opp_optimistic = fill_rate * ev_optimistic

        rows.append({
            "max_ask": max_ask,
            "fill_rate": fill_rate,
            "wr_base": wr,
            "ev_per_trade": ev_per_trade,
            "ev_per_opp": ev_per_opp,
            "monthly_fills": monthly_fills,
            "monthly_ev_base": monthly_ev,
            "wr_conservative": wr_conservative,
            "ev_per_trade_conservative": ev_conservative,
            "ev_per_opp_conservative": ev_opp_conservative,
            "monthly_ev_conservative": monthly_opps * ev_opp_conservative,
            "wr_optimistic": wr_optimistic,
            "ev_per_trade_optimistic": ev_optimistic,
            "ev_per_opp_optimistic": ev_opp_optimistic,
            "monthly_ev_optimistic": monthly_opps * ev_opp_optimistic,
        })

    result = pd.DataFrame(rows)
    return result, {
        "n_total": n_total,
        "n_wins": n_wins,
        "wr": wr_unconditional,
        "signals_per_day": signals_per_day,
        "signals_per_month": signals_per_month,
        "ask_mu": mu,
        "ask_sigma": sigma,
        "coin_wrs": coin_wrs,
    }


# ---------------------------------------------------------------------------
# 6. Sensitivity analysis: vary the ask distribution parameters
# ---------------------------------------------------------------------------
def sensitivity_analysis(
    wr: float,
    thresholds: List[float],
    streak_len: int,
    signals_per_month: float,
) -> pd.DataFrame:
    """Run the EV analysis across a range of ask distribution assumptions.

    Tests different (mu, sigma) pairs to see how sensitive the optimal
    max_ask is to our assumptions about the DOWN ask distribution.
    """
    base_mu, base_sigma = model_down_ask_after_streaks(streak_len)

    scenarios = {
        "tight_low":   (base_mu - 0.02, base_sigma - 0.01),
        "base":        (base_mu, base_sigma),
        "tight_high":  (base_mu + 0.02, base_sigma - 0.01),
        "wide_low":    (base_mu - 0.02, base_sigma + 0.01),
        "wide_high":   (base_mu + 0.02, base_sigma + 0.01),
        "very_tight":  (0.50, 0.02),
        "centered_50": (0.50, 0.04),
    }

    rows = []
    for scenario_name, (mu, sigma) in scenarios.items():
        fill_probs = simulate_fill_probabilities(mu, sigma, thresholds)

        best_ev_opp = -999
        best_threshold = 0
        for t in thresholds:
            ev_per_trade = wr * (1.0 - t) - (1.0 - wr) * t
            ev_per_opp = fill_probs[t] * ev_per_trade
            if ev_per_opp > best_ev_opp:
                best_ev_opp = ev_per_opp
                best_threshold = t

        fill_at_best = fill_probs[best_threshold]
        monthly_ev = signals_per_month * best_ev_opp

        rows.append({
            "scenario": scenario_name,
            "mu": mu,
            "sigma": sigma,
            "optimal_max_ask": best_threshold,
            "fill_rate_at_optimal": fill_at_best,
            "ev_per_opp": best_ev_opp,
            "monthly_ev": monthly_ev,
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 7. Per-coin analysis
# ---------------------------------------------------------------------------
def per_coin_analysis(
    df: pd.DataFrame,
    pattern_name: str,
    prefix_len: int,
    thresholds: List[float],
    total_days: float,
):
    """Run the analysis per-coin to find coin-specific optimal max_ask."""
    results = {}
    for coin in COINS:
        cdf = df[df["coin"] == coin].reset_index(drop=True)
        outcomes = cdf["outcome"].values
        n = len(outcomes)

        signals = []
        prefix = ["U"] * prefix_len
        for i in range(prefix_len, n):
            match = True
            for j in range(prefix_len):
                if outcomes[i - prefix_len + j] != "U":
                    match = False
                    break
            if not match:
                continue
            is_down = 1 if outcomes[i] == "D" else 0
            signals.append((coin, i, is_down))

        if len(signals) == 0:
            continue

        result_df, meta = analyze_pattern_ev(
            f"{pattern_name}_{coin}",
            signals,
            prefix_len,
            thresholds,
            total_days,
        )
        results[coin] = (result_df, meta)

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 80)
    print("FILL RATE & MAX_ASK OPTIMIZATION SIMULATION")
    print("Finding the optimal entry price for DOWN token GTC limit orders")
    print("=" * 80)

    # -----------------------------------------------------------------------
    # Step 1: Load and explore data
    # -----------------------------------------------------------------------
    print("\n--- STEP 1: Loading data ---")
    df = load_outcome_data()
    print(f"Loaded {len(df):,} resolved markets")
    print(f"Coins: {df['coin'].value_counts().to_dict()}")

    # Compute total days from data
    min_ts = df["unix_ts"].min()
    max_ts = df["unix_ts"].max()
    total_days = (max_ts - min_ts) / 86400
    print(f"Date range: {total_days:.1f} days")

    # -----------------------------------------------------------------------
    # Step 2: Analyze quote data for calibration
    # -----------------------------------------------------------------------
    print("\n")
    quote_data = analyze_quote_distribution()

    # -----------------------------------------------------------------------
    # Step 3: Extract pattern signals
    # -----------------------------------------------------------------------
    print("\n\n--- STEP 3: Extracting pattern signals ---")
    patterns = {
        "UUU": 3,
        "UUUU": 4,
        "UUUUU": 5,
    }
    signals = extract_pattern_signals(df, patterns)

    for pat_name, sigs in signals.items():
        n_total = len(sigs)
        n_wins = sum(h for _, _, h in sigs)
        wr = n_wins / n_total if n_total > 0 else 0
        per_day = n_total / total_days
        per_month = per_day * 30.44

        # Binomial test vs 50%
        btest = binomtest(n_wins, n_total, 0.5, alternative="greater")

        print(f"\n  {pat_name} -> DOWN:")
        print(f"    Total signals: {n_total:,}")
        print(f"    DOWN wins:     {n_wins:,}")
        print(f"    Win rate:      {wr:.4f} ({wr:.1%})")
        print(f"    p-value (>50%): {btest.pvalue:.6f}")
        print(f"    Per day:       {per_day:.1f}")
        print(f"    Per month:     {per_month:.0f}")

        # Per-coin breakdown
        for coin in COINS:
            coin_sigs = [(c, i, h) for c, i, h in sigs if c == coin]
            if len(coin_sigs) > 0:
                coin_wr = sum(h for _, _, h in coin_sigs) / len(coin_sigs)
                print(f"    {coin:>4}: N={len(coin_sigs):>5}, WR={coin_wr:.3f}")

    # -----------------------------------------------------------------------
    # Step 4: Main analysis - EV at different max_ask levels
    # -----------------------------------------------------------------------
    for pat_name, prefix_len in patterns.items():
        print("\n\n" + "=" * 80)
        print(f"PATTERN: {pat_name} -> DOWN")
        print("=" * 80)

        sigs = signals[pat_name]
        result_df, meta = analyze_pattern_ev(
            pat_name, sigs, prefix_len, MAX_ASK_THRESHOLDS, total_days,
        )

        print(f"\nUnconditional WR: {meta['wr']:.4f} ({meta['wr']:.1%})")
        print(f"Signals per day:  {meta['signals_per_day']:.1f}")
        print(f"Signals per month: {meta['signals_per_month']:.0f}")
        print(f"Modeled DOWN ask: N({meta['ask_mu']:.2f}, {meta['ask_sigma']:.2f})")

        # Display main results table
        print(f"\n{'max_ask':>8} {'fill%':>7} {'WR':>6} {'EV/trade':>10} "
              f"{'EV/opp':>10} {'fills/mo':>9} {'PnL/mo':>9} "
              f"{'conserv':>9} {'optimist':>9}")
        print("-" * 90)

        best_ev_opp = result_df["ev_per_opp"].max()
        best_row = result_df.loc[result_df["ev_per_opp"].idxmax()]

        for _, row in result_df.iterrows():
            marker = " <-- OPTIMAL" if row["ev_per_opp"] == best_ev_opp else ""
            # Monthly PnL assumes $5 per trade
            size = 5.0
            print(f"  ${row['max_ask']:.2f}  {row['fill_rate']:>6.1%}  "
                  f"{row['wr_base']:.3f}  "
                  f"${row['ev_per_trade']*size:>+8.4f}  "
                  f"${row['ev_per_opp']*size:>+8.4f}  "
                  f"{row['monthly_fills']:>8.0f}  "
                  f"${row['monthly_ev_base']*size:>+7.2f}  "
                  f"${row['monthly_ev_conservative']*size:>+7.2f}  "
                  f"${row['monthly_ev_optimistic']*size:>+7.2f}"
                  f"{marker}")

        print(f"\n  * OPTIMAL max_ask = ${best_row['max_ask']:.2f}")
        print(f"    Fill rate: {best_row['fill_rate']:.1%}")
        print(f"    EV per trade ($5): ${best_row['ev_per_trade']*5:+.4f}")
        print(f"    EV per opportunity ($5): ${best_row['ev_per_opp']*5:+.4f}")
        print(f"    Projected monthly fills: {best_row['monthly_fills']:.0f}")
        print(f"    Projected monthly PnL ($5 size): ${best_row['monthly_ev_base']*5:+.2f}")

        # Current config comparison
        current = 0.51
        if pat_name == "UUUUU":
            current_eth = 0.54
        elif pat_name == "UUUU":
            current_eth = 0.54
        else:
            current_eth = 0.51

        current_row = result_df[result_df["max_ask"] == current]
        if not current_row.empty:
            cr = current_row.iloc[0]
            print(f"\n  * CURRENT CONFIG (max_ask=${current:.2f}):")
            print(f"    Fill rate: {cr['fill_rate']:.1%}")
            print(f"    EV per opp ($5): ${cr['ev_per_opp']*5:+.4f}")
            print(f"    Monthly PnL ($5): ${cr['monthly_ev_base']*5:+.2f}")
            delta = (best_row["monthly_ev_base"] - cr["monthly_ev_base"]) * 5
            print(f"    vs. Optimal: ${delta:+.2f}/month ({delta/max(abs(cr['monthly_ev_base']*5), 0.01):+.0%})")

        # ---------------------------------------------------------------
        # Sensitivity analysis
        # ---------------------------------------------------------------
        print(f"\n  --- Sensitivity to ask distribution assumptions ---")
        sens_df = sensitivity_analysis(
            meta["wr"], MAX_ASK_THRESHOLDS, prefix_len, meta["signals_per_month"],
        )
        print(f"  {'Scenario':>15} {'mu':>6} {'sigma':>6} {'Opt.ask':>8} {'Fill%':>7} "
              f"{'EV/opp':>10} {'PnL/mo':>9}")
        print("  " + "-" * 70)
        for _, row in sens_df.iterrows():
            print(f"  {row['scenario']:>15} {row['mu']:.3f} {row['sigma']:.3f} "
                  f"${row['optimal_max_ask']:.2f}  {row['fill_rate_at_optimal']:.1%}  "
                  f"${row['ev_per_opp']*5:>+8.4f}  "
                  f"${row['monthly_ev']*5:>+7.2f}")

        # Range of optimal max_ask across scenarios
        opt_min = sens_df["optimal_max_ask"].min()
        opt_max = sens_df["optimal_max_ask"].max()
        print(f"\n  Optimal max_ask range across scenarios: ${opt_min:.2f} - ${opt_max:.2f}")

    # -----------------------------------------------------------------------
    # Step 5: Per-coin optimal max_ask
    # -----------------------------------------------------------------------
    print("\n\n" + "=" * 80)
    print("PER-COIN OPTIMAL MAX_ASK")
    print("=" * 80)

    for pat_name, prefix_len in patterns.items():
        print(f"\n--- {pat_name} -> DOWN ---")
        coin_results = per_coin_analysis(df, pat_name, prefix_len, MAX_ASK_THRESHOLDS, total_days)

        print(f"  {'Coin':>5} {'N':>6} {'WR':>7} {'Opt.ask':>8} {'Fill%':>7} "
              f"{'EV/opp':>10} {'PnL/mo':>9}")
        print("  " + "-" * 60)

        for coin in COINS:
            if coin not in coin_results:
                continue
            rdf, meta = coin_results[coin]
            best = rdf.loc[rdf["ev_per_opp"].idxmax()]
            print(f"  {coin:>5} {meta['n_total']:>6} {meta['wr']:>6.3f}  "
                  f"${best['max_ask']:.2f}  {best['fill_rate']:.1%}  "
                  f"${best['ev_per_opp']*5:>+8.4f}  "
                  f"${best['monthly_ev_base']*5:>+7.2f}")

    # -----------------------------------------------------------------------
    # Step 6: Aggregate portfolio analysis
    # -----------------------------------------------------------------------
    print("\n\n" + "=" * 80)
    print("PORTFOLIO ANALYSIS: ALL PATTERNS COMBINED")
    print("=" * 80)
    print("\nAssumption: bot runs all 3 patterns (UUU, UUUU, UUUUU)")
    print("UUUU and UUUUU signals are subsets of UUU signals.")
    print("With priority ordering, UUUUU > UUUU > UUU (longest match wins).\n")

    # For portfolio, we need non-overlapping signals:
    # UUUUU signals are a subset of UUUU which is a subset of UUU
    # With priority: UUUUU matches consume those opps, UUUU gets the rest, UUU gets the rest
    # Net signals = UUU signals (which includes UUUU and UUUUU)
    # But the max_ask differs per pattern level

    # Build non-overlapping signal counts
    n_uuuuu = len(signals["UUUUU"])
    n_uuuu_only = len(signals["UUUU"]) - n_uuuuu  # UUUU but not UUUUU
    n_uuu_only = len(signals["UUU"]) - len(signals["UUUU"])  # UUU but not UUUU

    print(f"Signal decomposition (across all 4 coins):")
    print(f"  UUUUU signals:     {n_uuuuu:>6}")
    print(f"  UUUU-only signals: {n_uuuu_only:>6}")
    print(f"  UUU-only signals:  {n_uuu_only:>6}")
    print(f"  Total unique:      {len(signals['UUU']):>6}")

    # Compute WR for each non-overlapping group
    uuuuu_sigs = signals["UUUUU"]
    uuuuu_set = set((c, i) for c, i, h in uuuuu_sigs)

    uuuu_only_sigs = [(c, i, h) for c, i, h in signals["UUUU"]
                       if (c, i) not in uuuuu_set]
    uuuu_set = set((c, i) for c, i, h in signals["UUUU"])

    uuu_only_sigs = [(c, i, h) for c, i, h in signals["UUU"]
                      if (c, i) not in uuuu_set]

    def group_wr(sigs):
        if not sigs:
            return 0.0
        return sum(h for _, _, h in sigs) / len(sigs)

    wr_uuuuu = group_wr(uuuuu_sigs)
    wr_uuuu_only = group_wr(uuuu_only_sigs)
    wr_uuu_only = group_wr(uuu_only_sigs)

    print(f"\nWin rates by group:")
    print(f"  UUUUU:     WR={wr_uuuuu:.3f} (N={len(uuuuu_sigs)})")
    print(f"  UUUU-only: WR={wr_uuuu_only:.3f} (N={len(uuuu_only_sigs)})")
    print(f"  UUU-only:  WR={wr_uuu_only:.3f} (N={len(uuu_only_sigs)})")

    # For each max_ask configuration, compute total portfolio EV
    print(f"\n--- Portfolio EV with UNIFORM max_ask across all patterns ---")
    print(f"{'max_ask':>8} {'UUUUU_ev':>10} {'UUUU_ev':>10} {'UUU_ev':>10} "
          f"{'total_ev':>10} {'fills/mo':>9} {'PnL/mo':>9}")
    print("-" * 75)

    best_total_ev = -999
    best_total_ask = 0

    for max_ask in MAX_ASK_THRESHOLDS:
        total_monthly_ev = 0
        total_monthly_fills = 0
        parts = []

        for group_name, group_sigs, streak_len in [
            ("UUUUU", uuuuu_sigs, 5),
            ("UUUU-only", uuuu_only_sigs, 4),
            ("UUU-only", uuu_only_sigs, 3),
        ]:
            if not group_sigs:
                parts.append(0)
                continue
            n = len(group_sigs)
            wr = sum(h for _, _, h in group_sigs) / n
            mu, sigma = model_down_ask_after_streaks(streak_len)
            fill_probs = simulate_fill_probabilities(mu, sigma, [max_ask])
            fill_rate = fill_probs[max_ask]

            ev_per_trade = wr * (1.0 - max_ask) - (1.0 - wr) * max_ask
            ev_per_opp = fill_rate * ev_per_trade
            monthly_opps = n / total_days * 30.44
            monthly_ev = monthly_opps * ev_per_opp
            monthly_fills = monthly_opps * fill_rate

            parts.append(monthly_ev)
            total_monthly_ev += monthly_ev
            total_monthly_fills += monthly_fills

        size = 5.0
        if total_monthly_ev > best_total_ev:
            best_total_ev = total_monthly_ev
            best_total_ask = max_ask

        marker = " <-- BEST" if max_ask == best_total_ask and total_monthly_ev == best_total_ev else ""
        print(f"  ${max_ask:.2f}  "
              f"${parts[0]*size:>+8.4f}  "
              f"${parts[1]*size:>+8.4f}  "
              f"${parts[2]*size:>+8.4f}  "
              f"${total_monthly_ev*size:>+8.4f}  "
              f"{total_monthly_fills:>8.0f}  "
              f"${total_monthly_ev*size:>+7.2f}{marker}")

    print(f"\n  OPTIMAL UNIFORM max_ask = ${best_total_ask:.2f}")
    print(f"  Monthly PnL ($5 trades): ${best_total_ev*5:+.2f}")

    # -----------------------------------------------------------------------
    # Step 7: Recommended config with per-pattern max_ask
    # -----------------------------------------------------------------------
    print("\n\n" + "=" * 80)
    print("RECOMMENDED CONFIG (per-pattern max_ask)")
    print("=" * 80)

    # Find optimal per-pattern
    recs = {}
    for pat_name, prefix_len in patterns.items():
        sigs = signals[pat_name]
        result_df, meta = analyze_pattern_ev(
            pat_name, sigs, prefix_len, MAX_ASK_THRESHOLDS, total_days,
        )
        best = result_df.loc[result_df["ev_per_opp"].idxmax()]
        recs[pat_name] = {
            "max_ask": best["max_ask"],
            "fill_rate": best["fill_rate"],
            "wr": meta["wr"],
            "ev_per_opp": best["ev_per_opp"],
            "monthly_fills": best["monthly_fills"],
            "monthly_ev": best["monthly_ev_base"],
        }

    total_monthly_ev = 0
    print(f"\n  {'Pattern':>8} {'Opt.ask':>8} {'Fill%':>7} {'WR':>6} "
          f"{'EV/opp':>10} {'fills/mo':>9} {'PnL/mo($5)':>12}")
    print("  " + "-" * 70)
    for pat_name in ["UUUUU", "UUUU", "UUU"]:
        r = recs[pat_name]
        pnl = r["monthly_ev"] * 5
        total_monthly_ev += pnl
        print(f"  {pat_name:>8} ${r['max_ask']:.2f}  {r['fill_rate']:.1%}  "
              f"{r['wr']:.3f}  ${r['ev_per_opp']*5:>+8.4f}  "
              f"{r['monthly_fills']:>8.0f}  ${pnl:>+10.2f}")

    print(f"\n  NOTE: UUUU and UUUUU signals overlap with UUU.")
    print(f"  The portfolio-level PnL (accounting for overlaps) is shown above")
    print(f"  in the 'Portfolio Analysis' section.")

    # -----------------------------------------------------------------------
    # Step 8: Comparison with current config
    # -----------------------------------------------------------------------
    print("\n\n" + "=" * 80)
    print("CURRENT CONFIG vs RECOMMENDED")
    print("=" * 80)

    current_config = {
        "UUUUU_ETH": 0.54,
        "UUUUU_other": 0.51,
        "UUUU_ETH": 0.54,
        "UUUU_other": 0.51,
        "UUU_all": 0.51,
    }

    print(f"\n  Current config (from mean_reversion.yaml):")
    for label, ask in current_config.items():
        print(f"    {label}: ${ask:.2f}")

    print(f"\n  Recommendations per pattern (all-coin):")
    for pat_name in ["UUUUU", "UUUU", "UUU"]:
        r = recs[pat_name]
        print(f"    {pat_name}: ${r['max_ask']:.2f}  "
              f"(fill={r['fill_rate']:.0%}, EV/opp=${r['ev_per_opp']*5:+.4f})")

    print("\n  KEY INSIGHT:")
    print("  The optimal max_ask balances fill rate vs per-trade EV.")
    print("  Raising max_ask increases fills but lowers per-trade profit.")
    print("  The sweet spot depends on the DOWN ask distribution at cycle start.")

    # -----------------------------------------------------------------------
    # Step 9: Break-even analysis
    # -----------------------------------------------------------------------
    print("\n\n" + "=" * 80)
    print("BREAK-EVEN ANALYSIS")
    print("=" * 80)
    print("\nAt what max_ask does EV per trade become zero?")
    print("EV = 0 when: WR * (1 - max_ask) = (1 - WR) * max_ask")
    print("           => max_ask = WR")
    print()

    for pat_name, prefix_len in patterns.items():
        sigs = signals[pat_name]
        n_total = len(sigs)
        n_wins = sum(h for _, _, h in sigs)
        wr = n_wins / n_total
        btest = binomtest(n_wins, n_total, 0.5, alternative="greater")

        print(f"  {pat_name}: WR = {wr:.4f}")
        print(f"    Break-even max_ask = ${wr:.4f}")
        print(f"    95% CI: [{btest.proportion_ci(confidence_level=0.95).low:.4f}, "
              f"{btest.proportion_ci(confidence_level=0.95).high:.4f}]")
        print(f"    Conservative break-even (lower CI): "
              f"${btest.proportion_ci(confidence_level=0.95).low:.4f}")
        print()

    # -----------------------------------------------------------------------
    # Final summary
    # -----------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("EXECUTIVE SUMMARY")
    print("=" * 80)

    print("""
The analysis simulates fill rates at different max_ask thresholds for DOWN
token GTC limit orders after UP-streak patterns.

KEY FINDINGS:

1. BREAK-EVEN PRICES (max_ask where EV per trade = 0):
   These equal the pattern win rate. Any max_ask below the WR is +EV per trade.""")

    for pat_name in ["UUU", "UUUU", "UUUUU"]:
        sigs = signals[pat_name]
        wr = sum(h for _, _, h in sigs) / len(sigs)
        print(f"   - {pat_name}: Break-even at ${wr:.4f} (WR = {wr:.1%})")

    print("""
2. FILL RATE vs EV TRADEOFF:
   Lower max_ask = higher EV per trade but fewer fills
   Higher max_ask = lower EV per trade but more fills
   The optimal max_ask maximizes (fill_rate * EV_per_trade)

3. OPTIMAL max_ask RECOMMENDATIONS:""")

    for pat_name in ["UUU", "UUUU", "UUUUU"]:
        r = recs[pat_name]
        print(f"   - {pat_name}: ${r['max_ask']:.2f} (fill={r['fill_rate']:.0%}, "
              f"monthly PnL=${r['monthly_ev']*5:+.2f} at $5/trade)")

    print("""
4. SENSITIVITY:
   The optimal max_ask is sensitive to the assumed DOWN ask distribution.
   Check the sensitivity tables above for each pattern.

5. CURRENT CONFIG ASSESSMENT:""")
    for pat_name in ["UUU", "UUUU", "UUUUU"]:
        r = recs[pat_name]
        current = 0.51
        if pat_name in ["UUUU", "UUUUU"]:
            current_note = " ($0.54 for ETH)"
        else:
            current_note = ""
        delta = r["max_ask"] - current
        if abs(delta) < 0.005:
            verdict = "CURRENT CONFIG IS NEAR-OPTIMAL"
        elif delta > 0:
            verdict = f"Consider RAISING to ${r['max_ask']:.2f} (+${delta:.2f})"
        else:
            verdict = f"Consider LOWERING to ${r['max_ask']:.2f} (${delta:.2f})"
        print(f"   - {pat_name}: {verdict}{current_note}")

    print("\n" + "=" * 80)
    print("DONE")
    print("=" * 80)


if __name__ == "__main__":
    main()
