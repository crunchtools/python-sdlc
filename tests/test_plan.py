#!/usr/bin/env python3
"""What each command runs, decided from a repo's pyproject.toml.

The launcher is shell and cannot read TOML, so it does exactly what `sdlc plan`
tells it. A wrong answer here is a check that silently does not run, so the
rules are pinned case by case:

  - each command runs its defaults; a block's `checks` adds, `skip` removes
  - ci runs push's checks except the two reviewers CI already runs elsewhere
  - a check that is not built in must name a kind, and only real kinds exist
  - the whole config is checked whichever command runs
  - CodeQL fails only on findings that are not in the committed baseline

Run inside the image, which has the interpreter and PyYAML the runner needs:
  podman run --rm -v .:/src:ro,z -w /src --entrypoint /opt/constitution-venv/bin/python3 \
      quay.io/crunchtools/python-sdlc:<tag> tests/test_plan.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

_SPEC = importlib.util.spec_from_loader(
    "sdlc_under_test",
    importlib.machinery.SourceFileLoader(
        "sdlc_under_test", str(Path(__file__).resolve().parent.parent / "sdlc")
    ),
)
assert _SPEC is not None and _SPEC.loader is not None
S = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(S)

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def cannot_run(fn, *args) -> str | None:
    """The CannotRun message `fn` raised, or None if it did not raise."""
    try:
        fn(*args)
    except S.CannotRunError as exc:
        return str(exc)
    return None


# --------------------------------------------------------------- resolution

empty: dict = {}
check(
    "commit defaults",
    S.resolve(empty, "commit") == ["ruff-check", "ruff-format", "hooks-installed"],
)
check("push defaults", S.resolve(empty, "push") == S.DEFAULTS["push"])
ci = S.resolve(empty, "ci")
check("ci leaves out the reviewers", "gourmand" not in ci and "gatehouse" not in ci, str(ci))
check(
    "ci keeps the other push checks",
    ci == [c for c in S.DEFAULTS["push"] if c not in S.CI_EXCLUDES],
)

cfg = {"push": {"skip": ["codeql"]}, "ci": {"checks": ["image"], "image": {"kind": "image-build"}}}
check("push skip removes a default", "codeql" not in S.resolve(cfg, "push"))
check("a push skip carries into ci", "codeql" not in S.resolve(cfg, "ci"))
check("ci checks adds after the defaults", S.resolve(cfg, "ci")[-1] == "image")
check("a repo check has its kind", S.kind_of(cfg, "image") == "image-build")
check("a built-in has no kind", S.kind_of(cfg, "mypy") is None)

# ----------------------------------------------------------------- refusals

msg = cannot_run(S.kind_of, {"commit": {"checks": ["mystery"]}}, "mystery")
check("an unknown check needs a kind", msg is not None and "needs a table" in msg, str(msg))
msg = cannot_run(S.kind_of, {"commit": {"x": {"kind": "shell"}}}, "x")
check("there is no shell kind", msg is not None and "the kinds are" in msg, str(msg))
msg = cannot_run(S.kind_of, {"commit": {"mypy": {"kind": "image-build"}}}, "mypy")
check("a built-in cannot be given a kind", msg is not None and "built in" in msg, str(msg))
msg = cannot_run(S.kind_of, {"commit": {"plan": {"kind": "image-build"}}}, "plan")
check("a command word cannot be a check", msg is not None and "command of this tool" in msg)
msg = cannot_run(S.check_settings, {"push": {"x": {"kind": "image-build"}}, "ci": {"x": {}}}, "x")
check("check names are unique across blocks", msg is not None and "more than one block" in msg)
msg = cannot_run(S.resolve, {"push": {"checks": "mypy"}}, "push")
check("checks must be a list", msg is not None and "list of check names" in msg)


def config_error(pyproject: str) -> str | None:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "pyproject.toml").write_text(pyproject)
        S.REPO = Path(root)
        return cannot_run(S.sdlc_config)


msg = config_error("[tool.sdlc.post]\n")
check("a block that is not a command is refused", msg is not None and "only blocks are" in msg)
msg = config_error('[tool.sdlc.ci]\nchecks = ["typo"]\n')
check("a typo in ci is found on commit", msg is not None and "typo" in msg, str(msg))

# ----------------------------------------------------------- codeql baseline


def sarif(results: list[tuple[str, str, str]]) -> dict:
    return {
        "runs": [
            {
                "results": [
                    {
                        "ruleId": rule,
                        "message": {"text": "x"},
                        "partialFingerprints": {"primaryLocationLineHash": fp},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": path},
                                    "region": {"startLine": 1},
                                }
                            }
                        ],
                    }
                    for rule, path, fp in results
                ]
            }
        ]
    }


with tempfile.TemporaryDirectory() as root:
    S.REPO = Path(root)
    out = Path(root, "r.sarif")
    base = Path(root, ".sdlc", "codeql-baseline.json")
    known = ("py/a", "src/a.py", "h1")
    out.write_text(json.dumps(sarif([known])))
    check("no baseline: every finding fails", S.report_sarif(out, [], base) == S.FOUND)
    base.parent.mkdir()
    base.write_text(json.dumps([{"rule": known[0], "path": known[1], "fingerprint": known[2]}]))
    check("a baselined finding passes", S.report_sarif(out, [], base) == S.CLEAN)
    out.write_text(json.dumps(sarif([known, ("py/b", "src/b.py", "h2")])))
    check("a new finding fails", S.report_sarif(out, [], base) == S.FOUND)
    out.write_text(json.dumps(sarif([known, ("py/b", "tests/fx/b.py", "h2")])))
    check("paths-ignore still applies", S.report_sarif(out, ["tests/fx"], base) == S.CLEAN)
    base.write_text("not json")
    check(
        "an unreadable baseline cannot run",
        cannot_run(S.report_sarif, out, [], base) is not None,
    )

# ------------------------------------------------------- path confinement

with tempfile.TemporaryDirectory() as root:
    S.REPO = Path(root)
    Path(root, "tests").mkdir()
    check("a repo path is accepted", S.in_repo("x", "mount", "tests") == "tests")
    for bad in ("../..", "/etc", "tests/../../.."):
        msg = cannot_run(S.in_repo, "x", "mount", bad)
        check(f"{bad!r} is refused", msg is not None and "outside the repository" in msg, str(msg))
    msg = cannot_run(S.kind_of, {"commit": {"x": {"kind": ["image-build"]}}}, "x")
    check("a kind that is not a name is refused", msg is not None and "must be a name" in msg)


# ------------------------------------------------- the plan cannot be forged
#
# A config value with a newline or a tab in it would split into a second plan
# line the launcher then obeys, so the plan refuses them at the source.

for bad in ("a\nevil\thost\tsh", "a\tb", "a\x1fb"):
    msg = cannot_run(S.host_step, "image", ["podman", "build", "--build-arg", f"V={bad}"])
    check(f"{bad!r} cannot reach the plan", msg is not None and "refused" in msg, str(msg))
check(
    "a host step is its argv joined by the unit separator",
    S.host_step("x", ["podman", "run", "a b"]) == ("x", "host", "podman\x1frun\x1fa b"),
)


def main() -> int:
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
