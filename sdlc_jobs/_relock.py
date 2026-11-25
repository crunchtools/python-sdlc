"""`uv` for Ashigaru's relock: the lock runs in a sibling container, never in `write`.

Ashigaru's relock.py runs `uv lock --no-build` in the checkout once a patch
has moved pyproject.toml. `--no-build` stops a dependency's backend, but uv
still executes the interpreter the branch's .python-version names in order to
resolve (#195, reproduced on uv 0.13.7): content from the branch, running in
the job that holds the runner's socket and ASHIGARU_APP_KEY.

So `write` puts the `uv` this module installs first on the PATH it hands
Ashigaru's scripts (relock.py keeps PATH and drops the rest), and that `uv`:

  - runs `lock --no-build` and nothing else
  - refuses a staged change to .python-version, uv.toml, pyproject's
    [tool.uv] or a file's mode: defense in depth, the sibling is the fix
  - locks in a sibling on this job's image by digest, with no socket and no
    secret, every capability dropped, a read-only root, the checkout mounted
    read-only and copied without its .git, and a network of its own rather
    than the runner's: the package index is what it is there to reach
  - hands back only uv.lock, taken only when it parses as a uv lock.

Its exit is 0, or 2 with the reason and uv's output on stderr; relock.py turns anything but
0 into a refusal, and the run hands over to a person.
"""

from __future__ import annotations

import json
import os
import posixpath
import secrets
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from sdlc_jobs import _core as core

ROOT = Path(__file__).resolve().parent.parent
ALLOWED = ["lock", "--no-build"]
# Files uv reads to choose an interpreter or its own configuration, at any depth.
DENIED_NAMES = frozenset({".python-version", ".python-versions", "uv.toml"})
RAW = ("--no-renames", "--no-abbrev", "--no-ext-diff", "--no-textconv")
GONE = "000000"  # git's mode for a side that does not exist
REFUSED = 2  # relock.py hands over on any nonzero; 2 says the lock could not run
SECONDS = 540  # podman's --timeout on the sibling: inside relock.py's 600
GRACE = 30  # the client's wait past that, so this shim and not relock removes the sibling
ENGINE_FAILED = (125, 126, 127)  # podman's own: the sibling never ran uv
MAX_LOCK = 50 * 1024 * 1024
SCRATCH = "/scratch"  # the sibling's only writable path but OUT: a tmpfs, its HOME and TMPDIR
TMPFS = f"{SCRATCH}:rw,exec,size=1g,mode=1777"
SRC, OUT = "/src", "/out"
# What the sibling runs: a copy of the checkout without .git, locked, and the lock out.
INSIDE = (
    "import shutil, subprocess, sys\n"
    f"shutil.copytree({SRC!r}, 'lock', symlinks=True, ignore=shutil.ignore_patterns('.git'))\n"
    "done = subprocess.run(['uv', 'lock', '--no-build'], cwd='lock')\n"
    f"done.returncode or shutil.copyfile('lock/uv.lock', {OUT + '/uv.lock'!r})\n"
    "sys.exit(done.returncode)\n"
)

Run = Callable[..., subprocess.CompletedProcess[str]]


def install(work: Path, env: Mapping[str, str]) -> Path:
    """Write the `uv` shim under `work` and return its directory, for the front of PATH.

    The shim runs this interpreter isolated (-I: no cwd, no PYTHON* variables),
    with what it needs to find the socket baked in, since relock.py drops the
    environment that carries it.
    """
    config = {
        "work": str(work),
        "env": {k: env[k] for k in ("CONTAINER_HOST", "HOSTNAME") if env.get(k)},
    }
    where = work / "relock-bin"
    where.mkdir(exist_ok=True)
    shim = where / "uv"
    shim.write_text(
        f"#!{sys.executable} -I\n"
        "import json, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from sdlc_jobs import _relock\n"
        f"sys.exit(_relock.shim(json.loads({json.dumps(config)!r}), sys.argv[1:]))\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return where


def shim(config: Mapping[str, Any], argv: Sequence[str], run: Run = subprocess.run) -> int:
    """`uv` as Ashigaru's relock sees it, in the checkout it is run in: see the module."""
    if list(argv) != ALLOWED:
        return refuse(f"write runs only `uv {' '.join(ALLOWED)}`, not `uv {' '.join(argv)}`")
    checkout = Path.cwd()
    why = staged_refusal(checkout)
    if why is not None:
        return refuse(f"the patch changes {why}; uv is not run on it")
    work = Path(config["work"])
    if checkout != work and work not in checkout.parents:
        return refuse(f"the checkout {checkout} is not under write's scratch dir {work}")
    env = dict(config.get("env") or {})
    try:  # a test hands the mount in; the job finds it through the socket
        host, image = config.get("mount") or core.host_mount(core.Tools(), env, work)
    except core.CannotRunError as err:
        return refuse(str(err))
    if "@sha256:" not in image:
        return refuse(f"this job's image is not pinned by digest: {image!r}")
    return lock_beside(env, image, (work, host), checkout, run)


def lock_beside(
    env: Mapping[str, str], image: str, mount: tuple[Path, str], checkout: Path, run: Run
) -> int:
    """Lock `checkout` in a sibling and take back its uv.lock; 0, or REFUSED.

    `mount` is write's scratch dir and its path on the host, where the engine mounts from.
    """
    work, host = mount

    def on_host(path: Path) -> str:
        return str(Path(host) / path.relative_to(work))

    out = Path(tempfile.mkdtemp(prefix="relock-", dir=work))
    name = f"sdlc-relock-{secrets.token_hex(6)}"
    argv = sibling(env, name, image, on_host(checkout), on_host(out))
    try:
        done = run(argv, capture_output=True, text=True, timeout=SECONDS + GRACE, check=False)
    except subprocess.TimeoutExpired:
        return refuse(f"the lock ran past {SECONDS}s")
    finally:
        remove(env, name, run)
    sys.stderr.write(done.stdout + done.stderr)
    if done.returncode in ENGINE_FAILED:
        return refuse(f"podman could not start the sibling (exit {done.returncode})")
    if done.returncode != 0:  # uv's failure is not a verdict: the lock could not be made
        return refuse(f"`uv lock --no-build` exited {done.returncode} in the sibling")
    why = take(out / "uv.lock", checkout / "uv.lock")
    return refuse(why) if why else 0


def remove(env: Mapping[str, str], name: str, run: Run) -> None:
    """`podman rm -f` of the sibling, as ai removes its model's; gone already is fine."""
    argv = [*core.podman(env), "rm", "-f", "--ignore", name]
    done = run(argv, capture_output=True, text=True, timeout=SECONDS, check=False)
    if done.returncode != 0:
        print(f"::warning::could not remove {name}: {(done.stderr or done.stdout).strip()}")


def sibling(env: Mapping[str, str], name: str, image: str, src: str, out: str) -> list[str]:
    """The sibling's `podman run`: `src` (host path) read-only, only `out` writable."""
    return [
        *core.podman(env),
        "run",
        "--rm",
        "--name",
        name,
        "--timeout",
        str(SECONDS),
        "--pull=never",
        "--user",
        "0",
        "--cap-drop=all",
        "--security-opt=no-new-privileges",
        "--security-opt=label=disable",
        "--read-only",
        "--network=private",
        "--tmpfs",
        TMPFS,
        "-v",
        f"{src}:{SRC}:ro",
        "-v",
        f"{out}:{OUT}:rw",
        "-w",
        SCRATCH,
        "-e",
        f"HOME={SCRATCH}",
        "-e",
        f"TMPDIR={SCRATCH}",
        "-e",
        "UV_NO_BUILD=1",
        "--entrypoint",
        "python3",
        image,
        "-I",
        "-c",
        INSIDE,
    ]


def staged_refusal(checkout: Path) -> str | None:
    """What in the index, against HEAD, the lock may not run on, or None.

    Judged from git's raw diff of the index, so a rename, a quoted path or a
    mode bit cannot hide: .python-version, .python-versions or uv.toml at any
    depth, any change of a file's mode, and any change to pyproject's [tool.uv].
    """

    def git(*args: str) -> bytes:
        done = subprocess.run(["git", *args], cwd=checkout, capture_output=True, check=False)
        if done.returncode != 0:
            raise ValueError(done.stderr.decode("utf-8", "replace").strip()[-300:])
        return done.stdout

    try:
        raw = git("-c", "core.quotePath=false", "diff", "--cached", "--raw", "-z", *RAW, "HEAD")
    except ValueError as err:
        return f"an index git cannot read ({err})"
    fields = raw.split(b"\0")
    for meta, name in zip(fields[0::2], fields[1::2], strict=False):
        if meta.startswith(b":"):
            old_mode, new_mode = meta[1:].decode("ascii").split(" ")[:2]
            why = judged(git, name.decode("utf-8", "backslashreplace"), old_mode, new_mode)
            if why is not None:
                return why
    return None


def judged(git: Callable[..., bytes], path: str, old_mode: str, new_mode: str) -> str | None:
    """Why one staged path stops the lock, or None."""
    base = posixpath.basename(path)
    if base in DENIED_NAMES:
        return f"`{path}`"
    if GONE not in (old_mode, new_mode) and old_mode != new_mode:
        return f"the mode of `{path}` ({old_mode} to {new_mode})"
    if base == "pyproject.toml":
        try:
            moved = uv_table(git, f"HEAD:{path}") != uv_table(git, f":{path}")
        except ValueError:  # TOMLDecodeError, bytes that are not UTF-8, a side missing
            moved = True
        if moved:
            return f"`[tool.uv]` in `{path}`"
    return None


def uv_table(git: Callable[..., bytes], spec: str) -> Any:
    """pyproject's [tool.uv] at `spec` (a git object name), parsed; {} for a new file."""
    if spec.startswith("HEAD:"):
        try:
            git("cat-file", "-e", spec)
        except ValueError:
            return {}
    text = git("show", spec).decode("utf-8")
    return tomllib.loads(text).get("tool", {}).get("uv", {})


def take(lock: Path, into: Path) -> str | None:
    """Put the sibling's `lock` at `into` if it is a uv lock; else why it was not taken.

    Replaced, never written through: a uv.lock the branch made a symlink is
    swapped for a file, not followed.
    """
    try:
        if not lock.is_file() or lock.is_symlink():
            return "the sibling handed back no uv.lock"
        if lock.stat().st_size > MAX_LOCK:
            return f"the sibling's uv.lock is over {MAX_LOCK} bytes"
        text = lock.read_text(encoding="utf-8")
        doc = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        return f"the sibling's uv.lock is not a uv lock: {err}"
    if not is_uv_lock(doc):
        return "the sibling's uv.lock is not a uv lock: no integer `version` or a bad `[[package]]`"
    mode = into.stat().st_mode & 0o777 if into.is_file() and not into.is_symlink() else 0o644
    fd, staged = tempfile.mkstemp(prefix=".uv.lock-", dir=into.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(staged, mode)
    os.replace(staged, into)
    return None


def is_uv_lock(doc: Mapping[str, Any]) -> bool:
    """Whether a parsed TOML document is laid out as uv writes uv.lock."""
    packages = doc.get("package", [])
    return (
        isinstance(doc.get("version"), int)
        and not isinstance(doc.get("version"), bool)
        and isinstance(packages, list)
        and all(isinstance(p, dict) and isinstance(p.get("name"), str) for p in packages)
    )


def refuse(why: str) -> int:
    """Say `why` on stderr, for relock.py's refusal, and exit 2."""
    print(f"sdlc relock: {why}", file=sys.stderr)
    return REFUSED
