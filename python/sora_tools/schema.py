"""Load, lint and interpret the Sora Input Model (SIM) schema in ``schemas/sim``."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SCHEMA_ENV = "SORA_SIM_SCHEMA"


@dataclass(frozen=True)
class Column:
    name: str
    type: str                      # logical type name, or "enum"
    required: bool
    description: str
    enum: str | None = None        # shared enum name when type == "enum"
    allowed: tuple[str, ...] = ()  # inline allowed values
    pattern: str | None = None
    range: tuple[float | None, float | None] | None = None
    example: str | None = None
    reg_ref: str | None = None
    pitfalls: str | None = None


@dataclass(frozen=True)
class ForeignKey:
    columns: tuple[str, ...]
    references: str
    ref_columns: tuple[str, ...]


@dataclass(frozen=True)
class Check:
    id: str
    severity: str        # error | warning
    description: str
    sql: str | None = None             # row predicate that must hold (NULL result = pass)
    violations_sql: str | None = None  # query returning the key of each violating row


@dataclass
class Table:
    name: str
    description: str
    grain: str
    primary_key: tuple[str, ...]
    columns: dict[str, Column]
    partition_by: tuple[str, ...] = ()
    required_by: tuple[str, ...] = ()
    foreign_keys: tuple[ForeignKey, ...] = ()
    checks: tuple[Check, ...] = ()
    table_checks: tuple[Check, ...] = ()


@dataclass
class Schema:
    version: str
    description: str
    conventions: list[str]
    types: dict[str, dict[str, Any]]
    enums: dict[str, dict[str, Any]]
    tables: dict[str, Table]
    manifest: dict[str, Any]
    root: Path = field(default=Path("."))

    def enum_values(self, column: Column) -> tuple[str, ...]:
        if column.enum:
            return tuple(self.enums[column.enum]["values"].keys())
        return column.allowed

    def storage_type(self, column: Column) -> str:
        if column.type == "enum":
            return "VARCHAR"
        return self.types[column.type]["storage"]

    def pattern(self, column: Column) -> str | None:
        if column.pattern:
            return column.pattern
        if column.type in self.types:
            return self.types[column.type].get("pattern")
        return None

    def value_range(self, column: Column) -> tuple[float | None, float | None] | None:
        if column.range:
            return column.range
        if column.type in self.types and "range" in self.types[column.type]:
            lo, hi = self.types[column.type]["range"]
            return (lo, hi)
        return None


def find_schema_dir(start: Path | None = None) -> Path:
    """Locate ``schemas/sim``: $SORA_SIM_SCHEMA, then parents of ``start``/cwd, then the package's repo."""
    env = os.environ.get(SCHEMA_ENV)
    if env:
        return Path(env)
    candidates = [start or Path.cwd(), Path(__file__).resolve().parent]
    for base in candidates:
        for d in [base, *base.parents]:
            p = d / "schemas" / "sim" / "sim.yaml"
            if p.is_file():
                return p.parent
    raise FileNotFoundError(f"schemas/sim not found; set {SCHEMA_ENV}")


def _tuple(v: Any) -> tuple:
    if v is None:
        return ()
    return tuple(v) if isinstance(v, (list, tuple)) else (v,)


def _check(d: dict[str, Any]) -> Check:
    return Check(id=d["id"], severity=d.get("severity", "error"), description=d["description"],
                 sql=d.get("sql"), violations_sql=d.get("violations_sql"))


def _column(name: str, d: dict[str, Any]) -> Column:
    rng = d.get("range")
    return Column(
        name=name,
        type=d["type"],
        required=bool(d.get("required", False)),
        description=str(d.get("description", "")).strip(),
        enum=d.get("enum"),
        allowed=_tuple(d.get("allowed")),
        pattern=d.get("pattern"),
        range=(rng[0], rng[1]) if rng else None,
        example=None if d.get("example") is None else str(d["example"]),
        reg_ref=d.get("reg_ref"),
        pitfalls=d.get("pitfalls"),
    )


def _table(d: dict[str, Any]) -> Table:
    return Table(
        name=d["name"],
        description=str(d["description"]).strip(),
        grain=str(d["grain"]).strip(),
        primary_key=_tuple(d["primary_key"]),
        columns={n: _column(n, c) for n, c in d["columns"].items()},
        partition_by=_tuple(d.get("partition_by")),
        required_by=_tuple(d.get("required_by")),
        foreign_keys=tuple(ForeignKey(_tuple(f["columns"]), f["references"], _tuple(f["ref_columns"]))
                           for f in d.get("foreign_keys", [])),
        checks=tuple(_check(c) for c in d.get("checks", [])),
        table_checks=tuple(_check(c) for c in d.get("table_checks", [])),
    )


def load_schema(path: Path | str | None = None) -> Schema:
    root = Path(path) if path else find_schema_dir()
    meta = yaml.safe_load((root / "sim.yaml").read_text())
    tables = {}
    for name in meta["tables"]:
        tables[name] = _table(yaml.safe_load((root / "tables" / f"{name}.yaml").read_text()))
    return Schema(
        version=meta["sim_version"],
        description=str(meta["description"]).strip(),
        conventions=list(meta.get("conventions", [])),
        types=meta["types"],
        enums=meta["enums"],
        tables=tables,
        manifest=meta["manifest"],
        root=root,
    )


_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")


def lint(schema: Schema) -> list[str]:
    """Return a list of schema problems (empty = clean)."""
    problems: list[str] = []
    table_files = {p.stem for p in (schema.root / "tables").glob("*.yaml")}
    for extra in sorted(table_files - set(schema.tables)):
        problems.append(f"tables/{extra}.yaml is not listed in sim.yaml")
    check_ids: set[str] = set()
    for t in schema.tables.values():
        where = t.name
        if not _IDENT.match(t.name):
            problems.append(f"{where}: invalid table name")
        if len(t.description) < 20:
            problems.append(f"{where}: description too short")
        for k in (*t.primary_key, *t.partition_by):
            if k not in t.columns:
                problems.append(f"{where}: key/partition column {k!r} not defined")
            elif not t.columns[k].required:
                problems.append(f"{where}: key/partition column {k!r} must be required")
        for c in t.columns.values():
            cw = f"{where}.{c.name}"
            if not _IDENT.match(c.name):
                problems.append(f"{cw}: invalid column name")
            if c.type == "enum":
                if not c.enum or c.enum not in schema.enums:
                    problems.append(f"{cw}: unknown enum {c.enum!r}")
            elif c.type not in schema.types:
                problems.append(f"{cw}: unknown type {c.type!r}")
            if len(c.description) < 10:
                problems.append(f"{cw}: description missing or too short")
            if c.example is None:
                problems.append(f"{cw}: example missing")
            elif c.type == "enum" and c.enum in schema.enums and c.example not in schema.enum_values(c):
                problems.append(f"{cw}: example {c.example!r} not in enum")
            elif c.allowed and c.example not in c.allowed:
                problems.append(f"{cw}: example {c.example!r} not in allowed values")
            pat = schema.pattern(c) if c.type in schema.types or c.pattern else None
            if pat and c.example is not None and not re.match(pat, c.example):
                problems.append(f"{cw}: example {c.example!r} does not match pattern {pat}")
        for fk in t.foreign_keys:
            if fk.references not in schema.tables:
                problems.append(f"{where}: foreign key references unknown table {fk.references}")
                continue
            ref = schema.tables[fk.references]
            for col in fk.columns:
                if col not in t.columns:
                    problems.append(f"{where}: foreign key column {col!r} not defined")
            for col in fk.ref_columns:
                if col not in ref.columns:
                    problems.append(f"{where}: foreign key target {fk.references}.{col} not defined")
            if tuple(fk.ref_columns) != ref.primary_key:
                problems.append(f"{where}: foreign key must reference the primary key of {fk.references}")
        for ck in (*t.checks, *t.table_checks):
            if ck.id in check_ids:
                problems.append(f"{where}: duplicate check id {ck.id}")
            check_ids.add(ck.id)
            if ck.severity not in ("error", "warning"):
                problems.append(f"{where}: check {ck.id} has invalid severity")
            if (ck.sql is None) == (ck.violations_sql is None):
                problems.append(f"{where}: check {ck.id} needs exactly one of sql / violations_sql")
    for name, e in schema.enums.items():
        if not e.get("values"):
            problems.append(f"enum {name}: no values")
        for v in e.get("values", {}):
            if not re.match(r"^[a-z][a-z0-9_]*$", str(v)):
                problems.append(f"enum {name}: value {v!r} is not lower_snake_case")
    return problems
