"""`pypi`: publish a merged release to PyPI by OIDC trusted publishing.

Runs on the default-branch push that merges an Ashigaru release PR, in the
`pypi` environment with `id-token: write` and a read-only GITHUB_TOKEN. It holds
no secret. The plan (`SDLC_CHECK`) only decides that this job runs: it
refuses unless `write` succeeded, the tree asks for PyPI, and the API agrees
with `write` about the release. The build backend is repo code, so `uv build`
runs in a nested container (no token, no capabilities, no socket) and only the
`uv publish` of the finished files runs here, where the OIDC request variables are.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

from sdlc_jobs._core import (
    CLEAN,
    Tools,
    cannot,
    host_mount,
    must,
    needed,
    podman,
    step,
    tool_sdlc,
    workspace,
)
from sdlc_jobs._release import verify_release

INDEX_CHECK = "https://pypi.org/simple/"


def parse_release(raw: str | None) -> dict:
    try:
        doc = json.loads(raw or "")
    except ValueError:
        cannot("SDLC_RELEASE is not JSON: write's output is missing")
    if not isinstance(doc, dict) or not all(
        isinstance(doc.get(k), str) for k in ("version", "sha")
    ):
        cannot("SDLC_RELEASE is not {version, sha}")
    return doc


def build(tools: Tools, env: Mapping[str, str], root: Path, version: str) -> list[Path]:
    """`uv build` in a nested container; the dist/ files, each carrying the version."""
    host, image = host_mount(tools, env, root)
    out = root / "dist"
    if out.exists():
        cannot(
            "dist/ already exists in the checkout: refusing to publish what the build did not make"
        )
    must(
        tools,
        [
            *podman(env),
            "run",
            "--rm",
            "--log-driver=none",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "-v",
            f"{host}:/repo:rw,z",
            "-w",
            "/repo",
            "--tmpfs",
            "/tmp:rw,exec,size=4g,mode=1777",  # noqa: S108 -- inside the nested container
            "-e",
            "HOME=/tmp",
            "--entrypoint",
            "uv",
            image,
            "build",
            "--out-dir",
            "dist",
        ],
    )
    name = tomllib.loads((root / "pyproject.toml").read_text())["project"]["name"]
    stem = re.sub(r"[-_.]+", "_", name).lower()
    files = sorted(p for p in out.glob("*") if p.is_file())
    same = stem.replace("_", "[-_.]")  # sdists of older backends keep the hyphens
    pattern = re.compile(rf"{same}-{re.escape(version)}(\.tar\.gz|-.*\.whl)", re.IGNORECASE)
    if not files or not all(pattern.fullmatch(p.name) for p in files):
        cannot(f"dist/ holds {[p.name for p in files]}, not {stem}-{version} files")
    return files


def run(event_name: str, event: dict, env: Mapping[str, str], tools: Tools) -> int:
    del event  # the job contract passes it; a push needs nothing from it
    if event_name != "push":
        cannot(f"pypi runs on a push, not {event_name!r}")
    needed("write", env.get("SDLC_WRITE"))
    release = parse_release(env.get("SDLC_RELEASE"))
    sha = env.get("GITHUB_SHA", "")
    root = workspace(env)

    if tool_sdlc(root).get("publish") != "pypi":
        cannot('the tree does not say [tool.sdlc] publish = "pypi"; the plan is not an authority')
    if not sha or release["sha"] != sha:
        cannot(f"write released {release['sha']}, this push is {sha or 'unknown'}")
    head = must(tools, ["git", "rev-parse", "HEAD"], cwd=str(root)).strip()
    if head != sha:
        cannot(f"the checkout is at {head}, not {sha}")

    step("pypi: re-derive the release from the API")
    verified = verify_release(tools, env, sha)
    if verified["version"] != release["version"]:
        cannot(f"the API says {verified['version']}, write said {release['version']}")

    step(f"pypi: build {verified['version']} in a nested container")
    files = build(tools, env, root, verified["version"])

    step(f"pypi: publish {len(files)} files by trusted publishing")
    # --check-url makes a re-run succeed: files already on PyPI with the same
    # hash are skipped, and a same-named file with different bytes is an error.
    must(
        tools,
        ["uv", "publish", "--trusted-publishing", "always", "--check-url", INDEX_CHECK]
        + [str(p) for p in files],
        cwd=str(root),
    )
    return CLEAN


def main(event_name: str, event: dict, env: Mapping[str, str]) -> int:
    return run(event_name, event, env, tools=Tools())
