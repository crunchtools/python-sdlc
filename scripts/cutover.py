#!/usr/bin/env python3
"""The fleet cutover (sdlc#88): what one consumer repo needs beyond convert_repo.py.

scripts/fleet_wave.py --cutover runs convert_repo.py over a checkout and then
this, so each repo gets one pull request that carries every change at once:

  constitution     every `crunchtools/constitution/.github/workflows/*.yml@vX.Y.Z`
                   pin moves to the target ref, with `Inherits:` in
                   .specify/memory/constitution.md and the constitution hook
                   `rev:` -- they move together, as fleet-bump.py moves them.
                   A major-tag call (`@v1`) already floats and is left alone
  Dependabot       .github/dependabot.yml and its auto-merge caller go, by the
                   target constitution's own fleet-bump.py: from v1.60.0 the
                   validator fails a repo that still has them
  the old name     a bare `python-sdlc` left in a workflow, the hooks file or
                   the sdlc config (a `downstream:` input, a dispatch loop)
                   becomes `sdlc`. convert_repo.py renames the image and the
                   reusable calls; this catches what it does not
  signing          every container-release.yml call gets `sign: true` and the
                   two cosign secrets, by name: from v1.60.0 a call without
                   them fails validation (IX). A workflow that pushes an image
                   itself is named for a human
  schedule         a weekly rebuild, for the repos the caller names (sdlc#88
                   lists the image repos that have neither one nor a parent
                   that dispatches them)
  release          examples/release.yml, as .github/workflows/release.yml, in a
                   repo that publishes an image and has no release flow: the
                   Ashigaru app tags v*.*.*, the only actor the cosign release-tag
                   ruleset lets through. A repo whose ashigaru.yml calls the
                   router, whose release job tags, gets examples/release-open.yml
                   instead -- the dispatch that opens the release pull request --
                   and any other tag job of its own is dropped, so each repo
                   ends with exactly one tag path or is named for a human
  Ashigaru caller  a pre-router .github/workflows/ashigaru.yml (triage, code
                   and fix called one by one, which Dependabot bumps one job
                   at a time) becomes crunchtools/ashigaru's examples/ashigaru.yml
                   at the target tag, which calls router.yml and brings the
                   release-tag job with it. A repo with no ashigaru.yml is not
                   enrolled here

What it will not guess at -- an existing release flow of another shape, a
call it cannot edit and read back -- goes to the `hand` list, and the rest of
the repo is still converted. Deterministic and offline: the same checkout and
target give the same result, and a second run finds nothing to do. Edits are
made by line, never by a YAML writer, and each workflow it edits is read back
to confirm the change and nothing else.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

try:
    import yaml
except ImportError:  # fleet_wave.py's main() says so, before any repo
    HAS_YAML = False
else:
    HAS_YAML = True

ROOT = Path(__file__).resolve().parent.parent
RELEASE_TEMPLATE = ROOT / "examples" / "release.yml"
OPEN_TEMPLATE = ROOT / "examples" / "release-open.yml"  # for a repo whose router tags

MANIFEST = Path(".specify/memory/constitution.md")
HOOKS = Path(".pre-commit-config.yaml")
WORKFLOWS = Path(".github/workflows")
RELEASE = WORKFLOWS / "release.yml"
ASHIGARU_CALLER = WORKFLOWS / "ashigaru.yml"
ASHIGARU_REPO = "ashigaru"
ASHIGARU_TAG = "v5"  # the major tag router.yml and release-tag.yml are called at
CONFIGS = (Path("pyproject.toml"), Path("sdlc.toml"))

# fleet-bump.py's own three patterns: the pins that move with a constitution release.
CONSTITUTION_PIN = re.compile(
    r"(crunchtools/constitution/\.github/workflows/[\w-]+\.ya?ml@)v\d+\.\d+\.\d+(?![\w-]|\.[\w-])"
)
# Any reusable-workflow call into the constitution, at whatever ref: what is left after the move.
CONSTITUTION_CALL = re.compile(r"crunchtools/constitution/\.github/workflows/[\w.-]+@([\w./-]+)")
INHERITS = re.compile(r"^(>\s*\*\*Inherits:\*\*.*?)v\d+\.\d+\.\d+", re.M)
HOOK_REV = re.compile(
    r"(repo:\s*https://github\.com/crunchtools/constitution/?\s*\n\s*rev:\s*)v\d+\.\d+\.\d+"
)
# The repo's old name as a word: not python-sdlc-extras, not a domain, but a
# sentence's last word is still one.
OLD_NAME = re.compile(r"(?<![\w.-])python-sdlc(?![\w-]|\.[\w-])")
RELEASE_CALL = re.compile(r"^crunchtools/constitution/\.github/workflows/container-release\.yml@")
ASHIGARU_TAGS = re.compile(r"^crunchtools/ashigaru/\.github/workflows/release(?:-tag)?\.yml@")
ROUTER_CALL = re.compile(r"^crunchtools/ashigaru/\.github/workflows/router\.yml@")
COSIGN_SECRETS = ("COSIGN_PRIVATE_KEY", "COSIGN_PASSWORD")
SAFE_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
# A step that publishes an image itself, outside container-release.yml.
SELF_PUBLISH = re.compile(
    r"\b(?:podman|buildah|docker)\s+push\b|\bskopeo\s+copy\b|docker/build-push-action@"
)
SIGN_NOTE = (
    "# Every published image is signed (sdlc#88): promote signs the gated\n"
    "# digest before any tag moves, with the key from this repo's\n"
    "# cosign-signing environment (default branch and v* only).\n"
)
SCHEDULE_NOTE = "# weekly: base-image errata, gated like any build"


@dataclass(frozen=True)
class Target:
    """What the fleet moves to: the release each repo is judged by, and its reach."""

    constitution: str  # X.Y.Z, for Inherits and the hook rev
    constitution_ref: str  # what the workflow pins become; vX.Y.Z unless told otherwise
    bump: ModuleType | None = None  # the target constitution's scripts/fleet-bump.py
    validator: ModuleType | None = None  # and its validate-constitution.py
    weekly: frozenset[str] = frozenset()  # repos that get a weekly rebuild
    ashigaru_caller: str = ""  # examples/ashigaru.yml at ASHIGARU_TAG; "" leaves callers alone
    ashigaru_source: str = ""  # where it was read: crunchtools/ashigaru@<sha>, for the report


@dataclass
class Result:
    """What cutover() did to one checkout, for fleet_wave.py's report and PR body.

    `changed` holds one line per edit it made and wrote. `hand` holds what it
    would not do safely -- each line names the file and what a human must do --
    and a repo with any is listed as hand work; the rest of its edits still ship.
    """

    changed: list[str] = field(default_factory=list)  # one line per change, for the report
    hand: list[str] = field(default_factory=list)  # what it would not guess at
    pins_left: list[str] = field(default_factory=list)  # constitution calls not at the target ref


def load(path: Path, name: str) -> ModuleType:
    """A script by path: fleet-bump.py and validate-constitution.py have hyphens."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise OSError(f"{path}: not a loadable Python file")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def constitution_modules(checkout: Path) -> tuple[ModuleType, ModuleType]:
    """(fleet-bump, validator) from a constitution checkout at the target release."""
    sys.path.insert(0, str(checkout / "scripts"))  # fleet-bump imports fleet_common
    return (
        load(checkout / "scripts" / "fleet-bump.py", "fleet_bump"),
        load(checkout / "validate-constitution.py", "validate_constitution"),
    )


# ------------------------------------------------------------------ by line


def block_end(lines: list[str], start: int, indent: int) -> int:
    """The index after the block opened at `start`: the next line at `indent` or less."""
    for i in range(start + 1, len(lines)):
        line = lines[i]
        content = line.strip() and not line.lstrip().startswith("#")
        if content and len(line) - len(line.lstrip()) <= indent:
            return i
    return len(lines)


def last_content(lines: list[str], start: int, end: int) -> int:
    """The index after the last line of content in lines[start:end].

    Comments trailing a block introduce whatever follows it, so they are not its end.
    """
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip()[0] == "#"):
        end -= 1
    return end


def find_key(lines: list[str], key: str, indent: int, start: int, end: int) -> int | None:
    pattern = re.compile(rf"^ {{{indent}}}{re.escape(key)}:(?:\s|$)")
    return next((i for i in range(start, end) if pattern.match(lines[i])), None)


def job_span(lines: list[str], job_id: str) -> tuple[int, int, int] | None:
    """(start, end, indent) of a job's block under the top-level `jobs:`."""
    top = find_key(lines, "jobs", 0, 0, len(lines))
    if top is None:
        return None
    end = block_end(lines, top, 0)
    child = next(
        (
            len(lines[i]) - len(lines[i].lstrip())
            for i in range(top + 1, end)
            if lines[i].strip() and not lines[i].lstrip().startswith("#")
        ),
        None,
    )
    if child is None:
        return None
    start = find_key(lines, job_id, child, top + 1, end)
    if start is None:
        return None
    return start, block_end(lines, start, child), child


# --------------------------------------------------------------- the pins


def move_constitution(repo: Path, target: Target, result: Result) -> None:
    """Inherits, the reusable workflow pins and the hook rev, together."""
    paths = [repo / MANIFEST, repo / HOOKS, *sorted((repo / WORKFLOWS).glob("*.y*ml"))]
    for path in paths:
        if not path.is_file():
            continue
        text = path.read_text()
        new = CONSTITUTION_PIN.sub(rf"\g<1>{target.constitution_ref}", text)
        if path.name == MANIFEST.name:
            new = INHERITS.sub(rf"\g<1>v{target.constitution}", new)
        if path.name == HOOKS.name:
            new = HOOK_REV.sub(rf"\g<1>v{target.constitution}", new)
        if new != text:
            path.write_text(new)
            result.changed.append(f"constitution: {target.constitution_ref} in {rel(repo, path)}")


def pins_left(repo: Path, target: Target) -> list[str]:
    """Each constitution workflow call at neither the target ref nor a major tag."""
    left = []
    for path in sorted((repo / WORKFLOWS).glob("*.y*ml")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            for ref in CONSTITUTION_CALL.findall(line.split("#", 1)[0]):
                if ref != target.constitution_ref and not re.fullmatch(r"v\d+", ref):
                    left.append(f"{rel(repo, path)}:{n} @{ref}")
    return left


def drop_dependabot(repo: Path, target: Target, result: Result) -> None:
    """The target constitution's own Renovate teardown, so the two never disagree."""
    if target.bump is None or target.validator is None:
        return
    try:
        changes = target.bump.drop_dependabot(repo, target.validator)
    except RuntimeError as exc:
        result.hand.append(f"Dependabot: {exc}")
        return
    result.changed += [f"Dependabot: {c}" for c in changes]


def rename(repo: Path, result: Result) -> None:
    paths = [repo / HOOKS, *(repo / c for c in CONFIGS)]
    paths += sorted((repo / WORKFLOWS).glob("*.y*ml"))
    for path in paths:
        if not path.is_file():
            continue
        text = path.read_text()
        new = OLD_NAME.sub("sdlc", text)
        if new != text:
            path.write_text(new)
            result.changed.append(f"renamed: python-sdlc to sdlc in {rel(repo, path)}")


# ----------------------------------------------------------------- signing


def release_calls(parsed: dict) -> dict[str, dict]:
    jobs = parsed.get("jobs") if isinstance(parsed, dict) else None
    return {
        job_id: job
        for job_id, job in (jobs or {}).items()
        if isinstance(job, dict) and RELEASE_CALL.match(str(job.get("uses", "")))
    }


def unsigned(job: dict) -> bool:
    secrets = job.get("secrets")
    passed = secrets == "inherit" or (
        isinstance(secrets, dict) and all(s in secrets for s in COSIGN_SECRETS)
    )
    return (job.get("with") or {}).get("sign") is not True or not passed


def signed_job(lines: list[str], job_id: str) -> list[str]:
    """The workflow's lines with job `job_id` signing: `sign: true` and the secrets."""
    span = job_span(lines, job_id)
    if span is None:
        raise ValueError(f"no job `{job_id}` under `jobs:`")
    start, end, indent = span
    if lines[start].split(":", 1)[1].split("#", 1)[0].strip():
        raise ValueError(f"job `{job_id}` is not a block mapping")
    key = indent + 2
    lines = list(lines)
    # Secrets first: they sit below `with:`, so its indices stay put.
    at = find_key(lines, "secrets", key, start + 1, end)
    if at is None:
        end = last_content(lines, start, end)
        lines[end:end] = [f"{' ' * key}secrets:\n"] + [
            f"{' ' * (key + 2)}{s}: ${{{{ secrets.{s} }}}}\n" for s in COSIGN_SECRETS
        ]
    elif not lines[at].split(":", 1)[1].strip().startswith("inherit"):
        stop = last_content(lines, at, block_end(lines, at, key))
        have = "".join(lines[at:stop])
        lines[stop:stop] = [
            f"{' ' * (key + 2)}{s}: ${{{{ secrets.{s} }}}}\n"
            for s in COSIGN_SECRETS
            if not re.search(rf"^\s*{s}:", have, re.M)
        ]
    span = job_span(lines, job_id)
    if span is None:  # the secrets edit above keeps the job; this is for mypy
        raise ValueError(f"no job `{job_id}` under `jobs:` after adding its secrets")
    start, end, indent = span
    at = find_key(lines, "with", key, start + 1, end)
    note = "".join(f"{' ' * (key + 2)}{line}\n" for line in SIGN_NOTE.splitlines())
    if at is None:
        uses = find_key(lines, "uses", key, start + 1, end)
        if uses is None:
            raise ValueError(f"job `{job_id}` has no `uses:` line at its keys' indent")
        lines[uses + 1 : uses + 1] = [f"{' ' * key}with:\n", note, f"{' ' * (key + 2)}sign: true\n"]
        return lines
    stop = block_end(lines, at, key)
    sign = find_key(lines, "sign", key + 2, at + 1, stop)
    if sign is not None:
        lines[sign] = re.sub(r"(sign:\s*)[^#\n]*?(\s*(?:#.*)?)$", r"\g<1>true\g<2>", lines[sign])
    else:
        stop = last_content(lines, at, stop)
        lines[stop:stop] = [note, f"{' ' * (key + 2)}sign: true\n"]
    return lines


def sign(repo: Path, result: Result) -> None:
    for path in sorted((repo / WORKFLOWS).glob("*.y*ml")):
        text = path.read_text()
        parsed = yaml.safe_load(text) or {}
        todo = [j for j, job in release_calls(parsed).items() if unsigned(job)]
        if not todo:
            continue
        lines = text.splitlines(keepends=True)
        try:
            for job_id in todo:
                lines = signed_job(lines, job_id)
            after = yaml.safe_load("".join(lines))
        except (ValueError, yaml.YAMLError) as exc:
            result.hand.append(f"signing: {rel(repo, path)}: {exc}; add `sign: true` by hand")
            continue
        if not same_but_signing(parsed, after, todo):
            result.hand.append(
                f"signing: {rel(repo, path)} did not read back as only `sign: true` and the "
                f"cosign secrets on {', '.join(todo)}; add them by hand"
            )
            continue
        path.write_text("".join(lines))
        result.changed.append(f"signing: sign: true on {', '.join(todo)} in {rel(repo, path)}")


def same_but_signing(before: dict, after: object, jobs: list[str]) -> bool:
    """`after` is `before` with only the signing keys added to `jobs`."""
    if not isinstance(after, dict):
        return False
    expected = yaml.safe_load(yaml.safe_dump(before))
    for job_id in jobs:
        job = expected["jobs"][job_id]
        job["with"] = {**(job.get("with") or {}), "sign": True}
        if job.get("secrets") != "inherit":
            job["secrets"] = {
                **(job.get("secrets") or {}),
                **{s: f"${{{{ secrets.{s} }}}}" for s in COSIGN_SECRETS},
            }
    return not any(unsigned(j) for j in release_calls(after).values()) and after == expected


def self_published(repo: Path, result: Result) -> None:
    """Name each workflow that publishes outside container-release.yml: signing it is by hand."""
    for path in sorted((repo / WORKFLOWS).glob("*.y*ml")):
        code = "\n".join(
            line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
        )
        if SELF_PUBLISH.search(code):
            result.hand.append(
                f"signing: {rel(repo, path)} publishes outside container-release.yml; "
                f"check it signs in the cosign-signing environment (IX)"
            )


# ---------------------------------------------------------------- schedule


def cron(name: str) -> str:
    """A weekly slot from the repo's name, so the fleet does not rebuild in one minute."""
    digest = int(hashlib.sha256(name.encode()).hexdigest(), 16)
    rest, minute = divmod(digest, 60)
    rest, hour = divmod(rest, 5)  # 02:00 to 06:59 UTC, the fleet's quiet hours
    return f"{minute} {2 + hour} * * {rest % 7}"


def add_schedule(repo: Path, name: str, result: Result) -> None:
    for path in sorted((repo / WORKFLOWS).glob("*.y*ml")):
        text = path.read_text()
        parsed = yaml.safe_load(text) or {}
        on = parsed.get("on", parsed.get(True))
        if not release_calls(parsed) or not isinstance(on, dict) or "schedule" in on:
            continue
        lines = text.splitlines(keepends=True)
        top = next(
            (i for i, x in enumerate(lines) if re.match(r"^(?:on|\"on\"|'on'):\s*$", x)), None
        )
        if top is None:
            result.hand.append(f"schedule: {rel(repo, path)} has no block `on:`; add one by hand")
            continue
        end = block_end(lines, top, 0)
        indent = next(
            len(x) - len(x.lstrip()) for x in lines[top + 1 : end] if x.strip() and x[0] == " "
        )
        at = find_key(lines, "workflow_dispatch", indent, top + 1, end)
        at = last_content(lines, top, end) if at is None else at
        pad = " " * indent
        lines[at:at] = [f"{pad}schedule:\n", f'{pad}  - cron: "{cron(name)}"   {SCHEDULE_NOTE}\n']
        after = yaml.safe_load("".join(lines)) or {}
        if (after.get("on") or after.get(True) or {}).get("schedule") != [{"cron": cron(name)}]:
            result.hand.append(f"schedule: {rel(repo, path)} did not read back; add one by hand")
            continue
        path.write_text("".join(lines))
        result.changed.append(f"schedule: weekly rebuild ({cron(name)}) in {rel(repo, path)}")


# --------------------------------------------------------- Ashigaru caller


def router_calls(parsed: object) -> set[str]:
    """The `uses:` of each job that calls Ashigaru's router.yml."""
    jobs = parsed.get("jobs") if isinstance(parsed, dict) else None
    return {
        str(job["uses"])
        for job in (jobs or {}).values()
        if isinstance(job, dict) and ROUTER_CALL.match(str(job.get("uses", "")))
    }


def recopy_caller(repo: Path, target: Target, result: Result) -> None:
    """A pre-router ashigaru.yml becomes the canonical router caller, copied unchanged."""
    path = repo / ASHIGARU_CALLER
    if not target.ashigaru_caller or not path.is_file():
        return
    try:
        have = router_calls(yaml.safe_load(path.read_text()))
    except yaml.YAMLError:
        have = set()  # a caller that does not parse calls nothing: replace it
    if have and have <= router_calls(yaml.safe_load(target.ashigaru_caller)):
        return  # already the router caller, at the target's tag
    path.write_text(target.ashigaru_caller)
    result.changed.append(
        f"ashigaru: {ASHIGARU_CALLER} re-copied as the router caller "
        f"(examples/ashigaru.yml at {target.ashigaru_source or ASHIGARU_TAG})"
    )


# ----------------------------------------------------------------- release


def reusable(workflow: object) -> bool:
    """A workflow other workflows call: Ashigaru's own, not a repo's caller."""
    on = workflow.get("on", workflow.get(True)) if isinstance(workflow, dict) else None
    return "workflow_call" in (on if isinstance(on, (dict, list)) else [on])


def tag_jobs(workflow: object) -> list[str]:
    """The jobs that call Ashigaru's release-tag.yml, or release.yml before it."""
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
    return [
        job_id
        for job_id, job in (jobs or {}).items()
        if isinstance(job, dict) and ASHIGARU_TAGS.match(str(job.get("uses", "")))
    ]


def tag_paths(repo: Path) -> list[str]:
    """Every job that tags a release through the app: the router caller's counts once."""
    paths = []
    for path in sorted((repo / WORKFLOWS).glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text()) or {}
        if reusable(workflow):
            continue
        jobs = workflow.get("jobs") or {}
        paths += [f"{rel(repo, path)}:{j}" for j in tag_jobs(workflow)]
        paths += [
            f"{rel(repo, path)}:{j}"
            for j, job in jobs.items()
            if isinstance(job, dict) and ROUTER_CALL.match(str(job.get("uses", "")))
        ]
    return paths


def drop_block(lines: list[str], at: int, indent: int) -> None:
    """Delete the key at `at`, its block, the comments that introduce it and a blank before."""
    stop = last_content(lines, at, block_end(lines, at, indent))
    while at > 0 and lines[at - 1].startswith(" " * indent + "#"):
        at -= 1
    if at > 0 and not lines[at - 1].strip():
        at -= 1
    del lines[at:stop]


def untagged(text: str, workflow: dict, jobs: list[str]) -> tuple[str, dict]:
    """(text, expected parse) of a workflow with `jobs` gone, and the
    pull_request_target trigger with them when nothing left needs it."""
    lines = text.splitlines(keepends=True)
    expected = yaml.safe_load(yaml.safe_dump(workflow))
    for job_id in jobs:
        span = job_span(lines, job_id)
        if span is None:
            raise ValueError(f"no job `{job_id}` under `jobs:`")
        drop_block(lines, span[0], span[2])
        del expected["jobs"][job_id]
    on_key = "on" if "on" in expected else True
    on = expected.get(on_key)
    if (
        isinstance(on, dict)
        and "pull_request_target" in on
        and "pull_request_target" not in yaml.safe_dump(expected["jobs"])
    ):
        top = next(
            (i for i, x in enumerate(lines) if re.match(r"^(?:on|\"on\"|'on'):\s*(?:#.*)?$", x)),
            None,
        )
        if top is None:
            raise ValueError("no block `on:`")
        end = block_end(lines, top, 0)
        indent = next(
            len(x) - len(x.lstrip()) for x in lines[top + 1 : end] if x.strip() and x[0] == " "
        )
        at = find_key(lines, "pull_request_target", indent, top + 1, end)
        if at is None:
            raise ValueError("`pull_request_target:` is not a key of the block `on:`")
        drop_block(lines, at, indent)
        del on["pull_request_target"]
        if not on:
            raise ValueError("no trigger would be left")
    return "".join(lines).rstrip("\n") + "\n", expected


def open_release(repo: Path, default: str, result: Result) -> None:
    """A router caller tags on merge: the rest of the repo only opens the pull request."""
    release = repo / RELEASE
    if release.is_file() and release.read_text() == templated(RELEASE_TEMPLATE, default):
        release.write_text(templated(OPEN_TEMPLATE, default))
        result.changed.append(f"release: {RELEASE} is now {OPEN_TEMPLATE.name}; the router tags")
        return
    for path in sorted((repo / WORKFLOWS).glob("*.y*ml")):
        text = path.read_text()
        workflow = yaml.safe_load(text) or {}
        jobs = tag_jobs(workflow)
        if reusable(workflow) or router_calls(workflow) or not jobs:
            continue
        if len(jobs) == len(workflow.get("jobs") or {}):
            path.unlink()  # nothing in it but a second tag path
            result.changed.append(
                f"release: {rel(repo, path)} removed; router.yml's release job tags"
            )
            continue
        try:
            new, expected = untagged(text, workflow, jobs)
            readback = yaml.safe_load(new)
        except (ValueError, yaml.YAMLError) as exc:
            result.hand.append(
                f"release: {rel(repo, path)}: {exc}; drop its tag job by hand, the router tags"
            )
            continue
        if readback != expected:
            result.hand.append(
                f"release: {rel(repo, path)} did not read back as only its tag job gone; "
                f"drop it by hand, the router tags"
            )
            continue
        path.write_text(new)
        result.changed.append(
            f"release: tag job {', '.join(jobs)} dropped from {rel(repo, path)}; "
            f"router.yml's release job tags"
        )
    if (repo / RELEASE).exists():
        if "ashigaru/release-" not in (repo / RELEASE).read_text():
            result.hand.append(
                f"release: {RELEASE} exists and does not open Ashigaru's release pull "
                f"request; merge {OPEN_TEMPLATE.name} by hand"
            )
        return
    write_template(repo, OPEN_TEMPLATE, default, result, "opens the release PR; the router tags")


def templated(template: Path, default: str) -> str:
    text = template.read_text()
    if default != "main":
        text = text.replace("refs/heads/main", f"refs/heads/{default}")
        text = text.replace("--base main", f"--base {default}")
    return text


def write_template(repo: Path, template: Path, default: str, result: Result, what: str) -> None:
    if not SAFE_BRANCH.fullmatch(default):
        # It lands in a shell line that runs with the app token: never written as is.
        result.hand.append(f"release: default branch {default!r} is not a plain name; add by hand")
        return
    (repo / RELEASE).write_text(templated(template, default))
    result.changed.append(f"release: {RELEASE} ({what})")


def add_release_flow(repo: Path, name: str, default: str, result: Result) -> None:
    if name == ASHIGARU_REPO:
        return  # it hosts router.yml and release-tag.yml, and releases by its own sweep
    workflows = sorted((repo / WORKFLOWS).glob("*.y*ml"))
    parsed = {path: yaml.safe_load(path.read_text()) or {} for path in workflows}
    routed = any(
        u.endswith(f"/router.yml@{ASHIGARU_TAG}")
        for workflow in parsed.values()
        if not reusable(workflow)
        for u in router_calls(workflow)
    )
    if routed:
        open_release(repo, default, result)
    else:
        tagged_release(repo, default, parsed, result)
    paths = tag_paths(repo)
    if len(paths) > 1:
        result.hand.append(
            f"release: {len(paths)} tag paths ({', '.join(paths)}); keep exactly one"
        )


def tagged_release(repo: Path, default: str, parsed: dict[Path, dict], result: Result) -> None:
    """No router: examples/release.yml both opens the pull request and tags it."""
    if not any(release_calls(p) for p in parsed.values()):
        return  # publishes no image, so no cosign release-tag ruleset to satisfy
    for path, workflow in parsed.items():
        for job in (workflow.get("jobs") or {}).values():
            uses = str(job.get("uses", "")) if isinstance(job, dict) else ""
            if uses.endswith(f"/release-tag.yml@{ASHIGARU_TAG}") and ASHIGARU_TAGS.match(uses):
                return  # already tags through the app
            if ASHIGARU_TAGS.match(uses):
                result.hand.append(
                    f"release: {rel(repo, path)} tags through {uses}; move it to "
                    f"release-tag.yml@{ASHIGARU_TAG} by hand rather than add a second flow"
                )
                return
    if (repo / RELEASE).exists():
        result.hand.append(f"release: {RELEASE} exists and is not the Ashigaru flow; merge by hand")
        return
    write_template(repo, RELEASE_TEMPLATE, default, result, "Ashigaru app tags v*.*.*")


# -------------------------------------------------------------------- run


def rel(repo: Path, path: Path) -> str:
    return str(path.relative_to(repo))


def cutover(repo: Path, name: str, default: str, target: Target) -> Result:
    """Apply every cutover change to the checkout at `repo`, in place."""
    result = Result()
    move_constitution(repo, target, result)
    drop_dependabot(repo, target, result)
    rename(repo, result)
    sign(repo, result)
    self_published(repo, result)
    if name in target.weekly:
        add_schedule(repo, name, result)
    recopy_caller(repo, target, result)  # before the release check, which counts the router
    add_release_flow(repo, name, default, result)
    result.pins_left = pins_left(repo, target)
    if result.pins_left:
        result.hand.append(
            f"constitution: {len(result.pins_left)} call(s) left off {target.constitution_ref}: "
            + ", ".join(result.pins_left)
        )
    return result


def validate(repo: Path, target: Target) -> list[str]:
    """The target constitution's verdict on the converted checkout; [] when it passes."""
    if target.validator is None:
        return []
    if not (repo / MANIFEST).is_file():
        # Nothing to judge is not a pass: a repo the validator cannot read ships unjudged.
        return [f"MANIFEST: no {MANIFEST}, so the target constitution cannot judge it"]
    mine = target.validator.own_version() or ""
    return [
        v
        for v in target.validator.validate(repo / MANIFEST)
        # A dry run ahead of the release judges with a validator one version short.
        if not (v.startswith("PIN:") and "newer than this validator" in v and mine)
    ]
