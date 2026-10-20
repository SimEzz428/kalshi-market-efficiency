
from __future__ import annotations

import argparse
import math
import sqlite3
import sys

DB = "kalshi_hist.db"
ENTRY_LO, ENTRY_HI = 90.0, 96.0


def fee_c(price_cents: float) -> float:
    p = price_cents / 100.0
    return math.ceil(round(0.07 * p * (1 - p) * 100, 9)) / 100.0 * 100.0


def stats(v):
    n = len(v)
    if n < 2:
        return (0.0, 0.0, 0.0, n)
    m = sum(v) / n
    var = sum((x - m) ** 2 for x in v) / (n - 1)
    se = math.sqrt(var / n) if var > 0 else 0.0
    return (m, se, (m / se if se else 0.0), n)


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def build_entries(conn, series, entry_secs, tol=45.0):
    rows = conn.execute("""
        SELECT c.ticker, c.secs_left,
               c.bid_close, c.ask_close, c.bid_low, c.ask_high,
               s.result
        FROM candles c
        JOIN hist_settlements s ON s.ticker = c.ticker
        WHERE s.result IN ('yes','no')
          AND c.secs_left BETWEEN -30 AND 1200
          AND (? IS NULL OR c.series = ?)
          AND c.ask_close IS NOT NULL AND c.bid_close IS NOT NULL
          AND c.ask_close > 0 AND c.ask_close < 100
        ORDER BY c.ticker, c.secs_left DESC
    """, (series, series)).fetchall()

    by: dict = {}
    for r in rows:
        by.setdefault(r[0], []).append(r)

    out = []
    for tk, path in by.items():
        cand = [r for r in path if abs(r[1] - entry_secs) <= tol]
        if not cand:
            continue
        e = min(cand, key=lambda r: abs(r[1] - entry_secs))
        _tk, secs, bid_c, ask_c, _bl, _ah, res = e

        no_ask = 100.0 - bid_c
        if ENTRY_LO <= ask_c <= ENTRY_HI:
            side, entry_px = "yes", ask_c
        elif ENTRY_LO <= no_ask <= ENTRY_HI:
            side, entry_px = "no", no_ask
        else:
            continue

        fwd = []
        for r in path:
            if r[1] >= secs:
                continue
            worst = r[4] if side == "yes" else (100.0 - r[5])
            if worst is not None and 0 <= worst <= 100:
                fwd.append((r[1], worst))
        out.append((tk, side, entry_px, res, fwd))
    return out


def calibration(entries):
    print("=" * 78)
    print("CALIBRATION — is the 90-96c favourite fairly priced?")
    print("=" * 78)
    if not entries:
        print("\n  no entries")
        return
    diffs = [(100.0 if e[3] == e[1] else 0.0) - e[2] for e in entries]
    m, se, t, n = stats(diffs)
    won = sum(1 for e in entries if e[3] == e[1])
    avg = sum(e[2] for e in entries) / n
    lo, hi = wilson(won, n)

    print(f"\n  {n:,} entries    mean price {avg:.2f}c")
    print(f"  settled in favour {won:,} ({100.0*won/n:.2f}%)  "
          f"95% CI [{100*lo:.2f}, {100*hi:.2f}]%")
    print(f"\n  RAW edge (before fees)  {m:+.3f}pt")
    print(f"  95% CI                  [{m-1.96*se:+.3f}, {m+1.96*se:+.3f}]pt")
    print(f"  t                       {t:+.2f}")

    net = m - fee_c(avg)
    print(f"\n  fee at {avg:.1f}c            {fee_c(avg):.2f}c")
    print(f"  NET edge after fees     {net:+.3f}pt  <- the only one that matters")

    print(f"\n  smallest detectable effect: {2*1.96*se:.2f}pt")
    if abs(t) < 1.96:
        print("  VERDICT: consistent with fair pricing.")
    elif net <= 0:
        print("  VERDICT: statistically non-zero but NEGATIVE after fees.")
    else:
        print("  VERDICT: positive after fees — needs a depth-filtered retest\n"
              "           before it means anything (see the docstring).")


def by_bucket(entries):
    print("\n" + "=" * 78)
    print("BY ENTRY PRICE — is the deviation smooth, or one spike?")
    print("=" * 78)
    buckets: dict = {}
    for tk, side, px, res, _f in entries:
        b = int(px)
        buckets.setdefault(b, []).append((100.0 if res == side else 0.0) - px)
    print(f"\n  {'price':>7} {'n':>7} {'edge':>10} {'SE':>8} {'t':>7}")
    print("  " + "-" * 44)
    ks = sorted(buckets)
    thr = 2.24 + 0.15 * len(ks)
    for b in ks:
        m, se, t, n = stats(buckets[b])
        if n < 20:
            continue
        flag = "  *" if abs(t) > thr else ""
        print(f"  {b:>5}-{b+1}c {n:>7,} {m:>+9.3f}pt {se:>8.3f} {t:>+7.2f}{flag}")
    print(f"\n  Bonferroni threshold for {len(ks)} buckets: |t| > {thr:.2f}")
    print("  A real mispricing is SMOOTH across neighbours. One starred")
    print("  bucket beside unstarred ones is noise wearing a pattern.")


def stop_test(entries, levels):
    print("\n" + "=" * 78)
    print("STOP LOSS — paired difference against hold-to-settlement")
    print("=" * 78)
    if not entries:
        return

    hold = []
    for _tk, side, px, res, _f in entries:
        hold.append((100.0 if res == side else 0.0) - px - fee_c(px))
    mh, seh, th, n = stats(hold)
    print(f"\n  hold to settlement  {mh:+.3f}c  (SE {seh:.3f}, t {th:+.2f}, "
          f"n {n:,})")

    print(f"\n  {'stop':>6} {'paired diff':>13} {'SE':>8} {'t':>7} "
          f"{'fired':>12} {'would have won':>16} {'slip':>7}")
    print("  " + "-" * 74)
    thr = 2.24 + 0.15 * len(levels)
    best = None
    for lvl in levels:
        diffs, fired, recov, slips = [], 0, 0, []
        for i, (_tk, side, px, res, fwd) in enumerate(entries):
            hit = next((w for _s, w in fwd if w < lvl), None)
            if hit is None:
                diffs.append(0.0)
                continue
            fired += 1
            slips.append(lvl - hit)
            if res == side:
                recov += 1
            stopped_pnl = hit - fee_c(max(hit, 0.5)) - px - fee_c(px)
            diffs.append(stopped_pnl - hold[i])
        m, se, t, _ = stats(diffs)
        if best is None or m > best[1]:
            best = (lvl, m, se, t, fired, recov)
        pf = 100.0 * fired / n
        pr = 100.0 * recov / fired if fired else 0.0
        ms = sum(slips) / len(slips) if slips else 0.0
        star = "  *" if abs(t) > thr else ""
        print(f"  {lvl:>5.0f}c {m:>+12.3f}c {se:>8.3f} {t:>+7.2f} "
              f"{fired:>6,} ({pf:>2.0f}%) {pr:>14.0f}% {ms:>6.1f}c{star}")

    print(f"\n  Bonferroni threshold for {len(levels)} levels: |t| > {thr:.2f}")
    lvl, m, se, t, fired, recov = best
    print(f"\n  best: {lvl:.0f}c -> {m:+.3f}c per contract (t={t:+.2f})")
    if abs(t) < thr or m <= 0:
        print("""
  The stop does not beat holding. That is what a martingale predicts: a
  contract trading at 70c is WORTH 70c, so selling there realises exactly
  what holding was worth, minus a second fee and a second spread.

  Note 'would have won' — every one of those is a loss the stop created.""")
    else:
        print("\n  Survives correction. Now split the sample in half and check\n"
              "  it replicates before believing it.")


def holdout(entries, levels):
    print("\n" + "=" * 78)
    print("OUT-OF-SAMPLE — first half vs second half")
    print("=" * 78)
    half = len(entries) // 2
    for name, part in (("FIRST ", entries[:half]), ("SECOND", entries[half:])):
        if len(part) < 30:
            continue
        d = [(100.0 if e[3] == e[1] else 0.0) - e[2] for e in part]
        m, se, t, n = stats(d)
        print(f"  {name}  n={n:,}  edge {m:+.3f}pt  SE {se:.3f}  t {t:+.2f}")
    print("\n  Signs that differ between halves mean the full-sample number\n"
          "  was fitted, not found.")


def self_test():
    import os
    import random
    import tempfile
    print("SELF-TEST — planted edge must be recovered\n")
    p = os.path.join(tempfile.mkdtemp(), "h.db")
    conn = sqlite3.connect(p)
    conn.executescript("""
        CREATE TABLE candles (ticker TEXT, series TEXT, end_ts REAL,
            close_ts REAL, secs_left REAL, bid_open REAL, bid_high REAL,
            bid_low REAL, bid_close REAL, ask_open REAL, ask_high REAL,
            ask_low REAL, ask_close REAL, trade_close REAL, volume REAL,
            open_int REAL);
        CREATE TABLE hist_settlements (ticker TEXT PRIMARY KEY, series TEXT,
            close_ts REAL, result TEXT, expiration_value REAL,
            floor_strike REAL);
    """)
    rng = random.Random(11)
    PLANT = 2.0
    N = 3000
    for i in range(N):
        tk = f"T{i}"
        px = rng.uniform(90, 96)
        win = rng.random() * 100 < (px + PLANT)
        conn.execute("INSERT INTO hist_settlements VALUES (?,?,?,?,?,?)",
                     (tk, "S", 0, "yes" if win else "no", None, None))
        for secs in (240, 180, 120, 60):
            drift = rng.gauss(0, 3)
            bid = max(1.0, min(98.0, px - 1 + drift))
            conn.execute(
                "INSERT INTO candles (ticker,series,secs_left,bid_low,bid_close,"
                "ask_high,ask_close) VALUES (?,?,?,?,?,?,?)",
                (tk, "S", secs, bid - 4, bid, px + 4, px))
    conn.commit()

    e = build_entries(conn, "S", 180.0)
    print(f"  {len(e):,} entries recovered from {N:,} synthetic markets")
    assert len(e) > N * 0.8, f"lost too many entries: {len(e)}"

    d = [(100.0 if x[3] == x[1] else 0.0) - x[2] for x in e]
    m, se, t, n = stats(d)
    print(f"  measured edge {m:+.3f}pt (planted {PLANT:+.1f}pt), t={t:+.2f}")
    assert abs(m - PLANT) < 4 * se, f"failed to recover: {m:+.3f} vs {PLANT}"
    print("  [ok] planted edge recovered within error")

    conn.execute("DELETE FROM candles")
    conn.execute("DELETE FROM hist_settlements")
    for i in range(N):
        tk = f"N{i}"
        no_px = rng.uniform(90, 96)
        yes_bid = 100.0 - no_px
        win = rng.random() * 100 < (no_px + PLANT)
        conn.execute("INSERT INTO hist_settlements VALUES (?,?,?,?,?,?)",
                     (tk, "S", 0, "no" if win else "yes", None, None))
        for secs in (240, 180, 120, 60):
            conn.execute(
                "INSERT INTO candles (ticker,series,secs_left,bid_low,bid_close,"
                "ask_high,ask_close) VALUES (?,?,?,?,?,?,?)",
                (tk, "S", secs, yes_bid - 2, yes_bid, yes_bid + 6, yes_bid + 2))
    conn.commit()
    e2 = build_entries(conn, "S", 180.0)
    sides = {x[1] for x in e2}
    assert sides == {"no"}, f"expected NO-side entries, got {sides}"
    d2 = [(100.0 if x[3] == x[1] else 0.0) - x[2] for x in e2]
    m2, se2, _, _ = stats(d2)
    print(f"  NO-side world: {len(e2):,} entries, edge {m2:+.3f}pt "
          f"(planted {PLANT:+.1f})")
    assert abs(m2 - PLANT) < 4 * se2, f"NO side broken: {m2:+.3f}"
    print("  [ok] NO side measured correctly (not inverted)")

    print(f"  [ok] fee at 93c = {fee_c(93.0):.2f}c")
    print("\nALL PASS\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--series", default="KXBTC15M")
    ap.add_argument("--entry-secs", type=float, default=180.0)
    ap.add_argument("--stops", type=float, nargs="*",
                    default=[85, 80, 75, 70, 65, 60])
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        self_test()
        sys.exit(0)

    conn = sqlite3.connect(a.db, timeout=10)
    n_c = conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
    n_s = conn.execute("SELECT COUNT(*) FROM hist_settlements").fetchone()[0]
    print(f"\n  {n_c:,} candles, {n_s:,} settlements in {a.db}")
    print(f"  entry target: {a.entry_secs:.0f}s before close, series {a.series}")
    print("\n  REMINDER: candles carry no DEPTH. Q7 showed edge collapses from")
    print("  +3.95pt (thin) to +0.03pt (deep). Everything below is an UPPER")
    print("  BOUND over a mostly untradeable population.\n")

    entries = build_entries(conn, a.series, a.entry_secs)
    calibration(entries)
    by_bucket(entries)
    stop_test(entries, a.stops)
    holdout(entries, a.stops)
