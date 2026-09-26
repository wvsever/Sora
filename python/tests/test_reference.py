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


def test_box9_floor_per_exposure():
    """Para 141: the floor applies per exposure. max(100*0.5, 70) + max(100*0.5, 10) = 70 + 50 = 120,
    while the segment-level max(200*0.5, 80) would give 100."""
    P = flat(lgd_s3=0.5)
    st = stock(0, 0, 200, prov=(0, 0, 80, 0))
    assert approx(ref.project_segment(st, {"baseline": P}, CFG)[0]["prov_old_s3"], 100)
    rows = ref.project_segment(st, {"baseline": P}, CFG, s3_exposures=[(100, 70), (100, 10)])
    assert approx(rows[0]["prov_old_s3"], 120)


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


MACRO_CFG = {"year_map": {1: 2025, 2: 2026, 3: 2027}, "country_fallback": ["WR", "EU"]}


def test_collateral_index_and_country_fallback():
    macro = {("real_gdp", k, "baseline", 2025): 1.0 for k in ("BE", "WR", "EU")}
    for sc, g in (("baseline", (2.0, 2.0, 2.0)), ("adverse", (-10.0, -5.0, 0.0))):
        for y, v in zip((2025, 2026, 2027), g):
            macro[("residential_property_prices", "BE", sc, y)] = v
            macro[("commercial_property_prices", "WR", sc, y)] = v / 2
    idx = ref.collateral_index("residential_property", "BE", macro, MACRO_CFG)
    assert approx(idx[("adverse", 0)], 1) and approx(idx[("adverse", 1)], 0.9)
    assert approx(idx[("adverse", 3)], 0.9 * 0.95) and approx(idx[("baseline", 2)], 1.02 ** 2)
    # IS has no scenario data: WR is used.
    assert approx(ref.collateral_index("commercial_property", "IS", macro, MACRO_CFG)[("adverse", 1)], 0.95)
    # Other collateral keeps its value.
    assert all(v == 1.0 for v in ref.collateral_index("cash", None, macro, MACRO_CFG).values())


def test_collateral_pro_rata_allocation():
    """NULL allocated amounts: market value pro rata to the GCA of the in-scope exposures; out of scope ignored."""
    allocs = [("E1", "C1", None, "residential_property", 300.0, "BE", 2.0),
              ("E2", "C1", None, "residential_property", 300.0, "BE", 2.0),
              ("E9", "C1", None, "residential_property", 300.0, "BE", 2.0),      # out of scope
              ("E1", "C2", 50.0, "commercial_property", 999.0, "BE", 1.0),
              ("E3", "C3", None, "cash", 10.0, None, 1.0)]
    v = {(e, t): x for e, t, _, x in ref.allocated_values(allocs, {"E1": 100.0, "E2": 300.0, "E3": 0.0})}
    assert approx(v[("E1", "residential_property")], 600 * 0.25) and approx(v[("E2", "residential_property")], 600 * 0.75)
    assert approx(v[("E1", "commercial_property")], 50) and approx(v[("E3", "cash")], 10)
    assert not any(e == "E9" for e, _ in v)


def test_nace_sector():
    """NACE Rev. 2 and Rev. 2.1 codes map to Rev. 2.1 sections by division; C splits into energy-intensive
    (C10-C12, C17-C30) and other."""
    cases = {"C24.10": "C_EI", "C10.11": "C_EI", "C30.30": "C_EI", "C13.10": "C_OT", "C16": "C_OT", "C": "C_OT",
             "L68.20": "M", "M68.20": "M", "J62.01": "K", "J58.11": "J", "K64.20": "L", "M69.10": "N", "N77.11": "O",
             "Q86.10": "R", "R93.11": "S", "S96.02": "T", "G45.11": "G", "24.10": "C_EI", " f41.20 ": "F", "L": "L",
             "T97.00": "UNKNOWN", "U99.00": "UNKNOWN", "U": "UNKNOWN", "C4": "UNKNOWN", "": "UNKNOWN", None: "UNKNOWN",
             "n/a": "UNKNOWN"}
    for code, sector in cases.items():
        assert ref.nace_sector(code) == sector, code


def test_sector_cells_add_up_to_the_segment():
    """Carrying the sector through the projection: the (segment, sector) projections add up to the segment."""
    P = flat(pd12m_s1=0.02, pd12m_s2=0.10, tr1_2=0.05, tr2_1=0.20, lgd_s1=0.40, lgd_s2=0.50, lgd_s3=0.60, lrlt_s2=0.08)
    exposures = [{"segment": "LOANS|NFC_SME_OTHER|BE", "portfolio": "NFC_SME_OTHER", "nace_code": code, "stage": st,
                  "gca": g, "allowance": a}
                 for code, st, g, a in (("C24.10", "stage1", 1000, 3), ("F41.20", "stage1", 500, 2),
                                        ("C24.10", "stage2", 200, 10), ("F41.20", "stage3", 100, 70),
                                        ("A01.11", "stage3", 100, 10), ("A01.11", "poci", 10, 4))]
    exposures.append({**exposures[0], "segment": "LOANS|HH_CONS|BE", "portfolio": "HH_CONS"})   # not NFC
    projected = {"LOANS|NFC_SME_OTHER|BE": {"baseline": P, "adverse": P}}
    params0 = {"LOANS|NFC_SME_OTHER|BE": P[1]}
    cells = ref.sector_cells(exposures, params0, projected, {"constraints": {"adverse_final_year_blend": [5 / 6, 1 / 6]}})
    assert sorted(k[1] for k in cells) == ["A", "C_EI", "F"]
    stock = {"stage1": [1500, 5], "stage2": [200, 10], "stage3": [200, 80], "poci": [10, 4]}
    whole = ref.project_segment(stock, projected["LOANS|NFC_SME_OTHER|BE"], CFG, [(100, 70), (100, 10)])
    for row in whole:
        slot = ref.SLOTS.index((row["scenario"], row["year"]))
        for k in ("exp_s1", "exp_s2", "exp_s3_new", "flow_s1_s3", "prov_old_s3", "prov_s1_s1", "prov_s2_s2"):
            assert approx(sum(c[slot][k] for c in cells.values()), row[k]), k
        assert approx(sum(c[slot]["prov_s3"] for c in cells.values()), row["prov_stock_s3"])
    # Year-1 S1 weights are the t0 stage 1 stocks; the energy-intensive part has 1000 of 1500.
    assert approx(cells[("LOANS|NFC_SME_OTHER|BE", "C_EI")][1]["w"][0], 1000)


def test_golden_results_are_reproducible(reference_sim, tmp_path):
    """tests/golden/20260630 is produced by the reference implementation from the reference mapping.
    Regenerate: python tools/reference/sora_reference.py --sim <sim> --scenario tests/scenarios/test_eba2025.yaml \
    --out tests/golden/20260630"""
    ref.run(reference_sim, REPO / "tests" / "scenarios" / "test_eba2025.yaml", tmp_path, REPO)
    golden = REPO / "tests" / "golden" / "20260630"
    for name in ("segments.csv", "parameters.csv", "projection.csv", "collateral.csv", "cr_sector.csv",
                 "off_balance.csv", "cr_scen_off_bs.csv", "benchmarks.csv", "sector_parameters.csv", "nii.csv"):
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
    actual = {}
    for r in csv.DictReader(open(golden / "collateral.csv")):             # static balance sheet LTV
        secured = [r[f"secured_exp_s{i}"] for i in (1, 2, 3)]
        assert actual.setdefault(r["segment"], secured) == secured          # t0 exposure in every year
        for st in ("s1", "s2", "s3"):
            if r[f"ltv_{st}"]:
                ltv, sec, col = float(r[f"ltv_{st}"]), float(r[f"secured_exp_{st}"]), float(r[f"re_collateral_{st}"])
                assert abs(ltv - sec / col) < 1e-6 + 0.005 * (1 + ltv) / col       # amounts printed to the cent
            else:
                assert float(r[f"re_collateral_{st}"]) == 0
    for r in csv.DictReader(open(golden / "parameters.csv")):
        assert float(r["pd12m_s1"]) + float(r["tr1_2"]) <= 1 + 1e-9
        assert float(r["pd12m_s2"]) + float(r["tr2_1"]) <= 1 + 1e-9


def test_nii_calendar_and_curves():
    """NII helpers: month arithmetic clamps to the month end; linear interpolation, flat beyond the curve."""
    day = ref.to_day(ref.date(2024, 1, 31))
    assert ref.add_months(day, 1) == ref.to_day(ref.date(2024, 2, 29))
    assert ref.add_months(day, 13) == ref.to_day(ref.date(2025, 2, 28))
    assert ref.add_months(day, -2) == ref.to_day(ref.date(2023, 11, 30))
    curve = [(0.25, 0.01), (1.0, 0.02), (10.0, 0.04)]
    assert ref.interp(curve, 0.1) == 0.01 and ref.interp(curve, 30) == 0.04
    assert approx(ref.interp(curve, 5.5), 0.03)
    assert [ref.tenor_months(t) for t in ("1M", "3M", "1Y", "30Y")] == [1, 3, 12, 360]


def test_nii_template_rows():
    """CSV_NII_CALC rows: assets by instrument and counterparty sector, deposits by sector and sight/term."""
    assert ref.asset_row("loan", "household", "house_purchase") == 5
    assert ref.asset_row("finance_lease", "household", None) == 6
    assert ref.asset_row("debt_security", "household", None) == 11
    assert ref.asset_row("debt_security", "other_financial", None) == 9
    assert ref.asset_row("loan", "central_bank", None) == 1
    assert ref.deposit_row("central_bank", True) == ref.deposit_row("central_bank", False) == 22
    assert ref.deposit_row("household", True) == 28 and ref.deposit_row("household", False) == 29
    assert ref.deposit_row("credit_institution", True) == 24 and ref.deposit_row("other_financial", False) == 30
    assert ref.deposit_row("non_financial_corporation", True) == 26
    assert {r for r, (side, _, _) in ref.NII_ROWS.items() if side == "asset"} == set(range(1, 12))


def test_nii_golden_invariants():
    """Static balance sheet: every cell keeps its t0 volume in all years and scenarios; performing interest is the sum
    of its reference-rate and margin parts; the summary totals add up nii.csv; the adverse cap is applied."""
    golden = REPO / "tests" / "golden" / "20260630"
    rows = list(csv.DictReader(open(golden / "nii.csv")))
    t0 = {(r["template_row"], r["currency"], r["rate_type"], r["status"]): r["volume"] for r in rows if r["year"] == "0"}
    by_year = {}
    for r in rows:
        assert r["volume"] == t0[(r["template_row"], r["currency"], r["rate_type"], r["status"])]
        if r["status"] == "performing":
            assert abs(float(r["interest"]) - float(r["interest_reference"]) - float(r["interest_margin"])) <= 0.011
        else:
            assert r["interest_reference"] == r["interest_margin"] == "" and float(r["provisions"]) >= 0
        side = by_year.setdefault((r["scenario"], r["year"]), {"asset": 0.0, "liability": 0.0})
        side[r["side"]] += float(r["interest"])
    nii = json.loads((golden / "summary.json").read_text())["nii"]
    sp = nii["starting_point"]
    assert abs(by_year[("actual", "0")]["asset"] - sp["interest_income"]) < 1
    assert abs(by_year[("actual", "0")]["liability"] - sp["interest_expense"]) < 1
    for key, tot in nii["totals"].items():
        scen, year = key.split("/")
        assert abs(by_year[(scen, year)]["asset"] - tot["interest_income"]) < 1
        assert abs(by_year[(scen, year)]["liability"] - tot["interest_expense"]) < 1
        if scen == "adverse":
            assert tot["nii_cap"] <= sp["nii"] + 0.01 and tot["nii_capped"] == min(tot["nii"], tot["nii_cap"])
    assert nii["positions"]["sight_deposits"] <= nii["positions"]["deposits"]


def test_off_balance_ccf_precedence():
    """Customer CCF: exposure row, then the segment hierarchy; else the regulatory fallback (CRR Art. 111(2))."""
    fb = ref.DEFAULT_CCF
    item = {"exposure_id": "CM-1", "exposure_type": "loan_commitment", "cancellable": False}
    seg = "LOANS|NFC_SME_OTHER|BE"
    assert ref.item_ccf(item, seg, fb, {}, {}) == (0.4, False)
    assert ref.item_ccf({**item, "cancellable": True}, seg, fb, {}, {}) == (0.1, False)
    assert ref.item_ccf({**item, "exposure_type": "other_commitment"}, seg, fb, {}, {}) == (0.5, False)
    assert ref.item_ccf({**item, "exposure_type": "financial_guarantee", "cancellable": True}, seg, fb, {}, {}) == (1.0, False)
    assert ref.item_ccf(item, seg, fb, {}, {"LOANS|ALL|ALL": 0.7}) == (0.7, True)
    assert ref.item_ccf(item, seg, fb, {}, {"LOANS|ALL|ALL": 0.7, "LOANS|NFC_SME_OTHER|ALL": 0.6}) == (0.6, True)
    assert ref.item_ccf(item, seg, fb, {"CM-1": 0.2}, {"LOANS|NFC_SME_OTHER|ALL": 0.6}) == (0.2, True)


def test_cr_scen_off_bs_layout(tmp_path):
    """22 rows per scenario and year; Sum rows add up the six sectors, Total the three commitment types."""
    rows = []
    for (seg, etype, v) in (("LOANS|HH_OTHER|BE", "loan_commitment", 1e6), ("LOANS|NFC_SME_OTHER|DE", "loan_commitment", 2e6),
                            ("LOANS|CI|OTHER", "financial_guarantee", 4e6)):
        for scen, year, _ in ref.OFF_BS_SLOTS:
            r = dict.fromkeys(ref.OFF_BALANCE_FIELDS, 0.0)
            r.update(segment=seg, exposure_type=etype, scenario=scen, year=year, nom_s1=v, exp_s1=v / 2, prov_stock_s1=v / 100)
            rows.append(r)
    ref.write_cr_scen_off_bs(tmp_path / "o.csv", rows, 2026)
    out = list(csv.DictReader(open(tmp_path / "o.csv")))
    assert len(out) == 7 * 22
    first = {r["RowNum"]: r for r in out[:22]}
    tot = "Total nominal amount before CCF (total NomAmount)"
    assert float(first["1"][tot]) == 3.0 and first["1"]["Asset classes"] == "Loan commitments given"
    assert float(first["7"][tot]) == 1.0 and first["7"]["Asset class 2"] == "Households"
    assert float(first["6"][tot]) == 2.0 and float(first["11"][tot]) == 4.0        # NFC loan commitments, CI guarantees
    assert float(first["22"][tot]) == 7.0 and float(first["22"]["Total nominal amount after CCF (total PostCCF)"]) == 3.5
    assert float(first["22"]["Stock of provisions (Prov Stock)"]) == 0.07
    assert [r["Year"] for r in out[::22]] == ["2026", "2027", "2028", "2029", "2027", "2028", "2029"]


def test_golden_off_balance_invariants():
    golden = REPO / "tests" / "golden" / "20260630"
    rows = list(csv.DictReader(open(golden / "off_balance.csv")))
    start = {(r["segment"], r["exposure_type"]): r for r in rows if r["scenario"] == "actual"}
    nom = ("nom_s1", "nom_s2", "nom_s3_old", "nom_s3_new", "nom_poci")
    post = ("exp_s1", "exp_s2", "exp_s3_old", "exp_s3_new", "exp_poci")
    for r in rows:
        s = start[(r["segment"], r["exposure_type"])]
        for cols in (nom, post):                                            # static balance sheet
            assert abs(sum(float(r[c]) for c in cols) - sum(float(s[c]) for c in cols)) < 0.05
        assert float(r["prov_old_s3"]) >= float(s["prov_stock_s3"]) - 0.005   # no S3 release
        assert float(r["exp_poci"]) == float(s["exp_poci"])                 # POCI static
        assert all(float(r[p]) <= float(r[n]) + 0.005 for p, n in zip(post, nom))   # CCF <= 1
    summary = json.loads((golden / "summary.json").read_text())["off_balance"]
    assert summary["items"] > 0 and summary["unmatched_items"] == 0


# ----------------------------------------------------------------------------------------- ECB benchmarks

BENCH_CFG = {"coverage_threshold": 0.10, "model_level": "portfolio", "sovereign": True, "country_fallback": ["EU"]}


def bench_group(base):
    return {(sc, t): {p: base for p in ref.BENCHMARK_GROUPS["pd_tr"]} for sc in ("baseline", "adverse") for t in (1, 2, 3)}


def bench_case(covered):
    """HH_HOUSE in BE (5), DE (45), FR (50) and GG in BE, DE (10 each); `covered`: segments whose PD/TR starting
    point is calibrated on the segment itself (LGD/LR always is)."""
    exp = {"LOANS|HH_HOUSE|BE": 5, "LOANS|HH_HOUSE|DE": 45, "LOANS|HH_HOUSE|FR": 50, "LOANS|GG|BE": 10, "LOANS|GG|DE": 10}
    segments = sorted(exp)
    stocks = {s: {"stage1": [v, 0.0], "stage2": [0.0, 0.0], "stage3": [0.0, 0.0], "poci": [0.0, 0.0]} for s, v in exp.items()}
    sources = {s: {"stage1": s if s in covered else "LOANS|ALL|ALL", "stage2": s, "stage3": s, "lgd": s, "lrlt": s}
               for s in segments}
    bench = {("LOANS", "HH_HOUSE", "BE"): {"pd_tr": bench_group(0.1)}, ("LOANS", "HH_HOUSE", "EU"): {"pd_tr": bench_group(0.2)},
             ("LOANS", "GG", "BE"): {"pd_tr": bench_group(0.3), "lgd_lr": {}}}
    return segments, sources, stocks, {"HH_HOUSE": {}, "GG": {}}, bench


def test_benchmark_rule_coverage_below_threshold():
    """MN 2027 para 117: satellite models for less than 10% of the pivot asset class -> benchmark for all of it."""
    segments, sources, stocks, sat, bench = bench_case({"LOANS|HH_HOUSE|BE", "LOANS|GG|BE", "LOANS|GG|DE"})
    dec, pivots = ref.benchmark_decisions(segments, sources, stocks, sat, bench, BENCH_CFG)
    assert [dec[s]["pd_tr"]["benchmark"] for s in ("LOANS|HH_HOUSE|BE", "LOANS|HH_HOUSE|DE", "LOANS|HH_HOUSE|FR")] == ["BE", "EU", "EU"]
    assert {dec[s]["pd_tr"]["rule"] for s in segments if "HH_HOUSE" in s} == {"coverage"}
    assert all(dec[s]["lgd_lr"]["rule"] == "none" for s in segments if "HH_HOUSE" in s)   # per group
    assert approx(pivots["LOANS|HH_HOUSE"]["model"]["pd_tr"], 5) and approx(pivots["LOANS|HH_HOUSE"]["benchmark"]["pd_tr"], 100)


def test_benchmark_rule_segments_without_model_and_sovereigns():
    """Para 115: above the threshold only the segments without a model; para 146: sovereigns of a country with a
    benchmark always take it (GG|DE has none: it keeps its model)."""
    segments, sources, stocks, sat, bench = bench_case({"LOANS|HH_HOUSE|BE", "LOANS|HH_HOUSE|DE", "LOANS|GG|BE", "LOANS|GG|DE"})
    dec, _ = ref.benchmark_decisions(segments, sources, stocks, sat, bench, BENCH_CFG)
    assert dec["LOANS|HH_HOUSE|FR"]["pd_tr"] == {"model": False, "rule": "no_model", "benchmark": "EU"}
    assert dec["LOANS|HH_HOUSE|DE"]["pd_tr"]["rule"] == "none"
    assert dec["LOANS|GG|BE"]["pd_tr"] == {"model": True, "rule": "sovereign", "benchmark": "BE"}
    assert dec["LOANS|GG|DE"]["pd_tr"]["rule"] == "none"
    dec, _ = ref.benchmark_decisions(segments, sources, stocks, sat, bench, {**BENCH_CFG, "sovereign": False})
    assert dec["LOANS|GG|BE"]["pd_tr"]["rule"] == "none"


def test_benchmark_rule_unavailable_and_model_level():
    segments, sources, stocks, sat, bench = bench_case(set())
    dec, _ = ref.benchmark_decisions(segments, sources, stocks, sat, bench, BENCH_CFG)
    assert dec["LOANS|GG|DE"]["pd_tr"] == {"model": False, "rule": "coverage", "benchmark": "unavailable"}
    for s in segments:                                          # portfolio-level calibration is a model ...
        sources[s]["stage1"] = "|".join(s.split("|")[:2]) + "|ALL"
    dec, _ = ref.benchmark_decisions(segments, sources, stocks, sat, bench, BENCH_CFG)
    assert dec["LOANS|HH_HOUSE|FR"]["pd_tr"]["rule"] == "none"
    dec, _ = ref.benchmark_decisions(segments, sources, stocks, sat, bench, {**BENCH_CFG, "model_level": "segment"})
    assert dec["LOANS|HH_HOUSE|FR"]["pd_tr"]["rule"] == "coverage"   # ... unless only segment-level counts
    del sat["HH_HOUSE"]                                        # no satellite model: no coverage
    dec, _ = ref.benchmark_decisions(segments, sources, stocks, sat, bench, BENCH_CFG)
    assert dec["LOANS|HH_HOUSE|FR"]["pd_tr"]["rule"] == "coverage"


def test_apply_benchmark_replaces_groups_without_adjustment():
    P = flat(pd12m_s1=0.01, lgd_s1=0.4, tr3_1=0.05)
    bench = {("LOANS", "CI", "DE"): {"pd_tr": bench_group(0.07)}}
    ref.apply_benchmark(P, {"pd_tr": {"benchmark": "DE"}, "lgd_lr": {"benchmark": ""}}, bench, "LOANS|CI|DE", "adverse")
    for t in (1, 2, 3, 4):
        assert P[t]["pd12m_s1"] == 0.07 and P[t]["tr2_1"] == 0.07
        assert P[t]["lgd_s1"] == 0.4 and P[t]["tr3_1"] == 0.05


def test_load_benchmarks(tmp_path):
    cfg = {"year_map": {1: 2027, 2: 2028, 3: 2029}}
    head = "# SYNTHETIC\ninstrument,portfolio,country,scenario,year,parameter,value\n"
    body = "".join(f"LOANS,CI,DE,{sc},{y},{p},0.01\n" for sc in ("baseline", "adverse") for y in (2026, 2027, 2028, 2029)
                   for p in ref.BENCHMARK_GROUPS["lgd_lr"])
    (tmp_path / "b.csv").write_text(head + body)
    b = ref.load_benchmarks(tmp_path / "b.csv", cfg)
    assert list(b) == [("LOANS", "CI", "DE")] and list(b[("LOANS", "CI", "DE")]) == ["lgd_lr"]
    assert sorted(b[("LOANS", "CI", "DE")]["lgd_lr"]) == [(sc, t) for sc in ("adverse", "baseline") for t in (1, 2, 3)]
    for bad in ("LOANS,CI,DE,baseline,2027,lgd_s1,0.1\n", "LOANS,CI,DE,baseline,2027,tr3_1,0.1\n",
                "LOANS,CI,DE,baseline,2027,lgd_s1,1.5\n", body + "LOANS,CI,DE,baseline,2027,lgd_s1,0.1\n"):
        (tmp_path / "x.csv").write_text(head + bad)
        with pytest.raises(ValueError):
            ref.load_benchmarks(tmp_path / "x.csv", cfg)


def test_synthetic_benchmark_file_is_generated(tmp_path):
    """tests/params/synthetic_ecb_benchmarks.csv is the deterministic output of its generator (SYNTHETIC values)."""
    import subprocess
    import sys
    subprocess.run([sys.executable, str(REPO / "tools" / "synthetic_ecb_benchmarks.py"), "-o", str(tmp_path / "b.csv")], check=True)
    committed = (REPO / "tests" / "params" / "synthetic_ecb_benchmarks.csv").read_text()
    assert (tmp_path / "b.csv").read_text() == committed
    assert committed.startswith("# SYNTHETIC")
    b = ref.load_benchmarks(REPO / "tests" / "params" / "synthetic_ecb_benchmarks.csv", {"year_map": {1: 2025, 2: 2026, 3: 2027}})
    assert ("LOANS", "CB", "EU") not in b and ("DEBT_SEC", "CI", "EU") not in b     # MN 2027 footnote 13
    assert all(set(g) == set(ref.BENCHMARK_GROUPS) for g in b.values())


def test_golden_benchmark_parameters_are_unadjusted():
    """Benchmarked segments carry the file's values for years 1..3 exactly (para 115), their own starting point, and
    source `benchmark`; the others keep `derived`."""
    golden = REPO / "tests" / "golden" / "20260630"
    cfg = {"year_map": {1: 2025, 2: 2026, 3: 2027}}
    bench = ref.load_benchmarks(REPO / "tests" / "params" / "synthetic_ecb_benchmarks.csv", cfg)
    dec = {r["segment"]: r for r in csv.DictReader(open(golden / "benchmarks.csv"))}
    n = 0
    for r in csv.DictReader(open(golden / "parameters.csv")):
        d = dec[r["key"]]
        if r["scenario"] == "actual":
            assert r["source"] == "derived"
            continue
        i, p, _ = r["key"].split("|")
        for g in ref.BENCHMARK_GROUPS:
            key = d[f"{g}_benchmark"]
            if key in ("", "unavailable"):
                continue
            n += 1
            for k in ref.BENCHMARK_GROUPS[g]:
                assert float(r[k]) == pytest.approx(bench[(i, p, key)][g][(r["scenario"], int(r["year"]))][k], abs=5e-10)
        applied = [d[f"{g}_benchmark"] not in ("", "unavailable") for g in ref.BENCHMARK_GROUPS]
        assert r["source"] == ("benchmark" if all(applied) else "mixed" if any(applied) else "derived")
    assert n > 0
    summary = json.loads((golden / "summary.json").read_text())["benchmark"]
    assert summary["pivots"]["LOANS|CI"]["pd_tr_model_coverage"] == 0.0
    assert summary["pivots"]["LOANS|CI"]["pd_tr_benchmark_share"] == 1.0


def facility(eid, etype, drawn, undrawn, allowance, stage="stage1", fx=1.0, cancellable=False):
    return {"exposure_id": eid, "exposure_type": etype, "drawn": drawn, "undrawn": undrawn, "gca": drawn * fx,
            "allowance": allowance * fx, "fx": fx, "stage": stage, "segment": "LOANS|HH_OTHER|BE",
            "cancellable": cancellable}


def test_split_facility_allowance():
    """The allowance of a facility is split pro rata between its drawn (on-balance) and undrawn (off-balance)
    parts when the undrawn part is projected off-balance; otherwise the loan keeps its whole allowance."""
    rows = [facility("L-1", "loan", 60.0, 40.0, 10.0), facility("L-2", "loan", 50.0, 0.0, 5.0),
            facility("C-1", "loan_commitment", 25.0, 75.0, 20.0, fx=0.5)]
    ref.split_facility_allowance(rows, {"off_balance": {"exposure_types": ["loan_commitment"]}})
    assert [r["allowance"] for r in rows] == [10.0, 5.0, 10.0]                # no split
    cfg = {"off_balance": {"exposure_types": ["loan_commitment"], "include_loan_undrawn": True,
                           "commitment_drawn_on_balance": True}}
    rows = [facility("L-1", "loan", 60.0, 40.0, 10.0), facility("L-2", "loan", 50.0, 0.0, 5.0),
            facility("C-1", "loan_commitment", 25.0, 75.0, 20.0, fx=0.5)]
    ref.split_facility_allowance(rows, cfg)
    assert approx(rows[0]["allowance"], 6.0) and rows[1]["allowance"] == 5.0 and approx(rows[2]["allowance"], 2.5)
    assert [r["allowance_total"] for r in rows] == [10.0, 5.0, 10.0]
    assert ref.drawn_on_balance_types(cfg) == ["loan_commitment"]
    assert ref.drawn_on_balance_types({"off_balance": {"exposure_types": ["loan_commitment"]}}) == []
    # The undrawn share goes with the off-balance item: on + off = the facility's allowance.
    items = ref.loan_undrawn_items(rows, cfg)
    assert [i["exposure_id"] for i in items] == ["L-1"]
    assert approx(items[0]["allowance"] + rows[0]["allowance"], 10.0)
    assert items[0]["nominal"] == 40.0 and items[0]["ccf_type"] == "loan_commitment"
    assert ref.loan_undrawn_items(rows, {"off_balance": {"exposure_types": []}}) == []


def test_loan_undrawn_ccf():
    """Undrawn credit facilities: CRR Annex I bucket 3 (loan commitment, 40%) whether revolving or not, bucket 5
    (10%) when unconditionally cancellable; a customer CCF of the loan or its segment takes precedence."""
    fb = ref.DEFAULT_CCF
    item = {**facility("L-1", "loan", 60.0, 40.0, 10.0), "exposure_type": "loan_commitment"}
    seg = "LOANS|HH_OTHER|BE"
    assert ref.item_ccf(item, seg, fb, {}, {}) == (0.4, False)
    assert ref.item_ccf({**item, "cancellable": True}, seg, fb, {}, {}) == (0.1, False)
    assert ref.item_ccf(item, seg, fb, {"L-1": 0.75}, {}) == (0.75, True)


def test_golden_facilities():
    """Golden run with include_loan_undrawn and commitment_drawn_on_balance: every facility's allowance is counted
    once, on-balance (drawn share) plus off-balance (undrawn share)."""
    golden = REPO / "tests" / "golden" / "20260630"
    summary = json.loads((golden / "summary.json").read_text())
    ob = summary["off_balance"]
    assert ob["loan_undrawn_items"] == 13619 and ob["commitment_drawn_exposures"] > 0
    rows = list(csv.DictReader(open(golden / "off_balance.csv")))
    assert {r["exposure_type"] for r in rows} == {"loan", "loan_commitment", "financial_guarantee", "other_commitment"}
    # Loan undrawn items stay in their loan's segment: every such group is an on-balance segment with loans.
    segments = {r["segment"] for r in csv.DictReader(open(golden / "segments.csv"))}
    assert {r["segment"] for r in rows if r["exposure_type"] == "loan"} <= segments
    # CR_SCEN_OFF_BS: loan commitments given include the undrawn part of loans.
    t0 = [r for r in rows if r["scenario"] == "actual"]
    nominal = lambda rs: sum(float(r[c]) for r in rs for c in ref.NOMINAL)  # noqa: E731
    cr = {r["RowNum"]: r for r in csv.DictReader(open(golden / "cr_scen_off_bs.csv")) if r["Scenario"] == "Actual"}
    lc = [r for r in t0 if r["exposure_type"] in ("loan", "loan_commitment")]
    assert abs(float(cr["1"]["Total nominal amount before CCF (total NomAmount)"]) * 1e6 - nominal(lc)) < 1
    # Provisions at t0, on- plus off-balance, equal the allowance of all staged amortised-cost exposures in scope.
    sp = summary["starting_point"]
    on = sum(sp[k] for k in ("prov_s1", "prov_s2", "prov_s3", "prov_poci"))
    off = sum(ob["totals"]["actual/0"][k] for k in ("prov_stock_s1", "prov_stock_s2", "prov_stock_s3", "prov_stock_poci"))
    assert abs(on + off - 231_249_324.98) < 1.0


# ----------------------------------------------------------------------------------------- sectoral (GVA) satellites

SECTOR_CFG = {"year_map": {1: 2025, 2: 2026, 3: 2027}, "country_fallback": ["EU"], "history_year": 2024,
              "normal_gdp_growth": 1.5, "calibration": {"pd_floor": 0.00001}}
SECTOR_SCFG = {"file": "x.csv", "gva_fallback": ["EU"]}


def sector_macro():
    macro = {}
    for y in (2025, 2026, 2027):
        for k in ("BE", "US", "EU"):
            macro[("real_gdp", k, "baseline", y)] = 1.0
            macro[("real_gdp", k, "adverse", y)] = -1.0 if k == "US" else -2.0
        for k in ("BE", "EU"):
            macro[("real_gva:F", k, "baseline", y)] = 1.5
            macro[("real_gva:F", k, "adverse", y)] = -5.0 if k == "BE" else -4.0
    return macro


def test_gva_sectors_by_division():
    """CR_SECTOR sectors (NACE Rev. 2.1) map to the Rev. 2 sections and aggregates of the ESRB GVA scenario by
    division; the same table as the engine's gva_sector()."""
    assert list(ref.GVA_SECTOR) == ["A", "B", "C_EI", "C_OT", *"DEFGHIJKLMNOPQRST"]
    assert ref.GVA_SECTOR["C_EI"] == "C_high" and ref.GVA_SECTOR["C_OT"] == "C_low"
    for code, sec in (("J62.01", "J"), ("J58.11", "J"), ("K64.20", "K"), ("L68.20", "L"), ("M69.10", "MN"),
                      ("N77.11", "MN"), ("Q86.10", "OPQ"), ("P85.10", "OPQ"), ("R93.11", "RSTU"), ("S96.02", "RSTU")):
        assert ref.GVA_SECTOR[ref.nace_sector(code)] == sec, code


def test_load_macro_keeps_real_gva_by_sector():
    macro = ref.load_macro(REPO / "scenarios" / "eba2025_macro.csv")
    assert ("real_gva:C_high", "DE", "adverse", 2025) in macro and ("real_gva:RSTU", "EU", "baseline", 2027) in macro
    assert not any(k[0] == "real_gva" for k in macro)                    # only by sector
    assert not any(k[0].startswith("real_gva:") and k[1] in ("US", "GB", "WR") for k in macro)   # EU 27, EA, EU only


def test_load_sector_satellites(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text("# SYNTHETIC\nsector,beta_gva,lgd_gva_sensitivity,description\nF,-0.15,0.8,x\nD,,0.3,LGD only\n")
    s = ref.load_sector_satellites(p)
    assert s == {"F": {"beta_gva": -0.15, "lgd_gva_sensitivity": 0.8}, "D": {"beta_gva": None, "lgd_gva_sensitivity": 0.3}}
    for body in ("C,-0.1,0.2,x\n", "F,-0.1,,x\nF,-0.2,,y\n", "F,,,x\n"):
        p.write_text("sector,beta_gva,lgd_gva_sensitivity,description\n" + body)
        with pytest.raises(ValueError):
            ref.load_sector_satellites(p)
    synthetic = REPO / "tests" / "params" / "synthetic_sector_satellites.csv"
    s = ref.load_sector_satellites(synthetic)
    assert "SYNTHETIC" in synthetic.read_text().splitlines()[0]
    assert "L" not in s and "P" not in s and s["D"]["beta_gva"] is None


def test_sector_satellite_parameters():
    """GVA growth replaces GDP growth in the PD/TR index; cumulative GVA decline raises LGD/LR. Same numbers as the
    engine's unit test (tests/cpp/test_sector_satellites.cpp)."""
    import math
    macro = sector_macro()
    coef = {"F": {"beta_gva": -0.2, "lgd_gva_sensitivity": 0.5}, "C_EI": {"beta_gva": -0.1, "lgd_gva_sensitivity": None}}
    sat = {"NFC_SME_OTHER": {"beta_gdp": -0.12, "beta_unemployment": 0.0, "beta_property": 0.0, "lgd_property_sensitivity": 0.0}}
    p0 = {"pd12m_s1": 0.02, "pd12m_s2": 0.10, "tr1_2": 0.05, "tr2_1": 0.20, "tr3_1": 0.01, "tr3_2": 0.02,
          "lgd_s1": 0.4, "lgd_s2": 0.4, "lgd_s3": 0.4, "lrlt_s2": 0.08}
    lg = lambda p: math.log(p / (1 - p))  # noqa: E731
    ex = lambda x: 1 / (1 + math.exp(-x))  # noqa: E731
    f = ref.sector_model("LOANS|NFC_SME_OTHER|BE", "F", coef, macro, SECTOR_CFG, SECTOR_SCFG)
    assert (f["gva_key"], f["relative"]) == ("BE", False)
    P = ref.project_parameters("LOANS|NFC_SME_OTHER|BE", p0, sat, macro, "adverse", SECTOR_CFG, sector=f)
    assert approx(P[1]["pd12m_s1"], ex(lg(0.02) + 1.3)) and approx(P[1]["tr2_1"], ex(lg(0.20) - 1.3))
    assert approx(P[1]["lgd_s1"], 0.4 * 1.025) and approx(P[3]["lrlt_s2"], 0.08 * (1 + 0.5 * (1 - 0.95 ** 3)))
    assert P[1]["tr3_1"] == 0.01
    base = ref.project_parameters("LOANS|NFC_SME_OTHER|BE", p0, sat, macro, "adverse", SECTOR_CFG)
    assert approx(base[1]["pd12m_s1"], ex(lg(0.02) - 0.12 * (-2 - 1.5))) and base[3]["lgd_s1"] == 0.4
    us = ref.sector_model("LOANS|NFC_SME_OTHER|US", "F", coef, macro, SECTOR_CFG, SECTOR_SCFG)
    assert (us["gva_key"], us["relative"]) == ("EU", True)
    assert ref.sector_growth(us, macro, "adverse", 2026) == -3.0         # US GDP + (EU GVA - EU GDP)
    with pytest.raises(KeyError):
        ref.sector_model("LOANS|NFC_SME_OTHER|US", "F", coef, macro, SECTOR_CFG, {"gva_fallback": ["EA"]})


def test_project_parts_add_up_and_sector_coverage():
    """A segment with parts on different parameter paths is the sum of the parts; one part is the segment."""
    P = flat(pd12m_s1=0.02, pd12m_s2=0.10, tr1_2=0.05, tr2_1=0.20, lgd_s1=0.40, lgd_s2=0.50, lgd_s3=0.60, lrlt_s2=0.08)
    Q = flat(pd12m_s1=0.04, pd12m_s2=0.20, tr1_2=0.05, tr2_1=0.10, lgd_s1=0.50, lgd_s2=0.50, lgd_s3=0.60, lrlt_s2=0.10)
    a, b = stock(1000, 200, 100, prov=(3, 10, 50, 0)), stock(500, 0, 100, prov=(1, 0, 20, 0))
    both = ref.project_parts([(a, {"baseline": P}, [(100, 50)]), (b, {"baseline": Q}, [(100, 20)])], CFG)
    ra = ref.project_segment(a, {"baseline": P}, CFG, [(100, 50)])
    rb = ref.project_segment(b, {"baseline": Q}, CFG, [(100, 20)])
    for r, x, y in zip(both, ra, rb):
        assert all(approx(r[k], x[k] + y[k]) for k in r if k not in ("scenario", "year"))
    assert ref.project_parts([(a, {"baseline": P}, [(100, 50)])], CFG) == ra
    coef = {"F": {"beta_gva": -0.2, "lgd_gva_sensitivity": None}}
    ex = [{"segment": "LOANS|NFC_SME_OTHER|BE", "portfolio": "NFC_SME_OTHER", "nace_code": "F41.20"},
          {"segment": "LOANS|NFC_SME_OTHER|DE", "portfolio": "NFC_SME_OTHER", "nace_code": "F41.20"},
          {"segment": "LOANS|NFC_SME_OTHER|DE", "portfolio": "NFC_SME_OTHER", "nace_code": "G47.11"},
          {"segment": "LOANS|HH_CONS|BE", "portfolio": "HH_CONS", "nace_code": "F41.20"}]
    cov = ref.sector_coverage(ex, coef)
    assert cov["LOANS|NFC_SME_OTHER|BE"] == {"pd_tr": True, "lgd_lr": False}
    assert cov["LOANS|NFC_SME_OTHER|DE"] == {"pd_tr": False, "lgd_lr": False}
    assert cov["LOANS|HH_CONS|BE"] == {"pd_tr": False, "lgd_lr": False}     # sectoral satellites are for NFCs only
    assert ref.sector_key(ex[0], coef) == "F" and ref.sector_key(ex[2], coef) is None and ref.sector_key(ex[3], coef) is None


def test_golden_sector_satellites():
    """Golden results of the sectoral satellites: CR_SECTOR columns 1-2 are 0 for Actual and 0 or 100 per sector (the
    benchmark rule does not touch the NFC portfolios of the reference data); the TOTAL row is the summary's share."""
    golden = REPO / "tests" / "golden" / "20260630"
    summary = json.loads((golden / "summary.json").read_text())["sector_satellites"]
    assert summary["sectors_pd_tr"] == 18 and summary["sectors_lgd_lr"] == 10
    cols = [ref.CR_SECTOR_COLUMNS[i][0] for i in (0, 1)]
    for r in csv.DictReader(open(golden / "cr_sector.csv")):
        v = [float(r[c]) for c in cols]
        exposure = float(r["Total exposure (total Exp)"])
        if r["Scenario"] == "Actual":
            assert v == [0.0, 0.0]
            continue
        if r["RowNum"] != "23" and exposure > 0:
            assert v[0] in (0.0, 100.0) and v[1] in (0.0, 100.0)
        if r["RowNum"] == "23" and r["Geographical breakdown"] == "Total":
            assert abs(v[0] - 100 * summary["pd_tr_share"]) < 1e-6 and abs(v[1] - 100 * summary["lgd_lr_share"]) < 1e-6
        if r["RowNum"] == "6" and exposure > 0:
            assert v == [0.0, 100.0]                      # D: LGD/LR only
        if r["RowNum"] == "7" and exposure > 0:
            assert v == [100.0, 0.0]                      # E: PD/TR only
    uses = {(r["sector"], r["pd_tr"], r["lgd_lr"]) for r in csv.DictReader(open(golden / "sector_parameters.csv"))}
    assert ("D", "portfolio", "sectoral") in uses and ("E", "sectoral", "portfolio") in uses
