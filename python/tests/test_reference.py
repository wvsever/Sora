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
                 "off_balance.csv", "cr_scen_off_bs.csv", "benchmarks.csv"):
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
                assert abs(float(r[f"ltv_{st}"]) - float(r[f"secured_exp_{st}"]) / float(r[f"re_collateral_{st}"])) < 1e-6
            else:
                assert float(r[f"re_collateral_{st}"]) == 0
    for r in csv.DictReader(open(golden / "parameters.csv")):
        assert float(r["pd12m_s1"]) + float(r["tr1_2"]) <= 1 + 1e-9
        assert float(r["pd12m_s2"]) + float(r["tr2_1"]) <= 1 + 1e-9


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
