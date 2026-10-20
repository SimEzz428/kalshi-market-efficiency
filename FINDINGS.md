# Findings ledger

**Oct 17, 2026.** Every number here came off a real run, with where it prints.
This is the source the README gets assembled from at the end — not the README.

Rule for this file: **a claim with no reproduction path does not belong in it.**
Section 6 holds the ones that currently fail that test.

---

# 12 — The C++ recorder: what is tested, what is not, and why it is split in two

The Python recorder works. Rewriting the hot path in C++ was not about making
it faster — at a one-second poll interval nothing in Python is the bottleneck.
It was about owning the layer underneath: signing a request by hand, holding a
connection open, writing to SQLite through a prepared statement, and being able
to say what the p99 latency was rather than guessing.

## 12.1 The split is the point

`book.hpp` / `book.cpp` is a **pure core**: no network, no database, no
cryptography, no clock. Everything in it is a function of its arguments.
`recorder.cpp` is an **I/O shell**: libcurl, OpenSSL, SQLite, JSON parsing, the
poll loop.

That boundary was forced by the environment — the machine this was written on
has `g++` but none of the four development headers, so half the program could
be compiled and half could not. It is the right boundary anyway, and the reason
is visible in the test file: **44 assertions run with no libraries installed and
no network access**, because everything worth getting wrong was moved to the
side that does not need them.

| | core | shell |
|---|---|---|
| lines | ~150 | ~420 |
| external dependencies | none | libcurl, OpenSSL, SQLite3, nlohmann/json |
| compiled and run | yes, 44 checks, clean under ASan + UBSan | type-checked only |

## 12.2 The one domain rule that must not be wrong

Kalshi's orderbook endpoint returns two arrays, `yes` and `no`. They are not a
bid side and an ask side. Both are **bids** — resting buy orders, one on each
contract.

A YES contract and a NO contract on the same market sum to 100 cents, so
somebody bidding 55 cents for NO is, in YES terms, offering to sell YES at 45.
The ask side of the YES book has to be *derived*:

```
yes_ask_price = 100 - no_bid_price
```

Get this wrong in either direction and nothing crashes. The database fills
normally, every row looks plausible, and the spread is systematically wrong —
in the sample used in the tests, 11 cents instead of 1. Every downstream
efficiency measurement would then be computed on a book that never existed.

This is why `assemble()` lives in the core rather than next to the JSON parser.
Six of the 44 assertions exist only to pin this rule down, including the two
boundary cases: a NO bid at 0 inverts to a YES ask at 100 (valid, kept) and a
NO price above 100 would invert to a negative price (invalid, dropped rather
than stored).

## 12.3 Decisions in the core that look arbitrary

- **`Level::size` is `double`, not `int`.** Contract quantities are integers
  today. The field is a double so that a fractional size cannot silently
  truncate to zero if that ever changes — a zero size reads as "no liquidity
  here", which is a different and much worse claim than "0.4 contracts".
- **`best_bid` scans rather than reading element 0.** The API returns sorted
  arrays. Trusting that means one upstream ordering change produces a book
  whose "best bid" is the worst one, silently. The scan costs nothing at ten
  levels and removes the assumption. `sort_sides()` exists for the same reason
  and is applied by `assemble()`, not left to the caller.
- **Every accessor returns `std::optional`.** An empty book is normal — thin
  markets have no resting orders for minutes at a time. A `Level{}` default
  would record a bid of 0 cents, which is a real price and a lie. `nullopt`
  becomes SQL `NULL`, and analysis can tell "no quote" from "quote at zero".
- **`depth_within` rejects a negative window** instead of returning the whole
  side, and treats the window as inclusive on both edges. The test pins the
  inclusive boundary explicitly, because a window of 3 that silently means 2 is
  the kind of error that shifts a liquidity number by a few percent and is
  never noticed.
- **`crossed()` is recorded, not corrected.** A crossed book is either a real
  arbitrage or a stale snapshot, and which one it is cannot be decided at
  capture time. It is stored as a flag so the question survives to analysis.

## 12.4 Backoff: the jitter is not decoration

`Backoff` doubles the delay per failure up to a 30-second cap, then multiplies
by a random factor in [0.5, 1.0] drawn from a seeded LCG. The seed comes from
the clock at startup.

Without jitter, every client that was polling when an outage began retries at
the same instant, and keeps retrying in lockstep — the exchange comes back up
and is immediately hit by a synchronised wall of requests from every recorder
that was running. The test asserts that eight differently-seeded instances do
not produce the same first delay, which is the property that actually matters.

The cap is checked by brute force: 40 further attempts with no reset, asserting
every delay stays in `[1, 30000]`. Doubling a `long` 40 times overflows; the
loop in `next_delay_ms()` stops shifting once it reaches the cap, and the test
is there to catch anyone who "simplifies" that into `base << attempt`.

## 12.5 Latency: p99, and why the window is cleared

`Latency` stores raw samples and sorts on demand rather than keeping a running
average. A mean request time is close to useless for a recorder — the mean is
dominated by the thousands of fast requests, and the thing that loses data is
the slow tail. The report prints p50, p95, p99 and max.

The window is **cleared after every report**, so each line describes the last
interval rather than the whole run. A p99 computed over six hours tells you
nothing about whether the last ten minutes were bad.

Two measurements are stored per request, not one: wall-clock time around
`curl_easy_perform`, and libcurl's own time-to-first-byte. The gap between them
separates "the exchange was slow to answer" from "the response was large or the
transfer stalled". A single number cannot distinguish those.

## 12.6 The storage layer, and the bug class it guards against

Writes go through one prepared statement, inside explicit transactions
committed every 200 rows, with `journal_mode=WAL` and
`synchronous=NORMAL`. WAL is what lets an analysis query read the database
while the recorder is still writing to it — with the default rollback journal,
a reader blocks the writer, and at a one-second poll interval that means
dropped snapshots.

The insert binds 19 values to 19 columns by incrementing an index. That is a
pattern where inserting one column in the middle and forgetting one bind call
shifts every subsequent value by one position — and SQLite will accept it,
because a price bound into the size column is still a number. There is a
`COLUMNS` constant and a runtime check that the final index matches it, which
turns a silent corruption of the entire dataset into a loud failure on the
first row.

The first draft had the optional columns bound inside `if (bid) { ... } else
{ ... }` branches, which made the bind count impossible to confirm by reading:
26 call sites for 19 columns. The branches were replaced with three
`bind_opt_*` helpers taking `std::optional`, so the sequence is straight-line
and the count is checkable by eye and by the guard.

Raw JSON storage is **off by default** (`KALSHI_KEEP_RAW=1` turns it on). The
derived columns are what analysis reads; keeping the raw body as well roughly
triples the database size, and is worth it only while the parser is still
under suspicion.

## 12.7 Secrets

The key ID and the private-key path are read from the environment, never from
`argv`. Anything on a command line is visible in `ps` to every user on the
machine and is written to shell history. `env_or_throw` says this in its error
message so the next person does not "fix" it by adding a flag.

Nothing in the process ever prints a header, a signature, or any part of the
key. The error path logs the HTTP status and at most 160 characters of the
response body, which is the exchange's own text.

## 12.8 ⚠️ What has not been verified

Stated plainly, because the difference between the two halves of this program
is large:

1. **The core is tested.** 44 assertions, run, zero failures, clean under
   `-Wall -Wextra -Wpedantic` and again under AddressSanitizer and
   UndefinedBehaviorSanitizer.
2. **The shell is type-checked only.** `recorder.cpp` compiles to an object
   file, but against **stub headers** carrying the library signatures — not
   against libcurl, OpenSSL, SQLite or nlohmann/json, none of which are
   installed here. That proves the code is internally consistent and
   well-typed. It proves nothing about whether the libraries behave as
   assumed.
3. **The signing message is the single highest-risk line.**
   `signing_message()` builds `timestamp_ms + method + path`. If that
   concatenation, the timestamp unit, or the exact path string differs from
   what the API expects, every request returns 401 and nothing else in the
   program matters. The working Python recorder already does this correctly:
   **diff `signing_message()` against it before the first run**, rather than
   trusting the version written from the documentation.
4. **Untested end to end:** PSS salt length, the base64 variant, connection
   reuse across requests, the WAL interaction under a concurrent reader, and
   whether `depth=10` is the right query parameter.

The first real run is therefore a test, not a deployment: one ticker, raw
storage on, and a check that the recorded best bid and ask match what the
Python recorder writes for the same market at the same minute.

---

# 17 — Concurrency: the capture loop must not wait for the disk

## 17.1 The problem the thread solves

The single-threaded recorder did this per ticker per cycle: sign, request, parse,
**write to SQLite**, sleep. The write is in the capture path, so every
`sqlite3_step` and every 200-row `COMMIT` is time the recorder is not polling.
At a one-second interval that is survivable. At 100ms across several tickers it
is not, and the thing that degrades is the measurement itself — intervals
stretch, and the timestamps stop meaning what the analysis assumes they mean.

So capture and persistence are now separate threads with a
`BoundedQueue<Snapshot>` between them. The HTTP loop parses and pushes; the
writer thread pops and writes.

## 17.2 ⚠️ The design decision: drop the oldest, never block

A bounded queue has to do something when it is full, and the choice is the whole
argument.

**Blocking the producer is wrong here.** If the disk stalls, a blocking queue
stops the capture loop, and the recorder stops observing the market for exactly
as long as the disk is slow. The data you lose is not the data in the queue — it
is every snapshot you never took, and you cannot tell afterwards that you missed
them.

**Dropping the newest is also wrong.** The newest snapshot is the one closest to
now, and a gap at the end of a series is worse than a gap in the middle.

So `push()` **drops the oldest item, counts it, and returns false**. The capture
loop never waits. A gap appears in the recorded series, the drop counter says
exactly how many, and the periodic report prints queue depth alongside it. A
recorder that loses 40 snapshots and says so is usable; one that silently
stretches its own sampling interval is not.

`queue.dropped() > 0` at shutdown prints an explicit line saying the writer
could not keep up, because a counter nobody reads is not instrumentation.

## 17.3 Shutdown has to drain, not abandon

`close()` sets a flag and `notify_all()`s. `pop()` returns `false` only when the
queue is **closed and empty**, so everything already queued is still written.
`main` closes the queue then `join()`s the writer, and the writer flushes the
open transaction on its way out.

The order matters. Joining before closing deadlocks — the writer is blocked in
`pop()` waiting for work that will never come. Closing without joining loses
whatever was still in the queue plus the uncommitted transaction. Two tests pin
both halves: one asserts `close()` wakes a blocked consumer, one asserts items
queued before `close()` are still drained after it.

## 17.4 Why the tests are what they are

18 of the 63 checks cover the queue. The one that matters most:

```
4 producers x 2000 items, 1 consumer, capacity 64
assert consumed + dropped == pushed
```

That is a conservation law. It fails if an item is lost, if one is delivered
twice, or if the drop counter and the queue disagree — and it exercises the
contended path, where capacity 64 against 8000 items guarantees the queue is
full most of the time.

The ordering test is subtler than it looks. After filling capacity 4 with 1-4
and pushing 5, the test asserts the front is **2**, not 1 and not 5. That single
assertion pins the whole eviction policy: it would fail if the newest were
dropped, if the push were refused, or if eviction took from the wrong end.

## 17.5 Verified under three sanitizers

`make test`, `make san` (AddressSanitizer + UndefinedBehaviorSanitizer) and
`make tsan` (ThreadSanitizer) all report **63 checks, 0 failures**, and
ThreadSanitizer reports **zero data races**.

TSan is the one that counts for this file. ASan finds memory errors and would
not notice an unsynchronised read of `dropped_` or a missing `notify_all` —
`dropped()` and `size()` take the mutex for exactly that reason, which is why
`m_` is `mutable`. A lock-free version would be faster and would need a far
stronger argument than "the disk is slow sometimes".

**Still not verified:** `recorder.cpp` type-checks against stub headers only.
The writer thread has never run against a real SQLite file, so queue depth under
real write latency is unmeasured, and `QUEUE_CAPACITY = 512` is a guess — large
enough to absorb a multi-second stall at a one-second interval, with no
measurement behind it.

---

