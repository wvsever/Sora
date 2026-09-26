#include "sora/cr_scen.hpp"

#include <cmath>
#include <cstdio>
#include <fstream>
#include <functional>
#include <optional>

namespace sora {

namespace fs = std::filesystem;

namespace {

// Everything one template cell group needs, additive over segments.
struct Agg {
    double exp_s1 = 0, exp_s2 = 0, exp_s3_old = 0, exp_s3_new = 0, exp_poci = 0;
    double prov_s1 = 0, prov_s2 = 0, prov_s3 = 0, prov_poci = 0;
    double flow_s2_s1 = 0, flow_s1_s2 = 0, flow_s1_s3 = 0, flow_s2_s3 = 0;
    double prov_s1_s2 = 0, prov_s2_s2 = 0, prov_s1_s3 = 0, prov_s2_s3 = 0;   // within year
    double cum_s1_s3 = 0, cum_s2_s3 = 0, prov_s1_s1 = 0, prov_s2_s1 = 0, prov_old_s3 = 0;
    ParamAccum pa;
    double mat_w = 0, mat_sum = 0;
    LtvCell ltv;

    void add(const Agg& o) {
        exp_s1 += o.exp_s1; exp_s2 += o.exp_s2; exp_s3_old += o.exp_s3_old; exp_s3_new += o.exp_s3_new; exp_poci += o.exp_poci;
        prov_s1 += o.prov_s1; prov_s2 += o.prov_s2; prov_s3 += o.prov_s3; prov_poci += o.prov_poci;
        flow_s2_s1 += o.flow_s2_s1; flow_s1_s2 += o.flow_s1_s2; flow_s1_s3 += o.flow_s1_s3; flow_s2_s3 += o.flow_s2_s3;
        prov_s1_s2 += o.prov_s1_s2; prov_s2_s2 += o.prov_s2_s2; prov_s1_s3 += o.prov_s1_s3; prov_s2_s3 += o.prov_s2_s3;
        cum_s1_s3 += o.cum_s1_s3; cum_s2_s3 += o.cum_s2_s3; prov_s1_s1 += o.prov_s1_s1; prov_s2_s1 += o.prov_s2_s1;
        prov_old_s3 += o.prov_old_s3;
        for (std::size_t i = 0; i < 3; ++i) pa.weight[i] += o.pa.weight[i];
        for (std::size_t i = 0; i < kParamCount; ++i) pa.sum[i] += o.pa.sum[i];
        mat_w += o.mat_w; mat_sum += o.mat_sum;
        ltv.add(o.ltv);
    }
};

struct Row {
    int num;
    const char* pivot;
    const char* portfolio;
    const char* ac1;
    const char* ac2;
    const char* label;
    // Does a segment (instrument, portfolio) contribute to this row?
    std::function<bool(const std::string&, const std::string&)> match;
};

bool is(const std::string& a, const char* b) { return a == b; }

const std::vector<Row>& rows() {
    static const std::vector<Row> r = {
        {1, "Sum", "Debt Securities", "", "", "Debt securities", [](auto& i, auto&) { return is(i, "DEBT_SEC"); }},
        {2, "Pivot", "Debt Securities", "Central banks", "", "Central banks", [](auto& i, auto& p) { return is(i, "DEBT_SEC") && is(p, "CB"); }},
        {3, "Pivot", "Debt Securities", "General governments", "", "General governments", [](auto& i, auto& p) { return is(i, "DEBT_SEC") && is(p, "GG"); }},
        {4, "Pivot", "Debt Securities", "Credit institutions", "", "Credit institutions", [](auto& i, auto& p) { return is(i, "DEBT_SEC") && is(p, "CI"); }},
        {5, "Pivot", "Debt Securities", "Other financial corporations", "", "Other financial corporations", [](auto& i, auto& p) { return is(i, "DEBT_SEC") && is(p, "OFC"); }},
        {6, "Pivot", "Debt Securities", "Non-financial corporations", "", "Non-financial corporations", [](auto& i, auto& p) { return is(i, "DEBT_SEC") && is(p, "NFC"); }},
        {7, "Memo", "Debt Securities", "Total debt: of which: Securitisations", "", "Total debt: of which: Securitisations", [](auto&, auto&) { return false; }},
        {8, "Sum", "Loans and advances", "", "", "Loans and advances", [](auto& i, auto&) { return is(i, "LOANS"); }},
        {9, "Pivot", "Loans and advances", "Central banks", "", "Central banks", [](auto& i, auto& p) { return is(i, "LOANS") && is(p, "CB"); }},
        {10, "Pivot", "Loans and advances", "General governments", "", "General governments", [](auto& i, auto& p) { return is(i, "LOANS") && is(p, "GG"); }},
        {11, "Pivot", "Loans and advances", "Credit institutions", "", "Credit institutions", [](auto& i, auto& p) { return is(i, "LOANS") && is(p, "CI"); }},
        {12, "Pivot", "Loans and advances", "Other financial corporations", "", "Other financial corporations", [](auto& i, auto& p) { return is(i, "LOANS") && is(p, "OFC"); }},
        {13, "Sum", "Loans and advances", "Non-financial corporations", "", "Non-financial corporations", [](auto& i, auto& p) { return is(i, "LOANS") && p.rfind("NFC", 0) == 0; }},
        {14, "Pivot", "Loans and advances", "Non-financial corporations", "Small and medium-sized enterprises (SME) - Commercial real estate (CRE) loans", "Small and medium-sized enterprises (SME) - Commercial real estate (CRE) loans", [](auto& i, auto& p) { return is(i, "LOANS") && is(p, "NFC_SME_CRE"); }},
        {15, "Pivot", "Loans and advances", "Non-financial corporations", "Small and medium-sized enterprises (SME) - Other loans", "Small and medium-sized enterprises (SME) - Other loans", [](auto& i, auto& p) { return is(i, "LOANS") && is(p, "NFC_SME_OTHER"); }},
        {16, "Pivot", "Loans and advances", "Non-financial corporations", "Non-financial corporations other than SMEs - Commercial real estate (CRE) loans", "Non-financial corporations other than SMEs - Commercial real estate (CRE) loans", [](auto& i, auto& p) { return is(i, "LOANS") && is(p, "NFC_LARGE_CRE"); }},
        {17, "Pivot", "Loans and advances", "Non-financial corporations", "Non-financial corporations other than SMEs - Other loans", "Non-financial corporations other than SMEs - Other loans", [](auto& i, auto& p) { return is(i, "LOANS") && is(p, "NFC_LARGE_OTHER"); }},
        {18, "Sum", "Loans and advances", "Households", "", "Households", [](auto& i, auto& p) { return is(i, "LOANS") && p.rfind("HH", 0) == 0; }},
        {19, "Pivot", "Loans and advances", "Households", "Lending for house purchase", "Lending for house purchase", [](auto& i, auto& p) { return is(i, "LOANS") && is(p, "HH_HOUSE"); }},
        {20, "Pivot", "Loans and advances", "Households", "Credit for consumption", "Credit for consumption", [](auto& i, auto& p) { return is(i, "LOANS") && is(p, "HH_CONS"); }},
        {21, "Pivot", "Loans and advances", "Households", "Other households loans", "Other households loans", [](auto& i, auto& p) { return is(i, "LOANS") && is(p, "HH_OTHER"); }},
        {22, "Sum", "Total", "", "", "Total", [](auto&, auto&) { return true; }},
    };
    return r;
}

using Value = std::optional<double>;
struct Column {
    const char* header;
    std::function<Value(const Agg&, bool actual)> get;
    bool percent;
};

Value amount(double v) { return v; }
Value param(const Agg& a, std::size_t i) {
    const double v = a.pa.average(i);
    return std::isnan(v) ? Value{} : Value{v};
}
Value ratio(double num, double den) { return den > 0 ? Value{num / den} : Value{}; }

const std::vector<Column>& columns() {
    auto flow = [](double Agg::*m) {
        return [m](const Agg& a, bool actual) -> Value { return actual ? Value{} : Value{a.*m}; };
    };
    static const std::vector<Column> c = {
        {"PD/TR - Percentage of exposures for which ECB benchmark parameters were used (%)", [](const Agg&, bool) { return Value{0.0}; }, true},
        {"LGD/LR - Percentage of exposures for which ECB benchmark parameters were used (%)", [](const Agg&, bool) { return Value{0.0}; }, true},
        {"PD PiT (%)", [](const Agg&, bool) { return Value{}; }, true},
        {"PD 12M S1 (TR1-3)", [](const Agg& a, bool) { return param(a, 0); }, true},
        {"TR1-2", [](const Agg& a, bool) { return param(a, 2); }, true},
        {"PD 12M S2 (TR2-3)", [](const Agg& a, bool) { return param(a, 1); }, true},
        {"TR2-1", [](const Agg& a, bool) { return param(a, 3); }, true},
        {"TR3-1", [](const Agg& a, bool actual) { return actual ? param(a, 4) : Value{}; }, true},
        {"TR3-2", [](const Agg& a, bool actual) { return actual ? param(a, 5) : Value{}; }, true},
        {"LGD PiT new (%)", [](const Agg&, bool) { return Value{}; }, true},
        {"LGD S1", [](const Agg& a, bool) { return param(a, 6); }, true},
        {"LGD S2", [](const Agg& a, bool) { return param(a, 7); }, true},
        {"LRLT S2", [](const Agg& a, bool) { return param(a, 9); }, true},
        {"LGD S3", [](const Agg& a, bool) { return param(a, 8); }, true},
        {"Stage 1 flow (S2-S1 flow)", flow(&Agg::flow_s2_s1), false},
        {"Stage 2 flow (S1-S2 flow)", flow(&Agg::flow_s1_s2), false},
        {"Stage 3 flow (SX-S3 flow)", [](const Agg& a, bool actual) { return actual ? Value{} : Value{a.flow_s1_s3 + a.flow_s2_s3}; }, false},
        {"Stage 3 flow from Stage 1 (S1-S3 Flow)", flow(&Agg::flow_s1_s3), false},
        {"Stage 3 flow from Stage 2 (S2-S3 Flow)", flow(&Agg::flow_s2_s3), false},
        {"Provisions stage 1 to stage 2 (Prov S1-S2)", flow(&Agg::prov_s1_s2), false},
        {"Provisions stage 2 to stage 2 (Prov S2-S2)", flow(&Agg::prov_s2_s2), false},
        {"Provisions new stage 3 (Prov SX-S3)", [](const Agg& a, bool actual) { return actual ? Value{} : Value{a.prov_s1_s3 + a.prov_s2_s3}; }, false},
        {"Provisions stage 1 to stage 3 (Prov S1-S3)", flow(&Agg::prov_s1_s3), false},
        {"Provisions stage 2 to stage 3 (Prov S2-S3)", flow(&Agg::prov_s2_s3), false},
        {"Cumulative provisions new stage 3 (Prov Cumul SX-S3)", [](const Agg& a, bool actual) { return actual ? Value{} : Value{a.cum_s1_s3 + a.cum_s2_s3}; }, false},
        {"Cumulative provisions stage 1 to stage 3 (Prov Cumul S1-S3)", flow(&Agg::cum_s1_s3), false},
        {"Cumulative provisions stage 2 to stage 3 (Prov Cumul S2-S3)", flow(&Agg::cum_s2_s3), false},
        {"Provisions stage 1 to stage 1 (Prov S1-S1)", flow(&Agg::prov_s1_s1), false},
        {"Provisions stage 2 to stage 1 (Prov S2-S1)", flow(&Agg::prov_s2_s1), false},
        {"Provisions old stage 3 (Prov old S3-S3)", flow(&Agg::prov_old_s3), false},
        {"Total exposure (total Exp)", [](const Agg& a, bool) { return amount(a.exp_s1 + a.exp_s2 + a.exp_s3_old + a.exp_s3_new + a.exp_poci); }, false},
        {"Performing exposure (Perf Exp)", [](const Agg& a, bool) { return amount(a.exp_s1 + a.exp_s2); }, false},
        {"of which: stage 1 (Exp S1)", [](const Agg& a, bool) { return amount(a.exp_s1); }, false},
        {"of which: stage 2 (Exp S2)", [](const Agg& a, bool) { return amount(a.exp_s2); }, false},
        {"Non-performing exposure (Exp S3)", [](const Agg& a, bool) { return amount(a.exp_s3_old + a.exp_s3_new); }, false},
        {"of which: existing Non-performing exposure (Old Exp S3)", [](const Agg& a, bool) { return amount(a.exp_s3_old); }, false},
        {"of which: cumulative new non-performing exposure (Cumul New Exp S3)", [](const Agg& a, bool) { return amount(a.exp_s3_new); }, false},
        {"POCI exposures (Exp POCI)", [](const Agg& a, bool) { return amount(a.exp_poci); }, false},
        {"Stock of provisions (Prov Stock)", [](const Agg& a, bool) { return amount(a.prov_s1 + a.prov_s2 + a.prov_s3 + a.prov_poci); }, false},
        {"of which: performing assets (Prov Stock Perf)", [](const Agg& a, bool) { return amount(a.prov_s1 + a.prov_s2); }, false},
        {"of which: stage 1 (Prov Stock S1)", [](const Agg& a, bool) { return amount(a.prov_s1); }, false},
        {"of which: overlays stage 1 (Overlays S1)", [](const Agg&, bool) { return Value{}; }, false},
        {"of which: stage 2 (Prov Stock S2)", [](const Agg& a, bool) { return amount(a.prov_s2); }, false},
        {"of which: overlays stage 2 (Overlays S2)", [](const Agg&, bool) { return Value{}; }, false},
        {"of which: non-performing assets (Prov Stock S3)", [](const Agg& a, bool) { return amount(a.prov_s3); }, false},
        {"of which: overlays non-performing assets (Overlays S3)", [](const Agg&, bool) { return Value{}; }, false},
        {"of which: POCI (Prov Stock POCI)", [](const Agg& a, bool) { return amount(a.prov_poci); }, false},
        {"of which: overlays POCI (Overlays POCI)", [](const Agg&, bool) { return Value{}; }, false},
        {"Coverage ratio: performing exposure", [](const Agg& a, bool) { return ratio(a.prov_s1 + a.prov_s2, a.exp_s1 + a.exp_s2); }, true},
        {"Coverage ratio: non-performing exposure", [](const Agg& a, bool) { return ratio(a.prov_s3, a.exp_s3_old + a.exp_s3_new); }, true},
        {"Average Maturity (yrs)", [](const Agg& a, bool) { return a.mat_w > 0 ? Value{a.mat_sum / a.mat_w} : Value{}; }, false},
        {"LTV ratio - Stage 1 (%)", [](const Agg& a, bool) { return ratio(a.ltv.secured_exp[0], a.ltv.re_value[0]); }, true},
        {"LTV ratio - Stage 2 (%)", [](const Agg& a, bool) { return ratio(a.ltv.secured_exp[1], a.ltv.re_value[1]); }, true},
        {"LTV ratio - Stage 3 (%)", [](const Agg& a, bool) { return ratio(a.ltv.secured_exp[2], a.ltv.re_value[2]); }, true},
    };
    return c;
}

std::string csv(const std::string& s) {
    if (s.find_first_of(",\"\n") == std::string::npos) return s;
    std::string o = "\"";
    for (char ch : s) { if (ch == '"') o += '"'; o += ch; }
    return o + "\"";
}

}  // namespace

void write_cr_scen(const Dataset& d, const Segmentation& s, const Projection& p, const fs::path& file,
                   const CollateralResult* collateral) {
    const auto nseg = s.segments.size();
    constexpr std::size_t kSlots = 7;   // 0 = actual, 1..3 baseline, 4..6 adverse
    std::vector<std::array<Agg, kSlots>> seg(nseg);

    // Starting point: stocks, maturity (static over the horizon) and year-0 parameters.
    for (std::size_t i = 0; i < d.exposures.size(); ++i) {
        const auto sid = s.segment_of[i];
        if (sid < 0) continue;
        const auto& e = d.exposures[i];
        auto& a = seg[static_cast<std::size_t>(sid)][0];
        const double g = to_double(e.gca) * s.fx[i], al = s.allowance[i];
        switch (e.stage) {
            case Stage::S1: a.exp_s1 += g; a.prov_s1 += al; break;
            case Stage::S2: a.exp_s2 += g; a.prov_s2 += al; break;
            case Stage::S3: a.exp_s3_old += g; a.prov_s3 += al; break;
            case Stage::Poci: a.exp_poci += g; a.prov_poci += al; break;
            case Stage::NotApplicable: break;
        }
        if (e.has_maturity) {
            const double years = std::max(0.0, static_cast<double>(e.maturity - d.manifest.reference_day) / 365.25);
            a.mat_w += g;
            a.mat_sum += g * years;
        }
    }
    for (std::size_t i = 0; i < nseg; ++i) {
        seg[i][0].pa = p.accum[i][0][0];
        for (std::size_t sc = 0; sc < 2; ++sc) {
            for (std::size_t t = 0; t < 3; ++t) {
                const auto& r = p.results[i][sc][t];
                auto& a = seg[i][1 + sc * 3 + t];
                a.exp_s1 = r.exp_s1; a.exp_s2 = r.exp_s2; a.exp_s3_old = r.exp_s3_old; a.exp_s3_new = r.exp_s3_new; a.exp_poci = r.exp_poci;
                a.prov_s1 = r.prov_stock_s1; a.prov_s2 = r.prov_stock_s2; a.prov_s3 = r.prov_stock_s3; a.prov_poci = r.prov_stock_poci;
                a.flow_s2_s1 = r.flow_s2_s1; a.flow_s1_s2 = r.flow_s1_s2; a.flow_s1_s3 = r.flow_s1_s3; a.flow_s2_s3 = r.flow_s2_s3;
                a.prov_s1_s2 = r.prov_s1_s2; a.prov_s2_s2 = r.prov_s2_s2;
                const double prev13 = t ? p.results[i][sc][t - 1].prov_cum_s1_s3 : 0.0;
                const double prev23 = t ? p.results[i][sc][t - 1].prov_cum_s2_s3 : 0.0;
                a.prov_s1_s3 = r.prov_cum_s1_s3 - prev13;
                a.prov_s2_s3 = r.prov_cum_s2_s3 - prev23;
                a.cum_s1_s3 = r.prov_cum_s1_s3; a.cum_s2_s3 = r.prov_cum_s2_s3;
                a.prov_s1_s1 = r.prov_s1_s1; a.prov_s2_s1 = r.prov_s2_s1; a.prov_old_s3 = r.prov_old_s3;
                a.pa = p.accum[i][sc][t + 1];
                a.mat_w = seg[i][0].mat_w;
                a.mat_sum = seg[i][0].mat_sum;
            }
        }
        // LTV (static balance sheet, collateral values per scenario and year). Same slot layout.
        if (collateral)
            for (std::size_t slot = 0; slot < kSlots; ++slot) seg[i][slot].ltv = collateral->cells[i][slot];
    }

    std::ofstream f(file, std::ios::binary);
    if (!f) throw Error("cannot write " + file.string());
    f << "RowNum,Pivot,Geographical breakdown,Scenario,Year,Portfolio,Asset class 1,Asset class 2,Asset classes";
    for (const auto& c : columns()) f << ',' << csv(c.header);
    f << '\n';

    std::vector<std::string> geos{"Total"};
    for (const auto& c : s.top_countries) geos.push_back(c);
    geos.emplace_back("Other");
    const int ref_year = std::stoi(d.manifest.reference_date.substr(0, 4));
    const char* scen_name[kSlots] = {"Actual", "Baseline", "Baseline", "Baseline", "Adverse", "Adverse", "Adverse"};
    const int year_off[kSlots] = {0, 1, 2, 3, 1, 2, 3};
    char buf[64];
    for (std::size_t slot = 0; slot < kSlots; ++slot) {
        for (const auto& geo : geos) {
            for (const auto& row : rows()) {
                Agg a;
                for (std::size_t i = 0; i < nseg; ++i) {
                    const auto& sg = s.segments[i];
                    const bool geo_ok = geo == "Total" || (geo == "Other" ? sg.bucket == "OTHER" : sg.bucket == geo);
                    if (geo_ok && row.match(sg.instrument, sg.portfolio)) a.add(seg[i][slot]);
                }
                f << row.num << ',' << row.pivot << ',' << geo << ',' << scen_name[slot] << ','
                  << (ref_year + year_off[slot]) << ',' << csv(row.portfolio) << ',' << csv(row.ac1) << ','
                  << csv(row.ac2) << ',' << csv(row.label);
                for (const auto& c : columns()) {
                    const Value v = c.get(a, slot == 0);
                    f << ',';
                    if (!v) continue;
                    if (c.percent) std::snprintf(buf, sizeof buf, "%.7f", *v * 100.0);
                    else if (std::string_view(c.header).find("Maturity") != std::string_view::npos) std::snprintf(buf, sizeof buf, "%.4f", *v);
                    else std::snprintf(buf, sizeof buf, "%.8f", *v / 1e6);
                    f << buf;
                }
                f << '\n';
            }
        }
    }
}

}  // namespace sora
