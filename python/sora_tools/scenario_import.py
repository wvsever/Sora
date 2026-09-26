"""Convert EBA / ESRB / ECB scenario workbooks into Sora's normalised long scenario table.

Supported workbooks (EBA EU-wide stress test layout, 2025 onwards):
* Macro-financial scenario (ESRB): one sheet per variable, one row per country / currency / item.
* Real GVA by sector: one sheet per NACE sector (`Real_GVA_<sector>`).

Every sheet has a header row with column groups ("Baseline growth (%)", "Adverse rate (%)", ...) and a
row of years under it. Derived columns ("Cumulative growth ...", "Level of deviation ...") are skipped.

Country keys are ISO 3166 alpha-2 (the ESRB's UK becomes GB). Aggregates keep the ESRB codes:
EA (euro area), EU, WR (world), AS (Asia), LA (Latin America).

Output columns: scenario, variable, measure, unit, key, tenor, sector, year, value.
Provenance (source files, SHA-256, sheets) is written to `<output>.meta.json`.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

MACRO_SHEETS = {
    "GDP": "real_gdp",
    "HICP": "hicp",
    "Unemployment": "unemployment_rate",
    "RRE prices": "residential_property_prices",
    "CRE prices": "commercial_property_prices",
    "Long-term rates": "long_term_rate",
    "SWAP rates": "swap_rate",
    "Stock prices": "stock_prices",
    "ForeignDemandall": "foreign_demand_commodities",
    "Itraxx": "itraxx",
    "Exchange rates": "fx_rate",
}
# Variables whose rows are keyed by country (ISO 3166 alpha-2, or ESRB aggregates EA, EU, WR, AS, LA).
COUNTRY_KEYED = {"real_gdp", "hicp", "unemployment_rate", "residential_property_prices",
                 "commercial_property_prices", "long_term_rate", "real_gva"}
KEY_OVERRIDES = {"UK": "GB"}   # ESRB uses UK; SIM uses ISO 3166 (GB)
FIELDS = ["scenario", "variable", "measure", "unit", "key", "tenor", "sector", "year", "value"]


@dataclass(frozen=True)
class Group:
    scenario: str
    measure: str
    unit: str


def classify(label: str) -> Group | None:
    """Map a column-group label to (scenario, measure, unit). None = derived column, skip."""
    s = re.sub(r"\s+", " ", label.strip().lower())
    if s.startswith(("cumulative", "minimum", "maximum", "level of deviation", "level deviation 20")):
        return None
    unit = "pct" if "(%)" in s else "pp" if "(p.p.)" in s else "level"
    if "deviation from" in s:
        return Group("adverse", "deviation_from_start", "pct")
    if s.startswith("starting point"):
        return Group("starting_point_average" if "average" in s else "starting_point", "level", unit)
    for prefix, scen in (("historical", "historical"), ("baseline", "baseline"), ("adverse", "adverse")):
        if s.startswith(prefix):
            return Group(scen, "growth" if "growth" in s else "level", unit)
    return None


def _num(v) -> Decimal | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return Decimal(repr(float(v))).quantize(Decimal("1e-9"))
    return None


def parse_sheet(rows: list[tuple], variable: str, source: str, sheet: str, sector: str | None = None) -> list[dict]:
    # Header row = first row with a classifiable group label; year row = next row.
    h = next((i for i, r in enumerate(rows[:15]) if any(isinstance(c, str) and classify(c) for c in r)), None)
    if h is None:
        return []
    header, years = rows[h], rows[h + 1]
    cols: dict[int, tuple[Group, int]] = {}
    group = None
    for j, cell in enumerate(header):
        if isinstance(cell, str) and cell.strip():
            group = classify(cell)
        y = years[j] if j < len(years) else None
        if group and isinstance(y, int) and 1990 < y < 2100:
            cols[j] = (group, y)
    if not cols:
        return []
    first = min(cols)
    out, carry = [], None
    for r in rows[h + 2:]:
        keys = [c for c in r[1:first] if c is not None]
        values = {j: _num(r[j]) for j in cols if j < len(r)}
        if not any(v is not None for v in values.values()):
            continue                       # blank or note row
        if variable == "swap_rate":        # currency carried down, tenor in column C
            carry = r[1] or carry
            key, tenor = carry, r[2]
        elif first >= 3:                   # name in B, code in C
            key, tenor = (r[2] or r[1]), None
        else:
            key, tenor = r[1], None
        if key is None or (variable == "swap_rate" and tenor is None):
            continue                       # unlabeled rows (checksums, footers)
        if variable in COUNTRY_KEYED:
            if not (isinstance(key, str) and re.fullmatch(r"[A-Z]{2}", key.strip())):
                continue                   # helper rows (e.g. column-number rows in the GVA sheets)
            key = KEY_OVERRIDES.get(key.strip(), key.strip())
        for j, v in values.items():
            if v is None:
                continue
            g, year = cols[j]
            out.append({"scenario": g.scenario, "variable": variable, "measure": g.measure, "unit": g.unit,
                        "key": str(key).strip(), "tenor": tenor, "sector": sector, "year": year, "value": str(v),
                        "source": source, "sheet": sheet})
    return out


def import_workbook(path: Path | str) -> list[dict]:
    import openpyxl  # optional dependency: pip install "sora-tools[scenario]"

    path = Path(path)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
        if ws.title in MACRO_SHEETS:
            out += parse_sheet(rows, MACRO_SHEETS[ws.title], path.name, ws.title)
        elif ws.title.startswith("Real_GVA_"):
            out += parse_sheet(rows, "real_gva", path.name, ws.title, sector=ws.title[len("Real_GVA_"):])
    return out


def import_scenarios(paths: list[Path | str], output: Path | str) -> int:
    rows = []
    for p in paths:
        rows += import_workbook(p)
    keyf = lambda r: (r["variable"], r["sector"] or "", r["key"], r["tenor"] or "", r["scenario"], r["measure"], r["year"])  # noqa: E731
    rows.sort(key=keyf)
    seen = set()
    for r in rows:
        k = keyf(r)
        if k in seen:
            raise ValueError(f"duplicate scenario value {k} ({r['source']} / {r['sheet']})")
        seen.add(k)
    with open(output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    meta = {"generated_by": "sora-tools scenario-import", "rows": len(rows), "sources": [
        {"file": Path(p).name, "sha256": hashlib.sha256(Path(p).read_bytes()).hexdigest(),
         "sheets": sorted({r["sheet"] for r in rows if r["source"] == Path(p).name})} for p in paths]}
    Path(str(output) + ".meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return len(rows)
