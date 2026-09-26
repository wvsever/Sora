#pragma once
// EBA CSV_CR_SECTOR layout (2027 EU-wide stress test draft templates): credit risk of the non-financial
// corporations portfolio by NACE Rev. 2.1 section, with manufacturing split into energy-intensive and other
// activities. 23 rows per geography (Total, top countries, Other), scenario and year, with the template's
// 46 value columns. Amounts in EUR million, parameters and ratios in percent.
//
// Scope: the NFC segments (loans and advances NFC_*, debt securities NFC), i.e. CR_SCEN rows 6 and 13. The
// sectors come from the per-exposure projection (Projection::sectors), so the TOTAL row reconciles with
// those CR_SCEN rows for every geography, scenario and year. See plans/03_scenario_engine.md, CR_SECTOR.

#include <filesystem>

#include "sora/projection.hpp"

namespace sora {

void write_cr_sector(const Dataset& d, const Segmentation& s, const Projection& p, const std::filesystem::path& file);

}  // namespace sora
