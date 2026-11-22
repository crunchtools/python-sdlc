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
# Each fixture's origin is https://github.com/sdlc-test-<run>/<name>, so its
# workspace volume is sdlc-node-sdlc-test-<run>-<name>: unique to this run, and
# every one of them removed on the way out.
RUN_ID=$$
VOLS="sdlc-node-sdlc-test-$RUN_ID-"
# shellcheck disable=SC2329  # the EXIT trap below calls it
cleanup() {
  rm -rf "$WORK"
  podman volume ls -q | grep "^$VOLS" | xargs -r podman volume rm -f >/dev/null 2>&1 || :
}
trap cleanup EXIT

fixture() {            # name test-body [dep]: a package with lint and test scripts
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
    npm install --package-lock-only --no-audit --no-fund is-number@7.0.0 ${3:+"$3"} >/dev/null
  # The fixture has no constitution manifest to validate.
  printf '[tool.sdlc.push]\nskip = ["constitution"]\n' > "$_d/sdlc.toml"
  # Whatever node_modules the repo's own npm left must not be there to be found.
  printf 'node_modules/\n' > "$_d/.gitignore"
  git -C "$_d" init -q
  git -C "$_d" remote add origin "https://github.com/sdlc-test-$RUN_ID/$1"
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

# A lint script that fails is a finding.
fixture badlint 'assert.ok(true);'
printf 'const = ;\n' >> "$WORK/badlint-$RUN_ID/index.js"
git -C "$WORK/badlint-$RUN_ID" -c user.email=t@t -c user.name=t commit -qam broken
sdlc_in badlint npm-lint
if [ "$rc" -eq 1 ] && grep -q 'npm-lint FAILED after' "$WORK/out"; then
  ok "a failing lint script is a finding (exit 1)"
else
  bad "npm-lint on a syntax error exited $rc, want 1"
  show
fi

# A dependency with a known critical advisory (minimist 1.2.5, prototype
# pollution, fixed in 1.2.6) is an audit finding, named.
fixture vulnerable 'assert.ok(true);' minimist@1.2.5
sdlc_in vulnerable npm-audit
if [ "$rc" -eq 1 ] && grep -q 'minimist' "$WORK/out" && grep -q 'npm-audit FAILED after' "$WORK/out"; then
  ok "a vulnerable dependency fails npm-audit (exit 1, named)"
else
  bad "npm-audit on minimist 1.2.5 exited $rc, want 1 naming minimist"
  show
fi

# package.json only in a subdirectory is still a node repo. It must not fall
# through to the docs stack and pass with its code never judged.
fixture nested 'assert.ok(true);'
_n="$WORK/nested-$RUN_ID"
mkdir "$_n/web"
git -C "$_n" mv package.json package-lock.json index.js index.test.js web/
git -C "$_n" -c user.email=t@t -c user.name=t commit -qm nested
sdlc_in nested ci
if [ "$rc" -eq 2 ] && grep -q 'no package.json at the repo root, but there is in web' "$WORK/out"; then
  ok "a package.json only in a subdirectory is CANNOT_RUN until packages names it (exit 2)"
else
  bad "sdlc ci with only web/package.json exited $rc, want 2 naming web"
  show
fi
printf '\n[tool.sdlc.push.npm-test]\npackages = ["web"]\n' >> "$_n/sdlc.toml"
git -C "$_n" -c user.email=t@t -c user.name=t commit -qam packages
sdlc_in nested npm-test
if [ "$rc" -eq 0 ] && grep -q 'npm run test (web)' "$WORK/out"; then
  ok "with packages = [\"web\"] the nested package is installed and tested"
else
  bad "npm-test with packages = [\"web\"] exited $rc, want 0"
  show
fi

# One repo's code cannot reach another's workspace. The victim installs first;
# then the attacker's postinstall -- run by `npm ci`, as any dependency's
# would be -- tries every place a shared cache would put the victim's tree. The
# shared cache volume is not mounted in a node step at all, and the victim's
# volume is not the attacker's.
fixture victim 'assert.ok(true);'
sdlc_in victim npm-test
fixture attacker 'assert.ok(true);'
_a="$WORK/attacker-$RUN_ID"
{
  printf 'const fs = require("fs");\n'
  printf 'for (const d of ["/cache", "/cache/node/victim-%s/tree", "/cache/node"]) {\n' "$RUN_ID"
  printf '  if (fs.existsSync("/cache/npm")) console.log("SHARED-CACHE-VISIBLE");\n'
  printf '  try { fs.mkdirSync(d, { recursive: true }); fs.writeFileSync(d + "/POISON", "x"); }\n'
  printf '  catch (e) { console.log("refused " + d); }\n'
  printf '}\n'
} > "$_a/poison.js"
sed 's/"test": "node --test"/"test": "node --test",\n    "postinstall": "node poison.js"/' \
  "$_a/package.json" > "$WORK/p" && mv "$WORK/p" "$_a/package.json"
git -C "$_a" add -A
git -C "$_a" -c user.email=t@t -c user.name=t commit -qm postinstall
sdlc_in attacker npm-test
_seen=$(podman run --rm --entrypoint="" -v "${VOLS}victim:/v:ro" "$IMAGE" find /v -name POISON)
if [ "$rc" -eq 0 ] && grep -q 'npm ci' "$WORK/out" && ! grep -q 'SHARED-CACHE-VISIBLE' "$WORK/out" \
   && [ -z "$_seen" ] && podman volume exists "${VOLS}attacker"; then
  ok "one repo's install scripts cannot write another repo's workspace"
else
  bad "isolation: exit $rc, victim volume holds '${_seen}'"
  show
fi

printf '\n'
[ "$FAILED" -eq 0 ] && { printf 'node stack PASSED\n'; exit 0; }
printf 'node stack FAILED: %s check(s)\n' "$FAILED"; exit 1
