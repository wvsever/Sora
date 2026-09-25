# Validation Plan

## Objective

Detect impossible or internally inconsistent stressed states with minimal overhead, and prove the projection is correct against independent results.

## Input validation (on load)

- The schema matches `_csv_column_types.json` (type, parseability)
- Required keys are present and unique (`contract_id` is unique across the dataset)
- Referential integrity: contract → counterparty, allocation → contract and collateral, guarantee → contract, allowance → contract
- Dimension values are known (currency, country, ESA sector, stage, product)
- The reporting date equals the manifest `reportingDate`

## Fast validation

Runs during normal execution.

Examples:

- PD, TR and LGD within [0, 1]. PD at or above the technical floor.
- Transition rows: `TR1-2 + PD12M_S1 ≤ 1`, `TR2-1 + PD12M_S2 ≤ 1`
- Stage masses per contract sum to 1 and are non-negative
- Non-negative collateral where required
- Stage in the supported range (S1, S2, S3, POCI)
- No invalid dimension IDs
- No integer overflow in cents aggregation

## Full validation

Runs for development, QA and selected production runs.

Starting-point reconciliation (on the reference dataset):

- `Σ contract_loan.impairment_allowance` = `Σ impairment_allowance.closing_balance` (latest period) per entity and stage
- `contract_loan.declared_stage` = `impairment_allowance.declared_stage_at_period_end` (latest period)
- Contract gross carrying amounts vs `gl_balance` on the matching `gl_account` codes, per entity, within tolerance
- Collateral allocations do not exceed collateral value

Projection consistency:

- Exposure conservation per segment: `Exp S1 + S2 + S3 + POCI` is constant over the horizon (static balance sheet)
- Segment-level results equal the EBA box formulas applied to segment totals (Boxes 3–9)
- Old-S3 provisions never fall below the t0 provisions. There are no S3 releases.
- Cumulative new-S3 provisions are non-decreasing
- The adverse is at or above the baseline for impairment per segment (warning, not error)
- Duplicate identity detection

## Invariants

Invariants are explicit named checks, so failures are machine-readable.

```text
INV-IN-001: contract_id unique across dataset
INV-IN-002: every contract references an existing counterparty
INV-RC-001: contract allowance equals impairment_allowance closing balance (per entity, stage)
INV-CR-001: stressed PD must be >= technical floor
INV-CR-002: no S3 -> S1/S2 transition when no_cure_from_s3
INV-CR-003: Prov Old S3(t) >= Prov S3(t0)
INV-CR-004: segment exposure conserved across years
INV-CR-005: contract-level aggregation equals segment-level EBA formula (tolerance 1 cent per segment)
INV-CL-004: collateral haircut cannot increase collateral value
```

## Reference results

The test data has no expected outputs. Correctness rests on three layers:

1. **Formula unit tests.** Hand-computed cases for each EBA box (Boxes 3–9), the 5/6–1/6 adverse blend, the Box 9 floor and matrix annualisation. The values are small and round, and live in `tests/unit/`.
2. **Golden results for `ref-1x`.** An independent reference implementation (Python + DuckDB, `tools/reference/`) that shares no code with the engine computes:
   - the calibrated starting-point parameters per segment
   - the 3-year baseline and adverse projection for a fixed test scenario
   - the CR_SCEN-style aggregates

   Its outputs are committed as `tests/golden/20260630/*.csv`. The engine must match them within 1 cent per segment and year. Changes to golden files require review.
3. **External reference (optional).** If the institution provides real starting-point parameters and a previously submitted CR_SCEN template, that pair becomes an end-to-end regression case.

A fixed test scenario (`tests/scenarios/test_adverse.yaml` plus macro CSV) is committed alongside the golden files. The golden results do not depend on the confidential satellite models.

## Failure policy

Modes:

- fail-fast
- accumulate-errors
- warnings-only

The default benchmark mode disables expensive full validation.
