#pragma once
// Net interest income (NII) under the static balance sheet: EBA MN 2025 section 4 (2027 draft section 4), scenario
// key `nii`. See plans/13_nii.md for the methodology and the decisions taken where the data or the MN leave room.
//
// Position by position (assets from sim_exposure, deposits from sim_deposit, debt issued from sim_debt_issued), the
// effective interest rate (EIR) is split into a reference-rate component (risk-free rate of the currency at the
// position's tenor, sim_rate_curve) and a margin. Floating positions reset the reference rate on their reset dates;
// fixed positions keep their rate until maturity; at maturity a position is replaced by the same instrument (same
// original term) priced at the scenario reference rate plus the new business margin of its portfolio, moved by the
// margin paths of Boxes 23-24. Sight deposits reprice immediately with the MN pass-through. Non-performing assets
// earn their EIR on the exposure net of provisions. Results per CSV_NII_CALC row, currency, rate type and
// performing status (nii.csv), and the Box 22 cap on the adverse NII (summary.json "nii").
//
// Deterministic: positions are projected in parallel into per-position slots and aggregated serially in position
// order, so results are bit-identical for any number of workers (and equal to tools/reference/sora_reference.py).

#include <array>
#include <cstdint>
#include <filesystem>
#include <iosfwd>
#include <map>
#include <optional>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "sora/dataset.hpp"
#include "sora/projection.hpp"
#include "sora/scenario.hpp"

namespace sora {

namespace nii {

// Calendar months added to a day (days since 1970-01-01), clamped to the month end (Jan 31 + 1 month = Feb 28/29).
Date add_months(Date day, int months);

// Rate curve: (tenor in years, rate) sorted by tenor; linear interpolation, flat beyond both ends (MN para 374).
using Curve = std::vector<std::pair<double, double>>;
double interp(const Curve& curve, double tenor_years);

// Template row of CSV_NII_CALC (2025 templates, RowNum 1-35 of the fixed-rate block).
struct TemplateRow {
    int row;
    bool asset;
    const char* label;
    double factor;   // lambda (assets, Box 24) or gamma (liabilities, Box 23)
};
const TemplateRow& template_row(int row);   // throws for rows not produced by the engine

// One position in the reporting currency.
struct Position {
    int row = 0;                     // CSV_NII_CALC row
    std::uint16_t currency = 0;      // index into NiiResult::currencies
    std::uint16_t country = 0;       // NiiResult::countries: counterparty residence (assets), booking entity (liabilities)
    bool performing = true;
    bool floating = false;
    bool sight = false;              // sight deposit: reprices every year at the 1M rate with pass-through `beta`
    double beta = 1.0;               // sight deposits: pass-through of the 1M rate change (0.5 HH, 0.75 NFC, 1 others)
    bool floor_zero = false;         // household sight deposits: EIR not below 0 (2027 draft para 397)
    double volume = 0;               // gross carrying amount / amount / carrying amount (EUR)
    double provisions = 0, net_volume = 0;   // non-performing assets
    double eir = 0;                  // rate at the reference date
    int freq = 0;                    // floating: reset frequency in months
    std::optional<Date> origination, maturity, next_reset;
    // Derived by project_nii (prepare step):
    std::optional<Date> term;        // original term in days (replacement term); none: open-ended
    double tenor = 0;                // years: reference-rate tenor (1M sight, index tenor floating, original term fixed)
    double ref0 = 0, margin0 = 0;    // starting-point split of the EIR
    std::array<std::array<double, 3>, 2> delta{};   // [baseline, adverse][year 1..3]: scenario change of the rate at `tenor`
    std::array<std::array<double, 3>, 2> shock{};   // [baseline, adverse][year 1..3]: margin path of Boxes 23-24
    double margin_new = 0;           // new business margin of the position's cell (row, currency, rate type)
};

// Interest of one position per [scenario slot][component]: slot 0 = actual/0, 1..3 = baseline years 1..3,
// 4..6 = adverse years 1..3; components interest, reference-rate part, margin part (0 for non-performing).
using PositionInterest = std::array<std::array<double, 3>, 7>;

// Rate environment of the projection: the bank's risk-free curves at the reference date and the scenario's
// changes of the swap curves against its starting point (MN paras 374-375, 408).
struct RateScenario {
    std::map<std::string, Curve> bank;                                  // currency -> risk-free curve
    std::map<std::tuple<std::string, std::string, int>, Curve> swaps;   // (currency key, scenario, year) -> swap curve
    int history_year = 0;
    std::array<int, 3> years{};   // scenario year of projection years 1..3
    const Curve& swap(const std::string& key, const std::string& scenario, int year) const;   // throws if missing
    std::string swap_key(const std::string& currency) const;   // the currency, else RoW (para 375)
    double rf0(const std::string& currency, double tenor) const;   // bank curve, else the scenario's starting point
    // Change of the swap rate at `tenor` from the scenario's starting point to projection year `year` (1..3).
    double delta(const std::string& currency, double tenor, int scenario /*0 baseline, 1 adverse*/, int year) const;
};

// Projects one prepared position under both scenarios. `bounds` are the ends of the projection years (D0 = reference
// date .. D3, one calendar year each). Pure function of its arguments.
PositionInterest project_position(const Position& p, const std::array<Date, 4>& bounds);

struct NiiCell {
    std::size_t positions = 0;
    double volume = 0, provisions = 0, net_volume = 0, interest = 0, interest_reference = 0, interest_margin = 0;
};
// (scenario 0 actual / 1 baseline / 2 adverse, year, template row, currency, rate type, status)
using NiiKey = std::tuple<int, int, int, std::string, std::string, std::string>;

struct NiiResult {
    std::vector<std::string> currencies, countries;   // names of Position::currency / Position::country
    std::map<NiiKey, NiiCell> cells;
    std::map<std::tuple<int, std::string, std::string>, double> margin_new_business;   // (row, currency, rate type)
    std::array<double, 7> income{}, expense{};   // per slot (see PositionInterest)
    double volume_performing = 0, volume_non_performing = 0, provisions_non_performing = 0;
    double npe_provisions_credit = 0;             // t0 S3 + POCI provisions of the credit projection scope
    std::array<double, 3> npe_provision_increase{}, nii_cap{};   // adverse years 1..3 (Box 22)
    std::string own_rating;
    double idiosyncratic_shock = 0;
    int new_business_months = 12;
    std::size_t assets = 0, non_performing = 0, deposits = 0, sight_deposits = 0, debt_issued = 0;
    std::size_t missing_rate = 0, floating_without_frequency = 0, term_fallback = 0, new_business_fallback_cells = 0;
};

}  // namespace nii

// Loads the NII positions and curves, projects them and aggregates. `projection` (the credit projection of the same
// run) gives the NPE provision increase for the Box 22 cap. `workers` as in project().
nii::NiiResult project_nii(Duck& duck, const Dataset& d, const Segmentation& s, const Projection& projection,
                           const MacroTable& macro, const ScenarioConfig& cfg, unsigned workers);

void write_nii(const nii::NiiResult& r, const Dataset& d, const std::filesystem::path& file);   // nii.csv
void write_nii_summary(std::ostream& f, const nii::NiiResult& r);                              // summary "nii"

}  // namespace sora
