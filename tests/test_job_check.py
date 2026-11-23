#!/usr/bin/env python3
"""`sdlc_jobs/check.py` must plan honestly and still gate (marketplace-free-ci.md).

The job writes the plan every later job reads, so a wrong `image`, `release` or
`write` either skips a release or runs one nobody merged. A fake `Tools` records each
argv and API call and answers from a script; nothing here runs podman, skopeo or the
GitHub API.

Run: tests/test_job_check.py   (needs python3 only)
"""

from __future__ import annotations

import base64
import gzip
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdlc_jobs import check
from sdlc_jobs._core import CannotRunError, Response

REPO = "crunchtools/demo"
SHA = "0123456789abcdef0123456789abcdef01234567"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
EVENT = {"repository": {"default_branch": "main"}}


def cp(rc: int = 0, out: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, out, "")


SDLC_REF = "quay.io/crunchtools/sdlc@sha256:" + "c" * 64


def ran_launcher(argv: list[str]) -> bool:
    """The pinned launcher `check` printed and then ran: <tmp>/sdlc-launcher-*/sdlc."""
    return Path(argv[0]).name == "sdlc" and Path(argv[0]).parent.name.startswith("sdlc-launcher-")


def gated(t: FakeTools, command: str = "ci") -> bool:
    return any(c == command for c, _ in t.launched)


class FakeTools:
    def __init__(self, gate_rc: int = 0, prs: list | None = None) -> None:
        self.argvs: list[list[str]] = []
        self.launched: list[tuple[str, dict]] = []
        self.calls: list[tuple[str, str, bytes | None]] = []
        self.gate_rc = gate_rc
        self.prs = prs if prs is not None else []
        self.audit_rc = 0
        self.base: str | None = DIGEST_A  # None: skopeo cannot read the parent
        self.built_on: str | None = DIGEST_A  # None: the published image has no label

    def run(self, argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        self.argvs.append(argv)
        if argv[0] == "podman" and argv[-2:] == ["inspect", "job-ctr"]:
            mount = {"Destination": tempfile.gettempdir(), "Source": "/host/tmp"}
            return cp(
                0, json.dumps([{"Mounts": [mount], "ImageName": SDLC_REF, "Image": "c0ffee"}])
            )
        if argv == [check.SDLC, "launcher", SDLC_REF]:
            return cp(0, "#!/bin/sh\n")
        if ran_launcher(argv):
            self.launched.append((argv[1], dict(_kw.get("env") or {})))  # type: ignore[call-overload]
            return cp(self.gate_rc) if argv[1] == "ci" else cp(self.audit_rc, "CVE-1 in foo")
        if argv[:2] == ["skopeo", "inspect"] and "--format" in argv:
            return cp(0, self.base) if self.base else cp(1)
        if argv[:2] == ["skopeo", "inspect"]:
            labels = {check.BASE_LABEL: self.built_on} if self.built_on else {}
            return cp(0, json.dumps({"Labels": labels}))
        raise AssertionError(f"unexpected argv {argv}")

    def http(self, method: str, url: str, _headers: object, data: bytes | None = None) -> Response:
        path = url.removeprefix("https://api.github.com")
        self.calls.append((method, path, data))
        if method == "GET" and path.endswith("/pulls"):
            return Response(200, json.dumps(self.prs).encode())
        return Response(204 if method == "PUT" else 201, b"")


def pr(ref: str = "ashigaru/release-1.2.3", user: str = "ashigaru[bot]", repo: str = REPO) -> dict:
    return {
        "number": 7,
        "merged_at": "2026-11-23T00:00:00Z",
        "head": {"ref": ref, "repo": {"full_name": repo}},
        "user": {"login": user},
    }


def go(  # noqa: PLR0913
    event_name: str,
    tools: FakeTools,
    *,
    files: dict[str, str] | None = None,
    cron: str | None = None,
    ref: str = "refs/heads/main",
    sarif: bool = False,
) -> tuple[int, dict]:
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        ws.mkdir()
        for name, text in (files or {}).items():
            (ws / name).write_text(text)
        out = Path(tmp) / "out"
        env = {
            "GITHUB_WORKSPACE": str(ws),
            "HOSTNAME": "job-ctr",
            "GITHUB_OUTPUT": str(out),
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_SHA": SHA,
            "GITHUB_REF": ref,
            "SDLC_SARIF_DIR": str(Path(tmp) / "sarif"),
        }
        if sarif:
            (Path(tmp) / "sarif").mkdir()
            (Path(tmp) / "sarif" / "codeql-results.sarif").write_text('{"runs": [{}]}')
        event = dict(EVENT)
        if cron:
            event["schedule"] = cron
        rc = check.run(event_name, event, env, tools)  # type: ignore[arg-type]
        text = out.read_text() if out.exists() else ""
        lines = text.splitlines()
        plan = json.loads(lines[1]) if len(lines) > 1 else {}
        return rc, plan


IMAGE = {"Containerfile": "FROM quay.io/ubi/ubi10:latest\nRUN true\n"}
PYPI = {"pyproject.toml": '[tool.sdlc]\npublish = "pypi"\n'}


def uploads(tools: FakeTools) -> list[tuple[str, str, bytes | None]]:
    return [c for c in tools.calls if c[1].endswith("/code-scanning/sarifs")]


def test_pull_request() -> None:
    t = FakeTools()
    rc, plan = go("pull_request", t, files=IMAGE, ref="refs/pull/1/merge")
    assert rc == 0 and plan["image"] is False and plan["release"] is None
    assert plan["write"] is False and gated(t)
    assert not t.calls


def test_push_image_repo() -> None:
    rc, plan = go("push", FakeTools(), files=IMAGE)
    assert rc == 0 and plan["image"] is True and plan["release"] is None and not plan["write"]


def test_push_no_containerfile() -> None:
    assert go("push", FakeTools())[1]["image"] is False


def test_push_other_branch_is_not_default() -> None:
    assert go("push", FakeTools(), files=IMAGE, ref="refs/heads/topic")[1]["image"] is False


def test_release_merge() -> None:
    rc, plan = go("push", FakeTools(prs=[pr()]), files=PYPI)
    assert rc == 0 and plan["release"] == {"version": "1.2.3", "publish": "pypi", "pr": 7}
    assert plan["write"] is True and plan["image"] is False


def test_release_without_publish() -> None:
    plan = go("push", FakeTools(prs=[pr()]), files={"pyproject.toml": "[tool.sdlc]\n"})[1]
    assert plan["release"]["publish"] == ""


def test_not_a_release() -> None:
    for prs in (
        [pr(user="someone")],
        [pr(ref="feature/x")],
        [pr(ref="ashigaru/release-1.2")],
        [pr(ref="ashigaru/release-01.2.3")],
        [pr(repo="evil/fork")],
        [{**pr(), "merged_at": None}],
        [pr(), pr()],
        [],
    ):
        plan = go("push", FakeTools(prs=prs), files=PYPI)[1]
        assert plan["release"] is None and plan["write"] is False, prs


def test_weekly() -> None:
    t = FakeTools()
    rc, plan = go("schedule", t, files={**IMAGE, **PYPI}, cron=check.WEEKLY)
    assert rc == 0 and plan["weekly"] is True and plan["image"] is True
    assert plan["audit"]["found"] is False and plan["write"] is False
    assert ("PUT", f"/repos/{REPO}/actions/workflows/ci.yml/enable", None) in t.calls
    assert not gated(t)


def test_weekly_audit_found() -> None:
    t = FakeTools()
    t.audit_rc = 1
    plan = go("schedule", t, files=PYPI, cron=check.WEEKLY)[1]
    assert plan["audit"]["found"] is True and "CVE-1" in plan["audit"]["summary"]
    assert plan["write"] is True


def test_weekly_audit_cannot_run() -> None:
    t = FakeTools()
    t.audit_rc = 2
    try:
        go("schedule", t, files=PYPI, cron=check.WEEKLY)
    except CannotRunError:
        return
    raise AssertionError("an audit that could not run must not pass")


def test_poll_moved() -> None:
    t = FakeTools()
    t.base = DIGEST_B
    rc, plan = go("schedule", t, files=IMAGE, cron=check.POLL)
    assert rc == 0 and plan["image"] is True and plan["weekly"] is False and not plan["write"]
    assert [
        "skopeo",
        "inspect",
        "--format",
        "{{.Digest}}",
        "docker://quay.io/ubi/ubi10:latest",
    ] in (t.argvs)
    assert ["skopeo", "inspect", "docker://quay.io/crunchtools/demo:latest"] in t.argvs
    assert not any(c[0] == "PUT" for c in t.calls)


def test_poll_unchanged() -> None:
    assert go("schedule", FakeTools(), files=IMAGE, cron=check.POLL)[1]["image"] is False


def test_poll_unreadable() -> None:
    cases: list[Callable[[FakeTools], None]] = [
        lambda t: setattr(t, "base", None),
        lambda t: setattr(t, "built_on", None),
    ]
    for breakit in cases:
        t = FakeTools()
        breakit(t)
        try:
            go("schedule", t, files=IMAGE, cron=check.POLL)
        except CannotRunError:
            continue
        raise AssertionError("an unreadable digest must be a 2, never a quiet false")


def test_poll_no_containerfile() -> None:
    t = FakeTools()
    assert go("schedule", t, cron=check.POLL)[1]["image"] is False and not t.argvs


def test_poll_final_stage() -> None:
    text = "ARG B=reg/x:1\nFROM reg/builder:9 AS build\nFROM ${B} AS mid\nFROM mid\n"
    assert check.final_base(text) == "reg/x:1"
    assert check.final_base("FROM scratch\n") is None


def test_gate_failure_still_plans() -> None:
    for code, want in ((1, 1), (2, 2), (3, 2)):
        rc, plan = go("push", FakeTools(gate_rc=code, prs=[pr()]), files={**IMAGE, **PYPI})
        assert rc == want and plan["image"] is True and plan["release"]["version"] == "1.2.3"


def test_dispatch_gates_and_plans_nothing() -> None:
    t = FakeTools()
    rc, plan = go("workflow_dispatch", t, files=IMAGE)
    assert rc == 0 and plan["image"] is False and plan["release"] is None and not plan["write"]
    assert gated(t)


def test_sarif_only_on_default_push() -> None:
    t = FakeTools()
    go("push", t, sarif=True)
    sent = uploads(t)
    assert len(sent) == 1
    body = json.loads(sent[0][2] or b"")
    assert body["commit_sha"] == SHA and body["ref"] == "refs/heads/main"
    doc = json.loads(gzip.decompress(base64.b64decode(body["sarif"])))
    assert doc["runs"][0]["automationDetails"]["id"] == "/language:python/"
    for name, ref in (("pull_request", "refs/pull/1/merge"), ("push", "refs/heads/topic")):
        t = FakeTools()
        go(name, t, sarif=True, ref=ref)
        assert not uploads(t), name
    t = FakeTools()
    go("push", t)  # no file, nothing to upload
    assert not uploads(t)


def test_launcher_is_pinned_and_mounts_the_host_path() -> None:
    t = FakeTools()
    go("pull_request", t)
    assert [check.SDLC, "launcher", SDLC_REF] in t.argvs
    command, child = t.launched[0]
    assert command == "ci"
    assert child["SDLC_HOST_REPO"].startswith("/host/tmp/")
    assert child["CONTAINER_HOST"] == "unix:///var/run/docker.sock"


def test_launcher_refuses_an_unpinned_image() -> None:
    global SDLC_REF  # noqa: PLW0603
    saved, SDLC_REF = SDLC_REF, "quay.io/crunchtools/sdlc:1"
    msg = ""
    try:
        go("pull_request", FakeTools())
    except CannotRunError as exc:
        msg = str(exc)
    finally:
        SDLC_REF = saved
    assert "pinned" in msg, "an unpinned image ran the gates"


def main() -> int:
    tests = {n: f for n, f in globals().items() if n.startswith("test_") and callable(f)}
    failed = 0
    for name, fn in tests.items():
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
