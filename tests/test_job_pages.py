#!/usr/bin/env python3
"""`sdlc_jobs.pages`: sign other repos' released packages and publish them as a dnf/apt site.

Drives `pages.run` with a fake Tools that records every argv, env and stdin, answers
gpg, rpmsign, rpmkeys, rpm, createrepo_c and git from a script, and answers the GitHub
API and the download URLs over `http`. The pool's .deb files are real (built with
test_debrepo's helper), so `_debrepo.write_packages` runs for real. Asserts the happy
path (key on stdin only, the tree pushed under a lease, the Pages build awaited), what
the pool leaves out, the signed RPM that is kept, every refusal, `pages.changed`, and
`check.pages_plan`.

Run: tests/test_job_pages.py   (needs python3 only)
"""

# ruff: noqa: ARG001, ARG002, ARG005, PT017
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_debrepo import make_deb

from sdlc_jobs import check, pages
from sdlc_jobs._core import CannotRunError, Response, Tools

REPO = "crunchtools/packages"
SITE = "https://crunchtools.github.io/packages"
KEY = (
    "-----BEGIN PGP PRIVATE KEY BLOCK-----\n"
    "SECRETKEYMATERIAL\n-----END PGP PRIVATE KEY BLOCK-----\n"
)
FPR = "A" * 40
OTHER_FPR = "B" * 40
OLD = "1" * 40  # gh-pages head before the run
NEW = "2" * 40  # the commit the run pushes
EVENT = {"repository": {"default_branch": "main"}}
RPM_PETIT2 = "petit-2.0-1.x86_64.rpm"
RPM_PETIT1 = "petit-1.0-1.x86_64.rpm"
RPM_AGENT = "claude-agent-1.0-1.x86_64.rpm"
DEB_PETIT2 = "petit_2.0_amd64.deb"
CONTROL = (
    "Package: {pkg}\nVersion: 2.0\nArchitecture: amd64\nMaintainer: A <a@example.com>\n"
    "Description: a test package\n"
)

DEFAULTS = {
    "name": '"crunchtools"',
    "site-url": f'"{SITE}"',
    "key": '"crunchtools.asc"',
    "products": '["crunchtools/petit", "crunchtools/claude-agent"]',
    "keep": "2",
    "static": '["index.html"]',
}


def toml_text(**fields: str | None) -> str:
    """sdlc.toml with [tool.sdlc.pages]; a field of None is left out, an unknown one added."""
    merged = {**DEFAULTS, **{k.replace("_", "-"): v for k, v in fields.items()}}
    lines = [f"{k} = {v}" for k, v in merged.items() if v is not None]
    return "[tool.sdlc.pages]\n" + "\n".join(lines) + "\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def deb_bytes(name: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        path = make_deb(Path(tmp) / name, CONTROL.format(pkg=name.split("_", maxsplit=1)[0]))
        return path.read_bytes()


def colons(fprs: list[str], kind: str) -> str:
    """gpg --with-colons: each primary key, its fingerprint, and a subkey that must be ignored."""
    sub = "ssb" if kind == "sec" else "sub"
    return "".join(
        f"{kind}:u:255:22:KEYID{i}:1700000000:::u:::scESC:::::::\nfpr:::::::::{f}:\n"
        f"grp:::::::::GRIP:\n{sub}:u:255:22:SUBID:1700000000:::::e::::::\n"
        f"fpr:::::::::{'C' * 40}:\n"
        for i, f in enumerate(fprs)
    )


class FakeTools(Tools):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.argvs: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []
        self.inputs: list[str | None] = []
        self.inherited: list[bool] = []  # the key in os.environ when each argv ran
        self.http_calls: list[tuple[str, str, dict[str, str]]] = []
        self.releases: dict[str, list[dict]] = {}
        self.blobs: dict[str, bytes] = {}
        self.index: object = None  # the live index.json; None is a 404
        self.secret_fprs = [FPR]
        self.public_fprs = [FPR]
        self.gh_pages = True  # does `git fetch` of gh-pages succeed
        self.live_tree: dict[str, bytes] = {}  # what FETCH_HEAD checks out
        self.checksig = "digests signatures OK\n"
        self.checksig_rc = 0
        self.headers: dict[tuple[str, str], str] = {}  # ("live"|"new", file) -> SHA256HEADER
        self.post_status = 201
        self.latest: dict | None = None  # None: built at NEW at once
        self.signed: list[str] = []
        self.signs: list[str] = []
        self.imported: str | None = None
        self.gnupghome = ""
        self.home_at_commit: bool | None = None
        self.tree: dict[str, bytes] = {}
        self.pushed = False

    def add_release(
        self, product: str, tag: str, names: list[str], *, draft: bool = False, pre: bool = False
    ) -> list[dict]:
        assets = []
        for n in names:
            blob = deb_bytes(n) if n.endswith(".deb") else f"RPM {n} {tag}".encode()
            url = f"https://github.com/{product}/releases/download/{tag}/{n}"
            self.blobs[url] = blob
            assets.append(
                {
                    "name": n,
                    "browser_download_url": url,
                    "digest": f"sha256:{sha(blob)}",
                }
            )
        rel = {"tag_name": tag, "draft": draft, "prerelease": pre, "assets": assets}
        self.releases.setdefault(product, []).append(rel)
        return assets

    def run(
        self,
        argv: list[str],
        *,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        **kw: object,
    ) -> subprocess.CompletedProcess[str]:
        self.argvs.append(argv)
        self.envs.append(None if env is None else dict(env))
        self.inputs.append(input)
        self.inherited.append(pages.KEY_ENV in os.environ)
        out, rc = "", 0
        tool = argv[0]
        match tool:
            case "gpg":
                self.gpg(argv, input, env)
            case "rpmsign":
                target = Path(argv[-1])
                target.write_bytes(target.read_bytes() + b"SIGNED")
                self.signed.append(target.name)
            case "rpmkeys" if "--checksig" in argv:
                out, rc = self.checksig, self.checksig_rc
            case "rpm":
                path = argv[-1]
                side = "live" if "/live/" in path else "new"
                name = Path(path).name
                out = self.headers.get((side, name), f"hdr-{name}")
            case "createrepo_c":
                repodata = Path(argv[-1]) / "repodata"
                repodata.mkdir()
                (repodata / "repomd.xml").write_text("<repomd/>")
            case "git":
                out, rc = self.git(argv, cwd or "")
        if tool == "gpg" and "--import-options" in argv:
            out = colons(self.public_fprs, "pub")
        elif tool == "gpg" and "--list-secret-keys" in argv:
            out = colons(self.secret_fprs, "sec")
        return subprocess.CompletedProcess(argv, rc, out, "")

    def gpg(self, argv: list[str], stdin: str | None, env: Mapping[str, str] | None) -> None:
        self.gnupghome = (env or {}).get("GNUPGHOME", "")
        if "--import-options" in argv or "--list-secret-keys" in argv:
            return
        if "--import" in argv:
            self.imported = stdin
            return
        for flag in ("--detach-sign", "--clearsign"):
            if flag in argv:
                Path(argv[argv.index("-o") + 1]).write_text(f"signature {flag}\n")
                self.signs.append(Path(argv[-1]).name)

    def git(self, argv: list[str], cwd: str) -> tuple[str, int]:
        match argv[1]:
            case "fetch":
                return "", 0 if self.gh_pages else 128
            case "checkout":
                for rel, data in self.live_tree.items():
                    (Path(cwd) / rel).parent.mkdir(parents=True, exist_ok=True)
                    (Path(cwd) / rel).write_bytes(data)
            case "rev-parse":
                return (OLD if cwd.endswith("/live") else NEW) + "\n", 0
            case "commit":
                base = Path(cwd)
                self.tree = {
                    str(p.relative_to(base)): p.read_bytes() for p in base.rglob("*") if p.is_file()
                }
                self.home_at_commit = bool(self.gnupghome) and Path(self.gnupghome).exists()
            case "push":
                self.pushed = True
        return "", 0

    def http(
        self, method: str, url: str, headers: Mapping[str, str], data: bytes | None = None
    ) -> Response:
        self.http_calls.append((method, url, dict(headers)))
        api = "https://api.github.com"
        if url.startswith(api):
            path = url.removeprefix(api)
            m = re.fullmatch(r"/repos/([^/]+/[^/]+)/releases\?per_page=30", path)
            if method == "GET" and m and m.group(1) in self.releases:
                return Response(200, json.dumps(self.releases[m.group(1)]).encode())
            if path == f"/repos/{REPO}/pages/builds" and method == "POST":
                return Response(self.post_status, b"{}")
            if path == f"/repos/{REPO}/pages/builds/latest" and method == "GET":
                doc = self.latest or {"commit": NEW, "status": "built"}
                return Response(200, json.dumps(doc).encode())
        elif url == f"{SITE}/index.json":
            if self.index is not None:
                return Response(200, json.dumps(self.index).encode())
        elif url in self.blobs:
            return Response(200, self.blobs[url])
        return Response(404, b"{}")

    @property
    def posted(self) -> bool:
        return any(m == "POST" for m, _, _ in self.http_calls)

    def downloads(self) -> list[tuple[str, str, dict[str, str]]]:
        return [c for c in self.http_calls if urlsplit(c[1]).hostname != "api.github.com"]


def make(
    toml: str | None = None,
) -> tuple[FakeTools, dict[str, str], Path, tempfile.TemporaryDirectory[str]]:
    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    root = base / "ws"
    (root / ".git").mkdir(parents=True)
    (base / "rt").mkdir()
    (root / "sdlc.toml").write_text(toml_text() if toml is None else toml)
    (root / "crunchtools.asc").write_text("PUBLIC KEY BLOCK\n")
    (root / "index.html").write_text("<h1>packages</h1>\n")
    (root / "index.json").write_text("[]\n")  # a static file that may not be named
    tools = FakeTools(root)
    tools.add_release("crunchtools/petit", "v3.0-draft", ["petit-3.0-1.x86_64.rpm"], draft=True)
    tools.add_release("crunchtools/petit", "v2.1-rc1", ["petit-2.1-1.x86_64.rpm"], pre=True)
    tools.add_release(
        "crunchtools/petit",
        "v2.0",
        [RPM_PETIT2, "petit-2.0-1.src.rpm", DEB_PETIT2, "petit.tar.gz"],
    )
    tools.add_release("crunchtools/petit", "v1.0", [RPM_PETIT1])
    tools.add_release("crunchtools/petit", "v0.9", ["petit-0.9-1.x86_64.rpm"])  # beyond keep
    tools.add_release("crunchtools/claude-agent", "v1.0", [RPM_AGENT])
    env = {
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKSPACE": str(root),
        "GITHUB_TOKEN": "ghs_tokenvalue",
        "RUNNER_TEMP": str(base / "rt"),
        pages.KEY_ENV: KEY,
    }
    return tools, env, root, tmp


def run(tools: FakeTools, env: dict[str, str], name: str = "push") -> int:
    """pages.run with the Pages wait's sleep stubbed, and the key in the process env first."""
    orig = pages.deploy
    pages.deploy = lambda t, e, r, s: orig(t, e, r, s, sleep=lambda _s: None)
    if pages.KEY_ENV in env:
        os.environ[pages.KEY_ENV] = env[pages.KEY_ENV]
    try:
        return pages.run(name, EVENT, env, tools)
    finally:
        pages.deploy = orig
        os.environ.pop(pages.KEY_ENV, None)


def refusal(tools: FakeTools, env: dict[str, str], name: str = "push") -> str:
    try:
        run(tools, env, name)
    except CannotRunError as exc:
        return str(exc)
    raise AssertionError("expected a refusal (exit 2)")


def refused(
    tweak: Callable[[FakeTools, dict[str, str]], None] | None = None,
    *,
    toml: str | None = None,
    name: str = "push",
) -> str:
    """The refusal message for this scenario; nothing was pushed or built meanwhile."""
    tools, env, _root, tmp = make(toml)
    with tmp:
        if tweak:
            tweak(tools, env)
        msg = refusal(tools, env, name)
        assert not tools.pushed and not tools.posted, "published after a refusal"
        assert not any(i for i in tools.inherited), "the key was in os.environ"
        return msg


def test_happy_path() -> None:
    tools, env, root, tmp = make()
    with tmp:
        assert run(tools, env) == 0
        # the key reaches gpg on stdin, never an argv, never an env, never os.environ
        assert tools.imported == KEY
        assert not any("SECRETKEYMATERIAL" in " ".join(a) for a in tools.argvs)
        assert [i for i in tools.inputs if i is not None] == [KEY]
        assert pages.KEY_ENV not in os.environ
        assert not any(tools.inherited)
        assert all(e is None or pages.KEY_ENV not in e for e in tools.envs)
        assert not any(KEY in str(e) for e in tools.envs)
        assert pages.KEY_ENV in env  # the caller's mapping is not rewritten
        home = tools.gnupghome
        assert home
        for argv, e in zip(tools.argvs, tools.envs, strict=True):
            if argv[0] in ("gpg", "rpmsign"):
                assert e is not None and e["GNUPGHOME"] == home, argv
        assert tools.home_at_commit is False, "the GNUPGHOME was still there at the commit"
        assert not Path(home).exists()
        # the RPMs are signed with the key's fingerprint
        rpmsign = [a for a in tools.argvs if a[0] == "rpmsign"]
        assert len(rpmsign) == 3
        for a in rpmsign:
            assert a[a.index("--define") + 1] == f"_gpg_name {FPR}"
            assert f"_gpg_path {home}" in a
        assert set(tools.signed) == {RPM_PETIT2, RPM_PETIT1, RPM_AGENT}
        # the pushed tree
        tree = tools.tree
        for want in (
            "rpm/repodata/repomd.xml",
            "rpm/repodata/repomd.xml.asc",
            "rpm/crunchtools.repo",
            "deb/Packages",
            "deb/Packages.gz",
            "deb/Release",
            "deb/InRelease",
            "deb/Release.gpg",
            "deb/crunchtools.sources",
            "crunchtools.asc",
            "index.html",
            "index.json",
            ".nojekyll",
            f"deb/{DEB_PETIT2}",
        ):
            assert want in tree, f"{want} not in {sorted(tree)}"
        assert {p for p in tree if p.startswith("rpm/") and p.endswith(".rpm")} == {
            f"rpm/{RPM_PETIT2}",
            f"rpm/{RPM_PETIT1}",
            f"rpm/{RPM_AGENT}",
        }
        assert all(tree[p].endswith(b"SIGNED") for p in tree if p.endswith(".rpm"))
        assert tree["crunchtools.asc"] == b"PUBLIC KEY BLOCK\n"
        assert tree["index.html"] == b"<h1>packages</h1>\n"
        assert tree[".nojekyll"] == b""
        assert b"Package: petit" in tree["deb/Packages"]
        assert b"gpgkey=" + SITE.encode() + b"/crunchtools.asc" in tree["rpm/crunchtools.repo"]
        assert f"URIs: {SITE}/deb/".encode() in tree["deb/crunchtools.sources"]
        assert sorted(tools.signs) == ["Release", "Release", "repomd.xml"]
        index = json.loads(tree["index.json"])
        assert [i["file"] for i in index] == sorted([RPM_PETIT2, RPM_PETIT1, RPM_AGENT, DEB_PETIT2])
        # index.json names the downloaded bytes, not the signed ones
        by_name = {i["file"]: i["sha256"] for i in index}
        assert by_name[RPM_AGENT] == sha(f"RPM {RPM_AGENT} v1.0".encode())
        # one push, leased on the old head, to gh-pages
        push = next(a for a in tools.argvs if a[:2] == ["git", "push"])
        assert f"--force-with-lease=refs/heads/gh-pages:{OLD}" in push
        assert push[-2:] == ["origin", "HEAD:refs/heads/gh-pages"]
        assert sum(1 for a in tools.argvs if a[:2] == ["git", "push"]) == 1
        # the Pages build was requested after the push, and awaited
        verbs = [(m, u.removeprefix("https://api.github.com")) for m, u, _ in tools.http_calls]
        assert ("POST", f"/repos/{REPO}/pages/builds") in verbs
        assert ("GET", f"/repos/{REPO}/pages/builds/latest") in verbs
        assert verbs.index(("POST", f"/repos/{REPO}/pages/builds")) < verbs.index(
            ("GET", f"/repos/{REPO}/pages/builds/latest")
        )
        # the work dir is gone, the checkout untouched
        assert not list((Path(env["RUNNER_TEMP"])).iterdir())
        assert (root / "crunchtools.asc").read_text() == "PUBLIC KEY BLOCK\n"


def test_downloads_carry_no_token() -> None:
    tools, env, _root, tmp = make()
    with tmp:
        run(tools, env)
        got = tools.downloads()
        assert len(got) == 4
        for _m, url, headers in got:
            assert url.startswith("https://github.com/crunchtools/")
            assert not any(k.lower() == "authorization" for k in headers), url
        api = [h for _m, u, h in tools.http_calls if urlsplit(u).hostname == "api.github.com"]
        assert api and all(h.get("Authorization") == "Bearer ghs_tokenvalue" for h in api)


def test_pool_leaves_out_what_it_should() -> None:
    tools, env, _root, tmp = make()
    with tmp:
        run(tools, env)
        files = {Path(p).name for p in tools.tree if p.endswith((".rpm", ".deb"))}
        assert files == {RPM_PETIT2, RPM_PETIT1, RPM_AGENT, DEB_PETIT2}
        fetched = {u.rsplit("/", 1)[1] for _m, u, _h in tools.downloads()}
        assert "petit-2.0-1.src.rpm" not in fetched  # source RPM
        assert "petit-3.0-1.x86_64.rpm" not in fetched  # draft
        assert "petit-2.1-1.x86_64.rpm" not in fetched  # prerelease
        assert "petit-0.9-1.x86_64.rpm" not in fetched  # beyond keep = 2
        assert "petit.tar.gz" not in fetched  # not a package


def test_keep_one() -> None:
    tools, env, _root, tmp = make(toml_text(keep="1"))
    with tmp:
        run(tools, env)
        assert {Path(p).name for p in tools.tree if p.endswith(".rpm")} == {RPM_PETIT2, RPM_AGENT}


def test_live_signed_rpm_is_kept() -> None:
    tools, env, _root, tmp = make()
    with tmp:
        tools.live_tree = {f"rpm/{RPM_PETIT2}": b"LIVE SIGNED BYTES"}
        run(tools, env)
        assert RPM_PETIT2 not in tools.signed
        assert tools.tree[f"rpm/{RPM_PETIT2}"] == b"LIVE SIGNED BYTES"
        assert set(tools.signed) == {RPM_PETIT1, RPM_AGENT}
        # the kept package is still in the index, by the bytes the release carries
        index = {i["file"]: i["sha256"] for i in json.loads(tools.tree["index.json"])}
        assert index[RPM_PETIT2] == sha(f"RPM {RPM_PETIT2} v2.0".encode())
        assert any(a[:1] == ["rpmkeys"] and "--import" in a for a in tools.argvs)


def test_live_rpm_with_another_header_is_resigned() -> None:
    tools, env, _root, tmp = make()
    with tmp:
        tools.live_tree = {f"rpm/{RPM_PETIT2}": b"LIVE SIGNED BYTES"}
        tools.headers[("live", RPM_PETIT2)] = "a different header"
        run(tools, env)
        assert RPM_PETIT2 in tools.signed
        assert tools.tree[f"rpm/{RPM_PETIT2}"] != b"LIVE SIGNED BYTES"


def test_live_rpm_with_bad_signature_is_resigned() -> None:
    for out, rc in (("digests SIGNATURES NOT OK\n", 1), ("digests OK\n", 0)):
        tools, env, _root, tmp = make()
        with tmp:
            tools.live_tree = {f"rpm/{RPM_PETIT2}": b"LIVE SIGNED BYTES"}
            tools.checksig, tools.checksig_rc = out, rc
            run(tools, env)
            assert RPM_PETIT2 in tools.signed, out


def test_no_gh_pages_branch_yet() -> None:
    tools, env, _root, tmp = make()
    with tmp:
        tools.gh_pages = False
        run(tools, env)
        push = next(a for a in tools.argvs if a[:2] == ["git", "push"])
        assert "--force-with-lease=refs/heads/gh-pages:" in push
        assert set(tools.signed) == {RPM_PETIT2, RPM_PETIT1, RPM_AGENT}


def test_schedule_with_nothing_new_publishes_nothing() -> None:
    tools, env, _root, tmp = make()
    with tmp:
        run(tools, env)
        live = dict(tools.tree)
        tools.pushed, tools.posted_before = False, len(tools.http_calls)
        tools.live_tree = live
        tools.signed.clear()
        assert run(tools, env, "schedule") == 0
        assert not tools.pushed
        assert not tools.signed  # every live RPM kept
        assert not any(m == "POST" for m, _u, _h in tools.http_calls[tools.posted_before :])
        # a push publishes all the same
        assert run(tools, env, "push") == 0
        assert tools.pushed


def test_schedule_with_a_new_package_publishes() -> None:
    tools, env, _root, tmp = make()
    with tmp:
        run(tools, env)
        tools.live_tree = {k: v for k, v in tools.tree.items() if "agent" not in k}
        tools.live_tree["index.json"] = b"[]\n"
        tools.pushed = False
        assert run(tools, env, "schedule") == 0
        assert tools.pushed


def test_wrong_event() -> None:
    assert "not 'pull_request'" in refused(name="pull_request")


def test_not_the_default_branch() -> None:
    msg = refused(lambda t, e: e.update(GITHUB_REF="refs/heads/dev"))
    assert "default branch only" in msg
    msg = refused(lambda t, e: e.update(GITHUB_REF="refs/tags/v1"), name="workflow_dispatch")
    assert "default branch only" in msg


def test_key_unset() -> None:
    assert "SDLC_PAGES_GPG_KEY is not set" in refused(lambda t, e: e.pop(pages.KEY_ENV))
    assert "is not set" in refused(lambda t, e: e.update({pages.KEY_ENV: ""}))


def test_key_fingerprint_mismatch() -> None:
    def tweak(t: FakeTools, e: dict[str, str]) -> None:
        t.secret_fprs = [OTHER_FPR]

    assert "is not the one crunchtools.asc publishes" in refused(tweak)


def test_two_secret_keys_are_refused() -> None:
    def tweak(t: FakeTools, e: dict[str, str]) -> None:
        t.secret_fprs = [FPR, OTHER_FPR]
        t.public_fprs = [FPR, OTHER_FPR]

    assert "is not the one" in refused(tweak)


def test_secret_subkey_fingerprint_does_not_count() -> None:
    def tweak(t: FakeTools, e: dict[str, str]) -> None:
        t.public_fprs = ["C" * 40]  # only the subkey's fingerprint matches

    assert "is not the one" in refused(tweak)


def test_download_digest_mismatch() -> None:
    def tweak(t: FakeTools, e: dict[str, str]) -> None:
        url = t.releases["crunchtools/claude-agent"][0]["assets"][0]["browser_download_url"]
        t.blobs[url] = b"tampered"

    msg = refused(tweak)
    assert RPM_AGENT in msg and "GitHub stored" in msg


def test_download_status_is_checked() -> None:
    def tweak(t: FakeTools, e: dict[str, str]) -> None:
        url = t.releases["crunchtools/claude-agent"][0]["assets"][0]["browser_download_url"]
        del t.blobs[url]

    assert "answered 404" in refused(tweak)


def test_download_url_outside_the_product() -> None:
    def tweak(t: FakeTools, e: dict[str, str]) -> None:
        asset = t.releases["crunchtools/claude-agent"][0]["assets"][0]
        asset["browser_download_url"] = "https://evil.example/releases/download/v1.0/x.rpm"

    assert "is not the release's" in refused(tweak)

    def other_repo(t: FakeTools, e: dict[str, str]) -> None:
        asset = t.releases["crunchtools/claude-agent"][0]["assets"][0]
        asset["browser_download_url"] = (
            f"https://github.com/crunchtools/petit/releases/download/v1/{RPM_AGENT}"
        )

    assert "is not the release's" in refused(other_repo)


def test_same_file_from_two_products() -> None:
    def tweak(t: FakeTools, e: dict[str, str]) -> None:
        t.add_release("crunchtools/claude-agent", "v0.5", [RPM_PETIT1])

    msg = refused(tweak)
    assert RPM_PETIT1 in msg and "shipped by both" in msg


def test_no_packages_at_all() -> None:
    def tweak(t: FakeTools, e: dict[str, str]) -> None:
        t.releases = {
            "crunchtools/petit": [
                {"tag_name": "v1", "draft": False, "prerelease": False, "assets": []}
            ],
            "crunchtools/claude-agent": [],
        }

    assert "empty site" in refused(tweak)


def test_release_list_unavailable() -> None:
    def tweak(t: FakeTools, e: dict[str, str]) -> None:
        del t.releases["crunchtools/claude-agent"]

    assert "answered 404" in refused(tweak)


def test_bad_settings() -> None:
    cases = [
        (toml_text(stray="1"), "stray"),
        (toml_text(name='"Bad Name"'), "name must be a lowercase id"),
        (toml_text(site_url='"http://crunchtools.github.io/packages"'), "site-url"),
        (toml_text(site_url=f'"{SITE}/"'), "site-url"),
        (toml_text(key='"missing.asc"'), "key"),
        (toml_text(key='"../crunchtools.asc"'), "key"),
        (toml_text(keep="0"), "keep must be"),
        (toml_text(keep="21"), "keep must be"),
        (toml_text(keep="true"), "keep must be"),
        (toml_text(products="[]"), "products must be"),
        (toml_text(products='["crunchtools/petit", "crunchtools/petit"]'), "products must be"),
        (toml_text(products='["not-a-repo"]'), "products must be"),
        (toml_text(static='["index.json"]'), "static files"),
        (toml_text(static='["crunchtools.asc"]'), "static files"),
        (toml_text(static='["missing.html"]'), "static"),
        (toml_text(static='"index.html"'), "static must be a list"),
        ("[tool.sdlc]\n", "no [tool.sdlc.pages]"),
    ]
    for text, fragment in cases:
        assert fragment in refused(toml=text), (text, fragment)


def test_pages_build_errors() -> None:
    def tweak(t: FakeTools, e: dict[str, str]) -> None:
        t.latest = {"commit": NEW, "status": "errored", "error": {"message": "jekyll boom"}}

    tools, env, _root, tmp = make()
    with tmp:
        tweak(tools, env)
        msg = refusal(tools, env)
        assert "errored" in msg and NEW in msg
        assert tools.pushed and tools.posted


def test_pages_build_post_refused() -> None:
    tools, env, _root, tmp = make()
    with tmp:
        tools.post_status = 403
        assert "answered 403" in refusal(tools, env)


def test_pages_build_never_reports_the_commit() -> None:
    tools, env, _root, tmp = make()
    with tmp:
        tools.latest = {"commit": "f" * 40, "status": "built"}  # a stale build
        sleeps: list[float] = []
        orig = pages.deploy
        pages.deploy = lambda t, e, r, s: orig(t, e, r, s, sleep=sleeps.append)
        os.environ[pages.KEY_ENV] = env[pages.KEY_ENV]
        try:
            pages.run("push", EVENT, env, tools)
        except CannotRunError as exc:
            assert "did not finish" in str(exc)
        else:
            raise AssertionError("expected a timeout")
        finally:
            pages.deploy = orig
            os.environ.pop(pages.KEY_ENV, None)
        assert sleeps and set(sleeps) == {pages.BUILD_POLL}
        assert len(sleeps) == pages.BUILD_WAIT // pages.BUILD_POLL + 1


def test_pages_build_pending_then_built() -> None:
    tools, env, _root, tmp = make()
    with tmp:
        states = iter(["building", "building", "built"])
        orig_http = tools.http

        def http(method: str, url: str, headers: Mapping[str, str], data: bytes | None = None):
            if url.endswith("/pages/builds/latest"):
                tools.http_calls.append((method, url, dict(headers)))
                doc = {"commit": NEW, "status": next(states)}
                return Response(200, json.dumps(doc).encode())
            return orig_http(method, url, headers, data)

        with patch.object(tools, "http", http):
            assert run(tools, env) == 0
        polls = [u for _m, u, _h in tools.http_calls if u.endswith("/builds/latest")]
        assert len(polls) == 3


def index_for(tools: FakeTools) -> list[dict[str, str]]:
    """index.json as the products' releases now say it: files by name, with stored digests."""
    rows = {}
    for rels in tools.releases.values():
        taken = [r for r in rels if not r["draft"] and not r["prerelease"]][:2]
        for rel in taken:
            for a in rel["assets"]:
                if re.fullmatch(r".*\.(rpm|deb)", a["name"]) and not a["name"].endswith(".src.rpm"):
                    rows.setdefault(a["name"], a["digest"].removeprefix("sha256:"))
    return [{"file": n, "sha256": rows[n]} for n in sorted(rows)]


def test_changed_when_asset_has_no_digest() -> None:
    tools, env, root, tmp = make()
    with tmp:
        tools.releases["crunchtools/claude-agent"][0]["assets"][0]["digest"] = None
        tools.index = []  # irrelevant: an asset without a digest cannot be compared
        assert pages.changed(tools, env, root) is True


def test_changed_when_index_differs() -> None:
    tools, env, root, tmp = make()
    with tmp:
        want = index_for(tools)
        tools.index = want[:-1]
        assert pages.changed(tools, env, root) is True
        tools.index = [{**want[0], "sha256": "0" * 64}, *want[1:]]
        assert pages.changed(tools, env, root) is True


def test_changed_when_no_index_is_served() -> None:
    tools, env, root, tmp = make()
    with tmp:
        tools.index = None
        assert pages.changed(tools, env, root) is True


def test_unchanged_when_index_equal() -> None:
    tools, env, root, tmp = make()
    with tmp:
        live = index_for(tools)
        tools.index = live
        assert len(live) == 4
        assert pages.changed(tools, env, root) is False
        assert not tools.downloads() or all(
            u == f"{SITE}/index.json" for _m, u, _h in tools.downloads()
        )


def plan(
    tools: FakeTools, env: dict[str, str], root: Path, *, now: bool, cron: object
) -> tuple[bool, list[bool]]:
    """check.pages_plan with pages.changed stubbed to answer True; (plan, calls made)."""
    calls: list[bool] = []
    with patch.object(pages, "changed", lambda t, e, r: calls.append(True) or True):
        return check.pages_plan(tools, env, root, now=now, cron=cron), calls


def test_plan_false_without_pages_table() -> None:
    tools, env, root, tmp = make("[tool.sdlc]\n")
    with tmp:
        for immediate, cron in ((True, None), (False, check.WEEKLY), (False, check.POLL)):
            got, calls = plan(tools, env, root, now=immediate, cron=cron)
            assert got is False and not calls


def test_plan_true_now() -> None:
    tools, env, root, tmp = make()
    with tmp:
        got, calls = plan(tools, env, root, now=True, cron=None)
        assert got is True and not calls


def test_plan_false_for_other_cron() -> None:
    tools, env, root, tmp = make()
    with tmp:
        for cron in ("0 0 * * *", None, ""):
            got, calls = plan(tools, env, root, now=False, cron=cron)
            assert got is False and not calls, cron


def test_plan_delegates_on_the_two_crons() -> None:
    tools, env, root, tmp = make()
    with tmp:
        for cron in (check.WEEKLY, check.POLL):
            got, calls = plan(tools, env, root, now=False, cron=cron)
            assert got is True and calls == [True], cron
        with patch.object(pages, "changed", lambda t, e, r: False):
            for cron in (check.WEEKLY, check.POLL):
                assert check.pages_plan(tools, env, root, now=False, cron=cron) is False


def test_plan_real_changed_through_the_poll() -> None:
    tools, env, root, tmp = make()
    with tmp:
        tools.index = index_for(tools)
        assert check.pages_plan(tools, env, root, now=False, cron=check.POLL) is False
        tools.index = []
        assert check.pages_plan(tools, env, root, now=False, cron=check.POLL) is True


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
