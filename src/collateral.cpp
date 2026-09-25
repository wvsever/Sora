#include "sora/collateral.hpp"

#include "sora/projection.hpp"

#include <cstdio>
#include <fstream>
#include <map>

namespace sora {

namespace fs = std::filesystem;

void load_collateral(Duck& duck, Dataset& d) {
    if (!sim_table_exists(d.sim_dir, "sim_collateral")) return;
    duck.query("SELECT CAST(collateral_id AS VARCHAR), CAST(collateral_type AS VARCHAR), CAST(currency AS VARCHAR), "
               "CAST(market_value AS DECIMAL(18,2)), CAST(property_country AS VARCHAR) FROM " +
                   sim_source(d.sim_dir, "sim_collateral") + " ORDER BY 1",
               [&](const Chunk& c) {
                   for (std::size_t r = 0; r < c.size(); ++r) {
                       const auto id = d.collateral_ids.intern(c.str(0, r));
                       if (id != d.collateral.size()) throw Error("duplicate collateral_id " + std::string(c.str(0, r)));
                       Collateral x;
                       const auto type = c.str(1, r);
                       x.type = type == "residential_property" ? CollateralType::ResidentialProperty
                                : type == "commercial_property" ? CollateralType::CommercialProperty
                                                                : CollateralType::Other;
                       x.currency = d.currencies.intern(c.str(2, r));
                       x.market_value = c.valid(3, r) ? c.cents(3, r) : 0;
                       x.property_country = c.valid(4, r) ? d.countries.intern(c.str(4, r)) : kNone;
                       d.collateral.push_back(x);
                   }
               });
    if (!sim_table_exists(d.sim_dir, "sim_collateral_allocation")) return;
    duck.query("SELECT CAST(exposure_id AS VARCHAR), CAST(collateral_id AS VARCHAR), "
               "CAST(allocated_amount AS DECIMAL(18,2)) FROM " +
                   sim_source(d.sim_dir, "sim_collateral_allocation") + " ORDER BY 1, 2",
               [&](const Chunk& c) {
                   for (std::size_t r = 0; r < c.size(); ++r) {
                       const auto e = d.exposure_ids.find(c.str(0, r));
                       const auto k = d.collateral_ids.find(c.str(1, r));
                       if (!e || !k)
                           throw Error("collateral allocation " + std::string(c.str(0, r)) + "/" + std::string(c.str(1, r)) +
                                       ": unknown " + (e ? "collateral" : "exposure"));
                       CollateralAllocation a;
                       a.exposure = *e;
                       a.collateral = *k;
                       a.has_amount = c.valid(2, r);
                       a.amount = a.has_amount ? c.cents(2, r) : 0;
                       d.collateral_allocations.push_back(a);
                   }
               });
}

CollateralIndex collateral_index(CollateralType type, const std::string& country, const MacroTable& macro,
                                 const ScenarioConfig& cfg) {
    CollateralIndex idx;
    for (auto& path : idx) path.fill(1.0);
    if (type == CollateralType::Other) return idx;
    const std::string key = macro_key(macro, country, cfg);
    const std::string var = type == CollateralType::ResidentialProperty ? "residential_property_prices" : "commercial_property_prices";
    for (std::size_t sc = 0; sc < 2; ++sc) {
        for (int t = 1; t <= 3; ++t) {
            const double g = macro.get(var, key, kScenarios[sc], cfg.year_map.at(t)).value_or(0.0);
            idx[sc][static_cast<std::size_t>(t)] = idx[sc][static_cast<std::size_t>(t) - 1] * (1 + g / 100);
        }
    }
    return idx;
}

CollateralResult collateral_ltv(const Dataset& d, const Segmentation& s, const MacroTable& macro, const ScenarioConfig& cfg) {
    CollateralResult out;
    out.cells.resize(s.segments.size());
    auto in_scope = [&](const CollateralAllocation& a) { return s.segment_of[a.exposure] >= 0; };
    auto gca = [&](std::uint32_t i) { return to_double(d.exposures[i].gca) * s.fx[i]; };

    // Pro-rata base per collateral: in-scope GCA and number of in-scope allocations.
    std::vector<double> base(d.collateral.size(), 0.0);
    std::vector<std::uint32_t> links(d.collateral.size(), 0);
    for (const auto& a : d.collateral_allocations) {
        if (!in_scope(a)) continue;
        base[a.collateral] += gca(a.exposure);
        links[a.collateral] += 1;
    }

    // Value index per (type, country), computed once.
    std::map<std::pair<CollateralType, std::uint32_t>, CollateralIndex> indices;
    auto index_of = [&](const Collateral& k) -> const CollateralIndex& {
        const auto key = std::make_pair(k.type, k.property_country);
        auto it = indices.find(key);
        if (it == indices.end()) {
            const std::string country = k.property_country == kNone ? std::string() : d.countries.at(k.property_country);
            it = indices.emplace(key, collateral_index(k.type, country, macro, cfg)).first;
        }
        return it->second;
    };

    // Allocations are ordered by exposure: accumulate per exposure, then add to its segment and stage.
    std::size_t i = 0;
    const auto n = d.collateral_allocations.size();
    while (i < n) {
        const auto ex = d.collateral_allocations[i].exposure;
        std::array<double, kCollateralSlots> value{};
        bool secured = false;
        for (; i < n && d.collateral_allocations[i].exposure == ex; ++i) {
            const auto& a = d.collateral_allocations[i];
            if (!in_scope(a)) continue;
            const auto& k = d.collateral[a.collateral];
            if (!a.has_amount) out.pro_rata_allocations += 1;
            if (k.type == CollateralType::Other) continue;
            const double fx = d.fx(k.currency);
            if (fx == 0.0) throw Error("no FX rate at the reference date for collateral currency " + d.currencies.at(k.currency));
            double v = 0;
            if (a.has_amount) v = to_double(a.amount) * fx;
            else if (base[a.collateral] > 0) v = to_double(k.market_value) * fx * gca(ex) / base[a.collateral];
            else v = to_double(k.market_value) * fx / links[a.collateral];
            const auto& idx = index_of(k);
            value[0] += v;
            for (std::size_t sc = 0; sc < 2; ++sc)
                for (std::size_t t = 1; t <= 3; ++t) value[1 + sc * 3 + t - 1] += v * idx[sc][t];
            secured = true;
        }
        if (!secured) continue;
        out.secured_exposures += 1;
        const auto st = static_cast<std::size_t>(d.exposures[ex].stage);
        if (st > 2) continue;   // POCI and not applicable: no LTV column
        auto& cells = out.cells[static_cast<std::size_t>(s.segment_of[ex])];
        for (std::size_t slot = 0; slot < kCollateralSlots; ++slot) {
            cells[slot].secured_exp[st] += gca(ex);
            cells[slot].re_value[st] += value[slot];
        }
    }
    return out;
}

void write_collateral(const Segmentation& s, const CollateralResult& c, const fs::path& file) {
    std::ofstream f(file, std::ios::binary);
    if (!f) throw Error("cannot write " + file.string());
    f << "segment,scenario,year,secured_exp_s1,secured_exp_s2,secured_exp_s3,re_collateral_s1,re_collateral_s2,"
         "re_collateral_s3,ltv_s1,ltv_s2,ltv_s3\n";
    const char* scen[kCollateralSlots] = {"actual", "baseline", "baseline", "baseline", "adverse", "adverse", "adverse"};
    const int year[kCollateralSlots] = {0, 1, 2, 3, 1, 2, 3};
    char buf[64];
    for (std::size_t i = 0; i < s.segments.size(); ++i) {
        for (std::size_t slot = 0; slot < kCollateralSlots; ++slot) {
            const auto& x = c.cells[i][slot];
            f << s.segments[i].key << ',' << scen[slot] << ',' << year[slot];
            for (double v : x.secured_exp) { std::snprintf(buf, sizeof buf, "%.2f", v); f << ',' << buf; }
            for (double v : x.re_value) { std::snprintf(buf, sizeof buf, "%.2f", v); f << ',' << buf; }
            for (std::size_t st = 0; st < 3; ++st) {
                f << ',';
                if (x.re_value[st] > 0) { std::snprintf(buf, sizeof buf, "%.9f", x.secured_exp[st] / x.re_value[st]); f << buf; }
            }
            f << '\n';
        }
    }
}

}  // namespace sora
