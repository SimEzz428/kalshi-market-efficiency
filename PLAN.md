# Kalshi Trading Plan — Sub-$1k Bankroll, Sports + Crypto

**Read this section first.** With under $1,000, the expected outcome of this project is
*learning*, not income. Even a genuinely good strategy returning 15%/year on $500 makes
$75. The realistic goal for the next 6 months is: build a system, prove you have edge on
paper, and only then decide whether it's worth scaling. If you treat the $1k as tuition
rather than capital, every decision downstream gets easier.

---

## 1. The hurdle you must clear

Kalshi's taker fee is:

```
fee_per_contract = 0.07 × P × (1 − P)     (P = price in dollars, rounded up to the cent on the total)
```

| Price | Fee/contract | As % of the $1 payout |
|-------|--------------|------------------------|
| 50¢   | 1.75¢        | 1.75%                  |
| 30¢   | 1.47¢        | 1.47%                  |
| 10¢   | 0.63¢        | 0.63%                  |
| 5¢    | 0.33¢        | 0.33%                  |

Maker fees are 25% of taker (and 0% on many standard markets). **This is the single most
important lever in the whole plan** — a resting limit order at 50¢ costs 0.44¢ or less
instead of 1.75¢. Roughly 4x cheaper.

Your breakeven edge, as a probability, is:

```
min_edge = fee + half_spread    (+ fee again if you plan to exit early rather than hold to settlement)
```

At 50¢ with a 2¢ spread, crossing the spread: you need your true probability to be **~2.75
percentage points** better than the market. Held to settlement as a maker on a 1¢ spread:
**~0.94 points.**

**Rule #1: be a maker, and hold to settlement.** This isn't a preference, it's the
difference between a viable strategy and a dead one.

---

## 2. Honest read on your two domains

### Crypto (BTC/ETH strike + range markets)

**Why it's the better starting point:**
- Free, high-quality, continuous data (Coinbase/Binance public candle APIs, no key needed)
- The fair value is *computable*. P(BTC > K at 5pm) is a math problem, not an opinion.
- Markets run hourly/daily → hundreds of independent observations per month, so you can
  actually reach statistical significance on whether you have edge. Sports gives you far
  fewer clean samples.
- You can backtest it fully offline with historical candles. No Kalshi history needed for
  the model itself.

**Where the edge plausibly lives:**
- **Fat tails.** A Black-Scholes/GBM baseline assumes lognormal returns. Crypto's actual
  short-horizon return distribution has much fatter tails. Far-from-money strikes are
  systematically *underpriced* by naive models. If the market is anchored to a naive model,
  the 3–8¢ contracts are where mispricing concentrates — and conveniently, that's also
  where fees are lowest.
- **Vol regime lag.** Realized vol over the last 30 minutes is a better predictor of the
  next hour than the trailing 24h. If Kalshi's pricing is slow to adjust after a vol
  spike, near-money contracts misprice.
- **Late-window pinning.** In the final minutes before settlement, probabilities collapse
  toward 0/1 deterministically. Slow order books can lag.

**What kills it:** Kalshi's crypto books are quoted by real market makers running the same
models. Assume the near-money is efficient. Your realistic hunting ground is the tails and
the illiquid strikes — which means small size and patience.

### Sports

**Why it's harder:** you're competing against sportsbook prices that already aggregate
enormous amounts of sharp money. You will not beat Pinnacle with a model you build as a
side project.

**The one credible angle:** *cross-venue relative value.* Take sharp sportsbook lines,
remove the vig to get a fair probability, and compare to Kalshi. Kalshi's sports books
are newer and thinner, so they drift from consensus more than a book does.

De-vigging two-way lines (multiplicative method):
```
p_fair_A = (1/odds_A) / (1/odds_A + 1/odds_B)
```
Better: use the *power* or *Shin* method, which handle favorite-longshot bias more
accurately than multiplicative. The included code has both.

**What kills it:** odds APIs cost money (The Odds API has a free tier ~500 calls/month —
enough to start), the discrepancies close fast, and liquidity on Kalshi sports may not
let you fill meaningful size. Treat sports as **phase 2**, not phase 1.

---

## 3. Phased plan

### Phase 0 — Infrastructure (week 1)
- [ ] Kalshi account + **demo environment** API keys (`demo-api.kalshi.co`)
- [ ] Get `kalshi_client.py` authenticating and pulling live markets
- [ ] Build a SQLite recorder that snapshots the orderbook for ~20 crypto markets every 60s
- [ ] **Let it run for 2–3 weeks before you do anything else.** You cannot backtest Kalshi
      without your own data — historical order books are not readily available. Start
      collecting on day one; this is the highest-value thing you'll do all month.

### Phase 1 — Crypto research (weeks 2–6)
- [ ] Pull 1-minute BTC/ETH candles from Coinbase (free, ~5 years available)
- [ ] Build the fair-value model (`crypto_scanner.py` has the GBM baseline)
- [ ] **Then improve it**: replace GBM with an empirical distribution of historical
      h-minute returns conditioned on current realized vol. This is the actual research —
      the GBM version is a straw man to beat.
- [ ] Calibration check: bucket your model's predictions into deciles. When you say 20%,
      does it happen ~20% of the time? **If your model isn't calibrated, nothing else
      matters.** Score with Brier score and a reliability plot.
- [ ] Once you have 2+ weeks of recorded Kalshi books, measure: does `model_prob − mid`
      predict settlement? Regress outcome on the divergence.

### Phase 2 — Paper trading (weeks 6–12)
- [ ] Run the scanner live against the demo environment, logging every hypothetical trade
      with model prob, market price, spread, fee, and outcome
- [ ] Target **150+ paper trades minimum** before drawing conclusions
- [ ] Assume you only get maker fills, and be pessimistic — assume you get filled *only
      when you're wrong* (adverse selection). If it survives that assumption, it's real.

### Phase 3 — Go/no-go (week 12)

Only move to real money if **all** of these hold:
1. Brier score beats the market's own price as a forecast (a strict test — most models fail)
2. Realized paper edge > 2× the fee hurdle, after pessimistic fill assumptions
3. 150+ trades, and the equity curve isn't driven by 3 lucky outliers
4. You can explain *why* the edge exists in one sentence. No explanation = overfit.

If it fails, you've built a real quant research pipeline and learned a lot. That's a fine
outcome — do not talk yourself into trading a strategy that failed its own test.

### Phase 4 — Live, tiny (month 4+)
- [ ] Start at **$25 max per position**, quarter-Kelly
- [ ] Limit orders only (maker fees), hold to settlement
- [ ] Hard stop: if you're down 25% of bankroll, halt and re-examine
- [ ] Journal every trade. Compare live results to paper — divergence means your fill
      assumptions were wrong

---

## 4. Position sizing

Fractional Kelly for a binary contract bought at price `P` with true probability `p`:

```
edge = p − P
kelly_fraction = edge / (1 − P)        # full Kelly
bet = bankroll × kelly_fraction × 0.25 # use QUARTER Kelly
```

Full Kelly is far too aggressive when `p` comes from a model you built yourself — your
probability estimates have error, and Kelly is extremely sensitive to overestimating edge.
Quarter Kelly gives up ~25% of growth for a large reduction in drawdown risk. With <$1k,
also cap any single position at 5% of bankroll regardless of what Kelly says.

---

## 5. Risks worth naming plainly

- **Overfitting.** With enough parameters you will find edge in noise every time. The
  calibration test and out-of-sample holdout exist specifically to catch this.
- **Adverse selection.** Your maker orders fill preferentially when someone knows something
  you don't. This is the #1 reason paper results beat live results.
- **Liquidity.** Backtests assume fills. Thin books don't provide them.
- **Taxes.** Kalshi contract gains are taxable; you'll get a 1099. Keep the trade journal.
- **Tilt.** The most common failure mode isn't a bad model, it's abandoning position limits
  after a losing streak.

---

## 6. Files in this folder

| File | Purpose |
|------|---------|
| `kalshi_client.py` | RSA-PSS authenticated API client (demo + prod) |
| `edge.py` | Fee model, breakeven edge, de-vigging, Kelly sizing |
| `crypto_scanner.py` | Realized-vol fair value for BTC/ETH markets, edge screen |
| `recorder.py` | SQLite orderbook snapshotter — **start this first** |
| `requirements.txt` | Dependencies |

Setup:
```bash
pip install -r requirements.txt
export KALSHI_KEY_ID="your-key-id"
export KALSHI_PRIVATE_KEY_PATH="./kalshi_key.pem"
export KALSHI_ENV="demo"        # keep this on 'demo' until Phase 4
python crypto_scanner.py
```

---

*This is educational tooling, not financial advice. Most retail algorithmic traders lose
money. Nothing here should be run with money you can't afford to lose entirely.*
