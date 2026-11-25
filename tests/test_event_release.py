#!/usr/bin/env python3
"""The `write` job's release halves: open the PR, verify and tag a merge, the audit issue.

A release is real only if the API says so, so each of the seven tests that
make a merge one is pinned alone, as are pyproject.toml absent versus
mismatched, the credentials a child may hold, and the nonzero exits.
"""

from __future__ import annotations

import base64
import contextlib
import io
import sys
import tempfile
from pathlib import Path
from unittest import mock

import sdlc_root
from fake_github import FakeGitHub, FakeRunner, check, finish

from sdlc_jobs import _release as launchers
from sdlc_jobs import _tools as tools
from sdlc_jobs._github import APP_LOGIN, ApiError, CannotRunError
from sdlc_jobs._release import audit_issue, bump_lock, open_release_pr, publish_release
from sdlc_jobs._tools import Done, Site

# The module attributes the image sets; these suites only read them off argv.
ASHIGARU = sdlc_root.ROOT / "ashigaru-scripts"
CONSTITUTION = sdlc_root.ROOT / "constitution-scripts"
tools.ASHIGARU_SCRIPTS = ASHIGARU
tools.CONSTITUTION_SCRIPTS = CONSTITUTION

REPO, SHA, TOKEN = "crunchtools/demo", "a" * 40, "app-token-123"
ENV = {"PATH": "/bin", "ASHIGARU_APP_KEY": "SECRET-KEY", "GITHUB_TOKEN": "gh-tok"}
PLAN = {"version": "1.2.3", "publish": "pypi"}


def pull(**over: object) -> dict:
    base = {
        "number": 7,
        "merged_at": "2026-01-01T00:00:00Z",
        "merge_commit_sha": SHA,
        "user": {"login": APP_LOGIN},
        "base": {"ref": "main"},
        "head": {"ref": "ashigaru/release-1.2.3", "repo": {"full_name": REPO}},
    }
    base.update(over)
    return base


def toml(version: str) -> dict:
    return {"content": base64.b64encode(f'[project]\nversion = "{version}"\n'.encode()).decode()}


def gh(pulls: list, pyproject: object = None, package: object = None) -> FakeGitHub:
    routes: dict = {("GET", f"/repos/{REPO}/commits/{SHA}/pulls"): pulls}
    if pyproject is not None:
        routes[("GET", f"/repos/{REPO}/contents/pyproject.toml")] = pyproject
    if package is not None:
        routes[("GET", f"/repos/{REPO}/contents/package.json")] = package
    return FakeGitHub(routes)


def npm(version: str) -> dict:
    return {"content": base64.b64encode(f'{{"version": "{version}"}}'.encode()).decode()}


TOOLS_ONLY = {"content": base64.b64encode(b"[tool.sdlc]\n").decode()}


def site_for(api: FakeGitHub, runner: FakeRunner | None = None) -> Site:
    return Site(REPO, ENV, runner or FakeRunner(), sdlc_root.ROOT, api_for=lambda _token: api)


def publish(api: FakeGitHub, runner: FakeRunner | None = None, plan: dict = PLAN):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        return publish_release(site_for(api, runner), TOKEN, plan, SHA, "main")


def refused(name: str, api: FakeGitHub, plan: dict = PLAN) -> None:
    runner = FakeRunner()
    got = publish(api, runner, plan)
    check(f"refuse: {name}", got == (1, None) and not runner.calls, str(got))


# publish_release: every refusal, alone
refused("no pull request", gh([]))
refused("two pull requests", gh([pull(), pull(number=8)]))
refused("not merged", gh([pull(merged_at=None)]))
refused("merge commit differs", gh([pull(merge_commit_sha="b" * 40)]))
refused("author is a person", gh([pull(user={"login": "scott"})]))
refused("author is github-actions", gh([pull(user={"login": "github-actions[bot]"})]))
refused("base is not the default branch", gh([pull(base={"ref": "dev"})]))
refused(
    "head is a fork",
    gh([pull(head={"ref": "ashigaru/release-1.2.3", "repo": {"full_name": "x/demo"}})]),
)
refused("head repo deleted", gh([pull(head={"ref": "ashigaru/release-1.2.3", "repo": None})]))
refused(
    "head ref is another branch", gh([pull(head={"ref": "feat/x", "repo": {"full_name": REPO}})])
)
refused(
    "head version not X.Y.Z",
    gh([pull(head={"ref": "ashigaru/release-1.2", "repo": {"full_name": REPO}})]),
)
refused(
    "head version has a suffix",
    gh([pull(head={"ref": "ashigaru/release-1.2.3-rc1", "repo": {"full_name": REPO}})]),
)
refused("plan names another version", gh([pull()]), {"version": "1.2.4"})
refused("pyproject mismatched", gh([pull()], toml("1.2.2")))
refused(
    "pyproject has no version", gh([pull()], {"content": base64.b64encode(b"[tool.x]\n").decode()})
)
refused("tools-only pyproject, package.json mismatched", gh([pull()], TOOLS_ONLY, npm("1.2.2")))
refused(
    "[project] without a version, even beside package.json",
    gh([pull()], {"content": base64.b64encode(b"[project]\nname = 'x'\n").decode()}, npm("1.2.3")),
)


# a dynamic version, read from [tool.hatch.version].path (ashigaru's VERSION)
def hatch(path: str) -> dict:
    text = f'[project]\nname = "x"\ndynamic = ["version"]\n[tool.hatch.version]\npath = "{path}"\n'
    return {"content": base64.b64encode(text.encode()).decode()}


def text(body: str) -> dict:
    return {"content": base64.b64encode(body.encode()).decode()}


def hatch_gh(path: str, files: dict) -> FakeGitHub:
    api = gh([pull()], hatch(path))
    for name, body in files.items():
        api.routes[("GET", f"/repos/{REPO}/contents/{name}")] = text(body)
    return api


refused("hatch path file missing", hatch_gh("VERSION", {}))
refused("hatch path escapes the repo", hatch_gh("../VERSION", {"../VERSION": "1.2.3\n"}))
refused("hatch path is absolute", hatch_gh("/etc/VERSION", {"/etc/VERSION": "1.2.3\n"}))
refused("hatch path file is not a version", hatch_gh("VERSION", {"VERSION": "1.2.3-dev\n"}))
refused("hatch path file mismatched", hatch_gh("VERSION", {"VERSION": "1.2.2\n"}))
refused(
    "dynamic version without a hatch path",
    gh([pull()], text('[project]\nname = "x"\ndynamic = ["version"]\n')),
)

# success, with pyproject absent (404) and matching
for label, api in (
    ("pyproject absent", gh([pull()])),
    ("pyproject matches", gh([pull()], toml("1.2.3"))),
    ("tools-only pyproject, package.json matches (rotv)", gh([pull()], TOOLS_ONLY, npm("1.2.3"))),
    ("no pyproject, package.json matches", gh([pull()], None, npm("1.2.3"))),
    (
        "dynamic version read from the hatch path (ashigaru)",
        hatch_gh("VERSION", {"VERSION": "1.2.3\n"}),
    ),
    ("hatch path in a subdirectory", hatch_gh("src/x/VERSION", {"src/x/VERSION": " 1.2.3 \n"})),
):
    runner = FakeRunner()
    code, release = publish(api, runner)
    check(
        f"success: {label}",
        code == 0 and release == {"version": "1.2.3", "sha": SHA, "pr": 7, "publish": "pypi"},
        str(release),
    )

runner = FakeRunner()
publish(gh([pull()]), runner)
argv, env, _, _ = runner.calls[0]
check(
    "publish argv carries repo, version, sha",
    argv[-3:] == [REPO, "1.2.3", SHA] and argv[:3] == ["python3", "-I", "-c"],
    str(argv[:3]),
)
check("publish code has no interpolated values", REPO not in argv[3] and SHA not in argv[3])
check("publish names Ashigaru's scripts from the module attribute", argv[4] == str(ASHIGARU))

check("publish env has GH_TOKEN", env.get("GH_TOKEN") == TOKEN)
check(
    "publish env has no other credential",
    not {"ASHIGARU_APP_KEY", "GITHUB_TOKEN"} & set(env),
    str(sorted(env)),
)
check(
    "publish with no plan.publish yields empty",
    publish(gh([pull()]), FakeRunner(), {"version": "1.2.3"})[1]["publish"] == "",
)

# the child's verdict
check(
    "CHANGELOG mismatch (exit 1) is a refusal",
    publish(gh([pull()]), FakeRunner([(["-I"], Done(1, "CHANGELOG", ""))])) == (1, None),
)
for rc in (2, 3, 127):
    try:
        publish(gh([pull()]), FakeRunner([(["-I"], Done(rc, "", "boom"))]))
        check(f"exit {rc} cannot run", False)
    except CannotRunError:
        check(f"exit {rc} cannot run", True)
try:
    publish(
        FakeGitHub({("GET", f"/repos/{REPO}/commits/{SHA}/pulls"): ApiError("GET", "x", 500, "no")})
    )
    check("API failure cannot run", False)
except CannotRunError:
    check("API failure cannot run", True)


# open_release_pr
def opener(branch: bool, open_prs: list, source: bool = True) -> FakeGitHub:
    routes: dict = {
        (
            "GET",
            f"/repos/{REPO}/pulls?head=crunchtools:ashigaru/release-1.2.3&state=open",
        ): open_prs,
        ("POST", f"/repos/{REPO}/pulls"): {"html_url": "https://x/pull/9"},
        ("POST", f"/repos/{REPO}/git/refs"): {},
        ("PATCH", f"/repos/{REPO}/git/refs/heads/ashigaru/release-1.2.3"): {},
    }
    if source:
        routes[("GET", f"/repos/{REPO}/git/ref/heads/chore/release-1.2.3")] = {
            "object": {"sha": "c" * 40}
        }
    if branch:
        routes[("GET", f"/repos/{REPO}/git/ref/heads/ashigaru/release-1.2.3")] = {
            "object": {"sha": "d" * 40}
        }
    return FakeGitHub(routes)


def opened(api: FakeGitHub, version: str = "1.2.3") -> int:
    with contextlib.redirect_stdout(io.StringIO()):
        return open_release_pr(api, REPO, version, "main")


api = opener(False, [])
check(
    "open: creates branch and PR",
    opened(api) == 0
    and api.made("POST", "*/git/refs")[0][2]
    == {"ref": "refs/heads/ashigaru/release-1.2.3", "sha": "c" * 40}
    and not api.made("PATCH"),
)
pr_body = api.made("POST", "*/pulls")[0][2]
check(
    "open: PR fields",
    pr_body["base"] == "main"
    and pr_body["head"] == "ashigaru/release-1.2.3"
    and pr_body["title"] == "chore: release 1.2.3"
    and "write" in pr_body["body"],
    str(pr_body),
)
api = opener(True, [])
check(
    "open: existing branch is force-moved",
    opened(api) == 0
    and api.made("PATCH")[0][2] == {"sha": "c" * 40, "force": True}
    and not api.made("POST", "*/git/refs"),
)
api = opener(True, [{"number": 9, "html_url": "https://x/pull/9"}])
check("open: already open opens nothing", opened(api) == 0 and not api.made("POST", "*/pulls"))
api = opener(False, [], source=False)
check("open: missing branch refused", opened(api) == 1 and not api.made("POST"))
check(
    "open: bad version refused",
    opened(opener(False, []), "1.2") == 1 and opened(opener(False, []), "1.2.3-rc1") == 1,
)


# uv.lock: the root package moves with the release (#187)
# gatehouse's shape: the distribution (gatehouse-crunchtools) is not the repo's
# name (demo), and a dependency locked at the same version must stay put.
GATEHOUSE_LOCK = """version = 1
requires-python = ">=3.12"

[[package]]
name = "gatehouse"
version = "1.2.2"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "gatehouse-crunchtools"
version = "1.2.2"
source = { editable = "." }
dependencies = [
    { name = "httpx" },
]

[package.metadata]
requires-dist = [{ name = "httpx", specifier = ">=0.27" }]

[[package]]
name = "httpx"
version = "0.28.1"
source = { registry = "https://pypi.org/simple" }
"""
bumped = bump_lock(GATEHOUSE_LOCK, "1.2.3")
check(
    "lock: the editable root moves, whatever its name",
    bumped
    == GATEHOUSE_LOCK.replace(
        'name = "gatehouse-crunchtools"\nversion = "1.2.2"',
        'name = "gatehouse-crunchtools"\nversion = "1.2.3"',
    ),
    str(bumped),
)
check(
    "lock: a dependency at the old version stays", 'name = "gatehouse"\nversion = "1.2.2"' in bumped
)
virtual = GATEHOUSE_LOCK.replace('{ editable = "." }', '{ virtual = "." }')
check(
    "lock: a virtual root moves",
    'version = "1.2.3"\nsource = { virtual = "." }' in (bump_lock(virtual, "1.2.3") or ""),
)
check("lock: already at the version is no change", bump_lock(bumped, "1.2.3") is None)
# ashigaru's shape: `dynamic = ["version"]`, so uv locks the root with no version line.
dynamic = GATEHOUSE_LOCK.replace('version = "1.2.2"\nsource = { editable', "source = { editable", 1)
check("lock: a dynamic-version root is no change", bump_lock(dynamic, "1.2.3") is None)
check(
    "lock: no root is no change",
    bump_lock(GATEHOUSE_LOCK.replace('{ editable = "." }', '{ editable = "pkg" }'), "1.2.3")
    is None,
)
for label, text in (
    (
        "two roots",
        GATEHOUSE_LOCK.replace('{ registry = "https://pypi.org/simple" }', '{ editable = "." }', 1),
    ),
    (
        "an unusual layout",
        GATEHOUSE_LOCK.replace(
            'version = "1.2.2"\nsource = { editable = "." }',
            'source = { editable = "." }\nversion = "1.2.2"',
        ),
    ),
):
    try:
        bump_lock(text, "1.2.3")
        check(f"lock: {label} refused", False)
    except ValueError:
        check(f"lock: {label} refused", True)


def lock_api(text: str, open_prs: list | None = None) -> FakeGitHub:
    api = opener(False, open_prs or [])
    api.routes[("GET", f"/repos/{REPO}/contents/uv.lock")] = {
        "content": base64.b64encode(text.encode()).decode(),
        "sha": "e" * 40,
    }
    api.routes[("PUT", f"/repos/{REPO}/contents/uv.lock")] = {}
    return api


api = lock_api(GATEHOUSE_LOCK)
check("open: a stale lock opens the PR", opened(api) == 0 and api.made("POST", "*/pulls"))
put = api.made("PUT", "*/contents/uv.lock")
check(
    "open: the lock is committed onto the App's branch, at the prepared tree",
    len(put) == 1
    and base64.b64decode(put[0][2]["content"]).decode() == bumped
    and put[0][2]["sha"] == "e" * 40
    and put[0][2]["branch"] == "ashigaru/release-1.2.3"
    and len(api.made("GET", f"*/contents/uv.lock?ref={'c' * 40}")) == 1,
    str(put),
)
check(
    "open: the lock is moved after the branch, before the PR",
    [c[0] for c in api.calls if c[0] != "GET"] == ["POST", "PUT", "POST"],
)
api = lock_api(GATEHOUSE_LOCK, [{"number": 9, "html_url": "https://x/pull/9"}])
check(
    "open: a rerun with the PR open moves the lock again",
    opened(api) == 0 and api.made("PUT") and not api.made("POST", "*/pulls"),
)
api = lock_api(bumped)
check("open: a current lock commits nothing", opened(api) == 0 and not api.made("PUT"))
api = lock_api(dynamic)
check(
    "open: a dynamic-version root opens the PR and commits nothing",
    opened(api) == 0 and not api.made("PUT") and api.made("POST", "*/pulls"),
)
api = opener(False, [])
check(
    "open: no uv.lock (404) opens the PR and commits nothing",
    opened(api) == 0
    and api.made("GET", f"*/contents/uv.lock?ref={'c' * 40}")
    and not api.made("PUT")
    and api.made("POST", "*/pulls"),
)
api = lock_api("not = [toml")
check(
    "open: an unreadable lock is refused before the PR",
    opened(api) == 1 and not api.made("PUT") and not api.made("POST", "*/pulls"),
)

# audit_issue
audit = {"dependencies": [{"name": "x", "version": "1", "vulns": []}]}
runner = FakeRunner()
with contextlib.redirect_stdout(io.StringIO()):
    code = audit_issue(site_for(FakeGitHub(), runner), audit, TOKEN, "https://run/1")
argv, env, _, _ = runner.calls[0]
check("audit: exit passes through", code == 0)
check(
    "audit: argv",
    argv[:3] == ["python3", "-I", "-c"]
    and argv[5:]
    == ["--json", argv[6], "--event", "schedule", "--repo", REPO, "--run-url", "https://run/1"],
    str(argv[4:]),
)
check(
    "audit names the constitution's scripts from the module attribute", argv[4] == str(CONSTITUTION)
)
check(
    "audit: AUTHOR override is the App's",
    'audit_alert.AUTHOR = "app/ashigaru-crunchtools-com"' in argv[3],
)
check(
    "audit: env is GH_TOKEN only",
    env.get("GH_TOKEN") == TOKEN and not {"ASHIGARU_APP_KEY", "GITHUB_TOKEN"} & set(env),
)
for rc, expect in ((1, 1), (2, None), (3, None)):
    r = FakeRunner([(["-I"], Done(rc, "", "boom"))])
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            got = audit_issue(site_for(FakeGitHub(), r), audit, TOKEN, "u")
        check(f"audit: exit {rc}", got == expect)
    except CannotRunError:
        check(f"audit: exit {rc}", expect is None)

# --- the launchers, executed -------------------------------------------------
# The inline programs run under a real interpreter against stand-in modules, so
# what they import, the argv they pass and the crash-to-3 mapping are exercised.

with tempfile.TemporaryDirectory(prefix="launchers-") as scratch_dir:
    scripts_dir = Path(scratch_dir)
    (scripts_dir / "release_publish.py").write_text(
        "import sys\n"
        "def publish(repo, version, sha):\n"
        "    print('publish', repo, version, sha)\n"
        "    return 0 if version != '9.9.9' else 1\n"
    )
    (scripts_dir / "audit_alert.py").write_text(
        "import sys\n"
        "AUTHOR = 'github-actions[bot]'\n"
        "def main():\n"
        "    print('audit', AUTHOR, ' '.join(sys.argv[1:]))\n"
        "    if '--repo' in sys.argv and sys.argv[sys.argv.index('--repo') + 1] == 'o/crash':\n"
        "        raise RuntimeError('boom')\n"
        "    return 0\n"
    )
    launched = Site("o/r", {}, tools.run, scripts_dir)
    with mock.patch.object(tools, "ASHIGARU_SCRIPTS", scripts_dir):
        done = tools.run(
            [
                sys.executable,
                "-I",
                "-c",
                launchers._PUBLISH,
                str(scripts_dir),
                "o/r",
                "1.2.3",
                "a" * 40,
            ],
            env=tools.child_env({}),
        )
        check(
            "launcher: publish runs release_publish.publish(repo, version, sha)",
            done.returncode == 0 and done.stdout.strip() == f"publish o/r 1.2.3 {'a' * 40}",
            done.stdout + done.stderr,
        )
        done = tools.run(
            [
                sys.executable,
                "-I",
                "-c",
                launchers._PUBLISH,
                str(scripts_dir / "missing"),
                "o/r",
                "1.2.3",
                "a" * 40,
            ],
            env=tools.child_env({}),
        )
        check("launcher: publish maps a crash to 3", done.returncode == 3, done.stderr)
    with mock.patch.object(tools, "CONSTITUTION_SCRIPTS", scripts_dir):
        argv = [
            sys.executable,
            "-I",
            "-c",
            launchers._AUDIT,
            str(scripts_dir),
            "--json",
            "r.json",
            "--repo",
            "o/r",
        ]
        done = tools.run(argv, env=tools.child_env({}))
        check(
            "launcher: audit sets AUTHOR to the App and passes the rest of argv",
            done.returncode == 0
            and done.stdout.strip()
            == "audit app/ashigaru-crunchtools-com --json r.json --repo o/r",
            done.stdout + done.stderr,
        )
        argv[-1] = "o/crash"
        done = tools.run(argv, env=tools.child_env({}))
        check("launcher: audit maps a crash to 3", done.returncode == 3, done.stderr)

if __name__ == "__main__":
    sys.exit(finish())
