#!/usr/bin/env python3
"""Open a version-bump pull request in every repo that pins python-sdlc.

Run after a release is promoted, so the image a bump points at exists. Each
consuming repo gets one PR moving its `rev:`, queued for auto-merge, and its own
CI decides whether it lands -- a bad release fails per repo, not fleet-wide.
The same shape as crunchtools/constitution's scripts/fleet-bump.py: clone,
rewrite, push, open, queue, and name the repos that failed so they can be rerun.

ONE BRANCH PER REPO, not one per version, and reused in place. Per-version
branches look tidier and are a trap: two releases close together open two PRs
against the same `rev:` line, and when the OLDER one merges it moves that line
out from under the newer branch, which goes CONFLICTING. v0.4.0 and v0.5.0 were
two minutes apart and left 10 repos exactly there.

What makes that bad rather than merely untidy is the shape of the failure: a
conflicting pull request runs no `pull_request` workflows AT ALL, so its
required check is ABSENT rather than red. Auto-merge never fires, nothing
reports a failure, and the PR sits looking green forever. So this also asks
GitHub whether each pull request can actually merge, and a run that leaves one
unmergeable FAILS -- the same reason this script already refuses to finish
quietly (RT#1553, below).

Usage: scripts/bump_consumers.py vX.Y.Z [--only repo,repo] [--dry-run]
Needs GH_TOKEN able to push branches and open PRs across the org.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ORG = "crunchtools"
SELF = "python-sdlc"
# One per repo, reused and force-updated each release. See the module docstring.
BRANCH = f"chore/{SELF}"
PIN = re.compile(
    r"(-\s*repo:\s*https://github\.com/crunchtools/python-sdlc(?:\.git)?\s*\n\s*rev:\s*)(v\d+\.\d+\.\d+)"
)


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def org_repos() -> list[str]:
    out = gh(
        "api",
        "--paginate",
        f"orgs/{ORG}/repos?per_page=100",
        "--jq",
        ".[] | select(.archived | not) | .name",
    )
    return sorted(name for name in out.split() if name != SELF)


def pinned_rev(repo: str) -> str | None:
    """The python-sdlc rev a repo pins, or None if it does not pin one."""
    proc = subprocess.run(
        ["gh", "api", f"repos/{ORG}/{repo}/contents/.pre-commit-config.yaml"],
        capture_output=True,
        text=True,
        check=False,
    )
    # Only a missing file means "does not pin". A rate limit, an auth failure
    # or a timeout must fail the run, or the repo is silently skipped and the
    # run reports success without having bumped it.
    if proc.returncode != 0:
        if "HTTP 404" in proc.stderr:
            return None
        raise RuntimeError(f"cannot read {repo}'s .pre-commit-config.yaml: {proc.stderr.strip()}")
    raw = proc.stdout
    text = base64.b64decode(json.loads(raw)["content"]).decode("utf-8", "replace")
    match = PIN.search(text)
    return match.group(2) if match else None


def open_bump(repo: str) -> str | None:
    """The bump pull request already open on this repo's branch, or None."""
    return (
        gh(
            "pr",
            "list",
            "-R",
            f"{ORG}/{repo}",
            "--head",
            BRANCH,
            "--state",
            "open",
            "--json",
            "url",
            "--jq",
            ".[0].url // empty",
        ).strip()
        or None
    )


def can_merge(repo: str, url: str) -> str:
    """MERGEABLE, CONFLICTING, or UNKNOWN if GitHub will not say in time.

    GitHub computes this asynchronously after a push, so the first answer is
    almost always UNKNOWN. UNKNOWN is NOT treated as a failure: a release must
    not go red because the API was slow. CONFLICTING is, because nothing else
    will ever report it -- see the module docstring.
    """
    for attempt in range(6):
        if attempt:
            time.sleep(5)
        state = gh(
            "pr", "view", url, "-R", f"{ORG}/{repo}", "--json", "mergeable", "--jq", ".mergeable"
        ).strip()
        if state and state != "UNKNOWN":
            return state
    return "UNKNOWN"


def bump(repo: str, version: str) -> str:
    branch = BRANCH
    with tempfile.TemporaryDirectory() as workdir:
        root = Path(workdir) / repo
        gh("repo", "clone", f"{ORG}/{repo}", str(root), "--", "--depth", "1", "-q")
        config = root / ".pre-commit-config.yaml"
        text, n = PIN.subn(rf"\g<1>{version}", config.read_text(encoding="utf-8"))
        if n != 1:
            raise RuntimeError(f"expected one python-sdlc pin, found {n}")
        config.write_text(text, encoding="utf-8")

        def git(*a: str) -> None:
            subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)

        # -B, not -b: the branch is this script's own and may already exist from
        # an earlier release. The clone is the CURRENT default branch, so the
        # rewrite is always against what the repo pins right now.
        git("checkout", "-q", "-B", branch)
        git(
            "-c",
            "user.name=crunchtools-ci",
            "-c",
            "user.email=ci@crunchtools.com",
            "commit",
            "-qam",
            f"chore: python-sdlc {version}",
        )
        # Forced, because this branch carries one commit that is rewritten each
        # release rather than a history worth keeping.
        git("push", "-q", "-u", "--force", "origin", branch)

        title = f"chore: python-sdlc {version}"
        body = (
            f"Moves the python-sdlc pin to {version}. Its own CI decides whether "
            f"it lands; it merges itself when green.\n\n"
            f"One branch per repo, reused each release, so two releases close "
            f"together cannot leave two pull requests fighting over the same line."
        )
        url = open_bump(repo)
        if url:
            # Reused rather than opened again. The force-push above has already
            # moved it to this version; this only makes it say so.
            gh("pr", "edit", url, "-R", f"{ORG}/{repo}", "--title", title, "--body", body)
            print(f"  updated in place: {url}")
        else:
            url = gh(
                "pr",
                "create",
                "-R",
                f"{ORG}/{repo}",
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ).strip()
        try:
            gh("pr", "merge", url, "--auto", "--merge")
        except subprocess.CalledProcessError as exc:
            reason = exc.stderr.strip()
            print(f"  warning: {repo}: auto-merge not queued: {reason}", file=sys.stderr)

        state = can_merge(repo, url)
        if state == "CONFLICTING":
            raise RuntimeError(
                f"{url} cannot merge. Nothing else will say so: a conflicting "
                f"pull request runs no workflows, so its required check is absent "
                f"rather than failing and auto-merge never fires"
            )
        return url


def survey(repos: list[str], version: str) -> tuple[list[str], list[str], list[str]]:
    """Split repos into those pinning `version`, those behind it, and unreadable ones."""
    current, stale, failed = [], [], []
    for repo in repos:
        try:
            rev = pinned_rev(repo)
        except RuntimeError as read_error:
            print(f"{repo}: {read_error}", file=sys.stderr)
            failed.append(repo)
            continue
        if rev is None:
            continue
        if rev == version:
            current.append(repo)
            continue
        print(f"{repo}: {rev} -> {version}")
        stale.append(repo)
    return current, stale, failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("version")
    ap.add_argument("--only", help="comma-separated repo names")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not re.fullmatch(r"v\d+\.\d+\.\d+", args.version):
        print(f"not a release tag: {args.version}", file=sys.stderr)
        return 2

    repos = args.only.split(",") if args.only else org_repos()
    current, stale, failed = survey(repos, args.version)
    opened = []
    if not args.dry_run:
        for repo in stale:
            try:
                print(f"  {bump(repo, args.version)}")
                opened.append(repo)
            except (subprocess.CalledProcessError, RuntimeError) as bump_error:
                detail = getattr(bump_error, "stderr", "") or ""
                print(f"  FAILED: {bump_error}: {detail}".rstrip(": "), file=sys.stderr)
                failed.append(repo)

    # Always say what happened. A run with nothing to do and a run that could
    # not open a pull request both used to print nothing at all, which is how
    # RT#1553 -- the App had no `pull_requests: write` -- survived two releases
    # looking like a job that had half worked.
    did = f"{len(stale)} would be bumped" if args.dry_run else f"{len(opened)} bumped"
    print(
        f"{len(current) + len(stale)} repo(s) pin python-sdlc: "
        f"{did} to {args.version}, {len(current)} already there, {len(failed)} failed"
    )
    if not current and not stale and not failed:
        print("no repo pins python-sdlc; nothing was bumped", file=sys.stderr)
    if failed:
        print(f"rerun with --only {','.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
