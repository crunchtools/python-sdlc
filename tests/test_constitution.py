#!/usr/bin/env python3
"""The constitution v2 checks: each one passes a compliant repo and fails a broken one.

A check that has never failed may be unable to fail (constitution K15), so
every case starts from a repo that passes and breaks exactly one thing.

Run: tests/test_constitution.py   (needs python3 >= 3.11)
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import re
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdlc_jobs import constitution as c

AGPL = "GNU AFFERO GENERAL PUBLIC LICENSE\nVersion 3, 19 November 2007\n"
CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n## [1.2.0] - 2026-11-24\n"


def mcp_server(root: Path) -> None:
    """A compliant mcp-server repo."""
    (root / "LICENSE").write_text(AGPL)
    (root / "CHANGELOG.md").write_text(CHANGELOG)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "1.2.0"\n\n[tool.sdlc]\nrepo-kinds = ["mcp-server"]\n'
    )
    (root / "uv.lock").write_text("version = 1\n")
    (root / "Containerfile").write_text('FROM quay.io/hummingbird/python\nLABEL version="1.2.0"\n')
    (root / "tests").mkdir()
    (root / "tests" / "test_tools.py").write_text(TESTS)
    write("src/x/client.py", CLIENT)(root)


TESTS = "READ_ONLY = 'readOnlyHint'\n\n\ndef test_response_too_large(): ...\n"


# K1: an HTTP client that reads in pieces and stops at a named ceiling.
CLIENT = """import httpx

MAX_RESPONSE_SIZE = 10


async def fetch(client, url):
    body = b""
    async with client.stream("GET", url) as response:
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > MAX_RESPONSE_SIZE:
                raise ValueError("too large")
    return body
"""
# The ceiling is read from the header only, in another function from the stream.
HEADER_ONLY = """import httpx

MAX_RESPONSE_SIZE = 10


def too_big(response):
    return int(response.headers["content-length"]) > MAX_RESPONSE_SIZE


async def fetch(client, url):
    async with client.stream("GET", url) as response:
        return b"".join([c async for c in response.aiter_bytes()])
"""
# S1: the lock exported frozen, then both installs with --no-deps.
LOCKED_INSTALL = """FROM quay.io/hummingbird/python
LABEL version="1.2.0"
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-hashes -o req.txt \\
 && pip install --no-deps -r req.txt \\
 && pip install --no-deps .
"""


def findings(setup, *breaks) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        setup(root)
        for b in breaks:
            b(root)
        return c.check(root).findings


def drop(name):
    return lambda root: (root / name).unlink()


def write(name, text):
    def go(root: Path) -> None:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)

    return go


def kinds(value: str):
    return write(
        "pyproject.toml",
        f'[project]\nname = "x"\nversion = "1.2.0"\n\n[tool.sdlc]\nrepo-kinds = {value}\n',
    )


FAILS: list[tuple[str, tuple, str]] = [
    # (name, breaks, the principle the finding must cite)
    ("no repo-kinds", (write("pyproject.toml", '[project]\nversion = "1.2.0"\n'),), "G1"),
    ("unknown kind", (kinds('["mcp-server", "toaster"]'),), "G1"),
    ("no license", (drop("LICENSE"),), "I1"),
    ("GPL is not AGPL", (write("LICENSE", "GNU GENERAL PUBLIC LICENSE\n"),), "I1"),
    ("no lockfile", (drop("uv.lock"),), "S1"),
    ("no tests", (drop("tests/test_tools.py"),), "C7"),
    ("no read-only partition test", (write("tests/test_tools.py", "x = 1\n"),), "K1"),
    (
        "HTTP read with no bounded pieces",
        (
            write(
                "src/x/client.py", "import httpx\n\ndef get(c, u):\n    return c.get(u).content\n"
            ),
        ),
        "K1",
    ),
    ("ceiling from content-length only", (write("src/x/client.py", HEADER_ONLY),), "K1"),
    (
        "no oversized-body test",
        (write("tests/test_tools.py", "READ_ONLY = 'readOnlyHint'\n"),),
        "K1",
    ),
    (
        "pip install . re-resolves",
        (write("Containerfile", LOCKED_INSTALL.replace("--no-deps .", ".")),),
        "S1",
    ),
    (
        "lock named only in a comment",
        (
            write(
                "Containerfile",
                'FROM x\nLABEL version="1.2.0"\n# installs from uv.lock\n'
                "RUN pip install --no-deps -r requirements.txt\n",
            ),
        ),
        "S1",
    ),
    (
        "renovate sets the cooldown",
        (write("renovate.json", '{"minimumReleaseAge": "1 day"}'),),
        "S2",
    ),
    (
        "renovate cooldown inside a packageRule, JSON5",
        (write(".github/renovate.json5", "{packageRules: [{stabilityDays: 0}]}"),),
        "S2",
    ),
    (
        "renovate extends a cooldown preset",
        (write("renovate.json", '{"extends": ["config:best-practices"]}'),),
        "S2",
    ),
    (
        "package.json renovate key",
        (write("package.json", '{"renovate": {"extends": ["security:minimumReleaseAgeNpm"]}}'),),
        "S2",
    ),
    ("no Containerfile", (drop("Containerfile"),), "K1"),
    ("no changelog", (drop("CHANGELOG.md"),), "R2"),
    ("no Unreleased", (write("CHANGELOG.md", "## [1.2.0] - 2026-11-24\n"),), "R2"),
    ("label disagrees", (write("Containerfile", 'FROM x\nLABEL version="1.1.0"\n'),), "R2"),
    ("host tool with a Containerfile", (kinds('["host-tool"]'),), "K4"),
    (
        "bootc without a bootc FROM",
        (kinds('["container-image", "bootc-image"]'),),
        "K7",
    ),
]


def changelog(*releases: str) -> str:
    return "# Changelog\n\n## [Unreleased]\n\n" + "\n".join(releases)


REMOVED = "### Removed\n\n- `--old`; use `--new` instead.\n"
R4_FAILS = {
    "removal in a MINOR after 1.0": changelog(
        "## [1.3.0] - 2026-11-24\n\n" + REMOVED, "## [1.2.0] - 2026-11-01\n"
    ),
    "removal in a PATCH before 1.0": changelog(
        "## [0.4.1] - 2026-11-24\n\n" + REMOVED, "## [0.4.0] - 2026-11-01\n"
    ),
    "deprecation with no end": changelog(
        "## [1.2.0] - 2026-11-24\n\n### Deprecated\n\n- `--old` is deprecated.\n",
        "## [1.1.0] - 2026-11-01\n",
    ),
    "deprecated since a version": changelog(
        "## [1.2.0] - 2026-11-24\n\n### Deprecated\n\n- `--old`, since 1.2.0.\n",
        "## [1.1.0] - 2026-11-01\n",
    ),
    "deprecation ending in the past": changelog(
        "## [1.2.0] - 2026-11-24\n\n### Deprecated\n\n- `--old`; removed in 1.1.0.\n",
        "## [1.1.0] - 2026-11-01\n",
    ),
}
R4_PASSES = {
    "no release yet": changelog(),
    "removal in a MAJOR": changelog(
        "## [2.0.0] - 2026-11-24\n\n" + REMOVED, "## [1.2.0] - 2026-11-01\n"
    ),
    "removal in a MINOR before 1.0": changelog(
        "## [0.5.0] - 2026-11-24\n\n" + REMOVED, "## [0.4.0] - 2026-11-01\n"
    ),
    "deprecation naming its end": changelog(
        "## [1.2.0] - 2026-11-24\n\n### Deprecated\n\n- `--old`; removed in 2.0.0.\n",
        "## [1.1.0] - 2026-11-01\n",
    ),
    "an old unannounced removal is history": changelog(
        "## [1.3.0] - 2026-11-24\n\n### Added\n\n- x\n",
        "## [1.2.1] - 2026-11-01\n\n" + REMOVED,
        "## [1.2.0] - 2026-10-01\n",
    ),
}


def test_r4_release() -> None:
    for name, text in R4_FAILS.items():
        got = findings(mcp_server, write("CHANGELOG.md", text))
        assert any(f.startswith("R4:") for f in got), (name, got)
    for name, text in R4_PASSES.items():
        got = [f for f in findings(mcp_server, write("CHANGELOG.md", text)) if f.startswith("R4:")]
        assert got == [], (name, got)


def test_compliant_is_clean() -> None:
    assert findings(mcp_server) == [], findings(mcp_server)


def test_each_break_is_caught() -> None:
    for name, breaks, principle in FAILS:
        got = findings(mcp_server, *breaks)
        assert any(f.startswith(f"{principle}:") for f in got), (name, got)


# S1: the lock exported in one stage, carried to another, installed relative to WORKDIR.
MULTI_STAGE = """FROM quay.io/hummingbird/python AS lock
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev -o /requirements.txt
FROM quay.io/hummingbird/python
LABEL version="1.2.0"
COPY --from=lock /requirements.txt /tmp/
WORKDIR /tmp
RUN pip install --no-deps -r requirements.txt
"""


def test_s1_the_install_itself_is_locked() -> None:
    fails = [
        # Copying the lock in is no proof: the install reads a file nobody exported.
        (
            "COPY uv.lock, install another file",
            'FROM x\nLABEL version="1.2.0"\nCOPY uv.lock requirements.txt ./\n'
            "RUN pip install --no-deps -r requirements.txt\n",
        ),
        (
            "exported without --frozen",
            'FROM x\nLABEL version="1.2.0"\nRUN uv export -o /r.txt\n'
            "RUN pip install --no-deps -r /r.txt\n",
        ),
        ("uv sync re-locks", 'FROM x\nLABEL version="1.2.0"\nRUN uv sync --no-dev\n'),
    ]
    for name, containerfile in fails:
        got = findings(mcp_server, write("Containerfile", containerfile))
        assert any(f.startswith("S1:") for f in got), (name, got)
    passes = [
        ("exported in one stage, installed in the next", MULTI_STAGE),
        ("uv sync --frozen", 'FROM x\nLABEL version="1.2.0"\nRUN uv sync --frozen --no-dev\n'),
        (
            "a hash-pinned requirements file is a lock",
            'FROM x\nLABEL version="1.2.0"\nCOPY req.txt /r.txt\n'
            "RUN pip install --require-hashes --no-deps -r /r.txt\n",
        ),
    ]
    for name, containerfile in passes:
        got = findings(mcp_server, write("Containerfile", containerfile))
        assert got == [], (name, got)


def test_s1_names_the_line_not_the_command() -> None:
    # The command may carry an index credential; the finding must not repeat it.
    got = findings(
        mcp_server,
        write(
            "Containerfile",
            'FROM x\nLABEL version="1.2.0"\n\n'
            "RUN pip install --index-url https://u:s3cr3tvalue@pypi.example.test/simple .\n",
        ),
    )
    s1 = [f for f in got if f.startswith("S1:")]
    assert s1 and all("s3cr3tvalue" not in f for f in s1), s1
    assert any("Containerfile:4 " in f for f in s1), s1


def test_s1_lock_committed_but_never_used() -> None:
    # Every install takes --no-deps, yet none reads the lock: only the file-level finding.
    unused = 'FROM x\nLABEL version="1.2.0"\nCOPY uv.lock ./\nRUN pip install --no-deps .\n'
    got = findings(mcp_server, write("Containerfile", unused))
    assert got == [
        "S1: Containerfile resolves Python dependencies but never installs from uv.lock "
        "(`uv export --frozen` to a file it installs, or `uv sync --frozen`)"
    ], got
    # With no lock committed there is nothing to install from (S1 still asks for one).
    got = findings(mcp_server, write("Containerfile", unused), drop("uv.lock"))
    assert not any("never installs" in f for f in got), got


def link(name, outside: Path, text: str):
    """Commit `name` as a symlink to a file outside the repo holding `text`."""

    def go(root: Path) -> None:
        target = outside / name.replace("/", "_")
        target.write_text(text)
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).unlink(missing_ok=True)
        (root / name).symlink_to(target)

    return go


def test_symlinked_files_are_not_read() -> None:
    # is_file() follows a link, so a committed symlink would be judged by a file
    # outside the repo. Each case fails as a real file and is ignored as a link.
    unlocked = 'FROM x\nLABEL version="1.2.0"\nRUN pip install .\n'
    cooldown = '{"minimumReleaseAge": "1 day"}'
    leak = '---\nname: leak\n---\npassword = "hunter2"\n'
    cases = [
        ("S1", mcp_server, "Containerfile", unlocked),
        ("S2", mcp_server, "renovate.json", cooldown),
        ("S2", mcp_server, ".github/renovate.json", cooldown),
        ("K11", skill, "skills/leak/SKILL.md", leak),
    ]
    for principle, setup, name, text in cases:
        assert any(f.startswith(f"{principle}:") for f in findings(setup, write(name, text))), name
        with tempfile.TemporaryDirectory() as outside:
            got = findings(setup, link(name, Path(outside), text))
        assert not any(f.startswith(f"{principle}:") for f in got), (name, got)
    # A path through a symlinked directory leaves the repo just the same.
    with tempfile.TemporaryDirectory() as outside:
        (Path(outside) / "renovate.json").write_text(cooldown)
        got = findings(mcp_server, lambda root: (root / ".github").symlink_to(outside))
    assert not any(f.startswith("S2:") for f in got), got
    # A symlinked uv.lock is no committed lock, so an install that skips it is fine.
    with tempfile.TemporaryDirectory() as outside:
        got = findings(
            mcp_server,
            link("uv.lock", Path(outside), "version = 1\n"),
            write("Containerfile", 'FROM x\nLABEL version="1.2.0"\nRUN pip install --no-deps .\n'),
        )
    assert not any(f.startswith("S1:") for f in got), got


def test_s2_unreadable_package_json() -> None:
    got = findings(mcp_server, write("package.json", '{"renovate": {'))
    assert any(f.startswith("S2:") and "package.json" in f for f in got), got


def test_new_checks_pass_compliant_variants() -> None:
    # Each passes the compliant repo with the rule's subject present and done right.
    ok = [
        ("locked install", write("Containerfile", LOCKED_INSTALL)),
        (
            "a named pin resolves nothing",
            write("Containerfile", LOCKED_INSTALL + "RUN pip install uv==0.9.0\n"),
        ),
        (
            "renovate config without the cooldown",
            write("renovate.json", '{"extends": ["config:recommended"], "labels": ["deps"]}'),
        ),
        (
            "package.json without renovate",
            write("package.json", '{"name": "x", "minimumReleaseAge": 1}'),
        ),
        ("no HTTP client at all", write("src/x/client.py", "import json\n")),
    ]
    for name, change in ok:
        got = findings(mcp_server, change)
        assert got == [], (name, got)
    # K1 is the MCP server's: a forked server's unbounded client is not judged by it.
    got = findings(
        mcp_server,
        kinds('["forked-mcp-server"]'),
        write("src/x/client.py", "import httpx\n"),
        write("tests/test_tools.py", "x = 1\n"),
    )
    assert got == [], got


def skill(root: Path) -> None:
    """A compliant claude-skill repo."""
    (root / "LICENSE").write_text(AGPL)
    (root / "CHANGELOG.md").write_text(CHANGELOG)
    (root / "sdlc.toml").write_text('[tool.sdlc]\nrepo-kinds = ["claude-skill"]\n')
    write(
        "skills/review/SKILL.md",
        "---\nname: review\ndescription: x\n---\n\n## Phase 1\n\n"
        'GITHUB_TOKEN="$(gh auth token)" gh pr view\n',
    )(root)
    # A checker's broken fixture under tests/ is not a skill this repo ships.
    write("tests/broken/SKILL.md", 'api_key = "not-a-real-key"\n')(root)


def test_skill() -> None:
    assert findings(skill) == [], findings(skill)
    for leak in ('password = "hunter2"', "TOKEN=ghp_abc123def456", "api_key: abc123"):
        got = findings(skill, write("skills/leak/SKILL.md", f"---\nname: leak\n---\n{leak}\n"))
        assert any(f.startswith("K11:") for f in got), (leak, got)
    # Expansions, placeholders and prose name a credential without holding one.
    fine = ("TOKEN=$GH_TOKEN", "api_key: <your key>", "token: {{ vault }}", "token: the one gh has")
    for line in fine:
        got = findings(skill, write("skills/fine/SKILL.md", f"---\nname: fine\n---\n{line}\n"))
        assert got == [], (line, got)
    # A vendored tree is not the repo's own source, and is never walked.
    for vendored in ("node_modules", ".venv"):
        got = findings(skill, write(f"{vendored}/x/SKILL.md", 'password = "hunter2"\n'))
        assert got == [], (vendored, got)
    # A vendor token or a private key is caught by its shape, whatever holds it.
    shapes = (
        "export GH=ghp_" + "a" * 36,
        "use github_pat_" + "A1" * 20,
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "key AKIA" + "ABCDEFGHIJKLMNOP",
    )
    for leak in shapes:
        got = findings(skill, write("skills/leak/SKILL.md", f"---\nname: leak\n---\n{leak}\n"))
        assert any("vendor credential" in f for f in got), (leak, got)
    # The credential rule is K11's: the same file in an mcp-server repo is P3's scan, not this.
    assert findings(mcp_server, write("skills/leak/SKILL.md", 'password = "hunter2"\n')) == []


def test_kinds_relax() -> None:
    # A forked server keeps the upstream license; a web app's tags are deploy markers.
    assert findings(mcp_server, kinds('["forked-mcp-server"]'), drop("LICENSE")) == []
    assert (
        findings(
            mcp_server,
            kinds('["web-application"]'),
            write("Containerfile", 'FROM x\nLABEL version="0.0.9"\n'),
        )
        == []
    )


def test_sdlc_toml_without_pyproject() -> None:
    got = findings(
        mcp_server,
        drop("pyproject.toml"),
        write("sdlc.toml", '[tool.sdlc]\nrepo-kinds = ["container-image"]\n'),
    )
    assert got == [], got


def test_v1_repo_is_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mcp_server(root)
        write(".specify/memory/constitution.md", "# x\n")(root)
        try:
            c.check(root)
        except c.CannotRunError:
            return
        raise AssertionError("a repo with a v1 manifest must not be judged by v2")


def test_bad_kinds_type_cannot_run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mcp_server(root)
        kinds('"mcp-server"')(root)
        assert c.main(["check", str(root)]) == c.CANNOT_RUN


def test_exit_codes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mcp_server(root)
        with redirect_stdout(io.StringIO()):
            assert c.main(["check", str(root)]) == c.CLEAN
            drop("LICENSE")(root)
            assert c.main(["check", str(root)]) == c.FOUND


def test_readme_section() -> None:
    readme = (
        "# Tool\n\nIntro.\n\n## What's different here\n\nThe cache is never shared.\n\n"
        "### Detail\n\nStill in.\n\n## Development\n\nOut.\n"
    )
    section = c.readme_section(readme)
    assert section.startswith("## What's different here"), section
    assert "Still in." in section and "Out." not in section, section
    assert c.readme_section("# Tool\n\n## Usage\n") == ""


def test_context() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        text = Path(tmp) / "constitution.md"
        text.write_text("# Crunchtools Constitution\n\n**C1. ...**\n")
        body = c.context("## What's different here\n\nNo cache.\n", text)
        assert body.startswith("# Crunchtools Constitution"), body
        assert body.rstrip().endswith("No cache."), body
        assert c.context("# no section\n", text).count("This repository") == 0
        try:
            c.context("", Path(tmp) / "missing.md")
        except c.CannotRunError:
            return
        raise AssertionError("a missing constitution text must not pass as empty")


def test_gatehouse_command() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root, out, text = Path(tmp) / "repo", Path(tmp) / "ctx.md", Path(tmp) / "c.md"
        root.mkdir()
        text.write_text("# Crunchtools Constitution\n")
        (root / "README.md").write_text("## What's different here\n\nNo cache.\n")
        saved, c.TEXT = c.TEXT, text
        try:
            assert c.gatehouse(["--stdin"], root, out) == [
                "gatehouse",
                "--constitution",
                str(out),
                "--stdin",
            ]
            assert "No cache." in out.read_text()
            write(".specify/memory/constitution.md", "# v1\n")(root)
            assert c.gatehouse(["--stdin"], root, out) == ["gatehouse", "--stdin"]
        finally:
            c.TEXT = saved


def load_sdlc():
    """The `sdlc` script, loaded by path (no .py extension); nothing runs on import."""
    path = Path(__file__).resolve().parent.parent / "sdlc"
    spec = importlib.util.spec_from_loader(
        "sdlc_routes", importlib.machinery.SourceFileLoader("sdlc_routes", str(path))
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECKER_EXIT = 7  # any code the checker returns is passed through unchanged


def test_t_constitution_runs_the_checker() -> None:
    sdlc = load_sdlc()
    ran: list[list[str]] = []

    def fake_run(cmd, **_kw):
        ran.append(cmd)
        return (CHECKER_EXIT, "")

    sdlc.run, sdlc.project_env = fake_run, dict
    with tempfile.TemporaryDirectory() as tmp:
        sdlc.REPO = Path(tmp)
        assert sdlc.t_constitution({}) == CHECKER_EXIT, "the checker's exit code is the check's"
        assert ran[-1] == ["sdlc-constitution", "check", tmp], ran


def test_coverage_lists_every_principle() -> None:
    expected = (
        [f"I{n}" for n in range(1, 4)]
        + [f"C{n}" for n in range(1, 9)]
        + [f"R{n}" for n in range(1, 6)]
        + [f"S{n}" for n in range(1, 7)]
        + [f"P{n}" for n in range(1, 6)]
        + [f"O{n}" for n in range(1, 8)]
        + [f"A{n}" for n in range(1, 5)]
        + [f"D{n}" for n in range(1, 4)]
        + [f"K{n}" for n in range(1, 16)]
        + [f"G{n}" for n in range(1, 6)]
    )
    assert sorted(c.COVERAGE) == sorted(expected), set(c.COVERAGE) ^ set(expected)
    assert set(c.COVERAGE.values()) <= {"rule", "review", "none"}


def test_every_kind_has_its_own_id() -> None:
    ids = [k.id for k in c.KINDS.values()]
    assert sorted(ids, key=lambda i: int(i[1:])) == [f"K{n}" for n in range(1, 16)], ids


def test_k11_shapes_match_the_secret_scan() -> None:
    script = (Path(__file__).resolve().parent.parent / "bin" / "sdlc-secret-scan").read_text()
    shapes = re.search(r"^shapes='([^']+)'", script, re.MULTILINE)
    assert shapes, "bin/sdlc-secret-scan no longer sets shapes="
    ours = c.rules._SECRET_SHAPE.pattern
    assert ours.startswith(shapes.group(1) + "|"), (shapes.group(1), ours)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_s1_lock_must_be_tracked() -> None:
    install = write("Containerfile", LOCKED_INSTALL)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mcp_server(root)
        install(root)
        _git(root, "init", "-q")
        _git(root, "add", "--", ".", ":!uv.lock")
        got = c.check(root).findings
        assert "S1: `uv.lock` is not committed: git does not track it" in got, got
        _git(root, "add", "uv.lock")
        assert c.check(root).findings == [], c.check(root).findings


def test_k1_time_bounds() -> None:
    no_timeout = CLIENT.replace("import httpx", "import requests\nimport httpx")
    timed = no_timeout + "\n\ndef get(u):\n    return requests.get(u, timeout=10)\n"
    off = CLIENT + "\n\nCLIENT = httpx.AsyncClient(timeout=None)\n"
    cases = [(no_timeout, "wait forever"), (off, "turns its HTTP timeout off"), (timed, None)]
    for source, finding in cases:
        got = findings(mcp_server, write("src/x/client.py", source))
        if finding is None:
            assert got == [], got
        else:
            assert any(f.startswith("K1:") and finding in f for f in got), (finding, got)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
