#!/usr/bin/env python3
"""Convert one repo to the floating sdlc: no `rev:` to pin, ever again.

Rewrites a checkout so its gates come from quay.io/crunchtools/sdlc:latest
with nothing in the repo naming a release -- which makes the PR wave that
applies this the last one: what the gates run changes when the image does.

  .sdlc/bin/sdlc           python-sdlc's bin/sdlc-bootstrap: asks the image for
                           its launcher and runs it. The one file a converted
                           repo carries; it names no release, so nothing bumps it
  .pre-commit-config.yaml  the rev-pinned python-sdlc hooks, and the standalone
                           Gourmand and Gatehouse hooks, become the two hooks of
                           examples/pre-commit-floating.yaml, which run it
  .github/workflows/       a workflow whose every job the stubs cover -- the
                           `sdlc ci` job, the Gourmand job -- is replaced by
                           examples/ci.yml; gatehouse.yml becomes
                           examples/gatehouse.yml when it differs from it only in
                           comments. Everything else (publish, release, a
                           container build) is kept. An `sdlc ci` job that
                           shares its workflow with jobs the stub does not cover
                           keeps its place and has its step rewritten instead.
  any image reference      quay.io/crunchtools/python-sdlc becomes
                           quay.io/crunchtools/sdlc, in workflows and config

Deterministic and offline: the same checkout gives the same diff. It prints
that diff and changes nothing without --apply. It never touches .specify/,
the per-repo constitution.

Exit 0 when it converted (or there was nothing to do), 2 when the repo has a
shape it will not guess at -- named, so it can be converted by hand.

Usage: scripts/convert_repo.py REPO_DIR [--apply]
Needs PyYAML, to read the workflows; it never writes YAML, only text.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import tomllib
from pathlib import Path

try:
    import yaml
except ImportError:  # the image's constitution venv has it; main() says so
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
HOOKS_TEMPLATE = EXAMPLES / "pre-commit-floating.yaml"
CI_TEMPLATE = EXAMPLES / "ci.yml"
GATEHOUSE_TEMPLATE = EXAMPLES / "gatehouse.yml"
BOOTSTRAP = ROOT / "bin" / "sdlc-bootstrap"
# Where a converted repo keeps the bootstrap. A path, because the constitution's
# host contract lets a `system` hook start a script in the repo named by one;
# and `bin/sdlc`, because the image reads a line running `...bin/sdlc ci` as the
# job that runs `sdlc ci`.
BOOTSTRAP_AT = Path(".sdlc/bin/sdlc")
FLOATING_CI_LINE = f"{BOOTSTRAP_AT} ci"

OLD_IMAGE = "quay.io/crunchtools/python-sdlc"
NEW_IMAGE = "quay.io/crunchtools/sdlc"
SDLC_REPO = re.compile(r"^https://github\.com/crunchtools/python-sdlc(?:\.git)?/?$")
STANDALONE_HOOKS = {"gourmand", "gatehouse"}
SDLC_HOOKS = {"sdlc-commit", "sdlc-push"}
GOURMAND_CALL = re.compile(r"^crunchtools/gatehouse/\.github/workflows/gourmand\.yml@")
SDLC_CI_LINE = re.compile(r"^\s*[\"']?(?:\S*/)?sdlc[\"']?\s+ci\b", re.M)

# The step every converted repo's `sdlc ci` job runs today, rev read from the
# hooks file and the launcher cloned at it. Matched exactly, indent aside: a
# step that says anything else is not one this script will guess at.
PINNED_STEP = re.compile(
    r"^(?P<i>[ \t]*)rev=\$\(awk "
    r"'/repo: https:\\/\\/github\.com\\/crunchtools\\/python-sdlc/ \{f=1; next\}\n"
    r"[ \t]*f && /rev:/ \{print \$2; exit\}' \.pre-commit-config\.yaml\)\n"
    r"[ \t]*\[ -n \"\$rev\" \] \|\| "
    r"\{ echo \"no python-sdlc rev in \.pre-commit-config\.yaml\" >&2; exit 2; \}\n"
    r"[ \t]*git clone -q --depth 1 --branch \"\$rev\" "
    r"https://github\.com/crunchtools/python-sdlc \"\$RUNNER_TEMP/sdlc\"\n"
    r"[ \t]*\$RUNNER_TEMP/sdlc/bin/sdlc ci\n",
    re.M,
)
STEP_NAME = re.compile(r"^(?P<i>[ \t]*)- name: sdlc ci\n(?P<j>[ \t]*)run: \|\n", re.M)


class RefusedError(Exception):
    """A shape this script will not guess at."""


# ------------------------------------------------------------------ templates


def parse(name: str, text: str) -> dict:
    """A YAML mapping, or a refusal naming the file."""
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RefusedError(f"{name} is not YAML: {exc}") from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise RefusedError(f"{name} is not a YAML mapping")
    return parsed


def floating_hooks_item() -> str:
    """The `- repo: local` item of examples/pre-commit-floating.yaml."""
    text = HOOKS_TEMPLATE.read_text()
    return text[text.index("  - repo: local\n") :]


# -------------------------------------------------------- .pre-commit-config


def split_items(body: list[str]) -> list[list[str]]:
    """The `repos:` list as items, each the lines from one `- repo:` to the next."""
    items: list[list[str]] = []
    for line in body:
        if re.match(r"^\s*-\s+repo:", line):
            items.append([line])
        elif items:
            items[-1].append(line)
        elif line.strip() and not line.lstrip().startswith("#"):
            raise RefusedError(f".pre-commit-config.yaml: `repos:` starts with {line.strip()!r}")
    return items


def drop_hooks(item: list[str], ids: set[str]) -> list[str]:
    """A `repo: local` item without the hooks named in `ids`."""
    out: list[str] = []
    skipping = False
    hook_indent = None
    for line in item:
        found = re.match(r"^(\s*)-\s+id:\s*(\S+)", line)
        if found:
            hook_indent = len(found.group(1))
            skipping = found.group(2) in ids
        elif hook_indent is not None and line.strip():
            indent = len(line) - len(line.lstrip())
            if indent <= hook_indent and not line.lstrip().startswith("#"):
                skipping = False
        if not skipping:
            out.append(line)
    return out


def refuse_hooks(repos: list) -> None:
    """Refuse a hooks file this script would have to guess at."""
    pinned = [r for r in repos if SDLC_REPO.match(str(r.get("repo", "")))]
    if any({h.get("id") for h in r.get("hooks") or []} != SDLC_HOOKS for r in pinned):
        raise RefusedError(
            ".pre-commit-config.yaml: python-sdlc is pinned with hooks other than its two"
        )
    local = {h.get("id") for r in repos if r.get("repo") == "local" for h in r.get("hooks") or []}
    if local & SDLC_HOOKS:
        raise RefusedError(
            ".pre-commit-config.yaml already has local sdlc hooks that are not the floating form"
        )
    if not pinned and local & STANDALONE_HOOKS != STANDALONE_HOOKS:
        raise RefusedError(
            ".pre-commit-config.yaml has neither the python-sdlc pin nor both standalone "
            "hooks (gourmand, gatehouse); there is nothing this script knows to replace"
        )


def only_replaced(repos: list) -> bool:
    """True when every hook in the file is one the floating hooks replace."""
    for r in repos:
        if SDLC_REPO.match(str(r.get("repo", ""))):
            continue
        if r.get("repo") != "local":
            return False
        if any(h.get("id") not in STANDALONE_HOOKS for h in r.get("hooks") or []):
            return False
    return True


def without_replaced(lines: list[str]) -> str:
    """The `repos:` items that survive: all but python-sdlc and the standalone hooks."""
    kept: list[str] = []
    for item in split_items(lines):
        repo = re.match(r"^\s*-\s+repo:\s*(\S+)", item[0]).group(1)
        if SDLC_REPO.match(repo):
            continue
        trimmed = drop_hooks(item, STANDALONE_HOOKS) if repo == "local" else item
        if any(re.match(r"^\s*-\s+id:", line) for line in trimmed):
            kept += trimmed
    return "".join(kept).rstrip("\n") + "\n" if kept else ""


FLOATING_ENTRIES = {"sdlc-commit": f"{BOOTSTRAP_AT} commit", "sdlc-push": f"{BOOTSTRAP_AT} push"}
BOTH_HOOK_TYPES = "default_install_hook_types: [pre-commit, pre-push]"


def is_floating(repos: list) -> bool:
    """Already converted: the floating hooks, and nothing they replace."""
    local = {
        h.get("id"): h.get("entry")
        for r in repos
        if r.get("repo") == "local"
        for h in r.get("hooks") or []
    }
    pinned = any(SDLC_REPO.match(str(r.get("repo", ""))) for r in repos)
    return (
        not pinned
        and not set(local) & STANDALONE_HOOKS
        and all(local.get(hook) == entry for hook, entry in FLOATING_ENTRIES.items())
    )


def with_both_hook_types(head: list[str]) -> list[str]:
    """The lines above `repos:`, with `default_install_hook_types` naming both.

    By line, not by pattern: the key may be written in block style, its list
    items on the lines below it, and a comment may say `repos:` too.
    """
    out: list[str] = []
    replaced = False
    skipping = False
    for line in head:
        if skipping and re.match(r"^\s+-", line):
            continue  # an item of the block-style list being replaced
        skipping = False
        if re.match(r"^default_install_hook_types:", line):
            out.append(BOTH_HOOK_TYPES + "\n")
            replaced = skipping = True
            continue
        out.append(line)
    if not replaced:
        out += [BOTH_HOOK_TYPES + "\n", "\n"]
    return out


def convert_hooks(text: str) -> str:
    """The hooks file with the floating hooks in place of the pinned or standalone ones."""
    template = HOOKS_TEMPLATE.read_text()
    if text == template:
        return text
    repos = parse(".pre-commit-config.yaml", text).get("repos") or []
    if is_floating(repos):
        return text
    refuse_hooks(repos)
    if only_replaced(repos):
        # Nothing else in it: the template, header and all, since the old
        # header explains a `rev:` that is gone.
        return template
    lines = text.splitlines(keepends=True)
    start = next((n for n, line in enumerate(lines) if re.match(r"^repos:\s*$", line)), None)
    if start is None:
        raise RefusedError(".pre-commit-config.yaml: no top-level `repos:` line")
    head = "".join(with_both_hook_types(lines[:start])) + lines[start]
    return head + without_replaced(lines[start + 1 :]) + floating_hooks_item()


# ------------------------------------------------------------------ workflows


def jobs_of(parsed: dict) -> dict:
    jobs = parsed.get("jobs")
    return jobs if isinstance(jobs, dict) else {}


def is_sdlc_job(job: object) -> bool:
    return (
        isinstance(job, dict)
        and job.get("name") == "sdlc ci"
        and any(
            isinstance(step, dict) and SDLC_CI_LINE.search(str(step.get("run", "")))
            for step in job.get("steps") or []
        )
    )


def is_gourmand_job(job: object) -> bool:
    return isinstance(job, dict) and bool(GOURMAND_CALL.match(str(job.get("uses", ""))))


# What examples/ci.yml replaces whole: a job with nothing on it but these keys,
# and steps that are only the checkout and the `sdlc ci` step. Anything more --
# another step, a service, an env, a matrix -- is something ci.yml would
# drop, so that workflow keeps its place and only its step is rewritten.
COVERED_SDLC_KEYS = {"name", "runs-on", "steps", "permissions"}
COVERED_GOURMAND_KEYS = {"name", "uses"}
COVERED_WORKFLOW_KEYS = {"name", True, "on", "permissions", "jobs"}  # YAML 1.1 reads on: as True
CHECKOUT = re.compile(r"^actions/checkout@")


def is_covered_step(step: object) -> bool:
    if not isinstance(step, dict):
        return False
    if CHECKOUT.match(str(step.get("uses", ""))):
        return set(step) <= {"uses", "with"} and set(step.get("with") or {}) <= {
            "persist-credentials"
        }
    run = str(step.get("run", "")) + "\n"
    rest = PINNED_STEP.sub("", run, count=1).replace("set -eu\n", "", 1).strip()
    pinned = PINNED_STEP.search(run) is not None and not rest
    floating = run.strip() == FLOATING_CI_LINE
    # examples/ci.yml's step carries one env entry; any other would be dropped.
    env_kept = (step.get("env") or {}) in ({}, FLOATING_ENV)
    return set(step) <= {"name", "run", "env"} and env_kept and (pinned or floating)


FLOATING_ENV = {"SDLC_IMAGE": "${{ vars.SDLC_IMAGE }}"}
GOURMAND_JOB_NAME = "Code Quality (Gourmand)"  # a required check: never renamed by this
# The events a floated `sdlc ci` step may run under. The bootstrap now comes
# from the checkout, so under any event that hands secrets to code a pull
# request controls (pull_request_target, workflow_run, issue_comment and the
# rest) it would run that code with them. An allowlist, not a denylist: an
# event added to GitHub later is refused until someone has looked at it.
FLOATABLE_EVENTS = {"push", "pull_request", "workflow_dispatch", "schedule", "merge_group"}
READ_ONLY = {"contents": "read"}
# The events examples/ci.yml answers. A workflow that also runs on a schedule, a
# release, another branch or a path filter would lose that by being replaced.
STUB_BRANCHES = {"main", "master"}


def is_covered_job(job: object) -> bool:
    """A job examples/ci.yml has in full, so replacing it loses nothing."""
    if is_gourmand_job(job):
        return set(job) <= COVERED_GOURMAND_KEYS and job.get("name") == GOURMAND_JOB_NAME
    return (
        is_sdlc_job(job)
        and set(job) <= COVERED_SDLC_KEYS
        and job.get("permissions", READ_ONLY) == READ_ONLY
        and all(is_covered_step(step) for step in job.get("steps") or [])
    )


def is_covered_trigger(on: object) -> bool:
    """`on:` asks for no more than examples/ci.yml answers."""
    if isinstance(on, str):
        on = {on: None}
    if isinstance(on, list):
        on = dict.fromkeys(on)
    if not isinstance(on, dict) or not set(on) <= {"push", "pull_request", "workflow_dispatch"}:
        return False
    # examples/ci.yml answers every pull request, so a pull_request limited to the
    # default branch loses nothing by being replaced; a push must be to one.
    return not on.get("workflow_dispatch") and all(
        isinstance(on.get(event) or {}, dict)
        and set(on.get(event) or {}) <= {"branches"}
        and set((on.get(event) or {}).get("branches") or []) <= STUB_BRANCHES
        for event in ("push", "pull_request")
    )


def is_covered_workflow(parsed: dict, jobs: list) -> bool:
    """A workflow examples/ci.yml replaces with nothing lost: events, permissions, jobs."""
    return (
        bool(jobs)
        and set(parsed) <= COVERED_WORKFLOW_KEYS
        and is_covered_trigger(parsed.get("on", parsed.get(True)))
        and parsed.get("permissions", READ_ONLY) == READ_ONLY
        and all(is_covered_job(j) for j in jobs)
    )


def float_sdlc_step(name: str, text: str) -> str:
    """The pinned `sdlc ci` step of a workflow the stub cannot replace, floated."""
    if not PINNED_STEP.search(text):
        if re.search(rf"^\s*{re.escape(FLOATING_CI_LINE)}\s*$", text, re.M):
            return text  # already floating
        raise RefusedError(
            f"{name}: its `sdlc ci` step is not the pinned form this script rewrites"
        )
    found = STEP_NAME.search(text)
    if found is None or len(STEP_NAME.findall(text)) != 1:
        raise RefusedError(
            f"{name}: no single `- name: sdlc ci` step directly followed by `run: |`"
        )
    j = found.group("j")
    env = f"{j}env:\n{j}  SDLC_IMAGE: ${{{{ vars.SDLC_IMAGE }}}}\n"
    text = text.replace(found.group(0), f"{found.group('i')}- name: sdlc ci\n{env}{j}run: |\n", 1)
    # `set -eu` stays the step's first line; the clone becomes the bootstrap.
    return PINNED_STEP.sub(lambda m: f"{m.group('i')}{FLOATING_CI_LINE}\n", text, count=1)


# gatehouse#150's change to the guard's script, from new back to old: the one
# earlier script a fleet copy may carry and still be brought up to date.
GUARD_BEFORE_150 = (
    (
        "a file out of .github/workflows/ or .sdlc/ unseen",
        "a workflow out of .github/workflows/ unseen",
    ),
    ("'^(\\.github/workflows|\\.sdlc)/'", "'^\\.github/workflows/'"),
    ("modifies .github/workflows/ or .sdlc/ —", "modifies .github/workflows/ —"),
)


def prior_guard(run: str) -> str:
    """The guard script as it was before gatehouse#150."""
    for after, prior in GUARD_BEFORE_150:
        run = run.replace(after, prior)
    return run


def same_gatehouse(current: dict, canonical: dict) -> bool:
    """`current` is examples/gatehouse.yml in all but comments, or its guard is the one before.

    gatehouse#150 changed the guard's script (it now refuses a fork's change to
    .sdlc/ too). A copy whose guard script is exactly the one before that is
    brought up to date; any other difference in any step, the guard's
    included, keeps the file as it is.
    """

    def guard_runs(doc: dict) -> list:
        guard = (doc.get("jobs") or {}).get("guard")
        steps = guard.get("steps") if isinstance(guard, dict) else None
        return [s.get("run") if isinstance(s, dict) else None for s in steps or []]

    if current == canonical:
        return True
    old, new = guard_runs(current), guard_runs(canonical)
    if len(old) != len(new):
        return False
    before = [prior_guard(run) if isinstance(run, str) else run for run in new]
    if old != before or old == new:
        return False
    jobs = dict(current["jobs"])
    jobs["guard"] = {**jobs["guard"], "steps": canonical["jobs"]["guard"]["steps"]}
    return {**current, "jobs": jobs} == canonical


def classify(files: list[Path]) -> tuple[list[Path], list[Path]]:
    """Workflows the CI stub covers whole, and ones that share an `sdlc ci` job."""
    covered: list[Path] = []
    mixed: list[Path] = []
    for path in files:
        if path.name.startswith("gatehouse"):
            continue
        parsed = parse(path.name, path.read_text())
        jobs = list(jobs_of(parsed).values())
        if is_covered_workflow(parsed, jobs):
            covered.append(path)
        elif any(is_sdlc_job(j) for j in jobs):
            mixed.append(path)
    return covered, mixed


def convert_workflows(repo: Path, planned: dict[Path, str | None], notes: list[str]) -> None:
    wf_dir = repo / ".github" / "workflows"
    files = sorted(wf_dir.glob("*.y*ml")) if wf_dir.is_dir() else []
    covered, mixed = classify(files)
    if mixed:
        # The `sdlc ci` job stays where it is, beside jobs examples/ci.yml does not
        # have; writing ci.yml as well would run it twice. Nothing is deleted.
        for path in mixed:
            on = parse(path.name, path.read_text())
            on = on.get("on", on.get(True))
            events = set(on) if isinstance(on, (dict, list)) else {on}
            if not events <= FLOATABLE_EVENTS:
                others = ", ".join(sorted(map(str, events - FLOATABLE_EVENTS)))
                raise RefusedError(
                    f"{rel(repo, path)} runs on {others}; "
                    f"its `sdlc ci` step could run a pull request's .sdlc/bin/sdlc with secrets"
                )
            planned[path] = float_sdlc_step(rel(repo, path), path.read_text())
            notes.append(f"floated: {rel(repo, path)} (its `sdlc ci` job shares the file)")
        notes += [f"kept: {rel(repo, p)} (no ci.yml stub to take its jobs)" for p in covered]
    else:
        ci = wf_dir / "ci.yml"
        if ci.exists() and ci not in covered:
            raise RefusedError(
                f"{rel(repo, ci)} has jobs the stub does not cover; move them to their "
                f"own workflow, then convert"
            )
        planned.update(dict.fromkeys(covered))
        planned[ci] = CI_TEMPLATE.read_text()

    gatehouse = wf_dir / "gatehouse.yml"
    canonical = GATEHOUSE_TEMPLATE.read_text()
    current = gatehouse.read_text() if gatehouse.exists() else canonical
    if same_gatehouse(parse(gatehouse.name, current), parse(GATEHOUSE_TEMPLATE.name, canonical)):
        planned[gatehouse] = canonical
    else:
        notes.append(f"kept: {rel(repo, gatehouse)} (differs from the stub in more than comments)")

    notes += [
        f"kept: {rel(repo, path)}"
        for path in files
        if path not in planned and path not in covered and path != gatehouse
    ]


# ---------------------------------------------------------------- the repo


def rel(repo: Path, path: Path) -> str:
    return str(path.relative_to(repo))


def refuse_links(repo: Path, paths: list[Path]) -> None:
    """Refuse a planned path that is, or sits under, a symlink.

    A checkout is someone else's content: a `.sdlc` or `.pre-commit-config.yaml`
    that links outside it would have --apply write, delete or chmod a file of
    the person running this.
    """
    for path in paths:
        for part in [path, *path.parents]:
            if part == repo:
                break
            if part.is_symlink():
                raise RefusedError(f"{rel(repo, part)} is a symlink; this script writes no links")


# ---------------------------------------------------------------- the config
#
# A converted repo must never meet "GATE CANNOT RUN" on its first run. Two
# shapes would: a stack this image names but has no checks for yet, and a
# Containerfile that CI cannot build without secrets. The planner's own rules,
# mirrored here, decide which repos those are.

PLANNED_STACKS = ("node", "bootc", "docs")
CONTAINERFILES = ("Containerfile", "Dockerfile")
CONTAINER_CI = ["container-build", "container-smoke", "container-scan"]
# A build that needs a secret CI does not hand `sdlc ci`: a BuildKit secret
# mount, or Red Hat subscription content. The publish workflow keeps building
# these with the secrets it has.
BUILD_SECRETS = re.compile(r"--mount=type=secret|subscription-manager|\brhsm\b", re.I)
SKIP_STACKS_NOTE = (
    "# Written by python-sdlc's scripts/convert_repo.py: the image has no checks\n"
    "# for these stacks yet, and a stack it cannot check makes every run exit 2.\n"
)
SKIP_BUILD_NOTE = (
    "# Written by python-sdlc's scripts/convert_repo.py: the Containerfile needs\n"
    "# build secrets (a secret mount, or RHSM) that `sdlc ci` is not given. The\n"
    "# publish workflow still builds it, with them.\n"
)


def needs_build_secrets(text: str) -> bool:
    """A secret mount or RHSM in an instruction; a comment saying "no RHSM" is not one."""
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    return BUILD_SECRETS.search(code) is not None


def detected_stacks(repo: Path) -> list[str]:
    """The planner's detect_stacks(), over a checkout."""
    found = ["python"] if (repo / "pyproject.toml").is_file() else []
    cf = next((repo / f for f in CONTAINERFILES if (repo / f).is_file()), None)
    bootc = cf is not None and any(
        line.split()[0].upper() == "FROM" and "bootc" in line.lower()
        for line in cf.read_text(errors="replace").splitlines()
        if line.strip()
    )
    if cf is not None and not bootc:
        found.append("container")
    if (repo / "package.json").is_file():
        found.append("node")
    if bootc:
        found.append("bootc")
    return found or ["docs"]


def with_key(text: str, table: str, key: str, value: list[str], note: str) -> str:
    """`text` with `key = value` set in `[table]`, written by line, not by a TOML writer."""
    line = note + f"{key} = {json.dumps(value)}\n"
    header = re.search(rf"^\[{re.escape(table)}\][ \t]*(#.*)?$\n?", text, re.M)
    if header is None:
        return text.rstrip("\n") + ("\n\n" if text.strip() else "") + f"[{table}]\n{line}"
    return text[: header.end()] + line + text[header.end() :]


def convert_config(repo: Path, planned: dict[Path, str | None], notes: list[str]) -> None:
    pyproject = repo / "pyproject.toml"
    path = pyproject if pyproject.is_file() else repo / "sdlc.toml"
    text = planned.get(path) or (path.read_text() if path.is_file() else "")
    try:
        cfg = tomllib.loads(text).get("tool", {}).get("sdlc", {})
    except tomllib.TOMLDecodeError as exc:
        raise RefusedError(f"{rel(repo, path)} is not TOML: {exc}") from exc
    if pyproject.is_file() and "stacks" not in cfg and "skip-stacks" not in cfg:
        return  # a Python repo naming no stacks runs the python stack alone
    drop = set(cfg.get("skip-stacks") or [])
    active = [s for s in [*detected_stacks(repo), *(cfg.get("stacks") or [])] if s not in drop]
    unbuilt = sorted({s for s in active if s in PLANNED_STACKS})
    ci = cfg.get("ci") or {}
    cf = next((repo / f for f in CONTAINERFILES if (repo / f).is_file()), None)
    secrets = (
        "container" in active
        and cf is not None
        and needs_build_secrets(cf.read_text(errors="replace"))
        and "container-build" not in (ci.get("skip") or [])
    )
    if unbuilt:
        if "skip-stacks" in cfg:
            raise RefusedError(
                f"{rel(repo, path)} skip-stacks does not name {', '.join(unbuilt)}, "
                f"which this image cannot check yet; add them by hand"
            )
        text = with_key(text, "tool.sdlc", "skip-stacks", unbuilt, SKIP_STACKS_NOTE)
        notes.append(f"skip-stacks: {', '.join(unbuilt)} in {rel(repo, path)} (not checked yet)")
    if secrets:
        if "skip" in ci:
            raise RefusedError(
                f"{rel(repo, path)} [tool.sdlc.ci] skip exists; the Containerfile needs build "
                f"secrets, so add {', '.join(CONTAINER_CI)} to it by hand"
            )
        text = with_key(text, "tool.sdlc.ci", "skip", CONTAINER_CI, SKIP_BUILD_NOTE)
        notes.append(f"skip in ci: the container checks, in {rel(repo, path)} (build secrets)")
    if not (unbuilt or secrets):
        return
    # Read back what was written: the stacks and the skips must be the ones
    # meant, in a file that is still TOML, or nothing is written at all.
    try:
        after = tomllib.loads(text).get("tool", {}).get("sdlc", {})
    except tomllib.TOMLDecodeError as exc:
        raise RefusedError(f"{rel(repo, path)} would not be TOML: {exc}") from exc
    if not set(unbuilt) <= set(after.get("skip-stacks") or []) or (
        secrets and not set(CONTAINER_CI) <= set((after.get("ci") or {}).get("skip") or [])
    ):
        raise RefusedError(f"{rel(repo, path)}: the keys written did not read back as meant")
    planned[path] = text


def plan(repo: Path) -> tuple[dict[Path, str | None], list[str]]:
    """Every file the conversion writes (None: deletes), and what it kept."""
    planned: dict[Path, str | None] = {repo / BOOTSTRAP_AT: BOOTSTRAP.read_text()}
    notes: list[str] = []
    hooks = repo / ".pre-commit-config.yaml"
    wf_dir = repo / ".github" / "workflows"
    refuse_links(repo, [hooks, *planned, wf_dir, repo / "pyproject.toml", repo / "sdlc.toml"])
    planned[hooks] = convert_hooks(hooks.read_text() if hooks.exists() else "")
    convert_workflows(repo, planned, notes)
    convert_config(repo, planned, notes)
    notes.append(f"writes: {BOOTSTRAP_AT}, executable (python-sdlc's bin/sdlc-bootstrap)")
    # An image reference in anything that survives, and in the repo's config.
    candidates = [repo / "pyproject.toml", repo / "sdlc.toml"]
    candidates += sorted(wf_dir.glob("*.y*ml")) if wf_dir.is_dir() else []
    refuse_links(repo, candidates)
    for path in candidates:
        if not path.exists() or planned.get(path, "") is None:
            continue
        text = planned.get(path) or path.read_text()
        moved = re.sub(re.escape(OLD_IMAGE) + r"(?![\w.-])", NEW_IMAGE, text)
        if moved != text:
            planned[path] = moved
    refuse_links(repo, list(planned))
    verify(repo, planned)
    return planned, notes


def parse_back(repo: Path, planned: dict[Path, str | None]) -> None:
    """Refuse a plan with a YAML or TOML file that would not load."""
    for path, text in planned.items():
        if text is not None and path.suffix in (".yml", ".yaml"):
            parse(rel(repo, path), text)
        if text is not None and path.suffix == ".toml":
            try:
                tomllib.loads(text)
            except tomllib.TOMLDecodeError as exc:
                raise RefusedError(f"{rel(repo, path)} would not be TOML: {exc}") from exc


def verify_hooks(text: str) -> None:
    """Refuse a hooks file that does not install both types and float."""
    hooks = parse(".pre-commit-config.yaml", text)
    types = hooks.get("default_install_hook_types") or []
    if not (isinstance(types, list) and {"pre-commit", "pre-push"} <= set(types)):
        raise RefusedError("the converted hooks file does not install both hook types")
    if not is_floating(hooks.get("repos") or []):
        raise RefusedError("the converted hooks file does not run .sdlc/bin/sdlc as its only sdlc")


def verify(repo: Path, planned: dict[Path, str | None]) -> None:
    """Refuse a plan whose files do not load, or do not wire what they should.

    Every YAML file this writes is parsed back, and the result is held to the
    wiring itself -- the floating hooks, both hook types, a job named `sdlc ci`
    running the bootstrap -- rather than to how the text was produced.
    """
    parse_back(repo, planned)
    verify_hooks(planned[repo / ".pre-commit-config.yaml"] or "")
    if planned[repo / BOOTSTRAP_AT] != BOOTSTRAP.read_text():
        raise RefusedError(f"{BOOTSTRAP_AT} would not be bin/sdlc-bootstrap")
    wf_dir = repo / ".github" / "workflows"
    texts = {p: planned.get(p, "") for p in planned if p.parent == wf_dir and planned[p]}
    texts |= {
        p: p.read_text()
        for p in (sorted(wf_dir.glob("*.y*ml")) if wf_dir.is_dir() else [])
        if p not in planned
    }
    # Nothing may still clone python-sdlc at the hooks file's rev: the rev is
    # gone, so such a step would exit 2 on every run from now on.
    for path, text in texts.items():
        if PINNED_STEP.search(text) or "github.com/crunchtools/python-sdlc" in text:
            raise RefusedError(
                f"{rel(repo, path)} still fetches python-sdlc by the rev this removes; "
                f"convert that step by hand"
            )
    floating = re.compile(rf"^\s*{re.escape(FLOATING_CI_LINE)}\s*$", re.M)
    if not any(
        is_sdlc_job(job)
        and any(floating.search(str(step.get("run", ""))) for step in job.get("steps") or [])
        for path, text in texts.items()
        for job in jobs_of(parse(rel(repo, path), text)).values()
    ):
        raise RefusedError(
            "no converted workflow has a job named `sdlc ci` running .sdlc/bin/sdlc ci"
        )


def diff(repo: Path, planned: dict[Path, str | None]) -> str:
    out = []
    for path in sorted(planned):
        before = path.read_text() if path.exists() else ""
        after = planned[path] or ""
        if before == after and (planned[path] is not None or not path.exists()):
            continue
        name = rel(repo, path)
        out += difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            f"a/{name}" if path.exists() else "/dev/null",
            f"b/{name}" if planned[path] is not None else "/dev/null",
        )
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("repo", type=Path, help="a checkout of the repo to convert")
    ap.add_argument("--apply", action="store_true", help="write the changes; default prints them")
    args = ap.parse_args()
    if yaml is None:
        print("convert_repo: needs PyYAML (python3-pyyaml)", file=sys.stderr)
        return 2
    repo = args.repo.resolve()
    if not (repo / ".git").exists():
        print(f"convert_repo: {repo} is not a git checkout", file=sys.stderr)
        return 2
    try:
        planned, notes = plan(repo)
    except RefusedError as exc:
        print(f"convert_repo: {repo.name}: NOT CONVERTED: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(diff(repo, planned))
    for note in notes:
        print(f"convert_repo: {repo.name}: {note}", file=sys.stderr)
    if args.apply:
        # Again at the moment of writing: plan() refused links, and the
        # checkout is not ours to trust between then and now.
        try:
            refuse_links(repo, list(planned))
        except RefusedError as exc:
            print(f"convert_repo: {repo.name}: NOT CONVERTED: {exc}", file=sys.stderr)
            return 2
        for path, text in planned.items():
            if text is None:
                path.unlink(missing_ok=True)
            elif not path.exists() or path.read_text() != text:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        (repo / BOOTSTRAP_AT).chmod(0o755)
    return 0


if __name__ == "__main__":
    sys.exit(main())
