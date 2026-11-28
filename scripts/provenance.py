#!/usr/bin/env python3
"""Write the SLSA v1 provenance predicate of a digest from the build record its
labels carry, for scripts/attest-digest.sh to attest (a997).

The build (scripts/self.sh, in ci.yml's image job) stamps the run that built
the image, the workflow that ran it and the commit into the image config, next
to the org.opencontainers.image.revision it already wrote, so the signature
covers them. The signer may take a digest an EARLIER run built, so the builder
is read off the image, never taken from the signing run. A digest built before
a997, or whose record is not this repo's ci.yml on main or a v* tag at its own
revision (inherited FROM a base image, or a branch's dispatched build), gets no
provenance rather than a wrong one. The same shape as constitution's
container_release.py provenance (#204), whose trusted builder is its reusable
workflow; sdlc hand-rolls its own, so the builder is this repo's ci.yml.
scripts/self.sh's sign step calls it (#266); it moved from container.yml
(retired, a1790) to ci.yml with the build.

scripts/hotfix.sh builds outside Actions, so its image records no run. As
promote's local_provenance trusts scripts/local-release.sh, this trusts a build
hotfix.sh names itself as (SDLC_LOCAL_BUILDER, pinned to the commit it released),
with an attempt unique to that invocation and no run id, and only from the sign
step of that same invocation: the image's run label must name this very attempt
(#275).

Reads `skopeo inspect` JSON on stdin; writes --out only when there is a record.

Usage: skopeo inspect docker://<repo>@<digest> | scripts/provenance.py --out <file> [--commit <sha>]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

BUILD_RUN = "com.crunchtools.build.run"
BUILD_BUILDER = "com.crunchtools.build.builder"
BUILD_WORKFLOW = "com.crunchtools.build.workflow"
BUILD_COMMIT = "com.crunchtools.build.commit"
REVISION = "org.opencontainers.image.revision"
WORKFLOW = ".github/workflows/ci.yml@"
COMMIT = re.compile(r"^[0-9a-f]{40}$")
# Only main and release tags build what promote publishes; a record from any
# other ref (a pull request's merge ref, a dispatched branch) is not one.
RELEASED_REF = re.compile(r"^(refs/heads/main|refs/tags/v[^/]+)$")
BUILD_TYPE = "https://slsa-framework.github.io/github-actions-buildtypes/workflow/v1"
# hotfix.sh is no workflow: its builds say so, with a build type of their own.
HOTFIX_BUILD_TYPE = "https://crunchtools.com/sdlc/buildtypes/hotfix/v1"
HOTFIX = "scripts/hotfix.sh"
LOCAL_RUN = "https://github.com/{repo}/actions/runs//attempts/{attempt}"
LOCAL_BUILDER = r"https://github\.com/{repo}/blob/([0-9a-f]{{40}})/scripts/hotfix\.sh"
ATTEMPT = re.compile(r"[0-9]+")


def recorded(labels: dict[str, str]) -> tuple[str, str, str, str]:
    """The (run, builder, workflow, commit) labels, "" where one is missing."""
    run_url, builder, workflow, commit = (
        labels.get(k, "") for k in (BUILD_RUN, BUILD_BUILDER, BUILD_WORKFLOW, BUILD_COMMIT)
    )
    return run_url, builder, workflow, commit


def build_record(labels: dict[str, str], repo: str) -> tuple[str, str, str, str] | None:
    """(run, builder, workflow, commit) when the labels record this repo's
    ci.yml building its own revision."""
    run_url, builder, workflow, commit = recorded(labels)
    own_run = re.compile(rf"^https://github\.com/{re.escape(repo)}/actions/runs/\d+/attempts/\d+$")
    trusted = f"{repo}/{WORKFLOW}"
    honest = (
        own_run.match(run_url)
        and builder.startswith(trusted)
        and RELEASED_REF.match(builder[len(trusted) :])
        and workflow == builder
        and COMMIT.match(commit)
        and commit == labels.get(REVISION)
    )
    return (run_url, builder, workflow, commit) if honest else None


def provenance(labels: dict[str, str], repo: str) -> dict | None:
    """The `slsaprovenance1` predicate naming the run that built the image; None if
    its labels record none of this repo's."""
    record = build_record(labels, repo)
    if record is None:
        return None
    run_url, builder, workflow, commit = record
    ref = workflow.removeprefix(f"{repo}/{WORKFLOW}")
    source = f"https://github.com/{repo}"
    workflow_params = {"workflow": {"ref": ref, "repository": source, "path": WORKFLOW[:-1]}}
    return predicate(BUILD_TYPE, workflow_params, commit, f"https://github.com/{builder}", run_url)


def local_provenance(
    labels: dict[str, str], repo: str, env: dict[str, str], commit: str
) -> dict | None:
    """The predicate for a build scripts/hotfix.sh ran, None for any other.

    hotfix.sh runs self.sh's image and sign in one invocation, with one
    GITHUB_RUN_ATTEMPT unique to it and no GITHUB_RUN_ID, which image wrote into
    the run label; it releases the tree it runs from, so its builder names
    `commit`, the tree sign runs from, and so does every built label.
    """
    builder, attempt, ref = (
        env.get(k, "") for k in ("SDLC_LOCAL_BUILDER", "GITHUB_RUN_ATTEMPT", "GITHUB_REF")
    )
    # fullmatch: `$` would also take a trailing newline.
    pinned = re.fullmatch(LOCAL_BUILDER.format(repo=re.escape(repo)), builder)
    run_label, builder_label, workflow_label, built = recorded(labels)
    # The image names no builder of its own: hotfix.sh's is in the environment.
    this_run = ATTEMPT.fullmatch(attempt) and not env.get("GITHUB_RUN_ID")
    honest = (
        pinned
        and this_run
        and run_label == LOCAL_RUN.format(repo=repo, attempt=attempt)
        and not (builder_label or workflow_label)
        and RELEASED_REF.fullmatch(ref)
        and COMMIT.fullmatch(commit)
        and pinned.group(1) == built == labels.get(REVISION) == commit
    )
    if not honest:
        return None
    source = f"https://github.com/{repo}"
    script = {"repository": source, "path": HOTFIX, "commit": commit, "ref": ref}
    return predicate(
        HOTFIX_BUILD_TYPE, {"script": script}, commit, builder, f"{builder}#attempt-{attempt}"
    )


def predicate(build_type: str, parameters: dict, commit: str, builder: str, run: str) -> dict:
    """SLSA v1 for either recorded build: `build_type` with its `parameters` (ci.yml's
    workflow, or hotfix.sh at its commit) built `commit` of the repository the
    parameters name, run as `run` by `builder`."""
    source = next(iter(parameters.values()))["repository"]
    return {
        "buildDefinition": {
            "buildType": build_type,
            "externalParameters": parameters,
            "resolvedDependencies": [{"uri": f"git+{source}", "digest": {"gitCommit": commit}}],
        },
        "runDetails": {"builder": {"id": builder}, "metadata": {"invocationId": run}},
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    p.add_argument("--out", required=True, help="the predicate, written only if there is one")
    p.add_argument("--commit", default="", help="the commit the signing tree is at (hotfix.sh)")
    a = p.parse_args(argv)
    if not a.repo:
        print("no --repo and no GITHUB_REPOSITORY", file=sys.stderr)
        return 1
    labels = json.load(sys.stdin).get("Labels") or {}
    found = provenance(labels, a.repo) or local_provenance(
        labels, a.repo, dict(os.environ), a.commit
    )
    if found is None:
        print(
            f"::warning::the image records no ci.yml run of {a.repo} on main or a v* tag "
            "at its own revision, nor this hotfix.sh run's own build (built before a997, "
            "a branch build, or a base image's record); no provenance attested",
            file=sys.stderr,
        )
        return 0
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(found, fh, indent=2)
    print(f"provenance: {found['runDetails']['metadata']['invocationId']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
