#!/usr/bin/env python3
"""Scaling benchmarks of the Sora engine (plans/07_benchmarking.md).

Runs `sora inspect` and `sora run` on the reference SIM replicated 1x/10x/100x (tools/scale_sim.py), plus
engine-thread scaling (--workers) and DuckDB memory-limit variants on the largest scale. Per run it records
the per-stage timings the engine prints to stderr, wall time, peak RSS (the engine's own figure and the
kernel's for the child process, and /usr/bin/time -v if installed), CPU time and exposures per second.

Output: benchmarks/results/<date>-<host>.json (all runs) and a Markdown summary (default benchmarks/RESULTS.md).

    python benchmarks/run_benchmarks.py                        # 1x, 10x, 100x; workers 1,2,4,8 at 100x
    python benchmarks/run_benchmarks.py --scales 1,10 --repeat 1 --markdown -
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCENARIO = REPO / "tests" / "scenarios" / "test_eba2025.yaml"
TIME_V = "/usr/bin/time"

STAGE_RE = re.compile(r"^  (\S.*?)\s+([0-9.]+) ms$")
RSS_RE = re.compile(r"peak RSS ([0-9.]+) MB")
TRACE_RE = re.compile(r"^  trace\s+([0-9.]+) ms \(engine\s+([0-9.]+) ms\)\s+(\d+) rows\s+(.*)$")
INSPECT_RE = re.compile(r"(\d+) counterparties, (\d+) exposures")
SCOPE_RE = re.compile(r"in scope \(default EBA scope\): (\d+) exposures in (\d+) segments")
MAXRSS_V_RE = re.compile(r"Maximum resident set size \(kbytes\): (\d+)")


def run_engine(cmd: list[str], env: dict[str, str]) -> dict:
    """Runs one engine command; returns stage timings, memory and CPU figures of that child process."""
    use_time = os.access(TIME_V, os.X_OK)
    full = [TIME_V, "-v", *cmd] if use_time else cmd
    with tempfile.TemporaryFile("w+") as fo, tempfile.TemporaryFile("w+") as fe:
        t0 = time.perf_counter()
        p = subprocess.Popen(full, stdout=fo, stderr=fe, env=env)
        _, status, ru = os.wait4(p.pid, 0)   # rusage of this child only
        wall = time.perf_counter() - t0
        p.returncode = os.waitstatus_to_exitcode(status)
        fo.seek(0)
        fe.seek(0)
        out, err = fo.read(), fe.read()
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({p.returncode}):\n{err[-3000:]}")
    cpu = ru.ru_utime + ru.ru_stime
    r: dict = {"cmd": cmd, "wall_s": round(wall, 3), "cpu_s": round(cpu, 2), "cpu_utilisation": round(cpu / wall, 2),
               "child_max_rss_mb": round(ru.ru_maxrss / 1024, 1), "stages_ms": {}, "trace": []}
    for line in err.splitlines():
        if m := TRACE_RE.match(line):
            r["trace"].append({"ms": float(m[1]), "engine_ms": float(m[2]), "rows": int(m[3]), "sql": m[4][:70]})
        elif m := STAGE_RE.match(line):
            r["stages_ms"][m[1]] = float(m[2])
        if m := RSS_RE.search(line):
            r["engine_peak_rss_mb"] = float(m[1])
        if m := MAXRSS_V_RE.search(line):
            r["time_v_max_rss_mb"] = round(int(m[1]) / 1024, 1)
    if m := INSPECT_RE.search(out):
        r["counterparties"], r["exposures"] = int(m[1]), int(m[2])
    if m := SCOPE_RE.search(out):
        r["in_scope"], r["segments"] = int(m[1]), int(m[2])
    # With /usr/bin/time the child is `time`, whose ru_maxrss is the engine's; either way this is the kernel's figure.
    r["peak_rss_mb"] = max(v for v in (r.get("engine_peak_rss_mb"), r["child_max_rss_mb"], r.get("time_v_max_rss_mb")) if v)
    return r


def best_of(n: int, fn) -> tuple[dict, list[dict]]:
    runs = [fn() for _ in range(n)]
    return min(runs, key=lambda r: r["wall_s"]), runs


def dataset_stats(sim: Path) -> dict:
    files = [f for f in sim.rglob("*") if f.is_file()]
    stats = {"files": len(files), "bytes": sum(f.stat().st_size for f in files), "tables": {}}
    try:
        import duckdb
        con = duckdb.connect()
        for t in sorted(d for d in sim.iterdir() if d.is_dir()):
            tf = list(t.rglob("*.parquet"))
            if not tf:
                continue
            n = con.execute(f"SELECT count(*) FROM read_parquet('{t}/**/*.parquet', union_by_name = true)").fetchone()[0]
            stats["tables"][t.name] = {"rows": n, "files": len(tf), "bytes": sum(f.stat().st_size for f in tf)}
    except ImportError:
        pass
    return stats


def ensure_scaled(base: Path, factor: int) -> Path:
    if factor == 1:
        return base
    sim = base.parent / f"{base.name}-x{factor}"
    manifest = sim / "sim_manifest.json"
    if manifest.exists() and json.loads(manifest.read_text()).get("scale", {}).get("factor") == factor:
        return sim
    print(f"creating {sim} ...", flush=True)
    subprocess.run([sys.executable, str(REPO / "tools" / "scale_sim.py"), str(base), str(sim), "--factor", str(factor)], check=True)
    return sim


def host_info(engine: Path) -> dict:
    cpu = ""
    try:
        cpu = next(l.split(":", 1)[1].strip() for l in Path("/proc/cpuinfo").read_text().splitlines() if l.startswith("model name"))
    except (OSError, StopIteration):
        pass
    mem = ""
    try:
        kb = int(next(l.split()[1] for l in Path("/proc/meminfo").read_text().splitlines() if l.startswith("MemTotal")))
        mem = f"{kb / 1024 / 1024:.1f} GB"
    except (OSError, StopIteration):
        pass
    commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True).stdout.strip()
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
    return {"host": socket.gethostname(), "cpu": cpu, "cores": os.cpu_count(), "memory": mem, "os": platform.platform(),
            "engine": str(engine), "commit": commit + ("+dirty" if dirty else ""), "load_avg_1m_at_start": load}


def fmt_ms(v: float | None) -> str:
    return "" if v is None else (f"{v / 1000:.2f} s" if v >= 1000 else f"{v:.0f} ms")


def markdown(res: dict) -> str:
    h = res["host"]
    lines = [
        "# Benchmark results",
        "",
        f"Generated by `benchmarks/run_benchmarks.py` on {res['date']} (commit `{h['commit']}`).",
        f"Host: {h['cpu']}, {h['cores']} cores, {h['memory']} RAM, {h['os']}. "
        f"Load average at start: {h['load_avg_1m_at_start']:.1f}." if h.get("load_avg_1m_at_start") is not None else "",
        f"Each figure is the fastest of {res['repeat']} runs (wall time); the machine may be shared, so treat small "
        "differences as noise. Scenario: `tests/scenarios/test_eba2025.yaml`. Datasets: the reference SIM "
        "replicated with `tools/scale_sim.py`.",
        "",
        "## Datasets",
        "",
        "| Scale | Exposures | Counterparties | Stage history rows | Files | Parquet MB |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for s in res["scales"]:
        st = s["dataset"]
        hist = st["tables"].get("sim_stage_history", {}).get("rows", 0)
        lines.append(f"| {s['factor']}x | {s['inspect'].get('exposures', 0):,} | {s['inspect'].get('counterparties', 0):,} | "
                     f"{hist:,} | {st['files']:,} | {st['bytes'] / 1e6:,.0f} |")
    lines += [
        "",
        "## `sora run` by scale (default workers and DuckDB threads)",
        "",
        "| Scale | Wall | Peak RSS | Exposures/s | CPU util. | load SIM | calibrate | project | other |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in res["scales"]:
        r = s["run"]
        st = r["stages_ms"]
        other = sum(v for k, v in st.items() if k not in ("load SIM", "calibrate", "project"))
        lines.append(f"| {s['factor']}x | {r['wall_s']:.2f} s | {r['peak_rss_mb']:,.0f} MB | {r['exposures_per_s']:,.0f} | "
                     f"{r['cpu_utilisation']:.2f} | {fmt_ms(st.get('load SIM'))} | {fmt_ms(st.get('calibrate'))} | "
                     f"{fmt_ms(st.get('project'))} | {fmt_ms(other)} |")
    lines += ["", "`sora inspect` (load, segment, input checks):", "",
              "| Scale | Wall | Peak RSS |", "|---|---:|---:|"]
    for s in res["scales"]:
        r = s["inspect"]
        lines.append(f"| {s['factor']}x | {r['wall_s']:.2f} s | {r['peak_rss_mb']:,.0f} MB |")
    if res.get("workers"):
        base = res["workers"][0]
        lines += ["", f"## Engine worker threads ({res['workers_scale']}x, `--workers N`)", "",
                  "Only the projection is parallel in the engine; loading and the stage-history sort run in DuckDB "
                  "(`--threads`, default all cores). Outputs are byte-identical for every N (checked by the harness).", "",
                  "| Workers | project | Speed-up | Wall | Peak RSS | Identical output |", "|---:|---:|---:|---:|---:|---|"]
        for w in res["workers"]:
            p0, p = base["stages_ms"].get("project"), w["stages_ms"].get("project")
            lines.append(f"| {w['workers']} | {fmt_ms(p)} | {p0 / p:.2f}x | {w['wall_s']:.2f} s | {w['peak_rss_mb']:,.0f} MB | "
                         f"{'yes' if w['identical'] else 'NO'} |")
    if res.get("memory_limits"):
        lines += ["", f"## DuckDB memory limit ({res['workers_scale']}x, `--memory-limit`)", "",
                  "| memory_limit | Wall | Peak RSS | load SIM | calibrate |", "|---|---:|---:|---:|---:|"]
        for m in res["memory_limits"]:
            if "failed" in m:
                lines.append(f"| {m['memory_limit']} | failed: {m['failed']} | | | |")
                continue
            lines.append(f"| {m['memory_limit']} | {m['wall_s']:.2f} s | {m['peak_rss_mb']:,.0f} MB | "
                         f"{fmt_ms(m['stages_ms'].get('load SIM'))} | {fmt_ms(m['stages_ms'].get('calibrate'))} |")
    if res.get("trace"):
        lines += ["", f"## Where the time goes ({res['workers_scale']}x, `SORA_TRACE=1`)", "",
                  "Per DuckDB query: wall time, and the part spent in the engine's own code (chunk callbacks).", "",
                  "| Query | Rows | Wall | Engine |", "|---|---:|---:|---:|"]
        for t in res["trace"]:
            if t["ms"] >= 50:
                lines.append(f"| `{t['sql'][:60]}…` | {t['rows']:,} | {fmt_ms(t['ms'])} | {fmt_ms(t['engine_ms'])} |")
    if res.get("baseline"):
        b = res["baseline"]
        lines += ["", f"## Compared with a baseline (`--baseline`, commit `{b['host']['commit']}`)", "",
                  f"Baseline measured on {b['date']} with the fastest of {b['repeat']} runs.", "",
                  "| Scale | Wall before | Wall after | Speed-up | Peak RSS before | Peak RSS after |",
                  "|---|---:|---:|---:|---:|---:|"]
        before = {x["factor"]: x["run"] for x in b["scales"]}
        for x in res["scales"]:
            if x["factor"] not in before:
                continue
            r0, r1 = before[x["factor"]], x["run"]
            lines.append(f"| {x['factor']}x | {r0['wall_s']:.2f} s | {r1['wall_s']:.2f} s | {r0['wall_s'] / r1['wall_s']:.1f}x | "
                         f"{r0['peak_rss_mb']:,.0f} MB | {r1['peak_rss_mb']:,.0f} MB |")
    if res.get("notes"):
        lines += ["", "## Notes", "", *[f"- {n}" for n in res["notes"]]]
    return "\n".join(l for l in lines if l is not None) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--engine", type=Path, default=REPO / "build" / "release" / "sora")
    p.add_argument("--sim", type=Path, default=REPO / "build" / "sim" / "20260630", help="1x SIM (scaled copies next to it)")
    p.add_argument("--scales", default="1,10,100")
    p.add_argument("--workers", default="1,2,4,8", help="worker counts for the thread-scaling runs ('' = skip)")
    p.add_argument("--memory-limits", default="", help="DuckDB memory limits to compare at the largest scale, e.g. 512MB,1GB,2GB")
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--out", type=Path, default=REPO / "benchmarks" / "results")
    p.add_argument("--markdown", default=str(REPO / "benchmarks" / "RESULTS.md"), help="summary file ('-' = stdout)")
    p.add_argument("--note", action="append", default=[], help="note for the Markdown summary (repeatable)")
    p.add_argument("--baseline", type=Path, help="results JSON of an earlier engine, for a before/after table")
    p.add_argument("--render", type=Path, help="only write the Markdown summary of an existing results JSON")
    args = p.parse_args()
    if args.render:
        res = json.loads(args.render.read_text())
        res["notes"] = res.get("notes", []) + args.note
        return write_summary(res, args)
    if not args.engine.exists():
        raise SystemExit(f"engine not found: {args.engine} (build it first)")
    if not (args.sim / "sim_manifest.json").exists():
        raise SystemExit(f"{args.sim}: no SIM (see README quick start)")

    env = dict(os.environ)
    env.pop("SORA_TRACE", None)
    scales = [int(s) for s in args.scales.split(",") if s]
    res: dict = {"date": dt.date.today().isoformat(), "host": host_info(args.engine), "repeat": args.repeat,
                 "scenario": str(SCENARIO.relative_to(REPO)), "scales": [], "notes": args.note}
    tmp = Path(tempfile.mkdtemp(prefix="sora-bench-"))
    try:
        def run_cmd(sim: Path, out: Path, *extra: str) -> list[str]:
            return [str(args.engine), "run", str(sim), "--scenario", str(SCENARIO), "-o", str(out), "--base", str(REPO), *extra]

        def one(cmd: list[str]) -> dict:
            return run_engine(cmd, env)

        largest = None
        for f in scales:
            sim = ensure_scaled(args.sim, f)
            largest = (f, sim)
            print(f"== {f}x ({sim})", flush=True)
            ins, ins_all = best_of(args.repeat, lambda: one([str(args.engine), "inspect", str(sim)]))
            run, run_all = best_of(args.repeat, lambda: one(run_cmd(sim, tmp / f"run-{f}")))
            run["exposures"] = ins.get("exposures")
            run["exposures_per_s"] = round(ins["exposures"] / run["wall_s"]) if ins.get("exposures") else None
            print(f"   inspect {ins['wall_s']:.2f} s {ins['peak_rss_mb']:.0f} MB | run {run['wall_s']:.2f} s "
                  f"{run['peak_rss_mb']:.0f} MB {run['exposures_per_s']:,} exp/s", flush=True)
            res["scales"].append({"factor": f, "sim": str(sim), "dataset": dataset_stats(sim), "inspect": ins, "run": run,
                                  "all_runs_wall_s": [r["wall_s"] for r in run_all]})

        if largest:
            f, sim = largest
            res["workers_scale"] = f
            ref = None
            res["workers"] = []
            for w in [int(x) for x in args.workers.split(",") if x]:
                out = tmp / f"workers-{w}"
                r, _ = best_of(args.repeat, lambda: one(run_cmd(sim, out, "--workers", str(w))))
                files = {x.name: x.read_bytes() for x in sorted(out.iterdir())}
                ref = ref or files
                r["workers"], r["identical"] = w, files == ref
                print(f"   workers {w}: project {r['stages_ms'].get('project')} ms, identical {r['identical']}", flush=True)
                res["workers"].append(r)
            res["memory_limits"] = []
            for m in [x for x in args.memory_limits.split(",") if x]:
                try:
                    r, _ = best_of(args.repeat, lambda: one(run_cmd(sim, tmp / "mem", "--memory-limit", m)))
                except RuntimeError as e:   # a too-small limit makes DuckDB fail: record it, do not abort
                    msg = next((l for l in str(e).splitlines() if "Error" in l), str(e).splitlines()[-1])
                    r = {"failed": msg.strip()[:200]}
                r["memory_limit"] = m
                print(f"   memory_limit {m}: " + (r["failed"] if "failed" in r else f"{r['wall_s']:.2f} s, {r['peak_rss_mb']:.0f} MB"), flush=True)
                res["memory_limits"].append(r)
            traced = run_engine(run_cmd(sim, tmp / "trace"), {**env, "SORA_TRACE": "1"})
            res["trace"] = traced["trace"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{res['date']}-{res['host']['host']}.json"
    path.write_text(json.dumps(res, indent=1) + "\n")
    print(f"wrote {path}")
    return write_summary(res, args)


def write_summary(res: dict, args: argparse.Namespace) -> int:
    if args.baseline:
        b = json.loads(args.baseline.read_text())
        res["baseline"] = {"date": b["date"], "host": b["host"], "repeat": b["repeat"],
                           "scales": [{"factor": x["factor"], "run": {k: x["run"][k] for k in ("wall_s", "peak_rss_mb", "stages_ms")}}
                                      for x in b["scales"]]}
    md = markdown(res)
    if args.markdown == "-":
        print(md)
    else:
        Path(args.markdown).write_text(md)
        print(f"wrote {args.markdown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
