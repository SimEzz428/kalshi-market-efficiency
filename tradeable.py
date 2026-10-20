
from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict

WIDE = "kalshi_wide.db"
CRYPTO = "kalshi_15m.db"


def med(v):
    if not v:
        return 0.0
    v = sorted(v)
    return v[len(v) // 2]


def screen(size_dollars=25.0, min_obs=30):
    print("=" * 84)
    print(f"TRADEABILITY SCREEN — can you fill ${size_dollars:.0f} here?")
    print("=" * 84)

    rows = []

    try:
        c = sqlite3.connect(CRYPTO)
        for series, yb, ya, bs, asz in c.execute(
                """SELECT series, yes_bid, yes_ask, yes_bid_size, yes_ask_size
                   FROM snapshots
                   WHERE yes_bid > 0 AND yes_ask < 100
                     AND secs_left BETWEEN 60 AND 900"""):
            rows.append((series, "Crypto 15m", ya - yb,
                         min(bs or 0, asz or 0), (yb + ya) / 2, 0.25))
        c.close()
    except sqlite3.Error as e:
        print(f"  (crypto db: {e})")

    try:
        w = sqlite3.connect(WIDE)
        for series, cat, yb, ya, bs, asz, hrs in w.execute(
                """SELECT series, category, yes_bid, yes_ask, bid_size, ask_size,
                          hours_left FROM wide_snapshots
                   WHERE yes_bid > 0 AND yes_ask < 100
                     AND bid_size IS NOT NULL"""):
            rows.append((series or "?", cat or "?", ya - yb,
                         min(bs or 0, asz or 0), (yb + ya) / 2, hrs or 24))
        w.close()
    except sqlite3.Error as e:
        print(f"  (wide db: {e})")

    if not rows:
        print("\n  No depth data yet.")
        print("  The wide sweep only started recording depth after the schema")
        print("  update — give it 20 minutes, then re-run.")
        return

    per = defaultdict(lambda: {"spr": [], "dep": [], "px": [], "hrs": [], "cat": ""})
    for series, cat, spr, dep, px, hrs in rows:
        d = per[series]
        d["spr"].append(spr)
        d["dep"].append(dep)
        d["px"].append(px)
        d["hrs"].append(hrs)
        d["cat"] = d["cat"] or cat

    scored = []
    for s, d in per.items():
        n = len(d["spr"])
        if n < min_obs:
            continue
        mspr = med(d["spr"])
        mdep = med(d["dep"])
        mpx = med(d["px"]) / 100.0 or 0.5
        contracts_needed = size_dollars / max(mpx, 0.01)
        fillable = mdep >= contracts_needed
        cost_pt = mspr + 2 * (0.07 * mpx * (1 - mpx) * 100)
        scored.append({
            "series": s, "cat": d["cat"], "n": n, "spread": mspr, "depth": mdep,
            "need": contracts_needed, "fillable": fillable, "cost": cost_pt,
            "hrs": med(d["hrs"]),
        })

    scored.sort(key=lambda r: (not r["fillable"], r["cost"], -r["depth"]))

    print(f"\n  {'series':<20} {'category':<14} {'depth':>8} {'need':>7} "
          f"{'spread':>8} {'cost':>8} {'fill?':>7}")
    print("  " + "-" * 82)

    shown_line = False
    for r in scored:
        if not r["fillable"] and not shown_line:
            print("  " + "=" * 82)
            print("  BELOW THIS LINE: cannot fill your size. Do not analyse these.")
            print("  " + "=" * 82)
            shown_line = True
        mark = "YES" if r["fillable"] else "no"
        print(f"  {r['series'][:20]:<20} {r['cat'][:14]:<14} {r['depth']:>8,.0f} "
              f"{r['need']:>7,.0f} {r['spread']:>7.2f}c {r['cost']:>7.2f}pt {mark:>7}")

    ok = [r for r in scored if r["fillable"]]
    print(f"\n  {len(ok)} of {len(scored)} series are fillable at ${size_dollars:.0f}.")

    if ok:
        print("\n  WHERE TO SPEND YOUR ANALYSIS TIME:")
        for r in ok[:5]:
            print(f"    {r['series']:<18} depth {r['depth']:>7,.0f}  "
                  f"round-trip cost {r['cost']:.2f}pt  settles in {r['hrs']:.0f}h")
        print("""
  The 'cost' column is what a round trip costs in probability points: the
  spread plus two taker fees. Any edge you hunt must exceed it. A market with
  a 3pt round-trip cost needs a 3pt edge before you make a single cent, and
  edges that large do not survive in liquid markets.

  So the sweet spot is: fillable, cheap round trip, settles fast enough to
  measure. Everything else is a research project, not a trade.""")
    else:
        print("""
  NOTHING is fillable at this size with current data. Two possibilities:
    - the wide sweep has not collected enough depth data yet (wait 20 min)
    - or Kalshi's short-dated markets are genuinely too thin for retail size

  If the second, that is itself the finding — and it would mean the tradeable
  universe is far smaller than the 80,000 open markets suggests.""")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=float, default=25.0)
    ap.add_argument("--min-obs", type=int, default=30)
    a = ap.parse_args()
    screen(a.size, a.min_obs)
