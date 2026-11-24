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
    (root / "tests" / "test_tools.py").write_text("READ_ONLY = 'readOnlyHint'\n")


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


def test_compliant_is_clean() -> None:
    assert findings(mcp_server) == [], findings(mcp_server)


def test_each_break_is_caught() -> None:
    for name, breaks, principle in FAILS:
        got = findings(mcp_server, *breaks)
        assert any(f.startswith(f"{principle}:") for f in got), (name, got)


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


def test_t_constitution_routes_by_manifest() -> None:
    sdlc = load_sdlc()
    ran: list[list[str]] = []

    def fake_run(cmd, **_kw):
        ran.append(cmd)
        return (7, "")

    sdlc.run, sdlc.project_env = fake_run, dict
    with tempfile.TemporaryDirectory() as tmp:
        sdlc.REPO = Path(tmp)
        assert sdlc.t_constitution({}) == 7, "the checker's exit code is the check's"
        assert ran[-1] == ["sdlc-constitution", "check", tmp], ran
        write(".specify/memory/constitution.md", "# v1\n")(Path(tmp))
        assert sdlc.t_constitution({}) == 7
        assert ran[-1] == ["sdlc-validate", ".specify/memory/constitution.md"], ran


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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
