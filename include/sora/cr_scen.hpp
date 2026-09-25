#pragma once
// EBA CSV_CR_SCEN layout (2027 EU-wide stress test draft templates): 22 portfolio rows per geography
// (Total, top countries, Other), scenario and year, with the template's 54 value columns.
// Amounts in EUR million, parameters and ratios in percent.

#include <filesystem>

#include "sora/collateral.hpp"
#include "sora/projection.hpp"

namespace sora {

// `collateral` fills the LTV columns (blank without it).
void write_cr_scen(const Dataset& d, const Segmentation& s, const Projection& p, const std::filesystem::path& file,
                   const CollateralResult* collateral = nullptr);

}  // namespace sora
