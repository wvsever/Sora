"""Read the engine's output directory (``sora run -o <dir>``).

Shared by ``explain`` and ``diff_runs``. Only the aggregate result files are read (segment level and above);
the engine writes no exposure-level data.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

SCENARIOS = ("actual", "baseline", "adverse")

# Key columns per output file (the rest are values). Unknown CSV files are keyed by their text columns.
FILE_KEYS: dict[str, tuple[str, ...]] = {
    "segments.csv": ("segment",),
    "parameters.csv": ("level", "key", "scenario", "year"),
    "projection.csv": ("segment", "scenario", "year"),
    "collateral.csv": ("segment", "scenario", "year"),
    "off_balance.csv": ("segment", "exposure_type", "scenario", "year"),
    "benchmarks.csv": ("segment",),
    "rea.csv": ("segment", "scenario", "year"),
    "cr_scen.csv": ("Pivot", "Geographical breakdown", "Scenario", "Year", "Portfolio", "Asset class 1",
                    "Asset class 2", "Asset classes"),
    "cr_scen_off_bs.csv": ("Pivot", "Geographical breakdown", "Scenario", "Year", "Portfolio", "Asset class 1",
                           "Asset class 2", "Asset classes"),
    "cr_sector.csv": ("Pivot", "Geographical breakdown", "Scenario", "Year", "COREP asset class", "NACE code"),
}
# Columns that are neither keys nor compared as numbers (row numbers change when rows are added).
IGNORED_COLUMNS = {"RowNum"}

PARAMS = ("pd12m_s1", "pd12m_s2", "tr1_2", "tr2_1", "tr3_1", "tr3_2", "lgd_s1", "lgd_s2", "lgd_s3", "lrlt_s2")
STOCK_COLS = ("prov_stock_s1", "prov_stock_s2", "prov_stock_s3", "prov_stock_poci")
EXP_COLS = ("exp_s1", "exp_s2", "exp_s3_old", "exp_s3_new", "exp_poci")
START_EXP = ("exp_s1", "exp_s2", "exp_s3", "exp_poci")
START_PROV = ("prov_s1", "prov_s2", "prov_s3", "prov_poci")


class ResultError(Exception):
    pass


def num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class RunOutput:
    """Lazy reader of one output directory."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        if not self.path.is_dir():
            raise ResultError(f"output directory not found: {self.path}")
        if not (self.path / "projection.csv").is_file() and not (self.path / "summary.json").is_file():
            raise ResultError(f"{self.path} is not a Sora output directory (no projection.csv or summary.json)")
        self._cache: dict[str, Any] = {}

    def files(self) -> list[str]:
        return sorted(p.name for p in self.path.iterdir() if p.is_file() and p.suffix in (".csv", ".json"))

    def has(self, name: str) -> bool:
        return (self.path / name).is_file()

    def rows(self, name: str) -> list[dict[str, str]]:
        if name not in self._cache:
            self._cache[name] = read_csv(self.path / name) if self.has(name) else []
        return self._cache[name]

    def json(self, name: str) -> dict[str, Any]:
        if name not in self._cache:
            self._cache[name] = json.loads((self.path / name).read_text(encoding="utf-8")) if self.has(name) else {}
        return self._cache[name]

    @property
    def summary(self) -> dict[str, Any]:
        return self.json("summary.json")

    @property
    def diagnostics(self) -> list[dict[str, Any]]:
        return self.json("diagnostics.json").get("findings", [])

    def segments(self) -> dict[str, dict[str, str]]:
        return {r["segment"]: r for r in self.rows("segments.csv")}

    def projection(self) -> dict[tuple[str, str, int], dict[str, str]]:
        return {(r["segment"], r["scenario"], int(r["year"])): r for r in self.rows("projection.csv")}

    def parameters(self) -> dict[tuple[str, str, str, int], dict[str, str]]:
        return {(r["level"], r["key"], r["scenario"], int(r["year"])): r for r in self.rows("parameters.csv")}

    def benchmarks(self) -> dict[str, dict[str, str]]:
        return {r["segment"]: r for r in self.rows("benchmarks.csv")}

    def scenario_points(self) -> list[tuple[str, int]]:
        pts = {(s, y) for _, s, y in self.projection()}
        return sorted(pts, key=lambda p: (SCENARIOS.index(p[0]) if p[0] in SCENARIOS else 9, p[0], p[1]))


def start_exposure(seg: dict[str, str] | None) -> float:
    return sum(num(seg.get(c)) or 0.0 for c in START_EXP) if seg else 0.0


def start_provisions(seg: dict[str, str] | None) -> float:
    return sum(num(seg.get(c)) or 0.0 for c in START_PROV) if seg else 0.0


def stock(row: dict[str, str] | None) -> float:
    return sum(num(row.get(c)) or 0.0 for c in STOCK_COLS) if row else 0.0


def exposure(row: dict[str, str] | None) -> float:
    return sum(num(row.get(c)) or 0.0 for c in EXP_COLS) if row else 0.0


def eur_m(v: float | None) -> str:
    return "n/a" if v is None else f"EUR {v / 1e6:,.2f}m"


def pct(v: float | None, digits: int = 2) -> str:
    return "n/a" if v is None else f"{100 * v:.{digits}f}%"
