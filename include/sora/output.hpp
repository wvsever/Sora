#pragma once
// Result files. The layout matches tests/golden/<date>/ so engine and reference outputs compare directly.

#include <filesystem>

#include "sora/projection.hpp"
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
};

void write_outputs(const RunOutput& run, const std::filesystem::path& dir);

}  // namespace sora
