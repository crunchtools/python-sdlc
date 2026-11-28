#!/usr/bin/env python3
"""`sdlc_jobs/_disk.py`'s pure functions: what the image job builds and promote re-validates.

The disk's table, its root keys, BIB's config, the room /mnt must have, and the artifact's
manifest, index and annotations. Nothing here runs podman.

Run: tests/test_disk.py   (needs python3 only)
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdlc_jobs import _disk
from sdlc_jobs._core import CannotRunError, images
from sdlc_jobs._disk import GIB, Disk

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGl0ZXN0a2V5"
RSA = "ssh-rsa AAAAB3NzaC1yc2E="
REF = "quay.io/crunchtools/demo"
DIGEST = "sha256:" + "b" * 64
SHA = "0123456789abcdef0123456789abcdef01234567"
BLOB = "d" * 64
SIZE = 1234
SCHEMA = 2
LABELS = {
    "org.opencontainers.image.revision": SHA,
    "org.opencontainers.image.source": "https://github.com/crunchtools/demo",
    "org.opencontainers.image.base.name": "ignored",
    "com.crunchtools.build.run": "https://github.com/crunchtools/demo/actions/runs/7",
    "com.example.other": "dropped",
}


def refused(call: Callable[..., object], *args: object) -> str:
    """The message of the refusal (exit 2) `call(*args)` raises, or a failed assertion."""
    try:
        call(*args)
    except CannotRunError as exc:
        return str(exc)
    raise AssertionError("expected a refusal (exit 2)")


def good(disk: Disk | None = None) -> str:
    disk = disk or Disk("raw", "xfs")
    notes = _disk.notes_for(LABELS, REF, DIGEST, disk)
    return _disk.manifest(BLOB, SIZE, notes).decode()


def test_need_grows_with_the_image_and_is_at_least_a_ten_gib_raw() -> None:
    small, big = _disk.need(GIB), _disk.need(20 * GIB)
    assert small >= 10 * GIB + 3 * GIB + _disk.MARGIN
    assert big > small and big == (40 + 1 + 2 * 20 + 2 * 20) * GIB + _disk.MARGIN


def test_room_takes_the_whole_uid_range_and_enough_free_space() -> None:
    wanted = _disk.need(2 * GIB)
    ok = f"uid_map: {_disk.FULL_UID_MAP}\nroot: 5\nmnt: {wanted}\n"
    _disk.room(ok, 2 * GIB)
    room = _disk.room
    assert "GiB free" in refused(room, ok.replace(str(wanted), str(wanted - 1)), 2 * GIB)
    assert "uid_map" in refused(room, ok.replace(_disk.FULL_UID_MAP, "0 0 1"), 2 * GIB)
    assert "uid_map" in refused(room, "root: 5\nmnt: 99999999999999\n", 2 * GIB)
    assert "free space" in refused(room, f"uid_map: {_disk.FULL_UID_MAP}\nmnt: x\n", 2 * GIB)
    assert "free space" in refused(room, f"uid_map: {_disk.FULL_UID_MAP}\n", 2 * GIB)


def test_runner_ok_only_on_a_github_hosted_runner() -> None:
    _disk.runner_ok({"RUNNER_ENVIRONMENT": "github-hosted"})
    assert "self-hosted" in refused(_disk.runner_ok, {"RUNNER_ENVIRONMENT": "self-hosted"})
    assert "unset" in refused(_disk.runner_ok, {})


def test_root_keys_keeps_the_key_and_drops_comments_options_and_blanks() -> None:
    text = f"# note\n\n{KEY} me@host\n  {RSA}\n"
    assert _disk.root_keys(text, "keys") == (KEY, RSA)


def test_root_keys_refuses_a_line_that_is_not_a_key() -> None:
    for line in (
        'name = "x"',
        f'command="true" {KEY}',
        "ssh-ed25519",
        "ssh-ed25519 not!base64",
        f"{KEY}\n[[customizations.user]]",
        f"{KEY}\x00",
        "ssh-dss AAAA",
    ):
        assert "keys" in refused(_disk.root_keys, line, "keys"), line
    assert "no SSH public key" in refused(_disk.root_keys, "# only a comment\n\n", "keys")


def test_config_toml_parses_and_carries_exactly_the_keys() -> None:
    assert _disk.config_toml(Disk("raw", "xfs")) == ""
    doc = tomllib.loads(_disk.config_toml(Disk("raw", "xfs", (KEY, RSA))))
    assert doc == {"customizations": {"user": [{"name": "root", "key": f"{KEY}\n{RSA}"}]}}


def test_disk_settings_defaults_and_refusals() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "keys").write_text(f"# c\n{KEY}\n")
        assert _disk_of(root, {}) is None
        assert _disk_of(root, {"disk": {}}) == Disk("raw", "xfs")
        assert _disk_of(root, {"disk": {"rootfs": "ext4", "root-keys": "keys"}}) == Disk(
            "raw", "ext4", (KEY,)
        )
        for bad in (
            3,
            {"entrypoint": "sh"},
            {"type": "qcow2"},
            {"rootfs": "btrfs"},
            {"root-keys": "../keys"},
            {"root-keys": "/keys"},
            {"root-keys": "missing"},
        ):
            assert "[tool.sdlc.image.disk]" in refused(_disk_of, root, {"disk": bad}), bad


def _disk_of(root: Path, settings: dict) -> Disk | None:
    return _disk.disk_settings(root, settings)


def test_the_gated_tag_names_the_commit() -> None:
    assert _disk.gated_tag(SHA) == f"disk-gated-{SHA}"


def test_notes_keep_the_provenance_labels_and_name_the_image_and_builder() -> None:
    notes = _disk.notes_for(LABELS, REF, DIGEST, Disk("raw", "ext4"))
    assert notes == {
        "org.opencontainers.image.revision": SHA,
        "org.opencontainers.image.source": "https://github.com/crunchtools/demo",
        "com.crunchtools.build.run": LABELS["com.crunchtools.build.run"],
        _disk.BASE_NAME: REF,
        _disk.BASE_DIGEST: DIGEST,
        _disk.BUILDER: _disk.BIB,
        _disk.DISK_TYPE: "raw",
        _disk.DISK_ROOTFS: "ext4",
    }


def test_manifest_is_one_zstd_layer_around_an_empty_config() -> None:
    raw = _disk.manifest(BLOB, SIZE, {"b": "2", "a": "1"})
    doc = json.loads(raw)
    assert doc["artifactType"] == _disk.ARTIFACT_TYPE and doc["schemaVersion"] == SCHEMA
    assert doc["config"] == {
        "mediaType": _disk.EMPTY_TYPE,
        "digest": _disk.EMPTY_DIGEST,
        "size": len(b"{}"),
    }
    (layer,) = doc["layers"]
    assert layer["digest"] == f"sha256:{BLOB}" and layer["size"] == SIZE
    assert layer["mediaType"] == _disk.LAYER_TYPE
    assert list(doc["annotations"]) == ["a", "b"]  # sorted, so the bytes are stable
    assert raw == _disk.manifest(BLOB, SIZE, {"a": "1", "b": "2"}) and b" " not in raw
    assert "sha256:" + hashlib.sha256(b"{}").hexdigest() == _disk.EMPTY_DIGEST


def test_index_names_the_manifest_by_its_digest_and_size() -> None:
    raw = _disk.manifest(BLOB, SIZE, {})
    (entry,) = json.loads(_disk.index(raw))["manifests"]
    assert entry["digest"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert entry["size"] == len(raw) and entry["artifactType"] == _disk.ARTIFACT_TYPE
    assert entry["annotations"] == {"org.opencontainers.image.ref.name": "disk"}


def test_check_manifest_accepts_what_the_image_job_pushes() -> None:
    for disk in (Disk("raw", "xfs"), Disk("raw", "ext4", (KEY,))):
        notes = _disk.check_manifest(good(disk), REF, DIGEST, SHA)
        assert notes[_disk.DISK_ROOTFS] == disk.rootfs and notes[_disk.BUILDER] == _disk.BIB


def test_check_manifest_refuses_what_is_not_this_commits_disk() -> None:
    def changed(key: str, value: str) -> str:
        doc = json.loads(good())
        doc["annotations"][key] = value
        return json.dumps(doc)

    check = _disk.check_manifest
    assert "revision" in refused(check, good(), REF, DIGEST, "f" * 40)
    assert "base.digest" in refused(check, good(), REF, "sha256:" + "c" * 64, SHA)
    assert "base.name" in refused(check, good(), REF + "-other", DIGEST, SHA)
    assert "sha256 digest" in refused(check, good(), REF, "latest", SHA)
    for builder in ("quay.io/centos-bootc/bootc-image-builder:latest", "evil@sha256:" + "a" * 64):
        assert "builder" in refused(check, changed(_disk.BUILDER, builder), REF, DIGEST, SHA)
    assert "type" in refused(check, changed(_disk.DISK_TYPE, "qcow2"), REF, DIGEST, SHA)
    assert "type" in refused(check, changed(_disk.DISK_ROOTFS, "btrfs"), REF, DIGEST, SHA)


def test_check_manifest_refuses_a_manifest_of_another_shape() -> None:
    doc = json.loads(good())
    for edit in (
        lambda d: d.update(artifactType="application/vnd.oci.image.config.v1+json"),
        lambda d: d["layers"].append(d["layers"][0]),
        lambda d: d.update(layers=[]),
        lambda d: d["layers"][0].update(mediaType="application/x"),
        lambda d: d["config"].update(mediaType="application/x"),
        lambda d: d.update(config=[]),
        lambda d: d.pop("annotations"),
    ):
        broken = json.loads(json.dumps(doc))
        edit(broken)
        assert "disk" in refused(_disk.check_manifest, json.dumps(broken), REF, DIGEST, SHA)
    assert "not JSON" in refused(_disk.check_manifest, "not json", REF, DIGEST, SHA)
    assert "not an object" in refused(_disk.check_manifest, "[]", REF, DIGEST, SHA)


def test_the_helper_scripts_do_what_the_job_relies_on() -> None:
    """The fake answers each script's output, so its text is held here."""
    prepare = _disk.PREPARE
    assert "apparmor_restrict_unprivileged_userns" in prepare
    assert 'echo "uid_map: ' in prepare and "/proc/self/uid_map" in prepare
    assert 'echo "root: $(df -B1 --output=avail /host' in prepare
    assert 'echo "mnt: $(df -B1 --output=avail /mnt' in prepare
    for sub in ("output", "var-tmp", "store", "graph", "graph-run"):
        assert f'"$W/{sub}"' in prepare, sub
    seed = _disk.SEED
    assert "skopeo copy --authfile /auth.json --preserve-digests" in seed
    assert '"containers-storage:[overlay@$W/graph+$W/graph-run]$SRC"' in seed
    pack = _disk.PACK
    assert "zstd -q -T0 -3 --rm -f output/image/disk.raw -o disk.raw.zst" in pack
    assert "sha256sum disk.raw.zst" in pack and "stat -c %s disk.raw.zst" in pack
    push = _disk.PUSH
    assert 'mv disk.raw.zst "layout/blobs/sha256/$BLOB"' in push
    assert '"$MANIFEST" > "layout/blobs/sha256/$MANIFEST_HEX"' in push
    assert '"$INDEX" > layout/index.json' in push
    assert "--preserve-digests --digestfile pushed.digest" in push
    assert 'oci:layout:disk "docker://$DEST"' in push and "cat pushed.digest" in push
    assert _disk.WIPE.strip() == 'rm -rf "$W"'


def test_bib_is_pinned_by_digest() -> None:
    assert _disk.BIB_PINNED.fullmatch(_disk.BIB)


def test_rhel10_bootc_server_can_declare_its_disk() -> None:
    """Its [tool.sdlc.image] as it is today, plus the table scripts/disk.sh retires into."""
    toml = """[tool.sdlc]
repo-kinds = ["bootc-image"]

[tool.sdlc.image]
image = "quay.io/crunchtools/rhel10-bootc-server"
format = "oci"
redhat-registry = true
rhsm = true
boot = "none"
boot-none-reason = "a bootc host: PID 1 is systemd; the bootc-lint gate judges the image"

[tool.sdlc.image.disk]
type = "raw"
rootfs = "xfs"
root-keys = "install/root-authorized-keys"
"""
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOk0bF9vYm9vdGNzZXJ2ZXJyb290a2V5"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "pyproject.toml").write_text(toml)
        (root / "install").mkdir()
        (root / "install" / "root-authorized-keys").write_text(f"# root, lotor\n{key} root@lotor\n")
        (root / "Containerfile").write_text("FROM registry.redhat.io/rhel10/rhel-bootc:10.1\n")
        settings = images(root, "crunchtools/rhel10-bootc-server")[0].settings
        disk = _disk.disk_settings(root, settings)
    assert disk == Disk("raw", "xfs", (key,)), disk


def main() -> int:
    tests: list[Callable[[], None]] = [v for k, v in globals().items() if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"ok   {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
