#pragma once
// Off-balance-sheet exposures (loan commitments, financial guarantees and other commitments given) and the EBA
// CSV_CR_SCEN_OFF_BS template (2027 draft methodological note, paras 78-82; plans/03_scenario_engine.md).
//
// Each item (a sim_exposure row of an off-balance type, nominal = off_balance_amount) is projected with the
// parameter path of the on-balance loan segment of its counterparty (same portfolio rules and country bucket;
// the portfolio's OTHER bucket when that segment has no loans), with the on-balance stage-flow and provision logic
// (Boxes 3-9): the post-CCF amount CCF x nominal carries the flows and provisions, and the nominal amount follows
// the same stage flows. CCF: customer parameter (`ccf`, actual/0, exposure row then segment hierarchy), else the
// regulatory fallback of the scenario (CRR Art. 111(2)). The starting provision is the undrawn share of the
// facility's loss allowance. The balance sheet is static, POCI is static and there are no cures from stage 3.

#include <array>
#include <filesystem>
#include <iosfwd>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

#include "sora/projection.hpp"

namespace sora {

// Customer CCFs (column `ccf` of the risk-parameter source, scenario actual, year 0).
class CustomerCcf {
public:
    // No-op when the source has no `ccf` column. Values outside [0, 1] are errors.
    void load(Duck& duck, const std::string& source, const Dataset& d);
    // Exposure row first, then the segment hierarchy (most specific level first).
    std::optional<double> find(std::size_t exposure, const Segmentation& s, const Segment& seg) const;
    void set_exposure(std::size_t exposure, double ccf) { by_exposure_[exposure] = ccf; }
    void set_level(const std::string& key, double ccf) { by_level_[key] = ccf; }
    bool empty() const noexcept { return by_exposure_.empty() && by_level_.empty(); }

private:
    std::unordered_map<std::size_t, double> by_exposure_;
    std::unordered_map<std::string, double> by_level_;
};

// Regulatory fallback CCF of the scenario for an item without a customer CCF.
double fallback_ccf(ExposureType type, bool unconditionally_cancellable, const OffBalanceConfig& c);

// One (parameter segment, exposure type) group. `nominal` carries the nominal amounts through the same stage flows
// (only its exp_* fields are used), `post` the post-CCF amounts, flows, provisions and impairment.
struct OffBalanceGroup {
    std::size_t segment = 0;   // index into Segmentation::segments (an on-balance LOANS segment)
    ExposureType type = ExposureType::LoanCommitment;
    std::size_t items = 0;
    std::array<double, 4> nominal0{}, post0{}, provision0{};   // starting point: S1, S2, S3, POCI
    std::array<std::array<YearResult, 3>, 2> nominal{}, post{};   // [scenario][year 1..3]
};

// Adds one item (reporting-currency amounts) to `g`. Exposed for unit tests.
void project_off_balance_item(Stage stage, double nominal, double ccf, double provision,
                              const std::array<ParamPath, 2>& paths, const ScenarioConfig& cfg, OffBalanceGroup& g);

struct OffBalanceResult {
    std::vector<OffBalanceGroup> groups;   // sorted by segment key, then exposure type name
    std::vector<ExposureType> types;       // configured exposure types
    std::size_t items = 0, fallback_items = 0, unmatched_items = 0, customer_ccf_items = 0;
    std::size_t exposures_with_own_parameters = 0;
};

// `parameter_source` is the FROM-clause source of the customer parameters (empty: none); `external` their
// PD/LGD overlay (null: none). Deterministic: groups are projected in parallel by `workers` threads, each group
// entirely by one worker in exposure order, so results are bit-identical for any number of workers.
// Invalid exposure-level parameters of an item throw sora::Error.
OffBalanceResult project_off_balance(Duck& duck, const Dataset& d, const Segmentation& s, const Projection& p,
                                     const std::map<std::string, Satellite>& satellites, const MacroTable& macro,
                                     const ScenarioConfig& cfg, const ExternalParameters* external,
                                     const std::string& parameter_source, unsigned workers);

// off_balance.csv (per segment, exposure type, scenario and year) and cr_scen_off_bs.csv (EBA layout).
void write_off_balance(const Dataset& d, const Segmentation& s, const OffBalanceResult& r, const std::filesystem::path& dir);
// The value of summary.json "off_balance" (a JSON object, indented for nesting at the top level).
void write_off_balance_summary(std::ostream& f, const OffBalanceResult& r);

}  // namespace sora
