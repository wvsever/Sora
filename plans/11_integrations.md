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

### Sora client behaviour (as built)

`include/sora/calculator.hpp`, `src/calculator.cpp`. HTTP with cpp-httplib, JSON with nlohmann/json (both vendored in `third_party/`), TLS with OpenSSL.

- **Capabilities first.** `GET /v1/capabilities` at run start. The run fails before any record is sent if the calculator does not list the calculation (`irb`) or the parameter set (`EU_CRR3_2025-01-01`). An unreachable calculator fails after 3 attempts, unless the replay cache holds its capabilities (see below).
- **Batching.** Each call (one scenario point) is split into balanced batches of at most `maxRecordsPerSyncRequest` records, sent to the synchronous endpoint. `--calculator-batch <n>` sets a larger batch size (capped by `maxRecordsPerRequest`); batches above the sync limit are sent as jobs (`POST /v1/jobs`, then `GET /v1/jobs/{id}` polled with a growing interval, then `GET /v1/jobs/{id}/result`; job timeout 1 hour).
- **Parallelism.** All batches of all scenario points share one pool of `maxConcurrentRequests` worker threads (1 if not stated), each with its own connection. Results are written into fixed slots, so output does not depend on timing.
- **Retries.** 429, 502, 503, 504 and connection errors or timeouts are retried up to 8 attempts per call, with exponential backoff (0.25 s doubling, at most 30 s). A `Retry-After` header in seconds replaces the computed delay. A retry always sends the same body and the same `Idempotency-Key`. Other statuses fail the run with the problem details (RFC 9457 `title`/`detail`).
- **Idempotency keys.** `Idempotency-Key` = `context.requestId` = an RFC 9562 version-8 UUID from SHA-256 over the run id, the calculation and the canonical request body without `requestId`. The run id defaults to `sora|<scenario name>|<reference date>|<mapping release>`, so a rerun with the same inputs sends the same keys, and the calculator can return its stored responses.
- **Exact decimals.** Sora holds amounts as cents and probabilities as integers scaled by 10^9, and writes them as decimal strings: amounts with 2 decimals, probabilities with 9, maturity in years with 4, turnover in EUR million with 8 (exact, from cents). Responses are parsed from the decimal strings straight into scaled integers (half-to-even rounding beyond the target scale), never through a binary float. REA and EL are summed in cents, and `rea.csv` prints those sums.
- **Canonical JSON.** Object keys are sorted and there is no whitespace, so the same records always give the same bytes, key and fingerprint. `context.inputFingerprint` is `sha256:<hex>` of the records array.
- **Response validation.** `meta.requestId` echoes the key. `meta.calculator` name and version equal the capabilities, and `meta.paramSet` equals the request's. There is exactly one result per record, with `recordId` echoed in request order. `status` is `ok` or `rejected`. For `ok`: `rea` and `expectedLoss` are present, match the Decimal pattern and are not negative; `riskWeight` is not negative; `pdApplied`/`lgdApplied` are in [0, 1]. Any violation fails the run, and nothing is cached.
- **Replay cache** (`--calculator-cache <dir>`). Validated responses are stored as `<dir>/<calculator name>_<version>/irb/<sha256 of request body>.json` (written to a temporary file, then renamed). Capabilities are stored under `<dir>/capabilities/`, keyed by URL. A rerun with identical inputs and the same calculator version reads every batch from the cache. If the calculator is unreachable, the run continues offline from the cached capabilities, and a batch missing from the cache fails the run. A new calculator version, or any changed input, is a cache miss.
- **Security.** `https://` URLs verify the server certificate against the system store or `--calculator-ca <file>`. mTLS uses `--calculator-cert <file> --calculator-key <file>`. An OAuth2 bearer token is read from the `SORA_CALCULATOR_TOKEN` environment variable only, never from the command line. Plain `http://` is for the local stub.
- **Diagnostics.** `CALC-000` (info) summarises batches, HTTP requests, retries, jobs and cache hits. `CALC-001` (info): offline replay. `CALC-002` (warning): PiT proxy used (below). `CALC-003` (info): exposures not sent (POCI, zero EAD). `CALC-010` (warning): records rejected, with count and examples. Rejected records are left out of `rea.csv` amounts but counted in `records_rejected`, and the run continues. `CALC-011` (error): every record rejected. The run then stops without writing results.

### IRB REA projection (`sora run --calculator <url>`)

`src/rea.cpp`. It runs after the IFRS 9 projection and does not change it.

- **Records.** One per in-scope exposure in stage 1, 2 or 3, per scenario point: actual/0, baseline/1–3, adverse/1–3. POCI exposures and exposures with zero gross carrying amount are not sent. `recordId` = `exposure_id|scenario|year`. `approach` = `airb`.
- **EAD.** The exposure's gross carrying amount at the reference date, in the reporting currency (converted exactly with the SIM FX rate), for every year (static balance sheet). Off-balance amounts and CCFs are not included yet.
- **PD / LGD.** From the projection's parameter path of the exposure's segment for that scenario and year, or its own path when it has exposure-level parameters:
  - stage 1: `pd` = `pd12m_s1`, `lgd` = `lgd_s1`
  - stage 2: `pd` = `pd12m_s2`, `lgd` = `lgd_s2`
  - stage 3: `isDefaulted` = true, `pd` = 1, `lgd` = `elbe` = `lgd_s3`
- **Maturity.** Residual maturity to `maturity_date` in years (actual days / 365.25), clipped to [1, 5]. 2.5 when unknown.
- **Firm size and financial sector.** `annualTurnoverEurMillions` from `sim_counterparty.annual_turnover_eur`, for corporate classes (SME correlation adjustment, Art. 153(4)). `isLargeFinancialSectorEntity` for credit institutions and other financial corporations with `total_assets_eur` ≥ EUR 70 billion (Art. 142(1)(4)).
- **Output.** `rea.csv` (`segment, scenario, year, ead, rea, expected_loss, records_ok, records_rejected`; `ead` of accepted records). `summary.json` gets a `rea` member with the calculator name and version, the parameter set, and totals per scenario point. Without `--calculator`, the outputs are unchanged.

**Caveat: PiT parameters as a proxy.** IRB own funds requirements need regulatory parameters: through-the-cycle PD with floors, downturn LGD, and ELBE / LGD in-default. The EBA projection works with 12-month point-in-time IFRS 9 parameters. Unless the customer supplies regulatory values, Sora sends the PiT parameters as a proxy (`CALC-002`). This understates REA in benign years and overstates its cyclicality. For defaulted exposures, LGD = ELBE gives K = 0. If the risk-parameter source (`sim_risk_parameter` or `--parameters`) has `pd_reg` / `lgd_reg`, those are sent instead, field by field. Precedence: exposure row, then segment hierarchy (specific to general). A scenario point without a value uses the actual/0 value, since through-the-cycle parameters do not move with the scenario unless supplied. Stage 3 then uses `lgd` = `lgd_reg` and `elbe` = `lgd_s3`.

**Exposure-class mapping** (CRR Art. 147), from the EBA sector, the SME flag and the household purpose:

| SIM / EBA portfolio | IRB `exposureClass` | Note |
|---|---|---|
| CB (central banks) | `central_governments` | Art. 147(2)(a) |
| GG (general governments) | `central_governments` | Simplification: regional/local governments and PSEs are treated as central governments. |
| CI (credit institutions) | `institutions` | `isLargeFinancialSectorEntity` when total assets ≥ EUR 70bn |
| OFC (other financial corporations) | `corporates_general` | Decision: CRR institutions are credit institutions and investment firms. The SIM sector does not separate investment firms, so all OFCs are corporates (with the large-FSE flag). |
| NFC, `is_sme` = true | `corporates_sme` | With `annualTurnoverEurMillions` when available |
| NFC, other (incl. unknown SME flag) | `corporates_general` | Turnover sent when available |
| HH, house purchase (loans) | `retail_residential_mortgage` | |
| HH, consumption / other, and household debt securities | `retail_other` | QRRE and retail SME are not identified from the SIM yet. |

Specialised lending, equity, purchased receivables, SA exposures and the output floor are not mapped yet.

### What stays inside Sora

- The IFRS 9 impairment projection (EBA Boxes 3–9). This is the core of the product.
- Stage flows, collateral repricing, aggregation, templates.
- An optional in-process IRB formula for cross-checking the calculator. It is not the production path.

### Stubs for testing

`python/sora_tools/calculator_stub.py` (`sora-tools calculator-stub`) is a dependency-free stub server (Python standard library) that implements the full OpenAPI contract, in three modes:

| Mode | Behaviour | Used for |
|---|---|---|
| `fixed` | Deterministic canned values from a lookup file (per exposure class / segment) | Engine and integration tests, golden results |
| `formula` | Simple reference formulas (IRB Art. 153 curve, flat SA risk weights per class, fixed floor factor, parameter = starting value × scenario multiplier) | Plausible end-to-end numbers, demos |
| `faults` | Injected latency, 429/503, timeouts, rejected records, wrong record count, out-of-range values | Client retry, idempotency and validation tests |

Also delivered:

- Contract tests: the stub and any real calculator can be run against `python/tests/contract/` (`SORA_CALCULATOR_URL=… pytest python/tests/contract -m "not stub_only"`), which checks the OpenAPI schema, idempotency, determinism and record echo. You can use it to verify your calculator adaptation.
- An in-memory stub in the C++ test suite (`tests/cpp/test_calculator.cpp`), so unit tests need no network: batching, retries, jobs, replay cache, validation.
- Engine integration tests against the stub in all three modes (`python/tests/test_calculator_engine.py`, ctest `sora_calculator`). `--reject <regex>` (or `start_background(reject=...)`) makes the stub reject records whose `recordId` matches, e.g. `7\|adverse\|3$`.

## 3. Phasing

| Phase | Integration |
|---|---|
| 0–1 | OpenAPI contract v0.1, stub server (`fixed`, `faults`), contract tests |
| 1–3 | SIM schema v1, export spec, reference mapping, `sora-tools map` / `sora-tools validate`; `Impairment` internal |
| 4–5 | REST client (batching, retries, replay cache) and IRB REA projection (done); `/v1/parameters/credit` integration; stub `formula` mode |
| 5b | `sora-mcp` (describe, profile, test_mapping, validate) |
| 6–7 | IRB, SA and output floor through the calculator; `explain_result`, `diff_runs`; Parquet bodies |
