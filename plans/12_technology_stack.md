# Technology Stack Plan

## Decision

| Layer | Technology | Why |
|---|---|---|
| Stress engine | **C++20** | Low memory, high throughput, deterministic numerics. All per-record work happens here. |
| Data interchange | **Parquet** (primary), CSV (supported) | Typed, columnar, compressed, exact decimals |
| Mapping and data tooling | **Python + DuckDB** | SQL on exported files, fast to build, customer-friendly |
| Calculator interface | **REST** (OpenAPI 3.1) | `schemas/calculator/openapi.yaml` |
| Agent interface | **MCP**, in Python | `sora-mcp` |

The rule is simple: **anything that touches every record at stress time is C++.** Python is used only for setup, mapping, validation tooling, tests, and reference implementations. Python never sits on the hot path of `sora run`.

## Component split

### C++: `sora` (engine binary and library)

| Command / part | Notes |
|---|---|
| `sora run` | Load SIM, project, aggregate, write results |
| `sora calibrate` | Starting-point parameters from history (production numerics = engine numerics) |
| `sora inspect` | SIM dataset summary |
| SIM reader | Parquet and CSV, streaming, column projection |
| REST calculator client | Batching, retries, idempotency, replay cache |
| Result writer | Parquet and CSV (CR_SCEN-layout long tables), NDJSON diagnostics |
| `libsora` | The same engine as a library, with a C API for embedding (e.g. Python bindings for notebooks later) |

### Python: `sora-tools` (package)

| Command / part | Notes |
|---|---|
| `sora-tools map` | Run mapping SQL on exported files with DuckDB → SIM Parquet |
| `sora-tools validate` | Schema, key, constraint and reconciliation checks on SIM (DuckDB) |
| `sora-tools profile` | Local profiling of export files (statistics, no data leaves) |
| `sora-tools scenario-import` | EBA/ESRB/ECB xlsx → normalised macro CSV/Parquet |
| `sora-tools schema` | Generate docs, DDL, LLM description and C++ bindings from `schemas/sim/` |
| `sora-mcp` | MCP server wrapping the above plus `sora` runs |
| `tools/reference/` | Independent reference implementation for golden results (not shipped) |
| `sora_tools/calculator_stub.py` | Stub REST calculator (standard library, no dependencies) |
| `python/tests/contract/` | Calculator contract tests (stub or real calculator) |

The engine re-checks its own inputs on load (fast validation). It never depends on `sora-tools validate` having run, and never trusts inputs blindly.

## C++ engine dependencies

Standard library first. Each dependency must earn its place by measurement.

| Need | Choice | Notes |
|---|---|---|
| Parquet/CSV reading | **DuckDB C API** (prebuilt `libduckdb`, pinned version and SHA-256) | **Decided.** Streaming results (≤ 2048-row chunks) with column projection. `DECIMAL(18,2)` arrives as `int64` cents and `DECIMAL(18,9)` as `int64` nano-units, with no float parsing. Memory is capped with `memory_limit`. The same engine is used by `sora-tools`, so SQL semantics match. Apache Arrow was rejected: no prebuilt packages, heavy source build. |
| CSV | Via DuckDB (`read_csv`, all VARCHAR, cast in SQL) | No own parser needed |
| HTTP client (REST) | `libcurl` (multi interface) or `cpp-httplib` | TLS and mTLS required. Tested against the stub. |
| JSON | `simdjson` (parse), own writer or `fmt` | Decimals as strings |
| YAML (scenario) | `rapidyaml` 0.9.0 single header (pinned download) | Load time only |
| Hashing | `xxHash` | Fingerprints, replay cache keys |
| Formatting / logging | `fmt`, `spdlog` (compile-time level) | Not in hot loops |
| Tests / benchmarks | `doctest` 2.4.11 (vendored in `third_party/`), Google Benchmark later | |
| Allocator | Default. `mimalloc` only if profiling shows allocator pressure. | |

Dependencies come through CMake `FetchContent` with pinned URLs and SHA-256 hashes (`cmake/Dependencies.cmake`). `SORA_DUCKDB_ROOT` and `SORA_RYML_HEADER` point to local copies for offline builds. The CA bundle from `SSL_CERT_FILE` is used behind TLS-intercepting proxies.

## Memory and performance rules (engine)

- SIM tables are read as streamed DuckDB results with only the needed columns. They are never loaded as full tables.
- DuckDB chunks are converted immediately into Sora's compact records (`02_data_model.md`) and released. DuckDB types do not leak past `sora/duck.hpp`.
- Large SIM tables (`sim_cashflow`, history tables) are streamed. Party and collateral data are held in packed arrays.
- The mapping step (DuckDB) runs as a separate process before `sora run`. Its memory is never added to the engine's. DuckDB's `memory_limit` is set by `sora-tools`.
- The Python and C++ implementations of the same calculation (reference vs engine) must agree to the cent. This is enforced by the golden tests.

## Build and packaging

- CMake presets: `debug`, `release`, `relwithdebinfo`, `asan-ubsan`.
- Targets: Linux x86-64 (primary), Windows x64 (customer requirement likely), macOS arm64 (development).
- `sora-tools` ships as a Python wheel (`pip install sora-tools`). Python ≥ 3.11, with pinned `duckdb` and `pyarrow`.
- A single release bundle contains the `sora` binary, the `sora-tools` wheel, `schemas/`, docs and the reference mapping.
- CI runs: C++ unit tests, Python tests, contract tests against the stub, golden tests on `ref-1x`, and a benchmark smoke test.

## Repository layout (additions)

```text
src/            C++ engine
include/sora/   public C++ headers / C API
python/sora_tools/   Python package (map, validate, profile, scenario-import, schema, mcp)
schemas/sim/         SIM schema (source of truth)
schemas/calculator/  OpenAPI contract
```
