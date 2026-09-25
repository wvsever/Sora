# Test data

`20260630.7z` is a synthetic, fully enumerated banking-group dataset used as the reference input for Sora.

| Property | Value |
|---|---|
| Reporting date | 2026-06-30 |
| Reporting group | CPPBank NV (`BANK-CPP-001`), Belgium, IFRS, reporting currency EUR |
| Entities | 70 in `reference/entity.csv`, 67 with partitioned data |
| Currencies | CHF, CZK, DKK, EUR, GBP, HKD, HUF, JPY, NOK, PLN, RON, SEK, SGD, USD |
| Regulatory parameter set | `EU_CRR3_2025-01-01` |
| Generator settings | `scale = 1.0`, `seed = 27` (see `plant_input/control_manifest.json`) |
| Size | 77 MB compressed, 1.06 GB uncompressed, 5,651 CSV + 2 JSON files |

All values are synthetic (`rate_source = cppbankrawaccgen-synthetic-modelled-not-observed`). No real institution or customer data is included.

## Layout

```text
_csv_column_types.json                 column name -> str | int | bool, per table (54 tables, 1,051 columns)
plant_input/control_manifest.json      generator settings and active producers
reference/<table>.csv                  unpartitioned reference data
accounting/<table>/entity_id=<id>/part-NNNN.csv
accounting/<table>/entity_id=<id>/period=YYYY-MM/part-NNNN.csv   (gl_balance, journal_*, impairment_allowance)
```

Partitioning is Hive-style. The `entity_id` value is also repeated inside each row.

## CSV conventions

- Header row. The column order is alphabetical, not semantic.
- Empty field = null. There is no quoting of null values.
- Booleans are `true` / `false`.
- Dates are `YYYY-MM-DD`. Periods are `YYYY-MM`.
- Monetary amounts are decimal strings with at most 2 decimals (largest observed value about 5.5e10). They map exactly to `int64` cents.
- Rates and ratios are decimal strings with at most 9 decimals. They map exactly to `int64` scaled by 1e9.
- `row_seq` is a per-file sequence number and is used as a deterministic tie-breaker.
- Business keys (`contract_id`, `counterparty_id`, `collateral_id`, ...) are strings such as `CL-000001`. `contract_id` is unique across the whole dataset, not only within an entity.

## Tables relevant to stress testing

Row counts are for the full dataset.

### Credit exposures

| Table | Rows | Key content |
|---|---:|---|
| `contract_loan` | 45,611 | Gross carrying amount, drawn/undrawn, `declared_stage` (`stage1`/`stage2`/`stage3`/`poci`), `impairment_allowance`, product, portfolio, rates, repricing, maturity, forbearance, watchlist, credit-impaired flag |
| `contract_commitment` | 14,782 | Off-balance commitments and guarantees given, undrawn amount, stage, provision |
| `contract_security_position` | 1,801 | Debt and equity securities, measurement category, stage, issuer rating |
| `contract_lease` | 420 | Finance leases (lessor) and lease liabilities |
| `contract_derivative`, `contract_sft` | 851 / 1,312 | Counterparty credit risk and market risk |
| `counterparty` | 23,852 | Country, ESA 2010 sector, NACE code, natural person flag, financials (turnover, EBITDA, debt, equity) |
| `counterparty_rating` | 8,756 | External ratings (S&P, Moody's, Fitch, DBRS; long- and short-term scales). Only about 21% of loans have a rated counterparty. |
| `counterparty_link` | 23,007 | Group structure and economic dependency |
| `collateral` / `collateral_allocation` | 23,263 / 22,652 | Collateral type, market or appraised value, valuation date, lien rank, property country. The allocation table links collateral to contracts. |
| `guarantee_received` | 3,763 | Guarantees, credit derivatives, credit insurance |

### Credit history (calibration inputs)

| Table | Rows | Key content |
|---|---:|---|
| `impairment_allowance` | 399,083 | Per contract and month (2025-09, 2025-12 to 2026-06): opening/closing allowance, charge, release, write-off, stage at period end, collective or individual basis |
| `contract_arrears` / `contract_arrears_history` | 34,861 / 280,205 | Due and paid amounts per due date; 25 monthly snapshots (2024-06 to 2026-06) |
| `contract_credit_event` | 4,358 | Non-accrual, distressed forbearance, bankruptcy, specific credit adjustment, sale at material credit loss |
| `contract_event` | 130,338 | Origination, repayment, write-off, recovery |
| `contract_recovery_cashflow` | 5,764 | Recovery and work-out cost cash flows on defaulted contracts |
| `repossessed_collateral` | 4 | Foreclosed assets |

### Funding, rates and market data

| Table | Rows | Key content |
|---|---:|---|
| `contract_deposit` | 80,037 | Deposit type, DGS coverage, operational and transactional flags, rate, notice period |
| `contract_debt_issued` | 1,223 | Issued debt, MREL features |
| `contract_cashflow` | 1,136,739 | Contractual interest, principal and fee flows |
| `irrbb_*_history` | about 223k | Prepayment, early redemption, non-maturity deposit balance history |
| `reference/risk_free_curve`, `credit_spread_curve` | 140 / 1,680 | Curves at the reporting date |
| `reference/fx_rate` | 205,016 | FX history |

### Accounting

| Table | Rows | Key content |
|---|---:|---|
| `gl_balance` | about 200k (est.) | Balance per entity, GL account and period |
| `journal_entry` / `journal_line` | about 1.3M / 3M (est.) | Posting-level ledger, 507 MB for `journal_line` |
| `reference/gl_account` | 216 | Chart of accounts with IFRS 9 category, measurement, statement mapping |

## Portfolio profile (`contract_loan`)

| Stage | Contracts | Gross carrying amount (EUR m, unconverted) | Allowance (EUR m) | Coverage |
|---|---:|---:|---:|---:|
| stage1 | 36,656 | 180,641 | 166 | 0.09% |
| stage2 | 4,827 | 5,036 | 282 | 5.60% |
| stage3 | 2,924 | 7,283 | 1,210 | 16.6% |
| poci | 1,204 | 2,133 | 203 | 9.50% |

The largest product codes are central bank reserves, `CORP_LARGE`, `CRE`, `RESI_MTG`, `CORP_RCF`, specialised lending, `SME_TERM`, interbank, `CONSUMER` and `CREDIT_CARD`.

Observed average monthly stage transitions for loans (from `impairment_allowance`, 2025-12 to 2026-06):

| From \ To | stage1 | stage2 | stage3 |
|---|---:|---:|---:|
| stage1 | 97.69% | 2.29% | 0.02% |
| stage2 | 9.73% | 82.70% | 7.56% |
| stage3 | – | 11.23% | 88.77% |

## What the dataset does not contain

- **No PD, LGD, EAD, CCF or lifetime-PD per contract.** Neither the IRB parameters nor the IFRS 9 model outputs are present. See `plans/09_risk_parameters.md`.
- No expected stress-test results. See `plans/06_validation.md` ("Reference results").
- No stress scenario. EBA/ESRB scenarios are in `docs/` as xlsx files.

## Usage

The archive is not extracted by the build. Tests extract it into the build tree:

```sh
7z x tests/data/20260630.7z -obuild/testdata/20260630
```
