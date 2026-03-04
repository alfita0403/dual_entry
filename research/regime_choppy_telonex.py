"""
regime_choppy_telonex.py - Test the "choppy regime" hypothesis at scale.

Session 3 found: when alternation rate > 60%, UU->DOWN had 84% WR (p=0.009, n=31).
Now we test with the full Telonex dataset: 60,605 markets, 74 days.

Hypothesis: UU->DOWN is a mean-reversion pattern that works best in choppy
(high-alternation) regimes and fails in trending (low-alternation) regimes.
"""

import re
import sys
import warnings
from collections import defaultdict

import numpy as np
import pandas as pd
from scipy.stats import binomtest

warnings.filterwarnings("ignore", category=FutureWarning)

# -------------------------------------------------------------
# 1. Load and prepare data
# -------------------------------------------------------------

DATA_PATH = "data/telonex_updown_5m.parquet"

print("=" * 80)
print("REGIME CHOPPY TELONEX ANALYSIS")
print("Testing alternation-rate regime filter on full Telonex dataset")
print("=" * 80)

df = pd.read_parquet(DATA_PATH)
print(f"\nLoaded {len(df):,} markets from {DATA_PATH}")

# Filter to settled markets only (result_id = '0' or '1')
df = df[df["result_id"].isin(["0", "1"])].copy()
print(f"Settled markets: {len(df):,}")

# Extract coin and timestamp from slug
def parse_slug(slug):
    m = re.match(r"(\w+)-updown-5m-(\d+)", slug)
    if m:
        return m.group(1).upper(), int(m.group(2))
    return None, None

df["coin"], df["timestamp"] = zip(*df["slug"].apply(parse_slug))
df = df.dropna(subset=["coin"])
df["timestamp"] = df["timestamp"].astype(int)

# Map result_id to outcome: 0 = UP, 1 = DOWN
df["outcome"] = df["result_id"].map({"0": "U", "1": "D"})

# Sort by coin, then timestamp
df = df.sort_values(["coin", "timestamp"]).reset_index(drop=True)

print(f"\nCoin distribution:")
for coin, cnt in df["coin"].value_counts().sort_index().items():
    print(f"  {coin}: {cnt:,} markets")

# Verify 5-minute spacing
print(f"\nDate range:")
for coin in sorted(df["coin"].unique()):
    sub = df[df["coin"] == coin]
    ts_min, ts_max = sub["timestamp"].min(), sub["timestamp"].max()
    from datetime import datetime, timezone
    dt_min = datetime.fromtimestamp(ts_min, tz=timezone.utc)
    dt_max = datetime.fromtimestamp(ts_max, tz=timezone.utc)
    gaps = sub["timestamp"].diff().dropna()
    print(f"  {coin}: {dt_min:%Y-%m-%d} to {dt_max:%Y-%m-%d} | "
          f"median gap: {gaps.median():.0f}s ({gaps.median()/60:.1f}m) | "
          f"n={len(sub):,}")

# -------------------------------------------------------------
# 2. Build outcome sequences and compute alternation rate
# -------------------------------------------------------------

def compute_alternation_rates(outcomes, windows):
    """Compute rolling alternation rate (look-back only, no look-ahead).

    Alternation = proportion of transitions where outcome[i] != outcome[i-1]
    over the last `window` transitions.

    Returns dict of window -> array of alternation rates (NaN where insufficient data).
    """
    n = len(outcomes)
    # Build alternation indicator: 1 if outcome differs from previous
    alternations = np.zeros(n, dtype=float)
    for i in range(1, n):
        alternations[i] = 1.0 if outcomes[i] != outcomes[i - 1] else 0.0

    result = {}
    for w in windows:
        rates = np.full(n, np.nan)
        for i in range(w, n):
            # Look at the last w transitions (from i-w+1 to i)
            # Transition at index j means outcome[j] vs outcome[j-1]
            # So transitions at indices i-w+1, i-w+2, ..., i
            rates[i] = alternations[i - w + 1:i + 1].mean()
        result[w] = rates
    return result


# Build per-coin sequences
WINDOWS = [10, 20]
PATTERNS = {
    "UU->DOWN":   (["U", "U"], "D"),
    "UUU->DOWN":  (["U", "U", "U"], "D"),
    "UUUU->DOWN": (["U", "U", "U", "U"], "D"),
    "UUUUU->DOWN": (["U", "U", "U", "U", "U"], "D"),
    # Also test DD patterns for symmetry
    "DD->UP":     (["D", "D"], "U"),
    "DDD->UP":    (["D", "D", "D"], "U"),
}

# Fixed regime bins
FIXED_BINS = {
    "Trending (<40%)": (0.0, 0.4),
    "Normal (40-60%)": (0.4, 0.6),
    "Choppy (>60%)":   (0.6, 1.01),  # 1.01 to include 1.0
}

print("\n" + "=" * 80)
print("COMPUTING ALTERNATION RATES AND PATTERN MATCHES")
print("=" * 80)

# Collect all pattern trades with their alternation rate context
# Structure: {pattern_name: {window: [(coin, idx, outcome_matches_target, alt_rate)]}}
trades = defaultdict(lambda: defaultdict(list))
all_coin_stats = {}

for coin in sorted(df["coin"].unique()):
    coin_df = df[df["coin"] == coin].reset_index(drop=True)
    outcomes = coin_df["outcome"].values
    n = len(outcomes)

    # Compute alternation rates for all windows
    alt_rates = compute_alternation_rates(outcomes, WINDOWS)

    # Overall alternation rate for this coin
    total_alternations = sum(1 for i in range(1, n) if outcomes[i] != outcomes[i - 1])
    overall_alt = total_alternations / (n - 1) if n > 1 else 0
    all_coin_stats[coin] = {"n": n, "overall_alt": overall_alt}

    print(f"\n{coin}: {n:,} cycles, overall alternation rate = {overall_alt:.3f}")

    # For each pattern, find all occurrences and record alternation rate
    for pat_name, (prefix, target) in PATTERNS.items():
        prefix_len = len(prefix)
        for i in range(prefix_len, n):
            # Check if the prefix matches (the last prefix_len outcomes before i)
            match = True
            for j, p in enumerate(prefix):
                if outcomes[i - prefix_len + j] != p:
                    match = False
                    break
            if not match:
                continue

            # This is a valid trade signal at position i
            # The outcome at position i is the "bet" result
            hit = 1 if outcomes[i] == target else 0

            for w in WINDOWS:
                # Alternation rate BEFORE the current cycle (at position i-1)
                # We need the alternation rate computed up to i-1
                # alt_rates[w][i-1] is the rate over the window ending at i-1
                if i - 1 >= w:
                    rate = alt_rates[w][i - 1]
                    if not np.isnan(rate):
                        trades[pat_name][w].append((coin, i, hit, rate))


# -------------------------------------------------------------
# 3. Analyze by fixed regime bins
# -------------------------------------------------------------

def analyze_regime_split(trade_list, bins, overall_wr, label):
    """Analyze trades split by regime bins."""
    rows = []
    for bin_name, (lo, hi) in bins.items():
        subset = [(c, i, h, r) for c, i, h, r in trade_list if lo <= r < hi]
        if len(subset) == 0:
            rows.append({
                "Regime": bin_name,
                "N": 0, "WR": np.nan, "Delta": np.nan,
                "z-score": np.nan, "p-value": np.nan
            })
            continue

        n = len(subset)
        hits = sum(h for _, _, h, _ in subset)
        wr = hits / n
        delta = wr - overall_wr

        # Binomial test: is this WR significantly different from overall?
        btest = binomtest(hits, n, overall_wr, alternative="two-sided")
        p_val = btest.pvalue

        # z-score (normal approximation)
        se = np.sqrt(overall_wr * (1 - overall_wr) / n) if n > 0 else 0
        z = delta / se if se > 0 else 0

        rows.append({
            "Regime": bin_name,
            "N": n,
            "Wins": hits,
            "WR": wr,
            "Delta": delta,
            "z-score": z,
            "p-value": p_val,
        })
    return pd.DataFrame(rows)


print("\n" + "=" * 80)
print("RESULTS: FIXED REGIME BINS")
print("=" * 80)

for pat_name in PATTERNS:
    for w in WINDOWS:
        trade_list = trades[pat_name][w]
        if not trade_list:
            continue

        n_total = len(trade_list)
        total_hits = sum(h for _, _, h, _ in trade_list)
        overall_wr = total_hits / n_total

        print(f"\n{'-' * 70}")
        print(f"Pattern: {pat_name} | Window: {w} | Total trades: {n_total:,} | Overall WR: {overall_wr:.3f}")
        print(f"{'-' * 70}")

        result_df = analyze_regime_split(trade_list, FIXED_BINS, overall_wr, pat_name)

        # Format display
        for _, row in result_df.iterrows():
            if row["N"] == 0:
                print(f"  {row['Regime']:25s}  N={row['N']:>5}  --")
            else:
                sig = ""
                if row["p-value"] < 0.001:
                    sig = " ***"
                elif row["p-value"] < 0.01:
                    sig = " **"
                elif row["p-value"] < 0.05:
                    sig = " *"
                print(f"  {row['Regime']:25s}  N={row['N']:>5,}  "
                      f"WR={row['WR']:.3f}  "
                      f"Delta={row['Delta']:+.3f}  "
                      f"z={row['z-score']:+.2f}  "
                      f"p={row['p-value']:.4f}{sig}")


# -------------------------------------------------------------
# 4. Analyze by quantile terciles
# -------------------------------------------------------------

print("\n\n" + "=" * 80)
print("RESULTS: QUANTILE TERCILE BINS")
print("=" * 80)

for pat_name in PATTERNS:
    for w in WINDOWS:
        trade_list = trades[pat_name][w]
        if not trade_list:
            continue

        n_total = len(trade_list)
        total_hits = sum(h for _, _, h, _ in trade_list)
        overall_wr = total_hits / n_total
        rates = np.array([r for _, _, _, r in trade_list])

        # Compute tercile boundaries
        q33 = np.percentile(rates, 33.33)
        q67 = np.percentile(rates, 66.67)

        tercile_bins = {
            f"Low (< {q33:.2f})":    (0.0, q33),
            f"Mid ({q33:.2f}-{q67:.2f})":  (q33, q67),
            f"High (>= {q67:.2f})":  (q67, 1.01),
        }

        print(f"\n{'-' * 70}")
        print(f"Pattern: {pat_name} | Window: {w} | Total: {n_total:,} | Overall WR: {overall_wr:.3f}")
        print(f"Tercile boundaries: Q33={q33:.3f}, Q67={q67:.3f}")
        print(f"{'-' * 70}")

        result_df = analyze_regime_split(trade_list, tercile_bins, overall_wr, pat_name)

        for _, row in result_df.iterrows():
            if row["N"] == 0:
                print(f"  {row['Regime']:30s}  N={row['N']:>5}  --")
            else:
                sig = ""
                if row["p-value"] < 0.001:
                    sig = " ***"
                elif row["p-value"] < 0.01:
                    sig = " **"
                elif row["p-value"] < 0.05:
                    sig = " *"
                print(f"  {row['Regime']:30s}  N={row['N']:>5,}  "
                      f"WR={row['WR']:.3f}  "
                      f"Delta={row['Delta']:+.3f}  "
                      f"z={row['z-score']:+.2f}  "
                      f"p={row['p-value']:.4f}{sig}")


# -------------------------------------------------------------
# 5. Per-coin breakdown for key pattern (UU->DOWN)
# -------------------------------------------------------------

print("\n\n" + "=" * 80)
print("PER-COIN BREAKDOWN: UU->DOWN by Fixed Regime Bins")
print("=" * 80)

for w in WINDOWS:
    print(f"\n{'=' * 70}")
    print(f"Window = {w}")
    print(f"{'=' * 70}")

    trade_list = trades["UU->DOWN"][w]
    if not trade_list:
        print("  No trades")
        continue

    # Group by coin
    coin_trades = defaultdict(list)
    for coin, idx, hit, rate in trade_list:
        coin_trades[coin].append((coin, idx, hit, rate))

    for coin in sorted(coin_trades.keys()):
        ct = coin_trades[coin]
        n_total = len(ct)
        total_hits = sum(h for _, _, h, _ in ct)
        overall_wr = total_hits / n_total

        print(f"\n  {coin}: {n_total:,} trades, Overall WR={overall_wr:.3f}")

        for bin_name, (lo, hi) in FIXED_BINS.items():
            subset = [(c, i, h, r) for c, i, h, r in ct if lo <= r < hi]
            if len(subset) == 0:
                print(f"    {bin_name:25s}  N={0:>5}  --")
                continue
            n = len(subset)
            hits = sum(h for _, _, h, _ in subset)
            wr = hits / n
            delta = wr - overall_wr
            btest = binomtest(hits, n, overall_wr, alternative="two-sided")
            se = np.sqrt(overall_wr * (1 - overall_wr) / n)
            z = delta / se if se > 0 else 0
            sig = ""
            if btest.pvalue < 0.001:
                sig = " ***"
            elif btest.pvalue < 0.01:
                sig = " **"
            elif btest.pvalue < 0.05:
                sig = " *"
            print(f"    {bin_name:25s}  N={n:>5,}  "
                  f"WR={wr:.3f}  Delta={delta:+.3f}  "
                  f"z={z:+.2f}  p={btest.pvalue:.4f}{sig}")


# -------------------------------------------------------------
# 6. Distribution of alternation rates
# -------------------------------------------------------------

print("\n\n" + "=" * 80)
print("ALTERNATION RATE DISTRIBUTION (at UU->DOWN trade points)")
print("=" * 80)

for w in WINDOWS:
    trade_list = trades["UU->DOWN"][w]
    if not trade_list:
        continue
    rates = np.array([r for _, _, _, r in trade_list])
    print(f"\nWindow = {w}:")
    print(f"  Count:  {len(rates):,}")
    print(f"  Mean:   {rates.mean():.3f}")
    print(f"  Median: {np.median(rates):.3f}")
    print(f"  Std:    {rates.std():.3f}")
    print(f"  Min:    {rates.min():.3f}")
    print(f"  Max:    {rates.max():.3f}")

    # Histogram by 10% bins
    print(f"  Distribution:")
    for lo_pct in range(0, 100, 10):
        lo_f = lo_pct / 100
        hi_f = (lo_pct + 10) / 100
        cnt = ((rates >= lo_f) & (rates < hi_f)).sum()
        # Also compute WR in this micro-bin
        subset = [(c, i, h, r) for c, i, h, r in trade_list
                  if lo_f <= r < hi_f]
        if len(subset) > 0:
            wr = sum(h for _, _, h, _ in subset) / len(subset)
            bar = "#" * (cnt * 50 // len(rates))
            print(f"    [{lo_pct:3d}%-{lo_pct+10:3d}%) N={cnt:>5,}  WR={wr:.3f}  {bar}")
        else:
            print(f"    [{lo_pct:3d}%-{lo_pct+10:3d}%) N={cnt:>5,}")


# -------------------------------------------------------------
# 7. Summary verdict
# -------------------------------------------------------------

print("\n\n" + "=" * 80)
print("SUMMARY VERDICT")
print("=" * 80)

# Focus on the key question: does UU->DOWN WR improve in choppy regime?
for w in WINDOWS:
    trade_list = trades["UU->DOWN"][w]
    if not trade_list:
        continue

    n_total = len(trade_list)
    total_hits = sum(h for _, _, h, _ in trade_list)
    overall_wr = total_hits / n_total

    choppy = [(c, i, h, r) for c, i, h, r in trade_list if r > 0.6]
    trending = [(c, i, h, r) for c, i, h, r in trade_list if r < 0.4]

    choppy_n = len(choppy)
    choppy_hits = sum(h for _, _, h, _ in choppy) if choppy else 0
    choppy_wr = choppy_hits / choppy_n if choppy_n > 0 else 0

    trending_n = len(trending)
    trending_hits = sum(h for _, _, h, _ in trending) if trending else 0
    trending_wr = trending_hits / trending_n if trending_n > 0 else 0

    # Binomial tests
    if choppy_n > 0:
        choppy_p = binomtest(choppy_hits, choppy_n, overall_wr, alternative="greater").pvalue
    else:
        choppy_p = 1.0

    if trending_n > 0:
        trending_p = binomtest(trending_hits, trending_n, overall_wr, alternative="less").pvalue
    else:
        trending_p = 1.0

    print(f"\nUU->DOWN | Window={w}")
    print(f"  Overall:   N={n_total:>5,}  WR={overall_wr:.4f}")
    print(f"  Choppy:    N={choppy_n:>5,}  WR={choppy_wr:.4f}  "
          f"Delta={choppy_wr - overall_wr:+.4f}  "
          f"p(greater)={choppy_p:.4f}")
    print(f"  Trending:  N={trending_n:>5,}  WR={trending_wr:.4f}  "
          f"Delta={trending_wr - overall_wr:+.4f}  "
          f"p(less)={trending_p:.4f}")

    # Session 3 comparison
    if w == 10:
        print(f"\n  Session 3 comparison (n=31, WR=84%, p=0.009):")
        print(f"  -> Telonex:  N={choppy_n:,}, WR={choppy_wr:.1%}, "
              f"p={choppy_p:.4f}")
        if choppy_p > 0.05:
            print(f"  -> VERDICT: Choppy regime edge does NOT replicate (p={choppy_p:.3f} > 0.05)")
        else:
            print(f"  -> VERDICT: Choppy regime edge REPLICATES (p={choppy_p:.4f} < 0.05)")

# Final overall conclusion
print(f"\n{'-' * 70}")
print("OVERALL CONCLUSION:")

uu_w10 = trades["UU->DOWN"][10]
if uu_w10:
    n_total = len(uu_w10)
    overall_wr = sum(h for _, _, h, _ in uu_w10) / n_total
    choppy = [(c, i, h, r) for c, i, h, r in uu_w10 if r > 0.6]
    if choppy:
        choppy_wr = sum(h for _, _, h, _ in choppy) / len(choppy)
        if abs(choppy_wr - overall_wr) < 0.02:
            print("The choppy regime filter shows NO meaningful WR improvement.")
            print("The Session 3 finding (84% WR, n=31) was likely a small-sample artifact.")
        elif choppy_wr > overall_wr + 0.02:
            print(f"The choppy regime filter shows a +{choppy_wr - overall_wr:.1%} WR improvement.")
            print("Further investigation warranted.")
        else:
            print(f"The choppy regime filter shows a {choppy_wr - overall_wr:+.1%} WR change.")
            print("No actionable edge found.")

print(f"{'-' * 70}")
print("Done.")
