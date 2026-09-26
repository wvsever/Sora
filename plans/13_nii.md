# Net Interest Income (NII) Plan

## Objective

Project net interest income over the 3-year horizon, baseline and adverse, under the static balance sheet, following
the EBA EU-wide stress test NII methodology (MN 2025 final, section 4, paragraphs 346-430; 2027 draft, section 4,
paragraphs 350-434) as far as the SIM data supports it. This is the first slice of phase 6 (`08_delivery_roadmap.md`):
a deterministic, position-level NII projection with the EBA margin paths and pass-through rules. Deposit outflows,
funding spread shocks beyond the MN margin paths, and the full NII templates are deferred (see the end).

Enabled by the scenario key `nii` (opt-in; credit results are unaffected, `sora run` without the key writes exactly
what it wrote before):

```yaml
nii:
  own_rating: A              # S&P rating of the bank: Box 23 idiosyncratic shock under the adverse scenario (50 bps)
  new_business_months: 12    # margin of new business: positions originated in the last 12 months (MN para 353)
```

Paragraph numbers below are MN 2025 unless marked "2027".

## Inputs

| Input | Content |
|---|---|
| `sim_exposure` | Assets: on-balance loans, debt securities and finance leases of all accounting categories except held for trading (para 348), with `current_interest_rate`, `interest_rate_type`, `origination_date`, `maturity_date`, `next_repricing_date` and the new column `repricing_frequency_months`. Intragroup exposures are excluded (para 403). |
| `sim_deposit` (new) | Deposits received: type (sight: `current`, `savings`, `call`; `notice`; `term`), amount, rate and repricing attributes, maturity and notice period, DGS and operational flags (the latter for the funding module). |
| `sim_debt_issued` (new) | Debt securities issued: instrument type, carrying and nominal amount net of own holdings, coupon and repricing attributes, maturity, first call date. Additional Tier 1 is excluded (para 398). |
| `sim_rate_curve` (new, optional) | Risk-free curve per currency (the bank's reference-rate curve at the reference date, para 351) and credit spread curves (sector, rating band, seniority; kept for the funding spread module). |
| `sim_counterparty`, `sim_entity` | Counterparty sector and residence (assets); the booking entity's country = location of the activity (liabilities, para 401). |
| Macro scenario | `swap_rate` curves by currency and tenor (1M-30Y, levels in %, `starting_point` = end of the year before the horizon) and `long_term_rate` by country (10-year sovereign yields), for baseline and adverse. |
| Credit projection | NPE (S3 + POCI) provision stock per year under the adverse scenario, for the Box 22 cap. |

## Method

All amounts are converted to the reporting currency at the reference-date FX rate and held there (static balance
sheet, no FX effect: para 376 deferred). Projection year *y* runs from the reference date plus *y-1* years to the
reference date plus *y* years; interest accrues on actual days within the year (`days / days in the year`).

1. **Static balance sheet (paras 368-369).** Every position keeps its volume in every year and scenario. A position
   that matures is replaced by the same instrument (same row, currency, rate type and original term); a replacement
   that matures again within the horizon is replaced again. No prepayment or other behavioural assumptions. A position
   past its maturity at the reference date is replaced on the first day.
2. **Volume (para 355).** Gross carrying amount (assets), deposit amount, carrying amount of debt issued (net of own
   holdings). Non-performing assets (stage 3 and POCI) earn interest on the exposure net of provisions (para 407).
3. **Rate type (paras 358, 379).** `fixed` keeps its rate until maturity. `floating` resets the reference-rate
   component at `next_repricing_date` and then every `repricing_frequency_months` (when the date is missing: rolled
   forward from origination), and keeps its margin until maturity. `mixed` is floating if the fixed period ends
   (`next_repricing_date`) within the horizon, else fixed. A floating position without a frequency resets monthly
   (diagnostic NII-002).
4. **Split of the EIR at the starting point (paras 351-352, 380-381).** Reference rate = the risk-free rate of the
   currency at the position's tenor on the bank's curve at the reference date (`sim_rate_curve`, linear interpolation,
   flat beyond the curve, para 374); margin = EIR - reference rate. Tenor: the original term for fixed positions
   (maturity - origination), the index tenor (reset frequency) for floating positions, 1 month for sight deposits
   (para 392) and open-ended fixed positions. Without a bank curve for a currency the scenario's starting-point swap
   curve is used.
5. **Reference rate path (paras 374-375, 408).** The reference rate of a position that reprices in year *y* is its
   starting-point reference rate plus the change of the scenario swap rate at its tenor from the scenario's starting
   point to year *y*: `rf0(T) + swap_y(T) - swap_start(T)` (currency without a scenario curve: `RoW`, para 375). MN
   para 408 prices new instruments at the scenario swap level; footnote 60 books the difference between the bank's
   starting point and the scenario's in the margin of new business. The two are the same total EIR; the shift keeps
   the bank's own reference-rate level (the reference date differs from the scenario's starting point) and makes a
   flat scenario reproduce the starting-point EIR exactly.
6. **Margin of new business (paras 353, 409).** Per cell (CSV_NII_CALC row, currency, rate type), the
   volume-weighted margin of the performing positions originated in the `new_business_months` before the reference
   date. A cell without new business takes the volume-weighted margin of its whole performing stock (para 409 asks
   for a hypothetical margin from comparable portfolios; diagnostic NII-004).
7. **Margin paths (Boxes 23-24, paras 419-427).** A replaced position takes the new business margin of its cell plus
   - assets: `lambda x max(delta sovereign spread, 0)`, lambda by row (0 central banks, 1 general governments, 0.5 credit
     institutions and other financial corporations and other assets, 0.15 NFCs and households);
   - liabilities: `gamma x max(delta sovereign spread, delta idiosyncratic)`, gamma by row (0 central bank deposits,
     1 CI/OFC deposits and other debt securities issued, 0.2 GG/NFC sight deposits and certificates of deposit, 0.1
     household sight deposits, 0.5 GG/NFC/HH term deposits, 0.75 covered bonds and ABS). The idiosyncratic component
     is 0 in the baseline and the Box 23 rating table value (e.g. A: 50 bps) in the adverse scenario, from the start.
   - Sovereign spread (para 362, Box 23) = long-term rate of the country - 10Y swap rate of the currency; delta against
     the scenario's starting point. Country: counterparty residence for assets, the booking entity's country for
     liabilities (para 400); countries without a long-term rate use the scenario's `country_fallback`.
   Margins of existing positions do not move before maturity (paras 418-419).
8. **Sight deposits (paras 363, 369, 392-397; 2027 para 397).** They reprice immediately: in every year the
   reference rate is `rf0(1M) + beta x delta swap(1M)` with pass-through beta 0.5 (households), 0.75 (NFCs), 1 (all
   others, para 397/2027 para 401), and the margin is the position's own margin plus the Box 23 path (gamma 0.1 / 0.2 /
   1). Household sight deposits are floored so that the EIR is not negative (2027 draft para 397: via the reference
   rate); legal floors and regulated deposits are not in the SIM (deferred). NFC and other sight deposits have no zero
   floor (MN: legal floor only), so a large rate cut can make their EIR negative.
9. **Non-performing exposures (paras 371-373, 378, 406-407, 413).** Decision: stage 3 and POCI assets form a static
   NPE stock earning their starting-point EIR on the exposure net of provisions, in both scenarios, not split into
   reference rate and margin. This meets the adverse cap on the NPE EIR (para 407: at most the end-of-starting-year
   average) trivially. The NPE flows of the credit projection (migration of performing volume to NPE, para 372-373,
   413) are not moved into the NII volumes yet (deferred); the Box 22 cap carries their effect at group level.
10. **Box 22 cap (paras 404-405).** Adverse group NII per year is capped at
    `NII_t0 - NII_t0 x (Prov_t - Prov_t0) / (Vol_PE + Vol_NPE - Prov_NPE)` and at `NII_t0` (para 404), where the
    provision increase is the S3 + POCI provision stock of the credit projection (on-balance scope of the credit module)
    against its starting point, and the volumes are the NII assets at the starting point. `NII_t0` is the annualised
    NII of the reference-date balance sheet (year 0 in `nii.csv`), not the accounting NII of the last year (not in the
    SIM). Both the NII and the capped NII are reported; `nii.csv` rows are uncapped.

## Outputs

`nii.csv`: one row per scenario (`actual` year 0 = annualised starting point, `baseline`/`adverse` years 1-3), CSV_NII_CALC
row (`template_row`, RowNum of the 2025 templates' fixed-rate block: 1-11 assets, 22-34 liabilities; label in
`nii_type`), currency, rate type (`fixed`/`floating`) and status (`performing`/`non_performing`): position count,
volume, NPE provisions, interest, its reference-rate and margin parts (performing only), EIR (interest over volume,
net volume for NPE), and for the starting-point performing rows the new business margin of the cell. Amounts in the
reporting currency; interest positive for income (assets) and expense (liabilities).

`summary.json` `nii`: settings, position counts, fallback counts, the starting point (interest income, expense, NII,
performing and NPE volume and provisions, NPE provisions of the credit scope), and per scenario and year interest income,
expense and NII, plus under the adverse scenario the NPE provision increase, the Box 22 cap and the capped NII.

Diagnostics: NII-000 positions, NII-001 missing rates (0 assumed), NII-002 floating without frequency, NII-003 no positive
original term (replaced yearly), NII-004 cells without new business, NII-005 no `own_rating`.

## Determinism and reference

`src/nii.cpp` projects positions in parallel into per-position slots and aggregates serially in position order
(assets by `exposure_id`, deposits by `deposit_id`, debt by `debt_id`): results are bit-identical for any `--workers`.
`tools/reference/sora_reference.py` (section "NII") implements the same method independently, with the same
floating-point operations in the same order; the engine's `nii.csv` is byte-identical to the golden file.

## Results on the reference data (synthetic)

Scope: 47,423 assets (4,125 non-performing), 85,692 deposits (30,754 sight), 990 debt securities issued; performing
assets EUR 30.70bn at 3.61%, liabilities EUR 28.03bn at 3.14%. Starting-point NII EUR 236.9m (income 1,115.9m, expense
878.9m). The CPPBank book is funded mostly by rate-sensitive term deposits (EUR 16.2bn households/NFC term, 3.1bn
interbank term) and short-term paper, and half of its assets are fixed-rate, so NII falls when rates rise:

| EUR m | Year 1 | Year 2 | Year 3 |
|---|---:|---:|---:|
| Baseline NII (income / expense) | 228.9 (1,046.8 / 817.9) | 224.7 (1,008.4 / 783.7) | 226.5 (1,021.6 / 795.2) |
| Adverse NII (income / expense) | 149.8 (1,248.1 / 1,098.3) | 110.6 (1,213.4 / 1,102.7) | 127.1 (1,201.2 / 1,074.1) |
| Adverse Box 22 cap (NPE provision increase) | 236.7 (29.2) | 236.3 (80.0) | 235.9 (136.9) |

Baseline: short EUR rates fall (1M swap -0.8pp against the 2024 starting point), so floating assets and liabilities both
reprice down; NII -3% to -5%. Adverse: rates rise (EUR 1M +0.9pp in year 1, 5Y +1.1pp) and funding margins widen with
the idiosyncratic 50 bps (gamma 0.5-1 on term deposits and bonds) and the sovereign spread; liabilities reprice faster
(term deposits of 3-12 months) than the fixed-rate assets: NII -37% / -53% / -46%. The Box 22 cap does not bind.
The results are synthetic (the curves of the export are steep and high, DS-048), not a stress-test outcome.

## Decisions and deviations from the MN

| Topic | MN | Sora (first slice) |
|---|---|---|
| Granularity | Portfolio level with prescribed average points of maturing (APM, para 361) and intertemporal consistency formulas (paras 410-412, Annex VIII) | Position level with exact repricing and maturity dates; the MN formulas are the portfolio-level summary of the same assumptions (para 412: "the EIR of an instrument shall not change unless it reprices") |
| Starting-point reference rate | Risk-free rate at the last repricing date (para 380) | Bank curve at the reference date (no curve history in the SIM); affects only the reference/margin split of existing positions, not their total EIR |
| New reference rate | Scenario swap level (para 408) with the starting-point difference in the margin (fn 60) | Bank curve plus the scenario change: same total EIR, see method 5 |
| Starting point year | Accounting NII of the last year, average volumes (paras 354-357) | Annualised run-rate at the reference date |
| NPE | Growth from CSV_CR_SCEN flows, EIR migration (paras 372-373, 413) | Static NPE stock at t0, net of provisions; Box 22 cap uses the credit projection's provision increase |
| Loans' repayments | Each repayment is a maturing product (para 369) | Whole position at contractual maturity (amortisation schedules in `contract_cashflow` not in the SIM yet) |
| Callable liabilities | Counterparty calls at the first date (para 369) | Contractual maturity (the export's calls are issuer calls) |
| Legal floors, regulated sight deposits, funding matches, derivatives, HFT | Paras 390, 393-396, 424-426, 382-390, 428-430 | Not in the SIM: deferred |

## Deferred

- NPE migration from the credit projection into NII volumes (paras 372-373, 413); drawn parts of commitments.
- Amortisation schedules from `contract_cashflow` (repayments as maturing products, para 369) and a `sim_cashflow` table.
- Deposit outflows by deposit type, DGS coverage and operational flags (funding module; the SIM columns are in place).
- Funding spread shocks beyond the MN margin paths (using the `sim_rate_curve` credit spread curves).
- Legal floors and regulated sight deposits (CSV_NII_SUM), funding matches (CSV_NII_CALC_FUNDING_MATCH), interest rate
  derivatives and hedge accounting, embedded caps and floors, held-for-trading NII (para 430), FX effect (para 376),
  central bank funding spread (para 420).
- The EBA template layouts CSV_NII_CALC (country/currency blocks with the Box 21 materiality rule, existing / maturing /
  new volumes and EIRs per APM) and CSV_NII_SUM; `nii.csv` already carries the CSV_NII_CALC row numbers.
- Streaming: the module holds all positions in memory (about 200 bytes each plus 168 bytes of results; the reference
  data adds about 40 MB to the peak RSS). A two-pass streamed version (new business margins first) is needed before
  the 100x benchmark datasets include funding tables.
- The 2025 vs 2027 differences in the sight deposit floor (2025: floor on the reference rate at 0 for households;
  2027: EIR not below 0, applied here) as a scenario option.
