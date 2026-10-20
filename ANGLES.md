# Every Angle — A Strategic Assessment

Research before action. Twenty-three angles, scored honestly.

---

## THE FACTS THAT CONSTRAIN EVERYTHING

**1. 90% of Kalshi's $43B volume is sports.**
That single number tells you where attention is. Sports = deepest liquidity AND
most competition. Everything non-sports is comparatively neglected — thinner
books, worse pricing, but harder to trade size in.

**2. The leaderboard is opt-in.**
It ranks volume, profit, and accuracy — but only among people who chose to be
displayed. That is survivorship bias layered on self-selection. You're seeing
winners who wanted to be seen. The distribution of everyone who lost is invisible.

**3. Ranked by profit, not return.**
Someone with $500k up 8% beats you at $1,000 up 300%. **Matching leaderboard
positions is a capital problem, not a skill problem.** Matching their *process*
is achievable. Matching their dollar profits is not, and chasing it is how people
oversize and blow up.

**4. Your measured results so far.**
Crypto 15-min: no edge, four statistical artefacts caught and killed. That is a
real data point about how efficient the fast, liquid corners are.

---

# CATEGORY A — STRUCTURAL / MECHANICAL
*No forecasting required. Either the numbers violate logic or they don't.*

### A1. Monotonicity arbitrage ✅ BUILT
Strike ladders must satisfy P(>60k) ≥ P(>62k). Violations are riskless.
- **Edge if found:** certain, unambiguous
- **Competition:** extreme — first thing anyone automates
- **Verdict: RUNNING.** Costs nothing. Expect near-zero hits.

### A2. Bucket-sum arbitrage ✅ BUILT
Exhaustive partitions must sum to 100.
- **Risk:** my partition detection is heuristic. Cumulative ladders are NOT
  partitions and will false-positive.
- **Verdict: RUNNING, verify manually before acting.**

### A3. YES/NO cross ✅ BUILT
`yes_bid + no_bid > 100` means free money.
- **Verdict: RUNNING.** Vanishingly rare.

### A4. Cross-venue: Kalshi vs Polymarket ⭐ WORTH BUILDING
Same event, two exchanges, different crowds. Polymarket is crypto-native and
international; Kalshi is US retail. Genuinely different pricing populations.
- **Edge:** plausible and structural, not statistical
- **Cost:** capital on both venues, crypto rails for Polymarket, execution risk
- **Competition:** moderate — needs two accounts and infrastructure, which filters
  out most retail
- **Verdict: HIGH PRIORITY.** Best structural angle not yet built.

### A5. Cross-venue: Kalshi vs sportsbooks ⭐⭐ HIGHEST PRIORITY
De-vig Pinnacle (sharpest book in the world) to get fair value, compare to Kalshi.
- **Why this beats modelling:** you don't need to be better than the betting
  industry. You use its output as truth and trade the venue that hasn't caught up.
- **Why it's plausible now:** 90% of Kalshi volume is sports, but that's
  concentrated in headline events. Lower-tier matches are thin and slow.
- **Cost:** odds data (The Odds API free tier ≈ 500 calls/month — enough to test)
- **Competition:** real, gaps close in seconds to minutes
- **Verdict: BUILD THIS NEXT.** De-vig code already exists in `edge.py`.

### A6. Settlement-rule quirks ✅ TWO ALREADY FOUND
The 60-second averaging; strike = previous settlement.
- **Track record:** two real mechanisms found in one day of reading, both of which
  published sources got *wrong*
- **Verdict: KEEP AUDITING.** Highest hit-rate research per hour spent. Read
  rulebooks for categories you might trade.

### A7. Fee-structure arbitrage
S&P 500 and Nasdaq markets carry a **half** fee multiplier (0.035 vs 0.07).
- **Verdict: PARK.** Not an edge alone, but if you ever find a strategy that works
  on index markets it costs half as much to run.

### A8. Cross-frequency consistency
Kalshi lists 15-min, hourly, daily, weekly BTC markets. They imply volatilities
that must be mutually consistent. If the daily implies 40% vol and the hourly
implies 70%, one is wrong.
- **Edge:** real if it appears; it's arithmetic, not opinion
- **Competition:** low — requires actually doing the vol maths
- **Verdict: WORTH BUILDING.** You already record several frequencies.

### A9. Tick-size exploitation
Sub-10¢ prices tick at 0.1¢; above at 1¢. The two sides of one contract have
different resolution.
- **Verdict: PARK.** Interesting mechanism, no obvious trade.

---

# CATEGORY B — INFORMATIONAL
*The only category where capital doesn't decide the winner.*

### B1. Niche sports domain edge ⭐ YOUR STATED STRENGTH
Tennis, European football.
- **Where it lives:** NOT Grand Slam finals. ATP 250 first rounds, Challengers,
  qualifying. Markets priced by people who've never seen either player.
- **Data:** Jeff Sackmann's `tennis_atp` on GitHub — free, every ATP match since
  1968, with surface and tournament tier
- **Known inefficiencies:** surface specialisation underweighted, fatigue/travel
  underweighted, overreaction to recent results
- **Honest caveat:** if Kalshi mirrors sportsbook prices, your model must beat the
  industry. It won't. **Combine with A5 instead** — use sharp books as truth.
- **Verdict: PURSUE, but as a cross-venue play, not a modelling play.**

### B2. Non-English information ⭐ GENUINELY YOURS
Arabic and French sports/news sources that US retail doesn't read.
- **Edge:** structural and unreplicable by competitors who don't speak the languages
- **Verdict: PURSUE.** This is the single most defensible advantage you have, and
  it costs nothing but attention.

### B3. Weather vs published models
NOAA/NWS forecasts are free. Kalshi settles on official station observations.
- **Edge:** documented gaps between model output and market pricing
- **Against you:** US-only, so no home advantage; and it's the most-cited "retail
  edge" so others are looking
- **Verdict: SECONDARY.** Real, but you have no informational advantage here.

### B4. Fast econ-release parsing
Parse BLS/BEA faster than the market reprices.
- **Verdict: DROP.** You'd be racing colocated firms on residential internet.

### B5. On-chain crypto data
Exchange flows, whale movements, protocol events.
- **Verdict: PARK.** Real signal exists but the horizon (hours-days) doesn't match
  Kalshi's crypto contracts (minutes), and you've measured those to be efficient.

---

# CATEGORY C — STATISTICAL / BEHAVIOURAL

### C1. Favourite-longshot bias ✅ TESTED, no signal
Q5's calibration curve is flat where it has data.
- **Verdict: MEASURED. Keep the sweep running across categories.**

### C2. Calibration by category ✅ RUNNING (`scan_all.py`)
- **Verdict: RUNNING.** Slow — needs settlements to accumulate. Weeks, not days.

### C3. New-market launches ⭐ UNDER-EXPLORED
Kalshi ships new series constantly. There's a window before makers calibrate.
- **Edge:** plausible and repeatable — it recurs every time they launch something
- **Competition:** low, because it requires *watching* rather than modelling
- **Verdict: WORTH BUILDING.** A launch monitor is ~30 lines against the series
  endpoint. Genuinely novel angle.

### C4. Time-of-day and weekend effects
Overnight/weekend liquidity is thinner. Is pricing worse?
- **Verdict: FREE — already in your data.** Just a query.

### C5. Momentum / reversal in prediction markets
Do prices overreact within a window?
- **Verdict: TESTABLE with existing data.** No new collection needed.

---

# CATEGORY D — MICROSTRUCTURE

### D1. Order-book imbalance ⭐ DATA ALREADY COLLECTING
You now record full depth. Imbalance is a classic short-horizon signal.
- **Edge:** works at exactly the horizons where valuation models don't
- **Verdict: WORTH TESTING.** You have the data and haven't looked.

### D2. Adverse-selection avoidance ✅ PARTIALLY BUILT
Q7 distinguishes real quotes from stale stubs.
- **Verdict: KEEP.** Not a strategy — a filter that makes other strategies honest.

### D3. Market making
- **Verdict: DROP.** Capital-intensive, adverse selection will eat $1,000.

### D4. Latency / cross-asset lead-lag
BTC moves, alts follow.
- **Verdict: DROP.** Infrastructure race you lose by construction.

---

# CATEGORY E — NON-TRADING

### E1. Tooling for other traders
Vendors already sell Kalshi backtest data and bots — the demand is proven.
- **Verdict: PARK,** but note your pipeline is already better than some of what's
  being sold.

### E2. The career path
- **Verdict: HIGHEST EXPECTED VALUE OF ANYTHING ON THIS LIST**, and it's a
  byproduct of work you're already doing.

---

# RANKED SHORTLIST

| # | Angle | Why | Effort |
|---|---|---|---|
| **1** | **A5 — Kalshi vs sharp sportsbooks** | Don't need to beat the industry, just use it as truth. De-vig code exists. | Medium |
| **2** | **B2 — non-English information** | Structurally yours. Nobody can copy it. | Ongoing |
| **3** | **C3 — new-market launch monitor** | Under-explored, repeatable, ~30 lines | Low |
| **4** | **A8 — cross-frequency vol consistency** | Arithmetic, not opinion. Data already collected. | Low |
| **5** | **D1 — depth imbalance** | Data already collected, never examined | Low |
| **6** | **A4 — Kalshi vs Polymarket** | Genuinely different crowds | High |

Items 3, 4 and 5 are cheap and use data you already have. Items 1 and 2 are where
a durable edge would actually come from.

---

# WHAT I'D DROP ENTIRELY

- Racing on latency (D4, B4) — infrastructure decides, you lose
- Market making (D3) — capital decides, you lose
- Beating sportsbook models with your own (B1 alone) — the industry decides, you lose
- Chasing leaderboard *rank* — capital decides, you lose

**The pattern: never compete where the winner is determined by an input you don't
have.** Compete on information and process, which are the only two inputs not
gated by capital.

---

# THE HONEST FRAME ON "BEATING THE LEADERBOARD"

You cannot out-earn a funded professional in dollars with $1,000. The leaderboard
ranks profit, so that race is decided before it starts.

What is genuinely achievable:

1. **Match their process** — you already have measurement discipline most retail
   traders never develop
2. **Beat them on percentage returns** in a niche too small for them to bother with
3. **Beat them on honesty** — you've killed four false signals in three days;
   plenty of people on that leaderboard are one variance run from giving it back

**The realistic ceiling on $1,000 is tens to low hundreds of dollars a month, and
only if an edge exists.** Everything above is about finding out whether one does,
cheaply, without lying to yourself.

The uncomfortable arithmetic: the same skill applied at a firm, with their capital,
pays 100x more than applying it to your own $1,000. That's not a consolation
prize — it's the actual highest-value use of everything you're building.

---

*Nothing here is financial advice. Every angle above is a hypothesis until measured.*
