# Input Model and Mapping Plan

## Objective

Give Sora one canonical, well-documented input data model: the **Sora Input Model (SIM)**. Customers bring their data into it with SQL. The customer's own staff or an AI agent writes that SQL, working from the model description.

## Principles

- Sora reads only SIM. Customer layouts are never parsed by the engine.
- Mapping is SQL: declarative, reviewable, runs on the customer's own platform, and familiar to bank data teams.
- The model description is the product. It must be precise enough for a person or an AI agent to write a correct mapping without talking to us.
- Mapping SQL is code. It is versioned, reviewed, tested and approved by the customer (BCBS 239 data lineage, model-risk governance). An AI-generated mapping never runs unreviewed in production.
- No customer data leaves the customer's premises.

## Sora Input Model (SIM)

A small, stress-test-oriented model. The 54-table test dataset is a source layout. SIM needs only a subset of it, flattened:

| SIM table | Grain | Main content |
|---|---|---|
| `sim_entity` | Legal entity | Country, functional currency, consolidation |
| `sim_counterparty` | Counterparty | Country, ESA sector, NACE, SME indicators, natural person, group |
| `sim_rating` | Counterparty × source | Internal / external grade, scale |
| `sim_exposure` | Contract | Type (loan, commitment, lease, debt security), product, portfolio, currency, gross carrying amount, drawn/undrawn, stage, allowance, rates, maturity, repricing, flags (forborne, watchlist, credit-impaired, POCI) |
| `sim_collateral` | Collateral item | Form, property country and type, value, valuation date, lien rank |
| `sim_collateral_allocation` | Exposure × collateral | Allocated amount, rank |
| `sim_guarantee` | Protection item | Form, guarantor, amount, protected exposure |
| `sim_stage_history` | Exposure × period | Stage, gross carrying amount, allowance (for calibration) |
| `sim_default_history` | Defaulted exposure | Default date, EAD at default, recoveries, costs, write-offs (for LGD) |
| `sim_cashflow` | Exposure × date | Contractual flows (NII module) |
| `sim_deposit` | Contract | Funding module |
| `sim_risk_parameter` | Exposure or segment × scenario × year | Customer PD/TR/LGD/LR/CCF (`09_risk_parameters.md`) |
| `sim_market_data` | Curve / FX point | Curves and FX at the reference date |

The exact column list is defined in the schema, not in this plan.

### Model description (the schema)

A machine-readable schema, `schemas/sim/*.yaml`, generates everything else:

- For every table: grain, primary key, foreign keys, partitioning, and which Sora modules require it
- For every column:
  - type, unit, scale, nullability, allowed values (enums with definitions)
  - value ranges and cross-field constraints (e.g. `drawn ≤ committed`, `stage = S3 ⇒ credit_impaired`)
  - business definition in plain language
  - regulatory reference (IFRS 9 paragraph, CRR article, FINREP/COREP/AnaCredit data point)
  - typical source systems and common pitfalls
  - a worked example
- Semantic hints for mapping: "equals FINREP F 18.00 gross carrying amount", "sign convention: assets positive"

The schema generates:

- C++ reader bindings and validation code (a single source of truth)
- Human documentation (HTML/Markdown)
- An LLM-oriented description: a compact, complete text version for agent context (see `11_integrations.md`)
- DDL for DuckDB / PostgreSQL / Snowflake / SQL Server, so customers can materialise SIM on their platform
- Empty templates and a validation profile

The schema is versioned (`sim_version`). Breaking changes require a major version and a migration note.

## Mapping workflow

```text
customer source systems / DWH
        │  SQL (customer-owned mapping, one view per SIM table)
        ▼
SIM tables (Parquet or CSV, partitioned by entity)
        │  sora validate
        ▼
Sora engine
```

1. The customer receives the SIM schema, documentation and DDL.
2. Mapping SQL is written per SIM table, by hand or with an AI agent through the Sora MCP server (`11_integrations.md`).
3. The mapping runs on the customer's platform. Alternatively, the `sora map` tool runs it with embedded DuckDB, which reads CSV, Parquet, and database connections through DuckDB extensions.
4. `sora validate` checks the output against the schema: types, keys, referential integrity, constraints, and reconciliation totals (e.g. SIM gross carrying amount vs the GL total provided by the customer).
5. The mapping, its validation report and the data fingerprints are stored together as an auditable mapping release.

DuckDB lives in the tooling (`sora map`, `sora validate`, MCP server), not in the C++ core engine. The core engine reads only SIM files.

## Interchange format

- **Parquet** is the primary SIM format: typed, columnar, compressed, and exact decimals with `DECIMAL(18,2)`.
- **CSV** remains supported, with the same conventions as the test data (see `tests/data/README.md`).
- Partitioned by `entity_id`. `period` is used for history tables.

## Reference mapping (proof of the approach)

`mappings/cppbank/*.sql` maps the test dataset (`tests/data/20260630.7z`) to SIM. It serves as:

- the worked example shipped to customers
- the test that the schema is complete (every SIM column is either mappable from a realistic source or explicitly optional)
- the input for all golden tests

Mapping rules that were decided in `09_risk_parameters.md` (sector from ESA 2010, household purpose from product code, and so on) live in this SQL, not in the engine.

## Mapping quality checks

- Coverage: the share of source exposure (by GL reconciliation) that reaches `sim_exposure`
- Completeness per required column, per module
- Distribution checks against the previous run (stage mix, coverage ratios, country and sector mix), flagging jumps
- Unmapped codes (unknown product or sector values) reported with counts, never silently dropped
