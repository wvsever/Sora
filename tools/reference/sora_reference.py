#!/usr/bin/env python3
"""Sora reference implementation: segmentation, calibration and the EBA credit-risk projection.

An independent, deliberately simple implementation used to produce golden results for the C++ engine.
It shares no code with the engine. It works on SEGMENT TOTALS, while the engine works per contract. Because
the EBA formulas are linear in exposure for given segment parameters, both must agree (to rounding).

    python tools/reference/sora_reference.py --sim <SIM dir> --scenario tests/scenarios/test_eba2025.yaml \
        --out tests/golden/20260630

Method (see plans/03_scenario_engine.md and plans/09_risk_parameters.md):

1. Scope and segments. Amortised-cost loans, finance leases and debt securities, excluding intragroup.
   Segment = instrument | EBA portfolio | country bucket (top N countries by exposure, others OTHER).
   Amounts are converted to EUR at the reference-date rate.
2. Calibration (starting point, year 0).
   * Monthly stage transition matrix per segment from consecutive month-ends in sim_stage_history,
     weighted by gross carrying amount. Rows with fewer than `min_observations` contract-months fall back to
     the parent segment: (instrument|portfolio|ALL), then (instrument|ALL|ALL), then (ALL|ALL|ALL).
   * 12-month rates are the 12th power of the monthly matrix. For PD/TR, stage 3 is absorbing; cures use the
     unrestricted matrix.
   * LGD_S3 = stage 3 coverage, LRLT_S2 = stage 2 coverage, LGD_S1 = LGD_S2 = LGD_S3 (EBA MN para 110
     approximation). Empty stocks fall back to the parent segment. Central banks get zero loss rates (para 146).
3. Projection (years 1..3, baseline and adverse) with a synthetic satellite model:
   z_t = b_gdp*(gdp_t - normal) + b_u*(u_t - u_0) + b_p*property_growth_t
   logit(PD_t) = logit(PD_0) + z_t for PD12M_S1, PD12M_S2 and TR1-2. For TR2-1 the sign is reversed.
   LGD/LR_t = LGD/LR_0 * (1 + s * max(0, -cumulative property growth_t)), capped at 1 (secured portfolios).
   Stage flows and provisions follow EBA 2027 draft MN Boxes 3-9. There are no cures from S3, the balance sheet
   is static, POCI is static, the old S3 floor applies per exposure (para 141), and in the final adverse year the t+2
   loss term is blended 5/6 adverse + 1/6 baseline.
4. Collateral and LTV (static balance sheet). Real-estate collateral allocated to in-scope exposures is revalued
   with the cumulative residential/commercial property price growth of its property country (country fallback as
   for the macro key); other collateral is unchanged. Allocated amounts are converted at the reference-date FX
   rate; a NULL amount gets the market value pro rata to the GCA of the in-scope exposures the collateral is
   allocated to. LTV per t0 stage = t0 GCA of exposures with real-estate collateral / their collateral value.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

import duckdb
import yaml

STAGES = ("stage1", "stage2", "stage3")
PARAMS = ("pd12m_s1", "pd12m_s2", "tr1_2", "tr2_1", "tr3_1", "tr3_2", "lgd_s1", "lgd_s2", "lgd_s3", "lrlt_s2")
PD_LIKE = ("pd12m_s1", "pd12m_s2", "tr1_2")
LOSS_LIKE = ("lgd_s1", "lgd_s2", "lgd_s3", "lrlt_s2")


# ----------------------------------------------------------------------------------------- data

def connect(sim: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for t in ("sim_exposure", "sim_counterparty", "sim_fx_rate", "sim_stage_history"):
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{sim}/{t}/**/*.parquet', hive_partitioning=false)")
    for t, cols in (("sim_collateral", "collateral_id VARCHAR, collateral_type VARCHAR, currency VARCHAR, "
                                        "market_value DECIMAL(18,2), property_country VARCHAR"),
                    ("sim_collateral_allocation", "exposure_id VARCHAR, collateral_id VARCHAR, allocated_amount DECIMAL(18,2)")):
        if any((sim / t).glob("**/*.parquet")):
            con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{sim}/{t}/**/*.parquet', hive_partitioning=false)")
        else:                                                  # optional tables: empty if absent
            con.execute(f"CREATE TABLE {t} ({cols})")
    return con


SEGMENT_SQL = """
WITH fx AS (
    SELECT currency, CAST(rate_to_reporting AS DOUBLE) AS r FROM sim_fx_rate WHERE rate_date = DATE '{ref}'
    UNION SELECT '{ccy}', 1.0
),
e AS (
    SELECT e.*, c.eba_sector, c.is_sme,
           coalesce(e.country_of_risk, c.country_of_residence) AS country,
           CASE WHEN e.exposure_type = 'debt_security' THEN 'DEBT_SEC' ELSE 'LOANS' END AS instrument,
           fx.r AS fx
    FROM sim_exposure e
    JOIN sim_counterparty c USING (counterparty_id)
    JOIN fx ON fx.currency = e.currency
    WHERE e.measurement_category IN ({mc}) AND e.exposure_type IN ({et})
      AND NOT ({excl} AND coalesce(e.is_intragroup, false))
)
SELECT exposure_id, instrument,
    CASE eba_sector
        WHEN 'central_bank' THEN 'CB'
        WHEN 'general_government' THEN 'GG'
        WHEN 'credit_institution' THEN 'CI'
        WHEN 'other_financial' THEN 'OFC'
        WHEN 'non_financial_corporation' THEN
            CASE WHEN instrument = 'DEBT_SEC' THEN 'NFC'
                 ELSE 'NFC_' || CASE WHEN coalesce(is_sme, false) THEN 'SME' ELSE 'LARGE' END
                      || CASE WHEN coalesce(is_cre, false) THEN '_CRE' ELSE '_OTHER' END END
        WHEN 'household' THEN
            CASE WHEN instrument = 'DEBT_SEC' THEN 'HH_OTHER'
                 WHEN household_purpose = 'house_purchase' THEN 'HH_HOUSE'
                 WHEN household_purpose = 'consumption' THEN 'HH_CONS'
                 ELSE 'HH_OTHER' END
    END AS portfolio,
    country, stage,
    CAST(gross_carrying_amount AS DOUBLE) * fx AS gca,
    CAST(coalesce(loss_allowance, 0) AS DOUBLE) * fx AS allowance,
    fx
FROM e
ORDER BY exposure_id
"""


def load_exposures(con, cfg, manifest) -> list[dict]:
    q = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731
    sql = SEGMENT_SQL.format(ref=manifest["reference_date"], ccy=manifest["reporting_currency"],
                             mc=q(cfg["scope"]["measurement_categories"]), et=q(cfg["scope"]["exposure_types"]),
                             excl="true" if cfg["scope"]["exclude_intragroup"] else "false")
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    # Country buckets: top N by exposure (ties by country code), others OTHER.
    by_country = defaultdict(float)
    for r in rows:
        by_country[r["country"]] += r["gca"]
    top = sorted(by_country, key=lambda c: (-by_country[c], c))[: cfg["segmentation"]["top_countries"]]
    for r in rows:
        r["bucket"] = r["country"] if r["country"] in top else "OTHER"
        r["segment"] = f"{r['instrument']}|{r['portfolio']}|{r['bucket']}"
    return rows


def parents(segment: str) -> list[str]:
    i, p, _ = segment.split("|")
    return [segment, f"{i}|{p}|ALL", f"{i}|ALL|ALL", "ALL|ALL|ALL"]


# ----------------------------------------------------------------------------------------- calibration

def matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def matpow(m, n):
    r = [[1.0 if i == j else 0.0 for j in range(3)] for i in range(3)]
    for _ in range(n):
        r = matmul(r, m)
    return r


def transition_counts(con, exposures, cfg) -> dict:
    """{level_key: {from_stage: {'n': int, 'w': float, 'to': {stage: weight}}}} for all hierarchy levels."""
    seg = {r["exposure_id"]: (r["segment"], r["fx"]) for r in exposures}
    rows = con.execute("""
        SELECT a.exposure_id, a.stage, b.stage, CAST(a.gross_carrying_amount AS DOUBLE)
        FROM sim_stage_history a
        JOIN sim_stage_history b
          ON b.exposure_id = a.exposure_id AND b.period_end = last_day(a.period_end + INTERVAL 1 DAY)
        WHERE a.stage IN ('stage1','stage2','stage3') AND b.stage IN ('stage1','stage2','stage3')
          AND a.gross_carrying_amount IS NOT NULL
        ORDER BY a.exposure_id, a.period_end
    """).fetchall()
    acc: dict = defaultdict(lambda: defaultdict(lambda: {"n": 0, "w": 0.0, "to": defaultdict(float)}))
    for eid, s_from, s_to, gca in rows:
        if eid not in seg:
            continue
        segment, fx = seg[eid]
        w = gca * fx
        for key in parents(segment):
            cell = acc[key][s_from]
            cell["n"] += 1
            cell["w"] += w
            cell["to"][s_to] += w
    return acc


def stock_ratios(exposures) -> dict:
    """{level_key: {stage: (gca, allowance)}} for all hierarchy levels."""
    acc: dict = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    for r in exposures:
        for key in parents(r["segment"]):
            acc[key][r["stage"]][0] += r["gca"]
            acc[key][r["stage"]][1] += r["allowance"]
    return acc


def calibrate(segments, counts, stocks, cfg) -> tuple[dict, dict]:
    """Starting-point parameters per segment, and the hierarchy level each one came from."""
    min_obs, floor = cfg["calibration"]["min_observations"], cfg["calibration"]["pd_floor"]
    params, source = {}, {}
    for s in segments:
        m, src = [], {}
        for i, st in enumerate(STAGES):
            level = next((k for k in parents(s) if counts[k][st]["n"] >= min_obs and counts[k][st]["w"] > 0), None)
            if level is None:
                row = [1.0 if j == i else 0.0 for j in range(3)]
                src[st] = "none"
            else:
                cell = counts[level][st]
                row = [cell["to"][t] / cell["w"] for t in STAGES]
                src[st] = level
            m.append(row)
        absorbing = [m[0], m[1], [0.0, 0.0, 1.0]]
        a12, c12 = matpow(absorbing, 12), matpow(m, 12)
        p = {
            "pd12m_s1": max(a12[0][2], floor), "tr1_2": a12[0][1],
            "pd12m_s2": max(a12[1][2], floor), "tr2_1": a12[1][0],
            "tr3_1": c12[2][0], "tr3_2": c12[2][1],
        }

        def coverage(stage):
            for k in parents(s):
                g, a = stocks[k][stage]
                if g > 0:
                    return a / g, k
            return 0.0, "none"

        p["lgd_s3"], src["lgd"] = coverage("stage3")
        p["lrlt_s2"], src["lrlt"] = coverage("stage2")
        p["lgd_s1"] = p["lgd_s2"] = p["lgd_s3"]
        if s.split("|")[1] == "CB":                      # EBA MN para 146: zero loss rate for central banks
            for k in LOSS_LIKE:
                p[k] = 0.0
        params[s], source[s] = p, src
    return params, source


# ----------------------------------------------------------------------------------------- scenario

def load_macro(path: Path) -> dict:
    macro = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            if r["tenor"] or r["sector"]:
                continue
            macro[(r["variable"], r["key"], r["scenario"], int(r["year"]))] = float(r["value"])
    return macro


def macro_key(macro, country, cfg):
    if country != "OTHER" and ("real_gdp", country, "baseline", cfg["year_map"][1]) in macro:
        return country
    for k in (cfg["country_fallback"] if country != "OTHER" else ["EU"]):
        if ("real_gdp", k, "baseline", cfg["year_map"][1]) in macro:
            return k
    raise KeyError(country)


def logit(p):
    p = min(max(p, 1e-12), 1 - 1e-12)
    return math.log(p / (1 - p))


def expit(x):
    return 1 / (1 + math.exp(-x))


def project_parameters(segment, p0, sat, macro, scenario, cfg) -> dict[int, dict]:
    """Parameters for years 1..3 (and 4 = flat continuation) under one scenario."""
    _, portfolio, bucket = segment.split("|")
    key = macro_key(macro, bucket, cfg)
    b = sat[portfolio]
    u0 = macro.get(("unemployment_rate", key, "historical", cfg["history_year"]))
    prop_var = "residential_property_prices" if portfolio == "HH_HOUSE" else "commercial_property_prices"
    out, cum_prop = {}, 1.0
    for t in (1, 2, 3):
        y = cfg["year_map"][t]
        gdp = macro[("real_gdp", key, scenario, y)]
        u = macro.get(("unemployment_rate", key, scenario, y), u0)
        hp = macro.get((prop_var, key, scenario, y), 0.0)
        cum_prop *= 1 + hp / 100
        z = (b["beta_gdp"] * (gdp - cfg["normal_gdp_growth"])
             + b["beta_unemployment"] * ((u - u0) if (u is not None and u0 is not None) else 0.0)
             + b["beta_property"] * hp)
        p = dict(p0)
        for k in PD_LIKE:
            p[k] = max(expit(logit(p0[k]) + z), cfg["calibration"]["pd_floor"]) if p0[k] > 0 else 0.0
        p["tr2_1"] = expit(logit(p0["tr2_1"]) - z) if p0["tr2_1"] > 0 else 0.0
        # Keep stage outflows feasible.
        p["tr1_2"] = min(p["tr1_2"], 1 - p["pd12m_s1"])
        p["tr2_1"] = min(p["tr2_1"], 1 - p["pd12m_s2"])
        mult = 1 + b["lgd_property_sensitivity"] * max(0.0, 1 - cum_prop)
        for k in LOSS_LIKE:
            p[k] = min(p0[k] * mult, 1.0)
        out[t] = p
    out[4] = dict(out[3])            # after the horizon: flat
    return out


def project_segment(stock, params_by_scen, cfg, s3_exposures=None) -> list[dict]:
    """Stage flows and provisions per EBA MN Boxes 3-9 for one segment. Returns one row per scenario and year.

    `s3_exposures` is a list of (gross carrying amount, provision) of the stage 3 exposures at t0. Box 9's
    no-release floor applies per exposure (MN para 141). Without the list, it is applied to the segment total.
    """
    rows = []
    w_adv, w_base = cfg["constraints"]["adverse_final_year_blend"]
    for scen, P in params_by_scen.items():
        e1, e2, e3old = stock["stage1"][0], stock["stage2"][0], stock["stage3"][0]
        e3new, epoci = 0.0, stock["poci"][0]
        prov_s3_0 = stock["stage3"][1]
        cum13 = cum23 = 0.0
        prev_total = stock["stage1"][1] + stock["stage2"][1] + stock["stage3"][1] + stock["poci"][1]
        if s3_exposures is None:
            old3 = max(e3old * P[1]["lgd_s3"], prov_s3_0)                               # Box 9
        else:                                                                          # Box 9, per exposure (para 141)
            old3 = sum(max(g * P[1]["lgd_s3"], a) for g, a in s3_exposures)
        for t in (0, 1, 2):
            p1, p2 = P[t + 1], P[t + 2]
            f12, f21 = e1 * p1["tr1_2"], e2 * p1["tr2_1"]
            f13, f23 = e1 * p1["pd12m_s1"], e2 * p1["pd12m_s2"]
            loss_next = p2["pd12m_s1"] * p2["lgd_s1"]
            if scen == "adverse" and t + 1 == 3:                                           # Boxes 4-5, final year
                pb = params_by_scen["baseline"][3]
                loss_next = w_adv * p2["pd12m_s1"] * p2["lgd_s1"] + w_base * pb["pd12m_s1"] * pb["lgd_s1"]
            prov11 = e1 * (1 - p1["tr1_2"] - p1["pd12m_s1"]) * loss_next                 # Box 5
            prov21 = f21 * loss_next                                                       # Box 4
            prov12 = f12 * p1["lrlt_s2"]                                                   # Box 6
            prov22 = e2 * (1 - p1["tr2_1"] - p1["pd12m_s2"]) * p1["lrlt_s2"]               # Box 7
            cum13 += f13 * p1["lgd_s1"]                                                    # Box 8
            cum23 += f23 * p1["lgd_s2"]
            n1, n2 = e1 - f12 - f13 + f21, e2 - f21 - f23 + f12
            e3new += f13 + f23
            e1, e2 = n1, n2
            s1, s2, s3 = prov11 + prov21, prov12 + prov22, cum13 + cum23 + old3            # Box 3
            total = s1 + s2 + s3 + stock["poci"][1]
            rows.append({
                "scenario": scen, "year": t + 1,
                "exp_s1": e1, "exp_s2": e2, "exp_s3_old": e3old, "exp_s3_new": e3new, "exp_poci": epoci,
                "flow_s1_s2": f12, "flow_s2_s1": f21, "flow_s1_s3": f13, "flow_s2_s3": f23,
                "prov_s1_s1": prov11, "prov_s2_s1": prov21, "prov_s1_s2": prov12, "prov_s2_s2": prov22,
                "prov_cum_s1_s3": cum13, "prov_cum_s2_s3": cum23, "prov_old_s3": old3,
                "prov_stock_s1": s1, "prov_stock_s2": s2, "prov_stock_s3": s3, "prov_stock_poci": stock["poci"][1],
                "impairment": total - prev_total,
            })
            prev_total = total
    return rows


# ----------------------------------------------------------------------------------------- collateral and LTV

LTV_SLOTS = (("actual", 0), ("baseline", 1), ("baseline", 2), ("baseline", 3),
             ("adverse", 1), ("adverse", 2), ("adverse", 3))
PROPERTY_VARIABLE = {"residential_property": "residential_property_prices",
                     "commercial_property": "commercial_property_prices"}


def collateral_index(collateral_type, country, macro, cfg) -> dict:
    """{(scenario, year): value index} for years 0..3; 1 throughout for non-property collateral."""
    idx = {(sc, 0): 1.0 for sc in ("baseline", "adverse")}
    var = PROPERTY_VARIABLE.get(collateral_type)
    key = macro_key(macro, country or "", cfg) if var else None
    for sc in ("baseline", "adverse"):
        for t in (1, 2, 3):
            g = macro.get((var, key, sc, cfg["year_map"][t]), 0.0) if var else 0.0
            idx[(sc, t)] = idx[(sc, t - 1)] * (1 + g / 100)
    return idx


def allocated_values(allocations, gca_by_exposure) -> list[tuple]:
    """(exposure_id, collateral type, property country, value in reporting currency) per in-scope allocation.

    `allocations` rows: (exposure_id, collateral_id, allocated_amount or None, type, market_value, country, fx).
    Allocations to exposures missing from `gca_by_exposure` (out of scope) are dropped. A None amount gets the
    market value pro rata to the GCA of the in-scope exposures of that collateral (equal shares if that is 0)."""
    rows = [a for a in allocations if a[0] in gca_by_exposure]
    base, links = defaultdict(float), defaultdict(int)
    for eid, cid, *_ in rows:
        base[cid] += gca_by_exposure[eid]
        links[cid] += 1
    out = []
    for eid, cid, amount, ctype, mv, country, fx in rows:
        if fx is None:                                         # only real-estate collateral needs a value
            if ctype in PROPERTY_VARIABLE:
                raise ValueError(f"no FX rate at the reference date for collateral {cid}")
            continue
        if amount is not None:
            v = amount * fx
        elif base[cid] > 0:
            v = mv * fx * gca_by_exposure[eid] / base[cid]
        else:
            v = mv * fx / links[cid]
        out.append((eid, ctype, country, v))
    return out


def collateral_ltv(con, exposures, macro, cfg, manifest) -> dict:
    """{segment: {(scenario, year): [secured exp S1, S2, S3, RE collateral S1, S2, S3]}} for all segments."""
    allocations = con.execute(f"""
        WITH fx AS (
            SELECT currency, CAST(rate_to_reporting AS DOUBLE) AS r FROM sim_fx_rate
            WHERE rate_date = DATE '{manifest["reference_date"]}'
            UNION SELECT '{manifest["reporting_currency"]}', 1.0
        )
        SELECT a.exposure_id, a.collateral_id, CAST(a.allocated_amount AS DOUBLE), c.collateral_type,
               CAST(c.market_value AS DOUBLE), c.property_country, fx.r
        FROM sim_collateral_allocation a
        JOIN sim_collateral c USING (collateral_id)
        LEFT JOIN fx ON fx.currency = c.currency
        ORDER BY a.exposure_id, a.collateral_id
    """).fetchall()
    exp = {r["exposure_id"]: r for r in exposures}
    values = allocated_values(allocations, {k: r["gca"] for k, r in exp.items()})
    indices, re_value = {}, {}                                    # re_value: exposure -> {slot: value}
    for eid, ctype, country, v in values:
        if ctype not in PROPERTY_VARIABLE:
            continue
        if (ctype, country) not in indices:
            indices[(ctype, country)] = collateral_index(ctype, country, macro, cfg)
        idx = indices[(ctype, country)]
        by_slot = re_value.setdefault(eid, defaultdict(float))
        for sc, t in LTV_SLOTS:
            by_slot[(sc, t)] += v * idx[("baseline" if sc == "actual" else sc, t)]
    out = {s: {slot: [0.0] * 6 for slot in LTV_SLOTS} for s in sorted({r["segment"] for r in exposures})}
    for eid, by_slot in re_value.items():
        r = exp[eid]
        if r["stage"] not in STAGES:                              # POCI: no LTV column
            continue
        i = STAGES.index(r["stage"])
        for slot in LTV_SLOTS:
            out[r["segment"]][slot][i] += r["gca"]
            out[r["segment"]][slot][3 + i] += by_slot[slot]
    return out


def write_collateral(path: Path, ltv: dict):
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["segment", "scenario", "year", "secured_exp_s1", "secured_exp_s2", "secured_exp_s3",
                    "re_collateral_s1", "re_collateral_s2", "re_collateral_s3", "ltv_s1", "ltv_s2", "ltv_s3"])
        for s, by_slot in ltv.items():
            for (sc, t), v in by_slot.items():
                ratios = [f"{v[i] / v[3 + i]:.9f}" if v[3 + i] > 0 else "" for i in range(3)]
                w.writerow([s, sc, t, *(f"{x:.2f}" for x in v), *ratios])


# ----------------------------------------------------------------------------------------- main

def run(sim: Path, scenario_path: Path, out: Path, repo: Path) -> dict:
    cfg = yaml.safe_load(scenario_path.read_text())
    manifest = json.loads((sim / "sim_manifest.json").read_text())
    con = connect(sim)
    exposures = load_exposures(con, cfg, manifest)
    segments = sorted({r["segment"] for r in exposures})
    counts = transition_counts(con, exposures, cfg)
    stocks = stock_ratios(exposures)
    params0, sources = calibrate(segments, counts, stocks, cfg)

    with open(repo / cfg["satellites"]) as f:
        sat = {r["portfolio"]: {k: float(v) for k, v in r.items() if k not in ("portfolio", "description")}
               for r in csv.DictReader(f)}
    macro = load_macro(repo / cfg["macro_path"])

    seg_stock = {s: {st: [0.0, 0.0] for st in (*STAGES, "poci")} for s in segments}
    counts_by_seg = defaultdict(int)
    s3_by_seg = defaultdict(list)
    for r in exposures:
        if r["stage"] == "stage3":
            s3_by_seg[r["segment"]].append((r["gca"], r["allowance"]))
        seg_stock[r["segment"]][r["stage"]][0] += r["gca"]
        seg_stock[r["segment"]][r["stage"]][1] += r["allowance"]
        counts_by_seg[r["segment"]] += 1

    out.mkdir(parents=True, exist_ok=True)
    fmt = lambda x: f"{x:.2f}"          # noqa: E731  money
    fmtp = lambda x: f"{x:.9f}"         # noqa: E731  rates

    # segments.csv: t0 stocks
    with open(out / "segments.csv", "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["segment", "instrument", "portfolio", "country", "macro_key", "contracts",
                    "exp_s1", "exp_s2", "exp_s3", "exp_poci", "prov_s1", "prov_s2", "prov_s3", "prov_poci"])
        for s in segments:
            i, p, c = s.split("|")
            st = seg_stock[s]
            w.writerow([s, i, p, c, macro_key(macro, c, cfg), counts_by_seg[s],
                        *(fmt(st[k][0]) for k in (*STAGES, "poci")), *(fmt(st[k][1]) for k in (*STAGES, "poci"))])

    # parameters.csv: starting point and projections (the sim_risk_parameter layout)
    projected = {}
    with open(out / "parameters.csv", "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["level", "key", "scenario", "year", *PARAMS, "source", "calibration_levels"])
        for s in segments:
            lv = ";".join(f"{k}={v}" for k, v in sorted(sources[s].items()))
            w.writerow(["segment", s, "actual", 0, *(fmtp(params0[s][k]) for k in PARAMS), "derived", lv])
            projected[s] = {}
            for scen in ("baseline", "adverse"):
                P = project_parameters(s, params0[s], sat, macro, scen, cfg)
                projected[s][scen] = P
                for t in (1, 2, 3):
                    w.writerow(["segment", s, scen, t, *(fmtp(P[t][k]) for k in PARAMS), "derived", ""])

    # projection.csv
    totals = defaultdict(lambda: defaultdict(float))
    fields = None
    with open(out / "projection.csv", "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        for s in segments:
            for row in project_segment(seg_stock[s], projected[s], cfg, s3_by_seg[s]):
                if fields is None:
                    fields = list(row)
                    w.writerow(["segment", *fields])
                w.writerow([s, row["scenario"], row["year"], *(fmt(row[k]) for k in fields[2:])])
                for k in fields[2:]:
                    totals[(row["scenario"], row["year"])][k] += row[k]

    write_collateral(out / "collateral.csv", collateral_ltv(con, exposures, macro, cfg, manifest))

    summary = {
        "reference_date": manifest["reference_date"], "sim_mapping_release": manifest.get("mapping_release"),
        "scenario": cfg["name"], "segments": len(segments), "exposures": len(exposures),
        "starting_point": {k: round(sum(seg_stock[s][st][i] for s in segments), 2)
                           for k, st, i in (("exp_s1", "stage1", 0), ("exp_s2", "stage2", 0), ("exp_s3", "stage3", 0),
                                            ("exp_poci", "poci", 0), ("prov_s1", "stage1", 1), ("prov_s2", "stage2", 1),
                                            ("prov_s3", "stage3", 1), ("prov_poci", "poci", 1))},
        "totals": {f"{sc}/{y}": {k: round(v, 2) for k, v in d.items()} for (sc, y), d in sorted(totals.items())},
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sim", type=Path, required=True)
    ap.add_argument("--scenario", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    repo = Path(__file__).resolve().parents[2]
    s = run(args.sim, args.scenario, args.out, repo)
    print(json.dumps({k: s[k] for k in ("segments", "exposures", "starting_point")}, indent=2))
    for k, v in s["totals"].items():
        print(f"{k:12s} impairment {v['impairment']:>16,.2f}  S3 exposure {v['exp_s3_old'] + v['exp_s3_new']:>18,.2f}")


if __name__ == "__main__":
    main()
