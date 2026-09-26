# Scenario Engine Plan

## Objective

Translate scenario definitions into fast immutable runtime rules. The primary target is the EBA EU-wide stress test methodology (2025 final, 2027 draft; see `docs/`).

## Scenario inputs

| Input | Source in `docs/` | Content |
|---|---|---|
| Macro-financial scenario | ESRB macro scenario xlsx (2025; 2027 when published) | Annual paths per country, baseline and adverse: GDP, unemployment, HICP, residential and commercial property prices, long-term rates, FX, equity prices |
| Sector GVA | "Real GVA by sector" xlsx | Annual GVA per country × NACE sector |
| Market risk scenario | ECB market risk scenario xlsx | Instantaneous shocks to rates, spreads, FX, equity, commodities |
| Corrections | ESRB corrigendum xlsx / letter | Patches to the published scenario |
| Satellite models | Customer (not in repo; synthetic for tests) | Macro → PD/TR, LGD/LR per segment |
| ECB benchmark parameters | ECB → customer, confidential (not in repo; synthetic for tests) | Stressed PD/TR, LGD/LR per portfolio, country, scenario and year |
| Starting-point parameters | `09_risk_parameters.md` | PD, TR, LGD, LR at the reference date |

The xlsx files are not read by the engine. A converter (`tools/scenario_import`) turns them into a normalised long CSV. This keeps spreadsheet parsing out of the core:

```text
scenario,variable,country,sector,year,value,unit
adverse,real_gdp_growth,BE,,2027,-2.1,pct
adverse,residential_property_prices,BE,,2027,-9.4,pct
adverse,real_gva,DE,C,2028,-3.0,pct
```

The corrigendum is applied during conversion. The converted file records the source file names and checksums.

## Horizon and time steps

- Reference date `t0`, and three annual steps (EBA 2027: t0 = 31 Dec 2026, projection 2027–2029).
- The test data has t0 = 30 Jun 2026. The engine must not hard-code a calendar year. Years are relative steps `t0+1 … t0+3`.
- Beyond the horizon (for lifetime loss rates), macro variables are held flat, except GDP, which keeps the last baseline growth rate. S1/S2 parameters revert linearly to the baseline over 6 years in the adverse (MN para 124).
- Sub-annual steps (monthly, quarterly) are supported by the time-step curve rule for non-EBA use.

## Scenario definition (human-authored)

```yaml
name: eba_2027_adverse
scenario_id: eba2027_adv_v1
reference_date: 2026-06-30          # t0 of the input dataset
steps: 3                            # annual
macro_path: scenarios/eba2027_macro.csv
scenario: adverse                   # baseline | adverse
starting_parameters: params/risk_parameters_20260630.csv
satellite_models: models/satellites.csv
benchmark_parameters: params/ecb_benchmarks.csv      # optional
constraints:
  no_cure_from_s3: true
  no_s3_provision_release: true
  static_balance_sheet: true
  pd_floor: 0.00001
```

Simple sensitivity shocks (for example a flat PD multiplier, a property price haircut or a parallel rate shift) are still supported as overlays on top of, or instead of, the macro path:

```yaml
overlays:
  - rule: pd_multiplier
    where: { sector: NFC, country: DE }
    value: 1.7
  - rule: collateral_haircut
    where: { collateral_form: immovable_property_commercial }
    value: -0.25
```

## Rule classes

Initial rule types:

1. Scalar multiplier
2. Scalar additive shock
3. Bounded shock (floor/cap)
4. Bucket lookup (segment × year table)
5. Transition matrix (stage S1/S2/S3; optionally rating grade)
6. Threshold event
7. Piecewise function
8. Time-step curve
9. Satellite model: `logit(PD_t) = logit(PD_t0) + Σ β_i · Δmacro_i,t` per segment (linear on LGD/LR). Coefficients come from data.

## Compilation step

Human-readable scenario definitions are compiled before execution into:

- Integer keys (segment, country, sector, year)
- A dense parameter table `[scenario][year][segment] → {PD12M_S1, PD12M_S2, TR1-2, TR2-1, LGD_S1, LGD_S2, LRLT_S2, LGD_S3}`
- Prevalidated transition matrices (rows sum to 1; S3 absorbing when no-cure is on)
- Collateral value indices `[year][country][collateral_form]`
- Ordered execution stages

The record-processing loop does no string comparisons and no scenario parsing. For EBA use the compiled table is small (< 1,000 segments × 4 years × 8 values), so it fits in L2 cache.

## Projection mechanics (EBA credit risk)

The EBA method is an expected-value flow model, not a simulation. Per segment and year (MN Boxes 3–9):

```text
S1→S2 flow(t+1) = Exp S1(t) · TR1-2(t+1)
S2→S1 flow(t+1) = Exp S2(t) · TR2-1(t+1)
S1→S3 flow(t+1) = Exp S1(t) · PD12M_S1(t+1)
S2→S3 flow(t+1) = Exp S2(t) · PD12M_S2(t+1)

Prov S1-S1(t+1) = Exp S1(t) · [1 − TR1-2(t+1) − PD12M_S1(t+1)] · PD12M_S1(t+2) · LGD_S1(t+2)
Prov S2-S1(t+1) = S2→S1 flow(t+1) · PD12M_S1(t+2) · LGD_S1(t+2)
Prov S1-S2(t+1) = S1→S2 flow(t+1) · LRLT_S2(t+1)
Prov S2-S2(t+1) = Exp S2(t) · [1 − TR2-1(t+1) − PD12M_S2(t+1)] · LRLT_S2(t+1)
Prov S1-S3(t+1) = Exp S1(t) · PD12M_S1(t+1) · LGD_S1(t+1)          (cumulative)
Prov S2-S3(t+1) = Exp S2(t) · PD12M_S2(t+1) · LGD_S2(t+1)          (cumulative)
Prov Old S3(t+1) = Σ over S3 exposures of max(Exp(t0) · LGD_S3(t0+1), Prov(t0))   (per exposure, MN para 141)
```

In the adverse final year, the `t+2` parameters are blended 5/6 adverse plus 1/6 baseline (Boxes 4–5).

Sora applies these at contract level, carrying fractional stage masses per contract (`w1 + w2 + w3 = 1`) so that results can be attributed to contracts. It then aggregates to the segment level. The segment-level result must equal the EBA formula applied to segment totals exactly. This equality is a validation check. Contract-level computation lets contract-specific overrides (PD, LGD, collateral) flow through the same code.

Static balance sheet: exposures maturing within the horizon are replaced with like-for-like exposures (same segment, stage mix and maturity). Total exposure per segment stays constant. POCI exposure is static, and only its provisions are projected.

## Collateral repricing and LTV

Implemented in `src/collateral.cpp` (engine) and `tools/reference/sora_reference.py` (reference). Output `collateral.csv`, and the LTV columns of `cr_scen.csv`.

1. **Inputs.** `sim_collateral` (collateral_id, collateral_type, currency, market_value, property_country) and `sim_collateral_allocation` (exposure_id, collateral_id, allocated_amount). Only allocations to in-scope exposures count (same scope and segmentation as the projection). Amounts are converted to the reporting currency at the reference-date FX rate of the collateral currency. If `allocated_amount` is NULL, the collateral's market value is allocated pro rata to the gross carrying amount of all in-scope exposures the collateral is allocated to (equal shares if that total is 0). In a mix of NULL and non-NULL rows for one item, each NULL row still gets its pro-rata share of the whole market value.
2. **Value index** per scenario and projection year `t = 1..3` (year 0 = 1):

   ```text
   residential_property: I(t) = Π_{k≤t} (1 + residential_property_prices growth(k) / 100)
   commercial_property:  I(t) = Π_{k≤t} (1 + commercial_property_prices growth(k) / 100)
   all other types:      I(t) = 1
   ```

   Growth rates are taken for the property country at the macro years of `year_map`. A property country without scenario data falls back to `country_fallback` (WR, then EU), as for the macro key. A missing year counts as 0 growth. In the reference data, property in IS, LI and SG uses WR.
3. **LTV (static balance sheet).** An exposure is *secured* if it has at least one in-scope real-estate (residential or commercial property) allocation. Each secured exposure keeps its t0 stage and t0 gross carrying amount; only collateral values move. The flow model's fractional stage masses are not used here. Per segment, scenario (actual year 0, baseline 1..3, adverse 1..3) and stage S1/S2/S3:

   ```text
   secured_exp_sN     = Σ t0 GCA of secured exposures in stage N
   re_collateral_sN   = Σ allocated real-estate value × I(t) of those exposures
   ltv_sN             = secured_exp_sN / re_collateral_sN      (blank if the denominator is 0)
   ```

   POCI and not-applicable exposures have no LTV column. Money has 2 decimals, LTV 9 decimals.
4. **CR_SCEN.** `LTV ratio - Stage 1/2/3 (%)` = 100 × the same ratio, with numerators and denominators summed over the segments of the row and geography first.
5. **LGD is not collateral-driven.** The LGD model stays the segment-level satellite multiplier on cumulative property prices. Contract-level collateral-driven LGD (e.g. `max(0, EAD − haircut × I(t) × collateral) / EAD` blended with an unsecured LGD) needs realised LGD data by collateral type, which the reference dataset lacks. That is future work.

The reference dataset's allocated amounts are close to the exposure amounts, not to the collateral value, so LTVs are above 100% (DS-018 in `tests/data/DATASET_ISSUES.md`).

## CR_SECTOR (NACE)

Implemented in `src/cr_sector.cpp` (engine, `cr_sector.csv`) and `tools/reference/sora_reference.py` (reference, golden
`tests/golden/20260630/cr_sector.csv`). Layout: EBA 2027 draft `CSV_CR_SECTOR` (the draft templates that `cr_scen.csv`
follows), MN 2027 draft section 2.3.8.

1. **Scope.** The non-financial corporations portfolio as in CR_SCEN: the NFC segments (loans and advances
   `NFC_SME_CRE`, `NFC_SME_OTHER`, `NFC_LARGE_CRE`, `NFC_LARGE_OTHER`, and debt securities `NFC`), i.e. CR_SCEN rows 6 and
   13. On-balance, amortised cost (the projection scope). Other financial corporations are not in scope, even though
   NACE has a section for them (K/L): the 2027 draft limits the template to NFC exposures.
2. **Sector.** From `sim_counterparty.nace_code` (principal activity of the counterparty), mapped to NACE Rev. 2.1
   sections A–T **by division number** (the two digits after the letter). Division numbers mean the same in Rev. 2 and
   Rev. 2.1; only the section letters moved (real estate L68 → M68, computer programming J62 → K62, …), so codes of
   either revision map correctly. Manufacturing is split into *energy-intensive* (divisions C10–C12, C17–C30, 2027 draft
   template guidance Table 4) and *other*. A bare section letter is read as a Rev. 2.1 section (a bare `C` counts as
   *other*: the split needs the division). Divisions 97–99 (households as employers, extraterritorial bodies), missing
   and malformed codes are *unknown*: included in the TOTAL row only (the template has no unknown row; the guidance asks
   for them in the explanatory note), so TOTAL still reconciles with CR_SCEN. The reference dataset has none.
3. **Projection: sector carried through, not allocated.** The projection is per exposure with its segment's
   parameters, so the sector is simply a second aggregation key: each NFC exposure is projected once into a zeroed
   buffer that is added to its segment and to its (segment, sector) slice (`Projection::sectors`). Adding a value to
   0 is exact, so the segment results stay bit-identical to the projection without the breakdown, and the slices are
   bit-identical for any `--workers N` (each segment is still processed by one worker, in exposure order). This equals
   the MN para 114 loss-distribution option (ii), allocation by sectoral exposure, done per stage and per year: Boxes
   3–8 are linear in the stage stocks, so a sector gets the segment's flows and provisions pro rata to its t0 exposure
   per stage; Box 9 (old S3 floor) stays per exposure. It is exact for exposure-level customer parameters too, and
   sectors sum to the segment. There are no sector-specific (GVA-driven) risk parameters yet: columns 1–2
   ("percentage of exposures with projections based on sectoral models") are 0. Sectoral satellites would plug into
   the same slices by giving each (segment, sector) its own parameter path.
4. **Rows.** Per slot (Actual t0; Baseline, Adverse years 1–3), geography (Total, the CR_SCEN top countries, Other) and
   23 sector rows: A, B, C (Pivot = energy-intensive + other), the two C o/w rows, D–T, TOTAL exposures to NFC (Sum).
   All country–sector combinations are written; the MN para 98 materiality threshold (0.5% of NFC exposure) is a
   reporting filter left to the submission step.
5. **Columns.** The template's 46 value columns, with the CR_SCEN definitions: exposure-weighted parameters (S1
   exposure at the start of the year for PD12M S1, TR1-2, LGD S1; S2 for PD12M S2, TR2-1, LGD S2, LRLT S2; old S3 for
   TR3-1/TR3-2 (actual only) and LGD S3), flows, provisions (within-year and cumulative new S3), end-of-year exposures
   and provision stocks per stage and POCI, coverage ratios. PD PiT and LGD PiT new are blank, as in `cr_scen.csv`. No
   overlays, maturity or LTV columns (not in the template). Amounts in EUR million (8 decimals), parameters and ratios
   in percent (7 decimals).
6. **Checks.** TOTAL equals CR_SCEN rows 6 + 13 for every geography, scenario and year; C equals its two o/w rows;
   the sectors add up to TOTAL (`python/tests/test_engine.py`). The golden test compares every cell with the reference
   to 1 cent (EUR million) and 1e-9 (ratios).

## Off-balance-sheet exposures (CR_SCEN_OFF_BS)

Implemented in `src/off_balance.cpp` (engine) and `tools/reference/sora_reference.py` (reference), following EBA 2027
draft MN paras 78–82: nominal amounts and nominal amounts after CCF by stage and POCI, and provisions, for loan
commitments, financial guarantees and other commitments given, projected "with the same logic and constraints" as
on-balance exposures at portfolio level. Outputs `off_balance.csv` and `cr_scen_off_bs.csv`.

1. **Scope.** Enabled by the scenario key `off_balance` (absent: nothing changes, so on-balance results do not depend
   on it). Items are `sim_exposure` rows of `off_balance.exposure_types` (a subset of `loan_commitment`,
   `financial_guarantee`, `other_commitment`) with an IFRS 9 stage, in the measurement and intragroup scope of
   `scope`. Nominal = `off_balance_amount` (undrawn committed amount, guaranteed amount) at the reference-date FX rate.

   ```yaml
   off_balance:
     exposure_types: [loan_commitment, financial_guarantee, other_commitment]
     ccf_fallback: {loan_commitment: 0.4, financial_guarantee: 1.0, other_commitment: 0.5,
                    unconditionally_cancellable: 0.1}
     include_loan_undrawn: true          # item 7 (default false)
     commitment_drawn_on_balance: true   # item 7 (default false)
   ```

2. **Parameters.** An item takes the parameter path (starting point, satellite projection, customer segment and
   exposure overlays) of the on-balance segment `LOANS|portfolio|bucket` of its counterparty: the on-balance portfolio
   rules (a commitment has no CRE flag or household purpose, so NFC_*_OTHER and HH_OTHER) and the on-balance top-country
   buckets. If that segment has no loans, the portfolio's `OTHER` bucket is used (OBS-001); if that is missing too,
   the item is not projected (OBS-002, warning). Exposure-level customer parameters of the item apply as on-balance.
3. **CCF.** Customer `ccf` (sim_risk_parameter or `--parameters`, `actual`/0; exposure row, then the segment
   hierarchy, most specific first; OBS-003 counts them), else the scenario's regulatory fallback: CRR Art. 111(2)
   buckets for Annex I items: financial guarantees (credit substitutes) 100%, loan commitments 40%, other commitments
   (performance guarantees, documentary credits: medium risk) 50%, and 10% for loan and other commitments the
   institution may cancel unconditionally at any time (`is_unconditionally_cancellable`; CRR3 10%, MN 2025 table on
   Art. 495d). The CCF is static over the horizon (static balance sheet).
4. **Stage flows and provisions.** Per item, with post-CCF amount `E = CCF × nominal`, the on-balance Boxes 3–9 apply
   unchanged: `E` flows between stages with the segment's TR1-2, TR2-1, PD12M_S1/S2 (no cures from S3), S1/S2
   provisions use the segment's PD × LGD and LRLT_S2, new S3 provisions LGD_S1/S2, the old S3 provision is
   `max(E × LGD_S3, provision t0)` per item (no release), POCI and its provision are static, and the final adverse
   year uses the 5/6–1/6 blend. The nominal amount follows the same stage flows (it carries no provision).
5. **Starting provision.** `loss_allowance` of a facility covers its drawn and undrawn parts (SIM grain: one row per
   facility). The off-balance provision is the undrawn share, `allowance × off_balance / (off_balance + GCA)`
   (the whole allowance if both are 0). The drawn part of a commitment is on-balance (item 7).
6. **CR_SCEN_OFF_BS.** Groups (parameter segment × exposure type) aggregate to commitment type × counterparty sector
   (CB, GG, CI, OFC, NFC, HH): per type a Sum row and six Pivot rows, then Total (22 rows), geography Total only (MN
   para 79), for Actual and baseline/adverse years 1–3. Nominal before CCF is reported for projected years too
   (MN para 81). EUR million. Groups of exposure type `loan` (item 7) count as loan commitments given.
7. **Facilities with a drawn and an undrawn part.** A facility is split the way FINREP reports it: the drawn part
   (gross carrying amount) is a loan and advance (FINREP F 04/F 18; MN para 60: CR_SCEN is the on-balance positions),
   the undrawn part a loan commitment given (F 09.01; MN paras 78, 80–81; template guidance para 41: FINREP Annex V
   Part 2.102–105, 113, 116; and, for CR_NPL, para 51: "the nominal value of loan commitments shall be the undrawn
   amount that the institution has committed to lend"). Two switches,
   off by default, so that existing scenarios are unchanged; `tests/scenarios/test_eba2025.yaml` enables both and the
   golden results cover them.
   * `include_loan_undrawn`: the undrawn part (`off_balance_amount` > 0) of each in-scope `loan` (13,619 in the
     reference SIM: revolving credit facilities, credit cards, working-capital and on-demand facilities, commodity
     finance) is an off-balance item of exposure type `loan` in `off_balance.csv`, reported as *Loan commitments
     given*. It keeps the loan's own on-balance segment (its CRE flag and household purpose included), its
     exposure-level customer parameters, and its customer CCF (exposure row, then the segment hierarchy); else the
     loan-commitment fallback: CRR Annex I bucket 3(a) ("the undrawn amount of commitments, regardless of the maturity
     of the underlying facility", 40%), or bucket 5(a)/(b) (unconditionally cancellable commitments and cancellable
     retail credit lines, 10%) when `is_unconditionally_cancellable`. Being revolving does not change the bucket
     (Annex I classifies undrawn facilities by cancellability, not by revolving character), so `is_revolving` is not
     used.
   * `commitment_drawn_on_balance`: the drawn part of each staged commitment of `exposure_types` with a gross carrying
     amount > 0 (DS-008: drawn amount plus accrued interest; 6,455 in the reference SIM) is an on-balance
     loans-and-advances exposure in every respect: segmentation (segment `LOANS|portfolio|bucket` by the on-balance
     portfolio rules), top-country ranking, calibration (stage history and coverage), projection, collateral/LTV,
     CR_SCEN, CR_SECTOR and the calculator records. Its undrawn part stays an off-balance item as before.
   * **Provisions.** The SIM has one allowance per facility; the ECL of the drawn and the undrawn component are not
     separately identifiable. Under IFRS 7.B8E (which FINREP follows for the accumulated impairment of the asset)
     the combined ECL would then be presented with the drawn asset, and nothing as a provision for the commitment.
     The stress test however asks for the off-balance provisions separately (MN para 78) and projects them from
     their own starting stock, and CR_SUM adds on- and off-balance impairments (template guidance para 24): keeping
     the whole allowance on-balance while projecting the undrawn part would either count the provision twice (if
     the off-balance item also got a share) or project the undrawn part from a zero stock (a year-1 build-up that
     is not a scenario effect). The allowance is therefore allocated pro rata to the amounts, as for commitments
     (item 5): on-balance `allowance × GCA / (GCA + undrawn)` (`Segmentation::allowance`, also used for the
     starting-point stocks and the coverage-based LGD S3 / LRLT S2 calibration, so that the calibrated coverage
     equals the reported CR_SCEN coverage), off-balance `allowance × undrawn / (GCA + undrawn)`. The two add up to
     the facility's allowance: no provision is counted twice or lost. (Before, the drawn share of the commitments'
     allowance, EUR 9.8m in the reference SIM, was in neither output.) A split by expected exposure
     (GCA vs CCF × undrawn) would put less of the allowance on the undrawn part; it is not used because it would
     make the starting provisions depend on the CCF. Loans without an undrawn part, and all loans without
     `include_loan_undrawn`, keep their whole allowance.

## Determinism

The flow model is deterministic by construction.

If discrete probabilistic events are enabled (non-EBA mode: draw default events per contract), they must be reproducible. Derive pseudo-random values from:

```text
hash(scenario_id, contract_key, event_type, time_step)
```

This avoids shared RNG state and keeps results reproducible under multithreading.

## Event generation

Events are compact fixed structs (contract index, step, event type, amount), not heap-allocated polymorphic objects.

Event types:

- Stage 1 → 2, 2 → 1, 1 → 3, 2 → 3 (flow mass per contract)
- Default (probabilistic mode only)
- Collateral revaluation
- Maturity and like-for-like replacement
- Deposit withdrawal (funding module)
- Repricing / refinancing cost increase (NII module)
