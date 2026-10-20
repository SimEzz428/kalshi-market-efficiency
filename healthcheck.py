
from __future__ import annotations

import argparse
import sqlite3
import time

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_results = []


def check(name: str, status: str, detail: str = "") -> None:
    _results.append((status, name))
    mark = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL"}[status]
    print(f"  [{mark}] {name}")
    if detail:
        for line in detail.rstrip().split("\n"):
            print(f"         {line}")


EXPECTED_SERIES = {"KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M",
                   "KXDOGE15M", "KXBNB15M", "KXHYPE15M"}


def main(db: str, tmin: int, tmax: int, lo: float, hi: float):
    conn = sqlite3.connect(db)
    now = time.time()

    print("=" * 72)
    print("RECORDER HEALTH CHECK")
    print("=" * 72 + "\n")

    row = conn.execute("SELECT MAX(ts), MIN(ts), COUNT(*) FROM snapshots").fetchone()
    if not row or row[0] is None:
        check("Data arriving", FAIL, "No snapshots at all. Recorder never ran.")
        return summarize()
    latest, earliest, n_snap = row
    age = now - latest
    hours = (latest - earliest) / 3600
    if age < 60:
        check("Data arriving", PASS, f"last snapshot {age:.0f}s ago, {n_snap:,} total")
    elif age < 600:
        check("Data arriving", WARN,
              f"last snapshot {age/60:.1f} min ago — expected <1 min. Recorder may be stalled.")
    else:
        check("Data arriving", FAIL,
              f"last snapshot {age/3600:.1f} HOURS ago. Recorder is dead.\n"
              f"Restart: nohup ./venv/bin/python recorder.py --run > recorder.log 2>&1 &")

    print(f"         collecting for {hours:.1f} hours\n")

    seen = {r[0] for r in conn.execute("SELECT DISTINCT series FROM snapshots")}
    missing = EXPECTED_SERIES - seen
    extra = seen - EXPECTED_SERIES
    if not missing:
        check("All 7 series present", PASS, f"{', '.join(sorted(seen))}")
    else:
        check("All 7 series present", FAIL, f"MISSING: {', '.join(sorted(missing))}")
    if extra:
        print(f"         (also recording: {', '.join(sorted(extra))})")

    row = conn.execute(
        """SELECT COUNT(*),
                  SUM(CASE WHEN yes_bid IS NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN yes_bid < 0 OR yes_bid > 100 THEN 1 ELSE 0 END),
                  MIN(yes_bid), MAX(yes_bid)
           FROM snapshots"""
    ).fetchone()
    tot, nulls, oob, mn, mx = row
    null_pct = nulls / tot if tot else 1
    if null_pct > 0.9:
        check("Prices populated", FAIL,
              f"{null_pct:.0%} of yes_bid are NULL. Field mapping is broken.\n"
              f"Run: python recorder.py --probe KXBTC15M")
    elif null_pct > 0.5:
        check("Prices populated", WARN,
              f"{null_pct:.0%} NULL — high, but some is normal (one-sided books "
              f"near expiry)")
    else:
        check("Prices populated", PASS, f"{100*(1-null_pct):.0f}% have a bid, "
                                        f"range {mn:.1f}-{mx:.1f}c")
    if oob:
        check("Prices in 0-100 range", FAIL, f"{oob} rows outside range — unit bug")

    n_win = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE secs_left BETWEEN ? AND ?", (tmin, tmax)
    ).fetchone()[0]
    if n_win == 0:
        check(f"Coverage at {tmin}-{tmax}s", FAIL, "No snapshots in the entry window.")
    else:
        check(f"Coverage at {tmin}-{tmax}s", PASS, f"{n_win:,} snapshots")

    joined = conn.execute(
        """SELECT COUNT(*) FROM snapshots s
           JOIN settlements t ON t.ticker = s.ticker
           WHERE s.secs_left BETWEEN ? AND ?""", (tmin, tmax)
    ).fetchone()[0]
    closed = conn.execute(
        "SELECT COUNT(DISTINCT ticker) FROM snapshots WHERE close_ts < ?", (now,)
    ).fetchone()[0]
    joinable = conn.execute(
        """SELECT COUNT(DISTINCT s.ticker) FROM snapshots s
           JOIN settlements t ON t.ticker = s.ticker WHERE s.close_ts < ?""", (now,)
    ).fetchone()[0]

    if closed == 0:
        check("Snapshot->settlement join", WARN,
              "No markets have closed yet. Re-check in ~20 minutes.")
    elif joinable == 0:
        check("Snapshot->settlement join", FAIL,
              f"{closed} markets closed but NONE join to settlements.\n"
              f"Ticker formats differ between endpoints — Q1 will be empty.\n"
              f"This is the failure that looks healthy until the very end.")
    elif joinable / closed < 0.5:
        check("Snapshot->settlement join", WARN,
              f"only {joinable}/{closed} closed markets join ({joinable/closed:.0%})")
    else:
        check("Snapshot->settlement join", PASS,
              f"{joinable}/{closed} closed markets join ({joinable/closed:.0%}), "
              f"{joined:,} usable rows in window")

    inband = conn.execute(
        """SELECT COUNT(DISTINCT s.ticker) FROM snapshots s
           JOIN settlements t ON t.ticker = s.ticker
           WHERE s.secs_left BETWEEN ? AND ?
             AND t.result IN ('yes','no')
             AND ((s.yes_bid BETWEEN ? AND ?) OR (100 - s.yes_ask BETWEEN ? AND ?))""",
        (tmin, tmax, lo, hi, lo, hi),
    ).fetchone()[0]

    gaps = conn.execute(
        """SELECT COUNT(*) FROM (
             SELECT ts - LAG(ts) OVER (ORDER BY ts) AS d
             FROM (SELECT DISTINCT ts FROM snapshots)
           ) WHERE d > 300"""
    ).fetchone()[0]
    if gaps == 0:
        check("No collection gaps", PASS)
    else:
        check("No collection gaps", WARN,
              f"{gaps} gaps over 5 min (sleep or network drops). Not fatal — "
              f"just slows accumulation.")

    depth = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE yes_bid_size IS NOT NULL"
    ).fetchone()[0]
    if depth == 0:
        check("Order book depth captured", WARN,
              "yes_bid_size all NULL — you lose the adverse-selection analysis, "
              "but Q1 still works.")
    else:
        check("Order book depth captured", PASS, f"{depth:,} rows with size data")

    print("\n" + "=" * 72)
    print("PROGRESS TOWARD AN ANSWER")
    print("=" * 72 + "\n")
    print(f"  in-band observations so far   {inband:>8,}")
    print(f"  needed for a 1.0pt edge       {2500:>8,}")
    print(f"  needed for a 2.0pt edge       {625:>8,}")
    if hours > 0.2 and inband > 0:
        rate = inband / hours
        print(f"\n  rate                          {rate:>8.1f} / hour")
        for target, label in ((625, "2.0pt"), (2500, "1.0pt")):
            if inband >= target:
                print(f"  -> already enough to resolve a {label} edge")
            else:
                d = (target - inband) / rate / 24
                print(f"  -> {label} edge resolvable in {d:.1f} more days")
    elif inband == 0:
        print("\n  No in-band observations yet. Normal in the first hour — the")
        print("  90-96c band only occurs when a market moves decisively.")

    summarize()


def summarize():
    print("\n" + "=" * 72)
    fails = sum(1 for s, _ in _results if s == FAIL)
    warns = sum(1 for s, _ in _results if s == WARN)
    if fails:
        print(f"{fails} FAILURE(S) — fix before trusting this data:")
        for s, n in _results:
            if s == FAIL:
                print(f"   - {n}")
    elif warns:
        print(f"HEALTHY, {warns} warning(s). Collection is usable.")
    else:
        print("ALL CHECKS PASSED. Leave it running.")
    print("=" * 72)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="kalshi_15m.db")
    ap.add_argument("--tmin", type=int, default=180)
    ap.add_argument("--tmax", type=int, default=360)
    ap.add_argument("--lo", type=float, default=90)
    ap.add_argument("--hi", type=float, default=96)
    a = ap.parse_args()
    main(a.db, a.tmin, a.tmax, a.lo, a.hi)
