"""What a pull request's run says about a commit: its tree, the PR a push merged, its jobs.

`check` skips the gates on a default-branch push whose tree its merged pull request
already passed `check` on, and the release build stamps the tree it built from.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from sdlc_jobs._core import Tools, api, cannot

TREE_LABEL = "com.crunchtools.build.tree"
WORKFLOW = ".github/workflows/ci.yml"
CHECK_JOB = "sdlc / check (pull_request)"
TREE = re.compile(r"[0-9a-f]{40}")


def tree_of(tools: Tools, env: Mapping[str, str], repo: str, sha: str) -> str:
    """The tree of a commit of this repo, read from the API."""
    doc = api(tools, env, "GET", f"/repos/{repo}/git/commits/{sha}")
    tree = (doc.get("tree") or {}).get("sha") if isinstance(doc, dict) else None
    if not (isinstance(tree, str) and TREE.fullmatch(tree)):
        cannot(f"commit {sha} of {repo} has no readable tree")
    return tree


def merged_head(tools: Tools, env: Mapping[str, str], repo: str, sha: str) -> str | None:
    """The head commit of the one pull request this push merged; None for a direct push."""
    prs = api(tools, env, "GET", f"/repos/{repo}/commits/{sha}/pulls")
    merged = [p for p in prs if p.get("merged_at")] if isinstance(prs, list) else []
    head = str((merged[0].get("head") or {}).get("sha", "")) if len(merged) == 1 else ""
    return head if TREE.fullmatch(head) else None


def pr_job(tools: Tools, env: Mapping[str, str], repo: str, head: str, name: str) -> dict | None:
    """The newest successful job `name` of this repo's ci.yml run on `head`, as GitHub ran it."""
    q = f"/repos/{repo}/actions/runs?head_sha={head}&event=pull_request&per_page=50"
    doc = api(tools, env, "GET", q)
    runs = [
        r
        for r in (doc.get("workflow_runs") or [] if isinstance(doc, dict) else [])
        if r.get("path") == WORKFLOW and (r.get("head_repository") or {}).get("full_name") == repo
    ]
    for run in sorted(runs, key=lambda r: int(r.get("id") or 0), reverse=True):
        jobs = api(tools, env, "GET", f"/repos/{repo}/actions/runs/{run.get('id')}/jobs")
        for job in jobs.get("jobs") or [] if isinstance(jobs, dict) else []:
            if job.get("name") == name and job.get("conclusion") == "success":
                return job
    return None
