# Benchmarking Plan

## Objective

Prevent performance regressions and quantify design choices.

## Benchmark datasets

Benchmarks use the same format as the reference dataset (`tests/data/20260630.7z`), so the benchmarked code path is the production path.

| Name | Source | Contracts (approx.) | Cash flows (approx.) |
|---|---|---:|---:|
| `ref-1x` | `tests/data/20260630.7z` (generator `scale = 1.0`, `seed = 27`) | 62k credit + 80k deposits | 1.1M |
| `ref-10x` | same generator, `scale = 10` | 620k | 11M |
| `ref-100x` | same generator, `scale = 100` | 6M | 110M |
| `ref-500x` | same generator, `scale = 500` | 30M | 550M |

The scaled datasets are generated, not committed. If the generator is not available, a Sora tool (`sora-replicate`) replicates `ref-1x` N times with rewritten business keys and deterministic perturbations. The tool must keep referential integrity (contract ↔ counterparty ↔ collateral).

Also report partition statistics (files, bytes, rows per file). File count, not only row count, drives cost at small scale.

## Metrics

Capture:

- Records/second per stage (parse, join, stress, aggregate, write)
- MB/second input
- MB/second output
- Files/second (small-file overhead)
- Peak resident memory
- Allocations per million records
- CPU utilisation
- Scaling from 1 to N threads
- Validation overhead
- Scenario complexity overhead (segments × years × rule count)

## Benchmark cases

1. Parse only (all tables; credit tables only; skip GL)
2. Load and join (exposure assembly, see `02_data_model.md`)
3. Starting-point derivation (`09_risk_parameters.md`)
4. Single PD shift
5. PD + LGD + collateral repricing
6. Stage transition projection, 3 years
7. Full credit scenario (baseline + adverse)
8. Full scenario with aggregation
9. Full scenario with output
10. Full validation enabled, including GL reconciliation

## Regression gates

Suggested initial gates:

- > 10% throughput regression requires review
- > 10% memory regression requires review
- Any nondeterministic result is a release blocker
- Any change in the `ref-1x` golden results (see `06_validation.md`) is a release blocker unless it is approved and the golden files are updated in the same change

## Build profiles

Maintain:

- Debug
- Release
- Release with symbols
- Sanitizer

Use sanitizers in CI, but never compare sanitizer benchmarks with release performance.
