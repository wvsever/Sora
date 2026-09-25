#include "sora/calibration.hpp"

#include <algorithm>

namespace sora {

Matrix3 matmul(const Matrix3& a, const Matrix3& b) {
    Matrix3 r{};
    for (int i = 0; i < 3; ++i)
        for (int j = 0; j < 3; ++j) {
            double acc = 0.0;
            for (int k = 0; k < 3; ++k) acc += a[i][k] * b[k][j];
            r[i][j] = acc;
        }
    return r;
}

Matrix3 matpow(const Matrix3& m, int n) {
    Matrix3 r{{{1, 0, 0}, {0, 1, 0}, {0, 0, 1}}};
    for (int i = 0; i < n; ++i) r = matmul(r, m);
    return r;
}

namespace {

struct Cell {
    std::uint64_t n = 0;
    double w = 0.0;
    std::array<double, 3> to{};
};

// Last day of the month following the month of `day` (days since epoch), without calendar libraries.
Date next_month_end(Date day) {
    // Civil-from-days (H. Hinnant), then days-from-civil of the next month end.
    auto civil = [](std::int64_t z) {
        z += 719468;
        const std::int64_t era = (z >= 0 ? z : z - 146096) / 146097;
        const auto doe = static_cast<unsigned>(z - era * 146097);
        const unsigned yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
        const std::int64_t y = static_cast<std::int64_t>(yoe) + era * 400;
        const unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
        const unsigned mp = (5 * doy + 2) / 153;
        const unsigned m = mp < 10 ? mp + 3 : mp - 9;
        return std::pair<std::int64_t, unsigned>{m <= 2 ? y + 1 : y, m};
    };
    auto days = [](std::int64_t y, unsigned m, unsigned dd) {
        y -= m <= 2;
        const std::int64_t era = (y >= 0 ? y : y - 399) / 400;
        const auto yoe = static_cast<unsigned>(y - era * 400);
        const unsigned doy = (153 * (m > 2 ? m - 3 : m + 9) + 2) / 5 + dd - 1;
        const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
        return era * 146097 + static_cast<std::int64_t>(doe) - 719468;
    };
    auto [y, m] = civil(day);
    // first day of the month after next, minus one day = end of next month
    unsigned m2 = m + 2;
    std::int64_t y2 = y;
    if (m2 > 12) { m2 -= 12; ++y2; }
    return static_cast<Date>(days(y2, m2, 1) - 1);
}

}  // namespace

Calibration calibrate(Duck& duck, const Dataset& d, const Segmentation& s, const CalibrationConfig& cfg) {
    const auto L = s.level_keys.size();
    std::vector<std::array<Cell, 3>> counts(L);
    std::vector<std::array<std::array<double, 2>, 4>> stocks(L);   // [stage S1,S2,S3,POCI] -> gca, allowance

    // Stocks at t0, in exposure order.
    for (std::size_t i = 0; i < d.exposures.size(); ++i) {
        const auto sid = s.segment_of[i];
        if (sid < 0) continue;
        const auto& e = d.exposures[i];
        const auto st = static_cast<std::size_t>(e.stage);
        if (st > 3) continue;
        const double g = to_double(e.gca) * s.fx[i], a = to_double(e.allowance) * s.fx[i];
        for (auto lv : s.segments[static_cast<std::size_t>(sid)].levels) {
            stocks[lv][st][0] += g;
            stocks[lv][st][1] += a;
        }
    }

    // Month-on-month transitions, streamed in (exposure_id, period_end) order. DuckDB joins each history row
    // to the exposure's index (its rank by exposure_id, which is the order of Dataset::exposures), so the sort
    // runs on integers instead of strings and the loop needs no key lookups. History rows of exposures that
    // are not in sim_exposure drop out of the join; they never contributed (no segment).
    if (sim_table_exists(d.sim_dir, "sim_stage_history") && !d.exposures.empty()) {
        // The index join below relies on load_dataset keeping every sim_exposure row, in the same
        // ORDER BY CAST(exposure_id AS VARCHAR). Check that invariant instead of trusting it.
        std::int64_t rows = -1;
        duck.query("SELECT count(*) FROM " + sim_source(d.sim_dir, "sim_exposure"),
                   [&](const Chunk& c) { if (c.size()) rows = c.i64(0, 0); });
        if (rows != static_cast<std::int64_t>(d.exposures.size()))
            throw Error("calibration: sim_exposure has " + std::to_string(rows) + " rows but " +
                        std::to_string(d.exposures.size()) + " were loaded; exposure index join would misalign");
        std::uint32_t prev_ex = kNone;
        Date prev_day = 0, prev_next = 0;   // prev_next = next_month_end(prev_day)
        int prev_stage = -1;
        double prev_w = 0.0;
        bool prev_has_w = false;
        std::int32_t prev_sid = -1;
        duck.query("SELECT x.ix, CAST(h.period_end AS DATE), CAST(CASE CAST(h.stage AS VARCHAR) WHEN 'stage1' THEN 0 "
                   "WHEN 'stage2' THEN 1 WHEN 'stage3' THEN 2 ELSE -1 END AS BIGINT), "
                   "CAST(h.gross_carrying_amount AS DECIMAL(18,2)) FROM " + sim_source(d.sim_dir, "sim_stage_history") +
                   " h JOIN (SELECT CAST(exposure_id AS VARCHAR) AS id, "
                   "row_number() OVER (ORDER BY CAST(exposure_id AS VARCHAR)) - 1 AS ix FROM " +
                   sim_source(d.sim_dir, "sim_exposure") + ") x ON x.id = CAST(h.exposure_id AS VARCHAR) ORDER BY 1, 2",
                   [&](const Chunk& c) {
                       for (std::size_t r = 0; r < c.size(); ++r) {
                           const auto ex = static_cast<std::uint32_t>(c.i64(0, r));
                           const Date day = c.date(1, r);
                           const int st = static_cast<int>(c.i64(2, r));
                           if (ex == prev_ex && prev_stage >= 0 && st >= 0 && prev_has_w && prev_sid >= 0 && day == prev_next) {
                               for (auto lv : s.segments[static_cast<std::size_t>(prev_sid)].levels) {
                                   auto& cell = counts[lv][static_cast<std::size_t>(prev_stage)];
                                   cell.n += 1;
                                   cell.w += prev_w;
                                   cell.to[static_cast<std::size_t>(st)] += prev_w;
                               }
                           }
                           if (ex != prev_ex) {
                               if (ex >= d.exposures.size()) throw Error("calibration: exposure index out of range");
                               prev_ex = ex;
                               prev_sid = s.segment_of[ex];
                           }
                           if (day != prev_day || prev_next == 0) prev_next = next_month_end(day);   // few distinct month ends
                           prev_day = day;
                           prev_stage = st;
                           prev_has_w = c.valid(3, r);
                           prev_w = prev_has_w && prev_sid >= 0 ? to_double(c.cents(3, r)) * s.fx[ex] : 0.0;
                       }
                   });
    }

    Calibration out;
    for (const auto& seg : s.segments) {
        Matrix3 m{};
        std::array<std::string, 5> src;
        for (std::size_t i = 0; i < 3; ++i) {
            const Cell* cell = nullptr;
            for (auto lv : seg.levels) {
                const auto& c = counts[lv][i];
                if (c.n >= cfg.min_observations && c.w > 0) {
                    cell = &c;
                    src[i] = s.level_keys.at(lv);
                    break;
                }
            }
            if (!cell) {
                m[i] = {0, 0, 0};
                m[i][i] = 1.0;
                src[i] = "none";
            } else {
                for (std::size_t j = 0; j < 3; ++j) m[i][j] = cell->to[j] / cell->w;
            }
        }
        Matrix3 absorbing = m;
        absorbing[2] = {0.0, 0.0, 1.0};
        const auto a12 = matpow(absorbing, 12), c12 = matpow(m, 12);
        Params p;
        p.pd12m_s1 = std::max(a12[0][2], cfg.pd_floor);
        p.tr1_2 = a12[0][1];
        p.pd12m_s2 = std::max(a12[1][2], cfg.pd_floor);
        p.tr2_1 = a12[1][0];
        p.tr3_1 = c12[2][0];
        p.tr3_2 = c12[2][1];
        auto coverage = [&](std::size_t stage, std::string& from) {
            for (auto lv : seg.levels) {
                const auto& [g, a] = stocks[lv][stage];
                if (g > 0) {
                    from = s.level_keys.at(lv);
                    return a / g;
                }
            }
            from = "none";
            return 0.0;
        };
        p.lgd_s3 = coverage(2, src[3]);
        p.lrlt_s2 = coverage(1, src[4]);
        p.lgd_s1 = p.lgd_s2 = p.lgd_s3;
        if (seg.portfolio == "CB") p.lgd_s1 = p.lgd_s2 = p.lgd_s3 = p.lrlt_s2 = 0.0;   // EBA MN para 146
        out.params.push_back(p);
        out.sources.push_back(src);
    }
    return out;
}

}  // namespace sora
