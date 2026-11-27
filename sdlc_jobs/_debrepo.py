"""A flat APT repository index in pure Python: `Packages`, `Packages.gz`, `Release`.

Replaces `apt-ftparchive packages .` and `apt-ftparchive release .`: the sdlc
image is RPM-based and carries no apt-utils. The `.deb` is release input a
build produced, so it is parsed defensively: only the control tarball is read,
every size is bounded, and anything malformed is a refusal (exit 2), never a
guess. Nothing is extracted to disk and no member path is ever used as one.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib
import io
import re
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from sdlc_jobs._core import cannot

if TYPE_CHECKING:
    from collections.abc import Mapping


try:
    importlib.import_module("compression.zstd")  # Python 3.14+: tarfile reads .zst through it

    HAVE_ZSTD = True
except ImportError:
    HAVE_ZSTD = False

AR_MAGIC = b"!<arch>\n"
AR_HEADER = 60
MAX_CONTROL = 1 << 20  # bytes of control file; real ones are a few KiB
MAX_TARBALL = 16 << 20  # bytes of control.tar.*: bounds what we read before decompressing
CHUNK = 1 << 20  # bytes per read when hashing: a MiB keeps memory flat on a large .deb
COMPRESSION: dict[str, str] = {"": "r:", ".gz": "r:gz", ".xz": "r:xz", ".zst": "r:zst"}
COMPUTED = ("Filename", "Size", "MD5sum", "SHA1", "SHA256")
FIELD = re.compile(r"([A-Za-z0-9][A-Za-z0-9._+-]*):[ \t]*(.*)")
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def ar_members(deb: Path) -> dict[str, bytes]:
    """The `debian-binary` and `control.tar*` members of an ar archive; the rest are skipped.

    Seeks past data.tar, which is most of the file and none of our business.
    """
    found: dict[str, bytes] = {}
    with deb.open("rb") as fh:
        if fh.read(len(AR_MAGIC)) != AR_MAGIC:
            cannot(f"{deb.name} is not an ar archive (bad magic)")
        order = 0
        while header := fh.read(AR_HEADER):
            if len(header) != AR_HEADER or header[58:60] != b"`\n":
                cannot(f"{deb.name} has a malformed ar header")
            name = header[:16].decode("ascii", "replace").strip().rstrip("/")
            try:
                size = int(header[48:58])
            except ValueError:
                cannot(f"{deb.name} has a malformed ar member size")
            if order == 0 and name != "debian-binary":
                cannot(f"{deb.name}: first member is {name!r}, not debian-binary")
            order += 1
            if name == "debian-binary" or name.startswith("control.tar"):
                if size > MAX_TARBALL:
                    cannot(f"{deb.name}: member {name} is {size} bytes, over the limit")
                if name in found:
                    cannot(f"{deb.name} has two {name} members")
                body = fh.read(size)
                if len(body) != size:
                    cannot(f"{deb.name} is truncated in {name}")
                found[name] = body
                fh.seek(size % 2, 1)
            else:
                fh.seek(size + size % 2, 1)
    return found


def parse_stanza(text: str, where: str) -> dict[str, str]:
    """One deb822 stanza; a continuation line stays in its field's value, newline and all."""
    fields: dict[str, str] = {}
    last = ""
    blank = False
    for line in text.split("\n"):
        if not line.strip():
            blank = blank or bool(fields)
            continue
        if blank:
            cannot(f"{where} holds more than one stanza")
        if line[0] in " \t":
            if not last:
                cannot(f"{where} starts with a continuation line")
            fields[last] += "\n" + line
            continue
        m = FIELD.fullmatch(line)
        if not m:
            cannot(f"{where} has a malformed line: {line[:60]!r}")
        last = m.group(1)
        if last.lower() in {k.lower() for k in fields}:
            cannot(f"{where} has the field {last} twice")
        fields[last] = m.group(2)
    if not fields:
        cannot(f"{where} is empty")
    return fields


def read_control(deb: str, name: str, blob: bytes, mode: str) -> bytes:
    """The bytes of the one regular `control` member; any unsafe member path is refused."""
    control: bytes | None = None
    try:  # cast: typeshed's tarfile.open overloads do not list r:zst (3.14) yet
        with tarfile.open(fileobj=io.BytesIO(blob), mode=cast("Any", mode)) as tar:
            for member in tar:
                parts = member.name.split("/")
                if member.name.startswith("/") or ".." in parts:
                    cannot(f"{deb}: control member path {member.name!r} escapes")
                if [p for p in parts if p not in ("", ".")] != ["control"]:
                    continue
                if not member.isreg():
                    cannot(f"{deb}: control is not a regular file")
                if control is not None:
                    cannot(f"{deb} has two control members")
                if member.size > MAX_CONTROL:
                    cannot(f"{deb}: control is {member.size} bytes, over 1 MiB")
                fobj = tar.extractfile(member)
                control = fobj.read(MAX_CONTROL) if fobj else b""
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        cannot(f"{deb}: {name} is unreadable: {exc}")
    if control is None:
        cannot(f"{deb}: {name} has no control file")
    return control


def control_of(deb: Path) -> dict[str, str]:
    """The control fields of a .deb, in file order."""
    members = ar_members(deb)
    if members.get("debian-binary") != b"2.0\n":
        cannot(f"{deb.name}: debian-binary is missing or not 2.0")
    tarballs = [n for n in members if n.startswith("control.tar")]
    if len(tarballs) != 1:
        cannot(f"{deb.name} needs exactly one control.tar member, has {len(tarballs)}")
    name = tarballs[0]
    mode = COMPRESSION.get(name.removeprefix("control.tar"))
    if mode is None or (mode == "r:zst" and not HAVE_ZSTD):
        cannot(f"{deb.name}: {name} uses a compression this Python cannot read")
    control = read_control(deb.name, name, members[name], mode)
    try:
        text = control.decode("utf-8")
    except UnicodeDecodeError:
        cannot(f"{deb.name}: control is not UTF-8")
    return parse_stanza(text, f"{deb.name} control")


def digests(path: Path) -> tuple[int, dict[str, str]]:
    """Size and the three hex digests, in one streaming pass."""
    hashers = {
        k: hashlib.new(a) for k, a in (("MD5sum", "md5"), ("SHA1", "sha1"), ("SHA256", "sha256"))
    }
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            size += len(chunk)
            for h in hashers.values():
                h.update(chunk)
    return size, {k: h.hexdigest() for k, h in hashers.items()}


def packages_stanza(deb: Path, filename: str) -> str:
    """The control fields as written, then Filename, Size, MD5sum, SHA1, SHA256."""
    fields = control_of(deb)
    clash = [k for k in fields if k.lower() in {c.lower() for c in COMPUTED}]
    if clash:
        cannot(f"{deb.name} control already carries {', '.join(clash)}")
    size, sums = digests(deb)
    fields.update({"Filename": filename, "Size": str(size), **sums})
    return "".join(f"{k}: {v}\n" for k, v in fields.items())


def gzip_n(payload: bytes) -> bytes:
    """`gzip -9n`: no name, no mtime, so the same input is the same bytes."""
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mtime=0, compresslevel=9, fileobj=buf, mode="wb") as gz:
        gz.write(payload)
    return buf.getvalue()


def write_packages(directory: Path) -> list[Path]:
    """`Packages` and `Packages.gz` for every regular .deb directly in `directory`.

    Symlinks are skipped, not followed: a link must not pull a file from
    outside the directory into the index.
    """
    debs = sorted(p for p in directory.glob("*.deb") if p.is_file() and not p.is_symlink())
    text = "".join(packages_stanza(p, f"./{p.name}") + "\n" for p in debs)
    plain, packed = directory / "Packages", directory / "Packages.gz"
    plain.write_bytes(text.encode())
    packed.write_bytes(gzip_n(text.encode()))
    return [plain, packed]


def rfc2822_utc(when: datetime) -> str:
    if when.tzinfo is None:
        cannot("the Release date has no timezone")
    w = when.astimezone(UTC)
    return f"{DAYS[w.weekday()]}, {w.day:02d} {MONTHS[w.month - 1]} {w.year} {w:%H:%M:%S} UTC"


def write_release(directory: Path, head: Mapping[str, str], date: datetime) -> Path:
    """`Release` listing `Packages` and `Packages.gz`; `date` is passed in for determinism.

    `head` is the header in order: Origin, Label, Suite, Codename.
    """
    for key, value in head.items():
        if not value or "\n" in value or "\r" in value:
            cannot(f"Release {key} is empty or has a newline")
    lines = [f"{k}: {v}" for k, v in head.items()] + [f"Date: {rfc2822_utc(date)}"]
    names = ("Packages", "Packages.gz")
    sums = {n: digests(directory / n) for n in names}
    for section in ("MD5sum", "SHA1", "SHA256"):
        lines.append("MD5Sum:" if section == "MD5sum" else f"{section}:")
        lines += [f" {sums[n][1][section]} {sums[n][0]:>16} {n}" for n in names]
    path = directory / "Release"
    path.write_text("\n".join(lines) + "\n")
    return path
