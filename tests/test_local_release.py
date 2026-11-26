#!/usr/bin/env python3
"""scripts/local-release.sh runs ci.yml's jobs in ci.yml's order, read from the script.

  - the jobs run check, image, boot, write, promote, pypi: `write` tags vX.Y.Z and
    publishes the GitHub Release only after the image is built and gated and
    its boot proof passed, so a release never exists without an image (a1667)
  - the PyPI token reaches the pypi job alone, as UV_PUBLISH_TOKEN, and that
    job gets no other secret; --pypi runs the pypi job and nothing else (a1738)
  - `write` is handed SDLC_IMAGE and SDLC_BOOT, so its own refusal holds here
    as it does in CI
  - the App key reaches `write` alone, and is unset before `promote`
  - the podman service holds none of the secrets: a build secret whose id names
    a variable in the service's environment reaches its RUN empty (a1679)

Run: python3 tests/test_local_release.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "local-release.sh"
SECRETS = ROOT / "scripts" / "local-release-secrets.sh"
FAILED: list[str] = []
ORDER = ["check", "image", "boot", "write", "promote", "pypi"]
# A call of the script's job(): a command line of `NAME=value` assignments, then
# `job <name>` and its `-e` names to the line's end (a trailing backslash continues
# either). Comments and messages that say "job" are not commands.
CALL = re.compile(r"^[ \t]*(?:\w+=\S*[ \t]+(?:\\\n[ \t]*)?)*job (\w+)((?:[^\n]*\\\n)*[^\n]*)", re.M)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


text = SCRIPT.read_text()
# The pypi job is called from the script's pypi(), for both modes: it runs where
# the release path calls pypi "$release".
last = text.find('pypi "$release"')
calls = [(m[1], m[2], last if m[1] == "pypi" else m.start()) for m in CALL.finditer(text)]
names = [name for name, _, _ in sorted(calls, key=lambda c: c[2])]
check("the jobs run check, image, boot, write, promote, pypi", names == ORDER, str(names))
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

holders = sorted(name for name, given in passed.items() if "UV_PUBLISH_TOKEN" in given)
check("the PyPI token reaches pypi alone", holders == ["pypi"], str(holders))
check(
    "pypi is handed the token and write's results, nothing else",
    passed.get("pypi") == {"UV_PUBLISH_TOKEN", "SDLC_WRITE", "SDLC_RELEASE"},
    str(passed.get("pypi")),
)
read = text.find("pypi_token=")
check(
    "the PyPI token is read unexported, and exported only inside pypi()",
    read > 0
    and not re.search(r"export\b[^\n]*(PYPI_API_TOKEN|pypi_token=)", text)
    and re.findall(r"export UV_PUBLISH_TOKEN\b", text) == ["export UV_PUBLISH_TOKEN"]
    and text.find("pypi() {")
    < text.find("export UV_PUBLISH_TOKEN")
    < text.find("unset UV_PUBLISH_TOKEN"),
)
check(
    "a release that publishes to PyPI without a token stops before image",
    text.find('die "the release publishes to PyPI and PYPI_API_TOKEN') < at.get("image", -1),
)
branch = re.search(r'^if \[ "\$mode" = pypi \]; then\n(.*?)^fi$', text, re.M | re.S)
body = branch[1] if branch else ""
check(
    "--pypi runs the pypi job and no other, before check",
    bool(branch)
    and "exit 0" in body
    and re.search(r"^\s*pypi ", body, re.M) is not None
    and not re.search(r"^\s*(?:\w+=\S*\s+)*job ", body, re.M)
    and branch.start() < at.get("check", -1),
)
emitted = SECRETS.read_text()
check(
    "local-release-secrets.sh emits PYPI_API_TOKEN",
    re.search(r"\{COSIGN_PRIVATE_KEY[^}]*\bPYPI_API_TOKEN\b[^}]*\}", emitted) is not None,
)

# Every secret the script reads from stdin, and the tokens it mints or hands on.
secrets = {"GITHUB_TOKEN", "PYPI_API_TOKEN", "UV_PUBLISH_TOKEN"}
for loop in re.findall(r"^for name in ([A-Z_ ]+); do", text, re.M):
    secrets |= set(loop.split())
service = re.search(r"^CONTAINERS_CONF_OVERRIDE=.*?system service", text, re.M | re.S)
unset_in_service = set(re.findall(r"-u (\w+)", service[0])) if service else set()
check(
    "the podman service is started without any secret in its environment",
    len(secrets) > 1 and secrets <= unset_in_service,
    str(sorted(secrets - unset_in_service)),
)


def finish() -> int:
    print(f"\n{len(FAILED)} failed: {', '.join(FAILED)}" if FAILED else "\nall passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(finish())
