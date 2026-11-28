"""A bootc repo's disk: built by `image` from the gated digest, promoted as an OCI artifact.

`[tool.sdlc.image.disk]` declares one, and only declares it (#273):

    [tool.sdlc.image.disk]
    type = "raw"                                  # the one type today
    rootfs = "xfs"                                # or ext4
    root-keys = "install/root-authorized-keys"    # optional: root's SSH keys

The `image` job holds the registry secrets and the socket, and bootc-image-builder
(BIB) runs privileged through that socket. The repo's config is data: no argv, no
podman option, no BIB config of its own. BIB is digest-pinned here, its flags are
this module's, and the keys file is read by sdlc and written into BIB's config as
validated `<type> <base64>` lines; any other line is refused.

The repo's code does run there, though: osbuild uses the gated image itself as its
buildroot, privileged. So BIB is given nothing worth stealing. It runs with
--network=none and no environment, and it never sees the socket, the auth file or
the engine's own storage, which holds the job container's config and so its
secrets. Its image store is a private one under the work directory, seeded by a
helper in sdlc's own image with only the gated digest. Its mounts are that work
directory (its output, temp, store and image store) and one read-only file, the
BIB config sdlc wrote in RUNNER_TEMP.

The build needs real root, osbuild's user namespaces and more disk than the root
filesystem of GitHub's runner has free. It runs only on a GitHub-hosted runner, a
VM thrown away after the job, where it may set the AppArmor userns sysctl and work
under /mnt, the runner's temp disk; on any other runner it is a 2, so a job never
touches one of our own machines' kernel settings. Before BIB starts, the job logs
the uid_map and the free space of / and /mnt, and refuses (2) a uid_map that is not
the whole range or a /mnt with less free than `need`.

The disk is pushed to the image's own repo as `disk-gated-<sha>`: an OCI 1.1
artifact (`ARTIFACT_TYPE`, empty config), one zstd layer, with annotations naming
the commit, the image digest it was built from and the BIB that built it. promote
re-validates them, signs the artifact with the fleet key, attests the image's own
SBOM and a SLSA v1 provenance for it, and tags it `disk-<tag>` for each tag it
gives the image.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sdlc_jobs._core import DIGEST, cannot, in_checkout

# Pinned by digest: it runs privileged with the registry secrets' socket. The one
# rhel10-bootc-server's disk.yml and scripts/disk.sh proved on GitHub's runner.
BIB = (
    "quay.io/centos-bootc/bootc-image-builder"
    "@sha256:dab86ee6b46dcade411b37f4e70b07d93b6a06da0dbfec6fe89247a220e0d9f8"
)
BIB_PINNED = re.compile(r"quay\.io/centos-bootc/bootc-image-builder@sha256:[0-9a-f]{64}")
ARTIFACT_TYPE = "application/vnd.crunchtools.bootc-disk.v1"
LAYER_TYPE = "application/vnd.crunchtools.bootc-disk.raw.v1+zstd"
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
EMPTY_TYPE = "application/vnd.oci.empty.v1+json"
EMPTY = b"{}"
EMPTY_DIGEST = "sha256:" + hashlib.sha256(EMPTY).hexdigest()
TITLE = "disk.raw.zst"
TAG_PREFIX = "disk-"

TYPES = ("raw",)
ROOTFS = ("xfs", "ext4")
KEYS = {"type", "rootfs", "root-keys"}
SSH_KEY = re.compile(
    r"(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)"
    r"|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)"
    r" [A-Za-z0-9+/]+={0,3}"
)

# Annotations on the artifact's manifest: what promote re-validates and records.
REVISION = "org.opencontainers.image.revision"
SOURCE = "org.opencontainers.image.source"
BASE_NAME = "org.opencontainers.image.base.name"
BASE_DIGEST = "org.opencontainers.image.base.digest"
BUILDER = "com.crunchtools.disk.builder"
DISK_TYPE = "com.crunchtools.disk.type"
DISK_ROOTFS = "com.crunchtools.disk.rootfs"
BUILD = "com.crunchtools.build."  # the image's run labels, carried over for provenance

# Where the work goes: GitHub's runner has ~14 GB free on / and its temp disk on /mnt.
MNT = "/mnt"
GITHUB_HOSTED = "github-hosted"
FULL_UID_MAP = "0 0 4294967295"
GIB = 1024**3
MIN_RAW = 10 * GIB  # BIB's smallest raw root, before /boot and the ESP
MARGIN = 4 * GIB

# Run in sdlc's own image, privileged, on the GitHub runner only (`runner_ok`):
# osbuild's sandbox needs unprivileged user namespaces, which Ubuntu 24.04's
# AppArmor confines (disk.yml, rhel10-bootc-server#51). /host is / read-only.
PREPARE = r"""set -eu
f=/proc/sys/kernel/apparmor_restrict_unprivileged_userns
if [ -e "$f" ]; then echo 0 > "$f"; fi
mkdir -p "$W/output" "$W/var-tmp" "$W/store" "$W/graph" "$W/graph-run"
chmod 0700 "$W"
echo "uid_map: $(tr -s ' \t' ' ' < /proc/self/uid_map | sed 's/^ //; s/ $//' | paste -sd';')"
echo "root: $(df -B1 --output=avail /host | tail -n1 | tr -d ' ')"
echo "mnt: $(df -B1 --output=avail /mnt | tail -n1 | tr -d ' ')"
"""
# BIB's only image store: the gated digest alone, copied by sdlc's skopeo, which
# holds the auth file BIB never sees. Privileged, for the overlay driver's mounts.
SEED = r"""set -eu
for try in 1 2 3; do
  skopeo copy --authfile /auth.json --preserve-digests "docker://$SRC" \
    "containers-storage:[overlay@$W/graph+$W/graph-run]$SRC" && break
  [ "$try" = 3 ] && exit 1
  sleep 20
done
"""
# The raw is sparse; zstd reads its holes as zeros and the copy is ~1/6 of it.
PACK = r"""set -eu
cd "$W"
zstd -q -T0 -3 --rm -f output/image/disk.raw -o disk.raw.zst
echo "$(sha256sum disk.raw.zst | cut -d' ' -f1) $(stat -c %s disk.raw.zst)"
"""
# An OCI layout around the blob, then one push; quay drops a big blob now and then.
PUSH = r"""set -eu
cd "$W"
mkdir -p layout/blobs/sha256
mv disk.raw.zst "layout/blobs/sha256/$BLOB"
printf '%s' '{}' > "layout/blobs/sha256/$EMPTY_HEX"
printf '%s' "$MANIFEST" > "layout/blobs/sha256/$MANIFEST_HEX"
printf '%s' '{"imageLayoutVersion":"1.0.0"}' > layout/oci-layout
printf '%s' "$INDEX" > layout/index.json
for try in 1 2 3; do
  skopeo copy --authfile /auth.json --preserve-digests --digestfile pushed.digest \
    oci:layout:disk "docker://$DEST" && break
  [ "$try" = 3 ] && exit 1
  sleep 20
done
cat pushed.digest
"""
WIPE = r"""rm -rf "$W" """


@dataclass(frozen=True)
class Disk:
    type: str
    rootfs: str
    keys: tuple[str, ...] = ()


def disk_settings(
    root: Path, settings: Mapping, where: str = "[tool.sdlc.image.disk]"
) -> Disk | None:
    """The image's `disk` table, checked; None when it declares none."""
    if "disk" not in settings:
        return None
    given = settings["disk"]
    if not isinstance(given, dict):
        cannot(f"{where} must be a table")
    stray = sorted(set(given) - KEYS)
    if stray:
        cannot(f"{where} has {', '.join(stray)}; a disk declares only {', '.join(sorted(KEYS))}")
    kind, rootfs = given.get("type", "raw"), given.get("rootfs", "xfs")
    if kind not in TYPES:
        cannot(f"{where} type must be one of {', '.join(TYPES)}; got {kind!r}")
    if rootfs not in ROOTFS:
        cannot(f"{where} rootfs must be one of {', '.join(ROOTFS)}; got {rootfs!r}")
    keys: tuple[str, ...] = ()
    if "root-keys" in given:
        path = in_checkout(root, given["root-keys"], f"{where} root-keys", directory=False)
        keys = root_keys((root / path).read_text(errors="replace"), f"{where} root-keys {path!r}")
    return Disk(kind, rootfs, keys)


def root_keys(text: str, where: str) -> tuple[str, ...]:
    """Each `<type> <base64>` of an authorized_keys file; anything else but a comment is a 2.

    Comments and blank lines are skipped; a line with options, or anything else, is
    refused. What reaches BIB's config is only what this module matched, so no line of
    the file can add TOML of its own."""
    keys = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        found = SSH_KEY.match(line)
        if not found or (len(line) > found.end() and not line[found.end()].isspace()):
            cannot(f"{where} line {n} is not an SSH public key")
        keys.append(found.group(0))
    if not keys:
        cannot(f"{where} holds no SSH public key")
    return tuple(keys)


def config_toml(disk: Disk) -> str:
    """BIB's config: root's keys, or nothing."""
    if not disk.keys:
        return ""
    key = json.dumps("\n".join(disk.keys))  # a JSON string of these characters is TOML's too
    return f'[[customizations.user]]\nname = "root"\nkey = {key}\n'


def runner_ok(env: Mapping[str, str]) -> None:
    """A 2 anywhere but GitHub's throwaway runner: the build changes the host it runs on."""
    where = env.get("RUNNER_ENVIRONMENT", "")
    if where != GITHUB_HOSTED:
        cannot(
            f"[tool.sdlc.image.disk] builds only on a GitHub-hosted runner "
            f"(RUNNER_ENVIRONMENT={where or 'unset'}): it sets the host's AppArmor userns "
            "sysctl and works under /mnt, which no job does to one of our own machines"
        )


def need(image_size: int) -> int:
    """Bytes /mnt must have free: the raw (fully allocated, BIB's root is twice the
    image and at least 10 GiB, plus /boot and the ESP), BIB's store (about twice the
    image), the seeded image store and the zstd copy (each at most the image) and a
    margin."""
    raw = max(MIN_RAW, 2 * image_size) + GIB
    return raw + 2 * image_size + 2 * image_size + MARGIN


def room(report: str, image_size: int) -> None:
    """Judge PREPARE's report: the whole uid range, and enough free on /mnt; else a 2."""
    got = dict(line.split(": ", 1) for line in report.splitlines() if ": " in line)
    uid_map = got.get("uid_map", "")
    if uid_map != FULL_UID_MAP:
        cannot(
            f"the engine's uid_map is {uid_map!r}, not {FULL_UID_MAP!r}: osbuild cannot "
            "chown in its build root on this runner, so no disk was built"
        )
    try:
        free = int(got.get("mnt", ""))
    except ValueError:
        cannot(f"could not read the free space of {MNT}: {got.get('mnt')!r}")
    wanted = need(image_size)
    if free < wanted:
        cannot(
            f"{MNT} has {free / GIB:.1f} GiB free; a disk from a {image_size / GIB:.1f} GiB "
            f"image needs {wanted / GIB:.1f} GiB, so no disk was built"
        )


def gated_tag(sha: str) -> str:
    return f"{TAG_PREFIX}gated-{sha}"


def notes_for(labels: Mapping[str, str], ref: str, digest: str, disk: Disk) -> dict[str, str]:
    """The artifact's annotations: the image's build labels, the image it is, and BIB."""
    out = {k: v for k, v in labels.items() if k in (REVISION, SOURCE) or k.startswith(BUILD)}
    out.update({BASE_NAME: ref, BASE_DIGEST: digest, BUILDER: BIB})
    out.update({DISK_TYPE: disk.type, DISK_ROOTFS: disk.rootfs})
    return out


def manifest(blob: str, size: int, notes: Mapping[str, str]) -> bytes:
    """The artifact's manifest, as the exact bytes pushed: `blob` is the layer's hex sha256."""
    doc = {
        "schemaVersion": 2,
        "mediaType": MANIFEST_TYPE,
        "artifactType": ARTIFACT_TYPE,
        "config": {"mediaType": EMPTY_TYPE, "digest": EMPTY_DIGEST, "size": len(EMPTY)},
        "layers": [
            {
                "mediaType": LAYER_TYPE,
                "digest": f"sha256:{blob}",
                "size": size,
                "annotations": {"org.opencontainers.image.title": TITLE},
            }
        ],
        "annotations": dict(sorted(notes.items())),
    }
    return json.dumps(doc, separators=(",", ":")).encode()


def index(raw: bytes) -> bytes:
    """The layout's index.json, naming the manifest `disk`."""
    doc = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": MANIFEST_TYPE,
                "artifactType": ARTIFACT_TYPE,
                "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
                "annotations": {"org.opencontainers.image.ref.name": "disk"},
            }
        ],
    }
    return json.dumps(doc, separators=(",", ":")).encode()


def check_manifest(raw: str, ref: str, image_digest: str, sha: str) -> dict[str, str]:
    """promote's re-validation of an artifact `image` pushed; its annotations, or a 2."""
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        cannot(f"the disk's manifest is not JSON: {exc}")
    if not isinstance(doc, dict):
        cannot("the disk's manifest is not an object")
    config, layers, notes = doc.get("config"), doc.get("layers"), doc.get("annotations")
    if (
        doc.get("artifactType") != ARTIFACT_TYPE
        or not isinstance(config, dict)
        or config.get("mediaType") != EMPTY_TYPE
        or not isinstance(layers, list)
        or len(layers) != 1
        or not isinstance(layers[0], dict)
        or layers[0].get("mediaType") != LAYER_TYPE
        or not isinstance(notes, dict)
    ):
        cannot(f"the artifact at {ref} is not a disk sdlc built: one {LAYER_TYPE} layer")
    if not DIGEST.fullmatch(image_digest):
        cannot(f"{image_digest!r} is not a sha256 digest")
    want = {REVISION: sha, BASE_NAME: ref, BASE_DIGEST: image_digest}
    for key, value in want.items():
        if notes.get(key) != value:
            cannot(f"the disk records {key}={notes.get(key)!r}, not {value!r}")
    # Any pinned BIB, not only this module's: image and promote each bootstrap
    # sdlc:<major> on their own, so an sdlc release between the two jobs of one run
    # can give them different pins. The provenance records the one that built it.
    if not BIB_PINNED.fullmatch(str(notes.get(BUILDER, ""))):
        cannot(f"the disk names builder {notes.get(BUILDER)!r}, not a digest-pinned BIB")
    if notes.get(DISK_TYPE) not in TYPES or notes.get(DISK_ROOTFS) not in ROOTFS:
        cannot("the disk's type or rootfs annotation is not one sdlc builds")
    return {k: str(v) for k, v in notes.items()}
