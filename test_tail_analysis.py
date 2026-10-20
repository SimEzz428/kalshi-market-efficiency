
import random

from crypto_scanner import tail_analysis

random.seed(42)
N = 60_000
BAR_MIN, HORIZON = 5, 60


def to_prices(returns: list[float], start: float = 100_000.0) -> list[float]:
    prices, p = [start], start
    for r in returns:
        p *= 2.718281828459045**r
        prices.append(p)
    return prices


def gbm(n: int, sd: float = 0.002) -> list[float]:
    return [random.gauss(0, sd) for _ in range(n)]


def student_t(n: int, sd: float = 0.002, df: float = 3.0) -> list[float]:
    scale = sd / (df / (df - 2)) ** 0.5
    out = []
    for _ in range(n):
        z = random.gauss(0, 1)
        chi = sum(random.gauss(0, 1) ** 2 for _ in range(int(df)))
        out.append(scale * z / (chi / df) ** 0.5)
    return out


def garch(n: int, sd: float = 0.002) -> list[float]:
    omega, alpha, beta = sd**2 * 0.02, 0.12, 0.86
    var, out = sd**2, []
    for _ in range(n):
        e = random.gauss(0, var**0.5)
        out.append(e)
        var = omega + alpha * e**2 + beta * var
    return out


def report(name: str, prices: list[float], expect: str) -> None:
    res = tail_analysis(prices, BAR_MIN, HORIZON, lookback_bars=24)
    print(f"\n{name}   (expected: {expect})")
    print(f"  {'k':>5} {'GBM':>9} {'actual':>9} {'ratio':>8} {'n':>8}")
    for k, (g, a, n) in sorted(res.items()):
        print(f"  {k:>5.1f} {g:>8.2%} {a:>8.2%} {a/g if g else 0:>7.2f}x {n:>8,}")
    return res


if __name__ == "__main__":
    print("=" * 66)
    print("VALIDATING THE TAIL STUDY ON DATA WITH KNOWN ANSWERS")
    print("=" * 66)

    r1 = report("1. Pure lognormal (GBM)", to_prices(gbm(N)),
                "ratio ~1.0 everywhere -- this is the null case")
    r2 = report("2. Student-t, df=3 (true fat tails)", to_prices(student_t(N)),
                "ratio > 1, growing with k")
    r3 = report("3. GARCH vol clustering (Gaussian steps!)", to_prices(garch(N)),
                "ratio > 1 despite NO fat tails per step")

    print("\n" + "=" * 66)
    print("CHECKS")
    ok = True

    r_1sig = r1[1.0][1] / r1[1.0][0]
    p1 = 0.80 < r_1sig < 1.30
    ok &= p1
    print(f"  [{'PASS' if p1 else 'FAIL'}] GBM null case at 1 sigma: {r_1sig:.2f}x (want ~1.0)")

    p2 = (r2[2.0][1] / r2[2.0][0]) > (r1[2.0][1] / r1[2.0][0])
    ok &= p2
    print(f"  [{'PASS' if p2 else 'FAIL'}] Student-t exceeds GBM at 2 sigma: "
          f"{r2[2.0][1]/r2[2.0][0]:.2f}x vs {r1[2.0][1]/r1[2.0][0]:.2f}x")

    p3 = (r2[3.0][1] / r2[3.0][0]) > (r2[1.0][1] / r2[1.0][0])
    ok &= p3
    print(f"  [{'PASS' if p3 else 'FAIL'}] Student-t ratio grows with k: "
          f"{r2[1.0][1]/r2[1.0][0]:.2f}x at 1s -> {r2[3.0][1]/r2[3.0][0]:.2f}x at 3s")

    p4 = (r3[2.0][1] / r3[2.0][0]) > (r1[2.0][1] / r1[2.0][0])
    ok &= p4
    print(f"  [{'PASS' if p4 else 'FAIL'}] GARCH also shows excess tails: "
          f"{r3[2.0][1]/r3[2.0][0]:.2f}x (Gaussian steps, no true fat tails)")

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    print("""
TAKEAWAY FOR THE BTC RUN
  Cases 2 and 3 produce similar-looking output from completely different causes.
  So when the live BTC study shows excess tails -- and it will -- you cannot yet
  say WHY. Distinguishing them is the real work:
    - If it's vol clustering (case 3), the fix is a better vol estimator, and
      market makers already have one. Edge is small and fleeting.
    - If it's true fat tails (case 2), cheap far-OTM contracts are structurally
      underpriced, and that edge is more durable.
  Test: re-run the study conditioning on vol regime. If the excess disappears
  once you control for current volatility, it was case 3.
""")
