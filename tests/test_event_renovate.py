#!/usr/bin/env python3
"""sdlc_jobs/renovate.py, against a fake GitHub, a fake git and a fake Renovate.

`renovate` holds the Renovate App's key and an org-wide token, so:

  - it runs in crunchtools/constitution only, and only on the weekly cron
    (the real run) or a dispatch from the default branch (a dry run)
  - without the key it is a 2, never a pass
  - a dry run mints a read-only token and sets RENOVATE_DRY_RUN; the real
    run mints the write set and keeps the preset's window
  - Renovate's environment carries the minted token and nothing else the job
    holds: not the App key, not a GITHUB_TOKEN
  - the token is released whether Renovate passed or not, and a failed run
    is a 2 that still writes its summary
  - org_token mints for the installation's every repo, from the org's
    installation, and revokes what it minted

Run: python3 tests/test_event_renovate.py
"""

from __future__ import annotations

import atexit
import contextlib
import io
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import sdlc_root
from fake_github import FakeGitHub, FakeRunner, check, finish

from sdlc_jobs import renovate
from sdlc_jobs._app_token import org_token
from sdlc_jobs._github import CannotRunError
from sdlc_jobs._tools import Site

HOME = "crunchtools/constitution"
SHA = "c" * 40
DEFAULT = {"repository": {"default_branch": "main", "full_name": HOME}}
TMP = Path(tempfile.mkdtemp(prefix="sdlc-renovate-test-"))
JOB_FILE = sdlc_root.ROOT / "sdlc_jobs" / "renovate.py"
atexit.register(shutil.rmtree, TMP, ignore_errors=True)


def quiet(fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kw)


def refusal(fn: Callable[..., Any], *args: Any) -> str | None:
    try:
        quiet(fn, *args)
    except CannotRunError as err:
        return str(err)
    return None


# ------------------------------------------------------------- which runs

weekly = {**DEFAULT, "schedule": renovate.WEEKLY}
on_main = {"GITHUB_REF": "refs/heads/main"}
check("the weekly cron is the real run", renovate.is_real("schedule", weekly, {}) is True)
check(
    "a dispatch from the default branch is a dry run",
    renovate.is_real("workflow_dispatch", DEFAULT, on_main) is False,
)
check(
    "the 6-hourly poll is refused",
    "weekly"
    in (refusal(renovate.is_real, "schedule", {**DEFAULT, "schedule": "47 */6 * * *"}, {}) or ""),
)
check(
    "a dispatch from another branch is refused",
    refusal(renovate.is_real, "workflow_dispatch", DEFAULT, {"GITHUB_REF": "refs/heads/x"})
    is not None,
)
for name in ("push", "pull_request", "pull_request_target", "issues"):
    check(f"{name} is refused", refusal(renovate.is_real, name, DEFAULT, on_main) is not None)

# main: through the contract, a refusal is a 2.
base_env = {"GITHUB_REPOSITORY": HOME, "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": SHA}
check(
    "another repo is a 2",
    quiet(
        renovate.main,
        "schedule",
        weekly,
        {**base_env, "GITHUB_REPOSITORY": "crunchtools/x", "RENOVATE_APP_KEY": "k"},
    )
    == 2,
)
check("no App key is a 2", quiet(renovate.main, "schedule", weekly, base_env) == 2)


# ------------------------------------------------------------------ a run


class Minter:
    """Records the permissions asked for and each release; yields (token, slug)."""

    def __init__(self) -> None:
        self.perms: list[dict[str, str]] = []
        self.released = 0

    def __call__(self, perms: Mapping[str, str]) -> Minter:
        self.perms.append(dict(perms))
        return self

    def __enter__(self) -> tuple[str, str]:
        return "minted-tok", "renovate-crunchtools"

    def __exit__(self, *_exc: object) -> None:
        self.released += 1


class FakeRenovate:
    """Writes a log as Renovate would and exits `rc`; records its argv and env."""

    LOG = (
        "INFO: Repository started (repository=crunchtools/a)\n"
        "INFO: DRY-RUN: Would commit files to branch renovate/x (repository=crunchtools/a)\n"
        "INFO: DRY-RUN: Would create PR: Update x (repository=crunchtools/a)\n"
        "INFO: Repository result: done (repository=crunchtools/a)\n"
        "INFO: Repository started (repository=crunchtools/b)\n"
        "ERROR: Repository has unknown error (repository=crunchtools/b)\n"
    )

    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, argv: Sequence[str], env: Mapping[str, str], log: Path) -> int:
        self.calls.append((list(argv), dict(env)))
        log.write_text(self.LOG)
        return self.rc


def scenario(*, real: bool, rc: int = 0, config: bool = True) -> dict[str, Any]:
    work = Path(tempfile.mkdtemp(dir=TMP))
    if config:
        (work / "repo" / "renovate").mkdir(parents=True)
        (work / "repo" / renovate.CONFIG).write_text("{}")
    summary = work / "summary.md"
    env = {
        **base_env,
        "RENOVATE_APP_KEY": "PEM-KEY",
        "GITHUB_TOKEN": "should-not-pass",
        "GITHUB_STEP_SUMMARY": str(summary),
        "PATH": "/p",
        "HOME": "/h",
    }
    api = FakeGitHub({("GET", "/users/renovate-crunchtools%5Bbot%5D"): {"id": 4242}})
    git = FakeRunner()
    site = Site(HOME, env, git, work, api_for=lambda _tok: api)
    mint, run = Minter(), FakeRenovate(rc)
    job = renovate.Job(site, mint, run)
    try:
        out: object = quiet(job.run, real)
    except CannotRunError as err:
        out = err
    return {
        "out": out,
        "mint": mint,
        "run": run,
        "git": git,
        "summary": summary.read_text() if summary.exists() else "",
        "work": work,
    }


dry = scenario(real=False)
check("a dry run passes", dry["out"] == 0, repr(dry["out"]))
check("a dry run mints the read set", dry["mint"].perms == [renovate.READ])
check("the token is released", dry["mint"].released == 1)
check(
    "the constitution is cloned at this run's sha, without a token",
    any(SHA in a for a in dry["git"].ran("git fetch"))
    and not any("GIT_CONFIG_VALUE_0" in e for _a, e, _c, _s in dry["git"].calls),
)
((argv, child),) = dry["run"].calls
check("renovate is the image's", argv == ["renovate"])
check("Renovate gets the minted token", child.get("RENOVATE_TOKEN") == "minted-tok")
check(
    "nothing else the job holds reaches Renovate",
    "RENOVATE_APP_KEY" not in child
    and "GITHUB_TOKEN" not in child
    and "PEM-KEY" not in child.values(),
    str(sorted(child)),
)
check(
    "Renovate commits as the App's bot",
    child.get("RENOVATE_GIT_AUTHOR")
    == "renovate-crunchtools[bot] <4242+renovate-crunchtools[bot]@users.noreply.github.com>",
)
check(
    "the global config is self-hosted.json at that sha",
    child.get("RENOVATE_CONFIG_FILE") == str(dry["work"] / "repo" / renovate.CONFIG),
)
check(
    "a dry run is full and ignores the window",
    child.get("RENOVATE_DRY_RUN") == "full" and child.get("RENOVATE_FORCE") == '{"schedule": []}',
)
check(
    "the summary counts repos, done, branches and PRs",
    all(
        line in dry["summary"]
        for line in (
            "- repos: 2",
            "- repos done: 1",
            "- branches it would commit: 1",
            "- PRs it would create: 1",
        )
    ),
    dry["summary"],
)
check("the summary samples errors", "ERROR: Repository has unknown error" in dry["summary"])

real = scenario(real=True)
check("the real run passes", real["out"] == 0)
check("the real run mints the write set", real["mint"].perms == [renovate.WRITE])
((_argv, child),) = real["run"].calls
check(
    "the real run keeps the window and writes",
    "RENOVATE_DRY_RUN" not in child and "RENOVATE_FORCE" not in child,
)

failed = scenario(real=True, rc=1)
check(
    "a failed Renovate is a 2",
    isinstance(failed["out"], CannotRunError) and "exited 1" in str(failed["out"]),
)
check("a failed Renovate still releases its token", failed["mint"].released == 1)
check("a failed Renovate still writes its summary", "### Renovate" in failed["summary"])

missing = scenario(real=False, config=False)
check(
    "no self-hosted.json is a 2 before any token is minted",
    isinstance(missing["out"], CannotRunError) and missing["mint"].perms == [],
)
bad_sha = renovate.Job(
    Site(HOME, {**base_env, "GITHUB_SHA": "main"}, FakeRunner(), TMP), Minter(), FakeRenovate()
)
check("a GITHUB_SHA that is not a commit is a 2", refusal(bad_sha.run, True) is not None)


# ---------------------------------------------------------------- org_token

app = FakeGitHub(
    {
        ("GET", "/app"): {"slug": "renovate-crunchtools"},
        ("GET", "/orgs/crunchtools/installation"): {"id": 77},
        ("POST", "/app/installations/77/access_tokens"): {"token": "org-tok"},
        ("DELETE", "/installation/token"): None,
    }
)
with (
    contextlib.redirect_stdout(io.StringIO()) as printed,
    org_token(app, "crunchtools", renovate.READ, api_for=lambda _t: app) as (tok, slug),
):
    pass
minted = app.made("POST", "/app/installations/*")
check(
    "org_token yields the token and the App's slug",
    (tok, slug) == ("org-tok", "renovate-crunchtools"),
)
check(
    "org_token mints for every repo of the installation, with only the permissions asked",
    minted and minted[0][2] == {"permissions": renovate.READ},
    str(minted),
)
check("org_token masks the token", "::add-mask::org-tok" in printed.getvalue())
check("org_token revokes on exit", len(app.made("DELETE", "/installation/token")) == 1)

check("the suite tests this checkout's job", Path(renovate.__file__).resolve() == JOB_FILE)

if __name__ == "__main__":
    sys.exit(finish())
