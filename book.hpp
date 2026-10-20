#pragma once

#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <mutex>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace kalshi {

struct Level {
    int price_cents = 0;
    double size = 0.0;
};

struct Book {
    std::string ticker;
    std::int64_t captured_ns = 0;
    std::vector<Level> yes_bids;
    std::vector<Level> yes_asks;
};

std::optional<Level> best_bid(const Book& b);
std::optional<Level> best_ask(const Book& b);
std::optional<int> spread_cents(const Book& b);
std::optional<double> mid_cents(const Book& b);

double depth_within(const std::vector<Level>& side, int reference_cents,
                    int window_cents);
double total_size(const std::vector<Level>& side);
bool crossed(const Book& b);

void sort_sides(Book& b);

using RawLevels = std::vector<std::pair<int, double>>;

std::vector<Level> yes_side(const RawLevels& rows);
std::vector<Level> asks_from_no_side(const RawLevels& rows);
Book assemble(std::string ticker, std::int64_t captured_ns,
              const RawLevels& yes_rows, const RawLevels& no_rows);

class Backoff {
public:
    Backoff(long base_ms = 250, long cap_ms = 30000, unsigned seed = 1);
    long next_delay_ms();
    void reset();
    int attempts() const { return attempt_; }

private:
    long base_ms_;
    long cap_ms_;
    unsigned state_;
    int attempt_ = 0;
};

template <typename T>
class BoundedQueue {
public:
    explicit BoundedQueue(std::size_t capacity)
        : capacity_(capacity < 1 ? 1 : capacity) {}

    BoundedQueue(const BoundedQueue&) = delete;
    BoundedQueue& operator=(const BoundedQueue&) = delete;

    bool push(T value) {
        bool dropped_one = false;
        {
            std::lock_guard<std::mutex> lock(m_);
            if (closed_) return false;
            if (q_.size() >= capacity_) {
                q_.pop_front();
                ++dropped_;
                dropped_one = true;
            }
            q_.push_back(std::move(value));
        }
        cv_.notify_one();
        return !dropped_one;
    }

    bool pop(T& out) {
        std::unique_lock<std::mutex> lock(m_);
        cv_.wait(lock, [this] { return !q_.empty() || closed_; });
        if (q_.empty()) return false;
        out = std::move(q_.front());
        q_.pop_front();
        return true;
    }

    void close() {
        {
            std::lock_guard<std::mutex> lock(m_);
            closed_ = true;
        }
        cv_.notify_all();
    }

    std::size_t size() const {
        std::lock_guard<std::mutex> lock(m_);
        return q_.size();
    }

    std::int64_t dropped() const {
        std::lock_guard<std::mutex> lock(m_);
        return dropped_;
    }

    bool closed() const {
        std::lock_guard<std::mutex> lock(m_);
        return closed_;
    }

    std::size_t capacity() const { return capacity_; }

private:
    mutable std::mutex m_;
    std::condition_variable cv_;
    std::deque<T> q_;
    std::size_t capacity_;
    std::int64_t dropped_ = 0;
    bool closed_ = false;
};

class Latency {
public:
    void add(std::int64_t micros);
    std::int64_t percentile(double p) const;
    std::int64_t count() const { return static_cast<std::int64_t>(samples_.size()); }
    std::int64_t max() const;
    void clear();

private:
    std::vector<std::int64_t> samples_;
};

}
