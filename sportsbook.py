
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from collections import defaultdict

import requests

from edge import american_to_decimal, devig_power, fee_per_contract
from kalshi_client import KalshiClient
from recorder import cents, num, parse_ts, pick

ODDS_HOST = "https://api.the-odds-api.com/v4"

SPORT_HINTS = ("NBA", "NFL", "MLB", "NHL", "NCAA", "ATP", "WTA", "TENNIS",
               "SOCCER", "EPL", "UEFA", "LIGUE", "BUNDESLIGA", "SERIEA",
               "LALIGA", "UFC", "BOX", "GOLF", "PGA", "F1", "MATCH", "GAME")


def looks_sport(ticker: str, title: str = "") -> bool:
    t = (ticker or "").upper()
    return any(h in t for h in SPORT_HINTS)


def liquidity_check(size_dollars=25.0, max_hours=72.0):
    print("=" * 78)
    print(f"STEP 1 — Can Kalshi sports absorb ${size_dollars:.0f}?")
    print("=" * 78)

    client = KalshiClient()
    now = time.time()

    KNOWN = ["KXNBAGAME", "KXNFLGAME", "KXMLBGAME", "KXNHLGAME", "KXATPMATCH",
             "KXWTAMATCH", "KXEPLGAME", "KXBUNDESLIGAGAME", "KXSERIEAGAME",
             "KXLALIGAGAME", "KXLIGUE1GAME", "KXUCLGAME", "KXNCAABGAME",
             "KXNCAAFGAME", "KXUFCFIGHT", "KXPGA", "KXF1RACE", "KXT20MATCH",
             "KXNPBGAME", "KXKBOGAME", "KXVALORANTGAME", "KXCSGOGAME"]

    markets = []
    print("\n  querying known sports series directly...")
    found_series = []
    for st in KNOWN:
        try:
            ms = client.get_markets(series_ticker=st, status="open", max_pages=3)
        except Exception:
            continue
        if ms:
            found_series.append((st, len(ms)))
            markets.extend(ms)
    for st, n in found_series:
        print(f"    {st:<22} {n:>5} open markets")
    if not found_series:
        print("    none of the guessed series exist — falling back to a scan")

    if len(markets) < 50:
        cursor = None
        MAX_PAGES = 600
        for page in range(MAX_PAGES):
            params = {"limit": 200, "status": "open",
                      "min_close_ts": int(now),
                      "max_close_ts": int(now + max_hours * 3600)}
            if cursor:
                params["cursor"] = cursor
            try:
                data = client.request("GET", "/markets", params=params)
            except Exception as e:
                print(f"  fetch error: {str(e)[:60]}")
                break
            markets.extend(data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        else:
            print(f"\n  !! TRUNCATED at the {MAX_PAGES}-page cap "
                  f"({len(markets):,} markets).")
            print("     Results below are a PARTIAL view, not a full census.")

    print(f"\n  {len(markets):,} markets to screen\n")

    per = defaultdict(lambda: {"n": 0, "dep": [], "spr": [], "px": []})
    for m in markets:
        ev = m.get("event_ticker") or ""
        series = ev.split("-")[0] if ev else "?"
        if not looks_sport(series, m.get("title") or ""):
            continue
        yb = cents(m, "yes_bid_dollars", "yes_bid")
        ya = cents(m, "yes_ask_dollars", "yes_ask")
        bs = num(pick(m, "yes_bid_size_fp", "yes_bid_size")) or 0
        asz = num(pick(m, "yes_ask_size_fp", "yes_ask_size")) or 0
        if yb is None or ya is None or not (0 < yb < 100 and 0 < ya < 100):
            continue
        d = per[series]
        d["n"] += 1
        d["dep"].append(min(bs, asz) * (yb / 100.0))
        d["spr"].append(ya - yb)
        d["px"].append((yb + ya) / 2)

    if not per:
        print("  No sports markets found in that window.")
        print("  Either none are open, or the ticker hints need widening.")
        return []

    def med(v):
        v = sorted(v)
        return v[len(v) // 2] if v else 0.0

    print(f"  {'series':<24} {'markets':>8} {'med $depth':>12} {'med spread':>12} "
          f"{'fill $25?':>10}")
    print("  " + "-" * 70)

    fillable = []
    for s, d in sorted(per.items(), key=lambda kv: -med(kv[1]["dep"])):
        if d["n"] < 5:
            continue
        md, ms = med(d["dep"]), med(d["spr"])
        ok = md >= size_dollars
        if ok:
            fillable.append(s)
        print(f"  {s[:24]:<24} {d['n']:>8} {md:>11,.0f}$ {ms:>11.2f}c "
              f"{'YES' if ok else 'no':>10}")

    print(f"\n  {len(fillable)} of {len(per)} sports series can absorb "
          f"${size_dollars:.0f}.")

    if not fillable:
        print("""
  VERDICT: the idea is dead here, and it cost nothing to find out.

  If Kalshi's sports books cannot absorb a $25 order, then no amount of
  divergence from Pinnacle is capturable. Do not buy odds data.""")
    else:
        print(f"""
  VERDICT: {len(fillable)} tradeable series. Worth proceeding to step 2.

  Next: get a free key at the-odds-api.com (500 requests/month), then
    export ODDS_API_KEY=...
    python sportsbook.py --compare""")
    return fillable


def list_sports(api_key):
    try:
        r = requests.get(f"{ODDS_HOST}/sports",
                         params={"apiKey": api_key}, timeout=20)
        if r.status_code != 200:
            print(f"  API {r.status_code}: {r.text[:150]}")
            return []
        sports = r.json()
    except Exception as e:
        print(f"  failed: {e}")
        return []

    print(f"\n  {len(sports)} sports available\n")
    groups = defaultdict(list)
    for sp in sports:
        groups[sp.get("group", "?")].append(sp)

    for g in sorted(groups):
        rows = groups[g]
        print(f"  {g}")
        for sp in rows:
            active = "LIVE" if sp.get("active") else "    "
            print(f"    {active}  {sp.get('key',''):<34} {sp.get('title','')[:34]}")
        print()

    tennis = [sp for sp in sports
              if "tennis" in (sp.get("key") or "").lower() and sp.get("active")]
    if tennis:
        print("  ACTIVE TENNIS KEYS — use one of these:")
        for sp in tennis:
            print(f"    python sportsbook.py --compare --sport {sp['key']}")
    else:
        print("  No active tennis tournaments right now.")
        print("  Tennis keys only exist while a tournament is running, so try")
        print("  another sport from the list above, or check back during an event.")
    return sports


def fetch_odds(sport_key, api_key, region="eu,us", books=None):
    try:
        r = requests.get(
            f"{ODDS_HOST}/sports/{sport_key}/odds",
            params={"apiKey": api_key, "regions": region, "markets": "h2h",
                    "oddsFormat": "american",
                    **({"bookmakers": books} if books else {})},
            timeout=20)
        if r.status_code != 200:
            print(f"  odds API {r.status_code}: {r.text[:120]}")
            return []
        remaining = r.headers.get("x-requests-remaining")
        if remaining:
            print(f"  ({remaining} API requests remaining this month)")
        return r.json()
    except Exception as e:
        print(f"  odds fetch failed: {e}")
        return []


MAX_OVERROUND = 1.25


def fair_probs(outcomes):
    if len(outcomes) != 2:
        return None
    try:
        dec = [american_to_decimal(int(o["price"])) for o in outcomes]
        overround = sum(1.0 / d for d in dec)
        if not (1.0 <= overround <= MAX_OVERROUND):
            raise ValueError(
                f"implausible overround {overround:.3f} "
                f"(real books are 1.02-1.08) — bad or mismatched odds")
        p = devig_power(dec)
    except (KeyError, ValueError, TypeError) as e:
        print(f"      [skipped: {str(e)[:70]}]")
        return None
    return {o["name"]: prob for o, prob in zip(outcomes, p)}


def compare(sport_key, api_key, min_edge_pt=3.0, size_dollars=25.0):
    print("=" * 78)
    print(f"STEP 2 — de-vigged {sport_key} odds vs Kalshi")
    print("=" * 78)

    events = fetch_odds(sport_key, api_key)
    if not events:
        print("\n  No odds returned. Check the sport key and your API key.")
        return

    print(f"\n  {len(events)} events with sharp lines\n")
    for ev in events[:20]:
        books = ev.get("bookmakers") or []
        if not books:
            continue

        best_mk, best_over, best_name = None, 9.9, "?"
        for bk in books:
            m = next((mm for mm in bk.get("markets", [])
                      if mm.get("key") == "h2h"), None)
            if not m or len(m.get("outcomes") or []) != 2:
                continue
            try:
                over = sum(1.0 / american_to_decimal(int(o["price"]))
                           for o in m["outcomes"])
            except (KeyError, ValueError, TypeError, ZeroDivisionError):
                continue
            is_pin = (bk.get("key") == "pinnacle")
            if is_pin:
                best_mk, best_over, best_name = m, over, "pinnacle"
                break
            if over < best_over:
                best_mk, best_over, best_name = m, over, bk.get("key", "?")
        if not best_mk:
            continue
        mk = best_mk
        fp = fair_probs(mk.get("outcomes") or [])
        if not fp:
            continue
        total = sum(fp.values())
        flag = "" if abs(total - 1.0) < 1e-6 else f"   !! SUMS TO {total:.1%}"
        print(f"  {ev.get('home_team','?')} vs {ev.get('away_team','?')}"
              f"   [{best_name}, vig {(best_over-1)*100:.1f}%]{flag}")
        for name, prob in fp.items():
            print(f"      {name[:30]:<30} fair {prob:>6.1%}")

    print("""
  MATCHING TO KALSHI IS THE HARD PART.

  Team names differ between venues ("LA Lakers" vs "Los Angeles Lakers"), and a
  wrong match produces a fake edge that looks enormous. This prints the sharp
  fair values for manual comparison rather than auto-matching, because an
  automated mismatch here is exactly the class of error that has produced every
  false positive in this project.

  Verify a handful by hand first. If real divergences appear repeatedly, THEN
  build the matcher — with a test that plants known mismatches and confirms
  they are caught.""")


def kalshi_side(series="KXATPMATCH", size_dollars=25.0):
    print("=" * 78)
    print(f"KALSHI SIDE — {series}")
    print("=" * 78)
    client = KalshiClient()
    try:
        ms = client.get_markets(series_ticker=series, status="open", max_pages=3)
    except Exception as e:
        print(f"  fetch failed: {e}")
        return
    if not ms:
        print("  no open markets")
        return

    rows = []
    for m in ms:
        yb = cents(m, "yes_bid_dollars", "yes_bid")
        ya = cents(m, "yes_ask_dollars", "yes_ask")
        bs = num(pick(m, "yes_bid_size_fp", "yes_bid_size")) or 0
        asz = num(pick(m, "yes_ask_size_fp", "yes_ask_size")) or 0
        if yb is None or ya is None or not (0 < yb < 100 and 0 < ya < 100):
            continue
        mid = (yb + ya) / 2
        depth = min(bs, asz) * (yb / 100.0)
        rows.append((m.get("yes_sub_title") or m.get("title") or m.get("ticker"),
                     yb, ya, mid, depth, ya - yb))

    rows.sort(key=lambda r: -r[4])
    print(f"\n  {len(rows)} quoted markets, deepest first\n")
    print(f"  {'player / market':<40} {'bid':>6} {'ask':>6} {'MID':>7} "
          f"{'$depth':>8} {'spr':>5}")
    print("  " + "-" * 78)
    for title, yb, ya, mid, dep, spr in rows[:40]:
        mark = " " if dep >= size_dollars else "!"
        print(f" {mark}{str(title)[:40]:<40} {yb:>6.0f} {ya:>6.0f} {mid:>6.1f}% "
              f"{dep:>7,.0f}$ {spr:>4.1f}c")
    print(f"\n  '!' = under ${size_dollars:.0f} of resting size, not fillable.")
    print("""
  NOW COMPARE BY HAND against the sharp fair values from --compare.
  Find the same player, subtract, and write down the gap. Ten of them.
  Anything under 0.5pt is inside your costs.""")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--liquidity", action="store_true")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--sports", action="store_true",
                    help="list valid sport keys (costs 1 API request)")
    ap.add_argument("--kalshi", action="store_true",
                    help="print Kalshi prices for side-by-side comparison")
    ap.add_argument("--series", default="KXATPMATCH")
    ap.add_argument("--sport", default="basketball_nba")
    ap.add_argument("--size", type=float, default=25.0)
    ap.add_argument("--max-hours", type=float, default=72.0)
    a = ap.parse_args()

    if a.liquidity:
        liquidity_check(a.size, a.max_hours)
    elif a.kalshi:
        kalshi_side(a.series, a.size)
    elif a.sports:
        key = os.environ.get("ODDS_API_KEY")
        if not key:
            print("Set ODDS_API_KEY first.")
        else:
            list_sports(key)
    elif a.compare:
        key = os.environ.get("ODDS_API_KEY")
        if not key:
            print("Set ODDS_API_KEY first (free tier at the-odds-api.com).")
        else:
            compare(a.sport, key, size_dollars=a.size)
    else:
        ap.print_help()
