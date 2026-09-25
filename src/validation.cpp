#include "sora/validation.hpp"

#include <cmath>
#include <cstdio>

namespace sora {

bool Diagnostics::has_errors() const {
    for (const auto& f : findings) if (f.severity == "error") return true;
    return false;
}

Diagnostics check_inputs(Duck& duck, const Dataset& d, const Segmentation& s) {
    Diagnostics diag;
    std::uint64_t bad_stage = 0, missing_gca = 0;
    for (std::size_t i = 0; i < d.exposures.size(); ++i) {
        const auto& e = d.exposures[i];
        const bool impaired = e.measurement == Measurement::AmortisedCost || e.measurement == Measurement::Fvoci;
        if (impaired != (e.stage != Stage::NotApplicable)) ++bad_stage;
        if (s.segment_of[i] >= 0 && !e.has_gca) ++missing_gca;
    }
    if (bad_stage)
        diag.findings.push_back({"INV-IN-003", "error", "stage inconsistent with measurement category", bad_stage});
    if (missing_gca)
        diag.findings.push_back({"INV-IN-004", "error", "in-scope exposure without gross carrying amount", missing_gca});

    // INV-RC-001: total allowance of SIM exposures vs the stage history at the reference month end.
    if (sim_table_exists(d.sim_dir, "sim_stage_history")) {
        double hist = 0.0, expo = 0.0;
        std::uint64_t n_hist = 0;
        duck.query("SELECT sum(CAST(h.loss_allowance AS DOUBLE) * coalesce(CAST(f.rate_to_reporting AS DOUBLE), 1.0)), count(*) FROM " +
                       sim_source(d.sim_dir, "sim_stage_history") + " h LEFT JOIN " + sim_source(d.sim_dir, "sim_fx_rate") +
                       " f ON f.currency = h.currency AND CAST(f.rate_date AS DATE) = DATE " + sql_quote(d.manifest.reference_date) +
                       " WHERE CAST(h.period_end AS DATE) = last_day(DATE " + sql_quote(d.manifest.reference_date) + ")",
                   [&](const Chunk& c) {
                       if (c.size() && c.valid(0, 0)) hist = c.f64(0, 0);
                       if (c.size()) n_hist = static_cast<std::uint64_t>(c.i64(1, 0));
                   });
        for (const auto& e : d.exposures) {
            if (e.stage != Stage::NotApplicable) expo += to_double(e.allowance) * d.fx(e.currency);
        }
        const double diff = expo - hist;
        char msg[256];
        std::snprintf(msg, sizeof msg, "allowance in sim_exposure %.2f vs sim_stage_history at reference month end %.2f "
                      "(difference %.2f, %llu history rows)", expo, hist, diff, static_cast<unsigned long long>(n_hist));
        const bool ok = std::fabs(diff) <= std::max(1.0, 1e-4 * std::fabs(expo));
        diag.findings.push_back({"INV-RC-001", ok ? "info" : "warning", msg, 0});
    }
    return diag;
}

}  // namespace sora
