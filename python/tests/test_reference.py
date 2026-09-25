"""Tests of the independent reference implementation (tools/reference) and the golden results."""

import csv
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("sora_reference", REPO / "tools" / "reference" / "sora_reference.py")
ref = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ref)

CFG = {"constraints": {"adverse_final_year_blend": [5 / 6, 1 / 6]}}


def flat(**kw):
    p = {k: 0.0 for k in ref.PARAMS}
    p.update(kw)
    return {t: dict(p) for t in (1, 2, 3, 4)}


def stock(s1=0.0, s2=0.0, s3=0.0, poci=0.0, prov=(0.0, 0.0, 0.0, 0.0)):
    return {"stage1": [s1, prov[0]], "stage2": [s2, prov[1]], "stage3": [s3, prov[2]], "poci": [poci, prov[3]]}


def approx(a, b):
    return a == pytest.approx(b, abs=1e-9)


def test_boxes_hand_computed_year1():
    """Round numbers, computed by hand from EBA 2027 draft MN Boxes 3-9."""
    P = flat(pd12m_s1=0.02, pd12m_s2=0.10, tr1_2=0.05, tr2_1=0.20, lgd_s1=0.40, lgd_s2=0.50, lgd_s3=0.60, lrlt_s2=0.08)
    rows = ref.project_segment(stock(1000, 200, 100, 10, prov=(3, 10, 50, 4)), {"baseline": P}, CFG)
    r = rows[0]
    # Flows: 1000*0.05=50 (S1->S2), 200*0.20=40 (S2->S1), 1000*0.02=20 (S1->S3), 200*0.10=20 (S2->S3)
    assert (r["flow_s1_s2"], r["flow_s2_s1"], r["flow_s1_s3"], r["flow_s2_s3"]) == (50, 40, 20, 20)
    # Exposures: S1 = 1000-50-20+40 = 970. S2 = 200-40-20+50 = 190. New S3 = 40.
    assert approx(r["exp_s1"], 970) and approx(r["exp_s2"], 190) and approx(r["exp_s3_new"], 40)
    # Box 5: 1000*(1-0.05-0.02)*0.02*0.40 = 7.44. Box 4: 40*0.02*0.40 = 0.32
    assert approx(r["prov_s1_s1"], 7.44) and approx(r["prov_s2_s1"], 0.32)
    # Box 6: 50*0.08 = 4. Box 7: 200*(1-0.2-0.1)*0.08 = 11.2
    assert approx(r["prov_s1_s2"], 4.0) and approx(r["prov_s2_s2"], 11.2)
    # Box 8: 20*0.40 = 8, 20*0.50 = 10. Box 9: max(100*0.60, 50) = 60
    assert approx(r["prov_cum_s1_s3"], 8) and approx(r["prov_cum_s2_s3"], 10) and approx(r["prov_old_s3"], 60)
    # Box 3 and impairment: stocks 7.76 + 15.2 + 78, POCI 4 -> total 104.96, start 67 -> charge 37.96
    assert approx(r["prov_stock_s1"], 7.76) and approx(r["prov_stock_s2"], 15.2) and approx(r["prov_stock_s3"], 78)
    assert approx(r["impairment"], 104.96 - 67)


def test_box9_floor_and_no_release():
    """The old S3 provision never falls below the starting provision (no release)."""
    P = flat(lgd_s3=0.10)
    rows = ref.project_segment(stock(0, 0, 100, prov=(0, 0, 30, 0)), {"baseline": P}, CFG)
    assert all(approx(r["prov_old_s3"], 30) for r in rows)
    assert all(r["impairment"] == pytest.approx(0) for r in rows)


def test_exposure_conserved_and_cumulative_s3():
    P = flat(pd12m_s1=0.03, pd12m_s2=0.2, tr1_2=0.1, tr2_1=0.3, lgd_s1=0.3, lgd_s2=0.4, lgd_s3=0.5, lrlt_s2=0.1)
    rows = ref.project_segment(stock(800, 150, 50, 5), {"baseline": P}, CFG)
    for r in rows:
        assert approx(r["exp_s1"] + r["exp_s2"] + r["exp_s3_old"] + r["exp_s3_new"], 1000)
    assert rows[0]["prov_cum_s1_s3"] < rows[1]["prov_cum_s1_s3"] < rows[2]["prov_cum_s1_s3"]


def test_adverse_final_year_blend():
    """Final adverse year: the t+2 loss term is 5/6 adverse + 1/6 baseline (Boxes 4-5)."""
    base = flat(pd12m_s1=0.01, lgd_s1=0.5)
    adv = flat(pd12m_s1=0.04, lgd_s1=0.5)
    rows = ref.project_segment(stock(1000), {"baseline": base, "adverse": adv}, CFG)
    a3 = [r for r in rows if r["scenario"] == "adverse" and r["year"] == 3][0]
    e1 = 1000 * (1 - 0.04) ** 2                                    # stage 1 at the start of year 3
    expected = e1 * (1 - 0.04) * (5 / 6 * 0.04 * 0.5 + 1 / 6 * 0.01 * 0.5)
    assert approx(a3["prov_s1_s1"], expected)


def test_matrix_annualisation():
    m = [[0.99, 0.01, 0.0], [0.1, 0.85, 0.05], [0.0, 0.0, 1.0]]
    a12 = ref.matpow(m, 12)
    assert all(approx(sum(row), 1.0) for row in a12)
    # Direct S1->S3 is 0 monthly, but reachable through S2 within 12 months.
    assert 0.015 < a12[0][2] < 0.025          # ≈ 2%: 1%/month to S2, then 5%/month to S3


def test_golden_results_are_reproducible(reference_sim, tmp_path):
    """tests/golden/20260630 is produced by the reference implementation from the reference mapping.
    Regenerate: python tools/reference/sora_reference.py --sim <sim> --scenario tests/scenarios/test_eba2025.yaml \
    --out tests/golden/20260630"""
    ref.run(reference_sim, REPO / "tests" / "scenarios" / "test_eba2025.yaml", tmp_path, REPO)
    golden = REPO / "tests" / "golden" / "20260630"
    for name in ("segments.csv", "parameters.csv", "projection.csv"):
        assert (tmp_path / name).read_text() == (golden / name).read_text(), name
    a, b = json.loads((tmp_path / "summary.json").read_text()), json.loads((golden / "summary.json").read_text())
    a.pop("sim_mapping_release"), b.pop("sim_mapping_release")
    assert a == b


def test_golden_invariants():
    golden = REPO / "tests" / "golden" / "20260630"
    seg = {r["segment"]: r for r in csv.DictReader(open(golden / "segments.csv"))}
    for r in csv.DictReader(open(golden / "projection.csv")):
        s = seg[r["segment"]]
        start = sum(float(s[k]) for k in ("exp_s1", "exp_s2", "exp_s3"))
        now = sum(float(r[k]) for k in ("exp_s1", "exp_s2", "exp_s3_old", "exp_s3_new"))
        assert abs(start - now) < 0.05, r["segment"]                       # static balance sheet
        assert float(r["prov_old_s3"]) >= float(s["prov_s3"]) - 0.005        # no S3 release
    for r in csv.DictReader(open(golden / "parameters.csv")):
        assert float(r["pd12m_s1"]) + float(r["tr1_2"]) <= 1 + 1e-9
        assert float(r["pd12m_s2"]) + float(r["tr2_1"]) <= 1 + 1e-9
