"""What every job shares: the exit contract, the tools it drives, its outputs.

The tools -- processes and HTTP -- go through one `Tools` object so the suites
can hand a job a fake one: no test runs podman, cosign or the GitHub API.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import secrets
import subprocess
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import IO, NoReturn, TypedDict, Unpack

CLEAN = 0
FOUND = 1
CANNOT_RUN = 2
DEFERRED = 3
PODMAN_CANNOT_CREATE = 125  # podman's own error, before anything in the container ran
TIMED_OUT = 124  # what Tools.run reports for a process it stopped at its timeout

FROM_AS_WORDS = 3  # image, AS, name

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
BUILD_VAR = re.compile(r"\$(?:\{(?P<braced>\w+)(?::?-(?P<default>[^}]*))?\}|(?P<bare>\w+))")


class CannotRunError(Exception):
    """Anything that is not a verdict. `sdlc event` turns it into exit 2."""


def cannot(msg: str) -> NoReturn:
    raise CannotRunError(msg)


def show(proc: subprocess.CompletedProcess[str]) -> None:
    """Print what a tool said, stdout then stderr, into the job's log."""
    print(proc.stdout + proc.stderr, end="", flush=True)


def step(msg: str) -> None:
    print(f"\n== {msg}", flush=True)


class RunOptions(TypedDict, total=False):
    """The keyword arguments `Tools.run` takes, for the helpers that pass them on."""

    input: str | None
    env: Mapping[str, str] | None
    cwd: str | None
    stream: bool
    timeout: float | None


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

    def run(self, argv: list[str], **kw: Unpack[RunOptions]) -> subprocess.CompletedProcess[str]:
        """argv's verdict. `stream` sends its output straight to the job log instead of
        holding it, for the long runs (gates) whose output nothing parses. A process
        still running at `timeout` seconds is killed and reported as exit TIMED_OUT."""
        env, timeout = kw.get("env"), kw.get("timeout")
        try:
            return subprocess.run(
                argv,
                input=kw.get("input"),
                env=None if env is None else dict(env),
                cwd=kw.get("cwd"),
                capture_output=not kw.get("stream", False),
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(argv, TIMED_OUT, "", f"timed out after {timeout}s")
        except OSError as exc:
            cannot(f"could not start {argv[0]}: {exc}")

    def converse(
        self,
        argv: list[str],
        messages: list[dict],
        *,
        timeout: int,
        env: Mapping[str, str] | None = None,
    ) -> tuple[dict[int, dict], str, int | None]:
        """Speak JSON-RPC lines on argv's stdio, then kill it: ({id: reply}, why, exit).

        Each message is written in turn, and one with an `id` waits for its reply;
        `why` is "" when every reply came within `timeout` seconds, else what went
        wrong with the tail of its stderr. Lines that are not JSON are logs, skipped.
        `exit` is the process's own exit code when it ended before it was killed.
        """
        with tempfile.TemporaryFile(mode="w+") as errlog:
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=errlog,
                    text=True,
                    env=None if env is None else dict(env),
                )
            except OSError as exc:
                cannot(f"could not start {argv[0]}: {exc}")
            try:
                replies, why = converse_on(proc, messages, timeout, errlog)
                if why:  # its stdout closed or it went silent: let an exit land
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=2)
                return replies, why, proc.poll()
            finally:
                proc.kill()
                proc.wait()

    def http(
        self, method: str, url: str, headers: Mapping[str, str], payload: bytes | None = None
    ) -> Response:
        if not url.startswith("https://"):
            cannot(f"refusing a non-https URL: {url}")
        req = urllib.request.Request(url, data=payload, method=method, headers=dict(headers))  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                return Response(resp.status, resp.read())
        except urllib.error.HTTPError as exc:
            return Response(exc.code, exc.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            cannot(f"{method} {url}: {exc}")


def converse_on(
    proc: subprocess.Popen[str], messages: list[dict], timeout: int, errlog: IO[str]
) -> tuple[dict[int, dict], str]:
    """`Tools.converse` on a started process; readline() blocks, so a thread reads."""
    stdin, stdout = proc.stdin, proc.stdout
    if stdin is None or stdout is None:
        cannot("the process was started without stdio pipes")
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        for line in stdout:
            lines.put(line)
        lines.put(None)

    def tail() -> str:
        errlog.seek(0)
        return errlog.read()[-4000:]

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + timeout
    replies: dict[int, dict] = {}
    for msg in messages:
        try:
            stdin.write(json.dumps(msg) + "\n")
            stdin.flush()
        except BrokenPipeError:
            return replies, f"the server exited before {msg['method']}:\n{tail()}"
        if "id" in msg and (why := await_reply(lines, msg, replies, deadline, timeout)):
            return replies, f"{why}:\n{tail()}"
    return replies, ""


def await_reply(
    lines: queue.Queue[str | None],
    msg: dict,
    replies: dict[int, dict],
    deadline: float,
    timeout: int,
) -> str:
    """Read lines into `replies` until `msg`'s id is answered: '' then, else why not."""
    while msg["id"] not in replies:
        try:
            line = lines.get(timeout=max(deadline - time.monotonic(), 0.1))
        except queue.Empty:
            return f"no reply to {msg['method']} within {timeout}s"
        if line is None:
            return f"the server exited without answering {msg['method']}"
        reply = json_line(line)  # a log line on stdout is skipped
        if isinstance(reply, dict) and isinstance(reply.get("id"), int):
            replies[reply["id"]] = reply
    return ""


def json_line(line: str) -> object:
    try:
        return json.loads(line)
    except ValueError:
        return None


def must(tools: Tools, argv: list[str], **kw: Unpack[RunOptions]) -> str:
    """Run argv; its stdout, or exit 2 naming what failed. For steps that are not gates."""
    proc = tools.run(argv, **kw)
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
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    return tools.http(method, base + path, headers, payload)


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
    # sdlc.toml holds the same [tool.sdlc] tables as pyproject.toml, as `sdlc` reads it.
    for name in ("pyproject.toml", "sdlc.toml"):
        path = root / name
        if not path.is_file():
            continue
        try:
            doc: object = tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError) as exc:
            cannot(f"{name} does not parse: {exc}")
        for k in ("tool", "sdlc"):
            doc = doc.get(k, {}) if isinstance(doc, dict) else {}
        if not isinstance(doc, dict):
            cannot(f"[tool.sdlc] in {name} is not a table")
        return doc
    return {}


# A container-release call's inputs, which scripts/convert_repo.py moves into
# [tool.sdlc.image]. The image job enforces the first group (the boot keys through
# sdlc_jobs/_boot.py). The second is shell, or the host's disk, which a job that
# holds the registry secrets and the socket cannot run: accepted so a converted
# repo is not refused, and printed as not enforced (crunchtools/sdlc#118).
IMAGE_ENFORCED = {
    "image",
    "containerfile",
    "context",
    "rhsm",
    "redhat-registry",
    "format",
    "build-args",
    "boot",
    "boot-env",
    "boot-run-args",
    "boot-cmd",
    "boot-units",
    "boot-port",
    "boot-path",
    "boot-timeout",
    "boot-none-reason",
}
IMAGE_NOT_ENFORCED = {"boot-command", "contract-test", "free-disk"}


def image_settings(root: Path, repo: str) -> dict:
    """[tool.sdlc.image], checked; `image` is quay.io/crunchtools/<repo>, nothing else.

    The registry credentials are the org's, so a repo that could name any image
    could push over another repo's. `image` may only restate its own.

    A string `image` is the seat's launcher tag (bin/sdlc), not these settings.
    """
    given = tool_sdlc(root).get("image", {})
    s = dict(given) if isinstance(given, dict) else {}
    stray = sorted(set(s) - IMAGE_ENFORCED - IMAGE_NOT_ENFORCED)
    if stray:
        cannot(f"[tool.sdlc.image] has {', '.join(stray)}, which is no setting of the image job")
    own = f"quay.io/crunchtools/{repo.rpartition('/')[2]}"
    s.setdefault("image", own)
    if s["image"] != own:
        cannot(f"[tool.sdlc.image] image must be this repo's own, {own}; got {s['image']!r}")
    for key in ("rhsm", "redhat-registry"):
        if not isinstance(s.get(key, False), bool):
            cannot(f"[tool.sdlc.image] {key} must be true or false")
    if s.get("format", "docker") not in ("docker", "oci"):
        cannot("[tool.sdlc.image] format must be docker or oci")
    # The build is container-build's plan, so the file and context are named there.
    named = s.get("containerfile", "Containerfile")
    if named not in ("Containerfile", "Dockerfile") or s.get("context", ".") != ".":
        cannot(
            "[tool.sdlc.image] containerfile and context are read from "
            "[tool.sdlc.ci.container-build]; move them there"
        )
    return s


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
    doc = inspect_self(tools, env, str(root))
    where = str(root)
    best: tuple[int, str] | None = None
    for mount in doc.get("Mounts") or []:
        dest = str(mount.get("Destination", "")).rstrip("/")
        inside = bool(dest) and (where == dest or where.startswith(dest + "/"))
        if inside and (best is None or len(dest) > best[0]):
            best = (len(dest), str(mount["Source"]).rstrip("/") + where[len(dest) :])
    image = image_of(doc)
    if best is None or not image:
        cannot(f"the workspace {root} is not a mount of this container, or its image is unknown")
    return best[1], image


def inspect_self(tools: Tools, env: Mapping[str, str], cwd: str | None = None) -> dict:
    """`podman inspect` of this container, through the socket (`HOSTNAME` is its id)."""
    me = env.get("HOSTNAME")
    if not me:
        cannot("HOSTNAME is not set: cannot find this container through the socket")
    try:
        doc = json.loads(must(tools, [*podman(env), "inspect", me], cwd=cwd))[0]
    except (ValueError, IndexError, TypeError) as exc:
        cannot(f"podman inspect of this container did not parse: {exc}")
    if not isinstance(doc, dict):
        cannot("podman inspect of this container is not an object")
    return doc


def image_of(doc: Mapping[str, object]) -> str:
    """The image an inspected container runs: the name it was run by when that is
    pinned by digest (the bootstrap runs it so), which the launcher needs; else
    the image id, which `podman run` takes as well. "" when neither is known."""
    name = str(doc.get("ImageName") or "")
    return name if "@sha256:" in name else str(doc.get("Image") or "")


def run_job(job: ModuleType, event_name: str, event: dict, env: Mapping[str, str]) -> int:
    """Run a job module's `main` (ai, write, gate), which makes its own tools, or
    its `run` with the real tools, the one place `Tools` is made.

    Its verdict must be one of the exit codes; anything else, and any exception
    that is not a CannotRunError, is a 2.
    """
    # `main` first: ai.py has a `run` of its own with another signature.
    try:
        if hasattr(job, "main"):
            verdict = job.main(event_name, event, env)
        elif hasattr(job, "run"):
            verdict = job.run(event_name, event, env, Tools())
        else:
            cannot(f"job {job.__name__} has neither run nor main")
    except Exception as exc:
        # A refusal stands; any other exception is a bug in the job, and a bug
        # is never a verdict: not a 1 (FOUND).
        if isinstance(exc, CannotRunError):
            raise
        cannot(f"job {job.__name__} raised {type(exc).__name__}: {exc}")
    if verdict == DEFERRED:
        cannot(f"job {job.__name__} deferred to CI, and a job is CI: nothing was judged")
    if verdict not in (CLEAN, FOUND, CANNOT_RUN):
        cannot(f"job {job.__name__} returned {verdict!r}, outside the exit contract")
    return verdict


def expand_build_vars(image: str, args: Mapping[str, str], *, strict: bool) -> str:
    """`image` with its $VAR and ${VAR:-default} filled in from the Containerfile's ARGs.

    Strict, an unset variable with no default is a 2; otherwise it expands to nothing.
    """

    def value(m: re.Match[str]) -> str:
        name = m["braced"] or m["bare"]
        if strict and name not in args and m["default"] is None:
            cannot(f"FROM {image} uses a build variable with no default: the parent is unknown")
        return args.get(name, m["default"] or "")

    return BUILD_VAR.sub(value, image)


def final_stage(text: str, *, strict: bool) -> tuple[int, str] | None:
    """(index of the FROM the last stage is built on, its image), None without a FROM.

    Global ARG defaults are filled in, and a `FROM <earlier stage>` is followed back to
    the FROM that names a real image. Small on purpose: the root `sdlc` script's
    `final_base` is the thorough one. See `expand_build_vars` for `strict`.
    """
    args: dict[str, str] = {}
    stages: list[tuple[str, str | None]] = []
    for raw in re.sub(r"\\\n", " ", text).splitlines():
        words = raw.split()
        op = words[0].upper() if words else ""
        if op == "ARG" and not stages:
            args.update(
                {k: v.strip("\"'") for k, eq, v in (w.partition("=") for w in words[1:]) if eq}
            )
        elif op == "FROM":
            rest = [w for w in words[1:] if not w.startswith("--")]
            if rest:
                aliased = len(rest) >= FROM_AS_WORDS and rest[1].upper() == "AS"
                stages.append((rest[0], rest[2].lower() if aliased else None))
    index = len(stages) - 1
    while index >= 0:
        image = expand_build_vars(stages[index][0], args, strict=strict).strip("\"'")
        earlier = [i for i in range(index) if stages[i][1] == image.lower()]
        if not earlier:
            return index, image
        index = earlier[-1]
    return None
