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
  - `write` outside the triage events, `image` and `pypi` act on `check`'s
    plan only when `check` succeeded: `needs.check.result == 'success'` in the
    `if:`, or an `if:` with no status function, where GitHub's implicit
    success() requires it
  - `image`'s env maps REGISTRY_REDHAT_IO_USER and REGISTRY_REDHAT_IO_PASSWORD
  - in sdlc's own file, `test` is the only job whose `if:` admits
    pull_request, with `permissions: { contents: read }`, no `environment` and
    no `container`; and sdlc's `check` does not admit pull_request
  - `ai`, `write` and `gate` are byte for byte the same in both files, an
    anchor's definition (`&boot {...}`) read as its alias (`*boot`): sdlc's
    file defines them in a different first job. Parsed, they are equal too
  - and of the fleet file: no `uses:` at all, top-level `permissions: {}`,
    every job that holds a secret has an environment and runs on
    ubuntu-latest, and `check` and `gate` hold none

sdlc's own ci.yml and `sdlc event` land in their own pull requests; until
each exists, the checks that read it are reported as skipped, not passed.

Run inside the image, which has the interpreter and PyYAML the script needs:
  podman run --rm -v .:/src:ro,z -w /src --entrypoint /opt/constitution-venv/bin/python3 \
      quay.io/crunchtools/sdlc:<tag> tests/test_workflows.py [SDLC_CI_YML]
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FLEET = ROOT / "examples" / "ci.yml"
OWN = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".github" / "workflows" / "ci.yml"
FAILED: list[str] = []
SKIPPED: list[str] = []

CHECK_OK = "needs.check.result == 'success'"
# A status function in an `if:` replaces GitHub's implicit success(), which
# is what otherwise skips a job whose needs did not all succeed.
STATUS = re.compile(r"\b(?:always|cancelled|failure|success)\(\)")
# The event literally, not pull_request_target or pull_request_review_comment.
PULL_REQUEST = re.compile(r"""['"]pull_request['"]""")
SECRET = re.compile(r"\$\{\{\s*secrets\.")
SHARED = ("ai", "write", "gate")
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


def acts_only_on_success(cond: str) -> bool:
    return CHECK_OK in cond or not STATUS.search(cond)


# ---------------------------------------------------------- the fleet file

fleet_text = FLEET.read_text()
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
        f"fleet: {job_id} acts on check's plan only when check succeeded",
        acts_only_on_success(cond),
        cond,
    )
    needs = fleet[job_id]["needs"]
    needs = needs if isinstance(needs, list) else [needs]
    check(f"fleet: {job_id} needs check", "check" in needs)
check(
    "fleet: pypi's if: names needs.check.result == 'success' itself (it runs under always())",
    CHECK_OK in str(fleet["pypi"]["if"]),
)

image_env = fleet["image"]["env"]
for name in ("REGISTRY_REDHAT_IO_USER", "REGISTRY_REDHAT_IO_PASSWORD"):
    check(f"fleet: image's env maps {name}", image_env.get(name) == f"${{{{ secrets.{name} }}}}")

check("fleet: no `uses:` anywhere", not re.search(r"^\s*-?\s*uses:", fleet_text, re.M))
check("fleet: top-level permissions are {}", parsed.get("permissions") == {})
for job_id, job in fleet.items():
    holds = bool(SECRET.search(yaml.safe_dump(job)))
    if holds:
        check(f"fleet: {job_id} holds secrets only in an environment", bool(job.get("environment")))
        on_hosted = job.get("runs-on") == "ubuntu-latest"
        check(f"fleet: {job_id} holds secrets on ubuntu-latest", on_hosted)
for job_id in ("check", "gate"):
    check(f"fleet: {job_id} holds no secret", not SECRET.search(yaml.safe_dump(fleet[job_id])))
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
