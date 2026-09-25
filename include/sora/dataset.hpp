#pragma once
// In-memory SIM dataset: dimensions, counterparties and credit exposures in compact records.
// Party and exposure data are small relative to cash flows and history, so they are held in
// flat arrays. Large tables (stage history, cash flows) are streamed by the modules that need them.

#include <cstdint>
#include <filesystem>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "sora/duck.hpp"
#include "sora/types.hpp"

namespace sora {

inline constexpr std::uint32_t kNone = UINT32_MAX;

// String interning: dense integer ids for repeated strings and business keys.
// Compact for millions of keys (exposure and counterparty ids at 100x scale): the strings live in one
// contiguous arena, and the index is an open-addressing table of 8-byte slots (32-bit hash tag + id),
// 30-40 bytes per key instead of about 100 for std::string + std::unordered_map nodes.
class Dictionary {
public:
    std::uint32_t intern(std::string_view s);
    std::optional<std::uint32_t> find(std::string_view s) const;
    std::string at(std::uint32_t id) const { return std::string(view(id)); }
    std::string_view view(std::uint32_t id) const;
    std::size_t size() const noexcept { return size_; }
    void reserve(std::size_t n, std::size_t bytes = 0);   // n keys with `bytes` characters in total
    std::size_t memory_bytes() const noexcept;

private:
    static std::uint32_t hash(std::string_view s) noexcept;
    std::uint64_t* probe(std::string_view s, std::uint32_t h) const noexcept;
    void rehash(std::size_t capacity);

    std::string arena_;                   // all strings, back to back
    std::vector<std::uint32_t> offsets_;  // start of string i; size_ + 1 entries
    std::vector<std::uint64_t> slots_;    // 0 = empty, else (hash << 32) | (id + 1); power-of-two size
    std::size_t size_ = 0;
};

struct Manifest {
    std::string sim_version;
    std::string reference_date;  // YYYY-MM-DD
    Date reference_day = 0;
    std::string reporting_currency;
    std::string reporting_entity_id;
    std::string mapping_release;
};

struct Counterparty {
    std::uint32_t country = kNone;
    EbaSector sector = EbaSector::NonFinancialCorporation;
    Flag is_sme = Flag::Unknown;
};

struct Exposure {
    std::uint32_t id = kNone;             // exposure_ids dictionary
    std::uint32_t counterparty = kNone;   // index into Dataset::counterparties
    std::uint32_t entity = kNone;
    std::uint32_t currency = kNone;
    std::uint32_t country_of_risk = kNone;
    ExposureType type = ExposureType::Loan;
    Measurement measurement = Measurement::AmortisedCost;
    Stage stage = Stage::NotApplicable;
    HouseholdPurpose purpose = HouseholdPurpose::None;
    Flag is_cre = Flag::Unknown;
    bool intragroup = false;
    bool has_gca = false;
    bool has_maturity = false;
    Date maturity = 0;         // legal final maturity (days since epoch)
    Cents gca = 0;             // gross carrying amount (on-balance part)
    Cents off_balance = 0;     // undrawn / nominal off-balance amount
    Cents allowance = 0;       // loss allowance / provision (0 if NULL)
};

// Collateral (sim_collateral) and its allocation to exposures (sim_collateral_allocation). Loaded by
// load_collateral (collateral.hpp); empty if the SIM has no collateral tables.
enum class CollateralType : std::uint8_t { ResidentialProperty, CommercialProperty, Other };

struct Collateral {
    CollateralType type = CollateralType::Other;
    std::uint32_t currency = kNone;
    std::uint32_t property_country = kNone;   // countries dictionary
    Cents market_value = 0;
};

struct CollateralAllocation {
    std::uint32_t exposure = kNone;     // index into Dataset::exposures
    std::uint32_t collateral = kNone;   // index into Dataset::collateral
    bool has_amount = false;            // false: allocated pro rata (see collateral.hpp)
    Cents amount = 0;                   // in the collateral's currency
};

struct Dataset {
    std::filesystem::path sim_dir;
    Manifest manifest;
    Dictionary exposure_ids, counterparty_ids, entities, currencies, countries;
    std::vector<Counterparty> counterparties;     // indexed by counterparty_ids
    std::vector<Exposure> exposures;               // ordered by exposure_id
    std::vector<Nano> fx_to_reporting;             // by currency id; 0 = no rate at the reference date
    Dictionary collateral_ids;
    std::vector<Collateral> collateral;                        // indexed by collateral_ids
    std::vector<CollateralAllocation> collateral_allocations;  // ordered by exposure, collateral

    double fx(std::uint32_t currency) const {
        return currency < fx_to_reporting.size() ? nano_to_double(fx_to_reporting[currency]) : 0.0;
    }
};

// FROM-clause source for a SIM table: read_parquet(...) or read_csv(...), depending on the files present.
std::string sim_source(const std::filesystem::path& sim_dir, std::string_view table);
bool sim_table_exists(const std::filesystem::path& sim_dir, std::string_view table);

// Loads manifest, FX, counterparties, exposures and collateral.
Dataset load_dataset(Duck& duck, const std::filesystem::path& sim_dir);

}  // namespace sora
