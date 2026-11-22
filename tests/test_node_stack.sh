#!/bin/sh
# The node stack end to end, through the real launcher and a real podman: a
# package.json-only repo's `sdlc ci` installs from its lockfile into the cache
# volume, lints, tests and audits it. Then the three ways it must NOT pass: a
# failing test is a finding (1), and a missing lockfile or a missing script is
# CANNOT_RUN (2) rather than a pass that ran nothing. Planned argv, settings
# validation and the audit classifier are unit-tested in test_plan.py; this is
# the part only npm, the registry and podman can prove.
#
# Needs registry.npmjs.org: the fixture's one dependency is installed from it
# and `npm audit` asks it about that dependency.
#
# Usage: tests/test_node_stack.sh <image-ref>

set -eu

IMAGE=${1:?usage: tests/test_node_stack.sh <image-ref>}
HERE=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
FAILED=0
ok()  { printf 'ok   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# The project name is the directory's name, and it keys the workspace in the
# cache volume, so a per-run suffix keeps a rerun from finding an old install.
RUN_ID=$$

fixture() {            # name test-body: a package with lint and test scripts and one dependency
  _d="$WORK/$1-$RUN_ID"
  mkdir -p "$_d"
  cat > "$_d/package.json" <<'EOF'
{
  "name": "fixture",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "lint": "node --check index.js",
    "test": "node --test"
  }
}
EOF
  printf 'console.log(require("is-number")(5));\n' > "$_d/index.js"
  printf "const test = require('node:test');\nconst assert = require('node:assert');\ntest('fixture', () => { %s });\n" "$2" \
    > "$_d/index.test.js"
  # The lockfile comes from npm inside the image, so this also proves npm runs
  # there, and the fixture never depends on a host npm.
  podman run --rm --entrypoint="" -e HOME=/tmp -v "$_d:/w:rw,z" -w /w "$IMAGE" \
    npm install --package-lock-only --no-audit --no-fund is-number@7.0.0 >/dev/null
  # The fixture has no constitution manifest to validate.
  printf '[tool.sdlc.push]\nskip = ["constitution"]\n' > "$_d/sdlc.toml"
  # Whatever node_modules the repo's own npm left must not be there to be found.
  printf 'node_modules/\n' > "$_d/.gitignore"
  git -C "$_d" init -q
  git -C "$_d" add -A
  git -C "$_d" -c user.email=t@t -c user.name=t commit -qm fixture
}

sdlc_in() {            # name args...: the launcher against a fixture, output to $WORK/out
  _d="$WORK/$1-$RUN_ID"; shift
  rc=0
  (cd "$_d" && SDLC_IMAGE=$IMAGE "$HERE/bin/sdlc" "$@") > "$WORK/out" 2>&1 || rc=$?
}

show() { tail -25 "$WORK/out" | sed 's/^/       /'; }

fixture passes 'assert.strictEqual(1, 1);'
sdlc_in passes ci
if [ "$rc" -eq 0 ] \
   && grep -q 'npm-lint passed' "$WORK/out" \
   && grep -q 'npm-test passed' "$WORK/out" \
   && grep -q 'npm-audit passed' "$WORK/out"; then
  ok "sdlc ci installs, lints, tests and audits a node repo (exit 0)"
else
  bad "sdlc ci on a passing node repo exited $rc"
  show
fi

# The install is the expensive part and it is keyed on the lockfile: a second
# run must not repeat it. Nothing may be written into the developer's tree.
sdlc_in passes ci
if [ "$rc" -eq 0 ] && ! grep -q 'npm ci' "$WORK/out"; then
  ok "a second run reuses the install (no npm ci)"
else
  bad "the second run exited $rc or ran npm ci again"
  show
fi
if [ ! -e "$WORK/passes-$RUN_ID/node_modules" ]; then
  ok "node_modules is never written into the repo's tree"
else
  bad "node_modules appeared in the repo's tree"
fi

fixture fails 'assert.strictEqual(1, 2);'
sdlc_in fails npm-test
if [ "$rc" -eq 1 ] && grep -q 'npm-test FAILED after' "$WORK/out"; then
  ok "a failing test is a finding (exit 1)"
else
  bad "npm-test on a failing test exited $rc, want 1"
  show
fi

# Arguments after `--` reach the test script, so one test can be re-run alone.
sdlc_in fails npm-test -- --test-name-pattern=nothing-matches-this
if [ "$rc" -eq 0 ]; then
  ok "words after -- reach the test script"
else
  bad "npm-test with a --test-name-pattern that matches nothing exited $rc, want 0"
  show
fi

# No lockfile: npm install would resolve whatever is newest today, so it is a 2.
fixture nolock 'assert.ok(true);'
rm "$WORK/nolock-$RUN_ID/package-lock.json"
sdlc_in nolock npm-test
if [ "$rc" -eq 2 ] && grep -q 'npm ci needs a lockfile' "$WORK/out"; then
  ok "a missing package-lock.json is CANNOT_RUN (exit 2)"
else
  bad "npm-test without a lockfile exited $rc, want 2"
  show
fi

# A repo with no lint script has nothing for npm-lint to run: 2, not a pass.
fixture noscript 'assert.ok(true);'
printf '{"name":"fixture","version":"1.0.0","private":true,"dependencies":{"is-number":"7.0.0"}}\n' \
  > "$WORK/noscript-$RUN_ID/package.json"
sdlc_in noscript npm-lint
if [ "$rc" -eq 2 ] && grep -q "defines a 'lint' script" "$WORK/out"; then
  ok "no lint script is CANNOT_RUN (exit 2), not a pass that ran nothing"
else
  bad "npm-lint with no lint script exited $rc, want 2"
  show
fi

printf '\n'
[ "$FAILED" -eq 0 ] && { printf 'node stack PASSED\n'; exit 0; }
printf 'node stack FAILED: %s check(s)\n' "$FAILED"; exit 1
