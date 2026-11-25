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
import os
import posixpath
import re
from pathlib import Path
from typing import Protocol


class Findings(Protocol):
    """Where a check reports. `constitution.Report` is the one `check()` uses."""

    def add(self, principle: str, message: str) -> None:
        """Record one finding: `principle` is the ID it serves (`S1`, `K11`) and
        `message` says what is wrong and where, naming files and lines, never
        a value that could be a secret. Recording must not raise or stop the
        check; every finding a check makes is kept."""
        ...


# Never part of the repository's own source.
SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__"})


def _files(root: Path, pattern: str) -> list[Path]:
    # Pruned while walking, so a `node_modules` or `.venv` is never descended.
    found = []
    for top, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            path = Path(top, name)
            if path.relative_to(root).full_match(pattern) and path.is_file():
                found.append(path)
    return sorted(found)


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


_FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _nested_functions(node: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The functions directly inside `node`, not descending into any of them."""
    found = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _FUNCTION_TYPES):
            found.append(child)
        else:
            found.extend(_nested_functions(child))
    return found


def _function_scopes(text: str) -> list[str]:
    """Each function's own source, nested function bodies removed.

    Unparsable files fall back to splitting on `def` lines.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:  # a null byte is one too, since Python 3.12
        return _FUNC_START.split(text)
    lines = text.splitlines()
    scopes = []
    # One pass: each node is visited once, under its innermost function.
    stack = _nested_functions(tree)
    while stack:
        fn = stack.pop()
        inner = _nested_functions(fn)
        stack.extend(inner)
        own, line = [], fn.lineno
        for nested in sorted(inner, key=lambda n: n.lineno):
            own.extend(lines[line - 1 : nested.lineno - 1])
            line = max(line, (nested.end_lineno or nested.lineno) + 1)
        own.extend(lines[line - 1 : fn.end_lineno or fn.lineno])
        scopes.append("\n".join(own))
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
_FROZEN = re.compile(r"(?<![\w-])--(?:frozen|locked)(?![\w-])")
# `uv export` writes the lock out as a requirements file; `uv sync` installs it.
_UV_EXPORT = re.compile(r"\buv\s+export\b")
_UV_SYNC = re.compile(r"\buv\s+sync\b")
# A requirements file installed with --require-hashes is a lockfile of its own:
# every entry must be pinned and hashed, or the install refuses.
_REQUIRE_HASHES = re.compile(r"(?<![\w-])--require-hashes(?![\w-])")
_EXPORT_TO = re.compile(r"(?:(?<!\S)(?:-o|--output-file)(?:\s+|=)|>\s*)([^\s;&|]+)")
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


def _resolving_install(command: str) -> tuple[bool, list[str]]:
    """Whether a pip/uv-pip install resolves a dependency graph, and its -r files.

    Resolving targets: the project (`.`, `-e .`, `.[extra]`, quoted or not), a
    local path, or a requirements file. A named package (`uv==0.13.7`) resolves
    nothing the repo locks.
    """
    match = _PIP_INSTALL.search(command)
    if not match:
        return False, []
    tokens = [t.strip("\"'") for t in command[match.end() :].split()]
    resolving, requirements = False, []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in ("-r", "--requirement") and i + 1 < len(tokens):
            requirements.append(tokens[i + 1])
            i += 2
            continue
        if token.startswith("--requirement="):
            requirements.append(token.partition("=")[2])
        elif token in _VALUE_FLAGS:
            i += 1
        elif not token.startswith("-"):
            base = token.split("[", 1)[0]
            resolving |= base in (".", "..") or base.startswith(("./", "../", "/"))
        i += 1
    return resolving or bool(requirements), requirements


def containerfile_commands(text: str) -> list[tuple[int, str]]:
    """Logical shell commands and the line each starts on: comments dropped,
    continuations joined, each split on && and ;.

    So a comment naming uv.lock is no compliance, and a `--no-deps` on a later
    clause cannot mask its absence on the install itself.
    """
    commands: list[tuple[int, str]] = []
    start, buf = 0, ""
    for number, raw in enumerate(text.splitlines(), 1):
        if re.match(r"\s*#", raw):
            continue
        line = raw.rstrip()
        start = start or number
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        commands += [
            (start, part.strip()) for part in re.split(r"&&|;", buf + line) if part.strip()
        ]
        start, buf = 0, ""
    if buf.strip():
        commands += [(start, part.strip()) for part in re.split(r"&&|;", buf) if part.strip()]
    return commands


def _locked_paths(commands: list[tuple[int, str]]) -> list[tuple[str, set[str]]]:
    """For each command, its WORKDIR and the files exported from the lock before it.

    A file is locked when `uv export --frozen|--locked` wrote it, or a COPY/ADD
    carried such a file to a new path (the fleet exports in one stage and
    installs in the next). Relative paths are taken against WORKDIR.
    """
    cwd, locked = "/", set[str]()
    seen: list[tuple[str, set[str]]] = []
    for _, command in commands:
        seen.append((cwd, set(locked)))
        word, _, rest = command.partition(" ")
        word = word.upper()
        if word == "FROM":
            cwd = "/"
        elif word == "WORKDIR":
            cwd = posixpath.join(cwd, rest.strip())
        elif word in ("COPY", "ADD"):
            args = [a for a in rest.split() if not a.startswith("--")]
            sources, dest = args[:-1], (args[-1] if args else "")
            for source in (posixpath.normpath(posixpath.join(cwd, x)) for x in sources):
                if source in locked:
                    into = posixpath.join(cwd, dest)
                    if dest.endswith("/"):
                        into = posixpath.join(into, posixpath.basename(source))
                    locked.add(posixpath.normpath(into))
        if _UV_EXPORT.search(command) and _FROZEN.search(command):
            locked.update(
                posixpath.normpath(posixpath.join(cwd, m)) for m in _EXPORT_TO.findall(command)
            )
    return seen


def _judge_install(
    command: str, cwd: str, locked: set[str], has_lock: bool
) -> tuple[bool, bool, list[str]]:
    """One command's S1 problems, and whether it resolves and whether it uses the lock."""
    problems: list[str] = []
    from_lock = False
    if _UV_SYNC.search(command):
        if _FROZEN.search(command):
            from_lock = True
        elif has_lock:
            problems.append("runs `uv sync` without --frozen or --locked")
    resolving, requirements = _resolving_install(command)
    if resolving and not _NO_DEPS.search(command):
        problems.append("installs without --no-deps, so pip re-resolves what the lock pinned")
    for requirement in requirements:
        exported = posixpath.normpath(posixpath.join(cwd, requirement)) in locked
        if exported or _REQUIRE_HASHES.search(command):
            from_lock = True
        elif has_lock:
            problems.append(
                "installs a requirements file that is neither exported from uv.lock "
                "(`uv export --frozen -o FILE`) nor hash-pinned (`--require-hashes`)"
            )
    return resolving, from_lock, problems


def check_locked_install(root: Path, report: Findings) -> None:
    """S1: an image installs its Python dependencies from the committed lock.

    Every resolving install takes --no-deps. When uv.lock is committed, every
    requirements file installed must have been exported from it, a `uv sync`
    must be --frozen or --locked, and at least one install must use the lock.
    Copying uv.lock in proves nothing on its own. A finding names the line,
    never the command, which may carry an index credential.
    """
    has_lock = (root / "uv.lock").is_file()
    for path in _files(root, "Containerfile*"):
        commands = containerfile_commands(path.read_text(errors="ignore"))
        any_resolving = any_locked = False
        states = _locked_paths(commands)
        for (line, command), (cwd, locked) in zip(commands, states, strict=True):
            resolving, from_lock, problems = _judge_install(command, cwd, locked, has_lock)
            any_resolving |= resolving
            any_locked |= from_lock
            for problem in problems:
                report.add("S1", f"{path.name}:{line} {problem}")
        if has_lock and any_resolving and not any_locked:
            report.add(
                "S1",
                f"{path.name} resolves Python dependencies but never installs from uv.lock "
                "(`uv export --frozen` to a file it installs, or `uv sync --frozen`)",
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
    """The Renovate config in a file; for package.json, only its `renovate` key.

    A package.json that does not parse raises ValueError: Renovate cannot read
    it either, so whether it overrides the cooldown is unknown, not "no".
    """
    if name != "package.json":
        return text
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as err:
        raise ValueError(f"{name} is not valid JSON ({err})") from err
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
        try:
            text = _renovate_text(name, path.read_text(errors="ignore"))
        except ValueError as err:
            report.add("S2", f"{err}, so its Renovate config cannot be judged")
            continue
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

# A literal assigned to a credential name, as shell (`TOKEN=...`), code
# (`api_key = "..."`) or YAML (`password: ...`). A value that starts with `$`,
# `<` or `{` is an expansion or a placeholder (`TOKEN="$(gh auth token)"`), not
# a secret. Unquoted, the value must look like a key (letters and digits, six
# or more) so that prose such as "token: the one gh holds" stays out.
_CREDENTIAL = re.compile(
    r"(?:api_key|password|secret|token)\s*[=:]\s*"
    r"(?:[\"'](?![$<{])[^\"'\n]+[\"']"
    r"|(?![$<{])(?=[\w./+-]*\d)(?=[\w./+-]*[A-Za-z])[\w./+-]{6,}(?![\w./+-]))",
    re.IGNORECASE,
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
