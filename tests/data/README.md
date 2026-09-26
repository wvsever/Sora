# Test data

`20260630.7z` is a synthetic, fully enumerated banking-group dataset used as the reference input for Sora.

| Property | Value |
|---|---|
| Reporting date | 2026-06-30 |
| Reporting group | CPPBank NV (`BANK-CPP-001`), Belgium, IFRS, reporting currency EUR |
| Entities | 70 in `reference/entity.csv`, 67 with partitioned data |
| Currencies | CHF, CZK, DKK, EUR, GBP, HKD, HUF, JPY, NOK, PLN, RON, SEK, SGD, USD |
| Regulatory parameter set | `EU_CRR3_2025-01-01` |
| Generator settings | `scale = 1.0`, `seed = 27`, `--history-months 60` (cppbankrawaccgen `1589b14`; see `plant_input/control_manifest.json`) |
| Size | 46 MB compressed, 508 MB uncompressed, 3,379 CSV + 2 JSON files (GL tables not included, see below) |

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
| `contract_loan` | 46,601 | Gross carrying amount, drawn/undrawn, `declared_stage` (`stage1`/`stage2`/`stage3`/`poci`), `impairment_allowance`, product, portfolio, rates, repricing, maturity, forbearance, watchlist, credit-impaired flag |
| `contract_commitment` | 14,828 | Off-balance commitments and guarantees given, undrawn amount, stage, provision |
| `contract_security_position` | 1,801 | Debt and equity securities, measurement category, stage, issuer rating |
| `contract_lease` | 420 | Finance leases (lessor) and lease liabilities |
| `contract_derivative`, `contract_sft` | 851 / 1,387 | Counterparty credit risk and market risk |
| `counterparty` | 23,852 | Country, ESA 2010 sector, NACE code, natural person flag, financials (turnover, EBITDA, debt, equity) |
| `counterparty_rating` | 8,756 | External ratings (S&P, Moody's, Fitch, DBRS; long- and short-term scales). Grades carry modifiers (`rating_modifier`: `+`/`-`, Moody's `1`/`2`/`3`). |
| `counterparty_link` | 23,007 | Group structure and economic dependency |
| `collateral` / `collateral_allocation` | 23,572 / 22,933 | Collateral type, market or appraised value, valuation date, lien rank, property country. The allocation table links collateral to contracts. |
| `guarantee_received` | 3,763 | Guarantees, credit derivatives, credit insurance |

### Credit history (calibration inputs)

| Table | Rows | Key content |
|---|---:|---|
| `impairment_allowance` | 1,437,705 | Per contract and month, 60 consecutive month-ends (2021-07 to 2026-06), including a downturn episode (`reference/macro_cycle_index.csv`): opening/closing allowance, charge, release, write-off, stage at period end, collective or individual basis |
| `contract_arrears` / `contract_arrears_history` / `contract_arrears_position` | 34,718 / 354,985 / 11,186 | Due and paid amounts per due date; 61 monthly snapshots (2021-06 to 2026-06); oldest unpaid due date per contract in arrears |
| `contract_credit_event` / `contract_default_cure_event` | 4,317 / 64 | Dated unlikely-to-pay triggers (non-accrual, distressed forbearance, bankruptcy, specific credit adjustment, sale at material credit loss); probation start/end on cure |
| `contract_event` | 301,603 | Origination, repayment, write-off, recovery |
| `contract_recovery_cashflow` | 8,246 | Recovery and work-out cost cash flows on every written-off contract (consistent with the `recovery` events in `contract_event`) |
| `repossessed_collateral` | 4 | Foreclosed assets |

### Funding, rates and market data

| Table | Rows | Key content |
|---|---:|---|
| `contract_deposit` | 92,543 | Deposit type, DGS coverage, operational and transactional flags, rate, notice period |
| `contract_debt_issued` | 1,223 | Issued debt, MREL features |
| `contract_cashflow` | 1,136,739 | Contractual interest, principal and fee flows |
| `irrbb_*_history` | about 223k | Prepayment, early redemption, non-maturity deposit balance history |
| `reference/risk_free_curve`, `credit_spread_curve` | 140 / 1,680 | Curves at the reporting date |
| `reference/fx_rate` | 205,016 | FX history |

### Accounting

The ledger tables below are produced by the generator but **not included in the archive**: with 60 months of history
they would take the archive past GitHub's 100 MB file limit (140 MB), and Sora does not read them. Regenerate the book
with `tools/New-SoraDataset.ps1` (add `-IncludeGlTables` to `-Package`) when you need them.

| Table | Rows | Key content |
|---|---:|---|
| `gl_balance` | about 200k (est.) | Balance per entity, GL account and period |
| `journal_entry` / `journal_line` | about 1.3M / 3M (est.) | Posting-level ledger, 507 MB for `journal_line` |
| `reference/gl_account` | 216 | Chart of accounts with IFRS 9 category, measurement, statement mapping |

## Portfolio profile (`contract_loan`)

| Stage | Contracts | Gross carrying amount (EUR m, unconverted) | Allowance (EUR m) | Coverage |
|---|---:|---:|---:|---:|
| stage1 | 37,307 | 171,041 | 192 | 0.11% |
| stage2 | 4,857 | 6,530 | 493 | 7.55% |
| stage3 | 2,926 | 2,791 | 2,012 | 72.1% |
| poci | 1,197 | 379 | 230 | 60.7% |
| no stage (FVTPL) | 314 | 12,067 | – | – |

The largest product codes are central bank reserves, `CORP_LARGE`, `CRE`, `RESI_MTG`, `CORP_RCF`, specialised lending, `SME_TERM`, interbank, `CONSUMER` and `CREDIT_CARD`.

Observed average monthly stage transitions for amortised-cost loans (from `impairment_allowance`, 2021-07 to 2026-06):

| From \ To | stage1 | stage2 | stage3 |
|---|---:|---:|---:|
| stage1 | 99.78% | 0.22% | – |
| stage2 | 1.53% | 97.61% | 0.86% |
| stage3 | – | 0.09% | 99.91% |

## Known issues

Issues found in the dataset (for the generator) are tracked in [`DATASET_ISSUES.md`](DATASET_ISSUES.md).

## What the dataset does not contain

- **No PD, LGD, EAD, CCF or lifetime-PD per contract.** Neither the IRB parameters nor the IFRS 9 model outputs are present. See `plans/09_risk_parameters.md`.
- No expected stress-test results. See `plans/06_validation.md` ("Reference results").
- No stress scenario. EBA/ESRB scenarios are in `docs/` as xlsx files.

## Usage

The archive is not extracted by the build. Tests extract it into the build tree (skipping the GL tables unless `--all` is given):

```sh
python tools/extract_testdata.py            # uses 7z if installed, else py7zr
```

## How the archive is regenerated

`20260630.7z` is produced by `cppbankrawaccgen` (a separate repository) run at `--scale 1.0 --seed 27` with
`--format csv`, `--with-group-entities`, `--with-group-portfolios`, `--check-identities` and
`--validate-output`, then staged into this layout and compressed. `tools/New-SoraDataset.ps1` runs that
generation step (plus the Vera risk-parameter derivation described in `plans/09_risk_parameters.md`) and can
build a fresh archive with `-Package`:

```powershell
pwsh tools/New-SoraDataset.ps1 -GeneratorRepo <cppbankrawaccgen checkout> -VeraRepo <baselcalculator checkout> `
  -OutRoot <out> -Seed 27 -Scale 1.0 -ReportingDate 2026-06-30 -SkipVera -Package
```

`-Package` stages the generated book with `robocopy /XD`, excluding `wire/` (a directory Vera reads that
this dataset has no use for) and, by default, the GL tables (`journal_line`, `journal_entry`, `gl_balance` -
`tools/extract_testdata.py`'s own default, about 70% of the volume; pass `-IncludeGlTables` to keep them),
then archives the staged tree with `7z`. Every run also writes `pipeline_manifest.json` next to the archive:
the generator's git SHA and exact command line, seed, scale and timings, so a regenerated archive's
provenance never has to be reconstructed from memory. Regenerating the archive is a full re-run at scale
1.0 (tens of minutes, see the generator's own docs) - it is not a quick edit, and `-SkipVera` avoids running
`bcal_cli` when only the book (not the risk parameters) needs regenerating.

The reference mapping `mappings/cppbank/` turns the export into a SIM dataset (see `python/README.md`).
