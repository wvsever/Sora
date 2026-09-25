#include "doctest.h"

#include "sora/collateral.hpp"

using namespace sora;

namespace {

ScenarioConfig config() {
    ScenarioConfig c;
    c.year_map = {{1, 2025}, {2, 2026}, {3, 2027}};
    c.country_fallback = {"WR", "EU"};
    return c;
}

// BE has residential prices; IS has no scenario data (falls back to WR).
MacroTable macro() {
    MacroTable m;
    for (const char* k : {"BE", "WR", "EU"}) m.set("real_gdp", k, "baseline", 2025, 1.0);
    const double adverse[3] = {-10, -5, 0};
    for (int t = 0; t < 3; ++t) {
        m.set("residential_property_prices", "BE", "baseline", 2025 + t, 2.0);
        m.set("residential_property_prices", "BE", "adverse", 2025 + t, adverse[t]);
        m.set("commercial_property_prices", "WR", "adverse", 2025 + t, adverse[t] / 2);
    }
    return m;
}

}  // namespace

TEST_CASE("collateral value index: cumulative property price growth with country fallback") {
    const auto m = macro();
    const auto cfg = config();
    const auto be = collateral_index(CollateralType::ResidentialProperty, "BE", m, cfg);
    CHECK(be[1][0] == 1.0);
    CHECK(be[1][1] == doctest::Approx(0.9));
    CHECK(be[1][3] == doctest::Approx(0.9 * 0.95));
    CHECK(be[0][2] == doctest::Approx(1.02 * 1.02));
    const auto is = collateral_index(CollateralType::CommercialProperty, "IS", m, cfg);
    CHECK(is[1][1] == doctest::Approx(0.95));
    CHECK(is[0][3] == 1.0);   // WR has no baseline commercial path: growth 0
    const auto other = collateral_index(CollateralType::Other, "BE", m, cfg);
    for (const auto& path : other)
        for (double v : path) CHECK(v == 1.0);
}

TEST_CASE("LTV: pro-rata allocation, out-of-scope exposures and t0 stage") {
    Dataset d;
    d.currencies.intern("EUR");
    d.currencies.intern("CHF");
    d.fx_to_reporting = {1'000'000'000, 2'000'000'000};   // 1 CHF = 2 EUR
    const auto be = d.countries.intern("BE");
    auto exposure = [&](Stage st, Cents gca) {
        Exposure e;
        e.id = d.exposure_ids.intern("E" + std::to_string(d.exposures.size()));
        e.stage = st;
        e.gca = gca;
        d.exposures.push_back(e);
    };
    exposure(Stage::S1, 10000);   // 0: EUR 100
    exposure(Stage::S2, 30000);   // 1: EUR 300
    exposure(Stage::S1, 50000);   // 2: out of scope
    exposure(Stage::S3, 20000);   // 3: EUR 200, only cash collateral
    d.collateral = {{CollateralType::ResidentialProperty, 1, be, 30000},   // CHF 300 = EUR 600, no amounts
                    {CollateralType::Other, 0, kNone, 1000}};
    d.collateral_allocations = {{0, 0, false, 0}, {1, 0, false, 0}, {2, 0, false, 0}, {3, 1, true, 1000}};

    Segmentation s;
    s.segments.resize(1);
    s.segment_of = {0, 0, -1, 0};
    s.fx = {1.0, 1.0, 0.0, 1.0};

    const auto r = collateral_ltv(d, s, macro(), config());
    CHECK(r.secured_exposures == 2);
    CHECK(r.pro_rata_allocations == 2);
    const auto& t0 = r.cells[0][0];
    CHECK(t0.secured_exp[0] == doctest::Approx(100));
    CHECK(t0.secured_exp[1] == doctest::Approx(300));
    CHECK(t0.secured_exp[2] == 0);                    // cash collateral does not secure for LTV
    CHECK(t0.re_value[0] == doctest::Approx(150));    // 600 x 100 / 400
    CHECK(t0.re_value[1] == doctest::Approx(450));
    const auto& adv1 = r.cells[0][4];
    CHECK(adv1.secured_exp[0] == doctest::Approx(100));
    CHECK(adv1.re_value[0] == doctest::Approx(135));  // -10% in adverse year 1
    CHECK(r.cells[0][3].re_value[1] == doctest::Approx(450 * 1.02 * 1.02 * 1.02));
}
