#!/bin/sh
# scripts/self.sh in a fake repo, with a fake podman (and the cosign inside it),
# skopeo, git ls-remote and curl: dispatch, the check plan, the image job's
# input validation and, above all, the sign job, which holds sdlc's key and is
# the only thing that may move :<major>. Every refusal is asserted to have
# signed, pushed and tagged nothing.
#
# Usage: tests/test_self.sh

set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
SRC=$PWD/scripts/self.sh
FAILED=0
ok()  { printf 'ok   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }

printf '== scripts/self.sh\n\n'

W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
mkdir "$W/bin" "$W/tmp" "$W/repo"
SYSPATH=$PATH
REALGIT=$(command -v git)
hex() { printf 'sha256:%s' "$(printf '%064d' 0 | tr 0 "$1")"; }
D1=$(hex a) D0=$(hex b) DB=$(hex c) D2=$(hex d)
A=quay.io/crunchtools/sdlc
B=quay.io/crunchtools/sdlc-bootstrap
CI=registry.access.redhat.com/hi/cosign@$(hex e)

# Fakes, steered by files in $W. All of them append their argv to $W/log.
# podman: `login` keeps stdin and writes --authfile 0600, as podman does;
# `build` is a no-op; `push --digestfile F` writes
# digest-sdlc or digest-boot; `run ... -- IMAGE ARGS` is cosign: public-key
# prints secret.pub, verify succeeds for a ref that has signed/<ref>__<repo>
# (one version per line) and prints cosign's JSON, sign records one;
# verify-attestation succeeds for a ref with att/<ref>__<type>, and attest
# records one unless `att-fails` exists. `scan` is syft, printing an SBOM
# (nothing on `syft-empty`, exit 1 on `syft-fails`).
{ printf '#!/bin/sh\nW=%s\n' "$W"; cat <<'EOF'; } > "$W/bin/podman"
printf 'podman %s\n' "$*" >> "$W/log"
san() { printf '%s' "$1" | tr '/:@' '___'; }
case $1 in
  login) cat > "$W/login-stdin"
         _af=; _next=
         for _a; do [ "$_next" != 1 ] || _af=$_a; _next=; [ "$_a" != --authfile ] || _next=1; done
         [ -z "$_af" ] || { printf '{}' > "$_af"; chmod 0600 "$_af"; } ;;
  build) ;;
  push) _f= _dest= _next=
        for _a; do
          if [ "$_next" = 1 ]; then _f=$_a; _next=; continue; fi
          [ "$_a" != --digestfile ] || _next=1
          _dest=$_a
        done
        case $_dest in *sdlc-bootstrap*) cat "$W/digest-boot" ;; *) cat "$W/digest-sdlc" ;; esac > "$_f" ;;
  run) shift
       # cosign runs as nonroot: it reads the mounted login only if others may.
       for _a; do case $_a in
         *:/auth:*) _ad=${_a%%:/auth:*}
                    [ ! -e "$_ad/config.json" ] \
                      || printf 'authmode %s %s\n' "$(stat -c %a "$_ad")" "$(stat -c %a "$_ad/config.json")" >> "$W/log" ;;
       esac; done
       while [ $# -gt 0 ] && [ "$1" != -- ]; do shift; done
       [ $# -gt 0 ] || exit 0          # shellcheck, test suites: not cosign
       shift 2
       case $1 in
         public-key) cat "$W/secret.pub" ;;
         verify) _n= _repo= _ref=
                 for _a; do
                   [ "$_n" != 1 ] || _repo=${_a#repo=}
                   _n=; [ "$_a" != -a ] || _n=1
                   _ref=$_a
                 done
                 _f=$W/signed/$(san "$_ref")__$(san "$_repo")
                 [ -s "$_f" ] || { printf 'error: no matching signatures for %s\n' "$_ref" >&2; exit 1; }
                 _out=[ _s=
                 while read -r _v; do
                   _out="$_out$_s{\"optional\":{\"repo\":\"$_repo\",\"version\":\"$_v\"}}"; _s=,
                 done < "$_f"
                 printf '%s]\n' "$_out" ;;
         sign) _n= _repo= _v= _ref=
               for _a; do
                 case $_n in 1) case $_a in repo=*) _repo=${_a#repo=} ;; version=*) _v=${_a#version=} ;; esac ;; esac
                 _n=; [ "$_a" != -a ] || _n=1
                 _ref=$_a
               done
               mkdir -p "$W/signed"
               printf '%s\n' "$_v" > "$W/signed/$(san "$_ref")__$(san "$_repo")" ;;
         verify-attestation|attest)
               _c=$1 _n= _t= _ref=
               for _a; do
                 [ "$_n" != 1 ] || _t=$_a
                 _n=; [ "$_a" != --type ] || _n=1
                 _ref=$_a
               done
               _f=$W/att/$(san "$_ref")__$_t
               if [ "$_c" = attest ]; then
                 [ -e "$W/att-fails" ] || { mkdir -p "$W/att"; : > "$_f"; }
               else [ -e "$_f" ] || { echo "error: no matching attestations for $_ref" >&2; exit 1; }; fi ;;
         scan) [ ! -e "$W/syft-fails" ] || exit 1
               [ -e "$W/syft-empty" ] || printf '{"spdxVersion":"SPDX-2.3"}\n' ;;
         *) exit 99 ;;
       esac ;;
  *) exit 99 ;;
esac
EOF

# skopeo: reg/tag/<ref> holds the digest a tag names, reg/label/<ref@digest>
# the version label. An absent tag is `manifest unknown` on stderr. `swap-at N`
# and `swap-to D` make the Nth inspect of :0 find D there instead.
{ printf '#!/bin/sh\nW=%s\n' "$W"; cat <<'EOF'; } > "$W/bin/skopeo"
printf 'skopeo %s\n' "$*" >> "$W/log"
san() { printf '%s' "$1" | tr '/:@' '___'; }
case $1 in
  inspect)
    for _a; do _ref=${_a#docker://}; done
    case $* in
      *'{{.Digest}}'*)
        case $_ref in *sdlc:0)
          _c=$(( $(cat "$W/count" 2> /dev/null || echo 0) + 1 )); echo "$_c" > "$W/count"
          if [ -e "$W/swap-at" ] && [ "$_c" = "$(cat "$W/swap-at")" ]; then
            cp "$W/swap-to" "$W/reg/tag/$(san "$_ref")"
          fi
          if [ -e "$W/err-at" ] && [ "$_c" = "$(cat "$W/err-at")" ]; then
            echo 'Error: unauthorized: authentication required' >&2; exit 1
          fi ;;
        esac
        _f=$W/reg/tag/$(san "$_ref")
        if [ -s "$_f" ]; then cat "$_f"
        elif [ -e "$W/deleted-0" ]; then echo 'unknown: Tag 0 was deleted or has expired. To pull, revive via time machine' >&2; exit 1
        else echo 'Error: manifest unknown' >&2; exit 1; fi ;;
      *--format*image.revision*)
        [ ! -e "$W/rev-fails" ] || { echo 'Error: unauthorized: authentication required' >&2; exit 1; }
        cat "$W/reg/rev/$(san "$_ref")" 2> /dev/null || : ;;
      *--format*) cat "$W/reg/label/$(san "$_ref")" 2> /dev/null || : ;;
      # No --format is the whole inspect, which provenance.py reads.
      *) cat "$W/reg/json/$(san "$_ref")" 2> /dev/null || printf '{"Labels":{}}\n' ;;
    esac ;;
  copy)
    for _a; do _src=${_dst:-}; _dst=${_a#docker://}; done
    _src=${_src#docker://}
    printf '%s' "${_src#*@}" > "$W/reg/tag/$(san "${_dst}")" ;;
  *) exit 99 ;;
esac
EOF

# git: ls-remote answers from `tags` (or fails on `ls-remote-fails`); all else
# is the real git, with fetches logged.
{ printf '#!/bin/sh\nW=%s\n' "$W"; cat <<EOF; } > "$W/bin/git"
if [ "\$1" = ls-remote ]; then
  printf 'git %s\n' "\$*" >> "\$W/log"
  [ ! -e "\$W/ls-remote-fails" ] || exit 128
  cat "\$W/tags" 2> /dev/null || :
  exit 0
fi
case " \$* " in *" fetch "*) printf 'git %s\n' "\$*" >> "\$W/log" ;; esac
exec $REALGIT "\$@"
EOF
# curl: the keepalive. Its stdin is the header, which must not be in argv.
{ printf '#!/bin/sh\nW=%s\n' "$W"; cat <<'EOF'; } > "$W/bin/curl"
printf 'curl %s\n' "$*" >> "$W/log"
cat > "$W/curl-stdin"
EOF
chmod +x "$W/bin/"*

# The fake repo: a git tree of its own, and every script test_all runs a stub.
mkdir -p "$W/repo/scripts" "$W/repo/boot/keys" "$W/repo/tests" "$W/repo/bin"
cp "$SRC" "$W/repo/scripts/self.sh"
for s in tests/test_host_contract.sh tests/test_bootstrap.sh tests/test_sign_digest.sh \
         tests/test_attest_digest.sh tests/test_provenance.py tests/test_revision_of.sh \
         tests/test_release_version.py tests/test_boot.sh tests/test_self.sh tests/test_hotfix.sh \
         tests/test_mirror_parent.sh tests/contract.sh \
         tests/test_container_stack.sh tests/test_node_stack.sh tests/test_bootc_stack.sh \
         tests/test_docs_stack.sh bin/sdlc; do
  # shellcheck disable=SC2016
  printf '#!/bin/sh\nprintf "stub %%s\\n" "$0 $*" >> "%s/log"\n' "$W" > "$W/repo/$s"
  chmod +x "$W/repo/$s"
done
write_tree() {
  printf 'FROM scratch\nARG SDLC_MAJOR=0\nARG SDLC_MIN_VERSION=0.12.0\n' > "$W/repo/boot/Containerfile"
  printf 'fake sdlc public key\n' > "$W/repo/boot/keys/sdlc.pub"
  printf '[project]\nname = "sdlc"\nversion = "0.13.0"\n' > "$W/repo/pyproject.toml"
  printf '# Changelog\n\n## [Unreleased]\n\n## [0.13.0] - 2026-11-23\n' > "$W/repo/CHANGELOG.md"
}
write_tree
cp scripts/provenance.py "$W/repo/scripts/"
git -C "$W/repo" init -q
git -C "$W/repo" add -A
git -C "$W/repo" -c user.name=t -c user.email=t@t commit -q -m init
REV=$(git -C "$W/repo" rev-parse HEAD)

reset() {
  rm -rf "$W/log" "$W/reg" "$W/signed" "$W/att" "$W/att-fails" "$W/syft-empty" "$W/syft-fails" "$W/count" "$W/swap-at" "$W/swap-to" "$W/err-at" "$W/rev-fails" "$W/deleted-0" "$W/tags" \
         "$W/ls-remote-fails" "$W/curl-stdin" "$W/login-stdin" "$W/repo/boot/keys/"*
  mkdir -p "$W/reg/tag" "$W/reg/label"
  write_tree
  cp "$W/repo/boot/keys/sdlc.pub" "$W/secret.pub"
  printf '%s' "$D1" > "$W/digest-sdlc"
  printf '%s' "$DB" > "$W/digest-boot"
}
san() { printf '%s' "$1" | tr '/:@' '___'; }
reg_tag()    { printf '%s' "$2" > "$W/reg/tag/$(san "$1")"; }
reg_label()  { printf '%s\n' "$2" > "$W/reg/label/$(san "$1")"; }
reg_rev()    { mkdir -p "$W/reg/rev"; printf '%s\n' "$2" > "$W/reg/rev/$(san "$1")"; }
mark_signed() { mkdir -p "$W/signed"; printf '%s\n' "$3" > "$W/signed/$(san "$1")__$(san "$2")"; }
tag_of()     { cat "$W/reg/tag/$(san "$1")" 2> /dev/null || :; }
# :0 as a release carried it: tagged, signed and labelled $2 at digest $1,
# built from the fake repo's commit.
carry()      { reg_tag "$A:0" "$1"; mark_signed "$A@$1" crunchtools/sdlc "$2"; reg_label "$A@$1" "$2"
               reg_rev "$A@$1" "$REV"; }

run() {              # job mode [VAR=value...]: sets RC and OUT
  _j=$1 _m=$2; shift 2
  RC=0; rm -f "$W/out"
  OUT=$(env -i PATH="$W/bin:$SYSPATH" HOME="$W" TMPDIR="$W/tmp" GITHUB_OUTPUT="$W/out" \
          COSIGN_IMAGE="$CI" SDLC_KEY=k SDLC_QUAY_USERNAME=u SDLC_QUAY_PASSWORD=p \
          GITHUB_JOB="$_j" MODE="$_m" "$@" sh "$W/repo/scripts/self.sh" 2>&1) || RC=$?
}
dump() { printf '%s\n' "$OUT" | tail -4 | sed 's/^/     | /'; }
in_log() { grep -q -- "$1" "$W/log" 2> /dev/null; }
n_log()  { grep -c -- "$1" "$W/log" 2> /dev/null || :; }
# A sign in cosign's legacy format, the only one podman and bootc read (a1751).
SIGNED=' sign --yes --tlog-upload=false --use-signing-config=false --new-bundle-format=false --key env://SDLC_KEY '
untouched() {        # nothing signed, pushed or tagged
  ! in_log ' sign --yes ' && ! in_log '^skopeo copy' && ! in_log '^podman push'
}
# A refusal: exit 1, for the stated reason, with nothing signed, pushed or moved.
refused() {
  if [ "$RC" -eq 1 ] && printf '%s' "$OUT" | grep -qF -- "$2" && untouched; then
    ok "$1"
  else
    bad "$1: exit $RC, wanted '$2'"; dump
  fi
}
# As refused, for the checks sign makes after it has signed sdlc: only the tags
# and the registry must be untouched.
refused_late() {
  if [ "$RC" -eq 1 ] && printf '%s' "$OUT" | grep -qF -- "$2" && ! in_log '^skopeo copy'; then
    ok "$1"
  else
    bad "$1: exit $RC, wanted '$2'"; dump
  fi
}
check() { _n=$1; shift; if "$@"; then ok "$_n"; else bad "$_n: exit $RC"; dump; fi; }
# shellcheck disable=SC2329  # called through check
rc_is() { [ "$RC" -eq "$1" ]; }
no_log_ok() { if [ ! -e "$W/log" ]; then ok "$1"; else bad "$1"; fi; }

printf '%s\n' '-- dispatch'
reset; run nope release
check "an unknown job is 2" rc_is 2
reset; run sign nope
check "an unknown mode is 2" rc_is 2
reset; run check release
check "a job with another job's mode is 2" rc_is 2
reset; printf 'FROM scratch\nARG SDLC_MIN_VERSION=0.12.0\n' > "$W/repo/boot/Containerfile"
run test test GITHUB_EVENT_NAME=pull_request
check "no ARG SDLC_MAJOR in boot/Containerfile is 2, before anything runs" rc_is 2
no_log_ok "  and nothing was called"
reset; printf 'FROM scratch\nARG SDLC_MAJOR=0\nARG SDLC_MIN_VERSION=latest\n' > "$W/repo/boot/Containerfile"
run test test GITHUB_EVENT_NAME=pull_request
check "a non-semver ARG SDLC_MIN_VERSION is 2" rc_is 2
reset; run check check GITHUB_EVENT_NAME=pull_request
refused "check on pull_request is refused: a PR's code runs only in test" "never runs on pull_request"
reset; run test test GITHUB_EVENT_NAME=push
refused "test on push is refused" "pull_request only"
reset; run image weekly GITHUB_EVENT_NAME=push
refused "image MODE=weekly on push is refused" "does not go with event push"
reset; run image release GITHUB_EVENT_NAME=schedule SDLC_WRITE=success SDLC_RELEASE=0.13.0
refused "image MODE=release on schedule is refused" "does not go with event schedule"
for me in release:schedule weekly:push rollback:push release:workflow_dispatch release:; do
  reset; run sign "${me%%:*}" EVENT="${me#*:}" BUILT="{\"digest\":\"$D1\"}" APPROVED=success
  refused "sign MODE=${me%%:*} with EVENT='${me#*:}' is refused" "does not go with event"
  no_log_ok "  before cosign, the registry or the key"
done

printf '\n%s\n' '-- test and check, and the plan'
reset; run test test GITHUB_EVENT_NAME=pull_request
if [ "$RC" -eq 0 ] && in_log "^podman build -f $W/repo/Containerfile" && in_log 'stub bin/sdlc ci' \
   && in_log "stub .*tests/contract.sh localhost/sdlc:$REV" && in_log 'stub tests/test_boot.sh' \
   && in_log 'stub tests/test_self.sh' && [ ! -e "$W/out" ]; then
  ok "test builds, runs every suite, the contract test and sdlc ci, and plans nothing"
else bad "test: exit $RC"; dump; fi

# The git-init fetch has no origin, and the constitution validator names the
# repo from it (#105): test and check add one, and never replace one.
# The raw config, not `remote get-url`, which applies a seat's url.insteadOf.
origin() { git -C "$W/repo" config --get remote.origin.url 2> /dev/null || :; }
git -C "$W/repo" remote remove origin 2> /dev/null || :
reset; run test test GITHUB_EVENT_NAME=pull_request
check "no origin: test adds the github.com default" [ "$(origin)" = https://github.com/crunchtools/sdlc ]
git -C "$W/repo" remote remove origin
reset; run check check GITHUB_EVENT_NAME=schedule GITHUB_SERVER_URL=https://ghe.example \
  GITHUB_REPOSITORY=o/r GITHUB_TOKEN=t GITHUB_API_URL=https://api
check "no origin: check adds GITHUB_SERVER_URL/GITHUB_REPOSITORY" [ "$(origin)" = https://ghe.example/o/r ]
git -C "$W/repo" remote set-url origin https://example.org/keep
reset; run test test GITHUB_EVENT_NAME=pull_request GITHUB_SERVER_URL=https://ghe.example GITHUB_REPOSITORY=o/r
check "an existing origin is kept" [ "$(origin)" = https://example.org/keep ]
git -C "$W/repo" remote remove origin
SRVENV="GITHUB_SERVER_URL=https://github.com GITHUB_REPOSITORY=crunchtools/sdlc"
reset; : > "$W/tags"
# shellcheck disable=SC2086
run check check GITHUB_EVENT_NAME=push $SRVENV
if [ "$RC" -eq 0 ] && grep -qxF 'result={"write":true,"image":true,"release":{"version":"0.13.0","publish":"image"}}' "$W/out" \
   && in_log '^git ls-remote --tags https://github.com/crunchtools/sdlc refs/tags/v0.13.0$' \
   && ! in_log '--no-cache'; then
  ok "plan: a version with a CHANGELOG heading and no tag is a release merge (cached build)"
else bad "plan release: exit $RC"; dump; fi
reset; printf 'x\trefs/tags/v0.13.0\n' > "$W/tags"
# shellcheck disable=SC2086
run check check GITHUB_EVENT_NAME=push $SRVENV
check "plan: a tag that exists is not a release" grep -qxF 'result={"write":false,"release":null}' "$W/out"
reset; : > "$W/tags"; printf '# Changelog\n\n## [0.12.0] - x\n## [0.13.01]\n' > "$W/repo/CHANGELOG.md"
# shellcheck disable=SC2086
run check check GITHUB_EVENT_NAME=push $SRVENV
check "plan: no CHANGELOG heading for the version is not a release" grep -qxF 'result={"write":false,"release":null}' "$W/out"
reset; sed -i 's/^version = .*/version = "0.13"/' "$W/repo/pyproject.toml"
# shellcheck disable=SC2086
run check check GITHUB_EVENT_NAME=push $SRVENV
refused "plan: a pyproject version that is not X.Y.Z fails the job" "is not X.Y.Z"
reset; touch "$W/ls-remote-fails"
# shellcheck disable=SC2086
run check check GITHUB_EVENT_NAME=push $SRVENV
refused "plan: a tag listing that cannot be read fails, it is not 'no tag'" "cannot list the tags"
reset
# shellcheck disable=SC2086
run check check GITHUB_EVENT_NAME=schedule GITHUB_TOKEN=tok123 GITHUB_API_URL=https://api $SRVENV
if [ "$RC" -eq 0 ] && grep -qxF 'result={"write":false,"release":null}' "$W/out" \
   && in_log '^podman build --no-cache' && in_log 'actions/workflows/ci.yml/enable' \
   && [ "$(cat "$W/curl-stdin")" = 'Authorization: Bearer tok123' ] && ! in_log tok123 \
   && ! in_log '^git ls-remote'; then
  ok "schedule: uncached build, no write, keepalive with the token on stdin and not in argv"
else bad "schedule: exit $RC"; dump; fi
reset
# shellcheck disable=SC2086
run check check GITHUB_EVENT_NAME=workflow_dispatch $SRVENV
if [ "$RC" -eq 0 ] && grep -qxF 'result={"write":false,"release":null}' "$W/out" && ! in_log '^curl'; then
  ok "workflow_dispatch: no write, no keepalive"
else bad "dispatch: exit $RC"; dump; fi

printf '\n%s\n' '-- image'
# check's plan, never write's result: write tags only once this build has
# passed (a1667), so image cannot wait on it.
img() { run image release GITHUB_EVENT_NAME=push "$@"; }
PLAN_R='SDLC_CHECK={"write":true,"image":true,"release":{"version":"0.13.0","publish":"image"}}'
reset; img "$PLAN_R"
WANT="result={\"digest\":\"$D1\",\"version\":\"0.13.0\",\"boot\":\"$DB\"}"
if [ "$RC" -eq 0 ] && grep -qxF "$WANT" "$W/out" && [ "$(cat "$W/login-stdin")" = p ] \
   && in_log "stub .*tests/contract.sh localhost/sdlc:$REV" \
   && in_log "^podman push .* docker://$A:gated-$REV\$" && in_log "^podman push .* docker://$B:gated-$REV\$" \
   && ! in_log ' sign --yes ' && ! in_log '^skopeo copy' && ! in_log 'u -p'; then
  ok "release: check's planned version builds before write runs; candidates pushed gated, nothing signed or tagged"
else bad "image release: exit $RC"; dump; cat "$W/out" 2> /dev/null || :; fi
reset; img SDLC_WRITE=failure "$PLAN_R"
if [ "$RC" -eq 0 ] && grep -qxF "$WANT" "$W/out"; then
  ok "release: write's result plays no part in the build"
else bad "image with a failed write: exit $RC"; dump; fi
reset; img
refused "release: no plan is a failure, not a skip" "check planned no release"
reset; img 'SDLC_CHECK={"write":false,"release":null}'
refused "release: a plan with no release builds nothing" "check planned no release"
for v in 0.13 1.2.3.4 01.2.3 v0.13.0 0.13.0-rc.1 ''; do
  reset; img "SDLC_CHECK={\"release\":{\"version\":\"$v\"}}"
  refused "release: version '$v' is not X.Y.Z" "check planned no release"
done
reset; img 'SDLC_CHECK={"release":{"version":'
refused "release: a plan that is not JSON is refused" "check planned no release"
reset; img 'SDLC_CHECK={"release":{"version":"0.14.0"}}'
refused "release: a version other than the tree's is refused" "the tree is 0.13.0, check planned 0.14.0"
reset; sed -i 's/^version = .*/version = "1.0.0"/' "$W/repo/pyproject.toml"
img 'SDLC_CHECK={"release":{"version":"1.0.0"}}'
refused "release: a version of another major than boot/Containerfile is refused" "is not of major 0"
reset; img "$PLAN_R" SDLC_QUAY_USERNAME=
refused "release: no SDLC_QUAY_USERNAME is refused before anything is built" "SDLC_QUAY_USERNAME"
no_log_ok "  and nothing was called"
reset; img "$PLAN_R" SDLC_QUAY_PASSWORD=
refused "release: no SDLC_QUAY_PASSWORD is refused" "SDLC_QUAY_PASSWORD"
reset; printf 'garbage' > "$W/digest-sdlc"; img "$PLAN_R"
check "release: a push that reports no digest fails" rc_is 1
# weekly rebuilds the commit :0's revision label names, fetched from the fake
# repo itself. No tag is fetched: a tag can be moved, the signed label cannot.
reset; carry "$D0" 0.13.0
run image weekly GITHUB_EVENT_NAME=schedule GITHUB_SERVER_URL="file://$W" GITHUB_REPOSITORY=repo
if [ "$RC" -eq 0 ] && grep -qxF "result={\"digest\":\"$D1\",\"version\":\"0.13.0\",\"boot\":\"\"}" "$W/out" \
   && in_log "^git .*fetch -q --depth=1 file://$W/repo $REV\$" && ! in_log 'refs/tags' \
   && in_log '^podman build --no-cache' && ! in_log "^podman push .*$B"; then
  ok "weekly: rebuilds the commit :0 names (not a tag), uncached, and pushes no sdlc-bootstrap"
else bad "weekly image: exit $RC"; dump; fi
reset; carry "$D0" 0.13.0; reg_rev "$A@$D0" "v0.13.0"
run image weekly GITHUB_EVENT_NAME=schedule GITHUB_SERVER_URL="file://$W" GITHUB_REPOSITORY=repo
refused "weekly: a revision label that is not a commit sha is refused" "is not a commit sha"
reset; carry "$D0" 0.13.0; touch "$W/rev-fails"
run image weekly GITHUB_EVENT_NAME=schedule GITHUB_SERVER_URL="file://$W" GITHUB_REPOSITORY=repo
refused "weekly: a revision label that cannot be read is refused" "cannot read the revision label"
check "  and nothing was fetched or built" sh -c "! grep -q '^git .*fetch\|^podman build' '$W/log'"
reset
run image weekly GITHUB_EVENT_NAME=schedule GITHUB_SERVER_URL="file://$W" GITHUB_REPOSITORY=repo
refused "weekly: no :0 means no release to rebuild" "does not exist"

printf '\n%s\n' '-- sign: inputs, cosign and the key'
BUILT_R="{\"digest\":\"$D1\",\"version\":\"0.13.0\",\"boot\":\"$DB\"}"
rel_setup() { reset; reg_label "$A@$D1" 0.13.0; reg_label "$B@$DB" 0.13.0; }
rel() { run sign release EVENT=push BUILT="$BUILT_R" SDLC_WRITE=success SDLC_RELEASE='{"version":"0.13.0"}' "$@"; }
rel_setup; rel SDLC_KEY=
refused "a missing SDLC_KEY is refused" "no SDLC_KEY"
no_log_ok "  before cosign ran"
for ci in "registry.access.redhat.com/hi/cosign:3.1.6" "quay.io/crunchtools/sdlc@$(hex e)" \
          "registry.access.redhat.com/hi/cosign@sha256:abc" "registry.access.redhat.com/hi/cosign@$(hex G)" ''; do
  rel_setup; rel COSIGN_IMAGE="$ci"
  refused "COSIGN_IMAGE '$ci' is refused" "COSIGN_IMAGE"
  no_log_ok "  before anything ran"
done
rel_setup; printf 'someone else\n' > "$W/secret.pub"; rel
refused "an SDLC_KEY whose public half is in no boot/keys file is refused" "not the private half"
rel_setup; rm -f "$W/repo/boot/keys/"*; rel
refused "no boot/keys/*.pub is refused" "holds no sdlc public key"
no_log_ok "  before login"
rel_setup; rel SDLC_QUAY_PASSWORD=
refused "no registry password is refused before cosign runs" "SDLC_QUAY_PASSWORD"

printf '\n%s\n' '-- sign release'
rel_setup; rel
if in_log '^authmode ' && ! grep '^authmode ' "$W/log" | grep -qv '^authmode 755 644$'; then
  ok "cosign's nonroot user can read the registry login it is handed"
else bad "authmode"; grep '^authmode' "$W/log" | sed 's/^/     | /'; fi
if [ "$RC" -eq 0 ] \
   && in_log "-a repo=crunchtools/sdlc -a version=0.13.0 $A@$D1\$" \
   && in_log "-a repo=crunchtools/sdlc-bootstrap -a version=0.13.0 $B@$DB\$" \
   && [ "$(n_log "$SIGNED")" -eq 2 ] \
   && [ "$(tag_of "$A:0.13.0")" = "$D1" ] && [ "$(tag_of "$A:0")" = "$D1" ] \
   && [ "$(tag_of "$A:latest")" = "$D1" ] && [ "$(tag_of "$B:0.13.0")" = "$DB" ] \
   && [ "$(n_log '^skopeo copy')" -eq 4 ] && ! in_log "^skopeo copy.*$B:0\$" && ! in_log "^skopeo copy.*$B:latest"; then
  ok "first release (:0 absent): signs sdlc and sdlc-bootstrap, moves 0.13.0, 0, latest and sdlc-bootstrap:0.13.0"
else bad "release first: exit $RC"; dump; fi
# Order: both signatures are in the log before the first tag moves, the
# bootstrap's before sdlc-bootstrap:<v> and sdlc's before :0. A tag moved ahead
# of its signature is a window in which an unsigned digest is what clients pull.
first() { grep -n -- "$1" "$W/log" 2> /dev/null | head -n 1 | cut -d: -f1; }
sg_a=$(first " sign --yes .*$A@$D1\$"); sg_b=$(first " sign --yes .*$B@$DB\$")
mv_any=$(first '^skopeo copy'); mv_0=$(first "^skopeo copy.* docker://$A:0\$")
mv_b=$(first "^skopeo copy.* docker://$B:0.13.0\$")
if [ -n "$sg_a" ] && [ -n "$sg_b" ] && [ -n "$mv_any" ] && [ -n "$mv_0" ] && [ -n "$mv_b" ] \
   && [ "$sg_a" -lt "$mv_any" ] && [ "$sg_b" -lt "$mv_any" ] \
   && [ "$sg_b" -lt "$mv_b" ] && [ "$sg_a" -lt "$mv_0" ]; then
  ok "release: every sign precedes the first tag move; sdlc's before :0, sdlc-bootstrap's before its tag"
else bad "release order: sign sdlc=$sg_a boot=$sg_b, first copy=$mv_any, :0=$mv_0, boot tag=$mv_b"; fi
if [ "$(n_log "^skopeo inspect .*{{.Digest}}.* docker://$A:latest\$")" -eq 1 ] \
   && [ "$(n_log "^skopeo inspect .*{{.Digest}}.* docker://$B:0.13.0\$")" -eq 1 ] \
   && in_log '^skopeo copy --authfile [^ ]* --all --preserve-digests'; then
  ok "each retag is a digest-preserving copy and is read back"
else bad "no read-back"; fi
# The key reaches cosign on public-key, sign and attest only, never on verify
# or syft, and cosign is always the pinned digest.
if [ "$(n_log '-e SDLC_KEY')" -eq 5 ] && ! grep -e ' verify ' -e ' verify-attestation ' -e ' scan ' "$W/log" | grep -q SDLC_KEY \
   && ! grep 'SDLC_KEY' "$W/log" | grep -qv -e ' public-key ' -e ' sign --yes ' -e ' attest --yes ' \
   && in_log '-e SDLC_KEY .* -- .* public-key ' && ! in_log 'SDLC_KEY=' \
   && [ "$(n_log ' verify ')" -ge 4 ] \
   && [ "$(n_log "-- $CI ")" -eq "$(( $(n_log ' verify ') + $(n_log ' verify-attestation ') + 5 ))" ]; then
  ok "the key is passed to public-key, sign and attest only, never to verify or syft, and never as a value"
else bad "key exposure"; grep -- 'run ' "$W/log" | sed 's/^/     | /'; fi

printf '\n%s\n' '-- sign release: attestations (#266)'
# The build record a ci.yml run on main stamps (self.sh build), for provenance.py.
record() {
  mkdir -p "$W/reg/json"
  _wf=crunchtools/sdlc/.github/workflows/ci.yml@refs/heads/main
  printf '{"Labels":{"org.opencontainers.image.revision":"%s","com.crunchtools.build.commit":"%s","com.crunchtools.build.run":"https://github.com/crunchtools/sdlc/actions/runs/7/attempts/1","com.crunchtools.build.builder":"%s","com.crunchtools.build.workflow":"%s"}}\n' \
    "$REV" "$REV" "$_wf" "$_wf" > "$W/reg/json/$(san "$1")"
}
has_att() { [ -e "$W/att/$(san "$1")__$2" ]; }
rel_setup; record "$A@$D1"; rel
mv_any=$(first '^skopeo copy')
at_a=$(first " attest --yes .*--type spdxjson .*$A@$D1\$"); at_p=$(first " attest --yes .*--type slsaprovenance1 .*$A@$D1\$")
at_b=$(first " attest --yes .*--type spdxjson .*$B@$DB\$")
if [ "$RC" -eq 0 ] && has_att "$A@$D1" spdxjson && has_att "$A@$D1" slsaprovenance1 \
   && has_att "$B@$DB" spdxjson && ! has_att "$B@$DB" slsaprovenance1 \
   && [ -n "$at_a" ] && [ -n "$at_p" ] && [ -n "$at_b" ] && [ -n "$mv_any" ] \
   && [ "$at_a" -lt "$mv_any" ] && [ "$at_p" -lt "$mv_any" ] && [ "$at_b" -lt "$mv_any" ] \
   && in_log ' attest --yes --tlog-upload=false --use-signing-config=false --new-bundle-format=false ' \
   && printf '%s' "$OUT" | grep -q 'no provenance attested'; then
  ok "release: SBOMs for both and sdlc's provenance from its build record, all before any tag moves"
else bad "release attest: exit $RC a=$at_a p=$at_p b=$at_b move=$mv_any"; dump; fi
if [ "$(n_log ' scan ')" -eq 2 ] && ! grep ' scan ' "$W/log" | grep -q -e SDLC_KEY -e "$CI" -e /auth -e DOCKER_CONFIG \
   && ! grep ' scan ' "$W/log" | grep -qv -e ' --read-only ' \
   && in_log "-- docker.io/anchore/syft:v[0-9.]*@sha256:[0-9a-f]\{64\} scan -q -o spdx-json registry:$A@$D1\$"; then
  ok "syft runs read-only from its pinned digest, with neither the key nor the registry login"
else bad "syft"; grep ' scan ' "$W/log" | sed 's/^/     | /'; fi
rel_setup; : > "$W/syft-fails"; rel
refused_late "syft failing moves no tag" "syft could not scan $A@$D1"
check "  and attests nothing" [ "$(n_log ' attest --yes ')" -eq 0 ]
rel_setup; : > "$W/syft-empty"; rel
refused_late "an empty SBOM moves no tag" "syft wrote no SBOM for $A@$D1"
check "  and attests nothing" [ "$(n_log ' attest --yes ')" -eq 0 ]
rel_setup; mkdir -p "$W/att"
for at in "$A@$D1"__spdxjson "$A@$D1"__slsaprovenance1 "$B@$DB"__spdxjson; do : > "$W/att/$(san "$at")"; done
rel
if [ "$RC" -eq 0 ] && [ "$(n_log ' attest --yes ')" -eq 0 ] && [ "$(n_log ' scan ')" -eq 0 ]; then
  ok "a re-run on attested digests scans and attests nothing"
else bad "attest re-run: exit $RC"; dump; fi
rel_setup; : > "$W/att-fails"; rel
refused_late "an attestation that does not verify after attesting moves no tag" "attestation does not verify"
if printf '%s' "$OUT" | grep -q '^self.sh:   '; then
  ok "  and cosign's own words are logged under the refusal"
else bad "no cosign diagnostic for a failed attestation"; dump; fi
rel_setup; record "$A@$D1"; record "$B@$DB"; rel GITHUB_ACTIONS=true
if [ "$RC" -eq 0 ] && has_att "$A@$D1" slsaprovenance1 && has_att "$B@$DB" slsaprovenance1; then
  ok "in Actions: both images' build records give provenance"
else bad "actions provenance: exit $RC"; dump; fi
rel_setup; record "$A@$D1"; rel GITHUB_ACTIONS=true
refused_late "in Actions: an image with no build record is refused before any tag moves" "refusing to publish it without provenance"

rel_setup; carry "$D0" 0.12.0; rel
if [ "$RC" -eq 0 ] && [ "$(tag_of "$A:0")" = "$D1" ] && [ "$(tag_of "$A:latest")" = "$D1" ]; then
  ok "a newer release than :0 moves :0"
else bad "newer than :0: exit $RC"; dump; fi
for old in 0.13.0 0.14.0; do
  rel_setup; carry "$D0" "$old"; rel
  refused "a release not newer than :0 ($old) is refused with no tag moved" "is not newer than $old"
done
rel_setup; BUILT_R="{\"digest\":\"sha256:abc\",\"boot\":\"$DB\"}" rel
BUILT_R="{\"digest\":\"$D1\",\"version\":\"0.13.0\",\"boot\":\"$DB\"}"
refused "a malformed BUILT digest is refused" "names no sha256 digest"
rel_setup; run sign release EVENT=push BUILT='not json'
refused "BUILT that is not JSON is refused" "names no sha256 digest"
rel_setup; run sign release EVENT=push BUILT=
refused "an empty BUILT is refused" "names no sha256 digest"
# a1667: write tags after image, and sign signs only what write released.
for wr in failure skipped cancelled ''; do
  rel_setup; rel SDLC_WRITE="$wr"
  refused "a release write did not tag ('$wr') signs nothing" "write did not succeed"
done
rel_setup; rel SDLC_RELEASE='{"version":"0.14.0"}'
refused "a release write tagged as another version signs nothing" "write released '0.14.0', the image is 0.13.0"
rel_setup; rel SDLC_RELEASE=
refused "a release with no write output signs nothing" "write released '', the image is 0.13.0"
rel_setup; reg_label "$A@$D1" 1.0.0; rel
refused "a label of another major is refused" "is not of major 0"
rel_setup; reg_label "$A@$D1" 0.14.0; rel
refused "a label that is not the merge's version is refused" "the release merge is 0.13.0, the image is 0.14.0"
rel_setup; reg_label "$A@$D1" latest; rel
refused "a label that is not X.Y.Z is refused" "is not X.Y.Z"
rel_setup; reg_label "$B@$DB" 0.12.9; rel
refused_late "an sdlc-bootstrap not labelled with the version is refused" "is not labelled 0.13.0"
rel_setup; BUILT_R="{\"digest\":\"$D1\",\"boot\":\"sha256:xyz\"}" rel
BUILT_R="{\"digest\":\"$D1\",\"version\":\"0.13.0\",\"boot\":\"$DB\"}"
refused_late "a malformed sdlc-bootstrap digest is refused" "malformed sdlc-bootstrap digest"
rel_setup; mark_signed "$A@$D1" crunchtools/sdlc 0.13.0; mark_signed "$B@$DB" crunchtools/sdlc-bootstrap 0.13.0; rel
if [ "$RC" -eq 0 ] && [ "$(n_log ' sign --yes ')" -eq 0 ] && [ "$(tag_of "$A:latest")" = "$D1" ] \
   && [ "$(tag_of "$B:0.13.0")" = "$DB" ]; then
  ok "a re-run on digests already signed as this version signs nothing and still succeeds"
else bad "re-run: exit $RC"; dump; fi
rel_setup; mark_signed "$A@$D1" crunchtools/sdlc 0.12.5; rel
refused "a digest signed as another version is refused, not skipped" "is signed as '0.12.5', not 0.13.0"
rel_setup; mark_signed "$B@$DB" crunchtools/sdlc-bootstrap 0.12.5; rel
refused_late "an sdlc-bootstrap signed as another version is refused" "is signed as '0.12.5', not 0.13.0"
rel_setup; carry "$D0" 0.12.0; reg_label "$A@$D0" 0.12.1; rel
refused ":0 whose label and annotation disagree is refused" "must agree"
rel_setup; carry "$D0" 0.12.0; mark_signed "$A@$D2" crunchtools/sdlc 0.12.0; reg_label "$A@$D2" 0.12.0
printf 2 > "$W/swap-at"; printf '%s' "$D2" > "$W/swap-to"; rel
refused_late ":0 moving between the first read and the retag is refused" "moved while this run was signing"
# A first release reads :0 as absent; a registry error on the re-read must stop
# the retag, not compare "" with "" and go ahead.
rel_setup; printf 2 > "$W/err-at"; rel
refused_late "a registry error re-reading :0 on a first release stops the retag" "cannot read $A:0"
# Two versions under sdlc's key: neither signed nor unsigned, and no third
# signature is added beside them.
rel_setup; mark_signed "$A@$D1" crunchtools/sdlc 0.13.0
printf '0.12.9\n' >> "$W/signed/$(san "$A@$D1")__crunchtools_sdlc"; rel
refused "an ambiguous signature is refused before anything is signed" "refusing to add another"
check "  and cosign sign never ran" [ "$(n_log ' sign --yes ')" -eq 0 ]

printf '\n%s\n' '-- sign weekly'
BUILT_W="{\"digest\":\"$D1\",\"version\":\"0.13.0\",\"boot\":\"$DB\"}"
wk_setup() { reset; reg_label "$A@$D1" 0.13.0; carry "$D0" 0.13.0; reg_rev "$A@$D1" "$REV"; }
# shellcheck disable=SC2119,SC2120
wk() { run sign weekly EVENT=schedule BUILT="$BUILT_W" "$@"; }
reset; reg_label "$A@$D1" 0.13.0; wk
refused ":0 absent is refused: a weekly run releases nothing" "does not exist"
wk_setup; carry "$D0" 0.12.9; wk
refused "a rebuild whose version is not :0's is refused" "weekly neither undoes a rollback nor releases"
wk_setup; reg_label "$A@$D0" 0.13.1; wk
refused ":0's annotation and label disagreeing is refused" "must agree"
# A moved vX.Y.Z tag builds other code under the same version: the revision
# label is what tells, and nothing is signed or tagged.
wk_setup; reg_rev "$A@$D1" "$(printf 'f%.0s' $(seq 40))"; wk
refused "a rebuild of another commit than :0's is refused" "was built from $REV"
check "  and nothing was signed" [ "$(n_log ' sign --yes ')" -eq 0 ]
wk_setup; touch "$W/rev-fails"; wk
refused "a revision label sign cannot read is refused" "cannot read the revision label"
wk_setup; wk
if [ "$RC" -eq 0 ] && [ "$(tag_of "$A:0.13.0")" = "$D1" ] && [ "$(tag_of "$A:0")" = "$D1" ] \
   && [ -z "$(tag_of "$A:latest")" ] && [ "$(n_log "$SIGNED")" -eq 1 ] \
   && in_log "-a repo=crunchtools/sdlc -a version=0.13.0 $A@$D1\$" && ! in_log "$B" \
   && [ "$(n_log '^skopeo copy')" -eq 2 ]; then
  ok "happy path: signs the rebuild, moves X.Y.Z and :0, not latest, touches no sdlc-bootstrap"
else bad "weekly happy: exit $RC"; dump; fi
check "  and attests the rebuild's SBOM" has_att "$A@$D1" spdxjson

printf '\n%s\n' '-- sign rollback'
rb_setup() { reset; mark_signed "$A@$D1" crunchtools/sdlc 0.13.0; reg_label "$A@$D1" 0.13.0; carry "$D0" 0.14.0; }
rb() { run sign rollback EVENT=workflow_dispatch APPROVED=success VERSION=0.13.0 DIGEST="$D1" "$@"; }
rb_setup; rb APPROVED=failure
refused "an unapproved rollback is refused" "sdlc-approval"
rb_setup; rb APPROVED=
refused "no approval result is refused" "sdlc-approval"
rb_setup; rb VERSION=0.11.9 DIGEST="$D1"
refused "a version below sdlc-bootstrap's minimum is refused" "below sdlc-bootstrap's minimum 0.12.0"
rb_setup; rb VERSION=1.0.0
refused "a version of another major is refused" "is not of major 0"
rb_setup; rb VERSION=latest
refused "a version that is not X.Y.Z is refused" "is not X.Y.Z"
rb_setup; rb DIGEST="$A@$D1"
refused "a DIGEST that is not sha256:<64 hex> is refused" "is not sha256"
rb_setup; rb DIGEST="$D2"; reg_label "$A@$D2" 0.13.0
refused "a digest sdlc's key never signed is refused" "is not signed by sdlc's key as 0.13.0"
# cosign's own words reach the log only when no key verifies: they are what
# tells "unsigned" from "registry unreachable".
if printf '%s' "$OUT" | grep -q "is not verified by sdlc's key as repo=crunchtools/sdlc; cosign said:" \
   && printf '%s' "$OUT" | grep -q "self.sh:   error: no matching signatures for $A@$D2"; then
  ok "an unverified digest logs cosign's stderr under the refusal"
else bad "no cosign diagnostic for an unverified digest"; dump; fi
rb_setup; mark_signed "$A@$D1" crunchtools/sdlc 0.12.5; rb
refused "a digest signed as another version is refused" "is not signed by sdlc's key as 0.13.0 ('0.12.5')"
rb_setup; reg_label "$A@$D1" 0.12.5; rb
refused "a label that disagrees with the version is refused" "version label is not 0.13.0"
rb_setup; rb
if [ "$RC" -eq 0 ] && [ "$(tag_of "$A:0")" = "$D1" ] && [ -z "$(tag_of "$A:latest")" ] && [ -z "$(tag_of "$A:0.13.0")" ] \
   && [ "$(n_log '^skopeo copy')" -eq 1 ] && [ "$(n_log ' sign --yes ')" -eq 0 ]; then
  ok "happy path: moves :0 and nothing else, signs nothing (not even down from a newer :0)"
else bad "rollback happy: exit $RC"; dump; fi
at_r=$(first " attest --yes .*--type spdxjson .*$A@$D1\$"); mv_r=$(first '^skopeo copy')
if has_att "$A@$D1" spdxjson && [ -n "$at_r" ] && [ -n "$mv_r" ] && [ "$at_r" -lt "$mv_r" ]; then
  ok "  and an unattested digest (from before #266) gets its SBOM before :0 moves"
else bad "rollback attest: attest=$at_r move=$mv_r"; dump; fi
rb_setup; mkdir -p "$W/att"; : > "$W/att/$(san "$A@$D1")__spdxjson"; rb
if [ "$RC" -eq 0 ] && [ "$(n_log ' scan ')" -eq 0 ] && [ "$(n_log ' attest --yes ')" -eq 0 ] \
   && [ "$(tag_of "$A:0")" = "$D1" ]; then
  ok "rollback to an attested digest scans and attests nothing"
else bad "rollback attested: exit $RC"; dump; fi
rb_setup; : > "$W/att-fails"; rb
if [ "$RC" -eq 1 ] && printf '%s' "$OUT" | grep -qF "attestation does not verify" \
   && [ "$(tag_of "$A:0")" = "$D0" ]; then
  ok "rollback to a digest whose attestation does not verify leaves :0 where it was"
else bad "rollback att-fails: exit $RC"; dump; fi
rb_setup; rb GITHUB_ACTIONS=true
if [ "$RC" -eq 0 ] && has_att "$A@$D1" spdxjson && ! has_att "$A@$D1" slsaprovenance1 \
   && [ "$(tag_of "$A:0")" = "$D1" ]; then
  ok "in Actions, rollback to a digest with no build record (a hotfix) still goes, with its SBOM"
else bad "rollback actions: exit $RC"; dump; fi
if ! printf '%s' "$OUT" | grep -q "cosign said"; then
  ok "a digest that verifies logs no cosign diagnostic"
else bad "a verified digest still logged a cosign diagnostic"; dump; fi
rb_setup; rb VERSION=0.12.0 DIGEST="$D2"; mark_signed "$A@$D2" crunchtools/sdlc 0.12.0; reg_label "$A@$D2" 0.12.0
rb VERSION=0.12.0 DIGEST="$D2"
check "a rollback to exactly sdlc-bootstrap's minimum is allowed" rc_is 0

printf '\n%s\n' '-- bootstrap'
bs_setup() { reset; printf '%s\trefs/tags/v0.13.0\n' "$REV" > "$W/tags"; }
# shellcheck disable=SC2086
bimg() { run image bootstrap $SRVENV GITHUB_EVENT_NAME=workflow_dispatch VERSION=0.13.0 APPROVED=success "$@"; }
bs_setup; bimg
WANT="result={\"digest\":\"$D1\",\"version\":\"0.13.0\",\"boot\":\"$DB\"}"
if [ "$RC" -eq 0 ] && grep -qxF "$WANT" "$W/out" \
   && in_log "^podman push .* docker://$A:gated-$REV\$" && in_log "^podman push .* docker://$B:gated-$REV\$" \
   && ! in_log ' sign --yes ' && ! in_log '^skopeo copy'; then
  ok "bootstrap image: no :0, tag at HEAD: both candidates pushed gated, nothing signed or tagged"
else bad "bootstrap image: exit $RC"; dump; fi
bs_setup; : > "$W/deleted-0"; bimg
check "bootstrap image: a :0 Quay reports deleted is absent" rc_is 0
bs_setup; printf 'x\trefs/tags/v0.13.0\n%s\trefs/tags/v0.13.0^{}\n' "$REV" > "$W/tags"; bimg
check "bootstrap image: an annotated tag is read at its peeled commit" rc_is 0
bs_setup
# shellcheck disable=SC2086
run image bootstrap $SRVENV GITHUB_EVENT_NAME=push VERSION=0.13.0 APPROVED=success
refused "bootstrap image on push is refused" "does not go with event"
bs_setup; bimg APPROVED=
refused "bootstrap image without approval is refused" "sdlc-approval"
bs_setup; bimg VERSION=0.13
refused "bootstrap image: a non-semver VERSION is refused" "is not X.Y.Z"
bs_setup; bimg VERSION=0.14.0
refused "bootstrap image: a VERSION the tree is not is refused" "the tree is 0.13.0"
bs_setup; : > "$W/tags"; bimg
refused "bootstrap image: no vX.Y.Z tag is refused" "no tag"
bs_setup; printf 'x\trefs/tags/v0.13.0\n' > "$W/tags"; bimg
refused "bootstrap image: a tag at another commit is refused" "v0.13.0 is at 'x'"
bs_setup; : > "$W/ls-remote-fails"; bimg
refused "bootstrap image: a tag listing that cannot be read is refused" "cannot list the tags"
bs_setup; carry "$D0" 0.12.0; bimg
refused "bootstrap image: an existing :0 is refused" "bootstrap runs once"

bsetup() { bs_setup; reg_label "$A@$D1" 0.13.0; reg_label "$B@$DB" 0.13.0; }
# shellcheck disable=SC2086
bsign() { run sign bootstrap $SRVENV EVENT=workflow_dispatch VERSION=0.13.0 APPROVED=success BUILT="$BUILT_R" "$@"; }
bsetup; bsign
if [ "$RC" -eq 0 ] && [ "$(n_log "$SIGNED")" -eq 2 ] \
   && in_log "-a repo=crunchtools/sdlc-bootstrap -a version=0.13.0 $B@$DB\$" \
   && [ "$(tag_of "$A:0.13.0")" = "$D1" ] && [ "$(tag_of "$A:0")" = "$D1" ] \
   && [ "$(tag_of "$A:latest")" = "$D1" ] && [ "$(tag_of "$B:0.13.0")" = "$DB" ]; then
  ok "bootstrap sign: signs both, moves 0.13.0, 0, latest and sdlc-bootstrap:0.13.0"
else bad "bootstrap sign: exit $RC"; dump; fi
bsetup; carry "$D0" 0.12.0; bsign
refused "bootstrap sign: an existing :0 is refused" "bootstrap runs once"
bsetup; bsign APPROVED=
refused "bootstrap sign without approval is refused" "sdlc-approval"
bsetup; bsign EVENT=push
refused "bootstrap sign on push is refused" "does not go with event"
bsetup; reg_label "$A@$D1" 0.12.0; reg_label "$B@$DB" 0.12.0; bsign VERSION=0.12.0
refused "bootstrap sign: a VERSION the tree is not is refused" "the tree is 0.13.0"
bsetup; : > "$W/tags"; bsign
refused "bootstrap sign: no vX.Y.Z tag is refused" "no tag"

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf 'self test PASSED\n'
  exit 0
fi
printf 'self test FAILED: %s check(s)\n' "$FAILED"
exit 1
