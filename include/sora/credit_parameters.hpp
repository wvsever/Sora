#pragma once
// Starting-point risk parameters per exposure from the regulatory calculator (POST /v1/parameters/credit), used
// as a customer parameter source (`sora run --calculator <url> --calculator-parameters <list>`). See
// plans/11_integrations.md and plans/09_risk_parameters.md.
//
// Records: one per exposure the run projects (in-scope on-balance exposures with a stage, and the off-balance items
// of the scenario's off_balance types), in exposure order; recordId = exposure_id, level = exposure, the segment key
// (on-balance), the stage and SIM attributes. One call at the reference date (scenario actual, years [0]).
//
// Precedence, field by field, for the starting point: exposure row of the parameter source (--parameters or
// sim_risk_parameter) > calculator value > segment rows of the parameter source > Sora's own value. A value the
// calculator does not return is never filled in: it is counted (CALC-022) and the next source applies.

#include <array>
#include <cstdint>
#include <map>
#include <ostream>
#include <string>
#include <vector>

#include "sora/calculator.hpp"
#include "sora/parameters.hpp"
#include "sora/scenario.hpp"
#include "sora/validation.hpp"

namespace sora {

// The parameters Sora uses from the calculator: the ten IFRS 9 parameters (kParamNames), then ccf, pd_reg, lgd_reg.
inline constexpr std::size_t kCalculatorParamCount = kParamCount + 3;
const char* calculator_param_name(std::size_t i);

// Comma-separated parameter names, or "all" for every one (in the order above). Throws Error on an unknown or
// repeated name, or an empty list.
std::vector<std::string> parse_calculator_parameters(const std::string& list);

struct CreditParameterCount {
    std::uint64_t received = 0;   // values returned for ok records
    std::uint64_t applied = 0;    // used by the run
    std::uint64_t file = 0;       // not used: the parameter source has an exposure row with that field
    std::uint64_t missing = 0;    // requested, record ok, but no value returned
};

struct CreditParameterResult {
    std::string calculator, version, param_set;
    std::vector<std::string> parameters;   // requested, in request order
    std::uint64_t records = 0, ok = 0, rejected = 0;
    std::map<std::string, CreditParameterCount> counts;   // by parameter
    std::map<std::string, std::uint64_t> sources;         // values by the calculator's source (model, ..., unspecified)
    // Every record, in exposure order: exposure index, status, the values returned (Nano) and which of the ten IFRS 9
    // fields the run took from the calculator (bit i = kParamNames[i]).
    struct Row {
        std::uint32_t exposure = 0;
        bool ok = false;
        std::uint32_t applied = 0;
        std::array<std::optional<Nano>, kCalculatorParamCount> values{};
    };
    std::vector<Row> rows;
    std::vector<Finding> findings;   // CALC-02x diagnostics
    calc::ClientStats stats;
    bool all_rejected = false;
};

struct CreditParameterInputs {
    const Dataset& dataset;
    const Segmentation& segmentation;
    const ScenarioConfig& config;
    std::string parameter_source;   // FROM-clause source of the customer parameters (may be empty)
    std::vector<std::string> parameters;
};

// Requests the parameters, validates the responses and merges the values into `ext` (the IFRS 9 fields) and its
// exposure extras (ccf, pd_reg, lgd_reg), below the source's exposure rows. Throws on protocol errors, an unreachable
// calculator without a replay cache, and unsupported capabilities. All records rejected sets `all_rejected`.
CreditParameterResult fetch_credit_parameters(Duck& duck, const CreditParameterInputs& in, ExternalParameters& ext,
                                              const calc::ClientOptions& options);

// parameters.csv rows (level exposure, actual/0) for the exposures with IFRS 9 fields from the calculator: those
// fields, the others empty (the segment row applies), source `calculator`.
void write_calculator_parameter_rows(std::ostream& f, const Dataset& d, const CreditParameterResult& r);
// calculator_parameters.csv: every record with its status and the values returned (empty = not returned).
void write_calculator_parameters_csv(const Dataset& d, const CreditParameterResult& r, const std::filesystem::path& file);
// The "calculator_parameters" member of summary.json.
void write_calculator_parameters_summary(std::ostream& f, const CreditParameterResult& r);

}  // namespace sora
