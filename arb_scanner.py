
from __future__ import annotations

import argparse
import re
import sqlite3
import time
from collections import defaultdict

from kalshi_client import KalshiClient
from recorder import cents, num, parse_ts, pick

DB = "kalshi_arb.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS violations (
    id        INTEGER PRIMARY KEY,
    ts        REAL,
    kind      TEXT,      -- monotonicity | bucket_sum | yes_no_cross
    event     TEXT,
    detail    TEXT,
    edge_c    REAL,      -- cents of theoretical profit per contract
    depth     REAL,      -- min resting size across the legs
    hours_left REAL
);
CREATE INDEX IF NOT EXISTS idx_v_ts ON violations(ts);
CREATE INDEX IF NOT EXISTS idx_v_kind ON violations(kind);
"""

def fee_c(p):
    return 0.07 * p * (1 - p) * 100


def strike_of(m: dict) -> float | None:
    v = num(pick(m, "floor_strike", "cap_strike", "strike_value"))
    if v is not None and v > 0:
        return v
    mt = re.search(r"-T([0-9.]+)$", m.get("ticker") or "")
    if mt:
        try:
            f = float(mt.group(1))
            return f if f > 0 else None
        except ValueError:
            return None
    return None


def scan_once(client: KalshiClient, conn, min_edge_c=0.5, verbose=True):
    now = time.time()
    markets, cursor = [], None
    for _ in range(400):
        params = {"limit": 200, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        try:
            data = client.request("GET", "/markets", params=params)
        except Exception as e:
            print(f"  fetch error: {str(e)[:60]}")
            break
        markets.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor:
            break

    if verbose:
        print(f"  scanned {len(markets):,} open markets")

    by_event = defaultdict(list)
    skipped_unquoted = 0
    for m in markets:
        yb = cents(m, "yes_bid_dollars", "yes_bid")
        ya = cents(m, "yes_ask_dollars", "yes_ask")
        nb = cents(m, "no_bid_dollars", "no_bid")
        bid_sz = num(pick(m, "yes_bid_size_fp", "yes_bid_size")) or 0
        ask_sz = num(pick(m, "yes_ask_size_fp", "yes_ask_size")) or 0

        if yb is None or ya is None or nb is None:
            skipped_unquoted += 1
            continue
        if not (0 < yb < 100 and 0 < ya < 100):
            skipped_unquoted += 1
            continue
        if bid_sz <= 0 or ask_sz <= 0:
            skipped_unquoted += 1
            continue
        by_event[m.get("event_ticker") or "?"].append({
            "ticker": m.get("ticker"), "yb": yb, "ya": ya, "nb": nb,
            "strike": strike_of(m),
            "bid_sz": bid_sz, "ask_sz": ask_sz,
            "close": parse_ts(m.get("close_time")),
            "title": (m.get("title") or "")[:50],
        })

    if verbose:
        print(f"  {skipped_unquoted:,} skipped (no live two-sided quote)")

    found = []

    for ev, ms in by_event.items():
        for m in ms:
            if m["nb"] is None:
                continue
            total = m["yb"] + m["nb"]
            if total > 100:
                gross = total - 100
                net = gross - fee_c(m["yb"] / 100) - fee_c(m["nb"] / 100)
                if net > min_edge_c:
                    found.append(("yes_no_cross", ev,
                                  f"{m['ticker']}: yes_bid {m['yb']:.1f} + "
                                  f"no_bid {m['nb']:.1f} = {total:.1f}",
                                  net, min(m["bid_sz"], m["ask_sz"]),
                                  (m["close"] - now) / 3600 if m["close"] else None))

    for ev, ms in by_event.items():
        ladder = sorted([m for m in ms if m["strike"] is not None],
                        key=lambda m: m["strike"])
        if len(ladder) < 2:
            continue
        if len({m["strike"] for m in ladder}) < 2:
            continue
        for a, b in zip(ladder, ladder[1:]):
            if b["strike"] <= a["strike"]:
                continue
            cost = a["ya"] - b["yb"]
            if cost < 0:
                net = -cost - fee_c(a["ya"] / 100) - fee_c(b["yb"] / 100)
                if net > min_edge_c:
                    found.append(("monotonicity", ev,
                                  f"ask({a['strike']:g})={a['ya']:.1f} <= "
                                  f"bid({b['strike']:g})={b['yb']:.1f} "
                                  f"[{a['ticker']} / {b['ticker']}]",
                                  net, min(a["ask_sz"], b["bid_sz"]),
                                  (a["close"] - now) / 3600 if a["close"] else None))


    if found:
        conn.executemany(
            """INSERT INTO violations (ts,kind,event,detail,edge_c,depth,hours_left)
               VALUES (?,?,?,?,?,?,?)""",
            [(now, k, e, d, ed, dp, h) for k, e, d, ed, dp, h in found])
        conn.commit()

    return found


def print_found(found):
    if not found:
        print("  no violations above threshold")
        return
    print(f"\n  {len(found)} VIOLATION(S):\n")
    print(f"  {'kind':<15} {'edge':>7} {'depth':>9} {'hrs':>6}  detail")
    print("  " + "-" * 92)
    for kind, ev, detail, edge, depth, hrs in sorted(found, key=lambda x: -x[3])[:25]:
        h = f"{hrs:.1f}" if hrs is not None else "?"
        print(f"  {kind:<15} {edge:>6.2f}c {depth:>9,.0f} {h:>6}  {detail[:60]}")
    print("""
  BEFORE ACTING ON ANY OF THESE:
    - depth column: can you actually fill BOTH legs at that size?
    - bucket_sum especially: my partition detection is a heuristic. Verify the
      markets really are mutually exclusive and exhaustive by reading the event
      page. Cumulative ladders ("above X") are NOT a partition and will sum to
      far more than 100 legitimately.
    - leg risk: filling one side and not the other converts a riskless trade
      into a naked directional bet. That is how arbitrage strategies lose money.""")


def report(conn):
    rows = conn.execute(
        """SELECT kind, COUNT(*), AVG(edge_c), MAX(edge_c), AVG(depth)
           FROM violations GROUP BY kind"""
    ).fetchall()
    if not rows:
        print("No violations recorded yet.")
        return
    print(f"  {'kind':<16} {'count':>7} {'avg edge':>10} {'max edge':>10} {'avg depth':>11}")
    print("  " + "-" * 58)
    for kind, n, avg, mx, dep in rows:
        print(f"  {kind:<16} {n:>7,} {avg:>9.2f}c {mx:>9.2f}c {dep or 0:>10,.0f}")

    span = conn.execute("SELECT MIN(ts), MAX(ts) FROM violations").fetchone()
    hrs = (span[1] - span[0]) / 3600 if span[0] else 0
    total = conn.execute("SELECT COUNT(*) FROM violations").fetchone()[0]
    if hrs > 0:
        print(f"\n  {total:,} over {hrs:.1f}h = {total/hrs:.1f}/hour")
        print("\n  Persistence matters more than count: a violation visible for one")
        print("  second is not tradeable. Check whether the same event repeats across")
        print("  consecutive scans.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--interval", type=float, default=120)
    ap.add_argument("--min-edge", type=float, default=0.5, help="cents, net of fees")
    ap.add_argument("--db", default=DB)
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()

    if a.report:
        report(conn)
    elif a.scan:
        print_found(scan_once(KalshiClient(), conn, a.min_edge))
    elif a.watch:
        c = KalshiClient()
        print(f"Watching every {a.interval:.0f}s, min edge {a.min_edge}c net\n")
        n = 0
        while True:
            t0 = time.time()
            f = scan_once(c, conn, a.min_edge, verbose=False)
            n += 1
            print(f"  scan {n:>4} | {len(f):>3} violations | {time.time()-t0:.0f}s")
            for kind, ev, detail, edge, depth, hrs in sorted(f, key=lambda x: -x[3])[:3]:
                print(f"       {kind}: {edge:.2f}c depth={depth:,.0f}  {detail[:55]}")
            time.sleep(max(0, a.interval - (time.time() - t0)))
    else:
        ap.print_help()
