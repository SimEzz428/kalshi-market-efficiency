#include "book.hpp"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cmath>
#include <set>
#include <thread>
#include <vector>

using namespace kalshi;

static int failures = 0;
static int checks = 0;

static void check(bool ok, const char* name) {
    ++checks;
    if (!ok) ++failures;
    std::printf("  %-58s %s\n", name, ok ? "pass" : "FAIL");
}

static Book sample() {
    Book b;
    b.ticker = "KXBTCD-26OCT19-T1";
    b.yes_bids = {{41, 120.0}, {39, 5.0}, {44, 2039.0}, {40, 11.5}};
    b.yes_asks = {{47, 60.0}, {45, 0.01}, {52, 900.0}};
    return b;
}

int main() {
    Book b = sample();

    check(best_bid(b).has_value() && best_bid(b)->price_cents == 44,
          "best_bid picks the highest bid, not the first");
    check(best_ask(b).has_value() && best_ask(b)->price_cents == 45,
          "best_ask picks the lowest ask, not the first");
    check(spread_cents(b).has_value() && *spread_cents(b) == 1,
          "spread is ask minus bid");
    check(mid_cents(b).has_value() && std::fabs(*mid_cents(b) - 44.5) < 1e-9,
          "mid is fractional, not integer-truncated");

    check(std::fabs(best_ask(b)->size - 0.01) < 1e-12,
          "fractional contract size survives (0.01, not 0)");

    Book empty;
    check(!best_bid(empty).has_value() && !best_ask(empty).has_value(),
          "empty book returns nullopt rather than a default Level");
    check(!spread_cents(empty).has_value() && !mid_cents(empty).has_value(),
          "spread and mid are nullopt on an empty book");

    Book one_sided;
    one_sided.yes_bids = {{30, 5.0}};
    check(!spread_cents(one_sided).has_value(),
          "one-sided book has no spread");
    check(best_bid(one_sided).has_value(),
          "one-sided book still reports its own side");

    check(std::fabs(depth_within(b.yes_bids, 44, 0) - 2039.0) < 1e-9,
          "depth_within window 0 is the reference level only");
    check(std::fabs(depth_within(b.yes_bids, 44, 4) - 2170.5) < 1e-9,
          "depth_within sums every level inside the window");
    check(std::fabs(depth_within(b.yes_bids, 44, 4) -
                    (total_size(b.yes_bids) - 5.0)) < 1e-9,
          "the window excludes the level 5 cents out, and only that one");
    check(std::fabs(depth_within(b.yes_bids, 44, 100) - total_size(b.yes_bids)) < 1e-9,
          "a wide window equals the whole side");
    check(std::fabs(depth_within(b.yes_bids, 44, -1)) < 1e-12,
          "negative window returns 0 rather than reading garbage");

    Book x;
    x.yes_bids = {{50, 1.0}};
    x.yes_asks = {{49, 1.0}};
    check(crossed(x), "a crossed book is detected");
    check(!crossed(b), "a normal book is not reported crossed");
    check(!crossed(empty), "an empty book is not reported crossed");

    Book s = sample();
    sort_sides(s);
    check(s.yes_bids.front().price_cents == 44 && s.yes_bids.back().price_cents == 39,
          "sort_sides puts bids in descending price order");
    check(s.yes_asks.front().price_cents == 45 && s.yes_asks.back().price_cents == 52,
          "sort_sides puts asks in ascending price order");

    Backoff bo(250, 30000, 7);
    long d1 = bo.next_delay_ms();
    long d2 = bo.next_delay_ms();
    long d3 = bo.next_delay_ms();
    check(d1 >= 125 && d1 <= 250, "first delay is base with jitter in [0.5x, 1x]");
    check(d2 >= d1 / 2, "delays grow with attempts");
    check(d3 <= 30000, "delay never exceeds the cap");

    bool capped = true;
    for (int i = 0; i < 40; ++i) {
        long d = bo.next_delay_ms();
        if (d < 1 || d > 30000) capped = false;
    }
    check(capped, "40 further attempts never overflow or exceed the cap");

    bo.reset();
    long after = bo.next_delay_ms();
    check(after >= 125 && after <= 250 && bo.attempts() == 1,
          "reset returns to the base delay");

    std::set<long> distinct;
    for (unsigned seed = 1; seed <= 8; ++seed) {
        Backoff j(1000, 30000, seed);
        distinct.insert(j.next_delay_ms());
    }
    check(distinct.size() > 1, "jitter differs across seeds, so clients do not sync");

    RawLevels yes_rows = {{44, 2039.0}, {41, 120.0}, {40, 11.5}, {39, 5.0}};
    RawLevels no_rows = {{55, 60.0}, {53, 0.01}, {48, 900.0}};
    Book a = assemble("KXBTCD-26OCT19-T1", 1234567890123456789LL, yes_rows, no_rows);

    check(a.ticker == "KXBTCD-26OCT19-T1" && a.captured_ns == 1234567890123456789LL,
          "assemble keeps the ticker and the full-precision timestamp");
    check(a.yes_bids.size() == 4 && a.yes_asks.size() == 3,
          "both sides survive assembly");
    check(best_ask(a).has_value() && best_ask(a)->price_cents == 45,
          "a NO bid at 55 becomes a YES ask at 45, not an ask at 55");
    check(a.yes_asks.front().price_cents == 45 &&
              a.yes_asks.back().price_cents == 52,
          "the whole NO side inverts: 55/53/48 -> 45/47/52, sorted ascending");
    check(spread_cents(a).has_value() && *spread_cents(a) == 1,
          "the spread after inversion is 1 cent, not 11");
    check(!crossed(a), "inverting the NO side does not produce a false cross");

    Book no_invert_check = a;
    check(std::fabs(no_invert_check.yes_asks.front().size - 60.0) < 1e-9,
          "inversion moves the price but keeps the size with its own level");

    RawLevels dirty = {{44, 10.0}, {44, 0.0}, {44, -3.0}, {-1, 5.0}, {101, 5.0}};
    check(yes_side(dirty).size() == 1,
          "zero, negative and out-of-range levels are dropped, not stored as 0");
    check(asks_from_no_side({{0, 5.0}}).size() == 1 &&
              asks_from_no_side({{0, 5.0}}).front().price_cents == 100,
          "a NO bid at 0 inverts to a YES ask at 100, still in range");
    check(asks_from_no_side({{101, 5.0}}).empty(),
          "a NO price above 100 would invert negative and is dropped");

    Book none = assemble("X", 1, {}, {});
    check(none.yes_bids.empty() && none.yes_asks.empty() &&
              !spread_cents(none).has_value(),
          "an empty orderbook assembles into an empty book, not a crash");

    Latency lat;
    check(lat.percentile(0.5) == 0 && lat.count() == 0 && lat.max() == 0,
          "empty latency reports zeros rather than crashing");
    for (int i = 1; i <= 100; ++i) lat.add(i);
    check(lat.count() == 100, "every sample is retained");
    check(lat.percentile(0.5) == 50 || lat.percentile(0.5) == 51,
          "p50 of 1..100 lands at the middle");
    check(lat.percentile(0.99) >= 99, "p99 of 1..100 is at the tail");
    check(lat.percentile(1.0) == 100 && lat.max() == 100, "p100 equals max");
    check(lat.percentile(0.0) == 1, "p0 is the minimum");
    lat.add(-5);
    check(lat.count() == 100, "a negative sample is rejected, not stored");
    lat.clear();
    check(lat.count() == 0, "clear empties the window");

    BoundedQueue<int> bq(4);
    check(bq.capacity() == 4 && bq.size() == 0 && bq.dropped() == 0,
          "a new queue is empty with no drops");

    for (int i = 1; i <= 4; ++i) check(bq.push(i), "push inside capacity accepts");
    check(bq.size() == 4 && bq.dropped() == 0, "capacity is filled without drops");

    check(!bq.push(5), "pushing past capacity reports that it dropped");
    check(bq.size() == 4 && bq.dropped() == 1,
          "the queue stays at capacity and counts the drop");

    int got = 0;
    check(bq.pop(got) && got == 2,
          "the OLDEST item was dropped, so 2 is now at the front");
    check(bq.pop(got) && got == 3 && bq.pop(got) && got == 4 &&
              bq.pop(got) && got == 5,
          "the remaining items come out in FIFO order");
    check(bq.size() == 0, "the queue is drained");

    BoundedQueue<int> waiter(2);
    std::atomic<bool> received{false};
    std::thread consumer([&waiter, &received] {
        int v = 0;
        if (waiter.pop(v) && v == 99) received = true;
    });
    std::this_thread::sleep_for(std::chrono::milliseconds(40));
    waiter.push(99);
    consumer.join();
    check(received.load(), "a consumer blocked on an empty queue wakes on push");

    BoundedQueue<int> closer(2);
    std::atomic<bool> returned_false{false};
    std::thread blocked([&closer, &returned_false] {
        int v = 0;
        returned_false = !closer.pop(v);
    });
    std::this_thread::sleep_for(std::chrono::milliseconds(40));
    closer.close();
    blocked.join();
    check(returned_false.load(),
          "close() wakes a blocked consumer instead of hanging forever");
    check(!closer.push(1), "a closed queue refuses new work");

    BoundedQueue<int> drainer(8);
    drainer.push(7);
    drainer.push(8);
    drainer.close();
    int d = 0;
    check(drainer.pop(d) && d == 7 && drainer.pop(d) && d == 8,
          "items queued before close are still drained after it");
    check(!drainer.pop(d), "pop reports exhaustion once a closed queue empties");

    const int PRODUCERS = 4;
    const int PER_PRODUCER = 2000;
    BoundedQueue<int> stress(64);
    std::atomic<long> consumed{0};
    std::atomic<long> sum{0};

    std::thread sink([&stress, &consumed, &sum] {
        int v = 0;
        while (stress.pop(v)) {
            ++consumed;
            sum += v;
        }
    });

    std::vector<std::thread> producers;
    for (int p = 0; p < PRODUCERS; ++p) {
        producers.emplace_back([&stress, p] {
            for (int i = 0; i < PER_PRODUCER; ++i) stress.push(p * PER_PRODUCER + i);
        });
    }
    for (auto& t : producers) t.join();
    stress.close();
    sink.join();

    const long pushed = static_cast<long>(PRODUCERS) * PER_PRODUCER;
    check(consumed.load() + stress.dropped() == pushed,
          "under 4 producers nothing is lost or duplicated: consumed + dropped = pushed");
    check(consumed.load() > 0 && consumed.load() <= pushed,
          "the consumer saw a sane number of items");
    check(stress.size() == 0, "the queue is empty after a clean shutdown");

    std::printf("\n  %d checks, %d failures\n", checks, failures);
    return failures == 0 ? 0 : 1;
}
