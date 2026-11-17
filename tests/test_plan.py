#!/usr/bin/env python3
"""What each command runs, decided from a repo's pyproject.toml.

The launcher is shell and cannot read TOML, so it does exactly what `sdlc plan`
tells it. A wrong answer here is a check that silently does not run, so the
rules are pinned case by case:

  - each command runs its defaults; a block's `checks` adds, `skip` removes
  - ci runs push's checks except the two reviewers CI already runs elsewhere,
    and a repo may not skip any of the rest: that would leave a verdict on
    pre-push alone, which is a developer's machine and one `--no-verify`
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
    S.resolve(empty, "commit") == ["ruff-check", "ruff-format", "gourmand", "hooks-installed"],
)
# Written out rather than read from DEFAULTS: DEFAULTS holds every stack's
# checks, and this is the list a Python repo ran before stacks existed.
PYTHON_PUSH = [
    "lint",
    "mypy",
    "pytest",
    "gourmand",
    "gatehouse",
    "semgrep",
    "pip-audit",
    "constitution",
    "codeql",
]
check("push defaults", S.resolve(empty, "push") == PYTHON_PUSH, str(S.resolve(empty, "push")))
ci = S.resolve(empty, "ci")
check("ci leaves out the reviewers", "gourmand" not in ci and "gatehouse" not in ci, str(ci))
check(
    "ci keeps the other push checks",
    ci == [c for c in PYTHON_PUSH if c not in S.CI_EXCLUDES],
    str(ci),
)

# python-sdlc#48: the commit tier's ruff steps fix and pass, so without a
# non-mutating verdict in push, ci ran no ruff at all.
check("ci renders the ruff verdict", "lint" in ci, str(ci))
check(
    "lint reads the tree, never writes it",
    S.plan_lines(empty, "lint") == [("lint", "image-ro", "lint")],
)
push_lanes = S.lane_of(empty, S.resolve(empty, "push"))
check(
    "lint keeps push's lanes",
    "lint" not in S.WRITES_TREE and set(push_lanes.values()) == {S.MAIN_LANE, S.REVIEW_LANE},
    str(push_lanes),
)
msg = cannot_run(S.resolve, {"ci": {"skip": ["lint"]}}, "ci")
check("ci may not skip lint", msg is not None and "lint" in msg, str(msg))
check(
    "push skip of lint takes it out of ci too",
    "lint" not in S.resolve({"push": {"skip": ["lint"]}}, "ci"),
)

cfg = {"push": {"skip": ["codeql"]}, "ci": {"checks": ["image"], "image": {"kind": "image-build"}}}
check("push skip removes a default", "codeql" not in S.resolve(cfg, "push"))
check("a push skip carries into ci", "codeql" not in S.resolve(cfg, "ci"))
check("ci checks adds after the defaults", S.resolve(cfg, "ci")[-1] == "image")
check("a repo check has its kind", S.kind_of(cfg, "image") == "image-build")
check("a built-in has no kind", S.kind_of(cfg, "mypy") is None)

# XII: CI runs push's verdicts. A ci-only skip would leave a check on pre-push
# alone -- a developer's machine, one `--no-verify` from gone -- while the
# required `sdlc ci` reported green. It is refused, whichever verdict it names.
for verdict in sorted(set(S.DEFAULTS["push"]) - set(S.CI_EXCLUDES)):
    msg = cannot_run(S.resolve, {"ci": {"skip": [verdict]}}, "ci")
    check(
        f"ci may not skip {verdict}",
        msg is not None and "CI may not skip a verdict" in msg,
        str(msg),
    )
msg = cannot_run(S.resolve, {"ci": {"skip": list(S.CI_EXCLUDES)}}, "ci")
check("ci may still name the two it never runs", msg is None, str(msg))
# The repo's own checks are its own business: it added them, it may skip them.
own = {"push": {"checks": ["img"], "img": {"kind": "image-build"}}, "ci": {"skip": ["img"]}}
check("ci may skip a check the repo added", "img" not in S.resolve(own, "ci"))

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
# The whole config is checked whichever command runs, so a ci-only skip of a
# verdict is caught on the next commit rather than in CI, where the skip is
# precisely what would have made CI read green.
msg = config_error('[tool.sdlc.ci]\nskip = ["mypy"]\n')
check("a ci skip of a verdict is found on commit", msg is not None and "mypy" in msg, str(msg))

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
# Gourmand judges what ruff leaves behind, so it must come after both ruff
# steps. A commit tier that reviewed the unformatted tree would report findings
# that the same commit had already fixed.
order = S.resolve(empty, "commit")
check(
    "gourmand runs after the two ruff steps",
    order.index("gourmand") > max(order.index("ruff-check"), order.index("ruff-format")),
    str(order),
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


# python-sdlc#50: a push skip is the only way a verdict leaves ci, so ci and
# tiers say so; otherwise a green ci reads the same whether lint ran or not.
check("no push skip, nothing to report", S.push_skips(empty) == [])
check(
    "a push skip is named, in push order",
    S.push_skips({"push": {"skip": ["codeql", "lint"]}}) == ["lint", "codeql"],
)
check(
    "the reviewers ci never runs are not named",
    S.push_skips({"push": {"skip": ["gatehouse", "mypy"]}}) == ["mypy"],
)
check(
    "a verdict ci adds back is not named",
    S.push_skips({"push": {"skip": ["lint"]}, "ci": {"checks": ["lint"]}}) == [],
)


def stderr_and_stdout(fn, target: str | None, pyproject: str) -> tuple[str, str]:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "pyproject.toml").write_text(pyproject)
        S.REPO = Path(root)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            fn(*([target] if target else []))
    return err.getvalue(), out.getvalue()


SKIPS = '[tool.sdlc.push]\nskip = ["lint", "codeql"]\n'
NAMED = "sdlc ci: not run, skipped in [tool.sdlc.push]: lint, codeql"
err, out = stderr_and_stdout(S.t_plan, "ci", SKIPS)
check("sdlc ci names its push skips once", err.splitlines() == [NAMED], err)
check("the plan itself stays clean", NAMED not in out, out)
err, _ = stderr_and_stdout(S.t_plan, "push", SKIPS)
check("only ci names them", err == "", err)
err, _ = stderr_and_stdout(S.t_plan, "ci", "[tool.ruff]\n")
check("a repo that skips nothing hears nothing", err == "", err)
_, out = stderr_and_stdout(S.t_tiers, None, SKIPS)
check("sdlc tiers prints the same line", f"  {NAMED}" in out.splitlines(), out)


TRENTINA = """
[project]
name = "fx"
version = "0.0.0"

[tool.sdlc.ci]
checks = ["image", "image-tests"]

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

# The in-image pytest run mounts the repo's test files, so it has the same
# exposure to the host's bytecode that bin/sdlc's own containers have: pytest's
# rewriter reads its cached code as marshalled and cannot correct the path it
# was compiled from. See tests/test_pycache_prefix.py.
argv = next(r[3].split(S.UNIT_SEPARATOR) for r in rows if r[0] == "image-tests" and r[2] == "host")
check(
    "the in-image pytest run keeps its bytecode out of the tree",
    "PYTHONPYCACHEPREFIX=/tmp/pycache" in argv,
    str(argv),
)

# Push has two lanes: everything that computes, and the reviewer. Gatehouse
# reads the diff bin/sdlc computed on the host, never the worktree, so it is the
# one check that can run beside a command that rewrites the tree -- and the one
# whose time is an API call rather than a CPU, so overlapping it is free.
pushed = plan_of("push", '[project]\nname = "fx"\nversion = "0.0.0"\n')
check(
    "push puts the reviewer in its own lane",
    {r[0] for r in pushed if r[1] == S.REVIEW_LANE} == {"gatehouse"},
    str([(r[0], r[1]) for r in pushed]),
)
check(
    "everything that computes stays in one lane",
    {r[1] for r in pushed} == {S.MAIN_LANE, S.REVIEW_LANE},
    str({r[1] for r in pushed}),
)
# CI gets Gatehouse from its own workflow, so there is nothing to split off.
ci_rows = plan_of("ci", '[project]\nname = "fx"\nversion = "0.0.0"\n')
check(
    "ci is one lane, having no reviewer to run beside",
    {r[1] for r in ci_rows} == {S.MAIN_LANE},
    str({r[1] for r in ci_rows}),
)


# ------------------------------------------------------------------ the stacks
#
# One image, several kinds of repo. Detection reads the tree; a config block may
# add or drop a stack but never name a command. And the promise that makes it
# safe to ship: a Python repo that says nothing about stacks plans EXACTLY what
# it planned before stacks existed, Containerfile or not.


def stacks_of(files: dict[str, str]) -> tuple[str, ...] | str:
    """active_stacks() over a tree holding `files`, or the CannotRun message."""
    with tempfile.TemporaryDirectory() as root:
        for name, text in files.items():
            Path(root, name).write_text(text)
        S.REPO = Path(root)
        try:
            return S.active_stacks(S.config_document().get("tool", {}).get("sdlc", {}) or {})
        except S.CannotRunError as exc:
            return str(exc)


PY = '[project]\nname = "fx"\nversion = "0.0.0"\n'
CF = "FROM quay.io/hummingbird/python:3.14\n"
check("a Python repo is the python stack", stacks_of({"pyproject.toml": PY}) == ("python",))
check(
    "a Python repo naming no stacks keeps python alone, Containerfile or not",
    stacks_of({"pyproject.toml": PY, "Containerfile": CF, "package.json": "{}"}) == ("python",),
)
check(
    "a Python repo that opts in gets what was detected and what it asked for",
    stacks_of({"pyproject.toml": PY + '[tool.sdlc]\nstacks = ["container"]\n', "Containerfile": CF})
    == ("python", "container"),
)
check(
    "a Containerfile alone is the container stack",
    stacks_of({"Containerfile": CF}) == ("container",),
)
check("a Dockerfile counts too", stacks_of({"Dockerfile": CF}) == ("container",))
msg = stacks_of({"Containerfile": "FROM quay.io/fedora/fedora-bootc:42\n"})
check("a bootc Containerfile is not the container stack", "bootc" in str(msg), str(msg))
msg = stacks_of({"package.json": "{}"})
check("a stack this image cannot run yet is refused, not skipped", "node" in str(msg), str(msg))
check(
    "skip-stacks accepts running without it",
    stacks_of({"package.json": "{}", "sdlc.toml": '[tool.sdlc]\nskip-stacks = ["node"]\n'}) == (),
)
msg = stacks_of({"README.md": "x"})
check("a repo with nothing detected is docs, which is not here yet", "docs" in str(msg), str(msg))
msg = stacks_of({"Containerfile": CF, "sdlc.toml": '[tool.sdlc]\nstacks = ["rust"]\n'})
check("an unknown stack is refused", "rust" in str(msg), str(msg))
msg = stacks_of({"pyproject.toml": PY, "sdlc.toml": "[tool.sdlc]\n"})
check("sdlc.toml beside a pyproject.toml is refused", "both exist" in str(msg), str(msg))

check(
    "the python stack plans push exactly as before stacks",
    S.resolve(empty, "push", ("python",)) == PYTHON_PUSH,
)
check(
    "the default stacks argument is the python stack",
    S.resolve(empty, "ci") == S.resolve(empty, "ci", ("python",)),
)
check(
    "the container stack alone pushes hadolint and the common checks",
    S.resolve(empty, "push", ("container",))
    == ["containerfile-lint", "gourmand", "gatehouse", "constitution"],
)
check(
    "the container stack builds, smokes and scans in ci, after push's checks",
    S.resolve(empty, "ci", ("container",))
    == [
        "containerfile-lint",
        "constitution",
        "container-build",
        "container-smoke",
        "container-scan",
    ],
)
check(
    "commit has nothing from the container stack",
    S.resolve(empty, "commit", ("container",)) == ["gourmand", "hooks-installed"],
)
msg = cannot_run(S.resolve, {"ci": {"skip": ["containerfile-lint"]}}, "ci", ("container",))
check("ci may not skip hadolint, a push verdict", msg is not None, str(msg))
check(
    "ci may skip the scan, which only ci runs",
    "container-scan" not in S.resolve({"ci": {"skip": ["container-scan"]}}, "ci", ("container",)),
)


def container_plan(target: str, sdlc_toml: str = "", **env) -> list[list[str]]:
    with tempfile.TemporaryDirectory() as root:
        Path(root, "Containerfile").write_text(CF)
        if sdlc_toml:
            Path(root, "sdlc.toml").write_text(sdlc_toml)
        S.REPO = Path(root)
        saved = dict(os.environ)
        os.environ.update(
            {
                "SDLC_HOST_REPO": root,
                "SDLC_CACHE_VOLUME": "testvol",
                "SDLC_PROJECT": "fx",
                **env,
            }
        )
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                S.t_plan(target)
        finally:
            os.environ.clear()
            os.environ.update(saved)
    return [line.split("\t") for line in buf.getvalue().splitlines()]


rows = container_plan("ci")
by_check = {r[0]: r for r in rows}
check("every container plan line has four fields", all(len(r) == PLAN_FIELDS for r in rows))
check(
    "build, smoke and scan are one lane, off the main lane",
    len({by_check[c][1] for c in S.CI_DEFAULTS}) == 1
    and by_check["container-build"][1] != S.MAIN_LANE,
    str([(r[0], r[1]) for r in rows]),
)
build = by_check["container-build"][3].split(S.UNIT_SEPARATOR)
check(
    "container-build builds the root Containerfile at the default tag",
    build[:4] == ["podman", "build", "-f", "Containerfile"] and "localhost/fx:sdlc" in build,
    str(build),
)
smoke = by_check["container-smoke"]
words = smoke[3].split(S.UNIT_SEPARATOR)
check(
    "container-smoke is host-up, its first word the seconds",
    smoke[2] == "host-up" and words[0] == str(S.SMOKE_SECONDS) and words[1:3] == ["podman", "run"],
    str(smoke),
)
check("container-smoke has no network by default", "--network" in words and "none" in words)
check("container-smoke never pulls what it judges", "--pull=never" in words, str(words))
scan = by_check["container-scan"][3].split(S.UNIT_SEPARATOR)
check(
    "container-scan mounts the built image and runs this image's scan-rootfs",
    "type=image,source=localhost/fx:sdlc,destination=/scan,rw=false" in scan
    and scan[-2:] == [S.GATE_IMAGE, "scan-rootfs"],
    str(scan),
)
check("container-scan fails on a fixable critical by default", "SDLC_SCAN_FAIL_ON=critical" in scan)
rows = container_plan(
    "ci",
    '[tool.sdlc.ci.container-build]\ntag = "localhost/other:t"\n'
    '[tool.sdlc.ci.container-smoke]\nseconds = 3\nargs = ["--version"]\nnetwork = true\n',
)
by_check = {r[0]: r for r in rows}
words = by_check["container-smoke"][3].split(S.UNIT_SEPARATOR)
check(
    "smoke and scan follow container-build's tag",
    "localhost/other:t" in words
    and "type=image,source=localhost/other:t,destination=/scan,rw=false"
    in by_check["container-scan"][3].split(S.UNIT_SEPARATOR),
    str(words),
)
check(
    "smoke takes its seconds, args and network from its table",
    words[0] == "3" and words[-1] == "--version" and "none" not in words,
    str(words),
)
for bad_cfg, why in (
    ("seconds = 0", "seconds"),
    ("seconds = true", "seconds"),
    ('seconds = "10"', "seconds"),
):
    with tempfile.TemporaryDirectory():
        try:
            container_plan("ci", f"[tool.sdlc.ci.container-smoke]\n{bad_cfg}\n")
            msg = None
        except S.CannotRunError as exc:
            msg = str(exc)
    check(f"smoke refuses {bad_cfg}", msg is not None and why in msg, str(msg))
try:
    container_plan("ci", '[tool.sdlc.ci.container-scan]\nfail_on = "severe"\n')
    msg = None
except S.CannotRunError as exc:
    msg = str(exc)
check("scan refuses an unknown severity", msg is not None and "fail_on" in msg, str(msg))
try:
    container_plan("ci", '[tool.sdlc.ci.container-build]\nkind = "image-build"\n')
    msg = None
except S.CannotRunError as exc:
    msg = str(exc)
check("a built-in container check cannot set a kind", msg is not None and "built in" in msg)


# ------------------------------------------------------------- [tool.sdlc] image
#
# bin/sdlc reads the key with awk before this script runs. If TOML and awk
# disagree, the repo believes it runs one image and the launcher ran another.


def image_error(pyproject: str, launcher_read: str | None) -> str | None:
    saved = dict(os.environ)
    os.environ["SDLC_HOST_REPO"] = "/host"
    os.environ.pop("SDLC_CONFIG_IMAGE", None)
    if launcher_read is not None:
        os.environ["SDLC_CONFIG_IMAGE"] = launcher_read
    try:
        return config_error(pyproject)
    finally:
        os.environ.clear()
        os.environ.update(saved)


check(
    "image: the launcher and TOML agreeing is fine",
    image_error('[tool.sdlc]\nimage = "beta"\n', "beta") is None,
)
msg = image_error('tool.sdlc.image = "beta"\n', None)
check(
    "image: a form the launcher could not read is refused",
    msg is not None and "could not read" in msg,
    str(msg),
)
msg = image_error('[tool.sdlc]\nimage = "a/b:c"\n', "a/b:c")
check("image: only a tag of the fleet image", msg is not None and "must be a tag" in msg, str(msg))
msg = image_error("[tool.sdlc]\nimage = 3\n", "3")
check("image: a number is not a tag", msg is not None and "must be a tag" in msg, str(msg))
msg = image_error("[tool.ruff]\n", "beta")
check("image: a launcher reading one that TOML lacks is refused", msg is not None, str(msg))
check("image: none set anywhere is fine", image_error("[tool.ruff]\n", None) is None)


# -------------------------------------------------------------- scan-rootfs
#
# grype's exit codes are the reverse of this file's: 2 is a finding and 1 is
# "could not scan". scan-rootfs translates them, and a broken cache is a 2, not
# a traceback. A fake grype stands in, recording its argv.

FAKE_GRYPE = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_DIR/argv"
exit "$(cat "$FAKE_DIR/rc")"
"""


def scan_with(grype_rc: int, *, broken_cache: bool = False, **env) -> tuple[int | str, list[str]]:
    with tempfile.TemporaryDirectory() as root:
        bindir = Path(root, "bin")
        bindir.mkdir()
        fake = bindir / "grype"
        fake.write_text(FAKE_GRYPE)
        fake.chmod(0o755)
        Path(root, "rc").write_text(str(grype_rc))
        Path(root, "scan").mkdir()
        saved = (dict(os.environ), S.SCAN_ROOT, S.GRYPE_DB, S.REPO)
        os.environ.update({"PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_DIR": root, **env})
        S.SCAN_ROOT = Path(root, "scan")
        S.REPO = Path(root)
        if broken_cache:
            Path(root, "cache").write_text("a file where a directory goes")
        S.GRYPE_DB = Path(root, "cache", "grype")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                out: int | str = S.t_scan_rootfs()
        except S.CannotRunError as exc:
            out = str(exc)
        finally:
            os.environ.clear()
            os.environ.update(saved[0])
            S.SCAN_ROOT, S.GRYPE_DB, S.REPO = saved[1:]
        argv_file = Path(root, "argv")
        argv = argv_file.read_text().split("\n") if argv_file.is_file() else []
    return out, argv


out, argv = scan_with(0)
check("scan-rootfs: grype 0 is clean", out == S.CLEAN, str(out))
check(
    "scan-rootfs fails on a fixable critical by default",
    "--fail-on" in argv
    and argv[argv.index("--fail-on") + 1] == "critical"
    and "--only-fixed" in argv,
    str(argv),
)
out, _ = scan_with(2)
check("scan-rootfs: grype 2, above the threshold, is a finding", out == S.FOUND, str(out))
out, _ = scan_with(1)
check("scan-rootfs: grype 1 could not scan, which is a 2", isinstance(out, str), str(out))
out, argv = scan_with(0, SDLC_SCAN_FAIL_ON="high", SDLC_SCAN_ONLY_FIXED="0")
check(
    "scan-rootfs takes its threshold and drops --only-fixed when told",
    argv[argv.index("--fail-on") + 1] == "high" and "--only-fixed" not in argv,
    str(argv),
)
out, _ = scan_with(0, SDLC_SCAN_FAIL_ON="severe")
check("scan-rootfs refuses an unknown severity", isinstance(out, str) and "severe" in out, str(out))
out, _ = scan_with(0, broken_cache=True)
check(
    "scan-rootfs: a cache it cannot write is a 2, not a traceback",
    isinstance(out, str) and "database cache" in out,
    str(out),
)

with tempfile.TemporaryDirectory() as root:
    Path(root, "Containerfile").write_text(CF)
    Path(root, "sdlc.toml").write_bytes(b"\xff\xfe not utf-8")
    S.REPO = Path(root)
    msg = cannot_run(S.config_document)
check("an sdlc.toml that is not UTF-8 is a 2, not a traceback", msg is not None, str(msg))


# ------------------------------------------------ arguments for a single check
#
# bin/sdlc joins the words after `--` with the unit separator and exports
# SDLC_ARGS. It is not a plan field: the plan is this script telling the host
# what to run, and `plain()` guards it. This goes the other way and ends up in
# an argv list, so there is no shell between the caller and the tool.

PASSTHROUGH = [
    (None, []),
    ("", []),
    ("-q", ["-q"]),
    (f"-k{S.UNIT_SEPARATOR}", ["-k", ""]),  # `-k ''` is a legitimate argument
    (f"tests/x.py::test_y{S.UNIT_SEPARATOR}-n0", ["tests/x.py::test_y", "-n0"]),
    # A word with spaces survives as ONE word, which is the whole reason the
    # separator is used instead of splitting on whitespace.
    (f"-k{S.UNIT_SEPARATOR}not slow", ["-k", "not slow"]),
]
for raw, want in PASSTHROUGH:
    saved = os.environ.pop("SDLC_ARGS", None)
    if raw is not None:
        os.environ["SDLC_ARGS"] = raw
    try:
        check(f"passthrough {raw!r}", S.passthrough() == want, str(S.passthrough()))
    finally:
        os.environ.pop("SDLC_ARGS", None)
        if saved is not None:
            os.environ["SDLC_ARGS"] = saved


def main() -> int:
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
