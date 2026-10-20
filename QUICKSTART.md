# Quickstart — What To Do Now

Total setup time: ~20 minutes. Then it runs unattended for two weeks.

**Nothing here risks money.** The recorder only reads. It cannot place an order.

---

## Step 1 — Get API keys (5 min)

1. Log into Kalshi → Account → **API Keys** → create a key
2. **Save the private key immediately.** Kalshi does not store it and you cannot
   retrieve it again once the page closes. Save as `kalshi_key.pem`.
3. Copy the Key ID too.

---

## Step 2 — Set up (5 min)

Requires **Python 3.10+** (the code uses `X | None` type syntax).

```bash
pip install -r requirements.txt

export KALSHI_KEY_ID="your-key-id-here"
export KALSHI_PRIVATE_KEY_PATH="./kalshi_key.pem"
export KALSHI_ENV="prod"
```

`prod` is correct and safe here — the recorder is read-only, and demo has different
(often thinner) books that would corrupt your measurement.

Verify it works:

```bash
python kalshi_client.py
```

You should see your balance and a list of markets. If you get a 401, the key ID and
PEM file don't match.

---

## Step 3 — Find the real series tickers (2 min)

```bash
python recorder.py --discover
```

This lists every crypto series with open markets and shows each one's window length.
**Find the ones with a ~15 min window**, then open `recorder.py` and put those tickers
into `CANDIDATE_SERIES` at the top.

Don't skip this. My guesses (`KXBTC15M`, `KXETH15M`, …) are guesses — Kalshi has
renamed series before.

---

## Step 4 — Start recording (1 min, then leave it)

```bash
python recorder.py --run
```

Leave it running. To survive closing your terminal:

```bash
nohup python recorder.py --run > recorder.log 2>&1 &
```

It polls slowly most of the time and speeds up to 1/second near each close, so it
stays well inside the rate limit (~20 req/s available, this uses a fraction).

---

## Step 5 — Check in (any time)

```bash
python analyze.py
```

Safe to run while the recorder is going. Early on it will say "not enough data" —
that's correct behaviour, not a failure.

**Do not act on early results.** You need ~2,500 observations to resolve a 1-point
edge. The analyzer reports its own statistical power so you can see exactly how far
along you are.

| Edge size | Observations needed | Roughly |
|---|---|---|
| 1.0pt | 2,501 | 10–17 days |
| 2.0pt | 625 | 3–4 days |
| 3.0pt | 278 | 1–2 days |

---

## Step 6 — While it runs: three manual checks

These cost nothing and sharpen the analysis.

**A. Watch one window close.** Note the exact second the book stops accepting orders.
You've already observed it halts early — pin down whether it's consistent.

**B. Watch the spread at 6, 5, 4, 3 minutes** on a BTC window. If it's consistently
2¢+, resting orders will rarely fill at a useful price. Compare to what `analyze.py`
reports in Q4.

**C. Confirm the fee finding.** On <https://kalshi.com/fee-schedule> → Predictions
tab, type "BTC" in the search box on the non-standard fees table. Confirm your
15-minute series is **absent** (meaning: standard fees, zero maker fee).

---

## Step 7 — Read the verdict (~2 weeks)

`analyze.py` will say one of three things:

- **EDGE FOUND** — mean edge positive, CI excludes zero. Move to paper trading in the
  demo environment. Do not skip straight to live.
- **NEGATIVE EDGE** — favourites are overpriced. Kill it. You spent two weeks and $0
  and learned something true.
- **INCONCLUSIVE** — the most likely outcome. Markets are efficient here. Either
  collect more data or stop.

---

## What you'll know at the end

Regardless of the verdict:

1. Whether Kalshi's 15-min crypto books are efficient in the 90–96¢ band
2. The real spread structure across the window
3. Whether your strike observation was right (Q3)
4. When exactly the book closes (Q2)
5. How to build a data pipeline, test a hypothesis, and not fool yourself

Point 5 is the one that keeps paying regardless of what the other four say.

---

## Reminders

- **Never trade money you need.** The risk controls assume you can be patient.
- **Do not skip to live trading** because early numbers look good. At n=200 you are
  looking at noise.
- **The most likely outcome is no edge.** That is a successful experiment, not a
  failed one.
