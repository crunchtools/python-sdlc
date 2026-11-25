#!/usr/bin/env python3
"""scripts/hotfix.sh's tag: the Ashigaru App's TAG token, and sdlc_jobs._hotfix with it.

Tag ruleset 27841014 lets only the App create v*.*.* tags, so the hotfix path
must ask the App: hotfix.sh mints `write`'s TAG token for this repo alone
(tests/test_hotfix.sh drives that), and _hotfix runs Ashigaru's
release_publish with that token and nothing else. A tag already at the commit
is a re-run; one at another commit refuses.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
from typing import Any

import sdlc_root
from fake_github import FakeGitHub, FakeRunner, check, finish

from sdlc_jobs import _hotfix
from sdlc_jobs import _release as launchers
from sdlc_jobs import _tools as tools
from sdlc_jobs._github import CANNOT_RUN, ApiError, CannotRunError
from sdlc_jobs._tools import Done, Site
from sdlc_jobs.write import TAG

tools.ASHIGARU_SCRIPTS = sdlc_root.ROOT / "ashigaru-scripts"

REPO, VERSION, SHA, OTHER = "crunchtools/sdlc", "1.2.3", "a" * 40, "b" * 40
TOKEN, SERVER_ERROR = "ghs_installtok", 500
ENV = {"PATH": "/bin", "ASHIGARU_APP_KEY": "PEM-KEY", "SDLC_KEY": "SIGNING-KEY"}
REF = f"/repos/{REPO}/git/ref/tags/v{VERSION}"

# hotfix.sh asks the App for exactly `write`'s TAG, for sdlc alone.
hotfix = (sdlc_root.ROOT / "scripts" / "hotfix.sh").read_text()
body = re.search(r"^TAG_BODY='(.*)'$", hotfix, re.MULTILINE)
asked = json.loads(body.group(1)) if body else {}
check("hotfix.sh: the token holds write's TAG", asked.get("permissions") == TAG, str(asked))
check("hotfix.sh: the token is for sdlc alone", asked.get("repositories") == ["sdlc"], str(asked))
check("hotfix.sh: TAG is contents: write", TAG.get("contents") == "write", str(TAG))


def gh(ref: Any = None, tag_object: Any = None) -> FakeGitHub:
    routes: dict = {}
    if ref is not None:
        routes[("GET", REF)] = ref
    if tag_object is not None:
        routes[("GET", f"/repos/{REPO}/git/tags/*")] = tag_object
    return FakeGitHub(routes)


def run(api: FakeGitHub, runner: FakeRunner, version: str = VERSION, sha: str = SHA) -> int:
    tokens: list[str] = []

    def api_for(token: str) -> FakeGitHub:
        tokens.append(token)
        return api

    site = Site(REPO, ENV, runner, sdlc_root.ROOT, api_for=api_for)
    with contextlib.redirect_stdout(io.StringIO()):
        code = _hotfix.tag(site, TOKEN, version, sha)
    check(f"tag: every call holds the App's token ({version} {sha[:4]})", set(tokens) <= {TOKEN})
    return code


def publish_calls(runner: FakeRunner) -> list:
    return [c for c in runner.calls if launchers._PUBLISH in c[0]]


# No tag yet: release_publish runs with the App's token as GH_TOKEN and none
# of the host's secrets.
api, runner = gh(), FakeRunner()
check("tag: no tag yet is 0", run(api, runner) == 0)
check("tag: the existing tag is looked up", api.made("GET", REF) != [])
calls = publish_calls(runner)
check(
    "tag: release_publish runs for the repo, version and sha",
    len(calls) == 1 and calls[0][0][-3:] == [REPO, VERSION, SHA],
    str(runner.calls),
)
child = calls[0][1] if calls else {}
check("tag: release_publish's GH_TOKEN is the App's", child.get("GH_TOKEN") == TOKEN)
check(
    "tag: release_publish holds neither the App's key nor sdlc's",
    "ASHIGARU_APP_KEY" not in child and "SDLC_KEY" not in child,
    str(sorted(child)),
)

# A re-run after a failed sign: the tag is already this commit's.
api, runner = gh(ref={"object": {"type": "commit", "sha": SHA}}), FakeRunner()
check("tag: already at the sha is 0", run(api, runner) == 0)
check("tag: already at the sha publishes nothing", publish_calls(runner) == [])

# An annotated tag is followed to its commit.
api = gh(
    ref={"object": {"type": "tag", "sha": "c" * 40}},
    tag_object={"object": {"type": "commit", "sha": SHA}},
)
runner = FakeRunner()
check("tag: an annotated tag at the sha is 0", run(api, runner) == 0)
check("tag: an annotated tag at the sha publishes nothing", publish_calls(runner) == [])

# The version already names another tree: a refusal, never a skip.
api, runner = gh(ref={"object": {"type": "commit", "sha": OTHER}}), FakeRunner()
check("tag: at another sha refuses", run(api, runner) == 1)
check("tag: at another sha publishes nothing", publish_calls(runner) == [])

# Only a 404 means "no tag": any other answer could not run, and tags nothing.
api, runner = gh(ref=ApiError("GET", REF, SERVER_ERROR, "boom")), FakeRunner()
try:
    run(api, runner)
    raised = False
except ApiError as failure:
    raised = failure.status == SERVER_ERROR
check("tag: a non-404 lookup failure propagates", raised)
check("tag: a non-404 lookup failure publishes nothing", publish_calls(runner) == [])

# release_publish's own refusal (a CHANGELOG not at the version) is a 1.
runner = FakeRunner([(["release_publish"], Done(1, "CHANGELOG.md is at 1.2.2", ""))])
check("tag: release_publish's refusal is 1", run(gh(), runner) == 1)

# Malformed input never reaches GitHub.
for version, sha, label in ((VERSION[:3], SHA, "version"), (VERSION, "abc", "sha")):
    api = gh()
    try:
        run(api, FakeRunner(), version, sha)
        refused = False
    except CannotRunError:
        refused = True
    check(f"tag: a malformed {label} is CannotRunError", refused)
    check(f"tag: a malformed {label} calls nothing", api.calls == [])

# The CLI: no token on stdin, or the wrong arity, could not run.
with contextlib.redirect_stderr(io.StringIO()) as err:
    code = _hotfix.main([REPO, VERSION, SHA], {"PATH": "/bin"}, "")
check("main: no token is 2", code == CANNOT_RUN, str(code))
check("main: says the token is missing", "no App token" in err.getvalue(), err.getvalue())
with contextlib.redirect_stderr(io.StringIO()):
    check("main: wrong arity is 2", _hotfix.main([REPO], ENV, TOKEN) == CANNOT_RUN)

if __name__ == "__main__":
    sys.exit(finish())
