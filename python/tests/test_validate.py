"""Validator behaviour on small hand-made SIM datasets."""

import json
from pathlib import Path

import pytest

from sora_tools.validate import validate

MANIFEST = {"sim_version": "1.0.0-draft", "reference_date": "2026-06-30",
            "reporting_currency": "EUR", "reporting_entity_id": "E1"}

ENTITY = "entity_id,country,functional_currency\nE1,BE,EUR\n"
COUNTERPARTY = ("counterparty_id,country_of_residence,esa2010_sector,eba_sector,nace_code,is_natural_person,is_sme\n"
                "C1,BE,S.11,non_financial_corporation,C10,false,true\n"
                "C2,DE,S.14,household,,true,\n")
FX = "currency,rate_date,rate_to_reporting\nEUR,2026-06-30,1\nUSD,2026-06-30,0.91\n"
EXP_HEADER = ("exposure_id,entity_id,counterparty_id,exposure_type,product_code,currency,measurement_category,stage,"
              "gross_carrying_amount,off_balance_amount,loss_allowance,household_purpose,is_cre\n")
GOOD_EXPOSURES = ("L1,E1,C1,loan,CORP,EUR,amortised_cost,stage1,1000.00,,1.50,,false\n"
                  "L2,E1,C2,loan,MTG,USD,amortised_cost,stage2,500.00,,5.00,house_purchase,\n"
                  "K1,E1,C1,loan_commitment,RCF,EUR,amortised_cost,stage1,,200.00,0.10,,\n")


def write_sim(root: Path, exposures: str = GOOD_EXPOSURES, manifest=None, extra: dict | None = None) -> Path:
    files = {"sim_entity": ENTITY, "sim_counterparty": COUNTERPARTY, "sim_fx_rate": FX,
             "sim_exposure": EXP_HEADER + exposures, **(extra or {})}
    for table, content in files.items():
        (root / table).mkdir(parents=True, exist_ok=True)
        (root / table / "part-0.csv").write_text(content)
    (root / "sim_manifest.json").write_text(json.dumps(manifest or MANIFEST))
    return root


def ids(report, severity="error"):
    return {f.check_id for f in report.findings if f.severity == severity}


def test_good_dataset_passes(tmp_path):
    report = validate(write_sim(tmp_path), modules=["core"])
    assert report.ok, report.to_text()
    assert report.row_counts["sim_exposure"] == 3


@pytest.mark.parametrize("row, check_id", [
    ("L1,E1,C1,loan,CORP,EUR,amortised_cost,stage1,1000.00,,1.50,,false\n", "KEY-PK"),        # duplicate id
    ("L9,E1,C404,loan,CORP,EUR,amortised_cost,stage1,1.00,,0,,false\n", "KEY-FK"),            # unknown counterparty
    ("L9,E1,C1,loan,CORP,EUR,amortised_cost,stage9,1.00,,0,,false\n", "COL-ENUM"),            # bad stage
    ("L9,E1,C1,loan,CORP,eur,amortised_cost,stage1,1.00,,0,,false\n", "COL-PATTERN"),         # bad currency
    ("L9,E1,C1,loan,CORP,EUR,amortised_cost,stage1,abc,,0,,false\n", "STR-003"),              # not a number
    ("L9,E1,C1,loan,CORP,EUR,amortised_cost,stage1,,,0,,false\n", "EXP-001"),                 # on-balance without GCA
    ("L9,E1,C1,loan,CORP,EUR,held_for_trading,stage1,1.00,,,,false\n", "EXP-003"),            # FV with a stage
    ("L9,E1,C1,loan,CORP,EUR,amortised_cost,stage1,-1.00,,0,,false\n", "EXP-004"),            # negative amount
    ("L9,E1,C2,loan,CORP,EUR,amortised_cost,stage1,1.00,,0,,\n", "EXP-101"),                  # household w/o purpose
    ("L9,E1,C1,loan,CORP,JPY,amortised_cost,stage1,1.00,,0,,false\n", "DS-FX"),               # no FX rate
])
def test_errors_are_detected(tmp_path, row, check_id):
    report = validate(write_sim(tmp_path, GOOD_EXPOSURES + row), modules=["core"])
    assert check_id in ids(report), report.to_text()
    assert not report.ok


def test_missing_required_table(tmp_path):
    write_sim(tmp_path)
    report = validate(tmp_path, modules=["core", "credit"])
    missing = {f.table for f in report.findings if f.check_id == "STR-001"}
    assert {"sim_rating", "sim_collateral", "sim_collateral_allocation", "sim_guarantee"} <= missing


def test_manifest_problems(tmp_path):
    report = validate(write_sim(tmp_path, manifest={**MANIFEST, "sim_version": "0.9"}), modules=["core"])
    assert "MAN-004" in ids(report)
    (tmp_path / "sim_manifest.json").unlink()
    assert "MAN-001" in ids(validate(tmp_path, modules=["core"]))


def test_warning_does_not_fail(tmp_path):
    row = "L9,E1,C1,loan,CORP,EUR,amortised_cost,stage1,1.00,,5.00,,false\n"   # allowance > exposure
    report = validate(write_sim(tmp_path, GOOD_EXPOSURES + row), modules=["core"])
    assert "EXP-007" in ids(report, "warning")
    assert report.ok
