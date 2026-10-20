#include "book.hpp"

#include <algorithm>
#include <cmath>
#include <utility>

namespace kalshi {

std::optional<Level> best_bid(const Book& b) {
    if (b.yes_bids.empty()) return std::nullopt;
    return *std::max_element(
        b.yes_bids.begin(), b.yes_bids.end(),
        [](const Level& l, const Level& r) { return l.price_cents < r.price_cents; });
}

std::optional<Level> best_ask(const Book& b) {
    if (b.yes_asks.empty()) return std::nullopt;
    return *std::min_element(
        b.yes_asks.begin(), b.yes_asks.end(),
        [](const Level& l, const Level& r) { return l.price_cents < r.price_cents; });
}

std::optional<int> spread_cents(const Book& b) {
    auto bid = best_bid(b);
    auto ask = best_ask(b);
    if (!bid || !ask) return std::nullopt;
    return ask->price_cents - bid->price_cents;
}

std::optional<double> mid_cents(const Book& b) {
    auto bid = best_bid(b);
    auto ask = best_ask(b);
    if (!bid || !ask) return std::nullopt;
    return (static_cast<double>(bid->price_cents) +
            static_cast<double>(ask->price_cents)) / 2.0;
}

double depth_within(const std::vector<Level>& side, int reference_cents,
                    int window_cents) {
    if (window_cents < 0) return 0.0;
    double sum = 0.0;
    for (const Level& l : side) {
        if (std::abs(l.price_cents - reference_cents) <= window_cents) {
            sum += l.size;
        }
    }
    return sum;
}

double total_size(const std::vector<Level>& side) {
    double sum = 0.0;
    for (const Level& l : side) sum += l.size;
    return sum;
}

bool crossed(const Book& b) {
    auto bid = best_bid(b);
    auto ask = best_ask(b);
    if (!bid || !ask) return false;
    return bid->price_cents >= ask->price_cents;
}

void sort_sides(Book& b) {
    std::sort(b.yes_bids.begin(), b.yes_bids.end(),
              [](const Level& l, const Level& r) {
                  return l.price_cents > r.price_cents;
              });
    std::sort(b.yes_asks.begin(), b.yes_asks.end(),
              [](const Level& l, const Level& r) {
                  return l.price_cents < r.price_cents;
              });
}

static std::vector<Level> convert(const RawLevels& rows, bool invert) {
    std::vector<Level> out;
    out.reserve(rows.size());
    for (const auto& r : rows) {
        const int price = invert ? 100 - r.first : r.first;
        if (price < 0 || price > 100) continue;
        if (!(r.second > 0.0)) continue;
        out.push_back(Level{price, r.second});
    }
    return out;
}

std::vector<Level> yes_side(const RawLevels& rows) { return convert(rows, false); }

std::vector<Level> asks_from_no_side(const RawLevels& rows) {
    return convert(rows, true);
}

Book assemble(std::string ticker, std::int64_t captured_ns,
              const RawLevels& yes_rows, const RawLevels& no_rows) {
    Book b;
    b.ticker = std::move(ticker);
    b.captured_ns = captured_ns;
    b.yes_bids = yes_side(yes_rows);
    b.yes_asks = asks_from_no_side(no_rows);
    sort_sides(b);
    return b;
}

Backoff::Backoff(long base_ms, long cap_ms, unsigned seed)
    : base_ms_(base_ms < 1 ? 1 : base_ms),
      cap_ms_(cap_ms < 1 ? 1 : cap_ms),
      state_(seed == 0 ? 1u : seed) {}

long Backoff::next_delay_ms() {
    long shifted = base_ms_;
    for (int i = 0; i < attempt_ && shifted < cap_ms_; ++i) {
        shifted <<= 1;
    }
    if (shifted > cap_ms_ || shifted <= 0) shifted = cap_ms_;
    ++attempt_;

    state_ = state_ * 1664525u + 1013904223u;
    double jitter = 0.5 + (static_cast<double>(state_ >> 8) / 16777216.0) * 0.5;
    long out = static_cast<long>(static_cast<double>(shifted) * jitter);
    if (out < 1) out = 1;
    if (out > cap_ms_) out = cap_ms_;
    return out;
}

void Backoff::reset() { attempt_ = 0; }

void Latency::add(std::int64_t micros) {
    if (micros < 0) return;
    samples_.push_back(micros);
}

std::int64_t Latency::percentile(double p) const {
    if (samples_.empty()) return 0;
    if (p < 0.0) p = 0.0;
    if (p > 1.0) p = 1.0;
    std::vector<std::int64_t> sorted = samples_;
    std::sort(sorted.begin(), sorted.end());
    std::size_t idx = static_cast<std::size_t>(p * (sorted.size() - 1) + 0.5);
    if (idx >= sorted.size()) idx = sorted.size() - 1;
    return sorted[idx];
}

std::int64_t Latency::max() const {
    if (samples_.empty()) return 0;
    return *std::max_element(samples_.begin(), samples_.end());
}

void Latency::clear() { samples_.clear(); }

}
