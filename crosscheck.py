
from __future__ import annotations

import math
import sqlite3
import sys

DB = "kalshi_15m.db"
LO, HI = 90.0, 96.0
TMIN, TMAX = 180.0, 360.0


def naive_q1(db=DB):
    conn = sqlite3.connect(db)

    outcome = {}
    for ticker, result in conn.execute(
            "SELECT ticker, result FROM settlements WHERE result IN ('yes','no')"):
        outcome[ticker] = (result == "yes")

    rows = conn.execute(
        "SELECT ticker, ts, secs_left, yes_bid, yes_ask FROM snapshots "
        "WHERE secs_left >= ? AND secs_left <= ?", (TMIN, TMAX)).fetchall()
    conn.close()

    centre = (TMIN + TMAX) / 2.0
    candidates = []
    for ticker, ts, secs, yb, ya in rows:
        if ticker not in outcome:
            continue
        if yb is None or ya is None:
            continue
        if not (yb > 0 and ya < 100):
            continue
        won_yes = outcome[ticker]
        if LO <= yb <= HI:
            candidates.append((ticker, "YES", abs(secs - centre), yb,
                               1.0 if won_yes else 0.0))
        no_price = 100.0 - ya
        if LO <= no_price <= HI:
            candidates.append((ticker, "NO", abs(secs - centre), no_price,
                               0.0 if won_yes else 1.0))

    candidates.sort(key=lambda r: (r[0], r[1], r[2]))
    obs = []
    seen = set()
    for ticker, side, _dist, price, won in candidates:
        key = (ticker, side)
        if key in seen:
            continue
        seen.add(key)
        obs.append((price, won))

    if not obs:
        return None

    diffs = [won - price / 100.0 for price, won in obs]
    n = len(diffs)
    mean = sum(diffs) / n

    ss = 0.0
    for d in diffs:
        ss += (d - mean) ** 2
    sample_var = ss / (n - 1) if n > 1 else 0.0

    mean_price = sum(p for p, _ in obs) / n / 100.0
    binom_var = mean_price * (1.0 - mean_price)
    var = sample_var if sample_var > binom_var else binom_var
    se = math.sqrt(var / n)

    return {"n": n, "mean_pt": mean * 100, "se_pt": se * 100,
            "t": mean / se if se else 0.0,
            "wins": sum(1 for _, w in obs if w > 0.5)}


def analyze_q1(db=DB):
    import io
    import contextlib
    import analyze

    conn = sqlite3.connect(db)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        analyze.q1_settlement_rate(conn, LO, HI, int(TMIN), int(TMAX))
    conn.close()
    out = buf.getvalue()

    res = {}
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("mean edge"):
            res["mean_pt"] = float(s.split()[2])
        elif s.startswith("std error"):
            res["se_pt"] = float(s.split()[2])
        elif s.startswith("t-stat"):
            res["t"] = float(s.split()[1])
        elif "observations (deduped" in s:
            res["n"] = int(s.split()[0].replace(",", ""))
    return res or None


if __name__ == "__main__":
    print("=" * 70)
    print("INDEPENDENT CROSS-CHECK OF Q1")
    print("=" * 70)
    print("\nTwo implementations, deliberately different internals.\n")

    a = analyze_q1()
    b = naive_q1()

    if not a or not b:
        print("  Not enough data yet in one or both implementations.")
        sys.exit(0)

    print(f"  {'quantity':<16} {'analyze.py':>14} {'crosscheck.py':>16} {'diff':>10}")
    print("  " + "-" * 60)
    ok = True
    for key, label, tol in [("n", "observations", 0),
                            ("mean_pt", "mean edge (pt)", 0.02),
                            ("se_pt", "std error (pt)", 0.02),
                            ("t", "t-statistic", 0.05)]:
        va, vb = a.get(key), b.get(key)
        if va is None or vb is None:
            continue
        d = abs(va - vb)
        agree = d <= tol
        ok &= agree
        flag = "" if agree else "   <-- DISAGREE"
        print(f"  {label:<16} {va:>14.4f} {vb:>16.4f} {d:>10.4f}{flag}")

    print()
    if ok:
        print("  IMPLEMENTATIONS AGREE.")
        print("""
  Two independently written code paths — different join strategy, different
  dedup mechanism, different aggregation — produce the same number.

  A shared conceptual bug would have to make both wrong in exactly the same
  way. That is possible but unlikely, and this is the strongest check
  available short of a third implementation by someone else.

  This does NOT mean an edge exists. It means the number is being computed
  correctly. Those are different claims.""")
    else:
        print("  *** IMPLEMENTATIONS DISAGREE — one of them is wrong. ***")
        print("""
  Do not trust either number until this is resolved. Work through the naive
  version by hand on a handful of rows; it was written to be readable rather
  than fast, precisely so it can be audited line by line.""")
        sys.exit(1)
