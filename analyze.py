
from __future__ import annotations

import argparse
import math
import sqlite3
from collections import defaultdict

TAKER_RATE = 0.07
MAKER_RATE = 0.0


def taker_fee(p: float) -> float:
    return TAKER_RATE * p * (1 - p)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z**2 / n
    c = p + z**2 / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    return ((c - m) / d, (c + m) / d)


def hdr(title: str) -> None:
    print(f"\n{'='*74}\n{title}\n{'='*74}")


def honest_se(diffs: list[float], prices: list[float]) -> tuple[float, float]:
    n = len(diffs)
    if n < 2:
        return (1.0, 0.5)
    mean = sum(diffs) / n
    sample_var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    p_bar = sum(prices) / n / 100.0
    theoretical_var = max(p_bar * (1 - p_bar), 1e-6)
    var = max(sample_var, theoretical_var)
    return (math.sqrt(var / n), math.sqrt(var))


def q1_settlement_rate(conn, lo=90, hi=96, tmin=180, tmax=360):
    hdr(f"Q1. Do {lo}-{hi}c contracts at {tmin//60}-{tmax//60} min beat breakeven?")

    rows = conn.execute(
        """
        SELECT s.ticker, s.yes_bid, s.yes_ask, s.secs_left, t.result
        FROM snapshots s
        JOIN settlements t ON t.ticker = s.ticker
        WHERE s.secs_left BETWEEN ? AND ?
          AND s.yes_bid IS NOT NULL AND s.yes_ask IS NOT NULL
          AND s.yes_bid > 0 AND s.yes_ask < 100
          AND t.result IN ('yes','no')
        """,
        (tmin, tmax),
    ).fetchall()

    if not rows:
        print("  No matching observations yet. Keep the recorder running.")
        return

    best: dict[tuple, tuple] = {}
    centre = (tmin + tmax) / 2
    for ticker, yb, ya, secs, result in rows:
        for side in ("YES", "NO"):
            price = yb if side == "YES" else 100 - ya
            won = (result == "yes") if side == "YES" else (result == "no")
            if not (lo <= price <= hi):
                continue
            key = (ticker, side)
            d = abs(secs - centre)
            if key not in best or d < best[key][0]:
                best[key] = (d, price, won)

    if not best:
        print(f"  No contracts landed in the {lo}-{hi}c band yet.")
        return

    diffs, prices = [], []
    buckets = defaultdict(lambda: [0, 0])
    for _, price, won in best.values():
        p = price / 100.0
        diffs.append((1.0 if won else 0.0) - (p + MAKER_RATE))
        prices.append(price)
        b = (int(price) // 2) * 2
        buckets[b][1] += 1
        if won:
            buckets[b][0] += 1

    n = len(diffs)
    mean = sum(diffs) / n
    se, sd = honest_se(diffs, prices)
    var = sd**2
    ci_lo, ci_hi = mean - 1.96 * se, mean + 1.96 * se

    print(f"\n  {n:,} observations (deduped by ticker+side)\n")
    print("  Per-bucket detail (diagnostic only):")
    print(f"  {'price':>7} {'n':>6} {'settled YES':>12} {'breakeven':>10} {'95% CI':>17}")
    print("  " + "-" * 58)
    for b in sorted(buckets):
        k, bn = buckets[b]
        if bn < 5:
            continue
        lo_ci, hi_ci = wilson(k, bn)
        print(f"  {b:>4}-{b+2:<2} {bn:>6} {k/bn:>11.1%} {(b+1)/100:>10.1%} "
              f"[{lo_ci:>6.1%},{hi_ci:>6.1%}]")

    print(f"\n  HEADLINE — mean edge per contract (outcome minus price):")
    print(f"    mean edge   {mean*100:>+8.3f} pt")
    print(f"    std error   {se*100:>8.3f} pt")
    print(f"    95% CI      [{ci_lo*100:>+.3f}, {ci_hi*100:>+.3f}] pt")
    print(f"    t-stat      {mean/se if se else 0:>8.2f}")

    detectable = 1.96 * sd / math.sqrt(n) if n else 1.0
    need_1pt = (1.96 * sd / 0.01) ** 2
    print(f"\n  POWER:")
    print(f"    smallest edge this sample can detect: {detectable*100:.2f}pt")
    print(f"    observations needed for a 1.00pt edge: {need_1pt:,.0f}")
    if n < need_1pt:
        print(f"    -> you have {n:,}. Need ~{need_1pt-n:,.0f} more for 1pt resolution.")

    print(f"\n  VERDICT:")
    if n < 300:
        print(f"    Far too little data — {n} observations. Keep recording.")
    elif ci_lo > 0:
        print(f"    EDGE FOUND. Mean edge {mean*100:+.2f}pt, CI excludes zero.")
        print(f"    Expected profit per $30 position: ${mean*30:.3f}")
    elif ci_hi < 0:
        print(f"    NEGATIVE EDGE. Mean {mean*100:+.2f}pt, CI entirely below zero.")
        print(f"    Favourites are OVERpriced here. Kill the strategy.")
    else:
        print(f"    INCONCLUSIVE. CI [{ci_lo*100:+.2f},{ci_hi*100:+.2f}]pt straddles zero.")
        print(f"    Most likely reading: fairly priced. Collect more, or stop.")


def q1b_window_sweep(conn, lo=90, hi=96):
    hdr("Q1b. Edge by TIME WINDOW — which entry point is actually best?")

    windows = [(30, 60), (60, 90), (90, 120), (120, 180),
               (180, 240), (240, 300), (300, 420), (420, 600)]
    k = len(windows)
    z_corrected = 2.734

    print(f"\n  Testing {k} windows. Bonferroni-corrected threshold: z > {z_corrected:.2f}")
    print(f"  (uncorrected would be 1.96 — using that across {k} tests manufactures")
    print(f"   false positives roughly 34% of the time)\n")

    print(f"  {'window':>12} {'n':>6} {'mean edge':>11} {'SE':>7} {'z':>7} "
          f"{'fills OK?':>10} {'verdict':>12}")
    print("  " + "-" * 72)

    results = []
    for tmin, tmax in windows:
        rows = conn.execute(
            """SELECT s.ticker, s.yes_bid, s.yes_ask, t.result
               FROM snapshots s JOIN settlements t ON t.ticker = s.ticker
               WHERE s.secs_left BETWEEN ? AND ?
                 AND s.yes_bid IS NOT NULL AND s.yes_ask IS NOT NULL
                 AND s.yes_bid > 0 AND s.yes_ask < 100
                 AND t.result IN ('yes','no')""",
            (tmin, tmax),
        ).fetchall()

        best: dict = {}
        for ticker, yb, ya, result in rows:
            for side in ("YES", "NO"):
                price = yb if side == "YES" else 100 - ya
                won = (result == "yes") if side == "YES" else (result == "no")
                if lo <= price <= hi:
                    best[(ticker, side)] = (
                        (1.0 if won else 0.0) - price / 100.0, price)

        n = len(best)
        if n < 100:
            print(f"  {tmin:>4}-{tmax:<4}s {n:>6} {'need 100+':>40}")
            continue

        d = [v[0] for v in best.values()]
        pr = [v[1] for v in best.values()]
        mean = sum(d) / n
        se, _ = honest_se(d, pr)
        z = mean / se if se else 0.0

        quoted = conn.execute(
            """SELECT AVG(CASE WHEN yes_bid > 0 AND yes_ask < 100 THEN 1.0 ELSE 0 END)
               FROM snapshots WHERE secs_left BETWEEN ? AND ?""", (tmin, tmax)
        ).fetchone()[0] or 0

        if z > z_corrected:
            verdict = "EDGE"
        elif z < -z_corrected:
            verdict = "NEGATIVE"
        else:
            verdict = "—"

        print(f"  {tmin:>4}-{tmax:<4}s {n:>6} {mean*100:>+10.2f}pt {se*100:>6.2f} "
              f"{z:>7.2f} {quoted:>9.0%} {verdict:>12}")
        results.append((tmin, tmax, mean, se, z, n, quoted))

    if not results:
        print("\n  Not enough data yet in any window. Keep recording.")
        return

    sig = [r for r in results if abs(r[4]) > z_corrected]
    print(f"\n  {len(sig)} of {len(results)} windows significant after correction.")
    if sig:
        b = max(sig, key=lambda r: r[2])
        print(f"\n  BEST TRADEABLE WINDOW: {b[0]}-{b[1]}s")
        print(f"    edge {b[2]*100:+.2f}pt (z={b[4]:.2f}, n={b[5]:,}), "
              f"two-sided quotes {b[6]:.0%} of the time")
        print(f"    -> python sizing.py --edge {b[2]*100:.2f} --se {b[3]*100:.2f}")
        print("\n  Before trusting it: does the edge rise smoothly toward expiry, as the")
        print("  averaging model predicts? A single isolated window looking good with")
        print("  neighbours flat is far more likely noise than signal.")
    else:
        print("  No window clears the corrected bar. Either no edge, or not enough data.")


def q1c_per_coin(conn, lo=90, hi=96, tmin=180, tmax=360):
    hdr("Q1c. Edge BY COIN — is any single instrument mispriced?")

    z_corr = 2.690

    rows = conn.execute(
        """SELECT s.series, s.ticker, s.yes_bid, s.yes_ask, t.result
           FROM snapshots s JOIN settlements t ON t.ticker = s.ticker
           WHERE s.secs_left BETWEEN ? AND ?
             AND s.yes_bid IS NOT NULL AND s.yes_ask IS NOT NULL
             AND s.yes_bid > 0 AND s.yes_ask < 100
             AND t.result IN ('yes','no')""",
        (tmin, tmax),
    ).fetchall()

    per: dict = defaultdict(dict)
    for series, ticker, yb, ya, result in rows:
        for side in ("YES", "NO"):
            price = yb if side == "YES" else 100 - ya
            won = (result == "yes") if side == "YES" else (result == "no")
            if lo <= price <= hi:
                per[series][(ticker, side)] = (
                    (1.0 if won else 0.0) - price / 100.0, price)

    if not per:
        print("  No in-band observations yet.")
        return

    print(f"\n  Bonferroni threshold for 7 coins: z > {z_corr:.2f}\n")
    print(f"  {'coin':>11} {'n':>6} {'won':>5} {'lost':>5} {'mean edge':>11} "
          f"{'SE':>7} {'z':>7} {'verdict':>10}")
    print("  " + "-" * 68)

    for series in sorted(per):
        vals = list(per[series].values())
        n = len(vals)
        d = [v[0] for v in vals]
        pr = [v[1] for v in vals]
        won = sum(1 for x, p in vals if x > 0)
        if n < 30:
            print(f"  {series:>11} {n:>6} {won:>5} {n-won:>5} {'need 30+':>36}")
            continue
        mean = sum(d) / n
        se, _ = honest_se(d, pr)
        z = mean / se if se else 0.0
        v = "EDGE" if z > z_corr else ("NEGATIVE" if z < -z_corr else "—")
        print(f"  {series:>11} {n:>6} {won:>5} {n-won:>5} {mean*100:>+10.2f}pt "
              f"{se*100:>6.2f} {z:>7.2f} {v:>10}")

    print("""
  How to read this: if ONE coin shows an edge and six don't, be sceptical — that's
  what noise looks like when you test seven things. If the thin alts (DOGE, XRP,
  BNB, HYPE) lean positive TOGETHER while BTC/ETH sit at zero, that's a coherent
  story about market-maker attention and worth taking seriously.

  Pattern across related instruments beats significance in any single one.""")


def q5b_calibration_by_depth(conn, tmin=120, tmax=600):
    hdr("Q5b. IS THE CALIBRATION CURVE REAL, OR JUST STALE QUOTES?")

    rows = conn.execute(
        """SELECT s.ticker, s.yes_bid, s.yes_ask, s.yes_bid_size,
                  s.yes_ask_size, t.result
           FROM snapshots s JOIN settlements t ON t.ticker = s.ticker
           WHERE s.secs_left BETWEEN ? AND ?
             AND s.yes_bid > 0 AND s.yes_ask < 100
             AND t.result IN ('yes','no')""", (tmin, tmax)).fetchall()

    if not rows:
        print("  No data.")
        return

    obs = {}
    for ticker, yb, ya, bs, asz, result in rows:
        price = (yb + ya) / 2
        depth = min(bs or 0, asz or 0)
        obs.setdefault(ticker, (price, result == "yes", depth * price / 100.0))

    bands = [(1, 20, "cheap 1-20c"), (20, 50, "20-50c"),
             (50, 80, "50-80c"), (80, 99, "dear 80-99c")]

    depths = [(0, 25, "under $25"), (25, 250, "$25-250"), (250, 1e12, "$250+")]

    print(f"\n  {len(obs):,} settled markets. Deviation (settled% minus price):\n")
    print(f"  {'depth':>12} " + " ".join(f"{b[2]:>14}" for b in bands))
    print("  " + "-" * 74)

    for dlo, dhi, dlabel in depths:
        cells = []
        for blo, bhi, _ in bands:
            sel = [(p, w) for p, w, d in obs.values()
                   if blo <= p < bhi and dlo <= d < dhi]
            n = len(sel)
            if n < 30:
                cells.append(f"{'n='+str(n):>14}")
                continue
            ap = sum(p for p, _ in sel) / n / 100
            rate = sum(1 for _, w in sel if w) / n
            cells.append(f"{(rate-ap)*100:>+8.2f}[{n:>4}]")
        print(f"  {dlabel:>12} " + " ".join(cells))

    print("""
  READ THE ROWS:

    If the outward tilt (negative on the left, positive on the right) appears
    in the DEEP row too, the favourite-longshot bias is real and tradeable.

    If it appears only in the thin row and flattens as depth grows, it is the
    staleness artefact wearing the costume of a famous anomaly — and the two
    would have been impossible to tell apart without depth data.""")


def q5_calibration_curve(conn, tmin=120, tmax=600):
    hdr("Q5. CALIBRATION CURVE — is the market well-calibrated at EVERY price?")

    rows = conn.execute(
        """SELECT s.ticker, s.yes_bid, s.yes_ask, t.result
           FROM snapshots s JOIN settlements t ON t.ticker = s.ticker
           WHERE s.secs_left BETWEEN ? AND ?
             AND s.yes_bid IS NOT NULL AND s.yes_ask IS NOT NULL
             AND s.yes_bid > 0 AND s.yes_ask < 100
             AND t.result IN ('yes','no')""",
        (tmin, tmax),
    ).fetchall()

    if not rows:
        print("  No data yet.")
        return

    obs: dict = {}
    for ticker, yb, ya, result in rows:
        for side in ("YES", "NO"):
            price = yb if side == "YES" else 100 - ya
            won = (result == "yes") if side == "YES" else (result == "no")
            if 1 <= price <= 99:
                obs.setdefault((ticker, side), (price, won))

    bands = [(1, 10), (10, 20), (20, 30), (30, 40), (40, 50),
             (50, 60), (60, 70), (70, 80), (80, 90), (90, 95), (95, 99)]

    print(f"\n  {len(obs):,} observations, {tmin}-{tmax}s to close\n")
    print(f"  {'price band':>12} {'n':>6} {'avg price':>10} {'settled YES':>12} "
          f"{'deviation':>11} {'z':>7}")
    print("  " + "-" * 64)

    flagged = []
    for lo_b, hi_b in bands:
        sel = [(p, w) for p, w in obs.values() if lo_b <= p < hi_b]
        n = len(sel)
        if n < 30:
            print(f"  {lo_b:>5}-{hi_b:<6} {n:>6} {'need 30+':>42}")
            continue
        avg_p = sum(p for p, _ in sel) / n / 100.0
        rate = sum(1 for _, w in sel if w) / n
        dev = rate - avg_p
        se = math.sqrt(max(avg_p * (1 - avg_p), 1e-6) / n)
        z = dev / se if se else 0.0
        bar = "#" * min(20, int(abs(dev) * 200))
        sign = "+" if dev > 0 else "-"
        print(f"  {lo_b:>5}-{hi_b:<6} {n:>6} {avg_p:>9.1%} {rate:>11.1%} "
              f"{dev*100:>+10.2f}pt {z:>7.2f}  {sign}{bar}")
        flagged.append((lo_b, hi_b, n, dev, z))

    if not flagged:
        print("\n  Not enough data in any band yet.")
        return

    k = len(flagged)
    z_corr = {1: 1.96, 2: 2.24, 3: 2.39, 4: 2.50, 5: 2.58,
              6: 2.64, 7: 2.69, 8: 2.73, 9: 2.77, 10: 2.81, 11: 2.84}.get(k, 2.9)
    sig = [f for f in flagged if abs(f[4]) > z_corr]

    print(f"\n  Bonferroni threshold for {k} bands: |z| > {z_corr:.2f}")
    if sig:
        print(f"  {len(sig)} band(s) significantly mispriced:")
        for lo_b, hi_b, n, dev, z in sig:
            direction = "UNDERpriced (buy)" if dev > 0 else "OVERpriced (sell/fade)"
            print(f"    {lo_b}-{hi_b}c: {dev*100:+.2f}pt, z={z:.2f}, n={n} -> {direction}")
        print("\n  Before believing it: is the deviation part of a SMOOTH pattern across")
        print("  neighbouring bands, or one isolated spike? Smooth = mechanism.")
        print("  Isolated = almost certainly noise, even after correction.")
    else:
        print("  No band significantly mispriced. Consistent with an efficient market.")


def q6_gradient_and_holdout(conn, lo=90, hi=96):
    hdr("Q6. GRADIENT TEST + OUT-OF-SAMPLE HOLDOUT")

    def Phi(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    def Phinv(p):
        a, b = -9.0, 9.0
        for _ in range(200):
            m = (a + b) / 2
            if Phi(m) < p:
                a = m
            else:
                b = m
        return (a + b) / 2

    z91 = Phinv(0.91)

    def model_edge(tau):
        ratio = math.sqrt((tau - 40) / tau) if tau > 60 else tau / math.sqrt(10800)
        return (Phi(z91 / ratio) - 0.91) * 100

    windows = [(30, 60), (60, 90), (90, 120), (120, 180),
               (180, 240), (240, 300), (300, 420), (420, 600)]

    span = conn.execute("SELECT MIN(ts), MAX(ts) FROM snapshots").fetchone()
    if not span or span[0] is None:
        print("  No data.")
        return
    midpoint = (span[0] + span[1]) / 2

    def fit(ts_lo: float, ts_hi: float, label: str):
        pts = []
        for tmin, tmax in windows:
            rows = conn.execute(
                """SELECT s.ticker, s.yes_bid, s.yes_ask, t.result
                   FROM snapshots s JOIN settlements t ON t.ticker = s.ticker
                   WHERE s.secs_left BETWEEN ? AND ?
                     AND s.ts >= ? AND s.ts < ?
                     AND s.yes_bid IS NOT NULL AND s.yes_ask IS NOT NULL
                     AND s.yes_bid > 0 AND s.yes_ask < 100
                     AND t.result IN ('yes','no')""",
                (tmin, tmax, ts_lo, ts_hi)).fetchall()
            best = {}
            for ticker, yb, ya, result in rows:
                for side in ("YES", "NO"):
                    price = yb if side == "YES" else 100 - ya
                    won = (result == "yes") if side == "YES" else (result == "no")
                    if lo <= price <= hi:
                        best[(ticker, side)] = (
                            (1.0 if won else 0.0) - price / 100.0, price)
            n = len(best)
            if n < 40:
                continue
            d = [v[0] for v in best.values()]
            pr = [v[1] for v in best.values()]
            mean = sum(d) / n * 100
            se, _ = honest_se(d, pr)
            pts.append((model_edge((tmin + tmax) / 2), mean, se * 100, n))

        if len(pts) < 4:
            print(f"\n  {label}: only {len(pts)} usable windows — need 4+. Keep recording.")
            return None

        X = [p[0] for p in pts]
        Y = [p[1] for p in pts]
        W = [1 / p[2] ** 2 for p in pts]
        sw = sum(W)
        mx = sum(w * x for w, x in zip(W, X)) / sw
        my = sum(w * y for w, y in zip(W, Y)) / sw
        sxx = sum(w * (x - mx) ** 2 for w, x in zip(W, X))
        sxy = sum(w * (x - mx) * (y - my) for w, x, y in zip(W, X, Y))
        if sxx <= 0:
            return None
        slope = sxy / sxx
        inter = my - slope * mx
        dof = len(pts) - 2
        resid = [y - (inter + slope * x) for x, y in zip(X, Y)]
        s2 = sum(w * r * r for w, r in zip(W, resid)) / dof
        se_slope = math.sqrt(s2 / sxx)
        t = slope / se_slope if se_slope else 0.0

        print(f"\n  {label}  ({sum(p[3] for p in pts):,} observations, "
              f"{len(pts)} windows)")
        print(f"    slope      {slope:>+7.3f}  (0 = fully priced, 1 = fully available)")
        print(f"    intercept  {inter:>+7.3f}pt")
        print(f"    t-stat     {t:>7.2f}  dof={dof}")
        return slope, t, inter

    print("\n  FULL SAMPLE (the post-hoc fit — treat with suspicion):")
    full = fit(0, 9e18, "  full")

    print("\n" + "  " + "-" * 66)
    print("  OUT-OF-SAMPLE CHECK — fit the shape in each half independently")
    print("  " + "-" * 66)
    first = fit(0, midpoint, "  FIRST half")
    second = fit(midpoint, 9e18, "  SECOND half")

    print("\n  VERDICT:")
    if not (first and second):
        print("    Not enough data in both halves yet. This is the test that")
        print("    matters — keep collecting until both halves have 4+ windows.")
        return
    s1, t1, _ = first
    s2, t2, _ = second
    same_sign = (s1 > 0) == (s2 > 0)
    both_strong = t1 > 2.0 and t2 > 2.0
    if same_sign and both_strong:
        print(f"    REPLICATES. Slope {s1:+.2f} then {s2:+.2f}, both significant.")
        print("    The gradient appears independently in data collected at")
        print("    different times. That is what a real mechanism looks like.")
        print("    -> Worth paper trading. NOT worth real money yet.")
    elif same_sign:
        print(f"    Same direction ({s1:+.2f}, {s2:+.2f}) but not both significant.")
        print("    Suggestive, underpowered. Keep collecting.")
    else:
        print(f"    DOES NOT REPLICATE. Slope {s1:+.2f} then {s2:+.2f} — signs differ.")
        print("    The full-sample trend was almost certainly data-mined.")
        print("    This is the check doing its job. Do not trade this.")


def q7_stale_quote_test(conn, lo=90, hi=96):
    hdr("Q7. STALE-QUOTE TEST — is the near-expiry edge even real?")

    rows = conn.execute(
        """SELECT s.ticker, s.ts, s.secs_left, s.yes_bid, s.yes_ask,
                  s.yes_bid_size, s.yes_ask_size, t.result
           FROM snapshots s JOIN settlements t ON t.ticker = s.ticker
           WHERE s.secs_left BETWEEN 20 AND 120
             AND s.yes_bid IS NOT NULL AND s.yes_ask IS NOT NULL
             AND s.yes_bid > 0 AND s.yes_ask < 100
             AND t.result IN ('yes','no')
           ORDER BY s.ticker, s.ts"""
    ).fetchall()

    if len(rows) < 100:
        print(f"  Only {len(rows)} near-expiry rows. Need more data.")
        return

    obs = []
    prev_ticker = None
    last_quote = None
    last_change_ts = None
    for ticker, ts, secs, yb, ya, ybs, yas, result in rows:
        if ticker != prev_ticker:
            prev_ticker, last_quote, last_change_ts = ticker, (yb, ya), ts
        if (yb, ya) != last_quote:
            last_quote, last_change_ts = (yb, ya), ts
        frozen_for = ts - last_change_ts

        for side in ("YES", "NO"):
            price = yb if side == "YES" else 100 - ya
            size = ybs if side == "YES" else yas
            won = (result == "yes") if side == "YES" else (result == "no")
            if lo <= price <= hi:
                obs.append((frozen_for, size or 0, price,
                            (1.0 if won else 0.0) - price / 100.0))

    if len(obs) < 50:
        print(f"  Only {len(obs)} in-band. Need more.")
        return

    def report(label, sel):
        n = len(sel)
        if n < 20:
            print(f"  {label:<34} {n:>5}  {'need 20+':>28}")
            return
        d = [x[3] for x in sel]
        pr = [x[2] for x in sel]
        mean = sum(d) / n * 100
        se, _ = honest_se(d, pr)
        z = mean / (se * 100) if se else 0
        print(f"  {label:<34} {n:>5} {mean:>+9.2f}pt {se*100:>7.2f} {z:>7.2f}")

    print(f"\n  {len(obs):,} in-band observations at 20-120s to close\n")
    print(f"  {'group':<34} {'n':>5} {'mean edge':>11} {'SE':>7} {'z':>7}")
    print("  " + "-" * 66)

    print("  BY QUOTE FRESHNESS (seconds since bid/ask last moved)")
    report("    moving (<3s since change)", [o for o in obs if o[0] < 3])
    report("    slow (3-10s)", [o for o in obs if 3 <= o[0] < 10])
    report("    FROZEN (10s+)", [o for o in obs if o[0] >= 10])

    print("\n  BY RESTING DEPTH behind the quote")
    report("    thin (<100 contracts)", [o for o in obs if o[1] < 100])
    report("    medium (100-1000)", [o for o in obs if 100 <= o[1] < 1000])
    report("    deep (1000+)", [o for o in obs if o[1] >= 1000])

    print("\n  TRADEABLE SUBSET — fresh AND deep")
    tradeable = [o for o in obs if o[0] < 3 and o[1] >= 1000]
    report("    moving quote, 1000+ resting", tradeable)

    print("""
  HOW TO READ THIS:

    If the edge lives in FROZEN or THIN quotes, it is an artefact. Those prices
    were not available to trade — you cannot buy from a maker who has left.

    If the edge survives in the fresh-and-deep subset, it is a real price that
    real size was resting behind, and it deserves paper trading.

    The tradeable subset is the ONLY number that matters for whether you can
    make money. Everything else is measurement.""")


def q2_book_open(conn):
    hdr("Q2. Is the book open near the close? (gates the final-minute variant)")

    rows = conn.execute(
        """SELECT secs_left, status, yes_bid, yes_ask FROM snapshots
           WHERE secs_left BETWEEN -10 AND 130"""
    ).fetchall()
    if not rows:
        print("  No near-close snapshots yet.")
        return

    buckets = defaultdict(lambda: {"dec_n": 0, "dec_q": 0, "und_n": 0, "und_q": 0})
    for secs, status, yb, ya in rows:
        b = int(secs // 15) * 15
        d = buckets[b]
        quoted = bool(yb and ya and yb > 0 and ya < 100)
        mid = ((yb + ya) / 2) if (yb is not None and ya is not None) else None
        decided = mid is not None and (mid >= 97 or mid <= 3)
        if decided:
            d["dec_n"] += 1
            d["dec_q"] += quoted
        else:
            d["und_n"] += 1
            d["und_q"] += quoted

    print("\n  Split by whether the outcome was already decided (mid >=97c or <=3c):\n")
    print(f"  {'secs left':>12} {'DECIDED n':>10} {'quoted':>8}   "
          f"{'UNDECIDED n':>12} {'quoted':>8}")
    print("  " + "-" * 60)
    for b in sorted(buckets, reverse=True):
        d = buckets[b]
        dq = f"{d['dec_q']/d['dec_n']:.0%}" if d["dec_n"] else "—"
        uq = f"{d['und_q']/d['und_n']:.0%}" if d["und_n"] else "—"
        print(f"  {b:>5}-{b+15:<6} {d['dec_n']:>10} {dq:>8}   "
              f"{d['und_n']:>12} {uq:>8}")

    print("""
  THIS IS THE COLUMN THAT MATTERS: 'UNDECIDED quoted'.

  If decided markets stop quoting but undecided ones stay live to the close, then
  the book only empties when there's nothing left to trade — and the final minute
  IS reachable whenever the outcome is genuinely in doubt.

  That would mean the +4 to +8pt edge near expiry is tradeable after all, and my
  3-6 minute cap was leaving most of it on the table.""")


def q3_strike_linkage(conn):
    hdr("Q3. Is the next strike the previous settlement value?")

    rows = conn.execute(
        """SELECT series, close_ts, expiration_value, floor_strike, ticker
           FROM settlements
           WHERE expiration_value IS NOT NULL AND close_ts IS NOT NULL
           ORDER BY series, close_ts"""
    ).fetchall()
    if len(rows) < 4:
        print("  Need at least a few settled markets. Keep recording.")
        return

    by_series = defaultdict(list)
    for series, cts, ev, fs, tk in rows:
        by_series[series].append((cts, ev, fs, tk))

    matches = total = 0
    diffs = []
    for series, items in by_series.items():
        for (cts, ev, fs, _), (ncts, nev, nfs, _) in zip(items, items[1:]):
            if nfs is None or ev is None:
                continue
            if not (0 < ncts - cts < 20 * 60):
                continue
            total += 1
            d = abs(nfs - ev)
            diffs.append(d)
            if d < 0.51:
                matches += 1

    if not total:
        print("  No consecutive window pairs yet.")
        return

    diffs.sort()
    print(f"\n  {total} consecutive window pairs")
    print(f"  next floor_strike == prev expiration_value: {matches}/{total} "
          f"({matches/total:.0%})")
    print(f"  median |difference|: ${diffs[len(diffs)//2]:,.2f}")
    if matches / total > 0.9:
        print("\n  CONFIRMED — your observation was right. The strike is the previous")
        print("  60-second average, so windows open tilted toward the last minute's move.")
    else:
        print("\n  NOT a direct link. Kalshi likely bands the strike to a round number.")
        print("  The opening-bias idea needs re-derivation.")


def q4_spreads(conn):
    hdr("Q4. Spread by time remaining (can resting orders realistically fill?)")

    rows = conn.execute(
        """SELECT series, secs_left, yes_bid, yes_ask FROM snapshots
           WHERE yes_bid IS NOT NULL AND yes_ask IS NOT NULL
             AND yes_bid > 0 AND yes_ask < 100 AND secs_left BETWEEN 0 AND 900"""
    ).fetchall()
    if not rows:
        print("  No data yet.")
        return

    buckets = defaultdict(list)
    for series, secs, yb, ya in rows:
        buckets[(series, int(secs // 60))].append(ya - yb)

    series_list = sorted({s for s, _ in buckets})
    mins = sorted({m for _, m in buckets}, reverse=True)

    print(f"\n  Median spread in cents\n")
    print(f"  {'min left':>9} " + " ".join(f"{s[:9]:>10}" for s in series_list))
    print("  " + "-" * (10 + 11 * len(series_list)))
    for m in mins:
        cells = []
        for s in series_list:
            v = buckets.get((s, m))
            if v:
                v = sorted(v)
                cells.append(f"{v[len(v)//2]:>10.1f}")
            else:
                cells.append(f"{'-':>10}")
        print(f"  {m:>9} " + " ".join(cells))
    print("\n  A consistent 2c+ spread at 3-6 min means resting orders rarely fill")
    print("  at a useful price — that would be a serious problem for the strategy.")


def summary(conn):
    hdr("DATA COLLECTED")
    n_snap = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    n_sett = conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
    n_tick = conn.execute("SELECT COUNT(DISTINCT ticker) FROM snapshots").fetchone()[0]
    span = conn.execute("SELECT MIN(ts), MAX(ts) FROM snapshots").fetchone()
    hours = (span[1] - span[0]) / 3600 if span[0] else 0
    print(f"  snapshots      {n_snap:>10,}")
    print(f"  distinct mkts  {n_tick:>10,}")
    print(f"  settlements    {n_sett:>10,}")
    print(f"  time span      {hours:>10.1f} hours")
    if n_sett < 100:
        print(f"\n  Need ~300 settled observations in-band for a real answer.")
        print(f"  At 96 windows/day/coin you should get there in a few days.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="kalshi_15m.db")
    ap.add_argument("--lo", type=int, default=90)
    ap.add_argument("--hi", type=int, default=96)
    ap.add_argument("--tmin", type=int, default=180)
    ap.add_argument("--tmax", type=int, default=360)
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    summary(conn)
    q1_settlement_rate(conn, a.lo, a.hi, a.tmin, a.tmax)
    q1b_window_sweep(conn, a.lo, a.hi)
    q1c_per_coin(conn, a.lo, a.hi, a.tmin, a.tmax)
    q5_calibration_curve(conn)
    q5b_calibration_by_depth(conn)
    q6_gradient_and_holdout(conn, a.lo, a.hi)
    q7_stale_quote_test(conn, a.lo, a.hi)
    q2_book_open(conn)
    q3_strike_linkage(conn)
    q4_spreads(conn)
    print()
