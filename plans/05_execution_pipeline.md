# Execution Pipeline Plan

## Objective

Define a predictable, restartable, and testable execution sequence over the partitioned input dataset.

## Commands

```text
# Python tooling (sora-tools, DuckDB) - see 12_technology_stack.md
sora-tools map      <mapping.sql…> --export <dir> -o <sim>   # mapping SQL on exported files -> SIM Parquet
sora-tools validate <sim> [--level full]                     # schema, keys, constraints, reconciliation

# C++ engine (sora)
sora inspect   <sim>                              # tables, partitions, row counts
sora calibrate <sim> -o params.csv                # derive starting-point PD/TR/LGD/LR (09_risk_parameters.md)
sora run       <sim> --scenario s.yaml -o out     # project and aggregate
```

## Pipeline (`sora run`)

### Phase 1 - Discover SIM dataset

Read the run manifest (reference date, SIM version, mapping release). Enumerate the partitions of the required SIM tables. Fail early on missing required tables or columns.

### Phase 2 - Load dimensions and party data

Load reference tables, then counterparties, ratings, collateral, allocations and guarantees. Build dictionaries and dense indices. Freeze.

### Phase 3 - Load and compile scenario

Load the macro path, satellite coefficients, benchmarks, starting parameters and overlays.

Resolve textual identifiers to integer IDs.

Validate all rules.

### Phase 4 - Precompute tables

Examples:

- Parameter table `[scenario][year][segment]` (PD12M_S1/S2, TR1-2, TR2-1, LGD_S1/S2/S3, LRLT_S2)
- Collateral value indices by year, country and collateral form
- Stage transition matrices
- Rate shock and curve tables (NII, later)

### Phase 5 - Stream exposures

For each entity partition, in parallel: read the contract tables, join them to the party store and the latest allowance, and attach the segment and starting parameters.

### Phase 6 - Apply transformations

Order per contract, per year `t0+1 … t0+3`:

1. Collateral repricing (property and financial collateral indices) → LTV, secured share
2. Segment parameters for the year (satellite or table), plus contract overrides, floors and caps
3. Stage flows (S1↔S2, S1→S3, S2→S3). No cures from S3.
4. Provisions per flow (Boxes 4–8) and the old-S3 provision (Box 9)
5. Maturity and like-for-like replacement (static balance sheet)
6. Off-balance-sheet exposures: CCF conversion, then the same stage logic
7. Funding and liquidity changes (later)
8. Interest-rate and NII effects (later)

The ordering is explicit because later rules depend on earlier results.

### Phase 7 - Validate chunk

Run the configured invariant checks.

### Phase 8 - Aggregate

Update thread-local metrics keyed by `(scenario, year, segment, country)`.

### Phase 9 - Persist

Write the stressed state and/or event results, if requested.

### Phase 10 - Final reduction

Merge worker metrics deterministically, in `(entity_id, file, row_seq)` order.

### Phase 11 - Summary

Produce:

- EBA-layout result tables (CR_SCEN, CR_SCEN_OFF_BS, CR_SECTOR as long CSV)
- The parameter file actually used
- Diagnostics
- Fingerprints

## Restartability

For very large runs, optionally persist checkpoints at partition boundaries.

A checkpoint includes:

- Scenario fingerprint
- Input fingerprint (SIM manifest, mapping release, per-file size and xxHash)
- Last completed partition
- Aggregation state
- Output offsets
