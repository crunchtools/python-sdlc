#!/bin/sh
# shellcheck disable=SC2016
#   SC2016 is wrong throughout this file. Every `inside sh -c '... "$SDLC_*"'`
#   below MUST keep single quotes: those variables exist inside the container,
#   where the shell that expands them runs, and not on the host.
# Contract test: the image holds what it promises, without starting a service.
#
# The Container Image profile requires this and requires it to run against the
# PUSHED DIGEST on every trigger, including the weekly cron -- a rebuild changes
# packages without a commit.
#
# For this image the contract test is not a formality, it is the central test.
# The image's entire purpose is "these executables exist, at these versions". If
# a `uv tool install` silently resolved an older ruff, or a COPY --from landed a
# binary that cannot execute on this base, nothing else in CI would notice: the
# build succeeds and every consuming repo quietly starts linting with a
# different tool than it pinned.
#
# Usage: tests/contract.sh <image-ref>

set -eu

IMAGE=${1:?usage: tests/contract.sh <image-ref>}
FAILED=0

note() { printf '  %s\n' "$*"; }
ok()   { printf 'ok   %s\n' "$*"; }
bad()  { printf 'FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }

inside() { podman run --rm --entrypoint="" "$IMAGE" "$@"; }

printf '== contract test: %s\n\n' "$IMAGE"

# ------------------------------------------------- every tool's files are its own
#
# The gate runs its containers with `--cap-drop=all`, so the container's root
# has no CAP_DAC_OVERRIDE and cannot read a file it does not own whose mode says
# no. An image that ships such a file under /opt has a tool that works in
# testing -- where a bare `podman run` still has the capability -- and fails in
# the gate.
#
# This is not hypothetical. The CodeQL bundle extracts 25 precompiled `.qlx`
# files at mode 0600 owned by uid 1001, and with them unreadable
# `codeql database analyze` recompiled all 55 queries and then died with an
# internal error: 355 seconds and an exit 2 instead of a verdict, on trentina.
# The Containerfile normalizes them; this is what keeps the next bundle, or the
# next tool, from reintroducing it silently.
unreadable=$(inside sh -c 'find /opt -xdev -type f ! -perm -o=r 2>/dev/null | head -20')
if [ -z "$unreadable" ]; then
  ok "every file under /opt is readable without CAP_DAC_OVERRIDE"
else
  bad "files under /opt need a capability the gate drops:"
  printf '%s\n' "$unreadable" | sed 's/^/       /'
fi

# ---------------------------------------------------------- every tool executes
#
# `command -v` is not enough: a COPY --from can land a binary that is present
# and cannot run on this base (wrong libc, wrong arch). Each one is executed.

for probe in \
  "ruff:ruff --version" \
  "semgrep:semgrep --version" \
  "pip-audit:pip-audit --version" \
  "gourmand:gourmand --version" \
  "codeql:codeql version --format=terse" \
  "uv:uv --version" \
  "hadolint:hadolint --version" \
  "grype:grype version" \
  "node:node --version" \
  "npm:npm --version" \
  "markdownlint-cli2:markdownlint-cli2 --help | head -1" \
  "lychee:lychee --version" \
  "podman-remote:podman-remote --version" \
  "skopeo:skopeo --version" \
  "docker:docker --version" \
  "cosign:cosign version" \
  "syft:syft --version" \
  "zizmor:zizmor --version" \
  "claude:claude --version" \
  "pyjwt:/opt/constitution-venv/bin/python3 -c 'import jwt; print(jwt.__version__)'" \
  "gh:gh --version" \
  "ashigaru:test -f /opt/ashigaru/scripts/common.py && ls /opt/ashigaru/prompts/review.md /opt/ashigaru/schemas/review.json" \
  "constitution-scripts:test -f /opt/constitution/scripts/audit_alert.py && echo present" \
  "sdlc_jobs-path:/opt/constitution-venv/bin/python3 -c 'import sys; assert \"/usr/local/lib/sdlc\" in sys.path; print(\"on path\")'" \
  "git:git --version" \
  "jq:jq --version" \
  "curl:curl --version" \
  "bash:bash --version"
do
  name=${probe%%:*}
  cmd=${probe#*:}
  if out=$(inside sh -c "$cmd" 2>&1); then
    ok "$name executes ($(printf '%s' "$out" | head -1))"
  else
    bad "$name does not execute: $out"
  fi
done

# Gatehouse has no --version; its usage line is the only thing it offers.
if inside sh -c 'gatehouse --help' >/dev/null 2>&1; then
  ok "gatehouse executes"
else
  bad "gatehouse does not execute"
fi

# ------------------------------------------------- the pinned versions are real
#
# The Containerfile ARGs, the OCI labels and what is actually installed are
# three separate claims. This asserts the third matches the first.

check_version() {
  _tool=$1; _want=$2; _got=$3
  case "$_got" in
    *"$_want"*) ok "$_tool is $_want" ;;
    *)          bad "$_tool should be $_want but reports: $_got" ;;
  esac
}

check_version ruff      "$(inside sh -c 'printf %s "$SDLC_RUFF_VERSION"')" \
                        "$(inside sh -c 'ruff --version' 2>&1)"
check_version semgrep   "$(inside sh -c 'printf %s "$SDLC_SEMGREP_VERSION"')" \
                        "$(inside sh -c 'semgrep --version' 2>&1)"
check_version pip-audit "$(inside sh -c 'printf %s "$SDLC_PIP_AUDIT_VERSION"')" \
                        "$(inside sh -c 'pip-audit --version' 2>&1)"

check_version hadolint  "$(inside sh -c 'printf %s "$SDLC_HADOLINT_VERSION"')" \
                        "$(inside sh -c 'hadolint --version' 2>&1)"
check_version grype     "$(inside sh -c 'printf %s "$SDLC_GRYPE_VERSION"')" \
                        "$(inside sh -c 'grype version' 2>&1)"
check_version node      "$(inside sh -c 'printf %s "$SDLC_NODE_VERSION"')" \
                        "$(inside sh -c 'node --version' 2>&1)"
# The probe is `--help | head -1` ("markdownlint-cli2 v0.24.0 (markdownlint
# ..."): the package has no --version flag, and its banner names the version.
check_version markdownlint-cli2 "$(inside sh -c 'printf %s "$SDLC_MARKDOWNLINT_CLI2_VERSION"')" \
                        "$(inside sh -c 'markdownlint-cli2 --help' 2>&1 | head -1)"
check_version lychee    "$(inside sh -c 'printf %s "$SDLC_LYCHEE_VERSION"')" \
                        "$(inside sh -c 'lychee --version' 2>&1)"

# podman-remote and skopeo float with the Hummingbird repo, so there is no pin
# to compare; they must run (above) and be the Hummingbird majors.
check_version podman-remote "podman-remote version 6." "$(inside sh -c 'podman-remote --version' 2>&1)"
check_version skopeo    "skopeo version 1." "$(inside sh -c 'skopeo --version' 2>&1)"
check_version docker    "$(inside sh -c 'printf %s "$SDLC_DOCKER_VERSION"')" \
                        "$(inside sh -c 'docker --version' 2>&1)"
check_version cosign    "$(inside sh -c 'printf %s "$SDLC_COSIGN_VERSION"')" \
                        "$(inside sh -c 'cosign version' 2>&1)"
check_version syft      "$(inside sh -c 'printf %s "$SDLC_SYFT_VERSION"')" \
                        "$(inside sh -c 'syft --version' 2>&1)"
check_version zizmor    "$(inside sh -c 'printf %s "$SDLC_ZIZMOR_VERSION"')" \
                        "$(inside sh -c 'zizmor --version' 2>&1)"
check_version claude    "$(inside sh -c 'printf %s "$SDLC_CLAUDE_CODE_VERSION"')" \
                        "$(inside sh -c 'claude --version' 2>&1)"
check_version renovate  "$(inside sh -c 'printf %s "$SDLC_RENOVATE_VERSION"')" \
                        "$(inside sh -c 'renovate --version' 2>&1)"
check_version cryptography "$(inside sh -c 'printf %s "$SDLC_CRYPTOGRAPHY_VERSION"')" \
                        "$(inside sh -c '/opt/constitution-venv/bin/python3 -c "import cryptography; print(cryptography.__version__)"' 2>&1)"
check_version pyjwt     "$(inside sh -c 'printf %s "$SDLC_PYJWT_VERSION"')" \
                        "$(inside sh -c '/opt/constitution-venv/bin/python3 -c "import jwt; print(jwt.__version__)"' 2>&1)"
check_version gh        "$(inside sh -c 'printf %s "$SDLC_GH_VERSION"')" \
                        "$(inside sh -c 'gh --version' 2>&1)"
# An image must not update itself.
if [ "$(inside sh -c 'printf %s "$DISABLE_AUTOUPDATER"')" = 1 ]; then
  ok "claude's auto-updater is disabled"
else
  bad "DISABLE_AUTOUPDATER is not 1 in the image"
fi

# codeql's bundle name is codeql-bundle-vX.Y.Z and the CLI reports X.Y.Z.
_bundle=$(inside sh -c 'printf %s "$SDLC_CODEQL_BUNDLE"')
check_version codeql "${_bundle#codeql-bundle-v}" \
                     "$(inside sh -c 'codeql version --format=terse' 2>&1)"

# Gourmand is EXEMPT from this check, and not because of a defect: its image is
# a WRAPPER. `crunchtools/gourmand:1.0.0` is that packaging repo's own first
# release and the binary inside is upstream mattdm/gourmand v0.16.5, so the two
# numbers are both correct and asserting they match would be asserting something
# untrue. `sdlc versions` prints both columns instead.
note "gourmand's image tag is a packaging version, not the binary's -- both shown by 'sdlc versions'"

# ------------------------------------------------------- the script's own shape

if inside sdlc list >/dev/null 2>&1; then
  ok "sdlc list runs"
else
  bad "sdlc list does not run"
fi

# Every target `list` advertises must be one the dispatcher accepts.
#
# The first version of this only grepped each target's output for 'unknown
# target' and then printed success unconditionally -- so a target that failed
# for ANY other reason, including one that could not run at all, still passed.
# Gatehouse caught that it asserted nothing.
#
# Run with no repo mounted, every target is expected to FAIL. What is being
# asserted is HOW: exit 2 with a real reason ("could not run"), never exit 2
# with 'unknown target', which is the shape of a typo between the list and the
# dispatch table.
advertised=$(inside sdlc list 2>/dev/null | sed -n 's/^  \([a-z-]*\)$/\1/p')
[ -n "$advertised" ] || bad "sdlc list advertised no targets at all"

for target in $advertised; do
  out=$(inside sdlc "$target" 2>&1 || true)
  case "$out" in
    *"unknown target"*)
      bad "sdlc advertises '$target' but the dispatcher rejects it as unknown"
      continue ;;
  esac
  # It must also SAY something. A target that exits silently is the
  # green-because-nothing-looked failure the exit-code contract exists to
  # prevent, and it reads identically to success.
  #
  # What it says is not asserted: with no repo mounted most targets reach
  # "GATE CANNOT RUN", but `gatehouse` handed an empty diff legitimately
  # reports "nothing to review" and passes. Demanding a failure here was the
  # first version of this check, and it was wrong about exactly that.
  if [ -z "$out" ]; then
    bad "sdlc '$target' exited silently; a target must report what happened"
  fi
done
ok "every advertised target is dispatchable and reports a reason"

# Exit 2 for an unknown target: the contract says unknown input is 'could not
# run', not 'found nothing'.
inside sdlc definitely-not-a-target >/dev/null 2>&1 && rc=0 || rc=$?
if [ "$rc" -eq 2 ]; then
  ok "an unknown target exits 2"
else
  bad "an unknown target exits $rc, should be 2"
fi

# ---------------------------- Renovate's token is out of its children's reach
#
# sdlc#121. Dependency code that Renovate's package managers run (an sdist's
# setup.py) is a child of Renovate, as the same uid. The `renovate` job starts
# Renovate with renovate-node, node at mode 0111, so the kernel marks it not
# dumpable and that child cannot read its /proc/<pid>/environ or mem, or ptrace
# it. Proved here with the job's own lockdown flags (sdlc_jobs/renovate.py
# Job.nested): a stand-in token in the parent's environment, a Python child
# that tries all three, and plain node as the control that shows the probe
# would see a leak.
RPROBE=$(mktemp -d)
cat > "$RPROBE/child.py" <<'PY'
import ctypes, os
parent = os.getppid()
for name in ("environ", "mem"):
    try:
        with open(f"/proc/{parent}/{name}", "rb") as fh:
            print(name, "LEAK" if name == "mem" or b"probe-renovate-token" in fh.read() else "clean")
    except OSError:
        print(name, "denied")
libc = ctypes.CDLL(None, use_errno=True)
print("ptrace", "denied" if libc.ptrace(0x4206, parent, 0, 0) != 0 else "LEAK")
PY
cat > "$RPROBE/parent.js" <<'JS'
const { spawnSync } = require("child_process");
const out = spawnSync("python3", ["/probe/child.py"], { env: { PATH: process.env.PATH } });
process.stdout.write(out.stdout.toString() + out.stderr.toString());
JS
chmod -R a+rX "$RPROBE"
renovate_child() {
  podman run --rm --cap-drop=all --security-opt=no-new-privileges --read-only \
    --tmpfs /tmp:rw,exec,size=64m,mode=1777 -e HOME=/tmp \
    -e RENOVATE_TOKEN=probe-renovate-token -v "$RPROBE:/probe:ro,z" \
    --entrypoint "$1" "$IMAGE" /probe/parent.js 2>&1
}
_mode=$(inside stat -c '%a %u' /usr/local/libexec/renovate-node 2>&1)
if [ "$_mode" = "111 0" ]; then
  ok "renovate-node is execute-only and root's"
else
  bad "renovate-node is '$_mode', not mode 111 owned by root"
fi
_got=$(renovate_child /usr/local/libexec/renovate-node)
if [ "$_got" = "$(printf 'environ denied\nmem denied\nptrace denied')" ]; then
  ok "a child of Renovate's node cannot read its environ or mem, or ptrace it"
else
  bad "a child of Renovate's node reached it:"
  printf '%s\n' "$_got" | sed 's/^/       /'
fi
if renovate_child node | grep -q '^environ LEAK$'; then
  ok "the control: under plain node the same child reads the token"
else
  bad "the control did not leak under plain node, so the probe proves nothing"
fi
_ver=$(podman run --rm --cap-drop=all --security-opt=no-new-privileges --read-only \
  --tmpfs /tmp -e HOME=/tmp --entrypoint /usr/local/libexec/renovate-node "$IMAGE" \
  /opt/renovate/node_modules/renovate/dist/renovate.js --version 2>&1)
if [ "$_ver" = "$(inside sh -c 'printf %s "$SDLC_RENOVATE_VERSION"')" ]; then
  ok "Renovate runs under renovate-node, as the job starts it ($_ver)"
else
  bad "Renovate under renovate-node says '$_ver'"
fi
rm -rf "$RPROBE"

# -------------------------------------------- nothing that must not ship is here

# A root-owned /tmp entry reappears at run time through podman's tmpfs copy-up.
if [ -z "$(inside sh -c 'ls -A /tmp 2>/dev/null')" ]; then
  ok "/tmp is empty"
else
  bad "/tmp is not empty; podman's tmpfs copy-up will resurrect its contents"
fi

# RHSM entitlement or consumer certs must never ship (Container Image profile).
if inside sh -c 'ls /etc/pki/entitlement/* /etc/pki/consumer/* 2>/dev/null' | grep -q .; then
  bad "RHSM entitlement or consumer certificates are in the image"
else
  ok "no RHSM entitlement or consumer certificates"
fi

# ------------------------------------- the offline targets actually RUN, on a
# fixture
#
# Dispatchability is not runnability, and an earlier version of this file
# confused the two twice -- first asserting nothing, then accepting a
# "GATE CANNOT RUN" message as proof a target works. So the cheap offline
# targets are given a real project and must return 0 on it.
#
# Three of the ten run here. The other seven are excluded for stated reasons,
# not overlooked:
#   mypy, pytest, pip-audit  need `uv sync` against a network
#   gatehouse                needs an API key
#   constitution             needs to clone the fleet constitution
#   codeql                   builds a database; minutes, not seconds
#   gourmand                 audits REPOSITORY hygiene, not just the code --
#                            IF001 wants a configured .pre-commit-config.yaml,
#                            and satisfying its whole check set would mean
#                            growing this fixture into a full repo. It is
#                            verified against real ones instead: trentina, and
#                            this repo's own pre-commit hook.
# Those are covered by the dispatch check above and the verification in
# README.md.

FIXTURE=$(mktemp -d)
trap 'rm -rf "$FIXTURE"' EXIT 0
mkdir -p "$FIXTURE/src" "$FIXTURE/.semgrep"

cat > "$FIXTURE/pyproject.toml" <<'TOML'
[project]
name = "fixture"
version = "0.0.0"

[tool.ruff]
line-length = 100

[tool.sdlc.push.semgrep]
paths = ["src"]
TOML

# Clean, formatted, lint-free, and not slop: every offline target must pass it.
cat > "$FIXTURE/src/example.py" <<'PY'
"""A module the offline gates should have nothing to say about."""


def add(left: int, right: int) -> int:
    """Return the sum of two integers."""
    return left + right
PY

# gourmand refuses to enable its cache unless a .gitignore lists the cache
# directory, and then writes it -- which is why the fleet's gourmand hook mounts
# the tree read-WRITE while its gatehouse hook mounts read-only. A fixture
# without this is not a realistic repo.
printf '.gourmand-cache/\n' > "$FIXTURE/.gitignore"

cat > "$FIXTURE/.semgrep/rules.yml" <<'YAML'
rules:
  - id: fixture-no-eval
    languages: [python]
    severity: ERROR
    message: eval() is not allowed
    pattern: eval(...)
YAML

# No run-tests.sh in the fixture, so the semgrep target skips the rule-test step
# and goes straight to the scan.
for target in ruff-check ruff-format semgrep; do
  if out=$(podman run --rm -v "$FIXTURE:/repo:rw,z" -w /repo \
             --entrypoint="" "$IMAGE" sdlc "$target" 2>&1); then
    ok "$target runs clean on a fixture project"
  else
    bad "$target failed on a clean fixture project"
    printf '%s\n' "$out" | sed 's/^/       /' | tail -8
  fi
done

# lint is the verdict half of those two, and it runs on a READ-ONLY tree: push
# and ci mount it that way, so a ruff that tried to write .ruff_cache would be a
# check that cannot run.
if out=$(podman run --rm -v "$FIXTURE:/repo:ro,z" -w /repo \
           --entrypoint="" "$IMAGE" sdlc lint 2>&1); then
  ok "lint runs clean on a read-only fixture project"
else
  bad "lint failed on a clean, read-only fixture project"
  printf '%s\n' "$out" | sed 's/^/       /' | tail -8
fi

# The plan the launcher reads must name every default check, in order, and
# resolve a repo's own setting. This is the seam between the TOML the image
# reads and the shell that acts on it, so it is checked on the real image.
plan=$(podman run --rm -v "$FIXTURE:/repo:ro,z" -w /repo --entrypoint="" \
         "$IMAGE" sdlc plan push 2>&1 | cut -f1 | tr '\n' ' ')
want="lint mypy pytest gourmand gatehouse semgrep pip-audit constitution codeql "
if [ "$plan" = "$want" ]; then
  ok "sdlc plan push lists the push defaults in order"
else
  bad "sdlc plan push gave '$plan', want '$want'"
fi
plan=$(podman run --rm -v "$FIXTURE:/repo:ro,z" -w /repo --entrypoint="" \
         "$IMAGE" sdlc plan ci 2>&1 | cut -f1 | tr '\n' ' ')
case "$plan" in
  *gourmand*|*gatehouse*) bad "sdlc plan ci runs a reviewer CI already runs: $plan" ;;
  *) ok "sdlc plan ci leaves out the two reviewers" ;;
esac

# A repo with no image chain gets ONE lane, which is the launcher's sequential
# path: the same run it had before lanes existed.
lanes=$(podman run --rm -v "$FIXTURE:/repo:ro,z" -w /repo --entrypoint="" \
          "$IMAGE" sdlc plan ci 2>&1 | cut -f2 | sort -u | tr '\n' ' ')
if [ "$lanes" = "main " ]; then
  ok "a repo with no image build plans one lane"
else
  bad "a repo with no image build planned lanes '$lanes', want 'main '"
fi

# And a repo with one gets two: the build and the tests that run what it built
# in their own lane, every uv sync in the other. This is the seam between the
# TOML the image reads and the parallel executor in the shell, so it is checked
# on the real image rather than only in the unit test.
CHAIN=$(mktemp -d)
mkdir -p "$CHAIN/tests"
printf 'FROM scratch\n' > "$CHAIN/Containerfile"
cat > "$CHAIN/pyproject.toml" <<'TOML'
[project]
name = "chain"
version = "0.0.0"

[tool.sdlc.ci]
checks = ["image", "image-tests"]

[tool.sdlc.ci.image]
kind = "image-build"

[tool.sdlc.ci.image-tests]
kind = "pytest-in-image"
tests = ["tests/"]
TOML

chain_plan=$(podman run --rm -v "$CHAIN:/repo:ro,z" -w /repo --entrypoint="" \
               -e SDLC_HOST_REPO="$CHAIN" -e SDLC_CACHE_VOLUME=contract-test \
               "$IMAGE" sdlc plan ci 2>&1)
rm -rf "$CHAIN"
nlanes=$(printf '%s\n' "$chain_plan" | cut -f2 | sort -u | grep -c .)
off_main=$(printf '%s\n' "$chain_plan" | awk -F'\t' '$2 != "main" {print $1}' | sort -u | tr '\n' ' ')
syncers=$(printf '%s\n' "$chain_plan" | awk -F'\t' '
  $1 == "mypy" || $1 == "pytest" || $1 == "pip-audit" {print $2}' | sort -u | grep -c .)
if [ "$nlanes" = 2 ] && [ "$off_main" = "image image-tests " ]; then
  ok "an image chain plans a second lane, and only the chain is in it"
else
  bad "an image chain planned $nlanes lane(s), off-main: '$off_main'"
  printf '%s\n' "$chain_plan" | sed 's/^/       /'
fi
if [ "$syncers" = 1 ]; then
  ok "mypy, pytest and pip-audit share a lane, so two uv syncs cannot race"
else
  bad "the checks that run uv sync were spread across $syncers lanes"
fi

# And it must still FAIL on something broken -- a gate that passes everything is
# the other way to be useless.
cat > "$FIXTURE/src/broken.py" <<'PY'
import os, sys
def f( x ):
  return eval( x )
PY
if podman run --rm -v "$FIXTURE:/repo:ro,z" -w /repo --entrypoint="" \
     "$IMAGE" sdlc ruff-check >/dev/null 2>&1; then
  bad "ruff-check passed a file with unused imports and bad formatting"
else
  ok "ruff-check fails on broken code"
fi
# A finding ruff could fix is still a finding here -- that is the whole of #48.
# Both tools must report: a lint that stopped at the first failure would hide
# the formatter's findings behind the linter's.
out=$(podman run --rm -v "$FIXTURE:/repo:ro,z" -w /repo --entrypoint="" \
        "$IMAGE" sdlc lint 2>&1) && rc=0 || rc=$?
if [ "$rc" -eq 1 ]; then
  ok "lint fails on code ruff would fix, without fixing it"
else
  bad "lint exited $rc on unused imports and bad formatting, want 1"
fi
case "$out" in
  *F401*"1 file would be reformatted"*)
    ok "lint reports the linter's and the formatter's findings in one run" ;;
  *)
    bad "lint did not report both an F401 and a file to reformat"
    printf '%s\n' "$out" | sed 's/^/       /' | tail -12 ;;
esac
# One fault each, so a lint that judged only one tool, or turned a finding into
# a 2, cannot pass. lint_case <what> <file body> <want> [ruff config line]
lint_case() {
  case_dir=$(mktemp -d)
  printf '[project]\nname = "lint-case"\nversion = "0.0.0"\n\n[tool.ruff]\n%s\n' \
    "${4:-}" > "$case_dir/pyproject.toml"
  printf '%b' "$2" > "$case_dir/x.py"
  before=$(cat "$case_dir/x.py")
  podman run --rm -v "$case_dir:/repo:ro,z" -w /repo --entrypoint="" \
    "$IMAGE" sdlc lint >/dev/null 2>&1 && rc=0 || rc=$?
  after=$(cat "$case_dir/x.py")
  rm -rf "$case_dir"
  if [ "$rc" -eq "$3" ] && [ "$before" = "$after" ]; then
    ok "lint exits $3 on $1"
  else
    bad "lint exited $rc on $1, want $3 and an untouched file"
  fi
}
lint_case "a lint finding alone" 'import os\n' 1
lint_case "a format finding alone" 'x = ( 1 )\n' 1
lint_case "a syntax error (a finding, not a gate that cannot run)" 'def f(:\n' 1
# A repo's fix-only silenced `--no-fix`: exit 0, nothing reported (#49 review).
lint_case "an F821 under fix-only = true" 'def f():\n    return undefined_name\n' 1 \
  'fix-only = true'

if podman run --rm -v "$FIXTURE:/repo:ro,z" -w /repo --entrypoint="" \
     "$IMAGE" sdlc semgrep >/dev/null 2>&1; then
  bad "semgrep passed a file matching its own fixture rule"
else
  ok "semgrep fails on code its rule forbids"
fi

# ---------------------------------- the launcher runs inside a container
#
# bin/sdlc's CPU budget walks the cgroup tree, and inside a container with its
# own cgroup namespace -- podman's default, and the self-hosted GitHub runner --
# /proc/self/cgroup says "0::/". The walk once never terminated there, so every
# command hung before printing a line; a CI job sat for 30 minutes. Run it in a
# container, with shims for the two programs it checks for, and give it 20
# seconds.
LSHIM=$(mktemp -d)
printf '#!/bin/sh\ncase "$*" in *show-toplevel*) pwd ;; esac\nexit 0\n' > "$LSHIM/git"
printf '#!/bin/sh\nexit 0\n' > "$LSHIM/podman"
chmod +x "$LSHIM/git" "$LSHIM/podman"
rc=0
podman run --rm -v "$PWD:/s:ro,z" -v "$LSHIM:/shim:ro,z" -w /s \
  -e PATH=/shim:/usr/bin:/bin registry.fedoraproject.org/fedora:43 \
  timeout 20 sh bin/sdlc --help >/dev/null 2>&1 || rc=$?
rm -rf "$LSHIM"
if [ "$rc" -eq 0 ]; then
  ok "bin/sdlc runs inside a container (cgroup namespace root)"
else
  bad "bin/sdlc inside a container exited $rc (124 means it hung)"
fi

# ------------------------------------------------------- the container stack
#
# A repo with a Containerfile and no pyproject.toml is the container stack and
# nothing else: no ruff, no pytest, and CI builds, smokes and scans the image.
# Planned on the real image because detection reads the mounted tree.
CFIX=$(mktemp -d)
printf 'FROM quay.io/hummingbird/python:3.14\nUSER 65532\n' > "$CFIX/Containerfile"
plan=$(podman run --rm -v "$CFIX:/repo:ro,z" -w /repo --entrypoint="" \
         -e SDLC_HOST_REPO=/host -e SDLC_CACHE_VOLUME=v -e SDLC_IMAGE=gate \
         "$IMAGE" sdlc plan ci 2>&1 | cut -f1 | awk '!s[$0]++' | tr '\n' ' ')
want="containerfile-lint constitution container-build container-smoke container-scan "
if [ "$plan" = "$want" ]; then
  ok "a Containerfile-only repo plans the container stack in ci"
else
  bad "sdlc plan ci on a Containerfile-only repo gave '$plan', want '$want'"
fi
if out=$(podman run --rm -v "$CFIX:/repo:ro,z" -w /repo --entrypoint="" \
           "$IMAGE" sdlc containerfile-lint 2>&1); then
  ok "containerfile-lint runs clean on a read-only fixture Containerfile"
else
  bad "containerfile-lint failed on a clean fixture Containerfile"
  printf '%s\n' "$out" | sed 's/^/       /' | tail -8
fi
lint() { podman run --rm -v "$CFIX:/repo:ro,z" -w /repo --entrypoint="" "$IMAGE" sdlc containerfile-lint 2>&1; }
# The fleet's own conventions: a floating base tag (DL3007), dnf without
# package versions (DL3041), and root as the last USER (DL3002, a warning).
printf 'FROM registry.access.redhat.com/ubi10/ubi-init:latest\nRUN dnf install -y less && dnf clean all\nUSER root\n' \
  > "$CFIX/Containerfile"
rc=0
out=$(lint) || rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q DL3002 \
   && ! printf '%s' "$out" | grep -q 'DL3007\|DL3041'; then
  ok "containerfile-lint passes the fleet's conventions, ignores DL3007/DL3041, still prints DL3002"
else
  bad "containerfile-lint on the fleet's conventions exited $rc"
  printf '%s\n' "$out" | sed 's/^/       /' | tail -6
fi
printf 'FROM quay.io/hummingbird/python:3.14\nWORKDIR app\n' > "$CFIX/Containerfile"
rc=0
lint >/dev/null || rc=$?
if [ "$rc" -eq 1 ]; then
  ok "containerfile-lint fails on an error-level rule, a relative WORKDIR (exit 1)"
else
  bad "containerfile-lint on a relative WORKDIR exited $rc, want 1"
fi
# Exec-form RUN with a quoted script: hadolint's ShellCheck pass raises SC1072
# on it (about 13 mcp-* repos), so the defaults ignore SC1072.
printf 'FROM quay.io/hummingbird/python:3.14\nRUN ["python3", "-c", "import sys; print(sys.version)"]\n' \
  > "$CFIX/Containerfile"
rc=0
out=$(lint) || rc=$?
if [ "$rc" -eq 0 ] && ! printf '%s' "$out" | grep -q SC1072; then
  ok "containerfile-lint ignores SC1072 on an exec-form RUN"
else
  bad "containerfile-lint on an exec-form RUN exited $rc"
  printf '%s\n' "$out" | sed 's/^/       /' | tail -6
fi
# A RUN heredoc hadolint cannot parse: advisory, printed, not a finding.
printf 'FROM quay.io/hummingbird/python:3.14\nRUN python3 - <<'"'"'EOF'"'"'\nprint(1)\nEOF\nRUN true\n' \
  > "$CFIX/Containerfile"
rc=0
out=$(lint) || rc=$?
if [ "$rc" -eq 4 ] && printf '%s' "$out" | grep -q 'ADVISORY: hadolint could not parse'; then
  ok "a Containerfile hadolint cannot parse is advisory (exit 4, printed)"
else
  bad "containerfile-lint on an unparsable Containerfile exited $rc, want 4"
  printf '%s\n' "$out" | sed 's/^/       /' | tail -6
fi
# A repo's own .hadolint.yaml takes the defaults away: here it asks for warnings.
printf 'FROM registry.access.redhat.com/ubi10/ubi-init:latest\n' > "$CFIX/Containerfile"
printf 'failure-threshold: warning\n' > "$CFIX/.hadolint.yaml"
rc=0
out=$(lint) || rc=$?
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q DL3007; then
  ok "a repo's .hadolint.yaml overrides the stack defaults (DL3007 fails at warning)"
else
  bad "containerfile-lint with a .hadolint.yaml exited $rc, want 1 on DL3007"
fi
rm -rf "$CFIX"

# ------------------------------------------------------ the docs stack, in here
#
# A repo with Markdown and nothing else is the docs stack. Both checks run on a
# READ-ONLY mount: the generated markdownlint and lychee configs have to land
# under /tmp, and lychee has to do its work offline. The end-to-end run through
# the launcher is tests/test_docs_stack.sh.
DFIX=$(mktemp -d)
mkdir "$DFIX/docs"
printf '# Title\n\n## Usage\n\nSee [the guide](docs/guide.md#setup).\n' > "$DFIX/README.md"
printf '# Guide\n\n## Setup\n\nBack to the [top](../README.md#usage).\n' > "$DFIX/docs/guide.md"
docs() { podman run --rm --network=none -v "$DFIX:/repo:ro,z" -w /repo --entrypoint="" "$IMAGE" sdlc "$1" 2>&1; }
plan=$(podman run --rm -v "$DFIX:/repo:ro,z" -w /repo --entrypoint="" \
         -e SDLC_HOST_REPO=/host -e SDLC_CACHE_VOLUME=v -e SDLC_IMAGE=gate \
         "$IMAGE" sdlc plan ci 2>&1 | cut -f1 | awk '!s[$0]++' | tr '\n' ' ')
want="markdown-lint link-check constitution "
if [ "$plan" = "$want" ]; then
  ok "a Markdown-only repo plans the docs stack in ci"
else
  bad "sdlc plan ci on a Markdown-only repo gave '$plan', want '$want'"
fi
for check in markdown-lint link-check; do
  if out=$(docs "$check"); then
    ok "$check runs clean on a read-only, network-less fixture"
  else
    bad "$check failed on a clean fixture"
    printf '%s\n' "$out" | sed 's/^/       /' | tail -8
  fi
done
printf '\nSee [gone](docs/missing.md) and [bad](docs/guide.md#nowhere).\n' >> "$DFIX/README.md"
rc=0
out=$(docs link-check) || rc=$?
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'missing.md' && printf '%s' "$out" | grep -q 'nowhere'; then
  ok "link-check fails on a missing file and a missing #fragment (exit 1)"
else
  bad "link-check on broken links exited $rc, want 1 naming both"
  printf '%s\n' "$out" | sed 's/^/       /' | tail -8
fi
rm -rf "$DFIX"

# --------------------------------------- `sdlc versions` matches the build pins
#
# The version table is the image's central promise: a consuming repo pins this
# image and gets those tools. Probing binaries one by one, as above, does not
# assert that `sdlc versions` -- the thing a human and a CI job actually read --
# reports them.

versions_out=$(inside sdlc versions 2>&1)
for row in \
  "ruff:SDLC_RUFF_VERSION" \
  "semgrep:SDLC_SEMGREP_VERSION" \
  "pip-audit:SDLC_PIP_AUDIT_VERSION" \
  "gourmand:SDLC_GOURMAND_VERSION" \
  "gatehouse:SDLC_GATEHOUSE_VERSION" \
  "codeql:SDLC_CODEQL_BUNDLE" \
  "uv:SDLC_UV_VERSION" \
  "hadolint:SDLC_HADOLINT_VERSION" \
  "grype:SDLC_GRYPE_VERSION" \
  "node:SDLC_NODE_VERSION" \
  "markdownlint-cli2:SDLC_MARKDOWNLINT_CLI2_VERSION" \
  "lychee:SDLC_LYCHEE_VERSION" \
  "docker:SDLC_DOCKER_VERSION" \
  "cosign:SDLC_COSIGN_VERSION" \
  "syft:SDLC_SYFT_VERSION" \
  "zizmor:SDLC_ZIZMOR_VERSION" \
  "claude-code:SDLC_CLAUDE_CODE_VERSION" \
  "renovate:SDLC_RENOVATE_VERSION" \
  "cryptography:SDLC_CRYPTOGRAPHY_VERSION" \
  "pyjwt:SDLC_PYJWT_VERSION" \
  "gh:SDLC_GH_VERSION" \
  "ashigaru:SDLC_ASHIGARU_VERSION" \
  "constitution:SDLC_CONSTITUTION_VERSION"
do
  tool=${row%%:*}
  var=${row#*:}
  pinned=$(inside sh -c "printf %s \"\$$var\"")
  if [ -z "$pinned" ] || [ "$pinned" = unknown ]; then
    bad "$tool has no build-time pin in the image environment ($var)"
    continue
  fi
  reported=$(printf '%s\n' "$versions_out" | awk -v t="$tool" '$1==t {print $2}')
  if [ "$reported" = "$pinned" ]; then
    ok "sdlc versions reports $tool $pinned"
  else
    bad "sdlc versions reports $tool '$reported' but the image pinned '$pinned'"
  fi
done

# ------------------------------------------- the launcher the image carries

# A repo with no `rev:` runs the launcher this image prints, so the image must
# carry THIS commit's bin/sdlc and write the ref it was asked for into it.
printf '\n== the launcher\n\n'
CLAUNCH=$(mktemp)
if podman run --rm --network=none "$IMAGE" launcher quay.io/crunchtools/sdlc:latest > "$CLAUNCH" \
   && sed "s|^LAUNCHED_FROM=\$|LAUNCHED_FROM='quay.io/crunchtools/sdlc:latest'|" bin/sdlc | cmp -s - "$CLAUNCH"; then
  ok "sdlc launcher prints this commit's bin/sdlc, with the ref written in"
else
  bad "sdlc launcher does not print this commit's bin/sdlc"
fi
rm -f "$CLAUNCH"
rc=0
podman run --rm --network=none "$IMAGE" launcher "x'; id; '" > /dev/null 2>&1 || rc=$?
if [ "$rc" -eq 2 ]; then
  ok "sdlc launcher refuses a ref that is not an image reference (exit 2)"
else
  bad "sdlc launcher took a ref with quotes in it (exit $rc)"
fi
if inside cat /usr/share/sdlc/bootstrap | cmp -s - bin/sdlc-bootstrap; then
  ok "the image carries this commit's bin/sdlc-bootstrap"
else
  bad "/usr/share/sdlc/bootstrap is not this commit's bin/sdlc-bootstrap"
fi

# The gate's confinement on this host: its containers can still signal their own
# processes, rootless and rootful (crunchtools/sdlc#181).
printf '\n'
if tests/test_apparmor.sh; then
  ok "gate containers signal their own processes on this host"
else
  bad "tests/test_apparmor.sh failed: a gate container cannot signal its own processes here"
fi
printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf 'contract test PASSED\n'
  exit 0
fi
printf 'contract test FAILED: %s check(s)\n' "$FAILED"
exit 1
