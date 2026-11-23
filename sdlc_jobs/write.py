"""The `write` job: the Ashigaru App key, and no model.

Everything that changes GitHub on the fleet's behalf happens here, with an
installation token minted per step for this repository and the permissions
that step needs, and revoked after it:

  pull_request_target  posts what `ai` reviewed; applies Ashigaru's fix round
                       (the commit, its answers) through fix_publish.py
  issues               applies triage (triage_apply.py) and opens the
                       ashigaru/issue-N pull request (publish.py)
  workflow_dispatch    with VERSION, opens the release pull request
  push                 on a merge `check` planned as a release: verifies it
                       from the API, tags it, publishes the GitHub Release
  schedule             when `check` planned it: keeps the audit alert issue

A dispatch carrying DIGEST is sdlc's rollback. `write` has nothing to do
there and exits before it reads anything else: a rollback must never open a
release pull request for its VERSION.

What `ai` hands on is data, never instructions. The repository, the pull
request and its head come from this job's own event; a result naming another
is refused. A review is posted only as a COMMENT or REQUEST_CHANGES on this
head, so nothing a model wrote can become an approval.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from sdlc_jobs import _release
from sdlc_jobs import _tools as tools
from sdlc_jobs._app_token import app_token
from sdlc_jobs._github import (
    FOUND,
    OK,
    Api,
    CannotRunError,
    contract,
    repo_of,
    set_output,
)
from sdlc_jobs._handoff import from_env
from sdlc_jobs._tools import Site

# Per step, the least the App token may do.
REVIEW = {"pull_requests": "write", "contents": "write", "issues": "write"}
ISSUE = {"contents": "write", "issues": "write", "pull_requests": "write"}
RELEASE_PR = {"contents": "write", "pull_requests": "write"}
TAG = {"contents": "write", "pull_requests": "read"}
AUDIT = {"issues": "write"}
REVIEW_EVENTS = ("COMMENT", "REQUEST_CHANGES")
COMMENT_KEYS = ("path", "line", "side", "start_line", "start_side", "body", "subject_type")
MAX_ROUNDS = 5


@contract
def main(event_name: str, event: dict[str, Any], env: Mapping[str, str]) -> int:
    """`sdlc event`'s entry for the `write` job.

    Acts on `event` with the App's token as the module says, and returns 0,
    1 for a refusal (a release that does not verify, `ai`'s cap) or 2 when it
    could not run. With DIGEST set it does nothing at all and returns 0.
    """
    # Nothing is read before this test, the repository included: a rollback
    # dispatch is refused work, not work that failed.
    rollback = bool(env.get("DIGEST"))
    if not rollback:
        repo = repo_of(event, env)
        with tempfile.TemporaryDirectory(prefix="sdlc-write-") as scratch:
            job = Job(Site(repo, env, tools.run, Path(scratch)))
            return job.route(event_name, event)
    print("write: a dispatch with a digest is sdlc's rollback; nothing for write to do.")
    return OK


Mint = Callable[[Mapping[str, str]], AbstractContextManager[str]]


class Job:
    """One run of `write`: the site it acts on, and how it mints an App token.

    `mint` takes the permissions a step needs and returns a context manager
    that yields a token holding exactly those; the default is app_token, and
    a test passes a fake."""

    def __init__(self, site: Site, mint: Mint | None = None) -> None:
        self.site = site
        self.env, self.repo, self.runner, self.work = site.env, site.repo, site.runner, site.work
        self.api_for = site.api_for
        # How a token is minted for a set of permissions; a test passes a fake.
        self.mint: Mint = mint or (lambda perms: app_token(site.env, site.repo, perms))

    def route(self, event_name: str, event: Mapping[str, Any]) -> int:
        """The event's work. Outside the triage events, only what `check` planned."""
        direct = {
            "pull_request_target": self.pull_request,
            "issues": self.issue,
            "workflow_dispatch": self.release_pr,
        }
        handler = direct.get(event_name)
        if handler is not None:
            return handler(event)
        plan = check_plan(self.env)
        if event_name == "push" and plan.get("release"):
            return self.release(event, plan["release"])
        if event_name == "schedule" and plan.get("audit") is not None:
            return self.audit(plan["audit"])
        print(f"write: nothing planned for {event_name}")
        return OK

    # -- what ai handed on -------------------------------------------------------------------

    def handed(self, kind: str, number: int) -> dict[str, Any] | None:
        """`ai`'s result for this event, or None when there is nothing to act on.

        A result that is missing while `ai` succeeded is a job that cannot run;
        missing after `ai` failed is that failure, which `gate` already reports.
        """
        ai = self.env.get("SDLC_AI", "")
        try:
            result = from_env(self.env)
        except ValueError as err:
            if ai == "success":
                raise CannotRunError(f"ai succeeded but handed on nothing usable: {err}") from err
            print(f"write: ai {ai or 'did not run'}; nothing to post ({err})")
            return None
        if result.get("kind") != kind or result.get("number") != number:
            raise CannotRunError(
                f"ai's result is for {result.get('kind')} #{result.get('number')}, "
                f"not {kind} #{number}"
            )
        return result

    def too_large(self, api: Api, number: int, size: int) -> int:
        """`ai`'s cap was hit: a person takes it, told how big it was."""
        api.request(
            "POST", f"/repos/{self.repo}/issues/{number}/labels", {"labels": ["needs-human"]}
        )
        api.request(
            "POST",
            f"/repos/{self.repo}/issues/{number}/comments",
            {
                "body": f"Ashigaru's result here is {size:,} bytes encoded, over the 450,000 a run "
                "can hand on. Nothing was truncated or applied; this one is for a person."
            },
        )
        return FOUND

    # -- pull requests -----------------------------------------------------------------------

    def pull_request(self, event: Mapping[str, Any]) -> int:
        """Post `ai`'s review of this head, then apply its fix round if `ai` succeeded."""
        pr = event["pull_request"]
        number, head = int(pr["number"]), str(pr["head"]["sha"])
        result = self.handed("pr", number)
        if result is None:
            return OK
        if "too_large" not in result and result.get("head_sha") != head:
            raise CannotRunError(
                f"ai reviewed {result.get('head_sha')}, but this run's head is {head}"
            )
        with self.mint(REVIEW) as token:
            api = self.api_for(token)
            if "too_large" in result:
                return self.too_large(api, number, int(result["too_large"]))
            posted = self.post_reviews(
                api, number, head, (result.get("review") or {}).get("posts") or []
            )
            fix = result.get("fix") or {}
            if self.env.get("SDLC_AI") == "success" and fix.get("action") in ("fix", "cap"):
                self.apply_fix(api, token, pr, fix, posted)
        return OK

    def post_reviews(
        self, api: Api, number: int, head: str, posts: list[Any]
    ) -> list[tuple[int, list[dict[str, Any]]]]:
        """Post each recorded review; return each one's id and the comments it was sent."""
        done: list[tuple[int, list[dict[str, Any]]]] = []
        for payload in posts:
            review = clean_review(payload, head)
            made = api.request("POST", f"/repos/{self.repo}/pulls/{number}/reviews", review)
            done.append((int(made["id"]), review.get("comments", [])))
            print(f"write: posted review {made['id']} on {head[:7]}")
        return done

    def apply_fix(
        self,
        api: Api,
        token: str,
        pr: Mapping[str, Any],
        fix: Mapping[str, Any],
        posted: list[tuple[int, list[dict[str, Any]]]],
    ) -> None:
        """fix.yml's publish job: fix_publish.py on the PR branch, this run's findings resolved."""
        number, head, branch = int(pr["number"]), str(pr["head"]["sha"]), str(fix["branch"])
        if branch != pr["head"]["ref"]:
            raise CannotRunError(f"ai's fix is for {branch}, not {pr['head']['ref']}")
        now = api.request("GET", f"/repos/{self.repo}/git/ref/heads/{branch}")["object"]["sha"]
        if now != head:
            # A newer push started its own run, which reviews and fixes that head.
            print(f"write: {branch} moved from {head[:7]} to {now[:7]}; this round is stale")
            return
        ids = real_ids(api, self.repo, number, posted)
        collected, answer = resolve(dict(fix["findings"]), dict(fix.get("result") or {}), ids)
        findings = self.save("findings.json", json.dumps(collected))
        patch = self.save("change.patch", str(fix.get("patch") or ""))
        answer_path = self.save("result.json", json.dumps(answer))
        checkout = tools.clone(self.site, head, self.work / "pr", token=token, branch=branch)
        done, _ = tools.ashigaru(
            self.site,
            "fix_publish.py",
            [
                "--repo",
                self.repo,
                "--mode",
                str(fix["action"]),
                "--findings",
                str(findings),
                "--patch",
                str(patch),
                "--result",
                str(answer_path),
                "--branch",
                branch,
                "--max-rounds",
                str(MAX_ROUNDS),
            ],
            env=self.writer_env(token),
            cwd=checkout,
        )
        tools.checked(done, "fix_publish.py")

    # -- issues ------------------------------------------------------------------------------

    def issue(self, event: Mapping[str, Any]) -> int:
        """Apply `ai`'s triage and publish its patch as the ashigaru/issue-N pull request."""
        number = int(event["issue"]["number"])
        result = self.handed("issue", number)
        if result is None or ("skip" in result and "too_large" not in result):
            return OK
        with self.mint(ISSUE) as token:
            if "too_large" in result:
                return self.too_large(self.api_for(token), number, int(result["too_large"]))
            triage = result.get("triage")
            if triage:
                self.apply_triage(token, number, triage, str(result.get("trust", "")))
            code = result.get("code")
            if code and self.env.get("SDLC_AI") == "success":
                self.publish_code(token, number, code)
        return OK

    def apply_triage(self, token: str, number: int, triage: Mapping[str, Any], trust: str) -> None:
        """triage_apply.py with `ai`'s verdict, queue and trust; a failure is CannotRunError."""
        done, outputs = tools.ashigaru(
            self.site,
            "triage_apply.py",
            [
                "--repo",
                self.repo,
                "--issue",
                str(number),
                "--result",
                json.dumps(triage["verdict"]),
                "--queue",
                str(triage.get("queue", "fresh")),
                "--trust",
                trust,
            ],
            env=self.writer_env(token, git=False),
        )
        tools.checked(done, "triage_apply.py")
        print(f"write: triage of #{number}: {outputs.get('state', '?')}")

    def publish_code(self, token: str, number: int, code: Mapping[str, Any]) -> None:
        """publish.py on a fresh checkout of the base branch; a failure is CannotRunError."""
        base = str(code["base"])
        patch = self.save("change.patch", str(code.get("patch") or ""))
        answer = self.save("result.json", json.dumps(code.get("result") or {}))
        checkout = tools.clone(self.site, base, self.work / "repo", token=token, branch=base)
        done, outputs = tools.ashigaru(
            self.site,
            "publish.py",
            [
                "--repo",
                self.repo,
                "--issue",
                str(number),
                "--patch",
                str(patch),
                "--result",
                str(answer),
                "--base",
                base,
            ],
            env=self.writer_env(token),
            cwd=checkout,
        )
        tools.checked(done, "publish.py")
        print(f"write: issue #{number}: {outputs.get('pr') or 'no pull request'}")

    # -- releases and the audit --------------------------------------------------------------

    def release_pr(self, event: Mapping[str, Any]) -> int:
        """A `version` dispatch: open ashigaru/release-X.Y.Z from chore/release-X.Y.Z."""
        version = self.env.get("VERSION", "").strip()
        if not version:
            print("write: a dispatch without a version opens nothing")
            return OK
        with self.mint(RELEASE_PR) as token:
            return _release.open_release_pr(
                self.api_for(token), self.repo, version, default_branch(event)
            )

    def release(self, event: Mapping[str, Any], planned: Mapping[str, Any]) -> int:
        """A merge `check` planned as a release: verify, tag, publish, and output `release`."""
        sha = self.env.get("GITHUB_SHA") or str(event.get("after", ""))
        with self.mint(TAG) as token:
            code, release = _release.publish_release(
                self.site, token, planned, sha, default_branch(event)
            )
        if release is not None:
            set_output(self.env, "release", json.dumps(release, sort_keys=True))
        return code

    def audit(self, report: Mapping[str, Any]) -> int:
        """The weekly audit `check` planned: keep the one alert issue in step with the scan."""
        run_url = (
            f"{self.env.get('GITHUB_SERVER_URL', 'https://github.com')}/{self.repo}"
            f"/actions/runs/{self.env.get('GITHUB_RUN_ID', '')}"
        )
        with self.mint(AUDIT) as token:
            return _release.audit_issue(self.site, report, token, run_url)

    # -- shared ------------------------------------------------------------------------------

    def writer_env(self, token: str, *, git: bool = True) -> dict[str, str]:
        """An Ashigaru script's environment: the App token for gh and, where it pushes, for git."""
        return tools.child_env(self.env, GH_TOKEN=token, **(tools.git_auth(token) if git else {}))

    def save(self, name: str, text: str) -> Path:
        """Write `text` as UTF-8 to `name` in the job's scratch dir and return its path."""
        path = self.work / name
        path.write_text(text, encoding="utf-8")
        return path


def check_plan(env: Mapping[str, str]) -> dict[str, Any]:
    """`check`'s plan from SDLC_CHECK, a JSON object; anything else is CannotRunError."""
    try:
        plan = json.loads(env.get("SDLC_CHECK") or "{}")
    except json.JSONDecodeError as err:
        raise CannotRunError(f"check's plan is not JSON: {err}") from err
    if not isinstance(plan, dict):
        raise CannotRunError("check's plan is not an object")
    return plan


def default_branch(event: Mapping[str, Any]) -> str:
    """The event's repository.default_branch; an event without one is CannotRunError."""
    branch = (event.get("repository") or {}).get("default_branch")
    if not branch:
        raise CannotRunError("the event names no default branch")
    return str(branch)


def clean_review(payload: Any, head: str) -> dict[str, Any]:
    """A recorded review, reduced to what a Gatehouse review is, pinned to this head."""
    if not isinstance(payload, dict):
        raise CannotRunError("ai handed on a review that is not an object")
    event = payload.get("event") if payload.get("event") in REVIEW_EVENTS else "COMMENT"
    review: dict[str, Any] = {
        "event": event,
        "commit_id": head,
        "body": str(payload.get("body", "")),
    }
    comments = [
        {k: c[k] for k in COMMENT_KEYS if k in c}
        for c in payload.get("comments") or []
        if isinstance(c, dict) and c.get("path") and c.get("body")
    ]
    if comments:
        review["comments"] = comments
    return review


def real_ids(
    api: Api, repo: str, number: int, posted: list[tuple[int, list[dict[str, Any]]]]
) -> dict[int, int]:
    """Each pending id (-1, -2, ... in post order) mapped to the comment GitHub made for it.

    A review's comments come back in the order they were sent; each is also
    matched on path and body, and one that does not match is left unmapped
    rather than answered on the wrong thread.
    """
    ids: dict[int, int] = {}
    pending = 0
    for review_id, sent in posted:
        made = (
            api.pages(f"/repos/{repo}/pulls/{number}/reviews/{review_id}/comments") if sent else []
        )
        for i, comment in enumerate(sent):
            pending += 1
            if (
                i < len(made)
                and made[i].get("path") == comment.get("path")
                and made[i].get("body") == comment.get("body")
            ):
                ids[-pending] = int(made[i]["id"])
            else:
                print(f"::warning::finding {-pending} did not map to a posted comment; left open")
    return ids


def resolve(
    collected: dict[str, Any], answer: dict[str, Any], ids: Mapping[int, int]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """findings.json and the model's dispositions with pending ids swapped for real ones.

    A pending finding with no real comment, and any disposition of it, is
    dropped: fix_publish would otherwise reply to an id that does not exist.
    """

    def keep(item: Mapping[str, Any]) -> bool:
        """False for a pending finding that never got a real comment."""
        fid = item.get("id")
        return not (isinstance(fid, int) and fid < 0 and fid not in ids)

    def swap(item: Mapping[str, Any]) -> dict[str, Any]:
        """The item with a pending id replaced by its real one."""
        fid = item.get("id")
        return {**item, "id": ids.get(fid, fid)} if isinstance(fid, int) else dict(item)

    collected["findings"] = [swap(f) for f in collected.get("findings") or [] if keep(f)]
    answer["dispositions"] = [swap(d) for d in answer.get("dispositions") or [] if keep(d)]
    return collected, answer
