"""Releases from the `write` job: open the PR, verify the merge, tag it, keep the audit issue.

`write` holds the Ashigaru App's token and never runs a model. A release is the
one thing it does that outlives the run, so nothing here trusts what `check`
planned: the plan decides which jobs are needed, the API decides whether the
release is real. Tagging and the GitHub Release stay Ashigaru's own code
(`release_publish.publish`) and the audit issue stays sdlc's own
`_audit_alert` (vendored from the constitution); this module only decides
when they may run and with what.

Return ints follow the contract in `_github`: 0 done, 1 a refusal or verdict,
and `CannotRunError` for anything that stopped the job from judging.
"""

from __future__ import annotations

import base64
import json
import re
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from sdlc_jobs import _tools as tools
from sdlc_jobs._github import APP_LOGIN, APP_SLUG, Api, ApiError
from sdlc_jobs._tools import Site

_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_BRANCH = "ashigaru/release-"
_NOT_FOUND = 404
# A pyproject.toml that is there but cannot be read as one. A named tuple, not
# PEP 758's bare list, which Gourmand's parser cannot read.
_UNREADABLE = (KeyError, ValueError, UnicodeDecodeError)
# How uv.lock records the package it locks from the checkout itself.
_ROOT_SOURCES = ({"editable": "."}, {"virtual": "."})
# The directory holding the `sdlc_jobs` package, handed to the audit child so it
# imports the vendored `_audit_alert` without relying on the interpreter's path.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent

# Run under `python3 -I`, with the repo, version and sha as argv and never
# spliced into the code. `publish` returns 1 for a CHANGELOG that is not at the
# version. An uncaught exception would also exit 1, so a crash is turned into a
# 3: only a real verdict may look like a refusal.
_PUBLISH = """
import sys, traceback
sys.path.insert(0, sys.argv[1])
try:
    import release_publish
    code = release_publish.publish(sys.argv[2], sys.argv[3], sys.argv[4])
except BaseException:
    traceback.print_exc()
    code = 3
sys.exit(code)
"""

# `audit_alert.alerts()` keeps an issue only when its author is `AUTHOR` or the
# literal "app/github-actions". gh names an App's account `app/<slug>`, and the
# App now writes the issue, so the default AUTHOR would never find it and the
# weekly run would open a second alert each time. Setting AUTHOR to the App's
# name matches its own issues, and the literal still matches the old ones.
_AUDIT = f"""
import sys, traceback
sys.path.insert(0, sys.argv[1])
sys.argv[1:2] = []
try:
    from sdlc_jobs import _audit_alert as audit_alert
    audit_alert.AUTHOR = "app/{APP_SLUG}"
    code = audit_alert.main()
except BaseException:
    traceback.print_exc()
    code = 3
sys.exit(code)
"""


def _error(message: str) -> int:
    print(f"::error::{message}")
    return 1


def open_release_pr(api: Api, repo: str, version: str, default_branch: str) -> int:
    """Copy `chore/release-X.Y.Z` to `ashigaru/release-X.Y.Z` and open its PR, as the App.

    Ashigaru tags only a pull request the App wrote, so the App copies the
    prepared branch rather than a person opening the PR. A rerun finds the
    branch already there and moves it to the prepared commit; a PR already open
    from it is left alone. Either way the copy then gets uv.lock's root package
    at the version, the one version string a hand-prepared branch tends to miss.
    """
    if not _VERSION.fullmatch(version):
        return _error(f"not X.Y.Z: {version}")
    source, branch = f"chore/release-{version}", f"{_BRANCH}{version}"
    try:
        sha = api.request("GET", f"/repos/{repo}/git/ref/heads/{source}")["object"]["sha"]
    except ApiError as err:
        if err.status != _NOT_FOUND:
            raise
        return _error(f"{repo} has no branch {source}: prepare the release there first")
    _point(api, repo, branch, sha)
    stale = _lock_to(api, repo, branch, sha, version)
    if stale:
        return _error(f"{repo} {source}: {stale}")
    return _open_pr(api, repo, sha, version, default_branch)


def _open_pr(api: Api, repo: str, sha: str, version: str, default_branch: str) -> int:
    """Open the release PR from `ashigaru/release-X.Y.Z`, unless one is already open."""
    source, branch = f"chore/release-{version}", f"{_BRANCH}{version}"
    owner = repo.split("/", 1)[0]
    open_prs = api.request("GET", f"/repos/{repo}/pulls?head={owner}:{branch}&state=open") or []
    if open_prs:
        print(f"already open: {open_prs[0].get('html_url', open_prs[0].get('number'))}")
        return 0
    body = (
        f"Release {version}, from {source} at {sha[:7]}. On merge, the `write` job of "
        f"ci.yml verifies the merge, tags v{version} and publishes the GitHub Release."
    )
    made = api.request(
        "POST",
        f"/repos/{repo}/pulls",
        {
            "title": f"chore: release {version}",
            "head": branch,
            "base": default_branch,
            "body": body,
        },
    )
    print(f"opened: {(made or {}).get('html_url', branch)}")
    return 0


def _point(api: Api, repo: str, branch: str, sha: str) -> None:
    """Make `branch` point at `sha`: created when it is not there, force-moved when it is."""
    try:
        api.request("GET", f"/repos/{repo}/git/ref/heads/{branch}")
    except ApiError as err:
        if err.status != _NOT_FOUND:
            raise
        api.request("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})
    else:
        api.request("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {"sha": sha, "force": True})


def _lock_root(text: str) -> dict[str, Any] | None:
    """The package uv.lock locks from the checkout itself, or None; ValueError for two."""
    found = tomllib.loads(text).get("package") or []
    roots = [p for p in found if isinstance(p, dict) and p.get("source") in _ROOT_SOURCES]
    if len(roots) > 1:
        raise ValueError(f"{len(roots)} packages are locked from the checkout")
    return roots[0] if roots else None


def bump_lock(text: str, version: str) -> str | None:
    """uv.lock with its root package at `version`; None when it is already there or has no root.

    The root is the package uv locks from the checkout, `source = { editable = "." }`
    or `{ virtual = "." }`, found by that and never by name: gatehouse's distribution
    is gatehouse-crunchtools. A root with `dynamic = ["version"]` is locked with no
    version line (ashigaru), so it has nothing to move. Raises ValueError for a lock
    that cannot be edited safely.
    """
    root = _lock_root(text)
    if root is None or root.get("version") in {None, version}:
        return None
    name, old = (re.escape(str(root.get(key))) for key in ("name", "version"))
    entry = re.compile(
        rf'^(\[\[package\]\]\nname = "{name}"\nversion = "){old}'
        rf'("\nsource = \{{ (?:editable|virtual) = "\." \}}\n)',
        re.MULTILINE,
    )
    bumped, count = entry.subn(rf"\g<1>{version}\g<2>", text)
    if count != 1:
        raise ValueError(f"the entry for {root.get('name')!r} is not laid out as uv writes it")
    return bumped


def _file_at(api: Api, repo: str, path: str, sha: str) -> dict[str, Any] | None:
    """The contents API's answer for `path` at `sha`, or None when there is no such file."""
    try:
        return api.request("GET", f"/repos/{repo}/contents/{path}?ref={sha}")
    except ApiError as err:
        if err.status == _NOT_FOUND:
            return None
        raise


def _lock_to(api: Api, repo: str, branch: str, sha: str, version: str) -> str | None:
    """Commit uv.lock's root package at `version` onto `branch`, if it is not; the refusal, if any.

    A prepared branch that moved pyproject.toml and not uv.lock fails `uv sync
    --locked` in `check` (#187), so the App moves it rather than the PR going red.
    """
    got = _file_at(api, repo, "uv.lock", sha)
    if got is None:
        return None
    try:
        bumped = bump_lock(base64.b64decode(got["content"]).decode(), version)
    except _UNREADABLE as err:
        return f"uv.lock cannot be brought to {version}: {err}"
    if bumped is not None:
        content = base64.b64encode(bumped.encode()).decode()
        message = f"chore: uv.lock carries {version}"
        update = {"message": message, "content": content, "sha": got["sha"], "branch": branch}
        api.request("PUT", f"/repos/{repo}/contents/uv.lock", update)
        print(f"uv.lock: the root package now at {version}")
    return None


def _tree_version(api: Api, repo: str, sha: str) -> str | bool | None:
    """The version the tree at `sha` states; False when it states none anywhere.

    pyproject.toml's `[project].version` is the Python package's. A pyproject
    with no `[project]` table only configures tools (rotv's carries just
    `[tool.sdlc]`), so the version is package.json's, as a Node repo keeps it.
    A file that names a version table but holds no string version is None.
    """
    got = _file_at(api, repo, "pyproject.toml", sha)
    if got is not None:
        try:
            doc = tomllib.loads(base64.b64decode(got["content"]).decode())
        except _UNREADABLE:
            return None
        if doc.get("project") is not None:
            return _project_version(api, repo, sha, doc)
    node = _file_at(api, repo, "package.json", sha)
    if node is None:
        return False if got is None else None
    try:
        found = json.loads(base64.b64decode(node["content"]).decode()).get("version")
    except (*_UNREADABLE, AttributeError):
        return None
    return found if isinstance(found, str) else None


def _project_version(api: Api, repo: str, sha: str, doc: dict[str, Any]) -> str | None:
    """`[project].version`, or a dynamic one from the hatch path; None when neither."""
    project = doc["project"] if isinstance(doc["project"], dict) else {}
    found = project.get("version")
    if isinstance(found, str):
        return found
    dynamic = project.get("dynamic")
    if isinstance(dynamic, list) and "version" in dynamic:
        return _hatch_version(api, repo, sha, doc)
    return None


def _hatch_version(api: Api, repo: str, sha: str, doc: dict[str, Any]) -> str | None:
    """A dynamic version read from `[tool.hatch.version].path` at `sha` (ashigaru's VERSION).

    Only a relative path inside the repository, and only a file that holds an
    X.Y.Z; anything else is None, as a pyproject naming no version is.
    """
    hatch = (doc.get("tool") or {}).get("hatch") if isinstance(doc.get("tool"), dict) else None
    table = hatch.get("version") if isinstance(hatch, dict) else None
    path = table.get("path") if isinstance(table, dict) else None
    if not isinstance(path, str) or not path or "\\" in path:
        return None
    parts = PurePosixPath(path).parts
    if PurePosixPath(path).is_absolute() or ".." in parts or not parts:
        return None
    got = _file_at(api, repo, "/".join(parts), sha)
    if got is None:
        return None
    try:
        found = base64.b64decode(got["content"]).decode().strip()
    except (*_UNREADABLE, TypeError):
        return None
    return found if _VERSION.fullmatch(found) else None


def _verify(
    api: Api, repo: str, sha: str, planned: Mapping[str, Any], default_branch: str
) -> tuple[str, dict[str, Any]] | str:
    """The (version, pull request) of a real release merge, or the failed test, as text."""
    pulls = api.request("GET", f"/repos/{repo}/commits/{sha}/pulls") or []
    if len(pulls) != 1:
        return f"{sha[:7]} belongs to {len(pulls)} pull requests, not exactly one"
    pull = pulls[0]
    head, base = pull.get("head") or {}, pull.get("base") or {}
    ref = str(head.get("ref") or "")
    version = ref.removeprefix(_BRANCH)
    tests = (
        (bool(pull.get("merged_at")), "the pull request is not merged"),
        (pull.get("merge_commit_sha") == sha, "its merge commit is not this commit"),
        ((pull.get("user") or {}).get("login") == APP_LOGIN, f"its author is not {APP_LOGIN}"),
        (base.get("ref") == default_branch, f"its base is not {default_branch}"),
        (((head.get("repo") or {}).get("full_name")) == repo, f"its head is not in {repo}"),
        (ref.startswith(_BRANCH) and bool(_VERSION.fullmatch(version)), f"head ref {ref!r}"),
        (version == planned.get("version"), f"{version!r} is not the planned version"),
    )
    for ok, why in tests:
        if not ok:
            return why
    tree = _tree_version(api, repo, sha)
    if tree is not False and tree != version:
        return f"pyproject.toml at {sha[:7]} is at {tree!r}, not {version}"
    return version, pull


def publish_release(
    site: Site, token: str, planned: Mapping[str, Any], sha: str, default_branch: str
) -> tuple[int, dict[str, Any] | None]:
    """Verify a release merge from the API, then tag it and publish the GitHub Release.

    `planned` is `check`'s plan, which only decides that this job runs. Every
    test is re-derived here, and any failure refuses. Tagging is Ashigaru's own
    `release_publish.publish`, which also checks the CHANGELOG at `sha`, skips
    a tag that exists, and runs `gh release create`.

    Repo topics are not consulted, on purpose: Ashigaru's `released()` would
    skip a repo its sweep is not configured for, but a release PR the App
    opened and a person merged is an explicit request.
    """
    repo = site.repo
    verdict = _verify(site.api_for(token), repo, sha, planned, default_branch)
    if isinstance(verdict, str):
        return _error(f"{repo}@{sha[:7]} is not a release merge: {verdict}"), None
    version, pull = verdict
    if publish(site, token, version, sha):
        return 1, None
    release = {
        "version": version,
        "sha": sha,
        "pr": pull.get("number"),
        "publish": planned.get("publish") or "",
    }
    return 0, release


def publish(site: Site, token: str, version: str, sha: str) -> int:
    """Tag `sha` as v`version` and publish its GitHub Release, as Ashigaru does.

    Only the caller decides that `sha` is that release: `publish_release` from
    the merged release pull request, `_hotfix` from the tree scripts/hotfix.sh
    just signed.
    """
    done = site.runner(
        ["python3", "-I", "-c", _PUBLISH, str(tools.ASHIGARU_SCRIPTS), site.repo, version, sha],
        env=tools.child_env(site.env, GH_TOKEN=token),
        timeout=tools.GIT_SECONDS,
    )
    if tools.verdict(done, "release_publish"):
        return _error(f"{site.repo} v{version} was not published: {done.stdout.strip()[-300:]}")
    return 0


def audit_issue(site: Site, audit: Mapping[str, Any], token: str, run_url: str) -> int:
    """Keep the weekly audit's one alert issue in step with the scan, as the App.

    `audit` is the pip-audit JSON report `check` hands on. The vendored
    `_audit_alert` decides what to create, rewrite or close; this runs it with
    the App's token and teaches it that the App's issues are its own.
    """
    with tempfile.TemporaryDirectory(prefix="sdlc-audit-") as scratch:
        report = Path(scratch) / "audit.json"
        report.write_text(json.dumps(audit), encoding="utf-8")
        done = site.runner(
            [
                "python3",
                "-I",
                "-c",
                _AUDIT,
                str(PACKAGE_ROOT),
                "--json",
                str(report),
                "--event",
                "schedule",
                "--repo",
                site.repo,
                "--run-url",
                run_url,
            ],
            env=tools.child_env(site.env, GH_TOKEN=token),
            timeout=tools.GIT_SECONDS,
        )
    return tools.verdict(done, "audit_alert")
