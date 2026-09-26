#include "doctest.h"

#include <cstring>
#include <filesystem>
#include <fstream>

#include "sora/benchmark.hpp"
#include "sora/duck.hpp"
#include "sora/projection.hpp"

using namespace sora;
namespace fs = std::filesystem;

namespace {

fs::path temp_file(const std::string& name, const std::string& text) {
    const auto p = fs::temp_directory_path() / name;
    std::ofstream(p, std::ios::binary) << text;
    return p;
}

// Two pivot asset classes in three countries: LOANS|HH_HOUSE (BE 5, DE 45, FR 50) and LOANS|GG (10 each), one
// stage-1 exposure per segment (amounts in EUR).
struct Book {
    Dataset d;
    Segmentation s;
    ScenarioConfig c;
    MacroTable macro;
    std::map<std::string, Satellite> sats{{"HH_HOUSE", {-0.1, 0.1, 0, 0}}, {"GG", {-0.05, 0.05, 0, 0}}};
    Calibration cal;
};

Book book() {
    Book b;
    auto& d = b.d;
    d.currencies.intern("EUR");
    d.fx_to_reporting = {1'000'000'000};
    for (const char* c : {"BE", "DE", "FR"}) d.countries.intern(c);
    const struct { EbaSector sector; std::uint32_t country; Cents gca; } rows[] = {
        {EbaSector::Household, 0, 500}, {EbaSector::Household, 1, 4500}, {EbaSector::Household, 2, 5000},
        {EbaSector::GeneralGovernment, 0, 1000}, {EbaSector::GeneralGovernment, 1, 1000}, {EbaSector::GeneralGovernment, 2, 1000}};
    for (const auto& r : rows) {
        Counterparty cp;
        cp.country = r.country;
        cp.sector = r.sector;
        d.counterparties.push_back(cp);
        Exposure e;
        e.id = d.exposure_ids.intern("E" + std::to_string(d.exposures.size()));
        e.counterparty = static_cast<std::uint32_t>(d.counterparties.size() - 1);
        e.currency = 0;
        e.stage = Stage::S1;
        e.purpose = HouseholdPurpose::HousePurchase;
        e.has_gca = true;
        e.gca = r.gca * 100;
        d.exposures.push_back(e);
    }
    b.s = segment(d, ScopeConfig{});
    b.c.year_map = {{1, 2025}, {2, 2026}, {3, 2027}};
    b.c.history_year = 2024;
    for (const char* k : {"BE", "DE", "FR"})
        for (int y = 2025; y <= 2027; ++y) {
            b.macro.set("real_gdp", k, "baseline", y, 1.0);
            b.macro.set("real_gdp", k, "adverse", y, -2.0);
        }
    b.c.benchmark.enabled = true;
    b.c.benchmark.country_fallback = {"EU"};
    for (const auto& seg : b.s.segments) {
        Params p;
        p.pd12m_s1 = 0.01; p.pd12m_s2 = 0.1; p.tr1_2 = 0.05; p.tr2_1 = 0.2; p.tr3_1 = 0.01; p.tr3_2 = 0.02;
        p.lgd_s1 = p.lgd_s2 = p.lgd_s3 = 0.3; p.lrlt_s2 = 0.05;
        b.cal.params.push_back(p);
        const std::string own(b.s.level_keys.view(seg.levels[0]));
        b.cal.sources.push_back({own, own, own, own, own});   // everything calibrated on the segment itself
    }
    return b;
}

std::size_t seg_index(const Book& b, const std::string& key) {
    for (std::size_t i = 0; i < b.s.segments.size(); ++i)
        if (b.s.segments[i].key == key) return i;
    FAIL("no segment " << key);
    return 0;
}

// Benchmark values of one group: distinct per key, scenario and year so that the source of a value is visible.
void add(BenchmarkTable& t, const std::string& key, std::size_t group, double base) {
    for (int sc = 0; sc < 2; ++sc)
        for (int y = 1; y <= 3; ++y)
            for (std::size_t k = 0; k < 4; ++k)
                t.set(key, sc, y, kBenchmarkGroupParams[group][k], base + 0.01 * sc + 0.001 * y + 0.0001 * static_cast<double>(k));
}

BenchmarkTable table() {
    BenchmarkTable t;
    for (std::size_t g = 0; g < 2; ++g) {
        add(t, "LOANS|HH_HOUSE|BE", g, 0.10);
        add(t, "LOANS|HH_HOUSE|EU", g, 0.20);
        add(t, "LOANS|GG|BE", g, 0.30);
    }
    t.finish();
    return t;
}

}  // namespace

TEST_CASE("benchmark file: format, year mapping and validation") {
    Duck duck;
    ScenarioConfig c;
    c.year_map = {{1, 2027}, {2, 2028}, {3, 2029}};
    std::string text = "# SYNTHETIC\ninstrument,portfolio,country,scenario,year,parameter,value\n";
    for (const char* sc : {"baseline", "adverse"})
        for (int y = 2026; y <= 2029; ++y)   // 2026 is outside the horizon: ignored
            for (const char* p : {"pd12m_s1", "pd12m_s2", "tr1_2", "tr2_1"})
                text += std::string("LOANS,CI,DE,") + sc + ',' + std::to_string(y) + ',' + p + ',' + (y == 2029 ? "0.05" : "0.01") + '\n';
    BenchmarkTable t;
    t.load(duck, temp_file("sora_bmk_ok.csv", text), c);
    const auto* e = t.find("LOANS|CI|DE");
    REQUIRE(e);
    CHECK(e->has[0]);
    CHECK(!e->has[1]);                                   // the LGD/LR group is absent: allowed
    CHECK(e->values[1][2].pd12m_s1 == 0.05);             // adverse, year 3 = 2029
    CHECK(e->values[0][0].tr2_1 == 0.01);
    CHECK(t.rows() == 24);
    CHECK(!t.find("LOANS|CI|FR"));

    auto rejects = [&](const std::string& name, const std::string& body) {
        BenchmarkTable bad;
        CHECK_THROWS_AS(bad.load(duck, temp_file(name, "instrument,portfolio,country,scenario,year,parameter,value\n" + body), c), Error);
    };
    rejects("sora_bmk_incomplete.csv", "LOANS,CI,DE,baseline,2027,pd12m_s1,0.01\n");
    rejects("sora_bmk_range.csv", "LOANS,CI,DE,baseline,2027,lgd_s1,1.5\n");
    rejects("sora_bmk_param.csv", "LOANS,CI,DE,baseline,2027,tr3_1,0.1\n");
    rejects("sora_bmk_instrument.csv", "BONDS,CI,DE,baseline,2027,lgd_s1,0.1\n");
    rejects("sora_bmk_scenario.csv", "LOANS,CI,DE,actual,2027,lgd_s1,0.1\n");
    rejects("sora_bmk_dup.csv", "LOANS,CI,DE,baseline,2027,lgd_s1,0.1\nLOANS,CI,DE,baseline,2027,lgd_s1,0.2\n");
}

TEST_CASE("benchmark rule: 10% coverage per pivot asset class, segments without a model, sovereigns") {
    Book b = book();
    const auto t = table();
    const auto be = seg_index(b, "LOANS|HH_HOUSE|BE"), de = seg_index(b, "LOANS|HH_HOUSE|DE"), fr = seg_index(b, "LOANS|HH_HOUSE|FR");
    const auto gg_be = seg_index(b, "LOANS|GG|BE"), gg_de = seg_index(b, "LOANS|GG|DE");
    auto no_model = [&](std::size_t i, std::size_t part) { b.cal.sources[i][part] = "LOANS|ALL|ALL"; };

    SUBCASE("coverage below the threshold: the whole pivot asset class takes the benchmark") {
        for (auto i : {de, fr}) no_model(i, 0);   // PD/TR modelled for BE only: 5% of the exposure
        const auto r = decide_benchmarks(b.d, b.s, b.cal, b.sats, t, b.c.benchmark, nullptr);
        for (auto i : {be, de, fr}) {
            CHECK(r.decisions[i][0].rule == BenchmarkRule::Coverage);
            CHECK(r.decisions[i][0].applied());
            CHECK(r.decisions[i][1].rule == BenchmarkRule::None);   // LGD/LR: 100% modelled, per group
        }
        CHECK(r.decisions[be][0].key == "BE");
        CHECK(r.decisions[de][0].key == "EU");                      // country fallback
        const auto& pv = r.pivots[1];
        REQUIRE(pv.key == "LOANS|HH_HOUSE");
        CHECK(pv.exposure == 10000.0);
        CHECK(pv.model[0] == 500.0);
        CHECK(pv.benchmark[0] == 10000.0);
        CHECK(pv.benchmark[1] == 0.0);
    }
    SUBCASE("coverage at or above the threshold: only the segments without a model") {
        no_model(fr, 3);                          // LGD/LR modelled for BE and DE: 50%
        const auto r = decide_benchmarks(b.d, b.s, b.cal, b.sats, t, b.c.benchmark, nullptr);
        CHECK(r.decisions[fr][1].rule == BenchmarkRule::NoModel);
        CHECK(r.decisions[fr][1].key == "EU");
        CHECK(!r.decisions[be][1].applied());
        CHECK(!r.decisions[de][1].applied());
        CHECK(!r.decisions[fr][0].applied());
    }
    SUBCASE("model level: portfolio-level calibration is a model unless model_level is segment") {
        for (auto i : {be, de, fr}) b.cal.sources[i][0] = "LOANS|HH_HOUSE|ALL";
        CHECK(!decide_benchmarks(b.d, b.s, b.cal, b.sats, t, b.c.benchmark, nullptr).decisions[be][0].applied());
        b.c.benchmark.model_level = 0;
        CHECK(decide_benchmarks(b.d, b.s, b.cal, b.sats, t, b.c.benchmark, nullptr).decisions[be][0].applied());
    }
    SUBCASE("sovereigns: the country's benchmark is mandatory, other countries keep their model") {
        const auto r = decide_benchmarks(b.d, b.s, b.cal, b.sats, t, b.c.benchmark, nullptr);
        CHECK(r.decisions[gg_be][0].rule == BenchmarkRule::Sovereign);
        CHECK(r.decisions[gg_be][1].key == "BE");
        CHECK(!r.decisions[gg_de][0].applied());
        b.c.benchmark.sovereign = false;
        CHECK(!decide_benchmarks(b.d, b.s, b.cal, b.sats, t, b.c.benchmark, nullptr).decisions[gg_be][0].applied());
    }
    SUBCASE("no satellite model and no benchmark: reported unavailable, the model parameters stay") {
        b.sats.erase("GG");
        const auto r = decide_benchmarks(b.d, b.s, b.cal, b.sats, t, b.c.benchmark, nullptr);
        CHECK(r.decisions[gg_be][0].applied());                     // sovereign BE
        CHECK(r.decisions[gg_de][0].rule == BenchmarkRule::Coverage);
        CHECK(r.decisions[gg_de][0].unavailable);
        CHECK(!r.decisions[gg_de][0].applied());
    }
    SUBCASE("customer projections of a whole group within the pivot asset class count as a model") {
        for (auto i : {be, de, fr}) no_model(i, 3);
        std::string cols = "level, key, scenario, year", rows;
        for (auto n : kParamNames) cols += std::string(", ") + n;
        for (const char* sc : {"baseline", "adverse"})
            for (int y = 1; y <= 3; ++y)
                rows += std::string(rows.empty() ? "" : ", ") + "('segment', 'LOANS|HH_HOUSE|ALL', '" + sc + "', " + std::to_string(y) +
                        ", NULL, NULL, NULL, NULL, NULL, NULL, 0.2, 0.2, 0.3, 0.05)";
        Duck duck;
        ExternalParameters ext;
        ext.load(duck, "(SELECT * FROM (VALUES " + rows + ") t(" + cols + "))", b.d);
        CHECK(decide_benchmarks(b.d, b.s, b.cal, b.sats, t, b.c.benchmark, nullptr).decisions[be][1].applied());
        const auto r = decide_benchmarks(b.d, b.s, b.cal, b.sats, t, b.c.benchmark, &ext);
        CHECK(r.decisions[be][1].model);
        CHECK(!r.decisions[be][1].applied());
    }
}

TEST_CASE("project() replaces the benchmarked groups without adjustment, bit-identical for any number of workers") {
    Book b = book();
    const auto t = table();
    const auto be = seg_index(b, "LOANS|HH_HOUSE|BE"), de = seg_index(b, "LOANS|HH_HOUSE|DE");
    for (auto i : {be, de, seg_index(b, "LOANS|HH_HOUSE|FR")}) b.cal.sources[i][0] = "none";   // no PD/TR model at all

    const Projection p = project(b.d, b.s, b.cal, b.sats, b.macro, b.c, nullptr, 1, &t);
    REQUIRE(p.benchmark.enabled);
    const auto* bm = t.find("LOANS|HH_HOUSE|BE");
    for (std::size_t sc = 0; sc < 2; ++sc) {
        CHECK(std::memcmp(&p.params[be][sc][0], &b.cal.params[be], sizeof(Params)) == 0);   // starting point: own
        for (std::size_t y = 1; y <= 3; ++y) {
            const auto& got = p.params[be][sc][y];
            for (auto k : kBenchmarkGroupParams[0]) CHECK(param_field(got, k) == param_field(bm->values[sc][y - 1], k));
            CHECK(got.tr3_1 == 0.01);                                              // not benchmarked
            CHECK(p.path_source[be][sc][y - 1] == "mixed");                         // PD/TR benchmark, LGD/LR model
        }
        CHECK(std::memcmp(&p.params[be][sc][4], &p.params[be][sc][3], sizeof(Params)) == 0);
    }
    CHECK(p.params[de][1][2].pd12m_s1 == t.find("LOANS|HH_HOUSE|EU")->values[1][1].pd12m_s1);
    CHECK(p.path_source[seg_index(b, "LOANS|GG|BE")][0][0] == "benchmark");     // both groups (sovereign)

    // The projection uses the replaced paths, and does not depend on the number of workers.
    std::array<std::array<YearResult, 3>, 2> seq{};
    const auto& e = b.d.exposures[static_cast<std::size_t>(std::find(b.s.segment_of.begin(), b.s.segment_of.end(),
                                                                   static_cast<std::int32_t>(be)) - b.s.segment_of.begin())];
    project_exposure(e.stage, to_double(e.gca), 0, p.params[be], b.c, seq);
    CHECK(std::memcmp(&seq, &p.results[be], sizeof seq) == 0);
    const Projection q = project(b.d, b.s, b.cal, b.sats, b.macro, b.c, nullptr, 4, &t);
    for (std::size_t g = 0; g < p.results.size(); ++g) {
        CHECK(std::memcmp(&p.results[g], &q.results[g], sizeof p.results[g]) == 0);
        CHECK(std::memcmp(&p.params[g], &q.params[g], sizeof p.params[g]) == 0);
    }

    // Exposure-level paths take the segment's benchmark too (portfolio level, not rating class level).
    Duck duck;
    ExternalParameters ext;
    ext.load(duck, "(SELECT 'exposure' AS level, 'E0' AS key, 'adverse' AS scenario, 2 AS year, 0.9 AS pd12m_s1, "
                   "NULL AS pd12m_s2, NULL AS tr1_2, NULL AS tr2_1, NULL AS tr3_1, NULL AS tr3_2, 0.9 AS lgd_s1, NULL AS lgd_s2, "
                   "NULL AS lgd_s3, NULL AS lrlt_s2)", b.d);
    const auto own = exposure_param_paths(b.s, b.s.segments[be], p.params[be][0][0], b.sats.at("HH_HOUSE"), b.macro, b.c, ext, 0,
                                          p.benchmark_of(be));
    CHECK(own[1][2].pd12m_s1 == bm->values[1][1].pd12m_s1);   // benchmark wins over the exposure's own PD/TR
    CHECK(own[1][2].lgd_s1 == 0.9);                            // LGD/LR: not benchmarked, the exposure's value

    // A portfolio without satellite model is projected only if both groups take the benchmark.
    b.sats.erase("GG");
    CHECK_THROWS_AS(project(b.d, b.s, b.cal, b.sats, b.macro, b.c, nullptr, 1, &t), Error);   // GG DE, FR: none
}

TEST_CASE("scenario key benchmark_parameters") {
    const std::string base = "name: t\nmacro_path: m.csv\nsatellites: s.csv\nyear_map: {1: 2025, 2: 2026, 3: 2027}\n"
                             "history_year: 2024\nnormal_gdp_growth: 1.5\ncountry_fallback: [WR, EU]\n"
                             "scope:\n  measurement_categories: [amortised_cost]\n  exposure_types: [loan]\n"
                             "  exclude_intragroup: true\nsegmentation: {top_countries: 10}\n"
                             "calibration: {min_observations: 100, pd_floor: 0.00001}\n"
                             "constraints: {no_cure_from_s3: true, adverse_final_year_blend: [0.8, 0.2]}\n";
    CHECK(!load_scenario(temp_file("sora_bmk_none.yaml", base), "/base").benchmark.enabled);
    const auto c = load_scenario(temp_file("sora_bmk_defaults.yaml", base + "benchmark_parameters: {file: b.csv}\n"), "/base");
    CHECK(c.benchmark.enabled);
    CHECK(c.benchmark.file == fs::path("/base") / "b.csv");
    CHECK(c.benchmark.coverage_threshold == 0.10);
    CHECK(c.benchmark.model_level == 1);
    CHECK(c.benchmark.sovereign);
    CHECK(c.benchmark.country_fallback == std::vector<std::string>{"WR", "EU"});   // the scenario's
    const auto d = load_scenario(temp_file("sora_bmk_set.yaml", base + "benchmark_parameters:\n  file: b.csv\n  coverage_threshold: 0.2\n"
                                                                       "  model_level: segment\n  sovereign: false\n  country_fallback: [EU]\n"), ".");
    CHECK(d.benchmark.coverage_threshold == 0.2);
    CHECK(d.benchmark.model_level == 0);
    CHECK(!d.benchmark.sovereign);
    CHECK(d.benchmark.country_fallback == std::vector<std::string>{"EU"});
    CHECK_THROWS_AS(load_scenario(temp_file("sora_bmk_bad1.yaml", base + "benchmark_parameters: {file: b.csv, model_level: all}\n"), "."), Error);
    CHECK_THROWS_AS(load_scenario(temp_file("sora_bmk_bad2.yaml", base + "benchmark_parameters: {file: b.csv, coverage_threshold: 2}\n"), "."), Error);
    CHECK_THROWS_AS(load_scenario(temp_file("sora_bmk_bad3.yaml", base + "benchmark_parameters: {coverage_threshold: 0.1}\n"), "."), Error);
}
