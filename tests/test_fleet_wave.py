#!/usr/bin/env python3
"""scripts/fleet_wave.py, against a fake API and bare repos standing in for GitHub.

The wave is the last fleet-wide one, so what it does to a repo is pinned:

  - a dry run converts and verifies in a throwaway clone and writes nothing
    anywhere else: no push, no pull request, no API call but reads
  - the default branch is the one the API names, master included; a repo the
    anonymous listing does not show is cloned at its HEAD, and with a token a
    name not in the listing is skipped
  - a refusal, a no-op, an earlier wave's open PR and a bad SDLC_IMAGE are
    each skipped with a reason, and the run goes on
  - a private repo in the listing is refused and named unless --include-private
    (prerequisite 9), as is one the listing does not show; with the flag, both
    are processed
  - --apply commits the templated message (commit_message), pushes
    chore/fleet-cutover with the bootstrap executable, opens the PR with
    pr_fields -- and updates the PR already open rather than a second. It
    queues no auto-merge: the PR merges by cutover.py --merge
  - with a cutover.Target the wave also cuts each repo over: the plan names
    the constitution move and the one ci.yml, --out writes REPO.diff, a
    validator's violation is "invalid" and nothing is pushed, a refusal by the
    converter is hand work and the rest ships, --drop-gemini removes .gemini/
    and nothing else does, and write_report() lists clean repos apart from
    those that need a human
  - the ledger makes a re-run skip what is done and retry what is not
  - the token goes in a header and nowhere else

Run inside the image, which has the interpreter, git and PyYAML it needs:
  podman run --rm -v .:/src:ro,z -w /src --entrypoint /opt/constitution-venv/bin/python3 \
      quay.io/crunchtools/sdlc:<tag> tests/test_fleet_wave.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "fleet_wave.py"
_SPEC = importlib.util.spec_from_file_location("fleet_wave", SCRIPT)
W = importlib.util.module_from_spec(_SPEC)
sys.modules["fleet_wave"] = W  # dataclasses look their module up there
_SPEC.loader.exec_module(W)

FAILED: list[str] = []
# A stand-in GH_CANARY, asserted never to appear in anything the wave prints or writes.
CANARY = "ghs_fake0123456789abcdef"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


PINNED_HOOKS = """\
default_install_hook_types: [pre-commit, pre-push]
repos:
  - repo: https://github.com/crunchtools/python-sdlc
    rev: v0.8.0
    hooks:
      - id: sdlc-commit
      - id: sdlc-push
"""

PINNED_CI = """\
name: CI
on:
  pull_request:
jobs:
  sdlc:
    name: sdlc ci
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - name: sdlc ci
        run: |
          set -eu
          rev=$(awk '/repo: https:\\/\\/github.com\\/crunchtools\\/python-sdlc/ {f=1; next}
                     f && /rev:/ {print $2; exit}' .pre-commit-config.yaml)
          [ -n "$rev" ] || { echo "no python-sdlc rev in .pre-commit-config.yaml" >&2; exit 2; }
          git clone -q --depth 1 --branch "$rev" https://github.com/crunchtools/python-sdlc "$RUNNER_TEMP/sdlc"
          $RUNNER_TEMP/sdlc/bin/sdlc ci
"""

CONVERTIBLE = {".pre-commit-config.yaml": PINNED_HOOKS, ".github/workflows/ci.yml": PINNED_CI}
# Neither the python-sdlc pin nor both standalone hooks: convert_repo.py refuses.
UNKNOWN_SHAPE = {".pre-commit-config.yaml": "repos: []\n"}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout


def origin(base: Path, name: str, files: dict[str, str], branch: str = "main") -> Path:
    """A bare repo at base/crunchtools/name, its `branch` holding `files`."""
    work = base / "work" / name
    work.mkdir(parents=True)
    git(work, "init", "-q", "-b", branch)
    for path, text in files.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(text)
    git(work, "add", "-A")
    git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    bare = base / "crunchtools" / name
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return bare


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


class FakeApi(W.Api):
    """Answers from a table; records every call. Writes answer as GitHub does."""

    def __init__(self, token: str | None, routes: dict, pr_exists: set | None = None) -> None:
        super().__init__(token)
        self.routes = routes
        self.calls: list[tuple[str, str, object]] = []
        self.pr_exists = pr_exists or set()

    def call(self, method: str, path: str, body: object = None) -> tuple[int, object]:
        # The GraphQL repo listing is a read, and is recorded as one; only a POST is.
        reads = method == "GET" or (method, path) == ("POST", "/graphql")
        self.calls.append(("GET" if reads else method, path, body))
        if not reads:
            return self.write(method, path)
        listing = "/orgs/crunchtools/repos?type=all&per_page=100&page=1"
        return as_graphql(self.read(listing)) if method == "POST" else self.read(path)

    def read(self, path: str) -> tuple[int, object]:
        for prefix, answer in self.routes.items():
            if path.startswith(prefix):
                return answer
        return 404, {"message": "Not Found"}

    def write(self, method: str, path: str) -> tuple[int, object]:
        repo = path.split("/")[3]
        if method == "POST":
            if repo in self.pr_exists:
                return 422, {
                    "message": "Validation Failed",
                    "errors": [{"message": "A pull request already exists for x."}],
                }
            return 201, {
                "html_url": f"https://github.com/crunchtools/{repo}/pull/1",
                "node_id": f"PR_{repo}",
                "number": 1,
            }
        return 200, {
            "html_url": f"https://github.com/crunchtools/{repo}/pull/7",
            "node_id": f"PR_{repo}",
            "number": 7,
        }


def listing(private: tuple = (), **repos: str) -> tuple[int, list]:
    return 200, [
        {"name": name, "default_branch": branch, "archived": False, "private": name in private}
        for name, branch in repos.items()
    ]


def search(*hits: str) -> tuple[int, dict]:
    return 200, {
        "items": [
            {
                "repository_url": f"https://api.github.com/repos/crunchtools/{h}",
                "number": 3,
                "html_url": f"https://github.com/crunchtools/{h}/pull/3",
            }
            for h in hits
        ]
    }


def routes(earlier: tuple = (), ours: tuple = (), private: tuple = (), **repos: str) -> dict:
    q = "/search/issues?q=org%3Acrunchtools%20is%3Apr%20is%3Aopen%20head%3A"
    return {
        "/orgs/crunchtools/repos?type=all&per_page=100&page=1": listing(private, **repos),
        "/orgs/crunchtools/repos": (200, []),
        q + "chore/python-sdlc": search(*earlier),
        q + "chore/sdlc-floating": search(),
        q + "chore/renovate": search(),
        q + W.BRANCH: search(*ours),
    }


LAST: list[W.Wave] = []  # the wave wave_run last ran, for what it recorded


def wave_run(
    api: FakeApi, base: Path, names: list[str], apply: bool, **attrs: object
) -> tuple[dict, Path, str]:
    """W.run over names, its stdout captured: (counts, ledger, output).

    `attrs` are set on the Wave (target, sdlc, out ...) before it runs.
    """
    available = W.org_repos(api)
    wave = W.survey(api, apply)
    wave.clone_base = f"file://{base}"
    for key, value in attrs.items():
        setattr(wave, key, value)
    LAST[:] = [wave]
    ledger = base / "ledger.tsv"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        counts = W.run(wave, names, available, ledger, 0)
    return counts, ledger, out.getvalue()


def rows(ledger: Path) -> dict[str, list[str]]:
    lines = ledger.read_text().splitlines()
    return {line.split("\t")[0]: line.split("\t") for line in lines[1:]}


# -------------------------------------------------------------- SDLC_IMAGE

for value, ok in (
    (None, True),
    ("", True),
    ("main", True),
    ("latest", True),
    ("0.9.0", True),
    ("@sha256:" + "a" * 64, True),
    ("quay.io/crunchtools/sdlc:0.9.0", True),
    ("quay.io/crunchtools/sdlc@sha256:" + "0" * 64, True),
    ("localhost/python-sdlc:dev", False),
    ("0.9", False),
    ("v0.9.0", False),
    ("@sha256:" + "A" * 64, False),
    ("quay.io/crunchtools/sdlcx:main", False),
    ("quay.io/crunchtools/python-sdlc:0.8.0", False),
):
    check(
        f"SDLC_IMAGE={value!r} is {'taken' if ok else 'refused'}",
        (W.image_problem(value) is None) == ok,
    )

check(
    "a refusal is read from convert_repo.py's stderr",
    W.refusal("convert_repo: x: NOT CONVERTED: ci.yml has jobs\n") == "ci.yml has jobs",
)

# ----------------------------------------------------------------- dry run

base = Path(tempfile.mkdtemp())
origin(base, "pinned", CONVERTIBLE)
origin(base, "old", CONVERTIBLE, branch="master")
origin(base, "odd", UNKNOWN_SHAPE)
origin(base, "bumping", CONVERTIBLE)
floating = origin(base, "floating", CONVERTIBLE)
# A repo already converted: convert it the way --apply would, then push.
tmp = Path(tempfile.mkdtemp()) / "floating"
subprocess.run(["git", "clone", "-q", str(floating), str(tmp)], check=True)
subprocess.run(
    [sys.executable, str(W.CONVERTER), str(tmp), "--apply"], check=True, capture_output=True
)
git(tmp, "add", "-A")
git(tmp, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "floated")
git(tmp, "push", "-q", "origin", "HEAD:main")
# The same, but its bootstrap committed without +x: the diff is empty, the work is not.
origin(base, "noexec", CONVERTIBLE)
tmp = Path(tempfile.mkdtemp()) / "noexec"
subprocess.run(["git", "clone", "-q", str(base / "crunchtools" / "noexec"), str(tmp)], check=True)
subprocess.run(
    [sys.executable, str(W.CONVERTER), str(tmp), "--apply"], check=True, capture_output=True
)
(tmp / ".sdlc/bin/sdlc").chmod(0o644)
git(tmp, "add", "-A")
git(tmp, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "floated, no +x")
git(tmp, "push", "-q", "origin", "HEAD:main")

api = FakeApi(
    None,
    routes(
        earlier=("bumping",),
        pinned="main",
        old="master",
        odd="main",
        bumping="main",
        floating="main",
        noexec="main",
        **{"python-sdlc": "main", "sdlc": "main"},
    ),
)
# Private, so an anonymous listing does not show it; its HEAD is master.
origin(base, "hidden", CONVERTIBLE, branch="master")
names = [
    "pinned",
    "old",
    "odd",
    "bumping",
    "floating",
    "noexec",
    "python-sdlc",
    "sdlc",
    "hidden",
    "gone",
]
counts, ledger, out = wave_run(api, base, names, apply=False, include_private=True)
got = rows(ledger)
check("dry run: a pinned repo would open", got["pinned"][1] == "would-open", str(got["pinned"]))
check(
    "dry run: the plan names the files it changes",
    ".sdlc/bin/sdlc" in got["pinned"][3] and ".pre-commit-config.yaml" in got["pinned"][3],
)
check(
    "dry run: a master default branch is cloned as master",
    got["old"][1] == "would-open",
    str(got["old"]),
)
check(
    "dry run: a refusal is skipped with convert_repo.py's reason",
    got["odd"][1] == "refused" and "nothing this script knows" in got["odd"][3],
    str(got["odd"]),
)
check(
    "dry run: an earlier wave's open PR is skipped and named",
    got["bumping"][1] == "skipped" and "/bumping/pull/3" in got["bumping"][3],
)
check(
    "dry run: an already floating repo is a no-op",
    got["floating"][1] == "noop",
    str(got["floating"]),
)
check(
    "dry run: a floating repo whose bootstrap lacks +x is not a no-op",
    got["noexec"][1] == "would-open",
    str(got["noexec"]),
)
check("dry run: the converter's own repo is skipped", got["python-sdlc"][1] == "skipped")
check(
    "dry run: the converter's own repo under its new name is skipped",
    got["sdlc"][1] == "skipped" and "converter's own repo" in got["sdlc"][3],
    str(got["sdlc"]),
)
check(
    "dry run: a repo the anonymous listing lacks is cloned at its HEAD with --include-private",
    got["hidden"][1] == "would-open",
    str(got["hidden"]),
)
check(
    "dry run: a name that does not clone is a failure, with git's reason",
    got["gone"][1] == "failed" and "clone" in got["gone"][3],
    str(got["gone"]),
)
check("dry run: only reads", all(m == "GET" for m, _, _ in api.calls), str(api.calls))
check(
    "dry run: nothing is pushed",
    W.BRANCH not in git(base / "crunchtools" / "pinned", "branch", "-a"),
)
check(
    "dry run: SDLC_IMAGE is not read without a token",
    not any("variables" in p for _, p, _ in api.calls),
)
check("dry run: counts add up", sum(counts.values()) == len(names), str(counts))

# The ledger: a re-run skips what is done and retries the rest.
counts, _, _ = wave_run(api, base, names, apply=False, include_private=True)
check(
    "re-run: done repos are skipped", counts.get("already done") == 5, str(counts)
)  # pinned, old, floating, noexec, hidden
check(
    "re-run: refused, skipped and failed repos are tried again",
    counts.get("refused") == 1 and counts.get("skipped") == 3 and counts.get("failed") == 1,
    str(counts),
)

# ----------------------------------------------------------------- private

base = Path(tempfile.mkdtemp())
origin(base, "open", CONVERTIBLE)
origin(base, "closed", CONVERTIBLE)
origin(base, "hidden", CONVERTIBLE)
names = ["open", "closed", "hidden"]
api = FakeApi(None, routes(private=("closed",), open="main", closed="main"))
counts, ledger, _ = wave_run(api, base, names, apply=False)
got = rows(ledger)
check("private: a public repo in the listing is processed", got["open"][1] == "would-open")
check(
    "private: a private repo in the listing is refused, and named",
    got["closed"][1] == "refused"
    and "closed" in got["closed"][3]
    and "--include-private" in got["closed"][3],
    str(got["closed"]),
)
check(
    "private: a repo the listing does not show is refused too, its privacy unknown",
    got["hidden"][1] == "refused"
    and "hidden" in got["hidden"][3]
    and "unknown" in got["hidden"][3],
    str(got["hidden"]),
)
check("private: a refused repo is not done, so a re-run tries again", "refused" not in W.DONE)
base = Path(tempfile.mkdtemp())
origin(base, "closed", CONVERTIBLE)
origin(base, "hidden", CONVERTIBLE)
api = FakeApi(None, routes(private=("closed",), closed="main"))
counts, ledger, _ = wave_run(api, base, ["closed", "hidden"], apply=False, include_private=True)
got = rows(ledger)
check(
    "private: --include-private processes the private repo and the unlisted one",
    got["closed"][1] == "would-open" and got["hidden"][1] == "would-open",
    str(got),
)
base = Path(tempfile.mkdtemp())
api = FakeApi(CANARY, routes(private=("closed",), closed="main"))
counts, ledger, _ = wave_run(api, base, ["closed"], apply=True)
check(
    "private: --apply refuses it too, and writes nothing",
    rows(ledger)["closed"][1] == "refused" and all(m == "GET" for m, _, _ in api.calls),
    str(api.calls),
)

# ------------------------------------------------------------------- apply

base = Path(tempfile.mkdtemp())
bare = origin(base, "pinned", CONVERTIBLE)
origin(base, "reused", CONVERTIBLE)
origin(base, "badvar", CONVERTIBLE)
r = routes(ours=("reused",), pinned="main", reused="main", badvar="main")
r["/repos/crunchtools/badvar/actions/variables/SDLC_IMAGE"] = (200, {"value": "localhost/x"})
r["/repos/crunchtools/reused/pulls?head="] = (200, [{"number": 7}])
api = FakeApi(CANARY, r, pr_exists={"reused"})
counts, ledger, out = wave_run(api, base, ["pinned", "reused", "badvar"], apply=True)
got = rows(ledger)
check("apply: a pinned repo is opened", got["pinned"][1] == "opened", str(got["pinned"]))
check("apply: the ledger has the PR URL", got["pinned"][2].endswith("/pinned/pull/1"))
check(
    "apply: the open PR on the wave branch is updated, not doubled",
    got["reused"][1] == "updated" and got["reused"][2].endswith("/pull/7"),
    str(got["reused"]),
)
check(
    "apply: a repository SDLC_IMAGE the bootstrap refuses is skipped",
    got["badvar"][1] == "skipped" and "localhost/x" in got["badvar"][3],
    str(got["badvar"]),
)
check(
    "apply: the org variable is read once",
    sum(p == "/orgs/crunchtools/actions/variables/SDLC_IMAGE" for _, p, _ in api.calls) == 1,
)

wave = LAST[0]
log = git(bare, "log", "-1", "--format=%an%n%B", W.BRANCH)
check(
    "apply: the branch is pushed with the templated message",
    log.startswith("crunchtools-ci\n" + W.commit_message(wave).splitlines()[0]),
    log,
)
check(
    "apply: the commit carries the Co-Authored-By trailer",
    "Co-Authored-By: Claude" in git(bare, "log", "-1", "--format=%(trailers)", W.BRANCH),
)
check(
    "apply: the bootstrap is committed executable",
    git(bare, "ls-tree", W.BRANCH, ".sdlc/bin/sdlc").startswith("100755"),
)
check(
    "apply: the default branch is untouched",
    ".sdlc" not in git(bare, "ls-tree", "-r", "--name-only", "main"),
)

opened = [b for m, p, b in api.calls if m == "POST" and p == "/repos/crunchtools/pinned/pulls"]
check(
    "apply: the PR is opened from the wave branch to the default branch",
    opened and opened[0]["head"] == W.BRANCH and opened[0]["base"] == "main",
)
check(
    "apply: the PR is the templated one, with a rollback",
    bool(opened)
    and {k: opened[0][k] for k in ("title", "body")} == W.pr_fields(wave, "pinned")
    and "How to roll back" in opened[0]["body"],
)
merges = [b for m, p, b in api.calls if p == "/graphql" and "enablePullRequestAutoMerge" in str(b)]
check("apply: no auto-merge is queued", not merges and len(api.calls) > 0, str(merges))
check(
    "apply: the PR body says it merges by cutover.py --merge",
    "cutover.py --merge" in opened[0]["body"] and "--move-rulesets" in opened[0]["body"],
)
check("apply: the token is never printed", CANARY not in out and CANARY not in ledger.read_text())

# With a token, a name the listing does not have is skipped, not cloned.
base = Path(tempfile.mkdtemp())
api = FakeApi(CANARY, routes(pinned="main"))
counts, ledger, _ = wave_run(api, base, ["ghost"], apply=False)
check(
    "authenticated: a name not in the listing is skipped",
    rows(ledger)["ghost"][1] == "skipped" and "non-archived" in rows(ledger)["ghost"][3],
    str(rows(ledger)),
)

# ------------------------------------------------------------------- verify

base = Path(tempfile.mkdtemp())
bare = origin(base, "stray", CONVERTIBLE)
checkout = base / "stray-checkout"
subprocess.run(["git", "clone", "-q", str(bare), str(checkout)], check=True)
subprocess.run(
    [sys.executable, str(W.CONVERTER), str(checkout), "--apply"], check=True, capture_output=True
)
(checkout / "README.md").write_text("not the converter's\n")
plain = W.Wave(FakeApi(None, {}), False)
try:
    W.verify(plain, "stray", checkout)
    stray = ""
except RuntimeError as exc:
    stray = str(exc)
check("verify: a change outside the converter's files is refused", "README.md" in stray, stray)
(checkout / "README.md").unlink()
git(checkout, "rm", "-q", "--cached", "README.md")
(checkout / ".sdlc/bin/sdlc").chmod(0o644)
try:
    W.verify(plain, "stray", checkout)
    mode = ""
except RuntimeError as exc:
    mode = str(exc)
check("verify: a bootstrap that is not executable is refused", "100644" in mode, mode)

for owned in (
    ".sdlc/bin/sdlc",
    ".specify/memory/constitution.md",
    ".github/dependabot.yml",
    ".gemini/config.yaml",
    ".gemini/sub/styleguide.md",
    ".github/workflows/release.yml",
    "pyproject.toml",
):
    check(f"verify: the converter owns {owned}", bool(W.CONVERTER_OWNS.match(owned)))
for stranger in (
    "README.md",
    ".github/CODEOWNERS",
    ".github/workflows/sub/x.yml",
    "src/dependabot.yml",
    ".gemini",
    "src/.gemini/config.yaml",
):
    check(f"verify: the converter does not own {stranger}", not W.CONVERTER_OWNS.match(stranger))

# ----------------------------------------------------------------- cutover

CALL = "crunchtools/constitution/.github/workflows/container-release.yml"
CONTAINER = f"""\
name: Container
on:
  push:
    branches: [main]
  workflow_dispatch:
jobs:
  build:
    uses: {CALL}@v1.59.0
    with:
      image: quay.io/crunchtools/x
    secrets:
      QUAY_USERNAME: ${{{{ secrets.QUAY_USERNAME }}}}
"""
MANIFEST = "# C\n\n> **Inherits:** crunchtools/constitution v1.59.0\n"
PUSHER = """\
name: Push
on: [push]
jobs:
  p:
    runs-on: ubuntu-latest
    steps:
      - run: podman push quay.io/crunchtools/y:latest
"""
CUTTABLE = {
    **CONVERTIBLE,
    ".github/workflows/container.yml": CONTAINER,
    ".specify/memory/constitution.md": MANIFEST,
}
TARGET = W.cutover.Target(constitution="1.60.0")


def verdict(*violations: str) -> W.cutover.Target:
    v = types.SimpleNamespace(own_version=lambda: "1.60.0", validate=lambda _p: list(violations))
    return W.cutover.Target("1.60.0", validator=v)


base = Path(tempfile.mkdtemp())
out_dir = Path(tempfile.mkdtemp())
origin(base, "clean", CUTTABLE)
origin(base, "handy", {**CUTTABLE, ".github/workflows/push.yml": PUSHER})
origin(
    base,
    "odd",
    {
        **UNKNOWN_SHAPE,
        ".github/workflows/container.yml": CONTAINER,
        ".specify/memory/constitution.md": MANIFEST,
    },
)
# Both Gemini files: left alone unless a human said the app is gone.
GEMINI = {".gemini/config.yaml": "have_fun: false\n", ".gemini/styleguide.md": "# style\n"}
origin(base, "gemini", {**CUTTABLE, **GEMINI})
api = FakeApi(None, routes(clean="main", handy="main", odd="main", gemini="main"))
counts, ledger, _ = wave_run(
    api,
    base,
    ["clean", "handy", "odd", "gemini"],
    apply=False,
    target=TARGET,
    sdlc="0.9.0",
    out=out_dir,
)
got = rows(ledger)
wave = LAST[0]
check("cutover: a repo to cut over would open", got["clean"][1] == "would-open", str(got["clean"]))
check(
    "cutover: the plan names the constitution move, the one ci.yml and the deleted workflow",
    ".specify/memory/constitution.md" in got["clean"][3]
    and ".github/workflows/ci.yml" in got["clean"][3]
    and ".github/workflows/container.yml" in got["clean"][3],
    str(got["clean"]),
)
report = wave.reports["clean"]
check(
    "cutover: the report has the constitution move and the converter's changes",
    any(c.startswith("constitution:") for c in report.changes)
    and any(c.startswith("float:") for c in report.changes)
    and not report.hand
    and not report.invalid,
    str(report),
)
check("cutover: a dry run still only reads", all(m == "GET" for m, _, _ in api.calls))
diff = (out_dir / "clean.diff").read_text() if (out_dir / "clean.diff").exists() else ""
check(
    "cutover: --out writes REPO.diff, with the fleet file in it",
    "# One file, every repo" in diff and "v1.60.0" in diff,
    diff[:200],
)
check(
    "cutover: the diff is of the cutover, not of the converter alone",
    "Inherits:** crunchtools/constitution v1.60.0" in diff and ".sdlc/bin/sdlc" in diff,
)
# No fleet file, no pull request: cutover.py --merge would never merge it.
check(
    "cutover: a repo convert_repo.py refuses is refused, named, and opens nothing",
    got["odd"][1] == "refused" and "pre-commit-config" in got["odd"][3],
    str(got["odd"]),
)
check(
    "cutover: a workflow the fleet file cannot cover refuses the repo, naming it",
    got["handy"][1] == "refused"
    and "push.yml" in got["handy"][3]
    and not wave.reports["clean"].hand,
    str(got["handy"]),
)
# Hand work a converted repo can still carry: the cutover's own, here Dependabot's.
wave.reports["handy"].hand.append("Dependabot: a hand-edited dependabot.yml; remove it by hand")
fields = W.pr_fields(wave, "handy")
check(
    "cutover: the PR body names the hand work and the target",
    "Left for a human" in fields["body"]
    and "dependabot.yml" in fields["body"]
    and "v1.60.0" in fields["body"]
    and "0.9.0" in fields["body"]
    and "{" not in fields["title"],
    fields["body"][:300],
)
check(
    "cutover: .gemini/ is left alone without --drop-gemini",
    got["gemini"][1] == "would-open"
    and ".gemini" not in got["gemini"][3]
    and ".gemini/config.yaml"
    in git(base / "crunchtools" / "gemini", "ls-tree", "-r", "--name-only", "main"),
    str(got["gemini"]),
)
check("cutover: the title is the commit's first line", "constitution 1.60.0" in fields["title"])

base2 = Path(tempfile.mkdtemp())
bare = origin(base2, "gemini", {**CUTTABLE, **GEMINI})
api = FakeApi(CANARY, routes(gemini="main"))
dropper = W.cutover.Target("1.60.0", validator=verdict().validator, drop_gemini=True)
counts, ledger, _ = wave_run(api, base2, ["gemini"], apply=True, target=dropper, sdlc="0.9.0")
check(
    "gemini: --drop-gemini plans and ships the deletion of .gemini/",
    rows(ledger)["gemini"][1] == "opened"
    and any(c.startswith("gemini:") for c in LAST[0].reports["gemini"].changes)
    and ".gemini" not in git(bare, "ls-tree", "-r", "--name-only", W.BRANCH),
    str((rows(ledger), LAST[0].reports["gemini"])),
)
W.write_report(LAST[0], base2 / "report.md", "")
check("gemini: the report says it", "`.gemini/` removed" in (base2 / "report.md").read_text())

# The report: clean repos apart from the ones that need a human.
wave.outcomes["gone"] = ("skipped", "", "not a non-archived repo in crunchtools")
write_to = out_dir / "report.md"
W.write_report(wave, write_to, "node,bootc")
text = write_to.read_text()
check(
    "report: the clean repos are listed apart from hand work",
    "**Clean** (2): clean, gemini" in text and "**Hand work** (3):" in text,
    text[:400],
)
hand_line = next(x for x in text.splitlines() if x.startswith("- **Hand work**"))
check(
    "report: handy, odd and the skipped repo are the hand work",
    all(r in hand_line for r in ("handy", "odd", "gone"))
    and "clean" not in hand_line.split(":", 1)[1],
    hand_line,
)
check(
    "report: it names the mode, target and unbuilt stacks",
    "(dry run)" in text
    and "constitution v1.60.0" in text
    and "`node,bootc`" in text
    and "0.9.0" in text
    and "cutover.py --merge" in text
    and "private repos refused" in text,
    text[:300],
)
check(
    "report: each repo's section has its changes and its hand work",
    "## clean: would-open" in text and "- constitution:" in text and "- HAND Dependabot:" in text,
    text,
)
check("report: a skipped repo says why", "not a non-archived repo" in text)

# A target validator's violation: nothing is pushed.
base = Path(tempfile.mkdtemp())
bare = origin(base, "unfit", CUTTABLE)
api = FakeApi(CANARY, routes(unfit="main"))
counts, ledger, _ = wave_run(
    api, base, ["unfit"], apply=True, target=verdict("IX: signing is wrong"), sdlc="0.9.0"
)
got = rows(ledger)
check(
    "invalid: a validator's violation is action invalid, with the violation",
    got["unfit"][1] == "invalid" and "IX: signing is wrong" in got["unfit"][3],
    str(got),
)
check(
    "invalid: nothing is pushed, no PR opened, no merge queued",
    W.BRANCH not in git(bare, "branch", "-a") and all(m == "GET" for m, _, _ in api.calls),
    str(api.calls),
)
check("invalid: it is not done, so a re-run tries again", "invalid" not in W.DONE)
check(
    "invalid: the report carries it",
    LAST[0].reports["unfit"].invalid == ["IX: signing is wrong"],
)
W.write_report(LAST[0], base / "report.md", "")
check(
    "invalid: the report lists it as hand work",
    "- **Hand work** (1): unfit" in (base / "report.md").read_text()
    and "- INVALID IX: signing is wrong" in (base / "report.md").read_text(),
)

# A repo with no manifest cannot be judged, so it is invalid, not shipped unjudged.
base = Path(tempfile.mkdtemp())
bare = origin(
    base,
    "unjudged",
    {k: v for k, v in CUTTABLE.items() if k != ".specify/memory/constitution.md"},
)
api = FakeApi(CANARY, routes(unjudged="main"))
counts, ledger, _ = wave_run(api, base, ["unjudged"], apply=True, target=verdict(), sdlc="0.9.0")
got = rows(ledger)
check(
    "no manifest: the repo is invalid, named for its missing manifest",
    got["unjudged"][1] == "invalid" and "MANIFEST: no" in got["unjudged"][3],
    str(got),
)
check(
    "no manifest: nothing is pushed, no PR opened, no merge queued",
    W.BRANCH not in git(bare, "branch", "-a") and all(m == "GET" for m, _, _ in api.calls),
    str(api.calls),
)
check("exit: an invalid repo makes the run exit 1", W.exit_status(counts) == 1, str(counts))
check("exit: a run of clean plans exits 0", W.exit_status({"would-open": 3, "noop": 1}) == 0)

# A clean verdict ships, with the cutover in the commit.
base = Path(tempfile.mkdtemp())
bare = origin(base, "fit", CUTTABLE)
api = FakeApi(CANARY, routes(fit="main"))
counts, ledger, _ = wave_run(api, base, ["fit"], apply=True, target=verdict(), sdlc="0.9.0")
check("cutover: a repo the validator passes is opened", rows(ledger)["fit"][1] == "opened")
shipped = git(bare, "show", f"{W.BRANCH}:.github/workflows/ci.yml")
check(
    "cutover: the pushed branch has examples/ci.yml byte for byte, and no other workflow",
    shipped == (ROOT / "examples" / "ci.yml").read_text()
    and git(bare, "ls-tree", "--name-only", W.BRANCH, ".github/workflows/").split()
    == [".github/workflows/ci.yml"],
    shipped[:200],
)
check(
    "cutover: and the constitution move",
    "v1.60.0" in git(bare, "show", f"{W.BRANCH}:.specify/memory/constitution.md"),
)
check(
    "cutover: the pushed commit message names the constitution",
    "constitution 1.60.0" in git(bare, "log", "-1", "--format=%s", W.BRANCH),
)

# ---------------------------------------------------------------------- Api


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
        return b'{"message": "Bad credentials ' + CANARY.encode() + b'"}'

    def close(self) -> None:
        pass


conn = FakeConnection()
try:
    W.Api(CANARY, connect=lambda: conn).call("GET", "/orgs/crunchtools/repos")
    error = ""
except W.ApiError as exc:
    error = str(exc)
check(
    "Api: the token is sent as a bearer header",
    bool(conn.sent) and conn.sent[0][3].get("Authorization") == f"Bearer {CANARY}",
)
check("Api: the token is not in the path", bool(conn.sent) and CANARY not in conn.sent[0][1])
check(
    "Api: a 403 raises, and its message is redacted",
    "HTTP 403" in error and CANARY not in error,
    error,
)

# --------------------------------------------------------------------- main

REQUIRED = ["--constitution-dir", "/nonexistent", "--constitution", "1.60.0", "--sdlc", "0.9.0"]
env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
proc = subprocess.run(
    [sys.executable, str(SCRIPT), "--apply", *REQUIRED],
    capture_output=True,
    text=True,
    env=env,
    check=False,
)
check(
    "main: --apply without GH_TOKEN cannot run (exit 2)",
    proc.returncode == 2 and "GH_TOKEN" in proc.stderr,
    proc.stderr,
)
proc = subprocess.run(
    [sys.executable, str(SCRIPT), *REQUIRED],
    capture_output=True,
    text=True,
    env=env,
    check=False,
)
check(
    "main: a --constitution-dir that is not a checkout cannot run (exit 2)",
    proc.returncode == 2 and "--constitution-dir" in proc.stderr,
    proc.stderr,
)


# ------------------------------------------------- the sdlc-bootstrap placeholder

PINNED_BOOT = "quay.io/crunchtools/sdlc-bootstrap@sha256:" + "a" * 64
check(
    "boot: the placeholder digest is unpinned",
    W.cutover.unpinned('image: "x/sdlc-bootstrap@sha256:<digest>"'),
)
check("boot: a real digest is pinned", not W.cutover.unpinned(f'image: "{PINNED_BOOT}"'))
check(
    "boot: a tag is unpinned", W.cutover.unpinned('image: "quay.io/crunchtools/sdlc-bootstrap:1"')
)
if W.cutover.unpinned(W.cutover.FLEET_FILE.read_text()):
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "fleet_wave.py"),
            "--constitution-dir",
            "/nonexistent",
            "--constitution",
            "1.60.0",
            "--sdlc",
            "0.12.0",
            "--apply",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GH_TOKEN": "x"},
    )
    check(
        "boot: --apply refuses while the fleet file has no sdlc-bootstrap digest",
        proc.returncode == 2 and "no sdlc-bootstrap digest" in proc.stderr,
        proc.stderr,
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
