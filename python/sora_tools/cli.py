"""``sora-tools`` command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import docs
from .mapping import MappingError, run_mapping
from .schema import lint, load_schema
from .validate import MODULE_TABLES, validate
from .vera_params import VeraParamsError, convert as convert_vera_params


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
        Path(args.output).write_text(text, encoding="utf-8")
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


def cmd_profile(args) -> int:
    from .profile import profile_export
    doc = profile_export(args.export, args.output, max_codes=args.max_codes, list_codes=not args.no_codes,
                         types_file=args.types_file)
    print(f"{len(doc['tables'])} tables profiled -> {args.output}")
    return 0


def cmd_scenario_import(args) -> int:
    from .scenario_import import import_scenarios
    n = import_scenarios(args.workbooks, args.output)
    print(f"{n:,d} scenario values written to {args.output}")
    return 0


def cmd_vera_params(args) -> int:
    try:
        stats = convert_vera_params(args.input, args.output, extras_csv=args.extras,
                                    scenario=args.scenario, year=args.year)
    except VeraParamsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(stats.to_text())
    if args.report:
        Path(args.report).write_text(json.dumps(stats.to_dict(), indent=2) + "\n")
    return 1 if (args.strict and stats.has_range_violations) else 0


def cmd_calculator_stub(args) -> int:
    from .calculator_stub import serve
    server = serve(args.mode, args.host, args.port, args.latency, args.reject)
    print(f"Sora calculator stub ({args.mode}) on http://{args.host}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


# -- results: explain / diff-runs (also sora-mcp tools) ------------------------------------------------------
def cmd_explain(args) -> int:
    from .explain import explain_result
    from .results import ResultError
    try:
        r = explain_result(args.output, args.segment, args.scenario, args.year, top=args.top)
    except ResultError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(json.dumps(r, indent=2) if args.json else r["narrative"])
    return 0


def cmd_diff_runs(args) -> int:
    from .diff_runs import diff_runs, to_json
    from .results import ResultError
    try:
        r = diff_runs(args.run_a, args.run_b, abs_tol=args.abs_tol, rel_tol=args.rel_tol, top=args.top,
                      segment=args.segment, scenario=args.scenario, year=args.year)
    except ResultError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if args.report:
        Path(args.report).write_text(to_json(r) + "\n")
    print(to_json(r) if args.json else r["narrative"])
    return 0 if r["identical"] or not args.fail_on_diff else 1


def _register_results_commands(sub) -> None:
    e = sub.add_parser("explain", help="explain a result: a segment's impairment, or an overview of a run")
    e.add_argument("output", help="engine output directory (sora run -o)")
    e.add_argument("--segment", help="segment key, e.g. 'LOANS|HH_HOUSE|DE' (default: overview)")
    e.add_argument("--scenario", choices=["baseline", "adverse"])
    e.add_argument("--year", type=int)
    e.add_argument("--top", type=int, default=10, help="segments in the overview")
    e.add_argument("--json", action="store_true", help="print the structured result instead of the narrative")
    e.set_defaults(func=cmd_explain)

    d = sub.add_parser("diff-runs", help="compare two engine output directories")
    d.add_argument("run_a")
    d.add_argument("run_b")
    d.add_argument("--abs-tol", type=float, default=0.01, help="absolute tolerance (default 0.01)")
    d.add_argument("--rel-tol", type=float, default=1e-9, help="relative tolerance (default 1e-9)")
    d.add_argument("--top", type=int, default=10, help="top movers (default 10)")
    d.add_argument("--segment")
    d.add_argument("--scenario")
    d.add_argument("--year", type=int)
    d.add_argument("--json", action="store_true", help="print the structured result instead of the narrative")
    d.add_argument("--report", help="write the structured result as JSON")
    d.add_argument("--fail-on-diff", action="store_true", help="exit 1 if the runs differ")
    d.set_defaults(func=cmd_diff_runs)


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

    pr = sub.add_parser("profile", help="profile an export and write a source data dictionary (no data values)")
    pr.add_argument("export", help="directory with the exported source files")
    pr.add_argument("-o", "--output", required=True, help="output YAML")
    pr.add_argument("--max-codes", type=int, default=30, help="list code values for columns with at most N values")
    pr.add_argument("--no-codes", action="store_true", help="never list any values")
    pr.add_argument("--types-file", help="optional column-type JSON inside the export (e.g. _csv_column_types.json)")
    pr.set_defaults(func=cmd_profile)

    si = sub.add_parser("scenario-import", help="convert EBA/ESRB scenario workbooks (xlsx) to a normalised CSV")
    si.add_argument("workbooks", nargs="+", help="macro-financial scenario and/or real GVA workbooks")
    si.add_argument("-o", "--output", required=True)
    si.set_defaults(func=cmd_scenario_import)

    vp = sub.add_parser("vera-params", help="convert Vera's risk_parameters.csv (bcal_cli --out-dir) to sim_risk_parameter")
    vp.add_argument("input", help="Vera's risk_parameters.csv")
    vp.add_argument("-o", "--output", required=True, help="output sim_risk_parameter CSV path")
    vp.add_argument("--extras", help="optional side CSV: is_defaulted/default_date/default_trigger/dpd/ead per contract")
    vp.add_argument("--scenario", default="actual")
    vp.add_argument("--year", type=int, default=0)
    vp.add_argument("--report", help="write the conversion report as JSON")
    vp.add_argument("--strict", action="store_true", help="exit 1 if any PAR-010 pre-check value was dropped")
    vp.set_defaults(func=cmd_vera_params)

    c = sub.add_parser("calculator-stub", help="run the stub regulatory calculator (REST, for tests and demos)")
    c.add_argument("--mode", choices=["fixed", "formula", "faults"], default="formula")
    c.add_argument("--host", default="127.0.0.1")
    c.add_argument("--port", type=int, default=8080)
    c.add_argument("--latency", type=float, default=0.0, help="seconds added to every request")
    c.add_argument("--reject", help="regular expression: reject every record whose recordId it matches")
    c.set_defaults(func=cmd_calculator_stub)

    from .satellites import register as register_satellites
    register_satellites(sub)
    _register_results_commands(sub)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
