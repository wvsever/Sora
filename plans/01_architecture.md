# Architecture Plan

## Objective

Build a small native engine with explicit ownership, stable data contracts, and minimal runtime overhead. It reads the partitioned accounting dataset format of `tests/data/` and produces EBA-style stress projections.

## Core modules

### 1. Input

Responsibilities:

- Read Sora Input Model (SIM) tables (Parquet or CSV, partitioned by `entity_id`, see `10_input_model_and_mapping.md`). Customer layouts, including the raw test dataset, are mapped to SIM with SQL beforehand. The engine never parses them.
- Read only the tables and columns that enabled modules declare
- Map columns by name, never by position
- Validate against the generated SIM schema (type, nullability, keys, constraints)
- Convert external representations into compact internal records (exact decimal → scaled integer, see `02_data_model.md`)
- Read the reporting date, base currency and SIM version from the run manifest

Design rules:

- Prefer sequential reads
- Use buffered I/O or memory mapping
- Parse directly into final internal types where possible
- Process partitions in parallel. Many small files are the norm.
- Keep the `.7z` archive and SQL mapping out of the engine. `sora map` (DuckDB-based tooling) produces the SIM files first.

### 2. Reference and party store

Responsibilities:

- Load dimensions (entity, currency, country, sector, NACE, product, portfolio, GL account, rating scale)
- Load counterparties, ratings, collateral, collateral allocation and guarantees into frozen, dense-indexed arrays
- Resolve business keys (`CPTY-…`, `COLL-…`) to dense indices once

### 3. Exposure assembly

Responsibilities:

- Join credit contracts (`contract_loan`, `contract_commitment`, `contract_lease`, `contract_security_position` at AC/FVOCI) with counterparty, rating, collateral, guarantees and the latest `impairment_allowance`
- Produce one denormalised `Exposure` per contract (`02_data_model.md`)
- Report orphans (a contract without a counterparty, an allocation without a contract) as diagnostics

### 4. Risk parameters and calculators

Responsibilities:

- Attach starting-point PD, TR, LGD, LRLT and CCF per contract or segment, from an external file, derived calibration, or benchmarks (`09_risk_parameters.md`)
- Run calibration (`sora calibrate`) from the history tables. This is a separate command whose output is a reviewable parameter file.
- Call the external regulatory calculator over REST (`schemas/calculator/openapi.yaml`) for parameter models, IRB, SA and output floor (`11_integrations.md`)

### 5. Scenario

Responsibilities:

- Load the normalised macro path (converted from the ESRB/ECB xlsx files in `docs/`)
- Load satellite model coefficients and benchmark tables
- Resolve overlays
- Validate ranges and required fields
- Compile into immutable per-scenario, per-year, per-segment parameter tables (`03_scenario_engine.md`)

### 6. Segmentation

Responsibilities:

Map each exposure to integer stress segment IDs at load time:

- EBA CR_SCEN portfolio (sector × NFC SME/CRE split × household purpose)
- Country (top 10 plus Other)
- NACE section (CR_SECTOR)
- Product and portfolio
- Collateral form
- Maturity band
- Currency
- Rating grade (optional)

Segmentation tables are data (`segments.csv`), not code.

### 7. Stress rules and projection

Responsibilities:

- Apply the stage-flow projection and provision formulas (EBA Boxes 3–9) per contract and year
- Support additive, multiplicative, lookup-table, satellite and transition-matrix rules
- Enforce constraints: no cure from S3, no S3 provision release, static balance sheet, POCI static
- Avoid virtual dispatch in hot loops

Preferred approaches:

- `std::variant` or tagged structs for rule types
- Precompiled lookup tables
- Function objects instantiated before execution

### 8. Transformation

Responsibilities:

- Produce the stressed exposure state per year (stage masses, exposure, provisions, collateral value)
- Generate compact events
- Preserve traceability to the original records

The transformation layer does not allocate per record.

### 9. Validation

Responsibilities:

- Range checks
- Reconciliation checks against the source (contract vs `impairment_allowance` vs `gl_balance`)
- Invariant checks
- Segment-level equality with the EBA formulas
- Portfolio-level consistency checks

Validation supports configurable levels:

- `off`
- `fast`
- `full`

### 10. Aggregation

Responsibilities:

- Stocks per stage and segment (Exp S1/S2/S3/POCI, Prov stocks)
- Flows per transition
- Impairment P&L per year
- Coverage ratios, LTV and average maturity per stage
- Totals by entity, country, sector and portfolio

Use thread-local accumulators followed by deterministic reduction.

### 11. Output

Responsibilities:

- Write stressed contract state per year (optional, high volume)
- Write the event stream (optional)
- Write aggregated results in EBA CR_SCEN / CR_SCEN_OFF_BS / CR_SECTOR layout (long CSV)
- Write the parameter file used (for audit)
- Write diagnostics

Support:

- CSV for inspection and templates
- NDJSON for interoperability
- A compact binary format for performance

## Module boundaries and phase scope

| Module | Phase 1 | Later |
|---|---|---|
| Credit risk (IFRS 9 provisions, stage flows) | Yes | IRB/STA REA (PDreg, LGDreg), output floor |
| NII | — | Repricing and margins from `contract_cashflow`, deposits and curves (EBA ch. 4) |
| Market risk | — | Revaluation of FV positions with the ECB shocks |
| Funding / liquidity | — | Deposit outflows by `deposit_type`, DGS coverage and operational flags |
| CCR | — | Default of the largest counterparties |

## Around the core (tooling, not engine)

| Component | Role |
|---|---|
| `schemas/sim/` | Single source of truth for the input model. Generates C++ bindings, docs, DDL and the LLM description. |
| `sora map` / `sora validate` | Run customer mapping SQL (embedded DuckDB) on exported source files, and validate the SIM output |
| `sora-mcp` | MCP server for AI agents: model description, profiling, mapping tests, validation, runs, explanations |
| Calculator client | REST client for the regulatory calculator (batching, retries, replay cache); stub server for tests |

## Architectural constraints

- No global mutable state
- No hidden allocations in hot paths
- No exception-based normal control flow
- No lock contention on record processing paths
- No runtime reflection
- No dependency injection framework
- No spreadsheet (xlsx) parsing in the engine. Conversion happens in tools.
