"""sora-mcp tools, explain_result and diff_runs.

The tool functions are tested without the MCP SDK; the protocol smoke test runs only if ``mcp`` is installed.
Engine tests run ``sora`` (SORA_ENGINE or build/release/sora) and are skipped if it is not built.
"""

import asyncio
import csv
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from sora_tools import cli
from sora_tools.diff_runs import diff_runs
from sora_tools.explain import explain_result
from sora_tools.mcp_server import Policy, PolicyError, SoraTools
from sora_tools.mcp_server import main as mcp_main
from sora_tools.results import PARAMS, ResultError, RunOutput

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests" / "golden" / "20260630"
SCENARIO = REPO / "tests" / "scenarios" / "test_eba2025.yaml"
ENGINE = Path(os.environ.get("SORA_ENGINE", REPO / "build" / "release" / "sora"))
needs_engine = pytest.mark.skipif(not ENGINE.is_file(), reason="C++ engine not built")


def make_tools(tmp_path, *roots, **kw) -> SoraTools:
    (tmp_path / "data").mkdir(exist_ok=True)
    policy = Policy.create(roots=[REPO, tmp_path / "data", *roots], workdir=tmp_path / "work",
                           approvals_dir=tmp_path / "approvals", engine=ENGINE if ENGINE.is_file() else None, **kw)
    return SoraTools(policy)


# -- tiny export and mapping (fast) ------------------------------------------------------------------------
def tiny_export(root: Path) -> Path:
    exp = root / "export"
    (exp / "ref").mkdir(parents=True)
    (exp / "ref" / "entity.csv").write_text("entity_id,country,ccy,legal_name\nE1,BE,EUR,Alpha Bank SA\n"
                                            "E2,DE,EUR,Beta Bank AG\n")
    (exp / "ref" / "fx.csv").write_text("ccy,day,rate\nUSD,2026-06-30,0.9\nGBP,2026-06-30,1.17\n")
    return exp


def tiny_mapping(root: Path, status: str | None = None) -> Path:
    m = root / "mapping"
    m.mkdir(parents=True)
    (m / "mapping.yaml").write_text(
        "name: tiny\nsim_version: 1.0.0-draft\n" + (f"status: {status}\n" if status else "")
        + "manifest: {reference_date: 2026-06-30, reporting_currency: EUR, reporting_entity_id: E1}\n"
        "sources:\n  format: csv\n  path_template: 'ref/{table}.csv'\n  tables:\n    entity:\n    fx:\n"
        "tables: [sim_entity, sim_fx_rate]\n")
    (m / "sim_entity.sql").write_text("SELECT entity_id, country, ccy AS functional_currency FROM src.entity")
    (m / "sim_fx_rate.sql").write_text(
        "SELECT ccy AS currency, CAST(day AS DATE) AS rate_date, CAST(rate AS DECIMAL(18,9)) AS rate_to_reporting "
        "FROM src.fx")
    return m


# -- describe ----------------------------------------------------------------------------------------------
def test_describe(tmp_path):
    t = make_tools(tmp_path)
    ov = t.call("describe")
    assert ov["status"] == "ok" and any(x["name"] == "sim_exposure" for x in ov["tables"])
    tab = t.call("describe", table="sim_exposure")
    assert "gross_carrying_amount" in tab["columns"] and tab["primary_key"]
    col = t.call("describe", table="sim_exposure", column="stage")
    assert col["column"]["values"]
    assert "TABLE sim_exposure" in t.call("describe", format="llm")["text"]
    assert "projection.csv" in t.call("describe", table="outputs")["output_files"]
    assert t.call("describe", table="nope")["status"] == "error"
    assert t.call("describe", bogus=1)["status"] == "error"          # arguments are checked
    assert t.call("rm_rf")["status"] == "error"                      # unknown tool


# -- security: roots, values, approval -----------------------------------------------------------------------
def test_paths_outside_roots_are_denied(tmp_path):
    t = make_tools(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "data" / "link").symlink_to(outside, target_is_directory=True)
    for p in ("/etc", str(outside), str(tmp_path / "data" / ".." / "outside"), str(tmp_path / "data" / "link"),
              "../../../../etc", "bad\x00path"):
        r = t.call("explain_result", output_dir=p)
        assert r["status"] == "denied", (p, r)
    assert t.call("profile_source", export_dir="/")["status"] == "denied"
    assert t.call("validate_sim", sim_dir=str(outside))["status"] == "denied"
    assert t.call("diff_runs", run_a=str(GOLDEN), run_b="/tmp")["status"] == "denied"
    # inside the roots it works (relative paths are relative to the first root)
    assert t.call("explain_result", output_dir="tests/golden/20260630")["status"] == "ok"
    with pytest.raises(PolicyError):
        Policy.create(roots=[], workdir=tmp_path / "w")
    with pytest.raises(PolicyError):   # approvals must not be writable through the work directory
        Policy.create(roots=[REPO], workdir=tmp_path / "w", approvals_dir=tmp_path / "w" / "approvals")


def test_profile_source_is_metadata_only_by_default(tmp_path):
    exp = tiny_export(tmp_path / "data")
    t = make_tools(tmp_path)
    r = t.call("profile_source", export_dir=str(exp))
    assert r["status"] == "ok" and set(r["tables"]) == {"entity", "fx"}
    assert r["tables"]["entity"]["rows"] == 2 and not r["values_included"]
    text = json.dumps(r)
    assert "Alpha Bank" not in text and "0.9" not in text and "sample" not in r["tables"]["entity"]
    assert "codes" not in r["tables"]["entity"]["columns"]["legal_name"]
    # include_values is a server setting, off by default
    assert t.call("profile_source", export_dir=str(exp), include_values=True)["status"] == "denied"
    t2 = make_tools(tmp_path, allow_values=True, max_rows=1)
    r2 = t2.call("profile_source", export_dir=str(exp), tables=["entity"], include_values=True, max_rows=1000)
    assert r2["values_included"] and len(r2["tables"]["entity"]["sample"]) == 1       # capped
    assert set(r2["tables"]) == {"entity"} and r2["tables"]["entity"]["columns"]["country"]["min"] == "BE"
    assert t.call("profile_source", export_dir=str(exp), tables=["nope"])["status"] == "error"


def test_profile_source_reference_export(tmp_path, testdata):
    t = make_tools(tmp_path, testdata)
    r = t.call("profile_source", export_dir=str(testdata), tables=["entity", "fx_rate"])
    assert r["status"] == "ok" and r["tables"]["fx_rate"]["rows"] > 0
    assert "contract_loan" in r["available_tables"]
    assert all("sample" not in x for x in r["tables"].values())


def test_draft_mapping_runs_and_validates(tmp_path):
    root = tmp_path / "data"
    exp, m = tiny_export(root), tiny_mapping(root)
    t = make_tools(tmp_path)
    r = t.call("test_mapping", mapping_dir=str(m), export_dir=str(exp))
    assert r["status"] == "ok", r
    sim = Path(r["sim_dir"])
    assert sim.is_relative_to(tmp_path / "work" / "sim") and (sim / "sim_manifest.json").is_file()
    assert r["tables"]["sim_entity"]["rows"] == 2
    v = r["validation"]
    assert not v["ok"] and any(f["check_id"] == "STR-001" for f in v["findings"])   # sim_exposure etc. missing
    assert all(f["samples"] == [] for f in v["findings"])                           # no sample keys by default
    again = t.call("validate_sim", sim_dir=r["sim_dir"], modules=["core"])
    assert again["status"] == "ok" and again["row_counts"]["sim_fx_rate"] == 2
    assert t.call("validate_sim", sim_dir=r["sim_dir"], modules=["nope"])["status"] == "error"
    # audit log: every call, with an input fingerprint
    log = [json.loads(x) for x in (tmp_path / "work" / "audit.jsonl").read_text().splitlines()]
    assert [x["tool"] for x in log] == ["test_mapping", "validate_sim", "validate_sim"]
    assert all(x["input_fingerprint"].startswith("sha256:") for x in log)


def test_production_mapping_requires_approval(tmp_path, capsys):
    root = tmp_path / "data"
    exp, m = tiny_export(root), tiny_mapping(root, status="production")
    t = make_tools(tmp_path)
    r = t.call("test_mapping", mapping_dir=str(m), export_dir=str(exp))
    assert r["status"] == "requires_approval" and "status: production" in r["reason"]
    rid = r["approval_request"]["id"]
    assert not (tmp_path / "work" / "sim").exists()                    # nothing written
    assert (tmp_path / "approvals" / f"{rid}.pending.json").is_file()
    # the same request again: same id, still not approved
    assert t.call("test_mapping", mapping_dir=str(m), export_dir=str(exp))["approval_request"]["id"] == rid

    assert mcp_main(["pending", "--approvals-dir", str(tmp_path / "approvals")]) == 0
    assert rid in capsys.readouterr().out
    assert mcp_main(["approve", "0000", "--by", "x", "--approvals-dir", str(tmp_path / "approvals")]) == 2
    assert mcp_main(["approve", rid, "--by", "Jane Reviewer", "--approvals-dir", str(tmp_path / "approvals")]) == 0

    ok = t.call("test_mapping", mapping_dir=str(m), export_dir=str(exp))
    assert ok["status"] == "ok" and ok["approval"]["approved_by"] == "Jane Reviewer"
    # any change to the mapping is a new release and needs a new approval
    (m / "sim_entity.sql").write_text("SELECT entity_id, country, 'EUR' AS functional_currency FROM src.entity")
    r2 = t.call("test_mapping", mapping_dir=str(m), export_dir=str(exp))
    assert r2["status"] == "requires_approval" and r2["approval_request"]["id"] != rid


def test_production_path_pattern(tmp_path):
    root = tmp_path / "data"
    exp = tiny_export(root)
    m = tiny_mapping(root / "production")
    r = make_tools(tmp_path).call("test_mapping", mapping_dir=str(m), export_dir=str(exp))
    assert r["status"] == "requires_approval" and "pattern" in r["reason"]
    # a custom pattern list replaces the default
    t = make_tools(tmp_path, production_patterns=["*/released/*"])
    assert t.call("test_mapping", mapping_dir=str(m), export_dir=str(exp))["status"] == "ok"


def test_mapping_types_file_must_stay_in_export(tmp_path):
    root = tmp_path / "data"
    exp, m = tiny_export(root), tiny_mapping(root)
    y = (m / "mapping.yaml").read_text().replace("  format: csv\n", "  format: csv\n  types_file: ../../secret.json\n")
    (m / "mapping.yaml").write_text(y)
    assert make_tools(tmp_path).call("test_mapping", mapping_dir=str(m), export_dir=str(exp))["status"] == "denied"


def test_reconcile(tmp_path, testdata, reference_sim):
    controls = tmp_path / "data" / "controls.yaml"
    (tmp_path / "data").mkdir(exist_ok=True)
    controls.write_text("""
controls:
  - id: LOANS
    keys: 1
    sim: SELECT entity_id, count(*), sum(gross_carrying_amount) FROM sim_exposure WHERE exposure_type = 'loan' GROUP BY 1
    source: SELECT entity_id, count(*), sum(CAST(gross_carrying_amount AS DECIMAL(18,2))) FROM src.contract_loan GROUP BY 1
  - id: SHIFTED
    sim: SELECT count(*) + 5 AS n FROM sim_counterparty
    source: SELECT count(*) FROM src.counterparty
    keys: 0
  - id: BROKEN
    sim: SELECT nope FROM sim_counterparty
    source: SELECT 1
""")
    t = make_tools(tmp_path, testdata, reference_sim)
    r = t.call("reconcile", sim_dir=str(reference_sim), export_dir=str(testdata), mapping_dir="mappings/cppbank",
               controls_file=str(controls))
    assert r["status"] == "ok" and not r["ok"]
    by = {c["id"]: c for c in r["controls"]}
    assert by["LOANS"]["status"] == "ok" and by["LOANS"]["groups"] > 1
    assert by["SHIFTED"]["status"] == "difference" and by["SHIFTED"]["totals"]["n"]["difference"] == 5
    assert by["BROKEN"]["status"] == "error"
    assert (REPO / "mappings" / "cppbank" / "reconciliation.yaml").is_file()   # the default controls


# -- explain_result -------------------------------------------------------------------------------------------
def test_explain_golden_segment():
    r = explain_result(GOLDEN, "LOANS|GG|BE", "adverse", 3)
    assert r["segment_info"]["portfolio"] == "GG" and r["starting_point"]["exposure"]["total"] > 0
    sp = r["parameters"]["starting_point"]
    assert sp["source"] == "derived" and sp["calibration_levels"]["stage3"]["level"] in ("instrument", "portfolio", "all", "segment")
    assert r["benchmark"]["pd_tr"]["rule"] == "sovereign" and r["benchmark"]["pd_tr"]["applied"]
    assert [(p["scenario"], p["year"]) for p in r["projection"]] == [("adverse", 3)]
    p = r["projection"][0]
    boxes = {c["box"] for c in p["provision_components"]}
    assert boxes == {"Box 4", "Box 5", "Box 6", "Box 7", "Box 8", "Box 9"}
    assert all(p["checks"].values())
    assert any(x["source"] == "benchmark" for x in r["parameters"]["path"])
    assert "ECB benchmark" in r["narrative"] and "Adverse year 3" in r["narrative"]


def test_explain_all_golden_segments_reconcile():
    out = RunOutput(GOLDEN)
    for seg in out.segments():
        r = explain_result(GOLDEN, seg)
        assert len(r["projection"]) == 6
        for p in r["projection"]:
            assert all(p["checks"].values()), (seg, p["scenario"], p["year"])
            comp = {c["column"]: c["amount"] for c in p["provision_components"]}
            st = p["provision_stock"]
            assert abs(comp["prov_s1_s1"] + comp["prov_s2_s1"] - st["s1"]) < 0.05
            assert abs(comp["prov_s1_s2"] + comp["prov_s2_s2"] - st["s2"]) < 0.05


def test_explain_overview_and_errors(capsys):
    r = explain_result(GOLDEN, scenario="adverse")
    assert {t["year"] for t in r["totals"]} == {1, 2, 3} and len(r["top_segments"]) == 10
    total = json.loads((GOLDEN / "summary.json").read_text())["totals"]["adverse/2"]["impairment"]
    assert abs(next(t["impairment"] for t in r["totals"] if t["year"] == 2) - total) < 1.0
    with pytest.raises(ResultError, match="similar"):
        explain_result(GOLDEN, "LOANS|NFC|DE")
    with pytest.raises(ResultError):
        explain_result(GOLDEN, "LOANS|GG|BE", "adverse", 7)
    assert cli.main(["explain", str(GOLDEN), "--segment", "LOANS|HH_HOUSE|DE", "--scenario", "adverse"]) == 0
    assert "Segment LOANS|HH_HOUSE|DE" in capsys.readouterr().out
    assert cli.main(["explain", str(GOLDEN), "--segment", "nope"]) == 2


# -- diff_runs (synthetic) ------------------------------------------------------------------------------------
def _copy_golden(dst: Path) -> Path:
    shutil.copytree(GOLDEN, dst, ignore=shutil.ignore_patterns("README.md"))
    return dst


def _scale_segment(out: Path, segment: str, factor: float):
    """Scale every amount of one segment (exposure and provisions): a pure exposure change."""
    for name, amount_cols in (("segments.csv", lambda c: c.startswith(("exp_", "prov_"))),
                              ("projection.csv", lambda c: c not in ("segment", "scenario", "year"))):
        with open(out / name) as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            if r["segment"] == segment:
                for c in r:
                    if amount_cols(c):
                        r[c] = f"{float(r[c]) * factor:.2f}"
        with open(out / name, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)


def test_diff_identical_and_exposure_attribution(tmp_path, capsys):
    a = GOLDEN
    same = diff_runs(a, a)
    assert same["identical"] and same["impairment"]["rows_changed"] == 0
    b = _copy_golden(tmp_path / "b")
    seg = "LOANS|HH_HOUSE|DE"
    _scale_segment(b, seg, 2.0)
    r = diff_runs(a, b, top=3)
    assert not r["identical"]
    assert r["files"]["segments.csv"]["rows_changed"] == 1 and r["files"]["parameters.csv"]["status"] == "same"
    movers = r["impairment"]["top_movers"]
    assert movers and all(m["segment"] == seg for m in movers)
    for m in movers:        # doubled exposure at unchanged coverage: all exposure effect
        assert abs(m["attribution"]["exposure"] - m["delta"]) < 0.05 * abs(m["delta"]) + 1.0
        assert any("starting exp_s1" in d for d in m["drivers"])
    for t in r["impairment"]["totals"]:
        assert abs(t["attribution"]["exposure"] + t["attribution"]["coverage"] - t["delta"]) < 1e-6
    # filters
    f = diff_runs(a, b, segment="LOANS|GG|BE")
    assert f["files"]["projection.csv"]["status"] == "same" and f["impairment"]["rows_changed"] == 0
    assert cli.main(["diff-runs", str(a), str(b), "--fail-on-diff"]) == 1
    assert cli.main(["diff-runs", str(a), str(a), "--fail-on-diff"]) == 0
    assert "identical" in capsys.readouterr().out
    with pytest.raises(ResultError):
        diff_runs(a, tmp_path / "missing")


def test_diff_segment_only_in_one_run(tmp_path):
    b = _copy_golden(tmp_path / "b")
    seg = "LOANS|GG|BE"
    for name in ("segments.csv", "projection.csv"):
        with open(b / name) as f:
            rows = [r for r in csv.DictReader(f) if r["segment"] != seg]
        with open(b / name, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    r = diff_runs(GOLDEN, b, segment=seg)
    assert r["files"]["segments.csv"]["only_in_a"] == 1
    m = r["impairment"]["top_movers"][0]
    assert m["attribution"]["coverage"] == 0 and "segment only in A" in m["drivers"]


# -- engine: run_scenario, overlay, diff ------------------------------------------------------------------------
@pytest.fixture(scope="module")
def engine_runs(reference_sim, tmp_path_factory):
    """Base run and a run with a PD overlay (adverse PDs x 1.5 for every segment), both through run_scenario."""
    if not ENGINE.is_file():
        pytest.skip("C++ engine not built")
    tmp = tmp_path_factory.mktemp("mcp_engine")
    (tmp / "data").mkdir()
    t = SoraTools(Policy.create(roots=[REPO, tmp / "data", reference_sim], workdir=tmp / "work",
                                approvals_dir=tmp / "approvals", engine=ENGINE))
    base = t.call("run_scenario", sim_dir=str(reference_sim), scenario=str(SCENARIO))
    assert base["status"] == "ok", base
    overlay = tmp / "data" / "pd_multiplier_adverse.csv"
    with open(Path(base["output_dir"]) / "parameters.csv") as f, open(overlay, "w", newline="") as g:
        w = csv.DictWriter(g, fieldnames=["level", "key", "scenario", "year", *PARAMS, "source"])
        w.writeheader()
        for r in csv.DictReader(f):
            if r["level"] == "segment" and r["scenario"] == "adverse":
                p1 = min(float(r["pd12m_s1"]) * 1.5, 1 - float(r["tr1_2"]))
                p2 = min(float(r["pd12m_s2"]) * 1.5, 1 - float(r["tr2_1"]))
                w.writerow({"level": "segment", "key": r["key"], "scenario": "adverse", "year": r["year"],
                            "pd12m_s1": f"{p1:.9f}", "pd12m_s2": f"{p2:.9f}", "source": "external"})
    stressed = t.call("run_scenario", sim_dir=str(reference_sim), scenario=str(SCENARIO), parameters=str(overlay),
                      workers=2)
    assert stressed["status"] == "ok", stressed
    return t, tmp, Path(base["output_dir"]), Path(stressed["output_dir"])


@needs_engine
def test_run_scenario_outputs(engine_runs):
    t, tmp, base, _stressed = engine_runs
    assert base.is_relative_to(tmp / "work" / "runs") and (base / "run.json").is_file()
    assert "projection.csv" in RunOutput(base).files()
    # the engine output matches the golden results (the golden tolerance: 1 cent per value)
    g = diff_runs(GOLDEN, base, abs_tol=0.011)
    for name in ("projection.csv", "parameters.csv", "segments.csv", "benchmarks.csv", "off_balance.csv"):
        assert g["files"][name]["status"] == "same", (name, g["files"][name])
    ex = t.call("explain_result", output_dir=str(base))
    assert ex["status"] == "ok" and any(d["id"] == "BMK-002" for d in ex["diagnostics"])


@needs_engine
def test_diff_runs_pd_overlay(engine_runs):
    t, _tmp, base, stressed = engine_runs
    r = t.call("diff_runs", run_a=str(base), run_b=str(stressed), top=5)
    assert r["status"] == "ok" and not r["identical"]
    assert r["files"]["segments.csv"]["status"] == "same"
    assert r["files"]["projection.csv"]["rows_changed"] > 0
    assert r["files"]["parameters.csv"]["columns"]["source"]["changed"] > 0
    assert any(d["id"] == "PAR-000" for d in r["diagnostics"]["added"])
    tot = {(x["scenario"], x["year"]): x for x in r["impairment"]["totals"]}
    for y in (1, 2, 3):
        assert abs(tot.get(("baseline", y), {"delta": 0})["delta"]) < 0.01      # baseline untouched
        a = tot[("adverse", y)]
        assert a["delta"] > 0                                                   # higher PDs, more impairment
        assert abs(a["attribution"]["exposure"]) < 1.0                          # static balance sheet, same SIM
        assert abs(a["attribution"]["coverage"] - a["delta"]) < 1.0
    sa = json.loads((base / "summary.json").read_text())["totals"]
    sb = json.loads((stressed / "summary.json").read_text())["totals"]
    assert abs(tot[("adverse", 3)]["delta"] - (sb["adverse/3"]["impairment"] - sa["adverse/3"]["impairment"])) < 1.0
    top = r["impairment"]["top_movers"]
    assert len(top) == 5 and top[0]["scenario"] == "adverse"
    assert any(d.startswith("pd12m_s1 adverse/") and "+50.0%" in d for d in top[0]["drivers"])
    assert any(e["path"] == "totals.adverse/3.impairment" for e in r["summary"]["entries"])
    assert "coverage/parameters" in r["narrative"]
    # explain the biggest mover in the stressed run: external/mixed parameters
    ex = t.call("explain_result", output_dir=str(stressed), segment=top[0]["segment"], scenario="adverse")
    assert {p["source"] for p in ex["parameters"]["path"] if p["scenario"] == "adverse"} <= {"mixed", "external", "benchmark"}


@needs_engine
def test_run_scenario_security(engine_runs, tmp_path):
    t, tmp, _base, _stressed = engine_runs
    sim = t.policy.roots[-1]
    assert t.call("run_scenario", sim_dir=str(sim), scenario="/etc/hostname")["status"] == "denied"
    # paths inside the scenario YAML are checked too
    bad = tmp / "data" / "bad.yaml"
    bad.write_text(SCENARIO.read_text().replace("scenarios/eba2025_macro.csv", "/etc/passwd"))
    r = t.call("run_scenario", sim_dir=str(sim), scenario=str(bad))
    assert r["status"] == "denied" and "macro_path" in r["error"]
    assert t.call("run_scenario", sim_dir=str(sim), scenario=str(SCENARIO), workers=0)["status"] == "error"
    # shell metacharacters are just (non-existent) paths, never interpreted
    assert t.call("run_scenario", sim_dir=str(sim), scenario="x.yaml; touch /tmp/pwned")["status"] == "error"
    no_engine = make_tools(tmp_path)
    no_engine.policy.engine = None
    assert no_engine.call("run_scenario", sim_dir=str(REPO), scenario=str(SCENARIO))["status"] == "error"


# -- MCP protocol smoke test ---------------------------------------------------------------------------------
def test_mcp_protocol_smoke(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO / "python"), os.environ.get("PYTHONPATH", "")])}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sora_tools.mcp_server", "serve", "--root", str(REPO), "--workdir", str(tmp_path / "work"),
              "--approvals-dir", str(tmp_path / "approvals")],
        env=env)

    def payload(res):
        return json.loads(res.content[0].text)

    async def session():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                assert names == set(SoraTools.TOOLS)
                d = payload(await s.call_tool("describe", {"table": "sim_entity"}))
                assert d["status"] == "ok" and d["table"] == "sim_entity"
                e = payload(await s.call_tool("explain_result", {"output_dir": str(GOLDEN), "segment": "LOANS|GG|BE",
                                                                  "scenario": "adverse", "year": 1}))
                assert e["status"] == "ok" and "narrative" in e
                x = payload(await s.call_tool("explain_result", {"output_dir": "/etc"}))
                assert x["status"] == "denied"

    asyncio.run(asyncio.wait_for(session(), timeout=120))
    assert (tmp_path / "work" / "audit.jsonl").read_text().count("\n") == 3
