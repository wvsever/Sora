#include "sora/output.hpp"

#include "sora/collateral.hpp"
#include "sora/cr_scen.hpp"
#include "sora/cr_sector.hpp"
#include "sora/json_text.hpp"

#include <cstdio>
#include <fstream>
#include <map>

namespace sora {

namespace fs = std::filesystem;

namespace {

std::string money(double x) {
    char buf[64];
    std::snprintf(buf, sizeof buf, "%.2f", x);
    return buf;
}

std::string rate(double x) {
    char buf[64];
    std::snprintf(buf, sizeof buf, "%.9f", x);
    return buf;
}

std::ofstream open(const fs::path& p) {
    std::ofstream f(p, std::ios::binary);
    if (!f) throw Error("cannot write " + p.string());
    return f;
}

void write_params_row(std::ofstream& f, const std::string& key, const char* scen, int year, const Params& p,
                      const std::string& levels, const std::string& source = "derived") {
    f << "segment," << key << ',' << scen << ',' << year << ',' << rate(p.pd12m_s1) << ',' << rate(p.pd12m_s2) << ','
      << rate(p.tr1_2) << ',' << rate(p.tr2_1) << ',' << rate(p.tr3_1) << ',' << rate(p.tr3_2) << ','
      << rate(p.lgd_s1) << ',' << rate(p.lgd_s2) << ',' << rate(p.lgd_s3) << ',' << rate(p.lrlt_s2)
      << ',' << source << ',' << levels << '\n';
}

// sector_parameters.csv: the sectoral satellite paths per NFC segment and sector with coefficients, and where each
// group comes from (sectoral, portfolio, benchmark; none = flat, portfolio without satellite coefficients).
void write_sector_parameters(const Segmentation& s, const Projection& p, const fs::path& file) {
    auto f = open(file);
    f << "segment,sector,gva_sector,gva_key,gva_relative,scenario,year,pd12m_s1,pd12m_s2,tr1_2,tr2_1,tr3_1,tr3_2,lgd_s1,"
         "lgd_s2,lgd_s3,lrlt_s2,pd_tr,lgd_lr\n";
    for (std::size_t i = 0; i < s.segments.size(); ++i) {
        const auto* bm = p.benchmark_of(i);
        for (const auto& sp : p.sector_paths[i]) {
            const char* use[2];
            for (std::size_t g = 0; g < 2; ++g)
                use[g] = bm && bm->applied[g] ? "benchmark" : sp.model.coef.covers(g) ? "sectoral" : p.portfolio_model[i] ? "portfolio" : "none";
            for (std::size_t sc = 0; sc < 2; ++sc)
                for (std::size_t t = 1; t <= 3; ++t) {
                    f << s.segments[i].key << ',' << sector_code(sp.model.sector) << ',' << gva_sector(sp.model.sector) << ','
                      << sp.model.gva_key << ',' << (sp.model.relative ? 1 : 0) << ',' << kScenarios[sc] << ',' << t;
                    for (std::size_t k = 0; k < kParamCount; ++k) f << ',' << rate(param_field(sp.params[sc][t], k));
                    f << ',' << use[0] << ',' << use[1] << '\n';
                }
        }
    }
}

// The summary.json "sector_satellites" object: settings, sectors with a model per group, and the t0 exposure of the
// NFC portfolio projected with sectoral models per group (after the ECB benchmark rule).
void write_sector_summary(std::ostream& f, const Dataset& d, const Segmentation& s, const Projection& p,
                          const ScenarioConfig& cfg) {
    std::size_t sectors[2] = {};
    for (const auto& c : p.sector_coefficients)
        for (std::size_t g = 0; g < 2; ++g) sectors[g] += c && c->covers(g) ? 1 : 0;
    std::size_t relative = 0;
    for (const auto& paths : p.sector_paths) {
        bool any = false;
        for (const auto& sp : paths) any = any || sp.model.relative;
        relative += any ? 1 : 0;
    }
    double total = 0, used[2] = {};
    for (std::size_t i = 0; i < d.exposures.size(); ++i) {
        const auto sid = s.segment_of[i];
        const auto& e = d.exposures[i];
        if (sid < 0 || e.stage == Stage::NotApplicable || !has_sector_breakdown(s.segments[static_cast<std::size_t>(sid)])) continue;
        const double g = to_double(e.gca) * s.fx[i];
        total += g;
        const auto* sp = p.sector_path(static_cast<std::size_t>(sid), d.counterparties[e.counterparty].nace);
        for (std::size_t k = 0; k < 2; ++k) used[k] += sp && sp->sectoral[k] ? g : 0.0;
    }
    f << "{\n    \"file\": \"" << json_escape(cfg.sector_satellites.file.filename().string()) << "\",\n    \"gva_fallback\": [";
    for (std::size_t k = 0; k < cfg.sector_satellites.gva_fallback.size(); ++k)
        f << (k ? ", " : "") << '"' << json_escape(cfg.sector_satellites.gva_fallback[k]) << '"';
    f << "],\n    \"sectors_pd_tr\": " << sectors[0] << ",\n    \"sectors_lgd_lr\": " << sectors[1]
      << ",\n    \"segments_gva_relative\": " << relative << ",\n    \"nfc_exposure\": " << money(total)
      << ",\n    \"pd_tr_exposure\": " << money(used[0]) << ",\n    \"lgd_lr_exposure\": " << money(used[1])
      << ",\n    \"pd_tr_share\": " << rate(total > 0 ? used[0] / total : 0.0)
      << ",\n    \"lgd_lr_share\": " << rate(total > 0 ? used[1] / total : 0.0) << "\n  }";
}

}  // namespace

void write_outputs(const RunOutput& run, const fs::path& dir) {
    fs::create_directories(dir);
    const auto& d = run.dataset;
    const auto& s = run.segmentation;
    const auto nseg = s.segments.size();

    // t0 stocks per segment: [S1,S2,S3,POCI] x [exposure, provision]
    std::vector<std::array<std::array<double, 2>, 4>> stock(nseg);
    std::vector<std::size_t> contracts(nseg, 0);
    for (std::size_t i = 0; i < d.exposures.size(); ++i) {
        const auto sid = s.segment_of[i];
        if (sid < 0) continue;
        const auto& e = d.exposures[i];
        const auto st = static_cast<std::size_t>(e.stage);
        contracts[static_cast<std::size_t>(sid)] += 1;
        if (st > 3) continue;
        stock[static_cast<std::size_t>(sid)][st][0] += to_double(e.gca) * s.fx[i];
        stock[static_cast<std::size_t>(sid)][st][1] += s.allowance[i];
    }

    {
        auto f = open(dir / "segments.csv");
        f << "segment,instrument,portfolio,country,macro_key,contracts,exp_s1,exp_s2,exp_s3,exp_poci,"
             "prov_s1,prov_s2,prov_s3,prov_poci\n";
        for (std::size_t i = 0; i < nseg; ++i) {
            const auto& g = s.segments[i];
            f << g.key << ',' << g.instrument << ',' << g.portfolio << ',' << g.bucket << ','
              << macro_key(run.macro, g.bucket, run.config) << ',' << contracts[i];
            for (int k = 0; k < 2; ++k)
                for (int st = 0; st < 4; ++st) f << ',' << money(stock[i][static_cast<std::size_t>(st)][static_cast<std::size_t>(k)]);
            f << '\n';
        }
    }
    {
        auto f = open(dir / "parameters.csv");
        f << "level,key,scenario,year,pd12m_s1,pd12m_s2,tr1_2,tr2_1,tr3_1,tr3_2,lgd_s1,lgd_s2,lgd_s3,lrlt_s2,source,"
             "calibration_levels\n";
        for (std::size_t i = 0; i < nseg; ++i) {
            const auto& src = run.calibration.sources[i];
            // Sorted by part name, as in the reference: lgd, lrlt, stage1, stage2, stage3.
            const std::string levels = "lgd=" + src[3] + ";lrlt=" + src[4] + ";stage1=" + src[0] + ";stage2=" + src[1] +
                                       ";stage3=" + src[2];
            if (run.projection) {
                write_params_row(f, s.segments[i].key, "actual", 0, run.projection->params[i][0][0], levels,
                                 run.projection->start_source[i]);
            } else {
                write_params_row(f, s.segments[i].key, "actual", 0, run.calibration.params[i], levels);
            }
            if (run.projection) {
                for (std::size_t sc = 0; sc < 2; ++sc)
                    for (int t = 1; t <= 3; ++t)
                        write_params_row(f, s.segments[i].key, kScenarios[sc], t,
                                         run.projection->params[i][sc][static_cast<std::size_t>(t)], "",
                                         run.projection->path_source[i][sc][static_cast<std::size_t>(t - 1)]);
            }
        }
    }

    std::map<std::string, std::array<double, 21>> totals;   // "scenario/year" -> sums
    if (run.projection) {
        auto f = open(dir / "projection.csv");
        f << "segment,scenario,year";
        for (auto name : kYearResultFields) f << ',' << name;
        f << '\n';
        for (std::size_t i = 0; i < nseg; ++i) {
            for (std::size_t sc = 0; sc < 2; ++sc) {
                for (std::size_t t = 0; t < 3; ++t) {
                    const auto v = fields(run.projection->results[i][sc][t]);
                    f << s.segments[i].key << ',' << kScenarios[sc] << ',' << (t + 1);
                    for (double x : v) f << ',' << money(x);
                    f << '\n';
                    auto& tot = totals[std::string(kScenarios[sc]) + "/" + std::to_string(t + 1)];
                    for (std::size_t k = 0; k < v.size(); ++k) tot[k] += v[k];
                }
            }
        }
    }

    if (run.projection) {
        const auto coll = collateral_ltv(d, s, run.macro, run.config);
        write_collateral(s, coll, dir / "collateral.csv");
        write_cr_scen(d, s, *run.projection, dir / "cr_scen.csv", &coll);
        write_cr_sector(d, s, *run.projection, dir / "cr_sector.csv");
    }
    if (run.projection && run.projection->benchmark.enabled) write_benchmarks(s, run.projection->benchmark, dir / "benchmarks.csv");
    if (run.projection && run.projection->sectoral) write_sector_parameters(s, *run.projection, dir / "sector_parameters.csv");
    if (run.rea) write_rea_csv(s, *run.rea, dir / "rea.csv");
    if (run.off_balance) write_off_balance(d, s, *run.off_balance, dir);

    {
        auto f = open(dir / "summary.json");
        double sp[8] = {};
        for (std::size_t i = 0; i < nseg; ++i)
            for (int st = 0; st < 4; ++st) {
                sp[st] += stock[i][static_cast<std::size_t>(st)][0];
                sp[4 + st] += stock[i][static_cast<std::size_t>(st)][1];
            }
        const char* spn[8] = {"exp_s1", "exp_s2", "exp_s3", "exp_poci", "prov_s1", "prov_s2", "prov_s3", "prov_poci"};
        f << "{\n  \"reference_date\": \"" << json_escape(d.manifest.reference_date) << "\",\n"
          << "  \"sim_mapping_release\": \"" << json_escape(d.manifest.mapping_release) << "\",\n"
          << "  \"scenario\": \"" << json_escape(run.config.name) << "\",\n"
          << "  \"segments\": " << nseg << ",\n  \"exposures\": " << s.in_scope << ",\n  \"starting_point\": {";
        for (int k = 0; k < 8; ++k) f << (k ? ",\n" : "\n") << "    \"" << spn[k] << "\": " << money(sp[k]);
        f << "\n  },\n  \"totals\": {";
        bool first = true;
        for (const auto& [key, tot] : totals) {
            f << (first ? "\n" : ",\n") << "    \"" << key << "\": {";
            for (std::size_t k = 0; k < tot.size(); ++k)
                f << (k ? ",\n" : "\n") << "      \"" << kYearResultFields[k] << "\": " << money(tot[k]);
            f << "\n    }";
            first = false;
        }
        f << "\n  }";
        if (run.projection && run.projection->benchmark.enabled) {
            f << ",\n  \"benchmark\": ";
            write_benchmark_summary(f, run.config.benchmark, run.projection->benchmark);
        }
        if (run.projection && run.projection->sectoral) {
            f << ",\n  \"sector_satellites\": ";
            write_sector_summary(f, d, s, *run.projection, run.config);
        }
        if (run.rea) {
            f << ",\n  \"rea\": ";
            write_rea_summary(f, *run.rea);
        }
        if (run.off_balance) {
            f << ",\n  \"off_balance\": ";
            write_off_balance_summary(f, *run.off_balance);
        }
        f << "\n}\n";
    }

    {
        auto f = open(dir / "diagnostics.json");
        f << "{\n  \"findings\": [";
        for (std::size_t i = 0; i < run.diagnostics.findings.size(); ++i) {
            const auto& x = run.diagnostics.findings[i];
            f << (i ? ",\n" : "\n") << "    {\"id\": \"" << json_escape(x.id) << "\", \"severity\": \"" << json_escape(x.severity)
              << "\", \"count\": " << x.count << ", \"message\": \"" << json_escape(x.message) << "\"}";
        }
        f << "\n  ]\n}\n";
    }
}

}  // namespace sora
