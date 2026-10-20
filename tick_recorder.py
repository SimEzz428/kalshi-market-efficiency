
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time

DB = "kalshi_ticks.db"
WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"

SERIES = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M",
          "KXBNB15M", "KXHYPE15M", "KXZEC15M", "KXNEAR15M"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    series TEXT,
    close_ts REAL,
    secs_left REAL,
    yes_bid REAL, yes_ask REAL,
    bid_size REAL, ask_size REAL,
    spot REAL,
    strike REAL,
    dist REAL,
    volume REAL, open_interest REAL
);
CREATE INDEX IF NOT EXISTS idx_tick_ticker ON ticks(ticker);
CREATE INDEX IF NOT EXISTS idx_tick_secs   ON ticks(secs_left);

CREATE TABLE IF NOT EXISTS windows (
    ticker TEXT PRIMARY KEY,
    series TEXT, close_ts REAL, strike REAL,
    result TEXT, settlement_value REAL, recorded_ts REAL
);

CREATE TABLE IF NOT EXISTS spot_ticks (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL, index_id TEXT NOT NULL,
    value REAL, avg60 REAL, settle_avg REAL, window_size INTEGER
);
CREATE INDEX IF NOT EXISTS idx_spot ON spot_ticks(ts, index_id);
"""


def open_db(path=DB):
    conn = sqlite3.connect(path, timeout=20)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    return conn


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fair_probability(spot, strike, secs_left, sigma_per_sec):
    if spot is None or strike is None or secs_left is None or not sigma_per_sec:
        return None
    t = max(float(secs_left), 0.0)
    avg_win = min(60.0, t)
    pre = max(t - 60.0, 0.0)
    var = (sigma_per_sec ** 2) * (pre + avg_win / 3.0)
    if var <= 0:
        return 100.0 if spot > strike else 0.0
    return 100.0 * norm_cdf((spot - strike) / math.sqrt(var))


def realised_sigma(values, dt=1.0):
    if len(values) < 30:
        return None
    d = [values[i] - values[i - 1] for i in range(1, len(values))]
    m = sum(d) / len(d)
    var = sum((x - m) ** 2 for x in d) / (len(d) - 1)
    return math.sqrt(max(var, 1e-12) / dt)


def ws_headers(client):
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    ts = str(int(time.time() * 1000))
    sig = client.private_key.sign(
        f"{ts}GET{WS_PATH}".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256())
    return [f"KALSHI-ACCESS-KEY: {client.key_id}",
            f"KALSHI-ACCESS-SIGNATURE: {base64.b64encode(sig).decode()}",
            f"KALSHI-ACCESS-TIMESTAMP: {ts}"]


def live_markets(client, series_list, horizon=1800):
    from recorder import num, parse_ts, pick
    out, now = [], time.time()
    for s in series_list:
        try:
            r = client.request("GET", "/markets", params={
                "series_ticker": s, "limit": 200,
                "min_close_ts": int(now), "max_close_ts": int(now + horizon)})
        except Exception:
            continue
        for m in r.get("markets", []):
            ct = parse_ts(m.get("close_time"))
            if ct:
                out.append({"ticker": m["ticker"], "series": s,
                            "close_ts": ct, "status": m.get("status"),
                            "strike": num(pick(m, "floor_strike"))})
    return sorted(out, key=lambda x: x["close_ts"])


def probe(client, seconds=120):
    try:
        import websocket
    except ImportError:
        print("  pip install websocket-client")
        return False

    mkts = live_markets(client, SERIES)
    print(f"\n  {len(mkts)} market(s) closing within 30 min:")
    for m in mkts[:12]:
        print(f"    {m['ticker']:<30} status={str(m['status']):<12} "
              f"strike={m['strike']}  in "
              f"{(m['close_ts']-time.time())/60:+.1f}m")
    if not mkts:
        print("  nothing live — retry nearer a quarter hour")
        return False

    tickers = [m["ticker"] for m in mkts]
    ws = websocket.create_connection(WS_URL, header=ws_headers(client),
                                     timeout=20)
    print(f"\n  connected. subscribing {len(tickers)} tickers...\n")
    ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {
        "channels": ["ticker"], "market_tickers": tickers}}))
    ws.send(json.dumps({"id": 2, "cmd": "subscribe", "params": {
        "channels": ["orderbook_delta"], "market_tickers": tickers}}))
    ws.send(json.dumps({"id": 3, "cmd": "subscribe", "params": {
        "channels": ["cfbenchmarks_value"], "index_ids": ["all"]}}))

    seen: dict = {}
    end = time.time() + seconds
    while time.time() < end:
        try:
            m = json.loads(ws.recv())
        except Exception as e:
            print(f"  recv: {str(e)[:90]}")
            break
        t = m.get("type", "?")
        if t not in seen:
            seen[t] = 0
            print(f"  --- FIRST '{t}'")
            print(f"      {json.dumps(m)[:620]}\n")
        seen[t] += 1
    ws.close()

    print("\n  frame counts:")
    for t, n in sorted(seen.items(), key=lambda kv: -kv[1]):
        print(f"    {t:<30} {n:>6}")
    print("""
  CHECK BEFORE WRITING THE PARSER:
    - does `ticker` carry top-of-book SIZE or only price?
    - does `orderbook_delta` open with a full snapshot?
    - which index_ids exist (cfbenchmarks_value_indexlist)?
    - are prices dollar strings, as everywhere else?""")
    return True


def token_of(series: str) -> str:
    return series[2:-3] if series.startswith("KX") and series.endswith("15M") \
        else series


def match_index(token: str, available: set) -> str | None:
    for cand in (f"{token}USD_RTI", f"{token}_RTI", token):
        if cand in available:
            return cand
    if token == "BTC" and "BRTI" in available:
        return "BRTI"
    for a in available:
        if a.upper().startswith(token.upper()):
            return a
    return None


def run(client, conn, minutes=1440):
    try:
        import websocket
    except ImportError:
        print("  pip install websocket-client")
        return

    from recorder import num

    ws = websocket.create_connection(WS_URL, header=ws_headers(client),
                                     timeout=30)
    ws.settimeout(5.0)
    print(f"  connected to {WS_URL}")

    markets: dict = {}
    spot: dict = {}
    latest: dict = {}
    index_ids: set = set()
    sub_id = [10]

    def resubscribe():
        live = live_markets(client, SERIES, horizon=1800)
        new = [m for m in live if m["ticker"] not in markets]
        for m in live:
            markets[m["ticker"]] = m
        if new:
            sub_id[0] += 1
            ws.send(json.dumps({"id": sub_id[0], "cmd": "subscribe",
                                "params": {"channels": ["ticker"],
                                           "market_tickers":
                                               [m["ticker"] for m in new]}}))
            print(f"  +{len(new)} market(s) subscribed "
                  f"({len(markets)} tracked)")
        cutoff = time.time() - 300
        for t in [t for t, m in markets.items() if m["close_ts"] < cutoff]:
            markets.pop(t, None)
            latest.pop(t, None)

    resubscribe()
    ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {
        "channels": ["cfbenchmarks_value"], "index_ids": ["all"]}}))

    t_end = time.time() + minutes * 60
    next_write = time.time() + 1.0
    next_resub = time.time() + 60.0
    next_settle = time.time() + 120.0
    n_rows = 0

    while time.time() < t_end:
        try:
            msg = json.loads(ws.recv())
        except Exception:
            msg = None

        if msg:
            typ = msg.get("type")
            m = msg.get("msg") or {}
            if typ == "ticker":
                tk = m.get("market_ticker")
                if tk:
                    latest[tk] = m
            elif typ == "cfbenchmarks_value":
                iid = m.get("index_id")
                if iid:
                    index_ids.add(iid)
                    val = None
                    try:
                        val = num(json.loads(m.get("data") or "{}").get("value"))
                    except (ValueError, TypeError):
                        pass
                    a60 = (m.get("avg_60s_data") or {}).get("value")
                    s15 = (m.get("last_60s_windowed_average_15min") or {})
                    spot[iid] = val
                    conn.execute(
                        "INSERT INTO spot_ticks (ts,index_id,value,avg60,"
                        "settle_avg,window_size) VALUES (?,?,?,?,?,?)",
                        (time.time(), iid, val, num(a60),
                         num(s15.get("value")), s15.get("window_size")))

        now = time.time()

        if now >= next_write:
            next_write = now + 1.0
            for tk, mk in markets.items():
                tick = latest.get(tk)
                if not tick:
                    continue
                secs_left = mk["close_ts"] - now
                if secs_left < -30 or secs_left > 1000:
                    continue
                iid = match_index(token_of(mk["series"]), index_ids)
                sp = spot.get(iid) if iid else None
                strike = mk.get("strike")
                yb = num(tick.get("yes_bid_dollars"))
                ya = num(tick.get("yes_ask_dollars"))
                conn.execute(
                    """INSERT INTO ticks (ts,ticker,series,close_ts,secs_left,
                       yes_bid,yes_ask,bid_size,ask_size,spot,strike,dist,
                       volume,open_interest)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (now, tk, mk["series"], mk["close_ts"], secs_left,
                     round(yb * 100, 4) if yb is not None else None,
                     round(ya * 100, 4) if ya is not None else None,
                     num(tick.get("yes_bid_size_fp")),
                     num(tick.get("yes_ask_size_fp")),
                     sp, strike,
                     (sp - strike) if (sp is not None and strike is not None)
                     else None,
                     num(tick.get("volume_fp")),
                     num(tick.get("open_interest_fp"))))
                n_rows += 1
            conn.commit()

        if now >= next_resub:
            next_resub = now + 60.0
            try:
                resubscribe()
            except Exception as e:
                print(f"  resubscribe failed: {str(e)[:70]}")
            print(f"\r  {n_rows:,} rows | {len(markets)} markets | "
                  f"{len(index_ids)} indices   ", end="", flush=True)

        if now >= next_settle:
            next_settle = now + 120.0
            try:
                record_settlements(client, conn)
            except Exception:
                pass

    ws.close()
    conn.commit()
    print(f"\n  DONE: {n_rows:,} tick rows")


def record_settlements(client, conn):
    from recorder import num, parse_ts, pick
    rows = conn.execute("""
        SELECT DISTINCT t.ticker, t.series, t.close_ts FROM ticks t
        LEFT JOIN windows w ON w.ticker = t.ticker
        WHERE w.result IS NULL AND t.close_ts < ?
    """, (time.time() - 60,)).fetchall()
    for tk, series, close_ts in rows[:40]:
        try:
            m = client.request("GET", f"/markets/{tk}").get("market", {})
        except Exception:
            continue
        res = m.get("result")
        if res not in ("yes", "no"):
            continue
        conn.execute(
            "INSERT OR REPLACE INTO windows VALUES (?,?,?,?,?,?,?)",
            (tk, series, close_ts, num(pick(m, "floor_strike")), res,
             num(pick(m, "expiration_value")), time.time()))
    conn.commit()


def self_test():
    print("SELF-TEST — fair-value model\n")
    sig = 5.0

    for t in (600, 60, 10):
        assert abs(fair_probability(100.0, 100.0, t, sig) - 50.0) < 1e-9
    print("  [ok] spot == strike -> 50c at every horizon")

    a = fair_probability(105.0, 100.0, 300, sig)
    b = fair_probability(115.0, 100.0, 300, sig)
    assert b > a > 50.0
    print(f"  [ok] +5 -> {a:.1f}c, +15 -> {b:.1f}c  (monotone in distance)")

    c1 = fair_probability(105.0, 100.0, 600, sig)
    c2 = fair_probability(105.0, 100.0, 60, sig)
    assert c2 > c1
    print(f"  [ok] 600s -> {c1:.1f}c, 60s -> {c2:.1f}c  (time reduces doubt)")

    p_avg = fair_probability(105.0, 100.0, 30, sig)
    p_end = 100 * norm_cdf(5.0 / math.sqrt(sig ** 2 * 30.0))
    assert p_avg > p_end
    print(f"  [ok] 30s left: averaging {p_avg:.1f}c vs endpoint {p_end:.1f}c "
          f"({p_avg-p_end:+.1f}c) — sqrt(3) matters")

    up = fair_probability(105.0, 100.0, 120, sig)
    dn = fair_probability(95.0, 100.0, 120, sig)
    assert abs(up + dn - 100.0) < 1e-9
    print(f"  [ok] symmetric: {up:.2f} + {dn:.2f} = 100.00")

    assert fair_probability(None, 100.0, 60, sig) is None
    assert fair_probability(105.0, 100.0, 60, 0) is None
    print("  [ok] missing inputs -> None, never a fabricated probability")

    vals = [100 + 0.5 * math.sin(i / 3) for i in range(200)]
    s = realised_sigma(vals)
    assert s and s > 0
    print(f"  [ok] sigma from 200 ticks = {s:.4f}; <30 ticks -> "
          f"{realised_sigma([1,2,3])}")
    print("\nALL PASS\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--minutes", type=int, default=1440)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        self_test()
        sys.exit(0)

    from kalshi_client import KalshiClient
    client = KalshiClient()
    if a.probe:
        sys.exit(0 if probe(client, a.seconds) else 1)

    if a.run:
        conn = open_db(a.db)
        print(f"\n  recording to {a.db} — Ctrl+C to stop")
        try:
            run(client, conn, a.minutes)
        except KeyboardInterrupt:
            conn.commit()
            n = conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
            ns = conn.execute("SELECT COUNT(*) FROM spot_ticks").fetchone()[0]
            print(f"\n  stopped. {n:,} ticks, {ns:,} spot rows")
        sys.exit(0)

    print("Run --probe first. Four parsers here were written before probing")
    print("and all four were wrong.")
    ap.print_help()
