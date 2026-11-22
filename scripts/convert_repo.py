#!/usr/bin/env python3
"""Convert one repo to the floating sdlc: no `rev:` to pin, ever again.

Rewrites a checkout so its gates come from quay.io/crunchtools/sdlc:latest
with nothing in the repo naming a release -- which makes the PR wave that
applies this the last one: what the gates run changes when the image does.

  .sdlc/bin/sdlc           crunchtools/sdlc's bin/sdlc-bootstrap: asks the image for
                           its launcher and runs it. The one file a converted
                           repo carries; it names no release, so nothing bumps it
  .pre-commit-config.yaml  the rev-pinned sdlc hooks (under either repo name), and
                           the standalone
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
  the old name             the repo was crunchtools/python-sdlc: its image
                           (quay.io/crunchtools/python-sdlc) and a reusable
                           workflow's `uses: crunchtools/python-sdlc/...@` become
                           crunchtools/sdlc's, in workflows, config and the hooks
                           file. The hooks `repo:` pin needs no rename: it is
                           replaced, under either name. A repo already floating
                           gets this, and the current bootstrap, alone

Deterministic and offline: the same checkout gives the same diff. It prints
that diff and changes nothing without --apply. It never touches .specify/,
the per-repo constitution.

Exit 0 when it converted (or there was nothing to do), 2 when the repo has a
shape it will not guess at -- named, so it can be converted by hand.

Usage: scripts/convert_repo.py REPO_DIR [--apply] [--unbuilt-stacks LIST]
Needs PyYAML, to read the workflows; it never writes YAML, only text.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import TypeGuard

try:
    import yaml
except ImportError:  # the image's constitution venv has it; main() says so
    HAS_YAML = False
else:
    HAS_YAML = True

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

# The repo was crunchtools/python-sdlc, and its image quay.io/crunchtools/
# python-sdlc, which is now a mirror that goes away after the fleet wave. Each
# old reference and its new form; a longer name (python-sdlc-extras, python-sdlc.x)
# is not one, but a sentence-ending period after the name is not part of it.
RENAMED = (
    (re.compile(r"quay\.io/crunchtools/python-sdlc(?![\w-]|\.[\w-])"), "quay.io/crunchtools/sdlc"),
    (
        re.compile(r"(?<![\w.-])crunchtools/python-sdlc/(?=\.github/workflows/[^\s@]+@)"),
        "crunchtools/sdlc/",
    ),
)
# The pinned hooks repo under either name: GitHub redirects the old one.
SDLC_REPO = re.compile(r"^https://github\.com/crunchtools/(?:python-)?sdlc(?:\.git)?/?$")
# A clone of sdlc under either name, over https or ssh.
CLONES_SDLC = re.compile(
    r"git clone\b.*github\.com[:/]crunchtools/(?:python-)?sdlc(?:\.git)?(?![\w-]|\.[\w-])"
)
STANDALONE_HOOKS = {"gourmand", "gatehouse"}
SDLC_HOOKS = {"sdlc-commit", "sdlc-push"}
GOURMAND_CALL = re.compile(r"^crunchtools/gatehouse/\.github/workflows/gourmand\.yml@")
SDLC_CI_LINE = re.compile(r"^\s*[\"']?(?:\S*/)?sdlc[\"']?\s+ci\b", re.M)

# The step every converted repo's `sdlc ci` job runs today, rev read from the
# hooks file and the launcher cloned at it, the launcher's path quoted or not.
# Matched exactly, indent aside: a step that says anything else is not one this
# script will guess at.
PINNED_BODY = (
    r"[ \t]*rev=\$\(awk "
    r"'/repo: https:\\/\\/github\.com\\/crunchtools\\/(?:python-)?sdlc/ \{f=1; next\}\n"
    r"[ \t]*f && /rev:/ \{print \$2; exit\}' \.pre-commit-config\.yaml\)\n"
    r"[ \t]*\[ -n \"\$rev\" \] \|\| "
    r"\{ echo \"no (?:python-)?sdlc rev in \.pre-commit-config\.yaml\" >&2; exit 2; \}\n"
    r"[ \t]*git clone -q --depth 1 --branch \"\$rev\" "
    r"https://github\.com/crunchtools/(?:python-)?sdlc \"\$RUNNER_TEMP/sdlc\"\n"
    r"[ \t]*(?P<q>\"?)\$RUNNER_TEMP/sdlc/bin/sdlc(?P=q) ci\n"
)
PINNED_STEP = re.compile(r"^" + PINNED_BODY, re.M)
STEP_NAME = re.compile(r"^(?P<i>[ \t]*)- name: sdlc ci\n(?P<j>[ \t]*)run: \|\n", re.M)
# The whole pinned step: its name, a `set -eu` the old form opened with, and
# the pinned body, with no line after it still inside the `run: |` block. The
# validator wants the step's run to be exactly the launcher, so the block goes.
WHOLE_STEP = re.compile(
    r"^(?P<i>[ \t]*)- name: sdlc ci\n(?P<j>[ \t]*)run: \|\n"
    r"(?:[ \t]*set -eu\n)?" + PINNED_BODY + r"(?!(?P=j)[ \t]+\S)",
    re.M,
)
# What an earlier convert_repo.py left: the launcher still inside a block that
# opens with `set -eu`, which v1.46.0's exact-run check fails.
LEFTOVER_RUN = re.compile(
    rf"^(?P<j>[ \t]*)run: \|\n[ \t]*set -eu\n[ \t]*{re.escape(FLOATING_CI_LINE)}\n"
    r"(?!(?P=j)[ \t]+\S)",
    re.M,
)


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
        raise RefusedError(".pre-commit-config.yaml: sdlc is pinned with hooks other than its two")
    local = {h.get("id") for r in repos if r.get("repo") == "local" for h in r.get("hooks") or []}
    if local & SDLC_HOOKS:
        raise RefusedError(
            ".pre-commit-config.yaml already has local sdlc hooks that are not the floating form"
        )
    if not pinned and local & STANDALONE_HOOKS != STANDALONE_HOOKS:
        raise RefusedError(
            ".pre-commit-config.yaml has neither the sdlc pin nor both standalone "
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
    """The `repos:` items that survive: all but the sdlc pin and the standalone hooks."""
    kept: list[str] = []
    for item in split_items(lines):
        named = re.match(r"^\s*-\s+repo:\s*(\S+)", item[0])
        if not named:
            raise RefusedError(f".pre-commit-config.yaml: {item[0].strip()!r} names no repo")
        repo = named.group(1)
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


def is_sdlc_job(job: object) -> TypeGuard[dict]:
    return (
        isinstance(job, dict)
        and job.get("name") == "sdlc ci"
        and any(
            isinstance(step, dict) and SDLC_CI_LINE.search(str(step.get("run", "")))
            for step in job.get("steps") or []
        )
    )


def is_gourmand_job(job: object) -> TypeGuard[dict]:
    return isinstance(job, dict) and bool(GOURMAND_CALL.match(str(job.get("uses", ""))))


# What examples/ci.yml replaces whole: a job with nothing on it but these keys,
# and steps that are only the checkout and the `sdlc ci` step. Anything more --
# another step, a service, an env, a matrix -- is something ci.yml would
# drop, so that workflow keeps its place and only its step is rewritten.
COVERED_SDLC_KEYS = {"name", "runs-on", "steps", "permissions", "timeout-minutes"}
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
        if LEFTOVER_RUN.search(text):
            return LEFTOVER_RUN.sub(lambda m: f"{m.group('j')}run: {FLOATING_CI_LINE}\n", text)
        if re.search(rf"^\s*run: {re.escape(FLOATING_CI_LINE)}\s*$", text, re.M):
            return text  # already floating
        raise RefusedError(
            f"{name}: its `sdlc ci` step is not the pinned form this script rewrites"
        )
    found = WHOLE_STEP.search(text)
    if found is None or len(STEP_NAME.findall(text)) != 1:
        raise RefusedError(
            f"{name}: no single `- name: sdlc ci` step whose `run: |` is the pinned "
            f"form and nothing else"
        )
    i, j = found.group("i"), found.group("j")
    env = f"{j}env:\n{j}  SDLC_IMAGE: ${{{{ vars.SDLC_IMAGE }}}}\n"
    floated = f"{i}- name: sdlc ci\n{env}{j}run: {FLOATING_CI_LINE}\n"
    return text[: found.start()] + floated + text[found.end() :]


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
    if current == canonical:
        return True
    guard = (current.get("jobs") or {}).get("guard")
    new = (canonical.get("jobs") or {}).get("guard") or {}
    if not isinstance(guard, dict):
        return False
    steps = guard.get("steps")
    if not isinstance(steps, list) or len(steps) != len(new.get("steps") or []):
        return False
    # Only a `run` that is exactly the pre-#150 script becomes the new one;
    # every other field of every step is then compared as it is.
    updated = [
        {**step, "run": want["run"]}
        if isinstance(step, dict)
        and isinstance(want, dict)
        and isinstance(want.get("run"), str)
        and step.get("run") == prior_guard(want["run"])
        else step
        for step, want in zip(steps, new["steps"], strict=True)
    ]
    jobs = {**current["jobs"], "guard": {**guard, "steps": updated}}
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


TEMPLATE_RUNNER = "ubuntu-latest"


def runner_block(text: str, job_id: str) -> tuple[list[str], int] | None:
    """The `runs-on:` lines of job `job_id`, the comment just above them
    included, and their indent."""
    lines = text.splitlines(keepends=True)

    def indent_of(x: str) -> int:
        return len(x) - len(x.lstrip())

    top = next((i for i, x in enumerate(lines) if re.match(r"jobs:\s*$", x)), None)
    head = next(
        (
            i
            for i in range((top or 0) + 1, len(lines))
            if re.match(rf"\s+{re.escape(job_id)}:\s*$", lines[i])
        ),
        None,
    )
    if top is None or head is None:
        return None
    stop = next(
        (
            i
            for i in range(head + 1, len(lines))
            if lines[i].strip()
            and not lines[i].lstrip().startswith("#")
            and indent_of(lines[i]) <= indent_of(lines[head])
        ),
        len(lines),
    )
    at = next((i for i in range(head + 1, stop) if lines[i].lstrip().startswith("runs-on:")), None)
    if at is None:
        return None
    indent = indent_of(lines[at])
    start = at
    while (
        start > head + 1
        and lines[start - 1].strip().startswith("#")
        and indent_of(lines[start - 1]) == indent
    ):
        start -= 1
    end = at + 1
    while end < stop and lines[end].strip() and indent_of(lines[end]) > indent:
        end += 1
    return lines[start:end], indent


# The contexts a job's `runs-on` may read that examples/ci.yml's `sdlc` job does not
# have: copied across, the expression has nothing to resolve against.
FOREIGN_CONTEXTS = re.compile(r"\$\{\{(?:(?!\}\}).)*\b(matrix|inputs|needs|strategy)\b", re.S)


def with_runner(template: str, covered: list[Path], notes: list[str]) -> str:
    """examples/ci.yml, on the runner the repo's own `sdlc ci` job chose.

    A fleet job routes same-repo pull requests to the cached self-hosted runner
    and pushes to the uncached one; the template's `ubuntu-latest` would move every
    run to GitHub's hosted runners, about three times slower and billed for a
    private repo. One runner across the covered files is kept; two that disagree,
    one the stub cannot carry, or one that reads `matrix`, `inputs`, `needs` or
    `strategy` leave the template as it is, and say so in `notes`.
    """
    runners: dict[str, tuple[object, tuple[list[str], int] | None]] = {}
    for path in covered:
        text = path.read_text()
        jobs = jobs_of(parse(path.name, text))
        job_id = next((k for k, j in jobs.items() if is_sdlc_job(j)), None)
        runs_on = None if job_id is None else jobs[job_id].get("runs-on")
        key = json.dumps(runs_on, sort_keys=True)
        if runs_on is not None and runners.get(key, (None, None))[1] is None:
            # Two files on one runner: the block either can lift serves.
            runners[key] = (runs_on, runner_block(text, str(job_id)))

    def fallback(why: str) -> str:
        notes.append(f"runner: ci.yml takes {TEMPLATE_RUNNER}, {why}")
        return template

    if not runners:
        return template
    if len(runners) > 1:
        return fallback("the covered workflows' `sdlc ci` jobs disagree on theirs")
    [(key, (want, found))] = runners.items()
    if want == TEMPLATE_RUNNER:
        return template
    if found is None:
        return fallback("the `sdlc ci` job's runs-on could not be lifted out")
    if (m := FOREIGN_CONTEXTS.search(key)) is not None:
        return fallback(f"the `sdlc ci` job's runs-on reads `{m[1]}`, which the stub has none of")
    block, indent = found
    shift = 4 - indent  # the template's job keys sit at four spaces
    moved = [(" " * shift + x if shift >= 0 else x[-shift:]) if x.strip() else x for x in block]
    new = template.replace(f"    runs-on: {TEMPLATE_RUNNER}\n", "".join(moved), 1)
    try:  # an alias whose anchor stayed behind does not load at all
        got = jobs_of(yaml.safe_load(new)).get("sdlc", {}).get("runs-on")
    except yaml.YAMLError:
        got = None
    return new if got == want else fallback("the `sdlc ci` job's runs-on did not read back")


def convert_workflows(repo: Path, planned: dict[Path, str | None], notes: list[str]) -> None:
    wf_dir = repo / ".github" / "workflows"
    files = sorted(wf_dir.glob("*.y*ml")) if wf_dir.is_dir() else []
    covered, mixed = classify(files)
    if mixed:
        # The `sdlc ci` job stays where it is, beside jobs examples/ci.yml does not
        # have; writing ci.yml as well would run it twice. Nothing is deleted.
        for path in mixed:
            parsed = parse(path.name, path.read_text())
            on = parsed.get("on", parsed.get(True))
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
        planned[ci] = with_runner(CI_TEMPLATE.read_text(), covered, notes)

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

# The stacks the image once named without checks. Since sdlc#92 it checks all
# three, so --unbuilt-stacks defaults to none of them; one that is not listed is
# checked, so a skip-stacks entry the converter wrote for it comes out again.
PLANNED_STACKS = ("node", "bootc", "docs")
CONTAINERFILES = ("Containerfile", "Dockerfile")
CONTAINER_CI = ["container-build", "container-smoke", "container-scan"]
# A bootc image is built and scanned like a container but linted rather than
# smoke-run, so the same build chain is a different list of checks.
BOOTC_CI = ["container-build", "bootc-lint", "container-scan"]
# A build that needs a secret CI does not hand `sdlc ci`: a BuildKit secret
# mount, Red Hat subscription content, or a base image registry.redhat.io will
# not serve without a login. The publish workflow keeps building these with the
# secrets it has.
BUILD_SECRETS = re.compile(
    r"--mount=type=secret|subscription-manager|\brhsm\b|registry\.redhat\.io/", re.I
)
SKIP_STACKS_NOTE = (
    "# Written by crunchtools/sdlc's scripts/convert_repo.py: the image has no\n"
    "# checks for these stacks yet, and a stack it cannot check makes every run\n"
    "# exit 2.\n"
)
SKIP_BUILD_NOTE = (
    "# Written by crunchtools/sdlc's scripts/convert_repo.py: the Containerfile\n"
    "# needs build secrets (a secret mount, or RHSM) that `sdlc ci` is not given.\n"
    "# The publish workflow still builds it, with them.\n"
)


def needs_build_secrets(text: str) -> bool:
    """A secret mount or RHSM in an instruction; a comment saying "no RHSM" is not one."""
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    return BUILD_SECRETS.search(code) is not None


# Mirrors the planner's final_base() in `sdlc`: this file does not import it.
BUILD_VAR = re.compile(r"\$(?:\{(?P<braced>\w+)(?::?-(?P<default>[^}]*))?\}|(?P<bare>\w+))")
FROM_AS_WORDS = 3  # image, AS, name


def logical_lines(text: str) -> list[str]:
    """Containerfile instructions: continuations joined, comment lines dropped."""
    lines, buf = [], ""
    for raw in text.splitlines():
        if raw.lstrip().startswith("#"):
            continue  # a comment, even inside a continuation, is not an instruction
        if raw.rstrip().endswith("\\"):
            buf += raw.rstrip()[:-1] + " "
            continue
        lines.append((buf + raw).strip())
        buf = ""
    if buf.strip():
        lines.append(buf.strip())
    return lines


def build_stages(text: str) -> tuple[dict[str, str], list[tuple[str, str | None]]]:
    """The global ARG defaults, and each stage's (image word, lowercased AS name)."""
    args: dict[str, str] = {}
    stages: list[tuple[str, str | None]] = []
    for words in (line.split() for line in logical_lines(text)):
        op = words[0].upper() if words else ""
        if op == "ARG" and not stages:
            for name, eq, value in (w.partition("=") for w in words[1:]):
                if eq:
                    args[name] = value.strip("\"'")
        elif op == "FROM":
            rest = [w for w in words[1:] if not w.startswith("--")]
            is_aliased = len(rest) >= FROM_AS_WORDS and rest[1].upper() == "AS"
            if rest:
                stages.append((rest[0], rest[2].lower() if is_aliased else None))
    return args, stages


def final_base(text: str) -> str | None:
    """The image the FINAL stage of a Containerfile is built on, or None.

    Substring-matching every FROM line called a bootc builder stage under a plain
    runtime image bootc, and missed `ARG BASE=...-bootc` + `FROM ${BASE}`. What
    ships is the last stage, so resolve that: fill $X, ${X} and ${X:-d} from the
    global ARG defaults, and follow a `FROM <earlier-stage>` back to its own
    base. An unset variable gives None.
    """
    args, stages = build_stages(text)
    index = len(stages) - 1
    seen: set[int] = set()
    while index >= 0 and index not in seen:
        seen.add(index)
        image = stages[index][0]
        names = [m["braced"] or m["bare"] for m in BUILD_VAR.finditer(image)]
        defaults = [m["default"] for m in BUILD_VAR.finditer(image)]
        if any(n not in args and d is None for n, d in zip(names, defaults, strict=True)):
            return None
        image = BUILD_VAR.sub(
            lambda m: args.get(m["braced"] or m["bare"], m["default"] or ""), image
        )
        earlier = [i for i in range(index) if stages[i][1] == image.lower()]
        if not earlier:
            return image or None
        index = earlier[-1]
    return None


def detected_stacks(repo: Path) -> list[str]:
    """The planner's detect_stacks(), over a checkout."""
    found = ["python"] if (repo / "pyproject.toml").is_file() else []
    cf = next((repo / f for f in CONTAINERFILES if (repo / f).is_file()), None)
    base = final_base(cf.read_text(errors="replace")) if cf is not None else None
    bootc = base is not None and "bootc" in base.lower()
    if cf is not None and not bootc:
        found.append("container")
    nested = False
    for here, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git")]
        if Path(here) != repo and "package.json" in files:
            nested = True
            break
    if (repo / "package.json").is_file() or nested:
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


SKIP_STACKS_LINE = re.compile(r"^(skip-stacks[ \t]*=[ \t]*)\[[^\]\n]*\]([ \t]*#.*)?[ \t]*\n?", re.M)


def without_stacks(text: str, kept: list[str], where: str) -> str:
    """`text` with the skip-stacks line holding only `kept`, or gone (with its note) if empty."""
    found = SKIP_STACKS_LINE.search(text)
    if found is None:
        raise RefusedError(f"{where} skip-stacks is not one line; drop the checked stacks by hand")
    if kept:
        comment = found.group(2) or ""
        line = f"{found.group(1)}{json.dumps(kept)}{comment}\n"
        return text[: found.start()] + line + text[found.end() :]
    head = text[: found.start()]
    head = head.removesuffix(SKIP_STACKS_NOTE)
    return head + text[found.end() :]


def drop_checked(
    text: str, kept: list[str], checked: list[str], where: str, notes: list[str]
) -> str:
    """`text` without the skip-stacks entries for `checked` stacks, saying so in `notes`."""
    if not checked:
        return text
    notes.append(f"skip-stacks: dropped {', '.join(checked)} in {where} (now checked)")
    return without_stacks(text, kept, where)


def convert_config(
    repo: Path,
    planned: dict[Path, str | None],
    notes: list[str],
    unbuilt_stacks: tuple[str, ...] = (),
) -> None:
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
    unbuilt = sorted({s for s in active if s in unbuilt_stacks})
    # A planned stack the image now checks, still skipped: its entry comes out.
    listed = list(cfg.get("skip-stacks") or [])
    checked = [s for s in listed if s in PLANNED_STACKS and s not in unbuilt_stacks]
    kept = [s for s in listed if s not in checked]
    ci = cfg.get("ci") or {}
    cf = next((repo / f for f in CONTAINERFILES if (repo / f).is_file()), None)
    chain = BOOTC_CI if "bootc" in active else CONTAINER_CI
    secrets = (
        ("container" in active or "bootc" in active)
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
    text = drop_checked(text, kept, checked, rel(repo, path), notes)
    if secrets:
        if "skip" in ci:
            raise RefusedError(
                f"{rel(repo, path)} [tool.sdlc.ci] skip exists; the Containerfile needs build "
                f"secrets, so add {', '.join(chain)} to it by hand"
            )
        text = with_key(text, "tool.sdlc.ci", "skip", chain, SKIP_BUILD_NOTE)
        notes.append(f"skip in ci: the container checks, in {rel(repo, path)} (build secrets)")
    if not (unbuilt or secrets or checked):
        return
    # Read back what was written: the stacks and the skips must be the ones
    # meant, in a file that is still TOML, or nothing is written at all.
    try:
        after = tomllib.loads(text).get("tool", {}).get("sdlc", {})
    except tomllib.TOMLDecodeError as exc:
        raise RefusedError(f"{rel(repo, path)} would not be TOML: {exc}") from exc
    now = after.get("skip-stacks") or []
    if (
        not set(unbuilt) <= set(now)
        or (checked and now != kept)
        or (secrets and not set(chain) <= set((after.get("ci") or {}).get("skip") or []))
    ):
        raise RefusedError(f"{rel(repo, path)}: the keys written did not read back as meant")
    planned[path] = text


def plan(
    repo: Path, unbuilt_stacks: tuple[str, ...] = ()
) -> tuple[dict[Path, str | None], list[str]]:
    """Every file the conversion writes (None: deletes), and what it kept."""
    planned: dict[Path, str | None] = {repo / BOOTSTRAP_AT: BOOTSTRAP.read_text()}
    notes: list[str] = []
    hooks = repo / ".pre-commit-config.yaml"
    wf_dir = repo / ".github" / "workflows"
    refuse_links(repo, [hooks, *planned, wf_dir, repo / "pyproject.toml", repo / "sdlc.toml"])
    planned[hooks] = convert_hooks(hooks.read_text() if hooks.exists() else "")
    convert_workflows(repo, planned, notes)
    convert_config(repo, planned, notes, unbuilt_stacks)
    notes.append(f"writes: {BOOTSTRAP_AT}, executable (crunchtools/sdlc's bin/sdlc-bootstrap)")
    # The old name in anything that survives, and in the repo's config.
    candidates = [hooks, repo / "pyproject.toml", repo / "sdlc.toml"]
    candidates += sorted(wf_dir.glob("*.y*ml")) if wf_dir.is_dir() else []
    refuse_links(repo, candidates)
    for path in candidates:
        if not path.exists() or planned.get(path, "") is None:
            continue
        text = planned.get(path) or path.read_text()
        moved = text
        for old, new in RENAMED:
            moved = old.sub(new, moved)
        if moved != text:
            planned[path] = moved
            notes.append(f"renamed: python-sdlc to sdlc in {rel(repo, path)}")
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
    texts = {p: text for p, text in planned.items() if p.parent == wf_dir and text}
    texts |= {
        p: p.read_text()
        for p in (sorted(wf_dir.glob("*.y*ml")) if wf_dir.is_dir() else [])
        if p not in planned
    }
    # Nothing may still clone sdlc, under either name, at the hooks file's rev:
    # the rev is gone, so such a step would exit 2 on every run from now on.
    for path, text in texts.items():
        if (
            PINNED_STEP.search(text)
            or "github.com/crunchtools/python-sdlc" in text
            or CLONES_SDLC.search(text)
        ):
            raise RefusedError(
                f"{rel(repo, path)} still fetches sdlc by the rev this removes; "
                f"convert that step by hand"
            )
    # Exactly the launcher, as constitution v1.46.0's check_floating_ci reads it:
    # a `set -eu` beside it fails the required check.
    if not any(
        is_sdlc_job(job)
        and any(
            str(step.get("run", "")).strip() == FLOATING_CI_LINE for step in job.get("steps") or []
        )
        for path, text in texts.items()
        for job in jobs_of(parse(rel(repo, path), text)).values()
    ):
        raise RefusedError(
            "no converted workflow has a job named `sdlc ci` running .sdlc/bin/sdlc ci"
        )


def diff(repo: Path, planned: dict[Path, str | None]) -> str:
    out: list[str] = []
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


def unbuilt_stacks_of(ap: argparse.ArgumentParser, value: str) -> tuple[str, ...]:
    """The --unbuilt-stacks list; a name that is not a planned stack is a typo, not a stack."""
    stacks = tuple(s.strip() for s in value.split(",") if s.strip())
    if not set(stacks) <= set(PLANNED_STACKS):
        ap.error(f"--unbuilt-stacks names only some of {', '.join(PLANNED_STACKS)}")
    return stacks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("repo", type=Path, help="a checkout of the repo to convert")
    ap.add_argument("--apply", action="store_true", help="write the changes; default prints them")
    ap.add_argument(
        "--unbuilt-stacks",
        default="",
        metavar="LIST",
        help="comma-separated stacks the image still has no checks for (default: %(default)s); "
        "empty means all exist, and a skip-stacks entry for any other is dropped",
    )
    args = ap.parse_args()
    unbuilt = unbuilt_stacks_of(ap, args.unbuilt_stacks)
    if not HAS_YAML:
        print("convert_repo: needs PyYAML (python3-pyyaml)", file=sys.stderr)
        return 2
    repo = args.repo.resolve()
    if not (repo / ".git").exists():
        print(f"convert_repo: {repo} is not a git checkout", file=sys.stderr)
        return 2
    try:
        planned, notes = plan(repo, unbuilt)
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
