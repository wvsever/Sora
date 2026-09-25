"""Unit tests for sora_tools.vera_params on a small hand-made Vera export fixture (no DuckDB, no book,
no bcal_cli run - see _LANE_CONTRACT.md: this lane writes code only, the integrator runs the real pipeline)."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from sora_tools.vera_params import SIM_COLUMNS, VERA_COLUMNS, VeraParamsError, convert, convert_row, ConversionStats


def _write_vera_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=VERA_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in VERA_COLUMNS})


def _base_row(**overrides) -> dict[str, str]:
    row = {c: "" for c in VERA_COLUMNS}
    row.update({
        "entity_id": "BANK-CPP-001",
        "contract_id": "CL-000001",
        "exposure_id": "BANK-CPP-001/CL-000001",
        "counterparty_id": "CPTY-000001",
        "instrument_kind": "loan",
        "declared_stage": "stage1",
        "is_defaulted": "false",
    })
    row.update(overrides)
    return row


def _read_out(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_output_header_matches_sim_schema(tmp_path):
    src = tmp_path / "risk_parameters.csv"
    _write_vera_csv(src, [_base_row(pd12m_pit="0.01")])
    out = tmp_path / "sim_risk_parameter.csv"
    convert(src, out)
    with out.open(newline="", encoding="utf-8") as fh:
        assert next(csv.reader(fh)) == SIM_COLUMNS


def test_stage1_maps_pd12m_s1_and_lgd_s1(tmp_path):
    src = tmp_path / "risk_parameters.csv"
    _write_vera_csv(src, [_base_row(declared_stage="stage1", pd12m_pit="0.004000000", lgd_ifrs9="0.22",
                                     ccf="0.4", pd_reg="0.005", lgd_reg="0.25")])
    out = tmp_path / "sim_risk_parameter.csv"
    stats = convert(src, out)
    rows = _read_out(out)
    assert len(rows) == 1
    r = rows[0]
    assert r["level"] == "exposure" and r["key"] == "CL-000001" and r["scenario"] == "actual" and r["year"] == "0"
    assert r["source"] == "external"
    assert r["pd12m_s1"] == "0.004000000"
    assert r["lgd_s1"] == "0.220000000"
    assert r["pd12m_s2"] == "" and r["lgd_s2"] == "" and r["lgd_s3"] == "" and r["lrlt_s2"] == ""
    # tr* are never sourced from Vera today.
    assert r["tr1_2"] == "" and r["tr2_1"] == "" and r["tr3_1"] == "" and r["tr3_2"] == ""
    # pass-through fields, same name both sides.
    assert r["ccf"] == "0.400000000" and r["pd_reg"] == "0.005000000" and r["lgd_reg"] == "0.250000000"
    assert stats.rows_in == 1 and stats.rows_out == 1
    assert stats.by_stage == {"stage1": 1}


def test_stage2_maps_pd12m_s2_lgd_s2_lrlt_s2(tmp_path):
    src = tmp_path / "risk_parameters.csv"
    _write_vera_csv(src, [_base_row(declared_stage="stage2", pd12m_pit="0.09", lgd_ifrs9="0.24", lrlt="0.09")])
    out = tmp_path / "sim_risk_parameter.csv"
    convert(src, out)
    r = _read_out(out)[0]
    assert r["pd12m_s2"] == "0.090000000"
    assert r["lgd_s2"] == "0.240000000"
    assert r["lrlt_s2"] == "0.090000000"
    assert r["pd12m_s1"] == "" and r["lgd_s1"] == "" and r["lgd_s3"] == ""


def test_stage3_maps_only_lgd_s3(tmp_path):
    src = tmp_path / "risk_parameters.csv"
    _write_vera_csv(src, [_base_row(declared_stage="stage3", lgd_s3="0.35", pd12m_pit="0.9")])
    out = tmp_path / "sim_risk_parameter.csv"
    convert(src, out)
    r = _read_out(out)[0]
    assert r["lgd_s3"] == "0.350000000"
    # pd12m_pit is not used for stage3 - only lgd_s3 is a starting-point parameter for the existing stock.
    assert r["pd12m_s1"] == "" and r["pd12m_s2"] == ""


def test_poci_defaulted_treated_like_stage3(tmp_path):
    src = tmp_path / "risk_parameters.csv"
    _write_vera_csv(src, [_base_row(declared_stage="poci", is_defaulted="true", lgd_s3="0.4")])
    out = tmp_path / "sim_risk_parameter.csv"
    stats = convert(src, out)
    r = _read_out(out)[0]
    assert r["lgd_s3"] == "0.400000000"
    assert stats.by_stage == {"stage3_or_poci_defaulted": 1}


def test_poci_not_defaulted_has_no_stage_bucket_value_and_is_dropped_if_nothing_else(tmp_path):
    src = tmp_path / "risk_parameters.csv"
    _write_vera_csv(src, [_base_row(declared_stage="poci", is_defaulted="false")])
    out = tmp_path / "sim_risk_parameter.csv"
    stats = convert(src, out)
    assert _read_out(out) == []
    assert stats.rows_dropped_all_empty == 1
    assert stats.empty_reasons["POCI_NOT_DEFAULTED_UNSTAGED"] == 1


def test_row_with_no_contract_id_is_dropped(tmp_path):
    src = tmp_path / "risk_parameters.csv"
    _write_vera_csv(src, [_base_row(contract_id="", pd12m_pit="0.01")])
    out = tmp_path / "sim_risk_parameter.csv"
    stats = convert(src, out)
    assert _read_out(out) == []
    assert stats.rows_dropped_no_contract_id == 1
    assert stats.rows_in == 1 and stats.rows_out == 0


def test_row_with_every_parameter_empty_is_dropped(tmp_path):
    src = tmp_path / "risk_parameters.csv"
    _write_vera_csv(src, [_base_row(declared_stage="stage1")])   # no pd12m_pit, no lgd_ifrs9, no passthroughs
    out = tmp_path / "sim_risk_parameter.csv"
    stats = convert(src, out)
    assert _read_out(out) == []
    assert stats.rows_dropped_all_empty == 1
    # still counted by stage even though ultimately dropped - the report must not hide these exposures.
    assert stats.by_stage == {"stage1": 1}


def test_par010_range_violation_is_dropped_to_empty_not_clamped(tmp_path):
    src = tmp_path / "risk_parameters.csv"
    _write_vera_csv(src, [_base_row(declared_stage="stage1", pd12m_pit="0.01", lgd_ifrs9="1.5")])
    out = tmp_path / "sim_risk_parameter.csv"
    stats = convert(src, out)
    r = _read_out(out)[0]
    assert r["lgd_s1"] == ""              # dropped, never clamped to 1.0
    assert r["pd12m_s1"] == "0.010000000"  # the other field on the same row is unaffected
    assert stats.range_violations["lgd_s1"] == 1
    assert any("lgd_s1=1.5" in s for s in stats.range_violation_samples)


def test_strict_mode_flags_range_violations_via_has_range_violations(tmp_path):
    src = tmp_path / "risk_parameters.csv"
    _write_vera_csv(src, [_base_row(declared_stage="stage1", pd12m_pit="0.01", lgd_ifrs9="-0.1")])
    out = tmp_path / "sim_risk_parameter.csv"
    stats = convert(src, out)
    assert stats.has_range_violations is True
    clean = ConversionStats()
    assert clean.has_range_violations is False


def test_malformed_number_is_treated_as_absent_never_as_zero(tmp_path):
    src = tmp_path / "risk_parameters.csv"
    _write_vera_csv(src, [_base_row(declared_stage="stage1", pd12m_pit="not-a-number", ccf="0.4")])
    out = tmp_path / "sim_risk_parameter.csv"
    stats = convert(src, out)
    r = _read_out(out)[0]
    assert r["pd12m_s1"] == ""             # never "0.000000000"
    assert r["ccf"] == "0.400000000"       # the rest of the row is unaffected
    assert stats.empty_reasons["MALFORMED:pd12m_pit"] == 1


def test_extras_side_file_carries_default_state(tmp_path):
    src = tmp_path / "risk_parameters.csv"
    _write_vera_csv(src, [_base_row(is_defaulted="true", default_date="2026-03-15", default_trigger="90dpd",
                                     dpd="97", ead="125000.00", pd12m_pit="0.9")])
    out = tmp_path / "sim_risk_parameter.csv"
    extras = tmp_path / "extras.csv"
    convert(src, out, extras_csv=extras)
    with extras.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["is_defaulted"] == "true"
    assert rows[0]["default_date"] == "2026-03-15"
    assert rows[0]["dpd"] == "97"
    assert rows[0]["ead"] == "125000.00"


def test_missing_required_columns_raises_structural_error(tmp_path):
    src = tmp_path / "bad.csv"
    src.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(VeraParamsError):
        convert(src, tmp_path / "out.csv")


def test_convert_row_is_deterministic_given_same_input():
    stats_a, stats_b = ConversionStats(), ConversionStats()
    row = _base_row(declared_stage="stage2", pd12m_pit="0.09", lgd_ifrs9="0.24", lrlt="0.09")
    a = convert_row(row, stats_a, "actual", 0)
    b = convert_row(dict(row), stats_b, "actual", 0)
    assert a == b
