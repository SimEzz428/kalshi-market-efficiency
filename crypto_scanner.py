
from __future__ import annotations

import argparse
import math
import statistics
import sys
import time

import requests

from edge import evaluate, min_edge_required

COINBASE = "https://api.exchange.coinbase.com"
MINUTES_PER_YEAR = 365.25 * 24 * 60


def fetch_candles(product: str = "BTC-USD", granularity: int = 60, limit: int = 300) -> list[dict]:
    r = requests.get(
        f"{COINBASE}/products/{product}/candles",
        params={"granularity": granularity},
        timeout=15,
        headers={"User-Agent": "kalshi-research/0.1"},
    )
    r.raise_for_status()
    rows = sorted(r.json(), key=lambda x: x[0])[-limit:]
    return [
        {"time": c[0], "low": c[1], "high": c[2], "open": c[3], "close": c[4], "volume": c[5]}
        for c in rows
    ]


def spot_price(product: str = "BTC-USD") -> float:
    r = requests.get(f"{COINBASE}/products/{product}/ticker", timeout=15,
                     headers={"User-Agent": "kalshi-research/0.1"})
    r.raise_for_status()
    return float(r.json()["price"])


def log_returns(closes: list[float]) -> list[float]:
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
            if closes[i - 1] > 0 and closes[i] > 0]


def realized_vol(closes: list[float], annualize: bool = True) -> float:
    rets = log_returns(closes)
    if len(rets) < 2:
        return 0.0
    sd = statistics.stdev(rets)
    return sd * math.sqrt(MINUTES_PER_YEAR) if annualize else sd


def ewma_vol(closes: list[float], halflife_min: float = 30.0) -> float:
    rets = log_returns(closes)
    if len(rets) < 2:
        return 0.0
    lam = 0.5 ** (1.0 / halflife_min)
    weights = [lam ** (len(rets) - 1 - i) for i in range(len(rets))]
    wsum = sum(weights)
    mean = sum(w * r for w, r in zip(weights, rets)) / wsum
    var = sum(w * (r - mean) ** 2 for w, r in zip(weights, rets)) / wsum
    return math.sqrt(var) * math.sqrt(MINUTES_PER_YEAR)


def vol_term_structure(product: str = "BTC-USD") -> dict[str, float]:
    candles = fetch_candles(product, granularity=60, limit=300)
    closes = [c["close"] for c in candles]
    return {
        "rv_30m": realized_vol(closes[-31:]),
        "rv_60m": realized_vol(closes[-61:]),
        "rv_120m": realized_vol(closes[-121:]),
        "rv_300m": realized_vol(closes),
        "ewma_30m_hl": ewma_vol(closes, halflife_min=30),
    }


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_above(spot: float, strike: float, vol_annual: float, hours: float) -> float:
    if hours <= 0:
        return 1.0 if spot > strike else 0.0
    if vol_annual <= 0 or spot <= 0 or strike <= 0:
        return 1.0 if spot > strike else 0.0
    tau = hours / (365.25 * 24)
    sigma_t = vol_annual * math.sqrt(tau)
    d = (math.log(spot / strike) - 0.5 * sigma_t**2) / sigma_t
    return norm_cdf(d)


def prob_between(spot: float, low: float, high: float, vol_annual: float, hours: float) -> float:
    return max(0.0, prob_above(spot, low, vol_annual, hours) -
               prob_above(spot, high, vol_annual, hours))


def tail_analysis(
    closes: list[float], bar_min: int = 5, horizon_min: int = 60, lookback_bars: int = 24
) -> dict[float, tuple[float, float, int]]:
    steps = max(1, horizon_min // bar_min)
    buckets: dict[float, list[int]] = {1.0: [0, 0], 1.5: [0, 0], 2.0: [0, 0],
                                       2.5: [0, 0], 3.0: [0, 0]}
    for i in range(lookback_bars, len(closes) - steps):
        window = closes[i - lookback_bars : i + 1]
        rets = log_returns(window)
        if len(rets) < 10:
            continue
        sd_bar = statistics.stdev(rets)
        if sd_bar <= 0:
            continue
        sd_h = sd_bar * math.sqrt(steps)
        actual = abs(math.log(closes[i + steps] / closes[i]))
        for k, cnt in buckets.items():
            cnt[1] += 1
            if actual > k * sd_h:
                cnt[0] += 1
    return {
        k: (2 * (1 - norm_cdf(k)), hits / total if total else 0.0, total)
        for k, (hits, total) in sorted(buckets.items())
    }


def print_tail_table(results: dict[float, tuple[float, float, int]]) -> None:
    print(f"  {'move':>6} {'GBM says':>10} {'actual':>10} {'ratio':>8}   interpretation")
    print("  " + "-" * 62)
    for k, (gbm, act, n) in sorted(results.items()):
        if n == 0:
            continue
        ratio = act / gbm if gbm > 0 else float("nan")
        verdict = ("GBM UNDERPRICES" if ratio > 1.15
                   else "GBM overprices" if ratio < 0.85 else "roughly fair")
        print(f"  {k:>5.1f}s {gbm:>9.2%} {act:>9.2%} {ratio:>7.2f}x   {verdict}")


def tail_study(product: str = "BTC-USD", horizon_min: int = 60) -> None:
    print(f"Fetching history for {product}...")
    all_candles: list[dict] = []
    end = int(time.time())
    for _ in range(12):
        try:
            r = requests.get(
                f"{COINBASE}/products/{product}/candles",
                params={"granularity": 300, "end": end},
                timeout=15,
                headers={"User-Agent": "kalshi-research/0.1"},
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            all_candles.extend(batch)
            end = min(c[0] for c in batch) - 1
            time.sleep(0.35)
        except Exception as e:
            print(f"  (stopped fetching: {e})")
            break

    if len(all_candles) < 500:
        print("Not enough history retrieved. Check network access to Coinbase.")
        return

    candles = sorted({c[0]: c for c in all_candles}.values(), key=lambda x: x[0])
    closes = [c[4] for c in candles]
    bar_min = 5

    print(f"  {len(closes)} bars of {bar_min}m data "
          f"(~{len(closes)*bar_min/1440:.1f} days), horizon={horizon_min}m\n")

    print_tail_table(tail_analysis(closes, bar_min, horizon_min, lookback_bars=24))

    print("""
  How to read this: ratio > 1 means the real world produces big moves more often
  than the lognormal model expects. Where that holds, cheap far-from-money YES
  contracts (3-10c) priced off a GBM-style model are systematically too cheap --
  and those strikes also carry the lowest Kalshi fees.

  Caveats before you get excited:
    - This is unconditional. Real vol clusters, so some of the excess is just
      the model using a stale sigma, not true fat tails. Condition on vol regime
      to separate the two.
    - Kalshi's makers are not naive GBM users. Verify against real books before
      assuming the mispricing exists.
    - Small n in the 3-sigma bucket. Treat it as a hypothesis, not a result.
""")


def parse_strike(market: dict) -> float | None:
    for field in ("floor_strike", "cap_strike", "strike_value"):
        v = market.get(field)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def scan_live(series: str = "KXBTCD", bankroll: float = 500.0, product: str = "BTC-USD") -> None:
    try:
        from kalshi_client import KalshiClient, time_to_close
    except ImportError:
        print("kalshi_client.py not found.")
        return

    client = KalshiClient()
    print(f"env={client.env}  series={series}")

    candles = fetch_candles(product, granularity=60, limit=300)
    closes = [c["close"] for c in candles]
    spot = spot_price(product)
    vol = ewma_vol(closes, halflife_min=30)
    print(f"spot={spot:,.0f}  ewma_vol={vol:.1%} annualized\n")

    markets = client.get_markets(series_ticker=series, status="open")
    if not markets:
        print(f"No open markets for series '{series}'. List available series first:")
        print("  python -c \"from kalshi_client import KalshiClient as K; "
              "print([e['series_ticker'] for e in K().get_events()][:40])\"")
        return

    found = []
    for m in markets:
        strike = parse_strike(m)
        hours = time_to_close(m)
        bid, ask = m.get("yes_bid"), m.get("yes_ask")
        if not strike or not hours or hours <= 0 or not bid or not ask:
            continue
        if bid <= 0 or ask >= 100:
            continue

        model_p = prob_above(spot, strike, vol, hours)
        opp = evaluate(m["ticker"], model_p, bid / 100.0, ask / 100.0, bankroll, maker=True)
        if opp:
            found.append((opp, strike, hours))

    if not found:
        print("No fee-adjusted edges found. That is the normal and expected result.")
        return

    found.sort(key=lambda x: -x[0].ev)
    print(f"{len(found)} candidate(s), best first:\n")
    for opp, strike, hours in found[:15]:
        print(f"  {opp}")
        print(f"      strike=${strike:,.0f}  {hours:.1f}h to close  "
              f"hurdle={min_edge_required(opp.market_price, opp.spread, True):.2%}")

    print("""
  Before acting on ANY of these, ask: is the model right, or is the market right?
  The market is right far more often than your model is. A large reported edge on
  a liquid near-money strike almost always means your vol estimate is wrong, not
  that you found a mispricing. Log these to a file and check them against
  settlement for a few weeks before you risk a cent.
""")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tails", action="store_true", help="GBM vs reality tail study (offline)")
    ap.add_argument("--vol", action="store_true", help="show realized vol term structure")
    ap.add_argument("--scan", action="store_true", help="live Kalshi edge scan (needs keys)")
    ap.add_argument("--product", default="BTC-USD")
    ap.add_argument("--series", default="KXBTCD")
    ap.add_argument("--bankroll", type=float, default=500.0)
    ap.add_argument("--horizon", type=int, default=60, help="tail study horizon in minutes")
    args = ap.parse_args()

    if not any([args.tails, args.vol, args.scan]):
        print("No mode selected. Showing the pricing model on synthetic inputs.\n")
        spot, vol = 100_000.0, 0.55
        print(f"  spot=${spot:,.0f}  vol={vol:.0%} annualized\n")
        print(f"  {'strike':>10} {'1h':>8} {'6h':>8} {'24h':>8}")
        print("  " + "-" * 38)
        for k in [96_000, 98_000, 99_000, 100_000, 101_000, 102_000, 104_000]:
            row = "  ".join(f"{prob_above(spot, k, vol, h):>7.1%}" for h in (1, 6, 24))
            print(f"  {k:>10,} {row}")
        print("\n  Note how a $2k move is nearly impossible in 1h but routine in 24h.")
        print("  Run with --tails, --vol, or --scan. See --help.")
        sys.exit(0)

    if args.vol:
        print(f"Realized volatility, {args.product} (annualized):\n")
        for k, v in vol_term_structure(args.product).items():
            print(f"  {k:<16} {v:>7.1%}")
        print("\n  Big gap between rv_30m and rv_300m = regime change in progress.")
    if args.tails:
        tail_study(args.product, args.horizon)
    if args.scan:
        scan_live(args.series, args.bankroll, args.product)
