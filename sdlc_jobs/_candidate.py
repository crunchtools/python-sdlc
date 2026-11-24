"""Build once: a pull request's `image` pushes a candidate, the merge promotes it.

A candidate is the release image a same-repo pull request built and gated, pushed to
`quay.io/crunchtools/candidates:<repo>-<tree>`. The key is the git tree, not a commit:
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

Adopting re-checks what the candidate says about itself: the tree and source labels
must be this commit's tree and this repo, and the copy must land at the same digest.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping

from sdlc_jobs._core import DIGEST, Tools, api, cannot, step

CANDIDATES = "quay.io/crunchtools/candidates"
ROBOT = "crunchtools+candidates"
AUDIENCE = "quay"
EXCHANGE = "https://quay.io/oauth2/federation/robot/token"
TREE_LABEL = "com.crunchtools.build.tree"
SOURCE_LABEL = "org.opencontainers.image.source"
TREE = re.compile(r"[0-9a-f]{40}")
MISSING = re.compile(r"manifest unknown|not found|no such|name unknown|deleted or has expired", re.I)


def tag(repo: str, tree: str) -> str:
    """`candidates:<repo name>-<tree>`, the one place the key is spelled."""
    if not TREE.fullmatch(tree):
        cannot(f"{tree!r} is not a git tree id")
    return f"{CANDIDATES}:{repo.rpartition('/')[2]}-{tree}"


def tree_of(tools: Tools, env: Mapping[str, str], repo: str, sha: str) -> str:
    """The tree of a commit of this repo, read from the API."""
    doc = api(tools, env, "GET", f"/repos/{repo}/git/commits/{sha}")
    tree = (doc.get("tree") or {}).get("sha") if isinstance(doc, dict) else None
    if not (isinstance(tree, str) and TREE.fullmatch(tree)):
        cannot(f"commit {sha} of {repo} has no readable tree")
    return tree


def oidc_token(tools: Tools, env: Mapping[str, str]) -> str | None:
    """This job's GitHub OIDC token for Quay; None when the job may not have one (a fork)."""
    url, bearer = env.get("ACTIONS_ID_TOKEN_REQUEST_URL"), env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not bearer:
        return None
    resp = tools.http("GET", f"{url}&audience={AUDIENCE}", {"Authorization": f"Bearer {bearer}"})
    doc = resp.json() if resp.status == 200 else None
    value = doc.get("value") if isinstance(doc, dict) else None
    if not isinstance(value, str) or not value:
        cannot(f"GitHub issued no OIDC token (HTTP {resp.status})")
    return value


def robot_token(tools: Tools, jwt: str) -> str:
    """Quay's robot token for this job's OIDC token: valid an hour, candidates only."""
    basic = base64.b64encode(f"{ROBOT}:{jwt}".encode()).decode()
    resp = tools.http("GET", EXCHANGE, {"Authorization": f"Basic {basic}"})
    doc = resp.json() if resp.status == 200 else None
    token = doc.get("token") if isinstance(doc, dict) else None
    if not isinstance(token, str) or not token:
        cannot(f"Quay refused the OIDC exchange for {ROBOT} (HTTP {resp.status})")
    return token


def annotate(repo: str, tree: str, digest: str) -> None:
    """The candidate on the run's summary, for a person: nothing reads it back."""
    doc = json.dumps({"candidate": tag(repo, tree), "digest": digest}, separators=(",", ":"))
    print(f"::notice title=sdlc candidate::{doc}", flush=True)


def find(
    tools: Tools, env: Mapping[str, str], repo: str, tree: str
) -> tuple[str, dict[str, str]] | None:
    """(digest, labels) of the candidate for `tree`, or None when there is none to adopt.

    A candidate whose labels are not this tree and this repo is reported and ignored:
    the caller builds instead.
    """
    ref = tag(repo, tree)
    step(f"candidate {ref}")
    proc = tools.run(["skopeo", "inspect", f"docker://{ref}"], env=dict(env))
    if proc.returncode != 0:
        if MISSING.search(proc.stderr or ""):
            print("no candidate for this tree: building", flush=True)
            return None
        cannot(f"cannot read {ref}: {(proc.stderr or '').strip()}")
    try:
        doc = json.loads(proc.stdout)
    except ValueError as exc:
        cannot(f"skopeo inspect of {ref} is not JSON: {exc}")
    digest, labels = doc.get("Digest", ""), doc.get("Labels") or {}
    source = f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}"
    why = (
        "its digest is malformed" if not DIGEST.fullmatch(str(digest))
        else f"it is labelled tree {labels.get(TREE_LABEL)!r}" if labels.get(TREE_LABEL) != tree
        else f"it is labelled source {labels.get(SOURCE_LABEL)!r}" if labels.get(SOURCE_LABEL) != source
        else ""
    )
    if why:
        print(f"candidate {ref} is not adopted: {why}; building", flush=True)
        return None
    return str(digest), labels
