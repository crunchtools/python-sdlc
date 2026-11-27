#!/usr/bin/env python3
"""`scripts/provenance.py` must attest only a build record it can trust (a997).

attest-digest.sh signs whatever predicate this writes, so a wrong record is worse
than none. The record is the labels ci.yml's build stamps into the image: the run,
the builder, the workflow and the commit. This feeds `skopeo inspect`-shaped JSON
to the script and asserts that a record of this repo's ci.yml at its own
revision yields the exact SLSA v1 predicate, and that every other shape (no
labels, another repo's run, another workflow, a ref other than main or a v*
tag, a commit that is not the revision) yields exit 0, no file and a ::warning::.

Run: tests/test_provenance.py   (needs python3 only)
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "provenance.py"

REPO = "crunchtools/sdlc"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
BUILDER = f"{REPO}/.github/workflows/ci.yml@refs/heads/main"
RUN = f"https://github.com/{REPO}/actions/runs/123/attempts/2"

VALID = {
    "com.crunchtools.build.run": RUN,
    "com.crunchtools.build.builder": BUILDER,
    "com.crunchtools.build.workflow": BUILDER,
    "com.crunchtools.build.commit": COMMIT,
    "org.opencontainers.image.revision": COMMIT,
}

EXPECTED = {
    "buildDefinition": {
        "buildType": "https://slsa-framework.github.io/github-actions-buildtypes/workflow/v1",
        "externalParameters": {
            "workflow": {
                "ref": "refs/heads/main",
                "repository": f"https://github.com/{REPO}",
                "path": ".github/workflows/ci.yml",
            }
        },
        "resolvedDependencies": [
            {"uri": f"git+https://github.com/{REPO}", "digest": {"gitCommit": COMMIT}}
        ],
    },
    "runDetails": {
        "builder": {"id": f"https://github.com/{BUILDER}"},
        "metadata": {"invocationId": RUN},
    },
}


def with_labels(**changes: str | None) -> dict[str, str]:
    """VALID with short-keyed changes; None drops the label."""
    names = {
        "run": "com.crunchtools.build.run",
        "builder": "com.crunchtools.build.builder",
        "workflow": "com.crunchtools.build.workflow",
        "commit": "com.crunchtools.build.commit",
        "revision": "org.opencontainers.image.revision",
    }
    labels = dict(VALID)
    for key, value in changes.items():
        if value is None:
            del labels[names[key]]
        else:
            labels[names[key]] = value
    return labels


def run(stdin: str, repo: str | None = REPO, env_repo: str | None = None):
    """(exit code, stderr, predicate or None) of the script run as the workflow runs it."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "predicate.json"
        cmd = [sys.executable, "-I", str(SCRIPT), "--out", str(out)]
        if repo is not None:
            cmd += ["--repo", repo]
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_REPOSITORY"}
        if env_repo is not None:
            env["GITHUB_REPOSITORY"] = env_repo
        proc = subprocess.run(
            cmd, input=stdin, env=env, capture_output=True, text=True, check=False
        )
        written = json.loads(out.read_text()) if out.exists() else None
        return proc.returncode, proc.stderr, written


def inspect(labels: dict[str, str] | None) -> str:
    return json.dumps({"Labels": labels})


def load_module():
    spec = importlib.util.spec_from_file_location("provenance", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REFUSALS = [
    ("no labels at all", json.dumps({}), True),
    ("Labels is null", inspect(None), True),
    ("empty labels", inspect({}), True),
    (
        "run of another repo",
        inspect(with_labels(run="https://github.com/crunchtools/other/actions/runs/1/attempts/1")),
        True,
    ),
    ("run with extra suffix", inspect(with_labels(run=RUN + "/jobs/9")), True),
    ("run without attempt", inspect(with_labels(run=RUN.rsplit("/attempts", 1)[0])), True),
    (
        "builder is release.yml",
        inspect(
            with_labels(
                builder=f"{REPO}/.github/workflows/release.yml@refs/heads/main",
                workflow=f"{REPO}/.github/workflows/release.yml@refs/heads/main",
            )
        ),
        True,
    ),
    (
        "builder is constitution's reusable workflow",
        inspect(
            with_labels(
                builder="crunchtools/constitution/.github/workflows/container.yml@refs/heads/main",
                workflow="crunchtools/constitution/.github/workflows/container.yml@refs/heads/main",
            )
        ),
        True,
    ),
    (
        "builder is the prefix with nothing after @",
        inspect(
            with_labels(
                builder=f"{REPO}/.github/workflows/ci.yml@",
                workflow=f"{REPO}/.github/workflows/ci.yml@",
            )
        ),
        True,
    ),
    (
        "workflow is not the builder",
        inspect(with_labels(workflow=f"{REPO}/.github/workflows/ci.yml@refs/tags/v1")),
        True,
    ),
    *[
        (
            f"builder ref is {ref}",
            inspect(
                with_labels(
                    builder=f"{REPO}/.github/workflows/ci.yml@{ref}",
                    workflow=f"{REPO}/.github/workflows/ci.yml@{ref}",
                )
            ),
            True,
        )
        for ref in (
            "refs/pull/91/merge",
            "refs/heads/feat/a997",
            "refs/heads/mainline",
            "refs/tags/1.0.0",
            "refs/tags/v1/x",
        )
    ],
    ("commit is not 40 hex", inspect(with_labels(commit="abc123", revision="abc123")), True),
    (
        "commit is uppercase hex",
        inspect(with_labels(commit=COMMIT.upper(), revision=COMMIT.upper())),
        True,
    ),
    ("commit is not the revision", inspect(with_labels(revision="f" * 40)), True),
    ("revision missing", inspect(with_labels(revision=None)), True),
]


def check(name: str, ok: bool, detail: str = "") -> int:
    print(f"{'ok' if ok else 'FAIL'}: {name}{'' if ok else ' (' + detail + ')'}")
    return 0 if ok else 1


def main() -> int:
    failed = 0

    code, err, written = run(inspect(VALID))
    failed += check("valid record writes the exact predicate", written == EXPECTED, repr(written))
    failed += check("valid record exits 0", code == 0, f"exit {code}: {err}")
    failed += check("valid record has no warning", "::warning::" not in err, err)

    tag = f"{REPO}/.github/workflows/ci.yml@refs/tags/v0.12.0"
    code, _, written = run(inspect(with_labels(builder=tag, workflow=tag)))
    failed += check(
        "a v* tag's record is trusted",
        code == 0
        and written is not None
        and written["buildDefinition"]["externalParameters"]["workflow"]["ref"]
        == "refs/tags/v0.12.0",
        repr(written),
    )

    code, _, written = run(inspect(VALID), repo=None, env_repo=REPO)
    failed += check("GITHUB_REPOSITORY stands in for --repo", code == 0 and written == EXPECTED)

    for name, stdin, _ in REFUSALS:
        code, err, written = run(stdin)
        ok = code == 0 and written is None and "::warning::" in err
        failed += check(f"refused: {name}", ok, f"exit {code}, file {written is not None}, {err!r}")

    code, _, written = run(inspect(VALID), repo=None)
    failed += check("no --repo and no GITHUB_REPOSITORY exits 1", code == 1 and written is None)

    mod = load_module()
    failed += check("provenance() matches the script", mod.provenance(VALID, REPO) == EXPECTED)
    failed += check(
        "build_record() returns the four fields",
        mod.build_record(VALID, REPO) == (RUN, BUILDER, BUILDER, COMMIT),
    )
    failed += check("another repo's labels give no record", mod.build_record(VALID, "a/b") is None)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
