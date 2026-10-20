
import os
import random
import sqlite3
import subprocess
import sys

from recorder import SCHEMA

random.seed(20)


def build(path: str, edge_pt: float, n_windows: int = 4000, close_at: float = 45.0,
          spread_c: int = 0):
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)

    base_ts = 1_800_000_000.0
    prev_ev = 100_000.0

    snaps, setts = [], []
    for w in range(n_windows):
        series = "KXBTC15M"
        ticker = f"KXBTC15M-W{w:05d}"
        close_ts = base_ts + w * 900

        strike = round(prev_ev, 2)

        price = random.randint(88, 97)
        true_p = min(0.999, max(0.001, price / 100 + edge_pt / 100))
        won = random.random() < true_p

        ev = strike + abs(random.gauss(0, 120)) * (1 if won else -1)
        prev_ev = ev

        setts.append((ticker, series, close_ts, "yes" if won else "no", ev, strike, close_ts))

        for secs in [600, 420, 360, 300, 240, 180, 120, 90, 60, 45, 30, 15, 5]:
            ts = close_ts - secs
            book_open = secs > close_at
            yb = price - (spread_c // 2 + spread_c % 2)
            ya = price + spread_c // 2
            if not book_open:
                yb, ya, status = None, None, "closed"
            else:
                status = "open"
            snaps.append((
                ts, ticker, series, close_ts, secs, status,
                yb, ya,
                (100 - ya) if ya else None, (100 - yb) if yb else None,
                1000, 500, strike, price,
            ))

    conn.executemany(
        """INSERT INTO snapshots
           (ts,ticker,series,close_ts,secs_left,status,yes_bid,yes_ask,
            no_bid,no_ask,volume,open_interest,floor_strike,last_price)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", snaps)
    conn.executemany(
        """INSERT INTO settlements
           (ticker,series,close_ts,result,expiration_value,floor_strike,recorded_ts)
           VALUES (?,?,?,?,?,?,?)""", setts)
    conn.commit()
    conn.close()


def run(path):
    r = subprocess.run([sys.executable, "analyze.py", "--db", path],
                       capture_output=True, text=True)
    return r.stdout + r.stderr


if __name__ == "__main__":
    TMP = os.environ.get("TMPDIR", "/tmp")
    cases = [
        (os.path.join(TMP, "world_fair.db"),  0.0, "FAIR — analyzer must find NO edge"),
        (os.path.join(TMP, "world_edge.db"),  2.0, "EDGE +2pt — analyzer must FIND it"),
        (os.path.join(TMP, "world_neg.db"),  -2.0, "NEGATIVE -2pt — analyzer must flag it"),
    ]

    spread_case = (os.path.join(TMP, "world_spread.db"), 0.0,
                   "FAIR MID + 2c SPREAD — analyzer must recover half-spread capture")

    results = {}
    for path, edge, label in cases + [spread_case]:
        print(f"\n{'#'*74}\n# {label}\n{'#'*74}")
        build(path, edge, spread_c=(2 if path == spread_case[0] else 0))
        out = run(path)
        block = out.split("Q1.")[1].split("=====")[0] if "Q1." in out else out
        print("Q1." + block)
        results[path] = out

    print(f"\n{'#'*74}\n# CHECKS\n{'#'*74}")
    ok = True

    fair = results[os.path.join(TMP,"world_fair.db")]
    edge = results[os.path.join(TMP,"world_edge.db")]
    neg = results[os.path.join(TMP,"world_neg.db")]

    c1 = "EDGE FOUND" in edge
    ok &= c1
    print(f"  [{'PASS' if c1 else 'FAIL'}] planted +2pt edge was detected")

    c2 = "EDGE FOUND" not in fair
    ok &= c2
    print(f"  [{'PASS' if c2 else 'FAIL'}] fair world did NOT produce a false positive")

    c3 = "NEGATIVE EDGE" in neg or "INCONCLUSIVE" in neg
    ok &= c3
    print(f"  [{'PASS' if c3 else 'FAIL'}] negative world flagged as negative/inconclusive")

    c4 = "CONFIRMED" in edge and "strike" in edge.lower()
    ok &= c4
    print(f"  [{'PASS' if c4 else 'FAIL'}] Q3 detected the planted strike linkage")

    c5 = "0%" in edge.split("Q2.")[1].split("Q3.")[0] if "Q2." in edge else False
    ok &= c5
    print(f"  [{'PASS' if c5 else 'FAIL'}] Q2 detected the book closing early")

    spr = results[spread_case[0]]
    import re
    m = re.search(r"mean edge\s+([+-][\d.]+) pt", spr)
    val = float(m.group(1)) if m else 0.0
    c6 = 0.5 < val < 1.6
    ok &= c6
    print(f"  [{'PASS' if c6 else 'FAIL'}] half-spread capture recovered: {val:+.2f}pt "
          f"(expect ~+1.0pt from a 2c spread)")

    print("\n" + ("ALL CHECKS PASSED — the analyzer is trustworthy."
                  if ok else "SOME CHECKS FAILED — do not trust its output yet."))

    print("""
  WHAT THE 4TH CASE MEANS FOR THE STRATEGY:
    Resting at the bid mechanically captures half the spread. With maker fees at
    $0.00 on these markets, that half-spread is pure profit -- BEFORE any averaging
    edge. On a 2c spread that is ~+1pt, comparable to the entire hypothesised
    averaging effect.

    So the strategy's base edge is just market making. The averaging effect is a
    bonus on top. And the single thing that destroys it is adverse selection:
    if you only get filled when the price is about to run through you, the
    half-spread you 'captured' is an illusion.

    That is why the recorder must log fills, not just quotes.
""")

    for p, _, _ in cases + [spread_case]:
        if os.path.exists(p):
            os.remove(p)
