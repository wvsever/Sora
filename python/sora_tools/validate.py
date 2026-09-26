"""Validate a SIM dataset against the schema.

Checks, in order:

1. Manifest: present, required fields, SIM version.
2. Structure: required tables present, columns present, parseable types.
3. Column rules from the schema: required, enum, pattern, range.
4. Primary keys unique, foreign keys resolvable.
5. Row checks (``checks``) and table checks (``table_checks``) from the schema.
6. Built-in dataset checks: FX coverage at the reference date, reporting entity exists.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .duck import quote_ident, quote_str, sandboxed_connection
from .schema import Schema, Table, load_schema

MODULE_TABLES = {
    "core": {"sim_entity", "sim_counterparty", "sim_exposure", "sim_fx_rate"},
    "credit": {"sim_rating", "sim_collateral", "sim_collateral_allocation", "sim_guarantee"},
    "parameters": {"sim_risk_parameter"},
    "calibration": {"sim_stage_history", "sim_credit_event", "sim_recovery_flow"},
}
REQUIRED_TABLES = MODULE_TABLES["core"]


@dataclass
class Finding:
    check_id: str
    severity: str          # error | warning | info
    table: str
    message: str
    column: str | None = None
    count: int = 0
    samples: list[str] = field(default_factory=list)


@dataclass
class Report:
    sim_dir: str
    sim_version: str | None
    findings: list[Finding] = field(default_factory=list)
    row_counts: dict[str, int] = field(default_factory=dict)

    @property
    def errors(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    @property
    def warnings(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warning")

    @property
    def ok(self) -> bool:
        return self.errors == 0

    def to_json(self) -> str:
        d = asdict(self)
        d["summary"] = {"errors": self.errors, "warnings": self.warnings, "ok": self.ok}
        return json.dumps(d, indent=2)

    def to_text(self) -> str:
        lines = [f"SIM dataset {self.sim_dir} (SIM {self.sim_version})", ""]
        for t, n in self.row_counts.items():
            lines.append(f"  {t:28s} {n:>12,d} rows")
        lines.append("")
        for f in sorted(self.findings, key=lambda f: ({"error": 0, "warning": 1}.get(f.severity, 2), f.table, f.check_id)):
            where = f.table + (f".{f.column}" if f.column else "")
            cnt = f" ({f.count:,d} rows)" if f.count else ""
            lines.append(f"  {f.severity.upper():7s} {f.check_id:14s} {where}: {f.message}{cnt}")
            if f.samples:
                lines.append(f"          e.g. {', '.join(f.samples)}")
        lines.append("")
        lines.append(f"Result: {'PASSED' if self.ok else 'FAILED'} - {self.errors} errors, {self.warnings} warnings")
        return "\n".join(lines)


def _files(sim_dir: Path, table: str) -> tuple[str | None, str | None]:
    d = sim_dir / table
    if not d.is_dir():
        return None, None
    if any(d.rglob("*.parquet")):
        return "parquet", str(d / "**" / "*.parquet")
    if any(d.rglob("*.csv")):
        return "csv", str(d / "**" / "*.csv")
    return None, None


class Validator:
    def __init__(self, sim_dir: Path | str, schema: Schema | None = None, modules: list[str] | None = None,
                 samples: int = 5, memory_limit: str = "4GB"):
        self.sim_dir = Path(sim_dir).resolve()
        self.schema = schema or load_schema()
        self.modules = modules or ["core", "credit"]
        self.samples = samples
        self.con = sandboxed_connection([self.sim_dir], memory_limit=memory_limit)
        self.report = Report(sim_dir=str(self.sim_dir), sim_version=None)
        self.present: set[str] = set()
        self.manifest: dict[str, Any] = {}

    # -- helpers -------------------------------------------------------------------------------
    def add(self, check_id, severity, table, message, column=None, count=0, samples=None):
        self.report.findings.append(Finding(check_id, severity, table, message, column, count, samples or []))

    def _count_and_samples(self, table: Table, where: str) -> tuple[int, list[str]]:
        t = quote_ident(table.name)
        n = self.con.execute(f"SELECT count(*) FROM {t} WHERE {where}").fetchone()[0]
        samples: list[str] = []
        if n and self.samples:
            key = " || '|' || ".join(f"coalesce(CAST({quote_ident(k)} AS VARCHAR), '∅')" for k in table.primary_key)
            samples = [r[0] for r in self.con.execute(
                f"SELECT {key} FROM {t} WHERE {where} LIMIT {self.samples}").fetchall()]
        return n, samples

    # -- steps ---------------------------------------------------------------------------------
    def check_manifest(self):
        p = self.sim_dir / "sim_manifest.json"
        if not p.is_file():
            self.add("MAN-001", "error", "sim_manifest", "sim_manifest.json is missing")
            return
        try:
            self.manifest = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            self.add("MAN-002", "error", "sim_manifest", f"invalid JSON: {e}")
            return
        self.report.sim_version = self.manifest.get("sim_version")
        for name, spec in self.schema.manifest["fields"].items():
            if spec.get("required") and not self.manifest.get(name):
                self.add("MAN-003", "error", "sim_manifest", f"field {name!r} is missing")
        if self.manifest.get("sim_version") and self.manifest["sim_version"] != self.schema.version:
            self.add("MAN-004", "error", "sim_manifest",
                     f"dataset is SIM {self.manifest['sim_version']}, validator schema is {self.schema.version}")

    def load_tables(self):
        needed = set().union(*(MODULE_TABLES[m] for m in self.modules)) | REQUIRED_TABLES
        for tname, table in self.schema.tables.items():
            fmt, glob = _files(self.sim_dir, tname)
            if fmt is None:
                if tname in needed:
                    self.add("STR-001", "error", tname, "table is missing (required by modules "
                             + ", ".join(m for m in self.modules if tname in MODULE_TABLES.get(m, ())) + ")")
                # Create an empty typed table so cross-table checks still compile.
                cols = ", ".join(f"{quote_ident(c.name)} {self.schema.storage_type(c)}" for c in table.columns.values())
                self.con.execute(f"CREATE TABLE {quote_ident(tname)} ({cols})")
                continue
            reader = (f"read_parquet({quote_str(glob)}, hive_partitioning = false, union_by_name = true)" if fmt == "parquet"
                      else f"read_csv({quote_str(glob)}, header = true, all_varchar = true, hive_partitioning = false, union_by_name = true)")
            self.con.execute(f"CREATE VIEW {quote_ident('raw_' + tname)} AS SELECT * FROM {reader}")
            available = self.con.sql(f"SELECT * FROM {quote_ident('raw_' + tname)} LIMIT 0").columns
            extra = [c for c in available if c not in table.columns]
            if extra:
                self.add("STR-002", "warning", tname, f"columns not in the schema are ignored: {', '.join(extra)}")
            select = []
            for c in table.columns.values():
                typ = self.schema.storage_type(c)
                q = quote_ident(c.name)
                if c.name in available:
                    select.append(f"TRY_CAST({q} AS {typ}) AS {q}")
                    bad = self.con.execute(
                        f"SELECT count(*) FROM {quote_ident('raw_' + tname)} "
                        f"WHERE {q} IS NOT NULL AND TRY_CAST({q} AS {typ}) IS NULL").fetchone()[0]
                    if bad:
                        self.add("STR-003", "error", tname, f"values not convertible to {typ}", c.name, bad)
                else:
                    select.append(f"CAST(NULL AS {typ}) AS {q}")
                    self.add("STR-004", "error" if c.required else "info", tname,
                             "column is missing" + ("" if c.required else " (optional)"), c.name)
            self.con.execute(f"CREATE TABLE {quote_ident(tname)} AS SELECT {', '.join(select)} "
                             f"FROM {quote_ident('raw_' + tname)}")
            self.present.add(tname)
            self.report.row_counts[tname] = self.con.execute(f"SELECT count(*) FROM {quote_ident(tname)}").fetchone()[0]

    def check_columns(self, table: Table):
        for c in table.columns.values():
            q = quote_ident(c.name)
            rules: list[tuple[str, str, str]] = []
            if c.required:
                rules.append(("COL-REQ", f"{q} IS NULL", "required value is NULL"))
            values = self.schema.enum_values(c)
            if values:
                lst = ", ".join(quote_str(v) for v in values)
                rules.append(("COL-ENUM", f"{q} IS NOT NULL AND {q} NOT IN ({lst})",
                              "value not in code list " + (c.enum or str(list(values)))))
            pat = self.schema.pattern(c)
            if pat:
                rules.append(("COL-PATTERN", f"{q} IS NOT NULL AND NOT regexp_full_match({q}, {quote_str(pat)})",
                              f"value does not match {pat}"))
            max_len = self.schema.types.get(c.type, {}).get("max_length")
            if max_len:
                rules.append(("COL-LENGTH", f"{q} IS NOT NULL AND length({q}) > {int(max_len)}",
                              f"longer than {max_len} characters"))
            rng = self.schema.value_range(c)
            if rng:
                lo, hi = rng
                conds = ([f"{q} < {lo}"] if lo is not None else []) + ([f"{q} > {hi}"] if hi is not None else [])
                if conds:
                    rules.append(("COL-RANGE", f"{q} IS NOT NULL AND ({' OR '.join(conds)})",
                                  f"value outside [{lo if lo is not None else '-inf'}, {hi if hi is not None else 'inf'}]"))
            for cid, where, msg in rules:
                n, samples = self._count_and_samples(table, where)
                if n:
                    self.add(cid, "error", table.name, msg, c.name, n, samples)

    def check_keys(self, table: Table):
        t = quote_ident(table.name)
        pk = ", ".join(quote_ident(k) for k in table.primary_key)
        dup = self.con.execute(
            f"SELECT count(*), list(k)[:{self.samples}] FROM (SELECT {pk}, CAST(({pk}) AS VARCHAR) AS k "
            f"FROM {t} GROUP BY ALL HAVING count(*) > 1)").fetchone()
        if dup[0]:
            self.add("KEY-PK", "error", table.name, f"duplicate primary key ({', '.join(table.primary_key)})",
                     count=dup[0], samples=list(dup[1] or []))
        for fk in table.foreign_keys:
            if fk.references not in self.present:
                continue
            on = " AND ".join(f"s.{quote_ident(a)} = r.{quote_ident(b)}" for a, b in zip(fk.columns, fk.ref_columns))
            notnull = " AND ".join(f"s.{quote_ident(a)} IS NOT NULL" for a in fk.columns)
            where = (f"{notnull} AND NOT EXISTS (SELECT 1 FROM {quote_ident(fk.references)} r WHERE {on})")
            n, samples = self._count_and_samples_alias(table, where)
            if n:
                self.add("KEY-FK", "error", table.name,
                         f"({', '.join(fk.columns)}) not found in {fk.references}", ", ".join(fk.columns), n, samples)

    def _count_and_samples_alias(self, table: Table, where: str) -> tuple[int, list[str]]:
        t = quote_ident(table.name)
        n = self.con.execute(f"SELECT count(*) FROM {t} s WHERE {where}").fetchone()[0]
        samples: list[str] = []
        if n and self.samples:
            key = " || '|' || ".join(f"coalesce(CAST(s.{quote_ident(k)} AS VARCHAR), '∅')" for k in table.primary_key)
            samples = [r[0] for r in self.con.execute(f"SELECT {key} FROM {t} s WHERE {where} LIMIT {self.samples}").fetchall()]
        return n, samples

    def check_rules(self, table: Table):
        for ck in table.checks:
            n, samples = self._count_and_samples(table, f"NOT coalesce(({ck.sql}), true)")
            if n:
                self.add(ck.id, ck.severity, table.name, ck.description, count=n, samples=samples)
        for ck in table.table_checks:
            rows = self.con.execute(f"SELECT * FROM ({ck.violations_sql}) v").fetchall()
            if rows:
                self.add(ck.id, ck.severity, table.name, ck.description, count=len(rows),
                         samples=[str(r[0]) for r in rows[:self.samples]])

    def check_dataset(self):
        ref = self.manifest.get("reference_date")
        rep = self.manifest.get("reporting_currency")
        if ref and "sim_fx_rate" in self.present:
            missing = self.con.execute(f"""
                WITH used AS (
                    SELECT currency FROM sim_exposure UNION SELECT currency FROM sim_collateral
                    UNION SELECT currency FROM sim_guarantee)
                SELECT list(currency ORDER BY currency) FROM used
                WHERE currency IS NOT NULL AND currency <> {quote_str(rep or '')} AND currency NOT IN
                    (SELECT currency FROM sim_fx_rate WHERE rate_date = CAST({quote_str(ref)} AS DATE))""").fetchone()[0]
            if missing:
                self.add("DS-FX", "error", "sim_fx_rate", f"no rate at reference date {ref} for: {', '.join(missing)}",
                         count=len(missing))
        ent = self.manifest.get("reporting_entity_id")
        if ent and "sim_entity" in self.present:
            if not self.con.execute(f"SELECT count(*) FROM sim_entity WHERE entity_id = {quote_str(ent)}").fetchone()[0]:
                self.add("DS-ENT", "error", "sim_entity", f"reporting entity {ent} not found")

    def run(self) -> Report:
        self.check_manifest()
        self.load_tables()
        for tname in self.schema.tables:
            if tname in self.present:
                table = self.schema.tables[tname]
                self.check_columns(table)
                self.check_keys(table)
                self.check_rules(table)
        self.check_dataset()
        return self.report


def validate(sim_dir: Path | str, modules: list[str] | None = None, schema: Schema | None = None,
             samples: int = 5) -> Report:
    return Validator(sim_dir, schema=schema, modules=modules, samples=samples).run()
