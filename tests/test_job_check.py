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
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdlc_jobs import _core, check
from sdlc_jobs._core import CannotRunError, Response, Tools


def no_container_id(mountinfo: str = "") -> str:
    """The fake engine answers to HOSTNAME; a suite that itself runs in a
    container must not find its own id in mountinfo first."""
    del mountinfo
    return ""


_core.own_container_id = no_container_id

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


class FakeTools(Tools):
    def __init__(self, gate_rc: int = 0, prs: list | None = None) -> None:
        self.argvs: list[list[str]] = []
        self.launched: list[tuple[str, dict]] = []
        self.calls: list[tuple[str, str, bytes | None]] = []
        self.gate_rc = gate_rc
        self.prs = prs if prs is not None else []
        self.audit_rc = 0
        self.audit_out: str | None = None  # None: the report for audit_rc
        self.base: str | None = DIGEST_A  # None: skopeo cannot read the parent
        self.built_on: str | None = DIGEST_A  # None: the published image has no label
        self.image = SDLC_REF  # what this container says it was run from
        self.stdin: list[str | None] = []
        self.cwds: list[str | None] = []
        self.env: dict[str, str] = {}  # added to the job's environment
        self.trees: dict[str, str] = {}  # commit -> tree, for gated_on_pr
        self.jobs: list[dict] = []  # the jobs of the merged PR head's ci.yml run
        self.head_repo = ""  # a pull request's head repo, in the event
        self.private = False  # the event's repository.private

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
        self.cwds.append(cwd)
        if argv[0] == "podman" and argv[-2:] == ["inspect", "job-ctr"]:
            mount = {"Destination": tempfile.gettempdir(), "Source": "/host/tmp"}
            return cp(
                0, json.dumps([{"Mounts": [mount], "ImageName": self.image, "Image": "c0ffee"}])
            )
        if argv == [check.SDLC, "launcher", self.image]:
            return cp(0, "#!/bin/sh\n")
        if ran_launcher(argv):
            self.launched.append((argv[1], dict(env or {})))
            if argv[1] == "ci":
                return cp(self.gate_rc)
            assert argv[2:] == ["--", "--report"], argv
            return cp(self.audit_rc, self.report() if self.audit_out is None else self.audit_out)
        return self.tool(argv)

    def tool(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        """The image's other tools: git's fetch, and skopeo reading the parent."""
        if argv[0] == "git":
            return cp(0)
        if argv[:2] == ["skopeo", "inspect"]:
            if "--format" in argv:
                return cp(0, self.base) if self.base else cp(1)
            labels = {check.BASE_LABEL: self.built_on} if self.built_on else {}
            return cp(0, json.dumps({"Labels": labels}))
        raise AssertionError(f"unexpected argv {argv}")

    def report(self) -> str:
        """What `sdlc pip-audit -- --report` prints: its log, then the report line."""
        vulns = [{"id": "CVE-1", "fix_versions": ["2.0"]}] if self.audit_rc == 1 else []
        deps = [{"name": "foo", "version": "1.0", "vulns": vulns}]
        doc = json.dumps({"dependencies": deps})
        return f"{check.AUDIT_REPORT}forged by the repo\nFound 1\n{check.AUDIT_REPORT}{doc}\n"

    def http(
        self, method: str, url: str, _headers: object, payload: bytes | None = None
    ) -> Response:
        path = url.removeprefix("https://api.github.com")
        self.calls.append((method, path, payload))
        if method == "GET" and path.endswith("/pulls"):
            return Response(200, json.dumps(self.prs).encode())
        if method == "GET" and "/git/commits/" in path:
            tree = self.trees[path.rpartition("/")[2]]
            return Response(200, json.dumps({"tree": {"sha": tree}}).encode())
        if method == "GET" and "/actions/runs?" in path:
            run = {
                "id": 9,
                "path": ".github/workflows/ci.yml",
                "head_repository": {"full_name": REPO},
            }
            return Response(200, json.dumps({"workflow_runs": [run]}).encode())
        if method == "GET" and path.endswith("/actions/runs/9/jobs"):
            return Response(200, json.dumps({"jobs": self.jobs}).encode())
        return Response(204 if method == "PUT" else 201, b"")


def pr(ref: str = "ashigaru/release-1.2.3", user: str = "ashigaru[bot]", repo: str = REPO) -> dict:
    return {
        "number": 7,
        "merged_at": "2026-11-23T00:00:00Z",
        "head": {"ref": ref, "repo": {"full_name": repo}},
        "user": {"login": user},
    }


def go(
    event_name: str,
    tools: FakeTools,
    *,
    files: dict[str, str] | None = None,
    cron: str | None = None,
    ref: str = "refs/heads/main",
) -> tuple[int, dict]:
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        ws.mkdir()
        for name, text in (files or {}).items():
            (ws / name).parent.mkdir(parents=True, exist_ok=True)
            (ws / name).write_text(text)
        out = Path(tmp) / "out"
        env = {
            "GITHUB_WORKSPACE": str(ws),
            "HOSTNAME": "job-ctr",
            "GITHUB_OUTPUT": str(out),
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_SHA": SHA,
            "GITHUB_REF": ref,
            "SDLC_SARIF_DIR": str(ws / "sarif"),
            **tools.env,
        }
        event = dict(EVENT)
        if tools.head_repo:
            event["pull_request"] = {"head": {"repo": {"full_name": tools.head_repo}}}
            event["repository"] = {**event.get("repository", {}), "private": tools.private}
        if cron:
            event["schedule"] = cron
        rc = check.run(event_name, event, env, tools)
        text = out.read_text() if out.exists() else ""
        lines = text.splitlines()
        plan = json.loads(lines[1]) if len(lines) > 1 else {}
        return rc, plan


IMAGE = {"Containerfile": "FROM quay.io/ubi/ubi10:latest\nRUN true\n"}
SARIF = {"sarif/codeql-results.sarif": '{"runs": [{}]}'}  # what the CodeQL gate leaves
PYPI = {"pyproject.toml": '[tool.sdlc]\npublish = "pypi"\n'}


def refusal(event_name: str, tools: FakeTools, **kwargs: Any) -> str:
    """The message of the CannotRunError `go` raises, or a failed assertion."""
    try:
        go(event_name, tools, **kwargs)
    except CannotRunError as exc:
        return str(exc)
    raise AssertionError("expected a refusal (exit 2)")


def uploads(tools: FakeTools) -> list[tuple[str, str, bytes | None]]:
    return [c for c in tools.calls if c[1].endswith("/code-scanning/sarifs")]


def test_other_events_are_refused() -> None:
    for event_name in ("issues", "pull_request_target", "pull_request_review_comment"):
        t = FakeTools()
        assert event_name in refusal(event_name, t)
        assert not t.argvs and not t.calls


def test_pull_request() -> None:
    t = FakeTools()
    rc, plan = go("pull_request", t, files=IMAGE, ref="refs/pull/1/merge")
    assert rc == 0 and plan["image"] is False and plan["release"] is None
    assert plan["write"] is False and gated(t)
    assert not t.calls


def test_same_repo_pull_request_plans_a_candidate_and_leaves_the_container_gates_to_it() -> None:
    t = FakeTools()
    t.head_repo = REPO
    rc, plan = go("pull_request", t, files=IMAGE, ref="refs/pull/1/merge")
    assert rc == 0 and plan["candidate"] is True and gated(t)
    skip = t.launched[0][1].get("SDLC_SKIP", "").split(",")
    assert set(check.CONTAINER_GATES) <= set(skip), skip


def test_no_candidate_for_a_private_repo() -> None:
    t = FakeTools()
    t.head_repo, t.private = REPO, True
    rc, plan = go("pull_request", t, files=IMAGE, ref="refs/pull/1/merge")
    assert rc == 0 and plan["candidate"] is False
    assert "SDLC_SKIP" not in t.launched[0][1]


def test_no_candidate_for_a_fork_or_rhsm_or_no_image() -> None:
    rhsm = {**IMAGE, "pyproject.toml": "[tool.sdlc.image]\nrhsm = true\n"}
    for files, head in ((IMAGE, "fork/demo"), (rhsm, REPO), ({}, REPO)):
        t = FakeTools()
        t.head_repo = head
        rc, plan = go("pull_request", t, files=files, ref="refs/pull/1/merge")
        assert rc == 0 and plan["candidate"] is False, (files, head)
        assert "SDLC_SKIP" not in t.launched[0][1], (files, head)


def merged(head_tree: str, conclusion: str = "success") -> FakeTools:
    p = pr(ref="fix/x")
    p["head"]["sha"] = "e" * 40
    t = FakeTools(prs=[p])
    t.trees = {SHA: "c" * 40, "e" * 40: head_tree}
    t.jobs = [{"name": check.REQUIRED_CHECK, "conclusion": conclusion}]
    return t


def test_push_of_a_tree_its_pr_passed_runs_no_gates() -> None:
    t = merged("c" * 40)
    rc, plan = go("push", t, files=IMAGE)
    assert rc == 0 and plan["image"] is True and not gated(t)


def test_push_gates_when_the_tree_or_the_check_differs() -> None:
    for t in (merged("d" * 40), merged("c" * 40, "failure"), FakeTools()):
        rc, _ = go("push", t, files=IMAGE)
        assert rc == 0 and gated(t)


def test_push_image_repo() -> None:
    rc, plan = go("push", FakeTools(), files=IMAGE)
    assert rc == 0 and plan["image"] is True and plan["release"] is None and not plan["write"]


def test_push_no_containerfile() -> None:
    assert go("push", FakeTools())[1]["image"] is False


def test_push_other_branch_is_not_default() -> None:
    assert go("push", FakeTools(), files=IMAGE, ref="refs/heads/topic")[1]["image"] is False


def test_release_merge() -> None:
    rc, plan = go("push", FakeTools(prs=[pr()]), files=PYPI)
    assert rc == 0 and plan["release"] == {"version": "1.2.3", "publish": "pypi"}
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
    # A clean report still goes to `write`: it closes the alert issue.
    assert plan["audit"]["dependencies"][0]["vulns"] == [] and plan["write"] is True
    assert ("PUT", f"/repos/{REPO}/actions/workflows/ci.yml/enable", None) in t.calls
    assert not gated(t)


def test_weekly_audit_found() -> None:
    t = FakeTools()
    t.audit_rc = 1
    plan = go("schedule", t, files=PYPI, cron=check.WEEKLY)[1]
    assert plan["audit"]["dependencies"][0]["vulns"][0]["id"] == "CVE-1"
    assert plan["write"] is True


def audit_of(deps: list[dict]) -> str:
    return f"{check.AUDIT_REPORT}{json.dumps({'dependencies': deps})}\n"


def test_big_audit_keeps_only_the_vulnerable() -> None:
    clean = [
        {"name": f"pkg{i}", "version": "1.0", "vulns": [], "pad": "x" * 200} for i in range(6000)
    ]
    bad = {"name": "foo", "version": "1.0", "vulns": [{"id": "CVE-1", "fix_versions": ["2.0"]}]}
    t = FakeTools()
    t.audit_out = audit_of([*clean, bad])
    rc, plan = go("schedule", t, files={"pyproject.toml": "[project]\n"}, cron=check.WEEKLY)
    assert rc == 0 and plan["audit"]["dependencies"] == [bad]


def test_small_audit_is_left_whole() -> None:
    deps = [{"name": "foo", "version": "1.0", "vulns": []}]
    t = FakeTools()
    t.audit_out = audit_of(deps)
    _, plan = go("schedule", t, files={"pyproject.toml": "[project]\n"}, cron=check.WEEKLY)
    assert plan["audit"]["dependencies"] == deps


def test_audit_too_big_even_when_cut_cannot_run() -> None:
    vuln = {"id": "CVE-1", "fix_versions": ["x" * 1000]}
    deps = [{"name": f"p{i}", "version": "1", "vulns": [vuln] * 10} for i in range(100)]
    t = FakeTools()
    t.audit_out = audit_of(deps)
    msg = refusal("schedule", t, files={"pyproject.toml": "[project]\n"}, cron=check.WEEKLY)
    assert "bytes" in msg


def test_oversized_plan_without_an_audit_cannot_run() -> None:
    """The cap holds with no audit to cut: an oversized plan is a 2, not a lost output."""
    plan = {"image": False, "release": {"version": "x" * (check.PLAN_LIMIT + 1)}, "audit": None}
    msg = ""
    try:
        check.fit(plan)
    except CannotRunError as exc:
        msg = str(exc)
    assert "bytes" in msg, "an oversized plan with no audit must not pass"


def test_weekly_no_python_project_audits_nothing() -> None:
    t = FakeTools()
    plan = go("schedule", t, files=IMAGE, cron=check.WEEKLY)[1]
    assert plan["audit"] is None and plan["write"] is False and not gated(t, "pip-audit")


def test_weekly_audit_without_a_report_cannot_run() -> None:
    for out in (
        "Found 1\n",
        f"{check.AUDIT_REPORT}not json\n",
        f'{check.AUDIT_REPORT}{{"x": 1}}\n',
    ):
        t = FakeTools()
        t.audit_out = out
        try:
            go("schedule", t, files=PYPI, cron=check.WEEKLY)
        except CannotRunError:
            continue
        raise AssertionError(f"an audit with no report must not pass: {out!r}")


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
    assert go("schedule", t, cron=check.POLL)[1]["image"] is False
    assert not [a for a in t.argvs if a[0] != "git"], "polled with no Containerfile"


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
    go("push", t, files=SARIF)
    sent = uploads(t)
    assert len(sent) == 1
    body = json.loads(sent[0][2] or b"")
    assert body["commit_sha"] == SHA and body["ref"] == "refs/heads/main"
    doc = json.loads(gzip.decompress(base64.b64decode(body["sarif"])))
    assert doc["runs"][0]["automationDetails"]["id"] == "/language:python/"
    for name, ref in (("pull_request", "refs/pull/1/merge"), ("push", "refs/heads/topic")):
        t = FakeTools()
        go(name, t, files=SARIF, ref=ref)
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
    t = FakeTools()
    t.image = "quay.io/crunchtools/sdlc:1"
    msg = ""
    try:
        go("pull_request", t)
    except CannotRunError as exc:
        msg = str(exc)
    assert "pinned" in msg, "an unpinned image ran the gates"


def test_fetch_tree_checks_out_the_event_commit() -> None:
    t = FakeTools()
    go("pull_request", t)
    gits = [a for a in t.argvs if a[0] == "git"]
    assert gits[0] == ["git", "init", "-q", "."], gits
    assert gits[1] == ["git", "remote", "add", "origin", f"https://github.com/{REPO}"], gits
    assert gits[2] == ["git", "fetch", "-q", "--depth=1", "origin", SHA], gits
    assert gits[3] == ["git", "checkout", "-q", "FETCH_HEAD"], gits


def test_fetch_tree_leaves_an_existing_tree() -> None:
    t = FakeTools()
    go("pull_request", t, files={".git/HEAD": "ref: refs/heads/main\n"})
    assert not [a for a in t.argvs if a[0] == "git"], "fetched over an existing tree"


def test_fetch_tree_needs_the_commit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        msg = ""
        try:
            check.fetch_tree(FakeTools(), {}, Path(tmp), REPO)
        except CannotRunError as exc:
            msg = str(exc)
    assert "GITHUB_SHA" in msg, "fetched with no commit named"


def test_launcher_cache_without_runner_temp_is_unmapped() -> None:
    t = FakeTools()
    go("pull_request", t)
    _command, child = t.launched[0]
    assert "SDLC_HOST_CACHE" not in child, "a cache mapped without RUNNER_TEMP"


def test_launcher_cache_is_named_by_its_host_path() -> None:
    t = FakeTools()
    with tempfile.TemporaryDirectory() as tmp:
        t.env["RUNNER_TEMP"] = tmp
        go("pull_request", t)
    _command, child = t.launched[0]
    assert child["XDG_CACHE_HOME"].endswith("/sdlc-cache"), child
    assert child["SDLC_HOST_CACHE"].startswith("/host/tmp/"), child
    assert child["SDLC_HOST_CACHE"].endswith("/sdlc-cache"), child


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
