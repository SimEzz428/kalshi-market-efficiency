
from __future__ import annotations

import argparse
import math
import sqlite3
import sys

DB = "kalshi_hist.db"

ENTRY_TIMES = [60, 120, 180, 240, 300, 420, 600]
PRICE_BANDS = [(1, 10), (10, 25), (25, 40), (40, 60),
               (60, 75), (75, 90), (90, 96), (96, 99)]
MIN_N = 100


def fee_c(price_cents: float) -> float:
    p = max(0.001, min(0.999, price_cents / 100.0))
    return math.ceil(round(0.07 * p * (1 - p) * 100, 9)) / 100.0 * 100.0


def stats(v, prices=None):
    n = len(v)
    if n < 2:
        return (0.0, 0.0, 0.0, n)
    m = sum(v) / n
    var = sum((x - m) ** 2 for x in v) / (n - 1)

    if prices:
        p = (sum(prices) / len(prices)) / 100.0
        p = min(max(p, 0.001), 0.999)
        var = max(var, p * (1.0 - p) * 10000.0)

    se = math.sqrt(var / n) if var > 0 else 0.0
    return (m, se, (m / se if se else 0.0), n)


def load(conn, series):
    rows = conn.execute("""
        SELECT c.ticker, c.secs_left, c.bid_close, c.ask_close,
               c.volume, s.result
        FROM candles c
        JOIN hist_settlements s ON s.ticker = c.ticker
        WHERE s.result IN ('yes','no')
          AND (? IS NULL OR c.series = ?)
          AND c.ask_close IS NOT NULL AND c.bid_close IS NOT NULL
          AND c.ask_close > 0 AND c.ask_close < 100
          AND c.secs_left BETWEEN -30 AND 1200
        ORDER BY c.ticker, c.secs_left DESC
    """, (series, series)).fetchall()
    by: dict = {}
    for tk, secs, bid, ask, vol, res in rows:
        by.setdefault(tk, []).append((secs, bid, ask, vol, res))
    return by


def entry_at(path, target, tol=45.0):
    c = [r for r in path if abs(r[0] - target) <= tol]
    return min(c, key=lambda r: abs(r[0] - target)) if c else None


def momentum(path, secs, lookback=120.0):
    now = entry_at(path, secs)
    then = entry_at(path, secs + lookback)
    if not now or not then:
        return None
    m_now = (now[1] + now[2]) / 2.0
    m_then = (then[1] + then[2]) / 2.0
    d = m_now - m_then
    return "up" if d > 2 else ("down" if d < -2 else "flat")


def cell(by, secs, lo, hi, cond=None, lookback=120.0):
    out, halves, pxs = [], [], []
    keys = sorted(by.keys())
    mid = len(keys) // 2
    for i, tk in enumerate(keys):
        path = by[tk]
        e = entry_at(path, secs)
        if not e:
            continue
        _s, bid, ask, _v, res = e

        no_ask = 100.0 - bid
        if lo <= ask <= hi:
            side, px = "yes", ask
        elif lo <= no_ask <= hi:
            side, px = "no", no_ask
        else:
            continue

        if cond is not None:
            mo = momentum(path, secs, lookback)
            if mo != cond:
                continue

        won = (res == side)
        out.append((100.0 if won else 0.0) - px - fee_c(px))
        halves.append(0 if i < mid else 1)
        pxs.append(px)
    return out, halves, pxs


def run_grid(by, conds):
    results = []
    for secs in ENTRY_TIMES:
        for lo, hi in PRICE_BANDS:
            for cond in conds:
                pnl, halves, pxs = cell(by, secs, lo, hi, cond)
                if len(pnl) < MIN_N:
                    continue
                m, se, t, n = stats(pnl, pxs)
                raw_se = stats(pnl)[1]
                a = [p for p, h in zip(pnl, halves) if h == 0]
                b = [p for p, h in zip(pnl, halves) if h == 1]
                ma = stats(a)[0] if len(a) > 5 else 0.0
                mb = stats(b)[0] if len(b) > 5 else 0.0
                results.append({
                    "secs": secs, "lo": lo, "hi": hi, "cond": cond or "any",
                    "n": n, "mean": m, "se": se, "t": t,
                    "h1": ma, "h2": mb,
                    "replicates": (ma > 0) == (mb > 0),
                    "floor_ratio": (se / raw_se) if raw_se > 1e-9 else 999.0,
                })
    return results


def report(results):
    k = len(results)
    if k == 0:
        print("\n  No cells had enough observations.")
        return
    thr = abs(_z_for(0.05 / (2 * k)))

    print("\n" + "=" * 82)
    print(f"GRID: {k} cells tested")
    print(f"Bonferroni threshold: |t| > {thr:.2f}   "
          f"(uncorrected 1.96 would yield ~{k*0.05:.0f} false positives)")
    print("=" * 82)

    survivors = [r for r in results
                 if abs(r["t"]) > thr and r["mean"] > 0 and r["replicates"]]
    near = [r for r in results if 1.96 < abs(r["t"]) <= thr and r["mean"] > 0]

    print(f"\n  cleared correction, positive, and replicated:  {len(survivors)}")
    print(f"  'significant' only at the naive 1.96 bar:      {len(near)}"
          "   <- these are the trap")

    floored = [r for r in results if r["floor_ratio"] > 2.0]
    if floored:
        print(f"\n  {len(floored)} cell(s) had their SE RAISED by the binomial")
        print("  floor — their raw variance was implausibly small, meaning a")
        print("  win/loss streak, not certainty. Without the floor these would")
        print("  have shown enormous t-statistics. Largest inflations:")
        for r in sorted(floored, key=lambda x: -x["floor_ratio"])[:4]:
            print(f"    {r['secs']:>4}s {r['lo']:>3}-{r['hi']:<3}c "
                  f"{r['cond']:>5}  n={r['n']:<6,} "
                  f"SE {r['se']/r['floor_ratio']:.2f} -> {r['se']:.2f} "
                  f"({r['floor_ratio']:.0f}x)  t now {r['t']:+.2f}")

    print("\n  TOP 15 BY NET EDGE (ranked, not filtered)")
    print(f"\n  {'secs':>5} {'band':>9} {'cond':>5} {'n':>7} {'net':>9} "
          f"{'SE':>7} {'t':>7} {'half1':>8} {'half2':>8} {'repl':>5}")
    print("  " + "-" * 78)
    for r in sorted(results, key=lambda x: -x["mean"])[:15]:
        mark = " *" if abs(r["t"]) > thr else ""
        print(f"  {r['secs']:>5} {r['lo']:>3}-{r['hi']:<5} {r['cond']:>5} "
              f"{r['n']:>7,} {r['mean']:>+8.2f}c {r['se']:>7.2f} "
              f"{r['t']:>+7.2f} {r['h1']:>+7.2f}c {r['h2']:>+7.2f}c "
              f"{'yes' if r['replicates'] else 'NO':>5}{mark}")

    print("\n  WORST 5 (a strategy that loses reliably is information too)")
    for r in sorted(results, key=lambda x: x["mean"])[:5]:
        print(f"  {r['secs']:>5} {r['lo']:>3}-{r['hi']:<5} {r['cond']:>5} "
              f"{r['n']:>7,} {r['mean']:>+8.2f}c {r['t']:>+7.2f}")

    print("\n" + "=" * 82)
    print("VERDICT")
    print("=" * 82)
    if not survivors:
        print(f"""
  ZERO strategies survive out of {k} tested.

  Note the {len(near)} cells that look significant at the naive 1.96 bar. Each
  one would feel like a discovery in isolation. Testing {k} things at once is
  what makes them visible as noise -- and it is why the grid is run all at
  once rather than one idea at a time.

  This is a real result: 15-minute BTC is directionally efficient across the
  whole price/time surface, not merely in the 90-96c band we started with.""")
    else:
        print(f"\n  {len(survivors)} cell(s) survived. Before believing any:")
        for r in survivors:
            print(f"    {r['secs']}s {r['lo']}-{r['hi']}c {r['cond']}: "
                  f"{r['mean']:+.2f}c, t={r['t']:+.2f}, n={r['n']:,}, "
                  f"halves {r['h1']:+.2f}/{r['h2']:+.2f}")
        print("""
  Next, in order:
    1. Re-run on a DIFFERENT series. A real effect is not BTC-only.
    2. Filter to deep quotes using kalshi_15m.db -- Q7 says thin-quote edge
       is unreachable, and this grid cannot see depth.
    3. Only then paper trade it.""")


def _z_for(p):
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    if p < 0.02425:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > 1 - 0.02425:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q, r = p - 0.5, (p - 0.5) ** 2
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def self_test():
    import os
    import random
    import tempfile
    print("SELF-TEST — plant an edge in one cell, find it, find nothing else\n")

    p = os.path.join(tempfile.mkdtemp(), "s.db")
    conn = sqlite3.connect(p)
    conn.executescript("""
        CREATE TABLE candles (ticker TEXT, series TEXT, secs_left REAL,
            bid_close REAL, ask_close REAL, volume REAL);
        CREATE TABLE hist_settlements (ticker TEXT PRIMARY KEY, series TEXT,
            result TEXT);
    """)
    rng = random.Random(5)
    PLANT_BAND, PLANT = (75, 90), 6.0
    N = 8000

    lo_b, hi_b = PLANT_BAND
    for i in range(N):
        tk = f"M{i}"
        base = rng.uniform(5, 95)
        no_px = 100.0 - base
        if lo_b <= base <= hi_b:
            true_yes = base + PLANT
        elif lo_b <= no_px <= hi_b:
            true_yes = base - PLANT
        else:
            true_yes = base
        win = rng.random() * 100 < true_yes
        conn.execute("INSERT INTO hist_settlements VALUES (?,?,?)",
                     (tk, "S", "yes" if win else "no"))
        for secs in ENTRY_TIMES:
            conn.execute("INSERT INTO candles VALUES (?,?,?,?,?,?)",
                         (tk, "S", secs, base - 0.5, base + 0.5, 100))
    conn.commit()

    by = load(conn, "S")
    res = run_grid(by, [None])
    thr = abs(_z_for(0.05 / (2 * len(res))))

    hits = [r for r in res if abs(r["t"]) > thr and r["mean"] > 0]
    planted = [r for r in hits
               if r["lo"] == PLANT_BAND[0] and r["hi"] == PLANT_BAND[1]]
    print(f"  {len(res)} cells, threshold |t| > {thr:.2f}")
    print(f"  cells clearing correction: {len(hits)}")
    print(f"  of which in the planted band: {len(planted)}")
    assert planted, "FAILED to recover a planted 4pt edge"
    m = max(planted, key=lambda r: r["t"])
    print(f"  [ok] planted band recovered: {m['lo']}-{m['hi']}c "
          f"{m['mean']:+.2f}c t={m['t']:+.2f} n={m['n']:,}")

    off = [r for r in hits if (r["lo"], r["hi"]) != PLANT_BAND]
    print(f"  [ok] false positives outside the planted band: {len(off)}")
    assert len(off) <= 1, f"too many false positives: {len(off)}"

    conn.execute("DELETE FROM candles")
    conn.execute("DELETE FROM hist_settlements")
    for i in range(N):
        tk = f"F{i}"
        base = rng.uniform(5, 95)
        win = rng.random() * 100 < base
        conn.execute("INSERT INTO hist_settlements VALUES (?,?,?)",
                     (tk, "S", "yes" if win else "no"))
        for secs in ENTRY_TIMES:
            conn.execute("INSERT INTO candles VALUES (?,?,?,?,?,?)",
                         (tk, "S", secs, base - 0.5, base + 0.5, 100))
    conn.commit()
    res2 = run_grid(load(conn, "S"), [None])
    thr2 = abs(_z_for(0.05 / (2 * len(res2))))
    hits2 = [r for r in res2 if abs(r["t"]) > thr2 and r["mean"] > 0]
    naive = [r for r in res2 if r["t"] > 1.96 and r["mean"] > 0]
    print(f"  [ok] fair world: {len(hits2)} survivors after correction, "
          f"{len(naive)} at the naive 1.96 bar")
    assert len(hits2) == 0, f"false discovery in a fair world: {len(hits2)}"

    print("\nALL PASS — the grid finds real edges and rejects fake ones.\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--series", default="KXBTC15M")
    ap.add_argument("--momentum", action="store_true",
                    help="also condition on 2-minute price direction (x3 cells)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        self_test()
        sys.exit(0)

    conn = sqlite3.connect(a.db, timeout=10)
    by = load(conn, a.series)
    print(f"\n  {len(by):,} settled {a.series} markets loaded")
    print("  REMINDER: candles carry no depth. Upper bound, not money.")

    conds = [None, "up", "down", "flat"] if a.momentum else [None]
    report(run_grid(by, conds))
