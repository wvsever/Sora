#!/usr/bin/env python3
"""Run the C++ engine on the reference SIM and compare its output with the golden results.

Tolerances: money per segment/scenario/year 1 cent or relative 1e-12 (whichever is larger); parameters 1e-9.
The reference SIM is produced on demand (extract test data + reference mapping) if --sim is not given.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def ensure_sim() -> Path:
    sim = REPO / "build" / "sim" / "20260630"
    if not (sim / "sim_manifest.json").exists():
        subprocess.run([sys.executable, str(REPO / "tools" / "extract_testdata.py")], check=True)
        sys.path.insert(0, str(REPO / "python"))
        from sora_tools.mapping import run_mapping
        run_mapping(REPO / "mappings" / "cppbank", REPO / "build" / "testdata" / "20260630", sim, log=lambda *_: None)
    return sim


def rows(path: Path, key_cols: list[str]) -> dict:
    with open(path) as f:
        return {tuple(r[k] for k in key_cols): r for r in csv.DictReader(f)}


def compare(name: str, golden: Path, actual: Path, keys: list[str], money: bool) -> list[str]:
    g, a = rows(golden / name, keys), rows(actual / name, keys)
    errors = []
    if set(g) != set(a):
        errors.append(f"{name}: key sets differ (missing {sorted(set(g) - set(a))[:3]}, extra {sorted(set(a) - set(g))[:3]})")
    worst = (0.0, None)
    for k in sorted(set(g) & set(a)):
        for col, gv in g[k].items():
            av = a[k][col]
            try:
                gf, af = float(gv), float(av)
            except ValueError:
                if gv != av:
                    errors.append(f"{name} {k} {col}: {av!r} != {gv!r}")
                continue
            tol = max(0.01, 1e-12 * abs(gf)) if money else 1e-9
            diff = abs(af - gf)
            if diff > worst[0]:
                worst = (diff, (k, col))
            if diff > tol:
                errors.append(f"{name} {k} {col}: engine {av} vs golden {gv} (diff {diff:.3g})")
    print(f"  {name:16s} {len(g):6d} rows, max abs diff {worst[0]:.3g}" + (f" at {worst[1]}" if worst[1] else ""))
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--engine", required=True)
    ap.add_argument("--golden", type=Path, required=True)
    ap.add_argument("--scenario", type=Path, required=True)
    ap.add_argument("--sim", type=Path)
    args = ap.parse_args()
    sim = args.sim or ensure_sim()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        subprocess.run([args.engine, "run", str(sim), "--scenario", str(args.scenario), "-o", str(out),
                        "--base", str(REPO)], check=True)
        errors = []
        errors += compare("segments.csv", args.golden, out, ["segment"], money=True)
        errors += compare("parameters.csv", args.golden, out, ["key", "scenario", "year"], money=False)
        errors += compare("projection.csv", args.golden, out, ["segment", "scenario", "year"], money=True)
        gs, es = json.loads((args.golden / "summary.json").read_text()), json.loads((out / "summary.json").read_text())
        for field in ("segments", "exposures"):
            if gs[field] != es[field]:
                errors.append(f"summary {field}: engine {es[field]} vs golden {gs[field]}")
    for e in errors[:30]:
        print("  MISMATCH", e)
    print(f"{'FAILED' if errors else 'PASSED'}: {len(errors)} mismatches")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
