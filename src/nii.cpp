#include "sora/nii.hpp"

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <fstream>
#include <ostream>
#include <thread>

#include "sora/json_text.hpp"

namespace sora {

namespace fs = std::filesystem;

namespace nii {

namespace {

// Civil calendar <-> days since 1970-01-01 (H. Hinnant's algorithms, proleptic Gregorian).
Date days_from_civil(long long y, unsigned m, unsigned d) {
    y -= m <= 2;
    const long long era = (y >= 0 ? y : y - 399) / 400;
    const auto yoe = static_cast<unsigned>(y - era * 400);
    const unsigned doy = (153 * (m > 2 ? m - 3 : m + 9) + 2) / 5 + d - 1;
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return static_cast<Date>(era * 146097 + static_cast<long long>(doe) - 719468);
}

void civil_from_days(Date z0, long long& y, unsigned& m, unsigned& d) {
    const long long z = static_cast<long long>(z0) + 719468;
    const long long era = (z >= 0 ? z : z - 146096) / 146097;
    const auto doe = static_cast<unsigned>(z - era * 146097);
    const unsigned yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    const unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    const unsigned mp = (5 * doy + 2) / 153;
    d = doy - (153 * mp + 2) / 5 + 1;
    m = mp < 10 ? mp + 3 : mp - 9;
    y = static_cast<long long>(yoe) + era * 400 + (m <= 2);
}

unsigned days_in_month(long long y, unsigned m) {
    static constexpr unsigned kDays[12] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    const bool leap = (y % 4 == 0 && y % 100 != 0) || y % 400 == 0;
    return m == 2 && leap ? 29 : kDays[m - 1];
}

constexpr TemplateRow kRows[] = {
    {1, true, "Loans and advances - Central banks", 0.0},
    {2, true, "Loans and advances - General governments", 1.0},
    {3, true, "Loans and advances - Credit Institutions and other financial corporations", 0.5},
    {4, true, "Loans and advances - Non-financial corporations", 0.15},
    {5, true, "Loans and advances - Households - Residential mortgage loans", 0.15},
    {6, true, "Loans and advances - Households - Credit for consumption and Other", 0.15},
    {7, true, "Debt securities - Central banks", 0.0},
    {8, true, "Debt securities - General governments", 1.0},
    {9, true, "Debt securities - Credit Institutions and other financial corporations", 0.5},
    {10, true, "Debt securities - Non-financial corporations", 0.15},
    {11, true, "Other assets", 0.5},
    {22, false, "Deposits (excl. repo) - Central banks", 0.0},
    {23, false, "Deposits (excl. repo) - General governments - sight", 0.2},
    {24, false, "Deposits (excl. repo) - Credit Institutions and other financial corporations - sight", 1.0},
    {26, false, "Deposits (excl. repo) - Non-financial corporations Other - sight", 0.2},
    {28, false, "Deposits (excl. repo) - Households Other - sight", 0.1},
    {29, false, "Deposits (excl. repo) - General governments / Non-financial Corporations / Households - term", 0.5},
    {30, false, "Deposits (excl. repo) - Credit Institutions and other financial corporations - term", 1.0},
    {32, false, "Debt securities issued - Certificates of deposits", 0.2},
    {33, false, "Debt securities issued - Asset-backed securities and Covered bonds", 0.75},
    {34, false, "Debt securities issued - Other debt securities and Hybrid contract", 1.0},
};

constexpr double kDaysPerYear = 365.25;   // original term in days -> tenor in years
constexpr const char* kScenarioNames[3] = {"actual", "baseline", "adverse"};

int asset_row(std::string_view type, std::string_view sector, std::string_view purpose) {
    const bool securities = type == "debt_security";
    if (sector == "household") return securities ? 11 : purpose == "house_purchase" ? 5 : 6;
    int base = 0;
    if (sector == "central_bank") base = 1;
    else if (sector == "general_government") base = 2;
    else if (sector == "credit_institution" || sector == "other_financial") base = 3;
    else if (sector == "non_financial_corporation") base = 4;
    else throw Error("nii: unknown eba_sector " + std::string(sector));
    return securities ? base + 6 : base;
}

int deposit_row(std::string_view sector, bool sight) {
    if (sector == "central_bank") return 22;
    const bool fin = sector == "credit_institution" || sector == "other_financial";
    if (!sight) return fin ? 30 : 29;
    if (fin) return 24;
    if (sector == "general_government") return 23;
    if (sector == "non_financial_corporation") return 26;
    if (sector == "household") return 28;
    throw Error("nii: unknown eba_sector " + std::string(sector));
}

std::string money(double x) {
    char buf[64];
    std::snprintf(buf, sizeof buf, "%.2f", x);
    return buf;
}

std::string rate(double x) {
    char buf[64];
    std::snprintf(buf, sizeof buf, "%.9f", x);
    return buf;
}

std::optional<Date> opt_date(const Chunk& c, std::size_t col, std::size_t r) {
    if (!c.valid(col, r)) return std::nullopt;
    return c.date(col, r);
}

// Dense ids for currency and country codes of the positions (keeps Position small).
struct Interner {
    std::vector<std::string>& names;
    std::map<std::string, std::uint16_t, std::less<>> ids;
    std::uint16_t id(std::string_view s) {
        const auto it = ids.find(s);
        if (it != ids.end()) return it->second;
        if (names.size() >= 65535) throw Error("nii: too many distinct codes");
        const auto k = static_cast<std::uint16_t>(names.size());
        names.emplace_back(s);
        ids.emplace(std::string(s), k);
        return k;
    }
};

// The FX CTE of the reference implementation: reference-date rates, the reporting currency at 1.
std::string fx_cte(const Dataset& d) {
    return "WITH fx AS (SELECT CAST(currency AS VARCHAR) AS currency, CAST(CAST(rate_to_reporting AS DECIMAL(18,9)) AS DOUBLE) AS r "
           "FROM " + sim_source(d.sim_dir, "sim_fx_rate") + " WHERE CAST(rate_date AS DATE) = DATE " +
           sql_quote(d.manifest.reference_date) + " UNION SELECT " + sql_quote(d.manifest.reporting_currency) + ", 1.0) ";
}

}  // namespace

Date add_months(Date day, int months) {
    long long y = 0;
    unsigned m = 0, d = 0;
    civil_from_days(day, y, m, d);
    const long long total = y * 12 + static_cast<long long>(m) - 1 + months;
    const long long ny = total >= 0 ? total / 12 : (total - 11) / 12;   // floor division
    const auto nm = static_cast<unsigned>(total - ny * 12) + 1;
    return days_from_civil(ny, nm, std::min(d, days_in_month(ny, nm)));
}

double interp(const Curve& c, double t) {
    if (c.empty()) throw Error("nii: empty rate curve");
    if (t <= c.front().first) return c.front().second;
    for (std::size_t i = 0; i + 1 < c.size(); ++i) {
        if (t < c[i + 1].first) {
            const auto [t0, r0] = c[i];
            const auto [t1, r1] = c[i + 1];
            return r0 + (r1 - r0) * (t - t0) / (t1 - t0);
        }
    }
    return c.back().second;
}

const TemplateRow& template_row(int row) {
    for (const auto& r : kRows)
        if (r.row == row) return r;
    throw Error("nii: no template row " + std::to_string(row));
}

const Curve& RateScenario::swap(const std::string& key, const std::string& scenario, int year) const {
    const auto it = swaps.find({key, scenario, year});
    if (it == swaps.end())
        throw Error("nii: the macro scenario has no swap_rate curve for " + key + " " + scenario + " " + std::to_string(year));
    return it->second;
}

std::string RateScenario::swap_key(const std::string& currency) const {
    return swaps.count({currency, "starting_point", history_year}) ? currency : std::string("RoW");
}

double RateScenario::rf0(const std::string& currency, double tenor) const {
    const auto it = bank.find(currency);
    if (it != bank.end()) return interp(it->second, tenor);
    return interp(swap(swap_key(currency), "starting_point", history_year), tenor);
}

double RateScenario::delta(const std::string& currency, double tenor, int scenario, int year) const {
    const auto k = swap_key(currency);
    return interp(swap(k, kScenarioNames[scenario + 1], years[static_cast<std::size_t>(year - 1)]), tenor) -
           interp(swap(k, "starting_point", history_year), tenor);
}

PositionInterest project_position(const Position& p, const std::array<Date, 4>& bounds) {
    PositionInterest out{};
    const double V = p.volume;
    if (!p.performing) {   // NPE: EIR on the exposure net of provisions, not split (MN paras 378, 407)
        const double interest = p.net_volume * p.eir;
        for (auto& slot : out) slot[0] = interest;
        return out;
    }
    {
        const double i_ref = V * p.ref0, i_mar = V * p.margin0;
        out[0] = {i_ref + i_mar, i_ref, i_mar};
    }
    const Date ref_day = bounds[0];
    for (std::size_t sc = 0; sc < 2; ++sc) {
        if (p.sight) {   // reprice immediately, every year (paras 369, 393, 397)
            for (std::size_t y = 0; y < 3; ++y) {
                const double margin = p.margin0 + p.shock[sc][y];
                double ref_rate = p.ref0 + p.beta * p.delta[sc][y];
                if (p.floor_zero) ref_rate = std::max(ref_rate, -margin);
                const double i_ref = V * ref_rate, i_mar = V * margin;
                out[1 + sc * 3 + y] = {i_ref + i_mar, i_ref, i_mar};
            }
            continue;
        }
        double ref_rate = p.ref0, margin = p.margin0;
        // A position past its maturity at the reference date is replaced at once (first day of year 1).
        std::optional<Date> next_mat;
        if (p.term) next_mat = std::max(*p.maturity, ref_day);
        Date anchor = 0;
        int k = 0;
        std::optional<Date> next_reset;
        if (p.floating) {
            if (p.next_reset && *p.next_reset > ref_day) {
                anchor = *p.next_reset;
                k = 0;
            } else {
                const auto start = p.next_reset ? p.next_reset : p.origination;
                if (!start) {
                    anchor = ref_day;
                    k = 1;
                } else {
                    anchor = *start;
                    k = 1;
                    while (add_months(anchor, k * p.freq) <= ref_day) ++k;
                }
            }
            next_reset = add_months(anchor, k * p.freq);
        }
        for (std::size_t y = 0; y < 3; ++y) {
            Date prev = bounds[y];
            const Date end = bounds[y + 1];
            double acc_r = 0.0, acc_m = 0.0;
            for (;;) {
                Date e = end;
                if (next_mat) e = std::min(e, *next_mat);
                if (next_reset) e = std::min(e, *next_reset);
                if (e >= end) break;
                acc_r += ref_rate * static_cast<double>(e - prev);
                acc_m += margin * static_cast<double>(e - prev);
                prev = e;
                ref_rate = p.ref0 + p.delta[sc][y];
                if (next_mat && e == *next_mat) {   // replacement (before a reset on the same day)
                    margin = p.margin_new + p.shock[sc][y];
                    next_mat = e + *p.term;
                    if (p.floating) {
                        anchor = e;
                        k = 1;
                        next_reset = add_months(anchor, p.freq);
                    }
                } else {   // reset of a floating reference rate
                    ++k;
                    next_reset = add_months(anchor, k * p.freq);
                }
            }
            acc_r += ref_rate * static_cast<double>(end - prev);
            acc_m += margin * static_cast<double>(end - prev);
            const auto days = static_cast<double>(end - bounds[y]);
            const double i_ref = V * (acc_r / days), i_mar = V * (acc_m / days);
            out[1 + sc * 3 + y] = {i_ref + i_mar, i_ref, i_mar};
        }
    }
    return out;
}

}  // namespace nii

using namespace nii;

namespace {

void load_positions(Duck& duck, const Dataset& d, Date horizon_end, std::vector<Position>& out, NiiResult& r) {
    Interner currencies{r.currencies, {}}, countries{r.countries, {}};
    std::size_t rows = 0;   // upper bound of the positions: reserve once (a regrowth would hold two copies)
    duck.query("SELECT (SELECT count(*) FROM " + sim_source(d.sim_dir, "sim_exposure") + ") + (SELECT count(*) FROM " +
                   sim_source(d.sim_dir, "sim_deposit") + ") + (SELECT count(*) FROM " + sim_source(d.sim_dir, "sim_debt_issued") + ")",
               [&](const Chunk& c) { rows = static_cast<std::size_t>(c.i64(0, 0)); });
    out.reserve(rows);
    auto base = [&](Position& p, bool has_rate, double eir, std::string_view rate_type, std::optional<Date> nrd,
                    std::optional<std::int64_t> freq) {
        if (!has_rate) {
            ++r.missing_rate;
            eir = 0.0;
        }
        p.eir = eir;
        p.next_reset = nrd;
        p.floating = rate_type == "floating" || (rate_type == "mixed" && nrd && *nrd < horizon_end);
        if (p.floating) {
            if (!freq || *freq <= 0) {
                ++r.floating_without_frequency;
                freq = 1;
            }
            p.freq = static_cast<int>(*freq);
        }
    };
    const std::string fx = fx_cte(d);
    // Assets: on-balance loans, debt securities and finance leases, all accounting categories except held for trading
    // (MN para 348), intragroup excluded (para 403).
    duck.query(fx + "SELECT CAST(e.exposure_type AS VARCHAR), CAST(c.eba_sector AS VARCHAR), CAST(e.household_purpose AS VARCHAR), "
               "CAST(c.country_of_residence AS VARCHAR), CAST(e.currency AS VARCHAR), fx.r, CAST(e.stage AS VARCHAR), "
               "CAST(CAST(e.gross_carrying_amount AS DECIMAL(18,2)) AS DOUBLE), "
               "CAST(CAST(coalesce(e.loss_allowance, 0) AS DECIMAL(18,2)) AS DOUBLE), "
               "CAST(CAST(e.current_interest_rate AS DECIMAL(18,9)) AS DOUBLE), CAST(e.interest_rate_type AS VARCHAR), "
               "CAST(e.origination_date AS DATE), CAST(e.maturity_date AS DATE), CAST(e.next_repricing_date AS DATE), "
               "CAST(e.repricing_frequency_months AS BIGINT) "
               "FROM " + sim_source(d.sim_dir, "sim_exposure") + " e JOIN " + sim_source(d.sim_dir, "sim_counterparty") +
               " c ON CAST(c.counterparty_id AS VARCHAR) = CAST(e.counterparty_id AS VARCHAR) "
               "JOIN fx ON fx.currency = CAST(e.currency AS VARCHAR) "
               "WHERE CAST(e.exposure_type AS VARCHAR) IN ('loan', 'debt_security', 'finance_lease') "
               "AND CAST(e.measurement_category AS VARCHAR) <> 'held_for_trading' "
               "AND CAST(e.gross_carrying_amount AS DECIMAL(18,2)) > 0 AND NOT coalesce(CAST(e.is_intragroup AS BOOLEAN), false) "
               "ORDER BY CAST(e.exposure_id AS VARCHAR)",
               [&](const Chunk& c) {
                   for (std::size_t i = 0; i < c.size(); ++i) {
                       Position p;
                       p.row = asset_row(c.str(0, i), c.str(1, i), c.str_or(2, i));
                       p.country = countries.id(c.str(3, i));
                       p.currency = currencies.id(c.str(4, i));
                       const double fxr = c.f64(5, i);
                       p.volume = c.f64(7, i) * fxr;
                       p.origination = opt_date(c, 11, i);
                       p.maturity = opt_date(c, 12, i);
                       base(p, c.valid(9, i), c.valid(9, i) ? c.f64(9, i) : 0.0, c.str_or(10, i), opt_date(c, 13, i),
                            c.valid(14, i) ? std::optional<std::int64_t>(c.i64(14, i)) : std::nullopt);
                       const auto stage = c.str(6, i);
                       if (stage == "stage3" || stage == "poci") {
                           p.performing = false;
                           p.provisions = c.f64(8, i) * fxr;
                           p.net_volume = std::max(p.volume - p.provisions, 0.0);
                       }
                       out.push_back(std::move(p));
                   }
               });
    // Deposits: intragroup excluded; the location of the activity is the booking entity's country (para 401).
    duck.query(fx + "SELECT CAST(x.deposit_type AS VARCHAR), CAST(c.eba_sector AS VARCHAR), CAST(n.country AS VARCHAR), "
               "CAST(x.currency AS VARCHAR), fx.r, CAST(CAST(x.amount AS DECIMAL(18,2)) AS DOUBLE), "
               "CAST(CAST(x.current_interest_rate AS DECIMAL(18,9)) AS DOUBLE), CAST(x.interest_rate_type AS VARCHAR), "
               "CAST(x.origination_date AS DATE), CAST(x.maturity_date AS DATE), CAST(x.next_repricing_date AS DATE), "
               "CAST(x.repricing_frequency_months AS BIGINT) "
               "FROM " + sim_source(d.sim_dir, "sim_deposit") + " x JOIN " + sim_source(d.sim_dir, "sim_counterparty") +
               " c ON CAST(c.counterparty_id AS VARCHAR) = CAST(x.counterparty_id AS VARCHAR) JOIN " +
               sim_source(d.sim_dir, "sim_entity") + " n ON CAST(n.entity_id AS VARCHAR) = CAST(x.entity_id AS VARCHAR) "
               "JOIN fx ON fx.currency = CAST(x.currency AS VARCHAR) "
               "WHERE CAST(x.amount AS DECIMAL(18,2)) > 0 AND NOT coalesce(CAST(x.is_intragroup AS BOOLEAN), false) "
               "ORDER BY CAST(x.deposit_id AS VARCHAR)",
               [&](const Chunk& c) {
                   for (std::size_t i = 0; i < c.size(); ++i) {
                       Position p;
                       const auto type = c.str(0, i);
                       const auto sector = c.str(1, i);
                       p.sight = type == "current" || type == "savings" || type == "call";
                       p.row = deposit_row(sector, p.sight);
                       p.country = countries.id(c.str(2, i));
                       p.currency = currencies.id(c.str(3, i));
                       p.volume = c.f64(5, i) * c.f64(4, i);
                       p.origination = opt_date(c, 8, i);
                       p.maturity = opt_date(c, 9, i);
                       base(p, c.valid(6, i), c.valid(6, i) ? c.f64(6, i) : 0.0, c.str_or(7, i), opt_date(c, 10, i),
                            c.valid(11, i) ? std::optional<std::int64_t>(c.i64(11, i)) : std::nullopt);
                       if (p.sight) {
                           p.beta = sector == "household" ? 0.5 : sector == "non_financial_corporation" ? 0.75 : 1.0;
                           p.floor_zero = sector == "household";
                       }
                       out.push_back(std::move(p));
                   }
               });
    // Debt securities issued; Additional Tier 1 instruments are excluded (para 398).
    duck.query(fx + "SELECT CAST(x.instrument_type AS VARCHAR), CAST(n.country AS VARCHAR), CAST(x.currency AS VARCHAR), fx.r, "
               "CAST(CAST(x.carrying_amount AS DECIMAL(18,2)) AS DOUBLE), CAST(CAST(x.current_interest_rate AS DECIMAL(18,9)) AS DOUBLE), "
               "CAST(x.interest_rate_type AS VARCHAR), CAST(x.issue_date AS DATE), CAST(x.maturity_date AS DATE), "
               "CAST(x.next_repricing_date AS DATE), CAST(x.repricing_frequency_months AS BIGINT) "
               "FROM " + sim_source(d.sim_dir, "sim_debt_issued") + " x JOIN " + sim_source(d.sim_dir, "sim_entity") +
               " n ON CAST(n.entity_id AS VARCHAR) = CAST(x.entity_id AS VARCHAR) "
               "JOIN fx ON fx.currency = CAST(x.currency AS VARCHAR) "
               "WHERE CAST(x.carrying_amount AS DECIMAL(18,2)) > 0 AND CAST(x.instrument_type AS VARCHAR) <> 'additional_tier1' "
               "ORDER BY CAST(x.debt_id AS VARCHAR)",
               [&](const Chunk& c) {
                   for (std::size_t i = 0; i < c.size(); ++i) {
                       Position p;
                       const auto type = c.str(0, i);
                       p.row = type == "certificate_of_deposit" ? 32 : (type == "covered_bond" || type == "asset_backed") ? 33 : 34;
                       p.country = countries.id(c.str(1, i));
                       p.currency = currencies.id(c.str(2, i));
                       p.volume = c.f64(4, i) * c.f64(3, i);
                       p.origination = opt_date(c, 7, i);
                       p.maturity = opt_date(c, 8, i);
                       base(p, c.valid(5, i), c.valid(5, i) ? c.f64(5, i) : 0.0, c.str_or(6, i), opt_date(c, 9, i),
                            c.valid(10, i) ? std::optional<std::int64_t>(c.i64(10, i)) : std::nullopt);
                       out.push_back(std::move(p));
                   }
               });
}

RateScenario load_rates(Duck& duck, const Dataset& d, const ScenarioConfig& cfg) {
    RateScenario rs;
    rs.history_year = cfg.history_year;
    for (std::size_t y = 0; y < 3; ++y) rs.years[y] = cfg.year_map.at(static_cast<int>(y + 1));
    if (sim_table_exists(d.sim_dir, "sim_rate_curve")) {
        duck.query("SELECT CAST(currency AS VARCHAR), CAST(tenor_months AS BIGINT), CAST(CAST(rate AS DECIMAL(18,9)) AS DOUBLE) "
                   "FROM " + sim_source(d.sim_dir, "sim_rate_curve") + " WHERE CAST(curve_type AS VARCHAR) = 'risk_free' ORDER BY 1, 2",
                   [&](const Chunk& c) {
                       for (std::size_t i = 0; i < c.size(); ++i)
                           rs.bank[std::string(c.str(0, i))].emplace_back(static_cast<double>(c.i64(1, i)) / 12.0, c.f64(2, i));
                   });
    }
    // Swap curves of the macro scenario (percent), by currency key, scenario and year; tenors 1M .. 30Y.
    std::map<std::tuple<std::string, std::string, int>, std::vector<std::pair<long long, double>>> raw;
    duck.query("SELECT key, scenario, year, tenor, value FROM read_csv(" + sql_quote(cfg.macro_path.string()) +
                   ", header = true, types = {'variable': 'VARCHAR', 'key': 'VARCHAR', 'scenario': 'VARCHAR', "
                   "'tenor': 'VARCHAR', 'sector': 'VARCHAR', 'year': 'BIGINT', 'value': 'DOUBLE'}) "
                   "WHERE variable = 'swap_rate' AND coalesce(tenor, '') <> ''",
               [&](const Chunk& c) {
                   for (std::size_t i = 0; i < c.size(); ++i) {
                       const std::string label(c.str(3, i));
                       if (label.size() < 2 || (label.back() != 'M' && label.back() != 'Y'))
                           throw Error("nii: unknown swap tenor " + label);
                       const long long months = std::stoll(label.substr(0, label.size() - 1)) * (label.back() == 'Y' ? 12 : 1);
                       raw[{std::string(c.str(0, i)), std::string(c.str(1, i)), static_cast<int>(c.i64(2, i))}]
                           .emplace_back(months, c.f64(4, i));
                   }
               });
    for (auto& [key, pts] : raw) {
        std::sort(pts.begin(), pts.end());
        auto& curve = rs.swaps[key];
        for (const auto& [m, v] : pts) curve.emplace_back(static_cast<double>(m) / 12.0, v / 100.0);
    }
    if (!rs.swaps.count({"RoW", "starting_point", rs.history_year}))
        throw Error("nii: the macro scenario has no swap_rate starting point (RoW, " + std::to_string(rs.history_year) + ")");
    return rs;
}

}  // namespace

NiiResult project_nii(Duck& duck, const Dataset& d, const Segmentation& s, const Projection& projection,
                      const MacroTable& macro, const ScenarioConfig& cfg, unsigned workers) {
    NiiResult r;
    r.own_rating = cfg.nii.own_rating;
    r.idiosyncratic_shock = cfg.nii.own_rating.empty() ? 0.0 : *idiosyncratic_shock_bps(cfg.nii.own_rating) / 10000.0;
    r.new_business_months = cfg.nii.new_business_months;
    const Date ref_day = d.manifest.reference_day;
    std::array<Date, 4> bounds{};
    for (int y = 0; y < 4; ++y) bounds[static_cast<std::size_t>(y)] = add_months(ref_day, 12 * y);

    std::vector<Position> pos;
    load_positions(duck, d, bounds[3], pos, r);
    const RateScenario rates = load_rates(duck, d, cfg);

    // Sovereign spread (Box 23): long-term rate of the country minus the 10Y swap rate of the currency.
    auto country_key = [&](const std::string& country) {
        if (macro.get("long_term_rate", country, "baseline", rates.years[0])) return country;
        for (const auto& k : cfg.country_fallback)
            if (macro.get("long_term_rate", k, "baseline", rates.years[0])) return k;
        throw Error("nii: no long_term_rate for " + country + " or the country_fallback keys");
    };
    auto sov_spread = [&](const std::string& key, const std::string& currency, const std::string& scen, int year) {
        const auto ltr = macro.get("long_term_rate", key, scen, year);
        if (!ltr) throw Error("nii: no long_term_rate for " + key + " " + scen + " " + std::to_string(year));
        return *ltr / 100.0 - interp(rates.swap(rates.swap_key(currency), scen, year), 10.0);
    };

    // Prepare: tenor, starting-point split, scenario deltas and margin paths; new business margins per cell.
    const Date nb_start = add_months(ref_day, -cfg.nii.new_business_months);
    std::map<std::tuple<int, std::string, std::string>, std::array<double, 4>> nb;   // [nb V, nb V*m, V, V*m]
    for (auto& p : pos) {
        if (!p.sight && p.maturity) {
            Date term = *p.maturity - (p.origination ? *p.origination : ref_day);
            if (term <= 0) {
                ++r.term_fallback;
                term = 365;
            }
            p.term = term;
        }
        if (p.sight) p.tenor = 1 / 12.0;
        else if (p.floating) p.tenor = static_cast<double>(p.freq) / 12.0;
        else p.tenor = p.term ? static_cast<double>(*p.term) / kDaysPerYear : 1 / 12.0;
        if (!p.performing) continue;
        const auto& currency = r.currencies[p.currency];
        p.ref0 = rates.rf0(currency, p.tenor);
        p.margin0 = p.eir - p.ref0;
        const auto ck = country_key(r.countries[p.country]);
        const auto& tr = template_row(p.row);
        const double s0 = sov_spread(ck, currency, "starting_point", cfg.history_year);
        for (int sc = 0; sc < 2; ++sc)
            for (int y = 1; y <= 3; ++y) {
                const double ds = sov_spread(ck, currency, kScenarioNames[sc + 1], rates.years[static_cast<std::size_t>(y - 1)]) - s0;
                const double floor = tr.asset ? 0.0 : (sc == 1 ? r.idiosyncratic_shock : 0.0);
                p.shock[static_cast<std::size_t>(sc)][static_cast<std::size_t>(y - 1)] = tr.factor * std::max(ds, floor);
                p.delta[static_cast<std::size_t>(sc)][static_cast<std::size_t>(y - 1)] = rates.delta(currency, p.tenor, sc, y);
            }
        if (!p.sight) {
            auto& cell = nb[{p.row, currency, p.floating ? "floating" : "fixed"}];
            if (p.origination && nb_start <= *p.origination && *p.origination <= ref_day) {
                cell[0] += p.volume;
                cell[1] += p.volume * p.margin0;
            }
            cell[2] += p.volume;
            cell[3] += p.volume * p.margin0;
        }
    }
    for (const auto& [key, v] : nb) {
        double m = 0.0;
        if (v[0] > 0) {
            m = v[1] / v[0];
        } else {
            ++r.new_business_fallback_cells;   // no new business: the cell's stock margin (MN para 409)
            m = v[2] > 0 ? v[3] / v[2] : 0.0;
        }
        r.margin_new_business[key] = m;
    }
    for (auto& p : pos)
        if (p.performing && !p.sight)
            p.margin_new = r.margin_new_business.at({p.row, r.currencies[p.currency], p.floating ? "floating" : "fixed"});

    // Project in parallel, one slot per position.
    std::vector<PositionInterest> res(pos.size());
    if (workers == 0) workers = std::max(1U, std::thread::hardware_concurrency());
    workers = static_cast<unsigned>(std::min<std::size_t>(workers, std::max<std::size_t>(pos.size(), 1)));
    std::atomic<std::size_t> next{0};
    constexpr std::size_t kBlock = 1024;
    auto worker = [&]() {
        for (;;) {
            const std::size_t b = next.fetch_add(kBlock);
            if (b >= pos.size()) return;
            const std::size_t e = std::min(pos.size(), b + kBlock);
            for (std::size_t i = b; i < e; ++i) res[i] = project_position(pos[i], bounds);
        }
    };
    if (workers <= 1) {
        worker();
    } else {
        std::vector<std::thread> pool;
        pool.reserve(workers);
        for (unsigned w = 0; w < workers; ++w) pool.emplace_back(worker);
        for (auto& t : pool) t.join();
    }

    // Aggregate serially in position order (the reference's order).
    for (std::size_t i = 0; i < pos.size(); ++i) {
        const auto& p = pos[i];
        const std::string rate_type = p.floating ? "floating" : "fixed";
        const std::string status = p.performing ? "performing" : "non_performing";
        const bool asset = template_row(p.row).asset;
        for (std::size_t slot = 0; slot < 7; ++slot) {
            const int scen = slot == 0 ? 0 : slot <= 3 ? 1 : 2;
            const int year = slot == 0 ? 0 : static_cast<int>((slot - 1) % 3 + 1);
            auto& c = r.cells[{scen, year, p.row, r.currencies[p.currency], rate_type, status}];
            c.positions += 1;
            c.volume += p.volume;
            if (!p.performing) {
                c.provisions += p.provisions;
                c.net_volume += p.net_volume;
            } else {
                c.interest_reference += res[i][slot][1];
                c.interest_margin += res[i][slot][2];
            }
            c.interest += res[i][slot][0];
            (asset ? r.income : r.expense)[slot] += res[i][slot][0];
        }
        if (asset) {
            ++r.assets;
            if (p.performing) {
                r.volume_performing += p.volume;
            } else {
                ++r.non_performing;
                r.volume_non_performing += p.volume;
                r.provisions_non_performing += p.provisions;
            }
        } else if (p.row <= 30) {
            ++r.deposits;
            r.sight_deposits += p.sight ? 1 : 0;
        } else {
            ++r.debt_issued;
        }
    }

    // Box 22 (paras 404-405): the adverse NII is capped at the starting point less its share lost to the increase
    // of NPE provisions in the credit projection (S3 + POCI provision stock, on-balance scope of the credit module).
    std::vector<double> seg_npe(s.segments.size(), 0.0);
    std::vector<double> seg_s3(s.segments.size(), 0.0), seg_poci(s.segments.size(), 0.0);
    for (std::size_t i = 0; i < d.exposures.size(); ++i) {
        const auto sid = s.segment_of[i];
        if (sid < 0) continue;
        const auto st = d.exposures[i].stage;
        if (st == Stage::S3) seg_s3[static_cast<std::size_t>(sid)] += s.allowance[i];
        else if (st == Stage::Poci) seg_poci[static_cast<std::size_t>(sid)] += s.allowance[i];
    }
    for (std::size_t g = 0; g < s.segments.size(); ++g) r.npe_provisions_credit += seg_s3[g] + seg_poci[g];
    const double nii0 = r.income[0] - r.expense[0];
    for (std::size_t y = 0; y < 3; ++y) {
        double s3 = 0, poci = 0;
        for (std::size_t g = 0; g < s.segments.size(); ++g) {
            s3 += projection.results[g][1][y].prov_stock_s3;
            poci += projection.results[g][1][y].prov_stock_poci;
        }
        const double dprov = (s3 + poci) - r.npe_provisions_credit;
        r.npe_provision_increase[y] = dprov;
        r.nii_cap[y] = std::min(nii0, nii0 - nii0 * dprov / (r.volume_performing + (r.volume_non_performing - r.provisions_non_performing)));
    }
    return r;
}

void write_nii(const NiiResult& r, const Dataset&, const fs::path& file) {
    std::ofstream f(file, std::ios::binary);
    if (!f) throw Error("cannot write " + file.string());
    f << "scenario,year,template_row,side,nii_type,currency,rate_type,status,positions,volume,provisions,interest,"
         "interest_reference,interest_margin,eir,margin_new_business\n";
    for (const auto& [key, c] : r.cells) {
        const auto& [scen, year, row, currency, rate_type, status] = key;
        const auto& tr = template_row(row);
        const bool perf = status == "performing";
        const double base = perf ? c.volume : c.net_volume;
        f << kScenarioNames[scen] << ',' << year << ',' << row << ',' << (tr.asset ? "asset" : "liability") << ','
          << tr.label << ',' << currency << ',' << rate_type << ',' << status << ',' << c.positions << ','
          << money(c.volume) << ',' << (perf ? "" : money(c.provisions)) << ',' << money(c.interest) << ','
          << (perf ? money(c.interest_reference) : "") << ',' << (perf ? money(c.interest_margin) : "") << ','
          << rate(base > 0 ? c.interest / base : 0.0) << ',';
        if (perf && scen == 0) {
            const auto it = r.margin_new_business.find({row, currency, rate_type});
            if (it != r.margin_new_business.end()) f << rate(it->second);
        }
        f << '\n';
    }
}

void write_nii_summary(std::ostream& f, const NiiResult& r) {
    const double nii0 = r.income[0] - r.expense[0];
    f << "{\n    \"own_rating\": ";
    if (r.own_rating.empty()) f << "null";
    else f << '"' << json_escape(r.own_rating) << '"';
    f << ",\n    \"idiosyncratic_shock\": " << rate(r.idiosyncratic_shock) << ",\n    \"new_business_months\": "
      << r.new_business_months << ",\n    \"positions\": {\n      \"assets\": " << r.assets
      << ",\n      \"non_performing\": " << r.non_performing << ",\n      \"deposits\": " << r.deposits
      << ",\n      \"sight_deposits\": " << r.sight_deposits << ",\n      \"debt_issued\": " << r.debt_issued
      << "\n    },\n    \"fallbacks\": {\n      \"missing_rate\": " << r.missing_rate
      << ",\n      \"floating_without_frequency\": " << r.floating_without_frequency << ",\n      \"term_fallback\": "
      << r.term_fallback << ",\n      \"new_business_fallback_cells\": " << r.new_business_fallback_cells
      << "\n    },\n    \"starting_point\": {\n      \"interest_income\": " << money(r.income[0])
      << ",\n      \"interest_expense\": " << money(r.expense[0]) << ",\n      \"nii\": " << money(nii0)
      << ",\n      \"volume_performing\": " << money(r.volume_performing) << ",\n      \"volume_non_performing\": "
      << money(r.volume_non_performing) << ",\n      \"provisions_non_performing\": " << money(r.provisions_non_performing)
      << ",\n      \"npe_provisions_credit\": " << money(r.npe_provisions_credit) << "\n    },\n    \"totals\": {";
    for (std::size_t slot = 1; slot < 7; ++slot) {
        const bool adverse = slot > 3;
        const std::size_t y = (slot - 1) % 3;
        const double nii = r.income[slot] - r.expense[slot];
        f << (slot > 1 ? ",\n" : "\n") << "      \"" << (adverse ? "adverse" : "baseline") << '/' << (y + 1)
          << "\": {\n        \"interest_income\": " << money(r.income[slot]) << ",\n        \"interest_expense\": "
          << money(r.expense[slot]) << ",\n        \"nii\": " << money(nii);
        if (adverse)
            f << ",\n        \"npe_provision_increase\": " << money(r.npe_provision_increase[y]) << ",\n        \"nii_cap\": "
              << money(r.nii_cap[y]) << ",\n        \"nii_capped\": " << money(std::min(nii, r.nii_cap[y]));
        f << "\n      }";
    }
    f << "\n    }\n  }";
}

}  // namespace sora
