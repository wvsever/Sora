# Delivery Roadmap

Every phase is delivered against the reference dataset `tests/data/20260630.7z` (see `tests/data/README.md`).

## Status

| Phase | Status |
|---|---|
| 0 Reference and test assets | Done, except the synthetic ECB benchmark file (moved to phase 5) |
| 1 Core skeleton and input | Done: SIM reader (DuckDB, streamed), dataset, segmentation, input checks, `sora inspect` |
| 2 Starting-point parameters | Done: derived calibration, and customer parameters (per exposure or segment hierarchy, starting point and projection) |
| 3 Credit stress projection | Done for on-balance amortised cost, matching the golden results exactly: EBA Boxes 3–9, static balance sheet (by construction), EBA CSV_CR_SCEN output, collateral repricing and LTV (`collateral.csv`, CR_SCEN LTV columns). Open: prior-year Actual rows (needs a second year-end in the data). |
| 4 Performance and parallel execution | Done: `--workers N` (bit-identical for any N), 10x/100x benchmarks (`benchmarks/RESULTS.md`): 100x = 6.2M exposures in 17.7 s. Open: pre-sorted SIM to remove DuckDB sorts; true chunked streaming of exposures. |
| 5 Scenario models and coverage | Off-balance-sheet done: commitments and guarantees given (scenario key `off_balance`), customer CCF with the CRR Art. 111(2) fallback, on-balance segment parameters and Boxes 3–9 on post-CCF amounts, `off_balance.csv` and EBA CSV_CR_SCEN_OFF_BS (`cr_scen_off_bs.csv`), matching the golden results exactly. Open: undrawn part of on-balance loans, ECB benchmark rule, CR_SECTOR, satellite estimation. |
| 5b Agent and calculator integration | Calculator REST client and IRB REA projection done (`--calculator`, `rea.csv`). Open: `sora-mcp`, `/v1/parameters/credit` integration, `explain_result`, `diff_runs`. |
| 6–8 | Not started. |

Hardening (2026-09-25): code review of phases 3–5b; ten findings fixed (calculator retry/offline fallback, token over HTTP, best-effort cache, JSON escaping, shared exposure parameter paths, per-batch REA records, worker stop flag, level-key lookup, calibration index invariant, option validation). CI (`python`, `cpp` release + ASan/UBSan) green on `main`.

## Phase 0 - Reference and test assets

Deliver:

- `tools/extract_testdata` (extract the `.7z` into the build tree; used by CTest)
- SIM schema v1 (`schemas/sim/`) with full descriptions, and generators for docs, DDL and the LLM description
- Reference mapping `mappings/cppbank/*.sql` (test dataset → SIM) and `sora-tools map` / `sora-tools validate` (DuckDB)
- `tools/scenario_import`: EBA/ESRB/ECB xlsx in `docs/` → normalised macro CSV
- `tools/reference/`: independent Python + DuckDB implementation of calibration and the EBA credit projection
- Synthetic stand-ins for customer inputs: satellite coefficients and an ECB-style benchmark file
- Regulatory calculator REST contract (`schemas/calculator/openapi.yaml`), stub server (`sora-tools calculator-stub`) and contract tests (`python/tests/contract/`)
- Export specification and source data dictionary format
- A fixed test scenario and golden results in `tests/golden/20260630/`
- Hand-computed unit cases for EBA Boxes 3–9

## Phase 1 - Core skeleton and input

Deliver:

- CMake project
- SIM reader for partitioned CSV and Parquet (header mapping, exact decimal parsing, nulls), with bindings generated from the schema
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

## Phase 5b - Agent and calculator integration

Deliver:

- `sora-mcp` (describe, profile_source, test_mapping, validate_sim, reconcile)
- REST calculator client (batching, retries, idempotency, replay cache), tested against the stub
- `/v1/parameters/credit` and `/v1/credit-risk/irb` in the pipeline
- `explain_result`, `diff_runs`

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

- SA and output floor through the calculator; Parquet request/response bodies
- Market risk revaluation and CCR
- Explicit SIMD
- Alternative allocators
- NUMA-aware execution
- Memory-mapped columnar input
- Custom compact binary format

## Release philosophy

Keep the core engine small. Add features only when they preserve deterministic behaviour, bounded memory use, and measurable throughput.
