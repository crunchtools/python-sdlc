#!/usr/bin/env python3
"""`sdlc_jobs/promote.py` must sign and move tags only on what it can re-validate.

The job holds the cosign key and moves the tags people pull, on a digest another job
built. So the suite is about refusals (a digest, ref or sha that is not this run's, a
release `write` did not confirm, a version from anywhere but `write`) and about tags
that never move backwards, and re-running the same inputs does nothing. A fake `Tools`
keeps a small registry; nothing here runs skopeo, cosign, syft or the GitHub API.

Run: tests/test_job_promote.py   (needs python3 only)
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdlc_jobs import promote
from sdlc_jobs._core import CannotRunError, Response, Tools
from sdlc_jobs.image import FLEET_PUBLIC_KEY

REPO = "crunchtools/demo"
REF = "quay.io/crunchtools/demo"
SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "f" * 40
NEW = "sha256:" + "a" * 64
OLD = "sha256:" + "b" * 64
RUN = f"https://github.com/{REPO}/actions/runs/7/attempts/1"
WORKFLOW = f"{REPO}/.github/workflows/ci.yml@refs/heads/main"


def cp(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, out, err)


def built_labels(version: str = "1.2.3") -> dict[str, str]:
    return {
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.revision": SHA,
        "com.crunchtools.build.run": RUN,
        "com.crunchtools.build.builder": WORKFLOW,
        "com.crunchtools.build.workflow": WORKFLOW,
        "com.crunchtools.build.commit": SHA,
    }


class FakeTools(Tools):
    """A registry with tags, labels, signatures and attestations, and a log of argvs."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.tags: dict[str, str] = {}
        self.labels: dict[str, dict[str, str]] = {NEW: built_labels()}
        self.signed: list[tuple[str, set[str]]] = []
        self.attested: set[tuple[str, str]] = set()
        self.release_tags: dict[str, str] = {}  # "1.2.3" -> commit
        self.annotated: dict[str, str] = {}  # "1.2.3" -> the commit its tag object points at
        self.key = FLEET_PUBLIC_KEY
        self.gated: str | None = NEW  # what gated-<sha> resolves to; None: no such tag
        self.api_status: int | None = None  # every GitHub answer, when set
        self.headers: list[Mapping[str, str]] = []
        self.image_missing = False  # the registry has no manifest at the digest
        self.stdin: list[str | None] = []
        self.cwds: list[str | None] = []

    def run(
        self,
        argv: list[str],
        *,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.argvs.append(argv)
        self.stdin.append(input)
        self.cwds.append(cwd)
        self.envs.append(dict(env or {}))
        if argv[0] == "skopeo":
            return self.skopeo(argv)
        if argv[0] == "syft":
            Path(argv[-1].split("=", 1)[1]).write_text("{}")
            return cp()
        assert argv[0] == "cosign", f"unexpected argv {argv}"
        return self.cosign(argv)

    def skopeo(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        target = argv[-1].removeprefix("docker://")
        if argv[1] == "inspect" and "--format" in argv:
            tag = target.rpartition(":")[2]
            if tag == f"gated-{SHA}":
                return cp(0, self.gated) if self.gated else cp(1, "", "manifest unknown")
            return cp(0, self.tags[tag]) if tag in self.tags else cp(1, "", "manifest unknown")
        if argv[1] == "inspect" and self.image_missing:
            return cp(1, "", "manifest unknown")
        if argv[1] == "inspect":
            digest = target.partition("@")[2] or self.tags[target.rpartition(":")[2]]
            return cp(0, json.dumps({"Labels": self.labels.get(digest, {})}))
        if argv[1] == "copy":
            source, dest = argv[-2].removeprefix("docker://"), argv[-1].removeprefix("docker://")
            self.tags[dest.rpartition(":")[2]] = source.rpartition("@")[2]
        return cp()

    def cosign(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        digest = argv[-1].partition("@")[2]
        notes = {argv[i + 1] for i, a in enumerate(argv) if a == "-a"}
        kind = argv[argv.index("--type") + 1] if "--type" in argv else ""
        rc, out = 0, ""
        match argv[1]:
            case "public-key":
                out = self.key
            case "sign":
                self.signed.append((digest, notes))
            case "attest":
                self.attested.add((digest, kind))
            case "verify":
                rc = 0 if any(d == digest and notes <= n for d, n in self.signed) else 1
            case "verify-attestation":
                rc = 0 if (digest, kind) in self.attested else 1
        return cp(rc, out)

    def http(
        self, method: str, url: str, headers: Mapping[str, str], _data: bytes | None = None
    ) -> Response:
        self.headers.append(headers)
        if self.api_status is not None:
            return Response(self.api_status, b"{}")
        path = url.removeprefix("https://api.github.com")
        if path.startswith(f"/repos/{REPO}/git/commits/"):  # verify_built reads the tree
            return Response(200, json.dumps({"tree": {"sha": "c" * 40}}).encode())
        tag_object = f"/repos/{REPO}/git/tags/"
        if path.startswith(tag_object):  # the hop an annotated tag takes to its commit
            sha = path.removeprefix(tag_object)
            commit = self.annotated.get(sha.removeprefix("tag-"))
            body = {"object": {"type": "commit", "sha": commit}}
            return Response(404 if commit is None else 200, json.dumps(body).encode())
        prefix = f"/repos/{REPO}/git/ref/tags/v"
        assert method == "GET" and path.startswith(prefix), path
        commit = self.release_tags.get(path.removeprefix(prefix))
        if commit is None and path.removeprefix(prefix) not in self.annotated:
            return Response(404, b"{}")
        if path.removeprefix(prefix) in self.annotated:
            tag = {"type": "tag", "sha": f"tag-{path.removeprefix(prefix)}"}
            return Response(200, json.dumps({"object": tag}).encode())
        return Response(200, json.dumps({"object": {"type": "commit", "sha": commit}}).encode())

    def copies(self) -> list[str]:
        return [a[-1].rpartition(":")[2] for a in self.argvs if a[:2] == ["skopeo", "copy"]]

    def signs(self) -> list[list[str]]:
        return [a for a in self.argvs if a[:2] == ["cosign", "sign"]]


def go(
    tools: FakeTools,
    *,
    release: str | None = None,
    plan_version: str | None = None,
    write: str = "success",
    result: dict | None = None,
    **extra: str,
) -> int:
    planned = plan_version or release
    event_name = extra.pop("event_name", "push")  # the job's event, not an env var
    env = {
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_SHA": SHA,
        "GITHUB_TOKEN": "tok",
        "SDLC_CHECK": json.dumps(
            {"image": True, "release": {"version": planned} if planned else None}
        ),
        "SDLC_IMAGE_RESULT": json.dumps(
            result or {"ref": REF, "digest": NEW, "sha": SHA, "tag": f"gated-{SHA}", "parent": ""}
        ),
        "SDLC_WRITE": write,
        "SDLC_RELEASE": json.dumps({"version": release, "sha": SHA}) if release else "",
        "QUAY_USERNAME": "robot",
        "QUAY_PASSWORD": "pw",
        "COSIGN_PRIVATE_KEY": "KEY",
        "COSIGN_PASSWORD": "pass",
        **extra,
    }
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
        env["RUNNER_TEMP"] = tmp
        return promote.run(event_name, {}, env, tools=tools)


def refused(
    tools: FakeTools,
    *,
    release: str | None = None,
    plan_version: str | None = None,
    write: str = "success",
    result: dict | None = None,
    **extra: str,
) -> str:
    """The message of the refusal (exit 2) `promote.run` raises, or a failed assertion."""
    try:
        go(
            tools,
            release=release,
            plan_version=plan_version,
            write=write,
            result=result,
            **extra,
        )
    except CannotRunError as exc:
        assert not tools.signs() and not tools.copies(), "acted after a refusal"
        return str(exc)
    raise AssertionError("expected a refusal (exit 2)")


def test_bad_digest_wrong_repo_wrong_sha_refused() -> None:
    good = {"ref": REF, "digest": NEW, "sha": SHA}
    for bad in (
        {**good, "digest": "sha256:xyz"},
        {**good, "digest": NEW.upper()},
        {**good, "ref": "quay.io/crunchtools/other"},
        {**good, "ref": f"{REF}:latest"},
        {**good, "sha": OTHER_SHA},
    ):
        t = FakeTools()
        refused(t, result=bad)
        assert not t.argvs, "a refused image reached a tool"
    t = FakeTools()
    refused(t, SDLC_IMAGE_RESULT="")


def test_image_must_exist() -> None:
    t = FakeTools()
    t.labels = {}
    t.image_missing = True
    assert "manifest unknown" in refused(t)


def test_release_needs_write_success() -> None:
    for write in ("skipped", "failure", "cancelled", ""):
        t = FakeTools()
        assert "`write` was needed" in refused(t, release="1.2.3", write=write), write


def test_release_version_comes_from_write() -> None:
    t = FakeTools()
    assert "does not match" in refused(t, release="1.2.3", plan_version="1.2.4")
    for loose in ("1.2", "01.2.3"):
        t = FakeTools()
        assert "not strict SemVer" in refused(t, release=loose, plan_version=loose), loose
    t = FakeTools()
    # The plan says a release and write output nothing: the plan alone is never enough.
    assert "missing or not JSON" in refused(t, plan_version="1.2.3")
    t = FakeTools()
    other = json.dumps({"version": "1.2.3", "sha": OTHER_SHA})
    assert "another commit" in refused(t, release="1.2.3", SDLC_RELEASE=other)


def test_release_moves_version_and_latest_and_signs_version() -> None:
    t = FakeTools()
    assert go(t, release="1.2.3") == 0
    assert t.tags == {"1.2.3": NEW, "latest": NEW}
    (sign,) = t.signs()
    notes = {sign[i + 1] for i, a in enumerate(sign) if a == "-a"}
    assert notes == {f"repo={REPO}", "version=1.2.3"} and sign[-1] == f"{REF}@{NEW}"
    assert t.attested == {(NEW, "spdxjson"), (NEW, "slsaprovenance1")}
    assert t.argvs.index(t.signs()[0]) < next(
        i for i, a in enumerate(t.argvs) if a[:2] == ["skopeo", "copy"]
    )


def test_latest_not_moved_backwards() -> None:
    t = FakeTools()
    t.tags["latest"] = OLD
    t.labels[OLD] = {"org.opencontainers.image.version": "2.0.0"}
    t.labels[NEW] = built_labels("1.9.1")
    assert go(t, release="1.9.1") == 0
    assert t.tags["latest"] == OLD and t.tags["1.9.1"] == NEW
    assert "rollback-latest" not in t.copies()


def test_release_moves_latest_forward_and_keeps_rollback() -> None:
    t = FakeTools()
    t.tags["latest"] = OLD
    t.labels[OLD] = {"org.opencontainers.image.version": "1.2.2"}
    assert go(t, release="1.2.3") == 0
    assert t.tags["latest"] == NEW and t.tags["rollback-latest"] == OLD


def test_version_tag_is_never_moved() -> None:
    t = FakeTools()
    t.tags["1.2.3"] = OLD
    assert "never moved" in refused(t, release="1.2.3")
    assert t.tags["1.2.3"] == OLD


def test_mislabelled_release_refused() -> None:
    t = FakeTools()
    t.labels[NEW] = built_labels("1.2.2")
    t.release_tags["1.2.2"] = OTHER_SHA
    assert "another release" in refused(t, release="1.2.3")
    t = FakeTools()  # a label that names no release of this repo is inherited, and fine
    t.labels[NEW] = built_labels("3.14.10")
    assert go(t, release="1.2.3") == 0


def test_tag_lookup_failure_is_a_2_and_404_is_no_tag() -> None:
    for status in (403, 500):
        t = FakeTools()  # a release whose image is labelled another version asks the API
        t.labels[NEW] = built_labels("1.2.2")
        t.api_status = status
        assert str(status) in refused(t, release="1.2.3"), status
        t = FakeTools()  # a rebuild asks whether v1.2.3 is on this commit
        t.api_status = status
        assert str(status) in refused(t), status
    t = FakeTools()
    t.api_status = 404
    assert go(t) == 0 and "1.2.3" not in t.tags
    t = FakeTools()
    t.labels[NEW] = built_labels("1.2.2")
    t.api_status = 404
    assert go(t, release="1.2.3") == 0


def test_token_is_sent_to_the_api_and_never_to_a_tool() -> None:
    t = FakeTools()
    assert go(t) == 0
    assert t.headers and all(h["Authorization"] == "Bearer tok" for h in t.headers)
    assert not any("GITHUB_TOKEN" in env for env in t.envs)
    t = FakeTools()
    assert "GITHUB_TOKEN is required" in refused(t, GITHUB_TOKEN="")
    assert not t.argvs


def test_label_mismatch_is_refused_not_skipped() -> None:
    for label in promote.BUILT_LABELS:
        for wrong in (OTHER_SHA, None):
            t = FakeTools()
            labels = built_labels()
            if wrong is None:
                del labels[label]
            else:
                labels[label] = wrong
            t.labels[NEW] = labels
            assert label in refused(t), (label, wrong)
            assert not t.attested, "attested a mismatched image"


def test_a_candidate_of_this_tree_is_promoted() -> None:
    t = FakeTools()
    labels = {k: OTHER_SHA if k in promote.BUILT_LABELS else v for k, v in built_labels().items()}
    t.labels[NEW] = {**labels, promote.TREE_LABEL: "c" * 40}
    assert go(t) == 0 and t.signs()


def test_a_candidate_of_another_tree_is_refused() -> None:
    t = FakeTools()
    labels = {k: OTHER_SHA if k in promote.BUILT_LABELS else v for k, v in built_labels().items()}
    t.labels[NEW] = {**labels, promote.TREE_LABEL: "d" * 40}
    assert "neither this run's" in refused(t) and not t.signs()


def test_an_adopted_build_records_the_merged_commit_as_its_source() -> None:
    labels = {
        **built_labels(),
        "com.crunchtools.build.builder": f"{REPO}/.github/workflows/ci.yml@refs/pull/4/merge",
    }
    labels["com.crunchtools.build.workflow"] = labels["com.crunchtools.build.builder"]
    pred = promote.provenance(labels, REPO, OTHER_SHA)
    assert pred is not None
    assert pred["buildDefinition"]["resolvedDependencies"][0]["digest"]["gitCommit"] == OTHER_SHA


def test_gated_tag_must_resolve_to_the_digest() -> None:
    for gated in (OLD, None):
        t = FakeTools()
        t.gated = gated
        assert f"gated-{SHA}" in refused(t, release="1.2.3"), gated
        assert not t.attested, "attested a digest the gated tag does not name"


def test_unhandled_event_is_refused() -> None:
    for name in ("pull_request", "pull_request_target", "issues", "release", ""):
        t = FakeTools()
        assert "does not run on" in refused(t, event_name=name), name
        assert not t.argvs, "an unhandled event reached a tool"
    for name in promote.EVENTS:
        assert go(FakeTools(), event_name=name) == 0, name


def test_non_release_moves_latest_only() -> None:
    t = FakeTools()
    t.tags["latest"] = OLD
    t.labels[OLD] = {"org.opencontainers.image.version": "1.2.3"}
    assert go(t) == 0
    assert t.copies() == ["rollback-latest", "latest"]
    assert set(t.tags) == {"latest", "rollback-latest"} and t.tags["latest"] == NEW
    (sign,) = t.signs()
    assert not any(a.startswith("version=") for a in sign)


def test_non_release_does_not_retag_an_inherited_version() -> None:
    # The Containerfile still says 1.2.3 and v1.2.3 is a different, older commit.
    t = FakeTools()
    t.release_tags["1.2.3"] = OTHER_SHA
    assert go(t) == 0
    assert "1.2.3" not in t.tags


def test_rebuild_of_the_release_moves_the_version_tag() -> None:
    t = FakeTools()
    t.release_tags["1.2.3"] = SHA
    try:
        go(t)
    except CannotRunError:
        raise AssertionError(
            "a rebuild of the release's commit must be able to re-tag it"
        ) from None
    assert t.tags["1.2.3"] == NEW and t.tags["latest"] == NEW
    t = FakeTools()  # and the weekly rebuild of an already-tagged release moves it forward
    t.release_tags["1.2.3"] = SHA
    t.tags["1.2.3"] = OLD
    assert go(t) == 0 and t.tags["1.2.3"] == NEW


def test_rebuild_of_an_annotated_release_tag_moves_it() -> None:
    t = FakeTools()
    t.annotated["1.2.3"] = SHA  # v1.2.3 is a tag object, one hop from this run's commit
    assert go(t) == 0 and t.tags["1.2.3"] == NEW
    t = FakeTools()  # the same, with the tag object pointing at another commit
    t.annotated["1.2.3"] = OTHER_SHA
    assert go(t) == 0 and "1.2.3" not in t.tags


def test_stale_rebuild_does_not_move_latest_back() -> None:
    t = FakeTools()
    t.tags["latest"] = OLD
    t.labels[OLD] = {"org.opencontainers.image.version": "2.0.0"}
    assert go(t) == 0
    assert t.tags["latest"] == OLD


def test_rerun_is_a_no_op() -> None:
    t = FakeTools()
    assert go(t, release="1.2.3") == 0
    before = dict(t.tags)
    t.argvs.clear()
    assert go(t, release="1.2.3") == 0
    assert t.tags == before
    assert not t.signs() and not t.copies()
    assert not any(a[:2] == ["cosign", "attest"] or a[0] == "syft" for a in t.argvs)


def test_wrong_signing_key_refused() -> None:
    t = FakeTools()
    t.key = "-----BEGIN PUBLIC KEY-----\nother\n-----END PUBLIC KEY-----\n"
    assert "not the private half" in refused(t)


def test_syft_runs_without_the_key_and_nothing_executes_the_image() -> None:
    t = FakeTools()
    assert go(t) == 0
    for argv, env in zip(t.argvs, t.envs, strict=True):
        if argv[0] == "syft" or argv[:2] == ["skopeo", "copy"]:
            assert "COSIGN_PRIVATE_KEY" not in env and "COSIGN_PASSWORD" not in env
        assert argv[0] in ("skopeo", "cosign", "syft"), f"{argv[0]} is not a registry tool"


def test_no_provenance_without_a_build_record() -> None:
    t = FakeTools()
    t.labels[NEW] = dict.fromkeys(promote.BUILT_LABELS, SHA)
    assert go(t) == 0
    assert t.attested == {(NEW, "spdxjson")}


def test_provenance_predicate_from_valid_labels() -> None:
    p = promote.provenance(built_labels(), REPO)
    assert p is not None
    source = f"https://github.com/{REPO}"
    assert p["buildDefinition"]["buildType"] == promote.BUILD_TYPE
    assert p["buildDefinition"]["externalParameters"]["workflow"] == {
        "ref": "refs/heads/main",
        "repository": source,
        "path": ".github/workflows/ci.yml",
    }
    assert p["buildDefinition"]["resolvedDependencies"] == [
        {"uri": f"git+{source}", "digest": {"gitCommit": SHA}}
    ]
    assert p["runDetails"]["builder"]["id"] == f"https://github.com/{WORKFLOW}"
    assert p["runDetails"]["metadata"]["invocationId"] == RUN


def test_provenance_refuses_malformed_or_mismatched_labels() -> None:
    other = "crunchtools/other"
    bad: list[dict[str, str]] = [
        {"com.crunchtools.build.run": f"https://github.com/{other}/actions/runs/7/attempts/1"},
        {"com.crunchtools.build.run": f"{RUN}/extra"},
        {"com.crunchtools.build.run": f"https://github.com/{REPO}/actions/runs/x/attempts/1"},
        {"com.crunchtools.build.run": ""},
        {"com.crunchtools.build.workflow": f"{REPO}/.github/workflows/other.yml@refs/heads/main"},
        {"com.crunchtools.build.workflow": f"{other}/.github/workflows/ci.yml@refs/heads/main"},
        {"com.crunchtools.build.builder": f"{REPO}/.github/workflows/ci.yml@refs/tags/v1"},
        {"com.crunchtools.build.commit": OTHER_SHA},
        {"com.crunchtools.build.commit": SHA[:7]},
        {"com.crunchtools.build.commit": SHA.upper()},
        {"org.opencontainers.image.revision": OTHER_SHA},
    ]
    for change in bad:
        assert promote.provenance({**built_labels(), **change}, REPO) is None, change
    assert promote.provenance({}, REPO) is None


def main() -> int:
    tests: list[Callable[[], None]] = [v for k, v in globals().items() if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"ok   {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
