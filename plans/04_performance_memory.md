# Performance and Memory Plan

## Objective

Make predictable memory use and throughput primary architectural concerns.

## Memory strategy

### Avoid

- Per-record heap allocation
- `std::string` in hot-path records
- `std::map` for large runtime lookups
- Nested pointer-heavy object graphs
- Large duplicated baseline/stressed datasets where transformation can be streamed

### Prefer

- `std::vector`
- Flat hash structures where lookup is necessary
- Integer dictionaries
- Reusable buffers
- Arenas for temporary batch data
- Chunk-based processing
- Memory mapped immutable files where useful

## Chunk processing

Recommended baseline:

```text
read chunk
-> transform chunk
-> aggregate chunk
-> write chunk
-> reuse buffers
```

Chunk size should be benchmark-configurable.

Candidates:

- 16k records
- 64k records
- 256k records

## Concurrency

Partition by independent record ranges.

Each worker owns:

- Input slice
- Scratch buffer
- Local aggregation state
- Local diagnostics

Avoid shared mutation during the main loop.

Perform deterministic final reduction after worker completion.

## False sharing

Align frequently updated thread-local counters to cache-line boundaries.

Use padding only after measurement confirms contention or cache-line interference.

## SIMD

Potential SIMD candidates:

- PD multiplication
- LGD shocks
- collateral repricing
- interest-rate transformations
- threshold comparisons

Do not introduce explicit intrinsics until compiler auto-vectorization has been measured.

## Memory budget targets

Example engineering targets:

- Core engine overhead: <100 MB excluding dataset buffers
- Chunk mode: bounded memory independent of full dataset size
- No unbounded caches
- No duplicated dimension strings

## Profiling

Use:

- Linux `perf`
- `heaptrack`
- `valgrind massif` when needed
- Visual Studio Profiler on Windows
- Compiler optimization reports
- Google Benchmark or a minimal custom benchmark harness

Every optimization should be based on measured bottlenecks.
