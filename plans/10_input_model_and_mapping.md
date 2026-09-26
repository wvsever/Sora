# Input Model and Mapping Plan

## Objective

Give Sora one canonical, well-documented input data model: the **Sora Input Model (SIM)**. Customers bring their data into it with SQL. The customer's own staff or an AI agent writes that SQL, working from the model description.

## Principles

- Sora reads only SIM. Customer layouts are never parsed by the engine.
- Mapping is SQL: declarative, reviewable, runs on the customer's own platform, and familiar to bank data teams.
- The model description is the product. It must be precise enough for a person or an AI agent to write a correct mapping without talking to us.
- Mapping SQL is code. It is versioned, reviewed, tested and approved by the customer (BCBS 239 data lineage, model-risk governance). An AI-generated mapping never runs unreviewed in production.
- No customer data leaves the customer's premises, and neither Sora nor the vendor connects to customer databases. The customer exports source data to files, and mapping runs on those files.

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
| `sim_stage_history` | Exposure × period | Stage, gross carrying amount (optional principal outstanding as a proxy), allowance (for calibration and the prior-year Actual rows of CR_SCEN / CR_SECTOR) |
| `sim_default_history` | Defaulted exposure | Default date, EAD at default, recoveries, costs, write-offs (for LGD) |
| `sim_cashflow` | Exposure × date | Contractual flows (NII module; not yet in the schema) |
| `sim_deposit` | Contract | Deposits received: type (sight / notice / term), rate and repricing, DGS and operational flags (NII and funding modules) |
| `sim_debt_issued` | Instrument | Debt securities issued: type, carrying and nominal amount, coupon and repricing (NII module) |
| `sim_risk_parameter` | Exposure or segment × scenario × year | Customer PD/TR/LGD/LR/CCF (`09_risk_parameters.md`) |
| `sim_fx_rate`, `sim_rate_curve` | Currency × date; curve × tenor | FX rates; risk-free and credit spread curves at the reference date (NII module, `13_nii.md`) |

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

## Mapping workflow (export-based)

```text
customer source systems / DWH
        │  customer export job (their tooling, their access rights)
        ▼
source export files (Parquet or CSV, one folder per source table) + source data dictionary
        │  mapping SQL, run by `sora-tools map` (embedded DuckDB, files only)
        ▼
SIM tables (Parquet or CSV, partitioned by entity)
        │  sora-tools validate
        ▼
Sora engine
```

1. **Export.** The customer exports the needed source tables to files, using their own tooling and access rights. Sora publishes an *export specification*: formats and conventions (as in `tests/data/README.md`), and the minimal source content per SIM table. A *source data dictionary* comes with the export (table and column names, types, descriptions, code lists). The dictionary contains no data values, so it can be shared with a mapping agent even when the data itself cannot.
2. **Map.** Mapping SQL is written per SIM table against the exported files, by hand or with an AI agent through `sora-mcp` (`11_integrations.md`). The agent works from the SIM schema and the source dictionary, plus profiling statistics computed locally.
3. **Run.** `sora-tools map` executes the SQL with embedded DuckDB directly on the export folder (`read_parquet('export/contract_loan/**/*.parquet')`). No database connection is involved. DuckDB runs with external access disabled beyond the export directory.
4. **Validate.** `sora-tools validate` checks the output against the schema: types, keys, referential integrity, constraints, and reconciliation totals (e.g. SIM gross carrying amount vs control totals exported with the data).
5. **Release.** The mapping SQL, source dictionary version, validation report and export and SIM fingerprints are stored together as an auditable mapping release.
   `mapping.yaml` may carry `status: draft | review | production` (default `draft`). Reconciliation controls (SIM totals vs source control totals) live in `reconciliation.yaml` of the mapping, so they are part of the release. An agent working through `sora-mcp` cannot run a production mapping (by status or by path) without a recorded human approval of that exact release (`plans/11_integrations.md`).

Everything runs where the export is: normally on the customer's premises. If a customer chooses to send an export to us (e.g. anonymised onboarding samples), the same tooling runs unchanged. That route is a contractual decision, not a technical requirement.

The test dataset in `tests/data/` is exactly such an export (Hive-partitioned CSV plus a column-type dictionary). The reference mapping runs on it the same way.

Customers who prefer to run the mapping inside their own warehouse can use the generated DDL and run the same SQL there. The output is identical SIM files.

DuckDB lives in the tooling (`sora-tools map`, `sora-tools validate`, MCP server), not in the C++ core engine. The core engine reads only SIM files.

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
