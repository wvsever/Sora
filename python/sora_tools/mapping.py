"""Run a mapping (SQL files on exported source files) and write a SIM dataset.

A mapping directory contains ``mapping.yaml`` and one ``<sim_table>.sql`` per SIM table. Each SQL file is
a single SELECT statement. It can reference:

* ``src.<source_table>`` - the exported source tables declared in ``mapping.yaml``
* ``<sim_table>`` - SIM tables produced earlier in the mapping order
* ``manifest`` - a one-row view with the manifest fields (e.g. ``reference_date``)
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .duck import quote_ident, quote_str, sandboxed_connection
from .schema import Schema, Table, load_schema

_CSV_TYPE = {"str": "VARCHAR", "int": "BIGINT", "bool": "BOOLEAN"}


@dataclass
class SourceTable:
    name: str
    path: str                  # glob relative to the export directory
    format: str = "csv"        # csv | parquet
    types: dict[str, str] = field(default_factory=dict)


@dataclass
class Mapping:
    name: str
    root: Path
    description: str
    sim_version: str
    sources: dict[str, SourceTable]
    manifest: dict[str, Any]
    tables: list[str]          # SIM tables in execution order

    def sql_for(self, table: str) -> str:
        return (self.root / f"{table}.sql").read_text()

    def release_id(self) -> str:
        h = hashlib.sha256()
        for p in sorted(self.root.glob("*")):
            if p.is_file():
                h.update(p.name.encode())
                h.update(p.read_bytes())
        return f"{self.name}@{h.hexdigest()[:12]}"


def load_mapping(root: Path | str, export_dir: Path | None = None) -> Mapping:
    root = Path(root)
    m = yaml.safe_load((root / "mapping.yaml").read_text())
    src_cfg = m["sources"]
    types_by_table: dict[str, dict[str, str]] = {}
    types_file = src_cfg.get("types_file")
    if types_file and export_dir is not None:
        raw = json.loads((Path(export_dir) / types_file).read_text())
        types_by_table = {t: {c: _CSV_TYPE.get(v, "VARCHAR") for c, v in cols.items()} for t, cols in raw.items()}
    default_fmt = src_cfg.get("format", "csv")
    sources = {}
    for name, spec in src_cfg["tables"].items():
        spec = spec or {}
        sources[name] = SourceTable(
            name=name,
            path=spec.get("path", src_cfg["path_template"].format(table=name)),
            format=spec.get("format", default_fmt),
            types=types_by_table.get(spec.get("types_key", name), {}),
        )
    return Mapping(
        name=m["name"], root=root, description=m.get("description", ""), sim_version=m["sim_version"],
        sources=sources, manifest=m["manifest"], tables=list(m["tables"]),
    )


def _source_view_sql(export_dir: Path, s: SourceTable) -> str:
    glob = quote_str(str(export_dir / s.path))
    if s.format == "parquet":
        return f"SELECT * FROM read_parquet({glob}, hive_partitioning = false, union_by_name = true)"
    opts = ["header = true", "hive_partitioning = false", "union_by_name = true", "auto_detect = true"]
    if s.types:
        opts.append("types = {" + ", ".join(f"{quote_str(c)}: {quote_str(t)}" for c, t in s.types.items()) + "}")
    else:
        opts.append("all_varchar = true")
    return f"SELECT * FROM read_csv({glob}, {', '.join(opts)})"


def _projection(schema: Schema, table: Table, available: list[str]) -> tuple[str, list[str], list[str]]:
    """SELECT list casting mapping output to the schema. Returns (sql, missing_required, missing_optional)."""
    cols, missing_req, missing_opt = [], [], []
    avail = set(available)
    for c in table.columns.values():
        typ = schema.storage_type(c)
        if c.name in avail:
            cols.append(f"CAST({quote_ident(c.name)} AS {typ}) AS {quote_ident(c.name)}")
        else:
            (missing_req if c.required else missing_opt).append(c.name)
            cols.append(f"CAST(NULL AS {typ}) AS {quote_ident(c.name)}")
    return ", ".join(cols), missing_req, missing_opt


class MappingError(Exception):
    pass


def run_mapping(mapping_dir: Path | str, export_dir: Path | str, out_dir: Path | str,
                schema: Schema | None = None, memory_limit: str = "4GB", threads: int | None = None,
                log=print) -> dict[str, Any]:
    """Execute a mapping and write SIM Parquet files plus ``sim_manifest.json``. Returns a run summary."""
    schema = schema or load_schema()
    export_dir, out_dir = Path(export_dir).resolve(), Path(out_dir).resolve()
    mapping = load_mapping(mapping_dir, export_dir)
    if mapping.sim_version != schema.version:
        raise MappingError(f"mapping targets SIM {mapping.sim_version}, schema is {schema.version}")
    unknown = [t for t in mapping.tables if t not in schema.tables]
    if unknown:
        raise MappingError(f"mapping lists unknown SIM tables: {unknown}")

    if out_dir.exists():
        for t in schema.tables:
            shutil.rmtree(out_dir / t, ignore_errors=True)
        (out_dir / "sim_manifest.json").unlink(missing_ok=True)
    con = sandboxed_connection([export_dir], [out_dir], memory_limit=memory_limit, threads=threads)

    con.execute("CREATE SCHEMA src")
    for s in mapping.sources.values():
        con.execute(f"CREATE VIEW src.{quote_ident(s.name)} AS {_source_view_sql(export_dir, s)}")

    man = mapping.manifest
    con.execute(
        "CREATE TABLE manifest AS SELECT "
        f"CAST({quote_str(str(man['reference_date']))} AS DATE) AS reference_date, "
        f"{quote_str(man['reporting_currency'])} AS reporting_currency, "
        f"{quote_str(man['reporting_entity_id'])} AS reporting_entity_id")

    summary: dict[str, Any] = {"mapping_release": mapping.release_id(), "tables": {}}
    for tname in mapping.tables:
        table = schema.tables[tname]
        user_sql = mapping.sql_for(tname).strip().rstrip(";")
        try:
            rel = con.sql(f"SELECT * FROM ({user_sql}) LIMIT 0")
        except Exception as e:  # noqa: BLE001 - surface the DuckDB message
            raise MappingError(f"{tname}.sql: {e}") from e
        available = rel.columns
        extra = [c for c in available if c not in table.columns]
        if extra:
            raise MappingError(f"{tname}.sql returns columns not in the SIM schema: {extra}")
        proj, missing_req, missing_opt = _projection(schema, table, available)
        if missing_req:
            raise MappingError(f"{tname}.sql does not return required columns: {missing_req}")
        order = ", ".join(quote_ident(k) for k in table.primary_key)
        try:
            con.execute(f"CREATE TABLE {quote_ident(tname)} AS SELECT {proj} FROM ({user_sql}) ORDER BY {order}")
        except Exception as e:  # noqa: BLE001
            raise MappingError(f"{tname}.sql failed: {e}") from e
        n = con.execute(f"SELECT count(*) FROM {quote_ident(tname)}").fetchone()[0]
        target = out_dir / tname
        target.mkdir(parents=True, exist_ok=True)
        if table.partition_by:
            parts = ", ".join(quote_ident(p) for p in table.partition_by)
            con.execute(f"COPY {quote_ident(tname)} TO {quote_str(str(target))} "
                        f"(FORMAT parquet, PARTITION_BY ({parts}), WRITE_PARTITION_COLUMNS true, OVERWRITE_OR_IGNORE true)")
        else:
            con.execute(f"COPY {quote_ident(tname)} TO {quote_str(str(target / 'part-0.parquet'))} (FORMAT parquet)")
        summary["tables"][tname] = {"rows": n, "missing_optional_columns": missing_opt}
        log(f"  {tname:28s} {n:>10,d} rows" + (f"  (unmapped optional: {len(missing_opt)})" if missing_opt else ""))

    manifest = {
        "sim_version": schema.version,
        "reference_date": str(man["reference_date"]),
        "reporting_currency": man["reporting_currency"],
        "reporting_entity_id": man["reporting_entity_id"],
        "mapping_release": summary["mapping_release"],
        "source_fingerprint": source_fingerprint(export_dir, mapping),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (out_dir / "sim_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    summary["manifest"] = manifest
    return summary


def source_fingerprint(export_dir: Path, mapping: Mapping) -> str:
    """Fingerprint of the source files a mapping reads: relative names and sizes (fast, content-agnostic)."""
    h = hashlib.sha256()
    for s in sorted(mapping.sources.values(), key=lambda s: s.name):
        for p in sorted(export_dir.glob(s.path)):
            h.update(str(p.relative_to(export_dir)).encode())
            h.update(str(p.stat().st_size).encode())
    return h.hexdigest()[:16]
