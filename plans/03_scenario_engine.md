# Scenario Engine Plan

## Objective

Translate scenario definitions into fast immutable runtime rules.

## Rule classes

Initial rule types:

1. Scalar multiplier
2. Scalar additive shock
3. Bounded shock
4. Bucket lookup
5. Transition matrix
6. Threshold event
7. Piecewise function
8. Time-step curve

## Example runtime rule

```cpp
struct PdMultiplierRule {
    std::uint16_t sectorId;
    std::uint16_t countryId;
    double multiplier;
    std::uint32_t floorPpm;
    std::uint32_t capPpm;
};
```

## Compilation step

Human-readable scenario definitions should be compiled before execution into:

- Integer keys
- Dense lookup tables
- Prevalidated transition matrices
- Precomputed coefficients
- Ordered execution stages

The record-processing loop should not perform string comparisons or scenario parsing.

## Determinism

If probabilistic events are supported, they must be reproducible.

Use deterministic pseudo-random values derived from:

```text
hash(scenario_id, record_id, event_type, time_step)
```

This avoids shared RNG state and preserves reproducibility under multithreading.

## Time evolution

Support step-based scenarios:

- 1 month
- 3 months
- 12 months
- 36 months

Rules may define curves instead of one terminal shock.

## Event generation

Examples:

- downgrade
- Stage 1 -> Stage 2
- Stage 2 -> Stage 3
- default
- cure
- collateral revaluation
- deposit withdrawal
- refinancing cost increase

Events should use compact bitsets or fixed structs rather than heap-allocated polymorphic objects.
