
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys

HIST_DB = "kalshi_hist.db"
LIVE_DB = "kalshi_15m.db"
ORDER_SIZE = 4


def fee_c(price_cents, count=ORDER_SIZE):
    p = max(0.001, min(0.999, price_cents / 100.0))
    return math.ceil(round(0.07 * count * p * (1 - p) * 100, 9)) / 100.0 \
        / count * 100.0


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def summarise(label, obs, detail=""):
    n = len(obs)
    print("\n" + "=" * 76)
    print(label)
    if detail:
        print(detail)
    print("=" * 76)
    if n < 2:
        print("\n  not enough observations")
        return None

    wins = sum(1 for _p, w, _d in obs if w)
    losses = n - wins
    avg_px = sum(p for p, _w, _d in obs) / n
    fee = fee_c(avg_px)
    breakeven = avg_px + fee
    wr = 100.0 * wins / n
    lo, hi = wilson(wins, n)

    pnl = [(100.0 if w else 0.0) - p - fee_c(p) for p, w, _d in obs]
    m = sum(pnl) / n
    var = sum((x - m) ** 2 for x in pnl) / (n - 1)

    p_bar = (sum(px for px, _w, _d in obs) / n) / 100.0
    p_bar = min(max(p_bar, 0.001), 0.999)
    floor_var = p_bar * (1.0 - p_bar) * 10000.0
    raw_se = math.sqrt(var / n)
    var = max(var, floor_var)
    se = math.sqrt(var / n)
    if raw_se > 1e-9 and se / raw_se > 1.5:
        print(f"\n  !! variance floor applied: SE {raw_se:.3f} -> {se:.3f} "
              f"({se/raw_se:.0f}x)")
        print(f"     {wins}/{n} won with {losses} losses — the raw variance")
        print("     reflects a streak, not certainty. Trust the floored value.")

    print(f"\n  TRADES        {n:,}")
    print(f"  WON           {wins:,}")
    print(f"  LOST          {losses:,}")
    print(f"\n  win rate      {wr:.2f}%   95% CI [{100*lo:.2f}, {100*hi:.2f}]%")
    print(f"  mean entry    {avg_px:.2f}c")
    print(f"  fee           {fee:.2f}c per contract at {ORDER_SIZE} contracts")
    print(f"  BREAK-EVEN    {breakeven:.2f}%   <- you must beat THIS, "
          f"not {avg_px:.0f}%")

    gap = wr - breakeven
    print(f"\n  win rate minus break-even:  {gap:+.2f} points")
    print(f"  net P&L per contract:       {m:+.3f}c")
    print(f"  95% CI                      [{m-1.96*se:+.3f}, {m+1.96*se:+.3f}]c")
    print(f"  t                           {m/se if se else 0:+.2f}")

    per_trade = m / 100.0 * ORDER_SIZE
    print(f"\n  at {ORDER_SIZE} contracts: {per_trade:+.3f} dollars per trade")
    print(f"  over {n:,} trades:  {per_trade*n:+.2f} dollars")

    need = int((1.96 * math.sqrt(n) * se / 1.0) ** 2) if se else 0
    print(f"\n  detectable effect at this n: {2*1.96*se:.2f}c")
    print(f"  n needed to resolve 1c:      {need:,}")

    if abs(m) < 1.96 * se:
        print("\n  VERDICT: consistent with fair pricing. Not distinguishable"
              " from zero.")
    elif m < 0:
        print("\n  VERDICT: significantly NEGATIVE. This loses money.")
    else:
        print("\n  VERDICT: significantly positive — check depth before"
              " believing it.")
    return m


def from_candles(db, series, lo, hi):
    if not os.path.exists(db):
        print(f"\n  {db} not found — skipping candle analysis")
        return []
    conn = sqlite3.connect(db, timeout=10)
    rows = conn.execute("""
        SELECT c.ticker, c.secs_left, c.bid_close, c.ask_close, s.result
        FROM candles c
        JOIN hist_settlements s ON s.ticker = c.ticker
        WHERE s.result IN ('yes','no')
          AND (? IS NULL OR c.series = ?)
          AND c.secs_left BETWEEN 30 AND 90
          AND c.ask_close > 0 AND c.ask_close < 100
          AND c.bid_close > 0
        ORDER BY c.ticker, ABS(c.secs_left - 60)
    """, (series, series)).fetchall()

    seen, obs = set(), []
    for tk, _s, bid, ask, res in rows:
        if tk in seen:
            continue
        seen.add(tk)
        no_ask = 100.0 - bid
        if lo <= ask <= hi:
            side, px = "yes", ask
        elif lo <= no_ask <= hi:
            side, px = "no", no_ask
        else:
            continue
        obs.append((px, res == side, None))
    return obs


def from_snapshots(db, series, lo, hi, t_lo, t_hi):
    if not os.path.exists(db):
        print(f"\n  {db} not found — skipping snapshot analysis")
        return []
    conn = sqlite3.connect(db, timeout=10)
    try:
        rows = conn.execute("""
            SELECT s.ticker, s.secs_left, s.yes_bid, s.yes_ask,
                   s.yes_bid_size, s.yes_ask_size, t.result
            FROM snapshots s
            JOIN settlements t ON t.ticker = s.ticker
            WHERE t.result IN ('yes','no')
              AND (? IS NULL OR s.series = ?)
              AND s.secs_left BETWEEN ? AND ?
              AND s.yes_bid > 0 AND s.yes_ask > 0 AND s.yes_ask < 100
            ORDER BY s.ticker, s.secs_left DESC
        """, (series, series, t_lo, t_hi)).fetchall()
    except sqlite3.OperationalError as e:
        print(f"\n  snapshot query failed: {e}")
        return []

    seen, obs = set(), []
    for tk, _s, yb, ya, ybs, yas, res in rows:
        if tk in seen:
            continue
        seen.add(tk)
        no_ask = 100.0 - yb
        if lo <= ya <= hi:
            side, px, dep = "yes", ya, (yas or 0)
        elif lo <= no_ask <= hi:
            side, px, dep = "no", no_ask, (ybs or 0)
        else:
            continue
        obs.append((px, res == side, dep))
    return obs


def by_depth(obs, thresholds=(0, 100, 1000)):
    have = [o for o in obs if o[2] is not None]
    if len(have) < 40:
        return
    print("\n" + "-" * 76)
    print("  SPLIT BY RESTING DEPTH (the only column that decides tradeability)")
    print("-" * 76)
    print(f"\n  {'depth':>16} {'n':>7} {'won':>7} {'win%':>8} {'net':>9}")
    print("  " + "-" * 52)
    bands = [(0, 100, "thin  <100"), (100, 1000, "med   100-1k"),
             (1000, 10 ** 9, "deep  1000+")]
    for a, b, name in bands:
        sub = [o for o in have if a <= o[2] < b]
        if len(sub) < 10:
            continue
        w = sum(1 for _p, won, _d in sub if won)
        pnl = [(100.0 if won else 0.0) - p - fee_c(p) for p, won, _d in sub]
        m = sum(pnl) / len(pnl)
        print(f"  {name:>16} {len(sub):>7,} {w:>7,} "
              f"{100.0*w/len(sub):>7.1f}% {m:>+8.2f}c")
    print("\n  If the edge lives only in the thin row, it was never available.")


def self_test():
    print("SELF-TEST\n")
    assert abs(fee_c(93.0, 4) - 0.50) < 1e-9, fee_c(93.0, 4)
    print(f"  [ok] fee at 93c, 4 contracts = {fee_c(93.0,4):.2f}c")
    print(f"       break-even win rate     = {93.0 + fee_c(93.0,4):.2f}%")

    assert fee_c(92.0, 4) > fee_c(93.0, 4), "expected the ceiling quirk"
    print(f"  [ok] non-monotonic: 92c costs {fee_c(92.0,4):.2f}c but "
          f"93c costs {fee_c(93.0,4):.2f}c")
    print("       (the ceiling is applied to the ORDER, so a worse price")
    print("        can round down into a cheaper bracket)")

    import random
    rng = random.Random(4)
    px = 93.0
    be = (px + fee_c(px)) / 100.0
    obs = [(px, rng.random() < be, None) for _ in range(200000)]
    pnl = [(100.0 if w else 0.0) - p - fee_c(p) for p, w, _d in obs]
    m = sum(pnl) / len(pnl)
    sd = math.sqrt(sum((x - m) ** 2 for x in pnl) / (len(pnl) - 1))
    se = sd / math.sqrt(len(pnl))
    assert abs(m) < 4 * se, \
        f"break-even rate should give ~0, got {m:+.4f} (4*SE = {4*se:.4f})"
    print(f"  [ok] winning exactly {100*be:.2f}% yields {m:+.3f}c "
          f"± {1.96*se:.3f} — zero, as required")

    obs2 = [(px, rng.random() < 0.93, None) for _ in range(200000)]
    pnl2 = [(100.0 if w else 0.0) - p - fee_c(p) for p, w, _d in obs2]
    m2 = sum(pnl2) / len(pnl2)
    assert m2 < -0.5, m2
    print(f"  [ok] winning 93.00% yields {m2:+.3f}c — a LOSS, "
          f"despite a 93% win rate")

    lo, hi = wilson(93, 100)
    print(f"  [ok] Wilson CI for 93/100 = [{100*lo:.1f}, {100*hi:.1f}]%")
    print("\nALL PASS\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hist-db", default=HIST_DB)
    ap.add_argument("--live-db", default=LIVE_DB)
    ap.add_argument("--series", default="KXBTC15M",
                    help="use 'all' to pool every series")
    ap.add_argument("--lo", type=float, default=90.0)
    ap.add_argument("--hi", type=float, default=96.0)
    ap.add_argument("--t-lo", type=float, default=30.0)
    ap.add_argument("--t-hi", type=float, default=60.0)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        self_test()
        sys.exit(0)

    series = None if a.series.lower() == "all" else a.series
    tag = a.series if series else "ALL SERIES"

    c = from_candles(a.hist_db, series, a.lo, a.hi)
    summarise(f"CANDLES — {tag}, entry at T-60s, {a.lo:.0f}-{a.hi:.0f}c",
              c,
              "1-minute bars: T-60 is the ONLY entry point they can express.")

    s = from_snapshots(a.live_db, series, a.lo, a.hi, a.t_lo, a.t_hi)
    summarise(f"SNAPSHOTS — {tag}, {a.t_lo:.0f}-{a.t_hi:.0f}s, "
              f"{a.lo:.0f}-{a.hi:.0f}c",
              s,
              "15-second resolution: the TRUE window you asked about.")
    by_depth(s)
