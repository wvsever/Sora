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
