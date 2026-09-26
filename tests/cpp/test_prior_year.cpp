// Prior-year Actual rows (CR_SCEN, CR_SECTOR): the date rule, the stocks per exposure from the stage history, and
// which template cells a missing amount blanks.

#include "doctest.h"

#include <cmath>
#include <filesystem>
#include <fstream>

#include "sora/prior_year.hpp"

using namespace sora;
namespace fs = std::filesystem;

TEST_CASE("prior year-end: 31 December of the year before the reference date, or a configured month end") {
    CHECK(prior_year_end("2026-06-30", "") == "2025-12-31");
    CHECK(prior_year_end("2026-12-31", "") == "2025-12-31");   // EBA: t0 = end of 2026, historical end of 2025
    CHECK(prior_year_end("2027-01-31", "") == "2026-12-31");
    CHECK(prior_year_end("2026-06-30", "2025-06-30") == "2025-06-30");   // t0 minus 12 months
    CHECK(prior_year_end("2025-03-31", "2024-02-29") == "2024-02-29");   // leap year
    CHECK_THROWS_AS(prior_year_end("2026-06-30", "2025-06-29"), Error);  // not a month end
    CHECK_THROWS_AS(prior_year_end("2024-06-30", "2023-02-29"), Error);
    CHECK_THROWS_AS(prior_year_end("2026-06-30", "2026-03-31"), Error);  // same year as the reference date
    CHECK_THROWS_AS(prior_year_end("2026-06-30", "2025-13-31"), Error);
    CHECK_THROWS_AS(prior_year_end("2026-06-30", "31.12.2025"), Error);
}

TEST_CASE("prior-year cells: exposure cells need every amount, provision cells every FX rate") {
    CHECK(prior_cell_known("of which: stage 1 (Exp S1)", false, true, true));
    CHECK(!prior_cell_known("of which: stage 1 (Exp S1)", false, false, true));
    CHECK(!prior_cell_known("Total exposure (total Exp)", false, false, true));
    CHECK(!prior_cell_known("Performing exposure (Exp)", false, false, true));   // CR_SECTOR header
    CHECK(!prior_cell_known("POCI exposures (Exp POCI)", false, false, true));
    CHECK(prior_cell_known("of which: stage 1 (Prov Stock S1)", false, false, true));
    CHECK(!prior_cell_known("of which: stage 1 (Prov Stock S1)", false, true, false));
    CHECK(!prior_cell_known("Coverage ratio: performing exposure", true, false, true));
    CHECK(!prior_cell_known("Coverage ratio: performing exposure", true, true, false));
    CHECK(prior_cell_known("Coverage ratio: performing exposure", true, true, true));
    CHECK(prior_cell_known("PD/TR - Percentage of exposures for which ECB benchmark parameters were used (%)", true, false, false));
}

namespace {

struct Book {
    Dataset d;
    ScopeConfig scope;
    Segmentation s;
};

// Loans L1..L8 (L8 at FVOCI), in EUR unless stated. t0 stage S1 everywhere.
Book book(const fs::path& dir) {
    Book b;
    b.d.sim_dir = dir;
    b.d.manifest.reference_date = "2026-06-30";
    b.d.manifest.reporting_currency = "EUR";
    for (const char* c : {"EUR", "USD", "GBP"}) b.d.currencies.intern(c);
    b.d.fx_to_reporting = {1'000'000'000, 800'000'000, 1'100'000'000};
    Counterparty cp;
    cp.country = b.d.countries.intern("BE");
    cp.sector = EbaSector::Household;
    b.d.counterparties.push_back(cp);
    b.d.counterparty_ids.intern("CP1");
    struct X { const char* id; std::uint32_t ccy; Cents gca, undrawn; Measurement m; };
    const X xs[] = {{"L1", 0, 100000, 0, Measurement::AmortisedCost}, {"L2", 1, 50000, 0, Measurement::AmortisedCost},
                    {"L3", 0, 70000, 30000, Measurement::AmortisedCost}, {"L4", 0, 30000, 10000, Measurement::AmortisedCost},
                    {"L5", 2, 10000, 0, Measurement::AmortisedCost}, {"L6", 0, 10000, 0, Measurement::AmortisedCost},
                    {"L7", 0, 10000, 0, Measurement::AmortisedCost}, {"L8", 0, 10000, 0, Measurement::Fvoci}};
    for (const auto& x : xs) {
        Exposure e;
        e.id = b.d.exposure_ids.intern(x.id);
        e.counterparty = 0;
        e.currency = x.ccy;
        e.measurement = x.m;
        e.stage = Stage::S1;
        e.has_gca = true;
        e.gca = x.gca;
        e.off_balance = x.undrawn;
        e.allowance = 100;
        e.purpose = HouseholdPurpose::Consumption;
        b.d.exposures.push_back(e);
    }
    b.scope.loan_undrawn_off_balance = true;
    b.s = segment(b.d, b.scope);
    return b;
}

void write(const fs::path& p, const std::string& text) {
    fs::create_directories(p.parent_path());
    std::ofstream(p, std::ios::binary) << text;
}

}  // namespace

TEST_CASE("prior-year stocks from the stage history: amount, principal proxy, FX, allowance split, missing data") {
    const auto dir = fs::temp_directory_path() / "sora_prior_year_sim";
    fs::remove_all(dir);
    write(dir / "sim_fx_rate" / "part.csv", "currency,rate_date,rate_to_reporting\nUSD,2025-12-31,0.900000000\n"
                                            "USD,2026-06-30,0.800000000\nGBP,2026-06-30,1.100000000\n");
    write(dir / "sim_stage_history" / "part.csv",
          "exposure_id,entity_id,period_end,stage,currency,gross_carrying_amount,off_balance_amount,loss_allowance,"
          "write_off_in_period,principal_outstanding\n"
          "L1,E,2025-11-30,stage1,EUR,999.00,,1.00,,\n"
          "L1,E,2025-12-31,stage2,EUR,1000.00,,10.00,,\n"
          "L2,E,2025-12-31,stage1,USD,,,5.00,,500.00\n"      // principal proxy, USD at the prior-date rate
          "L3,E,2025-12-31,stage1,EUR,400.00,,20.00,,\n"     // undrawn unknown: t0 drawn share 0.7
          "L4,E,2025-12-31,stage3,EUR,300.00,100.00,8.00,,\n"   // undrawn in the history: 300 / 400
          "L5,E,2025-12-31,stage1,GBP,100.00,,1.00,,\n"      // no GBP rate at the prior date
          "L7,E,2025-12-31,poci,EUR,,,3.00,,\n"              // no amount
          "L8,E,2025-12-31,stage1,EUR,100.00,,1.00,,\n"      // FVOCI: out of the t0 scope
          "X9,E,2025-12-31,stage1,USD,100.00,,2.00,,\n");    // derecognised: not in sim_exposure
    Book b = book(dir);
    Duck duck;
    const auto p = load_prior_year(duck, b.d, b.s, b.scope, prior_year_end(b.d.manifest.reference_date, ""));
    REQUIRE(p.available);
    CHECK(p.date == "2025-12-31");
    CHECK(p.year == 2025);
    CHECK(p.history_rows == 8);
    CHECK(p.exposures == 6);
    CHECK(p.amount_gca == 4);
    CHECK(p.amount_principal == 1);
    CHECK(p.missing_amount == 1);
    CHECK(p.missing_fx == 1);
    CHECK(p.allowance_split_history == 1);
    CHECK(p.allowance_split_t0_share == 1);
    CHECK(p.out_of_scope == 1);
    CHECK(p.not_in_sim_exposure == 1);
    CHECK(p.not_in_sim_exposure_allowance == doctest::Approx(1.8));
    auto ix = [&](const char* id) { return static_cast<std::size_t>(*b.d.exposure_ids.find(id)); };
    CHECK(p.stage[ix("L1")] == Stage::S2);
    CHECK(p.exposure[ix("L1")] == 1000.0);
    CHECK(p.allowance[ix("L1")] == 10.0);
    CHECK(p.exposure[ix("L2")] == doctest::Approx(450.0));
    CHECK(p.allowance[ix("L2")] == doctest::Approx(4.5));
    CHECK(p.exposure[ix("L3")] == 400.0);
    CHECK(p.allowance[ix("L3")] == doctest::Approx(14.0));
    CHECK(p.stage[ix("L4")] == Stage::S3);
    CHECK(p.allowance[ix("L4")] == doctest::Approx(6.0));
    CHECK(std::isnan(p.exposure[ix("L5")]));
    CHECK(std::isnan(p.allowance[ix("L5")]));
    CHECK(p.stage[ix("L6")] == Stage::NotApplicable);   // no history row: not on the balance sheet then
    CHECK(std::isnan(p.exposure[ix("L7")]));
    CHECK(p.allowance[ix("L7")] == 3.0);
    CHECK(p.stage[ix("L8")] == Stage::NotApplicable);

    PriorStock all;
    for (std::size_t i = 0; i < b.d.exposures.size(); ++i) all.add(p, i);
    CHECK(all.contracts == 6);
    CHECK(all.missing_amount == 1);
    CHECK(all.missing_fx == 1);
    CHECK(!all.exposures_known(true));
    CHECK(!all.provisions_known(true));
    PriorStock known;   // L1..L4: complete
    for (const char* id : {"L1", "L2", "L3", "L4"}) known.add(p, ix(id));
    CHECK(known.exposures_known(true));
    CHECK(!known.exposures_known(false));
    CHECK(known.exp[0] == doctest::Approx(850.0));
    CHECK(known.exp[1] == 1000.0);
    CHECK(known.prov[2] == doctest::Approx(6.0));

    // A later prior year-end without history rows: nothing available; the principal column is optional.
    write(dir / "sim_stage_history" / "part.csv",
          "exposure_id,entity_id,period_end,stage,currency,gross_carrying_amount,off_balance_amount,loss_allowance\n"
          "L1,E,2025-12-31,stage1,EUR,,,1.00\n");
    const auto q = load_prior_year(duck, b.d, b.s, b.scope, "2025-06-30");
    CHECK(!q.available);
    CHECK(q.exposures == 0);
    const auto r = load_prior_year(duck, b.d, b.s, b.scope, "2025-12-31");
    CHECK(r.available);
    CHECK(r.missing_amount == 1);
    fs::remove_all(dir);
}
