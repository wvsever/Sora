#pragma once
// REST client for the external regulatory calculator (schemas/calculator/openapi.yaml, plans/11_integrations.md).
//
// - Reads /v1/capabilities first (retried like a batch) and fails fast if the calculation or the parameter set is
//   not supported.
// - Splits every call into batches (maxRecordsPerSyncRequest by default); a batch above that limit is sent as
//   an async job (/v1/jobs) and polled. Up to maxConcurrentRequests batches run in parallel.
// - Retries 429/502/503/504 and connection errors with exponential backoff, honouring Retry-After, always with
//   the same Idempotency-Key. Keys are UUIDs derived from SHA-256(run id, calculation, request body), so a rerun
//   sends the same keys.
// - Exact decimals both ways: amounts and probabilities are scaled integers in Sora and decimal strings on the
//   wire. Nothing is formatted or parsed through a binary float.
// - Every response is validated (one result per record, recordIds echoed in order, status, decimal format).
// - Optional replay cache on disk, keyed by calculator name/version and request fingerprint: a rerun with
//   identical inputs replays stored responses and needs no calculator (capabilities are cached too). Writing it
//   is best effort.
// - Streaming (IrbStream): records are produced and consumed per batch, within a bounded window of batches.

#include <cstdint>
#include <filesystem>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "sora/types.hpp"

namespace sora::calc {

struct CalculatorError : Error {
    using Error::Error;
};

// ---------------------------------------------------------------------------------------------- decimals

// Exact decimal text of `scaled` / 10^scale, e.g. (12345, 2) -> "123.45", (-5, 3) -> "-0.005".
std::string format_decimal(std::int64_t scaled, int scale);
// True if `s` matches the contract's Decimal pattern ^-?[0-9]+(\.[0-9]+)?$.
bool is_decimal(std::string_view s);
// Parses a Decimal string into an integer scaled by 10^scale. Digits beyond `scale` are rounded half to even.
// Returns nullopt if `s` is not a Decimal or does not fit into 64 bits.
std::optional<std::int64_t> parse_decimal(std::string_view s, int scale);
// A model parameter held as double, rounded to 9 decimals (the SIM probability scale).
Nano to_nano(double x);

// ---------------------------------------------------------------------------------------------- hashing

std::string sha256_hex(std::string_view data);
// RFC 9562 version-8 UUID from the first 128 bits of SHA-256(data).
std::string uuid_from_hash(std::string_view data);
// Idempotency-Key (= context.requestId) of a request: `body` is the request encoded without a requestId.
std::string idempotency_key(std::string_view run_id, std::string_view calculation, std::string_view body);

// ---------------------------------------------------------------------------------------------- protocol

struct Capabilities {
    std::string name, version, api_version;
    std::vector<std::string> calculations, param_sets;
    std::size_t max_records = 0, max_sync_records = 0, max_concurrent = 1;
};
Capabilities parse_capabilities(std::string_view json);   // throws CalculatorError

// Identifies the Sora run and the projection point (RequestContext without requestId).
struct RequestContext {
    std::string run_id, scenario = "actual", reference_date, param_set, currency;
    int year = 0;
};

// One IRB record (IrbRecord in the contract). Amounts in cents, probabilities in Nano.
struct IrbRecord {
    std::string record_id, exposure_class, approach = "airb";
    Cents ead = 0;
    Nano pd = 0, lgd = 0;
    std::int64_t maturity_bp = 25'000;          // maturity in years x 10^4 (2.5 years)
    bool defaulted = false;
    std::optional<Nano> elbe;
    std::optional<Cents> turnover_eur;           // sent as annualTurnoverEurMillions (exact)
    bool large_financial_entity = false;
    std::string currency, country;
};

struct Message {
    std::string code, message, field;
};

struct IrbResult {
    std::string record_id;
    bool ok = false;
    Cents rea = 0, expected_loss = 0;
    std::optional<Nano> risk_weight;
    std::vector<Message> errors;   // rejected records
};

// Request body (canonical JSON: sorted keys, no whitespace) for records[first, first + count).
// `request_id` empty leaves context.requestId out (the form hashed into the idempotency key).
std::string encode_irb_request(const RequestContext& ctx, const std::vector<IrbRecord>& records, std::size_t first,
                               std::size_t count, const std::string& request_id);

// A request ready to send: `key` = idempotency_key(ctx.run_id, "irb", body without requestId) = context.requestId.
// The records are encoded once; the key is hashed from the parts of that encoding, then requestId is inserted.
// `body` is byte-identical to encode_irb_request(ctx, records, first, count, key).
struct IrbRequest {
    std::string key, body;
};
IrbRequest make_irb_request(const RequestContext& ctx, const std::vector<IrbRecord>& records, std::size_t first,
                            std::size_t count);
// Validates an IrbResponse against the request and decodes it. Throws CalculatorError on any violation.
std::vector<IrbResult> decode_irb_response(std::string_view json, const std::vector<IrbRecord>& records,
                                           std::size_t first, std::size_t count, const std::string& request_id,
                                           const Capabilities& caps, const std::string& param_set);

// ---------------------------------------------------------------------------------------------- transport

struct Response {
    int status = 0;                                // 0 = no HTTP response (connection error, timeout)
    std::string body, error;
    std::map<std::string, std::string> headers;    // lower-case names
};
using Headers = std::vector<std::pair<std::string, std::string>>;

// One connection (not thread-safe); the client creates one per worker thread.
class Transport {
public:
    virtual ~Transport() = default;
    // `path` is relative to the calculator base URL, e.g. "/v1/capabilities". `body` empty = GET.
    virtual Response send(const std::string& method, const std::string& path, const std::string& body,
                          const Headers& headers) = 0;
};
using TransportFactory = std::function<std::unique_ptr<Transport>()>;

struct ClientOptions {
    std::string url;                          // base URL, http(s)://host[:port][/prefix]
    std::filesystem::path ca_file;            // CA bundle for the server certificate (default: system store)
    std::filesystem::path cert_file, key_file;   // client certificate for mTLS
    std::string token_env = "SORA_CALCULATOR_TOKEN";   // bearer token, read from the environment only
    std::filesystem::path cache_dir;          // replay cache (empty = off)
    std::string param_set = "EU_CRR3_2025-01-01";
    std::string run_id;
    std::size_t max_batch = 0;                // 0 = maxRecordsPerSyncRequest; larger batches use /v1/jobs
    std::size_t max_concurrent = 0;           // 0 = maxConcurrentRequests from the capabilities
    int max_attempts = 8;                     // per HTTP call, including the first
    double backoff_initial = 0.25, backoff_max = 30.0;   // seconds
    double connect_timeout = 10.0, read_timeout = 300.0, job_timeout = 3600.0;
};

// An HTTP(S) transport for `options.url` (TLS, mTLS and bearer token as configured). Throws CalculatorError if a
// bearer token is configured and the URL is plain http:// to a host that is not loopback.
std::unique_ptr<Transport> http_transport(const ClientOptions& options);
// True for localhost, 127.0.0.0/8 and ::1 (with or without a port, IPv6 in brackets), e.g. "127.0.0.1:8080", "[::1]".
bool is_loopback_host(std::string_view host_port);

struct IrbCall {
    RequestContext context;
    std::vector<IrbRecord> records;
};

// Streaming IRB calls: records are produced per batch and consumed with their results per batch, so only a
// bounded window of batches (twice the number of worker threads) is in memory at any time.
struct IrbStream {
    std::vector<RequestContext> contexts;   // one per call (run id and parameter set are set by the client)
    std::vector<std::size_t> counts;        // records per call
    // Appends records [first, first + count) of `call` to `out` (empty on entry). Called concurrently from the
    // worker threads; must depend only on its arguments, so a rerun produces the same bytes.
    std::function<void(std::size_t call, std::size_t first, std::size_t count, std::vector<IrbRecord>& out)> fill;
    // Receives the results of records [first, first + results.size()) of `call`. Called one batch at a time, in
    // call and record order (the records themselves are released as soon as the response is decoded).
    std::function<void(std::size_t call, std::size_t first, std::vector<IrbResult>& results)> sink;
};

struct ClientStats {
    std::size_t requests = 0, retries = 0, jobs = 0, cache_hits = 0, batches = 0;
    bool offline = false;         // capabilities replayed from the cache (calculator unreachable)
    std::string offline_reason;   // the last error of GET /v1/capabilities, when offline
    std::size_t cache_write_failures = 0;   // replay cache entries that could not be written (best effort)
    std::string cache_write_error;          // the first such error
};

class Client {
public:
    // `factory` defaults to http_transport(options); tests pass an in-memory stub.
    explicit Client(ClientOptions options, TransportFactory factory = {});

    // GET /v1/capabilities (retried like a batch) and check `calculation` and the parameter set. If every attempt
    // fails (no response or a retryable status) and the replay cache holds the capabilities, continues offline
    // (stats().offline; every batch must then be in the cache).
    const Capabilities& connect(const std::string& calculation);
    const Capabilities& capabilities() const { return caps_; }

    // All batches of all calls share one worker pool; see IrbStream.
    void irb(const IrbStream& stream);
    // Results per call, in record order (all records in memory; for tests and small inputs).
    std::vector<std::vector<IrbResult>> irb(const std::vector<IrbCall>& calls);

    ClientStats stats() const;

private:
    struct Batch;
    // One HTTP call with retries (`attempts` 0 = options.max_attempts).
    Response call(Transport& t, const std::string& method, const std::string& path, const std::string& body,
                  const std::string& key, int attempts = 0);
    std::string run_batch(Transport& t, const Batch& b, const std::string& body, const std::string& key);
    // Best effort: a failure is counted in stats_ (and reported as CALC-004 by the REA projection), never thrown.
    void cache_write(const std::filesystem::path& file, const std::string& data);

    ClientOptions opt_;
    TransportFactory factory_;
    Capabilities caps_;
    ClientStats stats_;
    mutable std::mutex mu_;   // guards stats_ (workers update it)
};

}  // namespace sora::calc
