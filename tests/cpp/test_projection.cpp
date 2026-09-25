#include "doctest.h"

#include <cstring>

#include "sora/projection.hpp"
#include "sora/segmentation.hpp"

using namespace sora;

namespace {

std::array<ParamPath, 2> flat(const Params& p) {
    ParamPath path;
    path.fill(p);
    return {path, path};
}

ScenarioConfig cfg() { return {}; }

}  // namespace

// Same hand-computed case as python/tests/test_reference.py (EBA 2027 draft MN Boxes 3-9).
TEST_CASE("Boxes 3-9, year 1, one segment of four exposures") {
    Params p;
    p.pd12m_s1 = 0.02; p.pd12m_s2 = 0.10; p.tr1_2 = 0.05; p.tr2_1 = 0.20;
    p.lgd_s1 = 0.40; p.lgd_s2 = 0.50; p.lgd_s3 = 0.60; p.lrlt_s2 = 0.08;
    std::array<std::array<YearResult, 3>, 2> acc{};
    const auto paths = flat(p);
    project_exposure(Stage::S1, 1000, 3, paths, cfg(), acc);
    project_exposure(Stage::S2, 200, 10, paths, cfg(), acc);
    project_exposure(Stage::S3, 100, 50, paths, cfg(), acc);
    project_exposure(Stage::Poci, 10, 4, paths, cfg(), acc);
    const auto& r = acc[0][0];
    CHECK(r.flow_s1_s2 == doctest::Approx(50));
    CHECK(r.flow_s2_s1 == doctest::Approx(40));
    CHECK(r.flow_s1_s3 == doctest::Approx(20));
    CHECK(r.flow_s2_s3 == doctest::Approx(20));
    CHECK(r.exp_s1 == doctest::Approx(970));
    CHECK(r.exp_s2 == doctest::Approx(190));
    CHECK(r.exp_s3_new == doctest::Approx(40));
    CHECK(r.prov_s1_s1 == doctest::Approx(7.44));
    CHECK(r.prov_s2_s1 == doctest::Approx(0.32));
    CHECK(r.prov_s1_s2 == doctest::Approx(4.0));
    CHECK(r.prov_s2_s2 == doctest::Approx(11.2));
    CHECK(r.prov_cum_s1_s3 == doctest::Approx(8));
    CHECK(r.prov_cum_s2_s3 == doctest::Approx(10));
    CHECK(r.prov_old_s3 == doctest::Approx(60));
    CHECK(r.impairment == doctest::Approx(104.96 - 67));
}

TEST_CASE("Box 9 floor applies per exposure (para 141)") {
    Params p;
    p.lgd_s3 = 0.5;
    std::array<std::array<YearResult, 3>, 2> acc{};
    project_exposure(Stage::S3, 100, 70, flat(p), cfg(), acc);
    project_exposure(Stage::S3, 100, 10, flat(p), cfg(), acc);
    for (const auto& y : acc[0]) CHECK(y.prov_old_s3 == doctest::Approx(120));
}

TEST_CASE("exposure is conserved over the horizon") {
    Params p;
    p.pd12m_s1 = 0.03; p.pd12m_s2 = 0.2; p.tr1_2 = 0.1; p.tr2_1 = 0.3;
    p.lgd_s1 = 0.3; p.lgd_s2 = 0.4; p.lgd_s3 = 0.5; p.lrlt_s2 = 0.1;
    std::array<std::array<YearResult, 3>, 2> acc{};
    project_exposure(Stage::S1, 800, 0, flat(p), cfg(), acc);
    project_exposure(Stage::S2, 150, 0, flat(p), cfg(), acc);
    project_exposure(Stage::S3, 50, 0, flat(p), cfg(), acc);
    for (const auto& y : acc[0]) CHECK(y.exp_s1 + y.exp_s2 + y.exp_s3_old + y.exp_s3_new == doctest::Approx(1000));
}

TEST_CASE("final adverse year blends the t+2 loss term") {
    Params base, adv;
    base.pd12m_s1 = 0.01; base.lgd_s1 = 0.5;
    adv.pd12m_s1 = 0.04; adv.lgd_s1 = 0.5;
    ParamPath pb, pa;
    pb.fill(base);
    pa.fill(adv);
    std::array<std::array<YearResult, 3>, 2> acc{};
    project_exposure(Stage::S1, 1000, 0, {pb, pa}, cfg(), acc);
    const double e1 = 1000 * 0.96 * 0.96;
    CHECK(acc[1][2].prov_s1_s1 == doctest::Approx(e1 * 0.96 * (5.0 / 6 * 0.04 * 0.5 + 1.0 / 6 * 0.01 * 0.5)));
}

TEST_CASE("project() is bit-identical for any number of workers") {
    // Synthetic portfolio: 20k exposures over 3 countries, 2 sectors and all stages (deterministic LCG).
    Dataset d;
    d.currencies.intern("EUR");
    d.fx_to_reporting = {1'000'000'000};
    for (const char* c : {"BE", "DE", "FR"}) d.countries.intern(c);
    for (std::uint32_t i = 0; i < 300; ++i) {
        Counterparty cp;
        cp.country = i % 3;
        cp.sector = i % 2 ? EbaSector::Household : EbaSector::NonFinancialCorporation;
        cp.is_sme = i % 5 ? Flag::True : Flag::False;
        d.counterparties.push_back(cp);
    }
    std::uint64_t x = 42;
    auto next = [&x] { x = x * 6364136223846793005ULL + 1442695040888963407ULL; return x >> 33; };
    for (std::uint32_t i = 0; i < 20000; ++i) {
        Exposure e;
        e.id = d.exposure_ids.intern("E" + std::to_string(i));
        e.counterparty = static_cast<std::uint32_t>(next() % 300);
        e.currency = 0;
        e.stage = static_cast<Stage>(next() % 4);
        e.purpose = next() % 2 ? HouseholdPurpose::HousePurchase : HouseholdPurpose::Consumption;
        e.is_cre = next() % 3 ? Flag::False : Flag::True;
        e.has_gca = true;
        e.gca = static_cast<Cents>(next() % 100'000'000);
        e.allowance = static_cast<Cents>(next() % 1'000'000);
        d.exposures.push_back(e);
    }
    const Segmentation s = segment(d, ScopeConfig{});
    REQUIRE(s.segments.size() > 8);

    ScenarioConfig c;
    c.year_map = {{1, 2025}, {2, 2026}, {3, 2027}};
    c.history_year = 2024;
    MacroTable macro;
    for (const char* k : {"BE", "DE", "FR"})
        for (int y = 2025; y <= 2027; ++y) {
            macro.set("real_gdp", k, "baseline", y, 1.2);
            macro.set("real_gdp", k, "adverse", y, -2.5 + 0.1 * y - 202.5);
        }
    std::map<std::string, Satellite> sats;
    for (const auto& seg : s.segments) sats[seg.portfolio] = {-0.1, 0.05, -0.01, 0.5};
    Calibration cal;
    for (std::size_t i = 0; i < s.segments.size(); ++i) {
        Params p;
        p.pd12m_s1 = 0.01 + 0.001 * static_cast<double>(i); p.pd12m_s2 = 0.1; p.tr1_2 = 0.05; p.tr2_1 = 0.2;
        p.lgd_s1 = p.lgd_s2 = p.lgd_s3 = 0.35; p.lrlt_s2 = 0.1;
        cal.params.push_back(p);
    }

    const Projection one = project(d, s, cal, sats, macro, c, nullptr, 1);
    for (unsigned w : {2U, 3U, 8U, 0U}) {
        const Projection many = project(d, s, cal, sats, macro, c, nullptr, w);
        REQUIRE(many.results.size() == one.results.size());
        for (std::size_t g = 0; g < one.results.size(); ++g) {
            CHECK(std::memcmp(&one.results[g], &many.results[g], sizeof one.results[g]) == 0);
            CHECK(std::memcmp(&one.accum[g], &many.accum[g], sizeof one.accum[g]) == 0);
        }
    }
    // The parallel result is the sequential sum in exposure order.
    std::vector<std::array<std::array<YearResult, 3>, 2>> seq(s.segments.size());
    for (std::size_t i = 0; i < d.exposures.size(); ++i) {
        const auto g = s.segment_of[i];
        if (g < 0) continue;
        const auto& e = d.exposures[i];
        project_exposure(e.stage, to_double(e.gca) * s.fx[i], to_double(e.allowance) * s.fx[i],
                         one.params[static_cast<std::size_t>(g)], c, seq[static_cast<std::size_t>(g)]);
    }
    for (std::size_t g = 0; g < seq.size(); ++g) CHECK(std::memcmp(&seq[g], &one.results[g], sizeof seq[g]) == 0);
}
