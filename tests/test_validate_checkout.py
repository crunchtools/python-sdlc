#!/usr/bin/env python3
"""`validate` must tell an unreadable checkout from one missing a file.

An SELinux label or mode that blocks listing the cached constitution checkout
used to surface as "missing validate-constitution.py", sending the reader to
the constitution tag instead of the label. This makes `os.listdir` raise and
asserts the diagnostic names the checkout and the cause, and a plain missing
file still gets the missing-file message.

Run: tests/test_validate_checkout.py   (needs nothing but python3)
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import sys
import tempfile
from pathlib import Path
from unittest import mock

VALIDATE = Path(__file__).resolve().parent.parent / "validate"


def load():
    loader = importlib.machinery.SourceFileLoader("sdlc_validate", str(VALIDATE))
    spec = importlib.util.spec_from_loader("sdlc_validate", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def run(module, cache: Path, manifest: Path, listdir=None) -> tuple[int, str]:
    err = io.StringIO()
    patches = [
        mock.patch.object(module, "CACHE", cache),
        mock.patch.object(sys, "argv", ["validate", str(manifest)]),
    ]
    if listdir is not None:
        patches.append(mock.patch.object(module.os, "listdir", listdir))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(contextlib.redirect_stderr(err))
        try:
            module.main()
            code = 0
        except SystemExit as exc:
            code = exc.code
    return code, err.getvalue()


def main() -> int:
    module = load()
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manifest = root / "constitution.md"
        manifest.write_text("> **Inherits:** constitution v1.2.3\n", encoding="utf-8")
        cache = root / "cache"
        checkout = cache / "constitution" / "v1.2.3"
        (checkout / ".git").mkdir(parents=True)

        def denied(_path):
            raise PermissionError(13, "Permission denied")

        code, err = run(module, cache, manifest, listdir=denied)
        checks = [
            ("unreadable: exits CANNOT_RUN", code == module.CANNOT_RUN),
            ("unreadable: says cannot read", "cannot read the constitution checkout" in err),
            ("unreadable: names the path", str(checkout) in err),
            ("unreadable: carries the error", "Permission denied" in err),
            ("unreadable: not the missing-file message", "is missing" not in err),
        ]
        code, err = run(module, cache, manifest)
        checks += [
            ("missing file: exits CANNOT_RUN", code == module.CANNOT_RUN),
            ("missing file: says is missing", "is missing validate-constitution.py" in err),
        ]
    for name, ok in checks:
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
        failed += not ok
    print()
    print("validate checkout diagnostics " + ("FAILED" if failed else "PASSED"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
