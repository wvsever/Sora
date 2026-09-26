"""``sora-mcp``: a local MCP server (stdio) that lets an AI agent work with Sora.

The agent can read the SIM model description, profile an export, test a mapping, validate a SIM, reconcile
it with source totals, run the engine and explain or compare results. See ``plans/11_integrations.md``.

The tool functions (``SoraTools``) are plain Python and do not need the MCP SDK; ``build_server`` wraps them
with the official ``mcp`` package (``pip install -e "python[mcp]"``, SDK 1.x ``FastMCP`` or 2.x ``MCPServer``).

Security model
--------------
* **Read roots.** Every path argument is resolved (symlinks included) and must lie under a configured root
  (``--root`` / ``SORA_MCP_ROOTS``, ``os.pathsep``-separated) or under the server's work directory. Relative
  paths are relative to the first root. Paths the scenario YAML references are checked the same way.
* **Writes** go only to the work directory (``--workdir`` / ``SORA_MCP_WORKDIR``; default a new temporary
  directory): SIM datasets of ``test_mapping`` under ``sim/``, engine runs under ``runs/``, with names the server
  generates. Tools never take an output path.
* **Metadata only by default.** Tools return schema information, counts, statistics, validation messages,
  totals and segment-level results. Raw values (``profile_source`` sample rows and min/max, validation sample
  keys) need ``include_values=true`` in the call *and* the server setting ``--allow-values`` /
  ``SORA_MCP_ALLOW_VALUES=1``; rows are capped by ``--max-rows`` / ``SORA_MCP_MAX_ROWS`` (default 20).
* **No shell.** The engine (``--engine`` / ``SORA_ENGINE``; the agent cannot choose it) runs with an argument
  list, ``shell=False``, a timeout and resolved absolute paths. Mapping SQL runs in the sandboxed DuckDB
  connection of ``sora-tools map`` (files only, no network, no extensions).
* **Production mappings need human approval.** A mapping is *production* if ``mapping.yaml`` has
  ``status: production`` or its path matches a ``--production-pattern`` glob (``SORA_MCP_PRODUCTION_PATTERNS``;
  default ``*/production/*``). ``test_mapping`` on such a mapping writes nothing and returns
  ``status: requires_approval`` with a request id bound to the mapping release (a hash of all mapping files)
  and the export. A person approves it outside the agent's reach with ``sora-mcp approve <id> --by <name>``
  (the approvals directory, ``--approvals-dir``, must not be inside the work directory). Any change to the
  mapping gives a new release and needs a new approval.
* **Audit.** Every tool call is appended to ``<workdir>/audit.jsonl`` (or ``--audit-log``): time, OS user, tool,
  arguments, their SHA-256 fingerprint, status and duration.
"""

from __future__ import annotations

import argparse
import fnmatch
import getpass
import hashlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any

import yaml

SERVER_NAME = "sora"
DEFAULT_MAX_ROWS = 20
DEFAULT_PRODUCTION_PATTERNS = ("*/production/*",)
SCENARIO_PATH_KEYS = (("macro_path",), ("satellites",), ("starting_parameters",), ("satellite_models",),
                      ("benchmark_parameters", "file"))

OUTPUT_FILES = {
    "segments.csv": "Segments and starting-point stocks: exposure and provisions per stage (EUR).",
    "prior_year.csv": "Stocks per segment at the prior year-end (CR_SCEN/CR_SECTOR prior-year Actual rows; EUR, "
                      "blank where not derivable).",
    "parameters.csv": "Starting-point and projected parameters per segment, scenario and year, with source "
                      "(derived/external/mixed/benchmark) and calibration_levels; with --calculator-parameters also "
                      "exposure rows (actual/0) with the fields taken from the calculator (source calculator).",
    "projection.csv": "Stage flows, exposures, provisions per component (EBA Boxes 3-9) and impairment per "
                      "segment, scenario and year.",
    "collateral.csv": "Static-balance-sheet LTV per segment, scenario, year and t0 stage.",
    "off_balance.csv": "Off-balance items per segment, exposure type, scenario and year.",
    "benchmarks.csv": "ECB benchmark rule per segment: model coverage, rule, benchmark key.",
    "cr_scen.csv": "EBA CSV_CR_SCEN template (EUR million, percent).",
    "cr_scen_off_bs.csv": "EBA CSV_CR_SCEN_OFF_BS template.",
    "cr_sector.csv": "EBA CSV_CR_SECTOR template (NFC by NACE section).",
    "rea.csv": "IRB REA and expected loss per segment, scenario and year (with --calculator).",
    "calculator_parameters.csv": "Credit parameters returned per exposure by the calculator's /v1/parameters/credit "
                                 "(with --calculator-parameters): status, values, and which were used.",
    "summary.json": "Totals per scenario and year, starting point, benchmark and off-balance summaries.",
    "diagnostics.json": "Engine findings (id, severity, count, message).",
}


class ToolError(Exception):
    """A tool call that cannot be served (bad input, missing file). Returned as ``status: error``."""


class PolicyError(ToolError):
    """A call the security policy refuses. Returned as ``status: denied``."""


def _env_list(name: str) -> list[str]:
    v = os.environ.get(name, "")
    return [x for x in v.split(os.pathsep) if x]


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Policy:
    roots: list[Path]
    workdir: Path
    allow_values: bool = False
    max_rows: int = DEFAULT_MAX_ROWS
    production_patterns: tuple[str, ...] = DEFAULT_PRODUCTION_PATTERNS
    approvals_dir: Path | None = None
    engine: Path | None = None
    schema: Path | None = None
    audit_log: Path | None = None
    engine_timeout: float = 3600.0
    user: str = field(default_factory=lambda: _safe_user())

    @classmethod
    def create(cls, roots: list[str | Path] | None = None, workdir: str | Path | None = None,
               allow_values: bool | None = None, max_rows: int | None = None,
               production_patterns: list[str] | None = None, approvals_dir: str | Path | None = None,
               engine: str | Path | None = None, schema: str | Path | None = None,
               audit_log: str | Path | None = None, engine_timeout: float | None = None) -> Policy:
        """Build the policy from arguments, falling back to the ``SORA_MCP_*`` environment variables."""
        roots = list(roots or []) or _env_list("SORA_MCP_ROOTS")
        if not roots:
            raise PolicyError("no read roots configured: pass --root or set SORA_MCP_ROOTS")
        rr = []
        for r in roots:
            p = Path(r).expanduser().resolve()
            if not p.is_dir():
                raise PolicyError(f"root is not a directory: {p}")
            rr.append(p)
        wd = workdir or os.environ.get("SORA_MCP_WORKDIR")
        wdp = Path(wd).expanduser().resolve() if wd else Path(tempfile.mkdtemp(prefix="sora-mcp-")).resolve()
        wdp.mkdir(parents=True, exist_ok=True)
        ad = approvals_dir or os.environ.get("SORA_MCP_APPROVALS_DIR") or (Path.home() / ".sora-mcp" / "approvals")
        adp = Path(ad).expanduser().resolve()
        if adp == wdp or adp.is_relative_to(wdp):
            raise PolicyError("the approvals directory must not be inside the work directory")
        eng = engine or os.environ.get("SORA_ENGINE")
        pats = production_patterns or _env_list("SORA_MCP_PRODUCTION_PATTERNS") or list(DEFAULT_PRODUCTION_PATTERNS)
        return cls(
            roots=rr, workdir=wdp,
            allow_values=_truthy(os.environ.get("SORA_MCP_ALLOW_VALUES")) if allow_values is None else allow_values,
            max_rows=int(max_rows if max_rows is not None else os.environ.get("SORA_MCP_MAX_ROWS", DEFAULT_MAX_ROWS)),
            production_patterns=tuple(pats), approvals_dir=adp,
            engine=Path(eng).expanduser().resolve() if eng else None,
            schema=Path(schema).expanduser().resolve() if schema else None,
            audit_log=Path(audit_log).expanduser().resolve() if audit_log else wdp / "audit.jsonl",
            engine_timeout=float(engine_timeout or os.environ.get("SORA_MCP_ENGINE_TIMEOUT", "3600")),
        )

    # -- paths -------------------------------------------------------------------------------------
    def readable(self, path: str | Path, kind: str = "path", must_exist: bool = True, is_dir: bool | None = None) -> Path:
        s = str(path)
        if not s or "\x00" in s:
            raise PolicyError(f"invalid {kind}")
        p = Path(s).expanduser()
        if not p.is_absolute():
            p = self.roots[0] / p
        p = p.resolve()
        allowed = [*self.roots, self.workdir]
        if not any(p == r or p.is_relative_to(r) for r in allowed):
            raise PolicyError(f"{kind} {p} is outside the allowed roots ({', '.join(map(str, allowed))})")
        if must_exist and not p.exists():
            raise ToolError(f"{kind} not found: {p}")
        if is_dir is True and must_exist and not p.is_dir():
            raise ToolError(f"{kind} is not a directory: {p}")
        if is_dir is False and must_exist and not p.is_file():
            raise ToolError(f"{kind} is not a file: {p}")
        return p

    def new_output(self, kind: str, label: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in label)[:40] or "out"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        p = self.workdir / kind / f"{safe}-{stamp}-{uuid.uuid4().hex[:6]}"
        p.mkdir(parents=True)
        return p

    def rows(self, include_values: bool, max_rows: int | None) -> int:
        """Number of raw rows/values a call may return (0 = metadata only)."""
        if not include_values:
            return 0
        if not self.allow_values:
            raise PolicyError("include_values is disabled on this server (a customer setting: start sora-mcp "
                              "with --allow-values or SORA_MCP_ALLOW_VALUES=1)")
        n = self.max_rows if max_rows is None else int(max_rows)
        return max(0, min(n, self.max_rows))

    def is_production(self, mapping_dir: Path, mapping_doc: dict[str, Any]) -> str | None:
        status = str(mapping_doc.get("status", "draft")).lower()
        if status == "production":
            return "mapping.yaml status: production"
        posix = mapping_dir.as_posix() + "/"
        for pat in self.production_patterns:
            if fnmatch.fnmatch(posix, pat) or fnmatch.fnmatch(mapping_dir.as_posix(), pat):
                return f"path matches production pattern {pat!r}"
        return None


def _safe_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "unknown"


def _fingerprint(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


# -- approvals ------------------------------------------------------------------------------------------
class Approvals:
    """File-based approval store: ``<dir>/<id>.pending.json`` written by the server, ``<id>.approved.json``
    written by a person with ``sora-mcp approve``."""

    def __init__(self, directory: Path):
        self.dir = directory

    @staticmethod
    def request_id(request: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:16]

    def approved(self, rid: str) -> dict[str, Any] | None:
        p = self.dir / f"{rid}.approved.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None

    def request(self, rid: str, request: dict[str, Any], requested_by: str) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.dir / f"{rid}.pending.json"
        if not p.exists():
            p.write_text(json.dumps({"id": rid, "request": request, "requested_by": requested_by,
                                     "requested_at": _now()}, indent=2) + "\n", encoding="utf-8")
        return p

    def pending(self) -> list[dict[str, Any]]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in sorted(self.dir.glob("*.pending.json")):
            d = json.loads(p.read_text(encoding="utf-8"))
            if not (self.dir / f"{d['id']}.approved.json").exists():
                out.append(d)
        return out

    def approve(self, rid: str, approved_by: str, note: str = "") -> dict[str, Any]:
        p = self.dir / f"{rid}.pending.json"
        if not p.is_file():
            raise ToolError(f"no pending approval request {rid} in {self.dir}")
        d = json.loads(p.read_text(encoding="utf-8"))
        d.update({"approved_by": approved_by, "approved_at": _now(), "note": note, "os_user": _safe_user()})
        (self.dir / f"{rid}.approved.json").write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -- tools ------------------------------------------------------------------------------------------------
class SoraTools:
    """The sora-mcp tools as plain methods. ``call(name, **args)`` adds error handling and the audit log."""

    TOOLS = ("describe", "profile_source", "test_mapping", "validate_sim", "reconcile", "run_scenario",
             "explain_result", "diff_runs")

    def __init__(self, policy: Policy):
        self.policy = policy
        self.approvals = Approvals(policy.approvals_dir) if policy.approvals_dir else None
        self._schema = None

    # -- infrastructure --------------------------------------------------------------------------------
    def schema(self):
        from .schema import load_schema
        if self._schema is None:
            self._schema = load_schema(self.policy.schema)
        return self._schema

    def call(self, name: str, **args: Any) -> dict[str, Any]:
        if name not in self.TOOLS:
            return {"status": "error", "error": f"unknown tool {name!r}"}
        t0 = time.monotonic()
        fn = getattr(self, name)
        try:
            inspect.signature(fn).bind(**args)
        except TypeError as e:
            result = {"status": "error", "error": f"bad arguments: {e}"}
            self._audit(name, args, result["status"], time.monotonic() - t0)
            return result
        try:
            result = fn(**args)
        except PolicyError as e:
            result = {"status": "denied", "error": str(e)}
        except Exception as e:  # noqa: BLE001 - every failure becomes a structured result
            from .mapping import MappingError
            from .reconcile import ReconcileError
            from .results import ResultError
            if isinstance(e, (ToolError, MappingError, ReconcileError, ResultError, FileNotFoundError, ValueError)):
                result = {"status": "error", "error": str(e)}
            else:
                result = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        result.setdefault("status", "ok")
        self._audit(name, args, result["status"], time.monotonic() - t0)
        return result

    def _audit(self, tool: str, args: dict[str, Any], status: str, seconds: float) -> None:
        if not self.policy.audit_log:
            return
        rec = {"ts": _now(), "user": self.policy.user, "tool": tool, "args": args,
               "input_fingerprint": _fingerprint(args), "status": status, "ms": round(seconds * 1000)}
        try:
            self.policy.audit_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.policy.audit_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass

    # -- describe ----------------------------------------------------------------------------------------
    def describe(self, table: str | None = None, column: str | None = None, format: str = "summary") -> dict[str, Any]:
        """SIM model description. No table: overview (or the full LLM text with format='llm'); table: its
        columns, keys and checks; table+column: one column; table='outputs': the engine output files."""
        schema = self.schema()
        if table == "outputs":
            return {"output_files": OUTPUT_FILES}
        if format == "llm":
            from .docs import llm
            return {"sim_version": schema.version, "text": llm(schema)}
        if not table:
            return {
                "sim_version": schema.version, "description": schema.description, "conventions": schema.conventions,
                "tables": [{"name": t.name, "summary": t.description.split(". ")[0].rstrip(".") + ".",
                            "grain": t.grain, "primary_key": list(t.primary_key), "required_by": list(t.required_by),
                            "columns": len(t.columns)} for t in schema.tables.values()],
                "code_lists": sorted(schema.enums),
                "hint": "describe(table=...) for columns, describe(format='llm') for the complete text, "
                        "describe(table='outputs') for the engine output files",
            }
        if table not in schema.tables:
            if table in schema.enums:
                e = schema.enums[table]
                return {"code_list": table, "description": str(e["description"]).strip(), "values": e["values"]}
            raise ToolError(f"unknown SIM table or code list {table!r}; tables: {sorted(schema.tables)}")
        t = schema.tables[table]
        cols = {c.name: self._column(schema, c) for c in t.columns.values()}
        if column:
            if column not in cols:
                raise ToolError(f"{table} has no column {column!r}")
            return {"table": table, "column": cols[column]}
        return {
            "table": t.name, "description": t.description, "grain": t.grain, "primary_key": list(t.primary_key),
            "partition_by": list(t.partition_by), "required_by": list(t.required_by),
            "foreign_keys": [{"columns": list(fk.columns), "references": fk.references,
                              "ref_columns": list(fk.ref_columns)} for fk in t.foreign_keys],
            "columns": cols,
            "checks": [{"id": ck.id, "severity": ck.severity, "description": ck.description}
                       for ck in (*t.checks, *t.table_checks)],
        }

    @staticmethod
    def _column(schema, c) -> dict[str, Any]:
        d: dict[str, Any] = {"type": c.type, "storage": schema.storage_type(c), "required": c.required,
                             "description": c.description}
        if c.type == "enum":
            d["code_list"] = c.enum
            d["values"] = list(schema.enum_values(c))
        for k in ("allowed", "pattern", "range", "reg_ref", "pitfalls", "example"):
            v = getattr(c, k, None)
            if v not in (None, (), ""):
                d[k] = list(v) if isinstance(v, tuple) else v
        return d

    # -- profile_source ----------------------------------------------------------------------------------
    def profile_source(self, export_dir: str, tables: list[str] | None = None, types_file: str | None = None,
                       list_codes: bool = True, max_codes: int = 30, include_values: bool = False,
                       max_rows: int | None = None) -> dict[str, Any]:
        """Metadata profile of an export: tables, files, row counts, per column type, null rate, approximate
        distinct count and (for low-cardinality, non-sensitive code columns) the code list. Sample rows and
        min/max only with include_values (server setting, capped)."""
        from .profile import discover_tables, profile_export
        exp = self.policy.readable(export_dir, "export_dir", is_dir=True)
        n = self.policy.rows(include_values, max_rows)
        if types_file is None and (exp / "_csv_column_types.json").is_file():
            types_file = "_csv_column_types.json"
        if types_file:
            self.policy.readable(exp / types_file, "types_file", is_dir=False)
        available = sorted(discover_tables(exp))
        if tables:
            unknown = [t for t in tables if t not in available]
            if unknown:
                raise ToolError(f"unknown source tables {unknown}; available: {available}")
        doc = profile_export(exp, None, max_codes=max_codes, list_codes=list_codes, types_file=types_file,
                             tables=list(tables) if tables else None, sample_rows=n)
        doc["available_tables"] = available
        doc["values_included"] = n > 0
        return doc

    # -- test_mapping --------------------------------------------------------------------------------------
    def test_mapping(self, mapping_dir: str, export_dir: str, modules: list[str] | None = None,
                     include_values: bool = False, max_rows: int | None = None) -> dict[str, Any]:
        """Run a mapping on an export into a new SIM under the work directory and validate it. A production
        mapping returns status 'requires_approval' until a person approves this exact mapping release."""
        from .mapping import load_mapping, run_mapping
        from .validate import validate
        mdir = self.policy.readable(mapping_dir, "mapping_dir", is_dir=True)
        exp = self.policy.readable(export_dir, "export_dir", is_dir=True)
        if not (mdir / "mapping.yaml").is_file():
            raise ToolError(f"{mdir} has no mapping.yaml")
        n = self.policy.rows(include_values, max_rows)
        doc = yaml.safe_load((mdir / "mapping.yaml").read_text(encoding="utf-8")) or {}
        tf = (doc.get("sources") or {}).get("types_file")
        if tf:
            tfp = (exp / tf).resolve()
            if not tfp.is_relative_to(exp):
                raise PolicyError(f"types_file {tf!r} is outside the export directory")
        mapping = load_mapping(mdir, exp)
        release = mapping.release_id()
        reason = self.policy.is_production(mdir, doc)
        approval = None
        if reason:
            if not self.approvals:
                raise PolicyError("production mapping and no approvals directory configured")
            req = {"action": "test_mapping", "mapping_dir": str(mdir), "mapping_release": release,
                   "export_dir": str(exp)}
            rid = self.approvals.request_id(req)
            approval = self.approvals.approved(rid)
            if not approval:
                self.approvals.request(rid, req, self.policy.user)
                return {"status": "requires_approval", "reason": f"production mapping ({reason})",
                        "approval_request": {"id": rid, **req},
                        "message": "Nothing was written. A person must approve this mapping release: "
                                   f"sora-mcp approve {rid} --by <name> (approvals dir {self.approvals.dir}). "
                                   "Then call test_mapping again."}
        out = self.policy.new_output("sim", mapping.name)
        log: list[str] = []
        summary = run_mapping(mdir, exp, out, schema=self.schema(), log=log.append)
        report = validate(out, modules=modules, schema=self.schema(), samples=n)
        res = {"sim_dir": str(out), "mapping_release": release, "manifest": summary["manifest"],
               "tables": summary["tables"], "validation": self._report(report)}
        if approval:
            res["approval"] = {k: approval.get(k) for k in ("id", "approved_by", "approved_at")}
        return res

    @staticmethod
    def _report(report) -> dict[str, Any]:
        from dataclasses import asdict
        return {"ok": report.ok, "errors": report.errors, "warnings": report.warnings,
                "sim_version": report.sim_version, "row_counts": report.row_counts,
                "findings": [asdict(f) for f in report.findings]}

    # -- validate_sim ------------------------------------------------------------------------------------
    def validate_sim(self, sim_dir: str, modules: list[str] | None = None, include_values: bool = False,
                     max_rows: int | None = None) -> dict[str, Any]:
        """Full SIM validation report (structure, column rules, keys, row and table checks, dataset checks).
        Sample keys of failing rows only with include_values."""
        from .validate import MODULE_TABLES, validate
        sim = self.policy.readable(sim_dir, "sim_dir", is_dir=True)
        bad = [m for m in (modules or []) if m not in MODULE_TABLES]
        if bad:
            raise ToolError(f"unknown modules {bad}; choose from {sorted(MODULE_TABLES)}")
        n = self.policy.rows(include_values, max_rows)
        return {"sim_dir": str(sim), **self._report(validate(sim, modules=modules, schema=self.schema(), samples=n))}

    # -- reconcile ------------------------------------------------------------------------------------------
    def reconcile(self, sim_dir: str, export_dir: str, mapping_dir: str,
                  controls_file: str | None = None) -> dict[str, Any]:
        """Compare SIM totals with source control totals (controls in <mapping>/reconciliation.yaml)."""
        from .reconcile import reconcile
        sim = self.policy.readable(sim_dir, "sim_dir", is_dir=True)
        exp = self.policy.readable(export_dir, "export_dir", is_dir=True)
        mdir = self.policy.readable(mapping_dir, "mapping_dir", is_dir=True)
        cf = self.policy.readable(controls_file, "controls_file", is_dir=False) if controls_file else None
        return reconcile(sim, exp, mdir, cf)

    # -- run_scenario --------------------------------------------------------------------------------------
    def run_scenario(self, sim_dir: str, scenario: str, parameters: str | None = None, base: str | None = None,
                     workers: int | None = None) -> dict[str, Any]:
        """Run the engine (sora run) on a SIM with a scenario YAML. The output goes to a new directory under the
        work directory; returns the run id, status, the summary totals and the diagnostics."""
        eng = self.policy.engine
        if not eng or not eng.is_file():
            raise ToolError("engine not configured: start sora-mcp with --engine or set SORA_ENGINE")
        sim = self.policy.readable(sim_dir, "sim_dir", is_dir=True)
        scen = self.policy.readable(scenario, "scenario", is_dir=False)
        if base:
            basep = self.policy.readable(base, "base", is_dir=True)
        else:
            basep = next((r for r in self.policy.roots if scen.is_relative_to(r)), scen.parent)
        try:
            cfg = yaml.safe_load(scen.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ToolError(f"scenario is not valid YAML: {e}") from e
        for keys in SCENARIO_PATH_KEYS:
            v: Any = cfg
            for k in keys:
                v = v.get(k) if isinstance(v, dict) else None
            if isinstance(v, str) and v:
                p = Path(v) if Path(v).is_absolute() else basep / v
                self.policy.readable(p, "scenario " + ".".join(keys), must_exist=False)
        extra: list[str] = []
        if parameters:
            extra += ["--parameters", str(self.policy.readable(parameters, "parameters"))]
        if workers is not None:
            w = int(workers)
            if not 1 <= w <= 256:
                raise ToolError("workers must be between 1 and 256")
            extra += ["--workers", str(w)]
        out = self.policy.new_output("runs", scen.stem)
        # Argument list, no shell: every path is resolved and absolute, so none can be read as an option.
        args = [str(eng), "run", str(sim), "--scenario", str(scen), "-o", str(out), "--base", str(basep), *extra]
        t0 = time.monotonic()
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=self.policy.engine_timeout,
                               shell=False, stdin=subprocess.DEVNULL, check=False)
        except subprocess.TimeoutExpired:
            return {"status": "error", "run_id": out.name, "output_dir": str(out),
                    "error": f"engine timed out after {self.policy.engine_timeout:.0f} s"}
        log = (r.stderr or "") + (r.stdout or "")
        res: dict[str, Any] = {"run_id": out.name, "output_dir": str(out), "returncode": r.returncode,
                               "seconds": round(time.monotonic() - t0, 2), "log_tail": log.strip().splitlines()[-25:]}
        if r.returncode != 0:
            return {"status": "error", "error": "engine failed", **res}
        from .results import RunOutput
        o = RunOutput(out)
        s = o.summary
        res.update({"files": o.files(),
                    "summary": {k: s.get(k) for k in ("reference_date", "scenario", "sim_mapping_release",
                                                      "segments", "exposures", "starting_point")},
                    "impairment": {k: v.get("impairment") for k, v in (s.get("totals") or {}).items()},
                    "diagnostics": o.diagnostics})
        (out / "run.json").write_text(json.dumps({"args": args[1:], "user": self.policy.user, "at": _now(),
                                                  "returncode": r.returncode}, indent=2) + "\n")
        return res

    # -- explain_result / diff_runs -----------------------------------------------------------------------
    def explain_result(self, output_dir: str, segment: str | None = None, scenario: str | None = None,
                       year: int | None = None, top: int = 10) -> dict[str, Any]:
        """Explain a segment's impairment (starting stocks, parameters and their sources, benchmark rule, stage
        flows, provision components per EBA box) with a narrative. Without segment: an overview."""
        from .explain import explain_result
        out = self.policy.readable(output_dir, "output_dir", is_dir=True)
        return explain_result(out, segment, scenario, year, top=top)

    def diff_runs(self, run_a: str, run_b: str, abs_tol: float = 0.01, rel_tol: float = 1e-9, top: int = 10,
                  segment: str | None = None, scenario: str | None = None, year: int | None = None) -> dict[str, Any]:
        """Compare two output directories: per file differences within tolerance, summary and diagnostics
        changes, impairment totals and top movers with exposure/coverage attribution and parameter drivers."""
        from .diff_runs import diff_runs
        a = self.policy.readable(run_a, "run_a", is_dir=True)
        b = self.policy.readable(run_b, "run_b", is_dir=True)
        return diff_runs(a, b, abs_tol=abs_tol, rel_tol=rel_tol, top=top, segment=segment, scenario=scenario, year=year)


# -- MCP wrapper ----------------------------------------------------------------------------------------
INSTRUCTIONS = (
    "Sora stress-test tooling. Start with describe() for the SIM model, profile_source(export_dir) for an "
    "export, test_mapping(mapping_dir, export_dir) to map and validate, reconcile(...) against source totals, "
    "run_scenario(sim_dir, scenario) to run the engine, explain_result(output_dir, segment) and diff_runs(a, b) "
    "to understand results. Results are metadata and aggregates only; paths must be under the configured roots. "
    "status 'requires_approval' means a person must approve (never try to work around it)."
)


def build_server(tools: SoraTools):
    """Wrap the tools in an MCP server of the installed SDK (2.x MCPServer or 1.x FastMCP)."""
    try:
        from mcp.server.mcpserver import MCPServer as Server  # mcp >= 2
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as Server  # mcp 1.x
        except ImportError as e:
            raise RuntimeError('the MCP SDK is not installed: pip install -e "python[mcp]"') from e
    server = Server(SERVER_NAME, instructions=INSTRUCTIONS)

    def register(fn: Callable) -> None:
        server.tool(name=fn.__name__, description=" ".join((fn.__doc__ or "").split()))(fn)

    @register
    def describe(table: str | None = None, column: str | None = None, format: str = "summary") -> dict:
        """SIM model description: overview, a table, a column, format='llm' for the complete text, or
        table='outputs' for the engine output files."""
        return tools.call("describe", table=table, column=column, format=format)

    @register
    def profile_source(export_dir: str, tables: list[str] | None = None, types_file: str | None = None,
                       list_codes: bool = True, include_values: bool = False, max_rows: int | None = None) -> dict:
        """Metadata-only profile of an export directory: tables, row counts, column types, null rates, distinct
        counts, code lists. include_values adds capped sample rows if the server allows it."""
        return tools.call("profile_source", export_dir=export_dir, tables=tables, types_file=types_file,
                          list_codes=list_codes, include_values=include_values, max_rows=max_rows)

    @register
    def test_mapping(mapping_dir: str, export_dir: str, modules: list[str] | None = None,
                     include_values: bool = False, max_rows: int | None = None) -> dict:
        """Run a mapping directory on an export into a temporary SIM and return the validation report.
        Production mappings return status 'requires_approval' until a person approves them."""
        return tools.call("test_mapping", mapping_dir=mapping_dir, export_dir=export_dir, modules=modules,
                          include_values=include_values, max_rows=max_rows)

    @register
    def validate_sim(sim_dir: str, modules: list[str] | None = None, include_values: bool = False,
                     max_rows: int | None = None) -> dict:
        """Validate a SIM dataset against the schema (modules: core, credit, parameters, calibration)."""
        return tools.call("validate_sim", sim_dir=sim_dir, modules=modules, include_values=include_values,
                          max_rows=max_rows)

    @register
    def reconcile(sim_dir: str, export_dir: str, mapping_dir: str, controls_file: str | None = None) -> dict:
        """Reconcile SIM totals with source control totals (<mapping_dir>/reconciliation.yaml)."""
        return tools.call("reconcile", sim_dir=sim_dir, export_dir=export_dir, mapping_dir=mapping_dir,
                          controls_file=controls_file)

    @register
    def run_scenario(sim_dir: str, scenario: str, parameters: str | None = None, base: str | None = None,
                     workers: int | None = None) -> dict:
        """Run the Sora engine on a SIM with a scenario YAML; returns run id, output dir, totals, diagnostics."""
        return tools.call("run_scenario", sim_dir=sim_dir, scenario=scenario, parameters=parameters, base=base,
                          workers=workers)

    @register
    def explain_result(output_dir: str, segment: str | None = None, scenario: str | None = None,
                       year: int | None = None) -> dict:
        """Explain a segment's impairment per scenario and year (parameters and sources, flows, EBA boxes),
        or give an overview with the top segments when no segment is given."""
        return tools.call("explain_result", output_dir=output_dir, segment=segment, scenario=scenario, year=year)

    @register
    def diff_runs(run_a: str, run_b: str, abs_tol: float = 0.01, rel_tol: float = 1e-9, top: int = 10,
                  segment: str | None = None, scenario: str | None = None, year: int | None = None) -> dict:
        """Compare two output directories: file/segment/scenario/year differences, summary deltas, top movers
        with exposure vs coverage/parameter attribution."""
        return tools.call("diff_runs", run_a=run_a, run_b=run_b, abs_tol=abs_tol, rel_tol=rel_tol, top=top,
                          segment=segment, scenario=scenario, year=year)

    return server


def _protect_stdout() -> None:
    """Keep fd 1 for the protocol only: native libraries (DuckDB's progress bar) write to fd 1 directly."""
    proto = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = os.fdopen(proto, "w", encoding="utf-8", buffering=1)


def _policy_from_args(a) -> Policy:
    return Policy.create(roots=a.root, workdir=a.workdir, allow_values=True if a.allow_values else None,
                         max_rows=a.max_rows, production_patterns=a.production_pattern,
                         approvals_dir=a.approvals_dir, engine=a.engine, schema=a.schema, audit_log=a.audit_log)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sora-mcp", description="Sora MCP server (stdio) and approval commands")
    sub = p.add_subparsers(dest="command")

    def server_opts(sp):
        sp.add_argument("--root", action="append", help="readable root directory (repeatable; env SORA_MCP_ROOTS)")
        sp.add_argument("--workdir", help="work directory for SIM and run outputs (env SORA_MCP_WORKDIR)")
        sp.add_argument("--allow-values", action="store_true",
                        help="allow include_values (raw rows, capped; env SORA_MCP_ALLOW_VALUES=1)")
        sp.add_argument("--max-rows", type=int, help=f"cap for include_values rows (default {DEFAULT_MAX_ROWS})")
        sp.add_argument("--production-pattern", action="append",
                        help="glob of production mapping paths (repeatable; default */production/*)")
        sp.add_argument("--approvals-dir", help="approval store (default ~/.sora-mcp/approvals)")
        sp.add_argument("--engine", help="sora engine binary (env SORA_ENGINE)")
        sp.add_argument("--schema", help="schemas/sim directory")
        sp.add_argument("--audit-log", help="audit log (default <workdir>/audit.jsonl)")

    s = sub.add_parser("serve", help="run the MCP server on stdio (default)")
    server_opts(s)
    c = sub.add_parser("call", help="call one tool without MCP (JSON arguments), print the JSON result")
    server_opts(c)
    c.add_argument("tool", choices=SoraTools.TOOLS)
    c.add_argument("arguments", nargs="?", default="{}", help="JSON object")
    sub.add_parser("tools", help="list the tools")
    ap = sub.add_parser("approve", help="approve a pending request (run by a person, not by the agent)")
    ap.add_argument("request_id")
    ap.add_argument("--by", required=True, help="name of the approver")
    ap.add_argument("--note", default="")
    ap.add_argument("--approvals-dir")
    pe = sub.add_parser("pending", help="list pending approval requests")
    pe.add_argument("--approvals-dir")

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv = ["serve", *argv]
    a = p.parse_args(argv)

    if a.command == "tools":
        for name in SoraTools.TOOLS:
            doc = " ".join((getattr(SoraTools, name).__doc__ or "").split())
            print(f"{name:16s} {doc.split('. ')[0].rstrip('.')}.")
        return 0
    if a.command in ("approve", "pending"):
        d = Path(a.approvals_dir or os.environ.get("SORA_MCP_APPROVALS_DIR") or Path.home() / ".sora-mcp" / "approvals")
        store = Approvals(d.expanduser().resolve())
        if a.command == "pending":
            for r in store.pending():
                print(f"{r['id']}  {r['request'].get('action')}  {r['request'].get('mapping_release')}  "
                      f"{r['request'].get('mapping_dir')}  requested by {r.get('requested_by')} at {r.get('requested_at')}")
            return 0
        try:
            rec = store.approve(a.request_id, a.by, a.note)
        except ToolError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        print(f"approved {rec['id']} ({rec['request'].get('mapping_release')}) by {rec['approved_by']}")
        return 0
    try:
        policy = _policy_from_args(a)
    except PolicyError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    tools = SoraTools(policy)
    if a.command == "call":
        _protect_stdout()
        res = tools.call(a.tool, **json.loads(a.arguments))
        print(json.dumps(res, indent=2, default=str))
        return 0 if res.get("status") in ("ok", "requires_approval") else 1
    _protect_stdout()
    try:
        server = build_server(tools)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(f"sora-mcp: roots {', '.join(map(str, policy.roots))}; workdir {policy.workdir}; "
          f"values {'allowed' if policy.allow_values else 'off'}", file=sys.stderr)
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
