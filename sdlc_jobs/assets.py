"""`assets`: build a merged release's packages and attach them to its GitHub Release.

Runs on the default-branch push that merges an Ashigaru release PR, after `write`
tagged it and published the Release. It holds the job's GITHUB_TOKEN with
`contents: write` (no stored secret, no App key) and the runner's socket.
The plan (`SDLC_CHECK`) only decides that this job runs: it refuses unless `write`
succeeded, the tree declares `[[tool.sdlc.assets]]`, and the API agrees with
`write` about the release.

What it builds is what `sdlc ci` already built on the pull request: each entry
names an `image-build` check of `[tool.sdlc.ci]`, and the same Containerfile,
context and build-args are built again here, to the entry's `target` stage. The
build runs through the socket, so a `RUN` step gets no token, no socket and no
key. Every registry FROM in that Containerfile must be pinned by `@sha256:` in
the reviewed tree: these build roots (Fedora, CentOS, Debian) are not images the
fleet signs or `image` verifies, so the reviewed digest is what is trusted, and
a tag that could move under a release is refused.

The files are copied out of the built stage as data: regular files directly in
`path`, matching `files`, each carrying the release's version in its name. They
are attached with a SHA256SUMS of them all, last. An asset already on the
Release with the same bytes (GitHub's `digest`) is kept, so a re-run uploads
only what is missing; one with other bytes is replaced, with a warning, since
an RPM rebuilt on a re-run differs by its build time.

    [[tool.sdlc.assets]]
    check = "rpm-fc43"      # an image-build check in [tool.sdlc.ci]
    target = "build"        # the stage that holds the files
    path = "/out"           # the directory in that stage
    files = ["*.rpm"]       # globs on the file name; each must match at least one
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shutil
import tempfile
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sdlc_jobs._core import (
    CLEAN,
    DIGEST,
    Tools,
    api,
    cannot,
    fetch_tree,
    in_checkout,
    must,
    needed,
    podman,
    show,
    step,
    tool_sdlc,
    workspace,
)
from sdlc_jobs._tools import child_env, safe_directory
from sdlc_jobs._verify_release import verify_release
from sdlc_jobs.image import builder_stages, copy_sources
from sdlc_jobs.pypi import parse_release

EVENTS = ("push",)  # ci.yml's `assets` job runs on a push and nothing else
KEYS = {"check", "target", "path", "files"}
MAX_ENTRIES = 20
MAX_GLOBS = 20
CHUNK = 1 << 20  # bytes per read when hashing: a MiB keeps memory flat on a large asset
MAX_ASSET = 2 * 1024**3  # GitHub refuses a release asset of 2 GiB or more
MAX_ASSETS = 100  # one page of the Release's assets: more is a 2, never a guess
STAGE = re.compile(r"[a-z0-9][a-z0-9._-]*")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~-]*")  # what a build may name a file
RENAMED = re.compile(r"[^A-Za-z0-9._-]")  # what GitHub stores as "." (ashigaru+desk-*.rpm)
SUMS = "SHA256SUMS"
UPLOADS = "uploads.github.com"  # the only host an upload, and so the token, goes to
CREATED = 201
OCTETS = "application/octet-stream"


@dataclass(frozen=True)
class Entry:
    """One `[[tool.sdlc.assets]]` entry, checked against its image-build check."""

    check: str
    containerfile: str
    context: str
    build_args: dict[str, str]
    target: str
    path: str
    files: list[str]


def entries(root: Path, version: str, sha: str) -> list[Entry]:
    """`[[tool.sdlc.assets]]`, each checked; none is a 2: the plan is not an authority."""
    cfg = tool_sdlc(root)
    raw = cfg.get("assets")
    if not isinstance(raw, list) or not raw or not all(isinstance(e, dict) for e in raw):
        cannot("the tree has no [[tool.sdlc.assets]] entries; the plan is not an authority")
    if len(raw) > MAX_ENTRIES:
        cannot(f"[[tool.sdlc.assets]] has {len(raw)} entries, more than {MAX_ENTRIES}")
    ci = cfg.get("ci")
    checks: Mapping = ci if isinstance(ci, dict) else {}
    return [entry(root, checks, e, version, sha) for e in raw]


def entry(root: Path, checks: Mapping, raw: dict, version: str, sha: str) -> Entry:
    """One `[[tool.sdlc.assets]]` table, checked, as the build it names.

    containerfile, context and build_args come from the image-build check it names
    in `checks`, the build-args resolved for this release; anything malformed is a 2.
    """
    where = f"[[tool.sdlc.assets]] {raw.get('check')!r}"
    stray = sorted(set(raw) - KEYS)
    if stray:
        cannot(f"{where} has {', '.join(stray)}, which is no setting of an asset build")
    name = raw.get("check")
    table = checks.get(name) if isinstance(name, str) else None
    if (
        not isinstance(name, str)
        or not isinstance(table, dict)
        or table.get("kind") != "image-build"
    ):
        cannot(f"{where}: check must name an image-build check in [tool.sdlc.ci]")
    cf = in_checkout(
        root, table.get("containerfile", "Containerfile"), f"{where} containerfile", directory=False
    )
    ctx = in_checkout(root, table.get("context", "."), f"{where} context", directory=True)
    args = table.get("build_args", {})
    if not isinstance(args, dict):
        cannot(f"{where}: build_args of {name} must be a table")
    target = raw.get("target")
    if not isinstance(target, str) or not STAGE.fullmatch(target):
        cannot(f"{where}: target must be a stage name")
    path = raw.get("path")
    if not isinstance(path, str) or not path.startswith("/") or ".." in path.split("/"):
        cannot(f"{where}: path must be an absolute directory in the stage, without ..")
    files = raw.get("files")
    if (
        not isinstance(files, list)
        or not 1 <= len(files) <= MAX_GLOBS
        or not all(isinstance(g, str) and g and "/" not in g for g in files)
    ):
        cannot(f"{where}: files must be 1 to {MAX_GLOBS} globs on a file name")
    resolved = {str(k): resolve(where, v, version, sha) for k, v in args.items()}
    return Entry(name, cf, ctx, resolved, target, path, list(files))


def resolve(where: str, value: object, version: str, sha: str) -> str:
    """A build-arg as `sdlc ci` resolves it, from the release instead of the tree."""
    text = str(value)
    if not text.startswith("@"):
        return text
    known = {"@project.version": version, "@git.sha": sha}
    if text not in known:
        cannot(f"{where}: build-arg {text} has no meaning for a release build")
    return known[text]


def pinned(root: Path, e: Entry) -> None:
    """Every registry FROM must carry a reviewed digest; the target must be a stage."""
    text = (root / e.containerfile).read_text(encoding="utf-8", errors="replace")
    if not re.search(rf"(?im)^\s*FROM\s.+\sAS\s+{re.escape(e.target)}\s*$", text):
        cannot(f"{e.containerfile} has no stage named {e.target}")
    for parent in builder_stages(text, -1, e.build_args):
        found = parent.image.partition("@")[2]
        if not DIGEST.fullmatch(found):
            cannot(
                f"{e.containerfile}: FROM {parent.image} is not pinned by @sha256:; a "
                "release asset is built only on a build root the review saw"
            )
    for ref in copy_sources(text, e.build_args):
        if not DIGEST.fullmatch(ref.partition("@")[2]):
            cannot(
                f"{e.containerfile}: {ref}, named by a --from= or a RUN --mount from=, is "
                "not pinned by @sha256:; a release asset takes bytes only from images the "
                "review saw"
            )


def build(tools: Tools, job_env: Mapping[str, str], root: Path, e: Entry, out: Path) -> None:
    """The entry's stage, built through the socket; its `path` copied into `out` as data.

    podman gets the allowlisted base only: the socket is in its argv, and no
    GITHUB_TOKEN, volume or secret reaches the build.
    """
    env = child_env(job_env)
    tag = f"localhost/sdlc-assets:{e.check}"
    argv = [*podman(env), "build", "--pull=missing", "-f", str(root / e.containerfile)]
    for key, value in e.build_args.items():
        argv += ["--build-arg", f"{key}={value}"]
    argv += ["--target", e.target, "-t", tag, str(root / e.context)]
    proc = tools.run(argv, env=env, stream=True)
    show(proc)
    if proc.returncode != 0:
        cannot(f"{e.check}: podman build exited {proc.returncode}")
    # A stage on scratch has no command: `create` needs one and never runs it.
    cid = must(tools, [*podman(env), "create", tag, "/sdlc-assets-never-run"], env=env).strip()
    try:
        out.mkdir(parents=True)
        must(tools, [*podman(env), "cp", f"{cid}:{e.path.rstrip('/')}/.", str(out)], env=env)
    finally:
        tools.run([*podman(env), "rm", "-f", cid], env=env)


def collect(e: Entry, out: Path, version: str) -> dict[str, Path]:
    """The files the entry asks for: regular, top-level, named for this version.

    Keyed by the name GitHub serves: it stores `+` and `~` as `.`, so the file is
    attached, matched on a re-run, and listed in SHA256SUMS under that name.
    """
    found: dict[str, Path] = {}
    for p in sorted(out.iterdir()):
        if p.is_symlink() or not p.is_file():
            continue
        if any(fnmatch.fnmatchcase(p.name, g) for g in e.files):
            found[p.name] = p
    for g in e.files:
        if not any(fnmatch.fnmatchcase(n, g) for n in found):
            cannot(f"{e.check}: nothing in {e.target}:{e.path} matches {g!r}")
    for name, p in found.items():
        if not NAME.fullmatch(name) or name == SUMS:
            cannot(f"{e.check}: {name!r} is not a name to attach as it is")
        if version not in name:
            cannot(f"{e.check}: {name} does not carry the release version {version}")
        if p.stat().st_size >= MAX_ASSET:
            cannot(f"{e.check}: {name} is 2 GiB or more, which GitHub refuses")
    return as_served(e, found)


def as_served(e: Entry, found: Mapping[str, Path]) -> dict[str, Path]:
    """`found` keyed by the names GitHub will serve; two files served alike are a 2."""
    served: dict[str, Path] = {}
    for name, p in found.items():
        key = RENAMED.sub(".", name)
        if key in served:
            cannot(f"{e.check}: {name} and {served[key].name} are both {key} on GitHub")
        served[key] = p
    return served


def sha256(path: Path) -> str:
    """The SHA-256 hex digest of the file at `path`, read a chunk at a time."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def merge(sets: list[dict[str, Path]]) -> dict[str, Path]:
    """One set of names; a name two builds made is kept once if the bytes agree."""
    out: dict[str, Path] = {}
    for found in sets:
        for name, p in found.items():
            if name in out and sha256(out[name]) != sha256(p):
                cannot(f"two asset builds made {name} with different bytes")
            out.setdefault(name, p)
    return out


def sums(files: Mapping[str, Path], where: Path) -> Path:
    """SHA256SUMS of every asset, in `sha256sum` format, sorted by name."""
    path = where / SUMS
    path.write_text("".join(f"{sha256(files[n])}  {n}\n" for n in sorted(files)))
    return path


def release_of(tools: Tools, env: Mapping[str, str], repo: str, version: str, sha: str) -> dict:
    """The published Release of v`version`, whose tag names `sha`; anything else is a 2."""
    tag = f"v{version}"
    commit = api(tools, env, "GET", f"/repos/{repo}/commits/{tag}")
    if not isinstance(commit, dict) or commit.get("sha") != sha:
        cannot(f"{tag} does not name {sha}: write's tag is not this release")
    rel = api(tools, env, "GET", f"/repos/{repo}/releases/tags/{tag}")
    if not isinstance(rel, dict) or rel.get("draft") or rel.get("tag_name") != tag:
        cannot(f"the Release of {tag} is not published")
    return rel


def upload_base(rel: Mapping, repo: str) -> str:
    """`upload_url` without its {?name,label} template, checked to be this Release's.

    The token goes with every upload, so the host must be GitHub's upload host too.
    """
    url = str(rel.get("upload_url", "")).split("{", 1)[0]
    if url != f"https://{UPLOADS}/repos/{repo}/releases/{rel.get('id')}/assets":
        cannot(f"the Release's upload_url {url!r} is not this Release's")
    return url


def attach(
    tools: Tools, env: Mapping[str, str], repo: str, rel: Mapping, files: Mapping[str, Path]
) -> int:
    """Upload each file not already there with these bytes; the number uploaded."""
    have = api(tools, env, "GET", f"/repos/{repo}/releases/{rel['id']}/assets?per_page=100")
    if not isinstance(have, list) or len(have) >= MAX_ASSETS:
        cannot(f"the Release has {MAX_ASSETS} assets or more; this job reads one page")
    by_name = {a.get("name"): a for a in have if isinstance(a, dict)}
    base = upload_base(rel, repo)
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "sdlc-event",
        "Authorization": f"Bearer {env.get('GITHUB_TOKEN', '')}",
        "Content-Type": OCTETS,
    }
    sent = 0
    for name in [*sorted(n for n in files if n != SUMS), *([SUMS] if SUMS in files else [])]:
        digest = f"sha256:{sha256(files[name])}"
        old = by_name.get(name)
        if old is not None and old.get("digest") == digest:
            print(f"assets: {name} is already attached with these bytes", flush=True)
            continue
        if old is not None:
            print(f"::warning::assets: {name} is attached with other bytes; replacing it")
            api(tools, env, "DELETE", f"/repos/{repo}/releases/assets/{old['id']}")
        url = f"{base}?name={urllib.parse.quote(name)}"
        resp = tools.http("POST", url, headers, files[name].read_bytes())
        if resp.status != CREATED:
            cannot(f"uploading {name} answered {resp.status}: {resp.body[:300]!r}")
        got = resp.json()
        said = got.get("digest") if isinstance(got, dict) else None
        if said and said != digest:
            cannot(f"GitHub stored {name} as {said}, not {digest}")
        stored = got.get("name") if isinstance(got, dict) else None
        if stored and stored != name:
            cannot(f"GitHub stored {name} as {stored}: SHA256SUMS would name a file it lacks")
        print(f"assets: attached {name}", flush=True)
        sent += 1
    return sent


def run(event_name: str, event: dict, env: Mapping[str, str], tools: Tools) -> int:
    """Re-verify the release `write` made, build its assets and attach them; 0 attached.

    `event_name` must be one of `EVENTS`. `env` carries SDLC_WRITE and SDLC_RELEASE
    ({version, sha} from `write`), GITHUB_REPOSITORY, GITHUB_SHA and a GITHUB_TOKEN
    with `contents: write`. Raises CannotRunError, exit 2, when the release does
    not verify, a build fails, or an upload is refused.
    """
    del event  # the job contract passes it; a push needs nothing from it
    if event_name not in EVENTS:
        cannot(f"assets runs on {', '.join(EVENTS)}, not {event_name!r}")
    needed("write", env.get("SDLC_WRITE"))
    release = parse_release(env.get("SDLC_RELEASE"))
    sha = env.get("GITHUB_SHA", "")
    repo = env.get("GITHUB_REPOSITORY", "")
    if not sha or release["sha"] != sha:
        cannot(f"write released {release['sha']}, this push is {sha or 'unknown'}")
    root = workspace(env)
    fetch_tree(tools, env, root, repo)
    genv = {**env, **safe_directory(env, root)}  # the mounted workspace: #157
    head = must(tools, ["git", "rev-parse", "HEAD"], cwd=str(root), env=genv).strip()
    if head != sha:
        cannot(f"the checkout is at {head}, not {sha}")

    step("assets: re-derive the release from the API")
    version = verify_release(tools, env, sha)["version"]
    if version != release["version"]:
        cannot(f"the API says {version}, write said {release['version']}")
    todo = entries(root, version, sha)
    for e in todo:
        pinned(root, e)
    rel = release_of(tools, env, repo, version, sha)

    scratch = Path(tempfile.mkdtemp(prefix="sdlc-assets-", dir=env.get("RUNNER_TEMP") or None))
    try:
        sets = []
        for i, e in enumerate(todo):
            step(f"assets: build {e.check} to {e.target}")
            out = scratch / str(i)
            build(tools, env, root, e, out)
            sets.append(collect(e, out, version))
        files = merge(sets)
        files[SUMS] = sums(files, scratch)
        step(f"assets: attach {len(files)} files to v{version}")
        sent = attach(tools, env, repo, rel, files)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print(json.dumps({"version": version, "attached": sent, "files": sorted(files)}), flush=True)
    return CLEAN
