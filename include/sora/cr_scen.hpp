#pragma once
// EBA CSV_CR_SCEN layout (2027 EU-wide stress test draft templates): 22 portfolio rows per geography
// (Total, top countries, Other), scenario and year, with the template's 54 value columns.
// Amounts in EUR million, parameters and ratios in percent. With `prior`, the prior-year Actual rows (stocks at the
// prior year-end, prior_year.hpp) come first.

#include <filesystem>

#include "sora/collateral.hpp"
#include "sora/prior_year.hpp"
#include "sora/projection.hpp"

namespace sora {

// `collateral` fills the LTV columns (blank without it).
void write_cr_scen(const Dataset& d, const Segmentation& s, const Projection& p, const std::filesystem::path& file,
                   const CollateralResult* collateral = nullptr, const PriorYear* prior = nullptr);

}  // namespace sora
