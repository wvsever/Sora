#include "doctest.h"

#include "sora/calibration.hpp"
#include "sora/dataset.hpp"
#include "sora/duck.hpp"

using namespace sora;

TEST_CASE("decimal columns are read exactly as scaled integers") {
    Duck duck;
    std::vector<Cents> cents;
    std::vector<Nano> nanos;
    duck.query("SELECT CAST(v AS DECIMAL(18,2)), CAST(r AS DECIMAL(18,9)) FROM (VALUES "
               "('12345678901234.56', '0.038000000'), ('-0.01', '-1.000000001'), ('0.10', '0.000000001')) t(v, r)",
               [&](const Chunk& c) {
                   for (std::size_t i = 0; i < c.size(); ++i) {
                       cents.push_back(c.cents(0, i));
                       nanos.push_back(c.nano(1, i));
                   }
               });
    CHECK(cents == std::vector<Cents>{1234567890123456, -1, 10});
    CHECK(nanos == std::vector<Nano>{38000000, -1000000001, 1});
    CHECK(to_double(10) == 0.1);
}

TEST_CASE("typed accessors reject a wrong column type") {
    Duck duck;
    duck.query("SELECT 1.5::DOUBLE", [&](const Chunk& c) { CHECK_THROWS_AS(c.cents(0, 0), Error); });
    duck.query("SELECT CAST(1.5 AS DECIMAL(18,9))", [&](const Chunk& c) { CHECK_THROWS_AS(c.cents(0, 0), Error); });
}

TEST_CASE("strings, nulls and booleans") {
    Duck duck;
    duck.query("SELECT * FROM (VALUES ('short', true), ('a much longer string than twelve', NULL)) t(s, b)",
               [&](const Chunk& c) {
                   CHECK(c.str(0, 0) == "short");
                   CHECK(c.str(0, 1) == "a much longer string than twelve");
                   CHECK(c.flag(1, 0) == Flag::True);
                   CHECK(c.flag(1, 1) == Flag::Unknown);
               });
}

TEST_CASE("streaming covers results larger than one chunk") {
    Duck duck;
    std::int64_t n = 0, sum = 0;
    duck.query("SELECT range::BIGINT FROM range(100000)", [&](const Chunk& c) {
        for (std::size_t i = 0; i < c.size(); ++i) { ++n; sum += c.i64(0, i); }
    });
    CHECK(n == 100000);
    CHECK(sum == 100000LL * 99999 / 2);
}

TEST_CASE("dictionary interning") {
    Dictionary d;
    CHECK(d.intern("BE") == 0);
    CHECK(d.intern("DE") == 1);
    CHECK(d.intern("BE") == 0);
    CHECK(d.at(1) == "DE");
    CHECK(!d.find("FR"));
}

TEST_CASE("code lists") {
    CHECK(parse_stage("stage2") == Stage::S2);
    CHECK(parse_exposure_type("debt_security") == ExposureType::DebtSecurity);
    CHECK(parse_household_purpose("") == HouseholdPurpose::None);
    CHECK_THROWS_AS(parse_stage("Stage2"), Error);
}

TEST_CASE("12-month transition matrix") {
    Matrix3 m{{{0.99, 0.01, 0.0}, {0.1, 0.85, 0.05}, {0.0, 0.0, 1.0}}};
    const auto a = matpow(m, 12);
    for (const auto& row : a) CHECK(row[0] + row[1] + row[2] == doctest::Approx(1.0).epsilon(1e-12));
    CHECK(a[0][2] > 0.015);
    CHECK(a[0][2] < 0.025);
}
