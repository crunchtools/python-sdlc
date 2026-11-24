#!/usr/bin/env python3
"""Local Gatehouse fails on what CI's gate would hold a pull request for.

Gatehouse exits non-zero only on a finding every sample blocked on, while the
fleet's gate (sdlc_jobs/gate.py `_required`) makes a pull request answer every
CRITICAL, HIGH and MEDIUM finding it posts, and the LOWs of Bug Hunter and
Security Scan. A push that passed with one of those left CI to find it.

Run: tests/test_gatehouse_verdict.py   (needs nothing but python3)
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
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
required = _SDLC.ci_required_findings

CASES = [
    ("a high from any agent", "general", "high", True),
    ("a medium from an advisory agent", "performance", "medium", True),
    ("a critical", "consistency", "critical", True),
    ("a Bug Hunter low", "bugs", "low", True),
    ("a Security Scan low", "security", "low", True),
    ("an advisory low", "performance", "low", False),
    ("a general low", "general", "low", False),
]


def main() -> int:
    ok = True
    for label, agent, severity, want in CASES:
        got = bool(required({"findings": [{"agent": agent, "severity": severity}]}))
        if got != want:
            print(f"FAIL {label}: required={got}, want {want}")
            ok = False
    if required({"findings": []}) or required({}):
        print("FAIL no findings: something was required")
        ok = False
    print("gatehouse verdict test PASSED" if ok else "gatehouse verdict test FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
