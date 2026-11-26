#!/usr/bin/env python3
"""scripts/local-release.sh, run end to end against faked host tools.

The script runs unprivileged under SDLC_LOCAL_RELEASE_ROOT, from a committed
copy of the files it reads, with tests/local_release_stub.py first on PATH as podman,
curl, skopeo and the rest. Each job's launch records the names and values it
was handed, so these assert what ran, in what order, holding what:

  - the jobs run check, image, boot, write, promote, pypi: `write` tags vX.Y.Z and
    publishes the GitHub Release only after the image is built and gated and
    its boot proof passed, so a release never exists without an image (a1667)
  - `write` is handed SDLC_IMAGE and SDLC_BOOT, so its own refusal holds here
  - the App key reaches `write` alone; the PyPI token reaches `pypi` alone, as
    UV_PUBLISH_TOKEN, and pypi runs only when the plan publishes to PyPI (a1738)
  - a PyPI release with no token stops before image; a PyPI failure after
    promote says the release is out and how to finish it
  - --pypi runs the pypi job and nothing else, for the one vX.Y.Z tag at the
    commit, read across every page of tags
  - the podman service holds none of the secrets: a build secret whose id names
    a variable in the service's environment reaches its RUN empty (a1679)

Run: python3 tests/test_local_release.py   (needs sh, git and jq)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUB = ROOT / "tests" / "local_release_stub.py"
# What the script reads from its own checkout.
COPIED = [
    "scripts/local-release.sh",
    "bin/sdlc-bootstrap",
    "examples/ci.yml",
    ".github/workflows/ci.yml",
]
TOOLS = ["id", "flock", "unshare", "skopeo", "podman", "curl", "openssl"]
REPO = "crunchtools/mcp-x"
SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER = "f" * 40
KEY = "pypi-FAKE-a1738-token"
SECRETS = {
    "COSIGN_PRIVATE_KEY": "cosign-key-FAKE",
    "COSIGN_PASSWORD": "cosign-pass-FAKE",
    "QUAY_USERNAME": "quay-user-FAKE",
    "QUAY_PASSWORD": "quay-pass-FAKE",
    "ASHIGARU_APP_KEY": "app-key-FAKE",
    "RHSM_ACTIVATION_KEY": "rhsm-FAKE",
}
PYPI_PLAN = {"release": {"publish": "pypi"}, "boot": True}
RELEASE = {"version": "1.2.3", "sha": SHA}
ALL = ["check", "image", "boot", "write", "promote", "pypi"]


@dataclass
class Run:
    rc: int
    stderr: str
    log: list[dict] = field(default_factory=list)

    @property
    def jobs(self) -> list[dict]:
        return [e for e in self.log if "job" in e]

    @property
    def order(self) -> list[str]:
        return [e["job"] for e in self.jobs]

    def job(self, name: str) -> dict:
        found = [e for e in self.jobs if e["job"] == name]
        assert len(found) == 1, f"{name} ran {len(found)} times: {self.order}"
        return found[0]

    def holders(self, value: str) -> list[str]:
        """The jobs whose container was handed `value`."""
        return [e["job"] for e in self.jobs if value in e["values"].values()]


def release(
    *,
    plan: dict | None = None,
    token: bool = True,
    tags: list[dict] | None = None,
    fail: list[str] | None = None,
    pypi_only: bool = False,
) -> Run:
    if shutil.which("jq") is None or shutil.which("git") is None:
        raise AssertionError("the suite needs jq and git")
    with tempfile.TemporaryDirectory(prefix="lr", dir="/tmp") as tmp:
        base = Path(tmp)
        here = base / "sdlc"
        for rel in COPIED:
            (here / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, here / rel)
        git = ["git", "-C", str(here), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "copy"], check=True)
        stubs = base / "bin"
        stubs.mkdir()
        for tool in TOOLS:
            (stubs / tool).symlink_to(STUB)
        state = base / "state"
        state.mkdir()
        (state / "scenario.json").write_text(
            json.dumps(
                {
                    "plan": PYPI_PLAN if plan is None else plan,
                    "release": RELEASE,
                    "tags": tags or [],
                    "fail": fail or [],
                }
            )
        )
        (base / "root" / "var" / "mnt" / "onsite").mkdir(parents=True)
        secrets = dict(SECRETS, **({"PYPI_API_TOKEN": KEY} if token else {}))
        argv = ["sh", str(here / "scripts" / "local-release.sh")]
        argv += ["--pypi"] if pypi_only else []
        env = {
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "HOME": str(base),
            "SDLC_LOCAL_RELEASE_ROOT": str(base / "root"),
        }
        proc = subprocess.run(
            [*argv, REPO, SHA],
            input=json.dumps(secrets),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        logfile = state / "log.jsonl"
        lines = logfile.read_text().splitlines() if logfile.exists() else []
        return Run(proc.returncode, proc.stderr, [json.loads(x) for x in lines])


def tag(name: str, sha: str) -> dict:
    return {"name": name, "commit": {"sha": sha}}


def test_a_pypi_release_runs_every_job_in_order() -> None:
    run = release()
    assert run.rc == 0, run.stderr
    assert run.order == ALL


def test_write_is_handed_image_and_boot() -> None:
    run = release()
    values = run.job("write")["values"]
    assert values["SDLC_IMAGE"] == "success" and values["SDLC_BOOT"] == "success"


def test_the_app_key_reaches_write_alone() -> None:
    assert release().holders(SECRETS["ASHIGARU_APP_KEY"]) == ["write"]


def test_the_pypi_token_reaches_pypi_alone() -> None:
    run = release()
    assert run.holders(KEY) == ["pypi"]
    pypi = run.job("pypi")
    assert pypi["names"] == ["GITHUB_TOKEN", "SDLC_RELEASE", "SDLC_WRITE", "UV_PUBLISH_TOKEN"]
    assert pypi["values"]["UV_PUBLISH_TOKEN"] == KEY
    assert json.loads(pypi["values"]["SDLC_RELEASE"]) == RELEASE
    # Nor is it in any other job's podman client: exported for pypi alone.
    clients = [e["job"] for e in run.jobs if KEY in e["client"].values()]
    assert clients == ["pypi"], clients


def test_no_pypi_when_the_plan_does_not_publish() -> None:
    run = release(plan={"release": {}, "boot": True})
    assert run.rc == 0, run.stderr
    assert run.order == ["check", "image", "boot", "write", "promote"]


def test_no_write_and_no_pypi_without_a_release() -> None:
    run = release(plan={})
    assert run.rc == 0, run.stderr
    assert run.order == ["check", "image", "promote"]


def test_a_pypi_release_with_no_token_stops_before_image() -> None:
    run = release(token=False)
    assert run.rc != 0
    assert run.order == ["check"]
    assert "PYPI_API_TOKEN is missing" in run.stderr


def test_a_pypi_failure_after_promote_says_how_to_finish() -> None:
    run = release(fail=["pypi"])
    assert run.rc != 0
    assert run.order == ALL
    assert "is tagged and its image promoted" in run.stderr
    assert f"--pypi {REPO} {SHA}" in run.stderr


def test_a_failure_before_pypi_offers_no_retry() -> None:
    run = release(fail=["image"])
    assert run.rc != 0 and run.order == ["check", "image"]
    assert "--pypi" not in run.stderr


def test_the_service_holds_no_secret() -> None:
    run = release()
    service = [e["service"] for e in run.log if "service" in e]
    assert len(service) == 1
    held = sorted(k for k, v in service[0].items() if v in {*SECRETS.values(), KEY, "ghs_fake"})
    assert held == [], held


def test_pypi_only_publishes_the_tagged_release() -> None:
    # The tag on the second page, beside a non-release tag at the same commit
    # and a release tag of another.
    tags = [tag(f"v0.0.{n}", OTHER) for n in range(100)]
    tags += [tag("latest", SHA), tag("v1.2.3", SHA), tag("v1.2.2", OTHER)]
    run = release(pypi_only=True, tags=tags)
    assert run.rc == 0, run.stderr
    assert run.order == ["pypi"]
    pypi = run.job("pypi")
    assert json.loads(pypi["values"]["SDLC_RELEASE"]) == RELEASE
    assert pypi["values"]["SDLC_WRITE"] == "success"
    assert run.holders(KEY) == ["pypi"]
    pages = [e["api"].rpartition("page=")[2] for e in run.log if "/tags" in e.get("api", "")]
    assert pages == ["1", "2", "3"], pages  # two with tags, then the empty one


def test_pypi_only_refuses_two_release_tags() -> None:
    run = release(pypi_only=True, tags=[tag("v1.2.3", SHA), tag("v1.2.4", SHA)])
    assert run.rc != 0 and run.order == []
    assert "exactly one vX.Y.Z tag" in run.stderr


def test_pypi_only_refuses_a_commit_no_release_tag_names() -> None:
    for tags in ([tag("v1.2.3", OTHER)], [tag("v1.2", SHA), tag("latest", SHA)], []):
        run = release(pypi_only=True, tags=tags)
        assert run.rc != 0 and run.order == [], tags
        assert "exactly one vX.Y.Z tag" in run.stderr, tags


def test_pypi_only_without_a_token_stops_first() -> None:
    run = release(pypi_only=True, token=False, tags=[tag("v1.2.3", SHA)])
    assert run.rc != 0
    assert run.order == [] and not [e for e in run.log if "api" in e]
    assert "PYPI_API_TOKEN is missing" in run.stderr


def test_the_secrets_script_emits_the_pypi_token() -> None:
    text = (ROOT / "scripts" / "local-release-secrets.sh").read_text()
    emitted = text[text.index("{COSIGN_PRIVATE_KEY") :]
    assert "PYPI_API_TOKEN}" in emitted[: emitted.index("}") + 1]


def main() -> int:
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                failed += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            else:
                print(f"ok   {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
