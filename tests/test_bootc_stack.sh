#!/bin/sh
# The bootc stack end to end, through the real launcher and a real podman: a
# Containerfile whose FROM is a bootc image is built, linted with `bootc
# container lint` INSIDE the built image under the launcher's --cap-drop=all,
# and scanned; it is never smoke-run, because its PID 1 is systemd. Then an
# image bootc's own lint rejects must fail bootc-lint with exit 1. Planned argv
# is unit-tested in test_plan.py; this is the part only a bootc image can prove.
#
# The base is public and needs no login. It is ~1.6 GB and grype scans all of
# it, so this is the slow test: budget several minutes.
#
# Usage: tests/test_bootc_stack.sh <image-ref>

set -eu

IMAGE=${1:?usage: tests/test_bootc_stack.sh <image-ref>}
BASE=${BOOTC_BASE:-quay.io/centos-bootc/centos-bootc:stream10}
HERE=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
FAILED=0
ok()  { printf 'ok   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

fixture() {           # name run-line
  mkdir -p "$WORK/$1"
  printf 'FROM %s\nRUN %s\n' "$BASE" "$2" > "$WORK/$1/Containerfile"
  # The fixture has no constitution manifest to validate.
  printf '[tool.sdlc.push]\nskip = ["constitution"]\n' > "$WORK/$1/sdlc.toml"
  git -C "$WORK/$1" init -q
  git -C "$WORK/$1" add -A
  git -C "$WORK/$1" -c user.email=t@t -c user.name=t commit -qm fixture
}

# A stock bootc base itself warns (var-tmpfiles, on a file its own build left
# in /var). Warnings are not failures by default, which is what lets this pass.
#
# The scan cannot be left at its defaults here. The public base is a whole OS
# and grype's feed moves daily, so it routinely carries a fixable Critical
# (centos-bootc:stream10 did on the day this was written) that this repo can do
# nothing about, and a test that goes red on a feed update tests nothing. The
# fixture's own .grype.yaml -- the file a repo uses for exactly this -- accepts
# every fix-state=fixed finding. The scan still mounts the image, runs grype
# over all of it and reads that file, which is the plumbing under test; what a
# Critical does to the verdict is test_container_stack.sh's and test_plan.py's.
fixture good 'echo built > /usr/share/sdlc-fixture'
printf 'ignore:\n  - fix-state: fixed\n' > "$WORK/good/.grype.yaml"
git -C "$WORK/good" add -A
git -C "$WORK/good" -c user.email=t@t -c user.name=t commit -qm grype
rc=0
(cd "$WORK/good" && SDLC_IMAGE=$IMAGE "$HERE/bin/sdlc" ci) > "$WORK/good.out" 2>&1 || rc=$?
if [ "$rc" -eq 0 ] \
   && grep -q 'container-build passed' "$WORK/good.out" \
   && grep -q 'bootc-lint passed' "$WORK/good.out" \
   && grep -q 'container-scan passed' "$WORK/good.out" \
   && ! grep -q 'container-smoke' "$WORK/good.out"; then
  ok "sdlc ci builds, bootc-lints and scans a bootc image, with no smoke run (exit 0)"
else
  bad "sdlc ci on a bootc image exited $rc"
  tail -30 "$WORK/good.out" | sed 's/^/       /'
fi

# A physical /var/run is a fatal lint ("this is always a bug"). The build has
# to succeed first: a missing image is also a non-zero lint run, and must not
# pass for this one.
fixture bad 'rm -rf /var/run && mkdir -p /var/run'
rc=0
(cd "$WORK/bad" && SDLC_IMAGE=$IMAGE "$HERE/bin/sdlc" container-build) > "$WORK/build.out" 2>&1 || rc=$?
if [ "$rc" -ne 0 ]; then
  bad "the failing fixture did not build (exit $rc), so its lint proves nothing"
  tail -10 "$WORK/build.out" | sed 's/^/       /'
else
  rc=0
  (cd "$WORK/bad" && SDLC_IMAGE=$IMAGE "$HERE/bin/sdlc" bootc-lint) > "$WORK/bad.out" 2>&1 || rc=$?
  if [ "$rc" -eq 1 ] && grep -q 'Failed lint: var-run' "$WORK/bad.out"; then
    ok "an image bootc rejects fails bootc-lint (exit 1, bootc's own message)"
  else
    bad "bootc-lint on a bad image exited $rc, want 1 with the var-run lint"
    tail -10 "$WORK/bad.out" | sed 's/^/       /'
  fi
  # The same image, with that lint named in `skip`, passes: the setting reaches
  # bootc, and the failure above was that lint's and nothing else's.
  printf '[tool.sdlc.ci.bootc-lint]\nskip = ["var-run"]\n' >> "$WORK/bad/sdlc.toml"
  rc=0
  (cd "$WORK/bad" && SDLC_IMAGE=$IMAGE "$HERE/bin/sdlc" bootc-lint) > "$WORK/skip.out" 2>&1 || rc=$?
  if [ "$rc" -eq 0 ]; then
    ok "skip = [\"var-run\"] reaches bootc and the same image passes"
  else
    bad "bootc-lint with the failing lint skipped exited $rc, want 0"
    tail -10 "$WORK/skip.out" | sed 's/^/       /'
  fi
  # And fatal_warnings turns the base's own warning into a failure.
  printf 'fatal_warnings = true\n' >> "$WORK/bad/sdlc.toml"
  rc=0
  (cd "$WORK/bad" && SDLC_IMAGE=$IMAGE "$HERE/bin/sdlc" bootc-lint) > "$WORK/fatal.out" 2>&1 || rc=$?
  if [ "$rc" -eq 1 ] && grep -q 'Lint warning' "$WORK/fatal.out"; then
    ok "fatal_warnings = true makes a warning fail bootc-lint (exit 1)"
  else
    bad "bootc-lint with fatal_warnings exited $rc, want 1 on a warning"
    tail -10 "$WORK/fatal.out" | sed 's/^/       /'
  fi
fi

printf '\n'
[ "$FAILED" -eq 0 ] && { printf 'bootc stack PASSED\n'; exit 0; }
printf 'bootc stack FAILED: %s check(s)\n' "$FAILED"; exit 1
