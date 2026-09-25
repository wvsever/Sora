// Calculator client: decimals, hashing, JSON encoding/decoding, response validation, and the client's
// batching, retries, jobs and replay cache against an in-memory stub (no network).

#include "doctest.h"

#include <json.hpp>

#include <algorithm>
#include <filesystem>
#include <map>
#include <mutex>
#include <random>
#include <set>

#include "sora/calculator.hpp"
#include "sora/rea.hpp"

using namespace sora;
using namespace sora::calc;
using json = nlohmann::json;
namespace fs = std::filesystem;

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
    std::string param_set = "EU_CRR3_2025-01-01";
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
            r.status = 200;
            r.body = json{{"calculator", {{"name", "stub"}, {"version", "1.0"}}}, {"apiVersion", "0.1.0"},
                          {"calculations", {"irb"}}, {"paramSets", {param_set}},
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
            } else if (path == "/v1/jobs") {
                const std::string id = "job-" + key;
                jobs[id] = ok_response(j["request"]).dump();
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
