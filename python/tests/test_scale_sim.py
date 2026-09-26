"""tools/scale_sim.py: replicated benchmark datasets keep keys unique and references intact."""

import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from sora_tools.validate import validate

REPO = Path(__file__).resolve().parents[2]
ENGINE = Path(os.environ.get("SORA_ENGINE", REPO / "build" / "release" / "sora"))


@pytest.fixture(scope="module")
def scaled(reference_sim, tmp_path_factory):
    out = tmp_path_factory.mktemp("scaled") / "sim"
    subprocess.run([sys.executable, str(REPO / "tools" / "scale_sim.py"), str(reference_sim), str(out), "--factor", "3"],
                   check=True, capture_output=True)
    return out


def count(sim: Path, table: str, where: str = "") -> int:
    return duckdb.sql(f"SELECT count(*) FROM read_parquet('{sim}/{table}/**/*.parquet') {where}").fetchone()[0]


def test_row_counts_and_manifest(reference_sim, scaled):
    for table in ("sim_exposure", "sim_counterparty", "sim_stage_history", "sim_collateral", "sim_collateral_allocation",
                  "sim_guarantee", "sim_rating", "sim_credit_event", "sim_recovery_flow", "sim_deposit", "sim_debt_issued"):
        assert count(scaled, table) == 3 * count(reference_sim, table), table
    for table in ("sim_entity", "sim_fx_rate", "sim_rate_curve"):
        assert count(scaled, table) == count(reference_sim, table), table
    manifest = json.loads((scaled / "sim_manifest.json").read_text())
    assert manifest["scale"]["factor"] == 3
    assert manifest["reference_date"] == json.loads((reference_sim / "sim_manifest.json").read_text())["reference_date"]
    # Partitioned like the source.
    assert (scaled / "sim_exposure").glob("entity_id=*/*.parquet")
    assert {p.name for p in (scaled / "sim_exposure").iterdir()} == {p.name for p in (reference_sim / "sim_exposure").iterdir()}


def test_keys_are_suffixed_and_replica_zero_is_unchanged(reference_sim, scaled):
    orig = {r[0] for r in duckdb.sql(f"SELECT exposure_id FROM read_parquet('{reference_sim}/sim_exposure/**/*.parquet')").fetchall()}
    new = [r[0] for r in duckdb.sql(f"SELECT exposure_id FROM read_parquet('{scaled}/sim_exposure/**/*.parquet')").fetchall()]
    assert len(new) == len(set(new))
    assert orig <= set(new)
    assert {e for e in new if e not in orig} == {f"{e}~{r}" for e in orig for r in (1, 2)}


def test_referential_integrity(scaled):
    report = validate(scaled, modules=["core", "credit", "nii"], samples=0)
    assert report.ok, report.to_text()
    src = lambda t: f"read_parquet('{scaled}/{t}/**/*.parquet')"  # noqa: E731
    orphans = duckdb.sql(f"""SELECT count(*) FROM {src('sim_stage_history')} h
                             WHERE h.exposure_id NOT IN (SELECT exposure_id FROM {src('sim_exposure')})
                               AND h.exposure_id NOT LIKE '%~%'""").fetchone()[0]
    replica_orphans = duckdb.sql(f"""SELECT count(*) FROM {src('sim_stage_history')} h
                                     WHERE h.exposure_id NOT IN (SELECT exposure_id FROM {src('sim_exposure')})""").fetchone()[0]
    assert replica_orphans == 3 * orphans   # history of exposures missing from sim_exposure stays missing, per replica


@pytest.mark.skipif(not ENGINE.exists(), reason="C++ engine not built")
def test_engine_on_scaled_dataset(reference_sim, scaled, tmp_path):
    scenario = REPO / "tests" / "scenarios" / "test_eba2025.yaml"
    for sim, out in ((reference_sim, tmp_path / "x1"), (scaled, tmp_path / "x3")):
        subprocess.run([str(ENGINE), "run", str(sim), "--scenario", str(scenario), "-o", str(out), "--base", str(REPO)],
                       check=True, capture_output=True)
    one = json.loads((tmp_path / "x1" / "summary.json").read_text())
    three = json.loads((tmp_path / "x3" / "summary.json").read_text())
    assert three["exposures"] == 3 * one["exposures"]
    # Replicas scale the stocks by 3. Calibrated parameters stay the same where the calibration used the same
    # hierarchy levels (with 3x the observations, more segments reach min_observations at their own level).
    for k, v in one["starting_point"].items():
        assert three["starting_point"][k] == pytest.approx(3 * v, rel=1e-12, abs=0.02)
    p1 = {(r["key"], r["scenario"], r["year"]): r for r in csv.DictReader(open(tmp_path / "x1" / "parameters.csv"))}
    p3 = {(r["key"], r["scenario"], r["year"]): r for r in csv.DictReader(open(tmp_path / "x3" / "parameters.csv"))}
    assert p1.keys() == p3.keys()
    same = [k for k in p1 if k[1] == "actual" and p1[k]["calibration_levels"] == p3[k]["calibration_levels"]]
    assert len(same) > len(p1) // 14
    for k in same:
        for c in ("pd12m_s1", "pd12m_s2", "tr1_2", "tr2_1", "lgd_s3", "lrlt_s2"):
            assert float(p3[k][c]) == pytest.approx(float(p1[k][c]), abs=2e-9), (k, c)
