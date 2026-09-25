// sora - Sora stress engine command line.
//
//   sora inspect   <sim>                                       dataset summary and input checks
//   sora calibrate <sim> --scenario <yaml> -o <dir>            starting-point parameters only
//   sora run       <sim> --scenario <yaml> -o <dir>            calibration + projection
//
// Options: --base <dir> (resolve scenario paths, default: cwd), --memory-limit 1GB, --threads N

#include <sys/resource.h>

#include <chrono>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <map>
#include <string>

#include "sora/output.hpp"

namespace fs = std::filesystem;
using namespace sora;

namespace {

struct Args {
    std::string command;
    fs::path sim, scenario, out, base = fs::current_path();
    DuckOptions duck;
};

[[noreturn]] void usage() {
    std::fprintf(stderr,
                 "usage: sora inspect <sim> [options]\n"
                 "       sora calibrate <sim> --scenario <yaml> -o <dir> [options]\n"
                 "       sora run <sim> --scenario <yaml> -o <dir> [options]\n"
                 "options: --base <dir> --memory-limit <size> --threads <n>\n");
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
        else if (!std::strcmp(argv[i], "--memory-limit")) a.duck.memory_limit = next();
        else if (!std::strcmp(argv[i], "--threads")) a.duck.threads = std::stoi(next());
        else usage();
    }
    if (a.command != "inspect" && (a.scenario.empty() || a.out.empty())) usage();
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

        Calibration cal;
        { Timer t("calibrate"); cal = calibrate(duck, d, seg, cfg.calibration); }
        MacroTable macro;
        std::map<std::string, Satellite> sats;
        { Timer t("load scenario"); macro = load_macro(duck, cfg.macro_path); sats = load_satellites(duck, cfg.satellites_path); }
        Projection proj;
        const bool run = a.command == "run";
        if (run) { Timer t("project"); proj = project(d, seg, cal, sats, macro, cfg); }
        { Timer t("write outputs"); write_outputs({d, seg, cal, run ? &proj : nullptr, macro, cfg, diag}, a.out); }
        print_findings(diag);
        std::fprintf(stderr, "  %zu exposures, %zu segments -> %s (peak RSS %.1f MB)\n", seg.in_scope,
                     seg.segments.size(), a.out.string().c_str(), peak_rss_mb());
        return 0;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "sora: %s\n", e.what());
        return 1;
    }
}
