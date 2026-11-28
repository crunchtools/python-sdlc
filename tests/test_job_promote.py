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

import base64
import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdlc_jobs import _core, _disk, promote
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
        self.gated_at: dict[str, str] = {}  # ref -> what its gated-<sha> resolves to, over gated
        self.api_status: int | None = None  # every GitHub answer, when set
        self.headers: list[Mapping[str, str]] = []
        self.image_missing = False  # the registry has no manifest at the digest
        self.manifests: dict[str, str] = {}  # digest -> the artifact manifest's bytes
        self.predicates: dict[tuple[str, str], object] = {}  # (digest, type) -> what was attested
        self.sbom_out: str | None = None  # verify-attestation's stdout over what was attested
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
            Path(argv[-1].split("=", 1)[1]).write_text('{"spdxVersion": "SPDX-2.3"}')
            return cp()
        assert argv[0] == "cosign", f"unexpected argv {argv}"
        return self.cosign(argv)

    def skopeo(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        target = argv[-1].removeprefix("docker://")
        if argv[1] == "inspect" and "--raw" in argv:
            digest = target.partition("@")[2] or self.tags.get(target.rpartition(":")[2], "")
            raw = self.manifests.get(digest)
            return cp(1, "", "manifest unknown") if raw is None else cp(0, raw)
        if argv[1] == "inspect" and "--format" in argv:
            tag = target.rpartition(":")[2]
            if tag == f"gated-{SHA}":
                gated = self.gated_at.get(target.rpartition(":")[0], self.gated)
                return cp(0, gated) if gated else cp(1, "", "manifest unknown")
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
                path = Path(argv[argv.index("--predicate") + 1])
                self.predicates[(digest, kind)] = json.loads(path.read_text())
            case "verify":
                rc = 0 if any(d == digest and notes <= n for d, n in self.signed) else 1
            case "verify-attestation":
                rc = 0 if (digest, kind) in self.attested else 1
                predicate = self.predicates.get((digest, kind), {})
                statement = base64.b64encode(json.dumps({"predicate": predicate}).encode())
                out = json.dumps({"payload": statement.decode()}) + "\n"
                if self.sbom_out is not None and kind == "spdxjson":
                    out = self.sbom_out
        return cp(rc, out)

    def http(
        self, method: str, url: str, headers: Mapping[str, str], _data: bytes | None = None
    ) -> Response:
        self.headers.append(headers)
        if self.api_status is not None:
            return Response(self.api_status, b"{}")
        path = url.removeprefix("https://api.github.com")
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


def test_every_cosign_reads_large_attachments() -> None:
    # A workstation SBOM attestation is over cosign's 128 MiB default (a1654).
    t = FakeTools()
    assert go(t) == 0
    cosigns = [e for a, e in zip(t.argvs, t.envs, strict=True) if a[0] == "cosign"]
    assert cosigns and all(e.get("COSIGN_MAX_ATTACHMENT_SIZE") == "512MiB" for e in cosigns)


def test_every_signature_is_written_in_the_legacy_format() -> None:
    # cosign v3's default bundle is an OCI referrer podman and bootc cannot read (a1751).
    t = FakeTools()
    assert go(t) == 0
    writes = [a for a in t.argvs if a[:2] in (["cosign", "sign"], ["cosign", "attest"])]
    assert writes and all(
        "--use-signing-config=false" in a and "--new-bundle-format=false" in a for a in writes
    )


def test_a_planned_boot_must_have_succeeded() -> None:
    planned = json.dumps({"image": True, "boot": True, "release": None})
    for result in ("failure", "skipped", "cancelled", ""):
        t = FakeTools()
        assert "`boot` was needed" in refused(t, SDLC_CHECK=planned, SDLC_BOOT=result), result
    t = FakeTools()
    assert go(t, SDLC_CHECK=planned, SDLC_BOOT="success") == 0 and t.signs()
    t = FakeTools()  # no boot planned: a skipped boot, or none at all, is no objection
    assert go(t, SDLC_BOOT="skipped") == 0 and go(FakeTools()) == 0


def test_a_planned_boot_with_no_boot_result_proceeds_for_the_old_fleet_file() -> None:
    planned = json.dumps({"image": True, "boot": True, "release": None})
    t = FakeTools()  # a fleet file older than the boot job maps no SDLC_BOOT at all
    assert go(t, SDLC_CHECK=planned) == 0 and t.signs()
    t = FakeTools()
    assert "`boot` was needed" in refused(t, SDLC_CHECK=planned, SDLC_BOOT="failure")
    t = FakeTools()
    assert go(t, SDLC_CHECK=planned, SDLC_BOOT="success") == 0 and t.signs()


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
    t.release_tags["2.0.0"] = OTHER_SHA
    t.labels[NEW] = built_labels("1.9.1")
    assert go(t, release="1.9.1") == 0
    assert t.tags["latest"] == OLD and t.tags["1.9.1"] == NEW
    assert "rollback-latest" not in t.copies()


def test_latest_labelled_with_its_base_version_still_moves() -> None:
    # Hummingbird's python labels 3.14.10; latest inherited it, and it is no release here.
    t = FakeTools()
    t.tags["latest"] = OLD
    t.labels[OLD] = {"org.opencontainers.image.version": "3.14.10", "version": "0.8.2"}
    t.labels[NEW] = built_labels("0.9.0")
    assert go(t, release="0.9.0") == 0
    assert t.tags["latest"] == NEW and t.tags["rollback-latest"] == OLD


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


def test_rebuild_reads_the_repos_version_over_its_bases() -> None:
    # hermes: the repo labels version 1.2.0; hummingbird/python's OCI version is 3.13.18.
    t = FakeTools()
    t.labels[NEW] = {**built_labels("3.13.18"), "version": "1.2.0"}
    t.release_tags["1.2.0"] = SHA
    assert go(t) == 0 and t.tags["1.2.0"] == NEW and t.tags["latest"] == NEW


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
    t.release_tags["2.0.0"] = OTHER_SHA
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


# ------------------------------------------------------------------ local release

LOCAL = f"https://github.com/crunchtools/sdlc/blob/{'c' * 40}/scripts/local-release.sh"
LOCAL_ENV = {
    "SDLC_LOCAL_BUILDER": LOCAL,
    "GITHUB_RUN_ATTEMPT": "42",
    "GITHUB_REF": "refs/heads/main",
}


def local_labels() -> dict[str, str]:
    """What image labels a scripts/local-release.sh build: no run id, the run's attempt."""
    return {
        **built_labels(),
        "com.crunchtools.build.run": f"https://github.com/{REPO}/actions/runs//attempts/42",
        "com.crunchtools.build.builder": "",
        "com.crunchtools.build.workflow": "",
    }


def local_job(labels: dict[str, str], **env: str) -> promote.Job:
    full = {"GITHUB_SHA": SHA, **LOCAL_ENV, **env}
    return promote.Job(FakeTools(), full, REPO, REF, NEW, Path("."), labels)


def test_a_local_release_build_is_attested_with_provenance() -> None:
    t = FakeTools()
    t.labels[NEW] = local_labels()
    assert go(t, **LOCAL_ENV) == 0
    assert t.attested == {(NEW, "spdxjson"), (NEW, "slsaprovenance1")}


def test_local_provenance_names_the_script_and_this_run() -> None:
    p = promote.local_provenance(local_job(local_labels()))
    assert p is not None
    source = f"https://github.com/{REPO}"
    # A local-release build is no Actions workflow and must never claim to be one.
    assert p["buildDefinition"]["buildType"] == promote.LOCAL_BUILD_TYPE
    assert p["buildDefinition"]["buildType"] != promote.BUILD_TYPE
    assert p["buildDefinition"]["externalParameters"] == {
        "script": {
            "repository": "https://github.com/crunchtools/sdlc",
            "path": "scripts/local-release.sh",
            "commit": "c" * 40,
        },
        "source": {"repository": source, "ref": "refs/heads/main"},
    }
    assert ".github/workflows" not in json.dumps(p)
    assert p["buildDefinition"]["resolvedDependencies"] == [
        {"uri": f"git+{source}", "digest": {"gitCommit": SHA}}
    ]
    assert p["runDetails"]["builder"]["id"] == LOCAL
    assert p["runDetails"]["metadata"]["invocationId"] == f"{LOCAL}#attempt-42"


def test_local_provenance_only_for_this_local_runs_own_build() -> None:
    assert promote.local_provenance(local_job(local_labels(), SDLC_LOCAL_BUILDER="")) is None
    bad_env: list[dict[str, str]] = [
        {"SDLC_LOCAL_BUILDER": LOCAL.replace("c" * 40, "main")},
        {"SDLC_LOCAL_BUILDER": LOCAL.replace("crunchtools/sdlc", "someone/sdlc")},
        {"GITHUB_RUN_ID": "7"},
        {"GITHUB_RUN_ATTEMPT": "43"},
        {"GITHUB_RUN_ATTEMPT": ""},
        {"GITHUB_REF": "refs/tags/v1.2.3"},
        {"GITHUB_SHA": OTHER_SHA},
    ]
    for change in bad_env:
        assert promote.local_provenance(local_job(local_labels(), **change)) is None, change
    bad_labels: list[dict[str, str]] = [
        {"com.crunchtools.build.run": RUN},
        {"com.crunchtools.build.builder": WORKFLOW},
        {"com.crunchtools.build.workflow": WORKFLOW},
        {"com.crunchtools.build.commit": OTHER_SHA},
        {"org.opencontainers.image.revision": OTHER_SHA},
    ]
    for change in bad_labels:
        assert promote.local_provenance(local_job({**local_labels(), **change})) is None, change


def test_a_ci_build_keeps_its_own_provenance_under_a_local_promote() -> None:
    job = local_job(built_labels())
    assert promote.local_provenance(job) is None
    p = promote.provenance(job.labels, REPO)
    assert p is not None and p["runDetails"]["metadata"]["invocationId"] == RUN


# ------------------------------------------------------------------ many images

_core.EXTRA_IMAGES[REPO] = ("demo-base",)  # what sdlc would review


def one(ref: str, digest: str = NEW) -> dict:
    return {
        "name": ref.rpartition("/")[2],
        "ref": ref,
        "digest": digest,
        "sha": SHA,
        "tag": f"gated-{SHA}",
    }


def test_many_images_are_each_signed_as_this_repo() -> None:
    t = FakeTools()
    t.labels[OLD] = built_labels()
    t.gated_at[f"{REF}-base"] = OLD
    rc = go(t, result={"images": [one(f"{REF}-base", OLD), one(REF)], "sha": SHA})
    assert rc == 0
    signed = [a[-1] for a in t.signs()]
    assert signed == [f"{REF}-base@{OLD}", f"{REF}@{NEW}"], signed
    assert all(f"repo={REPO}" in a for a in t.signs())


def test_an_image_this_repo_may_not_publish_is_refused_before_anything() -> None:
    for ref in (
        "quay.io/crunchtools/rotv-base",
        "quay.io/crunchtools/demo-evil",
        f"{REF}-base:latest",
    ):
        t = FakeTools()
        msg = refused(t, result={"images": [one(REF), one(ref)]})
        assert "is not exactly one of" in msg and not t.argvs, (ref, msg)
    for bad in ({"images": []}, {"images": "x"}, {"images": [one(REF), one(REF)]}):
        t = FakeTools()
        refused(t, result=bad)
        assert not t.argvs, bad


def test_trentina_may_promote_its_l2_model_and_no_other_repo_may() -> None:
    """The real allowlist (a1793): trentina-l2-model is trentina's, not rotv's."""
    ref = "quay.io/crunchtools/trentina-l2-model"
    env = {"GITHUB_SHA": SHA, "SDLC_IMAGE_RESULT": json.dumps({"images": [one(ref)], "sha": SHA})}
    assert promote.validate_image(env, "crunchtools/trentina") == [(ref, NEW)]
    msg = ""
    try:
        promote.validate_image(env, "crunchtools/rotv")
    except CannotRunError as exc:
        msg = str(exc)
    assert "is not exactly one of" in msg, msg


def test_one_image_that_does_not_check_out_signs_none() -> None:
    t = FakeTools()
    t.labels[OLD] = {}
    msg = refused(t, result={"images": [one(REF), one(f"{REF}-base", OLD)]})
    assert "not this run's" in msg, msg


# ------------------------------------------------------------------ a bootc disk

DISK_BASE = _disk.Disk("raw", "xfs")


def manifest_of(image: str = NEW, blob: str = "c" * 64) -> tuple[str, str]:
    """(digest, bytes) of a disk artifact built from `image`, as `image` would push it."""
    notes = _disk.notes_for(built_labels(), REF, image, DISK_BASE)
    raw = _disk.manifest(blob, 1234, notes).decode()
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest(), raw


def with_disk(t: FakeTools) -> tuple[str, dict]:
    """A disk in the registry as disk-gated-<sha>; its digest and the image's result."""
    digest, raw = manifest_of()
    t.manifests[digest] = raw
    t.tags[_disk.gated_tag(SHA)] = digest
    disk = {"ref": REF, "digest": digest, "tag": _disk.gated_tag(SHA)}
    return digest, {**one(REF), "disk": disk}


def held_disk(t: FakeTools, tag: str) -> str:
    """Another disk, held by `disk-<tag>`."""
    digest, raw = manifest_of(OLD, "e" * 64)
    t.manifests[digest] = raw
    t.tags[_disk.TAG_PREFIX + tag] = digest
    return digest


def test_no_disk_is_todays_behaviour() -> None:
    t = FakeTools()
    assert go(t, release="1.2.3") == 0
    assert not any("--raw" in a for a in t.argvs)
    assert t.copies() == ["1.2.3", "latest"] and len(t.signs()) == 1
    assert t.attested == {(NEW, "spdxjson"), (NEW, "slsaprovenance1")}


def test_a_disk_that_does_not_check_out_signs_nothing() -> None:
    t = FakeTools()
    digest, result = with_disk(t)
    disk = result["disk"]
    for bad in (
        "x",
        {**disk, "ref": f"{REF}-base"},
        {**disk, "digest": "sha256:xyz"},
        {**disk, "digest": digest.upper()},
        {**disk, "tag": "disk-gated-" + OTHER_SHA},
        {**disk, "tag": f"gated-{SHA}"},
    ):
        t = FakeTools()
        with_disk(t)
        refused(t, result={**result, "disk": bad})
        assert not t.argvs, "a refused disk reached a tool"

    def served(raw: str) -> tuple[FakeTools, dict]:
        """A registry whose disk-gated-<sha> is `raw`, and the result naming it."""
        t = FakeTools()
        sha = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
        t.manifests[sha] = raw
        t.tags[_disk.gated_tag(SHA)] = sha
        return t, {**result, "disk": {**disk, "digest": sha}}

    doc = json.loads(manifest_of()[1])
    doc["annotations"][_disk.BUILDER] = "quay.io/x/bib:latest"
    unpinned = json.dumps(doc, separators=(",", ":"))
    cases = {
        "not a disk sdlc built": served('{"layers":[]}'),
        "base.digest": served(manifest_of(OLD)[1]),
        "not a digest-pinned BIB": served(unpinned),
    }
    t, bad = served(manifest_of()[1])
    t.manifests[bad["disk"]["digest"]] = manifest_of(blob="f" * 64)[1]
    cases["hashes to"] = (t, bad)
    t, bad = served(manifest_of()[1])
    t.tags.pop(_disk.gated_tag(SHA))
    cases["is missing"] = (t, bad)
    t, bad = served(manifest_of()[1])
    t.tags[_disk.gated_tag(SHA)] = held_disk(t, "x")
    cases["not the disk"] = (t, bad)
    for want, (t, given) in cases.items():
        msg = refused(t, result=given)
        assert want in msg, (want, msg)
        assert not any(a[:2] == ["cosign", "attest"] for a in t.argvs)


def test_a_disk_is_signed_and_given_the_images_sbom_and_a_provenance() -> None:
    t = FakeTools()
    disk, result = with_disk(t)
    assert go(t, release="1.2.3", result=result) == 0
    signed = dict(t.signed)
    assert signed[disk] == {f"repo={REPO}", "version=1.2.3"} and NEW in signed
    assert t.attested == {
        (NEW, "spdxjson"),
        (NEW, "slsaprovenance1"),
        (disk, "spdxjson"),
        (disk, "slsaprovenance1"),
    }
    assert t.predicates[(disk, "spdxjson")] == t.predicates[(NEW, "spdxjson")]
    assert not any(a[0] == "syft" and disk in a[2] for a in t.argvs)
    sbom_reads = [a for a in t.argvs if a[1:2] == ["verify-attestation"] and a[-1].endswith(NEW)]
    assert sbom_reads
    p = t.predicates[(disk, "slsaprovenance1")]
    assert isinstance(p, dict)
    build, image = p["buildDefinition"], t.predicates[(NEW, "slsaprovenance1")]
    assert isinstance(image, dict)
    assert build["buildType"] == promote.DISK_BUILD_TYPE
    assert (
        build["externalParameters"]["workflow"]
        == image["buildDefinition"]["externalParameters"]["workflow"]
    )
    assert build["externalParameters"]["disk"] == {
        "type": "raw",
        "rootfs": "xfs",
        "builder": _disk.BIB,
    }
    assert build["resolvedDependencies"] == [
        {"uri": f"git+https://github.com/{REPO}", "digest": {"gitCommit": SHA}},
        {"uri": f"oci://{REF}", "digest": {"sha256": "a" * 64}},
        {
            "uri": "oci://quay.io/centos-bootc/bootc-image-builder",
            "digest": {"sha256": _disk.BIB.rpartition("sha256:")[2]},
        },
    ]
    assert p["runDetails"] == image["runDetails"]


def test_a_disk_without_a_build_record_gets_no_provenance() -> None:
    t = FakeTools()
    t.labels[NEW] = dict.fromkeys(promote.BUILT_LABELS, SHA)
    disk, result = with_disk(t)
    assert go(t, result=result) == 0
    assert t.attested == {(NEW, "spdxjson"), (disk, "spdxjson")}


def test_a_release_moves_the_disks_version_and_latest_tags_with_a_rollback() -> None:
    t = FakeTools()
    disk, result = with_disk(t)
    t.tags["latest"] = OLD
    t.labels[OLD] = {"org.opencontainers.image.version": "1.2.2"}
    old = held_disk(t, "latest")
    assert go(t, release="1.2.3", result=result) == 0
    assert t.tags["disk-1.2.3"] == disk and t.tags["disk-latest"] == disk
    assert t.tags["disk-rollback-latest"] == old
    copies = [a for a in t.argvs if a[:2] == ["skopeo", "copy"] and ":disk-" in a[-1]]
    assert len(copies) == 3 and all("--preserve-digests" in a for a in copies)
    assert f"docker://{REF}@{old}" in copies[1] and f"docker://{REF}@{disk}" in copies[2]
    assert t.copies().index("disk-rollback-latest") < t.copies().index("disk-latest")


def test_a_non_release_moves_only_disk_latest() -> None:
    t = FakeTools()
    disk, result = with_disk(t)
    assert go(t, result=result) == 0
    assert t.tags["disk-latest"] == disk and "disk-1.2.3" not in t.tags


def test_a_latest_that_stays_leaves_disk_latest() -> None:
    t = FakeTools()
    disk, result = with_disk(t)
    t.tags["latest"] = OLD
    t.labels[OLD] = {"org.opencontainers.image.version": "2.0.0"}
    t.release_tags["2.0.0"] = OTHER_SHA
    t.labels[NEW] = built_labels("1.9.1")
    other = held_disk(t, "latest")
    assert go(t, release="1.9.1", result=result) == 0
    assert t.tags["disk-latest"] == other and t.tags["disk-1.9.1"] == disk


def test_a_rerun_with_everything_in_place_does_nothing() -> None:
    t = FakeTools()
    _, result = with_disk(t)
    assert go(t, release="1.2.3", result=result) == 0
    t.argvs.clear()
    assert go(t, release="1.2.3", result=result) == 0
    assert not t.signs() and not t.copies()
    assert not any(a[:2] == ["cosign", "attest"] or a[0] == "syft" for a in t.argvs)


def test_a_disk_version_tag_held_elsewhere_is_never_moved() -> None:
    t = FakeTools()
    _, result = with_disk(t)
    t.tags["1.2.3"] = NEW  # the image's is already here: nothing moved it this run
    old = held_disk(t, "1.2.3")
    assert "never moved" in refused(t, release="1.2.3", result=result)
    assert t.tags["disk-1.2.3"] == old


def test_a_first_release_never_moves_a_held_disk_version_tag() -> None:
    """The image's version tag is free, so this run moves it; the disk's is not a rebuild."""
    t = FakeTools()
    _, result = with_disk(t)
    old = held_disk(t, "1.2.3")
    assert "never moved" in refused(t, release="1.2.3", result=result)
    assert t.tags["disk-1.2.3"] == old


def test_a_rebuild_of_the_release_moves_the_disks_version_tag() -> None:
    t = FakeTools()
    disk, result = with_disk(t)
    t.release_tags["1.2.3"] = SHA
    t.tags["1.2.3"] = OLD
    held_disk(t, "1.2.3")
    assert go(t, result=result) == 0
    assert t.tags["disk-1.2.3"] == disk and t.tags["1.2.3"] == NEW


def test_a_disk_without_an_image_sbom_is_a_2() -> None:
    t = FakeTools()
    disk, result = with_disk(t)
    t.sbom_out = ""
    msg = ""
    try:
        go(t, result=result)
    except CannotRunError as exc:
        msg = str(exc)
    assert "no verified SBOM" in msg, msg
    assert not any(a[:2] == ["cosign", "attest"] and a[-1].endswith(disk) for a in t.argvs)


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
