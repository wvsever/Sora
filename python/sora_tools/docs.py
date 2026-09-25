"""Generate documentation and DDL from the SIM schema.

* ``markdown`` - full human-readable reference.
* ``llm`` - compact, complete plain-text description for an AI agent's context (used by sora-mcp).
* ``ddl`` - CREATE TABLE statements (DuckDB / PostgreSQL dialect) for customers who map in their warehouse.
"""

from __future__ import annotations

from .schema import Column, Schema, Table


def _type_label(schema: Schema, c: Column) -> str:
    if c.type == "enum":
        return f"enum `{c.enum}`"
    return f"{c.type} ({schema.storage_type(c)})"


def markdown(schema: Schema) -> str:
    out = [f"# Sora Input Model (SIM) {schema.version}", "", schema.description, "", "## Conventions", ""]
    out += [f"- {c}" for c in schema.conventions]
    out += ["", "## Types", "", "| Type | Storage | Description |", "|---|---|---|"]
    for name, t in schema.types.items():
        desc = t["description"] + (f" CSV: {t['csv_format']}" if t.get("csv_format") else "")
        out.append(f"| `{name}` | `{t['storage']}` | {desc} |")
    out += ["", "## Tables", ""]
    for t in schema.tables.values():
        out.append(f"- [`{t.name}`](#{t.name}): {t.description.split('. ')[0].rstrip('.')}.")
    for t in schema.tables.values():
        out += _table_md(schema, t)
    out += ["", "## Code lists", ""]
    for name, e in schema.enums.items():
        out += [f"### `{name}`", "", str(e["description"]).strip(), "", "| Value | Meaning |", "|---|---|"]
        out += [f"| `{v}` | {d} |" for v, d in e["values"].items()]
        out.append("")
    out += ["## Manifest (`sim_manifest.json`)", "", "| Field | Type | Required | Description |", "|---|---|---|---|"]
    for name, f in schema.manifest["fields"].items():
        out.append(f"| `{name}` | {f['type']} | {'yes' if f.get('required') else 'no'} | {f['description']} |")
    return "\n".join(out) + "\n"


def _table_md(schema: Schema, t: Table) -> list[str]:
    out = ["", f"### {t.name}", "", t.description, "",
           f"- **Grain:** {t.grain}",
           f"- **Primary key:** {', '.join(f'`{k}`' for k in t.primary_key)}"]
    if t.partition_by:
        out.append(f"- **Partitioned by:** {', '.join(f'`{k}`' for k in t.partition_by)}")
    if t.required_by:
        out.append(f"- **Required by modules:** {', '.join(t.required_by)}")
    for fk in t.foreign_keys:
        out.append(f"- **Foreign key:** ({', '.join(fk.columns)}) → `{fk.references}`")
    out += ["", "| Column | Type | Req. | Description | Example |", "|---|---|---|---|---|"]
    for c in t.columns.values():
        desc = c.description
        if c.allowed:
            desc += f" Allowed: {', '.join(f'`{a}`' for a in c.allowed)}."
        if c.reg_ref:
            desc += f" *Ref: {c.reg_ref}*"
        if c.pitfalls:
            desc += f" **Pitfall:** {c.pitfalls}"
        out.append(f"| `{c.name}` | {_type_label(schema, c)} | {'yes' if c.required else ''} | "
                   f"{desc.replace('|', '/')} | `{c.example}` |")
    checks = [*t.checks, *t.table_checks]
    if checks:
        out += ["", "Checks:", ""]
        out += [f"- `{ck.id}` ({ck.severity}): {ck.description}" for ck in checks]
    return out


def llm(schema: Schema) -> str:
    """Compact but complete description. Every column, type, rule and code list, no tables or decoration."""
    out = [f"SORA INPUT MODEL (SIM) {schema.version}", schema.description, "", "CONVENTIONS:"]
    out += [f"- {c}" for c in schema.conventions]
    out += ["", "TYPES:"]
    for name, t in schema.types.items():
        out.append(f"- {name}: {t['storage']}. {t['description']}" + (f" CSV: {t['csv_format']}" if t.get("csv_format") else ""))
    for t in schema.tables.values():
        out += ["", f"TABLE {t.name}", f"  {t.description}", f"  grain: {t.grain}",
                f"  primary key: {', '.join(t.primary_key)}"]
        for fk in t.foreign_keys:
            out.append(f"  foreign key: {', '.join(fk.columns)} -> {fk.references}({', '.join(fk.ref_columns)})")
        out.append("  columns:")
        for c in t.columns.values():
            typ = f"enum {c.enum}" if c.type == "enum" else c.type
            extra = []
            if c.allowed:
                extra.append(f"allowed {list(c.allowed)}")
            if c.pattern:
                extra.append(f"pattern {c.pattern}")
            if c.range:
                extra.append(f"range {list(c.range)}")
            if c.reg_ref:
                extra.append(f"ref: {c.reg_ref}")
            if c.pitfalls:
                extra.append(f"pitfall: {c.pitfalls}")
            out.append(f"  - {c.name} [{typ}, {'required' if c.required else 'optional'}] {c.description}"
                       + (f" ({'; '.join(extra)})" if extra else "") + f" e.g. {c.example}")
        for ck in (*t.checks, *t.table_checks):
            rule = ck.sql or ck.violations_sql.strip().replace("\n", " ")
            out.append(f"  check {ck.id} ({ck.severity}): {ck.description} [{rule}]")
    out += ["", "CODE LISTS:"]
    for name, e in schema.enums.items():
        out.append(f"- {name}: " + str(e["description"]).strip())
        out += [f"    {v}: {d}" for v, d in e["values"].items()]
    return "\n".join(out) + "\n"


def ddl(schema: Schema) -> str:
    out = [f"-- Sora Input Model (SIM) {schema.version}. Generated from schemas/sim - do not edit.", ""]
    for t in schema.tables.values():
        out.append(f"CREATE TABLE {t.name} (")
        lines = [f"    {c.name} {schema.storage_type(c)}{' NOT NULL' if c.required else ''}"
                 for c in t.columns.values()]
        lines.append(f"    PRIMARY KEY ({', '.join(t.primary_key)})")
        out.append(",\n".join(lines))
        out += [");", ""]
    return "\n".join(out)
