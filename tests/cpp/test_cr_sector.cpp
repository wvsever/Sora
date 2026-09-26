#include "doctest.h"

#include "sora/nace.hpp"

using namespace sora;

TEST_CASE("NACE codes of Rev. 2 and Rev. 2.1 map to the CR_SECTOR sections by division") {
    // Manufacturing: energy-intensive divisions C10-C12 and C17-C30 (2027 draft guidance, Table 4).
    CHECK(nace_sector("C24.10") == NaceSector::CEnergyIntensive);
    CHECK(nace_sector("C10.11") == NaceSector::CEnergyIntensive);
    CHECK(nace_sector("C30.30") == NaceSector::CEnergyIntensive);
    CHECK(nace_sector("C13.10") == NaceSector::COther);
    CHECK(nace_sector("C16") == NaceSector::COther);
    CHECK(nace_sector("C31.01") == NaceSector::COther);
    CHECK(nace_sector("C") == NaceSector::COther);            // no division: not classifiable as energy-intensive
    // Rev. 2 letters are re-sectioned to Rev. 2.1 by division.
    CHECK(nace_sector("L68.20") == NaceSector::M);             // real estate: Rev. 2 L, Rev. 2.1 M
    CHECK(nace_sector("M68.20") == NaceSector::M);
    CHECK(nace_sector("J62.01") == NaceSector::K);             // computer programming: Rev. 2 J, Rev. 2.1 K
    CHECK(nace_sector("J58.11") == NaceSector::J);
    CHECK(nace_sector("K64.20") == NaceSector::L);             // holding companies (Rev. 2 K)
    CHECK(nace_sector("M69.10") == NaceSector::N);
    CHECK(nace_sector("N77.11") == NaceSector::O);
    CHECK(nace_sector("Q86.10") == NaceSector::R);
    CHECK(nace_sector("R93.11") == NaceSector::S);
    CHECK(nace_sector("S96.02") == NaceSector::T);
    CHECK(nace_sector("G45.11") == NaceSector::G);             // Rev. 2 division 45 is part of Rev. 2.1 G
    CHECK(nace_sector("A01.11") == NaceSector::A);
    CHECK(nace_sector("D35.11") == NaceSector::D);
    // Lenient forms: no section letter, lower case, blanks.
    CHECK(nace_sector("24.10") == NaceSector::CEnergyIntensive);
    CHECK(nace_sector(" f41.20 ") == NaceSector::F);
    // Bare letters are Rev. 2.1 sections.
    CHECK(nace_sector("L") == NaceSector::L);
    CHECK(nace_sector("T") == NaceSector::T);
    // Not an NFC activity, or not a code.
    CHECK(nace_sector("T97.00") == NaceSector::Unknown);
    CHECK(nace_sector("U99.00") == NaceSector::Unknown);
    CHECK(nace_sector("U") == NaceSector::Unknown);
    CHECK(nace_sector("C4") == NaceSector::Unknown);
    CHECK(nace_sector("") == NaceSector::Unknown);
    CHECK(nace_sector("n/a") == NaceSector::Unknown);
}
