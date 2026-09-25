#pragma once
// EBA credit-risk projection (2027 draft methodological note, Boxes 3-9), computed per exposure and
// aggregated per segment. Each exposure carries fractional stage masses (S1, S2, new S3) so that the
// segment result equals the EBA formula on segment totals while staying attributable to contracts.

#include <array>
#include <string>
#include <vector>

#include "sora/dataset.hpp"
#include "sora/parameters.hpp"
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

// Exposure-weighted parameter sums for reporting (EBA templates ask for exposure-weighted averages).
// Weights: stage 1 exposure at the start of the year for pd12m_s1, tr1_2, lgd_s1; stage 2 exposure for
// pd12m_s2, tr2_1, lgd_s2, lrlt_s2; existing stage 3 exposure for tr3_1, tr3_2, lgd_s3.
struct ParamAccum {
    std::array<double, 3> weight{};              // S1, S2, S3 exposure
    std::array<double, kParamCount> sum{};       // sum of parameter x weight
    double average(std::size_t param) const;     // NaN if the weight is 0
};
std::size_t param_weight_stage(std::size_t param);   // 0 = S1, 1 = S2, 2 = S3

struct Projection {
    // [segment][scenario 0=baseline,1=adverse][year 0..2 = years 1..3]
    std::vector<std::array<std::array<YearResult, 3>, 2>> results;
    // [segment][scenario] parameter paths (index 0 = effective starting point)
    std::vector<std::array<ParamPath, 2>> params;
    // [segment]: where the starting point came from: derived | external | mixed
    std::vector<std::string> start_source;
    // [segment][scenario][year 1..3 at index 0..2]: derived | external | mixed
    std::vector<std::array<std::array<std::string, 3>, 2>> path_source;
    // [segment][scenario][year 0..3] exposure-weighted parameters, including exposure-level overrides
    std::vector<std::array<std::array<ParamAccum, 4>, 2>> accum;
    std::size_t exposures_with_own_parameters = 0;
    std::vector<std::string> parameter_errors;
};

// Projects one exposure (reporting-currency amounts) and adds its contribution to `acc`.
// Exposed for unit tests; `paths[0]` is the baseline path, `paths[1]` the adverse path.
void project_exposure(Stage stage, double gca, double allowance, const std::array<ParamPath, 2>& paths,
                      const ScenarioConfig& cfg, std::array<std::array<YearResult, 3>, 2>& acc,
                      std::array<std::array<ParamAccum, 4>, 2>* param_acc = nullptr);

// `external` may be null (derived parameters only). `workers` threads project the segments in parallel
// (0 = hardware concurrency); the results are bit-identical for any number of workers.
Projection project(const Dataset& d, const Segmentation& s, const Calibration& cal,
                   const std::map<std::string, Satellite>& satellites, const MacroTable& macro,
                   const ScenarioConfig& cfg, const ExternalParameters* external = nullptr, unsigned workers = 1);

}  // namespace sora
