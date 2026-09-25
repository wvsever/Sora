#!/usr/bin/env python3
"""Extract the reference test dataset (tests/data/20260630.7z) into build/testdata/20260630.

Uses the `7z` command line tool if installed, otherwise the `py7zr` Python package.
The GL tables (journal_line, journal_entry, gl_balance; about 70% of the volume) are skipped
unless --all is given, because the stress engine does not need them.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ARCHIVE = REPO / "tests" / "data" / "20260630.7z"
DEFAULT_OUT = REPO / "build" / "testdata" / "20260630"
GL_TABLES = ("accounting/journal_line/", "accounting/journal_entry/", "accounting/gl_balance/")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("-o", "--output", type=Path, default=DEFAULT_OUT)
    p.add_argument("--all", action="store_true", help="also extract the GL tables")
    p.add_argument("--force", action="store_true", help="re-extract even if the output exists")
    args = p.parse_args()

    marker = args.output / ".extracted"
    if marker.exists() and not args.force:
        print(f"{args.output} already extracted")
        return 0
    if args.output.exists():
        shutil.rmtree(args.output)
    args.output.mkdir(parents=True)

    if shutil.which("7z"):
        cmd = ["7z", "x", "-y", f"-o{args.output}", str(ARCHIVE)]
        if not args.all:
            cmd += [f"-xr!{t.split('/')[1]}" for t in GL_TABLES]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    else:
        try:
            import py7zr
        except ImportError:
            print("Install 7-Zip (7z) or `pip install py7zr`.", file=sys.stderr)
            return 1
        with py7zr.SevenZipFile(ARCHIVE) as z:
            names = [i.filename for i in z.list() if not i.is_directory
                     and (args.all or not i.filename.startswith(GL_TABLES))]
            z.extract(path=args.output, targets=names)
    marker.write_text("ok\n")
    print(f"Extracted {ARCHIVE.name} to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
