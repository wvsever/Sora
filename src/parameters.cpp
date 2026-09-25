#include "sora/parameters.hpp"

#include <algorithm>
#include <cstdio>

namespace sora {

double& param_field(Params& p, std::size_t i) {
    switch (i) {
        case 0: return p.pd12m_s1;
        case 1: return p.pd12m_s2;
        case 2: return p.tr1_2;
        case 3: return p.tr2_1;
        case 4: return p.tr3_1;
        case 5: return p.tr3_2;
        case 6: return p.lgd_s1;
        case 7: return p.lgd_s2;
        case 8: return p.lgd_s3;
        default: return p.lrlt_s2;
    }
}

double param_field(const Params& p, std::size_t i) { return param_field(const_cast<Params&>(p), i); }

bool OptParams::any() const {
    for (const auto& x : v) if (x) return true;
    return false;
}

void ExternalParameters::load(Duck& duck, const std::string& source, const Dataset& d) {
    std::string cols;
    for (auto n : kParamNames) cols += std::string(", CAST(") + n + " AS DOUBLE)";
    duck.query("SELECT CAST(level AS VARCHAR), CAST(key AS VARCHAR), CAST(scenario AS VARCHAR), CAST(year AS BIGINT)" +
                   cols + " FROM " + source + " ORDER BY 1, 2, 3, 4",
               [&](const Chunk& c) {
                   for (std::size_t r = 0; r < c.size(); ++r) {
                       const auto level = c.str(0, r);
                       const auto key = c.str(1, r);
                       const auto scen = c.str(2, r);
                       const auto year = c.i64(3, r);
                       ParamKey k;
                       if (scen == "actual" && year == 0) k = {0, 0};
                       else if (scen == "baseline" && year >= 1 && year <= 3) k = {1, static_cast<int>(year)};
                       else if (scen == "adverse" && year >= 1 && year <= 3) k = {2, static_cast<int>(year)};
                       else throw Error("risk parameters: unsupported scenario/year " + std::string(scen) + "/" + std::to_string(year) +
                                        " for " + std::string(key));
                       OptParams op;
                       for (std::size_t i = 0; i < kParamCount; ++i)
                           if (c.valid(4 + i, r)) op.v[i] = c.f64(4 + i, r);
                       Slots* slots = nullptr;
                       if (level == "segment") {
                           slots = &by_level_[std::string(key)];
                       } else if (level == "exposure") {
                           const auto ex = d.exposure_ids.find(key);
                           if (!ex) { unknown_keys.emplace_back(key); continue; }
                           slots = &by_exposure_[*ex];
                       } else {
                           throw Error("risk parameters: level must be segment or exposure, got " + std::string(level));
                       }
                       (*slots)[slot(k)] = op;
                       ++rows_;
                   }
               });
}

std::size_t ExternalParameters::apply_segment(const Segmentation& s, const Segment& seg, ParamKey k, Params& p) const {
    std::size_t n = 0;
    for (auto it = seg.levels.rbegin(); it != seg.levels.rend(); ++it) {   // general -> specific
        const auto f = by_level_.find(s.level_keys.view(*it));
        if (f == by_level_.end()) continue;
        const auto& op = f->second[slot(k)];
        for (std::size_t i = 0; i < kParamCount; ++i)
            if (op.v[i]) { param_field(p, i) = *op.v[i]; ++n; }
    }
    return n;
}

std::size_t ExternalParameters::apply_exposure(std::size_t exposure, ParamKey k, Params& p) const {
    const auto f = by_exposure_.find(exposure);
    if (f == by_exposure_.end()) return 0;
    std::size_t n = 0;
    const auto& op = f->second[slot(k)];
    for (std::size_t i = 0; i < kParamCount; ++i)
        if (op.v[i]) { param_field(p, i) = *op.v[i]; ++n; }
    return n;
}

std::vector<std::string> ExternalParameters::unmatched_segment_keys(const Segmentation& s) const {
    std::vector<std::string> out;
    for (const auto& [k, _] : by_level_)
        if (!s.level_keys.find(k)) out.push_back(k);
    std::sort(out.begin(), out.end());
    return out;
}

std::vector<std::string> check_parameters(const Params& p) {
    std::vector<std::string> out;
    for (std::size_t i = 0; i < kParamCount; ++i) {
        const double v = param_field(p, i);
        if (!(v >= 0.0 && v <= 1.0)) out.push_back(std::string(kParamNames[i]) + " outside [0, 1]");
    }
    if (p.pd12m_s1 + p.tr1_2 > 1.0 + 1e-12) out.emplace_back("pd12m_s1 + tr1_2 > 1");
    if (p.pd12m_s2 + p.tr2_1 > 1.0 + 1e-12) out.emplace_back("pd12m_s2 + tr2_1 > 1");
    return out;
}

}  // namespace sora
