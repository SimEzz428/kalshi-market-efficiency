
from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime

from kalshi_client import KalshiClient

CENTI_CENTS_PER_DOLLAR = 10000.0


def dollars(cc) -> float:
    try:
        return float(cc) / CENTI_CENTS_PER_DOLLAR
    except (TypeError, ValueError):
        return 0.0


def hours_between(start, end):
    try:
        a = datetime.fromisoformat((start or "").replace("Z", "+00:00"))
        b = datetime.fromisoformat((end or "").replace("Z", "+00:00"))
        return max((b - a).total_seconds() / 3600.0, 1e-6)
    except Exception:
        return None


def reward_per_day(prog):
    h = hours_between(prog.get("start_date"), prog.get("end_date"))
    if not h:
        return None
    return dollars(prog.get("period_reward")) / (h / 24.0)


def capital_needed(target_size, price_cents, two_sided=True):
    if two_sided:
        return target_size * 1.0
    return target_size * price_cents / 100.0


def screen(client, balance, max_markets, two_sided):
    print("=" * 82)
    print(f"LIQUIDITY PROGRAM SCREEN — balance ${balance:,.2f}, "
          f"{'two-sided' if two_sided else 'ONE-SIDED (optimistic)'}")
    print("=" * 82)

    progs, cursor = [], None
    for _ in range(20):
        params = {"status": "active", "type": "liquidity", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        raw = client.request("GET", "/incentive_programs", params=params)
        batch = raw.get("incentive_programs", []) or []
        progs.extend(batch)
        cursor = raw.get("next_cursor")
        if not cursor or not batch:
            break
    print(f"\n  {len(progs):,} active liquidity programs")

    if two_sided:
        print("""
  ASSUMING TWO-SIDED QUOTING. The yes and no legs of a contract always sum
  to $1, so resting N contracts on both sides costs $N regardless of price.
  The cheap-strike advantage disappears entirely under this assumption.

  Run with --one-sided to see the optimistic case.""")

    rows = []
    for p in progs:
        rpd = reward_per_day(p)
        if rpd is None:
            continue
        try:
            tgt = float(p.get("target_size_fp") or 0)
        except ValueError:
            continue
        if tgt <= 0:
            continue
        rows.append({
            "ticker": p.get("market_ticker", ""),
            "series": (p.get("market_ticker") or "").split("-")[0],
            "rpd": rpd,
            "target": tgt,
            "hours": hours_between(p.get("start_date"), p.get("end_date")),
            "discount": p.get("discount_factor_bps"),
        })

    if not rows:
        print("\n  nothing to rank")
        return

    by_series: dict = {}
    for r in rows:
        d = by_series.setdefault(r["series"], {
            "n": 0, "rpd": 0.0, "targets": set(), "hours": set()})
        d["n"] += 1
        d["rpd"] += r["rpd"]
        d["targets"].add(r["target"])
        if r["hours"]:
            d["hours"].add(round(r["hours"], 1))

    print(f"\n  {'series':<26} {'mkts':>5} {'$/day all':>11} {'$/day ea':>9} "
          f"{'target':>7} {'cap 1 mkt':>10} {'fits?':>6}")
    print("  " + "-" * 80)
    affordable = []
    for s, d in sorted(by_series.items(), key=lambda kv: -kv[1]["rpd"])[:25]:
        tgt = min(d["targets"])
        per = d["rpd"] / max(d["n"], 1)
        cap = capital_needed(tgt, 1.0, two_sided)
        fits = cap <= balance
        if fits:
            affordable.append((s, per, cap, tgt))
        print(f"  {s[:26]:<26} {d['n']:>5} {d['rpd']:>11,.2f} {per:>9,.2f} "
              f"{tgt:>7,.0f} {cap:>9,.0f}$ {'YES' if fits else 'no':>6}")

    print("\n" + "=" * 82)
    print("VERDICT")
    print("=" * 82)
    if not affordable:
        cheapest = min(
            capital_needed(min(d["targets"]), 1.0, two_sided)
            for d in by_series.values())
        print(f"""
  NOTHING is fundable at ${balance:,.2f}.

  The cheapest program on the board needs ${cheapest:,.0f} to hit its target
  size once — {cheapest/max(balance,1):.1f}x your balance — and that is for a
  SINGLE market for a SINGLE period.

  This is not a strategy problem. Liquidity provision is a capital business:
  you are paid rent on inventory you post, so the revenue scales with the
  inventory and there is no version that works without it.""")
    else:
        print(f"\n  {len(affordable)} series fundable at ${balance:,.2f}:\n")
        for s, per, cap, tgt in sorted(affordable, key=lambda x: -x[1])[:10]:
            ret = 100.0 * per / max(cap, 1e-9)
            print(f"    {s:<26} ${per:>7,.2f}/day per market, "
                  f"${cap:,.0f} capital  ({ret:.1f}%/day GROSS)")
        print("""
  Before believing any of those percentages:
    - period_reward is near-certainly a POOL. Divide by the number of
      makers competing, which the API does not report.
    - discount_factor_bps 5000 means quoting far from mid earns less.
    - resting orders get adversely selected. That cost is unmeasured and
      is the reason market making is hard rather than free money.

  The only way to learn the true number is to rest a small quote and
  measure what actually gets paid and what gets picked off.""")


def self_test():
    print("SELF-TEST\n")
    assert dollars(800000) == 80.0
    assert dollars(3333333) == 333.3333
    print(f"  [ok] 800,000 centi-cents = ${dollars(800000):.2f}")
    print(f"  [ok] 3,333,333 centi-cents = ${dollars(3333333):.2f}")

    h = hours_between("2026-08-15T20:00:00Z", "2026-08-15T21:00:00Z")
    assert abs(h - 1.0) < 1e-6, h
    print(f"  [ok] KXTEMPMIAH period = {h:.1f} hour")

    p = {"start_date": "2026-08-15T20:00:00Z", "end_date": "2026-08-15T21:00:00Z",
         "period_reward": 800000}
    assert abs(reward_per_day(p) - 1920.0) < 1e-6, reward_per_day(p)
    print(f"  [ok] $80/hour -> ${reward_per_day(p):,.0f}/day (pool, "
          f"before splitting)")

    p2 = {"start_date": "2026-08-15T16:01:55Z",
          "end_date": "2026-09-05T14:00:00Z", "period_reward": 3333333}
    r2 = reward_per_day(p2)
    assert 15 < r2 < 17, r2
    print(f"  [ok] KXEOWEEK -> ${r2:.2f}/day  "
          f"({reward_per_day(p)/r2:.0f}x less than the hourly programs)")

    for px in (1, 25, 50, 99):
        c = capital_needed(1000, px, two_sided=True)
        assert abs(c - 1000.0) < 1e-9, (px, c)
    print("  [ok] two-sided capital is $1/contract at EVERY price "
          "(yes+no legs sum to $1)")
    assert capital_needed(1000, 2.0, two_sided=False) == 20.0
    assert capital_needed(1000, 50.0, two_sided=False) == 500.0
    print("  [ok] one-sided: 1,000 ct at 2c = $20, at 50c = $500 "
          "(25x — the cheap-strike trick, if allowed)")
    print("\nALL PASS\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--balance", type=float, default=115.0)
    ap.add_argument("--max-markets", type=int, default=400)
    ap.add_argument("--one-sided", action="store_true",
                    help="assume a one-sided quote qualifies (optimistic)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        self_test()
        sys.exit(0)

    screen(KalshiClient(), a.balance, a.max_markets, not a.one_sided)
