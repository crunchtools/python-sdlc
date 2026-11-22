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
  - --apply commits the templated message (commit_message), pushes
    chore/fleet-cutover with the bootstrap executable, opens the PR with
    pr_fields, queues a squash auto-merge -- and updates the PR already open
    rather than a second
  - with a cutover.Target the wave also cuts each repo over: the plan names
    the constitution move and signing, --out writes REPO.diff, a validator's
    violation is "invalid" and nothing is pushed, a refusal by the converter
    is hand work and the rest ships, and write_report() lists clean repos
    apart from those that need a human
  - the Ashigaru caller is read at the tag's commit, through an annotated tag
    too, and one that calls no router at the tag cannot run the wave; the
    report names its commit, the repos that got it, and the open Dependabot
    pin bumps it makes moot
  - the ledger makes a re-run skip what is done and retry what is not
  - the token goes in a header and nowhere else

Run inside the image, which has the interpreter, git and PyYAML it needs:
  podman run --rm -v .:/src:ro,z -w /src --entrypoint /opt/constitution-venv/bin/python3 \
      quay.io/crunchtools/sdlc:<tag> tests/test_fleet_wave.py
"""

from __future__ import annotations

import base64
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


class FakeApi(W.Api):
    """Answers from a table; records every call. Writes answer as GitHub does."""

    def __init__(self, token: str | None, routes: dict, pr_exists: set | None = None) -> None:
        super().__init__(token)
        self.routes = routes
        self.calls: list[tuple[str, str, object]] = []
        self.pr_exists = pr_exists or set()
        self.auto_merge_error: str | None = None

    def call(self, method: str, path: str, body: object = None) -> tuple[int, object]:
        self.calls.append((method, path, body))
        if path == "/graphql":
            return self.graphql()
        if method == "GET":
            return self.read(path)
        return self.write(method, path)

    def read(self, path: str) -> tuple[int, object]:
        for prefix, answer in self.routes.items():
            if path.startswith(prefix):
                return answer
        return 404, {"message": "Not Found"}

    def graphql(self) -> tuple[int, object]:
        errors = [{"message": self.auto_merge_error}] if self.auto_merge_error else []
        return 200, {"errors": errors} if errors else {"data": {}}

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


def listing(**repos: str) -> tuple[int, list]:
    return 200, [
        {"name": name, "default_branch": branch, "archived": False, "private": False}
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


BUMPS = "/search/issues?q=org%3Acrunchtools%20is%3Apr%20is%3Aopen%20author%3Aapp/dependabot"


def bumps(*titles: tuple[str, str]) -> tuple[int, dict]:
    return 200, {"items": [{"html_url": url, "title": title} for url, title in titles]}


def routes(earlier: tuple = (), ours: tuple = (), **repos: str) -> dict:
    q = "/search/issues?q=org%3Acrunchtools%20is%3Apr%20is%3Aopen%20head%3A"
    return {
        BUMPS: bumps(),
        "/orgs/crunchtools/repos?type=all&per_page=100&page=1": listing(**repos),
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
counts, ledger, out = wave_run(api, base, names, apply=False)
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
    "dry run: a repo the anonymous listing lacks is cloned at its HEAD, not skipped",
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
counts, _, _ = wave_run(api, base, names, apply=False)
check(
    "re-run: done repos are skipped", counts.get("already done") == 5, str(counts)
)  # pinned, old, floating, noexec, hidden
check(
    "re-run: refused, skipped and failed repos are tried again",
    counts.get("refused") == 1 and counts.get("skipped") == 3 and counts.get("failed") == 1,
    str(counts),
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
merges = [b for m, p, b in api.calls if p == "/graphql"]
check(
    "apply: auto-merge is queued as a squash for each PR",
    len(merges) == 2
    and all("SQUASH" in b["query"] for b in merges)
    and merges[0]["variables"]["id"] == "PR_pinned",
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

# A PR that opens but cannot auto-merge is recorded, retried, and fails the run.
base = Path(tempfile.mkdtemp())
origin(base, "nomerge", CONVERTIBLE)
api = FakeApi(CANARY, routes(nomerge="main"))
api.auto_merge_error = "Auto merge is not allowed for this repository"
counts, ledger, _ = wave_run(api, base, ["nomerge"], apply=True)
got = rows(ledger)
check(
    "apply: a PR auto-merge refused is recorded as no-automerge, with why",
    got["nomerge"][1] == "no-automerge" and "not allowed" in got["nomerge"][3],
    str(got),
)
check("apply: no-automerge is not done", "no-automerge" not in W.DONE)

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
    W.verify(plain, "stray", "main", checkout)
    stray = ""
except RuntimeError as exc:
    stray = str(exc)
check("verify: a change outside the converter's files is refused", "README.md" in stray, stray)
(checkout / "README.md").unlink()
git(checkout, "rm", "-q", "--cached", "README.md")
(checkout / ".sdlc/bin/sdlc").chmod(0o644)
try:
    W.verify(plain, "stray", "main", checkout)
    mode = ""
except RuntimeError as exc:
    mode = str(exc)
check("verify: a bootstrap that is not executable is refused", "100644" in mode, mode)

for owned in (
    ".sdlc/bin/sdlc",
    ".specify/memory/constitution.md",
    ".github/dependabot.yml",
    ".github/workflows/release.yml",
    "pyproject.toml",
):
    check(f"verify: the converter owns {owned}", bool(W.CONVERTER_OWNS.match(owned)))
for stranger in (
    "README.md",
    ".github/CODEOWNERS",
    ".github/workflows/sub/x.yml",
    "src/dependabot.yml",
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
TARGET = W.cutover.Target(constitution="1.60.0", constitution_ref="v1.60.0")


def verdict(*violations: str) -> W.cutover.Target:
    v = types.SimpleNamespace(own_version=lambda: "1.60.0", validate=lambda _p: list(violations))
    return W.cutover.Target("1.60.0", "v1.60.0", validator=v)


base = Path(tempfile.mkdtemp())
out_dir = Path(tempfile.mkdtemp())
origin(base, "clean", CUTTABLE)
origin(base, "handy", {**CUTTABLE, ".github/workflows/push.yml": PUSHER})
origin(base, "odd", {**UNKNOWN_SHAPE, ".github/workflows/container.yml": CONTAINER})
api = FakeApi(None, routes(clean="main", handy="main", odd="main"))
counts, ledger, _ = wave_run(
    api, base, ["clean", "handy", "odd"], apply=False, target=TARGET, sdlc="0.9.0", out=out_dir
)
got = rows(ledger)
wave = LAST[0]
check("cutover: a repo to cut over would open", got["clean"][1] == "would-open", str(got["clean"]))
check(
    "cutover: the plan names the constitution move and signing",
    ".specify/memory/constitution.md" in got["clean"][3]
    and ".github/workflows/container.yml" in got["clean"][3],
    str(got["clean"]),
)
report = wave.reports["clean"]
check(
    "cutover: the report has the constitution move, signing, and the float",
    any(c.startswith("constitution:") for c in report.changes)
    and any(c.startswith("signing:") for c in report.changes)
    and any(c.startswith("float:") for c in report.changes)
    and not report.hand
    and not report.invalid,
    str(report),
)
check("cutover: a dry run still only reads", all(m == "GET" for m, _, _ in api.calls))
diff = (out_dir / "clean.diff").read_text() if (out_dir / "clean.diff").exists() else ""
check("cutover: --out writes REPO.diff", "sign: true" in diff and "v1.60.0" in diff, diff[:200])
check(
    "cutover: the diff is of the cutover, not of the converter alone",
    "Inherits:** crunchtools/constitution v1.60.0" in diff and ".sdlc/bin/sdlc" in diff,
)
check(
    "cutover: a repo convert_repo.py refuses is hand work, and the rest still ships",
    got["odd"][1] == "would-open"
    and any(h.startswith("float: ") for h in wave.reports["odd"].hand),
    str((got["odd"], wave.reports["odd"])),
)
check(
    "cutover: a workflow that pushes an image is hand work, not a stop",
    got["handy"][1] == "would-open"
    and any("push.yml" in h for h in wave.reports["handy"].hand)
    and not wave.reports["clean"].hand,
    str(wave.reports["handy"]),
)
fields = W.pr_fields(wave, "handy")
check(
    "cutover: the PR body names the hand work and the target",
    "Left for a human" in fields["body"]
    and "push.yml" in fields["body"]
    and "v1.60.0" in fields["body"]
    and "0.9.0" in fields["body"]
    and "{" not in fields["title"],
    fields["body"][:300],
)
check("cutover: the title is the commit's first line", "constitution 1.60.0" in fields["title"])

# The report: clean repos apart from the ones that need a human.
wave.outcomes["gone"] = ("skipped", "", "not a non-archived repo in crunchtools")
write_to = out_dir / "report.md"
W.write_report(wave, write_to, "node,bootc")
text = write_to.read_text()
check(
    "report: the clean repos are listed apart from hand work",
    "**Clean** (1): clean" in text and "**Hand work** (3):" in text,
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
    "(dry run)" in text and "`v1.60.0`" in text and "`node,bootc`" in text and "0.9.0" in text,
    text[:300],
)
check(
    "report: each repo's section has its changes and its hand work",
    "## clean: would-open" in text
    and "- signing:" in text
    and "- HAND signing:" in text
    and "- HAND float:" in text,
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
shipped = git(bare, "show", f"{W.BRANCH}:.github/workflows/container.yml")
check("cutover: the pushed branch carries the signing", "sign: true" in shipped, shipped)
check(
    "cutover: and the constitution move",
    "v1.60.0" in git(bare, "show", f"{W.BRANCH}:.specify/memory/constitution.md"),
)
check(
    "cutover: the pushed commit message names the constitution",
    "constitution 1.60.0" in git(bare, "log", "-1", "--format=%s", W.BRANCH),
)

# ---------------------------------------------------------- Ashigaru caller

TAG = W.cutover.ASHIGARU_TAG
CALLER = (
    "name: Ashigaru\non:\n  issues:\njobs:\n  ashigaru:\n"
    f"    uses: crunchtools/ashigaru/.github/workflows/router.yml@{TAG}\n"
)
ASH = "/repos/crunchtools/ashigaru"


def caller_routes(text: str, annotated: bool = False) -> dict:
    commit = {"type": "commit", "sha": "c0ffee"}
    tag = {"type": "tag", "sha": "7a6"} if annotated else commit
    return {
        f"{ASH}/git/ref/tags/{TAG}": (200, {"object": tag}),
        f"{ASH}/git/tags/7a6": (200, {"object": commit}),
        f"{ASH}/contents/examples/ashigaru.yml?ref=c0ffee": (
            200,
            {"content": base64.b64encode(text.encode()).decode()},
        ),
    }


for annotated in (False, True):
    api = FakeApi(None, caller_routes(CALLER, annotated))
    got_caller = W.ashigaru_caller(api)
    check(
        f"caller: read at the tag's commit ({'annotated' if annotated else 'lightweight'})",
        got_caller == (CALLER, "c0ffee")
        and any(path.endswith("?ref=c0ffee") for _, path, _ in api.calls)
        and not any("ref=main" in path for _, path, _ in api.calls),
        str(api.calls),
    )
try:
    W.ashigaru_caller(FakeApi(None, caller_routes(CALLER.replace(f"@{TAG}", "@v4"))))
    error = ""
except W.ApiError as exc:
    error = str(exc)
check(
    "caller: one that calls no router at the tag cannot run", "calls no router.yml" in error, error
)
try:
    W.ashigaru_caller(FakeApi(None, {}))
    error = ""
except W.ApiError as exc:
    error = str(exc)
check("caller: a tag that cannot be read cannot run", "HTTP 404" in error, error)

PRE_ROUTER = "name: Ashigaru\non:\n  issues:\njobs:\n" + "".join(
    f"  {job}:\n    uses: crunchtools/ashigaru/.github/workflows/{job}.yml@v2\n"
    for job in ("triage", "code", "fix")
)
ROUTED = W.cutover.Target(
    "1.60.0", "v1.60.0", ashigaru_caller=CALLER, ashigaru_source="crunchtools/ashigaru@c0ffee"
)
base = Path(tempfile.mkdtemp())
out_dir = Path(tempfile.mkdtemp())
origin(base, "enrolled", {**CUTTABLE, ".github/workflows/ashigaru.yml": PRE_ROUTER})
origin(base, "plain", CUTTABLE)
bump_routes = routes(enrolled="main", plain="main")
bump_routes[BUMPS] = bumps(
    (
        "https://github.com/crunchtools/enrolled/pull/9",
        "chore(deps): bump crunchtools/ashigaru/.github/workflows/fix.yml from 2 to 3",
    ),
    (
        "https://github.com/crunchtools/enrolled/pull/8",
        "chore(deps): bump actions/checkout from 6 to 7",
    ),
)
api = FakeApi(None, bump_routes)
counts, ledger, _ = wave_run(
    api, base, ["enrolled", "plain"], apply=False, target=ROUTED, sdlc="0.9.0", out=out_dir
)
wave = LAST[0]
diff = (out_dir / "enrolled.diff").read_text()
check(
    "caller: the dry run re-copies the pre-router caller",
    f"+    uses: crunchtools/ashigaru/.github/workflows/router.yml@{TAG}" in diff
    and "-  triage:" in diff
    and not (out_dir / "plain.diff").read_text().count("ashigaru.yml"),
    diff[-600:],
)
check(
    "caller: the other diffs are as without it; release.yml is the open-only one",
    {c for c in wave.reports["plain"].changes if not c.startswith("release:")}
    == {c for c in wave.reports["enrolled"].changes if not c.startswith(("ashigaru:", "release:"))}
    and "ashigaru.yml" not in str(wave.reports["plain"].changes)
    and "the router tags" in str(wave.reports["enrolled"].changes)
    and "+# Release, open half" in diff,
    str((wave.reports["plain"].changes, wave.reports["enrolled"].changes)),
)
check(
    "caller: only the Ashigaru pin bumps are listed",
    wave.pin_bumps == ["https://github.com/crunchtools/enrolled/pull/9"],
    str(wave.pin_bumps),
)
W.write_report(wave, out_dir / "report.md", "")
text = (out_dir / "report.md").read_text()
check(
    "report: the caller's source commit, the repos that got it, and the bumps to close",
    "crunchtools/ashigaru@c0ffee" in text
    and "**Ashigaru router caller re-copied** (1): enrolled" in text
    and "## Dependabot Ashigaru pin bumps to close (1)" in text
    and "- https://github.com/crunchtools/enrolled/pull/9" in text,
    text,
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


def main() -> int:
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
