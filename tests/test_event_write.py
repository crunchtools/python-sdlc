#!/usr/bin/env python3
"""sdlc_jobs/write.py, against a fake GitHub, a fake runner and a fake App token.

`write` holds the App key and runs no model, so what `ai` hands it is data:

  - a rollback dispatch (DIGEST) returns before it reads anything else
  - a result for another pull request or another head is refused, and a
    review is posted only as a COMMENT or REQUEST_CHANGES on this head
  - a fix round is applied only to the head it was made for, with this run's
    pending finding ids swapped for the real comment ids (or dropped)
  - issues apply triage, and publish code only when `ai` succeeded
  - dispatch, push and schedule do what `check` planned, and nothing else
  - a release is tagged only once the image and boot it planned passed

Every token is minted by a fake that records the permissions asked for and
that it was released; nothing here touches the network or runs a tool.

Run: python3 tests/test_event_write.py
"""

from __future__ import annotations

import atexit
import base64
import contextlib
import io
import json
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

import sdlc_root
from fake_github import FakeGitHub, FakeRunner, check, finish

from sdlc_jobs import _handoff, write
from sdlc_jobs import _tools as tools
from sdlc_jobs._github import CannotRunError, contract
from sdlc_jobs._tools import Done, Site

REPO = "o/r"
HEAD, MOVED = "a" * 40, "d" * 40
BRANCH = "ashigaru/issue-7"
JOB_ENV = {
    "ASHIGARU_APP_KEY": "PEM-KEY",
    "ASHIGARU_CLIENT_ID": "cid",
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok",
    "OPENROUTER_API_KEY": "or-key",
    "GITHUB_TOKEN": "read-tok",
    "PATH": "/p",
    "HOME": "/h",
}
TMP = Path(tempfile.mkdtemp(prefix="sdlc-write-test-"))
atexit.register(shutil.rmtree, TMP, ignore_errors=True)
# The scripts are only named on argv here, never run; keep the path off /opt.
mock.patch.object(tools, "ASHIGARU_SCRIPTS", sdlc_root.ROOT / "ashigaru-scripts").start()
INLINE = [
    {"path": "a.py", "line": 3, "body": "b1", "side": "RIGHT", "extra": "dropped"},
    {"path": "b.py", "line": 4, "body": "b2"},
]
POST = {"event": "COMMENT", "commit_id": HEAD, "body": "Gatehouse review", "comments": INLINE}
FIX = {
    "action": "fix",
    "branch": BRANCH,
    "findings": {"round": 2, "findings": [{"id": 11}, {"id": -1}, {"id": -2}]},
    "result": {"dispositions": [{"id": -1, "d": "fixed"}, {"id": -2, "d": "fixed"}, {"id": 11}]},
    "patch": "PATCH TEXT",
}


def quiet(fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kw)


def refusal(fn: Callable[..., Any], *args: Any) -> str | None:
    try:
        quiet(fn, *args)
    except CannotRunError as err:
        return str(err)
    return None


def arg_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


class Minter:
    """A token factory: records the permissions asked for and each release."""

    def __init__(self) -> None:
        self.perms: list[dict[str, str]] = []
        self.released = 0

    def __call__(self, perms: dict[str, str]) -> Minter:
        self.perms.append(dict(perms))
        return self

    def __enter__(self) -> str:
        return "tok"

    def __exit__(self, *_exc: object) -> None:
        self.released += 1


class Rig:
    """One scenario: GitHub's answers, the scripts' behaviour, and what was seen."""

    def __init__(self, routes: dict[tuple[str, str], Any] | None = None, **cfg: Any) -> None:
        self.cfg: dict[str, Any] = {
            "script_exit": 0,
            "ref_sha": HEAD,
            "outputs": "state=ok\n",
        } | cfg
        self.api = FakeGitHub(
            {
                ("POST", f"/repos/{REPO}/pulls/7/reviews"): {"id": 55},
                (
                    "GET",
                    f"/repos/{REPO}/pulls/7/reviews/55/comments",
                ): [
                    {"id": 901, "path": "a.py", "body": "b1"},
                    {"id": 902, "path": "elsewhere.py", "body": "b2"},
                ],
                ("GET", f"/repos/{REPO}/git/ref/heads/{BRANCH}"): lambda _b: {
                    "object": {"sha": self.cfg["ref_sha"]}
                },
                ("POST", f"/repos/{REPO}/issues/*/labels"): [],
                ("POST", f"/repos/{REPO}/issues/*/comments"): {},
            }
            | (routes or {})
        )
        self.minter = Minter()
        self.work = Path(tempfile.mkdtemp(dir=TMP))
        self.script: dict[str, Any] = {}
        self.runner = FakeRunner(
            [
                (["fix_publish.py"], self.record),
                (["triage_apply.py"], self.record),
                (["publish.py"], self.record),
            ]
        )

    def record(self, args: list[str], env: dict[str, str], cwd: Path | None, _stdin: Any) -> Done:
        name = Path(args[1]).name
        files = {}
        for flag in ("--findings", "--patch", "--result"):
            if flag in args and name != "triage_apply.py":
                files[flag] = Path(arg_after(args, flag)).read_text()
        self.script[name] = {"args": args, "env": env, "cwd": cwd, "files": files}
        reply = self.cfg["outputs"]
        with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            handle.write(reply)
        return Done(self.cfg["script_exit"], "", "script stderr")

    def job(self, env: dict[str, str]) -> write.Job:
        site = Site(REPO, env, self.runner, self.work, api_for=lambda _t: self.api)
        return write.Job(site, self.minter)

    def route(self, name: str, event: dict[str, Any], env: dict[str, str]) -> int:
        return quiet(self.job(env).route, name, event)

    def git(self, needle: str) -> list[tuple[list[str], dict[str, str], Path | None, str | None]]:
        return [c for c in self.runner.calls if needle in " ".join(c[0])]


def with_result(result: dict[str, Any] | None, ai: str = "success", **extra: str) -> dict[str, str]:
    env = dict(JOB_ENV)
    if result is not None:
        env.update(zip(_handoff.ENV_NAMES, _handoff.encode(result), strict=True))
    if ai:
        env["SDLC_AI"] = ai
    return env | extra


def pr_event(number: int = 7, ref: str = BRANCH, head: str = HEAD) -> dict[str, Any]:
    return {
        "pull_request": {
            "number": number,
            "head": {"sha": head, "ref": ref, "repo": {"full_name": "o/r"}},
        }
    }


def pr_result(**over: Any) -> dict[str, Any]:
    return {"v": 1, "kind": "pr", "number": 7, "head_sha": HEAD, "review": {"posts": [POST]}} | over


# --- DIGEST (sdlc's rollback) -----------------------------------------------


with (
    mock.patch.object(tools, "run", side_effect=AssertionError("rollback ran a process")),
    mock.patch.object(write, "app_token", side_effect=AssertionError("rollback minted a token")),
    mock.patch.object(write._release, "open_release_pr", side_effect=AssertionError("rollback")),
    mock.patch.object(write, "repo_of", side_effect=AssertionError("rollback read the repo")),
):
    code = quiet(write.main, "workflow_dispatch", {}, {"DIGEST": "sha256:abc", "VERSION": "1.2.3"})
check("DIGEST: write.main exits 0 with no repo, no token, no API, no subprocess", code == 0)

code = quiet(write.main, "workflow_dispatch", {}, {"VERSION": "1.2.3"})
check("no DIGEST: the same env without a repo is exit 2", code == 2)

# --- main, end to end -------------------------------------------------------
# The same entry without DIGEST: the event reaches its handler through the real
# Site and Job, with only the token minting and the release call faked.

opened: list[tuple[Any, ...]] = []
main_minter = Minter()


def record_open(*args: Any) -> int:
    opened.append(args)
    return 0


with (
    mock.patch.object(write, "app_token", lambda _env, _repo, perms: main_minter(perms)),
    mock.patch.object(write._release, "open_release_pr", record_open),
):
    code = quiet(
        write.main,
        "workflow_dispatch",
        {"repository": {"default_branch": "main"}},
        {"GITHUB_REPOSITORY": "o/r", "VERSION": "1.2.3"},
    )
check(
    "main: a version dispatch opens the release PR with a token for exactly that",
    code == 0
    and [a[1:] for a in opened] == [("o/r", "1.2.3", "main")]
    and main_minter.perms == [write.RELEASE_PR]
    and main_minter.released == 1,
)

# --- pull requests: posting what ai reviewed --------------------------------

rig = Rig()
code = rig.route("pull_request_target", pr_event(), with_result(pr_result()))
posted = rig.api.made("POST", f"/repos/{REPO}/pulls/7/reviews")
check("pr: exit 0", code == 0)
check("pr: the review is POSTed to the PR's reviews", len(posted) == 1)
check("pr: minted with the review permissions", rig.minter.perms == [write.REVIEW])
check("pr: and released", rig.minter.released == 1)
check(
    "pr: the body and commit_id are the head's",
    posted[0][2]["body"] == "Gatehouse review" and posted[0][2]["commit_id"] == HEAD,
)
check(
    "pr: only the known comment keys survive",
    posted[0][2]["comments"][0] == {"path": "a.py", "line": 3, "body": "b1", "side": "RIGHT"},
)

rig = Rig()
two = pr_result(review={"posts": [POST, {**POST, "body": "second", "comments": []}]})
rig.route("pull_request_target", pr_event(), with_result(two))
check(
    "pr: each post is its own review, in order",
    [c[2]["body"] for c in rig.api.made("POST", "*/reviews")] == ["Gatehouse review", "second"],
)

hostile = {
    "event": "APPROVE",
    "commit_id": "f" * 40,
    "body": 5,
    "unknown": "x",
    "comments": [
        {"path": "a", "body": "c", "line": 1, "evil": 1},
        {"path": "", "body": "no path"},
        {"path": "p"},
        "not a dict",
    ],
}
rig = Rig()
rig.route("pull_request_target", pr_event(), with_result(pr_result(review={"posts": [hostile]})))
sent = rig.api.made("POST", "*/reviews")[0][2]
check("clean: an APPROVE becomes a COMMENT", sent["event"] == "COMMENT")
check("clean: commit_id is forced to the event head", sent["commit_id"] == HEAD)
check("clean: unknown keys are dropped", set(sent) == {"event", "commit_id", "body", "comments"})
check("clean: the body is a string", sent["body"] == "5")
check(
    "clean: only complete comments, only their known keys",
    sent["comments"] == [{"path": "a", "line": 1, "body": "c"}],
)

check(
    "clean: REQUEST_CHANGES is kept",
    write.clean_review({"event": "REQUEST_CHANGES"}, HEAD)["event"] == "REQUEST_CHANGES",
)
check(
    "clean: COMMENT is kept", write.clean_review({"event": "COMMENT"}, HEAD)["event"] == "COMMENT"
)
check("clean: no event is a COMMENT", write.clean_review({}, HEAD)["event"] == "COMMENT")
check(
    "clean: DISMISS and PENDING are COMMENTs",
    all(
        write.clean_review({"event": e}, HEAD)["event"] == "COMMENT"
        for e in ("DISMISS", "PENDING", "approve")
    ),
)
check(
    "clean: no comments, no comments key",
    "comments" not in write.clean_review({"comments": [{"path": "x"}]}, HEAD),
)
check(
    "clean: a non-object review is CannotRunError",
    refusal(write.clean_review, "a string", HEAD) is not None,
)
check("clean: so is a list", refusal(write.clean_review, [1], HEAD) is not None)

rig = Rig()
env = with_result(pr_result(number=8))
why = refusal(rig.job(env).route, "pull_request_target", pr_event())
check(
    "pr: a result for another PR number is CannotRunError",
    why is not None and "#8" in why and "#7" in why,
)
check(
    "pr: and it is exit 2 under the contract",
    quiet(contract(rig.job(env).route), "pull_request_target", pr_event()) == 2,
)
check("pr: nothing is posted for it", rig.api.calls == [] and rig.minter.perms == [])
why = refusal(
    rig.job(with_result(pr_result(kind="issue"))).route, "pull_request_target", pr_event()
)
check("pr: a result of another kind is CannotRunError", why is not None and "issue" in why)
rig = Rig()
why = refusal(
    rig.job(with_result(pr_result(head_sha=MOVED))).route, "pull_request_target", pr_event()
)
check("pr: a result for another head is CannotRunError", why is not None and MOVED in why)
check("pr: and nothing is posted or minted", rig.api.calls == [] and rig.minter.perms == [])
why = refusal(rig.job(with_result(pr_result())).route, "pull_request_target", pr_event(head=MOVED))
check("pr: an event whose head moved on is CannotRunError too", why is not None)

rig = Rig()
code = rig.route("pull_request_target", pr_event(), with_result(None, ai="failure"))
check(
    "pr: ai failed with no outputs is exit 0, nothing posted",
    code == 0 and rig.api.calls == [] and rig.minter.perms == [],
)
rig = Rig()
code = rig.route("pull_request_target", pr_event(), with_result(None, ai=""))
check("pr: ai absent with no outputs is exit 0 too", code == 0 and rig.api.calls == [])
rig = Rig()
why = refusal(rig.job(with_result(None, ai="success")).route, "pull_request_target", pr_event())
check(
    "pr: ai succeeded with no outputs is CannotRunError",
    why is not None and "handed on nothing" in why,
)
check("pr: and nothing is posted", rig.api.calls == [])
rig = Rig()
garbage = {**JOB_ENV, "SDLC_AI": "success", "SDLC_AI_0": "!!!"}
check(
    "pr: ai succeeded with garbage outputs is CannotRunError",
    refusal(rig.job(garbage).route, "pull_request_target", pr_event()) is not None,
)

rig = Rig()
big = {"v": 1, "kind": "pr", "number": 7, "too_large": 500_000}
code = rig.route("pull_request_target", pr_event(), with_result(big, ai="failure"))
check("too large: returns 1", code == 1)
labels = rig.api.made("POST", f"/repos/{REPO}/issues/7/labels")
comments = rig.api.made("POST", f"/repos/{REPO}/issues/7/comments")
check(
    "too large: needs-human label", len(labels) == 1 and labels[0][2] == {"labels": ["needs-human"]}
)
check(
    "too large: a comment with the size", len(comments) == 1 and "500,000" in comments[0][2]["body"]
)
check("too large: no review is posted", rig.api.made("POST", "*/reviews") == [])
check("too large: needs no head match", rig.minter.released == 1)

# --- fix apply --------------------------------------------------------------

rig = Rig()
env = with_result(pr_result(fix=FIX))
code = rig.route("pull_request_target", pr_event(), env)
check("fix: exit 0", code == 0)
check("fix: the review is posted first", rig.api.made("POST", "*/reviews") != [])
check(
    "fix: the branch is checked for movement",
    len(rig.api.made("GET", f"/repos/{REPO}/git/ref/heads/{BRANCH}")) == 1,
)
check(
    "fix: the posted review's comments are fetched",
    len(rig.api.made("GET", "*/reviews/55/comments")) == 1,
)
run = rig.script["fix_publish.py"]
argv = run["args"]
check(
    "fix: fix_publish.py by path under the Ashigaru scripts",
    argv[:2] == ["python3", str(tools.ASHIGARU_SCRIPTS / "fix_publish.py")],
)
check(
    "fix: its args",
    (
        arg_after(argv, "--repo"),
        arg_after(argv, "--mode"),
        arg_after(argv, "--branch"),
        arg_after(argv, "--max-rounds"),
    )
    == (REPO, "fix", BRANCH, "5"),
)
check("fix: the patch is the model's", run["files"]["--patch"] == "PATCH TEXT")
sent_findings = json.loads(run["files"]["--findings"])
check(
    "fix: pending ids become the real comment ids",
    [f["id"] for f in sent_findings["findings"]] == [11, 901],
    str(sent_findings),
)
check(
    "fix: a comment that did not map drops that finding",
    all(f["id"] != -2 and f["id"] != 902 for f in sent_findings["findings"]),
)
dispositions = json.loads(run["files"]["--result"])["dispositions"]
check(
    "fix: dispositions follow the same swap",
    [d["id"] for d in dispositions] == [901, 11],
    str(dispositions),
)
check(
    "fix: and the dropped finding's disposition is gone",
    {"id": -2, "d": "fixed"} not in dispositions,
)
check("fix: GH_TOKEN is the App token", run["env"]["GH_TOKEN"] == "tok")
check(
    "fix: git auth is the extraheader with that token",
    base64.b64decode(run["env"]["GIT_CONFIG_VALUE_0"].removeprefix("AUTHORIZATION: basic "))
    == b"x-access-token:tok",
)
check(
    "fix: no job credential reaches the script",
    not {
        "ASHIGARU_APP_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENROUTER_API_KEY",
        "GITHUB_TOKEN",
        "ASHIGARU_CLIENT_ID",
    }
    & set(run["env"]),
)
check("fix: it runs in the clone of the PR branch", run["cwd"] == rig.work / "pr")
fetch = rig.git("git fetch")[0]
check(
    "fix: the clone fetches the head with the token in env only",
    fetch[0][-1] == HEAD
    and "GIT_CONFIG_VALUE_0" in fetch[1]
    and all(part != "tok" for c in rig.runner.calls for part in c[0]),
)
check("fix: and checks out the branch", rig.git("git checkout")[0][0][3:5] == ["-B", BRANCH])

rig = Rig(ref_sha=MOVED)
code = rig.route("pull_request_target", pr_event(), with_result(pr_result(fix=FIX)))
check(
    "fix: a moved branch applies nothing",
    code == 0 and rig.git("fix_publish.py") == [] and rig.git("git fetch") == [],
)
check("fix: but the review is still posted", len(rig.api.made("POST", "*/reviews")) == 1)
check("fix: and its comments are not fetched", rig.api.made("GET", "*/comments") == [])

rig = Rig()
rig.route("pull_request_target", pr_event(), with_result(pr_result(fix=FIX), ai="failure"))
check(
    "fix: ai not success applies no fix",
    rig.git("fix_publish.py") == [] and rig.api.made("GET", "*/git/ref/*") == [],
)
check("fix: but the review is still posted", len(rig.api.made("POST", "*/reviews")) == 1)

rig = Rig()
rig.route(
    "pull_request_target",
    pr_event(),
    with_result(pr_result(fix={"action": "none", "reason": "wait"})),
)
check(
    "fix: action none applies nothing",
    rig.git("fix_publish.py") == [] and rig.api.made("GET", "*/git/ref/*") == [],
)
rig = Rig()
rig.route("pull_request_target", pr_event(), with_result(pr_result(review={"posts": []})))
check(
    "fix: a result without a fix posts and applies nothing",
    rig.git("fix_publish.py") == [] and rig.api.made("POST", "*/reviews") == [],
)

rig = Rig()
rig.route("pull_request_target", pr_event(), with_result(pr_result(fix={**FIX, "action": "cap"})))
check(
    "fix: the cap action runs fix_publish --mode cap",
    arg_after(rig.script["fix_publish.py"]["args"], "--mode") == "cap",
)

rig = Rig()
why = refusal(
    rig.job(with_result(pr_result(fix={**FIX, "branch": "ashigaru/other"}))).route,
    "pull_request_target",
    pr_event(),
)
check(
    "fix: a fix for another branch is CannotRunError", why is not None and "ashigaru/other" in why
)
check("fix: and runs nothing", rig.git("fix_publish.py") == [])

rig = Rig()
fork_event = pr_event()
fork_event["pull_request"]["head"]["repo"] = {"full_name": "evil/r"}
why = refusal(rig.job(with_result(pr_result(fix=FIX))).route, "pull_request_target", fork_event)
check(
    "fix: a fix round for a branch in another repository is CannotRunError",
    why is not None and "evil/r" in why and rig.git("fix_publish.py") == [],
)

rig = Rig(script_exit=1)
why = refusal(rig.job(with_result(pr_result(fix=FIX))).route, "pull_request_target", pr_event())
check(
    "fix: fix_publish.py failing is CannotRunError",
    why is not None and "fix_publish.py failed" in why,
)
check("fix: and the token is still released", rig.minter.released == 1)

ids = quiet(
    write.real_ids,
    FakeGitHub(
        {
            ("GET", f"/repos/{REPO}/pulls/7/reviews/1/comments"): [
                {"id": 5, "path": "a", "body": "x"}
            ]
        }
    ),
    REPO,
    7,
    [(1, [{"path": "a", "body": "x"}, {"path": "b", "body": "y"}]), (2, [])],
)
check("real_ids: matches by position, path and body; the rest unmapped", ids == {-1: 5})
swapped, answer = write.resolve(
    {"findings": [{"id": -1}, {"id": -2}, {"id": "x"}, {}]},
    {"dispositions": [{"id": -2}, {"id": -1}]},
    {-1: 5},
)
check(
    "resolve: swaps mapped, drops unmapped, keeps others",
    swapped["findings"] == [{"id": 5}, {"id": "x"}, {}] and answer["dispositions"] == [{"id": 5}],
)

# --- issues -----------------------------------------------------------------

ISSUE = {"v": 1, "kind": "issue", "number": 9, "trust": "write"}
TRIAGE = {"verdict": {"category": "bug", "confidence": 0.9}, "queue": "fresh"}
CODE = {"result": {"summary": "s"}, "patch": "CODE PATCH", "base": "main"}


def issue_event() -> dict[str, Any]:
    return {"issue": {"number": 9}, "repository": {"default_branch": "main"}}


rig = Rig()
why = refusal(
    rig.job(with_result(ISSUE | {"code": CODE | {"base": "release-1.x"}})).route,
    "issues",
    issue_event(),
)
check(
    "issue code: a patch for a branch other than the default is CannotRunError",
    why is not None and "release-1.x" in why and rig.git("publish.py") == [],
)

rig = Rig()
code = rig.route("issues", issue_event(), with_result(ISSUE | {"triage": TRIAGE}, ai="success"))
check(
    "issue triage: exit 0, minted with the issue permissions",
    code == 0 and rig.minter.perms == [write.ISSUE] and rig.minter.released == 1,
)
tri = rig.script["triage_apply.py"]
targv = tri["args"]
check(
    "issue triage: repo and issue",
    (arg_after(targv, "--repo"), arg_after(targv, "--issue")) == (REPO, "9"),
)
check(
    "issue triage: --result is the verdict JSON",
    json.loads(arg_after(targv, "--result")) == TRIAGE["verdict"],
)
check(
    "issue triage: queue and trust",
    (arg_after(targv, "--queue"), arg_after(targv, "--trust")) == ("fresh", "write"),
)
check(
    "issue triage: GH_TOKEN is the App token, no git auth",
    tri["env"]["GH_TOKEN"] == "tok" and "GIT_CONFIG_VALUE_0" not in tri["env"],
)
check(
    "issue triage: no clone and no publish",
    rig.git("git fetch") == [] and rig.git("publish.py") == [],
)
check(
    "issue triage: no job credential reaches it",
    not {"ASHIGARU_APP_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENROUTER_API_KEY", "GITHUB_TOKEN"}
    & set(tri["env"]),
)

rig = Rig()
rig.route("issues", issue_event(), with_result({**ISSUE, "triage": {"verdict": {}}, "trust": ""}))
targv = rig.script["triage_apply.py"]["args"]
check(
    "issue triage: queue defaults to fresh, trust to empty",
    (arg_after(targv, "--queue"), arg_after(targv, "--trust")) == ("fresh", ""),
)

rig = Rig()
rig.route("issues", issue_event(), with_result(ISSUE | {"triage": TRIAGE, "code": CODE}))
check(
    "issue code: triage first, then publish.py",
    list(rig.script) == ["triage_apply.py", "publish.py"],
)
pub = rig.script["publish.py"]
pargv = pub["args"]
check(
    "issue code: args",
    (arg_after(pargv, "--repo"), arg_after(pargv, "--issue"), arg_after(pargv, "--base"))
    == (REPO, "9", "main"),
)
check(
    "issue code: the patch and result files hold what ai handed on",
    pub["files"]["--patch"] == "CODE PATCH"
    and json.loads(pub["files"]["--result"]) == {"summary": "s"},
)
check(
    "issue code: the token is GH_TOKEN and the git extraheader",
    pub["env"]["GH_TOKEN"] == "tok"
    and "x-access-token"
    in base64.b64decode(
        pub["env"]["GIT_CONFIG_VALUE_0"].removeprefix("AUTHORIZATION: basic ")
    ).decode(),
)
check("issue code: runs in the base clone", pub["cwd"] == rig.work / "repo")
fetch = rig.git("git fetch")[0]
check(
    "issue code: the base branch is cloned with the token in env only",
    fetch[0][-1] == "main"
    and "GIT_CONFIG_VALUE_0" in fetch[1]
    and rig.git("git checkout")[0][0][3:5] == ["-B", "main"],
)

rig = Rig()
rig.route(
    "issues", issue_event(), with_result(ISSUE | {"triage": TRIAGE, "code": CODE}, ai="failure")
)
check("issue code: ai not success publishes nothing", "publish.py" not in rig.script)
check("issue code: but triage is still applied", "triage_apply.py" in rig.script)

rig = Rig()
rig.route("issues", issue_event(), with_result(ISSUE | {"code": CODE}))
check(
    "issue code: code without triage (ready-to-code) is published",
    list(rig.script) == ["publish.py"],
)

for label, result in (
    ("a guard skip", ISSUE | {"skip": "guard refused triage"}),
    (
        "not enrolled",
        {"v": 1, "kind": "issue", "number": 9, "skip": "Ashigaru is not enrolled here"},
    ),
):
    rig = Rig()
    code = rig.route("issues", issue_event(), with_result(result))
    check(
        f"issue: {label} does nothing",
        code == 0 and rig.script == {} and rig.minter.perms == [] and rig.api.calls == [],
    )

rig = Rig()
code = rig.route("issues", issue_event(), with_result(ISSUE))
check("issue: a result with neither triage nor code runs nothing", code == 0 and rig.script == {})

rig = Rig()
code = rig.route(
    "issues", issue_event(), with_result({**ISSUE, "too_large": 700_000, "skip": "x"}, ai="failure")
)
check(
    "issue: too large labels and comments, returns 1",
    code == 1
    and len(rig.api.made("POST", f"/repos/{REPO}/issues/9/labels")) == 1
    and "700,000" in rig.api.made("POST", f"/repos/{REPO}/issues/9/comments")[0][2]["body"],
)

rig = Rig()
why = refusal(
    rig.job(with_result({**ISSUE, "number": 10, "triage": TRIAGE})).route, "issues", issue_event()
)
check(
    "issue: a result for another issue is CannotRunError",
    why is not None and "#10" in why and rig.script == {},
)
rig = Rig()
check(
    "issue: ai failed with no outputs is exit 0",
    rig.route("issues", issue_event(), with_result(None, ai="failure")) == 0 and rig.script == {},
)
rig = Rig()
check(
    "issue: ai succeeded with no outputs is CannotRunError",
    refusal(rig.job(with_result(None)).route, "issues", issue_event()) is not None,
)
rig = Rig(script_exit=1)
why = refusal(rig.job(with_result(ISSUE | {"triage": TRIAGE})).route, "issues", issue_event())
check(
    "issue: triage_apply.py failing is CannotRunError",
    why is not None and "triage_apply.py failed" in why,
)

# --- dispatch: the release pull request -------------------------------------

opener_patch = mock.patch.object(write._release, "open_release_pr", return_value=0)
mock_open = opener_patch.start()
dispatch_event = {"repository": {"default_branch": "trunk"}}

rig = Rig()
code = rig.route("workflow_dispatch", dispatch_event, {**JOB_ENV, "VERSION": "1.2.3"})
check(
    "dispatch: VERSION opens the release PR with the default branch",
    code == 0 and mock_open.call_args_list[0].args[1:] == (REPO, "1.2.3", "trunk"),
)
check(
    "dispatch: with the minted token's API",
    mock_open.call_args_list[0].args[0] is rig.api
    and rig.minter.perms == [write.RELEASE_PR]
    and rig.minter.released == 1,
)
mock_open.return_value = 1
check(
    "dispatch: the PR opener's exit passes through",
    rig.route("workflow_dispatch", dispatch_event, {**JOB_ENV, "VERSION": "1.2.3"}) == 1,
)
mock_open.return_value = 0
mock_open.reset_mock()
rig = Rig()
rig.route("workflow_dispatch", dispatch_event, {**JOB_ENV, "VERSION": "  1.2.3\n"})
check("dispatch: the version is stripped", mock_open.call_args_list[0].args[2] == "1.2.3")
for label, env in (
    ("no VERSION", dict(JOB_ENV)),
    ("an empty VERSION", {**JOB_ENV, "VERSION": ""}),
    ("a blank VERSION", {**JOB_ENV, "VERSION": "  "}),
):
    mock_open.reset_mock()
    rig = Rig()
    code = rig.route("workflow_dispatch", dispatch_event, env)
    check(
        f"dispatch: {label} opens nothing",
        code == 0 and not mock_open.called and rig.minter.perms == [],
    )
rig = Rig()
why = refusal(rig.job({**JOB_ENV, "VERSION": "1.2.3"}).route, "workflow_dispatch", {})
check("dispatch: no default branch is CannotRunError", why is not None and "default branch" in why)
opener_patch.stop()

# --- push: tagging a planned release ----------------------------------------

release_answer = (0, {"version": "1.2.3", "sha": HEAD, "pr": 7, "publish": "pypi"})
publish_patch = mock.patch.object(write._release, "publish_release", return_value=release_answer)
mock_publish = publish_patch.start()
planned = {"version": "1.2.3", "publish": "pypi"}
out_file = TMP / "gh-output"
push_env = {
    **JOB_ENV,
    "SDLC_CHECK": json.dumps({"release": planned}),
    "GITHUB_SHA": HEAD,
    "GITHUB_OUTPUT": str(out_file),
}
push_event = {"after": MOVED, "repository": {"default_branch": "main"}}

rig = Rig()
code = rig.route("push", push_event, push_env)
args = mock_publish.call_args_list[0].args
check("push: a planned release is published", code == 0 and mock_publish.call_count == 1)
check(
    "push: with a site for this repo and runner, the token, plan, sha and branch",
    (args[0].repo, args[0].runner, dict(args[0].env), args[1:])
    == (REPO, rig.runner, push_env, ("tok", planned, HEAD, "main")),
)
check("push: the site's api_for is the job's", args[0].api_for("any") is rig.api)
check(
    "push: minted with the tag permissions and released",
    rig.minter.perms == [write.TAG] and rig.minter.released == 1,
)
outputs = tools.read_outputs(out_file.read_text())
check(
    "push: the release output is the sorted JSON",
    json.loads(outputs["release"]) == release_answer[1]
    and outputs["release"] == json.dumps(release_answer[1], sort_keys=True),
)

mock_publish.reset_mock()
rig = Rig()
rig.route("push", push_event, {**push_env, "GITHUB_SHA": ""})
check(
    "push: the sha falls back to the event's after", mock_publish.call_args_list[0].args[3] == MOVED
)

mock_publish.return_value = (1, None)
out_file.write_text("")
mock_publish.reset_mock()
rig = Rig()
code = rig.route("push", push_event, push_env)
check(
    "push: a refused release returns its code and writes no output",
    code == 1 and out_file.read_text() == "",
)

mock_publish.return_value = (0, {"version": "1.2.3"})
for label, plan in (
    ("no release key", {}),
    ("a null release", {"release": None}),
    ("an empty release", {"release": {}}),
    ("only an audit", {"audit": {}}),
):
    mock_publish.reset_mock()
    rig = Rig()
    code = rig.route("push", push_event, {**push_env, "SDLC_CHECK": json.dumps(plan)})
    check(
        f"push: a plan with {label} does nothing",
        code == 0 and not mock_publish.called and rig.minter.perms == [],
    )
mock_publish.reset_mock()
rig = Rig()
code = rig.route("push", push_event, {k: v for k, v in push_env.items() if k != "SDLC_CHECK"})
check("push: no plan at all does nothing", code == 0 and not mock_publish.called)

for label, text in (("not JSON", "{oops"), ("a list", "[1]"), ("a string", '"x"')):
    rig = Rig()
    why = refusal(rig.job({**push_env, "SDLC_CHECK": text}).route, "push", push_event)
    check(f"push: a plan that is {label} is CannotRunError", why is not None and "plan" in why)
check("push: and a bad plan publishes nothing", not mock_publish.called)

# a1667: a release is tagged only once the image and boot jobs it planned passed.
gated = {"release": planned, "image": True, "boot": True}
for label, plan, results in (
    ("image failed", gated, {"SDLC_IMAGE": "failure", "SDLC_BOOT": "skipped"}),
    ("image was cancelled", gated, {"SDLC_IMAGE": "cancelled", "SDLC_BOOT": "skipped"}),
    ("a planned image was skipped", gated, {"SDLC_IMAGE": "skipped", "SDLC_BOOT": "skipped"}),
    ("boot failed", gated, {"SDLC_IMAGE": "success", "SDLC_BOOT": "failure"}),
    ("a planned boot was skipped", gated, {"SDLC_IMAGE": "success", "SDLC_BOOT": "skipped"}),
    ("image's result is empty", gated, {"SDLC_IMAGE": "", "SDLC_BOOT": "success"}),
):
    mock_publish.reset_mock()
    rig = Rig()
    code = rig.route("push", push_event, {**push_env, "SDLC_CHECK": json.dumps(plan), **results})
    check(
        f"push: not tagged when {label}",
        code == 1 and not mock_publish.called and rig.minter.perms == [],
    )
for label, plan, results in (
    ("image and boot passed", gated, {"SDLC_IMAGE": "success", "SDLC_BOOT": "success"}),
    (
        "neither was planned (a library)",
        {"release": planned},
        {"SDLC_IMAGE": "skipped", "SDLC_BOOT": "skipped"},
    ),
    (
        "image passed and no boot was planned",
        {"release": planned, "image": True},
        {"SDLC_IMAGE": "success", "SDLC_BOOT": "skipped"},
    ),
    ("a fleet file older than a1667 maps neither", gated, {}),
):
    mock_publish.reset_mock()
    rig = Rig()
    code = rig.route("push", push_event, {**push_env, "SDLC_CHECK": json.dumps(plan), **results})
    check(f"push: tagged when {label}", code == 0 and mock_publish.call_count == 1)
publish_patch.stop()

# --- schedule: the audit issue ----------------------------------------------

audit_patch = mock.patch.object(write._release, "audit_issue", return_value=0)
mock_audit = audit_patch.start()
report = {"dependencies": [], "fixes": []}
sched_env = {**JOB_ENV, "SDLC_CHECK": json.dumps({"audit": report}), "GITHUB_RUN_ID": "4242"}

rig = Rig()
code = rig.route("schedule", {}, sched_env)
args = mock_audit.call_args_list[0].args
check("schedule: a planned audit runs audit_issue", code == 0 and mock_audit.call_count == 1)
check(
    "schedule: with a site for this repo, the report and the token",
    (args[0].repo, args[0].runner, dict(args[0].env), args[1], args[2])
    == (REPO, rig.runner, sched_env, report, "tok"),
)
check(
    "schedule: the run url is github.com by default",
    args[3] == f"https://github.com/{REPO}/actions/runs/4242",
)
check(
    "schedule: minted with the issues permission only and released",
    rig.minter.perms == [write.AUDIT] and rig.minter.released == 1,
)
mock_audit.reset_mock()
rig = Rig()
rig.route("schedule", {}, {**sched_env, "GITHUB_SERVER_URL": "https://ghe.example.com"})
check(
    "schedule: the run url follows GITHUB_SERVER_URL",
    mock_audit.call_args_list[0].args[3] == f"https://ghe.example.com/{REPO}/actions/runs/4242",
)
mock_audit.return_value = 1
check("schedule: audit_issue's exit passes through", rig.route("schedule", {}, sched_env) == 1)
mock_audit.return_value = 0
mock_audit.reset_mock()
rig = Rig()
rig.route("schedule", {}, {**sched_env, "SDLC_CHECK": json.dumps({"audit": {}})})
check(
    "schedule: an empty report still runs the audit (it closes the alert)",
    mock_audit.call_count == 1,
)
for label, plan in (
    ("no audit", {}),
    ("a null audit", {"audit": None}),
    ("only a release", {"release": {"version": "1"}}),
):
    mock_audit.reset_mock()
    rig = Rig()
    code = rig.route("schedule", {}, {**sched_env, "SDLC_CHECK": json.dumps(plan)})
    check(
        f"schedule: a plan with {label} does nothing",
        code == 0 and not mock_audit.called and rig.minter.perms == [],
    )
audit_patch.stop()

rig = Rig()
code = rig.route("workflow_run", {}, {**JOB_ENV, "SDLC_CHECK": "{}"})
check(
    "route: any other event is nothing planned, exit 0",
    code == 0 and rig.api.calls == [] and rig.minter.perms == [],
)

if __name__ == "__main__":
    sys.exit(finish())
