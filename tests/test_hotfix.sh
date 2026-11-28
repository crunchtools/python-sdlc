#!/bin/sh
# scripts/hotfix.sh, against a fake self.sh, openssl, curl and podman in a
# scratch git tree: the order is image -> mint the App's token (two curls) ->
# tag in the container -> sign -> revoke; the App key stays on stdin (never
# argv, never in self.sh's or the container's environment), the token goes to
# the container on stdin, a failed tag or mint stops before sign (the token is
# still revoked), and a missing secret, missing tool or dirty tree is refused
# before anything runs.
#
# Usage: tests/test_hotfix.sh

set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
FAILED=0
ok()  { printf 'ok   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
TREE=$WORK/tree BIN=$WORK/bin LOG=$WORK/log NOSSL=$WORK/nossl
mkdir -p "$TREE/scripts" "$BIN" "$NOSSL"
cp scripts/hotfix.sh "$TREE/scripts/hotfix.sh"
printf 'version = "1.2.3"\n' > "$TREE/pyproject.toml"
cat > "$TREE/scripts/self.sh" <<'SHIM'
#!/bin/sh
echo "self job=$GITHUB_JOB sdlckey=${SDLC_KEY:+set}${SDLC_KEY:-empty} app=${ASHIGARU_APP_KEY+key}${ASHIGARU_CLIENT_ID+id}" >> "$LOG"
echo "self-in job=$GITHUB_JOB check=${SDLC_CHECK:-} write=${SDLC_WRITE:-} release=${SDLC_RELEASE:-}" >> "$LOG"
echo "self-env job=$GITHUB_JOB builder=${SDLC_LOCAL_BUILDER-unset} attempt=${GITHUB_RUN_ATTEMPT-unset} ref=${GITHUB_REF-unset} runid=${GITHUB_RUN_ID-unset} wfref=${GITHUB_WORKFLOW_REF-unset}" >> "$LOG"
echo "self-at job=$GITHUB_JOB at=$0 stale=$([ -e "$(dirname "$0")/../stale.pyc" ] && echo yes || echo no)" >> "$LOG"
[ "$GITHUB_JOB" = image ] &&
  echo 'result={"digest":"sha256:abc","version":"1.2.3","boot":""}' >> "$GITHUB_OUTPUT"
exit 0
SHIM
cat > "$BIN/podman" <<'SHIM'
#!/bin/sh
echo "podman $*" >> "$LOG"
echo "podman-env app=${ASHIGARU_APP_KEY+key}${ASHIGARU_CLIENT_ID+id}" >> "$LOG"
echo "podman-stdin $(cat)" >> "$LOG"
[ ! -e "$FAIL_PODMAN" ] || { echo 'tag refused' >&2; exit 1; }
SHIM
cat > "$BIN/openssl" <<'SHIM'
#!/bin/sh
echo "openssl $*" >> "$LOG"
case $1 in
  base64) exec base64 -w0 ;;
  dgst)   key=$(cat); case $key in *KEYSECRET*) echo "openssl-key-on-stdin" >> "$LOG" ;; esac
          out=; next=
          for a; do [ "$next" != 1 ] || out=$a; next=; [ "$a" != -out ] || next=1; done
          if [ -n "$out" ]; then printf 'sigbytes' > "$out"; else printf 'sigbytes'; fi ;;
esac
SHIM
cat > "$BIN/curl" <<'SHIM'
#!/bin/sh
echo "curl $*" >> "$LOG"
echo "curl-stdin $(head -1)" >> "$LOG"
prev= url= method=GET
for a in "$@"; do
  [ "$prev" = -d ] && echo "curl-body $a" >> "$LOG"
  [ "$prev" = -X ] && method=$a
  prev=$a url=$a
done
case $url in
  */installation)       echo '{"id":42}' ;;
  */access_tokens)      [ ! -e "$FAIL_MINT" ] || exit 22; echo '{"token":"ghs_TOKEN"}' ;;
  */installation/token) echo "curl-revoke $method" >> "$LOG" ;;
esac
SHIM
chmod +x "$TREE/scripts/self.sh" "$BIN"/*
# A PATH with every tool hotfix.sh needs except openssl.
for t in git jq dirname cat sed head tr date mktemp rm base64; do ln -s "$(command -v "$t")" "$NOSSL/$t"; done
ln -s "$BIN/curl" "$BIN/podman" "$NOSSL/"
printf '*.pyc\n' > "$TREE/.gitignore"
git -C "$TREE" init -q
git -C "$TREE" add -A
git -C "$TREE" -c user.name=t -c user.email=t@t commit -qm fake
SHA=$(git -C "$TREE" rev-parse HEAD)
# Ignored, so `status --porcelain` is clean; a build from $TREE itself would see it.
echo stale > "$TREE/stale.pyc"

ALL='"SDLC_KEY":"k","SDLC_KEY_PASSWORD":"p","SDLC_QUAY_USERNAME":"u","SDLC_QUAY_PASSWORD":"q"'
FULL="{$ALL,\"ASHIGARU_CLIENT_ID\":\"CIDSECRET\",\"ASHIGARU_APP_KEY\":\"KEYSECRET\"}"
BODY='{"repositories":["sdlc"],"permissions":{"contents":"write","pull_requests":"read"}}'

# run <json> [path]: leaves rc, $WORK/err and $LOG for the assertions.
run() {
  : > "$LOG"
  rc=0
  # shellcheck disable=SC2086  # HF_CALLER_ENV is NAME=value words, split on purpose
  printf '%s' "$1" | env ${HF_CALLER_ENV:-} LOG=$LOG FAIL_PODMAN=$WORK/${FAIL_PODMAN:-none} FAIL_MINT=$WORK/${FAIL_MINT:-none} \
    PATH="${2:-$BIN:$PATH}" "$SH" "$TREE/scripts/hotfix.sh" > "$WORK/out" 2> "$WORK/err" || rc=$?
}
SH=$(command -v sh)
has()    { grep -qF -- "$1" "$LOG"; }
events() {
  sed -n -e 's/^self job=\([a-z]*\).*/self-\1/' -e 's|^curl .*/installation$|curl-install|' \
    -e 's|^curl .*/access_tokens$|curl-mint|' -e 's/^podman run.*/podman/' -e 's/^curl-revoke.*/revoke/' \
    -e '/^\(self-[a-z]*\|curl-install\|curl-mint\|podman\|revoke\)$/p' "$LOG" | tr '\n' ' '
}

printf '== scripts/hotfix.sh\n\n'

run "$FULL"
first_attempt=$(sed -n 's/^self-env job=image .*attempt=\([0-9]*\) .*/\1/p' "$LOG")
worktrees=$(git -C "$TREE" worktree list | wc -l)
if [ "$rc" -eq 0 ] && [ "$(events)" = 'self-image curl-install curl-mint podman self-sign revoke ' ]; then
  ok "image, mint, tag, sign, revoke (exit 0)"
else bad "order: rc=$rc events='$(events)' err=$(cat "$WORK/err")"; fi

for job in image sign; do
  at=$(sed -n "s/^self-at job=$job at=\\(.*\\) stale=.*/\\1/p" "$LOG")
  case $at in
    "$TREE/scripts/self.sh"|'') bad "$job ran self.sh from the tree itself, not a worktree: '$at'" ;;
    */scripts/self.sh) ok "$job runs a worktree's self.sh ($at)" ;;
    *) bad "$job self.sh path: '$at'" ;;
  esac
done
if [ "$worktrees" -eq 1 ] && [ ! -e "${at%/scripts/self.sh}" ]; then ok "the worktree is gone after the run"
else bad "worktrees after the run ($worktrees): $(git -C "$TREE" worktree list)"; fi
if [ "$(grep -c 'stale=no$' "$LOG")" -eq 2 ] && [ -e "$TREE/stale.pyc" ]; then
  ok "an ignored file (stale.pyc) is in the tree but never reaches image or sign"
else bad "stale.pyc: $(grep '^self-at' "$LOG")"; fi

if has "curl-body $BODY"; then ok "the token request asks for sdlc alone, contents write and pull_requests read"
else bad "mint body: $(grep '^curl-body' "$LOG")"; fi
if has 'podman-stdin ghs_TOKEN'; then ok "the container reads the token on stdin"
else bad "podman stdin: $(grep '^podman-stdin' "$LOG")"; fi
run_line=$(grep '^podman run' "$LOG" || true)
for want in "localhost/sdlc:$SHA" "-m sdlc_jobs._hotfix crunchtools/sdlc 1.2.3 $SHA"; do
  case $run_line in *"$want"*) ok "tag run carries: $want" ;; *) bad "tag run lacks '$want': $run_line" ;; esac
done
case " $run_line " in
  *" -e "*|*" --env"*) bad "podman got an -e: $run_line" ;;
  *) ok "podman argv has no -e" ;;
esac
if has openssl-key-on-stdin; then ok "the key went to openssl on stdin"; else bad "openssl never read the key"; fi
if ! grep -E '^(curl|podman|openssl) ' "$LOG" | grep -q 'KEYSECRET\|CIDSECRET\|ghs_TOKEN'; then
  ok "no App key, id or token on any argv"
else bad "secret on argv: $(grep -E '^(curl|podman|openssl) ' "$LOG" | grep 'KEYSECRET\|CIDSECRET\|ghs_TOKEN')"; fi
if ! grep -E '^(self|podman-env) ' "$LOG" | grep -q 'app=.'; then ok "self.sh and the container never see the App's names"
else bad "App name in an environment: $(grep -E '^(self|podman-env) ' "$LOG")"; fi
if has 'self job=image sdlckey=empty' && has 'self job=sign sdlckey=set'; then ok "image runs with an empty SDLC_KEY, sign with the real one"
else bad "SDLC_KEY: $(grep '^self' "$LOG")"; fi

# a1667: self.sh's image builds check's planned version without write, and its
# sign wants write's success and release, which the tag before it stands for.
if has 'self-in job=image check={"write":true,"image":true,"release":{"version":"1.2.3","publish":"image"}} write= release='
then ok "image is handed check's plan for 1.2.3 and nothing of write's"
else bad "image inputs: $(grep '^self-in job=image' "$LOG")"; fi
if has "self-in job=sign check= write=success release={\"version\":\"1.2.3\",\"sha\":\"$SHA\"}"
then ok "sign is handed write's success and the release it tagged"
else bad "sign inputs: $(grep '^self-in job=sign' "$LOG")"; fi

# #275: the build record sign's provenance.py trusts. The caller's own run id
# and workflow are dropped; the builder names this script at the tree's HEAD.
HF_CALLER_ENV='GITHUB_RUN_ID=9 GITHUB_WORKFLOW_REF=x' run "$FULL"
want_builder="https://github.com/crunchtools/sdlc/blob/$SHA/scripts/hotfix.sh"
img_env=$(grep '^self-env job=image ' "$LOG" || true)
sign_env=$(grep '^self-env job=sign ' "$LOG" || true)
for job in image sign; do
  line=$(grep "^self-env job=$job " "$LOG" || true)
  case $line in
    "self-env job=$job builder=$want_builder attempt="*" ref=refs/tags/v1.2.3 runid=unset wfref=unset")
      ok "$job: builder pinned to HEAD, tag ref, no run id or workflow ref even when the caller set them" ;;
    *) bad "$job env: $line" ;;
  esac
done
img_attempt=${img_env#*attempt=}; img_attempt=${img_attempt%% *}
sign_attempt=${sign_env#*attempt=}; sign_attempt=${sign_attempt%% *}
case $img_attempt in ''|*[!0-9]*) bad "attempt is not all digits: '$img_attempt'" ;;
  *) if [ "$img_attempt" = "$sign_attempt" ]; then ok "one all-digit GITHUB_RUN_ATTEMPT across image and sign"
     else bad "attempt differs: image=$img_attempt sign=$sign_attempt"; fi ;;
esac

if [ -n "$first_attempt" ] && [ "$first_attempt" != "$img_attempt" ]; then ok "two runs get different attempts"
else bad "attempts repeat: '$first_attempt' then '$img_attempt'"; fi

for name in ASHIGARU_APP_KEY ASHIGARU_CLIENT_ID; do
  run "$(printf '%s' "$FULL" | jq -c --arg n "$name" 'del(.[$n])')"
  if [ "$rc" -ne 0 ] && grep -qF "$name" "$WORK/err" && [ ! -s "$LOG" ]; then ok "missing $name: refused, nothing ran"
  else bad "missing $name: rc=$rc err='$(cat "$WORK/err")' log='$(cat "$LOG")'"; fi
done

: > "$WORK/failpodman"
FAIL_PODMAN=failpodman run "$FULL"
if [ "$rc" -ne 0 ] && has 'podman run' && ! has 'job=sign' && has 'curl-revoke DELETE'; then
  ok "a failed tag stops before sign and the token is still revoked (exit $rc)"
else bad "failed tag: rc=$rc events='$(events)'"; fi

: > "$WORK/failmint"
FAIL_MINT=failmint run "$FULL"
if [ "$rc" -ne 0 ] && ! has 'podman run' && ! has 'job=sign'; then ok "a failed mint stops before the tag and sign (exit $rc)"
else bad "failed mint: rc=$rc events='$(events)'"; fi

run "$FULL" "$NOSSL"
if [ "$rc" -ne 0 ] && grep -q openssl "$WORK/err" && [ ! -s "$LOG" ]; then ok "no openssl: refused before anything ran"
else bad "no openssl: rc=$rc err='$(cat "$WORK/err")' log='$(cat "$LOG")'"; fi

echo dirty > "$TREE/untracked"
run "$FULL"
if [ "$rc" -ne 0 ] && grep -q dirty "$WORK/err" && [ ! -s "$LOG" ]; then ok "a dirty tree is refused before anything runs"
else bad "dirty tree: rc=$rc log='$(cat "$LOG")'"; fi
rm -f "$TREE/untracked"

printf '\n'
[ "$FAILED" -eq 0 ] && { printf 'hotfix PASSED\n'; exit 0; }
printf 'hotfix FAILED: %s check(s)\n' "$FAILED"; exit 1
