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
import contextlib
import gzip
import io
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from http import HTTPStatus
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
BASE_SHA = "b" * 40
DEFAULT_TIP = "d" * 40
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
        self.review_rc = 0  # what the launcher's `gourmand` exits with
        self.prs = prs if prs is not None else []
        self.audit_rc = 0
        self.audit_out: str | None = None  # None: the report for audit_rc
        self.base: str | None = DIGEST_A  # None: skopeo cannot read the parent
        self.built_on: str | None = DIGEST_A  # None: the published image has no label
        self.image = SDLC_REF  # what this container says it was run from
        self.stdin: list[str | None] = []
        self.cwds: list[str | None] = []
        self.envs: list[dict[str, str]] = []  # each run's env, in argvs' order
        self.env: dict[str, str] = {}  # added to the job's environment
        self.trees: dict[str, str] = {}  # commit -> tree, for gated_on_pr
        self.jobs: list[dict] = []  # the jobs of the merged PR head's ci.yml run
        self.runs_status = 200  # what listing the merged PR head's Actions runs answers
        self.pr_base = BASE_SHA  # the pull request's base.sha, in the event
        self.before: str | None = None  # a push's `before`, in the event
        self.head_repo = ""  # a pull request's head repo, in the event
        self.private = False  # the event's repository.private
        self.tags = f"{SHA}\trefs/tags/v1.2.3\n"  # `git ls-remote --tags origin`: a release here
        self.pr_files: list[dict] | None = [{"filename": "src/x.py"}]  # None: the API refuses
        self.sarif: dict[str, str] = {}  # what `sdlc sarif` writes, file name -> text
        self.sarif_rc = 0
        self.planned: list[str] | None = None  # the planner's list; None: what `sarif` names
        self.upload_status = 202
        self.upload_body = b'{"message": "refused"}'

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
        self.envs.append(dict(env or {}))
        if argv[0] == "podman" and argv[-2:] == ["inspect", "job-ctr"]:
            mount = {"Destination": tempfile.gettempdir(), "Source": "/host/tmp"}
            return cp(
                0, json.dumps([{"Mounts": [mount], "ImageName": self.image, "Image": "c0ffee"}])
            )
        if argv == [check.SDLC, "launcher", self.image]:
            return cp(0, "#!/bin/sh\n")
        if ran_launcher(argv):
            return self.launcher(argv, env)
        return self.tool(argv)

    def launcher(
        self, argv: list[str], env: Mapping[str, str] | None
    ) -> subprocess.CompletedProcess[str]:
        """The launcher's targets: the gates, Gourmand, the SARIF scan, the audit."""
        self.launched.append((argv[1], dict(env or {})))
        if argv[1] == "ci":
            return cp(self.gate_rc)
        if argv[1] == check.REVIEW:
            assert argv[2:] == [], argv
            return cp(self.review_rc)
        if argv[1] == "sarif":
            # The launcher mounts the host path; this container sees it under its temp.
            host = (env or {})["SDLC_HOST_SARIF"]
            out = Path(tempfile.gettempdir()) / Path(host).relative_to("/host/tmp")
            for name, text in self.sarif.items():
                (out / name).write_text(text)
            files = {f: c for c, (_, f) in check.SARIF_FILES.items()}
            planned = self.planned
            if planned is None:
                planned = [files[name] for name in self.sarif]
            if planned != ["-"]:  # "-": the planner wrote no list
                (out / check.SARIF_PLANNED).write_text("".join(f"{c}\n" for c in planned))
            return cp(self.sarif_rc)
        assert argv[2:] == ["--", "--report"], argv
        return cp(self.audit_rc, self.report() if self.audit_out is None else self.audit_out)

    def tool(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        """The image's other tools: git's fetch, and skopeo reading the parent."""
        if argv[:2] == ["git", "ls-remote"]:
            return cp(0, self.tags)
        if argv[:3] == ["git", "rev-parse", "FETCH_HEAD"]:
            return cp(0, DEFAULT_TIP + "\n")
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
        if method == "GET" and "/pulls/1/files?" in path:
            listed = None if self.pr_files is None else json.dumps(self.pr_files).encode()
            return Response(403, b"") if listed is None else Response(200, listed)
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
            listed = {"workflow_runs": [run]} if self.runs_status == HTTPStatus.OK else {}
            return Response(self.runs_status, json.dumps(listed).encode())
        if method == "GET" and path.endswith("/actions/runs/9/jobs"):
            return Response(200, json.dumps({"jobs": self.jobs}).encode())
        return self.write(method, path)

    def write(self, method: str, path: str) -> Response:
        """A POST or PUT: the SARIF upload answers `upload_status`, the rest succeed."""
        if method == "POST" and path.endswith("/code-scanning/sarifs"):
            return Response(self.upload_status, self.upload_body)
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
            "RUNNER_TEMP": str(Path(tmp) / "runner-temp"),
            **tools.env,
        }
        event = dict(EVENT)
        if event_name == "pull_request":
            head = {"repo": {"full_name": tools.head_repo or REPO}}
            event["pull_request"] = {"number": 1, "head": head, "base": {"sha": tools.pr_base}}
            event["repository"] = {**event.get("repository", {}), "private": tools.private}
        if tools.before is not None:
            event["before"] = tools.before
        if cron:
            event["schedule"] = cron
        rc = check.run(event_name, event, env, tools)
        text = out.read_text() if out.exists() else ""
        lines = text.splitlines()
        plan = json.loads(lines[1]) if len(lines) > 1 else {}
        return rc, plan


IMAGE = {"Containerfile": "FROM quay.io/ubi/ubi10:latest\nRUN true\n"}
SARIF = {"codeql-results.sarif": '{"runs": [{}]}', "semgrep.sarif": '{"runs": [{}]}'}
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
    assert plan["write"] is False and gated(t) and "candidate" not in plan
    assert [c[1] for c in t.calls] == [f"/repos/{REPO}/pulls/1/files?per_page=100&page=1"]


def skipped(t: FakeTools) -> set[str]:
    return set(t.launched[0][1].get("SDLC_SKIP", "").split(",")) - {""}


def test_pull_request_test_builds_only_when_the_image_inputs_change() -> None:
    t = FakeTools()
    go("pull_request", t, files=IMAGE, ref="refs/pull/1/merge")
    assert set(check.CONTAINER_GATES) <= skipped(t), "a code-only PR built the image"
    for name in (
        "Containerfile",
        "images/Containerfile.base",
        "Dockerfile",
        ".containerignore",
        "uv.lock",
        "web/package-lock.json",
        "requirements-dev.txt",
    ):
        t = FakeTools()
        t.pr_files = [{"filename": "README.md"}, {"filename": name}]
        go("pull_request", t, files=IMAGE, ref="refs/pull/1/merge")
        assert not skipped(t), name
    t = FakeTools()
    t.pr_files = [{"filename": "new/Containerfile", "previous_filename": "Containerfile"}]
    go("pull_request", t, files=IMAGE, ref="refs/pull/1/merge")
    assert not skipped(t), "a renamed Containerfile is a change to it"


def pr_build_paths(*globs: str) -> dict[str, str]:
    """The IMAGE repo, with [tool.sdlc.image] pr-build-paths in its sdlc.toml."""
    listed = ", ".join(f'"{g}"' for g in globs)
    return {**IMAGE, "sdlc.toml": f"[tool.sdlc.image]\npr-build-paths = [{listed}]\n"}


def test_pr_build_paths_add_to_the_image_inputs() -> None:
    """#186: an image that copies deps/ in test-builds on a deps/-only pull request,
    but only when the repo says so; the default inputs still count."""
    for files, changed, builds in (
        (IMAGE, "deps/openbao.tgz", False),
        (pr_build_paths("deps/**", "rootfs/**"), "deps/openbao.tgz", True),
        (pr_build_paths("deps/**", "rootfs/**"), "rootfs/etc/.hidden", True),
        (pr_build_paths("deps/**", "rootfs/**"), "src/deps/a.py", False),
        (pr_build_paths("deps/**", "rootfs/**"), "src/app.py", False),
        (pr_build_paths("scripts/*.sh"), "scripts/build.sh", True),
        (pr_build_paths("scripts/*.sh"), "scripts/lib/x.sh", False),
        (pr_build_paths("deps/**"), "uv.lock", True),
    ):
        t = FakeTools()
        t.pr_files = [{"filename": "README.md"}, {"filename": changed}]
        go("pull_request", t, files=files, ref="refs/pull/1/merge")
        assert (not skipped(t)) is builds, (changed, files.get("sdlc.toml"))
    t = FakeTools()
    t.pr_files = [{"filename": "vendor/x", "previous_filename": "deps/x"}]
    go("pull_request", t, files=pr_build_paths("deps/**"), ref="refs/pull/1/merge")
    assert not skipped(t), "a file renamed out of deps/ is a change to deps/"


def test_pr_build_paths_must_be_globs_inside_the_repo() -> None:
    for toml in (
        'pr-build-paths = "deps/**"',
        "pr-build-paths = [1]",
        'pr-build-paths = [""]',
        'pr-build-paths = ["/etc/**"]',
        'pr-build-paths = ["../other/**"]',
        'pr-build-paths = ["deps/../../x"]',
        'pr-build-paths = ["deps\\\\x"]',
        "pr-build-paths = [" + ", ".join(f'"d{n}/**"' for n in range(51)) + "]",
    ):
        files = {**IMAGE, "sdlc.toml": f"[tool.sdlc.image]\n{toml}\n"}
        assert "pr-build-paths" in refusal("pull_request", FakeTools(), files=files), toml


def test_pull_request_whose_files_cannot_be_read_test_builds() -> None:
    t = FakeTools()
    t.pr_files = None
    go("pull_request", t, files=IMAGE, ref="refs/pull/1/merge")
    assert not skipped(t)
    t = FakeTools()
    t.pr_files = [{"filename": f"src/{n}.py"} for n in range(100)]  # a full page, then none
    go("pull_request", t, files=IMAGE, ref="refs/pull/1/merge")
    assert not skipped(t), "only a short page ends the listing"


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
    assert rc == 0 and plan["image"] is False and not gated(t)


def test_push_gates_when_the_tree_or_the_check_differs() -> None:
    for t in (merged("d" * 40), merged("c" * 40, "failure"), FakeTools()):
        rc, _ = go("push", t, files=IMAGE)
        assert rc == 0 and gated(t)


def test_push_gates_when_the_token_may_not_list_actions_runs() -> None:
    # local-release.sh's App token on a private repo: 403, no evidence, every gate.
    t = merged("c" * 40)
    t.runs_status = 403
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc, plan = go("push", t, files=IMAGE)
    assert rc == 0 and gated(t), plan
    assert "may not read crunchtools/demo's Actions runs (403); no PR evidence" in out.getvalue()


def test_push_other_actions_runs_failures_are_still_a_2() -> None:
    for status in (401, 404, 500):
        t = merged("c" * 40)
        t.runs_status = status
        msg = refusal("push", t, files=IMAGE)
        assert f"answered {status}" in msg and "/actions/runs?" in msg, (status, msg)


def test_push_image_repo_builds_only_a_release() -> None:
    rc, plan = go("push", FakeTools(), files=IMAGE)
    assert rc == 0 and plan["image"] is False and plan["release"] is None and not plan["write"]
    rc, plan = go("push", FakeTools(prs=[pr()]), files=IMAGE)
    assert rc == 0 and plan["image"] is True and plan["release"]["version"] == "1.2.3"


def test_rebuilds_publish_only_a_release_commit() -> None:
    for cron in (check.WEEKLY, check.POLL):
        for tags in ("", f"{'f' * 40}\trefs/tags/v1.2.3\n", f"{SHA}\trefs/tags/nightly\n"):
            t = FakeTools()
            t.tags, t.base = tags, DIGEST_B
            plan = go("schedule", t, files=IMAGE, cron=cron)[1]
            assert plan["image"] is False, (cron, tags)
        t = FakeTools()
        t.tags, t.base = f"{'f' * 40}\trefs/tags/v1.2.3\n{SHA}\trefs/tags/v1.2.3^{{}}\n", DIGEST_B
        assert go("schedule", t, files=IMAGE, cron=cron)[1]["image"] is True, "an annotated tag"


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


def launched(t: FakeTools) -> list[str]:
    return [c for c, _ in t.launched]


def test_a_fleet_pull_request_runs_gourmand_after_the_gates() -> None:
    """#177: the cutover deleted every gourmand.yml and `sdlc ci` leaves Gourmand out."""
    t = FakeTools()
    rc, _ = go("pull_request", t, ref="refs/pull/1/merge")
    assert rc == 0 and launched(t) == ["ci", check.REVIEW]
    _, child = t.launched[1]
    assert child["SDLC_HOST_REPO"].startswith("/host/tmp/")  # the same launcher, nested


def test_gourmand_runs_wherever_the_gates_run() -> None:
    for name, ref in (
        ("push", "refs/heads/main"),
        ("push", "refs/heads/topic"),
        ("workflow_dispatch", "refs/heads/main"),
    ):
        t = FakeTools()
        go(name, t, ref=ref)
        scan = ["sarif"] if ref == "refs/heads/main" and name == "push" else []
        assert launched(t) == ["ci", check.REVIEW, *scan], (name, ref)


def test_no_gates_no_gourmand() -> None:
    t = merged("c" * 40)  # a push of the tree its pull request passed
    go("push", t, files=IMAGE)
    assert check.REVIEW not in launched(t)
    t = FakeTools()
    go("schedule", t, cron=check.POLL, files=IMAGE)
    assert check.REVIEW not in launched(t)


def test_gourmand_verdict_joins_the_gates() -> None:
    for gate_rc, review_rc, want in ((0, 1, 1), (1, 0, 1), (0, 2, 2), (0, 3, 2), (1, 2, 2)):
        t = FakeTools(gate_rc=gate_rc)
        t.review_rc = review_rc
        rc, _ = go("pull_request", t, ref="refs/pull/1/merge")
        assert rc == want, (gate_rc, review_rc)
        assert launched(t) == ["ci", check.REVIEW]  # a failed gate still gets reviewed


def test_a_repo_that_skips_gourmand_on_push_is_not_reviewed() -> None:
    skip = {"pyproject.toml": '[tool.sdlc.push]\nskip = ["gourmand", "semgrep"]\n'}
    t = FakeTools()
    rc, _ = go("pull_request", t, files=skip, ref="refs/pull/1/merge")
    assert rc == 0 and launched(t) == ["ci"]
    other = {"pyproject.toml": '[tool.sdlc.push]\nskip = ["semgrep"]\n'}
    t = FakeTools()
    go("pull_request", t, files=other, ref="refs/pull/1/merge")
    assert launched(t) == ["ci", check.REVIEW]


def sent_categories(tools: FakeTools) -> list[str]:
    """The category each upload carries, in upload order."""
    out = []
    for _, _, payload in uploads(tools):
        body = json.loads(payload or b"")
        assert body["commit_sha"] == SHA and body["ref"] == "refs/heads/main"
        doc = json.loads(gzip.decompress(base64.b64decode(body["sarif"])))
        out += [r["automationDetails"]["id"] for r in doc["runs"]]
    return out


def scanned(tools: FakeTools) -> bool:
    return gated(tools, "sarif")


def test_sarif_on_default_push_after_the_gates() -> None:
    t = FakeTools()
    t.sarif = SARIF
    rc, _ = go("push", t)
    assert rc == 0 and gated(t) and scanned(t)
    assert launched(t) == ["ci", check.REVIEW, "sarif"]
    assert sent_categories(t) == ["/language:python/", "semgrep/"]


def test_no_scan_off_the_default_branch() -> None:
    for name, ref in (("pull_request", "refs/pull/1/merge"), ("push", "refs/heads/topic")):
        t = FakeTools()
        t.sarif = SARIF
        go(name, t, ref=ref)
        assert not scanned(t) and not uploads(t), name


def test_only_the_scan_gets_the_sarif_mount() -> None:
    """`sdlc ci` runs the repo's code; only `sdlc sarif`, which runs none, may write the upload."""
    t = FakeTools()
    go("push", t)
    mounts = {command: child.get("SDLC_HOST_SARIF") for command, child in t.launched}
    assert mounts["ci"] is None
    assert mounts["sarif"].startswith("/host/tmp/") and mounts["sarif"].endswith(check.SARIF_DIR)


def test_a_push_its_pr_gated_still_scans_and_its_findings_do_not_fail_it() -> None:
    t = merged("c" * 40)
    t.sarif, t.sarif_rc = SARIF, 1
    rc, _ = go("push", t)
    assert rc == 0 and not gated(t) and scanned(t)
    assert sent_categories(t) == ["/language:python/", "semgrep/"]


def test_a_scan_that_could_not_run_is_an_error_annotation_on_a_push() -> None:
    """The gates already judged the tree: a broken scan is reported, not a held release."""
    for t in (merged("c" * 40), FakeTools()):
        t.sarif_rc = 3
        rc, out = printed("push", t)
        assert rc == check.CLEAN and scanned(t)
        assert "::error::the code-scanning scan did not complete" in out


def test_a_push_whose_gates_found_still_fails_whatever_the_scan() -> None:
    t = FakeTools(gate_rc=1)
    t.sarif_rc = 3
    assert printed("push", t)[0] == check.FOUND


def test_weekly_scans_and_its_verdict_is_the_scan() -> None:
    for scan_rc, want in ((0, 0), (1, 1), (3, 2)):
        t = FakeTools()
        t.sarif, t.sarif_rc = SARIF, scan_rc
        rc, _ = go("schedule", t, files=PYPI, cron=check.WEEKLY)
        assert rc == want and scanned(t) and not gated(t), scan_rc
        assert sent_categories(t) == ["/language:python/", "semgrep/"]


def test_the_parent_poll_does_not_scan() -> None:
    t = FakeTools()
    go("schedule", t, files=IMAGE, cron=check.POLL)
    assert not scanned(t) and not uploads(t)


def test_nothing_written_is_nothing_uploaded() -> None:
    t = FakeTools()
    go("push", t)
    assert scanned(t) and not uploads(t)


def test_a_file_from_an_earlier_run_is_not_uploaded() -> None:
    t = FakeTools()
    with tempfile.TemporaryDirectory() as tmp:
        stale = Path(tmp) / check.SARIF_DIR
        stale.mkdir()
        (stale / "codeql-results.sarif").write_text('{"runs": [{}]}')
        t.env = {"RUNNER_TEMP": tmp}
        go("push", t)
    assert scanned(t) and not uploads(t)


def printed(event_name: str, tools: FakeTools) -> tuple[int, str]:
    """`go`, with what the job printed."""
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rc, _ = go(event_name, tools)
    return rc, out.getvalue()


def test_a_refused_upload_is_a_warning_not_a_failure() -> None:
    t = FakeTools()
    t.sarif, t.upload_status = SARIF, 403
    rc, out = printed("push", t)
    assert rc == 0 and len(uploads(t)) == len(SARIF)
    assert "::warning::uploading codeql-results.sarif answered 403" in out


def weekly_printed(tools: FakeTools) -> tuple[int, str]:
    """The weekly run, with what the job printed."""
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rc, _ = go("schedule", tools, files=PYPI, cron=check.WEEKLY)
    return rc, out.getvalue()


def test_a_refused_upload_fails_the_weekly_run() -> None:
    """The weekly scan is the verdict, and a scan nobody received did not report."""
    for status in (403, 422, 500):
        t = FakeTools()
        t.sarif, t.upload_status = SARIF, status
        rc, out = weekly_printed(t)
        assert rc == check.CANNOT_RUN and len(uploads(t)) == len(SARIF), status
        assert f"::error::uploading codeql-results.sarif answered {status}" in out


def test_code_scanning_not_offered_is_not_a_weekly_failure() -> None:
    """A private repo without Advanced Security has nowhere to send it; nothing to fix."""
    for said in (
        b'{"message": "Advanced Security must be enabled for this repository to use code '
        b'scanning."}',
        b'{"message": "Code Security must be enabled for this repository to use code scanning."}',
    ):
        t = FakeTools()
        t.sarif, t.upload_status, t.upload_body = SARIF, 403, said
        rc, out = weekly_printed(t)
        assert rc == check.CLEAN and "::warning::code scanning is not offered here" in out
        assert "::error::" not in out


def test_unreadable_sarif_fails_the_weekly_run() -> None:
    t = FakeTools()
    t.sarif = {"codeql-results.sarif": "not json", "semgrep.sarif": '{"runs": [{}]}'}
    rc, out = weekly_printed(t)
    assert rc == check.CANNOT_RUN and sent_categories(t) == ["semgrep/"]
    assert "::error::codeql-results.sarif is not readable SARIF" in out


def test_sarif_without_a_list_of_run_objects_fails_the_weekly_run() -> None:
    for shape in ("[]", '{"runs": {}}', '{"runs": [1]}', '{"runs": [{}, "x"]}'):
        t = FakeTools()
        t.sarif = {"codeql-results.sarif": shape, "semgrep.sarif": '{"runs": [{}]}'}
        rc, out = weekly_printed(t)
        assert rc == check.CANNOT_RUN and sent_categories(t) == ["semgrep/"], shape
        assert "::error::codeql-results.sarif has no SARIF runs, not uploaded" in out, shape


def test_a_planned_check_with_no_sarif_fails_the_weekly_run() -> None:
    for scan_rc in (0, 1):
        t = FakeTools()
        t.sarif, t.sarif_rc = {"semgrep.sarif": '{"runs": [{}]}'}, scan_rc
        t.planned = ["codeql", "semgrep"]
        rc, out = weekly_printed(t)
        assert rc == check.CANNOT_RUN and sent_categories(t) == ["semgrep/"], scan_rc
        assert "::error::codeql ran under sdlc sarif and wrote no codeql-results.sarif" in out


def test_a_planned_check_with_no_sarif_is_an_error_annotation_on_a_push() -> None:
    t = FakeTools()
    t.sarif, t.planned = {}, ["codeql"]
    rc, out = printed("push", t)
    assert rc == check.CLEAN
    assert "::error::codeql ran under sdlc sarif and wrote no codeql-results.sarif" in out
    assert "::error::the code-scanning scan did not complete" in out


def test_no_list_of_what_was_planned_fails_the_weekly_run() -> None:
    t = FakeTools()
    t.sarif, t.planned = SARIF, ["-"]
    rc, out = weekly_printed(t)
    assert rc == check.CANNOT_RUN and "::error::sdlc sarif wrote no list" in out


def test_a_scan_that_could_not_run_is_not_asked_for_its_files() -> None:
    t = FakeTools()
    t.sarif_rc, t.planned = 3, ["codeql"]
    rc, out = weekly_printed(t)
    assert rc == check.CANNOT_RUN and "wrote no codeql-results.sarif" not in out


def test_unreadable_or_runless_sarif_is_skipped_and_the_rest_sent() -> None:
    for bad in ("not json", '{"runs": "x"}', "[]"):
        t = FakeTools()
        t.sarif = {"codeql-results.sarif": bad, "semgrep.sarif": '{"runs": [{}]}'}
        rc, out = printed("push", t)
        assert rc == 0 and sent_categories(t) == ["semgrep/"], bad
        assert "::warning::codeql-results.sarif" in out, bad


def test_an_oversized_sarif_is_not_sent() -> None:
    t = FakeTools()
    t.sarif = SARIF
    limit, check.SARIF_LIMIT = check.SARIF_LIMIT, 10
    try:
        rc, out = printed("push", t)
    finally:
        check.SARIF_LIMIT = limit
    assert rc == 0 and not uploads(t) and "over GitHub's limit" in out


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
    # The secret scan's base fetch (--no-tags) is not fetch_tree's.
    assert not [a for a in t.argvs if a[0] == "git" and "--no-tags" not in a], (
        "fetched over an existing tree"
    )


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
    t.env = {"RUNNER_TEMP": ""}
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


# ------------------------------------------------------------------ publish, many images

_core.EXTRA_IMAGES[REPO] = ("demo-base",)  # what sdlc would review
OPT_OUT = {**IMAGE, "pyproject.toml": "[tool.sdlc.image]\npublish = false\n"}
MANY = {
    **IMAGE,
    "Containerfile.base": "FROM quay.io/ubi/ubi10-minimal:latest\n",
    "pyproject.toml": (
        '[[tool.sdlc.images]]\nname = "demo-base"\ncontainerfile = "Containerfile.base"\n'
    ),
}


def test_trentina_may_publish_its_l2_model() -> None:
    """EXTRA_IMAGES gives crunchtools/trentina its L2 model image (a1793), and no other repo."""
    entry = (
        '[[tool.sdlc.images]]\nname = "trentina-l2-model"\ncontainerfile = "m/Containerfile"\n'
        'context = "m"\nboot = "none"\nboot-none-reason = "data only"\n'
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "m").mkdir()
        (root / "m" / "Containerfile").write_text("FROM scratch\n")
        (root / "pyproject.toml").write_text("[tool.sdlc.image]\npublish = false\n" + entry)
        found = _core.images(root, "crunchtools/trentina")
        assert [i.name for i in found] == ["trentina-l2-model"], found
        msg = ""
        try:
            _core.images(root, "crunchtools/rotv")
        except CannotRunError as exc:
            msg = str(exc)
        assert "may publish only" in msg, msg


def test_publish_false_plans_no_image() -> None:
    rc, plan = go("push", FakeTools(prs=[pr()]), files=OPT_OUT)
    assert rc == 0 and plan["image"] is False
    t = FakeTools()
    t.base = DIGEST_B
    assert go("schedule", t, files=OPT_OUT, cron=check.POLL)[1]["image"] is False
    assert not [a for a in t.argvs if a[0] == "skopeo"], "polled an image it does not publish"
    rc, plan = go("schedule", FakeTools(), files=OPT_OUT, cron=check.WEEKLY)
    assert plan["image"] is False


def test_publish_false_with_an_extra_image_still_plans_it() -> None:
    files = {
        **MANY,
        "pyproject.toml": "[tool.sdlc.image]\npublish = false\n" + MANY["pyproject.toml"],
    }
    assert go("push", FakeTools(prs=[pr()]), files=files)[1]["image"] is True


def test_many_images_poll_each_parent() -> None:
    t = FakeTools()
    rc, plan = go("schedule", t, files=MANY, cron=check.POLL)
    assert rc == 0 and plan["image"] is False
    published = [a[-1] for a in t.argvs if a[:2] == ["skopeo", "inspect"] and "--format" not in a]
    assert published == [
        "docker://quay.io/crunchtools/demo:latest",
        "docker://quay.io/crunchtools/demo-base:latest",
    ], published
    t = FakeTools()
    t.base = DIGEST_B
    assert go("schedule", t, files=MANY, cron=check.POLL)[1]["image"] is True


def test_plan_boot_is_true_only_when_an_image_sets_a_boot_job_key() -> None:
    def planned(body: str) -> object:
        files = {**IMAGE, "pyproject.toml": f"[tool.sdlc.image]\n{body}"}
        return go("push", FakeTools(), files=files)[1]["boot"]

    assert go("push", FakeTools(), files=IMAGE)[1]["boot"] is False
    assert go("push", FakeTools())[1]["boot"] is False
    assert planned('boot = "mcp"\n') is False
    assert planned("free-disk = false\n") is False
    assert planned('boot = "command"\nboot-command = "true"\n') is True
    assert planned('contract-test = "true"\n') is True
    assert planned("free-disk = true\n") is True
    extra = {**MANY, "pyproject.toml": MANY["pyproject.toml"] + 'contract-test = "true"\n'}
    assert go("push", FakeTools(), files=extra)[1]["boot"] is True


def test_poll_reads_the_parent_build_args_choose() -> None:
    text = "ARG B=reg/x:1\nFROM ${B}\n"
    assert check.final_base(text, {"B": "quay.io/crunchtools/demo-base:latest"}) == (
        "quay.io/crunchtools/demo-base:latest"
    )


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


def test_launcher_trusts_the_mounted_workspace() -> None:
    """bin/sdlc runs git in the runner's workspace, owned by another uid (#157)."""
    t = FakeTools()
    t.env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.x", "GIT_CONFIG_VALUE_0": "y"}
    go("pull_request", t)
    _, child = t.launched[0]
    assert child["GIT_CONFIG_COUNT"] == "2"
    assert (child["GIT_CONFIG_KEY_0"], child["GIT_CONFIG_VALUE_0"]) == ("core.x", "y")
    assert child["GIT_CONFIG_KEY_1"] == "safe.directory"
    assert Path(child["GIT_CONFIG_VALUE_1"]).name == "ws"  # the workspace, never `*`


def test_fetch_tree_trusts_only_the_workspace_and_keeps_git_config() -> None:
    """fetch_tree's git calls name the workspace as safe.directory after the job's own
    GIT_CONFIG_* entries, which survive (#157)."""
    t = FakeTools()
    t.env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.x",
        "GIT_CONFIG_VALUE_0": "y",
        "GITHUB_TOKEN": "tok",
    }
    go("pull_request", t)
    fetched = [e for a, e in zip(t.argvs, t.envs, strict=True) if a[:1] == ["git"]]
    assert [a[:2] for a in t.argvs if a[:1] == ["git"] and "--no-tags" not in a] == [
        ["git", "init"],
        ["git", "remote"],
        ["git", "fetch"],
        ["git", "checkout"],
    ]
    for env in fetched:
        assert env["GIT_CONFIG_COUNT"] == "3"
        assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("core.x", "y")
        assert env["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraheader"
        assert env["GIT_CONFIG_KEY_2"] == "safe.directory"
        assert Path(env["GIT_CONFIG_VALUE_2"]).name == "ws"  # the workspace, never `*`


def fetches(t: FakeTools) -> list[list[str]]:
    return [a for a in t.argvs if a[:2] == ["git", "fetch"] and "--no-tags" in a]


def test_a_pull_request_diffs_against_its_base() -> None:
    t = FakeTools()
    go("pull_request", t, ref="refs/pull/1/merge")
    assert ["git", "fetch", "-q", "--no-tags", "--depth=1", "origin", BASE_SHA] in fetches(t)
    (_, ci_env), _ = t.launched[0], t.launched[1]
    assert ci_env["SDLC_DIFF_BASE"] == BASE_SHA
    assert all("SDLC_DIFF_BASE" not in e for c, e in t.launched if c != "ci")


def test_a_push_diffs_against_what_it_moved_from() -> None:
    t = FakeTools()
    t.before = "e" * 40
    go("push", t, ref="refs/heads/topic")
    assert ["git", "fetch", "-q", "--no-tags", "--depth=1", "origin", "e" * 40] in fetches(t)
    assert t.launched[0][1]["SDLC_DIFF_BASE"] == "e" * 40


def test_a_new_branch_diffs_against_the_default_branch_tip() -> None:
    for before in (None, "0" * 40):
        t = FakeTools()
        t.before = before
        go("push", t, ref="refs/heads/topic")
        assert ["git", "fetch", "-q", "--no-tags", "--depth=1", "origin", "main"] in fetches(t)
        assert t.launched[0][1]["SDLC_DIFF_BASE"] == DEFAULT_TIP


def test_a_dispatch_has_no_base() -> None:
    t = FakeTools()
    go("workflow_dispatch", t)
    assert not fetches(t) and "SDLC_DIFF_BASE" not in t.launched[0][1]


def test_a_malformed_base_cannot_run() -> None:
    for bad in ("--upload-pack=x", "ABCDEF" * 7, "abc123"):
        t = FakeTools()
        t.pr_base = bad
        assert "not a commit sha" in refusal("pull_request", t, ref="refs/pull/1/merge")
        assert not fetches(t) and not t.launched
