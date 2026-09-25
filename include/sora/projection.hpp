#pragma once
// EBA credit-risk projection (2027 draft methodological note, Boxes 3-9), computed per exposure and
// aggregated per segment. Each exposure carries fractional stage masses (S1, S2, new S3) so that the
// segment result equals the EBA formula on segment totals while staying attributable to contracts.

#include <array>
#include <string>
#include <vector>

#include "sora/dataset.hpp"
#include "sora/scenario.hpp"

namespace sora {

// Output fields per segment, scenario and year, in the column order of projection.csv.
struct YearResult {
    double exp_s1 = 0, exp_s2 = 0, exp_s3_old = 0, exp_s3_new = 0, exp_poci = 0;
    double flow_s1_s2 = 0, flow_s2_s1 = 0, flow_s1_s3 = 0, flow_s2_s3 = 0;
    double prov_s1_s1 = 0, prov_s2_s1 = 0, prov_s1_s2 = 0, prov_s2_s2 = 0;
    double prov_cum_s1_s3 = 0, prov_cum_s2_s3 = 0, prov_old_s3 = 0;
    double prov_stock_s1 = 0, prov_stock_s2 = 0, prov_stock_s3 = 0, prov_stock_poci = 0;
    double impairment = 0;
};
inline constexpr std::array<const char*, 21> kYearResultFields = {
    "exp_s1", "exp_s2", "exp_s3_old", "exp_s3_new", "exp_poci",
    "flow_s1_s2", "flow_s2_s1", "flow_s1_s3", "flow_s2_s3",
    "prov_s1_s1", "prov_s2_s1", "prov_s1_s2", "prov_s2_s2",
    "prov_cum_s1_s3", "prov_cum_s2_s3", "prov_old_s3",
    "prov_stock_s1", "prov_stock_s2", "prov_stock_s3", "prov_stock_poci", "impairment"};
std::array<double, 21> fields(const YearResult& r);

inline constexpr std::array<const char*, 2> kScenarios = {"baseline", "adverse"};

struct Projection {
    // [segment][scenario 0=baseline,1=adverse][year 0..2 = years 1..3]
    std::vector<std::array<std::array<YearResult, 3>, 2>> results;
    // [segment][scenario] parameter paths
    std::vector<std::array<ParamPath, 2>> params;
};

// Projects one exposure (reporting-currency amounts) and adds its contribution to `acc`.
// Exposed for unit tests; `paths[0]` is the baseline path, `paths[1]` the adverse path.
void project_exposure(Stage stage, double gca, double allowance, const std::array<ParamPath, 2>& paths,
                      const ScenarioConfig& cfg, std::array<std::array<YearResult, 3>, 2>& acc);

Projection project(const Dataset& d, const Segmentation& s, const Calibration& cal,
                   const std::map<std::string, Satellite>& satellites, const MacroTable& macro,
                   const ScenarioConfig& cfg);

}  // namespace sora
