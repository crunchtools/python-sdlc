#!/usr/bin/env python3
"""`sdlc_jobs.pypi` and `_release.verify_release`: publish only a release it re-derived.

Drives `pypi.run` with a fake Tools that records every argv and answers the
GitHub API from a script. Asserts the happy path (nested build, then trusted
publishing from the job itself), each of the seven release tests failing alone,
and the refusals before them: not a push, `write` skipped, the tree not asking
for PyPI, and a release that disagrees with `write`. Without OIDC, an API token
in UV_PUBLISH_TOKEN publishes, reaching the `uv publish` process alone.

Run: tests/test_job_pypi.py   (needs python3 only)
"""

# ruff: noqa: ARG002
from __future__ import annotations

import copy
import json
import os
import shutil
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
BOT = "ashigaru-crunchtools-com[bot]"
FAKE_KEY = "pypi-AgEIcHlwaS5vcmcTOKENVALUE"  # not a real one

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
        self.envs: list[dict[str, str] | None] = []
        self.inherited: list[bool] = []  # the token in os.environ when each argv ran
        self.urls: list[str] = []
        self.dist = [f"mcp_example-{VERSION}.tar.gz", f"mcp_example-{VERSION}-py3-none-any.whl"]
        self.head = SHA
        self.rewrite: str | None = None  # what a hostile build backend writes to pyproject.toml

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
        self.envs.append(None if env is None else dict(env))
        self.inherited.append("UV_PUBLISH_TOKEN" in os.environ)
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
            if self.rewrite is not None:
                (self.root / "pyproject.toml").write_text(self.rewrite)
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
        return [" ".join(a[:3:2] if a[:2] == ["uv", "--no-config"] else a[:2]) for a in self.argvs]

    def call(self, *head: str) -> int:
        """The index of the one argv starting with `head`."""
        found = [i for i, a in enumerate(self.argvs) if a[: len(head)] == list(head)]
        assert len(found) == 1, f"{head}: {found}"
        return found[0]

    def publish(self) -> int:
        return self.call("uv", "--no-config", "publish")


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
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example/req",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc-req",
    }
    return tools, env, tmp


def token_env(env: dict[str, str]) -> dict[str, str]:
    """As scripts/local-release.sh runs the job: no Actions run, so no OIDC; a token."""
    env = {k: v for k, v in env.items() if not k.startswith("ACTIONS_ID_TOKEN_")}
    env["UV_PUBLISH_TOKEN"] = FAKE_KEY
    return env


def refusal(tools: FakeTools, env: dict[str, str], event: str = "push") -> str:
    """The message of the CannotRunError pypi.run raises, or a failed assertion."""
    try:
        pypi.run(event, {}, env, tools)
    except CannotRunError as exc:
        return str(exc)
    raise AssertionError("expected a refusal (exit 2)")


def refused(
    tweak: Callable[[dict], None] | None = None,
    *,
    pyproject: str = PYPROJECT,
    changelog: str = CHANGELOG,
    default: str = "main",
    pulls: object = None,
) -> str:
    """The refusal message for this scenario; nothing was built or published meanwhile."""
    tools, env, tmp = make(
        tweak, pyproject=pyproject, changelog=changelog, default=default, pulls=pulls
    )
    with tmp:
        msg = refusal(tools, env)
        assert "podman run" not in " ".join(tools.verbs()), "built after a refusal"
        assert "uv publish" not in " ".join(tools.verbs()), "published after a refusal"
        return msg


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
        i = tools.publish()
        publish = tools.argvs[i]
        assert publish[3:5] == ["--trusted-publishing", "always"]
        assert publish[publish.index("--publish-url") + 1] == pypi.UPLOAD
        assert "--check-url" in publish
        assert {Path(p).name for p in publish if p.endswith((".whl", ".gz"))} == set(tools.dist)
        upload = tools.envs[i]
        assert upload is not None and upload["ACTIONS_ID_TOKEN_REQUEST_URL"]
        assert "UV_PUBLISH_TOKEN" not in upload
        b = tools.call("podman", "run")
        assert tools.envs[b] is not None and tools.envs[b]["GITHUB_REPOSITORY"] == REPO


def test_not_a_push() -> None:
    tools, env, tmp = make()
    with tmp:
        assert "runs on push" in refusal(tools, env, "pull_request")
        assert not tools.argvs


def test_other_events_are_refused() -> None:
    tools, env, tmp = make()
    with tmp:
        for event in ("pull_request", "pull_request_target", "issues", "schedule"):
            assert "runs on push" in refusal(tools, env, event), event
        assert not tools.argvs


def test_write_skipped() -> None:
    tools, env, tmp = make()
    with tmp:
        env["SDLC_WRITE"] = "skipped"
        msg = refusal(tools, env)
        assert "write" in msg and "skipped" in msg
        assert not tools.argvs


def test_plan_says_pypi_tree_does_not() -> None:
    msg = refused(pyproject=PYPROJECT.replace('publish = "pypi"', ""))
    assert 'publish = "pypi"' in msg


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
        assert "SDLC_RELEASE is not JSON" in refusal(tools, env)


def _set(path: str, value: object) -> Callable[[dict], None]:
    def tweak(pull: dict) -> None:
        node = pull
        *parents, last = path.split(".")
        for key in parents:
            node = node[key]
        node[last] = value

    return tweak


def test_seven_tests() -> None:
    cases: list[tuple[str, dict]] = [
        ("test 1", {"pulls": []}),
        ("test 1", {"pulls": [PULL, PULL]}),
        ("test 1", {"tweak": _set("merged_at", None)}),
        ("test 2", {"tweak": _set("merge_commit_sha", "f" * 40)}),
        ("test 3", {"tweak": _set("user.login", "someone")}),
        ("test 4", {"tweak": _set("base.ref", "dev")}),
        ("test 4", {"default": "trunk"}),
        ("test 5", {"tweak": _set("head.repo.full_name", "evil/mcp-example")}),
        ("test 5", {"tweak": _set("head.repo", None)}),
        ("test 6", {"tweak": _set("head.ref", "feature/x")}),
        ("test 6", {"tweak": _set("head.ref", "ashigaru/release-1.2")}),
        ("test 6", {"tweak": _set("head.ref", "ashigaru/release-v1.2.3")}),
        ("test 7", {"pyproject": PYPROJECT.replace(VERSION, "1.2.4")}),
        ("test 7", {"changelog": CHANGELOG.replace(f"## [{VERSION}]", "## [1.2.4]")}),
        ("test 7", {"changelog": "# Changelog\n\n## [Unreleased]\n"}),
        ("test 7", {"pyproject": '[project]\nname = "x"\n[tool.sdlc]\npublish = "pypi"\n'}),
    ]
    for want, scenario in cases:
        msg = refused(**scenario)
        assert want in msg, f"wanted {want!r} in {msg}: {scenario}"


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


def test_uv_gitignore_is_not_uploaded() -> None:
    tools, env, tmp = make()
    with tmp:
        tools.dist = [*tools.dist, ".gitignore"]
        assert pypi.run("push", {}, env, tools) == 0
        publish = tools.argvs[tools.publish()]
        assert not any(p.endswith(".gitignore") for p in publish)


def test_other_dotfiles_are_refused() -> None:
    tools, env, tmp = make()
    with tmp:
        tools.dist = [*tools.dist, ".pypirc"]
        assert "dist/" in refusal(tools, env)
        assert "uv publish" not in " ".join(tools.verbs())


def test_name_is_read_before_the_build() -> None:
    tools, env, tmp = make()
    with tmp:
        tools.rewrite = PYPROJECT.replace("mcp-example", "evil")
        tools.dist = [f"evil-{VERSION}.tar.gz"]
        msg = refusal(tools, env)
        assert "dist/" in msg and "mcp_example" in msg
        assert "uv publish" not in " ".join(tools.verbs())


def test_missing_name_is_a_refusal() -> None:
    no_name = PYPROJECT.replace('name = "mcp-example"\n', "")
    msg = refused(pyproject=no_name)
    assert "[project].name" in msg


def test_checkout_not_at_sha() -> None:
    tools, env, tmp = make()
    with tmp:
        tools.head = "f" * 40
        msg = refusal(tools, env)
        assert "checkout" in msg


def test_rev_parse_trusts_the_mounted_workspace() -> None:
    """The workspace is owned by another uid than the container's git (#157)."""
    tools, env, tmp = make()
    with tmp:
        assert pypi.run("push", {}, env, tools) == 0
    i = next(i for i, a in enumerate(tools.argvs) if a[:2] == ["git", "rev-parse"])
    genv = tools.envs[i]
    n = int(genv["GIT_CONFIG_COUNT"]) - 1
    assert genv[f"GIT_CONFIG_KEY_{n}"] == "safe.directory"
    assert genv[f"GIT_CONFIG_VALUE_{n}"] == str(tools.root.resolve())  # never `*`


def test_token_publishes_without_oidc() -> None:
    tools, env, tmp = make()
    with tmp:
        assert pypi.run("push", {}, token_env(env), tools) == 0
        i = tools.publish()
        assert tools.argvs[i][3:5] == ["--trusted-publishing", "never"]
        assert tools.argvs[i][tools.argvs[i].index("--publish-url") + 1] == pypi.UPLOAD
        assert "--check-url" in tools.argvs[i]
        assert tools.envs[i]["UV_PUBLISH_TOKEN"] == FAKE_KEY
        assert not any(FAKE_KEY in " ".join(a) for a in tools.argvs), "the token on an argv"
        others = [e or {} for j, e in enumerate(tools.envs) if j != i]
        assert others and not any(FAKE_KEY in e.values() for e in others), "the token left uv"
        build = tools.call("podman", "run")
        assert tools.envs[build] is not None, "the build inherits os.environ"
        assert "UV_PUBLISH_TOKEN" not in tools.envs[build]
        assert not any("UV_PUBLISH" in a for a in tools.argvs[build])


def test_token_leaves_os_environ_before_the_first_step() -> None:
    """Shared helpers run podman with env=None, which inherits os.environ."""
    tools, env, tmp = make()
    with tmp:
        os.environ["UV_PUBLISH_TOKEN"] = FAKE_KEY
        try:
            assert pypi.run("push", {}, token_env(env), tools) == 0
            assert tools.inherited and not any(tools.inherited)
            assert "UV_PUBLISH_TOKEN" not in os.environ
        finally:
            os.environ.pop("UV_PUBLISH_TOKEN", None)


def test_a_planted_uv_toml_cannot_move_the_upload() -> None:
    """The job's own upload argv, run by the real uv as a dry run in a checkout that
    names another index in uv.toml and [tool.uv]: uv still names PyPI."""
    uv = shutil.which("uv")
    if uv is None:
        print("skip: no uv on PATH")
        return
    tools, env, tmp = make()
    with tmp:
        assert pypi.run("push", {}, token_env(env), tools) == 0
        argv = list(tools.argvs[tools.publish()])
        root = tools.root
        evil = "http://127.0.0.1:9/evil/"
        (root / "uv.toml").write_text(f'publish-url = "{evil}"\n')
        argv[argv.index("publish") + 1 : argv.index("publish") + 1] = ["--dry-run"]
        at = argv.index("--check-url")  # dry run: no index to ask
        del argv[at : at + 2]
        proc = subprocess.run(
            [uv, *argv[1:]],
            cwd=root,
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(root), "UV_PUBLISH_TOKEN": "x"},
            capture_output=True,
            text=True,
            check=False,
        )
        out = proc.stdout + proc.stderr
        assert f"against {pypi.UPLOAD}" in out and evil not in out, out
        # and without the job's flags, the plant would have won: the test can see it
        bare = [uv, "publish", "--dry-run", "--trusted-publishing", "never", *argv[-2:]]
        proc = subprocess.run(
            bare,
            cwd=root,
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(root), "UV_PUBLISH_TOKEN": "x"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert evil in proc.stdout + proc.stderr


def test_oidc_and_a_token_is_a_refusal() -> None:
    tools, env, tmp = make()
    with tmp:
        env["UV_PUBLISH_TOKEN"] = FAKE_KEY
        msg = refusal(tools, env)
        assert "both OIDC and UV_PUBLISH_TOKEN" in msg and FAKE_KEY not in msg
        assert not tools.argvs


def test_no_credential_is_a_refusal() -> None:
    tools, env, tmp = make()
    with tmp:
        env = token_env(env)
        del env["UV_PUBLISH_TOKEN"]
        assert "nothing to publish with" in refusal(tools, env)
        assert not tools.argvs


def test_half_an_oidc_pair_is_not_oidc() -> None:
    tools, env, tmp = make()
    with tmp:
        del env["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
        assert "nothing to publish with" in refusal(tools, env)


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
