"""The rule-checked parts of K1, S1, S2 and K11, ported from the v1 validator.

Each check reads the tree only and adds findings citing the principle it
serves. They are tripwires over source text, not proofs of behaviour: what a
regex cannot decide stays with Gatehouse's review of the same principle (G2).

`sdlc_jobs/constitution.py` calls them from `check()`; they take any object
with `add(principle, message)`, so they can also be run on a v1 repo's tree
(the fleet sweep does) without `check()`'s refusal.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import re
from pathlib import Path
from typing import Protocol


class Findings(Protocol):
    def add(self, principle: str, message: str) -> None: ...


# Never part of the repository's own source.
SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__"})


def _files(root: Path, pattern: str) -> list[Path]:
    return [
        p
        for p in sorted(root.glob(pattern))
        if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(root).parts)
    ]


# -- K1: an MCP server bounds every request in size --------------------------------

# Only a server that speaks HTTP itself is judged. `urllib.request` is here
# because a survey that greps only for the third-party clients misses it.
HTTP_IMPORT = re.compile(
    r"^\s*(?:import|from)\s+(?:httpx2?|aiohttp|requests|urllib\.request)\b", re.MULTILINE
)
# Reading a body in bounded pieces. A cap taken from `content-length` alone is
# not one: the header is optional, so a chunked reply walks straight past it.
BOUNDED_READ = re.compile(
    r"\ba?iter_(?:bytes|raw)\b|\bnum_bytes_downloaded\b|\biter_content\b|\biter_chunked\b"
)
# Streaming alone bounds nothing: the same function must compare against a
# named `MAX_*_SIZE` / `MAX_*_BYTES` limit. Fakeable, but deliberately so.
CEILING_COMPARE = re.compile(
    r"[<>]=?\s*(?:self\.)?\w*MAX_\w*(?:SIZE|BYTES)\b|\bMAX_\w*(?:SIZE|BYTES)\w*\s*[<>]"
)
# The one fake a test name cannot catch is the header-only cap above; the test
# is what holds the behaviour.
OVERSIZE_TEST = re.compile(r"too.?large|too.?big|oversiz|MAX_RESPONSE", re.IGNORECASE)
_FUNC_START = re.compile(r"^(?=[ \t]*(?:async[ \t]+)?def[ \t])", re.MULTILINE)


def _function_scopes(text: str) -> list[str]:
    """Each function's own source, nested function bodies removed.

    Unparsable files fall back to splitting on `def` lines.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:  # a null byte is one too, since Python 3.12
        return _FUNC_START.split(text)
    lines = text.splitlines()
    func_types = (ast.FunctionDef, ast.AsyncFunctionDef)
    scopes = []
    for fn in ast.walk(tree):
        if not isinstance(fn, func_types):
            continue
        skip: set[int] = set()
        for inner in ast.walk(fn):
            if inner is not fn and isinstance(inner, func_types):
                skip.update(range(inner.lineno, (inner.end_lineno or inner.lineno) + 1))
        end = fn.end_lineno or fn.lineno
        scopes.append("\n".join(lines[i - 1] for i in range(fn.lineno, end + 1) if i not in skip))
    return scopes


def _server_sources(root: Path) -> list[str]:
    """The server's Python: `src/`, else every non-test file (never a silent pass)."""
    found = _files(root, "src/**/*.py")
    if not found:
        found = [
            p
            for p in _files(root, "**/*.py")
            if not p.relative_to(root).parts[0].startswith("test")
        ]
    return [p.read_text(errors="ignore") for p in found]


def check_http_bounds(root: Path, report: Findings) -> None:
    """K1: a server that makes HTTP requests bounds what it reads, and a test proves it."""
    sources = _server_sources(root)
    if not any(HTTP_IMPORT.search(t) for t in sources):
        return
    if not any(BOUNDED_READ.search(t) for t in sources):
        report.add(
            "K1",
            "the server makes HTTP requests but never reads a response in bounded pieces "
            "(`aiter_bytes`, `iter_bytes`, `iter_content`, ...); a `content-length` check "
            "is no bound, since a chunked reply carries no such header",
        )
    elif not any(
        BOUNDED_READ.search(scope) and CEILING_COMPARE.search(scope)
        for t in sources
        for scope in _function_scopes(t)
    ):
        report.add(
            "K1",
            "no function that reads a response in pieces compares it against a ceiling "
            "(`MAX_*_SIZE` or `MAX_*_BYTES`); streaming without one bounds nothing",
        )
    tests = _files(root, "test*/**/*.py")
    if not any(OVERSIZE_TEST.search(p.read_text(errors="ignore")) for p in tests):
        report.add("K1", "no test under test*/ rejects an oversized response body")


# -- S1: a build installs exactly what the lockfiles record ---------------------------

# A pip/uv install invocation. `uv tool install` is deliberately not matched: it
# installs a standalone tool, not the project's dependency set.
_PIP_INSTALL = re.compile(r"\b(?:pip[0-9.]*|uv\s+pip|python[0-9.]*\s+-m\s+pip)\s+install\b")
_NO_DEPS = re.compile(r"(?<![\w-])--no-deps(?![\w-])")
# How a Containerfile may legitimately install the locked set.
_LOCK_REFERENCE = re.compile(
    r"uv\.lock|uv\s+export\b[^\n]*--frozen|uv\s+sync\b[^\n]*--(?:frozen|locked)"
)
# Flags that consume the next token as their value, so that value is not an
# install target (`--python /app/.venv/bin/python` reads as a path otherwise).
# `-e` is not here: its value is the target and must be judged.
_VALUE_FLAGS = frozenset(
    {
        *("-p", "--python", "--python-version", "--python-platform", "--prefix"),
        *("-t", "--target", "--root", "--src", "--index-url", "--extra-index-url"),
        *("--index", "--default-index", "-f", "--find-links", "-c", "--constraint"),
        *("--cache-dir", "--exclude-newer", "--exclude-newer-package", "--link-mode"),
        *("-C", "--config-setting", "--config-settings", "--no-binary", "--only-binary"),
        *("--platform", "--abi", "--implementation", "--report", "--hash"),
        *("-o", "--output-file"),
    }
)


def resolving_install(command: str) -> bool:
    """A pip/uv-pip install that resolves a dependency graph, not a named pin.

    Resolving targets: the project (`.`, `-e .`, `.[extra]`, quoted or not), a
    local path, or a requirements file. A named package (`uv==0.13.7`) resolves
    nothing the repo locks.
    """
    match = _PIP_INSTALL.search(command)
    if not match:
        return False
    tokens = command[match.end() :].split()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in ("-r", "--requirement") or token.startswith("--requirement="):
            return True
        if token in _VALUE_FLAGS:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        base = token.strip("\"'").split("[", 1)[0]
        if base in (".", "..") or base.startswith(("./", "../", "/")):
            return True
        i += 1
    return False


def containerfile_commands(text: str) -> list[str]:
    """Logical shell commands: comments dropped, continuations joined, split on && and ;.

    So a comment naming uv.lock is no compliance, and a `--no-deps` on a later
    clause cannot mask its absence on the install itself.
    """
    kept = "\n".join(line for line in text.splitlines() if not re.match(r"\s*#", line))
    joined = re.sub(r"\\\s*\n", " ", kept).splitlines()
    return [part.strip() for line in joined for part in re.split(r"&&|;", line) if part.strip()]


def check_locked_install(root: Path, report: Findings) -> None:
    """S1: an image installs its Python dependencies from the committed lock."""
    has_lock = (root / "uv.lock").is_file()
    for path in _files(root, "Containerfile*"):
        commands = containerfile_commands(path.read_text(errors="ignore"))
        resolving = [c for c in commands if resolving_install(c)]
        for command in resolving:
            if not _NO_DEPS.search(command):
                report.add(
                    "S1",
                    f"{path.name} installs without --no-deps (`{command[:70]}`), so pip "
                    "re-resolves what the lock pinned",
                )
        if resolving and has_lock and not any(_LOCK_REFERENCE.search(c) for c in commands):
            report.add(
                "S1",
                f"{path.name} resolves Python dependencies but never installs from uv.lock "
                "(`COPY uv.lock`, `uv export --frozen` or `uv sync --frozen`)",
            )


# -- S2: a repo leaves the fleet's release-age cooldown alone ---------------------

# Every file Renovate reads a repo's own config from.
RENOVATE_CONFIG_FILES = (
    *(
        f"{d}{n}"
        for d in ("", ".github/", ".gitlab/")
        for n in ("renovate.json", "renovate.jsonc", "renovate.json5")
    ),
    *(".renovaterc", ".renovaterc.json", ".renovaterc.jsonc", ".renovaterc.json5"),
    "package.json",
)
# The release-age cooldown: the wait before a new version is proposed. It is
# fleet policy, so a repo config may override anything but these keys
# (`stabilityDays` is the old name Renovate migrates to minimumReleaseAge) ...
RENOVATE_LOCKED = ("minimumReleaseAge", "minimumReleaseAgeBehaviour", "internalChecksFilter")
RENOVATE_LOCKED_OLD = ("stabilityDays",)
# ... and the built-in presets that set them themselves (fnmatch).
RENOVATE_LOCKED_PRESETS = ("config:best-practices", "security:minimumReleaseAge*")
_RENOVATE_KEY = re.compile(
    r"[\"']?\b("
    + "|".join(map(re.escape, RENOVATE_LOCKED + RENOVATE_LOCKED_OLD))
    + r")\b[\"']?\s*:"
)
_QUOTED = re.compile(r"[\"']([\w:./>@#-]+?)(?:\([^\"']*\))?[\"']")


def _renovate_text(name: str, text: str) -> str:
    """The Renovate config in a file; for package.json, only its `renovate` key."""
    if name != "package.json":
        return text
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return ""
    renovate = doc.get("renovate") if isinstance(doc, dict) else None
    return json.dumps(renovate) if renovate is not None else ""


def check_renovate_overrides(root: Path, report: Findings) -> None:
    """S2: a repo's Renovate config leaves the fleet's release-age cooldown alone.

    Matched as text, because JSON5 and JSONC do not parse here and a key inside
    a packageRule loosens the cooldown as surely as one at the top.
    """
    for name in RENOVATE_CONFIG_FILES:
        path = root / name
        if not path.is_file():
            continue
        text = _renovate_text(name, path.read_text(errors="ignore"))
        for key in sorted(set(_RENOVATE_KEY.findall(text))):
            report.add(
                "S2",
                f"{name} sets `{key}`; the release-age cooldown is the fleet's, "
                "not a repo's to change",
            )
        presets = {
            p
            for p in _QUOTED.findall(text)
            if any(fnmatch.fnmatchcase(p, g) for g in RENOVATE_LOCKED_PRESETS)
        }
        for preset in sorted(presets):
            report.add(
                "S2",
                f"{name} extends `{preset}`, which sets its own release-age cooldown",
            )


# -- K11: a skill carries no credentials ---------------------------------------------

# A literal assigned to a credential name. A value that starts with `$`, `<` or
# `{` is an expansion or a placeholder (`TOKEN="$(gh auth token)"`), not a secret.
_CREDENTIAL = re.compile(
    r"(?:api_key|password|secret|token)\s*=\s*[\"'](?![$<{])[^\"'\n]+[\"']", re.IGNORECASE
)


def check_skills(root: Path, report: Findings) -> None:
    """K11: no SKILL.md assigns a credential literal.

    Fixtures under a top-level test*/ directory are skipped: a checker's own
    broken examples are not skills it ships.
    """
    for path in _files(root, "**/SKILL.md"):
        name = path.relative_to(root)
        if name.parts[0].startswith("test"):
            continue
        if _CREDENTIAL.search(path.read_text(errors="ignore")):
            report.add("K11", f"{name} assigns what looks like a credential literal")
