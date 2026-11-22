#!/usr/bin/env python3
"""scripts/cutover.py, over small repos built here in the shapes the fleet has.

The cutover is what one consumer repo needs beyond convert_repo.py, so each
change is pinned on its own fixture:

  - the constitution pins, `Inherits:` and the hook `rev:` move together, and
    a `@v1` major-tag call is left alone
  - a bare `python-sdlc` becomes `sdlc` in a `downstream:` input and a dispatch
    loop, and `python-sdlc-extras` is not touched
  - every container-release.yml call signs: `sign: true` (added, or flipped
    from false) and the two cosign secrets, placed before the comments that
    introduce the next job; `secrets: inherit` and an already-signed call are
    left as they are
  - the weekly schedule goes before workflow_dispatch, for a repo in
    `weekly` that has none, at a cron that is the same every run
  - examples/release.yml becomes .github/workflows/release.yml, with the
    default branch substituted, only in a repo that publishes an image and has
    no release flow; any other release flow is reported, not merged
  - a pre-router ashigaru.yml (triage, code and fix called one by one, a
    release.yml@v2 job among them) becomes the target's router caller,
    unchanged, and then counts as the release flow; a router caller at the
    tag, a repo with no ashigaru.yml and a target with no caller are left alone
  - a workflow that pushes an image itself is reported; a commented-out push
    is not
  - Dependabot's removal is the target constitution's, and its failure is hand
    work; the target validator's verdict drops only the "newer than this
    validator" PIN line
  - a second run of every fixture finds nothing to do

Run inside the image, which has the interpreter and PyYAML the script needs:
  podman run --rm -v .:/src:ro,z -w /src --entrypoint /opt/constitution-venv/bin/python3 \
      quay.io/crunchtools/sdlc:<tag> tests/test_cutover.py
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("cutover", ROOT / "scripts" / "cutover.py")
cut = importlib.util.module_from_spec(_SPEC)
sys.modules["cutover"] = cut  # dataclasses look their module up there
_SPEC.loader.exec_module(cut)

FAILED: list[str] = []
REF = "v1.60.0"
TARGET = cut.Target(constitution="1.60.0", constitution_ref=REF)
TEMPLATE = (ROOT / "examples" / "release.yml").read_text()
COSIGN = ("COSIGN_PRIVATE_KEY", "COSIGN_PASSWORD")
CALL = "crunchtools/constitution/.github/workflows/container-release.yml"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def make(files: dict[str, str]) -> Path:
    base = Path(tempfile.mkdtemp())
    for path, text in files.items():
        (base / path).parent.mkdir(parents=True, exist_ok=True)
        (base / path).write_text(text)
    return base


def run(
    files: dict[str, str],
    name: str = "app",
    default: str = "main",
    target: cut.Target = TARGET,
    label: str = "",
) -> tuple[Path, cut.Result]:
    """Cut a fixture over, and pin that a second run finds nothing to do."""
    repo = make(files)
    result = cut.cutover(repo, name, default, target)
    again = cut.cutover(repo, name, default, target)
    check(
        f"idempotent: {label or name}: a second run changes nothing",
        not again.changed,
        str(again.changed),
    )
    return repo, result


def read(repo: Path, path: str) -> str:
    return (repo / path).read_text()


def jobs(repo: Path, path: str) -> dict:
    return yaml.safe_load(read(repo, path))["jobs"]


def release_job(secrets: str = "", with_: str = "      image: quay.io/crunchtools/app\n") -> str:
    """A container.yml whose one job calls container-release.yml, at an exact release."""
    body = f"    uses: {CALL}@v1.59.0\n"
    if with_:
        body += f"    with:\n{with_}"
    body += secrets
    return (
        "name: Container\non:\n  push:\n    branches: [main]\n  workflow_dispatch:\n"
        f"jobs:\n  build:\n{body}"
    )


QUAY = "    secrets:\n      QUAY_USERNAME: ${{ secrets.QUAY_USERNAME }}\n"
CI_ONLY = "name: CI\non: [push]\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make test\n"

# ------------------------------------------------------------ the constitution

MANIFEST = "# Constitution\n\n> **Inherits:** crunchtools/constitution v1.59.0\n\nbody v1.59.0\n"
HOOKS = (
    "repos:\n  - repo: https://github.com/crunchtools/constitution\n    rev: v1.59.0\n"
    "    hooks:\n      - id: validate\n"
    "  - repo: https://github.com/other/thing\n    rev: v1.59.0\n    hooks: []\n"
)
PINS = (
    "name: P\non: [push]\njobs:\n"
    "  a:\n    uses: crunchtools/constitution/.github/workflows/gate.yml@v1.59.0\n"
    "  b:\n    uses: crunchtools/constitution/.github/workflows/gate.yml@v1\n"
    "  c:\n    uses: someone/else/.github/workflows/gate.yml@v1.59.0\n"
)
repo, result = run(
    {
        ".specify/memory/constitution.md": MANIFEST,
        ".pre-commit-config.yaml": HOOKS,
        ".github/workflows/pins.yml": PINS,
    },
    label="pins",
)
pins = read(repo, ".github/workflows/pins.yml")
check("pins: a workflow pin moves to the target ref", f"gate.yml@{REF}" in pins, pins)
check(
    "pins: a @v1 major-tag call is left alone",
    "constitution/.github/workflows/gate.yml@v1\n" in pins,
)
check(
    "pins: another org's pin is left alone",
    "someone/else/.github/workflows/gate.yml@v1.59.0" in pins,
)
check(
    "pins: Inherits moves with them, and only Inherits",
    "Inherits:** crunchtools/constitution v1.60.0" in read(repo, ".specify/memory/constitution.md")
    and "body v1.59.0" in read(repo, ".specify/memory/constitution.md"),
)
hooks = read(repo, ".pre-commit-config.yaml")
check("pins: the constitution hook rev moves", "constitution\n    rev: v1.60.0" in hooks, hooks)
check("pins: another hook's rev does not", "other/thing\n    rev: v1.59.0" in hooks)
check(
    "pins: each file is reported",
    sum(c.startswith("constitution:") for c in result.changed) == 3,
    str(result.changed),
)

repo, _ = run(
    {".github/workflows/pins.yml": PINS},
    target=cut.Target(constitution="1.60.0", constitution_ref="v1"),
    label="pins, ref v1",
)
check(
    "pins: a major-tag target ref is written as given",
    "gate.yml@v1\n" in read(repo, ".github/workflows/pins.yml"),
)

# ---------------------------------------------------------------- the old name

RENAME = """\
name: Dispatch
on: [workflow_dispatch]
jobs:
  fan:
    uses: crunchtools/constitution/.github/workflows/gate.yml@v1
    with:
      downstream: python-sdlc
  loop:
    runs-on: ubuntu-latest
    steps:
      - run: |
          for repo in python-sdlc; do echo "$repo"; done
          for repo in python-sdlc-extras; do echo "$repo"; done
          echo "see https://github.com/crunchtools/python-sdlc.git and python-sdlc."
"""
repo, result = run({".github/workflows/dispatch.yml": RENAME}, label="rename")
text = read(repo, ".github/workflows/dispatch.yml")
check("rename: a downstream: input", "downstream: sdlc\n" in text, text)
check("rename: a for-loop word", "for repo in sdlc; do" in text, text)
check("rename: python-sdlc-extras is untouched", "for repo in python-sdlc-extras; do" in text)
check("rename: a sentence's last word is renamed", "and sdlc." in text, text)
check("rename: a URL's path is left alone", "crunchtools/python-sdlc.git" in text, text)
check(
    "rename: it is reported",
    any(c.startswith("renamed:") for c in result.changed),
    str(result.changed),
)

repo, _ = run(
    {
        ".pre-commit-config.yaml": "# python-sdlc hooks\nrepos: []\n",
        "pyproject.toml": '[tool.sdlc]\ndownstream = ["python-sdlc"]\n',
    },
    label="rename, hooks and config",
)
check(
    "rename: the hooks file and the config",
    "python-sdlc" not in read(repo, ".pre-commit-config.yaml") + read(repo, "pyproject.toml"),
)

# ---------------------------------------------------------------- signing


def signed(repo: Path, path: str = ".github/workflows/container.yml", job: str = "build") -> bool:
    j = jobs(repo, path)[job]
    secrets = j.get("secrets")
    return (j.get("with") or {}).get("sign") is True and (
        secrets == "inherit" or all(s in secrets for s in COSIGN)
    )


repo, result = run(
    {".github/workflows/container.yml": release_job(QUAY)}, label="sign, no sign key"
)
text = read(repo, ".github/workflows/container.yml")
w = jobs(repo, ".github/workflows/container.yml")["build"]
check("sign: a call with no sign key signs", signed(repo), text)
check("sign: the pin moved in the same run", f"container-release.yml@{REF}" in text)
check(
    "sign: the existing input and secret survive",
    w["with"]["image"] == "quay.io/crunchtools/app" and "QUAY_USERNAME" in w["secrets"],
    str(w),
)
check(
    "sign: the note and the key sit at the end of with:, before secrets:",
    text.index("image:")
    < text.index("# Every published image")
    < text.index("sign: true")
    < text.index("    secrets:"),
    text,
)
check(
    "sign: the cosign secrets are passed by name",
    all(f"{s}: ${{{{ secrets.{s} }}}}" in text for s in COSIGN),
    text,
)
check(
    "sign: it is reported", any(c.startswith("signing:") and "build" in c for c in result.changed)
)

repo, _ = run(
    {".github/workflows/container.yml": release_job(QUAY, "      image: x\n      sign: false\n")},
    label="sign: false",
)
text = read(repo, ".github/workflows/container.yml")
check(
    "sign: sign: false is flipped",
    signed(repo) and "sign: false" not in text and text.count("sign:") == 1,
    text,
)

repo, _ = run(
    {
        ".github/workflows/container.yml": release_job(
            QUAY, "      image: x\n      sign: false  # kept by hand\n"
        )
    },
    label="sign: false, comment",
)
text = read(repo, ".github/workflows/container.yml")
check(
    "sign: a trailing comment on the flipped line survives",
    "sign: true  # kept by hand" in text,
    text,
)

repo, _ = run({".github/workflows/container.yml": release_job(QUAY, "")}, label="sign, no with")
text = read(repo, ".github/workflows/container.yml")
check("sign: a call with no with: block gets one", signed(repo), text)
check(
    "sign: the new with: follows uses:",
    text.index("uses:")
    < text.index("    with:")
    < text.index("sign: true")
    < text.index("secrets:"),
    text,
)

repo, _ = run({".github/workflows/container.yml": release_job("", "")}, label="sign, neither block")
check(
    "sign: a call with neither block gets both",
    signed(repo),
    read(repo, ".github/workflows/container.yml"),
)

inherit = release_job("    secrets: inherit\n")
repo, result = run({".github/workflows/container.yml": inherit}, label="secrets: inherit")
text = read(repo, ".github/workflows/container.yml")
check("sign: secrets: inherit gets sign: true", signed(repo), text)
check(
    "sign: secrets: inherit gets no secrets added",
    "COSIGN" not in text and "secrets: inherit" in text,
    text,
)

# Comments after the secrets block introduce the next job; the new secrets go before them.
FOLLOWED = (
    release_job(QUAY)
    + "\n  # the second job\n  # runs after the build\n  later:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
)
repo, _ = run({".github/workflows/container.yml": FOLLOWED}, label="sign, trailing comments")
text = read(repo, ".github/workflows/container.yml")
check(
    "sign: secrets added before the next job's comments",
    signed(repo) and text.index("COSIGN_PASSWORD") < text.index("# the second job"),
    text,
)
check(
    "sign: the next job is intact",
    jobs(repo, ".github/workflows/container.yml")["later"]["steps"] == [{"run": "echo hi"}],
)

FOLLOWED_WITH = (
    release_job(QUAY, "      image: x\n      # tail comment\n")
    + "  # next\n  later:\n    runs-on: x\n"
)
repo, _ = run({".github/workflows/container.yml": FOLLOWED_WITH}, label="sign, comment ends with:")
text = read(repo, ".github/workflows/container.yml")
check(
    "sign: the key goes before a comment that ends with:",
    signed(repo) and text.index("sign: true") < text.index("# tail comment"),
    text,
)

PARTIAL = release_job("    secrets:\n      COSIGN_PRIVATE_KEY: ${{ secrets.COSIGN_PRIVATE_KEY }}\n")
repo, _ = run({".github/workflows/container.yml": PARTIAL}, label="sign, one cosign secret")
text = read(repo, ".github/workflows/container.yml")
check(
    "sign: only the missing cosign secret is added",
    signed(repo) and text.count("COSIGN_PRIVATE_KEY:") == 1,
    text,
)

DONE_ALREADY = release_job(
    "    secrets:\n      COSIGN_PRIVATE_KEY: ${{ secrets.COSIGN_PRIVATE_KEY }}\n      COSIGN_PASSWORD: ${{ secrets.COSIGN_PASSWORD }}\n",
    "      image: x\n      sign: true\n",
).replace("@v1.59.0", f"@{REF}")
repo, result = run(
    {".github/workflows/container.yml": DONE_ALREADY, ".github/workflows/release.yml": TEMPLATE},
    label="already signed",
)
check(
    "sign: an already-signed call is byte-for-byte untouched",
    read(repo, ".github/workflows/container.yml") == DONE_ALREADY,
)
check(
    "sign: and nothing is reported",
    result.changed == [] and result.hand == [],
    str((result.changed, result.hand)),
)

TWO = (
    "name: C\non: [push]\njobs:\n"
    f"  one:\n    uses: {CALL}@v1\n    with:\n      image: a\n"
    f"  two:\n    uses: {CALL}@v1\n    with:\n      image: b\n    secrets: inherit\n"
    f"  other:\n    uses: crunchtools/constitution/.github/workflows/gate.yml@v1\n"
)
repo, _ = run({".github/workflows/container.yml": TWO}, label="sign, two calls")
check("sign: every call in a file signs", signed(repo, job="one") and signed(repo, job="two"))
check(
    "sign: another reusable workflow does not",
    "sign" not in str(jobs(repo, ".github/workflows/container.yml")["other"]),
)

repo, result = run({".github/workflows/ci.yml": CI_ONLY}, label="no release call")
check(
    "sign: a workflow with no release call is untouched",
    read(repo, ".github/workflows/ci.yml") == CI_ONLY and result.changed == [],
)

# A job with no secrets key, followed by the next job's comments.
NO_SECRETS = release_job("") + "  # next job\n  later:\n    runs-on: x\n"
repo, _ = run(
    {".github/workflows/container.yml": NO_SECRETS}, label="sign, no secrets key, comments follow"
)
text = read(repo, ".github/workflows/container.yml")
check(
    "sign: secrets for a job that had none go before the next job's comments",
    signed(repo) and text.index("COSIGN_PASSWORD") < text.index("# next job"),
    text,
)

# --------------------------------------------------------- self-publishing

SELF = "name: Push\non: [push]\njobs:\n  p:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n          {line}\n"
for line, hand in (
    ("podman push quay.io/x/y:latest", True),
    ("docker push quay.io/x/y:latest", True),
    ("skopeo copy docker://a docker://b", True),
    ("# podman push quay.io/x/y:latest", False),
    ("echo podman pushes nothing", False),
):
    repo, result = run(
        {".github/workflows/push.yml": SELF.format(line=line)}, label=f"self-publish {line[:20]}"
    )
    got = any(
        "publishes outside container-release.yml" in h and "push.yml" in h for h in result.hand
    )
    check(
        f"self-publish: {line!r} is {'reported' if hand else 'not reported'}",
        got == hand,
        str(result.hand),
    )

# A flow-style job is valid YAML that signed_job cannot edit by line: the repo
# gets a hand note and the workflow is left as it was, not a crash.
FLOW = (
    "name: C\non:\n  push:\njobs:\n  build: {uses: "
    "crunchtools/constitution/.github/workflows/container-release.yml@v1, "
    "with: {image: quay.io/crunchtools/app}}\n"
)
repo, result = run({".github/workflows/container.yml": FLOW}, label="signing, flow style")
check(
    "signing: a flow-style job is left unchanged",
    read(repo, ".github/workflows/container.yml") == FLOW,
)
check(
    "signing: and named for a human",
    any(h.startswith("signing:") and "by hand" in h for h in result.hand),
    str(result.hand),
)

# ---------------------------------------------------------------- schedule

ON_FULL = release_job(QUAY).replace("@v1.59.0", f"@{REF}")
SIGNED = ON_FULL.replace(
    "      image: quay.io/crunchtools/app\n", "      image: x\n      sign: true\n"
).replace(
    QUAY,
    QUAY
    + "      COSIGN_PRIVATE_KEY: ${{ secrets.COSIGN_PRIVATE_KEY }}\n      COSIGN_PASSWORD: ${{ secrets.COSIGN_PASSWORD }}\n",
)
weekly = cut.Target("1.60.0", REF, weekly=frozenset({"app"}))
repo, result = run({".github/workflows/container.yml": SIGNED}, target=weekly, label="schedule")
text = read(repo, ".github/workflows/container.yml")
on = yaml.safe_load(text)[True]
check("schedule: a weekly repo gets one", on["schedule"] == [{"cron": cut.cron("app")}], str(on))
check(
    "schedule: it goes before workflow_dispatch",
    text.index("  schedule:") < text.index("  workflow_dispatch:"),
    text,
)
check(
    "schedule: the other triggers survive",
    on["push"] == {"branches": ["main"]} and "workflow_dispatch" in on,
)
check("schedule: it is reported", any(c.startswith("schedule:") for c in result.changed))

repo, _ = run(
    {".github/workflows/container.yml": SIGNED},
    name="other",
    target=weekly,
    label="schedule, not weekly",
)
check(
    "schedule: a repo not in weekly gets none",
    "schedule" not in read(repo, ".github/workflows/container.yml"),
)

HAS = SIGNED.replace(
    "  workflow_dispatch:\n", "  workflow_dispatch:\n  schedule:\n    - cron: '1 2 * * 3'\n"
)
repo, _ = run({".github/workflows/container.yml": HAS}, target=weekly, label="schedule exists")
check(
    "schedule: an existing schedule is left alone",
    read(repo, ".github/workflows/container.yml") == HAS,
)

NO_DISPATCH = SIGNED.replace("  workflow_dispatch:\n", "")
repo, _ = run(
    {".github/workflows/container.yml": NO_DISPATCH}, target=weekly, label="schedule, no dispatch"
)
text = read(repo, ".github/workflows/container.yml")
check(
    "schedule: with no workflow_dispatch it goes at the end of on:",
    yaml.safe_load(text)[True]["schedule"] == [{"cron": cut.cron("app")}]
    and text.index("schedule:") < text.index("jobs:"),
    text,
)

repo, _ = run(
    {".github/workflows/ci.yml": CI_ONLY}, target=weekly, label="schedule, no release call"
)
check(
    "schedule: a workflow that publishes nothing gets none",
    read(repo, ".github/workflows/ci.yml") == CI_ONLY,
)

check("schedule: the cron is deterministic", cut.cron("app") == cut.cron("app") == cut.cron("app"))
check(
    "schedule: and varies by repo",
    len({cut.cron(n) for n in ("gatehouse", "openclaw", "ubi10-httpd-php", "google-workspace-mcp")})
    > 1,
)
minute, hour, dom, month, dow = cut.cron("app").split()
check(
    "schedule: the cron is a valid weekly slot",
    0 <= int(minute) < 60 and 2 <= int(hour) <= 6 and dom == month == "*" and 0 <= int(dow) < 7,
    cut.cron("app"),
)

# ----------------------------------------------------------------- release

PUBLISHES = {".github/workflows/container.yml": SIGNED}

repo, result = run(PUBLISHES, label="release, main")
check(
    "release: written for a main repo, as the template",
    read(repo, ".github/workflows/release.yml") == TEMPLATE,
)
check(
    "release: it is reported",
    any(c.startswith("release:") for c in result.changed),
    str(result.changed),
)

repo, _ = run(PUBLISHES, default="master", label="release, master")
text = read(repo, ".github/workflows/release.yml")
check(
    "release: master is substituted for main",
    "refs/heads/master" in text
    and "refs/heads/main" not in text
    and "--base master" in text
    and "--base main" not in text,
    text,
)
check("release: and the result is a workflow", yaml.safe_load(text)["jobs"])

repo, result = run(PUBLISHES, default="m$(id)", label="release, unsafe branch")
check(
    "release: a default branch that is not a plain name is left to a human, never written",
    not (repo / ".github/workflows/release.yml").exists()
    and any("not a plain name" in h for h in result.hand),
    str(result.hand),
)

repo, result = run({".github/workflows/ci.yml": CI_ONLY}, label="release, publishes nothing")
check(
    "release: not written for a repo that publishes nothing",
    not (repo / ".github/workflows/release.yml").exists() and not result.hand,
)

ASHIGARU = (
    "name: R\non: [workflow_dispatch]\njobs:\n  tag:\n"
    "    uses: crunchtools/ashigaru/.github/workflows/release.yml@v2\n"
)
repo, result = run(
    {**PUBLISHES, ".github/workflows/old-release.yml": ASHIGARU}, label="release, ashigaru v2"
)
check(
    "release: an ashigaru release.yml@v2 caller is reported, not given a second flow",
    not (repo / ".github/workflows/release.yml").exists()
    and any(
        h.startswith("release:") and "old-release.yml" in h and "release-tag.yml@v5" in h
        for h in result.hand
    ),
    str(result.hand),
)

OTHER = "name: Mine\non: [workflow_dispatch]\njobs:\n  go:\n    runs-on: x\n    steps:\n      - run: ./release.sh\n"
repo, result = run(
    {**PUBLISHES, ".github/workflows/release.yml": OTHER}, label="release, different release.yml"
)
check(
    "release: a different release.yml is kept and reported",
    read(repo, ".github/workflows/release.yml") == OTHER
    and any("exists" in h and "merge by hand" in h for h in result.hand),
    str(result.hand),
)

V5 = ASHIGARU.replace("release.yml@v2", "release-tag.yml@v5")
repo, result = run({**PUBLISHES, ".github/workflows/tag.yml": V5}, label="release, already v5")
check(
    "release: release-tag.yml@v5 already called is silent",
    not (repo / ".github/workflows/release.yml").exists()
    and not any(h.startswith("release:") for h in result.hand)
    and not any(c.startswith("release:") for c in result.changed),
    str((result.hand, result.changed)),
)

repo, result = run(
    {**PUBLISHES, ".github/workflows/release.yml": TEMPLATE},
    label="release, template already there",
)
check(
    "release: the template already in place is silent",
    not any(h.startswith("release:") for h in result.hand)
    and read(repo, ".github/workflows/release.yml") == TEMPLATE,
)

# --------------------------------------------------------------- Dependabot

seen = []


def drop(repo: Path, validator: object) -> list[str]:
    seen.append((repo, validator))
    return ["removed .github/dependabot.yml", "removed .github/workflows/dependabot-auto-merge.yml"]


def boom(_repo: Path, _validator: object) -> list[str]:
    raise RuntimeError("renovate config missing")


validator = SimpleNamespace(own_version=lambda: "1.60.0", validate=lambda _p: [])
repo = make({"README.md": "x\n"})
result = cut.cutover(
    repo,
    "app",
    "main",
    cut.Target("1.60.0", REF, bump=SimpleNamespace(drop_dependabot=drop), validator=validator),
)
check(
    "dependabot: the constitution's own teardown is called with the checkout and validator",
    seen == [(repo, validator)],
    str(seen),
)
check(
    "dependabot: each change is reported",
    result.changed
    == [
        "Dependabot: removed .github/dependabot.yml",
        "Dependabot: removed .github/workflows/dependabot-auto-merge.yml",
    ],
    str(result.changed),
)
result = cut.cutover(
    repo,
    "app",
    "main",
    cut.Target("1.60.0", REF, bump=SimpleNamespace(drop_dependabot=boom), validator=validator),
)
check(
    "dependabot: a RuntimeError is hand work, not a crash",
    result.hand == ["Dependabot: renovate config missing"] and result.changed == [],
    str((result.hand, result.changed)),
)
seen.clear()
cut.cutover(
    repo,
    "app",
    "main",
    cut.Target("1.60.0", REF, bump=SimpleNamespace(drop_dependabot=drop), validator=None),
)
cut.cutover(repo, "app", "main", cut.Target("1.60.0", REF, bump=None, validator=validator))
check("dependabot: skipped when either module is None", seen == [])

# ----------------------------------------------------------------- validate


def judging(verdict: list[str], own: str | None = "1.59.0") -> cut.Target:
    v = SimpleNamespace(own_version=lambda: own, validate=lambda _path: list(verdict))
    return cut.Target("1.60.0", REF, validator=v)


VERDICT = [
    "PIN: constitution v1.60.0 is newer than this validator (1.59.0)",
    "PIN: workflow pins v1.58.0, not v1.60.0",
    "IX: container-release.yml call does not sign",
]
repo = make({".specify/memory/constitution.md": MANIFEST})
check(
    "validate: the newer-than-this-validator PIN line is filtered, the rest kept",
    cut.validate(repo, judging(VERDICT)) == VERDICT[1:],
    str(cut.validate(repo, judging(VERDICT))),
)
check(
    "validate: with no validator version nothing is filtered",
    cut.validate(repo, judging(VERDICT, own=None)) == VERDICT,
)
check(
    "validate: a non-PIN line that says it is kept",
    cut.validate(repo, judging(["IX: newer than this validator"]))
    == ["IX: newer than this validator"],
)
check("validate: a clean verdict is []", cut.validate(repo, judging([])) == [])
check("validate: no target validator is []", cut.validate(repo, TARGET) == [])
unjudged = cut.validate(make({"README.md": "x"}), judging([]))
check(
    "validate: a repo with no manifest is not a pass",
    len(unjudged) == 1 and unjudged[0].startswith("MANIFEST:"),
    str(unjudged),
)
asked = []
cut.validate(
    repo,
    cut.Target(
        "1.60.0",
        REF,
        validator=SimpleNamespace(
            own_version=lambda: "1", validate=lambda p: asked.append(p) or []
        ),
    ),
)
check(
    "validate: it judges the manifest",
    asked == [repo / ".specify/memory/constitution.md"],
    str(asked),
)

# ------------------------------------------------- the target constitution

# constitution_modules() over a checkout shaped like constitution's: fleet-bump.py
# imports fleet_common from scripts/, and the validator sits at the top.
fake = make(
    {
        "scripts/fleet_common.py": "MARK = 'common'\n",
        "scripts/fleet-bump.py": (
            "from fleet_common import MARK\n"
            "def drop_dependabot(root, validator):\n"
            "    return [MARK + ':' + validator.own_version()]\n"
        ),
        "validate-constitution.py": (
            "def own_version():\n    return '1.60.0'\ndef validate(path):\n    return []\n"
        ),
    }
)
bump, validator = cut.constitution_modules(fake)
loaded = cut.Target("1.60.0", REF, bump=bump, validator=validator)
result = cut.Result()
cut.drop_dependabot(make({}), loaded, result)
check(
    "constitution_modules: fleet-bump.py loads with its fleet_common and drives the drop",
    result.changed == ["Dependabot: common:1.60.0"] and not result.hand,
    str(result),
)
check(
    "constitution_modules: the validator loads and judges a manifest",
    cut.validate(make({".specify/memory/constitution.md": MANIFEST}), loaded) == [],
)

# --------------------------------------------------------------- everything

# One repo with all of it: the changes are independent, and a second run is quiet.
ALL = {
    ".specify/memory/constitution.md": MANIFEST,
    ".pre-commit-config.yaml": HOOKS,
    ".github/workflows/container.yml": release_job(QUAY) + "      # more\n",
    ".github/workflows/dispatch.yml": RENAME,
}
repo, result = run(ALL, default="master", target=weekly, label="everything")
kinds = {c.split(":")[0] for c in result.changed}
check(
    "everything: each kind of change is reported once over",
    {"constitution", "renamed", "signing", "schedule", "release"} <= kinds,
    str(kinds),
)
check(
    "everything: every workflow still parses",
    all(yaml.safe_load(p.read_text()) for p in (repo / ".github/workflows").glob("*.yml")),
)


# --------------------------------------------------------- Ashigaru caller

CALLER = (
    "# Ashigaru: copy unchanged.\nname: Ashigaru\non:\n  issues:\n  pull_request_target:\n"
    "permissions: {}\njobs:\n  ashigaru:\n    name: Ashigaru\n"
    f"    uses: crunchtools/ashigaru/.github/workflows/router.yml@{cut.ASHIGARU_TAG}\n"
    "    secrets:\n      ASHIGARU_APP_KEY: ${{ secrets.ASHIGARU_APP_KEY }}\n"
)
ROUTED = cut.Target(
    "1.60.0", REF, ashigaru_caller=CALLER, ashigaru_source="crunchtools/ashigaru@abc123"
)
PRE_ROUTER = "name: Ashigaru\non:\n  issues:\njobs:\n" + "".join(
    f"  {job}:\n    uses: crunchtools/ashigaru/.github/workflows/{job}.yml@{ref}\n"
    for job, ref in (("triage", "v2"), ("code", "v3"), ("fix", "v2"), ("release", "v2"))
)
CALLER_PATH = ".github/workflows/ashigaru.yml"

repo, result = run({CALLER_PATH: PRE_ROUTER}, target=ROUTED, label="caller, pre-router")
check(
    "caller: a pre-router ashigaru.yml becomes the router caller", read(repo, CALLER_PATH) == CALLER
)
check(
    "caller: the re-copy is reported with its source",
    any(c.startswith("ashigaru:") and "crunchtools/ashigaru@abc123" in c for c in result.changed),
    str(result.changed),
)

repo, result = run(
    {**PUBLISHES, CALLER_PATH: PRE_ROUTER}, target=ROUTED, label="caller, pre-router publisher"
)
check(
    "caller: the router's release job passes the release check: no release.yml, no hand work",
    not (repo / ".github/workflows/release.yml").exists()
    and not any(h.startswith("release:") for h in result.hand)
    and any(c.startswith("ashigaru:") for c in result.changed),
    str((result.hand, result.changed)),
)
repo, result = run(
    {**PUBLISHES, CALLER_PATH: CALLER}, target=TARGET, label="caller, router already there"
)
check(
    "release: a router.yml caller at the tag is the release flow",
    not (repo / ".github/workflows/release.yml").exists()
    and not any(h.startswith("release:") for h in result.hand),
    str(result.hand),
)

CUSTOM = CALLER.replace("# Ashigaru: copy unchanged.", "# ours, edited")
repo, result = run({CALLER_PATH: CUSTOM}, target=ROUTED, label="caller, router edited")
check(
    "caller: a router caller at the tag is left as it is, edits and all",
    read(repo, CALLER_PATH) == CUSTOM and not result.changed,
)
OLD_ROUTER = CALLER.replace(f"router.yml@{cut.ASHIGARU_TAG}", "router.yml@v4")
repo, _ = run({CALLER_PATH: OLD_ROUTER}, target=ROUTED, label="caller, router v4")
check("caller: a router caller at an older tag is re-copied", read(repo, CALLER_PATH) == CALLER)

repo, result = run({".github/workflows/ci.yml": CI_ONLY}, target=ROUTED, label="caller, none")
check(
    "caller: a repo with no ashigaru.yml is not enrolled",
    not (repo / CALLER_PATH).exists() and not result.changed,
)
repo, result = run({CALLER_PATH: PRE_ROUTER}, target=TARGET, label="caller, no target caller")
check(
    "caller: without a caller in the target, ashigaru.yml is untouched",
    read(repo, CALLER_PATH) == PRE_ROUTER and not result.changed,
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
