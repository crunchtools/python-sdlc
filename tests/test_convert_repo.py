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


def convert(repo: Path, apply: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(repo), *(["--apply"] if apply else [])],
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
    "\n          .sdlc/bin/sdlc ci\n" in tests_yml
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


# A mixed workflow whose `sdlc ci` step is not the exact pinned form, or has
# two of them, is not rewritten by guesswork.
for label, text in (
    ("an edited pinned step", MIXED.replace("--depth 1", "--depth 2")),
    ("two sdlc ci steps", MIXED + PINNED_STEP),
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
    and "\n          .sdlc/bin/sdlc ci\n" in after[".github/workflows/ci.yml"],
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
        and "\n          .sdlc/bin/sdlc ci\n" in kept,
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


def main() -> int:
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
