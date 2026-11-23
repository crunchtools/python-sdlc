"""The `promote` job: sign, attest and move tags on a digest it did not build.

It holds the cosign key and the quay robot, and nothing that builds: it never runs
the image and never calls the container socket. `image` built and gated the digest;
this job trusts that output for one thing, WHICH digest, and re-validates the rest: a
well-formed digest, the ref that is exactly `quay.io/crunchtools/<this repo>`, the sha
that is this run's, and an image that exists at that digest.

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
the key in its environment; only `cosign sign` and `cosign attest` see it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sdlc_jobs._core import (
    CLEAN,
    DIGEST,
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
from sdlc_jobs.image import ORG, QUAY

VERSION_LABELS = ("org.opencontainers.image.version", "version")
MISSING = re.compile(r"manifest unknown|not found|no such|name unknown", re.IGNORECASE)
COMMIT = re.compile(r"[0-9a-f]{40}")
RELEASED_REF = re.compile(r"refs/heads/.+")
BUILD_TYPE = "https://slsa-framework.github.io/github-actions-buildtypes/workflow/v1"
WORKFLOW = ".github/workflows/ci.yml@"
OK = 200
NOT_FOUND = 404
HANDLED_EVENTS = ("push", "schedule", "workflow_dispatch")  # image's `if:` less pull_request
BUILT_LABELS = ("org.opencontainers.image.revision", "com.crunchtools.build.commit")
SECRET_KEYS = ("COSIGN_PRIVATE_KEY", "COSIGN_PASSWORD")


@dataclass
class Job:
    tools: Tools
    env: Mapping[str, str]
    repo: str
    ref: str
    digest: str
    scratch: Path
    labels: dict[str, str] = field(default_factory=dict)

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


def validate_image(env: Mapping[str, str], repo: str) -> tuple[str, str]:
    """(ref, digest) of what `image` built, after every check it does not get to skip."""
    doc = parse_json(env.get("SDLC_IMAGE_RESULT"), "SDLC_IMAGE_RESULT")
    ref = f"{QUAY}/{ORG}/{repo.rpartition('/')[2]}"
    sha = env.get("GITHUB_SHA", "")
    if not (isinstance(doc.get("digest"), str) and DIGEST.fullmatch(doc["digest"])):
        cannot("image's digest is not a well-formed sha256 digest")
    if doc.get("ref") != ref:
        cannot(f"image's ref {doc.get('ref')!r} is not exactly {ref}")
    if not COMMIT.fullmatch(sha) or doc.get("sha") != sha:
        cannot(f"image built {doc.get('sha')!r}, not this run's commit {sha!r}")
    return ref, doc["digest"]


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
    output), SDLC_CHECK, SDLC_WRITE and SDLC_RELEASE, and the signing key.
    `tools` runs skopeo and cosign (a fake in the suites). Raises
    CannotRunError, exit 2, when an input is malformed or would move a version
    backwards, or a signing or tagging step fails.
    """
    del event  # the job contract passes it; promote reads only the environment
    if event_name not in HANDLED_EVENTS:
        cannot(f"promote does not run on {event_name!r}: only {', '.join(HANDLED_EVENTS)}")
    repo = env.get("GITHUB_REPOSITORY", "")
    if repo.count("/") != 1:
        cannot("GITHUB_REPOSITORY is not owner/name")
    if not env.get("GITHUB_TOKEN"):
        cannot("GITHUB_TOKEN is required: a tag read without it fails open on a private repo")
    ref, digest = validate_image(env, repo)
    version = release_version(env, plan(env).get("release"))
    scratch = Path(tempfile.mkdtemp(prefix="sdlc-promote-", dir=env.get("RUNNER_TEMP") or None))
    scratch.chmod(0o700)
    (scratch / "docker").mkdir()
    job = Job(tools, env, repo, ref, digest, scratch)
    try:
        login(job)
        job.labels = inspect_labels(job)
        verify_built(job)
        if version:
            refuse_mislabelled(job, version)
        moves = plan_tags(job, version)
        sign(job, version)
        attest(job)
        move_tags(job, moves)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return CLEAN


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

    Both are refusals, never skips: a digest that is not what this commit built does not
    get signed or attested.
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
    base = ["cosign", "sign", "--yes", "--tlog-upload=false", "--key", "env://COSIGN_PRIVATE_KEY"]
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
    predicate = provenance(job.labels, job.repo)
    if predicate is None:
        print("the image records no run of this repo's ci.yml: no provenance attested", flush=True)
        return
    path = job.scratch / "provenance.json"
    path.write_text(json.dumps(predicate))
    attest_one(job, "slsaprovenance1", path)


def attest_one(job: Job, kind: str, predicate: Path) -> None:
    argv = ["cosign", "attest", "--yes", "--tlog-upload=false", "--type", kind]
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


# ------------------------------------------------------------------ tags


def digest_of(job: Job, tag: str) -> str | None:
    """What `tag` resolves to; None when it does not exist, 2 when it cannot be read."""
    proc = job.run(["skopeo", "inspect", "--format", "{{.Digest}}", f"docker://{job.ref}:{tag}"])
    if proc.returncode == 0:
        return proc.stdout.strip()
    if MISSING.search(proc.stderr or ""):
        return None
    cannot(f"cannot read {job.ref}:{tag} to guard it: {(proc.stderr or '').strip()}")
    return None


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
    return current is not None and current > new


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
