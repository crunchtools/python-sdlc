"""The `check` job: gate the commit, then say which later jobs it needs.

It holds a read token, `security-events: write`, `actions: write` and the runner's
container socket, and no secret. That is the one level a pull request may run, so
the repo's own code reaches it only through the launcher's nested containers.

It does four things, in this order, because a failed check still prints its plan
(nothing acts on it: every later job also needs `check` to have succeeded):

1. Work out the plan and write it to `$GITHUB_OUTPUT` as `result` (the schema is in
   `_core.plan`). It is a plan, never an authority: `write` and `image` re-derive
   what they act on.
2. Run the gates (pull_request, push, dispatch): the launcher's `ci`, which is the
   fleet's `sdlc ci` -- ruff, mypy, pytest, semgrep, pip-audit, CodeQL, the
   constitution validator, and the container build, smoke and scan for an image repo.
3. Upload SARIF on a default-branch push.
4. The weekly run also audits dependencies and keeps the schedule alive.

An exit that is not a verdict (the launcher's 2 or 3, an unreadable digest, an API
that refused) is a 2. A 3 is "CI will judge it", and here we are CI.
"""

from __future__ import annotations

import base64
import gzip
import json
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sdlc_jobs._core import (
    CANNOT_RUN,
    CLEAN,
    FOUND,
    SEMVER,
    SOCKET,
    Tools,
    api,
    call,
    cannot,
    final_stage,
    host_mount,
    image_settings,
    must,
    set_output,
    step,
    tool_sdlc,
    workspace,
)

SDLC = "sdlc"  # this image's own in-image half, found on PATH
ACCEPTED = 202
WEEKLY = "17 6 * * 1"
POLL = "47 */6 * * *"
RELEASE_REF = "ashigaru/release-"
# The line `sdlc pip-audit -- --report` prints its JSON report on; the same in `sdlc`.
AUDIT_REPORT = "SDLC-AUDIT-REPORT "
BASE_LABEL = "org.opencontainers.image.base.digest"  # what the image job stamps on a build
# Where a gate leaves SARIF, as category -> file name. CodeQL writes
# codeql-results.sarif to the temp dir inside its nested container, which is gone when it exits, and
# nothing runs zizmor yet: until they land here, there is nothing to upload.
SARIF_FILES = {"/language:python": "codeql-results.sarif", "zizmor": "zizmor.sarif"}


def run(event_name: str, event: dict, env: Mapping[str, str], tools: Tools) -> int:
    """Gate this commit and write the plan; the gates' verdict is the exit code.

    `event_name` and `event` are the workflow's: a push to the default branch
    (named in `event`) may plan an image and a release, a schedule's cron the
    weekly audit or the parent poll. `env` is the job's environment
    (GITHUB_*, GITHUB_OUTPUT for the plan). `tools` runs podman, skopeo and the
    GitHub API (a fake in the suites). 0 clean, 1 a gate found something, and
    CannotRunError, exit 2, when a gate or the plan could not be made.
    """
    root = workspace(env)
    repo = env.get("GITHUB_REPOSITORY", "")
    default_push = event_name == "push" and env.get("GITHUB_REF") == (
        f"refs/heads/{(event.get('repository') or {}).get('default_branch', '')}"
    )
    cron = event.get("schedule") if event_name == "schedule" else None
    set_output(env, "result", json.dumps(make_plan(tools, env, root, default_push, cron)))

    if event_name not in ("pull_request", "push", "workflow_dispatch"):
        return CLEAN
    rc = gates(tools, env, root)
    if default_push:
        upload_sarif(tools, env, repo)
    return rc


def make_plan(
    tools: Tools, env: Mapping[str, str], root: Path, default_push: bool, cron: object
) -> dict[str, Any]:
    """The plan for this event, in the shape `_core.plan` documents."""
    repo = env.get("GITHUB_REPOSITORY", "")
    out: dict[str, Any] = {
        "image": False,
        "release": None,
        "write": False,
        "weekly": cron == WEEKLY,
        "audit": None,
    }
    has_image = containerfile(root) is not None
    if default_push:
        out["image"] = has_image
        out["release"] = release_plan(tools, env, root, repo)
    elif out["weekly"]:
        out["image"] = has_image
        step("weekly: audit and keepalive")
        out["audit"] = audit(tools, env, root)
        api(tools, env, "PUT", f"/repos/{repo}/actions/workflows/ci.yml/enable")
    elif cron == POLL and has_image:
        out["image"] = parent_moved(tools, root, repo)
    # A clean report goes to `write` too: it is what closes the alert issue.
    out["write"] = bool(out["release"]) or out["audit"] is not None
    return out


def containerfile(root: Path) -> str | None:
    """The root Containerfile or Dockerfile, Containerfile first."""
    return next((f for f in ("Containerfile", "Dockerfile") if (root / f).is_file()), None)


def launch(
    tools: Tools,
    env: Mapping[str, str],
    root: Path,
    command: str,
    *args: str,
    stream: bool = False,
) -> subprocess.CompletedProcess[str]:
    """The launcher's `command` for this tree, judged by this very image.

    `sdlc launcher <ref>` prints the launcher pinned to the image this job runs,
    as bin/sdlc-bootstrap does on a laptop. It drives the runner's engine through
    the socket, so it mounts the workspace by its host path.
    """
    host, image = host_mount(tools, env, root)
    if "@sha256:" not in image:
        cannot(f"this container's image is not pinned by digest ({image}): no launcher for it")
    script = Path(tempfile.mkdtemp(prefix="sdlc-launcher-")) / "sdlc"
    script.write_text(must(tools, [SDLC, "launcher", image]))
    script.chmod(0o700)
    child = {
        **env,
        "SDLC_HOST_REPO": host,
        "CONTAINER_HOST": env.get("CONTAINER_HOST") or SOCKET,
    }
    argv = [str(script), command, *(["--", *args] if args else [])]
    return tools.run(argv, cwd=str(root), env=child, stream=stream)


def gates(tools: Tools, env: Mapping[str, str], root: Path) -> int:
    """`sdlc ci` for this tree, through the launcher so repo code runs only nested."""
    step("gates: sdlc ci")
    proc = launch(tools, env, root, "ci", stream=True)
    return proc.returncode if proc.returncode in (CLEAN, FOUND) else CANNOT_RUN


def release_plan(tools: Tools, env: Mapping[str, str], root: Path, repo: str) -> dict | None:
    """The release this push merges, as a plan: `write` verifies it again from the API."""
    prs = api(tools, env, "GET", f"/repos/{repo}/commits/{env.get('GITHUB_SHA', '')}/pulls")
    if not isinstance(prs, list) or len(prs) != 1 or not prs[0].get("merged_at"):
        return None
    pr = prs[0]
    head = pr.get("head") or {}
    version = str(head.get("ref", "")).removeprefix(RELEASE_REF)
    ok = (
        str(head.get("ref", "")).startswith(RELEASE_REF)
        and SEMVER.fullmatch(version)
        and (head.get("repo") or {}).get("full_name") == repo
        and str((pr.get("user") or {}).get("login", "")).endswith("[bot]")
    )
    if not ok:
        return None
    publish = "pypi" if tool_sdlc(root).get("publish") == "pypi" else ""
    return {"version": version, "publish": publish}


def audit(tools: Tools, env: Mapping[str, str], root: Path) -> dict | None:
    """The weekly dependency audit: pip-audit's JSON report, from the image's own gate.

    `write` hands it to the constitution's `audit_alert`, which opens, rewrites or
    closes the one alert issue, so a clean report is handed on as well. None when
    there is no Python project to audit.
    """
    if not (root / "pyproject.toml").is_file():
        return None
    proc = launch(tools, env, root, "pip-audit", "--report")
    if proc.returncode not in (CLEAN, FOUND):
        cannot(f"pip-audit could not run (exit {proc.returncode})")
    lines = [line for line in proc.stdout.splitlines() if line.startswith(AUDIT_REPORT)]
    if not lines:
        cannot("pip-audit printed no report")
    try:
        report = json.loads(lines[-1].removeprefix(AUDIT_REPORT))
    except ValueError as exc:
        cannot(f"pip-audit's report is not JSON: {exc}")
    if not isinstance(report, dict) or not isinstance(report.get("dependencies"), list):
        cannot("pip-audit's report has no dependencies list")
    return report


def final_base(text: str) -> str | None:
    """The image the last stage of a Containerfile is built on; None for scratch or unknown."""
    found = final_stage(text, strict=False)
    if found is None or not found[1] or found[1].lower() == "scratch":
        return None
    return found[1]


def parent_moved(tools: Tools, root: Path, repo: str) -> bool:
    """Has the final stage's FROM moved since the last published image was built on it?

    Unreadable on either side is a 2: a poll that guessed "unchanged" would never rebuild.
    """
    name = containerfile(root)
    if name is None:
        return False
    base = final_base((root / name).read_text(encoding="utf-8", errors="replace"))
    if base is None:
        return False
    step(f"poll: {base}")
    now = must(tools, ["skopeo", "inspect", "--format", "{{.Digest}}", f"docker://{base}"]).strip()
    published = f"{image_settings(root, repo)['image']}:latest"
    raw = must(tools, ["skopeo", "inspect", f"docker://{published}"])
    try:
        built_on = json.loads(raw).get("Labels", {}).get(BASE_LABEL)
    except (ValueError, AttributeError) as exc:
        cannot(f"{published} did not inspect as JSON: {exc}")
    if not built_on:
        cannot(f"{published} has no {BASE_LABEL} label; cannot tell whether the parent moved")
    return str(built_on) != now


def upload_sarif(tools: Tools, env: Mapping[str, str], repo: str) -> None:
    """Send each SARIF the gates left to the Security tab, under its own category."""
    root = Path(env.get("SDLC_SARIF_DIR") or tempfile.gettempdir())
    for category, fname in SARIF_FILES.items():
        path = root / fname
        if not path.is_file():
            print(f"no {fname}: nothing to upload for {category}", flush=True)
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            cannot(f"{path} is not readable SARIF: {exc}")
        for r in doc.get("runs", []):
            r.setdefault("automationDetails", {})["id"] = f"{category}/"
        packed = gzip.compress(json.dumps(doc).encode())
        body = {
            "commit_sha": env.get("GITHUB_SHA", ""),
            "ref": env.get("GITHUB_REF", ""),
            "sarif": base64.b64encode(packed).decode(),
        }
        resp = call(tools, env, "POST", f"/repos/{repo}/code-scanning/sarifs", body)
        if resp.status not in (200, 201, ACCEPTED):
            cannot(f"uploading {fname} answered {resp.status}: {resp.body[:300]!r}")
