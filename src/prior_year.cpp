#include "sora/prior_year.hpp"

#include <limits>
#include <unordered_map>

namespace sora {

namespace {

bool leap(int y) { return (y % 4 == 0 && y % 100 != 0) || y % 400 == 0; }

int days_in_month(int y, int m) {
    static constexpr int kDays[12] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    return m == 2 && leap(y) ? 29 : kDays[m - 1];
}

bool digits(const std::string& s, std::size_t pos, std::size_t n) {
    for (std::size_t k = pos; k < pos + n; ++k)
        if (s[k] < '0' || s[k] > '9') return false;
    return true;
}

}  // namespace

std::string prior_year_end(const std::string& reference_date, const std::string& configured) {
    if (reference_date.size() < 4 || !digits(reference_date, 0, 4)) throw Error("invalid reference date " + reference_date);
    const int ref_year = std::stoi(reference_date.substr(0, 4));
    const std::string date = configured.empty() ? std::to_string(ref_year - 1) + "-12-31" : configured;
    const bool shape = date.size() == 10 && date[4] == '-' && date[7] == '-' && digits(date, 0, 4) && digits(date, 5, 2) &&
                       digits(date, 8, 2);
    const int y = shape ? std::stoi(date.substr(0, 4)) : 0, m = shape ? std::stoi(date.substr(5, 2)) : 0,
              dd = shape ? std::stoi(date.substr(8, 2)) : 0;
    if (!shape || m < 1 || m > 12 || dd != days_in_month(y, m) || y >= ref_year)
        throw Error("prior_year_end " + date + ": must be a month end (YYYY-MM-DD) in a year before the reference date");
    return date;
}

bool prior_cell_known(std::string_view header, bool percent, bool exposures_known, bool provisions_known) {
    static constexpr std::string_view kExposure[] = {
        "Total exposure (total Exp)", "Performing exposure (Perf Exp)", "Performing exposure (Exp)",
        "of which: stage 1 (Exp S1)", "of which: stage 2 (Exp S2)", "Non-performing exposure (Exp S3)",
        "of which: existing Non-performing exposure (Old Exp S3)",
        "of which: cumulative new non-performing exposure (Cumul New Exp S3)", "POCI exposures (Exp POCI)"};
    for (auto h : kExposure)
        if (header == h) return exposures_known;
    if (header.rfind("Coverage ratio", 0) == 0) return exposures_known && provisions_known;
    return percent || provisions_known;
}

PriorYear load_prior_year(Duck& duck, const Dataset& d, const Segmentation& s, const ScopeConfig& scope,
                          const std::string& date) {
    constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();
    PriorYear p;
    p.date = date;
    p.year = std::stoi(date.substr(0, 4));
    const auto n = d.exposures.size();
    p.stage.assign(n, Stage::NotApplicable);
    p.exposure.assign(n, kNaN);
    p.allowance.assign(n, kNaN);
    p.has_amount.assign(n, 0);
    if (!sim_table_exists(d.sim_dir, "sim_stage_history")) return p;

    // FX at the prior year-end; the reporting currency is 1 by definition.
    std::unordered_map<std::string, double> fx{{d.manifest.reporting_currency, 1.0}};
    if (sim_table_exists(d.sim_dir, "sim_fx_rate"))
        duck.query("SELECT CAST(currency AS VARCHAR), CAST(rate_to_reporting AS DECIMAL(18,9)) FROM " +
                       sim_source(d.sim_dir, "sim_fx_rate") + " WHERE CAST(rate_date AS DATE) = DATE " + sql_quote(date),
                   [&](const Chunk& c) {
                       for (std::size_t r = 0; r < c.size(); ++r) {
                           std::string ccy(c.str(0, r));
                           if (ccy != d.manifest.reporting_currency && c.valid(1, r)) fx[ccy] = nano_to_double(c.nano(1, r));
                       }
                   });

    // principal_outstanding is optional in the SIM schema (proxy for the gross carrying amount).
    const auto history = sim_source(d.sim_dir, "sim_stage_history");
    bool has_principal = false;
    duck.query("SELECT count(*) FROM (DESCRIBE SELECT * FROM " + history + ") WHERE column_name = 'principal_outstanding'",
               [&](const Chunk& c) { has_principal = c.size() && c.i64(0, 0) > 0; });

    duck.query(std::string("SELECT CAST(exposure_id AS VARCHAR), CAST(stage AS VARCHAR), CAST(currency AS VARCHAR), "
                           "CAST(gross_carrying_amount AS DECIMAL(18,2)), ") +
                   (has_principal ? "CAST(principal_outstanding AS DECIMAL(18,2))" : "CAST(NULL AS DECIMAL(18,2))") +
                   ", CAST(off_balance_amount AS DECIMAL(18,2)), CAST(loss_allowance AS DECIMAL(18,2)) FROM " + history +
                   " WHERE CAST(period_end AS DATE) = DATE " + sql_quote(date) + " ORDER BY 1",
               [&](const Chunk& c) {
                   for (std::size_t r = 0; r < c.size(); ++r) {
                       ++p.history_rows;
                       const auto rate = fx.find(std::string(c.str(2, r)));
                       const bool has_fx = rate != fx.end();
                       const double allowance = c.valid(6, r) ? to_double(c.cents(6, r)) : 0.0;
                       const auto id = d.exposure_ids.find(c.str(0, r));
                       if (!id) {   // derecognised before the reference date: no t0 portfolio (counted only)
                           ++p.not_in_sim_exposure;
                           if (has_fx) p.not_in_sim_exposure_allowance += allowance * rate->second;
                           continue;
                       }
                       const std::size_t i = *id;
                       if (s.segment_of[i] < 0) {   // not in the t0 scope (measurement, type, intragroup)
                           ++p.out_of_scope;
                           continue;
                       }
                       ++p.exposures;
                       p.stage[i] = parse_stage(c.str(1, r));
                       const bool has_gca = c.valid(3, r), has_prin = c.valid(4, r);
                       const double amount = has_gca ? to_double(c.cents(3, r)) : has_prin ? to_double(c.cents(4, r)) : 0.0;
                       p.has_amount[i] = has_gca || has_prin ? 1 : 0;
                       if (has_gca) ++p.amount_gca;
                       else if (has_prin) ++p.amount_principal;
                       else ++p.missing_amount;
                       if (!has_fx) {
                           ++p.missing_fx;
                           continue;
                       }
                       const double f = rate->second;
                       double prov = allowance * f;
                       const auto& e = d.exposures[i];
                       if (undrawn_is_off_balance(e, scope)) {   // drawn share of the facility's allowance, as at t0
                           if (c.valid(5, r) && p.has_amount[i]) {
                               ++p.allowance_split_history;
                               const double u = to_double(c.cents(5, r));
                               if (u > 0) prov = prov * (amount / (amount + u));
                           } else if (e.off_balance > 0) {
                               ++p.allowance_split_t0_share;
                               const double g = to_double(e.gca), u = to_double(e.off_balance);
                               prov = prov * (g / (g + u));
                           }
                       }
                       p.allowance[i] = prov;
                       if (p.has_amount[i]) p.exposure[i] = amount * f;
                   }
               });
    p.available = p.history_rows > 0;
    return p;
}

}  // namespace sora
