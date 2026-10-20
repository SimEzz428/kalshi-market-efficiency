
from __future__ import annotations

import argparse
import math
import sqlite3
import sys

DB = "kalshi_hist.db"
ENTRY_LO, ENTRY_HI = 90.0, 96.0

STOPS = [None, 89.0, 85.0, 80.0, 70.0]
TARGETS = [None, 97.0, 98.0, 99.0]


ORDER_SIZE = 4


def fee_c(price_cents: float, count: int = ORDER_SIZE) -> float:
    p = max(0.001, min(0.999, price_cents / 100.0))
    order_fee = math.ceil(round(0.07 * count * p * (1 - p) * 100, 9)) / 100.0
    return order_fee / count * 100.0


def stats(v):
    n = len(v)
    if n < 2:
        return (0.0, 0.0, 0.0, n)
    m = sum(v) / n
    var = sum((x - m) ** 2 for x in v) / (n - 1)
    se = math.sqrt(var / n) if var > 0 else 0.0
    return (m, se, (m / se if se else 0.0), n)


def build(conn, series, entry_secs, tol=45.0):
    rows = conn.execute("""
        SELECT c.ticker, c.secs_left, c.bid_close, c.ask_close,
               c.bid_low, c.bid_high, c.ask_low, c.ask_high, s.result
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
    for r in rows:
        by.setdefault(r[0], []).append(r)

    out = []
    for tk, path in by.items():
        cand = [r for r in path if abs(r[1] - entry_secs) <= tol]
        if not cand:
            continue
        e = min(cand, key=lambda r: abs(r[1] - entry_secs))
        _tk, secs, bid_c, ask_c, _bl, _bh, _al, _ah, res = e

        no_ask = 100.0 - bid_c
        if ENTRY_LO <= ask_c <= ENTRY_HI:
            side, px = "yes", ask_c
        elif ENTRY_LO <= no_ask <= ENTRY_HI:
            side, px = "no", no_ask
        else:
            continue

        fwd = []
        for r in path:
            if r[1] >= secs:
                continue
            _t, s2, _bc, _ac, bl, bh, al, ah = r[:8]
            if side == "yes":
                lo, hi = bl, bh
            else:
                lo, hi = (100.0 - ah if ah is not None else None,
                          100.0 - al if al is not None else None)
            if lo is None or hi is None:
                continue
            fwd.append((s2, lo, hi))
        out.append((tk, side, px, res, fwd))
    return out


def simulate(entries, stop, target):
    pnl, n_stop, n_target, n_settle, recovered = [], 0, 0, 0, 0
    for _tk, side, px, res, fwd in entries:
        cost = px + fee_c(px)
        exited = None
        for _s, lo, hi in fwd:
            hit_stop = stop is not None and lo < stop
            hit_target = target is not None and hi >= target
            if hit_stop:
                exited = ("stop", lo)
                break
            if hit_target:
                exited = ("target", target)
                break
        if exited is None:
            n_settle += 1
            pnl.append((100.0 if res == side else 0.0) - cost)
        else:
            kind, fill = exited
            if kind == "stop":
                n_stop += 1
                if res == side:
                    recovered += 1
            else:
                n_target += 1
            pnl.append(fill - fee_c(max(fill, 0.5)) - cost)
    return pnl, {"stop": n_stop, "target": n_target, "settle": n_settle,
                 "recovered": recovered}


def report(entries, entry_secs):
    print("=" * 84)
    print(f"EXIT GRID — buy the 90-96c favourite at {entry_secs:.0f}s, "
          f"{len(entries):,} entries")
    print("=" * 84)
    if not entries:
        print("\n  no qualifying entries")
        return

    base, bd = simulate(entries, None, None)
    mb, seb, tb, n = stats(base)
    won = sum(1 for e in entries if e[3] == e[1])
    print(f"\n  mean entry {sum(e[2] for e in entries)/n:.2f}c   "
          f"settled in favour {100.0*won/n:.1f}%")
    print(f"\n  BASELINE — hold to settlement: {mb:+.3f}c  "
          f"(SE {seb:.3f}, t {tb:+.2f})")

    combos = [(s, t) for s in STOPS for t in TARGETS if (s, t) != (None, None)]
    thr = 2.24 + 0.15 * len(combos)

    print(f"\n  {'stop':>6} {'target':>7} {'net P&L':>10} {'vs hold':>10} "
          f"{'SE':>7} {'t':>7} {'n_diff':>7} {'stopped':>8} {'took':>6} "
          f"{'settled':>8}")
    print("  " + "-" * 80)

    rows = []
    for s, t in combos:
        p, d = simulate(entries, s, t)
        m, se, _, _ = stats(p)
        diffs = [a - b for a, b in zip(p, base)]
        nz = sum(1 for x in diffs if abs(x) > 1e-9)
        md, sd, td, _ = stats(diffs)
        rows.append((s, t, m, md, sd, td, nz, d))
        star = " *" if abs(td) > thr else ""
        print(f"  {('-' if s is None else f'{s:.0f}c'):>6} "
              f"{('-' if t is None else f'{t:.0f}c'):>7} "
              f"{m:>+9.3f}c {md:>+9.3f}c {sd:>7.3f} {td:>+7.2f} {nz:>7,} "
              f"{d['stop']:>8,} {d['target']:>6,} {d['settle']:>8,}{star}")

    print(f"\n  Bonferroni threshold for {len(combos)} rules: |t| > {thr:.2f}")
    print("  'n_diff' is the REAL sample size — rules that never fired")
    print("  contribute an exact zero and carry no information.")

    stops_only = [r for r in rows if r[1] is None]
    targets_only = [r for r in rows if r[0] is None]

    print("\n" + "=" * 84)
    print("THE TWO STRATEGIES, SIDE BY SIDE")
    print("=" * 84)
    if stops_only:
        b = max(stops_only, key=lambda r: r[3])
        print(f"\n  STOP LOSS   best: {b[0]:.0f}c -> {b[3]:+.3f}c vs hold "
              f"(t={b[5]:+.2f}, n={b[6]:,})")
        w = b[7]
        if w["stop"]:
            print(f"              fired {w['stop']:,} times, "
                  f"{100.0*w['recovered']/w['stop']:.0f}% of those would "
                  f"have won anyway")
    if targets_only:
        b = max(targets_only, key=lambda r: r[3])
        print(f"\n  TAKE PROFIT best: {b[1]:.0f}c -> {b[3]:+.3f}c vs hold "
              f"(t={b[5]:+.2f}, n={b[6]:,})")
        exit_fee = fee_c(b[1])
        print(f"              exit fee at {b[1]:.0f}c is {exit_fee:.2f}c — "
              f"predicted cost of leaving early")
        gap = b[3] + exit_fee
        print(f"              observed {b[3]:+.3f}c vs predicted "
              f"{-exit_fee:+.3f}c   (residual {gap:+.3f}c)")
        if abs(gap) < 0.3:
            print("              -> matches the fee almost exactly. Take profit")
            print("                 is EV-neutral before costs, as a martingale")
            print("                 requires. It buys lower variance, not edge.")

    print("\n" + "=" * 84)
    print("VERDICT")
    print("=" * 84)
    best = max(rows, key=lambda r: r[3])
    if best[3] <= 0 or abs(best[5]) < thr:
        print("""
  No exit rule beats holding.

  This is what a martingale requires: a contract trading at X cents is WORTH
  X cents, so any rule that closes at X realises exactly what holding was
  worth -- minus a second fee, and for stops, minus slippage.

  Exit rules move risk around. They do not create edge.""")
    else:
        print(f"\n  {('stop ' + str(best[0])) if best[0] else ''} "
              f"{('target ' + str(best[1])) if best[1] else ''} "
              f"beats holding by {best[3]:+.3f}c (t={best[5]:+.2f}).")
        print("  Re-run on another series and another entry time before"
              " believing it.")


def self_test():
    import random
    from statistics import NormalDist
    print("SELF-TEST — fair market, no exit rule may win\n")

    def cdf(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    rng = random.Random(19)
    nd = NormalDist()
    entries = []
    while len(entries) < 6000:
        sig, tau0, x = 1.0, 1.0, 0.0
        k = -sig * math.sqrt(tau0) * nd.inv_cdf(rng.uniform(0.90, 0.96))
        p0 = cdf((x - k) / (sig * math.sqrt(tau0))) * 100
        if not (ENTRY_LO <= p0 <= ENTRY_HI):
            continue
        fwd, steps = [], 16
        dt = tau0 / steps
        for i in range(1, steps):
            prev = x
            x += rng.gauss(0, sig * math.sqrt(dt))
            tau = tau0 - i * dt
            a = cdf((prev - k) / (sig * math.sqrt(tau))) * 100
            b = cdf((x - k) / (sig * math.sqrt(tau))) * 100
            fwd.append((120 * tau / tau0, max(0.5, min(99.5, min(a, b))),
                        max(0.5, min(99.5, max(a, b)))))
        x += rng.gauss(0, sig * math.sqrt(dt))
        entries.append(("T", "yes", p0, "yes" if x > k else "no", fwd))

    base, _ = simulate(entries, None, None)
    mb = stats(base)[0]
    print(f"  hold to settlement: {mb:+.3f}c "
          f"(expected ~{-fee_c(93.0):+.2f}c, the entry fee)\n")

    worst_violation = 0.0
    for s in (89.0, 85.0, 70.0):
        p, _ = simulate(entries, s, None)
        d = [a - b for a, b in zip(p, base)]
        m, se, t, _ = stats(d)
        print(f"  stop   {s:>5.0f}c: {m:>+7.3f}c vs hold  t={t:+.2f}")
        worst_violation = max(worst_violation, m - 2 * se)
    for tg in (97.0, 98.0, 99.0):
        p, _ = simulate(entries, None, tg)
        d = [a - b for a, b in zip(p, base)]
        m, se, t, _ = stats(d)
        print(f"  target {tg:>5.0f}c: {m:>+7.3f}c vs hold  t={t:+.2f}  "
              f"(exit fee {fee_c(tg):.2f}c)")
        worst_violation = max(worst_violation, m - 2 * se)

    assert worst_violation <= 0.25, (
        f"an exit rule beat holding by {worst_violation:+.3f}c in a FAIR "
        f"market — the simulator is granting impossible fills")
    print(f"\n  [ok] no rule beats holding by more than noise "
          f"(worst {worst_violation:+.3f}c)")
    print("  [ok] fee model: 92c -> "
          f"{fee_c(92.0):.2f}c, 98c -> {fee_c(98.0):.2f}c")
    print("\nALL PASS\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--series", default="KXBTC15M")
    ap.add_argument("--entry-secs", type=float, default=120.0)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        self_test()
        sys.exit(0)

    conn = sqlite3.connect(a.db, timeout=10)
    print("\n  REMINDER: candles carry no depth. Upper bound, not money.")
    report(build(conn, a.series, a.entry_secs), a.entry_secs)
