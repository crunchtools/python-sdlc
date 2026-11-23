#!/usr/bin/env python3
"""`sdlc event`: the one command every job of the fleet's ci.yml runs.

It routes on GITHUB_JOB to sdlc_jobs.<job>.run, so the rules that keep a job
from passing by accident are pinned here:

  - no job, no event name or an unreadable payload is a 2, never a no-op
  - a job with no module yet is a 2 naming the module
  - a helper (a name starting with `_`) is never a job
  - `write` with DIGEST set is sdlc's rollback dispatch and does nothing
  - a job's CannotRunError and any return outside 0-3 are a 2

Run: python3 tests/test_event.py
"""

from __future__ import annotations

import contextlib
import email.message
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_SPEC = importlib.util.spec_from_loader(
    "sdlc_under_test", importlib.machinery.SourceFileLoader("sdlc_under_test", str(ROOT / "sdlc"))
)
assert _SPEC is not None and _SPEC.loader is not None
S = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(S)

core = importlib.import_module("sdlc_jobs._core")

FAILED: list[str] = []
CALLS: list[tuple[str, dict]] = []
SEEN: list[tuple[object, object]] = []  # the (env, tools) each call got


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def fake_job(name: str, result: object) -> None:
    """Register sdlc_jobs.<name> whose run records its call and returns or raises `result`."""

    def run(event_name: str, event: dict, env: object, tools: object) -> object:
        CALLS.append((event_name, event))
        SEEN.append((env, tools))
        if isinstance(result, Exception):
            raise result
        return result

    module = types.ModuleType(f"sdlc_jobs.{name}")
    vars(module)["run"] = run
    sys.modules[f"sdlc_jobs.{name}"] = module


def event(job: str | None, env: dict[str, str], payload: object = None) -> tuple[int | str, str]:
    """Run t_event under env; (exit code or the CannotRun message, stdout)."""
    with tempfile.TemporaryDirectory() as tmp:
        full = {"GITHUB_EVENT_NAME": "push", **env}
        if payload is not None:
            path = Path(tmp) / "event.json"
            path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
            full["GITHUB_EVENT_PATH"] = str(path)
        saved = dict(os.environ)
        for key in ("GITHUB_JOB", "GITHUB_EVENT_NAME", "GITHUB_EVENT_PATH", "DIGEST"):
            os.environ.pop(key, None)
        os.environ.update(full)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                return S.t_event(job), out.getvalue()
        except S.CannotRunError as exc:
            return str(exc), out.getvalue()
        finally:
            os.environ.clear()
            os.environ.update(saved)


def test_refuses_without_a_job() -> None:
    rc, _ = event(None, {})
    check("no GITHUB_JOB is a 2", isinstance(rc, str) and "no job" in rc, str(rc))
    rc, _ = event("_core", {})
    check("a helper is never a job", isinstance(rc, str) and "no job" in rc, str(rc))
    rc, _ = event("Check", {})
    check("a job name is lower case", isinstance(rc, str), str(rc))


def test_refuses_without_an_event() -> None:
    fake_job("ev_ok", 0)
    rc, _ = event("ev_ok", {"GITHUB_EVENT_NAME": ""})
    check("no event name is a 2", isinstance(rc, str) and "GITHUB_EVENT_NAME" in rc, str(rc))
    rc, _ = event("ev_ok", {}, payload="{not json")
    check("a payload that does not load is a 2", isinstance(rc, str) and "load" in rc, str(rc))
    rc, _ = event("ev_ok", {}, payload=[1])
    check("a payload that is not an object is a 2", isinstance(rc, str), str(rc))


def test_missing_module_names_it() -> None:
    rc, _ = event("nosuchjob", {})
    check(
        "a job with no module is a 2 naming it",
        isinstance(rc, str) and "sdlc_jobs/nosuchjob.py" in rc,
        str(rc),
    )


def test_routes_on_github_job() -> None:
    fake_job("ev_found", 1)
    CALLS.clear()
    SEEN.clear()
    rc, _ = event(None, {"GITHUB_JOB": "ev_found", "GITHUB_EVENT_NAME": "issues"}, {"n": 7})
    check("GITHUB_JOB picks the module and its verdict is the exit", rc == 1, str(rc))
    check("the job gets the event name and payload", CALLS == [("issues", {"n": 7})], str(CALLS))
    (env, tools), *_ = SEEN
    check("the job gets the process environment", env is os.environ, str(env))
    check("and the real Tools", type(tools) is core.Tools, str(tools))
    CALLS.clear()
    rc, _ = event("ev_found", {"GITHUB_JOB": "other"})
    check("an argument overrides GITHUB_JOB", rc == 1 and len(CALLS) == 1, str(rc))


def test_write_with_digest_does_nothing() -> None:
    fake_job("write", 0)
    CALLS.clear()
    rc, out = event(None, {"GITHUB_JOB": "write", "DIGEST": "sha256:" + "a" * 64})
    check("write with DIGEST exits 0", rc == 0, str(rc))
    check("write with DIGEST never runs the job", CALLS == [], str(CALLS))
    check("and says why", "rollback" in out, out)
    rc, _ = event(None, {"GITHUB_JOB": "write"})
    check("write without DIGEST runs", rc == 0 and len(CALLS) == 1, str(CALLS))


def test_job_failures_are_a_2() -> None:
    fake_job("ev_cannot", core.CannotRunError("registry refused"))
    rc, _ = event("ev_cannot", {})
    check("a job's CannotRunError is a 2 with its message", rc == "registry refused", str(rc))
    fake_job("ev_seven", 7)
    rc, _ = event("ev_seven", {})
    check("a return outside 0-3 is a 2", isinstance(rc, str) and "7" in rc, str(rc))
    fake_job("ev_none", None)
    rc, _ = event("ev_none", {})
    check("a job that returns nothing is a 2", isinstance(rc, str), str(rc))


class FakeReply(io.BytesIO):
    """What urlopen yields: a context manager with a status and a body."""

    status = 200


def http(url: str, urlopen: object) -> object:
    """Tools().http(GET, url) with urlopen replaced; the Response or the CannotRun message."""
    with mock.patch.object(core.urllib.request, "urlopen", urlopen):
        try:
            return core.Tools().http("GET", url, {"Accept": "x"})
        except core.CannotRunError as exc:
            return str(exc)


def test_http_returns_a_https_response() -> None:
    seen: list[tuple[str, str, float]] = []

    def urlopen(req: urllib.request.Request, timeout: float) -> FakeReply:
        seen.append((req.full_url, req.get_method(), timeout))
        return FakeReply(b'{"ok": true}')

    got = http("https://api.example/x", urlopen)
    check("a 200 is a Response with its body", got == core.Response(200, b'{"ok": true}'), str(got))
    check("sent as the method, with a timeout", seen == [("https://api.example/x", "GET", 60)])


def test_http_error_status_is_a_response() -> None:
    def urlopen(*_: object, **__: object) -> FakeReply:
        raise urllib.error.HTTPError(
            "https://api.example/x", 404, "nf", email.message.Message(), io.BytesIO(b"{}")
        )

    got = http("https://api.example/x", urlopen)
    check("an HTTPError is the Response of its status", got == core.Response(404, b"{}"), str(got))


def test_http_network_failure_is_a_2() -> None:
    for exc in (urllib.error.URLError("no route"), TimeoutError("slow"), OSError("reset")):

        def urlopen(*_: object, exc: Exception = exc, **__: object) -> FakeReply:
            raise exc

        got = http("https://api.example/x", urlopen)
        check(
            f"{type(exc).__name__} is a 2 naming the request",
            isinstance(got, str) and "GET https://api.example/x" in got,
            str(got),
        )


def test_http_refuses_a_non_https_url() -> None:
    def urlopen(*_: object, **__: object) -> FakeReply:
        raise AssertionError("opened a non-https URL")

    for url in ("http://api.example/x", "file:///etc/passwd", "ftp://api.example/x"):
        got = http(url, urlopen)
        check(f"{url} is refused", isinstance(got, str) and "non-https" in got, str(got))


def main() -> int:
    for test in (
        test_refuses_without_a_job,
        test_refuses_without_an_event,
        test_missing_module_names_it,
        test_routes_on_github_job,
        test_write_with_digest_does_nothing,
        test_job_failures_are_a_2,
        test_http_returns_a_https_response,
        test_http_error_status_is_a_response,
        test_http_network_failure_is_a_2,
        test_http_refuses_a_non_https_url,
    ):
        test()
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
