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
  "git:git --version"
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

# codeql's bundle name is codeql-bundle-vX.Y.Z and the CLI reports X.Y.Z.
_bundle=$(inside sh -c 'printf %s "$SDLC_CODEQL_BUNDLE"')
check_version codeql "${_bundle#codeql-bundle-v}" \
                     "$(inside sh -c 'codeql version --format=terse' 2>&1)"

# Gourmand is EXEMPT from this check on purpose: its image tag and its binary
# disagree upstream (tag 1.0.0 ships a binary reporting 0.16.5). `sdlc versions`
# shows both columns so the gap is visible; asserting they match here would fail
# the build for someone else's version mismatch.
note "gourmand tag/binary mismatch is reported by 'sdlc versions', not asserted here"

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

[tool.sdlc]
lint_paths = ["src"]
scan_paths = ["src"]
semgrep_config = ".semgrep"
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
if podman run --rm -v "$FIXTURE:/repo:ro,z" -w /repo --entrypoint="" \
     "$IMAGE" sdlc semgrep >/dev/null 2>&1; then
  bad "semgrep passed a file matching its own fixture rule"
else
  ok "semgrep fails on code its rule forbids"
fi

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
  "uv:SDLC_UV_VERSION"
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

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf 'contract test PASSED\n'
  exit 0
fi
printf 'contract test FAILED: %s check(s)\n' "$FAILED"
exit 1
