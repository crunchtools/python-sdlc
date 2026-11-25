"""bin/sdlc-secret-scan, run against a scratch repo.

Ported from gatehouse@9ac16df (tests/test_secret_scan.py) when the fleet ci.yml
replaced gourmand.yml (sdlc#213): the `Refuse plaintext secrets` step's shell
now lives in bin/sdlc-secret-scan. Each case commits a base, commits the lines
under test, and scans BASE..HEAD. Refused lines fail with an `::error`; a named
literal in a test path only warns (a475); git unable to diff exits 2.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import inspect
import io
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "bin" / "sdlc-secret-scan"
SDLC = Path(__file__).parent.parent / "sdlc"
CANNOT_RUN = 2  # the exit for a scan that could not diff


CASES: dict[str, list[dict]] = {}


def cases(names: str | tuple[str, ...], values: Iterable) -> Callable:
    """Run the test once per value, as `main` does: pytest's parametrize, without pytest.

    Stacked, the cases multiply. A test whose first parameter is `tmp_path` gets a
    fresh directory for each one.
    """
    keys = names if isinstance(names, tuple) else (names,)

    def mark(fn: Callable) -> Callable:
        rows = [
            dict(zip(keys, v if isinstance(names, tuple) else (v,), strict=True)) for v in values
        ]
        CASES[fn.__name__] = [
            {**row, **rest} for row in rows for rest in CASES.get(fn.__name__, [{}])
        ]
        return fn

    return mark


def _env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GIT_AUTHOR_NAME": "Alice",
        "GIT_AUTHOR_EMAIL": "alice@example.com",
        "GIT_COMMITTER_NAME": "Alice",
        "GIT_COMMITTER_EMAIL": "alice@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _repo(
    tmp_path: Path, files: dict[str, str], base_files: dict[str, str] | None = None
) -> tuple[Path, dict[str, str], str]:
    """Commit `files` on top of a base commit; return (repo, env, base sha)."""
    env = _env(tmp_path)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    for name, text in (base_files or {"README.md": "base\n"}).items():
        (repo / name).write_text(text)
    _git(repo, env, "add", ".")
    _git(repo, env, "commit", "-qm", "base")
    base = _git(repo, env, "rev-parse", "HEAD")
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, env, "add", ".")
    _git(repo, env, "commit", "-qm", "change")
    return repo, env, base


def _run(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(SCRIPT), *args], check=False, cwd=repo, env=env, capture_output=True, text=True
    )


def _scan(
    tmp_path: Path, files: dict[str, str], base_files: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Commit `files` on top of a base commit and scan base..HEAD."""
    repo, env, base = _repo(tmp_path, files, base_files)
    return _run(repo, env, base)


REFUSED = {
    "vendor shape": ("src/app.py", 'KEY = "ghp_' + "a" * 36 + '"\n'),
    "or fallback": (
        "src/db.js",
        "password: process.env.PGPASSWORD || 'rotv',\n",  # gatehouse: allow-secret
    ),
    "nullish fallback": (
        "src/db.ts",
        "const s = process.env.SESSION_SECRET ?? 'hunter22';\n",  # gatehouse: allow-secret
    ),
    "placeholder fallback": (
        "server.js",
        "secret: process.env.SESSION_SECRET || 'change-this',\n",  # gatehouse: allow-secret
    ),
    "shell default": (
        "run.sh",
        'psql "postgres://rotv:${PGPASSWORD:-rotv}@db/rotv"\n',  # gatehouse: allow-secret
    ),
    "getenv default": (
        "src/config.py",
        'pw = os.environ.get("PGPASSWORD", "imageserver")\n',  # gatehouse: allow-secret
    ),
    "fallback in a test": (
        "tests/db.test.js",
        "password: process.env.PGPASSWORD || 'rotv',\n",  # gatehouse: allow-secret
    ),
    "quoted assignment": (
        "src/settings.py",
        'db_password = "s3cr3t-value"\n',  # gatehouse: allow-secret
    ),
    "json key": (
        "backup/dump.sql",
        '{"refresh_token": "1//0abcdefghij"}\n',  # gatehouse: allow-secret
    ),
    "env file": (".env.test", "PGPASSWORD=rotv\n"),
    "literal password": (
        "src/setup.mjs",
        "const DEFAULT_PASSWORD = 'password';\n",  # gatehouse: allow-secret
    ),
    "compose list": ("compose.yaml", "    environment:\n      - POSTGRES_PASSWORD=rotv\n"),
    "yaml quoted": ("deploy/app.yml", 'API_TOKEN: "abc123def"\n'),  # gatehouse: allow-secret
    "unit environment": ("rotv.container", "Environment=PGPASSWORD=rotv\n"),
    "quoted unit environment": ("rotv.service", 'Environment="PGPASSWORD=rotv"\n'),
    "containerfile env": ("Containerfile", "ENV PGPASSWORD=rotv\n"),
    "suffixed containerfile": ("app.Containerfile", "ARG PGPASSWORD=rotv\n"),
    "podman args": ("rotv.container", "PodmanArgs=--env PGPASSWORD=rotv\n"),
    "placeholder word inside": (
        "src/settings.py",
        'api_key = "myexample-prod-key-9f3a"\n',  # gatehouse: allow-secret
    ),
    "x run inside": (
        "src/settings.py",
        'api_key = "xxxREALSECRETxxx"\n',  # gatehouse: allow-secret
    ),
    "vendor shape in a doc": ("README.md", "Use `ghp_" + "a" * 36 + "`.\n"),
}

PASSED = {
    "env read only": ("src/db.js", "password: process.env.PGPASSWORD,\n"),
    "empty default": ("src/config.py", 'pw = os.environ.get("PGPASSWORD", "")\n'),
    "changeme": ("src/settings.py", 'db_password = "changeme"\n'),
    "angle placeholder": ("deploy/app.yml", "API_TOKEN: <your token>\n"),
    "template": ("deploy/app.yml", 'password: "${DB_PASSWORD}"\n'),
    "actions expression": (".github/workflows/ci.yml", "  token: ${{ secrets.GH_TOKEN }}\n"),
    "file path key": ("src/settings.py", 'TOKEN_FILE = "/run/secrets/token"\n'),
    "boolean": (".env", "REQUIRE_PASSPHRASE=yes\n"),
    "env name in code": ("src/app.py", "GITLAB_TOKEN: Required. GitLab Personal Access Token.\n"),
    "allow comment": ("src/settings.py", 'db_password = "s3cr3t"  # gatehouse: allow-secret\n'),
    "secrets plural": (".github/workflows/ci.yml", "    secrets: inherit\n"),
    "shell reference": (".github/workflows/ci.yml", '        -e GITHUB_TOKEN="$GH_TOKEN" \\\n'),
    "ci fake": ("pyproject.toml", 'env = { GITLAB_TOKEN = "test_token_for_ci" }\n'),
    "field name": ("src/shapes.py", '    STATIC_TOKEN = "static_token"\n'),
    "mask": ("src/Settings.jsx", "const MASKED_SECRET = '••••••';\n"),
    "switch fallback": ("src/reconnect.py", 'r = env.get("RELAY_REQUIRE_PASSPHRASE", "yes")\n'),
    "unset sentinel": ("run.sh", 'TOKEN="${TOKEN:-unset}"\n'),
    "missing sentinel": ("run.sh", ': "${VAULT_TOKEN:-missing}"\n'),
    "not set sentinel": ("src/config.py", 'pw = os.environ.get("PGPASSWORD", "not-set")\n'),
    "placeholder word": (
        ".github/workflows/ci.yml",
        "      boot-env: |\n        EXAMPLE_API_KEY=gate-dummy\n",
    ),
    "change this, with a suffix": (
        "deploy/app.env",
        "SESSION_SECRET=change-this-to-a-random-string\n",
    ),
    "own name": ("src/consts.py", 'TOKEN = "token"\n'),
    "quoted placeholder in yaml": ("deploy/app.yml", 'API_TOKEN: "changeme"\n'),
    "quoted placeholder in env": (".env", 'PGPASSWORD="changeme"\n'),
    "own name, any case": ("src/locales/en.json", '{"password": "Password"}\n'),
    "systemd credential": ("app.container", "Environment=API_TOKEN=%d/api_token\n"),
    "systemd specifier in a unit": ("app.service", "Environment=PGPASSFILE=%h/.pgpass\n"),
    "systemd specifier in a drop-in": (
        "app.service.d/override.conf",
        "Environment=API_TOKEN=%d/api_token\n",
    ),
}


@cases(
    ("name", "text"),
    [
        ("src/settings.py", 'password = "%Kq9vLm2Xz8"\n'),  # gatehouse: allow-secret
        (".env", "PGPASSWORD=%Kq9vLm2Xz8\n"),  # gatehouse: allow-secret
        ("app.container", "Environment=API_TOKEN=%Kq9vLm2Xz8\n"),  # gatehouse: allow-secret
        ("app.service", "Environment=API_TOKEN=%dq9vLm2Xz8\n"),  # gatehouse: allow-secret
    ],
)
def test_a_percent_literal_that_is_no_specifier_is_refused(tmp_path, name, text):
    result = _scan(tmp_path, {name: text})
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"::error file={name},line=1::" in result.stdout


@cases("case", REFUSED)
def test_refused(tmp_path, case):
    name, text = REFUSED[case]
    result = _scan(tmp_path, {name: text})
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"::error file={name}," in result.stdout


@cases("case", PASSED)
def test_passed(tmp_path, case):
    name, text = PASSED[case]
    result = _scan(tmp_path, {name: text})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::error" not in result.stdout
    assert "::warning" not in result.stdout


@cases(
    "name",
    [
        "tests/test_auth.py",
        "backend/tests/x.js",
        "src/auth.test.ts",
        "conftest.py",
        "pkg/x_test.go",
    ],
)
def test_named_literal_in_a_test_only_warns(tmp_path, name):
    result = _scan(tmp_path, {name: 'access_token = "tok123"\n'})  # gatehouse: allow-secret
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"::warning file={name},line=1::" in result.stdout
    assert "::error" not in result.stdout


@cases(
    ("name", "text"),
    [
        ("README.md", 'password: "hunter2"\n'),  # gatehouse: allow-secret
        ("docs/setup.txt", "pw = process.env.PGPASSWORD || 'rotv'\n"),  # gatehouse: allow-secret
        (".env.example", "SESSION_SECRET=s3cr3t-value\n"),  # gatehouse: allow-secret
        ("config.yml.sample", 'api_token: "abc123def"\n'),  # gatehouse: allow-secret
    ],
)
def test_prose_about_a_secret_only_warns(tmp_path, name, text):
    result = _scan(tmp_path, {name: text})
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"::warning file={name},line=1::" in result.stdout
    assert "::error" not in result.stdout


MULTI_REFUSED = {
    "containerfile env": ("Containerfile", "ENV A=x PGPASSWORD=rotv\n"),
    "quoted unit pairs": ("rotv.service", 'Environment="A=x" "PGPASSWORD=rotv"\n'),
    "unit pairs": ("rotv.container", "Environment=A=x PGPASSWORD=rotv\n"),
    "shell export": ("run.sh", "export A=x PGPASSWORD=rotv\n"),
    "docker run": ("run.sh", "docker run -e A=x -e PGPASSWORD=rotv img\n"),
    "quoted docker run": ("run.sh", 'podman run -e A=x -e "PGPASSWORD=rotv" img\n'),
    "podman args": ("rotv.container", "PodmanArgs=--env A=x --env=PGPASSWORD=rotv\n"),
    "continued run": ("run.sh", "  -e PGPASSWORD=rotv \\\n"),
    "hash inside a value": ("Containerfile", "ENV A=x#y PGPASSWORD=rotv\n"),
    "quoted value led by a hash": (
        "Containerfile",
        'ENV A=x PGPASSWORD="#s3cret"\n',  # gatehouse: allow-secret
    ),
    "percent outside a unit": ("Containerfile", "ENV A=x API_TOKEN=%d/api_token\n"),
    "percent that is no specifier": (
        "app.container",
        "Environment=A=x API_TOKEN=%Kq9vLm2Xz8\n",  # gatehouse: allow-secret
    ),
}

MULTI_PASSED = {
    "placeholder": ("Containerfile", "ENV A=x PGPASSWORD=changeme\n"),
    "reference": ("run.sh", 'export A=x TOKEN="$T"\n'),
    "docker reference": ("run.sh", 'docker run -e A=x -e GITHUB_TOKEN="$GH_TOKEN" img\n'),
    "own name": ("Containerfile", "ENV A=x TOKEN=token\n"),
    "systemd credential": ("app.container", "Environment=A=x API_TOKEN=%d/api_token\n"),
    "file path key": ("Containerfile", "ENV A=x TOKEN_FILE=/run/secrets/token\n"),
    "comment": ("run.sh", "export A=x  # then set PGPASSWORD=rotv by hand\n"),
}


@cases("case", MULTI_REFUSED)
def test_a_later_pair_on_a_line_is_refused(tmp_path, case):
    name, text = MULTI_REFUSED[case]
    result = _scan(tmp_path, {name: text})
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"::error file={name},line=1::plaintext secret in " in result.stdout
    assert "plaintext secret in A\n" not in result.stdout


@cases("case", MULTI_PASSED)
def test_a_later_pair_holding_no_secret_passes(tmp_path, case):
    name, text = MULTI_PASSED[case]
    result = _scan(tmp_path, {name: text})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::error" not in result.stdout
    assert "::warning" not in result.stdout


@cases("name", ["tests/run.sh", ".env.example"])
def test_a_later_pair_in_a_test_or_doc_only_warns(tmp_path, name):
    result = _scan(tmp_path, {name: "export A=x SESSION_SECRET=s3cr3t-value\n"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"::warning file={name},line=1::" in result.stdout


def test_a_later_pair_hit_names_the_key_that_failed(tmp_path):
    result = _scan(tmp_path, {"Containerfile": "ENV API_TOKEN=changeme PGPASSWORD=rotv\n"})
    assert "::error file=Containerfile,line=1::plaintext secret in PGPASSWORD" in result.stdout


def test_a_later_pair_hit_names_its_own_key(tmp_path):
    result = _scan(tmp_path, {"Containerfile": "ENV A=x PGPASSWORD=rotv\n"})
    assert "::error file=Containerfile,line=1::plaintext secret in PGPASSWORD" in result.stdout


def test_an_added_line_that_looks_like_a_header_is_read(tmp_path):
    result = _scan(tmp_path, {".env": "++ counter\nPGPASSWORD=rotv\n"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "::error file=.env,line=2::plaintext secret in PGPASSWORD" in result.stdout


def test_a_unit_hit_names_the_variable(tmp_path):
    result = _scan(tmp_path, {"rotv.container": 'Environment="PGPASSWORD=rotv"\n'})
    assert "::error file=rotv.container,line=1::plaintext secret in PGPASSWORD" in result.stdout


def test_each_file_is_classified_on_its_own(tmp_path):
    result = _scan(
        tmp_path,
        {
            "README.md": 'password: "hunter2"\n',  # gatehouse: allow-secret
            "src/settings.py": 'password = "hunter2"\n',  # gatehouse: allow-secret
            "tests/test_x.py": 'password = "hunter2"\n',  # gatehouse: allow-secret
        },
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "::warning file=README.md,line=1::" in result.stdout
    assert "::error file=src/settings.py,line=1::plaintext secret in password" in result.stdout
    assert "::warning file=tests/test_x.py,line=1::" in result.stdout


def test_a_hit_names_the_key_and_line_never_the_value(tmp_path):
    result = _scan(
        tmp_path, {"compose.yaml": "services:\n  db:\n    POSTGRES_PASSWORD: hunter22\n"}
    )
    assert result.returncode == 1
    assert (
        "::error file=compose.yaml,line=3::plaintext secret in POSTGRES_PASSWORD" in result.stdout
    )
    assert "hunter22" not in result.stdout + result.stderr


def test_only_added_lines_are_read(tmp_path):
    committed = "PGPASSWORD=rotv\n"  # gatehouse: allow-secret
    result = _scan(tmp_path, {".env": committed + "PGHOST=db\n"}, base_files={".env": committed})
    assert result.returncode == 0
    assert "No plaintext secrets" in result.stdout


GITHUB_TOKENS = {
    "classic": "ghp_" + "a" * 36,
    "oauth": "gho_" + "b" * 36,
    "server": "ghs_" + "c" * 36,
    "fine-grained": "github_pat_" + "d" * 22 + "_" + "e" * 59,
}


@cases("path", ["src/app.py", "tests/test_app.py"])
@cases("kind", GITHUB_TOKENS)
def test_a_planted_github_token_is_refused_anywhere(tmp_path, kind, path):
    token = GITHUB_TOKENS[kind]
    result = _scan(tmp_path, {path: f'X = "{token}"\n'})
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"::error file={path},line=1::" in result.stdout
    assert token not in result.stdout + result.stderr


def test_a_base_git_cannot_resolve_exits_two(tmp_path):
    repo, env, _ = _repo(tmp_path, {"a.txt": "x\n"})
    result = _run(repo, env, "0" * 40)
    assert result.returncode == CANNOT_RUN, result.stdout + result.stderr


def test_a_usage_error_exits_two(tmp_path):
    repo, env, _ = _repo(tmp_path, {"a.txt": "x\n"})
    result = _run(repo, env)
    assert result.returncode == CANNOT_RUN, result.stdout + result.stderr
    assert "usage" in result.stderr


# ----------------------------------------------- `sdlc secret-scan`, the check


def _planner():
    spec = importlib.util.spec_from_loader(
        "sdlc_secret_scan", importlib.machinery.SourceFileLoader("sdlc_secret_scan", str(SDLC))
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _check(tmp_path: Path, **given: str) -> Iterator:
    """`sdlc` over a repo whose HEAD adds a planted token, with `given` in the env.

    Yields (sdlc, repo, env, base); the process environment is put back after.
    """
    repo, env, base = _repo(tmp_path, {"src/app.py": 'KEY = "ghp_' + "a" * 36 + '"\n'})
    # The image has the script on PATH as sdlc-secret-scan.
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "sdlc-secret-scan").symlink_to(SCRIPT.resolve())
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(env, PATH=f"{shim}{os.pathsep}{env['PATH']}", **given)
    try:
        sdlc = _planner()
        sdlc.REPO = repo
        yield sdlc, repo, env, base
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_the_check_refuses_a_planted_token_against_the_base_ci_names(tmp_path):
    with _check(tmp_path) as (sdlc, _, _, base):
        os.environ["SDLC_DIFF_BASE"] = base
        assert sdlc.t_secret_scan({}) == sdlc.FOUND


def test_the_check_diffs_from_where_the_branch_left_the_default_branch(tmp_path):
    with _check(tmp_path) as (sdlc, repo, env, base):
        _git(repo, env, "branch", "main-tip", base)
        os.environ["SDLC_DEFAULT_BRANCH"] = "main-tip"
        assert sdlc.t_secret_scan({}) == sdlc.FOUND
        # HEAD itself as the default branch: nothing on this branch is new.
        os.environ["SDLC_DEFAULT_BRANCH"] = "HEAD"
        assert sdlc.t_secret_scan({}) == sdlc.CLEAN


def test_with_no_base_the_check_says_so_and_passes(tmp_path):
    with _check(tmp_path) as (sdlc, *_), contextlib.redirect_stdout(io.StringIO()) as out:
        assert sdlc.t_secret_scan({}) == sdlc.CLEAN
    assert "nothing to scan" in out.getvalue()


@cases("given", ["main", "0" * 40, "f" * 39])
def test_a_base_ci_names_and_git_cannot_read_cannot_run(tmp_path, given):
    with _check(tmp_path, SDLC_DIFF_BASE=given) as (sdlc, *_):
        try:
            sdlc.t_secret_scan({})
        except sdlc.CannotRunError:
            return
    raise AssertionError(f"SDLC_DIFF_BASE={given!r} did not refuse to run")


def test_secret_scan_runs_at_push_and_in_ci_for_any_repo():
    sdlc = _planner()
    assert "secret-scan" in sdlc.resolve({}, "push")
    assert "secret-scan" in sdlc.resolve({}, "ci")
    assert "secret-scan" not in sdlc.STACK_OF


def main() -> int:
    if shutil.which("git") is None:
        print("skip: git needed")
        return 0
    tests = {n: f for n, f in globals().items() if n.startswith("test_") and callable(f)}
    failed = total = 0
    for name, fn in tests.items():
        fresh = next(iter(inspect.signature(fn).parameters), None) == "tmp_path"
        for kwargs in CASES.get(name, [{}]):
            total += 1
            label = f"{name}{list(kwargs.values()) if kwargs else ''}"
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    fn(Path(tmp), **kwargs) if fresh else fn(**kwargs)
                except Exception as exc:
                    failed += 1
                    print(f"FAIL {label}: {type(exc).__name__}: {exc}")
                else:
                    print(f"ok   {label}")
    print(f"{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
