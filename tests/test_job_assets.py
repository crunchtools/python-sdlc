#!/usr/bin/env python3
"""`sdlc_jobs.assets`: attach a merged release's packages to its GitHub Release.

Drives `assets.run` with a fake Tools that records every argv and API call, answers
`podman build/create/cp/rm` (copying configurable files out of the "stage") and the
GitHub API (release, assets, uploads) from a script. Asserts the happy path, a
re-run that uploads only what is missing, a replaced asset, and the refusals, each
with nothing attached: the plan is not an authority, an unpinned build root, a bad
entry, a Release that is not `write`'s, a build that makes the wrong files, and an
upload GitHub does not take as sent.

Run: tests/test_job_assets.py   (needs python3 only)
"""

# ruff: noqa: ARG002
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdlc_jobs import assets
from sdlc_jobs._core import CannotRunError, Response, Tools

REPO = "crunchtools/ashigaru"
SHA = "0123456789abcdef0123456789abcdef01234567"
VERSION = "1.2.3"
BOT = "ashigaru-crunchtools-com[bot]"
TOKEN = "ghs_jobtoken"
RELEASE_ID = 9
UPLOADS = f"https://uploads.github.com/repos/{REPO}/releases/{RELEASE_ID}/assets"
ROOT_FROM = "registry.fedoraproject.org/fedora:43@sha256:" + "a" * 64
RPM = f"ashigaru-{VERSION}-1.fc43.noarch.rpm"
SRPM = f"ashigaru-{VERSION}-1.fc43.src.rpm"

PULL = {
    "number": 7,
    "merged_at": "2026-11-23T00:00:00Z",
    "merge_commit_sha": SHA,
    "user": {"login": BOT},
    "base": {"ref": "main"},
    "head": {"ref": f"ashigaru/release-{VERSION}", "repo": {"full_name": REPO}},
}
PYPROJECT = f"""\
[project]
name = "ashigaru"
version = "{VERSION}"

[tool.sdlc.ci.rpm-fc43]
kind = "image-build"
containerfile = "packaging/rpm.Containerfile"
build_args = {{ TARGET = "fc43", VERSION = "@project.version" }}

[[tool.sdlc.assets]]
check = "rpm-fc43"
target = "build"
path = "/out"
files = ["*.rpm"]
"""
CHANGELOG = f"# Changelog\n\n## [Unreleased]\n\n## [{VERSION}] - 2026-11-23\n\n## [1.2.2]\n"
CONTAINERFILE = f"""\
ARG TARGET
ARG VERSION
FROM {ROOT_FROM} AS build
RUN mkdir /out
FROM scratch AS other
"""


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class FakeTools(Tools):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.argvs: list[list[str]] = []
        self.calls: list[tuple[str, str, Mapping[str, str], bytes | None]] = []
        self.head = SHA
        # what each stage holds, by check name; "*" for any other
        self.out: dict[str, dict[str, bytes]] = {
            "*": {RPM: b"noarch bytes", SRPM: b"source bytes", "notes.txt": b"hello"}
        }
        self.links: dict[str, str] = {"evil.rpm": "/etc/passwd"}  # a symlink beside the files
        self.build_status = 0
        self.tag_sha = SHA
        self.draft = False
        self.upload_url = UPLOADS + "{?name,label}"
        self.existing: list[dict] = []  # assets already on the Release
        self.status: dict[str, int] = {}  # an upload's answer, by asset name
        self.said: dict[str, str] = {}  # the digest GitHub claims, by asset name
        self.cids: dict[str, str] = {}

    def run(  # noqa: PLR0913
        self,
        argv: list[str],
        *,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stream: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["podman", "--remote", "--url"]:
            argv = ["podman", *argv[4:]]
        self.argvs.append(argv)
        out = ""
        code = 0
        match argv[:2]:
            case ["podman", "build"]:
                code = self.build_status
            case ["podman", "create"]:
                tag = argv[2].rpartition(":")[2]
                self.cids[f"cid-{tag}"] = tag
                out = f"cid-{tag}\n"
            case ["podman", "cp"]:
                cid, _, path = argv[2].partition(":")
                assert path == "/out/.", path
                dest = Path(argv[3])
                files = self.out.get(self.cids[cid], self.out["*"])
                for name, data in files.items():
                    (dest / name).write_bytes(data)
                for name, target in self.links.items():
                    (dest / name).symlink_to(target)
                (dest / "subdir").mkdir()
                (dest / "subdir" / f"deep-{VERSION}.rpm").write_bytes(b"deep")
            case ["git", "rev-parse"]:
                out = self.head + "\n"
        return subprocess.CompletedProcess(argv, code, out, "")

    def http(
        self, method: str, url: str, headers: Mapping[str, str], data: bytes | None = None
    ) -> Response:
        self.calls.append((method, url, headers, data))
        if url.startswith("https://uploads.github.com/"):
            return self.upload(url, data or b"")
        path = url.removeprefix("https://api.github.com").partition("?")[0]
        rel = {
            "id": RELEASE_ID,
            "tag_name": f"v{VERSION}",
            "draft": self.draft,
            "upload_url": self.upload_url,
        }
        routes = {
            ("GET", f"/repos/{REPO}/commits/{SHA}/pulls"): [self.pull],
            ("GET", f"/repos/{REPO}"): {"default_branch": "main"},
            ("GET", f"/repos/{REPO}/commits/v{VERSION}"): {"sha": self.tag_sha},
            ("GET", f"/repos/{REPO}/releases/tags/v{VERSION}"): rel,
            ("GET", f"/repos/{REPO}/releases/{RELEASE_ID}/assets"): self.existing,
        }
        if (method, path) in routes:
            return Response(200, json.dumps(routes[(method, path)]).encode())
        if method == "DELETE" and path.startswith(f"/repos/{REPO}/releases/assets/"):
            return Response(204, b"")
        return Response(404, b"{}")

    def upload(self, url: str, data: bytes) -> Response:
        parts = urllib.parse.urlsplit(url)
        name = urllib.parse.parse_qs(parts.query)["name"][0]
        code = self.status.get(name, 201)
        if code != assets.CREATED:
            return Response(code, b'{"message": "Validation Failed"}')
        body = {"name": name, "digest": self.said.get(name, digest(data))}
        return Response(201, json.dumps(body).encode())

    # what the job did
    def builds(self) -> list[list[str]]:
        return [a for a in self.argvs if a[:2] == ["podman", "build"]]

    def posts(self) -> list[tuple[str, str, Mapping[str, str], bytes | None]]:
        return [c for c in self.calls if c[0] == "POST"]

    def deletes(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "DELETE"]

    def uploaded(self) -> dict[str, bytes]:
        """Asset name to body, in the order sent."""
        return {
            urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)["name"][0]: d or b""
            for _, u, _, d in self.posts()
        }

    pull: dict = PULL


def make(
    pyproject: str = PYPROJECT,
    containerfile: str = CONTAINERFILE,
) -> tuple[FakeTools, dict[str, str], tempfile.TemporaryDirectory[str]]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "ws"
    (root / "packaging").mkdir(parents=True)
    (root / "pyproject.toml").write_text(pyproject)
    (root / "CHANGELOG.md").write_text(CHANGELOG)
    (root / "packaging" / "rpm.Containerfile").write_text(containerfile)
    tools = FakeTools(root)
    tools.pull = copy.deepcopy(PULL)
    env = {
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_SHA": SHA,
        "GITHUB_WORKSPACE": str(root),
        "GITHUB_TOKEN": TOKEN,
        "HOSTNAME": "jobctr",
        "RUNNER_TEMP": tmp.name,
        "SDLC_WRITE": "success",
        "SDLC_RELEASE": json.dumps({"version": VERSION, "sha": SHA}),
    }
    return tools, env, tmp


def refusal(tools: FakeTools, env: dict[str, str], event: str = "push") -> str:
    """The message of the CannotRunError assets.run raises, or a failed assertion."""
    try:
        assets.run(event, {}, env, tools)
    except CannotRunError as exc:
        return str(exc)
    raise AssertionError("expected a refusal (exit 2)")


def refused(
    pyproject: str = PYPROJECT,
    containerfile: str = CONTAINERFILE,
    *,
    built: bool = False,
    setup: Callable[[FakeTools], None] | None = None,
) -> str:
    """The refusal message for this scenario; nothing uploaded or deleted meanwhile,
    and nothing built either unless the scenario can only be seen from a build."""
    tools, env, tmp = make(pyproject, containerfile)
    with tmp:
        if setup:
            setup(tools)
        msg = refusal(tools, env)
        assert not tools.posts() and not tools.deletes(), "attached after a refusal"
        assert bool(tools.builds()) == built, f"builds: {tools.builds()}"
        return msg


def test_happy_path() -> None:
    tools, env, tmp = make()
    with tmp:
        assert assets.run("push", {}, env, tools) == 0
        (build,) = tools.builds()
        assert build[build.index("--target") + 1] == "build"
        assert "VERSION=1.2.3" in build and "TARGET=fc43" in build
        assert build[build.index("-f") + 1] == str(tools.root / "packaging/rpm.Containerfile")
        assert build[-1] == str(tools.root / ".")
        creates = [a for a in tools.argvs if a[:2] == ["podman", "create"]]
        assert len(creates) == 1 and creates[0][-1] == "/sdlc-assets-never-run"
        sent = tools.uploaded()
        assert list(sent) == [RPM, SRPM, "SHA256SUMS"], list(sent)  # sums last
        assert sent[RPM] == b"noarch bytes" and sent[SRPM] == b"source bytes"
        want = f"{digest(sent[RPM])[7:]}  {RPM}\n{digest(sent[SRPM])[7:]}  {SRPM}\n"
        assert sent["SHA256SUMS"].decode() == want
        for _, url, headers, _ in tools.posts():
            assert url.startswith(UPLOADS + "?name="), url
            assert headers["Content-Type"] == "application/octet-stream"
            assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert not tools.deletes()
        assert ["podman", "rm", "-f", "cid-rpm-fc43"] in tools.argvs


def test_scratch_is_removed() -> None:
    tools, env, tmp = make()
    with tmp:
        assert assets.run("push", {}, env, tools) == 0
        assert not [p for p in Path(tmp.name).iterdir() if p.name.startswith("sdlc-assets-")]


def test_rerun_with_everything_present_uploads_nothing() -> None:
    tools, env, tmp = make()
    with tmp:
        assert assets.run("push", {}, env, tools) == 0
        sent = tools.uploaded()
        tools2, env2, tmp2 = make()
        with tmp2:
            tools2.existing = [
                {"id": 100 + i, "name": n, "digest": digest(b)}
                for i, (n, b) in enumerate(sent.items())
            ]
            assert assets.run("push", {}, env2, tools2) == 0
            assert not tools2.posts() and not tools2.deletes()
            assert tools2.builds(), "the files are rebuilt to compare their bytes"


def test_partial_rerun_uploads_only_what_is_missing() -> None:
    tools, env, tmp = make()
    with tmp:
        tools.existing = [{"id": 5, "name": RPM, "digest": digest(b"noarch bytes")}]
        assert assets.run("push", {}, env, tools) == 0
        assert list(tools.uploaded()) == [SRPM, "SHA256SUMS"]
        assert not tools.deletes()


def test_different_digest_is_deleted_then_replaced() -> None:
    tools, env, tmp = make()
    with tmp:
        tools.existing = [{"id": 42, "name": RPM, "digest": digest(b"old bytes")}]
        assert assets.run("push", {}, env, tools) == 0
        assert tools.deletes() == [f"https://api.github.com/repos/{REPO}/releases/assets/42"]
        assert list(tools.uploaded()) == [RPM, SRPM, "SHA256SUMS"]
        kinds = [c[0] for c in tools.calls if c[0] in ("DELETE", "POST")]
        assert kinds[:2] == ["DELETE", "POST"], kinds
        assert tools.uploaded()[RPM] == b"noarch bytes"


def test_event_not_push() -> None:
    tools, env, tmp = make()
    with tmp:
        for event in ("pull_request", "schedule", "workflow_dispatch"):
            assert "runs on push" in refusal(tools, env, event), event
        assert not tools.argvs and not tools.calls


def test_write_skipped() -> None:
    tools, env, tmp = make()
    with tmp:
        env["SDLC_WRITE"] = "skipped"
        msg = refusal(tools, env)
        assert "write" in msg and "skipped" in msg
        assert not tools.argvs and not tools.calls


def test_release_sha_differs() -> None:
    tools, env, tmp = make()
    with tmp:
        env["SDLC_RELEASE"] = json.dumps({"version": VERSION, "sha": "f" * 40})
        assert "write released" in refusal(tools, env)
        env["SDLC_RELEASE"] = ""
        assert "SDLC_RELEASE is not JSON" in refusal(tools, env)
        assert not tools.argvs and not tools.posts()


def test_release_version_differs() -> None:
    tools, env, tmp = make()
    with tmp:
        env["SDLC_RELEASE"] = json.dumps({"version": "1.2.4", "sha": SHA})
        assert "write said 1.2.4" in refusal(tools, env)
        assert not tools.builds() and not tools.posts()


def test_checkout_not_at_sha() -> None:
    msg = refused(setup=lambda t: setattr(t, "head", "f" * 40))
    assert "checkout" in msg


def test_no_assets_table() -> None:
    msg = refused(PYPROJECT.partition("[[tool.sdlc.assets]]")[0])
    assert "[[tool.sdlc.assets]]" in msg


def test_check_is_not_an_image_build() -> None:
    other = PYPROJECT.replace('"image-build"', '"command"')
    assert "image-build check" in refused(other)
    missing = PYPROJECT.replace('check = "rpm-fc43"', 'check = "nope"')
    assert "image-build check" in refused(missing)


def test_unknown_key_in_an_entry() -> None:
    msg = refused(PYPROJECT.replace('path = "/out"', 'path = "/out"\nprivileged = true'))
    assert "privileged" in msg


def test_from_by_tag_is_refused_before_a_build() -> None:
    tagged = CONTAINERFILE.replace("@sha256:" + "a" * 64, "")
    assert "not pinned" in refused(containerfile=tagged)
    two = CONTAINERFILE + "FROM docker.io/library/debian:13 AS extra\n"
    assert "not pinned" in refused(containerfile=two)
    assert "not pinned" in refused(containerfile=CONTAINERFILE.replace("a" * 64, "a" * 63))


def test_pinned_froms_alias_and_scratch_pass() -> None:
    cf = CONTAINERFILE + "FROM build AS test\nFROM scratch\n"
    tools, env, tmp = make(containerfile=cf)
    with tmp:
        assert assets.run("push", {}, env, tools) == 0


def test_now_build_arg_is_refused() -> None:
    msg = refused(PYPROJECT.replace('"@project.version"', '"@now"'))
    assert "@now" in msg


def test_git_sha_build_arg_resolves() -> None:
    tools, env, tmp = make(PYPROJECT.replace('"@project.version"', '"@git.sha"'))
    with tmp:
        assert assets.run("push", {}, env, tools) == 0
        assert f"VERSION={SHA}" in tools.builds()[0]


def test_path_with_dotdot() -> None:
    msg = refused(PYPROJECT.replace('"/out"', '"/out/../etc"'))
    assert ".." in msg
    assert "absolute" in refused(PYPROJECT.replace('"/out"', '"out"'))


def test_files_glob_with_a_slash() -> None:
    assert "globs" in refused(PYPROJECT.replace('["*.rpm"]', '["sub/*.rpm"]'))
    assert "globs" in refused(PYPROJECT.replace('["*.rpm"]', "[]"))


def test_target_stage_missing() -> None:
    msg = refused(PYPROJECT.replace('target = "build"', 'target = "package"'))
    assert "no stage named package" in msg
    # a stage whose name merely ends in the target is not it
    assert "no stage named build" in refused(
        containerfile=CONTAINERFILE.replace("AS build", "AS rebuild")
    )


def test_tag_points_at_another_sha() -> None:
    msg = refused(built=False, setup=lambda t: setattr(t, "tag_sha", "f" * 40))
    assert "does not name" in msg


def test_draft_release() -> None:
    assert "not published" in refused(setup=lambda t: setattr(t, "draft", True))


def test_upload_url_for_another_release() -> None:
    other = f"https://uploads.github.com/repos/{REPO}/releases/8/assets" + "{?name,label}"
    msg = refused(built=True, setup=lambda t: setattr(t, "upload_url", other))
    assert "upload_url" in msg
    foreign = "https://uploads.github.com/repos/evil/x/releases/9/assets{?name}"
    assert "upload_url" in refused(built=True, setup=lambda t: setattr(t, "upload_url", foreign))
    plain = f"http://uploads.github.com/repos/{REPO}/releases/9/assets"
    assert "upload_url" in refused(built=True, setup=lambda t: setattr(t, "upload_url", plain))


def test_build_failure() -> None:
    msg = refused(built=True, setup=lambda t: setattr(t, "build_status", 1))
    assert "podman build exited 1" in msg


def test_glob_matching_nothing() -> None:
    msg = refused(PYPROJECT.replace('["*.rpm"]', '["*.rpm", "*.tar.gz"]'), built=True)
    assert "matches '*.tar.gz'" in msg


def test_file_name_lacking_the_version() -> None:
    def setup(t: FakeTools) -> None:
        t.out["*"] = {"ashigaru-9.9.9-1.fc43.noarch.rpm": b"x"}

    msg = refused(built=True, setup=setup)
    assert "does not carry the release version 1.2.3" in msg


def test_odd_names_are_refused() -> None:
    def setup(t: FakeTools) -> None:
        t.out["*"] = {f"-rf {VERSION}.rpm": b"x"}

    assert "not a name" in refused(built=True, setup=setup)


def test_two_entries_same_name_different_bytes() -> None:
    py = PYPROJECT.replace("[[tool.sdlc.assets]]", "[tool.sdlc.ci.rpm-fc44]\n" + _ci44())
    py += _second_entry()

    def setup(t: FakeTools) -> None:
        t.out = {"rpm-fc43": {RPM: b"one"}, "rpm-fc44": {RPM: b"two"}, "*": {}}
        t.links = {}

    msg = refused(py, built=True, setup=setup)
    assert "different bytes" in msg


def test_two_entries_same_name_same_bytes_uploaded_once() -> None:
    py = PYPROJECT.replace("[[tool.sdlc.assets]]", "[tool.sdlc.ci.rpm-fc44]\n" + _ci44())
    py += _second_entry()
    tools, env, tmp = make(py)
    with tmp:
        tools.out = {"rpm-fc43": {RPM: b"same"}, "rpm-fc44": {RPM: b"same"}, "*": {}}
        tools.links = {}
        assert assets.run("push", {}, env, tools) == 0
        assert len(tools.builds()) == len(tools.out) - 1  # one build per entry
        assert list(tools.uploaded()) == [RPM, "SHA256SUMS"]
        assert len(tools.posts()) == len(tools.uploaded())


def _ci44() -> str:
    return (
        'kind = "image-build"\ncontainerfile = "packaging/rpm.Containerfile"\n'
        'build_args = { TARGET = "fc44" }\n\n[[tool.sdlc.assets]]'
    )


def _second_entry() -> str:
    entry = 'check = "rpm-fc44"\ntarget = "build"\npath = "/out"\nfiles = ["*.rpm"]\n'
    return "\n[[tool.sdlc.assets]]\n" + entry


def test_upload_answered_422() -> None:
    def setup(t: FakeTools) -> None:
        t.status[SRPM] = 422

    msg = refused_after_some(setup)
    assert "422" in msg and SRPM in msg


def refused_after_some(setup: Callable[[FakeTools], None]) -> str:
    """A refusal that comes mid-upload: what went before stays, and SHA256SUMS never goes."""
    tools, env, tmp = make()
    with tmp:
        setup(tools)
        msg = refusal(tools, env)
        assert "SHA256SUMS" not in tools.uploaded()
        return msg


def test_github_returns_another_digest() -> None:
    def setup(t: FakeTools) -> None:
        t.said[RPM] = "sha256:" + "0" * 64

    msg = refused_after_some(setup)
    assert "GitHub stored" in msg and RPM in msg


def test_a_digest_less_answer_is_accepted() -> None:
    tools, env, tmp = make()
    with tmp:
        tools.said[RPM] = ""
        assert assets.run("push", {}, env, tools) == 0


def test_a_full_page_of_assets_is_refused() -> None:
    def setup(t: FakeTools) -> None:
        t.existing = [{"id": i, "name": f"x{i}", "digest": ""} for i in range(100)]

    assert "100 assets or more" in refused(built=True, setup=setup)


HATCH = PYPROJECT.replace(
    f'version = "{VERSION}"', 'dynamic = ["version"]\n\n[tool.hatch.version]\npath = "VERSION"'
)


def test_a_hatch_version_file_is_the_release_version() -> None:
    """ashigaru's shape: [project] dynamic version, read from VERSION as `write` reads it."""
    tools, env, tmp = make(pyproject=HATCH)
    with tmp:
        (tools.root / "VERSION").write_text(f"{VERSION}\n")
        assert assets.run("push", {}, env, tools) == 0
        (build,) = tools.builds()
        assert f"VERSION={VERSION}" in build


def test_a_hatch_version_file_that_is_not_semver_is_refused() -> None:
    tools, env, tmp = make(pyproject=HATCH)
    with tmp:
        (tools.root / "VERSION").write_text("one point two\n")
        assert "not X.Y.Z" in refusal(tools, env)
        assert not tools.builds()


def test_a_hatch_version_path_outside_the_tree_is_refused() -> None:
    tools, env, tmp = make(pyproject=HATCH.replace('path = "VERSION"', 'path = "../VERSION"'))
    with tmp:
        assert "leaves the tree" in refusal(tools, env)
        assert not tools.builds()


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
