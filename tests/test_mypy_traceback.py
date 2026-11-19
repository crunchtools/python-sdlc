#!/usr/bin/env python3
"""A mypy crash must leave its traceback in the log, not a request for one.

mcp-trove#132's mypy hit an INTERNAL ERROR on one runner pool only. Without
--show-traceback mypy prints "please use --show-traceback" and exits 2, so the
log said COULD NOT RUN and nothing about why, and a rerun elsewhere passed.
This fakes that crash with a stand-in `uv` and pins both halves: the flag is on
the invocation, and what mypy prints reaches the log.

Run: tests/test_mypy_traceback.py   (needs the image's python3 and PyYAML)
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
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

# A stand-in `uv` that records its argv and crashes the way mypy does: the
# traceback only when asked for it, and exit 2 either way.
FAKE_UV = """\
#!/bin/sh
printf '%s\\n' "$@" > "$ARGV_FILE"
echo 'src/x.py:1: error: INTERNAL ERROR -- Please try using mypy master on GitHub:' >&2
case " $* " in
*" --show-traceback "*)
    echo 'Traceback (most recent call last):'
    echo '  File "mypy/checker.py", line 1, in accept'
    echo 'AssertionError: crash-cause-sentinel'
    ;;
*)
    echo 'src/x.py:1: note: please use --show-traceback to print a traceback' >&2
    ;;
esac
exit 2
"""


def check(ok: bool, what: str) -> bool:
    print(f"{'ok  ' if ok else 'FAIL'} {what}")
    return ok


def crash(tmp: Path) -> tuple[int, list[str], str]:
    """Run t_mypy against the fake uv; return its status, uv's argv and the log."""
    bindir = tmp / "bin"
    bindir.mkdir()
    uv = bindir / "uv"
    uv.write_text(FAKE_UV, encoding="utf-8")
    uv.chmod(0o755)
    argv_file = tmp / "argv"
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "ARGV_FILE": str(argv_file),
    }
    _SDLC.need_project = lambda: None
    _SDLC.need_lock_dep = lambda _name: None
    _SDLC.project_env = lambda: dict(env)
    _SDLC.sync_project = lambda _env: None
    _SDLC.project_dir = lambda: tmp
    _SDLC.project_name = lambda: "fake"

    # run() hands mypy this process's own stdout and stderr, which is how its
    # output reaches the CI log; capture at the descriptor to see exactly that.
    log = tmp / "log"
    sys.stdout.flush()
    sys.stderr.flush()
    saved = os.dup(1), os.dup(2)
    with log.open("wb") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        try:
            rc = _SDLC.t_mypy({})
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(saved[0], 1)
            os.dup2(saved[1], 2)
            os.close(saved[0])
            os.close(saved[1])
    return rc, argv_file.read_text(encoding="utf-8").splitlines(), log.read_text("utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        rc, argv, log = crash(Path(tmp))
    ok = check(rc == _SDLC.CANNOT_RUN, "a mypy crash is mypy's own exit 2, COULD NOT RUN")
    ok &= check("--show-traceback" in argv, "mypy is invoked with --show-traceback")
    ok &= check("INTERNAL ERROR" in log, "the INTERNAL ERROR line reaches the log")
    ok &= check(
        "Traceback (most recent call last):" in log and "crash-cause-sentinel" in log,
        "the traceback naming the crash's cause reaches the log",
    )
    ok &= check("please use --show-traceback" not in log, "no request for a traceback instead")
    print("mypy traceback PASSED" if ok else "mypy traceback FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
