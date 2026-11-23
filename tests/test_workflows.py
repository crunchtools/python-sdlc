#!/usr/bin/env python3
"""The fleet file's guards, read from the files themselves.

examples/ci.yml is copied into every fleet repo byte for byte, and sdlc's own
.github/workflows/ci.yml is the one exception to it. Each guard below is a
line of one `if:`, `env:` or `permissions:` that keeps a secret, a write grant
or a signing key where it belongs, so each is pinned here rather than trusted
to review:

  - `write`'s `if:` carries `!inputs.digest`: a rollback dispatch never runs
    `write`; and `sdlc event`, given DIGEST in `write`'s env, exits 0 having
    done nothing (the second guard, run when the image has `sdlc event`)
  - examples/ci.yml is the fleet block of docs/marketplace-free-ci.md, byte
    for byte, so the design and the file cannot drift apart
  - `write` outside the triage events, `image` and `pypi` act on `check`'s
    plan only when `check` succeeded: each `if:` says
    `needs.check.result == 'success'` itself
  - `image`'s env maps REGISTRY_REDHAT_IO_USER and REGISTRY_REDHAT_IO_PASSWORD
  - in sdlc's own file, `test` is the only job whose `if:` admits
    pull_request, with `permissions: { contents: read }`, no `environment` and
    no `container`; and sdlc's `check` does not admit pull_request and is
    named `sdlc / plan`, never the required `sdlc / check`
  - `ai`, `write` and `gate` are byte for byte the same in both files, an
    anchor's definition (`&boot {...}`) read as its alias (`*boot`): sdlc's
    file defines them in a different first job. Parsed, they are equal too
  - and of the fleet file: no `uses:` at all, top-level `permissions: {}`,
    no environment but pypi's and renovate's (every secret is an org
    secret), and every job that holds a secret runs on
    crunchtools-uncached only while the variable names exactly that (never the
    cached class), a fork's pull_request_target on ubuntu-latest, and `check`
    and `gate` hold none
  - `renovate` runs only in crunchtools/constitution, on the weekly cron or a
    bare default-branch dispatch, within its token's hour, and holds the
    Renovate App key alone: no GITHUB_TOKEN, `permissions: {}`

sdlc's own ci.yml and `sdlc event` land in their own pull requests; until
each exists, the checks that read it are reported as skipped, not passed.

Run inside the image, which has the interpreter and PyYAML the script needs:
  podman run --rm -v .:/src:ro,z -w /src --entrypoint /opt/constitution-venv/bin/python3 \
      quay.io/crunchtools/sdlc:<tag> tests/test_workflows.py [SDLC_CI_YML]
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
FLEET = ROOT / "examples" / "ci.yml"
OWN = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".github" / "workflows" / "ci.yml"
FAILED: list[str] = []
SKIPPED: list[str] = []

CHECK_OK = "needs.check.result == 'success'"
# A GitHub App installation token lives an hour; `renovate` must end inside it.
TOKEN_MINUTES = 55
# A status function in an `if:` replaces GitHub's implicit success(), which
# is what otherwise skips a job whose needs did not all succeed.
# The event literally, not pull_request_target or pull_request_review_comment.
PULL_REQUEST = re.compile(r"""['"]pull_request['"]""")
SECRET = re.compile(r"\$\{\{\s*secrets\.")
SHARED = ("ai", "write", "gate")
# Every job that holds a secret: Exedore's uncached class while the org variable
# names exactly it, GitHub's otherwise and for a fork's pull_request_target.
GUARDED_RUNNER = (
    "${{ (github.event_name == 'issues'"
    ' && contains(fromJSON(\'["OWNER","MEMBER","COLLABORATOR"]\'),'
    " github.event.issue.author_association)"
    " || github.event_name != 'issues' && (github.event_name != 'pull_request_target'"
    " || github.event.pull_request.head.repo.full_name == github.repository))"
    " && vars.UNCACHED_RUNNER_LABEL == 'crunchtools-uncached'"
    " && 'crunchtools-uncached' || 'ubuntu-latest' }}"
)
DOC = ROOT / "docs" / "marketplace-free-ci.md"
# The doc's first ```yaml block, which "The fleet file" opens with.
FLEET_BLOCK = re.compile(r"^```yaml\n(?P<body>.*?)^```$", re.M | re.S)
# `key: &name <value>` to the end of the line: the anchor's definition.
ANCHOR = re.compile(r"&(\w+) .*$", re.M)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def skip(name: str, why: str) -> None:
    print(f"skip {name} -- {why}")
    SKIPPED.append(name)


def jobs_of(text: str) -> dict:
    return (yaml.safe_load(text) or {}).get("jobs") or {}


def job_text(text: str, job_id: str) -> str:
    """The lines of one job under `jobs:`, from its key to the next job's, as written."""
    lines = text.splitlines(keepends=True)
    top = next(i for i, x in enumerate(lines) if re.match(r"jobs:\s*$", x))
    start = next(i for i in range(top + 1, len(lines)) if lines[i].startswith(f"  {job_id}:"))
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"  [\w-]+:", lines[i])),
        len(lines),
    )
    return "".join(lines[start:end])


def as_aliases(text: str) -> str:
    """The text with each anchor's definition written as its alias."""
    return ANCHOR.sub(r"*\1", text)


def admits_pull_request(job: dict) -> bool:
    """A job runs on a `pull_request` event: no `if:` at all, or one naming it."""
    cond = job.get("if")
    return cond is None or bool(PULL_REQUEST.search(str(cond)))


# GitHub's expression syntax, as far as these files use it, parsed as Python:
# a context path becomes a string the walker looks up, `&&`/`||`/`!` the Python
# words (which return operands, as GitHub's do), and only the four functions
# below are callable. Nothing is executed: the tree is walked.
CONTEXT_PATH = re.compile(r"\b(?:github|vars|needs|inputs)(?:\.[\w-]+)+")
FUNCTIONS = {
    "fromJSON": json.loads,
    "contains": lambda haystack, needle: needle in haystack,
    "format": lambda fmt, *args: fmt.format(*args),
    "always": lambda: True,
}


def bool_op(node: ast.BoolOp, ctx: dict[str, object]) -> object:
    """`and`/`or` as GitHub's `&&`/`||`: the deciding operand, not a bool."""
    value: object = None
    for operand in node.values:
        value = walk(operand, ctx)
        if bool(value) == isinstance(node.op, ast.Or):
            return value
    return value


def compare(node: ast.Compare, ctx: dict[str, object]) -> bool:
    if len(node.ops) != 1 or not isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
        raise ValueError(f"not a comparison these files use: {ast.dump(node)}")
    same = walk(node.left, ctx) == walk(node.comparators[0], ctx)
    return same if isinstance(node.ops[0], ast.Eq) else not same


def call(node: ast.Call, ctx: dict[str, object]) -> object:
    if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
        raise ValueError(f"not a function these files use: {ast.dump(node)}")
    return FUNCTIONS[node.func.id](*(walk(a, ctx) for a in node.args))


def unary(node: ast.UnaryOp, ctx: dict[str, object]) -> bool:
    if not isinstance(node.op, ast.Not):
        raise TypeError(f"not an operator these files use: {ast.dump(node)}")
    return not walk(node.operand, ctx)


WALKERS: dict[type, Callable[[Any, dict[str, object]], object]] = {
    ast.Expression: lambda node, ctx: walk(node.body, ctx),
    ast.Constant: lambda node, _ctx: node.value,
    ast.Subscript: lambda node, ctx: ctx.get(str(walk(node.slice, ctx)), ""),  # ctx["a.b"]
    ast.BoolOp: bool_op,
    ast.UnaryOp: unary,
    ast.Compare: compare,
    ast.Call: call,
}


def walk(node: ast.AST, ctx: dict[str, object]) -> object:
    """The value of one node of a parsed expression; anything unknown is an error."""
    walker = WALKERS.get(type(node))
    if walker is None:
        raise ValueError(f"not an expression these files use: {ast.dump(node)}")
    return walker(node, ctx)


def evaluate(expr: str, ctx: dict[str, object]) -> object:
    """An `if:` or `${{ }}` expression's value for the context `ctx` (dotted paths)."""
    py = expr.strip().removeprefix("${{").removesuffix("}}")
    py = CONTEXT_PATH.sub(lambda m: f"ctx[{m[0]!r}]", py)
    py = py.replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?!=)", " not ", py)
    return walk(ast.parse(py.strip(), mode="eval"), ctx)


def event(name: str, **rest: object) -> dict[str, object]:
    base = {
        "github.event_name": name,
        "github.repository": "crunchtools/app",
        "github.ref": "refs/heads/main",
        "github.event.repository.default_branch": "main",
        "vars.UNCACHED_RUNNER_LABEL": "gha-uncached",
    }
    return {**base, **rest}


# ---------------------------------------------------------- the fleet file

fleet_text = FLEET.read_text()
block = FLEET_BLOCK.search(DOC.read_text())
check(
    "fleet: examples/ci.yml is the doc's fleet block, byte for byte",
    block is not None and block["body"] == fleet_text,
    're-copy it from docs/marketplace-free-ci.md\'s "The fleet file"',
)
fleet = jobs_of(fleet_text)
parsed = yaml.safe_load(fleet_text)

write_if = str(fleet["write"]["if"])
check("fleet: write's if: has !inputs.digest", "!inputs.digest" in write_if, write_if)
check(
    "fleet: write's DIGEST comes from inputs.digest",
    fleet["write"]["env"].get("DIGEST") == "${{ inputs.digest }}",
)
# Outside the two triage events, the only way in is through a successful check.
triage, _, planned = write_if.partition(CHECK_OK)
check("fleet: write's if: requires a successful check outside triage", bool(planned), write_if)
check(
    "fleet: before that test, write's if: admits only the triage events",
    set(re.findall(r"github\.event_name == '(\w+)'", triage)) == {"pull_request_target", "issues"},
    triage,
)
for job_id in ("image", "pypi"):
    cond = str(fleet[job_id]["if"])
    check(
        f"fleet: {job_id}'s if: names needs.check.result == 'success' itself",
        CHECK_OK in cond,
        cond,
    )
    needs = fleet[job_id]["needs"]
    needs = needs if isinstance(needs, list) else [needs]
    check(f"fleet: {job_id} needs check", "check" in needs)

image_env = fleet["image"]["env"]
for name in ("REGISTRY_REDHAT_IO_USER", "REGISTRY_REDHAT_IO_PASSWORD"):
    check(f"fleet: image's env maps {name}", image_env.get(name) == f"${{{{ secrets.{name} }}}}")

check("fleet: no `uses:` anywhere", not re.search(r"^\s*-?\s*uses:", fleet_text, re.M))
check("fleet: top-level permissions are {}", parsed.get("permissions") == {})
# Every secret is an org secret: no environment to provision in any repo.
# pypi's names PyPI's trusted publisher and holds no secret; renovate's runs in
# one repo. Neither needs setting up anywhere else.
NAMED_ENVIRONMENTS = {"pypi", "renovate"}
for job_id, job in fleet.items():
    check(
        f"fleet: {job_id} names no environment but pypi or renovate",
        job.get("environment") in {None} | NAMED_ENVIRONMENTS,
        str(job.get("environment")),
    )
    holds = SECRET.search(yaml.safe_dump(job)) or job.get("environment") == "pypi"
    if holds and job_id != "renovate":
        check(
            f"fleet: {job_id} shares the secret jobs' runner", job.get("runs-on") == GUARDED_RUNNER
        )
fork = {"github.event.pull_request.head.repo.full_name": "stranger/app"}
same_repo = {"github.event.pull_request.head.repo.full_name": "crunchtools/app"}
uncached = {"vars.UNCACHED_RUNNER_LABEL": "crunchtools-uncached"}
author = "github.event.issue.author_association"
guarded_routes = (
    *(
        (f"an issue by {who}", event("issues", **uncached, **{author: who}), "crunchtools-uncached")
        for who in ("OWNER", "MEMBER", "COLLABORATOR")
    ),
    *(
        (f"an issue by {who}", event("issues", **uncached, **{author: who}), "ubuntu-latest")
        for who in ("CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE", "")
    ),
    ("a push", event("push", **uncached), "crunchtools-uncached"),
    (
        "a same-repo PR's target run",
        event("pull_request_target", **uncached, **same_repo),
        "crunchtools-uncached",
    ),
    ("a fork's target run", event("pull_request_target", **uncached, **fork), "ubuntu-latest"),
    ("a deleted fork's target run", event("pull_request_target", **uncached), "ubuntu-latest"),
    (
        "a push with no runner variable",
        event("push", **{"vars.UNCACHED_RUNNER_LABEL": ""}),
        "ubuntu-latest",
    ),
    (
        "a push with the variable on the cached class",
        event("push", **{"vars.UNCACHED_RUNNER_LABEL": "crunchtools-cached"}),
        "ubuntu-latest",
    ),
    ("a push with any other label", event("push"), "ubuntu-latest"),
)
for label, ctx, want in guarded_routes:
    got = evaluate(GUARDED_RUNNER, ctx)
    check(f"fleet: guarded jobs run {label} on {want}", got == want, str(got))
for job_id in ("check", "gate"):
    check(f"fleet: {job_id} holds no secret", not SECRET.search(yaml.safe_dump(fleet[job_id])))
# check: the one job a fork's PR runs, and the only one on our runners.
check_if, check_runner = str(fleet["check"]["if"]), str(fleet["check"]["runs-on"])
admitted = {
    "pull_request": event("pull_request"),
    "schedule": event("schedule"),
    "workflow_dispatch": event("workflow_dispatch"),
    "a default-branch push": event("push"),
}
refused = {
    "a push to another branch": event("push", **{"github.ref": "refs/heads/topic"}),
    "a tag push": event("push", **{"github.ref": "refs/tags/v1.0.0"}),
    "pull_request_target": event("pull_request_target"),
    "issues": event("issues"),
    "a review comment": event("pull_request_review_comment"),
}
for label, ctx in admitted.items():
    check(f"fleet: check runs on {label}", bool(evaluate(check_if, ctx)))
for label, ctx in refused.items():
    check(f"fleet: check does not run on {label}", not evaluate(check_if, ctx))
routes = (
    ("a same-repo PR", event("pull_request", **same_repo), "gha-uncached"),
    ("a fork's PR", event("pull_request", **fork), "ubuntu-latest"),
    ("a push", event("push"), "gha-uncached"),
    (
        "a push with no runner variable",
        event("push", **{"vars.UNCACHED_RUNNER_LABEL": ""}),
        "ubuntu-latest",
    ),
)
for label, ctx, want in routes:
    got = evaluate(check_runner, ctx)
    check(f"fleet: check runs {label} on {want}", got == want, str(got))

# renovate: the org's Renovate, from the constitution only, holding its App key alone.
renovate = fleet["renovate"]
renovate_if = str(renovate["if"])
home = {"github.repository": "crunchtools/constitution"}
weekly = {"github.event.schedule": "17 6 * * 1"}
for label, ctx in {
    "the weekly cron": event("schedule", **home, **weekly),
    "a bare default-branch dispatch": event("workflow_dispatch", **home, **{"inputs.version": ""}),
}.items():
    check(f"fleet: renovate runs on {label}", bool(evaluate(renovate_if, ctx)))
for label, ctx in {
    "the weekly cron in another repo": event("schedule", **weekly),
    "the 6-hourly poll": event("schedule", **home, **{"github.event.schedule": "47 */6 * * *"}),
    "a version dispatch": event("workflow_dispatch", **home, **{"inputs.version": "1.2.3"}),
    "a dispatch from a branch": event(
        "workflow_dispatch", **home, **{"inputs.version": "", "github.ref": "refs/heads/topic"}
    ),
    "a push": event("push", **home),
    "pull_request": event("pull_request", **home),
    "pull_request_target": event("pull_request_target", **home),
}.items():
    check(f"fleet: renovate does not run on {label}", not evaluate(renovate_if, ctx))
check(
    "fleet: renovate's weekly run goes to the uncached class only while the variable names it",
    evaluate(
        GUARDED_RUNNER, event("schedule", **{"vars.UNCACHED_RUNNER_LABEL": "crunchtools-uncached"})
    )
    == "crunchtools-uncached"
    and evaluate(
        GUARDED_RUNNER, event("schedule", **{"vars.UNCACHED_RUNNER_LABEL": "crunchtools-cached"})
    )
    == "ubuntu-latest",
)
check(
    "fleet: renovate holds the Renovate App key and nothing else",
    renovate.get("env") == {"RENOVATE_APP_KEY": "${{ secrets.RENOVATE_APP_KEY }}"},
    str(renovate.get("env")),
)
check("fleet: renovate's token is granted nothing", renovate.get("permissions") == {})
check(
    "fleet: renovate ends inside its token's hour",
    0 < int(renovate.get("timeout-minutes") or 0) <= TOKEN_MINUTES,
)
check(
    "fleet: renovate's key is in the renovate environment",
    renovate.get("environment") == "renovate",
)

# gate: required, so it runs on pull_request_target and on a review comment
# only from someone with write access, and on nothing else.
gate_if = str(fleet["gate"]["if"])
for who in ("OWNER", "MEMBER", "COLLABORATOR"):
    ctx = event("pull_request_review_comment", **{"github.event.comment.author_association": who})
    check(f"fleet: gate re-runs on a review comment by an {who}", bool(evaluate(gate_if, ctx)))
for who in ("CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE"):
    ctx = event("pull_request_review_comment", **{"github.event.comment.author_association": who})
    check(f"fleet: gate does not re-run on a review comment by {who}", not evaluate(gate_if, ctx))
check(
    "fleet: gate runs on pull_request_target", bool(evaluate(gate_if, event("pull_request_target")))
)
for name in ("pull_request", "push", "issues", "schedule", "workflow_dispatch"):
    check(f"fleet: gate does not run on {name}", not evaluate(gate_if, event(name)))
check(
    "fleet: the required checks carry their event in the name",
    fleet["check"]["name"] == "sdlc / check (${{ github.event_name }})"
    and fleet["gate"]["name"] == "sdlc / gate (${{ github.event_name }})",
)

# ------------------------------------------------------- sdlc's own file

if not OWN.is_file():
    skip("sdlc's own ci.yml", f"{OWN} does not exist yet")
else:
    own_text = OWN.read_text()
    own = jobs_of(own_text)
    if "test" not in own:
        skip("sdlc's own ci.yml", "it is not the new file yet: no `test` job")
    else:
        admitting = sorted(j for j, job in own.items() if admits_pull_request(job))
        only_test = admitting == ["test"]
        check("own: test is the only job that admits pull_request", only_test, str(admitting))
        test = own["test"]
        read_only = test.get("permissions") == {"contents": "read"}
        check("own: test has permissions { contents: read }", read_only)
        check("own: test has no environment", "environment" not in test)
        check("own: test has no container", "container" not in test)
        check("own: test holds no secret", not SECRET.search(yaml.safe_dump(test)))
        check("own: check does not admit pull_request", not admits_pull_request(own["check"]))
        check(
            "own: check is named sdlc / plan, never the required name",
            str(own["check"].get("name", "")).startswith("sdlc / plan ")
            and own["check"].get("name") != fleet["check"]["name"],
            str(own["check"].get("name")),
        )
        own_write_if = str(own["write"]["if"])
        check("own: write's if: has !inputs.digest", "!inputs.digest" in own_write_if)
        for job_id in SHARED:
            same = job_id in own and as_aliases(job_text(own_text, job_id)) == as_aliases(
                job_text(fleet_text, job_id)
            )
            check(f"own: {job_id} is byte for byte the fleet file's", same)
            check(f"own: {job_id} parses as the fleet file's", own.get(job_id) == fleet[job_id])

# -------------------------------------------- the dispatcher's own guard

SDLC = ROOT / "sdlc"
if not re.search(r"""["']event["']""", SDLC.read_text()):
    skip("sdlc event with DIGEST", "the image's `sdlc` has no `event` subcommand yet")
else:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GITHUB_JOB": "write",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "DIGEST": "sha256:" + "0" * 64,
        "VERSION": "1.2.3",
    }
    done = subprocess.run(
        [sys.executable, str(SDLC), "event"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    check(
        "dispatcher: write with DIGEST exits 0 having done nothing",
        done.returncode == 0,
        f"exit {done.returncode}: {done.stderr.strip()[-300:]}",
    )


def main() -> int:
    if FAILED:
        print(f"\n{len(FAILED)} failed")
        return 1
    print(f"\nall passed ({len(SKIPPED)} skipped)" if SKIPPED else "\nall passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
