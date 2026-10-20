
import json

from kalshi_client import KalshiClient
from recorder import CANDIDATE_SERIES

c = KalshiClient()

ticker = None
for s in CANDIDATE_SERIES:
    ms = c.get_markets(series_ticker=s, status="open", max_pages=1)
    if ms:
        ticker = ms[0]["ticker"]
        print(f"Using live market: {ticker}")
        print(f"  yes_bid_dollars={ms[0].get('yes_bid_dollars')} "
              f"yes_ask_dollars={ms[0].get('yes_ask_dollars')}")
        print(f"  yes_bid_size_fp={ms[0].get('yes_bid_size_fp')} "
              f"yes_ask_size_fp={ms[0].get('yes_ask_size_fp')}")
        break

if not ticker:
    print("No open markets found.")
    raise SystemExit(1)

print("\n" + "=" * 70)
print("RAW RESPONSE — the full envelope, unwrapped")
print("=" * 70)
raw = c.request("GET", f"/markets/{ticker}/orderbook", params={"depth": 10})
print(json.dumps(raw, indent=2)[:2500])

print("\n" + "=" * 70)
print("STRUCTURE")
print("=" * 70)
print("top-level keys:", list(raw.keys()))
for k, v in raw.items():
    print(f"  {k!r}: {type(v).__name__}", end="")
    if isinstance(v, dict):
        print(f"  keys={list(v.keys())}")
        for k2, v2 in v.items():
            print(f"      {k2!r}: {type(v2).__name__}", end="")
            if isinstance(v2, list):
                print(f"  len={len(v2)}  first={v2[0] if v2 else 'EMPTY'}")
            else:
                print(f"  = {v2}")
    elif isinstance(v, list):
        print(f"  len={len(v)}  first={v[0] if v else 'EMPTY'}")
    else:
        print(f"  = {v}")

print("\n" + "=" * 70)
print("TRYING WITHOUT THE depth PARAMETER")
print("=" * 70)
raw2 = c.request("GET", f"/markets/{ticker}/orderbook")
print(json.dumps(raw2, indent=2)[:1200])
