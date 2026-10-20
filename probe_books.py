
from __future__ import annotations

import time

from kalshi_client import KalshiClient
from recorder import parse_ts

SERIES = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M",
          "KXBNB15M", "KXHYPE15M", "KXZEC15M", "KXNEAR15M"]


def main():
    c = KalshiClient()
    now = time.time()
    n_two_sided = 0
    n_markets = 0

    for s in SERIES:
        try:
            r = c.request("GET", "/markets", params={
                "series_ticker": s, "limit": 200,
                "min_close_ts": int(now), "max_close_ts": int(now + 1800)})
        except Exception as e:
            print(f"\n=== {s}: market fetch failed — {str(e)[:70]}")
            continue
        ms = r.get("markets", [])
        print(f"\n=== {s}: {len(ms)} market(s) closing within 30 min")

        for m in ms:
            n_markets += 1
            ct = parse_ts(m.get("close_time"))
            mins = (ct - now) / 60 if ct else None
            when = f"{mins:+.1f} min" if mins is not None else "unknown"
            print(f"  {m['ticker']:<30} status={str(m.get('status')):<12} "
                  f"closes in {when}")

            try:
                raw = c.request("GET", f"/markets/{m['ticker']}/orderbook",
                                params={"depth": 3})
                print(f"     RAW   : {str(raw)[:220]}")
            except Exception as e:
                print(f"     RAW   : FAILED {str(e)[:90]}")
                continue

            try:
                nb = c.get_orderbook(m["ticker"], depth=3)
                yes, no = nb.get("yes") or [], nb.get("no") or []
                print(f"     PARSED: yes={yes}  no={no}")
                if yes and no:
                    n_two_sided += 1
                    ya = 100.0 - no[0][0]
                    print(f"     -> two-sided: yes_bid {yes[0][0]:.1f}c x"
                          f"{yes[0][1]:.0f}, yes_ask {ya:.1f}c x{no[0][1]:.0f}")
            except Exception as e:
                print(f"     PARSED: FAILED {str(e)[:90]}")
            time.sleep(0.3)

    print(f"\n{'='*70}")
    print(f"  {n_markets} markets examined, {n_two_sided} genuinely two-sided")
    if n_markets and not n_two_sided:
        print("""
  If RAW was populated while PARSED was empty, get_orderbook() is the
  problem. If both were empty, these windows are not quoting -- check the
  `status` column: `initialized` means the window has not opened yet, and
  a 30-minute lookahead will always catch the NEXT window as well as the
  current one.""")


if __name__ == "__main__":
    main()
