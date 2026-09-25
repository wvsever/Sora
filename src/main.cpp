// sora - Sora stress engine command line.
//
//   sora inspect   <sim>                                       dataset summary and input checks
//   sora calibrate <sim> --scenario <yaml> -o <dir>            starting-point parameters only
//   sora run       <sim> --scenario <yaml> -o <dir>            calibration + projection (+ cr_scen.csv)
//
// Options: --base <dir> (resolve scenario paths, default: cwd), --memory-limit 1GB, --threads N (DuckDB),
//          --temp-dir <dir> (DuckDB spill files for sorts beyond the memory limit),
//          --workers N (engine threads, default: hardware concurrency; results do not depend on it),
//          --parameters <file|dir>  customer risk parameters (default: SIM table sim_risk_parameter if present)
//          --calculator <url>  IRB REA through the regulatory calculator (rea.csv), with --calculator-cache <dir>,
//          --calculator-ca <file>, --calculator-cert <file> --calculator-key <file>, --calculator-batch <n>;
//          bearer token from $SORA_CALCULATOR_TOKEN

#include <sys/resource.h>

#include <chrono>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <map>
#include <optional>
#include <string>

#include "sora/output.hpp"

namespace fs = std::filesystem;
using namespace sora;

namespace {

struct Args {
    std::string command;
    fs::path sim, scenario, out, parameters, base = fs::current_path();
    DuckOptions duck;
    calc::ClientOptions calculator;
    unsigned workers = 0;   // 0 = hardware concurrency
};

[[noreturn]] void usage() {
    std::fprintf(stderr,
                 "usage: sora inspect <sim> [options]\n"
                 "       sora calibrate <sim> --scenario <yaml> -o <dir> [options]\n"
                 "       sora run <sim> --scenario <yaml> -o <dir> [options]\n"
                 "options: --base <dir> --parameters <file|dir> --memory-limit <size> --threads <n> --workers <n>\n"
                 "         --temp-dir <dir>\n"
                 "         --calculator <url> [--calculator-cache <dir>] [--calculator-ca <file>]\n"
                 "         [--calculator-cert <file> --calculator-key <file>] [--calculator-batch <n>]\n");
    std::exit(2);
}

Args parse(int argc, char** argv) {
    if (argc < 3) usage();
    Args a;
    a.command = argv[1];
    a.sim = argv[2];
    for (int i = 3; i < argc; ++i) {
        auto next = [&]() -> std::string { if (i + 1 >= argc) usage(); return argv[++i]; };
        if (!std::strcmp(argv[i], "--scenario")) a.scenario = next();
        else if (!std::strcmp(argv[i], "-o") || !std::strcmp(argv[i], "--output")) a.out = next();
        else if (!std::strcmp(argv[i], "--base")) a.base = next();
        else if (!std::strcmp(argv[i], "--parameters")) a.parameters = next();
        else if (!std::strcmp(argv[i], "--memory-limit")) a.duck.memory_limit = next();
        else if (!std::strcmp(argv[i], "--threads")) a.duck.threads = std::stoi(next());
        else if (!std::strcmp(argv[i], "--calculator")) a.calculator.url = next();
        else if (!std::strcmp(argv[i], "--calculator-cache")) a.calculator.cache_dir = next();
        else if (!std::strcmp(argv[i], "--calculator-ca")) a.calculator.ca_file = next();
        else if (!std::strcmp(argv[i], "--calculator-cert")) a.calculator.cert_file = next();
        else if (!std::strcmp(argv[i], "--calculator-key")) a.calculator.key_file = next();
        else if (!std::strcmp(argv[i], "--calculator-batch")) a.calculator.max_batch = std::stoul(next());
        else if (!std::strcmp(argv[i], "--temp-dir")) a.duck.temp_directory = next();
        else if (!std::strcmp(argv[i], "--workers")) a.workers = static_cast<unsigned>(std::stoul(next()));
        else usage();
    }
    if (a.command != "inspect" && (a.scenario.empty() || a.out.empty())) usage();
    if (a.calculator.cert_file.empty() != a.calculator.key_file.empty()) usage();
    return a;
}

double peak_rss_mb() {
    rusage ru{};
    getrusage(RUSAGE_SELF, &ru);
    return static_cast<double>(ru.ru_maxrss) / 1024.0;
}

class Timer {
public:
    explicit Timer(const char* what) : what_(what), t0_(std::chrono::steady_clock::now()) {}
    ~Timer() {
        const auto ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0_).count();
        std::fprintf(stderr, "  %-22s %9.1f ms\n", what_, ms);
    }
private:
    const char* what_;
    std::chrono::steady_clock::time_point t0_;
};

void print_findings(const Diagnostics& diag) {
    for (const auto& f : diag.findings)
        std::fprintf(stderr, "  %-7s %-11s %s%s\n", f.severity.c_str(), f.id.c_str(), f.message.c_str(),
                     f.count ? (" (" + std::to_string(f.count) + ")").c_str() : "");
}

}  // namespace

int main(int argc, char** argv) {
    try {
        const Args a = parse(argc, argv);
        Duck duck(a.duck);
        Dataset d;
        { Timer t("load SIM"); d = load_dataset(duck, a.sim); }

        ScenarioConfig cfg;
        if (!a.scenario.empty()) cfg = load_scenario(a.scenario, a.base);
        Segmentation seg;
        { Timer t("segment"); seg = segment(d, cfg.scope); }
        Diagnostics diag;
        { Timer t("check inputs"); diag = check_inputs(duck, d, seg); }

        if (a.command == "inspect") {
            std::map<std::string, std::size_t> by_type;
            for (const auto& e : d.exposures) by_type[std::string(to_string(e.type))] += 1;
            std::printf("SIM %s, reference date %s, reporting currency %s, mapping %s\n", d.manifest.sim_version.c_str(),
                        d.manifest.reference_date.c_str(), d.manifest.reporting_currency.c_str(), d.manifest.mapping_release.c_str());
            std::printf("  %zu counterparties, %zu exposures, %zu entities, %zu currencies\n", d.counterparties.size(),
                        d.exposures.size(), d.entities.size(), d.currencies.size());
            for (const auto& [t, n] : by_type) std::printf("    %-22s %8zu\n", t.c_str(), n);
            std::printf("  in scope (default EBA scope): %zu exposures in %zu segments\n", seg.in_scope, seg.segments.size());
            print_findings(diag);
            std::fprintf(stderr, "  peak RSS %.1f MB\n", peak_rss_mb());
            return diag.has_errors() ? 1 : 0;
        }
        if (a.command != "calibrate" && a.command != "run") usage();
        if (diag.has_errors()) {
            print_findings(diag);
            return 1;
        }

        // Customer parameters: --parameters, else the SIM table if present.
        ExternalParameters ext;
        std::string ext_source;
        if (!a.parameters.empty()) {
            const auto ext_name = a.parameters.extension().string();
            ext_source = fs::is_directory(a.parameters) ? sim_source(a.parameters.parent_path(), a.parameters.filename().string())
                         : ext_name == ".parquet" ? "read_parquet(" + sql_quote(a.parameters.string()) + ")"
                                                  : "read_csv(" + sql_quote(a.parameters.string()) + ", header = true, all_varchar = true)";
        } else if (sim_table_exists(d.sim_dir, "sim_risk_parameter")) {
            ext_source = sim_source(d.sim_dir, "sim_risk_parameter");
        }
        if (!ext_source.empty()) {
            Timer t("load parameters");
            ext.load(duck, ext_source, d);
            std::string unmatched;
            const auto um = ext.unmatched_segment_keys(seg);
            for (std::size_t i = 0; i < um.size() && i < 5; ++i) unmatched += (i ? ", " : "") + um[i];
            if (!um.empty()) diag.findings.push_back({"PAR-001", "warning", "segment keys match no segment: " + unmatched, um.size()});
            if (!ext.unknown_keys.empty())
                diag.findings.push_back({"PAR-002", "warning", "exposure keys not in sim_exposure (e.g. " + ext.unknown_keys.front() + ")", ext.unknown_keys.size()});
            diag.findings.push_back({"PAR-000", "info", "customer risk parameters loaded", ext.rows()});
        }

        Calibration cal;
        { Timer t("calibrate"); cal = calibrate(duck, d, seg, cfg.calibration); }
        MacroTable macro;
        std::map<std::string, Satellite> sats;
        { Timer t("load scenario"); macro = load_macro(duck, cfg.macro_path); sats = load_satellites(duck, cfg.satellites_path); }
        Projection proj;
        const bool run = a.command == "run";
        if (run) {
            Timer t("project");
            proj = project(d, seg, cal, sats, macro, cfg, ext.empty() ? nullptr : &ext, a.workers);
            if (proj.exposures_with_own_parameters)
                diag.findings.push_back({"PAR-003", "info", "exposures with exposure-level parameters", proj.exposures_with_own_parameters});
            if (!proj.parameter_errors.empty()) {
                for (std::size_t i = 0; i < proj.parameter_errors.size() && i < 10; ++i)
                    std::fprintf(stderr, "  error   PAR-010     %s\n", proj.parameter_errors[i].c_str());
                std::fprintf(stderr, "sora: %zu invalid parameter values; no results written\n", proj.parameter_errors.size());
                return 1;
            }
        }
        std::optional<ReaResult> rea;
        if (run && !a.calculator.url.empty()) {
            Timer t("calculator (IRB REA)");
            rea = project_rea(duck, {d, seg, proj, cfg, sats, macro, ext.empty() ? nullptr : &ext, ext_source}, a.calculator);
            diag.findings.insert(diag.findings.end(), rea->findings.begin(), rea->findings.end());
            if (rea->all_rejected) {
                print_findings(diag);
                std::fprintf(stderr, "sora: the calculator rejected every record; no results written\n");
                return 1;
            }
        }
        { Timer t("write outputs"); write_outputs({d, seg, cal, run ? &proj : nullptr, macro, cfg, diag, rea ? &*rea : nullptr}, a.out); }
        print_findings(diag);
        std::fprintf(stderr, "  %zu exposures, %zu segments -> %s (peak RSS %.1f MB)\n", seg.in_scope,
                     seg.segments.size(), a.out.string().c_str(), peak_rss_mb());
        return 0;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "sora: %s\n", e.what());
        return 1;
    }
}
