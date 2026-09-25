# Data Model Plan

## Objective

Represent financial records using compact, contiguous structures that are cheap to scan and transform.

## Principles

- Prefer integer IDs over repeated strings
- Prefer fixed-width numeric types
- Use basis points or scaled integers where exact decimal precision is required
- Separate rarely used metadata from hot-path fields
- Avoid owning strings in record structs
- Use dictionaries for repeated dimensions

## Example exposure record

```cpp
struct ExposureRecord {
    std::uint64_t exposureId;
    std::uint32_t counterpartyId;
    std::uint32_t productId;
    std::uint16_t countryId;
    std::uint16_t sectorId;
    std::uint16_t ratingId;
    std::uint16_t currencyId;

    std::int64_t carryingAmountCents;
    std::int64_t eadCents;
    std::int64_t collateralValueCents;

    std::uint32_t pdPpm;
    std::uint32_t lgdPpm;

    std::uint16_t maturityMonths;
    std::uint8_t stage;
    std::uint8_t flags;
};
```

Actual layout should be confirmed with profiling and alignment measurements.

## Structure of Arrays vs Array of Structures

Support both where useful.

Use Array of Structures when:

- The transformation touches most fields together
- Simplicity matters

Use Structure of Arrays when:

- Vectorized processing is beneficial
- Only a subset of columns is needed for specific rules
- Scan throughput dominates

Benchmark both before standardizing.

## String dictionaries

Convert repeated strings during ingestion:

```text
"BE" -> 12
"FR" -> 33
"Manufacturing" -> 104
"Mortgage" -> 7
```

Store dictionaries once per dataset.

## Decimal representation

Avoid floating point for monetary values when exact reconciliation matters.

Recommended:

- Monetary amounts: signed 64-bit scaled integers
- Ratios: 32-bit or 64-bit scaled integers where possible
- Scenario factors: `double` may be acceptable for intermediate calculations
- Final persisted values: normalized scaled representation

## Record identity

Every transformed record must preserve stable identity so baseline and stressed states can be compared efficiently.
