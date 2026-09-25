# Architecture Plan

## Objective

Build a small native engine with explicit ownership, stable data contracts, and minimal runtime overhead.

## Core modules

### 1. Input

Responsibilities:

- Read account, counterparty, exposure, collateral, cash-flow, funding, and market-position data
- Validate minimal structural requirements
- Convert external representations into compact internal records

Design rules:

- Prefer sequential reads
- Use buffered I/O or memory mapping
- Parse directly into final internal types where possible
- Avoid DOM-style JSON parsing for large files

### 2. Scenario

Responsibilities:

- Load scenario parameters
- Resolve scenario inheritance or overlays
- Validate ranges and required fields
- Convert human-readable parameters into immutable runtime structures

Runtime scenario objects should be compact and immutable.

### 3. Segmentation

Responsibilities:

- Map records to stress buckets
- Country
- Sector
- Product
- Rating
- Collateral type
- Maturity band
- Currency
- Counterparty class

Segmentation IDs should be integer-based after load time.

### 4. Stress rules

Responsibilities:

- Convert scenario shocks into record-level effects
- Support additive, multiplicative, lookup-table, and transition-matrix rules
- Avoid virtual dispatch in hot loops

Preferred approaches:

- `std::variant` or tagged structs for rule types
- Precompiled lookup tables
- Function objects instantiated before execution

### 5. Transformation

Responsibilities:

- Apply stress effects
- Produce stressed record state
- Generate event flags
- Preserve traceability to original records

The transformation layer should not allocate per record.

### 6. Validation

Responsibilities:

- Range checks
- Reconciliation checks
- Invariant checks
- Portfolio-level consistency checks

Validation should support configurable levels:

- `off`
- `fast`
- `full`

### 7. Aggregation

Responsibilities:

- Portfolio totals
- Loss totals
- Migration counts
- Stage distribution
- Default counts
- Collateral impact
- Funding and liquidity changes

Use thread-local accumulators followed by deterministic reduction.

### 8. Output

Responsibilities:

- Write stressed records
- Write event stream
- Write aggregated results
- Write diagnostics

Support:

- CSV for inspection
- NDJSON for interoperability
- Compact binary format for performance

## Architectural constraints

- No global mutable state
- No hidden allocations in hot paths
- No exception-based normal control flow
- No lock contention on record processing paths
- No runtime reflection
- No dependency injection framework
