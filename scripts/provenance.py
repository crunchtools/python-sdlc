#!/usr/bin/env python3
"""Write the SLSA v1 provenance predicate of a digest from the build record its
labels carry, for scripts/attest-digest.sh to attest (a997).

container.yml's build step stamps the run that built the image, the workflow
that ran it and the commit into the image config, next to the
org.opencontainers.image.revision it already wrote, so the signature covers
them. Promote usually takes a digest an EARLIER run built (a tag run builds
nothing), so the builder is read off the image, never taken from the promote
run. A digest built before a997, or whose record is not this repo's
container.yml on main or a v* tag at its own revision (inherited FROM a base
image, or a branch's dispatched build), gets no provenance rather than a wrong
one. The same shape as constitution's
container_release.py provenance (#204), whose trusted builder is its reusable
workflow; sdlc hand-rolls its own, so the builder is this repo's container.yml.

Reads `skopeo inspect` JSON on stdin; writes --out only when there is a record.

Usage: skopeo inspect docker://<repo>@<digest> | scripts/provenance.py --out <file>
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
WORKFLOW = ".github/workflows/container.yml@"
COMMIT = re.compile(r"^[0-9a-f]{40}$")
# Only main and release tags build what promote publishes; a record from any
# other ref (a pull request's merge ref, a dispatched branch) is not one.
RELEASED_REF = re.compile(r"^(refs/heads/main|refs/tags/v[^/]+)$")
BUILD_TYPE = "https://slsa-framework.github.io/github-actions-buildtypes/workflow/v1"


def build_record(labels: dict[str, str], repo: str) -> tuple[str, str, str, str] | None:
    """(run, builder, workflow, commit) when the labels record this repo's
    container.yml building its own revision."""
    run_url, builder, workflow, commit = (
        labels.get(k, "") for k in (BUILD_RUN, BUILD_BUILDER, BUILD_WORKFLOW, BUILD_COMMIT)
    )
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
    path, ref = workflow.removeprefix(f"{repo}/").split("@", 1)
    source = f"https://github.com/{repo}"
    return {
        "buildDefinition": {
            "buildType": BUILD_TYPE,
            "externalParameters": {"workflow": {"ref": ref, "repository": source, "path": path}},
            "resolvedDependencies": [{"uri": f"git+{source}", "digest": {"gitCommit": commit}}],
        },
        "runDetails": {
            "builder": {"id": f"https://github.com/{builder}"},
            "metadata": {"invocationId": run_url},
        },
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    p.add_argument("--out", required=True, help="the predicate, written only if there is one")
    a = p.parse_args(argv)
    if not a.repo:
        print("no --repo and no GITHUB_REPOSITORY", file=sys.stderr)
        return 1
    labels = json.load(sys.stdin).get("Labels") or {}
    predicate = provenance(labels, a.repo)
    if predicate is None:
        print(
            f"::warning::the image records no container.yml run of {a.repo} on main or a v* tag "
            "at its own revision (built before a997, a branch build, or a base image's record); "
            "no provenance attested",
            file=sys.stderr,
        )
        return 0
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(predicate, fh, indent=2)
    print(f"provenance: {predicate['runDetails']['metadata']['invocationId']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
