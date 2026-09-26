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
| `summary.json` | Totals (`off_balance`: item counts and off-balance totals per scenario and year) |

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
8 decimals) is compared to 2e-8.

Off-balance results: with the regulatory fallback CCFs (no customer CCF in the reference SIM), 13,660 staged
commitments (EUR 2.41bn nominal, 1.32bn post-CCF). Five `other_commitment` items to general government in SG take
the parameters of `LOANS|GG|OTHER` (no GG loans in SG). The synthetic book provisions commitments at a flat
coverage of drawn plus undrawn (about 1% in stage 1), far above PD x LGD of the loan segments, so the year-1
impairment on off-balance items is a release in both scenarios. This is a property of the data, not of the method.
