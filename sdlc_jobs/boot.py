"""The `boot` job: a repo's own boot proof against the gated image, holding no secret.

`[tool.sdlc.image]`'s `boot = "command"` (`boot-command`) and `contract-test` are
shell from the repo, run from the repo root with `$IMAGE` set, as container-release
ran them on the runner: in a container of this job's image started beside it through
the socket, with the workspace at its runner path, TMPDIR under it, the socket, and
the runner's network, so a `-v` source, `mktemp` and 127.0.0.1 mean what they did.
The `image` job holds the registry secrets (quay, RHSM, registry.redhat.io) and the
socket, so repo shell never runs there; it runs here, in a job with a read
token and nothing else (crunchtools/sdlc#118). `promote` needs this job, so a digest
whose boot proof fails is never signed or tagged.

The image is the one `image` pushed, `gated-<sha>`; `image` never runs on a pull
request, so neither does this job. Its result is re-validated here as `promote`
validates it, then each image is pulled anonymously by digest, so what is booted
is exactly what is promoted. That costs the "no ungated digest in the registry" property for a repo
that sets these keys: its `gated-<sha>` is pushed before this job judges it.

`free-disk` removes a GitHub-hosted runner's preinstalled SDKs before the pull, the
directories container-release removed, so a multi-GB image fits; our own runners carry
no SDKs and the step says so. Every runner's socket is its host podman's, the fleet
file's launch, so the pull and the shell's container go through podman-remote. The
shell's output runs with workflow commands stopped. Its container is given PASSED and
nothing else of this job's environment, but it holds the socket, and through it can
exec into this job's own container and read GITHUB_TOKEN (contents: read) and
ACTIONS_RUNTIME_TOKEN. That is the job's whole credential, and it is why the job holds
no secret: the premise is our runner design, where each crunchtools-uncached runner is
an ephemeral VM with its own engine, running one job and gone.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sdlc_jobs._boot import TIMEOUT, boot_settings
from sdlc_jobs._core import (
    CLEAN,
    DIGEST,
    EXTRA_IMAGES,
    FOUND,
    PODMAN_CANNOT_CREATE,
    TIMED_OUT,
    Image,
    Tools,
    cannot,
    fetch_tree,
    host_mount,
    images,
    inspect_self,
    must,
    podman,
    step,
    workspace,
)
from sdlc_jobs.image import ORG, QUAY, SECRETS, SHA

EVENTS = ("push", "schedule", "workflow_dispatch")  # image's
# The job's tokens: refused or not, the GITHUB_TOKEN and OIDC request are the job's own.
OWN_TOKENS = ("GITHUB_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL")
# All the shell's container is given of the job's environment: no token, no path of
# this container's. Everything else it gets is set below (IMAGE, TMPDIR, the socket).
PASSED = (
    "GITHUB_ACTIONS",
    "GITHUB_EVENT_NAME",
    "GITHUB_REF",
    "GITHUB_REF_NAME",
    "GITHUB_REPOSITORY",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_RUN_ID",
    "GITHUB_SERVER_URL",
    "GITHUB_SHA",
    "RUNNER_ARCH",
    "RUNNER_ENVIRONMENT",
    "RUNNER_OS",
)
SOCKET_PATH = "/var/run/docker.sock"  # where the fleet file mounts the runner's socket
TMP = ".sdlc-boot-tmp"  # under the workspace: the shell's TMPDIR and HOME, on the runner
# container-release's free-disk list, on a GitHub-hosted runner.
EMPTY = "find /free -mindepth 2 -maxdepth 2 -exec rm -rf {} +"
SDK_DIRS = ("/usr/share/dotnet", "/usr/local/lib/android", "/opt/ghc", "/opt/hostedtoolcache")


def run(event_name: str, event: dict, env: Mapping[str, str], tools: Tools) -> int:
    """Run each gated image's boot-command and contract-test; 0 all passed, 1 one failed.

    Everything comes from `env`: GITHUB_REPOSITORY, GITHUB_SHA, GITHUB_WORKSPACE,
    SDLC_IMAGE_RESULT (image's output) and RUNNER_ENVIRONMENT. A secret in `env` is a
    2: this job is the one place repo shell meets the socket, so it must hold none.
    """
    del event  # the job contract passes it; boot reads only the environment
    if event_name not in EVENTS:
        cannot(f"boot does not run on {event_name!r}, only on {', '.join(EVENTS)}")
    if held := sorted(k for k in SECRETS if k not in OWN_TOKENS and env.get(k)):
        cannot(f"boot runs repo shell and must hold no secret; it was given {', '.join(held)}")
    repo, sha = env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_SHA", "")
    if repo.count("/") != 1 or not SHA.fullmatch(sha):
        cannot("GITHUB_REPOSITORY and GITHUB_SHA are required, and the SHA is 40 hex digits")
    root = workspace(env)
    fetch_tree(tools, env, root, repo)
    own = {img.name: img for img in images(root, repo)}
    gated = gated_images(env, repo, own)
    if not gated:
        print("boot: no gated image has anything for this job to run", flush=True)
        return CLEAN
    host_root, own_image = host_mount(tools, env, root)
    ctx = Ctx(tools, env, root, podman(env), host_root, own_image, socket_of(tools, env))
    for img, image in gated:
        if boot_image(ctx, img, image) == FOUND:
            return FOUND
    return CLEAN


@dataclass
class Ctx:
    tools: Tools
    env: Mapping[str, str]
    root: Path
    engine: list[str]  # podman-remote, through the runner's socket
    host_root: str  # the workspace's path on the runner, where the shell sees it
    image: str  # this job's own image: bash and the podman shim
    socket: str  # the runner's socket, on the runner


def socket_of(tools: Tools, env: Mapping[str, str]) -> str:
    """The runner's path of the socket this container has at SOCKET_PATH; or a 2."""
    mounts = inspect_self(tools, env).get("Mounts") or []
    found = [m for m in mounts if str(m.get("Destination", "")).rstrip("/") == SOCKET_PATH]
    if not found:
        cannot(f"the runner's socket is not mounted at {SOCKET_PATH} in this container")
    return str(found[0]["Source"])


def gated_images(
    env: Mapping[str, str], repo: str, own: dict[str, Image]
) -> list[tuple[Image, str]]:
    """(image, `ref@digest`) of each image `image` pushed, after every check promote makes.

    An image with nothing for this job to do is left out.
    """
    try:
        doc = json.loads(env.get("SDLC_IMAGE_RESULT") or "")
    except ValueError as exc:
        cannot(f"SDLC_IMAGE_RESULT is missing or not JSON: {exc}")
    if not isinstance(doc, dict):
        cannot("SDLC_IMAGE_RESULT is not an object")
    name = repo.rpartition("/")[2]
    docs = doc.get("images") if "images" in doc else [{**doc, "name": name}]
    if not isinstance(docs, list) or not docs or not all(isinstance(d, dict) for d in docs):
        cannot("SDLC_IMAGE_RESULT images is not a list of images")
    allowed = {name, *EXTRA_IMAGES.get(repo, ())} & set(own)
    out: list[tuple[Image, str]] = []
    seen: set[str] = set()
    for one in docs:
        which, image = checked(one, env, allowed)
        if which in seen:
            cannot(f"image's result names {which} twice")
        seen.add(which)
        img = own[which]
        if work(img.settings) or img.settings.get("free-disk"):
            out.append((img, image))
    return out


def checked(one: dict, env: Mapping[str, str], allowed: set[str]) -> tuple[str, str]:
    """(name, `ref@digest`) of one image's result, or a 2."""
    which, ref, digest = one.get("name"), one.get("ref"), one.get("digest")
    if not isinstance(which, str) or which not in allowed:
        cannot(f"image's result names {which!r}, which this repo does not publish")
    if not (isinstance(digest, str) and DIGEST.fullmatch(digest)):
        cannot("image's digest is not a well-formed sha256 digest")
    if one.get("sha") != env.get("GITHUB_SHA"):
        cannot(f"image built {one.get('sha')!r}, not this run's commit")
    if ref != f"{QUAY}/{ORG}/{which}":
        cannot(f"image's ref {ref!r} is not {QUAY}/{ORG}/{which}")
    return which, f"{ref}@{digest}"


def work(settings: dict) -> list[tuple[str, str]]:
    """(what, shell) this job runs for one image: its boot-command, then its contract-test."""
    spec = boot_settings(settings)
    out = []
    if spec is not None and spec.profile == "command":
        out.append(("boot-command", str(settings["boot-command"])))
    contract = settings.get("contract-test", "")
    if not isinstance(contract, str):
        cannot("[tool.sdlc.image] contract-test must be a string of shell")
    if contract.strip():
        out.append(("contract-test", contract))
    return out


def boot_image(ctx: Ctx, img: Image, image: str) -> int:
    """Free the disk if asked, pull `image` by digest, then run its shell; 1 on a failure."""
    if img.settings.get("free-disk"):
        free_disk(ctx)
    step(f"pull {image}")
    must(ctx.tools, [*ctx.engine, "pull", "--quiet", image], env=dict(ctx.env))
    spec = boot_settings(img.settings)
    timeout = spec.timeout if spec is not None else TIMEOUT
    (ctx.root / TMP).mkdir(exist_ok=True)
    for n, (what, shell) in enumerate(work(img.settings)):
        step(f"{img.name}: {what}")
        name = f"sdlc-boot-{ctx.env.get('GITHUB_RUN_ID', '0')}-{img.name}-{n}"
        resume = stop_commands()  # repo shell's output cannot annotate
        try:
            done = ctx.tools.run(
                sibling(ctx, name, image, shell), env=dict(ctx.env), stream=True, timeout=timeout
            )
        finally:
            print(f"::{resume}::", flush=True)
            ctx.tools.run([*ctx.engine, "rm", "--force", name], env=dict(ctx.env))
        if done.returncode == PODMAN_CANNOT_CREATE:  # 126/127 are bash's, on the repo's shell
            cannot(f"{what}: podman could not start the container (exit 125); nothing was judged")
        if done.returncode == TIMED_OUT:
            print(f"{what}: REFUSED: still running after {timeout}s", flush=True)
            return FOUND
        if done.returncode != 0:
            print(f"{what}: REFUSED: exited {done.returncode}", flush=True)
            return FOUND
        print(f"{what}: passed", flush=True)
    return CLEAN


def stop_commands() -> str:
    """Stop workflow commands until `::<token>::`: repo shell's output cannot annotate."""
    token = secrets.token_hex(16)
    print(f"::stop-commands::{token}", flush=True)
    return token


def sibling(ctx: Ctx, name: str, image: str, shell: str) -> list[str]:
    """`<engine> run` of `shell` beside this job, as container-release ran it on the runner.

    The workspace is mounted at its runner path and is the working directory, TMPDIR
    is under it, the socket is mounted and the network is the runner's: so a `-v`
    source the shell names, `mktemp`, and 127.0.0.1 mean what they did there. Its
    environment is PASSED and these, never this job's: a token in this process's
    environment cannot be read from a container that never shared it.
    """
    root, sock = ctx.host_root, SOCKET_PATH
    given = {k: ctx.env[k] for k in PASSED if k in ctx.env}
    given |= {
        "CI": "true",
        "HOME": f"{root}/{TMP}",
        "TMPDIR": f"{root}/{TMP}",
        "GITHUB_WORKSPACE": root,
        "CONTAINER_HOST": f"unix://{sock}",
        "DOCKER_HOST": f"unix://{sock}",
        "IMAGE": image,
    }
    return [
        *ctx.engine,
        *["run", "--rm", "--name", name, "--network=host", "--security-opt=no-new-privileges"],
        *["-v", f"{root}:{root}", "-v", f"{ctx.socket}:{sock}", "-w", root],
        *[w for k, v in sorted(given.items()) for w in ("-e", f"{k}={v}")],
        *["--entrypoint", "bash", ctx.image, "-euo", "pipefail", "-c", shell],
    ]


def free_disk(ctx: Ctx) -> None:
    """container-release's free-disk: empty the SDK directories, then prune images.

    Only on a GitHub-hosted runner, whose SDKs are root's: each directory is
    mounted into a container of this job's own image and emptied there. Our runners
    carry no SDKs, so there it is a no-op that says so.
    """
    step("free-disk")
    if ctx.env.get("RUNNER_ENVIRONMENT") != "github-hosted":
        print("free-disk: not a GitHub-hosted runner, which has no SDKs to remove", flush=True)
        return
    mounts = [w for i, d in enumerate(SDK_DIRS) for w in ("-v", f"{d}:/free/{i}")]
    must(
        ctx.tools,
        [
            *[*ctx.engine, "run", "--rm", "--network=none", *mounts],
            *["--entrypoint", "sh", ctx.image, "-c", EMPTY],
        ],
        env=dict(ctx.env),
    )
    must(ctx.tools, [*ctx.engine, "image", "prune", "--all", "--force"], env=dict(ctx.env))
