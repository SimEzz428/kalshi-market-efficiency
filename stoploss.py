
from __future__ import annotations

import argparse
import math
import sqlite3
import sys

DB = "kalshi_15m.db"
ENTRY_LO, ENTRY_HI = 90.0, 96.0


def fee(count, price_cents):
    p = price_cents / 100.0
    return math.ceil(round(0.07 * count * p * (1 - p) * 100, 9)) / 100.0


def fee_per_contract(price_cents):
    return fee(1, price_cents) * 100.0


def load_paths(conn, entry_secs, window):
    rows = conn.execute("""
        SELECT s.ticker, s.secs_left, s.yes_bid, s.yes_ask, s.no_bid, s.no_ask,
               t.result
        FROM snapshots s
        JOIN settlements t ON t.ticker = s.ticker
        WHERE s.series = 'KXBTC15M'
          AND t.result IN ('yes','no')
          AND s.secs_left BETWEEN 0 AND ?
          AND s.yes_bid > 0 AND s.yes_ask > 0 AND s.yes_ask < 100
        ORDER BY s.ticker, s.secs_left DESC
    """, (entry_secs + window,)).fetchall()

    by_ticker: dict = {}
    for tk, secs, yb, ya, nb, na, res in rows:
        by_ticker.setdefault(tk, []).append((secs, yb, ya, nb, na, res))

    out = []
    for tk, path in by_ticker.items():
        entry = next((r for r in path if r[0] <= entry_secs), None)
        if entry is None:
            continue
        secs, yb, ya, nb, na, res = entry

        if ya is not None and ENTRY_LO <= ya <= ENTRY_HI:
            side, entry_px = "yes", ya
        elif na is not None and ENTRY_LO <= na <= ENTRY_HI:
            side, entry_px = "no", na
        else:
            continue

        fwd = []
        for r in path:
            if r[0] >= secs:
                continue
            bid = r[1] if side == "yes" else r[3]
            if bid is not None and 0 < bid < 100:
                fwd.append((r[0], bid))
        out.append((tk, side, entry_px, res, fwd))
    return out


def simulate(paths, stop_level):
    hold, stopped, n_stopped, recovered, slips = [], [], 0, 0, []

    for _tk, side, entry_px, res, fwd in paths:
        won = (res == side)
        entry_cost = entry_px + fee_per_contract(entry_px)

        hold.append((100.0 if won else 0.0) - entry_cost)

        exit_at = None
        for _secs, bid in fwd:
            if bid < stop_level:
                exit_at = bid
                break

        if exit_at is None:
            stopped.append((100.0 if won else 0.0) - entry_cost)
        else:
            n_stopped += 1
            slips.append(stop_level - exit_at)
            if won:
                recovered += 1
            stopped.append(exit_at - fee_per_contract(exit_at) - entry_cost)

    diag = {
        "n_stopped": n_stopped,
        "recovered": recovered,
        "mean_slip": (sum(slips) / len(slips)) if slips else 0.0,
        "max_slip": max(slips) if slips else 0.0,
    }
    return hold, stopped, diag


def stats(v):
    n = len(v)
    if n < 2:
        return (0.0, 0.0, 0.0, n)
    m = sum(v) / n
    var = sum((x - m) ** 2 for x in v) / (n - 1)
    se = math.sqrt(var / n)
    return (m, se, (m / se if se else 0.0), n)


def report(conn, entry_secs, window, levels):
    paths = load_paths(conn, entry_secs, window)
    print("=" * 76)
    print(f"STOP-LOSS BACKTEST — entry at {entry_secs}s, "
          f"{ENTRY_LO:.0f}-{ENTRY_HI:.0f}c favourite, BTC 15-min")
    print("=" * 76)
    if not paths:
        print("\n  No qualifying entries. Check the DB path and series.")
        return

    sides = {}
    for _t, s, _p, _r, _f in paths:
        sides[s] = sides.get(s, 0) + 1
    print(f"\n  {len(paths):,} entries   "
          f"({sides.get('yes',0):,} YES / {sides.get('no',0):,} NO)")
    avg_entry = sum(p[2] for p in paths) / len(paths)
    won = sum(1 for p in paths if p[3] == p[1])
    print(f"  mean entry price   {avg_entry:.2f}c")
    print(f"  settled in favour  {won:,} ({100.0*won/len(paths):.1f}%)")
    print(f"  fee at entry       {fee_per_contract(avg_entry):.2f}c per contract")
    print(f"  breakeven win rate {avg_entry + fee_per_contract(avg_entry):.2f}%")

    hold, _, _ = simulate(paths, -1)
    m, se, t, n = stats(hold)
    print("\n  BASELINE — hold to settlement, no stop")
    print(f"    mean P&L   {m:+.3f}c per contract")
    print(f"    95% CI     [{m-1.96*se:+.3f}, {m+1.96*se:+.3f}]c")
    print(f"    t          {t:+.2f}")

    print(f"\n  WITH A STOP  (Bonferroni threshold for {len(levels)} levels: "
          f"|t| > {abs(round(2.24 + 0.15*len(levels), 2)):.2f})")
    print(f"\n  {'stop':>6} {'mean P&L':>11} {'95% CI':>20} {'t':>7} "
          f"{'stopped':>9} {'of those, won':>14} {'slip':>7}")
    print("  " + "-" * 76)

    rows = []
    for lvl in levels:
        _, sp, d = simulate(paths, lvl)
        m2, se2, t2, _ = stats(sp)
        rows.append((lvl, m2, se2, t2, d))
        pct_stop = 100.0 * d["n_stopped"] / len(paths)
        rec = (100.0 * d["recovered"] / d["n_stopped"]) if d["n_stopped"] else 0
        print(f"  {lvl:>5.0f}c {m2:>+10.3f}c "
              f"[{m2-1.96*se2:>+7.3f},{m2+1.96*se2:>+7.3f}] {t2:>+7.2f} "
              f"{d['n_stopped']:>5,} ({pct_stop:>2.0f}%) {rec:>12.0f}% "
              f"{d['mean_slip']:>6.1f}c")

    print("\n  HOW TO READ THIS")
    print("    'stopped'        how often the stop fired")
    print("    'of those, won'  positions the stop CLOSED that would have WON.")
    print("                     Every one is a loss the stop manufactured.")
    print("    'slip'           how far below the stop level you actually got")
    print("                     out. A stop is not a price you are promised.")

    print("\n" + "=" * 76)
    print("PAIRED TEST — same markets, difference per market")
    print("=" * 76)
    print(f"\n  {'stop':>6} {'mean diff':>11} {'SE':>8} {'t':>7} "
          f"{'markets that differ':>21}")
    print("  " + "-" * 60)
    paired = []
    for lvl in levels:
        h2, s2, _d = simulate(paths, lvl)
        diffs = [a - b for a, b in zip(s2, h2)]
        nz = [d for d in diffs if abs(d) > 1e-9]
        md, sd, td, _ = stats(diffs)
        paired.append((lvl, md, sd, td, len(nz)))
        print(f"  {lvl:>5.0f}c {md:>+10.3f}c {sd:>8.3f} {td:>+7.2f} "
              f"{len(nz):>13,} of {len(diffs):,}")
    print("\n  'markets that differ' is the REAL sample size for this")
    print("  comparison. Everything else contributes an exact zero.")

    best = max(rows, key=lambda r: r[1])
    bp = max(paired, key=lambda r: r[1])
    print("\n" + "=" * 76)
    print("VERDICT")
    print("=" * 76)
    print(f"\n  hold to settlement   {m:+.3f}c  (t={t:+.2f})")
    print(f"  best stop level      {best[0]:.0f}c -> {best[1]:+.3f}c "
          f"(t={best[3]:+.2f})")
    diff = best[1] - m
    print(f"  difference           {diff:+.3f}c per contract")
    print(f"  PAIRED best          {bp[0]:.0f}c -> {bp[1]:+.3f}c  "
          f"(t={bp[3]:+.2f}, n={bp[4]})")

    if se > 0:
        print(f"\n  smallest detectable effect at this n: {1.96*se*2:.2f}c")
        need = int((1.96 * math.sqrt(len(hold)) * se / 1.0) ** 2)
        print(f"  entries needed to resolve a 1c effect: ~{need:,} "
              f"(you have {len(hold):,})")

    if diff <= 0:
        print("""
  The stop does NOT improve expected value, which is what the theory
  predicted: a binary's price already IS its probability, so exiting at 70c
  realises exactly what holding from 70c was worth — minus a second round of
  fees and spread.

  If the win rate went UP while P&L went DOWN, that is the illusion this test
  exists to expose. Win rate is not profit.""")
    else:
        print(f"""
  The stop appears to add {diff:+.3f}c. Before believing it:
    1. Is |t| above the corrected threshold, or just above 1.96?
    2. Does it survive on a held-out half of the data?
    3. Is the improvement smooth across stop levels, or one spike?
  A single level winning a sweep of {len(levels)} is what noise looks like.""")


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fair_paths(n, seed=7, steps=18, sigma=1.0):
    import random
    rng = random.Random(seed)
    out = []
    tries = 0
    while len(out) < n and tries < n * 200:
        tries += 1
        tau0 = 1.0
        x = 0.0
        k = -sigma * math.sqrt(tau0) * _norm_cdf_inv(rng.uniform(0.90, 0.96))
        price0 = _norm_cdf((x - k) / (sigma * math.sqrt(tau0))) * 100.0
        if not (ENTRY_LO <= price0 <= ENTRY_HI):
            continue

        fwd = []
        dt = tau0 / steps
        for i in range(1, steps):
            x += rng.gauss(0.0, sigma * math.sqrt(dt))
            tau = tau0 - i * dt
            p = _norm_cdf((x - k) / (sigma * math.sqrt(tau))) * 100.0
            fwd.append((180.0 * tau / tau0, max(0.5, min(99.5, p))))
        x += rng.gauss(0.0, sigma * math.sqrt(dt))
        out.append(("T", "yes", price0, "yes" if x > k else "no", fwd))
    return out


def _norm_cdf_inv(p):
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > ph:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q, r = p - 0.5, (p - 0.5) ** 2
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def self_test():
    print("SELF-TEST — true martingale world\n")

    paths = fair_paths(6000)
    entry_avg = sum(p[2] for p in paths) / len(paths)
    won = sum(1 for p in paths if p[3] == p[1])
    expected_fee = fee_per_contract(entry_avg)

    print(f"  {len(paths):,} synthetic paths, mean entry {entry_avg:.2f}c")
    print(f"  settled in favour {100.0*won/len(paths):.1f}% "
          f"(should be ~{entry_avg:.1f}%)")

    hold, _, _ = simulate(paths, -1)
    mh, seh, _, _ = stats(hold)
    print(f"\n  hold to settle  {mh:+.3f}c  (SE {seh:.3f})")
    print(f"  expected        {-expected_fee:+.3f}c  (the fee, nothing else)")

    assert abs(mh + expected_fee) < 4 * seh, \
        f"generator is biased: {mh:+.3f}c vs expected {-expected_fee:+.3f}c"
    print("  [ok] baseline is -fee — the generator is a true martingale")

    for lvl in (80, 70, 60):
        _, sp, d = simulate(paths, lvl)
        ms, ses, _, _ = stats(sp)
        joint = 2 * math.sqrt(seh**2 + ses**2)
        assert ms <= mh + joint, f"stop {lvl} beat hold: {ms:+.3f} vs {mh:+.3f}"
        print(f"  [ok] stop {lvl}c: {ms:+.3f}c, stopped "
              f"{100.0*d['n_stopped']/len(paths):.0f}%, of those "
              f"{100.0*d['recovered']/max(d['n_stopped'],1):.0f}% would have won"
              f" — does not beat hold")

    assert abs(fee_per_contract(92.0) - 1.0) < 0.51, fee_per_contract(92.0)
    print(f"  [ok] fee at 92c = {fee_per_contract(92.0):.2f}c per contract")
    print("\nALL PASS — the simulator recovers the known answer.\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--entry-secs", type=float, default=180.0)
    ap.add_argument("--window", type=float, default=20.0,
                    help="how far before entry-secs to start looking")
    ap.add_argument("--stops", type=float, nargs="*",
                    default=[80, 75, 70, 65, 60, 50])
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        self_test()
        sys.exit(0)

    conn = sqlite3.connect(a.db, timeout=10)
    report(conn, a.entry_secs, a.window, a.stops)
