"""The boot gate: container-release's `boot` profiles, run by the image job before it pushes.

`[tool.sdlc.image] boot` names how the built image proves it starts, as the
`boot` input of constitution's container-release.yml did:

    mcp          podman run -i, then initialize / initialized / tools/list on stdio
    mcp-http     podman run -d, then the same over streamable HTTP on boot-port
    systemd      podman run -d --systemd=always; running, and every boot-unit active
    healthcheck  podman run -d; the image's own HEALTHCHECK passes
    none         no gate, and boot-none-reason says why in the log

container-release ran this in a job with no credentials. This one runs in the job
that holds the registry secrets and the runner's socket, and anything that can
drive that socket can read them (`podman inspect` of the job's own container). So
the repo supplies data, never code or a podman option of its own choosing:
`boot-run-args` is held to `RUN_FLAGS` and `RUN_VALUED`, the MCP clients are this
module's, and the HTTP one runs in a container of the sdlc image that joins the
server's network namespace, because the server's loopback is not this container's.
`boot: command` and `contract-test` are shell: they are not run here, and the job
prints so. They need a job of their own with no secret (crunchtools/sdlc#118).

A missing `boot` is no gate: a repo that never called container-release has
container-smoke. Every key is checked before the job logs in, so a typo is a 2
before anything is built.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sdlc_jobs._core import cannot, show, step

if TYPE_CHECKING:
    from sdlc_jobs.image import Job

PROFILES = ("mcp", "mcp-http", "systemd", "healthcheck", "command", "none")
MCP_PROTOCOL = "2025-06-18"
TIMEOUT = 180  # container-release's boot-timeout default
TIMEOUT_MAX = 1800
PORT_MAX = 65535
POLL_SECONDS = 2
HTTP_TIMEOUT = 10  # seconds per request to the server under test
LOG_TAIL = 4000  # characters of a failed container's log worth showing
ENV_LINE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=[^\x00-\x1f]*")
UNIT = re.compile(r"[\w@:.\\-]+")
# A tmpfs at an absolute path, with podman's options after the colon.
TMPFS = re.compile(r"/[\w./-]*(?::[\w=,.-]+)?")
# A volume from the checkout only, read-only or as an overlay, so the container
# sees the repo's fixture and cannot write it: ./<path>:/<dest>:<ro|O>[,z|,Z].
REPO_VOLUME = re.compile(r"\./[\w./-]*:/[\w./-]*:(?:ro|O)(?:,[zZ])?")
SETTLED = ("running", "degraded", "maintenance", "stopping", "offline")
sleep: Callable[[float], None] = time.sleep  # the suites make it instant

# `boot-run-args` may hold only these: what the fleet's container-release calls
# passed, and their near kin. Each changes the container and nothing outside it;
# a host path, a host namespace, --privileged or a device would reach the runner,
# and its socket. A volume is the checkout's own, mounted by its host path.
RUN_FLAGS = ("--read-only",)
RUN_VALUED: dict[str, Callable[[str], bool]] = {
    "-e": lambda v: ENV_LINE.fullmatch(v) is not None,
    "--env": lambda v: ENV_LINE.fullmatch(v) is not None,
    "--tmpfs": lambda v: TMPFS.fullmatch(v) is not None,
    "-u": lambda v: re.fullmatch(r"[\w.-]+(:[\w.-]+)?", v) is not None,
    "--user": lambda v: re.fullmatch(r"[\w.-]+(:[\w.-]+)?", v) is not None,
    "-w": lambda v: v.startswith("/"),
    "--workdir": lambda v: v.startswith("/"),
    "--entrypoint": bool,
    "--shm-size": lambda v: re.fullmatch(r"\d+[bkmg]?", v) is not None,
    "-v": lambda v: REPO_VOLUME.fullmatch(v) is not None and ".." not in v.split(":")[0],
    "--volume": lambda v: REPO_VOLUME.fullmatch(v) is not None and ".." not in v.split(":")[0],
}


@dataclass
class Boot:
    """One boot gate, checked: the profile and how to start the image for it."""

    profile: str
    env: list[str] = field(default_factory=list)
    run_args: list[str] = field(default_factory=list)
    cmd: list[str] = field(default_factory=list)
    units: list[str] = field(default_factory=list)
    port: int = 8080
    path: str = "/mcp"
    timeout: int = TIMEOUT
    reason: str = ""

    def args(self) -> list[str]:
        """podman run words: the dummy environment, then the repo's allowed extras."""
        return [w for kv in self.env for w in ("--env", kv)] + self.run_args


def text(s: dict, key: str) -> str:
    value = s.get(key, "")
    if not isinstance(value, str) or "\x00" in value:
        cannot(f"[tool.sdlc.image] {key} must be a string")
    return value


def whole(s: dict, key: str, default: int, top: int) -> int:
    value = s.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= top:
        cannot(f"[tool.sdlc.image] {key} must be a whole number from 1 to {top}")
    return value


def boot_settings(s: dict) -> Boot | None:
    """`[tool.sdlc.image]`'s boot keys as a Boot, None when there is no `boot`; or a 2."""
    if "boot" not in s:
        stray = sorted(k for k in s if k.startswith("boot-"))
        if stray:
            cannot(f"[tool.sdlc.image] has {', '.join(stray)} but no boot profile")
        return None
    profile = s["boot"]
    if profile not in PROFILES:
        cannot(f"[tool.sdlc.image] boot must be one of {', '.join(PROFILES)}; got {profile!r}")
    env = [kv.strip() for kv in text(s, "boot-env").splitlines() if kv.strip()]
    for kv in env:
        if not ENV_LINE.fullmatch(kv):
            cannot(f"[tool.sdlc.image] boot-env line {kv.split('=')[0]!r} is not KEY=VALUE")
    run_args, cmd = words(s, "boot-run-args"), words(s, "boot-cmd")
    check_run_args(run_args)
    units = text(s, "boot-units").split()
    if bad := [u for u in units if not UNIT.fullmatch(u) or u.startswith("-")]:
        cannot(f"[tool.sdlc.image] boot-units has {bad[0]!r}, which is no unit name")
    path = text(s, "boot-path") or "/mcp"
    if not path.startswith("/") or any(c.isspace() for c in path):
        cannot("[tool.sdlc.image] boot-path must be a URL path starting with /")
    reason = text(s, "boot-none-reason").strip()
    if profile == "none" and not reason:
        cannot('[tool.sdlc.image] boot = "none" needs boot-none-reason')
    if profile == "command" and not text(s, "boot-command").strip():
        cannot('[tool.sdlc.image] boot = "command" needs boot-command')
    return Boot(
        profile,
        env,
        run_args,
        cmd,
        units,
        whole(s, "boot-port", 8080, PORT_MAX),
        path,
        whole(s, "boot-timeout", TIMEOUT, TIMEOUT_MAX),
        reason,
    )


def words(s: dict, key: str) -> list[str]:
    try:
        return shlex.split(text(s, key))
    except ValueError as exc:
        cannot(f"[tool.sdlc.image] {key} does not split: {exc}")


def check_run_args(words: list[str]) -> None:
    """`--opt value` and `--opt=value` alike; anything not in RUN_FLAGS/RUN_VALUED is a 2."""
    i = 0
    while i < len(words):
        opt, eq, value = words[i].partition("=")
        i += 1
        if opt in RUN_FLAGS and not eq:
            continue
        if opt in RUN_VALUED:
            if not eq and i < len(words):
                value, i = words[i], i + 1
            if (eq or value) and RUN_VALUED[opt](value):
                continue
        cannot(
            f"[tool.sdlc.image] boot-run-args may not hold {words[i - 1]!r}: "
            f"it takes only {', '.join([*RUN_FLAGS, *RUN_VALUED])}"
        )


# ------------------------------------------------------------------ the gate


def boot_gate(job: Job, spec: Boot, image: str) -> bool:
    """Run the profile against the built `image`. True passed (or `none`), False a finding."""
    step(f"boot ({spec.profile})")
    if spec.profile == "none":
        print(f"boot: SKIPPED: {spec.reason}", flush=True)
        return True
    if spec.profile == "command":
        print('boot: NOT ENFORCED: boot = "command" is shell (crunchtools/sdlc#118)')
        return True
    spec.run_args = [on_host(job, w) for w in spec.run_args]
    finding = PROFILE_RUN[spec.profile](job, spec, image)
    if finding:
        print(f"boot: REFUSED: {finding}", flush=True)
        return False
    return True


def on_host(job: Job, word: str) -> str:
    """A checked `./path:...` volume word, `-v=` form too, with the checkout's host path."""
    opt, eq, value = word.partition("=")
    if eq and opt in ("-v", "--volume"):
        return f"{opt}={on_host(job, value)}"
    if not word.startswith("./"):
        return word
    return f"{job.host_root}/{word[2:]}"


def initialize() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "sdlc-boot-gate", "version": "1"},
        },
    }


def judge_tools(init: object, listed: object) -> str:
    """'' when initialize succeeded and tools/list named at least one tool; else why not."""
    if not isinstance(init, dict) or not isinstance(init.get("result"), dict):
        return f"initialize failed: {json.dumps(init)[:500]}"
    result = listed.get("result") if isinstance(listed, dict) else None
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list) or not tools:
        return f"tools/list returned no tools: {json.dumps(listed)[:500]}"
    server = init["result"].get("serverInfo") or {}
    print(f"  {server.get('name', '?')} {server.get('version', '?')}: {len(tools)} tools")
    return ""


def boot_mcp(job: Job, spec: Boot, image: str) -> str:
    """The server on stdio: initialize, notifications/initialized, tools/list."""
    argv = [*job.podman, "run", "--rm", "-i", "--pull=never", *spec.args(), image, *spec.cmd]
    msgs = [
        initialize(),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    replies, why = job.tools.converse(argv, msgs, timeout=spec.timeout, env=job.child_env)
    return why or judge_tools(replies.get(1), replies.get(2))


# Run inside the sdlc image, in the server container's network namespace: the
# server's 127.0.0.1 is reachable only from there. argv: port, path, timeout.
# Prints {"init": ..., "tools": ...} or {"error": ...}; the job judges.
HTTP_CLIENT = r"""
import http.client, json, sys, time
port, path, timeout = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
INIT = json.loads(sys.argv[4])
def post(body, sid):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if sid:
        h["Mcp-Session-Id"] = sid
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.request("POST", path, body=json.dumps(body), headers=h)
        r = c.getresponse()
        sid = r.getheader("Mcp-Session-Id") or sid
        text = r.read().decode()
    finally:
        c.close()
    if r.status >= 400:
        raise ValueError(f"{path} answered HTTP {r.status}: {text[:500]}")
    if "text/event-stream" in (r.getheader("Content-Type") or ""):
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:]), sid
        return None, sid
    return (json.loads(text) if text.strip() else None), sid
deadline = time.monotonic() + timeout
while True:
    try:
        first, sid = post(INIT, None)
        break
    except (OSError, http.client.HTTPException, ValueError) as exc:
        if time.monotonic() > deadline:
            print(json.dumps({"error": f"127.0.0.1:{port}{path} never answered initialize: {exc}"}))
            sys.exit(0)
        time.sleep(2)
try:
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    listed, _ = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid)
except (OSError, http.client.HTTPException, ValueError) as exc:
    print(json.dumps({"error": f"after initialize: {exc}"}))
    sys.exit(0)
print(json.dumps({"init": first, "tools": listed}))
"""


def boot_mcp_http(job: Job, spec: Boot, image: str) -> str:
    """The server detached; a client in its network namespace speaks streamable HTTP."""

    def check(name: str) -> str:
        proc = job.run(
            [
                *job.podman,
                "run",
                "--rm",
                "--pull=never",
                "--network",
                f"container:{name}",
                "--cap-drop=all",
                "--security-opt=no-new-privileges",
                "--entrypoint",
                "python3",
                job.image_id,
                "-I",
                "-c",
                HTTP_CLIENT,
                str(spec.port),
                spec.path,
                str(spec.timeout),
                json.dumps(initialize()),
            ]
        )
        try:
            got = json.loads(proc.stdout.strip().splitlines()[-1])
        except ValueError, IndexError:
            got = None
        if proc.returncode != 0 or not isinstance(got, dict):
            show(proc)
            cannot(f"boot: the MCP HTTP client could not run (exit {proc.returncode})")
        if "error" in got:
            return str(got["error"])
        return judge_tools(got.get("init"), got.get("tools"))

    return detached(job, spec, image, check)


def boot_systemd(job: Job, spec: Boot, image: str) -> str:
    """systemd as PID 1 reaches `running`, and every boot-unit is active."""

    def check(name: str) -> str:
        state = ""
        for _ in range(max(1, spec.timeout // POLL_SECONDS)):
            proc = job.run([*job.podman, "exec", name, "systemctl", "is-system-running"])
            state = proc.stdout.strip()
            if state in SETTLED:
                break
            sleep(POLL_SECONDS)
        if state != "running":
            failed = job.run([*job.podman, "exec", name, "systemctl", "--failed", "--no-legend"])
            shown = state or "unreachable"
            return f"systemd is {shown!r}, not running. Failed units:\n{failed.stdout}"
        inactive = [
            unit
            for unit in spec.units
            if job.run([*job.podman, "exec", name, "systemctl", "is-active", unit]).stdout.strip()
            != "active"
        ]
        if inactive:
            return f"units not active: {', '.join(inactive)}"
        print(f"  systemd running; {len(spec.units)} required unit(s) active")
        return ""

    return detached(job, spec, image, check, "--systemd=always")


def boot_healthcheck(job: Job, spec: Boot, image: str) -> str:
    """The image's own HEALTHCHECK passes within boot-timeout."""
    declared = job.run(
        [*job.podman, "image", "inspect", "--format", "{{json .Config.Healthcheck}}", image]
    ).stdout.strip()
    if declared in ("", "null", "<nil>", "{}"):
        return (
            "boot: healthcheck, but the image carries no HEALTHCHECK "
            '(buildah drops it in OCI format; build with format = "docker")'
        )

    def check(name: str) -> str:
        for _ in range(max(1, spec.timeout // POLL_SECONDS)):
            if job.run([*job.podman, "healthcheck", "run", name]).returncode == 0:
                print("  healthy")
                return ""
            sleep(POLL_SECONDS)
        return f"not healthy within {spec.timeout}s"

    return detached(job, spec, image, check)


def detached(job: Job, spec: Boot, image: str, check: Callable[[str], str], *extra: str) -> str:
    """Start the image detached, run `check(name)`, show its log on a finding, remove it."""
    name = f"sdlc-boot-{job.volume}"
    started = job.run(
        [
            *job.podman,
            "run",
            "-d",
            "--name",
            name,
            "--pull=never",
            *extra,
            *spec.args(),
            image,
            *spec.cmd,
        ]
    )
    try:
        if started.returncode != 0:
            show(started)
            return "the image could not be started"
        finding = check(name)
        if finding:
            logs = job.run([*job.podman, "logs", name])
            print((logs.stdout + logs.stderr)[-LOG_TAIL:], flush=True)
        return finding
    finally:
        job.run([*job.podman, "rm", "-f", "-v", name])


PROFILE_RUN: dict[str, Callable[[Job, Boot, str], str]] = {
    "mcp": boot_mcp,
    "mcp-http": boot_mcp_http,
    "systemd": boot_systemd,
    "healthcheck": boot_healthcheck,
}
