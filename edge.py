
from __future__ import annotations

import math
from dataclasses import dataclass


TAKER_RATE = 0.07
MAKER_MULTIPLIER = 0.25


def trade_fee(price: float, contracts: int = 1, maker: bool = False) -> float:
    if not 0.0 < price < 1.0:
        raise ValueError(f"price must be in (0, 1), got {price}")
    rate = TAKER_RATE * (MAKER_MULTIPLIER if maker else 1.0)
    raw = rate * contracts * price * (1.0 - price)
    return math.ceil(round(raw * 100, 9)) / 100.0


def fee_per_contract(price: float, maker: bool = False) -> float:
    rate = TAKER_RATE * (MAKER_MULTIPLIER if maker else 1.0)
    return rate * price * (1.0 - price)


def min_edge_required(
    price: float,
    spread: float = 0.02,
    maker: bool = False,
    exit_early: bool = False,
) -> float:
    fee = fee_per_contract(price, maker=maker)
    if exit_early:
        fee *= 2.0
    spread_cost = 0.0 if maker else spread / 2.0
    return fee + spread_cost


def american_to_decimal(odds: int) -> float:
    if odds == 0:
        raise ValueError("American odds cannot be 0")
    return 1.0 + (odds / 100.0 if odds > 0 else 100.0 / abs(odds))


def implied_probs(decimal_odds: list[float]) -> list[float]:
    return [1.0 / o for o in decimal_odds]


def devig_multiplicative(decimal_odds: list[float]) -> list[float]:
    raw = implied_probs(decimal_odds)
    total = sum(raw)
    return [p / total for p in raw]


def devig_power(decimal_odds: list[float], tol: float = 1e-10) -> list[float]:
    raw = implied_probs(decimal_odds)
    lo, hi = 0.05, 30.0
    for _ in range(300):
        k = (lo + hi) / 2.0
        s = sum(p**k for p in raw)
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2.0
    out = [p**k for p in raw]
    total = sum(out)
    if abs(total - 1.0) > 1e-4:
        raise ValueError(
            f"de-vig failed to converge: probabilities sum to {total:.4f}, "
            f"not 1. raw implied = {[round(p,4) for p in raw]}")
    return out


def devig_shin(decimal_odds: list[float], tol: float = 1e-10) -> list[float]:
    raw = implied_probs(decimal_odds)
    total = sum(raw)
    n = len(raw)
    if n != 2:
        return devig_power(decimal_odds)

    lo, hi = 0.0, 0.4
    for _ in range(200):
        z = (lo + hi) / 2.0
        probs = [
            (math.sqrt(z**2 + 4 * (1 - z) * (p**2) / total) - z) / (2 * (1 - z))
            for p in raw
        ]
        s = sum(probs)
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            lo = z
        else:
            hi = z
    z = (lo + hi) / 2.0
    return [
        (math.sqrt(z**2 + 4 * (1 - z) * (p**2) / total) - z) / (2 * (1 - z))
        for p in raw
    ]


def kelly_fraction(true_prob: float, price: float) -> float:
    if not 0.0 < price < 1.0:
        raise ValueError(f"price must be in (0, 1), got {price}")
    f = (true_prob - price) / (1.0 - price)
    return max(0.0, f)


def position_size(
    true_prob: float,
    price: float,
    bankroll: float,
    kelly_multiplier: float = 0.25,
    max_position_pct: float = 0.05,
) -> tuple[int, float]:
    f = kelly_fraction(true_prob, price) * kelly_multiplier
    f = min(f, max_position_pct)
    dollars = bankroll * f
    contracts = int(dollars / price)
    return contracts, contracts * price


@dataclass
class Opportunity:
    ticker: str
    side: str
    model_prob: float
    market_price: float
    spread: float
    raw_edge: float
    net_edge: float
    contracts: int
    dollars: float
    ev: float

    def __str__(self) -> str:
        return (
            f"{self.ticker:<28} {self.side:<4} "
            f"model={self.model_prob:6.1%}  mkt={self.market_price:6.1%}  "
            f"net_edge={self.net_edge:+6.2%}  size={self.contracts:>4}c "
            f"(${self.dollars:6.2f})  EV=${self.ev:+6.2f}"
        )


def evaluate(
    ticker: str,
    model_prob: float,
    yes_bid: float,
    yes_ask: float,
    bankroll: float,
    maker: bool = True,
    exit_early: bool = False,
) -> Opportunity | None:
    spread = yes_ask - yes_bid
    if spread <= 0:
        return None

    candidates = []

    yes_price = yes_bid if maker else yes_ask
    if 0.0 < yes_price < 1.0:
        hurdle = min_edge_required(yes_price, spread, maker, exit_early)
        raw = model_prob - yes_price
        if raw - hurdle > 0:
            candidates.append(("YES", model_prob, yes_price, raw, raw - hurdle))

    no_yes_price = yes_ask if maker else yes_bid
    no_price = 1.0 - no_yes_price
    no_model_prob = 1.0 - model_prob
    if 0.0 < no_price < 1.0:
        hurdle = min_edge_required(no_price, spread, maker, exit_early)
        raw = no_model_prob - no_price
        if raw - hurdle > 0:
            candidates.append(("NO", no_model_prob, no_price, raw, raw - hurdle))

    if not candidates:
        return None

    side, p, price, raw, net = max(candidates, key=lambda c: c[4])
    contracts, dollars = position_size(p, price, bankroll)
    if contracts < 1:
        return None

    return Opportunity(
        ticker=ticker,
        side=side,
        model_prob=p,
        market_price=price,
        spread=spread,
        raw_edge=raw,
        net_edge=net,
        contracts=contracts,
        dollars=dollars,
        ev=net * contracts,
    )


if __name__ == "__main__":
    ok = True

    def check(name: str, got, want, tol=1e-6):
        global ok
        passed = abs(got - want) < tol
        ok &= passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}: got {got:.6f}, want {want:.6f}")

    print("Fees (hand-checked against Kalshi's published formula)")
    check("taker fee, 1 contract @ 50c", trade_fee(0.50, 1), 0.02)
    check("taker fee, 100 contracts @ 50c", trade_fee(0.50, 100), 1.75)
    check("taker fee, 100 contracts @ 10c", trade_fee(0.10, 100), 0.63)
    check("maker fee, 100 contracts @ 50c", trade_fee(0.50, 100, maker=True), 0.44)
    check("per-contract unrounded @ 50c", fee_per_contract(0.50), 0.0175)

    print("\nBreakeven edge")
    check("taker, 2c spread, round trip", min_edge_required(0.50, 0.02, False, True), 0.045)
    check("taker, 2c spread, hold", min_edge_required(0.50, 0.02, False, False), 0.0275)
    check("maker, hold to settlement", min_edge_required(0.50, 0.02, True, False), 0.004375)

    print("\nDe-vigging (-110 / -110 is the standard US sports line)")
    d = [american_to_decimal(-110), american_to_decimal(-110)]
    check("decimal odds of -110", d[0], 1.909090909)
    raw = implied_probs(d)
    check("raw implied sum (the vig)", sum(raw), 1.047619048)
    for method, fn in [("multiplicative", devig_multiplicative), ("power", devig_power), ("shin", devig_shin)]:
        p = fn(d)
        check(f"{method}: symmetric line -> 50%", p[0], 0.5, tol=1e-5)

    print("\nDe-vigging an asymmetric line (-250 / +200)")
    d2 = [american_to_decimal(-250), american_to_decimal(200)]
    for method, fn in [("multiplicative", devig_multiplicative), ("power", devig_power), ("shin", devig_shin)]:
        p = fn(d2)
        assert abs(sum(p) - 1.0) < 1e-6, f"{method} must sum to 1"
        print(f"  [PASS] {method:<15} fav={p[0]:.4f} dog={p[1]:.4f} (sums to 1)")
    print("  note: power/shin assign the LONGSHOT a lower probability than multiplicative")
    print("        (0.305 / 0.310 vs 0.318). That is the favorite-longshot bias correction:")
    print("        raw implied odds systematically overstate longshots, so more of the vig")
    print("        should be stripped from the underdog side than from the favorite.")

    print("\nKelly sizing")
    check("full Kelly, p=55% @ 50c", kelly_fraction(0.55, 0.50), 0.10)
    check("no edge -> no bet", kelly_fraction(0.45, 0.50), 0.0)
    c, d_ = position_size(0.55, 0.50, 500)
    check("quarter Kelly contracts on $500", c, 25)
    check("quarter Kelly dollars on $500", d_, 12.50)
    c, d_ = position_size(0.95, 0.50, 500)
    check("5% cap enforced on large edge", d_, 25.00)

    print("\nOpportunity screen")
    opp = evaluate("BTC-EXAMPLE", 0.58, 0.50, 0.52, 500, maker=True)
    assert opp is not None, "should find edge"
    print(f"  [PASS] {opp}")
    opp2 = evaluate("BTC-NOEDGE", 0.51, 0.50, 0.52, 500, maker=False)
    print(f"  [{'PASS' if opp2 is None else 'FAIL'}] taker: thin edge correctly rejected")
    ok &= opp2 is None

    opp3 = evaluate("BTC-MM", 0.51, 0.50, 0.52, 500, maker=True)
    print(f"  [INFO] maker inside a 2c spread always shows edge: {opp3.side} "
          f"net={opp3.net_edge:+.2%}  <- this is market making, not alpha")

    print("\n" + ("ALL TESTS PASSED" if ok else "SOME TESTS FAILED"))
