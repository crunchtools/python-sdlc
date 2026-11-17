#!/usr/bin/env python3
"""`scripts/release-version.sh` must release only what it is sure of.

release.yml cuts whatever tag this prints. Two ways it used to get that wrong:
the CHANGELOG heading was matched as a regex, so the dots in a version matched
any character, and every `gh release view` failure counted as "not released",
so an auth or network error looked like a version waiting to ship. This runs
the script against a fake `gh` and asserts each case.

Run: tests/test_release_version.py   (needs python3 and bash)
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release-version.sh"

GH_RELEASED = "#!/bin/sh\necho 'title: v0.7.3'\n"
GH_MISSING = "#!/bin/sh\necho 'release not found' >&2\nexit 1\n"
GH_AUTH = "#!/bin/sh\necho 'HTTP 401: Bad credentials' >&2\nexit 1\n"


def run(version: str, changelog: str, gh: str) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
        (root / "CHANGELOG.md").write_text(changelog)
        bindir = root / "bin"
        bindir.mkdir()
        (bindir / "gh").write_text(gh)
        (bindir / "gh").chmod(0o755)
        env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
        proc = subprocess.run(
            [str(SCRIPT)], cwd=root, env=env, capture_output=True, text=True, check=False
        )
        return proc.returncode, proc.stdout.strip()


CASES = [
    # (name, version, changelog, gh, expected exit, expected stdout)
    ("unreleased with heading", "0.7.3", "## [0.7.3] - 2026-11-17\n", GH_MISSING, 0, "v0.7.3"),
    ("heading with no date", "0.7.3", "## [0.7.3]\n", GH_MISSING, 0, "v0.7.3"),
    ("already released", "0.7.3", "## [0.7.3] - 2026-11-17\n", GH_RELEASED, 0, ""),
    ("no heading", "0.7.3", "## [0.7.2] - 2026-11-17\n", GH_MISSING, 1, ""),
    ("dots are not wildcards", "0.7.3", "## [0x7x3] - 2026-11-17\n", GH_MISSING, 1, ""),
    ("longer version is not a match", "0.7.3", "## [0.7.30] - 2026-11-17\n", GH_MISSING, 1, ""),
    ("gh auth failure is not unreleased", "0.7.3", "## [0.7.3] - 2026-11-17\n", GH_AUTH, 1, ""),
    ("no version", "", "## [0.7.3]\n", GH_MISSING, 1, ""),
]


def main() -> int:
    failed = 0
    for name, version, changelog, gh, want_code, want_out in CASES:
        code, out = run(version, changelog, gh)
        ok = code == want_code and out == want_out
        failed += not ok
        detail = "" if ok else f" (exit {code}, stdout {out!r})"
        print(f"{'ok' if ok else 'FAIL'}: {name}{detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
