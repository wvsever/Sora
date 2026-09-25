# Sora Stress & Scenario Engine

Sora is a high-performance C++ engine for applying deterministic stress scenarios to large banking and financial datasets.

Sora is a product for credit institutions (our customers). Customers run it on their own data, with their own risk models and supervisory inputs. The repository contains only the engine, formats, tools and synthetic test data.

Its purpose is to transform a coherent baseline dataset into one or more stressed states while preserving accounting, contractual, and portfolio-level consistency as far as the configured scenario model allows.

The design priorities are:

- Very low memory overhead
- High throughput on large datasets
- Deterministic and reproducible execution
- Minimal allocations in hot paths
- Streaming and chunk-based processing
- Parallel execution where it provides measurable benefit
- Clear separation between scenario definition, stress rules, transformation, validation, and output
- Easy embedding in batch pipelines and server-side services

## Quick start

```sh
# Python tooling
pip install -e "python[test,scenario]"
python tools/extract_testdata.py
sora-tools map mappings/cppbank --export build/testdata/20260630 -o build/sim/20260630 --validate

# C++ engine (downloads pinned DuckDB and rapidyaml on first configure)
cmake -S . -B build/release -DCMAKE_BUILD_TYPE=Release
cmake --build build/release -j
ctest --test-dir build/release --output-on-failure          # unit tests + golden comparison

build/release/sora inspect build/sim/20260630
build/release/sora run build/sim/20260630 --scenario tests/scenarios/test_eba2025.yaml -o build/out
#   -> segments.csv, parameters.csv, projection.csv, collateral.csv (LTV), cr_scen.csv (EBA CSV_CR_SCEN layout),
#      summary.json, diagnostics.json
#   add --parameters <file> to use customer risk parameters (plans/09_risk_parameters.md)

# IRB REA through a regulatory calculator (here the stub): adds rea.csv and summary.json "rea"
sora-tools calculator-stub --mode formula --port 8080 &
build/release/sora run build/sim/20260630 --scenario tests/scenarios/test_eba2025.yaml -o build/out \
    --calculator http://127.0.0.1:8080 --calculator-cache build/calculator-cache
#   TLS: --calculator-ca <file>, mTLS: --calculator-cert <file> --calculator-key <file>,
#   bearer token from $SORA_CALCULATOR_TOKEN (never on the command line)
```

## Core use cases

Sora can model stresses such as:

- GDP shocks
- Unemployment shocks
- Interest-rate shocks
- FX shocks
- Residential and commercial property price shocks
- Sector-specific deterioration
- Country-risk shocks
- Probability-of-default multipliers
- Rating migration
- Arrears progression
- Default events
- Collateral repricing
- IFRS 9 stage migration
- Expected credit loss impacts
- Deposit outflows
- Wholesale funding deterioration
- Net-interest-income changes
- Liquidity deterioration
- Market-value shocks

A scenario may combine multiple shocks and apply them by country, sector, portfolio, product, rating bucket, counterparty class, or other segmentation dimensions.

## Reference methodology

The primary target is the EBA EU-wide stress test credit-risk methodology: the 2025 final and 2027 draft methodological notes in `docs/`. The engine projects IFRS 9 stage flows and provisions over a 3-year horizon, under baseline and adverse macro scenarios, from 12-month point-in-time starting-point parameters per portfolio segment. See `plans/03_scenario_engine.md` and `plans/09_risk_parameters.md`.

## Inputs

| Input | Location | Notes |
|---|---|---|
| Bank data in the Sora Input Model (SIM) | Customer, mapped with SQL | Documented schema in `schemas/sim/`. The reference source is `tests/data/20260630.7z`, mapped by `mappings/cppbank/`. See `plans/10_input_model_and_mapping.md`. |
| Macro scenario | `docs/` (ESRB/ECB xlsx) | Converted to a normalised CSV by `tools/scenario_import` |
| Starting-point PD / TR / LGD / LR | Customer model output, or `sora calibrate` | Not present in the dataset. See `plans/09_risk_parameters.md`. |
| Satellite models | Customer | Macro → parameter sensitivities per segment (synthetic in tests) |
| ECB benchmark parameters | Customer (received from ECB, confidential) | Loaded in Sora's benchmark format (synthetic in tests) |

## Integration

- **Mapping with SQL on exports:** customers export source tables to files. Mapping SQL, written by hand or with an AI agent, runs on those files with embedded DuckDB. There is no database access.
- **MCP server (`sora-mcp`):** lets an agent read the model description, profile sources, test mappings, validate, run and explain results. It runs locally, returns metadata only by default, and requires human approval for production mappings.
- **Regulatory calculator over REST:** PD/LGD models, IRB, SA and the output floor are computed by an external calculator implementing `schemas/calculator/openapi.yaml`. A stub server (`sora-tools calculator-stub`) and contract tests are used for testing. `sora run --calculator <url>` projects IRB REA and expected loss per segment, scenario and year (`rea.csv`), with batching, retries, idempotency keys and an on-disk replay cache. See `plans/11_integrations.md`.

## Example scenario

```yaml
name: eba_2027_adverse
scenario_id: eba2027_adv_v1
reference_date: 2026-06-30
steps: 3
macro_path: scenarios/eba2027_macro.csv
scenario: adverse
starting_parameters: params/risk_parameters_20260630.csv
satellite_models: models/satellites.csv
constraints:
  no_cure_from_s3: true
  no_s3_provision_release: true
  static_balance_sheet: true

overlays:                      # optional sensitivity shocks
  - rule: pd_multiplier
    where: { sector: NFC, country: DE }
    value: 1.7
```

## Processing model

The engine should be implemented as a staged pipeline:

```text
Dataset Discovery (partitioned CSV)
    -> Reference & Party Store
    -> Exposure Assembly (contract + counterparty + collateral + allowance)
    -> Risk Parameters (external | calibrated | benchmark)
    -> Scenario Loader & Compiler
    -> Segmentation
    -> Stress Rule Evaluation
    -> Event Generation
    -> State Transformation
    -> Consistency Checks
    -> Aggregation
    -> Output Writer
```

The preferred execution model is streaming and partitioned. The engine should avoid loading the full dataset into memory unless the input is small enough that doing so is clearly faster.

## Performance goals

Initial engineering targets:

- Process tens of millions of records without excessive RAM growth
- Keep hot-path allocations close to zero
- Prefer contiguous memory layouts
- Use compact numeric representations
- Support multithreaded execution by independent partitions
- Preserve deterministic results regardless of thread count
- Allow zero-copy parsing where practical
- Use memory mapping for large immutable input files where beneficial
- Provide binary output options for high-volume workflows
- Handle many small partition files efficiently (the reference dataset has 5,651)

## Technology

- **C++20 engine** (`sora`): everything that touches records at stress time. Low memory, high throughput, deterministic.
- **Python + DuckDB tooling** (`sora-tools`, `sora-mcp`): mapping SQL on exported files, validation, profiling, scenario import, schema generation, MCP server, test stubs and the reference implementation.
- **Parquet** is the primary interchange format. CSV is supported.

C++ engine baseline:

- C++20, CMake
- Standard library first. Avoid heavy frameworks in the core engine.
- Optional dependencies only where justified by measurable performance or implementation simplicity:
  - Apache Arrow C++ (Parquet component only): row-group streaming with column projection
  - `cpp-httplib` + `nlohmann/json` (vendored single headers) and OpenSSL for the REST calculator client
  - `simdjson`, `yaml-cpp`/`rapidyaml`, `fmt`, `spdlog`, `xxHash`
  - `mimalloc` only after profiling shows allocator pressure

See `plans/12_technology_stack.md`.

## Repository structure

```text
sora/
├── README.md
├── CMakeLists.txt
├── include/
│   └── sora/
├── src/                      # C++ engine
├── python/                   # sora-tools: schema, map, validate, calculator stub (+ tests, contract tests)
├── mappings/
│   └── cppbank/              # reference mapping SQL + source data dictionary: test dataset -> SIM
├── scenarios/                # normalised scenario data (sora-tools scenario-import)
├── tools/
│   ├── extract_testdata.py   # extract tests/data/*.7z into build/
│   └── reference/            # independent reference implementation (golden results)
├── tests/
│   ├── data/                 # reference dataset (20260630.7z) + README
│   ├── golden/               # expected results for the reference dataset
│   ├── params/               # synthetic stand-ins for customer inputs (satellites, ...)
│   └── scenarios/
├── benchmarks/
├── examples/
├── schemas/
│   ├── export/               # source export specification (formats, manifest, data dictionary)
│   ├── sim/                  # Sora Input Model: the single source of truth
│   └── calculator/           # REST contract for the regulatory calculator (OpenAPI)
├── docs/                     # EBA guidelines and EU-wide stress test material (2025, 2027 draft)
└── plans/
    ├── 01_architecture.md
    ├── 02_data_model.md
    ├── 03_scenario_engine.md
    ├── 04_performance_memory.md
    ├── 05_execution_pipeline.md
    ├── 06_validation.md
    ├── 07_benchmarking.md
    ├── 08_delivery_roadmap.md
    ├── 09_risk_parameters.md
    ├── 10_input_model_and_mapping.md
    ├── 11_integrations.md
    └── 12_technology_stack.md
```

## Non-goals

The first version should not attempt to become a general-purpose risk platform.

Avoid initially:

- GUI development
- Large dependency stacks
- Embedded scripting languages (SQL mapping runs in tooling, not in the core engine)
- Generic workflow engines
- Distributed execution frameworks
- Complex database persistence layers
- Premature GPU acceleration

The first objective is a small, deterministic, extremely fast native engine.

## Guiding principle

Every feature should be judged against two questions:

1. Does it materially improve scenario realism or analytical value?
2. Can it be implemented without compromising predictable memory use and throughput?

If the answer to both is not clear, keep it out of the core engine.
