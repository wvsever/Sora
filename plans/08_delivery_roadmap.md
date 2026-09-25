# Delivery Roadmap

## Phase 1 - Core skeleton

Deliver:

- CMake project
- Core data structures
- Scenario schema
- CSV input
- CSV output
- Single-threaded pipeline
- Basic PD/LGD/collateral shocks
- Unit tests

## Phase 2 - Performance baseline

Deliver:

- Chunk processing
- String dictionaries
- Binary internal IDs
- Benchmark harness
- Allocation measurements
- Peak-memory measurements

## Phase 3 - Credit stress

Deliver:

- Rating transitions
- Default generation
- Stage migration
- Collateral haircuts
- Loss aggregation
- Deterministic pseudo-random events

## Phase 4 - Parallel execution

Deliver:

- Partitioned workers
- Thread-local aggregators
- Deterministic merge
- Scaling benchmarks

## Phase 5 - Time evolution

Deliver:

- Multi-period scenarios
- Scenario curves
- Migration over time
- Cumulative events

## Phase 6 - Funding and rates

Deliver:

- Deposit outflows
- Funding spread shocks
- Repricing effects
- Simplified NII transformation

## Phase 7 - Production hardening

Deliver:

- Binary high-throughput input/output
- Checkpointing
- Crash-safe output handling
- Structured diagnostics
- Scenario fingerprints
- Input fingerprints
- Performance regression CI

## Phase 8 - Advanced optimization

Only after profiling:

- Explicit SIMD
- Alternative allocators
- NUMA-aware execution
- Memory-mapped columnar input
- Custom compact binary format

## Release philosophy

Keep the core engine small. Add features only when they preserve deterministic behavior, bounded memory use, and measurable throughput.
