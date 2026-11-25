#!/usr/bin/env python3
"""scripts/hotfix.sh's tag: sdlc_jobs._hotfix asks the Ashigaru App for vX.Y.Z.

Tag ruleset 27841014 lets only the App create v*.*.* tags, so the hotfix path
must mint the App's token, with `write`'s TAG permissions and for this repo
alone, and run Ashigaru's release_publish with that token and nothing else. A
tag that is already at the commit is a re-run; one at another commit refuses.
"""

from __future__ import annotations

import contextlib
import io
import sys
from typing import Any

import sdlc_root
from fake_github import FakeGitHub, FakeRunner, check, finish

from sdlc_jobs import _hotfix
from sdlc_jobs import _release as launchers
from sdlc_jobs import _tools as tools
from sdlc_jobs._github import CANNOT_RUN, CannotRunError
from sdlc_jobs._tools import Done, Site
from sdlc_jobs.write import TAG

tools.ASHIGARU_SCRIPTS = sdlc_root.ROOT / "ashigaru-scripts"

REPO, VERSION, SHA, OTHER = "crunchtools/sdlc", "1.2.3", "a" * 40, "b" * 40
INSTALL = "ghs_installtok"
ENV = {
    "PATH": "/bin",
    "ASHIGARU_CLIENT_ID": "Iv1.abc",
    "ASHIGARU_APP_KEY": "PEM-KEY",
    "SDLC_KEY": "SIGNING-KEY",
}
REF = f"/repos/{REPO}/git/ref/tags/v{VERSION}"


class Rig:
    """The App's API (its JWT) apart from the installation token's."""

    def __init__(self, ref: Any = None, tag_object: Any = None) -> None:
        self.app = FakeGitHub(
            {
                ("GET", f"/repos/{REPO}/installation"): {"id": 42},
                ("POST", "/app/installations/42/access_tokens"): {"token": INSTALL},
            }
        )
        routes: dict = {("DELETE", "/installation/token"): None}
        if ref is not None:
            routes[("GET", REF)] = ref
        if tag_object is not None:
            routes[("GET", f"/repos/{REPO}/git/tags/*")] = tag_object
        self.install = FakeGitHub(routes)
        self.tokens: list[str] = []

    def api_for(self, token: str) -> FakeGitHub:
        self.tokens.append(token)
        return self.install if token == INSTALL else self.app


def run(rig: Rig, runner: FakeRunner, version: str = VERSION, sha: str = SHA) -> int:
    site = Site(REPO, ENV, runner, sdlc_root.ROOT, api_for=rig.api_for)
    with contextlib.redirect_stdout(io.StringIO()):
        return _hotfix.tag(site, version, sha, signer=lambda *_: "the.jwt")


def publish_calls(runner: FakeRunner) -> list:
    return [c for c in runner.calls if launchers._PUBLISH in c[0]]


# No tag yet: the App mints TAG for this repo alone, and release_publish runs
# with the installation token as GH_TOKEN and none of the host's secrets.
rig, runner = Rig(), FakeRunner()
got = run(rig, runner)
minted = rig.app.made("POST", "/app/installations/42/access_tokens")
check("tag: no tag yet is 0", got == 0, str(got))
check(
    "tag: the App's token holds TAG for this repo alone",
    len(minted) == 1 and minted[0][2] == {"repositories": ["sdlc"], "permissions": TAG},
    str(minted),
)
check("tag: TAG is contents: write", TAG.get("contents") == "write", str(TAG))
check("tag: the existing tag is read with the App's token", rig.install.made("GET", REF) != [])
calls = publish_calls(runner)
check(
    "tag: release_publish runs for the repo, version and sha",
    len(calls) == 1 and calls[0][0][-3:] == [REPO, VERSION, SHA],
    str(runner.calls),
)
child = calls[0][1] if calls else {}
check("tag: release_publish's GH_TOKEN is the App's", child.get("GH_TOKEN") == INSTALL)
check(
    "tag: release_publish holds neither the App's key nor sdlc's",
    "ASHIGARU_APP_KEY" not in child and "SDLC_KEY" not in child,
    str(sorted(child)),
)
check("tag: the App's token is revoked", rig.install.made("DELETE", "/installation/token") != [])

# A re-run after a failed sign: the tag is already this commit's.
rig, runner = Rig(ref={"object": {"type": "commit", "sha": SHA}}), FakeRunner()
check("tag: already at the sha is 0", run(rig, runner) == 0)
check("tag: already at the sha publishes nothing", publish_calls(runner) == [])

# An annotated tag is followed to its commit.
rig = Rig(
    ref={"object": {"type": "tag", "sha": "c" * 40}},
    tag_object={"object": {"type": "commit", "sha": SHA}},
)
runner = FakeRunner()
check("tag: an annotated tag at the sha is 0", run(rig, runner) == 0)
check("tag: an annotated tag at the sha publishes nothing", publish_calls(runner) == [])

# The version already names another tree: a refusal, never a skip.
rig, runner = Rig(ref={"object": {"type": "commit", "sha": OTHER}}), FakeRunner()
check("tag: at another sha refuses", run(rig, runner) == 1)
check("tag: at another sha publishes nothing", publish_calls(runner) == [])

# release_publish's own refusal (a CHANGELOG not at the version) is a 1.
rig = Rig()
runner = FakeRunner([(["release_publish"], Done(1, "CHANGELOG.md is at 1.2.2", ""))])
check("tag: release_publish's refusal is 1", run(rig, runner) == 1)

# Malformed input never reaches the App.
for version, sha, label in (("1.2", SHA, "version"), (VERSION, "abc", "sha")):
    rig = Rig()
    try:
        run(rig, FakeRunner(), version, sha)
        refused = False
    except CannotRunError:
        refused = True
    check(f"tag: a malformed {label} is CannotRunError", refused)
    check(f"tag: a malformed {label} mints nothing", rig.tokens == [])

# Without the App's credentials the CLI could not run: 2, not a refusal.
with contextlib.redirect_stderr(io.StringIO()) as err:
    code = _hotfix.main([REPO, VERSION, SHA], {"PATH": "/bin"})
check("main: no App credentials is 2", code == CANNOT_RUN, str(code))
check("main: names the missing credentials", "ASHIGARU_CLIENT_ID" in err.getvalue())
with contextlib.redirect_stderr(io.StringIO()):
    check("main: wrong arity is 2", _hotfix.main([REPO], ENV) == CANNOT_RUN)

if __name__ == "__main__":
    sys.exit(finish())
