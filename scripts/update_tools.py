#!/usr/bin/env python3
"""Move the baked Gourmand and Gatehouse pins to their latest releases.

The Containerfile pins both exactly, so rebuilding when one of them releases
changes nothing: the build pulls the same version it pulled before. What a
release has to do is MOVE THE PIN. This script does that, and when anything
moved it also bumps this image's own PATCH version, so the change ships as a
python-sdlc release that every Python repo then receives as a bump PR.

Run by .github/workflows/update-tools.yml. Writes `changed=true|false` and a
one-line `summary` to $GITHUB_OUTPUT when that is set.

Usage: scripts/update_tools.py [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = {"GOURMAND_VERSION": "crunchtools/gourmand", "GATEHOUSE_VERSION": "crunchtools/gatehouse"}
SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def semver(text: str) -> tuple[int, int, int]:
    match = SEMVER.match(text.strip())
    if not match:
        raise ValueError(f"not a release version: {text!r}")
    major, minor, patch = (int(g) for g in match.groups())
    return major, minor, patch


def latest_release(repo: str) -> str:
    """The highest release TAG, not the "latest release" object.

    crunchtools/gourmand tags its releases and creates no GitHub Release, so
    `releases/latest` answers 404 for it. Tags are what both repos have.
    """
    out = subprocess.run(
        ["gh", "api", "--paginate", f"repos/{repo}/tags", "--jq", ".[].name"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    versions = sorted(semver(tag) for tag in out if SEMVER.match(tag))
    if not versions:
        raise ValueError(f"{repo} has no release tags")
    return ".".join(str(n) for n in versions[-1])


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


def changelog_entry(version: str, moves: list[tuple[str, str, str]]) -> str:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    lines = [f"## [{version}] - {today}", "", "### Changed", ""]
    lines += [f"- {repo.split('/')[1]} {old} -> {new}." for repo, old, new in moves]
    return "\n".join(lines) + "\n\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    containerfile = ROOT / "Containerfile"
    text = containerfile.read_text(encoding="utf-8")
    moves = []
    for arg, repo in TOOLS.items():
        current = pinned(text, arg)
        latest = latest_release(repo)
        if semver(latest) > semver(current):
            moves.append((repo, current, latest, arg))

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
    summary = ", ".join(f"{repo.split('/')[1]} {old} -> {new}" for repo, old, new, _ in moves)
    print(f"python-sdlc {current} -> {version}: {summary}")
    if args.dry_run:
        return 0

    for _, old, new, arg in moves:
        # Both copies: one before the first FROM, one after it (an ARG declared
        # before FROM is out of scope inside the stage).
        rewrite(containerfile, f"ARG {arg}={old}\n", f"ARG {arg}={new}\n", count=2)
    rewrite(containerfile, f'LABEL version="{current}"', f'LABEL version="{version}"', count=1)
    rewrite(ROOT / "pyproject.toml", f'version = "{current}"', f'version = "{version}"', count=1)
    image_tag.write_text(version + "\n", encoding="utf-8")
    entry = changelog_entry(version, [(r, o, n) for r, o, n, _ in moves])
    rewrite(ROOT / "CHANGELOG.md", "## [Unreleased]\n\n", "## [Unreleased]\n\n" + entry, count=1)

    if output:
        with Path(output).open("a", encoding="utf-8") as fh:
            fh.write(f"changed=true\nversion={version}\nsummary={summary}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
