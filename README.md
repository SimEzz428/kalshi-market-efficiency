# Kalshi market efficiency study

Can you make money on Kalshi's 15-minute crypto markets?

I spent months finding out. **The answer is no, and that is the result.**

I recorded 700,000 order-book snapshots, tested 307 trading strategies against
them, and not one survived correction for the fact that I had tried 307 things.
The markets are efficient at this horizon. The edge isn't there.

## Why a negative result is the point

It would have been easy to find a "winning" strategy here. Test enough
configurations and some will look profitable by luck alone — that is what
multiple testing does to you.

The confirmation came from a different direction. A model of trading costs
predicted what the markets should price, and it matched what they actually
priced **to 0.005 cents**. That is not a strategy failing to find an edge; that
is a market being priced correctly.

## What's in here

| | |
|---|---|
| `recorder.py` | records order books from the live API |
| `analyze.py` | tests strategies, with multiple-comparison correction |
| `execute.py` | places and reconciles real orders |
| `honest.py` | the checks that killed my own earlier results |
| `book.hpp` / `book.cpp` | C++17 recorder core, no dependencies |
| `recorder.cpp` | C++ recorder shell: HTTP, signing, SQLite |

## Run it

Secrets come from the environment, never the command line — anything in `argv`
shows up in `ps` and in your shell history.

```sh
export KALSHI_KEY_ID=...
export KALSHI_PRIVATE_KEY_PATH=$HOME/.kalshi/key.pem
python3 recorder.py
python3 analyze.py
```

The C++ core builds and tests with no libraries installed:

```sh
make test     # 63 checks
make san      # AddressSanitizer + UndefinedBehaviorSanitizer
make tsan     # ThreadSanitizer, for the queue
```

## Bugs I found in my own analysis

**Win streaks were inflating my t-statistics to 13–18.** Short runs of wins made
sample variance collapse toward zero, which made nonsense look significant.
Flooring the variance at its theoretical minimum killed a whole class of false
positives across three analyzers.

**The YES and NO sides are both bid sides.** They are not a bid and an ask.
A NO bid at 55 cents is a YES offer at 45. Get this backwards and nothing
crashes — every recorded spread is just quietly wrong. Six of the 63 C++ checks
exist only to pin this down.

**The recorder drops data rather than stalling.** If the disk can't keep up, the
oldest snapshot is discarded and counted. Blocking the capture loop instead
would silently stretch the sampling interval, which corrupts every timestamp the
analysis depends on.


