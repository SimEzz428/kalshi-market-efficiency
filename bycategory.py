
from __future__ import annotations

import argparse
import math
import sqlite3
from collections import defaultdict

WIDE = "kalshi_wide.db"


def med(v):
    if not v:
        return 0.0
    v = sorted(v)
    return v[len(v) // 2]


def se_of(diffs, prices):
    n = len(diffs)
    if n < 2:
        return 1.0
    m = sum(diffs) / n
    sv = sum((d - m) ** 2 for d in diffs) / (n - 1)
    p = sum(prices) / n / 100.0
    return math.sqrt(max(sv, max(p * (1 - p), 1e-6)) / n)


def hdr(t):
    print(f"\n{'='*82}\n{t}\n{'='*82}")


def load(db):
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            """SELECT s.series, s.category, s.ticker, s.ts, s.yes_bid, s.yes_ask,
                      s.bid_size, s.ask_size, s.hours_left, t.result
               FROM wide_snapshots s
               JOIN wide_settlements t ON t.ticker = s.ticker
               WHERE s.yes_bid > 0 AND s.yes_ask < 100
                 AND t.result IN ('yes','no')""").fetchall()
    except sqlite3.Error as e:
        print(f"  query failed: {e}")
        rows = []

    try:
        struct = conn.execute(
            """SELECT series, category, yes_bid, yes_ask, bid_size, ask_size,
                      hours_left FROM wide_snapshots
               WHERE yes_bid > 0 AND yes_ask < 100""").fetchall()
    except sqlite3.Error:
        struct = []
    conn.close()
    return rows, struct


def main(db, size_dollars, min_settled, min_analyse):
    rows, struct = load(db)

    hdr("STAGE 1 — INVENTORY: which series have enough settled observations?")

    per = defaultdict(lambda: {"obs": {}, "cat": ""})
    for series, cat, ticker, ts, yb, ya, bs, asz, hrs, result in rows:
        d = per[series or "?"]
        d["cat"] = d["cat"] or (cat or "?")
        if ticker in d["obs"]:
            continue
        price = (yb + ya) / 2
        d["obs"][ticker] = {
            "edge": (1.0 if result == "yes" else 0.0) - price / 100.0,
            "price": price, "ts": ts,
            "depth": min(bs or 0, asz or 0), "spread": ya - yb,
        }

    sper = defaultdict(lambda: {"spr": [], "dep": [], "px": [], "n": 0, "cat": ""})
    for series, cat, yb, ya, bs, asz, hrs in struct:
        d = sper[series or "?"]
        d["cat"] = d["cat"] or (cat or "?")
        d["n"] += 1
        d["spr"].append(ya - yb)
        d["dep"].append(min(bs or 0, asz or 0))
        d["px"].append((yb + ya) / 2)

    total_series = len(sper)
    have_settled = {s: d for s, d in per.items() if len(d["obs"]) >= min_settled}

    print(f"\n  {total_series} series observed")
    print(f"  {len(per)} have at least one settled market")
    print(f"  {len(have_settled)} have {min_settled}+ settled observations\n")

    if not have_settled:
        print(f"  Nothing has {min_settled}+ settled observations yet.")
        print("""
  This is a TIMING limit, not a bug. The wide sweep only snapshots markets
  closing within 24h, and those need to actually settle before they can be
  analysed. Non-crypto categories settle daily at best, so this table fills
  up over weeks rather than hours.

  Re-run in a few days. Meanwhile Stage 2 below still works — structure needs
  no settlements.""")

    for s in sorted(have_settled, key=lambda k: -len(have_settled[k]["obs"]))[:25]:
        d = have_settled[s]
        print(f"    {s[:26]:<26} {d['cat'][:14]:<14} {len(d['obs']):>6} settled")

    hdr(f"STAGE 2 — TRADEABLE: which can you fill at ${size_dollars:.0f}?")
    print("\n  (structure only — no settlements needed, so this is well powered)\n")
    print(f"  {'series':<26} {'category':<13} {'n':>7} {'depth':>8} "
          f"{'need':>6} {'spread':>8} {'fill?':>6}")
    print("  " + "-" * 78)

    tradeable = set()
    ranked = sorted(sper.items(), key=lambda kv: -med(kv[1]["dep"]))
    for s, d in ranked[:30]:
        if d["n"] < 30:
            continue
        mdep = med(d["dep"])
        mpx = med(d["px"]) / 100.0 or 0.5
        need = size_dollars / max(mpx, 0.01)
        fill = mdep >= need
        if fill:
            tradeable.add(s)
        print(f"  {s[:26]:<26} {d['cat'][:13]:<13} {d['n']:>7,} {mdep:>8,.0f} "
              f"{need:>6,.0f} {med(d['spr']):>7.2f}c {'YES' if fill else 'no':>6}")

    print(f"\n  {len(tradeable)} of {total_series} series are fillable at "
          f"${size_dollars:.0f}.")

    hdr("STAGE 3 — CALIBRATION: are the survivors mispriced?")

    candidates = {s: d for s, d in have_settled.items()
                  if s in tradeable and len(d["obs"]) >= min_analyse}
    if not candidates:
        print(f"\n  No series is BOTH fillable AND has {min_analyse}+ settled")
        print("  observations yet. That intersection is what the funnel exists")
        print("  to find, and it needs more time.")
        if have_settled:
            print("\n  For reference, series with data but NOT fillable:")
            for s in list(have_settled)[:8]:
                print(f"    {s[:30]:<30} {len(have_settled[s]['obs']):>5} settled, "
                      f"depth {med(sper[s]['dep']):,.0f}")
        return

    k = len(candidates)
    z_thresh = 1.96 + 0.42 * math.log(max(k, 1))
    print(f"\n  {k} candidate(s) survived stages 1-2.")
    print(f"  Correction applied to {k} tests only -> |z| > {z_thresh:.2f}")
    print("  (correcting for 30+ series we never analysed would be far too")
    print("   harsh — this is why the funnel order matters)\n")

    print(f"  {'series':<26} {'n':>6} {'edge':>10} {'SE':>6} {'z':>7} {'verdict':>10}")
    print("  " + "-" * 72)

    survivors = []
    for s, d in sorted(candidates.items(), key=lambda kv: -len(kv[1]["obs"])):
        v = list(d["obs"].values())
        diffs = [o["edge"] for o in v]
        prices = [o["price"] for o in v]
        n = len(diffs)
        m = sum(diffs) / n * 100
        se = se_of(diffs, prices) * 100
        z = m / se if se else 0
        verd = "MISPRICED" if abs(z) > z_thresh else "—"
        if abs(z) > z_thresh:
            survivors.append((s, d, m, se, z))
        print(f"  {s[:26]:<26} {n:>6} {m:>+9.2f}pt {se:>5.2f} {z:>7.2f} {verd:>10}")

    hdr("STAGE 4 — REPLICATION: does it hold out-of-sample?")
    if not survivors:
        print("\n  Nothing reached stage 4. No series is both tradeable and")
        print("  significantly mispriced. Consistent with efficient markets.")
        return

    for s, d, m, se, z in survivors:
        v = sorted(d["obs"].values(), key=lambda o: o["ts"])
        half = len(v) // 2
        print(f"\n  {s}  (full sample {m:+.2f}pt, z={z:.2f})")
        for label, part in (("first half", v[:half]), ("second half", v[half:])):
            if len(part) < 20:
                print(f"    {label:<12} too few ({len(part)})")
                continue
            dd = [o["edge"] for o in part]
            pp = [o["price"] for o in part]
            mm = sum(dd) / len(dd) * 100
            ss = se_of(dd, pp) * 100
            print(f"    {label:<12} n={len(part):>5}  {mm:>+7.2f}pt  "
                  f"z={mm/ss if ss else 0:>6.2f}")
        print("""
    If both halves share the sign and are individually meaningful, this is a
    genuine candidate -> check the mechanism, then paper trade.
    If the halves disagree, the full-sample result was noise.""")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=WIDE)
    ap.add_argument("--size", type=float, default=25.0)
    ap.add_argument("--min-settled", type=int, default=20)
    ap.add_argument("--min-analyse", type=int, default=50)
    a = ap.parse_args()
    main(a.db, a.size, a.min_settled, a.min_analyse)
