#pragma once
// NACE sectors of the EBA CSV_CR_SECTOR template (2027 draft): NACE Rev. 2.1 sections A-T, with manufacturing
// (C) split into energy-intensive activities (divisions C10-C12, C17-C30, template guidance Table 4) and other.

#include <cstdint>
#include <string_view>

namespace sora {

enum class NaceSector : std::uint8_t {
    A, B, CEnergyIntensive, COther, D, E, F, G, H, I, J, K, L, M, N, O, P, Q, R, S, T, Unknown
};
inline constexpr std::size_t kNaceSectors = 22;   // including Unknown

// Sector of a NACE code of Rev. 2 or Rev. 2.1 ("C24.10", "C24", "24.10", "C"). The section is taken from the
// division number, which has the same meaning in both revisions (only the section letters moved, e.g.
// real estate is L68 in Rev. 2 and M68 in Rev. 2.1). A bare letter is read as a Rev. 2.1 section; a bare "C"
// is COther (the energy-intensive split needs the division). Divisions 97-99 (households as employers,
// extraterritorial bodies), unused numbers, empty and malformed codes are Unknown.
NaceSector nace_sector(std::string_view code) noexcept;

}  // namespace sora
