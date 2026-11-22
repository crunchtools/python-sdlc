#!/usr/bin/env python3
"""Run scripts/convert_repo.py across the org: the last fleet-wide PR wave.

Every public, non-archived repo in the org (or the ones a --repos file names)
is cloned at its default branch, converted, and verified. Without --apply that
is all: the plan goes to the ledger and nothing leaves this machine. With
--apply each converted repo gets one commit on chore/sdlc-floating, pushed, a
pull request with a fixed body, and auto-merge (squash) -- and its own CI
decides whether it lands.

Per repo, in order, and a repo that fails a step is skipped with the reason:

  preflight   SDLC_IMAGE, the org's and the repo's, is unset or a value the
              bootstrap takes (main, latest, X.Y.Z, @sha256:<64 hex>); no pull
              request is open from an earlier wave's branch, whose `rev:` line
              this rewrites out from under it
  convert     a shallow clone of the default branch, read from the API, never
              assumed; convert_repo.py's exit 2 is a refusal, an empty diff a
              no-op
  verify      convert_repo.py --apply, then a second run must find nothing to
              do, the bootstrap must be committed executable, and nothing
              outside the files the converter owns may have changed

The branch is one per repo and reused: a re-run force-pushes it and updates
the pull request already open on it rather than opening a second.

THE LEDGER is a TSV (repo, action, pr_url, reason), appended one row per repo
as it finishes, so a run that dies leaves what it did. A re-run reads it and
skips every repo already done (opened, updated, noop, or would-* for a dry
run); refused, skipped and failed repos are tried again. Dry runs and applied
runs keep separate ledgers by default, so a plan never reads as done.

Sequential, with a pause between repos: no parallel pushes.

Credentials: the API token comes from the environment variable --token-env
names (GH_TOKEN by default), goes in a header, and is never printed or taken as
an argument. Without it a dry run reads what anonymous API calls can and says
which checks it could not make. git pushes with whatever credentials git
already has (on a seat, the gateway's git route).

Exit 0 when every repo reached an outcome, 1 when one failed or was left
without auto-merge (rerun to retry them), 2 when the wave could not run.

Usage: scripts/fleet_wave.py [--repos FILE] [--apply] [--ledger TSV] [--sleep S]
                             [--token-env NAME]
"""

from __future__ import annotations

import argparse
import http.client
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any

ORG = "crunchtools"
# The converter's own repo, the source and not a consumer, under either name.
SELF = {"sdlc", "python-sdlc"}
BRANCH = "chore/sdlc-floating"
# Branches of earlier waves whose open pull requests edit the `rev:` line this
# wave deletes: one merging after the conversion would conflict, and a
# conflicting PR runs no checks, so it would sit looking green forever.
EARLIER_WAVES = ("chore/python-sdlc",)
CONVERTER = Path(__file__).resolve().parent / "convert_repo.py"
API_HOST = "api.github.com"
API_TIMEOUT = 30  # seconds, per call
CLONE_BASE = "https://github.com"
# What convert_repo.py may write. Anything else changed in the checkout is a
# converter that has grown a new reach, which a fleet wave is not the place to
# find out about.
CONVERTER_OWNS = re.compile(
    r"^(\.sdlc/bin/sdlc|\.pre-commit-config\.yaml|\.github/workflows/[^/]+\.ya?ml"
    r"|pyproject\.toml|sdlc\.toml)$"
)
# bin/sdlc-bootstrap's own rule, which refuses anything else with exit 2.
FLEET_IMAGE = "quay.io/crunchtools/sdlc"
VALID_IMAGE = re.compile(r"^(main|latest|\d+\.\d+\.\d+|@sha256:[0-9a-f]{64})$")

REFUSED = 2  # convert_repo.py: a shape it will not guess at
PAGE = 100
UNANSWERED = {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN, HTTPStatus.TOO_MANY_REQUESTS}

DONE = {"opened", "updated", "noop", "would-open", "would-update"}
LEDGER_HEADER = ("repo", "action", "pr_url", "reason")

COMMIT_MESSAGE = """\
chore: float the sdlc gates on quay.io/crunchtools/sdlc

Converted by crunchtools/sdlc's scripts/convert_repo.py. The hooks and
the `sdlc ci` job run .sdlc/bin/sdlc, which takes its launcher from
quay.io/crunchtools/sdlc:latest, so nothing in this repo pins an sdlc
release and no version-bump pull request follows this one. What still
named crunchtools/python-sdlc, the repo's old name, now names
crunchtools/sdlc.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
"""
PR_TITLE = COMMIT_MESSAGE.splitlines()[0]
PR_BODY = """\
The last fleet-wide PR wave: this repo's gates move to the floating sdlc, so \
what they run changes when the image does and no pull request has to bump a pin.

## What changed

- `.sdlc/bin/sdlc` (new or refreshed, executable): crunchtools/sdlc's \
`bin/sdlc-bootstrap`. It asks `quay.io/crunchtools/sdlc` for its launcher, \
checks its signature, and runs it. It names no release, so no pin bumps it.
- `.pre-commit-config.yaml`: the rev-pinned sdlc hooks (or the \
standalone Gourmand and Gatehouse hooks) become two local hooks running \
`.sdlc/bin/sdlc commit` and `.sdlc/bin/sdlc push`. Other hooks are kept.
- `.github/workflows/`: the `sdlc ci` job runs `.sdlc/bin/sdlc ci` with \
`SDLC_IMAGE: ${{ vars.SDLC_IMAGE }}`. Its name, and so the required check, is \
unchanged. Workflows the stub does not cover are kept.
- The old name: the repo was `crunchtools/python-sdlc`. \
`quay.io/crunchtools/python-sdlc`, now a mirror that goes away, becomes \
`quay.io/crunchtools/sdlc`, and a `uses: crunchtools/python-sdlc/...` \
becomes `crunchtools/sdlc/...`, in workflows and `[tool.sdlc]` config.
- A repo that already floats gets only the current bootstrap and stubs, and the \
rename.
- `.specify/` is not touched.

## How to roll back

- Before it merges: close this pull request.
- A bad image, not this change: set the repository variable `SDLC_IMAGE` to \
the last good release (`X.Y.Z`). CI uses it on its next run, with no pull \
request; the organization variable does the same fleet-wide. Locally, \
`SDLC_IMAGE=X.Y.Z git push`.
- This change itself: `git revert` its squash commit on the default branch. \
That restores the pinned hooks and workflows exactly as they were.

It merges itself (squash) when its required checks pass. Opened by \
crunchtools/sdlc's `scripts/fleet_wave.py`; a re-run of the wave reuses the branch \
`chore/sdlc-floating` and this pull request.
"""


class ApiError(Exception):
    """The API could not answer: network, auth, rate limit, or a 5xx."""


class LeftAloneError(Exception):
    """A repo this wave leaves alone, with the action and reason to record."""

    def __init__(self, action: str, reason: str) -> None:
        super().__init__(reason)
        self.action = action
        self.reason = reason


# ------------------------------------------------------------------- the API


class Api:
    """GitHub's REST and GraphQL APIs. The token lives in a header, nowhere else."""

    def __init__(self, token: str | None, connect: Any = None) -> None:
        self._token = token
        self._connect = connect or (
            lambda: http.client.HTTPSConnection(API_HOST, timeout=API_TIMEOUT)
        )

    @property
    def authenticated(self) -> bool:
        return bool(self._token)

    def redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        """(status, decoded body). 4xx other than 401/403/429 is returned, not raised."""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "crunchtools-fleet-wave",
            **({"Authorization": f"Bearer {self._token}"} if self._token else {}),
        }
        conn = self._connect()
        try:
            conn.request(method, path, None if body is None else json.dumps(body), headers)
            response = conn.getresponse()
            status, raw = response.status, response.read()
        except (OSError, http.client.HTTPException) as exc:
            raise ApiError(self.redact(f"{method} {path}: {exc}")) from exc
        finally:
            conn.close()
        decoded = json.loads(raw) if raw.strip() else None
        if status in UNANSWERED or status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            message = decoded.get("message", "") if isinstance(decoded, dict) else ""
            raise ApiError(self.redact(f"{method} {path}: HTTP {status} {message}".rstrip()))
        return status, decoded


def org_repos(api: Api) -> dict[str, str]:
    """Every public, non-archived repo: name -> its default branch, as the API says."""
    repos: dict[str, str] = {}
    for page in range(1, 50):
        status, page_repos = api.call(
            "GET", f"/orgs/{ORG}/repos?type=public&per_page={PAGE}&page={page}"
        )
        if status != HTTPStatus.OK or not isinstance(page_repos, list):
            raise ApiError(f"listing {ORG}'s repos: HTTP {status}")
        for repo in page_repos:
            if not repo["archived"] and not repo["private"]:
                repos[repo["name"]] = repo["default_branch"]
        if len(page_repos) < PAGE:
            break
    return repos


def open_prs_from(api: Api, branch: str) -> dict[str, dict]:
    """Open pull requests from `branch`, by repo: one search for the whole org."""
    found: dict[str, dict] = {}
    query = urllib.parse.quote(f"org:{ORG} is:pr is:open head:{branch}")
    for page in range(1, 11):
        status, hits = api.call("GET", f"/search/issues?q={query}&per_page={PAGE}&page={page}")
        if status != HTTPStatus.OK or not isinstance(hits, dict):
            raise ApiError(f"searching for open pull requests from {branch}: HTTP {status}")
        for item in hits["items"]:
            repo = item["repository_url"].rsplit("/", 1)[1]
            found[repo] = {"number": item["number"], "url": item["html_url"]}
        if len(hits["items"]) < PAGE:
            break
    return found


def sdlc_image(api: Api, path: str) -> str | None:
    """The SDLC_IMAGE variable at `path`, or None when it is unset."""
    status, variable = api.call("GET", f"{path}/actions/variables/SDLC_IMAGE")
    if status == HTTPStatus.NOT_FOUND:
        return None
    if status != HTTPStatus.OK or not isinstance(variable, dict):
        raise ApiError(f"reading SDLC_IMAGE at {path}: HTTP {status}")
    return str(variable.get("value", ""))


def image_problem(value: str | None) -> str | None:
    """Why the bootstrap would refuse this SDLC_IMAGE, or None when it would not."""
    if not value:
        return None  # unset, or empty: `${SDLC_IMAGE:-latest}` reads both as latest
    # The bootstrap's two spellings with the repository named in full.
    bare = value.removeprefix(f"{FLEET_IMAGE}:")
    if bare.startswith(f"{FLEET_IMAGE}@"):
        bare = bare.removeprefix(FLEET_IMAGE)
    if VALID_IMAGE.match(bare):
        return None
    return f"SDLC_IMAGE={value!r} is not main, latest, X.Y.Z or @sha256:<64 hex>"


# ------------------------------------------------------------------- the repo


@dataclass
class Wave:
    api: Api
    apply: bool
    clone_base: str = CLONE_BASE
    org_image: str | None = None
    earlier: dict[str, str] = field(default_factory=dict)  # repo -> open PR url
    ours: dict[str, dict] = field(default_factory=dict)  # repo -> open PR on BRANCH
    notes: list[str] = field(default_factory=list)


def git(cwd: Path, *args: str, stdin: str | None = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], input=stdin, capture_output=True, text=True, check=False
    )
    if proc.returncode:
        verb = next(a for a in args if not a.startswith("-") and "=" not in a)
        raise RuntimeError(f"git {verb}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def converter(checkout: Path, *args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(CONVERTER), str(checkout), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def refusal(stderr: str) -> str:
    """convert_repo.py's own reason, without its prefix."""
    for line in stderr.splitlines():
        if "NOT CONVERTED:" in line:
            return line.split("NOT CONVERTED:", 1)[1].strip()
    return stderr.strip().splitlines()[-1] if stderr.strip() else "refused, no reason given"


def preflight(wave: Wave, repo: str) -> None:
    if repo in SELF:
        raise LeftAloneError("skipped", "the converter's own repo, not a consumer")
    if repo in wave.earlier:
        raise LeftAloneError(
            "skipped", f"open pull request from an earlier wave: {wave.earlier[repo]}"
        )
    problem = image_problem(wave.org_image)
    if problem:
        raise LeftAloneError("skipped", f"organization {problem}")
    if not wave.api.authenticated:
        return  # a dry run without GH_TOKEN; main() has said which checks it skipped
    problem = image_problem(sdlc_image(wave.api, f"/repos/{ORG}/{repo}"))
    if problem:
        raise LeftAloneError("skipped", f"repository {problem}")


def convert(wave: Wave, repo: str, default: str, checkout: Path) -> list[str]:
    """Clone, convert, verify. The paths changed, staged in the checkout."""
    source = f"{wave.clone_base}/{ORG}/{repo}"
    git(
        checkout.parent,
        "clone",
        "-q",
        "--depth",
        "1",
        "--single-branch",
        "--branch",
        default,
        source,
        checkout.name,
    )
    code, diff, err = converter(checkout)
    if code == REFUSED:
        raise LeftAloneError("refused", refusal(err))
    if code:
        raise RuntimeError(f"convert_repo.py exited {code}: {err.strip()[-300:]}")
    # The diff shows no modes: a bootstrap committed without +x is still work.
    if not diff and bootstrap_mode(checkout) == "100755":
        raise LeftAloneError("noop", "already floating")
    code, _, err = converter(checkout, "--apply")
    if code:
        raise RuntimeError(f"convert_repo.py --apply exited {code}: {err.strip()[-300:]}")
    return verify(checkout)


def verify(checkout: Path) -> list[str]:
    """The paths the conversion changed, staged; or every way it is not fit to ship."""
    code, diff, _ = converter(checkout)
    git(checkout, "add", "-A")
    changed = git(checkout, "diff", "--cached", "--name-only").split()
    mode = bootstrap_mode(checkout)
    problems = [
        f"changed {p}, which it does not own" for p in changed if not CONVERTER_OWNS.match(p)
    ]
    if code or diff:
        problems.append(f"is not a fixed point: a second convert_repo.py run exits {code}")
    if mode != "100755":
        problems.append(f"would commit .sdlc/bin/sdlc as mode {mode or 'absent'}")
    if problems:
        raise RuntimeError("the conversion " + "; ".join(problems))
    return changed


def bootstrap_mode(checkout: Path) -> str:
    """The bootstrap's mode in the index, or "" when it is not there."""
    return (git(checkout, "ls-files", "-s", "--", ".sdlc/bin/sdlc").split() or [""])[0]


def ship(wave: Wave, repo: str, default: str, checkout: Path) -> tuple[str, str, str]:
    """Commit, push, open or update the pull request, queue auto-merge."""
    git(checkout, "checkout", "-q", "-B", BRANCH)
    # No hooks: whatever the operator's git config installs is not this repo's
    # gate; the pull request's own CI is.
    git(
        checkout,
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "user.name=crunchtools-ci",
        "-c",
        "user.email=ci@crunchtools.com",
        "commit",
        "-q",
        "-F",
        "-",
        stdin=COMMIT_MESSAGE,
    )
    # Forced: the branch is this wave's own, one per repo, and carries one
    # commit rebuilt from the default branch on every run.
    git(checkout, "push", "-q", "--force", "origin", f"HEAD:refs/heads/{BRANCH}")
    pr, action = open_or_update(wave.api, repo, default)
    problem = auto_merge(wave.api, pr["node_id"])
    if problem:
        return "no-automerge", pr["html_url"], f"auto-merge not enabled: {problem}"
    return action, pr["html_url"], ""


def message(reply: Any) -> str:
    if not isinstance(reply, dict):
        return ""
    details = [e.get("message", "") for e in reply.get("errors") or [] if isinstance(e, dict)]
    return "; ".join(filter(None, [reply.get("message", ""), *details]))


def open_or_update(api: Api, repo: str, default: str) -> tuple[dict, str]:
    """The pull request from BRANCH: opened, or the open one updated in place."""
    pulls = f"/repos/{ORG}/{repo}/pulls"
    fields = {"title": PR_TITLE, "body": PR_BODY}
    status, pull = api.call("POST", pulls, {**fields, "head": BRANCH, "base": default})
    if status == HTTPStatus.CREATED:
        return pull, "opened"
    if status == HTTPStatus.UNPROCESSABLE_ENTITY and "already exists" in message(pull):
        head = urllib.parse.quote(f"{ORG}:{BRANCH}")
        status, found = api.call("GET", f"{pulls}?head={head}&state=open")
        if status == HTTPStatus.OK and found:
            status, pull = api.call("PATCH", f"{pulls}/{found[0]['number']}", fields)
            if status == HTTPStatus.OK:
                return pull, "updated"
    raise RuntimeError(f"opening the pull request: HTTP {status} {message(pull)}".rstrip())


AUTO_MERGE = """\
mutation($id: ID!) {
  enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: SQUASH}) {
    pullRequest { autoMergeRequest { mergeMethod } }
  }
}"""


def auto_merge(api: Api, node_id: str) -> str | None:
    """Queue a squash merge; None when queued, else why not."""
    try:
        status, reply = api.call(
            "POST", "/graphql", {"query": AUTO_MERGE, "variables": {"id": node_id}}
        )
    except ApiError as exc:
        return str(exc)
    if status != HTTPStatus.OK or not isinstance(reply, dict):
        return f"HTTP {status} {message(reply)}".rstrip()
    if reply.get("errors"):
        return message(reply)
    return None


def process(wave: Wave, repo: str, default: str) -> tuple[str, str, str]:
    """One repo, start to finish: (action, pr_url, reason). Never raises for the repo's sake."""
    ours = wave.ours.get(repo, {}).get("url", "")
    try:
        preflight(wave, repo)
        with tempfile.TemporaryDirectory(prefix="fleet-wave-") as workdir:
            checkout = Path(workdir) / repo
            changed = convert(wave, repo, default, checkout)
            if wave.apply:
                return ship(wave, repo, default, checkout)
            return "would-update" if ours else "would-open", ours, ", ".join(changed)
    except LeftAloneError as left:
        return left.action, ours, left.reason
    except (RuntimeError, ApiError, OSError) as exc:
        return "failed", ours, wave.api.redact(str(exc))


# ----------------------------------------------------------------- the ledger


def read_ledger(path: Path) -> dict[str, str]:
    """Each repo's last recorded action."""
    if not path.exists():
        return {}
    last: dict[str, str] = {}
    for line in path.read_text().splitlines()[1:]:
        repo, _, rest = line.partition("\t")
        if rest:
            last[repo] = rest.split("\t", 1)[0]
    return last


def record(path: Path, row: tuple[str, str, str, str]) -> None:
    clean = [re.sub(r"[\t\r\n]+", " ", cell) for cell in row]
    new = not path.exists()
    with path.open("a") as ledger:
        if new:
            ledger.write("\t".join(LEDGER_HEADER) + "\n")
        ledger.write("\t".join(clean) + "\n")


# ------------------------------------------------------------------- the wave


def wanted(path: Path) -> list[str]:
    """The repo names a --repos file lists: one per line, `#` comments, org/ optional."""
    names = []
    for line in path.read_text().splitlines():
        name = line.split("#", 1)[0].strip().removeprefix(f"{ORG}/")
        if name:
            names.append(name)
    return names


def survey(api: Api, apply: bool) -> Wave:
    """What the whole wave needs to know before the first repo: a few calls, not one per repo."""
    wave = Wave(api, apply)
    wave.earlier = {
        repo: pr["url"]
        for branch in EARLIER_WAVES
        for repo, pr in open_prs_from(api, branch).items()
    }
    wave.ours = open_prs_from(api, BRANCH)
    if api.authenticated:
        wave.org_image = sdlc_image(api, f"/orgs/{ORG}")
    else:
        wave.notes.append("no API token: SDLC_IMAGE (organization and repository) not checked")
    return wave


def run(
    wave: Wave, names: list[str], available: dict[str, str], ledger: Path, pause: float
) -> dict[str, int]:
    """Process `names` in order, one at a time, `pause` seconds apart.

    `available` is the org listing (name -> default branch); a name not in it is
    skipped. Each repo's outcome is appended to `ledger` as it finishes, and a
    repo whose last ledger action is in DONE is not processed again. Returns
    how many repos ended in each action, plus "already done" for those passed
    over -- so the counts always add up to len(names).
    """
    done = read_ledger(ledger)
    counts: dict[str, int] = {}
    for n, repo in enumerate(names):
        if done.get(repo) in DONE:
            counts["already done"] = counts.get("already done", 0) + 1
            continue
        if repo not in available:
            row = ("skipped", "", f"not a public, non-archived repo in {ORG}")
        else:
            row = process(wave, repo, available[repo])
            if n < len(names) - 1:
                time.sleep(pause)
        record(ledger, (repo, *row))
        counts[row[0]] = counts.get(row[0], 0) + 1
        print("\t".join((repo, *row)).rstrip("\t"), flush=True)
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repos", type=Path, help="a file of repo names, one per line; default all")
    ap.add_argument("--apply", action="store_true", help="push and open PRs; default plans only")
    ap.add_argument("--ledger", type=Path, help="TSV; default fleet-wave[-plan].tsv here")
    ap.add_argument("--sleep", type=float, default=3.0, help="seconds between repos")
    ap.add_argument(
        "--token-env",
        default="GH_TOKEN",
        metavar="NAME",
        help="the environment variable holding the GitHub API token (default %(default)s; "
        "required with --apply). The token itself is never an argument",
    )
    args = ap.parse_args()
    ledger = args.ledger or Path("fleet-wave.tsv" if args.apply else "fleet-wave-plan.tsv")
    token = os.environ.get(args.token_env, "")
    if args.apply and not token:
        print(f"fleet_wave: --apply needs {args.token_env} in the environment", file=sys.stderr)
        return 2
    # convert_repo.py runs under this interpreter. Without PyYAML every repo
    # would come back refused, which reads as a plan.
    if importlib.util.find_spec("yaml") is None:
        print(f"fleet_wave: convert_repo.py needs PyYAML in {sys.executable}", file=sys.stderr)
        return 2
    api = Api(token)
    try:
        available = org_repos(api)
        names = wanted(args.repos) if args.repos else sorted(available)
        wave = survey(api, args.apply)
    except (ApiError, OSError) as exc:
        print(f"fleet_wave: CANNOT RUN: {api.redact(str(exc))}", file=sys.stderr)
        return 2
    for note in wave.notes:
        print(f"fleet_wave: note: {note}", file=sys.stderr)
    counts = run(wave, names, available, ledger, args.sleep)
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    print(f"fleet_wave: {len(names)} repo(s): {summary or 'nothing to do'}; ledger {ledger}")
    return 1 if counts.get("failed") or counts.get("no-automerge") else 0


if __name__ == "__main__":
    sys.exit(main())
