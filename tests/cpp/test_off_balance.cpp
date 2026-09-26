#include "doctest.h"

#include <filesystem>
#include <fstream>

#include "sora/off_balance.hpp"

using namespace sora;
namespace fs = std::filesystem;

namespace {

std::array<ParamPath, 2> flat(const Params& p) {
    ParamPath path;
    path.fill(p);
    return {path, path};
}

Params params() {
    Params p;
    p.pd12m_s1 = 0.02; p.pd12m_s2 = 0.10; p.tr1_2 = 0.05; p.tr2_1 = 0.20;
    p.lgd_s1 = 0.40; p.lgd_s2 = 0.50; p.lgd_s3 = 0.60; p.lrlt_s2 = 0.08;
    return p;
}

fs::path temp_file(const std::string& name, const std::string& text) {
    const auto p = fs::temp_directory_path() / name;
    std::ofstream(p, std::ios::binary) << text;
    return p;
}

}  // namespace

TEST_CASE("regulatory fallback CCF (CRR Art. 111(2) buckets)") {
    OffBalanceConfig c;
    CHECK(fallback_ccf(ExposureType::LoanCommitment, false, c) == 0.4);
    CHECK(fallback_ccf(ExposureType::LoanCommitment, true, c) == 0.1);
    CHECK(fallback_ccf(ExposureType::OtherCommitment, false, c) == 0.5);
    CHECK(fallback_ccf(ExposureType::OtherCommitment, true, c) == 0.1);
    CHECK(fallback_ccf(ExposureType::FinancialGuarantee, true, c) == 1.0);   // a guarantee is not a cancellable commitment
    CHECK_THROWS_AS(fallback_ccf(ExposureType::Loan, false, c), Error);
    c.ccf_loan_commitment = 0.75;
    CHECK(fallback_ccf(ExposureType::LoanCommitment, false, c) == 0.75);
}

// Same parameters as the on-balance hand-computed case: flows on the post-CCF amount, nominal through the same flows.
TEST_CASE("off-balance item: post-CCF flows and provisions, nominal follows the same stage flows") {
    OffBalanceGroup g;
    project_off_balance_item(Stage::S1, 1000, 0.4, 3, flat(params()), {}, g);
    project_off_balance_item(Stage::S2, 200, 0.5, 2, flat(params()), {}, g);
    CHECK(g.items == 2);
    CHECK(g.nominal0[0] == 1000);
    CHECK(g.post0[0] == doctest::Approx(400));
    CHECK(g.post0[1] == doctest::Approx(100));
    CHECK(g.provision0[0] == 3);
    const auto& post = g.post[0][0];
    const auto& nom = g.nominal[0][0];
    // Post-CCF: S1 400, S2 100. Flows 20 (S1->S2), 20 (S2->S1), 8 (S1->S3), 10 (S2->S3).
    CHECK(post.flow_s1_s2 == doctest::Approx(20));
    CHECK(post.flow_s2_s1 == doctest::Approx(20));
    CHECK(post.exp_s1 == doctest::Approx(400 - 20 - 8 + 20));
    CHECK(post.exp_s3_new == doctest::Approx(18));
    // Box 5: 400 * 0.93 * 0.02 * 0.4; Box 4: 20 * 0.008; Box 6: 20 * 0.08; Box 7: 100 * 0.7 * 0.08
    CHECK(post.prov_s1_s1 == doctest::Approx(2.976));
    CHECK(post.prov_s2_s1 == doctest::Approx(0.16));
    CHECK(post.prov_s1_s2 == doctest::Approx(1.6));
    CHECK(post.prov_s2_s2 == doctest::Approx(5.6));
    CHECK(post.prov_cum_s1_s3 == doctest::Approx(3.2));
    CHECK(post.prov_cum_s2_s3 == doctest::Approx(5.0));
    CHECK(post.impairment == doctest::Approx(2.976 + 0.16 + 1.6 + 5.6 + 3.2 + 5.0 - 5));
    // Nominal: S1 1000, S2 200 through the same rates.
    CHECK(nom.exp_s1 == doctest::Approx(1000 - 50 - 20 + 40));
    CHECK(nom.exp_s2 == doctest::Approx(200 - 40 - 20 + 50));
    CHECK(nom.exp_s3_new == doctest::Approx(40));
    for (const auto& y : g.nominal[1]) CHECK(y.exp_s1 + y.exp_s2 + y.exp_s3_new == doctest::Approx(1200));   // static
}

TEST_CASE("off-balance stage 3 floor per item and static POCI") {
    Params p = params();
    OffBalanceGroup g;
    project_off_balance_item(Stage::S3, 100, 0.5, 40, flat(p), {}, g);   // max(50 * 0.6, 40) = 40
    project_off_balance_item(Stage::S3, 100, 1.0, 10, flat(p), {}, g);   // max(100 * 0.6, 10) = 60
    project_off_balance_item(Stage::Poci, 30, 1.0, 7, flat(p), {}, g);
    project_off_balance_item(Stage::NotApplicable, 999, 1.0, 1, flat(p), {}, g);   // no stage: ignored
    CHECK(g.items == 3);
    for (const auto& y : g.post[1]) {
        CHECK(y.prov_old_s3 == doctest::Approx(100));
        CHECK(y.prov_stock_poci == doctest::Approx(7));
        CHECK(y.exp_poci == doctest::Approx(30));
        CHECK(y.exp_s3_old == doctest::Approx(150));
    }
    CHECK(g.nominal[0][2].exp_s3_old == doctest::Approx(200));
}

TEST_CASE("customer CCF: exposure row, then the segment hierarchy") {
    Segmentation s;
    Segment seg;
    seg.levels = {s.level_keys.intern("LOANS|HH_OTHER|BE"), s.level_keys.intern("LOANS|HH_OTHER|ALL"),
                  s.level_keys.intern("LOANS|ALL|ALL"), s.level_keys.intern("ALL|ALL|ALL")};
    CustomerCcf c;
    CHECK(!c.find(5, s, seg));
    c.set_level("ALL|ALL|ALL", 0.9);
    CHECK(*c.find(5, s, seg) == 0.9);
    c.set_level("LOANS|HH_OTHER|ALL", 0.7);
    CHECK(*c.find(5, s, seg) == 0.7);
    c.set_exposure(5, 0.2);
    CHECK(*c.find(5, s, seg) == 0.2);
    CHECK(*c.find(6, s, seg) == 0.7);
}

TEST_CASE("customer CCF loaded from a parameter file") {
    Duck duck;
    Dataset d;
    d.exposure_ids.intern("CM-1");
    d.exposure_ids.intern("CM-2");
    const auto with = temp_file("sora_ccf_test.csv",
                                "level,key,scenario,year,pd12m_s1,ccf\n"
                                "exposure,CM-1,actual,0,0.01,0.25\n"
                                "exposure,CM-2,adverse,1,,0.9\n"       // projection rows: CCF is static
                                "exposure,NO-SUCH,actual,0,,0.3\n"
                                "segment,LOANS|ALL|ALL,actual,0,,0.6\n");
    CustomerCcf c;
    c.load(duck, "read_csv('" + with.string() + "', header = true, all_varchar = true)", d);
    Segmentation s;
    Segment seg;
    seg.levels = {s.level_keys.intern("LOANS|CI|BE"), s.level_keys.intern("LOANS|CI|ALL"),
                  s.level_keys.intern("LOANS|ALL|ALL"), s.level_keys.intern("ALL|ALL|ALL")};
    CHECK(*c.find(0, s, seg) == 0.25);
    CHECK(*c.find(1, s, seg) == 0.6);

    const auto without = temp_file("sora_ccf_none.csv", "level,key,scenario,year,pd12m_s1\nexposure,CM-1,actual,0,0.01\n");
    CustomerCcf none;
    none.load(duck, "read_csv('" + without.string() + "', header = true, all_varchar = true)", d);
    CHECK(none.empty());

    const auto bad = temp_file("sora_ccf_bad.csv", "level,key,scenario,year,ccf\nexposure,CM-1,actual,0,1.5\n");
    CustomerCcf invalid;
    CHECK_THROWS_AS(invalid.load(duck, "read_csv('" + bad.string() + "', header = true, all_varchar = true)", d), Error);
}

TEST_CASE("scenario key off_balance") {
    const std::string base = "name: t\nmacro_path: m.csv\nsatellites: s.csv\nyear_map: {1: 2025, 2: 2026, 3: 2027}\n"
                             "history_year: 2024\nnormal_gdp_growth: 1.5\ncountry_fallback: [EU]\n"
                             "scope:\n  measurement_categories: [amortised_cost]\n  exposure_types: [loan]\n"
                             "  exclude_intragroup: true\nsegmentation: {top_countries: 10}\n"
                             "calibration: {min_observations: 100, pd_floor: 0.00001}\n"
                             "constraints: {no_cure_from_s3: true, adverse_final_year_blend: [0.8, 0.2]}\n";
    const auto yaml = temp_file("sora_off_balance_scenario.yaml", base);
    CHECK(!load_scenario(yaml, ".").off_balance.enabled);
    std::ofstream(yaml, std::ios::app) << "off_balance:\n  exposure_types: [loan_commitment, other_commitment]\n"
                                          "  ccf_fallback: {other_commitment: 0.2}\n";
    const auto c = load_scenario(yaml, ".");
    CHECK(c.off_balance.enabled);
    CHECK(c.off_balance.types.size() == 2);
    CHECK(c.off_balance.ccf_other_commitment == 0.2);
    CHECK(c.off_balance.ccf_loan_commitment == 0.4);   // default
    const auto bad = temp_file("sora_off_balance_bad.yaml", base + "off_balance:\n  exposure_types: [loan]\n");
    CHECK_THROWS_AS(load_scenario(bad, "."), Error);
    const auto bad_ccf = temp_file("sora_off_balance_bad_ccf.yaml",
                                   base + "off_balance:\n  exposure_types: [loan_commitment]\n  ccf_fallback: {loan_commitment: 2}\n");
    CHECK_THROWS_AS(load_scenario(bad_ccf, "."), Error);
}

TEST_CASE("facilities: drawn part of commitments on-balance, allowance split between drawn and undrawn parts") {
    Dataset d;
    d.currencies.intern("EUR");
    d.fx_to_reporting = {1'000'000'000};
    d.countries.intern("BE");
    Counterparty cp;
    cp.country = 0;
    cp.sector = EbaSector::Household;
    d.counterparties.push_back(cp);
    auto add = [&](const char* id, ExposureType type, Stage stage, Cents gca, Cents undrawn, Cents allowance) {
        Exposure e;
        e.id = d.exposure_ids.intern(id);
        e.counterparty = 0;
        e.currency = 0;
        e.type = type;
        e.stage = stage;
        e.has_gca = gca > 0;
        e.gca = gca;
        e.off_balance = undrawn;
        e.allowance = allowance;
        d.exposures.push_back(e);
    };
    add("L-1", ExposureType::Loan, Stage::S1, 60'000, 40'000, 1'000);              // loan with an undrawn part
    add("L-2", ExposureType::Loan, Stage::S2, 50'000, 0, 500);                     // fully drawn loan
    add("C-1", ExposureType::LoanCommitment, Stage::S1, 25'000, 75'000, 2'000);    // partly drawn commitment
    add("C-2", ExposureType::LoanCommitment, Stage::S1, 0, 10'000, 100);           // undrawn commitment
    add("G-1", ExposureType::FinancialGuarantee, Stage::S3, 10'000, 30'000, 800);  // called guarantee

    // Default scope: commitments are off-balance only, loans keep their whole allowance.
    const auto off = segment(d, ScopeConfig{});
    CHECK(off.in_scope == 2);
    CHECK(off.segment_of[2] < 0);
    CHECK(off.allowance[0] == 10.0);
    CHECK(off.drawn_commitments == 0);

    ScopeConfig scope;
    scope.drawn_types = {ExposureType::LoanCommitment, ExposureType::FinancialGuarantee};
    scope.loan_undrawn_off_balance = true;
    CHECK(undrawn_is_off_balance(d.exposures[0], scope));
    CHECK(undrawn_is_off_balance(d.exposures[2], scope));
    CHECK(!undrawn_is_off_balance(d.exposures[0], ScopeConfig{}));
    const auto on = segment(d, scope);
    CHECK(on.in_scope == 4);   // L-1, L-2, C-1, G-1; C-2 has no drawn part
    CHECK(on.drawn_commitments == 2);
    CHECK(on.segment_of[3] < 0);
    CHECK(on.segment_of[2] == on.segment_of[0]);   // the loan segment of the counterparty
    CHECK(on.segments[static_cast<std::size_t>(on.segment_of[2])].key == "LOANS|HH_OTHER|BE");
    // Drawn share on-balance: allowance x GCA / (GCA + undrawn); the undrawn share is the off-balance provision.
    CHECK(on.allowance[0] == doctest::Approx(10.0 * 0.6));
    CHECK(on.allowance[1] == 5.0);   // no undrawn part: the whole allowance
    CHECK(on.allowance[2] == doctest::Approx(20.0 * 0.25));
    CHECK(on.allowance[4] == doctest::Approx(8.0 * 0.25));
    // Without include_loan_undrawn, the loan keeps its whole allowance on-balance.
    scope.loan_undrawn_off_balance = false;
    CHECK(segment(d, scope).allowance[0] == 10.0);
}

TEST_CASE("scenario keys off_balance.include_loan_undrawn and commitment_drawn_on_balance") {
    const std::string base = "name: t\nmacro_path: m.csv\nsatellites: s.csv\nyear_map: {1: 2025, 2: 2026, 3: 2027}\n"
                             "history_year: 2024\nnormal_gdp_growth: 1.5\ncountry_fallback: [EU]\n"
                             "scope:\n  measurement_categories: [amortised_cost]\n  exposure_types: [loan]\n"
                             "  exclude_intragroup: true\nsegmentation: {top_countries: 10}\n"
                             "calibration: {min_observations: 100, pd_floor: 0.00001}\n"
                             "constraints: {no_cure_from_s3: true, adverse_final_year_blend: [0.8, 0.2]}\n"
                             "off_balance:\n  exposure_types: [loan_commitment, other_commitment]\n";
    const auto plain = load_scenario(temp_file("sora_facility_plain.yaml", base), ".");
    CHECK(!plain.off_balance.include_loan_undrawn);
    CHECK(!plain.off_balance.commitment_drawn_on_balance);
    CHECK(plain.scope.drawn_types.empty());
    CHECK(!plain.scope.loan_undrawn_off_balance);
    const auto both = load_scenario(temp_file("sora_facility_both.yaml", base + "  include_loan_undrawn: true\n"
                                                                                "  commitment_drawn_on_balance: true\n"), ".");
    CHECK(both.off_balance.include_loan_undrawn);
    CHECK(both.scope.loan_undrawn_off_balance);
    CHECK(both.scope.drawn_types == std::vector<ExposureType>{ExposureType::LoanCommitment, ExposureType::OtherCommitment});
    const auto bad = temp_file("sora_facility_bad.yaml", base + "  include_loan_undrawn: maybe\n");
    CHECK_THROWS_AS(load_scenario(bad, "."), Error);
}
