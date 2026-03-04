"""
four_hypotheses_telonex.py - Test 4 hypotheses on the Telonex dataset.

Dataset: 60,605 resolved 5-min Up/Down markets for BTC, ETH, SOL, XRP
         over 74 days (Dec 18 2025 - Mar 1 2026).

Hypotheses:
  H1: Cross-Coin Divergence  -- when 3/4 coins streak UU but 1 doesn't,
      does the divergent coin have higher P(DOWN)?
  H2: Streak Speed           -- is consecutive UUU (no gaps) a stronger
      signal than UUU spread over time with gaps?
  H3: Day of Week            -- do patterns work differently on weekends
      vs weekdays?
  H4: Post-Loss Autocorrelation -- after a pattern trade loses, is the
      next pattern trade more/less likely to win?

Statistical rigour:
  - Fisher exact test for all 2x2 comparisons
  - Bonferroni correction: alpha = 0.05 / 4 = 0.0125
  - Both UU->DOWN and UUU->DOWN patterns tested
  - Per-coin breakdowns where meaningful
"""

import re
import sys
import warnings
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from scipy.stats import fisher_exact, binomtest

warnings.filterwarnings("ignore", category=FutureWarning)

ALPHA_BONF = 0.05 / 4  # 0.0125

# ================================================================
# 1. Load and prepare data
# ================================================================

DATA_PATH = "data/telonex_updown_5m.parquet"

print("=" * 90)
print("FOUR HYPOTHESES TELONEX ANALYSIS")
print("Dataset: 60,605 resolved 5-min Up/Down markets | 4 coins | 74 days")
print(f"Bonferroni-corrected alpha = {ALPHA_BONF}")
print("=" * 90)

df = pd.read_parquet(DATA_PATH)
print(f"\nLoaded {len(df):,} markets from {DATA_PATH}")

# Filter to settled markets only (result_id = '0' or '1')
df = df[df["result_id"].isin(["0", "1"])].copy()
print(f"Settled markets: {len(df):,}")


def parse_slug(slug):
    m = re.match(r"(\w+)-updown-5m-(\d+)", slug)
    if m:
        return m.group(1).upper(), int(m.group(2))
    return None, None


df["coin"], df["start_timestamp"] = zip(*df["slug"].apply(parse_slug))
df = df.dropna(subset=["coin"])
df["start_timestamp"] = df["start_timestamp"].astype(int)

# Map result_id to outcome: 0 = UP, 1 = DOWN
df["outcome"] = df["result_id"].map({"0": "U", "1": "D"})

# Sort by coin, then timestamp
df = df.sort_values(["coin", "start_timestamp"]).reset_index(drop=True)

# Add datetime columns
df["dt"] = pd.to_datetime(df["start_timestamp"], unit="s", utc=True)
df["day_of_week"] = df["dt"].dt.dayofweek  # 0=Mon ... 6=Sun
df["is_weekend"] = df["day_of_week"].isin([5, 6])

COINS = sorted(df["coin"].unique())
print(f"\nCoins: {COINS}")
print(f"Coin distribution:")
for coin, cnt in df["coin"].value_counts().sort_index().items():
    settled = df[(df["coin"] == coin)].shape[0]
    up_pct = (df[(df["coin"] == coin) & (df["outcome"] == "U")].shape[0]) / settled
    print(f"  {coin}: {settled:>6,} markets  (P(UP) = {up_pct:.3f})")

# Verify 5-minute spacing
print(f"\nDate range and gap statistics:")
for coin in COINS:
    sub = df[df["coin"] == coin].sort_values("start_timestamp")
    ts_min, ts_max = sub["start_timestamp"].min(), sub["start_timestamp"].max()
    dt_min = datetime.fromtimestamp(ts_min, tz=timezone.utc)
    dt_max = datetime.fromtimestamp(ts_max, tz=timezone.utc)
    gaps = sub["start_timestamp"].diff().dropna()
    pct_300 = (gaps == 300).mean() * 100
    print(
        f"  {coin}: {dt_min:%Y-%m-%d} to {dt_max:%Y-%m-%d} | "
        f"median gap: {gaps.median():.0f}s | "
        f"consecutive (300s): {pct_300:.1f}% | n={len(sub):,}"
    )

# ================================================================
# 2. Build per-coin outcome sequences with timestamps
# ================================================================

# Store per-coin data as aligned arrays for fast access
coin_data = {}
for coin in COINS:
    sub = df[df["coin"] == coin].sort_values("start_timestamp").reset_index(drop=True)
    coin_data[coin] = {
        "outcomes": sub["outcome"].values,
        "timestamps": sub["start_timestamp"].values,
        "day_of_week": sub["day_of_week"].values,
        "is_weekend": sub["is_weekend"].values,
        "dt": sub["dt"].values,
        "n": len(sub),
    }


def sig_label(p, alpha=ALPHA_BONF):
    """Return significance label."""
    if p < 0.001:
        return "***"
    elif p < alpha:
        return "**"
    elif p < 0.05:
        return "*"
    else:
        return ""


def fisher_2x2(wins_a, n_a, wins_b, n_b):
    """Fisher exact test on 2x2 contingency table.
    Returns odds ratio and p-value (two-sided).
    """
    table = np.array(
        [[wins_a, n_a - wins_a], [wins_b, n_b - wins_b]]
    )
    odds_ratio, p_val = fisher_exact(table, alternative="two-sided")
    return odds_ratio, p_val


def print_comparison(label_a, wins_a, n_a, label_b, wins_b, n_b, indent="  "):
    """Print a neat comparison between two groups with Fisher test."""
    wr_a = wins_a / n_a if n_a > 0 else float("nan")
    wr_b = wins_b / n_b if n_b > 0 else float("nan")
    delta = wr_a - wr_b if n_a > 0 and n_b > 0 else float("nan")

    print(f"{indent}{label_a:35s}  N={n_a:>6,}  Wins={wins_a:>6,}  WR={wr_a:.4f}")
    print(f"{indent}{label_b:35s}  N={n_b:>6,}  Wins={wins_b:>6,}  WR={wr_b:.4f}")

    if n_a > 0 and n_b > 0:
        odds, p = fisher_2x2(wins_a, n_a, wins_b, n_b)
        sig = sig_label(p)
        print(
            f"{indent}  -> Delta(A-B) = {delta:+.4f}  |  "
            f"Fisher p = {p:.6f}  {sig}  |  OR = {odds:.3f}"
        )
        if p < ALPHA_BONF:
            print(f"{indent}  -> SIGNIFICANT at Bonferroni alpha={ALPHA_BONF}")
        else:
            print(f"{indent}  -> Not significant (p > {ALPHA_BONF})")
    else:
        print(f"{indent}  -> Insufficient data for test")


# ================================================================
# HYPOTHESIS 1: Cross-Coin Divergence
# ================================================================

print("\n\n" + "=" * 90)
print("HYPOTHESIS 1: CROSS-COIN DIVERGENCE")
print("When 3/4 coins have UU+ streak but 1 doesn't, does the divergent")
print("coin have higher P(DOWN) on its next cycle?")
print("=" * 90)

# Build a timeline aligned by start_timestamp across coins.
# For each unique start_timestamp, collect outcomes for all 4 coins.

# Create a pivot: timestamp -> coin -> outcome
pivot_df = df.pivot_table(
    index="start_timestamp", columns="coin", values="outcome", aggfunc="first"
)
# Only keep timestamps where all 4 coins have data
pivot_all4 = pivot_df.dropna(subset=COINS).copy()
pivot_all4 = pivot_all4.sort_index()

print(f"\nTimestamps with all 4 coins: {len(pivot_all4):,}")
print(f"Total timestamps in dataset: {len(pivot_df):,}")


def build_cross_coin_patterns(pivot, coins, pattern_len):
    """For each timestamp, check if exactly 3/4 coins have a streak of
    pattern_len consecutive U outcomes, and the 4th doesn't.

    Returns list of (divergent_coin, timestamp_of_next, next_outcome).
    """
    ts_arr = pivot.index.values
    n = len(ts_arr)
    outcomes = {c: pivot[c].values for c in coins}

    results_divergent = []  # trades on the divergent coin's next cycle
    results_all_streak = []  # trades when all 4 coins have the streak

    for i in range(pattern_len, n):
        # For each coin, check if it has pattern_len consecutive U ending at i-1
        coin_has_streak = {}
        for c in coins:
            has_streak = True
            for j in range(pattern_len):
                if outcomes[c][i - pattern_len + j] != "U":
                    has_streak = False
                    break
            coin_has_streak[c] = has_streak

        streak_count = sum(coin_has_streak.values())

        if streak_count == 3:
            # Exactly 3 coins have streak, 1 doesn't
            divergent_coin = [c for c in coins if not coin_has_streak[c]][0]
            # The divergent coin's outcome at position i (the "next" cycle)
            next_outcome = outcomes[divergent_coin][i]
            is_down = 1 if next_outcome == "D" else 0
            results_divergent.append((divergent_coin, ts_arr[i], is_down))

        elif streak_count == 4:
            # All 4 coins have streak -- check next outcome for each
            for c in coins:
                next_outcome = outcomes[c][i]
                is_down = 1 if next_outcome == "D" else 0
                results_all_streak.append((c, ts_arr[i], is_down))

    return results_divergent, results_all_streak


# Also compute baseline: when a coin has UU pattern, what's P(DOWN)?
def build_baseline_wr(pivot, coins, pattern_len):
    """Baseline: across all timestamps, when coin has pattern_len U streak,
    what's P(DOWN) on the next cycle?"""
    ts_arr = pivot.index.values
    n = len(ts_arr)
    outcomes = {c: pivot[c].values for c in coins}

    total = 0
    downs = 0
    for c in coins:
        for i in range(pattern_len, n):
            has_streak = True
            for j in range(pattern_len):
                if outcomes[c][i - pattern_len + j] != "U":
                    has_streak = False
                    break
            if has_streak:
                total += 1
                if outcomes[c][i] == "D":
                    downs += 1
    return downs, total


for pat_len, pat_name in [(2, "UU"), (3, "UUU")]:
    print(f"\n{'-' * 80}")
    print(f"Pattern: {pat_name} | Testing cross-coin divergence")
    print(f"{'-' * 80}")

    divergent_trades, all_streak_trades = build_cross_coin_patterns(
        pivot_all4, COINS, pat_len
    )
    baseline_downs, baseline_n = build_baseline_wr(pivot_all4, COINS, pat_len)

    print(f"\n  Baseline ({pat_name} -> next cycle across all coins):")
    baseline_wr = baseline_downs / baseline_n if baseline_n > 0 else 0
    print(f"    N={baseline_n:,}  P(DOWN)={baseline_wr:.4f}")

    # Divergent coin analysis
    div_n = len(divergent_trades)
    div_downs = sum(d for _, _, d in divergent_trades)
    div_wr = div_downs / div_n if div_n > 0 else 0

    print(f"\n  Divergent coin (3/4 have {pat_name}, 1 doesn't) -> next outcome:")
    print(f"    N={div_n:,}  P(DOWN)={div_wr:.4f}  Delta vs baseline={div_wr - baseline_wr:+.4f}")

    if div_n > 0 and baseline_n > 0:
        # Compare divergent vs baseline using Fisher test
        # Divergent: div_downs/div_n vs baseline: baseline_downs/baseline_n
        print_comparison(
            f"Divergent coin's next",
            div_downs, div_n,
            f"Baseline ({pat_name}->next)",
            baseline_downs, baseline_n,
            indent="    ",
        )

    # All-streak analysis
    all_n = len(all_streak_trades)
    all_downs = sum(d for _, _, d in all_streak_trades)
    all_wr = all_downs / all_n if all_n > 0 else 0

    print(f"\n  All 4 coins have {pat_name} simultaneously -> next outcome:")
    print(f"    N={all_n:,}  P(DOWN)={all_wr:.4f}  Delta vs baseline={all_wr - baseline_wr:+.4f}")

    if all_n > 0 and baseline_n > 0:
        print_comparison(
            f"All-4-streak next",
            all_downs, all_n,
            f"Baseline ({pat_name}->next)",
            baseline_downs, baseline_n,
            indent="    ",
        )

    # Per-coin breakdown for divergent
    if div_n > 0:
        print(f"\n  Per-coin breakdown (divergent coin):")
        for coin in COINS:
            coin_trades = [(c, t, d) for c, t, d in divergent_trades if c == coin]
            cn = len(coin_trades)
            cd = sum(d for _, _, d in coin_trades)
            cwr = cd / cn if cn > 0 else 0
            print(f"    {coin}: N={cn:>5,}  P(DOWN)={cwr:.4f}  Delta={cwr - baseline_wr:+.4f}")


# ================================================================
# HYPOTHESIS 2: Streak Speed (Consecutive vs Spread)
# ================================================================

print("\n\n" + "=" * 90)
print("HYPOTHESIS 2: STREAK SPEED (CONSECUTIVE vs SPREAD)")
print("Is UUU in 3 consecutive cycles (no gaps, 300s apart) a stronger")
print("signal than UUU spread over time (gaps present)?")
print("=" * 90)


def analyze_streak_speed(coin, outcomes, timestamps, pattern_len, target):
    """For each pattern occurrence, classify as consecutive (all gaps=300s)
    or spread (at least one gap > 300s).

    Returns (consecutive_trades, spread_trades) where each is list of (coin, idx, hit).
    """
    n = len(outcomes)
    consec = []
    spread = []

    for i in range(pattern_len, n):
        # Check if the pattern_len outcomes before position i match all U
        match = True
        for j in range(pattern_len):
            if outcomes[i - pattern_len + j] != "U":
                match = False
                break
        if not match:
            continue

        # Check if the pattern cycles are consecutive (gap = 300s each)
        is_consecutive = True
        for j in range(1, pattern_len + 1):
            # Check gap between position (i - pattern_len + j - 1) and (i - pattern_len + j)
            if j < pattern_len:
                gap = timestamps[i - pattern_len + j] - timestamps[i - pattern_len + j - 1]
            else:
                # Gap between last pattern cycle and the current cycle (prediction)
                gap = timestamps[i] - timestamps[i - 1]

            if gap != 300:
                is_consecutive = False
                break

        hit = 1 if outcomes[i] == target else 0

        if is_consecutive:
            consec.append((coin, i, hit))
        else:
            spread.append((coin, i, hit))

    return consec, spread


for pat_len, pat_name in [(3, "UUU->DOWN"), (2, "UU->DOWN")]:
    prefix_len = pat_len
    target = "D"
    print(f"\n{'-' * 80}")
    print(f"Pattern: {pat_name}")
    print(f"{'-' * 80}")

    all_consec = []
    all_spread = []

    for coin in COINS:
        cd = coin_data[coin]
        c_trades, s_trades = analyze_streak_speed(
            coin, cd["outcomes"], cd["timestamps"], prefix_len, target
        )
        all_consec.extend(c_trades)
        all_spread.extend(s_trades)

    consec_n = len(all_consec)
    consec_wins = sum(h for _, _, h in all_consec)
    spread_n = len(all_spread)
    spread_wins = sum(h for _, _, h in all_spread)

    print(f"\n  Overall:")
    print_comparison(
        "Consecutive (all gaps=300s)",
        consec_wins, consec_n,
        "Spread (at least 1 gap != 300s)",
        spread_wins, spread_n,
    )

    # Per-coin breakdown
    print(f"\n  Per-coin breakdown:")
    for coin in COINS:
        cc = [(c, i, h) for c, i, h in all_consec if c == coin]
        sc = [(c, i, h) for c, i, h in all_spread if c == coin]
        cc_n, cc_w = len(cc), sum(h for _, _, h in cc)
        sc_n, sc_w = len(sc), sum(h for _, _, h in sc)
        cc_wr = cc_w / cc_n if cc_n > 0 else float("nan")
        sc_wr = sc_w / sc_n if sc_n > 0 else float("nan")
        delta = cc_wr - sc_wr if cc_n > 0 and sc_n > 0 else float("nan")
        p_str = ""
        if cc_n > 0 and sc_n > 0:
            _, p = fisher_2x2(cc_w, cc_n, sc_w, sc_n)
            p_str = f"p={p:.4f} {sig_label(p)}"
        print(
            f"    {coin}: Consec N={cc_n:>5,} WR={cc_wr:.4f}  |  "
            f"Spread N={sc_n:>5,} WR={sc_wr:.4f}  |  "
            f"Delta={delta:+.4f}  {p_str}"
        )


# ================================================================
# HYPOTHESIS 3: Day of Week (Weekend vs Weekday)
# ================================================================

print("\n\n" + "=" * 90)
print("HYPOTHESIS 3: DAY OF WEEK")
print("Do UU->DOWN and UUU->DOWN patterns perform differently on")
print("weekends (Sat/Sun) vs weekdays?")
print("=" * 90)


def build_pattern_trades_with_metadata(coin, outcomes, timestamps, days, weekends,
                                        pattern_prefix, target):
    """Find all pattern matches and record day-of-week metadata."""
    n = len(outcomes)
    prefix_len = len(pattern_prefix)
    trades = []

    for i in range(prefix_len, n):
        match = True
        for j, p in enumerate(pattern_prefix):
            if outcomes[i - prefix_len + j] != p:
                match = False
                break
        if not match:
            continue

        hit = 1 if outcomes[i] == target else 0
        trades.append({
            "coin": coin,
            "idx": i,
            "hit": hit,
            "day_of_week": days[i],
            "is_weekend": weekends[i],
            "timestamp": timestamps[i],
        })
    return trades


PATTERN_CONFIGS = [
    ("UU->DOWN", ["U", "U"], "D"),
    ("UUU->DOWN", ["U", "U", "U"], "D"),
]

DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

for pat_name, prefix, target in PATTERN_CONFIGS:
    print(f"\n{'-' * 80}")
    print(f"Pattern: {pat_name}")
    print(f"{'-' * 80}")

    all_trades = []
    for coin in COINS:
        cd = coin_data[coin]
        trades = build_pattern_trades_with_metadata(
            coin, cd["outcomes"], cd["timestamps"],
            cd["day_of_week"], cd["is_weekend"], prefix, target
        )
        all_trades.extend(trades)

    total_n = len(all_trades)
    total_wins = sum(t["hit"] for t in all_trades)
    total_wr = total_wins / total_n if total_n > 0 else 0

    print(f"\n  Overall: N={total_n:,}  WR={total_wr:.4f}")

    # Weekend vs Weekday
    wkend = [t for t in all_trades if t["is_weekend"]]
    wkday = [t for t in all_trades if not t["is_weekend"]]
    we_n, we_w = len(wkend), sum(t["hit"] for t in wkend)
    wd_n, wd_w = len(wkday), sum(t["hit"] for t in wkday)

    print(f"\n  Weekend vs Weekday:")
    print_comparison(
        "Weekend (Sat+Sun)", we_w, we_n,
        "Weekday (Mon-Fri)", wd_w, wd_n,
    )

    # Per day-of-week breakdown
    print(f"\n  Per day-of-week:")
    for dow in range(7):
        day_trades = [t for t in all_trades if t["day_of_week"] == dow]
        dn = len(day_trades)
        dw = sum(t["hit"] for t in day_trades)
        dwr = dw / dn if dn > 0 else float("nan")
        delta = dwr - total_wr if dn > 0 else float("nan")
        # Binom test vs overall WR
        p_str = ""
        if dn > 0:
            bt = binomtest(dw, dn, total_wr, alternative="two-sided")
            p_str = f"p={bt.pvalue:.4f} {sig_label(bt.pvalue)}"
        print(
            f"    {DOW_NAMES[dow]:3s}: N={dn:>5,}  WR={dwr:.4f}  "
            f"Delta={delta:+.4f}  {p_str}"
        )

    # Per-coin weekend vs weekday
    print(f"\n  Per-coin Weekend vs Weekday:")
    for coin in COINS:
        coin_trades = [t for t in all_trades if t["coin"] == coin]
        c_we = [t for t in coin_trades if t["is_weekend"]]
        c_wd = [t for t in coin_trades if not t["is_weekend"]]
        c_we_n, c_we_w = len(c_we), sum(t["hit"] for t in c_we)
        c_wd_n, c_wd_w = len(c_wd), sum(t["hit"] for t in c_wd)
        c_we_wr = c_we_w / c_we_n if c_we_n > 0 else float("nan")
        c_wd_wr = c_wd_w / c_wd_n if c_wd_n > 0 else float("nan")
        delta = c_we_wr - c_wd_wr if c_we_n > 0 and c_wd_n > 0 else float("nan")
        p_str = ""
        if c_we_n > 0 and c_wd_n > 0:
            _, p = fisher_2x2(c_we_w, c_we_n, c_wd_w, c_wd_n)
            p_str = f"p={p:.4f} {sig_label(p)}"
        print(
            f"    {coin}: WE N={c_we_n:>4,} WR={c_we_wr:.4f}  |  "
            f"WD N={c_wd_n:>4,} WR={c_wd_wr:.4f}  |  "
            f"Delta={delta:+.4f}  {p_str}"
        )


# ================================================================
# HYPOTHESIS 4: Post-Loss Autocorrelation
# ================================================================

print("\n\n" + "=" * 90)
print("HYPOTHESIS 4: POST-LOSS AUTOCORRELATION")
print("After a pattern trade loses, is the NEXT pattern trade")
print("more or less likely to win?")
print("=" * 90)


def build_sequential_pattern_trades(coin, outcomes, prefix, target):
    """Return list of (coin, idx, hit) for all pattern matches in order."""
    n = len(outcomes)
    prefix_len = len(prefix)
    trades = []
    for i in range(prefix_len, n):
        match = True
        for j, p in enumerate(prefix):
            if outcomes[i - prefix_len + j] != p:
                match = False
                break
        if not match:
            continue
        hit = 1 if outcomes[i] == target else 0
        trades.append((coin, i, hit))
    return trades


for pat_name, prefix, target in PATTERN_CONFIGS:
    print(f"\n{'-' * 80}")
    print(f"Pattern: {pat_name}")
    print(f"{'-' * 80}")

    # Collect sequential trades per coin, then analyze transitions
    all_win_after_win = 0
    all_n_after_win = 0
    all_win_after_loss = 0
    all_n_after_loss = 0

    # Also track loss-after-loss and win-after-loss for full picture
    per_coin_stats = {}

    for coin in COINS:
        cd = coin_data[coin]
        trades = build_sequential_pattern_trades(
            coin, cd["outcomes"], prefix, target
        )

        n_trades = len(trades)
        total_wins = sum(h for _, _, h in trades)
        base_wr = total_wins / n_trades if n_trades > 0 else 0

        # Sequential analysis: for each trade i > 0, check outcome conditioned
        # on previous trade's outcome
        waw = 0  # win after win
        naw = 0  # count after win
        wal = 0  # win after loss
        nal = 0  # count after loss
        lal = 0  # loss after loss
        lal_n = 0

        for i in range(1, len(trades)):
            prev_hit = trades[i - 1][2]
            curr_hit = trades[i][2]

            if prev_hit == 1:  # previous was a win
                naw += 1
                if curr_hit == 1:
                    waw += 1
            else:  # previous was a loss
                nal += 1
                if curr_hit == 1:
                    wal += 1

        per_coin_stats[coin] = {
            "n_trades": n_trades,
            "base_wr": base_wr,
            "waw": waw, "naw": naw,
            "wal": wal, "nal": nal,
        }

        all_win_after_win += waw
        all_n_after_win += naw
        all_win_after_loss += wal
        all_n_after_loss += nal

    # Overall
    print(f"\n  Overall ({pat_name}):")
    total_trades = sum(s["n_trades"] for s in per_coin_stats.values())
    overall_wr = sum(s["base_wr"] * s["n_trades"] for s in per_coin_stats.values()) / total_trades
    print(f"    Total trades: {total_trades:,}  Overall WR: {overall_wr:.4f}")

    print_comparison(
        "Win after previous WIN",
        all_win_after_win, all_n_after_win,
        "Win after previous LOSS",
        all_win_after_loss, all_n_after_loss,
    )

    # Also compare each to the base rate
    print(f"\n  Comparison to base rate:")
    if all_n_after_win > 0:
        wr_aw = all_win_after_win / all_n_after_win
        bt = binomtest(all_win_after_win, all_n_after_win, overall_wr, alternative="two-sided")
        print(
            f"    After WIN:  N={all_n_after_win:>6,}  WR={wr_aw:.4f}  "
            f"Delta vs base={wr_aw - overall_wr:+.4f}  "
            f"binom p={bt.pvalue:.4f} {sig_label(bt.pvalue)}"
        )
    if all_n_after_loss > 0:
        wr_al = all_win_after_loss / all_n_after_loss
        bt = binomtest(all_win_after_loss, all_n_after_loss, overall_wr, alternative="two-sided")
        print(
            f"    After LOSS: N={all_n_after_loss:>6,}  WR={wr_al:.4f}  "
            f"Delta vs base={wr_al - overall_wr:+.4f}  "
            f"binom p={bt.pvalue:.4f} {sig_label(bt.pvalue)}"
        )

    # Per-coin breakdown
    print(f"\n  Per-coin breakdown:")
    for coin in COINS:
        s = per_coin_stats[coin]
        waw_wr = s["waw"] / s["naw"] if s["naw"] > 0 else float("nan")
        wal_wr = s["wal"] / s["nal"] if s["nal"] > 0 else float("nan")
        delta = waw_wr - wal_wr if s["naw"] > 0 and s["nal"] > 0 else float("nan")
        p_str = ""
        if s["naw"] > 0 and s["nal"] > 0:
            _, p = fisher_2x2(s["waw"], s["naw"], s["wal"], s["nal"])
            p_str = f"p={p:.4f} {sig_label(p)}"
        print(
            f"    {coin}: Base WR={s['base_wr']:.4f} | "
            f"After Win: N={s['naw']:>4,} WR={waw_wr:.4f} | "
            f"After Loss: N={s['nal']:>4,} WR={wal_wr:.4f} | "
            f"Delta={delta:+.4f}  {p_str}"
        )

    # Streak analysis: consecutive losses
    print(f"\n  Consecutive loss streaks:")
    for coin in COINS:
        cd = coin_data[coin]
        trades = build_sequential_pattern_trades(
            coin, cd["outcomes"], prefix, target
        )
        if len(trades) < 3:
            continue

        # Track streaks of 2+ consecutive losses
        streak_len = 0
        after_streak = {2: [], 3: [], 4: []}

        for i in range(len(trades)):
            hit = trades[i][2]
            if hit == 0:
                streak_len += 1
            else:
                # Win breaks the streak
                if streak_len >= 2:
                    for k in after_streak:
                        if streak_len >= k:
                            after_streak[k].append(1)  # won after k-loss streak
                streak_len = 0

            # If we're at a loss and previous were also losses, record for longer streaks
            if hit == 0 and streak_len >= 2:
                # We won't record the outcome yet -- we record when the streak breaks
                pass

        # Also handle ongoing streaks
        # (streak hasn't been broken by a win, so we don't record anything)

        if any(len(v) > 0 for v in after_streak.values()):
            parts = []
            for k in [2, 3, 4]:
                arr = after_streak[k]
                if arr:
                    parts.append(f"after {k}L: {len(arr)} breaks")
            print(f"    {coin}: {', '.join(parts)}")


# ================================================================
# SUMMARY VERDICTS
# ================================================================

print("\n\n" + "=" * 90)
print("SUMMARY VERDICTS")
print(f"Bonferroni-corrected alpha = {ALPHA_BONF}")
print("=" * 90)

# Re-compute key p-values for the summary

# H1: Cross-coin divergence
print("\n--- H1: Cross-Coin Divergence ---")
for pat_len, pat_name in [(2, "UU"), (3, "UUU")]:
    div_trades, all_trades = build_cross_coin_patterns(pivot_all4, COINS, pat_len)
    base_downs, base_n = build_baseline_wr(pivot_all4, COINS, pat_len)

    div_n = len(div_trades)
    div_downs = sum(d for _, _, d in div_trades)
    div_wr = div_downs / div_n if div_n > 0 else 0
    base_wr = base_downs / base_n if base_n > 0 else 0

    if div_n > 0 and base_n > 0:
        _, p1 = fisher_2x2(div_downs, div_n, base_downs, base_n)
    else:
        p1 = 1.0

    delta = div_wr - base_wr
    verdict = "SIGNIFICANT" if p1 < ALPHA_BONF else ("NEEDS MORE DATA" if div_n < 100 else "DEAD")
    print(
        f"  {pat_name} divergent: N={div_n:,}  P(DOWN)={div_wr:.4f}  "
        f"Baseline={base_wr:.4f}  Delta={delta:+.4f}  "
        f"p={p1:.6f}  -> {verdict}"
    )

# H2: Streak speed
print("\n--- H2: Streak Speed (Consecutive vs Spread) ---")
for pat_len, pat_name in [(3, "UUU->DOWN"), (2, "UU->DOWN")]:
    prefix_len = pat_len
    target = "D"
    all_consec = []
    all_spread = []
    for coin in COINS:
        cd = coin_data[coin]
        c_trades, s_trades = analyze_streak_speed(
            coin, cd["outcomes"], cd["timestamps"], prefix_len, target
        )
        all_consec.extend(c_trades)
        all_spread.extend(s_trades)

    cn, cw = len(all_consec), sum(h for _, _, h in all_consec)
    sn, sw = len(all_spread), sum(h for _, _, h in all_spread)
    c_wr = cw / cn if cn > 0 else 0
    s_wr = sw / sn if sn > 0 else 0

    if cn > 0 and sn > 0:
        _, p2 = fisher_2x2(cw, cn, sw, sn)
    else:
        p2 = 1.0

    delta = c_wr - s_wr
    min_n = min(cn, sn)
    verdict = "SIGNIFICANT" if p2 < ALPHA_BONF else ("NEEDS MORE DATA" if min_n < 100 else "DEAD")
    print(
        f"  {pat_name}: Consec N={cn:,} WR={c_wr:.4f}  |  "
        f"Spread N={sn:,} WR={s_wr:.4f}  |  "
        f"Delta={delta:+.4f}  p={p2:.6f}  -> {verdict}"
    )

# H3: Day of week
print("\n--- H3: Day of Week (Weekend vs Weekday) ---")
for pat_name, prefix, target in PATTERN_CONFIGS:
    all_trades = []
    for coin in COINS:
        cd = coin_data[coin]
        trades = build_pattern_trades_with_metadata(
            coin, cd["outcomes"], cd["timestamps"],
            cd["day_of_week"], cd["is_weekend"], prefix, target
        )
        all_trades.extend(trades)

    wkend = [t for t in all_trades if t["is_weekend"]]
    wkday = [t for t in all_trades if not t["is_weekend"]]
    we_n, we_w = len(wkend), sum(t["hit"] for t in wkend)
    wd_n, wd_w = len(wkday), sum(t["hit"] for t in wkday)
    we_wr = we_w / we_n if we_n > 0 else 0
    wd_wr = wd_w / wd_n if wd_n > 0 else 0

    if we_n > 0 and wd_n > 0:
        _, p3 = fisher_2x2(we_w, we_n, wd_w, wd_n)
    else:
        p3 = 1.0

    delta = we_wr - wd_wr
    min_n = min(we_n, wd_n)
    verdict = "SIGNIFICANT" if p3 < ALPHA_BONF else ("NEEDS MORE DATA" if min_n < 100 else "DEAD")
    print(
        f"  {pat_name}: Weekend N={we_n:,} WR={we_wr:.4f}  |  "
        f"Weekday N={wd_n:,} WR={wd_wr:.4f}  |  "
        f"Delta={delta:+.4f}  p={p3:.6f}  -> {verdict}"
    )

# H4: Post-loss autocorrelation
print("\n--- H4: Post-Loss Autocorrelation ---")
for pat_name, prefix, target in PATTERN_CONFIGS:
    all_waw = 0
    all_naw = 0
    all_wal = 0
    all_nal = 0

    for coin in COINS:
        cd = coin_data[coin]
        trades = build_sequential_pattern_trades(
            coin, cd["outcomes"], prefix, target
        )
        for i in range(1, len(trades)):
            prev_hit = trades[i - 1][2]
            curr_hit = trades[i][2]
            if prev_hit == 1:
                all_naw += 1
                if curr_hit == 1:
                    all_waw += 1
            else:
                all_nal += 1
                if curr_hit == 1:
                    all_wal += 1

    waw_wr = all_waw / all_naw if all_naw > 0 else 0
    wal_wr = all_wal / all_nal if all_nal > 0 else 0

    if all_naw > 0 and all_nal > 0:
        _, p4 = fisher_2x2(all_waw, all_naw, all_wal, all_nal)
    else:
        p4 = 1.0

    delta = waw_wr - wal_wr
    min_n = min(all_naw, all_nal)
    verdict = "SIGNIFICANT" if p4 < ALPHA_BONF else ("NEEDS MORE DATA" if min_n < 100 else "DEAD")
    print(
        f"  {pat_name}: After Win N={all_naw:,} WR={waw_wr:.4f}  |  "
        f"After Loss N={all_nal:,} WR={wal_wr:.4f}  |  "
        f"Delta={delta:+.4f}  p={p4:.6f}  -> {verdict}"
    )

print("\n" + "=" * 90)
print("DONE - four_hypotheses_telonex.py")
print("=" * 90)
