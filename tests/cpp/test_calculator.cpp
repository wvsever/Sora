// Calculator client: decimals, hashing, JSON encoding/decoding, response validation, and the client's
// batching, retries, jobs and replay cache against an in-memory stub (no network).

#include "doctest.h"

#include <json.hpp>

#include <algorithm>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <map>
#include <mutex>
#include <random>
#include <set>

#include "sora/calculator.hpp"
#include "sora/credit_parameters.hpp"
#include "sora/json_text.hpp"
#include "sora/rea.hpp"

using namespace sora;
using namespace sora::calc;
using json = nlohmann::json;
namespace fs = std::filesystem;


namespace {
// POSIX setenv/unsetenv do not exist on Windows; _putenv_s with an empty value removes the variable there.
void set_env(const std::string& name, const char* value) {
#ifdef _WIN32
    _putenv_s(name.c_str(), value);
#else
    ::setenv(name.c_str(), value, 1);
#endif
}
void unset_env(const std::string& name) {
#ifdef _WIN32
    _putenv_s(name.c_str(), "");
#else
    ::unsetenv(name.c_str());
#endif
}
}  // namespace

TEST_CASE("decimals are formatted and parsed exactly") {
    CHECK(format_decimal(12345, 2) == "123.45");
    CHECK(format_decimal(-5, 3) == "-0.005");
    CHECK(format_decimal(0, 2) == "0.00");
    CHECK(format_decimal(7, 0) == "7");
    CHECK(format_decimal(1'000'000'000, 9) == "1.000000000");
    CHECK(format_decimal(INT64_MIN, 2) == "-92233720368547758.08");

    CHECK(parse_decimal("123.45", 2) == 12345);
    CHECK(parse_decimal("7", 2) == 700);
    CHECK(parse_decimal("0.0125", 9) == 12'500'000);
    CHECK(parse_decimal("-0.01", 2) == -1);
    CHECK(parse_decimal("0.125", 2) == 12);      // half to even
    CHECK(parse_decimal("0.135", 2) == 14);
    CHECK(parse_decimal("0.1251", 2) == 13);     // sticky digits
    CHECK(parse_decimal("-1.005", 2) == -100);
    CHECK(parse_decimal("92233720368547758.07", 2) == INT64_MAX);
    CHECK_FALSE(parse_decimal("92233720368547758.08", 2));
    for (const char* bad : {"", "-", "1.", ".5", "1e5", "+1", "1,5", " 1", "0x10", "NaN"}) {
        CHECK_FALSE(is_decimal(bad));
        CHECK_FALSE(parse_decimal(bad, 2));
    }
    CHECK(to_nano(0.0125) == 12'500'000);
    CHECK(to_nano(1.0 / 3.0) == 333'333'333);
}

TEST_CASE("SHA-256 and idempotency keys are deterministic") {
    CHECK(sha256_hex("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    CHECK(sha256_hex("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    CHECK(sha256_hex("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq") ==
          "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1");
    CHECK(sha256_hex(std::string(1000, 'a')) == "41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3");

    const auto k = idempotency_key("run-1", "irb", R"({"records":[]})");
    CHECK(k == idempotency_key("run-1", "irb", R"({"records":[]})"));
    CHECK(k != idempotency_key("run-2", "irb", R"({"records":[]})"));
    CHECK(k != idempotency_key("run-1", "sa", R"({"records":[]})"));
    CHECK(k != idempotency_key("run-1", "irb", R"({"records":[1]})"));
    REQUIRE(k.size() == 36);
    CHECK(k[8] == '-');
    CHECK(k[14] == '8');                                  // version 8
    CHECK(std::string("89ab").find(k[19]) != std::string::npos);   // RFC 9562 variant
}

namespace {

IrbRecord record(const std::string& id, Cents ead = 123456) {
    IrbRecord r;
    r.record_id = id;
    r.exposure_class = "corporates_sme";
    r.ead = ead;
    r.pd = to_nano(0.0123456789);
    r.lgd = 450'000'000;
    r.maturity_bp = 31'234;
    r.turnover_eur = 1'250'000'000;   // EUR 12.5 million in cents
    r.country = "BE";
    return r;
}

RequestContext context() {
    RequestContext c;
    c.run_id = "run-1";
    c.scenario = "adverse";
    c.year = 2;
    c.reference_date = "2026-06-30";
    c.param_set = "EU_CRR3_2025-01-01";
    c.currency = "EUR";
    return c;
}

Capabilities caps() {
    Capabilities c;
    c.name = "stub";
    c.version = "1.0";
    c.calculations = {"irb"};
    c.param_sets = {"EU_CRR3_2025-01-01"};
    c.max_records = 10;
    c.max_sync_records = 3;
    c.max_concurrent = 4;
    return c;
}

json ok_response(const json& request, const std::string& version = "1.0") {
    json results = json::array();
    for (const auto& r : request["records"]) {
        const std::string id = r["recordId"];
        if (id.size() > 2 && id.substr(id.size() - 2) == "-R") {
            results.push_back({{"recordId", id}, {"status", "rejected"}, {"errors", {{{"code", "X-1"}, {"message", "no"}}}}});
        } else {   // risk weight 1: rea = ead, EL = 1% of ead (rounded down, fine for a stub)
            const auto ead = *parse_decimal(r["ead"].get<std::string>(), 2);
            results.push_back({{"recordId", id}, {"status", "ok"}, {"riskWeight", "1.000000000"},
                               {"rea", r["ead"]}, {"expectedLoss", format_decimal(ead / 100, 2)}});
        }
    }
    return {{"meta", {{"requestId", request["context"]["requestId"]}, {"calculator", {{"name", "stub"}, {"version", version}}},
                      {"paramSet", request["context"]["paramSet"]}}},
            {"results", results}};
}

// /v1/parameters/credit: every requested parameter and year, except `omit`; value = 0.0<k> for the k-th requested
// parameter (x 2 for stage2 records); records ending in "-R" are rejected.
json params_response(const json& request, const std::set<std::string>& omit = {}) {
    json results = json::array();
    for (const auto& r : request["records"]) {
        const std::string id = r["recordId"];
        if (id.size() > 2 && id.substr(id.size() - 2) == "-R") {
            results.push_back({{"recordId", id}, {"status", "rejected"}, {"errors", {{{"code", "P-1"}, {"message", "no model"}}}}});
            continue;
        }
        json values = json::array();
        const Nano factor = r.contains("stage") && r["stage"] == "stage2" ? 2 : 1;
        for (const auto& y : request["years"]) {
            Nano k = 0;
            for (const auto& p : request["parameters"]) {
                ++k;
                if (omit.count(p.get<std::string>())) continue;
                values.push_back({{"year", y}, {"parameter", p}, {"value", format_decimal(k * factor * 10'000'000, 9)},
                                  {"source", "model"}});
            }
        }
        results.push_back({{"recordId", id}, {"status", "ok"}, {"values", values}});
    }
    return {{"meta", {{"requestId", request["context"]["requestId"]}, {"calculator", {{"name", "stub"}, {"version", "1.0"}}},
                      {"paramSet", request["context"]["paramSet"]}}},
            {"results", results}};
}

}  // namespace

TEST_CASE("IRB requests are canonical JSON with decimal strings") {
    const std::vector<IrbRecord> recs = {record("A"), record("B", 5)};
    const auto body = encode_irb_request(context(), recs, 0, 2, "key-1");
    CHECK(body == encode_irb_request(context(), recs, 0, 2, "key-1"));
    const auto j = json::parse(body);
    CHECK(j["context"]["requestId"] == "key-1");
    CHECK(j["context"]["scenario"] == "adverse");
    CHECK(j["context"]["projectionYear"] == 2);
    CHECK(j["context"]["inputFingerprint"].get<std::string>().rfind("sha256:", 0) == 0);
    const auto& r = j["records"][0];
    CHECK(r["recordId"] == "A");
    CHECK(r["ead"] == "1234.56");
    CHECK(r["pd"] == "0.012345679");
    CHECK(r["lgd"] == "0.450000000");
    CHECK(r["maturityYears"] == "3.1234");
    CHECK(r["annualTurnoverEurMillions"] == "12.50000000");
    CHECK(r["countryOfRisk"] == "BE");
    CHECK_FALSE(r.contains("isDefaulted"));
    CHECK(j["records"][1]["ead"] == "0.05");
    for (const auto& rec : j["records"])
        for (const auto& [k, v] : rec.items()) CHECK_FALSE(v.is_number());   // no JSON numbers in records
    // Without a requestId (the form hashed into the idempotency key), and a slice.
    CHECK_FALSE(json::parse(encode_irb_request(context(), recs, 0, 2, ""))["context"].contains("requestId"));
    CHECK(json::parse(encode_irb_request(context(), recs, 1, 1, "k"))["records"].size() == 1);
    // A defaulted record carries isDefaulted and elbe.
    auto d = record("D");
    d.defaulted = true;
    d.elbe = 350'000'000;
    const auto jd = json::parse(encode_irb_request(context(), {d}, 0, 1, "k"))["records"][0];
    CHECK(jd["isDefaulted"] == true);
    CHECK(jd["elbe"] == "0.350000000");
}

TEST_CASE("IRB responses are validated against the request") {
    const std::vector<IrbRecord> recs = {record("A", 100000), record("B-R"), record("C", 250)};
    const auto req = json::parse(encode_irb_request(context(), recs, 0, 3, "key-1"));
    const auto good = ok_response(req);
    const auto res = decode_irb_response(good.dump(), recs, 0, 3, "key-1", caps(), "EU_CRR3_2025-01-01");
    REQUIRE(res.size() == 3);
    CHECK(res[0].ok);
    CHECK(res[0].rea == 100000);
    CHECK(res[0].expected_loss == 1000);
    CHECK(res[0].risk_weight == 1'000'000'000);
    CHECK_FALSE(res[1].ok);
    REQUIRE(res[1].errors.size() == 1);
    CHECK(res[1].errors[0].code == "X-1");
    CHECK(res[2].rea == 250);

    const auto rejects = [&](json bad) {
        CHECK_THROWS_AS(decode_irb_response(bad.dump(), recs, 0, 3, "key-1", caps(), "EU_CRR3_2025-01-01"), CalculatorError);
    };
    json b = good;
    b["results"].erase(1);
    rejects(b);                                              // one result per record
    b = good;
    std::swap(b["results"][0], b["results"][2]);
    rejects(b);                                              // recordIds in order
    b = good;
    b["results"][0]["status"] = "error";
    rejects(b);
    b = good;
    b["results"][0]["rea"] = 1000.0;
    rejects(b);                                              // JSON number instead of a decimal string
    b = good;
    b["results"][0]["rea"] = "1.5e3";
    rejects(b);
    b = good;
    b["results"][0]["expectedLoss"] = "-1.00";
    rejects(b);
    b = good;
    b["results"][0].erase("rea");
    rejects(b);
    b = good;
    b["results"][0]["pdApplied"] = "1.5";
    rejects(b);
    b = good;
    b["meta"]["requestId"] = "other";
    rejects(b);
    rejects(ok_response(req, "2.0"));                        // calculator version changed
    CHECK_THROWS_AS(decode_irb_response("{not json", recs, 0, 3, "key-1", caps(), "EU_CRR3_2025-01-01"), CalculatorError);
}

TEST_CASE("capabilities are parsed and checked") {
    const auto c = parse_capabilities(R"({"calculator":{"name":"x","version":"1"},"apiVersion":"0.1.0","calculations":["irb"],
        "paramSets":["P"],"limits":{"maxRecordsPerRequest":100,"maxRecordsPerSyncRequest":500}})");
    CHECK(c.max_records == 100);
    CHECK(c.max_sync_records == 100);   // capped by maxRecordsPerRequest
    CHECK(c.max_concurrent == 1);
    CHECK_THROWS_AS(parse_capabilities(R"({"calculator":{"name":"x","version":"1"}})"), CalculatorError);
}

namespace {

// In-memory calculator: the contract's happy path plus injected faults. Shared by all transports of a client.
struct Stub {
    std::mutex mu;
    std::vector<std::pair<std::string, std::string>> calls;   // (path, idempotency key)
    std::set<std::string> faulted;
    int fail_first = 0;          // first N attempts per key: 503 (odd) or connection error (even)
    bool down = false;
    int caps_fail = 0;           // the next N GET /v1/capabilities fail
    std::string param_set = "EU_CRR3_2025-01-01";
    std::vector<std::string> calculations = {"irb", "parameters-credit"};
    std::set<std::string> omit;                // parameters never returned by /v1/parameters/credit
    std::map<std::string, std::string> jobs;   // job id -> result body
    std::map<std::string, int> polls;

    Response handle(const std::string& method, const std::string& path, const std::string& body, const Headers& headers) {
        std::lock_guard lock(mu);
        Response r;
        if (down) { r.error = "Connection"; return r; }
        std::string key;
        for (const auto& [k, v] : headers) if (k == "Idempotency-Key") key = v;
        calls.emplace_back(path, key);
        if (method == "GET" && path == "/v1/capabilities") {
            if (caps_fail > 0) {   // transient: connection error, then 503
                if (caps_fail-- % 2) r.error = "Connection"; else r.status = 503;
                return r;
            }
            r.status = 200;
            r.body = json{{"calculator", {{"name", "stub"}, {"version", "1.0"}}}, {"apiVersion", "0.1.0"},
                          {"calculations", calculations}, {"paramSets", {param_set}},
                          {"limits", {{"maxRecordsPerRequest", 10}, {"maxRecordsPerSyncRequest", 3}, {"maxConcurrentRequests", 4}}}}.dump();
            return r;
        }
        if (method == "POST") {
            const int seen = static_cast<int>(std::count_if(calls.begin(), calls.end(), [&](const auto& c) { return c.second == key; }));
            if (seen <= fail_first) {
                if (seen % 2) { r.status = 503; r.headers["retry-after"] = "0"; } else { r.error = "Read"; }
                return r;
            }
            const auto j = json::parse(body);
            if (path == "/v1/credit-risk/irb") {
                r.status = 200;
                r.body = ok_response(j).dump();
            } else if (path == "/v1/parameters/credit") {
                r.status = 200;
                r.body = params_response(j, omit).dump();
            } else if (path == "/v1/jobs") {
                const std::string id = "job-" + key;
                jobs[id] = (j["calculation"] == "irb" ? ok_response(j["request"]) : params_response(j["request"], omit)).dump();
                r.status = 202;
                r.body = json{{"jobId", id}, {"status", "queued"}}.dump();
            }
            return r;
        }
        if (path.rfind("/v1/jobs/", 0) == 0) {
            const bool result = path.size() > 7 && path.substr(path.size() - 7) == "/result";
            const std::string id = path.substr(9, result ? path.size() - 16 : std::string::npos);
            r.status = 200;
            if (result) r.body = jobs.at(id);
            else r.body = json{{"jobId", id}, {"status", ++polls[id] < 2 ? "running" : "succeeded"}}.dump();
        }
        return r;
    }
};

struct StubTransport : Transport {
    Stub& stub;
    explicit StubTransport(Stub& s) : stub(s) {}
    Response send(const std::string& m, const std::string& p, const std::string& b, const Headers& h) override {
        return stub.handle(m, p, b, h);
    }
};

ClientOptions fast_options() {
    ClientOptions o;
    o.url = "http://stub";
    o.run_id = "run-1";
    o.backoff_initial = 0.001;
    o.backoff_max = 0.01;
    return o;
}

std::vector<IrbCall> sample_calls(std::size_t n) {
    std::vector<IrbCall> calls(2);
    calls[0].context = context();
    calls[1].context = context();
    calls[1].context.scenario = "baseline";
    for (std::size_t i = 0; i < n; ++i) {
        calls[0].records.push_back(record("E" + std::to_string(i) + (i == 4 ? "-R" : ""), static_cast<Cents>(100 * (i + 1))));
        calls[1].records.push_back(record("E" + std::to_string(i), 7));
    }
    return calls;
}

}  // namespace

TEST_CASE("client batches by the sync limit, keeps order and uses stable keys") {
    Stub stub;
    Client client(fast_options(), [&] { return std::make_unique<StubTransport>(stub); });
    client.connect("irb");
    const auto calls = sample_calls(10);
    const auto res = client.irb(calls);
    REQUIRE(res.size() == 2);
    REQUIRE(res[0].size() == 10);
    for (std::size_t i = 0; i < 10; ++i) {
        CHECK(res[0][i].record_id == calls[0].records[i].record_id);
        CHECK(res[0][i].ok == (i != 4));
        if (i != 4) CHECK(res[0][i].rea == static_cast<Cents>(100 * (i + 1)));
    }
    CHECK(client.stats().batches == 8);   // 10 records, sync limit 3: 3+3+2+2 per call
    std::set<std::string> keys;
    for (const auto& [path, key] : stub.calls) if (path == "/v1/credit-risk/irb") keys.insert(key);
    CHECK(keys.size() == 8);
    // A rerun sends the same idempotency keys.
    Stub stub2;
    Client again(fast_options(), [&] { return std::make_unique<StubTransport>(stub2); });
    again.connect("irb");
    again.irb(calls);
    std::set<std::string> keys2;
    for (const auto& [path, key] : stub2.calls) if (path == "/v1/credit-risk/irb") keys2.insert(key);
    CHECK(keys == keys2);
}

TEST_CASE("client retries 503 and connection errors with the same key") {
    Stub stub;
    stub.fail_first = 2;   // 503, then a connection error, then success
    Client client(fast_options(), [&] { return std::make_unique<StubTransport>(stub); });
    client.connect("irb");
    const auto res = client.irb(sample_calls(3));
    CHECK(res[0][0].ok);
    CHECK(client.stats().retries == 4);   // 2 batches x 2 retries
    std::map<std::string, int> per_key;
    for (const auto& [path, key] : stub.calls) if (!key.empty()) ++per_key[key];
    CHECK(per_key.size() == 2);
    for (const auto& [k, n] : per_key) CHECK(n == 3);

    Stub hopeless;
    hopeless.fail_first = 100;
    auto opt = fast_options();
    opt.max_attempts = 3;
    Client c2(opt, [&] { return std::make_unique<StubTransport>(hopeless); });
    c2.connect("irb");
    CHECK_THROWS_AS(c2.irb(sample_calls(3)), CalculatorError);
}

TEST_CASE("client uses jobs for batches above the sync limit") {
    Stub stub;
    auto opt = fast_options();
    opt.max_batch = 10;
    Client client(opt, [&] { return std::make_unique<StubTransport>(stub); });
    client.connect("irb");
    const auto calls = sample_calls(8);
    const auto res = client.irb(calls);
    CHECK(client.stats().jobs == 2);
    CHECK(res[1][7].ok);
    CHECK(res[1][7].rea == 7);
    CHECK(std::count_if(stub.calls.begin(), stub.calls.end(), [](const auto& c) { return c.first == "/v1/credit-risk/irb"; }) == 0);
}

TEST_CASE("client fails fast on unsupported capabilities") {
    Stub stub;
    stub.param_set = "OTHER";
    Client client(fast_options(), [&] { return std::make_unique<StubTransport>(stub); });
    CHECK_THROWS_AS(client.connect("irb"), CalculatorError);
    Stub ok;
    Client c2(fast_options(), [&] { return std::make_unique<StubTransport>(ok); });
    CHECK_THROWS_AS(c2.connect("sa"), CalculatorError);
    Client c3(fast_options(), [&] { return std::make_unique<StubTransport>(ok); });
    CHECK_THROWS_AS(c3.irb(sample_calls(1)), CalculatorError);   // connect() first
}

TEST_CASE("replay cache: a rerun needs no calculator") {
    const fs::path dir = fs::temp_directory_path() / ("sora_calc_cache_" + std::to_string(std::random_device{}()));
    fs::remove_all(dir);
    auto opt = fast_options();
    opt.cache_dir = dir;
    const auto calls = sample_calls(7);
    Stub stub;
    std::vector<std::vector<IrbResult>> first;
    {
        Client client(opt, [&] { return std::make_unique<StubTransport>(stub); });
        client.connect("irb");
        first = client.irb(calls);
    }
    stub.down = true;
    Client offline(opt, [&] { return std::make_unique<StubTransport>(stub); });
    offline.connect("irb");
    CHECK(offline.stats().offline);
    const auto again = offline.irb(calls);
    CHECK(offline.stats().cache_hits == offline.stats().batches);
    for (std::size_t c = 0; c < calls.size(); ++c)
        for (std::size_t i = 0; i < calls[c].records.size(); ++i) {
            CHECK(again[c][i].rea == first[c][i].rea);
            CHECK(again[c][i].ok == first[c][i].ok);
        }
    // Different inputs are a cache miss, and offline that is an error.
    auto changed = calls;
    changed[0].records[0].ead += 1;
    CHECK_THROWS_AS(offline.irb(changed), CalculatorError);
    fs::remove_all(dir);
}

TEST_CASE("IRB exposure classes") {
    Exposure e;
    Counterparty cp;
    cp.sector = EbaSector::CentralBank;
    CHECK(irb_exposure_class(e, cp) == "central_governments");
    cp.sector = EbaSector::GeneralGovernment;
    CHECK(irb_exposure_class(e, cp) == "central_governments");
    cp.sector = EbaSector::CreditInstitution;
    CHECK(irb_exposure_class(e, cp) == "institutions");
    cp.sector = EbaSector::OtherFinancial;
    CHECK(irb_exposure_class(e, cp) == "corporates_general");
    cp.sector = EbaSector::NonFinancialCorporation;
    cp.is_sme = Flag::True;
    CHECK(irb_exposure_class(e, cp) == "corporates_sme");
    cp.is_sme = Flag::Unknown;
    CHECK(irb_exposure_class(e, cp) == "corporates_general");
    cp.sector = EbaSector::Household;
    e.purpose = HouseholdPurpose::HousePurchase;
    CHECK(irb_exposure_class(e, cp) == "retail_residential_mortgage");
    e.type = ExposureType::DebtSecurity;
    CHECK(irb_exposure_class(e, cp) == "retail_other");
    e.type = ExposureType::Loan;
    e.purpose = HouseholdPurpose::Consumption;
    CHECK(irb_exposure_class(e, cp) == "retail_other");
}

TEST_CASE("JSON strings are escaped for every control character") {
    CHECK(json_escape("a\"b\\c") == "a\\\"b\\\\c");
    CHECK(json_escape("line\nnext\ttab\x01" "end") == "line\\nnext\\ttab\\u0001end");
    CHECK(json_quote("\b\f\r\x1f") == "\"\\b\\f\\r\\u001f\"");
    CHECK(json_escape("caf\xc3\xa9 \x7f") == "caf\xc3\xa9 \x7f");   // UTF-8 and DEL unchanged
    // The same bytes as nlohmann/json for every ASCII character, and valid JSON that parses back.
    std::string all(1, '\0');
    for (int c = 1; c < 128; ++c) all += static_cast<char>(c);
    CHECK(json_quote(all) == json(all).dump());
    CHECK(json::parse(json_quote(all)).get<std::string>() == all);
}

namespace {

// The request encoding before the single-pass encoder (nlohmann objects). The replay cache file names and the
// idempotency keys depend on these exact bytes.
std::string nlohmann_irb_request(const RequestContext& ctx, const std::vector<IrbRecord>& records, const std::string& request_id) {
    json recs = json::array();
    for (const auto& r : records) {
        json o = {{"recordId", r.record_id}, {"exposureClass", r.exposure_class}, {"approach", r.approach},
                  {"ead", format_decimal(r.ead, 2)}, {"pd", format_decimal(r.pd, 9)}, {"lgd", format_decimal(r.lgd, 9)},
                  {"maturityYears", format_decimal(r.maturity_bp, 4)}};
        if (r.defaulted) o["isDefaulted"] = true;
        if (r.elbe) o["elbe"] = format_decimal(*r.elbe, 9);
        if (r.turnover_eur) o["annualTurnoverEurMillions"] = format_decimal(*r.turnover_eur, 8);
        if (r.large_financial_entity) o["isLargeFinancialSectorEntity"] = true;
        if (!r.currency.empty()) o["currency"] = r.currency;
        if (!r.country.empty()) o["countryOfRisk"] = r.country;
        recs.push_back(std::move(o));
    }
    json c = {{"runId", ctx.run_id}, {"scenario", ctx.scenario}, {"projectionYear", ctx.year},
              {"referenceDate", ctx.reference_date}, {"paramSet", ctx.param_set}, {"reportingCurrency", ctx.currency},
              {"inputFingerprint", "sha256:" + sha256_hex(recs.dump())}};
    if (!request_id.empty()) c["requestId"] = request_id;
    return json{{"context", std::move(c)}, {"records", std::move(recs)}}.dump();
}

}  // namespace

TEST_CASE("IRB requests are byte-identical to the nlohmann encoding, encoded once") {
    std::vector<IrbRecord> recs = {record("A"), record("B\"q\\\n\x02", 5), record("C")};
    recs[1].defaulted = true;
    recs[1].elbe = 350'000'000;
    recs[1].large_financial_entity = true;
    recs[1].currency = "USD";
    recs[1].turnover_eur.reset();
    recs[2].country.clear();
    recs[2].exposure_class = "retail_other";
    auto ctx = context();
    ctx.run_id = "sora|scen\t\"x\"|2026-06-30|rel";
    for (const std::string id : {"", "key-1"}) CHECK(encode_irb_request(ctx, recs, 0, 3, id) == nlohmann_irb_request(ctx, recs, id));
    CHECK(encode_irb_request(ctx, recs, 1, 2, "k") == nlohmann_irb_request(ctx, {recs[1], recs[2]}, "k"));
    CHECK(encode_irb_request(ctx, recs, 0, 0, "") == nlohmann_irb_request(ctx, {}, ""));
    // make_irb_request: the key of the body without requestId, and that body with the key inserted.
    const auto req = make_irb_request(ctx, recs, 0, 3);
    CHECK(req.key == idempotency_key(ctx.run_id, "irb", encode_irb_request(ctx, recs, 0, 3, "")));
    CHECK(req.body == nlohmann_irb_request(ctx, recs, req.key));
    CHECK_THROWS_AS(encode_irb_request(ctx, recs, 2, 2, ""), CalculatorError);
}

TEST_CASE("IRB responses: results beyond the records or not objects are rejected") {
    const std::vector<IrbRecord> recs = {record("A", 100000), record("C", 250)};
    const auto req = json::parse(encode_irb_request(context(), recs, 0, 2, "key-1"));
    auto more = ok_response(req);
    more["results"].push_back(more["results"][0]);
    CHECK_THROWS_AS(decode_irb_response(more.dump(), recs, 0, 2, "key-1", caps(), "EU_CRR3_2025-01-01"), CalculatorError);
    auto scalar = ok_response(req);
    scalar["results"][1] = "ok";
    CHECK_THROWS_AS(decode_irb_response(scalar.dump(), recs, 0, 2, "key-1", caps(), "EU_CRR3_2025-01-01"), CalculatorError);
    // Members in any order (meta after results), and unknown members, are fine.
    const auto good = ok_response(req);
    const std::string reordered = R"({"extra":{"results":[{"a":1}]},"results":)" + good["results"].dump() + R"(,"meta":)" +
                                  good["meta"].dump() + "}";
    const auto res = decode_irb_response(reordered, recs, 0, 2, "key-1", caps(), "EU_CRR3_2025-01-01");
    REQUIRE(res.size() == 2);
    CHECK(res[1].rea == 250);
}

TEST_CASE("a bearer token is never sent over plain http to another host") {
    for (const char* h : {"localhost", "LOCALHOST:8080", "127.0.0.1", "127.8.9.10:80", "[::1]:8080", "::1", "[::ffff:127.0.0.1]"})
        CHECK_MESSAGE(is_loopback_host(h), h);
    for (const char* h : {"calc.bank.internal", "10.0.0.1:8080", "128.0.0.1", "[::2]", "localhost.evil.com", "127.0.0.1.nip.io", ""})
        CHECK_FALSE_MESSAGE(is_loopback_host(h), h);

    ClientOptions o;
    o.token_env = "SORA_TEST_CALCULATOR_TOKEN";
    set_env(o.token_env, "secret");
    o.url = "http://calc.bank.internal:8080";
    CHECK_THROWS_WITH_AS(http_transport(o), doctest::Contains("refusing to send the bearer token"), CalculatorError);
    o.url = "http://127.0.0.1:8080";
    CHECK_NOTHROW(http_transport(o));
    o.url = "http://[::1]:8080/prefix";
    CHECK_NOTHROW(http_transport(o));
    unset_env(o.token_env);
    o.url = "http://calc.bank.internal:8080";   // no token: plain http is allowed (the stub)
    CHECK_NOTHROW(http_transport(o));
}

TEST_CASE("capabilities are retried before falling back to the replay cache") {
    const fs::path dir = fs::temp_directory_path() / ("sora_calc_caps_" + std::to_string(std::random_device{}()));
    fs::remove_all(dir);
    auto opt = fast_options();
    opt.cache_dir = dir;
    Stub stub;
    {
        Client c(opt, [&] { return std::make_unique<StubTransport>(stub); });
        c.connect("irb");   // fills the capabilities cache
        CHECK_FALSE(c.stats().offline);
    }
    // A transient failure (connection error, then 503) is retried: the run stays online despite the cache.
    stub.caps_fail = 2;
    Client online(opt, [&] { return std::make_unique<StubTransport>(stub); });
    online.connect("irb");
    CHECK_FALSE(online.stats().offline);
    CHECK(online.stats().retries == 2);
    // Every attempt fails: offline from the cache, with the reason.
    stub.caps_fail = 1000;
    opt.max_attempts = 4;
    Client offline(opt, [&] { return std::make_unique<StubTransport>(stub); });
    const std::size_t before = stub.calls.size();
    offline.connect("irb");
    CHECK(stub.calls.size() - before == 4);
    CHECK(offline.stats().offline);
    CHECK(offline.stats().offline_reason.find("after 4 attempts") != std::string::npos);
    // No cache: the error after all attempts.
    opt.cache_dir.clear();
    Client failing(opt, [&] { return std::make_unique<StubTransport>(stub); });
    CHECK_THROWS_WITH_AS(failing.connect("irb"), doctest::Contains("after 4 attempts"), CalculatorError);
    fs::remove_all(dir);
}

TEST_CASE("replay cache write failures are counted, not fatal") {
    const fs::path file = fs::temp_directory_path() / ("sora_calc_file_" + std::to_string(std::random_device{}()));
    std::ofstream(file) << "not a directory";
    auto opt = fast_options();
    opt.cache_dir = file / "cache";   // below a regular file: nothing can be written
    Stub stub;
    Client client(opt, [&] { return std::make_unique<StubTransport>(stub); });
    client.connect("irb");
    const auto res = client.irb(sample_calls(7));
    CHECK(res[0][0].ok);
    CHECK(res[1][6].rea == 7);
    const auto st = client.stats();
    CHECK(st.cache_write_failures == st.batches + 1);   // every batch and the capabilities
    CHECK_FALSE(st.cache_write_error.empty());
    fs::remove(file);
}

TEST_CASE("streaming: records are produced per batch, results delivered in order within a bounded window") {
    Stub stub;
    const auto opt = fast_options();
    Client client(opt, [&] { return std::make_unique<StubTransport>(stub); });
    client.connect("irb");   // sync limit 3, 4 concurrent requests: a window of 8 batches
    const auto calls = sample_calls(50);
    IrbStream s;
    for (const auto& c : calls) {
        s.contexts.push_back(c.context);
        s.counts.push_back(c.records.size());
    }
    std::mutex mu;
    std::size_t filled = 0, sunk = 0, max_open = 0;
    s.fill = [&](std::size_t call, std::size_t first, std::size_t count, std::vector<IrbRecord>& out) {
        for (std::size_t i = first; i < first + count; ++i) out.push_back(calls[call].records[i]);
        std::lock_guard lock(mu);
        max_open = std::max(max_open, ++filled - sunk);
    };
    std::vector<std::pair<std::size_t, std::size_t>> order;
    std::vector<std::string> ids;
    s.sink = [&](std::size_t call, std::size_t first, std::vector<IrbResult>& results) {
        order.emplace_back(call, first);
        for (const auto& r : results) ids.push_back(r.record_id);
        std::lock_guard lock(mu);
        ++sunk;
    };
    client.irb(s);
    CHECK(sunk == client.stats().batches);
    CHECK(std::is_sorted(order.begin(), order.end()));
    std::vector<std::string> expected;
    for (const auto& c : calls)
        for (const auto& r : c.records) expected.push_back(r.record_id);
    CHECK(ids == expected);
    CHECK(max_open <= 8);
    // The same requests as the all-in-memory call.
    Stub stub2;
    Client again(opt, [&] { return std::make_unique<StubTransport>(stub2); });
    again.connect("irb");
    again.irb(calls);
    std::multiset<std::string> k1, k2;
    for (const auto& [p, k] : stub.calls) if (!k.empty()) k1.insert(k);
    for (const auto& [p, k] : stub2.calls) if (!k.empty()) k2.insert(k);
    CHECK(k1 == k2);
    // A failure in one batch stops the others and is rethrown.
    const auto fill = s.fill;
    s.fill = [&](std::size_t call, std::size_t first, std::size_t count, std::vector<IrbRecord>& out) {
        if (call == 1 && first > 0) throw Error("fill failed");
        fill(call, first, count, out);
    };
    CHECK_THROWS_WITH(client.irb(s), "fill failed");
}

// ============================================================================================== credit parameters

namespace {

ParameterRecord param_record(const std::string& id, const std::string& stage = "stage1") {
    ParameterRecord r;
    r.record_id = id;
    r.segment = "LOANS|NFC_SME|BE";
    r.stage = stage;
    using Kind = ParameterAttribute::Kind;
    r.attributes = {{"is_sme", Kind::Boolean, "true"}, {"eba_sector", Kind::String, "non_financial_corporation"},
                    {"rating", Kind::Integer, "7"}};
    return r;
}

RequestContext param_context() {
    auto c = context();
    c.scenario = "actual";
    c.year = 0;
    return c;
}

ParameterSpec spec(std::vector<std::string> p = {"pd12m_s1", "lgd_s1", "ccf"}) {
    ParameterSpec s;
    s.parameters = std::move(p);
    return s;
}

}  // namespace

TEST_CASE("parameter requests are canonical JSON, keyed like IRB requests") {
    const std::vector<ParameterRecord> recs = {param_record("A"), param_record("B", "stage2")};
    const auto body = encode_parameter_request(param_context(), spec(), recs, 0, 2, "key-1");
    CHECK(body == encode_parameter_request(param_context(), spec(), recs, 0, 2, "key-1"));
    const auto j = json::parse(body);
    CHECK(j.dump() == body);   // sorted keys, no whitespace
    CHECK(j["context"]["requestId"] == "key-1");
    CHECK(j["context"]["scenario"] == "actual");
    CHECK(j["context"]["projectionYear"] == 0);
    CHECK(j["context"]["inputFingerprint"] == "sha256:" + sha256_hex(j["records"].dump()));
    CHECK(j["parameters"] == json({"pd12m_s1", "lgd_s1", "ccf"}));
    CHECK(j["years"] == json({0}));
    const auto& r = j["records"][1];
    CHECK(r["recordId"] == "B");
    CHECK(r["level"] == "exposure");
    CHECK(r["segment"] == "LOANS|NFC_SME|BE");
    CHECK(r["stage"] == "stage2");
    CHECK(r["attributes"]["is_sme"] == true);
    CHECK(r["attributes"]["rating"] == 7);
    CHECK(r["attributes"]["eba_sector"] == "non_financial_corporation");
    auto bare = param_record("C");
    bare.segment.clear();
    bare.stage.clear();
    bare.attributes.clear();
    const auto jb = json::parse(encode_parameter_request(param_context(), spec(), {bare}, 0, 1, ""));
    CHECK_FALSE(jb["context"].contains("requestId"));
    CHECK(jb["records"][0] == json({{"level", "exposure"}, {"recordId", "C"}}));
    const auto req = make_parameter_request(param_context(), spec(), recs, 0, 2);
    CHECK(req.key == idempotency_key("run-1", "parameters-credit", encode_parameter_request(param_context(), spec(), recs, 0, 2, "")));
    CHECK(req.body == encode_parameter_request(param_context(), spec(), recs, 0, 2, req.key));
    CHECK(req.key != make_parameter_request(param_context(), spec({"pd12m_s1"}), recs, 0, 2).key);
    CHECK_THROWS_AS(encode_parameter_request(param_context(), spec(), recs, 1, 2, ""), CalculatorError);
}

TEST_CASE("parameter responses are validated against the request") {
    const std::vector<ParameterRecord> recs = {param_record("A"), param_record("B-R"), param_record("C", "stage2")};
    const auto sp = spec();
    const auto req = json::parse(encode_parameter_request(param_context(), sp, recs, 0, 3, "key-1"));
    const auto good = params_response(req);
    const auto decode = [&](const json& j) {
        return decode_parameter_response(j.dump(), sp, recs, 0, 3, "key-1", caps(), "EU_CRR3_2025-01-01");
    };
    const auto res = decode(good);
    REQUIRE(res.size() == 3);
    CHECK(res[0].ok);
    REQUIRE(res[0].values.size() == 3);
    CHECK(res[0].values[0].parameter == "pd12m_s1");
    CHECK(res[0].values[0].value == 10'000'000);
    CHECK(res[0].values[2].value == 30'000'000);
    CHECK(res[0].values[0].source == "model");
    CHECK_FALSE(res[1].ok);
    CHECK(res[1].errors.at(0).code == "P-1");
    CHECK(res[2].values[1].value == 40'000'000);
    // Values left out are allowed (reported as missing by the caller, never filled in).
    auto partial = good;
    partial["results"][0]["values"].erase(1);
    partial["results"][2].erase("values");
    const auto pr = decode(partial);
    CHECK(pr[0].values.size() == 2);
    CHECK(pr[2].ok);
    CHECK(pr[2].values.empty());

    const auto rejects = [&](const json& bad) { CHECK_THROWS_AS(decode(bad), CalculatorError); };
    json b = good;
    b["results"][0]["values"][0]["year"] = 1;
    rejects(b);                                              // year not requested
    b = good;
    b["results"][0]["values"][0]["parameter"] = "lgd_s2";
    rejects(b);                                              // parameter not requested
    b = good;
    b["results"][0]["values"][1] = b["results"][0]["values"][0];
    rejects(b);                                              // returned twice
    b = good;
    b["results"][0]["values"][0]["value"] = "1.000000001";
    rejects(b);                                              // outside [0, 1]
    b = good;
    b["results"][0]["values"][0]["value"] = 0.01;
    rejects(b);                                              // JSON number
    b = good;
    b["results"][0]["values"][0]["source"] = "guess";
    rejects(b);
    b = good;
    b["results"][0]["values"] = "none";
    rejects(b);
    b = good;
    b["results"].erase(2);
    rejects(b);                                              // one result per record
    b = good;
    std::swap(b["results"][0], b["results"][2]);
    rejects(b);                                              // in order
    b = good;
    b["meta"]["calculator"]["version"] = "2.0";
    rejects(b);
    b = good;
    b["meta"]["paramSet"] = "OTHER";
    rejects(b);
}

TEST_CASE("client: parameters are batched, retried, sent as jobs and checked against the capabilities") {
    std::vector<ParameterRecord> recs;
    for (int i = 0; i < 10; ++i) recs.push_back(param_record("E" + std::to_string(i) + (i == 4 ? "-R" : ""), i % 2 ? "stage2" : "stage1"));
    const auto sp = spec();
    Stub stub;
    stub.fail_first = 1;   // one 503 per key
    {
        Client client(fast_options(), [&] { return std::make_unique<StubTransport>(stub); });
        client.connect("parameters-credit");
        const auto res = client.parameters(sp, param_context(), recs);
        REQUIRE(res.size() == 10);
        for (std::size_t i = 0; i < 10; ++i) {
            CHECK(res[i].record_id == recs[i].record_id);
            CHECK(res[i].ok == (i != 4));
        }
        CHECK(res[3].values[0].value == 20'000'000);   // stage2
        CHECK(client.stats().batches == 4);             // sync limit 3: 3+3+2+2
        CHECK(client.stats().retries == 4);
        std::set<std::string> keys;
        for (const auto& [path, key] : stub.calls) if (path == "/v1/parameters/credit") keys.insert(key);
        CHECK(keys.size() == 4);
    }
    // Batches above the sync limit go through /v1/jobs with calculation parameters-credit.
    Stub jobs;
    auto opt = fast_options();
    opt.max_batch = 10;
    Client jc(opt, [&] { return std::make_unique<StubTransport>(jobs); });
    jc.connect("parameters-credit");
    const auto jres = jc.parameters(sp, param_context(), recs);
    CHECK(jc.stats().jobs == 1);
    CHECK(jres[9].values[2].value == 60'000'000);
    // A calculator without parameters-credit fails fast.
    Stub no_params;
    no_params.calculations = {"irb"};
    Client nc(fast_options(), [&] { return std::make_unique<StubTransport>(no_params); });
    CHECK_THROWS_WITH_AS(nc.connect("parameters-credit"), doctest::Contains("does not support the calculation parameters-credit"),
                         CalculatorError);
    Client irb_only(fast_options(), [&] { return std::make_unique<StubTransport>(no_params); });
    irb_only.connect("irb");
    CHECK_THROWS_AS(irb_only.parameters(sp, param_context(), recs), CalculatorError);
    CHECK_THROWS_AS(jc.parameters(spec({}), param_context(), recs), CalculatorError);   // nothing requested
}

TEST_CASE("client: parameter responses replay from the cache when the calculator is down") {
    const fs::path dir = fs::temp_directory_path() / ("sora_calc_params_" + std::to_string(std::random_device{}()));
    fs::remove_all(dir);
    auto opt = fast_options();
    opt.cache_dir = dir;
    opt.max_attempts = 2;
    std::vector<ParameterRecord> recs;
    for (int i = 0; i < 7; ++i) recs.push_back(param_record("E" + std::to_string(i)));
    Stub stub;
    std::vector<ParameterResult> first;
    {
        Client c(opt, [&] { return std::make_unique<StubTransport>(stub); });
        c.connect("parameters-credit");
        first = c.parameters(spec(), param_context(), recs);
    }
    CHECK(fs::is_directory(dir / "stub_1.0" / "parameters-credit"));
    stub.down = true;
    Client offline(opt, [&] { return std::make_unique<StubTransport>(stub); });
    offline.connect("parameters-credit");
    CHECK(offline.stats().offline);
    const auto again = offline.parameters(spec(), param_context(), recs);
    CHECK(offline.stats().cache_hits == offline.stats().batches);
    for (std::size_t i = 0; i < recs.size(); ++i) {
        REQUIRE(again[i].values.size() == first[i].values.size());
        for (std::size_t k = 0; k < again[i].values.size(); ++k) CHECK(again[i].values[k].value == first[i].values[k].value);
    }
    // Another parameter list is another request: a cache miss, an error offline.
    CHECK_THROWS_WITH_AS(offline.parameters(spec({"pd12m_s1"}), param_context(), recs), doctest::Contains("not in the replay cache"),
                         CalculatorError);
    fs::remove_all(dir);
}

TEST_CASE("calculator parameter lists") {
    CHECK(parse_calculator_parameters("pd12m_s1, lgd_s1,ccf") == std::vector<std::string>{"pd12m_s1", "lgd_s1", "ccf"});
    const auto all = parse_calculator_parameters("all");
    REQUIRE(all.size() == kCalculatorParamCount);
    CHECK(all.front() == "pd12m_s1");
    CHECK(all.back() == "lgd_reg");
    CHECK_THROWS_WITH_AS(parse_calculator_parameters("pd12m_s1,pd_lifetime"), doctest::Contains("unknown parameter 'pd_lifetime'"), Error);
    CHECK_THROWS_WITH_AS(parse_calculator_parameters("ccf,ccf"), doctest::Contains("given twice"), Error);
    CHECK_THROWS_AS(parse_calculator_parameters(""), Error);
    CHECK_THROWS_AS(parse_calculator_parameters("pd12m_s1,"), Error);
}

TEST_CASE("calculator values fill only the fields an exposure row does not supply") {
    ExternalParameters ext;
    CHECK(ext.empty());
    OptParams file;
    file.v[0] = 0.5;   // pd12m_s1, as if from an exposure row of the parameter file
    CHECK(ext.fill_exposure(3, file) == 1U);
    OptParams calc;
    calc.v[0] = 0.01;   // pd12m_s1: the exposure already has it
    calc.v[6] = 0.2;    // lgd_s1
    CHECK(ext.fill_exposure(3, calc) == (1U << 6));
    Params p{};
    CHECK(ext.apply_exposure(3, {0, 0}, p) == 2);
    CHECK(p.pd12m_s1 == 0.5);
    CHECK(p.lgd_s1 == 0.2);
    CHECK(ext.has_exposure(3));
    CHECK_FALSE(ext.has_exposure(4));
    CHECK(ext.fill_exposure(4, OptParams{}) == 0U);
    CHECK_FALSE(ext.has_exposure(4));
    ExternalParameters extras_only;
    extras_only.set_exposure_extras(7, {0.4, std::nullopt, std::nullopt});
    CHECK_FALSE(extras_only.empty());
    CHECK(extras_only.exposure_extras().at(7).ccf == 0.4);
}
