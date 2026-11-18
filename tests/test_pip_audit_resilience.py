#!/usr/bin/env python3
"""A pip-audit that never reached PyPI must say so: not a finding, not a pass.

On the self-hosted runners a busy host made pip-audit's 15s read timeout trip
against a PyPI that answers in under a second, and pip-audit exits 1 for that --
the status of a real vulnerability. The opposite also happened: lookups that all
came back "not found" exited 0 and read as a pass. These pin the fix: a network
failure is retried with backoff, and one that outlasts the retries is a 2.

Run: tests/test_pip_audit_resilience.py   (needs the image's python3 and PyYAML)
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
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

LOCK = """\
version = 1

[[package]]
name = "requests"
version = "2.32.3"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "urllib3"
version = "1.26.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "petit-plus"
version = "0.1.0"
source = { directory = "tests/fixtures/petit-plus" }
"""

LOCK_MIRROR = """\
version = 1

[[package]]
name = "requests"
version = "2.32.3"
source = { registry = "https://mirror.example/simple" }

[[package]]
name = "idna"
version = "3.10"
source = { registry = "https://mirror.example/simple" }
"""


def skip_table(*rows: tuple[str, str]) -> str:
    """pip-audit's skipped-dependency table, as ColumnsFormat lays it out."""
    lines = [
        (n, f"Dependency not found on PyPI and could not be audited: {n} ({v})") for n, v in rows
    ]
    width = max(len("Name"), *(len(n) for n, _ in lines))
    reason = max(len(r) for _, r in lines)
    return "".join(
        [
            f"{'Name'.ljust(width)} Skip Reason\n",
            f"{'-' * width} {'-' * reason}\n",
            *(f"{n.ljust(width)} {r}\n" for n, r in lines),
        ]
    )


# What pip-audit 2.9.0 prints for each case, stderr and stdout merged as tee=True
# captures them: the summary on stderr first, then the tables on stdout.
READ_TIMEOUT = (
    "Traceback (most recent call last):\n"
    '  File "/tmp/venv/lib/python3.14/site-packages/requests/adapters.py", line 713, in send\n'
    "urllib3.exceptions.ReadTimeoutError: HTTPSConnectionPool(host='pypi.org', port=443): "
    "Read timed out. (read timeout=15)\n"
    "\nDuring handling of the above exception, another exception occurred:\n\n"
    "requests.exceptions.ReadTimeout: HTTPSConnectionPool(host='pypi.org', port=443): "
    "Read timed out. (read timeout=15)\n"
)
CLEAN_RUN = "No known vulnerabilities found\n" + skip_table(("petit-plus", "0.1.0"))
VULNERABLE = (
    "Found 1 known vulnerability in 1 package\n"
    "Name     Version ID                  Fix Versions\n"
    "-------- ------- ------------------- ------------\n"
    "requests 2.32.3  GHSA-9hjg-9r4m-mvj7 2.32.4\n"
)
# A real finding beside a version PyPI no longer has, and beside uv's own noise.
VULNERABLE_AND_404 = VULNERABLE + skip_table(("urllib3", "1.26.0"))
VULNERABLE_AND_UV_NOISE = (
    "warning: Failed to fetch: `https://pypi.org/simple/pip-audit/`, retrying\n" + VULNERABLE
)
PROXY_404 = "No known vulnerabilities found\n" + skip_table(
    ("petit-plus", "0.1.0"), ("requests", "2.32.3")
)
MIRROR_ALL_SKIPPED = "No known vulnerabilities found\n" + skip_table(
    ("idna", "3.10"), ("requests", "2.32.3")
)
MIRROR_ONE_SKIPPED = "No known vulnerabilities found\n" + skip_table(("idna", "3.10"))

# The other ways the network fails, one representative line each: pip-audit's own
# connect-timeout message, the requests exceptions it lets escape, and uv failing
# to fetch pip-audit itself.
OTHER_NETWORK = {
    "connect timeout": (
        "ERROR:pip_audit._cli:Could not connect to PyPI's vulnerability feed\n"
        "ERROR:pip_audit._cli:Tip: your network may be blocking this service. "
        "Try another service with `-s SERVICE`\n"
    ),
    "redirect loop": "ERROR:pip_audit._cli:PyPI is not redirecting properly\n",
    "connection reset": "requests.exceptions.ConnectionError: ('Connection aborted.')\n",
    "proxy": "requests.exceptions.ProxyError: HTTPSConnectionPool(host='pypi.org')\n",
    "tls": "requests.exceptions.SSLError: HTTPSConnectionPool(host='pypi.org')\n",
    "5xx": "requests.exceptions.HTTPError: 503 Server Error: Service Unavailable\n",
    "retries": "HTTPSConnectionPool(host='pypi.org', port=443): Max retries exceeded\n",
    "uv fetch": "error: Failed to fetch: `https://pypi.org/simple/pip-audit/`\n",
    "uv request": "Caused by: error sending request for url (https://pypi.org/simple/)\n",
    "connect timeout, raw": "requests.exceptions.ConnectTimeout: HTTPSConnectionPool\n",
}

# pip-audit's own --timeout default, which is pip's.
PIP_AUDIT_DEFAULT_TIMEOUT = 15
ONE_RETRY = 2


def check(ok: bool, what: str) -> bool:
    print(f"{'ok  ' if ok else 'FAIL'} {what}")
    return ok


class Harness:
    """Stand in for uv, the project and the clock; replay pip-audit's answers."""

    def __init__(self, project: Path, answers: list[tuple[int, str]]) -> None:
        self.answers = list(answers)
        self.calls: list[list[str]] = []
        self.slept: list[float] = []
        _SDLC.need_project = lambda: None
        venv = project / "venv"
        site = venv / "lib" / "python3.14" / "site-packages"
        for dist in ("requests-2.32.3", "idna-3.10", "petit_plus-0.1.0"):
            (site / f"{dist}.dist-info").mkdir(parents=True, exist_ok=True)
        _SDLC.project_env = lambda: {"UV_PROJECT_ENVIRONMENT": str(venv)}
        _SDLC.sync_project = lambda _env: None
        _SDLC.project_dir = lambda: project
        _SDLC.run = self.run
        _SDLC.time.sleep = self.slept.append
        _SDLC.BAKED["pip-audit"] = "2.9.0"

    def run(self, cmd: list[str], **_: object) -> tuple[int, str]:
        self.calls.append(cmd)
        # The last answer repeats, so an extra call shows up as a count, not a crash.
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def outcome(fn: Callable[[], int]) -> int | str:
    """The status `fn` returns, or "cannot" when it raised CannotRunError."""
    try:
        return fn()
    except _SDLC.CannotRunError:
        return "cannot"


def cases() -> bool:
    ok = True
    real_sleep = _SDLC.time.sleep
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        (project / "uv.lock").write_text(LOCK, encoding="utf-8")
        try:
            h = Harness(project, [(1, READ_TIMEOUT), (0, CLEAN_RUN)])
            got = outcome(lambda: _SDLC.t_pip_audit({}))
            ok &= check(got == 0, "a read timeout, then a clean run: retried, and a pass")
            ok &= check(len(h.calls) == ONE_RETRY, "exactly one retry after one timeout")
            ok &= check(h.slept == [_SDLC.PIP_AUDIT_BACKOFF[0]], "it backed off before retrying")
            ok &= check(
                h.calls[0][-4:-2] == ["--timeout", str(_SDLC.PIP_AUDIT_TIMEOUT)]
                and _SDLC.PIP_AUDIT_TIMEOUT > PIP_AUDIT_DEFAULT_TIMEOUT,
                "pip-audit runs with a longer timeout than its 15s default",
            )
            ok &= check(
                "pip-audit==2.9.0" in h.calls[0],
                "pip-audit is pinned to the version the image bakes",
            )

            attempts = len(_SDLC.PIP_AUDIT_BACKOFF) + 1
            h = Harness(project, [(1, READ_TIMEOUT)] * attempts)
            got = outcome(lambda: _SDLC.t_pip_audit({}))
            ok &= check(got == "cannot", "a timeout on every attempt is COULD NOT RUN, not FOUND")
            ok &= check(len(h.calls) == attempts, f"it gave up after {attempts} attempts")
            ok &= check(h.slept == list(_SDLC.PIP_AUDIT_BACKOFF), "the backoff grows")

            for what, text in OTHER_NETWORK.items():
                h = Harness(project, [(1, text)] * attempts)
                got = outcome(lambda: _SDLC.t_pip_audit({}))
                ok &= check(
                    got == "cannot" and len(h.calls) == attempts,
                    f"{what}: retried, then COULD NOT RUN",
                )

            h = Harness(project, [(0, PROXY_404)] * attempts)
            got = outcome(lambda: _SDLC.t_pip_audit({}))
            ok &= check(
                got == "cannot",
                "'not found' for a package the lock takes from PyPI is not a pass",
            )

            h = Harness(project, [(0, CLEAN_RUN)])
            got = outcome(lambda: _SDLC.t_pip_audit({}))
            ok &= check(
                got == 0 and len(h.calls) == 1,
                "a local path dependency skipped as not found is still a pass, first time",
            )

            h = Harness(project, [(1, VULNERABLE)])
            got = outcome(lambda: _SDLC.t_pip_audit({}))
            ok &= check(
                got == 1 and len(h.calls) == 1 and not h.slept,
                "a real vulnerability is a 1, at once, with no retry",
            )

            for what, text in {
                "a version PyPI no longer has": VULNERABLE_AND_404,
                "uv's own network noise": VULNERABLE_AND_UV_NOISE,
            }.items():
                h = Harness(project, [(1, text)])
                got = outcome(lambda: _SDLC.t_pip_audit({}))
                ok &= check(
                    got == 1 and len(h.calls) == 1 and not h.slept,
                    f"a real finding beside {what} is a 1, with no retry",
                )

            (project / "uv.lock").write_text(LOCK_MIRROR, encoding="utf-8")
            h = Harness(project, [(0, MIRROR_ALL_SKIPPED)] * attempts)
            got = outcome(lambda: _SDLC.t_pip_audit({}))
            ok &= check(
                got == "cannot" and len(h.calls) == attempts,
                "behind a mirror, every registry package skipped is a 2, not a pass",
            )
            h = Harness(project, [(0, MIRROR_ONE_SKIPPED)])
            got = outcome(lambda: _SDLC.t_pip_audit({}))
            ok &= check(
                got == 0 and len(h.calls) == 1,
                "behind a mirror, one package skipped while others were audited is a pass",
            )
        finally:
            _SDLC.time.sleep = real_sleep
    return ok


def bad_lock() -> bool:
    """A lock that cannot be read is a 2 before pip-audit runs, never a guess."""
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        h = Harness(project, [])
        ok &= check(outcome(lambda: _SDLC.t_pip_audit({})) == "cannot", "no uv.lock is a 2")
        (project / "uv.lock").write_text("[[package]\nname = ", encoding="utf-8")
        ok &= check(
            outcome(lambda: _SDLC.t_pip_audit({})) == "cannot" and not h.calls,
            "a malformed uv.lock is a 2, and pip-audit never ran",
        )
    return ok


def unpinned() -> bool:
    """With no baked version there is nothing to pin, and that is a 2."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        (project / "uv.lock").write_text(LOCK, encoding="utf-8")
        h = Harness(project, [])
        _SDLC.BAKED["pip-audit"] = "unknown"
        got = outcome(lambda: _SDLC.t_pip_audit({}))
        return check(got == "cannot" and not h.calls, "no baked pip-audit version is a 2")


def main() -> int:
    ok = cases()
    ok &= bad_lock()
    ok &= unpinned()
    print("pip-audit resilience PASSED" if ok else "pip-audit resilience FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
