#!/usr/bin/env python3
"""`sdlc_jobs/boot.py` must run a repo's boot shell on the pushed digest and hold no secret.

The job runs `boot-command` and `contract-test`, shell from the repo, against the image
`image` pushed, in a sibling container started through the runner's socket. So the suite
is about what that container is given (the image, the workspace at its runner path, the
socket, no token, no secret), what makes a verdict (a non-zero exit, a timeout), which
image it may be pointed at (only the one `image` pushed, re-validated as `promote` does),
what is refused and that each of the three keys is enforced by itself. A fake `Tools`
records each argv and answers from a script; nothing here runs podman, docker or bash.

Run: tests/test_job_boot.py   (needs python3 only)
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
from typing import Unpack, cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdlc_jobs import _core, boot
from sdlc_jobs._core import NNP, TIMED_OUT, CannotRunError, RunOptions, Tools


def no_container_id(mountinfo: str = "") -> str:
    """The fake engine answers to HOSTNAME; a suite that itself runs in a
    container must not find its own id in mountinfo first."""
    del mountinfo
    return ""


_core.own_container_id = no_container_id

REPO = "crunchtools/demo"
SHA = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "a" * 64
REF = "quay.io/crunchtools/demo"
LIMIT = 42
SELF = "quay.io/crunchtools/sdlc@sha256:" + "d" * 64
MERGE = {"ref": REF, "digest": DIGEST, "sha": SHA, "tag": f"gated-{SHA}"}
HOST = "/runner/work/demo"  # the workspace's path on the runner
SOCKET = "/run/user/1000/podman/podman.sock"  # the socket's path on the runner
CONTAINERFILE = {"Containerfile": "FROM quay.io/crunchtools/ubi10-core:latest\nRUN true\n"}


def cp(rc: int = 0, out: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, out, "")


class FakeTools(Tools):
    """The engine and bash: every argv is logged, a shell exits as `shell` says."""

    def __init__(self, shell: Callable[[str], int] | None = None) -> None:
        self.argvs: list[list[str]] = []
        self.runs: list[dict] = []  # each shell run: argv, env, timeout, tmp
        self.shell = shell or (lambda _sh: 0)
        self.workspace = ""  # the job's workspace, which the engine reports as mounted at HOST
        self.socket = True  # whether the job container has the runner's socket mounted
        self.inherited = False  # the socket comes by --volumes-from, as podman lists it
        self.image = SELF  # the job's own image, as inspect reports it
        self.log = io.StringIO()  # what the job printed, in order with the argvs
        self.marks: list[tuple[int, str]] = []  # (argv count, printed text) at each shell
        self.apparmor = ""  # what `podman info` says of .Host.Security.AppArmorEnabled
        self.policies: list[dict] = []  # the policy each skopeo copy was given, read at the call
        self.loaded = DIGEST  # the digest the engine reports for the loaded image

    def run(self, argv: list[str], **kw: Unpack[RunOptions]) -> subprocess.CompletedProcess[str]:
        env, stream, timeout = kw.get("env"), kw.get("stream"), kw.get("timeout")
        assert argv[0] != "git", f"boot runs no git: {argv}"
        self.argvs.append(argv)
        if argv[-4:-1] == ["-euo", "pipefail", "-c"]:
            tmp = Path(self.workspace, boot.TMP).is_dir()
            self.runs.append({"argv": argv, "env": dict(env or {}), "timeout": timeout, "tmp": tmp})
            self.marks.append((len(self.argvs), cast(io.StringIO, sys.stdout).getvalue()))
            assert stream, "repo shell must stream to the job log"
            return cp(self.shell(argv[-1]))
        if (pulled := self.pull(argv)) is not None:
            return pulled
        sock = {"Source": SOCKET, "Destination": boot.SOCKET_PATH}
        if argv[-2:] == ["inspect", "job-ctr"]:
            mounts = [{"Source": HOST, "Destination": self.workspace}]
            if self.socket and not self.inherited:
                mounts.append(sock)
            doc = {"ImageName": self.image, "Image": "", "Mounts": mounts}
            if self.inherited:
                doc["HostConfig"] = {"VolumesFrom": ["launcher-ctr:rw"]}
            return cp(0, json.dumps([doc]))
        if argv[-2:] == ["inspect", "launcher-ctr"]:
            return cp(0, json.dumps([{"Mounts": [sock] if self.socket else []}]))
        assert argv[0] == "podman", f"unexpected argv {argv}"
        if "{{.Host.Security.AppArmorEnabled}}" in argv:
            return cp(0, self.apparmor + "\n")
        return cp(0)

    def pull(self, argv: list[str]) -> subprocess.CompletedProcess[str] | None:
        """skopeo's copy, its policy read while it exists, and the loaded digest; else None."""
        if argv[0] == "skopeo":
            self.policies.append(json.loads(Path(argv[argv.index("--policy") + 1]).read_text()))
            return cp(0)
        if argv[-4:-1] == ["inspect", "--format", "{{.Digest}}"]:
            return cp(0, self.loaded + "\n")
        return None

    def verbs(self) -> list[str]:
        """Each engine call as `podman load`, `podman run`, `podman image prune`, in order;
        the pull as `skopeo copy`."""
        out = []
        for a in self.argvs:
            if a[0] == "skopeo":
                out.append("skopeo copy")
            if a[0] == "podman":
                rest = [w for w in a[1:] if w not in ("--remote",)]
                rest = rest[2:] if rest[:1] == ["--url"] else rest
                out.append(" ".join(["podman", *rest[: 2 if rest[0] == "image" else 1]]))
        return out

    def shells(self) -> list[str]:
        return [r["argv"][-1] for r in self.runs]

    def acted(self) -> list[list[str]]:
        """Every engine call but the inspects that find this container."""
        return [a for a in self.argvs if "inspect" not in a]


def settings(body: str = "") -> dict[str, str]:
    return {**CONTAINERFILE, "pyproject.toml": f"[tool.sdlc.image]\n{body}"}


def toml(**keys: object) -> str:
    return "".join(f"{k.replace('_', '-')} = {json.dumps(v)}\n" for k, v in keys.items())


def go(
    tools: FakeTools,
    body: str = "",
    *,
    event_name: str = "push",
    result: object = None,
    files: dict[str, str] | None = None,
    **extra: str,
) -> tuple[int, str, Path]:
    """(exit, what it printed, the workspace) of `boot.run` over a tree with `body`'s settings."""
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        ws.mkdir()
        (ws / ".git").mkdir()  # a checked-out tree: fetch_tree leaves it alone
        for name, text in (files or settings(body)).items():
            (ws / name).write_text(text)
        tools.workspace = str(ws)
        env = {
            "GITHUB_WORKSPACE": str(ws),
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_SHA": SHA,
            "GITHUB_TOKEN": "tok",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example",
            "HOSTNAME": "job-ctr",
            "RUNNER_TEMP": tmp,
            "SDLC_IMAGE_RESULT": json.dumps(result or MERGE),
            **extra,
        }
        out = io.StringIO()
        sys.stdout = tools.log = out  # the fake reads what was printed so far
        try:
            rc = boot.run(event_name, {}, env, tools=tools)
        finally:
            sys.stdout = sys.__stdout__
        return rc, out.getvalue(), ws


def refused(
    tools: FakeTools,
    body: str = "",
    *,
    event_name: str = "push",
    result: object = None,
    **kw: str,
) -> str:
    """The message of the refusal (exit 2) `boot.run` raises, or a failed assertion."""
    try:
        go(tools, body, event_name=event_name, result=result, **kw)
    except CannotRunError as exc:
        assert not tools.acted(), f"something ran after a refusal: {tools.acted()}"
        return str(exc)
    raise AssertionError("expected a refusal (exit 2)")


COMMAND = toml(boot="command", boot_command="./boot.sh\nexit 0")
CONTRACT = toml(contract_test="make contract")


def parts(argv: list[str]) -> dict:
    """A sibling's `run` argv as its -v, -e, -w and --name values, flags and image."""

    def flags(flag: str) -> list[str]:
        return [argv[i + 1] for i, w in enumerate(argv) if w == flag]

    return {
        "volumes": flags("-v"),
        "env": dict(kv.split("=", 1) for kv in flags("-e")),
        "workdir": flags("-w"),
        "name": flags("--name"),
        "entrypoint": flags("--entrypoint"),
        "image": argv[argv.index("--entrypoint") + 2],
    }


def test_boot_command_failing_is_a_1_and_passing_a_0() -> None:
    t = FakeTools(lambda _sh: 1)
    rc, out, _ = go(t, COMMAND)
    assert rc == 1 and "boot-command: REFUSED: exited 1" in out, out
    t = FakeTools()
    rc, out, _ = go(t, COMMAND)
    assert rc == 0 and "boot-command: passed" in out, out


def test_boot_command_runs_as_bash_in_a_sibling_of_the_jobs_image_with_the_image() -> None:
    t = FakeTools()
    go(t, COMMAND)
    (run,) = t.runs
    argv, got = run["argv"], parts(run["argv"])
    assert argv[:5] == ["podman", "--remote", "--url", _core.SOCKET, "run"], argv
    assert argv[-4:] == ["-euo", "pipefail", "-c", "./boot.sh\nexit 0"]
    assert got["entrypoint"] == ["bash"] and got["image"] == SELF
    assert got["env"]["IMAGE"] == f"{REF}@{DIGEST}"
    assert "--rm" in argv and run["tmp"], "the workspace's tmp directory was not made"


def test_boot_command_needs_boot_command_profile() -> None:
    body = toml(boot="command")
    for command in ("", " "):
        msg = refused(FakeTools(), body + toml(boot_command=command))
        assert 'boot = "command" needs boot-command' in msg, msg
    assert "needs boot-command" in refused(FakeTools(), body)


def test_boot_command_under_another_profile_warns_and_runs_nothing() -> None:
    body = toml(boot="none", boot_none_reason="no boot", boot_command="false")
    t = FakeTools(lambda _sh: 1)
    rc, out, _ = go(t, body)
    assert rc == 0 and not t.acted(), t.acted()
    assert "boot: WARNING: boot-command runs only under boot = \"command\", not 'none'" in out
    t = FakeTools()
    rc, out, _ = go(t, body + CONTRACT)
    assert rc == 0 and t.shells() == ["make contract"] and "WARNING" in out, out


def test_contract_test_failing_is_a_1_and_passing_a_0() -> None:
    t = FakeTools(lambda _sh: 1)
    rc, out, _ = go(t, CONTRACT)
    assert rc == 1 and "contract-test: REFUSED: exited 1" in out, out
    t = FakeTools()
    rc, out, _ = go(t, CONTRACT)
    assert rc == 0 and "contract-test: passed" in out, out
    assert parts(t.runs[0]["argv"])["env"]["IMAGE"] == f"{REF}@{DIGEST}"
    assert t.shells() == ["make contract"]


def test_contract_test_runs_after_boot_command_and_with_boot_none() -> None:
    t = FakeTools()
    assert go(t, COMMAND + CONTRACT)[0] == 0
    assert t.shells() == ["./boot.sh\nexit 0", "make contract"]
    t = FakeTools(lambda sh: 1 if sh.startswith("./boot") else 0)
    assert go(t, COMMAND + CONTRACT)[0] == 1
    assert t.shells() == ["./boot.sh\nexit 0"], "contract-test ran after a failed boot-command"
    t = FakeTools()
    none = toml(boot="none", boot_none_reason="no boot")
    assert go(t, none + CONTRACT)[0] == 0 and t.shells() == ["make contract"]


def test_free_disk_empties_the_sdks_and_prunes_before_the_pull_on_github_hosted() -> None:
    t = FakeTools()
    rc, _, _ = go(t, toml(free_disk=True), RUNNER_ENVIRONMENT="github-hosted")
    want = ["podman inspect"] * 2 + ["podman info", "podman run", "podman image prune"]
    assert rc == 0 and t.verbs() == [*want, "skopeo copy", "podman load", "podman image inspect"], (
        t.verbs()
    )
    run = t.argvs[3]
    for d in boot.SDK_DIRS:
        assert any(w.startswith(f"{d}:/free/") for w in run), (d, run)
    assert SELF in run and "--network=none" in run and run[-1] == boot.EMPTY
    assert t.argvs[4][4:] == ["image", "prune", "--all", "--force"]
    assert t.argvs[5][0] == "skopeo" and t.argvs[5][-2] == f"docker://{REF}@{DIGEST}"


def test_free_disk_with_no_known_image_of_the_job_is_a_2() -> None:
    t = FakeTools()
    t.image = ""
    msg = refused(t, toml(free_disk=True), RUNNER_ENVIRONMENT="github-hosted")
    assert "image is unknown" in msg, msg


def test_free_disk_is_a_no_op_off_github_hosted_and_says_so() -> None:
    for runner in ({}, {"RUNNER_ENVIRONMENT": "self-hosted"}):
        t = FakeTools()
        rc, out, _ = go(t, toml(free_disk=True), **runner)
        assert rc == 0 and t.verbs() == ["podman inspect"] * 2 + [
            "podman info",
            "skopeo copy",
            "podman load",
            "podman image inspect",
        ], t.verbs()
        assert "free-disk: not a GitHub-hosted runner" in out, out
    t = FakeTools()
    rc, out, _ = go(t, toml(free_disk=False))
    assert rc == 0 and not t.acted() and "free-disk" not in out


def test_a_secret_in_the_environment_is_a_2() -> None:
    for name in ("QUAY_PASSWORD", "QUAY_USERNAME", "RHSM_ORG_ID", "REGISTRY_REDHAT_IO_PASSWORD"):
        msg = refused(FakeTools(), CONTRACT, **{name: "s3cret"})
        assert name in msg and "s3cret" not in msg, msg


def test_the_shell_sees_no_token_and_no_secret() -> None:
    t = FakeTools()
    extra = {"ACTIONS_RUNTIME_TOKEN": "rt-s3", "COSIGN_PASSWORD": "pw-s3"}
    rc, _, _ = go(t, CONTRACT, COSIGN_PRIVATE_KEY="key-s3", KEEP="yes", PATH="/bin", **extra)
    assert rc == 0
    argv = t.runs[0]["argv"]
    seen = parts(argv)["env"]
    allowed = {*boot.PASSED, "CI", "HOME", "TMPDIR", "GITHUB_WORKSPACE", "IMAGE"}
    allowed |= {"CONTAINER_HOST", "DOCKER_HOST"}
    assert set(seen) <= allowed, sorted(set(seen) - allowed)
    for name in ("GITHUB_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL", "KEEP", "PATH", "QUAY_USERNAME"):
        assert name not in seen, name
    for value in ("tok", "oidc", "rt-s3", "pw-s3", "key-s3", "oidc.example"):
        assert not any(value in w for w in argv), value


def test_the_shell_is_passed_the_runs_own_variables_and_no_others() -> None:
    t = FakeTools()
    skip = ("GITHUB_SHA", "GITHUB_REPOSITORY")
    ours = {k: f"v-{k}" for k in boot.PASSED if k not in skip}
    go(t, CONTRACT, **ours)
    seen = parts(t.runs[0]["argv"])["env"]
    for name, value in ours.items():
        assert seen[name] == value, name
    assert seen["GITHUB_SHA"] == SHA and seen["GITHUB_REPOSITORY"] == REPO


def test_the_sibling_has_the_workspace_the_socket_and_the_runners_network() -> None:
    t = FakeTools()
    go(t, CONTRACT)
    argv = t.runs[0]["argv"]
    got = parts(argv)
    assert got["volumes"] == [f"{HOST}:{HOST}", f"{SOCKET}:/var/run/docker.sock"], got
    assert got["workdir"] == [HOST]
    assert "--network=host" in argv and "--security-opt=no-new-privileges" in argv
    assert "--security-opt=label=disable" in argv  # the runner's tree and socket keep their labels
    env = got["env"]
    assert env["GITHUB_WORKSPACE"] == HOST and env["CI"] == "true"
    assert env["TMPDIR"] == f"{HOST}/.sdlc-boot-tmp" and env["HOME"] == env["TMPDIR"]
    assert env["CONTAINER_HOST"] == env["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert not any(t.workspace in w for w in argv[:-1]), "a path of the job container's own"


def test_apparmor_engine_runs_the_sibling_without_no_new_privileges() -> None:
    """crunchtools/sdlc#181: no-new-privileges stacks AppArmor's profiles and breaks signals."""
    t = FakeTools()
    t.apparmor = "true"
    assert go(t, COMMAND + CONTRACT)[0] == 0
    assert t.shells() == ["./boot.sh\nexit 0", "make contract"]
    assert all(NNP not in r["argv"] for r in t.runs)
    assert sum("{{.Host.Security.AppArmorEnabled}}" in a for a in t.argvs) == 1


def test_a_socket_that_is_not_mounted_is_a_2() -> None:
    t = FakeTools()
    t.socket = False
    msg = refused(t, CONTRACT)
    assert "socket is not mounted at /var/run/docker.sock" in msg, msg


def test_a_socket_taken_by_volumes_from_is_found() -> None:
    """podman lists a --volumes-from container's mounts under the source container only."""
    t = FakeTools()
    t.inherited = True
    assert go(t, CONTRACT)[0] == 0
    assert t.runs and f"{SOCKET}:/var/run/docker.sock" in " ".join(t.runs[0]["argv"])
    t = FakeTools()
    t.inherited, t.socket = True, False
    assert "socket is not mounted at /var/run/docker.sock" in refused(t, CONTRACT)


def test_each_shell_is_followed_by_a_forced_removal_of_its_container() -> None:
    t = FakeTools()
    go(t, COMMAND + CONTRACT, GITHUB_RUN_ID="77")
    names = [parts(r["argv"])["name"][0] for r in t.runs]
    assert names == ["sdlc-boot-77-demo-0", "sdlc-boot-77-demo-1"], names
    calls = [a[4:] for a in t.argvs if a[4:5] in (["run"], ["rm"])]
    assert [c[0] for c in calls] == ["run", "rm", "run", "rm"], calls
    for n, name in enumerate(names):
        assert calls[2 * n + 1] == ["rm", "--force", name], calls


def test_the_container_is_removed_after_a_failure_and_after_a_timeout() -> None:
    for rc in (1, TIMED_OUT):
        t = FakeTools(lambda _sh, rc=rc: rc)
        assert go(t, CONTRACT)[0] == 1
        name = parts(t.runs[0]["argv"])["name"][0]
        assert t.argvs[-1][4:] == ["rm", "--force", name], t.argvs[-1]


def test_on_github_hosted_the_load_and_the_run_use_podman_too() -> None:
    t = FakeTools()
    rc, _, _ = go(t, COMMAND, RUNNER_ENVIRONMENT="github-hosted")
    assert rc == 0
    assert t.verbs()[-5:] == [
        "skopeo copy",
        "podman load",
        "podman image inspect",
        "podman run",
        "podman rm",
    ], t.verbs()
    assert parts(t.runs[0]["argv"])["env"]["RUNNER_ENVIRONMENT"] == "github-hosted"


def test_podman_failing_to_start_the_container_is_a_2() -> None:
    t = FakeTools(lambda _sh: 125)
    msg = ""
    try:
        go(t, CONTRACT)
    except CannotRunError as exc:
        msg = str(exc)
    assert "podman could not start the container" in msg, "a podman 125 was not a 2"
    assert t.argvs[-1][4:6] == ["rm", "--force"], t.argvs[-1]
    t = FakeTools(lambda _sh: 127)  # bash's: the repo's shell named a missing command
    assert go(t, CONTRACT)[0] == 1


def test_a_timeout_is_a_1_bounded_by_boot_timeout() -> None:
    t = FakeTools(lambda _sh: TIMED_OUT)
    rc, out, _ = go(t, CONTRACT + toml(boot="none", boot_none_reason="x", boot_timeout=LIMIT))
    assert rc == 1 and f"still running after {LIMIT}s" in out, out
    assert t.runs[0]["timeout"] == LIMIT
    t = FakeTools()
    go(t, CONTRACT)
    assert t.runs[0]["timeout"] == boot.TIMEOUT


def test_boot_does_not_run_on_a_pull_request() -> None:
    msg = refused(FakeTools(), CONTRACT, event_name="pull_request")
    assert "'pull_request'" in msg, msg


def test_a_push_runs_only_its_own_ref() -> None:
    msg = refused(FakeTools(), CONTRACT, result={**MERGE, "ref": "quay.io/evil/demo"})
    assert "is not quay.io/crunchtools/demo" in msg, msg
    refused(FakeTools(), CONTRACT, result={**MERGE, "ref": f"{REF}:latest"})
    msg = refused(FakeTools(), CONTRACT, result={**MERGE, "sha": "f" * 40})
    assert f"image built {'f' * 40!r}, not this run's commit" in msg, msg


def test_an_image_the_repo_does_not_publish_is_a_2() -> None:
    msg = refused(FakeTools(), CONTRACT, result={"images": [{**MERGE, "name": "other"}]})
    assert "which this repo does not publish" in msg, msg
    both = {"images": [{**MERGE, "name": "demo"}, {**MERGE, "name": "demo"}]}
    assert "twice" in refused(FakeTools(), CONTRACT, result=both)


def test_a_digest_that_is_not_sha256_is_a_2() -> None:
    for bad in ("sha256:xyz", DIGEST.upper(), "sha512:" + "a" * 128, f"{DIGEST}\n--rm", ""):
        msg = refused(FakeTools(), CONTRACT, result={**MERGE, "digest": bad})
        assert "sha256 digest" in msg, (bad, msg)


def test_the_sibling_is_removed_when_its_run_raises() -> None:
    def broken(_sh: str) -> int:
        raise CannotRunError("podman could not be started")

    t = FakeTools(broken)
    with contextlib.suppress(CannotRunError):
        go(t, CONTRACT)
        raise AssertionError("the run's refusal did not reach the job")
    assert t.argvs[-1][-3:-1] == ["rm", "--force"], t.argvs[-1]
    assert t.argvs[-1][-1].startswith("sdlc-boot-"), t.argvs[-1]


def test_a_malformed_image_result_is_a_2() -> None:
    for bad, said in (
        ("", "missing or not JSON"),
        ("{", "missing or not JSON"),
        ("[]", "is not an object"),
        ('"x"', "is not an object"),
        ('{"images": "x"}', "not a list of images"),
        ('{"images": {}}', "not a list of images"),
        ('{"images": []}', "not a list of images"),
        ('{"images": [1]}', "not a list of images"),
    ):
        msg = refused(FakeTools(), CONTRACT, SDLC_IMAGE_RESULT=bad)
        assert said in msg, (bad, msg)


def test_a_missing_or_invalid_repository_or_sha_is_a_2() -> None:
    for extra in (
        {"GITHUB_SHA": ""},
        {"GITHUB_SHA": "abc123"},
        {"GITHUB_SHA": "g" * 40},
        {"GITHUB_SHA": SHA + "0"},
        {"GITHUB_REPOSITORY": ""},
        {"GITHUB_REPOSITORY": "demo"},
        {"GITHUB_REPOSITORY": "crunchtools/demo/x"},
    ):
        msg = refused(FakeTools(), CONTRACT, **extra)
        assert "GITHUB_REPOSITORY and GITHUB_SHA are required" in msg, (extra, msg)


def test_a_contract_test_that_is_not_a_string_is_a_2() -> None:
    for value in (5, True, ["make"]):
        msg = refused(FakeTools(), toml(contract_test=value))
        assert "contract-test must be a string" in msg, (value, msg)


def test_an_image_with_none_of_the_keys_is_not_pulled() -> None:
    t = FakeTools()
    rc, _, _ = go(t, toml(boot="none", boot_none_reason="no boot"))
    assert rc == 0 and not t.acted(), t.acted()
    t = FakeTools()
    assert go(t)[0] == 0 and not t.acted()


def test_only_the_images_with_work_are_pulled() -> None:
    files = {
        **CONTAINERFILE,
        "Containerfile.base": "FROM quay.io/ubi/ubi10:latest\n",
        "pyproject.toml": (
            f"[tool.sdlc.image]\n{CONTRACT}"
            '[[tool.sdlc.images]]\nname = "acquacotta-base"\ncontainerfile = "Containerfile.base"\n'
        ),
    }
    result = {
        "images": [
            {**MERGE, "ref": "quay.io/crunchtools/acquacotta", "name": "acquacotta"},
            {**MERGE, "ref": "quay.io/crunchtools/acquacotta-base", "name": "acquacotta-base"},
        ]
    }
    t = FakeTools()
    with contextlib.redirect_stdout(io.StringIO()):
        rc = _run(t, files, result, "crunchtools/acquacotta")
    pulled = [a[-2] for a in t.argvs if a[0] == "skopeo"]
    assert rc == 0 and pulled == [f"docker://quay.io/crunchtools/acquacotta@{DIGEST}"], pulled


def test_the_pull_is_skopeos_by_digest_under_a_policy_for_that_digest_alone() -> None:
    t = FakeTools()
    rc, _, ws = go(t, CONTRACT)
    copy = next(a for a in t.argvs if a[0] == "skopeo")
    archive = f"{ws}/{boot.PULL}/image.tar"
    assert rc == 0 and copy[-2:] == [
        f"docker://{REF}@{DIGEST}",
        f"oci-archive:{archive}:{REF}:gated-{SHA}",
    ]
    assert {"--src-no-creds", "--preserve-digests"} <= set(copy), copy
    assert t.policies == [
        {
            "default": [{"type": "reject"}],
            "transports": {"docker": {f"{REF}@{DIGEST}": [{"type": "insecureAcceptAnything"}]}},
        }
    ], t.policies
    load = next(a for a in t.argvs if "load" in a)
    assert load[-2:] == ["--input", archive] and load[:2] == ["podman", "--remote"], load
    assert not (ws / boot.PULL).exists(), "the policy and the archive outlive the pull"
    assert not any(a[0] == "podman" and "pull" in a for a in t.argvs), "the engine pulled"


def test_the_policy_names_no_tag_and_no_other_repository() -> None:
    doc = boot.policy(f"{REF}@{DIGEST}")
    accepted = [
        scope
        for scopes in doc["transports"].values()
        for scope, reqs in scopes.items()
        if reqs != [{"type": "reject"}]
    ]
    assert doc["default"] == [{"type": "reject"}] and accepted == [f"{REF}@{DIGEST}"], doc


def test_a_load_the_engine_cannot_find_by_digest_is_a_2() -> None:
    t = FakeTools()
    t.loaded = "sha256:" + "b" * 64
    msg = ""
    try:
        go(t, CONTRACT)
    except CannotRunError as exc:
        msg = str(exc)
    assert "not " + DIGEST in msg, f"expected a refusal (exit 2), got {msg!r}"
    assert not t.runs, "a shell ran on an image the engine does not hold"


def _run(t: FakeTools, files: dict[str, str], result: dict, repo: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        t.workspace = tmp
        (ws / ".git").mkdir()
        for name, text in files.items():
            (ws / name).write_text(text)
        env = {
            "GITHUB_WORKSPACE": tmp,
            "GITHUB_REPOSITORY": repo,
            "GITHUB_SHA": SHA,
            "HOSTNAME": "job-ctr",
            "RUNNER_TEMP": tmp,
            "SDLC_IMAGE_RESULT": json.dumps(result),
        }
        sys.stdout = t.log = io.StringIO()
        try:
            return boot.run("push", {}, env, tools=t)
        finally:
            sys.stdout = sys.__stdout__


def test_stop_commands_surround_the_shell() -> None:
    t = FakeTools()
    _, out, _ = go(t, CONTRACT)
    lines = out.splitlines()
    stop = next(i for i, x in enumerate(lines) if x.startswith("::stop-commands::"))
    token = lines[stop].removeprefix("::stop-commands::")
    assert t.marks[0][1].splitlines()[-1] == lines[stop], "stop-commands came after the shell"
    assert f"::{token}::" in lines[stop + 1 :], "workflow commands were never resumed"
    t = FakeTools(lambda _sh: 1)
    _, out, _ = go(t, CONTRACT)
    assert f"::{out.split('::stop-commands::')[1].split()[0]}::" in out.splitlines()


def test_boot_does_not_run_on_another_event() -> None:
    msg = refused(FakeTools(), CONTRACT, event_name="issues")
    assert "'issues'" in msg, msg


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
