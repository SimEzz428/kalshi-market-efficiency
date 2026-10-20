
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict

DB = "kalshi_15m.db"
LO, HI = 90.0, 96.0


def se_binom(diffs, prices):
    n = len(diffs)
    if n < 2:
        return 1.0
    m = sum(diffs) / n
    sv = sum((d - m) ** 2 for d in diffs) / (n - 1)
    p = sum(prices) / n / 100.0
    return math.sqrt(max(sv, max(p * (1 - p), 1e-6)) / n)


def hdr(t):
    print(f"\n{'='*76}\n{t}\n{'='*76}")


def structure(conn):
    hdr("1. MARKET STRUCTURE BY COIN  (~39k snapshots each — well powered)")

    rows = conn.execute(
        """SELECT series, yes_bid, yes_ask, yes_bid_size, yes_ask_size, secs_left
           FROM snapshots
           WHERE yes_bid IS NOT NULL AND yes_ask IS NOT NULL
             AND yes_bid > 0 AND yes_ask < 100
             AND secs_left BETWEEN 30 AND 900""").fetchall()

    per = defaultdict(lambda: {"spr": [], "dep": [], "n": 0, "quoted": 0})
    for series, yb, ya, bs, asz, secs in rows:
        d = per[series]
        d["n"] += 1
        d["spr"].append(ya - yb)
        d["dep"].append(min(bs or 0, asz or 0))

    def med(v):
        if not v:
            return 0.0
        v = sorted(v)
        return v[len(v) // 2]

    print(f"\n  {'coin':>11} {'snapshots':>11} {'med spread':>12} {'med depth':>11} "
          f"{'thin (<100)':>12} {'deep (1000+)':>13}")
    print("  " + "-" * 74)

    profile = {}
    for s in sorted(per, key=lambda k: med(per[k]["spr"])):
        d = per[s]
        thin = sum(1 for x in d["dep"] if x < 100) / d["n"]
        deep = sum(1 for x in d["dep"] if x >= 1000) / d["n"]
        profile[s] = {"spread": med(d["spr"]), "depth": med(d["dep"]),
                      "thin": thin, "deep": deep, "n": d["n"]}
        print(f"  {s:>11} {d['n']:>11,} {med(d['spr']):>11.2f}c "
              f"{med(d['dep']):>11,.0f} {thin:>11.0%} {deep:>12.0%}")

    print("""
  READ: tight spread + deep book = heavily market-made = efficient.
  Wide spread + thin book = neglected = where mispricing is likelier to hide,
  and also where you are least able to trade it.""")
    return profile


def edge_by_coin(conn):
    hdr("2. EDGE BY COIN  (~124 settlements each — UNDERPOWERED, read carefully)")

    rows = conn.execute(
        """SELECT s.series, s.ticker, s.yes_bid, s.yes_ask, s.yes_bid_size,
                  s.yes_ask_size, t.result
           FROM snapshots s JOIN settlements t ON t.ticker = s.ticker
           WHERE s.secs_left BETWEEN 180 AND 360
             AND s.yes_bid > 0 AND s.yes_ask < 100
             AND t.result IN ('yes','no')""").fetchall()

    per = defaultdict(dict)
    for series, ticker, yb, ya, bs, asz, result in rows:
        for side in ("YES", "NO"):
            price = yb if side == "YES" else 100 - ya
            size = (bs if side == "YES" else asz) or 0
            won = (result == "yes") if side == "YES" else (result == "no")
            if LO <= price <= HI:
                per[series][(ticker, side)] = (
                    (1.0 if won else 0.0) - price / 100.0, price, size)

    print(f"\n  {'coin':>11} {'n':>6} {'mean edge':>11} {'SE':>7} "
          f"{'95% CI':>20} {'detectable':>11}")
    print("  " + "-" * 72)
    for s in sorted(per):
        v = list(per[s].values())
        n = len(v)
        if n < 20:
            print(f"  {s:>11} {n:>6}  need 20+")
            continue
        d = [x[0] for x in v]
        pr = [x[1] for x in v]
        m = sum(d) / n * 100
        se = se_binom(d, pr) * 100
        print(f"  {s:>11} {n:>6} {m:>+10.2f}pt {se:>6.2f} "
              f"[{m-1.96*se:>+6.2f},{m+1.96*se:>+6.2f}]pt {1.96*se:>10.2f}pt")

    print("""
  The 'detectable' column is the smallest edge each coin's sample could prove.
  All of them are 4-6pt. The effect we are hunting is 1-2pt. So NONE of these
  numbers can resolve the question — they are shown for completeness, not
  for decision-making. Treat any coin that looks good here as noise.""")


def depth_controlled_gradient(conn):
    hdr("3. DEPTH-CONTROLLED TIME GRADIENT  (the confound test)")

    rows = conn.execute(
        """SELECT s.ticker, s.secs_left, s.yes_bid, s.yes_ask,
                  s.yes_bid_size, s.yes_ask_size, t.result
           FROM snapshots s JOIN settlements t ON t.ticker = s.ticker
           WHERE s.secs_left BETWEEN 20 AND 600
             AND s.yes_bid > 0 AND s.yes_ask < 100
             AND t.result IN ('yes','no')""").fetchall()

    obs = {}
    for ticker, secs, yb, ya, bs, asz, result in rows:
        for side in ("YES", "NO"):
            price = yb if side == "YES" else 100 - ya
            size = (bs if side == "YES" else asz) or 0
            won = (result == "yes") if side == "YES" else (result == "no")
            if LO <= price <= HI:
                obs[(ticker, side, int(secs))] = (
                    secs, size, price, (1.0 if won else 0.0) - price / 100.0)

    windows = [(20, 60), (60, 120), (120, 240), (240, 600)]
    depths = [(0, 100, "thin <100"), (100, 1000, "medium"), (1000, 1e12, "deep 1000+")]

    print(f"\n  Edge (pt) in each time x depth cell. n in brackets.\n")
    print(f"  {'depth':>13} " + " ".join(f"{f'{a}-{b}s':>15}" for a, b, in
                                         [(w[0], w[1]) for w in windows]))
    print("  " + "-" * 76)

    grid = {}
    for dlo, dhi, dlabel in depths:
        cells = []
        for tlo, thi in windows:
            sel = [v for v in obs.values()
                   if tlo <= v[0] < thi and dlo <= v[1] < dhi]
            n = len(sel)
            if n < 25:
                cells.append(f"{'n='+str(n):>15}")
                grid[(dlabel, tlo)] = None
                continue
            d = [x[3] for x in sel]
            pr = [x[2] for x in sel]
            m = sum(d) / n * 100
            se = se_binom(d, pr) * 100
            grid[(dlabel, tlo)] = (m, se, n)
            cells.append(f"{m:>+8.2f} [{n:>4}]")
        print(f"  {dlabel:>13} " + " ".join(cells))

    filled = sum(1 for k in grid if grid[k] is not None)
    rows_ok = 0
    for _, _, dlabel in depths:
        cells = [grid.get((dlabel, t)) for t, _ in windows]
        if sum(1 for c in cells if c) >= 2:
            rows_ok += 1

    print(f"\n  IDENTIFIABILITY: {filled}/{len(depths)*len(windows)} cells have data; "
          f"{rows_ok}/{len(depths)} depth rows span 2+ time windows")
    if rows_ok < 2:
        print("""
  *** THE QUESTION IS NOT ANSWERABLE WITH THIS DATA ***

    Depth and time do not vary independently — thin quotes occur only near
    expiry, deep quotes only far from it. With no overlap, a depth effect and
    a time effect produce IDENTICAL grids. No amount of extra data in the same
    pattern will fix this; you would need observations that break the
    correlation (deep books near expiry, or thin books far from it).

    This is an identifiability problem, not a sample-size problem. Recognising
    the difference matters: more data solves one and never solves the other.""")
        return

    print("""
  HOW TO READ THIS GRID — it is the whole question:

    If the edge rises left-to-right WITHIN EACH ROW, time matters even at
    constant depth -> the averaging effect is real.

    If the edge only rises going UP the rows (thin > medium > deep) and is
    flat across each row, then DEPTH is the driver and the apparent time
    gradient was a confound. The strategy is dead.""")

    for dlabel in ("medium", "deep 1000+"):
        pts = [(t, grid[(dlabel, t)]) for t, _ in windows if grid.get((dlabel, t))]
        if len(pts) >= 3:
            xs = [p[0] for p in pts]
            ys = [p[1][0] for p in pts]
            ws = [1 / p[1][1] ** 2 for p in pts]
            sw = sum(ws)
            mx = sum(w * x for w, x in zip(ws, xs)) / sw
            my = sum(w * y for w, y in zip(ws, ys)) / sw
            sxx = sum(w * (x - mx) ** 2 for w, x in zip(ws, xs))
            sxy = sum(w * (x - mx) * (y - my) for w, x, y in zip(ws, xs, ys))
            if sxx > 0:
                slope = sxy / sxx
                direction = ("edge GROWS toward expiry" if slope < 0
                             else "edge SHRINKS toward expiry")
                print(f"  within '{dlabel}': slope vs time = {slope*1000:+.3f} "
                      f"pt per 1000s -> {direction}")


if __name__ == "__main__":
    conn = sqlite3.connect(DB)
    structure(conn)
    edge_by_coin(conn)
    depth_controlled_gradient(conn)
    print()
