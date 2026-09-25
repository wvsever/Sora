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

The scaled datasets are generated, not committed. If the generator is not available, a Sora tool replicates `ref-1x` N times with rewritten business keys. The tool must keep referential integrity (contract ↔ counterparty ↔ collateral).

Also report partition statistics (files, bytes, rows per file). File count, not only row count, drives cost at small scale.

### Implemented: `tools/scale_sim.py` (SIM level)

The generator is not available, so the scaled sets are made from the **mapped SIM** (not the export) by `tools/scale_sim.py`, in DuckDB SQL:

```sh
python tools/scale_sim.py build/sim/20260630 build/sim/20260630-x10  --factor 10    # 0.6M exposures, 4M history rows, 3 s
python tools/scale_sim.py build/sim/20260630 build/sim/20260630-x100 --factor 100   # 6.2M exposures, 40M history rows, 16 s, 0.3 GB
```

- Every column of SIM type `key` except entity ids (exposure, counterparty, collateral, guarantee and group ids, and all foreign keys to them) gets the suffix `~<replica>`. Replica 0 keeps the original keys, so a 1x copy equals the source. `sim_risk_parameter` exposure-level keys are suffixed; segment-level rows are kept once.
- `sim_entity` and `sim_fx_rate` are unchanged: the replicas are more business in the same entities. LEIs are kept for replica 0 only.
- Output is Parquet, partitioned like the source (`partition_by` of the schema). So file count stays the same (218) while rows grow: these sets measure row volume, not small-file overhead. `sim_manifest.json` gets a `scale` field.
- No perturbations: every replica has the same amounts and stage paths. Segment totals scale exactly by N; parameters change only where more observations lift a segment above `min_observations` (so they reach their own hierarchy level). `sora-tools validate` passes on the scaled sets; `python/tests/test_scale_sim.py` checks keys, integrity and the engine on a 3x copy.
- Replica keys sort next to the original (`CL-000001`, `CL-000001~1`, …). Sort-based code paths therefore see replicas adjacent; this favours nothing in the engine (all lookups are by hash or by DuckDB-computed index).

## Harness: `benchmarks/run_benchmarks.py`

```sh
python benchmarks/run_benchmarks.py --memory-limits 256MB,512MB,1GB,2GB    # 1x/10x/100x, workers 1,2,4,8
python benchmarks/run_benchmarks.py --render benchmarks/results/<file>.json --baseline <older>.json
```

It creates missing scaled sets, then runs `sora inspect` and `sora run` per scale (fastest of `--repeat`, default 3), the engine-thread scaling at the largest scale (`--workers 1,2,4,8`, checking that all outputs are byte-identical), optional DuckDB `memory_limit` variants, and one `SORA_TRACE=1` run. Per run it records the engine's stage timings (stderr), wall time, peak RSS (the engine's `getrusage` figure and the kernel's `wait4` figure for the child; `/usr/bin/time -v` when installed), CPU time and utilisation, and exposures per second (all exposures in `sim_exposure` / wall time). Raw results go to `benchmarks/results/<date>-<host>.json`, the summary to `benchmarks/RESULTS.md`.

`SORA_TRACE=1` makes the engine print, per DuckDB query, the wall time and the part spent in the engine's own chunk callbacks. It separates DuckDB time (scan, sort, join) from engine time.

Results of 2026-09-25 are in `benchmarks/RESULTS.md`; the decisions they led to are in `04_performance_memory.md`.

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
- Any nondeterministic result is a release blocker. Tested: `--workers 1/2/3/8` give byte-identical outputs (`test_engine.py::test_results_do_not_depend_on_workers`, the harness), and `project()` gives bit-identical doubles (`memcmp`) for 1..8 workers (doctest)
- Any change in the `ref-1x` golden results (see `06_validation.md`) is a release blocker unless it is approved and the golden files are updated in the same change

## Build profiles

Maintain:

- Debug
- Release
- Release with symbols
- Sanitizer

Use sanitizers in CI, but never compare sanitizer benchmarks with release performance.
