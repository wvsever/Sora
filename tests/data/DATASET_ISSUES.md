# Dataset issues (for the dataset generator)

Issues found in `tests/data/20260630.7z` (generator `cppbankrawaccgen`, `scale = 1.0`, `seed = 27`)
while building Sora. They are input for improving the generator. Sora works around each one in the
reference mapping (`mappings/cppbank/`), and the workaround is noted here.

Severity:
- **High**: blocks a Sora capability or makes results unrealistic.
- **Medium**: inconsistency the mapping must compensate for.
- **Low**: cosmetic or realism.

Status: `open` until the generator is fixed. When it is, remove the workaround and mark the issue `fixed (generator vX)`.

| ID | Severity | Area | Issue | Evidence | Sora workaround | Status |
|---|---|---|---|---|---|---|
| DS-001 | High | Risk parameters | No PD, LGD, EAD, CCF or lifetime PD per contract or segment (neither IFRS 9 nor IRB). | No such columns in `_csv_column_types.json`. | `sora calibrate` derives starting-point parameters from history. See `plans/09_risk_parameters.md`. | open |
| DS-002 | High | Default | No explicit default flag or default date. `contract_credit_event` has no `default` event type. | Event types: nonAccruedStatus, distressedForbearance, specificCreditAdjustment, obligorBankruptcyProtection, institutionFiledBankruptcy, saleAtMaterialCreditLoss. | `is_defaulted` = stage 3 or DPD > 90. Default date = first month-end in stage 3 (`sim_credit_event.sql`). | open |
| DS-003 | High | History length | Stage/allowance history covers only 8 month-ends (2025-09, 2025-12 … 2026-06), and 2025-10/11 are missing. Arrears history covers 25 months. EBA calibration needs several years, including a downturn. | `impairment_allowance.accounting_period` | Transitions are estimated from 6 consecutive months. Few observations for S1→S3. | open |
| DS-004 | High | Stage 2 allowances | 1,407 amortised-cost loans have no `impairment_allowance` row at 2026-06, and `contract_loan.impairment_allowance = 0`. About 750 of them are stage 2 (e.g. 200 RESI_MTG, 183 CONSUMER, 165 CREDIT_CARD). Stage 2 with zero lifetime ECL is unrealistic. | Anti-join of `contract_loan` and `impairment_allowance` (2026-06) | None. The loans carry allowance 0, which understates S2 coverage and biases LRLT S2 calibration. | open |
| DS-005 | Medium | Schema consistency | Files of the same table have different column sets across entity partitions (columns that are always empty for an entity are dropped). `contract_loan` has 21 header variants, `contract_security_position` 12, `contract_deposit` 9, `contract_lease` 5, `collateral` 4, `commitment` 3, `impairment_allowance` 3. Readers that assume one schema per table fail. | Per-file CSV headers | Read with `union_by_name = true` (done in `sora-tools map`). | open |
| DS-006 | Medium | Stage transitions | Monthly S3→S2 cure rate of 11.2% (≈ 75% annualised), and no S3→S1. Implausibly high cure from stage 3 for most portfolios. | Month-on-month stage transitions in `impairment_allowance` | Starting-point TR3-2 is taken as observed. The EBA projection applies no-cure regardless. | open |
| DS-007 | Medium | Commitments | `provision_balance` and `impairment_allowance` are identical in all rows (duplicate columns). | `contract_commitment` | `provision_balance` used. | open |
| DS-008 | Medium | Commitments | `gross_carrying_amount` differs from `drawn_amount` in 6,726 of 6,860 commitments with a drawn part (GCA > drawn in all of them). The semantics are unclear (accrued interest? fees?). | `contract_commitment` | GCA is used as the on-balance drawn part. | open |
| DS-009 | Medium | Commitments | 14,782 commitments, but only 13,664 have an `impairment_allowance` row at 2026-06. Total provisions differ slightly (403.99m vs 403.97m). | Join commitment ↔ allowance ledger | Contract-level provision is used. | open |
| DS-010 | Medium | Recoveries | Two inconsistent recovery sources. `contract_event` has `recovery` events for 4,108 contracts (EUR 133.7m), and `contract_recovery_cashflow` has recoveries for 2,882 contracts (EUR 2,085m). 1,226 written-off contracts have no recovery cash flows. | Both tables | `contract_recovery_cashflow` for recoveries/costs, `contract_event` for write-offs. `contract_event` recoveries are ignored. | open |
| DS-011 | Medium | Ratings | Only ~21% of loan obligors have a rating. There are no internal ratings and no PD master scale. External grades have no modifiers (`BBB`, not `BBB+`/`BBB-`). | `counterparty_rating` | Rating is a fallback PD source only. CQS is mapped on unmodified grades. | open |
| DS-012 | Low | Data quality | One loan row is almost empty: `CL-045510` (BANK-CPP-001-FNDG01, INTERBANK_TERM, GCA 266m) has NULL `instrument_kind`, `interest_rate_type`, `amortisation_type`, `is_revolving`, `watchlist_flag`, `secured_flag`. | `contract_loan` | Passed through with NULLs. | open |
| DS-013 | Low | Counterparty | 9 counterparties have `legal_form = natural_person` but NULL `is_natural_person`. | `counterparty` | `coalesce(is_natural_person, legal_form = 'natural_person')`. | open |
| DS-014 | Low | NACE | NACE codes only at division level (`A01`, `G47`). No group/class, and no NACE Rev. 2.1. CR_SECTOR needs section level plus the energy-intensive manufacturing split of C. | `counterparty.nace_code` | Section derived from the first letter. | open |
| DS-015 | Low | Default flag proxies | Loans have no days-past-due field. DPD must be derived from `contract_arrears` (oldest unpaid instalment). | `contract_loan`, `contract_arrears` | DPD derived in `sim_exposure.sql`. | open |
| DS-016 | High | Stage transitions | Month-on-month stage churn is far too high. Loans move S1→S2 at 2.29% and S2→S3 at 7.56% per month. Annualised, that is a 12-month PD of ≈4.7% from stage 1 and ≈36.5% from stage 2 (all-portfolio). A projection calibrated on it produces impairments of several times the starting allowance even in the baseline. | `sora_reference.py` calibration, level ALL\|ALL\|ALL | None. The golden results use it as is. They are consistent but not realistic. | open |
| DS-017 | Medium | History coverage | Debt securities and small sectors have too few stage-history observations for segment-level calibration (fall back to all-portfolio rates, dominated by retail loans). | `calibration_levels` column in `tests/golden/20260630/parameters.csv` | Hierarchical fallback. | open |
| DS-040 | Low | Leases | 30 lessor finance leases matured before the reference date (e.g. `LSE-000023`, maturity 2026-01-09) are still in the 2026-06-30 snapshot with a stage (28 stage 1, 2 stage 2) and a gross carrying amount, receivable and allowance of 0.00. Matured, fully repaid contracts should be derecognised, or at least carry no IFRS 9 stage. | `contract_lease`: `role = 'lessor'`, `is_finance_lease`, `gross_carrying_amount = 0`, `contractual_maturity_date` < 2026-06-30 | Mapped as is (no effect on the IFRS 9 projection). The calculator client does not send zero-EAD records (CALC-003). | open |

## Wishlist for the generator (not errors)

- Per-contract IFRS 9 parameters (PD 12m, lifetime PD, LGD, EAD/CCF) and IRB parameters (PDreg, LGDreg, ELBE) consistent with the allowances. This would allow testing the "customer-supplied parameters" path (DS-001).
- A longer monthly history (≥ 5 years) with a stress episode, for calibration tests (DS-003).
- Explicit default and cure events with dates (DS-002).
- A consistent column set per table across partitions, or a documented rule (DS-005).
- Optional Parquet output, with decimal types.
