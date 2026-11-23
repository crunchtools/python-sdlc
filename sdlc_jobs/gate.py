"""The `gate` job: the one required check, `sdlc / gate (pull_request_target)`.

It holds a GITHUB_TOKEN with `contents: read`, `pull-requests: write` and
`actions: write`, and no secret, no App key and no socket. That is why it can be
required: nothing in it can be taken, and it runs the base definition of ci.yml
and a released sdlc, never the pull request's tree.

Three questions, all asked on every run so one run says everything wrong:

  guard   a fork's pull request may not touch `.github/workflows/` or `.sdlc/`
  needs   `ai` and `write` both succeeded for this head. A skipped job is a
          failure, never a benign one: `always()` in ci.yml exists to make this
          judgement run, not to let it pass
  triage  Gatehouse reviewed this head to the end, and every required finding
          has an answer in a form we can verify

Triage is a port of gatehouse's triage.yml, which was jq. Its judgements are
kept as they were, because they were each paid for by a real pull request:
the newest review of the head decides (#161), no review at all is a failure
(#77, a1073), a rebuilt branch orphans findings only once a finished review has
re-read the new head, and an AI fix does not get to answer its own finding.

What differs, on purpose: the reviewer is the Ashigaru App. `write` posts
Gatehouse's review with the App's token now, and a pull request's own
`pull_request` run can post as github-actions[bot] but never as the App, so a
review by any other account is not a review.

A collaborator's reply re-runs this job inside the `pull_request_target` run
(the second half), because only that run's check counts toward branch rules.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from typing import Any

from sdlc_jobs._github import APP_LOGIN, FOUND, OK, Api, ApiError, GitHub, contract, repo_of

GATE_JOB = "sdlc / gate (pull_request_target)"
WORKFLOW_PATH = ".github/workflows/ci.yml"
GUARDED = (".github/workflows/", ".sdlc/")
GUARD_MARKER = "<!-- sdlc gate: workflow guard -->"
GUARD_COMMENT = (
    "🚫 **Workflow changes are not accepted via pull request.** "
    "Please open an issue and a maintainer will make the change."
)
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
REQUIRED_LOW_AGENTS = ("Bug Hunter", "Security Scan")

# The Ashigaru App posts Gatehouse's review, so it is the reviewer, and it is
# also the account whose replies never answer a finding (a1073): both are the
# one login.
REVIEWER = APP_LOGIN

REVIEW_FAILED = "<!-- gatehouse review failed -->"
MIN_REASON = 20  # characters a `not a bug:` reason needs, after whitespace collapse
POLLS, POLL_SECONDS = 60, 5
FORBIDDEN = 403
UNPROVEN = (FORBIDDEN, 404, 410)  # statuses that leave a deferred issue unproven

_CARRIED = re.compile(r"<!-- gatehouse carried ([0-9a-f ]*) -->")
_SHA40 = re.compile(r"[0-9a-f]{40}")
_FIXED = re.compile(r"fixed in (?P<s>[0-9a-f]{7,40})\b")
_NOT_A_BUG = re.compile(r"not a bug:\s*", re.IGNORECASE)
_DEFERRED = re.compile(
    r"deferred:\s*(?P<u>https://github\.com/(?P<o>[A-Za-z0-9_.-]+)/(?P<r>[A-Za-z0-9_.-]+)"
    r"/issues/(?P<n>[0-9]+))",
    re.IGNORECASE,
)


def _login(item: Mapping[str, Any]) -> str:
    return (item.get("user") or {}).get("login") or ""


def _escape(value: str, *, prop: bool = False) -> str:
    """Workflow-command escaping, so a comment's text cannot start a command."""
    value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return value.replace(":", "%3A").replace(",", "%2C") if prop else value


# --- guard ------------------------------------------------------------------


def _guard(api: Api, repo: str, pr: Mapping[str, Any]) -> str | None:
    """Why a fork's pull request is rejected, or None if it may proceed.

    Gated on where the branch lives, not on the author's role: a branch pushed
    into this repo already needed write access, and a fork is what
    pull_request_target guards against. It reads the changed-file LIST and never
    the files, so nothing the pull request wrote is executed.
    """
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    if head_repo == repo:
        print(f"guard: head branch is in {repo}; allowed.")
        return None
    number = pr["number"]
    paths: list[str] = []
    for changed in api.pages(f"/repos/{repo}/pulls/{number}/files"):
        # Both sides of a rename: a fork could move a file out of a guarded
        # directory unseen if only the new path were read.
        paths += [p for p in (changed.get("filename"), changed.get("previous_filename")) if p]
    touched = sorted({p for p in paths if p.startswith(GUARDED)})
    if not touched:
        print("guard: no workflow files changed.")
        return None
    comments = api.pages(f"/repos/{repo}/issues/{number}/comments")
    if not any(GUARD_MARKER in (c.get("body") or "") for c in comments):
        # Once. The marker is what a re-run looks for, so a rebuilt check does
        # not post the explanation again.
        api.request(
            "POST",
            f"/repos/{repo}/issues/{number}/comments",
            {"body": f"{GUARD_COMMENT}\n\n{GUARD_MARKER}"},
        )
    return f"a fork may not change {', '.join(touched[:5])}"


# --- needs ------------------------------------------------------------------


def _needs(env: Mapping[str, str]) -> list[str]:
    """Every needed job that did not succeed, named with its result."""
    return [
        f"{name} job result is {env.get(var) or 'unset'}, not success"
        for name, var in (("ai", "SDLC_AI"), ("write", "SDLC_WRITE"))
        if env.get(var) != "success"
    ]


# --- triage -----------------------------------------------------------------


def _is_release_pr(pr: Mapping[str, Any], repo: str) -> bool:
    """An Ashigaru release PR: review skips it without posting, so none is needed."""
    head = pr.get("head") or {}
    return (
        str(head.get("ref") or "").startswith("ashigaru/release-")
        and (head.get("repo") or {}).get("full_name") == repo
        and _login(pr) == APP_LOGIN
    )


def _review_problem(reviews: list[Any], head: str, *, release: bool) -> tuple[str, bool]:
    """(why the head has no finished review, or "", whether a finished one read it).

    The newest Gatehouse review OF THE HEAD decides, not the newest of all:
    pushes are reviewed concurrently and an older head's slower review can post
    after the head's (#161). With none of the head, triage fails (a1073), save
    for the release PR. The second value gates dropping orphaned findings.
    """
    mine = [
        r
        for r in reviews
        if _login(r) == REVIEWER and (r.get("body") or "").startswith("Gatehouse")
    ]
    on_head = [r for r in mine if (r.get("commit_id") or "").lower() == head.lower()]
    finished = [r for r in on_head if REVIEW_FAILED not in (r.get("body") or "")]
    fresh = bool(finished)
    if on_head:
        if REVIEW_FAILED in (on_head[-1].get("body") or ""):
            return f"the review of it failed: {on_head[-1].get('html_url')}", False
        return "", fresh
    if release:
        return "", False
    if mine:
        return f"only an older head was reviewed: {mine[-1].get('html_url')}", False
    return "no review of it was posted", False


def _carried(reviews: list[Any]) -> list[str]:
    """Commits a skipped review says its head carries over from an earlier head.

    A rebase orphans them, but the same change is on the PR, so they count as
    the PR's: findings written on them, and `fixed in` answers naming them,
    carry forward. Only the reviewer's own reviews are read, and only full SHAs
    are taken from them.
    """
    found: set[str] = set()
    for review in reviews:
        body = review.get("body") or ""
        if _login(review) != REVIEWER or not body.startswith("Gatehouse"):
            continue
        for match in _CARRIED.finditer(body):
            found.update(s for s in match.group(1).split(" ") if _SHA40.fullmatch(s))
    return sorted(found)


def _deferred_refs(comments: list[Any]) -> set[tuple[str, str, str]]:
    """The issues `deferred:` replies name. The pattern admits only name characters."""
    refs = set()
    # Replies that may answer: not the reviewer's, and not the App's (one account).
    for reply in comments:
        if reply.get("in_reply_to_id") is None or _login(reply) == REVIEWER:
            continue
        found = _DEFERRED.match((reply.get("body") or "").lstrip())
        if found:
            refs.add((found["o"], found["r"], found["n"]))
    return refs


def _open_issues(api: Api, refs: set[tuple[str, str, str]]) -> set[str]:
    """URLs of the named issues that are open and not pull requests.

    An issue this token may not see (403, 404: a private repository, a
    deleted issue) is unproven, so the thread stays unanswered, as triage.yml
    had it; the warning says why. A server or network failure is not a
    verdict on the reply, so it propagates and the gate cannot run (exit 2).
    """
    urls: set[str] = set()
    for owner, name, number in sorted(refs):
        try:
            issue = api.request("GET", f"/repos/{owner}/{name}/issues/{number}")
        except ApiError as err:
            if err.status not in UNPROVEN:
                raise
            print(
                f"::warning::could not look up {owner}/{name}#{number}; a deferred: reply "
                f"naming it does not count ({err})"
            )
            continue
        if issue.get("state") == "open" and issue.get("pull_request") is None:
            urls.add(str(issue.get("html_url") or "").lower().rstrip("/"))
    return urls


def _answers(body: str, commits: list[str], open_urls: set[str]) -> bool:
    """Is this reply one of the three verifiable forms?

    `fixed in` and `deferred:` read the first line; `not a bug:` reads the whole
    body, as triage.yml always did, so a reason may run on past the first line.
    """
    body = body.lstrip()
    line = body.split("\n", 1)[0].lower()
    fixed = _FIXED.match(line)
    if fixed and any(c.startswith(fixed["s"]) for c in commits):
        return True
    if body[:10].lower() == "not a bug:":
        reason = re.sub(r"\s+", " ", _NOT_A_BUG.sub("", body, count=1)).removesuffix(" ")
        if len(reason) >= MIN_REASON:
            return True
    deferred = _DEFERRED.match(line)
    return bool(deferred and deferred["u"].lower().rstrip("/") in open_urls)


def _required(first_line: str) -> bool:
    """CRITICAL, HIGH, MEDIUM always; LOW only from the agents that must be answered."""
    if not first_line.startswith("**LOW** ("):
        return True
    return any(first_line.startswith(f"**LOW** ({a}):") for a in REQUIRED_LOW_AGENTS)


def _unanswered(
    comments: list[Any], commits: list[str], open_urls: set[str], *, fresh: bool
) -> list[dict[str, Any]]:
    """The required findings with no answer, as {url, path, line, finding}."""
    commits = [c.lower() for c in commits]
    answered = {
        c["in_reply_to_id"]
        for c in comments
        if c.get("in_reply_to_id") is not None
        and _login(c) != REVIEWER
        and _answers(c.get("body") or "", commits, open_urls)
    }
    out = []
    for c in comments:
        if _login(c) != REVIEWER or c.get("in_reply_to_id") is not None:
            continue
        written = (c.get("original_commit_id") or "").lower()
        # A rebuilt branch orphans the commit a finding was written against, and
        # with it every answer naming it. Dropped only once a finished review has
        # re-read the head: otherwise a force-push of the same code would shed a
        # finding with nothing having re-read what it was about. A comment with no
        # original commit cannot be shown orphaned and still counts.
        if written and written not in commits and fresh:
            continue
        first = (c.get("body") or "").split("\n", 1)[0]
        if c.get("id") in answered or not _required(first):
            continue
        out.append(
            {
                "url": c.get("html_url"),
                "path": c.get("path"),
                "line": c.get("line") or c.get("original_line"),
                "finding": first,
            }
        )
    return out


def _triage(api: Api, repo: str, pr: Mapping[str, Any]) -> list[str]:
    """Why the head's review and findings do not clear, as a list of failures."""
    number, head = pr["number"], (pr.get("head") or {}).get("sha") or ""
    base = f"/repos/{repo}/pulls/{number}"
    reviews = api.pages(f"{base}/reviews")
    problem, fresh = _review_problem(reviews, head, release=_is_release_pr(pr, repo))
    if problem:
        return [f"no finished Gatehouse review of {head[:7]}: {problem}"]
    comments = api.pages(f"{base}/comments")
    commits = [c["sha"] for c in api.pages(f"{base}/commits")] + _carried(reviews)
    open_urls = _open_issues(api, _deferred_refs(comments))
    findings = _unanswered(comments, commits, open_urls, fresh=fresh)
    for f in findings:
        print(
            f"::error file={_escape(str(f['path']), prop=True)},line={f['line']}"
            f"::Unanswered Gatehouse finding: {_escape(str(f['url']))}"
        )
    return [f"{len(findings)} unanswered Gatehouse finding(s)"] if findings else []


# --- the two events ---------------------------------------------------------


def _is_ours(run: Mapping[str, Any], head_repo: str) -> bool:
    path = str(run.get("path") or "")
    return path.split("@", 1)[0] == WORKFLOW_PATH and (
        (run.get("head_repository") or {}).get("full_name") == head_repo
    )


def _retriage(
    api: Api, event: Mapping[str, Any], env: Mapping[str, str], sleep: Callable[[float], None]
) -> int:
    """Re-run `gate` inside the pull_request_target run, where its result counts.

    A reply's own run is only a doorbell: its checks attach to the head but the
    merge box and the ruleset ignore them (gatehouse#47, #50). Gate is
    deterministic, so a spurious re-run costs seconds and changes nothing.
    """
    if (event.get("comment") or {}).get("author_association") not in TRUSTED_ASSOCIATIONS:
        print("retriage: the commenter is not an owner, member or collaborator; nothing to do.")
        return OK
    repo, pr = repo_of(event, env), event["pull_request"]
    sha, head_repo = pr["head"]["sha"], pr["head"]["repo"]["full_name"]
    runs = api.pages(
        f"/repos/{repo}/actions/runs?event=pull_request_target&head_sha={sha}", key="workflow_runs"
    )
    ours = [r for r in runs if _is_ours(r, head_repo)]
    if not ours:
        print(
            f"::notice::no pull_request_target run of {WORKFLOW_PATH} for {sha}; nothing to re-run."
        )
        return OK
    run_id = max(ours, key=lambda r: r.get("run_number") or 0)["id"]
    # A run still in progress will gate on its own when it gets there, and GitHub
    # refuses to re-run a job until its run has completed.
    status = ""
    for _ in range(POLLS):
        status = api.request("GET", f"/repos/{repo}/actions/runs/{run_id}").get("status")
        if status == "completed":
            break
        sleep(POLL_SECONDS)
    if status != "completed":
        print(f"::error::run {run_id} is still {status} after {POLLS * POLL_SECONDS} seconds.")
        return FOUND
    jobs = api.pages(f"/repos/{repo}/actions/runs/{run_id}/jobs", key="jobs")
    job = next((j for j in jobs if j.get("name") == GATE_JOB), None)
    if job is None:
        print(f"::error::no job named '{GATE_JOB}' in run {run_id}.")
        return FOUND
    try:
        api.request("POST", f"/repos/{repo}/actions/jobs/{job['id']}/rerun")
    except ApiError as err:
        if err.status != FORBIDDEN:
            raise
        # A fork's pull request token is read-only, so this is expected there.
        print(
            f"::error::this token cannot re-run '{GATE_JOB}' (HTTP 403, a fork's token is "
            f"read-only): a maintainer must re-run gate in run {run_id}."
        )
        return FOUND
    print(f"re-ran '{GATE_JOB}' (job {job['id']}) in run {run_id}.")
    return OK


@contract
def main(
    event_name: str,
    event: dict[str, Any],
    env: Mapping[str, str],
    *,
    api: Api | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """`sdlc event`'s entry. `api` and `sleep` are for tests; the job passes neither."""
    api = api or GitHub(env.get("GITHUB_TOKEN", ""))
    if event_name == "pull_request_review_comment":
        return _retriage(api, event, env, sleep)
    if event_name != "pull_request_target":
        print(f"gate: nothing to do for {event_name}.")
        return OK
    repo, pr = repo_of(event, env), event["pull_request"]
    failures = {
        "guard": _guard(api, repo, pr),
        "needs": "; ".join(_needs(env)) or None,
        "triage": "; ".join(_triage(api, repo, pr)) or None,
    }
    for part, why in failures.items():
        print(f"gate {part}: {'FAIL: ' + why if why else 'ok'}")
        if why:
            print(f"::error::gate {part}: {_escape(why)}")
    return FOUND if any(failures.values()) else OK
