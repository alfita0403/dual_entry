"""
Streak Overlap Analysis — Do overlapping UP-streak rules amplify losses?

The live config (mean_reversion.yaml) has three rules that trigger on UP streaks:
    UUU   -> DOWN   (fires at streak position 3)
    UUUU  -> DOWN   (fires at streak position 4)
    UUUUU -> DOWN   (fires at streak position 5)

During a long UP streak (e.g., 7 consecutive UPs), the bot potentially fires:
    Position 3:  UUU match   -> buys DOWN (likely loses)
    Position 4:  UUUU match  -> buys DOWN (likely loses)
    Position 5:  UUUUU match -> buys DOWN (likely loses)

This script quantifies the damage from overlapping pattern fires and compares
three execution policies:
    A) "Fire all matching rules"  — every pattern fires when its length is reached
    B) "Fire only longest match"  — wait for the longest, fire once (current live)
    C) "Fire only first match"    — fire UUU once, skip the rest

Data: data/telonex_updown_5m.parquet (74-day resolved 5-min Up/Down markets)
"""

import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import pandas as pd
import numpy as np

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
COINS = ["BTC", "ETH", "SOL", "XRP"]
TRADE_SIZE = 5.0          # $ per trade (matches live config)
PATTERNS = ["UUU", "UUUU", "UUUUU"]  # UP-streak patterns -> DOWN bet
CYCLE_SECONDS = 300       # 5-minute markets


@dataclass
class StreakInfo:
    """Represents a contiguous UP streak in one coin's outcome sequence."""
    coin: str
    length: int
    start_idx: int          # index in the outcome list where streak starts
    outcomes_after: list     # outcomes that follow the streak positions
    timestamp: int = 0      # epoch of first market in streak


@dataclass
class TradeResult:
    """Single simulated trade."""
    coin: str
    pattern: str            # which pattern triggered
    streak_length: int      # total streak length this trade was part of
    position_in_streak: int # at which streak position the trade fired
    outcome: str            # UP or DOWN — the actual outcome of the NEXT market
    won: bool               # True if outcome == DOWN (our bet)
    pnl: float              # simplified P&L


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_data() -> Dict[str, List[str]]:
    """Load parquet, return {coin: [outcome_sequence]} sorted by time."""
    data_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "telonex_updown_5m.parquet"
    )
    df = pd.read_parquet(data_path)
    df = df[df["status"] == "resolved"].copy()
    df["coin"] = df["slug"].str.split("-").str[0].str.upper()
    df["ts"] = df["slug"].str.split("-").str[-1].astype(int)
    df["outcome"] = df["result_id"].map({"0": "UP", "1": "DOWN"})
    df = df.dropna(subset=["outcome"])
    df = df.sort_values(["coin", "ts"])

    sequences = {}
    ts_sequences = {}
    for coin in COINS:
        coin_df = df[df["coin"] == coin].copy()
        sequences[coin] = coin_df["outcome"].tolist()
        ts_sequences[coin] = coin_df["ts"].tolist()

    return sequences, ts_sequences


# ---------------------------------------------------------------------------
# Streak detection
# ---------------------------------------------------------------------------
def find_up_streaks(outcomes: List[str], timestamps: List[int],
                    coin: str, min_length: int = 3) -> List[StreakInfo]:
    """Find all contiguous UP streaks of length >= min_length."""
    streaks = []
    i = 0
    n = len(outcomes)

    while i < n:
        if outcomes[i] == "UP":
            start = i
            while i < n and outcomes[i] == "UP":
                i += 1
            length = i - start
            if length >= min_length:
                # For each position in the streak, track what outcome follows
                # Position k in the streak means k UPs have occurred; the
                # outcome of the (k+1)-th market is what our DOWN bet faces.
                outcomes_after = []
                for k in range(length):
                    idx = start + k + 1
                    if idx < n:
                        outcomes_after.append(outcomes[idx])
                    else:
                        outcomes_after.append(None)
                streaks.append(StreakInfo(
                    coin=coin,
                    length=length,
                    start_idx=start,
                    outcomes_after=outcomes_after,
                    timestamp=timestamps[start] if start < len(timestamps) else 0,
                ))
        else:
            i += 1

    return streaks


# ---------------------------------------------------------------------------
# Trade simulation for each policy
# ---------------------------------------------------------------------------
def simulate_fire_all(streak: StreakInfo) -> List[TradeResult]:
    """Policy A: Fire every matching pattern when its length is reached.

    At streak position 3: UUU fires   -> bet on DOWN, outcome is position 4
    At streak position 4: UUUU fires  -> bet on DOWN, outcome is position 5
    At streak position 5: UUUUU fires -> bet on DOWN, outcome is position 6
    """
    trades = []
    for pat in PATTERNS:
        pat_len = len(pat)
        if streak.length >= pat_len:
            # The bet fires AFTER seeing pat_len UPs in a row.
            # The outcome it bets on is the (pat_len)-th element (0-indexed)
            # of outcomes_after — i.e., the market that follows position pat_len.
            bet_idx = pat_len - 1  # index into outcomes_after
            if bet_idx < len(streak.outcomes_after):
                actual_outcome = streak.outcomes_after[bet_idx]
                if actual_outcome is None:
                    continue
                won = actual_outcome == "DOWN"
                # Simplified P&L: win = +$size * (1 - entry_price) - fees
                # lose = -$size * entry_price
                # At ~0.51 entry: win ~ +$5*0.49 - small fee, lose ~ -$5*0.51
                # For simplicity: win = +TRADE_SIZE * 0.49, lose = -TRADE_SIZE * 0.51
                # More precisely: win -> payout $1/share, paid ~0.51, net ~ +0.49*size
                #                 lose -> payout $0, paid ~0.51, net ~ -0.51*size
                pnl = TRADE_SIZE * 0.49 if won else -TRADE_SIZE * 0.51
                trades.append(TradeResult(
                    coin=streak.coin,
                    pattern=pat,
                    streak_length=streak.length,
                    position_in_streak=pat_len,
                    outcome=actual_outcome,
                    won=won,
                    pnl=pnl,
                ))
    return trades


def simulate_longest_only(streak: StreakInfo) -> List[TradeResult]:
    """Policy B: Fire only the longest matching pattern once (LOOK-AHEAD BIASED).

    WARNING: This policy has look-ahead bias! It knows the final streak length
    before trading. For a streak of exactly N, the longest pattern fires at
    position N, and the next outcome is ALWAYS DOWN (otherwise the streak
    would be longer). This makes it appear unrealistically profitable.

    Included for completeness but should NOT be used for decision-making.
    """
    # Determine the longest pattern that fits
    for pat in reversed(PATTERNS):  # UUUUU, UUUU, UUU
        pat_len = len(pat)
        if streak.length >= pat_len:
            bet_idx = pat_len - 1
            if bet_idx < len(streak.outcomes_after):
                actual_outcome = streak.outcomes_after[bet_idx]
                if actual_outcome is None:
                    return []
                won = actual_outcome == "DOWN"
                pnl = TRADE_SIZE * 0.49 if won else -TRADE_SIZE * 0.51
                return [TradeResult(
                    coin=streak.coin,
                    pattern=pat,
                    streak_length=streak.length,
                    position_in_streak=pat_len,
                    outcome=actual_outcome,
                    won=won,
                    pnl=pnl,
                )]
    return []


def simulate_uuuuu_only(streak: StreakInfo) -> List[TradeResult]:
    """Policy D: Only fire UUUUU (wait for 5 UPs, then bet DOWN once).

    No shorter patterns fire. Realistic alternative: skip UUU and UUUU,
    only trade the strongest signal.
    """
    pat = "UUUUU"
    pat_len = 5
    if streak.length >= pat_len:
        bet_idx = pat_len - 1
        if bet_idx < len(streak.outcomes_after):
            actual_outcome = streak.outcomes_after[bet_idx]
            if actual_outcome is None:
                return []
            won = actual_outcome == "DOWN"
            pnl = TRADE_SIZE * 0.49 if won else -TRADE_SIZE * 0.51
            return [TradeResult(
                coin=streak.coin,
                pattern=pat,
                streak_length=streak.length,
                position_in_streak=pat_len,
                outcome=actual_outcome,
                won=won,
                pnl=pnl,
            )]
    return []


def simulate_first_match(streak: StreakInfo) -> List[TradeResult]:
    """Policy C: Fire only UUU (shortest), then skip all longer patterns."""
    pat = "UUU"
    pat_len = 3
    if streak.length >= pat_len:
        bet_idx = pat_len - 1
        if bet_idx < len(streak.outcomes_after):
            actual_outcome = streak.outcomes_after[bet_idx]
            if actual_outcome is None:
                return []
            won = actual_outcome == "DOWN"
            pnl = TRADE_SIZE * 0.49 if won else -TRADE_SIZE * 0.51
            return [TradeResult(
                coin=streak.coin,
                pattern=pat,
                streak_length=streak.length,
                position_in_streak=pat_len,
                outcome=actual_outcome,
                won=won,
                pnl=pnl,
            )]
    return []


# ---------------------------------------------------------------------------
# Corrected simulation: live behavior fires once per cycle per coin
# ---------------------------------------------------------------------------
def simulate_live_behavior(streak: StreakInfo) -> List[TradeResult]:
    """Simulate actual live behavior: each cycle, the longest matching rule fires.

    During a streak of length N, the live bot sees:
      - After 3 UPs: history is UUU   -> longest match is UUU   -> fires UUU
      - After 4 UPs: history is UUUU  -> longest match is UUUU  -> fires UUUU
      - After 5 UPs: history is UUUUU -> longest match is UUUUU -> fires UUUUU
      - After 6+ UPs: history still ends UUUUU -> fires UUUUU again

    But the live code has a per-cycle traded_coins set — so it trades once per
    cycle per coin. However, each NEW cycle is a fresh opportunity. The key
    insight: at streak position 3, the history IS 'UUU', so UUU fires. At
    position 4, history is 'UUUU', so UUUU fires (a NEW cycle, fresh set).
    At position 5, UUUUU fires. Each of these is a different 5-min market.

    So the live bot actually fires UUU at pos 3, UUUU at pos 4, UUUUU at pos 5+.
    This IS "fire all" — just one per cycle, longest match.
    """
    trades = []
    for pos in range(3, streak.length + 1):
        # At this position, the history ends with `pos` UPs
        # The longest matching pattern is min(pos, 5) U's
        effective_len = min(pos, 5)
        pat = "U" * effective_len

        bet_idx = pos - 1  # outcome after position `pos`
        if bet_idx < len(streak.outcomes_after):
            actual_outcome = streak.outcomes_after[bet_idx]
            if actual_outcome is None:
                continue
            won = actual_outcome == "DOWN"
            pnl = TRADE_SIZE * 0.49 if won else -TRADE_SIZE * 0.51
            trades.append(TradeResult(
                coin=streak.coin,
                pattern=pat,
                streak_length=streak.length,
                position_in_streak=pos,
                outcome=actual_outcome,
                won=won,
                pnl=pnl,
            ))
    return trades


# ---------------------------------------------------------------------------
# 24h rolling worst-case drawdown (across all 4 coins)
# ---------------------------------------------------------------------------
def compute_24h_worst_drawdown(
    sequences: Dict[str, List[str]],
    ts_sequences: Dict[str, List[int]],
    policy: str,
) -> Tuple[float, str]:
    """For a given policy, compute the worst 24h P&L across all 4 coins combined.

    Returns (worst_pnl, description).
    """
    # Build a timeline of all trades with their timestamps
    all_trades_with_ts = []

    for coin in COINS:
        outcomes = sequences[coin]
        timestamps = ts_sequences[coin]
        streaks = find_up_streaks(outcomes, timestamps, coin, min_length=3)

        for streak in streaks:
            if policy == "live":
                trades = simulate_live_behavior(streak)
            elif policy == "longest_only":
                trades = simulate_longest_only(streak)
            elif policy == "first_match":
                trades = simulate_first_match(streak)
            elif policy == "uuuuu_only":
                trades = simulate_uuuuu_only(streak)
            else:
                trades = simulate_fire_all(streak)

            for t in trades:
                # Approximate timestamp of the trade
                trade_ts = streak.timestamp + t.position_in_streak * CYCLE_SECONDS
                all_trades_with_ts.append((trade_ts, t.pnl, t.coin, t.pattern))

    if not all_trades_with_ts:
        return 0.0, "No trades"

    all_trades_with_ts.sort(key=lambda x: x[0])

    # 24h sliding window
    window_sec = 24 * 3600
    worst_pnl = 0.0
    worst_start = 0
    worst_end = 0
    n = len(all_trades_with_ts)

    for i in range(n):
        running_pnl = 0.0
        for j in range(i, n):
            if all_trades_with_ts[j][0] - all_trades_with_ts[i][0] > window_sec:
                break
            running_pnl += all_trades_with_ts[j][1]
            if running_pnl < worst_pnl:
                worst_pnl = running_pnl
                worst_start = i
                worst_end = j

    if worst_pnl < 0:
        ts_start = all_trades_with_ts[worst_start][0]
        ts_end = all_trades_with_ts[worst_end][0]
        from datetime import datetime, timezone
        dt_start = datetime.fromtimestamp(ts_start, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        dt_end = datetime.fromtimestamp(ts_end, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

        # Count trades in that window
        trades_in_window = 0
        losses_in_window = 0
        for k in range(worst_start, worst_end + 1):
            trades_in_window += 1
            if all_trades_with_ts[k][1] < 0:
                losses_in_window += 1

        desc = (f"{dt_start} to {dt_end} UTC | "
                f"{trades_in_window} trades, {losses_in_window} losses")
    else:
        desc = "No drawdown"

    return worst_pnl, desc


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------
def main():
    print("=" * 80)
    print("STREAK OVERLAP ANALYSIS")
    print("Do overlapping UP-streak rules amplify losses during long streaks?")
    print("=" * 80)
    print()

    sequences, ts_sequences = load_data()

    # -----------------------------------------------------------------------
    # 1. Dataset summary
    # -----------------------------------------------------------------------
    print("DATASET SUMMARY")
    print("-" * 60)
    for coin in COINS:
        n = len(sequences[coin])
        ups = sum(1 for o in sequences[coin] if o == "UP")
        print(f"  {coin:4s}: {n:6d} markets | UP: {ups:5d} ({100*ups/n:.1f}%) | "
              f"DOWN: {n-ups:5d} ({100*(n-ups)/n:.1f}%)")
    print()

    # -----------------------------------------------------------------------
    # 2. Streak length distribution per coin
    # -----------------------------------------------------------------------
    print("UP STREAK LENGTH DISTRIBUTION (streaks >= 3)")
    print("-" * 70)
    all_streaks: Dict[str, List[StreakInfo]] = {}

    for coin in COINS:
        streaks = find_up_streaks(
            sequences[coin], ts_sequences[coin], coin, min_length=3
        )
        all_streaks[coin] = streaks

    # Aggregate by length
    max_streak_len = 0
    streak_counts: Dict[str, Dict[int, int]] = {c: defaultdict(int) for c in COINS}
    for coin in COINS:
        for s in all_streaks[coin]:
            streak_counts[coin][s.length] += 1
            max_streak_len = max(max_streak_len, s.length)

    header = f"{'Length':>8s}"
    for coin in COINS:
        header += f" | {coin:>6s}"
    header += " |  Total"
    print(header)
    print("-" * len(header))
    for length in range(3, max_streak_len + 1):
        row = f"{length:>8d}"
        total = 0
        for coin in COINS:
            cnt = streak_counts[coin].get(length, 0)
            row += f" | {cnt:>6d}"
            total += cnt
        row += f" | {total:>6d}"
        print(row)
    total_row = f"{'TOTAL':>8s}"
    grand_total = 0
    for coin in COINS:
        t = sum(streak_counts[coin].values())
        total_row += f" | {t:>6d}"
        grand_total += t
    total_row += f" | {grand_total:>6d}"
    print("-" * len(header))
    print(total_row)
    print()

    # -----------------------------------------------------------------------
    # 3. Streak continuation probability
    # -----------------------------------------------------------------------
    print("STREAK CONTINUATION PROBABILITY")
    print("(Given N consecutive UPs, what % does the next market also resolve UP?)")
    print("-" * 60)
    for coin in COINS:
        outcomes = sequences[coin]
        n = len(outcomes)
        print(f"\n  {coin}:")
        for streak_len in range(3, min(max_streak_len + 1, 10)):
            # Count how many times we see streak_len consecutive UPs
            # and what the next outcome is
            continues = 0
            breaks = 0
            for i in range(n - streak_len):
                if all(outcomes[i + k] == "UP" for k in range(streak_len)):
                    # Check if position before was NOT UP (streak starts here)
                    # Actually, we want: after seeing exactly streak_len UPs ending
                    # at position i+streak_len-1, what's the next outcome?
                    # But simpler: just check any run of streak_len UPs.
                    next_out = outcomes[i + streak_len]
                    if next_out == "UP":
                        continues += 1
                    else:
                        breaks += 1
            total = continues + breaks
            if total > 0:
                cont_pct = 100 * continues / total
                print(f"    After {streak_len} UPs: {cont_pct:5.1f}% continue UP "
                      f"({continues}/{total}) | {100-cont_pct:5.1f}% break DOWN")
    print()

    # -----------------------------------------------------------------------
    # 4. Simulate all three policies per streak
    # -----------------------------------------------------------------------
    print("=" * 80)
    print("POLICY COMPARISON: How each execution policy performs on UP streaks")
    print("=" * 80)
    print()

    policies = {
        "A) Live behavior (longest per cycle)": simulate_live_behavior,
        "B) Longest-only (LOOK-AHEAD BIASED)": simulate_longest_only,
        "C) Fire first match UUU only": simulate_first_match,
        "D) Fire only UUUUU (skip shorter)": simulate_uuuuu_only,
    }

    for policy_name, sim_func in policies.items():
        print(f"\n--- {policy_name} ---")
        total_trades = 0
        total_wins = 0
        total_pnl = 0.0
        max_streak_loss = 0.0
        max_streak_loss_info = ""
        all_losing_count = 0
        total_streaks_with_trades = 0
        three_plus_losses = 0
        pnl_by_coin = {c: 0.0 for c in COINS}
        trades_by_coin = {c: 0 for c in COINS}
        wins_by_coin = {c: 0 for c in COINS}

        for coin in COINS:
            for streak in all_streaks[coin]:
                trades = sim_func(streak)
                if not trades:
                    continue
                total_streaks_with_trades += 1

                streak_pnl = sum(t.pnl for t in trades)
                streak_losses = sum(1 for t in trades if not t.won)
                streak_wins = sum(1 for t in trades if t.won)

                for t in trades:
                    total_trades += 1
                    trades_by_coin[t.coin] += 1
                    pnl_by_coin[t.coin] += t.pnl
                    if t.won:
                        total_wins += 1
                        wins_by_coin[t.coin] += 1
                    total_pnl += t.pnl

                if streak_pnl < max_streak_loss:
                    max_streak_loss = streak_pnl
                    fired = [t.pattern for t in trades]
                    outcomes = [t.outcome for t in trades]
                    max_streak_loss_info = (
                        f"{coin} streak={streak.length} | "
                        f"fired: {fired} | outcomes: {outcomes} | "
                        f"P&L: ${streak_pnl:.2f}"
                    )

                if all(not t.won for t in trades) and len(trades) > 0:
                    all_losing_count += 1

                if streak_losses >= 3:
                    three_plus_losses += 1

        win_rate = 100 * total_wins / total_trades if total_trades > 0 else 0
        print(f"  Total trades:           {total_trades:>6d}")
        print(f"  Total wins:             {total_wins:>6d} ({win_rate:.1f}%)")
        print(f"  Total P&L:              ${total_pnl:>+10.2f}")
        print(f"  P&L per trade:          ${total_pnl/total_trades:>+8.2f}" if total_trades else "")
        print(f"  Streaks with trades:    {total_streaks_with_trades:>6d}")
        print(f"  Streaks where ALL lost: {all_losing_count:>6d} "
              f"({100*all_losing_count/total_streaks_with_trades:.1f}%)"
              if total_streaks_with_trades else "")
        print(f"  Streaks with 3+ losses: {three_plus_losses:>6d} "
              f"({100*three_plus_losses/total_streaks_with_trades:.1f}%)"
              if total_streaks_with_trades else "")
        print(f"  Worst single-streak:    {max_streak_loss_info}")
        print()

        print(f"  {'Coin':>6s} | {'Trades':>7s} | {'Wins':>6s} | {'WR%':>6s} | {'P&L':>10s} | {'$/trade':>8s}")
        print(f"  {'-'*6}-+-{'-'*7}-+-{'-'*6}-+-{'-'*6}-+-{'-'*10}-+-{'-'*8}")
        for coin in COINS:
            t = trades_by_coin[coin]
            w = wins_by_coin[coin]
            p = pnl_by_coin[coin]
            wr = 100 * w / t if t > 0 else 0
            pt = p / t if t > 0 else 0
            print(f"  {coin:>6s} | {t:>7d} | {w:>6d} | {wr:>5.1f}% | ${p:>+9.2f} | ${pt:>+7.2f}")
        print()

    # -----------------------------------------------------------------------
    # 5. Deep-dive: streak-by-streak comparison for the "live" policy
    # -----------------------------------------------------------------------
    print("=" * 80)
    print("DEEP-DIVE: Per-streak-length analysis (Live behavior policy)")
    print("=" * 80)
    print()

    # Group trades by streak length
    trades_by_streak_len: Dict[int, List[TradeResult]] = defaultdict(list)
    for coin in COINS:
        for streak in all_streaks[coin]:
            trades = simulate_live_behavior(streak)
            for t in trades:
                trades_by_streak_len[t.streak_length].append(t)

    print(f"  {'Streak':>7s} | {'Trades':>7s} | {'Wins':>6s} | {'WR%':>6s} | "
          f"{'Total PnL':>10s} | {'Avg PnL':>8s} | {'Avg Trades':>10s} | "
          f"{'Max Loss':>10s}")
    print(f"  {'-'*7}-+-{'-'*7}-+-{'-'*6}-+-{'-'*6}-+-"
          f"{'-'*10}-+-{'-'*8}-+-{'-'*10}-+-{'-'*10}")

    for streak_len in sorted(trades_by_streak_len.keys()):
        trades = trades_by_streak_len[streak_len]
        wins = sum(1 for t in trades if t.won)
        total_pnl = sum(t.pnl for t in trades)
        wr = 100 * wins / len(trades) if trades else 0

        # Count streaks of this length
        n_streaks = sum(1 for c in COINS
                        for s in all_streaks[c] if s.length == streak_len)
        avg_trades = len(trades) / n_streaks if n_streaks > 0 else 0

        # Worst single streak P&L
        streak_pnls = []
        for coin in COINS:
            for s in all_streaks[coin]:
                if s.length == streak_len:
                    st = simulate_live_behavior(s)
                    if st:
                        streak_pnls.append(sum(t.pnl for t in st))
        worst = min(streak_pnls) if streak_pnls else 0

        print(f"  {streak_len:>7d} | {len(trades):>7d} | {wins:>6d} | {wr:>5.1f}% | "
              f"${total_pnl:>+9.2f} | ${total_pnl/len(trades):>+7.2f} | "
              f"{avg_trades:>10.1f} | ${worst:>+9.2f}")
    print()

    # -----------------------------------------------------------------------
    # 6. Tail risk: % of streaks causing 3+ consecutive losses
    # -----------------------------------------------------------------------
    print("=" * 80)
    print("TAIL RISK ANALYSIS (Live behavior)")
    print("=" * 80)
    print()

    # Per-streak: how many trades lose consecutively?
    consec_loss_counts = defaultdict(int)  # {n_consec_losses: count}
    total_analyzed = 0

    for coin in COINS:
        for streak in all_streaks[coin]:
            trades = simulate_live_behavior(streak)
            if not trades:
                continue
            total_analyzed += 1

            # Count max consecutive losses within this streak's trades
            max_consec = 0
            current_consec = 0
            for t in trades:
                if not t.won:
                    current_consec += 1
                    max_consec = max(max_consec, current_consec)
                else:
                    current_consec = 0
            consec_loss_counts[max_consec] += 1

    print(f"  {'Max Consec Losses':>20s} | {'Streaks':>8s} | {'%':>7s} | {'Cumulative >=':>14s}")
    print(f"  {'-'*20}-+-{'-'*8}-+-{'-'*7}-+-{'-'*14}")
    cumulative = total_analyzed
    for n in sorted(consec_loss_counts.keys()):
        cnt = consec_loss_counts[n]
        pct = 100 * cnt / total_analyzed
        cum_pct = 100 * cumulative / total_analyzed
        print(f"  {n:>20d} | {cnt:>8d} | {pct:>6.1f}% | {cum_pct:>13.1f}%")
        cumulative -= cnt
    print()

    # -----------------------------------------------------------------------
    # 7. Worst 24h drawdown across all 4 coins
    # -----------------------------------------------------------------------
    print("=" * 80)
    print("WORST 24-HOUR DRAWDOWN (all 4 coins combined)")
    print("=" * 80)
    print()

    policy_labels = [
        ("live", "A) Live behavior (longest per cycle)"),
        ("longest_only", "B) Longest-only (LOOK-AHEAD BIASED)"),
        ("first_match", "C) Fire first match UUU only"),
        ("uuuuu_only", "D) Fire only UUUUU (skip shorter)"),
    ]

    for policy_key, label in policy_labels:
        worst_pnl, desc = compute_24h_worst_drawdown(
            sequences, ts_sequences, policy_key
        )
        print(f"  {label}")
        print(f"    Worst 24h P&L: ${worst_pnl:+.2f}")
        print(f"    Window:        {desc}")
        print()

    # -----------------------------------------------------------------------
    # 8. Concrete example: anatomy of the longest streaks
    # -----------------------------------------------------------------------
    print("=" * 80)
    print("ANATOMY OF LONGEST UP STREAKS (top 10 by length)")
    print("=" * 80)
    print()

    all_streaks_flat = []
    for coin in COINS:
        for s in all_streaks[coin]:
            all_streaks_flat.append(s)

    all_streaks_flat.sort(key=lambda s: s.length, reverse=True)
    top_streaks = all_streaks_flat[:10]

    for i, streak in enumerate(top_streaks, 1):
        trades_live = simulate_live_behavior(streak)
        trades_first = simulate_first_match(streak)
        trades_u5 = simulate_uuuuu_only(streak)

        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(streak.timestamp, tz=timezone.utc)
        date_str = dt.strftime("%Y-%m-%d %H:%M UTC")

        print(f"  #{i}: {streak.coin} | {streak.length} consecutive UPs | {date_str}")
        print(f"       Outcomes after each position: {streak.outcomes_after[:streak.length]}")

        if trades_live:
            pnl_live = sum(t.pnl for t in trades_live)
            detail = " | ".join(
                f"pos {t.position_in_streak}: {t.pattern}->{t.outcome}({'W' if t.won else 'L'})"
                for t in trades_live
            )
            print(f"       Live policy:    {len(trades_live):>2d} trades, P&L ${pnl_live:+.2f} [{detail}]")

        if trades_first:
            pnl_fo = sum(t.pnl for t in trades_first)
            t = trades_first[0]
            print(f"       First-match:     1 trade,  P&L ${pnl_fo:+.2f} "
                  f"[{t.pattern}->{t.outcome}({'W' if t.won else 'L'})]")

        if trades_u5:
            pnl_u5 = sum(t.pnl for t in trades_u5)
            t = trades_u5[0]
            print(f"       UUUUU-only:      1 trade,  P&L ${pnl_u5:+.2f} "
                  f"[{t.pattern}->{t.outcome}({'W' if t.won else 'L'})]")
        print()

    # -----------------------------------------------------------------------
    # 9. Summary & recommendations
    # -----------------------------------------------------------------------
    print("=" * 80)
    print("KEY FINDINGS & COMPARISON SUMMARY")
    print("=" * 80)
    print()

    # Compute total P&L for each policy across all streaks
    summary = {}
    for policy_name, sim_func in [
        ("Live (longest/cycle)", simulate_live_behavior),
        ("Longest-only *BIASED*", simulate_longest_only),
        ("First-match (UUU)", simulate_first_match),
        ("UUUUU-only", simulate_uuuuu_only),
    ]:
        total_trades = 0
        total_wins = 0
        total_pnl = 0.0
        for coin in COINS:
            for streak in all_streaks[coin]:
                trades = sim_func(streak)
                for t in trades:
                    total_trades += 1
                    if t.won:
                        total_wins += 1
                    total_pnl += t.pnl
        wr = 100 * total_wins / total_trades if total_trades else 0
        avg_pnl = total_pnl / total_trades if total_trades else 0
        summary[policy_name] = (total_trades, total_wins, wr, total_pnl, avg_pnl)

    print(f"  {'Policy':>25s} | {'Trades':>7s} | {'Wins':>6s} | {'WR%':>6s} | "
          f"{'Total PnL':>10s} | {'$/trade':>8s}")
    print(f"  {'-'*25}-+-{'-'*7}-+-{'-'*6}-+-{'-'*6}-+-{'-'*10}-+-{'-'*8}")
    for name, (trades, wins, wr, pnl, avg) in summary.items():
        print(f"  {name:>25s} | {trades:>7d} | {wins:>6d} | {wr:>5.1f}% | "
              f"${pnl:>+9.2f} | ${avg:>+7.2f}")
    print()

    # Compute the "overlap penalty" — extra losses from firing multiple times
    live_trades, _, _, live_pnl, _ = summary["Live (longest/cycle)"]
    fm_trades, _, _, fm_pnl, _ = summary["First-match (UUU)"]
    u5_trades, _, _, u5_pnl, _ = summary["UUUUU-only"]

    print(f"  NOTE: 'Longest-only *BIASED*' has look-ahead bias (knows final streak")
    print(f"  length before trading). It always wins on streaks <= 5 because the")
    print(f"  next outcome after the streak end is guaranteed DOWN. Not realistic.")
    print()

    extra_trades_fm = live_trades - fm_trades
    extra_pnl_fm = live_pnl - fm_pnl
    print(f"  OVERLAP PENALTY (Live vs First-match UUU only):")
    print(f"    Extra trades from overlapping fires: {extra_trades_fm}")
    print(f"    Extra P&L from those trades:         ${extra_pnl_fm:+.2f}")
    if extra_trades_fm > 0:
        print(f"    Avg P&L of extra trades:             ${extra_pnl_fm/extra_trades_fm:+.2f}")
    print()

    extra_trades_u5 = live_trades - u5_trades
    extra_pnl_u5 = live_pnl - u5_pnl
    print(f"  OVERLAP PENALTY (Live vs UUUUU-only):")
    print(f"    Extra trades from overlapping fires: {extra_trades_u5}")
    print(f"    Extra P&L from those trades:         ${extra_pnl_u5:+.2f}")
    if extra_trades_u5 > 0:
        print(f"    Avg P&L of extra trades:             ${extra_pnl_u5/extra_trades_u5:+.2f}")
    print()

    # What does the overlap cost per streak for long streaks?
    print(f"  PER-STREAK OVERLAP COST (streaks >= 5, live policy):")
    for slen in range(5, max_streak_len + 1):
        n_trades_per_streak = slen - 2  # fires at pos 3, 4, ..., slen
        # With first-match, only 1 trade at pos 3
        # With UUUUU-only, only 1 trade at pos 5
        # With live, n_trades_per_streak trades
        overlap_trades = n_trades_per_streak - 1
        streaks_of_len = sum(streak_counts[c].get(slen, 0) for c in COINS)
        if streaks_of_len > 0:
            # Compute actual average PnL for overlapping trades in this length
            overlap_pnl = 0.0
            for coin in COINS:
                for s in all_streaks[coin]:
                    if s.length == slen:
                        trades = simulate_live_behavior(s)
                        # Skip the first trade (UUU at pos 3)
                        for t in trades[1:]:
                            overlap_pnl += t.pnl
            avg_overlap = overlap_pnl / (streaks_of_len * overlap_trades) if overlap_trades > 0 else 0
            print(f"    Streak={slen:2d}: {streaks_of_len:3d} occurrences x "
                  f"{overlap_trades} extra trades = "
                  f"{streaks_of_len * overlap_trades:4d} extra bets, "
                  f"total ${overlap_pnl:+8.2f} "
                  f"(avg ${avg_overlap:+.2f}/trade)")
    print()


if __name__ == "__main__":
    main()
