# Performance and Memory Plan

## Objective

Make predictable memory use and throughput primary architectural concerns.

## Workload profile (reference dataset)

The reference dataset (`tests/data/20260630.7z`, scale 1.0) is small in record count but wide and fragmented:

| Class | Tables | Rows | Access pattern |
|---|---|---|---|
| Dimensions and reference | entity, portfolio, gl_account, curves, fx_rate | < 250k | Load fully, freeze |
| Party data | counterparty, rating, link, collateral, allocation, guarantee | < 100k each | Load fully, index by dense ID |
| Exposures | contract_loan, commitment, lease, security | about 62k | Stream per entity, or hold fully (small) |
| Histories | impairment_allowance, arrears_history | 0.4M / 0.3M | Calibration only, streamed |
| Large streams | contract_cashflow, journal_line, journal_entry | 1.1M / about 3M / about 1.3M | Streamed per partition, never held |

Observations that drive the design:

- There are 5,651 small files. Per-file overhead (open, header parse, dictionary lookups) dominates at scale 1.0. File handling must be cheap and parallel across partitions.
- About 70% of bytes are in the GL tables, which the stress engine needs only for reconciliation. They must be skippable.
- The generator supports a `scale` factor. Production-like volumes (tens of millions of contracts and cash flows) are reached by scaling, not by changing the format. See `07_benchmarking.md`.

## Memory strategy

### Avoid

- Per-record heap allocation
- `std::string` in hot-path records
- `std::map` for large runtime lookups
- Nested pointer-heavy object graphs
- Large duplicated baseline/stressed datasets where transformation can be streamed
- Materialising `contract_cashflow` or `journal_line` in full

### Prefer

- `std::vector`
- Flat hash structures for business-key lookup, built once and then frozen
- Integer dictionaries
- Reusable buffers
- Arenas for temporary batch data
- Chunk-based processing
- Memory-mapped immutable files where useful

## Chunk processing

Recommended baseline:

```text
read chunk
-> transform chunk
-> aggregate chunk
-> write chunk
-> reuse buffers
```

A chunk is at most one partition file, or a fixed number of records from it, whichever is smaller. Chunk size should be benchmark-configurable.

Candidates:

- 16k records
- 64k records
- 256k records

## Concurrency

Partition by entity (`entity_id=` directory). Split large entities into record ranges.

Each worker owns:

- Input slice
- Scratch buffer
- Local aggregation state
- Local diagnostics

Avoid shared mutation during the main loop. Shared read-only state (dimensions, counterparties, collateral, compiled scenario) is built before workers start.

Perform a deterministic final reduction after workers finish, ordered by `(entity_id, file, row_seq)` and never by completion order.

## False sharing

Align frequently updated thread-local counters to cache-line boundaries.

Use padding only after measurement confirms contention or cache-line interference.

## SIMD

Potential SIMD candidates:

- PD / transition-rate multiplication per segment
- LGD shocks
- Collateral repricing
- Interest-rate transformations
- Threshold comparisons (stage triggers)

Do not introduce explicit intrinsics until compiler auto-vectorisation has been measured.

## Memory budget targets

Example engineering targets:

- Core engine overhead: < 100 MB excluding dataset buffers
- Scale 1.0 full credit run: < 256 MB peak RSS
- Chunk mode: bounded memory independent of cash-flow and journal volume
- In-memory party and exposure data: at most 1.5x its packed binary size
- No unbounded caches
- No duplicated dimension strings

## Measured (2026-09-25, `benchmarks/RESULTS.md`)

4-core Xeon VM shared with other jobs (treat ±15% as noise), `sora run` with the test scenario, fastest of 3 runs. Datasets: the reference SIM replicated by `tools/scale_sim.py` (see `07_benchmarking.md`).

| Scale | Exposures | Stage history | Wall before → after | Peak RSS before → after | Exposures/s after |
|---|---:|---:|---:|---:|---:|
| 1x | 62k | 0.4M | 0.40 → 0.34 s | 133 → 129 MB | 180k |
| 10x | 0.62M | 4.0M | 2.92 → 1.50 s | 569 → 369 MB | 413k |
| 100x | 6.2M | 40M | 45.5 → 17.7 s | 2,799 → 2,189 MB | 351k |

Targets: **scale 1.0 < 256 MB: met (129 MB).** At 100x, peak RSS is about 0.8 GB of engine data plus DuckDB's `memory_limit` (default 1 GB) and a few hundred MB DuckDB holds outside it. It is bounded by configuration, not by row count of the streamed tables, but it is not "chunk mode" yet: exposures and counterparties are held in memory (about 130 bytes per exposure including its share of counterparties, dictionaries, segment and FX arrays).

`--memory-limit` at 100x: 512 MB → 21.0 s / 1.28 GB, 1 GB → 18.1 s / 2.18 GB, 2 GB → 14.2 s / 2.73 GB. 256 MB fails (DuckDB out of memory in the exposure sort); 512 MB is the practical minimum at 100x. The default stays 1 GB.

### What was changed, and why (profiled with gdb stack sampling, callgrind and `SORA_TRACE=1`)

| Bottleneck at 100x (before) | Change | Effect |
|---|---|---|
| Stage-history `ORDER BY exposure_id, period_end` on 40M rows with a VARCHAR key (13 s in DuckDB) and two `exposure_ids.find` per row in the loop | DuckDB joins each history row to the exposure's index (`row_number()` over `exposure_id`, which is the load order) and sorts on integers; stage mapped to an integer in SQL; month-end arithmetic cached | calibrate 19.7 → 7.3 s; loop 0.7 s of engine time for 40M rows |
| `Dictionary` = `std::deque<std::string>` + `std::unordered_map<string_view>`: ~100 B and 2 allocations per key, cache-missing lookups (6.2M exposure + 2.4M counterparty keys) | One string arena, `uint32` offsets, open-addressing table of 8-byte slots (32-bit hash tag + id), load ≤ 0.8; sized from a `count(*)`/`sum(length)` query | 30–40 B per key; no per-key allocation; exposure vector and dictionaries reserved once (no 2x regrowth peak) |
| Segmentation built one heap `std::string` key per exposure and a `std::map<string, vector>` | Integer cell (instrument, portfolio code, country bucket) per exposure; key strings only per distinct cell | segment 2.9 s → 0.16 s |
| `Chunk` accessors out of line (type check + C API call per value) | Inline accessors, validity bit test inline | type check and validity test compile into the callback loops |
| Projection single-threaded (0.8 s) | `--workers N`, by segment (below) | 0.85 s → 0.27 s with 4 workers |

Outputs are byte-identical to the previous engine at 1x, 10x and 100x (same summation order everywhere), and the golden tests pass unchanged.

### Engine threads: decisions

- **Projection** runs on `--workers N` threads (`std::thread`, default hardware concurrency). The unit of work is a whole **segment**: one worker processes all its exposures in exposure order into local accumulators and writes them to the segment's own result slot. No floating-point sum ever combines partial results from different workers, so results are bit-identical for any N and any scheduling (tested with `memcmp` for 1..8 workers, and byte-identical CSVs in `test_engine.py`). Segments are handed out largest first through an atomic counter; the assignment affects timing only. Exposure-level parameter errors are collected per segment and merged in exposure order. Speed-up at 100x: 2.0x with 2 workers, 3.1x with 4 (on 4 cores; the largest segment is 9% of exposures, so the ceiling is about 11x).
- This replaces "partition by entity" above for the projection: results are per segment, so segment partitioning needs no reduction at all.
- **Calibration** is not parallelised in the engine. Its cost is DuckDB's sort of the stage history (which DuckDB already runs on all cores, `--threads`); the engine loop is 0.7 s for 40M rows. Its cells are per hierarchy level (`ALL|ALL|ALL` collects every segment), so a bit-identical parallel version would need exact (integer) accumulation or a fixed merge order. Not worth it at this cost.
- Loading is a single streamed DuckDB result (one consumer thread); the parallelism is inside DuckDB.
- ThreadSanitizer (unit test) and ASan/UBSan (unit, golden and a 10x run with 8 workers) are clean.

### Next bottlenecks (100x, from `SORA_TRACE`)

1. `sim_exposure ORDER BY exposure_id` in DuckDB streaming mode: about 4 s of DuckDB time; the final merge runs on the consuming thread. Options: load unsorted and sort a permutation in C++ (parallel), or have the SIM writer guarantee order.
2. Engine time in the exposure load callbacks: about 4 s for 6.2M rows, mostly cache misses in the key dictionaries (random `counterparty_id` lookup, exposure id insert). Doing the counterparty join in DuckDB cut engine time to 2 s but added about the same DuckDB time on 4 cores; kept the lookup (simpler). Worth revisiting on more cores.
3. Stage-history sort (about 6 s at 1 GB, 4.5 s with 2 GB): bound by DuckDB sort and spill I/O.
4. Memory: DuckDB's share. A streamed, pre-sorted SIM (sorted Parquet written by `sora-tools map`, read without `ORDER BY`) would remove both sorts and most of DuckDB's memory.

## Profiling

Use:

- Linux `perf`
- `heaptrack`
- `valgrind massif` when needed
- Visual Studio Profiler on Windows
- Compiler optimisation reports
- Google Benchmark or a minimal custom benchmark harness

Every optimisation should be based on measured bottlenecks.
