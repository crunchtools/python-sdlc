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
                   ruleset lets through

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
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
RELEASE_TEMPLATE = ROOT / "examples" / "release.yml"

MANIFEST = Path(".specify/memory/constitution.md")
HOOKS = Path(".pre-commit-config.yaml")
WORKFLOWS = Path(".github/workflows")
RELEASE = WORKFLOWS / "release.yml"
CONFIGS = (Path("pyproject.toml"), Path("sdlc.toml"))

# fleet-bump.py's own three patterns: the pins that move with a constitution release.
CONSTITUTION_PIN = re.compile(
    r"(crunchtools/constitution/\.github/workflows/[\w-]+\.ya?ml@)v\d+\.\d+\.\d+(?![\w-]|\.[\w-])"
)
INHERITS = re.compile(r"^(>\s*\*\*Inherits:\*\*.*?)v\d+\.\d+\.\d+", re.M)
HOOK_REV = re.compile(
    r"(repo:\s*https://github\.com/crunchtools/constitution/?\s*\n\s*rev:\s*)v\d+\.\d+\.\d+"
)
# The repo's old name as a word: not python-sdlc-extras, not a domain, but a
# sentence's last word is still one.
OLD_NAME = re.compile(r"(?<![\w.-])python-sdlc(?![\w-]|\.[\w-])")
RELEASE_CALL = re.compile(r"^crunchtools/constitution/\.github/workflows/container-release\.yml@")
ASHIGARU_TAGS = re.compile(r"^crunchtools/ashigaru/\.github/workflows/release(?:-tag)?\.yml@")
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


@dataclass
class Result:
    changed: list[str] = field(default_factory=list)  # one line per change, for the report
    hand: list[str] = field(default_factory=list)  # what it would not guess at


def load(path: Path, name: str) -> ModuleType:
    """A script by path: fleet-bump.py and validate-constitution.py have hyphens."""
    spec = importlib.util.spec_from_file_location(name, path)
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
    start = None if child is None else find_key(lines, job_id, child, top + 1, end)
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
    start, end, indent = span
    at = find_key(lines, "with", key, start + 1, end)
    note = "".join(f"{' ' * (key + 2)}{line}\n" for line in SIGN_NOTE.splitlines())
    if at is None:
        uses = find_key(lines, "uses", key, start + 1, end)
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


# ----------------------------------------------------------------- release


def add_release_flow(repo: Path, default: str, result: Result) -> None:
    workflows = sorted((repo / WORKFLOWS).glob("*.y*ml"))
    parsed = {path: yaml.safe_load(path.read_text()) or {} for path in workflows}
    if not any(release_calls(p) for p in parsed.values()):
        return  # publishes no image, so no cosign release-tag ruleset to satisfy
    for path, workflow in parsed.items():
        for job in (workflow.get("jobs") or {}).values():
            uses = str(job.get("uses", "")) if isinstance(job, dict) else ""
            if uses.endswith("release-tag.yml@v5") and ASHIGARU_TAGS.match(uses):
                return  # already tags through the app
            if ASHIGARU_TAGS.match(uses):
                result.hand.append(
                    f"release: {rel(repo, path)} tags through {uses}; move it to "
                    f"release-tag.yml@v5 by hand rather than add a second flow"
                )
                return
    if (repo / RELEASE).exists():
        result.hand.append(f"release: {RELEASE} exists and is not the Ashigaru flow; merge by hand")
        return
    if not SAFE_BRANCH.fullmatch(default):
        # It lands in a shell line that runs with the app token: never written as is.
        result.hand.append(f"release: default branch {default!r} is not a plain name; add by hand")
        return
    text = RELEASE_TEMPLATE.read_text()
    if default != "main":
        text = text.replace("refs/heads/main", f"refs/heads/{default}")
        text = text.replace("--base main", f"--base {default}")
    (repo / RELEASE).write_text(text)
    result.changed.append(f"release: {RELEASE} (Ashigaru app tags v*.*.*)")


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
    add_release_flow(repo, default, result)
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
