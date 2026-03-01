"""Stress-test v2 — Real Polymarket mechanics
=============================================

Models actual trading costs instead of abstract slippage/fee parameters:

  1. BUY:  order N shares at ask, pay N × ask, receive N × (1 - TAKER_FEE)
  2. SELL: at bid, no additional fee (you sell your actual shares)
  3. Spread: bid = ask − 0.02 (already in historical data)
  4. Claim at expiry: FREE ($1/share if win, $0 if lose)
  5. Placing / cancelling orders: FREE

Latency scenarios model ADDITIONAL slippage from ~340ms order flight time
on top of the fixed mechanical costs above.

Usage:
    python strategies/stress_test_v2.py
"""

import sys
from pathlib import Path
from typing import List
import numpy as np
import pandas as pd

COINS = ["BTC", "ETH", "SOL", "XRP"]
UP_ASK = [f"{c.lower()}_up_ask" for c in COINS]
UP_BID = [f"{c.lower()}_up_bid" for c in COINS]

# ── Real Polymarket mechanics ───────────────────────────────
TAKER_FEE = 0.02            # 2% — order 5, receive 4.90
SIZE_ORDERED = 5             # shares we order per trade
SIZE_RECEIVED = SIZE_ORDERED * (1 - TAKER_FEE)  # 4.90


# ── Data loading (shared with stress_test.py) ───────────────
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
                if ask < 0.02 or ask > 0.98:
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


# ── Core simulator with real mechanics ──────────────────────
def simulate_real(signals, tp, timeout, entry_lat=0.0, exit_lat=0.0):
    """Simulate with real Polymarket cost structure.

    Fixed costs (every trade):
      - 2% taker fee on buy:  pay for SIZE_ORDERED, receive SIZE_RECEIVED
      - 2c bid-ask spread:    already in the data (bid = ask - 0.02)
      - No sell fee, no claim fee

    Variable costs (latency):
      - entry_lat:  ask moved up by this during 340ms flight (worse buy fill)
      - exit_lat:   bid moved down by this during 340ms flight (worse sell fill)
    """
    trades = []
    for sig in signals:
        # Entry: fill at ask + latency
        fill_price = sig["entry_ask"] + entry_lat
        cost = SIZE_ORDERED * fill_price     # USDC paid
        shares = SIZE_RECEIVED               # shares received (after 2% fee)

        ft, fb = sig["future_t"], sig["future_bid"]
        max_t = min(sig["t"] + timeout, 300)

        # TP fires when bid >= fill_price + tp
        # (TP is based on fill_price, as in the live bot)
        tp_target = fill_price + tp
        tp_mask = (fb >= tp_target) & (ft <= max_t)
        tp_idx = np.argmax(tp_mask) if tp_mask.any() else -1

        if tp_idx >= 0 and tp_mask[tp_idx]:
            # Sell at bid - exit latency (no sell fee)
            exit_bid = max(float(fb[tp_idx]) - exit_lat, 0.01)
            revenue = shares * exit_bid
            pnl = revenue - cost
            hold = int(ft[tp_idx]) - sig["t"]
            etype = "TP"
        else:
            # Timeout: sell at last bid before max_t
            to_mask = ft <= max_t
            if to_mask.any():
                last_i = np.where(to_mask)[0][-1]
                exit_bid = max(float(fb[last_i]) - exit_lat, 0.01)
                revenue = shares * exit_bid
                pnl = revenue - cost
                hold = int(ft[last_i]) - sig["t"]
                etype = "TO"
            elif sig["outcome"] == "UP":
                # Held to expiry — claim is FREE
                revenue = shares * 1.0
                pnl = revenue - cost
                hold = 300 - sig["t"]
                etype = "EX_W"
            elif sig["outcome"] == "DOWN":
                revenue = 0.0
                pnl = -cost
                hold = 300 - sig["t"]
                etype = "EX_L"
            else:
                continue

        trades.append({
            "pnl": pnl, "hold": hold, "exit": etype,
            "ci": sig["ci"], "cost": cost,
            "entry_ask": sig["entry_ask"], "coin": sig["coin"],
        })
    return trades


# ── Statistics ──────────────────────────────────────────────
def bootstrap_ci(vals, n_boot=10000, ci=0.95):
    arr = np.array(vals)
    n = len(arr)
    means = np.array([np.mean(np.random.choice(arr, n, replace=True)) for _ in range(n_boot)])
    lo = np.percentile(means, (1 - ci) / 2 * 100)
    hi = np.percentile(means, (1 + ci) / 2 * 100)
    return float(lo), float(hi)


def permutation_test(vals, n_perm=10000):
    arr = np.array(vals)
    obs_mean = np.mean(arr)
    count = sum(1 for _ in range(n_perm)
                if np.mean(arr * np.random.choice([-1, 1], len(arr))) >= obs_mean)
    return count / n_perm


# ── Main ────────────────────────────────────────────────────
def main():
    csv_files = sorted(str(f) for f in Path("data").glob("prices_*.csv"))
    if not csv_files:
        print("No CSV files in data/")
        sys.exit(1)

    df = load_data(csv_files)
    cycles = get_cycles(df)
    outcomes = [determine_outcomes(c) for c in cycles]

    print(f"Loaded {len(cycles)} cycles from {len(csv_files)} files")
    print(f"Real mechanics: order {SIZE_ORDERED} shares -> receive {SIZE_RECEIVED:.2f}"
          f" ({TAKER_FEE:.0%} taker fee)")
    print(f"Sell fee: 0%  |  Claim: free  |  Spread: in data (bid=ask-0.02)\n")

    # ── Strategies to test ───────────────────────────────────
    strategies = [
        {"name": "Sniper",  "sp": 0.12, "w": 30, "tp": 0.06, "to": 60},
        {"name": "Robust",  "sp": 0.12, "w": 30, "tp": 0.06, "to": 90},
        {"name": "Safe",    "sp": 0.12, "w": 30, "tp": 0.05, "to": 90},
        {"name": "Micro",   "sp": 0.12, "w": 30, "tp": 0.04, "to": 90},
        {"name": "Volume",  "sp": 0.10, "w": 90, "tp": 0.15, "to": 60},
        {"name": "MidVol",  "sp": 0.10, "w": 30, "tp": 0.06, "to": 90},
        {"name": "Wide",    "sp": 0.10, "w": 30, "tp": 0.06, "to": 60},
    ]

    # ── Latency scenarios (ADDITIONAL to mechanical costs) ───
    # Entry lat: ask moved up by X during 340ms flight -> worse buy fill
    # Exit lat:  bid moved down by X during 340ms -> worse sell fill
    scenarios = [
        ("Mechanical only",   0.00, 0.00),   # just 2% fee + 2c spread
        ("Light (0+1c)",      0.00, 0.01),   # 1c exit latency
        ("Moderate (1c+1c)",  0.01, 0.01),   # 1c each side
        ("Heavy (1c+2c)",     0.01, 0.02),   # 1c entry + 2c exit
        ("Extreme (2c+2c)",   0.02, 0.02),   # 2c each side
    ]

    np.random.seed(42)

    # Store results for final comparison
    summary = []

    for strat in strategies:
        sigs = find_signals(cycles, outcomes, strat["sp"], strat["w"])
        n_sigs = len(sigs)

        print(f"\n{'='*95}")
        print(f"  {strat['name'].upper()}: spread={strat['sp']} window={strat['w']}s"
              f" tp={strat['tp']} timeout={strat['to']}s")
        print(f"  Signals: {n_sigs} in {len(cycles)} cycles"
              f" ({n_sigs/len(cycles)*100:.0f}% hit rate)")
        print(f"{'='*95}")

        # ── Scenario table ──────────────────────────────────
        hdr = (f"  {'Scenario':<22} {'N':>3} {'WR':>5} {'AvgPnL':>8} {'$100->':>8}"
               f" {'PF':>6} {'TP%':>4}  {'95% CI':>22}  {'p-val':>7}")
        sep = (f"  {'-'*22} {'-'*3} {'-'*5} {'-'*8} {'-'*8}"
               f" {'-'*6} {'-'*4}  {'-'*22}  {'-'*7}")
        print(f"\n{hdr}\n{sep}")

        for label, e_lat, x_lat in scenarios:
            trades = simulate_real(sigs, strat["tp"], strat["to"], e_lat, x_lat)
            if len(trades) < 3:
                print(f"  {label:<22} <3 trades")
                continue

            pnls = [t["pnl"] for t in trades]
            n = len(trades)
            wr = sum(1 for p in pnls if p > 0) / n
            avg = np.mean(pnls)
            tp_pct = sum(1 for t in trades if t["exit"] == "TP") / n

            # Bankroll (pnl is already per-trade for SIZE_ORDERED shares)
            bal = 100.0 + sum(pnls)
            gw = sum(t["pnl"] for t in trades if t["pnl"] > 0)
            gl = sum(abs(t["pnl"]) for t in trades if t["pnl"] < 0)
            pf = gw / gl if gl > 0 else float("inf")

            lo, hi = bootstrap_ci(pnls)
            ci_str = f"[{lo:+.3f}, {hi:+.3f}]"
            ci_excl = lo > 0
            p_val = permutation_test(pnls)

            tag = " ***" if ci_excl else ("  OK" if avg > 0 else " NEG")
            print(f"  {label:<22} {n:>3} {wr:>4.0%} {avg:>+7.3f} ${bal:>7.2f}"
                  f" {pf:>6.2f} {tp_pct:>3.0%}  {ci_str:>22}  {p_val:>6.4f}{tag}")

            # Save "Moderate" results for comparison
            if "Moderate" in label:
                summary.append({
                    "name": strat["name"],
                    "params": f"sp={strat['sp']} w={strat['w']} tp={strat['tp']} to={strat['to']}",
                    "n": n, "wr": wr, "avg_pnl": avg, "bal": bal,
                    "pf": pf, "tp_pct": tp_pct,
                    "ci_lo": lo, "ci_hi": hi, "p_val": p_val,
                })

        # ── Exit breakdown (Moderate latency) ───────────────
        trades = simulate_real(sigs, strat["tp"], strat["to"], 0.01, 0.01)
        if len(trades) >= 3:
            tp_trades = [t for t in trades if t["exit"] == "TP"]
            to_trades = [t for t in trades if t["exit"] == "TO"]
            ex_w = [t for t in trades if t["exit"] == "EX_W"]
            ex_l = [t for t in trades if t["exit"] == "EX_L"]

            print(f"\n  Exit breakdown (Moderate latency: +1c/+1c):")
            if tp_trades:
                tp_avg = np.mean([t["pnl"] for t in tp_trades])
                tp_cost = np.mean([t["cost"] for t in tp_trades])
                print(f"    TP:       {len(tp_trades):>3} trades"
                      f"  avg PnL ${tp_avg:+.3f}  avg cost ${tp_cost:.2f}")
            if to_trades:
                to_avg = np.mean([t["pnl"] for t in to_trades])
                to_cost = np.mean([t["cost"] for t in to_trades])
                print(f"    Timeout:  {len(to_trades):>3} trades"
                      f"  avg PnL ${to_avg:+.3f}  avg cost ${to_cost:.2f}")
            if ex_w:
                ew_avg = np.mean([t["pnl"] for t in ex_w])
                print(f"    Exp WIN:  {len(ex_w):>3} trades"
                      f"  avg PnL ${ew_avg:+.3f}")
            if ex_l:
                el_avg = np.mean([t["pnl"] for t in ex_l])
                print(f"    Exp LOSE: {len(ex_l):>3} trades"
                      f"  avg PnL ${el_avg:+.3f}")

            # Avg entry ask
            avg_ask = np.mean([t["entry_ask"] for t in trades])
            print(f"    Avg entry ask: ${avg_ask:.3f}"
                  f"  |  Mechanical cost/trade: ${SIZE_ORDERED * avg_ask * TAKER_FEE:.3f}"
                  f" (fee) + ${SIZE_RECEIVED * 0.02:.3f} (spread)"
                  f" = ${SIZE_ORDERED * avg_ask * TAKER_FEE + SIZE_RECEIVED * 0.02:.3f}")

        # ── Fill rate sensitivity (Moderate latency) ────────
        print(f"\n  Fill rate sensitivity (Moderate latency: +1c/+1c):")
        for fill_pct in [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4]:
            n_runs = 500
            finals = []
            for _ in range(n_runs):
                if fill_pct < 1.0:
                    mask = np.random.random(len(sigs)) < fill_pct
                    subset = [s for s, m in zip(sigs, mask) if m]
                else:
                    subset = sigs
                trades = simulate_real(subset, strat["tp"], strat["to"], 0.01, 0.01)
                bal = 100.0 + sum(t["pnl"] for t in trades)
                finals.append(bal)

            avg_f = np.mean(finals)
            lo_f = np.percentile(finals, 5)
            hi_f = np.percentile(finals, 95)
            pct_profit = sum(1 for f in finals if f > 100) / n_runs * 100
            print(f"    {fill_pct:>3.0%} fill: avg=${avg_f:.2f}"
                  f" [${lo_f:.2f}-${hi_f:.2f}]"
                  f"  {pct_profit:.0f}% profitable"
                  f"  ~{fill_pct * n_sigs:.0f} trades")

        # ── Breakeven latency ───────────────────────────────
        print(f"\n  Breakeven: at what total latency does edge vanish?")
        print(f"  (This is ON TOP of the 2% fee + 2c spread)")
        for total_lat in np.arange(0.00, 0.15, 0.005):
            # Split: 40% entry, 60% exit (exit latency usually worse)
            e = round(total_lat * 0.4, 4)
            x = round(total_lat * 0.6, 4)
            trades = simulate_real(sigs, strat["tp"], strat["to"], e, x)
            if len(trades) < 3:
                continue
            avg = np.mean([t["pnl"] for t in trades])
            if avg <= 0:
                print(f"    Edge vanishes at total latency = {total_lat:.3f}"
                      f" (entry={e:.3f} exit={x:.3f})")
                break
        else:
            print(f"    Edge survives up to 0.15 total latency!")

        print()

    # ── Strategy comparison ─────────────────────────────────
    print(f"\n{'='*95}")
    print(f"  STRATEGY COMPARISON (Moderate latency: +1c entry / +1c exit)")
    print(f"  Fixed costs included: 2% taker fee + 2c spread per trade")
    print(f"{'='*95}")
    print(f"\n  {'Name':<10} {'Params':<32} {'N':>3} {'WR':>5} {'AvgPnL':>8}"
          f" {'$100->':>8} {'PF':>6} {'TP%':>4} {'p-val':>7} {'Signal':>8}")
    print(f"  {'-'*10} {'-'*32} {'-'*3} {'-'*5} {'-'*8}"
          f" {'-'*8} {'-'*6} {'-'*4} {'-'*7} {'-'*8}")

    for s in summary:
        signal = "***" if s["ci_lo"] > 0 else ("OK" if s["avg_pnl"] > 0 else "NEG")
        print(f"  {s['name']:<10} {s['params']:<32} {s['n']:>3} {s['wr']:>4.0%}"
              f" {s['avg_pnl']:>+7.3f} ${s['bal']:>7.2f} {s['pf']:>6.2f}"
              f" {s['tp_pct']:>3.0%} {s['p_val']:>6.4f} {signal:>8}")

    # ── Recommendation ──────────────────────────────────────
    profitable = [s for s in summary if s["avg_pnl"] > 0]
    profitable.sort(key=lambda x: x["avg_pnl"] * x["n"], reverse=True)

    sig_sorted = sorted(profitable, key=lambda x: x["p_val"])

    print(f"\n  {'='*60}")
    print(f"  RECOMMENDATION")
    print(f"  {'='*60}")

    if profitable:
        best = profitable[0]
        print(f"\n  Best total profit: {best['name']}")
        print(f"    {best['params']}")
        print(f"    {best['n']} trades x ${best['avg_pnl']:+.3f}/trade"
              f" = ${best['avg_pnl']*best['n']:+.2f} total")
        print(f"    p-value = {best['p_val']:.4f}"
              f"  CI = [{best['ci_lo']:+.3f}, {best['ci_hi']:+.3f}]")

        if sig_sorted and sig_sorted[0]["name"] != best["name"]:
            alt = sig_sorted[0]
            print(f"\n  Most significant:  {alt['name']}")
            print(f"    {alt['params']}")
            print(f"    {alt['n']} trades x ${alt['avg_pnl']:+.3f}/trade"
                  f" = ${alt['avg_pnl']*alt['n']:+.2f} total")
            print(f"    p-value = {alt['p_val']:.4f}"
                  f"  CI = [{alt['ci_lo']:+.3f}, {alt['ci_hi']:+.3f}]")
    else:
        print(f"\n  No profitable strategies under Moderate latency!")

    print(f"""
  COST MODEL:
    Every trade pays (fixed):
      {TAKER_FEE:.0%} taker fee on buy:  order {SIZE_ORDERED} -> receive {SIZE_RECEIVED:.2f} shares
      2c spread:             bid = ask - 0.02 (in data)
      Combined at ask=0.50:  $0.05 fee + $0.098 spread = $0.148/trade

    Latency (variable, 0-2c per side):
      Entry: FOK may fill at ask+X (340ms flight, ask moved)
      Exit:  sell may fill at bid-X (340ms flight, bid moved)
      Miss:  FOK may not fill at all (modeled via fill rate)

    No sell fee  |  Claim at expiry: FREE
""")


if __name__ == "__main__":
    main()
