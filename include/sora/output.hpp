#pragma once
// Result files. The layout matches tests/golden/<date>/ so engine and reference outputs compare directly.

#include <filesystem>

#include "sora/off_balance.hpp"
#include "sora/projection.hpp"
#include "sora/rea.hpp"
#include "sora/validation.hpp"

namespace sora {

struct RunOutput {
    const Dataset& dataset;
    const Segmentation& segmentation;
    const Calibration& calibration;
    const Projection* projection;   // null for `sora calibrate`
    const MacroTable& macro;
    const ScenarioConfig& config;
    const Diagnostics& diagnostics;
    const ReaResult* rea = nullptr;   // IRB REA from the calculator (--calculator): rea.csv, summary "rea"
    const OffBalanceResult* off_balance = nullptr;   // off_balance.csv, cr_scen_off_bs.csv, summary "off_balance"
};

void write_outputs(const RunOutput& run, const std::filesystem::path& dir);

}  // namespace sora
