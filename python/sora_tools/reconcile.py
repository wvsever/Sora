"""Reconcile a SIM dataset with control totals of the source export.

Controls live next to the mapping, in ``<mapping>/reconciliation.yaml`` (so they are part of the mapping
release), or in any YAML file with the same layout::

    controls:
      - id: REC-LOAN
        description: Loans per entity - count and gross carrying amount
        sim: SELECT entity_id, count(*) AS n, sum(gross_carrying_amount) AS gca FROM sim_exposure
             WHERE exposure_type = 'loan' GROUP BY 1
        source: SELECT entity_id, count(*), sum(CAST(gross_carrying_amount AS DECIMAL(18,2)))
                FROM src.contract_loan GROUP BY 1
        keys: 1              # leading key columns (default: all but the last column)
        tolerance: 0.01      # absolute, per value (default 0.01); rel_tolerance optional

Both sides are single SELECT statements. ``sim`` sees the SIM tables by name, ``source`` sees the exported
source tables as ``src.<table>`` (declared in ``mapping.yaml``) and a one-row ``manifest`` view. They run in
the same sandboxed DuckDB connection as the mapping (read-only on the SIM and export directories). The
leading ``keys`` columns are matched, and every remaining column is compared by position.

The result holds totals and differences only; no row-level data is returned.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .duck import quote_ident, quote_str, sandboxed_connection
from .mapping import _source_view_sql, load_mapping
from .validate import _files


class ReconcileError(Exception):
    pass


def load_controls(path: Path | str) -> list[dict[str, Any]]:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    controls = doc.get("controls") or []
    for c in controls:
        if not isinstance(c, dict) or not all(k in c for k in ("id", "sim", "source")):
            raise ReconcileError(f"{path}: every control needs id, sim and source")
    return controls


def _as_float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None if v is None else float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def reconcile(sim_dir: Path | str, export_dir: Path | str, mapping_dir: Path | str,
              controls_file: Path | str | None = None, memory_limit: str = "2GB",
              max_differences: int = 20) -> dict[str, Any]:
    sim_dir, export_dir, mapping_dir = Path(sim_dir).resolve(), Path(export_dir).resolve(), Path(mapping_dir).resolve()
    cfile = Path(controls_file) if controls_file else mapping_dir / "reconciliation.yaml"
    if not cfile.is_file():
        raise ReconcileError(f"no controls: {cfile} not found")
    controls = load_controls(cfile)
    mapping = load_mapping(mapping_dir, export_dir)
    con = sandboxed_connection([sim_dir, export_dir], memory_limit=memory_limit, progress_bar=False)
    for d in sorted(p.name for p in sim_dir.iterdir() if p.is_dir()):
        fmt, glob = _files(sim_dir, d)
        if fmt == "parquet":
            con.execute(f"CREATE VIEW {quote_ident(d)} AS SELECT * FROM read_parquet({quote_str(glob)}, "
                        "hive_partitioning = false, union_by_name = true)")
        elif fmt == "csv":
            con.execute(f"CREATE VIEW {quote_ident(d)} AS SELECT * FROM read_csv({quote_str(glob)}, header = true, "
                        "hive_partitioning = false, union_by_name = true)")
    con.execute("CREATE SCHEMA src")
    used = {m.lower() for c in controls for m in re.findall(r'\bsrc\.\"?(\w+)', str(c["source"]), re.I)}
    for s in mapping.sources.values():
        if s.name.lower() not in used:   # a view over a large export is costly to create (schema sniffing)
            continue
        con.execute(f"CREATE VIEW src.{quote_ident(s.name)} AS {_source_view_sql(export_dir, s)}")
    man = mapping.manifest
    con.execute(f"CREATE VIEW manifest AS SELECT CAST({quote_str(str(man['reference_date']))} AS DATE) AS reference_date, "
                f"{quote_str(str(man['reporting_currency']))} AS reporting_currency, "
                f"{quote_str(str(man['reporting_entity_id']))} AS reporting_entity_id")

    results = []
    for c in controls:
        results.append(_run_control(con, c, max_differences))
    ok = all(r["status"] == "ok" for r in results)
    return {"sim_dir": str(sim_dir), "export_dir": str(export_dir), "controls_file": str(cfile),
            "mapping_release": mapping.release_id(), "ok": ok,
            "summary": {s: sum(1 for r in results if r["status"] == s) for s in ("ok", "difference", "error")},
            "controls": results}


def _run_control(con, c: dict[str, Any], max_differences: int) -> dict[str, Any]:
    tol = float(c.get("tolerance", 0.01))
    rel = float(c.get("rel_tolerance", 0.0))
    res: dict[str, Any] = {"id": c["id"], "description": c.get("description", ""), "tolerance": tol}
    try:
        sim_rel = con.sql(str(c["sim"]).strip().rstrip(";"))
        src_rel = con.sql(str(c["source"]).strip().rstrip(";"))
        sim_rows, src_rows = sim_rel.fetchall(), src_rel.fetchall()
        names = sim_rel.columns
    except Exception as e:  # noqa: BLE001 - surface the DuckDB message
        return res | {"status": "error", "error": str(e).splitlines()[0]}
    width = len(names)
    if sim_rows and src_rows and len(src_rows[0]) != width:
        return res | {"status": "error", "error": f"sim returns {width} columns, source {len(src_rows[0])}"}
    nkeys = int(c.get("keys", width - 1))
    measures = names[nkeys:]
    a = {tuple(str(x) for x in r[:nkeys]): r[nkeys:] for r in sim_rows}
    b = {tuple(str(x) for x in r[:nkeys]): r[nkeys:] for r in src_rows}
    diffs = []
    totals = {m: {"sim": 0.0, "source": 0.0} for m in measures}
    for k in sorted(a.keys() | b.keys()):
        va, vb = a.get(k), b.get(k)
        for i, m in enumerate(measures):
            fa = _as_float(va[i]) if va else None
            fb = _as_float(vb[i]) if vb else None
            totals[m]["sim"] += fa or 0.0
            totals[m]["source"] += fb or 0.0
            if fa is None and fb is None:
                continue
            d = (fa or 0.0) - (fb or 0.0)
            if abs(d) > max(tol, rel * max(abs(fa or 0.0), abs(fb or 0.0))) or (va is None) != (vb is None):
                diffs.append({"key": list(k), "measure": m, "sim": fa, "source": fb, "difference": d})
    for t in totals.values():
        t["difference"] = t["sim"] - t["source"]
    diffs.sort(key=lambda x: -abs(x["difference"]))
    return res | {"status": "ok" if not diffs else "difference", "key_columns": list(names[:nkeys]),
                  "groups": len(a.keys() | b.keys()), "totals": totals, "differences": len(diffs),
                  "largest_differences": diffs[:max_differences]}
