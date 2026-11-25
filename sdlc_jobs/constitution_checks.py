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
import contextlib
import fnmatch
import functools
import json
import os
import posixpath
import re
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import json5

from sdlc_jobs import _tools as tools


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


def _own_file(root: Path, path: Path) -> bool:
    # A committed symlink, or a path through a symlinked directory, can point
    # out of the repo; is_file() follows it, so neither is read.
    return (
        not path.is_symlink() and path.is_file() and path.resolve().is_relative_to(root.resolve())
    )


def committed(root: Path, name: str) -> bool:
    """Whether `name` is the repo's own file and git tracks it.

    A lock that exists but is gitignored or untracked is not what CI builds
    from. Outside a git work tree (a bare tree, a fixture) existence is all
    there is to ask.
    """
    if not _own_file(root, root / name):
        return False
    try:
        tracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", name],
            capture_output=True,
            check=False,
        )
        inside = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            check=False,
        )
    except OSError:  # no git at all
        return True
    return tracked.returncode == 0 or inside.returncode != 0


def _files(root: Path, pattern: str) -> list[Path]:
    # Pruned while walking, so a `node_modules` or `.venv` is never descended.
    found = []
    for top, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            path = Path(top, name)
            if path.relative_to(root).full_match(pattern) and _own_file(root, path):
                found.append(path)
    return sorted(found)


# -- K1: an MCP server bounds every request in size and in time ---------------------

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
# Time: `requests` and `urllib.request` wait forever unless told otherwise;
# httpx and aiohttp carry a default that `timeout=None` throws away.
NO_DEFAULT_TIMEOUT = re.compile(
    r"^\s*(?:import|from)\s+(?:requests|urllib\.request)\b", re.MULTILINE
)
TIMEOUT_SET = re.compile(r"\btimeout\s*=|\bTimeout\(|\bClientTimeout\(")
TIMEOUT_NONE = re.compile(r"\btimeout\s*=\s*None\b|\bTimeout\(\s*None\b|\bClientTimeout\(\s*\)")


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
    """K1: a server that makes HTTP requests bounds what it reads and how long it
    waits, and a test proves the size bound."""
    sources = _server_sources(root)
    if not any(HTTP_IMPORT.search(t) for t in sources):
        return
    if any(TIMEOUT_NONE.search(t) for t in sources):
        report.add("K1", "the server turns its HTTP timeout off (`timeout=None`)")
    elif any(NO_DEFAULT_TIMEOUT.search(t) for t in sources) and not any(
        TIMEOUT_SET.search(t) for t in sources
    ):
        report.add(
            "K1",
            "the server uses `requests` or `urllib.request`, which wait forever by default, "
            "and never sets a `timeout=`",
        )
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
_EXPORT_TO = re.compile(r"(?:(?<!\S)(?:-o|--output-file)(?:\s+|=)|>>?\s*)([^\s;&|]+)")
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


# A requirements file installed with --require-hashes is a lockfile of its own
# (every entry must be pinned and hashed, or the install refuses) when the repo
# commits it; one the build generates is hashed against what it resolved then.
_REQUIRE_HASHES = "--require-hashes"


@dataclass(frozen=True)
class _Install:
    """A pip/uv-pip install: whether it resolves, its -r files, its own --require-hashes."""

    resolving: bool = False
    requirements: tuple[str, ...] = ()
    hashed: bool = False


def _resolving_install(command: str) -> _Install:
    """Whether a pip/uv-pip install resolves a dependency graph, and its -r files.

    Resolving targets: the project (`.`, `-e .`, `.[extra]`, quoted or not), a
    local path, or a requirements file. A named package (`uv==0.13.7`) resolves
    nothing the repo locks. --require-hashes counts only as one of the install's
    own tokens, never as text elsewhere in the command.
    """
    match = _PIP_INSTALL.search(command)
    if not match:
        return _Install()
    tokens = [t.strip("\"'") for t in command[match.end() :].split()]
    resolving, hashed, requirements = False, False, []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        hashed |= token == _REQUIRE_HASHES
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
    return _Install(resolving or bool(requirements), tuple(requirements), hashed)


_CLAUSE = re.compile(r"&&|\|\||;|\|")


def _uncommented(line: str) -> str:
    """`line` without its shell comment: a `#` that starts a word outside quotes."""
    quote = ""
    for i, char in enumerate(line):
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def _containerfile_commands(text: str) -> list[tuple[int, str]]:
    """Logical shell commands and the line each starts on: comments dropped,
    continuations joined, each split on &&, ||, | and ;.

    So a comment naming uv.lock is no compliance, and a `--no-deps` on another
    clause, or a pipe's, cannot stand in for one on the install itself.
    """
    commands: list[tuple[int, str]] = []
    start, buf = 0, ""
    for number, raw in enumerate(text.splitlines(), 1):
        if re.match(r"\s*#", raw):
            continue
        line = _uncommented(raw).rstrip()
        start = start or number
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        commands += [(start, part.strip()) for part in _CLAUSE.split(buf + line) if part.strip()]
        start, buf = 0, ""
    if buf.strip():
        commands += [(start, part.strip()) for part in _CLAUSE.split(buf) if part.strip()]
    return commands


# What one COPY/ADD put where in the image, in order: (kind, image path,
# build-context path). `file` and `dir` map a context file or directory there,
# `glob` a pattern's matches into a directory; `gone` is anything else copied
# there (another stage's files, a URL), and `named` a path a shell command named,
# so may have written (`>>`, `sed -i`, `cp`, `curl -o`): no commit holds either.
_Copied = tuple[str, str, str]
_GLOB = re.compile(r"[*?\[]")
# Instructions that run no shell; a RUN's later clauses are shell, unprefixed.
_NOT_SHELL = frozenset(
    {"FROM", "WORKDIR", "COPY", "ADD", "ENV", "ARG", "LABEL", "USER", "EXPOSE", "VOLUME"}
    | {"ENTRYPOINT", "CMD", "SHELL", "STOPSIGNAL", "HEALTHCHECK", "ONBUILD", "MAINTAINER"}
)


def _copied(root: Path, source: str, dest: str, into_dir: bool) -> _Copied:
    """Where one build-context source of a COPY/ADD lands in the image."""
    context = posixpath.normpath(source).lstrip("/")
    if "://" in source or context.startswith(".."):
        return ("gone", dest, "")
    if _GLOB.search(context):
        return ("glob", dest, context)
    if context == "." or (root / context).is_dir():
        return ("dir", dest, "" if context == "." else context)
    return (
        "file",
        posixpath.join(dest, posixpath.basename(context)) if into_dir else dest,
        context,
    )


_COPY_FLAGS = re.compile(r"((?:\s*--\S+)*)\s*(.*)", re.DOTALL)


def _copy_args(rest: str) -> tuple[list[str], list[str]]:
    """A COPY/ADD's flags and its sources-then-destination, in shell or JSON form."""
    match = _COPY_FLAGS.fullmatch(rest)
    flags, body = (match[1].split(), match[2]) if match else ([], rest)
    exec_form: object = None
    if body.startswith("["):
        with contextlib.suppress(json.JSONDecodeError):
            exec_form = json.loads(body)
    if isinstance(exec_form, list) and all(isinstance(a, str) for a in exec_form):
        return flags, exec_form
    return flags, body.split()


def _copies(root: Path, rest: str, cwd: str, dirs: set[str]) -> list[_Copied]:
    """Where a COPY/ADD put the build context's files (the repo root's).

    The destination is a directory, so a file keeps its own name there, when it
    ends in /, is `.`, takes several sources, or is one the build already knows
    as a directory (a WORKDIR, or where an earlier COPY put a directory).
    """
    flags, args = _copy_args(rest)
    *sources, last = args or [""]
    dest = posixpath.normpath(posixpath.join(cwd, last))
    if any(f.startswith("--from") for f in flags):
        return [("gone", dest, "")] if sources else []
    into_dir = last.endswith("/") or len(sources) > 1 or dest in dirs
    return [_copied(root, source, dest, into_dir) for source in sources]


def _under(path: str, at: str) -> bool:
    return path == at or path.startswith(at.rstrip("/") + "/")


def _committed_source(root: Path, copied: list[_Copied], path: str) -> bool:
    """Whether the image's `path` is, as last written, a file the repo commits."""
    for kind, at, context in reversed(copied):
        if kind == "file" and path == at:
            return committed(root, context)
        if kind == "dir" and _under(path, at):
            rel = posixpath.relpath(path, at)
            return committed(root, posixpath.join(context, rel) if context else rel)
        if kind == "glob" and posixpath.dirname(path) == at:
            name = posixpath.basename(path)
            if fnmatch.fnmatchcase(name, posixpath.basename(context)):
                return committed(root, posixpath.join(posixpath.dirname(context), name))
        if (kind == "gone" and _under(path, at)) or (kind == "named" and path == at):
            return False
    return False


def _named(command: str, cwd: str) -> set[str]:
    """Every path a shell clause names, except a pip install's -r files.

    A clause that names a file may have rewritten it, and telling a read from a
    write is past a tripwire, so either takes the file out of the repo's hands.
    """
    reading = set(_resolving_install(command).requirements)
    named = set[str]()
    for word in command.split():
        token = word.strip("\"'")
        value = token.partition("=")[2] if token.startswith("-") else token.lstrip("0123456789&<>")
        if value and value not in reading:
            named.add(posixpath.normpath(posixpath.join(cwd, value)))
    return named


def _locked_paths(
    root: Path, commands: list[tuple[int, str]]
) -> list[tuple[str, set[str], list[_Copied]]]:
    """For each command, its WORKDIR, the files exported from the lock before it,
    and what the stage has copied in from the repo so far.

    A file is locked when `uv export --frozen|--locked` wrote it, or a COPY/ADD
    carried such a file to a new path (the fleet exports in one stage and
    installs in the next). Relative paths are taken against WORKDIR. A file a
    later shell clause names, other than as an install's -r, is no longer the
    repo's copy.
    """
    cwd, locked = "/", set[str]()
    copied: list[_Copied] = []
    dirs = {"/"}
    seen: list[tuple[str, set[str], list[_Copied]]] = []
    for _, command in commands:
        seen.append((cwd, set(locked), list(copied)))
        word, _, rest = command.partition(" ")
        word = word.upper()
        if word == "FROM":
            cwd, copied, dirs = "/", [], {"/"}
        elif word == "WORKDIR":
            cwd = posixpath.normpath(posixpath.join(cwd, rest.strip()))
            dirs.add(cwd)
        elif word in ("COPY", "ADD"):
            new = _copies(root, rest, cwd, dirs)
            copied += new
            dirs.update(at for kind, at, _ in new if kind in ("dir", "glob"))
            dirs.update(posixpath.dirname(at) for kind, at, _ in new if kind == "file")
            args = _copy_args(rest)[1]
            sources, dest = args[:-1], (args[-1] if args else "")
            for source in (posixpath.normpath(posixpath.join(cwd, x)) for x in sources):
                if source in locked:
                    into = posixpath.join(cwd, dest)
                    if dest.endswith("/"):
                        into = posixpath.join(into, posixpath.basename(source))
                    locked.add(posixpath.normpath(into))
        if word not in _NOT_SHELL:
            shell = rest if word == "RUN" else command
            copied += [("named", path, "") for path in sorted(_named(shell, cwd))]
        if _UV_EXPORT.search(command) and _FROZEN.search(command):
            locked.update(
                posixpath.normpath(posixpath.join(cwd, m)) for m in _EXPORT_TO.findall(command)
            )
    return seen


def _judge_install(
    command: str, cwd: str, locked: set[str], has_lock: bool, from_repo: Callable[[str], bool]
) -> tuple[bool, bool, list[str]]:
    """One command's S1 problems, and whether it resolves and whether it uses the lock.

    A requirements file installed with --require-hashes is a lock only when it is
    the repo's own committed file: one generated during the build is hashed
    against whatever it resolved then.
    """
    problems: list[str] = []
    from_lock = False
    if _UV_SYNC.search(command):
        if _FROZEN.search(command):
            from_lock = True
        elif has_lock:
            problems.append("runs `uv sync` without --frozen or --locked")
    install = _resolving_install(command)
    if install.resolving and not _NO_DEPS.search(command):
        problems.append("installs without --no-deps, so pip re-resolves what the lock pinned")
    for requirement in install.requirements:
        path = posixpath.normpath(posixpath.join(cwd, requirement))
        if path in locked or (install.hashed and from_repo(path)):
            from_lock = True
        elif has_lock:
            problems.append(
                "installs a requirements file that is neither exported from uv.lock "
                "(`uv export --frozen -o FILE`) nor committed hash-pinned "
                "(`--require-hashes`)"
            )
    return install.resolving, from_lock, problems


def check_locked_install(root: Path, report: Findings) -> None:
    """S1: an image installs its Python dependencies from the committed lock.

    Every resolving install takes --no-deps. When uv.lock is committed, every
    requirements file installed must have been exported from it, a `uv sync`
    must be --frozen or --locked, and at least one install must use the lock.
    Copying uv.lock in proves nothing on its own. A finding names the line,
    never the command, which may carry an index credential.
    """
    has_lock = committed(root, "uv.lock")
    for path in _files(root, "Containerfile*"):
        commands = _containerfile_commands(path.read_text(errors="ignore"))
        any_resolving = any_locked = False
        states = _locked_paths(root, commands)
        for (line, command), (cwd, locked, copied) in zip(commands, states, strict=True):
            resolving, from_lock, problems = _judge_install(
                command,
                cwd,
                locked,
                has_lock,
                functools.partial(_committed_source, root, copied),
            )
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
# fleet policy, so a repo config may override anything but the keys and presets
# (fnmatch) the constitution lists in its fleet.toml; tests point this at a fixture.
FLEET = tools.CONSTITUTION_FLEET


# Renovate's built-in preset namespaces (`:x` is `default:x`), whose contents the
# pinned Renovate fixes, and the constitution's own presets. A preset from
# anywhere else can set the cooldown where this check cannot read it.
RENOVATE_BUILTIN = frozenset(
    {"abandonments", "config", "customManagers", "default", "docker", "group", "helpers"}
    | {"mergeConfidence", "monorepo", "npm", "packages", "preview", "regexManagers"}
    | {"replacements", "schedule", "security", "workarounds"}
)
_FLEET_PRESET = re.compile(r"(?:github|local)>crunchtools/constitution(?:$|[/:#])")


def _judgeable(preset: str) -> bool:
    """Whether a preset is Renovate's built-in or the constitution's."""
    if _FLEET_PRESET.match(preset):
        return True
    namespace, colon, _ = preset.partition(":")
    return bool(colon) and (namespace or "default") in RENOVATE_BUILTIN


def _fleet_locked() -> tuple[set[str], list[str]]:
    """The locked keys and presets from the constitution's fleet.toml.

    Raises ValueError when the file is missing or lacks either list: with no
    policy to judge against, a config is unknown, not compliant.
    """
    try:
        fleet = tomllib.loads(FLEET.read_text(encoding="utf-8"))["fleet"]
        keys, presets = list(fleet["renovate_locked"]), list(fleet["renovate_locked_presets"])
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError) as err:
        raise ValueError(
            f"the constitution's {FLEET} gives no locked Renovate keys ({err})"
        ) from err
    return {str(k) for k in keys}, [str(p) for p in presets]


def _renovate_config(name: str, text: str) -> object:
    """The Renovate config in a file, parsed; for package.json, only its `renovate` key.

    Every file parses as JSON5, a superset of JSON and JSONC, as Renovate reads
    them. One that does not raises ValueError: Renovate cannot read it either,
    so whether it overrides the cooldown is unknown, not "no".
    """
    try:
        doc = json5.loads(text)
    except ValueError as err:
        raise ValueError(f"{name} is not valid JSON5 ({err})") from err
    if name == "package.json":
        return doc.get("renovate") if isinstance(doc, dict) else None
    return doc


def _renovate_settings(node: object) -> tuple[set[str], set[str]]:
    """Every key the config sets and every preset it extends, at any depth.

    Keys, not text, so a locked name in a description, a comment or an
    ignorePresets entry is no override; nesting is followed because a key
    inside a packageRule loosens the cooldown as surely as one at the top.
    """
    keys: set[str] = set()
    presets: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(key)
            if key == "extends":
                for preset in value if isinstance(value, list) else [value]:
                    if isinstance(preset, str):
                        # A preset's arguments, `:foo(bar)`, are not its name.
                        presets.add(preset.split("(", 1)[0])
            sub_keys, sub_presets = _renovate_settings(value)
            keys |= sub_keys
            presets |= sub_presets
    elif isinstance(node, list):
        for item in node:
            sub_keys, sub_presets = _renovate_settings(item)
            keys |= sub_keys
            presets |= sub_presets
    return keys, presets


def check_renovate_overrides(root: Path, report: Findings) -> None:
    """S2: a repo's Renovate config leaves the fleet's release-age cooldown alone."""
    policy: tuple[set[str], list[str]] | None = None
    for name in RENOVATE_CONFIG_FILES:
        path = root / name
        if not _own_file(root, path):
            continue
        try:
            config = _renovate_config(name, path.read_text(errors="ignore"))
            policy = policy or _fleet_locked()
        except ValueError as err:
            report.add("S2", f"{err}, so {name} cannot be judged")
            continue
        locked_keys, locked_presets = policy
        keys, extended = _renovate_settings(config)
        for key in sorted(keys & locked_keys):
            report.add(
                "S2",
                f"{name} sets `{key}`; the release-age cooldown is the fleet's, "
                "not a repo's to change",
            )
        for preset in sorted(p for p in extended if not _judgeable(p)):
            report.add(
                "S2",
                f"{name} extends `{preset}`, a preset from outside Renovate and the "
                "constitution, so whether it changes the release-age cooldown cannot be judged",
            )
        presets = {p for p in extended if any(fnmatch.fnmatchcase(p, g) for g in locked_presets)}
        for preset in sorted(presets):
            report.add(
                "S2",
                f"{name} extends `{preset}`, which sets its own release-age cooldown",
            )


# -- K11: a skill carries no credentials ---------------------------------------------

# A vendor credential by its shape, whatever it is assigned to. The shapes are
# bin/sdlc-secret-scan's (a test holds them equal), plus a private key's PEM header.
_SECRET_SHAPE = re.compile(
    r"AIza[0-9A-Za-z_-]{35}|sk-ant-[A-Za-z0-9_-]{20,}|sk-or-v1-[0-9a-f]{32,}"
    r"|gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}|glpat-[A-Za-z0-9_-]{20,}"
    r"|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"
)

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
    """K11: no SKILL.md assigns a credential literal or carries a vendor token.

    Fixtures under a top-level test*/ directory are skipped: a checker's own
    broken examples are not skills it ships.
    """
    for path in _files(root, "**/SKILL.md"):
        name = path.relative_to(root)
        if name.parts[0].startswith("test"):
            continue
        text = path.read_text(errors="ignore")
        if _CREDENTIAL.search(text):
            report.add("K11", f"{name} assigns what looks like a credential literal")
        if _SECRET_SHAPE.search(text):
            report.add("K11", f"{name} carries what looks like a vendor credential or private key")
