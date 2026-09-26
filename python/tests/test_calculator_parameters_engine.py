"""The engine's credit-parameter source through the regulatory calculator (POST /v1/parameters/credit,
`sora run --calculator <url> --calculator-parameters <list>`), against the in-process stub (skipped if the engine
is not built). Registered with ctest as part of `sora_calculator`.

One parameter run sends ~60k records (in-scope exposures and off-balance items) in 13 batches and takes seconds.
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
PARAMS = ["pd12m_s1", "pd12m_s2", "tr1_2", "tr2_1", "tr3_1", "tr3_2", "lgd_s1", "lgd_s2", "lgd_s3", "lrlt_s2"]
ALL = [*PARAMS, "ccf", "pd_reg", "lgd_reg"]

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


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def findings(out):
    return {f["id"]: f for f in json.loads((out / "diagnostics.json").read_text())["findings"]}


def params_run(sim, out, server, *extra):
    return run(sim, out, "--calculator", url(server), "--calculator-parameters", "all", "--calculator-rea", "off", *extra)


@pytest.fixture(scope="module")
def plain_run(reference_sim, tmp_path_factory):
    out = tmp_path_factory.mktemp("plain") / "out"
    run(reference_sim, out)
    return out


@pytest.fixture(scope="module")
def formula_params(reference_sim, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("calc_params")
    server = start_background("formula")
    try:
        params_run(reference_sim, tmp / "out", server, "--calculator-cache", str(tmp / "cache"))
        calls = list(server.stub.calls)
    finally:
        stop(server)
    return tmp, calls, url(server)


def test_parameters_from_the_calculator_drive_the_projection(formula_params, plain_run):
    tmp, calls, _ = formula_params
    out = tmp / "out"
    assert {p for p, _ in calls} == {"/v1/parameters/credit"}              # REA off: no IRB calls
    keys = [k for _, k in calls]
    assert len(keys) == len(set(keys))                                      # one request per batch
    assert not (out / "rea.csv").exists()
    raw = read_csv(out / "calculator_parameters.csv")
    assert raw and all(r["status"] == "ok" for r in raw)
    assert list(raw[0])[2:-1] == ALL
    assert len({r["pd12m_s1"] for r in raw}) > 1                            # per-record values (stage, sector)
    summary = json.loads((out / "summary.json").read_text())["calculator_parameters"]
    assert summary["calculator"] == "sora-calculator-stub" and summary["records"] == len(raw)
    assert summary["records_ok"] == len(raw) and summary["records_rejected"] == 0
    assert all(c == {"received": len(raw), "applied": len(raw), "file": 0, "missing": 0}
               for c in summary["parameters"].values())
    assert summary["sources"] == {"model": len(raw) * len(ALL)}
    f = findings(out)
    assert f["CALC-020"]["count"] == len(raw) and f["PAR-003"]["count"] > 0
    assert not {"CALC-021", "CALC-022", "CALC-023", "CALC-025"} & f.keys()
    assert f["OBS-003"]["count"] > 0                                         # calculator CCFs for the off-balance items
    # parameters.csv: the segment rows are unchanged; one exposure row per exposure with calculator values.
    plain = read_csv(plain_run / "parameters.csv")
    rows = read_csv(out / "parameters.csv")
    assert [r for r in rows if r["level"] == "segment"] == plain
    exp = [r for r in rows if r["level"] == "exposure"]
    assert len(exp) == len(raw)
    by_id = {r["exposure_id"]: r for r in raw}
    for r in exp[:200]:
        assert r["source"] == "calculator" and (r["scenario"], r["year"]) == ("actual", "0")
        assert all(r[p] == by_id[r["key"]][p] for p in PARAMS)
    # The exposure-level starting points change the provisions.
    assert (out / "projection.csv").read_bytes() != (plain_run / "projection.csv").read_bytes()


def test_rerun_replays_parameters_from_cache_without_calculator(reference_sim, formula_params, tmp_path):
    tmp, _, calculator = formula_params                                     # the stub is stopped by now
    r = run(reference_sim, tmp_path / "out", "--calculator", calculator, "--calculator-parameters", "all",
            "--calculator-rea", "off", "--calculator-cache", str(tmp / "cache"))
    assert "CALC-021" in r.stderr
    for name in ("projection.csv", "parameters.csv", "calculator_parameters.csv", "cr_scen.csv"):
        assert (tmp_path / "out" / name).read_bytes() == (tmp / "out" / name).read_bytes(), name
    # Another parameter list is another request, not in the cache: the run fails offline.
    r = run(reference_sim, tmp_path / "miss", "--calculator", calculator, "--calculator-parameters", "pd12m_s1",
            "--calculator-rea", "off", "--calculator-cache", str(tmp / "cache"), check=False)
    assert r.returncode == 1 and "not in the replay cache" in r.stderr
    assert not (tmp_path / "miss" / "summary.json").exists()


def exposures(reference_sim, where, n=1):
    """In-scope (amortised cost, not intragroup) exposures matching `where`, by exposure_id."""
    import duckdb
    return [x for x, in duckdb.sql(f"""SELECT exposure_id FROM read_parquet('{reference_sim}/sim_exposure/**/*.parquet')
                                        WHERE measurement_category = 'amortised_cost' AND NOT coalesce(is_intragroup, false)
                                          AND {where} ORDER BY exposure_id LIMIT {n}""").fetchall()]


def write_params(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["level", "key", "scenario", "year", *ALL, "source"])
        w.writeheader()
        for r in rows:
            w.writerow({"source": "external", "scenario": "actual", "year": 0, **r})


def test_file_exposure_rows_win_and_missing_values_are_not_filled(reference_sim, tmp_path):
    loan, = exposures(reference_sim, "stage = 'stage1' AND exposure_type = 'loan'")
    commitment, = exposures(reference_sim, "exposure_type = 'loan_commitment' AND stage = 'stage1'")
    write_params(tmp_path / "p.csv", [{"level": "exposure", "key": loan, "pd12m_s1": "0.02"},
                                      {"level": "exposure", "key": commitment, "ccf": "0.75"},
                                      {"level": "segment", "key": "ALL|ALL|ALL", "lgd_s2": "0.33"}])
    server = start_background("formula", omit="lgd_s2,pd_reg")
    try:
        params_run(reference_sim, tmp_path / "out", server, "--parameters", str(tmp_path / "p.csv"))
    finally:
        stop(server)
    out = tmp_path / "out"
    raw = {r["exposure_id"]: r for r in read_csv(out / "calculator_parameters.csv")}
    n = len(raw)
    # Returned but not applied: the file's exposure rows win, field by field.
    assert raw[loan]["pd12m_s1"] and "pd12m_s1" not in raw[loan]["applied"].split(";")
    assert "lgd_s1" in raw[loan]["applied"].split(";")
    assert raw[commitment]["ccf"] and "ccf" not in raw[commitment]["applied"].split(";")
    exp = {r["key"]: r for r in read_csv(out / "parameters.csv") if r["level"] == "exposure"}
    assert exp[loan]["pd12m_s1"] == "" and exp[loan]["lgd_s1"] != ""
    # Omitted values are missing everywhere: never filled in, the next source (segment row / derived) applies.
    assert all(r["lgd_s2"] == "" and r["pd_reg"] == "" for r in raw.values())
    assert all(r["lgd_s2"] == "" for r in exp.values())
    s = json.loads((out / "summary.json").read_text())["calculator_parameters"]["parameters"]
    assert s["pd12m_s1"] == {"received": n, "applied": n - 1, "file": 1, "missing": 0}
    assert s["ccf"] == {"received": n, "applied": n - 1, "file": 1, "missing": 0}
    assert s["lgd_s2"] == {"received": 0, "applied": 0, "file": 0, "missing": n}
    f = findings(out)
    assert f["CALC-022"]["severity"] == "warning" and f["CALC-022"]["count"] == 2 * n
    assert "lgd_s2 " in f["CALC-022"]["message"] and "pd_reg " in f["CALC-022"]["message"]
    assert f["CALC-023"]["count"] == 2
    # The segment rows show the file's lgd_s2 (the calculator did not return one).
    seg = [r for r in read_csv(out / "parameters.csv") if r["level"] == "segment" and r["year"] == "0"]
    assert {r["lgd_s2"] for r in seg} == {"0.330000000"}


def test_rejected_records_fall_back_and_all_rejected_fails(reference_sim, tmp_path):
    first, = exposures(reference_sim, "stage = 'stage1' AND exposure_type = 'loan'")
    server = start_background("fixed", reject=f"^{first}$")
    try:
        params_run(reference_sim, tmp_path / "out", server)
    finally:
        stop(server)
    f = findings(tmp_path / "out")
    assert f["CALC-025"]["count"] == 1 and first in f["CALC-025"]["message"]
    raw = {r["exposure_id"]: r for r in read_csv(tmp_path / "out" / "calculator_parameters.csv")}
    assert raw[first]["status"] == "rejected" and raw[first]["pd12m_s1"] == "" and raw[first]["applied"] == ""
    assert first not in {r["key"] for r in read_csv(tmp_path / "out" / "parameters.csv") if r["level"] == "exposure"}

    server = start_background("fixed", reject=".")
    try:
        r = run(reference_sim, tmp_path / "none", "--calculator", url(server), "--calculator-parameters", "pd12m_s1",
                "--calculator-rea", "off", check=False)
    finally:
        stop(server)
    assert r.returncode == 1 and "CALC-026" in r.stderr
    assert not (tmp_path / "none" / "summary.json").exists()


def test_regulatory_parameters_from_the_calculator_reach_the_irb_records(reference_sim, tmp_path):
    server = start_background("fixed")
    try:
        run(reference_sim, tmp_path / "out", "--calculator", url(server), "--calculator-parameters", "pd_reg,lgd_reg")
        paths = {p for p, _ in server.stub.calls}
        irb = {r["recordId"]: r for p_body in server.stub.responses.values() for r in p_body[1]["results"]
               if "pdApplied" in r}
    finally:
        stop(server)
    assert paths == {"/v1/parameters/credit", "/v1/credit-risk/irb"}
    # fixed stub: pd_reg 0.01, lgd_reg 0.3 for every exposure, used at every scenario point (through the cycle);
    # defaulted exposures send PD 1.
    assert {r["pdApplied"] for r in irb.values()} == {"0.010000000", "1.000000000"}
    assert {r["lgdApplied"] for r in irb.values()} == {"0.300000000"}
    f = findings(tmp_path / "out")
    assert "CALC-002" not in f and {"CALC-000", "CALC-020"} <= f.keys()
    assert (tmp_path / "out" / "rea.csv").exists()


@pytest.mark.parametrize("args, message", [
    (["--calculator-parameters", "all"], "--calculator-parameters needs --calculator <url>"),
    (["--calculator-rea", "off"], "--calculator-rea needs --calculator <url>"),
])
def test_calculator_parameter_options_need_a_calculator(reference_sim, tmp_path, args, message):
    r = run(reference_sim, tmp_path / "out", *args, check=False)
    assert r.returncode == 2 and message in r.stderr


def test_unknown_calculator_parameter_is_an_error(reference_sim, tmp_path):
    r = run(reference_sim, tmp_path / "out", "--calculator", "http://127.0.0.1:9", "--calculator-parameters", "pd_lifetime",
            check=False)
    assert r.returncode == 1 and "unknown parameter 'pd_lifetime'" in r.stderr
