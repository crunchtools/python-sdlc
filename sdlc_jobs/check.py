"""The `check` job: gate the commit, then say which later jobs it needs.

It holds a read token, `security-events: write`, `actions: write` and the runner's
container socket, and no secret. That is the one level a pull request may run, so
the repo's own code reaches it only through the launcher's nested containers.

It does four things, in this order, because a failed check still prints its plan
(nothing acts on it: every later job also needs `check` to have succeeded):

1. Work out the plan and write it to `$GITHUB_OUTPUT` as `result` (the schema is in
   `_core.plan`). It is a plan, never an authority: `write` and `image` re-derive
   what they act on.
2. Run the gates (pull_request, push, dispatch): the launcher's `ci`, which is the
   fleet's `sdlc ci` -- ruff, mypy, pytest, semgrep, pip-audit, CodeQL, the
   constitution validator, and the container build, smoke and scan for an image repo.
   The secret scan diffs against a base commit this job fetches (`diff_base`).
   Then Gourmand (`review`), which `sdlc ci` leaves out and this job runs beside it.
3. Scan for code scanning on a default-branch push and the weekly run: `sdlc sarif`
   reruns CodeQL and semgrep with an output directory mounted, and this job uploads
   what they wrote (see `scan`).
4. The weekly run also audits dependencies and keeps the schedule alive.

One build per release: the plan says `image` on the release merge, and on the weekly
or parent rebuild only when a release tagged the commit being rebuilt, so unreleased
code is never published. A pull request runs the container gates only when it changes
a Containerfile or a lockfile (IMAGE_INPUTS); its other changes wait for the release
build. A default-branch push whose tree is the tree its merged pull request passed
`check` on runs no gates: they already judged these exact bytes.

An exit that is not a verdict (the launcher's 2 or 3, an unreadable digest, an API
that refused) is a 2. A 3 is "CI will judge it", and here we are CI.
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from sdlc_jobs import pages
from sdlc_jobs._core import (
    BOOT_JOB,
    CANNOT_RUN,
    CLEAN,
    FOUND,
    SEMVER,
    SOCKET,
    Image,
    Tools,
    api,
    build_arg_map,
    call,
    cannot,
    fetch_tree,
    final_stage,
    host_mount,
    image_settings,
    images,
    must,
    pr_build_paths,
    set_output,
    step,
    tool_sdlc,
    workspace,
)
from sdlc_jobs._prs import CHECK_JOB as REQUIRED_CHECK
from sdlc_jobs._prs import merged_head, pr_job, tree_of
from sdlc_jobs._tools import git_env, safe_directory

SDLC = "sdlc"  # this image's own in-image half, found on PATH
ACCEPTED = 202
FORBIDDEN = 403
WEEKLY = "17 6 * * 1"
POLL = "47 */6 * * *"
# The events ci.yml's `check` job runs on (its `if:`); anything else is not this job's.
EVENTS = ("pull_request", "push", "schedule", "workflow_dispatch")
# A job's outputs are capped at 1 MiB; the plan stays well under it.
PLAN_LIMIT = 900_000
RELEASE_REF = "ashigaru/release-"
CONTAINER_GATES = ("container-build", "container-smoke", "container-scan", "bootc-lint")
# The push verdict `sdlc ci` leaves out (its CI_EXCLUDES) that this job renders itself.
# Gourmand reads the tree and needs no secret and no model, so after the cutover deleted
# every repo's gourmand.yml this is where it runs (#177). Gatehouse, the other
# exclusion, is `ai` and `gate`'s.
REVIEW = "gourmand"
# A pull request that changes one of these builds the image as a test (never pushed).
IMAGE_INPUTS = re.compile(
    r"(^|/)(Containerfile|Dockerfile)(\.[^/]*)?$|(^|/)\.(container|docker)ignore$"
    r"|(^|/)(uv\.lock|poetry\.lock|Pipfile\.lock|package-lock\.json|Cargo\.lock|go\.sum)$"
    r"|(^|/)requirements[^/]*\.txt$"
)
PR_FILES_PAGES = 30  # GitHub lists at most 3,000 files of a pull request
PER_PAGE = 100
OK = 200
# The line `sdlc pip-audit -- --report` prints its JSON report on; the same in `sdlc`.
AUDIT_REPORT = "SDLC-AUDIT-REPORT "
BASE_LABEL = "org.opencontainers.image.base.digest"  # what the image job stamps on a build
# What `sdlc sarif` leaves in its output directory, as code-scanning category -> file.
# check -> (code-scanning category, the file `sdlc sarif` writes for it)
SARIF_FILES = {
    "codeql": ("/language:python", "codeql-results.sarif"),
    "semgrep": ("semgrep", "semgrep.sarif"),
}
SARIF_PLANNED = "planned"  # the planner's list of the checks the scan ran
# The one refusal that is not a failure: code scanning is not offered here at all
# (a private repo without Advanced Security). Anything else refused is.
NO_CODE_SCANNING = re.compile(
    r"(Advanced|Code) Security must be enabled|code scanning is not enabled", re.IGNORECASE
)
SARIF_DIR = "sdlc-sarif"  # under RUNNER_TEMP, which the runner's engine can mount
SARIF_LIMIT = 10_000_000  # bytes, gzipped: GitHub refuses a larger upload
# Where each event names the commit `secret-scan` diffs against (`diff_base`).
BASE_OF: dict[str, Callable[[dict], str | None]] = {
    "pull_request": lambda e: ((e.get("pull_request") or {}).get("base") or {}).get("sha"),
    "push": lambda e: e.get("before"),
}


def run(event_name: str, event: dict, env: Mapping[str, str], tools: Tools) -> int:
    """Gate this commit and write the plan; the gates' verdict is the exit code.

    `event_name` and `event` are the workflow's: a push to the default branch
    (named in `event`) may plan an image and a release, a schedule's cron the
    weekly audit or the parent poll. `env` is the job's environment
    (GITHUB_*, GITHUB_OUTPUT for the plan). `tools` runs podman, skopeo and the
    GitHub API (a fake in the suites). 0 clean, 1 a gate found something, and
    CannotRunError, exit 2, when a gate or the plan could not be made.
    """
    if event_name not in EVENTS:
        cannot(f"check runs on {', '.join(EVENTS)}, not {event_name!r}")
    root = workspace(env)
    repo = env.get("GITHUB_REPOSITORY", "")
    fetch_tree(tools, env, root, repo)
    default_push = event_name == "push" and env.get("GITHUB_REF") == (
        f"refs/heads/{(event.get('repository') or {}).get('default_branch', '')}"
    )
    cron = event.get("schedule") if event_name == "schedule" else None
    bare = (
        event_name == "workflow_dispatch"
        and env.get("GITHUB_REF")
        == f"refs/heads/{(event.get('repository') or {}).get('default_branch', '')}"
        and not str((event.get("inputs") or {}).get("version") or "").strip()
    )
    made = make_plan(tools, env, root, default_push, cron)
    made["pages"] = pages_plan(tools, env, root, now=default_push or bare, cron=cron)
    set_output(env, "result", fit(made))

    if event_name == "schedule":
        # The weekly run rescans what is already on the default branch: new queries
        # and new semgrep rules find old code, which no pull request will revisit.
        return scan(tools, env, root, repo, strict=True) if made["weekly"] else CLEAN
    if default_push and gated_on_pr(tools, env, repo):
        # The pull request's gates judged this tree; the scan only keeps the Security
        # tab current, so nothing it says, a 2 included, holds the release (see `rescan`).
        rescan(tools, env, root, repo)
        return CLEAN
    skip: tuple[str, ...] = ()
    if event_name == "pull_request":
        extra = pr_build_paths(image_settings(root, repo))
        if not touches_image(tools, env, repo, event, extra):
            skip = CONTAINER_GATES
    base = diff_base(tools, env, root, event_name, event)
    rc = max(gates(tools, env, root, skip=skip, base=base), review(tools, env, root))
    if default_push:
        rescan(tools, env, root, repo)
    return rc


def rescan(tools: Tools, env: Mapping[str, str], root: Path, repo: str) -> None:
    """`scan` on a default-branch push, where it is a report and not a gate.

    CodeQL and semgrep already judged this tree, in `sdlc ci` here or on its pull
    request. A scan that could not run, or that left a planned check's SARIF out, is
    an error annotation on the run, never the job's 2: that would hold `image` and
    `promote` for a report about code that has passed. A refused upload is a warning.
    The weekly run is where the scan is the verdict, and its 2 stands.
    """
    if scan(tools, env, root, repo, strict=False) == CANNOT_RUN:
        print(
            "::error::the code-scanning scan did not complete; the Security tab is not "
            "current for this commit, and the gates are unaffected",
            flush=True,
        )


def make_plan(
    tools: Tools,
    env: Mapping[str, str],
    root: Path,
    default_push: bool,
    cron: object,
) -> dict[str, Any]:
    """The plan for this event, in the shape `_core.plan` documents; `run` adds `pages`."""
    repo = env.get("GITHUB_REPOSITORY", "")
    out: dict[str, Any] = {
        "image": False,
        "boot": False,
        "release": None,
        "write": False,
        "weekly": cron == WEEKLY,
        "audit": None,
        "assets": False,
        "pages": False,
    }
    own = images(root, repo)
    has_image = bool(own)
    out["boot"] = any(img.settings.get(key) for img in own for key in BOOT_JOB)
    if default_push:
        out["release"] = release_plan(tools, env, root, repo)
        out["image"] = has_image and out["release"] is not None
    elif out["weekly"]:
        out["image"] = has_image and released_here(tools, env, root)
        step("weekly: audit and keepalive")
        out["audit"] = audit(tools, env, root)
        api(tools, env, "PUT", f"/repos/{repo}/actions/workflows/ci.yml/enable")
    elif cron == POLL and has_image:
        out["image"] = released_here(tools, env, root) and parent_moved(tools, root, repo)
    out["assets"] = bool(out["release"]) and "assets" in tool_sdlc(root)
    # A clean report goes to `write` too: it is what closes the alert issue.
    out["write"] = bool(out["release"]) or out["audit"] is not None
    return out


def pages_plan(
    tools: Tools, env: Mapping[str, str], root: Path, *, now: bool, cron: object
) -> bool:
    """Does `pages` run? Only where the tree declares a site: on a default-branch push
    or a bare dispatch, and on the 6-hourly and weekly polls when the products'
    releases differ from what the site serves."""
    if "pages" not in tool_sdlc(root):
        return False
    if now:
        return True
    if cron not in (WEEKLY, POLL):
        return False
    step("poll: the package site against its products' releases")
    return pages.changed(tools, env, root)


def released_here(tools: Tools, env: Mapping[str, str], root: Path) -> bool:
    """Does a release tag (vX.Y.Z) name this very commit? A rebuild publishes only a release."""
    sha = env.get("GITHUB_SHA", "")
    genv = {**env, **git_env(root, env.get("GITHUB_TOKEN", ""), env)}
    out = must(
        tools, ["git", "ls-remote", "--tags", "origin", "refs/tags/v*"], cwd=str(root), env=genv
    )
    for line in out.splitlines():
        commit, _, ref = line.partition("\t")
        name = ref.removeprefix("refs/tags/").removesuffix("^{}")
        if commit == sha and SEMVER.fullmatch(name.removeprefix("v")):
            return True
    print("rebuild: no release tags this commit; the next release takes the new parent")
    return False


def touches_image(
    tools: Tools,
    env: Mapping[str, str],
    repo: str,
    event: dict,
    extra: Sequence[re.Pattern[str]] = (),
) -> bool:
    """Does this pull request change a Containerfile or a lockfile (IMAGE_INPUTS), or a
    path the repo's [tool.sdlc.image] pr-build-paths names (`extra`)?

    Unreadable, or more files than GitHub lists, is "yes": the gates run.
    """
    number = (event.get("pull_request") or {}).get("number")
    if not isinstance(number, int):
        return True
    for page in range(1, PR_FILES_PAGES + 1):
        path = f"/repos/{repo}/pulls/{number}/files?per_page={PER_PAGE}&page={page}"
        resp = call(tools, env, "GET", path)
        files = resp.json() if resp.status == OK else None
        if not isinstance(files, list):
            return True
        names = [str(f.get("filename", "")) for f in files] + [
            str(f.get("previous_filename", "")) for f in files if f.get("previous_filename")
        ]
        if any(IMAGE_INPUTS.search(n) or any(p.match(n) for p in extra) for n in names):
            print("gates: this pull request changes the image's inputs: test-building it")
            return True
        if len(files) < PER_PAGE:
            print("gates: no image input changed: the image builds at release")
            return False
    return True


def fit(plan: dict[str, Any]) -> str:
    """The plan as JSON, small enough for a job output.

    A big audit report is cut to its vulnerable dependencies, the only ones
    `audit_alert` reads (name, version, vulns); a plan still too big, with or
    without an audit, is a 2.
    """
    text = json.dumps(plan)
    audit_report = plan["audit"]
    if len(text.encode()) <= PLAN_LIMIT:
        return text
    if audit_report is None:
        cannot(f"the plan is {len(text.encode())} bytes, over GitHub's job-output limit")
    deps = audit_report["dependencies"]
    plan["audit"] = {**audit_report, "dependencies": [d for d in deps if d.get("vulns")]}
    text = json.dumps(plan)
    if len(text.encode()) > PLAN_LIMIT:
        cannot(f"the plan is {len(text.encode())} bytes even with only the vulnerable dependencies")
    return text


def containerfile(root: Path) -> str | None:
    """The root Containerfile or Dockerfile, Containerfile first."""
    return next((f for f in ("Containerfile", "Dockerfile") if (root / f).is_file()), None)


def launch(
    tools: Tools,
    env: Mapping[str, str],
    root: Path,
    command: str,
    *args: str,
    stream: bool = False,
) -> subprocess.CompletedProcess[str]:
    """The launcher's `command` for this tree, judged by this very image.

    `sdlc launcher <ref>` prints the launcher pinned to the image this job runs,
    as bin/sdlc-bootstrap does on a laptop. It drives the runner's engine through
    the socket, so it mounts the workspace by its host path.
    """
    host, image = host_mount(tools, env, root)
    if "@sha256:" not in image:
        cannot(f"this container's image is not pinned by digest ({image}): no launcher for it")
    script = Path(tempfile.mkdtemp(prefix="sdlc-launcher-")) / "sdlc"
    script.write_text(must(tools, [SDLC, "launcher", image]))
    script.chmod(0o700)
    # The launcher runs git in the mounted workspace (rev-parse, diff): #157.
    child = {
        **env,
        **safe_directory(env, root),
        "SDLC_HOST_REPO": host,
        "CONTAINER_HOST": env.get("CONTAINER_HOST") or SOCKET,
    }
    # The launcher's cache is mounted into nested containers, so it lives on a
    # path the runner's engine can see: the runner's temp, by its host path.
    if env.get("RUNNER_TEMP"):
        cache = Path(env["RUNNER_TEMP"]) / "sdlc-cache"
        cache.mkdir(parents=True, exist_ok=True)
        child["XDG_CACHE_HOME"] = str(cache)
        child["SDLC_HOST_CACHE"] = host_mount(tools, env, cache)[0]
    argv = [str(script), command, *(["--", *args] if args else [])]
    return tools.run(argv, cwd=str(root), env=child, stream=stream)


def diff_base(
    tools: Tools, env: Mapping[str, str], root: Path, event_name: str, event: dict
) -> str | None:
    """The commit `secret-scan` diffs against, fetched into the workspace; None for none.

    The tree is at depth 1, so there is no base to find: the pull request's base, or
    what a push moved from. A new branch has no `before`, so it diffs from the default
    branch's tip. The sha comes from the payload, so it is checked before git sees it.
    """
    pick = BASE_OF.get(event_name)
    if pick is None:
        return None
    sha = pick(event) or ""
    ref = sha
    if not sha or set(sha) == {"0"}:
        branch = (event.get("repository") or {}).get("default_branch") or ""
        if not branch:
            cannot("a push with no `before` and no default branch has no base to diff")
        ref = branch
    elif not re.fullmatch(r"[0-9a-f]{40}", sha):
        cannot(f"the event's base {sha!r} is not a commit sha")
    genv = {**env, **git_env(root, env.get("GITHUB_TOKEN", ""), env)}
    must(
        tools,
        ["git", "fetch", "-q", "--no-tags", "--depth=1", "origin", ref],
        cwd=str(root),
        env=genv,
    )
    if ref == sha:
        return sha
    tip = must(tools, ["git", "rev-parse", "FETCH_HEAD"], cwd=str(root), env=genv).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", tip):
        cannot(f"the default branch's tip {tip!r} is not a commit sha")
    return tip


def gates(
    tools: Tools,
    env: Mapping[str, str],
    root: Path,
    skip: tuple[str, ...] = (),
    base: str | None = None,
) -> int:
    """`sdlc ci` for this tree, through the launcher so repo code runs only nested.

    `skip` is the container gates when `image` runs them on this pull request instead.
    `base` is what `secret-scan` diffs against, as SDLC_DIFF_BASE.
    """
    step("gates: sdlc ci")
    if base:
        env = {**env, "SDLC_DIFF_BASE": base}
    if skip:
        print(f"the container gates run in `image`, on the release build: {', '.join(skip)}")
        given = [s for s in env.get("SDLC_SKIP", "").split(",") if s.strip()]
        env = {**env, "SDLC_SKIP": ",".join([*given, *skip])}
    proc = launch(tools, env, root, "ci", stream=True)
    return proc.returncode if proc.returncode in (CLEAN, FOUND) else CANNOT_RUN


def review(tools: Tools, env: Mapping[str, str], root: Path) -> int:
    """Gourmand for this tree, through the launcher as the gates are; its verdict.

    Run unless the repo skips it in [tool.sdlc.push], the one tier that names it, so
    a repo that opted out of it locally is not judged by it here either.
    """
    step(f"review: sdlc {REVIEW}")
    skip = (tool_sdlc(root).get("push") or {}).get("skip") or []
    if isinstance(skip, list) and REVIEW in skip:
        print(f"review: {REVIEW} is skipped in [tool.sdlc.push]; not run", flush=True)
        return CLEAN
    proc = launch(tools, env, root, REVIEW, stream=True)
    return proc.returncode if proc.returncode in (CLEAN, FOUND) else CANNOT_RUN


def gated_on_pr(tools: Tools, env: Mapping[str, str], repo: str) -> bool:
    """Did the pull request this push merges pass `check` on this very tree?

    Its head's tree must be this commit's tree, so the gates judged these exact files,
    and the pass must be a job GitHub ran from ci.yml (sdlc_jobs/_prs.pr_job), not
    a check run anything could post. Anything less and the push runs the gates itself.
    Main's code scanning is then the pull request's: no SARIF is uploaded from here.
    """
    sha = env.get("GITHUB_SHA", "")
    head = merged_head(tools, env, repo, sha)
    if head is None:
        return False
    if tree_of(tools, env, repo, head) != tree_of(tools, env, repo, sha):
        print("the merged pull request was checked on another tree: gating")
        return False
    if pr_job(tools, env, repo, head, REQUIRED_CHECK) is None:
        return False
    print(f"gates: passed on the pull request at {head}, the same tree; not rerun")
    return True


def release_plan(tools: Tools, env: Mapping[str, str], root: Path, repo: str) -> dict | None:
    """The release this push merges, as a plan: `write` verifies it again from the API."""
    prs = api(tools, env, "GET", f"/repos/{repo}/commits/{env.get('GITHUB_SHA', '')}/pulls")
    if not isinstance(prs, list) or len(prs) != 1 or not prs[0].get("merged_at"):
        return None
    pr = prs[0]
    head = pr.get("head") or {}
    version = str(head.get("ref", "")).removeprefix(RELEASE_REF)
    ok = (
        str(head.get("ref", "")).startswith(RELEASE_REF)
        and SEMVER.fullmatch(version)
        and (head.get("repo") or {}).get("full_name") == repo
        and str((pr.get("user") or {}).get("login", "")).endswith("[bot]")
    )
    if not ok:
        return None
    publish = "pypi" if tool_sdlc(root).get("publish") == "pypi" else ""
    return {"version": version, "publish": publish}


def audit(tools: Tools, env: Mapping[str, str], root: Path) -> dict | None:
    """The weekly dependency audit: pip-audit's JSON report, from the image's own gate.

    `write` hands it to sdlc's `_audit_alert`, which opens, rewrites or
    closes the one alert issue, so a clean report is handed on as well. None when
    there is no Python project to audit.
    """
    if not (root / "pyproject.toml").is_file():
        return None
    proc = launch(tools, env, root, "pip-audit", "--report")
    if proc.returncode not in (CLEAN, FOUND):
        cannot(f"pip-audit could not run (exit {proc.returncode})")
    lines = [line for line in proc.stdout.splitlines() if line.startswith(AUDIT_REPORT)]
    if not lines:
        cannot("pip-audit printed no report")
    try:
        report = json.loads(lines[-1].removeprefix(AUDIT_REPORT))
    except ValueError as exc:
        cannot(f"pip-audit's report is not JSON: {exc}")
    if not isinstance(report, dict) or not isinstance(report.get("dependencies"), list):
        cannot("pip-audit's report has no dependencies list")
    return report


def final_base(text: str, overrides: Mapping[str, str] | None = None) -> str | None:
    """The image the last stage of a Containerfile is built on; None for scratch or unknown."""
    found = final_stage(text, strict=False, overrides=overrides)
    if found is None or not found[1] or found[1].lower() == "scratch":
        return None
    return found[1]


def parent_moved(tools: Tools, root: Path, repo: str) -> bool:
    """Has any image's final FROM moved since its published image was built on it?

    One moved rebuilds them all: the image job builds every image, siblings in order,
    so a moved base cascades into what is built FROM it. Unreadable on either side is
    a 2: a poll that guessed "unchanged" would never rebuild.
    """
    return any(moved(tools, root, img) for img in images(root, repo))


def moved(tools: Tools, root: Path, img: Image) -> bool:
    name = img.containerfile or containerfile(root)
    if name is None:
        return False
    text = (root / name).read_text(encoding="utf-8", errors="replace")
    base = final_base(text, build_arg_map(img.settings))
    if base is None:
        return False
    step(f"poll: {img.name} on {base}")
    now = must(tools, ["skopeo", "inspect", "--format", "{{.Digest}}", f"docker://{base}"]).strip()
    published = f"{img.ref}:latest"
    raw = must(tools, ["skopeo", "inspect", f"docker://{published}"])
    try:
        built_on = json.loads(raw).get("Labels", {}).get(BASE_LABEL)
    except (ValueError, AttributeError) as exc:
        cannot(f"{published} did not inspect as JSON: {exc}")
    if not built_on:
        cannot(f"{published} has no {BASE_LABEL} label; cannot tell whether the parent moved")
    return str(built_on) != now


def scan(tools: Tools, env: Mapping[str, str], root: Path, repo: str, *, strict: bool) -> int:
    """`sdlc sarif` for this tree, then its SARIF to code scanning; the scan's verdict.

    Only `sdlc sarif` gets a writable directory the upload reads. It runs CodeQL and
    semgrep, which read the tree and execute none of it; `sdlc ci` runs the repo's
    own code (pytest, conftest, a Containerfile) and is never given one, so nothing
    the repo runs can write what is uploaded. The directory is emptied first, so a
    file left by an earlier run on a reused runner is not sent as this commit's.
    The token stays in this job: the launcher passes nested containers no token.

    `strict` is the weekly run, where the scan IS the verdict: a planned check with
    no SARIF, or SARIF that did not reach code scanning, is a 2. Off it, only the
    missing SARIF is (an error annotation, from `rescan`); a refusal only warns.
    """
    step("scan: sdlc sarif")
    out = Path(env.get("RUNNER_TEMP") or tempfile.gettempdir()) / SARIF_DIR
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    host = host_mount(tools, env, out)[0]
    proc = launch(tools, {**env, "SDLC_HOST_SARIF": host}, root, "sarif", stream=True)
    rc = proc.returncode if proc.returncode in (CLEAN, FOUND) else CANNOT_RUN
    if rc != CLEAN:
        print(f"scan: sdlc sarif exited {proc.returncode}", flush=True)
    # A scan that could not run says why itself; one that did must account for
    # every check it planned.
    missing = expected_missing(out) if rc != CANNOT_RUN else False
    refused = upload_sarif(tools, env, repo, out, strict=strict)
    return CANNOT_RUN if missing or (strict and refused) else rc


def expected_missing(out: Path) -> bool:
    """Did a check the planner listed leave no SARIF? Each one is an error annotation."""
    try:
        planned = (out / SARIF_PLANNED).read_text(encoding="utf-8").split()
    except OSError:
        print("::error::sdlc sarif wrote no list of what it planned", flush=True)
        return True
    missing = [c for c in planned if c in SARIF_FILES and not (out / SARIF_FILES[c][1]).is_file()]
    for c in missing:
        print(f"::error::{c} ran under sdlc sarif and wrote no {SARIF_FILES[c][1]}", flush=True)
    return bool(missing)


def upload_sarif(
    tools: Tools, env: Mapping[str, str], repo: str, root: Path, *, strict: bool
) -> bool:
    """Send each SARIF the scan left to the Security tab, under its own category.

    True when any did not get there: unreadable, too big, or refused. `strict`
    makes each an error annotation, a warning otherwise. A repo where code scanning
    is not offered at all (a private one on GitHub Free answers 403 and says so)
    is not a failure in either: nothing it could fix would get the upload in.
    """
    level = "error" if strict else "warning"
    failed = False
    for category, fname in SARIF_FILES.values():
        path = root / fname
        if not path.is_file():
            print(f"no {fname}: nothing to upload for {category}", flush=True)
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"::{level}::{fname} is not readable SARIF, not uploaded: {exc}", flush=True)
            failed = True
            continue
        runs = doc.get("runs") if isinstance(doc, dict) else None
        if not isinstance(runs, list) or not all(isinstance(r, dict) for r in runs):
            print(f"::{level}::{fname} has no SARIF runs, not uploaded", flush=True)
            failed = True
            continue
        for r in runs:
            r.setdefault("automationDetails", {})["id"] = f"{category}/"
        packed = gzip.compress(json.dumps(doc).encode())
        if len(packed) > SARIF_LIMIT:
            print(
                f"::{level}::{fname} is {len(packed)} bytes gzipped, over GitHub's limit",
                flush=True,
            )
            failed = True
            continue
        body = {
            "commit_sha": env.get("GITHUB_SHA", ""),
            "ref": env.get("GITHUB_REF", ""),
            "sarif": base64.b64encode(packed).decode(),
        }
        resp = call(tools, env, "POST", f"/repos/{repo}/code-scanning/sarifs", body)
        if resp.status in (200, 201, ACCEPTED):
            print(f"uploaded {fname} as {category}", flush=True)
            continue
        said = resp.body.decode(errors="replace")
        if resp.status == FORBIDDEN and NO_CODE_SCANNING.search(said):
            print(f"::warning::code scanning is not offered here; {fname} not uploaded", flush=True)
            continue
        print(
            f"::{level}::uploading {fname} answered {resp.status}: {resp.body[:300]!r}", flush=True
        )
        failed = True
    return failed
