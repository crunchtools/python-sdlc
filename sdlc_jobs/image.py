"""The `image` job: verify the parent, build FROM its digest, gate, push `gated-<sha>`.

It holds the registry secrets (quay, RHSM, registry.redhat.io) and the runner's
container socket, never the cosign key. It runs on a default-branch push, the
schedule or a dispatch, so the Containerfile it builds is reviewed, merged code; a
pull request's is built only by `check`, which holds no secret. `pull_request` is a 2
here as the second guard behind the workflow's own `if:`.

`check`'s plan only says this job runs. The parent is resolved and verified here:
`quay.io/crunchtools/*` with the fleet key and `-a repo=<parent repo>`, Red Hat's
registries with Red Hat's release key (gha-runner's `policy.json`, run through
`skopeo --policy`), and any other registry is refused. The build then runs FROM that
verified `@sha256:` digest whatever tag the Containerfile names: `--from` when the stage
is the first, and a rewritten copy of the Containerfile otherwise, because podman's
`--from` only replaces the FIRST stage's FROM and the image that ships is the last.

The gates are the ones `sdlc ci` plans for this repo. `sdlc plan ci` is asked for the
build argv (so `[tool.sdlc.ci.container-build]` settings still hold) and for smoke, scan
and bootc-lint, and they are run here through the socket as the launcher runs them.
Container-release pushed before it gated; this job gates the local image and pushes only
what passed, so no ungated digest exists in the registry.

`result` is `{"ref", "digest", "sha", "tag", "parent"}`: all `promote` takes from here,
and it re-validates every field.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sdlc_jobs._core import (
    CLEAN,
    DIGEST,
    FLEET_PUBLIC_KEY,
    FOUND,
    IMAGE_NOT_ENFORCED,
    Tools,
    cannot,
    final_stage,
    host_mount,
    image_settings,
    must,
    podman,
    set_output,
    show,
    step,
    workspace,
)

QUAY = "quay.io"
ORG = "crunchtools"
RED_HAT = ("registry.redhat.io", "registry.access.redhat.com")
RH_KEY = "/etc/pki/sigstore/SIGSTORE-redhat-release3"
RH_REKOR = "/etc/pki/sigstore/REKOR-signing-key"
US = "\x1f"  # the plan's argv separator
SHA = re.compile(r"[0-9a-f]{40}")
GATES = ("container-smoke", "container-scan", "bootc-lint")
SECRETS = (
    "QUAY_USERNAME",
    "QUAY_PASSWORD",
    "RHSM_ACTIVATION_KEY",
    "RHSM_ORG_ID",
    "REGISTRY_REDHAT_IO_USER",
    "REGISTRY_REDHAT_IO_PASSWORD",
    "GITHUB_TOKEN",
)
FROM_LINE = re.compile(r"^(\s*FROM\s+(?:--\S+\s+)*)(\S+)", re.IGNORECASE)
# Podman options a repo's config must not be able to put on a host step.
DENIED = ("--privileged", "--cap-add", "--device", "--userns", "--pid", "--ipc", "--uts")
PLAN_FIELDS = 4  # name, stage, mode, argv
STATE_FIELDS = 3  # running, exit code, OOM-killed
PODMAN_CANNOT_CREATE = 125
HADOLINT_UNPARSED = 4  # containerfile-lint: hadolint could not parse the file
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
    settings: dict = field(default_factory=dict)  # [tool.sdlc.image], checked

    def run(self, argv: list[str], *, input: str | None = None) -> subprocess.CompletedProcess[str]:
        return self.tools.run(argv, input=input, env=self.child_env, cwd=str(self.root))

    def must(self, argv: list[str], *, input: str | None = None) -> str:
        return must(self.tools, argv, input=input, env=self.child_env, cwd=str(self.root))


def run(event_name: str, event: dict, env: Mapping[str, str], tools: Tools) -> int:
    """Build, gate and push this commit's image; 0 pushed, 1 a gate found something.

    `event_name` must not be `pull_request`: this job holds the registry secrets.
    `event` is unused, because everything comes from `env`: GITHUB_REPOSITORY,
    GITHUB_SHA, GITHUB_WORKSPACE, HOSTNAME (this container, for the host path),
    QUAY_*, RHSM_* and REGISTRY_REDHAT_IO_*. `tools` runs podman, skopeo and cosign
    (a fake in the suites). Raises CannotRunError, which is exit 2, when an input is
    malformed, the parent does not verify, or a step that is not a gate fails.
    """
    del event  # the job contract passes it; image reads only the environment
    if event_name == "pull_request":
        cannot("image never runs on pull_request: it holds the registry secrets")
    repo = env.get("GITHUB_REPOSITORY", "")
    sha = env.get("GITHUB_SHA", "")
    if repo.count("/") != 1 or not SHA.fullmatch(sha):
        cannot("GITHUB_REPOSITORY and GITHUB_SHA are required, and the SHA is 40 hex digits")
    root = workspace(env)
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
    job.settings = image_settings(root, repo)
    for key in sorted(IMAGE_NOT_ENFORCED & set(job.settings)):
        print(f"image: [tool.sdlc.image] {key} is not enforced yet (crunchtools/sdlc#110)")
    job.child_env = {k: v for k, v in env.items() if k not in SECRETS}
    job.child_env["REGISTRY_AUTH_FILE"] = str(job.auth)
    try:
        return build_and_push(job)
    finally:
        job.run([*job.podman, "volume", "rm", "-f", job.volume])
        shutil.rmtree(scratch, ignore_errors=True)


def build_and_push(job: Job) -> int:
    containerfile = next(
        (f for f in ("Containerfile", "Dockerfile") if (job.root / f).is_file()), None
    )
    if containerfile is None:
        cannot("there is no Containerfile or Dockerfile at the repo root to build")
    text = (job.root / containerfile).read_text(encoding="utf-8", errors="replace")
    login(job)
    parent = parent_stage(text)
    if parent is not None:
        verify_parent(job, parent)
    step("map the workspace and plan the gates")
    job.host_root, job.image_id = host_mount(job.tools, job.env, job.root)
    plan = gate_plan(job)
    if "container-build" not in plan:
        cannot("`sdlc plan ci` plans no container-build for this repo")
    found = lint(job)
    tag = build(job, plan, text, parent)
    for name in GATES:
        if name in plan and not run_gate(job, name, *plan[name]):
            found = True
    if found:
        print("\nimage: a gate found something; nothing is pushed", flush=True)
        return FOUND
    return push(job, tag, parent.pinned if parent else "")


def login(job: Job) -> None:
    """One auth file for skopeo and the remote podman, in this job's temp directory."""
    for host, user, password in (
        (QUAY, "QUAY_USERNAME", "QUAY_PASSWORD"),
        ("registry.redhat.io", "REGISTRY_REDHAT_IO_USER", "REGISTRY_REDHAT_IO_PASSWORD"),
    ):
        if not job.env.get(user):
            if host == QUAY:
                cannot("QUAY_USERNAME is not set: nothing could be pushed")
            if job.settings.get("redhat-registry"):
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


def parent_stage(text: str) -> Parent | None:
    """The FROM that names a registry image and that image; None for scratch.

    An unset build variable is a 2: nothing to verify.
    """
    found = final_stage(text, strict=True)
    if found is None:
        cannot("the Containerfile has no FROM to verify")
    index, image = found
    return None if image.lower() == "scratch" else Parent(index, image)


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
    digest = job.must(["skopeo", "inspect", "--format", "{{.Digest}}", f"docker://{image}"]).strip()
    if not DIGEST.fullmatch(digest):
        cannot(f"{image} resolved to {digest!r}, not a sha256 digest")
    pinned = f"{name}@{digest}"
    if host == QUAY and path.startswith(f"{ORG}/") and path.count("/") == 1:
        key = job.scratch / "fleet.pub"
        key.write_text(FLEET_PUBLIC_KEY)
        argv = ["cosign", "verify", "--insecure-ignore-tlog", "--key", str(key)]
        job.must([*argv, "-a", f"repo={path}", pinned])
    elif host in RED_HAT:
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
                    "default": [{"type": "insecureAcceptAnything"}],
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
                f"docker://{pinned}",
                f"dir:{job.scratch / 'parent'}",
            ]
        )
    else:
        cannot(f"the parent {image} is on {host}, which this job does not trust: refused")
    parent.pinned, parent.digest = pinned, digest
    print(f"parent verified: {pinned}", flush=True)


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
    return plan


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


def pin_args(job: Job, text: str, parent: Parent | None, argv: list[str]) -> list[str]:
    """podman build words that put the verified digest in the parent's FROM."""
    if parent is None:
        return argv
    if parent.index == 0:
        return ["--from", parent.pinned, *argv]
    kept: list[str] = []
    seen = -1
    for line in text.splitlines(keepends=True):
        m = FROM_LINE.match(line)
        if m:
            seen += 1
        if m and seen == parent.index:
            kept.append(f"{m[1]}{parent.pinned}{line[m.end() :]}")
        else:
            kept.append(line)
    copy = pinned_file(job)
    copy.write_text("".join(kept))
    if "-f" not in argv:
        cannot("the build plan names no Containerfile with -f")
    out = list(argv)
    out[out.index("-f") + 1] = copy.name
    return out


def build(job: Job, plan: dict[str, tuple[str, str]], text: str, parent: Parent | None) -> str:
    """Build through the socket at the tag smoke and scan read; return that tag."""
    step("build")
    words = plan["container-build"][1].split(US)
    if words[:2] != ["podman", "build"] or "-t" not in words:
        cannot("the container-build plan is not a `podman build` with a tag")
    tag = words[words.index("-t") + 1]
    argv = [
        *job.podman,
        "build",
        "--pull=always",
        "--format",
        job.settings.get("format") or ("oci" if "bootc-lint" in plan else "docker"),
    ]
    argv += ["--authfile", str(job.auth)]
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
        argv += pin_args(job, text, parent, words[2:])
        proc = job.run(argv)
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
    safe = ["--cap-drop=all", "--security-opt=no-new-privileges", "--events-backend=none"]
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


def check_words(job: Job, name: str, words: list[str]) -> None:
    """The planner validated these; a job with a socket holds the same line again."""
    allowed = (f"{job.volume}:", f"{job.host_root}:")
    for i, word in enumerate(words):
        if word == "--":
            break
        prev = words[i - 1] if i else ""
        bad = (
            word.split("=")[0] in DENIED
            or (word in ("--network", "--net") and words[i + 1 : i + 2] != ["none"])
            or (word.startswith(("--network=", "--net=")) and word.split("=", 1)[1] != "none")
            or (prev in ("-v", "--volume") and not word.startswith(allowed))
            or (prev == "--mount" and not word.startswith("type=image,"))
        )
        if bad:
            cannot(f"{name}: refused podman option {word!r} in a host step")


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


def push(job: Job, tag: str, pinned: str) -> int:
    ref = job.settings.get("image") or f"{QUAY}/{ORG}/{job.name}"
    gated = f"gated-{job.sha}"
    step(f"push {ref}:{gated}")
    job.must([*job.podman, "push", "--authfile", str(job.auth), tag, f"docker://{ref}:{gated}"])
    digest = job.must(
        ["skopeo", "inspect", "--format", "{{.Digest}}", f"docker://{ref}:{gated}"]
    ).strip()
    if not DIGEST.fullmatch(digest):
        cannot(f"{ref}:{gated} resolved to {digest!r}, not a sha256 digest")
    result = {"ref": ref, "digest": digest, "sha": job.sha, "tag": gated, "parent": pinned}
    set_output(job.env, "result", json.dumps(result, separators=(",", ":")))
    print(f"built once: {ref}@{digest} ({gated})", flush=True)
    return CLEAN
