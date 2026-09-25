# sora-tools

Python tooling around the Sora engine: SIM schema, SQL mapping on exported files, validation.

```sh
pip install -e "python[test]"

sora-tools schema lint                         # check schemas/sim
sora-tools schema markdown -o schemas/sim/SIM_REFERENCE.md
sora-tools schema llm                          # compact description for AI agents
sora-tools schema ddl                          # CREATE TABLE statements

python tools/extract_testdata.py               # -> build/testdata/20260630
sora-tools map mappings/cppbank --export build/testdata/20260630 -o build/sim/20260630 --validate
sora-tools validate build/sim/20260630 --modules core credit calibration

python -m pytest python/tests
```

Mapping SQL runs in a sandboxed DuckDB connection. It can read only the export directory and write only
the output directory. Network, extensions and attached databases are blocked. See `plans/10_input_model_and_mapping.md`.
