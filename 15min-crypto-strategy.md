# Kalshi 15-Minute Crypto Markets — Strategy Analysis

*Working document. Everything here is a hypothesis until measured.*

---

## 1. Confirmed mechanics

| Property | Value |
|---|---|
| Ticker family | `KXBTC15M` (canonical prefix often appears as `KXBTCD`) |
| Cadence | New up/down pair every quarter hour, 24/7, ~96 windows/day |
| Coins | BTC, ETH, SOL, XRP, DOGE, and others — 7+ families |
| Strike | Set near the prevailing price at open, **banded to a round number** |
| Depth | A few hundred to a few thousand contracts within 2¢ of mid |
| Settlement | Binary, $1 or $0 |

**Depth is the good news.** "Hundreds to thousands of contracts within 2¢ of mid" means a $1,000
account is nowhere near the size where market impact matters. Liquidity is not your constraint —
edge is.

**Book shape through the window** (this matters a lot for timing):

| Phase | Character |
|---|---|
| First 5 min | Wide spreads, exploratory pricing, MMs still finding fair value |
| **Middle 5 min** | **Tightest spreads, deepest book — best regime** |
| Last 5 min | Spreads tighten, but depth thins as MMs pull risk |
| Last 60 sec | Effectively a settlement auction; price slams to 0.99 or 0.01 |

Note where your 7-minute observation falls: 7 minutes remaining is 8 minutes into the window —
squarely in the deepest, tightest part of the book. Your instinct picked the right window.

---

## 2. THE OPEN QUESTION — resolve this first

**Sources conflict on how the 15-minute markets settle, and the answer determines whether
Strategy A below is real or imaginary.**

- **Kalshi's own help center** states that *all* crypto contracts settle on a 60-second average
  of the CF Benchmarks Real-Time Index: *"At the last minute before expiration, 60 RTI prices are
  collected, the official and final value is the average of these prices."*
- **A third-party data vendor** states that the 15-minute markets settle on an `expiration_value`
  captured directly on the market record, and that it's the *hourly* markets that use the CF
  Benchmarks index.

These cannot both be right. Kalshi's own documentation is the more authoritative source, but it
speaks generally about "crypto markets" and may not have been written with the 15-minute family
specifically in mind.

**How to settle it (costs nothing):**

1. Open any live `KXBTC15M` market on Kalshi and read the market rules text on the page itself.
   The per-market rules are authoritative over any help article.
2. Pull a few settled markets via the API and inspect the `expiration_value` field. Compare it
   against (a) the RTI print at the exact close timestamp and (b) the 60-second average of RTI
   prints. Whichever matches to the cent is your answer.

Do #2 regardless — it's about 20 lines of code and it converts a documentation question into a
measured fact.

---

## 3. Strategy A — The averaging edge

**Applies only if the 60-second averaging is confirmed for 15-minute markets.**

**Mechanism.** If settlement is the *average* of the final 60 seconds rather than the price *at*
the close, the settlement value has less variance than the closing price. For Brownian motion,
`Var(average over last 60s) = σ²(τ−40)` versus `Var(price at close) = σ²τ`. Averaging erases
40 seconds of variance. Less variance near expiry → fewer late flips → **favorites are worth more
than a naive point-in-time model says.**

**Size of the effect,** for a contract a naive model prices at 91¢:

| Time left | Naive | True | Edge |
|---|---|---|---|
| 15 min | 91% | 91.49% | +0.49pt |
| **7 min** | 91% | **92.07%** | **+1.07pt** |
| 5 min | 91% | 92.51% | +1.51pt |
| 3 min | 91% | 93.58% | +2.58pt |
| 2 min | 91% | 94.97% | +3.97pt |
| 1 min | 91% | 98.88% | +7.88pt |

**The bind:** the edge is biggest in the last 2 minutes, but that's exactly when depth thins and
the book converges to 0.99/0.01. Where the edge is large you can't get size; where you can get
size the edge is ~1 point. Seven minutes is the compromise, which is why your instinct landed there.

**Hurdle to clear:** 0.14pt as a maker, 0.57pt as a taker. A 1.07pt effect clears both — *if the
market hasn't already priced it.*

**Why it probably has.** The averaging rule is in Kalshi's public help center. The firms quoting
these books have read it. Assume the effect is priced in and be pleasantly surprised if not. What
you'd actually be measuring is whether it's *fully* priced, not whether it exists.

**Verdict:** worth measuring, don't expect much.

---

## 4. Strategy B — Up/Down pair arbitrage ⭐ most promising

**Mechanism.** Every window has a matched UP and DOWN contract. Exactly one pays $1. So if you
buy both and the combined cost plus fees is under $1, you have **locked in profit with zero
directional risk.** You do not care what Bitcoin does.

**What the sum has to be:**

| Prices | Taker: sum must be < | Maker: sum must be < |
|---|---|---|
| 50 / 50 | 96.5¢ (needs a 3.5¢ error) | **99.1¢** |
| 80 / 20 | 97.8¢ | **99.4¢** |
| 90 / 10 | 98.7¢ | **99.7¢** |
| 95 / 5 | 99.3¢ | **99.8¢** |

**This is the single most important row in this document:** as a *taker* you need a 3.5¢
mispricing that essentially never appears. As a *maker* at the wings you need less than **0.3¢**.
That is a completely different proposition.

Note the wings are cheapest — fees fall toward 0 and 100, so a 90/10 pair needs a far smaller
dislocation than a 50/50 pair. Combine that with your interest in high-probability contracts and
these point at the same place.

**Economics:** catching a 1¢ mispricing at 90/10 as a maker returns **0.69% per cycle, riskless**.
With 96 windows/day across 7 coins, you have up to ~672 opportunities per day to screen. You will
not fill most of them. You don't need to.

**Why this is the best fit for you:**

- No model required. No forecasting. No opinion about Bitcoin. It's arithmetic.
- No directional risk — the position is self-hedging by construction.
- Falsifiable in days: log the sums, count how often they're exploitable. You'll know within a
  week whether this exists.
- $1,000 is enough, because you're not taking risk, just capturing spread.

**The real risk is execution, not direction: leg risk.** If your UP fills and your DOWN doesn't,
you're suddenly holding a naked directional position you never wanted. Mitigations: quote both
sides simultaneously, size small, and have an automatic unwind if one leg sits unfilled past a
threshold. **This is the thing that would actually lose you money, and it's an engineering
problem — which is good news, because engineering is your strength.**

**Verdict: start here.**

---

## 5. Strategy C — Cross-asset lag

**Mechanism.** ETH, SOL, XRP and DOGE 15-minute markets all correlate strongly with BTC's path.
When BTC moves sharply, the alt markets should reprice within seconds. If the smaller, less liquid
alt books lag, there's a window to trade the alt market on information already visible in BTC.

**Assessment.** Plausible and in your domain. But this is a latency race, and latency races are won
by whoever has the best infrastructure — which will not be a student on residential internet
against a colocated firm. The edge, if any, lives in the sub-second range.

**Verdict:** interesting, but don't start here. Revisit if B works and you want to scale.

---

## 6. A word on crowding

There are commercial vendors selling historical order-book data and pre-built "KXBTC15M scalper"
bots for exactly these markets. That tells you two things:

1. Enough people are trading these that a data business is viable — so the obvious edges are hunted.
2. The strategies being sold are, by definition, not secret.

This isn't a reason to quit. It *is* a reason to expect edges to be small, to insist on measuring
before believing, and to be suspicious of any backtest — including your own — that shows large
returns.

---

## 7. Recommended sequence

1. **Resolve the settlement question** (§2). Free, ~20 lines of code, gates Strategy A entirely.
2. **Log UP+DOWN sums** across all coins for a week. Pure observation, zero capital. Count how
   often the sum falls below the maker threshold, and for how long it stays there.
3. **If the sums are exploitable** → build the two-legged maker bot in the demo environment. Focus
   engineering effort on leg risk, not on prediction.
4. **In parallel**, log price-at-T−7min versus settlement to test the averaging edge and the
   favorite-longshot question on the same recorded data. One recorder answers all three questions.
5. **Go/no-go** after ~2 weeks of data. Real money only if the arb sums are genuinely there.

Note that steps 1, 2 and 4 all use the **same** data collection. One recorder, three hypotheses,
zero capital at risk. That's the efficient path.

---

## 8. Honest bottom line

Of everything surveyed, **Strategy B (pair arbitrage) is the only one that doesn't require you to
predict anything.** That makes it the only one where your edge comes from engineering rather than
from out-forecasting professionals — and engineering is the thing you can actually be better at
than the person on the other side of the trade.

It's also the only one where being wrong doesn't cost you money, because a correctly-executed pair
trade has no losing outcome. The failure mode is "you never find a mispricing and you made $0,"
not "you lost your account."

That's the strategy that fits a $1,000 bankroll and a CS degree.

---

*Not financial advice. Every number here is a hypothesis until you've measured it yourself.*
