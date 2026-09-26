#pragma once
// Prior-year Actual rows of CR_SCEN and CR_SECTOR (EBA 2027 draft MN para 71 and Table 2: end-of-year stocks of the
// year before the starting point, "according to the portfolios applicable" at the starting point).
//
// Date: 31 December of the year before the reference date's year, or the scenario key `prior_year_end`; the rows are
// labelled with its year. Each in-scope t0 exposure (Segmentation::segment_of) with a sim_stage_history row at that
// date contributes, in its t0 segment and NACE sector, its stage at that date, its exposure (gross_carrying_amount,
// else the principal_outstanding proxy) and its loss allowance, converted at that date's FX rate (sim_fx_rate). A
// facility whose undrawn part is projected off-balance keeps the drawn share of its allowance, as at t0: pro rata to
// the history's drawn and undrawn amounts where the history has the undrawn amount, else with the t0 share. Nothing is
// estimated: an exposure without an amount (or FX rate) makes the exposure (provision) cells of its rows blank.
// See plans/03_scenario_engine.md, Prior-year Actual rows.

#include <array>
#include <cmath>
#include <string>
#include <string_view>
#include <vector>

#include "sora/dataset.hpp"
#include "sora/segmentation.hpp"

namespace sora {

// The prior year-end: `configured` (YYYY-MM-DD) if set, else 31 December of the year before the reference date's.
// Throws unless it is a month end in a calendar year before the reference date's.
std::string prior_year_end(const std::string& reference_date, const std::string& configured);

struct PriorYear {
    bool available = false;        // sim_stage_history has rows at the date
    std::string date;              // YYYY-MM-DD
    int year = 0;                  // template year (label of the rows)
    // Per exposure (Dataset order); `stage` NotApplicable = no history row at the date or not in the t0 scope.
    std::vector<Stage> stage;
    std::vector<double> exposure;   // reporting currency; NaN = no amount or no FX rate
    std::vector<double> allowance;  // on-balance loss allowance, reporting currency; NaN = no FX rate
    std::vector<char> has_amount;   // the history has an amount (gross carrying amount or principal proxy)
    // Counts (summary.json "prior_year", diagnostics PRY-*).
    std::size_t history_rows = 0, exposures = 0, amount_gca = 0, amount_principal = 0, missing_amount = 0,
                missing_fx = 0, allowance_split_history = 0, allowance_split_t0_share = 0, out_of_scope = 0,
                not_in_sim_exposure = 0;
    double not_in_sim_exposure_allowance = 0.0;   // their allowance at the date (reporting currency, where FX known)
};

PriorYear load_prior_year(Duck& duck, const Dataset& d, const Segmentation& s, const ScopeConfig& scope,
                          const std::string& date);

// Prior-year stocks of a set of exposures, additive. Unknown amounts are counted, not estimated.
struct PriorStock {
    std::array<double, 4> exp{}, prov{};   // S1, S2, S3, POCI
    std::size_t contracts = 0, missing_amount = 0, missing_fx = 0;

    void add(const PriorYear& p, std::size_t i) {
        const auto st = static_cast<std::size_t>(p.stage[i]);
        if (st > 3) return;
        ++contracts;
        missing_amount += p.has_amount[i] ? 0 : 1;
        missing_fx += std::isnan(p.allowance[i]) ? 1 : 0;
        if (!std::isnan(p.exposure[i])) exp[st] += p.exposure[i];
        if (!std::isnan(p.allowance[i])) prov[st] += p.allowance[i];
    }
    void add(const PriorStock& o) {
        for (std::size_t k = 0; k < 4; ++k) {
            exp[k] += o.exp[k];
            prov[k] += o.prov[k];
        }
        contracts += o.contracts;
        missing_amount += o.missing_amount;
        missing_fx += o.missing_fx;
    }
    // Every exposure has an amount and an FX rate (exposure cells), an FX rate (provision cells).
    bool exposures_known(bool available) const { return available && missing_amount == 0 && missing_fx == 0; }
    bool provisions_known(bool available) const { return available && missing_fx == 0; }
};

// Whether a template cell (CR_SCEN, CR_SECTOR header) of a prior-year row may be written: the exposure cells need
// every exposure's amount, the coverage ratios amounts and provisions, the other amounts (provision stocks) the
// provisions. Percentages other than coverage ratios are not affected.
bool prior_cell_known(std::string_view header, bool percent, bool exposures_known, bool provisions_known);

}  // namespace sora
