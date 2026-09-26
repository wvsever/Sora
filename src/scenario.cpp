#include "sora/scenario.hpp"

#include <algorithm>
#include <cmath>
#include <fstream>
#include <sstream>

#include <ryml_all.hpp>

namespace sora {

namespace fs = std::filesystem;

namespace {

std::string read_file(const fs::path& p) {
    std::ifstream in(p, std::ios::binary);
    if (!in) throw Error("cannot read " + p.string());
    std::ostringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

std::string str(ryml::ConstNodeRef n) {
    std::string s;
    ryml::from_chars(n.val(), &s);
    return s;
}

template <class T>
T num(ryml::ConstNodeRef n) {
    T v{};
    if (!ryml::from_chars(n.val(), &v)) throw Error("scenario: invalid number for " + std::string(n.key().str, n.key().len));
    return v;
}

}  // namespace

ScenarioConfig load_scenario(const fs::path& yaml, const fs::path& base_dir) {
    std::string text = read_file(yaml);
    ryml::Tree tree = ryml::parse_in_arena(ryml::to_csubstr(yaml.string()), ryml::to_csubstr(text));
    ryml::ConstNodeRef root = tree.crootref();
    ScenarioConfig c;
    auto path = [&](const char* key) {
        fs::path p = str(root[ryml::to_csubstr(key)]);
        return p.is_absolute() ? p : base_dir / p;
    };
    c.name = str(root["name"]);
    c.macro_path = path("macro_path");
    c.satellites_path = path("satellites");
    for (auto ch : root["year_map"].children()) {
        int k = 0, v = 0;
        ryml::from_chars(ch.key(), &k);
        ryml::from_chars(ch.val(), &v);
        c.year_map[k] = v;
    }
    c.history_year = num<int>(root["history_year"]);
    c.normal_gdp_growth = num<double>(root["normal_gdp_growth"]);
    for (auto ch : root["country_fallback"].children()) c.country_fallback.push_back(str(ch));

    auto scope = root["scope"];
    c.scope.measurements.clear();
    for (auto ch : scope["measurement_categories"].children()) c.scope.measurements.push_back(parse_measurement(str(ch)));
    c.scope.types.clear();
    for (auto ch : scope["exposure_types"].children()) c.scope.types.push_back(parse_exposure_type(str(ch)));
    c.scope.exclude_intragroup = str(scope["exclude_intragroup"]) == "true";
    c.scope.top_countries = num<std::size_t>(root["segmentation"]["top_countries"]);

    auto cal = root["calibration"];
    c.calibration.min_observations = num<std::uint64_t>(cal["min_observations"]);
    c.calibration.pd_floor = num<double>(cal["pd_floor"]);

    auto con = root["constraints"];
    c.no_cure_from_s3 = str(con["no_cure_from_s3"]) == "true";
    auto blend = con["adverse_final_year_blend"];
    c.blend_adverse = num<double>(blend[0]);
    c.blend_baseline = num<double>(blend[1]);
    if (c.year_map.size() != 3) throw Error("scenario: year_map must define projection years 1..3");

    if (root.has_child(ryml::to_csubstr("off_balance"))) {
        auto ob = root["off_balance"];
        c.off_balance.enabled = true;
        for (auto ch : ob["exposure_types"].children()) {
            const auto t = parse_exposure_type(str(ch));
            if (t != ExposureType::LoanCommitment && t != ExposureType::FinancialGuarantee && t != ExposureType::OtherCommitment)
                throw Error("scenario: off_balance.exposure_types must be loan_commitment, financial_guarantee or other_commitment");
            c.off_balance.types.push_back(t);
        }
        if (ob.has_child(ryml::to_csubstr("ccf_fallback"))) {
            auto fb = ob["ccf_fallback"];
            auto set = [&](const char* key, double& v) {
                if (!fb.has_child(ryml::to_csubstr(key))) return;
                v = num<double>(fb[ryml::to_csubstr(key)]);
                if (!(v >= 0.0 && v <= 1.0)) throw Error(std::string("scenario: off_balance.ccf_fallback.") + key + " outside [0, 1]");
            };
            set("loan_commitment", c.off_balance.ccf_loan_commitment);
            set("financial_guarantee", c.off_balance.ccf_financial_guarantee);
            set("other_commitment", c.off_balance.ccf_other_commitment);
            set("unconditionally_cancellable", c.off_balance.ccf_unconditionally_cancellable);
        }
    }
    return c;
}

std::string MacroTable::k(const std::string& v, const std::string& key, const std::string& s, int y) {
    return v + '\x1f' + key + '\x1f' + s + '\x1f' + std::to_string(y);
}

std::optional<double> MacroTable::get(const std::string& variable, const std::string& key, const std::string& scenario, int year) const {
    auto it = values_.find(k(variable, key, scenario, year));
    if (it == values_.end()) return std::nullopt;
    return it->second;
}

void MacroTable::set(const std::string& variable, const std::string& key, const std::string& scenario, int year, double v) {
    values_[k(variable, key, scenario, year)] = v;
}

MacroTable load_macro(Duck& duck, const fs::path& csv) {
    MacroTable m;
    duck.query("SELECT variable, key, scenario, year, value FROM read_csv(" + sql_quote(csv.string()) +
                   ", header = true, types = {'variable': 'VARCHAR', 'key': 'VARCHAR', 'scenario': 'VARCHAR', "
                   "'tenor': 'VARCHAR', 'sector': 'VARCHAR', 'year': 'BIGINT', 'value': 'DOUBLE'}) "
                   "WHERE coalesce(tenor, '') = '' AND coalesce(sector, '') = ''",
               [&](const Chunk& c) {
                   for (std::size_t r = 0; r < c.size(); ++r) {
                       m.set(std::string(c.str(0, r)), std::string(c.str(1, r)), std::string(c.str(2, r)),
                             static_cast<int>(c.i64(3, r)), c.f64(4, r));
                   }
               });
    return m;
}

std::map<std::string, Satellite> load_satellites(Duck& duck, const fs::path& csv) {
    std::map<std::string, Satellite> out;
    duck.query("SELECT portfolio, beta_gdp, beta_unemployment, beta_property, lgd_property_sensitivity FROM read_csv(" +
                   sql_quote(csv.string()) + ", header = true, types = {'portfolio': 'VARCHAR', 'beta_gdp': 'DOUBLE', "
                   "'beta_unemployment': 'DOUBLE', 'beta_property': 'DOUBLE', 'lgd_property_sensitivity': 'DOUBLE'})",
               [&](const Chunk& c) {
                   for (std::size_t r = 0; r < c.size(); ++r) {
                       out[std::string(c.str(0, r))] = {c.f64(1, r), c.f64(2, r), c.f64(3, r), c.f64(4, r)};
                   }
               });
    return out;
}

std::string macro_key(const MacroTable& macro, const std::string& bucket, const ScenarioConfig& cfg) {
    const int y1 = cfg.year_map.at(1);
    if (bucket != "OTHER" && macro.get("real_gdp", bucket, "baseline", y1)) return bucket;
    const std::vector<std::string> fallback = bucket != "OTHER" ? cfg.country_fallback : std::vector<std::string>{"EU"};
    for (const auto& k : fallback) {
        if (macro.get("real_gdp", k, "baseline", y1)) return k;
    }
    throw Error("no macro scenario for country " + bucket);
}

namespace {
double logit(double p) {
    p = std::min(std::max(p, 1e-12), 1 - 1e-12);
    return std::log(p / (1 - p));
}
double expit(double x) { return 1 / (1 + std::exp(-x)); }
}  // namespace

ParamPath project_parameters(const Segment& seg, const Params& p0, const Satellite& b, const MacroTable& macro,
                             const std::string& scenario, const ScenarioConfig& cfg) {
    const std::string key = macro_key(macro, seg.bucket, cfg);
    const auto u0 = macro.get("unemployment_rate", key, "historical", cfg.history_year);
    const std::string prop_var = seg.portfolio == "HH_HOUSE" ? "residential_property_prices" : "commercial_property_prices";
    ParamPath out;
    out[0] = p0;
    double cum_prop = 1.0;
    for (int t = 1; t <= 3; ++t) {
        const int y = cfg.year_map.at(t);
        const auto gdp = macro.get("real_gdp", key, scenario, y);
        if (!gdp) throw Error("macro: no real_gdp for " + key + " " + scenario + " " + std::to_string(y));
        const auto u = macro.get("unemployment_rate", key, scenario, y).value_or(u0.value_or(0.0));
        const double hp = macro.get(prop_var, key, scenario, y).value_or(0.0);
        cum_prop *= 1 + hp / 100;
        const double z = b.beta_gdp * (*gdp - cfg.normal_gdp_growth) +
                         b.beta_unemployment * (u0 ? (u - *u0) : 0.0) + b.beta_property * hp;
        Params p = p0;
        auto pd_like = [&](double v0) { return v0 > 0 ? std::max(expit(logit(v0) + z), cfg.calibration.pd_floor) : 0.0; };
        p.pd12m_s1 = pd_like(p0.pd12m_s1);
        p.pd12m_s2 = pd_like(p0.pd12m_s2);
        p.tr1_2 = pd_like(p0.tr1_2);
        p.tr2_1 = p0.tr2_1 > 0 ? expit(logit(p0.tr2_1) - z) : 0.0;
        p.tr1_2 = std::min(p.tr1_2, 1 - p.pd12m_s1);
        p.tr2_1 = std::min(p.tr2_1, 1 - p.pd12m_s2);
        const double mult = 1 + b.lgd_property_sensitivity * std::max(0.0, 1 - cum_prop);
        p.lgd_s1 = std::min(p0.lgd_s1 * mult, 1.0);
        p.lgd_s2 = std::min(p0.lgd_s2 * mult, 1.0);
        p.lgd_s3 = std::min(p0.lgd_s3 * mult, 1.0);
        p.lrlt_s2 = std::min(p0.lrlt_s2 * mult, 1.0);
        out[static_cast<std::size_t>(t)] = p;
    }
    out[4] = out[3];   // after the horizon: flat
    return out;
}

}  // namespace sora
