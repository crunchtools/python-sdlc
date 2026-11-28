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
OTHER = "f" * 40

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


HOTFIX_ENV_NAMES = ("SDLC_LOCAL_BUILDER", "GITHUB_RUN_ATTEMPT", "GITHUB_REF", "GITHUB_RUN_ID")


def run(
    stdin: str,
    repo: str | None = REPO,
    env_repo: str | None = None,
    extra_env: dict[str, str] | None = None,
    commit: str | None = None,
):
    """(exit code, stderr, predicate or None) of the script run as the workflow runs it."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "predicate.json"
        cmd = [sys.executable, "-I", str(SCRIPT), "--out", str(out)]
        if repo is not None:
            cmd += ["--repo", repo]
        if commit is not None:
            cmd += ["--commit", commit]
        skip = ("GITHUB_REPOSITORY", *HOTFIX_ENV_NAMES)
        env = {k: v for k, v in os.environ.items() if k not in skip}
        env.update(extra_env or {})
        if env_repo is not None:
            env["GITHUB_REPOSITORY"] = env_repo
        proc = subprocess.run(
            cmd, input=stdin, env=env, capture_output=True, text=True, check=False
        )
        written = json.loads(out.read_text()) if out.exists() else None
        return proc.returncode, proc.stderr, written


HF_BUILDER = f"https://github.com/{REPO}/blob/{COMMIT}/scripts/hotfix.sh"
HF_RUN = f"https://github.com/{REPO}/actions/runs//attempts/42"
HF_ENV = {
    "SDLC_LOCAL_BUILDER": HF_BUILDER,
    "GITHUB_RUN_ATTEMPT": "42",
    "GITHUB_REF": "refs/tags/v1.2.3",
}
HF_LABELS = with_labels(run=HF_RUN, builder="", workflow="")

HF_EXPECTED = {
    "buildDefinition": {
        "buildType": EXPECTED["buildDefinition"]["buildType"],
        "externalParameters": {
            "workflow": {
                "ref": "refs/tags/v1.2.3",
                "repository": f"https://github.com/{REPO}",
                "path": ".github/workflows/ci.yml",
            }
        },
        "resolvedDependencies": EXPECTED["buildDefinition"]["resolvedDependencies"],
    },
    "runDetails": {
        "builder": {"id": HF_BUILDER},
        "metadata": {"invocationId": f"{HF_BUILDER}#attempt-42"},
    },
}


def hf_env(**changes: str | None) -> dict[str, str]:
    """HF_ENV with changes; None drops the variable."""
    env = {**HF_ENV, **changes}
    return {k: v for k, v in env.items() if v is not None}


def hf_labels(**changes: str | None) -> dict[str, str]:
    """HF_LABELS with short-keyed changes (see with_labels)."""
    return with_labels(**{"run": HF_RUN, "builder": "", "workflow": "", **changes})


# (name, labels, env, --commit): a hotfix record that must be refused.
HF_REFUSALS = [
    ("hotfix: builder empty", HF_LABELS, hf_env(SDLC_LOCAL_BUILDER=""), COMMIT),
    (
        "hotfix: builder pinned to another commit than --commit",
        HF_LABELS,
        hf_env(SDLC_LOCAL_BUILDER=HF_BUILDER.replace(COMMIT, "f" * 40)),
        COMMIT,
    ),
    (
        "hotfix: builder in another repo",
        HF_LABELS,
        hf_env(SDLC_LOCAL_BUILDER=HF_BUILDER.replace(REPO, "crunchtools/other")),
        COMMIT,
    ),
    (
        "hotfix: builder names scripts/local-release.sh",
        HF_LABELS,
        hf_env(SDLC_LOCAL_BUILDER=HF_BUILDER.replace("hotfix.sh", "local-release.sh")),
        COMMIT,
    ),
    ("hotfix: GITHUB_RUN_ID set", HF_LABELS, hf_env(GITHUB_RUN_ID="9"), COMMIT),
    ("hotfix: attempt 43 against label 42", HF_LABELS, hf_env(GITHUB_RUN_ATTEMPT="43"), COMMIT),
    ("hotfix: attempt empty", HF_LABELS, hf_env(GITHUB_RUN_ATTEMPT=""), COMMIT),
    ("hotfix: attempt missing", HF_LABELS, hf_env(GITHUB_RUN_ATTEMPT=None), COMMIT),
    ("hotfix: ref is a pull request", HF_LABELS, hf_env(GITHUB_REF="refs/pull/1/merge"), COMMIT),
    ("hotfix: --commit missing", HF_LABELS, hf_env(), None),
    ("hotfix: label commit is another sha", hf_labels(commit="f" * 40), hf_env(), COMMIT),
    ("hotfix: label revision is another sha", hf_labels(revision="f" * 40), hf_env(), COMMIT),
    ("hotfix: label builder non-empty", hf_labels(builder=BUILDER), hf_env(), COMMIT),
    ("hotfix: label workflow non-empty", hf_labels(workflow=BUILDER), hf_env(), COMMIT),
    (
        "hotfix: revision label differs from the commit label",
        hf_labels(revision=OTHER),
        hf_env(),
        COMMIT,
    ),
    (
        "hotfix: builder, commit and revision agree on another sha than --commit",
        hf_labels(commit=OTHER, revision=OTHER),
        hf_env(SDLC_LOCAL_BUILDER=HF_BUILDER.replace(COMMIT, OTHER)),
        COMMIT,
    ),
    *[
        (
            f"hotfix: builder has {name} after hotfix.sh",
            HF_LABELS,
            hf_env(SDLC_LOCAL_BUILDER=HF_BUILDER + suffix),
            COMMIT,
        )
        for name, suffix in (("a .bak suffix", ".bak"), ("a /x path", "/x"), ("a newline", "\n"))
    ],
    (
        "hotfix: attempt 4a, with a run label that matches it",
        hf_labels(run=HF_RUN.replace("42", "4a")),
        hf_env(GITHUB_RUN_ATTEMPT="4a"),
        COMMIT,
    ),
    (
        "hotfix: fullwidth-digit attempt, with a run label that matches it",
        hf_labels(run=HF_RUN.replace("42", "\uff14\uff12")),
        hf_env(GITHUB_RUN_ATTEMPT="\uff14\uff12"),
        COMMIT,
    ),
    (
        "hotfix: ref has a trailing newline",
        HF_LABELS,
        hf_env(GITHUB_REF="refs/heads/main\n"),
        COMMIT,
    ),
    (
        "hotfix: run label has a run id",
        hf_labels(run=RUN.rsplit("/", 1)[0] + "/42"),
        hf_env(),
        COMMIT,
    ),
]


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

    code, err, written = run(inspect(HF_LABELS), extra_env=HF_ENV, commit=COMMIT)
    failed += check(
        "hotfix record writes the exact predicate", written == HF_EXPECTED, repr(written)
    )
    failed += check(
        "hotfix record exits 0 with no warning", code == 0 and "::warning::" not in err, err
    )

    for name, labels, env, commit in HF_REFUSALS:
        code, err, written = run(inspect(labels), extra_env=env, commit=commit)
        ok = code == 0 and written is None and "::warning::" in err
        failed += check(f"refused: {name}", ok, f"exit {code}, file {written is not None}, {err!r}")

    code, _, written = run(inspect(VALID), extra_env=HF_ENV, commit=COMMIT)
    failed += check(
        "CI record is unchanged with the hotfix env set", code == 0 and written == EXPECTED
    )

    code, _, written = run(inspect(VALID), repo=None)
    failed += check("no --repo and no GITHUB_REPOSITORY exits 1", code == 1 and written is None)

    mod = load_module()
    failed += check("provenance() matches the script", mod.provenance(VALID, REPO) == EXPECTED)
    failed += check(
        "build_record() returns the four fields",
        mod.build_record(VALID, REPO) == (RUN, BUILDER, BUILDER, COMMIT),
    )
    failed += check(
        "local_provenance() matches the script",
        mod.local_provenance(HF_LABELS, REPO, HF_ENV, COMMIT) == HF_EXPECTED,
    )
    failed += check(
        "local_provenance() refuses every hotfix refusal",
        all(mod.local_provenance(lb, REPO, env, c or "") is None for _, lb, env, c in HF_REFUSALS),
    )
    failed += check(
        "local_provenance() refuses a CI record",
        mod.local_provenance(VALID, REPO, HF_ENV, COMMIT) is None,
    )
    failed += check("another repo's labels give no record", mod.build_record(VALID, "a/b") is None)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
