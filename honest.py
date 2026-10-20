
from __future__ import annotations

import math

DEFAULT_ORDER_SIZE = 4


def fee_c(price_cents: float, count: int = DEFAULT_ORDER_SIZE) -> float:
    p = max(0.001, min(0.999, price_cents / 100.0))
    order = math.ceil(round(0.07 * count * p * (1 - p) * 100, 9)) / 100.0
    return order / count * 100.0


def breakeven(price_cents: float, count: int = DEFAULT_ORDER_SIZE) -> float:
    return price_cents + fee_c(price_cents, count)


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def honest_stats(values, prices=None):
    n = len(values)
    if n < 2:
        return {"mean": 0.0, "se": 0.0, "t": 0.0, "n": n,
                "raw_se": 0.0, "floored": False, "inflation": 1.0}

    m = sum(values) / n
    var = sum((x - m) ** 2 for x in values) / (n - 1)
    raw_se = math.sqrt(var / n)

    floored = False
    if prices:
        p = (sum(prices) / len(prices)) / 100.0
        p = min(max(p, 0.001), 0.999)
        fv = p * (1.0 - p) * 10000.0
        if fv > var:
            var = fv
            floored = True

    se = math.sqrt(var / n)
    return {
        "mean": m, "se": se, "t": (m / se if se else 0.0), "n": n,
        "raw_se": raw_se, "floored": floored,
        "inflation": (se / raw_se if raw_se > 1e-12 else float("inf")),
    }


def streak_probability(price_cents: float, n: int) -> float:
    return (price_cents / 100.0) ** n


def required_n(effect_cents: float, price_cents: float) -> int:
    p = price_cents / 100.0
    sd = math.sqrt(p * (1 - p)) * 100.0
    return int(((1.96 + 0.84) * sd / effect_cents) ** 2) + 1


def _self_test():
    print("SELF-TEST — honest.py\n")

    px = 93.4
    wins = [100.0 - px - fee_c(px)] * 35
    r = honest_stats(wins, [px] * 35)
    assert r["floored"], "floor did not bind on a 35-win streak"
    assert r["inflation"] > 10, r["inflation"]
    assert abs(r["t"]) < 2.0, f"t should be small, got {r['t']:+.2f}"
    print(f"  [ok] 35/35 wins: raw SE {r['raw_se']:.3f} -> {r['se']:.3f} "
          f"({r['inflation']:.0f}x), t {r['t']:+.2f}")
    print(f"       P(35 straight wins at {px}%) = "
          f"{100*streak_probability(px,35):.1f}%  — unremarkable")

    import random
    rng = random.Random(1)
    obs = [(100.0 if rng.random() < 0.934 else 0.0) - px - fee_c(px)
           for _ in range(2000)]
    r2 = honest_stats(obs, [px] * 2000)
    assert not r2["floored"], "floor bound on a healthy sample"
    print(f"  [ok] 2,000 mixed outcomes: floor did NOT bind, "
          f"SE {r2['se']:.3f}, t {r2['t']:+.2f}")

    assert fee_c(92.0, 4) == 0.75 and fee_c(93.0, 4) == 0.50
    assert fee_c(98.0, 1) == 1.00 and abs(fee_c(98.0, 100) - 0.14) < 0.01
    print(f"  [ok] fees: 92c->{fee_c(92.0,4):.2f}c  93c->{fee_c(93.0,4):.2f}c "
          f"(non-monotonic)")
    print(f"  [ok] 98c: 1 contract {fee_c(98.0,1):.2f}c vs "
          f"100 contracts {fee_c(98.0,100):.2f}c  (7x rounding penalty)")

    assert abs(breakeven(93.0, 4) - 93.50) < 1e-9
    print(f"  [ok] break-even at 93c = {breakeven(93.0,4):.2f}%")

    need = required_n(1.0, 93.0)
    print(f"  [ok] n for a 1c effect at 93c: {need:,}")

    lo, hi = wilson(35, 35)
    print(f"  [ok] Wilson for 35/35 = [{100*lo:.1f}, 100.0]% "
          f"(not [100,100] — the correction works)")
    assert lo < 0.93, lo
    print("\nALL PASS\n")


if __name__ == "__main__":
    _self_test()
