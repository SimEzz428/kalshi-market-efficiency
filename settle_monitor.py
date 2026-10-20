
from __future__ import annotations

import argparse
import base64
import json
import math
import sqlite3
import sys
import time

DB = "kalshi_settle.db"

WS_HOSTS = [
    "wss://api.elections.kalshi.com/trade-api/ws/v2",
    "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
]
WS_PATH = "/trade-api/ws/v2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    id INTEGER PRIMARY KEY,
    ts REAL, index_id TEXT,
    value REAL,                 -- raw index value
    avg60 REAL,                 -- trailing 60s average
    settle_avg REAL,            -- last_60s_windowed_average_15min
    window_size INTEGER,        -- how many of the 60 ticks are locked
    secs_to_close REAL
);
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY,
    ts REAL, ticker TEXT, strike REAL,
    yes_bid REAL, yes_ask REAL,
    secs_left REAL,
    locked_avg REAL, window_size INTEGER,
    threshold REAL,             -- what the rest must average
    fair_yes REAL,              -- our computed fair value, cents
    gap REAL                    -- fair - mid
);
"""


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def required_future_avg(strike, locked_avg, k, total=60):
    if k >= total:
        return None
    return (total * strike - k * locked_avg) / (total - k)


def fair_yes(spot, threshold, secs_remaining, sigma_per_sec):
    if threshold is None:
        return 100.0 if spot > 0 else 0.0
    if secs_remaining <= 0:
        return 100.0 if spot > threshold else 0.0
    sd = sigma_per_sec * math.sqrt(secs_remaining / 3.0)
    if sd <= 0:
        return 100.0 if spot > threshold else 0.0
    return 100.0 * (1.0 - norm_cdf((threshold - spot) / sd))


def estimate_sigma(prices, dt=1.0):
    if len(prices) < 10:
        return None
    rets = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(max(var, 1e-12) / dt)


def open_db(path=DB):
    conn = sqlite3.connect(path, timeout=10)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    return conn


def ws_headers(client):
    ts = str(int(time.time() * 1000))
    msg = f"{ts}GET{WS_PATH}".encode()
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    sig = client.private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256())
    return [
        f"KALSHI-ACCESS-KEY: {client.key_id}",
        f"KALSHI-ACCESS-SIGNATURE: {base64.b64encode(sig).decode()}",
        f"KALSHI-ACCESS-TIMESTAMP: {ts}",
    ]


def probe(client, seconds=90):
    try:
        import websocket
    except ImportError:
        print("  pip install websocket-client")
        return False

    for host in WS_HOSTS:
        print(f"\n  trying {host} ...")
        try:
            ws = websocket.create_connection(
                host, header=ws_headers(client), timeout=15)
        except Exception as e:
            print(f"    failed: {str(e)[:140]}")
            continue

        print("    connected. asking for the index list first —")
        print("    guessing index IDs is how the last three bugs happened.\n")
        ws.send(json.dumps({
            "id": 1, "cmd": "subscribe",
            "params": {"channels": ["cfbenchmarks_value"],
                       "index_ids": ["all"]}}))

        end = time.time() + seconds
        n = 0
        while time.time() < end:
            try:
                raw = ws.recv()
            except Exception as e:
                print(f"    recv error: {str(e)[:120]}")
                break
            n += 1
            try:
                m = json.loads(raw)
            except ValueError:
                print(f"    [{n}] non-JSON: {raw[:200]}")
                continue
            t = m.get("type")
            if n <= 5 or t in ("subscribed", "error",
                               "cfbenchmarks_value_indexlist"):
                print(f"    [{n}] {json.dumps(m)[:600]}")
            if t == "cfbenchmarks_value":
                msg = m.get("msg", {})
                if "last_60s_windowed_average_15min" in msg:
                    print("\n    *** FINAL-MINUTE FIELD PRESENT ***")
                    print(f"    {json.dumps(msg, indent=2)[:900]}")
                    ws.close()
                    return True
        ws.close()
        print(f"\n    {n} frames. If you never saw "
              f"'last_60s_windowed_average_15min', run this again")
        print("    within 60s of :00, :15, :30 or :45 — the field only")
        print("    exists inside the final minute before a quarter-hour.")
        return n > 0
    return False


def self_test():
    print("SELF-TEST — settlement arithmetic\n")

    thr = required_future_avg(strike=60000.0, locked_avg=60000.0, k=30)
    assert abs(thr - 60000.0) < 1e-9, thr
    print(f"  [ok] locked at strike, k=30 -> rest must average {thr:,.0f}")

    thr = required_future_avg(strike=60000.0, locked_avg=60050.0, k=30)
    assert abs(thr - 59950.0) < 1e-9, thr
    print(f"  [ok] locked 50 above, k=30 -> rest may average {thr:,.0f} "
          f"(50 BELOW strike)")

    prev = None
    print("\n  required move as the window fills (locked 60,050, strike 60,000):")
    for k in (10, 30, 45, 55, 59):
        t = required_future_avg(60000.0, 60050.0, k)
        move = 60050.0 - t
        print(f"    k={k:>2}  rest must average {t:>10,.1f}  "
              f"({move:>7,.1f} below current)")
        if prev is not None:
            assert move > prev, "required move must grow as k grows"
        prev = move
    print("  [ok] required move grows monotonically — positions decide early")

    assert required_future_avg(60000.0, 60000.0, 60) is None
    print("  [ok] k=60 -> no threshold; the outcome is already fixed")

    sig = 10.0
    f_avg = fair_yes(60050.0, 59950.0, 30.0, sig)
    sd_avg = sig * math.sqrt(30.0 / 3.0)
    expect = 100.0 * (1 - norm_cdf((59950.0 - 60050.0) / sd_avg))
    assert abs(f_avg - expect) < 1e-6
    sd_end = sig * math.sqrt(30.0)
    assert sd_end / sd_avg > 1.7, "endpoint sd should be sqrt(3)x the average"
    print(f"\n  [ok] average sd {sd_avg:.1f} vs endpoint sd {sd_end:.1f} "
          f"({sd_end/sd_avg:.2f}x)")
    print(f"       fair value {f_avg:.2f}c — using the endpoint variance")
    print(f"       instead would give "
          f"{100*(1-norm_cdf((59950.0-60050.0)/sd_end)):.2f}c")

    a = fair_yes(60050.0, required_future_avg(60000.0, 60010.0, 30), 30.0, sig)
    b = fair_yes(60050.0, required_future_avg(60000.0, 60090.0, 30), 30.0, sig)
    assert b > a, (a, b)
    print(f"  [ok] more cushion -> higher fair value ({a:.1f}c -> {b:.1f}c)")

    s = estimate_sigma([100 + i * 0.5 for i in range(30)])
    assert s is not None and s >= 0
    print(f"  [ok] sigma estimator returns {s:.4f} on a clean ramp")
    assert estimate_sigma([1, 2, 3]) is None
    print("  [ok] refuses to estimate sigma from <10 ticks")

    print("\nALL PASS\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--seconds", type=int, default=90)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        self_test()
        sys.exit(0)

    from kalshi_client import KalshiClient
    client = KalshiClient()

    if a.probe:
        sys.exit(0 if probe(client, a.seconds) else 1)

    print("Run --probe first. The frame shape must be confirmed before any")
    print("parser is trusted — that step has caught three bugs in this project.")
    ap.print_help()
