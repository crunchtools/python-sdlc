"""What `ai` hands `write`: one JSON result in four job outputs.

Artifacts cannot be made from a `run:` step, so the result travels as job
outputs: gzipped, base64, split into p0..p3 of at most 112,500 bytes each.
Each output reaches `write` as one environment variable, and Linux refuses one
longer than MAX_ARG_STRLEN (128 KiB), so the chunk stays well under it.
A result too big for the four is a failure, never a truncation.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import json
import zlib
from collections.abc import Mapping, Sequence
from typing import Any

CHUNK = 112_500
PARTS = 4
NAMES = tuple(f"p{i}" for i in range(PARTS))
ENV_NAMES = tuple(f"SDLC_AI_{i}" for i in range(PARTS))


class TooLargeError(ValueError):
    """A result too big for p0..p3; `size` is its encoded length."""

    def __init__(self, size: int) -> None:
        super().__init__(f"the result is {size:,} bytes encoded; the cap is {CHUNK * PARTS:,}")
        self.size = size


def encode(result: Mapping[str, Any]) -> list[str]:
    """The result as exactly four strings, empty ones last."""
    raw = json.dumps(result, separators=(",", ":"), sort_keys=True).encode()
    text = base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")
    if len(text) > CHUNK * PARTS:
        raise TooLargeError(len(text))
    return [text[i * CHUNK : (i + 1) * CHUNK] for i in range(PARTS)]


def decode(parts: Sequence[str]) -> dict[str, Any]:
    """The result back from its four parts. Anything malformed is a ValueError."""
    text = "".join(parts)
    if not text:
        raise ValueError("no result was handed on")
    try:
        result = json.loads(gzip.decompress(base64.b64decode(text, validate=True)))
    except (binascii.Error, OSError, EOFError, zlib.error, json.JSONDecodeError) as err:
        raise ValueError(f"the handed-on result is unreadable: {err}") from err
    if isinstance(result, dict):
        return result
    # A ValueError, not a TypeError: callers treat every unusable result alike.
    raise ValueError("the handed-on result is not an object")


def from_env(env: Mapping[str, str]) -> dict[str, Any]:
    """The result `write` was handed, from SDLC_AI_0..3."""
    return decode([env.get(name, "") for name in ENV_NAMES])
