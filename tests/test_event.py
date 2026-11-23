#!/usr/bin/env python3
"""`sdlc event`: the one command every job of the fleet's ci.yml runs.

It routes on GITHUB_JOB to sdlc_jobs.<job>.main, so the rules that keep a job
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
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_SPEC = importlib.util.spec_from_loader(
    "sdlc_under_test", importlib.machinery.SourceFileLoader("sdlc_under_test", str(ROOT / "sdlc"))
)
assert _SPEC is not None and _SPEC.loader is not None
S = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(S)

import sdlc_jobs._core as core  # noqa: E402

FAILED: list[str] = []
CALLS: list[tuple[str, dict]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def fake_job(name: str, result: object) -> None:
    """Register sdlc_jobs.<name> whose main records its call and returns or raises `result`."""

    def main(event_name: str, event: dict, env: object) -> object:  # noqa: ARG001
        CALLS.append((event_name, event))
        if isinstance(result, Exception):
            raise result
        return result

    module = types.ModuleType(f"sdlc_jobs.{name}")
    module.main = main  # type: ignore[attr-defined]
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
    rc, _ = event(None, {"GITHUB_JOB": "ev_found", "GITHUB_EVENT_NAME": "issues"}, {"n": 7})
    check("GITHUB_JOB picks the module and its verdict is the exit", rc == 1, str(rc))
    check("the job gets the event name and payload", CALLS == [("issues", {"n": 7})], str(CALLS))
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


def main() -> int:
    for test in (
        test_refuses_without_a_job,
        test_refuses_without_an_event,
        test_missing_module_names_it,
        test_routes_on_github_job,
        test_write_with_digest_does_nothing,
        test_job_failures_are_a_2,
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
