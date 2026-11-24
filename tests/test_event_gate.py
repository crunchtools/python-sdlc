#!/usr/bin/env python3
"""sdlc_jobs/gate.py, against a fake GitHub.

The gate is the required check, so each way it can pass is pinned along with
the way it must not:

  - a fork may not touch .github/workflows/ or .sdlc/, either side of a rename,
    and is told once; a same-repo head is allowed
  - ai and write must both have succeeded: skipped is a failure
  - triage is the port of gatehouse's triage.yml, case by case, with the
    Ashigaru App as the one reviewer (a github-actions[bot] review is no review)
  - a collaborator's reply re-runs gate inside the pull_request_target run, and
    a fork's read-only token is a verdict (1), not a crash (2)

Run: python3 tests/test_event_gate.py
"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path
from typing import Any

import sdlc_root
from fake_github import FakeGitHub, check, finish

from sdlc_jobs import gate
from sdlc_jobs._github import APP_LOGIN, CANNOT_RUN, ApiError

REPO = "o/r"
HEAD = "f" * 40
SHA = "0123456789abcdef0123456789abcdef01234567"
ISSUE = "https://github.com/o/r/issues/90"
APP = APP_LOGIN
CLEAN = "Gatehouse found no issues.\n\n<!-- gatehouse review complete -->"
FAILED = "Gatehouse could not review this PR.\n\n<!-- gatehouse review failed -->"
OK_ENV = {"GITHUB_REPOSITORY": REPO, "SDLC_AI": "success", "SDLC_WRITE": "success"}


def pr_event(
    head_repo: str = REPO, ref: str = "feat/x", author: str = "dev", head: str = HEAD
) -> dict[str, Any]:
    return {
        "pull_request": {
            "number": 7,
            "user": {"login": author},
            "head": {"sha": head, "ref": ref, "repo": {"full_name": head_repo}},
        }
    }


def review(rid: int, body: str = CLEAN, user: str = APP, sha: str = HEAD) -> dict[str, Any]:
    return {
        "id": rid,
        "user": {"login": user},
        "body": body,
        "commit_id": sha,
        "html_url": f"https://x/r/{rid}",
    }


def finding(cid: int, header: str = "**HIGH** (Bug Hunter)", **kw: Any) -> dict[str, Any]:
    return {
        "id": cid,
        "user": {"login": APP},
        "in_reply_to_id": None,
        "html_url": f"https://x/c/{cid}",
        "path": "src/a.py",
        "line": 10,
        "original_line": 10,
        "body": f"{header}: finding {cid}\nmore",
        **kw,
    }


def reply(
    cid: int, to: int, body: str, user: str = "maint", assoc: str = "MEMBER"
) -> dict[str, Any]:
    return {
        "id": cid,
        "user": {"login": user},
        "in_reply_to_id": to,
        "body": body,
        "author_association": assoc,
    }


def issue(state: str = "open", *, pull: bool = False, url: str = ISSUE) -> dict[str, Any]:
    return {"state": state, "html_url": url, "pull_request": {"url": "x"} if pull else None}


def pull_api(**knobs: Any) -> FakeGitHub:
    """A fake API for one pull request. Knobs, all optional: reviews, comments, commits
    (a tuple of shas), files, issues (number -> issue) and issue_comments."""
    routes: dict[tuple[str, str], Any] = {
        ("GET", "/repos/o/r/pulls/7/reviews"): [review(1)]
        if knobs.get("reviews") is None
        else knobs["reviews"],
        ("GET", "/repos/o/r/pulls/7/comments"): knobs.get("comments") or [],
        ("GET", "/repos/o/r/pulls/7/commits"): [
            {"sha": sha} for sha in knobs.get("commits", (SHA,))
        ],
        ("GET", "/repos/o/r/pulls/7/files"): knobs.get("files") or [],
        ("GET", "/repos/o/r/pulls/7"): {
            "changed_files": knobs.get("changed_files", len(knobs.get("files") or []))
        },
        ("GET", "/repos/o/r/issues/7/comments"): knobs.get("issue_comments") or [],
        ("POST", "/repos/o/r/issues/7/comments"): {},
    }
    for num, value in (knobs.get("issues") or {}).items():
        routes[("GET", f"/repos/o/r/issues/{num}")] = value
    return FakeGitHub(routes)


def run(
    api: FakeGitHub | None, name: str, event: dict[str, Any], env: dict[str, str] | None = None
) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = gate.main(
            name, event, OK_ENV if env is None else env, api=api, sleep=lambda _s: None
        )
    return code, out.getvalue()


def gate_pr(
    api: FakeGitHub, event: dict[str, Any] | None = None, env: dict[str, str] | None = None
) -> int:
    return run(api, "pull_request_target", event or pr_event(), env)[0]


def answered(body: str, **kw: Any) -> bool:
    api = pull_api(comments=[finding(1), reply(2, 1, body)], **kw)
    return gate_pr(api) == 0


# --- guard ------------------------------------------------------------------

fork = pr_event(head_repo="evil/r")
touch = [{"filename": "README.md"}, {"filename": ".github/workflows/ci.yml"}]
api = pull_api(files=touch)
check("fork touching workflows fails", gate_pr(api, fork) == 1)
posted = api.made("POST", "/repos/o/r/issues/7/comments")
check("exactly one comment posted", len(posted) == 1)
check(
    "comment has text and marker",
    gate.GUARD_MARKER in posted[0][2]["body"]
    and "Workflow changes are not accepted" in posted[0][2]["body"],
)
api = pull_api(files=touch, issue_comments=[{"body": f"x\n{gate.GUARD_MARKER}"}])
check(
    "re-run does not comment again",
    gate_pr(api, fork) == 1 and not api.made("POST", "/repos/o/r/issues/*"),
)
api = pull_api(files=[{"filename": "docs/a.md", "previous_filename": ".sdlc/launcher"}])
check("rename out of .sdlc/ is caught", gate_pr(api, fork) == 1)
check("fork with ordinary files passes", gate_pr(pull_api(files=[{"filename": "a.py"}]), fork) == 0)
# GitHub's files endpoint stops at 3,000 entries: filler sorting before
# .github/ would push ci.yml off the list. A short list is a rejection.
capped = [{"filename": f"-{i:04d}"} for i in range(3000)]
code, said = run(pull_api(files=capped, changed_files=3001), "pull_request_target", fork)
check(
    "fork PR whose file list is capped short of changed_files fails the guard",
    code == 1 and "lists only 3000" in said,
)
check(
    "a same-repo PR is not read at all, capped or not",
    gate_pr(pull_api(files=capped, changed_files=3001)) == 0,
)
api = pull_api(files=touch)
check(
    "same-repo head may change workflows",
    gate_pr(api, pr_event()) == 0 and not api.made("GET", "*files"),
)

# --- needs ------------------------------------------------------------------

for var in ("SDLC_AI", "SDLC_WRITE"):
    for result in ("skipped", "failure", "cancelled", ""):
        code, text = run(pull_api(), "pull_request_target", pr_event(), {**OK_ENV, var: result})
        check(f"{var}={result or 'unset'} fails", code == 1 and var.split("_")[1].lower() in text)

# --- triage: the review -----------------------------------------------------

check("clean review passes", gate_pr(pull_api()) == 0)
code, text = run(pull_api(reviews=[]), "pull_request_target", pr_event())
check("no review on head fails", code == 1 and "no review of it was posted" in text)
check("failed marker fails", gate_pr(pull_api(reviews=[review(1, FAILED)])) == 1)
check(
    "newest review of the head decides",
    gate_pr(pull_api(reviews=[review(1, FAILED), review(2)])) == 0
    and gate_pr(pull_api(reviews=[review(1), review(2, FAILED)])) == 1,
)
old = "a" * 40
check(
    "older head's review does not pass the head",
    gate_pr(pull_api(reviews=[review(1, sha=old)])) == 1,
)
check(
    "older head's later failed review does not hold the head",
    gate_pr(pull_api(reviews=[review(1), review(2, FAILED, sha=old)])) == 0,
)
check(
    "a github-actions[bot] review is not a review",
    gate_pr(pull_api(reviews=[review(1, user="github-actions[bot]")])) == 1,
)
check(
    "a review not starting Gatehouse is not a review",
    gate_pr(pull_api(reviews=[review(1, "LGTM")])) == 1,
)
check(
    "head sha compared case-insensitively",
    gate_pr(pull_api(reviews=[review(1, sha=HEAD.upper())])) == 0,
)
rel = pr_event(ref="ashigaru/release-1.2.3", author=APP)
check("release PR with no review of head passes", gate_pr(pull_api(reviews=[]), rel) == 0)
check(
    "release PR with a failed review fails",
    gate_pr(pull_api(reviews=[review(1, FAILED)]), rel) == 1,
)
check(
    "release lookalike from a fork is not exempt",
    gate_pr(pull_api(reviews=[]), pr_event("evil/r", "ashigaru/release-1", APP)) == 1,
)
check(
    "release branch by another author is not exempt",
    gate_pr(pull_api(reviews=[]), pr_event(ref="ashigaru/release-1", author="dev")) == 1,
)

# --- triage: answers --------------------------------------------------------

check("unanswered finding fails", gate_pr(pull_api(comments=[finding(1)])) == 1)
api = pull_api(comments=[finding(1)])
code, text = run(api, "pull_request_target", pr_event())
check(
    "annotation names file and line",
    "::error file=src%2Fa.py".replace("%2F", "/") + ",line=10::" in text,
)
good = {
    "fixed in full": f"fixed in {SHA}",
    "fixed in short, capitalised": f"Fixed in {SHA[:7]}: moved the check",
    "fixed in upper": f"fixed in {SHA.upper()}",
    "not a bug": "not a bug: the value is validated in parse() before it gets here",
    "not a bug, reason on later line": "Not a bug:\nthe caller holds the lock for the whole loop",
    "deferred": f"deferred: {ISSUE}",
    "deferred, indented, trailing": f"  Deferred: {ISSUE} (needs a schema change)",
}
for label, body in good.items():
    check(f"answer ok: {label}", answered(body, issues={"90": issue()}))
bad = {
    "won't fix": "won't fix",
    "ok": "ok",
    "not a bug, short": "not a bug: intended",
    "not a bug, blank": "not a bug:" + " " * 30,
    "fixed in a stranger": "fixed in abcdef0",
    "fixed in too short": "fixed in 0123",
    "form must lead": f"thanks, fixed in {SHA}",
    "deferred, not open": "deferred: https://github.com/o/r/issues/91",
    "deferred, no url": "deferred: later",
    "deferred to a PR": f"deferred: {ISSUE.replace('issues', 'pull')}",
}
for label, body in bad.items():
    check(
        f"answer rejected: {label}",
        not answered(body, issues={"90": issue(), "91": issue("closed")}),
    )
check(
    "deferred to a closed issue", not answered(f"deferred: {ISSUE}", issues={"90": issue("closed")})
)
check(
    "deferred to a pull request",
    not answered(f"deferred: {ISSUE}", issues={"90": issue(pull=True)}),
)
check(
    "deferred lookup error is unanswered, not a crash",
    not answered(f"deferred: {ISSUE}", issues={"90": ApiError("GET", "/x", 404, "gone")}),
)
for status in (0, 502):
    outage = pull_api(
        comments=[finding(1), reply(2, 1, f"deferred: {ISSUE}")],
        issues={"90": ApiError("GET", "/x", status, "outage")},
    )
    check(
        f"deferred lookup HTTP {status} cannot run (2), not a verdict",
        gate_pr(outage) == CANNOT_RUN,
    )
check("fixed in needs a commit on the PR", not answered(f"fixed in {SHA}", commits=()))
check(
    "a reply among chatter answers",
    gate_pr(pull_api(comments=[finding(1), reply(2, 1, "looking"), reply(3, 1, f"fixed in {SHA}")]))
    == 0,
)
check(
    "reviewer replying to itself does not answer",
    gate_pr(pull_api(comments=[finding(1), reply(2, 1, f"fixed in {SHA}", APP)])) == 1,
)
for assoc in ("CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE", ""):
    check(
        f"a {assoc or 'missing'} author's reply answers nothing (C3)",
        gate_pr(pull_api(comments=[finding(1), reply(2, 1, f"fixed in {SHA}", "fork", assoc)]))
        == 1,
    )
check(
    "one answered, one not: fails",
    gate_pr(pull_api(comments=[finding(1), finding(2), reply(3, 2, f"fixed in {SHA}")])) == 1,
)
check(
    "a human's own top-level comment is not a finding",
    gate_pr(pull_api(comments=[{**finding(1), "user": {"login": "maint"}}])) == 0,
)
check(
    "outdated finding reports original_line",
    "line=10::"
    in run(pull_api(comments=[finding(1, line=None)]), "pull_request_target", pr_event())[1],
)

# --- triage: LOW ------------------------------------------------------------

for agent in ("Bug Hunter", "Security Scan"):
    check(
        f"LOW from {agent} needs an answer",
        gate_pr(pull_api(comments=[finding(1, f"**LOW** ({agent})")])) == 1,
    )
for agent in ("Documentation", "Test Coverage", "Consistency Check"):
    check(
        f"LOW from {agent} is advisory",
        gate_pr(pull_api(comments=[finding(1, f"**LOW** ({agent})")])) == 0,
    )
check(
    "MEDIUM from an advisory agent still counts",
    gate_pr(pull_api(comments=[finding(1, "**MEDIUM** (Documentation)")])) == 1,
)
quoted = finding(1)
quoted["body"] += "\n**LOW** (Documentation): quoted"
check("a LOW on a later line does not make it advisory", gate_pr(pull_api(comments=[quoted])) == 1)

# --- triage: orphans and carried commits ------------------------------------

orphan = finding(1, original_commit_id="a" * 40)
check(
    "orphaned finding dropped when head freshly reviewed", gate_pr(pull_api(comments=[orphan])) == 0
)
check(
    "orphaned finding still blocks when the head review is not a finished one",
    gate_pr(pull_api(reviews=[], comments=[orphan]), rel) == 1,
)
check(
    "finding still on the PR blocks",
    gate_pr(pull_api(comments=[finding(1, original_commit_id=SHA.upper())])) == 1,
)
check("finding with no original commit blocks", gate_pr(pull_api(comments=[finding(1)])) == 1)
carried = "b" * 40
skip = review(
    2,
    f"Gatehouse skipped this review.\n\n<!-- gatehouse carried {carried} {'c' * 40} {'d' * 39} -->",
)
check(
    "carried commit keeps its finding alive",
    gate_pr(pull_api(reviews=[review(1), skip], comments=[finding(1, original_commit_id=carried)]))
    == 1,
)
check(
    "carried commit honours `fixed in`",
    gate_pr(
        pull_api(
            reviews=[review(1), skip],
            comments=[
                finding(1, original_commit_id=carried),
                reply(2, 1, f"fixed in {carried[:9]}"),
            ],
        )
    )
    == 0,
)
check(
    "carried markers from a stranger are ignored",
    gate_pr(
        pull_api(
            reviews=[review(1), {**skip, "user": {"login": "mallory"}}],
            comments=[finding(1, original_commit_id=SHA), reply(2, 1, f"fixed in {carried}")],
        )
    )
    == 1,
)

# --- all three parts reported every run -------------------------------------

code, text = run(
    pull_api(files=touch, reviews=[]), "pull_request_target", fork, {**OK_ENV, "SDLC_AI": "failure"}
)
check(
    "guard, needs and triage all reported",
    code == 1 and all(f"gate {p}: FAIL" in text for p in ("guard", "needs", "triage")),
)

# --- retriage ---------------------------------------------------------------


def comment_event(assoc: str = "MEMBER", head_repo: str = REPO) -> dict[str, Any]:
    return {**pr_event(head_repo), "comment": {"author_association": assoc}}


def run_row(
    rid: int, number: int, path: str = ".github/workflows/ci.yml", head_repo: str = REPO
) -> dict[str, Any]:
    return {
        "id": rid,
        "run_number": number,
        "path": path,
        "head_repository": {"full_name": head_repo},
    }


def retriage_api(
    runs: list[Any], status: Any = "completed", jobs: list[Any] | None = None, rerun: Any = None
) -> FakeGitHub:
    return FakeGitHub(
        {
            ("GET", "/repos/o/r/actions/runs"): {"workflow_runs": runs},
            ("GET", "/repos/o/r/actions/runs/*/jobs"): {
                "jobs": jobs if jobs is not None else [{"id": 55, "name": gate.GATE_JOB}]
            },
            ("GET", "/repos/o/r/actions/runs/*"): status
            if callable(status)
            else {"status": status},
            ("POST", "/repos/o/r/actions/jobs/*/rerun"): {} if rerun is None else rerun,
        }
    )


def reply_run(
    api: FakeGitHub, event: dict[str, Any] | None = None, sleeps: list[float] | None = None
) -> int:
    log = [] if sleeps is None else sleeps
    with contextlib.redirect_stdout(io.StringIO()):
        return gate.main(
            "pull_request_review_comment",
            event or comment_event(),
            OK_ENV,
            api=api,
            sleep=log.append,
        )


for assoc in ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", ""):
    api = retriage_api([run_row(1, 1)])
    check(
        f"association {assoc or 'missing'} does nothing",
        reply_run(api, comment_event(assoc)) == 0 and not api.calls,
    )
# A literal, not gate.TRUSTED_ASSOCIATIONS: CodeQL reads "trusted" in a name as
# a secret and flags it reaching check()'s print. The pin keeps the two in step.
RERUN_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
check("re-run associations pinned", gate.TRUSTED_ASSOCIATIONS == RERUN_ASSOCIATIONS)
for assoc in RERUN_ASSOCIATIONS:
    api = retriage_api([run_row(1, 1)])
    check(
        f"association {assoc} re-runs",
        reply_run(api, comment_event(assoc)) == 0 and len(api.made("POST")) == 1,
    )

api = retriage_api(
    [
        run_row(10, 3),
        run_row(11, 9, ".github/workflows/ci.yml@refs/heads/main"),
        run_row(12, 12, ".github/workflows/other.yml"),
        run_row(13, 15, head_repo="evil/r"),
        run_row(14, 5),
    ]
)
check(
    "newest matching run is chosen",
    reply_run(api) == 0 and api.made("GET", "/repos/o/r/actions/runs/11"),
)
check(
    "run search names event and head sha",
    any("event=pull_request_target&head_sha=" + HEAD in c[1] for c in api.calls),
)
api = retriage_api(
    [run_row(12, 12, ".github/workflows/other.yml"), run_row(13, 15, head_repo="evil/r")]
)
check("no matching run: exit 0, nothing posted", reply_run(api) == 0 and not api.made("POST"))
fork_ev = comment_event(head_repo="evil/r")
api = retriage_api([run_row(13, 15, head_repo="evil/r"), run_row(14, 16)])
check(
    "run must come from the PR head repo",
    reply_run(api, fork_ev) == 0 and api.made("GET", "/repos/o/r/actions/runs/13"),
)

states = iter(["queued", "in_progress", "completed"])
sleeps: list[float] = []
api = retriage_api([run_row(1, 1)], status=lambda _b: {"status": next(states)})
check(
    "polls until completed",
    reply_run(api, sleeps=sleeps) == 0 and sleeps == [5, 5] and api.made("POST"),
)
sleeps = []
api = retriage_api([run_row(1, 1)], status="in_progress")
check(
    "polling timeout exits 1",
    reply_run(api, sleeps=sleeps) == 1 and len(sleeps) == gate.POLLS and not api.made("POST"),
)
api = retriage_api([run_row(1, 1)], jobs=[{"id": 9, "name": "sdlc / gate (pull_request)"}])
check("job missing exits 1", reply_run(api) == 1 and not api.made("POST"))
api = retriage_api([run_row(1, 1)], rerun=ApiError("POST", "/x", 403, "Resource not accessible"))
check("403 on rerun exits 1", reply_run(api) == 1)
api = retriage_api([run_row(1, 1)])
check(
    "success posts rerun of the gate job",
    reply_run(api) == 0 and api.made("POST")[0][1] == "/repos/o/r/actions/jobs/55/rerun",
)

# --- the rest ---------------------------------------------------------------

check("unknown event exits 0 doing nothing", run(FakeGitHub(), "push", {})[0] == 0)
check("workflow_dispatch exits 0", run(FakeGitHub(), "workflow_dispatch", {})[0] == 0)


# main is the contract's: an API error is exit 2, never a traceback.
check(
    "main: API error becomes exit 2",
    run(FakeGitHub(), "pull_request_target", pr_event())[0] == CANNOT_RUN,
)
boom = retriage_api([run_row(1, 1)], rerun=ApiError("POST", "/x", 500, "boom"))
check(
    "main: retriage 500 becomes exit 2",
    run(boom, "pull_request_review_comment", comment_event())[0] == CANNOT_RUN,
)
check("main: unknown event exits 0", run(FakeGitHub(), "schedule", {}, {})[0] == 0)
check(
    "main: no repository is exit 2",
    run(FakeGitHub(), "pull_request_target", pr_event(), {})[0] == CANNOT_RUN,
)
check(
    "main: with no API given it builds a real client, which a no-op event never calls",
    run(None, "schedule", {}, {})[0] == 0,
)
check(
    "the suite exercises this checkout's gate",
    Path(gate.__file__).resolve() == sdlc_root.ROOT / "sdlc_jobs" / "gate.py",
)

if __name__ == "__main__":
    sys.exit(finish())
