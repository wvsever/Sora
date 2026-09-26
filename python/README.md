# sora-tools

Python tooling around the Sora engine: SIM schema, SQL mapping on exported files, validation.

```sh
pip install -e "python[test]"

sora-tools schema lint                         # check schemas/sim
sora-tools schema markdown -o schemas/sim/SIM_REFERENCE.md
sora-tools schema llm                          # compact description for AI agents
sora-tools schema ddl                          # CREATE TABLE statements

python tools/extract_testdata.py               # -> build/testdata/20260630
sora-tools profile build/testdata/20260630 --types-file _csv_column_types.json -o dictionary.yaml
sora-tools map mappings/cppbank --export build/testdata/20260630 -o build/sim/20260630 --validate
sora-tools validate build/sim/20260630 --modules core credit calibration

sora-tools scenario-import <macro.xlsx> <gva.xlsx> -o scenarios/eba2025_macro.csv
sora-tools calculator-stub --mode formula --port 8080

sora-tools estimate-satellites build/sim/20260630 \
    --cycle-index build/testdata/20260630/reference/macro_cycle_index.csv \
    --prior tests/params/synthetic_satellites.csv -o satellites.csv --report satellites_fit.json

python tools/reference/sora_reference.py --sim build/sim/20260630 \
    --scenario tests/scenarios/test_eba2025.yaml --out tests/golden/20260630

python -m pytest python/tests
```

Mapping SQL runs in a sandboxed DuckDB connection. It can read only the export directory and write only
the output directory. Network, extensions and attached databases are blocked. See `plans/10_input_model_and_mapping.md`.

`estimate-satellites` estimates the satellite coefficients (`beta_gdp`, `beta_unemployment`, `beta_property`) per EBA
portfolio from `sim_stage_history`: the logit of the rolling 12-month default rate of the performing book (or of the
chosen transition rates, `--transitions`) regressed on window means of macro drivers, with a trend per portfolio,
shrinkage towards the pooled fit and sign constraints. It writes the layout of `tests/params/synthetic_satellites.csv`
(usable as a scenario's `satellites` file) and a JSON fit report (n, R², SEs, shrinkage, fallbacks per equation).
Drivers come from `--macro-history` (historical series) or, for the reference dataset, which has no historical macro
series, from `--cycle-index`: the generator's cycle index as a **proxy** for GDP growth (unemployment and property
slopes are then not identified and 0). `lgd_property_sensitivity` is not estimated (taken from `--prior`). On synthetic
data the result demonstrates the tooling; it is not a calibration. Details and limits: `plans/03_scenario_engine.md`,
"Satellite estimation".
