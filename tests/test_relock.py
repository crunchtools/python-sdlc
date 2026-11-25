#!/usr/bin/env python3
"""sdlc_jobs/_relock.py: Ashigaru's `uv lock` runs in a sibling, never in `write` (#195).

A branch's .python-version names the interpreter uv executes to resolve. Here
it names one that writes a marker, and the lock is called as relock.py calls
it: `uv lock --no-build` by PATH, in the checkout, with the PATH `write` hands
Ashigaru's scripts and nothing else of the job's. The marker must not appear;
the `podman` on PATH is a fake that records the sibling it was asked for and
hands back a lock. Were write's PATH to lose the shim, the real uv would run
the marker, and the fake would never be called: both are checked.

Also: the staged changes the shim refuses before anything runs, the argv it
refuses, and the lock it will not take back.

Run: python3 tests/test_relock.py
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import sdlc_root
from fake_github import check, finish

from sdlc_jobs import _relock, write
from sdlc_jobs._tools import Done, Site

TMP = Path(tempfile.mkdtemp(prefix="sdlc-relock-test-"))
atexit.register(shutil.rmtree, TMP, ignore_errors=True)
IMAGE = "quay.io/crunchtools/sdlc@sha256:" + "0" * 64
SECRETS = {
    "ASHIGARU_APP_KEY": "PEM-KEY",
    "GITHUB_TOKEN": "read-tok",
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok",
}
PYPROJECT = '[project]\nname = "x"\nversion = "0"\nrequires-python = ">=3.9"\ndependencies = []\n'
LOCK = (
    'version = 1\nrevision = 3\nrequires-python = ">=3.9"\n\n'
    '[[package]]\nname = "x"\nversion = "0"\nsource = { editable = "." }\n'
)
FRESH = LOCK.replace('version = "0"', 'version = "1"')
FAKE_PODMAN = """#!{python} -I
import json, pathlib, sys
args = sys.argv[1:]
with open({log!r}, "a") as f:
    f.write(json.dumps(args) + "\\n")
if "inspect" in args:
    print(json.dumps([{{"Mounts": [{{"Source": {work!r}, "Destination": {work!r}}}],
                        "ImageName": {image!r}}}]))
elif "run" in args and pathlib.Path({log!r} + ".fail").exists():
    sys.exit(125)
elif "run" in args:
    out = next(a for a in args if a.endswith(":/out:rw")).rsplit(":", 2)[0]
    pathlib.Path(out, "uv.lock").write_text({lock!r})
    print("Resolved 1 package")
"""
EVIL = '#!/bin/sh\necho ran >> {marker}\nexec {python} "$@"\n'


def never_run(argv: object, **_kw: object) -> Done:
    """write's runner: nothing here goes through it, so a call is a failure."""
    raise AssertionError(f"write ran {argv}")


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


class Scene:
    """write's scratch dir with a checkout whose HEAD carries a hostile .python-version."""

    def __init__(self, name: str) -> None:
        self.work = TMP / name
        self.checkout = self.work / "pr"
        self.checkout.mkdir(parents=True)
        self.marker = TMP / f"{name}.marker"
        self.log = TMP / f"{name}.podman"
        evil = TMP / f"{name}.python"
        evil.write_text(EVIL.format(marker=self.marker, python=sys.executable))
        evil.chmod(0o755)
        fake = TMP / f"{name}-bin"
        fake.mkdir()
        (fake / "podman").write_text(
            FAKE_PODMAN.format(
                python=sys.executable,
                log=str(self.log),
                work=str(self.work),
                image=IMAGE,
                lock=FRESH,
            )
        )
        (fake / "podman").chmod(0o755)
        git(self.checkout, "init", "-q", ".")
        (self.checkout / ".python-version").write_text(f"{evil}\n")
        (self.checkout / "pyproject.toml").write_text(PYPROJECT)
        (self.checkout / "uv.lock").write_text(LOCK)
        git(self.checkout, "add", "-A")
        git(self.checkout, "commit", "-qm", "base")
        # The job's PATH: the fake engine, then the system's, where a real uv may be.
        job_env = {
            **SECRETS,
            "PATH": f"{fake}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
            "HOME": str(TMP),
            "CONTAINER_HOST": "unix:///fake.sock",
        }
        site = Site("o/r", job_env, never_run, self.work)
        job = write.Job(site, lambda _perms: contextlib.nullcontext("tok"))
        self.env = job.writer_env("tok")

    def stage(self, path: str, text: str) -> None:
        (self.checkout / path).parent.mkdir(parents=True, exist_ok=True)
        (self.checkout / path).write_text(text)
        git(self.checkout, "add", "--", path)

    def lock(self, *args: str) -> subprocess.CompletedProcess[str]:
        """The lock as relock.py runs it: by PATH, in the checkout, with its environment."""
        keep = {k: self.env[k] for k in ("PATH", "HOME") if k in self.env}
        return subprocess.run(
            ["uv", *args],
            cwd=self.checkout,
            capture_output=True,
            text=True,
            env=keep | {"UV_NO_BUILD": "1"},
            timeout=120,
            check=False,
        )

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def run_argv(self) -> list[str]:
        return next((c for c in self.calls() if "run" in c), [])


# -- the hostile interpreter, on the branch ----------------------------------------------------

s = Scene("hostile")
s.stage("pyproject.toml", PYPROJECT.replace('version = "0"', 'version = "1"'))
done = s.lock("lock", "--no-build")
argv = s.run_argv()
check("hostile: the branch's .python-version interpreter never ran in write", not s.marker.exists())
check("hostile: the lock ran in a sibling through the engine", bool(argv), done.stderr)
check(
    "hostile: the shim exited 0 and the sibling's lock was taken",
    done.returncode == 0 and (s.checkout / "uv.lock").read_text() == FRESH,
    done.stderr,
)
check(
    "hostile: only uv.lock changed in the worktree, as relock.py requires",
    git(s.checkout, "diff", "--name-only").split() == ["uv.lock"]
    and not git(s.checkout, "ls-files", "--others", "--exclude-standard"),
)
check(
    "sibling: on the job's image by digest, never pulled",
    argv[-4:-3] == [IMAGE] and "--pull=never" in argv,
)
check(
    "sibling: every capability dropped, no new privileges, read-only root",
    {"--cap-drop=all", "--security-opt=no-new-privileges", "--read-only"} <= set(argv),
)
check(
    "sibling: a network of its own, not the runner's",
    "--network=private" in argv and "--network=host" not in argv,
)
mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
check(
    "sibling: the checkout read-only and only the out dir writable, no socket",
    mounts == [f"{s.checkout}:/src:ro", f"{mounts[1].split(':')[0]}:/out:rw"]
    and not any("sock" in m for m in mounts),
)
given = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
check(
    "sibling: no secret and none of the job's environment",
    given == ["HOME=/scratch", "TMPDIR=/scratch", "UV_NO_BUILD=1"]
    and not any(v in " ".join(argv) for v in [*SECRETS.values(), "x-access-token"]),
)
check(
    "sibling: removed by name after the run",
    any("rm" in c and argv[argv.index("--name") + 1] in c for c in s.calls()),
)

# -- what the shim refuses before anything runs ------------------------------------------------

for name, path, text in [
    ("python-version", ".python-version", "3.12\n"),
    ("nested-python-version", "pkg/.python-version", "3.12\n"),
    ("uv-toml", "uv.toml", 'python-preference = "only-system"\n'),
    ("nested-uv-toml", "pkg/uv.toml", "\n"),
    ("tool-uv", "pyproject.toml", PYPROJECT + '\n[tool.uv]\npython-preference = "only-system"\n'),
]:
    s = Scene(name)
    s.stage(path, text)
    done = s.lock("lock", "--no-build")
    check(
        f"refused: a staged {path} stops the lock before the engine is reached",
        done.returncode == _relock.REFUSED
        and not s.calls()
        and not s.marker.exists()
        and path in done.stderr,
        done.stderr,
    )

s = Scene("mode")
git(s.checkout, "update-index", "--chmod=+x", "pyproject.toml")
done = s.lock("lock", "--no-build")
check(
    "refused: a staged mode change",
    done.returncode == _relock.REFUSED and "mode" in done.stderr and not s.calls(),
    done.stderr,
)

s = Scene("engine")
s.stage("pyproject.toml", PYPROJECT.replace('version = "0"', 'version = "1"'))
Path(f"{s.log}.fail").touch()
done = s.lock("lock", "--no-build")
check(
    "engine: podman failing to start the sibling is 2, said so, not uv's verdict",
    done.returncode == _relock.REFUSED and "could not start" in done.stderr,
    done.stderr,
)

s = Scene("argv")
done = s.lock("sync")
check(
    "refused: anything but `lock --no-build`", done.returncode == _relock.REFUSED and not s.calls()
)

# -- what it will not take back ----------------------------------------------------------------

out = TMP / "take"
out.mkdir()
(out / "uv.lock").write_text("not = [toml")
check(
    "take: a lock that does not parse is refused",
    "not a uv lock" in (_relock.take(out / "uv.lock", out / "dest") or ""),
)
(out / "uv.lock").write_text('version = "1"\n')
check(
    "take: TOML that is not uv's layout is refused",
    "not a uv lock" in (_relock.take(out / "uv.lock", out / "dest") or ""),
)
(out / "uv.lock").write_text(LOCK)
target = out / "elsewhere"
target.write_text("untouched")
(out / "dest").symlink_to(target)
check(
    "take: a uv.lock the branch made a symlink is replaced, not written through",
    _relock.take(out / "uv.lock", out / "dest") is None
    and target.read_text() == "untouched"
    and not (out / "dest").is_symlink()
    and (out / "dest").read_text() == LOCK,
)

check("install: the shim imports this checkout's sdlc_jobs", _relock.ROOT == sdlc_root.ROOT)

if __name__ == "__main__":
    sys.exit(finish())
