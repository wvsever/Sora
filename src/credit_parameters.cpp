#include "sora/credit_parameters.hpp"
#include "sora/json_text.hpp"

#include <algorithm>
#include <cstdio>
#include <fstream>
#include <unordered_map>

namespace sora {

namespace {

constexpr std::array<const char*, 3> kExtraNames = {"ccf", "pd_reg", "lgd_reg"};

std::string_view eba_sector_code(EbaSector s) {
    switch (s) {
        case EbaSector::CentralBank: return "central_bank";
        case EbaSector::GeneralGovernment: return "general_government";
        case EbaSector::CreditInstitution: return "credit_institution";
        case EbaSector::OtherFinancial: return "other_financial";
        case EbaSector::NonFinancialCorporation: return "non_financial_corporation";
        case EbaSector::Household: return "household";
    }
    return "non_financial_corporation";
}

std::string_view household_purpose_code(HouseholdPurpose p) {
    switch (p) {
        case HouseholdPurpose::HousePurchase: return "house_purchase";
        case HouseholdPurpose::Consumption: return "consumption";
        case HouseholdPurpose::Other: return "other";
        case HouseholdPurpose::None: break;
    }
    return {};
}

bool is_code(std::string_view s, std::size_t n) {
    return s.size() == n && std::all_of(s.begin(), s.end(), [](char c) { return c >= 'A' && c <= 'Z'; });
}

bool has_column(Duck& duck, const std::string& source, const std::string& column) {
    bool found = false;
    duck.query("SELECT count(*) FROM (DESCRIBE SELECT * FROM " + source + ") WHERE column_name = " + sql_quote(column),
               [&](const Chunk& c) { found = c.size() && c.i64(0, 0) > 0; });
    return found;
}

// Which of ccf / pd_reg / lgd_reg the parameter source has at exposure level for the starting point (bit i =
// kExtraNames[i]); those win over the calculator's values.
std::unordered_map<std::size_t, std::uint32_t> source_exposure_extras(Duck& duck, const std::string& source, const Dataset& d) {
    std::unordered_map<std::size_t, std::uint32_t> out;
    if (source.empty()) return out;
    std::string cols;
    bool any = false;
    for (const char* n : kExtraNames) {
        const bool has = has_column(duck, source, n);
        any = any || has;
        cols += has ? std::string(", ") + n + " IS NOT NULL" : std::string(", false");
    }
    if (!any) return out;
    duck.query("SELECT CAST(key AS VARCHAR)" + cols + " FROM " + source +
                   " WHERE CAST(level AS VARCHAR) = 'exposure' AND CAST(scenario AS VARCHAR) = 'actual' AND CAST(year AS BIGINT) = 0",
               [&](const Chunk& c) {
                   for (std::size_t r = 0; r < c.size(); ++r) {
                       const auto id = d.exposure_ids.find(c.str(0, r));
                       if (!id) continue;   // PAR-002
                       std::uint32_t bits = 0;
                       for (std::size_t k = 0; k < kExtraNames.size(); ++k)
                           if (c.valid(1 + k, r) && c.boolean(1 + k, r)) bits |= 1U << k;
                       out[*id] |= bits;
                   }
               });
    return out;
}

// The exposures whose parameters the run uses: in-scope on-balance exposures with a stage, and the off-balance items
// of the scenario's off-balance types (in the measurement and intragroup scope), in exposure order.
std::vector<std::uint32_t> exposures_to_request(const Dataset& d, const Segmentation& s, const ScenarioConfig& cfg) {
    std::vector<std::uint32_t> out;
    const auto& ob = cfg.off_balance;
    for (std::size_t i = 0; i < d.exposures.size(); ++i) {
        const auto& e = d.exposures[i];
        if (e.stage == Stage::NotApplicable) continue;
        bool use = s.segment_of[i] >= 0;
        if (!use && ob.enabled && std::find(ob.types.begin(), ob.types.end(), e.type) != ob.types.end() &&
            std::find(cfg.scope.measurements.begin(), cfg.scope.measurements.end(), e.measurement) != cfg.scope.measurements.end() &&
            !(cfg.scope.exclude_intragroup && e.intragroup))
            use = true;
        if (use) out.push_back(static_cast<std::uint32_t>(i));
    }
    return out;
}

calc::ParameterRecord make_record(const Dataset& d, const Segmentation& s, std::size_t i) {
    const auto& e = d.exposures[i];
    const auto& cp = d.counterparties[e.counterparty];
    calc::ParameterRecord r;
    r.record_id = d.exposure_ids.at(e.id);
    if (s.segment_of[i] >= 0) r.segment = s.segments[static_cast<std::size_t>(s.segment_of[i])].key;
    r.stage = std::string(to_string(e.stage));
    using Kind = calc::ParameterAttribute::Kind;
    const auto add = [&](const char* name, Kind kind, std::string_view text) { r.attributes.push_back({name, kind, std::string(text)}); };
    const auto& country = d.countries.at(e.country_of_risk != kNone ? e.country_of_risk : cp.country);
    if (is_code(country, 2)) add("country_of_risk", Kind::String, country);
    if (e.currency != kNone) add("currency", Kind::String, d.currencies.view(e.currency));
    add("eba_sector", Kind::String, eba_sector_code(cp.sector));
    add("exposure_type", Kind::String, to_string(e.type));
    if (const auto p = household_purpose_code(e.purpose); !p.empty()) add("household_purpose", Kind::String, p);
    if (e.is_cre != Flag::Unknown) add("is_cre", Kind::Boolean, e.is_cre == Flag::True ? "true" : "false");
    if (cp.is_sme != Flag::Unknown) add("is_sme", Kind::Boolean, cp.is_sme == Flag::True ? "true" : "false");
    add("measurement_category", Kind::String, to_string(e.measurement));
    return r;
}

std::string rate(Nano x) { return calc::format_decimal(x, 9); }

}  // namespace

const char* calculator_param_name(std::size_t i) { return i < kParamCount ? kParamNames[i] : kExtraNames[i - kParamCount]; }

std::vector<std::string> parse_calculator_parameters(const std::string& list) {
    std::vector<std::string> out;
    if (list == "all") {
        for (std::size_t i = 0; i < kCalculatorParamCount; ++i) out.emplace_back(calculator_param_name(i));
        return out;
    }
    std::size_t start = 0;
    while (start <= list.size()) {
        const auto comma = list.find(',', start);
        std::string name = list.substr(start, comma == std::string::npos ? std::string::npos : comma - start);
        name.erase(0, name.find_first_not_of(' '));
        name.erase(name.find_last_not_of(' ') + 1);
        bool known = false;
        for (std::size_t i = 0; i < kCalculatorParamCount; ++i) known = known || name == calculator_param_name(i);
        if (!known) {
            std::string names;
            for (std::size_t i = 0; i < kCalculatorParamCount; ++i) names += std::string(i ? ", " : "") + calculator_param_name(i);
            throw Error("--calculator-parameters: unknown parameter '" + name + "' (use all, or some of: " + names + ")");
        }
        if (std::find(out.begin(), out.end(), name) != out.end()) throw Error("--calculator-parameters: " + name + " given twice");
        out.push_back(std::move(name));
        if (comma == std::string::npos) break;
        start = comma + 1;
    }
    return out;
}

CreditParameterResult fetch_credit_parameters(Duck& duck, const CreditParameterInputs& in, ExternalParameters& ext,
                                              const calc::ClientOptions& options) {
    const auto& d = in.dataset;
    const auto& s = in.segmentation;
    calc::ClientOptions opt = options;
    if (opt.run_id.empty())
        opt.run_id = "sora|" + in.config.name + "|" + d.manifest.reference_date + "|" + d.manifest.mapping_release;
    calc::Client client(opt);
    const auto& caps = client.connect("parameters-credit");

    CreditParameterResult out;
    out.calculator = caps.name;
    out.version = caps.version;
    out.param_set = opt.param_set;
    out.parameters = in.parameters;
    // Position of each requested parameter in the calculator_param_name() order.
    std::vector<std::size_t> index;
    for (const auto& p : in.parameters)
        for (std::size_t i = 0; i < kCalculatorParamCount; ++i)
            if (p == calculator_param_name(i)) index.push_back(i);
    if (index.empty() || index.size() != in.parameters.size()) throw Error("calculator parameters: invalid parameter list");

    const auto sent = exposures_to_request(d, s, in.config);
    out.records = sent.size();
    out.rows.resize(sent.size());
    calc::ParameterSpec spec;
    spec.parameters = in.parameters;
    spec.years = {0};
    calc::ParameterStream stream;
    calc::RequestContext ctx;
    ctx.scenario = "actual";
    ctx.year = 0;
    ctx.reference_date = d.manifest.reference_date;
    ctx.currency = d.manifest.reporting_currency;
    stream.contexts = {ctx};
    stream.counts = {sent.size()};
    stream.fill = [&](std::size_t, std::size_t first, std::size_t count, std::vector<calc::ParameterRecord>& records) {
        records.reserve(count);
        for (std::size_t j = first; j < first + count; ++j) records.push_back(make_record(d, s, sent[j]));
    };
    std::vector<std::string> examples;
    // Called in record order: the rejection examples are the first ones.
    stream.sink = [&](std::size_t, std::size_t first, std::vector<calc::ParameterResult>& results) {
        for (std::size_t j = 0; j < results.size(); ++j) {
            auto& row = out.rows[first + j];
            const auto& res = results[j];
            row.exposure = sent[first + j];
            row.ok = res.ok;
            if (!res.ok) {
                ++out.rejected;
                if (examples.size() < 3) {
                    std::string ex = res.record_id;
                    if (!res.errors.empty()) ex += " (" + res.errors[0].code + ": " + res.errors[0].message + ")";
                    examples.push_back(std::move(ex));
                }
                continue;
            }
            ++out.ok;
            for (const auto& v : res.values) {
                for (std::size_t k = 0; k < in.parameters.size(); ++k)
                    if (v.parameter == in.parameters[k]) row.values[index[k]] = v.value;
                ++out.sources[v.source.empty() ? "unspecified" : v.source];
            }
        }
    };
    client.parameters(spec, stream);
    out.stats = client.stats();

    // Merge below the source's exposure rows, field by field.
    const auto file_extras = source_exposure_extras(duck, in.parameter_source, d);
    for (auto& row : out.rows) {
        if (!row.ok) continue;
        OptParams op;
        for (std::size_t i = 0; i < kParamCount; ++i)
            if (row.values[i]) op.v[i] = nano_to_double(*row.values[i]);
        row.applied = ext.fill_exposure(row.exposure, op);
        ExternalParameters::ExposureExtras extras;
        std::optional<double>* slots[3] = {&extras.ccf, &extras.pd_reg, &extras.lgd_reg};
        const auto f = file_extras.find(row.exposure);
        const std::uint32_t file_bits = f == file_extras.end() ? 0 : f->second;
        bool any_extra = false;
        for (std::size_t k = 0; k < kExtraNames.size(); ++k) {
            const auto& v = row.values[kParamCount + k];
            if (!v || (file_bits & (1U << k))) continue;
            *slots[k] = nano_to_double(*v);
            row.applied |= 1U << (kParamCount + k);
            any_extra = true;
        }
        if (any_extra) ext.set_exposure_extras(row.exposure, extras);
        for (std::size_t k = 0; k < index.size(); ++k) {
            auto& c = out.counts[in.parameters[k]];
            const std::size_t i = index[k];
            if (!row.values[i]) { ++c.missing; continue; }
            ++c.received;
            if (row.applied & (1U << i)) ++c.applied; else ++c.file;
        }
    }
    for (const auto& p : in.parameters) out.counts[p];   // every requested parameter is listed

    std::uint64_t received = 0, applied = 0, file = 0, missing = 0;
    std::string missing_by, file_by;
    for (const auto& p : in.parameters) {
        const auto& c = out.counts[p];
        received += c.received;
        applied += c.applied;
        file += c.file;
        missing += c.missing;
        if (c.missing) missing_by += (missing_by.empty() ? "" : ", ") + p + " " + std::to_string(c.missing);
        if (c.file) file_by += (file_by.empty() ? "" : ", ") + p + " " + std::to_string(c.file);
    }
    const auto& st = out.stats;
    out.findings.push_back({"CALC-020", "info",
                            "credit parameters by " + caps.name + " " + caps.version + " (" + opt.param_set + "): " +
                                std::to_string(st.batches) + " batches, " + std::to_string(st.requests) + " HTTP requests, " +
                                std::to_string(st.retries) + " retries, " + std::to_string(st.jobs) + " jobs, " +
                                std::to_string(st.cache_hits) + " batches from the replay cache; " + std::to_string(received) +
                                " values received, " + std::to_string(applied) + " applied",
                            out.records});
    if (st.offline)
        out.findings.push_back({"CALC-021", "info",
                                "calculator unreachable (" + st.offline_reason + "): credit parameters replayed from the cache",
                                st.cache_hits});
    if (missing)
        out.findings.push_back({"CALC-022", "warning",
                                "requested credit parameters not returned by the calculator (not filled in; the next source "
                                "applies): " + missing_by,
                                missing});
    if (file)
        out.findings.push_back({"CALC-023", "info",
                                "calculator values not used: the parameter source has an exposure row with the field (" +
                                    file_by + ")",
                                file});
    if (st.cache_write_failures)
        out.findings.push_back({"CALC-024", "warning",
                                "replay cache not written (results are unaffected; a rerun calls the calculator again): " +
                                    st.cache_write_error,
                                st.cache_write_failures});
    if (out.rejected) {
        std::string msg = "credit parameter records rejected by the calculator (the next source applies), e.g. ";
        for (std::size_t i = 0; i < examples.size(); ++i) msg += (i ? "; " : "") + examples[i];
        out.findings.push_back({"CALC-025", "warning", msg, out.rejected});
    }
    if (out.rejected && !out.ok) {
        out.all_rejected = true;
        out.findings.push_back({"CALC-026", "error", "all credit parameter records rejected by the calculator", out.rejected});
    }
    return out;
}

void write_calculator_parameter_rows(std::ostream& f, const Dataset& d, const CreditParameterResult& r) {
    for (const auto& row : r.rows) {
        if (!(row.applied & ((1U << kParamCount) - 1))) continue;
        f << "exposure," << d.exposure_ids.view(d.exposures[row.exposure].id) << ",actual,0";
        for (std::size_t i = 0; i < kParamCount; ++i) {
            f << ',';
            if (row.applied & (1U << i)) f << rate(*row.values[i]);
        }
        f << ",calculator,\n";
    }
}

void write_calculator_parameters_csv(const Dataset& d, const CreditParameterResult& r, const std::filesystem::path& file) {
    std::ofstream f(file, std::ios::binary);
    if (!f) throw Error("cannot write " + file.string());
    std::vector<std::size_t> index;
    f << "exposure_id,status";
    for (const auto& p : r.parameters) {
        f << ',' << p;
        for (std::size_t i = 0; i < kCalculatorParamCount; ++i)
            if (p == calculator_param_name(i)) index.push_back(i);
    }
    f << ",applied\n";
    for (const auto& row : r.rows) {
        f << d.exposure_ids.view(d.exposures[row.exposure].id) << ',' << (row.ok ? "ok" : "rejected");
        std::string applied;
        for (const std::size_t i : index) {
            f << ',';
            if (row.values[i]) f << rate(*row.values[i]);
            if (row.applied & (1U << i)) applied += (applied.empty() ? "" : ";") + std::string(calculator_param_name(i));
        }
        f << ',' << applied << '\n';
    }
}

void write_calculator_parameters_summary(std::ostream& f, const CreditParameterResult& r) {
    f << "{\n    \"calculator\": " << json_quote(r.calculator) << ",\n    \"calculator_version\": " << json_quote(r.version)
      << ",\n    \"param_set\": " << json_quote(r.param_set) << ",\n    \"records\": " << r.records
      << ",\n    \"records_ok\": " << r.ok << ",\n    \"records_rejected\": " << r.rejected << ",\n    \"parameters\": {";
    bool first = true;
    for (const auto& p : r.parameters) {
        const auto it = r.counts.find(p);
        const CreditParameterCount c = it == r.counts.end() ? CreditParameterCount{} : it->second;
        f << (first ? "\n" : ",\n") << "      " << json_quote(p) << ": {\"received\": " << c.received << ", \"applied\": " << c.applied
          << ", \"file\": " << c.file << ", \"missing\": " << c.missing << "}";
        first = false;
    }
    f << "\n    },\n    \"sources\": {";
    first = true;
    for (const auto& [src, n] : r.sources) {
        f << (first ? "" : ", ") << json_quote(src) << ": " << n;
        first = false;
    }
    f << "}\n  }";
}

}  // namespace sora
