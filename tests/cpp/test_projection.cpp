#include "doctest.h"

#include "sora/projection.hpp"

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
