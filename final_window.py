
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys

from honest import breakeven, fee_c, honest_stats, wilson

DB = "kalshi_15m.db"
SERIES = "KXBTC15M"


def load(db, series, t_lo, t_hi, lo, hi, min_depth):
    if not os.path.exists(db):
        print(f"  {db} not found")
        return []
    conn = sqlite3.connect(db, timeout=10)
    rows = conn.execute("""
        SELECT s.ticker, s.secs_left, s.yes_bid, s.yes_ask,
               s.yes_bid_size, s.yes_ask_size, t.result
        FROM snapshots s
        JOIN settlements t ON t.ticker = s.ticker
        WHERE s.series = ?
          AND t.result IN ('yes','no')
          AND s.secs_left BETWEEN -20 AND 400
          AND s.yes_bid > 0 AND s.yes_ask > 0 AND s.yes_ask < 100
        ORDER BY s.ticker, s.secs_left DESC
    """, (series,)).fetchall()

    by: dict = {}
    for r in rows:
        by.setdefault(r[0], []).append(r)

    out = []
    for tk, path in by.items():
        entry = None
        for r in path:
            _t, secs, yb, ya, ybs, yas, res = r
            if not (t_lo <= secs <= t_hi):
                continue
            no_ask = 100.0 - yb
            if lo <= ya <= hi:
                side, px, dep = "yes", ya, (yas or 0)
            elif lo <= no_ask <= hi:
                side, px, dep = "no", no_ask, (ybs or 0)
            else:
                continue
            if dep < min_depth:
                continue
            entry = (secs, side, px, dep, res)
            break
        if entry is None:
            continue
        secs, side, px, dep, res = entry

        fwd = []
        for r in path:
            if r[1] >= secs:
                continue
            bid = r[2] if side == "yes" else (100.0 - r[3])
            if bid is not None and 0 <= bid <= 100:
                fwd.append((r[1], bid))
        out.append((tk, side, px, secs, dep, res, fwd))
    return out


def drawdown_report(entries):
    print("\n" + "=" * 78)
    print("DRAWDOWN — how far does a 90-96c position actually drop?")
    print("=" * 78)
    if not entries:
        print("\n  no entries")
        return

    lows = []
    for _tk, _s, px, _sec, _d, _r, fwd in entries:
        lows.append(min([b for _s2, b in fwd], default=px))

    n = len(entries)
    print(f"\n  {n} entries. Lowest price reached before settlement:\n")
    bands = [(0, 20), (20, 40), (40, 60), (60, 70), (70, 80), (80, 90),
             (90, 101)]
    for a, b in bands:
        c = sum(1 for x in lows if a <= x < b)
        bar = "#" * int(40.0 * c / max(n, 1))
        print(f"  {a:>3}-{b - 1:<3}c  {c:>4} ({100.0 * c / n:>5.1f}%)  {bar}")

    print("\n  CUMULATIVE — fraction that EVER traded at or below:\n")
    for lvl in (89, 85, 80, 70, 60, 50, 40):
        c = sum(1 for x in lows if x <= lvl)
        print(f"    <= {lvl:>3}c   {c:>4} of {n}  ({100.0 * c / n:>5.1f}%)")

    hit60 = sum(1 for x in lows if x <= 60)
    print(f"\n  A 60c STOP WOULD FIRE {hit60} TIME(S) IN {n} ENTRIES "
          f"({100.0 * hit60 / n:.1f}%).")
    if hit60 == 0:
        print("""
  It never fires. The rule is INERT, not wrong — it changes nothing about
  any trade in this sample, so it cannot improve or worsen the result.
  Tuning the level is the only way to make it do anything at all.""")
    elif hit60 < 5:
        print(f"""
  It fires {hit60} time(s). Any P&L difference rests on {hit60} observation(s)
  and means nothing statistically, whatever sign it carries.""")


def stop_report(entries, stops):
    print("\n" + "=" * 78)
    print("STOP LOSS vs HOLD — paired, same markets")
    print("=" * 78)
    if len(entries) < 5:
        print("\n  too few entries to test")
        return

    hold, pxs = [], []
    for _tk, side, px, _s, _d, res, _f in entries:
        hold.append((100.0 if res == side else 0.0) - px - fee_c(px))
        pxs.append(px)
    h = honest_stats(hold, pxs)
    n = h["n"]
    wins = sum(1 for _t, s, _p, _sec, _d, r, _f in entries if r == s)
    avg = sum(pxs) / n
    lo, hi = wilson(wins, n)

    print(f"\n  {n} entries   won {wins}  lost {n - wins}   "
          f"({100.0 * wins / n:.1f}%)")
    print(f"  win rate 95% CI  [{100 * lo:.1f}, {100 * hi:.1f}]%")
    print(f"  mean entry {avg:.2f}c   break-even {breakeven(avg):.2f}%")
    print(f"\n  HOLD: {h['mean']:+.3f}c  (SE {h['se']:.3f}, t {h['t']:+.2f})")
    if h["floored"]:
        print(f"        variance floor applied ({h['inflation']:.0f}x) — "
              f"a streak, not certainty")

    print(f"\n  {'stop':>6} {'fired':>7} {'won anyway':>12} {'fill':>8} "
          f"{'vs hold':>10} {'SE':>7} {'t':>7} {'n_diff':>7}")
    print("  " + "-" * 68)
    for lvl in stops:
        diffs, fired, recov, fills = [], 0, 0, []
        for i, (_tk, side, px, _s, _d, res, fwd) in enumerate(entries):
            hit = next((b for _s2, b in fwd if b < lvl), None)
            if hit is None:
                diffs.append(0.0)
                continue
            fired += 1
            fills.append(hit)
            if res == side:
                recov += 1
            diffs.append((hit - fee_c(max(hit, 0.5)) - px - fee_c(px))
                         - hold[i])
        d = honest_stats(diffs)
        nz = sum(1 for x in diffs if abs(x) > 1e-9)
        mf = sum(fills) / len(fills) if fills else 0.0
        rec = f"{100.0 * recov / fired:.0f}%" if fired else "-"
        print(f"  {lvl:>5.0f}c {fired:>7} {rec:>12} "
              f"{(f'{mf:.1f}c' if fills else '-'):>8} "
              f"{d['mean']:>+9.3f}c {d['se']:>7.3f} {d['t']:>+7.2f} {nz:>7}")

    print("\n  'fill' is the price actually received — a stop is a market")
    print("  order, so it fills where the book is, not where you set it.")


def movement_report(entries):
    print("\n" + "=" * 78)
    print("PRICE MOVEMENT inside the final 60 seconds")
    print("=" * 78)
    if not entries:
        return
    moves = []
    for _tk, _s, px, _sec, _d, _r, fwd in entries:
        if not fwd:
            continue
        last = fwd[-1][1]
        moves.append(last - px)
    if not moves:
        return
    moves.sort()
    n = len(moves)

    def pct(q):
        return moves[min(n - 1, int(q * n))]

    print(f"\n  {n} entries. Change from entry price to the last quote:\n")
    print(f"    5th percentile   {pct(0.05):>+7.1f}c")
    print(f"    25th             {pct(0.25):>+7.1f}c")
    print(f"    median           {pct(0.50):>+7.1f}c")
    print(f"    75th             {pct(0.75):>+7.1f}c")
    print(f"    95th             {pct(0.95):>+7.1f}c")
    up = sum(1 for m in moves if m > 0)
    flat = sum(1 for m in moves if abs(m) < 0.5)
    print(f"\n    rose {100.0*up/n:.0f}%   "
          f"barely moved (<0.5c) {100.0*flat/n:.0f}%   "
          f"fell {100.0*(n-up)/n:.0f}%")
    print("\n  Prices this close to expiry mostly converge to 0 or 100.")
    print("  A large DROP is rare because the position is already nearly")
    print("  decided — which is exactly why a deep stop rarely triggers.")


def self_test():
    print("SELF-TEST\n")
    entries = [("T", "yes", 95.0, 45.0, 500, "yes",
                [(30.0, 96.0), (15.0, 97.0), (0.0, 99.0)])] * 40
    hold = [(100.0 - 95.0 - fee_c(95.0))] * 40
    diffs = []
    for i, (_t, side, px, _s, _d, res, fwd) in enumerate(entries):
        hit = next((b for _s2, b in fwd if b < 60.0), None)
        diffs.append(0.0 if hit is None else 1.0)
    assert all(d == 0.0 for d in diffs), "stop fired on a rising path"
    print("  [ok] a 60c stop never fires on paths that only rise")

    e2 = [("T", "yes", 95.0, 45.0, 500, "no", [(30.0, 70.0), (15.0, 40.0)])]
    hit = next((b for _s, b in e2[0][6] if b < 60.0), None)
    assert hit == 40.0, hit
    print(f"  [ok] fires on a collapse, filling at {hit:.0f}c "
          f"(not at the 60c level)")

    r = honest_stats([1.0] * 35, [93.4] * 35)
    assert r["floored"] and abs(r["t"]) < 2
    print(f"  [ok] variance floor imported from honest.py "
          f"(t={r['t']:+.2f} on a 35-win streak)")
    print("\nALL PASS\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--series", default=SERIES)
    ap.add_argument("--t-lo", type=float, default=30.0)
    ap.add_argument("--t-hi", type=float, default=60.0)
    ap.add_argument("--lo", type=float, default=90.0)
    ap.add_argument("--hi", type=float, default=96.0)
    ap.add_argument("--min-depth", type=float, default=0.0)
    ap.add_argument("--stops", type=float, nargs="*",
                    default=[89, 85, 80, 70, 60, 50])
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        self_test()
        sys.exit(0)

    e = load(a.db, a.series, a.t_lo, a.t_hi, a.lo, a.hi, a.min_depth)
    print("\n" + "=" * 78)
    print(f"{a.series}   entry {a.t_lo:.0f}-{a.t_hi:.0f}s   "
          f"{a.lo:.0f}-{a.hi:.0f}c   min depth {a.min_depth:.0f}")
    print("=" * 78)
    print(f"\n  {len(e)} qualifying entries")
    if e:
        print(f"  sides: {sum(1 for x in e if x[1]=='yes')} YES / "
              f"{sum(1 for x in e if x[1]=='no')} NO")
    drawdown_report(e)
    movement_report(e)
    stop_report(e, a.stops)
