#!/usr/bin/env python3
"""Move the baked tool pins to their latest releases.

Gourmand, Gatehouse, markdownlint-cli2 and lychee are pinned exactly in the
Containerfile, so rebuilding when one of them releases changes nothing: the
build pulls the same version it pulled before. What a release has to do is
MOVE THE PIN. This script does that, and when anything moved it also bumps
this image's own PATCH version, so the change ships as a sdlc release, which
every converted repo receives through sdlc:latest.

lychee is pinned twice over, by version and by image digest. The digest is
looked up on Docker Hub for the new version and written with it; a version
with no digest raises, so a bump with a stale digest is never written.

Run by .github/workflows/update-tools.yml. Writes `changed=true|false` and a
one-line `summary` to $GITHUB_OUTPUT when that is set.

Usage: scripts/update_tools.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
LYCHEE_FROM = re.compile(
    r"^(FROM docker\.io/lycheeverse/lychee:\$\{LYCHEE_VERSION\}@)"
    r"(sha256:[0-9a-f]{64})( AS lychee)$",
    re.M,
)


def semver(text: str) -> tuple[int, int, int]:
    match = SEMVER.match(text.strip())
    if not match:
        raise ValueError(f"not a release version: {text!r}")
    major, minor, patch = (int(g) for g in match.groups())
    return major, minor, patch


# Seconds for one registry lookup. The weekly job retries tomorrow; a lookup
# that hangs must not hold the runner until Actions' own timeout.
HTTP_TIMEOUT = 30


def http_json(url: str) -> dict:
    """A registry's JSON answer, through curl like gh_api goes through gh.

    curl rather than urllib: --proto keeps it to https whatever the URL says,
    and a failed status is an error (-f) rather than a body to parse.
    """
    out = subprocess.run(
        ["curl", "-fsSL", "--proto", "=https", "--max-time", str(HTTP_TIMEOUT), url],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


def gh_api(path: str, *extra: str) -> str:
    return subprocess.run(
        ["gh", "api", path, *extra],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def latest_release(repo: str) -> str:
    """The highest release TAG, not the "latest release" object.

    crunchtools/gourmand tags its releases and creates no GitHub Release, so
    `releases/latest` answers 404 for it. Tags are what both repos have.
    """
    out = gh_api(f"repos/{repo}/tags", "--paginate", "--jq", ".[].name").split()
    versions = sorted(semver(tag) for tag in out if SEMVER.match(tag))
    if not versions:
        raise ValueError(f"{repo} has no release tags")
    return ".".join(str(n) for n in versions[-1])


def latest_npm(package: str) -> str:
    version = http_json(f"https://registry.npmjs.org/{package}")["dist-tags"]["latest"]
    semver(version)  # refuse a prerelease or anything that is not x.y.z
    return version


def latest_lychee() -> str:
    """lychee's GitHub Releases are tagged `lychee-vX.Y.Z`; its plain tags stop at v0.15."""
    tag = gh_api("repos/lycheeverse/lychee/releases/latest", "--jq", ".tag_name").strip()
    return ".".join(str(n) for n in semver(tag.removeprefix("lychee-")))


def lychee_digest(version: str) -> str:
    """The manifest-list digest Docker Hub holds for lychee:<version>."""
    tag_record = http_json(
        f"https://hub.docker.com/v2/repositories/lycheeverse/lychee/tags/{version}"
    )
    digest = tag_record.get("digest")
    if not isinstance(digest, str) or not DIGEST.match(digest):
        raise ValueError(f"lychee:{version} has no usable digest on Docker Hub: {digest!r}")
    return digest


@dataclass(frozen=True)
class Tool:
    """One pinned tool: its ARG, and how to find its latest version (and digest)."""

    arg: str
    name: str
    latest: Callable[[], str]
    digest: Callable[[str], str] | None = None
    copies: int = 2  # ARG copies: one before the first FROM, one inside the stage


TOOLS = [
    Tool("GOURMAND_VERSION", "gourmand", lambda: latest_release("crunchtools/gourmand")),
    Tool("GATEHOUSE_VERSION", "gatehouse", lambda: latest_release("crunchtools/gatehouse")),
    Tool(
        "MARKDOWNLINT_CLI2_VERSION",
        "markdownlint-cli2",
        lambda: latest_npm("markdownlint-cli2"),
        copies=1,
    ),
    Tool("LYCHEE_VERSION", "lychee", latest_lychee, lychee_digest),
]


def pinned(containerfile: str, arg: str) -> str:
    values = set(re.findall(rf"^ARG {arg}=(\S+)$", containerfile, re.M))
    if len(values) != 1:
        raise ValueError(f"{arg} is pinned {len(values)} different ways: {sorted(values)}")
    return values.pop()


def bump_patch(version: str) -> str:
    major, minor, patch = semver(version)
    return f"{major}.{minor}.{patch + 1}"


def rewrite(path: Path, old: str, new: str, count: int | None = None) -> None:
    text = path.read_text(encoding="utf-8")
    found = text.count(old)
    if found == 0 or (count is not None and found != count):
        raise ValueError(f"{path.name}: expected {count or 'some'} of {old!r}, found {found}")
    path.write_text(text.replace(old, new), encoding="utf-8")


def apply_digest(containerfile: Path, digest: str) -> None:
    if not DIGEST.match(digest):
        raise ValueError(f"refusing to write a malformed digest: {digest!r}")
    text = containerfile.read_text(encoding="utf-8")
    found = len(LYCHEE_FROM.findall(text))
    if found != 1:
        raise ValueError(f"Containerfile: expected one pinned lychee FROM line, found {found}")
    containerfile.write_text(LYCHEE_FROM.sub(rf"\g<1>{digest}\g<3>", text), encoding="utf-8")


def changelog_entry(version: str, moves: list[tuple[str, str, str]]) -> str:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    lines = [f"## [{version}] - {today}", "", "### Changed", ""]
    lines += [f"- {name} {old} -> {new}." for name, old, new in moves]
    return "\n".join(lines) + "\n\n"


def plan_moves(text: str) -> list[tuple[Tool, str, str, str | None]]:
    """(tool, current, latest, new digest) for every tool whose pin is behind.

    The digest is looked up here, before anything is written, and a failed
    lookup raises: a version bump with a stale digest is never produced.
    """
    moves = []
    for tool in TOOLS:
        current = pinned(text, tool.arg)
        latest = tool.latest()
        if semver(latest) > semver(current):
            digest = tool.digest(latest) if tool.digest else None
            moves.append((tool, current, latest, digest))
    return moves


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    containerfile = ROOT / "Containerfile"
    text = containerfile.read_text(encoding="utf-8")
    moves = plan_moves(text)

    output = os.environ.get("GITHUB_OUTPUT")
    if not moves:
        print("every baked tool is at its latest release")
        if output:
            with Path(output).open("a", encoding="utf-8") as fh:
                fh.write("changed=false\n")
        return 0

    image_tag = ROOT / "IMAGE_TAG"
    current = image_tag.read_text(encoding="utf-8").strip()
    version = bump_patch(current)
    summary = ", ".join(f"{t.name} {old} -> {new}" for t, old, new, _ in moves)
    print(f"sdlc {current} -> {version}: {summary}")
    if args.dry_run:
        return 0

    for tool, old, new, digest in moves:
        # Both copies where there are two: one before the first FROM, one after
        # it (an ARG declared before FROM is out of scope inside the stage).
        rewrite(containerfile, f"ARG {tool.arg}={old}\n", f"ARG {tool.arg}={new}\n", tool.copies)
        if digest:
            apply_digest(containerfile, digest)
    rewrite(containerfile, f'LABEL version="{current}"', f'LABEL version="{version}"', count=1)
    rewrite(ROOT / "pyproject.toml", f'version = "{current}"', f'version = "{version}"', count=1)
    image_tag.write_text(version + "\n", encoding="utf-8")
    entry = changelog_entry(version, [(t.name, o, n) for t, o, n, _ in moves])
    rewrite(ROOT / "CHANGELOG.md", "## [Unreleased]\n\n", "## [Unreleased]\n\n" + entry, count=1)

    if output:
        with Path(output).open("a", encoding="utf-8") as fh:
            fh.write(f"changed=true\nversion={version}\nsummary={summary}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
