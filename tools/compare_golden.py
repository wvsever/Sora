#!/usr/bin/env python3
"""Run the C++ engine on the reference SIM and compare its output with the golden results.

Tolerances: money per segment/scenario/year 1 cent or relative 1e-12 (whichever is larger); parameters and
ratios (LTV) 1e-9. EBA template layouts print amounts in EUR million and parameters and ratios in percent:
cr_sector.csv uses the same tolerances in those units, plus one unit in the last printed decimal for rounding;
cr_scen_off_bs.csv (EUR million, 8 decimals) is compared to 2e-8 (2 cents). benchmarks.csv (ECB benchmark rule):
exposure to 1 cent, the model flags, rules and benchmark keys exactly; summary.json "benchmark": counts exactly,
pivot exposures to 1 cent and coverage shares to 1e-9. sector_parameters.csv (sectoral satellites): parameters to
1e-9, keys, GVA keys and group sources exactly; summary.json "sector_satellites": counts exactly, exposures to 1 cent
and shares to 1e-9. prior_year.csv (prior-year Actual stocks): counts exactly, amounts to 1 cent, blank cells exactly;
summary.json "prior_year": counts, date and flags exactly, amounts to 1 cent (null exactly).
The reference SIM is produced on demand (extract test data + reference mapping) if --sim is not given.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def ensure_sim() -> Path:
    sim = REPO / "build" / "sim" / "20260630"
    if not (sim / "sim_manifest.json").exists():
        subprocess.run([sys.executable, str(REPO / "tools" / "extract_testdata.py")], check=True)
        sys.path.insert(0, str(REPO / "python"))
        from sora_tools.mapping import run_mapping
        run_mapping(REPO / "mappings" / "cppbank", REPO / "build" / "testdata" / "20260630", sim, log=lambda *_: None)
    return sim


def rows(path: Path, key_cols: list[str]) -> dict:
    with open(path) as f:
        return {tuple(r[k] for k in key_cols): r for r in csv.DictReader(f)}


def template_percent(col: str) -> bool:
    """EBA template columns in percent (parameters and ratios); the others are amounts in EUR million."""
    return "%" in col or col.startswith(("PD ", "TR", "LGD", "LRLT", "Coverage ratio"))


def template_tolerance(col: str, golden: float) -> float:
    """1 cent (EUR million, 8 decimals) or 1e-9 (percent, 7 decimals), plus the last printed decimal."""
    if template_percent(col):
        return 1e-7 + 1e-7 + 1e-12
    return max(0.01, 1e-12 * abs(golden) * 1e6) / 1e6 + 1e-8 + 1e-12


def compare(name: str, golden: Path, actual: Path, keys: list[str], money: bool, ratio_prefix: str | None = None,
            tolerance=None, abs_tol: float = 0.01) -> list[str]:
    """`money`: amounts at `abs_tol` (default 1 cent) or a relative 1e-12, except columns starting with `ratio_prefix`
    (1e-9). Otherwise all 1e-9. `tolerance(column, golden value)`, if given, replaces both."""
    g, a = rows(golden / name, keys), rows(actual / name, keys)
    errors = []
    if set(g) != set(a):
        errors.append(f"{name}: key sets differ (missing {sorted(set(g) - set(a))[:3]}, extra {sorted(set(a) - set(g))[:3]})")
    worst = (0.0, None)
    for k in sorted(set(g) & set(a)):
        for col, gv in g[k].items():
            av = a[k][col]
            try:
                gf, af = float(gv), float(av)
            except ValueError:
                if gv != av:
                    errors.append(f"{name} {k} {col}: {av!r} != {gv!r}")
                continue
            is_money = money and not (ratio_prefix and col.startswith(ratio_prefix))
            tol = tolerance(col, gf) if tolerance else max(abs_tol, 1e-12 * abs(gf)) if is_money else 1e-9
            diff = abs(af - gf)
            if diff > worst[0]:
                worst = (diff, (k, col))
            if diff > tol:
                errors.append(f"{name} {k} {col}: engine {av} vs golden {gv} (diff {diff:.3g})")
    print(f"  {name:16s} {len(g):6d} rows, max abs diff {worst[0]:.3g}" + (f" at {worst[1]}" if worst[1] else ""))
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--engine", required=True)
    ap.add_argument("--golden", type=Path, required=True)
    ap.add_argument("--scenario", type=Path, required=True)
    ap.add_argument("--sim", type=Path)
    args = ap.parse_args()
    sim = args.sim or ensure_sim()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        subprocess.run([str(Path(args.engine).resolve()), "run", str(sim), "--scenario", str(args.scenario), "-o", str(out),
                        "--base", str(REPO)], check=True)
        errors = []
        errors += compare("segments.csv", args.golden, out, ["segment"], money=True)
        errors += compare("parameters.csv", args.golden, out, ["key", "scenario", "year"], money=False)
        errors += compare("projection.csv", args.golden, out, ["segment", "scenario", "year"], money=True)
        errors += compare("collateral.csv", args.golden, out, ["segment", "scenario", "year"], money=True, ratio_prefix="ltv_")
        errors += compare("cr_sector.csv", args.golden, out, ["RowNum", "Geographical breakdown", "Scenario", "Year"],
                          money=True, tolerance=template_tolerance)
        if (args.golden / "prior_year.csv").exists():         # prior-year Actual rows (stocks at the prior year-end)
            errors += compare("prior_year.csv", args.golden, out, ["segment"], money=True)
        if (args.golden / "off_balance.csv").exists():        # CR_SCEN_OFF_BS (scenario key off_balance)
            errors += compare("off_balance.csv", args.golden, out, ["segment", "exposure_type", "scenario", "year"], money=True)
            errors += compare("cr_scen_off_bs.csv", args.golden, out, ["RowNum", "Scenario", "Year"], money=True,
                              abs_tol=2e-8)
        if (args.golden / "benchmarks.csv").exists():         # ECB benchmark rule (scenario key benchmark_parameters)
            errors += compare("benchmarks.csv", args.golden, out, ["segment"], money=True)
        if (args.golden / "sector_parameters.csv").exists():  # sectoral satellites (scenario key sector_satellites)
            errors += compare("sector_parameters.csv", args.golden, out, ["segment", "sector", "scenario", "year"],
                              money=False)
        gs, es = json.loads((args.golden / "summary.json").read_text()), json.loads((out / "summary.json").read_text())
        for field in ("segments", "exposures"):
            if gs[field] != es[field]:
                errors.append(f"summary {field}: engine {es[field]} vs golden {gs[field]}")
        if "prior_year" in gs:
            gp, ep = gs["prior_year"], es.get("prior_year", {})
            for field, v in gp.items():
                values = v.items() if isinstance(v, dict) else [(None, v)]
                for sub, gv in values:
                    ev = ep.get(field, {}).get(sub) if sub else ep.get(field)
                    ok = abs(ev - gv) <= 0.01 if isinstance(gv, float) and isinstance(ev, (int, float)) else ev == gv
                    if not ok:
                        errors.append(f"summary prior_year.{field}{'.' + sub if sub else ''}: engine {ev} vs golden {gv}")
        if "off_balance" in gs:
            for field in ("items", "fallback_items", "unmatched_items", "customer_ccf_items", "loan_undrawn_items",
                          "commitment_drawn_exposures"):
                if field in gs["off_balance"] and gs["off_balance"][field] != es.get("off_balance", {}).get(field):
                    errors.append(f"summary off_balance.{field}: engine {es.get('off_balance', {}).get(field)} vs golden "
                                  f"{gs['off_balance'][field]}")
        if "benchmark" in gs:
            gb, eb = gs["benchmark"], es.get("benchmark", {})
            for field in ("segments_pd_tr", "segments_lgd_lr", "segments_unavailable", "model_level", "sovereign"):
                if gb[field] != eb.get(field):
                    errors.append(f"summary benchmark.{field}: engine {eb.get(field)} vs golden {gb[field]}")
            if set(gb["pivots"]) != set(eb.get("pivots", {})):
                errors.append("summary benchmark.pivots: pivot asset classes differ")
            for pivot, gv in gb["pivots"].items():
                for field, v in gv.items():
                    ev = eb.get("pivots", {}).get(pivot, {}).get(field)
                    if ev is None or abs(ev - v) > (0.01 if field == "exposure" else 1e-9):
                        errors.append(f"summary benchmark.pivots.{pivot}.{field}: engine {ev} vs golden {v}")
        if "sector_satellites" in gs:
            gv, ev = gs["sector_satellites"], es.get("sector_satellites", {})
            for field, v in gv.items():
                if isinstance(v, float):
                    tol = 0.01 if field.endswith("exposure") else 1e-9
                    if not isinstance(ev.get(field), (int, float)) or abs(ev[field] - v) > tol:
                        errors.append(f"summary sector_satellites.{field}: engine {ev.get(field)} vs golden {v}")
                elif ev.get(field) != v:
                    errors.append(f"summary sector_satellites.{field}: engine {ev.get(field)} vs golden {v}")
    for e in errors[:30]:
        print("  MISMATCH", e)
    print(f"{'FAILED' if errors else 'PASSED'}: {len(errors)} mismatches")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
