"""Every script-style suite in tests/, as pytest sees it.

The suites here are scripts: each checks as it goes and exits non-zero from
main() if anything failed, so they are run one by one in the image.
Importing one under pytest would run its checks at collection and report
nothing, which is why `python_files` points pytest at this file alone and this
file runs each suite as its own process -- the same way CI does, so a suite
that passes here passes there.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SUITES = sorted(p for p in HERE.glob("test_*.py") if p.name != Path(__file__).name)

# Suites for scripts that run on the Actions runner, not in the image, and the
# runner's tools they need. scripts/self.sh runs these on the host; in the image,
# which has no awk, they are skipped by name rather than failed.
HOST_TOOLS = {"test_release_version.py": ("awk", "bash")}


@pytest.mark.parametrize("suite", SUITES, ids=lambda p: p.name)
def test_suite(suite: Path) -> None:
    missing = [t for t in HOST_TOOLS.get(suite.name, ()) if shutil.which(t) is None]
    if missing:
        pytest.skip(f"runs on the Actions runner; no {', '.join(missing)} here")
    proc = subprocess.run(
        [sys.executable, str(suite)],
        capture_output=True,
        text=True,
        cwd=HERE.parent,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
