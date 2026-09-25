# Integrations Plan (MCP, regulatory calculators, models)

## Objective

Let Sora work inside the customer's landscape:

- AI agents help set it up and use it, through MCP.
- It exchanges data with the customer's risk models and regulatory calculators through stable, deterministic interfaces.

Two different kinds of integration:

| | Purpose | Protocol |
|---|---|---|
| AI agent ↔ Sora | Build mappings, validate, explain, run | **MCP** (Model Context Protocol) |
| Sora ↔ calculators / models | Compute PD, LGD, REA, EL and similar | **Plain APIs and file exchange** (not MCP) |

MCP is designed for agents. Engine-to-calculator calls must be deterministic, bulk-capable and auditable, and must not depend on an LLM.

## 1. Sora MCP server

A separate process (`sora-mcp`), shipped with Sora and run on the customer's premises next to the engine. It wraps the Sora tooling (`sora map`, `sora validate`, `sora run`). No engine logic lives in it.

### Resources (read-only context for the agent)

- SIM schema and documentation (the LLM-oriented description generated from `schemas/sim/`)
- Methodology descriptions (EBA credit risk flows, parameter definitions, segmentation)
- The reference mapping (`mappings/cppbank/`) as a worked example
- Run results and diagnostics (aggregates only)

### Tools

| Tool | Does |
|---|---|
| `describe_sim_table` / `describe_sim_column` | Definitions, constraints, regulatory references |
| `profile_source` | Profile a customer source table: columns, types, null rates, distinct values, samples when allowed |
| `test_mapping` | Run a candidate mapping SQL on a sample or full data, and return validation results |
| `validate_sim` | Full SIM validation report |
| `reconcile` | Compare SIM totals with customer control totals (GL, FINREP) |
| `run_scenario` | Start a run with a given configuration, and return the run ID and status |
| `explain_result` | Break down a result (segment, year) into drivers: exposure, TR, LGD, collateral |
| `diff_runs` | Compare two runs or two mapping versions |

### Safety and governance

- Runs locally. The customer chooses the agent and the LLM provider.
- Data minimisation by default: tools return metadata, statistics and validation messages. Returning raw rows (`profile_source` samples) is a customer setting that is off by default.
- Read-only on source systems. Mapping SQL is executed through DuckDB with read-only attachments, statement allow-listing and row and time limits.
- Mappings written by an agent are saved as drafts. Promotion to production requires human approval, which is recorded in the mapping release (see `10_input_model_and_mapping.md`).
- Every tool call is logged (who, what, when, input fingerprint) for audit.
- Agents never change engine results directly. They can only change versioned inputs (mapping, configuration), which go through validation.

## 2. Regulatory calculators and risk models

Sora keeps the stress mechanics. Regulatory formulas and customer risk models sit behind **ports**: stable interfaces with a built-in reference implementation and optional external adapters.

### Ports

| Port | Input | Output | Built-in reference |
|---|---|---|---|
| `ParameterModel` | Exposure attributes, segment, macro path per year | PD/TR, LGD/LR, CCF per year | Satellite model evaluator (`03_scenario_engine.md`), calibration (`09_risk_parameters.md`) |
| `CreditRiskSA` | Exposure class, risk-weight drivers, collateral, CCF | Exposure value, RW, REA | CRR3 SA subset (phase-wise) |
| `CreditRiskIRB` | PD, LGD, M, EAD, asset class, turnover | K, RW, REA, EL | CRR Art. 153/154 formulas (simple, fully implemented) |
| `OutputFloor` | SA and IRB REA | Floored TREA | CRR3 Art. 92(3) |
| `Impairment` | Stage flows, parameters | Provisions | EBA Boxes 3–9 (core, always internal) |

### Adapter types

- **In-process (C++)**: the built-in reference implementations. Fast, deterministic, used in tests.
- **Batch file exchange**: Sora writes a request dataset (Parquet), the customer's calculator (e.g. their COREP engine or IRB model platform) processes it, and Sora reads the response. This is the most robust option for bank environments and large volumes.
- **Service call**: gRPC or REST with batched requests, for calculators exposed as services. Calls are idempotent, and requests carry run ID, scenario, year and input fingerprint.

### Rules

- Every external result is stored with its request fingerprint. A rerun with identical inputs can replay stored results without calling the calculator again, which keeps results reproducible.
- External results pass the same validation as internal ones (ranges, completeness, one result per request row).
- The built-in reference calculators remain available to cross-check external ones (`diff` report). This also supports the customer's own validation function.
- Regulatory parameter sets are versioned data (`paramSet`, e.g. `EU_CRR3_2025-01-01`, as in the test data manifest), not code constants.

## 3. Phasing

| Phase | Integration |
|---|---|
| 1–3 | SIM schema v1, reference mapping, `sora validate`; `Impairment` internal; `ParameterModel` table-driven |
| 4–5 | `sora-mcp` (describe, profile, test_mapping, validate); satellite evaluator; batch file adapter for `ParameterModel` |
| 6–7 | `CreditRiskIRB` reference, `CreditRiskSA` subset, `OutputFloor`; batch and service adapters; `explain_result`, `diff_runs` |
| 8 | Full CRR3 SA coverage as needed by customers |
