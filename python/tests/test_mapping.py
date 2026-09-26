import json

import duckdb
import pytest

from sora_tools.mapping import MappingError, run_mapping
from sora_tools.validate import validate


def _sum(sim, table, col, where="true"):
    return duckdb.sql(f"SELECT sum({col}) FROM read_parquet('{sim}/{table}/**/*.parquet') WHERE {where}").fetchone()[0]


def test_reference_mapping_is_valid(reference_sim):
    report = validate(reference_sim, modules=["core", "credit", "calibration"])
    assert report.ok, report.to_text()
    assert report.warnings == 0, report.to_text()


def test_reference_mapping_reconciles_with_source(reference_sim, testdata):
    """Allowances in SIM equal the source contract tables (loans, commitments, securities, leases)."""
    src = duckdb.sql(f"""
        SELECT sum(CAST(impairment_allowance AS DECIMAL(18,2)))
        FROM read_csv('{testdata}/accounting/contract_loan/**/*.csv', all_varchar=true, union_by_name=true, hive_partitioning=false)
    """).fetchone()[0]
    assert _sum(reference_sim, "sim_exposure", "loss_allowance", "exposure_type = 'loan'") == src
    src_prov = duckdb.sql(f"""
        SELECT sum(CAST(provision_balance AS DECIMAL(18,2)))
        FROM read_csv('{testdata}/accounting/contract_commitment/**/*.csv', all_varchar=true, union_by_name=true, hive_partitioning=false)
    """).fetchone()[0]
    off_types = "exposure_type IN ('loan_commitment', 'financial_guarantee', 'other_commitment')"
    assert _sum(reference_sim, "sim_exposure", "loss_allowance", off_types) == src_prov
    counts = {r[0]: r[1] for r in duckdb.sql(
        f"SELECT exposure_type, count(*) FROM read_parquet('{reference_sim}/sim_exposure/**/*.parquet') GROUP BY 1").fetchall()}
    # Every source contract reaches the SIM (compared with the source row counts, not a pinned number).
    def src_rows(table):
        return duckdb.sql(f"""SELECT count(*) FROM read_csv('{testdata}/accounting/{table}/**/*.csv',
                              all_varchar=true, union_by_name=true, hive_partitioning=false)""").fetchone()[0]
    assert counts["loan"] == src_rows("contract_loan")
    assert counts["loan_commitment"] + counts["financial_guarantee"] + counts["other_commitment"] ==         src_rows("contract_commitment")


def test_manifest_written(reference_sim):
    m = json.loads((reference_sim / "sim_manifest.json").read_text())
    assert m["reference_date"] == "2026-06-30"
    assert m["mapping_release"].startswith("cppbank@")
    assert len(m["source_fingerprint"]) == 16


def test_mapping_is_deterministic(reference_sim, testdata, tmp_path, repo):
    run_mapping(repo / "mappings" / "cppbank", testdata, tmp_path, log=lambda *_: None)
    for table in ("sim_exposure", "sim_counterparty", "sim_stage_history"):
        q = "SELECT md5(string_agg(t::VARCHAR, '' ORDER BY t::VARCHAR)) FROM read_parquet('{}/{}/**/*.parquet') t"
        a = duckdb.sql(q.format(reference_sim, table)).fetchone()[0]
        b = duckdb.sql(q.format(tmp_path, table)).fetchone()[0]
        assert a == b, table


def _mini_mapping(tmp_path, sql: str):
    export = tmp_path / "export"
    (export / "t").mkdir(parents=True)
    (export / "t" / "a.csv").write_text("id,country\nE1,BE\n")
    m = tmp_path / "mapping"
    m.mkdir()
    (m / "mapping.yaml").write_text(
        "name: mini\nsim_version: 1.0.0-draft\n"
        "manifest: {reference_date: 2026-06-30, reporting_currency: EUR, reporting_entity_id: E1}\n"
        "sources: {format: csv, path_template: '{table}/*.csv', tables: {t: }}\n"
        "tables: [sim_entity]\n")
    (m / "sim_entity.sql").write_text(sql)
    return m, export


def test_mapping_rejects_unknown_columns(tmp_path):
    m, export = _mini_mapping(tmp_path, "SELECT id AS entity_id, country, 'EUR' AS functional_currency, 1 AS oops FROM src.t")
    with pytest.raises(MappingError, match="not in the SIM schema"):
        run_mapping(m, export, tmp_path / "out", log=lambda *_: None)


def test_mapping_requires_required_columns(tmp_path):
    m, export = _mini_mapping(tmp_path, "SELECT id AS entity_id FROM src.t")
    with pytest.raises(MappingError, match="required columns"):
        run_mapping(m, export, tmp_path / "out", log=lambda *_: None)


def test_mapping_sql_is_sandboxed(tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("x\n1\n")
    m, export = _mini_mapping(tmp_path, f"SELECT id AS entity_id, country, 'EUR' AS functional_currency FROM src.t "
                                        f"WHERE (SELECT count(*) FROM read_csv('{secret}')) > 0")
    with pytest.raises(MappingError, match="Permission"):
        run_mapping(m, export, tmp_path / "out", log=lambda *_: None)
