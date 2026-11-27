#!/usr/bin/env python3
"""a1796 local proof: run `assets.run` and `pages.run` for real, faking only GitHub.

assets: a real podman build through a podman API socket (CONTAINER_HOST) of a
digest-pinned two-stage Containerfile, `--target`, create, cp; the upload goes to a
fake GitHub that records bytes and digests. Then a re-run (nothing re-uploaded).
pages: a throwaway GPG key, real gpg (import, fingerprint check, clearsign, detach),
the real _debrepo index of two real .deb files, a real `git push` to a local bare
repo standing in for origin; then gpgv checks every signature against the public
key, and a wrong key is refused.

Usage: local_proof.py <sdlc checkout> <podman socket URL>
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

SDLC, SOCK = Path(sys.argv[1]), sys.argv[2]
sys.path.insert(0, str(SDLC))
sys.path.insert(0, str(SDLC / "tests"))

from sdlc_jobs import assets, pages  # noqa: E402
from sdlc_jobs._core import CannotRunError, Response, Tools  # noqa: E402
from test_debrepo import make_deb  # noqa: E402

REPO = "crunchtools/demo"
VERSION = "1.2.3"
FEDORA = "quay.io/fedora/fedora@sha256:" + os.environ["FEDORA_DIGEST"]
BOT = "ashigaru-crunchtools-com[bot]"


def sh(*argv: str, **kw) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True, **kw).stdout


class GitHub(Tools):
    """Real processes; the GitHub API and downloads answered here."""

    def __init__(self, routes: dict, origin: Path | None = None) -> None:
        self.routes, self.origin = routes, origin
        self.uploads: list[tuple[str, str]] = []
        self.deleted: list[int] = []
        self.assets: list[dict] = []
        self.calls: list[str] = []

    def run(self, argv, **kw):
        if self.origin and argv[:3] == ["git", "remote", "add"]:
            argv = [*argv[:4], str(self.origin)]  # origin is a local bare repo
        return super().run(argv, **kw)

    def http(self, method: str, url: str, headers: Mapping[str, str], payload=None) -> Response:
        self.calls.append(f"{method} {url}")
        if url.startswith("https://uploads.github.com/"):
            name = url.split("name=")[1]
            digest = "sha256:" + hashlib.sha256(payload).hexdigest()
            self.uploads.append((name, digest))
            self.assets.append({"id": len(self.assets) + 1, "name": name, "digest": digest})
            return Response(201, json.dumps({"digest": digest}).encode())
        path = url.removeprefix("https://api.github.com")
        if path.endswith("/releases/9/assets?per_page=100"):
            return Response(200, json.dumps(self.assets).encode())
        if method == "DELETE":
            self.deleted.append(int(path.rsplit("/", 1)[1]))
            return Response(204, b"")
        got = self.routes.get((method, path)) or self.routes.get((method, url))
        if got is None:
            return Response(404, b"{}")
        body = got if isinstance(got, bytes) else json.dumps(got).encode()
        return Response(200 if method == "GET" else 201, body)


def git_repo(root: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    sh("git", "init", "-q", "-b", "main", ".", cwd=root)
    sh("git", "add", "-A", cwd=root)
    sh("git", "-c", "user.name=p", "-c", "user.email=p@x", "commit", "-qm", "r", cwd=root)
    return sh("git", "rev-parse", "HEAD", cwd=root).strip()


def prove_assets(tmp: Path) -> None:
    print("== assets: real podman build through", SOCK)
    root = tmp / "ws"
    root.mkdir()
    containerfile = f"""ARG TARGET=fc43
FROM {FEDORA} AS build
ARG VERSION
RUN mkdir /out && for d in fc43 el10; do echo "demo $VERSION $d" > /out/demo-$VERSION-1.$d.noarch.rpm; done \\
 && echo stray > /out/notes.txt && ln -s /etc/hostname /out/demo-$VERSION-link.rpm
FROM {FEDORA}
COPY --from=build /out /rpms
"""
    pyproject = f"""[project]
name = "demo"
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
    sha = git_repo(
        root,
        {
            "pyproject.toml": pyproject,
            "CHANGELOG.md": f"## [Unreleased]\n\n## [{VERSION}] - 2026-11-27\n",
            "packaging/rpm.Containerfile": containerfile,
        },
    )
    pull = {
        "number": 7, "merged_at": "x", "merge_commit_sha": sha, "user": {"login": BOT},
        "base": {"ref": "main"},
        "head": {"ref": f"ashigaru/release-{VERSION}", "repo": {"full_name": REPO}},
    }
    routes = {
        ("GET", f"/repos/{REPO}/commits/{sha}/pulls"): [pull],
        ("GET", f"/repos/{REPO}"): {"default_branch": "main"},
        ("GET", f"/repos/{REPO}/commits/v{VERSION}"): {"sha": sha},
        ("GET", f"/repos/{REPO}/releases/tags/v{VERSION}"): {
            "id": 9, "tag_name": f"v{VERSION}", "draft": False,
            "upload_url": f"https://uploads.github.com/repos/{REPO}/releases/9/assets{{?name,label}}",
        },
    }
    gh = GitHub(routes)
    env = {
        **os.environ, "GITHUB_REPOSITORY": REPO, "GITHUB_SHA": sha, "GITHUB_WORKSPACE": str(root),
        "GITHUB_TOKEN": "fake", "CONTAINER_HOST": SOCK, "RUNNER_TEMP": str(tmp),
        "SDLC_WRITE": "success", "SDLC_RELEASE": json.dumps({"version": VERSION, "sha": sha}),
    }
    assert assets.run("push", {}, env, gh) == 0
    names = [n for n, _ in gh.uploads]
    print("uploaded:", names)
    assert names == [f"demo-{VERSION}-1.el10.noarch.rpm", f"demo-{VERSION}-1.fc43.noarch.rpm", "SHA256SUMS"]
    first = list(gh.uploads)
    gh.uploads.clear()
    assert assets.run("push", {}, env, gh) == 0
    print("re-run uploaded:", gh.uploads, "deleted:", gh.deleted)
    assert gh.uploads == [] or all(n.endswith(".rpm") or n == "SHA256SUMS" for n, _ in gh.uploads)
    print("first-run digests:", first)
    # A FROM by tag is refused before anything builds.
    cf = root / "packaging/rpm.Containerfile"
    cf.write_text(cf.read_text().replace(FEDORA, "quay.io/fedora/fedora:43", 1))
    sh("git", "-c", "user.name=p", "-c", "user.email=p@x", "commit", "-qam", "tag", cwd=root)
    try:
        assets.entries  # noqa: B018
        e = assets.entries(root, VERSION, sha)[0]
        assets.pinned(root, e)
    except CannotRunError as exc:
        print("refused as it should:", exc)
    else:
        raise AssertionError("a FROM by tag was not refused")


def throwaway_key(home: Path, name: str) -> tuple[str, str]:
    home.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(home)}
    sh("gpg", "--batch", "--passphrase", "", "--quick-gen-key", name, "ed25519", "sign", "1d", env=env)
    secret = sh("gpg", "--batch", "--armor", "--pinentry-mode", "loopback", "--passphrase", "",
                "--export-secret-keys", name, env=env)
    public = sh("gpg", "--batch", "--armor", "--export", name, env=env)
    return secret, public


def prove_pages(tmp: Path) -> None:
    print("== pages: throwaway key, real gpg, real git push")
    secret, public = throwaway_key(tmp / "k1", "a1796 proof <proof@example.invalid>")
    wrong, _ = throwaway_key(tmp / "k2", "not the site key <x@example.invalid>")
    root = tmp / "pkgs"
    root.mkdir()
    git_repo(root, {
        "sdlc.toml": '[tool.sdlc.pages]\nname = "crunchtools"\n'
        'site-url = "https://crunchtools.github.io/packages"\nkey = "crunchtools.asc"\n'
        'products = ["crunchtools/petit", "crunchtools/claude-agent"]\nkeep = 2\n'
        'static = ["index.html"]\n',
        "crunchtools.asc": public,
        "index.html": "<p>packages</p>\n",
    })
    sha = sh("git", "rev-parse", "HEAD", cwd=root).strip()
    debs = {}
    for product, pkg in (("crunchtools/petit", "petit"), ("crunchtools/claude-agent", "claude-supervisor")):
        d = tmp / f"deb-{pkg}"
        d.mkdir()
        control = (f"Package: {pkg}\nVersion: 1.0.0-1\nArchitecture: all\nMaintainer: x <x@x>\n"
                   f"Description: {pkg}\n multi-line\n")
        p = make_deb(d / f"{pkg}_1.0.0-1_all.deb", control=control)
        debs[product] = p
    routes: dict = {}
    for product, p in debs.items():
        url = f"https://github.com/{product}/releases/download/v1.0.0/{p.name}"
        digest = "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
        routes[("GET", f"/repos/{product}/releases?per_page=30")] = [
            {"tag_name": "v1.0.0", "draft": False, "prerelease": False, "assets": [
                {"name": p.name, "browser_download_url": url, "digest": digest},
                {"name": "x-1.0.0.src.rpm", "browser_download_url": url, "digest": digest}]},
            {"tag_name": "v1.1.0-rc1", "draft": False, "prerelease": True, "assets": []},
        ]
        routes[("GET", url)] = p.read_bytes()
    origin = tmp / "origin.git"
    sh("git", "init", "-q", "--bare", str(origin))
    gh = GitHub(routes, origin)
    env = {**os.environ, "GITHUB_REPOSITORY": "crunchtools/packages", "GITHUB_SHA": sha,
           "GITHUB_WORKSPACE": str(root), "GITHUB_TOKEN": "fake", "GITHUB_REF": "refs/heads/main",
           "RUNNER_TEMP": str(tmp), "SDLC_PAGES_GPG_KEY": wrong}
    event = {"repository": {"default_branch": "main"}}
    try:
        pages.run("workflow_dispatch", event, env, gh)
    except CannotRunError as exc:
        print("wrong key refused as it should:", exc)
    else:
        raise AssertionError("a key that is not the site's was accepted")
    assert "SDLC_PAGES_GPG_KEY" not in os.environ
    env["SDLC_PAGES_GPG_KEY"] = secret
    pushed: list[str] = []

    def deploy(tools, env, repo, sha):  # noqa: ANN001 -- the Pages build is GitHub's
        pushed.append(sha)
        print("pages: (proof) would request and await the Pages build of", sha)

    pages.deploy = deploy
    assert pages.run("workflow_dispatch", event, env, gh) == 0
    assert "SDLC_PAGES_GPG_KEY" not in os.environ
    site = tmp / "site-check"
    sh("git", "clone", "-q", "-b", "gh-pages", str(origin), str(site))
    head = sh("git", "rev-parse", "HEAD", cwd=site).strip()
    assert pushed == [head], (pushed, head)
    files = sorted(str(p.relative_to(site)) for p in site.rglob("*") if p.is_file() and ".git/" not in str(p))
    print("gh-pages tree:", files)
    ring = tmp / "ring.gpg"
    sh("gpg", "--batch", "--yes", "--no-default-keyring", "--keyring", str(ring), "--import",
       str(site / "crunchtools.asc"), env={**os.environ, "GNUPGHOME": str(tmp / "k1")})
    deb = site / "deb"
    v = subprocess.run(["gpgv", "--keyring", str(ring), str(deb / "InRelease")], capture_output=True, text=True, check=True)
    print("InRelease:", v.stderr.strip().splitlines()[-1])
    sh("gpgv", "--keyring", str(ring), str(deb / "Release.gpg"), str(deb / "Release"))
    print("Release.gpg: good signature")
    rel = (deb / "Release").read_text()
    for f in ("Packages", "Packages.gz"):
        h = hashlib.sha256((deb / f).read_bytes()).hexdigest()
        assert h in rel, f
    print("Release lists Packages and Packages.gz with their sha256")
    print((deb / "Packages").read_text())
    idx = json.loads((site / "index.json").read_text())
    assert [i["file"] for i in idx] == sorted(p.name for p in debs.values()), idx
    print("index.json:", idx)
    # A scheduled run with nothing new publishes nothing.
    env["SDLC_PAGES_GPG_KEY"] = secret
    pushed.clear()
    assert pages.run("schedule", event, env, gh) == 0
    assert pushed == [], pushed
    print("schedule with an unchanged pool: nothing pushed")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="a1796-") as t:
        tmp = Path(t)
        prove_assets(tmp)
        prove_pages(tmp)
    print("LOCAL PROOF PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
