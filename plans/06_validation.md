# Validation Plan

## Objective

Detect impossible or internally inconsistent stressed states with minimal overhead.

## Fast validation

Run during normal execution.

Examples:

- PD within allowed range
- LGD within allowed range
- Non-negative collateral where required
- Stage in supported range
- No invalid rating IDs
- No integer overflow
- Currency and dimension IDs valid

## Full validation

Run for development, QA, and selected production runs.

Examples:

- Balance reconciliation
- Exposure totals by portfolio
- Collateral allocation checks
- Stage migration reconciliation
- Default count consistency
- ECL movement consistency
- Funding-flow consistency
- Duplicate identity detection

## Invariants

Represent invariants as explicit named checks so failures are machine-readable.

Example:

```text
INV-CR-001: stressed PD must be >= baseline floor
INV-CR-002: defaulted exposure must have default flag
INV-CL-004: collateral haircut cannot increase collateral value
INV-ST-003: Stage 3 exposure must satisfy configured impairment criteria
```

## Failure policy

Modes:

- fail-fast
- accumulate-errors
- warnings-only

The default benchmark mode should disable expensive full validation.
