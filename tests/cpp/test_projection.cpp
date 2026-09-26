#include "doctest.h"

#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <string>

#include "sora/cr_sector.hpp"
#include "sora/duck.hpp"
#include "sora/projection.hpp"
#include "sora/segmentation.hpp"

using namespace sora;

namespace {

std::array<ParamPath, 2> flat(const Params& p) {
    ParamPath path;
    path.fill(p);
    return {path, path};
}

ScenarioConfig cfg() { return {}; }

}  // namespace

// Same hand-computed case as python/tests/test_reference.py (EBA 2027 draft MN Boxes 3-9).
TEST_CASE("Boxes 3-9, year 1, one segment of four exposures") {
    Params p;
    p.pd12m_s1 = 0.02; p.pd12m_s2 = 0.10; p.tr1_2 = 0.05; p.tr2_1 = 0.20;
    p.lgd_s1 = 0.40; p.lgd_s2 = 0.50; p.lgd_s3 = 0.60; p.lrlt_s2 = 0.08;
    std::array<std::array<YearResult, 3>, 2> acc{};
    const auto paths = flat(p);
    project_exposure(Stage::S1, 1000, 3, paths, cfg(), acc);
    project_exposure(Stage::S2, 200, 10, paths, cfg(), acc);
    project_exposure(Stage::S3, 100, 50, paths, cfg(), acc);
    project_exposure(Stage::Poci, 10, 4, paths, cfg(), acc);
    const auto& r = acc[0][0];
    CHECK(r.flow_s1_s2 == doctest::Approx(50));
    CHECK(r.flow_s2_s1 == doctest::Approx(40));
    CHECK(r.flow_s1_s3 == doctest::Approx(20));
    CHECK(r.flow_s2_s3 == doctest::Approx(20));
    CHECK(r.exp_s1 == doctest::Approx(970));
    CHECK(r.exp_s2 == doctest::Approx(190));
    CHECK(r.exp_s3_new == doctest::Approx(40));
    CHECK(r.prov_s1_s1 == doctest::Approx(7.44));
    CHECK(r.prov_s2_s1 == doctest::Approx(0.32));
    CHECK(r.prov_s1_s2 == doctest::Approx(4.0));
    CHECK(r.prov_s2_s2 == doctest::Approx(11.2));
    CHECK(r.prov_cum_s1_s3 == doctest::Approx(8));
    CHECK(r.prov_cum_s2_s3 == doctest::Approx(10));
    CHECK(r.prov_old_s3 == doctest::Approx(60));
    CHECK(r.impairment == doctest::Approx(104.96 - 67));
}

TEST_CASE("Box 9 floor applies per exposure (para 141)") {
    Params p;
    p.lgd_s3 = 0.5;
    std::array<std::array<YearResult, 3>, 2> acc{};
    project_exposure(Stage::S3, 100, 70, flat(p), cfg(), acc);
    project_exposure(Stage::S3, 100, 10, flat(p), cfg(), acc);
    for (const auto& y : acc[0]) CHECK(y.prov_old_s3 == doctest::Approx(120));
}

TEST_CASE("exposure is conserved over the horizon") {
    Params p;
    p.pd12m_s1 = 0.03; p.pd12m_s2 = 0.2; p.tr1_2 = 0.1; p.tr2_1 = 0.3;
    p.lgd_s1 = 0.3; p.lgd_s2 = 0.4; p.lgd_s3 = 0.5; p.lrlt_s2 = 0.1;
    std::array<std::array<YearResult, 3>, 2> acc{};
    project_exposure(Stage::S1, 800, 0, flat(p), cfg(), acc);
    project_exposure(Stage::S2, 150, 0, flat(p), cfg(), acc);
    project_exposure(Stage::S3, 50, 0, flat(p), cfg(), acc);
    for (const auto& y : acc[0]) CHECK(y.exp_s1 + y.exp_s2 + y.exp_s3_old + y.exp_s3_new == doctest::Approx(1000));
}

TEST_CASE("final adverse year blends the t+2 loss term") {
    Params base, adv;
    base.pd12m_s1 = 0.01; base.lgd_s1 = 0.5;
    adv.pd12m_s1 = 0.04; adv.lgd_s1 = 0.5;
    ParamPath pb, pa;
    pb.fill(base);
    pa.fill(adv);
    std::array<std::array<YearResult, 3>, 2> acc{};
    project_exposure(Stage::S1, 1000, 0, {pb, pa}, cfg(), acc);
    const double e1 = 1000 * 0.96 * 0.96;
    CHECK(acc[1][2].prov_s1_s1 == doctest::Approx(e1 * 0.96 * (5.0 / 6 * 0.04 * 0.5 + 1.0 / 6 * 0.01 * 0.5)));
}

namespace {

// Synthetic portfolio: n exposures over 3 countries, 2 sectors and all stages (deterministic LCG).
struct Synthetic {
    Dataset d;
    Segmentation s;
    ScenarioConfig c;
    MacroTable macro;
    std::map<std::string, Satellite> sats;
    Calibration cal;
};

Synthetic synthetic(std::uint32_t n) {
    Synthetic x;
    Dataset& d = x.d;
    d.currencies.intern("EUR");
    d.fx_to_reporting = {1'000'000'000};
    for (const char* c : {"BE", "DE", "FR"}) d.countries.intern(c);
    for (std::uint32_t i = 0; i < 300; ++i) {
        Counterparty cp;
        cp.country = i % 3;
        cp.sector = i % 2 ? EbaSector::Household : EbaSector::NonFinancialCorporation;
        cp.is_sme = i % 5 ? Flag::True : Flag::False;
        d.counterparties.push_back(cp);
    }
    std::uint64_t r = 42;
    auto next = [&r] { r = r * 6364136223846793005ULL + 1442695040888963407ULL; return r >> 33; };
    for (std::uint32_t i = 0; i < n; ++i) {
        Exposure e;
        e.id = d.exposure_ids.intern("E" + std::to_string(i));
        e.counterparty = static_cast<std::uint32_t>(next() % 300);
        e.currency = 0;
        e.stage = static_cast<Stage>(next() % 4);
        e.purpose = next() % 2 ? HouseholdPurpose::HousePurchase : HouseholdPurpose::Consumption;
        e.is_cre = next() % 3 ? Flag::False : Flag::True;
        e.has_gca = true;
        e.gca = static_cast<Cents>(next() % 100'000'000);
        e.allowance = static_cast<Cents>(next() % 1'000'000);
        d.exposures.push_back(e);
    }
    x.s = segment(d, ScopeConfig{});
    x.c.year_map = {{1, 2025}, {2, 2026}, {3, 2027}};
    x.c.history_year = 2024;
    for (const char* k : {"BE", "DE", "FR"})
        for (int y = 2025; y <= 2027; ++y) {
            x.macro.set("real_gdp", k, "baseline", y, 1.2);
            x.macro.set("real_gdp", k, "adverse", y, -2.5 + 0.1 * y - 202.5);
        }
    for (const auto& seg : x.s.segments) x.sats[seg.portfolio] = {-0.1, 0.05, -0.01, 0.5};
    for (std::size_t i = 0; i < x.s.segments.size(); ++i) {
        Params p;
        p.pd12m_s1 = 0.01 + 0.001 * static_cast<double>(i); p.pd12m_s2 = 0.1; p.tr1_2 = 0.05; p.tr2_1 = 0.2;
        p.lgd_s1 = p.lgd_s2 = p.lgd_s3 = 0.35; p.lrlt_s2 = 0.1;
        x.cal.params.push_back(p);
    }
    return x;
}

}  // namespace

TEST_CASE("project() is bit-identical for any number of workers") {
    const Synthetic x = synthetic(20000);
    const auto& [d, s, c, macro, sats, cal] = x;
    REQUIRE(s.segments.size() > 8);

    const Projection one = project(d, s, cal, sats, macro, c, nullptr, 1);
    for (unsigned w : {2U, 3U, 8U, 0U}) {
        const Projection many = project(d, s, cal, sats, macro, c, nullptr, w);
        REQUIRE(many.results.size() == one.results.size());
        for (std::size_t g = 0; g < one.results.size(); ++g) {
            CHECK(std::memcmp(&one.results[g], &many.results[g], sizeof one.results[g]) == 0);
            CHECK(std::memcmp(&one.accum[g], &many.accum[g], sizeof one.accum[g]) == 0);
        }
    }
    // The parallel result is the sequential sum in exposure order.
    std::vector<std::array<std::array<YearResult, 3>, 2>> seq(s.segments.size());
    for (std::size_t i = 0; i < d.exposures.size(); ++i) {
        const auto g = s.segment_of[i];
        if (g < 0) continue;
        const auto& e = d.exposures[i];
        project_exposure(e.stage, to_double(e.gca) * s.fx[i], to_double(e.allowance) * s.fx[i],
                         one.params[static_cast<std::size_t>(g)], c, seq[static_cast<std::size_t>(g)]);
    }
    for (std::size_t g = 0; g < seq.size(); ++g) CHECK(std::memcmp(&seq[g], &one.results[g], sizeof seq[g]) == 0);
}

TEST_CASE("exposure_param_paths are the paths project() uses for exposure-level parameters") {
    const Synthetic x = synthetic(2000);
    const auto& [d, s, c, macro, sats, cal] = x;
    const std::size_t own = 5;   // exposure E5
    REQUIRE(s.segment_of[own] >= 0);
    const auto g = static_cast<std::size_t>(s.segment_of[own]);
    const auto& seg = s.segments[g];
    // Exposure E5: own starting PD and an adverse/2 LGD; its segment: a baseline/1 transition rate.
    std::string cols = "level, key, scenario, year";
    for (auto n : kParamNames) cols += std::string(", ") + n;
    const std::string rows = "('exposure', 'E5', 'actual', 0, 0.3, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL), "
                             "('exposure', 'E5', 'adverse', 2, NULL, NULL, NULL, NULL, NULL, NULL, 0.6, NULL, NULL, NULL), "
                             "('segment', '" + seg.key + "', 'baseline', 1, NULL, NULL, 0.02, NULL, NULL, NULL, NULL, NULL, NULL, NULL)";
    Duck duck;
    ExternalParameters ext;
    ext.load(duck, "(SELECT * FROM (VALUES " + rows + ") t(" + cols + "))", d);
    REQUIRE(ext.has_exposure(own));

    const Projection proj = project(d, s, cal, sats, macro, c, &ext, 2);
    CHECK(proj.exposures_with_own_parameters == 1);
    const auto paths = exposure_param_paths(s, seg, proj.params[g][0][0], sats.at(seg.portfolio), macro, c, ext, own);
    for (std::size_t sc = 0; sc < 2; ++sc) {
        CHECK(paths[sc][0].pd12m_s1 == 0.3);                                            // own starting point
        CHECK(std::memcmp(&paths[sc][4], &paths[sc][3], sizeof(Params)) == 0);        // flat after the horizon
    }
    CHECK(paths[1][2].lgd_s1 == 0.6);                                                  // exposure overlay
    CHECK(paths[0][1].tr1_2 == 0.02);                                                  // segment overlay
    CHECK(paths[1][1].pd12m_s1 > paths[0][1].pd12m_s1);                                // projected: adverse is worse
    // The segment's provisions are exactly the sum with those paths for E5 and the segment's paths otherwise.
    std::array<std::array<YearResult, 3>, 2> seq{};
    for (std::size_t i = 0; i < d.exposures.size(); ++i) {
        if (s.segment_of[i] != static_cast<std::int32_t>(g)) continue;
        const auto& e = d.exposures[i];
        project_exposure(e.stage, to_double(e.gca) * s.fx[i], to_double(e.allowance) * s.fx[i],
                         i == own ? paths : proj.params[g], c, seq);
    }
    CHECK(std::memcmp(&seq, &proj.results[g], sizeof seq) == 0);
}

TEST_CASE("NACE sector breakdown of the NFC segments adds up to the segments, for any number of workers") {
    Synthetic x = synthetic(20000);
    const char* codes[] = {"C24.10", "C13.20", "F41.20", "L68.20", "", "J62.01", "A01.11"};
    for (std::size_t i = 0; i < x.d.counterparties.size(); ++i)
        if (x.d.counterparties[i].sector == EbaSector::NonFinancialCorporation) x.d.counterparties[i].nace = nace_sector(codes[i % 7]);
    const auto& [d, s, c, macro, sats, cal] = x;
    const Projection one = project(d, s, cal, sats, macro, c, nullptr, 1);
    std::size_t nfc = 0;
    for (std::size_t g = 0; g < s.segments.size(); ++g) {
        const auto& slices = one.sectors[g];
        if (!has_sector_breakdown(s.segments[g])) {
            CHECK(slices.empty());
            continue;
        }
        ++nfc;
        REQUIRE(slices.size() >= 2);
        for (std::size_t k = 1; k < slices.size(); ++k) CHECK(slices[k - 1].sector < slices[k].sector);
        for (std::size_t sc = 0; sc < 2; ++sc)
            for (std::size_t t = 0; t < 3; ++t) {
                std::array<double, 21> sum{};
                for (const auto& sl : slices) {
                    const auto f = fields(sl.results[sc][t]);
                    for (std::size_t k = 0; k < f.size(); ++k) sum[k] += f[k];
                }
                const auto seg = fields(one.results[g][sc][t]);
                for (std::size_t k = 0; k < seg.size(); ++k) CHECK(sum[k] == doctest::Approx(seg[k]).epsilon(1e-12));
            }
    }
    CHECK(nfc > 2);
    for (unsigned w : {3U, 8U}) {
        const Projection many = project(d, s, cal, sats, macro, c, nullptr, w);
        for (std::size_t g = 0; g < s.segments.size(); ++g) {
            REQUIRE(many.sectors[g].size() == one.sectors[g].size());
            for (std::size_t k = 0; k < one.sectors[g].size(); ++k)
                CHECK(std::memcmp(&one.sectors[g][k], &many.sectors[g][k], sizeof(SectorSlice)) == 0);
        }
    }
}

TEST_CASE("cr_sector.csv: TOTAL rows reconcile with the NFC segments, C with its two parts") {
    Synthetic x = synthetic(5000);
    x.d.manifest.reference_date = "2026-06-30";
    for (std::size_t i = 0; i < x.d.counterparties.size(); ++i)
        x.d.counterparties[i].nace = nace_sector(i % 3 == 0 ? "C24.10" : i % 3 == 1 ? "C14.10" : "F41.20");
    const auto& [d, s, c, macro, sats, cal] = x;
    const Projection p = project(d, s, cal, sats, macro, c, nullptr, 2);
    const auto file = std::filesystem::temp_directory_path() / "sora_test_cr_sector.csv";
    write_cr_sector(d, s, p, file);
    auto split = [](const std::string& l) {   // CSV fields; labels with commas are quoted
        std::vector<std::string> out(1);
        bool q = false;
        for (char ch : l) {
            if (ch == '"') q = !q;
            else if (ch == ',' && !q) out.emplace_back();
            else out.back() += ch;
        }
        return out;
    };
    std::map<std::string, double> total_exp;   // geography Total: "scenario/year/row" -> total exposure
    std::size_t rows = 0;
    {
        std::ifstream in(file);
        std::string line;
        std::getline(in, line);
        const auto header = split(line);
        REQUIRE(header.size() == 8 + 46);
        std::size_t exp_col = 0;
        for (std::size_t k = 0; k < header.size(); ++k)
            if (header[k] == "Total exposure (total Exp)") exp_col = k;
        REQUIRE(exp_col > 0);
        while (std::getline(in, line)) {
            const auto f = split(line);
            REQUIRE(f.size() == header.size());
            ++rows;
            if (f[2] == "Total") total_exp[f[3] + "/" + f[4] + "/" + f[0]] = std::stod(f[exp_col]);
        }
    }
    std::filesystem::remove(file);
    CHECK(rows == 7 * (s.top_countries.size() + 2) * 23);
    // Adverse 2029: TOTAL = the NFC segments; C = energy-intensive + other.
    double nfc = 0;
    for (std::size_t g = 0; g < s.segments.size(); ++g)
        if (has_sector_breakdown(s.segments[g])) {
            const auto& r = p.results[g][1][2];
            nfc += r.exp_s1 + r.exp_s2 + r.exp_s3_old + r.exp_s3_new + r.exp_poci;
        }
    CHECK(nfc > 0);
    CHECK(total_exp["Adverse/2029/23"] == doctest::Approx(nfc / 1e6).epsilon(1e-9));
    CHECK(total_exp["Adverse/2029/3"] == doctest::Approx(total_exp["Adverse/2029/4"] + total_exp["Adverse/2029/5"]).epsilon(1e-9));
    CHECK(total_exp["Adverse/2029/4"] > 0);
    CHECK(total_exp["Adverse/2029/5"] > 0);
    CHECK(total_exp["Adverse/2029/8"] > 0);
    CHECK(total_exp["Adverse/2029/1"] == 0);
}
