#include "sora/projection.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace sora {

std::size_t param_weight_stage(std::size_t param) {
    switch (param) {
        case 0: case 2: case 6: return 0;            // pd12m_s1, tr1_2, lgd_s1
        case 1: case 3: case 7: case 9: return 1;    // pd12m_s2, tr2_1, lgd_s2, lrlt_s2
        default: return 2;                           // tr3_1, tr3_2, lgd_s3
    }
}

double ParamAccum::average(std::size_t param) const {
    const double w = weight[param_weight_stage(param)];
    return w > 0 ? sum[param] / w : std::numeric_limits<double>::quiet_NaN();
}

namespace {
void accumulate(ParamAccum& a, const Params& p, double w1, double w2, double w3) {
    a.weight[0] += w1;
    a.weight[1] += w2;
    a.weight[2] += w3;
    for (std::size_t i = 0; i < kParamCount; ++i) {
        const double w = param_weight_stage(i) == 0 ? w1 : param_weight_stage(i) == 1 ? w2 : w3;
        if (w != 0) a.sum[i] += param_field(p, i) * w;
    }
}
}  // namespace

std::array<double, 21> fields(const YearResult& r) {
    return {r.exp_s1, r.exp_s2, r.exp_s3_old, r.exp_s3_new, r.exp_poci,
            r.flow_s1_s2, r.flow_s2_s1, r.flow_s1_s3, r.flow_s2_s3,
            r.prov_s1_s1, r.prov_s2_s1, r.prov_s1_s2, r.prov_s2_s2,
            r.prov_cum_s1_s3, r.prov_cum_s2_s3, r.prov_old_s3,
            r.prov_stock_s1, r.prov_stock_s2, r.prov_stock_s3, r.prov_stock_poci, r.impairment};
}

void project_exposure(Stage stage, double gca, double allowance, const std::array<ParamPath, 2>& paths,
                      const ScenarioConfig& cfg, std::array<std::array<YearResult, 3>, 2>& acc,
                      std::array<std::array<ParamAccum, 4>, 2>* param_acc) {
    const bool poci = stage == Stage::Poci;
    for (std::size_t sc = 0; sc < 2; ++sc) {
        const auto& P = paths[sc];
        double e1 = stage == Stage::S1 ? gca : 0.0;
        double e2 = stage == Stage::S2 ? gca : 0.0;
        const double e3old = stage == Stage::S3 ? gca : 0.0;
        double e3new = 0.0, cum13 = 0.0, cum23 = 0.0;
        const double prov_poci = poci ? allowance : 0.0;
        double prev_total = allowance;
        // Box 9, per exposure (MN para 141): no release below the starting provision.
        const double old3 = stage == Stage::S3 ? std::max(e3old * P[1].lgd_s3, allowance) : 0.0;
        if (param_acc) accumulate((*param_acc)[sc][0], P[0], e1, e2, e3old);
        for (std::size_t t = 0; t < 3; ++t) {
            const Params& p1 = P[t + 1];
            const Params& p2 = P[t + 2];
            if (param_acc) accumulate((*param_acc)[sc][t + 1], p1, e1, e2, e3old);
            const double f12 = e1 * p1.tr1_2, f21 = e2 * p1.tr2_1;
            const double f13 = e1 * p1.pd12m_s1, f23 = e2 * p1.pd12m_s2;
            double loss_next = p2.pd12m_s1 * p2.lgd_s1;
            if (sc == 1 && t + 1 == 3) {   // Boxes 4-5: final adverse year blends adverse and baseline
                const Params& pb = paths[0][3];
                loss_next = cfg.blend_adverse * p2.pd12m_s1 * p2.lgd_s1 + cfg.blend_baseline * pb.pd12m_s1 * pb.lgd_s1;
            }
            const double prov11 = e1 * (1 - p1.tr1_2 - p1.pd12m_s1) * loss_next;   // Box 5
            const double prov21 = f21 * loss_next;                                  // Box 4
            const double prov12 = f12 * p1.lrlt_s2;                                 // Box 6
            const double prov22 = e2 * (1 - p1.tr2_1 - p1.pd12m_s2) * p1.lrlt_s2;   // Box 7
            cum13 += f13 * p1.lgd_s1;                                               // Box 8
            cum23 += f23 * p1.lgd_s2;
            const double n1 = e1 - f12 - f13 + f21, n2 = e2 - f21 - f23 + f12;
            e3new += f13 + f23;
            e1 = n1;
            e2 = n2;
            const double s1 = prov11 + prov21, s2 = prov12 + prov22, s3 = cum13 + cum23 + old3;   // Box 3
            const double total = s1 + s2 + s3 + prov_poci;
            YearResult& r = acc[sc][t];
            r.exp_s1 += e1;
            r.exp_s2 += e2;
            r.exp_s3_old += e3old;
            r.exp_s3_new += e3new;
            r.exp_poci += poci ? gca : 0.0;
            r.flow_s1_s2 += f12;
            r.flow_s2_s1 += f21;
            r.flow_s1_s3 += f13;
            r.flow_s2_s3 += f23;
            r.prov_s1_s1 += prov11;
            r.prov_s2_s1 += prov21;
            r.prov_s1_s2 += prov12;
            r.prov_s2_s2 += prov22;
            r.prov_cum_s1_s3 += cum13;
            r.prov_cum_s2_s3 += cum23;
            r.prov_old_s3 += old3;
            r.prov_stock_s1 += s1;
            r.prov_stock_s2 += s2;
            r.prov_stock_s3 += s3;
            r.prov_stock_poci += prov_poci;
            r.impairment += total - prev_total;
            prev_total = total;
        }
    }
}

Projection project(const Dataset& d, const Segmentation& s, const Calibration& cal,
                   const std::map<std::string, Satellite>& satellites, const MacroTable& macro,
                   const ScenarioConfig& cfg, const ExternalParameters* external) {
    Projection out;
    const auto nseg = s.segments.size();
    out.results.resize(nseg);
    out.params.resize(nseg);
    out.start_source.resize(nseg);
    out.path_source.resize(nseg);
    out.accum.resize(nseg);
    auto check = [&](const Params& p, const std::string& what) {
        for (const auto& m : check_parameters(p)) out.parameter_errors.push_back(what + ": " + m);
    };
    std::vector<const Satellite*> sat(nseg);
    std::vector<Params> start(nseg);
    for (std::size_t i = 0; i < nseg; ++i) {
        const auto& seg = s.segments[i];
        const auto it = satellites.find(seg.portfolio);
        if (it == satellites.end()) throw Error("no satellite coefficients for portfolio " + seg.portfolio);
        sat[i] = &it->second;
        // Starting point: derived calibration, overlaid field-wise by customer parameters.
        start[i] = cal.params[i];
        const std::size_t n = external ? external->apply_segment(s, seg, {0, 0}, start[i]) : 0;
        out.start_source[i] = n == 0 ? "derived" : n >= kParamCount ? "external" : "mixed";
        check(start[i], seg.key + " actual/0");
        for (std::size_t sc = 0; sc < 2; ++sc) {
            auto& path = out.params[i][sc];
            path = project_parameters(seg, start[i], *sat[i], macro, kScenarios[sc], cfg);
            for (int t = 1; t <= 3; ++t) {
                const std::size_t n = external ? external->apply_segment(s, seg, {static_cast<int>(sc) + 1, t}, path[static_cast<std::size_t>(t)]) : 0;
                out.path_source[i][sc][static_cast<std::size_t>(t - 1)] = n == 0 ? "derived" : n >= kParamCount ? "external" : "mixed";
            }
            path[4] = path[3];
            for (int t = 1; t <= 3; ++t) check(path[static_cast<std::size_t>(t)], seg.key + " " + kScenarios[sc] + "/" + std::to_string(t));
        }
    }
    for (std::size_t i = 0; i < d.exposures.size(); ++i) {
        const auto sid = s.segment_of[i];
        if (sid < 0) continue;
        const auto& e = d.exposures[i];
        if (e.stage == Stage::NotApplicable) continue;
        const auto seg = static_cast<std::size_t>(sid);
        const double gca = to_double(e.gca) * s.fx[i], allowance = to_double(e.allowance) * s.fx[i];
        if (external && external->has_exposure(i)) {
            // Exposure-level parameters: own starting point and path (same macro drivers as the segment).
            Params p0 = start[seg];
            external->apply_exposure(i, {0, 0}, p0);
            std::array<ParamPath, 2> own;
            for (std::size_t sc = 0; sc < 2; ++sc) {
                own[sc] = project_parameters(s.segments[seg], p0, *sat[seg], macro, kScenarios[sc], cfg);
                for (int t = 1; t <= 3; ++t) {
                    const ParamKey k{static_cast<int>(sc) + 1, t};
                    external->apply_segment(s, s.segments[seg], k, own[sc][static_cast<std::size_t>(t)]);
                    external->apply_exposure(i, k, own[sc][static_cast<std::size_t>(t)]);
                    check(own[sc][static_cast<std::size_t>(t)], d.exposure_ids.at(e.id) + " " + kScenarios[sc] + "/" + std::to_string(t));
                }
                own[sc][4] = own[sc][3];
            }
            check(p0, d.exposure_ids.at(e.id) + " actual/0");
            ++out.exposures_with_own_parameters;
            project_exposure(e.stage, gca, allowance, own, cfg, out.results[seg], &out.accum[seg]);
        } else {
            project_exposure(e.stage, gca, allowance, out.params[seg], cfg, out.results[seg], &out.accum[seg]);
        }
    }
    return out;
}

}  // namespace sora
