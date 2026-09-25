import csv
from pathlib import Path

import pytest

pytest.importorskip("openpyxl")

from sora_tools.scenario_import import classify, import_scenarios  # noqa: E402

DOCS = Path(__file__).resolve().parents[2] / "docs" / "EBA 2025 EU-wide Stress Test"
MACRO = DOCS / "2025 EU-wide stress test - Macro financial scenario (updated 26 February 2025)-incl disclaimer for EBA (5).xlsx"
GVA = DOCS / "2025 EU-wide stress test - Real GVA by sector.xlsx"


@pytest.mark.parametrize("label, expected", [
    ("Baseline growth (%)", ("baseline", "growth", "pct")),
    ("Adverse rate (%)", ("adverse", "level", "pct")),
    ("Historical value (%)", ("historical", "level", "pct")),
    ("Starting point rates (%) – latest", ("starting_point", "level", "pct")),
    ("Starting point rates (%) – average", ("starting_point_average", "level", "pct")),
    ("Deviation from the starting point (%)", ("adverse", "deviation_from_start", "pct")),
    ("Cumulative growth from the starting point (%)", None),
    ("Level of deviation in 2027 (%)", None),
])
def test_classify(label, expected):
    g = classify(label)
    assert (g and (g.scenario, g.measure, g.unit)) == expected or (g is None and expected is None)


def test_import_eba_2025(tmp_path):
    out = tmp_path / "macro.csv"
    n = import_scenarios([MACRO, GVA], out)
    rows = list(csv.DictReader(out.open()))
    assert n == len(rows) > 5000
    idx = {(r["variable"], r["key"], r["tenor"], r["sector"], r["scenario"], r["year"]): r["value"] for r in rows}
    # Spot checks against the published tables.
    assert idx[("real_gdp", "BE", "", "", "adverse", "2026")] == "-3.674906899"
    assert idx[("residential_property_prices", "DE", "", "", "adverse", "2025")] == "-3.153867055"
    assert idx[("swap_rate", "EUR", "10Y", "", "baseline", "2027")] == "2.420874750"
    assert idx[("real_gva", "BE", "", "C_low", "adverse", "2025")] == "-1.136577634"


def test_committed_scenario_is_current(tmp_path, repo):
    """scenarios/eba2025_macro.csv is generated from docs/. Regenerate with `sora-tools scenario-import`."""
    out = tmp_path / "macro.csv"
    import_scenarios([MACRO, GVA], out)
    assert out.read_text() == (repo / "scenarios" / "eba2025_macro.csv").read_text()
