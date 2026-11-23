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
import re
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
    host_mount,
    must,
    set_output,
    step,
    tool_sdlc,
    workspace,
)

SDLC = "/usr/local/bin/sdlc"  # this image's own in-image half
WEEKLY = "17 6 * * 1"
POLL = "47 */6 * * *"
RELEASE_REF = "ashigaru/release-"
BASE_LABEL = "org.opencontainers.image.base.digest"  # what the image job stamps on a build
# Where a gate leaves SARIF, as category -> file name. CodeQL writes
# /tmp/codeql-results.sarif inside its nested container, which is gone when it exits, and
# nothing runs zizmor yet: until they land here, there is nothing to upload.
SARIF_FILES = {"/language:python": "codeql-results.sarif", "zizmor": "zizmor.sarif"}
FROM_AS_WORDS = 3  # image, AS, name


def main(event_name: str, event: dict, env: Mapping[str, str]) -> int:
    return run(event_name, event, env, tools=Tools())


def run(event_name: str, event: dict, env: Mapping[str, str], tools: Tools) -> int:
    root = workspace(env)
    repo = env.get("GITHUB_REPOSITORY", "")
    default_push = event_name == "push" and env.get("GITHUB_REF") == (
        f"refs/heads/{(event.get('repository') or {}).get('default_branch', '')}"
    )
    out: dict[str, Any] = {
        "image": False,
        "release": None,
        "write": False,
        "weekly": False,
        "audit": None,
    }

    cron = event.get("schedule") if event_name == "schedule" else None
    out["weekly"] = cron == WEEKLY
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
    out["write"] = bool(out["release"] or (out["audit"] or {}).get("found"))
    set_output(env, "result", json.dumps(out))

    if event_name not in ("pull_request", "push", "workflow_dispatch"):
        return CLEAN
    rc = gates(tools, env, root)
    if default_push:
        upload_sarif(tools, env, repo)
    return rc


def containerfile(root: Path) -> str | None:
    """The root Containerfile or Dockerfile, Containerfile first."""
    return next((f for f in ("Containerfile", "Dockerfile") if (root / f).is_file()), None)


def launch(
    tools: Tools, env: Mapping[str, str], root: Path, command: str
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
    return tools.run([str(script), command], cwd=str(root), env=child)


def gates(tools: Tools, env: Mapping[str, str], root: Path) -> int:
    """`sdlc ci` for this tree, through the launcher so repo code runs only nested."""
    step("gates: sdlc ci")
    proc = launch(tools, env, root, "ci")
    print(proc.stdout + proc.stderr, end="", flush=True)
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
    return {"version": version, "publish": publish, "pr": pr["number"]}


def audit(tools: Tools, env: Mapping[str, str], root: Path) -> dict:
    """The weekly dependency audit: the image's own `pip-audit` gate, 1 meaning findings."""
    if not (root / "pyproject.toml").is_file():
        return {"found": False, "summary": "no Python project to audit"}
    proc = launch(tools, env, root, "pip-audit")
    if proc.returncode not in (CLEAN, FOUND):
        cannot(f"pip-audit could not run (exit {proc.returncode})")
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-10:]
    return {"found": proc.returncode == FOUND, "summary": "\n".join(tail)[:2000]}


def final_base(text: str) -> str | None:
    """The image the last stage of a Containerfile is built on; None for scratch or unknown.

    Small on purpose: global ARG defaults filled in, and a `FROM <earlier stage>` followed
    back to its own base. The root `sdlc` script's `final_base` is the thorough one.
    """
    args: dict[str, str] = {}
    stages: list[tuple[str, str | None]] = []
    for raw in re.sub(r"\\\n", " ", text).splitlines():
        words = raw.split()
        op = words[0].upper() if words else ""
        if op == "ARG" and not stages:
            for name, eq, value in (w.partition("=") for w in words[1:]):
                if eq:
                    args[name] = value
        elif op == "FROM":
            rest = [w for w in words[1:] if not w.startswith("--")]
            aliased = len(rest) >= FROM_AS_WORDS and rest[1].upper() == "AS"
            if rest:
                stages.append((rest[0], rest[2].lower() if aliased else None))
    index = len(stages) - 1
    while index >= 0:
        image = re.sub(
            r"\$\{(\w+)(?::?-([^}]*))?\}|\$(\w+)",
            lambda m: args.get(m[1] or m[3], m[2] or ""),
            stages[index][0],
        ).strip("\"'")
        earlier = [i for i in range(index) if stages[i][1] == image.lower()]
        if not earlier:
            return image if image and image != "scratch" else None
        index = earlier[-1]
    return None


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
    published = f"quay.io/crunchtools/{repo.rpartition('/')[2]}:latest"
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
    root = Path(env.get("SDLC_SARIF_DIR") or "/tmp")  # noqa: S108
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
        if resp.status not in (200, 201, 202):
            cannot(f"uploading {fname} answered {resp.status}: {resp.body[:300]!r}")
