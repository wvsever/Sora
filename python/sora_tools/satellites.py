"""Satellite model estimation: macro drivers -> PD/TR shifts per EBA portfolio (roadmap phase 5).

``sora-tools estimate-satellites <sim> -o satellites.csv --report fit.json`` estimates the coefficients of the
engine's satellite model from the SIM stage history and writes them in the layout of
``tests/params/synthetic_satellites.csv``, which the engine and the reference read (scenario key ``satellites``).

The engine applies, per segment and projection year t (``src/scenario.cpp``, ``tools/reference/sora_reference.py``)::

    z_t = beta_gdp * (gdp_t - normal) + beta_unemployment * (u_t - u_0) + beta_property * property_growth_t
    logit(P_t) = logit(P_0) + z_t     for PD12M_S1, PD12M_S2, TR1-2;   logit(TR2-1_t) = logit(TR2-1_0) - z_t

so one slope vector per portfolio moves all four transition rates on the logit scale. The estimator fits that
structure:

1. Observations. Consecutive month-ends of ``sim_stage_history`` (stage 1/2 -> stage 1/2/3) give, per EBA
   portfolio (segmentation of the reference implementation), transition type and month, the number of contracts
   at risk and of transitions. Types: ``pd_perf`` (S1 or S2 -> S3, the default rate of the performing book; the
   default), ``pd_s1``, ``pd_s2``, ``tr1_2``, ``tr2_1``. Counts are summed over rolling windows (default 12 months);
   the window hazard is annualised (``1 - (1 - h)^12``) and taken to the empirical logit (+0.5 correction, so
   windows without events are usable).
2. Drivers. The mean of each macro driver over the same window (optionally lagged). ``--macro-history`` supplies
   real series; ``--cycle-index`` uses the generator's cycle index (``reference/macro_cycle_index.csv``) as a
   PROXY, mapped to GDP growth only: ``gdp = normal + gdp_per_cycle * cycle`` (an assumption that scales
   ``beta_gdp`` directly).
3. Equation per portfolio: ``y_{k,t} = alpha_k + gamma_k * t + s_k * beta' x_t`` with an intercept and (unless
   ``--no-trend``) a linear trend per transition type k (portfolio growth and seasoning), ``s_k = -1`` for
   S2->S1. With several types they share the slopes, as in the engine. Weighted least squares (weights = inverse
   binomial variance of the empirical logit, i.e. minimum logit chi-square), intercepts and trends removed by the
   within transformation. The standard error is the larger of Newey-West (Bartlett, lag = window - 1, on the
   scores summed per period) and the binomial one inflated by the window overlap (x window).
4. Pooling. The same equation on all portfolios stacked (an intercept per portfolio and transition) gives the
   pooled slopes. Portfolio slopes are shrunk towards them per coefficient with a random-effects (DerSimonian-Laird)
   weight ``tau^2 / (tau^2 + se^2)``; portfolios with fewer than ``--min-events`` defaults (S1/S2 -> S3 over the
   sample) or no data take the pooled slopes.
5. Sign constraints (``beta_gdp <= 0``, ``beta_unemployment >= 0``, ``beta_property <= 0``). The pooled fit uses
   an active set: the violating coefficient with the largest wrong-sign t-value is fixed at 0 and the rest
   re-estimated. A portfolio coefficient that has the wrong sign after shrinkage falls back to the pooled value.
6. ``lgd_property_sensitivity`` is not estimated (no realised-LGD history against property prices); it is copied
   from ``--prior`` (a satellite file) or 0.

Pure Python (no numpy): the systems are at most 3x3. See plans/03_scenario_engine.md ("Satellite estimation") for
the limits. Output is deterministic (sorted keys, fixed formatting).
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path

import duckdb

# Portfolio order and labels of tests/params/synthetic_satellites.csv.
PORTFOLIOS = {
    "CB": "Central banks",
    "GG": "General governments",
    "CI": "Credit institutions",
    "OFC": "Other financial corporations",
    "NFC": "Non-financial corporations (debt securities)",
    "NFC_SME_CRE": "NFC SME commercial real estate",
    "NFC_SME_OTHER": "NFC SME other",
    "NFC_LARGE_CRE": "NFC non-SME commercial real estate",
    "NFC_LARGE_OTHER": "NFC non-SME other",
    "HH_HOUSE": "Households: lending for house purchase",
    "HH_CONS": "Households: consumer credit",
    "HH_OTHER": "Households: other lending",
}
COLUMNS = ["portfolio", "beta_gdp", "beta_unemployment", "beta_property", "lgd_property_sensitivity", "description"]
COEFFICIENTS = ("beta_gdp", "beta_unemployment", "beta_property")
EXPECTED_SIGN = {"beta_gdp": -1, "beta_unemployment": 1, "beta_property": -1}
PROPERTY_PORTFOLIOS = ("NFC_SME_CRE", "NFC_LARGE_CRE", "HH_HOUSE")

# transition type -> (from stage, to stage, sign of z in the engine)
TRANSITIONS = {
    "pd_s1": ("stage1", "stage3", 1),
    "tr1_2": ("stage1", "stage2", 1),
    "pd_s2": ("stage2", "stage3", 1),
    "tr2_1": ("stage2", "stage1", -1),
    "pd_perf": ("performing", "stage3", 1),     # S1 or S2 -> S3: default rate of the performing book
}
DEFAULT_TRANSITIONS = ("pd_perf",)


class SatelliteError(Exception):
    pass


def property_variable(portfolio: str) -> str:
    """The property price series the engine uses for the portfolio."""
    return "residential_property_prices" if portfolio == "HH_HOUSE" else "commercial_property_prices"


def driver_of(coef: str, portfolio: str) -> str:
    return {"beta_gdp": "real_gdp", "beta_unemployment": "unemployment_rate"}.get(coef) or property_variable(portfolio)


# ----------------------------------------------------------------------------------------- data

# Segmentation of tools/reference/sora_reference.py (SEGMENT_SQL): EBA portfolio from the counterparty sector,
# SME flag, CRE flag and household purpose. Commitments have no CRE flag or purpose: NFC_*_OTHER and HH_OTHER.
COUNTS_SQL = """
WITH seg AS (
    SELECT e.exposure_id,
        CASE c.eba_sector
            WHEN 'central_bank' THEN 'CB'
            WHEN 'general_government' THEN 'GG'
            WHEN 'credit_institution' THEN 'CI'
            WHEN 'other_financial' THEN 'OFC'
            WHEN 'non_financial_corporation' THEN
                CASE WHEN e.exposure_type = 'debt_security' THEN 'NFC'
                     ELSE 'NFC_' || CASE WHEN coalesce(c.is_sme, false) THEN 'SME' ELSE 'LARGE' END
                          || CASE WHEN coalesce(e.is_cre, false) THEN '_CRE' ELSE '_OTHER' END END
            WHEN 'household' THEN
                CASE WHEN e.exposure_type = 'debt_security' THEN 'HH_OTHER'
                     WHEN e.household_purpose = 'house_purchase' THEN 'HH_HOUSE'
                     WHEN e.household_purpose = 'consumption' THEN 'HH_CONS'
                     ELSE 'HH_OTHER' END
        END AS portfolio
    FROM sim_exposure e
    JOIN sim_counterparty c USING (counterparty_id)
    WHERE e.exposure_type IN ({types})
      AND NOT ({excl} AND coalesce(e.is_intragroup, false))
)
SELECT seg.portfolio, strftime(a.period_end, '%Y-%m') AS month, a.stage AS s_from, b.stage AS s_to,
       count(*) AS n
FROM sim_stage_history a
JOIN sim_stage_history b
  ON b.exposure_id = a.exposure_id AND b.period_end = last_day(a.period_end + INTERVAL 1 DAY)
JOIN seg ON seg.exposure_id = a.exposure_id
WHERE a.stage IN ('stage1', 'stage2') AND b.stage IN ('stage1', 'stage2', 'stage3')
  AND seg.portfolio IS NOT NULL
GROUP BY ALL
ORDER BY ALL
"""

DEFAULT_EXPOSURE_TYPES = ("loan", "finance_lease", "debt_security", "loan_commitment", "financial_guarantee",
                          "other_commitment")


def _connect(sim: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for t in ("sim_exposure", "sim_counterparty", "sim_stage_history"):
        if not any((sim / t).glob("**/*.parquet")):
            raise SatelliteError(f"{t} not found in {sim}")
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{sim / t}/**/*.parquet', "
                    "hive_partitioning=false)")
    return con


def load_counts(sim: Path, exposure_types=DEFAULT_EXPOSURE_TYPES, exclude_intragroup: bool = True):
    """{(portfolio, transition, month): [events, at_risk]} from consecutive month-ends, and data notes."""
    sim = Path(sim)
    con = _connect(sim)
    q = ", ".join("'" + t.replace("'", "''") + "'" for t in exposure_types)
    rows = con.execute(COUNTS_SQL.format(types=q, excl="true" if exclude_intragroup else "false")).fetchall()
    unmatched = con.execute("SELECT count(DISTINCT h.exposure_id) FROM sim_stage_history h "
                            "ANTI JOIN sim_exposure e USING (exposure_id)").fetchone()[0]
    at_risk: dict = defaultdict(int)
    moves: dict = defaultdict(int)
    for portfolio, month, s_from, s_to, n in rows:
        at_risk[(portfolio, s_from, month)] += n
        moves[(portfolio, s_from, s_to, month)] += n
    counts = {}
    for (portfolio, s_from, month), n in at_risk.items():
        for k, (f, t, _) in TRANSITIONS.items():
            if f == s_from:
                counts[(portfolio, k, month)] = [moves.get((portfolio, f, t, month), 0), n]
    for portfolio, month in sorted({(p, m) for (p, _, m) in at_risk}):
        cells = [counts[(portfolio, k, month)] for k in ("pd_s1", "pd_s2") if (portfolio, k, month) in counts]
        counts[(portfolio, "pd_perf", month)] = [sum(c[0] for c in cells), sum(c[1] for c in cells)]
    notes = {"stage_history_exposures_without_sim_exposure": unmatched,
             "exposure_types": list(exposure_types), "exclude_intragroup": exclude_intragroup}
    return counts, notes


def _month_of(period: str) -> list[str]:
    """Months covered by a period label: YYYY, YYYY-Qn or YYYY-MM (or a YYYY-MM-DD date)."""
    p = period.strip()
    if len(p) == 4:
        return [f"{p}-{m:02d}" for m in range(1, 13)]
    if len(p) == 7 and p[5] in "Qq":
        q = int(p[6])
        return [f"{p[:4]}-{m:02d}" for m in range(3 * q - 2, 3 * q + 1)]
    return [p[:7]]


def load_cycle_proxy(path: Path, gdp_per_cycle: float, normal_gdp_growth: float) -> dict:
    """{month: {'real_gdp': normal + gdp_per_cycle * cycle}} from reference/macro_cycle_index.csv."""
    out = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            month = (r.get("accounting_period") or r["period_end_date"])[:7]
            out[month] = {"real_gdp": normal_gdp_growth + gdp_per_cycle * float(r["macro_cycle_index"])}
    if not out:
        raise SatelliteError(f"no rows in {path}")
    return out


def load_macro_history(path: Path, key: str) -> dict:
    """{month: {variable: value}} from a long CSV: variable, key, value and period (YYYY, YYYY-Qn, YYYY-MM) or
    year. With a scenario column (the scenario-file layout), only scenario 'historical' rows are used; rows with a
    tenor or sector are ignored. Growth rates in percent, the unemployment rate as a level in percent."""
    out: dict = defaultdict(dict)
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if "scenario" in r and r["scenario"] != "historical":
                continue
            if r.get("tenor") or r.get("sector") or r.get("key") != key:
                continue
            period = r.get("period") or r.get("year")
            for m in _month_of(period):
                out[m][r["variable"]] = float(r["value"])
    if not out:
        raise SatelliteError(f"no historical rows for key {key} in {path}")
    return dict(out)


# ----------------------------------------------------------------------------------------- linear algebra

def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting; None if singular (relative pivot < 1e-10)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    scale = max((abs(a[i][i]) for i in range(n)), default=0.0) or 1.0
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-10 * scale:
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for j in range(c, n + 1):
                    m[r][j] -= f * m[c][j]
    return [m[i][n] / m[i][i] for i in range(n)]


def _inverse(a: list[list[float]]) -> list[list[float]] | None:
    n = len(a)
    cols = [_solve(a, [1.0 if i == j else 0.0 for i in range(n)]) for j in range(n)]
    if any(c is None for c in cols):
        return None
    return [[cols[j][i] for j in range(n)] for i in range(n)]


# ----------------------------------------------------------------------------------------- estimation

def _logit(p: float) -> float:
    return math.log(p / (1 - p))


def window_observations(counts: dict, drivers: dict, portfolio: str, transitions, variables, window: int,
                        lag: int, months: list[str]) -> list[dict]:
    """Rolling-window observations of one portfolio: {'g': transition, 't': window end, 'y', 'w', 'x', 'k', 'n'}."""
    obs = []
    idx = {m: i for i, m in enumerate(months)}
    for k in transitions:
        sign = TRANSITIONS[k][2]
        for end in range(window - 1, len(months)):
            span = months[end - window + 1:end + 1]
            kn = [counts.get((portfolio, k, m)) for m in span]
            if any(c is None for c in kn):
                continue
            events, n = sum(c[0] for c in kn), sum(c[1] for c in kn)
            if n == 0:
                continue
            drv_months = []
            for m in span:
                i = idx[m] - lag
                if i < 0:
                    break
                drv_months.append(months[i])
            if len(drv_months) < window or any(v not in drivers.get(m, {}) for m in drv_months for v in variables):
                continue
            x = [sign * sum(drivers[m][v] for m in drv_months) / window for v in variables]
            h = (events + 0.5) / (n + 1.0)                       # empirical hazard with a 0.5 correction
            p12 = 1 - (1 - h) ** 12
            var_h = h * (1 - h) / (n + 1.0)
            dy = 12 * (1 - h) ** 11 / (p12 * (1 - p12))           # delta method: d logit(p12) / d h
            obs.append({"g": (portfolio, k), "t": end, "y": _logit(p12), "w": 1.0 / (dy * dy * var_h),
                        "x": x, "k": events, "n": n})
    return obs


def _within(obs: list[dict], nvar: int, trend: bool) -> list[tuple]:
    """Remove a (weighted) intercept, and with `trend` a linear time trend, per group g from y and x."""
    by_group: dict = defaultdict(list)
    for o in obs:
        by_group[o["g"]].append(o)
    rows = []
    for g in sorted(by_group):
        os_ = by_group[g]
        sw = sum(o["w"] for o in os_)
        tm = sum(o["w"] * o["t"] for o in os_) / sw
        stt = sum(o["w"] * (o["t"] - tm) ** 2 for o in os_) if trend else 0.0

        def resid(values, os_=os_, sw=sw, tm=tm, stt=stt):
            m = sum(o["w"] * v for o, v in zip(os_, values)) / sw
            b = sum(o["w"] * (o["t"] - tm) * (v - m) for o, v in zip(os_, values)) / stt if stt > 0 else 0.0
            return [v - m - b * (o["t"] - tm) for o, v in zip(os_, values)]

        ys = resid([o["y"] for o in os_])
        xs = [resid([o["x"][j] for o in os_]) for j in range(nvar)]
        rows += [(o["t"], o["w"], ys[i], [xs[j][i] for j in range(nvar)]) for i, o in enumerate(os_)]
    return rows


def wls_within(obs: list[dict], nvar: int, hac_lag: int, trend: bool = False) -> dict | None:
    """WLS of y on x with an intercept (and with `trend` a linear trend) per group g, removed by the within
    transformation; Newey-West (Bartlett) standard errors on the scores summed per period t."""
    if nvar == 0 or not obs:
        return None
    rows = _within(obs, nvar, trend)
    groups = {o["g"] for o in obs}
    a = [[sum(w * x[i] * x[j] for _, w, _, x in rows) for j in range(nvar)] for i in range(nvar)]
    b = [sum(w * x[i] * y for _, w, y, x in rows) for i in range(nvar)]
    beta = _solve(a, b)
    ainv = _inverse(a) if beta is not None else None
    if beta is None or ainv is None:
        return None
    resid = [(t, w, y - sum(beta[j] * x[j] for j in range(nvar)), x) for t, w, y, x in rows]
    sse = sum(w * e * e for _, w, e, _ in resid)
    sst = sum(w * y * y for _, w, y, _ in rows)
    scores: dict = defaultdict(lambda: [0.0] * nvar)
    for t, w, e, x in resid:
        for j in range(nvar):
            scores[t][j] += w * e * x[j]
    ts = sorted(scores)
    meat = [[0.0] * nvar for _ in range(nvar)]
    for lag in range(0, hac_lag + 1):
        kw = 1.0 - lag / (hac_lag + 1.0)
        for i, t in enumerate(ts):
            if t - lag not in scores:
                continue
            s, s_l = scores[t], scores[t - lag]
            for p in range(nvar):
                for q in range(nvar):
                    meat[p][q] += kw * (s[p] * s_l[q] + (s_l[p] * s[q] if lag else 0.0))
    n, k = len(rows), nvar + len(groups) * (2 if trend else 1)
    dfc = n / (n - k) if n > k else float("inf")
    cov = [[dfc * sum(ainv[p][i] * meat[i][j] * ainv[j][q] for i in range(nvar) for j in range(nvar))
            for q in range(nvar)] for p in range(nvar)]
    se_hac = [math.sqrt(max(cov[j][j], 0.0)) for j in range(nvar)]
    # Binomial (model) variance, inflated by the overlap: with windows of hac_lag + 1 months every month enters
    # that many windows. The reported SE is the larger of the two (HAC is biased down on short samples).
    se_bin = [math.sqrt(max(ainv[j][j], 0.0) * (hac_lag + 1)) for j in range(nvar)]
    return {"beta": beta, "se": [max(a, b) for a, b in zip(se_hac, se_bin)], "se_hac": se_hac, "se_binomial": se_bin,
            "r2_within": 1 - sse / sst if sst > 0 else 0.0, "n": n, "periods": len(ts), "groups": len(groups)}


def constrained_fit(obs: list[dict], coefs: list[str], hac_lag: int, trend: bool = False
                    ) -> tuple[dict, dict | None, dict[str, str]]:
    """Active-set sign constraints: drop the worst wrong-sign coefficient (fixed at 0) and refit, until none is
    left (a singular system drops the last driver). Returns ({coef: (beta, se)}, the final fit,
    {coefficient fixed at 0: 'sign' | 'singular'})."""
    active, dropped, reasons = list(coefs), [], {}
    while active:
        idx = [coefs.index(c) for c in active]
        sub = [dict(o, x=[o["x"][i] for i in idx]) for o in obs]
        fit = wls_within(sub, len(active), hac_lag, trend)
        if fit is None:                                        # singular: drop the last (least important) driver
            dropped.append(active.pop())
            reasons[dropped[-1]] = "singular"
            continue
        wrong = [(fit["beta"][j] / (fit["se"][j] or 1e-300) * EXPECTED_SIGN[c], c)
                 for j, c in enumerate(active) if fit["beta"][j] * EXPECTED_SIGN[c] < 0]
        if not wrong:
            est = {c: (fit["beta"][j], fit["se"][j]) for j, c in enumerate(active)}
            est.update({c: (0.0, None) for c in dropped})
            return est, fit, reasons
        c = min(wrong)[1]
        active.remove(c)
        dropped.append(c)
        reasons[c] = "sign"
    return {c: (0.0, None) for c in coefs}, None, reasons


def dersimonian_laird(estimates: list[tuple[float, float]]) -> float:
    """Between-portfolio variance tau^2 of a coefficient from (beta, se) pairs (random-effects meta-analysis)."""
    pairs = [(b, s) for b, s in estimates if s and s > 0]
    if len(pairs) < 2:
        return 0.0
    w = [1 / (s * s) for _, s in pairs]
    mean = sum(wi * b for wi, (b, _) in zip(w, pairs)) / sum(w)
    q = sum(wi * (b - mean) ** 2 for wi, (b, _) in zip(w, pairs))
    denom = sum(w) - sum(wi * wi for wi in w) / sum(w)
    return max(0.0, (q - (len(pairs) - 1)) / denom) if denom > 0 else 0.0


def estimate(counts: dict, drivers: dict, *, window: int = 12, lag: int = 0, transitions=DEFAULT_TRANSITIONS,
             trend: bool = True,
             min_events: int = 30, start: str | None = None, end: str | None = None,
             property_portfolios=PROPERTY_PORTFOLIOS, portfolios=tuple(PORTFOLIOS)) -> dict:
    """Estimate satellite slopes. `counts` from load_counts, `drivers` {month: {variable: value}}.
    Returns {'coefficients': {portfolio: {coef: value}}, 'report': {...}}."""
    months = sorted({m for (_, _, m) in counts} & set(drivers))
    months = [m for m in months if (start is None or m >= start) and (end is None or m <= end)]
    if len(months) < window + 2:
        raise SatelliteError(f"{len(months)} months with both stage history and drivers; need more than "
                             f"window + 2 = {window + 2}")
    # driver availability decides which coefficients are estimable
    have = {v for v in ("real_gdp", "unemployment_rate", "residential_property_prices",
                        "commercial_property_prices") if all(v in drivers[m] for m in months)}

    def coefs_for(portfolio):
        return [c for c in COEFFICIENTS if driver_of(c, portfolio) in have
                and (c != "beta_property" or portfolio in property_portfolios)]

    hac = window - 1
    per_obs = {}
    for p in portfolios:
        cs = coefs_for(p)
        o = window_observations(counts, drivers, p, transitions, [driver_of(c, p) for c in cs], window, lag, months)
        # pad to the full coefficient vector (property driver = 0 where not used) for the pooled equation
        full = []
        for ob in o:
            x = dict(zip(cs, ob["x"]))
            full.append(dict(ob, x=[x.get(c, 0.0) for c in COEFFICIENTS]))
        per_obs[p] = (cs, full)

    all_coefs = [c for c in COEFFICIENTS if any(c in cs for cs, _ in per_obs.values())]
    pooled_obs = [dict(o, x=[o["x"][COEFFICIENTS.index(c)] for c in all_coefs])
                  for _, ob in per_obs.values() for o in ob]
    pooled, pooled_fit, pooled_dropped = constrained_fit(pooled_obs, all_coefs, hac, trend)
    pooled = {c: pooled.get(c, (0.0, None)) for c in COEFFICIENTS}

    # unconstrained portfolio fits
    raw = {}
    for p in portfolios:
        cs, ob = per_obs[p]
        events = sum(counts[(p, "pd_perf", m)][0] for m in months if (p, "pd_perf", m) in counts)
        sub = [dict(o, x=[o["x"][COEFFICIENTS.index(c)] for c in cs]) for o in ob]
        fit = wls_within(sub, len(cs), hac, trend) if cs and events >= min_events else None
        raw[p] = {"coefs": cs, "fit": fit, "default_events": events, "obs": len(ob)}

    tau2 = {c: dersimonian_laird([(r["fit"]["beta"][r["coefs"].index(c)], r["fit"]["se"][r["coefs"].index(c)])
                                  for r in raw.values() if r["fit"] and c in r["coefs"]]) for c in COEFFICIENTS}

    coefficients, equations = {}, {}
    for p in portfolios:
        r = raw[p]
        out, detail = {}, {}
        for c in COEFFICIENTS:
            pb, pse = pooled[c]
            if c not in r["coefs"]:
                out[c], detail[c] = 0.0, {"source": "not_estimated"}
                continue
            if r["fit"] is None:
                out[c], detail[c] = pb, {"source": "pooled", "pooled": pb, "pooled_se": pse}
                continue
            j = r["coefs"].index(c)
            b, se = r["fit"]["beta"][j], r["fit"]["se"][j]
            z = tau2[c] / (tau2[c] + se * se) if se > 0 else 1.0
            val = z * b + (1 - z) * pb
            src = "shrunk"
            if val * EXPECTED_SIGN[c] < 0:
                val, src = pb, "pooled_sign_fallback"
            out[c] = val
            detail[c] = {"source": src, "estimate": b, "se": se, "se_hac": r["fit"]["se_hac"][j],
                         "se_binomial": r["fit"]["se_binomial"][j], "t": b / se if se > 0 else None,
                         "pooled": pb, "pooled_se": pse, "shrinkage_weight": z}
        coefficients[p] = out
        fit = r["fit"]
        equations[p] = {
            "observations": r["obs"], "default_events": r["default_events"],
            "estimated": fit is not None,
            "r2_within": fit["r2_within"] if fit else None,
            "n": fit["n"] if fit else r["obs"], "periods": fit["periods"] if fit else None,
            "coefficients": detail,
        }
    report = {
        "method": "grouped empirical-logit WLS, intercept (and trend) per transition, max(Newey-West, overlap-"
                  "inflated binomial) SE, "
                  "DerSimonian-Laird shrinkage to the pooled fit, sign constraints",
        "window_months": window, "lag_months": lag, "transitions": list(transitions), "group_trend": trend,
        "months": [months[0], months[-1]], "drivers_available": sorted(have), "min_default_events": min_events,
        "expected_signs": EXPECTED_SIGN,
        "pooled": {
            "coefficients": {c: {"estimate": pooled[c][0], "se": pooled[c][1]} for c in COEFFICIENTS},
            "fixed_at_zero_by_sign_constraint": [c for c, why in pooled_dropped.items() if why == "sign"],
            "dropped_singular": [c for c, why in pooled_dropped.items() if why == "singular"],
            "r2_within": pooled_fit["r2_within"] if pooled_fit else None,
            "n": pooled_fit["n"] if pooled_fit else len(pooled_obs),
            "groups": pooled_fit["groups"] if pooled_fit else None,
        },
        "tau2": tau2,
        "equations": equations,
    }
    return {"coefficients": coefficients, "report": report}


# ----------------------------------------------------------------------------------------- output

def fmt(x: float) -> str:
    """Fixed 6 decimals, trailing zeros stripped, no negative zero (deterministic, engine-readable)."""
    s = f"{round(x, 6) + 0.0:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def read_satellites(path: Path) -> dict:
    with open(path, newline="") as f:
        return {r["portfolio"]: r for r in csv.DictReader(f)}


def write_satellites(path: Path, coefficients: dict, report: dict, prior: dict | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(COLUMNS)
        for p, label in PORTFOLIOS.items():
            c = coefficients[p]
            lgd = float(prior[p]["lgd_property_sensitivity"]) if prior and p in prior else 0.0
            sources = sorted({d["source"] for d in report["equations"][p]["coefficients"].values()} - {"not_estimated"})
            w.writerow([p, *(fmt(c[k]) for k in COEFFICIENTS), fmt(lgd),
                        f"{label} (estimated: {' + '.join(sources) or 'none'})"])


def run(sim: Path, output: Path, report_path: Path | None = None, *, macro_history: Path | None = None,
        macro_key: str = "EU", cycle_index: Path | None = None, gdp_per_cycle: float = 5.0,
        normal_gdp_growth: float = 1.5, prior: Path | None = None, **kw) -> dict:
    counts, notes = load_counts(Path(sim), **{k: kw.pop(k) for k in ("exposure_types", "exclude_intragroup")
                                             if k in kw})
    if macro_history:
        drivers = load_macro_history(Path(macro_history), macro_key)
        driver_note = {"source": "macro_history", "file": str(macro_history), "key": macro_key}
    elif cycle_index:
        drivers = load_cycle_proxy(Path(cycle_index), gdp_per_cycle, normal_gdp_growth)
        driver_note = {"source": "cycle_index_proxy", "file": str(cycle_index), "gdp_per_cycle": gdp_per_cycle,
                       "normal_gdp_growth": normal_gdp_growth,
                       "warning": "PROXY DRIVER: the generator's macro cycle index mapped to GDP growth as "
                                  "normal + gdp_per_cycle * cycle. beta_gdp absorbs the whole cycle sensitivity "
                                  "and scales with 1/gdp_per_cycle; unemployment and property effects are not "
                                  "identified (0). Synthetic data: not a real calibration."}
    else:
        raise SatelliteError("give --macro-history or --cycle-index")
    result = estimate(counts, drivers, **kw)
    prior_rows = read_satellites(Path(prior)) if prior else None
    result["report"]["drivers"] = driver_note
    result["report"]["data"] = notes
    result["report"]["lgd_property_sensitivity"] = (f"not estimated: copied from {prior}" if prior
                                                    else "not estimated: 0")
    result["report"]["output"] = {p: {c: float(fmt(v)) for c, v in cs.items()}
                                  for p, cs in result["coefficients"].items()}
    write_satellites(Path(output), result["coefficients"], result["report"], prior_rows)
    if report_path:
        Path(report_path).write_text(json.dumps(result["report"], indent=2, sort_keys=True) + "\n")
    return result


# ----------------------------------------------------------------------------------------- CLI

def cmd_estimate_satellites(args) -> int:
    try:
        res = run(args.sim, args.output, args.report, macro_history=args.macro_history, macro_key=args.macro_key,
                  cycle_index=args.cycle_index, gdp_per_cycle=args.gdp_per_cycle,
                  normal_gdp_growth=args.normal_gdp_growth, prior=args.prior, window=args.window, lag=args.lag,
                  min_events=args.min_events, start=args.start, end=args.end,
                  transitions=tuple(args.transitions), trend=args.trend, exposure_types=tuple(args.exposure_types))
    except SatelliteError as e:
        print(f"ERROR: {e}", flush=True)
        return 2
    rep = res["report"]
    print(f"{'portfolio':<16} {'beta_gdp':>10} {'beta_u':>10} {'beta_prop':>10}  {'R2':>6} {'events':>7}  source")
    for p, c in res["coefficients"].items():
        eq = rep["equations"][p]
        r2 = f"{eq['r2_within']:.3f}" if eq["r2_within"] is not None else "-"
        src = ",".join(sorted({d["source"] for d in eq["coefficients"].values()} - {"not_estimated"})) or "-"
        print(f"{p:<16} {c['beta_gdp']:>10.4f} {c['beta_unemployment']:>10.4f} {c['beta_property']:>10.4f}  "
              f"{r2:>6} {eq['default_events']:>7}  {src}")
    if rep["drivers"]["source"] == "cycle_index_proxy":
        print("WARNING: " + rep["drivers"]["warning"])
    print(f"-> {args.output}" + (f", report {args.report}" if args.report else ""))
    return 0


def register(sub) -> None:
    """Add the estimate-satellites sub-command to the sora-tools parser."""
    e = sub.add_parser("estimate-satellites",
                       help="estimate satellite coefficients (macro -> PD/TR) from the SIM stage history")
    e.add_argument("sim", help="SIM directory (sim_stage_history, sim_exposure, sim_counterparty)")
    e.add_argument("-o", "--output", required=True,
                   help="satellite CSV (layout of tests/params/synthetic_satellites.csv)")
    e.add_argument("--report", help="JSON fit report")
    d = e.add_mutually_exclusive_group(required=True)
    d.add_argument("--macro-history", help="historical macro series (long CSV: variable,key,period|year,value)")
    d.add_argument("--cycle-index", help="PROXY: reference/macro_cycle_index.csv, mapped to GDP growth")
    e.add_argument("--macro-key", default="EU", help="country/region key of --macro-history (default EU)")
    e.add_argument("--gdp-per-cycle", type=float, default=5.0,
                   help="proxy: GDP growth pp per unit of the cycle index (default 5)")
    e.add_argument("--normal-gdp-growth", type=float, default=1.5, help="proxy: GDP growth at cycle 0 (default 1.5)")
    e.add_argument("--prior", help="satellite CSV to take lgd_property_sensitivity from (not estimated)")
    e.add_argument("--window", type=int, default=12, help="rolling window in months (default 12)")
    e.add_argument("--lag", type=int, default=0, help="driver lag in months (default 0)")
    e.add_argument("--min-events", type=int, default=30,
                   help="default events (S1/S2->S3) a portfolio needs for its own fit (default 30)")
    e.add_argument("--start", help="first month (YYYY-MM) of the stage history to use")
    e.add_argument("--end", help="last month (YYYY-MM) of the stage history to use")
    e.add_argument("--transitions", nargs="+", choices=list(TRANSITIONS), default=list(DEFAULT_TRANSITIONS),
                   help="transition rates in the equation (default pd_perf: S1/S2 -> S3); several share the slopes")
    e.add_argument("--no-trend", dest="trend", action="store_false",
                   help="no linear time trend per portfolio and transition (default: trend on)")
    e.add_argument("--exposure-types", nargs="+", default=list(DEFAULT_EXPOSURE_TYPES))
    e.set_defaults(func=cmd_estimate_satellites)
