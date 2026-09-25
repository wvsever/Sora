# Data Model Plan

## Objective

Represent financial records using compact, contiguous structures that are cheap to scan and transform. Map them one-to-one onto the reference input format in `tests/data/` (see `tests/data/README.md`).

## Principles

- Prefer integer IDs over repeated strings
- Prefer fixed-width numeric types
- Use scaled integers where the source is exact decimal
- Separate rarely used metadata from hot-path fields
- Avoid owning strings in record structs
- Use dictionaries for repeated dimensions
- Keep a stable mapping back to the source business key for output and traceability

## Source format

The input is a Hive-partitioned CSV dataset: 54 tables, 1,051 columns, with types in `_csv_column_types.json`. Only a subset is needed for the engine. Other tables are ignored unless a module declares them.

| Source type | Example | Internal representation |
|---|---|---|
| Amount (2 dp) | `400000.00` | `int64` cents (`Money`) |
| Rate / ratio (up to 9 dp) | `0.038000000` | `int64` scaled 1e9 (`Rate`) |
| Probability (PD, LGD, CCF) | derived | `uint32` parts-per-billion, or `double` in scenario-only code |
| Date | `2048-06-24` | `int32` days since 1970-01-01 (`Date`) |
| Period | `2026-06` | `int32` months since year 0 (`Period`) |
| Boolean | `true` / `false` / empty | bit in a `flags` field plus a "known" bit where null is meaningful |
| Enumerated string | `stage2`, `annuity`, `S.11` | `uint8`/`uint16` enum or dictionary ID |
| Business key | `CL-000001`, `CPTY-011566` | `uint32` dense index plus a key dictionary |
| Null | empty field | per-column sentinel or validity bit |

Parsing must be exact. Amounts and rates are parsed from their decimal digits directly into scaled integers, never through `double`.

## Dimensions (loaded once, integer IDs)

| Dimension | Source | Approximate cardinality |
|---|---|---|
| Entity | `reference/entity.csv` | 70 |
| Currency | manifest / data | 14 |
| Country | counterparty, collateral, entity | < 100 |
| ESA 2010 sector | counterparty | 16 |
| NACE section / division | counterparty `nace_code` | 21 / ~90 |
| Product code | contracts | ~20 per table |
| Portfolio | `reference/portfolio.csv` | 10 |
| GL account | `reference/gl_account.csv` | 216 |
| Rating scale and grade | `counterparty_rating` | 7 scales, < 40 grades |
| Collateral form | collateral | 11 |
| Stress segment | derived (see `09_risk_parameters.md`) | < 1,000 |

## Core records

### Counterparty (dimension-like, fully in memory)

```cpp
struct Counterparty {
    std::uint32_t id;              // dense index of counterparty_id
    std::uint16_t entityId;
    std::uint16_t countryId;       // country_of_residence
    std::uint16_t naceId;          // nace_code (division)
    std::uint8_t  esaSector;       // esa2010_sector
    std::uint8_t  flags;           // natural person, regulated FI, group entity
    std::uint8_t  ratingGradeId;   // best-available long-term external rating, or none
    std::uint8_t  reserved;
    std::uint32_t groupId;         // ultimate_parent_counterparty_id, for concentration
};
```

### Credit exposure (hot path)

One record per contract across `contract_loan`, `contract_commitment`, `contract_lease` and `contract_security_position` at amortised cost or FVOCI. The `table` field keeps the source.

```cpp
struct Exposure {
    std::uint32_t contractIdx;     // dense index; key dictionary holds "CL-000001"
    std::uint32_t counterpartyIdx;
    std::uint16_t entityId;
    std::uint16_t productId;
    std::uint16_t portfolioId;
    std::uint16_t currencyId;
    std::uint16_t segmentId;       // stress segment, resolved at load time
    std::uint8_t  table;           // loan | commitment | lease | security
    std::uint8_t  stage;           // 1, 2, 3, POCI

    std::int64_t  grossCarryingCents;
    std::int64_t  undrawnCents;
    std::int64_t  allowanceCents;
    std::int64_t  collateralCents;   // allocated collateral value after haircuts
    std::int64_t  guaranteeCents;

    std::uint32_t pd12mPpb;          // starting-point parameters (09_risk_parameters.md)
    std::uint32_t pdLifetimePpb;
    std::uint32_t lgdPpb;
    std::uint32_t ccfPpb;

    Date          maturity;
    Date          nextRepricing;
    std::int64_t  rateScaled;        // current_interest_rate, scaled 1e9
    std::uint16_t flags;             // secured, watchlist, forborne, credit_impaired, ...
};
```

Actual layout should be confirmed with profiling and alignment measurements. The target is at most 128 bytes per exposure.

### Other records

- `Collateral`: form, property country, value, valuation date, lien rank. Linked to exposures through a flattened `collateral_allocation` (exposure index, collateral index, allocated amount, rank).
- `Guarantee`: protection form, guarantor counterparty index, amount, protected exposure index.
- `Deposit`: type, DGS coverage, operational and transactional flags, rate, maturity, notice period (funding and NII modules).
- `CashFlow`: contract index, date, type, amount (streamed; NII and repricing).
- `AllowanceHistory` and `ArrearsHistory`: used only by the calibration step, not at stress time.

## Join model

The source is normalised, and stress rules need a denormalised exposure. The join runs once at load time:

```text
contract_loan / commitment / lease / security
   ├─ counterparty            (counterparty_id)          -> country, sector, NACE
   │   └─ counterparty_rating (counterparty_id)          -> best rating
   ├─ collateral_allocation   (contract_id, table)       -> collateral (collateral_id)
   ├─ guarantee_received      (protected_contract_id)
   ├─ impairment_allowance    (contract_id, latest period)
   └─ risk_parameters         (contract_id)              -> PD / LGD / CCF (external or derived)
```

- Counterparties, ratings, collateral and dimensions are small (< 100k rows each). They are loaded fully into flat arrays keyed by dense index. Business-key lookups use a flat hash map that is built once and then frozen.
- Contracts are then streamed per entity partition and resolved against these arrays. There are no string comparisons after load.
- Large streams (`contract_cashflow`, `journal_line`) are never joined in full. They are read per partition and aggregated by contract index.

## Structure of Arrays vs Array of Structures

Support both where useful.

Use Array of Structures when:

- The transformation touches most fields together
- Simplicity matters

Use Structure of Arrays when:

- Vectorised processing is beneficial
- Only a subset of columns is needed for specific rules
- Scan throughput dominates

Benchmark both before standardising.

## String dictionaries

Convert repeated strings during ingestion:

```text
"BE"        -> 12
"S.11"      -> 3
"RESI_MTG"  -> 7
"CL-000001" -> 0
```

Store dictionaries once per dataset. Keep them available for output.

## Decimal representation

Avoid floating point for monetary values when exact reconciliation matters.

- Monetary amounts: signed 64-bit cents. The maximum observed value (5.5e12 cents) leaves ample headroom. Aggregations use `int64`, with overflow checks in validation.
- Rates: 64-bit integer scaled 1e9, matching the source precision exactly.
- Probabilities: 32-bit parts-per-billion for storage. `double` is allowed inside rule evaluation.
- Final persisted values: normalised scaled representation. Loss amounts are rounded to cents with a documented rounding mode (half-even).

## Record identity

Every transformed record keeps its source `(table, contract_id)` through the dense index and key dictionary. This lets baseline and stressed states be compared, and lets outputs be joined back to the source dataset.
