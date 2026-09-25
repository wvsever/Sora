# Delivery Roadmap

Every phase is delivered against the reference dataset `tests/data/20260630.7z` (see `tests/data/README.md`).

## Phase 0 - Reference and test assets

Deliver:

- `tools/extract_testdata` (extract the `.7z` into the build tree; used by CTest)
- `tools/scenario_import`: EBA/ESRB/ECB xlsx in `docs/` → normalised macro CSV
- `tools/reference/`: independent Python + DuckDB implementation of calibration and the EBA credit projection
- A fixed test scenario and golden results in `tests/golden/20260630/`
- Hand-computed unit cases for EBA Boxes 3–9

## Phase 1 - Core skeleton and input

Deliver:

- CMake project
- Partitioned CSV reader (Hive partitions, header mapping, exact decimal parsing, nulls)
- Dimensions, party store, dictionaries
- Exposure assembly for `contract_loan` (+ counterparty, rating, collateral allocation, latest allowance)
- `sora inspect`
- Input and starting-point reconciliation checks (INV-IN-*, INV-RC-001)
- Single-threaded pipeline
- Unit tests

## Phase 2 - Starting-point risk parameters

Deliver:

- EBA CR_SCEN segmentation (`segments.csv`)
- External parameter file reader
- `sora calibrate`: stage transition matrices from `impairment_allowance` history, annualisation, LGD from collateral and recoveries, LRLT from S2 coverage, observation thresholds and fallbacks
- Match with the golden parameters from `tools/reference/`

## Phase 3 - Credit stress projection

Deliver:

- Compiled scenario parameter tables (table-driven projected parameters first)
- Stage-flow projection and provisions (Boxes 3–9), with the no-cure and no-release constraints
- Static balance sheet (like-for-like replacement), POCI static
- Collateral repricing by property price paths
- CR_SCEN-layout output
- Match with the golden projection

## Phase 4 - Performance baseline and parallel execution

Deliver:

- Chunk processing
- Partitioned workers per entity
- Thread-local aggregators and deterministic merge
- Benchmark harness, allocation and peak-memory measurements
- Scaled datasets (`ref-10x`, `ref-100x`) and scaling benchmarks

## Phase 5 - Scenario models and coverage

Deliver:

- Satellite models (macro → PD/TR, LGD/LR per segment)
- Benchmark fallback with the 10% coverage rule
- Adverse reversion beyond the horizon and the 5/6–1/6 final-year blend
- Off-balance-sheet exposures (`contract_commitment`), CCF and CR_SCEN_OFF_BS
- Debt securities at amortised cost (`contract_security_position`)
- CR_SECTOR (NACE) output

## Phase 6 - Funding and rates

Deliver:

- NII projection from `contract_cashflow`, repricing dates, deposits, and the risk-free and spread curves
- Deposit outflows (by `deposit_type`, DGS coverage, operational flags)
- Funding spread shocks
- Margin pass-through rules (EBA ch. 4)

## Phase 7 - Production hardening

Deliver:

- Binary high-throughput input/output
- Checkpointing
- Crash-safe output handling
- Structured diagnostics
- Scenario and input fingerprints
- Performance regression CI

## Phase 8 - Extensions and advanced optimisation

Only after profiling or explicit need:

- IRB/STA REA and output floor
- Market risk revaluation and CCR
- Explicit SIMD
- Alternative allocators
- NUMA-aware execution
- Memory-mapped columnar input
- Custom compact binary format

## Release philosophy

Keep the core engine small. Add features only when they preserve deterministic behaviour, bounded memory use, and measurable throughput.
