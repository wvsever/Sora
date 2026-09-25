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

## Profiling

Use:

- Linux `perf`
- `heaptrack`
- `valgrind massif` when needed
- Visual Studio Profiler on Windows
- Compiler optimisation reports
- Google Benchmark or a minimal custom benchmark harness

Every optimisation should be based on measured bottlenecks.
