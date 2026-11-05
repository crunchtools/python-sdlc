#!/usr/bin/env python3
"""A `.pyc` compiled on the host must not decide a verdict inside the gate.

The gate mounts the developer's worktree at `/repo`, so the container and the
laptop share the tree's `__pycache__`. Bytecode records the path it was
compiled from in `co_filename`, so a module compiled on the laptop carries
`/home/<user>/.../m.py` while `__file__` inside the container is
`/repo/.../m.py`. Any test comparing the two then fails in the gate and nowhere
else: trentina's `test_library_lines_are_fingerprinted` keys a dict on
`__file__` and matches it against `record.pathname`, which comes from
`co_filename`. That cost four of the five pushes on crunchtools/trentina#706
before anyone suspected the cache.

It is specifically PYTEST's cache, not Python's. The stdlib's source loader
calls `_imp._fix_co_filename` after unmarshalling (`_bootstrap_external.
_compile_bytecode`), so a plain `import` rewrites the stale path and never
shows this. pytest's assertion rewriter keeps its own
`*.cpython-NNN-pytest-X.Y.Z.pyc` beside it and its `_read_pyc`
(`_pytest/assertion/rewrite.py`) returns `marshal.load(fp)` as it stands -- it
has to, because the point of that cache is the REWRITTEN code. So the foreign
path survives, and only there.

`PYTHONPYCACHEPREFIX` is the fix at both ends: the rewriter's `get_cache_dir`
honours `sys.pycache_prefix`, so with one set it neither reads the tree's
bytecode nor writes any back into the mount.

Both halves of pytest's behaviour are reimplemented below rather than imported,
because this image has no pytest -- a repo's pytest comes from its own
`uv.lock`. They are small, and each says which pytest function it
copies so a drift in pytest can be checked against it.

The env var reaching the containers is asserted separately:
`tests/test_host_contract.sh` for the gate's own, `tests/test_plan.py` for
`pytest-in-image`.

Run: tests/test_pycache_prefix.py   (needs nothing but python3)
"""

from __future__ import annotations

import importlib.util
import marshal
import os
import py_compile
import sys
import tempfile
import types
from pathlib import Path

# What a host-compiled module carries: a path that does not exist in the
# container. That it differs from the real one is the whole point.
FOREIGN = "/home/somebody/elsewhere/subject.py"

SOURCE = '''\
"""A module whose only job is to report the path its bytecode came from."""


def where() -> str:
    return where.__code__.co_filename
'''

# The fixed part of a .pyc: magic, flags, source mtime, source size.
PYC_HEADER = 16

FAILED: list[str] = []


def ok(msg: str) -> None:
    print(f"ok   {msg}")


def bad(msg: str) -> None:
    FAILED.append(msg)
    print(f"FAIL {msg}")


def check(name: str, got: object, want: object) -> None:
    if got == want:
        ok(f"{name}: {got}")
    else:
        bad(f"{name}: {got!r}, want {want!r}")


# --------------------------------------------- pytest's two behaviours, copied


def rewriter_cache_dir(source: Path) -> Path:
    """`_pytest.assertion.rewrite.get_cache_dir`, copied.

    With a prefix, the tree's own directory is not involved at all: the cache
    goes under the prefix at the source's absolute path. That is the line this
    whole change rests on.
    """
    if sys.pycache_prefix:
        return Path(sys.pycache_prefix) / Path(*source.parts[1:-1])
    return source.parent / "__pycache__"


def rewriter_read_pyc(source: Path, pyc: Path) -> types.CodeType | None:
    """`_pytest.assertion.rewrite._read_pyc`, copied: the magic/mtime/size
    header checks, then the code object exactly as marshalled.

    No `_imp._fix_co_filename`. That absence IS the bug, and it is not a pytest
    defect -- the cached code is rewritten code, and pytest reads it back the
    way it wrote it.
    """
    try:
        with open(pyc, "rb") as fp:
            stat = os.stat(source)
            header = fp.read(PYC_HEADER)
            if len(header) != PYC_HEADER or header[:4] != importlib.util.MAGIC_NUMBER:
                return None
            if header[4:8] != b"\x00\x00\x00\x00":
                return None
            if int.from_bytes(header[8:12], "little") != int(stat.st_mtime) & 0xFFFFFFFF:
                return None
            if int.from_bytes(header[12:16], "little") != stat.st_size & 0xFFFFFFFF:
                return None
            code = marshal.load(fp)
    except OSError:
        return None
    return code if isinstance(code, types.CodeType) else None


# ------------------------------------------------------------------- the cases


def host_compiled(source: Path, into: Path) -> Path:
    """Bytecode for `source`, in `into`, stamped with a path from another machine."""
    into.mkdir(parents=True, exist_ok=True)
    pyc = into / f"{source.stem}.{sys.implementation.cache_tag}-pytest-9.1.1.pyc"
    py_compile.compile(str(source), cfile=str(pyc), dfile=FOREIGN, doraise=True)
    return pyc


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tree = (Path(tmp) / "tree").resolve()
        tree.mkdir()
        source = tree / "subject.py"
        source.write_text(SOURCE, encoding="utf-8")

        # ---- the bug, as the gate meets it. The laptop left bytecode in the
        # tree; the container shares the tree, so the rewriter reads it.
        sys.pycache_prefix = None
        shared = rewriter_cache_dir(source)
        check("without a prefix the cache is in the tree", shared, tree / "__pycache__")
        pyc = host_compiled(source, shared)
        code = rewriter_read_pyc(source, pyc)
        if code is None:
            bad("the copied header checks rejected bytecode they should accept")
            return 1
        check("co_filename the rewriter would use", code.co_filename, FOREIGN)
        if code.co_filename == str(source):
            bad("no mismatch, so nothing below proves anything")

        # ---- and why a plain import never showed it: the stdlib fixes the path.
        sys.modules.pop("subject_stdlib", None)
        spec = importlib.util.spec_from_file_location("subject_stdlib", source)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        check("co_filename through the stdlib loader", module.where(), str(source))

        # ---- the fix. With a prefix the rewriter looks outside the tree, so
        # the laptop's bytecode is not on its path at all.
        prefix = (Path(tmp) / "pycache").resolve()
        sys.pycache_prefix = str(prefix)
        try:
            prefixed = rewriter_cache_dir(source)
            if prefixed.is_relative_to(tree):
                bad(f"with a prefix the cache is still in the tree: {prefixed}")
            else:
                ok(f"with a prefix the cache is outside the tree: {prefixed}")
            check(
                "the stale bytecode is not where the rewriter now looks",
                (prefixed / pyc.name).exists(),
                False,
            )
            # The other direction, which matters because the commit tier mounts
            # the tree READ-WRITE: nothing the container compiles lands in it.
            before = sorted(p.name for p in shared.iterdir())
            host_compiled(source, prefixed)
            check(
                "the tree's __pycache__ is untouched",
                sorted(p.name for p in shared.iterdir()),
                before,
            )
        finally:
            sys.pycache_prefix = None

    print()
    if FAILED:
        print(f"PYTHONPYCACHEPREFIX FAILED: {len(FAILED)} check(s)")
        return 1
    print("PYTHONPYCACHEPREFIX PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
