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
name = "petit-plus"
version = "0.1.0"
source = { directory = "tests/fixtures/petit-plus" }
"""

# What pip-audit 2.9.0 actually prints for each case.
READ_TIMEOUT = (
    "Traceback (most recent call last):\n"
    "urllib3.exceptions.ReadTimeoutError: HTTPSConnectionPool(host='pypi.org', port=443): "
    "Read timed out. (read timeout=15)\n"
    "requests.exceptions.ReadTimeout: HTTPSConnectionPool(host='pypi.org', port=443): "
    "Read timed out. (read timeout=15)\n"
)
CLEAN_RUN = "No known vulnerabilities found, 1 dependency was skipped\n" + (
    "Name       Skip Reason\n"
    "---------- -----------\n"
    "petit-plus Dependency not found on PyPI and could not be audited: petit-plus (0.1.0)\n"
)
VULNERABLE = (
    "Found 1 known vulnerability in 1 package\n"
    "Name     Version ID                  Fix Versions\n"
    "requests 2.32.3  GHSA-xxxx-xxxx-xxxx 2.32.4\n"
)
# The other ways the network fails, one representative line each: pip-audit's own
# connect-timeout message, the requests exceptions it lets escape, and uv failing
# to fetch pip-audit itself.
OTHER_NETWORK = {
    "connect timeout": "ERROR:pip_audit._cli:Could not connect to PyPI's vulnerability feed\n",
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

PROXY_404 = "No known vulnerabilities found, 1 dependency was skipped\n" + (
    "requests Dependency not found on PyPI and could not be audited: requests (2.32.3)\n"
)


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
        _SDLC.project_env = dict
        _SDLC.sync_project = lambda _env: None
        _SDLC.project_dir = lambda: project
        _SDLC.run = self.run
        _SDLC.time.sleep = self.slept.append

    def run(self, cmd: list[str], **_: object) -> tuple[int, str]:
        self.calls.append(cmd)
        return self.answers.pop(0)


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
        finally:
            _SDLC.time.sleep = real_sleep
    return ok


def main() -> int:
    ok = cases()
    print("pip-audit resilience PASSED" if ok else "pip-audit resilience FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
