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
  - `image`'s env maps REGISTRY_REDHAT_IO_USER and REGISTRY_REDHAT_IO_PASSWORD;
    image never runs on a PR
  - `boot` holds no secret: no `secrets.` in it, `permissions: { contents: read }`,
    an env of GITHUB_TOKEN and `image`'s result alone, `check`'s runner, and an
    `if:` that wants check and image to have succeeded and the plan to say `boot`
  - `promote` needs `boot`, its `if:` admits a boot that succeeded (or was skipped
    with none planned), and its env maps SDLC_BOOT
  - `write` needs `image` and `boot`, maps their results, and its `if:` holds a
    release back until each succeeded or was skipped with none planned, so no
    vX.Y.Z is tagged without an image (a1667); sdlc's own `write` waits on its
    `image`, its `image` no longer needs `write`, and its `sign` does
  - in sdlc's own file, `test` is the only job whose `if:` admits
    pull_request, with `permissions: { contents: read }`, no `environment` and
    no `container`; and sdlc's `check` does not admit pull_request and is
    named `sdlc / plan`, never the required `sdlc / check`
  - `ai` and `gate` are byte for byte the same in both files, an anchor's
    definition (`&boot {...}`) read as its alias (`*boot`): sdlc's file
    defines them in a different first job. Parsed, they are equal too. `write`
    parses the same but for `needs`, `if` and `env`: sdlc has no `boot` job
  - and of the fleet file: no `uses:` at all, top-level `permissions: {}`,
    no environment but pypi's and renovate's (every secret is an org
    secret), and every job that holds a secret runs on
    crunchtools-uncached only while the variable names exactly that (never the
    cached class), a fork's pull_request_target on ubuntu-latest, and `check`
    and `gate` hold none
  - `renovate` runs only in crunchtools/constitution, on the weekly cron or a
    bare default-branch dispatch, within its token's hour, and holds the
    Renovate App key alone: no GITHUB_TOKEN, `permissions: {}`
  - every bootstrap step, in the fleet file and in `ai`, `write` and `gate`
    of sdlc's own, runs one launch line: podman.socket started, /run/podman
    0711, the socket chowned to the job user and never world-writable; a
    `podman --remote` run as `--user 0` with `label=disable`, the socket at
    /var/run/docker.sock and the workspace's parent mounted, exactly the fixed
    `--env` names, and no other option; the bootstrap pinned by sha256 digest,
    given `event`

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
import shlex
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
SHARED = ("ai", "gate")
# What sdlc's own `write` may differ in: it waits on `image`, as there is no `boot`.
WRITE_OWN_KEYS = ("needs", "if", "env")
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
# `key: &name <value>` to the end of the line, or the indented block under it.
ANCHOR = re.compile(r"&(\w+)(?: .*$|\n(?:      .*(?:\n|$))+)", re.M)


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
    """The text with each anchor's definition (inline, or a block below it) as its alias."""
    return ANCHOR.sub(
        lambda m: f"*{m.group(1)}" + ("\n" if m.group(0).endswith("\n") else ""), text
    )


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
    "cancelled": lambda: False,
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
    # fromJSON(...).key: an object's property, null when it has none.
    ast.Attribute: lambda node, ctx: (walk(node.value, ctx) or {}).get(node.attr),
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
    want = f"${{{{ secrets.{name} }}}}"
    check(f"fleet: image's env maps {name}; image never runs on a PR", image_env.get(name) == want)

boot_job = fleet["boot"]
boot_if = str(boot_job["if"])
check("fleet: boot holds no secret", "secrets." not in job_text(fleet_text, "boot"))
check(
    "fleet: boot has permissions { contents: read }",
    boot_job.get("permissions") == {"contents": "read"},
)
check(
    "fleet: boot runs where the secret jobs run, the socket being its credential",
    boot_job["runs-on"] == GUARDED_RUNNER,
    str(boot_job["runs-on"]),
)
check("fleet: boot needs exactly check and image", sorted(boot_job["needs"]) == ["check", "image"])
check(
    "fleet: boot's if: wants check and image to have succeeded and the plan's boot",
    CHECK_OK in boot_if
    and "needs.image.result == 'success'" in boot_if
    and "fromJSON(needs.check.outputs.result || '{}').boot" in boot_if,
    boot_if,
)
check(
    "fleet: boot's env is GITHUB_TOKEN and image's result alone",
    boot_job["env"]
    == {
        "GITHUB_TOKEN": "${{ github.token }}",
        "SDLC_IMAGE_RESULT": "${{ needs.image.outputs.result }}",
    },
    str(boot_job["env"]),
)
promote_if = str(fleet["promote"]["if"])
check("fleet: promote needs boot", "boot" in fleet["promote"]["needs"])
check(
    "fleet: promote's if: wants boot to have succeeded, or none planned",
    "needs.boot.result == 'success'" in promote_if
    and "needs.boot.result == 'skipped' && !fromJSON(needs.check.outputs.result || '{}').boot"
    in promote_if,
    promote_if,
)
check(
    "fleet: promote's env maps SDLC_BOOT",
    fleet["promote"]["env"].get("SDLC_BOOT") == "${{ needs.boot.result }}",
)

# a1667: write tags a release only after the gates. It needs image and boot, and
# its if: holds a release back unless each succeeded or was skipped unplanned.
write_job = fleet["write"]
check("fleet: write needs image and boot", {"image", "boot"} <= set(write_job["needs"]))
check(
    "fleet: write's env maps SDLC_IMAGE and SDLC_BOOT",
    write_job["env"].get("SDLC_IMAGE") == "${{ needs.image.result }}"
    and write_job["env"].get("SDLC_BOOT") == "${{ needs.boot.result }}",
)
RELEASE = {"version": "1.2.3", "publish": ""}


def runs_write(
    job_if: str, name: str, plan: dict[str, Any], image: str, boot: str | None = None
) -> bool:
    """Whether `write` runs for `name` with `check`'s plan and the gates' results."""
    ctx = event(
        name,
        **{
            "needs.check.result": "success",
            "needs.check.outputs.result": json.dumps(plan),
            "needs.image.result": image,
            "needs.boot.result": boot,
        },
    )
    return bool(evaluate(job_if, ctx))


GATED = {"write": True, "release": RELEASE, "image": True, "boot": True}
for label, plan, image, boot, want in (
    ("image and boot passed", GATED, "success", "success", True),
    ("image failed", GATED, "failure", "skipped", False),
    ("image was cancelled", GATED, "cancelled", "skipped", False),
    ("a planned image was skipped", GATED, "skipped", "skipped", False),
    ("boot failed", GATED, "success", "failure", False),
    ("a planned boot was skipped", GATED, "success", "skipped", False),
    ("image passed, no boot planned", {**GATED, "boot": False}, "success", "skipped", True),
    (
        "neither planned (a library)",
        {"write": True, "release": RELEASE},
        "skipped",
        "skipped",
        True,
    ),
):
    check(
        f"fleet: on a release push, write {'runs' if want else 'does not run'} when {label}",
        runs_write(write_if, "push", plan, image, boot) is want,
    )
check(
    "fleet: the weekly audit is not held back by a failed image",
    runs_write(
        write_if, "schedule", {"write": True, "audit": {}, "image": True}, "failure", "skipped"
    ),
)
check(
    "fleet: write still runs on pull_request_target with image and boot skipped",
    runs_write(write_if, "pull_request_target", {}, "skipped", "skipped"),
)

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
    if holds:
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
        same_repo_pr = event(
            "pull_request", **{"github.event.pull_request.head.repo.full_name": "crunchtools/app"}
        )
        fork_pr = event(
            "pull_request", **{"github.event.pull_request.head.repo.full_name": "stranger/app"}
        )
        label_set = {"vars.UNCACHED_RUNNER_LABEL": "crunchtools-uncached"}
        no_label = {"vars.UNCACHED_RUNNER_LABEL": ""}
        for label, ctx, want in (
            ("a same-repo PR, label set", {**same_repo_pr, **label_set}, "crunchtools-uncached"),
            ("a same-repo PR, no label", {**same_repo_pr, **no_label}, "ubuntu-latest"),
            ("a fork's PR, label set", {**fork_pr, **label_set}, "ubuntu-latest"),
        ):
            got = evaluate(str(test["runs-on"]), ctx)
            check(f"own: test runs {label} on {want}", got == want, str(got))
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
        # The bootstrap dispatch (MODE=bootstrap): approved, then built and
        # signed; release, weekly and rollback keep their own paths.
        approve_if = str(own["approve"]["if"])
        check(
            "own: approve runs on a rollback or bootstrap dispatch from the default branch",
            "(inputs.digest != '' || inputs.bootstrap)" in approve_if
            and "github.event_name == 'workflow_dispatch'" in approve_if
            and "github.event.repository.default_branch" in approve_if,
        )
        image = own["image"]
        check("own: image needs approve", "approve" in image.get("needs", []))
        check(
            "own: image runs a bootstrap only once approve succeeded",
            "inputs.bootstrap && needs.approve.result == 'success'" in str(image["if"]),
        )
        image_mode = str(image["env"]["MODE"])
        check(
            "own: image's MODE is bootstrap, weekly or release",
            image_mode.startswith("${{ inputs.bootstrap && 'bootstrap' ||")
            and "'weekly'" in image_mode
            and "'release'" in image_mode,
        )
        check(
            "own: image hands self.sh the version and approve's result",
            image["env"].get("VERSION") == "${{ inputs.version }}"
            and image["env"].get("APPROVED") == "${{ needs.approve.result }}",
        )
        sign = own["sign"]
        sign_mode = str(sign["env"]["MODE"])
        check(
            "own: sign's MODE is bootstrap first, then rollback, weekly or release",
            sign_mode.startswith("${{ inputs.bootstrap && 'bootstrap' ||")
            and "'rollback'" in sign_mode
            and "'weekly'" in sign_mode
            and "'release'" in sign_mode,
        )
        check(
            "own: sign never runs a bootstrap on approve alone",
            "needs.approve.result == 'success' && !inputs.bootstrap" in str(sign["if"]),
        )
        own_write = own["write"]
        check("own: write needs image", "image" in own_write["needs"])
        check("own: image does not need write", "write" not in own["image"]["needs"])
        check("own: sign needs write", "write" in own["sign"]["needs"])
        check(
            "own: write's env maps SDLC_IMAGE",
            own_write["env"].get("SDLC_IMAGE") == "${{ needs.image.result }}",
        )
        own_plan = {"write": True, "release": RELEASE}
        for result, want in (("success", True), ("failure", False), ("skipped", False)):
            check(
                f"own: on a release push, write {'runs' if want else 'skips'}: image {result}",
                runs_write(own_write_if, "push", own_plan, result) is want,
            )
        sign_ctx = event(
            "push", **{"needs.image.result": "success", "needs.approve.result": "skipped"}
        )
        for result, want in (("success", True), ("failure", False), ("skipped", False)):
            check(
                f"own: on a release push, sign {'runs' if want else 'skips'}: write {result}",
                bool(evaluate(str(own["sign"]["if"]), {**sign_ctx, "needs.write.result": result}))
                is want,
            )
        check(
            "own: sign maps write's result and release",
            own["sign"]["env"].get("SDLC_WRITE") == "${{ needs.write.result }}"
            and own["sign"]["env"].get("SDLC_RELEASE") == "${{ needs.write.outputs.release }}",
        )
        check(
            "own: write is the fleet file's but for needs, if and env",
            {k: v for k, v in own_write.items() if k not in WRITE_OWN_KEYS}
            == {k: v for k, v in fleet["write"].items() if k not in WRITE_OWN_KEYS},
        )
        check(
            "own: write's env is the fleet file's without SDLC_BOOT",
            own_write["env"]
            == {k: v for k, v in fleet["write"]["env"].items() if k != "SDLC_BOOT"},
        )
        for job_id in SHARED:
            same = job_id in own and as_aliases(job_text(own_text, job_id)) == as_aliases(
                job_text(fleet_text, job_id)
            )
            check(f"own: {job_id} is byte for byte the fleet file's", same)
            check(f"own: {job_id} parses as the fleet file's", own.get(job_id) == fleet[job_id])

# ------------------------------------------------------------ the launch step

SOCKET = "/run/podman/podman.sock"
# The commands before `podman run`, in order: /run/podman can be traversed but
# not listed, and the socket is the job user's alone, never 0666.
LAUNCH_PRELUDE = [
    ["sudo", "systemctl", "start", "podman.socket"],
    ["sudo", "chmod", "0711", "/run/podman"],
    ["sudo", "chown", "$(id -u)", SOCKET],
]
LAUNCH_REMOTE = ["podman", "--remote", "--url", f"unix://{SOCKET}", "run", "--rm"]
LAUNCH_MOUNTS = [f"{SOCKET}:/var/run/docker.sock", "${RUNNER_WORKSPACE%/*}:${RUNNER_WORKSPACE%/*}"]
# Every name the bootstrap reads; anything else stays on the runner.
LAUNCH_ENV = [
    "GITHUB_*", "RUNNER_*", "ACTIONS_*", "CI", "SDLC_*", "VERSION", "DIGEST",
    "QUAY_*", "RHSM_*", "REGISTRY_REDHAT_IO_*", "COSIGN_*", "ASHIGARU_*",
    "RENOVATE_*", "CLAUDE_CODE_OAUTH_TOKEN", "OPENROUTER_API_KEY",
]  # fmt: skip
LAUNCH_OPTIONS = [
    ("--user", "0"),
    ("--security-opt", "label=disable"),
    *(("-v", mount) for mount in LAUNCH_MOUNTS),
    ("-w", "$GITHUB_WORKSPACE"),
    *(("--env", name) for name in LAUNCH_ENV),
]
BOOTSTRAP = re.compile(r"quay\.io/crunchtools/sdlc-bootstrap@sha256:[0-9a-f]{64}")
# A chmod mode clause that grants write to others: `o+w`, `a=rw`, a bare `+w`.
SYMBOLIC = re.compile(r"(?P<who>[ugoa]*)(?P<op>[-+=])(?P<perms>[rwxXst]*)")


def world_writable(mode: str) -> bool:
    """Whether a chmod mode, octal or symbolic, lets anyone write."""
    if mode.isdigit():
        return bool(int(mode, 8) & 0o002)
    return any(
        (m := SYMBOLIC.fullmatch(clause))
        and m["op"] != "-"
        and "w" in m["perms"]
        and (not m["who"] or set(m["who"]) & {"o", "a"})
        for clause in mode.split(",")
    )


def launch_checks(label: str, jobs: dict, job_ids: list[str]) -> None:
    """The bootstrap's `run:` line in each of job_ids, one command at a time."""
    runs = {
        job_id: [
            step["run"]
            for step in (jobs.get(job_id) or {}).get("steps") or []
            if "sdlc-bootstrap" in str(step.get("run", ""))
        ]
        for job_id in job_ids
    }
    check(
        f"{label}: {', '.join(job_ids)} each run the bootstrap once",
        all(len(lines) == 1 for lines in runs.values()),
        str({job_id: len(lines) for job_id, lines in runs.items()}),
    )
    launches = {line for lines in runs.values() for line in lines}
    check(f"{label}: they share one launch line", len(launches) == 1)
    if not launches:
        return
    words = shlex.split(launches.pop())
    commands: list[list[str]] = [[]]
    for word in words:
        if word == "&&":
            commands.append([])
        else:
            commands[-1].append(word)
    *prelude, podman = commands
    check(
        f"{label}: the launch starts podman.socket, 0711 /run/podman, chowns the socket",
        prelude == LAUNCH_PRELUDE,
        str(prelude),
    )
    loose = [
        " ".join(c)
        for c in commands
        if "chmod" in c[:-1] and world_writable(c[c.index("chmod") + 1])
    ]
    check(f"{label}: nothing in the launch is made world-writable", not loose, str(loose))
    check(
        f"{label}: the bootstrap runs on the host's podman, removed after",
        podman[: len(LAUNCH_REMOTE)] == LAUNCH_REMOTE,
        " ".join(podman[: len(LAUNCH_REMOTE)]),
    )
    rest = podman[len(LAUNCH_REMOTE) :]
    options: list[tuple[str, str]] = []
    while len(rest) > 1 and rest[0].startswith("-"):
        options.append((rest[0], rest[1]))
        rest = rest[2:]
    check(
        f"{label}: as root in the container, SELinux labels off",
        ("--user", "0") in options and ("--security-opt", "label=disable") in options,
    )
    mounts = [value for flag, value in options if flag in ("-v", "--volume")]
    check(
        f"{label}: mounts the socket as docker.sock and the workspace's parent",
        mounts == LAUNCH_MOUNTS,
        str(mounts),
    )
    env = [value for flag, value in options if flag in ("--env", "-e")]
    check(
        f"{label}: forwards exactly the fixed --env names",
        env == LAUNCH_ENV,
        f"missing {sorted(set(LAUNCH_ENV) - set(env))}, extra {sorted(set(env) - set(LAUNCH_ENV))}",
    )
    check(
        f"{label}: no option but those",
        options == LAUNCH_OPTIONS,
        f"missing {[o for o in LAUNCH_OPTIONS if o not in options]},"
        f" extra {[o for o in options if o not in LAUNCH_OPTIONS]}",
    )
    check(
        f"{label}: runs the bootstrap pinned by digest, with `event`",
        rest[1:] == ["event"] and bool(BOOTSTRAP.fullmatch(rest[0])),
        " ".join(rest),
    )


for mode, loose in (
    *((mode, True) for mode in ("0666", "0777", "0002", "o+w", "a=rw", "+w", "u+x,o+w")),
    *((mode, False) for mode in ("0711", "0755", "0600", "g+w", "o-w", "u=rwx", "a+x")),
):
    check(
        f"launch: chmod {mode} {'is' if loose else 'is not'} world-writable",
        world_writable(mode) is loose,
    )
launch_checks("fleet", fleet, list(fleet))
if OWN.is_file() and "test" in (own_jobs := jobs_of(OWN.read_text())):
    launch_checks("own", own_jobs, list(SHARED))
else:
    skip("own: the launch step", f"{OWN} is not the new file yet")

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
