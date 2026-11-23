"""Releases from the `write` job: open the PR, verify the merge, tag it, keep the audit issue.

`write` holds the Ashigaru App's token and never runs a model. A release is the
one thing it does that outlives the run, so nothing here trusts what `check`
planned: the plan decides which jobs are needed, the API decides whether the
release is real. Tagging and the GitHub Release stay Ashigaru's own code
(`release_publish.publish`) and the audit issue stays the constitution's
`audit_alert`; this module only decides when they may run and with what.

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
from pathlib import Path
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
    import audit_alert
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
    from it is left alone.
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
    try:
        api.request("GET", f"/repos/{repo}/git/ref/heads/{branch}")
    except ApiError as err:
        if err.status != _NOT_FOUND:
            raise
        api.request("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})
    else:
        api.request("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {"sha": sha, "force": True})
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


def _tree_version(api: Api, repo: str, sha: str) -> str | bool | None:
    """The `[project].version` of pyproject.toml at `sha`; False when there is no such file."""
    try:
        got = api.request("GET", f"/repos/{repo}/contents/pyproject.toml?ref={sha}")
    except ApiError as err:
        if err.status == _NOT_FOUND:
            return False
        raise
    try:
        text = base64.b64decode(got["content"]).decode()
        found = tomllib.loads(text).get("project", {}).get("version")
    except _UNREADABLE:
        return None
    return found if isinstance(found, str) else None


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
    done = site.runner(
        ["python3", "-I", "-c", _PUBLISH, str(tools.ASHIGARU_SCRIPTS), repo, version, sha],
        env=tools.child_env(site.env, GH_TOKEN=token),
        timeout=tools.GIT_SECONDS,
    )
    if tools.verdict(done, "release_publish"):
        return _error(f"{repo} v{version} was not published: {done.stdout.strip()[-300:]}"), None
    release = {
        "version": version,
        "sha": sha,
        "pr": pull.get("number"),
        "publish": planned.get("publish") or "",
    }
    return 0, release


def audit_issue(site: Site, audit: Mapping[str, Any], token: str, run_url: str) -> int:
    """Keep the weekly audit's one alert issue in step with the scan, as the App.

    `audit` is the pip-audit JSON report `check` hands on. The constitution's
    `audit_alert` decides what to create, rewrite or close; this runs it with
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
                str(tools.CONSTITUTION_SCRIPTS),
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
