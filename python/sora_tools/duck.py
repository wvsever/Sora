"""Sandboxed DuckDB connections.

Mapping SQL may be written by people or AI agents, so it runs in a DuckDB connection that can only
read the export directory and write the output directory: no network, no extensions, no other files,
no attached databases, and a locked configuration.
"""

from __future__ import annotations

from pathlib import Path

import duckdb


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sandboxed_connection(read_dirs: list[Path], write_dirs: list[Path] = (),
                         memory_limit: str = "4GB", threads: int | None = None,
                         progress_bar: bool = True) -> duckdb.DuckDBPyConnection:
    for d in write_dirs:
        Path(d).mkdir(parents=True, exist_ok=True)
    dirs = [str(Path(d).resolve()) for d in [*read_dirs, *write_dirs]]
    con = duckdb.connect(":memory:")
    con.execute(f"SET memory_limit = {quote_str(memory_limit)}")
    if threads:
        con.execute(f"SET threads = {int(threads)}")
    if not progress_bar:   # the progress bar writes to stdout, which is the protocol channel of sora-mcp
        con.execute("SET enable_progress_bar = false")
    con.execute("SET autoinstall_known_extensions = false")
    con.execute("SET autoload_known_extensions = false")
    con.execute("SET allowed_directories = [" + ", ".join(quote_str(d) for d in dirs) + "]")
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con
