# Feasibility Assessment — Kalshi 15-Min Crypto Up/Down

*Deep caveat review before Phase 0. Every claim sourced or flagged as unverified.*

---

## VERDICT

**Engineering: doable.** Confirmed — no blockers found.

**Economics: unknown, but the hurdle is lower than I previously estimated.** One favourable
discovery (possible zero maker fees) and one confirmed mechanism (settlement averaging) improve the
odds. One unresolved question — whether the market already prices the effect — decides everything,
and it cannot be answered without collecting data.

**Revised probability of finding real edge: ~20–25%** (up from 10%). The upgrade is due to the fee
discovery below — **now CONFIRMED: resting orders on 15-min crypto cost $0.00.**

**⚠️ The binding constraint has moved.** With fees at zero, the question is no longer "can I clear
the hurdle" — it's "can I get filled without being adversely selected." Free maker orders mean
*everyone* wants to rest, queues are long, and you fill when the market runs through your price —
which is disproportionately when you're wrong. Fill quality is now the whole ballgame.

---

## GREEN — Confirmed favourable

### 1. Settlement averaging applies to 15-minute markets ✅

Kalshi's help center: *"All crypto market contracts are settled by averaging 60 seconds of CFB
Real-Time Indexes."* A specialist source explicitly debunks the vendor claim that 15-min markets
settle differently: *"There is no special '15-minute index.'"*

**Consequence:** the mechanism behind the entire strategy is real. Settlement has less variance
than the closing price, so favourites near expiry are worth more than a naive model says.

### 2. Resting orders may be completely free ✅ ⭐

Direct from the official fee schedule PDF (eff. Feb 5, 2026):

> *"Trading fees are only charged for orders that are immediately matched with orders sitting on the
> orderbook. Trading fees are **not** charged for orders placed that are not immediately matched and
> are instead left as resting orders on the orderbook **unless they are included in our 'Maker Fees'
> section.'"*

**This is the single most important finding in the whole review.** Breakeven at 91¢:

| Scenario | Breakeven | Edge needed |
|---|---|---|
| Crypto NOT on maker-fee list | **91.00%** | **0.00pt** |
| Crypto IS on maker-fee list | 91.15% | 0.15pt |
| Forced to cross the spread | 91.57% | 0.57pt |

If crypto isn't on the maker list, **any** positive edge is profitable. That changes the character
of the bet entirely — you're no longer trying to clear a hurdle, just to be right at all.

**⚠️ MUST VERIFY:** check <https://kalshi.com/fee-schedule> for the current maker-fee market list.
That page is JavaScript-rendered and I could not read it. **30-second manual check. Do this first.**

### 3. Rate limits are not a constraint ✅

Basic tier: 200 read tokens/sec, most endpoints cost 10 tokens → **~20 requests/sec**, with 2-second
burst capacity. You need to poll ~35 markets. Comfortable. WebSocket streaming is also available for
real-time order book updates, which is the right architecture anyway.

Note: 429 responses carry no `Retry-After` header — use exponential backoff with jitter. Live budget
at `GET /account/limits`.

### 4. Liquidity is not a constraint at your size ✅

Hundreds to thousands of contracts resting within 2¢ of mid mid-window. You're trading ~30
contracts. You are far below the size where impact matters.

### 5. No hidden costs ✅

No settlement fee. No membership fee. No ACH deposit or withdrawal fee. Confirmed in the official
schedule.

---

## YELLOW — Unresolved but cheaply checkable

### 6. Is the book open during the 60-second averaging window? ⚠️

Gates the ~10pt final-minute variant entirely. One source implies trading runs the full fifteen
minutes (*"traders price them for fifteen minutes, the window slams shut"*), which would mean yes —
but that's inference.

**Check:** watch one market close. Five minutes of observation.

### 7. Rounding tax on small orders ⚠️

Maker fee rounds **up** to the cent, so small orders pay proportionally more:

| Contracts | Raw fee | Billed | vs. theoretical |
|---|---|---|---|
| 11 | $0.0158 | $0.02 | **1.27x** |
| 33 (your size) | $0.0473 | $0.05 | **1.06x** |
| 550 | $0.7883 | $0.79 | 1.00x |

Kalshi reimburses rounding overpayment *only if it exceeds $10/month*. At ~$0.007 excess × 300
trades = $2.10/month, **you will never qualify.** You eat it permanently.

Mild argument for fewer, larger trades — but that conflicts with Kelly sizing, so accept it as a
small fixed drag. Irrelevant if crypto has no maker fee at all.

---

## RED — The real risks

### 8. Is the averaging effect already priced in? ⚠️⚠️⚠️ THE DECIDING QUESTION

**Evidence it IS priced:**
- The rule is in Kalshi's public help center. Market makers have read it.
- Kalshi crypto is among the highest-volume market families on the platform — heavy automated MM
  attention.
- The final minute is described as *"effectively a settlement auction. Price converges hard to
  either $0.99 or $0.01 unless BTC is right at the strike."* **That convergence is exactly what
  correct pricing of the averaging effect looks like.** A naive point-in-time model would not
  converge that fast.
- A 10-point systematic edge sitting unharvested in a flagship market is implausible.

**Evidence it might NOT be fully priced:**
- Favourite-longshot bias persists in many markets *despite* being well documented.
- Retail flow may anchor to spot price rather than the settlement index.
- The 3–6 minute effect is only 1–2.5pt — small enough to hide inside normal MM pricing noise, in a
  way the 8–10pt final-minute effect is not.

**My honest read: the big obvious edge in the final minute is almost certainly gone. The small,
subtle edge at 3–6 minutes is the one that might survive** — precisely because it's too small to be
worth a professional's attention but not too small to matter on a $1,000 account.

That happens to be exactly the window your strategy targets. Convenient, but unproven.

### 9. Adverse selection on resting orders ⚠️⚠️

Your bid fills preferentially when the market is moving against you. This is the standard reason
retail market-making fails, and it's not modellable in advance — it must be measured.

**Test:** after your resting bid at 91¢ fills, what fraction settle YES? If ~91%, fills are clean.
If materially below, you're being picked off and the maker advantage is an illusion.

### 10. A credible specialist looked at this market and didn't ship ⚠️

PredictionMarketsPicks — run by a former Wall Street bond trader, selling prediction-market edge
tools — explicitly has **no** 15-minute product, and says:

> *"The real enemy at this frequency is the round-trip cost... A model can be right on direction and
> still lose money after costs."*

Their read is that 15-min edge is a microstructure problem (realized vol + order flow), not a
valuation one. **Note the important asymmetry:** their objection is about *round-trip* cost. Your
strategy holds to settlement and rests orders — you never pay a round trip. Their critique may not
fully apply to your structure.

### 11. Correlation (already handled, don't undo it) ⚠️

Crypto is ~0.85 correlated on 15-min horizons. The one-position-at-a-time rule exists for this
reason. Running five simultaneous positions is one bet at 5x size — all five lose together roughly
once every 32 windows.

### 12. Thin overnight and weekend liquidity ⚠️

Crypto markets run 24/7 but liquidity thins overnight and on weekends. Wider spreads, worse fills.
Consider restricting to active hours, or at minimum log the time-of-day effect.

### 13. Taxes ⚠️

Kalshi gains are taxable and you'll receive a 1099. Keep the trade journal from day one — the
reconstruction is painful otherwise.

### 14. Psychological risk ⚠️

Buying 91¢ favourites means winning ~9 of 10 trades. That *feels* like a working system regardless
of whether it is. The temptation to increase size after a winning streak is the most likely way this
costs you real money, and it will arrive precisely when the strategy looks best.

The position cap exists to protect you from the version of yourself that has just won nine in a row.

---

## What changed vs. the previous plan

| | Before | After |
|---|---|---|
| Averaging applies to 15-min? | Unverified, contested | **Confirmed yes** |
| Maker fee hurdle | Assumed 0.14pt | **Possibly 0.00pt** |
| Combo variant | Under consideration | **Dropped** (dead + you deprioritised it) |
| Rate limits | Unknown | **Confirmed fine** |
| Probability of real edge | ~10% | **~20%** |

---

## Do these three things before writing any code

1. **Check <https://kalshi.com/fee-schedule>** — is crypto on the maker-fee list? Determines whether
   your hurdle is 0.00pt or 0.15pt. *30 seconds.*
2. **Watch one 15-min market close** — is the order book live at T−30s? Gates the big variant.
   *5 minutes.*
3. **Open a live market and record the spread** at 6, 5, 4, 3 minutes remaining. If the spread is
   consistently 2¢+ at your entry window, resting orders may rarely fill and the whole thing is
   harder than modelled. *15 minutes.*

All three are manual, free, and gate the code. **Then** Phase 0.

---

## Bottom line

The plan is **doable to build** and **cheap to test**. The engineering is confirmed feasible, the
mechanism is confirmed real, and the fee structure may be more favourable than assumed.

What remains genuinely unknown is whether Kalshi's market makers have already priced the effect. My
estimate is that they have priced the large final-minute version and *may* have left crumbs in the
3–6 minute window. Crumbs are enough at your account size — and they're also entirely consistent
with there being nothing at all.

The measurement is the only way through. It costs two weeks and zero dollars.

---

*Not financial advice.*

**Sources:** [Kalshi Fee Schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf) ·
[Crypto Markets — Kalshi Help Center](https://help.kalshi.com/en/articles/13823838-crypto-markets) ·
[Rate Limits and Tiers — Kalshi API Docs](https://docs.kalshi.com/getting_started/rate_limits) ·
[KXBTC15M: How They Settle](https://predictionmarketspicks.com/articles/kalshi-bitcoin-15-minute-markets) ·
[Kalshi BTC 15-Minute Markets](https://kalshibacktest.com/resources/kalshi-btc-15-minute-markets)
