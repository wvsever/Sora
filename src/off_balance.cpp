#include "sora/off_balance.hpp"

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <exception>
#include <fstream>
#include <ostream>
#include <thread>
#include <utility>

namespace sora {

namespace fs = std::filesystem;

namespace {

bool has_column(Duck& duck, const std::string& source, std::string_view column) {
    bool found = false;
    duck.query("DESCRIBE SELECT * FROM " + source, [&](const Chunk& c) {
        for (std::size_t r = 0; r < c.size(); ++r)
            if (c.str(0, r) == column) found = true;
    });
    return found;
}

// Exposures whose undrawn part the institution may cancel unconditionally at any time.
std::vector<char> cancellable_flags(Duck& duck, const Dataset& d) {
    std::vector<char> out(d.exposures.size(), 0);
    const auto src = sim_source(d.sim_dir, "sim_exposure");
    if (!has_column(duck, src, "is_unconditionally_cancellable")) return out;
    duck.query("SELECT CAST(exposure_id AS VARCHAR) FROM " + src + " WHERE CAST(is_unconditionally_cancellable AS BOOLEAN)",
               [&](const Chunk& c) {
                   for (std::size_t r = 0; r < c.size(); ++r)
                       if (const auto id = d.exposure_ids.find(c.str(0, r))) out[*id] = 1;
               });
    return out;
}

std::string money(double x) {
    char buf[64];
    std::snprintf(buf, sizeof buf, "%.2f", x);
    return buf;
}

// off_balance.csv columns after the keys: nominal amounts, then the projection.csv fields on post-CCF amounts.
constexpr std::array<const char*, 5> kNominalFields = {"nom_s1", "nom_s2", "nom_s3_old", "nom_s3_new", "nom_poci"};
constexpr std::size_t kFieldCount = kNominalFields.size() + kYearResultFields.size();
using Row = std::array<double, kFieldCount>;

Row year_row(const YearResult& nominal, const YearResult& post) {
    Row r{};
    const double nom[5] = {nominal.exp_s1, nominal.exp_s2, nominal.exp_s3_old, nominal.exp_s3_new, nominal.exp_poci};
    std::copy(nom, nom + 5, r.begin());
    const auto f = fields(post);
    std::copy(f.begin(), f.end(), r.begin() + 5);
    return r;
}

Row start_row(const OffBalanceGroup& g) {
    YearResult nominal, post;
    nominal.exp_s1 = g.nominal0[0]; nominal.exp_s2 = g.nominal0[1]; nominal.exp_s3_old = g.nominal0[2]; nominal.exp_poci = g.nominal0[3];
    post.exp_s1 = g.post0[0]; post.exp_s2 = g.post0[1]; post.exp_s3_old = g.post0[2]; post.exp_poci = g.post0[3];
    post.prov_old_s3 = g.provision0[2];
    post.prov_stock_s1 = g.provision0[0]; post.prov_stock_s2 = g.provision0[1];
    post.prov_stock_s3 = g.provision0[2]; post.prov_stock_poci = g.provision0[3];
    return year_row(nominal, post);
}

// Slots: actual/0, baseline 1..3, adverse 1..3.
Row slot_row(const OffBalanceGroup& g, std::size_t slot) {
    if (slot == 0) return start_row(g);
    const std::size_t sc = (slot - 1) / 3, t = (slot - 1) % 3;
    return year_row(g.nominal[sc][t], g.post[sc][t]);
}

std::size_t field_index(std::string_view name) {
    for (std::size_t i = 0; i < kNominalFields.size(); ++i) if (name == kNominalFields[i]) return i;
    for (std::size_t i = 0; i < kYearResultFields.size(); ++i) if (name == kYearResultFields[i]) return kNominalFields.size() + i;
    throw Error("off-balance: unknown field " + std::string(name));
}

constexpr std::array<ExposureType, 3> kTemplateTypes = {ExposureType::LoanCommitment, ExposureType::FinancialGuarantee,
                                                        ExposureType::OtherCommitment};
constexpr std::array<const char*, 3> kTypeLabels = {"Loan commitments given", "Financial guarantees given",
                                                    "Other Commitments given"};
constexpr std::array<const char*, 6> kSectorLabels = {"Central banks", "General governments", "Credit institutions",
                                                      "Other financial corporations", "Non-financial corporations",
                                                      "Households"};

std::size_t sector_of(const std::string& portfolio) {
    if (portfolio == "CB") return 0;
    if (portfolio == "GG") return 1;
    if (portfolio == "CI") return 2;
    if (portfolio == "OFC") return 3;
    if (portfolio.rfind("NFC", 0) == 0) return 4;
    return 5;   // HH_*
}

std::size_t type_slot(ExposureType t) {
    if (t == ExposureType::Loan) return 0;   // undrawn part of on-balance loans: loan commitments given (F 09.01)
    for (std::size_t i = 0; i < kTemplateTypes.size(); ++i) if (kTemplateTypes[i] == t) return i;
    throw Error("off-balance: not an off-balance exposure type");
}

struct TemplateColumn {
    const char* header;
    std::vector<const char*> sum;   // off_balance.csv fields added up
};

const std::vector<TemplateColumn>& template_columns() {
    static const std::vector<TemplateColumn> c = {
        {"Total nominal amount before CCF (total NomAmount)", {"nom_s1", "nom_s2", "nom_s3_old", "nom_s3_new", "nom_poci"}},
        {"Performing nominal amount before CCF (Perf NomAmount)", {"nom_s1", "nom_s2"}},
        {"of which: stage 1 (NomAmount S1)", {"nom_s1"}},
        {"of which: stage 2 (NomAmount S2)", {"nom_s2"}},
        {"Non-performing nominal amount before CCF (NomAmount S3)", {"nom_s3_old", "nom_s3_new"}},
        {"POCI nominal amount before CCF (NomAmount POCI)", {"nom_poci"}},
        {"Total nominal amount after CCF (total PostCCF)", {"exp_s1", "exp_s2", "exp_s3_old", "exp_s3_new", "exp_poci"}},
        {"Performing nominal amount after CCF (Perf PostCCF)", {"exp_s1", "exp_s2"}},
        {"of which: stage 1 (PostCCF S1)", {"exp_s1"}},
        {"of which: stage 2 (PostCCF S2)", {"exp_s2"}},
        {"Non-performing nominal amount after CCF (PostCCF S3)", {"exp_s3_old", "exp_s3_new"}},
        {"POCI nominal amount after CCF (PostCCF POCI)", {"exp_poci"}},
        {"Stock of provisions (Prov Stock)", {"prov_stock_s1", "prov_stock_s2", "prov_stock_s3", "prov_stock_poci"}},
        {"of which: performing assets (Prov Stock Perf)", {"prov_stock_s1", "prov_stock_s2"}},
        {"of which: stage 1 (Prov Stock S1)", {"prov_stock_s1"}},
        {"of which: stage 2 (Prov Stock S2)", {"prov_stock_s2"}},
        {"of which: non-performing assets (Prov Stock S3)", {"prov_stock_s3"}},
        {"of which: POCI (Prov Stock POCI)", {"prov_stock_poci"}},
    };
    return c;
}

}  // namespace

void CustomerCcf::load(Duck& duck, const std::string& source, const Dataset& d) {
    if (source.empty() || !has_column(duck, source, "ccf")) return;
    duck.query("SELECT CAST(level AS VARCHAR), CAST(key AS VARCHAR), CAST(ccf AS DOUBLE) FROM " + source +
                   " WHERE CAST(scenario AS VARCHAR) = 'actual' AND CAST(year AS BIGINT) = 0 AND ccf IS NOT NULL ORDER BY 1, 2",
               [&](const Chunk& c) {
                   for (std::size_t r = 0; r < c.size(); ++r) {
                       const auto level = c.str(0, r), key = c.str(1, r);
                       const double v = c.f64(2, r);
                       if (!(v >= 0.0 && v <= 1.0))
                           throw Error("risk parameters: ccf outside [0, 1] for " + std::string(level) + " " + std::string(key));
                       if (level == "exposure") {
                           if (const auto id = d.exposure_ids.find(key)) by_exposure_[*id] = v;   // unknown keys: PAR-002
                       } else {
                           by_level_[std::string(key)] = v;
                       }
                   }
               });
}

std::optional<double> CustomerCcf::find(std::size_t exposure, const Segmentation& s, const Segment& seg) const {
    if (const auto it = by_exposure_.find(exposure); it != by_exposure_.end()) return it->second;
    for (const auto level : seg.levels)   // most specific first
        if (const auto it = by_level_.find(std::string(s.level_keys.view(level))); it != by_level_.end()) return it->second;
    return std::nullopt;
}

double fallback_ccf(ExposureType type, bool unconditionally_cancellable, const OffBalanceConfig& c) {
    switch (type) {
        case ExposureType::FinancialGuarantee: return c.ccf_financial_guarantee;
        case ExposureType::LoanCommitment: return unconditionally_cancellable ? c.ccf_unconditionally_cancellable : c.ccf_loan_commitment;
        case ExposureType::OtherCommitment: return unconditionally_cancellable ? c.ccf_unconditionally_cancellable : c.ccf_other_commitment;
        default: throw Error("off-balance: no CCF for exposure type " + std::string(to_string(type)));
    }
}

void project_off_balance_item(Stage stage, double nominal, double ccf, double provision,
                              const std::array<ParamPath, 2>& paths, const ScenarioConfig& cfg, OffBalanceGroup& g) {
    const auto st = static_cast<std::size_t>(stage);
    if (st > 3) return;   // not_applicable: no IFRS 9 stage
    const double post = ccf * nominal;
    g.items += 1;
    g.nominal0[st] += nominal;
    g.post0[st] += post;
    g.provision0[st] += provision;
    project_exposure(stage, post, provision, paths, cfg, g.post);
    project_exposure(stage, nominal, 0.0, paths, cfg, g.nominal);
}

OffBalanceResult project_off_balance(Duck& duck, const Dataset& d, const Segmentation& s, const Projection& p,
                                     const MacroTable& macro, const ScenarioConfig& cfg, const ExternalParameters* external,
                                     const std::string& parameter_source, unsigned workers) {
    OffBalanceResult out;
    const auto& ob = cfg.off_balance;
    out.types = ob.types;
    CustomerCcf customer;
    customer.load(duck, parameter_source, d);
    const auto cancellable = cancellable_flags(duck, d);

    std::unordered_map<std::string, std::size_t> segment_index;
    for (std::size_t i = 0; i < s.segments.size(); ++i) segment_index.emplace(s.segments[i].key, i);
    std::vector<char> top(d.countries.size(), 0);
    for (const auto& c : s.top_countries)
        if (const auto id = d.countries.find(c)) top[*id] = 1;

    // Scope and parameter segment per item, in exposure order. Groups keyed by (segment, type).
    struct Item {
        std::uint32_t exposure;
        double nominal, ccf, provision;
    };
    std::map<std::pair<std::size_t, std::string_view>, std::vector<Item>> by_group;   // sorted: segment key, type name
    for (std::size_t i = 0; i < d.exposures.size(); ++i) {
        const auto& e = d.exposures[i];
        if (std::find(ob.types.begin(), ob.types.end(), e.type) == ob.types.end()) continue;
        if (std::find(cfg.scope.measurements.begin(), cfg.scope.measurements.end(), e.measurement) == cfg.scope.measurements.end()) continue;
        if (cfg.scope.exclude_intragroup && e.intragroup) continue;
        if (e.stage == Stage::NotApplicable) continue;
        const auto& cp = d.counterparties[e.counterparty];
        const auto country = e.country_of_risk != kNone ? e.country_of_risk : cp.country;
        const std::string portfolio = portfolio_of(e, cp);
        auto it = segment_index.find("LOANS|" + portfolio + "|" + (top[country] ? d.countries.at(country) : std::string("OTHER")));
        if (it == segment_index.end()) {   // no on-balance loans of that portfolio and country: its OTHER bucket
            it = segment_index.find("LOANS|" + portfolio + "|OTHER");
            if (it == segment_index.end()) { ++out.unmatched_items; continue; }
            ++out.fallback_items;
        }
        const double fx = d.fx(e.currency);
        if (fx == 0.0) throw Error("no FX rate at the reference date for " + d.currencies.at(e.currency));
        const double nominal = to_double(e.off_balance) * fx;
        // The allowance of a facility covers its drawn and undrawn parts: the undrawn share is the off-balance provision.
        const double undrawn = to_double(e.off_balance), base = undrawn + to_double(e.gca);
        const double provision = base > 0 ? to_double(e.allowance) * fx * (undrawn / base) : to_double(e.allowance) * fx;
        const auto own = customer.find(i, s, s.segments[it->second]);
        if (own) ++out.customer_ccf_items;
        const double ccf = own ? *own : fallback_ccf(e.type, cancellable[i] != 0, ob);
        by_group[{it->second, to_string(e.type)}].push_back({static_cast<std::uint32_t>(i), nominal, ccf, provision});
        ++out.items;
    }
    // Undrawn part of in-scope loans (include_loan_undrawn): a loan commitment given, in the loan's own segment,
    // with the undrawn share of the loan's allowance (the drawn share is on-balance, Segmentation::allowance).
    if (ob.include_loan_undrawn) {
        for (std::size_t i = 0; i < d.exposures.size(); ++i) {
            const auto& e = d.exposures[i];
            if (e.type != ExposureType::Loan || e.off_balance <= 0 || s.segment_of[i] < 0 || e.stage == Stage::NotApplicable) continue;
            const auto seg = static_cast<std::size_t>(s.segment_of[i]);
            const double fx = s.fx[i];
            const double undrawn = to_double(e.off_balance), base = undrawn + to_double(e.gca);
            const double nominal = undrawn * fx;
            const double provision = to_double(e.allowance) * fx * (undrawn / base);
            const auto own = customer.find(i, s, s.segments[seg]);
            if (own) ++out.customer_ccf_items;
            const double ccf = own ? *own : fallback_ccf(ExposureType::LoanCommitment, cancellable[i] != 0, ob);
            by_group[{seg, to_string(e.type)}].push_back({static_cast<std::uint32_t>(i), nominal, ccf, provision});
            ++out.items;
            ++out.loan_undrawn_items;
        }
    }
    out.commitment_drawn_exposures = s.drawn_commitments;

    // Groups in parallel; each group entirely by one worker, in exposure order, into its own slot.
    std::vector<const std::vector<Item>*> members;
    for (const auto& [key, items] : by_group) {
        OffBalanceGroup g;
        g.segment = key.first;
        g.type = d.exposures[items.front().exposure].type;
        out.groups.push_back(g);
        members.push_back(&items);
    }
    const std::size_t ngroups = out.groups.size();
    struct GroupLog {
        std::size_t own = 0;
        std::vector<std::pair<std::size_t, std::string>> errors;
        std::exception_ptr failure;
    };
    std::vector<GroupLog> logs(ngroups);
    auto run_group = [&](std::size_t gi) {
        auto& g = out.groups[gi];
        auto& log = logs[gi];
        const auto& seg = s.segments[g.segment];
        const bool by_sector = has_sector_breakdown(seg);
        for (const auto& item : *members[gi]) {
            const auto& e = d.exposures[item.exposure];
            // The on-balance rule: the segment's satellite (flat when the portfolio has none), the path of the
            // counterparty's sector when it has a sectoral satellite, the segment's ECB benchmark groups.
            const NaceSector nace = by_sector ? d.counterparties[e.counterparty].nace : NaceSector::Unknown;
            p.check_modelled(seg, g.segment, nace);
            if (external && external->has_exposure(item.exposure)) {
                const auto own = own_param_paths(p, s, g.segment, nace, macro, cfg, *external, item.exposure);
                for (std::size_t sc = 0; sc < 2; ++sc)
                    for (std::size_t t = 0; t <= 3; ++t) {
                        if (sc == 1 && t == 0) continue;
                        for (const auto& msg : check_parameters(own[sc][t]))
                            log.errors.emplace_back(item.exposure, d.exposure_ids.at(e.id) + " " +
                                                                       (t == 0 ? std::string("actual/0") : std::string(kScenarios[sc]) + "/" + std::to_string(t)) + ": " + msg);
                    }
                ++log.own;
                project_off_balance_item(e.stage, item.nominal, item.ccf, item.provision, own, cfg, g);
            } else {
                project_off_balance_item(e.stage, item.nominal, item.ccf, item.provision, p.paths(g.segment, nace), cfg, g);
            }
        }
    };
    std::atomic<std::size_t> next{0};
    std::atomic<bool> stop{false};
    auto worker = [&] {
        for (std::size_t k = next++; k < ngroups && !stop.load(std::memory_order_relaxed); k = next++) {
            try {
                run_group(k);
            } catch (...) {
                logs[k].failure = std::current_exception();
                stop.store(true, std::memory_order_relaxed);
            }
        }
    };
    if (workers == 0) workers = std::max(1U, std::thread::hardware_concurrency());
    workers = static_cast<unsigned>(std::min<std::size_t>(workers, std::max<std::size_t>(ngroups, 1)));
    if (workers <= 1) {
        worker();
    } else {
        std::vector<std::thread> pool;
        pool.reserve(workers);
        for (unsigned w = 0; w < workers; ++w) pool.emplace_back(worker);
        for (auto& t : pool) t.join();
    }
    std::vector<std::pair<std::size_t, std::string>> errors;
    for (auto& log : logs) {
        if (log.failure) std::rethrow_exception(log.failure);
        out.exposures_with_own_parameters += log.own;
        for (auto& e : log.errors) errors.push_back(std::move(e));
    }
    if (!errors.empty()) {
        std::stable_sort(errors.begin(), errors.end(), [](const auto& a, const auto& b) { return a.first < b.first; });
        throw Error("off-balance: " + std::to_string(errors.size()) + " invalid parameter values (PAR-010), first: " + errors.front().second);
    }
    return out;
}

void write_off_balance(const Dataset& d, const Segmentation& s, const OffBalanceResult& r, const fs::path& dir) {
    constexpr std::size_t kSlots = 7;
    const char* scen[kSlots] = {"actual", "baseline", "baseline", "baseline", "adverse", "adverse", "adverse"};
    const int year[kSlots] = {0, 1, 2, 3, 1, 2, 3};
    {
        std::ofstream f(dir / "off_balance.csv", std::ios::binary);
        if (!f) throw Error("cannot write " + (dir / "off_balance.csv").string());
        f << "segment,exposure_type,scenario,year";
        for (auto n : kNominalFields) f << ',' << n;
        for (auto n : kYearResultFields) f << ',' << n;
        f << '\n';
        for (const auto& g : r.groups)
            for (std::size_t slot = 0; slot < kSlots; ++slot) {
                f << s.segments[g.segment].key << ',' << to_string(g.type) << ',' << scen[slot] << ',' << year[slot];
                for (double x : slot_row(g, slot)) f << ',' << money(x);
                f << '\n';
            }
    }

    // CSV_CR_SCEN_OFF_BS: [slot][type][sector] sums, then 22 rows per slot (Total geography only).
    std::vector<std::array<std::array<Row, 6>, 3>> cells(kSlots);
    for (const auto& g : r.groups) {
        const std::size_t ti = type_slot(g.type), si = sector_of(s.segments[g.segment].portfolio);
        for (std::size_t slot = 0; slot < kSlots; ++slot) {
            const Row row = slot_row(g, slot);
            auto& c = cells[slot][ti][si];
            for (std::size_t k = 0; k < kFieldCount; ++k) c[k] += row[k];
        }
    }
    std::vector<std::vector<std::size_t>> column_fields;
    for (const auto& c : template_columns()) {
        column_fields.emplace_back();
        for (auto n : c.sum) column_fields.back().push_back(field_index(n));
    }
    std::ofstream f(dir / "cr_scen_off_bs.csv", std::ios::binary);
    if (!f) throw Error("cannot write " + (dir / "cr_scen_off_bs.csv").string());
    f << "RowNum,Pivot,Geographical breakdown,Scenario,Year,Portfolio,Asset class 1,Asset class 2,Asset classes";
    for (const auto& c : template_columns()) f << ',' << c.header;
    f << '\n';
    const int ref_year = std::stoi(d.manifest.reference_date.substr(0, 4));
    const char* label[kSlots] = {"Actual", "Baseline", "Baseline", "Baseline", "Adverse", "Adverse", "Adverse"};
    char buf[64];
    for (std::size_t slot = 0; slot < kSlots; ++slot) {
        auto emit = [&](int num, const char* pivot, const char* ac1, const char* ac2, const char* name,
                        const std::vector<std::pair<std::size_t, std::size_t>>& keys) {
            Row v{};
            for (const auto& [ti, si] : keys)
                for (std::size_t k = 0; k < kFieldCount; ++k) v[k] += cells[slot][ti][si][k];
            f << num << ',' << pivot << ",Total," << label[slot] << ',' << (ref_year + year[slot]) << ",Off-balance sheet,"
              << ac1 << ',' << ac2 << ',' << name;
            for (const auto& cf : column_fields) {
                double x = 0.0;
                for (const auto k : cf) x += v[k];
                std::snprintf(buf, sizeof buf, "%.8f", x / 1e6);
                f << ',' << buf;
            }
            f << '\n';
        };
        int num = 0;
        std::vector<std::pair<std::size_t, std::size_t>> all;
        for (std::size_t ti = 0; ti < kTemplateTypes.size(); ++ti) {
            std::vector<std::pair<std::size_t, std::size_t>> type_keys;
            for (std::size_t si = 0; si < kSectorLabels.size(); ++si) {
                type_keys.emplace_back(ti, si);
                all.emplace_back(ti, si);
            }
            emit(++num, "Sum", kTypeLabels[ti], "", kTypeLabels[ti], type_keys);
            for (std::size_t si = 0; si < kSectorLabels.size(); ++si)
                emit(++num, "Pivot", kTypeLabels[ti], kSectorLabels[si], kSectorLabels[si], {{ti, si}});
        }
        emit(++num, "Sum", "Total", "", "Total", all);
    }
}

void write_off_balance_summary(std::ostream& f, const OffBalanceResult& r) {
    f << "{\n    \"exposure_types\": [";
    for (std::size_t i = 0; i < r.types.size(); ++i) f << (i ? ", " : "") << '"' << to_string(r.types[i]) << '"';
    f << "],\n    \"items\": " << r.items << ",\n    \"fallback_items\": " << r.fallback_items
      << ",\n    \"unmatched_items\": " << r.unmatched_items << ",\n    \"customer_ccf_items\": " << r.customer_ccf_items
      << ",\n    \"loan_undrawn_items\": " << r.loan_undrawn_items
      << ",\n    \"commitment_drawn_exposures\": " << r.commitment_drawn_exposures
      << ",\n    \"totals\": {";
    // Keys sorted as strings: actual/0, adverse/1..3, baseline/1..3 (slots 0, 4..6, 1..3).
    const std::size_t order[7] = {0, 4, 5, 6, 1, 2, 3};
    const char* key[7] = {"actual/0", "baseline/1", "baseline/2", "baseline/3", "adverse/1", "adverse/2", "adverse/3"};
    bool first = true;
    for (const auto slot : order) {
        Row tot{};
        for (const auto& g : r.groups) {
            const Row row = slot_row(g, slot);
            for (std::size_t k = 0; k < kFieldCount; ++k) tot[k] += row[k];
        }
        f << (first ? "\n" : ",\n") << "      \"" << key[slot] << "\": {";
        for (std::size_t k = 0; k < kFieldCount; ++k)
            f << (k ? ",\n" : "\n") << "        \"" << (k < 5 ? kNominalFields[k] : kYearResultFields[k - 5]) << "\": " << money(tot[k]);
        f << "\n      }";
        first = false;
    }
    f << "\n    }\n  }";
}

}  // namespace sora
