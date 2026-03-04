"""Quick analysis of live trade log."""
import re
import json
import urllib.request
import time
from pathlib import Path

CACHE_FILE = Path(__file__).parent / "resolution_cache.json"

trades = []
with open(Path(__file__).parent.parent / "data" / "meanrev_trades.txt") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        m_coin = re.search(r'coin=(\w+)', line)
        m_entry = re.search(r'entry=([\d.]+)', line)
        m_size = re.search(r'size=([\d.]+)', line)
        m_pattern = re.search(r'pattern=(\w+)', line)
        m_outcome = re.search(r'outcome=(\w+)', line)
        m_ts = re.search(r'^([\d-]+ [\d:]+)', line)
        m_market = re.search(r'market=([\w-]+)', line)
        if not m_coin:
            continue
        coin = m_coin.group(1)
        entry = float(m_entry.group(1)) if m_entry else 0
        size = float(m_size.group(1)) if m_size else 5
        pattern = m_pattern.group(1) if m_pattern else '?'
        outcome = m_outcome.group(1) if m_outcome else '?'
        ts = m_ts.group(1) if m_ts else '?'
        market = m_market.group(1) if m_market else '?'

        if outcome == 'WIN':
            pnl = (1.0 - entry) * size
        elif outcome == 'LOSS':
            pnl = -entry * size
        else:
            pnl = 0  # PENDING

        trades.append({
            'ts': ts, 'coin': coin, 'entry': entry, 'size': size,
            'pattern': pattern, 'outcome': outcome, 'pnl': pnl,
            'market': market,
        })

# Resolve PENDING trades via Gamma
cache = {}
if CACHE_FILE.exists():
    with open(CACHE_FILE) as f:
        cache = json.load(f)

for t in trades:
    if t['outcome'] == 'PENDING':
        slug = t['market']
        if slug in cache and cache[slug] in ('UP', 'DOWN'):
            resolved = cache[slug]
        else:
            url = f"https://gamma-api.polymarket.com/markets/slug/{slug}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                prices = json.loads(data.get("outcomePrices", "[]"))
                outcomes_list = json.loads(data.get("outcomes", "[]"))
                resolved = None
                for i, p in enumerate(prices):
                    if p == "1":
                        resolved = outcomes_list[i].upper()
                cache[slug] = resolved if resolved else "UNKNOWN"
                time.sleep(0.1)
            except Exception:
                resolved = None

        if resolved == 'DOWN':
            t['outcome'] = 'WIN'
            t['pnl'] = (1.0 - t['entry']) * t['size']
        elif resolved == 'UP':
            t['outcome'] = 'LOSS'
            t['pnl'] = -t['entry'] * t['size']

with open(CACHE_FILE, "w") as f:
    json.dump(cache, f, indent=2)

# --- Summary ---
total = len(trades)
wins = sum(1 for t in trades if t['outcome'] == 'WIN')
losses = sum(1 for t in trades if t['outcome'] == 'LOSS')
pending = sum(1 for t in trades if t['outcome'] == 'PENDING')
total_pnl = sum(t['pnl'] for t in trades)

print(f"TOTAL: {total} trades ({wins}W / {losses}L / {pending}P)")
if wins + losses > 0:
    print(f"WR: {wins/(wins+losses)*100:.1f}%")
print(f"PnL: ${total_pnl:+.2f}")
print()

# Equity curve
cum = 0
peak = 0
max_dd = 0
for t in trades:
    cum += t['pnl']
    if cum > peak:
        peak = cum
    dd = peak - cum
    if dd > max_dd:
        max_dd = dd
print(f"Peak: ${peak:+.2f} | Max DD: ${max_dd:.2f} | Final: ${cum:+.2f}")
print()

# Last 25 trades
print("LAST 25 TRADES:")
cum2 = sum(t['pnl'] for t in trades[:-25])
for t in trades[-25:]:
    cum2 += t['pnl']
    print(f"  {t['ts']}  {t['coin']:4} {t['pattern']:6} "
          f"{t['entry']:5.3f} {t['outcome']:7} ${t['pnl']:+7.2f}  cum=${cum2:+7.2f}")
print()

# Per coin
print("PER COIN:")
for coin in ['BTC', 'ETH', 'SOL', 'XRP']:
    ct = [t for t in trades if t['coin'] == coin and t['outcome'] in ('WIN', 'LOSS')]
    if not ct:
        continue
    w = sum(1 for t in ct if t['outcome'] == 'WIN')
    p = sum(t['pnl'] for t in ct)
    print(f"  {coin}: {len(ct)}tr {w}W/{len(ct)-w}L ({w/len(ct)*100:.0f}%WR) ${p:+.2f}")
print()

# The bloodbath
for label, cutoff in [("07:30 Mar 4", "2026-03-04 07:30"), ("05:00 Mar 4", "2026-03-04 05:00")]:
    late = [t for t in trades if t['ts'] >= cutoff and t['outcome'] in ('WIN', 'LOSS')]
    if late:
        w = sum(1 for t in late if t['outcome'] == 'WIN')
        l = sum(1 for t in late if t['outcome'] == 'LOSS')
        p = sum(t['pnl'] for t in late)
        print(f"DESDE {label}: {len(late)}tr {w}W/{l}L ${p:+.2f}")

# --- RSI FILTER SIMULATION ---
# Check which trades would have been blocked by RSI(7) > 60 on 5m
print("\n" + "=" * 70)
print("RSI FILTER SIMULATION")
print("=" * 70)

# Get timestamps and fetch RSI
import sys
sys.path.insert(0, str(Path(__file__).parent))

# We need Binance klines for these timestamps
from backtest import compute_rsi, fetch_klines
import pandas as pd
import numpy as np

# Get time range
all_ts = []
for t in trades:
    m = re.search(r'(\d+)$', t['market'])
    if m:
        all_ts.append(int(m.group(1)))

if all_ts:
    ts_min = min(all_ts)
    ts_max = max(all_ts)
    warmup = 200 * 300
    start_ms = int((ts_min - warmup) * 1000)
    end_ms = int((ts_max + 600) * 1000)

    kl = fetch_klines("BTCUSDT", "5m", start_ms, end_ms)
    if not kl.empty:
        kl = kl.sort_values("open_time").reset_index(drop=True)
        close = kl["close"].values
        kl["rsi_7"] = compute_rsi(close, 7)
        kl["rsi_14"] = compute_rsi(close, 14)
        kl["ts_s"] = (kl["open_time_ms"] / 1000).astype(int)

        # Build lookup
        rsi_lookup = {}
        for _, row in kl.iterrows():
            rsi_lookup[int(row["ts_s"])] = (row["rsi_7"], row["rsi_14"])

        def get_rsi(ts):
            for offset in [0, -300, 300, -600, 600]:
                r = rsi_lookup.get(ts + offset)
                if r and not np.isnan(r[0]):
                    return r
            return (np.nan, np.nan)

        # Annotate each trade with RSI
        for t in trades:
            m = re.search(r'(\d+)$', t['market'])
            if m:
                ts = int(m.group(1))
                r7, r14 = get_rsi(ts)
                t['rsi7'] = r7
                t['rsi14'] = r14
            else:
                t['rsi7'] = np.nan
                t['rsi14'] = np.nan

        # Simulate filters
        for filter_name, key, thresh in [
            ("RSI(7) skip>60", "rsi7", 60),
            ("RSI(7) skip>65", "rsi7", 65),
            ("RSI(14) skip>60", "rsi14", 60),
        ]:
            resolved = [t for t in trades if t['outcome'] in ('WIN', 'LOSS')]
            passed = [t for t in resolved if not np.isnan(t[key]) and t[key] < thresh]
            blocked = [t for t in resolved if not np.isnan(t[key]) and t[key] >= thresh]

            pw = sum(1 for t in passed if t['outcome'] == 'WIN')
            pp = sum(t['pnl'] for t in passed)
            bw = sum(1 for t in blocked if t['outcome'] == 'WIN')
            bp = sum(t['pnl'] for t in blocked)

            print(f"\n  {filter_name}:")
            print(f"    Kept:    {len(passed)}tr {pw}W/{len(passed)-pw}L "
                  f"({pw/len(passed)*100:.0f}%WR) ${pp:+.2f}" if passed else f"    Kept: 0")
            print(f"    Blocked: {len(blocked)}tr {bw}W/{len(blocked)-bw}L "
                  f"({bw/len(blocked)*100:.0f}%WR) ${bp:+.2f}" if blocked else f"    Blocked: 0")
            print(f"    Saved:   ${-bp:+.2f} by blocking losers" if bp < 0 else
                  f"    Lost:    ${-bp:+.2f} by blocking winners")

        # Show the last 15 trades with RSI values
        print(f"\nLAST 15 TRADES WITH RSI:")
        print(f"  {'Time':<20} {'Coin':4} {'Pat':6} {'Result':7} {'PnL':>8} {'RSI7':>5} {'RSI14':>5} {'R7>60':>6} {'R14>60':>6}")
        for t in trades[-15:]:
            r7 = t.get('rsi7', np.nan)
            r14 = t.get('rsi14', np.nan)
            r7s = f"{r7:.0f}" if not np.isnan(r7) else "?"
            r14s = f"{r14:.0f}" if not np.isnan(r14) else "?"
            skip7 = "SKIP" if not np.isnan(r7) and r7 >= 60 else "ok"
            skip14 = "SKIP" if not np.isnan(r14) and r14 >= 60 else "ok"
            print(f"  {t['ts']:<20} {t['coin']:4} {t['pattern']:6} "
                  f"{t['outcome']:7} ${t['pnl']:+7.2f} {r7s:>5} {r14s:>5} {skip7:>6} {skip14:>6}")
