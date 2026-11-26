#!/usr/bin/env python3
"""The host tools scripts/local-release.sh drives, faked for tests/test_local_release.py.

Linked into <base>/bin under each tool's name (id, flock, unshare, skopeo,
podman, curl, openssl), which goes first on PATH; the name it is called by picks
the fake. <base>/state, beside that bin, holds scenario.json, which says what
the GitHub API and each job answer, and log.jsonl, where every job and the
podman service record what they were handed.

scenario.json:
  plan      check's `result`
  release   write's `release`
  tags      the repo's tags, newest first, as GitHub lists them
  fail      job names that exit 1
"""

from __future__ import annotations

import base64
import json
import os
import socket
import sys
import time
from itertools import pairwise
from pathlib import Path

STATE = Path(sys.argv[0]).parent.parent / "state"  # the link's own path, not resolved
SERVICE_LIFETIME = 600  # seconds; the script kills the service long before
DIGEST = "sha256:" + "a" * 64
PAGE = 100


def scenario() -> dict:
    return json.loads((STATE / "scenario.json").read_text())


def log(entry: dict) -> None:
    with (STATE / "log.jsonl").open("a") as f:
        f.write(json.dumps(entry) + "\n")


def output(path: str, name: str, value: object) -> None:
    with open(path, "a") as f:
        f.write(f"{name}<<EOF_x\n{json.dumps(value)}\nEOF_x\n")


def job(args: list[str]) -> int:
    """`podman --remote --url <sock> run ... -e NAME[=value] ... -- <bootstrap> event`."""
    given: dict[str, str] = {}
    names: list[str] = []
    for flag, value in pairwise(args):
        if flag != "-e":
            continue
        if "=" in value:
            key, _, val = value.partition("=")
            given[key] = val
        else:
            names.append(value)
            given[value] = os.environ.get(value, "")
    name = given["GITHUB_JOB"]
    log({"job": name, "names": sorted(names), "values": given, "client": dict(os.environ)})
    doc = scenario()
    if name in doc.get("fail", []):
        return 1
    out = given["GITHUB_OUTPUT"]
    if name == "check":
        output(out, "result", doc["plan"])
    elif name == "image":
        output(out, "result", {"ref": "quay.io/crunchtools/mcp-x", "digest": DIGEST})
    elif name == "write":
        output(out, "release", doc["release"])
    return 0


def podman(args: list[str]) -> int:
    while args[:1] in (["--root"], ["--runroot"]):
        args = args[2:]
    if args[:1] == ["--remote"]:
        return job(args[3:])
    if args[:2] == ["image", "inspect"]:
        print(f"quay.io/crunchtools/sdlc@{DIGEST}")
    return 0  # untag, pull, rmi, images, and the closing cosign verify


def unshare(args: list[str]) -> int:
    """The podman service: its socket, and what it holds, until it is killed."""
    log({"service": dict(os.environ)})
    path = next(a for a in args if a.startswith("unix://")).removeprefix("unix://")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(path)
    time.sleep(SERVICE_LIFETIME)
    return 0


def curl(args: list[str]) -> int:
    sys.stdin.read()  # the bearer, as config
    method = args[args.index("-X") + 1]
    url = args[-1].removeprefix("https://api.github.com")
    log({"api": f"{method} {url}"})
    path, _, query = url.partition("?")
    body: object
    match path.split("/")[1:]:
        case ["repos", _, _, "installation"]:
            body = {"id": 1}
        case ["app", "installations", _, "access_tokens"]:
            body = {"token": "ghs_fake"}
        case ["installation", "token"]:
            body = {}
        case ["repos", _, _, "compare", _]:
            body = {"status": "identical"}
        case ["repos", _, _, "tags"]:
            params = dict(p.split("=") for p in query.split("&"))
            assert params["per_page"] == str(PAGE)
            start = (int(params["page"]) - 1) * PAGE
            body = scenario().get("tags", [])[start : start + PAGE]
        case _:
            body = {"default_branch": "main"}
    print(json.dumps(body))
    return 0


def openssl(args: list[str]) -> int:
    data = sys.stdin.buffer.read()
    if args[:1] == ["base64"]:
        sys.stdout.write(base64.b64encode(data).decode())
    else:
        sys.stdout.buffer.write(b"signature")
    return 0


def main() -> int:
    tool = Path(sys.argv[0]).name
    args = sys.argv[1:]
    if tool == "id":
        print(0)
        return 0
    if tool == "skopeo":
        print(DIGEST)
        return 0
    if tool == "flock":
        return 0
    fake = {"podman": podman, "unshare": unshare, "curl": curl, "openssl": openssl}[tool]
    return fake(args)


if __name__ == "__main__":
    sys.exit(main())
