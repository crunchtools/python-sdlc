"""Re-derive a release from the API, never from a plan or another job's word.

`pypi` and `write` both call `verify_release` on the push that merges a release
PR. It runs the seven tests of "Releases run from the merge" and either returns
the release or exits 2 naming the one that failed.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from sdlc_jobs._core import SEMVER, Tools, api, cannot, workspace

APP_LOGIN = "ashigaru-crunchtools-com[bot]"  # the App's login: its slug is ashigaru-crunchtools-com
RELEASE_BRANCH = "ashigaru/release-"
HEADING = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)


def tree_version(root: Path) -> str:
    """[project].version of the merged tree's pyproject.toml, or the dynamic one hatch
    reads from `[tool.hatch.version].path` (ashigaru's VERSION), as `write` reads it;
    anything else is a refusal."""
    path = root / "pyproject.toml"
    if not path.is_file():
        cannot("test 7: the merged tree has no pyproject.toml to read the version from")
    try:
        doc = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        cannot(f"pyproject.toml does not parse: {exc}")
    project = doc.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if isinstance(version, str):
        return version
    dynamic = project.get("dynamic") if isinstance(project, dict) else None
    if not isinstance(dynamic, list) or "version" not in dynamic:
        cannot("test 7: pyproject.toml has no [project].version, static or dynamic")
    return hatch_version(root, doc)


def hatch_version(root: Path, doc: dict) -> str:
    """The X.Y.Z in the file `[tool.hatch.version].path` names, inside the tree."""
    tool = doc.get("tool")
    hatch = tool.get("hatch") if isinstance(tool, dict) else None
    table = hatch.get("version") if isinstance(hatch, dict) else None
    path = table.get("path") if isinstance(table, dict) else None
    if not isinstance(path, str) or not path or "\\" in path:
        cannot("test 7: a dynamic version needs [tool.hatch.version].path")
    real = (root / path).resolve()
    if PurePosixPath(path).is_absolute() or not real.is_relative_to(root.resolve()):
        cannot(f"test 7: [tool.hatch.version].path {path!r} leaves the tree")
    try:
        found = real.read_text().strip()
    except OSError as exc:
        cannot(f"test 7: {path} cannot be read: {exc}")
    if not SEMVER.fullmatch(found):
        cannot(f"test 7: {path} holds {found[:40]!r}, not X.Y.Z")
    return found


def changelog_version(root: Path) -> str:
    """The newest released heading: the first `## [X.Y.Z]`, skipping `## [Unreleased]`."""
    try:
        text = (root / "CHANGELOG.md").read_text()
    except OSError as exc:
        cannot(f"CHANGELOG.md cannot be read: {exc}")
    newest = next((h for h in HEADING.findall(text) if h.lower() != "unreleased"), None)
    if newest is None:
        cannot("test 7: CHANGELOG.md has no `## [X.Y.Z]` heading")
    return newest


def merged_pull(tools: Tools, env: Mapping[str, str], repo: str, sha: str) -> dict:
    """Tests 1 to 3: the one merged pull request, merged as `sha`, by the App."""
    login = env.get("SDLC_APP_LOGIN") or APP_LOGIN
    pulls = api(tools, env, "GET", f"/repos/{repo}/commits/{sha}/pulls")
    if not isinstance(pulls, list) or len(pulls) != 1 or not isinstance(pulls[0], dict):
        cannot(f"test 1: {sha[:7]} is not in exactly one pull request")
    pull = pulls[0]
    if not pull.get("merged_at"):
        cannot("test 1: its pull request is not merged")
    if pull.get("merge_commit_sha") != sha:
        cannot(f"test 2: the pull request merged {pull.get('merge_commit_sha')}, not {sha}")
    if (pull.get("user") or {}).get("login") != login:
        cannot(f"test 3: the pull request's author is not {login}")
    return pull


def verify_release(tools: Tools, env: Mapping[str, str], sha: str) -> dict:
    """The release `sha` merged, as the API and the merged tree say it, or CannotRunError."""
    repo = env.get("GITHUB_REPOSITORY", "")
    if not repo:
        cannot("GITHUB_REPOSITORY is not set")
    pull = merged_pull(tools, env, repo, sha)

    info = api(tools, env, "GET", f"/repos/{repo}")
    default = info.get("default_branch") if isinstance(info, dict) else None
    if not default or (pull.get("base") or {}).get("ref") != default:
        cannot(f"test 4: the pull request's base is not the default branch {default!r}")
    head = pull.get("head") or {}
    if ((head.get("repo") or {}).get("full_name") or "").lower() != repo.lower():
        cannot(f"test 5: the pull request's head repository is not {repo}")

    ref = head.get("ref") or ""
    version = ref.removeprefix(RELEASE_BRANCH)
    if not ref.startswith(RELEASE_BRANCH) or not SEMVER.fullmatch(version):
        cannot(f"test 6: head ref {ref!r} is not {RELEASE_BRANCH}X.Y.Z")

    root = workspace(env)
    for what, found in (
        ("pyproject.toml", tree_version(root)),
        ("CHANGELOG.md", changelog_version(root)),
    ):
        if found != version:
            cannot(f"test 7: {what} says {found}, the release branch says {version}")
    return {"version": version, "pr": pull.get("number"), "sha": sha}
