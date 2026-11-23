#!/usr/bin/env python3
"""sdlc_jobs/ai.py, against a fake GitHub and a fake subprocess runner.

`ai` holds the model keys and a read token and never writes to GitHub, so what
is pinned here is what it decides and what it lets each child see:

  - Gatehouse's skips (a release PR, no key, an unchanged patch), its failed
    review when none could run, and its recorded posts when one did
  - the shim and every child get an allowlisted environment: the reviewer
    gets the model key and the read token and not the Claude token; claude
    gets its own token and nothing else of the job's
  - Ashigaru's fix round runs only on its own branch in this repository, with
    this review's findings appended under negative ids
  - an issue is guarded, triaged, and coded only where the scripts say so
  - what is handed on is four outputs, and a result over the cap says how big

Ashigaru's scripts are stubs in a temporary directory: the two `ai` imports
(findings, triage_apply) are minimal stand-ins and nothing real is run.

Run: python3 tests/test_event_ai.py
"""

from __future__ import annotations

import atexit
import base64
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

import sdlc_root
from fake_github import FakeGitHub, FakeRunner, check, finish

from sdlc_jobs import _handoff, ai
from sdlc_jobs import _tools as tools
from sdlc_jobs._github import APP_LOGIN, APP_SLUG, ApiError, CannotRunError
from sdlc_jobs._tools import Done, Site

STUBS = Path(tempfile.mkdtemp(prefix="sdlc-ashigaru-stubs-"))
atexit.register(shutil.rmtree, STUBS, ignore_errors=True)
(STUBS / "scripts").mkdir()
(STUBS / "schemas").mkdir()
for schema in ("fix", "code", "triage"):
    (STUBS / "schemas" / f"{schema}.json").write_text("{}")
(STUBS / "scripts" / "findings.py").write_text(
    "def plan(findings, failed_checks, used, max_rounds):\n"
    "    if used >= max_rounds:\n"
    "        return 'cap'\n"
    "    return 'fix' if findings or failed_checks else 'none'\n"
    "\n"
    "def model_for(round_no, base, escalated, at):\n"
    "    return escalated if round_no >= at else base\n"
)
(STUBS / "scripts" / "triage_apply.py").write_text(
    "CALLS = []\n"
    "\n"
    "def decide(verdict, categories, min_confidence):\n"
    "    CALLS.append((verdict, sorted(categories), min_confidence))\n"
    "    return {'verdict': verdict}\n"
    "\n"
    "def settle(decision, trust):\n"
    "    go = decision['verdict'].get('go') and trust != 'none'\n"
    "    return ('start' if go else 'hold'), None\n"
    "\n"
    "def starts_now(state, queue):\n"
    "    return state == 'start' and queue == 'fresh'\n"
)
tools.ASHIGARU_SCRIPTS = STUBS / "scripts"

REPO = "o/r"
HEAD, BASE, EARLIER = "a" * 40, "b" * 40, "c" * 40
COMPLETE = "Gatehouse found no issues.\n\n<!-- gatehouse review complete -->"
INLINE = [
    {"path": "a.py", "line": 3, "body": "**HIGH** (Bug Hunter): one"},
    {"path": "b.py", "line": 4, "body": "**LOW** (Security Scan): two"},
]
POST = {"event": "COMMENT", "commit_id": HEAD, "body": COMPLETE, "comments": INLINE}
BASE_ENV = {
    "OPENROUTER_API_KEY": "or-key",
    "GITHUB_TOKEN": "read-tok",
    "ASHIGARU_APP_KEY": "app-key",
    "PATH": "/p",
    "HOME": "/h",
}
WITH_CLAUDE = {**BASE_ENV, "CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok"}
TMP = Path(tempfile.mkdtemp(prefix="sdlc-ai-test-"))
atexit.register(shutil.rmtree, TMP, ignore_errors=True)


def quiet(fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kw)


def arg_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def reply_outputs(env: dict[str, str], text: str) -> None:
    with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
        handle.write(text)


class Rig:
    """One scenario: the routes GitHub answers, what each tool does, and what it saw."""

    def __init__(self, routes: dict[tuple[str, str], Any] | None = None, **cfg: Any) -> None:
        self.cfg: dict[str, Any] = {
            "shim_exit": 0,
            "shim_posts": [{"repo": REPO, "pr": 7, "payload": POST}],
            "findings": {"round": 2, "findings": [{"id": 11, "body": "old"}], "failed_checks": []},
            "findings_action": "wait",
            "findings_exit": 0,
            "guard": {"triage": True, "code": True},
            "trust": "write",
            "verdict": {"go": True},
            "claude_exit": 0,
            "patch": "PATCH TEXT",
            "patch_id": None,
        } | cfg
        base_routes: dict[tuple[str, str], Any] = {
            ("GET", f"/repos/{REPO}/pulls/7/reviews"): [],
            ("GET", f"/repos/{REPO}/pulls/7"): "THE DIFF",
        }
        self.api = FakeGitHub(base_routes | (routes or {}))
        self.work = Path(tempfile.mkdtemp(dir=TMP))
        self.guard_seen: list[dict[str, Any]] = []
        self.runner = FakeRunner(
            [
                (["_gatehouse_shim"], self.shim),
                (["findings.py"], self.findings),
                (["guard.py"], self.guard),
                (["prompt.py"], self.prompt),
                (["claude", "-p"], self.claude),
                (["git", "patch-id"], self.patch_id),
                (["git", "diff", "--cached"], lambda *_a: Done(0, self.cfg["patch"], "")),
            ]
        )

    def shim(self, args: list[str], _env: dict[str, str], _cwd: Any, _stdin: Any) -> Done:
        posts = self.cfg["shim_posts"]
        if posts is not None:
            Path(args[3]).write_text(posts if isinstance(posts, str) else json.dumps(posts))
        return Done(self.cfg["shim_exit"], "shim out\n", "")

    def findings(self, args: list[str], env: dict[str, str], _cwd: Any, _stdin: Any) -> Done:
        reply_outputs(env, f"action={self.cfg['findings_action']}\n")
        if self.cfg["findings"] is not None:
            Path(arg_after(args, "--out")).write_text(json.dumps(self.cfg["findings"]))
        return Done(self.cfg["findings_exit"], "", "findings stderr")

    def guard(self, args: list[str], env: dict[str, str], _cwd: Any, _stdin: Any) -> Done:
        stage = arg_after(args, "--stage")
        self.guard_seen.append(
            {
                "stage": stage,
                "env": env,
                "event": json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text()),
            }
        )
        allowed = "true" if self.cfg["guard"].get(stage) else "false"
        reply_outputs(env, f"allowed={allowed}\ntrust={self.cfg['trust']}\n")
        return Done(self.cfg.get("guard_exit", 0), "", "guard stderr")

    def prompt(self, args: list[str], env: dict[str, str], _cwd: Any, _stdin: Any) -> Done:
        del env
        Path(arg_after(args, "--out")).write_text("PROMPT TEXT")
        return Done(0, "", "")

    def claude(self, args: list[str], env: dict[str, str], _cwd: Any, _stdin: Any) -> Done:
        del env
        if self.cfg["claude_exit"]:
            return Done(self.cfg["claude_exit"], "", "claude broke")
        coding = "Edit" in arg_after(args, "--allowedTools")
        answer = {"dispositions": []} if coding else self.cfg["verdict"]
        return Done(0, json.dumps({"structured_output": answer}), "")

    def patch_id(self, args: list[str], env: dict[str, str], _cwd: Any, stdin: Any) -> Done:
        del args, env
        if self.cfg["patch_id"] is not None:
            return self.cfg["patch_id"]
        return Done(0, f"{str(stdin).replace(' ', '_')} commit\n", "")

    def job(self, env: dict[str, str] | None = None) -> ai.Job:
        site = Site(REPO, env or BASE_ENV, self.runner, self.work, api_for=lambda _t: self.api)
        return ai.Job(site, self.api)

    def go(
        self, name: str, event: dict[str, Any], env: dict[str, str] | None = None
    ) -> tuple[int, dict[str, Any]]:
        return quiet(ai.run, name, event, self.job(env))

    def ran(self, needle: str) -> list[tuple[list[str], dict[str, str], Path | None, str | None]]:
        return [c for c in self.runner.calls if needle in " ".join(c[0])]


def pr_of(
    ref: str = "feat/x", head_repo: str = REPO, author: str = "dev", number: int = 7
) -> dict[str, Any]:
    return {
        "number": number,
        "user": {"login": author},
        "head": {"sha": HEAD, "ref": ref, "repo": {"full_name": head_repo}},
        "base": {"sha": BASE},
    }


def pr_event(action: str = "opened", **kw: Any) -> dict[str, Any]:
    return {"action": action, "pull_request": pr_of(**kw)}


def issue_event(action: str = "opened", label: str | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "action": action,
        "issue": {"number": 9},
        "repository": {"default_branch": "main"},
    }
    if label:
        event["label"] = {"name": label}
    return event


def earlier_review(sha: str = EARLIER, user: str = APP_LOGIN, rid: int = 1) -> dict[str, Any]:
    return {
        "id": rid,
        "user": {"login": user},
        "body": COMPLETE,
        "commit_id": sha,
        "html_url": f"https://x/r/{rid}",
    }


# --- not an event for ai ----------------------------------------------------

check("main: a push is nothing, exit 0", quiet(ai.main, "push", {}, {}) == 0)
check("main: no repository is exit 2", quiet(ai.main, "pull_request_target", pr_event(), {}) == 2)

# --- main, end to end -------------------------------------------------------
# The entry `sdlc event` calls, with only the HTTP client and the runner faked:
# the event reaches the job and the outputs land in GITHUB_OUTPUT.

with tempfile.TemporaryDirectory(prefix="ai-main-") as main_dir:
    outputs = Path(main_dir) / "out"
    main_env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_OUTPUT": str(outputs)}
    with (
        mock.patch.object(ai, "GitHub", return_value=FakeGitHub()),
        mock.patch.object(tools, "run", FakeRunner()),
    ):
        code = quiet(ai.main, "issues", issue_event(), main_env)
        handed = _handoff.decode(
            [tools.read_outputs(outputs.read_text()).get(name, "") for name in _handoff.NAMES]
        )
        check(
            "main: an issue where Ashigaru is not enrolled hands on the skip, exit 0",
            code == 0 and handed["kind"] == "issue" and handed["number"] == 9 and "skip" in handed,
        )
        outputs.write_text("")
        code = quiet(ai.main, "pull_request_target", pr_event(), main_env)
        handed = _handoff.decode(
            [tools.read_outputs(outputs.read_text()).get(name, "") for name in _handoff.NAMES]
        )
        check(
            "main: no model key is exit 2 and still hands on the failed review",
            code == 2
            and handed["review"]["status"] == "failed"
            and ai.FAILED in handed["review"]["posts"][0]["body"],
        )

# --- release PR skip --------------------------------------------------------

rig = Rig()
code, result = rig.go(
    "pull_request_target", pr_event(ref="ashigaru/release-1.2.3", author=APP_LOGIN), WITH_CLAUDE
)
check("release: exit 0", code == 0)
check(
    "release: review skipped",
    result["review"] == {"status": "skipped", "reason": "release", "posts": []},
)
check("release: no gatehouse run, no subprocess at all", rig.runner.calls == [])
check("release: no API call", rig.api.calls == [])
check("release: no fix round either", "fix" not in result)
check(
    "release: result names the pull request",
    (result["v"], result["kind"], result["number"], result["head_sha"]) == (1, "pr", 7, HEAD),
)

for label, kw in (
    ("authored by a person", {"author": "dev"}),
    ("from a fork", {"head_repo": "evil/r", "author": APP_LOGIN}),
):
    rig = Rig()
    _, result = rig.go("pull_request_target", pr_event(ref="ashigaru/release-1.2.3", **kw))
    check(
        f"release: a branch of that name {label} is reviewed",
        result["review"]["status"] == "reviewed",
    )

rig = Rig()
_, result = rig.go(
    "pull_request_target", pr_event(ref="ashigaru/release-1.2.3", author="dev"), WITH_CLAUDE
)
check(
    "release: a person's release-named branch gets no fix round",
    "fix" not in result and rig.ran("findings.py") == [],
)

# --- missing OPENROUTER_API_KEY ---------------------------------------------

rig = Rig()
code, result = rig.go("pull_request_target", pr_event(), {"GITHUB_TOKEN": "read-tok"})
review = result["review"]
check("no key: exit 2", code == 2)
check("no key: status failed", review["status"] == "failed")
check("no key: one failed-review post", len(review["posts"]) == 1)
post = review["posts"][0]
check(
    "no key: post carries the failed marker",
    ai.FAILED in post["body"] and "OPENROUTER_API_KEY" in post["body"],
)
check(
    "no key: post is a COMMENT on the head",
    post["event"] == "COMMENT" and post["commit_id"] == HEAD,
)
check("no key: no gatehouse run and no API call", rig.runner.calls == [] and rig.api.calls == [])

# --- same-patch skip --------------------------------------------------------

compare_head = f"/repos/{REPO}/compare/{BASE}...{HEAD}"
compare_earlier = f"/repos/{REPO}/compare/{BASE}...{EARLIER}"
carried = f"/repos/{REPO}/compare/{HEAD}...{EARLIER}"
same_routes = {
    ("GET", f"/repos/{REPO}/pulls/7/reviews"): [earlier_review()],
    ("GET", compare_head): "diff one",
    ("GET", compare_earlier): "diff one",
    ("GET", carried): {"commits": [{"sha": "c1"}, {"sha": "c2"}]},
}
rig = Rig(same_routes, patch_id=Done(0, "pid123 -\n", ""))
code, result = rig.go("pull_request_target", pr_event())
review = result["review"]
check(
    "same patch: exit 0, skipped",
    code == 0 and review["status"] == "skipped" and review["reason"] == "same-patch",
)
body = review["posts"][0]["body"]
check("same patch: the post carries the complete marker", ai.COMPLETE in body)
check(
    "same patch: and the carried marker with the commits",
    "<!-- gatehouse carried c1 c2 -->" in body,
)
check(
    "same patch: it names the earlier sha and review",
    EARLIER[:7] in body and "https://x/r/1" in body,
)
check(
    "same patch: a COMMENT on this head",
    review["posts"][0]["event"] == "COMMENT" and review["posts"][0]["commit_id"] == HEAD,
)
check("same patch: no gatehouse run", rig.ran("_gatehouse_shim") == [])
check(
    "same patch: patch-id ran verbatim on each diff",
    [c[3] for c in rig.ran("patch-id")] == ["diff one", "diff one"],
)

rig = Rig(
    same_routes
    | {("GET", f"/repos/{REPO}/pulls/7/reviews"): [earlier_review(user="github-actions[bot]")]},
    patch_id=Done(0, "pid123 -\n", ""),
)
code, result = rig.go("pull_request_target", pr_event())
check(
    "same patch: a github-actions[bot] review is ignored",
    result["review"]["status"] == "reviewed" and rig.ran("patch-id") == [],
)

rig = Rig(same_routes)
rig.api.routes[("GET", compare_earlier)] = "diff two"
code, result = rig.go("pull_request_target", pr_event())
check("same patch: a different patch is reviewed", result["review"]["status"] == "reviewed")

rig = Rig(same_routes, patch_id=Done(1, "", "git broke"))
_, result = rig.go("pull_request_target", pr_event())
check(
    "same patch: a failing patch-id falls through to a review",
    result["review"]["status"] == "reviewed",
)

rig = Rig(
    same_routes | {("GET", compare_earlier): ApiError("GET", "x", 500, "down")},
    patch_id=Done(0, "p -\n", ""),
)
_, result = rig.go("pull_request_target", pr_event())
check(
    "same patch: an API error falls through to a review", result["review"]["status"] == "reviewed"
)

failed_review = {**earlier_review(), "body": f"{COMPLETE}\n{ai.FAILED}"}
rig = Rig(
    same_routes | {("GET", f"/repos/{REPO}/pulls/7/reviews"): [failed_review]},
    patch_id=Done(0, "p -\n", ""),
)
_, result = rig.go("pull_request_target", pr_event())
check(
    "same patch: a failed review is not a review to carry", result["review"]["status"] == "reviewed"
)

# --- the diff ---------------------------------------------------------------

state = {"calls": 0}


def pulls_then_meta(changed: int) -> Callable[[Any], Any]:
    def route(_body: Any) -> Any:
        state["calls"] += 1
        if state["calls"] == 1:
            raise ApiError("GET", "/pulls/7", 406, "diff too large")
        return {"changed_files": changed}

    return route


FILES = [
    {"filename": "a.py", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"},
    {"filename": "new.py", "status": "added", "patch": "@@ -0,0 +1 @@\n+n"},
]
state["calls"] = 0
rig = Rig(
    {
        ("GET", f"/repos/{REPO}/pulls/7"): pulls_then_meta(2),
        ("GET", f"/repos/{REPO}/pulls/7/files"): FILES,
    }
)
code, result = rig.go("pull_request_target", pr_event())
check(
    "diff: rebuilt from files when the diff endpoint fails",
    result["review"]["status"] == "reviewed" and code == 0,
)
stdin = rig.ran("_gatehouse_shim")[0][3] or ""
check(
    "diff: the shim is fed the rebuilt diff",
    stdin.startswith("diff --git a/a.py b/a.py") and "+++ b/new.py" in stdin,
)

state["calls"] = 0
rig = Rig(
    {
        ("GET", f"/repos/{REPO}/pulls/7"): pulls_then_meta(3),
        ("GET", f"/repos/{REPO}/pulls/7/files"): FILES,
    }
)
code, result = rig.go("pull_request_target", pr_event())
check(
    "diff: fewer files listed than changed is exit 2",
    code == 2 and result["review"]["status"] == "failed",
)
check(
    "diff: and says how many",
    "3 files" in result["review"]["reason"] and "only 2" in result["review"]["reason"],
)
check(
    "diff: the failed post is on the head with the marker",
    result["review"]["posts"][0]["commit_id"] == HEAD
    and ai.FAILED in result["review"]["posts"][0]["body"],
)
check("diff: no gatehouse run", rig.ran("_gatehouse_shim") == [])

rig = Rig()
_, result = rig.go("pull_request_target", pr_event())
check(
    "diff: the PR diff is fed to the shim as fetched",
    rig.ran("_gatehouse_shim")[0][3] == "THE DIFF",
)

# --- the gatehouse run ------------------------------------------------------

env = {**WITH_CLAUDE, "GITHUB_STEP_SUMMARY": "/run/summary.md"}
rig = Rig()
code, result = rig.go("pull_request_target", pr_event(), env)
review = result["review"]
check(
    "gatehouse: reviewed, exit 0",
    code == 0 and review["status"] == "reviewed" and review["exit"] == 0,
)
check("gatehouse: the recorded payloads are the posts", review["posts"] == [POST])
call = rig.ran("_gatehouse_shim")[0]
argv, genv, _, stdin = call
check(
    "gatehouse: argv is the venv python, -I, the shim",
    argv[:3]
    == [tools.GATEHOUSE_PYTHON, "-I", str(sdlc_root.ROOT / "sdlc_jobs" / "_gatehouse_shim.py")],
)
check(
    "gatehouse: its flags",
    argv[4:9]
    == ["--stdin", "--comment", "--required-low-agents", ai.REQUIRED_LOW_AGENTS, "--advisory"],
)
check("gatehouse: not incremental on opened", "--incremental" not in argv)
check(
    "gatehouse: the env has the model key and the read token",
    genv["OPENROUTER_API_KEY"] == "or-key" and genv["GITHUB_TOKEN"] == "read-tok",
)
check(
    "gatehouse: no Claude token, no App key",
    "CLAUDE_CODE_OAUTH_TOKEN" not in genv and "ASHIGARU_APP_KEY" not in genv,
)
check(
    "gatehouse: nothing outside the allowlist",
    set(genv) - set(tools.child_env(env))
    <= {
        "OPENROUTER_API_KEY",
        "GITHUB_TOKEN",
        "GITHUB_REPOSITORY",
        "GITHUB_REF",
        "GATEHOUSE_CONTEXT_REPO",
        "GATEHOUSE_CONTEXT_REF",
        "GATEHOUSE_HEAD_SHA",
        "GATEHOUSE_HEAD_REPO",
        "GITHUB_STEP_SUMMARY",
    },
)
check(
    "gatehouse: the PR context",
    (genv["GITHUB_REPOSITORY"], genv["GITHUB_REF"], genv["GATEHOUSE_CONTEXT_REF"])
    == (REPO, "refs/pull/7/merge", BASE),
)
check(
    "gatehouse: the head", (genv["GATEHOUSE_HEAD_SHA"], genv["GATEHOUSE_HEAD_REPO"]) == (HEAD, REPO)
)
check(
    "gatehouse: capture path is argv[3], in the work dir",
    argv[3] == str(rig.work / "gatehouse-posts.json") and "SDLC_GATEHOUSE_CAPTURE" not in genv,
)
check(
    "gatehouse: the step summary is passed through",
    genv["GITHUB_STEP_SUMMARY"] == "/run/summary.md",
)
check("gatehouse: stdin is the diff", stdin == "THE DIFF")

rig = Rig()
rig.go("pull_request_target", pr_event("synchronize"))
check(
    "gatehouse: --incremental on synchronize", "--incremental" in rig.ran("_gatehouse_shim")[0][0]
)
rig = Rig()
rig.go("pull_request_target", pr_event("reopened"))
check(
    "gatehouse: not incremental on reopened",
    "--incremental" not in rig.ran("_gatehouse_shim")[0][0],
)

rig = Rig(shim_exit=1)
code, result = rig.go("pull_request_target", pr_event())
check(
    "gatehouse: exit 1 (findings) is still a completed review",
    code == 0 and result["review"]["status"] == "reviewed" and result["review"]["exit"] == 1,
)

failed_post = {
    "repo": REPO,
    "pr": 7,
    "payload": {"event": "COMMENT", "commit_id": HEAD, "body": f"could not\n{ai.FAILED}"},
}
rig = Rig(shim_exit=2, shim_posts=[failed_post])
code, result = rig.go("pull_request_target", pr_event())
check(
    "gatehouse: exit 2 with its own failed post keeps that post",
    code == 2 and result["review"]["status"] == "failed" and result["review"]["exit"] == 2,
)
check("gatehouse: and does not add another", result["review"]["posts"] == [failed_post["payload"]])

rig = Rig(shim_exit=2, shim_posts=[])
code, result = rig.go("pull_request_target", pr_event())
check(
    "gatehouse: exit 2 with no failed post synthesizes one",
    code == 2
    and len(result["review"]["posts"]) == 1
    and ai.FAILED in result["review"]["posts"][0]["body"],
)
check(
    "gatehouse: the synthesized post names the exit and the head",
    "exited 2" in result["review"]["reason"] and result["review"]["posts"][0]["commit_id"] == HEAD,
)

rig = Rig(shim_exit=2, shim_posts=[{"repo": REPO, "pr": 7, "payload": POST}])
code, result = rig.go("pull_request_target", pr_event())
check(
    "gatehouse: exit 2 with only a normal post is still failed",
    code == 2
    and ai.FAILED in result["review"]["posts"][0]["body"]
    and result["review"]["status"] == "failed",
)

rig = Rig(shim_posts=None, shim_exit=0)
code, result = rig.go("pull_request_target", pr_event())
check(
    "gatehouse: no capture file is failed, exit 2",
    code == 2
    and result["review"]["status"] == "failed"
    and ai.FAILED in result["review"]["posts"][0]["body"],
)
rig = Rig(shim_posts="not json")
code, result = rig.go("pull_request_target", pr_event())
check(
    "gatehouse: an unreadable capture is failed, exit 2",
    code == 2 and result["review"]["status"] == "failed",
)
rig = Rig(shim_posts=[{"no": "payload"}])
code, result = rig.go("pull_request_target", pr_event())
check(
    "gatehouse: a capture entry without a payload is failed",
    code == 2 and result["review"]["status"] == "failed",
)

# --- the fix path -----------------------------------------------------------

FIX_PR = {"ref": "ashigaru/issue-7"}
rig = Rig()
code, result = rig.go("pull_request_target", pr_event(**FIX_PR), WITH_CLAUDE)
fix = result["fix"]
check(
    "fix: exit 0 and action fix",
    code == 0 and fix["action"] == "fix" and fix["branch"] == "ashigaru/issue-7",
)
ids = [f["id"] for f in fix["findings"]["findings"]]
check("fix: pending findings get ids -1, -2 appended", ids == [11, -1, -2], str(ids))
pending = fix["findings"]["findings"][1]
check(
    "fix: a pending finding is in findings.py's shape",
    (pending["path"], pending["line"], pending["origin"], pending["kind"], pending["author"])
    == ("a.py", 3, "reviewer", "inline", APP_LOGIN),
)
check("fix: and keeps the comment's body", pending["body"] == INLINE[0]["body"])
fargv, fenv, _, _ = rig.ran("findings.py")[0]
check(
    "fix: findings.py by path under the stub directory",
    fargv[:2] == ["python3", str(STUBS / "scripts" / "findings.py")],
)
check(
    "fix: findings.py args",
    (
        arg_after(fargv, "--repo"),
        arg_after(fargv, "--branch"),
        arg_after(fargv, "--reviewer"),
        arg_after(fargv, "--bot"),
    )
    == (REPO, "ashigaru/issue-7", APP_LOGIN, APP_SLUG),
)
check(
    "fix: findings.py gets the read token as GH_TOKEN and no model key",
    fenv["GH_TOKEN"] == "read-tok"
    and "CLAUDE_CODE_OAUTH_TOKEN" not in fenv
    and "OPENROUTER_API_KEY" not in fenv
    and "GITHUB_TOKEN" not in fenv,
)
cargv, cenv, ccwd, cstdin = rig.ran("claude")[0]
check(
    "fix: claude is given the prompt on stdin",
    cstdin == "PROMPT TEXT" and "PROMPT TEXT" not in cargv,
)
creds = {
    k
    for k in cenv
    if k
    in {
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "OPENROUTER_API_KEY",
        "ASHIGARU_APP_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
    }
}
check(
    "fix: claude's only credential is its own token",
    creds == {"CLAUDE_CODE_OAUTH_TOKEN"} and cenv["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-tok",
)
check(
    "fix: --settings carries the /proc deny rule",
    arg_after(cargv, "--settings") == ai.SETTINGS
    and json.loads(ai.SETTINGS)["permissions"]["deny"]
    == [
        "Read(//proc/**)",
        "Grep(//proc/**)",
        "Glob(//proc/**)",
        "Read(//sys/**)",
        "Grep(//sys/**)",
        "Glob(//sys/**)",
    ],
)
check(
    "fix: edit tools, 80 turns, sonnet in round 2",
    (
        arg_after(cargv, "--allowedTools"),
        arg_after(cargv, "--max-turns"),
        arg_after(cargv, "--model"),
    )
    == (ai.EDIT_TOOLS, "80", "sonnet"),
)
check(
    "fix: the schema is passed compact",
    arg_after(cargv, "--json-schema") == "{}" and "--output-format" in cargv,
)
check("fix: claude runs in the PR checkout", ccwd == rig.work / "pr")
check("fix: the patch is collected from git diff", fix["patch"] == "PATCH TEXT")
adds = [i for i, c in enumerate(rig.runner.calls) if c[0][:3] == ["git", "add", "-A"]]
diffs = [i for i, c in enumerate(rig.runner.calls) if c[0][:3] == ["git", "diff", "--cached"]]
check(
    "fix: staged, then diffed binary-safe",
    adds and diffs and adds[0] < diffs[0] and "--binary" in rig.runner.calls[diffs[0]][0],
)
clones = rig.ran("git checkout")
check(
    "fix: the PR branch is checked out at the head",
    clones and clones[0][0][-3:] == ["-B", "ashigaru/issue-7", "FETCH_HEAD"] if clones else False,
)
check(
    "fix: the clone carries no token",
    all("GIT_CONFIG_VALUE_0" not in c[1] for c in rig.runner.calls if c[0][0] == "git"),
)
prompt_argv = rig.ran("prompt.py")[0][0]
check(
    "fix: prompt.py fix, with the findings file",
    prompt_argv[2] == "fix" and arg_after(prompt_argv, "--findings").endswith("findings.json"),
)
check(
    "fix: the answer is the structured output",
    fix["result"] == {"dispositions": []} and fix["model"] == "sonnet",
)
check(
    "fix: the findings sent to the model include the pending ones",
    [f["id"] for f in json.loads((rig.work / "findings.json").read_text())["findings"]]
    == [11, -1, -2],
)

rig = Rig(findings={"round": 3, "findings": [{"id": 11, "body": "old"}], "failed_checks": []})
_, result = rig.go("pull_request_target", pr_event(**FIX_PR), WITH_CLAUDE)
check(
    "fix: round 3 escalates the model",
    result["fix"]["model"] == "opus" and arg_after(rig.ran("claude")[0][0], "--model") == "opus",
)

rig = Rig(findings={"round": 6, "findings": [{"id": 11}], "failed_checks": []})
_, result = rig.go("pull_request_target", pr_event(**FIX_PR), WITH_CLAUDE)
check(
    "fix: past the round cap is action cap, no model",
    result["fix"]["action"] == "cap" and rig.ran("claude") == [] and "patch" not in result["fix"],
)
check("fix: and no clone", rig.ran("git fetch") == [])

rig = Rig(
    shim_posts=[{"repo": REPO, "pr": 7, "payload": {**POST, "comments": []}}],
    findings={"round": 1, "findings": [], "failed_checks": []},
)
_, result = rig.go("pull_request_target", pr_event(**FIX_PR), WITH_CLAUDE)
check(
    "fix: nothing open is action none",
    result["fix"]["action"] == "none" and rig.ran("claude") == [],
)

rig = Rig(findings=None, findings_action="wait")
code, result = rig.go("pull_request_target", pr_event(**FIX_PR), WITH_CLAUDE)
check(
    "fix: findings.py writing no file is none, with its reason",
    code == 0 and result["fix"] == {"action": "none", "reason": "wait"},
)

rig = Rig(findings_exit=1)
code, result = rig.go("pull_request_target", pr_event(**FIX_PR), WITH_CLAUDE)
check(
    "fix: findings.py failing fails ai (2) but keeps the review to hand on",
    code == 2
    and result["fix"]["action"] == "failed"
    and "findings.py failed (exit 1)" in result["fix"]["error"]
    and result["review"]["status"] == "reviewed",
)

rig = Rig(claude_exit=3)
code, result = rig.go("pull_request_target", pr_event(**FIX_PR), WITH_CLAUDE)
check(
    "fix: claude failing fails ai (2) but keeps the review to hand on",
    code == 2
    and result["fix"]["action"] == "failed"
    and "claude (fix) exited 3" in result["fix"]["error"]
    and result["review"]["status"] == "reviewed",
)

for label, kw, env_for in (
    ("a fork PR", {"ref": "ashigaru/issue-7", "head_repo": "evil/r"}, WITH_CLAUDE),
    (
        "a release branch of this repo",
        {"ref": "ashigaru/release-1.2.3", "author": "dev"},
        WITH_CLAUDE,
    ),
    ("a human branch", {"ref": "feat/x"}, WITH_CLAUDE),
    ("no Claude token", {"ref": "ashigaru/issue-7"}, BASE_ENV),
):
    rig = Rig()
    code, result = rig.go("pull_request_target", pr_event(**kw), env_for)
    check(
        f"fix: none for {label}",
        "fix" not in result and rig.ran("findings.py") == [] and rig.ran("claude") == [],
    )
    check(
        f"fix: but {label} is still reviewed",
        result["review"]["status"] == "reviewed" and code == 0,
    )

# --- issues -----------------------------------------------------------------

apply_calls = ai.ashigaru_module("triage_apply").CALLS

rig = Rig()
code, result = rig.go("issues", issue_event(), WITH_CLAUDE)
check(
    "issue opened: exit 0, kind issue",
    code == 0 and (result["v"], result["kind"], result["number"]) == (1, "issue", 9),
)
guards = rig.guard_seen
check(
    "issue opened: guard runs for triage then code",
    [g["stage"] for g in guards] == ["triage", "code"],
)
genv = guards[0]["env"]
check(
    "issue opened: guard gets the event file, name and repo",
    guards[0]["event"] == issue_event()
    and genv["GITHUB_EVENT_NAME"] == "issues"
    and genv["GITHUB_REPOSITORY"] == REPO,
)
check("issue opened: guard's GH_TOKEN is the read token", genv["GH_TOKEN"] == "read-tok")
check(
    "issue opened: guard sees no model credential",
    not {"CLAUDE_CODE_OAUTH_TOKEN", "OPENROUTER_API_KEY", "GITHUB_TOKEN", "ASHIGARU_APP_KEY"}
    & set(genv),
)
check("issue opened: trust comes from the guard", result["trust"] == "write")
check(
    "issue opened: the default branch is cloned",
    rig.ran("git fetch")[0][0][-1] == "main" and "-B" not in rig.ran("git checkout")[0][0],
)
tclaude = rig.ran("claude")[0]
check(
    "issue opened: triage model sonnet, 40 turns, read-only tools",
    (
        arg_after(tclaude[0], "--model"),
        arg_after(tclaude[0], "--max-turns"),
        arg_after(tclaude[0], "--allowedTools"),
    )
    == ("sonnet", "40", ai.READ_ONLY_TOOLS),
)
check(
    "issue opened: triage claude holds only its token",
    {k for k in tclaude[1] if "TOKEN" in k or "KEY" in k} == {"CLAUDE_CODE_OAUTH_TOKEN"},
)
check(
    "issue opened: the verdict and queue are kept",
    result["triage"] == {"verdict": {"go": True}, "queue": "fresh"},
)
prompts = [c[0] for c in rig.ran("prompt.py")]
check(
    "issue opened: prompt.py triage then code, for the issue",
    [p[2] for p in prompts] == ["triage", "code"]
    and all(arg_after(p, "--issue") == "9" for p in prompts),
)
check(
    "issue opened: triage_apply decided with the auto categories and confidence",
    apply_calls[-1] == ({"go": True}, sorted(ai.AUTO_CATEGORIES), ai.MIN_CONFIDENCE),
)
check(
    "issue opened: starts now, so there is code with a patch",
    result["code"] == {"result": {"dispositions": []}, "patch": "PATCH TEXT", "base": "main"},
)
check(
    "issue opened: the coder edits, 80 turns",
    arg_after(rig.ran("claude")[1][0], "--allowedTools") == ai.EDIT_TOOLS
    and arg_after(rig.ran("claude")[1][0], "--max-turns") == "80",
)

rig = Rig(verdict={"go": False})
code, result = rig.go("issues", issue_event(), WITH_CLAUDE)
check(
    "issue opened: a verdict that does not start now stops at triage",
    "code" not in result
    and result["triage"]["verdict"] == {"go": False}
    and len(rig.guard_seen) == 1,
)

rig = Rig(trust="none")
_, result = rig.go("issues", issue_event(), WITH_CLAUDE)
check("issue opened: trust none does not start now", "code" not in result and "triage" in result)

rig = Rig(guard={"triage": True, "code": False})
_, result = rig.go("issues", issue_event(), WITH_CLAUDE)
check(
    "issue opened: the guard refusing code leaves the triage alone",
    "triage" in result and "code" not in result and "skip" not in result,
)

rig = Rig(guard={"triage": False, "code": True})
code, result = rig.go("issues", issue_event(), WITH_CLAUDE)
check(
    "issue: a guard refusal is a skip",
    code == 0 and result["skip"] == "guard refused triage" and result["trust"] == "write",
)
check("issue: and no clone, no model", rig.ran("git fetch") == [] and rig.ran("claude") == [])

rig = Rig()
code, result = rig.go("issues", issue_event(), BASE_ENV)
check(
    "issue: no Claude token is 'not enrolled'",
    code == 0 and result["skip"] == "Ashigaru is not enrolled here",
)
check("issue: and nothing runs", rig.runner.calls == [])

rig = Rig()
code, result = rig.go("issues", issue_event("labeled", "ready-to-code"), WITH_CLAUDE)
check(
    "issue labeled ready-to-code: code directly",
    [g["stage"] for g in rig.guard_seen] == ["code"] and "triage" not in result,
)
check(
    "issue labeled ready-to-code: with a patch",
    result["code"]["patch"] == "PATCH TEXT"
    and result["code"]["base"] == "main"
    and len(rig.ran("claude")) == 1,
)

rig = Rig()
_, result = rig.go("issues", issue_event("labeled", "ready-for-triage"), WITH_CLAUDE)
check(
    "issue labeled ready-for-triage: backlog queue, not started",
    result["triage"]["queue"] == "backlog" and "code" not in result,
)

for label, event in (
    ("another label", issue_event("labeled", "bug")),
    ("closed", issue_event("closed")),
    ("edited", issue_event("edited")),
):
    rig = Rig()
    code, result = rig.go("issues", event, WITH_CLAUDE)
    check(
        f"issue {label}: nothing for Ashigaru",
        code == 0 and result["skip"] == "nothing for Ashigaru" and rig.runner.calls == [],
    )

rig = Rig(guard_exit=1)
try:
    rig.go("issues", issue_event(), WITH_CLAUDE)
    raised = None
except CannotRunError as err:
    raised = err
check(
    "issue: guard.py failing is CannotRunError",
    raised is not None and "guard.py failed" in str(raised),
)

event = issue_event()
del event["repository"]
rig = Rig()
try:
    rig.go("issues", event, WITH_CLAUDE)
    raised = None
except CannotRunError as err:
    raised = err
check(
    "issue: no default branch is CannotRunError",
    raised is not None and "default branch" in str(raised),
)

# --- helpers ----------------------------------------------------------------

check(
    "structured: structured_output",
    ai.structured(json.dumps({"structured_output": {"a": 1}}), "x") == {"a": 1},
)
check(
    "structured: result as JSON text",
    ai.structured(json.dumps({"result": '{"b": 2}'}), "x") == {"b": 2},
)
for label, text in (
    ("not JSON", "oops"),
    ("no answer", "{}"),
    ("a list", '{"structured_output": [1]}'),
    ("a non-JSON result", '{"result": "words"}'),
):
    try:
        ai.structured(text, "x")
        refused = False
    except CannotRunError:
        refused = True
    check(f"structured: {label} is CannotRunError", refused)


def hexsha(n: int) -> str:
    return f"{n:040x}"


reviews = [
    {"user": {"login": APP_LOGIN}, "body": COMPLETE, "commit_id": hexsha(n), "html_url": f"u{n}"}
    for n in range(1, 8)
]
heads = ai.earlier_heads(reviews)
check(
    "earlier_heads: newest first, five at most",
    [h[0] for h in heads] == [hexsha(n) for n in (7, 6, 5, 4, 3)],
)
bad = [
    {**reviews[0], "body": "Not gatehouse " + ai.COMPLETE},
    {**reviews[0], "body": "Gatehouse incomplete"},
    {**reviews[0], "body": f"Gatehouse {ai.COMPLETE} {ai.FAILED}"},
    {**reviews[0], "commit_id": "short"},
    {**reviews[0], "commit_id": "g" * 40},
    {**reviews[0], "user": {"login": "someone"}},
    {**reviews[0], "user": None},
]
check(
    "earlier_heads: only complete, un-failed, App, full-hex reviews count",
    ai.earlier_heads(bad) == [],
)
check(
    "earlier_heads: a repeated sha counts once",
    len(ai.earlier_heads([reviews[0], reviews[0]])) == 1,
)

diff = ai.rebuild_diff(
    [
        {"filename": "a", "status": "added", "patch": "P1"},
        {"filename": "b", "status": "removed", "patch": "P2"},
        {"filename": "n", "previous_filename": "o", "status": "renamed", "patch": None},
        {"filename": "m", "status": "modified", "patch": "P3"},
    ]
)
check(
    "rebuild_diff: an added file",
    "diff --git a/a b/a\nnew file mode 100644\n--- /dev/null\n+++ b/a\nP1" in diff,
)
check(
    "rebuild_diff: a removed file", "deleted file mode 100644\n--- a/b\n+++ /dev/null\nP2" in diff
)
check(
    "rebuild_diff: a rename without a patch has no hunk lines",
    "diff --git a/o b/n\nrename from o\nrename to n\ndiff --git a/m" in diff,
)
check("rebuild_diff: a modified file", "--- a/m\n+++ b/m\nP3" in diff and diff.endswith("\n"))

pend = ai.pending_findings(
    [
        {"comments": [{"path": "a", "line": 1, "body": "x"}]},
        {"body": "no comments"},
        {"comments": [{"path": "b", "line": 2, "body": "y"}]},
    ]
)
check(
    "pending_findings: numbered in post order across posts",
    [p["id"] for p in pend] == [-1, -2] and [p["path"] for p in pend] == ["a", "b"],
)
check("pending_findings: none when nothing is inline", ai.pending_findings([{"body": "x"}]) == [])

# --- hand_on ----------------------------------------------------------------

with tempfile.TemporaryDirectory() as scratch:
    out_path = Path(scratch) / "out"
    hand_env = {"GITHUB_OUTPUT": str(out_path)}
    small = {"v": 1, "kind": "pr", "number": 7, "review": {"posts": []}}
    code = quiet(ai.hand_on, hand_env, small, 0)
    written = tools.read_outputs(out_path.read_text())
    check("hand_on: writes p0..p3", sorted(written) == ["p0", "p1", "p2", "p3"])
    check(
        "hand_on: the parts decode to the result",
        _handoff.decode([written[n] for n in ("p0", "p1", "p2", "p3")]) == small,
    )
    check(
        "hand_on: returns the job's code", code == 0 and quiet(ai.hand_on, hand_env, small, 2) == 2
    )

with tempfile.TemporaryDirectory() as scratch:
    out_path = Path(scratch) / "out"
    hand_env = {"GITHUB_OUTPUT": str(out_path)}
    huge = {
        "v": 1,
        "kind": "pr",
        "number": 7,
        "blob": base64.b64encode(os.urandom(400_000)).decode(),
    }
    code = quiet(ai.hand_on, hand_env, huge, 0)
    written = tools.read_outputs(out_path.read_text())
    decoded = _handoff.decode([written[n] for n in ("p0", "p1", "p2", "p3")])
    check("hand_on: too large is code 1", code == 1)
    check(
        "hand_on: p0 decodes to the size and the identity only",
        set(decoded) == {"v", "kind", "number", "too_large"} and decoded["too_large"] > 450_000,
        str(set(decoded)),
    )
    check("hand_on: nothing of the blob is carried", "blob" not in decoded)
    check("hand_on: too large keeps a worse code", quiet(ai.hand_on, hand_env, huge, 2) == 2)

if __name__ == "__main__":
    sys.exit(finish())
