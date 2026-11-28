"""The `promote` job: sign, attest and move tags on a digest it did not build.

It holds the cosign key and the quay robot, and nothing that builds: it never runs
the image and never calls the container socket. `image` built and gated the digest;
this job trusts that output for one thing, WHICH digest, and re-validates the rest: a
well-formed digest, the ref that is exactly `quay.io/crunchtools/<this repo>` or one
EXTRA_IMAGES (sdlc_jobs/_core.py) gives this repo, the sha that is this run's, and an
image that exists at that digest. A repo with more than one image hands on
`{"images": [...]}`, and each is promoted in turn, by the same rules.

A release is a plan until `write` agrees. When `check`'s plan has `release`, `write`
must have succeeded, and the version comes ONLY from `write`'s output `SDLC_RELEASE`,
`{"version": "X.Y.Z", "sha": "<merge commit>"}`, never from the plan (which must agree
with it). The sha is the commit `write` tagged, and it must be this run's.

Tags follow a rollback-safe rule, because the job is queued (`queue: max`) and a stale
run can arrive after a newer one:
- a release moves `X.Y.Z` and `latest`; a version tag is never moved to another digest,
  and `latest` is left alone when it already carries a newer version;
- anything else (a default-branch push, the weekly or parent rebuild) moves `latest`
  under the same rule, plus the version the image carries when that release's tag is on
  this very commit (a rebuild of the release, not a later commit that inherits the label);
- the digest `latest` held is kept as `rollback-latest` first, as container-release did.
A re-run with the same inputs signs, attests and moves nothing and exits 0.

Signing, the SBOM and the SLSA v1 provenance port `scripts/sign-digest.sh` and
`scripts/attest-digest.sh` with the fleet key. syft parses the image, so it runs without
the key in its environment; only `cosign sign` and `cosign attest` see it. A build
`scripts/local-release.sh` ran records no Actions run; its provenance names that script
as the builder instead, and only when the image's own run label is this very local
run's (`local_provenance`).

A bootc image that declares a disk (sdlc_jobs/_disk.py) hands on its artifact as
`disk` in the image's result, and the disk rides the image: it is re-validated before
anything is signed (manifest bytes at the digest, annotations naming this commit and
this image digest, `disk-gated-<sha>` on it), signed like the image, and given the
image's own SBOM, since its filesystem is the image's tree, and a provenance that
names the image and BIB it was built from. It takes `disk-<tag>` for each tag the
image holds after this run, never moving a version tag the image did not move.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from sdlc_jobs._core import (
    CLEAN,
    DIGEST,
    EXTRA_IMAGES,
    FLEET_PUBLIC_KEY,
    SEMVER,
    Response,
    Tools,
    call,
    cannot,
    must,
    needed,
    plan,
    step,
)
from sdlc_jobs._disk import (
    BUILDER,
    DISK_ROOTFS,
    DISK_TYPE,
    TAG_PREFIX,
    check_manifest,
    gated_tag,
)
from sdlc_jobs.image import ORG, QUAY

# The repo's own `version` first: a base sets both, and a repo that labels its version
# usually overrides only `version` (hummingbird/python's OCI version is 3.13.x or 3.14.x).
VERSION_LABELS = ("version", "org.opencontainers.image.version")
MISSING = re.compile(r"manifest unknown|not found|no such|name unknown", re.IGNORECASE)
COMMIT = re.compile(r"[0-9a-f]{40}")
RELEASED_REF = re.compile(r"refs/heads/.+")
BUILD_TYPE = "https://slsa-framework.github.io/github-actions-buildtypes/workflow/v1"
DISK_BUILD_TYPE = "https://crunchtools.com/sdlc/buildtypes/bootc-disk/v1"
WORKFLOW = ".github/workflows/ci.yml@"
# local-release.sh is no workflow: its builds say so, with a build type of their own.
LOCAL_BUILD_TYPE = "https://crunchtools.com/sdlc/buildtypes/local-release/v1"
LOCAL_SCRIPT = "scripts/local-release.sh"
SDLC = "https://github.com/crunchtools/sdlc"
# What scripts/local-release.sh names itself as, pinned to the sdlc commit it ran from.
LOCAL_BUILDER = re.compile(
    r"https://github\.com/crunchtools/sdlc/blob/([0-9a-f]{40})/scripts/local-release\.sh"
)
OK = 200
NOT_FOUND = 404
EVENTS = ("push", "schedule", "workflow_dispatch")  # image's `if:` less pull_request
BUILT_LABELS = ("org.opencontainers.image.revision", "com.crunchtools.build.commit")
SECRET_KEYS = ("COSIGN_PRIVATE_KEY", "COSIGN_PASSWORD")
UNDECODABLE = (ValueError, KeyError, TypeError)  # an attestation line that is no statement


# cosign refuses to read an attachment over 128 MiB by default, after it was
# happy to upload it: rhel10-bootc-workstation's SBOM attestation is 185 MB, so
# its own verify-attestation failed (a1654). Every cosign here reads with this.
MAX_ATTACHMENT = "512MiB"


@dataclass
class Job:
    tools: Tools
    env: Mapping[str, str]
    repo: str
    ref: str
    digest: str
    scratch: Path
    labels: dict[str, str] = field(default_factory=dict)
    disk: str = ""  # the digest of the image's disk artifact, "" for an image without one
    notes: dict[str, str] = field(default_factory=dict)  # the disk's annotations, checked

    @property
    def at(self) -> str:
        return f"{self.ref}@{self.digest}"

    def child(self, *, key: bool = False) -> dict[str, str]:
        """The environment for a tool; the cosign key only when it must have it."""
        env = {
            k: v
            for k, v in self.env.items()
            if k != "GITHUB_TOKEN" and (key or k not in SECRET_KEYS)
        }
        env["DOCKER_CONFIG"] = str(self.scratch / "docker")
        env["REGISTRY_AUTH_FILE"] = str(self.scratch / "docker" / "config.json")
        env["COSIGN_MAX_ATTACHMENT_SIZE"] = MAX_ATTACHMENT
        return env

    def run(self, argv: list[str], *, key: bool = False) -> subprocess.CompletedProcess[str]:
        return self.tools.run(argv, env=self.child(key=key))

    def must(self, argv: list[str], *, key: bool = False, input: str | None = None) -> str:
        return must(self.tools, argv, input=input, env=self.child(key=key))


def parse_json(raw: str | None, what: str) -> dict:
    try:
        doc = json.loads(raw or "")
    except ValueError as exc:
        cannot(f"{what} is missing or not JSON: {exc}")
    if not isinstance(doc, dict):
        cannot(f"{what} is not an object")
    return doc


def validate_image(env: Mapping[str, str], repo: str) -> list[tuple[str, str]]:
    """(ref, digest) of each image `image` built, after every check it does not get to skip."""
    return [(ref, digest) for ref, digest, _ in validate_images(env, repo)]


def validate_images(env: Mapping[str, str], repo: str) -> list[tuple[str, str, str]]:
    """(ref, digest, disk digest or "") of each image `image` built, checked."""
    doc = parse_json(env.get("SDLC_IMAGE_RESULT"), "SDLC_IMAGE_RESULT")
    docs = doc.get("images") if "images" in doc else [doc]
    if not isinstance(docs, list) or not docs or not all(isinstance(d, dict) for d in docs):
        cannot("SDLC_IMAGE_RESULT images is not a list of images")
    own = repo.rpartition("/")[2]
    allowed = {f"{QUAY}/{ORG}/{n}" for n in (own, *EXTRA_IMAGES.get(repo, ()))}
    sha = env.get("GITHUB_SHA", "")
    out: list[tuple[str, str, str]] = []
    for one in docs:
        ref = one.get("ref")
        if not (isinstance(one.get("digest"), str) and DIGEST.fullmatch(one["digest"])):
            cannot("image's digest is not a well-formed sha256 digest")
        if ref not in allowed:
            cannot(f"image's ref {ref!r} is not exactly one of {', '.join(sorted(allowed))}")
        if not COMMIT.fullmatch(sha) or one.get("sha") != sha:
            cannot(f"image built {one.get('sha')!r}, not this run's commit {sha!r}")
        if ref in (r for r, _, _ in out):
            cannot(f"image's result names {ref} twice")
        out.append((ref, one["digest"], disk_digest(one, sha) if "disk" in one else ""))
    return out


def disk_digest(one: Mapping, sha: str) -> str:
    """The digest of the disk an image's result carries; anything malformed is a 2."""
    disk = one.get("disk")
    if not isinstance(disk, dict):
        cannot("image's disk is not an object")
    if disk.get("ref") != one.get("ref"):
        cannot(f"the disk's ref {disk.get('ref')!r} is not its image's {one.get('ref')!r}")
    digest = disk.get("digest")
    if not (isinstance(digest, str) and DIGEST.fullmatch(digest)):
        cannot("the disk's digest is not a well-formed sha256 digest")
    if disk.get("tag") != gated_tag(sha):
        cannot(f"the disk's tag {disk.get('tag')!r} is not {gated_tag(sha)}")
    return digest


def release_version(env: Mapping[str, str], planned: object) -> str | None:
    """The version `write` released, or None when the plan has no release."""
    if not planned:
        return None
    needed("write", env.get("SDLC_WRITE"))
    doc = parse_json(env.get("SDLC_RELEASE"), "SDLC_RELEASE (write's output)")
    version = doc.get("version")
    if not (isinstance(version, str) and SEMVER.fullmatch(version)):
        cannot(f"SDLC_RELEASE version {version!r} is not strict SemVer")
    if doc.get("sha") != env.get("GITHUB_SHA"):
        cannot("SDLC_RELEASE is for another commit than this run's")
    if not isinstance(planned, dict) or planned.get("version") != version:
        cannot(f"the plan's release does not match write's release {version}: refused")
    return version


def run(event_name: str, event: dict, env: Mapping[str, str], tools: Tools) -> int:
    """Sign, attest and tag the digest `image` pushed; 0 when it is promoted.

    Everything comes from `env`: GITHUB_REPOSITORY, SDLC_IMAGE_RESULT (image's
    output), SDLC_CHECK, SDLC_BOOT, SDLC_WRITE and SDLC_RELEASE, and the signing key.
    `tools` runs skopeo and cosign (a fake in the suites). Raises
    CannotRunError, exit 2, when an input is malformed or would move a version
    backwards, or a signing or tagging step fails.
    """
    del event  # the job contract passes it; promote reads only the environment
    if event_name not in EVENTS:
        cannot(f"promote does not run on {event_name!r}: only {', '.join(EVENTS)}")
    repo = env.get("GITHUB_REPOSITORY", "")
    if repo.count("/") != 1:
        cannot("GITHUB_REPOSITORY is not owner/name")
    if not env.get("GITHUB_TOKEN"):
        cannot("GITHUB_TOKEN is required: a tag read without it fails open on a private repo")
    built = validate_images(env, repo)
    # The digest's own boot proof (sdlc_jobs/boot.py) passed. A fleet file older than
    # the boot job maps no SDLC_BOOT: there the keys stay unenforced, as before it.
    if plan(env).get("boot") and "SDLC_BOOT" in env:
        needed("boot", env["SDLC_BOOT"])
    version = release_version(env, plan(env).get("release"))
    scratch = Path(tempfile.mkdtemp(prefix="sdlc-promote-", dir=env.get("RUNNER_TEMP") or None))
    scratch.chmod(0o700)
    (scratch / "docker").mkdir()
    jobs = [Job(tools, env, repo, ref, digest, scratch, disk=disk) for ref, digest, disk in built]
    try:
        login(jobs[0])
        # Every image and disk is checked, and its tag moves decided, before anything is signed.
        planned = [(job, *plan_job(job, version)) for job in jobs]
        for job, moves, disk_moves in planned:
            sign(job, version)
            attest(job)
            move_tags(job, moves)
            if job.disk:
                promote_disk(job, version, disk_moves)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return CLEAN


def plan_job(job: Job, version: str | None) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Check one image and its disk; the image's tag copies and the disk's, nothing yet done."""
    job.labels = inspect_labels(job)
    verify_built(job)
    if version:
        refuse_mislabelled(job, version)
    moves = plan_tags(job, version)
    if not job.disk:
        return moves, []
    job.notes = verify_disk(job)
    return moves, plan_disk_tags(job, moves, version)


def login(job: Job) -> None:
    user, password = job.env.get("QUAY_USERNAME"), job.env.get("QUAY_PASSWORD")
    if not user or not password:
        cannot("QUAY_USERNAME and QUAY_PASSWORD are required")
    config = job.scratch / "docker" / "config.json"
    job.must(
        ["skopeo", "login", "--authfile", str(config), "-u", user, "--password-stdin", QUAY],
        input=password,
    )


def inspect_labels(job: Job) -> dict[str, str]:
    step(f"the image at {job.at}")
    doc = parse_json(job.must(["skopeo", "inspect", f"docker://{job.at}"]), "skopeo inspect")
    return doc.get("Labels") or {}


def verify_built(job: Job) -> None:
    """The image at the digest is this run's commit, and `image` pushed it as gated-<sha>.

    Anything else is a refusal, never a skip: a digest that is not what this commit built
    does not get signed or attested.
    """
    sha = job.env.get("GITHUB_SHA", "")
    for label in BUILT_LABELS:
        if job.labels.get(label) != sha:
            cannot(f"{job.at} carries {label}={job.labels.get(label)!r}, not this run's {sha}")
    gated = digest_of(job, f"gated-{sha}")
    if gated != job.digest:
        cannot(f"{job.ref}:gated-{sha} is {gated or 'missing'}, not the digest {job.digest}")


def label_version(labels: Mapping[str, str]) -> tuple[int, int, int] | None:
    raw = next((labels[k] for k in VERSION_LABELS if labels.get(k)), "")
    m = SEMVER.fullmatch(raw.strip().removeprefix("v"))
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def tuple_of(version: str) -> tuple[int, int, int]:
    m = SEMVER.fullmatch(version)
    if not m:
        cannot(f"{version!r} is not strict SemVer")
    return int(m[1]), int(m[2]), int(m[3])


def refuse_mislabelled(job: Job, version: str) -> None:
    """A release whose image is labelled ANOTHER release of this repo is refused (#171).

    A label that names no release of this repo is inherited from the base and says
    nothing about this one.
    """
    carried = label_version(job.labels)
    if carried is None or carried == tuple_of(version):
        return
    name = "{}.{}.{}".format(*carried)
    if get(job, f"/repos/{job.repo}/git/ref/tags/v{name}").status == OK:
        cannot(f"release {version} would publish an image labelled {name}, another release")


# ------------------------------------------------------------------ sign, attest


def verifies(job: Job, *extra: str, kind: str = "verify") -> bool:
    pub = job.scratch / "fleet.pub"
    pub.write_text(FLEET_PUBLIC_KEY)
    argv = ["cosign", kind, "--insecure-ignore-tlog", "--key", str(pub), *extra, job.at]
    return job.run(argv).returncode == 0


# cosign v3 signs into a protobuf bundle stored as an OCI referrer, which podman
# and bootc cannot read, and refuses --tlog-upload=false under its default
# signing config. These two keep v3 writing the .sig/.att tags v2 did, which
# every verifier in the fleet reads; cosign v2.6.5 accepts them too (a1751).
LEGACY_FORMAT = ["--use-signing-config=false", "--new-bundle-format=false"]


def sign(job: Job, version: str | None) -> None:
    step(f"sign {job.at}")
    if not job.env.get("COSIGN_PRIVATE_KEY"):
        cannot("no COSIGN_PRIVATE_KEY in the cosign-signing environment")
    mine = job.must(["cosign", "public-key", "--key", "env://COSIGN_PRIVATE_KEY"], key=True)
    if mine.strip() != FLEET_PUBLIC_KEY.strip():
        cannot("COSIGN_PRIVATE_KEY is not the private half of the fleet key")
    notes = [f"repo={job.repo}"] + ([f"version={version}"] if version else [])
    flags = [a for n in notes for a in ("-a", n)]
    if verifies(job, *flags):
        print(f"{job.at} is already signed", flush=True)
        return
    base = [
        "cosign",
        "sign",
        "--yes",
        "--tlog-upload=false",
        *LEGACY_FORMAT,
        "--key",
        "env://COSIGN_PRIVATE_KEY",
    ]
    job.must([*base, *flags, job.at], key=True)
    if not verifies(job, *flags):
        cannot(f"{job.at} does not verify after signing")


def attest(job: Job) -> None:
    """The SBOM and the SLSA v1 provenance, each only when the digest lacks it."""
    step("attest")
    if verifies(job, "--type", "spdxjson", kind="verify-attestation"):
        print("an SBOM is already attested", flush=True)
    else:
        sbom = job.scratch / "sbom.spdx.json"
        env = {**job.child(), "SYFT_CHECK_FOR_APP_UPDATE": "false"}
        must(job.tools, ["syft", "scan", f"registry:{job.at}", "-o", f"spdx-json={sbom}"], env=env)
        attest_one(job, "spdxjson", sbom)
    if verifies(job, "--type", "slsaprovenance1", kind="verify-attestation"):
        print("provenance is already attested", flush=True)
        return
    predicate = provenance(job.labels, job.repo) or local_provenance(job)
    if predicate is None:
        print("the image records no run of this repo's ci.yml: no provenance attested", flush=True)
        return
    path = job.scratch / "provenance.json"
    path.write_text(json.dumps(predicate))
    attest_one(job, "slsaprovenance1", path)


def attest_one(job: Job, kind: str, predicate: Path) -> None:
    argv = ["cosign", "attest", "--yes", "--tlog-upload=false", *LEGACY_FORMAT, "--type", kind]
    job.must(
        [*argv, "--predicate", str(predicate), "--key", "env://COSIGN_PRIVATE_KEY", job.at],
        key=True,
    )
    if not verifies(job, "--type", kind, kind="verify-attestation"):
        cannot(f"the {kind} attestation does not verify on {job.at}")


def provenance(labels: Mapping[str, str], repo: str) -> dict | None:
    """The SLSA v1 predicate for the run the image's labels record, None without one.

    Read off the image, never this run: promote is usually a later run than the build.
    """
    run_url, builder, workflow, commit = (
        labels.get(k, "")
        for k in (
            "com.crunchtools.build.run",
            "com.crunchtools.build.builder",
            "com.crunchtools.build.workflow",
            "com.crunchtools.build.commit",
        )
    )
    trusted = f"{repo}/{WORKFLOW}"
    own_run = re.compile(rf"https://github\.com/{re.escape(repo)}/actions/runs/\d+/attempts/\d+")
    honest = (
        own_run.fullmatch(run_url)
        and builder.startswith(trusted)
        and RELEASED_REF.fullmatch(builder[len(trusted) :])
        and workflow == builder
        and COMMIT.fullmatch(commit)
        and commit == labels.get("org.opencontainers.image.revision")
    )
    if not honest:
        return None
    ref = workflow.removeprefix(trusted)
    source = f"https://github.com/{repo}"
    params = {"workflow": {"ref": ref, "repository": source, "path": WORKFLOW.removesuffix("@")}}
    return predicate(BUILD_TYPE, params, commit, f"https://github.com/{builder}", run_url)


def local_provenance(job: Job) -> dict | None:
    """The predicate for a build scripts/local-release.sh ran, None for any other.

    Only that script sets SDLC_LOCAL_BUILDER, and it runs image and promote in one
    invocation with one GITHUB_RUN_ATTEMPT unique to it and no GITHUB_RUN_ID, which
    image wrote into the run label. So a label naming no run and this attempt is this
    invocation's build, and here this run is the build run.
    """
    env, labels = job.env, job.labels
    builder, attempt, sha = (
        env.get(k, "") for k in ("SDLC_LOCAL_BUILDER", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA")
    )
    ref = env.get("GITHUB_REF", "")
    run_url = f"https://github.com/{job.repo}/actions/runs//attempts/{attempt}"
    pinned = LOCAL_BUILDER.fullmatch(builder)
    honest = (
        pinned
        and not env.get("GITHUB_RUN_ID")
        and attempt.isdigit()
        and labels.get("com.crunchtools.build.run") == run_url
        and not labels.get("com.crunchtools.build.builder")
        and not labels.get("com.crunchtools.build.workflow")
        and RELEASED_REF.fullmatch(ref)
        and COMMIT.fullmatch(sha)
        and all(labels.get(k) == sha for k in BUILT_LABELS)
    )
    if not honest or pinned is None:
        return None
    # The script is sdlc's, at the commit it ran from; the source is this repo at `ref`.
    params = {
        "source": {"repository": f"https://github.com/{job.repo}", "ref": ref},
        "script": {"repository": SDLC, "path": LOCAL_SCRIPT, "commit": pinned.group(1)},
    }
    return predicate(LOCAL_BUILD_TYPE, params, sha, builder, f"{builder}#attempt-{attempt}")


def predicate(build_type: str, parameters: dict, commit: str, builder: str, run: str) -> dict:
    """SLSA v1 for either recorded build: `build_type` with its `parameters` (ci.yml's
    workflow, or local-release.sh at its sdlc commit) built `commit` of the repository
    the first parameter block names, run as `run` by `builder`."""
    source = next(iter(parameters.values()))["repository"]
    return {
        "buildDefinition": {
            "buildType": build_type,
            "externalParameters": parameters,
            "resolvedDependencies": [{"uri": f"git+{source}", "digest": {"gitCommit": commit}}],
        },
        "runDetails": {"builder": {"id": builder}, "metadata": {"invocationId": run}},
    }


# ------------------------------------------------------------------ tags


def read_tag(job: Job, tag: str, *flags: str) -> str | None:
    """`skopeo inspect` of a tag; None when it does not exist, 2 when it cannot be read."""
    proc = job.run(["skopeo", "inspect", *flags, f"docker://{job.ref}:{tag}"])
    if proc.returncode == 0:
        return proc.stdout
    if MISSING.search(proc.stderr or ""):
        return None
    cannot(f"cannot read {job.ref}:{tag} to guard it: {(proc.stderr or '').strip()}")
    return None


def digest_of(job: Job, tag: str) -> str | None:
    """What `tag` resolves to; None when it does not exist, 2 when it cannot be read."""
    out = read_tag(job, tag, "--format", "{{.Digest}}")
    return None if out is None else out.strip()


def raw_digest_of(job: Job, tag: str) -> str | None:
    """As digest_of, for an artifact: the sha256 of its manifest's bytes, because its
    config is the OCI empty descriptor and `--format {{.Digest}}` wants an image config."""
    out = read_tag(job, tag, "--raw")
    return None if out is None else sha256_of(out)


def sha256_of(raw: str) -> str:
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def copy_to(job: Job, source: str, tag: str) -> None:
    argv = ["skopeo", "copy", "--all", "--preserve-digests", f"docker://{source}"]
    job.must([*argv, f"docker://{job.ref}:{tag}"])
    if digest_of(job, tag) != source.rpartition("@")[2]:
        cannot(f"{job.ref}:{tag} does not resolve to the promoted digest after the copy")
    print(f"  {job.ref}:{tag} -> {source.rpartition('@')[2]}", flush=True)


def latest_is_newer(job: Job, new: tuple[int, int, int] | None) -> bool:
    """Does `latest` already carry a newer version than the one being promoted?"""
    if new is None:
        return False
    raw = job.must(["skopeo", "inspect", f"docker://{job.ref}:latest"])
    current = label_version(parse_json(raw, "skopeo inspect of latest").get("Labels") or {})
    if current is None or current <= new:
        return False
    # As in refuse_mislabelled: a label that names no release of this repo is inherited
    # from the base (Hummingbird's python sets 3.14.x) and says nothing about this one.
    name = "{}.{}.{}".format(*current)
    return get(job, f"/repos/{job.repo}/git/ref/tags/v{name}").status == OK


def get(job: Job, path: str) -> Response:
    """One authenticated GET; a 404 is an answer, anything but 200 or 404 is a 2."""
    resp = call(job.tools, job.env, "GET", path)
    if resp.status not in (OK, NOT_FOUND):
        cannot(f"GET {path} answered {resp.status}: {resp.body[:300]!r}")
    return resp


def ref_object(resp: Response) -> dict:
    doc = resp.json() if resp.status == OK else None
    obj = doc.get("object") if isinstance(doc, dict) else None
    return obj if isinstance(obj, dict) else {}


def tagged_here(job: Job, version: str) -> bool:
    """Is the tag vX.Y.Z on this run's commit? A rebuild of the release, not a later push."""
    obj = ref_object(get(job, f"/repos/{job.repo}/git/ref/tags/v{version}"))
    if obj.get("type") == "tag":  # annotated: one more hop to the commit
        obj = ref_object(get(job, f"/repos/{job.repo}/git/tags/{obj.get('sha')}"))
    return obj.get("sha") == job.env.get("GITHUB_SHA")


def plan_tags(job: Job, version: str | None) -> list[tuple[str, str]]:
    """(source, tag) copies this run earns, decided before anything is signed.

    A release moves its version tag only onto a free one. A rebuild of the release's own
    commit moves it onto the new digest, which is forward by construction. `latest`
    stays when it already carries a newer version.
    """
    carried = label_version(job.labels)
    release, rebuild = version, False
    if release is None and carried is not None:
        name = "{}.{}.{}".format(*carried)
        release, rebuild = (name, True) if tagged_here(job, name) else (None, False)
    moves: list[tuple[str, str]] = []
    if release is not None:
        held = digest_of(job, release)
        if held is not None and held != job.digest and not rebuild:
            cannot(f"{job.ref}:{release} is held by {held}: a version tag is never moved")
        if held != job.digest:
            moves.append((job.at, release))
    held = digest_of(job, "latest")
    if held == job.digest:
        return moves
    if held and latest_is_newer(job, tuple_of(version) if version else carried):
        print("latest stays: it carries a newer version", flush=True)
        return moves
    if held:
        moves.append((f"{job.ref}@{held}", "rollback-latest"))
    return [*moves, (job.at, "latest")]


def move_tags(job: Job, moves: list[tuple[str, str]]) -> None:
    step("tags")
    if not moves:
        print("every tag already points here", flush=True)
    for source, tag in moves:
        copy_to(job, source, tag)


# ------------------------------------------------------------------ the disk


def verify_disk(job: Job) -> dict[str, str]:
    """The disk at its digest is a manifest `image` pushed for this commit and this image."""
    sha = job.env.get("GITHUB_SHA", "")
    step(f"the disk at {job.ref}@{job.disk}")
    raw = job.must(["skopeo", "inspect", "--raw", f"docker://{job.ref}@{job.disk}"])
    if sha256_of(raw) != job.disk:
        cannot(f"the manifest at {job.ref}@{job.disk} hashes to {sha256_of(raw)}")
    notes = check_manifest(raw, job.ref, job.digest, sha)
    gated = raw_digest_of(job, gated_tag(sha))
    if gated != job.disk:
        cannot(f"{job.ref}:{gated_tag(sha)} is {gated or 'missing'}, not the disk {job.disk}")
    return notes


def plan_disk_tags(
    job: Job, moves: list[tuple[str, str]], version: str | None
) -> list[tuple[str, str]]:
    """(source, tag) copies the disk earns: `disk-<T>` for each tag T the image holds
    after this run (R3, one build under the image's version), decided before any signing.

    A disk version tag held by another disk moves only when the image's own moved in this
    run, a rebuild of the release; `disk-latest` is kept as `disk-rollback-latest` first.
    """
    moved = {tag for _, tag in moves}
    wanted = dict.fromkeys([*(t for _, t in moves), *([version] if version else []), "latest"])
    copies: list[tuple[str, str]] = []
    for tag in (t for t in wanted if t != "rollback-latest"):
        if tag not in moved and digest_of(job, tag) != job.digest:
            continue  # the image does not hold this tag (latest stays on a newer version)
        held = raw_digest_of(job, TAG_PREFIX + tag)
        if held == job.disk:
            continue
        # A rebuild moves a version tag the image already had; a first release does not.
        rebuild = tag in moved and digest_of(job, tag) is not None
        if held and SEMVER.fullmatch(tag) and not rebuild:
            cannot(f"{job.ref}:{TAG_PREFIX}{tag} is held by {held}: a version tag is never moved")
        if held and tag == "latest":
            copies.append((f"{job.ref}@{held}", f"{TAG_PREFIX}rollback-latest"))
        copies.append((f"{job.ref}@{job.disk}", TAG_PREFIX + tag))
    return copies


def promote_disk(job: Job, version: str | None, copies: list[tuple[str, str]]) -> None:
    """Sign the disk, attest the image's SBOM and a provenance of its own, move its tags."""
    disk = replace(job, digest=job.disk)
    sign(disk, version)
    attest_disk(job, disk)
    step("disk tags")
    if not copies:
        print("every disk tag already points here", flush=True)
    for source, tag in copies:
        argv = ["skopeo", "copy", "--preserve-digests", f"docker://{source}"]
        job.must([*argv, f"docker://{job.ref}:{tag}"])
        if raw_digest_of(job, tag) != source.rpartition("@")[2]:
            cannot(f"{job.ref}:{tag} does not resolve to the disk after the copy")
        print(f"  {job.ref}:{tag} -> {source.rpartition('@')[2]}", flush=True)


def attest_disk(job: Job, disk: Job) -> None:
    """The disk's SBOM is its image's, its provenance names the image and BIB; each only
    when the disk lacks it."""
    step("attest the disk")
    if verifies(disk, "--type", "spdxjson", kind="verify-attestation"):
        print("an SBOM is already attested", flush=True)
    else:
        path = disk.scratch / "disk-sbom.spdx.json"
        path.write_text(json.dumps(image_sbom(job)))
        attest_one(disk, "spdxjson", path)
    if verifies(disk, "--type", "slsaprovenance1", kind="verify-attestation"):
        print("provenance is already attested", flush=True)
        return
    base = provenance(job.labels, job.repo) or local_provenance(job)
    if base is None:
        print("the image records no run of this repo's ci.yml: no disk provenance", flush=True)
        return
    path = disk.scratch / "disk-provenance.json"
    path.write_text(json.dumps(disk_provenance(base, job)))
    attest_one(disk, "slsaprovenance1", path)


def image_sbom(job: Job) -> dict:
    """The SPDX predicate the image's own SBOM attestation verifies with; no SBOM is a 2."""
    pub = job.scratch / "fleet.pub"
    pub.write_text(FLEET_PUBLIC_KEY)
    argv = ["cosign", "verify-attestation", "--insecure-ignore-tlog", "--key", str(pub)]
    proc = job.run([*argv, "--type", "spdxjson", job.at])
    for line in proc.stdout.splitlines() if proc.returncode == 0 else []:
        try:
            statement = json.loads(base64.b64decode(json.loads(line)["payload"]))
            predicate_ = statement["predicate"]
        except UNDECODABLE:
            continue
        if isinstance(predicate_, dict):
            return predicate_
    cannot(f"{job.at} has no verified SBOM to give its disk")
    return {}


def disk_provenance(base: dict, job: Job) -> dict:
    """The image's predicate as a disk's: the disk it asked for, the image and BIB it ran."""
    builder = job.notes[BUILDER]
    bib = builder.partition("@")[0]
    build = base["buildDefinition"]
    params = {
        **build["externalParameters"],
        "disk": {
            "type": job.notes[DISK_TYPE],
            "rootfs": job.notes[DISK_ROOTFS],
            "builder": builder,
        },
    }
    inputs = [
        {"uri": f"oci://{job.ref}", "digest": {"sha256": job.digest.removeprefix("sha256:")}},
        {"uri": f"oci://{bib}", "digest": {"sha256": builder.rpartition("sha256:")[2]}},
    ]
    return {
        "buildDefinition": {
            "buildType": DISK_BUILD_TYPE,
            "externalParameters": params,
            "resolvedDependencies": [*build["resolvedDependencies"], *inputs],
        },
        "runDetails": base["runDetails"],
    }
