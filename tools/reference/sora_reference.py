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
5. Off-balance items (scenario key `off_balance`, CR_SCEN_OFF_BS). Staged commitments of the configured exposure
   types (same measurement and intragroup scope), nominal = off_balance_amount. Each item takes the parameter path of
   the on-balance segment LOANS|portfolio|bucket of its counterparty (the portfolio's OTHER bucket if that segment
   does not exist). CCF: customer `ccf` (sim_risk_parameter, actual/0, exposure row then segment hierarchy), else the
   scenario's regulatory fallback (CRR Art. 111(2)). Post-CCF amount = CCF x nominal is projected with Boxes 3-9
   exactly as on-balance exposures (starting provision = undrawn share of the facility's allowance, old S3 floor
   per item); the nominal amount follows the same stage flows.
6. Facilities with a drawn and an undrawn part (off_balance.include_loan_undrawn, commitment_drawn_on_balance).
   The undrawn part of in-scope loans is an off-balance item (exposure type `loan`, reported as a loan commitment
   given) in the loan's own segment, CCF fallback of a loan commitment. The drawn part (GCA > 0) of staged commitments
   of the off-balance types is an on-balance exposure in step 1 (segments, top countries, calibration, projection).
   Whenever the undrawn part is off-balance, the facility's allowance is split pro rata: drawn share
   allowance x GCA / (GCA + undrawn) on-balance (also in the coverage calibration), the rest off-balance.
7. ECB benchmarks (scenario key `benchmark_parameters`, EBA 2027 draft MN paras 115-117 and 146). Model coverage
   per pivot asset class (instrument|portfolio) and parameter group (PD/TR, LGD/LR) = share of t0 exposure whose
   group starting point was calibrated within the pivot class (segment or portfolio level) and whose portfolio has
   a satellite model. General governments take the benchmark of their own country (mandatory); a pivot class below
   the coverage threshold (10%) takes the benchmark for all its segments; above it, only its segments without a
   model do. The benchmark (segment country, then the country fallback) replaces the projected parameters of the
   group for years 1..3 without adjustment; the starting point stays the institution's own.
8. Sectoral (GVA) satellites (scenario key `sector_satellites`, EBA 2027 draft MN para 114, template guidance paras
   44-45). NFC exposures whose NACE sector has coefficients take a sector path: the sector's real GVA growth replaces
   GDP growth in the PD/TR index (beta_gva), and the cumulative GVA decline raises LGD/LR (lgd_gva_sensitivity), for
   the groups the sector has coefficients for. Countries without sectoral GVA (non-EU) use their GDP growth plus the
   sector's GVA deviation from GDP in the `gva_fallback` key (EU). Customer overlays and the ECB benchmark apply on top
   (the benchmark wins); a segment whose exposures all have a sectoral model counts as modelled for the benchmark rule.
   Segments are projected as the sum of their parts with the same path (projection.csv, CR_SECTOR); off-balance items
   take the path of their counterparty's sector.
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
    SELECT e.*, c.eba_sector, c.is_sme, c.nace_code,
           coalesce(e.country_of_risk, c.country_of_residence) AS country,
           CASE WHEN e.exposure_type = 'debt_security' THEN 'DEBT_SEC' ELSE 'LOANS' END AS instrument,
           fx.r AS fx
    FROM sim_exposure e
    JOIN sim_counterparty c USING (counterparty_id)
    JOIN fx ON fx.currency = e.currency
    WHERE e.measurement_category IN ({mc})
      AND (e.exposure_type IN ({et})
           OR (e.exposure_type IN ({drawn}) AND e.gross_carrying_amount > 0
               AND e.stage IN ('stage1', 'stage2', 'stage3', 'poci')))
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
    country, stage, nace_code,
    CAST(gross_carrying_amount AS DOUBLE) * fx AS gca,
    CAST(coalesce(loss_allowance, 0) AS DOUBLE) * fx AS allowance,
    fx, exposure_type,
    CAST(coalesce(gross_carrying_amount, 0) AS DOUBLE) AS drawn,
    CAST(coalesce(off_balance_amount, 0) AS DOUBLE) AS undrawn,
    coalesce(is_unconditionally_cancellable, false) AS cancellable
FROM e
ORDER BY exposure_id
"""


def top_countries(rows, n) -> list[str]:
    """Country buckets: top N by exposure (ties by country code), largest first; the others are OTHER."""
    by_country = defaultdict(float)
    for r in rows:
        by_country[r["country"]] += r["gca"]
    return sorted(by_country, key=lambda c: (-by_country[c], c))[:n]


def load_exposures(con, cfg, manifest) -> list[dict]:
    q = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731
    drawn = drawn_on_balance_types(cfg)
    sql = SEGMENT_SQL.format(ref=manifest["reference_date"], ccy=manifest["reporting_currency"],
                             mc=q(cfg["scope"]["measurement_categories"]), et=q(cfg["scope"]["exposure_types"]),
                             drawn=q(drawn) if drawn else "''",
                             excl="true" if cfg["scope"]["exclude_intragroup"] else "false")
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    split_facility_allowance(rows, cfg)
    top = top_countries(rows, cfg["segmentation"]["top_countries"])
    for r in rows:
        r["bucket"] = r["country"] if r["country"] in top else "OTHER"
        r["segment"] = f"{r['instrument']}|{r['portfolio']}|{r['bucket']}"
    return rows



def drawn_on_balance_types(cfg) -> list[str]:
    """Commitment types whose drawn part (gross carrying amount) is an on-balance loans-and-advances exposure
    (scenario key off_balance.commitment_drawn_on_balance): the configured off-balance types, else none."""
    ob = cfg.get("off_balance") or {}
    return list(ob.get("exposure_types", [])) if ob.get("commitment_drawn_on_balance", False) else []


def undrawn_is_off_balance(r, cfg) -> bool:
    """Whether the undrawn part of an in-scope exposure is projected off-balance: commitments whose drawn part is
    on-balance, and loans with off_balance.include_loan_undrawn."""
    ob = cfg.get("off_balance") or {}
    return r["exposure_type"] in drawn_on_balance_types(cfg) or (
        r["exposure_type"] == "loan" and bool(ob.get("include_loan_undrawn", False)))


def split_facility_allowance(rows, cfg):
    """A facility's loss allowance covers its drawn and undrawn parts. When the undrawn part is projected
    off-balance, the on-balance provision is the drawn share, allowance x GCA / (GCA + undrawn), and the undrawn
    share goes with the off-balance item, so the facility's provision is counted once. `allowance_total` keeps the
    facility's allowance."""
    for r in rows:
        r["allowance_total"] = r["allowance"]
        if r["undrawn"] > 0 and undrawn_is_off_balance(r, cfg):
            r["allowance"] = r["allowance"] * (r["drawn"] / (r["drawn"] + r["undrawn"]))


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
    """{(variable, key, scenario, year): value}. Sector rows are kept for real GVA only, as variable
    `real_gva:<scenario sector>` (e.g. real_gva:C_high); other sector and tenor rows are not used."""
    macro = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            if r["tenor"] or (r["sector"] and r["variable"] != "real_gva"):
                continue
            var = f"real_gva:{r['sector']}" if r["sector"] else r["variable"]
            macro[(var, r["key"], r["scenario"], int(r["year"]))] = float(r["value"])
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


def project_parameters(segment, p0, sat, macro, scenario, cfg, sector=None) -> dict[int, dict]:
    """Parameters for years 1..3 (and 4 = flat continuation) under one scenario. `sector` (sector_model) is the
    sectoral (GVA) satellite of the exposure's NACE sector: its GVA growth replaces GDP growth in the PD/TR index
    (beta_gva), and the cumulative GVA decline raises LGD/LR (lgd_gva_sensitivity), for the groups it covers."""
    _, portfolio, bucket = segment.split("|")
    key = macro_key(macro, bucket, cfg)
    b = sat[portfolio]
    u0 = macro.get(("unemployment_rate", key, "historical", cfg["history_year"]))
    prop_var = "residential_property_prices" if portfolio == "HH_HOUSE" else "commercial_property_prices"
    out, cum_prop, cum_gva = {}, 1.0, 1.0
    for t in (1, 2, 3):
        y = cfg["year_map"][t]
        gdp = macro[("real_gdp", key, scenario, y)]
        u = macro.get(("unemployment_rate", key, scenario, y), u0)
        hp = macro.get((prop_var, key, scenario, y), 0.0)
        cum_prop *= 1 + hp / 100
        if sector is not None:
            gva = sector_growth(sector, macro, scenario, y)
            cum_gva *= 1 + gva / 100
        if sector is not None and sector["beta_gva"] is not None:
            activity = sector["beta_gva"] * (gva - cfg["normal_gdp_growth"])
        else:
            activity = b["beta_gdp"] * (gdp - cfg["normal_gdp_growth"])
        z = (activity
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
        if sector is not None and sector["lgd_gva_sensitivity"] is not None:
            mult += sector["lgd_gva_sensitivity"] * max(0.0, 1 - cum_gva)
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


# ----------------------------------------------------------------------------------------- off-balance (CR_SCEN_OFF_BS)

OFF_BALANCE_TYPES = ("loan_commitment", "financial_guarantee", "other_commitment")
# CR_SCEN_OFF_BS commitment type of off_balance.csv rows whose exposure type is not a template type: the undrawn
# part of on-balance loans is a loan commitment given (FINREP F 09.01).
TEMPLATE_TYPE = {"loan": "loan_commitment"}
OFF_BALANCE_LABELS = {"loan_commitment": "Loan commitments given", "financial_guarantee": "Financial guarantees given",
                      "other_commitment": "Other Commitments given"}
OFF_BALANCE_SECTORS = (("CB", "Central banks"), ("GG", "General governments"), ("CI", "Credit institutions"),
                       ("OFC", "Other financial corporations"), ("NFC", "Non-financial corporations"),
                       ("HH", "Households"))
# Regulatory fallback (CRR Art. 111(2) and Annex I buckets). `unconditionally_cancellable` applies to loan and
# other commitments that the institution may cancel at any time (CRR3 10%, MN 2025 Table on Art. 495d).
DEFAULT_CCF = {"loan_commitment": 0.4, "financial_guarantee": 1.0, "other_commitment": 0.5,
               "unconditionally_cancellable": 0.1}
NOMINAL = ("nom_s1", "nom_s2", "nom_s3_old", "nom_s3_new", "nom_poci")
POST_CCF = ("exp_s1", "exp_s2", "exp_s3_old", "exp_s3_new", "exp_poci")
OFF_BALANCE_FIELDS = (*NOMINAL, *POST_CCF, "flow_s1_s2", "flow_s2_s1", "flow_s1_s3", "flow_s2_s3",
                      "prov_s1_s1", "prov_s2_s1", "prov_s1_s2", "prov_s2_s2", "prov_cum_s1_s3", "prov_cum_s2_s3",
                      "prov_old_s3", "prov_stock_s1", "prov_stock_s2", "prov_stock_s3", "prov_stock_poci", "impairment")

OFF_BALANCE_SQL = """
WITH fx AS (
    SELECT currency, CAST(rate_to_reporting AS DOUBLE) AS r FROM sim_fx_rate WHERE rate_date = DATE '{ref}'
    UNION SELECT '{ccy}', 1.0
)
SELECT e.exposure_id, e.exposure_type,
    CASE c.eba_sector
        WHEN 'central_bank' THEN 'CB'
        WHEN 'general_government' THEN 'GG'
        WHEN 'credit_institution' THEN 'CI'
        WHEN 'other_financial' THEN 'OFC'
        WHEN 'non_financial_corporation' THEN
            'NFC_' || CASE WHEN coalesce(c.is_sme, false) THEN 'SME' ELSE 'LARGE' END
                   || CASE WHEN coalesce(e.is_cre, false) THEN '_CRE' ELSE '_OTHER' END
        WHEN 'household' THEN
            CASE WHEN e.household_purpose = 'house_purchase' THEN 'HH_HOUSE'
                 WHEN e.household_purpose = 'consumption' THEN 'HH_CONS'
                 ELSE 'HH_OTHER' END
    END AS portfolio,
    coalesce(e.country_of_risk, c.country_of_residence) AS country, e.stage, c.nace_code,
    CAST(coalesce(e.off_balance_amount, 0) AS DOUBLE) * fx.r AS nominal,
    CAST(coalesce(e.gross_carrying_amount, 0) AS DOUBLE) AS drawn,
    CAST(coalesce(e.off_balance_amount, 0) AS DOUBLE) AS undrawn,
    CAST(coalesce(e.loss_allowance, 0) AS DOUBLE) * fx.r AS allowance,
    coalesce(e.is_unconditionally_cancellable, false) AS cancellable
FROM sim_exposure e
JOIN sim_counterparty c USING (counterparty_id)
JOIN fx ON fx.currency = e.currency
WHERE e.measurement_category IN ({mc}) AND e.exposure_type IN ({et})
  AND e.stage IN ('stage1', 'stage2', 'stage3', 'poci')
  AND NOT ({excl} AND coalesce(e.is_intragroup, false))
ORDER BY e.exposure_id
"""


def customer_ccf(sim: Path) -> tuple[dict, dict]:
    """CCFs (scenario actual, year 0) from sim_risk_parameter, if the SIM has that table with a `ccf` column:
    ({exposure_id: ccf}, {segment level key: ccf})."""
    d = sim / "sim_risk_parameter"
    if any(d.glob("**/*.parquet")):
        src = f"read_parquet('{d}/**/*.parquet', hive_partitioning = false, union_by_name = true)"
    elif any(d.glob("**/*.csv")):
        src = f"read_csv('{d}/**/*.csv', header = true, all_varchar = true, hive_partitioning = false, union_by_name = true)"
    else:
        return {}, {}
    con = duckdb.connect()
    if "ccf" not in [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()]:
        return {}, {}
    by_exposure, by_level = {}, {}
    for level, key, ccf in con.execute(f"""
            SELECT CAST(level AS VARCHAR), CAST(key AS VARCHAR), CAST(ccf AS DOUBLE) FROM {src}
            WHERE CAST(scenario AS VARCHAR) = 'actual' AND CAST(year AS BIGINT) = 0 AND ccf IS NOT NULL""").fetchall():
        if not 0.0 <= ccf <= 1.0:
            raise ValueError(f"ccf outside [0, 1] for {level} {key}")
        (by_exposure if level == "exposure" else by_level)[key] = ccf
    return by_exposure, by_level


def item_ccf(item, segment, fallback, ccf_exposure, ccf_level) -> tuple[float, bool]:
    """(CCF, from customer parameters): exposure row, then the segment hierarchy, else the regulatory fallback."""
    if item["exposure_id"] in ccf_exposure:
        return ccf_exposure[item["exposure_id"]], True
    for k in parents(segment):
        if k in ccf_level:
            return ccf_level[k], True
    if item["cancellable"] and item["exposure_type"] != "financial_guarantee":
        return float(fallback["unconditionally_cancellable"]), False
    return float(fallback[item["exposure_type"]]), False


def loan_undrawn_items(exposures, cfg) -> list[dict]:
    """Off-balance items for the undrawn part of in-scope loans (off_balance.include_loan_undrawn): an undrawn
    credit facility is a loan commitment given (FINREP F 09; CRR Annex I bucket 3(a), or bucket 5 when
    unconditionally cancellable). The item keeps the loan's own on-balance segment, and its provision is the undrawn
    share of the loan's allowance (the drawn share stays on-balance, `split_facility_allowance`)."""
    if not (cfg.get("off_balance") or {}).get("include_loan_undrawn", False):
        return []
    items = []
    for r in exposures:
        if r["exposure_type"] != "loan" or r["undrawn"] <= 0 or r["stage"] not in (*STAGES, "poci"):
            continue
        base = r["undrawn"] + r["drawn"]
        items.append({"exposure_id": r["exposure_id"], "exposure_type": "loan", "ccf_type": "loan_commitment",
                      "segment": r["segment"], "portfolio": r.get("portfolio", ""), "nace_code": r.get("nace_code"),
                      "stage": r["stage"], "nominal": r["undrawn"] * r["fx"],
                      "allowance": r["allowance_total"] * (r["undrawn"] / base), "cancellable": r["cancellable"]})
    return items


def project_off_balance(con, sim, cfg, manifest, top, segments, projected, exposures=(), sector_paths=None,
                        coefficients=None, check_modelled=None) -> tuple[list[dict], dict]:
    """Off-balance items (EBA 2027 draft MN paras 78-82). Each item is projected with the parameters of the
    on-balance loan segment of its counterparty (same portfolio rules and country bucket), with the same stage flow
    and provision logic: the post-CCF amount (CCF x nominal) carries the flows and provisions, and the nominal
    amount follows the same stage flows. An NFC item whose counterparty's sector has a sectoral satellite takes the
    segment's path of that sector (`sector_paths`), as an on-balance exposure of the same counterparty does. Returns
    rows per segment, exposure type, scenario and year (including the starting point as actual/0), and run
    statistics. `check_modelled(segment, sector)` applies the on-balance satellite rule to each item (raises)."""
    sector_paths = sector_paths or {}
    ob = cfg["off_balance"]
    types = ob["exposure_types"]
    if any(t not in OFF_BALANCE_TYPES for t in types):
        raise ValueError(f"off_balance.exposure_types must be among {OFF_BALANCE_TYPES}")
    fallback = {**DEFAULT_CCF, **(ob.get("ccf_fallback") or {})}
    if any(not 0.0 <= float(v) <= 1.0 for v in fallback.values()):
        raise ValueError("off_balance.ccf_fallback values must be in [0, 1]")
    for key in ("include_loan_undrawn", "commitment_drawn_on_balance"):
        if not isinstance(ob.get(key, False), bool):
            raise ValueError(f"off_balance.{key} must be true or false")
    q = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731
    cur = con.execute(OFF_BALANCE_SQL.format(ref=manifest["reference_date"], ccy=manifest["reporting_currency"],
                                             mc=q(cfg["scope"]["measurement_categories"]), et=q(types),
                                             excl="true" if cfg["scope"]["exclude_intragroup"] else "false"))
    cols = [d[0] for d in cur.description]
    ccf_exposure, ccf_level = customer_ccf(sim)
    known = set(segments)
    groups: dict = {}
    stats = {"items": 0, "fallback_items": 0, "unmatched_items": 0, "customer_ccf_items": 0}
    items = []
    for item in (dict(zip(cols, r)) for r in cur.fetchall()):
        segment = f"LOANS|{item['portfolio']}|{item['country'] if item['country'] in top else 'OTHER'}"
        if segment not in known:                 # no on-balance loans of that portfolio and country: its OTHER bucket
            segment = f"LOANS|{item['portfolio']}|OTHER"
            if segment not in known:             # no on-balance loan segment to take parameters from
                stats["unmatched_items"] += 1
                continue
            stats["fallback_items"] += 1
        # The allowance of a facility covers its drawn and undrawn parts: the undrawn share is the off-balance
        # provision (the drawn part is on-balance).
        base = item["undrawn"] + item["drawn"]
        item["allowance"] = item["allowance"] * (item["undrawn"] / base) if base > 0 else item["allowance"]
        items.append({**item, "segment": segment, "ccf_type": item["exposure_type"]})
    # Undrawn part of on-balance loans (include_loan_undrawn): loan commitments given, in the loan's own segment.
    loan_items = loan_undrawn_items(exposures, cfg)
    stats["loan_undrawn_items"] = len(loan_items)
    # Commitments whose drawn part is on-balance (commitment_drawn_on_balance), in the on-balance scope.
    stats["commitment_drawn_exposures"] = sum(1 for r in exposures if r["exposure_type"] in OFF_BALANCE_TYPES)
    for item in items + loan_items:
        segment = item["segment"]
        if check_modelled:
            check_modelled(segment, sector_key(item, coefficients))
        ccf, customer = item_ccf({**item, "exposure_type": item["ccf_type"]}, segment, fallback, ccf_exposure, ccf_level)
        stats["items"] += 1
        stats["customer_ccf_items"] += customer
        # Parts of a group with different parameter paths: the segment's (key "") or a sector's (sectoral satellite).
        code = sector_key(item, coefficients) or ""
        g = groups.setdefault((segment, item["exposure_type"]), {}).setdefault(code, {
            "post": {st: [0.0, 0.0] for st in (*STAGES, "poci")}, "nom": {st: [0.0, 0.0] for st in (*STAGES, "poci")},
            "post_s3": [], "nom_s3": []})
        st = item["stage"]
        allowance = item["allowance"]
        g["post"][st][0] += ccf * item["nominal"]
        g["post"][st][1] += allowance
        g["nom"][st][0] += item["nominal"]
        if st == "stage3":
            g["post_s3"].append((ccf * item["nominal"], allowance))
            g["nom_s3"].append((item["nominal"], 0.0))
    rows = []
    for segment, etype in sorted(groups):
        parts = [groups[(segment, etype)][c] for c in sorted(groups[(segment, etype)])]
        post = {st: [sum(g["post"][st][i] for g in parts) for i in (0, 1)] for st in (*STAGES, "poci")}
        nom = {st: [sum(g["nom"][st][i] for g in parts) for i in (0, 1)] for st in (*STAGES, "poci")}
        paths = [sector_paths.get((segment, c), projected[segment]) for c in sorted(groups[(segment, etype)])]
        start = dict.fromkeys(OFF_BALANCE_FIELDS, 0.0)
        start.update(nom_s1=nom["stage1"][0], nom_s2=nom["stage2"][0], nom_s3_old=nom["stage3"][0], nom_poci=nom["poci"][0],
                     exp_s1=post["stage1"][0], exp_s2=post["stage2"][0], exp_s3_old=post["stage3"][0],
                     exp_poci=post["poci"][0], prov_old_s3=post["stage3"][1], prov_stock_s1=post["stage1"][1],
                     prov_stock_s2=post["stage2"][1], prov_stock_s3=post["stage3"][1], prov_stock_poci=post["poci"][1])
        rows.append({"segment": segment, "exposure_type": etype, "scenario": "actual", "year": 0, **start})
        post_rows = project_parts([(g["post"], P, g["post_s3"]) for g, P in zip(parts, paths)], cfg)
        nom_rows = project_parts([(g["nom"], P, g["nom_s3"]) for g, P in zip(parts, paths)], cfg)
        for pr, nr in zip(post_rows, nom_rows):
            rows.append({"segment": segment, "exposure_type": etype, "scenario": pr["scenario"], "year": pr["year"],
                         **{n: nr[e] for n, e in zip(NOMINAL, POST_CCF)},
                         **{k: pr[k] for k in OFF_BALANCE_FIELDS if k not in NOMINAL}})
    return rows, stats


def write_off_balance(path: Path, rows: list[dict]):
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["segment", "exposure_type", "scenario", "year", *OFF_BALANCE_FIELDS])
        for r in rows:
            w.writerow([r["segment"], r["exposure_type"], r["scenario"], r["year"], *(f"{r[k]:.2f}" for k in OFF_BALANCE_FIELDS)])


OFF_BS_COLUMNS = (
    ("Total nominal amount before CCF (total NomAmount)", NOMINAL),
    ("Performing nominal amount before CCF (Perf NomAmount)", ("nom_s1", "nom_s2")),
    ("of which: stage 1 (NomAmount S1)", ("nom_s1",)),
    ("of which: stage 2 (NomAmount S2)", ("nom_s2",)),
    ("Non-performing nominal amount before CCF (NomAmount S3)", ("nom_s3_old", "nom_s3_new")),
    ("POCI nominal amount before CCF (NomAmount POCI)", ("nom_poci",)),
    ("Total nominal amount after CCF (total PostCCF)", POST_CCF),
    ("Performing nominal amount after CCF (Perf PostCCF)", ("exp_s1", "exp_s2")),
    ("of which: stage 1 (PostCCF S1)", ("exp_s1",)),
    ("of which: stage 2 (PostCCF S2)", ("exp_s2",)),
    ("Non-performing nominal amount after CCF (PostCCF S3)", ("exp_s3_old", "exp_s3_new")),
    ("POCI nominal amount after CCF (PostCCF POCI)", ("exp_poci",)),
    ("Stock of provisions (Prov Stock)", ("prov_stock_s1", "prov_stock_s2", "prov_stock_s3", "prov_stock_poci")),
    ("of which: performing assets (Prov Stock Perf)", ("prov_stock_s1", "prov_stock_s2")),
    ("of which: stage 1 (Prov Stock S1)", ("prov_stock_s1",)),
    ("of which: stage 2 (Prov Stock S2)", ("prov_stock_s2",)),
    ("of which: non-performing assets (Prov Stock S3)", ("prov_stock_s3",)),
    ("of which: POCI (Prov Stock POCI)", ("prov_stock_poci",)),
)
OFF_BS_SLOTS = (("actual", 0, "Actual"), ("baseline", 1, "Baseline"), ("baseline", 2, "Baseline"),
                ("baseline", 3, "Baseline"), ("adverse", 1, "Adverse"), ("adverse", 2, "Adverse"), ("adverse", 3, "Adverse"))


def off_balance_sector(portfolio: str) -> str:
    return "NFC" if portfolio.startswith("NFC") else "HH" if portfolio.startswith("HH") else portfolio


def write_cr_scen_off_bs(path: Path, rows: list[dict], ref_year: int):
    """EBA CSV_CR_SCEN_OFF_BS layout (2027 draft templates): 22 rows per scenario and year (per commitment type a
    Sum row and six counterparty-sector rows, then Total), Total geography only, amounts in EUR million."""
    cells = defaultdict(lambda: dict.fromkeys(OFF_BALANCE_FIELDS, 0.0))     # (scenario, year, type, sector) -> sums
    for r in rows:
        c = cells[(r["scenario"], r["year"], TEMPLATE_TYPE.get(r["exposure_type"], r["exposure_type"]),
                   off_balance_sector(r["segment"].split("|")[1]))]
        for k in OFF_BALANCE_FIELDS:
            c[k] += r[k]
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["RowNum", "Pivot", "Geographical breakdown", "Scenario", "Year", "Portfolio", "Asset class 1",
                    "Asset class 2", "Asset classes", *(h for h, _ in OFF_BS_COLUMNS)])
        for scen, year, label in OFF_BS_SLOTS:
            def emit(num, pivot, ac1, ac2, name, keys):
                v = dict.fromkeys(OFF_BALANCE_FIELDS, 0.0)
                for t, sector in keys:
                    for k, x in cells.get((scen, year, t, sector), {}).items():
                        v[k] += x
                w.writerow([num, pivot, "Total", label, ref_year + year, "Off-balance sheet", ac1, ac2, name,
                            *(f"{sum(v[k] for k in fs) / 1e6:.8f}" for _, fs in OFF_BS_COLUMNS)])

            num = 0
            for t in OFF_BALANCE_TYPES:
                num += 1
                emit(num, "Sum", OFF_BALANCE_LABELS[t], "", OFF_BALANCE_LABELS[t], [(t, c) for c, _ in OFF_BALANCE_SECTORS])
                for code, name in OFF_BALANCE_SECTORS:
                    num += 1
                    emit(num, "Pivot", OFF_BALANCE_LABELS[t], name, name, [(t, code)])
            emit(num + 1, "Sum", "Total", "", "Total", [(t, c) for t in OFF_BALANCE_TYPES for c, _ in OFF_BALANCE_SECTORS])


# ----------------------------------------------------------------------------------------- ECB benchmarks

# Projected parameters by benchmark group (EBA MN 2027 draft para 117 applies the 10% rule to "the PD/TR and LR/LGD
# parameters, respectively"). TR3-1/TR3-2 are starting-point only and never benchmarked.
BENCHMARK_GROUPS = {"pd_tr": ("pd12m_s1", "pd12m_s2", "tr1_2", "tr2_1"), "lgd_lr": ("lgd_s1", "lgd_s2", "lgd_s3", "lrlt_s2")}
# Calibration parts (calibration_levels) behind each group's starting point.
BENCHMARK_PARTS = {"pd_tr": ("stage1", "stage2"), "lgd_lr": ("lgd", "lrlt")}
MODEL_LEVELS = {"segment": 0, "portfolio": 1}


def benchmark_config(cfg) -> dict | None:
    """The scenario's `benchmark_parameters` block with defaults, or None if absent."""
    b = cfg.get("benchmark_parameters")
    if not b:
        return None
    out = {"file": b["file"], "coverage_threshold": float(b.get("coverage_threshold", 0.10)),
           "model_level": b.get("model_level", "portfolio"), "sovereign": bool(b.get("sovereign", True)),
           "country_fallback": list(b.get("country_fallback", cfg.get("country_fallback", [])))}
    if out["model_level"] not in MODEL_LEVELS:
        raise ValueError("benchmark_parameters.model_level must be segment or portfolio")
    if not 0.0 <= out["coverage_threshold"] <= 1.0:
        raise ValueError("benchmark_parameters.coverage_threshold outside [0, 1]")
    return out


def load_benchmarks(path: Path, cfg) -> dict:
    """{(instrument, portfolio, country): {group: {(scenario, t): {parameter: value}}}} from Sora's benchmark
    format (long CSV, `#` comment lines). Years are scenario years, mapped to projection years by `year_map`.
    A group must be complete (all its parameters for baseline and adverse years 1..3) or absent."""
    year_to_t = {y: t for t, y in cfg["year_map"].items()}
    group_of = {p: g for g, ps in BENCHMARK_GROUPS.items() for p in ps}
    raw: dict = {}
    with open(path) as f:
        for r in csv.DictReader(line for line in f if not line.startswith("#")):
            key = (r["instrument"], r["portfolio"], r["country"])
            if r["instrument"] not in ("LOANS", "DEBT_SEC"):
                raise ValueError(f"benchmarks: unknown instrument {r['instrument']}")
            if r["parameter"] not in group_of:
                raise ValueError(f"benchmarks: unknown parameter {r['parameter']}")
            if r["scenario"] not in ("baseline", "adverse"):
                raise ValueError(f"benchmarks: unknown scenario {r['scenario']}")
            y = int(r["year"])
            if y not in year_to_t:                                  # outside the horizon of this scenario
                continue
            v = float(r["value"])
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"benchmarks: {r['parameter']} outside [0, 1] for {key}")
            cell = raw.setdefault(key, {}).setdefault(group_of[r["parameter"]], {}).setdefault((r["scenario"], year_to_t[y]), {})
            if r["parameter"] in cell:
                raise ValueError(f"benchmarks: duplicate {r['parameter']} for {key} {r['scenario']} {y}")
            cell[r["parameter"]] = v
    for key, groups in raw.items():
        for g, slots in groups.items():
            if len(slots) != 6 or any(len(v) != len(BENCHMARK_GROUPS[g]) for v in slots.values()):
                raise ValueError(f"benchmarks: incomplete {g} parameters for {'|'.join(key)}")
            for (scen, t), v in slots.items():
                if g == "pd_tr" and (v["pd12m_s1"] + v["tr1_2"] > 1 + 1e-12 or v["pd12m_s2"] + v["tr2_1"] > 1 + 1e-12):
                    raise ValueError(f"benchmarks: stage outflows above 1 for {'|'.join(key)} {scen} {t}")
    return raw


def benchmark_decisions(segments, sources, seg_stock, sat, bench, bcfg, sector_cov=None) -> tuple[dict, dict]:
    """The benchmark application rule (EBA MN 2027 draft paras 115-117 and 146; 2025 MN paras 124-126 and 155).

    Model coverage: a segment's group is covered by a satellite model if its portfolio has satellite coefficients
    (or every exposure of the segment has a sectoral satellite for the group, `sector_cov`) and the group's starting point was calibrated within the pivot asset class (calibration level no coarser than
    `model_level`: segment, or portfolio = instrument|portfolio|ALL). Per pivot asset class (instrument|portfolio)
    and group, coverage = covered t0 exposure / t0 exposure (gross carrying amount).
      * general governments (`sovereign`): the benchmark of the segment's own country is mandatory (para 146);
      * coverage < threshold: benchmark for every segment of the pivot asset class (para 117);
      * otherwise: benchmark for the segments without a model (para 115); the pivot asset class then mixes model
        and benchmark parameters, exposure-weighted (para 117's weighted average).
    The benchmark key is the segment's country, then `country_fallback`; if none has the group, the model
    parameters are kept ("unavailable"). Returns ({segment: {group: decision}}, {pivot: coverage})."""
    max_level = MODEL_LEVELS[bcfg["model_level"]]

    def exposure(s):
        return sum(seg_stock[s][st][0] for st in (*STAGES, "poci"))

    def covered(s, g):
        return (s.split("|")[1] in sat or (sector_cov or {}).get(s, {}).get(g, False)) and all(
            sources[s][part] in parents(s)[:max_level + 1] for part in BENCHMARK_PARTS[g])

    pivots: dict = {}
    for s in segments:
        pv = pivots.setdefault("|".join(s.split("|")[:2]), {"exposure": 0.0, "model": dict.fromkeys(BENCHMARK_GROUPS, 0.0),
                                                            "benchmark": dict.fromkeys(BENCHMARK_GROUPS, 0.0)})
        e = exposure(s)
        pv["exposure"] += e
        for g in BENCHMARK_GROUPS:
            pv["model"][g] += e if covered(s, g) else 0.0
    decisions = {}
    for s in segments:
        instrument, portfolio, bucket = s.split("|")
        pv = pivots[f"{instrument}|{portfolio}"]
        decisions[s] = {}
        for g in BENCHMARK_GROUPS:
            coverage = pv["model"][g] / pv["exposure"] if pv["exposure"] > 0 else 0.0
            chain = ([bucket] if bucket != "OTHER" else []) + bcfg["country_fallback"]
            has = lambda c: g in bench.get((instrument, portfolio, c), {})  # noqa: E731
            if bcfg["sovereign"] and portfolio == "GG" and bucket != "OTHER" and has(bucket):
                rule, chain = "sovereign", [bucket]
            elif coverage < bcfg["coverage_threshold"]:
                rule = "coverage"
            elif not covered(s, g):
                rule = "no_model"
            else:
                rule = "none"
            key = next((c for c in chain if has(c)), None) if rule != "none" else None
            decisions[s][g] = {"model": covered(s, g), "rule": rule,
                               "benchmark": key if key else ("unavailable" if rule != "none" else "")}
            if key:
                pv["benchmark"][g] += exposure(s)
    return decisions, pivots


def apply_benchmark(P: dict, decision: dict, bench: dict, segment: str, scenario: str) -> None:
    """Replace the projected parameters (years 1..3, year 4 = flat continuation) of the benchmarked groups, without
    any adjustment (MN para 115)."""
    instrument, portfolio, _ = segment.split("|")
    for g, d in decision.items():
        if d["benchmark"] in ("", "unavailable"):
            continue
        values = bench[(instrument, portfolio, d["benchmark"])][g]
        for t in (1, 2, 3):
            P[t].update(values[(scenario, t)])
    P[4] = dict(P[3])


def write_benchmarks(path: Path, segments, seg_stock, decisions):
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["segment", "exposure", "pd_tr_model", "lgd_lr_model", "pd_tr_rule", "lgd_lr_rule",
                    "pd_tr_benchmark", "lgd_lr_benchmark"])
        for s in segments:
            d = decisions[s]
            w.writerow([s, f"{sum(seg_stock[s][st][0] for st in (*STAGES, 'poci')):.2f}",
                        int(d["pd_tr"]["model"]), int(d["lgd_lr"]["model"]), d["pd_tr"]["rule"], d["lgd_lr"]["rule"],
                        d["pd_tr"]["benchmark"], d["lgd_lr"]["benchmark"]])


def benchmark_summary(bcfg, decisions, pivots) -> dict:
    count = lambda g, pred: sum(1 for d in decisions.values() if pred(d[g]))  # noqa: E731
    applied = lambda d: d["benchmark"] not in ("", "unavailable")          # noqa: E731
    return {
        "file": Path(bcfg["file"]).name, "coverage_threshold": bcfg["coverage_threshold"],
        "model_level": bcfg["model_level"], "sovereign": bcfg["sovereign"],
        "segments_pd_tr": count("pd_tr", applied), "segments_lgd_lr": count("lgd_lr", applied),
        "segments_unavailable": sum(1 for d in decisions.values() if any(x["benchmark"] == "unavailable" for x in d.values())),
        "pivots": {p: {"exposure": round(v["exposure"], 2),
                       **{f"{g}_model_coverage": round(v["model"][g] / v["exposure"], 9) if v["exposure"] > 0 else 0.0
                          for g in BENCHMARK_GROUPS},
                       **{f"{g}_benchmark_share": round(v["benchmark"][g] / v["exposure"], 9) if v["exposure"] > 0 else 0.0
                          for g in BENCHMARK_GROUPS}}
                   for p, v in sorted(pivots.items())},
    }


# ----------------------------------------------------------------------------------------- sectoral (GVA) satellites

# CR_SECTOR sector (NACE Rev. 2.1 section; manufacturing split into energy-intensive C_EI and other C_OT) -> sector of
# the ESRB "Real GVA by sector" scenario (NACE Rev. 2 sections and aggregates; C_high / C_low = high / low energy
# intensity manufacturing, MN 2027 para 95). Mapped by division: Rev. 2.1 J (58-60) and K (61-63) are Rev. 2 J,
# Rev. 2.1 L (64-66) is Rev. 2 K, M (68) is L, N (69-75) and O (77-82) are MN, P-R (84-88) OPQ, S-T (90-96) RSTU.
GVA_SECTOR = {"A": "A", "B": "B", "C_EI": "C_high", "C_OT": "C_low", "D": "D", "E": "E", "F": "F", "G": "G",
              "H": "H", "I": "I", "J": "J", "K": "J", "L": "K", "M": "L", "N": "MN", "O": "MN", "P": "OPQ",
              "Q": "OPQ", "R": "OPQ", "S": "RSTU", "T": "RSTU"}
# Coefficient of each parameter group (benchmark groups): an empty coefficient = no sectoral model for that group.
SECTOR_COEFFICIENTS = {"pd_tr": "beta_gva", "lgd_lr": "lgd_gva_sensitivity"}


def sector_satellite_config(cfg) -> dict | None:
    """The scenario's `sector_satellites` block with defaults, or None if absent."""
    s = cfg.get("sector_satellites")
    if not s:
        return None
    if "file" not in s:
        raise ValueError("sector_satellites.file is required")
    return {"file": s["file"], "gva_fallback": list(s.get("gva_fallback", ["EU"]))}


def load_sector_satellites(path: Path) -> dict:
    """{CR_SECTOR sector: {beta_gva, lgd_gva_sensitivity}} (None = no model for that group) from a CSV with columns
    sector, beta_gva, lgd_gva_sensitivity[, description]; lines starting with `#` are comments."""
    out = {}
    with open(path) as f:
        for r in csv.DictReader(line for line in f if not line.startswith("#")):
            code = (r["sector"] or "").strip()
            if code not in GVA_SECTOR:
                raise ValueError(f"sector satellites: unknown sector {code!r}")
            if code in out:
                raise ValueError(f"sector satellites: duplicate sector {code}")
            c = {k: float(r[k]) if (r.get(k) or "").strip() else None for k in SECTOR_COEFFICIENTS.values()}
            if all(v is None for v in c.values()):
                raise ValueError(f"sector satellites: no coefficients for sector {code}")
            if any(v is not None and not math.isfinite(v) for v in c.values()):
                raise ValueError(f"sector satellites: invalid coefficient for sector {code}")
            out[code] = c
    return out


def sector_model(segment, code, coefficients, macro, cfg, scfg) -> dict:
    """The sectoral satellite of CR_SECTOR sector `code` for the exposures of `segment`. GVA path: the scenario's real
    GVA of the sector for the segment's macro key; for a key without sectoral GVA (non-EU countries: the scenario has
    GVA for the EU 27, EA and EU only, template guidance para 45) the GDP growth of the macro key plus the sector's
    GVA deviation from GDP in the first `gva_fallback` key that has it (EU): gdp(key) + gva(EU) - gdp(EU)."""
    bucket = segment.split("|")[2]
    key = macro_key(macro, bucket, cfg)
    var = f"real_gva:{GVA_SECTOR[code]}"
    y1 = cfg["year_map"][1]
    if (var, key, "baseline", y1) in macro:
        gva_key, relative = key, False
    else:
        gva_key = next((k for k in scfg["gva_fallback"] if (var, k, "baseline", y1) in macro), None)
        if gva_key is None:
            raise KeyError(f"no real GVA path for sector {code} ({GVA_SECTOR[code]}) in {key} or {scfg['gva_fallback']}")
        relative = True
    return {"code": code, **coefficients[code], "var": var, "macro_key": key, "gva_key": gva_key, "relative": relative}


def sector_growth(sector, macro, scenario, year) -> float:
    """Real GVA growth (%) of the sector's path in a scenario year."""
    def get(var, key):
        v = macro.get((var, key, scenario, year))
        if v is None:
            raise KeyError(f"macro: no {var} for {key} {scenario} {year}")
        return v
    if not sector["relative"]:
        return get(sector["var"], sector["gva_key"])
    return get("real_gdp", sector["macro_key"]) + (get(sector["var"], sector["gva_key"]) - get("real_gdp", sector["gva_key"]))


def sector_key(r, coefficients) -> str | None:
    """The CR_SECTOR sector of an NFC exposure (or off-balance item) that has a sectoral satellite, else None
    (projected with the segment's portfolio path)."""
    if not coefficients or not r["portfolio"].startswith("NFC"):
        return None
    code = nace_sector(r["nace_code"])
    return code if code in coefficients else None


def sector_coverage(exposures, coefficients) -> dict:
    """{segment: {group: bool}}: every exposure of the (NFC) segment has a sectoral satellite for the group. Such a
    segment counts as modelled for the ECB benchmark rule even if its portfolio has no satellite coefficients."""
    out: dict = {}
    for r in exposures:
        code = sector_key(r, coefficients)
        cov = out.setdefault(r["segment"], dict.fromkeys(SECTOR_COEFFICIENTS, True))
        for g, coef in SECTOR_COEFFICIENTS.items():
            cov[g] = cov[g] and code is not None and coefficients[code][coef] is not None
    return out


def project_parts(parts, cfg) -> list[dict]:
    """Boxes 3-9 for a segment made of parts (stock, parameter paths, S3 exposures) with different parameter paths
    (sectoral satellites): the parts' rows added up. A single part is projected as a whole."""
    if len(parts) == 1:
        return project_segment(*parts[0][:2], cfg, parts[0][2])
    total = None
    for stock_, paths, s3 in parts:
        rows = project_segment(stock_, paths, cfg, s3)
        if total is None:
            total = [dict(r) for r in rows]
            continue
        for t, r in zip(total, rows):
            for k, v in r.items():
                if k not in ("scenario", "year"):
                    t[k] += v
    return total


def sector_summary(scfg, coefficients, exposures, sector_use, sector_rows) -> dict:
    """summary.json "sector_satellites": settings, sectors with a model per group, and the t0 exposure of the NFC
    portfolio (on-balance) projected with sectoral models per group (after the ECB benchmark rule)."""
    total, used = 0.0, dict.fromkeys(SECTOR_COEFFICIENTS, 0.0)
    for r in exposures:
        if not r["portfolio"].startswith("NFC"):
            continue
        total += r["gca"]
        use = sector_use.get((r["segment"], sector_key(r, coefficients)), {})
        for g in used:
            used[g] += r["gca"] if use.get(g) else 0.0
    return {
        "file": Path(scfg["file"]).name, "gva_fallback": scfg["gva_fallback"],
        **{f"sectors_{g}": sum(1 for c in coefficients.values() if c[k] is not None) for g, k in SECTOR_COEFFICIENTS.items()},
        "segments_gva_relative": len({seg for seg, _, model, _, _ in sector_rows if model["relative"]}),
        "nfc_exposure": round(total, 2),
        **{f"{g}_exposure": round(v, 2) for g, v in used.items()},
        **{f"{g}_share": round(v / total, 9) if total > 0 else 0.0 for g, v in used.items()},
    }


def write_sector_parameters(path: Path, rows: list):
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["segment", "sector", "gva_sector", "gva_key", "gva_relative", "scenario", "year", *PARAMS,
                    "pd_tr", "lgd_lr"])
        for segment, code, model, P, use in rows:
            for scen in ("baseline", "adverse"):
                for t in (1, 2, 3):
                    w.writerow([segment, code, GVA_SECTOR[code], model["gva_key"], int(model["relative"]), scen, t,
                                *(f"{P[scen][t][k]:.9f}" for k in PARAMS), use["pd_tr"], use["lgd_lr"]])


# ----------------------------------------------------------------------------------------- CR_SECTOR

# NACE Rev. 2.1 sections by division (01..99). Division numbers mean the same sections in NACE Rev. 2, except for
# the letters, so a code of either revision maps by its division. Divisions 97-99 (households as employers,
# extraterritorial bodies) and unused numbers are not NFC activities: sector unknown.
NACE_DIVISIONS = (("A", 1, 3), ("B", 5, 9), ("C", 10, 33), ("D", 35, 35), ("E", 36, 39), ("F", 41, 43),
                  ("G", 45, 47), ("H", 49, 53), ("I", 55, 56), ("J", 58, 60), ("K", 61, 63), ("L", 64, 66),
                  ("M", 68, 68), ("N", 69, 75), ("O", 77, 82), ("P", 84, 84), ("Q", 85, 85), ("R", 86, 88),
                  ("S", 90, 93), ("T", 94, 96))
ENERGY_INTENSIVE = range(17, 31)          # 2027 draft template guidance, Table 4: C10-C12 and C17-C30
ENERGY_INTENSIVE_LOW = range(10, 13)
CR_SECTOR_LABELS = {
    "A": "A - Agriculture, forestry and fishing", "B": "B - Mining and quarrying", "C": "C - Manufacturing",
    "C_EI": "C Manufacturing - energy-intensive activities", "C_OT": "C Manufacturing - other",
    "D": "D - Electricity, gas, steam and air conditioning supply",
    "E": "E - Water supply; sewerage, waste management and remediation activities", "F": "F - Construction",
    "G": "G - Wholesale and retail trade", "H": "H - Transportation and storage",
    "I": "I - Accommodation and food service activities",
    "J": "J - Publishing, broadcasting, and content production and distribution activities",
    "K": "K - Telecommunication, computer programming, consulting, computing infrastructure and other information "
         "service activities",
    "L": "L - Financial and insurance activities", "M": "M - Real estate activities",
    "N": "N - Professional, scientific and technical activities", "O": "O - Administrative and support service activities",
    "P": "P - Public administration and defence; compulsory social security", "Q": "Q - Education",
    "R": "R - Human health and social work activities", "S": "S - Arts, sports and recreation",
    "T": "T - Other service activities", "TOTAL": "TOTAL exposures to NFC",
}


def nace_sector(code) -> str:
    """CR_SECTOR sector of a NACE code (Rev. 2 or 2.1, e.g. C24.10, C24, 24.10 or C): the Rev. 2.1 section letter,
    C_EI / C_OT for manufacturing (energy-intensive or other), or UNKNOWN. A bare letter is read as a Rev. 2.1
    section; a bare C counts as C_OT (the division is needed for the energy-intensive split)."""
    code = (code or "").strip().upper()
    letter = code[:1] if code[:1].isalpha() else ""
    digits = code[len(letter):len(letter) + 2]
    if len(digits) == 2 and digits.isdigit():
        div = int(digits)
        for sec, lo, hi in NACE_DIVISIONS:
            if lo <= div <= hi:
                if sec == "C":
                    return "C_EI" if div in ENERGY_INTENSIVE or div in ENERGY_INTENSIVE_LOW else "C_OT"
                return sec
        return "UNKNOWN"
    if letter and len(code) == 1 and letter in CR_SECTOR_LABELS:
        return "C_OT" if letter == "C" else letter
    return "UNKNOWN"


# (row number, pivot, sector key, member sectors); the total includes exposures of unknown sector.
CR_SECTOR_ROWS = []
for _k in ("A", "B", "C", "C_EI", "C_OT", *"DEFGHIJKLMNOPQRST", "TOTAL"):
    CR_SECTOR_ROWS.append((len(CR_SECTOR_ROWS) + 1, "Sum" if _k == "TOTAL" else "o/w" if _k.startswith("C_") else "Pivot",
                           _k, None if _k == "TOTAL" else ("C_EI", "C_OT") if _k == "C" else (_k,)))

SLOTS = (("actual", 0), ("baseline", 1), ("baseline", 2), ("baseline", 3), ("adverse", 1), ("adverse", 2), ("adverse", 3))
AMOUNTS = ("exp_s1", "exp_s2", "exp_s3_old", "exp_s3_new", "exp_poci", "prov_s1", "prov_s2", "prov_s3", "prov_poci",
           "flow_s2_s1", "flow_s1_s2", "flow_s1_s3", "flow_s2_s3", "prov_s1_s2", "prov_s2_s2", "prov_s1_s3", "prov_s2_s3",
           "cum_s1_s3", "cum_s2_s3", "prov_s1_s1", "prov_s2_s1", "prov_old_s3")
WEIGHT_STAGE = {"pd12m_s1": 0, "tr1_2": 0, "lgd_s1": 0, "pd12m_s2": 1, "tr2_1": 1, "lgd_s2": 1, "lrlt_s2": 1,
                "tr3_1": 2, "tr3_2": 2, "lgd_s3": 2}


def new_agg() -> dict:
    # sw: t0 exposure, sused: of which projected with a sectoral model per group (projected slots only)
    return {**{k: 0.0 for k in AMOUNTS}, "w": [0.0, 0.0, 0.0], "psum": {k: 0.0 for k in PARAMS}, "sw": 0.0,
            "sused": [0.0, 0.0]}


def add_params(agg, p, w):
    """Exposure-weighted parameters: S1 exposure at the start of the year for PD12M S1, TR1-2, LGD S1; S2 for
    PD12M S2, TR2-1, LGD S2, LRLT S2; old S3 for TR3-1, TR3-2, LGD S3."""
    for i in range(3):
        agg["w"][i] += w[i]
    for k in PARAMS:
        if w[WEIGHT_STAGE[k]] != 0:
            agg["psum"][k] += p[k] * w[WEIGHT_STAGE[k]]


def sector_cells(exposures, params0, projected, cfg, sector_paths=None, sector_use=None) -> dict:
    """{(segment, sector): [agg per slot]} for the NFC segments. The sector is carried through the projection:
    each (segment, sector) stock is projected with the parameters its exposures have, exactly as the engine projects
    each exposure: the sectoral satellite path `sector_paths[(segment, sector)]` if there is one, else the segment's.
    Because Boxes 3-8 are linear in the stage stocks and Box 9 applies per exposure, the sectors of a segment add up
    to the segment. `sector_use[(segment, sector)]` = {group: projected with the sectoral model} (CR_SECTOR columns
    1-2, share of t0 exposure in the projected slots)."""
    sector_paths, sector_use = sector_paths or {}, sector_use or {}
    stocks, s3 = {}, defaultdict(list)
    for r in exposures:
        if not r["portfolio"].startswith("NFC") or r["stage"] not in (*STAGES, "poci"):
            continue
        key = (r["segment"], nace_sector(r["nace_code"]))
        st = stocks.setdefault(key, {k: [0.0, 0.0] for k in (*STAGES, "poci")})
        st[r["stage"]][0] += r["gca"]
        st[r["stage"]][1] += r["allowance"]
        if r["stage"] == "stage3":
            s3[key].append((r["gca"], r["allowance"]))
    cells = {}
    for key, st in sorted(stocks.items()):
        seg = key[0]
        slots = [new_agg() for _ in SLOTS]
        a = slots[0]
        (a["exp_s1"], a["prov_s1"]), (a["exp_s2"], a["prov_s2"]) = st["stage1"], st["stage2"]
        (a["exp_s3_old"], a["prov_s3"]), (a["exp_poci"], a["prov_poci"]) = st["stage3"], st["poci"]
        add_params(a, params0[seg], (st["stage1"][0], st["stage2"][0], st["stage3"][0]))
        paths = sector_paths.get(key, projected[seg])
        use = sector_use.get(key, {})
        t0 = sum(st[k][0] for k in (*STAGES, "poci"))
        for b in slots[1:]:
            b["sw"] = t0
            b["sused"] = [t0 if use.get(g) else 0.0 for g in SECTOR_COEFFICIENTS]
        prev = {}
        for row in project_segment(st, paths, cfg, s3[key]):
            sc, t = row["scenario"], row["year"]
            b = slots[SLOTS.index((sc, t))]
            e1, e2 = prev.get(sc, (st["stage1"][0], st["stage2"][0]))             # exposure at the start of the year
            add_params(b, paths[sc][t], (e1, e2, st["stage3"][0]))
            prev[sc] = (row["exp_s1"], row["exp_s2"])
            for k in ("exp_s1", "exp_s2", "exp_s3_old", "exp_s3_new", "exp_poci", "flow_s2_s1", "flow_s1_s2",
                      "flow_s1_s3", "flow_s2_s3", "prov_s1_s2", "prov_s2_s2", "prov_s1_s1", "prov_s2_s1", "prov_old_s3"):
                b[k] = row[k]
            b["prov_s1"], b["prov_s2"] = row["prov_stock_s1"], row["prov_stock_s2"]
            b["prov_s3"], b["prov_poci"] = row["prov_stock_s3"], row["prov_stock_poci"]
            b["cum_s1_s3"], b["cum_s2_s3"] = row["prov_cum_s1_s3"], row["prov_cum_s2_s3"]
            before = slots[SLOTS.index((sc, t - 1))] if t > 1 else None
            b["prov_s1_s3"] = row["prov_cum_s1_s3"] - (before["cum_s1_s3"] if before else 0.0)
            b["prov_s2_s3"] = row["prov_cum_s2_s3"] - (before["cum_s2_s3"] if before else 0.0)
        cells[key] = slots
    return cells


def _param(a, k):
    w = a["w"][WEIGHT_STAGE[k]]
    return a["psum"][k] / w if w > 0 else None


def _ratio(num, den):
    return num / den if den > 0 else None


def _flow(k):
    return lambda a, actual: None if actual else a[k]


def _sectoral(i):
    """Columns 1-2: share of t0 exposure projected with sectoral (GVA) satellites (0 for Actual, as the CR_SCEN
    benchmark columns)."""
    return lambda a, actual: 0.0 if actual or a["sw"] <= 0 else a["sused"][i] / a["sw"]


# (header, percent?, value(agg, actual)): the 2027 draft CSV_CR_SECTOR columns. Columns 1-2: exposures projected with
# sectoral satellites (scenario key sector_satellites); PD / LGD PiT are not produced (blank), as in cr_scen.csv.
CR_SECTOR_COLUMNS = (
    ("PD/TR - Percentage of exposures with projections based on sectoral models, e.g. via sensitivities by sector (%)",
     True, _sectoral(0)),
    ("LGD/LR - Percentage of exposures with projections based on sectoral models, e.g. via sensitivities by sector (%)",
     True, _sectoral(1)),
    ("PD PiT (%)", True, lambda a, actual: None),
    ("PD 12M S1 (TR1-3)", True, lambda a, actual: _param(a, "pd12m_s1")),
    ("TR1-2", True, lambda a, actual: _param(a, "tr1_2")),
    ("PD 12M S2 (TR2-3)", True, lambda a, actual: _param(a, "pd12m_s2")),
    ("TR2-1", True, lambda a, actual: _param(a, "tr2_1")),
    ("TR3-1", True, lambda a, actual: _param(a, "tr3_1") if actual else None),
    ("TR3-2", True, lambda a, actual: _param(a, "tr3_2") if actual else None),
    ("LGD PiT new (%)", True, lambda a, actual: None),
    ("LGD S1", True, lambda a, actual: _param(a, "lgd_s1")),
    ("LGD S2", True, lambda a, actual: _param(a, "lgd_s2")),
    ("LRLT S2", True, lambda a, actual: _param(a, "lrlt_s2")),
    ("LGD S3", True, lambda a, actual: _param(a, "lgd_s3")),
    ("Stage 1 flow (S2-S1 flow)", False, _flow("flow_s2_s1")),
    ("Stage 2 flow (S1-S2 flow)", False, _flow("flow_s1_s2")),
    ("Stage 3 flow (SX-S3 flow)", False, lambda a, actual: None if actual else a["flow_s1_s3"] + a["flow_s2_s3"]),
    ("Stage 3 flow from Stage 1 (S1-S3 Flow)", False, _flow("flow_s1_s3")),
    ("Stage 3 flow from Stage 2 (S2-S3 Flow)", False, _flow("flow_s2_s3")),
    ("Provisions stage 1 to stage 2 (Prov S1-S2)", False, _flow("prov_s1_s2")),
    ("Provisions stage 2 to stage 2 (Prov S2-S2)", False, _flow("prov_s2_s2")),
    ("Provisions new stage 3 (Prov SX-S3)", False, lambda a, actual: None if actual else a["prov_s1_s3"] + a["prov_s2_s3"]),
    ("Provisions stage 1 to stage 3 (Prov S1-S3)", False, _flow("prov_s1_s3")),
    ("Provisions stage 2 to stage 3 (Prov S2-S3)", False, _flow("prov_s2_s3")),
    ("Cumulative provisions new stage 3 (Prov Cumul SX-S3)", False,
     lambda a, actual: None if actual else a["cum_s1_s3"] + a["cum_s2_s3"]),
    ("Cumulative provisions stage 1 to stage 3 (Prov Cumul S1-S3)", False, _flow("cum_s1_s3")),
    ("Cumulative provisions stage 2 to stage 3 (Prov Cumul S2-S3)", False, _flow("cum_s2_s3")),
    ("Provisions stage 1 to stage 1 (Prov S1-S1)", False, _flow("prov_s1_s1")),
    ("Provisions stage 2 to stage 1 (Prov S2-S1)", False, _flow("prov_s2_s1")),
    ("Provisions old stage 3 (Prov old S3-S3)", False, _flow("prov_old_s3")),
    ("Total exposure (total Exp)", False,
     lambda a, actual: a["exp_s1"] + a["exp_s2"] + a["exp_s3_old"] + a["exp_s3_new"] + a["exp_poci"]),
    ("Performing exposure (Exp)", False, lambda a, actual: a["exp_s1"] + a["exp_s2"]),
    ("of which: stage 1 (Exp S1)", False, lambda a, actual: a["exp_s1"]),
    ("of which: stage 2 (Exp S2)", False, lambda a, actual: a["exp_s2"]),
    ("Non-performing exposure (Exp S3)", False, lambda a, actual: a["exp_s3_old"] + a["exp_s3_new"]),
    ("of which: existing Non-performing exposure (Old Exp S3)", False, lambda a, actual: a["exp_s3_old"]),
    ("of which: cumulative new non-performing exposure (Cumul New Exp S3)", False, lambda a, actual: a["exp_s3_new"]),
    ("POCI exposures (Exp POCI)", False, lambda a, actual: a["exp_poci"]),
    ("Stock of provisions (Prov Stock)", False,
     lambda a, actual: a["prov_s1"] + a["prov_s2"] + a["prov_s3"] + a["prov_poci"]),
    ("of which: performing assets (Prov Stock Perf)", False, lambda a, actual: a["prov_s1"] + a["prov_s2"]),
    ("of which: stage 1 (Prov Stock S1)", False, lambda a, actual: a["prov_s1"]),
    ("of which: stage 2 (Prov Stock S2)", False, lambda a, actual: a["prov_s2"]),
    ("of which: non-performing assets (Prov Stock S3)", False, lambda a, actual: a["prov_s3"]),
    ("of which: POCI (Prov Stock POCI)", False, lambda a, actual: a["prov_poci"]),
    ("Coverage ratio: performing exposure", True,
     lambda a, actual: _ratio(a["prov_s1"] + a["prov_s2"], a["exp_s1"] + a["exp_s2"])),
    ("Coverage ratio: non-performing exposure", True,
     lambda a, actual: _ratio(a["prov_s3"], a["exp_s3_old"] + a["exp_s3_new"])),
)


def write_cr_sector(path: Path, cells: dict, top: list[str], ref_year: int):
    """cr_sector.csv in the 2027 draft CSV_CR_SECTOR layout: 23 sector rows per geography (Total, top countries,
    Other), scenario and year. Amounts in EUR million (8 decimals), parameters and ratios in percent (7 decimals)."""
    geos = ["Total", *top, "Other"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["RowNum", "Pivot", "Geographical breakdown", "Scenario", "Year", "COREP asset class", "NACE code",
                    "Exposures by sector of economic activity (as per scope defined in section 2.3.3 EBA Methodology Note)",
                    *(c[0] for c in CR_SECTOR_COLUMNS)])
        for slot, (sc, t) in enumerate(SLOTS):
            for geo in geos:
                for num, pivot, key, members in CR_SECTOR_ROWS:
                    a = new_agg()
                    for (seg, sector), slots in cells.items():
                        bucket = seg.split("|")[2]
                        if geo != "Total" and bucket != (geo if geo != "Other" else "OTHER"):
                            continue
                        if members is not None and sector not in members:
                            continue
                        b = slots[slot]
                        for k in AMOUNTS:
                            a[k] += b[k]
                        for i in range(3):
                            a["w"][i] += b["w"][i]
                        a["sw"] += b["sw"]
                        for i in range(2):
                            a["sused"][i] += b["sused"][i]
                        for k in PARAMS:
                            a["psum"][k] += b["psum"][k]
                    values = []
                    for _, pct, get in CR_SECTOR_COLUMNS:
                        v = get(a, slot == 0)
                        values.append("" if v is None else f"{v * 100.0:.7f}" if pct else f"{v / 1e6:.8f}")
                    w.writerow([num, pivot, geo, sc.capitalize(), ref_year + t, "Exposures in scope of CSV_CR_SECTOR",
                                CR_SECTOR_LABELS[key], CR_SECTOR_LABELS[key], *values])


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

    # Sectoral (GVA) satellites (optional scenario key sector_satellites): NFC exposures by NACE sector.
    scfg = sector_satellite_config(cfg)
    coefficients = load_sector_satellites(repo / scfg["file"]) if scfg else {}
    sector_cov = sector_coverage(exposures, coefficients) if scfg else {}

    # ECB benchmarks (optional scenario key benchmark_parameters): which segments take benchmark parameters.
    bcfg = benchmark_config(cfg)
    decisions = pivots = None
    if bcfg:
        bench = load_benchmarks(repo / bcfg["file"], cfg)
        decisions, pivots = benchmark_decisions(segments, sources, seg_stock, sat, bench, bcfg, sector_cov)

    # parameters.csv: starting point and projections (the sim_risk_parameter layout)
    projected, sector_paths, sector_use, sector_rows = {}, {}, {}, []
    with open(out / "parameters.csv", "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["level", "key", "scenario", "year", *PARAMS, "source", "calibration_levels"])
        for s in segments:
            lv = ";".join(f"{k}={v}" for k, v in sorted(sources[s].items()))
            w.writerow(["segment", s, "actual", 0, *(fmtp(params0[s][k]) for k in PARAMS), "derived", lv])
            projected[s] = {}
            applied = [g for g, d in (decisions or {}).get(s, {}).items() if d["benchmark"] not in ("", "unavailable")]
            source = "derived" if not applied else "benchmark" if len(applied) == len(BENCHMARK_GROUPS) else "mixed"
            portfolio = s.split("|")[1]
            # A portfolio without satellite coefficients: every group benchmarked or covered by sectoral satellites.
            if portfolio not in sat and not all(g in applied or sector_cov.get(s, {}).get(g, False) for g in BENCHMARK_GROUPS):
                raise KeyError(f"no satellite coefficients for portfolio {portfolio}")
            seg_sat = sat if portfolio in sat else {portfolio: dict.fromkeys(
                ("beta_gdp", "beta_unemployment", "beta_property", "lgd_property_sensitivity"), 0.0)}
            for scen in ("baseline", "adverse"):
                P = project_parameters(s, params0[s], seg_sat, macro, scen, cfg)
                if applied:
                    apply_benchmark(P, decisions[s], bench, s, scen)
                projected[s][scen] = P
                for t in (1, 2, 3):
                    w.writerow(["segment", s, scen, t, *(fmtp(P[t][k]) for k in PARAMS), source, ""])
            # Sectoral paths of the NFC segment: one per sector with coefficients; the benchmark still wins.
            if coefficients and portfolio.startswith("NFC"):
                for code in (c for c in GVA_SECTOR if c in coefficients):
                    model = sector_model(s, code, coefficients, macro, cfg, scfg)
                    P = {}
                    for scen in ("baseline", "adverse"):
                        P[scen] = project_parameters(s, params0[s], seg_sat, macro, scen, cfg, sector=model)
                        if applied:
                            apply_benchmark(P[scen], decisions[s], bench, s, scen)
                    sector_paths[(s, code)] = P
                    use = {g: "benchmark" if g in applied else "sectoral" if coefficients[code][c] is not None
                           else "portfolio" if portfolio in sat else "none" for g, c in SECTOR_COEFFICIENTS.items()}
                    sector_use[(s, code)] = {g: u == "sectoral" for g, u in use.items()}
                    sector_rows.append((s, code, model, P, use))
    if bcfg:
        write_benchmarks(out / "benchmarks.csv", segments, seg_stock, decisions)
    if scfg:
        write_sector_parameters(out / "sector_parameters.csv", sector_rows)

    # Parts of each segment with their own parameter paths (sectoral satellites): stocks and S3 exposures per sector.
    parts = defaultdict(lambda: {"stock": {st: [0.0, 0.0] for st in (*STAGES, "poci")}, "s3": []})
    for r in exposures:
        part = parts[(r["segment"], sector_key(r, coefficients) or "")]
        part["stock"][r["stage"]][0] += r["gca"]
        part["stock"][r["stage"]][1] += r["allowance"]
        if r["stage"] == "stage3":
            part["s3"].append((r["gca"], r["allowance"]))

    # projection.csv
    totals = defaultdict(lambda: defaultdict(float))
    fields = None
    with open(out / "projection.csv", "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        for s in segments:
            codes = sorted(c for seg, c in parts if seg == s)
            if codes == [""]:                         # one parameter path: the segment projected as a whole
                seg_rows = project_segment(seg_stock[s], projected[s], cfg, s3_by_seg[s])
            else:
                seg_rows = project_parts([(parts[(s, c)]["stock"], sector_paths.get((s, c), projected[s]),
                                           parts[(s, c)]["s3"]) for c in codes], cfg)
            for row in seg_rows:
                if fields is None:
                    fields = list(row)
                    w.writerow(["segment", *fields])
                w.writerow([s, row["scenario"], row["year"], *(fmt(row[k]) for k in fields[2:])])
                for k in fields[2:]:
                    totals[(row["scenario"], row["year"])][k] += row[k]

    write_collateral(out / "collateral.csv", collateral_ltv(con, exposures, macro, cfg, manifest))
    top = top_countries(exposures, cfg["segmentation"]["top_countries"])
    write_cr_sector(out / "cr_sector.csv", sector_cells(exposures, params0, projected, cfg, sector_paths, sector_use),
                    top, int(manifest["reference_date"][:4]))

    summary = {
        "reference_date": manifest["reference_date"], "sim_mapping_release": manifest.get("mapping_release"),
        "scenario": cfg["name"], "segments": len(segments), "exposures": len(exposures),
        "starting_point": {k: round(sum(seg_stock[s][st][i] for s in segments), 2)
                           for k, st, i in (("exp_s1", "stage1", 0), ("exp_s2", "stage2", 0), ("exp_s3", "stage3", 0),
                                            ("exp_poci", "poci", 0), ("prov_s1", "stage1", 1), ("prov_s2", "stage2", 1),
                                            ("prov_s3", "stage3", 1), ("prov_poci", "poci", 1))},
        "totals": {f"{sc}/{y}": {k: round(v, 2) for k, v in d.items()} for (sc, y), d in sorted(totals.items())},
    }

    if bcfg:
        summary["benchmark"] = benchmark_summary(bcfg, decisions, pivots)
    if scfg:
        summary["sector_satellites"] = sector_summary(scfg, coefficients, exposures, sector_use, sector_rows)

    if cfg.get("off_balance"):                     # CR_SCEN_OFF_BS (optional; on-balance results are unaffected)
        def check_modelled(segment, code):
            """The on-balance rule for an item: a portfolio without satellite coefficients needs, per group, the
            segment's benchmark or the sectoral satellite of the item's sector."""
            if segment.split("|")[1] in sat:
                return
            applied = {g for g, d in (decisions or {}).get(segment, {}).items() if d["benchmark"] not in ("", "unavailable")}
            for g, coef in SECTOR_COEFFICIENTS.items():
                if g not in applied and not (code and coefficients[code][coef] is not None):
                    raise KeyError(f"no satellite coefficients for portfolio {segment.split('|')[1]} ({segment}, {code})")

        ob_rows, stats = project_off_balance(con, sim, cfg, manifest, top, segments, projected, exposures, sector_paths,
                                             coefficients, check_modelled)
        write_off_balance(out / "off_balance.csv", ob_rows)
        write_cr_scen_off_bs(out / "cr_scen_off_bs.csv", ob_rows, int(manifest["reference_date"][:4]))
        ob_totals = defaultdict(lambda: dict.fromkeys(OFF_BALANCE_FIELDS, 0.0))
        for r in ob_rows:
            for k in OFF_BALANCE_FIELDS:
                ob_totals[(r["scenario"], r["year"])][k] += r[k]
        summary["off_balance"] = {
            "exposure_types": list(cfg["off_balance"]["exposure_types"]), **stats,
            "totals": {f"{sc}/{y}": {k: round(v, 2) for k, v in d.items()} for (sc, y), d in sorted(ob_totals.items())},
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
