#include "sora/benchmark.hpp"

#include <algorithm>
#include <cstdio>
#include <fstream>
#include <ostream>

#include "sora/json_text.hpp"

namespace sora {

namespace {

// Group of a parameter index, or -1 (TR3-x: not benchmarked).
int group_of(std::size_t param) {
    for (std::size_t g = 0; g < kBenchmarkGroups; ++g)
        for (auto p : kBenchmarkGroupParams[g])
            if (p == param) return static_cast<int>(g);
    return -1;
}

std::size_t param_index(std::string_view name) {
    for (std::size_t i = 0; i < kParamCount; ++i)
        if (name == kParamNames[i]) return i;
    return kParamCount;
}

// Calibration parts (Calibration::sources: stage1, stage2, stage3, lgd, lrlt) behind each group's starting point.
constexpr std::array<std::array<std::size_t, 2>, kBenchmarkGroups> kGroupParts = {{{0, 1}, {3, 4}}};

}  // namespace

std::string_view to_string(BenchmarkRule r) noexcept {
    switch (r) {
        case BenchmarkRule::Sovereign: return "sovereign";
        case BenchmarkRule::Coverage: return "coverage";
        case BenchmarkRule::NoModel: return "no_model";
        default: return "none";
    }
}

void BenchmarkTable::set(std::string_view key, int scenario, int year, std::size_t param, double value) {
    if (group_of(param) < 0) throw Error("benchmarks: " + std::string(kParamNames[param]) + " is not a benchmark parameter");
    auto& e = by_key_[std::string(key)];
    auto& flag = e.set[static_cast<std::size_t>(scenario)][static_cast<std::size_t>(year - 1)][param];
    if (flag)
        throw Error("benchmarks: duplicate " + std::string(kParamNames[param]) + " for " + std::string(key) + " " +
                    (scenario == 0 ? "baseline" : "adverse") + " year " + std::to_string(year));
    flag = true;
    param_field(e.values[static_cast<std::size_t>(scenario)][static_cast<std::size_t>(year - 1)], param) = value;
    ++rows_;
}

void BenchmarkTable::load(Duck& duck, const std::filesystem::path& csv, const ScenarioConfig& cfg) {
    std::map<int, int> year_to_t;
    for (const auto& [t, y] : cfg.year_map) year_to_t[y] = t;
    duck.query("SELECT instrument, portfolio, country, scenario, CAST(year AS BIGINT), parameter, CAST(value AS DOUBLE) "
               "FROM read_csv(" + sql_quote(csv.string()) + ", header = true, comment = '#', all_varchar = true) "
               "ORDER BY 1, 2, 3, 4, 5, 6",
               [&](const Chunk& c) {
                   for (std::size_t r = 0; r < c.size(); ++r) {
                       for (std::size_t col = 0; col < 7; ++col)
                           if (!c.valid(col, r)) throw Error("benchmarks: empty field in " + csv.string());
                       const auto instrument = c.str(0, r), portfolio = c.str(1, r), country = c.str(2, r);
                       const auto scen = c.str(3, r), name = c.str(5, r);
                       const std::string key = std::string(instrument) + '|' + std::string(portfolio) + '|' + std::string(country);
                       if (instrument != "LOANS" && instrument != "DEBT_SEC")
                           throw Error("benchmarks: unknown instrument " + std::string(instrument));
                       const std::size_t p = param_index(name);
                       if (p == kParamCount || group_of(p) < 0) throw Error("benchmarks: unknown parameter " + std::string(name));
                       if (scen != "baseline" && scen != "adverse") throw Error("benchmarks: unknown scenario " + std::string(scen));
                       const auto t = year_to_t.find(static_cast<int>(c.i64(4, r)));
                       if (t == year_to_t.end()) continue;   // outside this scenario's horizon
                       const double v = c.f64(6, r);
                       if (!(v >= 0.0 && v <= 1.0)) throw Error("benchmarks: " + std::string(name) + " outside [0, 1] for " + key);
                       set(key, scen == "baseline" ? 0 : 1, t->second, p, v);
                   }
               });
    finish();
}

void BenchmarkTable::finish() {
    for (auto& [key, e] : by_key_) {
        for (std::size_t g = 0; g < kBenchmarkGroups; ++g) {
            std::size_t n = 0;
            for (const auto& sc : e.set)
                for (const auto& yr : sc)
                    for (auto p : kBenchmarkGroupParams[g]) n += yr[p] ? 1 : 0;
            if (n != 0 && n != 2 * 3 * kBenchmarkGroupParams[g].size())
                throw Error("benchmarks: incomplete " + std::string(kBenchmarkGroupNames[g]) + " parameters for " + key);
            e.has[g] = n != 0;
        }
        for (std::size_t sc = 0; sc < 2; ++sc)
            for (std::size_t t = 0; t < 3; ++t) {
                const auto& v = e.values[sc][t];
                for (std::size_t p = 0; p < kParamCount; ++p) {
                    const double x = param_field(v, p);
                    if (!(x >= 0.0 && x <= 1.0)) throw Error("benchmarks: " + std::string(kParamNames[p]) + " outside [0, 1] for " + key);
                }
                if (e.has[0] && (v.pd12m_s1 + v.tr1_2 > 1.0 + 1e-12 || v.pd12m_s2 + v.tr2_1 > 1.0 + 1e-12))
                    throw Error("benchmarks: stage outflows above 1 for " + key);
            }
    }
}

const BenchmarkTable::Entry* BenchmarkTable::find(std::string_view key) const {
    const auto it = by_key_.find(std::string(key));
    return it == by_key_.end() ? nullptr : &it->second;
}

BenchmarkResult decide_benchmarks(const Dataset& d, const Segmentation& s, const Calibration& cal,
                                  const std::map<std::string, Satellite>& satellites, const BenchmarkTable& table,
                                  const BenchmarkConfig& cfg, const ExternalParameters* external) {
    const auto nseg = s.segments.size();
    BenchmarkResult out;
    out.enabled = true;
    out.decisions.resize(nseg);
    out.apply.resize(nseg);
    out.exposure.assign(nseg, 0.0);

    // t0 exposure per segment: per stage in exposure order, then S1 + S2 + S3 + POCI.
    std::vector<std::array<double, 4>> by_stage(nseg, std::array<double, 4>{});
    for (std::size_t i = 0; i < d.exposures.size(); ++i) {
        const auto sid = s.segment_of[i];
        const auto st = static_cast<std::size_t>(d.exposures[i].stage);
        if (sid < 0 || st > 3) continue;
        by_stage[static_cast<std::size_t>(sid)][st] += to_double(d.exposures[i].gca) * s.fx[i];
    }
    for (std::size_t i = 0; i < nseg; ++i)
        out.exposure[i] = by_stage[i][0] + by_stage[i][1] + by_stage[i][2] + by_stage[i][3];

    auto covered = [&](std::size_t i, std::size_t g) {
        const auto& seg = s.segments[i];
        if (!satellites.count(seg.portfolio)) return false;
        if (external) {   // the customer's own projections of the whole group, within the pivot asset class
            bool all = true;
            for (int sc = 1; sc <= 2 && all; ++sc)
                for (int t = 1; t <= 3 && all; ++t)
                    for (auto p : kBenchmarkGroupParams[g]) {
                        const int lv = external->segment_level(s, seg, {sc, t}, p);
                        if (lv < 0 || lv > cfg.model_level) { all = false; break; }
                    }
            if (all) return true;
        }
        for (auto part : kGroupParts[g]) {
            const auto& src = cal.sources[i][part];
            bool ok = false;
            for (int lv = 0; lv <= cfg.model_level; ++lv)
                if (src == s.level_keys.view(seg.levels[static_cast<std::size_t>(lv)])) ok = true;
            if (!ok) return false;
        }
        return true;
    };

    // Model coverage per pivot asset class, in segment (key) order.
    std::map<std::string, std::size_t> pivot_of;
    std::vector<std::size_t> pivot(nseg);
    std::vector<std::array<bool, kBenchmarkGroups>> model(nseg);
    for (std::size_t i = 0; i < nseg; ++i) {
        const auto key = s.segments[i].instrument + '|' + s.segments[i].portfolio;
        auto [it, fresh] = pivot_of.emplace(key, out.pivots.size());
        if (fresh) out.pivots.push_back({key});
        pivot[i] = it->second;
        auto& pv = out.pivots[it->second];
        pv.exposure += out.exposure[i];
        for (std::size_t g = 0; g < kBenchmarkGroups; ++g) {
            model[i][g] = covered(i, g);
            pv.model[g] += model[i][g] ? out.exposure[i] : 0.0;
        }
    }

    for (std::size_t i = 0; i < nseg; ++i) {
        const auto& seg = s.segments[i];
        auto& pv = out.pivots[pivot[i]];
        const std::string prefix = seg.instrument + '|' + seg.portfolio + '|';
        for (std::size_t g = 0; g < kBenchmarkGroups; ++g) {
            auto has = [&](const std::string& country) {
                const auto* e = table.find(prefix + country);
                return e && e->has[g];
            };
            std::vector<std::string> chain;
            if (seg.bucket != "OTHER") chain.push_back(seg.bucket);
            chain.insert(chain.end(), cfg.country_fallback.begin(), cfg.country_fallback.end());
            const double coverage = pv.exposure > 0 ? pv.model[g] / pv.exposure : 0.0;
            auto& dec = out.decisions[i][g];
            dec.model = model[i][g];
            if (cfg.sovereign && seg.portfolio == "GG" && seg.bucket != "OTHER" && has(seg.bucket)) {
                dec.rule = BenchmarkRule::Sovereign;
                chain = {seg.bucket};
            } else if (coverage < cfg.coverage_threshold) {
                dec.rule = BenchmarkRule::Coverage;
            } else if (!dec.model) {
                dec.rule = BenchmarkRule::NoModel;
            }
            if (dec.rule == BenchmarkRule::None) continue;
            const auto found = std::find_if(chain.begin(), chain.end(), has);
            if (found == chain.end()) {
                dec.unavailable = true;
                continue;
            }
            dec.key = *found;
            pv.benchmark[g] += out.exposure[i];
            auto& ap = out.apply[i];
            ap.applied[g] = true;
            const auto& src = table.find(prefix + dec.key)->values;
            for (std::size_t sc = 0; sc < 2; ++sc)
                for (std::size_t t = 0; t < 3; ++t)
                    for (auto p : kBenchmarkGroupParams[g]) param_field(ap.values[sc][t], p) = param_field(src[sc][t], p);
        }
    }
    return out;
}

namespace {
std::string fixed(double x, int decimals) {
    char buf[64];
    std::snprintf(buf, sizeof buf, "%.*f", decimals, x);
    return buf;
}
std::string benchmark_cell(const BenchmarkDecision& d) { return d.applied() ? d.key : d.unavailable ? "unavailable" : ""; }
}  // namespace

void write_benchmarks(const Segmentation& s, const BenchmarkResult& b, const std::filesystem::path& file) {
    std::ofstream f(file, std::ios::binary);
    if (!f) throw Error("cannot write " + file.string());
    f << "segment,exposure,pd_tr_model,lgd_lr_model,pd_tr_rule,lgd_lr_rule,pd_tr_benchmark,lgd_lr_benchmark\n";
    for (std::size_t i = 0; i < s.segments.size(); ++i) {
        const auto& d = b.decisions[i];
        f << s.segments[i].key << ',' << fixed(b.exposure[i], 2) << ',' << (d[0].model ? 1 : 0) << ',' << (d[1].model ? 1 : 0)
          << ',' << to_string(d[0].rule) << ',' << to_string(d[1].rule) << ',' << benchmark_cell(d[0]) << ','
          << benchmark_cell(d[1]) << '\n';
    }
}

void write_benchmark_summary(std::ostream& f, const BenchmarkConfig& cfg, const BenchmarkResult& b) {
    std::size_t applied[2] = {}, unavailable = 0;
    for (const auto& d : b.decisions) {
        for (std::size_t g = 0; g < kBenchmarkGroups; ++g) applied[g] += d[g].applied() ? 1 : 0;
        unavailable += d[0].unavailable || d[1].unavailable ? 1 : 0;
    }
    f << "{\n    \"file\": \"" << json_escape(cfg.file.filename().string()) << "\",\n"
      << "    \"coverage_threshold\": " << fixed(cfg.coverage_threshold, 9) << ",\n"
      << "    \"model_level\": \"" << (cfg.model_level == 0 ? "segment" : "portfolio") << "\",\n"
      << "    \"sovereign\": " << (cfg.sovereign ? "true" : "false") << ",\n"
      << "    \"segments_pd_tr\": " << applied[0] << ",\n    \"segments_lgd_lr\": " << applied[1] << ",\n"
      << "    \"segments_unavailable\": " << unavailable << ",\n    \"pivots\": {";
    for (std::size_t k = 0; k < b.pivots.size(); ++k) {
        const auto& p = b.pivots[k];
        auto share = [&](double x) { return fixed(p.exposure > 0 ? x / p.exposure : 0.0, 9); };
        f << (k ? ",\n" : "\n") << "      \"" << json_escape(p.key) << "\": {\"exposure\": " << fixed(p.exposure, 2);
        for (std::size_t g = 0; g < kBenchmarkGroups; ++g)
            f << ", \"" << kBenchmarkGroupNames[g] << "_model_coverage\": " << share(p.model[g]);
        for (std::size_t g = 0; g < kBenchmarkGroups; ++g)
            f << ", \"" << kBenchmarkGroupNames[g] << "_benchmark_share\": " << share(p.benchmark[g]);
        f << '}';
    }
    f << "\n    }\n  }";
}

void apply_benchmark(const SegmentBenchmark& b, std::array<ParamPath, 2>& paths) {
    if (!b.any()) return;
    for (std::size_t sc = 0; sc < 2; ++sc) {
        for (std::size_t t = 0; t < 3; ++t)
            for (std::size_t g = 0; g < kBenchmarkGroups; ++g)
                if (b.applied[g])
                    for (auto p : kBenchmarkGroupParams[g]) param_field(paths[sc][t + 1], p) = param_field(b.values[sc][t], p);
        paths[sc][4] = paths[sc][3];
    }
}

}  // namespace sora
