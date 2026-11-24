#!/usr/bin/env python3
"""sdlc_jobs/renovate.py, against a fake GitHub, a fake git and a fake Renovate.

`renovate` holds the Renovate App's key and an org-wide token, so:

  - it runs in crunchtools/constitution only, and only on the weekly cron
    (the real run) or a dispatch from the default branch (a dry run)
  - without the key it is a 2, never a pass
  - the repositories are listed with a metadata-only token that never
    reaches Renovate, as autodiscover would list them
  - each repository gets a container of its own and a token minted for it
    alone (sdlc#121); a dry run's is read-only and sets RENOVATE_DRY_RUN, the
    real run's holds the write set and keeps the preset's window
  - Renovate is started by the execute-only node, so it is not dumpable
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
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest.mock
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import sdlc_root
from fake_github import FakeGitHub, FakeRunner, check, finish

from sdlc_jobs import _core as core
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
check(
    "a dispatch with a version is refused, whatever the YAML's if: says",
    refusal(
        renovate.is_real, "workflow_dispatch", {**DEFAULT, "inputs": {"version": "1.2.3"}}, on_main
    )
    is not None,
)
check(
    "a dispatch with an empty version is a dry run",
    renovate.is_real("workflow_dispatch", {**DEFAULT, "inputs": {"version": ""}}, on_main) is False,
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
os.environ["RENOVATE_APP_KEY"] = "PEM-KEY"
handed = {**base_env, "GITHUB_REPOSITORY": "crunchtools/x", "RENOVATE_APP_KEY": "PEM-KEY"}
quiet(renovate.main, "schedule", weekly, handed)
check(
    "main drops the App key from the process and from the env it was handed",
    "RENOVATE_APP_KEY" not in os.environ and "RENOVATE_APP_KEY" not in handed,
)


# ------------------------------------------------------------------ a run


class Minter:
    """Records each (permissions, repository) asked for and each release; yields
    (a token naming its repository, slug)."""

    def __init__(self) -> None:
        self.asked: list[tuple[dict[str, str], str | None]] = []
        self.released = 0

    @property
    def perms(self) -> list[dict[str, str]]:
        return [perms for perms, _repo in self.asked]

    def __call__(self, perms: Mapping[str, str], repo: str | None) -> Minter:
        self.asked.append((dict(perms), repo))
        return self

    def __enter__(self) -> tuple[str, str]:
        repo = self.asked[-1][1]
        return f"minted-tok-{repo or 'org'}", "renovate-crunchtools"

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

    def __init__(self, *rcs: int | BaseException) -> None:
        self.rcs = list(rcs)
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, argv: Sequence[str], env: Mapping[str, str], log: Path) -> int:
        self.calls.append((list(argv), dict(env)))
        if len(self.calls) == 1:
            log.write_text(self.LOG)
        rc = self.rcs[len(self.calls) - 1] if len(self.calls) <= len(self.rcs) else 0
        if isinstance(rc, BaseException):
            raise rc
        return rc


IMAGE = "quay.io/crunchtools/sdlc@sha256:" + "e" * 64


class FakeSocket:
    """The socket's tools: answers `podman inspect` of this container with its image,
    and records every command."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        doc = [{"ImageName": IMAGE, "Image": "f" * 64}] if "inspect" in argv else []
        return subprocess.CompletedProcess(argv, 0, json.dumps(doc), "")


GLOBAL = {"platform": "github", "autodiscover": True, "autodiscoverFilter": ["crunchtools/*"]}
INSTALLED: list[dict[str, Any]] = [
    {"full_name": "crunchtools/b"},
    {"full_name": "crunchtools/A"},
    {"full_name": "crunchtools/old", "archived": True},
    {"full_name": "elsewhere/x"},
    {},
]
REPOS = ["crunchtools/A", "crunchtools/b"]


def scenario(
    *,
    real: bool,
    rcs: Sequence[int | BaseException] = (),
    config: dict[str, Any] | None = GLOBAL,
    installed: list[dict[str, Any]] = INSTALLED,
    run: FakeRenovate | None = None,
) -> dict[str, Any]:
    work = Path(tempfile.mkdtemp(dir=TMP))
    if config is not None:
        (work / "repo" / "renovate").mkdir(parents=True)
        (work / "repo" / renovate.CONFIG).write_text(json.dumps(config))
    summary = work / "summary.md"
    env = {
        **base_env,
        "RENOVATE_APP_KEY": "PEM-KEY",
        "GITHUB_TOKEN": "should-not-pass",
        "GITHUB_STEP_SUMMARY": str(summary),
        "PATH": "/p",
        "HOME": "/h",
        "HOSTNAME": "self-id",
    }
    api = FakeGitHub(
        {
            ("GET", "/users/renovate-crunchtools%5Bbot%5D"): {"id": 4242},
            ("GET", "/installation/repositories"): {"repositories": installed},
        }
    )
    git = FakeRunner()
    tokens: list[str] = []

    def api_for(tok: str) -> FakeGitHub:
        tokens.append(tok)
        return api

    site = Site(HOME, env, git, work, api_for=api_for)
    mint = Minter()
    run = run or FakeRenovate(*rcs)
    sock = FakeSocket()
    job = renovate.Job(site, mint, sock, run)
    try:
        out: object = quiet(job.run, real)
    except Exception as err:  # a CannotRunError, or the crash a scenario raises
        out = err
    return {
        "out": out,
        "mint": mint,
        "run": run,
        "sock": sock,
        "git": git,
        "summary": summary.read_text() if summary.exists() else "",
        "work": work,
        "tokens": tokens,
    }


dry = scenario(real=False)
check("a dry run passes", dry["out"] == 0, repr(dry["out"]))
check(
    "the repositories are listed with a metadata-only token for the whole installation",
    dry["mint"].asked[0] == (renovate.DISCOVER, None) and set(dry["tokens"]) == {"minted-tok-org"},
    str(dry["mint"].asked),
)
check(
    "a dry run mints the read set for each repository alone, in turn",
    dry["mint"].asked[1:] == [(renovate.READ, r) for r in REPOS],
    str(dry["mint"].asked),
)
check("every token is released", dry["mint"].released == 1 + len(REPOS))
check(
    "archived repositories, other owners and empty entries are left out",
    [json.loads(c["RENOVATE_CONFIG"])["repositories"] for _a, c in dry["run"].calls]
    == [[r] for r in REPOS],
)
check(
    "the constitution is cloned at this run's sha, without a token",
    any(SHA in a for a in dry["git"].ran("git fetch"))
    and not any(
        "AUTHORIZATION" in e.get("GIT_CONFIG_VALUE_0", "") for _a, e, _c, _s in dry["git"].calls
    ),
)
(argv, child), (argv_b, child_b) = dry["run"].calls
check(
    "Renovate runs in a nested container from this job's image, by digest, "
    "started by the execute-only node",
    argv[argv.index("--entrypoint") + 1 :] == [renovate.NODE, IMAGE, renovate.RENOVATE_JS]
    and "run" in argv,
    str(argv),
)
check(
    "each repository's container has a name of its own",
    argv[argv.index("--name") + 1] != argv_b[argv_b.index("--name") + 1],
)
check(
    "each repository's Renovate gets that repository's token, never another's",
    child.get("RENOVATE_TOKEN") == "minted-tok-crunchtools/A"
    and child_b.get("RENOVATE_TOKEN") == "minted-tok-crunchtools/b",
)
check(
    "each Renovate is told its one repository and not to autodiscover",
    json.loads(child["RENOVATE_CONFIG"])
    == {**GLOBAL, "autodiscover": False, "repositories": ["crunchtools/A"]},
    child["RENOVATE_CONFIG"],
)
check(
    "the nested container gets no socket and none of this job's mounts",
    "--volumes-from" not in argv
    and not any("docker.sock" in a or "podman.sock" in a for a in argv[argv.index("run") :])
    and [argv[i + 1] for i, a in enumerate(argv) if a == "-v"] == [renovate.WORK],
    str(argv),
)
name = argv[argv.index("--name") + 1]
check(
    "the nested container has a unique name and podman's own time limit",
    name.startswith("sdlc-renovate-")
    and 0 < int(argv[argv.index("--timeout") + 1]) <= renovate.CONTAINER_SECONDS
    and renovate.CONTAINER_SECONDS < 55 * 60,
    str(argv),
)
check(
    "the container is removed by name after a clean run",
    any(c[-4:] == ["rm", "-f", "--ignore", name] for c in dry["sock"].calls),
    str(dry["sock"].calls),
)
check(
    "the nested container is locked down",
    {"--cap-drop=all", "--security-opt=no-new-privileges", "--read-only", "--pull=never"}
    <= set(argv),
)
env_args = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
check(
    "the nested container's environment is RENOVATE_* by name and three literals",
    set(env_args)
    == {*renovate.BY_NAME, "HOME=/tmp", f"RENOVATE_BASE_DIR={renovate.WORK}", "LOG_LEVEL=info"},
    str(env_args),
)
check(
    "no key and no token on the nested command line",
    not any("PEM-KEY" in a or "minted-tok" in a or "APP_KEY" in a for a in argv),
)
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
    "the global config is self-hosted.json at that sha, as RENOVATE_CONFIG",
    json.loads(child.get("RENOVATE_CONFIG", "null"))["platform"] == "github"
    and "RENOVATE_CONFIG_FILE" not in child,
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
check(
    "the real run mints the write set for each repository alone",
    real["mint"].asked[1:] == [(renovate.WRITE, r) for r in REPOS],
)
check(
    "the write set is never minted for the whole installation",
    all(repo for perms, repo in real["mint"].asked if perms == renovate.WRITE),
)
(_argv, child), _ = real["run"].calls
check(
    "the real run keeps the window and writes",
    "RENOVATE_DRY_RUN" not in child and "RENOVATE_FORCE" not in child,
)

failed = scenario(real=True, rcs=[1])
check(
    "a repository Renovate fails in is a 2 that names it",
    isinstance(failed["out"], CannotRunError)
    and "1 of 2 repositories: crunchtools/A;" in str(failed["out"]),
    repr(failed["out"]),
)
check("a failure does not stop the next repository", len(failed["run"].calls) == len(REPOS))
check("a failed Renovate still releases every token", failed["mint"].released == 1 + len(REPOS))
check("a failed Renovate still writes its summary", "### Renovate" in failed["summary"])

for label, err in (
    ("a stop signal", CannotRunError("renovate was stopped by SIGTERM")),
    ("a crash", RuntimeError("boom")),
):
    raised = scenario(real=True, rcs=[err])
    ((r_argv, _env),) = raised["run"].calls
    r_name = r_argv[r_argv.index("--name") + 1]
    check(
        f"{label} mid-run propagates, and still removes the container and revokes the tokens",
        raised["out"] is err
        and any(c[-4:] == ["rm", "-f", "--ignore", r_name] for c in raised["sock"].calls)
        and raised["mint"].released == 2,
        repr(raised["out"]),
    )

saved_seconds = renovate.CONTAINER_SECONDS
renovate.CONTAINER_SECONDS = renovate.LEAST_SECONDS - 1
late = scenario(real=True)
renovate.CONTAINER_SECONDS = saved_seconds
check(
    "with too little time left a repository is not started, and is reported",
    late["run"].calls == []
    and isinstance(late["out"], CannotRunError)
    and "crunchtools/A (out of time)" in str(late["out"]),
    repr(late["out"]),
)


class SlowRenovate(FakeRenovate):
    """Uses up all but 30 seconds of the run's budget on each repository."""

    def __init__(self, clock: list[float]) -> None:
        super().__init__()
        self.clock = clock

    def __call__(self, argv: Sequence[str], env: Mapping[str, str], log: Path) -> int:
        self.clock[0] += renovate.CONTAINER_SECONDS - 30
        return super().__call__(argv, env, log)


clock = [0.0]


def now() -> float:
    return clock[0]


with unittest.mock.patch.object(renovate.time, "monotonic", now):
    partway = scenario(real=True, run=SlowRenovate(clock))
check(
    "a run out of time partway reports the rest, without a token or container for them",
    len(partway["run"].calls) == 1
    and partway["mint"].asked[1:] == [(renovate.WRITE, "crunchtools/A")]
    and isinstance(partway["out"], CannotRunError)
    and "1 of 2 repositories: crunchtools/b (out of time)" in str(partway["out"]),
    repr(partway["out"]),
)

for label, cfg in (
    ("no autodiscover", {**GLOBAL, "autodiscover": False}),
    ("a regex filter", {**GLOBAL, "autodiscoverFilter": ["/crunchtools/.*/"]}),
    ("a negated filter", {**GLOBAL, "autodiscoverFilter": ["!crunchtools/x"]}),
    ("a brace filter", {**GLOBAL, "autodiscoverFilter": ["crunchtools/{a,b}"]}),
    ("autodiscoverTopics", {**GLOBAL, "autodiscoverTopics": ["x"]}),
    ("autodiscoverNamespaces", {**GLOBAL, "autodiscoverNamespaces": ["x"]}),
    ("autodiscoverProjects", {**GLOBAL, "autodiscoverProjects": ["x"]}),
    ("includeMirrors", {**GLOBAL, "includeMirrors": True}),
    ("fixed repositories", {**GLOBAL, "repositories": ["crunchtools/a"]}),
):
    odd = scenario(real=True, config=cfg)
    check(
        f"a config with {label} is a 2 before any write token is minted",
        isinstance(odd["out"], CannotRunError) and renovate.WRITE not in odd["mint"].perms,
        repr(odd["out"]),
    )
check(
    "a filter given as one string is matched like a list",
    scenario(real=False, config={**GLOBAL, "autodiscoverFilter": "crunchtools/a"})["out"] == 0,
)
check(
    "no matching repository is a 2",
    isinstance(
        scenario(real=False, installed=[{"full_name": "elsewhere/x"}])["out"], CannotRunError
    ),
)

with renovate.interruptible():
    stopped = refusal(os.kill, os.getpid(), signal.SIGTERM)
check("SIGTERM inside the run raises, so the finallys run", "SIGTERM" in (stopped or ""))
check(
    "the handlers are put back afterwards",
    signal.getsignal(signal.SIGTERM) is signal.SIG_DFL,
)

missing = scenario(real=False, config=None)
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
with (
    contextlib.redirect_stdout(io.StringIO()),
    org_token(app, "crunchtools", renovate.WRITE, repositories=["a"], api_for=lambda _t: app),
):
    pass
check(
    "org_token named repositories mints for those alone",
    app.made("POST", "/app/installations/*")[-1][2]
    == {"permissions": renovate.WRITE, "repositories": ["a"]},
)
check("org_token masks the token", "::add-mask::org-tok" in printed.getvalue())
check("org_token revokes each token on exit", len(app.made("DELETE", "/installation/token")) == 2)

# ------------------------------------------------------------------- stream

log = TMP / "stream.log"
shell = ["sh", "-c", "echo out; echo err >&2; exit 3"]
code = quiet(renovate.stream, shell, {"PATH": "/usr/bin:/bin"}, log)
check("stream returns the child's status", code == 3)
check("stream keeps stdout and stderr in the log", log.read_text().split() == ["out", "err"])
check(
    "a command that cannot start is a 2, not a status",
    refusal(renovate.stream, [str(TMP / "no-such-renovate")], {}, log) is not None,
)


class TaggedSocket(FakeSocket):
    def run(self, argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        doc = [{"ImageName": "quay.io/crunchtools/sdlc:latest", "Image": "f" * 64}]
        return subprocess.CompletedProcess(argv, 0, json.dumps(doc), "")


tagged = renovate.Job(
    Site(HOME, {**base_env, "HOSTNAME": "self-id"}, FakeRunner(), TMP),
    Minter(),
    TaggedSocket(),
    FakeRenovate(),
)
check(
    "an image not pinned by digest is a 2",
    "not pinned by digest" in (refusal(tagged.image) or ""),
)

# ------------------------------------------------------------- inspect_self


class AnswerSocket:
    def __init__(self, stdout: str, rc: int = 0) -> None:
        self.stdout, self.rc = stdout, rc

    def run(self, argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, "")


for label, sock, env in (
    ("inspect exits non-zero", AnswerSocket("", 125), {"HOSTNAME": "x"}),
    ("inspect is not JSON", AnswerSocket("not json"), {"HOSTNAME": "x"}),
    ("inspect is an empty list", AnswerSocket("[]"), {"HOSTNAME": "x"}),
    ("inspect's first item is not an object", AnswerSocket('["x"]'), {"HOSTNAME": "x"}),
):
    try:
        core.inspect_self(sock, env)
        raised = False
    except core.CannotRunError:
        raised = True
    check(f"inspect_self: {label} is a 2", raised)

# The id the engine named /etc/hostname's file by wins over HOSTNAME, which a
# runner's engine may set to the runner's own hostname.
CID = "ab" * 32
with tempfile.TemporaryDirectory() as tmp:
    mi = Path(tmp) / "mountinfo"
    mi.write_text(
        "1 2 0:1 / / rw - overlay overlay rw\n"
        f"3 1 253:0 /var/lib/containers/storage/overlay-containers/{CID}/userdata/hostname"
        " /etc/hostname rw - xfs /dev/x rw\n"
    )
    check(
        "own_container_id reads the engine's hostname mount", core.own_container_id(str(mi)) == CID
    )
    mi.write_text("1 2 0:1 / / rw - overlay overlay rw\n")
    check("own_container_id is empty outside a container", core.own_container_id(str(mi)) == "")
check("own_container_id is empty without mountinfo", core.own_container_id("/nonexistent") == "")

check("the suite tests this checkout's job", Path(renovate.__file__).resolve() == JOB_FILE)

if __name__ == "__main__":
    sys.exit(finish())
