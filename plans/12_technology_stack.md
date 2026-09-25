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
| `tests/stubs/calculator/` | Stub REST calculator (FastAPI) |
| `tests/contract/` | Calculator contract tests |

The engine re-checks its own inputs on load (fast validation). It never depends on `sora-tools validate` having run, and never trusts inputs blindly.

## C++ engine dependencies

Standard library first. Each dependency must earn its place by measurement.

| Need | Choice | Notes |
|---|---|---|
| Parquet read/write | **Apache Arrow C++ / `parquet`** (Parquet component only, no compute/dataset modules) | Read per row group with column projection, so memory is bounded by row-group size × projected columns. Decimals are read as `DECIMAL(18,2)` → `int64` cents directly. Benchmark against the DuckDB C API in phase 1 before locking in. |
| CSV | Own parser | Exact decimal → scaled integer, as described in `02_data_model.md` |
| HTTP client (REST) | `libcurl` (multi interface) or `cpp-httplib` | TLS and mTLS required. Tested against the stub. |
| JSON | `simdjson` (parse), own writer or `fmt` | Decimals as strings |
| YAML (scenario) | `yaml-cpp` or `rapidyaml` | Load time only |
| Hashing | `xxHash` | Fingerprints, replay cache keys |
| Formatting / logging | `fmt`, `spdlog` (compile-time level) | Not in hot loops |
| Tests / benchmarks | `doctest` or `Catch2`, Google Benchmark | |
| Allocator | Default. `mimalloc` only if profiling shows allocator pressure. | |

Dependencies come through vcpkg (manifest mode) or CMake `FetchContent`, with pinned versions.

## Memory and performance rules (engine)

- Parquet is read per row group and per projected column. It is never loaded as a full table.
- Arrow arrays are converted immediately into Sora's compact records (`02_data_model.md`), and the Arrow buffers are released. Arrow types do not leak past the reader.
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
