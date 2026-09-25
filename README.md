# Sora Stress & Scenario Engine

Sora is a high-performance C++ engine for applying deterministic stress scenarios to large banking and financial datasets.

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

## Example scenario

```yaml
name: severe_recession
scenario_id: severe_recession_001
horizon_months: 36

macro:
  gdp_change_pct: -3.2
  unemployment_change_pct: 2.1
  residential_property_change_pct: -18.0
  commercial_property_change_pct: -25.0

rates:
  eur_parallel_shift_bps: 150

credit:
  default_probability_multiplier: 1.70
  downgrade_bias: 0.25

fx:
  EURUSD_change_pct: -12.0
```

## Processing model

The engine should be implemented as a staged pipeline:

```text
Input Reader
    -> Scenario Loader
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
├── tests/
├── benchmarks/
├── examples/
├── schemas/
└── plans/
    ├── 01_architecture.md
    ├── 02_data_model.md
    ├── 03_scenario_engine.md
    ├── 04_performance_memory.md
    ├── 05_execution_pipeline.md
    ├── 06_validation.md
    ├── 07_benchmarking.md
    └── 08_delivery_roadmap.md
```

## Non-goals

The first version should not attempt to become a general-purpose risk platform.

Avoid initially:

- GUI development
- Large dependency stacks
- Embedded scripting languages
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
