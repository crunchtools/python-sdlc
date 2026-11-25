#!/usr/bin/env python3
"""host_mount and image_of on podman's Docker API, as the Exedore runner VMs serve it.

  - boot's sibling of the job container (`docker run --volumes-from`) lists no
    Mounts of its own on podman, only HostConfig.VolumesFrom; host_mount follows
    that to the job container and maps the workspace through its mounts
  - a container run by digest through podman's Docker API records the image's
    tag as ImageName; image_of pins it again from ImageDigest
  - Docker's shape (inherited mounts listed, ImageName absent) still works

Run: python3 tests/test_event_podman_compat.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import sdlc_root  # noqa: F401
from fake_github import check, finish

from sdlc_jobs import _core

DIGEST = "sha256:" + "ab" * 32
REPO = "quay.io/crunchtools/sdlc"
SIB = "f" * 64
JOB = "a" * 64


class Fake(_core.Tools):
    def __init__(self, docs: dict[str, dict[str, Any]]) -> None:
        self.docs = docs

    def run(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        del kw
        assert argv[:3] == ["podman", "--remote", "--url"], argv
        assert argv[-2] == "inspect", argv
        doc = self.docs.get(argv[-1])
        if doc is None:
            return subprocess.CompletedProcess(argv, 125, "", "no such container")
        return subprocess.CompletedProcess(argv, 0, json.dumps([doc]), "")


def mapped(docs: dict[str, dict[str, Any]], root: str) -> tuple[str, str]:
    orig = _core.own_container_id
    _core.own_container_id = lambda *_: SIB  # type: ignore[assignment]
    try:
        return _core.host_mount(Fake(docs), {}, Path(root))
    finally:
        _core.own_container_id = orig  # type: ignore[assignment]


podman_sibling = {
    SIB: {
        "Mounts": [],
        "HostConfig": {"VolumesFrom": [JOB]},
        "ImageName": f"{REPO}:0",
        "ImageDigest": DIGEST,
        "Image": "7eeeaa",
    },
    JOB: {"Mounts": [{"Source": "/home/runner/work", "Destination": "/__w"}]},
}
check(
    "podman: the workspace maps through the --volumes-from source, the image pins by digest",
    mapped(podman_sibling, "/__w/deployer/deployer")
    == ("/home/runner/work/deployer/deployer", f"{REPO}@{DIGEST}"),
)

with_mode = {
    **podman_sibling,
    SIB: {**podman_sibling[SIB], "HostConfig": {"VolumesFrom": [f"{JOB}:ro"]}},
}
check(
    "podman: a VolumesFrom entry with a mode names the same container",
    mapped(with_mode, "/__w/x/x")[0] == "/home/runner/work/x/x",
)

docker_sibling = {
    SIB: {
        "Mounts": [{"Source": "/home/runner/work", "Destination": "/__w"}],
        "HostConfig": {"VolumesFrom": [JOB]},
        "ImageName": f"{REPO}@{DIGEST}",
        "Image": "sha256:local",
    },
    JOB: {"Mounts": [{"Source": "/home/runner/work", "Destination": "/__w"}]},
}
check(
    "docker: inherited mounts listed on the sibling still map",
    mapped(docker_sibling, "/__w/r/r") == ("/home/runner/work/r/r", f"{REPO}@{DIGEST}"),
)

check(
    "image_of: a registry with a port keeps its port when the tag is dropped",
    _core.image_of({"ImageName": "reg:5000/a/b:0", "ImageDigest": DIGEST})
    == f"reg:5000/a/b@{DIGEST}",
)
check(
    "image_of: no digest anywhere falls back to the image id",
    _core.image_of({"ImageName": f"{REPO}:0", "Image": "7eeeaa"}) == "7eeeaa",
)

if __name__ == "__main__":
    sys.exit(finish())
