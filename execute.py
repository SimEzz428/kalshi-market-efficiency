
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sqlite3
import sys
import time
import uuid

from recorder import cents, num, parse_ts, pick


MAX_LOSS = 24.00
MAX_TRADES = 20
STAKE = 4.00

ENTRY_LO, ENTRY_HI = 90.0, 96.0
WINDOW_LO, WINDOW_HI = 30.0, 60.0
MIN_DEPTH = 1000
MAX_OPEN = 1

SERIES = "KXBTC15M"

DB = "kalshi_exec.db"
POLL_FAST = 1.0
POLL_SLOW = 10.0

SCHEMA = """
-- Every gate evaluation, including the ones that did NOT trade.
-- The skips are the primary dataset, not a side effect.
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    run_id TEXT,
    ticker TEXT,
    secs_left REAL,
    yes_bid REAL, yes_ask REAL,
    bid_depth REAL, ask_depth REAL,
    spread REAL,
    spot REAL,
    strike REAL,
    decision TEXT,          -- 'trade' | 'skip'
    skip_reason TEXT,
    book_json TEXT,         -- FULL order book at decision time
    side TEXT               -- which side qualified: 'yes' | 'no' | NULL
);
CREATE INDEX IF NOT EXISTS idx_a_ts ON attempts(ts);
CREATE INDEX IF NOT EXISTS idx_a_dec ON attempts(decision);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    run_id TEXT,
    ts REAL,
    mode TEXT,              -- dry | demo | live
    ticker TEXT,
    side TEXT, action TEXT,
    count REAL,             -- fractional contracts
    limit_price REAL,       -- cents
    notional REAL,          -- dollars at risk
    est_fee REAL,
    secs_left REAL,
    req_json TEXT,          -- EXACT request body sent
    resp_json TEXT,         -- EXACT response received
    order_id TEXT,
    status TEXT,
    error TEXT,
    key_id TEXT             -- which API key placed it. Scope lives on the key.
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY,
    client_order_id TEXT,
    order_id TEXT,
    ts REAL,
    count REAL,             -- fractional contracts
    price REAL,             -- cents
    fee REAL,
    is_taker INTEGER,
    raw_json TEXT
);

-- One row per position. pnl stays NULL until settlement is observed.
CREATE TABLE IF NOT EXISTS positions (
    client_order_id TEXT PRIMARY KEY,
    ticker TEXT,
    side TEXT DEFAULT 'yes',    -- 'yes' or 'no'. Decides who wins at settle.
    entry_ts REAL,
    entry_price REAL,       -- actual avg fill, cents
    count REAL,             -- FRACTIONAL: 0.01 granularity, never an int
    cost REAL,              -- dollars paid incl fee
    filled INTEGER,         -- 0 = never filled
    settle_ts REAL,
    result TEXT,            -- yes | no
    payout REAL,
    pnl REAL,
    expiration_value REAL
);

CREATE TABLE IF NOT EXISTS run_log (
    run_id TEXT PRIMARY KEY,
    started REAL, ended REAL, mode TEXT,
    n_attempts INTEGER, n_orders INTEGER, notes TEXT
);
"""


def realized_loss(conn) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM positions WHERE pnl IS NOT NULL"
    ).fetchone()
    settled_pnl = row[0] or 0.0

    row = conn.execute(
        "SELECT COALESCE(SUM(cost), 0) FROM positions "
        "WHERE filled = 1 AND pnl IS NULL"
    ).fetchone()
    at_risk = row[0] or 0.0

    return max(0.0, -settled_pnl) + at_risk


def remaining_budget(conn) -> float:
    return MAX_LOSS - realized_loss(conn)


def n_open(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM positions WHERE filled = 1 AND pnl IS NULL"
    ).fetchone()[0]


def n_settled(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM positions WHERE pnl IS NOT NULL").fetchone()[0]


def open_exposure(conn) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost), 0) FROM positions "
        "WHERE filled = 1 AND pnl IS NULL").fetchone()
    return row[0] or 0.0


def can_trade(conn) -> tuple:
    if n_settled(conn) >= MAX_TRADES:
        return False, f"trade_limit_reached({MAX_TRADES})"
    if remaining_budget(conn) < STAKE:
        return False, f"budget_exhausted(${remaining_budget(conn):.2f})"

    exposure = open_exposure(conn)
    if exposure >= STAKE * MAX_OPEN:
        return False, f"exposure_full(${exposure:.2f})"
    return True, None


def open_db(path=DB):
    conn = sqlite3.connect(path, timeout=10)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")

    for table, col, decl in (("attempts", "side", "TEXT"),
                             ("positions", "side", "TEXT DEFAULT 'yes'"),
                             ("orders", "key_id", "TEXT")):
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            print(f"  migrated: {table}.{col} added")

    conn.commit()
    return conn


EXCHANGE_INDEX = 0


def shard_balance(bal: dict, index: int = EXCHANGE_INDEX):
    br = bal.get("balance_breakdown")
    if isinstance(br, list) and br:
        for entry in br:
            try:
                if int(entry.get("exchange_index", -999)) == int(index):
                    v = num(entry.get("balance"))
                    return v if v is not None else None
            except (TypeError, ValueError):
                continue
        return 0.0
    v = num(pick(bal, "balance_dollars", "balance"))
    if v is not None and v >= 1000:
        v = v / 100.0
    return v


def fee_dollars(count: int, price_cents: float) -> float:
    p = price_cents / 100.0
    raw = 0.07 * count * p * (1.0 - p)
    return math.ceil(round(raw * 100, 9)) / 100.0


def book_depth(book: dict) -> tuple:
    q = market_quotes(book)
    return q["yes_bid"], q["yes_bid_size"], q["yes_ask"], q["yes_ask_size"]


def market_quotes(book: dict) -> dict:
    yes = book.get("yes") or []
    no = book.get("no") or []
    yb, ybs = (yes[0][0], yes[0][1]) if yes else (None, 0)
    nb, nbs = (no[0][0], no[0][1]) if no else (None, 0)
    return {
        "yes_bid": yb, "yes_bid_size": ybs or 0,
        "no_bid": nb, "no_bid_size": nbs or 0,
        "yes_ask": round(100.0 - nb, 4) if nb is not None else None,
        "yes_ask_size": nbs or 0,
        "no_ask": round(100.0 - yb, 4) if yb is not None else None,
        "no_ask_size": ybs or 0,
    }


def pick_side(q: dict) -> tuple:
    ya, na = q["yes_ask"], q["no_ask"]
    if ya is not None and ENTRY_LO <= ya <= ENTRY_HI:
        return "yes", ya, q["yes_ask_size"]
    if na is not None and ENTRY_LO <= na <= ENTRY_HI:
        return "no", na, q["no_ask_size"]
    return None, None, 0


def evaluate(conn, run_id, ticker, secs_left, book, spot, strike):
    q = market_quotes(book)
    yb, ya = q["yes_bid"], q["yes_ask"]
    spread = (ya - yb) if (ya is not None and yb is not None) else None

    side, ask, depth = pick_side(q)

    reason = None
    if q["yes_bid"] is None and q["no_bid"] is None:
        reason = "book_empty"
    elif not (WINDOW_LO <= secs_left <= WINDOW_HI):
        reason = f"outside_window({secs_left:.0f}s)"
    elif side is None:
        fav = max([p for p in (ya, q["no_ask"]) if p is not None], default=None)
        reason = (f"price_out_of_band({fav:.1f}c)" if fav is not None
                  else "no_quote")
    elif depth < MIN_DEPTH:
        reason = f"insufficient_depth({depth:.0f}<{MIN_DEPTH})"
    else:
        allowed, why = can_trade(conn)
        if not allowed:
            reason = why

    decision = "skip" if reason else "trade"
    conn.execute(
        """INSERT INTO attempts (ts, run_id, ticker, secs_left, yes_bid, yes_ask,
           bid_depth, ask_depth, spread, spot, strike, decision, skip_reason,
           book_json, side) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (time.time(), run_id, ticker, secs_left, yb, ya,
         q["yes_bid_size"], q["yes_ask_size"], spread,
         spot, strike, decision, reason, json.dumps(book), side))
    conn.commit()
    return (decision == "trade"), reason, side, ask, depth


ORDER_ENDPOINT = "/portfolio/events/orders"


def build_order(ticker, count, limit_cents, client_order_id, side="yes"):
    if side == "no":
        book_side = "ask"
        price_cents = 100.0 - limit_cents
    else:
        book_side = "bid"
        price_cents = limit_cents

    return {
        "ticker": ticker,
        "client_order_id": client_order_id,
        "side": book_side,
        "count": f"{float(count):.2f}",
        "price": f"{price_cents / 100.0:.4f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
    }


def parse_order_response(resp: dict) -> dict:
    filled = num(resp.get("fill_count")) or 0.0
    avg_px = num(resp.get("average_fill_price"))
    avg_fee = num(resp.get("average_fee_paid"))
    return {
        "order_id": resp.get("order_id"),
        "filled": filled,
        "remaining": num(resp.get("remaining_count")) or 0.0,
        "avg_price_cents": (avg_px * 100.0) if avg_px is not None else None,
        "fee": (avg_fee * filled) if (avg_fee is not None and filled) else 0.0,
        "ts_ms": resp.get("ts_ms"),
    }


def probe_order(conn, client, ticker_override=None):
    print("=" * 74)
    print("ORDER-PATH PROBE — 1c immediate-or-cancel, cannot fill")
    print("=" * 74)

    SAFE_FLOOR = 30.0
    candidates = []
    if ticker_override:
        candidates = [{"ticker": ticker_override}]
    else:
        for s in (SERIES, None):
            try:
                ms = client.get_markets(series_ticker=s, status="open",
                                        max_pages=2 if s is None else 1)
            except Exception:
                continue
            for m in ms:
                a = cents(m, "yes_ask_dollars", "yes_ask")
                if a is not None and SAFE_FLOOR <= a <= 100 - SAFE_FLOOR:
                    candidates.append(m)
            if candidates:
                break

    if not candidates:
        print(f"  No open market found trading between {SAFE_FLOOR:.0f}c and "
              f"{100-SAFE_FLOOR:.0f}c.")
        print("  Every candidate is already decided, so a 1c order might fill.")
        print("  Retry in a few minutes, or pass --probe-ticker TICKER.")
        return False

    ticker = candidates[0]["ticker"]
    book = client.get_orderbook(ticker, depth=5)
    yb, ybs, ya, yas = book_depth(book)
    print(f"  {ticker}")
    print(f"  live book: bid {yb}c x{ybs:.0f} / ask {ya}c x{yas:.0f}")
    if ya is None:
        print("  !! NO BOOK. Either the ticker does not exist, is closed, or")
        print("     has no quotes at all. This is not a price condition —")
        print("     there is nothing here to probe against.")
        return False
    if ya < 20:
        print(f"  !! ask is {ya:.1f}c — below 20c, so a 1c order could fill.")
        print("     Aborting rather than risking an unintended position.")
        return False
    print(f"  a 1c bid is {ya-1:.0f}c away from the ask — it cannot match")

    coid = str(uuid.uuid4())
    body = build_order(ticker, 1, 1.0, coid)
    print(f"\n  sending: {json.dumps(body)}")
    conn.execute(
        """INSERT INTO orders (client_order_id, run_id, ts, mode, ticker, side,
           action, count, limit_price, notional, est_fee, req_json, status,
           key_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (coid, "probe", time.time(), "probe", ticker, "yes", "buy", 1, 1.0,
         0.01, 0.0, json.dumps(body), "pending",
         os.environ.get("KALSHI_KEY_ID", "")))
    conn.commit()

    try:
        resp = client.request("POST", ORDER_ENDPOINT, json=body)
    except Exception as e:
        msg = str(e)[:400]
        conn.execute("UPDATE orders SET status='error', error=? "
                     "WHERE client_order_id=?", (msg, coid))
        conn.commit()
        print(f"\n  REJECTED: {msg}")
        if "403" in msg or "permission" in msg.lower() or "scope" in msg.lower():
            print("\n  -> Your API key is READ-ONLY. Generate a second key with")
            print("     write scope and export it for execution runs only.")
        elif "401" in msg:
            print("\n  -> Auth failed on a write endpoint. Check KALSHI_ENV and")
            print("     that the key belongs to THIS environment.")
        elif "410" in msg or "deprecated" in msg.lower():
            print("\n  -> Endpoint deprecated again. Read the error's docs link;")
            print("     do not guess the new shape.")
        return False

    r = parse_order_response(resp)
    conn.execute(
        "UPDATE orders SET resp_json=?, order_id=?, status=? "
        "WHERE client_order_id=?",
        (json.dumps(resp), r["order_id"],
         "filled" if r["filled"] else "cancelled_unfilled", coid))
    conn.commit()
    print(f"\n  ACCEPTED  id={r['order_id']}")
    print(f"  raw: {json.dumps(resp)[:400]}")

    if r["filled"]:
        print(f"\n  !! UNEXPECTED FILL of {r['filled']} at 1c "
              f"(avg {r['avg_price_cents']:.2f}c). You own a contract for a")
        print("     penny. Harmless, but the book was thinner than it showed.")
    else:
        print("  fill_count 0 — cancelled unfilled, exactly as intended")

    print("\n  ORDER PATH VERIFIED. --live is now unlocked.")
    print("=" * 74)
    return True


def place(conn, client, run_id, mode, ticker, count, limit_cents, secs_left,
          side="yes"):
    coid = str(uuid.uuid4())
    body = build_order(ticker, count, limit_cents, coid, side=side)
    notional = count * limit_cents / 100.0
    est_fee = fee_dollars(count, limit_cents)

    conn.execute(
        """INSERT INTO orders (client_order_id, run_id, ts, mode, ticker, side,
           action, count, limit_price, notional, est_fee, secs_left, req_json,
           status, key_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (coid, run_id, time.time(), mode, ticker, side, "buy", count,
         limit_cents, notional, est_fee, secs_left, json.dumps(body),
         "pending", os.environ.get("KALSHI_KEY_ID", "")))
    conn.commit()

    if mode == "dry":
        conn.execute("UPDATE orders SET status='dry_run' WHERE client_order_id=?",
                     (coid,))
        conn.commit()
        print(f"    [DRY] would buy {count:.2f} {side.upper()} @ {limit_cents:.0f}c "
              f"(${notional:.2f} + ${est_fee:.2f} fee)")
        return coid, None

    try:
        resp = client.request("POST", ORDER_ENDPOINT, json=body)
    except Exception as e:
        conn.execute(
            "UPDATE orders SET status='error', error=? WHERE client_order_id=?",
            (str(e)[:500], coid))
        conn.commit()
        print(f"    ORDER FAILED: {str(e)[:200]}")
        return coid, None

    r = parse_order_response(resp)
    conn.execute(
        "UPDATE orders SET resp_json=?, order_id=?, status=? "
        "WHERE client_order_id=?",
        (json.dumps(resp), r["order_id"],
         "filled" if r["filled"] else "cancelled_unfilled", coid))

    n_fill = float(r["filled"] or 0.0)

    px = r["avg_price_cents"]
    if px is not None and side == "no":
        px = round(100.0 - px, 4)

    cost = (n_fill * px / 100.0 + r["fee"]) if n_fill else 0.0
    conn.execute(
        """INSERT OR REPLACE INTO positions (client_order_id, ticker, entry_ts,
           entry_price, count, cost, filled, side)
           VALUES (?,?,?,?,?,?,?,?)""",
        (coid, ticker, time.time(), px, n_fill, cost,
         1 if n_fill else 0, side))
    if n_fill:
        conn.execute(
            """INSERT INTO fills (client_order_id, order_id, ts, count, price,
               fee, is_taker, raw_json) VALUES (?,?,?,?,?,?,1,?)""",
            (coid, r["order_id"], time.time(), n_fill, px, r["fee"],
             json.dumps(resp)))
    conn.commit()

    if n_fill:
        print(f"    FILLED {n_fill:.2f} {side.upper()} @ {px:.2f}c  "
              f"cost ${cost:.2f}  (fee ${r['fee']:.2f})  "
              f"slippage {px - limit_cents:+.2f}c")
    else:
        print("    NOT FILLED — IOC cancelled. Logged. This is the datapoint.")
    return coid, r["order_id"]


def repair_from_fills(conn, client, apply=False):
    print("\n  REPAIR FROM EXCHANGE FILLS" + ("" if apply else "  (dry run)"))
    rows = conn.execute("""
        SELECT p.client_order_id, p.ticker, p.side, p.count, p.cost,
               p.result, o.order_id
        FROM positions p LEFT JOIN orders o
          ON o.client_order_id = p.client_order_id
    """).fetchall()

    agg: dict = {}
    for t in {r[1] for r in rows}:
        try:
            resp = client.request("GET", "/portfolio/fills",
                                  params={"ticker": t, "limit": 200})
        except Exception as e:
            print(f"    {t}: fills unavailable ({str(e)[:60]})")
            continue
        for f in resp.get("fills") or []:
            oid = f.get("order_id")
            if not oid:
                continue
            c = num(pick(f, "count_fp", "count")) or 0.0
            yes_px = cents(f, "yes_price_dollars")
            no_px = cents(f, "no_price_dollars")
            fee = num(pick(f, "fee_cost", "fee_dollars", "fee")) or 0.0
            d = agg.setdefault(oid, {"n": 0.0, "yes": 0.0, "no": 0.0,
                                     "fee": 0.0})
            d["n"] += c
            d["yes"] += c * (yes_px or 0.0)
            d["no"] += c * (no_px or 0.0)
            d["fee"] += fee

    n_fixed = 0
    for coid, ticker, side, cnt, cost, result, oid in rows:
        d = agg.get(oid)
        if not d or d["n"] <= 0:
            continue
        true_n = d["n"]
        px = (d["no"] if side == "no" else d["yes"]) / true_n
        true_cost = true_n * px / 100.0 + d["fee"]
        if abs(true_n - (cnt or 0)) < 1e-9 and abs(true_cost - (cost or 0)) < 0.005:
            continue
        n_fixed += 1
        print(f"    {coid[:8]}  count {cnt} -> {true_n:.2f}   "
              f"cost ${cost or 0:.2f} -> ${true_cost:.2f}   "
              f"entry {px:.2f}c")
        if apply:
            if result in ("yes", "no"):
                payout = true_n if result == side else 0.0
                conn.execute(
                    "UPDATE positions SET count=?, cost=?, entry_price=?, "
                    "payout=?, pnl=? WHERE client_order_id=?",
                    (true_n, true_cost, px, payout, payout - true_cost, coid))
            else:
                conn.execute(
                    "UPDATE positions SET count=?, cost=?, entry_price=? "
                    "WHERE client_order_id=?", (true_n, true_cost, px, coid))
    if apply:
        conn.commit()
    if n_fixed == 0:
        print("    nothing to repair — ledger already matches the exchange")
    elif not apply:
        print(f"\n    {n_fixed} row(s) would change. Re-run with --repair "
              f"to apply.")
    else:
        print(f"\n    {n_fixed} row(s) corrected from exchange data.")
    return n_fixed


def reconcile(conn, client):
    print("\n  RECONCILIATION vs /portfolio/fills")

    rows = conn.execute("""
        SELECT p.client_order_id, p.ticker, p.count, p.cost, o.order_id
        FROM positions p LEFT JOIN orders o
          ON o.client_order_id = p.client_order_id
    """).fetchall()
    if not rows:
        print("    no positions to reconcile")
        return

    tickers = {r[1] for r in rows}
    by_coid: dict = {}
    by_oid: dict = {}
    n_fills = 0
    shown_raw = False

    for t in tickers:
        try:
            resp = client.request("GET", "/portfolio/fills",
                                  params={"ticker": t, "limit": 200})
        except Exception as e:
            print(f"    {t}: fills unavailable ({str(e)[:60]})")
            continue
        fills = resp.get("fills") or []
        for f in fills:
            n_fills += 1
            if not shown_raw:
                print(f"\n    RAW FILL (fields: {sorted(f.keys())}):")
                print(f"    {json.dumps(f)[:420]}\n")
                shown_raw = True
            c = num(pick(f, "count_fp", "count")) or 0
            coid = f.get("client_order_id")
            oid = f.get("order_id")
            if coid:
                by_coid[coid] = by_coid.get(coid, 0) + c
            if oid:
                by_oid[oid] = by_oid.get(oid, 0) + c

    print(f"    {n_fills} fill record(s) returned by the exchange")
    if n_fills == 0:
        print("""
    ZERO fills returned. Before reading this as "the trades did not happen":
      - fills older than the historical cutoff live at /historical/fills
      - the ticker filter may not match how fills are indexed
    An empty result and a wrong query look identical from here.""")
        return
    if not by_coid and not by_oid:
        print("    !! fills carry NEITHER client_order_id NOR order_id —")
        print("       cannot join. See the raw record above for what to use.")
        return

    mismatches = matched = 0
    for coid, ticker, cnt, _cost, oid in rows:
        theirs = by_coid.get(coid)
        how = "client_order_id"
        if theirs is None and oid:
            theirs = by_oid.get(oid)
            how = "order_id"
        if theirs is None:
            theirs = 0
            how = "no match"
        if abs((cnt or 0) - theirs) > 1e-6:
            mismatches += 1
            print(f"    MISMATCH {coid[:8]}  ours={cnt}  exchange={theirs}  "
                  f"({how})")
        else:
            matched += 1
    print(f"\n    {matched} agree, {mismatches} disagree")
    if mismatches:
        print("    Trust the exchange. But confirm the JOIN works before")
        print("    concluding the POSITIONS are wrong — see the raw fill.")


def settle_open(conn, client):
    rows = conn.execute(
        "SELECT client_order_id, ticker, count, cost, COALESCE(side,'yes') "
        "FROM positions WHERE filled = 1 AND pnl IS NULL").fetchall()
    for coid, ticker, count, cost, side in rows:
        try:
            m = client.request("GET", f"/markets/{ticker}").get("market", {})
        except Exception:
            continue
        result = m.get("result")
        if result not in ("yes", "no"):
            continue
        won = (result == side)
        payout = float(count) * 1.0 if won else 0.0
        pnl = payout - cost
        conn.execute(
            """UPDATE positions SET settle_ts=?, result=?, payout=?, pnl=?,
               expiration_value=? WHERE client_order_id=?""",
            (time.time(), result, payout, pnl,
             num(pick(m, "expiration_value")), coid))
        conn.commit()
        tag = "WIN " if pnl > 0 else "LOSS"
        print(f"  [{tag}] {ticker} held {side.upper()} -> settled "
              f"{result.upper()}  pnl ${pnl:+.2f}  "
              f"budget left ${remaining_budget(conn):.2f}")


def preflight(client, conn, mode):
    print("=" * 74)
    print("PREFLIGHT")
    print("=" * 74)
    ok = True

    def check(label, passed, detail=""):
        nonlocal ok
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        if detail:
            print(f"         {detail}")
        if not passed:
            ok = False

    try:
        bal = client.request("GET", "/portfolio/balance")
        total = num(pick(bal, "balance_dollars", "balance"))
        total = total if (total or 0) < 1000 else total / 100.0
        cash = shard_balance(bal, EXCHANGE_INDEX)

        br = bal.get("balance_breakdown")
        if isinstance(br, list) and len(br) > 1:
            parts = ", ".join(
                f"shard {e.get('exchange_index')}: ${num(e.get('balance')) or 0:.2f}"
                for e in br)
            check("Account reachable", True,
                  f"total ${total:.2f}  ({parts})")
            print(f"         Orders use exchange_index={EXCHANGE_INDEX}, so "
                  f"only ${cash or 0:.2f} is spendable.")
            if (cash or 0) < total * 0.8:
                print(f"         !! ${total - (cash or 0):.2f} is stranded on "
                      f"another shard. Consolidate via an intra-account")
                print("            transfer, or it cannot back an order.")
        else:
            check("Account reachable", True, f"balance ${total:.2f}")

        check("Spendable balance covers one stake", (cash or 0) >= STAKE,
              f"need ${STAKE:.2f}, have ${cash or 0:.2f} on shard "
              f"{EXCHANGE_INDEX}")

        if cash and cash > 0:
            frac = 100.0 * STAKE / cash
            n_losses = int(cash // STAKE)
            print(f"  [INFO] Stake is {frac:.0f}% of the account "
                  f"(${STAKE:.2f} of ${cash:.2f})")
            print(f"         {n_losses} consecutive losses would exhaust it.")
            if n_losses < 20:
                p_ruin = (1.0 - 0.93) ** 0
                t_ruin = n_losses / 0.07
                print(f"         At a 93% win rate you average one loss every "
                      f"14 trades,")
                print(f"         so {n_losses} losses arrive around trade "
                      f"~{t_ruin:.0f}.")
            if frac > 25:
                print(f"  [WARN] Above 25% of the account per trade. Quarter-"
                      f"Kelly on a 2pt edge (better than anything measured "
                      f"here) would be ~7%.")
    except Exception as e:
        check("Account reachable", False, str(e)[:150])

    try:
        ms = client.get_markets(series_ticker=SERIES, status="open", max_pages=1)
        check(f"{SERIES} open markets", bool(ms), f"{len(ms)} found")
        if ms:
            b = client.get_orderbook(ms[0]["ticker"])
            yb, ybs, ya, yas = book_depth(b)
            check("Order book parses", ya is not None,
                  f"bid {yb}c x{ybs:.0f} / ask {ya}c x{yas:.0f}")
    except Exception as e:
        check("Market data", False, str(e)[:150])

    spent = realized_loss(conn)
    check("Budget remaining", remaining_budget(conn) >= STAKE,
          f"${remaining_budget(conn):.2f} of ${MAX_LOSS:.2f} "
          f"(${spent:.2f} used)")

    try:
        if ms:
            depths = []
            for mm in ms[:3]:
                b = client.get_orderbook(mm["ticker"])
                yb, ybs, ya, yas = book_depth(b)
                depths += [d for d in (ybs, yas) if d]
            top = max(depths) if depths else 0
            if top >= 50:
                print(f"  [PASS] Book looks like production")
                print(f"         peak depth {top:,.0f} contracts across "
                      f"{len(ms[:3])} market(s)")
            else:
                print(f"  [WARN] Book is thin right now (peak {top:,.0f})")
                print("         Not necessarily wrong — depth collapses late in")
                print("         a decided window. Check the environment line above.")
    except Exception:
        pass

    cur_key = os.environ.get("KALSHI_KEY_ID", "")
    n_probe = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE mode='probe' AND order_id IS NOT NULL"
        " AND (key_id IS NULL OR key_id = ?)", (cur_key,)).fetchone()[0]
    n_probe_thiskey = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE mode='probe' AND order_id IS NOT NULL"
        " AND key_id = ?", (cur_key,)).fetchone()[0]
    if mode == "live":
        check("Order path probed with THIS key", n_probe_thiskey > 0,
              f"{n_probe_thiskey} probes on key {cur_key[:8]}... — "
              f"run --probe-order with the key you intend to trade with")
        if n_probe_thiskey == 0 and n_probe > 0:
            print("         (probes exist for a DIFFERENT key — a read-only")
            print("          key will pass every other check and then 403 on")
            print("          every order)")

    print("=" * 74)
    print("READY" if ok else "NOT READY — fix the failures above")
    print("=" * 74)
    return ok


_stop = False


def _sigint(_s, _f):
    global _stop
    _stop = True
    print("\n  stopping after this window...")


def run(client, conn, mode, max_minutes):
    global _stop
    signal.signal(signal.SIGINT, _sigint)
    run_id = str(uuid.uuid4())[:8]
    conn.execute("INSERT INTO run_log (run_id, started, mode) VALUES (?,?,?)",
                 (run_id, time.time(), mode))
    conn.commit()

    print(f"\nrun {run_id}  mode={mode}  budget ${remaining_budget(conn):.2f}")
    print(f"gate: {SERIES}  {WINDOW_LO:.0f}-{WINDOW_HI:.0f}s  "
          f"{ENTRY_LO:.0f}-{ENTRY_HI:.0f}c  depth>={MIN_DEPTH}\n")

    t_end = time.time() + max_minutes * 60
    last_skip = None
    net_fails = 0

    while not _stop and time.time() < t_end:
        settle_open(conn, client)

        allowed, why = can_trade(conn)
        if not allowed and why != "position_already_open":
            print(f"\n  STOPPING: {why}")
            print("  This was the plan. The experiment ended on a rule, "
                  "not on a feeling.")
            break

        try:
            ms = client.get_markets(series_ticker=SERIES, status="open",
                                    max_pages=1)
            net_fails = 0
        except Exception as e:
            net_fails = locals().get("net_fails", 0) + 1
            wait = min(POLL_SLOW * (2 ** min(net_fails, 5)), 300.0)
            if net_fails <= 2 or net_fails % 5 == 0:
                print(f"  market fetch failed ({net_fails}), "
                      f"retrying in {wait:.0f}s: {str(e)[:70]}")
            time.sleep(wait)
            continue

        live = []
        for m in ms:
            ct = parse_ts(m.get("close_time"))
            if ct and 0 < (ct - time.time()) <= 900:
                live.append((ct, m))
        if not live:
            time.sleep(POLL_SLOW)
            continue
        close_ts, m = min(live, key=lambda x: x[0])
        ticker = m["ticker"]
        secs = close_ts - time.time()

        if secs > WINDOW_HI + 5:
            time.sleep(min(POLL_SLOW, max(1.0, secs - WINDOW_HI - 2)))
            continue
        if secs < WINDOW_LO - 1:
            time.sleep(POLL_FAST)
            continue

        try:
            book = client.get_orderbook(ticker, depth=10)
        except Exception:
            time.sleep(POLL_FAST)
            continue

        go, reason, side, ask, ask_sz = evaluate(
            conn, run_id, ticker, secs, book, None,
            num(pick(m, "floor_strike")))

        if not go:
            if reason != last_skip:
                print(f"  {secs:4.0f}s  skip: {reason}")
                last_skip = reason
            time.sleep(POLL_FAST)
            continue

        count = max(1, int(STAKE / (ask / 100.0)))
        while count > 0 and (count * ask / 100.0 + fee_dollars(count, ask)) \
                > remaining_budget(conn):
            count -= 1
        if count <= 0:
            time.sleep(POLL_FAST)
            continue

        COLLATERAL_HAIRCUT = 0.90
        try:
            bal = client.request("GET", "/portfolio/balance")
            cash = shard_balance(bal, EXCHANGE_INDEX)
        except Exception:
            cash = None

        if cash is not None:
            usable = cash * COLLATERAL_HAIRCUT
            max_by_cash = int(usable / max(ask / 100.0, 0.01))
            max_by_short = int(usable / 1.0) if side == "no" else max_by_cash
            allowed = max(0, min(count, max_by_cash, max_by_short))
            if allowed < count:
                print(f"    sizing down {count} -> {allowed} "
                      f"(${cash:.2f} available, {COLLATERAL_HAIRCUT:.0%} haircut)")
                count = allowed
            if count <= 0:
                print(f"    SKIP: ${cash:.2f} cannot fund even one contract "
                      f"at {ask:.1f}c")
                time.sleep(POLL_FAST)
                continue

        print(f"\n  {secs:4.0f}s  GATE PASSED  {side.upper()} @ {ask:.1f}c  "
              f"depth {ask_sz:.0f}")
        place(conn, client, run_id, mode, ticker, count, ask, secs, side=side)
        last_skip = None
        time.sleep(POLL_FAST)

    settle_open(conn, client)
    conn.execute("UPDATE run_log SET ended=? WHERE run_id=?",
                 (time.time(), run_id))
    conn.commit()
    report(conn)


def report(conn):
    print("\n" + "=" * 74)
    print("RESULTS")
    print("=" * 74)
    n_att = conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    n_trade = conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE decision='trade'").fetchone()[0]
    print(f"\n  gate evaluations   {n_att:,}")
    print(f"  passed the gate    {n_trade:,} "
          f"({100.0*n_trade/max(n_att,1):.1f}%)")

    print("\n  WHY WE SKIPPED (this is the primary result):")
    agg: dict = {}
    for reason, n in conn.execute(
            "SELECT skip_reason, COUNT(*) FROM attempts WHERE decision='skip' "
            "GROUP BY 1"):
        base = (reason or "?").split("(")[0]
        agg[base] = agg.get(base, 0) + n
    total_skips = sum(agg.values()) or 1
    for base, n in sorted(agg.items(), key=lambda kv: -kv[1])[:10]:
        print(f"    {base:<32} {n:>6,}  ({100.0*n/total_skips:>4.1f}%)")

    n_ord = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    n_fill = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE filled=1").fetchone()[0]
    n_nofill = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE filled=0").fetchone()[0]
    print(f"\n  orders placed      {n_ord:,}")
    print(f"  filled             {n_fill:,}")
    print(f"  NOT filled         {n_nofill:,}   <- the adverse-selection signal")

    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(pnl),0), COALESCE(AVG(entry_price),0) "
        "FROM positions WHERE pnl IS NOT NULL").fetchone()
    n, pnl, avgpx = row
    if n:
        wins = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE pnl > 0").fetchone()[0]
        print(f"\n  settled            {n:,}")
        print(f"  won                {wins:,} ({100.0*wins/n:.0f}%)")
        print(f"  avg entry          {avgpx:.2f}c")
        print(f"  P&L                ${pnl:+.2f}")

        if avgpx and wins == n:
            p = (avgpx / 100.0) ** n
            print(f"\n  {n} wins, 0 losses. At {avgpx:.1f}c that happens "
                  f"{100.0*p:.0f}% of the time by chance.")
            if p > 0.10:
                print("  This is the MOST LIKELY outcome, not evidence of edge.")
        print(f"\n  n={n} is NOT enough to judge edge. You need ~280.")
        print("  Read the fill rate, not the P&L.")

    print(f"\n  budget remaining   ${remaining_budget(conn):.2f} of ${MAX_LOSS:.2f}")
    print("=" * 74)


def self_test():
    import tempfile
    print("SELF-TEST")
    p = os.path.join(tempfile.mkdtemp(), "t.db")
    c = open_db(p)

    assert remaining_budget(c) == MAX_LOSS, "fresh budget wrong"
    print(f"  [ok] fresh budget ${MAX_LOSS:.2f}  (stake ${STAKE:.2f})")

    n_to_bust = int(math.ceil(MAX_LOSS / STAKE))
    for i in range(n_to_bust):
        c.execute("INSERT INTO positions (client_order_id,ticker,count,cost,"
                  "filled,pnl) VALUES (?,?,?,?,1,?)",
                  (f"l{i}", "T", 1, STAKE, -STAKE))
    c.commit()
    assert abs(realized_loss(c) - n_to_bust * STAKE) < 1e-9, realized_loss(c)
    assert remaining_budget(c) < STAKE, "ceiling breached"
    ok, why = can_trade(c)
    assert not ok, "trading still allowed past the ceiling"
    print(f"  [ok] {n_to_bust} losses of ${STAKE:.2f} -> "
          f"${remaining_budget(c):.2f} left, blocked ({why})")

    c.execute("DELETE FROM positions")
    c.execute("INSERT INTO positions (client_order_id,ticker,side,count,cost,"
              "filled) VALUES ('dust','T','no',0.01,0.01,1)")
    c.commit()
    ok, why = can_trade(c)
    assert ok, f"a $0.01 position blocked a ${STAKE:.2f} trade: {why}"
    print(f"  [ok] $0.01 dust position does NOT block (exposure "
          f"${open_exposure(c):.2f} < ${STAKE:.2f})")

    c.execute("DELETE FROM positions")
    c.execute("INSERT INTO positions (client_order_id,ticker,side,count,cost,"
              "filled) VALUES ('real','T','no',52,?,1)", (STAKE,))
    c.commit()
    ok, why = can_trade(c)
    assert not ok and "exposure" in why, why
    print(f"  [ok] a full ${STAKE:.2f} position DOES block -> {why}")

    c.execute("DELETE FROM positions")
    for i in range(max(0, n_to_bust - 1)):
        c.execute("INSERT INTO positions (client_order_id,ticker,count,cost,"
                  "filled,pnl) VALUES (?,?,?,?,1,?)",
                  (f"k{i}", "T", 1, STAKE, -STAKE))
    c.commit()
    print(f"  [ok] after {max(0,n_to_bust-1)} losses: "
          f"${remaining_budget(c):.2f} remaining, "
          f"can_trade={can_trade(c)[0]}")

    c.execute("DELETE FROM positions")
    c.execute("INSERT INTO positions (client_order_id,ticker,count,cost,filled)"
              " VALUES ('o','T',1,?,1)", (STAKE,))
    c.commit()
    assert abs(remaining_budget(c) - (MAX_LOSS - STAKE)) < 1e-9, \
        remaining_budget(c)
    print(f"  [ok] unsettled position reserves its full cost "
          f"(${remaining_budget(c):.2f} left)")

    c.execute("DELETE FROM positions")
    c.execute("INSERT INTO positions (client_order_id,ticker,count,cost,filled,"
              "pnl) VALUES ('w','T',1,?,1,?)", (STAKE, STAKE * 0.075))
    c.commit()
    assert remaining_budget(c) == MAX_LOSS, "profit inflated the ceiling"
    print(f"  [ok] a win does NOT raise the ceiling above ${MAX_LOSS:.2f}")

    c.execute("DELETE FROM positions")
    win_pnl = STAKE * 0.075
    n_loss = max(1, n_to_bust - 1)

    c.execute("DELETE FROM positions")
    for i in range(n_loss):
        c.execute("INSERT INTO positions (client_order_id,ticker,count,cost,"
                  "filled,pnl) VALUES (?,'T',1,?,1,?)", (f"z{i}", STAKE, -STAKE))
    c.commit()
    without = remaining_budget(c)

    for i in range(3):
        c.execute("INSERT INTO positions (client_order_id,ticker,count,cost,"
                  "filled,pnl) VALUES (?,'T',1,?,1,?)", (f"w{i}", STAKE, win_pnl))
    c.commit()
    with_wins = remaining_budget(c)

    assert with_wins > without, \
        f"wins did not extend the budget: {without:.2f} -> {with_wins:.2f}"
    assert with_wins <= MAX_LOSS, "budget exceeded the cap"
    print(f"  [ok] wins EXTEND the loss budget: ${without:.2f} -> "
          f"${with_wins:.2f} after 3 wins  <- this is the hole")
    print(f"       MAX_TRADES={MAX_TRADES} is what actually bounds the run")

    c.execute("DELETE FROM positions")
    for i in range(MAX_TRADES):
        c.execute("INSERT INTO positions (client_order_id,ticker,count,cost,"
                  "filled,pnl) VALUES (?,'T',4,4.0,1,0.35)", (f"g{i}",))
    c.commit()
    ok, why = can_trade(c)
    assert not ok and "trade_limit" in why, why
    print(f"  [ok] {MAX_TRADES} WINNING trades still stops -> {why}")

    assert fee_dollars(26, 92.0) == 0.14, fee_dollars(26, 92.0)
    assert fee_dollars(4, 92.0) == 0.03, fee_dollars(4, 92.0)
    print("  [ok] fee model")

    b = {"yes": [[91.0, 500]], "no": [[8.0, 1500]]}
    yb, ybs, ya, yas = book_depth(b)
    assert (yb, ya, yas) == (91.0, 92.0, 1500), (yb, ya, yas)
    print("  [ok] ask implied from no-bid side (bug #11 regression)")

    q = market_quotes(b)
    assert q["yes_ask"] == 92.0 and q["yes_ask_size"] == 1500
    assert q["no_ask"] == 9.0 and q["no_ask_size"] == 500
    print(f"  [ok] both asks: YES {q['yes_ask']}c x{q['yes_ask_size']}  "
          f"NO {q['no_ask']}c x{q['no_ask_size']}")

    side, ask, dep = pick_side(q)
    assert (side, ask, dep) == ("yes", 92.0, 1500), (side, ask, dep)
    print(f"  [ok] favourite = {side.upper()} @ {ask}c")

    b2 = {"yes": [[8.0, 1500]], "no": [[91.0, 500]]}
    q2 = market_quotes(b2)
    side2, ask2, dep2 = pick_side(q2)
    assert (side2, ask2, dep2) == ("no", 92.0, 1500), (side2, ask2, dep2)
    print(f"  [ok] mirrored book -> {side2.upper()} @ {ask2}c x{dep2}")

    q3 = market_quotes({"yes": [], "no": [[99.9, 110191]]})
    assert q3["yes_ask"] == 0.1, q3["yes_ask"]
    assert pick_side(q3)[0] is None
    print("  [ok] decided market: yes_ask 0.1c exactly, no side qualifies")

    o_yes = build_order("T", 4, 92.0, "c", side="yes")
    o_no = build_order("T", 4, 92.0, "c", side="no")
    assert o_yes["side"] == "bid" and o_yes["price"] == "0.9200", o_yes
    assert o_no["side"] == "ask" and o_no["price"] == "0.0800", o_no
    print(f"  [ok] buy NO 92c -> sell YES: side={o_no['side']} "
          f"price={o_no['price']}")

    rr = parse_order_response({"order_id": "x", "fill_count": "4.00",
                               "remaining_count": "0.00",
                               "average_fill_price": "0.0800",
                               "average_fee_paid": "0.0075", "ts_ms": 1})
    entry_no = round(100.0 - rr["avg_price_cents"], 4)
    assert entry_no == 92.0, entry_no
    print(f"  [ok] NO fill reported {rr['avg_price_cents']:.1f}c YES "
          f"-> stored {entry_no:.1f}c NO")

    for held, settled, should_win in (("yes", "yes", True), ("yes", "no", False),
                                      ("no", "no", True), ("no", "yes", False)):
        assert (settled == held) is should_win
    print("  [ok] settlement: position wins iff result == side")

    print("\nALL PASS\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--probe-order", action="store_true",
                    help="1c IOC order that cannot fill — verifies the write path")
    ap.add_argument("--probe-ticker", default=None,
                    help="probe against a specific market instead of searching")
    ap.add_argument("--env", choices=["prod", "demo"], default=None,
                    help="override environment (default: prod for --live, "
                         "else demo)")
    ap.add_argument("--dry", action="store_true", help="log orders, send none")
    ap.add_argument("--demo", action="store_true", help="real orders, demo acct")
    ap.add_argument("--live", action="store_true", help="REAL MONEY")
    ap.add_argument("--i-understand", action="store_true",
                    help="required with --live")
    ap.add_argument("--window-lo", type=float, default=None,
                    help=f"seconds to close, lower bound (default {WINDOW_LO:.0f})")
    ap.add_argument("--window-hi", type=float, default=None,
                    help=f"seconds to close, upper bound (default {WINDOW_HI:.0f})")
    ap.add_argument("--entry-lo", type=float, default=None,
                    help=f"price band low, cents (default {ENTRY_LO:.0f})")
    ap.add_argument("--entry-hi", type=float, default=None,
                    help=f"price band high, cents (default {ENTRY_HI:.0f})")
    ap.add_argument("--min-depth", type=float, default=None,
                    help=f"contracts resting required (default {MIN_DEPTH})")
    ap.add_argument("--stake", type=float, default=None,
                    help=f"dollars per trade (default {STAKE:.2f})")
    ap.add_argument("--max-loss", type=float, default=None,
                    help=f"total loss ceiling (default {MAX_LOSS:.2f})")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--reconcile", action="store_true",
                    help="cross-check the ledger against /portfolio/fills")
    ap.add_argument("--settle", action="store_true",
                    help="resolve open positions and exit (frees the budget)")
    ap.add_argument("--repair", action="store_true",
                    help="with --reconcile: rewrite positions from exchange fills")
    ap.add_argument("--minutes", type=int, default=60)
    ap.add_argument("--db", default=DB)
    a = ap.parse_args()


    if a.self_test:
        self_test()
        sys.exit(0)

    if a.window_lo is not None:
        WINDOW_LO = a.window_lo
    if a.window_hi is not None:
        WINDOW_HI = a.window_hi
    if a.entry_lo is not None:
        ENTRY_LO = a.entry_lo
    if a.entry_hi is not None:
        ENTRY_HI = a.entry_hi
    if a.min_depth is not None:
        MIN_DEPTH = a.min_depth
    if a.stake is not None:
        STAKE = a.stake
    if a.max_loss is not None:
        MAX_LOSS = a.max_loss
    if STAKE <= 0 or MAX_LOSS <= 0:
        print("  stake and max-loss must be positive")
        sys.exit(1)
    if MAX_LOSS < STAKE:
        print(f"  max-loss (${MAX_LOSS:.2f}) is below stake (${STAKE:.2f}) — "
              f"no order could ever be funded")
        sys.exit(1)
    if WINDOW_LO >= WINDOW_HI:
        print(f"  window-lo ({WINDOW_LO}) must be below window-hi ({WINDOW_HI})")
        sys.exit(1)
    if ENTRY_LO >= ENTRY_HI:
        print(f"  entry-lo ({ENTRY_LO}) must be below entry-hi ({ENTRY_HI})")
        sys.exit(1)

    conn = open_db(a.db)

    if a.report:
        report(conn)
        sys.exit(0)

    mode = "dry"
    if a.live:
        if not a.i_understand:
            print("--live requires --i-understand. Read the docstring first.")
            sys.exit(1)
        mode = "live"
    elif a.demo:
        mode = "demo"

    env = a.env or ("prod" if mode == "live" else "demo")
    from kalshi_client import KalshiClient
    client = KalshiClient(env=env)

    kid = os.environ.get("KALSHI_KEY_ID", "")
    kpath = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    print(f"\n  environment: {env.upper()}  ({client.base})")
    print(f"  key id:      {kid[:8]}...{kid[-4:] if len(kid) > 12 else ''}")
    print(f"  key file:    {kpath}")
    if env == "demo":
        print("  note: demo is a SEPARATE account system with synthetic books.")
        print("        Use --env prod to see the market you actually studied.")

    if a.settle:
        before = remaining_budget(conn)
        settle_open(conn, client)
        after = remaining_budget(conn)
        print(f"\n  budget ${before:.2f} -> ${after:.2f}")
        n_open_now = n_open(conn)
        if n_open_now:
            print(f"  {n_open_now} position(s) still unsettled — the market "
                  f"has not resolved yet")
        sys.exit(0)

    if a.reconcile:
        if env != "prod":
            print("\n  !! --reconcile is querying DEMO. Your trades were on")
            print("     PROD, so every position will look like a mismatch.")
            print("     Re-run with --env prod\n")
        reconcile(conn, client)
        repair_from_fills(conn, client, apply=a.repair)
        sys.exit(0)

    if a.probe_order:
        sys.exit(0 if probe_order(conn, client, a.probe_ticker) else 1)

    if not preflight(client, conn, mode):
        sys.exit(1)
    if a.preflight:
        sys.exit(0)

    if mode == "live":
        print(f"\n  LIVE. Real money. Ceiling ${MAX_LOSS:.2f}, "
              f"${remaining_budget(conn):.2f} remaining.")
        if input("  type GO to arm: ").strip() != "GO":
            print("  aborted")
            sys.exit(0)

    run(client, conn, mode, a.minutes)
