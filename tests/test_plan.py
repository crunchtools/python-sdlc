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
  - an image chain is one lane and every `uv sync` is in another, so the
    launcher can run them at the same time without testing an image that was
    never built or syncing one venv twice at once

Run inside the image, which has the interpreter and PyYAML the runner needs:
  podman run --rm -v .:/src:ro,z -w /src --entrypoint /opt/constitution-venv/bin/python3 \
      quay.io/crunchtools/python-sdlc:<tag> tests/test_plan.py
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
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
check("a block that is not a command is refused", msg is not None and "only keys are" in msg)
msg = config_error('[tool.sdlc.ci]\nchecks = ["typo"]\n')
check("a typo in ci is found on commit", msg is not None and "typo" in msg, str(msg))

# --------------------------------------------------- project in a subdirectory
#
# The uv-driven checks can live one directory down. The value is validated when
# the config is read, so a bad path is caught on the next commit, not in CI.

msg = config_error('[tool.sdlc]\nproject = "nope"\n')
check(
    "a project directory with no pyproject.toml is refused",
    msg is not None and "no pyproject.toml" in msg,
    str(msg),
)
msg = config_error('[tool.sdlc]\nproject = "/etc"\n')
check("an absolute project path is refused", msg is not None and "relative path" in msg, str(msg))
msg = config_error('[tool.sdlc]\nproject = "../elsewhere"\n')
check(
    "a project path leaving the repo is refused",
    msg is not None and "relative path" in msg,
    str(msg),
)
msg = config_error("[tool.sdlc]\nproject = 3\n")
check("a non-string project is refused", msg is not None and "must be a string" in msg, str(msg))
msg = config_error("[tool.ruff]\n")
check("no project key is fine, and means the repo root", msg is None, str(msg))


def project_of(pyproject: str, sub: str | None = None) -> str:
    """Where project_dir() resolves to, relative to the repo root."""
    with tempfile.TemporaryDirectory() as root:
        Path(root, "pyproject.toml").write_text(pyproject)
        if sub:
            Path(root, sub).mkdir(parents=True)
            Path(root, sub, "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
        S.REPO = Path(root)
        return str(S.project_dir().relative_to(Path(root).resolve()))


check("the default project is the repo root", project_of("[tool.ruff]\n") == ".")
check(
    "a named project resolves to that directory",
    project_of('[tool.sdlc]\nproject = "image-server"\n', "image-server") == "image-server",
)

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

# ------------------------------------------------------------------- the lanes
#
# Steps of one lane run in sequence; lanes run at the same time. Getting this
# wrong is not a slow job, it is a check that ran against an image that had not
# been built yet, or two `uv sync`es in one venv. So every rule is pinned.

no_image = S.lane_of(empty, S.resolve(empty, "ci"))
check(
    "a repo with no image-build gets one lane",
    set(no_image.values()) == {S.MAIN_LANE},
    str(no_image),
)

chain = {
    "ci": {
        "checks": ["image", "image-tests"],
        "image": {"kind": "image-build"},
        "image-tests": {"kind": "pytest-in-image", "tests": ["tests/"]},
    }
}
lanes = S.lane_of(chain, S.resolve(chain, "ci"))
check(
    "an image chain is one lane, so the tests follow the build",
    lanes["image"] == lanes["image-tests"],
    str(lanes),
)
check("the image chain is not the main lane", lanes["image"] != S.MAIN_LANE, str(lanes))
check(
    "the other checks stay in the main lane",
    all(lanes[c] == S.MAIN_LANE for c in S.resolve(chain, "ci") if not c.startswith("image")),
    str(lanes),
)
check(
    "every uv sync is in one lane, so two cannot race in one venv",
    len({lanes[c] for c in ("mypy", "pytest", "pip-audit")}) == 1,
    str(lanes),
)

# A pytest-in-image listed BEFORE its build still joins that build's lane: the
# chain is keyed by the image reference, not by the order the repo wrote.
reordered = {
    "ci": {
        "checks": ["image-tests", "image"],
        "image": {"kind": "image-build"},
        "image-tests": {"kind": "pytest-in-image", "tests": ["tests/"]},
    }
}
lanes = S.lane_of(reordered, S.resolve(reordered, "ci"))
check("a chain written backwards is still one lane", lanes["image"] == lanes["image-tests"])

# Two builds writing ONE tag share a lane: concurrent builds of the same tag
# would race to leave the last one standing.
same_tag = {
    "ci": {
        "checks": ["a", "b"],
        "a": {"kind": "image-build"},
        "b": {"kind": "image-build"},
    }
}
lanes = S.lane_of(same_tag, S.resolve(same_tag, "ci"))
check("two builds of one tag share a lane", lanes["a"] == lanes["b"], str(lanes))

two_tags = {
    "ci": {
        "checks": ["a", "b"],
        "a": {"kind": "image-build", "tag": "localhost/a:sdlc"},
        "b": {"kind": "image-build", "tag": "localhost/b:sdlc"},
    }
}
lanes = S.lane_of(two_tags, S.resolve(two_tags, "ci"))
check("two builds of two tags get two lanes", lanes["a"] != lanes["b"], str(lanes))

# Tests against an image nothing in this command builds owe no lane anything.
prebuilt = {
    "ci": {
        "checks": ["image-tests"],
        "image-tests": {
            "kind": "pytest-in-image",
            "tests": ["tests/"],
            "image": "quay.io/x/y:1",
        },
    }
}
lanes = S.lane_of(prebuilt, S.resolve(prebuilt, "ci"))
check(
    "tests on a prebuilt image get their own lane",
    lanes["image-tests"] not in (S.MAIN_LANE, None) and lanes["image-tests"] == "image-tests",
    str(lanes),
)

# A check that rewrites the tree takes the whole command sequential. A build
# context or an in-image mount reading those files while ruff rewrites them is
# a check judging a tree that no longer exists.
rewrites = {
    "ci": {
        "checks": ["ruff-format", "image", "image-tests"],
        "image": {"kind": "image-build"},
        "image-tests": {"kind": "pytest-in-image", "tests": ["tests/"]},
    }
}
lanes = S.lane_of(rewrites, S.resolve(rewrites, "ci"))
check(
    "a check that rewrites the tree gets no lanes beside it",
    set(lanes.values()) == {S.MAIN_LANE},
    str(lanes),
)
commit = S.lane_of(empty, S.resolve(empty, "commit"))
check("commit is always one lane", set(commit.values()) == {S.MAIN_LANE}, str(commit))

# ------------------------------------------------- the plan the launcher reads
#
# Four tab-separated fields now, and the launcher splits on exactly that. A
# plan line that does not have them is a step the shell mis-reads.


def plan_of(target: str, pyproject: str, **env) -> list[list[str]]:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "pyproject.toml").write_text(pyproject)
        Path(root, "tests").mkdir()
        Path(root, "Containerfile").write_text("FROM scratch\n")
        S.REPO = Path(root)
        saved = dict(os.environ)
        os.environ.update(
            {"SDLC_HOST_REPO": root, "SDLC_CACHE_VOLUME": "testvol", "SDLC_PROJECT": "fx", **env}
        )
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                S.t_plan(target)
        finally:
            os.environ.clear()
            os.environ.update(saved)
    return [line.split("\t") for line in buf.getvalue().splitlines()]


TRENTINA = """
[project]
name = "fx"
version = "0.0.0"

[tool.sdlc.ci]
checks = ["image", "image-tests"]
skip = ["codeql"]

[tool.sdlc.ci.image]
kind = "image-build"

[tool.sdlc.ci.image-tests]
kind = "pytest-in-image"
tests = ["tests/"]
"""

PLAN_FIELDS = 4  # check, lane, mode, payload -- what bin/sdlc splits on
CHAIN_LANES = 2  # the main lane, and the image chain's

rows = plan_of("ci", TRENTINA)
check(
    "every plan line has four fields",
    all(len(r) == PLAN_FIELDS for r in rows),
    str(rows),
)
check(
    "the modes are still the third field",
    {r[2] for r in rows} <= {"image-ro", "image-rw", "local", "host"},
    str({r[2] for r in rows}),
)
lanes_seen = [r[1] for r in rows]
check(
    "the plan has exactly two lanes",
    len(set(lanes_seen)) == CHAIN_LANES,
    str(set(lanes_seen)),
)
check(
    "the build and its tests are the only steps off the main lane",
    {r[0] for r in rows if r[1] != S.MAIN_LANE} == {"image", "image-tests"},
    str(lanes_seen),
)
# The launcher groups a lane's steps by check and prints blocks in plan order,
# which both assume a check's steps are contiguous within its lane.
for lane in set(lanes_seen):
    seq = [r[0] for r in rows if r[1] == lane]
    runs = [c for i, c in enumerate(seq) if i == 0 or seq[i - 1] != c]
    check(f"lane {lane}: each check's steps are contiguous", len(runs) == len(set(runs)), str(seq))

single = plan_of("push", '[project]\nname = "fx"\nversion = "0.0.0"\n')
check(
    "push is one lane, so it runs exactly as it did before",
    {r[1] for r in single} == {S.MAIN_LANE},
    str({r[1] for r in single}),
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
