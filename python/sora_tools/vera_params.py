"""Convert Vera's (baselcalculator's) per-exposure ``risk_parameters.csv`` export into Sora's
``sim_risk_parameter`` format (``schemas/sim/tables/sim_risk_parameter.yaml``).

Division of labour (SORA-DS ``_LANE_CONTRACT.md``): Vera *derives* PD/LGD/EAD/CCF/default state from the
bank's accounting facts, one row per credit exposure, and writes them with the rule that produced each value
and a reason code where it could not derive one. This module is a pure re-shaping step: it never computes a
risk parameter itself, only relabels and routes Vera's already-derived values into the columns the stress
engine reads (:mod:`sora.parameters`, ``include/sora/parameters.hpp``). No blanket default, no fallback
constant: a value this module cannot place is left empty, exactly as Vera left it, with a named reason.

Mapping (contract, "Sora mapping"):

* ``level=exposure``, ``key=contract_id`` (Vera's own ``contract_id`` column - the same key
  ``schemas/sim/tables/sim_exposure.yaml``'s ``exposure_id`` uses; Vera's ``exposure_id`` is
  ``entity_id/contract_id`` and is not used here), ``scenario=actual``, ``year=0``, ``source=external``.
* ``declared_stage == stage1``: ``pd12m_s1 = pd12m_pit``, ``lgd_s1 = lgd_ifrs9``.
* ``declared_stage == stage2``: ``pd12m_s2 = pd12m_pit``, ``lgd_s2 = lgd_ifrs9``, ``lrlt_s2 = lrlt``.
* ``declared_stage == stage3``, or ``poci`` with ``is_defaulted``: ``lgd_s3 = lgd_s3`` (same name on both
  sides).
* ``ccf``, ``pd_reg``, ``lgd_reg`` pass through unchanged for every stage (same column name on both sides).
* ``tr1_2``, ``tr2_1``, ``tr3_1``, ``tr3_2`` pass through when Vera supplies them (optional columns). Vera
  reads them off the same 12-month stage-transition matrix as ``pd12m_pit``, so ``pd12m_s1 + tr1_2`` and
  ``pd12m_s2 + tr2_1`` are at most 1 by construction. Where Vera leaves them empty (a scorecard-priced PD),
  Sora falls back to its own segment calibration for the transition rate.

PAR-010 (``sora::check_parameters`` / ``schemas/sim/tables/sim_risk_parameter.yaml``'s ``RPA-002``/``RPA-003``)
would reject the whole run on one out-of-range value with no results written at all (``src/main.cpp``:
"N invalid parameter values; no results written"). This module runs the identical checks per row *before*
Sora does: an out-of-range field, or a stage-outflow pair summing over 1, is dropped back to empty (never
clamped, never zeroed) and counted with a reason code, so one bad exposure does not blank an entire run.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# schemas/sim/tables/sim_risk_parameter.yaml, primary_key = [level, key, scenario, year].
SIM_COLUMNS: list[str] = [
    "level", "key", "scenario", "year",
    "pd12m_s1", "pd12m_s2", "tr1_2", "tr2_1", "tr3_1", "tr3_2",
    "lgd_s1", "lgd_s2", "lgd_s3", "lrlt_s2", "ccf", "pd_reg", "lgd_reg", "source",
]

# Vera's export contract (SORA-DS _LANE_CONTRACT.md, "THE EXPORT CONTRACT - risk_parameters.csv").
VERA_COLUMNS: list[str] = [
    "entity_id", "contract_id", "exposure_id", "counterparty_id", "instrument_kind", "declared_stage",
    "is_defaulted", "default_date", "default_trigger", "dpd",
    "ead", "ccf", "pd_reg", "lgd_reg", "exposure_class", "approach",
    "pd12m_pit", "pd_lifetime", "lgd_ifrs9", "lgd_s3", "lrlt",
    "pd_rule", "lgd_rule", "ccf_rule", "reason_codes",
]

# Columns copied unchanged for every stage (same name and meaning in both formats).
_PASSTHROUGH_FIELDS: tuple[str, ...] = ("ccf", "pd_reg", "lgd_reg")
# Optional Vera columns (added after the first export contract): passed through when present, silently
# absent otherwise, so an older risk_parameters.csv still converts.
_OPTIONAL_PASSTHROUGH_FIELDS: tuple[str, ...] = ("tr1_2", "tr2_1", "tr3_1", "tr3_2")

# sim_risk_parameter fields this converter can populate, in schema order (excludes tr1_2/tr2_1/tr3_1/tr3_2,
# which no source here supplies, but they still take part in the PAR-010 outflow check below).
_NUMERIC_FIELDS: tuple[str, ...] = (
    "pd12m_s1", "pd12m_s2", "tr1_2", "tr2_1", "tr3_1", "tr3_2",
    "lgd_s1", "lgd_s2", "lgd_s3", "lrlt_s2", "ccf", "pd_reg", "lgd_reg",
)

# RPA-002 / RPA-003 / sora::check_parameters: individual [0, 1] range per field...
_RANGE_FIELDS: tuple[str, ...] = (
    "pd12m_s1", "pd12m_s2", "tr1_2", "tr2_1", "tr3_1", "tr3_2",
    "lgd_s1", "lgd_s2", "lgd_s3", "lrlt_s2", "ccf", "pd_reg", "lgd_reg",
)
# ...and the two stage-outflow sums that must not exceed 1.
_OUTFLOW_PAIRS: tuple[tuple[str, str], ...] = (("pd12m_s1", "tr1_2"), ("pd12m_s2", "tr2_1"))

_EXTRAS_COLUMNS: list[str] = [
    "contract_id", "entity_id", "exposure_id", "counterparty_id",
    "is_defaulted", "default_date", "default_trigger", "dpd", "ead",
]


class VeraParamsError(Exception):
    """Raised for a structural problem with the input file (missing required columns), never for a bad value."""


def _read_float(row: dict[str, str], key: str, stats: "ConversionStats") -> float | None:
    """Parse a numeric source field. Malformed text is treated as absent (never as 0), and counted."""
    raw = (row.get(key) or "").strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        _bump(stats.empty_reasons, f"MALFORMED:{key}")
        return None


def _to_bool(s: str | None) -> bool:
    return (s or "").strip().lower() == "true"


def _fmt(v: Any) -> str:
    if v == "" or v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.9f}"
    return str(v)


def _bump(d: dict[str, int], k: str, n: int = 1) -> None:
    d[k] = d.get(k, 0) + n


@dataclass
class ConversionStats:
    rows_in: int = 0
    rows_out: int = 0
    rows_dropped_all_empty: int = 0
    rows_dropped_no_contract_id: int = 0
    by_stage: dict[str, int] = field(default_factory=dict)
    empty_reasons: dict[str, int] = field(default_factory=dict)
    range_violations: dict[str, int] = field(default_factory=dict)
    range_violation_samples: list[str] = field(default_factory=list)

    @property
    def has_range_violations(self) -> bool:
        return sum(self.range_violations.values()) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "rows_dropped_all_empty": self.rows_dropped_all_empty,
            "rows_dropped_no_contract_id": self.rows_dropped_no_contract_id,
            "by_stage": dict(sorted(self.by_stage.items())),
            "empty_reasons": dict(sorted(self.empty_reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
            "range_violations": dict(sorted(self.range_violations.items())),
            "range_violation_samples": self.range_violation_samples,
        }

    def to_text(self) -> str:
        lines = [
            f"vera-params: {self.rows_in:,d} rows in -> {self.rows_out:,d} rows out "
            f"({self.rows_dropped_all_empty:,d} dropped: no derivable parameter; "
            f"{self.rows_dropped_no_contract_id:,d} dropped: no contract_id)",
            "  by stage: " + ", ".join(f"{k}={v:,d}" for k, v in sorted(self.by_stage.items())),
        ]
        if self.empty_reasons:
            lines.append("  empty reasons:")
            for k, v in sorted(self.empty_reasons.items(), key=lambda kv: (-kv[1], kv[0])):
                lines.append(f"    {v:>8,d}  {k}")
        if self.range_violations:
            lines.append("  PAR-010 pre-checks (value dropped to empty, never clamped):")
            for k, v in sorted(self.range_violations.items()):
                lines.append(f"    {v:>8,d}  {k}")
            if self.range_violation_samples:
                lines.append("    e.g. " + ", ".join(self.range_violation_samples))
        return "\n".join(lines)


def convert_row(row: dict[str, str], stats: ConversionStats, scenario: str, year: int,
                 max_samples: int = 10) -> dict[str, Any] | None:
    """Convert one Vera ``risk_parameters.csv`` row to one ``sim_risk_parameter`` row, or ``None`` to drop it."""
    contract_id = (row.get("contract_id") or "").strip()
    if not contract_id:
        stats.rows_dropped_no_contract_id += 1
        return None

    stage = (row.get("declared_stage") or "").strip()
    is_defaulted = _to_bool(row.get("is_defaulted"))

    out: dict[str, Any] = {c: "" for c in SIM_COLUMNS}
    out["level"] = "exposure"
    out["key"] = contract_id
    out["scenario"] = scenario
    out["year"] = year
    out["source"] = "external"

    pd12m_pit = _read_float(row, "pd12m_pit", stats)
    lgd_ifrs9 = _read_float(row, "lgd_ifrs9", stats)
    lgd_s3 = _read_float(row, "lgd_s3", stats)
    lrlt = _read_float(row, "lrlt", stats)

    if stage == "stage1":
        stage_bucket = "stage1"
        if pd12m_pit is not None:
            out["pd12m_s1"] = pd12m_pit
        else:
            _bump(stats.empty_reasons, "STAGE1_NO_PD12M_PIT")
        if lgd_ifrs9 is not None:
            out["lgd_s1"] = lgd_ifrs9
        else:
            _bump(stats.empty_reasons, "STAGE1_NO_LGD_IFRS9")
    elif stage == "stage2":
        stage_bucket = "stage2"
        if pd12m_pit is not None:
            out["pd12m_s2"] = pd12m_pit
        else:
            _bump(stats.empty_reasons, "STAGE2_NO_PD12M_PIT")
        if lgd_ifrs9 is not None:
            out["lgd_s2"] = lgd_ifrs9
        else:
            _bump(stats.empty_reasons, "STAGE2_NO_LGD_IFRS9")
        if lrlt is not None:
            out["lrlt_s2"] = lrlt
        else:
            _bump(stats.empty_reasons, "STAGE2_NO_LRLT")
    elif stage == "stage3" or (stage == "poci" and is_defaulted):
        stage_bucket = "stage3_or_poci_defaulted"
        if lgd_s3 is not None:
            out["lgd_s3"] = lgd_s3
        else:
            _bump(stats.empty_reasons, "STAGE3_NO_LGD_S3")
    elif stage == "poci":
        stage_bucket = "poci_not_defaulted"
        _bump(stats.empty_reasons, "POCI_NOT_DEFAULTED_UNSTAGED")
    else:
        stage_bucket = "unrecognised"
        _bump(stats.empty_reasons, f"UNRECOGNISED_STAGE:{stage or '(empty)'}")
    _bump(stats.by_stage, stage_bucket)

    for f in _PASSTHROUGH_FIELDS:
        v = _read_float(row, f, stats)
        if v is not None:
            out[f] = v
        else:
            _bump(stats.empty_reasons, f"NO_{f.upper()}")

    for f in _OPTIONAL_PASSTHROUGH_FIELDS:
        if f in row:
            v = _read_float(row, f, stats)
            if v is not None:
                out[f] = v

    # PAR-010 pre-check 1: individual [0, 1] range (RPA-002/003, sora::check_parameters).
    for f in _RANGE_FIELDS:
        v = out[f]
        if v != "" and not (0.0 <= v <= 1.0):
            _bump(stats.range_violations, f)
            if len(stats.range_violation_samples) < max_samples:
                stats.range_violation_samples.append(f"{contract_id}:{f}={v}")
            out[f] = ""
            _bump(stats.empty_reasons, f"PAR010_RANGE:{f}")

    # PAR-010 pre-check 2: stage outflow sums <= 1 (RPA-002/003) when Vera supplies both halves.
    for pd_field, tr_field in _OUTFLOW_PAIRS:
        pv, tv = out[pd_field], out[tr_field]
        if pv != "" and tv != "" and pv + tv > 1.0 + 1e-12:
            key = f"{pd_field}+{tr_field}"
            _bump(stats.range_violations, key)
            if len(stats.range_violation_samples) < max_samples:
                stats.range_violation_samples.append(f"{contract_id}:{key}={pv + tv}")
            out[pd_field] = out[tr_field] = ""
            _bump(stats.empty_reasons, f"PAR010_OUTFLOW:{key}")

    if all(out[c] == "" for c in _NUMERIC_FIELDS):
        stats.rows_dropped_all_empty += 1
        return None
    return out


def extras_row(row: dict[str, str]) -> dict[str, Any]:
    return {c: row.get(c, "") for c in _EXTRAS_COLUMNS}


def convert(input_csv: Path | str, output_csv: Path | str, *, extras_csv: Path | str | None = None,
            scenario: str = "actual", year: int = 0, max_samples: int = 10) -> ConversionStats:
    """Convert Vera's ``risk_parameters.csv`` to a ``sim_risk_parameter`` CSV. Returns the conversion stats.

    Every column in :data:`SIM_COLUMNS` is always present in the output, empty where nothing was derivable.
    Rows with no derivable parameter at all (every numeric field empty) are dropped, since a row carrying
    only the key adds nothing the engine can use. ``extras_csv``, if given, carries Vera's own default-state
    columns (``is_defaulted``, ``default_date``, ``default_trigger``, ``dpd``, ``ead``) per contract for the
    SIM mapping to draw on later; it is not part of the ``sim_risk_parameter`` schema.
    """
    input_csv = Path(input_csv)
    output_csv = Path(output_csv)
    stats = ConversionStats()

    with input_csv.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in ("contract_id", "declared_stage", "is_defaulted") if c not in (reader.fieldnames or [])]
        if missing:
            raise VeraParamsError(f"{input_csv}: missing required column(s) {missing} "
                                   f"(has {reader.fieldnames})")

        output_csv.parent.mkdir(parents=True, exist_ok=True)
        extras_fh = None
        extras_writer = None
        if extras_csv is not None:
            extras_csv = Path(extras_csv)
            extras_csv.parent.mkdir(parents=True, exist_ok=True)
            extras_fh = extras_csv.open("w", newline="", encoding="utf-8")
            extras_writer = csv.DictWriter(extras_fh, fieldnames=_EXTRAS_COLUMNS)
            extras_writer.writeheader()

        try:
            with output_csv.open("w", newline="", encoding="utf-8") as out_fh:
                writer = csv.DictWriter(out_fh, fieldnames=SIM_COLUMNS)
                writer.writeheader()
                for row in reader:
                    stats.rows_in += 1
                    if extras_writer is not None:
                        extras_writer.writerow(extras_row(row))
                    converted = convert_row(row, stats, scenario, year, max_samples=max_samples)
                    if converted is None:
                        continue
                    stats.rows_out += 1
                    writer.writerow({c: _fmt(converted[c]) for c in SIM_COLUMNS})
        finally:
            if extras_fh is not None:
                extras_fh.close()

    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", help="Vera's risk_parameters.csv (bcal_cli --out-dir)")
    p.add_argument("-o", "--output", required=True, help="output sim_risk_parameter CSV path")
    p.add_argument("--extras", help="optional side CSV: is_defaulted/default_date/default_trigger/dpd/ead per contract")
    p.add_argument("--scenario", default="actual")
    p.add_argument("--year", type=int, default=0)
    p.add_argument("--report", help="write the conversion report as JSON")
    p.add_argument("--strict", action="store_true", help="exit 1 if any PAR-010 pre-check value was dropped")
    args = p.parse_args(argv)
    try:
        stats = convert(args.input, args.output, extras_csv=args.extras, scenario=args.scenario, year=args.year)
    except VeraParamsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(stats.to_text())
    if args.report:
        Path(args.report).write_text(json.dumps(stats.to_dict(), indent=2) + "\n")
    if args.strict and stats.has_range_violations:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
