#!/usr/bin/env python3
"""scripts/update_tools.py moves the pins, and never a version without its digest.

Every network call is mocked: the lookups are module functions, replaced here.

  - gourmand and gatehouse still move by tag, two ARG copies each
  - markdownlint-cli2 moves by the npm registry, one ARG copy
  - lychee moves its two ARG copies AND the @sha256: of its FROM line
  - a lychee version with no digest (or a malformed one) raises and writes nothing
  - a run with every pin current writes nothing
  - the image PATCH version, LABEL, pyproject and CHANGELOG move with any bump

Run: python3 tests/test_update_tools.py
"""

# ruff: noqa: PLR2004, ARG005, PLW0603
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("update_tools", REPO / "scripts" / "update_tools.py")
assert SPEC and SPEC.loader
ut = importlib.util.module_from_spec(SPEC)
sys.modules["update_tools"] = ut
SPEC.loader.exec_module(ut)
# The real one: later checks stub ut.http_json, and the curl checks need it.
REAL_HTTP_JSON = ut.http_json

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64

CONTAINERFILE = f"""ARG GOURMAND_VERSION=1.0.0
ARG GATEHOUSE_VERSION=2.0.0
ARG LYCHEE_VERSION=0.24.2
FROM docker.io/lycheeverse/lychee:${{LYCHEE_VERSION}}@{OLD} AS lychee
FROM base
ARG GOURMAND_VERSION=1.0.0
ARG GATEHOUSE_VERSION=2.0.0
ARG LYCHEE_VERSION=0.24.2
ARG MARKDOWNLINT_CLI2_VERSION=0.24.0
LABEL version="3.4.5"
"""

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"{'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures += 1


def raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    return False


def run(versions: dict[str, str], digest=None) -> tuple[int, dict[str, str]]:
    """main() in a scratch copy of the repo; returns the exit code and the files."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "Containerfile").write_text(CONTAINERFILE)
        (root / "IMAGE_TAG").write_text("3.4.5\n")
        (root / "pyproject.toml").write_text('[project]\nversion = "3.4.5"\n')
        (root / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n### Added\n\n- x\n")
        saved = (ut.ROOT, ut.latest_release, ut.latest_npm, ut.latest_lychee, ut.lychee_digest)
        ut.ROOT = root
        ut.latest_release = lambda repo: versions[repo.split("/")[1]]
        ut.latest_npm = lambda _pkg: versions["markdownlint-cli2"]
        ut.latest_lychee = lambda: versions["lychee"]
        ut.lychee_digest = digest or (lambda _v: NEW)
        # gourmand, gatehouse and markdownlint look the module functions up at
        # call time; lychee holds the functions themselves, so rebuild it.
        tools = ut.TOOLS
        ut.TOOLS = [
            *tools[:3],
            ut.Tool("LYCHEE_VERSION", "lychee", ut.latest_lychee, ut.lychee_digest),
        ]
        sys.argv = ["update_tools.py"]
        os.environ.pop("GITHUB_OUTPUT", None)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = ut.main()
            files = {p.name: p.read_text() for p in root.iterdir()}
        finally:
            ut.ROOT, ut.latest_release, ut.latest_npm, ut.latest_lychee, ut.lychee_digest = saved
            ut.TOOLS = tools
        return code, files


CURRENT = {
    "gourmand": "1.0.0",
    "gatehouse": "2.0.0",
    "markdownlint-cli2": "0.24.0",
    "lychee": "0.24.2",
}

# Nothing newer: nothing written.
_, files = run(CURRENT)
check("current pins write nothing", files["Containerfile"] == CONTAINERFILE)
check("current pins keep IMAGE_TAG", files["IMAGE_TAG"] == "3.4.5\n")

# gourmand only: both ARG copies, PATCH bump, CHANGELOG.
_, files = run({**CURRENT, "gourmand": "1.1.0"})
check(
    "gourmand moves both ARG copies",
    files["Containerfile"].count("ARG GOURMAND_VERSION=1.1.0") == 2,
)
check(
    "gourmand leaves lychee digest",
    OLD in files["Containerfile"] and NEW not in files["Containerfile"],
)
check("gourmand bumps IMAGE_TAG", files["IMAGE_TAG"] == "3.4.6\n")
check("gourmand bumps LABEL", 'LABEL version="3.4.6"' in files["Containerfile"])
check("gourmand bumps pyproject", 'version = "3.4.6"' in files["pyproject.toml"])
check("gourmand in CHANGELOG", "- gourmand 1.0.0 -> 1.1.0." in files["CHANGELOG.md"])

# markdownlint-cli2: one ARG copy.
_, files = run({**CURRENT, "markdownlint-cli2": "0.25.0"})
check(
    "markdownlint moves its ARG", "ARG MARKDOWNLINT_CLI2_VERSION=0.25.0\n" in files["Containerfile"]
)
check("markdownlint in CHANGELOG", "- markdownlint-cli2 0.24.0 -> 0.25.0." in files["CHANGELOG.md"])

# lychee: ARGs and the digest.
asked: list[str] = []


def digest(version: str) -> str:
    asked.append(version)
    return NEW


_, files = run({**CURRENT, "lychee": "0.25.0"}, digest)
cf = files["Containerfile"]
check("lychee moves both ARG copies", cf.count("ARG LYCHEE_VERSION=0.25.0") == 2)
check(
    "lychee moves the FROM digest", f"${{LYCHEE_VERSION}}@{NEW} AS lychee" in cf and OLD not in cf
)
check("lychee digest asked for the new version", asked == ["0.25.0"], str(asked))
check("lychee in CHANGELOG", "- lychee 0.24.2 -> 0.25.0." in files["CHANGELOG.md"])

# Everything at once.
_, files = run(
    {"gourmand": "1.0.1", "gatehouse": "2.1.0", "markdownlint-cli2": "0.24.1", "lychee": "0.24.3"}
)
check("all four move in one entry", files["CHANGELOG.md"].count("->") == 4)
check("all four bump PATCH once", files["IMAGE_TAG"] == "3.4.6\n")


# A missing or malformed digest raises.
for label, payload in (
    ("missing", {}),
    ("malformed", {"digest": "sha256:abc"}),
    ("null", {"digest": None}),
):
    saved_http = ut.http_json
    ut.http_json = lambda _url, p=payload: p
    check(f"{label} digest raises", raises(lambda: ut.lychee_digest("0.25.0")))
    ut.http_json = saved_http

ut.http_json = lambda url: (
    {"digest": NEW}
    if url == "https://hub.docker.com/v2/repositories/lycheeverse/lychee/tags/0.25.0"
    else {}
)
check("digest is read from the tag's Docker Hub record", ut.lychee_digest("0.25.0") == NEW)

ut.http_json = lambda _url: {}
check(
    "a failed digest lookup aborts the run",
    raises(lambda: run({**CURRENT, "lychee": "0.25.0"}, ut.lychee_digest)),
)

# Version discovery.
ut.http_json = lambda url: {"dist-tags": {"latest": "0.25.1"}}
check("npm latest", ut.latest_npm("markdownlint-cli2") == "0.25.1")
ut.http_json = lambda url: {"dist-tags": {"latest": "0.25.0-beta.1"}}
check("npm prerelease refused", raises(lambda: ut.latest_npm("markdownlint-cli2")))
ut.gh_api = lambda path, *extra: "lychee-v0.24.9\n"
check("lychee tag prefix stripped", ut.latest_lychee() == "0.24.9")
ut.gh_api = lambda path, *extra: "v1.2.3 v1.10.0 v1.9.9 nightly\n"
check("repo tags sorted numerically", ut.latest_release("crunchtools/gourmand") == "1.10.0")

# The real Containerfile still matches what the script rewrites.
# ---------------------------------------------------- http_json, through curl


class FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def curl_answers(proc: FakeProc, url: str = "https://registry.npmjs.org/x"):
    """(what http_json returned or the error text, the argv curl was given)."""
    seen: list[list[str]] = []

    def fake_run(argv, **_kw):
        seen.append(argv)
        return proc

    saved = ut.subprocess.run
    ut.subprocess.run = fake_run
    try:
        return REAL_HTTP_JSON(url), seen[0]
    except ValueError as exc:
        return str(exc), seen[0] if seen else []
    finally:
        ut.subprocess.run = saved


got, argv = curl_answers(FakeProc(0, '{"dist-tags": {"latest": "1.2.3"}}'))
check("http_json parses curl's JSON", got == {"dist-tags": {"latest": "1.2.3"}}, str(got))
check(
    "http_json runs curl https-only, failing on an HTTP error, with a timeout",
    argv[:2] == ["curl", "-fsSL"]
    and argv[argv.index("--proto") + 1] == "=https"
    and argv[argv.index("--max-time") + 1] == str(ut.HTTP_TIMEOUT)
    and argv[-1] == "https://registry.npmjs.org/x",
    str(argv),
)
got, _ = curl_answers(FakeProc(22, "", "curl: (22) The requested URL returned error: 404"))
check(
    "a curl failure raises with curl's own stderr in it",
    isinstance(got, str) and "exit 22" in got and "returned error: 404" in got,
    str(got),
)
got, _ = curl_answers(FakeProc(0, "<html>rate limited</html>"))
check(
    "a non-JSON answer raises, naming the URL",
    isinstance(got, str) and "did not answer JSON" in got,
    str(got),
)

real = (REPO / "Containerfile").read_text()
check("real Containerfile: one lychee FROM", len(ut.LYCHEE_FROM.findall(real)) == 1)
check(
    "real Containerfile: lychee ARG x2",
    real.count(f"ARG LYCHEE_VERSION={ut.pinned(real, 'LYCHEE_VERSION')}\n") == 2,
)
check("real Containerfile: markdownlint ARG x1", real.count("ARG MARKDOWNLINT_CLI2_VERSION=") == 1)

shutil.rmtree(REPO / "scripts" / "__pycache__", ignore_errors=True)
print(f"{failures} failure(s)")
if __name__ == "__main__":
    sys.exit(1 if failures else 0)
