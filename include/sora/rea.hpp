#pragma once
// IRB risk exposure amounts (REA) and expected loss per segment, scenario and year, computed by the external
// regulatory calculator (POST /v1/credit-risk/irb) from the projection's parameters. See plans/11_integrations.md.
//
// Records: one per in-scope stage 1/2/3 exposure (POCI skipped), per point: actual/0, baseline/1..3,
// adverse/1..3; recordId = exposure_id|scenario|year. EAD is the t0 gross carrying amount in the reporting
// currency (static balance sheet). PD/LGD are the year's PiT IFRS 9 parameters (pd12m_s1/lgd_s1 for stage 1,
// pd12m_s2/lgd_s2 for stage 2; stage 3 is defaulted with LGD = ELBE = lgd_s3) as a proxy for PDreg/LGDreg,
// unless the risk-parameter source supplies pd_reg/lgd_reg.

#include <array>
#include <filesystem>
#include <map>
#include <ostream>
#include <string>
#include <vector>

#include "sora/calculator.hpp"
#include "sora/projection.hpp"
#include "sora/validation.hpp"

namespace sora {

inline constexpr std::size_t kReaSlots = 7;   // actual/0, baseline/1..3, adverse/1..3
const char* rea_slot_scenario(std::size_t slot);
int rea_slot_year(std::size_t slot);

struct ReaCell {
    Cents ead = 0, rea = 0, expected_loss = 0;   // accepted (ok) records only
    std::uint64_t ok = 0, rejected = 0;
};

struct ReaResult {
    std::string calculator, version, param_set;
    std::vector<std::array<ReaCell, kReaSlots>> cells;   // [segment][slot]
    std::vector<Finding> findings;                       // CALC-* diagnostics
    calc::ClientStats stats;
    bool all_rejected = false;
};

struct ReaInputs {
    const Dataset& dataset;
    const Segmentation& segmentation;
    const Projection& projection;
    const ScenarioConfig& config;
    const std::map<std::string, Satellite>& satellites;
    const MacroTable& macro;
    const ExternalParameters* external = nullptr;   // exposure-level parameter paths
    std::string parameter_source;                   // FROM-clause source of customer parameters (pd_reg/lgd_reg)
};

// IRB exposure class (CRR Art. 147) of an exposure, from its EBA sector, SME flag and household purpose.
std::string irb_exposure_class(const Exposure& e, const Counterparty& cp);

// Builds the records, calls the calculator and aggregates. Throws on protocol errors and unsupported
// capabilities. Rejected records are counted and reported (CALC-010); all rejected sets `all_rejected`.
ReaResult project_rea(Duck& duck, const ReaInputs& in, const calc::ClientOptions& options);

void write_rea_csv(const Segmentation& s, const ReaResult& r, const std::filesystem::path& file);
// The "rea" member of summary.json (a JSON object, indented for that file).
void write_rea_summary(std::ostream& f, const ReaResult& r);

}  // namespace sora
