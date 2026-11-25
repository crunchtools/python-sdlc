#!/bin/sh
# scripts/hotfix.sh, against a fake self.sh and a fake podman in a scratch git
# tree: the order is image -> tag (podman run of sdlc_jobs._hotfix) -> sign, the
# App's two secrets reach the container by name only (never on argv), a missing
# secret or a failed tag stops everything before anything is signed or moved,
# and a dirty tree is refused first.
#
# Usage: tests/test_hotfix.sh

set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
FAILED=0
ok()  { printf 'ok   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
TREE=$WORK/tree BIN=$WORK/bin LOG=$WORK/log
mkdir -p "$TREE/scripts" "$BIN"
cp scripts/hotfix.sh "$TREE/scripts/hotfix.sh"
printf 'version = "1.2.3"\n' > "$TREE/pyproject.toml"
cat > "$TREE/scripts/self.sh" <<'SHIM'
#!/bin/sh
echo "self GITHUB_JOB=$GITHUB_JOB sdlckey=${SDLC_KEY:+set}${SDLC_KEY:-empty} appkey=${ASHIGARU_APP_KEY+set}" >> "$LOG"
[ "$GITHUB_JOB" = image ] &&
  echo 'result={"digest":"sha256:abc","version":"1.2.3","boot":""}' >> "$GITHUB_OUTPUT"
exit 0
SHIM
cat > "$BIN/podman" <<'SHIM'
#!/bin/sh
echo "podman $*" >> "$LOG"
echo "podman-env id=$ASHIGARU_CLIENT_ID appkey=$ASHIGARU_APP_KEY" >> "$LOG"
[ ! -e "$FAKE_PODMAN_FAIL" ] || { echo 'tag refused' >&2; exit 1; }
SHIM
chmod +x "$TREE/scripts/self.sh" "$BIN/podman"
git -C "$TREE" init -q
git -C "$TREE" add -A
git -C "$TREE" -c user.name=t -c user.email=t@t commit -qm fake
SHA=$(git -C "$TREE" rev-parse HEAD)

ALL='"SDLC_KEY":"k","SDLC_KEY_PASSWORD":"p","SDLC_QUAY_USERNAME":"u","SDLC_QUAY_PASSWORD":"q"'
APP='"ASHIGARU_CLIENT_ID":"cid-VALUE","ASHIGARU_APP_KEY":"appkey-VALUE"'
FULL="{$ALL,$APP}"

# run <json>: leaves rc, $WORK/err and $LOG for the assertions.
run() {
  : > "$LOG"
  rc=0
  printf '%s' "$1" | LOG=$LOG FAKE_PODMAN_FAIL=${FAKE_PODMAN_FAIL:-$WORK/none} PATH="$BIN:$PATH" \
    sh "$TREE/scripts/hotfix.sh" > "$WORK/out" 2> "$WORK/err" || rc=$?
}
has() { grep -qF -- "$1" "$LOG"; }

printf '== scripts/hotfix.sh\n\n'

run "$FULL"
order=$(sed -n 's/^self GITHUB_JOB=\([a-z]*\).*/self-\1/p; s/^podman run.*/podman/p' "$LOG" | tr '\n' ' ')
if [ "$rc" -eq 0 ] && [ "$order" = 'self-image podman self-sign ' ]; then ok "image, then the tag, then sign (exit 0)"
else bad "order: rc=$rc order='$order'"; fi

run_line=$(grep '^podman run' "$LOG" || true)
for want in "-e ASHIGARU_CLIENT_ID -e ASHIGARU_APP_KEY" "localhost/sdlc:$SHA" \
            "-m sdlc_jobs._hotfix crunchtools/sdlc 1.2.3 $SHA"; do
  case $run_line in *"$want"*) ok "tag run carries: $want" ;; *) bad "tag run lacks '$want': $run_line" ;; esac
done
case $run_line in
  *VALUE*|*"-e SDLC_KEY"*) bad "secrets or SDLC_KEY on the tag run's argv: $run_line" ;;
  *) ok "no secret values and no SDLC_KEY on the tag run's argv" ;;
esac
if has 'podman-env id=cid-VALUE appkey=appkey-VALUE'; then ok "the container gets the App's two values by name"
else bad "podman env: $(grep '^podman-env' "$LOG")"; fi
if has 'self GITHUB_JOB=image sdlckey=empty' && has 'self GITHUB_JOB=sign sdlckey=set'; then ok "image runs with an empty SDLC_KEY, sign with the real one"
else bad "SDLC_KEY leaked to self.sh: $(grep '^self' "$LOG")"; fi

for name in ASHIGARU_APP_KEY ASHIGARU_CLIENT_ID; do
  json=$(printf '%s' "$FULL" | jq -c --arg n "$name" 'del(.[$n])')
  run "$json"
  if [ "$rc" -ne 0 ] && grep -qF "$name" "$WORK/err" && [ ! -s "$LOG" ]; then ok "missing $name: refused, nothing ran"
  else bad "missing $name: rc=$rc err='$(cat "$WORK/err")' log='$(cat "$LOG")'"; fi
done

: > "$WORK/failflag"
FAKE_PODMAN_FAIL=$WORK/failflag run "$FULL"
if [ "$rc" -ne 0 ] && has 'GITHUB_JOB=image' && has 'podman run' && ! has 'GITHUB_JOB=sign'; then
  ok "a failed tag stops before sign (exit $rc)"
else bad "failed tag: rc=$rc log='$(cat "$LOG")'"; fi

echo dirty > "$TREE/untracked"
run "$FULL"
if [ "$rc" -ne 0 ] && grep -q dirty "$WORK/err" && [ ! -s "$LOG" ]; then ok "a dirty tree is refused before anything runs"
else bad "dirty tree: rc=$rc log='$(cat "$LOG")'"; fi
rm -f "$TREE/untracked"

printf '\n'
[ "$FAILED" -eq 0 ] && { printf 'hotfix PASSED\n'; exit 0; }
printf 'hotfix FAILED: %s check(s)\n' "$FAILED"; exit 1
