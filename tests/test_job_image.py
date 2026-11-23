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
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdlc_jobs import image
from sdlc_jobs._core import CannotRunError

REPO = "crunchtools/demo"
SHA = "0123456789abcdef0123456789abcdef01234567"
PARENT = "sha256:" + "a" * 64
PUSHED = "sha256:" + "b" * 64
US = "\x1f"
HOST = "/host/ws"
INSPECT_ME = 2  # `inspect <container id>`, not a state query


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


class FakeTools:
    def __init__(self) -> None:
        self.argvs: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.verify_rc = 0
        self.scan_rc = 0
        self.smoke_up = "true"
        self.build_rc = 0
        self.bootc = False
        self.policy: dict | None = None
        self.built_file = ""

    def run(self, argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        self.argvs.append(argv)
        env = kw.get("env")
        self.envs.append(dict(env) if isinstance(env, dict) else {})
        cwd = str(kw.get("cwd") or ".")
        if argv[0] == "skopeo":
            return self.skopeo(argv)
        if argv[0] == "cosign":
            return cp(self.verify_rc)
        if argv[:2] == ["sdlc", "plan"]:
            return cp(0, plan_text(bootc=self.bootc))
        if argv[0] == "sdlc":
            return cp(0)
        assert argv[:3] == ["podman", "--remote", "--url"], argv
        return self.podman(argv[4:], cwd)

    def podman(self, sub: list[str], cwd: str) -> subprocess.CompletedProcess[str]:  # noqa: PLR0911
        if sub[0] == "inspect" and len(sub) == INSPECT_ME:
            mounts = [{"Destination": cwd, "Source": HOST}]
            return cp(0, json.dumps([{"Mounts": mounts, "Image": "imgid"}]))
        if sub[0] == "build":
            self.built_file = (Path(cwd) / sub[sub.index("-f") + 1]).read_text()
            return cp(self.build_rc)
        if sub[0] == "image":
            return cp(0, "{}")
        if sub[0] == "inspect":
            if "ExitCode" in sub[-2]:
                return cp(0, "true 0 false" if self.smoke_up == "true" else "false 1 false")
            return cp(0, self.smoke_up if self.smoke_up == "true" else "false")
        if sub[0] == "run" and "-d" not in sub:
            return cp(self.scan_rc)
        return cp(0)

    def skopeo(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[1] == "--policy":
            self.policy = json.loads(Path(argv[2]).read_text())
            return cp(self.verify_rc)
        if argv[1] == "inspect":
            return cp(0, PUSHED if ":gated-" in argv[-1] else PARENT)
        return cp(0)

    def pushed(self) -> bool:
        return any(a[4:5] == ["push"] for a in self.argvs)

    def built(self) -> bool:
        return any(a[4:5] == ["build"] for a in self.argvs)

    def build_argv(self) -> list[str]:
        return next(a for a in self.argvs if a[4:5] == ["build"])


def go(
    tools: FakeTools,
    containerfile: str,
    *,
    event: str = "push",
    extra: dict[str, str] | None = None,
) -> tuple[int, dict | None]:
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        ws.mkdir()
        (ws / "Containerfile").write_text(containerfile)
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
        with contextlib.redirect_stdout(io.StringIO()):
            rc = image.run(event, {}, env, tools=tools)  # type: ignore[arg-type]
        text = out.read_text() if out.exists() else ""
        result = json.loads(text.split("\n")[1]) if text else None
        return rc, result


QUAY_PARENT = "FROM quay.io/crunchtools/ubi10-core:latest\nRUN true\n"
RH_PARENT = "FROM registry.redhat.io/ubi10/ubi-minimal:10.1\nRUN true\n"


def refused(tools: FakeTools, containerfile: str, **kw: object) -> None:
    try:
        go(tools, containerfile, **kw)  # type: ignore[arg-type]
    except CannotRunError:
        assert not tools.pushed(), "something was pushed after a refusal"
        return
    raise AssertionError("expected a refusal (exit 2)")


def test_pull_request_refused() -> None:
    t = FakeTools()
    refused(t, QUAY_PARENT, event="pull_request")
    assert not t.argvs, "a pull request ran something"


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
        "FROM quay.io/hummingbird/python:3.14-builder AS b\nRUN true\n"
        "FROM quay.io/crunchtools/ubi10-core:latest\nCOPY --from=b /a /a\n"
    )
    go(t, cf)
    argv = t.build_argv()
    assert "--from" not in argv
    lines = t.built_file.splitlines()
    assert lines[0].endswith("AS b") and lines[2] == f"FROM quay.io/crunchtools/ubi10-core@{PARENT}"


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
    plan = plan_text().replace("--rm", "--rm\x1f--privileged", 1)
    orig = t.run

    def bad(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["sdlc", "plan"]:
            return cp(0, plan)
        return orig(argv, **kw)

    t.run = bad  # type: ignore[method-assign]
    refused(t, QUAY_PARENT)


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
    assert t.argvs.index(push) > max(i for i, a in enumerate(t.argvs) if a[4:5] == ["run"])


def test_scratch_has_no_parent() -> None:
    t = FakeTools()
    rc, result = go(t, "FROM scratch\nCOPY a /a\n")
    assert rc == 0 and result is not None and result["parent"] == ""
    assert "--from" not in t.build_argv()
    assert not any(a[0] == "cosign" for a in t.argvs)


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
