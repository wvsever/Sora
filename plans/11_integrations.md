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

### As built (phase 5b)

`python/sora_tools/mcp_server.py`, console script `sora-mcp` (stdio), optional extra `pip install -e "python[mcp]"`
(official MCP Python SDK, 1.x `FastMCP` or 2.x `MCPServer`). The tools are plain Python (`SoraTools`) and are tested
without the SDK; `sora-mcp call <tool> '<json>'` calls one without MCP. Usage and options: `python/README.md`.

| Tool | Built on |
|---|---|
| `describe` (table, column, code list, `format="llm"`, `table="outputs"`) | `schema.py`, `docs.llm` |
| `profile_source` | `profile.py` (no file written; `tables` filter; sample rows only on opt-in) |
| `test_mapping` | `mapping.run_mapping` into `<workdir>/sim/…`, then `validate.validate` |
| `validate_sim` | `validate.validate` |
| `reconcile` | `reconcile.py`: controls in `<mapping>/reconciliation.yaml` (a SIM query and a source query per control, keyed totals compared within a tolerance). `mappings/cppbank/reconciliation.yaml` checks loans per entity, the loss allowance against the allowance sub-ledger (finds the EUR 15,000 commitment difference of INV-RC-001) and counterparties |
| `run_scenario` | `sora run` via `SORA_ENGINE`, output in `<workdir>/runs/…`, returns totals and diagnostics |
| `explain_result` | `explain.py` (also `sora-tools explain`) |
| `diff_runs` | `diff_runs.py` (also `sora-tools diff-runs`) |

**Security design.** Reads only under configured roots (`--root` / `SORA_MCP_ROOTS`; resolved paths, symlinks
included; paths referenced by the scenario YAML and the mapping's `types_file` too). Writes only into
server-named directories of the work directory. Metadata only by default: raw values need `include_values` in the
call **and** the customer setting `--allow-values`, capped at `--max-rows`. The engine runs with an argument list,
no shell, a timeout and a binary the agent cannot choose. Every call is audited (`audit.jsonl`: time, user, tool,
arguments, input fingerprint, status). Results carry `status` `ok` / `error` / `denied` / `requires_approval`.

**Production mappings and approval.** A mapping is production when `mapping.yaml` has `status: production`
(`draft` by default) or its path matches a production pattern (`--production-pattern`, default `*/production/*`).
`test_mapping` on it writes nothing and returns `requires_approval` with a request id: a hash of the action, the
mapping release (hash of all mapping files) and the export. A person approves with
`sora-mcp approve <id> --by <name>` (`sora-mcp pending` lists requests); the record is kept in an approvals directory
outside the work directory. Any change to the mapping files is a new release and needs a new approval, so an agent
cannot edit an approved production mapping and run it.

**`explain_result`.** Per segment: starting stocks, stage shares and coverage; parameters per scenario and year with
their `source` and meaning, the calibration level of each parameter group (`calibration_levels`: segment, portfolio,
instrument, all) and the change against the starting point; the ECB benchmark rule per group (`none`, `sovereign`,
`coverage`, `no_model`, key or `unavailable`); per scenario and year the stage flows, the provision components with
their EBA box (Box 5 S1-S1, Box 4 S2-S1, Box 6 S1-S2, Box 7 S2-S2, Box 8 cumulative S1/S2-S3, Box 9 old S3, Box 3
stocks), the stock change by stage, and consistency checks (impairment = change in the stock; stage 3 stock = Boxes
8 + 9); for NFC segments with sectoral (GVA) satellites the sectors with their own path (`sector_parameters.csv`:
source per group, GVA key); off-balance items of the segment. Plus a short narrative. Without a segment: totals, top
segments and the sectoral-model shares.

**`diff_runs`.** Per file, rows matched by key (segment / scenario / year, template row keys for the CR_* files),
numbers within `max(abs_tol, rel_tol * |value|)` (parameter files: `abs_tol` at most 1e-9); rows only in one run, changed rows, per-column changes and the
largest difference; `summary.json` and diagnostics changes; impairment totals per scenario and year and the top-N
movers. Attribution: per segment the stock is `S_t = E_t * c_t`, each stock change is split symmetrically into
`dE * mean(c)` (exposure) and `dc * mean(E)` (coverage: parameters, stage mix, starting provisions), and the
impairment change `dS_t - dS_{t-1}` gets the difference, so the parts add up exactly. Drivers list the parameter
fields of the segment that changed (at t and t+1, with the `source`), moved sectoral satellite paths of the segment,
and changed starting stocks. With a PD overlay
(adverse PDs x 1.5, `python/tests/test_mcp_tools.py`) the whole change is coverage and the drivers show `+50.0%`.

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

- **Capabilities first.** `GET /v1/capabilities` at run start, retried like a batch (below). The run fails before any record is sent if the calculator does not list the calculation (`irb`, or `parameters-credit` for `--calculator-parameters`) or the parameter set (`EU_CRR3_2025-01-01`). An unreachable calculator fails after all attempts, unless the replay cache holds its capabilities (see below).
- **Batching.** Each call (one scenario point) is split into balanced batches of at most `maxRecordsPerSyncRequest` records, sent to the synchronous endpoint. `--calculator-batch <n>` sets a larger batch size (capped by `maxRecordsPerRequest`); batches above the sync limit are sent as jobs (`POST /v1/jobs`, then `GET /v1/jobs/{id}` polled with a growing interval, then `GET /v1/jobs/{id}/result`; job timeout 1 hour).
- **Parallelism.** All batches of all scenario points share one pool of `maxConcurrentRequests` worker threads (1 if not stated), each with its own connection. Results are delivered in batch order, so output does not depend on timing.
- **Streaming.** Records are generated per batch from the exposure list, encoded once and released when the response is decoded; response results are decoded while parsing (no full JSON tree). At most 2 × the worker threads batches are in memory, so memory does not grow with records × scenario points (1x reference with the stub: peak RSS about 190 MB, was 360–375 MB).
- **Retries.** 429, 502, 503, 504 and connection errors or timeouts are retried up to 8 attempts per call, with exponential backoff (0.25 s doubling, at most 30 s). A `Retry-After` header in seconds replaces the computed delay. A retry always sends the same body and the same `Idempotency-Key`. Other statuses fail the run with the problem details (RFC 9457 `title`/`detail`).
- **Idempotency keys.** `Idempotency-Key` = `context.requestId` = an RFC 9562 version-8 UUID from SHA-256 over the run id, the calculation and the canonical request body without `requestId`. The run id defaults to `sora|<scenario name>|<reference date>|<mapping release>`, so a rerun with the same inputs sends the same keys, and the calculator can return its stored responses.
- **Exact decimals.** Sora holds amounts as cents and probabilities as integers scaled by 10^9, and writes them as decimal strings: amounts with 2 decimals, probabilities with 9, maturity in years with 4, turnover in EUR million with 8 (exact, from cents). Responses are parsed from the decimal strings straight into scaled integers (half-to-even rounding beyond the target scale), never through a binary float. REA and EL are summed in cents, and `rea.csv` prints those sums.
- **Canonical JSON.** Object keys are sorted and there is no whitespace, so the same records always give the same bytes, key and fingerprint. `context.inputFingerprint` is `sha256:<hex>` of the records array. The body is written in one pass (the same bytes as nlohmann/json's `dump()`, so the keys and cache entries of earlier versions stay valid); the key is hashed from that body around the `requestId` member, which is then filled in. Strings are escaped by the shared escaper `include/sora/json_text.hpp` (also used for `summary.json` and `diagnostics.json`).
- **Response validation.** `meta.requestId` echoes the key. `meta.calculator` name and version equal the capabilities, and `meta.paramSet` equals the request's. There is exactly one result per record, with `recordId` echoed in request order. `status` is `ok` or `rejected`. For `ok`: `rea` and `expectedLoss` are present, match the Decimal pattern and are not negative; `riskWeight` is not negative; `pdApplied`/`lgdApplied` are in [0, 1]. Any violation fails the run, and nothing is cached.
- **Replay cache** (`--calculator-cache <dir>`). Validated responses are stored as `<dir>/<calculator name>_<version>/<calculation>/<sha256 of request body>.json` (`irb`, `parameters-credit`) (written to a temporary file, then renamed). Capabilities are stored under `<dir>/capabilities/`, keyed by URL. A rerun with identical inputs and the same calculator version reads every batch from the cache. If every attempt at the capabilities fails (no response or a retryable status), the run continues offline from the cached capabilities, and a batch missing from the cache fails the run. A new calculator version, or any changed input, is a cache miss. The cache is best effort: an entry that cannot be written (read-only or full disk) is counted and reported (`CALC-004`), and the run continues.
- **Security.** `https://` URLs verify the server certificate against the system store or `--calculator-ca <file>`. mTLS uses `--calculator-cert <file> --calculator-key <file>`. An OAuth2 bearer token is read from the `SORA_CALCULATOR_TOKEN` environment variable only, never from the command line. Plain `http://` is for the local stub: with a token set, the run refuses an `http://` URL unless the host is loopback (`localhost`, 127.0.0.0/8, `::1`). The `--calculator*` options are accepted by `sora run` only.
- **Calculations.** IRB and credit parameters share the client (`Client::run_stream`: batching, worker pool, bounded window, retries, jobs with `calculation` `irb` / `parameters-credit`, validation, replay cache); only the encoding and the response decoder differ. Parameter requests are canonical JSON (nlohmann `dump()`, sorted keys) with the same key and fingerprint rules.
- **Diagnostics.** `CALC-000` (info) summarises batches, HTTP requests, retries, jobs and cache hits. `CALC-001` (info): offline replay, with the last capabilities error. `CALC-004` (warning): replay cache entries not written, with the first error. `CALC-002` (warning): PiT proxy used (below). `CALC-003` (info): exposures not sent (POCI, zero EAD). `CALC-010` (warning): records rejected, with count and examples. Rejected records are left out of `rea.csv` amounts but counted in `records_rejected`, and the run continues. `CALC-011` (error): every record rejected. The run then stops without writing results.

### IRB REA projection (`sora run --calculator <url>`)

`src/rea.cpp`. It runs after the IFRS 9 projection and does not change it.

- **Records.** One per in-scope exposure in stage 1, 2 or 3, per scenario point: actual/0, baseline/1–3, adverse/1–3. POCI exposures and exposures with zero gross carrying amount are not sent. `recordId` = `exposure_id|scenario|year`. `approach` = `airb`.
- **EAD.** The exposure's gross carrying amount at the reference date, in the reporting currency (converted exactly with the SIM FX rate), for every year (static balance sheet). Off-balance amounts and CCFs are not included yet.
- **PD / LGD.** From the projection's parameter path of the exposure's segment for that scenario and year, or its own path when it has exposure-level parameters (`exposure_param_paths()`, the same function the projection uses for provisions):
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

### Credit parameters (`sora run --calculator <url> --calculator-parameters <list|all>`)

`src/credit_parameters.cpp`. The customer's parameter models as a run-time source of starting-point parameters
(`plans/09_risk_parameters.md`). It runs before the calibration and projection and feeds the customer-parameter path.

- **Options.** `--calculator-parameters` takes a comma-separated list of `pd12m_s1, pd12m_s2, tr1_2, tr2_1, tr3_1,
  tr3_2, lgd_s1, lgd_s2, lgd_s3, lrlt_s2, ccf, pd_reg, lgd_reg`, or `all` (other contract names such as
  `pd_lifetime` are not used by the engine and are refused). The IRB REA still runs with `--calculator`;
  `--calculator-rea off` skips it (a calculator that offers only `parameters-credit`). Both need `--calculator`, and
  all `--calculator*` options (TLS, mTLS, token, cache, batch size) apply to both calculations.
- **Records.** One per exposure the run projects, in exposure order: in-scope on-balance exposures with a stage (POCI
  included) and the off-balance items of the scenario's `off_balance` types. `recordId` = `exposure_id`, `level` =
  `exposure`, `segment` = the segment key (on-balance), `stage`, and `attributes` named as SIM columns:
  `country_of_risk`, `currency`, `eba_sector`, `exposure_type`, `household_purpose`, `is_cre`, `is_sme`,
  `measurement_category` (unknown values left out). One call: scenario `actual`, projection year 0, `years = [0]`, no
  macro path. A different parameter list is a different request (other keys, other cache entries).
- **Validation.** On top of the envelope checks of the IRB path: every value is for a requested parameter and year,
  at most once, a Decimal string in [0, 1] (read exactly to 9 decimals, half to even beyond), `source` is `model`,
  `benchmark` or `override` when given. `values` may leave parameters out. Any violation fails the run.
- **Precedence** (field by field, starting point): exposure row of the parameter source (`--parameters` or
  `sim_risk_parameter`) > calculator > segment rows of the source > Sora's own value. The IFRS 9 fields become
  exposure-level starting points (`ExternalParameters::fill_exposure`), projected with the segment's satellite like any
  exposure row; `ccf` feeds the off-balance CCF (before the source's segment rows and the regulatory fallback CCF);
  `pd_reg` / `lgd_reg` feed the IRB records (before the segment rows; they replace the PiT proxy, `CALC-002`).
- **Never fabricated.** A rejected record, or a requested parameter without a value, leaves the field to the next
  source. Both are counted and reported; the run stops only if every record is rejected.
- **Outputs.** `calculator_parameters.csv` (`exposure_id, status, <requested parameters>, applied`: every value
  received, and which were used), `parameters.csv` exposure rows (`level = exposure`, actual/0, the IFRS 9 fields taken
  from the calculator, the others empty, `source = calculator`), and `summary.json` `calculator_parameters`
  (calculator, version, parameter set, records ok/rejected, per parameter `received` / `applied` / `file` /
  `missing`, values by the calculator's `source`). Without `--calculator-parameters` the outputs are unchanged.
- **Offline / replay.** As for IRB: capabilities and batches from `--calculator-cache` when every capabilities attempt
  fails; a batch not in the cache fails the run. A rerun with the same inputs, list and calculator version replays
  every batch.
- **Diagnostics.** `CALC-020` (info): calculator, batches, requests, retries, jobs, cache hits, values received and
  applied. `CALC-021` (info): offline replay. `CALC-022` (warning): requested values not returned, per parameter.
  `CALC-023` (info): values not used because the source has an exposure row with the field. `CALC-024` (warning):
  replay cache entries not written. `CALC-025` (warning): records rejected, with examples. `CALC-026` (error): every
  record rejected; no results written.

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
- Engine integration tests against the stub in all three modes (`python/tests/test_calculator_engine.py` for IRB, `python/tests/test_calculator_parameters_engine.py` for credit parameters; ctest `sora_calculator`). `--reject <regex>` (or `start_background(reject=...)`) makes the stub reject records whose `recordId` matches, e.g. `7\|adverse\|3$`; `--omit lgd_s2,ccf` (`omit=`) leaves parameters out of every `/v1/parameters/credit` result. The stub's parameters are deterministic: `fixed` returns a base value per parameter, `formula` scales PDs and transition rates by the record's stage, `eba_sector` and `is_sme`.

## 3. Phasing

| Phase | Integration |
|---|---|
| 0–1 | OpenAPI contract v0.1, stub server (`fixed`, `faults`), contract tests |
| 1–3 | SIM schema v1, export spec, reference mapping, `sora-tools map` / `sora-tools validate`; `Impairment` internal |
| 4–5 | REST client (batching, retries, replay cache) and IRB REA projection (done); `/v1/parameters/credit` integration (done: `--calculator-parameters`); stub `formula` mode (done) |
| 5b | `sora-mcp` (describe, profile_source, test_mapping, validate_sim, reconcile, run_scenario, explain_result, diff_runs): done |
| 6–7 | IRB, SA and output floor through the calculator; Parquet bodies |
