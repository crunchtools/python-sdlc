#!/usr/bin/env python3
"""bin/podman-shim: every call goes to podman-remote on the socket, less local-only flags.

The image has no engine, only the runner's socket, so `podman` is a shim to
`podman-remote --url $CONTAINER_HOST`. A fake podman-remote echoes its argv.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

SHIM = Path(__file__).resolve().parent.parent / "bin" / "podman-shim"
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def shim(*argv: str, host: str | None = None) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "podman-remote"
        fake.write_text('#!/bin/sh\necho "$@"\n')
        fake.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k != "CONTAINER_HOST"}
        env["PATH"] = f"{tmp}:{env.get('PATH', '')}"
        if host:
            env["CONTAINER_HOST"] = host
        done = subprocess.run(
            ["sh", str(SHIM), *argv], env=env, capture_output=True, text=True, check=False
        )
        return done.stdout.strip()


SOCK = "--url unix:///var/run/docker.sock"
check(
    "default socket, argv passed through", shim("build", "-t", "a", ".") == f"{SOCK} build -t a ."
)
check("CONTAINER_HOST wins", shim("ps", host="unix:///x.sock") == "--url unix:///x.sock ps")
check("--remote dropped", shim("--remote", "ps", "-a") == f"{SOCK} ps -a")
check(
    "--events-backend=VALUE dropped", shim("--events-backend=file", "run", "x") == f"{SOCK} run x"
)
check(
    "--events-backend VALUE dropped, both words",
    shim("--events-backend", "file", "run", "x") == f"{SOCK} run x",
    shim("--events-backend", "file", "run", "x"),
)
check("a quoted argument stays one word", shim("run", "a b") == f"{SOCK} run a b")


def main() -> int:
    if FAILED:
        print(f"\n{len(FAILED)} failed")
        return 1
    print("\nall passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
