#!/usr/bin/env python3
"""A gourmand sidecar file with no gourmand.toml must stop the gate, not be ignored.

gourmand resolves `gourmand-exceptions.toml` and its siblings relative to
`gourmand.toml`. With no `gourmand.toml` the sidecar is found on disk, never
parsed, and nothing is printed about it -- so the gate goes red listing
violations that were already justified, which reads exactly like a wrong
justification. That cost an evening on this repository before the cause was
found, and 26 repositories in the fleet carry an exceptions file (#25).

Run: tests/test_gourmand_sidecar.py   (needs nothing but python3)
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import tempfile
from pathlib import Path

# `sdlc` has no .py extension, so it is loaded by path rather than imported by
# name. Safe to import: its entry point is behind `if __name__ == "__main__"`.
_SPEC = importlib.util.spec_from_loader(
    "sdlc_under_test",
    importlib.machinery.SourceFileLoader(
        "sdlc_under_test", str(Path(__file__).resolve().parent.parent / "sdlc")
    ),
)
assert _SPEC is not None and _SPEC.loader is not None
_SDLC = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SDLC)
gourmand_orphan = _SDLC.gourmand_orphan
SIDECARS = _SDLC.GOURMAND_SIDECARS


def tree(root: Path, *names: str) -> Path:
    for name in names:
        (root / name).write_text("")
    return root


def check(label: str, got: object, want: object) -> bool:
    if got == want:
        return True
    print(f"FAIL {label}: got {got!r}, want {want!r}")
    return False


def main() -> int:
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Every sidecar is caught, and named, so the message can say what it found.
        for name in SIDECARS:
            alone = root / name.removesuffix(".toml")
            alone.mkdir()
            tree(alone, name)
            ok &= check(f"{name} alone", gourmand_orphan(alone), name)

        # A sidecar WITH gourmand.toml is the normal case and changes nothing.
        both = root / "both"
        both.mkdir()
        tree(both, "gourmand.toml", "gourmand-exceptions.toml")
        ok &= check("sidecar beside gourmand.toml", gourmand_orphan(both), None)

        # Neither file: nothing new to say, and no new failure.
        bare = root / "bare"
        bare.mkdir()
        ok &= check("no gourmand files at all", gourmand_orphan(bare), None)

        # gourmand.toml on its own is fine too.
        config = root / "config-only"
        config.mkdir()
        tree(config, "gourmand.toml")
        ok &= check("gourmand.toml alone", gourmand_orphan(config), None)

    print("PASS" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
