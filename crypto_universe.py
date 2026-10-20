
from __future__ import annotations

import argparse
import statistics
import sys
import time

from recorder import parse_ts

DEFAULT_SIZES = [5.0, 10.0, 25.0, 50.0]

TOKENS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "ZEC", "NEAR",
          "ADA", "LTC", "AVAX", "LINK", "DOT", "TRX", "TON", "SUI", "APT",
          "ARB", "OP", "MATIC", "PEPE", "SHIB", "WIF", "BONK", "TAO", "INJ",
          "FIL", "ATOM", "ETC", "XLM", "ALGO", "ICP", "RENDER", "SEI")


def retry(fn, *args, tries=5, **kw):
    delay = 1.0
    last = None
    for i in range(tries):
        try:
            return fn(*args, **kw)
        except Exception as e:
            last = e
            msg = str(e)
            if "429" not in msg and "too_many_requests" not in msg:
                raise
            if i < tries - 1:
                time.sleep(delay)
                delay *= 2
    raise RuntimeError(f"rate limited after {tries} tries: {str(last)[:120]}")


def looks_15m(series_ticker: str) -> bool:
    st = (series_ticker or "").upper()
    if not st.startswith("KX") or not st.endswith("15M"):
        return False
    mid = st[2:-3]
    return mid in TOKENS


def discover(client):
    found = {}
    cursor = None
    for _ in range(50):
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            data = client.request("GET", "/series", params=params)
        except Exception:
            break
        batch = data.get("series") or []
        for s in batch:
            st = s.get("ticker") or ""
            if looks_15m(st):
                found[st] = s.get("title") or ""
        cursor = data.get("cursor")
        if not cursor or not batch:
            break

    if len(found) < 5:
        print("  catalogue sparse — probing constructed tickers directly")
        for tok in TOKENS:
            st = f"KX{tok}15M"
            if st in found:
                continue
            try:
                ms = retry(client.get_markets, series_ticker=st,
                           status="open", max_pages=1)
            except Exception as e:
                print(f"    !! {st}: probe failed ({str(e)[:60]}) — "
                      f"UNKNOWN, not absent")
                continue
            if ms:
                found[st] = "(probed)"
    return found


def screen(client, series_list, sizes):
    rows = []
    now0 = time.time()
    for st in sorted(series_list):
        try:
            ms = retry(client.request, "GET", "/markets",
                       params={"series_ticker": st, "limit": 200,
                               "min_close_ts": int(now0),
                               "max_close_ts": int(now0 + 1800)})
            ms = (ms or {}).get("markets", [])
        except Exception as e:
            rows.append({"series": st, "err": str(e)[:48]})
            continue
        if not ms:
            rows.append({"series": st, "n_open": 0, "confirmed_empty": True})
            continue

        now = time.time()
        live_ms = []
        for m in ms:
            ct = parse_ts(m.get("close_time"))
            if ct and 0 < (ct - now) <= 1800:
                live_ms.append((ct, m))
        live_ms.sort(key=lambda x: x[0])
        if not live_ms:
            stale = len(ms)
            rows.append({"series": st, "n_open": stale, "n_quoted": 0,
                         "all_stale": True})
            print(f"    {st}: {stale} 'open' market(s), none closing within "
                  f"30 min — stale, not live")
            continue

        spreads, fills = [], {s: 0 for s in sizes}
        n_quoted = 0
        for _ct, m in live_ms[:4]:
            try:
                book = retry(client.get_orderbook, m["ticker"], depth=5)
            except Exception as e:
                print(f"    !! {st}: orderbook failed ({str(e)[:50]})")
                continue
            yes = book.get("yes") or []
            no = book.get("no") or []
            if not yes or not no:
                continue
            yb, ybs = yes[0][0], yes[0][1]
            nb, nbs = no[0][0], no[0][1]
            ya = 100.0 - nb
            na = 100.0 - yb
            if not (0 < yb < 100 and 0 < ya < 100):
                continue
            n_quoted += 1
            spreads.append(ya - yb)
            for s in sizes:
                yes_ok = (nbs * ya / 100.0) >= s
                no_ok = (ybs * na / 100.0) >= s
                if yes_ok or no_ok:
                    fills[s] += 1

        rows.append({
            "series": st, "n_open": len(ms), "n_quoted": n_quoted,
            "spread": statistics.median(spreads) if spreads else None,
            "fills": fills,
        })
        time.sleep(0.35)
    return rows


def report(rows, sizes):
    print("\n" + "=" * 84)
    print("15-MINUTE CRYPTO UNIVERSE — fillability by ORDER SIZE")
    print("=" * 84)
    live = [r for r in rows if r.get("n_quoted")]
    print(f"\n  {len(rows)} series found, {len(live)} with live two-sided quotes\n")

    hdr = f"  {'series':<14} {'open':>5} {'spread':>8}"
    for s in sizes:
        hdr += f" {'$'+str(int(s)):>7}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for r in sorted(rows, key=lambda x: x.get("series", "")):
        if r.get("err"):
            print(f"  {r['series']:<14} error: {r['err']}")
            continue
        if r.get("all_stale"):
            print(f"  {r['series']:<14} {r.get('n_open',0):>5} "
                  f"{'STALE':>8}  (open but not closing soon — not live)")
            continue
        nq = r.get("n_quoted") or 0
        sp = r.get("spread")
        line = (f"  {r['series']:<14} {r.get('n_open',0):>5} "
                f"{(f'{sp:.1f}c' if sp is not None else '-'):>8}")
        for s in sizes:
            got = (r.get("fills") or {}).get(s, 0)
            line += f" {(f'{100*got/nq:.0f}%' if nq else '-'):>7}"
        print(line)

    print("""
  READ ACROSS A ROW, NOT DOWN A COLUMN.
    The same market is 'tradeable' at $5 and 'untradeable' at $50. Every
    earlier verdict in this project used $25 and reported it as a property
    of the market. It is a property of the ORDER.""")

    print("\n  SUGGESTED BACKFILL SET (any series with live quotes):")
    names = [r["series"] for r in live]
    if names:
        print(f"    --series {' '.join(names)}")
    else:
        print("    none — nothing is quoting right now, retry later")
    return names


def self_test():
    print("SELF-TEST\n")
    assert looks_15m("KXBTC15M") and looks_15m("KXZEC15M")
    assert looks_15m("KXNEAR15M")
    print("  [ok] accepts KXBTC15M, KXZEC15M, KXNEAR15M")

    assert not looks_15m("KXTVSEASONRELEASETHELASTOFUS")
    assert not looks_15m("KXSOLARECLIPSE")
    assert not looks_15m("KXBTCD")
    assert not looks_15m("KXBTC15MX")
    assert not looks_15m("")
    print("  [ok] rejects KXTVSEASONRELEASETHELASTOFUS (contains 'ETH')")
    print("  [ok] rejects KXSOLARECLIPSE, KXBTCD, KXBTC15MX, empty")

    ya, nbs = 93.0, 12.0
    avail = nbs * ya / 100.0
    assert abs(avail - 11.16) < 1e-9, avail
    print(f"  [ok] 12 contracts at 93c = ${avail:.2f} available")
    print(f"       -> fillable at $5 and $10, NOT at $25 or $50")
    assert avail >= 5 and avail >= 10 and avail < 25
    print("\nALL PASS\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sizes", type=float, nargs="*", default=DEFAULT_SIZES)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        self_test()
        sys.exit(0)

    from kalshi_client import KalshiClient
    client = KalshiClient()

    print("Discovering 15-minute crypto series from the catalogue...")
    found = discover(client)
    if not found:
        print("  none found — check credentials and try again")
        sys.exit(1)
    for st, title in sorted(found.items()):
        print(f"    {st:<14} {title[:56]}")

    report(screen(client, found, a.sizes), a.sizes)
