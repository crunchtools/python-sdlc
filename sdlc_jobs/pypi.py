"""`pypi`: publish a merged release to PyPI by OIDC trusted publishing, or a token.

Runs on the default-branch push that merges an Ashigaru release PR, in the
`pypi` environment with `id-token: write` and a read-only GITHUB_TOKEN. In CI it
holds no secret. The plan (`SDLC_CHECK`) only decides that this job runs: it
refuses unless `write` succeeded, the tree asks for PyPI, and the API agrees
with `write` about the release. The build backend is repo code, so `uv build`
runs in a nested container (no token, no capabilities, no socket) and only the
`uv publish` of the finished files runs here, where the OIDC request variables are.

With no OIDC request variables, as under scripts/local-release.sh where no
Actions run exists, a PyPI API token in UV_PUBLISH_TOKEN publishes instead. It
is taken out of this process's environment before the first step, so no
subprocess inherits it, and handed to the `uv publish` process alone, by its
environment, never argv.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

from sdlc_jobs._core import (
    CLEAN,
    Tools,
    cannot,
    fetch_tree,
    host_mount,
    must,
    needed,
    no_new_privileges,
    podman,
    step,
    tool_sdlc,
    workspace,
)
from sdlc_jobs._tools import safe_directory
from sdlc_jobs._verify_release import verify_release

INDEX_CHECK = "https://pypi.org/simple/"
# Named on the command line, with --no-config: uv runs in the checkout, where a
# committed uv.toml or [tool.uv] publish-url, or one the build wrote, would
# otherwise choose where the files and the credential go.
UPLOAD = "https://upload.pypi.org/legacy/"
PUBLISH_ENV = "UV_PUBLISH_TOKEN"
# What Actions sets for a job with `id-token: write`: uv mints its OIDC token from them.
OIDC = ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN")
EVENTS = ("push",)  # ci.yml's `pypi` job runs on a push and nothing else


def parse_release(raw: str | None) -> dict:
    try:
        doc = json.loads(raw or "")
    except ValueError as exc:
        cannot(f"SDLC_RELEASE is not JSON, so write's output is missing: {exc}")
    if not isinstance(doc, dict) or not all(
        isinstance(doc.get(k), str) for k in ("version", "sha")
    ):
        cannot("SDLC_RELEASE is not {version, sha}")
    return doc


def project_name(root: Path) -> str:
    """[project].name from pyproject.toml; read before the build, which can rewrite it."""
    try:
        doc = tomllib.loads((root / "pyproject.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        cannot(f"pyproject.toml is not readable: {exc}")
    name = (doc.get("project") or {}).get("name")
    if not isinstance(name, str) or not name:
        cannot("pyproject.toml has no [project].name to publish")
    return name


def credential(env: Mapping[str, str]) -> tuple[str, str | None]:
    """("oidc", None) or ("token", the token): one way to publish, never both or neither."""
    oidc = all(env.get(k) for k in OIDC)
    token = env.get(PUBLISH_ENV) or None
    if oidc and token:
        cannot(
            f"both OIDC and {PUBLISH_ENV} are set; a job publishes one way, and CI holds no token"
        )
    if not oidc and not token:
        cannot(f"no OIDC request variables and no {PUBLISH_ENV}: nothing to publish with")
    return ("oidc", None) if oidc else ("token", token)


def build(tools: Tools, env: Mapping[str, str], root: Path, version: str, name: str) -> list[Path]:
    """`uv build` in a nested container; the dist/ files, each carrying `name` and the version."""
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
            *no_new_privileges(env, tools.run),
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
        env=env,
    )
    return dist_files(out, name, version)


def dist_files(out: Path, name: str, version: str) -> list[Path]:
    """The files `uv build` left in `out`, each named for this project at this version."""
    stem = re.sub(r"[-_.]+", "_", name).lower()
    files = sorted(p for p in out.glob("*") if p.is_file())
    same = stem.replace("_", "[-_.]")  # sdists of older backends keep the hyphens
    pattern = re.compile(rf"{same}-{re.escape(version)}(\.tar\.gz|-.*\.whl)", re.IGNORECASE)
    if not files or not all(pattern.fullmatch(p.name) for p in files):
        cannot(f"dist/ holds {[p.name for p in files]}, not {stem}-{version} files")
    return files


def run(event_name: str, event: dict, env: Mapping[str, str], tools: Tools) -> int:
    """Re-verify the release `write` made and publish it to PyPI by OIDC; 0 published.

    `event_name` must be one of `EVENTS`. `env` carries SDLC_RELEASE ({version, sha} from
    `write`), GITHUB_REPOSITORY and the OIDC request variables, or with none of
    them UV_PUBLISH_TOKEN. `tools` runs
    the GitHub API, the build and the upload (a fake in the suites). Raises
    CannotRunError, exit 2, when the release does not verify from the API or
    a step fails.
    """
    del event  # the job contract passes it; a push needs nothing from it
    if event_name not in EVENTS:
        cannot(f"pypi runs on {', '.join(EVENTS)}, not {event_name!r}")
    needed("write", env.get("SDLC_WRITE"))
    how, token = credential(env)
    # Out of os.environ too: shared helpers run podman with the inherited environment.
    os.environ.pop(PUBLISH_ENV, None)
    env = {k: v for k, v in env.items() if k != PUBLISH_ENV}
    release = parse_release(env.get("SDLC_RELEASE"))
    sha = env.get("GITHUB_SHA", "")
    root = workspace(env)
    fetch_tree(tools, env, root, env.get("GITHUB_REPOSITORY", ""))

    if tool_sdlc(root).get("publish") != "pypi":
        cannot('the tree does not say [tool.sdlc] publish = "pypi"; the plan is not an authority')
    if not sha or release["sha"] != sha:
        cannot(f"write released {release['sha']}, this push is {sha or 'unknown'}")
    genv = {**env, **safe_directory(env, root)}  # the mounted workspace: #157
    head = must(tools, ["git", "rev-parse", "HEAD"], cwd=str(root), env=genv).strip()
    if head != sha:
        cannot(f"the checkout is at {head}, not {sha}")

    step("pypi: re-derive the release from the API")
    verified = verify_release(tools, env, sha)
    if verified["version"] != release["version"]:
        cannot(f"the API says {verified['version']}, write said {release['version']}")

    name = project_name(root)  # before the build gets the workspace read-write
    step(f"pypi: build {verified['version']} in a nested container")
    files = build(tools, env, root, verified["version"], name)

    # --check-url makes a re-run succeed: files already on PyPI with the same
    # hash are skipped, and a same-named file with different bytes is an error.
    argv = ["--publish-url", UPLOAD, "--check-url", INDEX_CHECK, *(str(p) for p in files)]
    if how == "oidc":
        step(f"pypi: publish {len(files)} files by trusted publishing")
        argv = ["--trusted-publishing", "always", *argv]
        upload_env = env
    else:
        step(f"pypi: publish {len(files)} files with an API token")
        argv = ["--trusted-publishing", "never", *argv]
        upload_env = {**env, PUBLISH_ENV: str(token)}
    must(tools, ["uv", "--no-config", "publish", *argv], cwd=str(root), env=upload_env)
    return CLEAN
