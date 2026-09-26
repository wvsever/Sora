#include "doctest.h"

#include "sora/nii.hpp"

using namespace sora;
using namespace sora::nii;

namespace {

// Days since 1970-01-01.
constexpr Date k20260630 = 20634, k20261230 = 20817, k20270630 = 20999, k20280630 = 21365, k20290630 = 21730;
constexpr Date k20260930 = 20726, k20240131 = 19753, k20240229 = 19782, k20251231 = 20453;
constexpr std::array<Date, 4> kBounds = {k20260630, k20270630, k20280630, k20290630};

Position performing(double volume, double ref0, double margin0) {
    Position p;
    p.row = 4;
    p.volume = volume;
    p.eir = ref0 + margin0;
    p.ref0 = ref0;
    p.margin0 = margin0;
    for (std::size_t sc = 0; sc < 2; ++sc)
        for (std::size_t y = 0; y < 3; ++y) {
            p.delta[sc][y] = (sc == 0 ? 0.01 : 0.02) * static_cast<double>(y + 1);   // baseline 1%, 2%, 3%; adverse 2%, 4%, 6%
            p.shock[sc][y] = 0.001 * static_cast<double>(y + 1) * (sc == 0 ? 1.0 : 2.0);
        }
    return p;
}

}  // namespace

TEST_CASE("nii: calendar months clamp to the month end") {
    CHECK(add_months(k20260630, 12) == k20270630);
    CHECK(add_months(k20260630, 36) == k20290630);
    CHECK(add_months(k20260630, -6) == k20251231 - 1);    // 2025-12-30
    CHECK(add_months(k20240131, 1) == k20240229);         // leap February
    CHECK(add_months(k20240229, 12) == k20240229 + 365);  // 2025-02-28
    CHECK(add_months(k20260930, 3) == k20261230);
    CHECK(add_months(k20260630, 0) == k20260630);
}

TEST_CASE("nii: linear interpolation, flat beyond the curve") {
    const Curve c = {{0.25, 0.01}, {1.0, 0.02}, {10.0, 0.04}};
    CHECK(interp(c, 0.1) == 0.01);
    CHECK(interp(c, 0.25) == 0.01);
    CHECK(interp(c, 0.625) == doctest::Approx(0.015));
    CHECK(interp(c, 5.5) == doctest::Approx(0.03));
    CHECK(interp(c, 1.0) == 0.02);
    CHECK(interp(c, 30.0) == 0.04);
    CHECK_THROWS(interp(Curve{}, 1.0));
}

TEST_CASE("nii: rate scenario uses the bank curve, the scenario change and the RoW fallback") {
    RateScenario rs;
    rs.history_year = 2024;
    rs.years = {2025, 2026, 2027};
    rs.bank["EUR"] = {{0.25, 0.02}, {10.0, 0.03}};
    for (const char* k : {"EUR", "RoW"}) {
        rs.swaps[{k, "starting_point", 2024}] = {{1 / 12.0, 0.03}, {10.0, 0.03}};
        for (int y = 2025; y <= 2027; ++y) {
            rs.swaps[{k, "baseline", y}] = {{1 / 12.0, 0.02}, {10.0, 0.025}};
            rs.swaps[{k, "adverse", y}] = {{1 / 12.0, 0.04}, {10.0, 0.05}};
        }
    }
    CHECK(rs.swap_key("EUR") == "EUR");
    CHECK(rs.swap_key("PLN") == "RoW");
    CHECK(rs.rf0("EUR", 0.25) == 0.02);          // bank curve
    CHECK(rs.rf0("PLN", 5.0) == 0.03);           // no bank curve: the scenario's starting point
    CHECK(rs.delta("EUR", 1 / 12.0, 0, 1) == doctest::Approx(-0.01));
    CHECK(rs.delta("PLN", 10.0, 1, 3) == doctest::Approx(0.02));
    CHECK_THROWS(rs.swap("EUR", "adverse", 2030));
}

TEST_CASE("nii: fixed position replaced at maturity with the new business margin") {
    auto p = performing(1000.0, 0.03, 0.02);
    p.maturity = k20261230;   // 183 days after the reference date, original term 183 days
    p.term = 183;
    p.margin_new = 0.015;
    const auto r = project_position(p, kBounds);
    // Starting point: V x EIR, split into reference rate and margin.
    CHECK(r[0][0] == doctest::Approx(50.0));
    CHECK(r[0][1] == doctest::Approx(30.0));
    CHECK(r[0][2] == doctest::Approx(20.0));
    // Baseline year 1: 183 days at 5%, then 182 days at (3% + 1%) + (1.5% + 0.1%).
    CHECK(r[1][0] == doctest::Approx(1000.0 * (183 * 0.05 + 182 * 0.056) / 365));
    CHECK(r[1][1] == doctest::Approx(1000.0 * (183 * 0.03 + 182 * 0.04) / 365));
    CHECK(r[1][2] == doctest::Approx(1000.0 * (183 * 0.02 + 182 * 0.016) / 365));
    // Year 2 (366 days): 1 day at the year-1 rate, then two replacements at year-2 rates (5% + 1.7%).
    CHECK(r[2][0] == doctest::Approx(1000.0 * (1 * 0.056 + 365 * 0.067) / 366));
    // Adverse year 1: 3% + 2% reference, 1.5% + 0.2% margin.
    CHECK(r[4][0] == doctest::Approx(1000.0 * (183 * 0.05 + 182 * 0.067) / 365));
}

TEST_CASE("nii: floating position resets the reference rate, keeps its margin") {
    auto p = performing(1000.0, 0.02, 0.01);
    p.floating = true;
    p.freq = 3;
    p.next_reset = k20260930;   // then 2026-12-30, 2027-03-30, 2027-06-30 (= start of year 2)
    p.margin_new = 0.5;         // never used: no maturity
    const auto r = project_position(p, kBounds);
    CHECK(r[1][0] == doctest::Approx(1000.0 * (92 * 0.03 + 273 * 0.04) / 365));
    CHECK(r[1][2] == doctest::Approx(10.0));                             // margin unchanged
    CHECK(r[2][0] == doctest::Approx(1000.0 * (0.02 + 0.02 + 0.01)));    // whole year 2 at the year-2 rate
    CHECK(r[6][0] == doctest::Approx(1000.0 * (0.02 + 0.06 + 0.01)));    // adverse year 3
}

TEST_CASE("nii: floating reset dates roll from origination when the next reset date is missing") {
    auto p = performing(1000.0, 0.02, 0.01);
    p.floating = true;
    p.freq = 12;
    p.origination = k20240131;   // resets every 31 January: next reset 2027-01-31
    const auto r = project_position(p, kBounds);
    const Date reset = add_months(k20240131, 36);
    CHECK(r[1][0] == doctest::Approx(1000.0 * ((reset - k20260630) * 0.03 + (k20270630 - reset) * 0.04) / 365));
}

TEST_CASE("nii: a position past maturity is replaced on the first day") {
    auto p = performing(1000.0, 0.03, 0.02);
    p.maturity = k20251231;
    p.term = 184;
    p.margin_new = 0.01;
    const auto r = project_position(p, kBounds);
    CHECK(r[1][0] == doctest::Approx(1000.0 * (0.03 + 0.01 + 0.01 + 0.001)));
}

TEST_CASE("nii: sight deposits reprice every year with pass-through and the household zero floor") {
    auto p = performing(100.0, 0.02, -0.019);
    p.row = 28;
    p.sight = true;
    p.beta = 0.5;
    p.floor_zero = true;
    for (auto& s : p.shock) s = {0.0, 0.0, 0.0};
    for (std::size_t y = 0; y < 3; ++y) p.delta[0][y] = -0.01;
    const auto r = project_position(p, kBounds);
    CHECK(r[1][0] == doctest::Approx(0.0));      // 2% - 0.5% - 1.9% < 0: EIR floored at 0 via the reference rate
    CHECK(r[1][1] == doctest::Approx(1.9));
    CHECK(r[1][2] == doctest::Approx(-1.9));
    CHECK(r[4][0] == doctest::Approx(100.0 * (0.02 + 0.5 * 0.02 - 0.019)));   // adverse: +2% x 0.5
    p.floor_zero = false;                        // e.g. NFC sight deposits: no zero floor
    CHECK(project_position(p, kBounds)[1][0] == doctest::Approx(100.0 * (0.015 - 0.019)));
}

TEST_CASE("nii: non-performing assets earn the EIR on the net exposure") {
    Position p;
    p.row = 5;
    p.performing = false;
    p.volume = 100.0;
    p.provisions = 60.0;
    p.net_volume = 40.0;
    p.eir = 0.05;
    const auto r = project_position(p, kBounds);
    for (const auto& slot : r) {
        CHECK(slot[0] == doctest::Approx(2.0));
        CHECK(slot[1] == 0.0);
        CHECK(slot[2] == 0.0);
    }
}

TEST_CASE("nii: template rows and the idiosyncratic shock table") {
    CHECK(template_row(4).asset);
    CHECK(template_row(4).factor == 0.15);
    CHECK(!template_row(29).asset);
    CHECK(template_row(29).factor == 0.5);
    CHECK_THROWS(template_row(12));
    CHECK(*idiosyncratic_shock_bps("A") == 50);
    CHECK(*idiosyncratic_shock_bps("BBB-") == 95);
    CHECK(*idiosyncratic_shock_bps("B-") == 175);
    CHECK(!idiosyncratic_shock_bps("A1"));
}
