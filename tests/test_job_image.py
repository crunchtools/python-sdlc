#!/usr/bin/env python3
"""`sdlc_jobs/image.py` must verify the parent, build FROM its digest, gate, then push.

The job holds the registry secrets and the runner's socket, so what it refuses matters
more than what it does: a pull request, a parent it cannot verify, a registry it does
not trust, a gate that found something. A fake `Tools` records each argv and answers from
a script; nothing here runs podman, skopeo or cosign.

Run: tests/test_job_image.py   (needs python3 only)
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
from typing import Unpack

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdlc_jobs import _boot, _core, image
from sdlc_jobs._core import CannotRunError, Response, RunOptions, Tools

REPO = "crunchtools/demo"
SHA = "0123456789abcdef0123456789abcdef01234567"
PARENT = "sha256:" + "a" * 64
PUSHED = "sha256:" + "b" * 64
TREE = "c" * 40
OWN = "quay.io/crunchtools/demo"
US = "\x1f"
HOST = "/host/ws"
INSPECT_ME = 2  # `inspect <container id>`, not a state query
TAG = "localhost/demo:sdlc"  # what the plan's container-build tags, and the boot gate runs
BOOT = "sdlc-bootstrap-sdlc-cache-7-1"  # the boot container: sdlc-bootstrap-<the job's volume>
_boot.sleep = lambda _seconds: None  # the boot gate polls; the tests do not wait
INIT_OK = {"result": {"serverInfo": {"name": "demo", "version": "1"}}}
TOOLS_OK = {"result": {"tools": [{"name": "t"}]}}


class Link(str):
    """A `files` value that go() commits as a symlink to it, not as a file's text."""

    __slots__ = ()


def cp(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, out, err)


def plan_text(*, bootc: bool = False) -> str:
    rows = [
        (
            "container-build",
            "host",
            ["podman", "build", "-f", "Containerfile", "-t", "localhost/demo:sdlc", "."],
        ),
        (
            "container-scan",
            "host",
            [
                *["podman", "run", "--rm", "--mount"],
                "type=image,source=localhost/demo:sdlc,destination=/scan,rw=false",
                *["-v", "sdlc-cache-7-1:/cache", "-v", f"{HOST}:/repo:ro,z"],
                *["--", "@sdlc-image", "scan-rootfs"],
            ],
        ),
        (
            "container-smoke",
            "host-up-advisory",
            ["1", "podman", "run", "--pull=never", "--", "localhost/demo:sdlc"],
        ),
    ]
    if bootc:
        rows.append(
            (
                "bootc-lint",
                "host",
                [
                    "podman",
                    "run",
                    "--rm",
                    "--entrypoint",
                    "bootc",
                    "--",
                    "localhost/demo:sdlc",
                    "container",
                    "lint",
                ],
            )
        )
    return "".join(f"{n}\tmain\t{m}\t{US.join(a)}\n" for n, m, a in rows)


class FakeTools(Tools):
    def __init__(self) -> None:
        self.argvs: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.verify_rc = 0
        self.signed_tag = PARENT  # what a hi/<name>:<tag> resolves to
        self.scan_rc = 0
        self.smoke_up = "true"
        self.build_rc = 0
        self.bootc = False
        self.lint_rc = 0  # what `sdlc containerfile-lint` exits
        self.policy: dict | None = None
        self.built_file = ""
        self.plan = ""  # the plan `sdlc plan ci` prints; empty is the stock one
        self.ci_skip: set[str] = set()  # checks `sdlc plan ci` leaves out; by name, planned
        self.stdin: list[str | None] = []
        self.digest: str | None = PUSHED  # what `podman push --digestfile` writes; None, none
        self.replies: dict[int, dict] = {1: INIT_OK, 2: TOOLS_OK}  # what the MCP server says
        self.why = ""  # what `converse` says went wrong; empty, nothing
        self.server_rc: int | None = None  # the stdio server's own exit; None, killed
        self.timeouts: list[float | None] = []  # each run's timeout, in argvs' order
        self.conversed: list[tuple[list[str], list[dict], dict]] = []
        self.boot_rc = 0  # what `podman run -d --name sdlc-bootstrap-...` exits
        self.healthcheck = '{"Test": ["CMD", "true"]}'  # the image's .Config.Healthcheck
        self.health_rc = 0  # what `podman healthcheck run` exits
        self.systemd = "running"  # what `systemctl is-system-running` prints
        self.systemd_first: list[str] = []  # printed before `systemd`, one per call
        self.inactive: set[str] = set()  # units `systemctl is-active` says are not
        self.http_rc = 0  # what the mcp-http client container exits
        self.http_out = json.dumps({"init": INIT_OK, "tools": TOOLS_OK})
        self.exec_rc = 0  # what `podman exec` itself exits; 125, podman failed
        self.boot_running = "true"  # `inspect {{.State.Running}}` of the boot container
        self.boot_inspect_rc = 0  # what that inspect exits; non-zero, the engine is gone
        self.rm_rc: dict[str, int] = {}  # container name -> what `podman rm -f -v` exits
        self.image_inspect_rc = 0  # what `podman image inspect` exits
        self.log = ""  # what the job printed, kept by go()
        self.landed = ""  # what gated-<sha> resolves to after a push
        self.http_calls: list[str] = []
        self.apparmor = ""  # what `podman info` says of .Host.Security.AppArmorEnabled

    def run(self, argv: list[str], **kw: Unpack[RunOptions]) -> subprocess.CompletedProcess[str]:
        done = self._answer(argv, **kw)
        if kw.get("stream"):  # as the real Tools: a streamed run's output went to the log
            return subprocess.CompletedProcess[str](argv, done.returncode)  # stdout, stderr: None
        return done

    def _answer(
        self, argv: list[str], **kw: Unpack[RunOptions]
    ) -> subprocess.CompletedProcess[str]:
        self.argvs.append(argv)
        self.timeouts.append(kw.get("timeout"))
        self.stdin.append(kw.get("input"))
        self.envs.append(dict(kw.get("env") or {}))
        cwd = kw.get("cwd")
        if argv[0] == "skopeo":
            return self.skopeo(argv)
        if argv[0] == "cosign":
            return cp(self.verify_rc)
        if argv[:2] == ["git", "rev-parse"]:
            return cp(0, TREE + "\n")
        if argv[:2] == ["sdlc", "plan"]:
            rows = (self.plan or plan_text(bootc=self.bootc)).splitlines(keepends=True)
            keep = (
                (lambda name: name not in self.ci_skip)
                if argv[2] == "ci"
                else (lambda name: name == argv[2])
            )
            return cp(0, "".join(r for r in rows if keep(r.split("\t", 1)[0])))
        if argv[0] == "sdlc":
            return cp(self.lint_rc if argv[1:] == ["containerfile-lint"] else 0)
        assert argv[:3] == ["podman", "--remote", "--url"], argv
        return self.podman(argv[4:], cwd or ".")

    def converse(
        self,
        argv: list[str],
        messages: list[dict],
        *,
        timeout: int,
        env: Mapping[str, str] | None = None,
    ) -> tuple[dict[int, dict], str, int | None]:
        self.argvs.append(argv)
        self.timeouts.append(None)
        self.conversed.append((argv, messages, {"timeout": timeout, "env": dict(env or {})}))
        return self.replies, self.why, self.server_rc

    def podman(self, sub: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
        rc, out = 0, ""
        up = self.smoke_up == "true"
        match sub[0]:
            case "inspect" if len(sub) == INSPECT_ME:
                mounts = [{"Destination": cwd, "Source": HOST}]
                out = json.dumps([{"Mounts": mounts, "Image": "imgid"}])
            case "build":
                self.built_file = (Path(cwd) / sub[sub.index("-f") + 1]).read_text()
                rc = self.build_rc
            case "inspect" if sub[-1].startswith("sdlc-bootstrap-"):
                return self.podman_boot(sub)
            case "inspect":
                state_query = "ExitCode" in sub[-2]
                out = (
                    ("true 0 false" if up else "false 1 false") if state_query else str(up).lower()
                )
            case "info":
                out = self.apparmor + "\n"
            case "push":
                if self.digest is not None:
                    Path(sub[sub.index("--digestfile") + 1]).write_text(self.digest + "\n")
            case "run" | "exec" | "healthcheck" | "image" | "rm":
                return self.podman_boot(sub)
        return cp(rc, out)

    def podman_boot(self, sub: list[str]) -> subprocess.CompletedProcess[str]:
        """`run` (a gate, or the boot gate's server and client), `exec`, `healthcheck`,
        `image inspect`, `rm` and the boot container's `inspect`."""
        rc, out = 0, ""
        match sub[0]:
            case "image":
                rc = self.image_inspect_rc
                out = self.healthcheck if "{{json .Config.Healthcheck}}" in sub else "{}"
            case "inspect":
                rc, out = self.boot_inspect_rc, self.boot_running
            case "rm" if self.rm_rc.get(sub[-1]):
                return cp(self.rm_rc[sub[-1]], "", f"Error: {sub[-1]} has dependent containers")
            case "run":
                return self.podman_run(sub)
            case "exec" if self.exec_rc:
                rc = self.exec_rc
            case "exec" if sub[3:] == ["is-system-running"]:
                out = (self.systemd_first.pop(0) if self.systemd_first else self.systemd) + "\n"
            case "exec" if sub[3:4] == ["is-active"]:
                out = "inactive\n" if sub[4] in self.inactive else "active\n"
            case "healthcheck":
                rc = self.health_rc
        return cp(rc, out)

    def podman_run(self, sub: list[str]) -> subprocess.CompletedProcess[str]:
        """A gate's `run`, or the boot gate's server and its mcp-http client."""
        if "python3" in sub:  # the mcp-http client, in the server's namespace
            return cp(self.http_rc, self.http_out)
        if "--name" in sub and sub[sub.index("--name") + 1].startswith("sdlc-bootstrap-"):
            return cp(self.boot_rc)
        return cp(self.scan_rc if "-d" not in sub else 0)

    def skopeo(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        rc, out = 0, ""
        match argv[1]:
            case "--policy":
                self.policy = json.loads(Path(argv[2]).read_text())
                rc = self.verify_rc
            case "inspect" if f"{OWN}:gated-" in argv[-1] or f"{OWN}-base:gated-" in argv[-1]:
                out = self.landed
            case "inspect":
                out = self.signed_tag if "registry.access.redhat.com/hi/" in argv[-1] else PARENT
        return cp(rc, out)

    def http(
        self, method: str, url: str, headers: Mapping[str, str], payload: bytes | None = None
    ) -> Response:
        del method, headers, payload
        self.http_calls.append(url)
        raise AssertionError(f"image called the API: {url}")

    def pushed(self) -> bool:
        return any(a[4:5] == ["push"] for a in self.argvs)

    def built(self) -> bool:
        return any(a[4:5] == ["build"] for a in self.argvs)

    def boot_calls(self) -> list[list[str]]:
        """The podman words (after --remote --url <url>) of what named the boot container."""
        return [a[4:] for a in self.argvs if BOOT in a or any(BOOT in w for w in a)]

    def boot_start(self) -> list[str]:
        """The `podman run -d` that started the boot container (not the smoke one)."""
        return next(a for a in self.argvs if a[4:5] == ["run"] and BOOT in a)

    def build_argv(self) -> list[str]:
        return next(a for a in self.argvs if a[4:5] == ["build"])


def go(
    tools: FakeTools,
    containerfile: str,
    *,
    event: str = "push",
    extra: dict[str, str] | None = None,
    files: dict[str, str] | None = None,
) -> tuple[int, dict | None]:
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        ws.mkdir()
        (ws / ".git").mkdir()  # a checked-out tree: fetch_tree leaves it alone
        (ws / "Containerfile").write_text(containerfile)
        for name, text in (files or {}).items():
            (ws / name).parent.mkdir(parents=True, exist_ok=True)
            if isinstance(text, Link):  # a symlink committed to the repo
                (ws / name).symlink_to(text)
            else:
                (ws / name).write_text(text)
        out = Path(tmp) / "out"
        env = {
            "GITHUB_WORKSPACE": str(ws),
            "GITHUB_OUTPUT": str(out),
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_SHA": SHA,
            "GITHUB_RUN_ID": "7",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/ci.yml@refs/heads/main",
            "HOSTNAME": "cid",
            "RUNNER_TEMP": tmp,
            "QUAY_USERNAME": "robot",
            "QUAY_PASSWORD": "pw",
            **(extra or {}),
        }
        log = io.StringIO()
        try:
            with contextlib.redirect_stdout(log):
                rc = image.run(event, {}, env, tools=tools)
        finally:
            tools.log = log.getvalue()
        text = out.read_text() if out.exists() else ""
        result = json.loads(text.split("\n")[1]) if text else None
        return rc, result


QUAY_PARENT = "FROM quay.io/crunchtools/ubi10-core:latest\nRUN true\n"
RH_PARENT = "FROM registry.redhat.io/ubi10/ubi-minimal:10.1\nRUN true\n"


def refused(
    tools: FakeTools,
    containerfile: str,
    *,
    event: str = "push",
    files: dict[str, str] | None = None,
    extra: dict[str, str] | None = None,
) -> str:
    """The message of the refusal (exit 2) `image.run` raises, or a failed assertion."""
    try:
        go(tools, containerfile, event=event, files=files, extra=extra)
    except CannotRunError as exc:
        assert not tools.pushed(), "something was pushed after a refusal"
        return str(exc)
    raise AssertionError("expected a refusal (exit 2)")


def test_image_checks_ci_leaves_out_are_still_run_here() -> None:
    names = ("container-build", "container-smoke", "container-scan")
    # An RHSM build skips all three in ci; a python repo with no stacks plans none.
    t = FakeTools()
    t.ci_skip = set(names)
    rc, _ = go(t, QUAY_PARENT)
    assert rc == 0 and t.built() and t.pushed(), t.log
    for name in names:
        assert ["sdlc", "plan", name] in t.argvs, name
    assert any("scan-rootfs" in a for a in t.argvs), "the scan ran"


def test_a_bootc_image_ci_leaves_out_is_still_linted_here() -> None:
    t = FakeTools()
    t.bootc = True
    t.ci_skip = {"container-build", "container-scan", "bootc-lint"}  # an RHSM bootc build
    rc, _ = go(t, "FROM quay.io/crunchtools/rhel10-bootc:latest\nRUN true\n")
    assert rc == 0 and t.pushed(), t.log
    assert ["sdlc", "plan", "bootc-lint"] in t.argvs
    assert ["sdlc", "plan", "container-smoke"] not in t.argvs
    assert t.build_argv()[t.build_argv().index("--format") + 1] == "oci"
    assert any("lint" in a for a in t.argvs if a[4:5] == ["run"])


def test_a_container_image_never_plans_bootc_lint() -> None:
    t = FakeTools()
    t.ci_skip = {"container-build", "container-smoke", "container-scan"}
    rc, _ = go(t, QUAY_PARENT)
    assert rc == 0, t.log
    assert ["sdlc", "plan", "bootc-lint"] not in t.argvs


def test_an_image_check_ci_plans_is_not_planned_again() -> None:
    t = FakeTools()
    rc, _ = go(t, QUAY_PARENT)
    assert rc == 0, t.log
    assert not any(
        a[:2] == ["sdlc", "plan"] and a[2] in ("container-build", "container-smoke")
        for a in t.argvs
    )


def test_a_scan_finding_on_a_check_ci_left_out_stops_the_push() -> None:
    t = FakeTools()
    t.ci_skip = {"container-build", "container-smoke", "container-scan"}
    t.scan_rc = 1
    rc, _ = go(t, QUAY_PARENT)
    assert rc != 0 and not t.pushed(), t.log


def test_only_the_workflow_events_run() -> None:
    for event in ("pull_request", "pull_request_target", "issues", "repository_dispatch", ""):
        t = FakeTools()
        assert "does not run on" in refused(t, QUAY_PARENT, event=event)
        assert not t.argvs, f"{event!r} ran something"
    for event in image.EVENTS:
        assert go(FakeTools(), QUAY_PARENT, event=event)[0] == 0, event
    assert image.EVENTS == ("push", "schedule", "workflow_dispatch")


def test_crunchtools_parent_verified() -> None:
    t = FakeTools()
    rc, _ = go(t, QUAY_PARENT)
    assert rc == 0
    verify = next(a for a in t.argvs if a[:2] == ["cosign", "verify"])
    pinned = f"quay.io/crunchtools/ubi10-core@{PARENT}"
    assert verify[-1] == pinned and "--insecure-ignore-tlog" in verify
    assert verify[verify.index("-a") + 1] == "repo=crunchtools/ubi10-core"


def test_red_hat_parent_verified() -> None:
    t = FakeTools()
    rc, _ = go(t, RH_PARENT)
    assert rc == 0 and t.policy is not None
    entry = t.policy["transports"]["docker"]["registry.redhat.io"][0]
    assert entry["type"] == "sigstoreSigned" and entry["keyPath"] == image.RH_KEY
    assert not any(a[0] == "cosign" for a in t.argvs)
    pull = next(a for a in t.argvs if a[1:2] == ["--policy"])
    assert pull[-2] == f"docker://registry.redhat.io/ubi10/ubi-minimal@{PARENT}"


def test_hummingbird_mirror_verified_at_red_hat() -> None:
    """quay.io/hummingbird is unsigned; the same digest under hi/ carries Red Hat's key."""
    t = FakeTools()
    cf = (
        f"FROM quay.io/hummingbird/python:3.14-builder@{PARENT} AS b\nRUN true\n"
        "FROM quay.io/hummingbird/python:3.14\nCOPY --from=b /a /a\n"
    )
    rc, result = go(t, cf)
    assert rc == 0 and result is not None and t.policy is not None
    entry = t.policy["transports"]["docker"]["registry.access.redhat.com"][0]
    assert entry["type"] == "sigstoreSigned" and entry["keyPath"] == image.RH_KEY
    assert "quay.io" not in t.policy["transports"]["docker"]
    pulled = [a[-2] for a in t.argvs if a[1:2] == ["--policy"]]
    assert pulled == [f"docker://registry.access.redhat.com/hi/python@{PARENT}"] * 2
    assert not any(a[0] == "cosign" for a in t.argvs)
    lines = t.built_file.splitlines()
    assert lines[0] == f"FROM quay.io/hummingbird/python@{PARENT} AS b"
    assert lines[2] == f"FROM quay.io/hummingbird/python@{PARENT}"
    assert result["parent"] == f"quay.io/hummingbird/python@{PARENT}"
    inspected = [a[-1] for a in t.argvs if a[:2] == ["skopeo", "inspect"]]
    assert f"docker://quay.io/hummingbird/python@{PARENT}" in inspected  # no tag: skopeo refuses
    # the tag-only FROM is resolved at hi/ as well; the digest-pinned one is not
    assert "docker://registry.access.redhat.com/hi/python:3.14" in inspected
    assert not any(
        i.startswith("docker://registry.access.redhat.com/hi/python:3.14-") for i in inspected
    )
    assert t.policy["default"] == [{"type": "reject"}]


def test_hummingbird_mirror_tag_must_match_hi() -> None:
    """A mirror tag that names a different digest than hi/ does is a rollback: refused."""
    t = FakeTools()
    t.signed_tag = "sha256:" + "c" * 64
    msg = refused(t, "FROM quay.io/hummingbird/python:3.14-builder\nRUN true\n")
    assert "registry.access.redhat.com/hi/python:3.14-builder" in msg, msg
    assert not any(a[1:2] == ["--policy"] for a in t.argvs)
    t = FakeTools()
    t.signed_tag = "sha256:" + "c" * 64
    refused(t, "FROM quay.io/hummingbird/core-runtime\nRUN true\n")
    assert "docker://registry.access.redhat.com/hi/core-runtime:latest" in [a[-1] for a in t.argvs]


def test_hummingbird_unverified_pushes_nothing() -> None:
    t = FakeTools()
    t.verify_rc = 1
    refused(t, "FROM quay.io/hummingbird/python:3.14-builder\nRUN true\n")
    assert not t.built()


def test_unknown_registry_refused() -> None:
    for parent in ("FROM docker.io/library/alpine:3\n", "FROM ghcr.io/x/y:1\n"):
        t = FakeTools()
        refused(t, parent)
        assert not t.built()


def test_verification_failure_pushes_nothing() -> None:
    for parent in (QUAY_PARENT, RH_PARENT):
        t = FakeTools()
        t.verify_rc = 1
        refused(t, parent)
        assert not t.built()


def test_build_pinned_to_digest() -> None:
    t = FakeTools()
    go(t, QUAY_PARENT)
    argv = t.build_argv()
    assert argv[argv.index("--from") + 1] == f"quay.io/crunchtools/ubi10-core@{PARENT}"
    assert f"org.opencontainers.image.base.digest={PARENT}" in argv
    assert "org.opencontainers.image.base.name=quay.io/crunchtools/ubi10-core:latest" in argv
    assert f"org.opencontainers.image.revision={SHA}" in argv
    assert argv[argv.index("-t") + 1] == "localhost/demo:sdlc"
    assert argv[argv.index("--format") + 1] == "docker"


def test_final_stage_not_first_is_rewritten() -> None:
    t = FakeTools()
    cf = (
        "FROM quay.io/crunchtools/builder:1 AS b\nRUN true\n"
        "FROM quay.io/crunchtools/ubi10-core:latest\nCOPY --from=b /a /a\n"
    )
    go(t, cf)
    argv = t.build_argv()
    assert "--from" not in argv
    lines = t.built_file.splitlines()
    assert lines[2] == f"FROM quay.io/crunchtools/ubi10-core@{PARENT}"


def test_a_continued_line_that_starts_with_from_is_not_rewritten() -> None:
    t = FakeTools()
    cf = (
        "FROM quay.io/crunchtools/builder:1 AS b\n"
        'RUN python -c "\\\nfrom tools.deps import missing; \\\nprint(missing)"\n'
        "FROM quay.io/crunchtools/ubi10-core:latest\nCOPY --from=b /a /a\n"
    )
    go(t, cf)
    lines = t.built_file.splitlines()
    assert lines[2] == "from tools.deps import missing; \\"
    assert lines[4] == f"FROM quay.io/crunchtools/ubi10-core@{PARENT}"


def test_builder_stage_is_verified_and_pinned() -> None:
    t = FakeTools()
    cf = (
        "ARG V=1\nFROM --platform=linux/amd64 registry.redhat.io/ubi10/go:${V} AS b\nRUN true\n"
        "FROM b AS c\nFROM scratch AS s\n"
        "FROM quay.io/crunchtools/ubi10-core:latest\nCOPY --from=b /a /a\n"
    )
    rc, result = go(t, cf)
    lines = t.built_file.splitlines()
    assert lines[1] == f"FROM --platform=linux/amd64 registry.redhat.io/ubi10/go@{PARENT} AS b"
    assert lines[5] == f"FROM quay.io/crunchtools/ubi10-core@{PARENT}"
    assert lines[3] == "FROM b AS c" and lines[4] == "FROM scratch AS s"
    assert "--from" not in t.build_argv()
    pulled = [a[-2] for a in t.argvs if a[1:2] == ["--policy"]]
    assert pulled == [f"docker://registry.redhat.io/ubi10/go@{PARENT}"]
    assert any(a[:2] == ["cosign", "verify"] for a in t.argvs)
    assert rc == 0 and result is not None and result["parent"].startswith("quay.io/crunchtools/")


def test_builder_stage_on_an_untrusted_registry_refused() -> None:
    t = FakeTools()
    cf = "FROM docker.io/library/golang:1 AS b\nRUN true\nFROM scratch\nCOPY --from=b /a /a\n"
    assert refused(t, cf) == (
        "the parent docker.io/library/golang:1 is on docker.io, "
        "which this job does not trust: refused"
    )
    assert not t.built()
    for ref in (
        "quay.io/hummingbird-community/minio:1",
        "quay.io/hummingbird/x/go:1",
        "quay.io/other/go:1",
    ):
        t = FakeTools()
        assert refused(t, f"FROM {ref} AS b\nFROM scratch\n") == (
            f"the parent {ref} is on quay.io, which this job does not trust: refused"
        )
        assert not t.built()


def test_stage_alias_followed_to_registry_parent() -> None:
    t = FakeTools()
    go(t, "FROM quay.io/crunchtools/ubi10-core:latest AS base\nFROM base\nRUN true\n")
    assert t.build_argv()[t.build_argv().index("--from") + 1].endswith(f"@{PARENT}")


def test_rhsm_secrets_and_no_secret_env() -> None:
    t = FakeTools()
    go(
        t,
        RH_PARENT,
        extra={
            "RHSM_ACTIVATION_KEY": "k",
            "RHSM_ORG_ID": "o",
            "REGISTRY_REDHAT_IO_USER": "u",
            "REGISTRY_REDHAT_IO_PASSWORD": "p",
        },
    )
    ids = [w.split(",")[0] for w in t.build_argv() if w.startswith("id=")]
    assert ids == ["id=activation_key", "id=org_id", "id=RHSM_ACTIVATION_KEY", "id=RHSM_ORG_ID"]
    assert any(a[:2] == ["skopeo", "login"] and a[-1] == "registry.redhat.io" for a in t.argvs)
    for env in t.envs:
        assert not {"QUAY_PASSWORD", "RHSM_ACTIVATION_KEY", "REGISTRY_REDHAT_IO_PASSWORD"} & set(
            env
        )


def test_bootc_builds_oci() -> None:
    t = FakeTools()
    t.bootc = True
    go(t, QUAY_PARENT)
    assert t.build_argv()[t.build_argv().index("--format") + 1] == "oci"
    assert any("lint" in a for a in t.argvs if a[4:5] == ["run"])


def test_gate_failure_pushes_nothing() -> None:
    t = FakeTools()
    t.scan_rc = 1
    rc, result = go(t, QUAY_PARENT)
    assert rc == 1 and result is None and not t.pushed()


def test_advisory_smoke_does_not_gate() -> None:
    t = FakeTools()
    t.smoke_up = "crash"
    rc, result = go(t, QUAY_PARENT)
    assert rc == 0 and result is not None


def test_gate_words_are_held() -> None:
    t = FakeTools()
    t.plan = plan_text().replace("--rm", "--rm\x1f--privileged", 1)
    msg = refused(t, QUAY_PARENT)
    assert "--privileged" in msg, msg


def test_output_shape() -> None:
    t = FakeTools()
    rc, result = go(t, QUAY_PARENT)
    assert rc == 0
    assert result == {
        "ref": "quay.io/crunchtools/demo",
        "digest": PUSHED,
        "sha": SHA,
        "tag": f"gated-{SHA}",
        "parent": f"quay.io/crunchtools/ubi10-core@{PARENT}",
    }
    push = next(a for a in t.argvs if a[4:5] == ["push"])
    assert push[-1] == f"docker://quay.io/crunchtools/demo:gated-{SHA}"
    assert "--digestfile" in push
    assert not any(a[0] == "skopeo" and a[1] == "inspect" and ":gated-" in a[-1] for a in t.argvs)
    assert t.argvs.index(push) > max(i for i, a in enumerate(t.argvs) if a[4:5] == ["run"])


def test_pushed_digest_missing_or_malformed_refused() -> None:
    for digest in (None, "", "sha256:abc", "latest", PUSHED.upper()):
        t = FakeTools()
        t.digest = digest
        msg = ""
        try:
            go(t, QUAY_PARENT)
        except CannotRunError as exc:
            msg = str(exc)
        assert "not a sha256 digest" in msg, (digest, msg)


def test_scratch_has_no_parent() -> None:
    t = FakeTools()
    rc, result = go(t, "FROM scratch\nCOPY a /a\n")
    assert rc == 0 and result is not None and result["parent"] == ""
    assert "--from" not in t.build_argv()
    assert not any(a[0] == "cosign" for a in t.argvs)


def gate_refused(*words: str, drop: tuple[str, str] | None = None) -> str:
    """A gate step with `words` after --rm (or `drop` swapped in) must be refused, unrun.

    Returns the refusal, which names the option it refused."""
    t = FakeTools()
    text = plan_text()
    text = text.replace(*drop) if drop else text.replace("--rm", US.join(["--rm", *words]), 1)
    t.plan = text
    msg = refused(t, QUAY_PARENT)
    assert "refused podman option" in msg, msg
    gates = [a for a in t.argvs if a[4:5] == ["run"] and "-d" not in a]
    assert not gates, f"a gate ran after the refusal: {gates}"
    return msg


def test_gate_network_is_refused_unless_none() -> None:
    gate_refused("--network", "host")
    gate_refused("--net", "host")
    gate_refused("--network=host")
    gate_refused("--net=host")
    t = FakeTools()
    t.plan = plan_text().replace("--rm", US.join(["--rm", "--network", "none"]), 1)
    rc, _ = go(t, QUAY_PARENT)
    assert rc == 0


def test_gate_unlisted_options_are_refused() -> None:
    for words in (
        ["--volume=/:/host"],
        ["-v=/:/host"],
        ["--mount=type=bind,source=/,destination=/host"],
        ["--volumes-from", "x"],
        ["--security-opt", "label=disable"],
        ["--cgroupns=host"],
        ["--systemd=always"],
        ["--privileged"],
        ["--network=host"],
        ["--net=host"],
        ["--net", "none"],
        ["--cap-add=all"],
        ["--pull=always"],
        ["--rm=true"],
        ["stray"],
        ["-v"],
    ):
        assert "refused podman option" in gate_refused(*words), words


def test_gate_volume_outside_the_allowed_sources_is_refused() -> None:
    assert "'/etc:/etc:ro'" in gate_refused("-v", "/etc:/etc:ro")
    assert "'/:/host'" in gate_refused("--volume", "/:/host")
    assert "'/etc:/repo:ro,z'" in gate_refused(drop=(f"{HOST}:/repo:ro,z", "/etc:/repo:ro,z"))


def test_gate_mount_that_is_not_an_image_is_refused() -> None:
    assert "type=bind" in gate_refused("--mount", "type=bind,source=/,destination=/host")
    assert "type=volume" in gate_refused(drop=("type=image,", "type=volume,"))


def test_gate_allowed_mounts_pass() -> None:
    t = FakeTools()
    t.plan = plan_text().replace("--rm", US.join(["--rm", "-v", f"{HOST}:/x:ro,z"]), 1)
    rc, _ = go(t, QUAY_PARENT)
    assert rc == 0 and t.pushed()


def test_gate_planner_options_pass_in_both_spellings() -> None:
    t = FakeTools()
    extra = ["--read-only", "-w", "/repo", "-e=A=b", f"--volume={HOST}:/y:ro", "--tmpfs=/t"]
    t.plan = plan_text().replace("--rm", US.join(["--rm", *extra]), 1)
    rc, _ = go(t, QUAY_PARENT)
    assert rc == 0 and t.pushed()


class SmokeTools(Tools):
    """A container-smoke podman: the image's config, then how the container starts and ends."""

    def __init__(self, config: str, start_rc: int = 0, state: str = "false 0 false") -> None:
        self.argvs: list[list[str]] = []
        self.config, self.start_rc, self.state = config, start_rc, state

    def run(self, argv: list[str], **kw: Unpack[RunOptions]) -> subprocess.CompletedProcess[str]:
        del kw  # the smoke fake answers by argv alone
        self.argvs.append(argv)
        match argv[1]:
            case "image":
                return cp(0, self.config)
            case "run":
                return cp(self.start_rc)
            case "inspect" if "ExitCode" in argv[-2]:
                return cp(0, self.state)
            case "inspect":
                return cp(0, "false")
        return cp(0)

    def subs(self) -> list[str]:
        return [a[1] for a in self.argvs]


def smoke(t: SmokeTools, words: list[str] | None = None) -> bool:
    job = image.Job(
        tools=t,
        env={},
        root=Path("."),
        scratch=Path("."),
        name="demo",
        sha=SHA,
        podman=["podman"],
        auth=Path("auth"),
        volume="vol",
    )
    with contextlib.redirect_stdout(io.StringIO()):
        return image.stays_up(job, "container-smoke", [], words or ["--", "img"], 0)


def test_smoke_skips_a_systemd_pid_1() -> None:
    for config in ('{"Entrypoint": ["/sbin/init"]}', '{"Cmd": ["/usr/lib/systemd/systemd"]}'):
        t = SmokeTools(config)
        assert smoke(t) is True
        assert t.subs() == ["image"], "a systemd image was started"


def test_smoke_podman_cannot_create_is_refused() -> None:
    t = SmokeTools("{}", start_rc=125)
    try:
        smoke(t)
    except CannotRunError:
        assert t.argvs[-1][1:5] == ["rm", "-f", "-v", "sdlc-smoke-vol"]
        return
    raise AssertionError("exit 125 was judged, not refused")


def test_smoke_entrypoint_that_cannot_start_fails() -> None:
    t = SmokeTools("{}", start_rc=127)
    assert smoke(t) is False
    assert t.argvs[-1][1:5] == ["rm", "-f", "-v", "sdlc-smoke-vol"]


def test_smoke_exit_0_early_passes_and_is_cleaned_up() -> None:
    t = SmokeTools("{}", state="false 0 false")
    assert smoke(t) is True
    assert "run" in t.subs() and t.argvs[-1][1:5] == ["rm", "-f", "-v", "sdlc-smoke-vol"]
    assert smoke(SmokeTools("{}", state="false 1 false")) is False


def test_lint_that_could_not_parse_is_refused_not_passed() -> None:
    t = FakeTools()
    t.lint_rc = 4
    assert "nothing was judged" in refused(t, QUAY_PARENT)
    t = FakeTools()
    t.lint_rc = 1
    assert go(t, QUAY_PARENT)[0] == 1 and not t.pushed()


# ------------------------------------------------------------- [tool.sdlc.image]


def settings(body: str, name: str = "pyproject.toml") -> dict[str, str]:
    return {name: f"[tool.sdlc.image]\n{body}"}


def test_settings_image_is_where_it_pushes() -> None:
    t = FakeTools()
    _, result = go(t, QUAY_PARENT, files=settings('image = "quay.io/crunchtools/demo"\n'))
    assert result is not None and result["ref"] == "quay.io/crunchtools/demo"
    assert any(a[-1] == f"docker://quay.io/crunchtools/demo:gated-{SHA}" for a in t.argvs)


def test_settings_image_of_another_repo_refused() -> None:
    """The registry credentials are the org's: naming a sibling would push over it."""
    msg = refused(FakeTools(), QUAY_PARENT, files=settings('image = "quay.io/crunchtools/other"\n'))
    assert "quay.io/crunchtools/demo" in msg


def test_settings_in_sdlc_toml_are_read() -> None:
    files = settings('image = "quay.io/crunchtools/other"\n', "sdlc.toml")
    msg = refused(FakeTools(), QUAY_PARENT, files=files)
    assert "quay.io/crunchtools/demo" in msg


def test_settings_image_outside_crunchtools_refused() -> None:
    msg = refused(FakeTools(), QUAY_PARENT, files=settings('image = "docker.io/evil/x"\n'))
    assert "quay.io/crunchtools/demo" in msg


def test_settings_rhsm_needs_its_secrets() -> None:
    msg = refused(FakeTools(), QUAY_PARENT, files=settings("rhsm = true\n"))
    assert "rhsm" in msg
    go(
        FakeTools(),
        QUAY_PARENT,
        files=settings("rhsm = true\n"),
        extra={"RHSM_ACTIVATION_KEY": "k", "RHSM_ORG_ID": "o"},
    )


def test_settings_redhat_registry_needs_its_login() -> None:
    msg = refused(FakeTools(), QUAY_PARENT, files=settings("redhat-registry = true\n"))
    assert "REGISTRY_REDHAT_IO_USER" in msg


def test_settings_format_overrides_the_default() -> None:
    t = FakeTools()
    go(t, QUAY_PARENT, files=settings('format = "oci"\n'))
    assert t.build_argv()[t.build_argv().index("--format") + 1] == "oci"


def test_settings_refuses_what_it_cannot_honour() -> None:
    for body, said in (
        ('imagee = "x"\n', "imagee"),
        ('containerfile = "build/Containerfile"\n', "container-build"),
        ('context = "sub"\n', "container-build"),
        ('format = "tar"\n', "docker or oci"),
        ('rhsm = "yes"\n', "rhsm must be true or false"),
    ):
        msg = refused(FakeTools(), QUAY_PARENT, files=settings(body))
        assert said in msg, (body, msg)


def test_settings_boot_inputs_are_accepted() -> None:
    body = 'boot = "mcp"\nboot-env = "K=v\\n"\nboot-timeout = 240\n'
    t = FakeTools()
    rc, _ = go(t, QUAY_PARENT, files=settings(body))
    assert rc == 0 and t.conversed[0][2]["timeout"] == int("240")


def test_settings_contract_test_and_free_disk_are_accepted() -> None:
    t = FakeTools()
    body = 'contract-test = "pytest tests/contract"\nfree-disk = true\n'
    rc, _ = go(t, QUAY_PARENT, files=settings(body))
    assert rc == 0 and t.pushed()


# ------------------------------------------------------------------ the boot gate


def boot(profile: str, **keys: object) -> dict[str, str]:
    """pyproject.toml with `boot = profile` and the boot-* keys (`boot_env` is `boot-env`)."""
    body = f"boot = {json.dumps(profile)}\n"
    body += "".join(f"{k.replace('_', '-')} = {json.dumps(v)}\n" for k, v in keys.items())
    return settings(body)


def run_boot(
    t: FakeTools,
    profile: str,
    *,
    tree: dict[str, str] | None = None,
    **keys: object,
) -> int:
    files = {**boot(profile, **keys), **(tree or {})}
    return go(t, QUAY_PARENT, files=files)[0]


CONFINED = ["--cap-drop=all", "--security-opt=no-new-privileges", "--network=none"]


def boot_refused(profile: str, **keys: object) -> str:
    t = FakeTools()
    msg = refused(t, QUAY_PARENT, files=boot(profile, **keys))
    assert not t.argvs, "something ran before the settings were refused"
    return msg


def test_boot_absent_is_no_gate() -> None:
    t = FakeTools()
    assert go(t, QUAY_PARENT)[0] == 0
    assert not t.conversed and not t.boot_calls() and t.pushed()


def test_boot_mcp_passes_and_pushes() -> None:
    t = FakeTools()
    assert run_boot(t, "mcp") == 0 and t.pushed()
    argv, msgs, _ = t.conversed[0]
    assert [m.get("method") for m in msgs] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    assert t.argvs.index(argv) < t.argvs.index(next(a for a in t.argvs if a[4:5] == ["push"]))


def test_boot_mcp_argv_carries_env_run_args_and_cmd() -> None:
    t = FakeTools()
    keys = {
        "boot_env": "A=1\n\nB=two words\n",
        "boot_run_args": "--read-only -e C=3 --tmpfs /run",
        "boot_cmd": "serve --stdio",
        "boot_timeout": 30,
    }
    assert run_boot(t, "mcp", **keys) == 0
    argv, _, seen = t.conversed[0]
    i = argv.index(TAG)
    assert argv[4:10] == ["run", "--rm", "-i", "--name", BOOT, "--pull=never"]
    assert argv[10:13] == CONFINED
    assert argv[13:i] == [
        *["--env", "A=1", "--env", "B=two words"],
        *["--read-only", "-e", "C=3", "--tmpfs", "/run"],
    ]
    assert argv[i + 1 :] == ["serve", "--stdio"]
    assert seen["timeout"] == keys["boot_timeout"]
    assert "QUAY_PASSWORD" not in seen["env"], "the boot client was handed a secret"


def test_boot_mcp_findings_push_nothing() -> None:
    for replies, why in (
        ({}, ""),
        ({1: {"error": {"code": -32600}}, 2: TOOLS_OK}, ""),
        ({1: INIT_OK, 2: {"result": {"tools": []}}}, ""),
        ({1: INIT_OK}, ""),
        ({1: INIT_OK, 2: TOOLS_OK}, "no reply to tools/list within 180s"),
    ):
        t = FakeTools()
        t.replies, t.why = replies, why
        rc, result = go(t, QUAY_PARENT, files=boot("mcp"))
        assert rc == 1 and result is None and not t.pushed(), (replies, why)


def test_boot_mcp_http_sidecar_joins_the_servers_namespace() -> None:
    t = FakeTools()
    keys = {"boot_port": 9000, "boot_path": "/rpc", "boot_timeout": 60, "boot_cmd": "serve"}
    assert run_boot(t, "mcp-http", **keys) == 0 and t.pushed()
    start = t.boot_start()
    assert start[4:] == ["run", "-d", "--name", BOOT, "--pull=never", *CONFINED, TAG, "serve"]
    client = next(a for a in t.argvs if "python3" in a)
    assert client[client.index("--network") + 1] == f"container:{BOOT}"
    assert client[client.index("--entrypoint") + 1] == "python3"
    assert client[client.index("python3") + 1] == "imgid", "the client is not the job's image"
    assert TAG not in client
    assert client[-4:-1] == ["9000", "/rpc", "60"]
    assert json.loads(client[-1])["method"] == "initialize"
    assert "--cap-drop=all" in client and "--security-opt=no-new-privileges" in client
    assert t.argvs[-1][4:] == ["volume", "rm", "-f", "sdlc-cache-7-1"]
    assert ["rm", "-f", "-v", BOOT] in t.boot_calls()


def nnp_runs(t: FakeTools) -> list[list[str]]:
    return [a for a in t.argvs if a[4:5] == ["run"] and image.NNP in a]


def test_apparmor_engine_runs_gates_and_boot_without_no_new_privileges() -> None:
    """crunchtools/sdlc#181 for the image job: it drives the engine itself, so it asks."""
    for profile in ("mcp", "mcp-http", "healthcheck"):
        t = FakeTools()
        t.apparmor = "true"
        assert run_boot(t, profile) == 0 and t.pushed(), profile
        runs = [a for a in t.argvs if a[4:5] == ["run"]] + [a for a, _, _ in t.conversed]
        assert len(runs) > 1 and all("--cap-drop=all" in a for a in runs), profile
        assert not [a for a in runs if image.NNP in a], profile


def test_engine_without_apparmor_keeps_no_new_privileges() -> None:
    for answer in ("false", ""):
        t = FakeTools()
        t.apparmor = answer
        assert run_boot(t, "mcp-http") == 0
        runs = [a for a in t.argvs if a[4:5] == ["run"]]
        assert runs and nnp_runs(t) == runs, answer


def test_boot_mcp_http_defaults_to_8080_mcp() -> None:
    t = FakeTools()
    assert run_boot(t, "mcp-http") == 0
    client = next(a for a in t.argvs if "python3" in a)
    assert client[-4:-1] == ["8080", "/mcp", str(_boot.TIMEOUT)]


def test_boot_mcp_http_findings_push_nothing() -> None:
    for out in (
        json.dumps({"error": "127.0.0.1:8080/mcp never answered initialize"}),
        json.dumps({"init": INIT_OK, "tools": {"result": {"tools": []}}}),
        json.dumps({"init": None, "tools": None}),
    ):
        t = FakeTools()
        t.http_out = out
        assert run_boot(t, "mcp-http") == 1 and not t.pushed(), out
        assert ["rm", "-f", "-v", BOOT] in t.boot_calls()
        assert ["logs", BOOT] in t.boot_calls(), "a failed boot showed no log"


def test_boot_mcp_http_client_that_cannot_run_is_refused() -> None:
    for rc, out in ((1, ""), (0, "not json"), (0, "[1]")):
        t = FakeTools()
        t.http_rc, t.http_out = rc, out
        assert "could not run" in refused(t, QUAY_PARENT, files=boot("mcp-http")), (rc, out)
        assert ["rm", "-f", "-v", BOOT] in t.boot_calls()


def test_boot_systemd_passes_with_every_unit_active() -> None:
    t = FakeTools()
    assert run_boot(t, "systemd", boot_units="a.service\nb@x.service") == 0 and t.pushed()
    start = t.boot_start()
    assert start[4:] == ["run", "-d", "--name", BOOT, "--pull=never", *_boot.SYSTEMD_CONFINED, TAG]
    active = [a[-1] for a in t.argvs if a[4:5] == ["exec"] and "is-active" in a]
    assert active == ["a.service", "b@x.service"]


def test_boot_systemd_inactive_unit_is_a_finding() -> None:
    t = FakeTools()
    t.inactive = {"b.service"}
    rc, result = go(t, QUAY_PARENT, files=boot("systemd", boot_units="a.service b.service"))
    assert rc == 1 and result is None and not t.pushed()
    assert ["rm", "-f", "-v", BOOT] in t.boot_calls()


def test_boot_systemd_not_running_is_a_finding() -> None:
    for state in ("degraded", "starting", "", "maintenance"):
        t = FakeTools()
        t.systemd = state
        assert run_boot(t, "systemd", boot_timeout=4) == 1 and not t.pushed(), state
        assert any("--failed" in a for a in t.argvs), "the failed units were not asked for"


def test_boot_systemd_polls_until_settled() -> None:
    t = FakeTools()
    t.systemd_first = ["starting", "starting"]
    assert run_boot(t, "systemd") == 0 and t.pushed()
    assert not t.systemd_first, "it stopped polling before systemd settled"


def test_boot_healthcheck_passes() -> None:
    t = FakeTools()
    assert run_boot(t, "healthcheck") == 0 and t.pushed()
    assert ["healthcheck", "run", BOOT] in t.boot_calls()


def test_boot_healthcheck_undeclared_is_a_finding() -> None:
    for declared in ("", "null", "<nil>", "{}"):
        t = FakeTools()
        t.healthcheck = declared
        assert run_boot(t, "healthcheck") == 1 and not t.pushed(), declared
        assert not t.boot_calls(), "a container was started for an image with no HEALTHCHECK"


def test_boot_healthcheck_unhealthy_is_a_finding() -> None:
    t = FakeTools()
    t.health_rc = 1
    assert run_boot(t, "healthcheck", boot_timeout=6) == 1 and not t.pushed()
    assert sum(a[4:5] == ["healthcheck"] for a in t.argvs) == len(range(0, 6, 2))
    assert ["rm", "-f", "-v", BOOT] in t.boot_calls()


def test_boot_container_podman_cannot_create_is_refused() -> None:
    """125 is podman's own failure: nothing in the image ran, so it is no verdict."""
    for profile in ("systemd", "healthcheck", "mcp-http"):
        t = FakeTools()
        t.boot_rc = 125
        assert "exit 125" in refused(t, QUAY_PARENT, files=boot(profile)), profile
        assert ["rm", "-f", "-v", BOOT] in t.boot_calls()
    t = FakeTools()
    t.why, t.server_rc = "the server exited before answering initialize", 125
    assert "exit 125" in refused(t, QUAY_PARENT, files=boot("mcp"))
    assert ["rm", "-f", "-v", BOOT] in t.boot_calls()


def test_boot_container_that_cannot_start_is_a_finding() -> None:
    """126/127 come from inside the container: the image's entrypoint is the finding."""
    for profile in ("systemd", "healthcheck"):
        for rc in (1, 126, 127):
            t = FakeTools()
            t.boot_rc = rc
            assert run_boot(t, profile) == 1 and not t.pushed(), (profile, rc)
            assert ["rm", "-f", "-v", BOOT] in t.boot_calls()
    t = FakeTools()
    t.why, t.server_rc = "the server exited before answering initialize", 127
    assert run_boot(t, "mcp") == 1 and not t.pushed()


def test_boot_mcp_container_is_removed_on_every_path() -> None:
    """`run --rm -i` leaks its container when the client is killed; it is named and removed."""
    for why in ("", "no reply to initialize within 1s"):
        t = FakeTools()
        t.why = why
        run_boot(t, "mcp")
        assert t.boot_calls()[-1] == ["rm", "-f", "-v", BOOT], why


def test_boot_containers_are_confined() -> None:
    for profile in ("healthcheck", "mcp-http"):
        t = FakeTools()
        assert run_boot(t, profile) == 0
        start = t.boot_start()
        assert start[start.index("--pull=never") + 1 :][:3] == CONFINED, profile
    t = FakeTools()
    assert run_boot(t, "systemd", boot_units="httpd.service") == 0
    words = t.boot_start()[t.boot_start().index("--pull=never") + 1 : t.boot_start().index(TAG)]
    assert words[0] == "--cap-drop=all" and "--network=none" in words
    assert "--systemd=always" in words
    added = {w.removeprefix("--cap-add=") for w in words if w.startswith("--cap-add=")}
    assert added == set(_boot.SYSTEMD_CAPS) and "SYS_ADMIN" not in added
    assert not any(w.startswith("--security-opt") or w == "--privileged" for w in words)


def test_boot_execs_and_the_http_client_are_bounded() -> None:
    t = FakeTools()
    assert run_boot(t, "systemd", boot_units="a.service") == 0
    execs = [n for a, n in zip(t.argvs, t.timeouts, strict=True) if a[4:5] == ["exec"]]
    assert execs and all(n == _boot.EXEC_TIMEOUT for n in execs)
    t = FakeTools()
    assert run_boot(t, "mcp-http", boot_timeout=40) == 0
    client = [n for a, n in zip(t.argvs, t.timeouts, strict=True) if "python3" in a]
    assert client == [40 + _boot.GRACE]
    t = FakeTools()
    t.http_rc = 124  # what Tools.run reports for a client it stopped
    assert "exit 124" in refused(t, QUAY_PARENT, files=boot("mcp-http"))


def test_boot_mcp_http_client_is_named_and_removed_before_the_server() -> None:
    """The client holds the server's namespace: removed first, even when it timed out."""
    for http_rc in (0, 124):
        t = FakeTools()
        t.http_rc = http_rc
        go_or_refused = refused if http_rc else go
        go_or_refused(t, QUAY_PARENT, files=boot("mcp-http"))
        client = next(a for a in t.argvs if "python3" in a)
        assert client[client.index("--name") + 1] == f"{BOOT}-client"
        removed = [a[-1] for a in t.boot_calls() if a[:3] == ["rm", "-f", "-v"]]
        assert removed == [f"{BOOT}-client", BOOT], (http_rc, removed)


def test_boot_failed_rm_is_logged_not_hidden() -> None:
    t = FakeTools()
    t.rm_rc = {BOOT: 125}
    assert run_boot(t, "healthcheck") == 0
    assert f"could not remove {BOOT} (exit 125)" in t.log
    assert "dependent containers" in t.log


def test_boot_engine_failure_inside_a_check_is_refused() -> None:
    """125 from exec or healthcheck under a live container is podman's failure: a 2."""
    for profile in ("systemd", "healthcheck"):
        t = FakeTools()
        t.exec_rc = t.health_rc = 125
        assert "exit 125" in refused(t, QUAY_PARENT, files=boot(profile)), profile
        assert ["rm", "-f", "-v", BOOT] in t.boot_calls()
        t = FakeTools()  # the engine is unreachable: even inspect fails
        t.exec_rc = t.health_rc = 125
        t.boot_inspect_rc = 125
        assert "cannot inspect" in refused(t, QUAY_PARENT, files=boot(profile)), profile
    t = FakeTools()  # the image's HEALTHCHECK cannot even be read
    t.image_inspect_rc = 125
    assert "cannot inspect" in refused(t, QUAY_PARENT, files=boot("healthcheck"))
    assert not t.boot_calls(), "a container was started on an image podman could not read"


def test_boot_hung_check_asks_the_engine() -> None:
    """A check that timed out: a 2 when the engine cannot answer, else still judged."""
    for profile in ("systemd", "healthcheck"):
        t = FakeTools()
        t.exec_rc = t.health_rc = 124
        t.boot_inspect_rc = 125
        assert "cannot inspect" in refused(t, QUAY_PARENT, files=boot(profile)), profile
        t = FakeTools()
        t.exec_rc = t.health_rc = 124
        assert run_boot(t, profile) == 1 and not t.pushed(), profile
        t = FakeTools()  # it hung because the container died: that is the finding
        t.exec_rc = t.health_rc = 124
        t.boot_running = "false"
        assert run_boot(t, profile) == 1 and "the container exited" in t.log, profile


def test_boot_container_that_exits_during_a_check_is_a_finding() -> None:
    """125 because the container is gone: the image died under test, a verdict."""
    for profile in ("systemd", "healthcheck"):
        t = FakeTools()
        t.exec_rc = t.health_rc = 125
        t.boot_running = "false"
        assert run_boot(t, profile) == 1 and not t.pushed(), profile


def test_boot_none_needs_its_reason() -> None:
    assert "boot-none-reason" in boot_refused("none")
    assert "boot-none-reason" in boot_refused("none", boot_none_reason="  ")
    t = FakeTools()
    assert run_boot(t, "none", boot_none_reason="a library, nothing to run") == 0
    assert t.pushed() and not t.conversed and not t.boot_calls()


def test_boot_command_needs_boot_command_and_is_not_run() -> None:
    assert "boot-command" in boot_refused("command")
    assert "boot-command" in boot_refused("command", boot_command=" ")
    t = FakeTools()
    assert run_boot(t, "command", boot_command="./test.sh") == 0
    assert t.pushed() and not t.conversed and not t.boot_calls()
    assert not any("./test.sh" in w for a in t.argvs for w in a), "the command was run"


def test_boot_command_under_another_profile_warns_and_does_not_fail() -> None:
    t = FakeTools()
    rc = run_boot(t, "none", boot_none_reason="no boot", boot_command="./test.sh")
    assert rc == 0 and t.pushed() and not t.boot_calls()
    assert "boot: WARNING: boot-command runs only under boot = \"command\", not 'none'" in t.log
    assert not any("./test.sh" in w for a in t.argvs for w in a), "the command was run"


def boot_message(fleet: str) -> str:
    """What the image job prints of boot-command under a fleet file with `fleet` as its text."""
    t = FakeTools()
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        ws.mkdir()
        (ws / ".git").mkdir()  # a checked-out tree: fetch_tree leaves it alone
        (ws / "Containerfile").write_text(QUAY_PARENT)
        (ws / "pyproject.toml").write_text(boot("command", boot_command="x")["pyproject.toml"])
        (ws / ".github" / "workflows").mkdir(parents=True)
        (ws / ".github" / "workflows" / "ci.yml").write_text(fleet)
        env = {
            "GITHUB_WORKSPACE": str(ws),
            "GITHUB_OUTPUT": str(Path(tmp) / "out"),
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_SHA": SHA,
            "GITHUB_RUN_ID": "7",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/ci.yml@refs/heads/main",
            "HOSTNAME": "cid",
            "RUNNER_TEMP": tmp,
            "QUAY_USERNAME": "robot",
            "QUAY_PASSWORD": "pw",
        }
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = image.run("push", {}, env, tools=t)
    assert rc == 0, out.getvalue()
    return out.getvalue()


def test_boot_command_points_at_the_boot_job_under_the_new_fleet_file() -> None:
    out = boot_message("jobs:\n  image:\n    steps: []\n  boot:\n    steps: []\n")
    assert "boot-command runs in the boot job" in out, out


def test_boot_command_is_not_enforced_under_an_old_fleet_file() -> None:
    out = boot_message("jobs:\n  image:\n    steps: []\n  promote:\n    steps: []\n")
    assert "boot-command is not enforced: this repo's fleet file has no boot job" in out, out


def test_boot_unknown_profile_refused() -> None:
    assert "boot must be one of" in boot_refused("shell")
    assert "boot must be one of" in boot_refused("MCP")


def test_boot_keys_without_boot_refused() -> None:
    for body in ('boot-env = "A=1"\n', "boot-port = 80\n", 'boot-none-reason = "x"\n'):
        t = FakeTools()
        msg = refused(t, QUAY_PARENT, files=settings(body))
        assert "no boot profile" in msg and body.split()[0] in msg, msg
        assert not t.argvs


def test_boot_bad_values_refused() -> None:
    for keys, said in (
        ({"boot_env": "NOEQUALS"}, "boot-env"),
        ({"boot_env": "1A=b"}, "boot-env"),
        ({"boot_env": "A-B=c"}, "boot-env"),
        ({"boot_env": 5}, "boot-env must be a string"),
        ({"boot_timeout": 0}, "boot-timeout"),
        ({"boot_timeout": -5}, "boot-timeout"),
        ({"boot_timeout": _boot.TIMEOUT_MAX + 1}, "boot-timeout"),
        ({"boot_timeout": True}, "boot-timeout"),
        ({"boot_timeout": "30"}, "boot-timeout"),
        ({"boot_timeout": 1.5}, "boot-timeout"),
        ({"boot_port": 0}, "boot-port"),
        ({"boot_port": 65536}, "boot-port"),
        ({"boot_port": True}, "boot-port"),
        ({"boot_path": "mcp"}, "boot-path"),
        ({"boot_path": "/a b"}, "boot-path"),
        ({"boot_units": "-x.service"}, "boot-units"),
        ({"boot_units": "a;b"}, "boot-units"),
        ({"boot_cmd": "'unclosed"}, "does not split"),
    ):
        assert said in boot_refused("mcp", **keys), keys


def test_boot_limits_are_accepted() -> None:
    keys = {"boot_timeout": _boot.TIMEOUT_MAX, "boot_port": 65535, "boot_path": "/"}
    assert run_boot(FakeTools(), "mcp-http", **keys) == 0
    assert run_boot(FakeTools(), "mcp-http", boot_timeout=1, boot_port=1) == 0


def test_boot_run_args_allowed_are_accepted() -> None:
    for args in (
        "--read-only",
        "--tmpfs /run",
        "--tmpfs=/run",
        "-e A=b --env C=d --env=E=f",
        "-u 1000:1000 --user app -w /app --workdir=/srv --shm-size 64m",
        "--entrypoint /bin/sh",
        "-v ./tests/image/home:/app/.openclaw:O",
        "--volume ./x:/y:ro",
    ):
        t = FakeTools()
        tree = {"tests/image/home/config.json": "{}", "x": ""}
        assert run_boot(t, "mcp", boot_run_args=args, tree=tree) == 0, args


def test_boot_run_args_tmpfs_with_options_is_accepted() -> None:
    for args in (
        "--tmpfs /tmp:rw,nosuid,size=256m",
        "--tmpfs=/tmp:rw,nosuid,size=256m",
        "--tmpfs /data:mode=1777",
    ):
        assert run_boot(FakeTools(), "mcp", boot_run_args=args) == 0, args


def test_boot_run_args_repo_volumes_are_rewritten_to_the_host_path() -> None:
    t = FakeTools()
    args = "-v ./tests/image/home:/app/.openclaw:O --volume=./x:/y:ro --read-only"
    tree = {"tests/image/home/config.json": "{}", "x/f": ""}
    assert run_boot(t, "healthcheck", boot_run_args=args, tree=tree) == 0
    start = t.boot_start()
    assert start[start.index("--pull=never") + 4 : start.index(TAG)] == [
        "-v",
        f"{HOST}/tests/image/home:/app/.openclaw:O",
        f"--volume={HOST}/x:/y:ro",
        "--read-only",
    ]
    t = FakeTools()
    assert run_boot(t, "mcp", boot_run_args="-v ./x:/y:ro", tree={"x": ""}) == 0
    argv = t.conversed[0][0]
    assert argv[argv.index("-v") + 1] == f"{HOST}/x:/y:ro"
    t = FakeTools()  # only a volume's source is the checkout's: other words stay
    args = "--entrypoint ./x -e P=./x -v ./x:/y:ro"
    assert run_boot(t, "mcp", boot_run_args=args, tree={"x": ""}) == 0
    argv = t.conversed[0][0]
    assert argv[argv.index("--entrypoint") + 1] == "./x"
    assert argv[argv.index("-e") + 1] == "P=./x"


def test_boot_volume_through_a_committed_symlink_is_refused() -> None:
    """`link -> /` passes every string check; the walk on disk is what refuses it."""
    for links, args in (
        ({"link": Link("/")}, "-v ./link:/h:ro"),
        ({"link": Link("/")}, "-v ./link/etc:/h:ro"),
        ({"fixtures/link": Link("/etc")}, "--volume=./fixtures/link:/h:O"),
        ({"inner": Link("x")}, "-v ./inner:/h:ro"),  # even one that stays inside
        ({"d/up": Link("..")}, "-v ./d/up/x:/h:ro"),
    ):
        t = FakeTools()
        files = {**boot("healthcheck", boot_run_args=args), "x": "", **links}
        msg = refused(t, QUAY_PARENT, files=files)
        assert "symlink" in msg, (args, msg)
        assert not t.boot_calls(), f"{args} started a container"
    t = FakeTools()
    msg = refused(t, QUAY_PARENT, files=boot("healthcheck", boot_run_args="-v ./absent:/h:ro"))
    assert "does not exist" in msg


def test_boot_run_args_that_reach_the_host_are_refused() -> None:
    for args in (
        "--privileged",
        "--network host",
        "--network=host",
        "--net host",
        "--device /dev/fuse",
        "--pid=host",
        "--cap-add=all",
        "--security-opt seccomp=unconfined",
        "-v /:/host:ro",
        "-v /etc:/etc:ro",
        "-v ./../x:/y:ro",
        "-v ./x/../../y:/y:ro",
        "-v ./x:/y",
        "-v ./x:/y:rw",
        "-v ./x:/y:ro,z",
        "-v ./x:/y:O,Z",
        "-v ./x:/y:z",
        "--volume=./x:/y",
        "-v named:/y:ro",
        "--mount type=bind,source=/,destination=/h",
        "--tmpfs relative",
        "-w relative",
        "-e",
        "-e NOEQUALS",
        "-e 1A=b",
        "--read-only=false",
        "--user",
        "stray",
        "--rm",
    ):
        t = FakeTools()
        msg = refused(t, QUAY_PARENT, files=boot("mcp", boot_run_args=args))
        assert "boot-run-args" in msg, (args, msg)
        assert not t.argvs, f"{args!r} ran something before it was refused"
        assert not t.conversed


def test_boot_run_args_refused_for_every_profile() -> None:
    for profile in _boot.PROFILES:
        keys = {"boot_run_args": "--privileged"}
        if profile == "none":
            keys["boot_none_reason"] = "x"
        if profile == "command":
            keys["boot_command"] = "x"
        assert "boot-run-args" in boot_refused(profile, **keys), profile


def test_build_args_become_build_arg_words() -> None:
    t = FakeTools()
    go(t, QUAY_PARENT, files=settings('build-args = "A=1\\n\\n  B=two words  \\n"\n'))
    argv = t.build_argv()
    pairs = [argv[i + 1] for i, w in enumerate(argv) if w == "--build-arg"]
    assert pairs == ["A=1", "B=two words"]


def test_build_args_bad_line_refused() -> None:
    for body in ('build-args = "NOEQUALS"\n', 'build-args = "A=1\\n1B=2"\n', "build-args = 5\n"):
        t = FakeTools()
        msg = refused(t, QUAY_PARENT, files=settings(body))
        assert "build-args" in msg and not t.argvs, (body, msg)


# ------------------------------------------------------------------ publish, many images

_core.EXTRA_IMAGES["crunchtools/demo"] = ("demo-base", "demo-dev")  # what sdlc would review
BASE_FILE = "FROM quay.io/crunchtools/ubi10-core:latest\nRUN true\n"
APP_FILE = "ARG BASE=quay.io/crunchtools/ubi10-core:latest\nFROM ${BASE}\nRUN true\n"
BASE_ENTRY = '[[tool.sdlc.images]]\nname = "demo-base"\ncontainerfile = "Containerfile.base"\n'
MANY = {
    "pyproject.toml": (
        '[tool.sdlc.image]\nbuild-args = "BASE=quay.io/crunchtools/demo-base:latest"\n' + BASE_ENTRY
    ),
    "Containerfile.base": BASE_FILE,
}


def test_publish_false_builds_nothing() -> None:
    t = FakeTools()
    msg = refused(t, QUAY_PARENT, files=settings("publish = false\n"))
    assert "publishes no image" in msg and not t.built(), msg


def test_publish_must_be_a_bool() -> None:
    msg = refused(FakeTools(), QUAY_PARENT, files=settings('publish = "no"\n'))
    assert "publish must be true or false" in msg, msg


def test_many_images_build_the_base_first_and_the_app_on_its_pushed_digest() -> None:
    t = FakeTools()
    rc, result = go(t, APP_FILE, files=MANY)
    assert rc == 0 and result is not None, t.log
    assert [r["ref"] for r in result["images"]] == [f"{OWN}-base", OWN], result
    assert result["sha"] == SHA and all(r["tag"] == f"gated-{SHA}" for r in result["images"])
    pushes = [a[-1] for a in t.argvs if a[4:5] == ["push"]]
    assert pushes == [f"docker://{OWN}-base:gated-{SHA}", f"docker://{OWN}:gated-{SHA}"], pushes
    # The app is built FROM the digest this job pushed, never verified as a stranger's.
    app = [a for a in t.argvs if a[4:5] == ["build"]][1]
    assert app[app.index("--from") + 1] == f"{OWN}-base@{PUSHED}", app
    verifies = [a for a in t.argvs if a[:2] == ["cosign", "verify"]]
    assert all("demo-base" not in a[-1] for a in verifies), verifies
    assert f"org.opencontainers.image.base.name={OWN}-base" in app, app
    assert f"org.opencontainers.image.base.digest={PUSHED}" in app, app
    base = next(a for a in t.argvs if a[4:5] == ["build"])
    assert base[base.index("-f") + 1] == "Containerfile.base" and base[-1] == ".", base
    assert "localhost/demo-base:sdlc" in base
    scans = [a for a in t.argvs if any("type=image,source=" in w for w in a)]
    sources = [next(w for w in a if w.startswith("type=image,")) for a in scans]
    assert sources[0].startswith("type=image,source=localhost/demo-base:sdlc,"), sources
    assert sources[1].startswith("type=image,source=localhost/demo:sdlc,"), sources


def test_a_gate_finding_on_the_base_pushes_nothing_after_it() -> None:
    t = FakeTools()
    t.scan_rc = 1
    rc, result = go(t, APP_FILE, files=MANY)
    assert rc == 1 and result is None, t.log
    assert not t.pushed() and len([a for a in t.argvs if a[4:5] == ["build"]]) == 1


def test_publish_false_with_an_extra_image_publishes_only_the_extra() -> None:
    files = {
        "pyproject.toml": "[tool.sdlc.image]\npublish = false\n" + BASE_ENTRY,
        "Containerfile.base": BASE_FILE,
    }
    t = FakeTools()
    rc, result = go(t, QUAY_PARENT, files=files)
    assert rc == 0 and result is not None, t.log
    assert [r["ref"] for r in result["images"]] == [f"{OWN}-base"], result


def test_an_image_the_allowlist_does_not_give_this_repo_is_refused() -> None:
    for name in ("trentina-demo", "demo-evil", "demo"):
        files = {
            "pyproject.toml": f'[[tool.sdlc.images]]\nname = "{name}"\ncontainerfile = "C"\n',
            "C": BASE_FILE,
        }
        msg = refused(FakeTools(), QUAY_PARENT, files=files)
        assert "may publish only" in msg or "named twice" in msg, (name, msg)


def test_an_image_entry_is_checked() -> None:
    for body, files, want in (
        ('name = "demo-base"\n', {}, "needs containerfile"),
        ('name = "demo-base"\ncontainerfile = "../x"\n', {}, "leaves the checkout"),
        ('name = "demo-base"\ncontainerfile = "/etc/passwd"\n', {}, "relative"),
        ('name = "demo-base"\ncontainerfile = "nope"\n', {}, "not a file"),
        ('name = "demo-base"\ncontainerfile = "C"\ncontext = "C"\n', {"C": "x"}, "directory"),
        ('name = "demo-base"\ncontainerfile = "C"\nimage = "x"\n', {"C": "x"}, "no setting"),
        # The repo's pull requests, not an image's: the root table carries it.
        (
            'name = "demo-base"\ncontainerfile = "C"\npr-build-paths = ["deps/**"]\n',
            {"C": "x"},
            "no setting",
        ),
        ('name = "demo-base"\ncontainerfile = "L"\n', {"L": Link("/etc/passwd")}, "leaves"),
    ):
        tree = {"pyproject.toml": "[[tool.sdlc.images]]\n" + body, **files}
        msg = refused(FakeTools(), QUAY_PARENT, files=tree)
        assert want in msg, (body, msg)


def test_images_built_from_each_other_in_a_cycle_are_refused() -> None:
    files = {
        "pyproject.toml": (
            "[tool.sdlc.image]\npublish = false\n"
            '[[tool.sdlc.images]]\nname = "demo-base"\ncontainerfile = "A"\n'
            '[[tool.sdlc.images]]\nname = "demo-dev"\ncontainerfile = "B"\n'
        ),
        "A": "FROM quay.io/crunchtools/demo-dev:latest\n",
        "B": "FROM quay.io/crunchtools/demo-base:latest\n",
    }
    msg = refused(FakeTools(), QUAY_PARENT, files=files)
    assert "cycle" in msg, msg


def test_build_args_choose_the_parent_that_is_verified() -> None:
    t = FakeTools()
    body = 'build-args = "BASE=registry.redhat.io/ubi10/ubi-minimal:10.1"\n'
    rc, _ = go(t, APP_FILE, files=settings(body))
    assert rc == 0, t.log
    pinned = t.build_argv()[t.build_argv().index("--from") + 1]
    assert t.policy is not None and pinned == f"registry.redhat.io/ubi10/ubi-minimal@{PARENT}"


def test_a_parent_another_repo_publishes_is_verified_as_that_repo() -> None:
    t = FakeTools()
    rc, _ = go(t, "FROM quay.io/crunchtools/rotv-base:latest\n")
    assert rc == 0, t.log
    verify = next(a for a in t.argvs if a[:2] == ["cosign", "verify"])
    assert verify[verify.index("-a") + 1] == "repo=crunchtools/rotv", verify


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


def test_git_and_sdlc_trust_the_mounted_workspace() -> None:
    """git rev-parse and `sdlc plan` run in the runner's workspace, owned by another uid (#157)."""
    t = FakeTools()
    rc, _ = go(t, QUAY_PARENT)
    assert rc == 0, t.log
    seen = [
        e
        for a, e in zip(t.argvs, t.envs, strict=True)
        if a[:2] in (["git", "rev-parse"], ["sdlc", "plan"])
    ]
    assert [len(seen)] == [2], t.argvs  # rev-parse and `sdlc plan`
    for env in seen:
        n = int(env["GIT_CONFIG_COUNT"]) - 1
        assert env[f"GIT_CONFIG_KEY_{n}"] == "safe.directory"
        assert Path(env[f"GIT_CONFIG_VALUE_{n}"]).name == "ws"  # the workspace, never `*`
