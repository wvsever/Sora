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

#ifdef _WIN32
#include <windows.h>
#include <psapi.h>
#else
#include <sys/resource.h>
#endif

#include <algorithm>
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
                 "         [--calculator-cert <file> --calculator-key <file>] [--calculator-batch <n>]   (run only)\n");
    std::exit(2);
}

Args parse(int argc, char** argv) {
    if (argc < 3) usage();
    Args a;
    a.command = argv[1];
    a.sim = argv[2];
    const char* calculator_option = nullptr;   // the first --calculator* option (rejected unless `run`)
    for (int i = 3; i < argc; ++i) {
        auto next = [&]() -> std::string { if (i + 1 >= argc) usage(); return argv[++i]; };
        if (!calculator_option && !std::strncmp(argv[i], "--calculator", 12)) calculator_option = argv[i];
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
    if (calculator_option && a.command != "run") {
        std::fprintf(stderr, "sora: %s is an option of `sora run` only\n", calculator_option);
        usage();
    }
    if (a.calculator.cert_file.empty() != a.calculator.key_file.empty()) usage();
    return a;
}

double peak_rss_mb() {
#ifdef _WIN32
    PROCESS_MEMORY_COUNTERS pmc{};
    if (!GetProcessMemoryInfo(GetCurrentProcess(), &pmc, sizeof(pmc))) return 0.0;
    return static_cast<double>(pmc.PeakWorkingSetSize) / (1024.0 * 1024.0);
#else
    rusage ru{};
    getrusage(RUSAGE_SELF, &ru);
    return static_cast<double>(ru.ru_maxrss) / 1024.0;
#endif
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
        BenchmarkTable benchmarks;   // ECB benchmark parameters (scenario key benchmark_parameters)
        SectorSatellites sector_sats{};   // sectoral (GVA) satellites (scenario key sector_satellites)
        if (run && cfg.sector_satellites.enabled) {
            Timer t("load sector satellites");
            sector_sats = load_sector_satellites(duck, cfg.sector_satellites.file);
            std::size_t n = 0;
            for (const auto& x : sector_sats) n += x ? 1 : 0;
            diag.findings.push_back({"SEC-000", "info", "NACE sectors with sectoral satellite coefficients", n});
        }
        if (run && cfg.benchmark.enabled) {
            Timer t("load benchmarks");
            benchmarks.load(duck, cfg.benchmark.file, cfg);
            diag.findings.push_back({"BMK-000", "info", "benchmark parameter values loaded (" + std::to_string(benchmarks.keys()) +
                                     " portfolio-country keys)", benchmarks.rows()});
        }
        if (run) {
            Timer t("project");
            proj = project(d, seg, cal, sats, macro, cfg, ext.empty() ? nullptr : &ext, a.workers,
                           cfg.benchmark.enabled ? &benchmarks : nullptr, cfg.sector_satellites.enabled ? &sector_sats : nullptr);
            if (proj.sectoral) {
                std::size_t relative = 0;
                for (const auto& paths : proj.sector_paths)
                    relative += !paths.empty() && std::any_of(paths.begin(), paths.end(), [](const SectorPath& x) { return x.model.relative; }) ? 1 : 0;
                diag.findings.push_back({"SEC-001", "info", "NFC exposures projected with sectoral satellites", proj.exposures_with_sector_model});
                if (relative)
                    diag.findings.push_back({"SEC-002", "info", "NFC segments without sectoral GVA paths (non-EU): GDP growth plus the sector's GVA deviation from GDP in the gva_fallback key", relative});
            }
            if (proj.benchmark.enabled) {
                std::size_t applied = 0, unavailable = 0;
                for (const auto& dec : proj.benchmark.decisions) {
                    applied += dec[0].applied() || dec[1].applied() ? 1 : 0;
                    unavailable += dec[0].unavailable || dec[1].unavailable ? 1 : 0;
                }
                diag.findings.push_back({"BMK-001", "info", "segments with ECB benchmark parameters", applied});
                if (unavailable)
                    diag.findings.push_back({"BMK-002", "warning", "segments that need ECB benchmark parameters but have none in the file: model parameters kept", unavailable});
            }
            if (proj.exposures_with_own_parameters)
                diag.findings.push_back({"PAR-003", "info", "exposures with exposure-level parameters", proj.exposures_with_own_parameters});
            if (!proj.parameter_errors.empty()) {
                for (std::size_t i = 0; i < proj.parameter_errors.size() && i < 10; ++i)
                    std::fprintf(stderr, "  error   PAR-010     %s\n", proj.parameter_errors[i].c_str());
                std::fprintf(stderr, "sora: %zu invalid parameter values; no results written\n", proj.parameter_errors.size());
                return 1;
            }
        }
        std::optional<OffBalanceResult> off_balance;   // CR_SCEN_OFF_BS (scenario key off_balance)
        if (run && cfg.off_balance.enabled) {
            Timer t("off-balance");
            off_balance = project_off_balance(duck, d, seg, proj, macro, cfg, ext.empty() ? nullptr : &ext, ext_source, a.workers);
            if (off_balance->fallback_items)
                diag.findings.push_back({"OBS-001", "info", "off-balance items without an on-balance loan segment of their country: parameters of the portfolio's OTHER bucket", off_balance->fallback_items});
            if (off_balance->unmatched_items)
                diag.findings.push_back({"OBS-002", "warning", "off-balance items without an on-balance loan segment of their portfolio: not projected", off_balance->unmatched_items});
            if (off_balance->customer_ccf_items)
                diag.findings.push_back({"OBS-003", "info", "off-balance items with a customer CCF", off_balance->customer_ccf_items});
            if (off_balance->loan_undrawn_items)
                diag.findings.push_back({"OBS-004", "info", "undrawn parts of on-balance loans projected as loan commitments given", off_balance->loan_undrawn_items});
            if (off_balance->commitment_drawn_exposures)
                diag.findings.push_back({"OBS-005", "info", "drawn parts of commitments projected on-balance (loans and advances)", off_balance->commitment_drawn_exposures});
        }
        // Prior-year Actual rows of CR_SCEN / CR_SECTOR: stocks at the prior year-end from the stage history.
        std::optional<PriorYear> prior;
        if (run) {
            Timer t("prior year-end");
            prior = load_prior_year(duck, d, seg, cfg.scope, prior_year_end(d.manifest.reference_date, cfg.prior_year_end));
            if (!prior->available)
                diag.findings.push_back({"PRY-001", "warning", "no stage history at the prior year-end " + prior->date + ": prior-year rows blank", 0});
            else
                diag.findings.push_back({"PRY-000", "info", "exposures in scope with stage history at the prior year-end " + prior->date, prior->exposures});
            if (prior->amount_principal)
                diag.findings.push_back({"PRY-002", "info", "prior year-end exposure from principal_outstanding (no gross carrying amount)", prior->amount_principal});
            if (prior->missing_amount)
                diag.findings.push_back({"PRY-003", "warning", "exposures without an amount at the prior year-end: exposure cells of their rows blank", prior->missing_amount});
            if (prior->missing_fx)
                diag.findings.push_back({"PRY-004", "warning", "exposures without an FX rate at the prior year-end: exposure and provision cells of their rows blank", prior->missing_fx});
            if (prior->allowance_split_t0_share)
                diag.findings.push_back({"PRY-005", "info", "facilities without undrawn history: prior-year allowance split with the t0 drawn share", prior->allowance_split_t0_share});
            if (prior->not_in_sim_exposure)
                diag.findings.push_back({"PRY-006", "info", "stage history rows at the prior year-end of exposures not in sim_exposure (derecognised): no t0 portfolio, not reported", prior->not_in_sim_exposure});
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
        { Timer t("write outputs"); write_outputs({d, seg, cal, run ? &proj : nullptr, macro, cfg, diag, rea ? &*rea : nullptr,
                                  off_balance ? &*off_balance : nullptr, prior ? &*prior : nullptr}, a.out); }
        print_findings(diag);
        std::fprintf(stderr, "  %zu exposures, %zu segments -> %s (peak RSS %.1f MB)\n", seg.in_scope,
                     seg.segments.size(), a.out.string().c_str(), peak_rss_mb());
        return 0;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "sora: %s\n", e.what());
        return 1;
    }
}
