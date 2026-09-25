import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TESTDATA = Path(os.environ.get("SORA_TESTDATA", REPO / "build" / "testdata" / "20260630"))


@pytest.fixture(scope="session")
def repo() -> Path:
    return REPO


@pytest.fixture(scope="session")
def testdata() -> Path:
    """The extracted reference export. Extracted on demand; tests skip if extraction is impossible."""
    if not (TESTDATA / ".extracted").exists():
        r = subprocess.run([sys.executable, str(REPO / "tools" / "extract_testdata.py"), "-o", str(TESTDATA)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip(f"test data not available: {r.stderr.strip()}")
    return TESTDATA


@pytest.fixture(scope="session")
def reference_sim(testdata, tmp_path_factory) -> Path:
    """SIM dataset produced by the reference mapping (once per test session)."""
    from sora_tools.mapping import run_mapping

    out = tmp_path_factory.mktemp("sim")
    run_mapping(REPO / "mappings" / "cppbank", testdata, out, log=lambda *_: None)
    return out
