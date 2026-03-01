"""Comprehensive strategy research v2 — multi-family, anti-overfit.

Tests 3 strategy families:
  1. STAT-ARB UP:   group mean divergence on UP asks (current strategy)
  2. STAT-ARB DOWN: group mean divergence on DOWN asks (derived: 1 - UP bid)
  3. CHEAP QUOTE:   buy any coin's UP when ask < absolute threshold

155 cycles, 14-fold CV.  Strategy must profit on ALL folds.
One trade per cycle (matches live bot constraint).

Usage:
    python strategies/research_v2.py
"""

import sys, time
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
    # UP group mean
    df["gm_up_ask"] = df[UP_ASK].mean(axis=1)
    # Derive DOWN prices: DOWN ask ≈ 1 - UP bid, DOWN bid ≈ 1 - UP ask
    for c in COINS:
        cl = c.lower()
        df[f"{cl}_dn_ask"] = 1.0 - df[f"{cl}_up_bid"]
        df[f"{cl}_dn_bid"] = 1.0 - df[f"{cl}_up_ask"]
    dn_ask_cols = [f"{c.lower()}_dn_ask" for c in COINS]
    df["gm_dn_ask"] = df[dn_ask_cols].mean(axis=1)
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


# ── Signal finders ───────────────────────────────────────────

def find_signals_statarb_up(cycles, outcomes, min_spread, max_entry_t):
    """Stat-arb UP: buy cheapest UP when deviation from group mean > threshold."""
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
                "family": "UP",
            })
            traded = True
    return signals


def find_signals_statarb_dn(cycles, outcomes, min_spread, max_entry_t):
    """Stat-arb DOWN: buy cheapest DOWN when deviation from group mean > threshold."""
    signals = []
    dn_ask_cols = [f"{c.lower()}_dn_ask" for c in COINS]
    for ci, (cycle, oc) in enumerate(zip(cycles, outcomes)):
        early = cycle[(cycle["early"] == True) & (cycle["seconds_elapsed"] <= max_entry_t)]
        t_arr = cycle["seconds_elapsed"].values
        # DOWN bid = 1 - UP ask
        dn_bid_arrays = {c: (1.0 - cycle[f"{c.lower()}_up_ask"].values) for c in COINS}
        traded = False
        for _, row in early.iterrows():
            if traded:
                break
            t = int(row["seconds_elapsed"])
            gm = row["gm_dn_ask"]
            best_c, best_d = None, 0.0
            for c in COINS:
                ask = row[f"{c.lower()}_dn_ask"]
                if ask < 0.10 or ask > 0.90:
                    continue
                d = gm - ask
                if d > best_d:
                    best_c, best_d = c, d
            if best_c is None or best_d < min_spread:
                continue
            entry_ask = row[f"{best_c.lower()}_dn_ask"]
            outcome_dn = None
            oc_up = oc.get(best_c)
            if oc_up == "UP":
                outcome_dn = "DOWN_LOSS"  # DOWN token loses when UP wins
            elif oc_up == "DOWN":
                outcome_dn = "DOWN_WIN"
            mask = t_arr > t
            signals.append({
                "ci": ci, "t": t, "coin": best_c, "dev": best_d,
                "entry_ask": entry_ask, "outcome": outcome_dn,
                "future_t": t_arr[mask],
                "future_bid": dn_bid_arrays[best_c][mask],
                "family": "DN",
            })
            traded = True
    return signals


def find_signals_cheap(cycles, outcomes, max_price, max_entry_t):
    """Cheap quote: buy any coin's UP when ask < absolute threshold."""
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
            # Find cheapest coin below threshold
            best_c, best_ask = None, max_price
            for c in COINS:
                ask = row[f"{c.lower()}_up_ask"]
                if ask < 0.10:
                    continue
                if ask < best_ask:
                    best_c, best_ask = c, ask
            if best_c is None:
                continue
            mask = t_arr > t
            signals.append({
                "ci": ci, "t": t, "coin": best_c, "dev": max_price - best_ask,
                "entry_ask": best_ask, "outcome": oc.get(best_c),
                "future_t": t_arr[mask],
                "future_bid": bid_arrays[best_c][mask],
                "family": "CHEAP",
            })
            traded = True
    return signals


# ── Trade simulation ─────────────────────────────────────────

def simulate(signals, tp, timeout, fee=0.01, entry_delay=0):
    trades = []
    for sig in signals:
        entry = sig["entry_ask"]
        ft, fb = sig["future_t"], sig["future_bid"]
        max_t = min(sig["t"] + timeout, 300)

        # GTC isn't placed until entry_delay seconds after signal
        # (on-chain settlement ~3-5s). TP can only fill after that.
        tp_min_t = sig["t"] + entry_delay
        tp_mask = (fb >= entry + tp) & (ft <= max_t) & (ft >= tp_min_t)
        tp_idx = np.argmax(tp_mask) if tp_mask.any() else -1

        if tp_idx >= 0 and tp_mask[tp_idx]:
            pnl = float(fb[tp_idx]) - entry - fee
            hold = int(ft[tp_idx]) - sig["t"]
            etype = "TP"
        else:
            to_mask = ft <= max_t
            if to_mask.any():
                last_i = np.where(to_mask)[0][-1]
                pnl = float(fb[last_i]) - entry - fee
                hold = int(ft[last_i]) - sig["t"]
                etype = "TO"
            else:
                # Expiry fallback
                oc = sig["outcome"]
                if oc == "UP" or oc == "DOWN_WIN":
                    pnl = 1.0 - entry - fee
                    hold, etype = 300 - sig["t"], "EX"
                elif oc == "DOWN" or oc == "DOWN_LOSS":
                    pnl = -entry - fee
                    hold, etype = 300 - sig["t"], "EX"
                else:
                    continue
        trades.append({"pnl": pnl, "hold": hold, "exit": etype, "ci": sig["ci"]})
    return trades


def bankroll(trades, start=100.0, bet=5.0):
    bal = start
    peak = start
    max_dd = 0.0
    gw = gl = 0.0
    for t in trades:
        r = t["pnl"] * bet
        bal += r
        if r > 0: gw += r
        else: gl += abs(r)
        if bal > peak: peak = bal
        dd = (peak - bal) / peak if peak > 0 else 0
        if dd > max_dd: max_dd = dd
    return {
        "final": bal, "profit": bal - start, "max_dd": max_dd * 100,
        "pf": gw / gl if gl > 0 else float("inf"),
    }


# ── Cross-validation folds ───────────────────────────────────

def build_folds(cycles, outcomes):
    n = len(cycles)
    folds = {}

    # Day-level
    day_map = {}
    for i, c in enumerate(cycles):
        day = str(c["cycle_start"].iloc[0].date())
        day_map.setdefault(day, []).append(i)
    for day, idx in sorted(day_map.items()):
        if len(idx) >= 5:
            folds[f"d_{day[-5:]}"] = ([cycles[i] for i in idx], [outcomes[i] for i in idx])

    # Chronological halves
    mid = n // 2
    folds["h_1st"] = (cycles[:mid], outcomes[:mid])
    folds["h_2nd"] = (cycles[mid:], outcomes[mid:])

    # Thirds
    t1, t2 = n // 3, 2 * n // 3
    folds["t_1"] = (cycles[:t1], outcomes[:t1])
    folds["t_2"] = (cycles[t1:t2], outcomes[t1:t2])
    folds["t_3"] = (cycles[t2:], outcomes[t2:])

    # Quintiles
    q = n // 5
    for i in range(5):
        s, e = i * q, (i + 1) * q if i < 4 else n
        folds[f"q_{i+1}"] = (cycles[s:e], outcomes[s:e])

    # Odd/even
    folds["odd"] = ([cycles[i] for i in range(n) if i % 2 == 1],
                    [outcomes[i] for i in range(n) if i % 2 == 1])
    folds["even"] = ([cycles[i] for i in range(n) if i % 2 == 0],
                     [outcomes[i] for i in range(n) if i % 2 == 0])

    return folds


# ── Main sweep ───────────────────────────────────────────────

def main():
    FEE = 0.01
    START = 100.0
    BET = 10.0
    ENTRY_DELAY = 5  # seconds: on-chain settlement before GTC is placed

    csv_files = sorted(str(f) for f in Path("data").glob("prices_*.csv"))
    if not csv_files:
        print("No data files found"); sys.exit(1)

    print(f"Loading {len(csv_files)} file(s)...")
    df = load_data(csv_files)
    cycles = get_cycles(df)
    outcomes = [determine_outcomes(c) for c in cycles]
    n_res = sum(1 for o in outcomes if any(v is not None for v in o.values()))
    print(f"{len(df):,} rows | {len(cycles)} cycles | {n_res} resolved")
    print(f"Fee={FEE}/$ RT | ${START} start | ${BET}/trade")

    folds = build_folds(cycles, outcomes)
    fold_names = list(folds.keys())
    n_folds = len(fold_names)
    print(f"CV: {n_folds} folds: {', '.join(fold_names)}")

    # ── Parameter grids ──────────────────────────────────────
    # Strategy 1: Stat-arb UP
    up_grid = []
    for sp in [0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.12, 0.14, 0.16, 0.18, 0.20]:
        for w in [30, 45, 60, 90, 120, 150, 180]:
            for tp in [0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12, 0.15]:
                for to in [15, 20, 30, 45, 60, 90, 300]:
                    up_grid.append({"sp": sp, "w": w, "tp": tp, "to": to})

    # Strategy 2: Stat-arb DOWN
    dn_grid = []
    for sp in [0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.22, 0.25]:
        for w in [30, 45, 60, 90, 120]:
            for tp in [0.03, 0.05, 0.06, 0.08, 0.10, 0.12, 0.15]:
                for to in [15, 20, 30, 45, 60, 90]:
                    dn_grid.append({"sp": sp, "w": w, "tp": tp, "to": to})

    # Strategy 3: Cheap quote UP (absolute threshold)
    cq_grid = []
    for px in [0.15, 0.20, 0.25, 0.30, 0.35, 0.40]:
        for w in [30, 45, 60, 90, 120]:
            for tp in [0.03, 0.05, 0.08, 0.10, 0.15, 0.20]:
                for to in [15, 20, 30, 45, 60, 90]:
                    cq_grid.append({"px": px, "w": w, "tp": tp, "to": to})

    total = len(up_grid) + len(dn_grid) + len(cq_grid)
    print(f"\nGrid: {len(up_grid)} UP + {len(dn_grid)} DN + {len(cq_grid)} CHEAP = {total} combos")

    # ── Sweep function ───────────────────────────────────────
    def sweep_up(cyc, oc, grid):
        cache = {}
        results = {}
        for p in grid:
            key = (p["sp"], p["w"])
            if key not in cache:
                cache[key] = find_signals_statarb_up(cyc, oc, p["sp"], p["w"])
            sigs = cache[key]
            if not sigs:
                continue
            trades = simulate(sigs, p["tp"], p["to"], FEE, ENTRY_DELAY)
            if len(trades) < 3:
                continue
            pnls = [t["pnl"] for t in trades]
            k = f"UP s={p['sp']:.2f} w={p['w']} tp={p['tp']:.2f} to={p['to']}s"
            results[k] = {
                "trades": trades, "n": len(trades),
                "wr": sum(1 for x in pnls if x > 0) / len(trades),
                "avg_pnl": float(np.mean(pnls)),
                "tp_pct": sum(1 for t in trades if t["exit"] == "TP") / len(trades),
                "hold": float(np.mean([t["hold"] for t in trades])),
            }
        return results

    def sweep_dn(cyc, oc, grid):
        cache = {}
        results = {}
        for p in grid:
            key = (p["sp"], p["w"])
            if key not in cache:
                cache[key] = find_signals_statarb_dn(cyc, oc, p["sp"], p["w"])
            sigs = cache[key]
            if not sigs:
                continue
            trades = simulate(sigs, p["tp"], p["to"], FEE, ENTRY_DELAY)
            if len(trades) < 3:
                continue
            pnls = [t["pnl"] for t in trades]
            k = f"DN s={p['sp']:.2f} w={p['w']} tp={p['tp']:.2f} to={p['to']}s"
            results[k] = {
                "trades": trades, "n": len(trades),
                "wr": sum(1 for x in pnls if x > 0) / len(trades),
                "avg_pnl": float(np.mean(pnls)),
                "tp_pct": sum(1 for t in trades if t["exit"] == "TP") / len(trades),
                "hold": float(np.mean([t["hold"] for t in trades])),
            }
        return results

    def sweep_cheap(cyc, oc, grid):
        cache = {}
        results = {}
        for p in grid:
            key = (p["px"], p["w"])
            if key not in cache:
                cache[key] = find_signals_cheap(cyc, oc, p["px"], p["w"])
            sigs = cache[key]
            if not sigs:
                continue
            trades = simulate(sigs, p["tp"], p["to"], FEE, ENTRY_DELAY)
            if len(trades) < 3:
                continue
            pnls = [t["pnl"] for t in trades]
            k = f"CQ p={p['px']:.2f} w={p['w']} tp={p['tp']:.2f} to={p['to']}s"
            results[k] = {
                "trades": trades, "n": len(trades),
                "wr": sum(1 for x in pnls if x > 0) / len(trades),
                "avg_pnl": float(np.mean(pnls)),
                "tp_pct": sum(1 for t in trades if t["exit"] == "TP") / len(trades),
                "hold": float(np.mean([t["hold"] for t in trades])),
            }
        return results

    # ── Run all families on full data + folds ────────────────
    def run_all(cyc, oc, label=""):
        t0 = time.time()
        r_up = sweep_up(cyc, oc, up_grid)
        r_dn = sweep_dn(cyc, oc, dn_grid)
        r_cq = sweep_cheap(cyc, oc, cq_grid)
        merged = {**r_up, **r_dn, **r_cq}
        dt = time.time() - t0
        if label:
            print(f"  {label}: {len(r_up)}+{len(r_dn)}+{len(r_cq)}={len(merged)} in {dt:.1f}s")
        return merged

    print(f"\n  Running full dataset...")
    full = run_all(cycles, outcomes, "FULL")

    fold_results = {}
    for fn, (fc, fo) in folds.items():
        fold_results[fn] = run_all(fc, fo, fn)

    # ── Cross-validate: must profit on ALL folds ─────────────
    rows = []
    for key, f in full.items():
        all_ok = True
        fold_data = {}
        for fn in fold_names:
            if key in fold_results[fn]:
                sim = bankroll(fold_results[fn][key]["trades"], START, BET)
                fold_data[fn] = sim
                if sim["profit"] <= 0:
                    all_ok = False
            else:
                all_ok = False
                fold_data[fn] = None
        if not all_ok:
            continue

        sim_full = bankroll(f["trades"], START, BET)
        fold_finals = {fn: fd["final"] for fn, fd in fold_data.items() if fd}

        rows.append({
            "key": key, "family": key.split()[0],
            "n": f["n"], "wr": f["wr"], "avg_pnl": f["avg_pnl"],
            "tp_pct": f["tp_pct"], "hold": f["hold"],
            "final": sim_full["final"], "profit": sim_full["profit"],
            "max_dd": sim_full["max_dd"], "pf": sim_full["pf"],
            "edge": f["avg_pnl"],
            "min_fold": min(fold_finals.values()),
            "min_profit": min(fd["profit"] for fd in fold_data.values() if fd),
            "max_fold_dd": max(fd["max_dd"] for fd in fold_data.values() if fd),
            **{f"f_{fn}": fold_finals.get(fn, 0) for fn in fold_names},
        })

    rdf = pd.DataFrame(rows) if rows else pd.DataFrame()

    # ── Report ───────────────────────────────────────────────
    W = 130
    print(f"\n{'='*W}")
    print(f"  COMPREHENSIVE RESULTS ({n_folds}-fold CV, fee={FEE}/$ RT, {len(cycles)} cycles)")
    print(f"  Total combos tested: {len(full)}")
    print(f"  Survive ALL {n_folds} folds: {len(rdf)}")
    if len(rdf) > 0:
        for fam in ["UP", "DN", "CQ"]:
            c = len(rdf[rdf["family"] == fam])
            print(f"    {fam}: {c}")
    print(f"{'='*W}")

    if len(rdf) == 0:
        # Relax to N-2 folds
        min_ok = n_folds - 2
        print(f"\n  No strategy survived all {n_folds} folds.")
        print(f"  Relaxing to {min_ok}/{n_folds}...\n")
        rows2 = []
        for key, f in full.items():
            fold_data = {}
            n_ok = 0
            for fn in fold_names:
                if key in fold_results[fn]:
                    sim = bankroll(fold_results[fn][key]["trades"], START, BET)
                    fold_data[fn] = sim
                    if sim["profit"] > 0:
                        n_ok += 1
                else:
                    fold_data[fn] = None
            if n_ok < min_ok:
                continue
            sim_full = bankroll(f["trades"], START, BET)
            fold_finals = {fn: fd["final"] for fn, fd in fold_data.items() if fd}
            rows2.append({
                "key": key, "family": key.split()[0],
                "n": f["n"], "wr": f["wr"], "avg_pnl": f["avg_pnl"],
                "tp_pct": f["tp_pct"], "hold": f["hold"],
                "final": sim_full["final"], "profit": sim_full["profit"],
                "max_dd": sim_full["max_dd"], "pf": sim_full["pf"],
                "edge": f["avg_pnl"],
                "min_fold": min(fold_finals.values()) if fold_finals else 0,
                "folds_ok": n_ok,
            })
        rdf = pd.DataFrame(rows2) if rows2 else pd.DataFrame()
        if len(rdf) == 0:
            print(f"  Nothing survived {min_ok}/{n_folds}. Need more data.")
            return
        for fam in ["UP", "DN", "CQ"]:
            c = len(rdf[rdf["family"] == fam])
            print(f"    {fam}: {c}")

    # ── Top by family ────────────────────────────────────────
    for fam, fam_name in [("UP", "STAT-ARB UP"), ("DN", "STAT-ARB DOWN"), ("CQ", "CHEAP QUOTE")]:
        sub = rdf[rdf["family"] == fam]
        if len(sub) == 0:
            print(f"\n  {fam_name}: NO strategies survived all folds")
            continue

        print(f"\n  {'-'*W}")
        print(f"  {fam_name} -- {len(sub)} strategies survive all {n_folds} folds")
        print(f"  {'-'*W}")

        # Top 10 by edge
        top = sub.nlargest(10, "edge")
        print(f"\n  Top 10 by EDGE (avg PnL per $1):")
        print(f"  {'#':>2} {'Edge':>6} {'Final':>7} {'PF':>5} {'N':>4} {'WR':>5} {'TP%':>4} {'Hld':>3} {'MinF':>6} {'DD':>5}  Params")
        for i, (_, r) in enumerate(top.iterrows(), 1):
            print(f"  {i:>2} {r['edge']:>+5.3f} ${r['final']:>6.2f} {r['pf']:>5.2f}"
                  f" {int(r['n']):>4} {100*r['wr']:>4.0f}% {100*r['tp_pct']:>3.0f}%"
                  f" {r['hold']:>3.0f} ${r['min_fold']:>5.1f} {r['max_dd']:>4.1f}%  {r['key']}")

        # Top 10 by balance
        top_b = sub.nlargest(10, "final")
        print(f"\n  Top 10 by BALANCE ($100->${{best}}):")
        print(f"  {'#':>2} {'Final':>7} {'Edge':>6} {'PF':>5} {'N':>4} {'WR':>5} {'TP%':>4} {'MinF':>6} {'DD':>5}  Params")
        for i, (_, r) in enumerate(top_b.iterrows(), 1):
            print(f"  {i:>2} ${r['final']:>6.2f} {r['edge']:>+5.3f} {r['pf']:>5.2f}"
                  f" {int(r['n']):>4} {100*r['wr']:>4.0f}% {100*r['tp_pct']:>3.0f}%"
                  f" ${r['min_fold']:>5.1f} {r['max_dd']:>4.1f}%  {r['key']}")

        # Top 5 most robust
        top_r = sub.nlargest(5, "min_fold")
        print(f"\n  Top 5 MOST ROBUST (best worst-fold):")
        print(f"  {'#':>2} {'MinF':>6} {'Final':>7} {'Edge':>6} {'PF':>5} {'N':>4} {'WR':>5}  Params")
        for i, (_, r) in enumerate(top_r.iterrows(), 1):
            print(f"  {i:>2} ${r['min_fold']:>5.2f} ${r['final']:>6.2f} {r['edge']:>+5.3f}"
                  f" {r['pf']:>5.2f} {int(r['n']):>4} {100*r['wr']:>4.0f}%  {r['key']}")

    # ── Overall best ─────────────────────────────────────────
    print(f"\n{'='*W}")
    print(f"  OVERALL RECOMMENDATIONS (across all families)")
    print(f"{'='*W}")

    best_edge = rdf.loc[rdf["edge"].idxmax()]
    print(f"\n  HIGHEST EDGE: {best_edge['key']}")
    print(f"    +{best_edge['edge']:.4f}/trade | $100 -> ${best_edge['final']:.2f}"
          f" in {int(best_edge['n'])} trades | WR={100*best_edge['wr']:.0f}%"
          f" PF={best_edge['pf']:.2f} | MinFold=${best_edge['min_fold']:.2f}")

    best_bal = rdf.loc[rdf["final"].idxmax()]
    print(f"\n  HIGHEST BALANCE: {best_bal['key']}")
    print(f"    +{best_bal['edge']:.4f}/trade | $100 -> ${best_bal['final']:.2f}"
          f" in {int(best_bal['n'])} trades | WR={100*best_bal['wr']:.0f}%"
          f" PF={best_bal['pf']:.2f} | DD={best_bal['max_dd']:.1f}%")

    safest = rdf.loc[rdf["min_fold"].idxmax()]
    print(f"\n  MOST ROBUST: {safest['key']}")
    print(f"    +{safest['edge']:.4f}/trade | $100 -> ${safest['final']:.2f}"
          f" in {int(safest['n'])} trades | MinFold=${safest['min_fold']:.2f}"
          f" | WR={100*safest['wr']:.0f}% PF={safest['pf']:.2f}")

    # Best with N >= 50 trades (enough samples)
    sub50 = rdf[rdf["n"] >= 50]
    if len(sub50) > 0:
        best50 = sub50.loc[sub50["edge"].idxmax()]
        print(f"\n  BEST HIGH-VOLUME (N>=50): {best50['key']}")
        print(f"    +{best50['edge']:.4f}/trade | $100 -> ${best50['final']:.2f}"
              f" in {int(best50['n'])} trades | WR={100*best50['wr']:.0f}%"
              f" PF={best50['pf']:.2f}")

    print(f"\n{'='*W}")


if __name__ == "__main__":
    main()
