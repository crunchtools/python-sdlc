#!/bin/sh
# scripts/revision-of.sh, against a fake skopeo: a readable tag prints its
# revision, "manifest unknown" prints nothing and succeeds, and every other
# failure -- auth, network -- exits non-zero, so the promote guard fails closed.
#
# Usage: tests/test_revision_of.sh

set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
FAILED=0
ok()  { printf 'ok   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }

BIN=$(mktemp -d)
trap 'rm -rf "$BIN"' EXIT
cat > "$BIN/skopeo" <<'SHIM'
#!/bin/sh
case "$FAKE_SKOPEO" in
  ok)       printf 'abc123\n' ;;
  missing)  echo 'Error: reading manifest main in quay.io/x/y: manifest unknown' >&2; exit 1 ;;
  auth)     echo 'Error: unauthorized: authentication required' >&2; exit 1 ;;
  network)  echo 'Error: pinging container registry quay.io: dial tcp: i/o timeout' >&2; exit 1 ;;
esac
SHIM
chmod +x "$BIN/skopeo"

run() { FAKE_SKOPEO=$1 PATH="$BIN:$PATH" scripts/revision-of.sh quay.io/x/y:main; }

rc=0; out=$(run ok 2>/dev/null) || rc=$?
if [ "$rc" -eq 0 ] && [ "$out" = abc123 ]; then ok "a readable tag prints its revision"
else bad "readable tag: rc=$rc out='$out'"; fi

rc=0; out=$(run missing 2>/dev/null) || rc=$?
if [ "$rc" -eq 0 ] && [ -z "$out" ]; then ok "manifest unknown is an absent tag: nothing, exit 0"
else bad "missing tag: rc=$rc out='$out'"; fi

for failure in auth network; do
  rc=0; run "$failure" >/dev/null 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then ok "a $failure failure stops the guard (exit $rc)"
  else bad "a $failure failure was read as an absent tag"; fi
done

printf '\n'
[ "$FAILED" -eq 0 ] && { printf 'revision-of PASSED\n'; exit 0; }
printf 'revision-of FAILED: %s check(s)\n' "$FAILED"; exit 1
