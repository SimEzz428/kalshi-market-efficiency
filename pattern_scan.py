
from __future__ import annotations

import argparse
import math
import sqlite3
from collections import defaultdict

DB = "kalshi_15m.db"
LO, HI = 90.0, 96.0


def se_of(diffs, prices):
    n = len(diffs)
    if n < 2:
        return 1.0
    m = sum(diffs) / n
    sv = sum((d - m) ** 2 for d in diffs) / (n - 1)
    p = sum(prices) / n / 100.0
    return math.sqrt(max(sv, max(p * (1 - p), 1e-6)) / n)


def load(conn, deep_only=False):
    spot = defaultdict(list)
    try:
        for ts, product, price in conn.execute(
                "SELECT ts, product, price FROM spot WHERE price > 0 ORDER BY ts"):
            spot[product].append((ts, price))
    except sqlite3.Error:
        pass

    prod_of = {"KXBTC15M": "BTC-USD", "KXETH15M": "ETH-USD",
               "KXSOL15M": "SOL-USD", "KXXRP15M": "XRP-USD",
               "KXDOGE15M": "DOGE-USD"}

    def spot_at(product, ts):
        arr = spot.get(product)
        if not arr:
            return None, None
        lo, hi = 0, len(arr) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if arr[mid][0] < ts:
                lo = mid + 1
            else:
                hi = mid
        cur = arr[lo][1]
        j = lo
        while j > 0 and arr[j][0] > ts - 120:
            j -= 1
        return cur, arr[j][1]

    rows = conn.execute(
        """SELECT s.ticker, s.series, s.ts, s.secs_left, s.yes_bid, s.yes_ask,
                  s.yes_bid_size, s.yes_ask_size, s.floor_strike, s.volume,
                  t.result
           FROM snapshots s JOIN settlements t ON t.ticker = s.ticker
           WHERE s.secs_left BETWEEN 60 AND 600
             AND s.yes_bid > 0 AND s.yes_ask < 100
             AND t.result IN ('yes','no')""").fetchall()

    obs = {}
    for (ticker, series, ts, secs, yb, ya, bs, asz, strike, vol, result) in rows:
        prod = prod_of.get(series)
        cur, prev = spot_at(prod, ts) if prod else (None, None)
        for side in ("YES", "NO"):
            price = yb if side == "YES" else 100 - ya
            size = (bs if side == "YES" else asz) or 0
            won = (result == "yes") if side == "YES" else (result == "no")
            if not (LO <= price <= HI):
                continue
            if deep_only and size < 100:
                continue
            key = (ticker, side)
            if key in obs:
                continue
            mom = None
            if cur and prev and prev > 0:
                raw = (cur - prev) / prev * 100
                mom = raw if side == "YES" else -raw
            dist = None
            if cur and strike and strike > 0:
                dist = abs(cur - strike) / strike * 100
            obs[key] = {
                "edge": (1.0 if won else 0.0) - price / 100.0,
                "price": price, "secs": secs, "depth": size,
                "spread": ya - yb, "vol": vol or 0, "ts": ts,
                "mom": mom, "dist": dist, "series": series,
                "hour": int((ts % 86400) // 3600),
            }
    return list(obs.values())


def bucket_test(obs, name, keyfn, buckets, min_n=40):
    obs = [o for o in obs if keyfn(o) is not None]
    if len(obs) < min_n * 2:
        return None

    obs_sorted = sorted(obs, key=lambda o: o["ts"])
    mid = len(obs_sorted) // 2
    halves = {"first": obs_sorted[:mid], "second": obs_sorted[mid:]}

    out = {"name": name, "cells": [], "replicates": False}
    for blo, bhi, blabel in buckets:
        sel = [o for o in obs if blo <= keyfn(o) < bhi]
        if len(sel) < min_n:
            out["cells"].append((blabel, len(sel), None, None, None, None))
            continue
        d = [o["edge"] for o in sel]
        p = [o["price"] for o in sel]
        m = sum(d) / len(d) * 100
        se = se_of(d, p) * 100
        hm = {}
        for hname, hobs in halves.items():
            hs = [o for o in hobs if blo <= keyfn(o) < bhi]
            if len(hs) >= min_n // 2:
                hd = [o["edge"] for o in hs]
                hp = [o["price"] for o in hs]
                hm[hname] = sum(hd) / len(hd) * 100
            else:
                hm[hname] = None
        out["cells"].append((blabel, len(sel), m, se, hm.get("first"),
                             hm.get("second")))
    return out


def report(res, z_thresh):
    if not res:
        return 0
    print(f"\n  {res['name']}")
    print(f"    {'bucket':<18} {'n':>6} {'edge':>9} {'SE':>6} {'z':>6} "
          f"{'1st half':>9} {'2nd half':>9} {'repl?':>6}")
    print("    " + "-" * 76)
    hits = 0
    for label, n, m, se, h1, h2 in res["cells"]:
        if m is None:
            print(f"    {label:<18} {n:>6}   (too few)")
            continue
        z = m / se if se else 0
        repl = ""
        if h1 is not None and h2 is not None:
            same_sign = (h1 > 0) == (h2 > 0) == (m > 0)
            if abs(z) > z_thresh and same_sign:
                repl = "YES"
                hits += 1
            elif abs(z) > z_thresh:
                repl = "no"
        s1 = f"{h1:+.2f}" if h1 is not None else "  --"
        s2 = f"{h2:+.2f}" if h2 is not None else "  --"
        print(f"    {label:<18} {n:>6} {m:>+8.2f}pt {se:>5.2f} {z:>6.2f} "
              f"{s1:>9} {s2:>9} {repl:>6}")
    return hits


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--deep-only", action="store_true",
                    help="restrict to quotes with 100+ resting contracts")
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    obs = load(conn, a.deep_only)
    conn.close()

    print("=" * 84)
    print("CONDITIONAL PATTERN SCAN — where, if anywhere, is the market wrong?")
    print("=" * 84)
    print(f"\n  {len(obs):,} observations"
          f"{' (deep quotes only)' if a.deep_only else ''}")

    tests = [
        ("BY RESTING DEPTH", lambda o: o["depth"],
         [(0, 100, "thin <100"), (100, 1000, "medium"),
          (1000, 1e9, "deep 1000+")]),
        ("BY SPREAD", lambda o: o["spread"],
         [(0, 0.5, "<0.5c"), (0.5, 1.01, "0.5-1c"), (1.01, 99, ">1c")]),
        ("BY TIME TO EXPIRY", lambda o: o["secs"],
         [(60, 120, "1-2 min"), (120, 240, "2-4 min"),
          (240, 400, "4-7 min"), (400, 601, "7-10 min")]),
        ("BY PRICE LEVEL", lambda o: o["price"],
         [(90, 92, "90-92c"), (92, 94, "92-94c"), (94, 96.1, "94-96c")]),
        ("BY SPOT MOMENTUM (last 2 min, signed toward the side held)",
         lambda o: o["mom"],
         [(-99, -0.05, "against <-0.05%"), (-0.05, 0.05, "flat"),
          (0.05, 99, "with >+0.05%")]),
        ("BY DISTANCE FROM STRIKE (% of spot)", lambda o: o["dist"],
         [(0, 0.05, "very close"), (0.05, 0.15, "close"),
          (0.15, 99, "far")]),
        ("BY MARKET VOLUME", lambda o: o["vol"],
         [(0, 500, "low"), (500, 5000, "medium"), (5000, 1e9, "high")]),
        ("BY HOUR OF DAY (UTC)", lambda o: o["hour"],
         [(0, 6, "00-06"), (6, 12, "06-12"), (12, 18, "12-18"), (18, 24, "18-24")]),
    ]

    n_tests = sum(len(t[2]) for t in tests)
    z_thresh = 1.96 + 0.42 * math.log(max(n_tests, 1))
    print(f"  {n_tests} bucket tests -> corrected threshold |z| > {z_thresh:.2f}")
    print("  (a naive 1.96 would produce ~{:.1f} false positives here)"
          .format(n_tests * 0.05))

    total = 0
    for name, keyfn, buckets in tests:
        total += report(bucket_test(obs, name, keyfn, buckets), z_thresh) or 0

    print("\n" + "=" * 84)
    if total:
        print(f"  {total} condition(s) both significant AND replicating.")
        print("""
  Do not trade these yet. A replicating pattern is a HYPOTHESIS, not a
  strategy. Next steps in order:
    1. Is it fillable? Check the depth in that bucket.
    2. Does it survive fees and spread? Compare to the round-trip cost.
    3. Is there a MECHANISM? A pattern with no explanation is usually a
       property of your sample, not of the market.
    4. Only then: paper trade it.""")
    else:
        print("""  NOTHING significant survives correction and replication.

  That is a real finding, not a failure. It means these markets are
  efficient under every condition tested — across depth, spread, timing,
  price level, momentum, distance from strike, volume and time of day.

  The honest conclusion: on this data, there is no conditional edge either.""")
