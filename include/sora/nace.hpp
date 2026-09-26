#pragma once
// NACE sectors of the EBA CSV_CR_SECTOR template (2027 draft): NACE Rev. 2.1 sections A-T, with manufacturing
// (C) split into energy-intensive activities (divisions C10-C12, C17-C30, template guidance Table 4) and other.

#include <cstddef>
#include <cstdint>
#include <optional>
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

// Sector codes of Sora's files (sectoral satellites, sector_parameters.csv): the Rev. 2.1 section letter, C_EI and
// C_OT for energy-intensive and other manufacturing, UNKNOWN.
std::string_view sector_code(NaceSector s) noexcept;
std::optional<NaceSector> parse_sector_code(std::string_view code) noexcept;   // never Unknown

// Sector of the ESRB "Real GVA by sector" scenario (NACE Rev. 2 sections and aggregates) that holds a CR_SECTOR
// sector, mapped by division: A, B, C_high (energy-intensive manufacturing), C_low (other manufacturing), D-I,
// J (Rev. 2.1 J and K: divisions 58-63), K (Rev. 2.1 L: 64-66), L (Rev. 2.1 M: 68), MN (Rev. 2.1 N and O: 69-82),
// OPQ (Rev. 2.1 P-R: 84-88), RSTU (Rev. 2.1 S and T: 90-96). Empty for Unknown.
std::string_view gva_sector(NaceSector s) noexcept;

}  // namespace sora
