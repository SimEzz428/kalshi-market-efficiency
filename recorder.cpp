#include "book.hpp"

#include <curl/curl.h>
#include <openssl/err.h>
#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/rsa.h>
#include <sqlite3.h>
#include <nlohmann/json.hpp>

#include <chrono>
#include <csignal>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace {

volatile std::sig_atomic_t g_stop = 0;

void on_signal(int) { g_stop = 1; }

std::int64_t now_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

std::int64_t now_ms() { return now_ns() / 1000000; }

std::string env_or_throw(const char* name) {
    const char* v = std::getenv(name);
    if (v == nullptr || *v == '\0') {
        throw std::runtime_error(
            std::string("environment variable ") + name +
            " is not set. Secrets are read from the environment only, never"
            " from the command line, because argv is visible in ps and lands"
            " in shell history.");
    }
    return std::string(v);
}

std::string env_or(const char* name, const std::string& fallback) {
    const char* v = std::getenv(name);
    return (v == nullptr || *v == '\0') ? fallback : std::string(v);
}

std::string openssl_error() {
    unsigned long code = ERR_get_error();
    if (code == 0) return "no OpenSSL error on the queue";
    char buf[256] = {0};
    ERR_error_string_n(code, buf, sizeof(buf));
    return std::string(buf);
}

std::string base64(const unsigned char* data, std::size_t len) {
    const std::size_t out_len = 4 * ((len + 2) / 3);
    std::string out(out_len + 1, '\0');
    int written = EVP_EncodeBlock(reinterpret_cast<unsigned char*>(&out[0]),
                                 data, static_cast<int>(len));
    if (written < 0) throw std::runtime_error("EVP_EncodeBlock failed");
    out.resize(static_cast<std::size_t>(written));
    return out;
}

class Signer {
public:
    explicit Signer(const std::string& pem_path) {
        FILE* fp = std::fopen(pem_path.c_str(), "r");
        if (fp == nullptr) {
            throw std::runtime_error("cannot open private key at " + pem_path);
        }
        key_ = PEM_read_PrivateKey(fp, nullptr, nullptr, nullptr);
        std::fclose(fp);
        if (key_ == nullptr) {
            throw std::runtime_error("cannot parse private key: " + openssl_error());
        }
    }

    ~Signer() {
        if (key_ != nullptr) EVP_PKEY_free(key_);
    }

    Signer(const Signer&) = delete;
    Signer& operator=(const Signer&) = delete;

    std::string sign(const std::string& message) const {
        EVP_MD_CTX* ctx = EVP_MD_CTX_new();
        if (ctx == nullptr) throw std::runtime_error("EVP_MD_CTX_new failed");

        std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)> guard(
            ctx, &EVP_MD_CTX_free);

        EVP_PKEY_CTX* pctx = nullptr;
        if (EVP_DigestSignInit(ctx, &pctx, EVP_sha256(), nullptr, key_) != 1) {
            throw std::runtime_error("EVP_DigestSignInit: " + openssl_error());
        }
        if (EVP_PKEY_CTX_set_rsa_padding(pctx, RSA_PKCS1_PSS_PADDING) <= 0) {
            throw std::runtime_error("set_rsa_padding: " + openssl_error());
        }
        if (EVP_PKEY_CTX_set_rsa_pss_saltlen(pctx, RSA_PSS_SALTLEN_DIGEST) <= 0) {
            throw std::runtime_error("set_rsa_pss_saltlen: " + openssl_error());
        }

        std::size_t len = 0;
        if (EVP_DigestSign(ctx, nullptr, &len,
                           reinterpret_cast<const unsigned char*>(message.data()),
                           message.size()) != 1) {
            throw std::runtime_error("EVP_DigestSign sizing: " + openssl_error());
        }
        std::vector<unsigned char> sig(len);
        if (EVP_DigestSign(ctx, sig.data(), &len,
                           reinterpret_cast<const unsigned char*>(message.data()),
                           message.size()) != 1) {
            throw std::runtime_error("EVP_DigestSign: " + openssl_error());
        }
        sig.resize(len);
        return base64(sig.data(), sig.size());
    }

private:
    EVP_PKEY* key_ = nullptr;
};

std::string signing_message(std::int64_t timestamp_ms, const std::string& method,
                            const std::string& path) {
    return std::to_string(timestamp_ms) + method + path;
}

struct Response {
    long status = 0;
    std::string body;
    std::int64_t total_micros = 0;
    std::int64_t ttfb_micros = 0;
};

std::size_t collect(char* ptr, std::size_t size, std::size_t nmemb, void* sink) {
    const std::size_t n = size * nmemb;
    static_cast<std::string*>(sink)->append(ptr, n);
    return n;
}

class Http {
public:
    Http(std::string base, std::string key_id, const Signer& signer, long timeout_ms)
        : base_(std::move(base)),
          key_id_(std::move(key_id)),
          signer_(signer),
          timeout_ms_(timeout_ms) {
        handle_ = curl_easy_init();
        if (handle_ == nullptr) throw std::runtime_error("curl_easy_init failed");
        curl_easy_setopt(handle_, CURLOPT_WRITEFUNCTION, collect);
        curl_easy_setopt(handle_, CURLOPT_FOLLOWLOCATION, 0L);
        curl_easy_setopt(handle_, CURLOPT_TCP_NODELAY, 1L);
        curl_easy_setopt(handle_, CURLOPT_TCP_KEEPALIVE, 1L);
        curl_easy_setopt(handle_, CURLOPT_TIMEOUT_MS, timeout_ms_);
        curl_easy_setopt(handle_, CURLOPT_CONNECTTIMEOUT_MS, timeout_ms_);
        curl_easy_setopt(handle_, CURLOPT_USERAGENT, "kalshi-recorder-cpp/1");
    }

    ~Http() {
        if (handle_ != nullptr) curl_easy_cleanup(handle_);
    }

    Http(const Http&) = delete;
    Http& operator=(const Http&) = delete;

    Response get(const std::string& path, const std::string& query) {
        const std::int64_t ts = now_ms();
        const std::string sig = signer_.sign(signing_message(ts, "GET", path));

        curl_slist* headers = nullptr;
        headers = curl_slist_append(headers, "Accept: application/json");
        headers = curl_slist_append(
            headers, ("KALSHI-ACCESS-KEY: " + key_id_).c_str());
        headers = curl_slist_append(
            headers, ("KALSHI-ACCESS-TIMESTAMP: " + std::to_string(ts)).c_str());
        headers = curl_slist_append(
            headers, ("KALSHI-ACCESS-SIGNATURE: " + sig).c_str());

        std::unique_ptr<curl_slist, decltype(&curl_slist_free_all)> hguard(
            headers, &curl_slist_free_all);

        const std::string url = base_ + path + (query.empty() ? "" : "?" + query);

        Response r;
        curl_easy_setopt(handle_, CURLOPT_URL, url.c_str());
        curl_easy_setopt(handle_, CURLOPT_HTTPHEADER, headers);
        curl_easy_setopt(handle_, CURLOPT_WRITEDATA, &r.body);

        const std::int64_t t0 = now_ns();
        const CURLcode rc = curl_easy_perform(handle_);
        r.total_micros = (now_ns() - t0) / 1000;

        if (rc != CURLE_OK) {
            r.status = -1;
            r.body = curl_easy_strerror(rc);
            return r;
        }
        curl_easy_getinfo(handle_, CURLINFO_RESPONSE_CODE, &r.status);

        curl_off_t ttfb = 0;
        if (curl_easy_getinfo(handle_, CURLINFO_STARTTRANSFER_TIME_T, &ttfb) ==
            CURLE_OK) {
            r.ttfb_micros = static_cast<std::int64_t>(ttfb);
        }
        return r;
    }

private:
    std::string base_;
    std::string key_id_;
    const Signer& signer_;
    long timeout_ms_;
    CURL* handle_ = nullptr;
};

void bind_opt_int(sqlite3_stmt* st, int idx, const std::optional<int>& v) {
    if (v) {
        sqlite3_bind_int(st, idx, *v);
    } else {
        sqlite3_bind_null(st, idx);
    }
}

void bind_opt_double(sqlite3_stmt* st, int idx, const std::optional<double>& v) {
    if (v) {
        sqlite3_bind_double(st, idx, *v);
    } else {
        sqlite3_bind_null(st, idx);
    }
}

void bind_opt_text(sqlite3_stmt* st, int idx, const std::optional<std::string>& v) {
    if (v) {
        sqlite3_bind_text(st, idx, v->c_str(), -1, SQLITE_TRANSIENT);
    } else {
        sqlite3_bind_null(st, idx);
    }
}

const char* const STORE_SCHEMA =
    "PRAGMA journal_mode=WAL;"
    "PRAGMA synchronous=NORMAL;"
    "CREATE TABLE IF NOT EXISTS snapshots ("
    "  id           INTEGER PRIMARY KEY,"
    "  ticker       TEXT    NOT NULL,"
    "  captured_ns  INTEGER NOT NULL,"
    "  best_bid     INTEGER,"
    "  best_ask     INTEGER,"
    "  bid_size     REAL,"
    "  ask_size     REAL,"
    "  spread_cents INTEGER,"
    "  mid_cents    REAL,"
    "  bid_depth_3  REAL,"
    "  ask_depth_3  REAL,"
    "  bid_total    REAL,"
    "  ask_total    REAL,"
    "  n_bid_levels INTEGER NOT NULL,"
    "  n_ask_levels INTEGER NOT NULL,"
    "  crossed      INTEGER NOT NULL,"
    "  http_status  INTEGER NOT NULL,"
    "  total_micros INTEGER NOT NULL,"
    "  ttfb_micros  INTEGER NOT NULL,"
    "  raw          TEXT,"
    "  UNIQUE(ticker, captured_ns)"
    ");"
    "CREATE INDEX IF NOT EXISTS snapshots_ticker_time"
    "  ON snapshots(ticker, captured_ns);";

class Store {
public:
    Store(const std::string& path, bool keep_raw) : keep_raw_(keep_raw) {
        if (sqlite3_open(path.c_str(), &db_) != SQLITE_OK) {
            throw std::runtime_error("cannot open database at " + path);
        }
        char* err = nullptr;
        if (sqlite3_exec(db_, STORE_SCHEMA, nullptr, nullptr, &err) != SQLITE_OK) {
            const std::string msg = err == nullptr ? "unknown" : err;
            sqlite3_free(err);
            throw std::runtime_error("schema failed: " + msg);
        }
        static const char* const INSERT =
            "INSERT OR IGNORE INTO snapshots("
            "ticker, captured_ns, best_bid, best_ask, bid_size, ask_size,"
            "spread_cents, mid_cents, bid_depth_3, ask_depth_3, bid_total,"
            "ask_total, n_bid_levels, n_ask_levels, crossed, http_status,"
            "total_micros, ttfb_micros, raw)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)";
        if (sqlite3_prepare_v2(db_, INSERT, -1, &insert_, nullptr) != SQLITE_OK) {
            throw std::runtime_error(std::string("prepare failed: ") +
                                     sqlite3_errmsg(db_));
        }
        begin();
    }

    ~Store() {
        try {
            commit();
        } catch (...) {
        }
        if (insert_ != nullptr) sqlite3_finalize(insert_);
        if (db_ != nullptr) sqlite3_close(db_);
    }

    Store(const Store&) = delete;
    Store& operator=(const Store&) = delete;

    void add(const kalshi::Book& b, const Response& r, const std::string& raw) {
        const auto bid = kalshi::best_bid(b);
        const auto ask = kalshi::best_ask(b);

        std::optional<int> bid_px, ask_px;
        std::optional<double> bid_sz, ask_sz, bid_depth, ask_depth;
        if (bid) {
            bid_px = bid->price_cents;
            bid_sz = bid->size;
            bid_depth = kalshi::depth_within(b.yes_bids, bid->price_cents, 3);
        }
        if (ask) {
            ask_px = ask->price_cents;
            ask_sz = ask->size;
            ask_depth = kalshi::depth_within(b.yes_asks, ask->price_cents, 3);
        }

        std::optional<std::string> raw_col;
        if (keep_raw_) raw_col = raw;

        sqlite3_reset(insert_);
        sqlite3_clear_bindings(insert_);

        int i = 1;
        sqlite3_bind_text(insert_, i++, b.ticker.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_int64(insert_, i++, b.captured_ns);
        bind_opt_int(insert_, i++, bid_px);
        bind_opt_int(insert_, i++, ask_px);
        bind_opt_double(insert_, i++, bid_sz);
        bind_opt_double(insert_, i++, ask_sz);
        bind_opt_int(insert_, i++, kalshi::spread_cents(b));
        bind_opt_double(insert_, i++, kalshi::mid_cents(b));
        bind_opt_double(insert_, i++, bid_depth);
        bind_opt_double(insert_, i++, ask_depth);
        sqlite3_bind_double(insert_, i++, kalshi::total_size(b.yes_bids));
        sqlite3_bind_double(insert_, i++, kalshi::total_size(b.yes_asks));
        sqlite3_bind_int(insert_, i++, static_cast<int>(b.yes_bids.size()));
        sqlite3_bind_int(insert_, i++, static_cast<int>(b.yes_asks.size()));
        sqlite3_bind_int(insert_, i++, kalshi::crossed(b) ? 1 : 0);
        sqlite3_bind_int64(insert_, i++, r.status);
        sqlite3_bind_int64(insert_, i++, r.total_micros);
        sqlite3_bind_int64(insert_, i++, r.ttfb_micros);
        bind_opt_text(insert_, i++, raw_col);

        if (i - 1 != COLUMNS) {
            throw std::runtime_error(
                "bound " + std::to_string(i - 1) + " values for " +
                std::to_string(COLUMNS) + " columns; the INSERT and the bind"
                " sequence have drifted apart and every row after this would"
                " be silently misaligned");
        }

        if (sqlite3_step(insert_) != SQLITE_DONE) {
            throw std::runtime_error(std::string("insert failed: ") +
                                     sqlite3_errmsg(db_));
        }
        ++pending_;
        ++written_;
        if (pending_ >= BATCH) {
            commit();
            begin();
        }
    }

    void flush() {
        commit();
        begin();
    }

    std::int64_t written() const { return written_; }

private:
    static const int BATCH = 200;
    static const int COLUMNS = 19;

    void exec(const char* sql) {
        char* err = nullptr;
        if (sqlite3_exec(db_, sql, nullptr, nullptr, &err) != SQLITE_OK) {
            const std::string msg = err == nullptr ? "unknown" : err;
            sqlite3_free(err);
            throw std::runtime_error(std::string(sql) + " failed: " + msg);
        }
    }

    void begin() {
        if (in_tx_) return;
        exec("BEGIN IMMEDIATE");
        in_tx_ = true;
        pending_ = 0;
    }

    void commit() {
        if (!in_tx_) return;
        exec("COMMIT");
        in_tx_ = false;
        pending_ = 0;
    }

    sqlite3* db_ = nullptr;
    sqlite3_stmt* insert_ = nullptr;
    bool keep_raw_ = false;
    bool in_tx_ = false;
    int pending_ = 0;
    std::int64_t written_ = 0;
};

kalshi::RawLevels rows_from(const nlohmann::json& side) {
    kalshi::RawLevels out;
    if (!side.is_array()) return out;
    for (const auto& row : side) {
        if (!row.is_array() || row.size() < 2) continue;
        if (!row[0].is_number() || !row[1].is_number()) continue;
        out.emplace_back(row[0].get<int>(), row[1].get<double>());
    }
    return out;
}

kalshi::Book parse_book(const std::string& body, const std::string& ticker,
                        std::int64_t captured) {
    const nlohmann::json doc = nlohmann::json::parse(body, nullptr, true, true);
    if (!doc.contains("orderbook") || !doc["orderbook"].is_object()) {
        throw std::runtime_error("response has no orderbook object");
    }
    const nlohmann::json& ob = doc["orderbook"];

    kalshi::RawLevels yes_rows, no_rows;
    if (ob.contains("yes")) yes_rows = rows_from(ob["yes"]);
    if (ob.contains("no")) no_rows = rows_from(ob["no"]);

    return kalshi::assemble(ticker, captured, yes_rows, no_rows);
}

struct Snapshot {
    kalshi::Book book;
    Response meta;
    std::string raw;
};

const std::size_t QUEUE_CAPACITY = 512;

void writer_loop(kalshi::BoundedQueue<Snapshot>& queue, Store& store,
                 std::int64_t& write_errors) {
    Snapshot item;
    while (queue.pop(item)) {
        try {
            store.add(item.book, item.meta, item.raw);
        } catch (const std::exception& e) {
            ++write_errors;
            std::cerr << "  write failed: " << e.what() << std::endl;
        }
    }
    try {
        store.flush();
    } catch (const std::exception& e) {
        ++write_errors;
        std::cerr << "  final flush failed: " << e.what() << std::endl;
    }
}

void report(const kalshi::Latency& lat, std::int64_t rows, std::int64_t errors) {
    std::cout << "  rows=" << rows << "  errors=" << errors
              << "  requests=" << lat.count()
              << "  p50=" << lat.percentile(0.50) << "us"
              << "  p95=" << lat.percentile(0.95) << "us"
              << "  p99=" << lat.percentile(0.99) << "us"
              << "  max=" << lat.max() << "us" << std::endl;
}

}

int main(int argc, char** argv) {
    if (argc < 2) {
        std::cerr
            << "usage: " << argv[0] << " TICKER [TICKER ...]\n\n"
            << "  reads KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH from the\n"
            << "  environment; optional KALSHI_API_BASE, KALSHI_DB,\n"
            << "  KALSHI_POLL_MS, KALSHI_TIMEOUT_MS, KALSHI_KEEP_RAW,\n"
            << "  KALSHI_REPORT_EVERY\n";
        return 2;
    }

    std::vector<std::string> tickers;
    for (int i = 1; i < argc; ++i) tickers.emplace_back(argv[i]);

    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);

    try {
        const std::string key_id = env_or_throw("KALSHI_API_KEY_ID");
        const std::string key_path = env_or_throw("KALSHI_PRIVATE_KEY_PATH");
        const std::string base =
            env_or("KALSHI_API_BASE", "https://api.elections.kalshi.com");
        const std::string db_path = env_or("KALSHI_DB", "books.db");
        const long poll_ms = std::stol(env_or("KALSHI_POLL_MS", "1000"));
        const long timeout_ms = std::stol(env_or("KALSHI_TIMEOUT_MS", "5000"));
        const bool keep_raw = env_or("KALSHI_KEEP_RAW", "0") == "1";
        const long report_every = std::stol(env_or("KALSHI_REPORT_EVERY", "60"));

        if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) {
            throw std::runtime_error("curl_global_init failed");
        }

        Signer signer(key_path);
        Http http(base, key_id, signer, timeout_ms);
        Store store(db_path, keep_raw);
        kalshi::Latency lat;
        kalshi::Backoff backoff(250, 30000,
                                static_cast<unsigned>(now_ns() & 0x7fffffff));

        kalshi::BoundedQueue<Snapshot> queue(QUEUE_CAPACITY);
        std::int64_t write_errors = 0;
        std::thread writer(writer_loop, std::ref(queue), std::ref(store),
                           std::ref(write_errors));

        std::cout << "recording " << tickers.size() << " ticker(s) into "
                  << db_path << " every " << poll_ms << "ms"
                  << (keep_raw ? " (raw json kept)" : " (derived only)")
                  << ", queue capacity " << QUEUE_CAPACITY << std::endl;

        std::int64_t errors = 0;
        std::int64_t consecutive = 0;
        std::int64_t last_report = now_ms();

        while (g_stop == 0) {
            const std::int64_t cycle_start = now_ms();

            for (const std::string& ticker : tickers) {
                if (g_stop != 0) break;

                const std::string path =
                    "/trade-api/v2/markets/" + ticker + "/orderbook";
                const std::int64_t captured = now_ns();
                const Response r = http.get(path, "depth=10");

                if (r.status < 200 || r.status >= 300) {
                    ++errors;
                    ++consecutive;
                    const long wait = backoff.next_delay_ms();
                    std::cerr << "  " << ticker << " http=" << r.status
                              << " failure " << consecutive << " in a row,"
                              << " backing off " << wait << "ms"
                              << "  body=" << r.body.substr(0, 160) << std::endl;
                    for (long slept = 0; slept < wait && g_stop == 0; slept += 100) {
                        std::this_thread::sleep_for(std::chrono::milliseconds(100));
                    }
                    continue;
                }

                consecutive = 0;
                backoff.reset();
                lat.add(r.total_micros);

                try {
                    Snapshot item;
                    item.book = parse_book(r.body, ticker, captured);
                    item.meta = r;
                    if (keep_raw) item.raw = r.body;
                    queue.push(std::move(item));
                } catch (const std::exception& e) {
                    ++errors;
                    std::cerr << "  " << ticker << " parse: " << e.what()
                              << std::endl;
                }
            }

            if (report_every > 0 &&
                now_ms() - last_report >= report_every * 1000) {
                report(lat, store.written(), errors);
                std::cout << "  queue depth=" << queue.size()
                          << "  dropped=" << queue.dropped()
                          << "  write errors=" << write_errors << std::endl;
                lat.clear();
                last_report = now_ms();
            }

            const std::int64_t elapsed = now_ms() - cycle_start;
            const std::int64_t remaining = poll_ms - elapsed;
            for (std::int64_t slept = 0; slept < remaining && g_stop == 0;
                 slept += 50) {
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            }
        }

        queue.close();
        writer.join();

        std::cout << "\nstopped." << std::endl;
        report(lat, store.written(), errors);
        std::cout << "  dropped=" << queue.dropped()
                  << "  write errors=" << write_errors << std::endl;
        if (queue.dropped() > 0) {
            std::cout << "  the writer could not keep up with capture; "
                         "oldest snapshots were discarded to keep polling"
                      << std::endl;
        }
        curl_global_cleanup();
        return 0;

    } catch (const std::exception& e) {
        std::cerr << "fatal: " << e.what() << std::endl;
        curl_global_cleanup();
        return 1;
    }
}
