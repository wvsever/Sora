#pragma once
// EBA CR_SCEN segmentation: instrument | portfolio | country bucket, with a 4-level hierarchy used for
// calibration fallbacks: segment -> instrument|portfolio|ALL -> instrument|ALL|ALL -> ALL|ALL|ALL.

#include <array>
#include <string>
#include <vector>

#include "sora/dataset.hpp"

namespace sora {

struct ScopeConfig {
    std::vector<Measurement> measurements{Measurement::AmortisedCost};
    std::vector<ExposureType> types{ExposureType::Loan, ExposureType::FinanceLease, ExposureType::DebtSecurity};
    bool exclude_intragroup = true;
    std::size_t top_countries = 10;
    // Commitment types whose drawn part (gross carrying amount > 0, staged) is an on-balance loans-and-advances
    // exposure (off_balance.commitment_drawn_on_balance). Their undrawn part is projected off-balance.
    std::vector<ExposureType> drawn_types;
    // The undrawn part of loans is projected off-balance (off_balance.include_loan_undrawn).
    bool loan_undrawn_off_balance = false;
};

// Whether the undrawn part of an in-scope exposure is projected off-balance, so that its allowance is split
// between the drawn (on-balance) and undrawn (off-balance) parts.
bool undrawn_is_off_balance(const Exposure& e, const ScopeConfig& scope);

struct Segment {
    std::string key;         // e.g. LOANS|NFC_SME_CRE|BE
    std::string instrument;  // LOANS | DEBT_SEC
    std::string portfolio;   // CB, GG, CI, OFC, NFC*, HH_*
    std::string bucket;      // country code or OTHER
    std::array<std::uint32_t, 4> levels{};  // ids in Segmentation::level_keys, most specific first
};

struct Segmentation {
    std::vector<Segment> segments;               // sorted by key
    std::vector<std::int32_t> segment_of;        // per exposure; -1 = out of scope
    std::vector<double> fx;                      // per exposure: rate to the reporting currency
    // per exposure: on-balance loss allowance in the reporting currency. The facility's allowance, or its drawn
    // share allowance x GCA / (GCA + undrawn) when the undrawn part is projected off-balance (undrawn_is_off_balance).
    std::vector<double> allowance;
    std::size_t drawn_commitments = 0;           // in-scope commitments (drawn part on-balance, ScopeConfig::drawn_types)
    Dictionary level_keys;                       // all hierarchy keys
    std::vector<std::string> top_countries;      // country buckets, largest exposure first
    std::size_t in_scope = 0;
};

Segmentation segment(const Dataset& d, const ScopeConfig& scope);

// The portfolio name of an exposure (EBA CR_SCEN pivot asset class).
std::string portfolio_of(const Exposure& e, const Counterparty& cp);

}  // namespace sora
