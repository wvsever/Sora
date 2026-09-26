#include "sora/segmentation.hpp"

#include <algorithm>
#include <array>

namespace sora {

namespace {

// Portfolio as a small code, so that per-exposure work needs no strings (names in kPortfolios).
constexpr std::array<const char*, 12> kPortfolios = {"CB", "GG", "CI", "OFC", "NFC", "NFC_SME_CRE", "NFC_SME_OTHER",
                                                     "NFC_LARGE_CRE", "NFC_LARGE_OTHER", "HH_HOUSE", "HH_CONS", "HH_OTHER"};

std::size_t portfolio_code(const Exposure& e, const Counterparty& cp) {
    const bool debt = e.type == ExposureType::DebtSecurity;
    switch (cp.sector) {
        case EbaSector::CentralBank: return 0;
        case EbaSector::GeneralGovernment: return 1;
        case EbaSector::CreditInstitution: return 2;
        case EbaSector::OtherFinancial: return 3;
        case EbaSector::NonFinancialCorporation:
            if (debt) return 4;
            return (cp.is_sme == Flag::True ? 5 : 7) + (e.is_cre == Flag::True ? 0 : 1);
        case EbaSector::Household:
            if (debt) return 11;
            if (e.purpose == HouseholdPurpose::HousePurchase) return 9;
            if (e.purpose == HouseholdPurpose::Consumption) return 10;
            return 11;
    }
    return 11;
}

}  // namespace

std::string portfolio_of(const Exposure& e, const Counterparty& cp) { return kPortfolios[portfolio_code(e, cp)]; }

bool undrawn_is_off_balance(const Exposure& e, const ScopeConfig& scope) {
    if (e.type == ExposureType::Loan) return scope.loan_undrawn_off_balance;
    return std::find(scope.drawn_types.begin(), scope.drawn_types.end(), e.type) != scope.drawn_types.end();
}

Segmentation segment(const Dataset& d, const ScopeConfig& scope) {
    Segmentation s;
    const auto n = d.exposures.size();
    s.segment_of.assign(n, -1);
    s.fx.assign(n, 0.0);
    s.allowance.assign(n, 0.0);

    // Pass 1: scope, FX, exposure per country (summed in exposure order).
    std::vector<std::uint32_t> country(n, kNone);
    std::vector<double> by_country(d.countries.size(), 0.0);   // exposure in reporting currency
    std::vector<char> has_country(d.countries.size(), 0);
    for (std::size_t i = 0; i < n; ++i) {
        const auto& e = d.exposures[i];
        if (std::find(scope.measurements.begin(), scope.measurements.end(), e.measurement) == scope.measurements.end()) continue;
        // Drawn part of a commitment (drawn_types): an on-balance loans-and-advances exposure if drawn and staged.
        const bool drawn = std::find(scope.drawn_types.begin(), scope.drawn_types.end(), e.type) != scope.drawn_types.end() &&
                           e.has_gca && e.gca > 0 && e.stage != Stage::NotApplicable;
        if (!drawn && std::find(scope.types.begin(), scope.types.end(), e.type) == scope.types.end()) continue;
        if (scope.exclude_intragroup && e.intragroup) continue;
        const double fx = d.fx(e.currency);
        if (fx == 0.0) throw Error("no FX rate at the reference date for " + d.currencies.at(e.currency));
        s.fx[i] = fx;
        s.drawn_commitments += drawn ? 1 : 0;
        // A facility's allowance covers its drawn and undrawn parts: the drawn share is on-balance when the
        // undrawn part is projected off-balance (the undrawn share goes with the off-balance item).
        s.allowance[i] = to_double(e.allowance) * fx;
        if (e.off_balance > 0 && undrawn_is_off_balance(e, scope)) {
            const double g = to_double(e.gca), u = to_double(e.off_balance);
            s.allowance[i] = s.allowance[i] * (g / (g + u));
        }
        country[i] = e.country_of_risk != kNone ? e.country_of_risk : d.counterparties[e.counterparty].country;
        by_country[country[i]] += to_double(e.gca) * fx;
        has_country[country[i]] = 1;
        ++s.in_scope;
    }

    // Top N countries by exposure (ties broken by country code); the rest is OTHER.
    std::vector<std::pair<std::string, double>> ranked;
    for (std::uint32_t c = 0; c < by_country.size(); ++c)
        if (has_country[c]) ranked.emplace_back(d.countries.at(c), by_country[c]);
    std::sort(ranked.begin(), ranked.end(), [](const auto& a, const auto& b) {
        return a.second != b.second ? a.second > b.second : a.first < b.first;
    });
    if (ranked.size() > scope.top_countries) ranked.resize(scope.top_countries);
    for (const auto& [k, _] : ranked) s.top_countries.push_back(k);
    // Bucket per country id: position in s.top_countries, or top_countries.size() for OTHER.
    const std::size_t nbucket = s.top_countries.size() + 1;
    std::vector<std::uint32_t> bucket(d.countries.size(), static_cast<std::uint32_t>(nbucket - 1));
    for (std::uint32_t c = 0; c < d.countries.size(); ++c)
        for (std::size_t k = 0; k < s.top_countries.size(); ++k)
            if (s.top_countries[k] == d.countries.view(c)) bucket[c] = static_cast<std::uint32_t>(k);

    // Pass 2: segment cell per exposure as an integer (instrument, portfolio, bucket), then segments sorted by key.
    std::vector<std::int32_t> cell_segment(2 * kPortfolios.size() * nbucket, -1);
    for (std::size_t i = 0; i < n; ++i) {
        if (country[i] == kNone) continue;
        const auto& e = d.exposures[i];
        const std::size_t cell = ((e.type == ExposureType::DebtSecurity ? 1 : 0) * kPortfolios.size() +
                                  portfolio_code(e, d.counterparties[e.counterparty])) * nbucket + bucket[country[i]];
        s.segment_of[i] = static_cast<std::int32_t>(cell);
        cell_segment[cell] = 0;
    }
    std::vector<std::pair<std::string, std::size_t>> keys;   // (key, cell)
    for (std::size_t cell = 0; cell < cell_segment.size(); ++cell) {
        if (cell_segment[cell] < 0) continue;
        const std::size_t b = cell % nbucket, p = cell / nbucket % kPortfolios.size(), ins = cell / nbucket / kPortfolios.size();
        keys.emplace_back(std::string(ins ? "DEBT_SEC" : "LOANS") + "|" + kPortfolios[p] + "|" +
                              (b + 1 == nbucket ? std::string("OTHER") : s.top_countries[b]), cell);
    }
    std::sort(keys.begin(), keys.end());
    for (const auto& [key, cell] : keys) {
        Segment seg;
        seg.key = key;
        const auto p1 = key.find('|'), p2 = key.find('|', p1 + 1);
        seg.instrument = key.substr(0, p1);
        seg.portfolio = key.substr(p1 + 1, p2 - p1 - 1);
        seg.bucket = key.substr(p2 + 1);
        seg.levels = {s.level_keys.intern(key), s.level_keys.intern(seg.instrument + "|" + seg.portfolio + "|ALL"),
                      s.level_keys.intern(seg.instrument + "|ALL|ALL"), s.level_keys.intern("ALL|ALL|ALL")};
        cell_segment[cell] = static_cast<std::int32_t>(s.segments.size());
        s.segments.push_back(std::move(seg));
    }
    for (auto& sid : s.segment_of)
        if (sid >= 0) sid = cell_segment[static_cast<std::size_t>(sid)];
    return s;
}

}  // namespace sora
