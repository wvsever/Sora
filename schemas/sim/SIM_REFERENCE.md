# Sora Input Model (SIM) 1.0.0-draft

The Sora Input Model (SIM) is the canonical input of the Sora stress engine. It describes a bank's
credit portfolio at one reference date: counterparties, credit exposures, collateral, guarantees,
and the history needed to calibrate stress parameters.

Customers produce SIM tables from their own systems with mapping SQL, usually run by
`sora-tools map` on exported source files. Sora never reads customer source layouts directly.

## Conventions

- Every SIM dataset describes a single reference date, given in `sim_manifest.json`.
- Amounts are in the currency of the row (`currency` column), not converted. Sora converts with `sim_fx_rate` at the reference date.
- Amounts are positive unless a column says otherwise. Allowances and provisions are positive numbers, not negative contra-amounts.
- Probabilities, rates and ratios are decimal fractions (0.0125 = 1.25%), never percentages.
- Business keys are strings, stable across reporting dates, and unique within their table across the whole dataset (not only within an entity).
- Missing values are NULL. Do not use sentinel values such as 0, -1, '9999-12-31' or 'N/A'.
- Code lists (enums) are case-sensitive and use lower_snake_case.
- Files are Parquet (preferred) or CSV, stored as `<table>/**/*.parquet` or `<table>/**/*.csv`. Partitioning by `entity_id=` is recommended for large tables.

## Types

| Type | Storage | Description |
|---|---|---|
| `string` | `VARCHAR` | UTF-8 text. |
| `key` | `VARCHAR` | Business key. Stable, case-sensitive, at most 64 characters, no leading or trailing spaces. |
| `integer` | `BIGINT` | Whole number. |
| `boolean` | `BOOLEAN` | True or false. NULL means unknown. CSV: `true` / `false` |
| `date` | `DATE` | Calendar date. CSV: YYYY-MM-DD |
| `amount` | `DECIMAL(18,2)` | Monetary amount in the currency of the row, exact to the cent. CSV: Decimal with at most 2 decimals, `.` as separator, no thousands separator (`1250000.00`). |
| `rate` | `DECIMAL(18,9)` | Interest rate or ratio as a decimal fraction per annum (0.038 = 3.8%). May be negative. CSV: Decimal with at most 9 decimals (`0.038000000`). |
| `probability` | `DECIMAL(18,9)` | Probability or loss rate as a decimal fraction in [0, 1]. CSV: Decimal with at most 9 decimals. |
| `currency` | `VARCHAR` | ISO 4217 alphabetic currency code (EUR, USD, ...). |
| `country` | `VARCHAR` | ISO 3166-1 alpha-2 country code (BE, DE, ...). Use `XX` only for supranational organisations. |

## Tables

- [`sim_entity`](#sim_entity): Legal entities in the consolidation scope.
- [`sim_counterparty`](#sim_counterparty): Obligors, guarantors, collateral providers and issuers.
- [`sim_rating`](#sim_rating): Ratings of counterparties at the reference date, both internal and external.
- [`sim_exposure`](#sim_exposure): Credit exposures at the reference date: loans and advances, debt securities, finance leases and off-balance-sheet commitments and guarantees given.
- [`sim_collateral`](#sim_collateral): Collateral items and funded credit protection received.
- [`sim_collateral_allocation`](#sim_collateral_allocation): Allocation of collateral to exposures (many-to-many).
- [`sim_guarantee`](#sim_guarantee): Unfunded credit protection received (guarantees, credit derivatives, credit insurance).
- [`sim_stage_history`](#sim_stage_history): Month-end history of stage and loss allowance per exposure.
- [`sim_credit_event`](#sim_credit_event): Credit events per exposure (defaults, cures, forbearance, write-offs, distressed sales).
- [`sim_recovery_flow`](#sim_recovery_flow): Cash flows after default (recoveries and workout costs) and write-offs.
- [`sim_fx_rate`](#sim_fx_rate): Exchange rates to the reporting currency.
- [`sim_risk_parameter`](#sim_risk_parameter): Credit risk parameters supplied by the institution (IFRS 9 / IRB models, satellite models, benchmarks) or produced by `sora calibrate`.

### sim_entity

Legal entities in the consolidation scope. Every exposure, collateral and history row belongs to one entity.

- **Grain:** One row per legal entity.
- **Primary key:** `entity_id`
- **Required by modules:** core
- **Foreign key:** (parent_entity_id) → `sim_entity`

| Column | Type | Req. | Description | Example |
|---|---|---|---|---|
| `entity_id` | key (VARCHAR) | yes | Identifier of the legal entity. Used as the partition key of other SIM tables. | `BANK-CPP-001` |
| `parent_entity_id` | key (VARCHAR) |  | Direct parent entity within the scope. NULL for the top entity. | `BANK-CPP-001` |
| `lei` | string (VARCHAR) |  | Legal Entity Identifier (ISO 17442), 20 characters. | `549300000081VT639246` |
| `country` | country (VARCHAR) | yes | Country of incorporation. | `BE` |
| `functional_currency` | currency (VARCHAR) | yes | Functional currency of the entity (IAS 21). | `EUR` |
| `entity_type` | string (VARCHAR) |  | Free-text or institution-defined entity type (credit institution, leasing company, insurer, ...). Informational. | `credit_institution` |
| `consolidation_method` | string (VARCHAR) |  | Accounting consolidation method (full, proportional, equity, not_consolidated). NULL for the top entity. | `full` |
| `ownership_pct` | probability (DECIMAL(18,9)) |  | Share held by the parent, as a fraction (1.0 = 100%). | `1.000000000` |

### sim_counterparty

Obligors, guarantors, collateral providers and issuers. Sora uses it to segment exposures (sector, country, SME), and for group structure and concentration. Personal data is not needed: do not map names, addresses or national identifiers of natural persons.

- **Grain:** One row per counterparty, group-wide (not per entity).
- **Primary key:** `counterparty_id`
- **Required by modules:** core

| Column | Type | Req. | Description | Example |
|---|---|---|---|---|
| `counterparty_id` | key (VARCHAR) | yes | Counterparty identifier, unique across the whole group. | `CPTY-000001` |
| `country_of_residence` | country (VARCHAR) | yes | Country of residence (natural persons) or of the registered office (legal persons). Drives the EBA country split and macro-scenario country assignment. *Ref: AnaCredit counterparty reference data, 'address - country'.* | `BE` |
| `esa2010_sector` | string (VARCHAR) | yes | Institutional sector according to ESA 2010 (Regulation (EU) 549/2013), e.g. S.11, S.121, S.122, S.1311, S.14, S.15. Use the most granular code available. *Ref: ESA 2010 chapter 2; FINREP counterparty sector breakdown.* | `S.11` |
| `eba_sector` | enum `eba_sector` | yes | EBA stress-test sector, derived from `esa2010_sector` with the mapping in the enum description. Provided explicitly so that institution-specific classification decisions are visible in the mapping. | `non_financial_corporation` |
| `nace_code` | string (VARCHAR) |  | NACE Rev. 2 (or Rev. 2.1) activity code of non-financial corporations. Section letter plus optional division, group and class (e.g. `C`, `C10`, `C10.1`, `C10.11`). Required for `eba_sector = non_financial_corporation` (CR_SECTOR template). **Pitfall:** Source systems often omit the section letter ("10.11"). The mapping must add it. | `C10.11` |
| `is_natural_person` | boolean (BOOLEAN) | yes | True for natural persons (retail customers, sole proprietors treated as persons). | `false` |
| `is_sme` | boolean (BOOLEAN) |  | Small or medium-sized enterprise according to Commission Recommendation 2003/361/EC (fewer than 250 employees, and turnover ≤ EUR 50m or balance sheet total ≤ EUR 43m). Required for non-financial corporations. It drives the EBA SME / non-SME split. **Pitfall:** Apply the group-level (consolidated) thresholds where the counterparty belongs to a group. | `true` |
| `annual_turnover_eur` | amount (DECIMAL(18,2)) |  | Latest annual turnover in EUR (consolidated at group level where applicable). Used for SME determination and the IRB SME correlation adjustment. | `12500000.00` |
| `total_assets_eur` | amount (DECIMAL(18,2)) |  | Latest balance sheet total in EUR. | `8300000.00` |
| `number_of_employees` | integer (BIGINT) |  | Number of employees (full-time equivalents). | `85` |
| `group_id` | key (VARCHAR) |  | Identifier of the group of connected clients (CRR Art. 4(1)(39)), typically the ultimate parent counterparty id. Used for concentration analysis and the CCR largest-counterparty stress. | `CPTY-004120` |
| `lei` | string (VARCHAR) |  | Legal Entity Identifier of legal persons. | `529900T8BM49AURSDO55` |

Checks:

- `CPT-001` (error): NFCs must have a NACE code.
- `CPT-002` (warning): NFCs should have an SME indicator.
- `CPT-003` (error): Natural persons are households.

### sim_rating

Ratings of counterparties at the reference date, both internal and external. Used for segmentation, as a fallback PD source (master scale), and for SA risk weights (credit quality step).

- **Grain:** One row per counterparty, rating source and rating scale (the latest rating on or before the reference date).
- **Primary key:** `counterparty_id`, `rating_source`, `rating_scale`
- **Required by modules:** credit
- **Foreign key:** (counterparty_id) → `sim_counterparty`

| Column | Type | Req. | Description | Example |
|---|---|---|---|---|
| `counterparty_id` | key (VARCHAR) | yes | Rated counterparty. | `CPTY-000123` |
| `rating_source` | enum `rating_source` | yes | Who assigned the rating. | `sp` |
| `rating_scale` | string (VARCHAR) | yes | Scale identifier, unique per source (e.g. `SNP_LT` for S&P long-term issuer, `INTERNAL_CORP_MS` for an internal corporate master scale). | `SNP_LT` |
| `is_long_term` | boolean (BOOLEAN) | yes | True for long-term scales, false for short-term scales. | `true` |
| `rating_grade` | string (VARCHAR) | yes | Grade as published on the scale (`BBB`, `Baa`, `R-1 M`, internal grade `5`). | `BBB` |
| `credit_quality_step` | integer (BIGINT) |  | Credit quality step 1-6 according to the ECAI mapping (Commission Implementing Regulation (EU) 2016/1799, as amended). Required for external long-term ratings used under SA. NULL for internal ratings. | `3` |
| `pd_master_scale` | probability (DECIMAL(18,9)) |  | Through-the-cycle PD of the grade on the institution's master scale, if available (internal ratings). | `0.004200000` |
| `rating_date` | date (DATE) | yes | Date the rating was assigned or last confirmed. | `2026-03-14` |

### sim_exposure

Credit exposures at the reference date: loans and advances, debt securities, finance leases and off-balance-sheet commitments and guarantees given. This is the population the stress engine projects. Include all exposures subject to credit risk, whatever their measurement category, so that totals reconcile with FINREP. The engine selects the ones in scope of each module (e.g. amortised cost only for EBA impairment projections).

- **Grain:** One row per contract (facility). A facility with drawn and undrawn parts is one row: the drawn part in `gross_carrying_amount` and the undrawn part in `off_balance_amount`, with `exposure_type` describing the product. Do not also report the undrawn part as a separate row.
- **Primary key:** `exposure_id`
- **Partitioned by:** `entity_id`
- **Required by modules:** core, credit
- **Foreign key:** (entity_id) → `sim_entity`
- **Foreign key:** (counterparty_id) → `sim_counterparty`

| Column | Type | Req. | Description | Example |
|---|---|---|---|---|
| `exposure_id` | key (VARCHAR) | yes | Contract identifier, unique across the dataset and stable over time. Used to join history, collateral, guarantees and parameters. | `CL-000001` |
| `entity_id` | key (VARCHAR) | yes | Legal entity that books the exposure. | `BANK-CPP-001` |
| `counterparty_id` | key (VARCHAR) | yes | Obligor (debtor). For debt securities, the issuer. | `CPTY-000001` |
| `exposure_type` | enum `exposure_type` | yes | Kind of exposure. It determines on-balance vs off-balance treatment. | `loan` |
| `product_code` | string (VARCHAR) | yes | Institution's product code. Used for segmentation and reporting. Not interpreted by the engine unless configured. | `RESI_MTG` |
| `portfolio_id` | string (VARCHAR) |  | Institution's accounting or business portfolio. | `PF-RETAIL-MORTGAGE` |
| `currency` | currency (VARCHAR) | yes | Currency of the contract and of all amounts on this row. | `EUR` |
| `measurement_category` | enum `measurement_category` | yes | IFRS 9 measurement category. For off-balance items, the category of the expected drawn exposure (normally amortised_cost). *Ref: FINREP F 04.xx accounting portfolios.* | `amortised_cost` |
| `stage` | enum `stage` | yes | IFRS 9 stage at the reference date. Use `not_applicable` for instruments not subject to impairment. *Ref: IFRS 9 5.5; FINREP F 04.04.1, F 18.00.* | `stage1` |
| `origination_date` | date (DATE) |  | Date of initial recognition (or of the latest recognition after substantial modification). | `2023-07-01` |
| `maturity_date` | date (DATE) |  | Legal final maturity date. NULL for open-ended facilities (current accounts, credit cards). | `2048-06-24` |
| `gross_carrying_amount` | amount (DECIMAL(18,2)) |  | Gross carrying amount of the on-balance (drawn) part (IFRS 9 Appendix A): amortised cost before deducting the loss allowance, including accrued interest, and after write-offs. For FVTPL/HFT instruments, the carrying amount. Required for on-balance exposure types. For off-balance types, the amount drawn or called and booked on balance, or NULL/0. *Ref: FINREP F 04.04.1 col 015; F 18.00.* **Pitfall:** Do not deduct the allowance. Do not exclude accrued interest. | `400000.00` |
| `accrued_interest` | amount (DECIMAL(18,2)) |  | Accrued interest included in `gross_carrying_amount`. Informational, used for NII. | `1250.00` |
| `off_balance_amount` | amount (DECIMAL(18,2)) |  | Nominal amount of the off-balance-sheet part: undrawn committed amount of a facility, or the guaranteed amount of a financial guarantee or other commitment given. Required for off-balance exposure types. Optional for loans with an undrawn part. *Ref: FINREP F 09.01.1 (nominal amount).* | `150000.00` |
| `committed_amount` | amount (DECIMAL(18,2)) |  | Total committed limit of the facility (drawn plus undrawn). | `550000.00` |
| `loss_allowance` | amount (DECIMAL(18,2)) |  | Accumulated impairment (on-balance) or provision (off-balance) under IFRS 9 at the reference date, as a positive amount. 0 if none. Required when `stage` is not `not_applicable` (check EXP-005). NULL for instruments not subject to impairment. *Ref: FINREP F 04.04.1, F 09.01.1, F 12.01.* | `320.55` |
| `accumulated_write_off` | amount (DECIMAL(18,2)) |  | Cumulative partial write-offs to date (amounts derecognised but still legally claimed). | `0.00` |
| `interest_rate_type` | enum `interest_rate_type` |  | Interest rate type. | `fixed` |
| `current_interest_rate` | rate (DECIMAL(18,9)) |  | Contractual rate applicable at the reference date, per annum. | `0.038000000` |
| `reference_rate` | string (VARCHAR) |  | Reference rate index for floating contracts (`EURIBOR_3M`, `ESTR`, ...). | `EURIBOR_3M` |
| `interest_spread` | rate (DECIMAL(18,9)) |  | Spread over the reference rate for floating contracts. | `0.015000000` |
| `next_repricing_date` | date (DATE) |  | Next date the rate resets (floating) or is renegotiated (fixed with a reset). NULL if fixed to maturity. | `2026-09-30` |
| `amortisation_type` | string (VARCHAR) |  | Repayment profile (annuity, linear, bullet, interest_only, custom). | `annuity` |
| `days_past_due` | integer (BIGINT) |  | Days past due of the oldest material past-due amount at the reference date (CRR Art. 178 counting). 0 if not past due. | `0` |
| `is_defaulted` | boolean (BOOLEAN) |  | In default per CRR Art. 178 at the reference date. | `false` |
| `is_credit_impaired` | boolean (BOOLEAN) |  | Credit-impaired per IFRS 9 Appendix A. | `false` |
| `is_forborne` | boolean (BOOLEAN) |  | Forborne exposure per CRR Art. 47b (forbearance measure granted and not yet ended probation). | `false` |
| `is_watchlist` | boolean (BOOLEAN) |  | On the institution's watchlist. | `false` |
| `is_revolving` | boolean (BOOLEAN) |  | Revolving facility (can be redrawn after repayment). | `false` |
| `is_unconditionally_cancellable` | boolean (BOOLEAN) |  | The institution may cancel the undrawn part unconditionally at any time without notice (relevant for CCF). | `false` |
| `is_intragroup` | boolean (BOOLEAN) |  | Exposure to another entity of the same consolidation scope. Intragroup exposures are eliminated in consolidated results. | `false` |
| `household_purpose` | enum `household_purpose` |  | Purpose of lending to households. Required when the counterparty is a household and `exposure_type = loan`. | `house_purchase` |
| `is_cre` | boolean (BOOLEAN) |  | Commercial real estate lending: exposures to NFCs whose repayment depends on income from, or sale of, commercial immovable property, or which finance its acquisition or development. Required for NFC loans (EBA split SME-CRE / non-SME-CRE). | `false` |
| `country_of_risk` | country (VARCHAR) |  | Country whose macro scenario drives the exposure if different from the obligor's residence (e.g. project location). NULL = counterparty country of residence. | `DE` |

Checks:

- `EXP-001` (error): On-balance exposures have a gross carrying amount.
- `EXP-002` (error): Off-balance exposure types have an off-balance amount.
- `EXP-003` (error): Instruments subject to impairment have a stage other than not_applicable. Others have not_applicable.
- `EXP-004` (error): Amounts are not negative.
- `EXP-005` (error): Impaired instruments report a loss allowance (0 allowed).
- `EXP-006` (warning): Maturity is not before origination.
- `EXP-007` (warning): Loss allowance does not exceed the exposure.
- `EXP-008` (warning): Stage 3 exposures are credit-impaired.
- `EXP-101` (error): Household loans have a household purpose.
- `EXP-102` (warning): NFC loans state whether they are CRE.

### sim_collateral

Collateral items and funded credit protection received. Values are at the reference date. Linked to exposures through `sim_collateral_allocation`.

- **Grain:** One row per collateral item.
- **Primary key:** `collateral_id`
- **Partitioned by:** `entity_id`
- **Required by modules:** credit
- **Foreign key:** (entity_id) → `sim_entity`
- **Foreign key:** (issuer_counterparty_id) → `sim_counterparty`
- **Foreign key:** (provider_counterparty_id) → `sim_counterparty`

| Column | Type | Req. | Description | Example |
|---|---|---|---|---|
| `collateral_id` | key (VARCHAR) | yes | Collateral identifier. | `COLL-000001` |
| `entity_id` | key (VARCHAR) | yes | Entity holding the collateral. | `BANK-CPP-001` |
| `collateral_type` | enum `collateral_type` | yes | Type of collateral. Drives haircuts and the collateral value path in scenarios. | `residential_property` |
| `currency` | currency (VARCHAR) | yes | Currency of the collateral value. | `EUR` |
| `market_value` | amount (DECIMAL(18,2)) | yes | Current market value (financial collateral) or latest appraised value (physical collateral), before regulatory or internal haircuts, and before prior-ranking claims. *Ref: CRR Art. 229; AnaCredit protection value.* | `487369.82` |
| `valuation_date` | date (DATE) | yes | Date of the latest valuation. | `2026-06-13` |
| `valuation_method` | string (VARCHAR) |  | Valuation method (full_appraisal, desktop_appraisal, indexed, statistical, mark_to_market, mark_to_model). | `full_appraisal` |
| `value_at_origination` | amount (DECIMAL(18,2)) |  | Value at origination of the secured exposure (used for LTV at origination). | `380605.16` |
| `property_country` | country (VARCHAR) |  | Country where the property is located. Required for property collateral, because property price shocks are country-specific. | `AT` |
| `lien_rank` | integer (BIGINT) |  | Rank of the institution's lien (1 = first lien). Property collateral only. | `1` |
| `prior_ranking_claims` | amount (DECIMAL(18,2)) |  | Claims of third parties ranking before the institution's lien. | `0.00` |
| `issuer_counterparty_id` | key (VARCHAR) |  | Issuer of securities collateral. | `CPTY-000456` |
| `provider_counterparty_id` | key (VARCHAR) |  | Counterparty that provided the collateral, if different from the obligor. | `CPTY-011566` |

Checks:

- `COL-001` (error): Property collateral has a property country.
- `COL-002` (error): Market value is not negative.

### sim_collateral_allocation

Allocation of collateral to exposures (many-to-many). Sora uses the allocated amounts to compute LTV and secured/unsecured parts. If the institution has no allocation, provide the full market value with one row per exposure–collateral pair. Sora then applies a pro-rata allocation (configurable).

- **Grain:** One row per exposure and collateral item.
- **Primary key:** `exposure_id`, `collateral_id`
- **Required by modules:** credit
- **Foreign key:** (exposure_id) → `sim_exposure`
- **Foreign key:** (collateral_id) → `sim_collateral`

| Column | Type | Req. | Description | Example |
|---|---|---|---|---|
| `exposure_id` | key (VARCHAR) | yes | Secured exposure. | `CL-000001` |
| `collateral_id` | key (VARCHAR) | yes | Collateral item. | `COLL-000001` |
| `allocated_amount` | amount (DECIMAL(18,2)) |  | Part of the collateral value allocated to this exposure, in the collateral's currency. NULL = not allocated by the institution. | `400000.00` |
| `allocation_rank` | integer (BIGINT) |  | Order in which collateral is applied to the exposure (1 = first). | `1` |

Checks:

- `CAL-001` (error): Allocated amount is not negative.
- `CAL-101` (warning): Total allocations of a collateral item do not exceed its market value.

### sim_guarantee

Unfunded credit protection received (guarantees, credit derivatives, credit insurance). It protects either one exposure or all exposures to a counterparty.

- **Grain:** One row per protection item.
- **Primary key:** `guarantee_id`
- **Required by modules:** credit
- **Foreign key:** (entity_id) → `sim_entity`
- **Foreign key:** (guarantor_counterparty_id) → `sim_counterparty`
- **Foreign key:** (protected_exposure_id) → `sim_exposure`
- **Foreign key:** (protected_counterparty_id) → `sim_counterparty`

| Column | Type | Req. | Description | Example |
|---|---|---|---|---|
| `guarantee_id` | key (VARCHAR) | yes | Protection identifier. | `GUAR-000001` |
| `entity_id` | key (VARCHAR) | yes | Entity receiving the protection. | `BANK-CPP-001` |
| `protection_type` | enum `protection_type` | yes | Type of protection. | `guarantee` |
| `guarantor_counterparty_id` | key (VARCHAR) | yes | Protection provider. Its sector and rating determine substitution effects. | `CPTY-000789` |
| `protected_exposure_id` | key (VARCHAR) |  | Protected exposure. NULL if the protection covers a counterparty as a whole. | `CL-000123` |
| `protected_counterparty_id` | key (VARCHAR) |  | Protected counterparty, when the protection is not exposure-specific. | `CPTY-000001` |
| `currency` | currency (VARCHAR) | yes | Currency of the protected amount. | `EUR` |
| `protected_amount` | amount (DECIMAL(18,2)) | yes | Maximum amount covered. | `250000.00` |
| `start_date` | date (DATE) |  | Start of the protection. | `2024-01-15` |
| `end_date` | date (DATE) |  | End of the protection. NULL = open-ended or aligned with the exposure maturity. | `2029-01-15` |

Checks:

- `GUA-001` (error): Protection covers an exposure or a counterparty.
- `GUA-002` (error): Protected amount is not negative.

### sim_stage_history

Month-end history of stage and loss allowance per exposure. Used by `sora calibrate` to estimate stage transition rates (TR1-2, TR2-1, PD12M S1/S2) and loss rates. Provide at least 12 months, preferably 5 years. Include the reference date.

- **Grain:** One row per exposure and period end.
- **Primary key:** `exposure_id`, `period_end`
- **Partitioned by:** `entity_id`
- **Required by modules:** calibration
- **Foreign key:** (entity_id) → `sim_entity`

| Column | Type | Req. | Description | Example |
|---|---|---|---|---|
| `exposure_id` | key (VARCHAR) | yes | Exposure. It may no longer exist at the reference date (matured, written off, sold). Such exposures are still needed for unbiased transition rates. | `CL-000001` |
| `entity_id` | key (VARCHAR) | yes | Booking entity. | `BANK-CPP-001` |
| `period_end` | date (DATE) | yes | Last calendar day of the observed month. | `2026-05-31` |
| `stage` | enum `stage` | yes | Stage at period end. | `stage2` |
| `currency` | currency (VARCHAR) | yes | Currency of the amounts. | `EUR` |
| `gross_carrying_amount` | amount (DECIMAL(18,2)) |  | Gross carrying amount at period end (on-balance). Used as the exposure weight. | `398500.00` |
| `off_balance_amount` | amount (DECIMAL(18,2)) |  | Off-balance nominal at period end. | `0.00` |
| `loss_allowance` | amount (DECIMAL(18,2)) | yes | Loss allowance or provision at period end (positive). | `4120.00` |
| `write_off_in_period` | amount (DECIMAL(18,2)) |  | Amount written off during the month. | `0.00` |

Checks:

- `STH-001` (error): Period end is a month end.
- `STH-002` (error): Stage history covers impaired instruments only.

### sim_credit_event

Credit events per exposure (defaults, cures, forbearance, write-offs, distressed sales). Used for default rates and to delimit workout periods for LGD.

- **Grain:** One row per exposure, event type and event date.
- **Primary key:** `exposure_id`, `event_type`, `event_date`
- **Partitioned by:** `entity_id`
- **Required by modules:** calibration
- **Foreign key:** (entity_id) → `sim_entity`

| Column | Type | Req. | Description | Example |
|---|---|---|---|---|
| `exposure_id` | key (VARCHAR) | yes | Exposure concerned. | `CL-004512` |
| `entity_id` | key (VARCHAR) | yes | Booking entity. | `BANK-CPP-001` |
| `event_type` | enum `credit_event_type` | yes | Type of event. | `default` |
| `event_date` | date (DATE) | yes | Date the event occurred (not the booking date). | `2025-11-03` |
| `currency` | currency (VARCHAR) |  | Currency of `amount`. | `EUR` |
| `amount` | amount (DECIMAL(18,2)) |  | Amount associated with the event (exposure at default for `default`, amount written off for `write_off`, sale price for `distressed_sale`). | `125000.00` |

### sim_recovery_flow

Cash flows after default (recoveries and workout costs) and write-offs. Used to estimate realised (workout) LGD.

- **Grain:** One row per exposure, flow date and flow type (aggregate multiple flows of the same type on the same day).
- **Primary key:** `exposure_id`, `flow_date`, `flow_type`
- **Partitioned by:** `entity_id`
- **Required by modules:** calibration
- **Foreign key:** (entity_id) → `sim_entity`

| Column | Type | Req. | Description | Example |
|---|---|---|---|---|
| `exposure_id` | key (VARCHAR) | yes | Defaulted exposure. | `CL-004512` |
| `entity_id` | key (VARCHAR) | yes | Booking entity. | `BANK-CPP-001` |
| `flow_date` | date (DATE) | yes | Date of the cash flow. | `2026-02-10` |
| `flow_type` | enum `recovery_flow_type` | yes | Type of flow. | `recovery` |
| `currency` | currency (VARCHAR) | yes | Currency of the amount. | `EUR` |
| `amount` | amount (DECIMAL(18,2)) | yes | Amount, positive. | `18000.00` |

Checks:

- `REC-001` (error): Amounts are positive.

### sim_fx_rate

Exchange rates to the reporting currency. At least the reference date is required for every currency used in the dataset. History is optional (used to convert history tables).

- **Grain:** One row per currency and rate date.
- **Primary key:** `currency`, `rate_date`
- **Required by modules:** core

| Column | Type | Req. | Description | Example |
|---|---|---|---|---|
| `currency` | currency (VARCHAR) | yes | Currency being converted. | `USD` |
| `rate_date` | date (DATE) | yes | Date of the rate (closing rate). | `2026-06-30` |
| `rate_to_reporting` | rate (DECIMAL(18,9)) | yes | Units of reporting currency per 1 unit of `currency` (amount_reporting = amount × rate). 1 for the reporting currency itself. **Pitfall:** Many sources quote the inverse (units of foreign currency per EUR). Invert when needed. | `0.912345678` |

Checks:

- `FXR-001` (error): Rates are positive.

### sim_risk_parameter

Credit risk parameters supplied by the institution (IFRS 9 / IRB models, satellite models, benchmarks) or produced by `sora calibrate`. They are given per exposure or per segment, for the starting point (`scenario = actual`, `year = 0`) and optionally for projection years. All are 12-month point-in-time values unless stated otherwise (EBA methodology).

- **Grain:** One row per level, key, scenario and projection year.
- **Primary key:** `level`, `key`, `scenario`, `year`
- **Required by modules:** credit

| Column | Type | Req. | Description | Example |
|---|---|---|---|---|
| `level` | string (VARCHAR) | yes | `exposure` (key = exposure_id) or `segment` (key = segment key, see the segmentation configuration). Allowed: `exposure`, `segment`. | `segment` |
| `key` | key (VARCHAR) | yes | Exposure id or segment key (e.g. `LOANS/NFC/SME_CRE/BE`). | `LOANS|NFC|SME_CRE|BE` |
| `scenario` | enum `scenario` | yes | `actual` for the starting point; `baseline`/`adverse` for projected values. | `actual` |
| `year` | integer (BIGINT) | yes | 0 = reference date (starting point), 1..n = projection year. | `0` |
| `pd12m_s1` | probability (DECIMAL(18,9)) |  | 12-month probability of moving from stage 1 to stage 3 (EBA TR1-3 / PD 12M S1). | `0.004000000` |
| `pd12m_s2` | probability (DECIMAL(18,9)) |  | 12-month probability of moving from stage 2 to stage 3 (EBA TR2-3 / PD 12M S2). | `0.090000000` |
| `tr1_2` | probability (DECIMAL(18,9)) |  | 12-month transition rate from stage 1 to stage 2. | `0.060000000` |
| `tr2_1` | probability (DECIMAL(18,9)) |  | 12-month transition rate from stage 2 to stage 1. | `0.250000000` |
| `tr3_1` | probability (DECIMAL(18,9)) |  | 12-month cure rate from stage 3 to stage 1 (starting point only). | `0.010000000` |
| `tr3_2` | probability (DECIMAL(18,9)) |  | 12-month cure rate from stage 3 to stage 2 (starting point only). | `0.050000000` |
| `lgd_s1` | probability (DECIMAL(18,9)) |  | Loss given default for exposures defaulting from stage 1. | `0.220000000` |
| `lgd_s2` | probability (DECIMAL(18,9)) |  | Loss given default for exposures defaulting from stage 2. | `0.240000000` |
| `lgd_s3` | probability (DECIMAL(18,9)) |  | Lifetime loss rate on the existing stage 3 stock. | `0.350000000` |
| `lrlt_s2` | probability (DECIMAL(18,9)) |  | Lifetime loss rate on stage 2 exposures (lifetime ECL / exposure). | `0.090000000` |
| `ccf` | probability (DECIMAL(18,9)) |  | Credit conversion factor for off-balance amounts. | `0.400000000` |
| `pd_reg` | probability (DECIMAL(18,9)) |  | Regulatory (IRB) PD, through the cycle, including floors. | `0.005000000` |
| `lgd_reg` | probability (DECIMAL(18,9)) |  | Regulatory (IRB) LGD, downturn, including floors. | `0.250000000` |
| `source` | enum `parameter_source` | yes | Origin of the values. | `external` |

Checks:

- `RPA-001` (error): Starting point uses scenario actual and year 0, and only it does.
- `RPA-002` (error): Stage 1 outflows do not exceed 100%.
- `RPA-003` (error): Stage 2 outflows do not exceed 100%.

## Code lists

### `stage`

IFRS 9 impairment stage at the reference date (IFRS 9 5.5.3-5.5.5, B5.5.26; purchased or originated credit-impaired per 5.5.13).

| Value | Meaning |
|---|---|
| `stage1` | 12-month expected credit loss. No significant increase in credit risk since initial recognition. |
| `stage2` | Lifetime expected credit loss, not credit-impaired. Significant increase in credit risk. |
| `stage3` | Lifetime expected credit loss, credit-impaired (usually aligned with default). |
| `poci` | Purchased or originated credit-impaired. |
| `not_applicable` | Not subject to IFRS 9 impairment (fair value through profit or loss, held for trading, equity). |

### `measurement_category`

IFRS 9 measurement category (FINREP accounting portfolio).

| Value | Meaning |
|---|---|
| `amortised_cost` | Amortised cost (hold to collect, SPPI passed). |
| `fvoci` | Fair value through other comprehensive income (debt instruments, with recycling). |
| `fvoci_equity` | Equity instruments designated at FVOCI (no recycling, no impairment). |
| `fvtpl_mandatory` | Non-trading, mandatorily at fair value through profit or loss. |
| `fvtpl_designated` | Designated at fair value through profit or loss (fair value option). |
| `held_for_trading` | Held for trading. |

### `exposure_type`

Kind of credit exposure. It drives on- or off-balance-sheet treatment and the EBA template portfolio (loans vs debt securities vs off-balance).

| Value | Meaning |
|---|---|
| `loan` | Loans and advances, including overdrafts, credit card balances, reverse repos with non-banks, and trade receivables. |
| `debt_security` | Debt security held (bond, note, commercial paper). |
| `finance_lease` | Finance lease receivable (lessor). |
| `loan_commitment` | Loan commitment given (undrawn committed facility). |
| `financial_guarantee` | Financial guarantee given. |
| `other_commitment` | Other commitment given (documentary credits, performance guarantees, acceptances, NIFs). |

### `eba_sector`

Counterparty sector used for EBA stress-test portfolios (CR_SCEN), derived from ESA 2010 sector. Mapping: S.121 → central_bank. S.13x → general_government. S.122 → credit_institution. S.123-S.129 → other_financial. S.11 → non_financial_corporation. S.14, S.15 → household.

| Value | Meaning |
|---|---|
| `central_bank` | Central banks (ESA S.121). |
| `general_government` | General governments, including regional, local and social security (ESA S.13). |
| `credit_institution` | Credit institutions (ESA S.122 deposit-taking corporations except the central bank). |
| `other_financial` | Other financial corporations (ESA S.123-S.129 - money market funds, investment funds, other financial intermediaries, auxiliaries, captives, insurance, pension funds). |
| `non_financial_corporation` | Non-financial corporations (ESA S.11). |
| `household` | Households, including sole proprietors and non-profit institutions serving households (ESA S.14, S.15). |

### `household_purpose`

Purpose of household lending (FINREP F 06 / EBA CR_SCEN).

| Value | Meaning |
|---|---|
| `house_purchase` | Lending for house purchase, secured by residential property (includes remortgage). |
| `consumption` | Consumer credit (credit cards, personal loans, car loans, instalment credit). |
| `other` | Other household lending (e.g. equity release, lending to sole proprietors for business purposes). |

### `collateral_type`

Type of collateral or funded credit protection.

| Value | Meaning |
|---|---|
| `residential_property` | Residential immovable property. |
| `commercial_property` | Commercial immovable property (offices, retail, industrial, hotels, land). |
| `cash` | Cash or deposits held with the institution. |
| `debt_security` | Debt securities. |
| `equity_security` | Equities. |
| `fund_unit` | Units in collective investment undertakings. |
| `gold` | Gold. |
| `life_policy` | Life insurance policies pledged or assigned. |
| `receivables` | Receivables. |
| `other_physical` | Other physical collateral (vehicles, equipment, ships, aircraft, inventory). |
| `other` | Other collateral. |

### `protection_type`

Type of unfunded credit protection received.

| Value | Meaning |
|---|---|
| `guarantee` | Guarantee. |
| `counter_guarantee` | Counter-guarantee. |
| `credit_derivative` | Credit derivative (e.g. CDS). |
| `credit_insurance` | Credit insurance. |

### `interest_rate_type`

Interest rate type.

| Value | Meaning |
|---|---|
| `fixed` | Fixed until maturity. |
| `floating` | Floating (resets to a reference rate). |
| `mixed` | Fixed for an initial period, then floating, or other combinations. |

### `rating_source`

Origin of a rating.

| Value | Meaning |
|---|---|
| `internal` | Institution's internal rating system. |
| `sp` | S&P Global Ratings. |
| `moodys` | Moody's. |
| `fitch` | Fitch Ratings. |
| `dbrs` | DBRS Morningstar. |
| `other_ecai` | Other ECAI. |

### `credit_event_type`

Credit events relevant for default and loss history.

| Value | Meaning |
|---|---|
| `default` | Default per CRR Art. 178 (unlikely to pay and/or more than 90 days past due). |
| `forbearance` | Forbearance measure granted to an obligor in financial difficulty. |
| `bankruptcy` | Obligor has entered bankruptcy or similar protection. |
| `non_accrual` | Interest accrual stopped (non-accrued status). |
| `distressed_sale` | Sale of the exposure at a material credit-related economic loss. |
| `specific_credit_adjustment` | Specific credit risk adjustment booked due to credit deterioration. |
| `cure` | Return to non-defaulted status. |
| `write_off` | Full or partial write-off. |

### `recovery_flow_type`

Type of cash flow after default.

| Value | Meaning |
|---|---|
| `recovery` | Cash recovered (payments, collateral liquidation proceeds, guarantee calls). |
| `workout_cost` | Direct cost of the workout (legal, external collection, liquidation costs). |
| `write_off` | Amount written off (a non-cash loss event). |

### `parameter_source`

Origin of a risk parameter.

| Value | Meaning |
|---|---|
| `external` | Supplied by the institution's own models. |
| `derived` | Derived by `sora calibrate` from SIM history. |
| `benchmark` | Supervisory benchmark (e.g. ECB benchmark parameters). |
| `override` | Expert override (must be documented). |

### `scenario`

Scenario identifier for parameters and results.

| Value | Meaning |
|---|---|
| `actual` | Observed or starting-point values at the reference date. |
| `baseline` | Baseline scenario. |
| `adverse` | Adverse scenario. |

## Manifest (`sim_manifest.json`)

| Field | Type | Required | Description |
|---|---|---|---|
| `sim_version` | string | yes | SIM schema version the dataset conforms to. |
| `reference_date` | date | yes | Reference (reporting) date of all stock data. |
| `reporting_currency` | currency | yes | Currency of results. |
| `reporting_entity_id` | key | yes | Top entity of the consolidation scope (must exist in sim_entity). |
| `mapping_release` | string | no | Identifier of the mapping release that produced the dataset. |
| `source_fingerprint` | string | no | Fingerprint of the source export. |
| `created_at` | string | no | ISO 8601 timestamp of creation. |
