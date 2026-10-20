
from __future__ import annotations

import argparse
import signal
import sqlite3
import time
from collections import defaultdict

from kalshi_client import KalshiClient
from recorder import cents, num, parse_ts, pick


def series_of(m: dict) -> str:
    st = pick(m, "series_ticker")
    if st:
        return str(st)
    ev = m.get("event_ticker") or m.get("ticker") or ""
    return ev.split("-")[0] if ev else "?"

DB_PATH = "kalshi_wide.db"
_stop = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS wide_snapshots (
    id         INTEGER PRIMARY KEY,
    ts         REAL NOT NULL,
    ticker     TEXT NOT NULL,
    series     TEXT,
    category   TEXT,
    close_ts   REAL,
    hours_left REAL,
    yes_bid    REAL,
    yes_ask    REAL,
    volume     REAL,
    open_int   REAL,
    bid_size   REAL,      -- resting contracts at the bid
    ask_size   REAL,      -- resting contracts at the ask
    liquidity  REAL
);
CREATE INDEX IF NOT EXISTS idx_ws_ticker ON wide_snapshots(ticker);
CREATE INDEX IF NOT EXISTS idx_ws_series ON wide_snapshots(series);
CREATE INDEX IF NOT EXISTS idx_ws_hours  ON wide_snapshots(hours_left);

CREATE TABLE IF NOT EXISTS wide_settlements (
    ticker   TEXT PRIMARY KEY,
    series   TEXT,
    category TEXT,
    close_ts REAL,
    result   TEXT,
    recorded REAL
);
CREATE INDEX IF NOT EXISTS idx_wt_series ON wide_settlements(series);
"""


def _sigint(s, f):
    global _stop
    if _stop:
        print("\nForce quit.")
        raise SystemExit(1)
    _stop = True
    print("\nStopping after this sweep (Ctrl-C again to force quit)...")


def interruptible_sleep(seconds: float, step: float = 0.5) -> None:
    end = time.time() + seconds
    while time.time() < end and not _stop:
        time.sleep(min(step, max(0.0, end - time.time())))


def open_db(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for col, typ in (("bid_size", "REAL"), ("ask_size", "REAL"),
                     ("liquidity", "REAL")):
        try:
            conn.execute(f"ALTER TABLE wide_snapshots ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    return conn


def survey(client: KalshiClient) -> None:
    print("Paginating all open events...\n")
    events = client.get_events(status="open")
    print(f"  {len(events):,} open events\n")

    by_series = defaultdict(lambda: {"n": 0, "title": "", "cat": ""})
    for ev in events:
        st = ev.get("series_ticker") or "?"
        d = by_series[st]
        d["n"] += 1
        d["title"] = d["title"] or (ev.get("title") or "")[:44]
        d["cat"] = d["cat"] or (ev.get("category") or "")

    print(f"  {len(by_series):,} distinct series\n")
    print("Sampling close times to find fast-settling series")
    print("(this is the number that decides what's usable)\n")

    print(f"  {'series':<24} {'events':>7} {'median close':>14} {'usable?':>9}")
    print("  " + "-" * 60)

    now = time.time()
    fast = []
    for st in sorted(by_series, key=lambda s: -by_series[s]["n"])[:45]:
        try:
            ms = client.get_markets(series_ticker=st, status="open", max_pages=1)
        except Exception:
            continue
        if not ms:
            continue
        hrs = sorted(
            (parse_ts(m.get("close_time")) - now) / 3600
            for m in ms if parse_ts(m.get("close_time"))
        )
        if not hrs:
            continue
        med = hrs[len(hrs) // 2]
        label = (f"{med:.1f}h" if med < 48 else f"{med/24:.0f}d")
        usable = "YES" if med < 24 * 7 else "too slow"
        if med < 24 * 7:
            fast.append((st, med, by_series[st]["n"]))
        print(f"  {st:<24} {by_series[st]['n']:>7} {label:>14} {usable:>9}")

    print(f"\n  {len(fast)} series settle within 7 days -> these carry the study.")
    print("\n  Add them to FAST_SERIES, or just run --run which filters by")
    print("  close time automatically.")


def sweep(client: KalshiClient, conn: sqlite3.Connection, max_hours: float,
          min_activity: float) -> tuple[int, int]:
    now = time.time()
    kept = seen = 0
    rows = []
    cursor = None

    close_window = {"min_close_ts": int(now),
                    "max_close_ts": int(now + max_hours * 3600)}

    for page in range(400):
        if _stop:
            break
        params = {"limit": 200, "status": "open", **close_window}
        if cursor:
            params["cursor"] = cursor
        try:
            data = client.request("GET", "/markets", params=params)
        except Exception as e:
            if close_window and page == 0:
                print("  (close-time filter unsupported, falling back)")
                close_window = {}
                continue
            print(f"\n  sweep error: {str(e)[:70]}")
            break

        markets = data.get("markets", [])
        seen += len(markets)
        for m in markets:
            close_ts = parse_ts(m.get("close_time"))
            if not close_ts:
                continue
            hours = (close_ts - now) / 3600
            if hours < 0 or hours > max_hours:
                continue
            vol = num(pick(m, "volume_fp", "volume")) or 0
            oi = num(pick(m, "open_interest_fp", "open_interest")) or 0
            if vol + oi < min_activity:
                continue
            yb = cents(m, "yes_bid_dollars", "yes_bid")
            ya = cents(m, "yes_ask_dollars", "yes_ask")

            if yb is None or ya is None:
                continue
            if not (0 < yb < 100 and 0 < ya < 100):
                continue
            rows.append((now, m.get("ticker"), series_of(m),
                         m.get("category"), close_ts, hours, yb, ya, vol, oi,
                         num(pick(m, "yes_bid_size_fp", "yes_bid_size")) or 0,
                         num(pick(m, "yes_ask_size_fp", "yes_ask_size")) or 0,
                         num(pick(m, "liquidity_dollars", "liquidity"))))
            kept += 1

        cursor = data.get("cursor")
        if not cursor:
            break
    else:
        print(f"\n  !! hit the {400}-page cap at {seen:,} markets — results are")
        print("     TRUNCATED, not complete. Narrow the filter.")

    if rows:
        conn.executemany(
            """INSERT INTO wide_snapshots
               (ts,ticker,series,category,close_ts,hours_left,yes_bid,yes_ask,
                volume,open_int,bid_size,ask_size,liquidity)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        conn.commit()
    return seen, kept


def sweep_settled(client: KalshiClient, conn: sqlite3.Connection) -> int:
    known = {r[0] for r in conn.execute("SELECT ticker FROM wide_settlements")}
    added = 0
    cursor = None
    for _ in range(200):
        params = {"limit": 200, "status": "settled"}
        if cursor:
            params["cursor"] = cursor
        try:
            data = client.request("GET", "/markets", params=params)
        except Exception:
            break
        for m in data.get("markets", []):
            t = m.get("ticker")
            if not t or t in known or m.get("result") not in ("yes", "no"):
                continue
            conn.execute(
                """INSERT OR REPLACE INTO wide_settlements
                   (ticker,series,category,close_ts,result,recorded)
                   VALUES (?,?,?,?,?,?)""",
                (t, series_of(m), m.get("category"),
                 parse_ts(m.get("close_time")), m.get("result"), time.time()))
            added += 1
        cursor = data.get("cursor")
        if not cursor:
            break
    if added:
        conn.commit()
    return added


def run(client, conn, interval, max_hours, min_activity, duration):
    signal.signal(signal.SIGINT, _sigint)
    print(f"Wide sweep every {interval/60:.0f} min")
    print(f"  keeping markets closing within {max_hours:.0f}h")
    print(f"  and with volume+OI >= {min_activity:.0f}")
    print(f"  -> {DB_PATH}\n")

    started = time.time()
    n = 0
    while not _stop:
        if duration and time.time() - started > duration:
            break
        t0 = time.time()
        seen, kept = sweep(client, conn, max_hours, min_activity)
        settled = sweep_settled(client, conn)
        n += 1
        tot = conn.execute("SELECT COUNT(*) FROM wide_snapshots").fetchone()[0]
        tots = conn.execute("SELECT COUNT(*) FROM wide_settlements").fetchone()[0]
        print(f"  sweep {n:>4} | scanned {seen:>6,} | kept {kept:>5,} | "
              f"+{settled:>4} settled | totals {tot:,} / {tots:,} | "
              f"took {time.time()-t0:.0f}s")
        interruptible_sleep(max(0.0, interval - (time.time() - t0)))
    print("Stopped.")


def analyze(conn, min_n=100):
    import math

    print("=" * 74)
    print("CALIBRATION BY CATEGORY")
    print("=" * 74)

    rows = conn.execute(
        """SELECT s.category, s.series, s.ticker, s.yes_bid, s.yes_ask,
                  s.hours_left, t.result
           FROM wide_snapshots s
           JOIN wide_settlements t ON t.ticker = s.ticker
           WHERE t.result IN ('yes','no')
             AND s.yes_bid > 0 AND s.yes_ask < 100"""
    ).fetchall()

    if not rows:
        print("\n  No settled markets with snapshots yet.")
        print("  Categories need time to resolve — check back in a few days.")
        return

    best = {}
    for cat, series, ticker, yb, ya, hrs, result in rows:
        price = (yb + ya) / 2
        won = result == "yes"
        if ticker not in best or hrs > best[ticker][0]:
            best[ticker] = (hrs, cat or "?", price, won)

    by_cat = defaultdict(list)
    for hrs, cat, price, won in best.values():
        by_cat[cat].append((price, won))

    print(f"\n  {len(best):,} settled observations across {len(by_cat)} categories\n")
    print(f"  {'category':<22} {'n':>7} {'avg price':>10} {'settled YES':>12} "
          f"{'deviation':>11} {'z':>7}")
    print("  " + "-" * 72)

    results = []
    for cat in sorted(by_cat, key=lambda c: -len(by_cat[c])):
        sel = by_cat[cat]
        n = len(sel)
        if n < min_n:
            continue
        avg_p = sum(p for p, _ in sel) / n / 100
        rate = sum(1 for _, w in sel if w) / n
        dev = rate - avg_p
        se = math.sqrt(max(avg_p * (1 - avg_p), 1e-6) / n)
        z = dev / se if se else 0
        print(f"  {cat[:22]:<22} {n:>7,} {avg_p:>9.1%} {rate:>11.1%} "
              f"{dev*100:>+10.2f}pt {z:>7.2f}")
        results.append((cat, n, dev, z))

    if not results:
        print(f"  (no category has {min_n}+ settled observations yet)")
        return

    k = len(results)
    z_corr = 1.96 + 0.45 * math.log(max(k, 1))
    sig = [r for r in results if abs(r[3]) > z_corr]
    print(f"\n  Threshold for {k} categories: |z| > {z_corr:.2f}")
    if sig:
        print(f"\n  {len(sig)} CATEGORY(IES) MISPRICED:")
        for cat, n, dev, z in sorted(sig, key=lambda r: -abs(r[3])):
            d = "UNDERpriced (buy YES)" if dev > 0 else "OVERpriced (buy NO)"
            print(f"    {cat}: {dev*100:+.2f}pt, z={z:.2f}, n={n:,} -> {d}")
        print("""
  BEFORE BELIEVING ANY OF THIS:
    1. Is it tradeable? Check spread and depth — an illiquid market can look
       mispriced and be impossible to trade.
    2. Does it hold out-of-sample? Split your data in half by date and confirm
       the effect appears in BOTH halves. This is the single best defence
       against having data-mined a coincidence.
    3. Is there a mechanism? "Category X is mispriced" with no explanation is
       usually a story about your sample, not about the market.""")
    else:
        print("  No category significantly mispriced. Consistent with efficiency.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--survey", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--analyze", action="store_true")
    ap.add_argument("--interval", type=float, default=1800, help="seconds between sweeps")
    ap.add_argument("--max-hours", type=float, default=168, help="skip slower-settling markets")
    ap.add_argument("--min-activity", type=float, default=100, help="skip untraded markets")
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--min-n", type=int, default=100)
    ap.add_argument("--db", default=DB_PATH)
    a = ap.parse_args()

    DB_PATH = a.db
    if a.survey:
        survey(KalshiClient())
    elif a.run:
        run(KalshiClient(), open_db(a.db), a.interval, a.max_hours,
            a.min_activity, a.duration)
    elif a.analyze:
        analyze(open_db(a.db), a.min_n)
    else:
        ap.print_help()
