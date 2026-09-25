# Integrations Plan (MCP, regulatory calculators, models)

## Objective

Let Sora work inside the customer's landscape:

- AI agents help set it up and use it, through MCP.
- It exchanges data with the customer's risk models and regulatory calculators through stable, deterministic interfaces.

Two different kinds of integration:

| | Purpose | Protocol |
|---|---|---|
| AI agent ↔ Sora | Build mappings, validate, explain, run | **MCP** (Model Context Protocol) |
| Sora ↔ regulatory calculator / models | Compute PD, LGD, REA, EL and similar | **REST** (`schemas/calculator/openapi.yaml`), not MCP |

MCP is designed for agents. Engine-to-calculator calls must be deterministic, bulk-capable and auditable, and must not depend on an LLM.

**Data access principle:** Sora (the vendor, and any agent working for the vendor) never connects to customer databases. Customers export source data to files, and all mapping and profiling runs on those files (see `10_input_model_and_mapping.md`).

## 1. Sora MCP server

A separate process (`sora-mcp`), shipped with Sora and run on the customer's premises next to the engine. It wraps the Sora tooling (`sora-tools map`, `sora-tools validate`, `sora run`). No engine logic lives in it.

### Resources (read-only context for the agent)

- SIM schema and documentation (the LLM-oriented description generated from `schemas/sim/`)
- Methodology descriptions (EBA credit risk flows, parameter definitions, segmentation)
- The reference mapping (`mappings/cppbank/`) as a worked example
- Run results and diagnostics (aggregates only)

### Tools

| Tool | Does |
|---|---|
| `describe_sim_table` / `describe_sim_column` | Definitions, constraints, regulatory references |
| `profile_source` | Profile an exported source file: columns, types, null rates, distinct values, samples when allowed |
| `test_mapping` | Run a candidate mapping SQL on a sample or full data, and return validation results |
| `validate_sim` | Full SIM validation report |
| `reconcile` | Compare SIM totals with customer control totals (GL, FINREP) |
| `run_scenario` | Start a run with a given configuration, and return the run ID and status |
| `explain_result` | Break down a result (segment, year) into drivers: exposure, TR, LGD, collateral |
| `diff_runs` | Compare two runs or two mapping versions |

### Safety and governance

- Runs locally. The customer chooses the agent and the LLM provider.
- Data minimisation by default: tools return metadata, statistics and validation messages. Returning raw rows (`profile_source` samples) is a customer setting that is off by default.
- Files only: mapping SQL runs in DuckDB over the exported source files, with no database connections, external access disabled, statement allow-listing, and row and time limits.
- Mappings written by an agent are saved as drafts. Promotion to production requires human approval, which is recorded in the mapping release (see `10_input_model_and_mapping.md`).
- Every tool call is logged (who, what, when, input fingerprint) for audit.
- Agents never change engine results directly. They can only change versioned inputs (mapping, configuration), which go through validation.

## 2. Regulatory calculators and risk models (REST)

Sora keeps the stress mechanics. Regulatory formulas and risk-parameter models are computed by an **external regulatory calculator over REST**. Sora defines the interface, and the calculator implements it.

**Interface:** `schemas/calculator/openapi.yaml` (OpenAPI 3.1, version 0.1.0, a proposal open to change).

| Endpoint | Computes |
|---|---|
| `GET /v1/capabilities` | Calculator name and version, supported calculations, parameter sets (`EU_CRR3_2025-01-01`), batch limits, formats |
| `GET /v1/health` | Liveness |
| `POST /v1/credit-risk/irb` | Correlation, maturity adjustment, K, RW, REA, EL per exposure (CRR Art. 153–154, 158) |
| `POST /v1/credit-risk/sa` | Exposure value, CCF, RW, REA, LTV per exposure (CRR3 SA) |
| `POST /v1/credit-risk/output-floor` | Floor factor, binding flag, floored TREA per reporting unit |
| `POST /v1/parameters/credit` | PD / TR / LGD / LR / CCF per exposure or segment, per scenario year, optionally from a macro path |
| `POST /v1/jobs`, `GET /v1/jobs/{id}`, `GET /v1/jobs/{id}/result` | The same calculations asynchronously, for large batches |

Contract rules (normative, in the spec):

- **Batch-first:** many records per call. Sora sizes batches from `/v1/capabilities` limits and switches to jobs above the sync limit.
- **Exact decimals:** amounts and probabilities are decimal strings, never JSON floats.
- **Idempotent:** `Idempotency-Key` equals `requestId`, so retries are safe.
- **Deterministic:** the same request, `paramSet` and calculator version give the same result. The calculator version is returned in every response and becomes part of Sora's result fingerprint.
- **Record-level errors:** a batch returns one result per record (`ok` or `rejected` with coded messages). Request-level errors use RFC 9457 problem details.
- **Formats:** JSON is required. Parquet request and response bodies are optional for bulk.
- **Security:** on-premises deployment. mTLS and/or OAuth2 client credentials.

### Sora client behaviour

- Reads capabilities at run start. Fails fast if the required calculation or `paramSet` is not supported.
- Parallel batches up to `maxConcurrentRequests`. Timeouts, and retries with exponential backoff on 429/503 (same idempotency key).
- Validates every response: one result per record, echoed `recordId`, value ranges, and the same checks as internal results.
- Stores responses keyed by request fingerprint and calculator version. A rerun with identical inputs replays the stored results without calling the calculator again. This keeps runs reproducible and auditable, and allows offline reruns.
- Rejected records are reported in diagnostics. The run policy (fail-fast or accumulate) decides whether the run continues.

### What stays inside Sora

- The IFRS 9 impairment projection (EBA Boxes 3–9). This is the core of the product.
- Stage flows, collateral repricing, aggregation, templates.
- An optional in-process IRB formula for cross-checking the calculator. It is not the production path.

### Stubs for testing

`tests/stubs/calculator/` holds a stub server that implements the full OpenAPI contract, in three modes:

| Mode | Behaviour | Used for |
|---|---|---|
| `fixed` | Deterministic canned values from a lookup file (per exposure class / segment) | Engine and integration tests, golden results |
| `formula` | Simple reference formulas (IRB Art. 153 curve, flat SA risk weights per class, fixed floor factor, parameter = starting value × scenario multiplier) | Plausible end-to-end numbers, demos |
| `faults` | Injected latency, 429/503, timeouts, rejected records, wrong record count, out-of-range values | Client retry, idempotency and validation tests |

Also delivered:

- Contract tests: the stub and any real calculator can be run against `tests/contract/`, which checks the OpenAPI schema, idempotency, determinism and record echo. You can use it to verify your calculator adaptation.
- An in-memory stub in the C++ test suite, so unit tests need no network.

## 3. Phasing

| Phase | Integration |
|---|---|
| 0–1 | OpenAPI contract v0.1, stub server (`fixed`, `faults`), contract tests |
| 1–3 | SIM schema v1, export spec, reference mapping, `sora-tools map` / `sora-tools validate`; `Impairment` internal |
| 4–5 | REST client (batching, retries, replay cache); `/v1/parameters/credit` integration; stub `formula` mode |
| 5b | `sora-mcp` (describe, profile, test_mapping, validate) |
| 6–7 | IRB, SA and output floor through the calculator; `explain_result`, `diff_runs`; Parquet bodies |
