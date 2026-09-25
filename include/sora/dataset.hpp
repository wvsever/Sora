#pragma once
// In-memory SIM dataset: dimensions, counterparties and credit exposures in compact records.
// Party and exposure data are small relative to cash flows and history, so they are held in
// flat arrays. Large tables (stage history, cash flows) are streamed by the modules that need them.

#include <cstdint>
#include <deque>
#include <filesystem>
#include <optional>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

#include "sora/duck.hpp"
#include "sora/types.hpp"

namespace sora {

inline constexpr std::uint32_t kNone = UINT32_MAX;

// String interning: dense integer ids for repeated strings and business keys.
class Dictionary {
public:
    std::uint32_t intern(std::string_view s);
    std::optional<std::uint32_t> find(std::string_view s) const;
    const std::string& at(std::uint32_t id) const { return names_.at(id); }
    std::size_t size() const noexcept { return names_.size(); }

private:
    std::deque<std::string> names_;  // stable addresses for the string_view keys
    std::unordered_map<std::string_view, std::uint32_t> index_;
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

struct Dataset {
    std::filesystem::path sim_dir;
    Manifest manifest;
    Dictionary exposure_ids, counterparty_ids, entities, currencies, countries;
    std::vector<Counterparty> counterparties;     // indexed by counterparty_ids
    std::vector<Exposure> exposures;               // ordered by exposure_id
    std::vector<Nano> fx_to_reporting;             // by currency id; 0 = no rate at the reference date

    double fx(std::uint32_t currency) const {
        return currency < fx_to_reporting.size() ? nano_to_double(fx_to_reporting[currency]) : 0.0;
    }
};

// FROM-clause source for a SIM table: read_parquet(...) or read_csv(...), depending on the files present.
std::string sim_source(const std::filesystem::path& sim_dir, std::string_view table);
bool sim_table_exists(const std::filesystem::path& sim_dir, std::string_view table);

// Loads manifest, FX, counterparties and exposures.
Dataset load_dataset(Duck& duck, const std::filesystem::path& sim_dir);

}  // namespace sora
