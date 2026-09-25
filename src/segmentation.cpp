#include "sora/segmentation.hpp"

#include <algorithm>
#include <map>
#include <unordered_map>

namespace sora {

std::string portfolio_of(const Exposure& e, const Counterparty& cp) {
    const bool debt = e.type == ExposureType::DebtSecurity;
    switch (cp.sector) {
        case EbaSector::CentralBank: return "CB";
        case EbaSector::GeneralGovernment: return "GG";
        case EbaSector::CreditInstitution: return "CI";
        case EbaSector::OtherFinancial: return "OFC";
        case EbaSector::NonFinancialCorporation:
            if (debt) return "NFC";
            return std::string("NFC_") + (cp.is_sme == Flag::True ? "SME" : "LARGE") +
                   (e.is_cre == Flag::True ? "_CRE" : "_OTHER");
        case EbaSector::Household:
            if (debt) return "HH_OTHER";
            if (e.purpose == HouseholdPurpose::HousePurchase) return "HH_HOUSE";
            if (e.purpose == HouseholdPurpose::Consumption) return "HH_CONS";
            return "HH_OTHER";
    }
    return "?";
}

Segmentation segment(const Dataset& d, const ScopeConfig& scope) {
    Segmentation s;
    const auto n = d.exposures.size();
    s.segment_of.assign(n, -1);
    s.fx.assign(n, 0.0);

    std::vector<bool> in(n, false);
    std::vector<std::uint32_t> country(n, kNone);
    std::unordered_map<std::uint32_t, double> by_country;   // exposure in reporting currency
    for (std::size_t i = 0; i < n; ++i) {
        const auto& e = d.exposures[i];
        if (std::find(scope.measurements.begin(), scope.measurements.end(), e.measurement) == scope.measurements.end()) continue;
        if (std::find(scope.types.begin(), scope.types.end(), e.type) == scope.types.end()) continue;
        if (scope.exclude_intragroup && e.intragroup) continue;
        const double fx = d.fx(e.currency);
        if (fx == 0.0) throw Error("no FX rate at the reference date for " + d.currencies.at(e.currency));
        in[i] = true;
        s.fx[i] = fx;
        country[i] = e.country_of_risk != kNone ? e.country_of_risk : d.counterparties[e.counterparty].country;
        by_country[country[i]] += to_double(e.gca) * fx;
        ++s.in_scope;
    }

    // Top N countries by exposure (ties broken by country code); the rest is OTHER.
    std::vector<std::pair<std::string, double>> ranked;
    for (const auto& [c, v] : by_country) ranked.emplace_back(d.countries.at(c), v);
    std::sort(ranked.begin(), ranked.end(), [](const auto& a, const auto& b) {
        return a.second != b.second ? a.second > b.second : a.first < b.first;
    });
    if (ranked.size() > scope.top_countries) ranked.resize(scope.top_countries);
    for (const auto& [k, _] : ranked) s.top_countries.push_back(k);
    auto bucket_of = [&](std::uint32_t c) {
        const auto& code = d.countries.at(c);
        for (const auto& [k, _] : ranked) if (k == code) return code;
        return std::string("OTHER");
    };

    std::map<std::string, std::vector<std::size_t>> members;   // sorted by key
    std::vector<std::string> key_of(n);
    for (std::size_t i = 0; i < n; ++i) {
        if (!in[i]) continue;
        const auto& e = d.exposures[i];
        const std::string instrument = e.type == ExposureType::DebtSecurity ? "DEBT_SEC" : "LOANS";
        key_of[i] = instrument + "|" + portfolio_of(e, d.counterparties[e.counterparty]) + "|" + bucket_of(country[i]);
        members[key_of[i]].push_back(i);
    }
    for (const auto& [key, idx] : members) {
        Segment seg;
        seg.key = key;
        const auto p1 = key.find('|'), p2 = key.find('|', p1 + 1);
        seg.instrument = key.substr(0, p1);
        seg.portfolio = key.substr(p1 + 1, p2 - p1 - 1);
        seg.bucket = key.substr(p2 + 1);
        seg.levels = {s.level_keys.intern(key), s.level_keys.intern(seg.instrument + "|" + seg.portfolio + "|ALL"),
                      s.level_keys.intern(seg.instrument + "|ALL|ALL"), s.level_keys.intern("ALL|ALL|ALL")};
        const auto sid = static_cast<std::int32_t>(s.segments.size());
        for (auto i : idx) s.segment_of[i] = sid;
        s.segments.push_back(std::move(seg));
    }
    return s;
}

}  // namespace sora
