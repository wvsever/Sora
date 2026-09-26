"""Integration tests of the C++ engine (skipped if it is not built).

Build first:  cmake -S . -B build/release && cmake --build build/release -j
Or set SORA_ENGINE to the `sora` binary.
"""

import csv
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ENGINE = Path(os.environ.get("SORA_ENGINE", REPO / "build" / "release" / "sora"))
SCENARIO = REPO / "tests" / "scenarios" / "test_eba2025.yaml"
GOLDEN = REPO / "tests" / "golden" / "20260630"
PARAMS = ["pd12m_s1", "pd12m_s2", "tr1_2", "tr2_1", "tr3_1", "tr3_2", "lgd_s1", "lgd_s2", "lgd_s3", "lrlt_s2"]

pytestmark = pytest.mark.skipif(not ENGINE.exists(), reason="C++ engine not built")


def run(sim, out, *extra, check=True):
    r = subprocess.run([str(ENGINE), "run", str(sim), "--scenario", str(SCENARIO), "-o", str(out), "--base", str(REPO), *extra],
                       capture_output=True, text=True)
    if check:
        assert r.returncode == 0, r.stderr
    return r


def read(path, *key):
    with open(path) as f:
        return {tuple(r[k] for k in key): r for r in csv.DictReader(f)}


@pytest.fixture(scope="module")
def base_run(reference_sim, tmp_path_factory):
    out = tmp_path_factory.mktemp("base")
    run(reference_sim, out)
    return out


def write_params(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["level", "key", "scenario", "year", *PARAMS, "source"])
        w.writeheader()
        for r in rows:
            w.writerow({"source": "external", **r})


def test_matches_golden(base_run):
    for name, key in (("projection.csv", ("segment", "scenario", "year")), ("parameters.csv", ("key", "scenario", "year"))):
        g, a = read(GOLDEN / name, *key), read(base_run / name, *key)
        assert g.keys() == a.keys()
        for k in g:
            for col in g[k]:
                try:
                    assert abs(float(g[k][col]) - float(a[k][col])) <= 0.01, (name, k, col)
                except ValueError:
                    assert g[k][col] == a[k][col], (name, k, col)


def test_external_starting_point_round_trip(reference_sim, tmp_path):
    """Supplying the derived starting point as customer parameters reproduces the results. The parameter file
    carries 9 decimals, so results agree to that rounding (relative 1e-6 for PDs near 0.1%), not to the cent."""
    start = [r for r in csv.DictReader(open(GOLDEN / "parameters.csv")) if r["scenario"] == "actual"]
    write_params(tmp_path / "p.csv", [{"level": "segment", "key": r["key"], "scenario": "actual", "year": 0,
                                        **{p: r[p] for p in PARAMS}} for r in start])
    run(reference_sim, tmp_path / "out", "--parameters", str(tmp_path / "p.csv"))
    params = read(tmp_path / "out" / "parameters.csv", "key", "scenario", "year")
    assert all(r["source"] == "external" for k, r in params.items() if k[1] == "actual")
    g, a = read(GOLDEN / "projection.csv", "segment", "scenario", "year"), read(tmp_path / "out" / "projection.csv", "segment", "scenario", "year")
    for k in g:
        # A parameter rounded to 9 decimals is off by at most 5e-10, so a money result can be off by up to
        # 5e-10 x the segment's exposure per parameter it depends on; allow 1e-9 x exposure.
        exposure = sum(abs(float(v)) for c, v in g[k].items() if c.startswith("exp_"))
        for c in g[k]:
            if c not in ("segment", "scenario", "year"):
                gv, av = float(g[k][c]), float(a[k][c])
                assert abs(gv - av) <= 0.05 + 1e-6 * abs(gv) + 1e-9 * exposure, (k, c, gv, av)


def test_segment_hierarchy_and_projection_overrides(reference_sim, base_run, tmp_path):
    seg = "LOANS|HH_HOUSE|BE"
    write_params(tmp_path / "p.csv", [
        {"level": "segment", "key": "LOANS|HH_HOUSE|ALL", "scenario": "actual", "year": 0, "lgd_s3": "0.5"},    # portfolio level
        {"level": "segment", "key": seg, "scenario": "actual", "year": 0, "pd12m_s1": "0.01"},                   # segment level
        {"level": "segment", "key": seg, "scenario": "adverse", "year": 2, "pd12m_s1": "0.2"},                   # projection
    ])
    run(reference_sim, tmp_path / "out", "--parameters", str(tmp_path / "p.csv"))
    p = read(tmp_path / "out" / "parameters.csv", "key", "scenario", "year")
    assert p[(seg, "actual", "0")]["pd12m_s1"] == "0.010000000"
    assert p[(seg, "actual", "0")]["lgd_s3"] == "0.500000000"                  # inherited from the portfolio level
    assert p[(seg, "actual", "0")]["source"] == "mixed"
    assert p[("LOANS|HH_HOUSE|DE", "actual", "0")]["lgd_s3"] == "0.500000000"   # other countries too
    assert p[(seg, "adverse", "2")]["pd12m_s1"] == "0.200000000"
    assert p[(seg, "adverse", "2")]["source"] == "mixed"
    base = read(base_run / "projection.csv", "segment", "scenario", "year")
    new = read(tmp_path / "out" / "projection.csv", "segment", "scenario", "year")
    assert new[(seg, "adverse", "2")]["flow_s1_s3"] != base[(seg, "adverse", "2")]["flow_s1_s3"]
    # Segments outside HH_HOUSE are untouched.
    k = ("LOANS|HH_CONS|BE", "adverse", "2")
    assert new[k] == base[k]


def test_exposure_level_parameters(reference_sim, base_run, tmp_path):
    seg_rows = list(csv.DictReader(open(base_run / "segments.csv")))
    import duckdb
    eid, = duckdb.sql(f"""SELECT exposure_id FROM read_parquet('{reference_sim}/sim_exposure/**/*.parquet')
                          WHERE stage = 'stage1' AND measurement_category = 'amortised_cost' AND exposure_type = 'loan'
                            AND household_purpose = 'house_purchase' ORDER BY gross_carrying_amount DESC LIMIT 1""").fetchone()
    write_params(tmp_path / "p.csv", [{"level": "exposure", "key": eid, "scenario": "actual", "year": 0, "pd12m_s1": "0.5"}])
    r = run(reference_sim, tmp_path / "out", "--parameters", str(tmp_path / "p.csv"))
    assert "PAR-003" in r.stderr and "(1)" in r.stderr
    base = read(base_run / "projection.csv", "segment", "scenario", "year")
    new = read(tmp_path / "out" / "projection.csv", "segment", "scenario", "year")
    changed = {k[0] for k in base if base[k] != new[k]}
    assert len(changed) == 1 and "HH_HOUSE" in next(iter(changed))
    assert len(seg_rows) == len(list(csv.DictReader(open(GOLDEN / "segments.csv"))))


def test_invalid_parameters_are_rejected(reference_sim, tmp_path):
    write_params(tmp_path / "p.csv", [{"level": "segment", "key": "ALL|ALL|ALL", "scenario": "actual", "year": 0, "lgd_s1": "1.5"}])
    r = run(reference_sim, tmp_path / "out", "--parameters", str(tmp_path / "p.csv"), check=False)
    assert r.returncode == 1 and "PAR-010" in r.stderr
    assert not (tmp_path / "out" / "projection.csv").exists()


def test_unknown_keys_are_reported(reference_sim, tmp_path):
    write_params(tmp_path / "p.csv", [
        {"level": "segment", "key": "LOANS|NO_SUCH|XX", "scenario": "actual", "year": 0, "lgd_s1": "0.1"},
        {"level": "exposure", "key": "NO-SUCH-ID", "scenario": "actual", "year": 0, "lgd_s1": "0.1"}])
    r = run(reference_sim, tmp_path / "out", "--parameters", str(tmp_path / "p.csv"))
    assert "PAR-001" in r.stderr and "PAR-002" in r.stderr


def test_cr_scen_layout_and_consistency(base_run):
    rows = list(csv.DictReader(open(base_run / "cr_scen.csv")))
    summary = json.loads((base_run / "summary.json").read_text())
    geos = {r["Geographical breakdown"] for r in rows}
    assert len(rows) == 8 * len(geos) * 22 and {"Total", "Other"} <= geos     # prior-year Actual, Actual, 2 x 3 years
    assert len(rows[0]) == 9 + 54
    idx = {(r["Geographical breakdown"], r["Scenario"], r["Year"], r["RowNum"]): r for r in rows}
    num = lambda r, c: float(r[c] or 0)  # noqa: E731
    exp = "Total exposure (total Exp)"
    # Actual total equals the starting point (EUR million).
    sp = summary["starting_point"]
    total0 = idx[("Total", "Actual", "2026", "22")]
    assert abs(num(total0, exp) * 1e6 - (sp["exp_s1"] + sp["exp_s2"] + sp["exp_s3"] + sp["exp_poci"])) < 1
    for (geo, scen, year, n), r in idx.items():
        if n != "22":
            continue
        if any(idx[(geo, scen, year, k)][exp] == "" for k in ("1", "8", "9", "10", "11", "12", "13", "18", "22")):
            assert year == "2025"          # prior-year rows: blank where an exposure has no history amount
            continue
        # Total = debt securities + loans; loans = CB + GG + CI + OFC + NFC + HH.
        parts = sum(num(idx[(geo, scen, year, k)], exp) for k in ("1", "8"))
        assert abs(num(r, exp) - parts) < 1e-6
        loans = sum(num(idx[(geo, scen, year, k)], exp) for k in ("9", "10", "11", "12", "13", "18"))
        assert abs(num(idx[(geo, scen, year, "8")], exp) - loans) < 1e-6
    # Projected totals equal the projection summary.
    for key, tot in summary["totals"].items():
        scen, year = key.split("/")
        r = idx[("Total", scen.capitalize(), str(2026 + int(year)), "22")]
        assert abs(num(r, "Stock of provisions (Prov Stock)") * 1e6 -
                   (tot["prov_stock_s1"] + tot["prov_stock_s2"] + tot["prov_stock_s3"] + tot["prov_stock_poci"])) < 1
    # Countries add up to the total (top countries + Other).
    for scen, year in (("Actual", "2026"), ("Adverse", "2029")):
        s = sum(num(idx[(g, scen, year, "22")], exp) for g in geos if g != "Total")
        assert abs(s - num(idx[("Total", scen, year, "22")], exp)) < 1e-6
    # Parameters are percentages within [0, 100].
    for r in rows:
        for c in ("PD 12M S1 (TR1-3)", "TR1-2", "LGD S1", "LRLT S2"):
            if r[c]:
                assert 0 <= float(r[c]) <= 100


def test_cr_sector_matches_golden_and_reconciles_with_cr_scen(base_run):
    """cr_sector.csv equals the golden file (EUR million to 1 cent, percent to 1e-7), and its TOTAL row equals
    the non-financial corporations rows of CR_SCEN (debt securities row 6 + loans row 13) for every geography,
    scenario and year; C = energy-intensive + other; sectors add up to the total."""
    key = ("RowNum", "Geographical breakdown", "Scenario", "Year")
    g, a = read(GOLDEN / "cr_sector.csv", *key), read(base_run / "cr_sector.csv", *key)
    assert g.keys() == a.keys() and len(next(iter(a.values()))) == 8 + 46
    for k in g:
        for col in g[k]:
            if not g[k][col] or col in key or col in ("Pivot", "COREP asset class", "NACE code") or col.startswith("Exposures by"):
                assert g[k][col] == a[k][col], (k, col)
            else:   # one cent in EUR million, 1e-9 in percent, plus the last printed decimal
                pct = "%" in col or col.startswith(("PD ", "TR", "LGD", "LRLT", "Coverage ratio"))
                assert abs(float(g[k][col]) - float(a[k][col])) <= (2.1e-7 if pct else 2.1e-8), (k, col)
    scen = read(base_run / "cr_scen.csv", "Geographical breakdown", "Scenario", "Year", "RowNum")
    num = lambda r, c: float(r[c] or 0)  # noqa: E731
    amounts = ("Total exposure (total Exp)", "of which: stage 1 (Exp S1)", "of which: stage 2 (Exp S2)",
               "Non-performing exposure (Exp S3)", "POCI exposures (Exp POCI)", "Stock of provisions (Prov Stock)",
               "of which: non-performing assets (Prov Stock S3)", "Provisions old stage 3 (Prov old S3-S3)",
               "Stage 3 flow (SX-S3 flow)")
    totals = [k for k in a if k[0] == "23"]
    assert len(totals) == len(a) // 23
    for rn, geo, sc, year in totals:
        t = a[(rn, geo, sc, year)]
        for col in amounts:
            nfc = num(scen[(geo, sc, year, "6")], col) + num(scen[(geo, sc, year, "13")], col)
            assert abs(num(t, col) - nfc) < 1e-6, (geo, sc, year, col)
            if not t[col]:   # prior-year rows: blank where an exposure has no history amount (flows: all Actual rows)
                assert sc == "Actual", (geo, sc, year, col)
            else:
                parts = sum(num(a[(str(n), geo, sc, year)], col) for n in (1, 2, 3, *range(6, 23)))
                assert abs(num(t, col) - parts) < 1e-6, (geo, sc, year, col)              # no unknown sectors here
            c = [a[(n, geo, sc, year)][col] for n in ("3", "4", "5")]
            if all(c):
                assert abs(float(c[0]) - float(c[1]) - float(c[2])) < 1e-6
            else:
                assert sc == "Actual", (geo, sc, year, col)
    exp = "Total exposure (total Exp)"
    # The reference data's manufacturers are all in energy-intensive divisions (C10, C19-C21, C23-C25, C28).
    assert num(a[("4", "Total", "Actual", "2026")], exp) > 0 and num(a[("5", "Total", "Actual", "2026")], exp) == 0
    assert num(a[("23", "Total", "Adverse", "2029")], "Stock of provisions (Prov Stock)") > \
        num(a[("23", "Total", "Baseline", "2029")], "Stock of provisions (Prov Stock)")


def test_collateral_matches_golden_and_cr_scen_ltv(base_run):
    """collateral.csv equals the golden file, and the CR_SCEN LTV columns (percent) are the ratio of the summed
    secured exposure to the summed real-estate collateral value (Total row, all geographies)."""
    key = ("segment", "scenario", "year")
    g, a = read(GOLDEN / "collateral.csv", *key), read(base_run / "collateral.csv", *key)
    assert g.keys() == a.keys()
    for k in g:
        for col in g[k]:
            if col in key or not g[k][col]:
                assert g[k][col] == a[k][col], (k, col)
            else:
                tol = 1e-9 if col.startswith("ltv_") else 0.01
                assert abs(float(g[k][col]) - float(a[k][col])) <= tol, (k, col)
    sums = {}
    for r in a.values():
        s = sums.setdefault((r["scenario"], r["year"]), [0.0] * 6)
        for i, c in enumerate(("secured_exp_s1", "secured_exp_s2", "secured_exp_s3",
                               "re_collateral_s1", "re_collateral_s2", "re_collateral_s3")):
            s[i] += float(r[c])
    cr = {(r["Scenario"], r["Year"]): r for r in csv.DictReader(open(base_run / "cr_scen.csv"))
          if r["Geographical breakdown"] == "Total" and r["RowNum"] == "22"}
    assert len(cr) == 8                                     # prior-year Actual (no LTV), Actual, 2 x 3 years
    assert not cr[("Actual", "2025")]["LTV ratio - Stage 1 (%)"]
    for (scen, year), s in sums.items():
        r = cr[(scen.capitalize(), str(2026 + int(year)))]
        for i in range(3):
            assert s[3 + i] > 0
            assert abs(float(r[f"LTV ratio - Stage {i + 1} (%)"]) - 100 * s[i] / s[3 + i]) < 1e-6, (scen, year, i)
    # Adverse property prices fall: LTV rises over the adverse horizon.
    ltv = [float(cr[("Adverse", str(2026 + t))]["LTV ratio - Stage 1 (%)"]) for t in (1, 2, 3)]
    assert float(cr[("Actual", "2026")]["LTV ratio - Stage 1 (%)"]) < ltv[0] < ltv[1] < ltv[2]
    # Portfolios without real-estate collateral have blank LTV.
    assert all(not r["LTV ratio - Stage 1 (%)"] for r in csv.DictReader(open(base_run / "cr_scen.csv")) if r["RowNum"] == "1")


def test_results_do_not_depend_on_workers(reference_sim, tmp_path):
    """Engine worker threads partition the projection by segment: outputs are byte-identical for any count,
    including exposure-level parameters and the order of reported parameter errors."""
    import duckdb
    ids = [r[0] for r in duckdb.sql(f"""SELECT exposure_id FROM read_parquet('{reference_sim}/sim_exposure/**/*.parquet')
                                        WHERE stage IN ('stage1', 'stage2') AND measurement_category = 'amortised_cost'
                                        ORDER BY hash(exposure_id) LIMIT 40""").fetchall()]
    rows = [{"level": "exposure", "key": e, "scenario": "actual", "year": 0, "pd12m_s1": "0.05", "lgd_s1": "0.3"} for e in ids]
    rows += [{"level": "exposure", "key": e, "scenario": "adverse", "year": 3, "pd12m_s2": "0.4"} for e in ids[:10]]
    write_params(tmp_path / "p.csv", rows)
    outputs = {}
    for w in (1, 3, 8):
        run(reference_sim, tmp_path / f"w{w}", "--workers", str(w), "--parameters", str(tmp_path / "p.csv"))
        outputs[w] = {f.name: f.read_bytes() for f in sorted((tmp_path / f"w{w}").iterdir())}
    assert "projection.csv" in outputs[1] and "cr_scen.csv" in outputs[1] and "nii.csv" in outputs[1]
    assert outputs[1] == outputs[3] == outputs[8]

    write_params(tmp_path / "bad.csv", [{"level": "exposure", "key": e, "scenario": "actual", "year": 0, "lgd_s2": "1.5"}
                                        for e in ids])
    errors = {w: run(reference_sim, tmp_path / f"bad{w}", "--workers", str(w), "--parameters", str(tmp_path / "bad.csv"),
                     check=False) for w in (1, 4)}
    assert errors[1].returncode == errors[4].returncode == 1
    lines = {w: [x for x in r.stderr.splitlines() if "PAR-010" in x] for w, r in errors.items()}
    assert len(lines[1]) == 10 and lines[1] == lines[4]


def test_nii_matches_golden(base_run):
    """nii.csv equals the golden file (amounts to 1 cent, rates to 1e-9) and summary.json "nii" the golden summary."""
    key = ("scenario", "year", "template_row", "currency", "rate_type", "status")
    g, a = read(GOLDEN / "nii.csv", *key), read(base_run / "nii.csv", *key)
    assert g.keys() == a.keys()
    for k in g:
        for col, gv in g[k].items():
            av = a[k][col]
            try:
                tol = 1e-9 if col in ("eir", "margin_new_business") else 0.01
                assert abs(float(gv) - float(av)) <= tol, (k, col, av, gv)
            except ValueError:
                assert gv == av, (k, col)
    golden = json.loads((GOLDEN / "summary.json").read_text())["nii"]
    summary = json.loads((base_run / "summary.json").read_text())["nii"]
    assert summary["positions"] == golden["positions"] and summary["fallbacks"] == golden["fallbacks"]
    for block in ("starting_point", *(f"totals/{k}" for k in golden["totals"])):
        gv = golden["starting_point"] if block == "starting_point" else golden["totals"][block[7:]]
        av = summary["starting_point"] if block == "starting_point" else summary["totals"][block[7:]]
        assert gv.keys() == av.keys()
        assert all(abs(gv[k] - av[k]) <= 0.01 for k in gv), block


def test_nii_is_opt_in_and_leaves_credit_results_unchanged(reference_sim, base_run, tmp_path):
    """Without the scenario key `nii` there is no nii.csv, no summary block, and every other output is identical."""
    text = SCENARIO.read_text()
    start = text.index("\nnii:")
    end = text.index("\nsegmentation:")
    scenario = tmp_path / "no_nii.yaml"
    scenario.write_text(text[:start] + text[end:])
    r = subprocess.run([str(ENGINE), "run", str(reference_sim), "--scenario", str(scenario), "-o", str(tmp_path / "out"),
                        "--base", str(REPO)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "out" / "nii.csv").exists() and "NII-000" not in r.stderr
    for f in base_run.iterdir():
        if f.name in ("nii.csv", "summary.json", "diagnostics.json"):
            continue
        assert (tmp_path / "out" / f.name).read_bytes() == f.read_bytes(), f.name
    with_nii = json.loads((base_run / "summary.json").read_text())
    without = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert "nii" not in without and {k: v for k, v in with_nii.items() if k != "nii"} == without


def test_off_balance_matches_golden_and_cr_scen_off_bs(base_run):
    """off_balance.csv equals the golden file; CR_SCEN_OFF_BS (EUR million) adds up to it and to the summary."""
    key = ("segment", "exposure_type", "scenario", "year")
    g, a = read(GOLDEN / "off_balance.csv", *key), read(base_run / "off_balance.csv", *key)
    assert g.keys() == a.keys()
    for k in g:
        for col in g[k]:
            if col not in key:
                assert abs(float(g[k][col]) - float(a[k][col])) <= 0.01, (k, col)
    cr = {(r["Scenario"], r["Year"], r["RowNum"]): r for r in csv.DictReader(open(base_run / "cr_scen_off_bs.csv"))}
    assert len(cr) == 7 * 22
    num = lambda r, c: float(r[c])  # noqa: E731
    for (scen, year, n), r in cr.items():
        if n != "22":
            continue
        parts = [num(cr[(scen, year, k)], "Stock of provisions (Prov Stock)") for k in ("1", "8", "15")]
        assert abs(num(r, "Stock of provisions (Prov Stock)") - sum(parts)) < 1e-6
        assert abs(num(r, "Total nominal amount after CCF (total PostCCF)") -
                   num(r, "Performing nominal amount after CCF (Perf PostCCF)") -
                   num(r, "Non-performing nominal amount after CCF (PostCCF S3)") -
                   num(r, "POCI nominal amount after CCF (PostCCF POCI)")) < 1e-6
    totals = json.loads((base_run / "summary.json").read_text())["off_balance"]["totals"]
    for label, scen, year in (("Actual", "actual", 0), ("Adverse", "adverse", 3)):
        t = totals[f"{scen}/{year}"]
        r = cr[(label, str(2026 + year), "22")]
        assert abs(num(r, "Total nominal amount before CCF (total NomAmount)") * 1e6 -
                   sum(t[c] for c in ("nom_s1", "nom_s2", "nom_s3_old", "nom_s3_new", "nom_poci"))) < 1
        assert abs(num(r, "Stock of provisions (Prov Stock)") * 1e6 -
                   sum(t[c] for c in ("prov_stock_s1", "prov_stock_s2", "prov_stock_s3", "prov_stock_poci"))) < 1


def test_off_balance_customer_ccf(reference_sim, base_run, tmp_path):
    """A customer CCF (segment hierarchy, then exposure row) replaces the regulatory fallback. On-balance results
    and nominal amounts are unchanged; post-CCF amounts follow the CCF."""
    import duckdb
    eid, nominal = duckdb.sql(f"""
        SELECT exposure_id, CAST(off_balance_amount AS DOUBLE)
        FROM read_parquet('{reference_sim}/sim_exposure/**/*.parquet')
        WHERE exposure_type = 'loan_commitment' AND stage = 'stage1' AND currency = 'EUR'
          AND measurement_category = 'amortised_cost' AND NOT coalesce(is_intragroup, false)
        ORDER BY off_balance_amount DESC, exposure_id LIMIT 1""").fetchone()
    with open(tmp_path / "p.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["level", "key", "scenario", "year", *PARAMS, "ccf", "source"])
        w.writerow(["segment", "ALL|ALL|ALL", "actual", 0, *[""] * len(PARAMS), "1.0", "external"])
        w.writerow(["exposure", eid, "actual", 0, *[""] * len(PARAMS), "0.0", "external"])
    r = run(reference_sim, tmp_path / "out", "--parameters", str(tmp_path / "p.csv"))
    assert "OBS-003" in r.stderr
    assert (tmp_path / "out" / "projection.csv").read_bytes() == (base_run / "projection.csv").read_bytes()
    summary = json.loads((tmp_path / "out" / "summary.json").read_text())["off_balance"]
    base = json.loads((base_run / "summary.json").read_text())["off_balance"]
    assert summary["customer_ccf_items"] == summary["items"] == base["items"]
    t, b = summary["totals"]["actual/0"], base["totals"]["actual/0"]
    assert t["nom_s1"] == b["nom_s1"]
    # CCF 1 everywhere except that exposure (0): post-CCF stage 1 = nominal stage 1 - its nominal.
    assert abs(t["exp_s1"] - (t["nom_s1"] - nominal)) < 0.05
    assert t["exp_s1"] > b["exp_s1"]


BENCHMARK_COLUMNS = ("PD/TR - Percentage of exposures for which ECB benchmark parameters were used (%)",
                     "LGD/LR - Percentage of exposures for which ECB benchmark parameters were used (%)")


def test_benchmarks_match_golden_and_cr_scen(base_run):
    """ECB benchmark rule on the reference data (scenario key benchmark_parameters): benchmarks.csv and the summary
    match the golden results; CR_SCEN reports the exposure share with benchmark parameters (MN 2027 para 117)."""
    g, a = read(GOLDEN / "benchmarks.csv", "segment"), read(base_run / "benchmarks.csv", "segment")
    assert g.keys() == a.keys()
    for k in g:
        assert abs(float(g[k]["exposure"]) - float(a[k]["exposure"])) <= 0.01
        assert {c: v for c, v in g[k].items() if c != "exposure"} == {c: v for c, v in a[k].items() if c != "exposure"}
    summary = json.loads((base_run / "summary.json").read_text())["benchmark"]
    golden = json.loads((GOLDEN / "summary.json").read_text())["benchmark"]
    assert {k: v for k, v in summary.items() if k != "pivots"} == {k: v for k, v in golden.items() if k != "pivots"}
    assert "BMK-001" in (base_run / "diagnostics.json").read_text()
    rows = {(r["Geographical breakdown"], r["Scenario"], r["Year"], r["RowNum"]): r
            for r in csv.DictReader(open(base_run / "cr_scen.csv"))}
    for (geo, scen, year, n), r in rows.items():
        for c in BENCHMARK_COLUMNS:
            assert 0.0 <= float(r[c]) <= 100.0 + 1e-9
            if scen == "Actual":
                assert float(r[c]) == 0.0                  # the starting point is always the institution's own
    for scen, year in (("Baseline", "2027"), ("Adverse", "2029")):
        assert float(rows[("Total", scen, year, "11")][BENCHMARK_COLUMNS[0]]) == 100.0     # loans to CI: 0% coverage
        assert float(rows[("Total", scen, year, "9")][BENCHMARK_COLUMNS[1]]) == 0.0        # CB: no benchmark exists
        pv = summary["pivots"]["LOANS|GG"]
        assert abs(float(rows[("Total", scen, year, "10")][BENCHMARK_COLUMNS[0]]) - 100 * pv["pd_tr_benchmark_share"]) < 1e-6
        total = sum(v["exposure"] * v["pd_tr_benchmark_share"] for v in summary["pivots"].values())
        exposure = sum(v["exposure"] for v in summary["pivots"].values())
        assert abs(float(rows[("Total", scen, year, "22")][BENCHMARK_COLUMNS[0]]) - 100 * total / exposure) < 1e-6


def test_benchmark_rule_variant_matches_reference(reference_sim, tmp_path):
    """With segment-level models only and a 50% threshold, the rule exercises every branch on the reference data
    (coverage, segments without a model, sovereigns, PD/TR and LGD/LR separately); engine and reference agree, and
    without the scenario key nothing is benchmarked."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("sora_reference", REPO / "tools" / "reference" / "sora_reference.py")
    ref = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ref)
    text = SCENARIO.read_text()
    variant = tmp_path / "variant.yaml"
    variant.write_text(text.replace("model_level: portfolio", "model_level: segment")
                       .replace("coverage_threshold: 0.10", "coverage_threshold: 0.50"))
    ref.run(reference_sim, variant, tmp_path / "ref", REPO)
    r = subprocess.run([str(ENGINE), "run", str(reference_sim), "--scenario", str(variant), "-o", str(tmp_path / "eng"),
                        "--base", str(REPO), "--workers", "3"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    rules = {row["pd_tr_rule"] for row in csv.DictReader(open(tmp_path / "ref" / "benchmarks.csv"))}
    rules |= {row["lgd_lr_rule"] for row in csv.DictReader(open(tmp_path / "ref" / "benchmarks.csv"))}
    assert rules == {"none", "coverage", "no_model", "sovereign"}
    for name, key in (("benchmarks.csv", ("segment",)), ("parameters.csv", ("key", "scenario", "year")),
                      ("projection.csv", ("segment", "scenario", "year"))):
        g, a = read(tmp_path / "ref" / name, *key), read(tmp_path / "eng" / name, *key)
        assert g.keys() == a.keys()
        for k in g:
            for col in g[k]:
                try:
                    assert abs(float(g[k][col]) - float(a[k][col])) <= 0.01, (name, k, col)
                except ValueError:
                    assert g[k][col] == a[k][col], (name, k, col)
    assert "mixed" in {row["source"] for row in csv.DictReader(open(tmp_path / "eng" / "parameters.csv"))}

    plain = tmp_path / "plain.yaml"
    lines, skip = [], False
    for line in text.splitlines():
        skip = line.startswith("benchmark_parameters:") or (skip and line.startswith(" "))
        if not skip:
            lines.append(line)
    plain.write_text("\n".join(lines) + "\n")
    r = subprocess.run([str(ENGINE), "run", str(reference_sim), "--scenario", str(plain), "-o", str(tmp_path / "plain"),
                        "--base", str(REPO)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "plain" / "benchmarks.csv").exists()
    assert "benchmark" not in json.loads((tmp_path / "plain" / "summary.json").read_text())
    params = list(csv.DictReader(open(tmp_path / "plain" / "parameters.csv")))
    assert {row["source"] for row in params} == {"derived"}


def test_facility_switches(reference_sim, base_run, tmp_path):
    """off_balance.include_loan_undrawn moves the undrawn share of loan allowances off-balance (exposures unchanged,
    provisions on- plus off-balance unchanged); without both switches, loans keep their whole allowance and no
    commitment is on-balance."""
    text = SCENARIO.read_text()
    assert "include_loan_undrawn: true" in text and "commitment_drawn_on_balance: true" in text
    runs = {}
    for name, drop in (("undrawn", ("commitment_drawn_on_balance",)),
                       ("none", ("commitment_drawn_on_balance", "include_loan_undrawn"))):
        yaml = tmp_path / f"{name}.yaml"
        yaml.write_text("".join(line for line in text.splitlines(keepends=True) if not any(k in line for k in drop)))
        out = tmp_path / name
        r = subprocess.run([str(ENGINE), "run", str(reference_sim), "--scenario", str(yaml), "-o", str(out),
                            "--base", str(REPO)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        runs[name] = (out, json.loads((out / "summary.json").read_text()), r.stderr)
    (u, su, eu), (n, sn, en) = runs["undrawn"], runs["none"]
    assert "OBS-004" in eu and "OBS-004" not in en and "OBS-005" not in eu
    assert su["exposures"] == sn["exposures"] and sn["off_balance"]["loan_undrawn_items"] == 0
    for k in ("exp_s1", "exp_s2", "exp_s3", "exp_poci"):
        assert su["starting_point"][k] == sn["starting_point"][k]
    prov = lambda s: (sum(s["starting_point"][k] for k in ("prov_s1", "prov_s2", "prov_s3", "prov_poci")) +  # noqa: E731
                      sum(s["off_balance"]["totals"]["actual/0"][k]
                          for k in ("prov_stock_s1", "prov_stock_s2", "prov_stock_s3", "prov_stock_poci")))
    assert abs(prov(su) - prov(sn)) < 1.0                                      # moved, not double counted
    assert su["starting_point"]["prov_s1"] < sn["starting_point"]["prov_s1"]
    assert "loan" not in {r["exposure_type"] for r in csv.DictReader(open(n / "off_balance.csv"))}
    # The drawn part of commitments adds exposure and the drawn share of their allowance (base run: both switches).
    base = json.loads((base_run / "summary.json").read_text())
    assert base["exposures"] - sn["exposures"] == base["off_balance"]["commitment_drawn_exposures"] > 0
    assert prov(base) > prov(su)


SECTOR_COLUMNS = ("PD/TR - Percentage of exposures with projections based on sectoral models, e.g. via sensitivities by sector (%)",
                  "LGD/LR - Percentage of exposures with projections based on sectoral models, e.g. via sensitivities by sector (%)")


def test_sector_satellites_match_golden(base_run):
    """Sectoral (GVA) satellites (scenario key sector_satellites): sector_parameters.csv and the summary match the
    golden results; CR_SECTOR columns 1-2 report the exposure share projected with sectoral models."""
    key = ("segment", "sector", "scenario", "year")
    g, a = read(GOLDEN / "sector_parameters.csv", *key), read(base_run / "sector_parameters.csv", *key)
    assert g.keys() == a.keys() and len(g) > 0
    for k in g:
        for col in g[k]:
            if col in PARAMS:
                assert abs(float(g[k][col]) - float(a[k][col])) <= 1e-9, (k, col)
            else:
                assert g[k][col] == a[k][col], (k, col)
    summary = json.loads((base_run / "summary.json").read_text())["sector_satellites"]
    golden = json.loads((GOLDEN / "summary.json").read_text())["sector_satellites"]
    assert summary.keys() == golden.keys()
    for k, v in golden.items():
        assert (abs(summary[k] - v) <= 0.01) if isinstance(v, float) else summary[k] == v, k
    diagnostics = (base_run / "diagnostics.json").read_text()
    assert "SEC-000" in diagnostics and "SEC-001" in diagnostics
    rows = read(base_run / "cr_sector.csv", "RowNum", "Geographical breakdown", "Scenario", "Year")
    for (n, geo, scen, year), r in rows.items():
        for i, c in enumerate(SECTOR_COLUMNS):
            assert 0.0 <= float(r[c]) <= 100.0 + 1e-9
            if scen == "Actual":
                assert float(r[c]) == 0.0
            elif n == "23" and geo == "Total":
                assert abs(float(r[c]) - 100 * summary[("pd_tr_share", "lgd_lr_share")[i]]) < 1e-6


def sector_variant(tmp_path, satellites: str, sectors: str) -> Path:
    """The test scenario with other portfolio and sector satellite files (absolute paths)."""
    (tmp_path / "sat.csv").write_text(satellites)
    (tmp_path / "sec.csv").write_text(sectors)
    text = SCENARIO.read_text().replace("satellites: tests/params/synthetic_satellites.csv", f"satellites: {tmp_path / 'sat.csv'}")
    text = text.replace("file: tests/params/synthetic_sector_satellites.csv", f"file: {tmp_path / 'sec.csv'}")
    variant = tmp_path / "variant.yaml"
    variant.write_text(text)
    return variant


def test_sector_satellites_without_portfolio_model_match_reference(reference_sim, tmp_path):
    """NFC portfolios without satellite coefficients, every NACE sector with both sectoral coefficients: the sectoral
    satellites are the model (ECB benchmark rule: coverage from the sectors), on- and off-balance, engine and
    reference agree. Without the sector file the NFC portfolios have no model: both stop."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("sora_reference", REPO / "tools" / "reference" / "sora_reference.py")
    ref = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ref)
    sats = "".join(line for line in (REPO / "tests" / "params" / "synthetic_satellites.csv").read_text().splitlines(keepends=True)
                   if not line.startswith("NFC"))
    sectors = "sector,beta_gva,lgd_gva_sensitivity,description\n" + "".join(
        f"{c},-0.{10 + i % 7},0.{3 + i % 5},x\n" for i, c in enumerate(ref.GVA_SECTOR))
    variant = sector_variant(tmp_path, sats, sectors)
    ref.run(reference_sim, variant, tmp_path / "ref", REPO)
    r = subprocess.run([str(ENGINE), "run", str(reference_sim), "--scenario", str(variant), "-o", str(tmp_path / "eng"),
                        "--base", str(REPO), "--workers", "3"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    for name, key in (("projection.csv", ("segment", "scenario", "year")), ("benchmarks.csv", ("segment",)),
                      ("sector_parameters.csv", ("segment", "sector", "scenario", "year")),
                      ("off_balance.csv", ("segment", "exposure_type", "scenario", "year"))):
        g, a = read(tmp_path / "ref" / name, *key), read(tmp_path / "eng" / name, *key)
        assert g.keys() == a.keys(), name
        for k in g:
            for col in g[k]:
                try:
                    assert abs(float(g[k][col]) - float(a[k][col])) <= 0.01, (name, k, col)
                except ValueError:
                    assert g[k][col] == a[k][col], (name, k, col)
    bm = {row["segment"]: row for row in csv.DictReader(open(tmp_path / "eng" / "benchmarks.csv"))}
    assert bm["LOANS|NFC_SME_OTHER|BE"]["pd_tr_model"] == "1" and bm["LOANS|NFC_SME_OTHER|BE"]["pd_tr_rule"] == "none"
    uses = {(row["pd_tr"], row["lgd_lr"]) for row in csv.DictReader(open(tmp_path / "eng" / "sector_parameters.csv"))}
    assert uses == {("sectoral", "sectoral")}
    summary = json.loads((tmp_path / "eng" / "summary.json").read_text())["sector_satellites"]
    assert summary["pd_tr_share"] == 1.0 and summary["lgd_lr_share"] == 1.0

    plain = tmp_path / "plain.yaml"
    plain.write_text("".join(line for line in variant.read_text().splitlines(keepends=True)
                             if not line.startswith(("sector_satellites:", "  file: /", "  gva_fallback:"))))
    assert "sector_satellites:" not in plain.read_text() and "benchmark_parameters:" in plain.read_text()
    r = subprocess.run([str(ENGINE), "run", str(reference_sim), "--scenario", str(plain), "-o", str(tmp_path / "plain"),
                        "--base", str(REPO)], capture_output=True, text=True)
    assert r.returncode != 0 and "no satellite coefficients for portfolio NFC" in r.stderr
    with pytest.raises(KeyError):
        ref.run(reference_sim, plain, tmp_path / "plain_ref", REPO)


def test_sector_satellites_off(reference_sim, base_run, tmp_path):
    """Without the scenario key: no sector_parameters.csv, CR_SECTOR columns 1-2 are 0, only NFC results change
    (the segment parameter paths are the portfolio model's with or without it)."""
    text = SCENARIO.read_text()
    lines, skip = [], False
    for line in text.splitlines():
        skip = line.startswith("sector_satellites:") or (skip and line.startswith(" "))
        if not skip:
            lines.append(line)
    plain = tmp_path / "plain.yaml"
    plain.write_text("\n".join(lines) + "\n")
    r = subprocess.run([str(ENGINE), "run", str(reference_sim), "--scenario", str(plain), "-o", str(tmp_path / "out"),
                        "--base", str(REPO)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = tmp_path / "out"
    assert not (out / "sector_parameters.csv").exists()
    assert "sector_satellites" not in json.loads((out / "summary.json").read_text())
    assert (out / "parameters.csv").read_text() == (base_run / "parameters.csv").read_text()
    for r in csv.DictReader(open(out / "cr_sector.csv")):
        assert float(r[SECTOR_COLUMNS[0]]) == 0.0 and float(r[SECTOR_COLUMNS[1]]) == 0.0
    a, b = read(out / "projection.csv", "segment", "scenario", "year"), read(base_run / "projection.csv", "segment", "scenario", "year")
    changed = {k[0] for k in a if a[k] != b[k]}
    assert changed and all("|NFC" in s for s in changed)


# ----------------------------------------------------------------------------------------- prior-year Actual rows

STOCK_COLUMNS = {"of which: stage 1 (Exp S1)": "exp_s1", "of which: stage 2 (Exp S2)": "exp_s2",
                 "Non-performing exposure (Exp S3)": "exp_s3", "POCI exposures (Exp POCI)": "exp_poci",
                 "of which: stage 1 (Prov Stock S1)": "prov_s1", "of which: stage 2 (Prov Stock S2)": "prov_s2",
                 "of which: non-performing assets (Prov Stock S3)": "prov_s3", "of which: POCI (Prov Stock POCI)": "prov_poci"}


def test_cr_scen_prior_year_rows(base_run):
    """CR_SCEN starts with the prior-year Actual rows (31 Dec 2025, MN 2027 draft para 71 and Table 2): stocks of the t0
    portfolios from prior_year.csv, blank where an exposure of the row has no history amount; no parameters, flows,
    overlays, maturity or LTV."""
    rows = list(csv.DictReader(open(base_run / "cr_scen.csv")))
    geos = {r["Geographical breakdown"] for r in rows}
    prior = rows[:len(geos) * 22]
    assert {(r["Scenario"], r["Year"]) for r in prior} == {("Actual", "2025")}
    assert rows[len(geos) * 22]["Year"] == "2026"
    segments = list(csv.DictReader(open(base_run / "prior_year.csv")))
    assert segments == list(csv.DictReader(open(GOLDEN / "prior_year.csv")))
    by_row = {"19": "LOANS|HH_HOUSE|", "20": "LOANS|HH_CONS|", "14": "LOANS|NFC_SME_CRE|", "16": "LOANS|NFC_LARGE_CRE|"}
    total = {r["RowNum"]: r for r in prior if r["Geographical breakdown"] == "Total"}
    for num, prefix in by_row.items():
        members = [s for s in segments if s["segment"].startswith(prefix)]
        for col, field in STOCK_COLUMNS.items():
            assert abs(float(total[num][col]) * 1e6 - sum(float(s[field]) for s in members)) < 0.05, (num, col)
    for col, field in STOCK_COLUMNS.items():
        if field.startswith("prov"):
            assert abs(float(total["22"][col]) * 1e6 - sum(float(s[field]) for s in segments)) < 0.05, col
        else:
            assert total["22"][col] == "" and total["1"][col] == ""        # debt securities: no history amounts
    for r in prior:
        for c in ("PD 12M S1 (TR1-3)", "LGD S3", "Stage 2 flow (S1-S2 flow)", "Provisions old stage 3 (Prov old S3-S3)",
                  "of which: overlays stage 1 (Overlays S1)", "Average Maturity (yrs)", "LTV ratio - Stage 1 (%)"):
            assert r[c] == "", (r["RowNum"], c)
        if r["Total exposure (total Exp)"]:
            assert float(r["of which: cumulative new non-performing exposure (Cumul New Exp S3)"]) == 0.0
    diagnostics = (base_run / "diagnostics.json").read_text()
    assert "PRY-000" in diagnostics and "PRY-003" in diagnostics


def test_prior_year_variant_matches_reference(reference_sim, tmp_path):
    """Loans only (no commitments on-balance) at the last complete history month end of 2025 (30 Sep 2025, prior_year_end):
    every exposure has an amount, so the prior-year rows are filled; engine and reference agree (prior_year.csv,
    CR_SECTOR, summary), and the engine output is identical for --workers 1, 3 and 8."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("sora_reference", REPO / "tools" / "reference" / "sora_reference.py")
    ref = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ref)
    lines, skip = [], False
    for line in SCENARIO.read_text().splitlines():
        skip = line.startswith("off_balance:") or (skip and line.startswith(" "))
        if not skip:
            lines.append(line.replace("exposure_types: [loan, finance_lease, debt_security]", "exposure_types: [loan]"))
    lines.append("prior_year_end: 2025-09-30")
    variant = tmp_path / "variant.yaml"
    variant.write_text("\n".join(lines) + "\n")
    ref.run(reference_sim, variant, tmp_path / "ref", REPO)
    outputs = {}
    for w in (1, 3, 8):
        r = subprocess.run([str(ENGINE), "run", str(reference_sim), "--scenario", str(variant), "-o", str(tmp_path / f"w{w}"),
                            "--base", str(REPO), "--workers", str(w)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        outputs[w] = {n: (tmp_path / f"w{w}" / n).read_bytes() for n in ("cr_scen.csv", "cr_sector.csv", "prior_year.csv")}
    assert outputs[1] == outputs[3] == outputs[8]
    eng, gold = tmp_path / "w3", tmp_path / "ref"
    assert (eng / "prior_year.csv").read_text() == (gold / "prior_year.csv").read_text()
    p = json.loads((eng / "summary.json").read_text())["prior_year"]
    assert p == json.loads((gold / "summary.json").read_text())["prior_year"]
    assert p["date"] == "2025-09-30" and p["missing_amount"] == 0 and p["missing_fx"] == 0 and p["exposures"] > 30000
    assert all(v is not None for v in p["stocks"].values())
    key = ("RowNum", "Geographical breakdown", "Scenario", "Year")
    g, a = read(gold / "cr_sector.csv", *key), read(eng / "cr_sector.csv", *key)
    assert g.keys() == a.keys()
    for k in g:
        for col in g[k]:
            if not g[k][col] or col in key or col in ("Pivot", "COREP asset class", "NACE code") or col.startswith("Exposures by"):
                assert g[k][col] == a[k][col], (k, col)
            else:
                pct = "%" in col or col.startswith(("PD ", "TR", "LGD", "LRLT", "Coverage ratio"))
                assert abs(float(g[k][col]) - float(a[k][col])) <= (2.1e-7 if pct else 2.1e-8), (k, col)
    total = a[("23", "Total", "Actual", "2025")]
    assert float(total["Total exposure (total Exp)"]) > 0 and float(total["Coverage ratio: non-performing exposure"]) > 0
    # CR_SCEN prior-year Total = CR_SECTOR TOTAL for the NFC rows; loans total = prior_year.csv.
    scen = read(eng / "cr_scen.csv", "Geographical breakdown", "Scenario", "Year", "RowNum")
    exp = "Total exposure (total Exp)"
    assert abs(float(scen[("Total", "Actual", "2025", "13")][exp]) - float(total[exp])) < 1e-6
    loans = sum(float(r[f"exp_{k}"]) for r in csv.DictReader(open(eng / "prior_year.csv")) for k in ("s1", "s2", "s3", "poci"))
    assert abs(float(scen[("Total", "Actual", "2025", "22")][exp]) * 1e6 - loans) < 1
