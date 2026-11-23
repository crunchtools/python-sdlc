#!/usr/bin/env python3
"""scripts/cutover.py: the offline half over small repos, the API half over a fake.

Offline, each change is pinned on its own fixture:

  - `Inherits:` and the constitution hook `rev:` move together; no workflow is
    touched by the move
  - a bare `python-sdlc` becomes `sdlc`, and `python-sdlc-extras` does not
  - `.gemini/` goes when the caller says so, and never through a symlink
  - any `uses:` left in a workflow is hand work; a comment is not one
  - Dependabot's removal is the target constitution's, and its failure is hand
    work; the target validator's verdict drops only the "newer than this
    validator" PIN line
  - a second run of every fixture finds nothing to do

Online, an in-memory GitHub (routes to (status, body), every write recorded):

  - the ruleset move: happy path, a re-run that PUTs nothing, and a refusal for a
    reverted repo, a non-explicit include list and a new ruleset without the
    required checks; private repos stay on the old list without --include-private
  - --merge, in order: merge, rulesets, update-branch; each
    refusal among its steps 1 to 6 writes nothing; a re-run on a merged PR resumes
  - --check: rulesets out of step and the missing release-branch ruleset
  - a private repo without --include-private is refused and named (prerequisite 9)

Run: python3 tests/test_cutover.py
"""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("cutover", ROOT / "scripts" / "cutover.py")
cut = importlib.util.module_from_spec(_SPEC)
sys.modules["cutover"] = cut  # dataclasses look their module up there
_SPEC.loader.exec_module(cut)

FAILED: list[str] = []
TARGET = cut.Target(constitution="1.60.0")
GEMINI_TARGET = cut.Target(constitution="1.60.0", drop_gemini=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def make(files: dict[str, str]) -> Path:
    base = Path(tempfile.mkdtemp())
    for path, text in files.items():
        (base / path).parent.mkdir(parents=True, exist_ok=True)
        (base / path).write_text(text)
    return base


def run(
    files: dict[str, str],
    name: str = "app",
    target: cut.Target = TARGET,
    label: str = "",
) -> tuple[Path, cut.Result]:
    """Cut a fixture over, and pin that a second run finds nothing to do."""
    repo = make(files)
    result = cut.cutover(repo, target)
    again = cut.cutover(repo, target)
    check(
        f"idempotent: {label or name}: a second run changes nothing",
        not again.changed,
        str(again.changed),
    )
    return repo, result


def read(repo: Path, path: str) -> str:
    return (repo / path).read_text()


# ------------------------------------------------------------ the constitution

MANIFEST = "# Constitution\n\n> **Inherits:** crunchtools/constitution v1.59.0\n\nbody v1.59.0\n"
HOOKS = (
    "repos:\n  - repo: https://github.com/crunchtools/constitution\n    rev: v1.59.0\n"
    "    hooks:\n      - id: validate\n"
    "  - repo: https://github.com/other/thing\n    rev: v1.59.0\n    hooks: []\n"
)
OLD_PINS = (
    "name: P\non: [push]\njobs:\n"
    "  a:\n    uses: crunchtools/constitution/.github/workflows/gate.yml@v1.59.0\n"
)
repo, result = run(
    {
        ".specify/memory/constitution.md": MANIFEST,
        ".pre-commit-config.yaml": HOOKS,
        ".github/workflows/ci.yml": OLD_PINS,
    },
    label="constitution",
)
check(
    "constitution: Inherits moves, and only Inherits",
    "Inherits:** crunchtools/constitution v1.60.0" in read(repo, ".specify/memory/constitution.md")
    and "body v1.59.0" in read(repo, ".specify/memory/constitution.md"),
)
hooks = read(repo, ".pre-commit-config.yaml")
check("constitution: the hook rev moves", "constitution\n    rev: v1.60.0" in hooks, hooks)
check("constitution: another hook's rev does not", "other/thing\n    rev: v1.59.0" in hooks)
check(
    "constitution: a workflow pin is not moved any more",
    read(repo, ".github/workflows/ci.yml") == OLD_PINS,
)
check(
    "constitution: the two files are reported, nothing else",
    sorted(c.split(" in ")[1] for c in result.changed)
    == [".pre-commit-config.yaml", ".specify/memory/constitution.md"],
    str(result.changed),
)

# ---------------------------------------------------------------- the old name

RENAME = """\
name: Dispatch
on: [workflow_dispatch]
jobs:
  loop:
    runs-on: ubuntu-latest
    steps:
      - run: |
          for repo in python-sdlc; do echo "$repo"; done
          for repo in python-sdlc-extras; do echo "$repo"; done
          echo "see https://github.com/crunchtools/python-sdlc.git and python-sdlc."
"""
repo, result = run({".github/workflows/dispatch.yml": RENAME}, label="rename")
text = read(repo, ".github/workflows/dispatch.yml")
check("rename: a for-loop word", "for repo in sdlc; do" in text, text)
check("rename: python-sdlc-extras is untouched", "for repo in python-sdlc-extras; do" in text)
check("rename: a sentence's last word is renamed", "and sdlc." in text, text)
check("rename: a URL's path is left alone", "crunchtools/python-sdlc.git" in text, text)
check(
    "rename: it is reported",
    any(c.startswith("renamed:") for c in result.changed),
    str(result.changed),
)
repo, _ = run(
    {
        ".pre-commit-config.yaml": "# python-sdlc hooks\nrepos: []\n",
        "pyproject.toml": '[tool.sdlc]\ndownstream = ["python-sdlc"]\n',
    },
    label="rename, hooks and config",
)
check(
    "rename: the hooks file and the config",
    "python-sdlc" not in read(repo, ".pre-commit-config.yaml") + read(repo, "pyproject.toml"),
)

# ------------------------------------------------------------------ .gemini/

GEMINI = {".gemini/config.yaml": "a: 1\n", ".gemini/styleguide.md": "# s\n", "README.md": "x\n"}
repo, result = run(GEMINI, target=GEMINI_TARGET, label="gemini")
check(
    "gemini: the directory goes, and only it",
    not (repo / ".gemini").exists() and (repo / "README.md").exists(),
)
check(
    "gemini: it is reported with the file count",
    result.changed == ["gemini: removed .gemini/ (2 file(s))"],
    str(result.changed),
)
repo, result = run(GEMINI, label="gemini, not asked")
check("gemini: left alone unless the target says so", (repo / ".gemini/config.yaml").exists())
repo, result = run({"README.md": "x\n"}, target=GEMINI_TARGET, label="gemini, absent")
check("gemini: no directory, nothing to do", not result.changed and not result.hand)
repo = make({"README.md": "x\n"})
outside = make({"secret": "s\n"})
(repo / ".gemini").mkdir()
(repo / ".gemini" / "config.yaml").symlink_to(outside / "secret")
result = cut.cutover(repo, GEMINI_TARGET)
check(
    "gemini: a symlink inside is hand work, and nothing is deleted",
    (repo / ".gemini/config.yaml").is_symlink()
    and (outside / "secret").exists()
    and not result.changed
    and any(h.startswith("gemini:") and "symlink" in h for h in result.hand),
    str((result.changed, result.hand)),
)
repo = make({"README.md": "x\n"})
(repo / ".gemini").symlink_to(outside)
result = cut.cutover(repo, GEMINI_TARGET)
check(
    "gemini: a symlinked directory is refused too",
    (outside / "secret").exists() and any("symlink" in h for h in result.hand),
    str(result.hand),
)

# ------------------------------------------------------------------ uses_left

repo, result = run(
    {
        ".github/workflows/ci.yml": "name: CI\non: [push]\njobs:\n  t:\n    runs-on: x\n"
        "    steps:\n      - run: sdlc event\n",
        ".github/workflows/old.yml": "# uses: actions/checkout@v4\nname: O\non: [push]\njobs:\n"
        "  a:\n    uses: someone/else/.github/workflows/x.yml@v1\n"
        "  b:\n    runs-on: x\n    steps:\n      - uses: actions/checkout@abc  # pinned\n",
    },
    label="uses",
)
check(
    "uses: every `uses:` counts, any owner, any ref, comments excluded",
    result.uses_left
    == [
        ".github/workflows/old.yml:6 uses someone/else/.github/workflows/x.yml@v1",
        ".github/workflows/old.yml:10 uses actions/checkout@abc",
    ],
    str(result.uses_left),
)
check("uses: the repo is hand work", len(result.hand) == 1 and "2 `uses:`" in result.hand[0])
repo, result = run(
    {".github/workflows/ci.yml": "# uses: x/y@v1\nname: CI\non: [push]\njobs: {}\n"},
    label="no uses",
)
check("uses: a comment alone leaves nothing", not result.uses_left and not result.hand)
check(
    "uses: no workflows, no pins",
    cut.cutover(make({"README.md": "x"}), TARGET).uses_left == [],
)

# --------------------------------------------------------------- Dependabot

seen = []


def drop(repo: Path, validator: object) -> list[str]:
    seen.append((repo, validator))
    return ["removed .github/dependabot.yml", "removed .github/workflows/dependabot-auto-merge.yml"]


def boom(_repo: Path, _validator: object) -> list[str]:
    raise RuntimeError("renovate config missing")


validator = SimpleNamespace(own_version=lambda: "1.60.0", validate=lambda _p: [])
repo = make({"README.md": "x\n"})
result = cut.cutover(
    repo,
    cut.Target("1.60.0", bump=SimpleNamespace(drop_dependabot=drop), validator=validator),
)
check(
    "dependabot: the constitution's own teardown is called with the checkout and validator",
    seen == [(repo, validator)],
    str(seen),
)
check(
    "dependabot: each change is reported",
    result.changed
    == [
        "Dependabot: removed .github/dependabot.yml",
        "Dependabot: removed .github/workflows/dependabot-auto-merge.yml",
    ],
    str(result.changed),
)
result = cut.cutover(
    repo,
    cut.Target("1.60.0", bump=SimpleNamespace(drop_dependabot=boom), validator=validator),
)
check(
    "dependabot: a RuntimeError is hand work, not a crash",
    result.hand == ["Dependabot: renovate config missing"] and result.changed == [],
    str((result.hand, result.changed)),
)
seen.clear()
cut.cutover(
    repo,
    cut.Target("1.60.0", bump=SimpleNamespace(drop_dependabot=drop), validator=None),
)
cut.cutover(repo, cut.Target("1.60.0", bump=None, validator=validator))
check("dependabot: skipped when either module is None", seen == [])

# ----------------------------------------------------------------- validate


def judging(verdict: list[str], own: str | None = "1.59.0") -> cut.Target:
    v = SimpleNamespace(own_version=lambda: own, validate=lambda _path: list(verdict))
    return cut.Target("1.60.0", validator=v)


VERDICT = [
    "PIN: constitution v1.60.0 is newer than this validator (1.59.0)",
    "PIN: workflow pins v1.58.0, not v1.60.0",
    "IX: container-release.yml call does not sign",
]
repo = make({".specify/memory/constitution.md": MANIFEST})
check(
    "validate: the newer-than-this-validator PIN line is filtered, the rest kept",
    cut.validate(repo, judging(VERDICT)) == VERDICT[1:],
    str(cut.validate(repo, judging(VERDICT))),
)
check(
    "validate: with no validator version nothing is filtered",
    cut.validate(repo, judging(VERDICT, own=None)) == VERDICT,
)
check(
    "validate: a non-PIN line that says it is kept",
    cut.validate(repo, judging(["IX: newer than this validator"]))
    == ["IX: newer than this validator"],
)
check("validate: a clean verdict is []", cut.validate(repo, judging([])) == [])
check("validate: no target validator is []", cut.validate(repo, TARGET) == [])
unjudged = cut.validate(make({"README.md": "x"}), judging([]))
check(
    "validate: a repo with no manifest is not a pass",
    len(unjudged) == 1 and unjudged[0].startswith("MANIFEST:"),
    str(unjudged),
)
asked = []
cut.validate(
    repo,
    cut.Target(
        "1.60.0",
        validator=SimpleNamespace(
            own_version=lambda: "1", validate=lambda p: asked.append(p) or []
        ),
    ),
)
check(
    "validate: it judges the manifest",
    asked == [repo / ".specify/memory/constitution.md"],
    str(asked),
)

# ======================================================================= the API

# Two fixtures from the real fleet file, whatever digest it pins: one carrying
# the `<digest>` marker it had before sdlc-bootstrap's first release, which every
# mode must refuse, and one pinned to a stand-in digest for the API tests.
PLACEHOLDER = "sdlc-bootstrap@sha256:<digest>"
FLEET_TEXT = re.sub(r"sdlc-bootstrap@sha256:[0-9a-f]{64}", PLACEHOLDER, cut.FLEET_FILE.read_text())
PINNED_DIR = Path(tempfile.mkdtemp())
UNPINNED = PINNED_DIR / "unpinned.yml"
UNPINNED.write_text(FLEET_TEXT)
(PINNED_DIR / "ci.yml").write_text(
    FLEET_TEXT.replace(PLACEHOLDER, "sdlc-bootstrap@sha256:" + "a" * 64)
)
cut.FLEET_FILE = PINNED_DIR / "ci.yml"
FLEET = cut.FLEET_FILE.read_bytes()
NEW, OLD, SHA, TOKEN = cut.NEW_RULESET, cut.OLD_RULESET, "a" * 40, "ghp_SECRETTOKEN"
O = "/repos/crunchtools"  # noqa: E741
GA = {"app": {"slug": "github-actions"}}


def b64(data: bytes) -> dict:
    return {"content": base64.b64encode(data).decode()}


def as_graphql(answer: tuple[int, object]) -> tuple[int, object]:
    """A REST repo listing fixture as the GraphQL page fleet() asks for."""
    status, body = answer
    if status != 200 or not isinstance(body, list):
        return status, body
    nodes = [
        {
            "name": r["name"],
            "isPrivate": bool(r.get("private")),
            "isArchived": bool(r.get("archived")),
            "defaultBranchRef": {"name": r.get("default_branch", "main")},
        }
        for r in body
    ]
    repos = {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}
    return 200, {"data": {"organization": {"repositories": repos}}}


def runs_as_graphql(answer: tuple[int, object]) -> tuple[int, object]:
    """A REST check-runs fixture as the GraphQL commit commit_runs() asks for."""
    status, body = answer
    if status != 200 or not isinstance(body, dict):
        return status, body
    suites: dict[str, list[dict]] = {}
    for r in body.get("check_runs", []):
        node = {
            "databaseId": r["id"],
            "name": r["name"],
            "conclusion": (r.get("conclusion") or "").upper() or None,
        }
        suites.setdefault((r.get("app") or {}).get("slug"), []).append(node)
    nodes = [{"app": {"slug": app}, "checkRuns": {"nodes": runs}} for app, runs in suites.items()]
    commit = {"checkSuites": {"nodes": nodes}}
    return 200, {"data": {"repository": {"object": commit}}}


class FakeApi(cut.Api):
    """An in-memory GitHub: (method, path) -> (status, body) or a callable giving one.

    A path is looked up whole, then without its query. Every write is recorded in
    `writes`, in order, and answers as GitHub does unless the table says otherwise.
    """

    def __init__(self, routes: dict) -> None:
        super().__init__(TOKEN)
        self.routes = routes
        self.writes: list[tuple[str, str, object]] = []
        self.calls = 0

    def call(self, method: str, path: str, body: object = None) -> tuple[int, object]:
        self.calls += 1
        if (method, path) == ("POST", "/graphql"):  # a read: the repo listing or a commit's runs
            return self.graphql(body)
        if method != "GET":
            self.writes.append((method, path, body))
        for key in (path, path.split("?", maxsplit=1)[0]):
            if (method, key) in self.routes:
                answer = self.routes[(method, key)]
                return answer(body) if callable(answer) else answer
        if method == "DELETE":
            return 204, None
        if method == "PUT":
            return (202 if path.endswith("/update-branch") else 200), {}
        return 404, {"message": "Not Found"}

    def graphql(self, body: object) -> tuple[int, object]:
        """The two queries the cutover sends, answered from their REST fixtures."""
        query = body.get("query", "") if isinstance(body, dict) else ""
        if "checkSuites" not in query:
            listing = self.routes[("GET", f"/orgs/{cut.ORG}/repos")]
            return as_graphql(listing() if callable(listing) else listing)
        variables = body["variables"]
        runs = self.routes.get(
            ("GET", f"{O}/{variables['name']}/commits/{variables['oid']}/check-runs"),
            (404, {"message": "Not Found"}),
        )
        return runs_as_graphql(runs() if callable(runs) else runs)


def ruleset(rid: int, name: str, include: list[str], checks: bool = True) -> dict:
    required = [{"context": c, "integration_id": cut.ACTIONS_APP} for c in cut.REQUIRED_CHECKS]
    return {
        "id": rid,
        "name": name,
        "enforcement": "active",
        "conditions": {
            "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []},
            "repository_name": {"include": include, "exclude": []},
        },
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": required if checks else required[:1]},
            }
        ],
    }


class World:
    """The org: done1 and done2 converted, app about to be, old1 old2 and private priv not."""

    def __init__(self, merged: bool = False) -> None:
        names = {"done1": 11, "done2": 12, "app": 42, "old1": 13, "old2": 14, "priv": 15}
        self.repos = {
            n: {
                "name": n,
                "full_name": f"crunchtools/{n}",
                "id": i,
                "private": n == "priv",
                "archived": False,
                "default_branch": "main",
            }
            for n, i in names.items()
        }
        self.converted = {"done1", "done2"} | ({"app"} if merged else set())
        old = ["old1", "old2", "priv"] + ([] if merged else ["app"])
        self.rs = {
            1: ruleset(1, NEW, sorted(self.converted)),
            2: ruleset(2, OLD, sorted(old)),
            3: {
                "id": 3,
                "name": "Ashigaru release branches",
                "enforcement": "active",
                "conditions": {"ref_name": {"include": [cut.RELEASE_REF], "exclude": []}},
                "rules": [],
            },
        }
        self.pr = {
            "number": 7,
            "state": "open",
            "merged": False,
            "user": {"type": "Bot", "login": "fleet"},
            "head": {
                "ref": cut.BRANCH,
                "sha": SHA,
                "repo": {"full_name": "crunchtools/app"},
            },
            "base": {"ref": "main"},
        }
        self.open = [
            self.pr,
            {
                "number": 9,
                "user": {"type": "Bot", "login": "renovate[bot]"},
                "head": {"ref": "renovate/x", "sha": "b" * 40},
            },
        ]
        self.runs = [
            {"id": 1, "name": c, "conclusion": "success", **GA} for c in cut.REQUIRED_CHECKS
        ]
        self.routes: dict = {
            ("GET", "/orgs/crunchtools/repos"): lambda _b=None: (200, list(self.repos.values())),
            ("GET", "/orgs/crunchtools/rulesets"): lambda _b=None: (
                200,
                [{"id": i, "name": r["name"]} for i, r in self.rs.items()],
            ),
            ("GET", f"{O}/app"): lambda _b=None: (200, self.repos["app"]),
            ("GET", f"{O}/priv"): lambda _b=None: (200, self.repos["priv"]),
            ("GET", f"{O}/app/pulls/7"): lambda _b=None: (200, self.pr),
            ("GET", f"{O}/app/pulls"): lambda _b=None: (200, self.open),
            ("GET", f"{O}/app/contents/.github/workflows/ci.yml?ref={SHA}"): (200, b64(FLEET)),
            ("GET", f"{O}/app/commits/{SHA}/check-runs"): lambda _b=None: (
                200,
                {"check_runs": self.runs},
            ),
            ("PUT", f"{O}/app/pulls/7/merge"): self.merge,
        }
        for rid in self.rs:
            self.register(rid)
        for name in self.repos:
            self.routes[("GET", f"{O}/{name}/contents/.github/workflows/ci.yml")] = (
                lambda _b=None, name=name: (
                    (200, b64(FLEET)) if name in self.converted else (404, {})
                )
            )

    def register(self, rid: int) -> None:
        self.routes[("GET", f"/orgs/crunchtools/rulesets/{rid}")] = lambda _b=None: (
            200,
            self.rs[rid],
        )
        self.routes[("PUT", f"/orgs/crunchtools/rulesets/{rid}")] = lambda body: self.put_ruleset(
            rid, body
        )

    def merge(self, _body: object) -> tuple[int, object]:
        # The contents API lags a squash merge: the default branch still reads
        # as unconverted here, so the move must count the merge itself.
        self.pr.update(state="closed", merged=True)
        return 200, {"merged": True}

    def put_ruleset(self, rid: int, body: dict) -> tuple[int, object]:
        self.rs[rid]["conditions"] = body["conditions"]
        return 200, self.rs[rid]

    def include(self, rid: int) -> list[str]:
        return self.rs[rid]["conditions"]["repository_name"]["include"]


HEAD_CI = ("GET", f"{O}/app/contents/.github/workflows/ci.yml?ref={SHA}")


def refusing(_body: object = None) -> tuple[int, object]:
    raise cut.ApiError("GET x: HTTP 403 Resource not accessible")


def cli(world: World, *argv: str) -> tuple[int, FakeApi, str, str]:
    api = FakeApi(world.routes)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cut.main(list(argv), api=api)
    return code, api, out.getvalue(), err.getvalue()


def merge_cli(world: World, *extra: str) -> tuple[int, FakeApi, str, str]:
    return cli(world, "--merge", "app", "--pr", "7", *extra)


def paths(api: FakeApi) -> list[str]:
    return [f"{m} {p}" for m, p, _b in api.writes]


# ---------------------------------------------------------- the ruleset move

w = World()
code, api, out, err = cli(w, "--move-rulesets")
check(
    "move: the new list is the converted repos, the old list the rest",
    code == 0
    and w.include(1) == ["done1", "done2"]
    and w.include(2) == ["app", "old1", "old2", "priv"],
    f"{code} {w.include(1)} {w.include(2)}",
)
check("move: nothing to change is no PUT", not api.writes and "already in step" in out, out)

w = World()
w.converted.add("app")
code, api, out, err = cli(w, "--move-rulesets")
check(
    "move: app converted: on the new list, off the old, new PUT first",
    code == 0
    and w.include(1) == ["app", "done1", "done2"]
    and w.include(2) == ["old1", "old2", "priv"]
    and paths(api) == ["PUT /orgs/crunchtools/rulesets/1", "PUT /orgs/crunchtools/rulesets/2"],
    f"{code} {paths(api)} {err}",
)
check(
    "move: conditions go whole, the other keys kept",
    api.writes[0][2]["conditions"]["ref_name"] == {"include": ["~DEFAULT_BRANCH"], "exclude": []}
    and "repository_name" in api.writes[1][2]["conditions"],
)
code, api, out, err = cli(w, "--move-rulesets")
check(
    "move: a second run changes nothing and PUTs nothing",
    code == 0 and not api.writes and w.include(1) == ["app", "done1", "done2"],
    str(api.writes),
)

w = World()
w.converted.add("app")
w.rs[1]["conditions"]["repository_name"]["include"] = ["app", "done1", "done2"]  # crash after PUT 1
code, api, out, err = cli(w, "--move-rulesets")
check(
    "move: after a crash between the two PUTs only the old list is written",
    code == 0 and paths(api) == ["PUT /orgs/crunchtools/rulesets/2"],
    f"{code} {paths(api)}",
)

w = World()
w.converted.add("app")
w.rs[1]["conditions"]["repository_name"]["include"] = ["done1", "done2", "old1"]
code, api, out, err = cli(w, "--move-rulesets")
check(
    "move: a repo on the new list that is not converted refuses, writing nothing",
    code == 1 and not api.writes and "old1" in err and "reverted" in err,
    f"{code} {err}",
)

w = World()
w.converted = {"app"}
w.rs[1]["conditions"]["repository_name"]["include"] = []  # the first canary
w.rs[2]["conditions"]["repository_name"]["include"] = ["app", "old1"]
code, api, out, err = cli(w, "--move-rulesets")
check(
    "move: an empty new list fills",
    code == 0 and w.include(1) == ["app"] and w.include(2) == ["old1"],
    f"{code} {w.include(1)} {w.include(2)} {err}",
)

w = World()
w.converted = {"app"}
w.rs[1]["conditions"]["repository_name"]["include"] = []
w.rs[2]["conditions"]["repository_name"]["include"] = ["app"]  # the last repo of the wave
code, api, out, err = cli(w, "--move-rulesets")
check(
    "move: an active ruleset is never PUT an empty list; nothing is written",
    code == 1 and not api.writes and "no repos" in err and "disabled" in err,
    f"{code} {paths(api)} {err}",
)
w.rs[2]["enforcement"] = "disabled"  # what the refusal asks a person to do
code, api, out, err = cli(w, "--move-rulesets")
check(
    "move: a disabled old ruleset that would empty is left as it is",
    code == 0 and paths(api) == ["PUT /orgs/crunchtools/rulesets/1"] and w.include(2) == ["app"],
    f"{code} {paths(api)} {err}",
)

# An archived (or deleted) repo leaves both lists; it is not a revert.
w = World()
w.repos["old2"]["archived"] = True
w.repos["done2"]["archived"] = True
code, api, out, err = cli(w, "--move-rulesets")
check(
    "move: archived repos leave both lists, and are not read as reverted",
    code == 0 and w.include(1) == ["done1"] and w.include(2) == ["app", "old1", "priv"],
    f"{code} {w.include(1)} {w.include(2)} {err}",
)

# sdlc's own ci.yml is the stated exception: its own jobs, the fleet's names and gate.
fleet_jobs = cut.yaml.safe_load(FLEET)["jobs"]
own = {
    "jobs": {
        "test": {"name": fleet_jobs["check"]["name"], "runs-on": "ubuntu-latest"},
        "gate": fleet_jobs["gate"],
    }
}
own_text = cut.yaml.safe_dump(own).encode()
check("converted: sdlc's own file counts for sdlc", cut.is_converted("sdlc", own_text))
check(
    "converted: sdlc's own file does not count for a fleet repo",
    not cut.is_converted("app", own_text),
)
check("converted: the fleet file counts for a fleet repo", cut.is_converted("app", FLEET))
own["jobs"]["gate"] = {**fleet_jobs["gate"], "permissions": {"contents": "write"}}
check(
    "converted: sdlc with a gate that is not the fleet's does not count",
    not cut.is_converted("sdlc", cut.yaml.safe_dump(own).encode()),
)
check("converted: no file is not converted", not cut.is_converted("sdlc", None))

for label, include in (("~ALL", ["~ALL"]), ("a wildcard", ["old*", "app"])):
    w = World()
    w.rs[2]["conditions"]["repository_name"]["include"] = include
    code, api, out, err = cli(w, "--move-rulesets")
    check(
        f"move: {label} in the old include is refused, naming the prerequisite",
        code == 1 and not api.writes and "explicit" in err and OLD in err,
        f"{code} {err}",
    )
w = World()
del w.rs[1]["conditions"]["repository_name"]
code, api, out, err = cli(w, "--move-rulesets")
check("move: a new ruleset with no include list is refused", code == 1 and not api.writes, err)

w = World()
w.rs[1] = ruleset(1, NEW, ["done1", "done2"], checks=False)
code, api, out, err = cli(w, "--move-rulesets")
check(
    "move: a new ruleset without both required checks is refused",
    code == 1 and not api.writes and "must require" in err,
    err,
)
w = World()
for rule in w.rs[1]["rules"]:
    for c in rule["parameters"]["required_status_checks"]:
        c["integration_id"] = 999
code, api, out, err = cli(w, "--move-rulesets")
check("move: checks pinned to another app are refused", code == 1 and not api.writes, err)
w = World()
del w.rs[2]
code, api, out, err = cli(w, "--move-rulesets")
check("move: a missing old ruleset is refused", code == 1 and OLD in err, err)

w = World()
w.rs[4] = ruleset(4, "Other gates", ["old2", "app"])
w.register(4)
w.converted.add("app")
code, api, out, err = cli(
    w, "--move-rulesets", "--old-ruleset", OLD, "--old-ruleset", "Other gates"
)
check(
    "move: --old-ruleset repeats, and each old list loses the converted",
    code == 0 and w.include(2) == ["old1", "old2", "priv"] and w.include(4) == ["old2"],
    f"{code} {w.include(2)} {w.include(4)} {err}",
)

w = World()
w.converted |= {"priv", "app"}
code, api, out, err = cli(w, "--move-rulesets")
check(
    "move: a private repo stays on the old list without --include-private",
    code == 0 and "priv" not in w.include(1) and "priv" in w.include(2),
    f"{w.include(1)} {w.include(2)}",
)
code, api, out, err = cli(w, "--move-rulesets", "--include-private")
check(
    "move: --include-private moves it",
    code == 0 and "priv" in w.include(1) and "priv" not in w.include(2),
    f"{w.include(1)} {w.include(2)}",
)

w = World()
w.rs[1]["conditions"]["repository_name"]["include"] = ["done1", "done2", "priv"]
w.converted.add("priv")
code, api, out, err = cli(w, "--move-rulesets")
check(
    "move: a private repo on the new list is not counted converted without the flag",
    code == 1 and "priv" in err and not api.writes,
    err,
)

w = World()
w.routes[("GET", "/orgs/crunchtools/rulesets")] = (403, {"message": "x"})
w.routes[("GET", "/orgs/crunchtools/repos")] = lambda _b=None: (_ for _ in ()).throw(
    cut.ApiError(f"GET /orgs: HTTP 403 {TOKEN}")
)
code, api, out, err = cli(w, "--move-rulesets")
check("move: an API error is exit 2", code == 2 and not api.writes, f"{code} {err}")

# ------------------------------------------------------------------ the merge

w = World()
code, api, out, err = merge_cli(w)
check("merge: happy path exits 0", code == 0, f"{code} {out} {err}")
check(
    "merge: in order: merge, new ruleset, old ruleset, update-branch; no org secret touched",
    paths(api)
    == [
        f"PUT {O}/app/pulls/7/merge",
        "PUT /orgs/crunchtools/rulesets/1",
        "PUT /orgs/crunchtools/rulesets/2",
        f"PUT {O}/app/pulls/9/update-branch",
    ],
    str(paths(api)),
)
check(
    "merge: squash at the head sha, the ruleset move from real state",
    api.writes[0][2] == {"merge_method": "squash", "sha": SHA}
    and w.include(1) == ["app", "done1", "done2"]
    and w.include(2) == ["old1", "old2", "priv"],
)
check(
    "merge: only the Bot PR is updated, at its own head sha",
    api.writes[-1][2] == {"expected_head_sha": "b" * 40},
)
check(
    "merge: one line per step",
    all(f"\n{n} " in "\n" + out for n in range(1, 10)),
    out,
)
check("merge: the token is never printed", TOKEN not in out + err)

# A re-run on the merged PR resumes at 7: no merge, the rest still happens.
code2, api2, out2, err2 = merge_cli(w)
check(
    "merge: a re-run on a merged PR does not merge again and moves nothing twice",
    code2 == 0
    and f"PUT {O}/app/pulls/7/merge" not in paths(api2)
    and "PUT /orgs/crunchtools/rulesets/1" not in paths(api2),
    f"{code2} {paths(api2)} {err2}",
)
w = World()
w.pr.update(state="closed", merged=True)  # the crash came after the merge, before the move
code, api, out, err = merge_cli(w)
check(
    "merge: a crash between merge and move is finished by the re-run",
    code == 0
    and paths(api)[0] == "PUT /orgs/crunchtools/rulesets/1"
    and w.include(2) == ["old1", "old2", "priv"]
    and f"PUT {O}/app/pulls/7/merge" not in paths(api)
    and "resuming" in out,
    f"{code} {paths(api)} {out} {err}",
)

w = World()
w.pr.update(state="closed", merged=True)
w.routes[HEAD_CI] = (200, b64(b"name: CI\n"))  # merged, but not the conversion
code, api, out, err = merge_cli(w)
check(
    "merge: a merged PR without the fleet ci.yml does not resume; nothing written",
    code == 1 and not api.writes and "does not carry the fleet ci.yml" in err,
    f"{code} {paths(api)} {err}",
)

# Every mode refuses while the fleet file has no sdlc-bootstrap digest.
cut.FLEET_FILE = UNPINNED
for argv in (["--check"], ["--move-rulesets"], ["--merge", "app", "--pr", "7"]):
    w = World()
    code, api, out, err = cli(w, *argv)
    check(
        f"unpinned: {argv[0]} exits 2 and touches nothing",
        code == 2 and not api.calls and "no sdlc-bootstrap digest" in err,
        f"{code} {err}",
    )
cut.FLEET_FILE = PINNED_DIR / "ci.yml"


def refused(label: str, mutate: object, *extra: str, needs: str = "") -> None:
    world = World()
    mutate(world)
    code, api, _out, err = merge_cli(world, *extra)
    check(
        f"merge refuses, writing nothing: {label}",
        code == 1 and not api.writes and needs in err,
        f"{code} {api.writes} {err}",
    )


refused("archived", lambda w: w.repos["app"].update(archived=True), needs="archived")
refused(
    "a PR from another branch",
    lambda w: w.pr["head"].update(ref="feature"),
    needs=cut.BRANCH,
)
refused("a PR from a fork", lambda w: w.pr["head"].update(repo={"full_name": "x/app"}))
refused("a PR with no head repo", lambda w: w.pr["head"].update(repo=None))
refused("a PR into another branch", lambda w: w.pr["base"].update(ref="dev"), needs="main")
refused("a closed, unmerged PR", lambda w: w.pr.update(state="closed"), needs="closed")
refused(
    "a head whose ci.yml is not the fleet file",
    lambda w: w.routes.update({HEAD_CI: (200, b64(b"x"))}),
    needs="fleet ci.yml",
)
refused(
    "a head with no ci.yml",
    lambda w: w.routes.update({HEAD_CI: (404, {})}),
)
refused(
    "a person's open PR, named",
    lambda w: w.open.append(
        {"number": 12, "user": {"type": "User", "login": "alice"}, "head": {"ref": "x", "sha": "c"}}
    ),
    needs="#12",
)
refused(
    "an old-flow release PR, though a Bot opened it",
    lambda w: w.open.append(
        {
            "number": 13,
            "user": {"type": "Bot", "login": "ashigaru[bot]"},
            "head": {"ref": "ashigaru/release-1.2.3", "sha": "c"},
        }
    ),
    needs="release",
)
PYPI = '[project]\nname = "app-dist"\n[tool.sdlc]\npublish = "pypi"\n'


def pypi(w: World) -> None:
    w.routes[("GET", f"{O}/app/contents/pyproject.toml?ref={SHA}")] = (200, b64(PYPI.encode()))


refused("a PyPI repo with no ledger", pypi, needs="pypi.org/manage/project/app-dist/settings/pub")
ledger = Path(tempfile.mkdtemp()) / "ledger.tsv"
HEADER = "repo\tproject\tadded_by\tremoved_by\n"
ledger.write_text(HEADER + "other\tx\tme\tme\napp\tapp-dist\tscott\t\n")
refused("a PyPI row with no removed_by", pypi, "--pypi-ledger", str(ledger), needs="app-dist")
ledger.write_text(HEADER + "other\tx\tme\tme\n")
refused("a PyPI repo with no ledger row", pypi, "--pypi-ledger", str(ledger), needs="app-dist")
ledger.write_text(HEADER + "app\tapp-dist\tscott\tscott\n")
w = World()
pypi(w)
code, api, out, err = merge_cli(w, "--pypi-ledger", str(ledger))
check("merge: a complete ledger row merges", code == 0 and bool(api.writes), f"{code} {err}")
w = World()
w.routes[("GET", f"{O}/app/contents/pyproject.toml?ref={SHA}")] = (
    200,
    b64(b'[project]\nname = "app"\n[tool.sdlc]\npublish = "none"\n'),
)
code, api, out, err = merge_cli(w)
check("merge: a project that does not publish to PyPI needs no ledger", code == 0, err)

refused(
    "a ruleset move that would refuse (a reverted repo)",
    lambda w: w.rs[1]["conditions"]["repository_name"]["include"].append("old1"),
    needs="old1",
)
refused(
    "a ruleset move over a wildcard include",
    lambda w: w.rs[2]["conditions"]["repository_name"].update(include=["*"]),
    needs="explicit",
)
refused("a check that has not run", lambda w: w.runs.pop(), needs="gate (pull_request_target)")
refused(
    "a required check that failed",
    lambda w: w.runs[0].update(conclusion="failure"),
    needs="check (pull_request)",
)
refused("a required check from another app", lambda w: w.runs[1].update(app={"slug": "other"}))
refused("a required check that is skipped", lambda w: w.runs[0].update(conclusion="skipped"))
w = World()
w.runs.append({"id": 9, "name": cut.REQUIRED_CHECKS[0], "conclusion": "failure", **GA})
code, api, out, err = merge_cli(w)
check("merge refuses: the newest run of a check decides", code == 1 and not api.writes, err)

w = World()
w.routes[("PUT", f"{O}/app/pulls/7/merge")] = (405, {"message": "not mergeable"})
code, api, out, err = merge_cli(w)
check(
    "merge: a refused merge stops before any ruleset is touched",
    code == 1 and paths(api) == [f"PUT {O}/app/pulls/7/merge"] and "not mergeable" in err,
    f"{paths(api)} {err}",
)

w = World()
w.routes[("PUT", f"{O}/app/pulls/9/update-branch")] = (422, {})
code, api, out, err = merge_cli(w)
check(
    "merge: a failed update-branch is named, exit 1, the rest done",
    code == 1
    and "PR #9" in out
    and w.include(1) == ["app", "done1", "done2"]
    and len(paths(api)) == 4,
    f"{code} {out}",
)

# ------------------------------------------------------ private repos (prereq 9)

w = World()
w.repos["app"]["private"] = True
code, api, out, err = merge_cli(w)
check(
    "private: a private repo without --include-private is refused and named",
    code == 1 and not api.writes and "crunchtools/app" in err and "--include-private" in err,
    f"{code} {err}",
)
code, api, out, err = merge_cli(w, "--include-private")
check(
    "private: --include-private lets it through",
    code == 0 and f"PUT {O}/app/pulls/7/merge" in paths(api),
    f"{code} {err}",
)
check(
    "private: the refusal helper names the repo and the flag, and is None otherwise",
    "--include-private" in (cut.refuse_private({"name": "p", "private": True}, False) or "")
    and cut.refuse_private({"name": "p", "private": True}, True) is None
    and cut.refuse_private({"name": "p", "private": False}, False) is None,
)

# -------------------------------------------------------------------- --check


def consistent() -> World:
    return World(merged=True)


w = consistent()
code, api, out, err = cli(w, "--check")
check("check: a clean fleet exits 0", code == 0 and "clean" in out, f"{out} {err}")

w = consistent()
w.rs[2]["conditions"]["repository_name"]["include"].append("done1")
code, api, out, err = cli(w, "--check")
check("check: rulesets out of step is a problem", code == 1 and "rulesets out of step" in out, out)
w = consistent()
del w.rs[3]
code, api, out, err = cli(w, "--check")
check("check: no release-branch ruleset is a problem", code == 1 and "prerequisite 12" in out, out)
w = consistent()
w.rs[3]["enforcement"] = "disabled"
code, api, out, err = cli(w, "--check")
check("check: a disabled release ruleset is no guard", code == 1 and "prerequisite 12" in out, out)
w = consistent()
w.rs[3]["conditions"]["ref_name"]["include"] = ["refs/heads/ashigaru/*"]
code, api, out, err = cli(w, "--check")
check("check: a wider pattern covers the release branches", code == 0, out)

# ------------------------------------------------------------- the CLI and Api

for argv in (
    ["--merge", "app"],
    ["--merge", "app", "--pr", "7", "--check"],
    ["--check", "--move-rulesets"],
    [],
):
    # argparse exits on a usage error: run the script, as a person would.
    code = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "cutover.py"), *argv],
        capture_output=True,
        check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    ).returncode
    check(f"cli: bad arguments {argv} exit 2", code == 2, str(code))
with contextlib.redirect_stderr(io.StringIO()):
    code = cut.main(["--check", "--token-env", "NO_SUCH_VARIABLE_XYZ"])
check("cli: an empty token variable exits 2", code == 2, str(code))


class FakeConnection:
    """http.client's half the Api uses: one request, one 403 echoing the credential."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, object, dict]] = []
        self.status = 403

    def request(self, method: str, path: str, body: object, headers: dict) -> None:
        self.sent.append((method, path, body, headers))

    def getresponse(self) -> FakeConnection:
        return self

    def read(self) -> bytes:
        return b'{"message": "Bad credentials ' + TOKEN.encode() + b'"}'

    def close(self) -> None:
        pass


conn = FakeConnection()
try:
    cut.Api(TOKEN, connect=lambda: conn).call("GET", "/orgs/crunchtools/repos")
    error = ""
except cut.ApiError as exc:
    error = str(exc)
check(
    "Api: the token is sent as a bearer header",
    bool(conn.sent) and conn.sent[0][3].get("Authorization") == f"Bearer {TOKEN}",
)
check("Api: a 403 raises, redacted", "HTTP 403" in error and TOKEN not in error, error)
check(
    "Api: authenticated follows the token",
    cut.Api(TOKEN).authenticated and not cut.Api(None).authenticated,
)


conn = FakeConnection()
with contextlib.suppress(cut.ApiError):
    cut.Api(TOKEN, connect=lambda: conn).call("GET", "/orgs/crunchtools/repos")
check("Api: by default a path goes as it is", conn.sent[0][1] == "/orgs/crunchtools/repos")
conn = FakeConnection()
with contextlib.suppress(cut.ApiError):
    cut.Api(TOKEN, connect=lambda: conn, base="http://gw:8019/proxy/http/github-api/").call(
        "GET", "/orgs/crunchtools/repos"
    )
check(
    "Api: a base URL's path prefixes every request",
    conn.sent[0][1] == "/proxy/http/github-api/orgs/crunchtools/repos",
    str(conn.sent),
)

opened: list[tuple[str, str]] = []


def opener(kind: str) -> Callable[..., FakeConnection]:
    def open_(host: str, **_kw: object) -> FakeConnection:
        opened.append((kind, host))
        return FakeConnection()

    return open_


real = (cut.http.client.HTTPConnection, cut.http.client.HTTPSConnection)
KINDS = ("HTTPConnection", "HTTPSConnection")
for name, kind in zip(KINDS, ("http", "https"), strict=True):
    setattr(cut.http.client, name, opener(kind))
try:
    for base in ("", "http://gw:8019/proxy/http/github-api"):
        with contextlib.suppress(cut.ApiError):
            cut.Api(TOKEN, base=base).call("GET", "/x")
    os.environ["CUTOVER_TEST_TOKEN"] = TOKEN
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        cut.main(["--check", "--token-env", "CUTOVER_TEST_TOKEN", "--api-url", "http://gw:1/p"])
finally:
    for name, cls in zip(KINDS, real, strict=True):
        setattr(cut.http.client, name, cls)
    os.environ.pop("CUTOVER_TEST_TOKEN", None)
check(
    "Api: https to api.github.com by default, http to a gateway named by --api-url",
    opened[:2] == [("https", cut.API_HOST), ("http", "gw:8019")] and ("http", "gw:1") in opened,
    str(opened),
)


def main() -> int:
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
