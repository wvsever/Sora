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
        for c in g[k]:
            if c not in ("segment", "scenario", "year"):
                gv, av = float(g[k][c]), float(a[k][c])
                assert abs(gv - av) <= 0.05 + 1e-6 * abs(gv), (k, c, gv, av)


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
    assert len(seg_rows) == 152


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
    assert len(rows) == 7 * len(geos) * 22 and {"Total", "Other"} <= geos
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
