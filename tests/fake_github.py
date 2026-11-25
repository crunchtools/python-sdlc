"""Stand-ins for GitHub and for subprocesses, shared by the sdlc_jobs suites.

Not a suite itself (no `test_` prefix): the suites import it. Nothing here
touches the network or runs a real tool.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import sdlc_root

from sdlc_jobs._github import JSON, ApiError
from sdlc_jobs._tools import Done

# Where a suite that needs a directory but never writes to it can point a Site.
WORK = sdlc_root.ROOT / "tests"

Route = Any  # a value to return, or a callable(body) -> value, or an ApiError to raise


class FakeGitHub:
    """Answers (method, path) from a table and records every call.

    A route's path may carry `*` wildcards and is matched without the query
    string unless the route itself has one. A call with no route is an
    ApiError 404, so a job that asks for something unexpected fails loudly.
    """

    def __init__(self, routes: Mapping[tuple[str, str], Route] | None = None) -> None:
        self.routes: dict[tuple[str, str], Route] = dict(routes or {})
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, body: Any = None, *, accept: str = JSON) -> Any:
        del accept  # every fake answer is already in the shape the caller asked for
        self.calls.append((method, path, body))
        bare = path.split("?", 1)[0]
        for (known, pattern), found in self.routes.items():
            target = path if "?" in pattern else bare
            if known == method and fnmatch.fnmatchcase(target, pattern):
                value = found
                break
        else:
            raise ApiError(method, path, 404, "no fake route")
        if isinstance(value, ApiError):
            raise value
        return value(body) if callable(value) else value

    def pages(self, path: str, key: str | None = None) -> list[Any]:
        body = self.request("GET", path)
        return list(body[key] if key else body)

    def made(self, method: str, pattern: str = "*") -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] == method and fnmatch.fnmatchcase(c[1], pattern)]


class FakeRunner:
    """Answers subprocesses by the first rule whose every string occurs in the
    command line, and records them. Anything unmatched exits 0 silently."""

    def __init__(self, answers: Sequence[tuple[Sequence[str], Done | Callable[..., Done]]] = ()):
        self.answers = list(answers)
        self.calls: list[tuple[list[str], dict[str, str], Path | None, str | None]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path | None = None,
        stdin: str | None = None,
        timeout: float | None = None,
    ) -> Done:
        del timeout
        args = list(argv)
        self.calls.append((args, dict(env), cwd, stdin))
        for prefix, answer in self.answers:
            if all(part in " ".join(args) for part in prefix):
                return answer(args, env, cwd, stdin) if callable(answer) else answer
        return Done(0, "", "")

    def ran(self, needle: str) -> list[list[str]]:
        return [c[0] for c in self.calls if needle in " ".join(c[0])]


FAILED: list[str] = []


def carries_credential(env: Mapping[str, str]) -> bool:
    """Does any git config in `env` set an extraheader or carry an authorization header?

    Header names are case-insensitive, so the match is too.
    """
    keys = (v for k, v in env.items() if k.startswith("GIT_CONFIG_KEY_"))
    values = (v for k, v in env.items() if k.startswith("GIT_CONFIG_VALUE_"))
    return any("extraheader" in k.lower() for k in keys) or any(
        "authorization" in v.lower() for v in values
    )


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def finish() -> int:
    if FAILED:
        print(f"\n{len(FAILED)} failed: {', '.join(FAILED)}")
        return 1
    print("\nall passed")
    return 0
