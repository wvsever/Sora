#include "sora/dataset.hpp"

#include "sora/collateral.hpp"
#include <functional>
#include <stdexcept>

namespace sora {

namespace fs = std::filesystem;

std::uint32_t Dictionary::hash(std::string_view s) noexcept {
    const auto h = static_cast<std::uint64_t>(std::hash<std::string_view>{}(s));
    return static_cast<std::uint32_t>(h ^ (h >> 32));
}

std::string_view Dictionary::view(std::uint32_t id) const {
    if (id >= size_) throw std::out_of_range("Dictionary::view: id " + std::to_string(id));
    return {arena_.data() + offsets_[id], offsets_[id + 1] - offsets_[id]};
}

// Linear probing. Returns the slot holding `s`, or the empty slot where it belongs.
std::uint64_t* Dictionary::probe(std::string_view s, std::uint32_t h) const noexcept {
    const std::size_t mask = slots_.size() - 1;
    auto* slots = const_cast<std::uint64_t*>(slots_.data());
    for (std::size_t i = h & mask;; i = (i + 1) & mask) {
        const std::uint64_t v = slots[i];
        if (v == 0) return slots + i;
        if (static_cast<std::uint32_t>(v >> 32) == h) {
            const auto id = static_cast<std::uint32_t>(v) - 1;
            if (s == std::string_view(arena_.data() + offsets_[id], offsets_[id + 1] - offsets_[id])) return slots + i;
        }
    }
}

void Dictionary::rehash(std::size_t capacity) {
    std::vector<std::uint64_t> old(capacity, 0);
    old.swap(slots_);
    const std::size_t mask = capacity - 1;
    for (const auto v : old) {
        if (v == 0) continue;
        std::size_t i = static_cast<std::uint32_t>(v >> 32) & mask;
        while (slots_[i] != 0) i = (i + 1) & mask;
        slots_[i] = v;
    }
}

void Dictionary::reserve(std::size_t n, std::size_t bytes) {
    offsets_.reserve(n + 1);
    arena_.reserve(bytes);
    std::size_t cap = 16;
    while (cap * 4 < n * 5) cap *= 2;   // load factor <= 0.8 (the hash tags keep long probe runs cheap)
    if (cap > slots_.size()) rehash(cap);
}

std::size_t Dictionary::memory_bytes() const noexcept {
    return arena_.capacity() + offsets_.capacity() * sizeof(std::uint32_t) + slots_.capacity() * sizeof(std::uint64_t);
}

std::uint32_t Dictionary::intern(std::string_view s) {
    if (slots_.empty()) rehash(16);
    const auto h = hash(s);
    std::uint64_t* slot = probe(s, h);
    if (*slot != 0) return static_cast<std::uint32_t>(*slot) - 1;
    if (arena_.size() + s.size() > UINT32_MAX || size_ >= UINT32_MAX - 1) throw Error("Dictionary: too many keys");
    const auto id = static_cast<std::uint32_t>(size_);
    if (offsets_.empty()) offsets_.push_back(0);
    arena_.append(s);
    offsets_.push_back(static_cast<std::uint32_t>(arena_.size()));
    *slot = (static_cast<std::uint64_t>(h) << 32) | (static_cast<std::uint64_t>(id) + 1);
    ++size_;
    if (size_ * 5 > slots_.size() * 4) rehash(slots_.size() * 2);
    return id;
}

std::optional<std::uint32_t> Dictionary::find(std::string_view s) const {
    if (slots_.empty()) return std::nullopt;
    const std::uint64_t v = *probe(s, hash(s));
    if (v == 0) return std::nullopt;
    return static_cast<std::uint32_t>(v) - 1;
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

// Row count and total key length of a table, so arrays are sized once instead of regrowing (a regrowth
// briefly holds the old and the new copy, which at 100x scale is the peak of the load).
void reserve_keys(Duck& duck, const std::string& source, const char* key, Dictionary& ids, std::size_t& rows) {
    std::size_t bytes = 0;
    duck.query(std::string("SELECT count(*), CAST(coalesce(sum(length(CAST(") + key + " AS VARCHAR))), 0) AS BIGINT) FROM " + source,
               [&](const Chunk& c) {
                   rows = static_cast<std::size_t>(c.i64(0, 0));
                   bytes = static_cast<std::size_t>(c.i64(1, 0));
               });
    ids.reserve(rows, bytes);
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

    std::size_t rows = 0;
    reserve_keys(duck, sim_source(sim_dir, "sim_counterparty"), "counterparty_id", d.counterparty_ids, rows);
    d.counterparties.reserve(rows);
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

    reserve_keys(duck, sim_source(sim_dir, "sim_exposure"), "exposure_id", d.exposure_ids, rows);
    d.exposures.reserve(rows);
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
    load_collateral(duck, d);
    if (d.fx_to_reporting.size() < d.currencies.size()) d.fx_to_reporting.resize(d.currencies.size(), 0);
    return d;
}

}  // namespace sora
