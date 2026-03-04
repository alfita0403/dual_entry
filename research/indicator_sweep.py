"""
Exhaustive indicator sweep for RSI boost optimization.

Tests ALL combinations of:
  - Indicators: RSI, Stochastic %K, MACD histogram, Bollinger %B
  - Timeframes: 1m, 3m, 5m, 15m
  - Periods: 5, 7, 9, 14, 21 (where applicable)
  - Thresholds: fine-grained sweep per indicator

For each combo, computes:
  1. Point-biserial correlation (indicator value vs trade outcome)
  2. Boost PnL improvement (base $5 -> $8 when indicator confirms)
  3. Bonferroni-corrected p-values across ALL tests

Uses ETHUSDT klines (ETH-only strategy). Telonex 74-day dataset.
No look-ahead bias: uses last COMPLETED candle before market open.
"""

import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from pathlib import Path
from scipy.stats import pointbiserialr, mannwhitneyu
from backtest import (
    load_telonex,
    build_sequences,
    find_trades,
    fetch_klines,
    compute_rsi,
    compute_stoch,
    compute_macd,
    compute_bb,
    compute_vol_ratio,
    OUTPUT_DIR,
)

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    HAS_MPL = True
except ImportError:
    HAS_MPL = False


RULES = [
    {"pattern": "DDDDD", "side": "UP", "coins": ["ETH"], "max_ask": 0.55},
    {"pattern": "DDDD", "side": "UP", "coins": ["ETH"], "max_ask": 0.55},
    {"pattern": "UUUU", "side": "DOWN", "coins": ["ETH"], "max_ask": 0.55},
    {"pattern": "UUU", "side": "DOWN", "coins": ["ETH"], "max_ask": 0.55},
    {"pattern": "DDD", "side": "UP", "coins": ["ETH"], "max_ask": 0.55},
]

BASE_SIZE = 5.0
BOOST_SIZE = 8.0
SYMBOL = "ETHUSDT"

# What we test
TIMEFRAMES = ["1m", "3m", "5m", "15m"]
RSI_PERIODS = [5, 7, 9, 14, 21]
STOCH_PERIODS = [5, 7, 9, 14, 21]
MACD_CONFIGS = [(8, 17, 9), (12, 26, 9)]  # (fast, slow, signal)
BB_PERIODS = [10, 20]

TF_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
}


def get_indicator_at_ts(lookup, ts, col, tf_s):
    """Look up indicator value using last COMPLETED candle (no look-ahead)."""
    snapped = (ts // tf_s) * tf_s
    last_completed = snapped - tf_s
    for candidate in [last_completed, last_completed - tf_s]:
        if candidate in lookup:
            v = lookup[candidate].get(col)
            if v is not None and np.isfinite(v):
                return v
    return np.nan


def build_lookup(kl, columns):
    """Build {ts_s -> {col: val}} lookup from klines DataFrame."""
    lookup = {}
    for _, row in kl.iterrows():
        ts = int(row["ts_s"])
        d = {}
        for c in columns:
            v = row.get(c)
            if v is not None and isinstance(v, (int, float, np.integer, np.floating)):
                d[c] = float(v)
        lookup[ts] = d
    return lookup


def fetch_and_compute(tf, ts_min, ts_max):
    """Fetch ETHUSDT klines and compute ALL indicators for this timeframe."""
    tf_s = TF_SECONDS[tf]
    warmup_s = 200 * tf_s
    start_ms = int((ts_min - warmup_s) * 1000)
    end_ms = int((ts_max + tf_s * 2) * 1000)
    kl = fetch_klines(SYMBOL, tf, start_ms, end_ms)
    if kl.empty:
        return {}, []

    kl["ts_s"] = (kl["open_time_ms"] / 1000).astype(int)
    close = kl["close"].values
    high = kl["high"].values
    low = kl["low"].values
    vol = kl["volume"].values

    columns = []

    # RSI at various periods
    for p in RSI_PERIODS:
        col = f"RSI_{p}"
        kl[col] = compute_rsi(close, p)
        columns.append(col)

    # Stochastic %K at various periods
    for p in STOCH_PERIODS:
        col = f"STOCH_{p}"
        kl[col] = compute_stoch(close, high, low, p, smooth=3)
        columns.append(col)

    # MACD histogram
    for fast, slow, sig in MACD_CONFIGS:
        col = f"MACD_{fast}_{slow}_{sig}"
        kl[col] = compute_macd(close, fast, slow, sig)
        columns.append(col)

    # Bollinger %B
    for p in BB_PERIODS:
        col = f"BB_PCT_{p}"
        sma, upper, lower = compute_bb(close, p)
        # %B = (close - lower) / (upper - lower)
        bw = upper - lower
        pct_b = np.where(bw > 0, (close - lower) / bw, 0.5)
        # Set NaN where BB is NaN
        pct_b = np.where(np.isnan(sma), np.nan, pct_b)
        kl[col] = pct_b
        columns.append(col)

    # Volume ratio
    for p in [10, 20]:
        col = f"VOL_RATIO_{p}"
        kl[col] = compute_vol_ratio(vol, p)
        columns.append(col)

    lookup = build_lookup(kl, columns)
    return lookup, columns


def test_indicator_significance(trades_df, lookup, col, tf_s):
    """Test if indicator values differ between wins and losses.

    Returns (r, p, n_valid, mean_win, mean_loss) from point-biserial.
    """
    vals = []
    outcomes = []
    for _, row in trades_df.iterrows():
        v = get_indicator_at_ts(lookup, row["start_ts"], col, tf_s)
        if not np.isnan(v):
            vals.append(v)
            outcomes.append(1 if row["hit"] else 0)

    if len(vals) < 30:
        return np.nan, 1.0, len(vals), np.nan, np.nan

    vals = np.array(vals)
    outcomes = np.array(outcomes)
    r, p = pointbiserialr(outcomes, vals)
    mean_win = vals[outcomes == 1].mean()
    mean_loss = vals[outcomes == 0].mean()
    return r, p, len(vals), mean_win, mean_loss


def test_indicator_significance_by_side(trades_df, lookup, col, tf_s):
    """Test separately for UP and DOWN bets."""
    results = {}
    for side_label, side_val in [("UP", "UP"), ("DOWN", "DOWN")]:
        sub = trades_df[trades_df["side"] == side_val]
        vals = []
        outcomes = []
        for _, row in sub.iterrows():
            v = get_indicator_at_ts(lookup, row["start_ts"], col, tf_s)
            if not np.isnan(v):
                vals.append(v)
                outcomes.append(1 if row["hit"] else 0)

        if len(vals) < 20:
            results[side_label] = (np.nan, 1.0, len(vals))
            continue

        vals = np.array(vals)
        outcomes = np.array(outcomes)
        r, p = pointbiserialr(outcomes, vals)
        results[side_label] = (r, p, len(vals))
    return results


def compute_boost_pnl(
    trades_df, lookup, col, tf_s, up_thresh, down_thresh, up_below=True, down_above=True
):
    """Simulate boost PnL with given thresholds.

    up_below=True means UP bets boost when indicator < up_thresh (like RSI oversold).
    down_above=True means DOWN bets boost when indicator > down_thresh (like RSI overbought).
    """
    flat_pnl = 0.0
    boost_pnl = 0.0
    boosted_n = 0
    boosted_wins = 0
    total_n = 0

    for _, row in trades_df.iterrows():
        v = get_indicator_at_ts(lookup, row["start_ts"], col, tf_s)
        side = row["side"]
        hit = row["hit"]
        ma = row["max_ask"]

        flat = ((1.0 - ma) * BASE_SIZE) if hit else (-ma * BASE_SIZE)
        flat_pnl += flat

        boost = False
        if not np.isnan(v):
            if side == "UP":
                if up_below and v < up_thresh:
                    boost = True
                elif not up_below and v > up_thresh:
                    boost = True
            elif side == "DOWN":
                if down_above and v > down_thresh:
                    boost = True
                elif not down_above and v < down_thresh:
                    boost = True

        size = BOOST_SIZE if boost else BASE_SIZE
        pnl = ((1.0 - ma) * size) if hit else (-ma * size)
        boost_pnl += pnl
        total_n += 1
        if boost:
            boosted_n += 1
            if hit:
                boosted_wins += 1

    bwr = boosted_wins / boosted_n * 100 if boosted_n > 0 else 0
    return {
        "flat_pnl": flat_pnl,
        "boost_pnl": boost_pnl,
        "improvement": boost_pnl - flat_pnl,
        "boosted_n": boosted_n,
        "boosted_wr": bwr,
        "total_n": total_n,
    }


def main():
    print("=" * 80)
    print("  INDICATOR SWEEP - Telonex 74 days, ETHUSDT")
    print("=" * 80)

    # Load trades
    print("\n  Loading Telonex...")
    tdf = load_telonex()
    seqs = build_sequences(tdf)
    trades_df = find_trades(seqs, RULES, size=BASE_SIZE, fee=0.0)
    print(f"  {len(trades_df)} trades")
    up_trades = len(trades_df[trades_df["side"] == "UP"])
    dn_trades = len(trades_df[trades_df["side"] == "DOWN"])
    print(f"  UP={up_trades}, DOWN={dn_trades}")
    flat_pnl = trades_df["pnl"].sum()
    print(f"  Flat baseline: ${flat_pnl:+.2f}")

    ts_min = int(trades_df["start_ts"].min())
    ts_max = int(trades_df["start_ts"].max())

    # ---------------------------------------------------------------
    # Phase 1: Significance testing (point-biserial correlation)
    # ---------------------------------------------------------------
    print(f"\n{'=' * 80}")
    print("  PHASE 1: SIGNIFICANCE TESTING")
    print(f"{'=' * 80}")

    all_results = []
    total_tests = 0

    for tf in TIMEFRAMES:
        print(f"\n  --- Timeframe: {tf} ---")
        tf_s = TF_SECONDS[tf]
        lookup, columns = fetch_and_compute(tf, ts_min, ts_max)
        if not lookup:
            print(f"  No data for {tf}")
            continue

        for col in columns:
            # Overall significance
            r, p, n, mean_win, mean_loss = test_indicator_significance(
                trades_df, lookup, col, tf_s
            )
            # Per-side significance
            side_results = test_indicator_significance_by_side(
                trades_df, lookup, col, tf_s
            )
            up_r, up_p, up_n = side_results.get("UP", (np.nan, 1.0, 0))
            dn_r, dn_p, dn_n = side_results.get("DOWN", (np.nan, 1.0, 0))

            all_results.append(
                {
                    "tf": tf,
                    "indicator": col,
                    "r_overall": r,
                    "p_overall": p,
                    "n_overall": n,
                    "mean_win": mean_win,
                    "mean_loss": mean_loss,
                    "r_up": up_r,
                    "p_up": up_p,
                    "n_up": up_n,
                    "r_down": dn_r,
                    "p_down": dn_p,
                    "n_down": dn_n,
                }
            )
            total_tests += 1

    # Apply Bonferroni correction
    n_tests = total_tests * 3  # overall + UP + DOWN per indicator
    print(f"\n  Total indicator/timeframe combos: {total_tests}")
    print(
        f"  Bonferroni correction for {n_tests} tests (alpha=0.05 -> {0.05 / n_tests:.6f})"
    )

    bonferroni_alpha = 0.05 / n_tests

    # Sort by most significant overall
    all_results.sort(key=lambda x: x["p_overall"])

    print(
        f"\n  {'Indicator':<20} {'TF':<5} {'r':>7} {'p-val':>10} {'Bonf':>6} "
        f"{'n':>5} {'UP_r':>7} {'UP_p':>10} {'DN_r':>7} {'DN_p':>10}"
    )
    print(
        f"  {'-' * 20} {'-' * 5} {'-' * 7} {'-' * 10} {'-' * 6} "
        f"{'-' * 5} {'-' * 7} {'-' * 10} {'-' * 7} {'-' * 10}"
    )

    significant = []
    for res in all_results:
        is_sig = res["p_overall"] < bonferroni_alpha
        up_sig = res["p_up"] < bonferroni_alpha
        dn_sig = res["p_down"] < bonferroni_alpha
        sig_str = "***" if is_sig else ("*" if res["p_overall"] < 0.05 else "")

        print(
            f"  {res['indicator']:<20} {res['tf']:<5} "
            f"{res['r_overall']:>+.4f} {res['p_overall']:>10.6f} {sig_str:>6} "
            f"{res['n_overall']:>5} "
            f"{res['r_up']:>+.4f} {res['p_up']:>10.6f} "
            f"{res['r_down']:>+.4f} {res['p_down']:>10.6f}"
        )

        if is_sig or up_sig or dn_sig:
            significant.append(res)

    # ---------------------------------------------------------------
    # Phase 2: Boost PnL for significant indicators
    # ---------------------------------------------------------------
    print(f"\n{'=' * 80}")
    print("  PHASE 2: BOOST PnL FOR SIGNIFICANT INDICATORS")
    print(f"{'=' * 80}")

    # Also test the top-10 by raw p-value even if not Bonferroni-significant
    candidates = list(significant)
    for res in all_results[:10]:
        if res not in candidates:
            candidates.append(res)

    if not candidates:
        print("\n  No significant indicators found.")
        candidates = all_results[:10]
        print(f"  Testing top {len(candidates)} by p-value anyway.\n")

    boost_results = []

    for res in candidates:
        tf = res["tf"]
        col = res["indicator"]
        tf_s = TF_SECONDS[tf]
        lookup, _ = fetch_and_compute(tf, ts_min, ts_max)

        # Determine boost direction based on correlation sign
        # For UP bets: negative r means lower indicator = more wins -> boost when below
        # For DOWN bets: positive r means higher indicator = more wins -> boost when above

        # Collect indicator values for threshold calibration
        up_vals = []
        dn_vals = []
        for _, row in trades_df.iterrows():
            v = get_indicator_at_ts(lookup, row["start_ts"], col, tf_s)
            if not np.isnan(v):
                if row["side"] == "UP":
                    up_vals.append(v)
                else:
                    dn_vals.append(v)

        if not up_vals or not dn_vals:
            continue

        up_arr = np.array(up_vals)
        dn_arr = np.array(dn_vals)

        # Determine direction from correlation
        is_rsi_like = "RSI" in col or "STOCH" in col or "BB_PCT" in col
        # RSI-like: bounded 0-100 (or 0-1 for BB%B)
        # MACD/VOL: unbounded

        if is_rsi_like:
            # UP bets: try boost when indicator is LOW (oversold)
            up_thresholds = np.percentile(up_arr, [20, 30, 40, 50])
            # DOWN bets: try boost when indicator is HIGH (overbought)
            dn_thresholds = np.percentile(dn_arr, [50, 60, 70, 80])

            best_imp = -9999
            best_cfg = None

            for up_th in up_thresholds:
                for dn_th in dn_thresholds:
                    r = compute_boost_pnl(
                        trades_df,
                        lookup,
                        col,
                        tf_s,
                        up_th,
                        dn_th,
                        up_below=True,
                        down_above=True,
                    )
                    if r["improvement"] > best_imp:
                        best_imp = r["improvement"]
                        best_cfg = {
                            "up_thresh": up_th,
                            "dn_thresh": dn_th,
                            "up_dir": "below",
                            "dn_dir": "above",
                            **r,
                        }
        else:
            # MACD: UP bets boost when MACD < 0 (bearish, confirms oversold)
            #        DOWN bets boost when MACD > 0 (bullish, confirms overbought)
            up_thresholds = np.percentile(up_arr, [30, 40, 50])
            dn_thresholds = np.percentile(dn_arr, [50, 60, 70])

            best_imp = -9999
            best_cfg = None

            for up_th in up_thresholds:
                for dn_th in dn_thresholds:
                    r = compute_boost_pnl(
                        trades_df,
                        lookup,
                        col,
                        tf_s,
                        up_th,
                        dn_th,
                        up_below=True,
                        down_above=True,
                    )
                    if r["improvement"] > best_imp:
                        best_imp = r["improvement"]
                        best_cfg = {
                            "up_thresh": up_th,
                            "dn_thresh": dn_th,
                            "up_dir": "below",
                            "dn_dir": "above",
                            **r,
                        }

        if best_cfg:
            boost_results.append(
                {
                    "tf": tf,
                    "indicator": col,
                    "p_overall": res["p_overall"],
                    "p_up": res["p_up"],
                    "p_down": res["p_down"],
                    "bonferroni_sig": res["p_overall"] < bonferroni_alpha,
                    **best_cfg,
                }
            )

    # Sort by improvement
    boost_results.sort(key=lambda x: -x["improvement"])

    print(
        f"\n  {'Indicator':<20} {'TF':<5} {'p-val':>10} {'Bonf':>5} "
        f"{'UP<':>8} {'DN>':>8} {'Boost#':>7} {'BstWR':>6} {'Improv':>8}"
    )
    print(
        f"  {'-' * 20} {'-' * 5} {'-' * 10} {'-' * 5} "
        f"{'-' * 8} {'-' * 8} {'-' * 7} {'-' * 6} {'-' * 8}"
    )

    for br in boost_results:
        sig = "YES" if br["bonferroni_sig"] else "no"
        print(
            f"  {br['indicator']:<20} {br['tf']:<5} "
            f"{br['p_overall']:>10.6f} {sig:>5} "
            f"{br['up_thresh']:>8.2f} {br['dn_thresh']:>8.2f} "
            f"{br['boosted_n']:>7} {br['boosted_wr']:>5.1f}% "
            f"${br['improvement']:>+7.2f}"
        )

    # ---------------------------------------------------------------
    # Phase 3: Summary and recommendation
    # ---------------------------------------------------------------
    print(f"\n{'=' * 80}")
    print("  PHASE 3: SUMMARY")
    print(f"{'=' * 80}")

    bonf_significant = [r for r in boost_results if r["bonferroni_sig"]]
    if bonf_significant:
        print(f"\n  {len(bonf_significant)} Bonferroni-significant indicators found:")
        for r in bonf_significant[:5]:
            print(
                f"    {r['indicator']} @ {r['tf']}: "
                f"p={r['p_overall']:.6f}, "
                f"UP {r['up_dir']}<{r['up_thresh']:.1f}, "
                f"DN {r['dn_dir']}>{r['dn_thresh']:.1f}, "
                f"improvement=${r['improvement']:+.2f}"
            )
    else:
        print("\n  NO Bonferroni-significant indicators found.")
        print("  Top 3 by raw p-value:")
        for r in boost_results[:3]:
            print(
                f"    {r['indicator']} @ {r['tf']}: "
                f"p={r['p_overall']:.6f}, "
                f"improvement=${r['improvement']:+.2f}"
            )

    # Current config comparison
    print(f"\n  CURRENT CONFIG: RSI_7 @ 5m, UP<35, DN>55")
    current = None
    for br in boost_results:
        if br["indicator"] == "RSI_7" and br["tf"] == "5m":
            current = br
            break
    if current:
        print(
            f"    p={current['p_overall']:.6f}, improvement=${current['improvement']:+.2f}"
        )
    else:
        # Compute it directly
        lookup, _ = fetch_and_compute("5m", ts_min, ts_max)
        r = compute_boost_pnl(
            trades_df, lookup, "RSI_7", 300, 35, 55, up_below=True, down_above=True
        )
        print(f"    improvement=${r['improvement']:+.2f}")

    # Find best overall
    if boost_results:
        best = boost_results[0]
        print(f"\n  BEST OVERALL: {best['indicator']} @ {best['tf']}")
        print(f"    p={best['p_overall']:.6f}, improvement=${best['improvement']:+.2f}")
        print(
            f"    UP {best['up_dir']}<{best['up_thresh']:.2f}, "
            f"DN {best['dn_dir']}>{best['dn_thresh']:.2f}"
        )
        print(
            f"    Boosted {best['boosted_n']}/{best['total_n']} trades, "
            f"WR={best['boosted_wr']:.1f}%"
        )

    # Plot if available
    if HAS_MPL and boost_results:
        OUTPUT_DIR.mkdir(exist_ok=True)
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 7))
        fig.suptitle(
            f"Indicator Sweep - {total_tests} combos, "
            f"Bonferroni alpha={bonferroni_alpha:.5f}",
            fontsize=12,
            fontweight="bold",
        )

        # Left: p-values by indicator type
        for res in all_results:
            color = (
                "green"
                if res["p_overall"] < bonferroni_alpha
                else ("orange" if res["p_overall"] < 0.05 else "gray")
            )
            ind_type = res["indicator"].split("_")[0]
            x_map = {"RSI": 0, "STOCH": 1, "MACD": 2, "BB": 3, "VOL": 4}
            x = x_map.get(ind_type, 5)
            tf_offset = {"1m": -0.3, "3m": -0.1, "5m": 0.1, "15m": 0.3}
            ax1.scatter(
                x + tf_offset.get(res["tf"], 0),
                -np.log10(max(res["p_overall"], 1e-20)),
                color=color,
                s=40,
                alpha=0.7,
            )
        ax1.axhline(
            -np.log10(bonferroni_alpha),
            color="red",
            ls="--",
            label=f"Bonferroni ({bonferroni_alpha:.5f})",
        )
        ax1.axhline(-np.log10(0.05), color="orange", ls="--", label="p=0.05")
        ax1.set_xticks(range(5))
        ax1.set_xticklabels(["RSI", "STOCH", "MACD", "BB%B", "VOL"])
        ax1.set_ylabel("-log10(p-value)")
        ax1.set_title("Significance by Indicator Type")
        ax1.legend(fontsize=8)

        # Right: improvement by indicator
        names = [f"{r['indicator']}\n{r['tf']}" for r in boost_results[:15]]
        imps = [r["improvement"] for r in boost_results[:15]]
        colors = [
            "green" if r["bonferroni_sig"] else "gray" for r in boost_results[:15]
        ]
        ax2.barh(range(len(names)), imps, color=colors)
        ax2.set_yticks(range(len(names)))
        ax2.set_yticklabels(names, fontsize=7)
        ax2.set_xlabel("PnL Improvement ($)")
        ax2.set_title("Top 15 Boost Improvements")
        ax2.invert_yaxis()

        plt.tight_layout()
        path = OUTPUT_DIR / "indicator_sweep.png"
        plt.savefig(path, dpi=150)
        print(f"\n  Plot saved: {path}")

    print(f"\n{'=' * 80}")
    print("  Done!")


if __name__ == "__main__":
    main()
