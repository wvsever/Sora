#!/usr/bin/env python3
"""Replicate a SIM dataset N times into a new SIM directory (benchmark datasets, plans/07_benchmarking.md).

Every business key (columns of SIM type `key`: exposure_id, counterparty_id, collateral_id, guarantee_id,
group_id and the foreign keys to them) gets a replica suffix `~<r>`, so referential integrity holds within
each replica. Replica 0 keeps the original keys, so a 1x "scaled" copy equals the source. Entity ids stay
unchanged (the replicas are more business in the same legal entities). `sim_entity` and `sim_fx_rate` are
copied unchanged. `sim_risk_parameter` rows at level `exposure` are replicated with the suffixed key;
segment-level rows are kept once. LEIs are kept for replica 0 only (NULL for the others) so they stay unique.

Output is Parquet, partitioned like the source (PARTITION_BY the schema's `partition_by`, i.e. entity_id),
plus `sim_manifest.json` with an added `scale` field. Everything is done in DuckDB SQL, streamed, with a
memory limit and spilling to a temporary directory.

    python tools/scale_sim.py build/sim/20260630 build/sim/20260630-x10 --factor 10
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import duckdb
import yaml

REPO = Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO / "schemas" / "sim" / "tables"
UNCHANGED = {"sim_entity", "sim_fx_rate"}
ENTITY_KEYS = {"entity_id", "parent_entity_id"}
SUFFIX = "~"


def quote_ident(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def quote_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def load_tables() -> dict[str, dict]:
    return {p.stem: yaml.safe_load(p.read_text()) for p in sorted(SCHEMA_DIR.glob("*.yaml"))}


def source_of(table_dir: Path) -> str:
    if any(table_dir.rglob("*.parquet")):
        return (f"read_parquet({quote_str(str(table_dir / '**' / '*.parquet'))}, "
                "hive_partitioning = false, union_by_name = true)")
    if any(table_dir.rglob("*.csv")):
        # CSV SIM: typed by DuckDB's sniffer is not safe for keys; read as VARCHAR, Parquet out keeps VARCHAR.
        return (f"read_csv({quote_str(str(table_dir / '**' / '*.csv'))}, header = true, all_varchar = true, "
                "hive_partitioning = false, union_by_name = true)")
    raise SystemExit(f"no data files in {table_dir}")


def projection(con: duckdb.DuckDBPyConnection, src: str, table: str, spec: dict | None) -> tuple[str, bool]:
    """SELECT list for replica r (column `r` from range()); returns (select list, replicate?)."""
    cols = [c[0] for c in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()]
    if table in UNCHANGED:
        return ", ".join(quote_ident(c) for c in cols), False
    types = {n: (d or {}).get("type") for n, d in ((spec or {}).get("columns") or {}).items()}
    out = []
    for c in cols:
        q = quote_ident(c)
        if table == "sim_risk_parameter" and c == "key":
            out.append(f"CASE WHEN r = 0 OR level <> 'exposure' THEN {q} ELSE {q} || '{SUFFIX}' || r END AS {q}")
        elif types.get(c) == "key" and c not in ENTITY_KEYS:
            out.append(f"CASE WHEN r = 0 OR {q} IS NULL THEN {q} ELSE {q} || '{SUFFIX}' || r END AS {q}")
        elif table == "sim_counterparty" and c == "lei":
            out.append(f"CASE WHEN r = 0 THEN {q} END AS {q}")
        else:
            out.append(q)
    return ", ".join(out), True


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("source", type=Path, help="source SIM directory")
    p.add_argument("output", type=Path, help="new SIM directory (replaced if it exists)")
    p.add_argument("--factor", type=int, required=True, help="number of replicas (1 = copy)")
    p.add_argument("--memory-limit", default="4GB")
    p.add_argument("--threads", type=int, default=0)
    args = p.parse_args()
    if args.factor < 1:
        raise SystemExit("--factor must be >= 1")
    if not (args.source / "sim_manifest.json").exists():
        raise SystemExit(f"{args.source}: not a SIM directory (no sim_manifest.json)")
    if args.output.resolve() == args.source.resolve():
        raise SystemExit("output must differ from source")

    specs = load_tables()
    if args.output.exists():
        shutil.rmtree(args.output)
    args.output.mkdir(parents=True)
    tmp = args.output / ".tmp"
    con = duckdb.connect()
    con.execute(f"SET memory_limit = {quote_str(args.memory_limit)}")
    con.execute(f"SET temp_directory = {quote_str(str(tmp))}")
    con.execute("SET preserve_insertion_order = false")
    if args.threads:
        con.execute(f"SET threads = {args.threads}")

    t_all = time.perf_counter()
    rows: dict[str, int] = {}
    for table_dir in sorted(d for d in args.source.iterdir() if d.is_dir() and d.name.startswith("sim_")):
        table = table_dir.name
        spec = specs.get(table)
        if spec is None:
            print(f"  warning: {table} is not in the SIM schema; copied unchanged", file=sys.stderr)
        src = source_of(table_dir)
        select, replicate = projection(con, src, table, spec)
        query = (f"SELECT {select} FROM {src}, range({args.factor}) AS rep(r)" if replicate
                 else f"SELECT {select} FROM {src}")
        if table == "sim_risk_parameter":   # segment rows once, exposure rows per replica
            query += " WHERE r = 0 OR level = 'exposure'"
        target = args.output / table
        parts = (spec or {}).get("partition_by") or []
        t0 = time.perf_counter()
        if parts:
            target.mkdir()
            con.execute(f"COPY ({query}) TO {quote_str(str(target))} (FORMAT parquet, PARTITION_BY "
                        f"({', '.join(quote_ident(c) for c in parts)}), WRITE_PARTITION_COLUMNS true)")
        else:
            target.mkdir()
            con.execute(f"COPY ({query}) TO {quote_str(str(target / 'part-0.parquet'))} (FORMAT parquet)")
        rows[table] = con.execute(f"SELECT count(*) FROM read_parquet({quote_str(str(target / '**' / '*.parquet'))})").fetchone()[0]
        print(f"  {table:28s} {rows[table]:>12,d} rows  {time.perf_counter() - t0:6.1f} s")

    manifest = json.loads((args.source / "sim_manifest.json").read_text())
    manifest["scale"] = {"factor": args.factor, "source": str(args.source), "key_suffix": f"{SUFFIX}<replica>",
                         "tool": "tools/scale_sim.py", "rows": rows}
    (args.output / "sim_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    con.close()
    shutil.rmtree(tmp, ignore_errors=True)
    size = sum(f.stat().st_size for f in args.output.rglob("*") if f.is_file())
    print(f"{args.output}: {args.factor}x, {size / 1e6:,.0f} MB, {time.perf_counter() - t_all:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
