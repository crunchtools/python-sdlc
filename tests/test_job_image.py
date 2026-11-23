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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdlc_jobs import image
from sdlc_jobs._core import CannotRunError, Tools

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


class FakeTools(Tools):
    def __init__(self) -> None:
        self.argvs: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.verify_rc = 0
        self.scan_rc = 0
        self.smoke_up = "true"
        self.build_rc = 0
        self.bootc = False
        self.lint_rc = 0  # what `sdlc containerfile-lint` exits
        self.policy: dict | None = None
        self.built_file = ""
        self.plan = ""  # the plan `sdlc plan ci` prints; empty is the stock one
        self.stdin: list[str | None] = []
        self.digest: str | None = PUSHED  # what `podman push --digestfile` writes; None, none

    def run(
        self,
        argv: list[str],
        *,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stream: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del stream  # captured or streamed, the fake answers the same
        self.argvs.append(argv)
        self.stdin.append(input)
        self.envs.append(dict(env or {}))
        if argv[0] == "skopeo":
            return self.skopeo(argv)
        if argv[0] == "cosign":
            return cp(self.verify_rc)
        if argv[:2] == ["sdlc", "plan"]:
            return cp(0, self.plan or plan_text(bootc=self.bootc))
        if argv[0] == "sdlc":
            return cp(self.lint_rc if argv[1:] == ["containerfile-lint"] else 0)
        assert argv[:3] == ["podman", "--remote", "--url"], argv
        return self.podman(argv[4:], cwd or ".")

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
            case "image":
                out = "{}"
            case "inspect":
                state_query = "ExitCode" in sub[-2]
                out = (
                    ("true 0 false" if up else "false 1 false") if state_query else str(up).lower()
                )
            case "push":
                if self.digest is not None:
                    Path(sub[sub.index("--digestfile") + 1]).write_text(self.digest + "\n")
            case "run" if "-d" not in sub:
                rc = self.scan_rc
        return cp(rc, out)

    def skopeo(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        rc, out = 0, ""
        match argv[1]:
            case "--policy":
                self.policy = json.loads(Path(argv[2]).read_text())
                rc = self.verify_rc
            case "inspect":
                out = PARENT
        return cp(rc, out)

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
    files: dict[str, str] | None = None,
) -> tuple[int, dict | None]:
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        ws.mkdir()
        (ws / "Containerfile").write_text(containerfile)
        for name, text in (files or {}).items():
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
        with contextlib.redirect_stdout(io.StringIO()):
            rc = image.run(event, {}, env, tools=tools)
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


def test_pull_request_refused() -> None:
    t = FakeTools()
    msg = refused(t, QUAY_PARENT, event="pull_request")
    assert "pull_request" in msg, msg
    assert not t.argvs, "a pull request ran something"


def test_only_the_workflow_events_run() -> None:
    for event in ("pull_request_target", "issues", "repository_dispatch", ""):
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
    assert "docker.io" in refused(t, cf)
    assert not t.built()
    t = FakeTools()
    assert "quay.io" in refused(t, "FROM quay.io/hummingbird/go:1 AS b\nFROM scratch\n")
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

    def run(
        self,
        argv: list[str],
        *,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stream: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del input, env, cwd, stream
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
    rc, _ = go(FakeTools(), QUAY_PARENT, files=settings(body))
    assert rc == 0


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
