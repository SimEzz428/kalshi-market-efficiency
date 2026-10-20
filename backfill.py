
from __future__ import annotations

import argparse
import sqlite3
import sys
import time

from kalshi_client import KalshiClient
from recorder import CANDIDATE_SERIES, num, parse_ts, pick

DB = "kalshi_hist.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    id         INTEGER PRIMARY KEY,
    ticker     TEXT NOT NULL,
    series     TEXT,
    end_ts     REAL NOT NULL,       -- candle period end, unix
    close_ts   REAL,                -- market close, unix
    secs_left  REAL,                -- close_ts - end_ts
    bid_open   REAL, bid_high REAL, bid_low REAL, bid_close REAL,
    ask_open   REAL, ask_high REAL, ask_low REAL, ask_close REAL,
    trade_close REAL,               -- last traded price in the period
    volume     REAL,
    open_int   REAL
);
CREATE INDEX IF NOT EXISTS idx_c_ticker ON candles(ticker);
CREATE INDEX IF NOT EXISTS idx_c_secs   ON candles(secs_left);

CREATE TABLE IF NOT EXISTS hist_settlements (
    ticker   TEXT PRIMARY KEY,
    series   TEXT,
    close_ts REAL,
    result   TEXT,
    expiration_value REAL,
    floor_strike     REAL
);

CREATE TABLE IF NOT EXISTS backfill_log (
    ticker TEXT PRIMARY KEY,
    n_candles INTEGER,
    fetched_ts REAL
);
"""


def _retry(fn, *args, tries=6, **kw):
    delay = 1.0
    last = None
    for i in range(tries):
        try:
            return fn(*args, **kw)
        except Exception as e:
            last = e
            if "429" not in str(e) and "too_many_requests" not in str(e):
                raise
            if i < tries - 1:
                time.sleep(delay)
                delay *= 2
    raise RuntimeError(f"rate limited after {tries} tries: {str(last)[:100]}")


def dollars_to_cents(v):
    f = num(v)
    return None if f is None else round(f * 100.0, 4)


def open_db(path=DB):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    return conn


def probe(client, series="KXBTC15M"):
    print("Finding a recently settled market...\n")
    ms = client.get_markets(series_ticker=series, status="settled", max_pages=1)
    if not ms:
        print(f"  no settled markets in {series}")
        return
    m = ms[0]
    ticker = m["ticker"]
    close_ts = parse_ts(m.get("close_time"))
    print(f"  {ticker}  closed {m.get('close_time')}  result={m.get('result')}")

    raw = client.request(
        "GET", f"/series/{series}/markets/{ticker}/candlesticks",
        params={"start_ts": int(close_ts - 1200), "end_ts": int(close_ts + 60),
                "period_interval": 1})

    import json
    print("\n" + "=" * 70)
    print("RAW RESPONSE (first 2 candles)")
    print("=" * 70)
    cs = raw.get("candlesticks", [])
    print(json.dumps({"ticker": raw.get("ticker"), "candlesticks": cs[:2]}, indent=2)[:2200])
    print(f"\n  {len(cs)} candles returned")
    if cs:
        print(f"  keys: {list(cs[0].keys())}")
        if "yes_bid" in cs[0]:
            print(f"  yes_bid keys: {list(cs[0]['yes_bid'].keys())}")


def fetch_candles(client, series, ticker, close_ts, minutes_before=16):
    try:
        raw = _retry(
            client.request, "GET",
            f"/series/{series}/markets/{ticker}/candlesticks",
            params={"start_ts": int(close_ts - minutes_before * 60),
                    "end_ts": int(close_ts + 60), "period_interval": 1})
    except Exception:
        return []
    out = []
    for c in raw.get("candlesticks", []):
        end_ts = num(c.get("end_period_ts"))
        if end_ts is None:
            continue
        b = c.get("yes_bid") or {}
        a = c.get("yes_ask") or {}
        p = c.get("price") or {}
        out.append((
            ticker, series, end_ts, close_ts, close_ts - end_ts,
            dollars_to_cents(b.get("open_dollars")),
            dollars_to_cents(b.get("high_dollars")),
            dollars_to_cents(b.get("low_dollars")),
            dollars_to_cents(b.get("close_dollars")),
            dollars_to_cents(a.get("open_dollars")),
            dollars_to_cents(a.get("high_dollars")),
            dollars_to_cents(a.get("low_dollars")),
            dollars_to_cents(a.get("close_dollars")),
            dollars_to_cents(p.get("close_dollars")),
            num(c.get("volume_fp")), num(c.get("open_interest_fp")),
        ))
    return out


def run(client, conn, series_list, days, limit_markets):
    now = time.time()
    cutoff = now - days * 86400
    total_c = total_m = 0

    for series in series_list:
        print(f"\n{series}")
        done = {r[0] for r in conn.execute("SELECT ticker FROM backfill_log")}

        markets, cursor = [], None
        for _ in range(300):
            params = {"limit": 200, "status": "settled",
                      "series_ticker": series}
            if cursor:
                params["cursor"] = cursor
            try:
                data = _retry(client.request, "GET", "/markets", params=params)
            except Exception as e:
                print(f"\n  !! ABORTING {series}: {str(e)[:80]}")
                print("     Partial results would look like a complete census.")
                markets = None
                break
            batch = data.get("markets", [])
            markets.extend(batch)
            cursor = data.get("cursor")
            oldest = min((parse_ts(m.get("close_time")) or now) for m in batch) \
                if batch else now
            if not cursor or oldest < cutoff:
                break

        if markets is None:
            print(f"  {series}: SKIPPED — could not enumerate markets "
                  f"(this is NOT 'no markets exist')")
            continue
        markets = [m for m in markets
                   if (parse_ts(m.get("close_time")) or 0) >= cutoff]
        if limit_markets:
            markets = markets[:limit_markets]
        print(f"  {len(markets):,} settled markets in the last {days} days")

        setts = []
        for m in markets:
            if m.get("result") in ("yes", "no"):
                setts.append((m["ticker"], series, parse_ts(m.get("close_time")),
                              m.get("result"),
                              num(pick(m, "expiration_value")),
                              num(pick(m, "floor_strike"))))
        if setts:
            conn.executemany(
                """INSERT OR REPLACE INTO hist_settlements
                   (ticker,series,close_ts,result,expiration_value,floor_strike)
                   VALUES (?,?,?,?,?,?)""", setts)
            conn.commit()

        todo = [m for m in markets if m["ticker"] not in done]
        print(f"  {len(todo):,} still to fetch ({len(markets)-len(todo):,} cached)")

        t0 = time.time()
        for i, m in enumerate(todo, 1):
            ticker = m["ticker"]
            close_ts = parse_ts(m.get("close_time"))
            if not close_ts:
                continue
            rows = fetch_candles(client, series, ticker, close_ts)
            if rows:
                conn.executemany(
                    """INSERT INTO candles
                       (ticker,series,end_ts,close_ts,secs_left,
                        bid_open,bid_high,bid_low,bid_close,
                        ask_open,ask_high,ask_low,ask_close,
                        trade_close,volume,open_int)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
                total_c += len(rows)
            conn.execute("INSERT OR REPLACE INTO backfill_log VALUES (?,?,?)",
                         (ticker, len(rows), time.time()))
            total_m += 1
            if i % 50 == 0:
                conn.commit()
                rate = i / max(time.time() - t0, 1)
                eta = (len(todo) - i) / max(rate, 0.01) / 60
                print(f"\r    {i:,}/{len(todo):,}  {total_c:,} candles  "
                      f"{rate:.1f} mkt/s  ETA {eta:.0f}m   ", end="", flush=True)
        conn.commit()
        print()

    print(f"\nDONE: {total_m:,} markets, {total_c:,} candles")
    n_s = conn.execute("SELECT COUNT(*) FROM hist_settlements").fetchone()[0]
    print(f"      {n_s:,} settlements")
    print("\nNow run:  python hist_analyze.py")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true",
                    help="print one raw response before trusting any parser")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--series", nargs="*", default=None)
    ap.add_argument("--limit", type=int, default=0, help="cap markets per series")
    ap.add_argument("--db", default=DB)
    a = ap.parse_args()

    client = KalshiClient()
    if a.probe:
        probe(client, (a.series or CANDIDATE_SERIES)[0])
    elif a.run:
        run(client, open_db(a.db), a.series or CANDIDATE_SERIES,
            a.days, a.limit)
    else:
        ap.print_help()
