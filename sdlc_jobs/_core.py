"""What every job shares: the exit contract, the tools it drives, its outputs.

The tools -- processes and HTTP -- go through one `Tools` object so the suites
can hand a job a fake one: no test runs podman, cosign or the GitHub API.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import tomllib
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

CLEAN = 0
FOUND = 1
CANNOT_RUN = 2
DEFERRED = 3

# The runner's container socket, as the job container sees it: GitHub mounts it there.
SOCKET = "unix:///var/run/docker.sock"
# The fleet's cosign key, public half: what container-release and sign-digest.sh verify with.
FLEET_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE5TlIf0f5VMBaCLJGtmh9fL3C/xYM
nHVNg0KnBqqs/x+WThX8zEKaGmiZ2SM5336mPfJKZl68/lXBnoWlU0E9Tg==
-----END PUBLIC KEY-----
"""

SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class CannotRunError(Exception):
    """Anything that is not a verdict. `sdlc event` turns it into exit 2."""


def cannot(msg: str) -> NoReturn:
    raise CannotRunError(msg)


def step(msg: str) -> None:
    print(f"\n== {msg}", flush=True)


@dataclass
class Response:
    status: int
    body: bytes

    def json(self) -> object:
        try:
            return json.loads(self.body or b"null")
        except ValueError as exc:
            cannot(f"the API answered {self.status} with no JSON: {exc}")


class Tools:
    """The real processes and network. Suites pass a fake with the same methods."""

    def run(
        self,
        argv: list[str],
        *,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                argv,
                input=input,
                env=None if env is None else dict(env),
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            cannot(f"could not start {argv[0]}: {exc}")

    def http(
        self, method: str, url: str, headers: Mapping[str, str], data: bytes | None = None
    ) -> Response:
        if not url.startswith("https://"):
            cannot(f"refusing a non-https URL: {url}")
        req = urllib.request.Request(url, data=data, method=method, headers=dict(headers))  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                return Response(resp.status, resp.read())
        except urllib.error.HTTPError as exc:
            return Response(exc.code, exc.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            cannot(f"{method} {url}: {exc}")


def must(tools: Tools, argv: list[str], **kw: object) -> str:
    """Run argv; its stdout, or exit 2 naming what failed. For steps that are not gates."""
    proc = tools.run(argv, **kw)  # type: ignore[arg-type]
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        cannot(f"`{' '.join(argv[:3])}` exited {proc.returncode}: {' / '.join(tail)}")
    return proc.stdout


def call(
    tools: Tools, env: Mapping[str, str], method: str, path: str, body: object = None
) -> Response:
    """One GitHub REST call with the job's own GITHUB_TOKEN, whatever it answers."""
    base = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    if not base.startswith("https://"):
        cannot(f"GITHUB_API_URL is not https: {base}")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "sdlc-event",
    }
    if env.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {env['GITHUB_TOKEN']}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    return tools.http(method, base + path, headers, data)


def api(
    tools: Tools, env: Mapping[str, str], method: str, path: str, body: object = None
) -> object:
    """`call`, where anything but 200, 201 or 204 is a 2. Use `call` to read a 404."""
    resp = call(tools, env, method, path, body)
    if resp.status not in (200, 201, 204):
        cannot(f"{method} {path} answered {resp.status}: {resp.body[:300]!r}")
    return resp.json()


def set_output(env: Mapping[str, str], name: str, value: str) -> None:
    """Append one step output to $GITHUB_OUTPUT, multi-line safe."""
    path = env.get("GITHUB_OUTPUT")
    if not path:
        cannot("GITHUB_OUTPUT is not set: this job's result would be lost")
    delim = f"EOF_{secrets.token_hex(16)}"
    if delim in value:
        cannot("output collides with its delimiter")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{name}<<{delim}\n{value}\n{delim}\n")


def needed(name: str, result: str | None) -> None:
    """A job the plan needed must have succeeded; skipped is a failure, not a pass."""
    if result != "success":
        cannot(f"`{name}` was needed and its result is {result or 'missing'!r}, not success")


def workspace(env: Mapping[str, str]) -> Path:
    return Path(env.get("GITHUB_WORKSPACE") or os.getcwd())


def tool_sdlc(root: Path) -> dict:
    """The repo's [tool.sdlc]: pyproject.toml, or sdlc.toml without one; {} for neither."""
    for name, key in (("pyproject.toml", ("tool", "sdlc")), ("sdlc.toml", ())):
        path = root / name
        if not path.is_file():
            continue
        try:
            doc: object = tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError) as exc:
            cannot(f"{name} does not parse: {exc}")
        for k in key:
            doc = doc.get(k, {}) if isinstance(doc, dict) else {}
        if not isinstance(doc, dict):
            cannot(f"[tool.sdlc] in {name} is not a table")
        return doc
    return {}


def plan(env: Mapping[str, str]) -> dict:
    """check's plan as a later job reads it from SDLC_CHECK; {} when absent.

    The plan is a plan, never an authority: it says which jobs are needed, and
    each job re-checks what it acts on. Its shape, written by check.py:

        image    bool  image and promote run (default-branch push of an image
                       repo, the weekly rebuild, or a moved parent digest)
        release  null, or {"version": "X.Y.Z", "publish": "pypi" | "", "pr": N}
                 on the push that merges an App-authored ashigaru/release-X.Y.Z
        write    bool  write runs outside its own events: a release merge, or
                       the weekly audit with an alert to keep
        weekly   bool  this is the weekly run (audit, rebuild, keepalive)
        audit    null, or {"found": bool, "summary": str} on the weekly run
    """
    raw = env.get("SDLC_CHECK") or "{}"
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        cannot(f"SDLC_CHECK is not JSON: {exc}")
    if not isinstance(doc, dict):
        cannot("SDLC_CHECK is not an object")
    return doc


def podman(env: Mapping[str, str]) -> list[str]:
    """podman driving the runner's engine through the socket, never one of its own."""
    return ["podman", "--remote", "--url", env.get("CONTAINER_HOST") or SOCKET]


def host_mount(tools: Tools, env: Mapping[str, str], root: Path) -> tuple[str, str]:
    """(host path of `root`, this container's image), found through the socket.

    A container started through the socket mounts paths on the host, not in this
    container, so the host path comes from inspecting this container (`HOSTNAME`
    is its id) and mapping the longest mount that holds `root`.
    """
    me = env.get("HOSTNAME")
    if not me:
        cannot("HOSTNAME is not set: cannot find this container to map the workspace")
    try:
        doc = json.loads(must(tools, [*podman(env), "inspect", me], cwd=str(root)))[0]
    except (ValueError, IndexError, TypeError) as exc:
        cannot(f"podman inspect of this container did not parse: {exc}")
    where = str(root)
    best: tuple[int, str] | None = None
    for mount in doc.get("Mounts") or []:
        dest = str(mount.get("Destination", "")).rstrip("/")
        inside = bool(dest) and (where == dest or where.startswith(dest + "/"))
        if inside and (best is None or len(dest) > best[0]):
            best = (len(dest), str(mount["Source"]).rstrip("/") + where[len(dest) :])
    # The name it was run by when that is pinned by digest (the bootstrap runs it so),
    # which the launcher needs; else the image id, which `podman run` takes as well.
    name = str(doc.get("ImageName") or "")
    image = name if "@sha256:" in name else doc.get("Image")
    if best is None or not image:
        cannot(f"the workspace {root} is not a mount of this container, or its image is unknown")
    return best[1], str(image)
