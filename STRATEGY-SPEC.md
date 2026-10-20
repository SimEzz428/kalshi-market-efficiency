# Kalshi 15-Minute Crypto — Firm Plan v2

**Status: plan is firm. Edge is unproven. Zero data collected. Paper only.**

---

## 1. The core strategy

Buy the **favorite** on Kalshi 15-minute crypto up/down markets, as a **maker**, and **hold to
settlement**.

| Parameter | Value | Why |
|---|---|---|
| Coins (trade) | **BTC, ETH** | Tight spreads → you measure edge, not spread noise |
| Coins (log) | BTC, ETH, SOL, XRP, DOGE | Alts may be worse priced; need a baseline to tell |
| Timing | **3–6 minutes remaining** | Edge grows toward expiry; your size is too small for thin depth to bind |
| Price band | **90¢ – 96¢** | Below 90 the edge shrinks; above 96 a 1¢ spread eats 25% of a 4¢ upside |
| Order type | **Resting limit at the bid — never cross** | Cuts the hurdle from 0.57pt to 0.14pt. Biggest single lever. |
| Unfilled | Skip the window | Never chase |
| Size | **$25–30**, one position at a time | Quarter-Kelly on $1,000, 3% cap |
| Exit | **Hold to settlement** | Early exit doubles fees (+0.39pt hurdle) |
| Exit exception | Consider closing above 96¢ | At 97¢ you risk 97¢ to win 3¢ (32:1) — a worse bet than you entered |

**Why 3–6 minutes rather than a hard 7:**

| Time left | Averaging edge | Depth |
|---|---|---|
| 7 min | +1.07pt | Deep |
| 5 min | +1.51pt | Good |
| 3 min | +2.58pt | Thinning |
| 2 min | +3.97pt | Thin |

At $25–30 you're buying ~30 contracts. That's small enough that thinning depth likely doesn't bind
on you — a genuine small-player advantage. Institutions have to pull back where you don't.

**Hard rule: ONE position at a time across all coins.** Crypto is ~0.85 correlated on 15-minute
horizons. Five simultaneous positions is one bet at 5x size — it nearly doubles your volatility for
zero extra expected return. All five lose together roughly once every 32 windows.

**Risk controls**

| Control | Limit |
|---|---|
| Max trades/day | ~10 (fee drag is linear in trade count) |
| Daily stop | −$100 → done for the day |
| Hard halt | −20% of bankroll → stop and re-examine |

---

## 2. The correlated-combo variant

**The finding.** If Kalshi prices multi-leg combos as the simple *product* of the leg probabilities,
that assumes independence. Positively correlated events co-occur **more** often than independence
predicts — so a combo of correlated legs priced at the product is **underpriced**.

Worked example, two legs at 91.30% and 98.04%:

| Correlation | True P(both) | vs. product | Edge |
|---|---|---|---|
| 0.00 | 89.54% | — | none |
| 0.60 | 90.41% | +0.89% | +0.89pt |
| **0.85 (two cryptos)** | **91.10%** | **+1.59%** | **+1.59pt** |

That's larger than the base strategy's edge.

**Critical requirement: the legs must be correlated.**

- ✅ **BTC + ETH** (both 15-min, same direction) → correlation ~0.85 → edge if priced at product
- ❌ **Tennis + crypto** → correlation ≈ 0 → product IS fair → **no edge**. This is just a leveraged
  independent bet, and fees on both legs compound the drag.

**Note the symmetry with §1:** correlation *hurts* you when holding separate positions (variance) and
*helps* you inside a combo priced at independence. Same fact, opposite sign, depending on structure.

**Two things to verify before trusting this:**

1. Does Kalshi price combos at the product, or does it apply a correlation adjustment? Test: price a
   BTC+ETH same-direction combo, compare the cost to the product of the individual legs.
2. Does Kalshi even permit same-category correlated combos? Sportsbooks routinely ban these because
   this exploit is well known.

---

## 3. Known vs. unknown

**Verified**

- Kalshi fee formula: `0.07 × contracts × P × (1−P)`, maker = 25% of taker
- Maker breakeven at 91¢: **91.14%**. Taker: **91.57%**
- Kalshi's help center states all crypto contracts settle on a 60-second average of the CF
  Benchmarks Real-Time Index
- Book depth: hundreds to thousands of contracts within 2¢ of mid — not a constraint at your size
- Book shape: widest in first 5 min, deepest in middle, thins in last 5

**Unknown — and each one gates something**

| # | Question | Gates |
|---|---|---|
| 1 | Does the 60s averaging apply to **15-minute** markets? (Kalshi says all crypto; a vendor says hourly only) | The entire averaging edge, and §4 |
| 2 | Is the order book open **during** the averaging window? | §4 entirely |
| 3 | Does Kalshi price combos at the independent product? | §2 entirely |
| 4 | Is favorite-longshot bias present on Kalshi? | §1's second mechanism |
| 5 | What's the real fill rate on resting bids, and are they adversely selected? | Whether maker-only is executable at all |

---

## 4. The big variant (gated on unknowns #1 and #2)

If settlement is a 60-second average **and** the book stays open during it, then partway through the
final minute part of the settlement value is already locked in and observable:

| Seconds left | Effective vol vs. naive | Market says 90% → true |
|---|---|---|
| 60s | 58% | 98.7% |
| 45s | 43% | 99.9% |
| 30s | 29% | ~100% |

~10 points versus ~1 point for the base strategy. Also a latency race in the final minute against
anyone else who worked it out.

**Verify #1 and #2 before spending any effort here.** If either is false, this is worth zero.

---

## 5. Phase plan

### Phase 0 — Facts (this week, ~2 hours, $0)

- [ ] Pull settled 15-min markets via API; compare `expiration_value` against (a) the index print at
      close and (b) the 60-second average. Whichever matches to the cent answers unknown #1.
- [ ] Watch one market close. Is the book live at T−60s? Answers unknown #2.
- [ ] Price a BTC+ETH same-direction combo; compare to the product of the legs. Answers unknown #3.
- [ ] Read the per-market rules text on an actual `KXBTC15M` page — authoritative over help articles.

**Gate:** these three facts determine which of §1, §2, §4 is even worth pursuing. Do not skip.

### Phase 1 — Observation (2 weeks, $0)

- [ ] Run the logger across all five coins. Record every 15-min window: timestamps, best bid/ask on
      both sides, spot, strike, seconds remaining, and settlement.
- [ ] Target **300+ observations** in the 90–96¢ band at 3–6 minutes.

### Phase 2 — Analysis (1 day)

The one question: **did contracts priced 90–96¢ settle YES more often than price + fee?**

- [ ] Bucket by price (90–92, 92–94, 94–96) and by time remaining (3–4, 4–5, 5–6 min)
- [ ] Compute realized settlement rate per bucket vs. the breakeven rate
- [ ] Also compute: implied vol vs. realized vol per coin; UP+DOWN sum distribution (pair-arb
      frequency); resting-bid fill quality

### Phase 3 — Paper (4 weeks, $0)

- [ ] Run the full strategy in Kalshi's demo environment
- [ ] Assume **pessimistic fills** — that you only fill when you're about to be wrong
- [ ] 150+ paper trades minimum

### Phase 4 — Live, tiny (only if Phases 2–3 pass)

- [ ] $25 max per position, one at a time, maker only
- [ ] Journal every trade; compare live fills to paper assumptions

---

## 6. Kill criteria

Stop and do something else if **any** of these are true:

- The 90–96¢ band settles at or below breakeven over 300+ observations
- Resting bids fill under ~50% of the time, or fills are clearly adversely selected
- UP+DOWN sums never drop below the maker arb threshold
- Kalshi prices combos with a correlation adjustment (kills §2)
- The book is closed during the averaging window AND settlement isn't averaged (kills §4 and most of §1)
- Live results diverge sharply from paper for two consecutive weeks

---

## 7. Honest expectations

| Outcome | Probability (my estimate) | Result |
|---|---|---|
| No edge found; Phase 2 kills it | **~65%** | $0 lost, real skills gained |
| Small edge, marginal after execution | ~25% | $20–60/month |
| Real edge | ~10% | $50–150/month |

The plan is built so that the 65% case costs you nothing but time. That's the whole design intent.

Note the ceiling: even the good case is bounded by your bankroll, not your cleverness. The strategy
does not scale far past a few thousand dollars for a solo operator.

---

*Not financial advice. Every number above is a hypothesis until measured.*
