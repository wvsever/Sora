#include "sora/calculator.hpp"

#include <httplib.h>
#include <json.hpp>

#include <algorithm>
#include <array>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <fstream>
#include <limits>
#include <mutex>
#include <sstream>
#include <thread>

namespace sora::calc {

namespace fs = std::filesystem;
using json = nlohmann::json;   // std::map objects: keys are sorted, so encoding is canonical

// ============================================================================================== decimals

namespace {
constexpr std::array<std::uint64_t, 19> kPow10 = {
    1ULL, 10ULL, 100ULL, 1000ULL, 10000ULL, 100000ULL, 1000000ULL, 10000000ULL, 100000000ULL, 1000000000ULL,
    10000000000ULL, 100000000000ULL, 1000000000000ULL, 10000000000000ULL, 100000000000000ULL,
    1000000000000000ULL, 10000000000000000ULL, 100000000000000000ULL, 1000000000000000000ULL};
}

std::string format_decimal(std::int64_t scaled, int scale) {
    if (scale < 0 || scale > 18) throw Error("format_decimal: scale out of range");
    const bool neg = scaled < 0;
    const std::uint64_t u = neg ? 0 - static_cast<std::uint64_t>(scaled) : static_cast<std::uint64_t>(scaled);
    const std::uint64_t p = kPow10[static_cast<std::size_t>(scale)];
    std::string out = neg ? "-" : "";
    out += std::to_string(u / p);
    if (scale > 0) {
        std::string frac = std::to_string(u % p);
        out += '.';
        out.append(static_cast<std::size_t>(scale) - frac.size(), '0');
        out += frac;
    }
    return out;
}

bool is_decimal(std::string_view s) {
    std::size_t i = 0;
    if (i < s.size() && s[i] == '-') ++i;
    const auto digits = [&] {
        const std::size_t start = i;
        while (i < s.size() && s[i] >= '0' && s[i] <= '9') ++i;
        return i > start;
    };
    if (!digits()) return false;
    if (i == s.size()) return true;
    if (s[i] != '.') return false;
    ++i;
    return digits() && i == s.size();
}

std::optional<std::int64_t> parse_decimal(std::string_view s, int scale) {
    if (scale < 0 || scale > 18 || !is_decimal(s)) return std::nullopt;
    const bool neg = s.front() == '-';
    if (neg) s.remove_prefix(1);
    const auto dot = s.find('.');
    const std::string_view ip = s.substr(0, dot), fp = dot == std::string_view::npos ? std::string_view{} : s.substr(dot + 1);
    constexpr std::uint64_t kMax = static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max());
    std::uint64_t v = 0;
    const auto push = [&](char c) {
        const std::uint64_t d = static_cast<std::uint64_t>(c - '0');
        if (v > (kMax - d) / 10) return false;
        v = v * 10 + d;
        return true;
    };
    for (char c : ip)
        if (!push(c)) return std::nullopt;
    for (std::size_t k = 0; k < static_cast<std::size_t>(scale); ++k)
        if (!push(k < fp.size() ? fp[k] : '0')) return std::nullopt;
    if (fp.size() > static_cast<std::size_t>(scale)) {   // round half to even on the dropped digits
        const int next = fp[static_cast<std::size_t>(scale)] - '0';
        const bool sticky = fp.find_first_not_of('0', static_cast<std::size_t>(scale) + 1) != std::string_view::npos;
        if (next > 5 || (next == 5 && (sticky || (v & 1U)))) {
            if (v == kMax) return std::nullopt;
            ++v;
        }
    }
    return neg ? -static_cast<std::int64_t>(v) : static_cast<std::int64_t>(v);
}

Nano to_nano(double x) { return static_cast<Nano>(std::llround(x * 1e9)); }

// ============================================================================================== hashing

namespace {

// SHA-256 (FIPS 180-4). Small and dependency-free, so hashing works without OpenSSL.
class Sha256 {
public:
    void update(std::string_view data) {
        for (unsigned char c : data) {
            block_[fill_++] = c;
            if (fill_ == 64) { compress(); fill_ = 0; }
        }
        bits_ += static_cast<std::uint64_t>(data.size()) * 8;
    }
    std::array<std::uint8_t, 32> digest() {
        const std::uint64_t bits = bits_;
        block_[fill_++] = 0x80;
        if (fill_ > 56) {
            while (fill_ < 64) block_[fill_++] = 0;
            compress();
            fill_ = 0;
        }
        while (fill_ < 56) block_[fill_++] = 0;
        for (int i = 7; i >= 0; --i) block_[fill_++] = static_cast<std::uint8_t>(bits >> (i * 8));
        compress();
        std::array<std::uint8_t, 32> out{};
        for (std::size_t i = 0; i < 8; ++i)
            for (std::size_t j = 0; j < 4; ++j) out[i * 4 + j] = static_cast<std::uint8_t>(h_[i] >> (24 - 8 * j));
        return out;
    }

private:
    static std::uint32_t rotr(std::uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }
    void compress() {
        static constexpr std::uint32_t k[64] = {
            0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
            0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
            0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
            0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
            0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
            0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
            0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
            0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};
        std::uint32_t w[64];
        for (std::size_t i = 0; i < 16; ++i)
            w[i] = static_cast<std::uint32_t>(block_[i * 4]) << 24 | static_cast<std::uint32_t>(block_[i * 4 + 1]) << 16 |
                   static_cast<std::uint32_t>(block_[i * 4 + 2]) << 8 | static_cast<std::uint32_t>(block_[i * 4 + 3]);
        for (std::size_t i = 16; i < 64; ++i) {
            const std::uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
            const std::uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16] + s0 + w[i - 7] + s1;
        }
        std::uint32_t a = h_[0], b = h_[1], c = h_[2], d = h_[3], e = h_[4], f = h_[5], g = h_[6], h = h_[7];
        for (std::size_t i = 0; i < 64; ++i) {
            const std::uint32_t t1 = h + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + k[i] + w[i];
            const std::uint32_t t2 = (rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
            h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
        }
        h_[0] += a; h_[1] += b; h_[2] += c; h_[3] += d; h_[4] += e; h_[5] += f; h_[6] += g; h_[7] += h;
    }
    std::array<std::uint32_t, 8> h_ = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                       0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    std::array<std::uint8_t, 64> block_{};
    std::size_t fill_ = 0;
    std::uint64_t bits_ = 0;
};

std::string hex(const std::uint8_t* p, std::size_t n) {
    static constexpr char digits[] = "0123456789abcdef";
    std::string out;
    for (std::size_t i = 0; i < n; ++i) {
        out += digits[p[i] >> 4];
        out += digits[p[i] & 15];
    }
    return out;
}

}  // namespace

std::string sha256_hex(std::string_view data) {
    Sha256 h;
    h.update(data);
    const auto d = h.digest();
    return hex(d.data(), d.size());
}

std::string uuid_from_hash(std::string_view data) {
    Sha256 h;
    h.update(data);
    auto d = h.digest();
    d[6] = static_cast<std::uint8_t>((d[6] & 0x0F) | 0x80);   // version 8 (custom)
    d[8] = static_cast<std::uint8_t>((d[8] & 0x3F) | 0x80);   // RFC 9562 variant
    const std::string x = hex(d.data(), 16);
    return x.substr(0, 8) + "-" + x.substr(8, 4) + "-" + x.substr(12, 4) + "-" + x.substr(16, 4) + "-" + x.substr(20, 12);
}

std::string idempotency_key(std::string_view run_id, std::string_view calculation, std::string_view body) {
    // Length-prefixed parts, so no two different inputs share an encoding.
    std::string m = "sora-calculator-v1\n";
    for (auto part : {run_id, calculation, body}) m += std::to_string(part.size()) + ":" + std::string(part) + "\n";
    return uuid_from_hash(m);
}

// ============================================================================================== protocol

namespace {

const json& field(const json& j, const char* name, const std::string& where) {
    if (!j.is_object() || !j.contains(name)) throw CalculatorError(where + ": missing field " + name);
    return j.at(name);
}

std::string str_field(const json& j, const char* name, const std::string& where) {
    const auto& v = field(j, name, where);
    if (!v.is_string()) throw CalculatorError(where + ": " + name + " is not a string");
    return v.get<std::string>();
}

std::int64_t decimal_field(const json& j, const char* name, int scale, const std::string& where) {
    const auto& v = field(j, name, where);
    if (!v.is_string()) throw CalculatorError(where + ": " + name + " is not a decimal string");
    const auto x = parse_decimal(v.get_ref<const std::string&>(), scale);
    if (!x) throw CalculatorError(where + ": " + name + " is not a valid decimal: " + v.get<std::string>());
    return *x;
}

json parse_json(std::string_view text, const std::string& what) {
    try {
        return json::parse(text);
    } catch (const json::exception& e) {
        throw CalculatorError(what + ": invalid JSON (" + e.what() + ")");
    }
}

std::string problem_text(const Response& r) {
    if (r.status == 0) return r.error.empty() ? "no response" : r.error;
    std::string out = "HTTP " + std::to_string(r.status);
    try {
        const auto p = json::parse(r.body);
        if (p.is_object()) {
            if (p.contains("title") && p["title"].is_string()) out += " " + p["title"].get<std::string>();
            if (p.contains("detail") && p["detail"].is_string() && !p["detail"].get<std::string>().empty())
                out += ": " + p["detail"].get<std::string>();
        }
    } catch (const json::exception&) {
        if (!r.body.empty()) out += ": " + r.body.substr(0, 200);
    }
    return out;
}

}  // namespace

Capabilities parse_capabilities(std::string_view text) {
    const auto j = parse_json(text, "capabilities");
    const std::string where = "capabilities";
    Capabilities c;
    const auto& calc = field(j, "calculator", where);
    c.name = str_field(calc, "name", where + ".calculator");
    c.version = str_field(calc, "version", where + ".calculator");
    c.api_version = str_field(j, "apiVersion", where);
    for (const char* name : {"calculations", "paramSets"}) {
        const auto& a = field(j, name, where);
        if (!a.is_array()) throw CalculatorError(where + ": " + name + " is not an array");
        for (const auto& x : a) {
            if (!x.is_string()) throw CalculatorError(where + ": " + name + " holds a non-string");
            (std::strcmp(name, "calculations") == 0 ? c.calculations : c.param_sets).push_back(x.get<std::string>());
        }
    }
    const auto& lim = field(j, "limits", where);
    const auto positive = [&](const char* name, bool required) -> std::size_t {
        if (!lim.contains(name)) {
            if (required) throw CalculatorError(where + ".limits: missing " + name);
            return 1;
        }
        const auto& v = lim.at(name);
        if (!v.is_number_integer() || v.get<std::int64_t>() < 1)
            throw CalculatorError(where + ".limits: " + name + " must be a positive integer");
        return static_cast<std::size_t>(v.get<std::int64_t>());
    };
    c.max_records = positive("maxRecordsPerRequest", true);
    c.max_sync_records = std::min(positive("maxRecordsPerSyncRequest", true), c.max_records);
    c.max_concurrent = positive("maxConcurrentRequests", false);
    return c;
}

std::string encode_irb_request(const RequestContext& ctx, const std::vector<IrbRecord>& records, std::size_t first,
                               std::size_t count, const std::string& request_id) {
    json recs = json::array();
    for (std::size_t i = first; i < first + count; ++i) {
        const auto& r = records.at(i);
        json o = {{"recordId", r.record_id},
                  {"exposureClass", r.exposure_class},
                  {"approach", r.approach},
                  {"ead", format_decimal(r.ead, 2)},
                  {"pd", format_decimal(r.pd, 9)},
                  {"lgd", format_decimal(r.lgd, 9)},
                  {"maturityYears", format_decimal(r.maturity_bp, 4)}};
        if (r.defaulted) o["isDefaulted"] = true;
        if (r.elbe) o["elbe"] = format_decimal(*r.elbe, 9);
        if (r.turnover_eur) o["annualTurnoverEurMillions"] = format_decimal(*r.turnover_eur, 8);   // cents / 10^8
        if (r.large_financial_entity) o["isLargeFinancialSectorEntity"] = true;
        if (!r.currency.empty()) o["currency"] = r.currency;
        if (!r.country.empty()) o["countryOfRisk"] = r.country;
        recs.push_back(std::move(o));
    }
    json c = {{"runId", ctx.run_id},
              {"scenario", ctx.scenario},
              {"projectionYear", ctx.year},
              {"referenceDate", ctx.reference_date},
              {"paramSet", ctx.param_set},
              {"reportingCurrency", ctx.currency},
              {"inputFingerprint", "sha256:" + sha256_hex(recs.dump())}};
    if (!request_id.empty()) c["requestId"] = request_id;
    return json{{"context", std::move(c)}, {"records", std::move(recs)}}.dump();
}

std::vector<IrbResult> decode_irb_response(std::string_view text, const std::vector<IrbRecord>& records,
                                           std::size_t first, std::size_t count, const std::string& request_id,
                                           const Capabilities& caps, const std::string& param_set) {
    const std::string where = "IRB response " + request_id;
    const auto j = parse_json(text, where);
    const auto& meta = field(j, "meta", where);
    const std::string wm = where + ".meta", wc = where + ".meta.calculator";
    if (str_field(meta, "requestId", wm) != request_id)
        throw CalculatorError(where + ": meta.requestId does not echo the request");
    const auto& calc = field(meta, "calculator", wm);
    const auto name = str_field(calc, "name", wc), version = str_field(calc, "version", wc);
    if (name != caps.name || version != caps.version)
        throw CalculatorError(where + ": calculator " + name + " " + version + " differs from the capabilities (" + caps.name +
                              " " + caps.version + ")");
    if (str_field(meta, "paramSet", wm) != param_set)
        throw CalculatorError(where + ": meta.paramSet differs from the request");
    const auto& results = field(j, "results", where);
    if (!results.is_array()) throw CalculatorError(where + ": results is not an array");
    if (results.size() != count)
        throw CalculatorError(where + ": " + std::to_string(results.size()) + " results for " + std::to_string(count) + " records");
    std::vector<IrbResult> out(count);
    for (std::size_t k = 0; k < count; ++k) {
        const auto& r = results[k];
        const auto& expected = records.at(first + k).record_id;
        const std::string w = where + " result " + std::to_string(k);
        IrbResult& x = out[k];
        x.record_id = str_field(r, "recordId", w);
        if (x.record_id != expected) throw CalculatorError(w + ": recordId " + x.record_id + ", expected " + expected);
        const auto status = str_field(r, "status", w);
        if (status == "ok") {
            x.ok = true;
            x.rea = decimal_field(r, "rea", 2, w);
            x.expected_loss = decimal_field(r, "expectedLoss", 2, w);
            if (x.rea < 0 || x.expected_loss < 0) throw CalculatorError(w + ": negative rea or expectedLoss");
            if (r.contains("riskWeight")) {
                x.risk_weight = decimal_field(r, "riskWeight", 9, w);
                if (*x.risk_weight < 0) throw CalculatorError(w + ": negative riskWeight");
            }
            for (const char* p : {"pdApplied", "lgdApplied"}) {
                if (!r.contains(p)) continue;
                const auto v = decimal_field(r, p, 9, w);
                if (v < 0 || v > 1'000'000'000) throw CalculatorError(w + ": " + p + " outside [0, 1]");
            }
        } else if (status == "rejected") {
            if (r.contains("errors")) {
                if (!r["errors"].is_array()) throw CalculatorError(w + ": errors is not an array");
                for (const auto& m : r["errors"]) {
                    Message msg;
                    msg.code = str_field(m, "code", w + ".errors");
                    msg.message = str_field(m, "message", w + ".errors");
                    if (m.contains("field") && m["field"].is_string()) msg.field = m["field"].get<std::string>();
                    x.errors.push_back(std::move(msg));
                }
            }
        } else {
            throw CalculatorError(w + ": status must be ok or rejected, got " + status);
        }
    }
    return out;
}

// ============================================================================================== transport

namespace {

struct Url {
    std::string scheme, host_port, prefix;
};

Url parse_url(const std::string& url) {
    const auto p = url.find("://");
    if (p == std::string::npos) throw CalculatorError("calculator URL must start with http:// or https://: " + url);
    Url u;
    u.scheme = url.substr(0, p);
    if (u.scheme != "http" && u.scheme != "https") throw CalculatorError("unsupported calculator URL scheme: " + u.scheme);
    const auto rest = url.substr(p + 3);
    const auto slash = rest.find('/');
    u.host_port = rest.substr(0, slash);
    if (slash != std::string::npos) u.prefix = rest.substr(slash);
    while (!u.prefix.empty() && u.prefix.back() == '/') u.prefix.pop_back();
    if (u.host_port.empty()) throw CalculatorError("calculator URL has no host: " + url);
    return u;
}

class HttpTransport final : public Transport {
public:
    explicit HttpTransport(const ClientOptions& o) : url_(parse_url(o.url)) {
        const std::string base = url_.scheme + "://" + url_.host_port;
        if (url_.scheme == "https") {
#ifdef CPPHTTPLIB_OPENSSL_SUPPORT
            client_ = std::make_unique<httplib::Client>(base, o.cert_file.string(), o.key_file.string());
            if (!o.ca_file.empty()) client_->set_ca_cert_path(o.ca_file.string());
            client_->enable_server_certificate_verification(true);
#else
            throw CalculatorError("this build of Sora has no TLS support (OpenSSL not found at build time)");
#endif
        } else {
            if (!o.cert_file.empty() || !o.ca_file.empty())
                throw CalculatorError("--calculator-ca/--calculator-cert need an https:// calculator URL");
            client_ = std::make_unique<httplib::Client>(base);
        }
        if (!client_->is_valid()) throw CalculatorError("cannot create an HTTP client for " + o.url);
        const auto usec = [](double s) { return std::chrono::microseconds(static_cast<std::int64_t>(s * 1e6)); };
        client_->set_connection_timeout(usec(o.connect_timeout));
        client_->set_read_timeout(usec(o.read_timeout));
        client_->set_write_timeout(usec(o.read_timeout));
        client_->set_keep_alive(true);
        if (!o.token_env.empty()) {
            if (const char* token = std::getenv(o.token_env.c_str()); token && *token) client_->set_bearer_token_auth(token);
        }
    }

    Response send(const std::string& method, const std::string& path, const std::string& body, const Headers& headers) override {
        httplib::Headers h(headers.begin(), headers.end());
        const std::string p = url_.prefix + path;
        auto res = method == "GET" ? client_->Get(p, h) : client_->Post(p, h, body, "application/json");
        Response r;
        if (!res) {
            r.error = httplib::to_string(res.error());
            return r;
        }
        r.status = res->status;
        r.body = std::move(res->body);
        for (const auto& [k, v] : res->headers) {
            std::string key = k;
            std::transform(key.begin(), key.end(), key.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
            r.headers[key] = v;
        }
        return r;
    }

private:
    Url url_;
    std::unique_ptr<httplib::Client> client_;
};

std::string path_safe(const std::string& s) {
    std::string out;
    for (char c : s) out += (std::isalnum(static_cast<unsigned char>(c)) || c == '.' || c == '-' || c == '_') ? c : '_';
    return out.empty() ? "_" : out;
}

std::optional<std::string> read_file(const fs::path& p) {
    std::ifstream f(p, std::ios::binary);
    if (!f) return std::nullopt;
    std::ostringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

void sleep_for(double seconds) {
    if (seconds > 0) std::this_thread::sleep_for(std::chrono::microseconds(static_cast<std::int64_t>(seconds * 1e6)));
}

bool retryable(const Response& r) {
    return r.status == 0 || r.status == 429 || r.status == 502 || r.status == 503 || r.status == 504;
}

}  // namespace

std::unique_ptr<Transport> http_transport(const ClientOptions& options) { return std::make_unique<HttpTransport>(options); }

// ============================================================================================== client

struct Client::Batch {
    std::size_t call = 0, first = 0, count = 0;
    bool job = false;
};

Client::Client(ClientOptions options, TransportFactory factory) : opt_(std::move(options)), factory_(std::move(factory)) {
    if (!factory_) {
        parse_url(opt_.url);   // fail early on a malformed URL
        factory_ = [o = opt_] { return http_transport(o); };
    }
    if (opt_.max_attempts < 1) opt_.max_attempts = 1;
}

ClientStats Client::stats() const {
    std::lock_guard lock(mu_);
    return stats_;
}

Response Client::call(Transport& t, const std::string& method, const std::string& path, const std::string& body,
                      const std::string& key, int attempts) {
    Headers headers = {{"Accept", "application/json"}};
    if (!key.empty()) headers.emplace_back("Idempotency-Key", key);
    if (attempts <= 0) attempts = opt_.max_attempts;
    for (int attempt = 1;; ++attempt) {
        Response r = t.send(method, path, body, headers);
        {
            std::lock_guard lock(mu_);
            ++stats_.requests;
        }
        if (!retryable(r) || attempt >= attempts) return r;
        double wait = std::min(opt_.backoff_initial * std::pow(2.0, attempt - 1), opt_.backoff_max);
        if (const auto it = r.headers.find("retry-after"); it != r.headers.end()) {
            // Delay in seconds (an HTTP-date is not used by the contract and falls back to the backoff).
            if (const auto s = parse_decimal(it->second, 3); s && *s >= 0) wait = std::min(static_cast<double>(*s) / 1e3, opt_.backoff_max);
        }
        {
            std::lock_guard lock(mu_);
            ++stats_.retries;
        }
        sleep_for(wait);
    }
}

const Capabilities& Client::connect(const std::string& calculation) {
    auto t = factory_();
    const fs::path cached = opt_.cache_dir.empty() ? fs::path{}
                                                   : opt_.cache_dir / "capabilities" / (sha256_hex(opt_.url).substr(0, 16) + ".json");
    // One quick attempt first: if the calculator is down and the cache knows it, replay offline.
    Response r = call(*t, "GET", "/v1/capabilities", "", "", 1);
    std::string text;
    if (r.status == 0 && !cached.empty() && fs::is_regular_file(cached)) {
        text = read_file(cached).value_or("");
        stats_.offline = true;
    } else {
        if (retryable(r)) r = call(*t, "GET", "/v1/capabilities", "", "", std::min(opt_.max_attempts, 3));   // fail fast
        if (r.status != 200) throw CalculatorError("calculator " + opt_.url + ": GET /v1/capabilities failed: " + problem_text(r));
        text = r.body;
    }
    caps_ = parse_capabilities(text);
    if (std::find(caps_.calculations.begin(), caps_.calculations.end(), calculation) == caps_.calculations.end())
        throw CalculatorError("calculator " + caps_.name + " " + caps_.version + " does not support the calculation " + calculation);
    if (std::find(caps_.param_sets.begin(), caps_.param_sets.end(), opt_.param_set) == caps_.param_sets.end())
        throw CalculatorError("calculator " + caps_.name + " " + caps_.version + " does not support the parameter set " + opt_.param_set);
    if (!stats_.offline && !cached.empty()) cache_write(cached, text);
    return caps_;
}

void Client::cache_write(const fs::path& file, const std::string& data) const {
    std::error_code ec;
    fs::create_directories(file.parent_path(), ec);
    // Write then rename: readers never see a partial file, and concurrent writers store identical content.
    std::ostringstream tmp_name;
    tmp_name << file.filename().string() << ".tmp." << std::this_thread::get_id();
    const fs::path tmp = file.parent_path() / tmp_name.str();
    {
        std::ofstream f(tmp, std::ios::binary);
        if (!f || !(f << data)) throw CalculatorError("cannot write the calculator cache file " + tmp.string());
    }
    fs::rename(tmp, file, ec);
    if (ec) throw CalculatorError("cannot write the calculator cache file " + file.string() + ": " + ec.message());
}

std::string Client::run_batch(Transport& t, const Batch& b, const std::string& body, const std::string& key) {
    if (!b.job) {
        const Response r = call(t, "POST", "/v1/credit-risk/irb", body, key);
        if (r.status != 200) throw CalculatorError("POST /v1/credit-risk/irb (" + key + ") failed: " + problem_text(r));
        return r.body;
    }
    // Async job. The job body is canonical too ("calculation" sorts before "request").
    const Response r = call(t, "POST", "/v1/jobs", R"({"calculation":"irb","request":)" + body + "}", key);
    if (r.status != 202 && r.status != 200) throw CalculatorError("POST /v1/jobs (" + key + ") failed: " + problem_text(r));
    {
        std::lock_guard lock(mu_);
        ++stats_.jobs;
    }
    auto job = parse_json(r.body, "job " + key);
    const std::string id = str_field(job, "jobId", "job " + key);
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::microseconds(static_cast<std::int64_t>(opt_.job_timeout * 1e6));
    double poll = 0.05;
    for (;;) {
        const auto status = str_field(job, "status", "job " + id);
        if (status == "failed" || status == "cancelled") {
            std::string detail;
            if (job.contains("error") && job["error"].is_object() && job["error"].contains("detail") && job["error"]["detail"].is_string())
                detail = ": " + job["error"]["detail"].get<std::string>();
            throw CalculatorError("calculator job " + id + " " + status + detail);
        }
        if (status == "succeeded") {
            const Response res = call(t, "GET", "/v1/jobs/" + id + "/result", "", "");
            if (res.status == 200) return res.body;
            if (res.status != 409) throw CalculatorError("GET /v1/jobs/" + id + "/result failed: " + problem_text(res));
        } else if (status != "queued" && status != "running") {
            throw CalculatorError("calculator job " + id + ": unknown status " + status);
        }
        if (std::chrono::steady_clock::now() > deadline) throw CalculatorError("calculator job " + id + " timed out");
        sleep_for(poll);
        poll = std::min(poll * 1.5, 5.0);
        const Response s = call(t, "GET", "/v1/jobs/" + id, "", "");
        if (s.status != 200) throw CalculatorError("GET /v1/jobs/" + id + " failed: " + problem_text(s));
        job = parse_json(s.body, "job " + id);
    }
}

std::vector<std::vector<IrbResult>> Client::irb(const std::vector<IrbCall>& calls) {
    if (caps_.name.empty()) throw CalculatorError("calculator client: connect() first");
    // Batch size: the sync limit, unless a larger batch is configured (then those batches go through jobs).
    const std::size_t size = opt_.max_batch ? std::min(opt_.max_batch, caps_.max_records) : caps_.max_sync_records;
    std::vector<Batch> batches;
    std::vector<std::vector<IrbResult>> out(calls.size());
    for (std::size_t c = 0; c < calls.size(); ++c) {
        const std::size_t n = calls[c].records.size();
        out[c].resize(n);
        const std::size_t k = (n + size - 1) / size;   // balanced batches of at most `size` records
        for (std::size_t i = 0, first = 0; i < k; ++i) {
            const std::size_t count = n / k + (i < n % k ? 1 : 0);
            batches.push_back({c, first, count, count > caps_.max_sync_records});
            first += count;
        }
    }
    {
        std::lock_guard lock(mu_);
        stats_.batches += batches.size();
    }
    const fs::path cache = opt_.cache_dir.empty() ? fs::path{}
                                                  : opt_.cache_dir / (path_safe(caps_.name) + "_" + path_safe(caps_.version)) / "irb";

    std::atomic<std::size_t> next{0};
    std::atomic<bool> stop{false};
    std::exception_ptr failure;
    std::mutex failure_mu;
    auto worker = [&] {
        std::unique_ptr<Transport> t;
        try {
            for (std::size_t i; !stop && (i = next++) < batches.size();) {
                const Batch& b = batches[i];
                const IrbCall& call_in = calls[b.call];
                RequestContext ctx = call_in.context;
                ctx.run_id = opt_.run_id;
                ctx.param_set = opt_.param_set;
                const std::string key = idempotency_key(opt_.run_id, "irb", encode_irb_request(ctx, call_in.records, b.first, b.count, ""));
                const std::string body = encode_irb_request(ctx, call_in.records, b.first, b.count, key);
                const fs::path file = cache.empty() ? fs::path{} : cache / (sha256_hex(body) + ".json");
                std::vector<IrbResult> res;
                bool hit = false;
                if (!file.empty()) {
                    if (const auto text = read_file(file)) {
                        try {
                            res = decode_irb_response(*text, call_in.records, b.first, b.count, key, caps_, opt_.param_set);
                            hit = true;
                        } catch (const CalculatorError&) {   // unreadable entry: fetch again and overwrite it
                        }
                    }
                }
                if (hit) {
                    std::lock_guard lock(mu_);
                    ++stats_.cache_hits;
                } else {
                    if (stats_.offline)
                        throw CalculatorError("calculator unreachable and batch " + key + " is not in the replay cache");
                    if (!t) t = factory_();
                    const std::string text = run_batch(*t, b, body, key);
                    res = decode_irb_response(text, call_in.records, b.first, b.count, key, caps_, opt_.param_set);
                    if (!file.empty()) cache_write(file, text);
                }
                std::move(res.begin(), res.end(), out[b.call].begin() + static_cast<std::ptrdiff_t>(b.first));
            }
        } catch (...) {
            std::lock_guard lock(failure_mu);
            if (!failure) failure = std::current_exception();
            stop = true;
        }
    };
    std::size_t nthreads = caps_.max_concurrent;
    if (opt_.max_concurrent) nthreads = std::min(nthreads, opt_.max_concurrent);
    nthreads = std::max<std::size_t>(1, std::min(nthreads, batches.size()));
    std::vector<std::thread> pool;
    for (std::size_t i = 1; i < nthreads; ++i) pool.emplace_back(worker);
    worker();
    for (auto& th : pool) th.join();
    if (failure) std::rethrow_exception(failure);
    return out;
}

}  // namespace sora::calc
