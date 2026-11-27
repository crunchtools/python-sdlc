#!/opt/constitution-venv/bin/python3
"""a1796 Dolza proof driver. Runs INSIDE the built sdlc image (run.sh step 4).

    drive.py assets <work> <socket-url>   build + attach, per repo, fake GitHub
    drive.py pages  <work>                sign + publish to a local bare repo, verify

Real: processes (podman build through the socket, git, gpg, rpmsign, createrepo_c,
rpmkeys, gpgv), the sdlc_jobs code in the image. Faked: only the GitHub API,
release downloads/uploads, and the Pages build request (pages.deploy).
Results are appended to <work>/results.json; FINDINGs are printed loudly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path

from sdlc_jobs import _verify_release, assets, pages
from sdlc_jobs._core import CannotRunError, Response, Tools

BOT = "ashigaru-crunchtools-com[bot]"
REPOS = ("crunchtools/ashigaru", "crunchtools/petit")
SITE_URL = "https://packages.invalid"
KEY_ALGO = os.environ.get("KEY_ALGO", "rsa4096")


def sh(*argv: str, **kw) -> str:
    done = subprocess.run(argv, capture_output=True, text=True, **kw)
    if done.returncode != 0:
        raise SystemExit(f"FAILED {argv}: rc={done.returncode}\n{done.stdout}\n{done.stderr}")
    return done.stdout


def git(cwd: Path, *args: str) -> str:
    return sh("git", "-c", "user.name=a1796-proof", "-c", "user.email=proof@example.invalid",
              *args, cwd=cwd)


def finding(work: Path, text: str) -> None:
    print(f"\n!!! FINDING: {text}\n", flush=True)
    with (work / "findings.txt").open("a") as fh:
        fh.write(text + "\n")


class GitHub(Tools):
    """Real processes; the GitHub API, uploads and downloads answered here."""

    def __init__(self, routes: dict, origin: Path | None = None, pool: Path | None = None) -> None:
        self.routes, self.origin, self.pool = routes, origin, pool
        self.uploads: list[tuple[str, str]] = []
        self.assets: list[dict] = []

    def run(self, argv, **kw):
        if self.origin and argv[:3] == ["git", "remote", "add"]:
            argv = [*argv[:4], str(self.origin)]  # origin is a local bare repo
        return super().run(argv, **kw)

    def http(self, method: str, url: str, headers: Mapping[str, str], payload=None) -> Response:
        if url.startswith("https://uploads.github.com/"):
            name = url.split("name=")[1]
            digest = "sha256:" + hashlib.sha256(payload).hexdigest()
            self.uploads.append((name, digest))
            self.assets.append({"id": len(self.assets) + 1, "name": name, "digest": digest})
            if self.pool is not None:
                (self.pool / name).write_bytes(payload)
            return Response(201, json.dumps({"digest": digest}).encode())
        path = url.removeprefix("https://api.github.com")
        if path.endswith("/releases/9/assets?per_page=100"):
            return Response(200, json.dumps(self.assets).encode())
        if method == "DELETE":
            return Response(204, b"")
        got = self.routes.get((method, path)) or self.routes.get((method, url))
        if got is None:
            return Response(404, b"{}")
        body = got if isinstance(got, bytes) else json.dumps(got).encode()
        return Response(200 if method == "GET" else 201, body)


def project_version(clone: Path) -> str:
    """The version the adoption's release would carry: pyproject, else VERSION."""
    doc = tomllib.loads((clone / "pyproject.toml").read_text())
    v = doc["project"].get("version")
    if isinstance(v, str):
        return v
    return (clone / "VERSION").read_text().strip()


def prove_assets(work: Path, sock: str) -> None:
    for repo in REPOS:
        short = repo.split("/")[1]
        clone = work / short
        print(f"\n===== assets: {repo} =====", flush=True)
        version = project_version(clone)
        changelog = (clone / "CHANGELOG.md").read_text()
        heads = [h for h in re.findall(r"^## \[([^\]]+)\]", changelog, re.M) if h != "Unreleased"]
        if heads[0] != version:
            finding(work, f"{repo}: CHANGELOG newest release {heads[0]} != version {version}; "
                    "the proof rewrites nothing, so verify_release will refuse")
        # verify_release needs a static [project].version (test 7).
        try:
            _verify_release.tree_version(clone)
        except CannotRunError as exc:
            finding(work, f"{repo}: verify_release refuses this tree ({exc}). assets.run re-derives "
                    "the release through verify_release, so adopting assets in this repo needs a "
                    "static [project].version (ashigaru: version is dynamic via VERSION). "
                    "PROOF WORKAROUND in the throwaway clone only: pyproject gets "
                    f'version = "{version}" in place of dynamic = ["version"].')
            p = clone / "pyproject.toml"
            text = p.read_text()
            assert 'dynamic = ["version"]' in text, "cannot apply the proof-only static version"
            p.write_text(text.replace('dynamic = ["version"]', f'version = "{version}"', 1))
            git(clone, "commit", "-qam", "proof only: static version for verify_release")
        sha = git(clone, "rev-parse", "HEAD").strip()
        pull = {
            "number": 7, "merged_at": "x", "merge_commit_sha": sha, "user": {"login": BOT},
            "base": {"ref": "main"},
            "head": {"ref": f"ashigaru/release-{version}", "repo": {"full_name": repo}},
        }
        routes = {
            ("GET", f"/repos/{repo}/commits/{sha}/pulls"): [pull],
            ("GET", f"/repos/{repo}"): {"default_branch": "main"},
            ("GET", f"/repos/{repo}/commits/v{version}"): {"sha": sha},
            ("GET", f"/repos/{repo}/releases/tags/v{version}"): {
                "id": 9, "tag_name": f"v{version}", "draft": False,
                "upload_url": f"https://uploads.github.com/repos/{repo}/releases/9/assets{{?name,label}}",
            },
        }
        pool = work / "pool" / short
        pool.mkdir(parents=True, exist_ok=True)
        gh = GitHub(routes, pool=pool)
        tmp = work / "tmp"
        tmp.mkdir(exist_ok=True)
        env = {
            **os.environ, "GITHUB_REPOSITORY": repo, "GITHUB_SHA": sha,
            "GITHUB_WORKSPACE": str(clone), "GITHUB_TOKEN": "fake", "CONTAINER_HOST": sock,
            "RUNNER_TEMP": str(tmp), "SDLC_WRITE": "success",
            "SDLC_RELEASE": json.dumps({"version": version, "sha": sha}),
        }
        rc = assets.run("push", {}, env, gh)
        assert rc == 0, rc
        names = sorted(p.name for p in pool.iterdir())
        print(f"\n{repo} v{version}: {len(names)} files uploaded")
        for n in names:
            print(f"  {(pool / n).stat().st_size:>10}  {n}")
        assert "SHA256SUMS" in names, names
        assert all(version in n for n in names if n != "SHA256SUMS"), names
        sums = (pool / "SHA256SUMS").read_text()
        for n in names:
            if n != "SHA256SUMS":
                want = hashlib.sha256((pool / n).read_bytes()).hexdigest()
                assert f"{want}  {n}\n" in sums, f"SHA256SUMS lacks {n}"
        if short == "ashigaru":
            for dist in ("fc43", "fc45", "el10"):
                for kind in ("noarch", "src"):
                    want = f"ashigaru-{version}-1.{dist}.{kind}.rpm"
                    assert want in names, f"missing {want}; have {names} (dist tag differs?)"
        else:
            assert any(n.endswith(".rpm") and ".src." not in n for n in names), names
            assert any(n.endswith(".deb") for n in names), names
        print(f"assets OK for {repo}: files exist, carry {version}, SHA256SUMS matches")
        (work / f"version-{short}").write_text(version)


def throwaway_key(home: Path, uid: str) -> tuple[str, str]:
    home.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(home)}
    sh("gpg", "--batch", "--passphrase", "", "--quick-gen-key", uid, KEY_ALGO, "sign", "1d", env=env)
    secret = sh("gpg", "--batch", "--armor", "--pinentry-mode", "loopback", "--passphrase", "",
                "--export-secret-keys", uid, env=env)
    public = sh("gpg", "--batch", "--armor", "--export", uid, env=env)
    return secret, public


def prove_pages(work: Path) -> None:
    print(f"\n===== pages: throwaway {KEY_ALGO} key, real gpg/rpmsign/createrepo_c =====", flush=True)
    secret, public = throwaway_key(work / "gnupg-site", "a1796 proof <proof@example.invalid>")
    (work / "tmp").mkdir(exist_ok=True)
    root = work / "packages"
    root.mkdir()
    (root / "sdlc.toml").write_text(
        '[tool.sdlc.pages]\nname = "crunchtools"\n'
        f'site-url = "{SITE_URL}"\nkey = "crunchtools.asc"\n'
        f'products = ["crunchtools/petit", "crunchtools/ashigaru"]\nkeep = 2\n'
        'static = ["index.html"]\n')
    (root / "crunchtools.asc").write_text(public)
    (root / "index.html").write_text("<p>a1796 proof site</p>\n")
    git(root, "init", "-q", "-b", "main", ".")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "packages tree")
    sha = git(root, "rev-parse", "HEAD").strip()

    routes: dict = {}
    for repo in REPOS:
        short = repo.split("/")[1]
        version = (work / f"version-{short}").read_text()
        rel_assets = []
        for p in sorted((work / "pool" / short).iterdir()):
            url = f"https://github.com/{repo}/releases/download/v{version}/{p.name}"
            digest = "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
            rel_assets.append({"name": p.name, "browser_download_url": url, "digest": digest})
            routes[("GET", url)] = p.read_bytes()
        routes[("GET", f"/repos/{repo}/releases?per_page=30")] = [
            {"tag_name": f"v{version}", "draft": False, "prerelease": False, "assets": rel_assets}]
    origin = work / "origin.git"
    sh("git", "init", "-q", "--bare", "-b", "main", str(origin))
    gh = GitHub(routes, origin)
    env = {**os.environ, "GITHUB_REPOSITORY": "crunchtools/packages", "GITHUB_SHA": sha,
           "GITHUB_WORKSPACE": str(root), "GITHUB_TOKEN": "fake", "GITHUB_REF": "refs/heads/main",
           "RUNNER_TEMP": str(work / "tmp"), "SDLC_PAGES_GPG_KEY": secret}
    pushed: list[str] = []

    def deploy(tools, env, repo, sha):  # noqa: ANN001 -- the Pages build is GitHub's
        pushed.append(sha)
        print("pages: (proof) would request and await the Pages build of", sha)

    pages.deploy = deploy
    assert pages.run("workflow_dispatch", {"repository": {"default_branch": "main"}}, env, gh) == 0
    assert "SDLC_PAGES_GPG_KEY" not in os.environ

    site = work / "site"
    sh("git", "clone", "-q", "-b", "gh-pages", str(origin), str(site))
    head = git(site, "rev-parse", "HEAD").strip()
    assert pushed == [head], (pushed, head)
    tree = sorted(str(p.relative_to(site)) for p in site.rglob("*")
                  if p.is_file() and ".git/" not in str(p))
    print("gh-pages tree:")
    for t in tree:
        print("  ", t)

    # The generated repo/sources files, as the install step will rewrite them.
    repo_file = (site / "rpm" / "crunchtools.repo").read_text()
    want = ("[crunchtools]\nname=crunchtools\nbaseurl=https://packages.invalid/rpm\nenabled=1\n"
            "gpgcheck=1\nrepo_gpgcheck=1\ngpgkey=https://packages.invalid/crunchtools.asc\n")
    assert repo_file == want, repo_file
    srcs = (site / "deb" / "crunchtools.sources").read_text()
    assert "URIs: https://packages.invalid/deb/\n" in srcs and "Suites: ./\n" in srcs, srcs
    print("generated crunchtools.repo and crunchtools.sources are as the install step assumes")

    # Signatures. A temp rpmdb that trusts only the site's public key.
    db = work / "verify-rpmdb"
    db.mkdir()
    sh("rpmkeys", "--dbpath", str(db), "--import", str(site / "crunchtools.asc"))
    rpms = sorted((site / "rpm").glob("*.rpm"))
    assert rpms, "no RPMs on the site"
    assert not list((site / "rpm").glob("*.src.rpm")), "a source RPM reached the site"
    for r in rpms:
        out = subprocess.run(["rpmkeys", "--dbpath", str(db), "--checksig", str(r)],
                             capture_output=True, text=True)
        line = (out.stdout + out.stderr).strip()
        print("  ", line)
        assert out.returncode == 0 and "signatures OK" in line, line
    # Negative control: the unsigned pool copy must NOT verify.
    ctl = next(p for p in sorted((work / "pool" / "petit").glob("petit-*.rpm")))
    out = subprocess.run(["rpmkeys", "--dbpath", str(db), "--checksig", str(ctl)],
                         capture_output=True, text=True)
    cline = (out.stdout + out.stderr).strip()
    print("   control (unsigned pool copy):", cline)
    assert "signatures OK" not in cline, "the unsigned control verified: the check proves nothing"
    print(f"{len(rpms)} RPMs on the site, every one signed by the site key")

    ring_home = work / "gnupg-verify"
    ring_home.mkdir(mode=0o700)
    ring = work / "ring.gpg"
    sh("gpg", "--batch", "--yes", "--no-default-keyring", "--keyring", str(ring), "--import",
       str(site / "crunchtools.asc"), env={**os.environ, "GNUPGHOME": str(ring_home)})
    deb = site / "deb"
    sh("gpgv", "--keyring", str(ring), str(deb / "InRelease"))
    print("InRelease: good signature")
    sh("gpgv", "--keyring", str(ring), str(deb / "Release.gpg"), str(deb / "Release"))
    print("Release.gpg: good signature")
    sh("gpgv", "--keyring", str(ring), str(site / "rpm/repodata/repomd.xml.asc"),
       str(site / "rpm/repodata/repomd.xml"))
    print("repomd.xml.asc: good signature")
    debs = sorted(deb.glob("*.deb"))
    assert debs, "no debs on the site"
    print("debs:", [d.name for d in debs])
    # Tamper control: a changed Release must fail gpgv.
    bad = work / "Release.tampered"
    bad.write_text((deb / "Release").read_text() + "x\n")
    t = subprocess.run(["gpgv", "--keyring", str(ring), str(deb / "Release.gpg"), str(bad)],
                       capture_output=True, text=True)
    assert t.returncode != 0, "a tampered Release verified"
    print("control: tampered Release is refused by gpgv")
    (work / "site-names.json").write_text(json.dumps([p.name for p in rpms]))
    print("pages OK")


def main() -> int:
    phase, work = sys.argv[1], Path(sys.argv[2])
    gitconfig = work / "proof-gitconfig"
    gitconfig.write_text("[safe]\n\tdirectory = *\n[user]\n\tname = a1796-proof\n"
                         "\temail = proof@example.invalid\n[init]\n\tdefaultBranch = main\n")
    os.environ["GIT_CONFIG_GLOBAL"] = str(gitconfig)
    if phase == "assets":
        prove_assets(work, sys.argv[3])
    elif phase == "pages":
        prove_pages(work)
    else:
        raise SystemExit(f"unknown phase {phase}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
