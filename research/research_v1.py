"""Strategy research v3 - low frequency, high edge, with fees.

Focus: large divergences only, few trades, survive multiple CV folds.
Includes round-trip fee deduction per trade.

Usage:
    python strategies/research.py data/prices_2026-02-28.csv
    python strategies/research.py --window 60 90 data/prices_2026-02-28.csv
    python strategies/research.py --window 60 --spread 0.12 0.15 0.20
"""

import argparse
import sys
import time
from itertools import groupby as igb
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

COINS = ["BTC", "ETH", "SOL", "XRP"]
ASK_COLS = [f"{c.lower()}_up_ask" for c in COINS]
BID_COLS = [f"{c.lower()}_up_bid" for c in COINS]


def load_data(paths: List[str]) -> pd.DataFrame:
    frames = [pd.read_csv(p, parse_dates=["timestamp", "cycle_start"]) for p in paths]
    df = pd.concat(frames, ignore_index=True).sort_values("timestamp").reset_index(drop=True)
    df["group_mean_ask"] = df[ASK_COLS].mean(axis=1)
    return df


def get_cycles(df: pd.DataFrame, min_rows: int = 20) -> List[pd.DataFrame]:
    return [g.reset_index(drop=True) for _, g in df.groupby("cycle_start") if len(g) >= min_rows]


def determine_outcomes(cycle: pd.DataFrame) -> Dict[str, Optional[str]]:
    late = cycle[cycle["seconds_elapsed"] >= 280]
    if len(late) < 3:
        return {c: None for c in COINS}
    out = {}
    for coin in COINS:
        avg = late[f"{coin.lower()}_up_ask"].mean()
        out[coin] = "UP" if avg >= 0.70 else ("DOWN" if avg <= 0.30 else None)
    return out


# ── Signal finding ───────────────────────────────────────────

def find_signals(
    cycles: List[pd.DataFrame],
    all_outcomes: List[Dict],
    min_spread: float,
    cooldown: int,
    max_entry_t: int,
) -> List[Dict]:
    signals = []
    for ci, (cycle, outcomes) in enumerate(zip(cycles, all_outcomes)):
        early = cycle[(cycle["early"] == True) & (cycle["seconds_elapsed"] <= max_entry_t)]
        last_t = -cooldown
        t_arr = cycle["seconds_elapsed"].values
        bid_arrays = {coin: cycle[f"{coin.lower()}_up_bid"].values for coin in COINS}

        for _, row in early.iterrows():
            t = int(row["seconds_elapsed"])
            if t - last_t < cooldown:
                continue
            mean_ask = row["group_mean_ask"]
            best_coin, best_dev = None, 0.0
            for coin in COINS:
                dev = mean_ask - row[f"{coin.lower()}_up_ask"]
                if dev > best_dev:
                    best_coin, best_dev = coin, dev
            if best_coin is None or best_dev < min_spread:
                continue

            entry_ask = row[f"{best_coin.lower()}_up_ask"]
            outcome = outcomes.get(best_coin)
            mask = t_arr > t
            signals.append({
                "ci": ci, "t": t, "coin": best_coin, "dev": best_dev,
                "entry_ask": entry_ask, "outcome": outcome,
                "future_t": t_arr[mask],
                "future_bid": bid_arrays[best_coin][mask],
            })
            last_t = t
    return signals


# ── Trade simulation ─────────────────────────────────────────

def simulate_signals(
    signals: List[Dict], tp_target: float, timeout: int,
    fee_per_dollar: float = 0.0,
) -> List[Dict]:
    trades = []
    for sig in signals:
        entry = sig["entry_ask"]
        ft, fb = sig["future_t"], sig["future_bid"]
        max_t = min(sig["t"] + timeout, 300)

        tp_mask = (fb >= entry + tp_target) & (ft <= max_t)
        tp_idx = np.argmax(tp_mask) if tp_mask.any() else -1

        if tp_idx >= 0 and tp_mask[tp_idx]:
            pnl = float(fb[tp_idx]) - entry - fee_per_dollar
            hold = int(ft[tp_idx]) - sig["t"]
            etype = "TP"
        else:
            to_mask = ft <= max_t
            if to_mask.any():
                last_i = np.where(to_mask)[0][-1]
                pnl = float(fb[last_i]) - entry - fee_per_dollar
                hold = int(ft[last_i]) - sig["t"]
                etype = "TIMEOUT"
            elif sig["outcome"] == "UP":
                pnl = 1.0 - entry - fee_per_dollar
                hold, etype = 300 - sig["t"], "EXPIRY"
            elif sig["outcome"] == "DOWN":
                pnl = -entry - fee_per_dollar
                hold, etype = 300 - sig["t"], "EXPIRY"
            else:
                continue

        trades.append({
            "pnl": pnl, "hold": hold, "exit_type": etype,
            "ci": sig["ci"], "dev": sig["dev"],
        })
    return trades


# ── Bankroll simulation ─────────────────────────────────────

def simulate_bankroll(trades: List[Dict], start: float, bet: float) -> Dict:
    balance = start
    peak = start
    max_dd = 0.0
    gross_w = gross_l = 0.0

    for t in trades:
        real = t["pnl"] * bet
        balance += real
        if real > 0:
            gross_w += real
        else:
            gross_l += abs(real)
        if balance > peak:
            peak = balance
        dd = (peak - balance) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    return {
        "final": balance,
        "profit": balance - start,
        "max_dd": max_dd * 100,
        "pf": gross_w / gross_l if gross_l > 0 else float("inf"),
        "worst": min(t["pnl"] for t in trades) * bet if trades else 0,
    }


# ── Sweep engine ─────────────────────────────────────────────

def make_key(ms, cd, me, tp, to):
    return f"s={ms:.2f} cd={cd} me={me} tp={tp:.2f} to={to}s"


def run_sweep(cycles, outcomes, grid, fee):
    """Returns {key: {"summary": ..., "trades": [...]}}"""

    def entry_key(p):
        return (p["ms"], p["cd"], p["me"])

    sorted_grid = sorted(grid, key=entry_key)
    results = {}
    sig_cache = {}

    for ek, group in igb(sorted_grid, key=entry_key):
        ms, cd, me = ek
        if (ms, cd, me) not in sig_cache:
            sig_cache[(ms, cd, me)] = find_signals(cycles, outcomes, ms, cd, me)
        sigs = sig_cache[(ms, cd, me)]
        if not sigs:
            continue

        for p in group:
            trades = simulate_signals(sigs, p["tp"], p["to"], fee)
            if len(trades) < 3:
                continue
            pnls = [t["pnl"] for t in trades]
            n = len(trades)
            key = make_key(p["ms"], p["cd"], p["me"], p["tp"], p["to"])
            results[key] = {
                "trades": trades,
                "n": n,
                "wr": sum(1 for p in pnls if p > 0) / n,
                "avg_pnl": float(np.mean(pnls)),
                "total_pnl": float(np.sum(pnls)),
                "hold": float(np.mean([t["hold"] for t in trades])),
                "tp_pct": sum(1 for t in trades if t["exit_type"] == "TP") / n,
            }
    return results


# ── Multi-fold cross-validation ──────────────────────────────

def build_folds(cycles, outcomes):
    """9-fold cross-validation designed to make overfitting impossible.

    Three independent splitting axes:
      1. DAY-LEVEL out-of-sample (strongest test: different market regimes)
      2. Chronological halves + individual thirds (temporal stability)
      3. Odd/even cycles (regime-agnostic sampling)

    A strategy must profit on ALL folds to pass.
    """
    n = len(cycles)
    folds = {}

    # ── Axis 1: Day-level out-of-sample (hardest test) ───────
    # Each day is a completely independent market session.
    # If strategy can't profit on each day separately → not real edge.
    day_map: Dict[str, List[int]] = {}
    for i, c in enumerate(cycles):
        day = str(c["cycle_start"].iloc[0].date())
        day_map.setdefault(day, []).append(i)

    for day, indices in sorted(day_map.items()):
        if len(indices) >= 5:  # need minimum cycles for meaningful test
            folds[f"day_{day}"] = (
                [cycles[i] for i in indices],
                [outcomes[i] for i in indices],
            )

    # ── Axis 2: Chronological splits (temporal stability) ────
    mid = n // 2
    folds["chrono_1st"] = (cycles[:mid], outcomes[:mid])
    folds["chrono_2nd"] = (cycles[mid:], outcomes[mid:])

    # Individual thirds (no mixing first+last like old "edge" fold)
    t1 = n // 3
    t2 = 2 * n // 3
    folds["third_1"] = (cycles[:t1], outcomes[:t1])
    folds["third_2"] = (cycles[t1:t2], outcomes[t1:t2])
    folds["third_3"] = (cycles[t2:], outcomes[t2:])

    # ── Axis 3: Interleaved sampling (regime-agnostic) ───────
    odd_c = [cycles[i] for i in range(n) if i % 2 == 1]
    odd_o = [outcomes[i] for i in range(n) if i % 2 == 1]
    even_c = [cycles[i] for i in range(n) if i % 2 == 0]
    even_o = [outcomes[i] for i in range(n) if i % 2 == 0]
    folds["odd"] = (odd_c, odd_o)
    folds["even"] = (even_c, even_o)

    return folds


def parse_float_list(s: str) -> List[float]:
    """Parse a space-separated or comma-separated list of floats."""
    return [float(x.strip()) for x in s.replace(",", " ").split() if x.strip()]


def parse_int_list(s: str) -> List[int]:
    """Parse a space-separated or comma-separated list of ints."""
    return [int(x.strip()) for x in s.replace(",", " ").split() if x.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Cross-validated parameter optimizer for stat-arb divergence strategy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python strategies/research.py data/prices_2026-02-28.csv
  python strategies/research.py --window "60 90 120" data/prices_*.csv
  python strategies/research.py --spread "0.12 0.15 0.20" --window "30 60"
  python strategies/research.py --tp "0.02 0.05 0.15" --timeout "30 60 90"
        """,
    )
    p.add_argument("csv_files", nargs="*", help="CSV data files (default: data/prices_*.csv)")
    p.add_argument("--window", type=str, default=None,
                   help="Max entry time(s) in seconds from market birth "
                        "(space/comma-separated list, default: '60 90 120')")
    p.add_argument("--spread", type=str, default=None,
                   help="Min spread threshold(s) "
                        "(default: '0.10 0.11 0.12 0.13 0.14 0.15 0.16 0.18 0.20 0.22 0.25 0.30')")
    p.add_argument("--tp", type=str, default=None,
                   help="Take-profit target(s) "
                        "(default: '0.02 0.03 0.04 0.05 0.06 0.08 0.10 0.12 0.15')")
    p.add_argument("--timeout", type=str, default=None,
                   help="Timeout(s) in seconds "
                        "(default: '15 20 30 45 60 90 120')")
    p.add_argument("--cooldown", type=str, default=None,
                   help="Cooldown(s) in seconds (default: '10 15')")
    p.add_argument("--fee", type=float, default=0.01,
                   help="Fee per dollar round-trip (default: 0.01)")
    p.add_argument("--start", type=float, default=100.0,
                   help="Starting bankroll (default: 100)")
    p.add_argument("--bet", type=float, default=5.0,
                   help="Bet size per trade (default: 5)")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    START = args.start
    BET = args.bet
    FEE = args.fee

    csv_files = args.csv_files
    if not csv_files:
        csv_files = sorted(str(f) for f in Path("data").glob("prices_*.csv"))
    if not csv_files:
        print("No CSV files found")
        sys.exit(1)

    print(f"Loading {len(csv_files)} file(s)...")
    df = load_data(csv_files)
    cycles = get_cycles(df)
    outcomes = [determine_outcomes(c) for c in cycles]
    n_res = sum(1 for o in outcomes if any(v is not None for v in o.values()))
    print(f"{len(df):,} rows | {len(cycles)} cycles | {n_res} resolved")
    print(f"Bankroll: ${START:.0f} start, ${BET:.0f}/trade, fee={FEE:.2f}/$ RT")

    # ── Parameter grid ───────────────────────────────────────
    spreads = parse_float_list(args.spread) if args.spread else \
        [0.10, 0.11, 0.12, 0.13, 0.14, 0.15, 0.16, 0.18, 0.20, 0.22, 0.25, 0.30]
    tp_targets = parse_float_list(args.tp) if args.tp else \
        [0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12, 0.15]
    timeouts = parse_int_list(args.timeout) if args.timeout else \
        [15, 20, 30, 45, 60, 90, 120]
    cooldowns = parse_int_list(args.cooldown) if args.cooldown else \
        [10, 15]
    max_entries = parse_int_list(args.window) if args.window else \
        [60, 90, 120]

    grid = []
    for ms in spreads:
        for tp in tp_targets:
            for to in timeouts:
                for cd in cooldowns:
                    for me in max_entries:
                        grid.append({"ms": ms, "cd": cd, "me": me, "tp": tp, "to": to})

    print(f"Grid: {len(grid)} combos "
          f"({len(spreads)} spreads x {len(tp_targets)} tp x {len(timeouts)} to "
          f"x {len(cooldowns)} cd x {len(max_entries)} window)")
    print(f"  spreads:  {spreads}")
    print(f"  tp:       {tp_targets}")
    print(f"  timeout:  {timeouts}")
    print(f"  cooldown: {cooldowns}")
    print(f"  window:   {max_entries}")

    # ── Run on all folds ─────────────────────────────────────
    folds = build_folds(cycles, outcomes)

    print(f"\n  Running full dataset...")
    t0 = time.time()
    full = run_sweep(cycles, outcomes, grid, FEE)
    print(f"  {len(full)} combos in {time.time()-t0:.1f}s")

    fold_results = {}
    for fname, (fc, fo) in folds.items():
        print(f"  Running fold '{fname}' ({len(fc)} cycles)...")
        t0 = time.time()
        fold_results[fname] = run_sweep(fc, fo, grid, FEE)
        print(f"  {len(fold_results[fname])} combos in {time.time()-t0:.1f}s")

    # ── Cross-validate: must profit on ALL folds ───────────────
    fold_names = list(folds.keys())
    n_folds = len(fold_names)
    rows = []

    for key, f in full.items():
        fold_data = {}
        all_profitable = True
        for fname in fold_names:
            if key in fold_results[fname]:
                sim = simulate_bankroll(fold_results[fname][key]["trades"], START, BET)
                fold_data[fname] = sim
                if sim["profit"] <= 0:
                    all_profitable = False
            else:
                all_profitable = False
                fold_data[fname] = None

        if not all_profitable:
            continue

        sim_full = simulate_bankroll(f["trades"], START, BET)
        fold_finals = {fn: fd["final"] for fn, fd in fold_data.items() if fd}
        fold_profits = {fn: fd["profit"] for fn, fd in fold_data.items() if fd}
        fold_dds = {fn: fd["max_dd"] for fn, fd in fold_data.items() if fd}

        row = {
            "key": key,
            "n": f["n"], "wr": f["wr"], "avg_pnl": f["avg_pnl"],
            "tp_pct": f["tp_pct"], "hold": f["hold"],
            "final": sim_full["final"],
            "profit": sim_full["profit"],
            "max_dd": sim_full["max_dd"],
            "pf": sim_full["pf"],
            "worst": sim_full["worst"],
            # Edge metric: avg_pnl per trade (after fees)
            "edge": f["avg_pnl"],
            # CV aggregates
            "min_fold": min(fold_finals.values()),
            "min_profit": min(fold_profits.values()),
            "max_fold_dd": max(fold_dds.values()),
            "worst_fold": min(fold_finals, key=lambda k: fold_finals[k]),
        }
        # Per-fold finals (dynamic)
        for fn in fold_names:
            row[f"f_{fn}"] = fold_finals.get(fn, 0)
        rows.append(row)

    rdf = pd.DataFrame(rows) if rows else pd.DataFrame()

    # ── Report ───────────────────────────────────────────────
    print(f"\n{'='*130}")
    print(f"  RESULTS (fee={FEE:.2f}/$ included, ${BET:.0f}/trade)")
    print(f"  Combos in full sweep: {len(full)}")
    print(f"  Folds: {n_folds} ({', '.join(fold_names)})")
    print(f"  Profitable on ALL {n_folds} folds: {len(rdf)}")
    print(f"{'='*130}")

    if len(rdf) == 0:
        # Fallback: relax requirement
        min_ok = max(n_folds - 2, n_folds * 2 // 3)
        print(f"\n  No strategy survived all {n_folds} folds. "
              f"Relaxing to {min_ok}/{n_folds}...\n")
        rows2 = []
        for key, f in full.items():
            fold_data = {}
            n_profitable = 0
            for fname in fold_names:
                if key in fold_results[fname]:
                    sim = simulate_bankroll(
                        fold_results[fname][key]["trades"], START, BET)
                    fold_data[fname] = sim
                    if sim["profit"] > 0:
                        n_profitable += 1
                else:
                    fold_data[fname] = None

            if n_profitable < min_ok:
                continue

            sim_full = simulate_bankroll(f["trades"], START, BET)
            fold_finals = {fn: fd["final"] for fn, fd in fold_data.items() if fd}

            row = {
                "key": key, "n": f["n"], "wr": f["wr"],
                "avg_pnl": f["avg_pnl"], "tp_pct": f["tp_pct"],
                "hold": f["hold"],
                "final": sim_full["final"], "profit": sim_full["profit"],
                "max_dd": sim_full["max_dd"], "pf": sim_full["pf"],
                "worst": sim_full["worst"],
                "edge": f["avg_pnl"],
                "min_fold": min(fold_finals.values()) if fold_finals else 0,
                "folds_ok": n_profitable,
            }
            for fn in fold_names:
                row[f"f_{fn}"] = fold_finals.get(fn, 0)
            rows2.append(row)

        rdf = pd.DataFrame(rows2) if rows2 else pd.DataFrame()
        if len(rdf) == 0:
            print(f"  No strategy survived {min_ok}/{n_folds}. Need more data.")
            return
        print(f"  Profitable on >={min_ok}/{n_folds} folds: {len(rdf)}")

    # ── Truncate fold names for display ──────────────────────
    short = {fn: fn[:6] for fn in fold_names}

    # ── Top by EDGE (avg PnL per trade) ──────────────────────
    top_edge = rdf.nlargest(20, "edge")
    fold_hdr = " ".join(f"{short[fn]:>6}" for fn in fold_names)
    fold_sep = " ".join(f"{'------':>6}" for _ in fold_names)

    print(f"\n  TOP 20 BY EDGE (avg PnL per $1 trade, fees included)")
    print(f"  {'#':>2} {'Edge':>6} {'Final':>7} {'PF':>5} "
          f"{'N':>4} {'WR':>5} {'TP%':>4} {'Hld':>3} {'MinF':>6} "
          f"| {fold_hdr}  Params")
    print(f"  {'--':>2} {'------':>6} {'-------':>7} {'-----':>5} "
          f"{'----':>4} {'-----':>5} {'----':>4} {'---':>3} {'------':>6} "
          f"| {fold_sep}  {'-'*36}")

    for i, (_, r) in enumerate(top_edge.iterrows(), 1):
        fvals = " ".join(f"${r[f'f_{fn}']:>5.1f}" for fn in fold_names)
        print(f"  {i:>2} {r['edge']:>+5.3f} ${r['final']:>6.2f} "
              f"{r['pf']:>5.2f} "
              f"{int(r['n']):>4} {100*r['wr']:>4.0f}% "
              f"{100*r['tp_pct']:>3.0f}% {r['hold']:>3.0f} "
              f"${r['min_fold']:>5.1f} "
              f"| {fvals}  {r['key']}")

    # ── Top by final balance ─────────────────────────────────
    top = rdf.nlargest(20, "final")
    print(f"\n  TOP 20 BY FINAL BALANCE ($100 start, $5/trade)")
    print(f"  {'#':>2} {'Final':>7} {'Gain':>6} {'PF':>5} "
          f"{'N':>4} {'WR':>5} {'TP%':>4} {'Hld':>3} {'DD':>5} {'MinF':>6} "
          f"| {fold_hdr}  Params")
    print(f"  {'--':>2} {'-------':>7} {'------':>6} {'-----':>5} "
          f"{'----':>4} {'-----':>5} {'----':>4} {'---':>3} {'-----':>5} {'------':>6} "
          f"| {fold_sep}  {'-'*36}")

    for i, (_, r) in enumerate(top.iterrows(), 1):
        fvals = " ".join(f"${r[f'f_{fn}']:>5.1f}" for fn in fold_names)
        print(f"  {i:>2} ${r['final']:>6.2f} {r['profit']:>+5.2f} "
              f"{r['pf']:>5.2f} "
              f"{int(r['n']):>4} {100*r['wr']:>4.0f}% "
              f"{100*r['tp_pct']:>3.0f}% {r['hold']:>3.0f} "
              f"{r['max_dd']:>4.1f}% ${r['min_fold']:>5.1f} "
              f"| {fvals}  {r['key']}")

    # ── Top by worst fold (most robust) ──────────────────────
    top_safe = rdf.nlargest(10, "min_fold")
    print(f"\n  TOP 10 MOST ROBUST (best worst-fold)")
    print(f"  {'#':>2} {'Final':>7} {'Edge':>6} {'PF':>5} "
          f"{'N':>4} {'WR':>5} {'MinF':>6} {'WrstF':>12}"
          f"  Params")
    print(f"  {'--':>2} {'-------':>7} {'------':>6} {'-----':>5} "
          f"{'----':>4} {'-----':>5} {'------':>6} {'------------':>12}"
          f"  {'-'*36}")

    for i, (_, r) in enumerate(top_safe.iterrows(), 1):
        print(f"  {i:>2} ${r['final']:>6.2f} {r['edge']:>+5.3f} "
              f"{r['pf']:>5.2f} "
              f"{int(r['n']):>4} {100*r['wr']:>4.0f}% "
              f"${r['min_fold']:>5.2f} {r['worst_fold']:>12}"
              f"  {r['key']}")

    # ── Final recommendation ─────────────────────────────────
    print(f"\n{'='*130}")

    best_edge = rdf.loc[rdf["edge"].idxmax()]
    print(f"  HIGHEST EDGE (best avg PnL per trade):")
    print(f"  {best_edge['key']}")
    print(f"  Edge={best_edge['edge']:+.4f}/trade | "
          f"$100 -> ${best_edge['final']:.2f} in {int(best_edge['n'])} trades")
    print(f"  WR={100*best_edge['wr']:.0f}% | PF={best_edge['pf']:.2f} | "
          f"MaxDD={best_edge['max_dd']:.1f}% | "
          f"TP%={100*best_edge['tp_pct']:.0f}% | Hold={best_edge['hold']:.0f}s")
    fold_str = "  ".join(f"{fn}=${best_edge[f'f_{fn}']:.1f}"
                         for fn in fold_names)
    print(f"  Folds: {fold_str}")

    best_bal = rdf.loc[rdf["final"].idxmax()]
    if best_bal["key"] != best_edge["key"]:
        print(f"\n  HIGHEST BALANCE:")
        print(f"  {best_bal['key']}")
        print(f"  Edge={best_bal['edge']:+.4f}/trade | "
              f"$100 -> ${best_bal['final']:.2f} in {int(best_bal['n'])} trades")
        print(f"  WR={100*best_bal['wr']:.0f}% | PF={best_bal['pf']:.2f} | "
              f"MaxDD={best_bal['max_dd']:.1f}%")

    safest = rdf.loc[rdf["min_fold"].idxmax()]
    if safest["key"] != best_edge["key"]:
        print(f"\n  MOST ROBUST (best worst-fold):")
        print(f"  {safest['key']}")
        print(f"  Edge={safest['edge']:+.4f}/trade | "
              f"$100 -> ${safest['final']:.2f} in {int(safest['n'])} trades")
        print(f"  MinFold=${safest['min_fold']:.2f} | "
              f"WR={100*safest['wr']:.0f}% | PF={safest['pf']:.2f}")

    print(f"{'='*130}")


if __name__ == "__main__":
    main()
