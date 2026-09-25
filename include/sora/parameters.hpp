#pragma once
// Customer-supplied risk parameters (SIM table sim_risk_parameter, or a separate file).
//
// Rows are keyed by (level, key, scenario, year):
//   level = segment:  key is a segmentation key at any hierarchy level, e.g. LOANS|NFC_SME_CRE|BE,
//                     LOANS|NFC_SME_CRE|ALL, LOANS|ALL|ALL or ALL|ALL|ALL
//   level = exposure: key is an exposure_id
// scenario = actual with year 0 is the starting point; baseline/adverse with year 1..3 are projections.
// Every parameter is optional (NULL = not supplied). Precedence per field, most specific first:
//   exposure row > segment row (segment, then portfolio, instrument, all) > Sora's own value
//   (derived calibration for the starting point, satellite projection for later years).

#include <array>
#include <filesystem>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

#include "sora/calibration.hpp"
#include "sora/segmentation.hpp"

namespace sora {

inline constexpr std::size_t kParamCount = 10;
inline constexpr std::array<const char*, kParamCount> kParamNames = {
    "pd12m_s1", "pd12m_s2", "tr1_2", "tr2_1", "tr3_1", "tr3_2", "lgd_s1", "lgd_s2", "lgd_s3", "lrlt_s2"};
double& param_field(Params& p, std::size_t i);
double param_field(const Params& p, std::size_t i);

struct OptParams {
    std::array<std::optional<double>, kParamCount> v{};
    bool any() const;
};

// Scenario index: 0 = actual (year 0), 1 = baseline, 2 = adverse; year 0..3.
struct ParamKey {
    int scenario = 0;
    int year = 0;
    bool operator==(const ParamKey&) const = default;
};

class ExternalParameters {
public:
    // Loads from a FROM-clause source (read_parquet/read_csv). Unknown scenarios or levels are errors.
    void load(Duck& duck, const std::string& source, const Dataset& d);
    bool empty() const noexcept { return rows_ == 0; }
    std::size_t rows() const noexcept { return rows_; }

    // Field-wise overlay for a segment (walks the hierarchy from general to specific, so specific wins).
    // Returns the number of fields set.
    std::size_t apply_segment(const Segmentation& s, const Segment& seg, ParamKey k, Params& p) const;
    // Field-wise overlay for one exposure (by exposure index). Returns the number of fields set.
    std::size_t apply_exposure(std::size_t exposure, ParamKey k, Params& p) const;
    bool has_exposure(std::size_t exposure) const { return by_exposure_.count(exposure) != 0; }

    // Segment-level keys that match no hierarchy key of the segmentation.
    std::vector<std::string> unmatched_segment_keys(const Segmentation& s) const;

    std::vector<std::string> unknown_keys;   // keys that match no segment level or exposure (diagnostics)

private:
    static std::size_t slot(ParamKey k) { return static_cast<std::size_t>(k.scenario == 0 ? 0 : (k.scenario - 1) * 3 + k.year); }
    using Slots = std::array<OptParams, 7>;   // actual/0, baseline/1..3, adverse/1..3
    std::unordered_map<std::string, Slots> by_level_;
    std::unordered_map<std::size_t, Slots> by_exposure_;
    std::size_t rows_ = 0;
};

// Validation of supplied values (range [0,1] and stage outflows <= 1). Messages for diagnostics.
std::vector<std::string> check_parameters(const Params& p);

}  // namespace sora
