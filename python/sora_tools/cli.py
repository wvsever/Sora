"""``sora-tools`` command line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import docs
from .mapping import MappingError, run_mapping
from .schema import lint, load_schema
from .validate import MODULE_TABLES, validate


def _schema(args):
    return load_schema(args.schema)


def cmd_schema(args) -> int:
    schema = _schema(args)
    if args.action == "lint":
        problems = lint(schema)
        for p in problems:
            print(p)
        print(f"SIM {schema.version}: {len(schema.tables)} tables, "
              f"{sum(len(t.columns) for t in schema.tables.values())} columns, {len(problems)} problems")
        return 1 if problems else 0
    text = {"markdown": docs.markdown, "llm": docs.llm, "ddl": docs.ddl}[args.action](schema)
    if args.output:
        Path(args.output).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


def cmd_map(args) -> int:
    print(f"Mapping {args.mapping} on {args.export} -> {args.output}")
    try:
        summary = run_mapping(args.mapping, args.export, args.output, schema=_schema(args),
                              memory_limit=args.memory_limit, threads=args.threads)
    except MappingError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(f"Mapping release {summary['mapping_release']}, manifest written.")
    if args.validate:
        return _report(validate(args.output, schema=_schema(args)), args)
    return 0


def _report(report, args) -> int:
    print(report.to_text())
    if getattr(args, "report", None):
        Path(args.report).write_text(report.to_json())
    return 0 if report.ok else 1


def cmd_validate(args) -> int:
    return _report(validate(args.sim, modules=args.modules, schema=_schema(args), samples=args.samples), args)


def cmd_calculator_stub(args) -> int:
    from .calculator_stub import serve
    server = serve(args.mode, args.host, args.port, args.latency)
    print(f"Sora calculator stub ({args.mode}) on http://{args.host}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sora-tools", description="Sora data tooling")
    p.add_argument("--schema", help="path to schemas/sim (default: auto-detect or $SORA_SIM_SCHEMA)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("schema", help="lint the SIM schema or generate docs / LLM description / DDL")
    s.add_argument("action", choices=["lint", "markdown", "llm", "ddl"])
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_schema)

    m = sub.add_parser("map", help="run mapping SQL on exported source files and write SIM Parquet")
    m.add_argument("mapping", help="mapping directory (mapping.yaml + <sim_table>.sql)")
    m.add_argument("--export", required=True, help="directory with the exported source files")
    m.add_argument("-o", "--output", required=True, help="output SIM directory")
    m.add_argument("--memory-limit", default="4GB")
    m.add_argument("--threads", type=int)
    m.add_argument("--validate", action="store_true", help="validate the SIM output afterwards")
    m.add_argument("--report", help="write the validation report as JSON")
    m.set_defaults(func=cmd_map)

    v = sub.add_parser("validate", help="validate a SIM dataset")
    v.add_argument("sim", help="SIM directory")
    v.add_argument("--modules", nargs="+", choices=sorted(MODULE_TABLES), default=["core", "credit"])
    v.add_argument("--samples", type=int, default=5, help="sample keys per finding (0 = none)")
    v.add_argument("--report", help="write the report as JSON")
    v.set_defaults(func=cmd_validate)

    c = sub.add_parser("calculator-stub", help="run the stub regulatory calculator (REST, for tests and demos)")
    c.add_argument("--mode", choices=["fixed", "formula", "faults"], default="formula")
    c.add_argument("--host", default="127.0.0.1")
    c.add_argument("--port", type=int, default=8080)
    c.add_argument("--latency", type=float, default=0.0, help="seconds added to every request")
    c.set_defaults(func=cmd_calculator_stub)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
