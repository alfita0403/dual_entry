"""Research V3 — Hypothesis-Driven Strategy Analysis.

Fundamental change from v2: NO grid search.  Each strategy has fixed
parameters derived from economic theory, not from the data.  This
eliminates the multiple-comparisons overfitting that plagued v2.

Statistical framework:
  - Bootstrap 95% CI on expected edge
  - Permutation test (sign-flip) for p-values
  - Bonferroni correction across all tested hypotheses
  - Chronological train/test split (60/40)
  - Base-rate analysis to ground-truth market behavior

Usage:
    python strategies/research_v3.py
"""

import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

# Reuse data loading from v2
from research_v2 import load_data, get_cycles, determine_outcomes, COINS

# ── Worst-case trading assumptions ──────────────────────────────
FEE = 0.02
SLIPPAGE = 0.01
ENTRY_DELAY = 5
BET = 10.0
START = 100.0
N_BOOT = 10000
N_PERM = 5000
ALPHA = 0.05

# ── Simulation (identical to v2 but fixed params) ───────────────

def simulate(signals, tp, timeout):
    """Simulate trades with worst-case assumptions baked in."""
    expire_mode = timeout >= 300
    trades = []
    for sig in signals:
        entry = sig["entry_ask"] + SLIPPAGE
        ft, fb = sig["future_t"], sig["future_bid"]
        max_t = min(sig["t"] + timeout, 300)
        tp_min_t = sig["t"] + ENTRY_DELAY

        tp_mask = (fb >= entry + tp) & (ft <= max_t) & (ft >= tp_min_t)
        tp_idx = np.argmax(tp_mask) if tp_mask.any() else -1

        if tp_idx >= 0 and tp_mask[tp_idx]:
            pnl = tp - FEE
            hold = int(ft[tp_idx]) - sig["t"]
            etype = "TP"
        elif expire_mode:
            oc = sig["outcome"]
            if oc == "UP":
                pnl = 1.0 - entry - FEE
            elif oc == "DOWN":
                pnl = -entry - FEE
            else:
                continue
            hold = 300 - sig["t"]
            etype = "EX"
        else:
            to_mask = ft <= max_t
            if to_mask.any():
                last_i = np.where(to_mask)[0][-1]
                pnl = float(fb[last_i]) - entry - FEE
                hold = int(ft[last_i]) - sig["t"]
                etype = "TO"
            else:
                oc = sig["outcome"]
                if oc == "UP":
                    pnl = 1.0 - entry - FEE
                elif oc == "DOWN":
                    pnl = -entry - FEE
                else:
                    continue
                hold = 300 - sig["t"]
                etype = "EX"
        trades.append({"pnl": pnl, "hold": hold, "exit": etype, "ci": sig["ci"]})
    return trades


# ── Statistical functions ───────────────────────────────────────

def bootstrap_ci(pnls, n_boot=N_BOOT):
    """95% CI on mean PnL via bootstrap."""
    arr = np.array(pnls)
    n = len(arr)
    rng = np.random.default_rng(42)
    boot = rng.choice(arr, size=(n_boot, n), replace=True)
    means = boot.mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def permutation_pvalue(pnls, n_perm=N_PERM):
    """One-sided sign-flip test: H0: E[pnl] <= 0."""
    arr = np.array(pnls)
    real_mean = np.mean(arr)
    rng = np.random.default_rng(42)
    signs = rng.choice([-1, 1], size=(n_perm, len(arr)))
    perm_means = (arr * signs).mean(axis=1)
    return float(np.mean(perm_means >= real_mean))


def strategy_stats(trades, label=""):
    """Compute all statistics for a set of trades."""
    if not trades:
        return None
    pnls = np.array([t["pnl"] for t in trades])
    n = len(pnls)
    wins = np.sum(pnls > 0)
    gross_w = float(np.sum(pnls[pnls > 0])) if wins > 0 else 0.0
    gross_l = float(np.sum(np.abs(pnls[pnls <= 0])))
    edge = float(np.mean(pnls))
    wr = wins / n
    pf = gross_w / gross_l if gross_l > 0 else float("inf")
    sharpe = edge / float(np.std(pnls)) if np.std(pnls) > 0 else 0.0

    ci_lo, ci_hi = bootstrap_ci(pnls) if n >= 5 else (edge, edge)
    pval = permutation_pvalue(pnls) if n >= 5 else 1.0

    # Exit breakdown
    tp_n = sum(1 for t in trades if t["exit"] == "TP")
    to_n = sum(1 for t in trades if t["exit"] == "TO")
    ex_n = sum(1 for t in trades if t["exit"] == "EX")
    avg_hold = float(np.mean([t["hold"] for t in trades]))

    # Bankroll simulation
    bal = START
    peak = START
    max_dd = 0.0
    for t in trades:
        bal += t["pnl"] * BET
        if bal > peak:
            peak = bal
        dd = (peak - bal) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    return {
        "label": label, "n": n, "edge": edge, "wr": wr, "pf": pf,
        "sharpe": sharpe, "ci_lo": ci_lo, "ci_hi": ci_hi, "pval": pval,
        "final": bal, "max_dd": max_dd * 100,
        "tp": tp_n, "to": to_n, "ex": ex_n, "avg_hold": avg_hold,
    }


# ── Signal generators ──────────────────────────────────────────
# Each returns list of signal dicts compatible with simulate().
# Parameters are FIXED by theory — no grid search.

def _build_signal(ci, t, coin, entry_ask, outcome, cycle):
    """Helper: build a signal dict with future bid data."""
    t_arr = cycle["seconds_elapsed"].values
    fb = cycle[f"{coin.lower()}_up_bid"].values
    mask = t_arr > t
    return {
        "ci": ci, "t": t, "coin": coin, "entry_ask": entry_ask,
        "outcome": outcome, "future_t": t_arr[mask], "future_bid": fb[mask],
    }


def H1_statarb(cycles, outcomes, spread=0.15, max_t=60):
    """Cross-sectional mean reversion.

    WHY: 4 coins track the same crypto market direction.  When one
    coin's UP ask diverges below the group mean, it's a temporary
    mispricing due to MM latency or liquidity imbalance.  The gap
    closes as arbitrageurs act.

    Params: spread=0.15 (~1.5σ of typical group dispersion), window=60s.
    """
    signals = []
    for ci, (cycle, oc) in enumerate(zip(cycles, outcomes)):
        t_arr = cycle["seconds_elapsed"].values
        traded = False
        for idx in range(len(cycle)):
            if traded:
                break
            t = int(t_arr[idx])
            if t > max_t:
                break
            row = cycle.iloc[idx]
            asks = {}
            for c in COINS:
                a = row[f"{c.lower()}_up_ask"]
                if 0.10 <= a <= 0.90:
                    asks[c] = a
            if len(asks) < 4:
                continue
            gm = sum(asks.values()) / len(asks)
            best_c, best_d = None, 0.0
            for c, a in asks.items():
                d = gm - a
                if d > best_d:
                    best_c, best_d = c, d
            if best_c and best_d >= spread:
                signals.append(_build_signal(
                    ci, t, best_c, asks[best_c], oc.get(best_c), cycle
                ))
                traded = True
    return signals


def H2_cheap_asymmetry(cycles, outcomes, max_price=0.25, max_t=120):
    """Payout asymmetry on cheap shares.

    WHY: When UP ask < 0.25, market implies < 25% UP probability.
    If actual probability is even slightly higher (e.g. 30%), the
    asymmetric payout ($1 if wins, $0 if loses) creates positive EV.
    Hold to expiry to capture full asymmetry.

    Params: max_price=0.25 (roughly P(UP) < 25%), window=120s.
    Exit: hold to resolution (no TP, no timeout sell).
    """
    signals = []
    for ci, (cycle, oc) in enumerate(zip(cycles, outcomes)):
        t_arr = cycle["seconds_elapsed"].values
        traded = False
        for idx in range(len(cycle)):
            if traded:
                break
            t = int(t_arr[idx])
            if t > max_t:
                break
            row = cycle.iloc[idx]
            best_c, best_ask = None, max_price
            for c in COINS:
                a = row[f"{c.lower()}_up_ask"]
                if a < 0.10:
                    continue
                if a < best_ask:
                    best_c, best_ask = c, a
            if best_c:
                signals.append(_build_signal(
                    ci, t, best_c, best_ask, oc.get(best_c), cycle
                ))
                traded = True
    return signals


def H3_momentum(cycles, outcomes, lookback=20, min_move=0.05, max_t=60):
    """Early momentum continuation.

    WHY: In crypto, momentum is a well-documented factor.  Informed
    traders act early in the 5-min window.  If a coin's UP ask rose
    significantly in the first `lookback` seconds, it's likely to
    continue rising as more participants pile in.

    Params: lookback=20s, min_move=0.05, window=60s.
    Signal fires ONCE at t=lookback for the coin with strongest momentum.
    """
    signals = []
    for ci, (cycle, oc) in enumerate(zip(cycles, outcomes)):
        t_arr = cycle["seconds_elapsed"].values
        if len(t_arr) < lookback + 10:
            continue
        # Opening asks (first row)
        open_asks = {c: cycle.iloc[0][f"{c.lower()}_up_ask"] for c in COINS}
        # Find row at t=lookback
        lb_idx = np.searchsorted(t_arr, lookback)
        if lb_idx >= len(cycle):
            continue
        t = int(t_arr[lb_idx])
        if t > max_t:
            continue
        row = cycle.iloc[lb_idx]
        # Coin with biggest INCREASE
        best_c, best_move = None, 0.0
        for c in COINS:
            current = row[f"{c.lower()}_up_ask"]
            move = current - open_asks[c]  # positive = UP ask rose
            if move > best_move and 0.10 <= current <= 0.90:
                best_c, best_move = c, move
        if best_c and best_move >= min_move:
            signals.append(_build_signal(
                ci, t, best_c, row[f"{best_c.lower()}_up_ask"],
                oc.get(best_c), cycle
            ))
    return signals


def H4_reversal(cycles, outcomes, lookback=20, min_drop=0.05, max_t=60):
    """Early dip reversal.

    WHY: In illiquid 5-min markets, early drops are often overreactions
    to noise (a large sell, a market-maker pulling quotes).  The
    overreaction reverts as liquidity returns.  Buy the coin that
    dropped most.

    Params: lookback=20s, min_drop=0.05, window=60s.
    """
    signals = []
    for ci, (cycle, oc) in enumerate(zip(cycles, outcomes)):
        t_arr = cycle["seconds_elapsed"].values
        if len(t_arr) < lookback + 10:
            continue
        open_asks = {c: cycle.iloc[0][f"{c.lower()}_up_ask"] for c in COINS}
        lb_idx = np.searchsorted(t_arr, lookback)
        if lb_idx >= len(cycle):
            continue
        t = int(t_arr[lb_idx])
        if t > max_t:
            continue
        row = cycle.iloc[lb_idx]
        # Coin with biggest DROP (most negative move = biggest drop)
        best_c, best_drop = None, 0.0
        for c in COINS:
            current = row[f"{c.lower()}_up_ask"]
            drop = open_asks[c] - current  # positive = price fell
            if drop > best_drop and 0.10 <= current <= 0.90:
                best_c, best_drop = c, drop
        if best_c and best_drop >= min_drop:
            signals.append(_build_signal(
                ci, t, best_c, row[f"{best_c.lower()}_up_ask"],
                oc.get(best_c), cycle
            ))
    return signals


def H5_leadlag(cycles, outcomes, leader_window=15, min_leader_move=0.05):
    """BTC lead-lag: when BTC moves, buy lagging altcoins.

    WHY: BTC is the dominant crypto asset.  Information arrives at BTC
    first (highest liquidity, most watched).  Altcoins follow with a
    delay in these 5-min markets.  Buy the altcoin that moved LEAST
    in BTC's direction.

    Params: leader_window=15s, min_leader_move=0.05.
    Signal fires at t=leader_window.
    """
    alts = [c for c in COINS if c != "BTC"]
    signals = []
    for ci, (cycle, oc) in enumerate(zip(cycles, outcomes)):
        t_arr = cycle["seconds_elapsed"].values
        if len(t_arr) < leader_window + 10:
            continue
        open_asks = {c: cycle.iloc[0][f"{c.lower()}_up_ask"] for c in COINS}
        lb_idx = np.searchsorted(t_arr, leader_window)
        if lb_idx >= len(cycle):
            continue
        t = int(t_arr[lb_idx])
        row = cycle.iloc[lb_idx]
        btc_move = row["btc_up_ask"] - open_asks["BTC"]
        if abs(btc_move) < min_leader_move:
            continue
        # Find altcoin that moved LEAST in BTC's direction
        best_c, best_lag = None, float("inf")
        for c in alts:
            current = row[f"{c.lower()}_up_ask"]
            if not (0.10 <= current <= 0.90):
                continue
            move = current - open_asks[c]
            if btc_move > 0:
                # BTC went UP.  Find alt that went up LEAST (lagging).
                lag = move  # smaller = more lag
            else:
                # BTC went DOWN.  For DOWN bets we'd buy DOWN shares,
                # but we only support UP.  Skip DOWN signals.
                continue
            if lag < best_lag:
                best_c, best_lag = c, lag
        if best_c:
            signals.append(_build_signal(
                ci, t, best_c, row[f"{best_c.lower()}_up_ask"],
                oc.get(best_c), cycle
            ))
    return signals


def H6_bid_momentum(cycles, outcomes, lookback=10, min_rise=0.03, max_t=90):
    """Bid-side momentum (order flow proxy).

    WHY: Rising bids = active buying pressure.  In a thin 5-min market,
    a bid increase of >3 cents in 10 seconds signals genuine demand,
    not just quote noise.  This predicts continued upward movement.

    Params: lookback=10s, min_rise=0.03, window=90s.
    """
    signals = []
    for ci, (cycle, oc) in enumerate(zip(cycles, outcomes)):
        t_arr = cycle["seconds_elapsed"].values
        traded = False
        for idx in range(len(cycle)):
            if traded:
                break
            t = int(t_arr[idx])
            if t < lookback or t > max_t:
                continue
            row = cycle.iloc[idx]
            # Find lookback row
            lb_idx = np.searchsorted(t_arr, t - lookback)
            if lb_idx >= len(cycle):
                continue
            lb_row = cycle.iloc[lb_idx]
            best_c, best_rise = None, 0.0
            for c in COINS:
                bid_now = row[f"{c.lower()}_up_bid"]
                bid_prev = lb_row[f"{c.lower()}_up_bid"]
                rise = bid_now - bid_prev
                if rise > best_rise and 0.10 <= row[f"{c.lower()}_up_ask"] <= 0.90:
                    best_c, best_rise = c, rise
            if best_c and best_rise >= min_rise:
                signals.append(_build_signal(
                    ci, t, best_c, row[f"{best_c.lower()}_up_ask"],
                    oc.get(best_c), cycle
                ))
                traded = True
    return signals


def H7_autocorrelation(cycles, outcomes, max_t=10):
    """Previous-cycle outcome continuation.

    WHY: Crypto markets exhibit short-term autocorrelation.  If a coin
    went UP in the previous 5-min cycle, there's residual momentum
    into the next cycle (trending markets, herding behavior).  Buy
    UP for coins that won in the previous cycle.

    Params: entry at t=5s (first reliable price), window=10s.
    """
    signals = []
    for ci in range(1, len(cycles)):
        prev_oc = outcomes[ci - 1]
        cycle = cycles[ci]
        oc = outcomes[ci]
        t_arr = cycle["seconds_elapsed"].values
        if len(t_arr) < 10:
            continue
        # Enter at first available time
        t_idx = np.searchsorted(t_arr, 5)
        if t_idx >= len(cycle):
            continue
        t = int(t_arr[t_idx])
        if t > max_t:
            continue
        row = cycle.iloc[t_idx]
        # Find coins that went UP in previous cycle
        up_coins = [c for c in COINS if prev_oc.get(c) == "UP"]
        if not up_coins:
            continue
        # Buy the cheapest among them
        best_c, best_ask = None, 1.0
        for c in up_coins:
            a = row[f"{c.lower()}_up_ask"]
            if 0.10 <= a <= 0.90 and a < best_ask:
                best_c, best_ask = c, a
        if best_c:
            signals.append(_build_signal(
                ci, t, best_c, best_ask, oc.get(best_c), cycle
            ))
    return signals


# ── Base rate analysis ──────────────────────────────────────────

def compute_base_rates(cycles, outcomes):
    """Compute fundamental market statistics."""
    stats = {}
    # Per-coin UP win rate
    for c in COINS:
        wins = sum(1 for oc in outcomes if oc.get(c) == "UP")
        resolved = sum(1 for oc in outcomes if oc.get(c) in ("UP", "DOWN"))
        stats[f"{c}_up_rate"] = wins / resolved if resolved > 0 else 0.5
        stats[f"{c}_resolved"] = resolved

    # Cheap shares: when UP ask < 0.25, how often does UP win?
    cheap_wins, cheap_total = 0, 0
    for cycle, oc in zip(cycles, outcomes):
        for c in COINS:
            early = cycle[cycle["seconds_elapsed"] <= 120]
            if len(early) == 0:
                continue
            min_ask = early[f"{c.lower()}_up_ask"].min()
            if min_ask < 0.25 and min_ask >= 0.10:
                cheap_total += 1
                if oc.get(c) == "UP":
                    cheap_wins += 1
    stats["cheap_up_rate"] = cheap_wins / cheap_total if cheap_total > 0 else 0
    stats["cheap_n"] = cheap_total

    # Avg UP ask at t=10
    avg_asks = {c: [] for c in COINS}
    for cycle in cycles:
        t_arr = cycle["seconds_elapsed"].values
        idx = np.searchsorted(t_arr, 10)
        if idx < len(cycle):
            row = cycle.iloc[idx]
            for c in COINS:
                avg_asks[c].append(row[f"{c.lower()}_up_ask"])
    for c in COINS:
        stats[f"{c}_avg_ask_t10"] = np.mean(avg_asks[c]) if avg_asks[c] else 0.5

    # Cross-coin correlation of outcomes
    outcomes_matrix = []
    for oc in outcomes:
        row = [1 if oc.get(c) == "UP" else (0 if oc.get(c) == "DOWN" else np.nan)
               for c in COINS]
        outcomes_matrix.append(row)
    df = pd.DataFrame(outcomes_matrix, columns=COINS).dropna()
    if len(df) > 10:
        stats["outcome_corr"] = df.corr().values[np.triu_indices(4, k=1)].mean()
    else:
        stats["outcome_corr"] = 0.0

    return stats


# ── Main ────────────────────────────────────────────────────────

def main():
    csv_files = sorted(str(f) for f in Path("data").glob("prices_*.csv"))
    if not csv_files:
        print("No data files in data/"); sys.exit(1)

    print("Loading data...")
    df = load_data(csv_files)
    cycles = get_cycles(df)
    outcomes = [determine_outcomes(c) for c in cycles]
    n_resolved = sum(1 for o in outcomes if any(v is not None for v in o.values()))
    print(f"  {len(df):,} rows | {len(cycles)} cycles | {n_resolved} resolved\n")

    # ── Base rates ──
    br = compute_base_rates(cycles, outcomes)
    print("=" * 90)
    print("  MARKET BASE RATES")
    print("=" * 90)
    for c in COINS:
        r = br[f"{c}_up_rate"]
        n = br[f"{c}_resolved"]
        avg = br[f"{c}_avg_ask_t10"]
        print(f"  {c}: P(UP wins) = {r:.1%} (n={n})  |  avg ask @t=10s = {avg:.3f}")
    print(f"\n  Cheap shares (ask<0.25): P(UP wins) = {br['cheap_up_rate']:.1%}"
          f" (n={br['cheap_n']})")
    cr = br["outcome_corr"]
    print(f"  Cross-coin outcome correlation: {cr:.3f}"
          f"  ({'high' if cr > 0.3 else 'moderate' if cr > 0.15 else 'low'})")

    # ── Train/test split (60/40 chronological) ──
    n_train = int(len(cycles) * 0.60)
    c_train, o_train = cycles[:n_train], outcomes[:n_train]
    c_test, o_test = cycles[n_train:], outcomes[n_train:]
    print(f"\n  Train: {n_train} cycles | Test: {len(cycles) - n_train} cycles")

    # ── Define strategies ──
    # Each: (name, hypothesis, signal_func, tp, timeout)
    strategies = [
        ("H1: Stat-arb divergence",
         "Cross-sectional mean reversion (spread>=0.15, w=60s)",
         lambda c, o: H1_statarb(c, o, spread=0.15, max_t=60),
         0.15, 20),

        ("H2: Cheap shares (expire)",
         "Payout asymmetry: buy UP<0.25, hold to resolution",
         lambda c, o: H2_cheap_asymmetry(c, o, max_price=0.25, max_t=120),
         1.0, 300),  # tp=1.0 = never fires, timeout=300 = expire

        ("H3: Early momentum",
         "Continuation: buy coin with strongest UP move in first 20s",
         lambda c, o: H3_momentum(c, o, lookback=20, min_move=0.05, max_t=60),
         0.10, 30),

        ("H4: Early dip reversal",
         "Mean reversion: buy coin that dropped most in first 20s",
         lambda c, o: H4_reversal(c, o, lookback=20, min_drop=0.05, max_t=60),
         0.10, 30),

        ("H5: BTC lead-lag",
         "BTC leads, altcoins follow: buy lagging alt when BTC moves",
         lambda c, o: H5_leadlag(c, o, leader_window=15, min_leader_move=0.05),
         0.10, 20),

        ("H6: Bid momentum",
         "Order flow: buy when bid rises >3c in 10s (buying pressure)",
         lambda c, o: H6_bid_momentum(c, o, lookback=10, min_rise=0.03, max_t=90),
         0.10, 20),

        ("H7: Prev-cycle continuation",
         "Autocorrelation: buy coins that went UP in previous cycle",
         lambda c, o: H7_autocorrelation(c, o, max_t=10),
         0.10, 30),
    ]

    n_strategies = len(strategies)
    bonferroni = ALPHA / n_strategies

    print(f"\n{'=' * 90}")
    print(f"  HYPOTHESIS-DRIVEN ANALYSIS")
    print(f"  {n_strategies} strategies | 0 grid-searched params | "
          f"Bonferroni alpha = {bonferroni:.4f}")
    print(f"  Worst-case: fee={FEE}, slippage={SLIPPAGE}, delay={ENTRY_DELAY}s")
    print(f"{'=' * 90}\n")

    # ── Run all strategies ──
    results_full = []
    results_train = []
    results_test = []

    for name, hypothesis, sig_func, tp, timeout in strategies:
        t0 = time.time()

        # Full dataset
        sigs_full = sig_func(cycles, outcomes)
        trades_full = simulate(sigs_full, tp, timeout)
        stats_full = strategy_stats(trades_full, name)

        # Train
        sigs_train = sig_func(c_train, o_train)
        trades_train = simulate(sigs_train, tp, timeout)
        stats_train = strategy_stats(trades_train, name)

        # Test
        sigs_test = sig_func(c_test, o_test)
        trades_test = simulate(sigs_test, tp, timeout)
        stats_test = strategy_stats(trades_test, name)

        elapsed = time.time() - t0

        results_full.append(stats_full)
        results_train.append(stats_train)
        results_test.append(stats_test)

        print(f"  {name}: {stats_full['n'] if stats_full else 0} trades ({elapsed:.1f}s)")

    # ── Results table: FULL DATASET ──
    print(f"\n{'=' * 90}")
    print(f"  FULL DATASET RESULTS ({len(cycles)} cycles)")
    print(f"{'=' * 90}")
    print(f"  {'#':<2} {'Strategy':<28} {'N':>4} {'Edge':>7} {'WR':>5} "
          f"{'PF':>5} {'Sharpe':>6}  {'95% CI':>16}  {'p-val':>6} {'Sig?':>5}")
    print(f"  {'-'*86}")

    for i, s in enumerate(results_full):
        if s is None:
            print(f"  {i+1:<2} {strategies[i][0]:<28}    0    N/A")
            continue
        sig = "***" if s["pval"] < bonferroni else ("*" if s["pval"] < ALPHA else "")
        print(f"  {i+1:<2} {s['label']:<28} {s['n']:>4} {s['edge']:>+.4f} "
              f"{s['wr']:>4.0%} {s['pf']:>5.2f} {s['sharpe']:>6.2f}"
              f"  [{s['ci_lo']:>+.3f},{s['ci_hi']:>+.3f}]"
              f"  {s['pval']:>.4f} {sig:>5}")

    # ── Results table: TRAIN vs TEST ──
    print(f"\n{'=' * 90}")
    print(f"  TRAIN ({n_train} cycles) vs TEST ({len(cycles)-n_train} cycles)")
    print(f"{'=' * 90}")
    print(f"  {'#':<2} {'Strategy':<28} {'Train':>30}  {'|':>1}  {'Test (OOS)':>30}")
    print(f"     {'':28} {'N':>4} {'Edge':>7} {'WR':>5} {'PF':>5}"
          f"  | {'N':>4} {'Edge':>7} {'WR':>5} {'PF':>5} {'p-val':>6}")
    print(f"  {'-'*86}")

    for i, (s_tr, s_te) in enumerate(zip(results_train, results_test)):
        name = strategies[i][0]
        if s_tr is None and s_te is None:
            print(f"  {i+1:<2} {name:<28}    0  N/A               |    0  N/A")
            continue
        tr_n = s_tr["n"] if s_tr else 0
        tr_e = s_tr["edge"] if s_tr else 0
        tr_w = s_tr["wr"] if s_tr else 0
        tr_p = s_tr["pf"] if s_tr else 0
        te_n = s_te["n"] if s_te else 0
        te_e = s_te["edge"] if s_te else 0
        te_w = s_te["wr"] if s_te else 0
        te_p = s_te["pf"] if s_te else 0
        te_pv = s_te["pval"] if s_te else 1.0
        oos = "OK" if te_e > 0 else "XX"
        print(f"  {i+1:<2} {name:<28} {tr_n:>4} {tr_e:>+.4f} {tr_w:>4.0%} {tr_p:>5.2f}"
              f"  | {te_n:>4} {te_e:>+.4f} {te_w:>4.0%} {te_p:>5.2f} {te_pv:>.4f} {oos}")

    # ── Exit breakdown ──
    print(f"\n{'=' * 90}")
    print(f"  EXIT TYPE BREAKDOWN (full dataset)")
    print(f"{'=' * 90}")
    for i, s in enumerate(results_full):
        if s is None or s["n"] == 0:
            continue
        n = s["n"]
        print(f"  {strategies[i][0]:<28}  TP={s['tp']:>3}/{n}"
              f"  TO={s['to']:>3}/{n}  EX={s['ex']:>3}/{n}"
              f"  avg_hold={s['avg_hold']:.0f}s")

    # ── Verdict ──
    print(f"\n{'=' * 90}")
    print(f"  VERDICT")
    print(f"{'=' * 90}")

    sig_strategies = [
        (strategies[i][0], s)
        for i, s in enumerate(results_full)
        if s and s["pval"] < bonferroni and s["n"] >= 10
    ]

    if sig_strategies:
        print(f"\n  SIGNIFICANT strategies (p < {bonferroni:.4f}, Bonferroni-corrected):\n")
        for name, s in sig_strategies:
            print(f"    {name}")
            print(f"      Edge: {s['edge']:+.4f}/trade  (${s['edge']*BET:+.2f} per ${BET} bet)")
            print(f"      N={s['n']}  WR={s['wr']:.0%}  PF={s['pf']:.2f}  Sharpe={s['sharpe']:.2f}")
            print(f"      95% CI: [{s['ci_lo']:+.4f}, {s['ci_hi']:+.4f}]")
            print(f"      p-value: {s['pval']:.4f}")
            # Check OOS
            idx = next(j for j, (n, _) in enumerate(strategies) if n == name)
            te = results_test[idx]
            if te and te["edge"] > 0:
                print(f"      OOS: CONFIRMED (edge={te['edge']:+.4f}, N={te['n']})")
            else:
                print(f"      OOS: FAILED (edge={te['edge']:+.4f})" if te else "      OOS: NO DATA")
    else:
        print(f"\n  NO strategy reached Bonferroni significance (p < {bonferroni:.4f}).")
        print(f"  This means either:")
        print(f"    1. There is no exploitable edge in this market with these strategies")
        print(f"    2. The sample size ({len(cycles)} cycles) is too small to detect a thin edge")
        print(f"    3. The edge exists but is smaller than our worst-case fees can capture")

    # Best candidate (lowest p-value, even if not significant)
    valid = [(i, s) for i, s in enumerate(results_full) if s and s["n"] >= 5]
    if valid:
        best_i, best_s = min(valid, key=lambda x: x[1]["pval"])
        print(f"\n  Closest to significance:")
        print(f"    {best_s['label']}: p={best_s['pval']:.4f}, edge={best_s['edge']:+.4f}, N={best_s['n']}")
        if best_s["pval"] < ALPHA:
            print(f"    (significant at uncorrected alpha=0.05, but NOT after Bonferroni)")

    print(f"\n{'=' * 90}")


if __name__ == "__main__":
    main()
