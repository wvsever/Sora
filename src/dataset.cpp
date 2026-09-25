#include "sora/dataset.hpp"

namespace sora {

namespace fs = std::filesystem;

std::uint32_t Dictionary::intern(std::string_view s) {
    if (auto it = index_.find(s); it != index_.end()) return it->second;
    const auto id = static_cast<std::uint32_t>(names_.size());
    names_.emplace_back(s);
    index_.emplace(names_.back(), id);
    return id;
}

std::optional<std::uint32_t> Dictionary::find(std::string_view s) const {
    if (auto it = index_.find(s); it != index_.end()) return it->second;
    return std::nullopt;
}

namespace {
bool has_ext(const fs::path& dir, std::string_view ext) {
    if (!fs::is_directory(dir)) return false;
    for (const auto& e : fs::recursive_directory_iterator(dir)) {
        if (e.is_regular_file() && e.path().extension() == ext) return true;
    }
    return false;
}
}  // namespace

bool sim_table_exists(const fs::path& sim_dir, std::string_view table) {
    const auto dir = sim_dir / table;
    return has_ext(dir, ".parquet") || has_ext(dir, ".csv");
}

std::string sim_source(const fs::path& sim_dir, std::string_view table) {
    const auto dir = sim_dir / table;
    if (has_ext(dir, ".parquet")) {
        return "read_parquet(" + sql_quote((dir / "**" / "*.parquet").string()) +
               ", hive_partitioning = false, union_by_name = true)";
    }
    if (has_ext(dir, ".csv")) {
        return "read_csv(" + sql_quote((dir / "**" / "*.csv").string()) +
               ", header = true, all_varchar = true, hive_partitioning = false, union_by_name = true)";
    }
    throw Error("SIM table " + std::string(table) + " not found in " + sim_dir.string());
}

namespace {

Manifest load_manifest(Duck& duck, const fs::path& sim_dir) {
    const auto path = sim_dir / "sim_manifest.json";
    if (!fs::is_regular_file(path)) throw Error("missing " + path.string());
    Manifest m;
    duck.query("SELECT CAST(sim_version AS VARCHAR), CAST(CAST(reference_date AS DATE) AS VARCHAR), "
               "CAST(reference_date AS DATE), CAST(reporting_currency AS VARCHAR), "
               "CAST(reporting_entity_id AS VARCHAR), CAST(mapping_release AS VARCHAR) "
               "FROM read_json(" + sql_quote(path.string()) + ", union_by_name = true)",
               [&](const Chunk& c) {
                   m.sim_version = c.str_or(0, 0);
                   m.reference_date = c.str_or(1, 0);
                   m.reference_day = c.date(2, 0);
                   m.reporting_currency = c.str_or(3, 0);
                   m.reporting_entity_id = c.str_or(4, 0);
                   m.mapping_release = c.str_or(5, 0);
               });
    if (m.reference_date.empty() || m.reporting_currency.empty()) throw Error("incomplete sim_manifest.json");
    return m;
}

}  // namespace

Dataset load_dataset(Duck& duck, const fs::path& sim_dir) {
    Dataset d;
    d.sim_dir = sim_dir;
    d.manifest = load_manifest(duck, sim_dir);

    // FX at the reference date. The reporting currency is 1 by definition.
    d.currencies.intern(d.manifest.reporting_currency);
    d.fx_to_reporting.assign(1, 1'000'000'000);
    duck.query("SELECT CAST(currency AS VARCHAR), CAST(rate_to_reporting AS DECIMAL(18,9)) FROM " +
                   sim_source(sim_dir, "sim_fx_rate") + " WHERE CAST(rate_date AS DATE) = DATE " +
                   sql_quote(d.manifest.reference_date) + " ORDER BY 1",
               [&](const Chunk& c) {
                   for (std::size_t r = 0; r < c.size(); ++r) {
                       const auto id = d.currencies.intern(c.str(0, r));
                       if (d.fx_to_reporting.size() <= id) d.fx_to_reporting.resize(id + 1, 0);
                       if (d.currencies.at(id) != d.manifest.reporting_currency) d.fx_to_reporting[id] = c.nano(1, r);
                   }
               });

    duck.query("SELECT CAST(counterparty_id AS VARCHAR), CAST(country_of_residence AS VARCHAR), "
               "CAST(eba_sector AS VARCHAR), CAST(is_sme AS BOOLEAN) FROM " +
                   sim_source(sim_dir, "sim_counterparty") + " ORDER BY 1",
               [&](const Chunk& c) {
                   for (std::size_t r = 0; r < c.size(); ++r) {
                       const auto id = d.counterparty_ids.intern(c.str(0, r));
                       if (id != d.counterparties.size()) throw Error("duplicate counterparty_id " + std::string(c.str(0, r)));
                       Counterparty cp;
                       cp.country = d.countries.intern(c.str(1, r));
                       cp.sector = parse_eba_sector(c.str(2, r));
                       cp.is_sme = c.flag(3, r);
                       d.counterparties.push_back(cp);
                   }
               });

    duck.query(
        "SELECT CAST(exposure_id AS VARCHAR), CAST(entity_id AS VARCHAR), CAST(counterparty_id AS VARCHAR), "
        "CAST(exposure_type AS VARCHAR), CAST(currency AS VARCHAR), CAST(measurement_category AS VARCHAR), "
        "CAST(stage AS VARCHAR), CAST(gross_carrying_amount AS DECIMAL(18,2)), "
        "CAST(off_balance_amount AS DECIMAL(18,2)), CAST(loss_allowance AS DECIMAL(18,2)), "
        "CAST(household_purpose AS VARCHAR), CAST(is_cre AS BOOLEAN), CAST(is_intragroup AS BOOLEAN), "
        "CAST(country_of_risk AS VARCHAR), CAST(maturity_date AS DATE) FROM " + sim_source(sim_dir, "sim_exposure") + " ORDER BY 1",
        [&](const Chunk& c) {
            for (std::size_t r = 0; r < c.size(); ++r) {
                Exposure e;
                e.id = d.exposure_ids.intern(c.str(0, r));
                if (e.id != d.exposures.size()) throw Error("duplicate exposure_id " + std::string(c.str(0, r)));
                e.entity = d.entities.intern(c.str(1, r));
                const auto cp = d.counterparty_ids.find(c.str(2, r));
                if (!cp) throw Error("exposure " + std::string(c.str(0, r)) + ": unknown counterparty " + std::string(c.str(2, r)));
                e.counterparty = *cp;
                e.type = parse_exposure_type(c.str(3, r));
                e.currency = d.currencies.intern(c.str(4, r));
                e.measurement = parse_measurement(c.str(5, r));
                e.stage = parse_stage(c.str(6, r));
                e.has_gca = c.valid(7, r);
                e.gca = e.has_gca ? c.cents(7, r) : 0;
                e.off_balance = c.valid(8, r) ? c.cents(8, r) : 0;
                e.allowance = c.valid(9, r) ? c.cents(9, r) : 0;
                e.purpose = parse_household_purpose(c.str_or(10, r));
                e.is_cre = c.flag(11, r);
                e.intragroup = c.flag(12, r) == Flag::True;
                e.country_of_risk = c.valid(13, r) ? d.countries.intern(c.str(13, r)) : kNone;
                e.has_maturity = c.valid(14, r);
                e.maturity = e.has_maturity ? c.date(14, r) : 0;
                d.exposures.push_back(e);
            }
        });
    if (d.fx_to_reporting.size() < d.currencies.size()) d.fx_to_reporting.resize(d.currencies.size(), 0);
    return d;
}

}  // namespace sora
