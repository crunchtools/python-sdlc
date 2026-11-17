#!/bin/sh
# The container stack end to end, through the real launcher and a real podman:
# a Containerfile-only repo's `sdlc ci` builds the image, smoke-runs it and
# scans it with grype through `podman run --mount type=image`. Then an image
# that dies on start must fail the smoke run. Planned argv is unit-tested in
# test_plan.py; this is the part only podman can prove.
#
# Usage: tests/test_container_stack.sh <image-ref>

set -eu

IMAGE=${1:?usage: tests/test_container_stack.sh <image-ref>}
HERE=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
FAILED=0
ok()  { printf 'ok   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

fixture() {           # name entrypoint-json
  mkdir -p "$WORK/$1"
  printf 'FROM quay.io/hummingbird/python:3.14\nUSER 65532\nENTRYPOINT %s\n' "$2" \
    > "$WORK/$1/Containerfile"
  # The fixture has no constitution manifest to validate.
  printf '[tool.sdlc.push]\nskip = ["constitution"]\n' > "$WORK/$1/sdlc.toml"
  git -C "$WORK/$1" init -q
  git -C "$WORK/$1" add -A
  git -C "$WORK/$1" -c user.email=t@t -c user.name=t commit -qm fixture
}

fixture serves '["python3", "-m", "http.server", "8080"]'
rc=0
(cd "$WORK/serves" && SDLC_IMAGE=$IMAGE "$HERE/bin/sdlc" ci) > "$WORK/serves.out" 2>&1 || rc=$?
if [ "$rc" -eq 0 ] \
   && grep -q 'container-smoke passed' "$WORK/serves.out" \
   && grep -q 'container-scan passed' "$WORK/serves.out"; then
  ok "sdlc ci builds, smokes and scans a service image (exit 0)"
else
  bad "sdlc ci on a service image exited $rc"
  tail -30 "$WORK/serves.out" | sed 's/^/       /'
fi

fixture crashes '["python3", "-c", "import missing_module"]'
# The build has to succeed, and the failure has to be the entrypoint's: a
# missing image is also a non-zero smoke run, and must not pass for this one.
rc=0
(cd "$WORK/crashes" && SDLC_IMAGE=$IMAGE "$HERE/bin/sdlc" container-build) > "$WORK/build.out" 2>&1 || rc=$?
if [ "$rc" -ne 0 ]; then
  bad "the crashing fixture did not build (exit $rc), so its smoke run proves nothing"
  tail -10 "$WORK/build.out" | sed 's/^/       /'
else
  rc=0
  (cd "$WORK/crashes" && SDLC_IMAGE=$IMAGE "$HERE/bin/sdlc" container-smoke) > "$WORK/crashes.out" 2>&1 || rc=$?
  if [ "$rc" -eq 1 ] \
     && grep -q "No module named 'missing_module'" "$WORK/crashes.out" \
     && grep -q 'the container exited 1 within' "$WORK/crashes.out"; then
    ok "an image that dies on start fails container-smoke (exit 1, its own error)"
  else
    bad "container-smoke on a crashing image exited $rc, want 1 from the entrypoint"
    tail -10 "$WORK/crashes.out" | sed 's/^/       /'
  fi
fi

# An entrypoint that does not exist: podman's own 127. That is the image's
# defect -- exactly what a smoke run is for -- so a finding, not a 2.
fixture noentry '["/no/such/entrypoint"]'
rc=0
(cd "$WORK/noentry" && SDLC_IMAGE=$IMAGE "$HERE/bin/sdlc" container-build) > "$WORK/build.out" 2>&1 || rc=$?
if [ "$rc" -ne 0 ]; then
  bad "the missing-entrypoint fixture did not build (exit $rc)"
else
  rc=0
  (cd "$WORK/noentry" && SDLC_IMAGE=$IMAGE "$HERE/bin/sdlc" container-smoke) > "$WORK/noentry.out" 2>&1 || rc=$?
  if [ "$rc" -eq 1 ] && grep -q 'could not start its entrypoint' "$WORK/noentry.out"; then
    ok "a missing entrypoint fails container-smoke (exit 1)"
  else
    bad "container-smoke on a missing entrypoint exited $rc, want 1"
    tail -10 "$WORK/noentry.out" | sed 's/^/       /'
  fi
fi

# An image that declares a VOLUME gets an anonymous volume per run. The smoke
# run must take it away with the container, or every run leaks one: a fleet
# measurement once filled a seat's disk with 13 GB of them.
fixture volume '["python3", "-m", "http.server", "8080"]'
printf 'VOLUME /data\n' >> "$WORK/volume/Containerfile"
git -C "$WORK/volume" -c user.email=t@t -c user.name=t commit -qam volume
podman volume ls -q | sort > "$WORK/volumes.before"
rc=0
(cd "$WORK/volume" && SDLC_IMAGE=$IMAGE "$HERE/bin/sdlc" container-build \
   && SDLC_IMAGE=$IMAGE "$HERE/bin/sdlc" container-smoke) > "$WORK/volume.out" 2>&1 || rc=$?
podman volume ls -q | sort > "$WORK/volumes.after"
leaked=$(comm -13 "$WORK/volumes.before" "$WORK/volumes.after" | wc -l | tr -d ' ')
if [ "$rc" -eq 0 ] && [ "$leaked" -eq 0 ]; then
  ok "a smoke run of an image with a VOLUME leaves no volume behind"
else
  bad "smoke run of a VOLUME image exited $rc and left $leaked volume(s)"
  tail -10 "$WORK/volume.out" | sed 's/^/       /'
fi

printf '\n'
[ "$FAILED" -eq 0 ] && { printf 'container stack PASSED\n'; exit 0; }
printf 'container stack FAILED: %s check(s)\n' "$FAILED"; exit 1
