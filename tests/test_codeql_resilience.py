#!/usr/bin/env python3
"""CodeQL must not fail for want of memory or for a half-built cache.

python-sdlc#33: on josui-skills a killed run left a half-built database that a
person had to delete by hand, and the evaluator was sized by CodeQL's own guess
rather than the container's budget, so analyze died and every push was refused.

Run: tests/test_codeql_resilience.py   (needs the image's python3 and PyYAML)
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import stat
import sys
import tempfile
from pathlib import Path

# Loaded by path, as in test_codeql_globs.py: `sdlc` has no .py extension.
_SPEC = importlib.util.spec_from_loader(
    "sdlc_under_test",
    importlib.machinery.SourceFileLoader(
        "sdlc_under_test", str(Path(__file__).resolve().parent.parent / "sdlc")
    ),
)
assert _SPEC is not None and _SPEC.loader is not None
_SDLC = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SDLC)

# CodeQL's own floor, and what an 8g budget should yield: three quarters of it.
FLOOR_MB = 2048
EIGHT_G_MB = 8192
FOUR_G_MB = 4096
EIGHT_G_RAM_MB = 6144


def check(ok: bool, what: str) -> bool:
    print(f"{'ok  ' if ok else 'FAIL'} {what}")
    return ok


def database_and_lock() -> bool:
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        # A killed run's leftovers: trap/ and working/, no db-python, and a
        # read-only directory that a plain rmtree cannot empty.
        db = Path(tmp) / "codeql" / "proj"
        (db / "trap" / "deep").mkdir(parents=True)
        (db / "working").mkdir()
        (db / "trap" / "deep" / "x.trap").write_text("x")
        os.chmod(db / "trap" / "deep", stat.S_IRUSR | stat.S_IXUSR)
        _SDLC.clear_database(db)
        ok &= check(not db.exists(), "a half-built database, read-only parts and all, is cleared")
        _SDLC.clear_database(db)
        ok &= check(not db.exists(), "clearing a database that is not there is not an error")

        # The lock is per project, in a directory that may not exist yet.
        _SDLC.lock_codeql(Path(tmp) / "fresh" / "proj")
        lock = Path(tmp) / "fresh" / "proj.lock"
        ok &= check(lock.is_file(), "the lock file is created beside the database")
    return ok


def memory() -> bool:
    ok = True
    os.environ["SDLC_MEMORY"] = "8g"
    limit = _SDLC.memory_limit_mb()
    ok &= check(
        limit is not None and limit <= EIGHT_G_MB, f"an 8g budget caps the limit ({limit} MB)"
    )
    os.environ["SDLC_MEMORY"] = "4096m"
    ok &= check(_SDLC.memory_limit_mb() <= FOUR_G_MB, "a budget in m is read as MB")
    os.environ["SDLC_MEMORY"] = "1g"
    ok &= check(_SDLC.codeql_ram_mb() == FLOOR_MB, "never below CodeQL's own 2048 MB floor")
    os.environ["SDLC_MEMORY"] = "8g"
    ram = _SDLC.codeql_ram_mb()
    ok &= check(
        FLOOR_MB <= ram <= EIGHT_G_RAM_MB, f"three quarters of 8g to the evaluator ({ram} MB)"
    )
    return ok


def failures() -> bool:
    """Either CodeQL step exiting non-zero is a 2 that names the step."""
    ok = True
    real_run, real_step = _SDLC.run, _SDLC.step
    _SDLC.step = lambda _msg: None
    try:
        for failing, says in (
            ("create", "could not build a database"),
            ("analyze", "analyze failed"),
        ):
            _SDLC.run = lambda cmd, failing=failing, **_kw: (7 if cmd[2] == failing else 0, "")
            try:
                _SDLC.run_codeql(
                    ["codeql", "database", "create"], ["codeql", "database", "analyze"], Path("/x")
                )
                raised = ""
            except _SDLC.CannotRunError as exc:
                raised = str(exc)
            ok &= check(
                says in raised and "exit 7" in raised, f"a failed {failing} is a 2 that says so"
            )
    finally:
        _SDLC.run, _SDLC.step = real_run, real_step
    return ok


def main() -> int:
    saved = dict(os.environ)
    try:
        ok = database_and_lock()
        ok &= memory()
        ok &= failures()
    finally:
        os.environ.clear()
        os.environ.update(saved)
    print("codeql resilience PASSED" if ok else "codeql resilience FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
