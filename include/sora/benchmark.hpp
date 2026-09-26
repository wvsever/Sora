#pragma once
// ECB credit-risk benchmark parameters and their application rule (scenario key `benchmark_parameters`).
//
// EBA 2027 draft methodological note (2025 final MN in brackets):
//   para 113 (122)  models first: benchmarks only where no appropriate satellite model is available;
//   para 115 (124)  such portfolios use the ECB benchmark parameters without any adjustment, at portfolio level,
//                   not at rating class level;
//   para 117 (126)  if the satellite models do not estimate the PD/TR and LR/LGD parameters, respectively, for at
//                   least 10% of the pivot asset class exposure, the benchmark applies to the entire pivot asset
//                   class; above 10%, a weighted average of model and benchmark parameters is allowed; CR_SCEN
//                   reports the percentage of exposures with benchmark parameters;
//   para 146 (155)  sovereign exposures: the ECB parameters are mandatory for every country they are given for;
//   footnote 13     no benchmarks for debt securities to CB, CI, OFC and NFC, nor for loans to central banks.
//
// Sora's rule, per pivot asset class (instrument|portfolio) and parameter group (PD/TR, LGD/LR):
//   * model coverage = share of t0 exposure (gross carrying amount) whose segment has a satellite model and
//     whose group starting point was estimated within the pivot class: calibrated at the segment or portfolio
//     level (BenchmarkConfig::model_level), or projected by the customer for all years at such a level;
//   * general governments take the benchmark of their own country where the file has one (`sovereign`);
//   * coverage < threshold: every segment of the pivot class takes the benchmark;
//   * otherwise the segments without a model take it (exposure-weighted mix at pivot level, para 117);
//   * benchmark key: the segment's country, then BenchmarkConfig::country_fallback; if none has the group, the
//     model parameters are kept and the segment is reported as unavailable.
// A benchmark replaces the group's projected parameters (years 1..3; year 4 = year 3) of the segment and of every
// exposure in it, after satellite and customer values. The starting point stays the institution's own (MN para 109).

#include <array>
#include <map>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

#include "sora/parameters.hpp"
#include "sora/scenario.hpp"

namespace sora {

inline constexpr std::size_t kBenchmarkGroups = 2;   // 0 = PD/TR, 1 = LGD/LR
inline constexpr std::array<const char*, kBenchmarkGroups> kBenchmarkGroupNames = {"pd_tr", "lgd_lr"};
// Parameter indices (kParamNames) of each group. TR3-1 / TR3-2 are starting-point only, never benchmarked.
inline constexpr std::array<std::array<std::size_t, 4>, kBenchmarkGroups> kBenchmarkGroupParams = {{{0, 1, 2, 3}, {6, 7, 8, 9}}};

// The benchmark file: per instrument|portfolio|country, the groups it has, and their values per scenario
// (0 = baseline, 1 = adverse) and projection year 1..3 (index 0..2).
class BenchmarkTable {
public:
    struct Entry {
        std::array<bool, kBenchmarkGroups> has{};
        std::array<std::array<Params, 3>, 2> values{};
        std::array<std::array<std::array<bool, kParamCount>, 3>, 2> set{};   // loading only
    };
    // Loads Sora's benchmark format (long CSV, `#` comment lines): instrument, portfolio, country, scenario, year
    // (scenario year, mapped to projection years by cfg.year_map; other years are ignored), parameter, value.
    void load(Duck& duck, const std::filesystem::path& csv, const ScenarioConfig& cfg);
    // Sets one value (loading and tests). `scenario` 0 = baseline, 1 = adverse; `year` 1..3.
    void set(std::string_view key, int scenario, int year, std::size_t param, double value);
    // Checks ranges, completeness of each group (all four parameters, both scenarios, years 1..3, or nothing)
    // and stage outflows; sets Entry::has. Throws sora::Error.
    void finish();
    const Entry* find(std::string_view key) const;   // key: instrument|portfolio|country
    std::size_t rows() const noexcept { return rows_; }
    std::size_t keys() const noexcept { return by_key_.size(); }

private:
    std::unordered_map<std::string, Entry> by_key_;
    std::size_t rows_ = 0;
};

enum class BenchmarkRule : std::uint8_t { None, Sovereign, Coverage, NoModel };
std::string_view to_string(BenchmarkRule r) noexcept;

struct BenchmarkDecision {
    bool model = false;                    // group covered by a model
    BenchmarkRule rule = BenchmarkRule::None;
    std::string key;                       // benchmark country applied ("" = none)
    bool unavailable = false;              // required by the rule, but no benchmark in the file
    bool applied() const noexcept { return !key.empty(); }
};

// What the rule does to one segment's parameter paths.
struct SegmentBenchmark {
    std::array<bool, kBenchmarkGroups> applied{};
    std::array<std::array<Params, 3>, 2> values{};   // [scenario][year 1..3 at 0..2]
    bool any() const noexcept { return applied[0] || applied[1]; }
};

struct BenchmarkPivot {
    std::string key;                                   // instrument|portfolio
    double exposure = 0;                               // t0 gross carrying amount (reporting currency)
    std::array<double, kBenchmarkGroups> model{}, benchmark{};   // exposure with a model / with the benchmark
};

struct BenchmarkResult {
    bool enabled = false;
    std::vector<std::array<BenchmarkDecision, kBenchmarkGroups>> decisions;   // per segment
    std::vector<SegmentBenchmark> apply;                                      // per segment
    std::vector<double> exposure;                                             // per segment, t0
    std::vector<BenchmarkPivot> pivots;                                       // sorted by key
};

// The rule for every segment. `external` may be null. Deterministic (serial, exposure order).
BenchmarkResult decide_benchmarks(const Dataset& d, const Segmentation& s, const Calibration& cal,
                                  const std::map<std::string, Satellite>& satellites, const BenchmarkTable& table,
                                  const BenchmarkConfig& cfg, const ExternalParameters* external);

// Replaces the applied groups in years 1..3 of both scenario paths and sets year 4 = year 3.
void apply_benchmark(const SegmentBenchmark& b, std::array<ParamPath, 2>& paths);

// benchmarks.csv: the decision per segment (t0 exposure, model coverage, rule and benchmark key per group).
void write_benchmarks(const Segmentation& s, const BenchmarkResult& b, const std::filesystem::path& file);
// The summary.json "benchmark" object: settings, segment counts and coverage per pivot asset class.
void write_benchmark_summary(std::ostream& f, const BenchmarkConfig& cfg, const BenchmarkResult& b);

}  // namespace sora
