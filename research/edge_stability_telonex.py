"""
edge_stability_telonex.py - Is the mean-reversion edge stable, growing, or decaying?

Critical question: the live strategy has ~54% WR on UUU->DOWN patterns validated
on 74 days of Telonex data. But is this edge STABLE across those 74 days, or was
it stronger early and has been decaying?

Tests:
  1. Rolling 7-day window WR analysis with linear trend
  2. Monthly split (Dec / Jan / Feb) with Fisher exact tests
  3. First-half vs second-half comparison
  4. Cumulative PnL trajectory (all 6 YAML rules, $5 size)
  5. Market maker adaptation test (DOWN ask drift after UP streaks)
  6. Daily PnL autocorrelation

Data: telonex_updown_5m.parquet (60,605 markets, 74 days Dec 18 - Mar 1)
"""

import re
import sys
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
from scipy.stats import binomtest, fisher_exact, linregress

warnings.filterwarnings("ignore", category=FutureWarning)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DATA_FILE = Path(__file__).parent.parent / "data" / "telonex_updown_5m.parquet"
COINS = ["BTC", "ETH", "SOL", "XRP"]
FEE_RATE = 0.015   # 1.5% taker fee
SIZE = 5.0          # $5 per trade

# Live YAML rules (from strategies/mean_reversion.yaml)
RULES = [
    {"pattern": "UUUUU", "side": "DOWN", "coins": ["ETH"],              "max_ask": 0.54},
    {"pattern": "UUUUU", "side": "DOWN", "coins": ["BTC","SOL","XRP"],  "max_ask": 0.51},
    {"pattern": "UUUU",  "side": "DOWN", "coins": ["ETH"],              "max_ask": 0.54},
    {"pattern": "UUUU",  "side": "DOWN", "coins": ["BTC","SOL","XRP"],  "max_ask": 0.51},
    {"pattern": "UDUU",  "side": "DOWN", "coins": ["BTC"],              "max_ask": 0.51},
    {"pattern": "UUU",   "side": "DOWN", "coins": ["BTC","ETH","SOL","XRP"], "max_ask": 0.51},
]

# ---------------------------------------------------------------------------
# 1. Load and prepare data
# ---------------------------------------------------------------------------
print("=" * 80)
print("EDGE STABILITY ANALYSIS")
print("Is the mean-reversion edge stable, growing, or decaying?")
print("=" * 80)

df = pd.read_parquet(DATA_FILE)
df = df[df["result_id"].isin(["0", "1"])].copy()

# Parse coin and timestamp from slug
df["coin"] = df["slug"].str.extract(r"^(\w+)-updown-5m-")[0].str.upper()
df["unix_ts"] = df["slug"].str.extract(r"-(\d+)$")[0].astype(float)
df["dt"] = pd.to_datetime(df["unix_ts"], unit="s", utc=True)
df["outcome"] = df["result_id"].map({"0": "U", "1": "D"})
df["date"] = df["dt"].dt.date
df = df.sort_values(["coin", "unix_ts"]).reset_index(drop=True)

print(f"\nLoaded {len(df):,} settled markets")
print(f"Date range: {df['date'].min()} to {df['date'].max()}")
n_days = (df["date"].max() - df["date"].min()).days + 1
print(f"Span: {n_days} days")
print(f"Coins: {sorted(df['coin'].unique())}")

# ---------------------------------------------------------------------------
# Build per-coin outcome sequences with date info
# ---------------------------------------------------------------------------
coin_seqs = {}
for coin in COINS:
    cdf = df[df["coin"] == coin].sort_values("unix_ts").reset_index(drop=True)
    coin_seqs[coin] = {
        "outcomes": cdf["outcome"].values,
        "dates": cdf["date"].values,
        "unix_ts": cdf["unix_ts"].values,
        "n": len(cdf),
    }
    print(f"  {coin}: {len(cdf):,} cycles")


# ---------------------------------------------------------------------------
# Helper: identify pattern trades across all coins
# ---------------------------------------------------------------------------
def find_pattern_trades(coin_seqs, pattern_str):
    """Find all occurrences where pattern_str precedes the next outcome.

    pattern_str: e.g. "UUU" means last 3 outcomes were U,U,U.
    Returns list of (coin, idx, outcome, date) where outcome is what happened
    at position idx (the trade).
    """
    pat = list(pattern_str)
    plen = len(pat)
    trades = []
    for coin, data in coin_seqs.items():
        outcomes = data["outcomes"]
        dates = data["dates"]
        n = data["n"]
        for i in range(plen, n):
            # Check if the last plen outcomes match
            match = True
            for j in range(plen):
                if outcomes[i - plen + j] != pat[j]:
                    match = False
                    break
            if match:
                trades.append({
                    "coin": coin,
                    "idx": i,
                    "outcome": outcomes[i],
                    "date": dates[i],
                })
    return trades


def find_yaml_rule_trades(coin_seqs, rules):
    """Simulate the live YAML rules with priority ordering.

    For each coin's sequence, at each position check rules top-to-bottom.
    First matching rule fires (higher priority = longer pattern checked first).
    """
    all_trades = []
    for coin, data in coin_seqs.items():
        outcomes = data["outcomes"]
        dates = data["dates"]
        n = data["n"]

        for i in range(1, n):
            for rule in rules:
                if coin not in rule["coins"]:
                    continue
                pat = list(rule["pattern"])
                plen = len(pat)
                if i < plen:
                    continue
                # Check pattern match
                match = True
                for j in range(plen):
                    if outcomes[i - plen + j] != pat[j]:
                        match = False
                        break
                if not match:
                    continue
                # Rule matches -- record trade
                side = rule["side"]  # "DOWN"
                max_ask = rule["max_ask"]
                actual = outcomes[i]
                hit = 1 if (side == "DOWN" and actual == "D") or \
                           (side == "UP" and actual == "U") else 0
                all_trades.append({
                    "coin": coin,
                    "idx": i,
                    "outcome": actual,
                    "date": dates[i],
                    "pattern": rule["pattern"],
                    "side": side,
                    "max_ask": max_ask,
                    "hit": hit,
                })
                break  # first matching rule wins
    return all_trades


# ---------------------------------------------------------------------------
# 2. Rolling window analysis
# ---------------------------------------------------------------------------
print("\n\n" + "=" * 80)
print("SECTION 2: ROLLING 7-DAY WINDOW ANALYSIS")
print("=" * 80)

for pat_name, pat_str, target in [("UU->DOWN", "UU", "D"), ("UUU->DOWN", "UUU", "D")]:
    trades = find_pattern_trades(coin_seqs, pat_str)
    trades_df = pd.DataFrame(trades)
    trades_df["hit"] = (trades_df["outcome"] == target).astype(int)

    # Get unique dates sorted
    all_dates = sorted(trades_df["date"].unique())
    n_dates = len(all_dates)
    date_to_idx = {d: i for i, d in enumerate(all_dates)}

    # Rolling 7-day window, step 1 day
    window_size = 7
    rows = []
    for start_i in range(0, n_dates - window_size + 1):
        window_dates = set(all_dates[start_i:start_i + window_size])
        window_trades = trades_df[trades_df["date"].isin(window_dates)]
        n = len(window_trades)
        if n < 5:
            continue
        wins = window_trades["hit"].sum()
        wr = wins / n
        rows.append({
            "start_date": all_dates[start_i],
            "end_date": all_dates[start_i + window_size - 1],
            "n": n,
            "wins": wins,
            "wr": wr,
            "window_idx": start_i,
        })

    rolling_df = pd.DataFrame(rows)

    print(f"\n{pat_name}: Rolling 7-day windows (step=1 day)")
    print(f"{'Start':>12s}  {'End':>12s}  {'N':>5s}  {'Wins':>5s}  {'WR':>6s}  {'Bar'}")
    print("-" * 70)
    for _, r in rolling_df.iterrows():
        bar_len = int(r["wr"] * 40)
        bar = "#" * bar_len
        wr_str = f"{r['wr']:.3f}"
        flag = " <-- below 50%" if r["wr"] < 0.50 else ""
        print(f"{str(r['start_date']):>12s}  {str(r['end_date']):>12s}  "
              f"{r['n']:>5.0f}  {r['wins']:>5.0f}  {wr_str:>6s}  {bar}{flag}")

    # Linear regression: WR over time
    if len(rolling_df) >= 5:
        x = rolling_df["window_idx"].values
        y = rolling_df["wr"].values
        slope, intercept, r_val, p_val, se = linregress(x, y)
        print(f"\nLinear trend: slope = {slope:+.5f}/day  "
              f"(p={p_val:.4f}, R^2={r_val**2:.4f})")
        if p_val < 0.05:
            direction = "INCREASING" if slope > 0 else "DECREASING"
            print(f"  ** SIGNIFICANT {direction} trend detected **")
        else:
            print(f"  No significant trend (p={p_val:.3f} > 0.05)")

        # Projected WR at day 74 vs day 1
        wr_start = intercept
        wr_end = intercept + slope * (len(rolling_df) - 1)
        print(f"  Projected WR: start={wr_start:.3f}, end={wr_end:.3f}, "
              f"delta={wr_end - wr_start:+.3f}")


# ---------------------------------------------------------------------------
# 3. Monthly split
# ---------------------------------------------------------------------------
print("\n\n" + "=" * 80)
print("SECTION 3: MONTHLY SPLIT")
print("=" * 80)

from datetime import date as dt_date

month_bins = {
    "Dec (18-31)": (dt_date(2025, 12, 18), dt_date(2025, 12, 31)),
    "Jan (1-31)":  (dt_date(2026, 1, 1),   dt_date(2026, 1, 31)),
    "Feb (1-28)":  (dt_date(2026, 2, 1),   dt_date(2026, 2, 28)),
}

for pat_name, pat_str, target in [("UU->DOWN", "UU", "D"), ("UUU->DOWN", "UUU", "D")]:
    trades = find_pattern_trades(coin_seqs, pat_str)
    trades_df = pd.DataFrame(trades)
    trades_df["hit"] = (trades_df["outcome"] == target).astype(int)

    print(f"\n{pat_name}:")
    print(f"  {'Month':>15s}  {'N':>6s}  {'Wins':>6s}  {'WR':>7s}  {'95% CI':>14s}")
    print("  " + "-" * 55)

    month_data = {}
    for mname, (d_start, d_end) in month_bins.items():
        mask = (trades_df["date"] >= d_start) & (trades_df["date"] <= d_end)
        sub = trades_df[mask]
        n = len(sub)
        wins = sub["hit"].sum()
        wr = wins / n if n > 0 else 0
        # Wilson CI
        if n > 0:
            bt = binomtest(wins, n)
            ci = bt.proportion_ci(confidence_level=0.95)
            ci_str = f"[{ci.low:.3f}, {ci.high:.3f}]"
        else:
            ci_str = "  --"
        print(f"  {mname:>15s}  {n:>6,}  {wins:>6,}  {wr:>7.3f}  {ci_str:>14s}")
        month_data[mname] = (wins, n - wins, n)

    # Fisher exact tests between months
    print(f"\n  Fisher exact tests (2x2):")
    month_names = list(month_data.keys())
    for i in range(len(month_names)):
        for j in range(i + 1, len(month_names)):
            m1, m2 = month_names[i], month_names[j]
            w1, l1, n1 = month_data[m1]
            w2, l2, n2 = month_data[m2]
            table = [[w1, l1], [w2, l2]]
            odds_ratio, p_val = fisher_exact(table)
            wr1 = w1 / n1 if n1 > 0 else 0
            wr2 = w2 / n2 if n2 > 0 else 0
            sig = " *" if p_val < 0.05 else ""
            print(f"    {m1} vs {m2}: "
                  f"WR {wr1:.3f} vs {wr2:.3f}, "
                  f"OR={odds_ratio:.3f}, p={p_val:.4f}{sig}")


# ---------------------------------------------------------------------------
# 4. First half vs second half
# ---------------------------------------------------------------------------
print("\n\n" + "=" * 80)
print("SECTION 4: FIRST HALF vs SECOND HALF")
print("=" * 80)

all_dates_sorted = sorted(df["date"].unique())
midpoint = len(all_dates_sorted) // 2
first_half_dates = set(all_dates_sorted[:midpoint])
second_half_dates = set(all_dates_sorted[midpoint:])

print(f"First half:  {min(first_half_dates)} to {max(first_half_dates)} "
      f"({len(first_half_dates)} days)")
print(f"Second half: {min(second_half_dates)} to {max(second_half_dates)} "
      f"({len(second_half_dates)} days)")

for pat_name, pat_str, target in [("UU->DOWN", "UU", "D"), ("UUU->DOWN", "UUU", "D")]:
    trades = find_pattern_trades(coin_seqs, pat_str)
    trades_df = pd.DataFrame(trades)
    trades_df["hit"] = (trades_df["outcome"] == target).astype(int)

    first = trades_df[trades_df["date"].isin(first_half_dates)]
    second = trades_df[trades_df["date"].isin(second_half_dates)]

    n1, w1 = len(first), first["hit"].sum()
    n2, w2 = len(second), second["hit"].sum()
    wr1 = w1 / n1 if n1 > 0 else 0
    wr2 = w2 / n2 if n2 > 0 else 0

    table = [[w1, n1 - w1], [w2, n2 - w2]]
    odds_ratio, p_val = fisher_exact(table)

    bt1 = binomtest(w1, n1)
    bt2 = binomtest(w2, n2)
    ci1 = bt1.proportion_ci(confidence_level=0.95)
    ci2 = bt2.proportion_ci(confidence_level=0.95)

    print(f"\n{pat_name}:")
    print(f"  First half:  N={n1:>5,}  WR={wr1:.4f}  CI=[{ci1.low:.3f}, {ci1.high:.3f}]")
    print(f"  Second half: N={n2:>5,}  WR={wr2:.4f}  CI=[{ci2.low:.3f}, {ci2.high:.3f}]")
    print(f"  Delta: {wr2 - wr1:+.4f}  Fisher p={p_val:.4f}  OR={odds_ratio:.3f}")
    if p_val < 0.05:
        print(f"  ** SIGNIFICANT difference **")
    else:
        print(f"  No significant difference (p={p_val:.3f})")


# ---------------------------------------------------------------------------
# 5. Cumulative PnL trajectory (all 6 YAML rules)
# ---------------------------------------------------------------------------
print("\n\n" + "=" * 80)
print("SECTION 5: CUMULATIVE PnL TRAJECTORY (Live YAML rules)")
print("=" * 80)

yaml_trades = find_yaml_rule_trades(coin_seqs, RULES)
yaml_df = pd.DataFrame(yaml_trades)
yaml_df = yaml_df.sort_values("date").reset_index(drop=True)

print(f"\nTotal YAML rule trades: {len(yaml_df):,}")
print(f"Win rate: {yaml_df['hit'].mean():.4f}")
print(f"Rules breakdown:")
for pat in yaml_df["pattern"].unique():
    sub = yaml_df[yaml_df["pattern"] == pat]
    print(f"  {pat}: N={len(sub):,}, WR={sub['hit'].mean():.3f}")

# Compute PnL per trade
# When we buy DOWN at max_ask, and it resolves DOWN: profit = (1 - ask) * size - fee
# When it resolves UP: loss = -ask * size - fee
# Fee = FEE_RATE * size (on entry, approximately)
def compute_pnl(row):
    ask = row["max_ask"]
    if row["hit"]:
        # Win: payout $1 per share, paid ask per share
        shares = SIZE / ask
        pnl = shares * (1.0 - ask) - SIZE * FEE_RATE
    else:
        # Loss: shares worth $0
        pnl = -SIZE - SIZE * FEE_RATE
    return pnl

yaml_df["pnl"] = yaml_df.apply(compute_pnl, axis=1)
yaml_df["cum_pnl"] = yaml_df["pnl"].cumsum()

print(f"\nPnL summary (size=${SIZE:.0f}, fee={FEE_RATE*100:.1f}%):")
print(f"  Total PnL: ${yaml_df['pnl'].sum():.2f}")
print(f"  Avg PnL/trade: ${yaml_df['pnl'].mean():.3f}")
print(f"  Total trades: {len(yaml_df):,}")
print(f"  Win trades: {yaml_df['hit'].sum():,} ({yaml_df['hit'].mean()*100:.1f}%)")

# Daily PnL
daily_pnl = yaml_df.groupby("date").agg(
    n_trades=("pnl", "count"),
    daily_pnl=("pnl", "sum"),
    daily_wr=("hit", "mean"),
).reset_index()
daily_pnl = daily_pnl.sort_values("date").reset_index(drop=True)
daily_pnl["cum_pnl"] = daily_pnl["daily_pnl"].cumsum()
daily_pnl["day_num"] = range(len(daily_pnl))

print(f"\nCumulative PnL by day:")
print(f"{'Day':>4s}  {'Date':>12s}  {'Trades':>6s}  {'DailyPnL':>9s}  {'CumPnL':>9s}  {'DailyWR':>8s}  {'Trajectory'}")
print("-" * 90)
for _, r in daily_pnl.iterrows():
    # ASCII trajectory bar
    cum = r["cum_pnl"]
    if cum >= 0:
        bar = "+" * min(int(cum / 2), 30)
    else:
        bar = "-" * min(int(abs(cum) / 2), 30)
    print(f"{r['day_num']:>4.0f}  {str(r['date']):>12s}  {r['n_trades']:>6.0f}  "
          f"${r['daily_pnl']:>+8.2f}  ${r['cum_pnl']:>+8.2f}  "
          f"{r['daily_wr']:>8.3f}  {bar}")

# Linear regression on cumulative PnL
if len(daily_pnl) >= 10:
    x = daily_pnl["day_num"].values.astype(float)
    y = daily_pnl["cum_pnl"].values

    slope, intercept, r_val, p_val, se = linregress(x, y)
    print(f"\nCumulative PnL linear fit:")
    print(f"  Slope: ${slope:+.3f}/day  (daily expected profit)")
    print(f"  R^2: {r_val**2:.4f}")
    print(f"  p-value: {p_val:.6f}")

    # Test for curvature (concavity = decaying edge)
    # Fit quadratic and check coefficient
    coeffs = np.polyfit(x, y, 2)
    a, b, c = coeffs
    print(f"\n  Quadratic fit: PnL = {a:.5f}*t^2 + {b:.3f}*t + {c:.2f}")
    if a < -0.001:
        print(f"  ** CONCAVE (a={a:.5f} < 0): edge may be decaying **")
    elif a > 0.001:
        print(f"  ** CONVEX (a={a:.5f} > 0): edge may be strengthening **")
    else:
        print(f"  Approximately linear (a={a:.5f} ~= 0): stable edge")

    # Compare first-half slope vs second-half slope
    mid = len(daily_pnl) // 2
    dp1 = daily_pnl.iloc[:mid]
    dp2 = daily_pnl.iloc[mid:]

    if len(dp1) >= 5 and len(dp2) >= 5:
        s1, _, _, _, _ = linregress(dp1["day_num"].values.astype(float),
                                     dp1["daily_pnl"].cumsum().values)
        s2, _, _, _, _ = linregress(dp2["day_num"].values.astype(float) - dp2["day_num"].values[0],
                                     dp2["daily_pnl"].cumsum().values)
        print(f"\n  Half-period slopes:")
        print(f"    First half:  ${s1:+.3f}/day")
        print(f"    Second half: ${s2:+.3f}/day")
        if s2 < s1 * 0.5:
            print(f"    ** Second half slope < 50% of first half: POSSIBLE DECAY **")
        elif s2 > s1 * 1.5:
            print(f"    ** Second half slope > 150% of first half: STRENGTHENING **")
        else:
            print(f"    Slopes comparable: STABLE")


# ---------------------------------------------------------------------------
# 5b. Also do PnL for UU->DOWN and UUU->DOWN individually
# ---------------------------------------------------------------------------
print("\n\nPer-pattern PnL trajectory:")
for pat_name, pat_str, target in [("UU->DOWN", "UU", "D"), ("UUU->DOWN", "UUU", "D")]:
    trades = find_pattern_trades(coin_seqs, pat_str)
    tdf = pd.DataFrame(trades)
    tdf["hit"] = (tdf["outcome"] == target).astype(int)
    # Use max_ask=0.51 as default (most rules use this)
    ask = 0.51
    tdf["pnl"] = tdf["hit"].apply(
        lambda h: (SIZE / ask) * (1 - ask) - SIZE * FEE_RATE if h
        else -SIZE - SIZE * FEE_RATE
    )
    tdf = tdf.sort_values("date")
    daily = tdf.groupby("date")["pnl"].sum().reset_index()
    daily = daily.sort_values("date").reset_index(drop=True)
    daily["cum_pnl"] = daily["pnl"].cumsum()
    total = daily["cum_pnl"].iloc[-1]
    avg_daily = daily["pnl"].mean()
    print(f"\n  {pat_name}: Total PnL=${total:.2f}, Avg daily=${avg_daily:.2f}, "
          f"WR={tdf['hit'].mean():.3f}, N={len(tdf):,}")

    # Quick trend test
    x = np.arange(len(daily), dtype=float)
    y = daily["cum_pnl"].values
    if len(x) >= 10:
        coeffs = np.polyfit(x, y, 2)
        a = coeffs[0]
        slope_lin, _, r_val, _, _ = linregress(x, y)
        print(f"    Linear slope: ${slope_lin:.3f}/day (R^2={r_val**2:.3f}), "
              f"Quadratic a={a:.5f} ({'concave' if a < 0 else 'convex/flat'})")


# ---------------------------------------------------------------------------
# 6. Market maker adaptation test
# ---------------------------------------------------------------------------
print("\n\n" + "=" * 80)
print("SECTION 6: MARKET MAKER ADAPTATION TEST")
print("=" * 80)

# Check if we have price data in any of the parquet files
# The main dataset doesn't have ask/bid prices.
# Check btc/eth_telonex_with_binance for any price info
adaptation_data_available = False

for coin_file in ["btc_telonex_with_binance.parquet", "eth_telonex_with_binance.parquet"]:
    fpath = Path(__file__).parent.parent / "data" / coin_file
    if fpath.exists():
        cdf = pd.read_parquet(fpath)
        if "outcome" in cdf.columns and "coin" in cdf.columns:
            # These files have outcome + vol/ret data but no Polymarket book prices
            # We can still test if the UNDERLYING price dynamics changed
            adaptation_data_available = True
            break

print("\nNote: The Telonex dataset does not contain Polymarket order book")
print("ask/bid prices per market. We cannot directly test whether DOWN ask")
print("prices drifted higher after UP streaks over time.")
print()

# Instead, test a PROXY: does the underlying DOWN base rate change over time?
# If MMs adapt, they might also be reflecting a real shift in market dynamics.
print("PROXY TEST: Is the base DOWN rate drifting over time?")
print("(If the underlying UP/DOWN ratio shifts, both MMs and our edge would be affected)")

for coin in COINS:
    cdf = df[df["coin"] == coin].sort_values("date")
    dates = sorted(cdf["date"].unique())
    mid = len(dates) // 2

    first_dates = set(dates[:mid])
    second_dates = set(dates[mid:])

    first = cdf[cdf["date"].isin(first_dates)]
    second = cdf[cdf["date"].isin(second_dates)]

    dr1 = (first["outcome"] == "D").mean()
    dr2 = (second["outcome"] == "D").mean()
    n1, n2 = len(first), len(second)
    w1 = (first["outcome"] == "D").sum()
    w2 = (second["outcome"] == "D").sum()

    _, p = fisher_exact([[w1, n1 - w1], [w2, n2 - w2]])

    print(f"  {coin}: DOWN rate first_half={dr1:.4f} (n={n1:,}) "
          f"vs second_half={dr2:.4f} (n={n2:,})  "
          f"delta={dr2-dr1:+.4f}  p={p:.4f}")

# Also check: after UUU streaks, does DOWN rate change over time?
print(f"\nDOWN rate after UUU streaks by month:")
uuu_trades = find_pattern_trades(coin_seqs, "UUU")
uuu_df = pd.DataFrame(uuu_trades)
uuu_df["hit"] = (uuu_df["outcome"] == "D").astype(int)

for mname, (d_start, d_end) in month_bins.items():
    mask = (uuu_df["date"] >= d_start) & (uuu_df["date"] <= d_end)
    sub = uuu_df[mask]
    if len(sub) > 0:
        wr = sub["hit"].mean()
        print(f"  {mname}: UUU->DOWN WR = {wr:.4f} (n={len(sub):,})")

# Check the incycle parquet for any insight about ask prices
incycle_path = Path(__file__).parent.parent / "data" / "telonex_sample_quotes_incycle.parquet"
if incycle_path.exists():
    idf = pd.read_parquet(incycle_path)
    if "ask" in idf.columns and "secs" in idf.columns:
        # This file has ONE market's quote history only (18k rows, 1 slug)
        n_slugs = idf["slug"].nunique()
        print(f"\n  Incycle quotes file: {len(idf):,} rows, {n_slugs} market(s)")
        if n_slugs <= 5:
            print(f"  (Only {n_slugs} market(s) -- insufficient for cross-time analysis)")
            print(f"  Cannot test ask price drift across time.")
        else:
            print(f"  Sufficient markets for price drift analysis -- extending...")


# ---------------------------------------------------------------------------
# 7. Autocorrelation of daily PnL
# ---------------------------------------------------------------------------
print("\n\n" + "=" * 80)
print("SECTION 7: AUTOCORRELATION OF DAILY PnL")
print("=" * 80)

daily_pnl_series = daily_pnl["daily_pnl"].values

print(f"\nDaily PnL stats:")
print(f"  Mean:   ${np.mean(daily_pnl_series):.3f}")
print(f"  Std:    ${np.std(daily_pnl_series):.3f}")
print(f"  Median: ${np.median(daily_pnl_series):.3f}")
print(f"  Min:    ${np.min(daily_pnl_series):.3f}")
print(f"  Max:    ${np.max(daily_pnl_series):.3f}")

# Compute autocorrelation at lags 1-5
print(f"\nAutocorrelation:")
n_days_pnl = len(daily_pnl_series)
for lag in range(1, 6):
    if n_days_pnl > lag + 2:
        corr, p_val = stats.pearsonr(daily_pnl_series[:-lag], daily_pnl_series[lag:])
        sig = " *" if p_val < 0.05 else ""
        print(f"  Lag {lag}: r={corr:+.4f}  p={p_val:.4f}{sig}")

# Also check: autocorrelation of daily WIN RATE
daily_wr_series = daily_pnl["daily_wr"].values
print(f"\nDaily WR autocorrelation:")
for lag in range(1, 4):
    if n_days_pnl > lag + 2:
        corr, p_val = stats.pearsonr(daily_wr_series[:-lag], daily_wr_series[lag:])
        sig = " *" if p_val < 0.05 else ""
        print(f"  Lag {lag}: r={corr:+.4f}  p={p_val:.4f}{sig}")

# Runs test: are positive/negative days randomly distributed?
pos_days = (daily_pnl_series > 0).astype(int)
n_pos = pos_days.sum()
n_neg = len(pos_days) - n_pos
runs = 1
for i in range(1, len(pos_days)):
    if pos_days[i] != pos_days[i - 1]:
        runs += 1

# Expected runs under independence
n_total_runs = n_pos + n_neg
if n_total_runs > 0:
    expected_runs = 1 + 2 * n_pos * n_neg / n_total_runs
    var_runs = (2 * n_pos * n_neg * (2 * n_pos * n_neg - n_total_runs)) / \
               (n_total_runs**2 * (n_total_runs - 1)) if n_total_runs > 1 else 1
    z_runs = (runs - expected_runs) / np.sqrt(var_runs) if var_runs > 0 else 0
    p_runs = 2 * (1 - stats.norm.cdf(abs(z_runs)))
    print(f"\nRuns test (independence of + / - days):")
    print(f"  Positive days: {n_pos}/{len(pos_days)} ({n_pos/len(pos_days)*100:.1f}%)")
    print(f"  Runs: {runs} (expected {expected_runs:.1f})")
    print(f"  z={z_runs:+.3f}, p={p_runs:.4f}")
    if p_runs < 0.05:
        if z_runs < 0:
            print(f"  ** Fewer runs than expected: clustering (streaks) **")
        else:
            print(f"  ** More runs than expected: mean-reverting daily PnL **")
    else:
        print(f"  No significant departure from randomness")


# ---------------------------------------------------------------------------
# 8. Structural break test (Chow-like)
# ---------------------------------------------------------------------------
print("\n\n" + "=" * 80)
print("SECTION 8: STRUCTURAL BREAK DETECTION")
print("=" * 80)

# Test every possible breakpoint in the daily WR series
yaml_daily_wr = yaml_df.groupby("date").agg(
    n=("hit", "count"),
    wins=("hit", "sum"),
).reset_index()
yaml_daily_wr = yaml_daily_wr.sort_values("date").reset_index(drop=True)
yaml_daily_wr["wr"] = yaml_daily_wr["wins"] / yaml_daily_wr["n"]

n_rows = len(yaml_daily_wr)
min_segment = max(5, n_rows // 10)  # at least 10% of days per segment

best_break = None
best_stat = 0
best_p = 1.0
results = []

for bp in range(min_segment, n_rows - min_segment):
    seg1 = yaml_daily_wr.iloc[:bp]
    seg2 = yaml_daily_wr.iloc[bp:]

    w1 = seg1["wins"].sum()
    n1 = seg1["n"].sum()
    w2 = seg2["wins"].sum()
    n2 = seg2["n"].sum()

    wr1 = w1 / n1 if n1 > 0 else 0
    wr2 = w2 / n2 if n2 > 0 else 0

    table = [[w1, n1 - w1], [w2, n2 - w2]]
    _, p = fisher_exact(table)

    results.append({
        "bp": bp,
        "date": yaml_daily_wr.iloc[bp]["date"],
        "wr1": wr1, "n1": n1,
        "wr2": wr2, "n2": n2,
        "delta": wr2 - wr1,
        "p": p,
    })

    if p < best_p:
        best_p = p
        best_break = bp
        best_stat = abs(wr2 - wr1)

break_df = pd.DataFrame(results)

if best_break is not None:
    br = break_df.iloc[break_df["p"].argmin()]
    print(f"\nMost significant breakpoint:")
    print(f"  Date: {br['date']}")
    print(f"  Before: WR={br['wr1']:.4f} (n={br['n1']:.0f})")
    print(f"  After:  WR={br['wr2']:.4f} (n={br['n2']:.0f})")
    print(f"  Delta:  {br['delta']:+.4f}")
    print(f"  Fisher p={br['p']:.4f}")

    # Bonferroni correction for multiple comparisons
    n_tests = len(results)
    bonf_p = min(br["p"] * n_tests, 1.0)
    print(f"  Bonferroni-corrected p={bonf_p:.4f} ({n_tests} breakpoints tested)")
    if bonf_p < 0.05:
        print(f"  ** SIGNIFICANT structural break detected **")
    else:
        print(f"  No significant structural break after correction")


# ---------------------------------------------------------------------------
# 9. Per-coin stability
# ---------------------------------------------------------------------------
print("\n\n" + "=" * 80)
print("SECTION 9: PER-COIN EDGE STABILITY")
print("=" * 80)

for coin in COINS:
    coin_yaml = yaml_df[yaml_df["coin"] == coin].sort_values("date")
    if len(coin_yaml) < 10:
        print(f"\n{coin}: too few trades ({len(coin_yaml)})")
        continue

    n = len(coin_yaml)
    mid = n // 2
    first = coin_yaml.iloc[:mid]
    second = coin_yaml.iloc[mid:]

    wr1 = first["hit"].mean()
    wr2 = second["hit"].mean()
    w1, n1 = first["hit"].sum(), len(first)
    w2, n2 = second["hit"].sum(), len(second)

    _, p = fisher_exact([[w1, n1 - w1], [w2, n2 - w2]])

    print(f"\n{coin}: Total N={n}, WR={coin_yaml['hit'].mean():.3f}")
    print(f"  First half:  N={n1}, WR={wr1:.3f}")
    print(f"  Second half: N={n2}, WR={wr2:.3f}")
    print(f"  Delta: {wr2-wr1:+.3f}, Fisher p={p:.4f}")


# ---------------------------------------------------------------------------
# FINAL VERDICT
# ---------------------------------------------------------------------------
print("\n\n" + "=" * 80)
print("FINAL VERDICT: EDGE STABILITY ASSESSMENT")
print("=" * 80)

# Gather evidence
overall_wr = yaml_df["hit"].mean()
total_pnl = yaml_df["pnl"].sum()

# Get the rolling trend for UUU->DOWN
uuu_trades_list = find_pattern_trades(coin_seqs, "UUU")
uuu_tdf = pd.DataFrame(uuu_trades_list)
uuu_tdf["hit"] = (uuu_tdf["outcome"] == "D").astype(int)

# First half vs second half for UUU
mid = len(uuu_tdf) // 2
wr_h1 = uuu_tdf.iloc[:mid]["hit"].mean()
wr_h2 = uuu_tdf.iloc[mid:]["hit"].mean()

print(f"\n1. Overall: WR={overall_wr:.4f}, Total PnL=${total_pnl:.2f}")
print(f"2. UUU->DOWN first-half WR={wr_h1:.4f} vs second-half WR={wr_h2:.4f} "
      f"(delta={wr_h2-wr_h1:+.4f})")

# Quadratic coefficient from PnL curve
x_pnl = np.arange(len(daily_pnl), dtype=float)
y_pnl = daily_pnl["cum_pnl"].values
if len(x_pnl) >= 10:
    coeffs_pnl = np.polyfit(x_pnl, y_pnl, 2)
    a_pnl = coeffs_pnl[0]
    print(f"3. PnL curve quadratic coefficient: a={a_pnl:.5f} "
          f"({'CONCAVE/decaying' if a_pnl < -0.001 else 'CONVEX/growing' if a_pnl > 0.001 else 'LINEAR/stable'})")

# Breakpoint
if best_break is not None:
    br = break_df.iloc[break_df["p"].argmin()]
    bonf_p = min(br["p"] * len(results), 1.0)
    print(f"4. Structural break: best p={br['p']:.4f}, "
          f"Bonferroni p={bonf_p:.4f} "
          f"({'SIGNIFICANT' if bonf_p < 0.05 else 'not significant'})")

# Autocorrelation
if n_days_pnl > 3:
    acf1, acf1_p = stats.pearsonr(daily_pnl_series[:-1], daily_pnl_series[1:])
    print(f"5. Daily PnL autocorrelation(1): r={acf1:+.4f}, p={acf1_p:.4f}")

# Monthly trend
print(f"\n6. Monthly WR trend (YAML rules):")
for mname, (d_start, d_end) in month_bins.items():
    mask = (yaml_df["date"] >= d_start) & (yaml_df["date"] <= d_end)
    sub = yaml_df[mask]
    if len(sub) > 0:
        print(f"   {mname}: WR={sub['hit'].mean():.4f}, N={len(sub):,}, "
              f"PnL=${sub['pnl'].sum():.2f}")

print(f"\n{'='*80}")
print("CONCLUSION:")
print("="*80)

# Automated verdict based on evidence
decay_signals = 0
stable_signals = 0

# WR trend
if wr_h2 < wr_h1 - 0.02:
    decay_signals += 1
    print("  [-] Second-half WR lower than first-half")
elif wr_h2 > wr_h1 + 0.02:
    stable_signals += 1
    print("  [+] Second-half WR higher than first-half")
else:
    stable_signals += 1
    print("  [=] Halves have similar WR (stable)")

# PnL curvature
if len(x_pnl) >= 10:
    if a_pnl < -0.005:
        decay_signals += 1
        print("  [-] PnL curve is concave (decaying)")
    elif a_pnl > 0.005:
        stable_signals += 1
        print("  [+] PnL curve is convex (strengthening)")
    else:
        stable_signals += 1
        print("  [=] PnL curve is approximately linear (stable)")

# Breakpoint
if best_break is not None:
    bonf_p = min(br["p"] * len(results), 1.0)
    if bonf_p < 0.05:
        decay_signals += 1
        print("  [-] Significant structural break detected")
    else:
        stable_signals += 1
        print("  [+] No significant structural break")

# Final call
print(f"\n  Stability score: {stable_signals} stable signals, {decay_signals} decay signals")
if decay_signals > stable_signals:
    print("  VERDICT: EDGE APPEARS TO BE DECAYING -- proceed with caution")
elif stable_signals > decay_signals + 1:
    print("  VERDICT: EDGE APPEARS STABLE -- forward viability looks good")
else:
    print("  VERDICT: MIXED SIGNALS -- monitor closely, no clear decay trend")

print(f"\n{'='*80}")
print("Done.")
