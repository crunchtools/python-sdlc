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
  .github/workflows/       examples/ci.yml, byte for byte, as ci.yml, and every other
                           workflow deleted -- when each of its jobs is a row of
                           the table in docs/marketplace-free-ci.md ("What `sdlc
                           event` runs"): the `sdlc ci` job, a call of one of an
                           allowlist of the fleet's reusable workflows (constitution,
                           gatehouse, ashigaru), gatehouse.yml and release.yml as the
                           examples have them, an inline CodeQL, PyPI publish or
                           Dependabot automerge job. Triggers do not matter: ci.yml
                           answers every event. A workflow with any other job is
                           named and the repo refused, whole -- a conversion that
                           deletes some and keeps the rest would run both. The
                           RPM, Pages and repo-specific ones belong in
                           [tool.sdlc.commands] (`sdlc run <name>`), by hand
  [tool.sdlc]              what a deleted workflow said per repo: `publish = "pypi"`
                           for a PyPI publish job, a container-release call's inputs
                           (but `sign` and `downstream`) as [tool.sdlc.image]. A
                           value already there that differs is refused, never replaced
  the old name             the repo was crunchtools/python-sdlc: its image
                           (quay.io/crunchtools/python-sdlc) and a reusable
                           workflow's `uses: crunchtools/python-sdlc/...@` become
                           crunchtools/sdlc's, in config and the hooks file. The hooks
                           `repo:` pin needs no rename: it is
                           replaced, under either name. A repo already floating
                           gets this, and the current bootstrap, alone

Deterministic and offline: the same checkout gives the same diff. It prints
that diff and changes nothing without --apply. It never touches .specify/,
the per-repo constitution, and sets no variable, ruleset, PyPI publisher or
repo setting: a Pages source is a human step.

Exit 0 when it converted (or there was nothing to do), 2 when the repo has a
shape it will not guess at -- named, so it can be converted by hand.

Usage: scripts/convert_repo.py REPO_DIR [--apply] [--unbuilt-stacks LIST]
Needs PyYAML, to read the workflows; it never writes YAML, only text.
"""

from __future__ import annotations

import argparse
import difflib
import functools
import json
import os
import re
import sys
import tomllib
from collections.abc import Sequence
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
STANDALONE_HOOKS = {"gourmand", "gatehouse"}
SDLC_HOOKS = {"sdlc-commit", "sdlc-push"}
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
#
# examples/ci.yml answers every event, so a repo's own workflows go: each job of
# each one is a row of the spec's "What `sdlc event` runs" table, or the repo is
# refused. Be strict here -- deleting real work is the danger, refusing is safe.


def jobs_of(parsed: dict) -> dict:
    jobs = parsed.get("jobs")
    return jobs if isinstance(jobs, dict) else {}


def keys_within(value: object, allowed: set[str]) -> bool:
    """An absent mapping, or one with no key outside `allowed`; anything else is not."""
    return value is None or (isinstance(value, dict) and set(value) <= allowed)


def run_lines(step: dict) -> list[str]:
    """A `run:` as its commands: continuations joined, comments and blank lines gone."""
    joined = str(step.get("run", "")).replace("\\\n", " ")
    lines = (line.strip() for line in joined.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def is_sdlc_job(job: object) -> TypeGuard[dict]:
    return (
        isinstance(job, dict)
        and job.get("name") == "sdlc ci"
        and any(
            isinstance(step, dict) and SDLC_CI_LINE.search(str(step.get("run", "")))
            for step in job.get("steps") or []
        )
    )


# The inline `sdlc ci` job: a job with nothing on it but these keys, and steps
# that are only the checkout and the `sdlc ci` step. Anything more -- another
# step, a service, an env, a matrix -- is work ci.yml would drop.
COVERED_SDLC_KEYS = {"name", "runs-on", "steps", "permissions", "timeout-minutes"}
COVERED_WORKFLOW_KEYS = {"name", "run-name", True, "on", "permissions", "concurrency", "jobs"}
CHECKOUT = re.compile(r"^actions/checkout@")
FLOATING_ENV = {"SDLC_IMAGE": "${{ vars.SDLC_IMAGE }}"}
READ_ONLY = {"contents": "read"}


def is_checkout(step: dict) -> bool:
    """The plain checkout: it keeps no credentials the later steps could use, or its depth."""
    return (
        CHECKOUT.match(str(step.get("uses", ""))) is not None
        and set(step) <= {"name", "uses", "with"}
        and keys_within(step.get("with"), {"persist-credentials", "fetch-depth"})
    )


def is_covered_step(step: object) -> bool:
    if not isinstance(step, dict):
        return False
    if "uses" in step:
        return is_checkout(step)
    run = str(step.get("run", "")) + "\n"
    rest = PINNED_STEP.sub("", run, count=1).replace("set -eu\n", "", 1).strip()
    pinned = PINNED_STEP.search(run) is not None and not rest
    floating = run.strip() == FLOATING_CI_LINE
    # examples/ci.yml's step carries one env entry; any other would be dropped.
    env_kept = (step.get("env") or {}) in ({}, FLOATING_ENV)
    return set(step) <= {"name", "run", "env"} and env_kept and (pinned or floating)


def is_covered_sdlc_job(job: object) -> bool:
    return (
        is_sdlc_job(job)
        and set(job) <= COVERED_SDLC_KEYS
        and job.get("permissions", READ_ONLY) == READ_ONLY
        and all(is_covered_step(step) for step in job.get("steps") or [])
    )


# A job that only calls a reusable workflow, whose repo and file name a row of
# the table. A name not listed is refused: nothing here guesses what it did.
REUSABLE = {
    # validate.yml: check's constitution + zizmor; container-release.yml: image,
    # promote; dependency-audit.yml: the weekly audit; dependabot-automerge.yml:
    # Renovate's automerge replaces it
    "constitution": {
        "validate.yml",
        "container-release.yml",
        "dependency-audit.yml",
        "dependabot-automerge.yml",
    },
    # gourmand.yml: check's gourmand; review.yml: ai's review; triage.yml: gate's
    # triage; retriage.yml: gate on a collaborator's review comment
    "gatehouse": {"gourmand.yml", "review.yml", "triage.yml", "retriage.yml"},
    # router.yml and the five before it: ai and write's review, fix, triage, code;
    # release.yml: write's release PR; release-tag.yml: write's tag
    "ashigaru": {
        "router.yml",
        "triage.yml",
        "code.yml",
        "fix.yml",
        "review.yml",
        "release.yml",
        "release-tag.yml",
    },
}
CALL = re.compile(
    r"^crunchtools/(?P<repo>[\w.-]+)/\.github/workflows/(?P<file>[\w.-]+\.ya?ml)@\S+$"
)
LOCAL_CALL = re.compile(r"^\./\.github/workflows/(?P<file>[\w.-]+\.ya?ml)$")
CALL_KEYS = {"name", "uses", "permissions", "secrets", "needs", "if", "concurrency"}
IMAGE_CALL = ("constitution", "container-release.yml")
# container-release's inputs that are not config: the promote job signs always,
# and a child polls for its parent's digest instead of being dispatched.
IMAGE_DROPPED = {"sign", "downstream"}


def call_of(job: dict, local: set[str]) -> tuple[str, str] | None:
    """(repo, file) of the allowlisted workflow a job calls; repo is "" for this repo's own."""
    uses = job.get("uses")
    if not isinstance(uses, str):
        return None
    if (found := LOCAL_CALL.match(uses)) is not None:
        # Its own callee is judged as a workflow of its own: refused, it refuses the repo.
        return ("", found["file"]) if found["file"] in local else None
    if (found := CALL.match(uses)) is None:
        return None
    repo = "sdlc" if found["repo"] == "python-sdlc" else found["repo"]  # the old name
    return (repo, found["file"]) if found["file"] in REUSABLE.get(repo, ()) else None


# The `if:` conditions the canonical callers put on a call (constitution's
# gatehouse.yml, gatehouse-retriage.yml and pre-router ashigaru.yml), whitespace
# folded: each only picks the event that `sdlc event` routes to the same row.
# Any other condition narrows the call in a way the fleet file would drop -- a
# release branch, a path, a label -- so the workflow is refused, not deleted.
# `needs` and `concurrency` only order and serialize jobs that are all covered.
CALL_CONDITIONS = {
    "always()",
    "github.event_name == 'pull_request_target'",
    "github.event.workflow_run.event == 'pull_request_review_comment'",
    "github.event_name == 'issues' && (github.event.action == 'opened' "
    "|| github.event.label.name == 'ready-for-triage')",
    "always() && github.event_name == 'issues' && (needs.triage.outputs.auto_code == 'true' "
    "|| (github.event.action == 'labeled' && github.event.label.name == 'ready-to-code'))",
    "github.event_name == 'workflow_run' && github.event.workflow_run.event == "
    "'pull_request_target' && github.event.workflow_run.head_repository.full_name == "
    "github.repository && startsWith(github.event.workflow_run.head_branch, 'ashigaru/')",
}


def is_covered_call(job: dict, local: set[str]) -> bool:
    call = call_of(job, local)
    known_if = "if" not in job or " ".join(str(job["if"]).split()) in CALL_CONDITIONS
    # `with:` is config, and only container-release's has a home to move to.
    keys = CALL_KEYS | ({"with"} if call == IMAGE_CALL else set())
    return call is not None and known_if and set(job) <= keys


def image_inputs(where: str, job: dict) -> dict:
    """A container-release call's `with:` as config: all but what is dropped."""
    given = job.get("with") or {}
    if not isinstance(given, dict):
        raise RefusedError(f"{where}: `with:` is not a mapping")
    out = {}
    for key, value in given.items():
        if key in IMAGE_DROPPED:
            continue
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise RefusedError(f"{where}: input {key!r} is not a name config can hold")
        if not isinstance(value, (str, int, float, bool)):
            raise RefusedError(f"{where}: input {key} has no plain value to write as config")
        if isinstance(value, str) and "${{" in value:
            raise RefusedError(f"{where}: input {key} is an expression; it cannot be config")
        out[key] = value
    return out


# examples/release.yml and release-open.yml, as copied into a repo: the default
# branch was substituted at `refs/heads/main` and `--base main`, so a job is
# the example's when it is the same once the example is given the file's own.
BRANCH = re.compile(r"'refs/heads/([\w./-]+)'")


@functools.cache
def release_jobs(branch: str) -> tuple[dict, ...]:
    jobs: list[dict] = []
    for name in ("release.yml", "release-open.yml"):
        text = (EXAMPLES / name).read_text()
        text = text.replace("refs/heads/main", f"refs/heads/{branch}")
        text = text.replace("--base main", f"--base {branch}")
        jobs += jobs_of(yaml.safe_load(text)).values()
    return tuple(jobs)


# An inline CodeQL job: the checkout and the three steps of github/codeql-action.
CODEQL = re.compile(r"^github/codeql-action/(?:init|autobuild|analyze)@\S+$")
CODEQL_KEYS = {"name", "runs-on", "permissions", "steps", "timeout-minutes", "strategy"}


def is_codeql_job(job: dict) -> bool:
    steps = job.get("steps")
    strategy = job.get("strategy")
    if not isinstance(steps, list) or not set(job) <= CODEQL_KEYS:
        return False
    if strategy is not None and not (
        isinstance(strategy, dict) and set(strategy) <= {"fail-fast", "matrix"}
    ):
        return False
    analyzes = False
    for step in steps:
        if not isinstance(step, dict) or not set(step) <= {"name", "id", "uses", "with"}:
            return False
        uses = str(step.get("uses", ""))
        if CODEQL.match(uses):
            analyzes = analyzes or uses.startswith("github/codeql-action/analyze@")
        elif not is_checkout(step):
            return False
    return analyzes


# An inline PyPI publish: the build, the artifact handed between jobs, the
# upload. Each `run:` line is one of these and nothing else; a token or an
# `env:` would be a credential the OIDC publish in examples/ci.yml replaces.
PYPI_USES = re.compile(
    r"^(?:astral-sh/setup-uv|actions/setup-python|actions/upload-artifact"
    r"|actions/download-artifact)@\S+$"
)
PYPI_ACTION = "pypa/gh-action-pypi-publish@"
PYPI_ACTION_WITH = {"packages-dir", "skip-existing", "print-hash", "attestations", "verbose"}
PYPI_KEYS = {
    "name",
    "runs-on",
    "permissions",
    "steps",
    "timeout-minutes",
    "environment",
    "needs",
    "if",
}
UV_BUILD = re.compile(r"^uv build(?: --(?:sdist|wheel|no-sources))*(?: --out-dir [\w./-]+)?$")
PY_BUILD = re.compile(r"^python3? -m build(?: --(?:sdist|wheel))*$")
UV_PUBLISH = re.compile(r"^uv publish(?: --trusted-publishing (?:always|automatic))?(?: dist/\*)?$")
PIP_BUILD = re.compile(r"^(?:python3? -m )?pip install(?: --upgrade| -U)*(?: pip)? build$")
UV_PYTHON = re.compile(r"^uv python install(?: \d+(?:\.\d+){0,2})?$")
SET_E = re.compile(r"^set -[eu]+(?: -o pipefail)?$")


def pypi_run(step: dict) -> str | None:
    """A `run:` step of a publish workflow: "publish", "build", "", None if a line is other."""
    if "uses" in step or "with" in step:
        return None
    kinds = set()
    for line in run_lines(step):
        if UV_PUBLISH.match(line):
            kinds.add("publish")
        elif UV_BUILD.match(line) or PY_BUILD.match(line):
            kinds.add("build")
        elif not (PIP_BUILD.match(line) or UV_PYTHON.match(line) or SET_E.match(line)):
            return None
    return "publish" if "publish" in kinds else "build" if kinds else ""


def pypi_step(step: object) -> str | None:
    """ "publish", "build", "" for a step that is neither but allowed, None for any other."""
    if not isinstance(step, dict) or not set(step) <= {"name", "id", "uses", "with", "run", "if"}:
        return None
    if "run" in step:
        return pypi_run(step)
    uses = str(step.get("uses", ""))
    if uses.startswith(PYPI_ACTION):
        return "publish" if keys_within(step.get("with"), PYPI_ACTION_WITH) else None
    if CHECKOUT.match(uses):
        return "" if is_checkout(step) else None
    return "" if PYPI_USES.match(uses) else None


def pypi_job_kind(job: dict) -> str | None:
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps or not set(job) <= PYPI_KEYS:
        return None
    kinds = [pypi_step(step) for step in steps]
    if None in kinds:
        return None
    return "publish" if "publish" in kinds else "build" if "build" in kinds else None


# An inline Dependabot automerge: its `if:` is about the bot, and its steps are
# fetch-metadata and `gh pr merge` or `gh pr review --approve`, with the token.
GH_PR = re.compile(
    r"""^gh pr (?:merge|review --approve)(?: (?:--[\w-]+|"?\$[A-Za-z_]\w*"?"""
    r"""|"?\$\{[A-Za-z_]\w*\}"?|"?\$\{\{ ?[\w.]+ ?\}\}"?))*$"""
)
AUTOMERGE_ENV = re.compile(
    r"^\$\{\{ ?(?:github\.token|secrets\.GITHUB_TOKEN"
    r"|github\.event\.pull_request\.(?:html_url|number)) ?\}\}$"
)
AUTOMERGE_KEYS = {"name", "runs-on", "permissions", "steps", "timeout-minutes", "if", "needs"}


def is_automerge_job(job: dict) -> bool:
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps or not set(job) <= AUTOMERGE_KEYS:
        return False
    if "dependabot[bot]" not in str(job.get("if", "")):
        return False
    for step in steps:
        if not isinstance(step, dict) or not set(step) <= {
            "name",
            "id",
            "uses",
            "with",
            "run",
            "if",
            "env",
        }:
            return False
        env = step.get("env") or {}
        if not isinstance(env, dict) or not all(
            isinstance(v, str) and AUTOMERGE_ENV.match(v) for v in env.values()
        ):
            return False
        if "uses" in step:
            ok = str(step["uses"]).startswith("dependabot/fetch-metadata@")
            ok = ok and "run" not in step and keys_within(step.get("with"), {"github-token"})
        else:
            lines = run_lines(step)
            ok = bool(lines) and all(GH_PR.match(line) for line in lines) and "with" not in step
        if not ok:
            return False
    return True


def job_kind(job: object, branch: str, local: set[str]) -> str | None:
    """Which row of the table a job is, or None: sdlc, call, release, codeql, automerge, ..."""
    if not isinstance(job, dict):
        return None
    for kind, found in (
        ("sdlc", is_covered_sdlc_job(job)),
        ("call", is_covered_call(job, local)),
        ("release", job in release_jobs(branch)),
        ("codeql", is_codeql_job(job)),
        ("automerge", is_automerge_job(job)),
    ):
        if found:
            return kind
    return pypi_job_kind(job)  # publish, build, or None


class Judged:
    """What one workflow file is: the jobs no row covers, and the config its jobs held."""

    def __init__(self) -> None:
        self.uncovered: list[str] = []
        self.publish = False
        self.images: list[tuple[str, dict]] = []  # (job id, container-release's inputs)
        self.dropped: list[str] = []  # inputs of a container-release call, not moved


def judge(where: str, parsed: dict, text: str, local: set[str]) -> Judged:
    out = Judged()
    extra = sorted(str(k) for k in set(parsed) - COVERED_WORKFLOW_KEYS)
    if extra:
        out.uncovered.append(f"(workflow-level {', '.join(extra)})")
    jobs = jobs_of(parsed)
    branch = (BRANCH.search(text) or ["", "main"])[1]
    kinds = {str(jid): job_kind(job, branch, local) for jid, job in jobs.items()}
    # A build alone is the publish workflow's first half, and only there.
    out.publish = "publish" in kinds.values()
    out.uncovered += [
        jid for jid, kind in kinds.items() if kind is None or (kind == "build" and not out.publish)
    ]
    if not jobs:
        out.uncovered.append("(no jobs)")
    if not out.uncovered:
        for jid, job in jobs.items():
            if call_of(job, local) == IMAGE_CALL:
                out.images.append((str(jid), image_inputs(f"{where} job {jid}", job)))
                out.dropped += sorted(IMAGE_DROPPED & set(job.get("with") or {}))
    return out


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


# Two fields the fleet's copies differ in, both Gatehouse's own: the guard on
# ubuntu-latest (before the private-repo runner), and triage handed review's
# result. Either value of each is the template; nothing else is.
TRIAGE_REVIEW_RESULT = "${{ needs.review.result }}"


def known_variants(doc: dict, canonical: dict) -> dict:
    """`doc` with the guard's runner and triage's review_result as the template has them."""
    jobs = doc.get("jobs")
    want = canonical.get("jobs") or {}
    if not isinstance(jobs, dict):
        return doc
    jobs = dict(jobs)
    guard, guard_want = jobs.get("guard"), want.get("guard") or {}
    if isinstance(guard, dict) and guard.get("runs-on") == "ubuntu-latest":
        jobs["guard"] = {**guard, "runs-on": guard_want.get("runs-on")}
    triage = jobs.get("triage")
    passed = triage.get("with") if isinstance(triage, dict) else None
    if isinstance(passed, dict) and passed.get("review_result") == TRIAGE_REVIEW_RESULT:
        rest = {k: v for k, v in passed.items() if k != "review_result"}
        jobs["triage"] = {k: v for k, v in triage.items() if k != "with"} | (
            {"with": rest} if rest else {}
        )
    return {**doc, "jobs": jobs}


def same_gatehouse(current: dict, canonical: dict) -> bool:
    """`current` is examples/gatehouse.yml in all but comments and the known variants,
    or its guard is the one before.

    gatehouse#150 changed the guard's script (it now refuses a fork's change to
    .sdlc/ too). A copy whose guard script is exactly the one before that is
    brought up to date; any other difference in any step, the guard's
    included, keeps the file as it is.
    """
    current = known_variants(current, canonical)
    if current == canonical:
        return True
    jobs = current.get("jobs")
    if not isinstance(jobs, dict):
        return False
    guard = jobs.get("guard")
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
    jobs = {**jobs, "guard": {**guard, "steps": updated}}
    return {**current, "jobs": jobs} == canonical


def convert_workflows(repo: Path, planned: dict[Path, str | None], notes: list[str]) -> Judged:
    """Plan ci.yml in, every other workflow out, or refuse; what they held, as config to move."""
    wf_dir = repo / ".github" / "workflows"
    files = sorted(wf_dir.glob("*.y*ml")) if wf_dir.is_dir() else []
    local = {path.name for path in files}
    template = CI_TEMPLATE.read_text()
    canonical = parse(GATEHOUSE_TEMPLATE.name, GATEHOUSE_TEMPLATE.read_text())
    ci = wf_dir / "ci.yml"
    refused: list[tuple[Path, list[str]]] = []
    pages = False
    held = Judged()
    for path in files:
        text = path.read_text()
        if path == ci and text == template:
            continue
        parsed = parse(rel(repo, path), text)
        if path.name in ("gatehouse.yml", "gatehouse.yaml") and same_gatehouse(parsed, canonical):
            continue
        judged = judge(rel(repo, path), parsed, text, local)
        if judged.uncovered:
            refused.append((path, judged.uncovered))
            pages = pages or re.search(r"actions/deploy-pages\b", text) is not None
            continue
        held.publish = held.publish or judged.publish
        held.images += judged.images
        held.dropped += judged.dropped
    if refused:
        named = "; ".join(f"{rel(repo, p)} (jobs: {', '.join(ids)})" for p, ids in refused)
        why = (
            f"workflows with jobs `sdlc event` does not run: {named}. Move what they do to "
            f"[tool.sdlc.commands] (run as `sdlc run <name>`) by hand, then convert"
        )
        if pages:
            why += "; the Pages source (actions/deploy-pages) is then a human step in the settings"
        raise RefusedError(why)
    planned.update(dict.fromkeys(files))
    planned[ci] = template
    notes += [
        f"deleted: {rel(repo, p)} (every job is a row of `sdlc event`)" for p in files if p != ci
    ]
    if held.dropped:
        notes.append(
            f"dropped: {', '.join(sorted(set(held.dropped)))} of the container-release call "
            f"(promote always signs; children poll, nothing dispatches them)"
        )
    if len({json.dumps(inputs, sort_keys=True) for _, inputs in held.images}) > 1:
        raise RefusedError(
            "more than one container-release call, with different inputs; "
            "one [tool.sdlc.image] cannot hold both"
        )
    return held


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


def toml_value(value: object) -> str:
    """A TOML value for a string, bool, number or list of them."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(value, ensure_ascii=False)


def with_keys(text: str, table: str, items: Sequence[tuple[str, object]], note: str) -> str:
    """`text` with each `key = value` set in `[table]`, written by line, not by a TOML writer."""
    lines = note + "".join(f"{key} = {toml_value(value)}\n" for key, value in items)
    header = re.search(rf"^\[{re.escape(table)}\][ \t]*(#.*)?$\n?", text, re.M)
    if header is None:
        return text.rstrip("\n") + ("\n\n" if text.strip() else "") + f"[{table}]\n{lines}"
    return text[: header.end()] + lines + text[header.end() :]


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


MOVED_NOTE = "# Written by crunchtools/sdlc's scripts/convert_repo.py: moved from the workflows.\n"


def config_file(repo: Path) -> Path:
    pyproject = repo / "pyproject.toml"
    return pyproject if pyproject.is_file() else repo / "sdlc.toml"


def refuse_clashes(where: str, cfg: dict, held: Judged, image: dict) -> dict:
    """The [tool.sdlc.image] already there, or a refusal when a value differs from the call's."""
    have = cfg.get("image", {})
    if not isinstance(have, dict):
        raise RefusedError(f"{where}: [tool.sdlc] image is not a table")
    if held.publish and cfg.get("publish", "pypi") != "pypi":
        raise RefusedError(f"{where}: [tool.sdlc] publish is {cfg['publish']!r}, not 'pypi'")
    clash = sorted(k for k, v in image.items() if k in have and have[k] != v)
    if clash:
        raise RefusedError(
            f"{where}: [tool.sdlc.image] {', '.join(clash)} differ from the container-release call"
        )
    return have


def move_config(
    repo: Path, planned: dict[Path, str | None], notes: list[str], held: Judged
) -> None:
    """What the deleted workflows said per repo, as [tool.sdlc]: publish, and the image's inputs.

    The spec says only that it "moves into [tool.sdlc]": `publish = "pypi"` for a
    deleted PyPI publish, and a container-release call's inputs (but `sign` and
    `downstream`) as `[tool.sdlc.image]`. A key already there with another value
    is refused, never overwritten; the same value is nothing to do.
    """
    image = held.images[0][1] if held.images else {}
    if not (held.publish or image):
        return
    path = config_file(repo)
    text = planned.get(path) or (path.read_text() if path.is_file() else "")
    where = rel(repo, path)
    try:
        cfg = tomllib.loads(text).get("tool", {}).get("sdlc", {})
    except tomllib.TOMLDecodeError as exc:
        raise RefusedError(f"{where} is not TOML: {exc}") from exc
    have = refuse_clashes(where, cfg, held, image)
    publish = [("publish", "pypi")] if held.publish and "publish" not in cfg else []
    new = [(k, v) for k, v in image.items() if k not in have]
    if publish:
        text = with_keys(text, "tool.sdlc", publish, MOVED_NOTE)
        notes.append(f'publish = "pypi" in {where} (the PyPI publish job went)')
    if new:
        text = with_keys(text, "tool.sdlc.image", new, MOVED_NOTE)
        notes.append(f"[tool.sdlc.image] in {where}: {', '.join(k for k, _ in new)}")
    if not (publish or new):
        return
    try:
        after = tomllib.loads(text).get("tool", {}).get("sdlc", {})
    except tomllib.TOMLDecodeError as exc:
        raise RefusedError(f"{where} would not be TOML: {exc}") from exc
    back = after.get("image") or {}
    if after.get("publish") != ("pypi" if held.publish else cfg.get("publish")) or any(
        back.get(k) != v for k, v in image.items()
    ):
        raise RefusedError(f"{where}: the keys written did not read back as meant")
    planned[path] = text


def convert_config(
    repo: Path,
    planned: dict[Path, str | None],
    notes: list[str],
    unbuilt_stacks: tuple[str, ...] = (),
) -> None:
    path = config_file(repo)
    text = planned.get(path) or (path.read_text() if path.is_file() else "")
    try:
        cfg = tomllib.loads(text).get("tool", {}).get("sdlc", {})
    except tomllib.TOMLDecodeError as exc:
        raise RefusedError(f"{rel(repo, path)} is not TOML: {exc}") from exc
    if (repo / "pyproject.toml").is_file() and "stacks" not in cfg and "skip-stacks" not in cfg:
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
        text = with_keys(text, "tool.sdlc", [("skip-stacks", unbuilt)], SKIP_STACKS_NOTE)
        notes.append(f"skip-stacks: {', '.join(unbuilt)} in {rel(repo, path)} (not checked yet)")
    text = drop_checked(text, kept, checked, rel(repo, path), notes)
    if secrets:
        if "skip" in ci:
            raise RefusedError(
                f"{rel(repo, path)} [tool.sdlc.ci] skip exists; the Containerfile needs build "
                f"secrets, so add {', '.join(chain)} to it by hand"
            )
        text = with_keys(text, "tool.sdlc.ci", [("skip", chain)], SKIP_BUILD_NOTE)
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
    flows = sorted(wf_dir.glob("*.y*ml")) if wf_dir.is_dir() else []
    refuse_links(
        repo, [hooks, *planned, wf_dir, *flows, repo / "pyproject.toml", repo / "sdlc.toml"]
    )
    planned[hooks] = convert_hooks(hooks.read_text() if hooks.exists() else "")
    move_config(repo, planned, notes, convert_workflows(repo, planned, notes))
    convert_config(repo, planned, notes, unbuilt_stacks)
    notes.append(f"writes: {BOOTSTRAP_AT}, executable (crunchtools/sdlc's bin/sdlc-bootstrap)")
    # The old name in anything that survives, and in the repo's config.
    candidates = [hooks, repo / "pyproject.toml", repo / "sdlc.toml"]
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
    wiring itself -- the floating hooks, both hook types, ci.yml as the example,
    no other workflow -- rather than to how the text was produced.
    """
    parse_back(repo, planned)
    verify_hooks(planned[repo / ".pre-commit-config.yaml"] or "")
    if planned[repo / BOOTSTRAP_AT] != BOOTSTRAP.read_text():
        raise RefusedError(f"{BOOTSTRAP_AT} would not be bin/sdlc-bootstrap")
    wf_dir = repo / ".github" / "workflows"
    if planned.get(wf_dir / "ci.yml") != CI_TEMPLATE.read_text():
        raise RefusedError("ci.yml would not be examples/ci.yml")
    # One workflow, and no `uses:` in it: a marketplace action or reusable
    # workflow left anywhere is a version the fleet file exists to have none of.
    flows = sorted(wf_dir.glob("*.y*ml")) if wf_dir.is_dir() else []
    survivors = {
        p for p in [*flows, *planned] if p.parent == wf_dir and planned.get(p, "") is not None
    }
    if survivors != {wf_dir / "ci.yml"}:
        left = ", ".join(sorted(rel(repo, p) for p in survivors - {wf_dir / "ci.yml"}))
        raise RefusedError(f"workflows would survive the conversion: {left}")
    if re.search(r"^[^#\n]*\buses:", CI_TEMPLATE.read_text(), re.M):
        raise RefusedError("ci.yml has a `uses:`")


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
