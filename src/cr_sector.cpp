#include "sora/cr_sector.hpp"

#include <cmath>
#include <cstdio>
#include <fstream>
#include <optional>
#include <string>

namespace sora {

namespace fs = std::filesystem;

// ----------------------------------------------------------------------------------------- NACE

namespace {

// NACE Rev. 2.1 section per division 01..99 (same division numbers in Rev. 2). kNaceSectors = no NFC section.
constexpr std::array<std::uint8_t, 100> division_sectors() {
    std::array<std::uint8_t, 100> t{};
    for (auto& x : t) x = static_cast<std::uint8_t>(NaceSector::Unknown);
    struct Range { NaceSector s; int lo, hi; };
    constexpr Range ranges[] = {
        {NaceSector::A, 1, 3}, {NaceSector::B, 5, 9}, {NaceSector::COther, 10, 33}, {NaceSector::D, 35, 35},
        {NaceSector::E, 36, 39}, {NaceSector::F, 41, 43}, {NaceSector::G, 45, 47}, {NaceSector::H, 49, 53},
        {NaceSector::I, 55, 56}, {NaceSector::J, 58, 60}, {NaceSector::K, 61, 63}, {NaceSector::L, 64, 66},
        {NaceSector::M, 68, 68}, {NaceSector::N, 69, 75}, {NaceSector::O, 77, 82}, {NaceSector::P, 84, 84},
        {NaceSector::Q, 85, 85}, {NaceSector::R, 86, 88}, {NaceSector::S, 90, 93}, {NaceSector::T, 94, 96},
        // Energy-intensive manufacturing (2027 draft template guidance, Table 4).
        {NaceSector::CEnergyIntensive, 10, 12}, {NaceSector::CEnergyIntensive, 17, 30},
    };
    for (const auto& r : ranges)
        for (int d = r.lo; d <= r.hi; ++d) t[static_cast<std::size_t>(d)] = static_cast<std::uint8_t>(r.s);
    return t;
}
constexpr auto kDivisionSector = division_sectors();

bool is_digit(char c) { return c >= '0' && c <= '9'; }

}  // namespace

NaceSector nace_sector(std::string_view code) noexcept {
    while (!code.empty() && code.front() == ' ') code.remove_prefix(1);
    while (!code.empty() && code.back() == ' ') code.remove_suffix(1);
    char letter = 0;
    if (!code.empty() && ((code[0] >= 'A' && code[0] <= 'Z') || (code[0] >= 'a' && code[0] <= 'z'))) {
        letter = static_cast<char>(code[0] & ~0x20);   // upper case
        code.remove_prefix(1);
    }
    if (code.size() >= 2 && is_digit(code[0]) && is_digit(code[1]))
        return static_cast<NaceSector>(kDivisionSector[static_cast<std::size_t>((code[0] - '0') * 10 + (code[1] - '0'))]);
    if (letter && code.empty()) {
        if (letter == 'A') return NaceSector::A;
        if (letter == 'B') return NaceSector::B;
        if (letter == 'C') return NaceSector::COther;
        if (letter >= 'D' && letter <= 'T') return static_cast<NaceSector>(static_cast<int>(NaceSector::D) + (letter - 'D'));
    }
    return NaceSector::Unknown;
}

// ----------------------------------------------------------------------------------------- template

namespace {

// Everything a template cell needs, additive over segments and sectors.
struct Agg {
    double exp_s1 = 0, exp_s2 = 0, exp_s3_old = 0, exp_s3_new = 0, exp_poci = 0;
    double prov_s1 = 0, prov_s2 = 0, prov_s3 = 0, prov_poci = 0;
    double flow_s2_s1 = 0, flow_s1_s2 = 0, flow_s1_s3 = 0, flow_s2_s3 = 0;
    double prov_s1_s2 = 0, prov_s2_s2 = 0, prov_s1_s3 = 0, prov_s2_s3 = 0;   // within year
    double cum_s1_s3 = 0, cum_s2_s3 = 0, prov_s1_s1 = 0, prov_s2_s1 = 0, prov_old_s3 = 0;
    ParamAccum pa;

    void add(const Agg& o) {
        exp_s1 += o.exp_s1; exp_s2 += o.exp_s2; exp_s3_old += o.exp_s3_old; exp_s3_new += o.exp_s3_new; exp_poci += o.exp_poci;
        prov_s1 += o.prov_s1; prov_s2 += o.prov_s2; prov_s3 += o.prov_s3; prov_poci += o.prov_poci;
        flow_s2_s1 += o.flow_s2_s1; flow_s1_s2 += o.flow_s1_s2; flow_s1_s3 += o.flow_s1_s3; flow_s2_s3 += o.flow_s2_s3;
        prov_s1_s2 += o.prov_s1_s2; prov_s2_s2 += o.prov_s2_s2; prov_s1_s3 += o.prov_s1_s3; prov_s2_s3 += o.prov_s2_s3;
        cum_s1_s3 += o.cum_s1_s3; cum_s2_s3 += o.cum_s2_s3; prov_s1_s1 += o.prov_s1_s1; prov_s2_s1 += o.prov_s2_s1;
        prov_old_s3 += o.prov_old_s3;
        add_params(o.pa);
    }
    void add_params(const ParamAccum& o) {
        for (std::size_t i = 0; i < 3; ++i) pa.weight[i] += o.weight[i];
        for (std::size_t i = 0; i < kParamCount; ++i) pa.sum[i] += o.sum[i];
    }
};

constexpr std::uint32_t bit(NaceSector s) { return 1U << static_cast<unsigned>(s); }

struct Row {
    int num;
    const char* pivot;
    const char* label;       // NACE code column and row label (2027 draft template)
    std::uint32_t sectors;   // NaceSector bit mask
};

// The TOTAL row also holds exposures of unknown sector, so that it reconciles with CR_SCEN.
constexpr Row kRows[] = {
    {1, "Pivot", "A - Agriculture, forestry and fishing", bit(NaceSector::A)},
    {2, "Pivot", "B - Mining and quarrying", bit(NaceSector::B)},
    {3, "Pivot", "C - Manufacturing", bit(NaceSector::CEnergyIntensive) | bit(NaceSector::COther)},
    {4, "o/w", "C Manufacturing - energy-intensive activities", bit(NaceSector::CEnergyIntensive)},
    {5, "o/w", "C Manufacturing - other", bit(NaceSector::COther)},
    {6, "Pivot", "D - Electricity, gas, steam and air conditioning supply", bit(NaceSector::D)},
    {7, "Pivot", "E - Water supply; sewerage, waste management and remediation activities", bit(NaceSector::E)},
    {8, "Pivot", "F - Construction", bit(NaceSector::F)},
    {9, "Pivot", "G - Wholesale and retail trade", bit(NaceSector::G)},
    {10, "Pivot", "H - Transportation and storage", bit(NaceSector::H)},
    {11, "Pivot", "I - Accommodation and food service activities", bit(NaceSector::I)},
    {12, "Pivot", "J - Publishing, broadcasting, and content production and distribution activities", bit(NaceSector::J)},
    {13, "Pivot", "K - Telecommunication, computer programming, consulting, computing infrastructure and other information service activities", bit(NaceSector::K)},
    {14, "Pivot", "L - Financial and insurance activities", bit(NaceSector::L)},
    {15, "Pivot", "M - Real estate activities", bit(NaceSector::M)},
    {16, "Pivot", "N - Professional, scientific and technical activities", bit(NaceSector::N)},
    {17, "Pivot", "O - Administrative and support service activities", bit(NaceSector::O)},
    {18, "Pivot", "P - Public administration and defence; compulsory social security", bit(NaceSector::P)},
    {19, "Pivot", "Q - Education", bit(NaceSector::Q)},
    {20, "Pivot", "R - Human health and social work activities", bit(NaceSector::R)},
    {21, "Pivot", "S - Arts, sports and recreation", bit(NaceSector::S)},
    {22, "Pivot", "T - Other service activities", bit(NaceSector::T)},
    {23, "Sum", "TOTAL exposures to NFC", (1U << kNaceSectors) - 1},
};

using Value = std::optional<double>;
struct Column {
    const char* header;
    bool percent;
    Value (*get)(const Agg&, bool actual);
};

Value param(const Agg& a, std::size_t i) {
    const double v = a.pa.average(i);
    return std::isnan(v) ? Value{} : Value{v};
}
Value ratio(double num, double den) { return den > 0 ? Value{num / den} : Value{}; }
Value flow(bool actual, double v) { return actual ? Value{} : Value{v}; }

// The 2027 draft CSV_CR_SECTOR columns. Sora has no sectoral models (columns 1-2 are 0: sector results come
// from the segment parameters), and PD / LGD PiT are not produced (blank), as in cr_scen.csv.
constexpr Column kColumns[] = {
    {"PD/TR - Percentage of exposures with projections based on sectoral models, e.g. via sensitivities by sector (%)", true, [](const Agg&, bool) { return Value{0.0}; }},
    {"LGD/LR - Percentage of exposures with projections based on sectoral models, e.g. via sensitivities by sector (%)", true, [](const Agg&, bool) { return Value{0.0}; }},
    {"PD PiT (%)", true, [](const Agg&, bool) { return Value{}; }},
    {"PD 12M S1 (TR1-3)", true, [](const Agg& a, bool) { return param(a, 0); }},
    {"TR1-2", true, [](const Agg& a, bool) { return param(a, 2); }},
    {"PD 12M S2 (TR2-3)", true, [](const Agg& a, bool) { return param(a, 1); }},
    {"TR2-1", true, [](const Agg& a, bool) { return param(a, 3); }},
    {"TR3-1", true, [](const Agg& a, bool actual) { return actual ? param(a, 4) : Value{}; }},
    {"TR3-2", true, [](const Agg& a, bool actual) { return actual ? param(a, 5) : Value{}; }},
    {"LGD PiT new (%)", true, [](const Agg&, bool) { return Value{}; }},
    {"LGD S1", true, [](const Agg& a, bool) { return param(a, 6); }},
    {"LGD S2", true, [](const Agg& a, bool) { return param(a, 7); }},
    {"LRLT S2", true, [](const Agg& a, bool) { return param(a, 9); }},
    {"LGD S3", true, [](const Agg& a, bool) { return param(a, 8); }},
    {"Stage 1 flow (S2-S1 flow)", false, [](const Agg& a, bool actual) { return flow(actual, a.flow_s2_s1); }},
    {"Stage 2 flow (S1-S2 flow)", false, [](const Agg& a, bool actual) { return flow(actual, a.flow_s1_s2); }},
    {"Stage 3 flow (SX-S3 flow)", false, [](const Agg& a, bool actual) { return flow(actual, a.flow_s1_s3 + a.flow_s2_s3); }},
    {"Stage 3 flow from Stage 1 (S1-S3 Flow)", false, [](const Agg& a, bool actual) { return flow(actual, a.flow_s1_s3); }},
    {"Stage 3 flow from Stage 2 (S2-S3 Flow)", false, [](const Agg& a, bool actual) { return flow(actual, a.flow_s2_s3); }},
    {"Provisions stage 1 to stage 2 (Prov S1-S2)", false, [](const Agg& a, bool actual) { return flow(actual, a.prov_s1_s2); }},
    {"Provisions stage 2 to stage 2 (Prov S2-S2)", false, [](const Agg& a, bool actual) { return flow(actual, a.prov_s2_s2); }},
    {"Provisions new stage 3 (Prov SX-S3)", false, [](const Agg& a, bool actual) { return flow(actual, a.prov_s1_s3 + a.prov_s2_s3); }},
    {"Provisions stage 1 to stage 3 (Prov S1-S3)", false, [](const Agg& a, bool actual) { return flow(actual, a.prov_s1_s3); }},
    {"Provisions stage 2 to stage 3 (Prov S2-S3)", false, [](const Agg& a, bool actual) { return flow(actual, a.prov_s2_s3); }},
    {"Cumulative provisions new stage 3 (Prov Cumul SX-S3)", false, [](const Agg& a, bool actual) { return flow(actual, a.cum_s1_s3 + a.cum_s2_s3); }},
    {"Cumulative provisions stage 1 to stage 3 (Prov Cumul S1-S3)", false, [](const Agg& a, bool actual) { return flow(actual, a.cum_s1_s3); }},
    {"Cumulative provisions stage 2 to stage 3 (Prov Cumul S2-S3)", false, [](const Agg& a, bool actual) { return flow(actual, a.cum_s2_s3); }},
    {"Provisions stage 1 to stage 1 (Prov S1-S1)", false, [](const Agg& a, bool actual) { return flow(actual, a.prov_s1_s1); }},
    {"Provisions stage 2 to stage 1 (Prov S2-S1)", false, [](const Agg& a, bool actual) { return flow(actual, a.prov_s2_s1); }},
    {"Provisions old stage 3 (Prov old S3-S3)", false, [](const Agg& a, bool actual) { return flow(actual, a.prov_old_s3); }},
    {"Total exposure (total Exp)", false, [](const Agg& a, bool) { return Value{a.exp_s1 + a.exp_s2 + a.exp_s3_old + a.exp_s3_new + a.exp_poci}; }},
    {"Performing exposure (Exp)", false, [](const Agg& a, bool) { return Value{a.exp_s1 + a.exp_s2}; }},
    {"of which: stage 1 (Exp S1)", false, [](const Agg& a, bool) { return Value{a.exp_s1}; }},
    {"of which: stage 2 (Exp S2)", false, [](const Agg& a, bool) { return Value{a.exp_s2}; }},
    {"Non-performing exposure (Exp S3)", false, [](const Agg& a, bool) { return Value{a.exp_s3_old + a.exp_s3_new}; }},
    {"of which: existing Non-performing exposure (Old Exp S3)", false, [](const Agg& a, bool) { return Value{a.exp_s3_old}; }},
    {"of which: cumulative new non-performing exposure (Cumul New Exp S3)", false, [](const Agg& a, bool) { return Value{a.exp_s3_new}; }},
    {"POCI exposures (Exp POCI)", false, [](const Agg& a, bool) { return Value{a.exp_poci}; }},
    {"Stock of provisions (Prov Stock)", false, [](const Agg& a, bool) { return Value{a.prov_s1 + a.prov_s2 + a.prov_s3 + a.prov_poci}; }},
    {"of which: performing assets (Prov Stock Perf)", false, [](const Agg& a, bool) { return Value{a.prov_s1 + a.prov_s2}; }},
    {"of which: stage 1 (Prov Stock S1)", false, [](const Agg& a, bool) { return Value{a.prov_s1}; }},
    {"of which: stage 2 (Prov Stock S2)", false, [](const Agg& a, bool) { return Value{a.prov_s2}; }},
    {"of which: non-performing assets (Prov Stock S3)", false, [](const Agg& a, bool) { return Value{a.prov_s3}; }},
    {"of which: POCI (Prov Stock POCI)", false, [](const Agg& a, bool) { return Value{a.prov_poci}; }},
    {"Coverage ratio: performing exposure", true, [](const Agg& a, bool) { return ratio(a.prov_s1 + a.prov_s2, a.exp_s1 + a.exp_s2); }},
    {"Coverage ratio: non-performing exposure", true, [](const Agg& a, bool) { return ratio(a.prov_s3, a.exp_s3_old + a.exp_s3_new); }},
};

std::string csv(const std::string& s) {
    if (s.find_first_of(",\"\n") == std::string::npos) return s;
    std::string o = "\"";
    for (char ch : s) { if (ch == '"') o += '"'; o += ch; }
    return o + "\"";
}

}  // namespace

void write_cr_sector(const Dataset& d, const Segmentation& s, const Projection& p, const fs::path& file) {
    constexpr std::size_t kSlots = 7;   // 0 = actual, 1..3 baseline, 4..6 adverse
    const std::size_t nbucket = s.top_countries.size() + 1;   // top countries, then OTHER
    const auto nseg = s.segments.size();
    std::vector<std::size_t> bucket(nseg, nbucket - 1);
    for (std::size_t i = 0; i < nseg; ++i)
        for (std::size_t k = 0; k < s.top_countries.size(); ++k)
            if (s.segments[i].bucket == s.top_countries[k]) bucket[i] = k;

    // Additive cells [slot][bucket][sector]; template rows and geographies are unions of them.
    std::vector<Agg> cells(kSlots * nbucket * kNaceSectors);
    auto cell = [&](std::size_t slot, std::size_t b, NaceSector sec) -> Agg& {
        return cells[(slot * nbucket + b) * kNaceSectors + static_cast<std::size_t>(sec)];
    };

    // Starting point: stocks per exposure (as in cr_scen), parameters from the projection.
    for (std::size_t i = 0; i < d.exposures.size(); ++i) {
        const auto sid = s.segment_of[i];
        if (sid < 0 || !has_sector_breakdown(s.segments[static_cast<std::size_t>(sid)])) continue;
        const auto& e = d.exposures[i];
        auto& a = cell(0, bucket[static_cast<std::size_t>(sid)], d.counterparties[e.counterparty].nace);
        const double g = to_double(e.gca) * s.fx[i], al = to_double(e.allowance) * s.fx[i];
        switch (e.stage) {
            case Stage::S1: a.exp_s1 += g; a.prov_s1 += al; break;
            case Stage::S2: a.exp_s2 += g; a.prov_s2 += al; break;
            case Stage::S3: a.exp_s3_old += g; a.prov_s3 += al; break;
            case Stage::Poci: a.exp_poci += g; a.prov_poci += al; break;
            case Stage::NotApplicable: break;
        }
    }
    for (std::size_t i = 0; i < nseg && i < p.sectors.size(); ++i) {
        for (const auto& sl : p.sectors[i]) {
            cell(0, bucket[i], sl.sector).add_params(sl.accum[0][0]);
            for (std::size_t sc = 0; sc < 2; ++sc) {
                for (std::size_t t = 0; t < 3; ++t) {
                    const auto& r = sl.results[sc][t];
                    Agg a;
                    a.exp_s1 = r.exp_s1; a.exp_s2 = r.exp_s2; a.exp_s3_old = r.exp_s3_old; a.exp_s3_new = r.exp_s3_new; a.exp_poci = r.exp_poci;
                    a.prov_s1 = r.prov_stock_s1; a.prov_s2 = r.prov_stock_s2; a.prov_s3 = r.prov_stock_s3; a.prov_poci = r.prov_stock_poci;
                    a.flow_s2_s1 = r.flow_s2_s1; a.flow_s1_s2 = r.flow_s1_s2; a.flow_s1_s3 = r.flow_s1_s3; a.flow_s2_s3 = r.flow_s2_s3;
                    a.prov_s1_s2 = r.prov_s1_s2; a.prov_s2_s2 = r.prov_s2_s2;
                    a.prov_s1_s3 = r.prov_cum_s1_s3 - (t ? sl.results[sc][t - 1].prov_cum_s1_s3 : 0.0);
                    a.prov_s2_s3 = r.prov_cum_s2_s3 - (t ? sl.results[sc][t - 1].prov_cum_s2_s3 : 0.0);
                    a.cum_s1_s3 = r.prov_cum_s1_s3; a.cum_s2_s3 = r.prov_cum_s2_s3;
                    a.prov_s1_s1 = r.prov_s1_s1; a.prov_s2_s1 = r.prov_s2_s1; a.prov_old_s3 = r.prov_old_s3;
                    a.pa = sl.accum[sc][t + 1];
                    cell(1 + sc * 3 + t, bucket[i], sl.sector).add(a);
                }
            }
        }
    }

    std::ofstream f(file, std::ios::binary);
    if (!f) throw Error("cannot write " + file.string());
    f << "RowNum,Pivot,Geographical breakdown,Scenario,Year,COREP asset class,NACE code,"
         "Exposures by sector of economic activity (as per scope defined in section 2.3.3 EBA Methodology Note)";
    for (const auto& c : kColumns) f << ',' << csv(c.header);
    f << '\n';

    std::vector<std::string> geos{"Total"};
    for (const auto& c : s.top_countries) geos.push_back(c);
    geos.emplace_back("Other");
    const int ref_year = std::stoi(d.manifest.reference_date.substr(0, 4));
    const char* scen_name[kSlots] = {"Actual", "Baseline", "Baseline", "Baseline", "Adverse", "Adverse", "Adverse"};
    const int year_off[kSlots] = {0, 1, 2, 3, 1, 2, 3};
    char buf[64];
    for (std::size_t slot = 0; slot < kSlots; ++slot) {
        for (std::size_t g = 0; g < geos.size(); ++g) {
            for (const auto& row : kRows) {
                Agg a;
                for (std::size_t b = 0; b < nbucket; ++b) {
                    if (g != 0 && b != g - 1) continue;   // geos[g] = bucket g - 1; geos[0] = Total
                    for (std::size_t k = 0; k < kNaceSectors; ++k)
                        if (row.sectors & (1U << k)) a.add(cell(slot, b, static_cast<NaceSector>(k)));
                }
                const std::string label = csv(row.label);
                f << row.num << ',' << row.pivot << ',' << geos[g] << ',' << scen_name[slot] << ','
                  << (ref_year + year_off[slot]) << ",Exposures in scope of CSV_CR_SECTOR," << label << ',' << label;
                for (const auto& c : kColumns) {
                    const Value v = c.get(a, slot == 0);
                    f << ',';
                    if (!v) continue;
                    if (c.percent) std::snprintf(buf, sizeof buf, "%.7f", *v * 100.0);
                    else std::snprintf(buf, sizeof buf, "%.8f", *v / 1e6);
                    f << buf;
                }
                f << '\n';
            }
        }
    }
}

}  // namespace sora
