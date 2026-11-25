"""The `image` job: verify the parent, build FROM its digest, gate, push `gated-<sha>`.

It holds the registry secrets (quay, RHSM, registry.redhat.io) and the runner's
container socket, never the cosign key. It builds once per release: on the release
merge `check` plans, on the weekly or parent rebuild of the commit a release tagged, or
a dispatch. A pull request builds nothing here (`check` runs the container gates when
the Containerfile or a lockfile changed). Any other event is a 2 here, the second guard
behind the workflow's `if:`.

`check`'s plan only says this job runs. Every registry FROM, a builder stage's as much
as the final one's, is resolved and verified here:
`quay.io/crunchtools/*` with the fleet key and `-a repo=<parent repo>`, Red Hat's
registries with Red Hat's release key (gha-runner's `policy.json`, run through
`skopeo --policy`), and any other registry is refused. `quay.io/hummingbird/<name>`
is Hummingbird's unsigned mirror of `registry.access.redhat.com/hi/<name>`: the same
digest is verified there with Red Hat's key, and the build uses the mirror's bytes by
that digest. The build then runs FROM that
verified `@sha256:` digest whatever tag the Containerfile names: `--from` when the stage
is the only registry stage and the first, and a rewritten copy of the Containerfile
otherwise, because podman's `--from` only replaces the FIRST stage's FROM.

The gates are the ones `sdlc ci` plans for this repo. `sdlc plan ci` is asked for the
build argv (so `[tool.sdlc.ci.container-build]` settings still hold) and for smoke, scan
and bootc-lint, and they are run here through the socket as the launcher runs them.
Then the boot gate, `[tool.sdlc.image] boot`, as container-release ran it
(sdlc_jobs/_boot.py).
Container-release pushed before it gated; this job gates the local image and pushes only
what passed, so no ungated digest exists in the registry, except for a repo whose
boot-command, contract-test or free-disk the `boot` job judges after the push.

A repo publishes its root image unless `[tool.sdlc.image] publish = false`, and each
`[[tool.sdlc.images]]` entry EXTRA_IMAGES allows it (sdlc_jobs/_core.py). They are built
in dependency order; a FROM naming a sibling this run pushed builds on that digest.

`result` is `{"ref", "digest", "sha", "tag", "parent"}` for a repo's one own image, and
`{"images": [{..., "name"}], "sha"}` otherwise: all `promote` takes from here, and it
re-validates every field.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sdlc_jobs import _prs
from sdlc_jobs._boot import Boot, boot_gate, boot_settings
from sdlc_jobs._core import (
    BOOT_JOB,
    CLEAN,
    DIGEST,
    FLEET_PUBLIC_KEY,
    FOUND,
    FROM_AS_WORDS,
    PODMAN_CANNOT_CREATE,
    Image,
    RunOptions,
    Tools,
    build_arg_map,
    build_args,
    cannot,
    expand_build_vars,
    fetch_tree,
    final_stage,
    host_mount,
    image_owner,
    images,
    must,
    podman,
    set_output,
    show,
    step,
    workspace,
)
from sdlc_jobs._tools import safe_directory

QUAY = "quay.io"
ORG = "crunchtools"
RED_HAT = ("registry.redhat.io", "registry.access.redhat.com")
HUMMINGBIRD = "hummingbird"  # quay.io/hummingbird/<name> mirrors HUMMINGBIRD_SIGNED/<name>
HUMMINGBIRD_SIGNED = "registry.access.redhat.com/hi"
RH_KEY = "/etc/pki/sigstore/SIGSTORE-redhat-release3"
RH_REKOR = "/etc/pki/sigstore/REKOR-signing-key"
US = "\x1f"  # the plan's argv separator
SHA = re.compile(r"[0-9a-f]{40}")
GATES = ("container-smoke", "container-scan", "bootc-lint")
IMAGE_CHECKS = ("container-build", "container-smoke", "container-scan")
BOOTC_CHECKS = ("container-build", "container-scan", "bootc-lint")  # the bootc stack's
SECRETS = (
    "QUAY_USERNAME",
    "QUAY_PASSWORD",
    "RHSM_ACTIVATION_KEY",
    "RHSM_ORG_ID",
    "REGISTRY_REDHAT_IO_USER",
    "REGISTRY_REDHAT_IO_PASSWORD",
    "GITHUB_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_URL",
)
FROM_LINE = re.compile(r"^(\s*FROM\s+(?:--\S+\s+)*)(\S+)", re.IGNORECASE)
# The `podman run` options the planner emits for a host step (container-smoke,
# container-scan, bootc-lint), and nothing else: a repo's config cannot add one.
RUN_FLAGS = ("--rm", "--read-only")
RUN_VALUED = (
    "--pull",
    "--network",
    "-e",
    "-v",
    "--volume",
    "--mount",
    "-w",
    "--entrypoint",
    "--tmpfs",
)
EVENTS = ("push", "schedule", "workflow_dispatch")  # the workflow's `if:`
PLAN_FIELDS = 4  # name, stage, mode, argv
STATE_FIELDS = 3  # running, exit code, OOM-killed
HADOLINT_UNPARSED = 4  # containerfile-lint: hadolint could not parse the file
# bin/sdlc's rule, crunchtools/sdlc#181: on an AppArmor engine no-new-privileges
# stacks the container's profile onto crun's (`containers-default//&crun`), and a
# process cannot signal its own child (EPERM). There every gate container keeps
# --cap-drop=all and AppArmor and goes without it; anywhere else it is passed.
NNP = "--security-opt=no-new-privileges"
SYSTEMD = ("/sbin/init", "/usr/sbin/init", "/lib/systemd/systemd", "/usr/lib/systemd/systemd")


@dataclass
class Job:
    tools: Tools
    env: Mapping[str, str]
    root: Path
    scratch: Path
    name: str
    sha: str
    podman: list[str]
    auth: Path
    volume: str
    host_root: str = ""
    image_id: str = ""
    child_env: dict[str, str] = field(default_factory=dict)
    settings: dict = field(default_factory=dict)  # the image being built's settings, checked
    boot: Boot | None = None  # its boot gate, checked; None is none asked for
    images: list[Image] = field(default_factory=list)  # every image, in build order
    needs: dict[str, set[str]] = field(default_factory=dict)  # name -> siblings it is FROM
    built: dict[str, Parent] = field(default_factory=dict)  # name -> pushed this run
    results: list[dict] = field(default_factory=list)  # what promote takes, per image
    bootc: bool = False  # the plan has bootc-lint: build OCI
    tree: str = ""  # the git tree of `sha`, stamped as a label
    event: str = ""
    nnp: list[str] = field(default_factory=lambda: [NNP])  # [] on an AppArmor engine

    def run(
        self,
        argv: list[str],
        *,
        input: str | None = None,
        stream: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        kw: RunOptions = {"input": input, "env": self.child_env, "cwd": str(self.root)}
        kw["stream"] = stream
        if timeout is not None:  # only the boot gate's calls bound it
            kw["timeout"] = timeout
        return self.tools.run(argv, **kw)

    def must(self, argv: list[str], *, input: str | None = None) -> str:
        return must(self.tools, argv, input=input, env=self.child_env, cwd=str(self.root))


def run(event_name: str, event: dict, env: Mapping[str, str], tools: Tools) -> int:
    """Build, gate and push this commit's image; 0 pushed, 1 a gate found something.

    `event_name` must be one of `EVENTS`: this job holds the registry secrets, so any
    other (`pull_request` above all) is a 2.
    `event` is unused, because everything comes from `env`: GITHUB_REPOSITORY,
    GITHUB_SHA, GITHUB_WORKSPACE, HOSTNAME (this container, for the host path),
    QUAY_*, RHSM_* and REGISTRY_REDHAT_IO_*. `tools` runs podman, skopeo and cosign
    (a fake in the suites). Raises CannotRunError, which is exit 2, when an input is
    malformed, the parent does not verify, or a step that is not a gate fails.
    """
    del event  # the job contract passes it; image reads only the environment
    if event_name not in EVENTS:
        cannot(f"image does not run on {event_name!r}, only on {', '.join(EVENTS)}")
    repo = env.get("GITHUB_REPOSITORY", "")
    sha = env.get("GITHUB_SHA", "")
    if repo.count("/") != 1 or not SHA.fullmatch(sha):
        cannot("GITHUB_REPOSITORY and GITHUB_SHA are required, and the SHA is 40 hex digits")
    root = workspace(env)
    fetch_tree(tools, env, root, repo)
    name = repo.rpartition("/")[2]
    scratch = Path(tempfile.mkdtemp(prefix="sdlc-image-", dir=env.get("RUNNER_TEMP") or None))
    scratch.chmod(0o700)
    job = Job(
        tools,
        env,
        root,
        scratch,
        name,
        sha,
        podman(env),
        scratch / "auth.json",
        f"sdlc-cache-{env.get('GITHUB_RUN_ID', '0')}-{env.get('GITHUB_RUN_ATTEMPT', '0')}",
    )
    job.images = images(root, repo)
    if not job.images:
        cannot("this repo publishes no image: no root Containerfile, or publish = false")
    for img in job.images:  # every key is checked before anything logs in
        boot_settings(img.settings)
        build_args(img.settings)
        for key in sorted(BOOT_JOB & set(img.settings)):
            where = (
                "runs in the boot job, against the pushed digest"
                if boot_job_declared(root)
                else "is not enforced: this repo's fleet file has no boot job (#118)"
            )
            print(f"image: {img.name}: {key} {where}")
    job.images, job.needs = build_order(root, job.images)
    job.child_env = {k: v for k, v in env.items() if k not in SECRETS}
    job.child_env["REGISTRY_AUTH_FILE"] = str(job.auth)
    job.child_env.update(
        safe_directory(job.child_env, root)
    )  # git and `sdlc` in the workspace: #157
    job.event = event_name
    try:
        job.tree = job.must(["git", "rev-parse", "HEAD^{tree}"]).strip()
        return build_and_push(job)
    finally:
        job.run([*job.podman, "volume", "rm", "-f", job.volume])
        shutil.rmtree(scratch, ignore_errors=True)


def boot_job_declared(root: Path) -> bool:
    """True when this commit's fleet file has the `boot` job. Under an older one, which
    the floating image also runs, boot-command, contract-test and free-disk run nowhere."""
    try:
        text = (root / ".github" / "workflows" / "ci.yml").read_text()
    except OSError:
        return False
    return re.search(r"^  boot:", text, re.M) is not None


def build_and_push(job: Job) -> int:
    """Each image in build order: build, gate and push it.

    An image built FROM a sibling this run pushed builds on that digest. A gate that
    finds something stops the run there: nothing after it is built or pushed.
    """
    login(job)
    plan: dict[str, tuple[str, str]] | None = None
    found = False
    for img in job.images:
        job.settings, job.boot = img.settings, boot_settings(img.settings)
        if plan is None:
            step("map the workspace and plan the gates")
            job.host_root, job.image_id = host_mount(job.tools, job.env, job.root)
            job.nnp = no_new_privileges(job)
            plan = gate_plan(job)
            job.bootc = "bootc-lint" in plan
            found = lint(job)
        if build_one(job, img, plan, found) == FOUND:
            return FOUND
    hand_on(job)
    return CLEAN


def hand_on(job: Job) -> None:
    """Record what was pushed as `result` for promote.

    One image that is the repo's own keeps the shape promote has always read; more, or
    another, is `{"images": [...]}`.
    """
    if len(job.results) == 1 and job.images[0].root:
        out: dict = {k: v for k, v in job.results[0].items() if k != "name"}
    else:
        out = {"images": job.results, "sha": job.sha}
    set_output(job.env, "result", json.dumps(out, separators=(",", ":")))


def stages_of(job: Job, img: Image) -> tuple[str, Parent | None, list[Parent]]:
    """(Containerfile text, final parent, every registry stage), each pinned: a sibling
    this job pushed by its digest, anything else verified."""
    name = img.containerfile or next(
        (f for f in ("Containerfile", "Dockerfile") if (job.root / f).is_file()), None
    )
    if name is None:
        cannot("there is no Containerfile or Dockerfile at the repo root to build")
    text = (job.root / name).read_text(encoding="utf-8", errors="replace")
    overrides = build_arg_map(img.settings)
    parent = parent_stage(text, overrides)
    stages = [
        *([parent] if parent else []),
        *builder_stages(text, parent.index if parent else -1, overrides),
    ]
    for stage in stages:
        host, path = split_ref(stage.image)
        sibling = job.built.get(path) if host == QUAY else None
        if sibling is None:
            verify_parent(job, stage)
            continue
        # Built and gated by this very job, a step ago: its bytes, by digest.
        stage.pinned, stage.digest, stage.image = sibling.pinned, sibling.digest, sibling.image
        print(f"parent built here: {stage.pinned}", flush=True)
    return text, parent, stages


def build_one(job: Job, img: Image, plan: dict[str, tuple[str, str]], linted: bool) -> int:
    """Build, gate and push one image; FOUND when a gate (or the lint) found something."""
    text, parent, stages = stages_of(job, img)
    if img.containerfile is None or img.context is None:  # the root image: the plan's build
        if "container-build" not in plan:
            cannot("`sdlc plan ci` plans no container-build for this repo")
        words, gates = plan["container-build"][1].split(US)[2:], plan
    else:
        tag = f"localhost/{img.name}:sdlc"
        words = ["-f", img.containerfile, "-t", tag, img.context]
        gates = extra_gates(job, plan, tag)
    step(f"image {img.ref}")
    tag = build(job, ["podman", "build", *words], text, parent, stages)
    found = linted
    for name in GATES:
        if name in gates and not run_gate(job, name, *gates[name]):
            found = True
    if job.boot is not None and not boot_gate(job, job.boot, tag):
        found = True
    if found:
        print(f"\nimage: a gate found something; {img.name} and what follows are not pushed")
        return FOUND
    return push(job, img, tag, parent)


def extra_gates(job: Job, plan: dict[str, tuple[str, str]], tag: str) -> dict[str, tuple[str, str]]:
    """container-scan (and bootc-lint) as planned for this repo, judging `tag` instead.

    Only the image word changes, and check_words still holds the line. Smoke is the
    root image's (its env and args are that image's): an extra image proves it starts
    with its own boot gate.
    """
    if "container-scan" not in plan:
        for line in job.must(["sdlc", "plan", "container-scan"]).splitlines():
            parts = line.split("\t", PLAN_FIELDS - 1)
            if len(parts) == PLAN_FIELDS and parts[0] == "container-scan":
                plan = {**plan, "container-scan": (parts[2], parts[3])}
    if "container-scan" not in plan:
        cannot("`sdlc plan container-scan` planned no scan")
    out: dict[str, tuple[str, str]] = {}
    for name in ("container-scan", "bootc-lint"):
        if name not in plan:
            continue
        mode, payload = plan[name]
        words = payload.split(US)
        if "--" not in words:
            cannot(f"{name}: the plan has no `--` before its image")
        if name == "bootc-lint":
            words[words.index("--") + 1] = tag
        else:
            words = [
                re.sub(r"(?<=^type=image,source=)[^,]*", tag, w)
                if w.startswith("type=image,")
                else w
                for w in words
            ]
        out[name] = (mode, US.join(words))
    print("container-smoke: the root image's; this image is judged by its boot gate", flush=True)
    return out


def build_order(root: Path, imgs: list[Image]) -> tuple[list[Image], dict[str, set[str]]]:
    """`imgs` with every image after the siblings it is built FROM, and each image's
    siblings by name; a cycle is a 2."""
    names = {i.name for i in imgs}
    needs: dict[str, set[str]] = {}
    for img in imgs:
        path = img.containerfile or next(
            (f for f in ("Containerfile", "Dockerfile") if (root / f).is_file()), ""
        )
        text = (root / path).read_text(encoding="utf-8", errors="replace") if path else ""
        overrides = build_arg_map(img.settings)
        parent = parent_stage(text, overrides) if text else None
        stages = [*([parent] if parent else [])]
        if text:
            stages += builder_stages(text, parent.index if parent else -1, overrides)
        needs[img.name] = {
            split_ref(st.image)[1].partition("/")[2]
            for st in stages
            if split_ref(st.image)[0] == QUAY and split_ref(st.image)[1].partition("/")[0] == ORG
        } & (names - {img.name})
    out: list[Image] = []
    while len(out) < len(imgs):
        ready = [i for i in imgs if i not in out and needs[i.name] <= {o.name for o in out}]
        if not ready:
            cannot(f"the images are built FROM each other in a cycle: {sorted(names)}")
        out.append(ready[0])
    return out, needs


def login(job: Job) -> None:
    """One auth file for skopeo and the remote podman, in this job's temp directory."""
    for host, user, password in (
        (QUAY, "QUAY_USERNAME", "QUAY_PASSWORD"),
        ("registry.redhat.io", "REGISTRY_REDHAT_IO_USER", "REGISTRY_REDHAT_IO_PASSWORD"),
    ):
        if not job.env.get(user):
            if host == QUAY:
                cannot("QUAY_USERNAME is not set: nothing could be pushed")
            if any(i.settings.get("redhat-registry") for i in job.images):
                cannot(f"[tool.sdlc.image] redhat-registry is true but {user} is not set")
            continue
        job.must(
            [
                "skopeo",
                "login",
                "--authfile",
                str(job.auth),
                "-u",
                job.env[user],
                "--password-stdin",
                host,
            ],
            input=job.env.get(password, ""),
        )


# ------------------------------------------------------------------ the parent


@dataclass
class Parent:
    """The registry image a Containerfile builds on: which FROM, and what was verified."""

    index: int
    image: str
    pinned: str = ""
    digest: str = ""


def parent_stage(text: str, overrides: Mapping[str, str] | None = None) -> Parent | None:
    """The FROM that names a registry image and that image; None for scratch.

    An unset build variable is a 2: nothing to verify. `overrides` are build-args.
    """
    found = final_stage(text, strict=True, overrides=overrides)
    if found is None:
        cannot("the Containerfile has no FROM to verify")
    index, image = found
    return None if image.lower() == "scratch" else Parent(index, image)


def builder_stages(
    text: str, final: int, overrides: Mapping[str, str] | None = None
) -> list[Parent]:
    """Every other FROM that names a registry image: a builder stage ships bytes too.

    Not scratch, not a reference to an earlier stage's alias, not the `final` FROM
    that `parent_stage` already has; the same variable expansion as `final_stage`.
    """
    args: dict[str, str] = {}
    aliases: list[str] = []
    found: list[Parent] = []
    index = -1
    for raw in re.sub(r"\\\n", " ", text).splitlines():
        words = raw.split()
        op = words[0].upper() if words else ""
        if op == "ARG" and index < 0:
            args.update(
                {k: v.strip("\"'") for k, eq, v in (w.partition("=") for w in words[1:]) if eq}
            )
        elif op == "FROM" and (rest := [w for w in words[1:] if not w.startswith("--")]):
            if index < 0:
                args.update(overrides or {})
            index += 1
            image = expand_build_vars(rest[0], args, strict=True).strip("\"'")
            if index != final and image.lower() != "scratch" and image.lower() not in aliases:
                found.append(Parent(index, image))
            if len(rest) >= FROM_AS_WORDS and rest[1].upper() == "AS":
                aliases.append(rest[2].lower())
    return found


def split_ref(image: str) -> tuple[str, str]:
    """(registry host, repository path) of a reference, tag and digest dropped."""
    first, _, rest = image.partition("/")
    host, path = (
        (first, rest)
        if rest and ("." in first or ":" in first or first == "localhost")
        else ("docker.io", image)
    )
    path = path.split("@")[0]
    head, _, last = path.rpartition("/")
    return host, (head + "/" if head else "") + last.split(":")[0]


def verify_parent(job: Job, parent: Parent) -> None:
    """Resolve the parent to a digest and verify it, or a 2; sets `pinned` and `digest`."""
    image = parent.image
    step(f"parent: {image}")
    host, path = split_ref(image)
    name = f"{host}/{path}"
    # skopeo refuses `name:tag@sha256:...`; the digest alone names the image, the tag
    # is documentation.
    ref = f"{name}@{image.partition('@')[2]}" if "@" in image else image
    digest = job.must(["skopeo", "inspect", "--format", "{{.Digest}}", f"docker://{ref}"]).strip()
    if not DIGEST.fullmatch(digest):
        cannot(f"{image} resolved to {digest!r}, not a sha256 digest")
    pinned = f"{name}@{digest}"
    if host == QUAY and path.startswith(f"{ORG}/") and path.count("/") == 1:
        key = job.scratch / "fleet.pub"
        key.write_text(FLEET_PUBLIC_KEY)
        argv = ["cosign", "verify", "--insecure-ignore-tlog", "--key", str(key)]
        # Signed by the repo that publishes it: its own, or the one EXTRA_IMAGES names.
        job.must([*argv, "-a", f"repo={image_owner(path)}", pinned])
    elif host in RED_HAT:
        red_hat_verify(job, parent, pinned)
    elif host == QUAY and path.startswith(f"{HUMMINGBIRD}/") and path.count("/") == 1:
        # The mirror carries no signature; Red Hat signs the same digest under hi/.
        # A digest names the bytes, so verifying it there verifies the mirror's copy.
        signed = f"{HUMMINGBIRD_SIGNED}/{path.partition('/')[2]}"
        if "@" not in image:
            # A tag is resolved at hi/, not the unsigned mirror: a stale or tampered
            # mirror could otherwise roll the build back to any older signed digest.
            tag = image.rpartition("/")[2].partition(":")[2] or "latest"
            at_hi = job.must(
                ["skopeo", "inspect", "--format", "{{.Digest}}", f"docker://{signed}:{tag}"]
            ).strip()
            if at_hi != digest:
                cannot(f"{image} is {digest} on the mirror but {at_hi!r} at {signed}:{tag}")
        red_hat_verify(job, parent, f"{signed}@{digest}")
    else:
        cannot(f"the parent {image} is on {host}, which this job does not trust: refused")
    parent.pinned, parent.digest = pinned, digest
    print(f"parent verified: {pinned}", flush=True)


def red_hat_verify(job: Job, parent: Parent, signed: str) -> None:
    """Verify `signed` (a Red Hat registry @digest) with Red Hat's release key, or a 2."""
    policy = job.scratch / "policy.json"
    entry = [
        {
            "type": "sigstoreSigned",
            "keyPath": job.env.get("SDLC_RH_KEY", RH_KEY),
            "rekorPublicKeyPath": job.env.get("SDLC_RH_REKOR_KEY", RH_REKOR),
        }
    ]
    policy.write_text(
        json.dumps(
            {
                "default": [{"type": "reject"}],
                "transports": {"docker": dict.fromkeys(RED_HAT, entry)},
            }
        )
    )
    job.must(
        [
            "skopeo",
            "--policy",
            str(policy),
            "copy",
            "-q",
            f"docker://{signed}",
            f"dir:{job.scratch / f'parent-{parent.index}'}",
        ]
    )


def labels(job: Job, parent: Parent | None) -> dict[str, str]:
    env = job.env
    repo = env["GITHUB_REPOSITORY"]
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    run_url = f"{server}/{repo}/actions/runs/{env.get('GITHUB_RUN_ID', '')}"
    run_url += f"/attempts/{env.get('GITHUB_RUN_ATTEMPT', '')}"
    workflow = env.get("GITHUB_WORKFLOW_REF", "")
    return {
        "org.opencontainers.image.revision": job.sha,
        "org.opencontainers.image.source": f"{server}/{repo}",
        "org.opencontainers.image.base.name": parent.image if parent else "",
        "org.opencontainers.image.base.digest": parent.digest if parent else "",
        "com.crunchtools.build.run": run_url,
        "com.crunchtools.build.builder": workflow,
        "com.crunchtools.build.workflow": workflow,
        "com.crunchtools.build.commit": job.sha,
        _prs.TREE_LABEL: job.tree,
    }


# ------------------------------------------------------------------ build, gates


def gate_plan(job: Job) -> dict[str, tuple[str, str]]:
    """check -> (mode, payload) from `sdlc plan ci`, the same plan the launcher runs."""
    job.must([*job.podman, "volume", "create", job.volume])
    job.child_env.update(
        SDLC_REPO=str(job.root),
        SDLC_PROJECT=job.name,
        SDLC_HOST_REPO=job.host_root,
        SDLC_CACHE_VOLUME=job.volume,
    )
    plan: dict[str, tuple[str, str]] = {}
    for line in job.must(["sdlc", "plan", "ci"]).splitlines():
        parts = line.split("\t", PLAN_FIELDS - 1)
        if len(parts) == PLAN_FIELDS:
            plan.setdefault(parts[0], (parts[2], parts[3]))
    # ci leaves the image's own checks out where the check job cannot run them (an
    # RHSM build, which needs secrets only this job holds) or where a python repo
    # names no container stack. The image is still built and gated here: planned by
    # name, a check ignores ci's skip and stacks.
    checks = BOOTC_CHECKS if root_is_bootc(job) else IMAGE_CHECKS
    for check in checks:
        if check in plan:
            continue
        for line in job.must(["sdlc", "plan", check]).splitlines():
            parts = line.split("\t", PLAN_FIELDS - 1)
            if len(parts) == PLAN_FIELDS:
                plan.setdefault(parts[0], (parts[2], parts[3]))
    return plan


def root_is_bootc(job: Job) -> bool:
    """The planner's rule for the bootc stack: the root image's final base names bootc."""
    name = next((f for f in ("Containerfile", "Dockerfile") if (job.root / f).is_file()), None)
    if name is None:
        return False
    text = (job.root / name).read_text(encoding="utf-8", errors="replace")
    found = final_stage(text, strict=False, overrides=build_arg_map(job.settings))
    return bool(found and found[1] and "bootc" in found[1].lower())


def lint(job: Job) -> bool:
    """containerfile-lint runs here: this container is the sdlc image. True is a finding."""
    step("containerfile-lint")
    proc = job.run(["sdlc", "containerfile-lint"])
    show(proc)
    if proc.returncode == 0:
        return False
    if proc.returncode == HADOLINT_UNPARSED:
        cannot("containerfile-lint could not parse the Containerfile; nothing was judged")
    if proc.returncode != 1:
        cannot(f"containerfile-lint could not run (exit {proc.returncode})")
    return True


def pinned_file(job: Job) -> Path:
    return job.root / f".sdlc-pinned-{job.sha[:12]}.Containerfile"


def pin_args(job: Job, text: str, stages: list[Parent], argv: list[str]) -> list[str]:
    """podman build words that put each verified digest in its stage's FROM."""
    if not stages:
        return argv
    if len(stages) == 1 and stages[0].index == 0:
        return ["--from", stages[0].pinned, *argv]
    pinned = {stage.index: stage.pinned for stage in stages}
    kept: list[str] = []
    seen = -1
    for line in text.splitlines(keepends=True):
        m = FROM_LINE.match(line)
        if m:
            seen += 1
        if m and seen in pinned:
            kept.append(f"{m[1]}{pinned[seen]}{line[m.end() :]}")
        else:
            kept.append(line)
    copy = pinned_file(job)
    copy.write_text("".join(kept))
    if "-f" not in argv:
        cannot("the build plan names no Containerfile with -f")
    out = list(argv)
    out[out.index("-f") + 1] = copy.name
    return out


def build(
    job: Job,
    words: list[str],
    text: str,
    parent: Parent | None,
    stages: list[Parent],
) -> str:
    """Build through the socket at the tag smoke and scan read; return that tag."""
    step("build")
    if words[:2] != ["podman", "build"] or "-t" not in words:
        cannot("the container-build plan is not a `podman build` with a tag")
    tag = words[words.index("-t") + 1]
    argv = [
        *job.podman,
        "build",
        "--pull=always",
        "--format",
        job.settings.get("format") or ("oci" if job.bootc else "docker"),
    ]
    argv += ["--authfile", str(job.auth)]
    for kv in build_args(job.settings):
        argv += ["--build-arg", kv]
    for key, value in labels(job, parent).items():
        if value:
            argv += ["--label", f"{key}={value}"]
    key, org = job.env.get("RHSM_ACTIVATION_KEY", ""), job.env.get("RHSM_ORG_ID", "")
    if job.settings.get("rhsm") and not (key and org):
        cannot("[tool.sdlc.image] rhsm is true but RHSM_ACTIVATION_KEY or RHSM_ORG_ID is not set")
    if key:
        # Files, not env=: the runner's podman predates env-sourced secrets. Both
        # spellings are in the fleet's Containerfiles; an extra secret costs nothing.
        for fname, value in (("activation_key", key), ("org_id", org)):
            path = job.scratch / fname
            path.write_text(value)
            path.chmod(0o600)
        for sid, fname in (
            ("activation_key", "activation_key"),
            ("org_id", "org_id"),
            ("RHSM_ACTIVATION_KEY", "activation_key"),
            ("RHSM_ORG_ID", "org_id"),
        ):
            argv += ["--secret", f"id={sid},src={job.scratch / fname}"]
    copy = pinned_file(job)
    try:
        argv += pin_args(job, text, stages, words[2:])
        proc = job.run(argv, stream=True)  # a build's log goes to the job log, unbuffered
    finally:
        copy.unlink(missing_ok=True)
    show(proc)
    if proc.returncode != 0:
        print("image: the build failed", flush=True)
        cannot(f"podman build exited {proc.returncode}")
    return tag


def run_gate(job: Job, name: str, mode: str, payload: str) -> bool:
    """Run one planned `podman run` gate. True when it passed, or is advisory or skipped."""
    step(name)
    argv = payload.split(US)
    seconds = int(argv.pop(0)) if mode.startswith("host-up") else 0
    if argv[:2] != ["podman", "run"]:
        cannot(f"{name}: a host step may only `podman run`; refused {argv[:2]}")
    words = [job.image_id if w == "@sdlc-image" else w for w in argv[2:]]
    check_words(job, name, words)
    safe = ["--cap-drop=all", *job.nnp, "--events-backend=none"]
    if seconds:
        ok = stays_up(job, name, safe, words, seconds)
        return ok or mode == "host-up-advisory"
    proc = job.run([*job.podman, "run", "--log-driver=none", *safe, *words])
    show(proc)
    if proc.returncode == 0:
        return True
    if proc.returncode != 1:
        cannot(f"{name}: podman run exited {proc.returncode}; nothing was judged")
    return False


def no_new_privileges(job: Job) -> list[str]:
    """[NNP], or [] when the engine reports AppArmor. Asked once, as bin/sdlc asks it;
    an engine that does not answer keeps the flag."""
    proc = job.run([*job.podman, "info", "--format", "{{.Host.Security.AppArmorEnabled}}"])
    apparmor = proc.returncode == 0 and (proc.stdout or "").strip() == "true"
    if apparmor:
        print("image: the engine enforces AppArmor; gates run without no-new-privileges")
    return [] if apparmor else [NNP]


def check_words(job: Job, name: str, words: list[str]) -> None:
    """The planner validated these; a job with a socket holds the same line again.

    An allowlist: only the options the planner emits, `--opt=value` and `--opt value`
    read alike. An unknown option, a stray word or a value off its rule is a 2.
    """
    sources = (f"{job.volume}:", f"{job.host_root}:")
    i = 0
    while i < len(words):
        word = words[i]
        i += 1
        if word == "--":
            break
        opt, eq, value = word.partition("=")
        if opt in RUN_FLAGS and not eq:
            continue
        shown = word
        if opt in RUN_VALUED:
            if not eq and i < len(words):
                value, i = words[i], i + 1
            elif not eq:
                value = ""
            shown = value
            if run_value_ok(opt, value, sources):
                continue
        cannot(f"{name}: refused podman option {shown!r} in a host step")


def run_value_ok(opt: str, value: str, sources: tuple[str, ...]) -> bool:
    """An allowlisted option's value. --pull, --network, -v/--volume and --mount
    must be the planner's own (never, none, an allowed source, type=image,); any
    other allowlisted option takes any non-empty value."""
    rules: dict[str, Callable[[str], bool]] = {
        "--pull": lambda v: v == "never",
        "--network": lambda v: v == "none",
        "-v": lambda v: v.startswith(sources),
        "--volume": lambda v: v.startswith(sources),
        "--mount": lambda v: v.startswith("type=image,"),
    }
    return rules.get(opt, bool)(value)


def stays_up(job: Job, name: str, safe: list[str], words: list[str], seconds: int) -> bool:
    """container-smoke: still running when the seconds are up, or exited 0, passes."""
    cname = f"sdlc-smoke-{job.volume}"
    image = words[words.index("--") + 1] if "--" in words else ""
    pid1 = job.run([*job.podman, "image", "inspect", "--format", "{{json .Config}}", image])
    try:
        config = json.loads(pid1.stdout)
    except ValueError:
        config = None
    if not isinstance(config, dict):
        config = {}
    first = ((config.get("Entrypoint") or []) + (config.get("Cmd") or []) + [""])[0]
    if first in SYSTEMD or first in ("init", "systemd"):
        print(f"{name}: SKIPPED: the image starts {first} as PID 1; never a pass", flush=True)
        return True
    started = job.run(
        [*job.podman, "run", "-d", "--name", cname, "--log-driver=k8s-file", *safe, *words]
    )
    try:
        if started.returncode == PODMAN_CANNOT_CREATE:
            cannot(f"{name}: podman could not create the container; nothing was judged")
        if started.returncode != 0:
            print(f"{name}: the image could not start its entrypoint", flush=True)
            return False
        for _ in range(seconds):
            if (
                job.run(
                    [*job.podman, "inspect", "--format", "{{.State.Running}}", cname]
                ).stdout.strip()
                != "true"
            ):
                break
            time.sleep(1)
        state = job.run(
            [
                *job.podman,
                "inspect",
                "--format",
                "{{.State.Running}} {{.State.ExitCode}} {{.State.OOMKilled}}",
                cname,
            ]
        )
        logs = job.run([*job.podman, "logs", cname])
        show(logs)
    finally:
        job.run([*job.podman, "rm", "-f", "-v", cname])
    got = state.stdout.split()
    if len(got) != STATE_FIELDS:
        cannot(f"{name}: podman could not report the container state; nothing was judged")
    return got[0] == "true" or (got[1] == "0")


# ------------------------------------------------------------------ push


def push(job: Job, img: Image, tag: str, parent: Parent | None) -> int:
    ref = img.ref
    gated = f"gated-{job.sha}"
    step(f"push {ref}:{gated}")
    # The digest podman reports for what it pushed, not whatever the tag names later.
    digestfile = job.scratch / f"pushed-{img.name}.digest"
    argv = [*job.podman, "push", "--authfile", str(job.auth), "--digestfile", str(digestfile)]
    job.must([*argv, tag, f"docker://{ref}:{gated}"])
    digest = digestfile.read_text().strip() if digestfile.is_file() else ""
    if not DIGEST.fullmatch(digest):
        cannot(f"podman push of {ref}:{gated} reported {digest!r}, not a sha256 digest")
    # A sibling built FROM this one builds on these bytes; its label names the image.
    job.built[f"{ORG}/{img.name}"] = Parent(-1, img.ref, f"{ref}@{digest}", digest)
    pinned = parent.pinned if parent else ""
    job.results.append(
        {
            "name": img.name,
            "ref": ref,
            "digest": digest,
            "sha": job.sha,
            "tag": gated,
            "parent": pinned,
        }
    )
    print(f"built once: {ref}@{digest} ({gated})", flush=True)
    return CLEAN
