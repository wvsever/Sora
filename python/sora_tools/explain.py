"""Explain a Sora result: why a segment's impairment is what it is.

``explain_result(output_dir, segment, scenario, year)`` reads the engine output (``segments.csv``,
``parameters.csv``, ``benchmarks.csv``, ``projection.csv``, ``off_balance.csv``) and returns structured JSON plus a
short narrative:

* starting stocks (exposure and provisions per stage, coverage),
* parameters per scenario and year with their source (``derived`` / ``external`` / ``mixed`` / ``benchmark``),
  the calibration level of every parameter group and the ECB benchmark rule that applied,
* stage flows and the provision components per EBA box (MN Boxes 3-9), with the impairment of each year
  reconciled to the change in the provision stock,
* for NFC segments with sectoral (GVA) satellites (``sector_parameters.csv``): the NACE sectors with their own
  parameter path, which group each takes from the sectoral model, and its GVA key. The segment's parameters in
  ``parameters.csv`` are then the portfolio model's, used by the exposures of sectors without coefficients.

Without a segment it returns an overview: totals per scenario and year and the segments with the largest
impairment. Everything is aggregate (segment level); no exposure data is read.
"""

from __future__ import annotations

import difflib
from typing import Any

from .results import (
    PARAMS, SCENARIOS, ResultError, RunOutput, eur_m, num, pct, start_exposure, start_provisions, stock,
)

SOURCES = {
    "derived": "Sora's own values: calibrated from the SIM history (starting point) and projected with the "
               "satellite models (scenario years)",
    "external": "customer parameter file (--parameters or sim_risk_parameter), all fields",
    "mixed": "some fields from the customer file or the ECB benchmark, the others Sora's own values",
    "benchmark": "ECB benchmark parameters for both groups (PD/TR and LGD/LR), without adjustment",
}
CALIBRATION_GROUPS = {
    "stage1": ("pd12m_s1", "tr1_2"),
    "stage2": ("pd12m_s2", "tr2_1"),
    "stage3": ("tr3_1", "tr3_2"),
    "lgd": ("lgd_s1", "lgd_s2", "lgd_s3"),
    "lrlt": ("lrlt_s2",),
}
SECTOR_SOURCES = {
    "sectoral": "the sectoral satellite: the sector's real GVA path drives the group (MN para 114)",
    "portfolio": "the portfolio satellite: the sector has no sectoral coefficient for the group",
    "benchmark": "the segment's ECB benchmark, which replaces the sectoral model",
    "none": "flat (the portfolio has no satellite coefficients and the sector no sectoral one)",
}
BENCHMARK_RULES = {
    "none": "no benchmark: the satellite model covers the group",
    "sovereign": "general governments: the country's ECB benchmark is mandatory (MN para 146)",
    "coverage": "the pivot asset class has less than the threshold of model coverage (MN paras 115-117)",
    "no_model": "the segment has no model in a pivot class above the coverage threshold",
}
# Provision components: (column, EBA box, description)
COMPONENTS = (
    ("prov_s1_s1", "Box 5", "stage 1 staying in stage 1: Exp S1 x (1 - TR1-2 - PD S1) x PD S1(t+1) x LGD S1(t+1)"),
    ("prov_s2_s1", "Box 4", "cured from stage 2 to stage 1: flow S2-S1 x PD S1(t+1) x LGD S1(t+1)"),
    ("prov_s1_s2", "Box 6", "migrated from stage 1 to stage 2: flow S1-S2 x LRLT S2"),
    ("prov_s2_s2", "Box 7", "stage 2 staying in stage 2: Exp S2 x (1 - TR2-1 - PD S2) x LRLT S2"),
    ("prov_cum_s1_s3", "Box 8", "cumulative new stage 3 from stage 1: flow S1-S3 x LGD S1"),
    ("prov_cum_s2_s3", "Box 8", "cumulative new stage 3 from stage 2: flow S2-S3 x LGD S2"),
    ("prov_old_s3", "Box 9", "existing stage 3: max(Exp x LGD S3, starting provision) per exposure (MN para 141)"),
)
FLOWS = ("flow_s1_s2", "flow_s2_s1", "flow_s1_s3", "flow_s2_s3")
STOCKS = {"s1": "prov_stock_s1", "s2": "prov_stock_s2", "s3": "prov_stock_s3", "poci": "prov_stock_poci"}


def _level_label(level_key: str, segment: str) -> str:
    if level_key == segment:
        return "segment"
    parts = level_key.split("|")
    if parts == ["ALL", "ALL", "ALL"]:
        return "all"
    if len(parts) == 3 and parts[1:] == ["ALL", "ALL"]:
        return "instrument"
    if len(parts) == 3 and parts[2] == "ALL":
        return "portfolio"
    return "other"


def parse_calibration_levels(text: str, segment: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for part in filter(None, (text or "").split(";")):
        group, _, key = part.partition("=")
        out[group] = {"key": key, "level": _level_label(key, segment),
                      "parameters": list(CALIBRATION_GROUPS.get(group, ()))}
    return out


def _params(row: dict[str, str]) -> dict[str, float | None]:
    return {p: num(row.get(p)) for p in PARAMS}


def _points(out: RunOutput, scenario: str | None, year: int | None) -> list[tuple[str, int]]:
    pts = out.scenario_points()
    if scenario:
        pts = [p for p in pts if p[0] == scenario]
    if year is not None:
        pts = [p for p in pts if p[1] == int(year)]
    if not pts:
        raise ResultError(f"no projection rows for scenario={scenario!r} year={year!r}; "
                          f"available: {[f'{s}/{y}' for s, y in out.scenario_points()]}")
    return pts


def explain_result(output_dir, segment: str | None = None, scenario: str | None = None,
                   year: int | None = None, top: int = 10) -> dict[str, Any]:
    out = RunOutput(output_dir)
    if not segment:
        return _overview(out, scenario, year, top)
    segs = out.segments()
    if segment not in segs:
        close = difflib.get_close_matches(segment, list(segs), n=5, cutoff=0.5)
        raise ResultError(f"segment {segment!r} not in {out.path / 'segments.csv'}"
                          + (f"; similar: {close}" if close else ""))
    seg = segs[segment]
    pts = _points(out, scenario, year)
    result: dict[str, Any] = {
        "output_dir": str(out.path),
        "segment": segment,
        "scenario_filter": scenario,
        "year_filter": year,
        "run": {k: out.summary.get(k) for k in ("reference_date", "scenario", "sim_mapping_release")},
        "segment_info": {k: seg.get(k) for k in ("instrument", "portfolio", "country", "macro_key")}
        | {"contracts": int(num(seg.get("contracts")) or 0)},
        "starting_point": _starting_point(seg),
        "parameters": _parameters(out, segment, pts),
        "benchmark": _benchmark(out.benchmarks().get(segment)),
        "projection": _projection(out, segment, seg, pts),
    }
    if out.has("sector_parameters.csv"):
        result["sectors"] = _sectors(out, segment, pts)
    if out.has("off_balance.csv"):
        result["off_balance"] = _off_balance(out, segment, pts)
    result["narrative"] = _narrative(result)
    return result


def _starting_point(seg: dict[str, str]) -> dict[str, Any]:
    exp = {s: num(seg.get(f"exp_{s}")) or 0.0 for s in ("s1", "s2", "s3", "poci")}
    prov = {s: num(seg.get(f"prov_{s}")) or 0.0 for s in ("s1", "s2", "s3", "poci")}
    total_e, total_p = start_exposure(seg), start_provisions(seg)
    return {
        "exposure": exp | {"total": total_e},
        "provisions": prov | {"total": total_p},
        "stage_share": {s: (exp[s] / total_e if total_e else None) for s in exp},
        "coverage": {s: (prov[s] / exp[s] if exp[s] else None) for s in exp}
        | {"total": total_p / total_e if total_e else None},
    }


def _parameters(out: RunOutput, segment: str, pts: list[tuple[str, int]]) -> dict[str, Any]:
    params = out.parameters()
    start = params.get(("segment", segment, "actual", 0))
    res: dict[str, Any] = {"starting_point": None, "path": [], "exposure_level_rows": 0}
    if start:
        res["starting_point"] = {
            "values": _params(start), "source": start.get("source"),
            "source_meaning": SOURCES.get(start.get("source", ""), ""),
            "calibration_levels": parse_calibration_levels(start.get("calibration_levels", ""), segment),
        }
    base = _params(start) if start else {}
    for sc, y in sorted({(s, y) for s, y in pts} | {(s, yy) for s, _ in pts for yy in (1, 2, 3)},
                        key=lambda p: (SCENARIOS.index(p[0]) if p[0] in SCENARIOS else 9, p[1])):
        row = params.get(("segment", segment, sc, y))
        if not row:
            continue
        vals = _params(row)
        rel = {p: (vals[p] / base[p] - 1) for p in PARAMS
               if vals.get(p) is not None and base.get(p) not in (None, 0.0)}
        res["path"].append({"scenario": sc, "year": y, "values": vals, "source": row.get("source"),
                            "source_meaning": SOURCES.get(row.get("source", ""), ""),
                            "change_vs_start": rel})
    res["exposure_level_rows"] = sum(1 for (lvl, *_), _r in params.items() if lvl == "exposure")
    return res


def _benchmark(row: dict[str, str] | None) -> dict[str, Any] | None:
    if not row:
        return None
    res: dict[str, Any] = {"exposure": num(row.get("exposure"))}
    for grp, label in (("pd_tr", "PD/TR"), ("lgd_lr", "LGD/LR")):
        rule = row.get(f"{grp}_rule", "")
        key = row.get(f"{grp}_benchmark", "")
        res[grp] = {
            "group": label,
            "model_coverage": num(row.get(f"{grp}_model")),
            "rule": rule,
            "rule_meaning": BENCHMARK_RULES.get(rule, ""),
            "benchmark_key": key or None,
            "applied": bool(key) and key != "unavailable",
            "unavailable": key == "unavailable",
        }
    return res


def _projection(out: RunOutput, segment: str, seg: dict[str, str], pts) -> list[dict[str, Any]]:
    proj = out.projection()
    rows = []
    for sc, y in pts:
        r = proj.get((segment, sc, y))
        if not r:
            continue
        prev = proj.get((segment, sc, y - 1)) if y > 1 else None
        prev_stock = stock(prev) if prev else start_provisions(seg)
        prev_stages = ({k: num(prev.get(c)) or 0.0 for k, c in STOCKS.items()} if prev else
                       {k: num(seg.get(f"prov_{k}")) or 0.0 for k in STOCKS})
        stocks = {k: num(r.get(c)) or 0.0 for k, c in STOCKS.items()}
        comps = [{"column": c, "box": box, "description": d, "amount": num(r.get(c)) or 0.0}
                 for c, box, d in COMPONENTS]
        s3_check = sum(x["amount"] for x in comps if x["column"] in ("prov_cum_s1_s3", "prov_cum_s2_s3", "prov_old_s3"))
        impairment = num(r.get("impairment")) or 0.0
        total = sum(stocks.values())
        rows.append({
            "scenario": sc, "year": y,
            "exposure": {c: num(r.get(c)) or 0.0 for c in ("exp_s1", "exp_s2", "exp_s3_old", "exp_s3_new", "exp_poci")},
            "flows": {c: num(r.get(c)) or 0.0 for c in FLOWS},
            "provision_components": comps,
            "provision_stock": {"box": "Box 3"} | stocks | {"total": total},
            "stock_change_by_stage": {k: stocks[k] - prev_stages[k] for k in stocks},
            "impairment": impairment,
            "checks": {
                "impairment_equals_stock_change": abs((total - prev_stock) - impairment) <= max(0.05, 1e-9 * abs(total)),
                "stage3_stock_equals_boxes_8_9": abs(s3_check - stocks["s3"]) <= max(0.05, 1e-9 * abs(s3_check)),
            },
        })
    return rows


def _sectors(out: RunOutput, segment: str, pts) -> list[dict[str, Any]]:
    """Sectoral satellite paths of the segment (empty for non-NFC segments)."""
    want = set(pts)
    res: dict[str, dict[str, Any]] = {}
    for (seg, sector, sc, y), r in out.sector_parameters().items():
        if seg != segment:
            continue
        s = res.setdefault(sector, {
            "sector": sector, "gva_sector": r.get("gva_sector"), "gva_key": r.get("gva_key"),
            "gva_relative": r.get("gva_relative") == "1",
            "pd_tr": r.get("pd_tr"), "lgd_lr": r.get("lgd_lr"),
            "pd_tr_meaning": SECTOR_SOURCES.get(r.get("pd_tr", ""), ""),
            "lgd_lr_meaning": SECTOR_SOURCES.get(r.get("lgd_lr", ""), ""), "path": []})
        if (sc, y) in want:
            s["path"].append({"scenario": sc, "year": y, "values": _params(r)})
    return list(res.values())


def _off_balance(out: RunOutput, segment: str, pts) -> list[dict[str, Any]]:
    want = set(pts)
    res = []
    for r in out.rows("off_balance.csv"):
        if r["segment"] == segment and (r["scenario"], int(r["year"])) in want:
            res.append({"exposure_type": r["exposure_type"], "scenario": r["scenario"], "year": int(r["year"]),
                        "nominal": sum(num(r.get(c)) or 0.0 for c in ("nom_s1", "nom_s2", "nom_s3_old", "nom_s3_new", "nom_poci")),
                        "post_ccf": sum(num(r.get(c)) or 0.0 for c in ("exp_s1", "exp_s2", "exp_s3_old", "exp_s3_new", "exp_poci")),
                        "provision_stock": stock(r), "impairment": num(r.get("impairment")) or 0.0})
    return res


def _narrative(r: dict[str, Any]) -> str:
    info, sp = r["segment_info"], r["starting_point"]
    e, share, cov = sp["exposure"], sp["stage_share"], sp["coverage"]
    lines = [
        f"Segment {r['segment']} ({info.get('instrument')}, portfolio {info.get('portfolio')}, country "
        f"{info.get('country')}, macro key {info.get('macro_key')}), {info['contracts']:,d} contracts.",
        f"Starting point: exposure {eur_m(e['total'])} (S1 {pct(share['s1'], 1)}, S2 {pct(share['s2'], 1)}, "
        f"S3 {pct(share['s3'], 1)}, POCI {pct(share['poci'], 1)}), provisions {eur_m(sp['provisions']['total'])} "
        f"(coverage S1 {pct(cov['s1'])}, S2 {pct(cov['s2'])}, S3 {pct(cov['s3'])}).",
    ]
    par = r["parameters"]["starting_point"]
    if par:
        v = par["values"]
        lv = par["calibration_levels"]
        levels = ", ".join(f"{g} from {x['level']} {x['key']}" for g, x in lv.items()) if lv else "no calibration levels"
        lines.append(f"Starting parameters ({par['source']}): PD S1 {pct(v['pd12m_s1'], 3)}, PD S2 {pct(v['pd12m_s2'], 3)}, "
                     f"TR1-2 {pct(v['tr1_2'])}, LGD S1 {pct(v['lgd_s1'])}, LRLT S2 {pct(v['lrlt_s2'])}, "
                     f"LGD S3 {pct(v['lgd_s3'])}; calibration: {levels}.")
    b = r["benchmark"]
    if b:
        for grp in ("pd_tr", "lgd_lr"):
            g = b[grp]
            if g["applied"]:
                lines.append(f"{g['group']}: ECB benchmark {g['benchmark_key']} replaces the projected values "
                             f"(rule {g['rule']}: {g['rule_meaning']}).")
            elif g["unavailable"]:
                lines.append(f"{g['group']}: the benchmark rule {g['rule']} applies but the file has no benchmark "
                             "for this segment, so the model parameters are kept (BMK-002).")
    for p in r["projection"]:
        par_row = next((x for x in r["parameters"]["path"] if x["scenario"] == p["scenario"] and x["year"] == p["year"]), None)
        f, st = p["flows"], p["stock_change_by_stage"]
        biggest = max(p["provision_components"], key=lambda c: abs(c["amount"]))
        txt = (f"{p['scenario'].capitalize()} year {p['year']}: impairment {eur_m(p['impairment'])}"
               f" (stock change S1 {eur_m(st['s1'])}, S2 {eur_m(st['s2'])}, S3 {eur_m(st['s3'])}); "
               f"flows S1-S2 {eur_m(f['flow_s1_s2'])}, S2-S1 {eur_m(f['flow_s2_s1'])}, "
               f"to S3 {eur_m(f['flow_s1_s3'] + f['flow_s2_s3'])}; stock {eur_m(p['provision_stock']['total'])}, "
               f"largest component {biggest['box']} {biggest['column']} {eur_m(biggest['amount'])}")
        if par_row:
            ch = par_row["change_vs_start"]
            txt += (f"; parameters {par_row['source']}, PD S1 {pct(par_row['values']['pd12m_s1'], 3)}"
                    + (f" ({ch['pd12m_s1'] * 100:+.0f}% vs start)" if "pd12m_s1" in ch else ""))
        lines.append(txt + ".")
    secs = r.get("sectors") or []
    if secs:
        own = [s["sector"] for s in secs if "sectoral" in (s["pd_tr"], s["lgd_lr"])]
        rel = sorted({s["gva_key"] for s in secs if s["gva_relative"]})
        lines.append(f"Sectoral (GVA) satellites: exposures of sectors {', '.join(own) or 'none'} take their sector's "
                     "path (sector_parameters.csv); the parameters above are the portfolio model's, for the other "
                     "sectors" + (f"; no sectoral GVA for this country: GDP plus the sector's {', '.join(rel)} GVA "
                                  "deviation" if rel else "") + ".")
    ob = r.get("off_balance") or []
    if ob:
        imp = sum(x["impairment"] for x in ob)
        lines.append(f"Off-balance items of the segment ({', '.join(sorted({x['exposure_type'] for x in ob}))}): "
                     f"impairment {eur_m(imp)} over the selected years.")
    bad = [f"{p['scenario']}/{p['year']}" for p in r["projection"] if not all(p["checks"].values())]
    if bad:
        lines.append(f"Warning: internal consistency checks failed for {', '.join(bad)}.")
    return "\n".join(lines)


def _overview(out: RunOutput, scenario: str | None, year: int | None, top: int) -> dict[str, Any]:
    pts = _points(out, scenario, year)
    proj = out.projection()
    totals = []
    for sc, y in pts:
        rows = [r for (s, a, b), r in proj.items() if a == sc and b == y]
        totals.append({"scenario": sc, "year": y,
                       "impairment": sum(num(r.get("impairment")) or 0.0 for r in rows),
                       "provision_stock": sum(stock(r) for r in rows)})
    by_seg: dict[str, float] = {}
    for (s, sc, y), r in proj.items():
        if (sc, y) in set(pts):
            by_seg[s] = by_seg.get(s, 0.0) + (num(r.get("impairment")) or 0.0)
    segs = out.segments()
    top_segs = [{"segment": s, "impairment": v, "exposure": start_exposure(segs.get(s))}
                for s, v in sorted(by_seg.items(), key=lambda kv: -abs(kv[1]))[:top]]
    sources: dict[str, int] = {}
    for (lvl, _k, sc, y), r in out.parameters().items():
        if lvl == "segment" and (sc, y) in set(pts):
            sources[r.get("source", "")] = sources.get(r.get("source", ""), 0) + 1
    total_imp = sum(t["impairment"] for t in totals)
    sector = out.summary.get("sector_satellites")
    narrative = [f"Run {out.summary.get('scenario')} at {out.summary.get('reference_date')}: "
                 f"{out.summary.get('segments', len(segs))} segments, {out.summary.get('exposures', 'n/a')} exposures."]
    narrative += [f"{t['scenario'].capitalize()} year {t['year']}: impairment {eur_m(t['impairment'])}, "
                  f"provision stock {eur_m(t['provision_stock'])}." for t in totals]
    if top_segs:
        narrative.append("Largest contributors over the selected years: " + ", ".join(
            f"{x['segment']} {eur_m(x['impairment'])} ({x['impairment'] / total_imp:.0%})" if total_imp else x["segment"]
            for x in top_segs[:5]) + ".")
    if sector:
        narrative.append(f"Sectoral (GVA) satellites: {sector.get('pd_tr_share', 0):.1%} of the NFC exposure is projected "
                         f"with a sectoral PD/TR model and {sector.get('lgd_lr_share', 0):.1%} with a sectoral LGD/LR model "
                         "(CR_SECTOR columns 1-2).")
    return {"output_dir": str(out.path), "scenario_filter": scenario, "year_filter": year,
            "run": {k: out.summary.get(k) for k in ("reference_date", "scenario", "sim_mapping_release", "segments", "exposures")},
            "totals": totals, "top_segments": top_segs, "parameter_sources": sources, "sector_satellites": sector,
            "diagnostics": [{k: d.get(k) for k in ("id", "severity", "count", "message")} for d in out.diagnostics],
            "narrative": "\n".join(narrative)}
