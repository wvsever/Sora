# Execution Pipeline Plan

## Objective

Define a predictable, restartable, and testable execution sequence.

## Pipeline

### Phase 1 - Load dimensions

Load dictionaries and reference data.

### Phase 2 - Load and compile scenario

Resolve textual identifiers to integer IDs.

Validate all rules.

### Phase 3 - Precompute tables

Examples:

- Rating migration probabilities
- Sector multipliers
- Country multipliers
- Time-step curves
- Rate shock tables

### Phase 4 - Stream records

Process records in bounded chunks.

### Phase 5 - Apply transformations

Recommended order:

1. Macro-derived risk factors
2. Rating migration
3. PD/LGD changes
4. Default determination
5. Collateral repricing
6. Stage migration
7. Impairment impact
8. Funding/liquidity changes
9. Interest-rate effects

Exact ordering must be explicit because later rules may depend on earlier results.

### Phase 6 - Validate chunk

Run configured invariant checks.

### Phase 7 - Aggregate

Update thread-local metrics.

### Phase 8 - Persist

Write stressed state and/or event results.

### Phase 9 - Final reduction

Merge worker metrics deterministically.

### Phase 10 - Summary

Produce compact result summary and diagnostics.

## Restartability

For very large runs, optionally persist checkpoints at partition boundaries.

A checkpoint should include:

- Scenario fingerprint
- Input fingerprint
- Last completed partition
- Aggregation state
- Output offsets
