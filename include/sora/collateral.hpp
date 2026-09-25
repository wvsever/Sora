#pragma once
// Collateral value paths under the macro scenarios and LTV reporting (see plans/03_scenario_engine.md,
// "Collateral repricing and LTV").
//
// Real-estate collateral is revalued with the cumulative residential or commercial property price growth
// of its property country; other collateral keeps its value. LTV is static-balance-sheet: exposures keep
// their t0 stage and gross carrying amount, and only the collateral values move. The LGD model is not
// affected (it stays the segment-level satellite multiplier).

#include <array>
#include <filesystem>
#include <vector>

#include "sora/scenario.hpp"

namespace sora {

// Loads sim_collateral and sim_collateral_allocation into `d` (no-op if the tables are absent).
// Needs the exposures loaded. Allocations to unknown exposures or collateral are errors.
void load_collateral(Duck& duck, Dataset& d);

// Output slots, as in cr_scen: 0 = actual (year 0), 1..3 = baseline years 1..3, 4..6 = adverse years 1..3.
inline constexpr std::size_t kCollateralSlots = 7;

// Collateral value index per scenario (0 = baseline, 1 = adverse) and year 0..3 (year 0 = 1).
// Property types use the property price growth of `country`, falling back like macro_key; other types are 1.
using CollateralIndex = std::array<std::array<double, 4>, 2>;
CollateralIndex collateral_index(CollateralType type, const std::string& country, const MacroTable& macro,
                                 const ScenarioConfig& cfg);

// Additive LTV aggregates per stage (S1, S2, S3), in the reporting currency.
struct LtvCell {
    std::array<double, 3> secured_exp{};   // t0 gross carrying amount of exposures with real-estate collateral
    std::array<double, 3> re_value{};      // real-estate collateral value allocated to them
    void add(const LtvCell& o) {
        for (std::size_t i = 0; i < 3; ++i) { secured_exp[i] += o.secured_exp[i]; re_value[i] += o.re_value[i]; }
    }
};

struct CollateralResult {
    std::vector<std::array<LtvCell, kCollateralSlots>> cells;   // [segment][slot]
    std::size_t secured_exposures = 0;                          // in scope, with real-estate collateral
    std::size_t pro_rata_allocations = 0;                       // in-scope allocations without an amount
};

// Allocated values: `allocated_amount` converted at the reference-date FX rate of the collateral currency.
// A NULL amount gets the collateral's market value pro rata to the gross carrying amount of all in-scope
// exposures the collateral is allocated to (equal shares if that total is 0). Out-of-scope allocations
// are ignored.
CollateralResult collateral_ltv(const Dataset& d, const Segmentation& s, const MacroTable& macro,
                                const ScenarioConfig& cfg);

// collateral.csv: segment, scenario, year, secured_exp_s1..3, re_collateral_s1..3, ltv_s1..3.
void write_collateral(const Segmentation& s, const CollateralResult& c, const std::filesystem::path& file);

}  // namespace sora
