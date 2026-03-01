"""Stress test: how much real-world friction can the top strategies survive?

Tests:
  1. Increasing fee/slippage levels (optimistic -> worst case)
  2. Bootstrap 95% CI for mean PnL per trade
  3. Fill rate sensitivity (FOK misses)
  4. Breakeven penalty analysis (at what cost does the edge vanish?)

Usage:
    python strategies/stress_test.py
"""

import sys
from pathlib import Path
from typing import Dict, List, Optional
import numpy as np
import pandas as pd

COINS = ["BTC", "ETH", "SOL", "XRP"]
UP_ASK = [f"{c.lower()}_up_ask" for c in COINS]
UP_BID = [f"{c.lower()}_up_bid" for c in COINS]


def load_data(paths):
    frames = [pd.read_csv(p, parse_dates=["timestamp", "cycle_start"]) for p in paths]
    df = pd.concat(frames, ignore_index=True).sort_values("timestamp").reset_index(drop=True)
    df["gm_up_ask"] = df[UP_ASK].mean(axis=1)
    return df


def get_cycles(df, min_rows=20):
    return [g.reset_index(drop=True) for _, g in df.groupby("cycle_start") if len(g) >= min_rows]


def determine_outcomes(cycle):
    late = cycle[cycle["seconds_elapsed"] >= 280]
    if len(late) < 3:
        return {c: None for c in COINS}
    out = {}
    for c in COINS:
        avg = late[f"{c.lower()}_up_ask"].mean()
        out[c] = "UP" if avg >= 0.70 else ("DOWN" if avg <= 0.30 else None)
    return out


def find_signals(cycles, outcomes, min_spread, max_entry_t):
    signals = []
    for ci, (cycle, oc) in enumerate(zip(cycles, outcomes)):
        early = cycle[(cycle["early"] == True) & (cycle["seconds_elapsed"] <= max_entry_t)]
        t_arr = cycle["seconds_elapsed"].values
        bid_arrays = {c: cycle[f"{c.lower()}_up_bid"].values for c in COINS}
        traded = False
        for _, row in early.iterrows():
            if traded:
                break
            t = int(row["seconds_elapsed"])
            gm = row["gm_up_ask"]
            best_c, best_d = None, 0.0
            for c in COINS:
                ask = row[f"{c.lower()}_up_ask"]
                if ask < 0.10 or ask > 0.90:
                    continue
                d = gm - ask
                if d > best_d:
                    best_c, best_d = c, d
            if best_c is None or best_d < min_spread:
                continue
            entry_ask = row[f"{best_c.lower()}_up_ask"]
            mask = t_arr > t
            signals.append({
                "ci": ci, "t": t, "coin": best_c, "dev": best_d,
                "entry_ask": entry_ask, "outcome": oc.get(best_c),
                "future_t": t_arr[mask],
                "future_bid": bid_arrays[best_c][mask],
            })
            traded = True
    return signals


def simulate_with_penalties(signals, tp, timeout, entry_slip, exit_slip, fee_rt):
    """Simulate with explicit entry/exit slippage and round-trip fee."""
    trades = []
    for sig in signals:
        raw_entry = sig["entry_ask"]
        entry = raw_entry + entry_slip  # worse fill on buy
        ft, fb = sig["future_t"], sig["future_bid"]
        max_t = min(sig["t"] + timeout, 300)

        # TP: bid must reach entry + tp (raw entry, not penalized)
        # But when we sell, we get bid - exit_slip
        tp_price = raw_entry + tp
        tp_mask = (fb >= tp_price) & (ft <= max_t)
        tp_idx = np.argmax(tp_mask) if tp_mask.any() else -1

        if tp_idx >= 0 and tp_mask[tp_idx]:
            exit_price = float(fb[tp_idx]) - exit_slip
            pnl = exit_price - entry - fee_rt
            hold = int(ft[tp_idx]) - sig["t"]
            etype = "TP"
        else:
            to_mask = ft <= max_t
            if to_mask.any():
                last_i = np.where(to_mask)[0][-1]
                exit_price = float(fb[last_i]) - exit_slip
                pnl = exit_price - entry - fee_rt
                hold = int(ft[last_i]) - sig["t"]
                etype = "TO"
            elif sig["outcome"] == "UP":
                pnl = 1.0 - entry - fee_rt
                hold, etype = 300 - sig["t"], "EX"
            elif sig["outcome"] == "DOWN":
                pnl = 0.0 - entry - fee_rt
                hold, etype = 300 - sig["t"], "EX"
            else:
                continue

        trades.append({"pnl": pnl, "hold": hold, "exit": etype, "ci": sig["ci"]})
    return trades


def bootstrap_ci(pnls, n_boot=10000, ci=0.95):
    """Bootstrap confidence interval for mean PnL."""
    arr = np.array(pnls)
    n = len(arr)
    means = np.array([np.mean(np.random.choice(arr, n, replace=True)) for _ in range(n_boot)])
    lo = np.percentile(means, (1 - ci) / 2 * 100)
    hi = np.percentile(means, (1 + ci) / 2 * 100)
    return float(lo), float(hi)


def permutation_test(pnls, n_perm=10000):
    """Permutation test: what's the probability of observing this mean by chance?"""
    arr = np.array(pnls)
    obs_mean = np.mean(arr)
    count = 0
    for _ in range(n_perm):
        signs = np.random.choice([-1, 1], len(arr))
        if np.mean(arr * signs) >= obs_mean:
            count += 1
    return count / n_perm


def main():
    csv_files = sorted(str(f) for f in Path("data").glob("prices_*.csv"))
    if not csv_files:
        print("No data"); sys.exit(1)

    df = load_data(csv_files)
    cycles = get_cycles(df)
    outcomes = [determine_outcomes(c) for c in cycles]
    print(f"{len(cycles)} cycles loaded\n")

    # ── Strategies to stress-test ────────────────────────────
    strategies = [
        {"name": "Sniper",  "sp": 0.12, "w": 30, "tp": 0.06, "to": 60},
        {"name": "Robust",  "sp": 0.12, "w": 30, "tp": 0.06, "to": 90},
        {"name": "Safe",    "sp": 0.12, "w": 30, "tp": 0.05, "to": 90},
        {"name": "Micro",   "sp": 0.12, "w": 30, "tp": 0.04, "to": 90},
        {"name": "Volume",  "sp": 0.10, "w": 90, "tp": 0.15, "to": 60},
        {"name": "MidVol",  "sp": 0.10, "w": 30, "tp": 0.06, "to": 90},
        {"name": "Wide",    "sp": 0.10, "w": 30, "tp": 0.06, "to": 60},
    ]

    # ── Penalty scenarios ────────────────────────────────────
    # Each: (label, entry_slip, exit_slip, fee_round_trip)
    #
    # Research baseline: entry at ask, exit at bid, fee=0.01/$ RT
    # = 1 cent fee per dollar traded round-trip
    #
    # Real-world costs from Ireland server:
    #   Entry: FOK at ask+0.03 slippage. Fills at ask or ask+0.01 typically.
    #   Exit:  340ms latency. Bid can move 1-3c adverse in volatile 5min markets.
    #   Fees:  Polymarket taker 0.5-1.5%. For 0.50 token: 0.25c-0.75c/side.
    #          Round-trip on $1 notional: ~1-3 cents.
    #   FOK miss: ~10-20% of orders killed (ask moved during 340ms flight).
    scenarios = [
        ("Research (baseline)",  0.00, 0.00, 0.01),
        ("Conservative",         0.01, 0.01, 0.01),
        ("Realistic",            0.01, 0.02, 0.02),
        ("Pessimistic",          0.02, 0.02, 0.02),
        ("Harsh",                0.02, 0.03, 0.03),
        ("Worst case",           0.03, 0.03, 0.03),
        ("Nightmare",            0.04, 0.04, 0.04),
    ]

    np.random.seed(42)

    for strat in strategies:
        sigs = find_signals(cycles, outcomes, strat["sp"], strat["w"])
        n_sigs = len(sigs)

        print(f"{'='*90}")
        print(f"  {strat['name'].upper()}: spread={strat['sp']} window={strat['w']}s"
              f" tp={strat['tp']} timeout={strat['to']}s")
        print(f"  Signals found: {n_sigs} in {len(cycles)} cycles"
              f" ({n_sigs/len(cycles)*100:.0f}% cycle hit rate)")
        print(f"{'='*90}")

        print(f"\n  {'Scenario':<22} {'N':>3} {'WR':>5} {'AvgPnL':>8} {'$100->':>8}"
              f" {'PF':>6} {'TP%':>4}  {'95% CI':>20}  {'p-val':>7}")
        print(f"  {'-'*22} {'---':>3} {'-----':>5} {'--------':>8} {'--------':>8}"
              f" {'------':>6} {'----':>4}  {'-'*20}  {'-------':>7}")

        for label, e_slip, x_slip, fee in scenarios:
            trades = simulate_with_penalties(sigs, strat["tp"], strat["to"],
                                            e_slip, x_slip, fee)
            if len(trades) < 3:
                print(f"  {label:<22} {'<3 trades':>3}")
                continue

            pnls = [t["pnl"] for t in trades]
            n = len(trades)
            wr = sum(1 for p in pnls if p > 0) / n
            avg = np.mean(pnls)
            tp_pct = sum(1 for t in trades if t["exit"] == "TP") / n

            # Bankroll
            bal = 100.0
            for t in trades:
                bal += t["pnl"] * 5.0
            gw = sum(t["pnl"] * 5 for t in trades if t["pnl"] > 0)
            gl = sum(abs(t["pnl"]) * 5 for t in trades if t["pnl"] < 0)
            pf = gw / gl if gl > 0 else float("inf")

            # Bootstrap CI
            lo, hi = bootstrap_ci(pnls)
            ci_str = f"[{lo:+.4f}, {hi:+.4f}]"

            # Color-code: green if CI excludes zero, red if includes zero
            excludes_zero = lo > 0
            ci_marker = "***" if excludes_zero else ""

            # Permutation test
            p_val = permutation_test(pnls)

            profitable = avg > 0
            marker = "  OK" if profitable else " NEG"

            print(f"  {label:<22} {n:>3} {wr:>4.0%} {avg:>+7.4f} ${bal:>7.2f}"
                  f" {pf:>6.2f} {tp_pct:>3.0%}  {ci_str:>20}  {p_val:>6.4f} {ci_marker}{marker}")

        # ── Fill rate sensitivity ────────────────────────────
        print(f"\n  Fill rate sensitivity (Realistic penalties: +0.01/+0.02/0.02 RT):")
        for fill_pct in [1.0, 0.9, 0.8, 0.7, 0.6, 0.5]:
            n_runs = 200
            finals = []
            for _ in range(n_runs):
                # Randomly drop signals
                if fill_pct < 1.0:
                    mask = np.random.random(len(sigs)) < fill_pct
                    subset = [s for s, m in zip(sigs, mask) if m]
                else:
                    subset = sigs
                trades = simulate_with_penalties(subset, strat["tp"], strat["to"],
                                                0.01, 0.02, 0.02)
                pnls = [t["pnl"] for t in trades]
                bal = 100.0 + sum(p * 5.0 for p in pnls)
                finals.append(bal)

            avg_f = np.mean(finals)
            lo_f = np.percentile(finals, 5)
            hi_f = np.percentile(finals, 95)
            pct_profit = sum(1 for f in finals if f > 100) / n_runs * 100
            n_trades_avg = fill_pct * n_sigs
            print(f"    {fill_pct:>3.0%} fill: avg=${avg_f:.2f}"
                  f" [${lo_f:.2f}-${hi_f:.2f}]"
                  f"  {pct_profit:.0f}% profitable"
                  f"  ~{n_trades_avg:.0f} trades")

        # ── Breakeven analysis ───────────────────────────────
        print(f"\n  Breakeven: at what total penalty does edge = 0?")
        for total_pen in np.arange(0.00, 0.15, 0.005):
            # Split penalty: 40% entry, 30% exit, 30% fee
            e = total_pen * 0.4
            x = total_pen * 0.3
            f = total_pen * 0.3
            trades = simulate_with_penalties(sigs, strat["tp"], strat["to"], e, x, f)
            if len(trades) < 3:
                continue
            avg = np.mean([t["pnl"] for t in trades])
            if avg <= 0:
                print(f"    Edge vanishes at total penalty = {total_pen:.3f}"
                      f" (entry={e:.3f} exit={x:.3f} fee={f:.3f})")
                break
        else:
            print(f"    Edge survives up to 0.15 total penalty!")

        print()

    # ── Final summary ────────────────────────────────────────
    print(f"{'='*90}")
    print(f"  LIVE TRADING ASSESSMENT")
    print(f"{'='*90}")
    print(f"""
  REAL-WORLD COST MODEL (Ireland -> Polymarket):
    Entry slip:  +0.01 to +0.02  (FOK fills at/near ask, 340ms flight time)
    Exit slip:   +0.01 to +0.03  (bid moves during 340ms sell submission)
    Fees:        ~0.01 to 0.02/$ (0.5-1% taker per side)
    Fill rate:   ~70-90% FOK     (ask can move during 340ms, killing FOK)
    Total RT:    ~0.03 to 0.07 per dollar traded

  The 'Realistic' scenario (+0.01 entry, +0.02 exit, 0.02 fee RT)
  is the most likely real-world outcome. Any strategy that's profitable
  there and has bootstrap CI excluding zero has a genuine edge.

  Strategies where the 'Pessimistic' scenario is still profitable have
  a safety margin for bad days.
""")


if __name__ == "__main__":
    main()
