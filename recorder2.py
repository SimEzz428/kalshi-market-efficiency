
from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import sys
import time

import requests

from kalshi_client import KalshiClient
from recorder import (CANDIDATE_SERIES, SCHEMA, cents, num, open_db, parse_ts,
                      pick, snapshot_series, sweep_settlements)

DB_PATH = "kalshi_15m.db"
_stop = False

SPOT_PRODUCTS = {
    "KXBTC15M": "BTC-USD", "KXETH15M": "ETH-USD", "KXSOL15M": "SOL-USD",
    "KXXRP15M": "XRP-USD", "KXDOGE15M": "DOGE-USD", "KXBNB15M": None,
    "KXHYPE15M": None,
}

WIDE_SERIES = [
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHAUS",
    "KXCPIYOY", "KXFED", "KXU3",
    "KXNBAGAME", "KXNHLGAME", "KXMLBGAME",
]

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS orderbooks (
    id        INTEGER PRIMARY KEY,
    ts        REAL NOT NULL,
    ticker    TEXT NOT NULL,
    secs_left REAL,
    -- JSON arrays of [price_cents, size] pairs, best first.
    -- Stored as JSON rather than one row per level: 20x fewer rows, and we
    -- almost always want the whole book at once anyway.
    yes_book  TEXT,
    no_book   TEXT,
    yes_depth REAL,     -- total contracts resting on the yes side
    no_depth  REAL
);
CREATE INDEX IF NOT EXISTS idx_ob_ticker ON orderbooks(ticker);
CREATE INDEX IF NOT EXISTS idx_ob_ts     ON orderbooks(ts);

CREATE TABLE IF NOT EXISTS spot (
    id      INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,
    product TEXT NOT NULL,
    price   REAL,
    bid     REAL,
    ask     REAL,
    volume  REAL
);
CREATE INDEX IF NOT EXISTS idx_spot_ts      ON spot(ts);
CREATE INDEX IF NOT EXISTS idx_spot_product ON spot(product);

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_ts REAL,
    notes      TEXT
);
"""


def _sigint(signum, frame):
    global _stop
    _stop = True
    print("\nStopping after this cycle...")


def upgrade_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_V2)
    conn.execute(
        "INSERT OR IGNORE INTO schema_version VALUES (?,?,?)",
        (2, time.time(), "added orderbooks + spot; v1 snapshots/settlements unchanged"),
    )
    conn.commit()


def record_orderbooks(client: KalshiClient, conn: sqlite3.Connection,
                      tickers: list[tuple[str, float]], depth: int = 10) -> int:
    now = time.time()
    rows = []
    for ticker, secs_left in tickers:
        try:
            ob = client.get_orderbook(ticker, depth=depth)
        except Exception:
            continue
        yes = ob.get("yes") or []
        no = ob.get("no") or []
        if not yes and not no:
            continue
        rows.append((
            now, ticker, secs_left,
            json.dumps(yes), json.dumps(no),
            sum(lvl[1] for lvl in yes if len(lvl) > 1),
            sum(lvl[1] for lvl in no if len(lvl) > 1),
        ))
    if rows:
        conn.executemany(
            """INSERT INTO orderbooks (ts,ticker,secs_left,yes_book,no_book,
                                       yes_depth,no_depth)
               VALUES (?,?,?,?,?,?,?)""", rows)
        conn.commit()
    return len(rows)


def record_spot(conn: sqlite3.Connection, products: list[str]) -> int:
    now = time.time()
    rows = []
    for p in products:
        try:
            r = requests.get(
                f"https://api.exchange.coinbase.com/products/{p}/ticker",
                timeout=8, headers={"User-Agent": "kalshi-research/2.0"})
            if r.status_code != 200:
                continue
            d = r.json()
            rows.append((now, p, num(d.get("price")), num(d.get("bid")),
                         num(d.get("ask")), num(d.get("volume"))))
        except Exception:
            continue
    if rows:
        conn.executemany(
            "INSERT INTO spot (ts,product,price,bid,ask,volume) VALUES (?,?,?,?,?,?)",
            rows)
        conn.commit()
    return len(rows)


def estimate_storage(depth: int = 10, wide: bool = False) -> None:
    n_series = len(CANDIDATE_SERIES) + (len(WIDE_SERIES) if wide else 0)
    print("STORAGE ESTIMATE (assuming ~5s average poll interval)\n")
    cycles_day = 86400 / 5
    print(f"  {'table':<14} {'rows/day':>12} {'bytes/row':>10} {'MB/day':>9} {'GB/14d':>9}")
    print("  " + "-" * 58)
    total = 0.0
    for name, per_cycle, bpr in [
        ("snapshots", n_series, 200),
        ("orderbooks", 7, 100 + depth * 2 * 14),
        ("spot", 5, 80),
    ]:
        rpd = cycles_day * per_cycle
        mbd = rpd * bpr / 1e6
        total += mbd
        print(f"  {name:<14} {rpd:>12,.0f} {bpr:>10} {mbd:>8.1f} {mbd*14/1000:>8.2f}")
    print("  " + "-" * 58)
    print(f"  {'TOTAL':<14} {'':<12} {'':<10} {total:>8.1f} {total*14/1000:>8.2f}")
    print(f"\n  Two weeks: ~{total*14/1000:.1f} GB. SQLite handles this without effort.")
    print("  Compare to the cost of NOT collecting it: another two weeks.\n")

    print("RATE LIMIT BUDGET (Basic tier: 200 tokens/s, 10 tokens/request = ~20 req/s)\n")
    fast = len(CANDIDATE_SERIES) + 7
    slow = len(WIDE_SERIES) if wide else 0
    print(f"  fast calls per cycle  {fast}  (crypto snapshots + order books)")
    print(f"  slow calls per minute {slow}  (weather/econ/sports)")
    print(f"  at 1/s near close     {fast + slow/60:.1f} req/s = "
          f"{(fast + slow/60)/20:.0%} of budget")
    print(f"  at 1/5s normal        {fast/5 + slow/60:.1f} req/s = "
          f"{(fast/5 + slow/60)/20:.0%} of budget")
    print("\n  Tiering keeps this inside budget. Without it, --wide at 1Hz would")
    print("  have exceeded the limit and started dropping requests silently.\n")


def run(client: KalshiClient, conn: sqlite3.Connection, series_list: list[str],
        depth: int, spot_every: float, duration: float | None,
        slow_series: list[str] | None = None, slow_every: float = 60.0):
    signal.signal(signal.SIGINT, _sigint)
    upgrade_db(conn)

    slow_series = slow_series or []
    fast_series = [s for s in series_list if s not in slow_series]

    products = sorted({v for k, v in SPOT_PRODUCTS.items()
                       if v and k in fast_series})
    print(f"Recording {len(fast_series)} fast series (adaptive 1-15s)")
    if slow_series:
        print(f"          {len(slow_series)} slow series (every {slow_every:.0f}s)")
    print(f"  order book depth : top {depth} levels")
    print(f"  spot products    : {', '.join(products) or 'none'}")
    print("Ctrl-C to stop.\n")

    started = time.time()
    cycles = 0
    last_spot = 0.0
    last_sweep = 0.0
    last_slow = 0.0

    while not _stop:
        if duration and time.time() - started > duration:
            break
        t0 = time.time()

        n_snap = sum(snapshot_series(client, conn, s) for s in fast_series)

        if slow_series and time.time() - last_slow > slow_every:
            n_snap += sum(snapshot_series(client, conn, s) for s in slow_series)
            last_slow = time.time()

        qs = ",".join("?" * len(fast_series))
        live = conn.execute(
            f"""SELECT ticker, secs_left FROM snapshots
                WHERE ts > ? AND secs_left > 0 AND series IN ({qs})""",
            (t0 - 1, *fast_series)).fetchall()

        n_ob = record_orderbooks(client, conn, live, depth=depth)

        n_spot = 0
        if time.time() - last_spot > spot_every and products:
            n_spot = record_spot(conn, products)
            last_spot = time.time()

        if time.time() - last_sweep > 300:
            sweep_settlements(client, conn, fast_series + slow_series)
            last_sweep = time.time()

        cycles += 1
        nearest = min((s for _, s in live), default=999)
        sleep = 1.0 if nearest < 90 else (5.0 if nearest < 420 else 15.0)

        tot_ob = conn.execute("SELECT COUNT(*) FROM orderbooks").fetchone()[0]
        tot_sp = conn.execute("SELECT COUNT(*) FROM spot").fetchone()[0]
        print(f"\r  cycle {cycles:>5} | {n_snap:>2} snaps | {n_ob:>2} books "
              f"| {n_spot} spot | totals: {tot_ob:,} books, {tot_sp:,} spot "
              f"| next {sleep:.0f}s   ", end="", flush=True)

        time.sleep(max(0.0, sleep - (time.time() - t0)))

    print("\nStopped.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--wide", action="store_true",
                    help="also record weather/econ/sports for cross-category calibration")
    ap.add_argument("--depth", type=int, default=10)
    ap.add_argument("--spot-every", type=float, default=5.0)
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--slow-every", type=float, default=60.0,
                    help="poll interval for non-crypto series")
    ap.add_argument("--db", default=DB_PATH)
    a = ap.parse_args()

    if a.estimate:
        estimate_storage(a.depth, a.wide)
        sys.exit(0)

    if not a.run:
        ap.print_help()
        sys.exit(0)

    slow = WIDE_SERIES if a.wide else []
    series = list(CANDIDATE_SERIES) + slow
    conn = open_db(a.db)
    run(KalshiClient(), conn, series, a.depth, a.spot_every, a.duration,
        slow_series=slow, slow_every=a.slow_every)
