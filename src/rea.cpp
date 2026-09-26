#include "sora/rea.hpp"
#include "sora/json_text.hpp"

#include <algorithm>
#include <cstdint>
#if defined(_MSC_VER) && !defined(__clang__)
#include <intrin.h>
#endif
#include <atomic>
#include <cmath>
#include <fstream>
#include <optional>
#include <unordered_map>

namespace sora {

namespace fs = std::filesystem;

const char* rea_slot_scenario(std::size_t slot) { return slot == 0 ? "actual" : slot <= 3 ? "baseline" : "adverse"; }
int rea_slot_year(std::size_t slot) { return slot == 0 ? 0 : static_cast<int>(slot <= 3 ? slot : slot - 3); }

std::string irb_exposure_class(const Exposure& e, const Counterparty& cp) {
    switch (cp.sector) {
        case EbaSector::CentralBank:                  // Art. 147(2)(a): central governments and central banks;
        case EbaSector::GeneralGovernment:            // regional governments and PSEs simplified to the same class
            return "central_governments";
        case EbaSector::CreditInstitution: return "institutions";
        case EbaSector::OtherFinancial: return "corporates_general";   // Art. 147(2)(c): not institutions
        case EbaSector::NonFinancialCorporation: return cp.is_sme == Flag::True ? "corporates_sme" : "corporates_general";
        case EbaSector::Household:
            return e.type != ExposureType::DebtSecurity && e.purpose == HouseholdPurpose::HousePurchase
                       ? "retail_residential_mortgage" : "retail_other";
    }
    return "corporates_general";
}

namespace {

// Exact conversion of an amount to the reporting currency: cents x rate (1e-9) rounded half to even.
Cents to_reporting(Cents c, Nano fx) {
    if (fx == 1'000'000'000) return c;
#if defined(_MSC_VER) && !defined(__clang__)
    // MSVC has no __int128: the 128-bit product and its division by 1e9 use the x64 intrinsics.
    std::int64_t hi = 0;
    const std::int64_t lo = _mul128(c, fx, &hi);
    std::int64_t r = 0;
    std::int64_t q = _div128(hi, lo, 1'000'000'000, &r);   // truncates toward zero
#else
    __extension__ typedef __int128 i128;
    const i128 p = static_cast<i128>(c) * fx;
    i128 q = p / 1'000'000'000, r = p % 1'000'000'000;
#endif
    if (r < 0) { r += 1'000'000'000; q -= 1; }   // floor division
    if (r > 500'000'000 || (r == 500'000'000 && (q & 1))) q += 1;
    return static_cast<Cents>(q);
}

bool has_column(Duck& duck, const std::string& source, const std::string& column) {
    bool found = false;
    duck.query("SELECT count(*) FROM (DESCRIBE SELECT * FROM " + source + ") WHERE column_name = " + sql_quote(column),
               [&](const Chunk& c) { found = c.size() && c.i64(0, 0) > 0; });
    return found;
}

// Counterparty attributes the IRB records need beyond the Dataset (loaded here to keep the dataset lean).
struct CounterpartyExtra {
    std::optional<Cents> turnover, total_assets;
};

std::vector<CounterpartyExtra> load_counterparty_extras(Duck& duck, const Dataset& d) {
    std::vector<CounterpartyExtra> out(d.counterparties.size());
    const auto src = sim_source(d.sim_dir, "sim_counterparty");
    const bool turnover = has_column(duck, src, "annual_turnover_eur"), assets = has_column(duck, src, "total_assets_eur");
    if (!turnover && !assets) return out;
    duck.query(std::string("SELECT CAST(counterparty_id AS VARCHAR), ") +
                   (turnover ? "CAST(annual_turnover_eur AS DECIMAL(18,2))" : "CAST(NULL AS DECIMAL(18,2))") + ", " +
                   (assets ? "CAST(total_assets_eur AS DECIMAL(18,2))" : "CAST(NULL AS DECIMAL(18,2))") + " FROM " + src,
               [&](const Chunk& c) {
                   for (std::size_t r = 0; r < c.size(); ++r) {
                       const auto id = d.counterparty_ids.find(c.str(0, r));
                       if (!id) continue;
                       if (c.valid(1, r)) out[*id].turnover = c.cents(1, r);
                       if (c.valid(2, r)) out[*id].total_assets = c.cents(2, r);
                   }
               });
    return out;
}

// Regulatory PD/LGD (pd_reg, lgd_reg) from the customer risk parameters, by exposure and segment level.
class RegParams {
public:
    struct Value {
        std::optional<Nano> pd, lgd;
    };
    void load(Duck& duck, const std::string& source, const Dataset& d) {
        if (source.empty()) return;
        const bool pd = has_column(duck, source, "pd_reg"), lgd = has_column(duck, source, "lgd_reg");
        if (!pd && !lgd) return;
        const std::string pd_col = pd ? "CAST(pd_reg AS DECIMAL(18,9))" : "CAST(NULL AS DECIMAL(18,9))";
        const std::string lgd_col = lgd ? "CAST(lgd_reg AS DECIMAL(18,9))" : "CAST(NULL AS DECIMAL(18,9))";
        duck.query("SELECT CAST(level AS VARCHAR), CAST(key AS VARCHAR), CAST(scenario AS VARCHAR), CAST(year AS BIGINT), " +
                       pd_col + ", " + lgd_col + " FROM " + source + " WHERE " + (pd ? "pd_reg IS NOT NULL" : "false") +
                       " OR " + (lgd ? "lgd_reg IS NOT NULL" : "false"),
                   [&](const Chunk& c) {
                       for (std::size_t r = 0; r < c.size(); ++r) {
                           const auto scen = c.str(2, r);
                           const auto year = c.i64(3, r);
                           std::size_t slot;
                           if (scen == "actual" && year == 0) slot = 0;
                           else if (scen == "baseline" && year >= 1 && year <= 3) slot = static_cast<std::size_t>(year);
                           else if (scen == "adverse" && year >= 1 && year <= 3) slot = static_cast<std::size_t>(year) + 3;
                           else continue;   // rejected by ExternalParameters::load already
                           Value v;
                           if (c.valid(4, r)) v.pd = c.nano(4, r);
                           if (c.valid(5, r)) v.lgd = c.nano(5, r);
                           if (c.str(0, r) == "exposure") {
                               const auto ex = d.exposure_ids.find(c.str(1, r));
                               if (ex) by_exposure_[*ex][slot] = v;
                           } else {
                               by_level_[std::string(c.str(1, r))][slot] = v;
                           }
                           ++rows_;
                       }
                   });
    }
    // Starting-point pd_reg/lgd_reg of an exposure from the calculator (/v1/parameters/credit): only stored where
    // the source has no exposure-level value, so fields the loaded rows already set stay.
    void add_exposure(std::size_t exposure, std::optional<Nano> pd, std::optional<Nano> lgd) {
        if (!pd && !lgd) return;
        auto& v = by_exposure_[exposure][0];
        if (!v.pd) v.pd = pd;
        if (!v.lgd) v.lgd = lgd;
        ++rows_;
    }
    bool empty() const { return rows_ == 0; }
    // Most specific value per field: exposure, then segment levels (specific to general). A projection point
    // without a value falls back to the starting point (regulatory parameters are through the cycle).
    Value get(const Segmentation& s, const Segment& seg, std::size_t exposure, std::size_t slot) const {
        Value out;
        for (std::size_t sl : {slot, std::size_t{0}}) {
            auto take = [&](const Slots& x) {
                if (!out.pd) out.pd = x[sl].pd;
                if (!out.lgd) out.lgd = x[sl].lgd;
            };
            if (const auto f = by_exposure_.find(exposure); f != by_exposure_.end()) take(f->second);
            for (auto level : seg.levels)
                if (const auto f = by_level_.find(s.level_keys.at(level)); f != by_level_.end()) take(f->second);
            if (sl == 0) break;
        }
        return out;
    }

private:
    using Slots = std::array<Value, kReaSlots>;
    std::unordered_map<std::string, Slots> by_level_;
    std::unordered_map<std::size_t, Slots> by_exposure_;
    std::size_t rows_ = 0;
};

bool is_code(const std::string& s, std::size_t n) {
    return s.size() == n && std::all_of(s.begin(), s.end(), [](char c) { return c >= 'A' && c <= 'Z'; });
}

}  // namespace

ReaResult project_rea(Duck& duck, const ReaInputs& in, const calc::ClientOptions& options) {
    const auto& d = in.dataset;
    const auto& s = in.segmentation;
    const auto& proj = in.projection;
    calc::ClientOptions opt = options;
    if (opt.run_id.empty())
        opt.run_id = "sora|" + in.config.name + "|" + d.manifest.reference_date + "|" + d.manifest.mapping_release;
    calc::Client client(opt);
    const auto& caps = client.connect("irb");

    const auto extras = load_counterparty_extras(duck, d);
    RegParams reg;
    reg.load(duck, in.parameter_source, d);
    if (in.external)
        for (const auto& [i, x] : in.external->exposure_extras())
            reg.add_exposure(i, x.pd_reg ? std::optional<Nano>(calc::to_nano(*x.pd_reg)) : std::nullopt,
                             x.lgd_reg ? std::optional<Nano>(calc::to_nano(*x.lgd_reg)) : std::nullopt);

    // Exposures sent, in exposure order (the same records in every call). Records are generated per batch from
    // this list and released once their results are aggregated, so memory does not grow with 7 x N records.
    std::vector<std::uint32_t> sent;
    std::uint64_t skipped = 0;
    for (std::size_t i = 0; i < d.exposures.size(); ++i) {
        if (s.segment_of[i] < 0) continue;
        const auto& e = d.exposures[i];
        if (e.stage != Stage::S1 && e.stage != Stage::S2 && e.stage != Stage::S3) {
            if (e.stage == Stage::Poci) ++skipped;
            continue;
        }
        if (to_reporting(e.gca, d.fx_to_reporting.at(e.currency)) <= 0) { ++skipped; continue; }
        sent.push_back(static_cast<std::uint32_t>(i));
    }

    // Record of exposure `i` for scenario point `k`. `pit` is set when the PiT proxy is used.
    const auto make_record = [&](std::size_t i, std::size_t k, bool& pit) {
        const auto& e = d.exposures[i];
        const auto seg = static_cast<std::size_t>(s.segment_of[i]);
        const auto& segment = s.segments[seg];
        const auto& cp = d.counterparties[e.counterparty];
        const auto& extra = extras[e.counterparty];

        calc::IrbRecord r;
        r.exposure_class = irb_exposure_class(e, cp);
        r.ead = to_reporting(e.gca, d.fx_to_reporting.at(e.currency));
        if (e.has_maturity) {
            const double years = static_cast<double>(e.maturity - d.manifest.reference_day) / 365.25;
            r.maturity_bp = std::clamp<std::int64_t>(std::llround(years * 1e4), 10'000, 50'000);   // M in [1, 5]
        }
        if (r.exposure_class.rfind("corporates", 0) == 0 && extra.turnover) r.turnover_eur = *extra.turnover;
        // Art. 142(1)(4): large financial sector entity, total assets >= EUR 70 billion.
        if ((cp.sector == EbaSector::CreditInstitution || cp.sector == EbaSector::OtherFinancial) && extra.total_assets &&
            *extra.total_assets >= 7'000'000'000'000)
            r.large_financial_entity = true;
        const auto& country = d.countries.at(e.country_of_risk != kNone ? e.country_of_risk : cp.country);
        if (is_code(country, 2)) r.country = country;
        const int year = rea_slot_year(k);
        r.record_id = d.exposure_ids.at(e.id) + "|" + rea_slot_scenario(k) + "|" + std::to_string(year);

        // Parameters of this point: the segment's path, or the exposure's own path when it has exposure-level
        // parameters (exposure_param_paths, the same paths project() used for the provisions).
        const std::size_t sc = k <= 3 ? 0 : 1;
        Params p;
        const NaceSector nace = has_sector_breakdown(segment) ? cp.nace : NaceSector::Unknown;
        if (in.external && in.external->has_exposure(i)) {
            p = own_param_paths(proj, s, seg, nace, in.macro, in.config, *in.external, i)[sc][static_cast<std::size_t>(year)];
        } else {
            p = proj.paths(seg, nace)[sc][static_cast<std::size_t>(year)];
        }
        const auto rp = reg.get(s, segment, i, k);
        if (e.stage == Stage::S3) {
            r.defaulted = true;
            r.pd = 1'000'000'000;
            r.elbe = calc::to_nano(p.lgd_s3);
            r.lgd = rp.lgd ? *rp.lgd : *r.elbe;
            pit = !rp.lgd;
        } else {
            const bool s1 = e.stage == Stage::S1;
            r.pd = rp.pd ? *rp.pd : calc::to_nano(s1 ? p.pd12m_s1 : p.pd12m_s2);
            r.lgd = rp.lgd ? *rp.lgd : calc::to_nano(s1 ? p.lgd_s1 : p.lgd_s2);
            pit = !rp.pd || !rp.lgd;
        }
        return r;
    };

    ReaResult out;
    out.calculator = caps.name;
    out.version = caps.version;
    out.param_set = opt.param_set;
    out.cells.assign(s.segments.size(), {});

    std::atomic<std::uint64_t> proxy{0};
    std::uint64_t ok = 0, rejected = 0;
    std::vector<std::string> examples;
    calc::IrbStream stream;
    for (std::size_t k = 0; k < kReaSlots; ++k) {
        calc::RequestContext ctx;
        ctx.scenario = rea_slot_scenario(k);
        ctx.year = rea_slot_year(k);
        ctx.reference_date = d.manifest.reference_date;
        ctx.currency = d.manifest.reporting_currency;
        stream.contexts.push_back(std::move(ctx));
        stream.counts.push_back(sent.size());
    }
    stream.fill = [&](std::size_t k, std::size_t first, std::size_t count, std::vector<calc::IrbRecord>& records) {
        records.reserve(count);
        std::uint64_t pit_records = 0;
        for (std::size_t j = first; j < first + count; ++j) {
            bool pit = false;
            records.push_back(make_record(sent[j], k, pit));
            pit_records += pit ? 1 : 0;
        }
        proxy += pit_records;
    };
    // Called in call and record order, so the rejection examples are the first ones, as before.
    stream.sink = [&](std::size_t k, std::size_t first, std::vector<calc::IrbResult>& results) {
        for (std::size_t j = 0; j < results.size(); ++j) {
            const auto& res = results[j];
            const auto& e = d.exposures[sent[first + j]];
            auto& cell = out.cells[static_cast<std::size_t>(s.segment_of[sent[first + j]])][k];
            if (res.ok) {
                cell.ead += to_reporting(e.gca, d.fx_to_reporting.at(e.currency));   // the record's EAD
                cell.rea += res.rea;
                cell.expected_loss += res.expected_loss;
                ++cell.ok;
                ++ok;
            } else {
                ++cell.rejected;
                ++rejected;
                if (examples.size() < 3) {
                    std::string ex = res.record_id;
                    if (!res.errors.empty()) ex += " (" + res.errors[0].code + ": " + res.errors[0].message + ")";
                    examples.push_back(ex);
                }
            }
        }
    };
    client.irb(stream);
    out.stats = client.stats();

    const auto& st = out.stats;
    out.findings.push_back({"CALC-000", "info",
                            "IRB REA by " + caps.name + " " + caps.version + " (" + opt.param_set + "): " +
                                std::to_string(st.batches) + " batches, " + std::to_string(st.requests) + " HTTP requests, " +
                                std::to_string(st.retries) + " retries, " + std::to_string(st.jobs) + " jobs, " +
                                std::to_string(st.cache_hits) + " batches from the replay cache",
                            ok + rejected});
    if (st.offline)
        out.findings.push_back({"CALC-001", "info",
                                "calculator unreachable (" + st.offline_reason + "): results replayed from the cache",
                                st.cache_hits});
    if (st.cache_write_failures)
        out.findings.push_back({"CALC-004", "warning",
                                "replay cache not written (results are unaffected; a rerun calls the calculator again): " +
                                    st.cache_write_error,
                                st.cache_write_failures});
    if (proxy)
        out.findings.push_back({"CALC-002", "warning",
                                "IFRS 9 point-in-time PD/LGD used as a proxy for the regulatory PD/LGD (no pd_reg/lgd_reg supplied)",
                                proxy});
    if (skipped) out.findings.push_back({"CALC-003", "info", "exposures not sent to the calculator (POCI or zero EAD)", skipped});
    if (rejected) {
        std::string msg = "records rejected by the calculator, e.g. ";
        for (std::size_t i = 0; i < examples.size(); ++i) msg += (i ? "; " : "") + examples[i];
        out.findings.push_back({"CALC-010", "warning", msg, rejected});
    }
    if (rejected && !ok) {
        out.all_rejected = true;
        out.findings.push_back({"CALC-011", "error", "all records rejected by the calculator", rejected});
    }
    return out;
}

void write_rea_csv(const Segmentation& s, const ReaResult& r, const fs::path& file) {
    std::ofstream f(file, std::ios::binary);
    if (!f) throw Error("cannot write " + file.string());
    f << "segment,scenario,year,ead,rea,expected_loss,records_ok,records_rejected\n";
    for (std::size_t i = 0; i < s.segments.size() && i < r.cells.size(); ++i) {
        const auto& cells = r.cells[i];
        if (std::none_of(cells.begin(), cells.end(), [](const ReaCell& c) { return c.ok + c.rejected > 0; })) continue;
        for (std::size_t k = 0; k < kReaSlots; ++k) {
            const auto& c = cells[k];
            f << s.segments[i].key << ',' << rea_slot_scenario(k) << ',' << rea_slot_year(k) << ','
              << calc::format_decimal(c.ead, 2) << ',' << calc::format_decimal(c.rea, 2) << ','
              << calc::format_decimal(c.expected_loss, 2) << ',' << c.ok << ',' << c.rejected << '\n';
        }
    }
}

void write_rea_summary(std::ostream& f, const ReaResult& r) {
    f << "{\n    \"calculator\": " << json_quote(r.calculator) << ",\n    \"calculator_version\": " << json_quote(r.version)
      << ",\n    \"param_set\": " << json_quote(r.param_set) << ",\n    \"totals\": {";
    for (std::size_t k = 0; k < kReaSlots; ++k) {
        ReaCell t;
        for (const auto& cells : r.cells) {
            t.ead += cells[k].ead;
            t.rea += cells[k].rea;
            t.expected_loss += cells[k].expected_loss;
            t.ok += cells[k].ok;
            t.rejected += cells[k].rejected;
        }
        f << (k ? ",\n" : "\n") << "      \"" << rea_slot_scenario(k) << '/' << rea_slot_year(k) << "\": {\"ead\": "
          << calc::format_decimal(t.ead, 2) << ", \"rea\": " << calc::format_decimal(t.rea, 2)
          << ", \"expected_loss\": " << calc::format_decimal(t.expected_loss, 2) << ", \"records_ok\": " << t.ok
          << ", \"records_rejected\": " << t.rejected << "}";
    }
    f << "\n    }\n  }";
}

}  // namespace sora
