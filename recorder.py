
from __future__ import annotations

import argparse
import datetime as dt
import signal
import sqlite3
import sys
import time

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kalshi_client import KalshiClient


DB_PATH = "kalshi_15m.db"

CANDIDATE_SERIES = [
    "KXBTC15M",
    "KXETH15M",
    "KXSOL15M",
    "KXXRP15M",
    "KXDOGE15M",
    "KXBNB15M",
    "KXHYPE15M",
]


_stop = False


def _handle_sigint(signum, frame):
    global _stop
    _stop = True
    print("\nStopping after this cycle...")


SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY,
    ts            REAL NOT NULL,        -- unix seconds, when WE observed
    ticker        TEXT NOT NULL,
    series        TEXT,
    close_ts      REAL,                 -- market close time, unix
    secs_left     REAL,                 -- close_ts - ts
    status        TEXT,                 -- open/closed/settled - answers Q2
    yes_bid       REAL,                 -- CENTS (converted from *_dollars)
    yes_ask       REAL,
    no_bid        REAL,
    no_ask        REAL,
    yes_bid_size  REAL,                 -- depth: how many contracts at the bid
    yes_ask_size  REAL,
    liquidity     REAL,
    volume        REAL,
    open_interest REAL,
    floor_strike  REAL,
    last_price    REAL,
    can_close_early INTEGER,            -- relevant to the early-close observation
    settle_timer  REAL
);
CREATE INDEX IF NOT EXISTS idx_snap_ticker ON snapshots(ticker);
CREATE INDEX IF NOT EXISTS idx_snap_secs   ON snapshots(secs_left);

CREATE TABLE IF NOT EXISTS settlements (
    ticker           TEXT PRIMARY KEY,
    series           TEXT,
    close_ts         REAL,
    result           TEXT,              -- 'yes' or 'no'
    expiration_value REAL,              -- what it actually settled at
    floor_strike     REAL,
    recorded_ts      REAL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def open_db(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def parse_ts(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return dt.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).timestamp()
    except (ValueError, TypeError):
        return None


def num(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pick(m: dict, *names):
    for n in names:
        v = m.get(n)
        if v is not None and v != "":
            return v
    return None


def cents(m: dict, *names) -> float | None:
    raw = pick(m, *names)
    v = num(raw)
    if v is None:
        return None
    used_dollars = any(
        n.endswith("_dollars") and m.get(n) not in (None, "") for n in names
    )
    return round(v * 100.0, 4) if (used_dollars and v <= 1.0) else v


CRYPTO_TOKENS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LTC", "BNB", "HYPE")

PROBE_TICKERS = [
    f"KX{c}{suffix}"
    for c in CRYPTO_TOKENS
    for suffix in ("15M", "D", "H", "", "MIN", "HOURLY", "1H", "UPDOWN")
]


def _looks_crypto(series_ticker: str, title: str) -> bool:
    st = series_ticker.upper()
    for tok in CRYPTO_TOKENS:
        for prefix in (f"KX{tok}", tok):
            if not st.startswith(prefix):
                continue
            rest = st[len(prefix):]
            if not rest or rest[0].isdigit() or len(rest) <= 6:
                return True
    t = title.upper()
    return any(w in t for w in ("BITCOIN", "ETHEREUM", "SOLANA", "DOGECOIN", "RIPPLE"))


def _describe(client: KalshiClient, st: str) -> str | None:
    try:
        ms = client.get_markets(series_ticker=st, status="open", max_pages=1)
    except Exception:
        return None
    if not ms:
        return None
    m = ms[0]
    ct, ot = parse_ts(m.get("close_time")), parse_ts(m.get("open_time"))
    mins = (ct - ot) / 60 if ct and ot else None
    dur = f"{mins:.0f} min" if mins is not None else "?"
    flag = "  <<< 15-MIN" if mins is not None and 10 <= mins <= 20 else ""
    return (f"  {st:<22} {len(ms):>3} open  window={dur:<10} "
            f"{m.get('ticker','')[:38]}{flag}")


def discover(client: KalshiClient) -> None:
    found: dict[str, str] = {}

    print("Strategy 1 — probing likely tickers directly...\n")
    for st in PROBE_TICKERS:
        line = _describe(client, st)
        if line:
            found[st] = line
            print(line)
    if not found:
        print("  (none of the guessed tickers exist)")

    print("\nStrategy 2 — paginating ALL open events...\n")
    try:
        events = client.get_events(status="open")
        print(f"  fetched {len(events):,} events")
    except Exception as e:
        print(f"  failed: {e}")
        events = []

    cands = {
        ev.get("series_ticker", ""): ev.get("title", "")
        for ev in events
        if _looks_crypto(ev.get("series_ticker") or "", ev.get("title") or "")
    }
    cands.pop("", None)
    print(f"  {len(cands)} crypto-looking series\n")

    for st in sorted(cands):
        if st in found:
            continue
        line = _describe(client, st)
        if line:
            found[st] = line
            print(line)
        else:
            print(f"  {st:<22} (no open markets)  {cands[st][:40]}")

    if not found:
        print("\nStrategy 3 — series catalogue...\n")
        for cat in ("Crypto", "Financials", None):
            try:
                for s in client.get_series_list(category=cat):
                    st = s.get("ticker", "")
                    if _looks_crypto(st, s.get("title", "")):
                        print(f"  {st:<22} {s.get('title','')[:44]}")
            except Exception as e:
                print(f"  category={cat}: {str(e)[:70]}")

    print("\n" + "=" * 70)
    if found:
        print("Copy the ones marked <<< 15-MIN into CANDIDATE_SERIES in this file.")
    else:
        print("Nothing found. Open kalshi.com/category/crypto in a browser, click a")
        print("15-minute market, and read the ticker from the URL. Then run:")
        print("  python recorder.py --probe TICKER")


def snapshot_series(client: KalshiClient, conn: sqlite3.Connection, series: str) -> int:
    try:
        markets = client.get_markets(series_ticker=series, status="open", max_pages=2)
    except Exception as e:
        print(f"  [{series}] fetch error: {str(e)[:80]}")
        return 0

    now = time.time()
    rows = []
    for m in markets:
        close_ts = parse_ts(m.get("close_time"))
        rows.append((
            now,
            m.get("ticker"),
            series,
            close_ts,
            (close_ts - now) if close_ts else None,
            m.get("status"),
            cents(m, "yes_bid_dollars", "yes_bid"),
            cents(m, "yes_ask_dollars", "yes_ask"),
            cents(m, "no_bid_dollars", "no_bid"),
            cents(m, "no_ask_dollars", "no_ask"),
            num(pick(m, "yes_bid_size_fp", "yes_bid_size")),
            num(pick(m, "yes_ask_size_fp", "yes_ask_size")),
            num(pick(m, "liquidity_dollars", "liquidity")),
            num(pick(m, "volume_fp", "volume")),
            num(pick(m, "open_interest_fp", "open_interest")),
            num(pick(m, "floor_strike", "custom_strike")),
            cents(m, "last_price_dollars", "last_price"),
            1 if m.get("can_close_early") else 0,
            num(m.get("settlement_timer_seconds")),
        ))

    if rows:
        conn.executemany(
            """INSERT INTO snapshots
               (ts,ticker,series,close_ts,secs_left,status,yes_bid,yes_ask,
                no_bid,no_ask,yes_bid_size,yes_ask_size,liquidity,volume,
                open_interest,floor_strike,last_price,can_close_early,settle_timer)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()
    return len(rows)


def sweep_settlements(client: KalshiClient, conn: sqlite3.Connection, series_list) -> int:
    known = {r[0] for r in conn.execute("SELECT ticker FROM settlements")}
    added = 0
    for series in series_list:
        try:
            markets = client.get_markets(series_ticker=series, status="settled", max_pages=2)
        except Exception:
            continue
        for m in markets:
            t = m.get("ticker")
            if not t or t in known:
                continue
            conn.execute(
                """INSERT OR REPLACE INTO settlements
                   (ticker,series,close_ts,result,expiration_value,floor_strike,recorded_ts)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    t, series, parse_ts(m.get("close_time")), m.get("result"),
                    num(pick(m, "expiration_value", "expiration_value_dollars")),
                    num(pick(m, "floor_strike", "custom_strike")), time.time(),
                ),
            )
            added += 1
    if added:
        conn.commit()
    return added


def run(client: KalshiClient, conn: sqlite3.Connection, series_list, duration: float | None):
    signal.signal(signal.SIGINT, _handle_sigint)

    print("Preflight: verifying field mapping...")
    probe = snapshot_series(client, conn, series_list[0])
    row = conn.execute(
        "SELECT yes_bid, yes_ask, floor_strike FROM snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.execute("DELETE FROM snapshots WHERE id = (SELECT MAX(id) FROM snapshots)")
    conn.commit()

    if not probe or row is None:
        print("  ABORT: no markets returned. Check series tickers with --discover.")
        return
    yb, ya, fs = row
    print(f"  yes_bid={yb}  yes_ask={ya}  floor_strike={fs}")
    if yb is None and ya is None:
        print("\n  ABORT: prices came back NULL — the API field names have changed.")
        print("  Run  python recorder.py --probe " + series_list[0])
        print("  and update the cents(...) field lists in snapshot_series().")
        return
    if yb is not None and not (0 <= yb <= 100):
        print(f"\n  ABORT: yes_bid={yb} is outside 0-100. Unit conversion is wrong.")
        return
    print("  OK — prices parsed and in cents.\n")

    started = time.time()
    cycles = 0
    last_sweep = 0.0

    print(f"Recording {len(series_list)} series -> {DB_PATH}")
    print("Ctrl-C to stop.\n")

    while not _stop:
        if duration and time.time() - started > duration:
            print("Duration reached.")
            break

        t0 = time.time()
        total = sum(snapshot_series(client, conn, s) for s in series_list)
        cycles += 1

        row = conn.execute(
            "SELECT MIN(secs_left) FROM snapshots WHERE ts > ? AND secs_left > 0",
            (t0 - 1,),
        ).fetchone()
        nearest = row[0] if row and row[0] is not None else 999

        if nearest < 90:
            sleep = 1.0
        elif nearest < 420:
            sleep = 5.0
        else:
            sleep = 15.0

        if time.time() - last_sweep > 300:
            n = sweep_settlements(client, conn, series_list)
            last_sweep = time.time()
            if n:
                print(f"  +{n} settlements recorded")

        snaps = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        setts = conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
        print(
            f"\r  cycle {cycles:>5} | {total:>2} markets | nearest close {nearest:>6.0f}s "
            f"| {snaps:>7,} snapshots | {setts:>5,} settled | next in {sleep:.0f}s   ",
            end="", flush=True,
        )

        elapsed = time.time() - t0
        time.sleep(max(0.0, sleep - elapsed))

    print("\n\nFinal sweep for settlements...")
    sweep_settlements(client, conn, series_list)
    snaps = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    setts = conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
    print(f"Done. {snaps:,} snapshots, {setts:,} settlements in {DB_PATH}")
    print("Run:  python analyze.py")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--discover", action="store_true", help="list available crypto series")
    ap.add_argument("--probe", nargs="+", metavar="TICKER",
                    help="test specific series tickers you found in the browser")
    ap.add_argument("--run", action="store_true", help="start recording")
    ap.add_argument("--duration", type=float, default=None, help="seconds to record")
    ap.add_argument("--series", nargs="*", default=None, help="override series list")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()

    DB_PATH = args.db
    try:
        from kalshi_client import KalshiClient
        client = KalshiClient()
    except RuntimeError as e:
        print(f"{e}\n\nSet:\n  export KALSHI_KEY_ID=...\n  export KALSHI_PRIVATE_KEY_PATH=...")
        sys.exit(1)

    if args.probe:
        for st in args.probe:
            line = _describe(client, st)
            print(line if line else f"  {st:<22} no open markets / not found")
            if line:
                try:
                    m = client.get_markets(series_ticker=st, status="open", max_pages=1)[0]
                    print(f"      fields: {', '.join(sorted(m.keys()))}")
                    for k in ("yes_bid", "yes_ask", "floor_strike", "status",
                              "open_time", "close_time", "expiration_value"):
                        print(f"      {k:<18} {m.get(k)}")
                except Exception:
                    pass
    elif args.discover:
        discover(client)
    elif args.run:
        conn = open_db(DB_PATH)
        series = args.series or CANDIDATE_SERIES
        run(client, conn, series, args.duration)
    else:
        ap.print_help()
