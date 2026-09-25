# Benchmarking Plan

## Objective

Prevent performance regressions and quantify design choices.

## Benchmark datasets

Create synthetic datasets at:

- 100k records
- 1M records
- 10M records
- 50M records

## Metrics

Capture:

- Records/second
- MB/second input
- MB/second output
- Peak resident memory
- Allocations per million records
- CPU utilization
- Scaling from 1 to N threads
- Validation overhead
- Scenario complexity overhead

## Benchmark cases

1. Parse only
2. Single PD multiplier
3. PD + LGD + collateral
4. Rating transition
5. Full credit scenario
6. Full scenario with aggregation
7. Full scenario with output
8. Full validation enabled

## Regression gates

Suggested initial gates:

- >10% throughput regression requires review
- >10% memory regression requires review
- Any nondeterministic result is a release blocker

## Build profiles

Maintain:

- Debug
- Release
- Release with symbols
- Sanitizer

Use sanitizers in CI but never compare sanitizer benchmarks with release performance.
