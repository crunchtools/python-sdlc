#!/usr/bin/env python3
"""scripts/fleet_wave.py, against a fake API and bare repos standing in for GitHub.

The wave is the last fleet-wide one, so what it does to a repo is pinned:

  - a dry run converts and verifies in a throwaway clone and writes nothing
    anywhere else: no push, no pull request, no API call but reads
  - the default branch is the one the API names, master included
  - a refusal, a no-op, an earlier wave's open PR and a bad SDLC_IMAGE are
    each skipped with a reason, and the run goes on
  - --apply commits the canonical message, pushes chore/sdlc-floating with
    the bootstrap executable, opens the PR with the fixed body, queues a
    squash auto-merge -- and updates the PR already open rather than a second
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


def routes(earlier: tuple = (), ours: tuple = (), **repos: str) -> dict:
    q = "/search/issues?q=org%3Acrunchtools%20is%3Apr%20is%3Aopen%20head%3A"
    return {
        "/orgs/crunchtools/repos?type=public&per_page=100&page=1": listing(**repos),
        "/orgs/crunchtools/repos": (200, []),
        q + "chore/python-sdlc": search(*earlier),
        q + "chore/sdlc-floating": search(*ours),
    }


def wave_run(api: FakeApi, base: Path, names: list[str], apply: bool) -> tuple[dict, Path, str]:
    """W.run over names, its stdout captured: (counts, ledger, output)."""
    available = W.org_repos(api)
    wave = W.survey(api, apply)
    wave.clone_base = f"file://{base}"
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
        **{"python-sdlc": "main"},
    ),
)
names = ["pinned", "old", "odd", "bumping", "floating", "noexec", "python-sdlc", "gone"]
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
check("dry run: a name not in the org is skipped", got["gone"][1] == "skipped")
check("dry run: only reads", all(m == "GET" for m, _, _ in api.calls), str(api.calls))
check(
    "dry run: nothing is pushed",
    "chore/sdlc-floating" not in git(base / "crunchtools" / "pinned", "branch", "-a"),
)
check(
    "dry run: SDLC_IMAGE is not read without a token",
    not any("variables" in p for _, p, _ in api.calls),
)
check("dry run: counts add up", sum(counts.values()) == len(names), str(counts))

# The ledger: a re-run skips what is done and retries the rest.
counts, _, _ = wave_run(api, base, names, apply=False)
check(
    "re-run: done repos are skipped", counts.get("already done") == 4, str(counts)
)  # pinned, old, floating, noexec
check(
    "re-run: refused and skipped repos are tried again",
    counts.get("refused") == 1 and counts.get("skipped") == 3,
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

log = git(bare, "log", "-1", "--format=%an%n%B", "chore/sdlc-floating")
check(
    "apply: the branch is pushed with the canonical message",
    log.startswith("crunchtools-ci\n" + W.COMMIT_MESSAGE.splitlines()[0]),
    log,
)
check(
    "apply: the commit carries the Co-Authored-By trailer",
    "Co-Authored-By: Claude"
    in git(bare, "log", "-1", "--format=%(trailers)", "chore/sdlc-floating"),
)
check(
    "apply: the bootstrap is committed executable",
    git(bare, "ls-tree", "chore/sdlc-floating", ".sdlc/bin/sdlc").startswith("100755"),
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
    "apply: the PR body is the fixed one, with a rollback",
    opened and opened[0]["body"] == W.PR_BODY and "How to roll back" in W.PR_BODY,
)
merges = [b for m, p, b in api.calls if p == "/graphql"]
check(
    "apply: auto-merge is queued as a squash for each PR",
    len(merges) == 2
    and all("SQUASH" in b["query"] for b in merges)
    and merges[0]["variables"]["id"] == "PR_pinned",
)
check("apply: the token is never printed", CANARY not in out and CANARY not in ledger.read_text())

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
try:
    W.verify(checkout)
    stray = ""
except RuntimeError as exc:
    stray = str(exc)
check("verify: a change outside the converter's files is refused", "README.md" in stray, stray)
(checkout / "README.md").unlink()
git(checkout, "rm", "-q", "--cached", "README.md")
(checkout / ".sdlc/bin/sdlc").chmod(0o644)
try:
    W.verify(checkout)
    mode = ""
except RuntimeError as exc:
    mode = str(exc)
check("verify: a bootstrap that is not executable is refused", "100644" in mode, mode)

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

env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
proc = subprocess.run(
    [sys.executable, str(SCRIPT), "--apply"], capture_output=True, text=True, env=env, check=False
)
check(
    "main: --apply without GH_TOKEN cannot run (exit 2)",
    proc.returncode == 2 and "GH_TOKEN" in proc.stderr,
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
