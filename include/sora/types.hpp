#pragma once
// Core value types of the Sora engine.

#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>

namespace sora {

// Monetary amount in cents (SIM `amount`, DECIMAL(18,2)). Exact, never parsed through double.
using Cents = std::int64_t;
// Rate or probability scaled by 1e9 (SIM `rate` / `probability`, DECIMAL(18,9)).
using Nano = std::int64_t;
// Days since 1970-01-01 (DuckDB DATE).
using Date = std::int32_t;

// Same conversion DuckDB uses for CAST(DECIMAL AS DOUBLE), so results match SQL-based tooling bit for bit.
inline double to_double(Cents c) noexcept { return static_cast<double>(c) / 100.0; }
inline double nano_to_double(Nano n) noexcept { return static_cast<double>(n) / 1e9; }

struct Error : std::runtime_error {
    using std::runtime_error::runtime_error;
};

enum class Stage : std::uint8_t { S1 = 0, S2 = 1, S3 = 2, Poci = 3, NotApplicable = 4 };
enum class ExposureType : std::uint8_t {
    Loan, DebtSecurity, FinanceLease, LoanCommitment, FinancialGuarantee, OtherCommitment
};
enum class Measurement : std::uint8_t {
    AmortisedCost, Fvoci, FvociEquity, FvtplMandatory, FvtplDesignated, HeldForTrading
};
enum class EbaSector : std::uint8_t {
    CentralBank, GeneralGovernment, CreditInstitution, OtherFinancial, NonFinancialCorporation, Household
};
enum class HouseholdPurpose : std::uint8_t { None, HousePurchase, Consumption, Other };

// Parsers for SIM code lists. They throw sora::Error on unknown codes (the input must be validated SIM).
Stage parse_stage(std::string_view s);
ExposureType parse_exposure_type(std::string_view s);
Measurement parse_measurement(std::string_view s);
EbaSector parse_eba_sector(std::string_view s);
HouseholdPurpose parse_household_purpose(std::string_view s);  // empty -> None

std::string_view to_string(Stage s) noexcept;
std::string_view to_string(ExposureType t) noexcept;
std::string_view to_string(Measurement m) noexcept;

// Tri-state flag for nullable SIM booleans.
enum class Flag : std::int8_t { Unknown = -1, False = 0, True = 1 };

}  // namespace sora
