"""The `renovate` job: the Renovate App's key, and nothing else.

The fleet's self-hosted Renovate (constitution XV, a1099), which used to be
the constitution's own renovate.yml. It runs in crunchtools/constitution only,
where its config lives, and walks every crunchtools/* repo:

  schedule           the weekly cron: the real run, inside the preset's window
  workflow_dispatch  from the default branch: a dry run that ignores the
                     window, so it shows the week's plan on any day

Renovate has its own App, renovate.crunchtools.com, with ordinary permissions
and no workflows permission. Its key reaches this job from the `renovate`
environment, which admits the default branch only. The job holds no
GITHUB_TOKEN: the token minted here is the only credential it has, org-wide by
design, read-only on a dry run, and revoked when Renovate exits. That is why it
runs on the uncached runners, whose storage starts empty every job, or on
GitHub's machines, and never on the cached ones.

The global config is renovate/self-hosted.json, read from this run's own commit
of the constitution, so a config change takes effect when it merges. Renovate
is the image's, at RENOVATE_VERSION.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sdlc_jobs import _tools as tools
from sdlc_jobs._app_token import app_jwt, org_token
from sdlc_jobs._github import OK, CannotRunError, GitHub, contract, default_branch, repo_of
from sdlc_jobs._tools import Site

ORG = "crunchtools"
HOME_REPO = "crunchtools/constitution"
# renovate.crunchtools.com's client ID. Not a secret.
CLIENT_ID = "Iv23liH4RqEwxk282x3X"
CONFIG = "renovate/self-hosted.json"
WEEKLY = "17 6 * * 1"
# Renovate reads issues while opening each repo, so even the dry run needs them.
READ = {"contents": "read", "pull_requests": "read", "issues": "read"}
WRITE = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "statuses": "write",
    "checks": "read",
    "vulnerability_alerts": "read",
}
SAMPLE_LINES = 400
SAMPLE = re.compile(
    r"DRY-RUN: Would (create PR|commit files to branch)|Repository result:"
    r"|Repository is a fork|ERROR:"
)
COUNTS = (
    ("repos", "Repository started"),
    ("repos done", "Repository result: done"),
    ("branches it would commit", "DRY-RUN: Would commit files to branch"),
    ("PRs it would create", "DRY-RUN: Would create PR"),
)

Mint = Callable[[Mapping[str, str]], AbstractContextManager[tuple[str, str]]]
Stream = Callable[[Sequence[str], Mapping[str, str], Path], int]


@contract
def main(event_name: str, event: dict[str, Any], env: Mapping[str, str]) -> int:
    """`sdlc event`'s entry for the `renovate` job: 0 when Renovate ran clean, else 2."""
    real = is_real(event_name, event, env)
    repo = repo_of(event, env)
    if repo != HOME_REPO:
        raise CannotRunError(f"renovate runs in {HOME_REPO} only, not {repo}")
    key = env.get("RENOVATE_APP_KEY", "")
    if not key:
        raise CannotRunError(
            "RENOVATE_APP_KEY must reach `renovate` "
            "(the `renovate` environment admits the default branch only)"
        )
    with tempfile.TemporaryDirectory(prefix="sdlc-renovate-") as scratch:
        site = Site(repo, env, tools.run, Path(scratch))

        def mint(perms: Mapping[str, str]) -> AbstractContextManager[tuple[str, str]]:
            return org_token(GitHub(app_jwt(CLIENT_ID, key, time.time())), ORG, perms)

        return Job(site, mint).run(real)


def is_real(event_name: str, event: Mapping[str, Any], env: Mapping[str, str]) -> bool:
    """True for the weekly run, False for a dispatch's dry run; anything else is refused."""
    if event_name == "schedule":
        if event.get("schedule") != WEEKLY:
            raise CannotRunError(f"renovate runs on the weekly cron {WEEKLY!r} only")
        return True
    if event_name == "workflow_dispatch":
        if env.get("GITHUB_REF") != f"refs/heads/{default_branch(event)}":
            raise CannotRunError("a renovate dry run starts from the default branch only")
        return False
    raise CannotRunError(f"renovate runs on schedule and workflow_dispatch, not {event_name!r}")


def stream(argv: Sequence[str], env: Mapping[str, str], log: Path) -> int:
    """Run argv with its output in the job log as it comes, and in `log` too."""
    with log.open("w", encoding="utf-8") as out:
        try:
            proc = subprocess.Popen(
                list(argv),
                env=dict(env),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as err:
            raise CannotRunError(f"could not start {argv[0]}: {err}") from err
        if proc.stdout is None:
            raise CannotRunError(f"{argv[0]} started with no output pipe")
        for line in proc.stdout:
            print(line, end="", flush=True)
            out.write(line)
        return proc.wait()


@dataclass
class Job:
    """One run: the site, how a token is minted, how Renovate is run. Tests fake the last two."""

    site: Site
    mint: Mint
    runner: Stream = field(default=stream)

    def run(self, real: bool) -> int:
        """Renovate over the org with this commit's config; OK, or CannotRunError."""
        config = self.config()
        log = self.site.work / "renovate.log"
        print(f"renovate: {'a real run' if real else 'a dry run'} of {ORG}/*, config {CONFIG}")
        with self.mint(WRITE if real else READ) as (token, slug):
            env = self.child(token, self.author(token, slug), config, real)
            rc = self.runner(["renovate"], env, log)
        self.summarise(log)
        if rc != 0:
            raise CannotRunError(f"renovate exited {rc}; its log above says which repo")
        return OK

    def config(self) -> Path:
        """renovate/self-hosted.json from this run's commit of the constitution."""
        sha = self.site.env.get("GITHUB_SHA", "")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise CannotRunError(f"GITHUB_SHA is not a commit: {sha!r}")
        config = tools.clone(self.site, sha, self.site.work / "repo") / CONFIG
        if not config.is_file():
            raise CannotRunError(f"{HOME_REPO}@{sha[:12]} has no {CONFIG}")
        return config

    def author(self, token: str, slug: str) -> str:
        """The App's bot identity, read from the slug the token was minted for."""
        login = f"{slug}[bot]"
        user = self.site.api_for(token).request("GET", f"/users/{urllib.parse.quote(login)}")
        return f"{login} <{user['id']}+{login}@users.noreply.github.com>"

    def child(self, token: str, author: str, config: Path, real: bool) -> dict[str, str]:
        extra = {
            "RENOVATE_TOKEN": token,
            "RENOVATE_GIT_AUTHOR": author,
            "RENOVATE_CONFIG_FILE": str(config),
            "RENOVATE_BASE_DIR": str(self.site.work / "base"),
            "LOG_LEVEL": "info",
        }
        if not real:
            # A dry run ignores the preset's weekly window (schedule:weekly), so
            # it shows the week's plan on any day; a real run keeps the window.
            extra |= {"RENOVATE_DRY_RUN": "full", "RENOVATE_FORCE": json.dumps({"schedule": []})}
        return tools.child_env(self.site.env, **extra)

    def summarise(self, log: Path) -> None:
        """Counts and a sample of the log, printed and added to the step summary."""
        if not log.is_file():
            return
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        counts = [f"- {name}: {sum(needle in ln for ln in lines)}" for name, needle in COUNTS]
        sample = [ln for ln in lines if SAMPLE.search(ln)][:SAMPLE_LINES]
        text = "\n".join(["### Renovate", "", *counts, "", "```", *sample, "```", ""])
        print(text)
        summary = self.site.env.get("GITHUB_STEP_SUMMARY", "")
        if summary:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(text)
