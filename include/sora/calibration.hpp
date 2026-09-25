#pragma once
// Starting-point parameter calibration from SIM history (see plans/09_risk_parameters.md).

#include <array>
#include <string>
#include <vector>

#include "sora/dataset.hpp"
#include "sora/segmentation.hpp"

namespace sora {

// EBA credit-risk parameters of one segment for one year (12-month, point in time).
struct Params {
    double pd12m_s1 = 0, pd12m_s2 = 0, tr1_2 = 0, tr2_1 = 0, tr3_1 = 0, tr3_2 = 0;
    double lgd_s1 = 0, lgd_s2 = 0, lgd_s3 = 0, lrlt_s2 = 0;
};

struct CalibrationConfig {
    std::uint64_t min_observations = 100;
    double pd_floor = 0.00001;
};

struct Calibration {
    std::vector<Params> params;   // per segment
    // Hierarchy level each part came from, per segment: stage1, stage2, stage3, lgd, lrlt ("none" if no data).
    std::vector<std::array<std::string, 5>> sources;
};

using Matrix3 = std::array<std::array<double, 3>, 3>;
Matrix3 matmul(const Matrix3& a, const Matrix3& b);
Matrix3 matpow(const Matrix3& m, int n);

Calibration calibrate(Duck& duck, const Dataset& d, const Segmentation& s, const CalibrationConfig& cfg);

}  // namespace sora
