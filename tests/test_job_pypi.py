#!/usr/bin/env python3
"""`sdlc_jobs.pypi` and `_release.verify_release`: publish only a release it re-derived.

Drives `pypi.run` with a fake Tools that records every argv and answers the
GitHub API from a script. Asserts the happy path (nested build, then trusted
publishing from the job itself), each of the seven release tests failing alone,
and the refusals before them: not a push, `write` skipped, the tree not asking
for PyPI, and a release that disagrees with `write`.

Run: tests/test_job_pypi.py   (needs python3 only)
"""

# ruff: noqa: ARG002
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdlc_jobs import pypi
from sdlc_jobs._core import CannotRunError, Response, Tools

REPO = "crunchtools/mcp-example"
SHA = "0123456789abcdef0123456789abcdef01234567"
VERSION = "1.2.3"
BOT = "ashigaru-crunchtools[bot]"

PULL = {
    "number": 7,
    "merged_at": "2026-11-23T00:00:00Z",
    "merge_commit_sha": SHA,
    "user": {"login": BOT},
    "base": {"ref": "main"},
    "head": {"ref": f"ashigaru/release-{VERSION}", "repo": {"full_name": REPO}},
}
PYPROJECT = (
    f'[project]\nname = "mcp-example"\nversion = "{VERSION}"\n[tool.sdlc]\npublish = "pypi"\n'
)
CHANGELOG = f"# Changelog\n\n## [Unreleased]\n\n## [{VERSION}] - 2026-11-23\n\n## [1.2.2]\n"


class FakeTools(Tools):
    def __init__(self, root: Path, pulls: object, default: str = "main") -> None:
        self.root = root
        self.pulls = pulls
        self.default = default
        self.argvs: list[list[str]] = []
        self.urls: list[str] = []
        self.dist = [f"mcp_example-{VERSION}.tar.gz", f"mcp_example-{VERSION}-py3-none-any.whl"]
        self.head = SHA

    def run(
        self,
        argv: list[str],
        *,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["podman", "--remote", "--url"]:
            argv = ["podman", *argv[4:]]
        self.argvs.append(argv)
        out = ""
        if argv[:2] == ["podman", "inspect"]:
            out = json.dumps(
                [
                    {
                        "ImageName": "quay.io/crunchtools/sdlc:abc",
                        "Image": "imgid",
                        "Mounts": [{"Source": "/host/ws", "Destination": str(self.root.parent)}],
                    }
                ]
            )
        elif argv[:2] == ["podman", "run"]:
            (self.root / "dist").mkdir()
            for name in self.dist:
                (self.root / "dist" / name).write_text("x")
        elif argv[:2] == ["git", "rev-parse"]:
            out = self.head + "\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    def http(
        self, method: str, url: str, headers: Mapping[str, str], data: bytes | None = None
    ) -> Response:
        self.urls.append(url)
        path = url.removeprefix("https://api.github.com")
        if path == f"/repos/{REPO}/commits/{SHA}/pulls":
            return Response(200, json.dumps(self.pulls).encode())
        if path == f"/repos/{REPO}":
            return Response(200, json.dumps({"default_branch": self.default}).encode())
        return Response(404, b"{}")

    def verbs(self) -> list[str]:
        return [" ".join(a[:2]) for a in self.argvs]


def make(
    tweak: Callable[[dict], None] | None = None,
    *,
    pyproject: str = PYPROJECT,
    changelog: str = CHANGELOG,
    default: str = "main",
    pulls: object = None,
) -> tuple[FakeTools, dict[str, str], tempfile.TemporaryDirectory[str]]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "ws"
    root.mkdir()
    (root / "pyproject.toml").write_text(pyproject)
    (root / "CHANGELOG.md").write_text(changelog)
    pull = copy.deepcopy(PULL)
    if tweak:
        tweak(pull)
    tools = FakeTools(root, [pull] if pulls is None else pulls, default)
    env = {
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_SHA": SHA,
        "GITHUB_WORKSPACE": str(root),
        "GITHUB_TOKEN": "t",
        "HOSTNAME": "jobctr",
        "SDLC_WRITE": "success",
        "SDLC_RELEASE": json.dumps({"version": VERSION, "sha": SHA}),
    }
    return tools, env, tmp


def refusal(tools: FakeTools, env: dict[str, str], event: str = "push") -> str:
    """The message of the CannotRunError pypi.run raises, or a failed assertion."""
    try:
        pypi.run(event, {}, env, tools)
    except CannotRunError as exc:
        return str(exc)
    raise AssertionError("expected a refusal (exit 2)")


def refused(
    want: str, tweak: Callable[[dict], None] | None = None, *, event: str = "push", **kw: object
) -> None:
    tools, env, tmp = make(tweak, **kw)  # type: ignore[arg-type]
    with tmp:
        msg = refusal(tools, env, event)
        assert want in msg, f"wanted {want!r} in {msg}"
        assert "podman run" not in " ".join(tools.verbs()), "built after a refusal"
        assert "uv publish" not in " ".join(tools.verbs()), "published after a refusal"


def test_happy_path() -> None:
    tools, env, tmp = make()
    with tmp:
        assert pypi.run("push", {}, env, tools) == 0
        verbs = tools.verbs()
        assert verbs.index("podman run") < verbs.index("uv publish")
        build = next(a for a in tools.argvs if a[:2] == ["podman", "run"])
        assert "--cap-drop=all" in build and "/host/ws/ws:/repo:rw,z" in build
        assert "--entrypoint" in build and "imgid" in build
        assert not any(a.startswith("-e") and "TOKEN" in a for a in build)
        assert "/var/run/docker.sock" not in " ".join(build)
        publish = next(a for a in tools.argvs if a[:2] == ["uv", "publish"])
        assert publish[2:4] == ["--trusted-publishing", "always"]
        assert "--check-url" in publish
        assert {Path(p).name for p in publish if p.endswith((".whl", ".gz"))} == set(tools.dist)


def test_not_a_push() -> None:
    refused("runs on a push", event="pull_request")


def test_write_skipped() -> None:
    tools, env, tmp = make()
    with tmp:
        env["SDLC_WRITE"] = "skipped"
        msg = refusal(tools, env)
        assert "write" in msg and "skipped" in msg
        assert not tools.argvs


def test_plan_says_pypi_tree_does_not() -> None:
    refused('publish = "pypi"', pyproject=PYPROJECT.replace('publish = "pypi"', ""))


def test_release_mismatch() -> None:
    tools, env, tmp = make()
    with tmp:
        env["SDLC_RELEASE"] = json.dumps({"version": "1.2.4", "sha": SHA})
        msg = refusal(tools, env)
        assert "write said 1.2.4" in msg
        env["SDLC_RELEASE"] = json.dumps({"version": VERSION, "sha": "f" * 40})
        msg = refusal(tools, env)
        assert "write released" in msg


def test_no_release_output() -> None:
    tools, env, tmp = make()
    with tmp:
        env["SDLC_RELEASE"] = ""
        refusal(tools, env)


def _set(path: str, value: object) -> Callable[[dict], None]:
    def tweak(pull: dict) -> None:
        node = pull
        *parents, last = path.split(".")
        for key in parents:
            node = node[key]
        node[last] = value

    return tweak


def test_seven_tests() -> None:
    refused("test 1", pulls=[])
    refused("test 1", pulls=[PULL, PULL])
    refused("test 1", _set("merged_at", None))
    refused("test 2", _set("merge_commit_sha", "f" * 40))
    refused("test 3", _set("user.login", "someone"))
    refused("test 4", _set("base.ref", "dev"))
    refused("test 4", default="trunk")
    refused("test 5", _set("head.repo.full_name", "evil/mcp-example"))
    refused("test 5", _set("head.repo", None))
    refused("test 6", _set("head.ref", "feature/x"))
    refused("test 6", _set("head.ref", "ashigaru/release-1.2"))
    refused("test 6", _set("head.ref", "ashigaru/release-v1.2.3"))
    refused("test 7", pyproject=PYPROJECT.replace(VERSION, "1.2.4"))
    refused("test 7", changelog=CHANGELOG.replace(f"## [{VERSION}]", "## [1.2.4]"))
    refused("test 7", changelog="# Changelog\n\n## [Unreleased]\n")
    refused("test 7", pyproject='[project]\nname = "x"\n[tool.sdlc]\npublish = "pypi"\n')


def test_custom_app_login() -> None:
    tools, env, tmp = make(_set("user.login", "other[bot]"))
    with tmp:
        env["SDLC_APP_LOGIN"] = "other[bot]"
        assert pypi.run("push", {}, env, tools) == 0


def test_bad_dist_names() -> None:
    tools, env, tmp = make()
    with tmp:
        tools.dist = ["mcp_example-9.9.9.tar.gz"]
        msg = refusal(tools, env)
        assert "dist/" in msg
        assert "uv publish" not in " ".join(tools.verbs())


def test_checkout_not_at_sha() -> None:
    tools, env, tmp = make()
    with tmp:
        tools.head = "f" * 40
        msg = refusal(tools, env)
        assert "checkout" in msg


def main() -> int:
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                failed += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            else:
                print(f"ok   {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
