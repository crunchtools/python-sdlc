#!/usr/bin/env python3
"""scripts/local-release.sh runs ci.yml's jobs in ci.yml's order, read from the script.

  - the jobs run check, image, boot, write, promote: `write` tags vX.Y.Z and
    publishes the GitHub Release only after the image is built and gated and
    its boot proof passed, so a release never exists without an image (a1667)
  - `write` is handed SDLC_IMAGE and SDLC_BOOT, so its own refusal holds here
    as it does in CI
  - the App key reaches `write` alone, and is unset before `promote`

Run: python3 tests/test_local_release.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "local-release.sh"
FAILED: list[str] = []
ORDER = ["check", "image", "boot", "write", "promote"]
# A call of the script's job(): a command line of `NAME=value` assignments, then
# `job <name>` and its `-e` names to the line's end (a trailing backslash continues
# either). Comments and messages that say "job" are not commands.
CALL = re.compile(r"^[ \t]*(?:\w+=\S*[ \t]+(?:\\\n[ \t]*)?)*job (\w+)((?:[^\n]*\\\n)*[^\n]*)", re.M)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


text = SCRIPT.read_text()
calls = [(m[1], m[2], m.start()) for m in CALL.finditer(text)]
names = [name for name, _, _ in calls]
check("the jobs run check, image, boot, write, promote", names == ORDER, str(names))
passed = {name: set(re.findall(r"-e (\w+)", args)) for name, args, _ in calls}
check(
    "write is handed image's and boot's results",
    {"SDLC_IMAGE", "SDLC_BOOT"} <= passed.get("write", set()),
    str(passed.get("write")),
)
holders = sorted(name for name, given in passed.items() if "ASHIGARU_APP_KEY" in given)
check("the App key reaches write alone", holders == ["write"], str(holders))
at = {name: start for name, _, start in calls}
unset = text.find("unset ASHIGARU_APP_KEY")
check(
    "the App key is unset after write and before promote",
    at.get("write", -1) < unset < at.get("promote", -1),
)


def finish() -> int:
    print(f"\n{len(FAILED)} failed: {', '.join(FAILED)}" if FAILED else "\nall passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(finish())
