
from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from kalshi_client import KalshiClient

CENTI_CENTS_PER_DOLLAR = 10000.0


def dollars(centi_cents) -> float:
    try:
        return float(centi_cents) / CENTI_CENTS_PER_DOLLAR
    except (TypeError, ValueError):
        return 0.0


def fetch(client, status="active", itype="all", max_pages=20):
    out, cursor = [], None
    for _ in range(max_pages):
        params = {"status": status, "type": itype, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        raw = client.request("GET", "/incentive_programs", params=params)
        batch = raw.get("incentive_programs", []) or []
        out.extend(batch)
        cursor = raw.get("next_cursor")
        if not cursor or not batch:
            break
    return out


def series_of(ticker: str) -> str:
    return (ticker or "").split("-")[0]


def report(progs, status):
    print("=" * 78)
    print(f"KALSHI INCENTIVE PROGRAMS — status={status}")
    print("=" * 78)
    if not progs:
        print(f"""
  No {status} programs returned.

  That is a real answer, not an error. If Kalshi is not currently paying for
  liquidity on any market, then the one remaining strategy category that does
  not require predicting anything is simply not available right now.

  Try --all to see whether programs have existed historically; if they run
  periodically, the question becomes "when do they return" rather than "do
  they exist".""")
        return

    total = sum(dollars(p.get("period_reward")) for p in progs)
    print(f"\n  {len(progs):,} programs   total period reward "
          f"${total:,.2f}\n")

    by_type: dict = defaultdict(list)
    for p in progs:
        by_type[p.get("incentive_type", "?")].append(p)
    for t, ps in sorted(by_type.items()):
        print(f"  {t:<12} {len(ps):>5} programs   "
              f"${sum(dollars(x.get('period_reward')) for x in ps):>12,.2f}")

    by_series: dict = defaultdict(lambda: {"n": 0, "reward": 0.0,
                                           "type": set(), "sizes": []})
    for p in progs:
        s = series_of(p.get("market_ticker"))
        d = by_series[s]
        d["n"] += 1
        d["reward"] += dollars(p.get("period_reward"))
        d["type"].add(p.get("incentive_type", "?"))
        if p.get("target_size_fp"):
            try:
                d["sizes"].append(float(p["target_size_fp"]))
            except ValueError:
                pass

    print(f"\n  {'series':<24} {'mkts':>6} {'total $':>12} {'$/market':>10} "
          f"{'target size':>12} {'type':>10}")
    print("  " + "-" * 78)
    for s, d in sorted(by_series.items(), key=lambda kv: -kv[1]["reward"])[:25]:
        med = (sorted(d["sizes"])[len(d["sizes"]) // 2] if d["sizes"] else 0)
        print(f"  {s[:24]:<24} {d['n']:>6,} {d['reward']:>12,.2f} "
              f"{d['reward']/max(d['n'],1):>10,.2f} {med:>12,.0f} "
              f"{','.join(sorted(d['type'])):>10}")

    OURS = {"KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M",
            "KXDOGE15M", "KXBNB15M", "KXHYPE15M"}
    hit = {s: d for s, d in by_series.items() if s in OURS}
    print("\n" + "=" * 78)
    print("DOES IT COVER THE MARKETS WE STUDIED?")
    print("=" * 78)
    if hit:
        for s, d in sorted(hit.items(), key=lambda kv: -kv[1]["reward"]):
            print(f"\n  {s}: {d['n']:,} markets, ${d['reward']:,.2f} total, "
                  f"${d['reward']/max(d['n'],1):,.2f} per market")
        print("""
  Worth pursuing. Next questions, in order:
    1. What does the description say you must do to qualify?
    2. Is target_size affordable at your account size?
    3. Does the reward per market exceed what adverse selection costs?
       Only resting live orders answers that one.""")
    else:
        print("""
  No. None of the seven 15-minute crypto series carry an incentive program.

  So the zero-maker-fee advantage on those markets is not amplified by any
  rebate -- making there earns the spread and nothing else, against the
  adverse selection your live orders already demonstrated.

  Look at the series list above instead: if the programs sit on markets you
  have not screened, the liquidity question has to be asked again from
  scratch for those. Do not assume depth carries across market types --
  7 of 245 series could absorb $25, and that was measured, not guessed.""")

    print("\n  SAMPLE DESCRIPTIONS (what you must actually do):")
    seen = set()
    for p in progs:
        d = (p.get("incentive_description") or "").strip()
        if d and d not in seen:
            seen.add(d)
            print(f"    - {d[:150]}")
        if len(seen) >= 6:
            break


def self_test():
    print("SELF-TEST\n")
    assert dollars(10000) == 1.0, dollars(10000)
    assert dollars("25000") == 2.5, dollars("25000")
    assert dollars(None) == 0.0
    assert dollars("junk") == 0.0
    print("  [ok] centi-cents: 10,000 -> $1.00, 25,000 -> $2.50")
    print("       (a 100x unit error here would make a $50 program look")
    print("        like $5,000 — worth one assertion)")
    assert series_of("KXBTC15M-26AUG061030-30") == "KXBTC15M"
    assert series_of("") == ""
    print("  [ok] series extraction")
    print("\nALL PASS\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", default="active",
                    choices=["all", "active", "upcoming", "closed", "paid_out"])
    ap.add_argument("--all", action="store_true", help="shorthand for --status all")
    ap.add_argument("--type", default="all",
                    choices=["all", "liquidity", "volume"])
    ap.add_argument("--raw", type=int, default=0,
                    help="dump N full program records — read before assuming")
    ap.add_argument("--series", default=None,
                    help="filter to one series prefix, e.g. KXEOWEEK")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        self_test()
        sys.exit(0)

    status = "all" if a.all else a.status
    client = KalshiClient()
    try:
        progs = fetch(client, status, a.type)
    except Exception as e:
        print(f"  request failed: {str(e)[:200]}")
        print("  (the spec lists this endpoint as unauthenticated, but if it "
              "401s, source your read-only key first)")
        sys.exit(1)
    if a.series:
        progs = [p for p in progs
                 if series_of(p.get("market_ticker")).startswith(a.series)]
        print(f"  filtered to {a.series}: {len(progs)} programs")

    if a.raw:
        import json as _json
        print("=" * 78)
        print(f"RAW RECORDS ({min(a.raw, len(progs))} of {len(progs)})")
        print("=" * 78)
        print("\n  Read every field before building anything on this.")
        print("  The number that decides feasibility is not in the summary:")
        print("  how long is a PERIOD, and is period_reward a POOL or a")
        print("  per-participant payment?\n")
        for p in progs[:a.raw]:
            print(_json.dumps(p, indent=2))
            print("-" * 60)
        spans = []
        for p in progs:
            s, e = p.get("start_date"), p.get("end_date")
            if s and e:
                try:
                    from datetime import datetime
                    fmt = "%Y-%m-%dT%H:%M:%S"
                    ds = datetime.fromisoformat(s.replace("Z", "+00:00"))
                    de = datetime.fromisoformat(e.replace("Z", "+00:00"))
                    spans.append((de - ds).total_seconds() / 3600.0)
                except Exception:
                    pass
        if spans:
            spans.sort()
            print(f"\n  PROGRAM DURATION (hours): "
                  f"min {spans[0]:.1f}  median {spans[len(spans)//2]:.1f}  "
                  f"max {spans[-1]:.1f}")
            print("  -> reward per market per DAY = period_reward / "
                  "(duration/24)")
        sys.exit(0)

    report(progs, status)
