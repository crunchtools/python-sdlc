#!/usr/bin/env python3
"""The fleet cutover (sdlc#88): what one consumer repo needs beyond convert_repo.py.

Two halves. The offline half, which scripts/fleet_wave.py --cutover runs over a
checkout after convert_repo.py, so each repo gets one pull request that carries
every change at once:

  constitution     `Inherits:` in .specify/memory/constitution.md and the
                   constitution hook `rev:` move to the target release, together,
                   as fleet-bump.py moves them. No workflow carries a pin any more
  Dependabot       .github/dependabot.yml and its auto-merge caller go, by the
                   target constitution's own fleet-bump.py: from v1.60.0 the
                   validator fails a repo that still has them
  the old name     a bare `python-sdlc` left in a workflow, the hooks file or
                   the sdlc config becomes `sdlc`
  .gemini/         deleted when the caller says the org no longer has the app
  uses:            any `uses:` left in a workflow is hand work: the fleet file
                   has none

The online half is the CLI, run by Josui Prime with an admin token:

  cutover.py --merge REPO --pr N     merge the conversion PR, then move the repo to
                                     the new ruleset and off the moved org secrets
  cutover.py --move-rulesets         the idempotent ruleset move, alone
  cutover.py --check                 prerequisites 3 and 12, over the converted fleet

A repo is converted when its default branch carries .github/workflows/ci.yml
byte for byte equal to examples/ci.yml. The ruleset move computes both rulesets'
repo lists from that, so running it twice, or after a crash between the merge and
the move, gives the same rulesets. Exit 0 ok, 1 problems or refused, 2 could not run.

What it will not guess at goes to the `hand` list, and the rest of the repo is
still converted. The offline half is deterministic: the same checkout and target
give the same result, and a second run finds nothing to do. Edits are made by
line, never by a YAML writer.
"""

from __future__ import annotations

import argparse
import base64
import csv
import fnmatch
import http.client
import importlib.util
import json
import os
import re
import shutil
import sys
import tomllib
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from types import ModuleType
from typing import Any

try:
    import yaml
except ImportError:  # fleet_wave.py's main() says so, before any repo
    HAS_YAML = False
else:
    HAS_YAML = True

ROOT = Path(__file__).resolve().parent.parent
FLEET_FILE = ROOT / "examples" / "ci.yml"

ORG = "crunchtools"
BRANCH = "chore/fleet-cutover"  # the conversion PR's head
CI = ".github/workflows/ci.yml"
SELF = {"sdlc"}  # its own ci.yml is the one exception to the fleet file
OLD_RULESET = "Python SDLC gates"
NEW_RULESET = "sdlc fleet CI"
ACTIONS_APP = 15368  # GitHub Actions, as a ruleset's integration_id
RELEASE_REF = "refs/heads/ashigaru/release-*"
RELEASE_PREFIX = "ashigaru/release-"
REQUIRED_CHECKS = ("sdlc / check (pull_request)", "sdlc / gate (pull_request_target)")
# The org secrets that moved into environments (prerequisite 2). Not named
# *_SECRETS: CodeQL reads that name as sensitive data and flags every message
# that carries one of these names (py/clear-text-logging-sensitive-data).
MOVED_TO_ENVIRONMENTS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENROUTER_API_KEY",
    "ASHIGARU_APP_KEY",
    "QUAY_USERNAME",
    "QUAY_PASSWORD",
    "RHSM_ACTIVATION_KEY",
    "RHSM_ORG_ID",
    "REGISTRY_REDHAT_IO_USER",
    "REGISTRY_REDHAT_IO_PASSWORD",
    "COSIGN_PRIVATE_KEY",
    "COSIGN_PASSWORD",
)

API_HOST = "api.github.com"
API_TIMEOUT = 30  # seconds, per call
PAGE = 100
MAX_PAGES = 50
UNANSWERED = {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN, HTTPStatus.TOO_MANY_REQUESTS}

MANIFEST = Path(".specify/memory/constitution.md")
HOOKS = Path(".pre-commit-config.yaml")
WORKFLOWS = Path(".github/workflows")
GEMINI = Path(".gemini")
CONFIGS = (Path("pyproject.toml"), Path("sdlc.toml"))

INHERITS = re.compile(r"^(>\s*\*\*Inherits:\*\*.*?)v\d+\.\d+\.\d+", re.M)
HOOK_REV = re.compile(
    r"(repo:\s*https://github\.com/crunchtools/constitution/?\s*\n\s*rev:\s*)v\d+\.\d+\.\d+"
)
# The repo's old name as a word: not python-sdlc-extras, not a domain, but a
# sentence's last word is still one.
OLD_NAME = re.compile(r"(?<![\w.-])python-sdlc(?![\w-]|\.[\w-])")
USES = re.compile(r"(?:^|[\s{,])uses:\s*(\S+)")


@dataclass(frozen=True)
class Target:
    """What the fleet moves to: the release each repo is judged by, and its reach."""

    constitution: str  # X.Y.Z, for Inherits and the hook rev
    bump: ModuleType | None = None  # the target constitution's scripts/fleet-bump.py
    validator: ModuleType | None = None  # and its validate-constitution.py
    drop_gemini: bool = False  # the org no longer has Gemini Code Assist: .gemini/ goes


@dataclass
class Result:
    """What cutover() did to one checkout, for fleet_wave.py's report and PR body.

    `changed` holds one line per edit it made and wrote. `hand` holds what it
    would not do safely -- each line names the file and what a human must do --
    and a repo with any is listed as hand work; the rest of its edits still ship.
    """

    changed: list[str] = field(default_factory=list)  # one line per change, for the report
    hand: list[str] = field(default_factory=list)  # what it would not guess at
    uses_left: list[str] = field(default_factory=list)  # every `uses:` still in a workflow


def load(path: Path, name: str) -> ModuleType:
    """A script by path: fleet-bump.py and validate-constitution.py have hyphens."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise OSError(f"{path}: not a loadable Python file")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def constitution_modules(checkout: Path) -> tuple[ModuleType, ModuleType]:
    """(fleet-bump, validator) from a constitution checkout at the target release."""
    sys.path.insert(0, str(checkout / "scripts"))  # fleet-bump imports fleet_common
    return (
        load(checkout / "scripts" / "fleet-bump.py", "fleet_bump"),
        load(checkout / "validate-constitution.py", "validate_constitution"),
    )


# ----------------------------------------------------------- the offline half


def rel(repo: Path, path: Path) -> str:
    return str(path.relative_to(repo))


def move_constitution(repo: Path, target: Target, result: Result) -> None:
    """Inherits and the hook rev, together."""
    for path in (repo / MANIFEST, repo / HOOKS):
        if not path.is_file():
            continue
        text = path.read_text()
        new = text
        if path.name == MANIFEST.name:
            new = INHERITS.sub(rf"\g<1>v{target.constitution}", new)
        if path.name == HOOKS.name:
            new = HOOK_REV.sub(rf"\g<1>v{target.constitution}", new)
        if new != text:
            path.write_text(new)
            result.changed.append(f"constitution: v{target.constitution} in {rel(repo, path)}")


def uses_left(repo: Path) -> list[str]:
    """Each `uses:` still in a workflow, comments excluded: the fleet file has none."""
    left = []
    for path in sorted((repo / WORKFLOWS).glob("*.y*ml")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for used in USES.findall(line):
                left.append(f"{rel(repo, path)}:{n} uses {used}")
    return left


def drop_dependabot(repo: Path, target: Target, result: Result) -> None:
    """The target constitution's own Renovate teardown, so the two never disagree."""
    if target.bump is None or target.validator is None:
        return
    try:
        changes = target.bump.drop_dependabot(repo, target.validator)
    except RuntimeError as exc:
        result.hand.append(f"Dependabot: {exc}")
        return
    result.changed += [f"Dependabot: {c}" for c in changes]


def rename(repo: Path, result: Result) -> None:
    paths = [repo / HOOKS, *(repo / c for c in CONFIGS)]
    paths += sorted((repo / WORKFLOWS).glob("*.y*ml"))
    for path in paths:
        if not path.is_file():
            continue
        text = path.read_text()
        new = OLD_NAME.sub("sdlc", text)
        if new != text:
            path.write_text(new)
            result.changed.append(f"renamed: python-sdlc to sdlc in {rel(repo, path)}")


def remove_gemini(repo: Path, target: Target, result: Result) -> None:
    """Gemini Code Assist's config, when the org no longer has the app."""
    root = repo / GEMINI
    if not target.drop_gemini or not (root.exists() or root.is_symlink()):
        return
    paths = [root, *root.rglob("*")] if root.is_dir() and not root.is_symlink() else [root]
    links = [rel(repo, p) for p in paths if p.is_symlink()]
    if links or not root.is_dir():
        # A checkout is someone else's content: a link could point outside it.
        why = f"{', '.join(links)} is a symlink" if links else f"{GEMINI} is not a directory"
        result.hand.append(f"gemini: {why}; this script deletes no links; remove {GEMINI}/ by hand")
        return
    files = sum(p.is_file() for p in paths)
    shutil.rmtree(root)
    result.changed.append(f"gemini: removed {GEMINI}/ ({files} file(s))")


def cutover(repo: Path, target: Target) -> Result:
    """Apply every cutover change to the checkout at `repo`, in place."""
    result = Result()
    move_constitution(repo, target, result)
    drop_dependabot(repo, target, result)
    rename(repo, result)
    remove_gemini(repo, target, result)
    result.uses_left = uses_left(repo)
    if result.uses_left:
        result.hand.append(
            f"workflows: {len(result.uses_left)} `uses:` left, and the fleet file has none: "
            + ", ".join(result.uses_left)
        )
    return result


def validate(repo: Path, target: Target) -> list[str]:
    """The target constitution's verdict on the converted checkout; [] when it passes."""
    if target.validator is None:
        return []
    if not (repo / MANIFEST).is_file():
        # Nothing to judge is not a pass: a repo the validator cannot read ships unjudged.
        return [f"MANIFEST: no {MANIFEST}, so the target constitution cannot judge it"]
    mine = target.validator.own_version() or ""
    return [
        v
        for v in target.validator.validate(repo / MANIFEST)
        # A dry run ahead of the release judges with a validator one version short.
        if not (v.startswith("PIN:") and "newer than this validator" in v and mine)
    ]


# ------------------------------------------------------------------- the API


class ApiError(Exception):
    """The API could not answer: network, auth, rate limit, or a 5xx."""


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
        try:
            decoded = json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError as exc:
            raise ApiError(self.redact(f"{method} {path}: HTTP {status}, not JSON")) from exc
        if status in UNANSWERED or status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            message = decoded.get("message", "") if isinstance(decoded, dict) else ""
            raise ApiError(self.redact(f"{method} {path}: HTTP {status} {message}".rstrip()))
        return status, decoded


class RefusedError(Exception):
    """A precondition that failed: nothing was written (or, after the merge, what was)."""


def refuse_private(record: dict, include_private: bool) -> str | None:
    """Why a private repo is refused (prerequisite 9), or None when it may go."""
    if record.get("private") and not include_private:
        return (
            f"{record.get('full_name') or record.get('name')} is private; "
            f"pass --include-private once the org's plan covers it (prerequisite 9)"
        )
    return None


def must(api: Api, path: str) -> Any:
    """The decoded body of a GET that has to answer 200."""
    status, body = api.call("GET", path)
    if status != HTTPStatus.OK or body is None:
        raise ApiError(f"GET {path}: HTTP {status}")
    return body


def paged(api: Api, path: str, key: str | None = None) -> list[Any]:
    """Every item of a list endpoint; `key` names the list inside an object body."""
    sep = "&" if "?" in path else "?"
    items: list[Any] = []
    for page in range(1, MAX_PAGES + 1):
        status, body = api.call("GET", f"{path}{sep}per_page={PAGE}&page={page}")
        got = body.get(key) if key and isinstance(body, dict) else body
        if status != HTTPStatus.OK or not isinstance(got, list):
            raise ApiError(f"GET {path}: HTTP {status}")
        items += got
        if len(got) < PAGE:
            return items
    raise ApiError(f"GET {path}: more than {MAX_PAGES} pages")


def read_file(api: Api, repo: str, path: str, ref: str) -> bytes | None:
    """A file's bytes at `ref`, or None when the repo has none there."""
    quoted = urllib.parse.quote(ref, safe="")
    status, body = api.call("GET", f"/repos/{ORG}/{repo}/contents/{path}?ref={quoted}")
    if status == HTTPStatus.NOT_FOUND:
        return None
    if status != HTTPStatus.OK or not isinstance(body, dict) or "content" not in body:
        raise ApiError(f"reading {path} at {ORG}/{repo}@{ref}: HTTP {status}")
    return base64.b64decode(body["content"])


def fleet(api: Api) -> list[dict]:
    """Every non-archived repo of the org the token can see."""
    repos = paged(api, f"/orgs/{ORG}/repos?type=all")
    return [r for r in repos if not r["archived"]]


def is_converted(repo: str, have: bytes | None) -> bool:
    """The fleet file byte for byte; for sdlc, its own file, the stated exception.

    sdlc's ci.yml counts when it reports both required checks and its `gate`
    is the fleet's: its `test` job carries the check name, and its `ai`,
    `write` and `gate` are tests/test_workflows.py's to pin.
    """
    want = FLEET_FILE.read_bytes()
    if have is None or repo not in SELF:
        return have == want
    try:
        jobs = (yaml.safe_load(have) or {}).get("jobs") or {}
    except yaml.YAMLError:
        return False
    fleet_jobs = yaml.safe_load(want)["jobs"]
    names = {str(j.get("name")) for j in jobs.values() if isinstance(j, dict)}
    return {fleet_jobs["check"]["name"], fleet_jobs["gate"]["name"]} <= names and jobs.get(
        "gate"
    ) == fleet_jobs["gate"]


BOOT_PIN = re.compile(r"sdlc-bootstrap@sha256:[0-9a-f]{64}\b")


def unpinned(text: str) -> bool:
    """The fleet file names sdlc-bootstrap by anything but a real digest."""
    return "sdlc-bootstrap" in text and not BOOT_PIN.search(text)


def live_names(api: Api) -> set[str]:
    """Every non-archived repo of the org: a name on a ruleset list and not here is gone."""
    return {r["name"] for r in fleet(api)}


def converted_repos(api: Api, include_private: bool) -> set[str]:
    """The repos whose default branch carries the fleet file, byte for byte.

    A private repo is never counted unless --include-private: it stays on the old list.
    """
    return {
        r["name"]
        for r in fleet(api)
        if not refuse_private(r, include_private)
        and is_converted(r["name"], read_file(api, r["name"], CI, r["default_branch"]))
    }


# ---------------------------------------------------------- the ruleset move


@dataclass
class Put:
    """One ruleset update: its id and the whole `conditions` to send."""

    name: str
    id: int
    conditions: dict
    line: str


def org_rulesets(api: Api) -> list[dict]:
    """Every org ruleset, whole: the listing has no conditions, so each is read by id."""
    listed = paged(api, f"/orgs/{ORG}/rulesets")
    return [must(api, f"/orgs/{ORG}/rulesets/{r['id']}") for r in listed]


def explicit_include(ruleset: dict) -> list[str]:
    include = (ruleset.get("conditions") or {}).get("repository_name", {}).get("include")
    ok = isinstance(include, list) and all(
        isinstance(n, str) and n != "~ALL" and not re.search(r"[*?\[]", n) for n in include
    )
    if not ok:  # empty is explicit: the new list before the first canary, an old one after the wave
        raise RefusedError(
            f"ruleset {ruleset.get('name')!r} does not target repos by an explicit "
            f"conditions.repository_name.include list (no ~ALL, no wildcards); the move "
            f"cannot edit it. Prerequisite: list the repos by name"
        )
    return list(include)


def requires_checks(ruleset: dict) -> bool:
    have = {
        (c.get("context"), c.get("integration_id"))
        for rule in ruleset.get("rules") or []
        if rule.get("type") == "required_status_checks"
        for c in (rule.get("parameters") or {}).get("required_status_checks") or []
    }
    return all((check, ACTIONS_APP) in have for check in REQUIRED_CHECKS)


def by_name(rulesets: list[dict], name: str) -> dict:
    found = [r for r in rulesets if r.get("name") == name]
    if len(found) != 1:
        raise RefusedError(f"expected one org ruleset named {name!r}, found {len(found)}")
    return found[0]


def plan_move(
    rulesets: list[dict],
    converted: set[str],
    old_names: list[str],
    new_name: str,
    live: set[str] | None = None,
) -> list[Put]:
    """The PUTs that make the new list the converted repos and each old list the rest.

    Refuses, before anything is written, when a ruleset cannot be edited safely,
    when a repo on the new list is no longer converted (a revert), when a repo
    an old list would drop is not on the new one (a required check that never
    reports blocks every merge silently), or when a PUT would send an empty
    list to an active ruleset: GitHub may read an empty include as every repo,
    which would require checks nothing reports anywhere. A name not in `live`
    (archived or deleted) leaves both lists and is never read as a revert.
    """
    new = by_name(rulesets, new_name)
    olds = [by_name(rulesets, n) for n in old_names]
    gone = set() if live is None else {n for r in [new, *olds] for n in explicit_include(r)} - live
    new_have = explicit_include(new)
    old_have = [explicit_include(r) for r in olds]
    if not requires_checks(new):
        raise RefusedError(
            f"ruleset {new_name!r} must require {' and '.join(REQUIRED_CHECKS)} "
            f"with GitHub Actions (integration {ACTIONS_APP}) as the source"
        )
    reverted = sorted(set(new_have) - converted - gone)
    if reverted:
        raise RefusedError(
            f"{', '.join(reverted)} on {new_name!r} but the default branch no longer "
            f"carries the fleet ci.yml (reverted?); fix or remove it first"
        )
    new_want = sorted(converted)
    plan: list[tuple[dict, list[str], list[str]]] = [(new, new_have, new_want)]
    for ruleset, have in zip(olds, old_have, strict=True):
        want = [n for n in have if n not in converted and n not in gone]
        dropped = sorted(set(have) - set(want) - gone)
        if not set(dropped) <= set(new_want):
            raise RefusedError(
                f"{', '.join(dropped)} would leave {ruleset['name']!r} but not join {new_name!r}"
            )
        plan.append((ruleset, have, want))
    puts = []
    for ruleset, have, want in plan:
        if have == want:
            continue
        if not want:
            if ruleset is not new and ruleset.get("enforcement") != "active":
                continue  # emptied and disabled: never PUT an empty list, nothing enforces it
            raise RefusedError(
                f"ruleset {ruleset['name']!r} would be left with no repos, and GitHub may "
                f"read an empty include as every repo; set its enforcement to disabled by "
                f"hand, then re-run"
            )
        conditions = json.loads(json.dumps(ruleset["conditions"]))
        conditions["repository_name"]["include"] = want
        added, removed = sorted(set(want) - set(have)), sorted(set(have) - set(want))
        line = f"ruleset {ruleset['name']!r}: +{len(added)} -{len(removed)} ({len(want)} repos)"
        puts.append(Put(ruleset["name"], ruleset["id"], conditions, line))
    return puts


def apply_move(api: Api, puts: list[Put], say: Callable[[str], None]) -> None:
    for put in puts:  # the new ruleset is first, so a repo is never on neither
        path = f"/orgs/{ORG}/rulesets/{put.id}"
        status, _ = api.call("PUT", path, {"conditions": put.conditions})
        if status != HTTPStatus.OK:
            raise RefusedError(f"PUT ruleset {put.name!r}: HTTP {status}; a re-run is safe")
        say(put.line)


# ------------------------------------------------------------------ the merge


def pypi_project(api: Api, repo: str, sha: str) -> tuple[bool, str]:
    """(publishes to PyPI, the project's name) from the config at the head sha."""
    for config in CONFIGS:
        raw = read_file(api, repo, str(config), sha)
        if raw is not None:
            break
    else:
        return False, repo
    try:
        parsed = tomllib.loads(raw.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise RefusedError(f"{config} at {sha[:7]} is not TOML: {exc}") from exc
    publish = parsed.get("tool", {}).get("sdlc", {}).get("publish")
    return publish == "pypi", str(parsed.get("project", {}).get("name") or repo)


def ledger_row(path: str | None, repo: str) -> dict[str, str] | None:
    if path is None:
        return None
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            if row.get("repo") == repo:
                return row
    return None


def passing(runs: list[dict], check: str) -> bool:
    """The newest GitHub Actions run of `check` succeeded."""
    mine = [r for r in runs if r.get("name") == check]
    latest = max(mine, key=lambda r: r.get("id", 0), default=None)
    return (
        latest is not None
        and latest.get("conclusion") == "success"
        and (latest.get("app") or {}).get("slug") == "github-actions"
    )


@dataclass
class Options:
    include_private: bool = False
    ledger: str | None = None
    old: list[str] = field(default_factory=lambda: [OLD_RULESET])
    new: str = NEW_RULESET


@dataclass
class Run:
    """One --merge run: what every step needs, so the steps take one argument."""

    api: Api
    repo: str
    number: int
    opts: Options
    say: Callable[[str], None]
    record: dict = field(default_factory=dict)
    default: str = ""
    sha: str = ""

    @property
    def base(self) -> str:
        return f"/repos/{ORG}/{self.repo}"


def verify_repo(run: Run) -> None:
    """Step 1: the repo is public or allowed, and not archived."""
    run.record = must(run.api, run.base)
    reason = refuse_private(run.record, run.opts.include_private)
    if reason:
        raise RefusedError(reason)
    if run.record.get("archived"):
        raise RefusedError(f"{ORG}/{run.repo} is archived")
    run.say("1 repo: public or allowed, not archived")


def verify_pr(run: Run) -> bool:
    """Step 2: the PR is the conversion PR. Returns whether it is already merged."""
    number = run.number
    pr = must(run.api, f"{run.base}/pulls/{number}")
    run.default = run.record["default_branch"]
    head = pr.get("head") or {}
    run.sha = head.get("sha", "")
    if (
        head.get("ref") != BRANCH
        or (head.get("repo") or {}).get("full_name") != f"{ORG}/{run.repo}"
    ):
        raise RefusedError(f"PR #{number} is not from {BRANCH} in {ORG}/{run.repo}")
    if (pr.get("base") or {}).get("ref") != run.default:
        raise RefusedError(f"PR #{number} does not target {run.default}")
    if not pr.get("merged") and pr.get("state") != "open":
        raise RefusedError(f"PR #{number} is {pr.get('state')}, not open")
    if read_file(run.api, run.repo, CI, run.sha) != FLEET_FILE.read_bytes():
        raise RefusedError(f"PR #{number} head {run.sha[:7]} does not carry the fleet ci.yml")
    if pr.get("merged"):
        run.say(f"2 PR #{number}: already merged, with the fleet ci.yml; resuming at the merge")
        return True
    run.say(f"2 PR #{number}: open, from {BRANCH}, carries the fleet ci.yml")
    return False


def merge_pr(run: Run, already: bool) -> None:
    """Step 8: squash-merge at the sha the checks ran on, unless it is already merged."""
    if already:
        run.say("8 merge: skipped, already merged")
        return
    status, body = run.api.call(
        "PUT", f"{run.base}/pulls/{run.number}/merge", {"merge_method": "squash", "sha": run.sha}
    )
    if status != HTTPStatus.OK:
        note = body.get("message", "") if isinstance(body, dict) else ""
        raise RefusedError(f"merge of PR #{run.number}: HTTP {status} {note}".rstrip())
    run.say(f"8 merge: PR #{run.number} squashed at {run.sha[:7]}")


def move_rulesets(run: Run) -> None:
    """Step 9: the idempotent ruleset move.

    The contents API lags a squash merge, so a fresh read can miss the repo
    just merged, find both lists in step, and strand it on the old ruleset
    whose checks never report again. The merge (or the resume, which read the
    merged head's ci.yml) is the proof it is converted, so it is counted.
    """
    opts = run.opts
    done = converted_repos(run.api, opts.include_private) | {run.repo}
    puts = plan_move(org_rulesets(run.api), done, opts.old, opts.new, live_names(run.api))
    apply_move(run.api, puts, lambda line: run.say(f"9 {line}"))
    if not puts:
        run.say("9 rulesets: already in step")


def remove_secret_access(run: Run, problems: list[str]) -> None:
    """Step 10: take the repo off each moved org secret's list; failures join `problems`."""
    for name in MOVED_TO_ENVIRONMENTS:
        status, entry = run.api.call("GET", f"/orgs/{ORG}/actions/secrets/{name}")
        if status == HTTPStatus.NOT_FOUND:
            continue
        if status != HTTPStatus.OK or not isinstance(entry, dict):
            problems.append(f"org secret {name}: reading it gave HTTP {status}; re-run")
            continue
        if entry.get("visibility") != "selected":
            problems.append(f"org secret {name} is visible to {entry.get('visibility')}")
            continue
        status, _ = run.api.call(
            "DELETE", f"/orgs/{ORG}/actions/secrets/{name}/repositories/{run.record['id']}"
        )
        if status == HTTPStatus.NO_CONTENT:
            run.say(f"10 org secret {name}: {run.repo} removed from its list")
        else:
            problems.append(f"org secret {name}: removing {run.repo} gave HTTP {status}; re-run")


def update_bot_branches(run: Run, problems: list[str]) -> None:
    """Step 11: bring each open bot PR up to the default branch; failures join `problems`."""
    for other in open_prs(run.api, run.repo):
        if (other.get("user") or {}).get("type") != "Bot" or other["number"] == run.number:
            continue
        sha_other = (other.get("head") or {}).get("sha")
        status, _ = run.api.call(
            "PUT",
            f"{run.base}/pulls/{other['number']}/update-branch",
            {"expected_head_sha": sha_other},
        )
        if status == HTTPStatus.ACCEPTED:
            run.say(f"11 PR #{other['number']}: brought up to {run.default}")
        else:
            problems.append(f"PR #{other['number']}: update-branch gave HTTP {status}")


def merge(
    api: Api, repo: str, number: int, opts: Options, say: Callable[[str], None] = print
) -> list[str]:
    """Merge the conversion PR and move the repo. Raises RefusedError, writing nothing,
    on a failed precondition; returns the problems of the steps after the merge."""
    run = Run(api, repo, number, opts, say)
    verify_repo(run)
    already = verify_pr(run)
    if not already:
        preconditions(run)
    merge_pr(run, already)
    problems: list[str] = []
    move_rulesets(run)
    remove_secret_access(run, problems)
    update_bot_branches(run, problems)
    for problem in problems:
        say(f"problem: {problem}")
    return problems


def open_prs(api: Api, repo: str) -> list[dict]:
    return paged(api, f"/repos/{ORG}/{repo}/pulls?state=open")


def check_busy(run: Run) -> None:
    """Step 3: no person's open PR, no old-flow release PR."""
    busy = []
    for other in open_prs(run.api, run.repo):
        if other["number"] == run.number:
            continue
        ref = (other.get("head") or {}).get("ref", "")
        if ref.startswith(RELEASE_PREFIX):
            busy.append(f"#{other['number']} is an old-flow release PR ({ref})")
        elif (other.get("user") or {}).get("type") != "Bot":
            who = (other.get("user") or {}).get("login")
            busy.append(f"#{other['number']} is open work by {who}")
    if busy:
        raise RefusedError(f"{ORG}/{run.repo} is busy: " + "; ".join(busy))
    run.say("3 no person's open PR, no old release PR")


def check_pypi(run: Run) -> None:
    """Step 4: a PyPI repo has its trusted-publisher swap on the ledger."""
    publishes, project = pypi_project(run.api, run.repo, run.sha)
    if not publishes:
        run.say("4 PyPI: not a PyPI repo")
        return
    row = ledger_row(run.opts.ledger, run.repo)
    if not row or not row.get("added_by") or not row.get("removed_by"):
        raise RefusedError(
            f"{run.repo} publishes to PyPI as {project}: the ledger needs the new publisher "
            f"added and the old removed, with who did them. "
            f"https://pypi.org/manage/project/{project}/settings/publishing/"
        )
    run.say(f"4 PyPI {project}: ledger records both steps")


def check_secret_scope(run: Run) -> None:
    """Step 5: each moved org secret is scoped to selected repositories."""
    for name in MOVED_TO_ENVIRONMENTS:
        status, secret = run.api.call("GET", f"/orgs/{ORG}/actions/secrets/{name}")
        if status == HTTPStatus.NOT_FOUND:
            continue
        if status != HTTPStatus.OK or not isinstance(secret, dict):
            raise RefusedError(f"org secret {name}: HTTP {status}")
        if secret.get("visibility") != "selected":
            raise RefusedError(
                f"org secret {name} is visible to {secret.get('visibility')} repositories; "
                f"prerequisite 3: scope it to selected repositories"
            )
    run.say("5 org secrets: each is scoped to selected repositories")


def check_move_plans(run: Run) -> None:
    """Step 6: the ruleset move would go through, planned and not applied."""
    done = converted_repos(run.api, run.opts.include_private) | {run.repo}
    plan_move(org_rulesets(run.api), done, run.opts.old, run.opts.new, live_names(run.api))
    run.say("6 rulesets: the move would go through")


def check_runs(run: Run) -> None:
    """Step 7: the head commit has a passing Actions run of each required check."""
    runs = paged(run.api, f"{run.base}/commits/{run.sha}/check-runs", "check_runs")
    missing = [c for c in REQUIRED_CHECKS if not passing(runs, c)]
    if missing:
        raise RefusedError(
            f"{run.sha[:7]} has no passing GitHub Actions run of: {', '.join(missing)}"
        )
    run.say("7 check runs: both required checks passed")


def preconditions(run: Run) -> None:
    """Steps 3 to 7: each raises RefusedError, and none writes."""
    check_busy(run)
    check_pypi(run)
    check_secret_scope(run)
    check_move_plans(run)
    check_runs(run)


# ------------------------------------------------------------------- --check


def secret_names(api: Api, path: str) -> list[str]:
    key = "secrets"
    return [s["name"] for s in paged(api, path, key)]


def check_fleet(api: Api, opts: Options) -> list[str]:
    """Prerequisites 3 and 12, as problems; a refused read is a problem, not "none"."""
    problems: list[str] = []
    done = converted_repos(api, opts.include_private)
    for repo in sorted(done):
        for scope, path in (
            ("repo", "actions/secrets"),
            ("org", "actions/organization-secrets"),
        ):
            try:
                names = secret_names(api, f"/repos/{ORG}/{repo}/{path}")
            except ApiError as exc:
                problems.append(f"{repo}: could not read {scope} secrets: {exc}")
                continue
            problems += [
                f"{repo}: moved {scope} secret {n} is still readable"
                for n in sorted(names)
                if n in MOVED_TO_ENVIRONMENTS
            ]
    rulesets = org_rulesets(api)
    try:
        if plan_move(rulesets, done, opts.old, opts.new, live_names(api)):
            problems.append("rulesets out of step: run --move-rulesets")
    except RefusedError as exc:
        problems.append(f"rulesets out of step: {exc}")
    if not any(releases_guarded(r) for r in rulesets):
        problems.append(
            f"prerequisite 12: no active org ruleset covers {RELEASE_REF} "
            f"(conditions.ref_name.include)"
        )
    return problems


def releases_guarded(ruleset: dict) -> bool:
    include = (ruleset.get("conditions") or {}).get("ref_name", {}).get("include") or []
    return ruleset.get("enforcement") == "active" and any(
        i == "~ALL" or fnmatch.fnmatchcase(RELEASE_REF, i) for i in include
    )


# ------------------------------------------------------------------------ CLI


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="The fleet cutover's API half (sdlc#88).")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--merge", metavar="REPO", help="merge REPO's conversion PR and move it")
    mode.add_argument("--move-rulesets", action="store_true", help="the idempotent ruleset move")
    mode.add_argument("--check", action="store_true", help="prerequisites 3 and 12")
    p.add_argument("--pr", type=int, help="the conversion PR's number (with --merge)")
    p.add_argument("--include-private", action="store_true", help="also move private repos")
    p.add_argument("--pypi-ledger", metavar="TSV", help="repo, project, added_by, removed_by")
    p.add_argument("--old-ruleset", action="append", help=f"default {OLD_RULESET!r}; repeatable")
    p.add_argument("--new-ruleset", default=NEW_RULESET, help=f"default {NEW_RULESET!r}")
    p.add_argument("--token-env", default="GH_TOKEN", help="the variable holding the token")
    return p


def run_mode(args: argparse.Namespace, api: Api, opts: Options) -> list[str]:
    """Run the chosen mode; returns its problems."""
    if args.merge:
        return merge(api, args.merge, args.pr, opts)
    if args.move_rulesets:
        done = converted_repos(api, opts.include_private)
        puts = plan_move(org_rulesets(api), done, opts.old, opts.new, live_names(api))
        apply_move(api, puts, print)
        if not puts:
            print("rulesets: already in step")
        return []
    problems = check_fleet(api, opts)
    for problem in problems:
        print(f"problem: {problem}")
    if not problems:
        print("check: clean")
    return problems


def main(argv: list[str] | None = None, api: Api | None = None) -> int:
    args = parser().parse_args(argv)
    if unpinned(FLEET_FILE.read_text()):
        # Until sdlc's trust root records the sdlc-bootstrap digest (prerequisite 4),
        # a merge would land a ci.yml no runner can pull, in any mode's wake.
        print(f"cutover: {FLEET_FILE.name} has no sdlc-bootstrap digest yet", file=sys.stderr)
        return 2
    if args.merge and args.pr is None:
        print("cutover: --merge needs --pr", file=sys.stderr)
        return 2
    token = os.environ.get(args.token_env)
    if api is None:
        if not token:
            print(f"cutover: ${args.token_env} is empty", file=sys.stderr)
            return 2
        api = Api(token)
    opts = Options(
        include_private=args.include_private,
        ledger=args.pypi_ledger,
        old=args.old_ruleset or [OLD_RULESET],
        new=args.new_ruleset,
    )
    try:
        problems = run_mode(args, api, opts)
    except RefusedError as exc:
        print(f"cutover: refused: {exc}", file=sys.stderr)
        return 1
    except (ApiError, OSError) as exc:
        print(f"cutover: could not run: {api.redact(str(exc))}", file=sys.stderr)
        return 2
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
