"""The engine's IRB REA projection through the regulatory calculator, against the in-process stub
(skipped if the engine is not built). Registered with ctest as `sora_calculator`.

Each run sends ~310k IRB records (44k exposures x 7 scenario points), so these tests take a minute.
"""

import csv
import json
import os
import subprocess
from pathlib import Path

import pytest

from sora_tools.calculator_stub import start_background

REPO = Path(__file__).resolve().parents[2]
ENGINE = Path(os.environ.get("SORA_ENGINE", REPO / "build" / "release" / "sora"))
SCENARIO = REPO / "tests" / "scenarios" / "test_eba2025.yaml"
POINTS = {("actual", "0"), *((s, str(y)) for s in ("baseline", "adverse") for y in (1, 2, 3))}

pytestmark = pytest.mark.skipif(not ENGINE.exists(), reason="C++ engine not built")


def run(sim, out, *extra, check=True):
    r = subprocess.run([str(ENGINE), "run", str(sim), "--scenario", str(SCENARIO), "-o", str(out), "--base", str(REPO), *extra],
                       capture_output=True, text=True)
    if check:
        assert r.returncode == 0, r.stderr
    return r


def url(server):
    return f"http://127.0.0.1:{server.server_address[1]}"


def stop(server):
    server.shutdown()
    server.server_close()


def rows(out):
    with open(out / "rea.csv") as f:
        return list(csv.DictReader(f))


def findings(out):
    return {f["id"]: f for f in json.loads((out / "diagnostics.json").read_text())["findings"]}


@pytest.fixture(scope="module")
def formula_run(reference_sim, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("calc_formula")
    server = start_background("formula")
    try:
        run(reference_sim, tmp / "out", "--calculator", url(server), "--calculator-cache", str(tmp / "cache"))
        keys = [k for p, k in server.stub.calls if p == "/v1/credit-risk/irb"]
    finally:
        stop(server)
    return tmp, keys, url(server)


def test_formula_mode_rea(reference_sim, formula_run, tmp_path):
    tmp, keys, _ = formula_run
    out = tmp / "out"
    r = rows(out)
    assert {(x["scenario"], x["year"]) for x in r} == POINTS
    assert sum(float(x["rea"]) for x in r) > 0
    assert all(x["records_rejected"] == "0" for x in r)
    assert len(keys) == len(set(keys))                     # one request per batch, distinct idempotency keys
    summary = json.loads((out / "summary.json").read_text())["rea"]
    assert summary["calculator"] == "sora-calculator-stub"
    adverse, actual = summary["totals"]["adverse/3"], summary["totals"]["actual/0"]
    assert adverse["ead"] == actual["ead"]                 # static balance sheet
    assert adverse["rea"] > actual["rea"]                  # stressed PDs raise REA
    f = findings(out)
    assert {"CALC-000", "CALC-002"} <= f.keys() and "CALC-010" not in f
    # The calculator does not change the IFRS 9 projection.
    run(reference_sim, tmp_path / "plain")
    assert (tmp_path / "plain" / "projection.csv").read_bytes() == (out / "projection.csv").read_bytes()
    assert not (tmp_path / "plain" / "rea.csv").exists()


def test_rerun_replays_from_cache_without_calculator(reference_sim, formula_run, tmp_path):
    tmp, _, calculator = formula_run                       # the stub is stopped by now
    r = run(reference_sim, tmp_path / "out", "--calculator", calculator, "--calculator-cache", str(tmp / "cache"))
    assert "CALC-001" in r.stderr
    assert (tmp_path / "out" / "rea.csv").read_bytes() == (tmp / "out" / "rea.csv").read_bytes()


def test_unreachable_calculator_without_cache_fails(reference_sim, tmp_path):
    r = run(reference_sim, tmp_path / "out", "--calculator", "http://127.0.0.1:9", check=False)
    assert r.returncode == 1 and "capabilities" in r.stderr
    assert not (tmp_path / "out" / "summary.json").exists()


def test_fixed_mode_rea_equals_ead_through_jobs(reference_sim, tmp_path):
    server = start_background("fixed")
    try:
        # Batches of 8000 records exceed the stub's sync limit (5000), so they go through /v1/jobs.
        run(reference_sim, tmp_path / "out", "--calculator", url(server), "--calculator-batch", "8000")
        paths = {p for p, _ in server.stub.calls}
    finally:
        stop(server)
    assert "/v1/jobs" in paths and "/v1/credit-risk/irb" not in paths
    r = rows(tmp_path / "out")
    assert r and all(x["rea"] == x["ead"] for x in r)      # risk weight 1, exact decimals
    assert " jobs" in findings(tmp_path / "out")["CALC-000"]["message"]


def test_faults_mode_retries_and_reports_rejections(reference_sim, tmp_path):
    server = start_background("faults", reject=r"7\|adverse\|3$")
    try:
        run(reference_sim, tmp_path / "out", "--calculator", url(server))
    finally:
        stop(server)
    f = findings(tmp_path / "out")
    assert " 0 retries" not in f["CALC-000"]["message"]    # injected 429/503 were retried
    r = rows(tmp_path / "out")
    rejected = sum(int(x["records_rejected"]) for x in r)
    assert rejected > 0 and f["CALC-010"]["count"] == rejected
    assert all(x["records_rejected"] == "0" for x in r if (x["scenario"], x["year"]) != ("adverse", "3"))
    assert "7|adverse|3" in f["CALC-010"]["message"]


def test_all_records_rejected_fails_the_run(reference_sim, tmp_path):
    server = start_background("formula", reject=".")
    try:
        r = run(reference_sim, tmp_path / "out", "--calculator", url(server), check=False)
    finally:
        stop(server)
    assert r.returncode == 1 and "CALC-011" in r.stderr
    assert not (tmp_path / "out" / "rea.csv").exists()


def sent_records(reference_sim, out, *extra):
    """Runs against the `fixed` stub, which echoes the PD/LGD it received as pdApplied/lgdApplied."""
    server = start_background("fixed")
    try:
        run(reference_sim, out, "--calculator", url(server), *extra)
        return {r["recordId"]: r for _, body in server.stub.responses.values() for r in body["results"]}
    finally:
        stop(server)


def write_params(path, rows):
    params = ["pd12m_s1", "pd12m_s2", "tr1_2", "tr2_1", "tr3_1", "tr3_2", "lgd_s1", "lgd_s2", "lgd_s3", "lrlt_s2"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["level", "key", "scenario", "year", *params, "pd_reg", "lgd_reg", "source"])
        w.writeheader()
        for r in rows:
            w.writerow({"source": "external", **r})


def test_exposure_level_parameters_reach_the_records(reference_sim, tmp_path):
    import duckdb
    eid, = duckdb.sql(f"""SELECT exposure_id FROM read_parquet('{reference_sim}/sim_exposure/**/*.parquet')
                          WHERE stage = 'stage1' AND measurement_category = 'amortised_cost' AND exposure_type = 'loan'
                            AND household_purpose = 'house_purchase' ORDER BY gross_carrying_amount DESC LIMIT 1""").fetchone()
    write_params(tmp_path / "p.csv", [{"level": "exposure", "key": eid, "scenario": "actual", "year": 0, "pd12m_s1": "0.5"}])
    sent = sent_records(reference_sim, tmp_path / "out", "--parameters", str(tmp_path / "p.csv"))
    assert sent[f"{eid}|actual|0"]["pdApplied"] == "0.500000000"
    assert sent[f"{eid}|adverse|1"]["pdApplied"] != "0.500000000"      # projected with the segment's satellite
    assert "CALC-002" in findings(tmp_path / "out")


def test_regulatory_parameters_replace_the_pit_proxy(reference_sim, tmp_path):
    write_params(tmp_path / "p.csv", [{"level": "segment", "key": "ALL|ALL|ALL", "scenario": "actual", "year": 0,
                                        "pd_reg": "0.02", "lgd_reg": "0.3"}])
    sent = sent_records(reference_sim, tmp_path / "out", "--parameters", str(tmp_path / "p.csv"))
    # pd_reg for every point (starting-point fallback), PD 1 for defaulted exposures; lgd_reg everywhere.
    assert {r["pdApplied"] for r in sent.values()} == {"0.020000000", "1.000000000"}
    assert {r["lgdApplied"] for r in sent.values()} == {"0.300000000"}
    assert "CALC-002" not in findings(tmp_path / "out")
