# Golden results: reference dataset 2026-06-30

Expected outputs for the C++ engine, produced by the independent reference implementation
(`tools/reference/sora_reference.py`) from:

- the SIM dataset of the reference mapping (`mappings/cppbank` on `tests/data/20260630.7z`)
- the fixed test scenario `tests/scenarios/test_eba2025.yaml` (EBA 2025 macro paths, synthetic satellite coefficients)

| File | Content |
|---|---|
| `segments.csv` | Segments and starting-point stocks (EUR): exposure and provisions per stage |
| `parameters.csv` | Calibrated starting-point parameters (`actual`, year 0) and projected parameters per scenario and year, in the `sim_risk_parameter` layout. `calibration_levels` shows which hierarchy level each part came from. |
| `projection.csv` | Stage flows, exposures, provisions per component (EBA Boxes 3–9) and impairment per segment, scenario and year |
| `collateral.csv` | Static-balance-sheet LTV per segment, scenario (actual year 0, baseline and adverse 1..3) and t0 stage: secured exposure, real-estate collateral value under the property price paths, LTV (`plans/03_scenario_engine.md`, Collateral repricing and LTV) |
| `cr_sector.csv` | EBA 2027 draft CSV_CR_SECTOR layout: the NFC portfolio (CR_SCEN rows 6 + 13) by NACE Rev. 2.1 section, with C split into energy-intensive and other, per geography (Total, top countries, Other), scenario and year (actual, baseline and adverse 1..3); 46 template columns, amounts in EUR million, parameters and ratios in percent (`plans/03_scenario_engine.md`, CR_SECTOR) |
| `off_balance.csv` | Off-balance items (loan commitments, financial guarantees, other commitments given; scenario key `off_balance`) per parameter segment, exposure type, scenario and year (actual year 0, baseline and adverse 1..3): nominal amounts by stage (`nom_*`), post-CCF amounts, stage flows, provisions per component and impairment in the `projection.csv` columns (`plans/03_scenario_engine.md`, Off-balance-sheet exposures) |
| `cr_scen_off_bs.csv` | The same in the EBA CSV_CR_SCEN_OFF_BS layout (2027 draft templates): 22 rows per scenario and year (per commitment type a Sum row and six counterparty-sector rows, then Total), EUR million |
| `benchmarks.csv` | ECB benchmark rule (scenario key `benchmark_parameters`, synthetic benchmarks `tests/params/synthetic_ecb_benchmarks.csv`) per segment: t0 exposure, model coverage per group (PD/TR, LGD/LR), the rule that applies (`none`, `sovereign`, `coverage`, `no_model`) and the benchmark key used, `unavailable` or empty (`plans/09_risk_parameters.md`, ECB benchmark parameters) |
| `summary.json` | Totals (`off_balance`: item counts and off-balance totals per scenario and year; `benchmark`: segment counts, model coverage and benchmark share per pivot asset class) |

The results are **synthetic**. Since the dataset was regenerated with realistic stage transitions (DS-016 fixed,
see `tests/data/DATASET_ISSUES.md`), the magnitudes are plausible, but they still exist to check that the engine
computes the same numbers, not to be read as a stress-test outcome.

Regenerate after an intended change, and review the diff:

```sh
python tools/extract_testdata.py
sora-tools map mappings/cppbank --export build/testdata/20260630 -o build/sim/20260630
python tools/reference/sora_reference.py --sim build/sim/20260630 \
    --scenario tests/scenarios/test_eba2025.yaml --out tests/golden/20260630
```

The script writes the summary's `sim_mapping_release` of the SIM it read. It is not compared; keep the committed value
when only other results change.

Engine tolerance: money is compared per segment, scenario and year to 1 cent, or a relative 1e-12
(whichever is larger). Parameters and LTV ratios are compared to 1e-9. In `cr_sector.csv` the same tolerances apply in
its units (EUR million, percent), plus one unit in the last printed decimal. `cr_scen_off_bs.csv` (EUR million,
8 decimals) is compared to 2e-8. `benchmarks.csv`: exposure to 1 cent, flags, rules and keys exactly; the summary's
`benchmark` counts exactly and its coverage shares to 1e-9.

Off-balance results: with the regulatory fallback CCFs (no customer CCF in the reference SIM), 13,660 staged
commitments (EUR 2.41bn nominal, 1.32bn post-CCF). Five `other_commitment` items to general government in SG take
the parameters of `LOANS|GG|OTHER` (no GG loans in SG). The synthetic book provisions commitments at a flat
coverage of drawn plus undrawn (about 1% in stage 1), far above PD x LGD of the loan segments, so the year-1
impairment on off-balance items is a release in both scenarios. This is a property of the data, not of the method.

ECB benchmark results (synthetic benchmark values; `model_level: portfolio`, 10% threshold, sovereigns on). On the
reference data model coverage is all or nothing per pivot asset class: a pivot class whose starting point had to be
calibrated at `LOANS|ALL|ALL` or `ALL|ALL|ALL` (too few transitions) has 0% coverage, all others 100%. 28 of 151
segments take benchmark parameters for both groups:

- `LOANS|CI` (EUR 492m, 0% coverage: stage 2 transitions and LGD come from `LOANS|ALL|ALL`) - all 11 segments, BE, DE,
  ES, FR, LU, PL with their own benchmark, CH, JP, SG, US, OTHER with `WR`. The derived PD12M S1 was at the 0.001% floor
  with TR1-2 = 0; the benchmark (e.g. `LOANS|CI|OTHER` adverse year 3: PD12M S1 0.41%, TR1-2 6.2%, LGD 50%) raises the
  year-3 provision stock from EUR 0.03m to 4.34m (baseline) and from 0.06m to 11.93m (adverse).
- `DEBT_SEC|GG` (EUR 147m, 0% coverage): sovereign benchmark for BE, DE, ES, FR, LU, PL, `WR` for the others; LGD falls
  from the pooled 65.5% to 30-38%, so the year-3 stock falls from 2.44m to 0.38m (baseline) and 3.54m to 1.00m (adverse).
- `LOANS|GG` (100% coverage): only the six countries with a sovereign benchmark (54.2% of the exposure, para 146);
  year-3 stock +0.19m (baseline), +0.54m (adverse).
- 31 segments need a benchmark but the file has none (MN footnote 13: debt securities to CI, OFC, NFC and loans to
  central banks): they keep their model parameters (BMK-002).

Totals: impairment +1.03m / +0.69m / +0.72m (baseline years 1-3) and +2.82m / +3.65m / +3.41m (adverse); year-3
provision stock 296.4m -> 298.9m (baseline), 472.3m -> 482.2m (adverse); new S3 exposure in adverse year 3 +14.9m.
Off-balance items take the benchmarked `LOANS|CI` and `LOANS|GG` paths: year-3 off-balance provisions +8.4m
(baseline) and +22.8m (adverse), mostly CI guarantees and other commitments; the year-1 off-balance impairment is
still a release (-9.9m baseline, -4.4m adverse). `segments.csv`, `collateral.csv` and `cr_sector.csv` (NFC, 100%
coverage) are unchanged. Without the scenario key every result is byte-identical to the run without benchmarks.
