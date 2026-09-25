# Risk Parameters Plan (PD, LGD, transition rates)

## Objective

Provide the starting-point credit risk parameters that the stress projection needs. The reference dataset does not contain them.

## The gap

The EBA 2027 methodology (Draft Methodological Note, §2.3.3.1, Table 3) projects IFRS 9 provisions from 12-month point-in-time (PiT), exposure-weighted parameters per portfolio segment:

| Parameter | Meaning | Used in |
|---|---|---|
| `PD12M_S1` (TR1-3) | S1 → S3 within 12 months | Boxes 5, 8 |
| `PD12M_S2` (TR2-3) | S2 → S3 within 12 months | Boxes 7, 8 |
| `TR1-2` | S1 → S2 | Boxes 5, 6 |
| `TR2-1` | S2 → S1 | Boxes 4, 7 |
| `TR3-1`, `TR3-2` | Cures (starting point only, not projected) | Reporting |
| `LGD_S1`, `LGD_S2` | Loss given S1→S3 or S2→S3 | Boxes 4, 5, 8 |
| `LRLT_S2` | Lifetime loss rate on S2 exposure | Boxes 6, 7 |
| `LGD_S3` | Lifetime loss rate on the existing S3 stock | Box 9 |

The same parameters are then needed for each year 2027–2029, per scenario (baseline, adverse). These are the projected parameters produced by the satellite models or ECB benchmarks from the macro scenario.

`tests/data/20260630.7z` contains none of these per contract or per segment. It does contain the history needed to estimate most of them:

| Parameter | Source in test data | Method |
|---|---|---|
| `TR1-2`, `TR2-1`, `PD12M_S1`, `PD12M_S2`, `TR3-x` | `impairment_allowance.declared_stage_at_period_end`, monthly 2025-12 to 2026-06 (plus 2025-09) | Exposure-weighted monthly transition matrix per segment, annualised (matrix power 12, S3 absorbing for PD). The rating-based PD master scale is the fallback for thin segments. |
| `LGD_S3` | `impairment_allowance` S3 coverage, `contract_recovery_cashflow`, `contract_event` (write_off, recovery) | Max of S3 coverage and realised workout LGD (discounted recoveries net of work-out cost) |
| `LGD_S1`, `LGD_S2` | Collateral (`collateral`, `collateral_allocation`, haircuts by form), `guarantee_received`, realised LGD | Secured/unsecured split: `max(0, EAD − haircut × collateral) / EAD` blended with realised unsecured LGD |
| `LRLT_S2` | S2 coverage in `impairment_allowance` and `contract_loan.impairment_allowance` | Coverage ratio, checked against `lifetime PD × LGD_S2` |
| `CCF` (off-balance) | `contract_utilisation_snapshot`, `contract_commitment` | Observed drawdown in the history; CRR Art. 111 as the regulatory fallback |
| `LTV` | Collateral value vs gross carrying amount | Direct |
| Contract PD (optional) | `counterparty_rating` (21% of loans rated) | External-rating master scale |

Observed monthly loan stage transitions (all segments): S1→S2 2.29%, S1→S3 0.02%, S2→S1 9.73%, S2→S3 7.56%, S3→S2 11.23%. These give plausible annual magnitudes but are synthetic. See `tests/data/README.md`.

## Design

### Who supplies what

Sora is vendor software. The customer (the institution) runs it on its own data. Anything that is model output or supervisory material is a **customer input**. Sora defines the format and the validation, and never ships the content:

| Input | Owner | Sora's role |
|---|---|---|
| Bank dataset | Customer | Input format, mapping, validation |
| Starting-point PD/TR/LGD/LR | Customer (IFRS 9 / IRB models) | Import format, checks, optional derivation (`sora calibrate`) |
| Satellite model coefficients / projected parameters | Customer | Import format, evaluation engine |
| ECB benchmark parameters | ECB → customer, confidential, per exercise | Import format and application rules (10% rule, portfolio level, no adjustment) |
| Macro / market scenarios | EBA / ESRB / ECB, public | Converter (`tools/scenario_import`) |

For development and testing, all customer inputs are replaced by **synthetic equivalents** generated in-house: the reference dataset, derived parameters, synthetic satellite coefficients and a synthetic benchmark file. Their format matches the real inputs exactly. Real customer or supervisory content is never committed.

### Parameter sources, in priority order

1. **External parameter file** (`risk_parameters.csv`): the customer's own IFRS 9 / IRB model output, per contract or per segment. This is the production path, and the only one acceptable for a real EBA submission.
2. **Derived starting point**: `sora calibrate` estimates the parameters from the history tables above. It is used for the reference dataset, for demos and onboarding, and as a challenger or plausibility check against the customer's parameters in production.
3. **Benchmark fallback**: customer-loaded benchmark tables (e.g. the ECB benchmark PD/TR and LGD/LR per portfolio and country) for segments with no model or insufficient data. The EBA 10% coverage rule is applied per pivot asset class. The benchmark file format is Sora's own. The customer maps the ECB-provided files into it, or a per-exercise import adapter is added once a customer can share the layout (not the values).

Every parameter records its source (`external`, `derived`, `benchmark`) and the observation count. Both are reported in the output.

### Parameter file format

```text
level,segment_or_contract_id,scenario,year,pd12m_s1,pd12m_s2,tr1_2,tr2_1,tr3_1,tr3_2,lgd_s1,lgd_s2,lgd_s3,lrlt_s2,ccf,source
segment,NFC_SME_CRE|BE,actual,2026,0.012,0.18,0.06,0.25,0.00,0.05,0.22,0.24,0.35,0.09,,external
contract,CL-000001,actual,2026,0.004,,,,,,0.12,,,,,external
```

- `level = segment` or `contract`. Contract values override segment values.
- `scenario = actual` is the starting point. `baseline`/`adverse` rows for 2027–2029 are optional overrides of the projected values.
- Values are decimal fractions. They are stored as parts-per-billion.

### Segmentation

Segments follow the EBA CR_SCEN portfolios (2027 MN Table 4) and are resolved once at load time:

- **Loans:**
  - Sectors: central banks, general governments, credit institutions, other financial corporations.
  - NFCs: SME-CRE, SME-Other, non-SME-CRE, non-SME-Other.
  - Households: house purchase, consumption, other.
- **Debt securities:** the same sectors, without the NFC/household splits.
- **Country:** the top 10 exposure countries plus "Other".
- **CR_SECTOR:** NACE section for NFCs.

Mapping rules in the reference dataset:

- **Sector:** from `counterparty.esa2010_sector`:
  - S.121 → central banks
  - S.13x → general governments
  - S.122/S.123 → credit institutions
  - S.124–S.129 → other financial corporations
  - S.11 → NFCs
  - S.14/S.15 → households
- **Household purpose:** from `product_code`, refined by `purpose_code`:
  - `RESI_MTG` → house purchase. `equity_release` and `home_improvement` go to other if configured.
  - `CONSUMER`, `CREDIT_CARD`, consumer `PURCHASED_RECEIVABLES` → consumption.
  - Everything else → other.
- **NFC SME:** from counterparty turnover, employees and balance sheet (EU SME definition).
- **CRE:** `product_code = CRE`, or commercial property collateral.
- **Country:** `counterparty.country_of_residence`.

The mapping table is data (`segments.csv`), not code. Unmapped records are reported as a validation error.

### Calibration rules

- Exposure-weighted, per segment, from the latest 6–12 monthly observations.
- Annualise transitions with the matrix power of the average monthly matrix. Never multiply a monthly rate by 12.
- Minimum observation count per segment (configurable, default 100 contracts). Below it, fall back to the parent segment (country → Total) and then to the benchmark.
- Conservatism only: approximated starting points may only be adjusted upwards (MN paras 109–110).
- Technical floor on PD of 0.001% (from the 2025 template guidance; configurable).
- Calibration is deterministic and writes a parameter file in the external format. That file can be reviewed, edited and passed back as source 1.

### Projection (2027–2029)

The projected parameters come from the scenario engine (`03_scenario_engine.md`), in one of two ways:

- Satellite models (linear / logit-linear in macro variables per segment)
- User-supplied projected parameter tables

The risk-parameter module only supplies the starting point and the per-segment sensitivities.

## Result data

Validating the engine needs expected results. None are included in the test data. The plan (details in `06_validation.md`):

1. **Hand-computed unit cases** for each EBA box formula (Boxes 3–9), using a few segments with round numbers.
2. **An independent reference implementation** (Python/DuckDB script under `tools/reference/`) that computes the calibrated parameters and the 3-year provision projection on `ref-1x`. Its output is committed as golden CSVs under `tests/golden/20260630/`.
3. **Customer acceptance runs**: at onboarding, the customer runs Sora next to its existing stress-test process and compares the CR_SCEN output. This happens on the customer's premises. Only anonymised differences come back as regression cases, never data.

## Open questions

- Is the format of `tests/data` (the synthetic generator output) Sora's canonical input format, or do customers deliver in another layout that needs a mapping layer?
- Which EBA exercises must be supported at the same time (2025 final, 2027 draft, later)? This decides how the methodology is versioned.
- Should IRB/STA REA (PDreg, LGDreg, ELBE, output floor) be in scope in phase 1, or only IFRS 9 provisions?
