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
import inspect
import io
import json
import os
import sys
import tempfile
import types
import urllib.error
import urllib.request
import urllib.response
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
github = importlib.import_module("sdlc_jobs._github")

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


def test_a_main_job_is_called_as_main() -> None:
    """ai, write and gate expose main(event_name, event, env); ai has a `run` too."""

    def main(event_name: str, event: dict, env: object) -> int:
        CALLS.append((event_name, event))
        SEEN.append((env, None))
        return 1

    def run(*_: object) -> int:
        raise AssertionError("run was called on a job that has main")

    module = types.ModuleType("sdlc_jobs.ev_main")
    vars(module).update(main=main, run=run)
    sys.modules["sdlc_jobs.ev_main"] = module
    CALLS.clear()
    SEEN.clear()
    rc, _ = event(None, {"GITHUB_JOB": "ev_main", "GITHUB_EVENT_NAME": "push"}, {"n": 1})
    check("main is called, not run, and its verdict is the exit", rc == 1, str(rc))
    check("main gets the event and the environment", CALLS == [("push", {"n": 1})], str(CALLS))
    check("main gets no Tools", SEEN[0][0] is os.environ, str(SEEN))


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


def test_an_unexpected_exception_is_a_2() -> None:
    fake_job("ev_bug", KeyError("name"))
    rc, _ = event("ev_bug", {})
    check(
        "a job raising KeyError is a 2 naming the job and class",
        isinstance(rc, str) and "ev_bug" in rc and "KeyError" in rc,
        str(rc),
    )


class FakeReply(io.BytesIO):
    """What the opener's open yields: a context manager with a status and a body."""

    status = 200


def http(url: str, opener_open: object) -> object:
    """Tools().http(GET, url) with the opener's open replaced; Response or CannotRun message."""
    with mock.patch.object(core, "SAFE_REDIRECTS", mock.Mock(open=opener_open)):
        try:
            return core.Tools().http("GET", url, {"Accept": "x"})
        except core.CannotRunError as exc:
            return str(exc)


def test_http_returns_a_https_response() -> None:
    seen: list[tuple[str, str, float]] = []

    def opener_open(req: urllib.request.Request, timeout: float) -> FakeReply:
        seen.append((req.full_url, req.get_method(), timeout))
        return FakeReply(b'{"ok": true}')

    got = http("https://api.example/x", opener_open)
    check("a 200 is a Response with its body", got == core.Response(200, b'{"ok": true}'), str(got))
    check("sent as the method, with a timeout", seen == [("https://api.example/x", "GET", 60)])


def test_http_error_status_is_a_response() -> None:
    def opener_open(*_: object, **__: object) -> FakeReply:
        raise urllib.error.HTTPError(
            "https://api.example/x", 404, "nf", email.message.Message(), io.BytesIO(b"{}")
        )

    got = http("https://api.example/x", opener_open)
    check("an HTTPError is the Response of its status", got == core.Response(404, b"{}"), str(got))


def test_http_network_failure_is_a_2() -> None:
    for exc in (urllib.error.URLError("no route"), TimeoutError("slow"), OSError("reset")):

        def opener_open(*_: object, exc: Exception = exc, **__: object) -> FakeReply:
            raise exc

        got = http("https://api.example/x", opener_open)
        check(
            f"{type(exc).__name__} is a 2 naming the request",
            isinstance(got, str) and "GET https://api.example/x" in got,
            str(got),
        )


class StubHttps(urllib.request.HTTPSHandler):
    """The network as {URL: (status, Location, body)}; remembers each request."""

    def __init__(self, answers: dict[str, tuple[int, str, bytes]]) -> None:
        super().__init__()
        self.answers = answers
        self.seen: list[urllib.request.Request] = []

    def https_open(self, req: urllib.request.Request) -> urllib.response.addinfourl:
        self.seen.append(req)
        status, location, body = self.answers[req.full_url]
        hdrs = email.message.Message()
        if location:
            hdrs["Location"] = location
        resp = urllib.response.addinfourl(io.BytesIO(body), hdrs, req.full_url, status)
        resp.msg = "stub"  # type: ignore[attr-defined]
        return resp


REDIRECT_CODES = (301, 302, 303, 307, 308)
START = "https://api.example/x"
CREDENTIALS = {"Authorization": "Bearer t", "Cookie": "s=1", "X-Api-Key": "k"}


def through_redirect(
    code: int, location: str, target: str
) -> tuple[object, list[urllib.request.Request]]:
    """Tools().http with credentials, through the real handler, to a URL answering `code`."""
    stub = StubHttps({START: (code, location, b"moved"), target: (200, "", b"landed")})
    opener = urllib.request.build_opener(github.SafeRedirect, stub)
    with mock.patch.object(core, "SAFE_REDIRECTS", opener):
        try:
            got: object = core.Tools().http("GET", START, {"Accept": "x", **CREDENTIALS})
        except core.CannotRunError as exc:
            got = str(exc)
    return got, stub.seen


def seen_of(seen: list[urllib.request.Request]) -> str:
    return str([(r.full_url, r.header_items()) for r in seen])


def test_http_redirect_to_another_host_drops_the_credentials() -> None:
    target = "https://objects.example/asset"
    for code in REDIRECT_CODES:
        got, seen = through_redirect(code, target, target)
        check(f"a cross-host {code} lands on its body", got == core.Response(200, b"landed"))
        check(
            f"the cross-host {code} follow-up carries no credential header",
            [r.full_url for r in seen] == [START, target]
            and not any(seen[-1].has_header(h.capitalize()) for h in CREDENTIALS),
            seen_of(seen),
        )
        check(
            f"the cross-host {code} follow-up keeps Accept",
            seen[-1].get_header("Accept") == "x",
            seen_of(seen),
        )


def test_http_redirect_on_the_same_host_keeps_the_token() -> None:
    target = "https://api.example/y"
    for code in REDIRECT_CODES:
        got, seen = through_redirect(code, "/y", target)
        check(f"a same-host {code} lands on its body", got == core.Response(200, b"landed"))
        check(
            f"the same-host {code} follow-up keeps Authorization",
            [r.full_url for r in seen] == [START, target]
            and seen[-1].get_header("Authorization") == "Bearer t",
            seen_of(seen),
        )


def test_http_refuses_a_redirect_off_https() -> None:
    target = "http://api.example/y"
    for code in REDIRECT_CODES:
        got, seen = through_redirect(code, target, target)
        check(f"a {code} to http is the {code} itself", got == core.Response(code, b"moved"))
        check(f"no request follows a {code} to http", len(seen) == 1, seen_of(seen))
    uses = any(isinstance(h, github.SafeRedirect) for h in core.SAFE_REDIRECTS.handlers)
    check("Tools.http opens through that handler", uses)


def test_github_client_uses_the_safe_redirect() -> None:
    opener = inspect.signature(github.GitHub).parameters["opener"].default
    check("GitHub opens through SAFE_REDIRECTS", opener == github.SAFE_REDIRECTS.open, str(opener))


def test_http_refuses_a_non_https_url() -> None:
    def opener_open(*_: object, **__: object) -> FakeReply:
        raise AssertionError("opened a non-https URL")

    for url in ("http://api.example/x", "file:///etc/passwd", "ftp://api.example/x"):
        got = http(url, opener_open)
        check(f"{url} is refused", isinstance(got, str) and "non-https" in got, str(got))


def main() -> int:
    for test in (
        test_refuses_without_a_job,
        test_refuses_without_an_event,
        test_missing_module_names_it,
        test_routes_on_github_job,
        test_a_main_job_is_called_as_main,
        test_write_with_digest_does_nothing,
        test_job_failures_are_a_2,
        test_an_unexpected_exception_is_a_2,
        test_http_returns_a_https_response,
        test_http_error_status_is_a_response,
        test_http_network_failure_is_a_2,
        test_http_redirect_to_another_host_drops_the_token,
        test_http_redirect_on_the_same_host_keeps_the_token,
        test_http_refuses_a_redirect_off_https,
        test_github_client_uses_the_safe_redirect,
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
