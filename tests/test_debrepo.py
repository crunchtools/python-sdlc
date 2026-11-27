#!/usr/bin/env python3
"""`sdlc_jobs._debrepo`: a flat APT index from stdlib alone, refusing malformed debs.

Builds tiny .deb files (an ar archive of debian-binary, control.tar.*, data.tar.gz)
with the stdlib, then checks field order, hashes, byte-stable Packages.gz, the
Release file, and each refusal. Compares with `apt-ftparchive` when it exists.

Run: tests/test_debrepo.py   (needs python3 only)
"""

from __future__ import annotations

import gzip
import hashlib
import io
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdlc_jobs import _debrepo
from sdlc_jobs._core import CannotRunError

CONTROL = (
    "Package: hello\nVersion: 1.0-1\nArchitecture: amd64\nMaintainer: A <a@example.com>\n"
    "Installed-Size: 12\nDescription: a greeting\n The long part.\n .\n Second paragraph.\n"
)
DATE = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)

type WriteMode = Literal["w:", "w:gz", "w:xz", "w:zst"]
WRITE_MODES: dict[str, WriteMode] = {".gz": "w:gz", ".xz": "w:xz", ".zst": "w:zst", "": "w:"}


def tar_of(entries: list[tuple[str, bytes, bytes]], mode: WriteMode) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tar:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            if kind == b"l":
                info.type, info.linkname, info.size = tarfile.SYMTYPE, "/etc/passwd", 0
            tar.addfile(info, io.BytesIO(data) if info.size else None)
    return buf.getvalue()


def ar_member(name: str, data: bytes) -> bytes:
    head = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode()
    return head + data + (b"\n" if len(data) % 2 else b"")


def make_deb(
    path: Path,
    control: str = CONTROL,
    ext: str = ".gz",
    entries: list[tuple[str, bytes, bytes]] | None = None,
    lead: tuple[bytes, bytes] = (b"!<arch>\n", b"2.0\n"),  # (ar magic, debian-binary body)
) -> Path:
    magic, binary = lead
    mode = WRITE_MODES[ext]
    ents = [("./control", control.encode(), b"")] if entries is None else entries
    data = tar_of([("./usr/share/hello", b"hi\n", b"")], "w:gz")
    path.write_bytes(
        magic
        + ar_member("debian-binary", binary)
        + ar_member(f"control.tar{ext}", tar_of(ents, mode))
        + ar_member("data.tar.gz", data)
    )
    return path


def refusal(fn: Callable[[], object]) -> str:
    """The message of the CannotRunError `fn` raises; no refusal is a failed test."""
    try:
        fn()
    except CannotRunError as exc:
        return str(exc)
    raise AssertionError("no refusal")


def test_fields_and_order_kept() -> None:
    with tempfile.TemporaryDirectory() as t:
        got = _debrepo.control_of(make_deb(Path(t) / "a.deb"))
    assert list(got) == [
        "Package",
        "Version",
        "Architecture",
        "Maintainer",
        "Installed-Size",
        "Description",
    ]
    assert got["Description"] == "a greeting\n The long part.\n .\n Second paragraph."


def test_compressions() -> None:
    exts = [".gz", ".xz", ""] + ([".zst"] if _debrepo.HAVE_ZSTD else [])
    for ext in exts:
        with tempfile.TemporaryDirectory() as t:
            got = _debrepo.control_of(make_deb(Path(t) / "a.deb", ext=ext))
        assert got["Package"] == "hello", ext
    if not _debrepo.HAVE_ZSTD:
        print("note: compression.zstd missing, .zst variant skipped")


def test_stanza_has_hashes_and_size() -> None:
    with tempfile.TemporaryDirectory() as t:
        deb = make_deb(Path(t) / "a.deb")
        raw = deb.read_bytes()
        stanza = _debrepo.packages_stanza(deb, "./a.deb")
    assert stanza.startswith(CONTROL)
    md5 = hashlib.new("md5", raw, usedforsecurity=False).hexdigest()
    sha1 = hashlib.new("sha1", raw, usedforsecurity=False).hexdigest()
    sha256 = hashlib.sha256(raw).hexdigest()
    assert stanza[len(CONTROL) :] == (
        f"Filename: ./a.deb\nSize: {len(raw)}\nMD5sum: {md5}\nSHA1: {sha1}\nSHA256: {sha256}\n"
    )


def test_packages_files_stable() -> None:
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        make_deb(d / "b.deb", CONTROL.replace("hello", "bravo"))
        make_deb(d / "a.deb")
        plain, packed = _debrepo.write_packages(d)
        first = (plain.read_bytes(), packed.read_bytes())
        _debrepo.write_packages(d)
        assert (plain.read_bytes(), packed.read_bytes()) == first
        assert gzip.decompress(first[1]) == first[0]
        text = first[0].decode()
        assert text.index("Package: hello") < text.index("Package: bravo")  # a.deb first
        assert "\n\nPackage: bravo" in text and text.endswith("\n\n")
        assert first[1][3] == 0 and first[1][4:8] == b"\0\0\0\0"  # no name flag, no mtime


def test_release() -> None:
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        make_deb(d / "a.deb")
        _debrepo.write_packages(d)
        rel = _debrepo.write_release(
            d, {"Origin": "O", "Label": "L", "Suite": "stable", "Codename": "c"}, DATE
        ).read_text()
        lines = rel.split("\n")
        assert lines[:5] == [
            "Origin: O",
            "Label: L",
            "Suite: stable",
            "Codename: c",
            "Date: Fri, 27 Nov 2026 12:00:00 UTC",
        ]
        for head, algo in (("MD5Sum:", "md5"), ("SHA1:", "sha1"), ("SHA256:", "sha256")):
            i = lines.index(head)
            for n, line in zip(("Packages", "Packages.gz"), lines[i + 1 : i + 3], strict=True):
                data = (d / n).read_bytes()
                assert line == f" {hashlib.new(algo, data).hexdigest()} {len(data):>16} {n}"


def test_bad_magic() -> None:
    with tempfile.TemporaryDirectory() as t:
        deb = make_deb(Path(t) / "a.deb", lead=(b"!<arcX>\n", b"2.0\n"))
        assert "bad magic" in refusal(lambda: _debrepo.control_of(deb))


def raw_deb(path: Path, name: str, size_field: bytes) -> Path:
    """A deb whose second member `name` carries `size_field` verbatim, then 60 filler bytes."""
    head = (
        f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}".encode() + size_field.ljust(10) + b"`\n"
    )
    path.write_bytes(b"!<arch>\n" + ar_member("debian-binary", b"2.0\n") + head + b"x" * 60)
    return path


def refusal_fast(fn: Callable[[], object]) -> str:
    """`refusal`, but a regression that loops forever fails after 5 seconds, not never."""

    def stuck(*_: object) -> None:
        raise AssertionError("ar_members did not return")

    old = signal.signal(signal.SIGALRM, stuck)
    signal.alarm(5)
    try:
        return refusal(fn)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def test_malformed_ar_size() -> None:
    for name in ("junk", "control.tar.gz"):
        for field in (b"-60", b"+60", b"1_0", b"0x10", b"", b"6 0"):
            with tempfile.TemporaryDirectory() as t:
                deb = raw_deb(Path(t) / "a.deb", name, field)
                msg = refusal_fast(lambda d=deb: _debrepo.control_of(d))
                assert "malformed ar member size" in msg, (name, field, msg)


def test_too_many_members() -> None:
    with tempfile.TemporaryDirectory() as t:
        junk = [(f"./f{i}", b"", b"") for i in range(_debrepo.MAX_MEMBERS)]
        deb = make_deb(Path(t) / "a.deb", entries=[*junk, ("./control", CONTROL.encode(), b"")])
        assert "members" in refusal(lambda: _debrepo.control_of(deb))


def test_data_tar_exactly_one() -> None:
    with tempfile.TemporaryDirectory() as t:
        deb = make_deb(Path(t) / "a.deb")
        whole = deb.read_bytes()
        cut = whole.index(b"data.tar.gz/")
        deb.write_bytes(whole[:cut])
        assert "exactly one data.tar member, has 0" in refusal(lambda: _debrepo.control_of(deb))
        deb.write_bytes(whole + ar_member("data.tar.xz", b"zz"))
        assert "exactly one data.tar member, has 2" in refusal(lambda: _debrepo.control_of(deb))


def test_required_fields() -> None:
    for field in ("Package", "Version", "Architecture"):
        with tempfile.TemporaryDirectory() as t:
            lines = [ln for ln in CONTROL.splitlines(True) if not ln.startswith(field)]
            deb = make_deb(Path(t) / "a.deb", control="".join(lines))
            assert field.lower() in refusal(lambda d=deb: _debrepo.control_of(d))
    with tempfile.TemporaryDirectory() as t:  # deb822 names are case-insensitive
        deb = make_deb(Path(t) / "a.deb", control=CONTROL.replace("Package:", "package:", 1))
        assert _debrepo.control_of(deb)["package"] == "hello"


def test_bad_debian_binary() -> None:
    with tempfile.TemporaryDirectory() as t:
        deb = make_deb(Path(t) / "a.deb", lead=(b"!<arch>\n", b"3.0\n"))
        assert "not 2.0" in refusal(lambda: _debrepo.control_of(deb))


def test_missing_control() -> None:
    with tempfile.TemporaryDirectory() as t:
        deb = make_deb(Path(t) / "a.deb", entries=[("./postinst", b"#!/bin/sh\n", b"")])
        assert "no control file" in refusal(lambda: _debrepo.control_of(deb))


def test_escaping_member() -> None:
    for name in ("../control", "/control", "./a/../control"):
        with tempfile.TemporaryDirectory() as t:
            deb = make_deb(Path(t) / "a.deb", entries=[(name, CONTROL.encode(), b"")])
            assert "escapes" in refusal(lambda d=deb: _debrepo.control_of(d))


def test_control_not_regular() -> None:
    with tempfile.TemporaryDirectory() as t:
        deb = make_deb(Path(t) / "a.deb", entries=[("./control", b"", b"l")])
        assert "not a regular file" in refusal(lambda: _debrepo.control_of(deb))


def test_two_stanzas() -> None:
    with tempfile.TemporaryDirectory() as t:
        deb = make_deb(Path(t) / "a.deb", control=CONTROL + "\n" + CONTROL)
        assert "more than one stanza" in refusal(lambda: _debrepo.control_of(deb))


def test_oversize_control() -> None:
    with tempfile.TemporaryDirectory() as t:
        deb = make_deb(Path(t) / "a.deb", control=CONTROL + "X: " + "a" * (1 << 20) + "\n")
        assert "over 1 MiB" in refusal(lambda: _debrepo.control_of(deb))


def test_precomputed_field() -> None:
    with tempfile.TemporaryDirectory() as t:
        deb = make_deb(Path(t) / "a.deb", control=CONTROL + "SHA256: abc\n")
        assert "already carries" in refusal(lambda: _debrepo.packages_stanza(deb, "./a.deb"))


def test_symlinked_deb_skipped() -> None:
    with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as out:
        make_deb(Path(out) / "real.deb")
        d = Path(t)
        (d / "link.deb").symlink_to(Path(out) / "real.deb")
        _debrepo.write_packages(d)
        assert (d / "Packages").read_text() == ""


def test_against_apt_ftparchive() -> None:
    if not (shutil.which("dpkg-deb") and shutil.which("apt-ftparchive")):
        print("note: dpkg-deb/apt-ftparchive not on PATH, comparison skipped")
        return
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        root = d / "root"
        (root / "DEBIAN").mkdir(parents=True)
        (root / "DEBIAN" / "control").write_text(CONTROL)
        subprocess.run(
            ["dpkg-deb", "--build", str(root), str(d / "hello.deb")],
            check=True,
            capture_output=True,
        )
        want = subprocess.run(
            ["apt-ftparchive", "packages", "."], cwd=d, check=True, capture_output=True, text=True
        ).stdout
        _debrepo.write_packages(d)
        assert (d / "Packages").read_text() == want


def main() -> int:
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                failed += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            else:
                print(f"ok   {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
