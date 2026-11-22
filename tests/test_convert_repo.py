#!/usr/bin/env python3
"""scripts/convert_repo.py, over repos built here in the shapes the fleet has.

The conversion is the last PR wave, so what it writes is pinned case by case:

  - the rev-pinned python-sdlc hooks and the standalone Gourmand and Gatehouse
    hooks both become examples/pre-commit-floating.yaml, and a hooks file with
    other repos in it keeps them
  - a workflow whose every job the CI stub covers is replaced by it; one that
    shares its `sdlc ci` job with other jobs keeps them and floats in place
  - what the stubs do not cover is kept, and a ci.yml it cannot replace is
    refused rather than guessed at
  - the result runs `sdlc ci` by the constitution validator's reading and by
    the image's own, so the required check is the same job
  - nothing is written without --apply, a second run finds nothing to do, and
    .specify/ is never touched

Run inside the image, which has the interpreter and PyYAML the script needs:
  podman run --rm -v .:/src:ro,z -w /src --entrypoint /opt/constitution-venv/bin/python3 \
      quay.io/crunchtools/sdlc:<tag> tests/test_convert_repo.py
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "convert_repo.py"
HOOKS = (ROOT / "examples" / "pre-commit-floating.yaml").read_text()
CI = (ROOT / "examples" / "ci.yml").read_text()
GATEHOUSE = (ROOT / "examples" / "gatehouse.yml").read_text()
BOOTSTRAP = (ROOT / "bin" / "sdlc-bootstrap").read_text()

# constitution validate-constitution.py `_runs_sdlc_ci`, and the image's own
# CodeQL-deferral reading (`_RUNS_SDLC_CI` in sdlc). Both must still see the job.
VALIDATOR = re.compile(r"^\s*[\"']?(?:\S*/)?sdlc[\"']?\s+ci\b", re.M)
IMAGE = re.compile(r"""(?:^|[\s;&|("'=])\S*bin/sdlc\s+ci\b""")

FAILED: list[str] = []
REFUSED = 2  # the script's exit code for a shape it will not guess at
EXECUTABLE = 0o111


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

GOURMAND = """\
name: Gourmand
on:
  pull_request:
jobs:
  gourmand:
    name: Code Quality (Gourmand)
    uses: crunchtools/gatehouse/.github/workflows/gourmand.yml@v0
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

PUBLISH = """\
name: Publish
on:
  release:
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - run: podman pull quay.io/crunchtools/python-sdlc:0.8.0
      - run: echo quay.io/crunchtools/python-sdlc-extras
"""

CONSTITUTION = "# per-repo constitution\nInherits: v1.44.0\n"


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


def runs_sdlc_ci(text: str) -> bool:
    for job in (yaml.safe_load(text).get("jobs") or {}).values():
        if job.get("name") != "sdlc ci":
            continue
        for step in job.get("steps") or []:
            run = str(step.get("run", ""))
            if VALIDATOR.search(run) and any(IMAGE.search(line) for line in run.splitlines()):
                return True
    return False


# ------------------------------------------------- a rev-pinned Python repo

repo = make_repo(
    {
        ".pre-commit-config.yaml": PINNED_HOOKS,
        ".github/workflows/ci.yml": OLD_CI,
        ".github/workflows/gourmand.yml": GOURMAND,
        ".github/workflows/gatehouse.yml": "# an older comment\n" + GATEHOUSE,
        ".github/workflows/publish.yml": PUBLISH,
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
check("python: ci.yml is the stub", after.get(".github/workflows/ci.yml") == CI)
check(
    "python: .sdlc/bin/sdlc is bin/sdlc-bootstrap, executable",
    after.get(".sdlc/bin/sdlc") == BOOTSTRAP
    and (repo / ".sdlc/bin/sdlc").stat().st_mode & EXECUTABLE == EXECUTABLE,
)
check(
    "python: gourmand.yml, covered by the stub, is gone",
    ".github/workflows/gourmand.yml" not in after,
)
check("python: gatehouse.yml is the stub", after[".github/workflows/gatehouse.yml"] == GATEHOUSE)
check(
    "python: publish.yml is kept, its image moved to quay.io/crunchtools/sdlc",
    "podman pull quay.io/crunchtools/sdlc:0.8.0" in after[".github/workflows/publish.yml"]
    and "quay.io/crunchtools/python-sdlc-extras" in after[".github/workflows/publish.yml"],
)
check(
    "python: .specify is untouched",
    after[".specify/memory/constitution.md"] == CONSTITUTION,
)
check(
    "python: no hook repo is pinned at a rev",
    all("rev" not in r for r in yaml.safe_load(after[".pre-commit-config.yaml"])["repos"]),
)
check(
    "python: the stub runs `sdlc ci` by both readings",
    runs_sdlc_ci(after[".github/workflows/ci.yml"]),
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

# ------------------------------------------------ a container-only repo

repo = make_repo(
    {
        ".pre-commit-config.yaml": STANDALONE_HOOKS,
        ".github/workflows/gourmand.yml": GOURMAND,
        ".github/workflows/container.yml": (
            "name: Container\non: push\njobs:\n  build:\n    uses: x/y/.github/workflows/c.yml@v1\n"
        ),
        "Containerfile": "FROM scratch\n",
    }
)
done = convert(repo, apply=True)
after = snapshot(repo)
check("container: --apply exits 0", done.returncode == 0, done.stderr)
check(
    "container: the standalone hooks become the floating ones",
    after[".pre-commit-config.yaml"] == HOOKS,
)
check("container: ci.yml is written", after.get(".github/workflows/ci.yml") == CI)
check(
    "container: gatehouse.yml is written", after.get(".github/workflows/gatehouse.yml") == GATEHOUSE
)
check("container: container.yml is kept", ".github/workflows/container.yml" in after)
check("container: gourmand.yml is gone", ".github/workflows/gourmand.yml" not in after)

# ------------------------------- an sdlc ci job sharing its workflow

repo = make_repo(
    {
        ".pre-commit-config.yaml": PINNED_HOOKS,
        ".github/workflows/tests.yml": MIXED,
        ".github/workflows/gourmand.yml": GOURMAND,
    }
)
done = convert(repo, apply=True)
after = snapshot(repo)
tests_yml = after[".github/workflows/tests.yml"]
check("mixed: --apply exits 0", done.returncode == 0, done.stderr)
check("mixed: no ci.yml stub beside it", ".github/workflows/ci.yml" not in after)
check("mixed: the other job is kept", "make test" in tests_yml)
check("mixed: gourmand.yml is kept, no stub took it", ".github/workflows/gourmand.yml" in after)
check(
    "mixed: the step floats, reading SDLC_IMAGE from the repository variable",
    "\n        run: .sdlc/bin/sdlc ci\n" in tests_yml
    and "set -eu" not in tests_yml
    and "SDLC_IMAGE: ${{ vars.SDLC_IMAGE }}" in tests_yml
    and "git clone" not in tests_yml,
)
check("mixed: it still runs `sdlc ci` by both readings", runs_sdlc_ci(tests_yml))
check("mixed: the result is still YAML", isinstance(yaml.safe_load(tests_yml), dict))
again = convert(repo)
check(
    "mixed: a second run finds nothing to do",
    again.returncode == 0 and again.stdout == "",
    again.stdout,
)

# ------------------------------------------------------ what it refuses

repo = make_repo(
    {
        ".pre-commit-config.yaml": STANDALONE_HOOKS,
        ".github/workflows/ci.yml": (
            "name: CI\non: push\njobs:\n  build:\n    runs-on: x\n    steps:\n      - run: make\n"
        ),
    }
)
before = snapshot(repo)
refused = convert(repo, apply=True)
check(
    "a ci.yml the stub cannot replace is refused (exit 2), and nothing is written",
    refused.returncode == REFUSED
    and "NOT CONVERTED" in refused.stderr
    and snapshot(repo) == before,
    refused.stderr,
)

for broken in ("repos: [\n", "- a list\n"):
    repo = make_repo({".pre-commit-config.yaml": broken})
    refused = convert(repo)
    check(
        f"a hooks file that is not a YAML mapping ({broken.strip()!r}) is refused, not a traceback",
        refused.returncode == REFUSED and "Traceback" not in refused.stderr,
        refused.stderr,
    )

repo = make_repo({".pre-commit-config.yaml": "repos: []\n"})
refused = convert(repo)
check(
    "a hooks file with nothing to replace is refused (exit 2)",
    refused.returncode == REFUSED,
    refused.stderr,
)

custom = GATEHOUSE.replace(
    "uses: crunchtools/gatehouse/.github/workflows/review.yml@v0",
    "uses: crunchtools/gatehouse/.github/workflows/review.yml@v0\n    with:\n      blocking: true",
)
repo = make_repo(
    {
        ".pre-commit-config.yaml": RUFF_AND_STANDALONE,
        ".github/workflows/gatehouse.yml": custom,
    }
)
done = convert(repo, apply=True)
after = snapshot(repo)
hooks = yaml.safe_load(after[".pre-commit-config.yaml"])
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
check(
    "a gatehouse.yml that differs in more than comments is kept",
    after[".github/workflows/gatehouse.yml"] == custom
    and "kept: .github/workflows/gatehouse.yml" in done.stderr,
)


# constitution v1.46.0 wants the step's run to be exactly the launcher: no
# `set -eu` left above it. gatehouse's ci.yml quoted the launcher's path, and an
# earlier convert_repo.py left `set -eu` in the block; both float to the exact line.
for label, text in (
    (
        "a quoted launcher path",
        MIXED.replace("$RUNNER_TEMP/sdlc/bin/sdlc ci", '"$RUNNER_TEMP/sdlc/bin/sdlc" ci'),
    ),
    (
        "the leftover `set -eu` block",
        MIXED.replace(
            PINNED_STEP.split("run: |\n", 1)[1], "          set -eu\n          .sdlc/bin/sdlc ci\n"
        ),
    ),
):
    repo = make_repo({".pre-commit-config.yaml": PINNED_HOOKS, ".github/workflows/tests.yml": text})
    done = convert(repo, apply=True)
    steps = yaml.safe_load(snapshot(repo)[".github/workflows/tests.yml"])["jobs"]["sdlc"]["steps"]
    check(
        f"mixed: {label} floats to a run that is exactly `.sdlc/bin/sdlc ci`",
        done.returncode == 0 and [s.get("run") for s in steps] == [".sdlc/bin/sdlc ci"],
        f"{steps} {done.stderr}",
    )
    check(f"mixed: {label}: a second run finds nothing to do", convert(repo).stdout == "")

# A mixed workflow whose `sdlc ci` step is not the exact pinned form, or has
# two of them, is not rewritten by guesswork.
for label, text in (
    ("an edited pinned step", MIXED.replace("--depth 1", "--depth 2")),
    ("two sdlc ci steps", MIXED + PINNED_STEP),
    ("a line after the pinned body", MIXED + "          echo done\n"),
    (
        "mismatched quotes",
        MIXED.replace("$RUNNER_TEMP/sdlc/bin/sdlc ci", '"$RUNNER_TEMP/sdlc/bin/sdlc ci'),
    ),
):
    repo = make_repo({".pre-commit-config.yaml": PINNED_HOOKS, ".github/workflows/tests.yml": text})
    before = snapshot(repo)
    refused = convert(repo, apply=True)
    check(
        f"mixed: {label} is refused (exit 2) and nothing is written",
        refused.returncode == REFUSED and snapshot(repo) == before,
        refused.stderr,
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

# ---------------------------------------------- what the stub would drop

# A job carrying more than examples/ci.yml has -- a step, an env, a service --
# keeps its workflow; only its `sdlc ci` step is rewritten.
richer = OLD_CI.replace(
    "      - uses: actions/checkout@v7\n",
    "      - uses: actions/checkout@v7\n      - run: make generated-files\n",
)
repo = make_repo({".pre-commit-config.yaml": PINNED_HOOKS, ".github/workflows/ci.yml": richer})
done = convert(repo, apply=True)
after = snapshot(repo)
check(
    "a richer sdlc ci job keeps its extra step, floated in place",
    done.returncode == 0
    and "make generated-files" in after[".github/workflows/ci.yml"]
    and "\n        run: .sdlc/bin/sdlc ci\n" in after[".github/workflows/ci.yml"],
    done.stderr,
)
for extra in ("    env:\n      FOO: bar\n", "    services:\n      db:\n        image: x\n"):
    job_level = OLD_CI.replace(
        "    runs-on: ubuntu-latest\n", f"    runs-on: ubuntu-latest\n{extra}", 1
    )
    repo = make_repo(
        {".pre-commit-config.yaml": PINNED_HOOKS, ".github/workflows/ci.yml": job_level}
    )
    done = convert(repo, apply=True)
    check(
        f"a job with {extra.split(':')[0].strip()} is not replaced by the stub",
        extra in snapshot(repo)[".github/workflows/ci.yml"],
        done.stderr,
    )

# Events and permissions are part of what a workflow does. One that runs on
# more than the stub does, or with other permissions, is not replaced.
for label, old_text, new_text in (
    (
        "a schedule",
        "on:\n  pull_request:\n",
        "on:\n  pull_request:\n  schedule:\n    - cron: '0 6 * * 1'\n",
    ),
    (
        "a release branch",
        "on:\n  pull_request:\n",
        "on:\n  pull_request:\n  push:\n    branches: [release]\n",
    ),
    ("a path filter", "on:\n  pull_request:\n", "on:\n  pull_request:\n    paths: ['src/**']\n"),
    (
        "workflow permissions",
        "on:\n  pull_request:\n",
        "on:\n  pull_request:\npermissions:\n  contents: write\n",
    ),
    (
        "job permissions",
        "    runs-on: ubuntu-latest\n",
        "    runs-on: ubuntu-latest\n    permissions:\n      packages: write\n",
    ),
):
    text = OLD_CI.replace(old_text, new_text, 1)
    repo = make_repo({".pre-commit-config.yaml": PINNED_HOOKS, ".github/workflows/ci.yml": text})
    done = convert(repo, apply=True)
    kept = snapshot(repo)[".github/workflows/ci.yml"]
    check(
        f"a ci.yml with {label} keeps it, its step floated in place",
        done.returncode == 0
        and new_text.strip() in kept
        and "\n        run: .sdlc/bin/sdlc ci\n" in kept,
        done.stderr,
    )

# The review's nits: what a replacement would silently change, or break.
renamed = OLD_CI.replace("    name: Code Quality (Gourmand)\n", "    name: Gourmand\n")
repo = make_repo({".pre-commit-config.yaml": PINNED_HOOKS, ".github/workflows/ci.yml": renamed})
done = convert(repo, apply=True)
check(
    "a Gourmand job under another name keeps its workflow (its required check name)",
    done.returncode == 0 and "    name: Gourmand\n" in snapshot(repo)[".github/workflows/ci.yml"],
    done.stderr,
)
with_env = OLD_CI.replace(
    "      - name: sdlc ci\n", "      - name: sdlc ci\n        env:\n          FOO: bar\n"
)
repo = make_repo({".pre-commit-config.yaml": PINNED_HOOKS, ".github/workflows/ci.yml": with_env})
before = snapshot(repo)
refused = convert(repo, apply=True)
check(
    "an sdlc ci step with its own env is not replaced; it is refused, nothing written",
    refused.returncode == REFUSED and snapshot(repo) == before,
    refused.stderr,
)
for event in ("pull_request_target", "workflow_run", "issue_comment", "pull_request_review"):
    privileged = MIXED.replace("on:\n  pull_request:\n", f"on:\n  {event}:\n")
    repo = make_repo(
        {".pre-commit-config.yaml": PINNED_HOOKS, ".github/workflows/tests.yml": privileged}
    )
    before = snapshot(repo)
    refused = convert(repo, apply=True)
    check(
        f"an sdlc ci step under {event} is refused, nothing written",
        refused.returncode == REFUSED and snapshot(repo) == before,
        refused.stderr,
    )
orphan = PUBLISH.replace(
    "      - run: echo quay.io/crunchtools/python-sdlc-extras\n",
    '      - run: git clone --branch "$rev" https://github.com/crunchtools/python-sdlc x\n',
)
repo = make_repo(
    {
        ".pre-commit-config.yaml": PINNED_HOOKS,
        ".github/workflows/ci.yml": OLD_CI,
        ".github/workflows/publish.yml": orphan,
    }
)
before = snapshot(repo)
refused = convert(repo, apply=True)
check(
    "a kept workflow still cloning python-sdlc by rev is refused, nothing written",
    refused.returncode == REFUSED and snapshot(repo) == before and "publish.yml" in refused.stderr,
    refused.stderr,
)

# ------------------------------------- the repo's new name: crunchtools/sdlc

NEW_PINNED_HOOKS = PINNED_HOOKS.replace("crunchtools/python-sdlc", "crunchtools/sdlc")
NEW_PINNED_STEP = (
    PINNED_STEP.replace("crunchtools\\/python-sdlc", "crunchtools\\/sdlc")
    .replace("no python-sdlc rev", "no sdlc rev")
    .replace("crunchtools/python-sdlc", "crunchtools/sdlc")
)
RENAMED_PUBLISH = """\
name: Publish
on:
  release:
jobs:
  build:
    uses: crunchtools/python-sdlc/.github/workflows/build.yml@v1
  extras:
    uses: crunchtools/python-sdlc-extras/.github/workflows/x.yml@v1
  publish:
    runs-on: ubuntu-latest
    steps:
      - run: podman pull quay.io/crunchtools/python-sdlc:0.11.0
      - run: echo quay.io/crunchtools/python-sdlc-extras
"""

# A repo that already floats still has the old name in a kept workflow.
repo = make_repo(
    {
        ".pre-commit-config.yaml": HOOKS,
        ".sdlc/bin/sdlc": BOOTSTRAP,
        ".github/workflows/ci.yml": CI,
        ".github/workflows/publish.yml": RENAMED_PUBLISH,
    }
)
(repo / ".sdlc/bin/sdlc").chmod(0o755)
done = convert(repo, apply=True)
published = snapshot(repo)[".github/workflows/publish.yml"]
check(
    "floating: the old image and reusable workflow become crunchtools/sdlc's",
    done.returncode == 0
    and "quay.io/crunchtools/sdlc:0.11.0" in published
    and "uses: crunchtools/sdlc/.github/workflows/build.yml@v1" in published
    and "crunchtools/python-sdlc/" not in published
    and "python-sdlc:" not in published,
    done.stderr + published,
)
check(
    "floating: a longer name (python-sdlc-extras) is left alone, as a workflow and as an image",
    "uses: crunchtools/python-sdlc-extras/.github/workflows/x.yml@v1" in published
    and "quay.io/crunchtools/python-sdlc-extras" in published,
    published,
)
check(
    "floating: a `renamed:` note names the file",
    any(
        line.startswith("convert_repo: ") and "renamed:" in line and "publish.yml" in line
        for line in done.stderr.splitlines()
    ),
    done.stderr,
)
check(
    "floating: only the kept workflow changed",
    snapshot(repo)[".pre-commit-config.yaml"] == HOOKS
    and snapshot(repo)[".github/workflows/ci.yml"] == CI,
)
again = convert(repo)
check(
    "floating: a second run finds nothing to do",
    again.returncode == 0 and again.stdout == "",
    again.stdout,
)

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

# A mixed workflow whose pinned step names the new repo floats like the old one.
repo = make_repo(
    {
        ".pre-commit-config.yaml": NEW_PINNED_HOOKS,
        ".github/workflows/tests.yml": MIXED.replace(PINNED_STEP, NEW_PINNED_STEP),
    }
)
done = convert(repo, apply=True)
tests_yml = snapshot(repo)[".github/workflows/tests.yml"]
check(
    "new name: a pinned `sdlc ci` step naming crunchtools/sdlc floats in place",
    NEW_PINNED_STEP != PINNED_STEP
    and done.returncode == 0
    and "\n        run: .sdlc/bin/sdlc ci\n" in tests_yml
    and "git clone" not in tests_yml
    and "make test" in tests_yml
    and runs_sdlc_ci(tests_yml),
    done.stderr + tests_yml,
)
check(
    "new name: that floated workflow's second run finds nothing to do", convert(repo).stdout == ""
)

# A kept workflow cloning sdlc by rev is refused under either name, over https
# or ssh, with or without `.git`.
for url in (
    "https://github.com/crunchtools/sdlc",
    "https://github.com/crunchtools/sdlc.git",
    "git@github.com:crunchtools/sdlc",
    "git@github.com:crunchtools/python-sdlc.git",
):
    orphan = PUBLISH.replace(
        "      - run: echo quay.io/crunchtools/python-sdlc-extras\n",
        f'      - run: git clone --branch "$rev" {url} x\n',
    )
    repo = make_repo(
        {
            ".pre-commit-config.yaml": PINNED_HOOKS,
            ".github/workflows/ci.yml": OLD_CI,
            ".github/workflows/publish.yml": orphan,
        }
    )
    before = snapshot(repo)
    refused = convert(repo, apply=True)
    check(
        f"a kept workflow cloning {url} by rev is refused, nothing written",
        refused.returncode == REFUSED
        and snapshot(repo) == before
        and "publish.yml" in refused.stderr,
        refused.stderr,
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
    repo / ".github/workflows/ci.yml": CI,
}
for label, path, text in (
    ("YAML that does not load", ".github/workflows/ci.yml", "jobs: [\n"),
    ("hooks without pre-push", ".pre-commit-config.yaml", HOOKS.replace(", pre-push]", "]")),
    ("hooks still pinned", ".pre-commit-config.yaml", PINNED_HOOKS),
    ("no sdlc ci job running the bootstrap", ".github/workflows/ci.yml", GOURMAND),
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

# ------------------------------- config: a converted repo never exits 2 at once


HOOKED = {".pre-commit-config.yaml": PINNED_HOOKS}


def sdlc_cfg(repo: Path, name: str = "sdlc.toml") -> dict:
    return tomllib.loads(snapshot(repo).get(name, "")).get("tool", {}).get("sdlc", {})


for label, files, want in (
    ("node", {"package.json": "{}\n"}, ["node"]),
    ("bootc", {"Containerfile": "FROM quay.io/fedora/fedora-bootc:42\n"}, ["bootc"]),
    ("docs", {"README.md": "# hi\n"}, ["docs"]),
):
    repo = make_repo({**HOOKED, **files})
    first = convert(repo, apply=True)
    again = convert(repo, apply=True)
    check(
        f"a {label} repo gets skip-stacks for the stack this image cannot check yet",
        first.returncode == 0 and sdlc_cfg(repo).get("skip-stacks") == want,
        first.stderr + first.stdout,
    )
    check(
        f"a {label} repo: the second run changes nothing",
        again.returncode == 0 and again.stdout == "",
        again.stderr + again.stdout,
    )

repo = make_repo(
    {
        **HOOKED,
        "package.json": "{}\n",
        "Containerfile": "FROM quay.io/fedora/fedora-bootc:42\n",
        "sdlc.toml": '[tool.sdlc]\nskip-stacks = ["node"]\n',
    }
)
refused = convert(repo, apply=True)
check(
    "a skip-stacks that leaves an unchecked stack out is refused, not rewritten",
    refused.returncode == REFUSED
    and "bootc" in refused.stderr
    and sdlc_cfg(repo).get("skip-stacks") == ["node"],
    refused.stderr,
)

# --unbuilt-stacks says which planned stacks the image still cannot check. A
# stack that is not listed is checked now, so the skip-stacks entry the
# converter wrote for it comes out, with its note, and nothing else does.
PROJECT = '[project]\nname = "x"\nversion = "0"\n'
EVERY_STACK_BUILT = ("--unbuilt-stacks", "")
for config, head in (
    ("sdlc.toml", "[tool.sdlc]\n"),
    ("pyproject.toml", PROJECT + '\n[tool.sdlc]\nstacks = ["python"]\n'),
):
    repo = make_repo({**HOOKED, "package.json": "{}\n", config: head})
    wrote = convert(repo, apply=True)
    check(
        f"{config}: the default still writes skip-stacks and its note",
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

# ----------------------------- gatehouse: an older guard is brought up to date

# The guard as the fleet copied it, before gatehouse#150 added .sdlc/.
OLD_GUARD = (
    GATEHOUSE.replace("^(\\.github/workflows|\\.sdlc)/", "^\\.github/workflows/")
    .replace(
        "a file out of .github/workflows/ or .sdlc/ unseen",
        "a workflow out of .github/workflows/ unseen",
    )
    .replace("modifies .github/workflows/ or .sdlc/ —", "modifies .github/workflows/ —")
)
repo = make_repo({**HOOKED, ".github/workflows/gatehouse.yml": OLD_GUARD})
done = convert(repo, apply=True)
check(
    "an older gatehouse.yml guard is replaced by the example's, which covers .sdlc/",
    OLD_GUARD != GATEHOUSE
    and done.returncode == 0
    and snapshot(repo)[".github/workflows/gatehouse.yml"] == GATEHOUSE,
    done.stderr,
)
CUSTOM = GATEHOUSE.replace("review.yml@v0", "review.yml@v1")
repo = make_repo({**HOOKED, ".github/workflows/gatehouse.yml": CUSTOM})
done = convert(repo, apply=True)
check(
    "a gatehouse.yml customized beyond the guard is kept",
    CUSTOM != GATEHOUSE
    and done.returncode == 0
    and snapshot(repo)[".github/workflows/gatehouse.yml"] == CUSTOM,
    done.stderr,
)
GUARD_CHANGED = OLD_GUARD.replace(
    'echo "No workflow files changed."', 'echo "No workflow files changed."\n          make audit'
)
repo = make_repo({**HOOKED, ".github/workflows/gatehouse.yml": GUARD_CHANGED})
done = convert(repo, apply=True)
check(
    "a guard script changed beyond gatehouse#150's lines is kept, not overwritten",
    GUARD_CHANGED != OLD_GUARD
    and done.returncode == 0
    and snapshot(repo)[".github/workflows/gatehouse.yml"] == GUARD_CHANGED,
    done.stderr,
)
GUARD_ENV = OLD_GUARD.replace(
    "          EVENT: ${{ github.event_name }}\n",
    "          EVENT: ${{ github.event_name }}\n          EXTRA: kept\n",
)
repo = make_repo({**HOOKED, ".github/workflows/gatehouse.yml": GUARD_ENV})
done = convert(repo, apply=True)
check(
    "an older guard whose step has its own env is kept, not overwritten",
    GUARD_ENV != OLD_GUARD
    and done.returncode == 0
    and snapshot(repo)[".github/workflows/gatehouse.yml"] == GUARD_ENV,
    done.stderr,
)
check("the gatehouse example's guard covers .sdlc/", "|\\.sdlc)/" in GATEHOUSE)


# ------------------------------------------------- the fleet's own runner

# The 54 fleet ci.yml files route the `sdlc ci` job between the cached and the
# uncached self-hosted runners; replacing the job must not move it to GitHub's.
FLEET_RUNNER = """\
    # Same-repo PRs run on our cached runner, pushes and schedules on the uncached
    # one; forks and Dependabot stay on GitHub's. An empty variable falls back to
    # ubuntu-latest (crunchtools/trentina security.yml).
    runs-on: >-
      ${{ github.event_name == 'pull_request'
          && github.event.pull_request.head.repo.full_name == github.repository
          && github.event.pull_request.user.login != 'dependabot[bot]'
          && (vars.CACHED_RUNNER_LABEL || 'ubuntu-latest')
          || github.event_name != 'pull_request'
          && (vars.UNCACHED_RUNNER_LABEL || 'ubuntu-latest')
          || 'ubuntu-latest' }}
"""
FLEET_CI = CI.replace("    runs-on: ubuntu-latest\n", FLEET_RUNNER, 1)
# Four spaces a level: the runner block moves left to the stub's job keys.
WIDE_CI = (
    """\
name: CI
on:
    pull_request:
jobs:
    sdlc:
        name: sdlc ci
"""
    + "".join("    " + x if x.strip() else x for x in FLEET_RUNNER.splitlines(keepends=True))
    + """\
        steps:
            - uses: actions/checkout@v7
            - name: sdlc ci
              env:
                  SDLC_IMAGE: ${{ vars.SDLC_IMAGE }}
              run: .sdlc/bin/sdlc ci
"""
)
check("runner: the fixture differs from the stub", FLEET_CI != CI)
for label, old_ci in (
    ("the stub's shape", FLEET_CI),
    ("a pinned job", OLD_CI.replace("    runs-on: ubuntu-latest\n", FLEET_RUNNER, 1)),
    ("four-space indents", WIDE_CI),
):
    repo = make_repo(
        {
            ".pre-commit-config.yaml": PINNED_HOOKS,
            ".github/workflows/ci.yml": old_ci,
            ".specify/memory/constitution.md": CONSTITUTION,
        }
    )
    done = convert(repo, apply=True)
    ci = snapshot(repo).get(".github/workflows/ci.yml", "")
    job = ((yaml.safe_load(ci) or {}).get("jobs") or {}).get("sdlc") or {}
    want = (yaml.safe_load(old_ci) or {}).get("jobs", {})
    want = next((j.get("runs-on") for j in want.values() if j.get("name") == "sdlc ci"), None)
    check(f"runner: {label}: --apply exits 0", done.returncode == 0, done.stderr)
    check(f"runner: {label}: the job keeps its runner", job.get("runs-on") == want, ci)
    check(f"runner: {label}: and its comment", "Same-repo PRs run on our cached" in ci)
    check(f"runner: {label}: it still runs sdlc ci", runs_sdlc_ci(ci))
    check(f"runner: {label}: a second run finds nothing to do", convert(repo).stdout == "")


def main() -> int:
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
