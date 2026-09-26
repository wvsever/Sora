// Sectoral (GVA) satellites (scenario key sector_satellites) and the consistency of the satellite rule between the
// on-balance projection, the off-balance items and the calculator records.

#include "doctest.h"

#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>

#include "sora/benchmark.hpp"
#include "sora/duck.hpp"
#include "sora/off_balance.hpp"
#include "sora/projection.hpp"

using namespace sora;
namespace fs = std::filesystem;

namespace {

fs::path temp_file(const std::string& name, const std::string& text) {
    const auto p = fs::temp_directory_path() / name;
    std::ofstream(p, std::ios::binary) << text;
    return p;
}

double logit(double p) { return std::log(p / (1 - p)); }
double expit(double x) { return 1 / (1 + std::exp(-x)); }

Params start_params() {
    Params p;
    p.pd12m_s1 = 0.02; p.pd12m_s2 = 0.10; p.tr1_2 = 0.05; p.tr2_1 = 0.20; p.tr3_1 = 0.01; p.tr3_2 = 0.02;
    p.lgd_s1 = p.lgd_s2 = p.lgd_s3 = 0.40; p.lrlt_s2 = 0.08;
    return p;
}

// NFC SME loans in BE (sectors F, C_EI, G) and US (F), one household loan in BE; a GG loan in BE. EU 2025 scenario
// shape: GDP for BE, US and EU; real GVA of F and C_high for BE and EU only (the US has no sectoral GVA).
struct Book {
    Dataset d;
    Segmentation s;
    ScenarioConfig c;
    MacroTable macro;
    std::map<std::string, Satellite> sats{{"NFC_SME_OTHER", {-0.12, 0.0, 0.0, 0.0}}, {"HH_CONS", {-0.1, 0.0, 0.0, 0.0}},
                                          {"GG", {-0.05, 0.0, 0.0, 0.0}}};
    Calibration cal;
    SectorSatellites sectors{};
};

Book book(bool with_g = true) {
    Book b;
    auto& d = b.d;
    d.currencies.intern("EUR");
    d.fx_to_reporting = {1'000'000'000};
    for (const char* c : {"BE", "US"}) d.countries.intern(c);
    const struct { EbaSector sector; std::uint32_t country; NaceSector nace; Stage stage; Cents gca; } rows[] = {
        {EbaSector::NonFinancialCorporation, 0, NaceSector::F, Stage::S1, 1000},
        {EbaSector::NonFinancialCorporation, 0, NaceSector::CEnergyIntensive, Stage::S1, 2000},
        {EbaSector::NonFinancialCorporation, 0, NaceSector::G, Stage::S2, 3000},
        {EbaSector::NonFinancialCorporation, 1, NaceSector::F, Stage::S2, 4000},
        {EbaSector::Household, 0, NaceSector::Unknown, Stage::S1, 5000},
        {EbaSector::GeneralGovernment, 0, NaceSector::Unknown, Stage::S1, 6000}};
    for (const auto& r : rows) {
        if (!with_g && r.nace == NaceSector::G) continue;
        Counterparty cp;
        cp.country = r.country;
        cp.sector = r.sector;
        cp.is_sme = Flag::True;
        cp.nace = r.nace;
        d.counterparties.push_back(cp);
        Exposure e;
        e.id = d.exposure_ids.intern("E" + std::to_string(d.exposures.size()));
        e.counterparty = static_cast<std::uint32_t>(d.counterparties.size() - 1);
        e.currency = 0;
        e.stage = r.stage;
        e.purpose = HouseholdPurpose::Consumption;
        e.is_cre = Flag::False;
        e.has_gca = true;
        e.gca = r.gca * 100;
        e.allowance = r.gca;   // 1%
        d.exposures.push_back(e);
    }
    b.s = segment(d, ScopeConfig{});
    b.c.year_map = {{1, 2025}, {2, 2026}, {3, 2027}};
    b.c.history_year = 2024;
    b.c.normal_gdp_growth = 1.5;
    b.c.country_fallback = {"EU"};
    b.c.sector_satellites.enabled = true;
    for (int y = 2025; y <= 2027; ++y) {
        for (const char* k : {"BE", "US", "EU"}) {
            b.macro.set("real_gdp", k, "baseline", y, 1.0);
            b.macro.set("real_gdp", k, "adverse", y, std::string(k) == "US" ? -1.0 : -2.0);
        }
        for (const char* k : {"BE", "EU"}) {
            b.macro.set("real_gva:F", k, "baseline", y, 1.5);
            b.macro.set("real_gva:F", k, "adverse", y, std::string(k) == "BE" ? -5.0 : -4.0);
            b.macro.set("real_gva:C_high", k, "baseline", y, 0.5);
            b.macro.set("real_gva:C_high", k, "adverse", y, -6.0);
        }
    }
    b.sectors[static_cast<std::size_t>(NaceSector::F)] = SectorSatellite{-0.2, 0.5};
    b.sectors[static_cast<std::size_t>(NaceSector::CEnergyIntensive)] = SectorSatellite{-0.1, std::nullopt};
    for (const auto& seg : b.s.segments) {
        b.cal.params.push_back(start_params());
        const std::string own(b.s.level_keys.view(seg.levels[0]));
        b.cal.sources.push_back({own, own, own, own, own});
    }
    return b;
}

std::size_t seg_index(const Book& b, const std::string& key) {
    for (std::size_t i = 0; i < b.s.segments.size(); ++i)
        if (b.s.segments[i].key == key) return i;
    FAIL("no segment " << key);
    return 0;
}

}  // namespace

TEST_CASE("sector codes and the GVA sectors of the ESRB scenario") {
    CHECK(sector_code(NaceSector::CEnergyIntensive) == "C_EI");
    CHECK(sector_code(NaceSector::M) == "M");
    CHECK(parse_sector_code("C_OT") == NaceSector::COther);
    CHECK(parse_sector_code("T") == NaceSector::T);
    CHECK(!parse_sector_code("UNKNOWN"));
    CHECK(!parse_sector_code("C"));
    // Rev. 2.1 sections to the Rev. 2 sections and aggregates of the scenario, by division.
    CHECK(gva_sector(NaceSector::CEnergyIntensive) == "C_high");
    CHECK(gva_sector(NaceSector::COther) == "C_low");
    CHECK(gva_sector(NaceSector::J) == "J");
    CHECK(gva_sector(NaceSector::K) == "J");     // computer programming: Rev. 2 J62
    CHECK(gva_sector(NaceSector::L) == "K");     // financial and insurance: Rev. 2 K
    CHECK(gva_sector(NaceSector::M) == "L");     // real estate: Rev. 2 L68
    CHECK(gva_sector(NaceSector::N) == "MN");
    CHECK(gva_sector(NaceSector::O) == "MN");
    CHECK(gva_sector(NaceSector::R) == "OPQ");   // human health: Rev. 2 Q86
    CHECK(gva_sector(NaceSector::T) == "RSTU");
    CHECK(gva_sector(NaceSector::Unknown).empty());
}

TEST_CASE("sector satellite file: format and validation") {
    Duck duck;
    const auto s = load_sector_satellites(duck, temp_file("sora_sec_ok.csv", "# SYNTHETIC\nsector,beta_gva,lgd_gva_sensitivity,description\n"
                                                                             "F,-0.15,0.8,Construction\nC_EI,-0.16,,EI\nD,,0.3,LGD only\n"));
    REQUIRE(s[static_cast<std::size_t>(NaceSector::F)]);
    CHECK(*s[static_cast<std::size_t>(NaceSector::F)]->beta_gva == -0.15);
    CHECK(s[static_cast<std::size_t>(NaceSector::F)]->covers(1));
    CHECK(s[static_cast<std::size_t>(NaceSector::CEnergyIntensive)]->covers(0));
    CHECK(!s[static_cast<std::size_t>(NaceSector::CEnergyIntensive)]->covers(1));
    CHECK(!s[static_cast<std::size_t>(NaceSector::D)]->covers(0));
    CHECK(!s[static_cast<std::size_t>(NaceSector::G)]);
    auto rejects = [&](const std::string& name, const std::string& body) {
        CHECK_THROWS_AS(load_sector_satellites(duck, temp_file(name, "sector,beta_gva,lgd_gva_sensitivity,description\n" + body)), Error);
    };
    rejects("sora_sec_unknown.csv", "C,-0.1,0.2,bare C\n");
    rejects("sora_sec_dup.csv", "F,-0.1,0.2,x\nF,-0.2,0.2,y\n");
    rejects("sora_sec_empty.csv", "F,,,nothing\n");
}

TEST_CASE("scenario key sector_satellites") {
    const std::string base = "name: t\nmacro_path: m.csv\nsatellites: s.csv\nyear_map: {1: 2025, 2: 2026, 3: 2027}\n"
                             "history_year: 2024\nnormal_gdp_growth: 1.5\ncountry_fallback: [WR, EU]\n"
                             "scope:\n  measurement_categories: [amortised_cost]\n  exposure_types: [loan]\n"
                             "  exclude_intragroup: true\nsegmentation: {top_countries: 10}\n"
                             "calibration: {min_observations: 100, pd_floor: 0.00001}\n"
                             "constraints: {no_cure_from_s3: true, adverse_final_year_blend: [0.8, 0.2]}\n";
    CHECK(!load_scenario(temp_file("sora_sec_none.yaml", base), "/base").sector_satellites.enabled);
    const auto c = load_scenario(temp_file("sora_sec_default.yaml", base + "sector_satellites: {file: x.csv}\n"), "/base");
    CHECK(c.sector_satellites.enabled);
    CHECK(c.sector_satellites.file == fs::path("/base") / "x.csv");
    CHECK(c.sector_satellites.gva_fallback == std::vector<std::string>{"EU"});
    const auto e = load_scenario(temp_file("sora_sec_set.yaml", base + "sector_satellites:\n  file: x.csv\n  gva_fallback: [EA, EU]\n"), ".");
    CHECK(e.sector_satellites.gva_fallback == std::vector<std::string>{"EA", "EU"});
    CHECK_THROWS_AS(load_scenario(temp_file("sora_sec_bad.yaml", base + "sector_satellites: {gva_fallback: [EU]}\n"), "."), Error);
}

TEST_CASE("sectoral satellite: GVA growth replaces GDP growth in the PD/TR index, cumulative GVA decline raises LGD/LR") {
    Book b = book();
    const auto be = seg_index(b, "LOANS|NFC_SME_OTHER|BE"), us = seg_index(b, "LOANS|NFC_SME_OTHER|US");
    const Params p0 = start_params();
    const auto sat = b.sats.at("NFC_SME_OTHER");
    const auto f_be = sector_model(b.macro, b.s.segments[be], NaceSector::F, *b.sectors[static_cast<std::size_t>(NaceSector::F)], b.c);
    CHECK(f_be.gva_key == "BE");
    CHECK(!f_be.relative);
    const auto path = project_parameters(b.s.segments[be], p0, sat, b.macro, "adverse", b.c, &f_be);
    // Year 1: z = -0.2 x (-5 - 1.5) = 1.3 (instead of the portfolio's -0.12 x (-2 - 1.5) = 0.42).
    CHECK(path[1].pd12m_s1 == doctest::Approx(expit(logit(0.02) + 1.3)).epsilon(1e-12));
    CHECK(path[1].tr2_1 == doctest::Approx(expit(logit(0.20) - 1.3)).epsilon(1e-12));
    // LGD: 1 + 0.5 x (1 - 0.95) in year 1, 1 + 0.5 x (1 - 0.95^3) in year 3.
    CHECK(path[1].lgd_s1 == doctest::Approx(0.40 * 1.025).epsilon(1e-12));
    CHECK(path[3].lrlt_s2 == doctest::Approx(0.08 * (1 + 0.5 * (1 - 0.95 * 0.95 * 0.95))).epsilon(1e-12));
    CHECK(path[1].tr3_1 == 0.01);   // cures are not projected
    // A PD/TR-only sector: LGD/LR from the portfolio model (no property sensitivity here: unchanged).
    const auto c_be = sector_model(b.macro, b.s.segments[be], NaceSector::CEnergyIntensive,
                                   *b.sectors[static_cast<std::size_t>(NaceSector::CEnergyIntensive)], b.c);
    const auto cpath = project_parameters(b.s.segments[be], p0, sat, b.macro, "adverse", b.c, &c_be);
    CHECK(cpath[2].pd12m_s2 == doctest::Approx(expit(logit(0.10) - 0.1 * (-6.0 - 1.5))).epsilon(1e-12));
    CHECK(cpath[3].lgd_s2 == 0.40);
    // No sectoral GVA for the US: US GDP growth plus the sector's GVA deviation from GDP in the EU (-1 + (-4 + 2)).
    const auto f_us = sector_model(b.macro, b.s.segments[us], NaceSector::F, *b.sectors[static_cast<std::size_t>(NaceSector::F)], b.c);
    CHECK(f_us.relative);
    CHECK(f_us.gva_key == "EU");
    CHECK(sector_growth(f_us, b.macro, "adverse", 2026) == -3.0);
    // Without a GVA path in any fallback key: an error, not a silent portfolio model.
    b.c.sector_satellites.gva_fallback = {"EA"};
    CHECK_THROWS_AS(sector_model(b.macro, b.s.segments[us], NaceSector::F, *b.sectors[static_cast<std::size_t>(NaceSector::F)], b.c), Error);
}

TEST_CASE("project(): NFC exposures take their sector's path, bit-identical for any number of workers") {
    Book b = book();
    const auto be = seg_index(b, "LOANS|NFC_SME_OTHER|BE"), hh = seg_index(b, "LOANS|HH_CONS|BE");
    const Projection plain = project(b.d, b.s, b.cal, b.sats, b.macro, b.c, nullptr, 1);
    const Projection p = project(b.d, b.s, b.cal, b.sats, b.macro, b.c, nullptr, 1, nullptr, &b.sectors);
    REQUIRE(p.sectoral);
    CHECK(p.exposures_with_sector_model == 3);   // BE F, BE C_EI, US F
    CHECK(p.sector_paths[hh].empty());           // not an NFC segment
    CHECK(p.sector_paths[be].size() == 2);       // every sector with coefficients (F, C_EI)
    CHECK(!p.sector_path(be, NaceSector::G));
    CHECK(&p.paths(be, NaceSector::G) == &p.params[be]);
    const auto* f = p.sector_path(be, NaceSector::F);
    REQUIRE(f);
    CHECK(f->sectoral == std::array<bool, 2>{true, true});
    CHECK(p.sector_path(be, NaceSector::CEnergyIntensive)->sectoral == std::array<bool, 2>{true, false});
    CHECK(p.sector_coverage[be] == std::array<bool, 2>{false, false});   // G has no sectoral model
    // The segment path is the portfolio model's, unchanged; the results are the per-exposure sums with the sector paths.
    CHECK(std::memcmp(&p.params[be], &plain.params[be], sizeof p.params[be]) == 0);
    std::array<std::array<YearResult, 3>, 2> seq{};
    for (std::size_t i = 0; i < b.d.exposures.size(); ++i) {
        if (b.s.segment_of[i] != static_cast<std::int32_t>(be)) continue;
        const auto& e = b.d.exposures[i];
        project_exposure(e.stage, to_double(e.gca), b.s.allowance[i], p.paths(be, b.d.counterparties[e.counterparty].nace), b.c, seq);
    }
    CHECK(std::memcmp(&seq, &p.results[be], sizeof seq) == 0);
    CHECK(p.results[be][1][2].prov_stock_s1 != plain.results[be][1][2].prov_stock_s1);
    // Other segments are unchanged.
    CHECK(std::memcmp(&p.results[hh], &plain.results[hh], sizeof p.results[hh]) == 0);
    for (unsigned w : {2U, 3U, 8U}) {
        const Projection q = project(b.d, b.s, b.cal, b.sats, b.macro, b.c, nullptr, w, nullptr, &b.sectors);
        for (std::size_t g = 0; g < p.results.size(); ++g) {
            CHECK(std::memcmp(&p.results[g], &q.results[g], sizeof p.results[g]) == 0);
            CHECK(std::memcmp(&p.accum[g], &q.accum[g], sizeof p.accum[g]) == 0);
            REQUIRE(p.sectors[g].size() == q.sectors[g].size());
            for (std::size_t k = 0; k < p.sectors[g].size(); ++k)
                CHECK(std::memcmp(&p.sectors[g][k].results, &q.sectors[g][k].results, sizeof p.sectors[g][k].results) == 0);
        }
    }
    // Exposure-level customer parameters: the exposure's own path is projected with its sector's model too.
    Duck duck;
    ExternalParameters ext;
    ext.load(duck, "(SELECT 'exposure' AS level, 'E0' AS key, 'actual' AS scenario, 0 AS year, 0.03 AS pd12m_s1, "
                   "NULL AS pd12m_s2, NULL AS tr1_2, NULL AS tr2_1, NULL AS tr3_1, NULL AS tr3_2, NULL AS lgd_s1, NULL AS lgd_s2, "
                   "NULL AS lgd_s3, NULL AS lrlt_s2)", b.d);
    const Projection x = project(b.d, b.s, b.cal, b.sats, b.macro, b.c, &ext, 1, nullptr, &b.sectors);
    const auto own = own_param_paths(x, b.s, be, NaceSector::F, b.macro, b.c, ext, 0);
    CHECK(own[1][1].pd12m_s1 == doctest::Approx(expit(logit(0.03) + 1.3)).epsilon(1e-12));
}

TEST_CASE("sectoral satellites and the ECB benchmark rule: a model for coverage, the benchmark still wins") {
    BenchmarkTable t;
    for (int sc = 0; sc < 2; ++sc)
        for (int y = 1; y <= 3; ++y)
            for (auto k : kBenchmarkGroupParams[0]) t.set("LOANS|NFC_SME_OTHER|BE", sc, y, k, 0.03 + 0.001 * y);
    t.finish();

    SUBCASE("benchmark applied to the segment: it replaces the group on the sector paths too") {
        Book b = book();
        b.c.benchmark.enabled = true;
        const auto be = seg_index(b, "LOANS|NFC_SME_OTHER|BE");
        b.cal.sources[be][0] = "none";   // no PD/TR model for the segment: no_model -> benchmark
        // Keep the pivot class above the threshold: the US segment is modelled.
        const Projection p = project(b.d, b.s, b.cal, b.sats, b.macro, b.c, nullptr, 1, &t, &b.sectors);
        REQUIRE(p.benchmark.decisions[be][0].applied());
        const auto* f = p.sector_path(be, NaceSector::F);
        CHECK(f->params[1][2].pd12m_s1 == t.find("LOANS|NFC_SME_OTHER|BE")->values[1][1].pd12m_s1);   // benchmark
        CHECK(f->params[1][2].lgd_s1 != 0.40);                    // LGD/LR: still the sector model
        CHECK(f->sectoral == std::array<bool, 2>{false, true});   // CR_SECTOR columns 1-2: LGD/LR only
    }
    SUBCASE("a portfolio without satellite coefficients whose exposures all have sectoral models") {
        Book b = book(false);   // no G exposure: BE has F and C_EI, US has F
        b.sats.erase("NFC_SME_OTHER");
        b.c.benchmark.enabled = true;
        const auto be = seg_index(b, "LOANS|NFC_SME_OTHER|BE"), us = seg_index(b, "LOANS|NFC_SME_OTHER|US");
        // PD/TR: every exposure sector-modelled, a model for the rule; LGD/LR: C_EI has none, so BE is not modelled.
        std::vector<std::array<bool, 2>> cov(b.s.segments.size(), std::array<bool, 2>{false, false});
        cov[be] = {true, false};
        cov[us] = {true, true};
        const auto r = decide_benchmarks(b.d, b.s, b.cal, b.sats, t, b.c.benchmark, nullptr, &cov);
        CHECK(r.decisions[be][0].model);
        CHECK(!r.decisions[be][1].model);
        CHECK(r.decisions[us][0].model);
        CHECK(!decide_benchmarks(b.d, b.s, b.cal, b.sats, t, b.c.benchmark, nullptr).decisions[us][0].model);
        // BE LGD/LR has neither a model nor a benchmark (the table has PD/TR only, or none): not projected.
        CHECK_THROWS_AS(project(b.d, b.s, b.cal, b.sats, b.macro, b.c, nullptr, 1, &t, &b.sectors), Error);
        CHECK_THROWS_AS(project(b.d, b.s, b.cal, b.sats, b.macro, b.c, nullptr, 1, nullptr, &b.sectors), Error);
        CHECK_THROWS_AS(project(b.d, b.s, b.cal, b.sats, b.macro, b.c, nullptr, 1, &t, nullptr), Error);
        // With an LGD/LR benchmark for BE (rule no_model: US covers 4000 of 7000), everything has a model.
        BenchmarkTable full = t;
        for (int sc = 0; sc < 2; ++sc)
            for (int y = 1; y <= 3; ++y)
                for (auto k : kBenchmarkGroupParams[1]) full.set("LOANS|NFC_SME_OTHER|BE", sc, y, k, 0.5);
        full.finish();
        const Projection p = project(b.d, b.s, b.cal, b.sats, b.macro, b.c, nullptr, 1, &full, &b.sectors);
        CHECK(p.sector_coverage[be] == std::array<bool, 2>{true, false});
        CHECK(p.sector_coverage[us] == std::array<bool, 2>{true, true});
        CHECK(!p.portfolio_model[us]);
        CHECK(!p.benchmark.decisions[be][0].applied());
        CHECK(p.benchmark.decisions[be][1].rule == BenchmarkRule::NoModel);
        CHECK(p.benchmark.decisions[be][1].applied());
        CHECK(p.sector_path(be, NaceSector::CEnergyIntensive)->params[0][1].lgd_s1 == 0.5);
        CHECK(p.sector_path(us, NaceSector::F)->params[1][1].pd12m_s1 == doctest::Approx(expit(logit(0.02) - 0.2 * (-3.0 - 1.5))).epsilon(1e-12));
        CHECK(p.params[us][1][1].pd12m_s1 == doctest::Approx(0.02).epsilon(1e-12));   // segment path: flat (no portfolio model), used by no exposure
    }
}

// The off-balance items follow the on-balance rule (Projection::check_modelled): before, a portfolio without
// satellite coefficients stopped the off-balance projection even when benchmarks covered it completely.
TEST_CASE("off-balance items: a portfolio without satellite coefficients, fully benchmarked; sector paths") {
    Book b = book();
    const auto dir = fs::temp_directory_path() / "sora_sec_sim";
    fs::create_directories(dir / "sim_exposure");
    std::ofstream(dir / "sim_exposure" / "part.csv", std::ios::binary) << "exposure_id,is_unconditionally_cancellable\nC0,false\n";
    b.d.sim_dir = dir;
    // Loan commitments given: to the GG counterparty (BE) and to the construction company (F, BE).
    for (std::uint32_t cp : {5U, 0U}) {
        Exposure e;
        e.id = b.d.exposure_ids.intern("C" + std::to_string(cp));
        e.counterparty = cp;
        e.currency = 0;
        e.type = ExposureType::LoanCommitment;
        e.stage = Stage::S1;
        e.off_balance = 1'000'000;
        e.allowance = 100;
        b.d.exposures.push_back(e);
    }
    b.s = segment(b.d, ScopeConfig{});
    b.c.off_balance.enabled = true;
    b.c.off_balance.types = {ExposureType::LoanCommitment};
    b.c.benchmark.enabled = true;
    b.c.benchmark.country_fallback = {};
    BenchmarkTable t;
    for (std::size_t g = 0; g < 2; ++g)
        for (int sc = 0; sc < 2; ++sc)
            for (int y = 1; y <= 3; ++y)
                for (auto k : kBenchmarkGroupParams[g]) t.set("LOANS|GG|BE", sc, y, k, 0.02 + 0.01 * static_cast<double>(g));
    t.finish();
    b.sats.erase("GG");   // sovereign BE: both groups benchmarked (MN para 146), so no satellite is needed

    Duck duck;
    const Projection p = project(b.d, b.s, b.cal, b.sats, b.macro, b.c, nullptr, 1, &t, &b.sectors);
    const auto gg = seg_index(b, "LOANS|GG|BE"), be = seg_index(b, "LOANS|NFC_SME_OTHER|BE");
    CHECK(!p.portfolio_model[gg]);
    CHECK_NOTHROW(p.check_modelled(b.s.segments[gg], gg, NaceSector::Unknown));
    const auto r = project_off_balance(duck, b.d, b.s, p, b.macro, b.c, nullptr, "", 1);
    REQUIRE(r.groups.size() == 2);
    CHECK(r.items == 2);
    for (const auto& g : r.groups) {
        const auto& seg = b.s.segments[g.segment];
        OffBalanceGroup expect;
        const auto& paths = g.segment == be ? p.sector_path(be, NaceSector::F)->params : p.params[gg];
        project_off_balance_item(Stage::S1, 10'000.0, 0.4, 1.0, paths, b.c, expect);
        CHECK(std::memcmp(&expect.post, &g.post, sizeof g.post) == 0);   // GG: benchmark path; F: sector path
        CHECK((seg.key == "LOANS|GG|BE" || seg.key == "LOANS|NFC_SME_OTHER|BE"));
    }
    // Without the benchmark for one group, the same rule stops both projections.
    Projection q = p;
    q.benchmark.apply[gg].applied[1] = false;
    CHECK_THROWS_AS(q.check_modelled(b.s.segments[gg], gg, NaceSector::Unknown), Error);
    CHECK_THROWS_AS(project_off_balance(duck, b.d, b.s, q, b.macro, b.c, nullptr, "", 1), Error);
    fs::remove_all(dir);
}
