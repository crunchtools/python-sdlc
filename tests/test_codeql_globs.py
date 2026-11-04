#!/usr/bin/env python3
"""`paths-ignore` matching must follow CodeQL's semantics, not an approximation.

This is the local stand-in for a config the CodeQL CLI will not read (see
docs/codeql.md), so an entry CodeQL honours and this does not means the local
gate reports findings CI ignores -- and with `codeql_gate = true`, fails on
them. trentina's config excludes `tests/fixtures/boundary-review`, which holds
deliberately vulnerable code used to grade a reviewer, so getting this wrong is
not hypothetical.

The first implementation matched only literal paths and directory prefixes.
Gatehouse caught that a code-scanning config also accepts globs, where `**`
crosses separators and `*` does not. These cases are that finding, kept.

Run: tests/test_codeql_globs.py   (needs nothing but python3)
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

# `sdlc` has no .py extension, so it is loaded by path rather than imported by
# name. It is safe to import: its entry point is behind
# `if __name__ == "__main__"`, so nothing runs on import.
#
# The first version of this read the file and exec(compile(...))'d it behind a
# a lint suppression. Gourmand flagged both the dangerous API and the comment
# hiding it, and was right twice -- importlib does the same job with neither.
_SPEC = importlib.util.spec_from_loader(
    "sdlc_under_test",
    importlib.machinery.SourceFileLoader(
        "sdlc_under_test", str(Path(__file__).resolve().parent.parent / "sdlc")
    ),
)
assert _SPEC is not None and _SPEC.loader is not None
_SDLC = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SDLC)
path_ignored = _SDLC.path_ignored

CASES = [
    # (artifact uri, paths-ignore entries, should be suppressed)
    # A plain directory, which is what the fleet actually writes today.
    ("tests/fixtures/boundary-review/bad.py", ["tests/fixtures/boundary-review"], True),
    ("tests/fixtures/boundary-review", ["tests/fixtures/boundary-review"], True),
    # A sibling directory sharing a prefix must NOT be caught.
    (
        "tests/fixtures/boundary-reviewer/x.py",
        ["tests/fixtures/boundary-review"],
        False,
    ),
    ("tests/fixtures/other/x.py", ["tests/fixtures/boundary-review"], False),
    # `**` crosses separators.
    ("tests/a/b/c.py", ["tests/**"], True),
    ("src/a.py", ["tests/**"], False),
    ("src/vendor/lib.py", ["**/vendor/*"], True),
    ("src/deep/vendor/lib.py", ["**/vendor/*"], True),
    # `*` does not cross separators -- the case the first version got wrong in
    # the other direction.
    ("src/gen_a.py", ["src/gen_*.py"], True),
    ("src/sub/gen_a.py", ["src/gen_*.py"], False),
    # A trailing slash in the config is legal and means the same thing.
    ("demo/x.py", ["demo/"], True),
    # Several entries, and an empty one, which a hand-edited config will have.
    ("demo/x.py", ["", "src", "demo"], True),
    ("lib/x.py", ["", "src", "demo"], False),
    # No entries at all suppresses nothing.
    ("src/a.py", [], False),
]


def main() -> int:
    failed = 0
    for artifact, ignores, want in CASES:
        got = path_ignored(artifact, ignores)
        if got == want:
            print(f"ok   {artifact:42} vs {ignores}")
        else:
            print(f"FAIL {artifact:42} vs {ignores} -> {got}, want {want}")
            failed += 1
    print()
    if failed:
        print(f"codeql glob matching FAILED: {failed} case(s)")
        return 1
    print("codeql glob matching PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
