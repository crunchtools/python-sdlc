"""The constitution, v2: the checks sdlc makes for a repository that has converted.

Constitution 2.0.0 states intent; this module holds the decisions that carry it
out (G3). A repository is on v2 when it carries no per-repo constitution
(`.specify/memory/constitution.md`) and declares what it is in its sdlc config:

    [tool.sdlc]
    repo-kinds = ["mcp-server"]

Every finding names the principle it serves, so a finding, an exception and
`sdlc-constitution coverage` all speak the same IDs (G2).

Commands (installed as `sdlc-constitution`):

    check [ROOT]                 0 clean, 1 findings, 2 could not run
    context [ROOT] [--readme F]  the text Gatehouse reviews against: the
                                 constitution plus the repo's README section
                                 "What's different here"
    coverage                     how each principle is checked: rule, review, none
    gatehouse [ARGS...]          run Gatehouse in the current directory against the
                                 context above (v2), or its own discovery (v1)

Stdlib only: it runs under the image's small venv, beside the v1 validator.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sdlc_jobs import _tools as tools
from sdlc_jobs import constitution_checks as rules

CLEAN, FOUND, CANNOT_RUN = 0, 1, 2

# The constitution's text in the image (Containerfile, the constitution fetch).
# A module attribute so tests can point it at a fixture.
TEXT = tools.CONSTITUTION_TEXT

V1_MANIFEST = Path(".specify/memory/constitution.md")
SECTION_TITLE = "What's different here"


# -- what each kind requires (section K) -------------------------------------------


@dataclass(frozen=True)
class Kind:
    """The machine-checkable part of one kind. Each field cites what it serves."""

    id: str  # its principle in section K
    files: tuple[str, ...] = ()  # globs that must each match (S1 locks, C7 tests, ...)
    forbid: tuple[str, ...] = ()  # globs that must match nothing
    agpl: bool = True  # I1: an AGPL LICENSE in the root
    version_sync: bool = True  # R2: version files agree with the changelog
    containerfile_from: str = ""  # a regex some Containerfile FROM must match
    tests_contain: tuple[str, ...] = ()  # regexes some test file must hold


PY = ("pyproject.toml", "uv.lock", "test*/**/*.py")

KINDS: dict[str, Kind] = {
    "mcp-server": Kind("K1", files=(*PY, "Containerfile*"), tests_contain=("readOnlyHint",)),
    "forked-mcp-server": Kind("K2", files=("Containerfile*",), agpl=False),
    "cli-tool": Kind("K3", files=(*PY, "Containerfile*")),
    "host-tool": Kind("K4", files=PY, forbid=("Containerfile*",)),
    "web-application": Kind("K5", files=("Containerfile*",), version_sync=False),
    "container-image": Kind("K6", files=("Containerfile*",)),
    "bootc-image": Kind("K7", files=("Containerfile*",), containerfile_from="bootc"),
    "autonomous-agent": Kind("K8", files=("Containerfile*",)),
    "security-gateway": Kind("K9", files=(*PY, "Containerfile*")),
    "workflow-automation": Kind("K10", files=("test*/**/*.py",)),
    "claude-skill": Kind("K11", files=("**/SKILL.md",)),
    "host-config": Kind("K12", agpl=False),
    "package-repository": Kind("K13", files=("*.asc",)),
    "data-archive": Kind("K14", files=("README.md",), agpl=False),
    "governance": Kind("K15", files=("pyproject.toml", "test*/**/*.py")),
}

# Which principle a file rule serves, for the finding's ID.
FILE_PRINCIPLE = {"uv.lock": "S1", "test*/**/*.py": "C7"}

# R2: where a release states its version. The first group of each pattern must
# equal the newest released `## [X.Y.Z]` heading in CHANGELOG.md.
VERSION_FILES: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", r'^version\s*=\s*"([^"]+)"'),
    ("src/*/__init__.py", r"""^__version__\s*=\s*["']([^"']+)["']"""),
    ("server.json", r'"version"\s*:\s*"([^"]+)"'),
    ("Containerfile*", r'^\s*(?:LABEL\s+)?version="([^"]+)"'),
    ("src/*/server.py", r'^\s*version\s*=\s*"([^"]+)"'),
    ("package.json", r'^  "version"\s*:\s*"([^"]+)"'),
)
RELEASED = re.compile(r"^##\s*\[(\d+\.\d+\.\d+)\]", re.MULTILINE)
RELEASE_VALUE = re.compile(r"\d+\.\d+\.\d+(?:[-+][\w.]+)?")

# G2: how each principle is checked today. "rule" means this module or another
# sdlc check decides it; "review" means Gatehouse judges it against the text;
# "none" means nothing checks it yet, and that gap is meant to stay visible.
COVERAGE: dict[str, str] = {
    **dict.fromkeys(("I2", "I3", "C1", "C8"), "review"),
    "I1": "rule",
    "C2": "rule",  # the required checks; a model-based one needs its measurement (G2)
    "C3": "rule",  # sdlc gate: every finding answered
    "C4": "rule",  # the 0/1/2 exit contract
    "C5": "rule",  # one image, one launcher
    "C6": "review",
    "C7": "rule",
    "R1": "review",
    "R2": "rule",
    "R3": "rule",  # build once, promote by digest (image/promote jobs)
    "R4": "rule",
    "R5": "review",
    "S1": "rule",
    # A repo overriding the fleet's release-age cooldown is a rule
    # (constitution_checks); the pins themselves and Renovate's PRs are review.
    "S2": "review",
    "S3": "rule",  # dependency audit and image scan
    "S4": "review",
    "S5": "rule",  # only the fleet file publishes, signed
    "S6": "review",
    "P1": "review",
    "P2": "review",
    "P3": "rule",  # the secrets scan; names and hosts are review
    "P4": "review",
    "P5": "none",
    **dict.fromkeys(("O1", "O2", "O3", "O4", "O5", "O6", "O7"), "review"),
    **dict.fromkeys(("A1", "A2", "A3", "A4"), "review"),
    "D1": "review",
    "D2": "review",
    "D3": "rule",
    # Partial rules stay "review" while the rest of the principle is judged:
    # K1's size bound and read-only test, K11's credential literal
    # (constitution_checks). K12's "private" needs the GitHub API, so
    # it is review with the rest of K12.
    **dict.fromkeys((f"K{n}" for n in range(1, 16)), "review"),
    "G1": "rule",
    "G2": "rule",
    "G3": "review",
    "G4": "review",
    "G5": "review",
}


# -- reading the repo ------------------------------------------------------------


class CannotRunError(Exception):
    """The check could not be made: exit 2, never a pass."""


def sdlc_table(root: Path) -> dict:
    """`[tool.sdlc]` from pyproject.toml, else sdlc.toml; {} for neither (as sdlc reads it)."""
    for name in ("pyproject.toml", "sdlc.toml"):
        path = root / name
        if not path.is_file():
            continue
        try:
            doc = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as err:
            raise CannotRunError(f"{name} is not valid TOML: {err}") from err
        table = doc.get("tool", {}).get("sdlc", {})
        if not isinstance(table, dict):
            raise CannotRunError(f"[tool.sdlc] in {name} is not a table")
        return table
    return {}


def declared_kinds(root: Path) -> list[str]:
    value = sdlc_table(root).get("repo-kinds")
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise CannotRunError("[tool.sdlc] repo-kinds must be a list of kind names")
    return value


def is_v2(root: Path) -> bool:
    """A repo is judged by v2 once it carries no per-repo constitution."""
    return not (root / V1_MANIFEST).is_file()


def readme_section(text: str) -> str:
    """The README's "What's different here" section, heading included; "" if absent.

    It runs from its heading to the next heading of the same or a higher level.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        found = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if not found or found.group(2).lower() != SECTION_TITLE.lower():
            continue
        level = len(found.group(1))
        end = len(lines)
        for j in range(i + 1, len(lines)):
            nxt = re.match(r"^(#{1,6})\s", lines[j])
            if nxt and len(nxt.group(1)) <= level:
                end = j
                break
        return "\n".join(lines[i:end]).rstrip() + "\n"
    return ""


# -- the checks ---------------------------------------------------------------------


@dataclass
class Report:
    findings: list[str] = field(default_factory=list)

    def add(self, principle: str, message: str) -> None:
        self.findings.append(f"{principle}: {message}")


def check_kinds(root: Path, report: Report) -> list[Kind]:
    kinds = declared_kinds(root)
    if not kinds:
        report.add(
            "G1",
            "no [tool.sdlc] repo-kinds: declare what this repository is, one or more of "
            + ", ".join(KINDS),
        )
        return []
    unknown = [k for k in kinds if k not in KINDS]
    if unknown:
        report.add(
            "G1", f"unknown repo-kinds {', '.join(unknown)}; the kinds are {', '.join(KINDS)}"
        )
    return [KINDS[k] for k in kinds if k in KINDS]


def check_files(root: Path, kinds: list[Kind], report: Report) -> None:
    """Each kind's required and forbidden files, its FROM and its test needles."""
    required: dict[str, str] = {}
    for k in kinds:
        for pattern in k.files:
            required.setdefault(pattern, FILE_PRINCIPLE.get(pattern, k.id))
    for pattern, principle in required.items():
        if not any(root.glob(pattern)):
            report.add(principle, f"nothing matches `{pattern}`")
    lock = root / "uv.lock"
    own_lock = "uv.lock" in required and lock.is_file() and not lock.is_symlink()
    if own_lock and not rules.committed(root, "uv.lock"):
        report.add("S1", "`uv.lock` is not committed: git does not track it")
    for k in kinds:
        for pattern in k.forbid:
            for found in sorted(root.glob(pattern)):
                report.add(k.id, f"`{found.relative_to(root)}` is not allowed for this kind")
        check_from(root, k, report)
        check_tests_contain(root, k, report)


def check_from(root: Path, kind: Kind, report: Report) -> None:
    if not kind.containerfile_from:
        return
    froms = [
        line
        for path in root.glob("Containerfile*")
        for line in path.read_text(errors="ignore").splitlines()
        if line.upper().startswith("FROM ")
    ]
    if not any(re.search(kind.containerfile_from, line) for line in froms):
        report.add(kind.id, f"no Containerfile FROM matches `{kind.containerfile_from}`")


def check_tests_contain(root: Path, kind: Kind, report: Report) -> None:
    tests = [p for p in root.glob("test*/**/*.py") if p.is_file()]
    for needle in kind.tests_contain:
        if not any(re.search(needle, p.read_text(errors="ignore")) for p in tests):
            report.add(kind.id, f"no test under test*/ contains `{needle}`")


def check_license(root: Path, kinds: list[Kind], report: Report) -> None:
    if not kinds or not all(k.agpl for k in kinds):
        return
    licenses = [p for p in root.iterdir() if p.name.upper().startswith(("LICENSE", "COPYING"))]
    # GPLv3 names the AGPL only in mixed case, so it cannot match this title.
    if not any(
        "GNU AFFERO GENERAL PUBLIC LICENSE" in p.read_text(errors="ignore") for p in licenses
    ):
        report.add("I1", "no AGPL-3.0 LICENSE file in the repository root")


def check_changelog(root: Path, report: Report) -> None:
    changelog = root / "CHANGELOG.md"
    if not changelog.is_file():
        report.add("R2", "no CHANGELOG.md in the repository root")
        return
    if not re.search(r"^#+\s*\[Unreleased\]", changelog.read_text(), re.MULTILINE):
        report.add("R2", "CHANGELOG.md has no `[Unreleased]` section")


RELEASE_HEAD = re.compile(r"^##\s*\[?v?(\d+)\.(\d+)\.(\d+)\]?", re.MULTILINE)
SUBHEAD = re.compile(r"^###\s*(\w+)", re.MULTILINE)
ENDS_IN = re.compile(
    r"\b(?:removed|dropped|ends?|ending|deleted|renamed|retired|goes away|until)\b"
    r"[^.;]*?\bv?(\d+)\.(\d+)(?:\.(\d+))?",
    re.IGNORECASE,
)


def _subsections(body: str) -> dict[str, str]:
    """`### Name` subsections of one release, as {lowercased name: text}."""
    heads = list(SUBHEAD.finditer(body))
    return {
        h.group(1).lower(): body[h.end() : heads[i + 1].start() if i + 1 < len(heads) else None]
        for i, h in enumerate(heads)
    }


def _bullets(text: str) -> list[str]:
    """Each bullet, with its wrapped continuation lines joined on by single spaces."""
    bullets: list[list[str]] = []
    open_bullet = False
    for line in text.splitlines():
        stripped = line.strip()
        # A marker needs its space or tab, so `-- aside` and `**bold**` lines are not bullets.
        if stripped.startswith(("- ", "* ", "-\t", "*\t")):
            bullets.append([stripped])
            open_bullet = True
        elif stripped and open_bullet:
            bullets[-1].append(stripped)
        else:
            open_bullet = False
    return [" ".join(lines) for lines in bullets]


def _newest_two(text: str) -> tuple[tuple[int, ...] | None, tuple[int, ...] | None, str]:
    """The newest release's version, the one before it, and the newest release's body."""
    heads = list(RELEASE_HEAD.finditer(text))
    versions = [tuple(int(n) for n in h.groups()) for h in heads[:2]]
    if not versions:
        return None, None, ""
    end = heads[1].start() if len(heads) > 1 else None
    return versions[0], versions[1] if len(versions) > 1 else None, text[heads[0].end() : end]


def _breaking(newest: tuple[int, ...], before: tuple[int, ...]) -> bool:
    """R1: MAJOR breaks; before 1.0, MINOR does."""
    return newest[0] > before[0] or (before[0] == 0 and newest[1] > before[1])


def check_release(root: Path, report: Report) -> None:
    """R4 on the newest release only, so history a repo already shipped cannot fail it."""
    changelog = root / "CHANGELOG.md"
    newest, before, body = (
        _newest_two(changelog.read_text()) if changelog.is_file() else (None, None, "")
    )
    if newest is None:
        return
    parts = _subsections(body)
    if before and _bullets(parts.get("removed", "")) and not _breaking(newest, before):
        shown = ".".join(map(str, newest)), ".".join(map(str, before))
        report.add(
            "R4", f"{shown[0]} removes something but is not a breaking release after {shown[1]}"
        )
    for bullet in _bullets(parts.get("deprecated", "")):
        if not _ends_after(bullet, newest):
            report.add("R4", f"deprecation names no later release that ends it: {bullet[:80]}")


def _ends_after(bullet: str, newest: tuple[int, ...]) -> bool:
    """The bullet says which release removes the thing, and that release is after this one."""
    end = ENDS_IN.search(bullet)
    return end is not None and tuple(int(n or 0) for n in end.groups()) > newest


def declared_versions(root: Path) -> list[tuple[str, str]]:
    found = []
    for glob, pattern in VERSION_FILES:
        regex = re.compile(pattern, re.MULTILINE)
        for path in sorted(root.glob(glob)):
            if not path.is_file() or ".git" in path.parts:
                continue
            for match in regex.finditer(path.read_text(errors="ignore")):
                if RELEASE_VALUE.fullmatch(match.group(1)):
                    found.append((str(path.relative_to(root)), match.group(1)))
    return found


def check_version_sync(root: Path, kinds: list[Kind], report: Report) -> None:
    if not all(k.version_sync for k in kinds):
        return
    found = declared_versions(root)
    changelog = root / "CHANGELOG.md"
    released = RELEASED.search(changelog.read_text()) if changelog.is_file() else None
    if released:
        expected, source = released.group(1), "CHANGELOG.md"
    elif found:
        expected, source = found[0][1], found[0][0]
    else:
        return
    for path, version in found:
        if version != expected:
            report.add("R2", f"{path} says {version}, {source} says {expected}")


def check(root: Path) -> Report:
    report = Report()
    if not is_v2(root):
        raise CannotRunError(f"{V1_MANIFEST} exists: this repository is judged by v1")
    kinds = check_kinds(root, report)
    check_license(root, kinds, report)
    check_files(root, kinds, report)
    check_changelog(root, report)
    check_release(root, report)
    check_version_sync(root, kinds, report)
    rules.check_locked_install(root, report)
    rules.check_renovate_overrides(root, report)
    ids = {k.id for k in kinds}
    if "K1" in ids:
        rules.check_http_bounds(root, report)
    if "K11" in ids:
        rules.check_skills(root, report)
    return report


def context(readme: str, text_path: Path = TEXT) -> str:
    """What Gatehouse reviews against for a v2 repo: the constitution, then the repo's section."""
    if not text_path.is_file():
        raise CannotRunError(f"{text_path} is missing; the image carries no constitution")
    body = text_path.read_text(encoding="utf-8").rstrip() + "\n"
    section = readme_section(readme)
    if section:
        body += "\n---\n\n# This repository\n\n" + section
    return body


def gatehouse(args: list[str], root: Path = Path(), out: Path | None = None) -> list[str]:
    """The Gatehouse command line for this repo: v2 reviews against the image's text.

    The context goes to a fresh temporary file (the container's tmpfs), never
    into the repo's tree.
    """
    if not is_v2(root):
        return ["gatehouse", *args]
    readme = root / "README.md"
    text = readme.read_text(encoding="utf-8") if readme.is_file() else ""
    if out is None:
        fd, name = tempfile.mkstemp(prefix="sdlc-constitution-", suffix=".md")
        os.close(fd)
        out = Path(name)
    out.write_text(context(text, TEXT), encoding="utf-8")
    return ["gatehouse", "--constitution", str(out), *args]


# -- the command ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sdlc-constitution", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check")
    c.add_argument("root", nargs="?", default=".")
    x = sub.add_parser("context")
    x.add_argument("root", nargs="?", default=".")
    x.add_argument("--readme", help="the README to take the section from (default ROOT/README.md)")
    x.add_argument("--text", default=str(TEXT), help="the constitution's text")
    sub.add_parser("coverage")
    g = sub.add_parser("gatehouse")
    g.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.command == "gatehouse":
            # Gatehouse's own exit code is the verdict; stdin (the diff) passes through.
            return subprocess.run(gatehouse(args.rest), check=False).returncode
        if args.command == "coverage":
            for principle, how in COVERAGE.items():
                print(f"{principle}\t{how}")
            return CLEAN
        root = Path(args.root)
        if args.command == "context":
            readme = Path(args.readme) if args.readme else root / "README.md"
            text = readme.read_text(encoding="utf-8") if readme.is_file() else ""
            sys.stdout.write(context(text, Path(args.text)))
            return CLEAN
        report = check(root)
    except CannotRunError as err:
        print(f"GATE CANNOT RUN: {err}", file=sys.stderr)
        return CANNOT_RUN
    for finding in report.findings:
        print(f"FAIL {finding}")
    if report.findings:
        print(f"constitution v2: {len(report.findings)} finding(s)")
        return FOUND
    print("constitution v2: clean")
    return CLEAN


if __name__ == "__main__":
    sys.exit(main())
