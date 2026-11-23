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
GITHUB_TOKEN. The key is read once, dropped from the job's environment, and
used only to mint installation tokens. It runs on the uncached runners, whose
storage starts empty every job, or on GitHub's machines, and never on the
cached ones.

Renovate's package managers run dependency code: a Python lock update builds
an sdist, which runs that package's setup.py. Renovate hands that code its
token on purpose (GIT_CONFIG_VALUE_n for uv, poetry, pdm and gomod, and the
clone's origin URL in .git/config), and no option turns that off (sdlc#121).
So the token dependency code can reach is made worth as little as possible:

  - the job lists the repositories itself, with a metadata-only token that
    never enters a container, as Renovate's autodiscover would
  - each repository gets a container of its own and a token minted for that
    repository alone, read-only on a dry run, revoked when its container is
    gone; whatever dependency code leaves running dies with the container
  - Renovate is started by an execute-only node (the Containerfile's
    renovate-node), so the kernel marks it not dumpable and nothing it runs can
    read its /proc/<pid>/environ or mem, or ptrace it

Each container is from this job's own image, by digest. It gets no socket, no
--volumes-from and nothing of this job's but the RENOVATE_* variables: never
the App key. It drops every capability, cannot gain privileges, has a
read-only root, and works in an anonymous volume on disk, gone with it. All of
them share 50 minutes, inside the job's 55.

The global config is renovate/self-hosted.json, read from this run's own commit
of the constitution, so a config change takes effect when it merges, and handed
to Renovate as RENOVATE_CONFIG. Renovate is the image's, at RENOVATE_VERSION.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import secrets
import signal
import subprocess
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sdlc_jobs import _core as core
from sdlc_jobs import _tools as tools
from sdlc_jobs._app_token import app_jwt, org_token
from sdlc_jobs._github import OK, CannotRunError, GitHub, contract, default_branch, repo_of
from sdlc_jobs._tools import Site

ORG = "crunchtools"
HOME_REPO = "crunchtools/constitution"
# renovate.crunchtools.com's client ID. Not a secret.
CLIENT_ID = "Iv23liH4RqEwxk282x3X"
CONFIG = "renovate/self-hosted.json"
# Plain owner/globs: the only autodiscoverFilter this job matches as Renovate would.
FILTER = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.*?-]+")
# Global options that change which repositories Renovate's autodiscover finds,
# beyond autodiscoverFilter; the job does not repeat them, so it refuses them.
SHAPING = (
    "repositories",
    "autodiscoverTopics",
    "autodiscoverNamespaces",
    "autodiscoverProjects",
    "includeMirrors",
)
WEEKLY = "17 6 * * 1"
# Listing the installation's repositories; this token never enters a container.
DISCOVER = {"metadata": "read"}
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
# Renovate's working tree in the nested container: an anonymous volume, on disk.
WORK = "/work"
# node at mode 0111 (the Containerfile), so the Renovate it runs is not dumpable.
NODE = "/usr/local/libexec/renovate-node"
RENOVATE_JS = "/opt/renovate/node_modules/renovate/dist/renovate.js"
# Passed to the nested container by name only, so no value is on a command line.
BY_NAME = (
    "RENOVATE_TOKEN",
    "RENOVATE_GIT_AUTHOR",
    "RENOVATE_CONFIG",
    "RENOVATE_DRY_RUN",
    "RENOVATE_FORCE",
)
# Every repository's container shares this many seconds, 50 minutes, inside the
# job's 55, leaving time to clean up; podman stops each at what is left.
CONTAINER_SECONDS = 3000
# A repository is not started with less than this left; it is reported instead.
LEAST_SECONDS = 60
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

# (permissions, a repository or None for the whole installation) -> (token, slug)
Mint = Callable[[Mapping[str, str], str | None], AbstractContextManager[tuple[str, str]]]
Stream = Callable[[Sequence[str], Mapping[str, str], Path], int]


@contract
def main(event_name: str, event: dict[str, Any], env: Mapping[str, str]) -> int:
    """`sdlc event`'s entry for the `renovate` job: 0 when Renovate ran clean, else 2."""
    # First of all, whatever follows: the key is held by the mint below and
    # nowhere else from here on.
    key = env.get("RENOVATE_APP_KEY", "")
    for holder in (env, os.environ):
        if isinstance(holder, MutableMapping):
            holder.pop("RENOVATE_APP_KEY", None)
    real = is_real(event_name, event, env)
    repo = repo_of(event, env)
    if repo != HOME_REPO:
        raise CannotRunError(f"renovate runs in {HOME_REPO} only, not {repo}")
    if not key:
        raise CannotRunError(
            "RENOVATE_APP_KEY must reach `renovate` "
            "(the `renovate` environment admits the default branch only)"
        )
    with tempfile.TemporaryDirectory(prefix="sdlc-renovate-") as scratch:
        site = Site(repo, env, tools.run, Path(scratch))

        def mint(
            perms: Mapping[str, str], repo: str | None
        ) -> AbstractContextManager[tuple[str, str]]:
            names = [repo.removeprefix(f"{ORG}/")] if repo else []
            app = GitHub(app_jwt(CLIENT_ID, key, time.time()))
            return org_token(app, ORG, perms, repositories=names)

        return Job(site, mint, core.Tools()).run(real)


def is_real(event_name: str, event: Mapping[str, Any], env: Mapping[str, str]) -> bool:
    """True for the weekly run, False for a dispatch's dry run; anything else is refused."""
    if event_name == "schedule":
        if event.get("schedule") != WEEKLY:
            raise CannotRunError(f"renovate runs on the weekly cron {WEEKLY!r} only")
        return True
    if event_name == "workflow_dispatch":
        if (event.get("inputs") or {}).get("version"):
            raise CannotRunError("a dispatch with a version is a release, not a renovate dry run")
        if env.get("GITHUB_REF") != f"refs/heads/{default_branch(event)}":
            raise CannotRunError("a renovate dry run starts from the default branch only")
        return False
    raise CannotRunError(f"renovate runs on schedule and workflow_dispatch, not {event_name!r}")


@contextmanager
def interruptible() -> Iterator[None]:
    """SIGTERM and SIGINT raise, so a cancelled or timed-out job still unwinds
    through its `finally`s: the container is removed and the token revoked."""

    def stop(signum: int, _frame: object) -> None:
        raise CannotRunError(f"renovate was stopped by {signal.Signals(signum).name}")

    before = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, handler in before.items():
            signal.signal(sig, handler)


def stream(argv: Sequence[str], env: Mapping[str, str], log: Path) -> int:
    """Run argv with its output in the job log as it comes, and in `log` too."""
    with log.open("a", encoding="utf-8") as out:
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
        try:
            if proc.stdout is None:
                raise CannotRunError(f"{argv[0]} started with no output pipe")
            for line in proc.stdout:
                print(line, end="", flush=True)
                out.write(line)
            return proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


@dataclass(frozen=True)
class Walk:
    """What every repository's Renovate shares in one run."""

    image: str
    settings: Mapping[str, Any]
    author: str
    real: bool
    log: Path


@dataclass
class Job:
    """One run: the site, how a token is minted, the socket's tools (to find this
    job's image) and how the nested container is run. Tests fake all but the site."""

    site: Site
    mint: Mint
    tools: core.Tools
    runner: Stream = field(default=stream)

    def run(self, real: bool) -> int:
        """Renovate over the org with this commit's config, a repository at a
        time; OK, or CannotRunError naming the repositories that failed."""
        settings, image = self.settings(self.config()), self.image()
        log = self.site.work / "renovate.log"
        print(f"renovate: {'a real run' if real else 'a dry run'} of {ORG}/*, config {CONFIG}")
        deadline = time.monotonic() + CONTAINER_SECONDS
        with interruptible():
            with self.mint(DISCOVER, None) as (token, slug):
                repos = self.discover(token, settings)
                author = self.author(token, slug)
            print(f"renovate: {len(repos)} repositories, each with a token for it alone")
            failed = self.each(Walk(image, settings, author, real, log), repos, deadline)
        self.summarise(log)
        if failed:
            raise CannotRunError(
                f"renovate failed in {len(failed)} of {len(repos)} repositories: "
                f"{', '.join(failed)}; its log above says why"
            )
        return OK

    def each(self, walk: Walk, repos: Sequence[str], deadline: float) -> list[str]:
        """Renovate over each of `repos` in turn, until `deadline` (monotonic);
        the ones it failed in or had no time for."""
        failed: list[str] = []
        for done, repo in enumerate(repos):
            seconds = int(deadline - time.monotonic())
            if seconds < LEAST_SECONDS:
                return failed + [f"{left} (out of time)" for left in repos[done:]]
            if self.one(walk, repo, seconds) != 0:
                failed.append(repo)
        return failed

    def one(self, walk: Walk, repo: str, seconds: int) -> int:
        """Renovate over `repo` alone, in a container of its own, with a token
        for `repo` alone; Renovate's exit status."""
        name = f"sdlc-renovate-{secrets.token_hex(8)}"
        alone = {**walk.settings, "autodiscover": False, "repositories": [repo]}
        # Inner to outer on every path, a signal included: the container goes,
        # then the token is revoked as the mint's context exits.
        with self.mint(WRITE if walk.real else READ, repo) as (token, _slug):
            env = self.child(token, walk.author, alone, walk.real)
            try:
                return self.runner(self.nested(walk.image, name, seconds), env, walk.log)
            finally:
                self.remove(name)

    def config(self) -> Path:
        """renovate/self-hosted.json from this run's commit of the constitution."""
        sha = self.site.env.get("GITHUB_SHA", "")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise CannotRunError(f"GITHUB_SHA is not a commit: {sha!r}")
        config = tools.clone(self.site, sha, self.site.work / "repo") / CONFIG
        if not config.is_file():
            raise CannotRunError(f"{HOME_REPO}@{sha[:12]} has no {CONFIG}")
        return config

    def settings(self, config: Path) -> dict[str, Any]:
        """The global config, which must be a JSON object."""
        try:
            settings = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            raise CannotRunError(f"{CONFIG} does not load: {err}") from err
        if not isinstance(settings, dict):
            raise CannotRunError(f"{CONFIG} is not a JSON object")
        return settings

    def discover(self, token: str, settings: Mapping[str, Any]) -> list[str]:
        """What Renovate's autodiscover would find: the installation's repositories
        that are not archived and match autodiscoverFilter's plain owner/globs.
        Anything else in the config that would change that set is refused."""
        filters = settings.get("autodiscoverFilter")
        if isinstance(filters, str):
            filters = [filters]
        if (
            settings.get("autodiscover") is not True
            or any(settings.get(option) for option in SHAPING)
            or not isinstance(filters, list)
            or not filters
            or not all(isinstance(f, str) and FILTER.fullmatch(f) for f in filters)
        ):
            raise CannotRunError(
                f"{CONFIG} must autodiscover by autodiscoverFilter alone, "
                "as plain owner/globs, for this job to split the run by repository"
            )
        found = self.site.api_for(token).pages("/installation/repositories", "repositories")
        repos = sorted(
            str(r["full_name"])
            for r in found
            if isinstance(r, dict) and r.get("full_name") and not r.get("archived")
        )
        # Renovate matches case-insensitively.
        chosen = [
            r
            for r in repos
            if r.split("/", 1)[0] == ORG
            and any(fnmatch.fnmatchcase(r.lower(), f.lower()) for f in filters)
        ]
        if not chosen:
            raise CannotRunError(f"no repository of the installation matches {filters}")
        return chosen

    def image(self) -> str:
        """This job's own image, by digest, found through the socket."""
        image = core.image_of(core.inspect_self(self.tools, self.site.env))
        if "@sha256:" not in image:
            raise CannotRunError(f"this job's image is not pinned by digest: {image!r}")
        return image

    def author(self, token: str, slug: str) -> str:
        """The App's bot identity, read from the slug the token was minted for."""
        login = f"{slug}[bot]"
        user = self.site.api_for(token).request("GET", f"/users/{urllib.parse.quote(login)}")
        return f"{login} <{user['id']}+{login}@users.noreply.github.com>"

    def remove(self, name: str) -> None:
        """`podman rm -f` of Renovate's container; gone already is fine, a failure is said."""
        proc = self.tools.run([*core.podman(self.site.env), "rm", "-f", "--ignore", name])
        if proc.returncode != 0:
            print(f"::warning::could not remove {name}: {(proc.stderr or proc.stdout).strip()}")

    def nested(self, image: str, name: str, seconds: int) -> list[str]:
        """podman's argv for Renovate's container: nothing of this job's but BY_NAME,
        and Renovate started by the execute-only node."""
        named = [arg for var in BY_NAME for arg in ("-e", var)]
        return [
            *core.podman(self.site.env),
            "run",
            "--rm",
            "--name",
            name,
            "--timeout",
            str(seconds),
            "--pull=never",
            "--log-driver=none",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,exec,size=1g,mode=1777",  # noqa: S108 -- inside the nested container
            "-v",
            WORK,
            "-w",
            WORK,
            "-e",
            "HOME=/tmp",
            "-e",
            f"RENOVATE_BASE_DIR={WORK}",
            "-e",
            "LOG_LEVEL=info",
            *named,
            "--entrypoint",
            NODE,
            image,
            RENOVATE_JS,
        ]

    def child(
        self, token: str, author: str, settings: Mapping[str, Any], real: bool
    ) -> dict[str, str]:
        """podman's own environment: the allowlist, and the values BY_NAME hands on."""
        extra = {
            "RENOVATE_TOKEN": token,
            "RENOVATE_GIT_AUTHOR": author,
            "RENOVATE_CONFIG": json.dumps(settings),
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
        # One pass, line by line: an org-wide log is never held whole.
        found = dict.fromkeys((name for name, _ in COUNTS), 0)
        sample: list[str] = []
        with log.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.rstrip("\n")
                for name, needle in COUNTS:
                    found[name] += needle in line
                if len(sample) < SAMPLE_LINES and SAMPLE.search(line):
                    sample.append(line)
        counts = [f"- {name}: {n}" for name, n in found.items()]
        text = "\n".join(["### Renovate", "", *counts, "", "```", *sample, "```", ""])
        print(text)
        summary = self.site.env.get("GITHUB_STEP_SUMMARY", "")
        if summary:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(text)
