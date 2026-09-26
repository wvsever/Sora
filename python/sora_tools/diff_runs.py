"""Compare two Sora output directories (``sora-tools diff-runs A B``, sora-mcp ``diff_runs``).

Typical uses: a run with and without a parameter overlay, two scenarios, two mapping releases, or two engine
versions. The comparison is at the level of the output files, which are aggregates (segment level and above).

* **Files:** every CSV present in either run is matched by its key columns (``results.FILE_KEYS``). Numbers
  are equal within ``max(abs_tol, rel_tol * max(|a|, |b|))`` (``abs_tol`` at most 1e-9 in the
  parameter files, ``results.RATE_FILES``), text exactly. Per file: rows only in A / only in
  B, changed rows, and per column the number of changes and the largest absolute difference.
* **Summary:** every numeric leaf of ``summary.json`` that moved; diagnostics added, removed or with a new count.
* **Impairment:** totals per scenario and year, and the top-N movers (segment, scenario, year) with an
  attribution of the change.

Attribution. Per segment the provision stock at year t is ``S_t = E_t * c_t`` (exposure times coverage; the
static balance sheet keeps ``E`` constant, ``S_0`` is the starting provision stock of ``segments.csv``).
The change of each stock is split symmetrically (Shapley, two factors)::

    dS_t = dE_t * (c_a + c_b) / 2  +  dc_t * (E_a + E_b) / 2
            exposure effect           coverage effect

and the impairment change ``dI_t = dS_t - dS_{t-1}`` gets the difference of the effects, so the two parts
add up exactly. The coverage effect comes from parameters, stage mix and starting provisions; ``drivers``
lists which of these differ (parameter fields of the segment at t and t+1, starting stocks). A segment
present in one run only is attributed entirely to exposure.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .results import (
    FILE_KEYS, IGNORED_COLUMNS, PARAMS, RATE_COLUMNS, RATE_FILES, START_EXP, START_PROV, RunOutput, eur_m, exposure, num, read_csv,
    start_exposure, start_provisions, stock,
)


def _close(a: float, b: float, abs_tol: float, rel_tol: float) -> bool:
    return abs(a - b) <= max(abs_tol, rel_tol * max(abs(a), abs(b)))


def _keyed(rows: list[dict[str, str]], key: tuple[str, ...]) -> dict[tuple, dict[str, str]]:
    out: dict[tuple, dict[str, str]] = {}
    for r in rows:
        k = tuple(r.get(c, "") for c in key)
        n = 1
        kk = k
        while kk in out:          # duplicate keys: number the occurrences
            n += 1
            kk = (*k, f"#{n}")
        out[kk] = r
    return out


def _guess_key(rows: list[dict[str, str]]) -> tuple[str, ...]:
    if not rows:
        return ()
    cols = [c for c in rows[0] if c not in IGNORED_COLUMNS]
    return tuple(c for c in cols if any(r[c] not in ("", None) and num(r[c]) is None for r in rows)) or ()


def diff_file(a_path: Path | None, b_path: Path | None, name: str, abs_tol: float, rel_tol: float,
              examples: int = 5, filters: dict[str, str] | None = None) -> dict[str, Any]:
    if a_path is None or b_path is None:
        present = a_path or b_path
        return {"status": "only_in_a" if b_path is None else "only_in_b", "rows": len(read_csv(present))}
    ra, rb = read_csv(a_path), read_csv(b_path)
    key = FILE_KEYS.get(name) or _guess_key(ra or rb)
    if filters:
        def keep(r):
            return all(r.get(c) == v for c, v in filters.items() if c in r)
        ra, rb = [r for r in ra if keep(r)], [r for r in rb if keep(r)]
    ka, kb = _keyed(ra, key), _keyed(rb, key)
    only_a, only_b = [k for k in ka if k not in kb], [k for k in kb if k not in ka]
    cols = [c for c in dict.fromkeys([*(ra[0] if ra else {}), *(rb[0] if rb else {})])
            if c not in key and c not in IGNORED_COLUMNS]
    col_stats: dict[str, dict[str, Any]] = {}
    changed_rows = 0
    for k in ka.keys() & kb.keys():
        x, y = ka[k], kb[k]
        row_changed = False
        for c in cols:
            va, vb = x.get(c, ""), y.get(c, "")
            if va == vb:
                continue
            fa, fb = num(va), num(vb)
            if fa is not None and fb is not None:
                if _close(fa, fb, min(abs_tol, 1e-9) if c in RATE_COLUMNS else abs_tol, rel_tol):
                    continue
                d = abs(fb - fa)
            else:
                d = None
            st = col_stats.setdefault(c, {"changed": 0, "max_abs_diff": 0.0, "example": None})
            st["changed"] += 1
            row_changed = True
            if d is not None and d >= (st["max_abs_diff"] or 0.0):
                st["max_abs_diff"] = d
                st["example"] = {"key": list(k), "a": va, "b": vb}
            elif st["example"] is None:
                st["example"] = {"key": list(k), "a": va, "b": vb}
        changed_rows += row_changed
    for c, st in col_stats.items():
        fa = [num(r.get(c)) for r in ra]
        fb = [num(r.get(c)) for r in rb]
        if any(v is not None for v in fa + fb):
            st["sum_a"] = sum(v for v in fa if v is not None)
            st["sum_b"] = sum(v for v in fb if v is not None)
    status = "same" if not (only_a or only_b or changed_rows) else "different"
    return {"status": status, "key": list(key), "rows_a": len(ra), "rows_b": len(rb),
            "only_in_a": len(only_a), "only_in_b": len(only_b),
            "only_in_a_examples": [list(k) for k in only_a[:examples]],
            "only_in_b_examples": [list(k) for k in only_b[:examples]],
            "rows_changed": changed_rows, "columns": dict(sorted(col_stats.items()))}


def _leaves(d: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(_leaves(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(d, list):
        for i, v in enumerate(d):
            out.update(_leaves(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = d
    return out


def diff_summary(a: dict, b: dict, abs_tol: float, rel_tol: float, limit: int = 50) -> dict[str, Any]:
    la, lb = _leaves(a), _leaves(b)
    changed = []
    for k in sorted(la.keys() | lb.keys()):
        va, vb = la.get(k), lb.get(k)
        if va == vb:
            continue
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and not isinstance(va, bool):
            if _close(float(va), float(vb), abs_tol, rel_tol):
                continue
            changed.append({"path": k, "a": va, "b": vb, "delta": vb - va,
                            "rel": (vb - va) / abs(va) if va else None})
        else:
            changed.append({"path": k, "a": va, "b": vb})
    changed.sort(key=lambda x: -abs(x.get("delta") or 0.0))
    return {"changed": len(changed), "entries": changed[:limit]}


def diff_diagnostics(a: list[dict], b: list[dict]) -> dict[str, Any]:
    da, db = {d.get("id"): d for d in a}, {d.get("id"): d for d in b}
    return {
        "added": [{"id": i, "severity": db[i].get("severity"), "count": db[i].get("count"), "message": db[i].get("message")}
                  for i in db if i not in da],
        "removed": [{"id": i, "severity": da[i].get("severity"), "count": da[i].get("count")} for i in da if i not in db],
        "changed": [{"id": i, "count_a": da[i].get("count"), "count_b": db[i].get("count"),
                     "message_a": da[i].get("message"), "message_b": db[i].get("message")}
                    for i in da.keys() & db.keys()
                    if (da[i].get("count"), da[i].get("message")) != (db[i].get("count"), db[i].get("message"))],
    }


def _shapley(ea: float, eb: float, sa: float, sb: float) -> tuple[float, float]:
    """(exposure effect, coverage effect) of the stock change sa -> sb."""
    if ea + eb == 0:
        return 0.0, sb - sa
    ca = sa / ea if ea else 0.0
    cb = sb / eb if eb else 0.0
    return (eb - ea) * (ca + cb) / 2, (cb - ca) * (ea + eb) / 2


def attribute(a: RunOutput, b: RunOutput, segment: str, scenario: str, year: int,
              abs_tol: float, rel_tol: float) -> dict[str, Any]:
    sa_seg, sb_seg = a.segments().get(segment), b.segments().get(segment)
    pa, pb = a.projection(), b.projection()
    ra, rb = pa.get((segment, scenario, year)), pb.get((segment, scenario, year))
    imp_a = num(ra.get("impairment")) or 0.0 if ra else 0.0
    imp_b = num(rb.get("impairment")) or 0.0 if rb else 0.0
    delta = imp_b - imp_a
    if (ra is None) != (rb is None):
        return {"exposure": delta, "coverage": 0.0, "drivers": ["segment only in " + ("B" if ra is None else "A")]}

    def stock_exp(out, seg, proj, y):
        if y == 0:
            return start_exposure(seg), start_provisions(seg)
        r = proj.get((segment, scenario, y))
        return exposure(r), stock(r)

    ea1, sa1 = stock_exp(a, sa_seg, pa, year)
    eb1, sb1 = stock_exp(b, sb_seg, pb, year)
    ea0, sa0 = stock_exp(a, sa_seg, pa, year - 1)
    eb0, sb0 = stock_exp(b, sb_seg, pb, year - 1)
    x1, c1 = _shapley(ea1, eb1, sa1, sb1)
    x0, c0 = _shapley(ea0, eb0, sa0, sb0)
    exp_eff, cov_eff = x1 - x0, c1 - c0
    # Numerical residue (impairment is printed rounded to cents): put it into the coverage effect.
    cov_eff += delta - (exp_eff + cov_eff)

    drivers: list[str] = []
    if sa_seg and sb_seg:
        for c in (*START_EXP, *START_PROV):
            va, vb = num(sa_seg.get(c)) or 0.0, num(sb_seg.get(c)) or 0.0
            if not _close(va, vb, abs_tol, rel_tol):
                drivers.append(f"starting {c}: {va:,.2f} -> {vb:,.2f}")
    para, parb = a.parameters(), b.parameters()
    for y in sorted({0, year, year + 1}):
        sc = "actual" if y == 0 else scenario
        xa, xb = para.get(("segment", segment, sc, y)), parb.get(("segment", segment, sc, y))
        if not xa or not xb:
            continue
        for p in PARAMS:
            va, vb = num(xa.get(p)), num(xb.get(p))
            if va is not None and vb is not None and not _close(va, vb, 0.0, 1e-9):
                drivers.append(f"{p} {sc}/{y}: {va:.6g} -> {vb:.6g} ({(vb / va - 1) * 100:+.1f}%)" if va
                               else f"{p} {sc}/{y}: {va:.6g} -> {vb:.6g}")
        if xa.get("source") != xb.get("source"):
            drivers.append(f"source {sc}/{y}: {xa.get('source')} -> {xb.get('source')}")
    # Sectoral (GVA) satellite paths of the segment (NFC): paths added or removed, and moved parameters.
    sa, sb = a.sector_parameters(), b.sector_parameters()
    sectors = sorted({k[1] for k in (*sa, *sb) if k[0] == segment and k[2] == scenario})
    for sector in sectors:
        ka = [k for k in sa if k[0] == segment and k[1] == sector and k[2] == scenario]
        kb = [k for k in sb if k[0] == segment and k[1] == sector and k[2] == scenario]
        if not ka or not kb:
            drivers.append(f"sector {sector}: sectoral path only in {'B' if not ka else 'A'}")
            continue
        moved = []
        for y in sorted({year, year + 1}):
            xa, xb = sa.get((segment, sector, scenario, y)), sb.get((segment, sector, scenario, y))
            if not xa or not xb:
                continue
            moved += [f"{p} {y}" for p in PARAMS if (num(xa.get(p)) is not None and num(xb.get(p)) is not None
                                                     and not _close(num(xa.get(p)), num(xb.get(p)), 0.0, 1e-9))]
            for g in ("pd_tr", "lgd_lr"):
                if xa.get(g) != xb.get(g):
                    moved.append(f"{g} {xa.get(g)} -> {xb.get(g)}")
        if moved:
            drivers.append(f"sector {sector} path: " + ", ".join(dict.fromkeys(moved)))
    return {"exposure": exp_eff, "coverage": cov_eff, "drivers": drivers}


def diff_runs(run_a, run_b, abs_tol: float = 0.01, rel_tol: float = 1e-9, top: int = 10,
              segment: str | None = None, scenario: str | None = None, year: int | None = None) -> dict[str, Any]:
    a, b = RunOutput(run_a), RunOutput(run_b)
    filters: dict[str, str] = {}
    if segment:
        filters["segment"] = segment
    if scenario:
        filters["scenario"] = scenario
    if year is not None:
        filters["year"] = str(int(year))
    files: dict[str, Any] = {}
    names = sorted(set(a.files()) | set(b.files()))
    for name in names:
        if not name.endswith(".csv"):
            continue
        files[name] = diff_file(a.path / name if a.has(name) else None, b.path / name if b.has(name) else None,
                                name, min(abs_tol, 1e-9) if name in RATE_FILES else abs_tol, rel_tol, filters=filters)

    # Impairment totals and movers.
    pa, pb = a.projection(), b.projection()

    def want(k):
        s, sc, y = k
        return ((not segment or s == segment) and (not scenario or sc == scenario)
                and (year is None or y == int(year)))

    keys = sorted({k for k in (pa.keys() | pb.keys()) if want(k)})
    totals: dict[tuple[str, int], dict[str, float]] = {}
    movers = []
    for k in keys:
        ia = num(pa[k].get("impairment")) or 0.0 if k in pa else 0.0
        ib = num(pb[k].get("impairment")) or 0.0 if k in pb else 0.0
        t = totals.setdefault((k[1], k[2]), {"a": 0.0, "b": 0.0, "exposure": 0.0, "coverage": 0.0})
        t["a"] += ia
        t["b"] += ib
        if not _close(ia, ib, abs_tol, rel_tol):
            movers.append((k, ia, ib))
    movers.sort(key=lambda m: -abs(m[2] - m[1]))
    # Attribution of every moved row (the totals need all of them); drivers only for the top movers.
    top_rows = []
    for i, (k, ia, ib) in enumerate(movers):
        att = attribute(a, b, *k, abs_tol, rel_tol)
        t = totals[(k[1], k[2])]
        t["exposure"] += att["exposure"]
        t["coverage"] += att["coverage"]
        if i < top:
            top_rows.append({"segment": k[0], "scenario": k[1], "year": k[2], "impairment_a": ia, "impairment_b": ib,
                             "delta": ib - ia, "attribution": {"exposure": att["exposure"], "coverage": att["coverage"]},
                             "drivers": att["drivers"][:12]})
    total_rows = [{"scenario": sc, "year": y, "impairment_a": v["a"], "impairment_b": v["b"], "delta": v["b"] - v["a"],
                   "attribution": {"exposure": v["exposure"], "coverage": v["coverage"]}}
                  for (sc, y), v in sorted(totals.items(), key=lambda kv: (kv[0][0] != "baseline", kv[0][0], kv[0][1]))]

    result = {
        "run_a": str(a.path), "run_b": str(b.path),
        "tolerance": {"abs": abs_tol, "rel": rel_tol},
        "filters": {"segment": segment, "scenario": scenario, "year": year},
        "identical": all(f["status"] == "same" for f in files.values()),
        "files": files,
        "summary": diff_summary(a.summary, b.summary, abs_tol, rel_tol) if not filters else None,
        "diagnostics": diff_diagnostics(a.diagnostics, b.diagnostics),
        "impairment": {"totals": total_rows, "rows_changed": len(movers), "top_movers": top_rows},
    }
    result["narrative"] = _narrative(result)
    return result


def _narrative(r: dict[str, Any]) -> str:
    diff_files = [n for n, f in r["files"].items() if f["status"] != "same"]
    if r["identical"]:
        lines = [f"The runs are identical within the tolerance (abs {r['tolerance']['abs']}, rel {r['tolerance']['rel']})."]
    else:
        lines = [f"{len(diff_files)} of {len(r['files'])} files differ: " + ", ".join(
            f"{n} ({f.get('rows_changed', 0)} rows changed, {f.get('only_in_a', 0)} only in A, {f.get('only_in_b', 0)} only in B)"
            if f["status"] == "different" else f"{n} ({f['status']})" for n, f in r["files"].items() if n in diff_files) + "."]
    for t in r["impairment"]["totals"]:
        if abs(t["delta"]) > r["tolerance"]["abs"]:
            att = t["attribution"]
            lines.append(f"{t['scenario'].capitalize()} year {t['year']}: impairment {eur_m(t['impairment_a'])} -> "
                         f"{eur_m(t['impairment_b'])} ({eur_m(t['delta'])}: exposure {eur_m(att['exposure'])}, "
                         f"coverage/parameters {eur_m(att['coverage'])}).")
    for m in r["impairment"]["top_movers"][:5]:
        lines.append(f"Mover {m['segment']} {m['scenario']}/{m['year']}: {eur_m(m['delta'])}"
                     + (f"; drivers: {'; '.join(m['drivers'][:3])}" if m["drivers"] else "") + ".")
    dg = r["diagnostics"]
    if dg["added"] or dg["removed"] or dg["changed"]:
        lines.append("Diagnostics: " + ", ".join(
            [f"+{d['id']}" for d in dg["added"]] + [f"-{d['id']}" for d in dg["removed"]]
            + [f"~{d['id']}" for d in dg["changed"]]) + ".")
    return "\n".join(lines)


def to_json(result: dict[str, Any]) -> str:
    return json.dumps(result, indent=2, default=str)
