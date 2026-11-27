"""`pages`: sign the released packages of other repos and publish them as a dnf/apt site.

Runs only in a repo whose tree declares `[tool.sdlc.pages]` (crunchtools/packages),
on the default branch: when `check` saw the products' releases differ from the
published `index.json` (6-hourly and weekly), on a push, or on a bare dispatch.
It is the one fleet job that holds a GPG key, `SDLC_PAGES_GPG_KEY`, mapped from
the `pages-signing` environment, which admits the default branch only and is the
key's only home. Its GITHUB_TOKEN has `contents: write` (the `gh-pages` push)
and `pages: write` (the build request); no App key.

No repo code runs here. What it signs is data: release assets of public repos,
fetched without a token and checked against the `digest` GitHub stored at
upload, and the tree's own public key, index page and products list. The key is
read from the environment once, imported into a GNUPGHOME of its own, dropped
from this process's environment, and that directory is removed before the
push. Every signing tool runs in this job's own (verified) sdlc image.

The site is the one crunchtools/packages' publish.yml built:

    <name>.asc  index.json  index.html  .nojekyll
    rpm/   *.rpm (each rpmsign'd)  repodata/ (repomd.xml.asc)  <name>.repo
    deb/   *.deb  Packages  Packages.gz  Release  InRelease  Release.gpg  <name>.sources

An RPM already on the site whose header digest matches and whose signature
checks is kept as it is, so an unchanged package keeps its bytes. The tree is
pushed to `gh-pages` as one commit (the site has no history to keep), leased
on the branch's old head, and a Pages build is requested: GitHub does not build
Pages for a push made with GITHUB_TOKEN. The job waits for that build.

    [tool.sdlc.pages]
    name = "crunchtools"                            # repo id, file names, Origin
    site-url = "https://crunchtools.github.io/packages"
    key = "crunchtools.asc"                         # the public half, in the tree
    products = ["crunchtools/petit", "crunchtools/claude-agent"]
    keep = 5                                        # releases per product
    static = ["index.html"]                         # copied to the site root
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sdlc_jobs import _debrepo
from sdlc_jobs._core import (
    CLEAN,
    Tools,
    api,
    call,
    cannot,
    fetch_tree,
    in_checkout,
    must,
    step,
    tool_sdlc,
    workspace,
)
from sdlc_jobs._tools import child_env, git_env

EVENTS = ("push", "schedule", "workflow_dispatch")
KEY_ENV = "SDLC_PAGES_GPG_KEY"
BRANCH = "gh-pages"
KEYS = {"name", "site-url", "key", "products", "keep", "static"}
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
ID = re.compile(r"[a-z][a-z0-9-]{0,39}")
FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~-]*\.(rpm|deb)")
SHA = re.compile(r"[0-9a-f]{40}")
MAX_PRODUCTS = 20
MAX_KEEP = 20
FPR_FIELD = 9  # index of the fingerprint in gpg --with-colons: field 10, counting from 1
MAX_FILE = 512 * 1024**2  # a package bigger than this is not one this site carries
OK = 200
BUILD_WAIT = 600  # seconds for the Pages build to report the pushed commit
BUILD_POLL = 10
BOT = ("github-actions[bot]", "41898282+github-actions[bot]@users.noreply.github.com")


@dataclass(frozen=True)
class Site:
    """`[tool.sdlc.pages]`, checked."""

    name: str
    url: str
    key: str
    products: list[str]
    keep: int
    static: list[str]


@dataclass(frozen=True)
class Asset:
    """One package of the pool: where it comes from and the bytes it must be."""

    product: str
    tag: str
    file: str
    url: str
    sha256: str  # "" when GitHub stored no digest (assets uploaded before it did)


def settings(root: Path) -> Site:
    raw = tool_sdlc(root).get("pages")
    if not isinstance(raw, dict):
        cannot("the tree has no [tool.sdlc.pages]; the plan is not an authority")
    stray = sorted(set(raw) - KEYS)
    if stray:
        cannot(f"[tool.sdlc.pages] has {', '.join(stray)}, which is no setting of the site")
    name = raw.get("name")
    if not isinstance(name, str) or not ID.fullmatch(name):
        cannot("[tool.sdlc.pages] name must be a lowercase id")
    url = raw.get("site-url")
    if not isinstance(url, str) or not re.fullmatch(r"https://[A-Za-z0-9.-]+(/[\w.-]+)*", url):
        cannot("[tool.sdlc.pages] site-url must be an https URL with no trailing slash")
    key = in_checkout(root, raw.get("key"), "[tool.sdlc.pages] key", directory=False)
    products = raw.get("products")
    if (
        not isinstance(products, list)
        or not 1 <= len(products) <= MAX_PRODUCTS
        or not all(isinstance(p, str) and REPO.fullmatch(p) for p in products)
        or len(set(products)) != len(products)
    ):
        cannot(f"[tool.sdlc.pages] products must be 1 to {MAX_PRODUCTS} distinct owner/repo")
    keep = raw.get("keep", 5)
    if not isinstance(keep, int) or isinstance(keep, bool) or not 1 <= keep <= MAX_KEEP:
        cannot(f"[tool.sdlc.pages] keep must be a whole number from 1 to {MAX_KEEP}")
    static = raw.get("static", [])
    if not isinstance(static, list):
        cannot("[tool.sdlc.pages] static must be a list of files")
    files = [in_checkout(root, s, "[tool.sdlc.pages] static", directory=False) for s in static]
    if any("/" in f or f in ("index.json", ".nojekyll", f"{name}.asc") for f in files):
        cannot("[tool.sdlc.pages] static files sit at the repo root and name no generated file")
    return Site(name, url, key, list(products), keep, files)


def pool(tools: Tools, env: Mapping[str, str], site: Site) -> list[Asset]:
    """The .rpm and .deb assets of each product's newest `keep` releases, by name.

    Source RPMs are left out: this is a binary repository. A name two products
    both ship is a 2, since one site path cannot hold both.
    """
    found: dict[str, Asset] = {}
    for product in site.products:
        rels = api(tools, env, "GET", f"/repos/{product}/releases?per_page=30")
        if not isinstance(rels, list):
            cannot(f"{product}'s releases are not a list")
        shown = [r for r in rels if isinstance(r, dict)]
        taken = [r for r in shown if not r.get("draft") and not r.get("prerelease")][: site.keep]
        for rel in taken:
            tag = str(rel.get("tag_name", ""))
            for a in rel.get("assets") or []:
                name = str(a.get("name", ""))
                if not FILE.fullmatch(name) or name.endswith(".src.rpm"):
                    continue
                url = str(a.get("browser_download_url", ""))
                if not url.startswith(f"https://github.com/{product}/releases/download/"):
                    cannot(f"{product} {tag} {name}: download URL {url!r} is not the release's")
                digest = str(a.get("digest") or "")
                hexed = digest.removeprefix("sha256:") if digest.startswith("sha256:") else ""
                if name in found and found[name].product != product:
                    cannot(f"{name} is shipped by both {found[name].product} and {product}")
                found.setdefault(name, Asset(product, tag, name, url, hexed))
    if not found:
        cannot("no product release carries an .rpm or .deb: refusing to publish an empty site")
    return [found[n] for n in sorted(found)]


def wanted(assets: list[Asset]) -> list[dict[str, str]]:
    """What index.json says about the pool: each file and its sha256."""
    return [{"file": a.file, "sha256": a.sha256} for a in assets]


def published(tools: Tools, site: Site) -> object:
    """The live index.json, or None when there is none to read."""
    resp = tools.http("GET", f"{site.url}/index.json", {"User-Agent": "sdlc-event"})
    if resp.status != OK:
        return None
    try:
        return json.loads(resp.body)
    except ValueError:
        return None


def changed(tools: Tools, env: Mapping[str, str], root: Path) -> bool:
    """`check`'s poll: does the site differ from what the products now release?

    An asset with no stored digest cannot be compared, so it counts as changed.
    """
    site = settings(root)
    want = wanted(pool(tools, env, site))
    if any(not w["sha256"] for w in want):
        return True
    return published(tools, site) != want


def fetch(tools: Tools, a: Asset, dest: Path) -> Path:
    """One asset by its public URL, no token, checked against its stored digest."""
    resp = tools.http("GET", a.url, {"User-Agent": "sdlc-event", "Accept": "*/*"})
    if resp.status != OK:
        cannot(f"downloading {a.product} {a.tag} {a.file} answered {resp.status}")
    if len(resp.body) > MAX_FILE:
        cannot(f"{a.file} is over {MAX_FILE} bytes")
    got = hashlib.sha256(resp.body).hexdigest()
    if a.sha256 and got != a.sha256:
        cannot(f"{a.product} {a.tag} {a.file} is {got}, GitHub stored {a.sha256}")
    if not a.sha256:
        print(f"::warning::{a.product} {a.tag} {a.file} has no stored digest; taken as served")
    path = dest / a.file
    path.write_bytes(resp.body)
    return path


class Signer:
    """gpg and rpmsign with the one key, in a GNUPGHOME of their own.

    `env` is what every tool that touches a package runs with: the allowlisted
    base and GNUPGHOME, never GITHUB_TOKEN, since they parse downloaded bytes.
    """

    def __init__(self, tools: Tools, env: Mapping[str, str], home: Path, public: Path) -> None:
        self.tools = tools
        self.home = home
        self.env = child_env(env, GNUPGHOME=str(home))
        self.public = public
        self.fpr = ""

    def gpg(self, *args: str, stdin: str | None = None) -> str:
        argv = ["gpg", "--batch", "--yes", "--no-tty", "--pinentry-mode", "loopback", *args]
        return must(self.tools, argv, env=self.env, input=stdin)

    def load(self, secret: str) -> None:
        """Import the key, and refuse it unless it is the private half of the tree's key."""
        self.home.mkdir(mode=0o700, parents=True)
        self.gpg("--import", stdin=secret)
        mine = fingerprints(self.gpg("--with-colons", "--list-secret-keys"), "sec")
        theirs = fingerprints(
            self.gpg(
                "--with-colons", "--import-options", "show-only", "--import", str(self.public)
            ),
            "pub",
        )
        if len(mine) != 1 or mine != theirs:
            cannot(f"the signing key {mine} is not the one {self.public.name} publishes {theirs}")
        self.fpr = mine[0]

    def detach(self, path: Path, out: Path) -> None:
        self.gpg("--local-user", self.fpr, "--armor", "--detach-sign", "-o", str(out), str(path))

    def clearsign(self, path: Path, out: Path) -> None:
        self.gpg("--local-user", self.fpr, "--clearsign", "-o", str(out), str(path))

    def rpm(self, path: Path) -> None:
        must(
            self.tools,
            [
                "rpmsign",
                "--addsign",
                "--define",
                f"_gpg_name {self.fpr}",
                "--define",
                f"_gpg_path {self.home}",
                str(path),
            ],
            env=self.env,
        )


def fingerprints(colons: str, kind: str) -> list[str]:
    """The primary keys' fingerprints in gpg's --with-colons listing."""
    out, primary = [], False
    for line in colons.splitlines():
        field = line.split(":")
        if field[0] in ("pub", "sec", "sub", "ssb"):
            primary = field[0] == kind
        elif field[0] == "fpr" and primary and len(field) > FPR_FIELD:
            out.append(field[9])
            primary = False
    return out


def kept(tools: Tools, env: Mapping[str, str], rpmdb: Path, old: Path, new: Path) -> bool:
    """Is the site's signed copy of `new` the same package, with a good signature?"""
    if not old.is_file():
        return False
    check = tools.run(["rpmkeys", "--dbpath", str(rpmdb), "--checksig", str(old)], env=env)
    if check.returncode != 0 or "signatures OK" not in check.stdout:
        return False
    q = ["rpm", "--dbpath", str(rpmdb), "-qp", "--nosignature", "--qf", "%{SHA256HEADER}"]
    return must(tools, [*q, str(old)], env=env) == must(tools, [*q, str(new)], env=env)


def build_site(
    root: Path, site: Site, signer: Signer, work: Path, api_env: Mapping[str, str]
) -> Path:
    """The signed tree, from the pool and the site's current branch, in `work`/site.

    `api_env` carries the token for the release listing; the package tools get `signer.env`.
    """
    tools, env = signer.tools, signer.env
    out, poold, live = work / "site", work / "pool", work / "live"
    for d in (out / "rpm", out / "deb", poold):
        d.mkdir(parents=True)
    assets = pool(tools, api_env, site)
    files = [fetch(tools, a, poold) for a in assets]
    index = [{"file": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]

    rpmdb = work / "rpmdb"
    rpmdb.mkdir()
    must(tools, ["rpmkeys", "--dbpath", str(rpmdb), "--import", str(root / site.key)], env=env)
    rpms = [p for p in files if p.suffix == ".rpm"]
    for p in rpms:
        dest = out / "rpm" / p.name
        if kept(tools, env, rpmdb, live / "rpm" / p.name, p):
            shutil.copy2(live / "rpm" / p.name, dest)
            continue
        shutil.copy2(p, dest)
        signer.rpm(dest)
    if rpms:
        must(tools, ["createrepo_c", "--quiet", str(out / "rpm")], env=env)
        repomd = out / "rpm" / "repodata" / "repomd.xml"
        signer.detach(repomd, repomd.with_name("repomd.xml.asc"))
        (out / "rpm" / f"{site.name}.repo").write_text(
            f"[{site.name}]\nname={site.name}\nbaseurl={site.url}/rpm\nenabled=1\n"
            f"gpgcheck=1\nrepo_gpgcheck=1\ngpgkey={site.url}/{site.name}.asc\n"
        )
    debs = [p for p in files if p.suffix == ".deb"]
    if debs:
        for p in debs:
            shutil.copy2(p, out / "deb" / p.name)
        _debrepo.write_packages(out / "deb")
        release = _debrepo.write_release(
            out / "deb",
            {"Origin": site.name, "Label": site.name, "Suite": "stable", "Codename": "stable"},
            datetime.now(UTC),
        )
        signer.clearsign(release, release.with_name("InRelease"))
        signer.detach(release, release.with_name("Release.gpg"))
        (out / "deb" / f"{site.name}.sources").write_text(
            f"Types: deb\nURIs: {site.url}/deb/\nSuites: ./\n"
            f"Signed-By: /etc/apt/keyrings/{site.name}.asc\n"
        )
    shutil.copy2(root / site.key, out / f"{site.name}.asc")
    for s in site.static:
        shutil.copy2(root / s, out / s)
    (out / ".nojekyll").write_text("")
    (out / "index.json").write_text(json.dumps(index, indent=1) + "\n")
    return out


def live_branch(tools: Tools, env: Mapping[str, str], repo: str, into: Path) -> str:
    """The current gh-pages head checked out in `into`, or "" when there is none yet."""
    into.mkdir()
    genv = {**env, **git_env(into, env.get("GITHUB_TOKEN", ""), env)}
    server = env.get("GITHUB_SERVER_URL") or "https://github.com"
    must(tools, ["git", "init", "-q", "."], cwd=str(into), env=genv)
    must(tools, ["git", "remote", "add", "origin", f"{server}/{repo}"], cwd=str(into), env=genv)
    got = tools.run(
        ["git", "fetch", "-q", "--depth=1", "origin", f"refs/heads/{BRANCH}"],
        cwd=str(into),
        env=genv,
    )
    if got.returncode != 0:
        print(f"pages: no {BRANCH} branch yet; every package is signed afresh", flush=True)
        return ""
    must(tools, ["git", "checkout", "-q", "FETCH_HEAD"], cwd=str(into), env=genv)
    return must(tools, ["git", "rev-parse", "HEAD"], cwd=str(into), env=genv).strip()


def push(tools: Tools, env: Mapping[str, str], repo: str, out: Path, old: str) -> str:
    """`out` as one commit on gh-pages, leased on `old`; the new commit's sha."""
    genv = {**env, **git_env(out, env.get("GITHUB_TOKEN", ""), env)} | {
        "GIT_AUTHOR_NAME": BOT[0],
        "GIT_AUTHOR_EMAIL": BOT[1],
        "GIT_COMMITTER_NAME": BOT[0],
        "GIT_COMMITTER_EMAIL": BOT[1],
    }
    server = env.get("GITHUB_SERVER_URL") or "https://github.com"
    run_id = env.get("GITHUB_RUN_ID", "local")
    for argv in (
        ["git", "init", "-q", "-b", BRANCH, "."],
        ["git", "remote", "add", "origin", f"{server}/{repo}"],
        ["git", "add", "-A", "."],
        ["git", "commit", "-q", "-m", f"Publish the package site (run {run_id})"],
    ):
        must(tools, argv, cwd=str(out), env=genv)
    sha = must(tools, ["git", "rev-parse", "HEAD"], cwd=str(out), env=genv).strip()
    lease = f"--force-with-lease=refs/heads/{BRANCH}:{old}"
    must(
        tools,
        ["git", "push", "-q", lease, "origin", f"HEAD:refs/heads/{BRANCH}"],
        cwd=str(out),
        env=genv,
    )
    return sha


def deploy(
    tools: Tools,
    env: Mapping[str, str],
    repo: str,
    sha: str,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Ask Pages to build and wait until its latest build is `sha`, built."""
    api(tools, env, "POST", f"/repos/{repo}/pages/builds")
    waited = 0
    while waited <= BUILD_WAIT:
        resp = call(tools, env, "GET", f"/repos/{repo}/pages/builds/latest")
        doc = resp.json() if resp.status == OK else {}
        if isinstance(doc, dict) and doc.get("commit") == sha:
            if doc.get("status") == "built":
                print(f"pages: built {sha}", flush=True)
                return
            if doc.get("status") == "errored":
                cannot(f"the Pages build of {sha} errored: {doc.get('error')}")
        sleep(BUILD_POLL)
        waited += BUILD_POLL
    cannot(f"the Pages build of {sha} did not finish within {BUILD_WAIT}s")


def run(event_name: str, event: dict, env: Mapping[str, str], tools: Tools) -> int:
    """Build, sign and publish the package site; 0 published or already current.

    `env` carries SDLC_PAGES_GPG_KEY (the private key, armoured), GITHUB_REPOSITORY,
    GITHUB_SHA and a GITHUB_TOKEN with contents and pages write. Raises
    CannotRunError, exit 2, on a key that is not the tree's, a download whose
    bytes are not the ones GitHub stored, a push that lost its lease, or a Pages
    build that did not finish.
    """
    if event_name not in EVENTS:
        cannot(f"pages runs on {', '.join(EVENTS)}, not {event_name!r}")
    default = (event.get("repository") or {}).get("default_branch") or ""
    if event_name != "schedule" and env.get("GITHUB_REF") != f"refs/heads/{default}":
        cannot("pages publishes from the default branch only")
    secret = env.get(KEY_ENV, "")
    os.environ.pop(KEY_ENV, None)  # shared helpers run tools with the inherited environment
    env = {k: v for k, v in env.items() if k != KEY_ENV}
    if not secret:
        cannot(f"{KEY_ENV} is not set: the pages-signing environment holds it")
    repo = env.get("GITHUB_REPOSITORY", "")
    root = workspace(env)
    fetch_tree(tools, env, root, repo)
    site = settings(root)

    work = Path(tempfile.mkdtemp(prefix="sdlc-pages-", dir=env.get("RUNNER_TEMP") or None))
    try:
        signer = Signer(tools, env, work / "gnupg", root / site.key)
        step("pages: load the signing key")
        signer.load(secret)
        del secret
        old = live_branch(tools, env, repo, work / "live")
        step("pages: fetch, check and sign the packages")
        out = build_site(root, site, signer, work, env)
        shutil.rmtree(signer.home)  # the key is gone before anything is pushed
        if event_name == "schedule" and old and same_tree(work / "live", out):
            print("pages: the site already serves these packages; nothing to publish", flush=True)
            return CLEAN
        step(f"pages: push {BRANCH}")
        sha = push(tools, env, repo, out, old)
        step("pages: request the Pages build")
        deploy(tools, env, repo, sha)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return CLEAN


def same_tree(live: Path, out: Path) -> bool:
    """Does the live site serve the same packages, signatures aside?

    Signatures carry their time, so InRelease, Release.gpg, repomd.xml.asc and
    Release's Date differ on every run; index.json names the packages' bytes and
    decides. Only a scheduled run asks: a push or a dispatch always publishes, so
    a changed index page or key reaches the site.
    """
    try:
        return (live / "index.json").read_text() == (out / "index.json").read_text() and sorted(
            p.name for p in (live / "rpm").glob("*.rpm")
        ) == sorted(p.name for p in (out / "rpm").glob("*.rpm"))
    except OSError:
        return False
