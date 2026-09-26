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
sora-tools calculator-stub --mode formula --port 8080   # --reject <regex>, --omit <parameters>, --latency <s>

sora-tools estimate-satellites build/sim/20260630 \
    --cycle-index build/testdata/20260630/reference/macro_cycle_index.csv \
    --prior tests/params/synthetic_satellites.csv -o satellites.csv --report satellites_fit.json

python tools/reference/sora_reference.py --sim build/sim/20260630 \
    --scenario tests/scenarios/test_eba2025.yaml --out tests/golden/20260630

sora-tools explain out/ --segment 'LOANS|HH_HOUSE|DE' --scenario adverse   # why this impairment? (--json)
sora-tools explain out/                                  # overview: totals and top segments
sora-tools diff-runs out_base/ out_overlay/ --top 10     # what changed, and why (--json, --fail-on-diff)

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

## Explaining and comparing results

`sora-tools explain <output dir> --segment <key> [--scenario] [--year]` (module `sora_tools/explain.py`) reads the
engine output and explains a segment's impairment: starting stocks and coverage, the parameters per scenario and
year with their `source` (`derived`, `external`, `mixed`, `benchmark`) and `calibration_levels`, the ECB benchmark
rule (`benchmarks.csv`), stage flows and the provision components per EBA box (Boxes 4-9 and the Box 3 stock),
with each year's impairment reconciled to the change in the stock. Without `--segment` it gives an overview with
the top segments. `--json` prints the structured result; the default is the narrative.

`sora-tools diff-runs A B` (module `sora_tools/diff_runs.py`) compares two output directories: every CSV matched by
its key columns, numbers within `max(--abs-tol, --rel-tol * |value|)` (default 0.01 and 1e-9), `summary.json`
leaves and diagnostics; impairment totals per scenario and year and the top movers. The change of each segment's
impairment is split into an **exposure** and a **coverage** effect (symmetric two-factor split of the provision
stock `S = E x c`, exact by construction), and the movers list their **drivers**: parameter fields that changed
(at t and t+1, with the `source`), and changed starting stocks. Filters: `--segment`, `--scenario`, `--year`.

## sora-mcp (MCP server)

```sh
pip install -e "python[mcp]"          # the official MCP Python SDK (1.x or 2.x); the tools work without it
sora-mcp --root /data/export --root /data/sora --workdir /data/sora-mcp --engine build/release/sora
sora-mcp tools                        # list the tools
sora-mcp call explain_result '{"output_dir": "tests/golden/20260630", "segment": "LOANS|GG|BE"}' --root .
sora-mcp pending                      # approval requests (for a person, not the agent)
sora-mcp approve <request id> --by "Jane Reviewer"
```

A local stdio server (module `sora_tools/mcp_server.py`, console script `sora-mcp`). Register it in the agent's
MCP client configuration, e.g. `{"command": "sora-mcp", "args": ["--root", "/data/export", ...]}`.

| Tool | Does |
|---|---|
| `describe` | SIM model: overview, a table, a column, a code list; `format="llm"` the full LLM description; `table="outputs"` the engine output files |
| `profile_source` | Metadata profile of an export: tables, files, rows, per column type, null rate, distinct count, code lists |
| `test_mapping` | Run a mapping on an export into a new SIM under the work directory, and validate it |
| `validate_sim` | Full SIM validation report |
| `reconcile` | SIM totals vs source control totals (`<mapping>/reconciliation.yaml`, `sora_tools/reconcile.py`) |
| `run_scenario` | Run `sora run` (binary from `--engine` / `SORA_ENGINE`) into a new output directory; totals and diagnostics |
| `explain_result` | As `sora-tools explain` |
| `diff_runs` | As `sora-tools diff-runs` |

Every result has `status`: `ok`, `error`, `denied` (security policy) or `requires_approval`.

Security:

- **Read roots.** Path arguments are resolved (symlinks too) and must be under a `--root` (`SORA_MCP_ROOTS`,
  `os.pathsep`-separated) or the work directory; relative paths are relative to the first root. Paths inside the
  scenario YAML (`macro_path`, `satellites`, `benchmark_parameters.file`, ...) are checked the same way, and a
  mapping's `types_file` must stay in the export.
- **Writes** go only to server-named directories under the work directory (`--workdir` / `SORA_MCP_WORKDIR`,
  default a new temporary directory): `sim/` (test_mapping) and `runs/` (run_scenario). No tool takes an output path.
- **Metadata only by default.** Raw values (profile sample rows and min/max, validation sample keys) need
  `include_values=true` in the call and the customer setting `--allow-values` (`SORA_MCP_ALLOW_VALUES=1`), capped at
  `--max-rows` (`SORA_MCP_MAX_ROWS`, default 20) rows. Code lists of low-cardinality, non-sensitive columns are
  metadata (as in `sora-tools profile`).
- **No shell.** The engine runs with an argument list (`shell=False`), a timeout, and resolved absolute paths; the
  agent cannot choose the binary. Mapping SQL runs in the sandboxed DuckDB connection (files only).
- **Production mappings need human approval.** A mapping is production if `mapping.yaml` has `status: production`
  (default `draft`) or its path matches `--production-pattern` (`SORA_MCP_PRODUCTION_PATTERNS`, default
  `*/production/*`). `test_mapping` then writes nothing and returns `requires_approval` with a request id bound to
  the mapping release (hash of all mapping files) and the export. A person runs `sora-mcp approve <id> --by <name>`;
  the record (who, when, OS user) is kept in `--approvals-dir` (`SORA_MCP_APPROVALS_DIR`, default
  `~/.sora-mcp/approvals`, never inside the work directory). A changed mapping is a new release and needs a new approval.
- **Audit.** Every call is appended to `<workdir>/audit.jsonl` (`--audit-log`): time, user, tool, arguments,
  SHA-256 input fingerprint, status, duration.
- The server keeps file descriptor 1 for the protocol and sends everything else (DuckDB progress, logs) to stderr.
