"""scripts/hotfix.sh's tag: vX.Y.Z made by the Ashigaru App, as `write` makes a release's.

Tag ruleset 27841014 lets only the App create v*.*.* tags, so a hotfix cut
from a host pushes none of its own. hotfix.sh runs this inside the image it
just built, after the image step and before sign moves :<major>, with the App's
key on the environment and nothing else of its secrets. The token is `write`'s
TAG token, and the tag and GitHub Release are Ashigaru's `release_publish`,
which checks the CHANGELOG at the commit, so a hotfix is tagged as a release
merge is.

`release_publish` skips a tag that exists without asking where it points. A
re-run after a failed sign finds its own tag and goes on; a tag at another
commit is a refusal, never a skip, so one version never names two trees.

Usage: python3 -m sdlc_jobs._hotfix <owner/repo> <X.Y.Z> <40-hex sha>
Exits 0 tagged (or already tagged here), 1 refused, 2 could not run.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from sdlc_jobs import _release
from sdlc_jobs import _tools as tools
from sdlc_jobs._app_token import app_jwt, app_token
from sdlc_jobs._github import CANNOT_RUN, FOUND, OK, Api, ApiError, CannotRunError
from sdlc_jobs._tools import Site
from sdlc_jobs.write import TAG

_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_SHA = re.compile(r"[0-9a-f]{40}")
_NOT_FOUND = 404
_USAGE = "usage: python3 -m sdlc_jobs._hotfix <owner/repo> <X.Y.Z> <sha>"


def tagged_at(api: Api, repo: str, version: str) -> str | None:
    """The commit v`version` names, or None when there is no such tag."""
    try:
        ref = api.request("GET", f"/repos/{repo}/git/ref/tags/v{version}")
    except ApiError as err:
        if err.status == _NOT_FOUND:
            return None
        raise
    obj = ref["object"]
    # An annotated tag names a tag object, which names the commit.
    if obj["type"] == "tag":
        obj = api.request("GET", f"/repos/{repo}/git/tags/{obj['sha']}")["object"]
    return str(obj["sha"])


def tag(
    site: Site, version: str, sha: str, *, signer: Callable[[str, str, float], str] = app_jwt
) -> int:
    """Tag `sha` as v`version` with the App's token, unless it already is."""
    if not _VERSION.fullmatch(version) or not _SHA.fullmatch(sha):
        raise CannotRunError(f"hotfix: '{version}' '{sha}' is not X.Y.Z and a full commit sha")
    with app_token(site.env, site.repo, TAG, api_for=site.api_for, signer=signer) as token:
        at = tagged_at(site.api_for(token), site.repo, version)
        if at == sha:
            print(f"hotfix: {site.repo} v{version} is already at {sha[:7]}")
            return OK
        if at is not None:
            print(f"::error::{site.repo} v{version} is at {at[:7]}, not {sha[:7]}; refusing")
            return FOUND
        return _release.publish(site, token, version, sha)


def main(argv: list[str], env: Mapping[str, str]) -> int:
    try:
        repo, version, sha = argv
    except ValueError:
        print(_USAGE, file=sys.stderr)
        return CANNOT_RUN
    with tempfile.TemporaryDirectory(prefix="sdlc-hotfix-") as scratch:
        try:
            return tag(Site(repo, env, tools.run, Path(scratch)), version, sha)
        except CannotRunError as err:
            print(f"::error::{err}", file=sys.stderr)
            return CANNOT_RUN


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
