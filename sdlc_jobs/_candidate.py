"""Build once: a pull request's `image` pushes a candidate, the merge promotes it.

A candidate is the release image a same-repo pull request built and gated, pushed to
`quay.io/crunchtools/candidates:<image>-<tree>`, where <image> is the name the image
publishes as: the repo's own, or a `[[tool.sdlc.images]]` name, which EXTRA_IMAGES
(sdlc_jobs/_core.py) gives one repo only. So the key is as unambiguous as the
registry's own names. The key is the git tree, not a commit:
a squash merge makes a new commit, but when the branch was up to date the merged tree
is the one the pull request built (GitHub builds `refs/pull/N/merge`, whose tree is the
merge result). On the default branch, `image` adopts the candidate for its tree by
digest instead of building, and `promote` signs that digest. No candidate, or one that
does not check out, and `image` builds as it always did: the candidate is a shortcut,
never a requirement.

The pull request holds no secret. It logs in with the job's GitHub OIDC token,
exchanged at Quay for an hour's token of the `crunchtools+candidates` robot, which can
write the candidates repository and nothing else (a federation per repo, subject
`repo:crunchtools/<repo>:pull_request`). A fork's pull request gets no OIDC token, so
it builds no candidate.

The binding comes from GitHub, not the registry: a merge adopts only the digest the
merged pull request's head recorded, as the one `sdlc candidate` annotation on its
`sdlc / image (pull_request)` job, read through runs -> jobs -> that job's check run.
A check run made through the API is never a job, and the PR job stops workflow commands
while repo code runs, so only sdlc's own code can write that annotation. The candidate's
labels are then checked as well, and the copy must land at the same digest.

Load-bearing: the org ruleset "Workflows: admins only, every branch" (a push ruleset on
`.github/workflows/**`). A pull_request run executes the branch's own ci.yml; without
that ruleset a branch could rewrite the job and hold the candidates robot itself.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
from collections.abc import Mapping
from typing import Any

from sdlc_jobs._core import DIGEST, Response, Tools, api, cannot, step

CANDIDATES = "quay.io/crunchtools/candidates"
ROBOT = "crunchtools+candidates"
AUDIENCE = "quay"
EXCHANGE = "https://quay.io/oauth2/federation/robot/token"
TREE_LABEL = "com.crunchtools.build.tree"
SOURCE_LABEL = "org.opencontainers.image.source"
OK = 200
WORKFLOW = ".github/workflows/ci.yml"
IMAGE_JOB = "sdlc / image (pull_request)"
CHECK_JOB = "sdlc / check (pull_request)"
TITLE = "sdlc candidate"
TREE = re.compile(r"[0-9a-f]{40}")
MISSING = re.compile(
    r"manifest unknown|not found|no such|name unknown|deleted or has expired", re.I
)


def tag(image: str, tree: str) -> str:
    """`candidates:<image name>-<tree>`, the one place the key is spelled.

    `image` is the name it publishes as; an `owner/repo` is read as its repo's own image.
    """
    if not TREE.fullmatch(tree):
        cannot(f"{tree!r} is not a git tree id")
    return f"{CANDIDATES}:{image.rpartition('/')[2]}-{tree}"


def tree_of(tools: Tools, env: Mapping[str, str], repo: str, sha: str) -> str:
    """The tree of a commit of this repo, read from the API."""
    doc = api(tools, env, "GET", f"/repos/{repo}/git/commits/{sha}")
    tree = (doc.get("tree") or {}).get("sha") if isinstance(doc, dict) else None
    if not (isinstance(tree, str) and TREE.fullmatch(tree)):
        cannot(f"commit {sha} of {repo} has no readable tree")
    return tree


def _field(resp: Response, name: str, refusal: str) -> str:
    """The non-empty string `name` of a 200 JSON response; anything else cannot run."""
    doc = resp.json() if resp.status == OK else None
    value = doc.get(name) if isinstance(doc, dict) else None
    if not isinstance(value, str) or not value:
        cannot(f"{refusal} (HTTP {resp.status})")
    return value


def oidc_token(tools: Tools, env: Mapping[str, str]) -> str | None:
    """This job's GitHub OIDC token for Quay; None when the job may not have one (a fork)."""
    url, bearer = env.get("ACTIONS_ID_TOKEN_REQUEST_URL"), env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not bearer:
        return None
    resp = tools.http("GET", f"{url}&audience={AUDIENCE}", {"Authorization": f"Bearer {bearer}"})
    return _field(resp, "value", "GitHub issued no OIDC token")


def robot_token(tools: Tools, jwt: str) -> str:
    """Quay's robot token for this job's OIDC token: valid an hour, candidates only."""
    basic = base64.b64encode(f"{ROBOT}:{jwt}".encode()).decode()
    resp = tools.http("GET", EXCHANGE, {"Authorization": f"Basic {basic}"})
    return _field(resp, "token", f"Quay refused the OIDC exchange for {ROBOT}")


def stop_commands() -> str:
    """Stop workflow commands until `resume`: repo code's output cannot annotate."""
    token = secrets.token_hex(16)
    print(f"::stop-commands::{token}", flush=True)
    return token


def resume(token: str) -> None:
    print(f"::{token}::", flush=True)


def annotate(repo: str, tree: str, digest: str, image: str = "") -> None:
    """The binding a merge reads back: this job's one `sdlc candidate` annotation per image."""
    key = tag(image or repo, tree)
    doc = json.dumps({"candidate": key, "digest": digest}, separators=(",", ":"))
    print(f"::notice title={TITLE}::{doc}", flush=True)


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


def _parsed(note: Mapping[str, Any]) -> dict[str, Any]:
    """A candidate annotation's JSON object; {} (and a line saying why) for anything else."""
    try:
        doc = json.loads(str(note.get("message", "")))
    except ValueError as exc:
        print(f"a candidate annotation is not JSON ({exc}): ignoring it", flush=True)
        return {}
    if not isinstance(doc, dict):
        print("a candidate annotation is not a JSON object: ignoring it", flush=True)
        return {}
    return doc


def recorded(
    tools: Tools, env: Mapping[str, str], repo: str, tree: str, image: str = ""
) -> str | None:
    """The digest the merged pull request's image job recorded for `tree` and `image`
    (default: the repo's own); None, none."""
    head = merged_head(tools, env, repo, env.get("GITHUB_SHA", ""))
    job = pr_job(tools, env, repo, head, IMAGE_JOB) if head else None
    url = str((job or {}).get("check_run_url", ""))
    base = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    if not url.startswith(f"{base}/repos/{repo}/check-runs/"):
        print("no image job on the merged pull request: building", flush=True)
        return None
    notes = api(tools, env, "GET", url.removeprefix(base) + "/annotations")
    mine = [n for n in notes if n.get("title") == TITLE] if isinstance(notes, list) else []
    key = tag(image or repo, tree)
    docs = [_parsed(note) for note in mine]
    if not mine:
        print("0 candidate annotations on the pull request's image job: building")
        return None
    ours = [d for d in docs if d.get("candidate") == key]
    if len(ours) > 1:
        print(f"{len(ours)} candidate annotations for {key}: building", flush=True)
        return None
    if not ours:
        print(f"the pull request's candidates are another tree or image than {key}: building")
        return None
    doc = ours[0]
    digest = doc.get("digest")
    if not (isinstance(digest, str) and DIGEST.fullmatch(digest)):
        cannot("the pull request's candidate annotation has no well-formed digest")
    return digest


def find(
    tools: Tools,
    env: Mapping[str, str],
    child_env: Mapping[str, str],
    tree: str,
    image: str = "",
) -> tuple[str, dict[str, str]] | None:
    """(digest, labels) of the candidate the merged pull request recorded for `tree`
    and `image` (default: the repo's own) of GITHUB_REPOSITORY.

    None when there is none to adopt, and the caller builds instead. `env` holds the
    token for the API; `child_env`, with no token, runs skopeo.
    """
    repo = env.get("GITHUB_REPOSITORY", "")
    step(f"candidate for {image or repo} at tree {tree}")
    digest = recorded(tools, env, repo, tree, image)
    if digest is None:
        return None
    ref = f"{CANDIDATES}@{digest}"
    proc = tools.run(["skopeo", "inspect", f"docker://{ref}"], env=dict(child_env))
    if proc.returncode != 0:
        if MISSING.search(proc.stderr or ""):
            print(f"{ref} is gone (pruned?): building", flush=True)
            return None
        cannot(f"cannot read {ref}: {(proc.stderr or '').strip()}")
    try:
        doc = json.loads(proc.stdout)
    except ValueError as exc:
        cannot(f"skopeo inspect of {ref} is not JSON: {exc}")
    labels = doc.get("Labels") or {}
    source = f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}"
    why = (
        f"its digest is {doc.get('Digest')!r}"
        if doc.get("Digest") != digest
        else f"it is labelled tree {labels.get(TREE_LABEL)!r}"
        if labels.get(TREE_LABEL) != tree
        else f"it is labelled source {labels.get(SOURCE_LABEL)!r}"
        if labels.get(SOURCE_LABEL) != source
        else ""
    )
    if why:
        cannot(f"the recorded candidate {ref} does not check out: {why}")
    return digest, labels
