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

- **Mapping with SQL:** customers map their source data to SIM with SQL views, written by hand or with an AI agent.
- **MCP server (`sora-mcp`):** lets an agent read the model description, profile sources, test mappings, validate, run and explain results. It runs locally, returns metadata only by default, and requires human approval for production mappings.
- **Calculator ports:** PD/LGD models and regulatory calculators (SA, IRB, output floor) connect through ports, each with a built-in reference implementation and batch-file or service adapters. See `plans/11_integrations.md`.

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

## C++ baseline

Recommended baseline:

- C++20 or newer
- CMake
- Standard library first
- Avoid heavy frameworks in the core engine
- Optional dependencies only where justified by measurable performance or implementation simplicity

Potential libraries:

- `simdjson` for high-performance JSON input
- `yaml-cpp` only for human-authored scenario files if YAML is required
- `fmt` for formatting
- `spdlog` for logging, preferably asynchronous or compile-time removable in hot paths
- `mimalloc` or `jemalloc` only after profiling demonstrates allocator pressure
- `xxHash` for fast fingerprints/checksums
- Apache Arrow only if interoperability benefits outweigh its memory footprint

## Repository structure

```text
sora/
├── README.md
├── CMakeLists.txt
├── include/
│   └── sora/
├── src/
├── mappings/
│   └── cppbank/              # reference mapping SQL: test dataset -> SIM
├── tools/
│   ├── mcp/                  # sora-mcp server
│   ├── scenario_import/      # xlsx scenarios -> normalised CSV
│   └── reference/            # independent reference implementation (golden results)
├── tests/
│   ├── data/                 # reference dataset (20260630.7z) + README
│   ├── golden/               # expected results for the reference dataset
│   └── scenarios/
├── benchmarks/
├── examples/
├── schemas/
│   └── sim/                  # Sora Input Model: the single source of truth
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
    └── 11_integrations.md
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
