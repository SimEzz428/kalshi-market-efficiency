
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
results = []


def check(name, cond, detail="", severity=FAIL):
    status = PASS if cond else severity
    results.append((status, name))
    mark = {PASS: "PASS", FAIL: "FAIL", WARN: "WARN", SKIP: "SKIP"}[status]
    print(f"  [{mark}] {name}")
    if detail and not cond:
        for line in str(detail).split("\n"):
            print(f"         {line}")


def section(t):
    print(f"\n{'='*72}\n{t}\n{'='*72}")


def q(conn, sql, default=0):
    try:
        r = conn.execute(sql).fetchone()
        return r[0] if r and r[0] is not None else default
    except sqlite3.Error:
        return default


def run_unit_tests():
    section("1. UNIT TESTS — do the tools still recover planted answers?")
    tests = [
        ("edge.py self-tests", [sys.executable, "edge.py"], "ALL TESTS PASSED"),
        ("analyzer vs synthetic worlds", [sys.executable, "test_analyze.py"],
         "ALL CHECKS PASSED"),
    ]
    for name, cmd, expect in tests:
        if not os.path.exists(cmd[1]):
            check(name, False, f"{cmd[1]} not found", SKIP)
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            check(name, expect in r.stdout,
                  (r.stdout or "")[-600:] + (r.stderr or "")[-400:])
        except subprocess.TimeoutExpired:
            check(name, False, "timed out")


def check_15m(path="kalshi_15m.db"):
    section("2. CRYPTO DATABASE — invariants")
    if not os.path.exists(path):
        check("database exists", False, path, SKIP)
        return
    c = sqlite3.connect(path)
    now = time.time()

    n = q(c, "SELECT COUNT(*) FROM snapshots")
    check("snapshots present", n > 0, f"{n} rows")
    print(f"         {n:,} snapshots")

    bad = q(c, "SELECT COUNT(*) FROM snapshots WHERE yes_bid < 0 OR yes_bid > 100 "
               "OR yes_ask < 0 OR yes_ask > 100")
    check("all prices within 0-100", bad == 0, f"{bad} rows outside range")

    crossed = q(c, "SELECT COUNT(*) FROM snapshots "
                   "WHERE yes_bid IS NOT NULL AND yes_ask IS NOT NULL "
                   "AND yes_bid > yes_ask")
    check("bid never above ask", crossed == 0,
          f"{crossed} crossed quotes — real arb or a parsing bug", WARN)

    nulls = q(c, "SELECT COUNT(*) FROM snapshots WHERE yes_bid IS NULL")
    rate = nulls / n if n else 1
    check("price NULL rate under 50%", rate < 0.5, f"{rate:.1%} NULL")

    incons = q(c, "SELECT COUNT(*) FROM snapshots WHERE close_ts IS NOT NULL "
                  "AND ABS((close_ts - ts) - secs_left) > 2")
    check("secs_left consistent with timestamps", incons == 0,
          f"{incons} rows where close_ts - ts != secs_left")

    future = q(c, f"SELECT COUNT(*) FROM snapshots WHERE ts > {now + 60}")
    check("no future timestamps", future == 0, f"{future} rows")

    badres = q(c, "SELECT COUNT(*) FROM settlements WHERE result NOT IN ('yes','no')")
    check("settlement results valid", badres == 0, f"{badres} bad results")

    closed = q(c, f"SELECT COUNT(DISTINCT ticker) FROM snapshots WHERE close_ts < {now}")
    joined = q(c, f"SELECT COUNT(DISTINCT s.ticker) FROM snapshots s "
                  f"JOIN settlements t ON t.ticker = s.ticker WHERE s.close_ts < {now}")
    jr = joined / closed if closed else 0
    check("snapshot->settlement join above 90%", jr > 0.9,
          f"{joined}/{closed} = {jr:.0%}")

    nb = q(c, "SELECT COUNT(*) FROM orderbooks")
    if nb:
        print(f"         {nb:,} order books")
        empty = q(c, "SELECT COUNT(*) FROM orderbooks WHERE yes_book IN ('[]','') "
                     "OR yes_book IS NULL")
        check("order books not empty", empty < nb * 0.5,
              f"{empty}/{nb} empty — parser may be broken")
        negd = q(c, "SELECT COUNT(*) FROM orderbooks WHERE yes_depth < 0 OR no_depth < 0")
        check("depths non-negative", negd == 0, f"{negd} rows")
    else:
        check("order books present", False, "none recorded", WARN)

    ns = q(c, "SELECT COUNT(*) FROM spot")
    if ns:
        print(f"         {ns:,} spot prices")
        nonpos = q(c, "SELECT COUNT(*) FROM spot WHERE price <= 0")
        check("spot prices positive", nonpos == 0, f"{nonpos} rows")
        btc = q(c, "SELECT AVG(price) FROM spot WHERE product='BTC-USD'")
        check("BTC spot in a plausible range", 1_000 < btc < 1_000_000,
              f"avg {btc:,.0f} — check the product mapping")
        print(f"         BTC avg {btc:,.0f}")
    else:
        check("spot prices present", False, "none recorded", WARN)

    last = q(c, "SELECT MAX(ts) FROM snapshots")
    age = now - last if last else 1e9
    check("collecting right now (last snapshot < 5 min)", age < 300,
          f"last was {age/60:.1f} min ago")
    c.close()


def check_wide(path="kalshi_wide.db"):
    section("3. WIDE DATABASE — invariants")
    if not os.path.exists(path):
        check("database exists", False, path, SKIP)
        return
    c = sqlite3.connect(path)
    now = time.time()

    n = q(c, "SELECT COUNT(*) FROM wide_snapshots")
    check("wide snapshots present", n > 0, f"{n} rows")
    print(f"         {n:,} snapshots, "
          f"{q(c,'SELECT COUNT(*) FROM wide_settlements'):,} settlements")

    bad = q(c, "SELECT COUNT(*) FROM wide_snapshots "
               "WHERE yes_bid <= 0 OR yes_bid >= 100 OR yes_ask <= 0 OR yes_ask >= 100")
    check("wide prices strictly inside 0-100", bad == 0, f"{bad} rows")

    beyond = q(c, "SELECT COUNT(*) FROM wide_snapshots WHERE hours_left > 25")
    check("close-time filter respected (<=24h)", beyond == 0,
          f"{beyond} rows beyond the filter — the filter is not being applied")

    dm = q(c, "SELECT COUNT(DISTINCT ticker) FROM wide_snapshots")
    check("sweeping a real number of markets", dm > 100,
          f"only {dm} distinct tickers — sweep may be truncated", WARN)
    print(f"         {dm:,} distinct markets")
    c.close()


def check_arb(path="kalshi_arb.db"):
    section("4. ARB DATABASE — invariants")
    if not os.path.exists(path):
        check("database exists", False, path, SKIP)
        return
    c = sqlite3.connect(path)
    n = q(c, "SELECT COUNT(*) FROM violations")
    print(f"         {n:,} violations recorded")

    check("violation count plausible (not a flood)", n < 200,
          f"{n} violations — that many means false positives, not free money")

    zero = q(c, "SELECT COUNT(*) FROM violations WHERE depth <= 0")
    check("no zero-depth violations", zero == 0,
          f"{zero} with no resting size — these are unquoted markets")

    bucket = q(c, "SELECT COUNT(*) FROM violations WHERE kind='bucket_sum'")
    check("bucket_sum check removed", bucket == 0,
          f"{bucket} bucket_sum rows — you are running the old scanner", WARN)
    c.close()


def cross_checks(path="kalshi_15m.db"):
    section("5. CROSS-CHECKS — do independent sources agree?")
    if not os.path.exists(path):
        check("database exists", False, path, SKIP)
        return
    c = sqlite3.connect(path)

    rows = c.execute(
        """SELECT series, close_ts, expiration_value, floor_strike
           FROM settlements WHERE expiration_value IS NOT NULL
           ORDER BY series, close_ts LIMIT 4000""").fetchall()
    pairs = match = 0
    prev = {}
    for series, cts, ev, fs in rows:
        if series in prev:
            pcts, pev = prev[series]
            if 0 < cts - pcts < 1200 and fs is not None:
                pairs += 1
                if abs(fs - pev) < 0.51:
                    match += 1
        prev[series] = (cts, ev)
    if pairs:
        check("strike == previous settlement (100%)", match == pairs,
              f"{match}/{pairs}")
        print(f"         {match:,}/{pairs:,} consecutive pairs match")
    else:
        check("strike linkage checkable", False, "not enough pairs yet", SKIP)

    y = q(c, "SELECT COUNT(*) FROM settlements WHERE result='yes'")
    nn = q(c, "SELECT COUNT(*) FROM settlements WHERE result='no'")
    if y + nn > 100:
        r = y / (y + nn)
        check("yes/no settlement mix not degenerate", 0.2 < r < 0.8,
              f"{r:.1%} yes — a lopsided mix suggests selection bias")
        print(f"         {y:,} yes / {nn:,} no ({r:.1%})")
    c.close()


if __name__ == "__main__":
    print("=" * 72)
    print("SYSTEM VERIFICATION")
    print("=" * 72)

    run_unit_tests()
    check_15m()
    check_wide()
    check_arb()
    cross_checks()

    section("SUMMARY")
    f = sum(1 for s, _ in results if s == FAIL)
    w = sum(1 for s, _ in results if s == WARN)
    p = sum(1 for s, _ in results if s == PASS)
    print(f"  {p} passed, {w} warnings, {f} failures\n")
    if f:
        print("  FAILURES — fix before trusting any analysis:")
        for s, n in results:
            if s == FAIL:
                print(f"    - {n}")
    elif w:
        print("  Warnings only. Review them, but the system is usable.")
        for s, n in results:
            if s == WARN:
                print(f"    - {n}")
    else:
        print("  ALL INVARIANTS HOLD. No known bugs.")
    print("""
  Note what this can and cannot tell you:
    CAN  — the code does what it claims, and the data is internally consistent
    CANNOT — whether your hypothesis is true, or whether an edge exists

  Verification proves the instrument is working. It says nothing about what
  the instrument is measuring.""")
    sys.exit(1 if f else 0)
