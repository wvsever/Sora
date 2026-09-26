#pragma once
// Scenario configuration (YAML), normalised macro paths (CSV from `sora-tools scenario-import`) and
// satellite model coefficients.

#include <array>
#include <filesystem>
#include <map>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

#include "sora/calibration.hpp"
#include "sora/segmentation.hpp"

namespace sora {

// ECB credit-risk benchmark rule (benchmark.hpp). Enabled by the scenario key `benchmark_parameters`.
struct BenchmarkConfig {
    bool enabled = false;
    std::filesystem::path file;                 // Sora's benchmark format (plans/09_risk_parameters.md)
    double coverage_threshold = 0.10;           // model coverage per pivot asset class (MN 2027 para 117)
    int model_level = 1;                        // coarsest calibration level that counts as a model: 0 segment, 1 portfolio
    bool sovereign = true;                      // general governments: the country's benchmark is mandatory (para 146)
    std::vector<std::string> country_fallback;  // benchmark keys after the segment's country (default: country_fallback)
};

// Off-balance-sheet projection (CR_SCEN_OFF_BS, off_balance.hpp). Enabled by the scenario key `off_balance`;
// the measurement and intragroup scope are those of `scope`.
struct OffBalanceConfig {
    bool enabled = false;
    std::vector<ExposureType> types;   // loan_commitment, financial_guarantee, other_commitment
    // Regulatory CCF when no customer CCF is supplied (CRR Art. 111(2), Annex I buckets).
    double ccf_loan_commitment = 0.4, ccf_financial_guarantee = 1.0, ccf_other_commitment = 0.5;
    double ccf_unconditionally_cancellable = 0.1;   // loan and other commitments cancellable at any time
    // Facilities with a drawn and an undrawn part (plans/03_scenario_engine.md, Off-balance-sheet exposures):
    // include_loan_undrawn: the undrawn part of in-scope loans is a loan commitment given, projected here in the
    // loan's segment; commitment_drawn_on_balance: the drawn part (GCA) of commitments of `types` is an on-balance
    // loans-and-advances exposure (ScopeConfig::drawn_types). The allowance is split pro rata in both cases.
    bool include_loan_undrawn = false;
    bool commitment_drawn_on_balance = false;
};

// Sectoral (GVA) satellites for NFC exposures by NACE sector (scenario key `sector_satellites`; EBA MN 2027 draft
// para 114, 2025 MN para 123; plans/03_scenario_engine.md, Sectoral satellites).
struct SectorSatelliteConfig {
    bool enabled = false;
    std::filesystem::path file;                              // sector, beta_gva, lgd_gva_sensitivity (CSV)
    // GVA key for a country without sectoral GVA paths (the scenario has the EU 27, EA and EU only): the sector's
    // GVA deviation from GDP in the first of these keys, added to the country's GDP growth.
    std::vector<std::string> gva_fallback{"EU"};
};

struct ScenarioConfig {
    std::string name;
    std::filesystem::path macro_path;
    std::filesystem::path satellites_path;
    std::map<int, int> year_map;            // projection year -> scenario year
    int history_year = 0;
    double normal_gdp_growth = 0.0;
    std::vector<std::string> country_fallback;
    ScopeConfig scope;
    CalibrationConfig calibration;
    bool no_cure_from_s3 = true;
    double blend_adverse = 5.0 / 6.0, blend_baseline = 1.0 / 6.0;
    // Prior-year Actual rows of CR_SCEN / CR_SECTOR (prior_year.hpp): YYYY-MM-DD, empty = 31 December of the
    // year before the reference date's year (scenario key prior_year_end).
    std::string prior_year_end;
    OffBalanceConfig off_balance;
    BenchmarkConfig benchmark;
    SectorSatelliteConfig sector_satellites;
};

// Relative paths in the YAML resolve against `base_dir` (the repository or working directory).
ScenarioConfig load_scenario(const std::filesystem::path& yaml, const std::filesystem::path& base_dir);

class MacroTable {
public:
    std::optional<double> get(const std::string& variable, const std::string& key, const std::string& scenario, int year) const;
    void set(const std::string& variable, const std::string& key, const std::string& scenario, int year, double v);
    std::size_t size() const noexcept { return values_.size(); }

private:
    static std::string k(const std::string& v, const std::string& key, const std::string& s, int y);
    std::unordered_map<std::string, double> values_;
};

MacroTable load_macro(Duck& duck, const std::filesystem::path& csv);

struct Satellite {
    double beta_gdp = 0, beta_unemployment = 0, beta_property = 0, lgd_property_sensitivity = 0;
};
std::map<std::string, Satellite> load_satellites(Duck& duck, const std::filesystem::path& csv);

// Sectoral satellite of one CR_SECTOR sector. A coefficient that is not set means no sectoral model for its group.
//   PD/TR (beta_gva): the sector's real GVA growth replaces GDP growth in the portfolio satellite's index:
//     z = beta_gva * (gva_t - normal_gdp_growth) + beta_unemployment * (u_t - u_0) + beta_property * hp_t
//   LGD/LR (lgd_gva_sensitivity): LGD_t = LGD_0 * (1 + lgd_property_sensitivity * max(0, 1 - I_prop,t)
//                                                   + lgd_gva_sensitivity * max(0, 1 - I_gva,t)), capped at 1,
//     I_gva,t = cumulative real GVA index of the sector.
// The other terms are the portfolio's (zero when the portfolio has no satellite coefficients).
struct SectorSatellite {
    std::optional<double> beta_gva, lgd_gva_sensitivity;
    bool covers(std::size_t group) const noexcept { return group == 0 ? beta_gva.has_value() : lgd_gva_sensitivity.has_value(); }
};
using SectorSatellites = std::array<std::optional<SectorSatellite>, kNaceSectors>;   // by NaceSector
// CSV with `#` comment lines: sector (sector_code), beta_gva, lgd_gva_sensitivity (empty = none), description.
// Unknown or duplicate sectors and rows without coefficients are errors.
SectorSatellites load_sector_satellites(Duck& duck, const std::filesystem::path& csv);

// The sectoral satellite of a sector for the exposures of one segment, with its GVA path: the scenario's real GVA of
// the sector (gva_sector) for the segment's macro key; for a key without sectoral GVA, GDP growth of the macro key plus
// the sector's GVA deviation from GDP in the first gva_fallback key that has it (`relative`).
struct SectorModel {
    NaceSector sector = NaceSector::Unknown;
    SectorSatellite coef;
    std::string gva_variable;   // real_gva:<scenario sector>
    std::string macro_key, gva_key;
    bool relative = false;
};
SectorModel sector_model(const MacroTable& macro, const Segment& seg, NaceSector sector, const SectorSatellite& coef,
                         const ScenarioConfig& cfg);
// Real GVA growth (%) of the model's path in a scenario year.
double sector_growth(const SectorModel& m, const MacroTable& macro, const std::string& scenario, int year);

// Macro key used for a country bucket (falls back to aggregates when the country has no scenario data).
std::string macro_key(const MacroTable& macro, const std::string& bucket, const ScenarioConfig& cfg);

// Parameters for years 1..3 plus year 4 (flat continuation) under one scenario. Index 0 is the starting point.
// `sector`: the exposure's sectoral satellite (groups it covers replace the portfolio model's), or null.
using ParamPath = std::array<Params, 5>;
ParamPath project_parameters(const Segment& seg, const Params& p0, const Satellite& sat, const MacroTable& macro,
                             const std::string& scenario, const ScenarioConfig& cfg, const SectorModel* sector = nullptr);

}  // namespace sora
