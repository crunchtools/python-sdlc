#!/usr/bin/env python3
"""A CodeQL that cannot run must not be what teaches people `--no-verify`.

python-sdlc#33: on josui-skills a killed run left a half-built database that a
person had to delete by hand, the evaluator was sized by CodeQL's own guess
rather than the container's budget, and an analysis that died blocked every
push with no door but `--no-verify` -- which skips every gate, not the broken
one. These are the three pieces of that fix that need no CodeQL to test.

Run: tests/test_codeql_resilience.py   (needs the image's python3 and PyYAML)
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import stat
import sys
import tempfile
from collections.abc import Callable
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


def raises(exc: type[BaseException], fn: Callable[[], object]) -> bool:
    """True when `fn` raises `exc`. Anything else it raises fails the test run."""
    try:
        fn()
    except exc:
        return True
    return False


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


def deferral() -> bool:
    """Deferring to CI is a push-only, outside-CI privilege."""
    ok = True
    os.environ.pop("CI", None)
    os.environ["SDLC_TIER"] = "ci"
    ok &= check(not _SDLC.ci_judges(), "`sdlc ci` never defers: nothing judges after it")
    os.environ["SDLC_TIER"] = "codeql"
    ok &= check(not _SDLC.ci_judges(), "the inner loop's `sdlc codeql` never defers")
    os.environ["SDLC_TIER"] = "push"
    os.environ["CI"] = "true"
    ok &= check(not _SDLC.ci_judges(), "a push inside CI never defers")
    ok &= check(
        raises(_SDLC.CannotRunError, lambda: _SDLC.codeql_broke("x")),
        "where CI does not judge, a broken CodeQL is still a 2",
    )
    return ok


def main() -> int:
    saved = dict(os.environ)
    try:
        ok = database_and_lock()
        ok &= memory()
        ok &= deferral()
    finally:
        os.environ.clear()
        os.environ.update(saved)
    print("codeql resilience PASSED" if ok else "codeql resilience FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
