"""
Telonex Backtest - Full pattern analysis on 73 days of Polymarket 5-min data.

Uses the FREE Telonex markets dataset (no API downloads needed) which contains
the resolution (UP/DOWN) of every 5-minute market since Dec 18, 2025.

This gives us 16,000+ BTC cycles vs our previous 471 from self-collected data.
The dataset contains only OUTCOMES (no tick-by-tick prices), so we can test:
  - Runs test (is the sequence random?)
  - UU->DOWN and all other patterns (WR and statistical significance)
  - Pattern performance by hour of day
  - Pattern performance by day of week
  - Temporal stability (rolling window WR)
  - Cross-coin analysis

What we CANNOT test (no price data):
  - Entry price / max_ask filter
  - Slippage / fees / PnL
  - Intra-cycle volatility

Usage:
    python research/telonex_backtest.py
    python research/telonex_backtest.py --coins BTC
    python research/telonex_backtest.py --coins BTC,ETH --pattern UU --side DOWN
    python research/telonex_backtest.py --all-patterns
    python research/telonex_backtest.py --from-date 2026-02-01 --to-date 2026-03-01
"""

import argparse
import sys
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import stats
from scipy.stats import norm

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATA_FILE = Path(__file__).parent.parent / "data" / "telonex_updown_5m.parquet"
COINS = ["BTC", "ETH", "SOL", "XRP"]

# Built-in patterns with default sides
DEFAULT_PATTERNS = {
    "UU": "DOWN",
    "DD": "UP",
    "UUU": "DOWN",
    "DDD": "UP",
    "UD": "DOWN",
    "DU": "UP",
    "DUD": "DOWN",
    "UDU": "UP",
    "UUUU": "DOWN",
    "DDDD": "UP",
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_telonex_data(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> pd.DataFrame:
    """Load Telonex 5-min up/down market data."""
    if not DATA_FILE.exists():
        print(f"ERROR: {DATA_FILE} not found.")
        print("Run: python -c \"import pandas as pd; "
              "df = pd.read_parquet('https://api.telonex.io/v1/datasets/polymarket/markets'); "
              "df[df['slug'].str.contains('-updown-5m-', na=False)]"
              ".to_parquet('data/telonex_updown_5m.parquet', index=False)\"")
        sys.exit(1)

    df = pd.read_parquet(DATA_FILE)

    # Extract coin and timestamp from slug
    df["coin"] = df["slug"].str.extract(r"^(\w+)-updown-5m-")[0].str.upper()
    df["unix_ts"] = df["slug"].str.extract(r"-(\d+)$")[0].astype(float)
    df["datetime"] = pd.to_datetime(df["unix_ts"], unit="s")
    df["date"] = df["datetime"].dt.date
    df["hour"] = df["datetime"].dt.hour
    df["dow"] = df["datetime"].dt.dayofweek  # 0=Monday

    # Outcome: result_id=0 -> UP wins, result_id=1 -> DOWN wins
    df["outcome"] = df["result_id"].map({"0": "UP", "1": "DOWN"})

    # Filter by date
    if from_date:
        df = df[df["datetime"] >= pd.Timestamp(from_date)]
    if to_date:
        df = df[df["datetime"] <= pd.Timestamp(to_date)]

    return df.sort_values(["coin", "unix_ts"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Runs Test
# ---------------------------------------------------------------------------
def runs_test(outcomes: np.ndarray) -> Tuple[float, float, int, int, int]:
    """Wald-Wolfowitz runs test. Returns (z, p, runs, n_up, n_dn)."""
    n = len(outcomes)
    n_up = outcomes.sum()
    n_dn = n - n_up

    if n_up == 0 or n_dn == 0:
        return 0.0, 1.0, 0, int(n_up), int(n_dn)

    runs = 1
    for i in range(1, n):
        if outcomes[i] != outcomes[i - 1]:
            runs += 1

    exp = 1 + 2 * n_up * n_dn / n
    var = (2 * n_up * n_dn * (2 * n_up * n_dn - n)) / (n**2 * (n - 1))
    z = (runs - exp) / np.sqrt(var) if var > 0 else 0.0
    p = 2 * (1 - norm.cdf(abs(z)))
    return z, p, runs, int(n_up), int(n_dn)


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------
def test_pattern(
    ud_seq: List[Optional[str]],
    pattern: str,
    buy_side_ud: str,
) -> Tuple[int, int, int]:
    """Count pattern matches and wins.
    
    Args:
        ud_seq: List of "U", "D", or None for each cycle
        pattern: Pattern string like "UU", "DUD" etc (uses U/D)
        buy_side_ud: Side to buy in U/D format ("U" for UP, "D" for DOWN)
    
    Returns (trades, wins, losses).
    """
    p_len = len(pattern)
    trades = 0
    wins = 0

    for i in range(p_len, len(ud_seq)):
        if ud_seq[i] is None:
            continue
        # Check no Nones in the pattern window
        window = ud_seq[i - p_len:i]
        if any(o is None for o in window):
            continue
        recent = "".join(window)
        if recent == pattern:
            trades += 1
            if ud_seq[i] == buy_side_ud:
                wins += 1

    return trades, wins, trades - wins


def _side_to_ud(side: str) -> str:
    """Convert 'UP'/'DOWN' to 'U'/'D'."""
    return "U" if side == "UP" else "D"


# ---------------------------------------------------------------------------
# Analyses
# ---------------------------------------------------------------------------
def section_runs_test(df: pd.DataFrame, active_coins: List[str]) -> None:
    """Section 1: Runs test per coin."""
    print("\n" + "=" * 78)
    print("  SECTION 1: RUNS TEST (is the outcome sequence random?)")
    print("=" * 78)
    print(f"  {'Coin':<6} {'N':>7} {'UP%':>6} {'runs':>6} {'exp':>6} "
          f"{'z':>7} {'p-value':>9}  Verdict")
    print(f"  {'-'*6} {'-'*7} {'-'*6} {'-'*6} {'-'*6} {'-'*7} {'-'*9}  {'-'*15}")

    for coin in active_coins:
        cdf = df[df["coin"] == coin].sort_values("unix_ts")
        outcomes = (cdf["outcome"] == "UP").astype(int).values
        z, p, runs, n_up, n_dn = runs_test(outcomes)
        n = len(outcomes)
        up_pct = n_up / n if n > 0 else 0

        verdict = "RANDOM"
        if p < 0.05:
            if z > 0:
                verdict = "MEAN-REVERTING *"
            else:
                verdict = "TRENDING *"
        if p < 0.01:
            verdict = verdict.replace("*", "**")

        print(f"  {coin:<6} {n:>7,} {up_pct:>5.1%} {runs:>6,} {n_up*n_dn*2//n+1:>6,} "
              f"{z:>+6.3f} {p:>9.6f}  {verdict}")


def section_patterns(
    df: pd.DataFrame,
    active_coins: List[str],
    patterns: Optional[Dict[str, str]] = None,
) -> None:
    """Section 2: Pattern backtesting."""
    if patterns is None:
        patterns = DEFAULT_PATTERNS

    print("\n" + "=" * 78)
    print("  SECTION 2: PATTERN BACKTESTING (all coins combined)")
    print("=" * 78)

    base_rates = {}
    for coin in active_coins:
        cdf = df[df["coin"] == coin]
        base_rates[coin] = (cdf["outcome"] == "DOWN").mean()
    avg_base = np.mean(list(base_rates.values()))

    print(f"  Base rate DOWN (avg): {avg_base:.1%}")
    print(f"  Coins: {', '.join(active_coins)}")
    print()
    print(f"  {'Pattern':<8} {'Side':<6} {'Trades':>7} {'Wins':>6} {'WR':>6} "
          f"{'vs base':>7} {'z':>7} {'p-val':>9} {'Verdict'}")
    print(f"  {'-'*8} {'-'*6} {'-'*7} {'-'*6} {'-'*6} "
          f"{'-'*7} {'-'*7} {'-'*9} {'-'*20}")

    n_tests = len(patterns)
    bonf_alpha = 0.05 / n_tests

    for pattern, side in sorted(patterns.items(), key=lambda x: len(x[0])):
        total_trades = 0
        total_wins = 0
        base = avg_base if side == "DOWN" else (1 - avg_base)

        for coin in active_coins:
            cdf = df[df["coin"] == coin].sort_values("unix_ts")
            outcomes = cdf["outcome"].tolist()
            # Convert to U/D for pattern matching
            ud = ["U" if o == "UP" else "D" if o == "DOWN" else None for o in outcomes]
            t, w, _ = test_pattern(ud, pattern, _side_to_ud(side))
            total_trades += t
            total_wins += w

        if total_trades == 0:
            continue

        wr = total_wins / total_trades
        delta = wr - base

        # Binomial test: is WR > base rate?
        bt = stats.binomtest(total_wins, total_trades, base, alternative="greater")
        p_val = bt.pvalue

        # z-score
        se = np.sqrt(base * (1 - base) / total_trades)
        z = (wr - base) / se if se > 0 else 0

        verdict = ""
        if p_val < bonf_alpha:
            verdict = f"SIGNIFICANT (Bonf {bonf_alpha:.4f})"
        elif p_val < 0.05:
            verdict = "nominal p<0.05"
        elif delta < -0.02:
            verdict = "NEGATIVE"

        print(f"  {pattern:<8} {side:<6} {total_trades:>7,} {total_wins:>6,} {wr:>5.1%} "
              f"{delta:>+6.1%} {z:>+6.2f} {p_val:>9.6f} {verdict}")


def section_pattern_by_coin(
    df: pd.DataFrame,
    active_coins: List[str],
    pattern: str,
    side: str,
) -> None:
    """Section 3: Single pattern broken down by coin."""
    print(f"\n  --- {pattern}->{side} per coin ---")
    print(f"  {'Coin':<6} {'Trades':>7} {'Wins':>6} {'WR':>6} {'Base':>6} "
          f"{'Delta':>7} {'p-val':>9}")
    print(f"  {'-'*6} {'-'*7} {'-'*6} {'-'*6} {'-'*6} {'-'*7} {'-'*9}")

    for coin in active_coins:
        cdf = df[df["coin"] == coin].sort_values("unix_ts")
        outcomes = cdf["outcome"].tolist()
        ud = ["U" if o == "UP" else "D" if o == "DOWN" else None for o in outcomes]
        t, w, _ = test_pattern(ud, pattern, _side_to_ud(side))

        if t == 0:
            continue

        base = (cdf["outcome"] == side).mean()
        wr = w / t
        delta = wr - base

        bt = stats.binomtest(w, t, base, alternative="greater")
        sig = " *" if bt.pvalue < 0.05 else ""
        if bt.pvalue < 0.01:
            sig = " **"

        print(f"  {coin:<6} {t:>7,} {w:>6,} {wr:>5.1%} {base:>5.1%} "
              f"{delta:>+6.1%} {bt.pvalue:>9.6f}{sig}")


def section_hour_of_day(
    df: pd.DataFrame,
    active_coins: List[str],
    pattern: str,
    side: str,
) -> None:
    """Section 4: Pattern WR by hour of day."""
    print("\n" + "=" * 78)
    print(f"  SECTION 4: {pattern}->{side} BY HOUR OF DAY")
    print("=" * 78)

    # Build trades with hour attached
    hour_trades: Dict[int, Tuple[int, int]] = {}  # hour -> (trades, wins)

    for coin in active_coins:
        cdf = df[df["coin"] == coin].sort_values("unix_ts").reset_index(drop=True)
        outcomes = cdf["outcome"].tolist()
        hours = cdf["hour"].tolist()
        ud = ["U" if o == "UP" else "D" if o == "DOWN" else None for o in outcomes]
        p_len = len(pattern)

        side_ud = _side_to_ud(side)
        for i in range(p_len, len(ud)):
            if ud[i] is None:
                continue
            window = ud[i - p_len:i]
            if any(o is None for o in window):
                continue
            recent = "".join(window)
            if recent == pattern:
                h = hours[i]
                t, w = hour_trades.get(h, (0, 0))
                won = 1 if ud[i] == side_ud else 0
                hour_trades[h] = (t + 1, w + won)

    total_t = sum(v[0] for v in hour_trades.values())
    total_w = sum(v[1] for v in hour_trades.values())
    overall_wr = total_w / total_t if total_t > 0 else 0.5

    print(f"  Overall: {total_t} trades, WR={overall_wr:.1%}")
    print(f"\n  {'Hour UTC':<10} {'Trades':>7} {'Wins':>5} {'WR':>6} "
          f"{'vs avg':>7} {'p-val':>7}")
    print(f"  {'-'*10} {'-'*7} {'-'*5} {'-'*6} {'-'*7} {'-'*7}")

    for h in range(24):
        if h not in hour_trades:
            continue
        t, w = hour_trades[h]
        if t < 10:
            continue
        wr = w / t
        delta = wr - overall_wr
        p = stats.binomtest(w, t, overall_wr).pvalue

        sig = ""
        if p < 0.01:
            sig = " **"
        elif p < 0.05:
            sig = " *"

        print(f"  {h:02d}:00 UTC  {t:>7} {w:>5} {wr:>5.1%} {delta:>+6.1%} {p:>6.3f}{sig}")


def section_temporal(
    df: pd.DataFrame,
    active_coins: List[str],
    pattern: str,
    side: str,
) -> None:
    """Section 5: Rolling WR over time (weekly windows)."""
    print("\n" + "=" * 78)
    print(f"  SECTION 5: {pattern}->{side} TEMPORAL STABILITY (by week)")
    print("=" * 78)

    all_trades = []  # (datetime, won)

    for coin in active_coins:
        cdf = df[df["coin"] == coin].sort_values("unix_ts").reset_index(drop=True)
        outcomes = cdf["outcome"].tolist()
        dts = cdf["datetime"].tolist()
        ud = ["U" if o == "UP" else "D" if o == "DOWN" else None for o in outcomes]
        p_len = len(pattern)

        side_ud = _side_to_ud(side)
        for i in range(p_len, len(ud)):
            if ud[i] is None:
                continue
            window = ud[i - p_len:i]
            if any(o is None for o in window):
                continue
            recent = "".join(window)
            if recent == pattern:
                won = ud[i] == side_ud
                all_trades.append((dts[i], won))

    if not all_trades:
        print("  No trades")
        return

    tdf = pd.DataFrame(all_trades, columns=["dt", "won"])
    tdf["dt"] = pd.to_datetime(tdf["dt"])
    tdf["week"] = tdf["dt"].dt.isocalendar().week.astype(int)
    tdf["year_week"] = tdf["dt"].dt.strftime("%Y-W%W")

    print(f"\n  {'Week':<10} {'Trades':>7} {'Wins':>5} {'WR':>6} {'PnL$':>8}")
    print(f"  {'-'*10} {'-'*7} {'-'*5} {'-'*6} {'-'*8}")

    for wk, group in tdf.groupby("year_week"):
        t = len(group)
        w = group["won"].sum()
        wr = w / t
        # Rough PnL estimate: assume 52.2% WR at ~$0 edge after costs
        # Just show as WR for now
        bar = "#" * int(wr * 20)
        print(f"  {wk:<10} {t:>7} {w:>5} {wr:>5.1%} {bar}")


def section_streak_length(
    df: pd.DataFrame,
    active_coins: List[str],
) -> None:
    """Section 6: Does WR change with streak length (UU vs UUU vs UUUU)?"""
    print("\n" + "=" * 78)
    print("  SECTION 6: WIN RATE BY STREAK LENGTH AT ENTRY")
    print("=" * 78)

    streak_data: Dict[int, Tuple[int, int]] = {}  # streak -> (trades, wins)

    for coin in active_coins:
        cdf = df[df["coin"] == coin].sort_values("unix_ts").reset_index(drop=True)
        outcomes = cdf["outcome"].tolist()
        ud = ["U" if o == "UP" else "D" if o == "DOWN" else None for o in outcomes]

        for i in range(2, len(ud)):
            if ud[i] is None:
                continue
            # Count UP streak ending at i-1
            streak = 0
            for j in range(i - 1, -1, -1):
                if ud[j] == "U":
                    streak += 1
                else:
                    break
            if streak >= 2:
                t, w = streak_data.get(streak, (0, 0))
                won = 1 if ud[i] == "D" else 0
                streak_data[streak] = (t + 1, w + won)

    print(f"  After N consecutive UPs, probability of DOWN:")
    print(f"\n  {'Streak':<10} {'Trades':>7} {'P(DOWN)':>8} {'Note'}")
    print(f"  {'-'*10} {'-'*7} {'-'*8} {'-'*30}")

    for s in sorted(streak_data.keys()):
        t, w = streak_data[s]
        if t < 10:
            continue
        wr = w / t
        note = ""
        if s == 2:
            note = "UU (our entry)"
        elif s == 3:
            note = "UUU (Kelly 1.5x would fire)"
        elif s == 4:
            note = "UUUU (Kelly 2x)"
        elif s >= 5:
            note = f"{'U'*s} (rare)"
        print(f"  {s:<10} {t:>7,} {wr:>7.1%} {note}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Telonex backtest: pattern analysis on 73 days of Polymarket data"
    )
    parser.add_argument("--coins", type=str, default=None,
                        help="Comma-separated coins (default: all 4)")
    parser.add_argument("--pattern", type=str, default=None,
                        help="Single pattern to focus on (e.g. UU)")
    parser.add_argument("--side", type=str, default=None,
                        help="Side for --pattern (e.g. DOWN)")
    parser.add_argument("--all-patterns", action="store_true",
                        help="Test all 10 built-in patterns")
    parser.add_argument("--from-date", type=str, default=None,
                        help="Start date filter (YYYY-MM-DD)")
    parser.add_argument("--to-date", type=str, default=None,
                        help="End date filter (YYYY-MM-DD)")
    args = parser.parse_args()

    active_coins = COINS
    if args.coins:
        active_coins = [c.strip().upper() for c in args.coins.split(",")]

    print(f"\n  Loading Telonex data...")
    df = load_telonex_data(args.from_date, args.to_date)

    # Filter to active coins
    df = df[df["coin"].isin(active_coins)]

    for coin in active_coins:
        n = (df["coin"] == coin).sum()
        print(f"  {coin}: {n:,} cycles")

    date_range = f"{df['date'].min()} to {df['date'].max()}"
    n_days = (df["date"].max() - df["date"].min()).days + 1
    print(f"  Date range: {date_range} ({n_days} days)")

    print("\n" + "=" * 78)
    print("  TELONEX BACKTEST: 73 DAYS OF POLYMARKET 5-MIN DATA")
    print("  Source: Telonex free markets dataset (resolution only, no prices)")
    print("=" * 78)

    # 1. Runs test
    section_runs_test(df, active_coins)

    # 2. Pattern testing
    if args.pattern:
        side = args.side or DEFAULT_PATTERNS.get(args.pattern, "DOWN")
        patterns = {args.pattern: side}
    elif args.all_patterns:
        patterns = DEFAULT_PATTERNS
    else:
        patterns = DEFAULT_PATTERNS

    section_patterns(df, active_coins, patterns)

    # 3. Per-coin breakdown for main pattern
    focus_pattern = args.pattern or "UU"
    focus_side = args.side or DEFAULT_PATTERNS.get(focus_pattern, "DOWN")

    print("\n" + "=" * 78)
    print(f"  SECTION 3: {focus_pattern}->{focus_side} PER COIN")
    print("=" * 78)
    section_pattern_by_coin(df, active_coins, focus_pattern, focus_side)

    # 4. Hour of day
    section_hour_of_day(df, active_coins, focus_pattern, focus_side)

    # 5. Temporal stability
    section_temporal(df, active_coins, focus_pattern, focus_side)

    # 6. Streak length
    section_streak_length(df, active_coins)

    # Final summary
    print("\n" + "=" * 78)
    print("  IMPORTANT CAVEATS")
    print("=" * 78)
    print("""
  This analysis uses OUTCOME DATA ONLY (no prices). It answers:
    "Does the pattern predict the next outcome above the base rate?"

  It does NOT answer:
    "Can you profit from this after slippage, fees, and entry price?"

  A pattern with 52% WR vs 50% base rate is statistically significant
  with n=4000, but the 2pp edge may not cover transaction costs (~4-5pp).

  To estimate profitability, you need the entry ask price, which requires
  either our self-collected CSV data or Telonex quotes downloads (paid).
    """)


if __name__ == "__main__":
    main()
