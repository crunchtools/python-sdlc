"""Running the tools the jobs call into: Ashigaru's scripts, Gatehouse, claude, git.

Every subprocess goes through a `Runner`, so a test can stand in for all of
them, and every one gets an environment built from an allowlist: a child sees
the credentials named for it and nothing else the job holds.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sdlc_jobs._github import Api, CannotRunError, GitHub

# Where the sdlc image puts what the jobs call into. The Containerfile owns
# these paths; tests point the module attributes at fixtures.
ASHIGARU_SCRIPTS = Path("/opt/ashigaru/scripts")
CONSTITUTION_SCRIPTS = Path("/opt/constitution/scripts")
CONSTITUTION_TEXT = Path("/opt/constitution/constitution.md")
GATEHOUSE_PYTHON = "/app/venv/bin/python"
SHIM = Path(__file__).resolve().parent / "_gatehouse_shim.py"

TIMED_OUT = 124  # timeout(1)'s status, for a child that ran out of time
NOT_STARTED = 127  # the shell's status for a command that could not be started
GIT_SECONDS = 600
SCRIPT_SECONDS = 1500  # findings.py alone may wait 900 s for the PR's checks

# What any child may inherit from the job. Credentials are never on this list:
# each call names the ones it needs.
_BASE = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "TZ")


@dataclass(frozen=True)
class Done:
    """A finished child: its status and what it printed."""

    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    """What runs a child. `run` is the real one; tests pass a fake."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path | None = None,
        stdin: str | None = None,
        timeout: float | None = None,
    ) -> Done: ...


def run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path | None = None,
    stdin: str | None = None,
    timeout: float | None = None,
) -> Done:
    """The real runner. A timeout and a command that cannot start are statuses, not raises."""
    try:
        proc = subprocess.run(
            list(argv),
            env=dict(env),
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        return Done(TIMED_OUT, str(err.stdout or ""), f"{argv[0]}: {err}")
    except OSError as err:
        return Done(NOT_STARTED, "", f"{argv[0]}: {err}")
    return Done(proc.returncode, proc.stdout, proc.stderr)


@dataclass(frozen=True)
class Site:
    """Where one job acts: the repository, the job's environment, its runner and scratch dir.

    `api_for` turns a token into an Api; a test makes it return a fake.
    """

    repo: str
    env: Mapping[str, str]
    runner: Runner
    work: Path
    api_for: Callable[[str], Api] = GitHub


def child_env(env: Mapping[str, str], **extra: str) -> dict[str, str]:
    """The allowlisted part of `env`, plus exactly the names passed in."""
    out = {k: env[k] for k in _BASE if k in env}
    out.setdefault("PATH", os.defpath)
    out.setdefault("HOME", tempfile.gettempdir())
    out.update(extra)
    return out


def git_auth(token: str) -> dict[str, str]:
    """git config, through the environment, that authenticates to github.com.

    A per-process http.extraheader: nothing is written to .git/config or to
    disk, so nothing persists past the command that was handed it.
    """
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        "GIT_TERMINAL_PROMPT": "0",
    }


def git_env(directory: Path, token: str = "") -> dict[str, str]:
    """git_auth(token), plus safe.directory=`directory` so a mounted workspace owned by another uid works.

    The bootstrap container runs git as a different uid than the one that owns
    the mounted workspace; git refuses it as "dubious ownership" otherwise.
    """
    env = git_auth(token) if token else {"GIT_TERMINAL_PROMPT": "0"}
    n = int(env.get("GIT_CONFIG_COUNT", "0"))
    env.update(
        {
            "GIT_CONFIG_COUNT": str(n + 1),
            f"GIT_CONFIG_KEY_{n}": "safe.directory",
            f"GIT_CONFIG_VALUE_{n}": str(directory.resolve()),
        }
    )
    return env


def echo(done: Done) -> None:
    """Print a child's stdout into the job's log, ending on a newline."""
    if done.stdout:
        print(done.stdout, end="" if done.stdout.endswith("\n") else "\n")


def verdict(done: Done, what: str) -> int:
    """A child whose 0 and 1 are verdicts: echoed, and anything else is CannotRunError."""
    echo(done)
    if done.returncode in (0, 1):
        return done.returncode
    tail = (done.stderr or done.stdout).strip()[-600:]
    raise CannotRunError(f"{what} failed (exit {done.returncode}): {tail}")


def checked(done: Done, what: str) -> Done:
    """`done` itself, or CannotRunError naming `what` and the child's last words."""
    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip()[-600:]
        raise CannotRunError(f"{what} failed (exit {done.returncode}): {tail}")
    return done


def clone(site: Site, ref: str, into: Path, *, token: str = "", branch: str = "") -> Path:
    """The site's repository at `ref` (a branch or a sha) in `into`, with `origin` set.

    Blobless, so history is whole (Ashigaru rebases and compares) and file
    contents arrive only as they are read. With `branch`, the checkout is on
    that local branch, as actions/checkout leaves a PR branch.
    """
    url = f"https://github.com/{site.repo}.git"
    genv = child_env(site.env, **git_env(into, token))
    into.mkdir(parents=True, exist_ok=True)
    steps: list[list[str]] = [
        ["git", "init", "-q", "."],
        ["git", "remote", "add", "origin", url],
        ["git", "fetch", "-q", "--filter=blob:none", "origin", ref],
        ["git", "checkout", "-q", *(["-B", branch] if branch else ["--detach"]), "FETCH_HEAD"],
    ]
    if branch:
        steps.append(["git", "update-ref", f"refs/remotes/origin/{branch}", "FETCH_HEAD"])
    for argv in steps:
        checked(site.runner(argv, env=genv, cwd=into, timeout=GIT_SECONDS), " ".join(argv[:3]))
    return into


_OUTPUT = re.compile(r"^([A-Za-z_][\w-]*)<<(\S+)\n(.*?)\n\2$", re.MULTILINE | re.DOTALL)


def read_outputs(text: str) -> dict[str, str]:
    """A GITHUB_OUTPUT file's `name<<DELIM` blocks and `name=value` lines."""
    found = {m.group(1): m.group(3) for m in _OUTPUT.finditer(text)}
    for line in _OUTPUT.sub("", text).splitlines():
        name, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[A-Za-z_][\w-]*", name):
            found[name] = value
    return found


def ashigaru(
    site: Site,
    script: str,
    args: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path | None = None,
) -> tuple[Done, dict[str, str]]:
    """Run one of Ashigaru's scripts by path and collect what it set as outputs.

    Its scripts write outputs to $GITHUB_OUTPUT, so each run gets a file of its
    own and the job reads it back. `env` must already be the child's.
    """
    with tempfile.NamedTemporaryFile("r", suffix=".out", delete=False, encoding="utf-8") as out:
        path = Path(out.name)
    try:
        done = site.runner(
            ["python3", str(ASHIGARU_SCRIPTS / script), *args],
            env={**env, "GITHUB_OUTPUT": str(path)},
            cwd=cwd,
            timeout=SCRIPT_SECONDS,
        )
        outputs = read_outputs(path.read_text(encoding="utf-8")) if path.exists() else {}
    finally:
        path.unlink(missing_ok=True)
    echo(done)
    return done, outputs
