#!/bin/sh
# Every step of crunchtools/sdlc's own ci.yml that is sdlc's own: test, check,
# image and sign (docs/marketplace-free-ci.md, "sdlc's own workflow"). The job
# is GITHUB_JOB and the mode is MODE, both set by the workflow, and every other
# input arrives through env:, never interpolated into a run: line.
#
#   test   pull_request. The PR's own code, in a job with a read-only token, no
#          secret and no socket of ours: builds both images and runs every
#          test, the contract test and `sdlc ci` from what it built.
#   check  default-branch push, schedule, dispatch. The same on reviewed code,
#          then the plan (`result`) and the 60-day schedule keepalive.
#   image  release: the release merge check planned, before write tags it.
#          weekly: the version :<major> carries, rebuilt from the commit its
#          revision label names (never the mutable vX.Y.Z tag).
#          bootstrap: sdlc's first release, once, by dispatch (see bootstrap_ok).
#          Pushes candidates by digest; signs nothing and moves no tag.
#   sign   the only step with sdlc's key. Never runs the image it signs: cosign
#          runs from COSIGN_IMAGE, a digest the workflow pins, and the release
#          is data. Re-validates everything except which digest was built.
#
# Exit 2 is "could not run" (an unknown job or mode); 1 is a refusal or a
# failed test.
#
# Usage: scripts/self.sh
set -eu

QUAY=quay.io/crunchtools/sdlc
BOOT=quay.io/crunchtools/sdlc-bootstrap
here=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

die() { printf 'self.sh: %s\n' "$*" >&2; exit 1; }
cannot() { printf 'self.sh: CANNOT RUN: %s\n' "$*" >&2; exit 2; }
say() { printf 'self.sh: %s\n' "$*"; }

# ------------------------------------------------------------------ versions

# Strict SemVer core: no prerelease, no build, no leading zeros. A prerelease
# never moves :<major>, so it never needs to pass here.
semver() {
  case $1 in ''|*[!0-9.]*|.*|*.|*..*|*.*.*.*) return 1 ;; esac
  case $1 in *.*.*) ;; *) return 1 ;; esac
  for _p in $(printf '%s' "$1" | tr . ' '); do
    case $_p in 0) ;; 0*) return 1 ;; esac
  done
}
major() { printf '%s' "${1%%.*}"; }
# Prints -1, 0 or 1 as $1 is below, equal to or above $2. Both strict SemVer.
vcmp() {
  printf '%s\n%s\n' "$1" "$2" | awk -F. '
    NR == 1 { a1 = $1; a2 = $2; a3 = $3 }
    NR == 2 { r = (a1 != $1) ? (a1 > $1 ? 1 : -1) : (a2 != $2) ? (a2 > $2 ? 1 : -1) \
                : (a3 != $3) ? (a3 > $3 ? 1 : -1) : 0; print r }'
}
digest_ok() {
  _hex=${1#sha256:}
  case $1 in sha256:*) ;; *) return 1 ;; esac
  case $_hex in ''|*[!0-9a-f]*) return 1 ;; esac
  [ "${#_hex}" -eq 64 ]
}
# The major and minimum sdlc-bootstrap bakes are the reviewed tree's, read from
# boot/Containerfile, so this file and the bootstrap cannot disagree.
boot_arg() {
  sed -n "s/^ARG $1=\\([0-9.]*\\)\$/\\1/p" "$here/boot/Containerfile" | head -1
}
tree_version() {
  sed -n 's/^version = "\(.*\)"$/\1/p' "$1/pyproject.toml" | head -1
}

# ------------------------------------------------------------- building/testing

SHELL_FILES='bin/sdlc bin/sdlc-bootstrap boot/sdlc scripts/self.sh scripts/release-version.sh
  scripts/hotfix.sh scripts/revision-of.sh scripts/sign-digest.sh scripts/attest-digest.sh tests/contract.sh
  tests/test_host_contract.sh tests/test_container_stack.sh tests/test_node_stack.sh
  tests/test_bootc_stack.sh tests/test_docs_stack.sh tests/test_revision_of.sh
  tests/test_bootstrap.sh tests/test_sign_digest.sh tests/test_attest_digest.sh
  tests/test_boot.sh tests/test_self.sh tests/test_hotfix.sh tests/test_apparmor.sh .semgrep/run-tests.sh'
SHELLCHECK=docker.io/koalaman/shellcheck:v0.11.0@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d

# Builds the tree in $1 as sdlc and sdlc-bootstrap, tagged $2 and $3. A run no
# commit triggered must not reuse a cached `dnf install`, or it ships no errata.
build() {
  _src=$1 _rev=$(git -C "$1" rev-parse HEAD) _ver=$(tree_version "$1") _args=
  [ "${GITHUB_EVENT_NAME:-}" = push ] || [ "${GITHUB_EVENT_NAME:-}" = pull_request ] \
    || _args=--no-cache
  _created=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  _run=${GITHUB_SERVER_URL:-}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}/attempts/${GITHUB_RUN_ATTEMPT:-}
  # The com.crunchtools.build.* labels are what scripts/provenance.py reads.
  # shellcheck disable=SC2086
  podman build $_args -f "$_src/Containerfile" \
    --build-arg "REVISION=$_rev" --build-arg "CREATED=$_created" \
    --label "com.crunchtools.build.run=$_run" \
    --label "com.crunchtools.build.builder=${GITHUB_WORKFLOW_REF:-}" \
    --label "com.crunchtools.build.workflow=${GITHUB_WORKFLOW_REF:-}" \
    --label "com.crunchtools.build.commit=$_rev" \
    -t "$2" "$_src"
  # shellcheck disable=SC2086
  podman build $_args -f "$_src/boot/Containerfile" \
    --build-arg "VERSION=$_ver" --build-arg "REVISION=$_rev" --build-arg "CREATED=$_created" \
    -t "$3" "$_src"
}

# Everything container.yml's build job runs today, plus sdlc-bootstrap's own tests
# and `sdlc ci` from the image just built. $1 is the sdlc image, $2 sdlc-bootstrap.
test_all() {
  cd "$here"
  # shellcheck disable=SC2086
  podman run --rm -v "$here:/mnt:ro,z" -w /mnt "$SHELLCHECK" $SHELL_FILES
  for t in tests/test_host_contract.sh tests/test_bootstrap.sh tests/test_sign_digest.sh \
           tests/test_attest_digest.sh tests/test_provenance.py tests/test_revision_of.sh \
           tests/test_release_version.py tests/test_boot.sh tests/test_self.sh \
           tests/test_hotfix.sh; do
    "$t"
  done
  tests/contract.sh "$1"
  # The four stacks are independent, so they run side by side; each one's output
  # is printed whole when all have finished. The image's own suites are not run
  # here: `sdlc ci` below runs every tests/test_*.py under pytest.
  _logs=$(mktemp -d)
  for s in container node bootc docs; do
    "tests/test_${s}_stack.sh" "$1" > "$_logs/$s" 2>&1 &
    echo $! > "$_logs/$s.pid"
  done
  _failed=
  for s in container node bootc docs; do
    wait "$(cat "$_logs/$s.pid")" || _failed="$_failed $s"
    printf '== %s stack\n' "$s"
    cat "$_logs/$s"
  done
  rm -rf "$_logs"
  [ -z "$_failed" ] || die "stack test(s) failed:$_failed"
  # sdlc-bootstrap runs nothing without a signed release, so its smoke test is
  # that every tool it needs is there and that it refuses on its own.
  podman run --rm --entrypoint sh "$2" -c 'cosign version && docker --version && jq --version'
  SDLC_IMAGE=$1 bin/sdlc ci
}

# ------------------------------------------------------------------ the plan

# A plan, not an authority (docs: "Releases run from the merge"). A
# default-branch push whose tree carries a version with a CHANGELOG heading
# and no vX.Y.Z tag yet is a release merge as far as check can tell; write
# verifies it against the API, and tags it only after image has passed; sign
# acts only on write's output. `image` is always true on a release: sdlc builds
# one for every release, and write.py refuses one whose planned image was
# skipped, so the plan must say so (a1667).
plan() {
  if [ "${GITHUB_EVENT_NAME:-}" != push ]; then
    printf '{"write":false,"release":null}'
    return
  fi
  _v=$(tree_version "$here")
  semver "$_v" || die "pyproject.toml version '$_v' is not X.Y.Z"
  _tags=$(git ls-remote --tags "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY" "refs/tags/v$_v") \
    || die "cannot list the tags of $GITHUB_REPOSITORY"
  if [ -z "$_tags" ] && grep -q "^## \\[$_v\\]\\( \\|\$\\)" "$here/CHANGELOG.md"; then
    jq -cn --arg v "$_v" '{write: true, image: true, release: {version: $v, publish: "image"}}'
  else
    printf '{"write":false,"release":null}'
  fi
}

# GitHub disables a public repo's schedule after 60 days without activity;
# enabling the workflow resets that clock. The token goes in on stdin, never
# in argv.
keepalive() {
  printf 'Authorization: Bearer %s\n' "$GITHUB_TOKEN" \
    | curl -fsS -X PUT -H @- -H 'Accept: application/vnd.github+json' \
        "$GITHUB_API_URL/repos/$GITHUB_REPOSITORY/actions/workflows/ci.yml/enable"
  say "kept the schedule alive"
}

# MODE=bootstrap: the release path's write runs in sdlc-bootstrap, which
# verifies :<major>, so the first release cannot run it. Once, by dispatch, a
# version whose vX.Y.Z tag the old release path already pushed at this commit
# is built and released as if write had; never once :<major> exists.
bootstrap_ok() {
  [ "${APPROVED:-}" = success ] || die "bootstrap needs the sdlc-approval job to have succeeded (${APPROVED:-none})"
  semver "${VERSION:-}" || die "VERSION '${VERSION:-}' is not X.Y.Z"
  [ "$(tree_version "$here")" = "$VERSION" ] || die "the tree is $(tree_version "$here"), not $VERSION"
  _ref=refs/tags/v$VERSION
  _tags=$(git ls-remote --tags "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY" "$_ref" "$_ref^{}") \
    || die "cannot list the tags of $GITHUB_REPOSITORY"
  # An annotated tag's commit is its peeled line.
  _at=$(printf '%s\n' "$_tags" | awk -v r="$_ref" '$2 == r "^{}" { p = $1 } $2 == r { t = $1 } END { print p ? p : t }')
  _head=$(git -C "$here" rev-parse HEAD)
  [ "$_at" = "$_head" ] || die "v$VERSION is at '${_at:-no tag}', this run is at $_head"
  _none=$(current)
  [ -z "$_none" ] || die "$QUAY:$MAJOR exists (${_none#* }); bootstrap runs once, before the first release"
}

output() {
  if [ -n "${GITHUB_OUTPUT:-}" ]; then printf '%s=%s\n' "$1" "$2" >> "$GITHUB_OUTPUT"; fi
  say "$1=$2"
}

# ------------------------------------------------------------------- registry

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
trap 'exit 2' HUP INT TERM
mkdir "$work/auth" "$work/keys"
AUTH=$work/auth/config.json

login() {
  [ -n "${SDLC_QUAY_USERNAME:-}" ] && [ -n "${SDLC_QUAY_PASSWORD:-}" ] \
    || die "no SDLC_QUAY_USERNAME/SDLC_QUAY_PASSWORD: sdlc's own robot is the only writer of $QUAY"
  printf '%s' "$SDLC_QUAY_PASSWORD" \
    | podman login --authfile "$AUTH" -u "$SDLC_QUAY_USERNAME" --password-stdin quay.io > /dev/null
  # cosign's image runs as nonroot, and podman login writes the file 0600:
  # unreadable, cosign fails every call, and a verify reads as "not signed".
  # $work is mktemp's 0700, so nobody else on the host can reach it.
  chmod 0755 "$work/auth"
  chmod 0644 "$AUTH"
}

# sdlc's public keys, the ones sdlc-bootstrap trusts (two during a rotation).
load_keys() {
  for _k in "$here"/boot/keys/*.pub; do
    [ -e "$_k" ] && cp "$_k" "$work/keys/"
  done
  ls "$work/keys/"*.pub > /dev/null 2>&1 || die "boot/keys holds no sdlc public key"
}

# cosign from the digest the workflow pins, never from anything this run built.
# The key reaches it only through `key`, and only as the env names cosign reads.
cosign_image() {
  case ${COSIGN_IMAGE:-} in
    ghcr.io/sigstore/cosign/cosign@sha256:*) digest_ok "${COSIGN_IMAGE#*@}" && return ;;
  esac
  die "COSIGN_IMAGE '${COSIGN_IMAGE:-}' is not ghcr.io/sigstore/cosign/cosign@sha256:<64 hex>"
}
cosign() {
  podman run --rm --cap-drop=all --security-opt=no-new-privileges --read-only \
    -v "$work/auth:/auth:ro,z" -v "$work/keys:/keys:ro,z" -e DOCKER_CONFIG=/auth \
    -- "$COSIGN_IMAGE" "$@"
}
cosign_key() {
  COSIGN_PASSWORD=${SDLC_KEY_PASSWORD:-} podman run --rm --cap-drop=all \
    --security-opt=no-new-privileges --read-only \
    -v "$work/auth:/auth:ro,z" -v "$work/keys:/keys:ro,z" -e DOCKER_CONFIG=/auth \
    -e SDLC_KEY -e COSIGN_PASSWORD -- "$COSIGN_IMAGE" "$@"
}

# The one version sdlc's key signed $1 with for repo $2, or nothing when no
# key verifies it. When none does, cosign's own words go to stderr: "not
# signed" and "could not reach the registry" read the same to the caller, and
# only they tell apart. Verified but with no version, or with two, is neither
# signed nor unsigned: that is a 1, and every caller dies on it rather than
# add a signature beside it.
signed_version() {
  : > "$work/verify.err"
  for _k in "$work/keys/"*.pub; do
    if cosign verify --key "/keys/${_k##*/}" --insecure-ignore-tlog=true \
         -a "repo=$2" "$1" > "$work/verify.json" 2>> "$work/verify.err"; then
      _vs=$(jq -r '[.[].optional.version // empty] | unique | .[]' "$work/verify.json")
      if [ "$(printf '%s\n' "$_vs" | grep -c .)" -ne 1 ]; then
        printf "self.sh: %s is signed by sdlc's key as repo=%s with versions '%s', not one\n" \
          "$1" "$2" "$(printf '%s' "$_vs" | tr '\n' ' ')" >&2
        return 1
      fi
      printf '%s' "$_vs"
      return 0
    fi
  done
  printf "self.sh: %s is not verified by sdlc's key as repo=%s; cosign said:\n" "$1" "$2" >&2
  sed 's/^/self.sh:   /' "$work/verify.err" >&2
}
label_version() {
  skopeo inspect --authfile "$AUTH" --format '{{ index .Labels "version" }}' "docker://$1"
}
# The commit an image says it was built from, which must be a full sha.
revision_of() {
  _r=$(skopeo inspect --authfile "$AUTH" \
         --format '{{ index .Labels "org.opencontainers.image.revision" }}' "docker://$1") \
    || die "cannot read the revision label of $1"
  case $_r in *[!0-9a-f]*|'') die "$1's revision label '$_r' is not a commit sha" ;; esac
  [ "${#_r}" -eq 40 ] || die "$1's revision label '$_r' is not a commit sha"
  printf '%s' "$_r"
}
# The digest a tag names, or nothing when the tag does not exist. Any other
# failure is fatal: a guard that cannot read what it guards waves it through.
tag_digest() {
  if _d=$(skopeo inspect --authfile "$AUTH" --format '{{.Digest}}' "docker://$1" 2> "$work/err"); then
    printf '%s' "$_d"
  # Quay answers a tag that once existed with "was deleted or has expired".
  elif ! grep -qiE 'manifest unknown|was deleted or has expired' "$work/err"; then
    cat "$work/err" >&2
    die "cannot read $1"
  fi
}
# :<major> as "<digest> <version>", or nothing when it does not exist yet. Its
# signature annotation and label must agree, or nothing here trusts it.
current() {
  _cd=$(tag_digest "$QUAY:$MAJOR")
  [ -n "$_cd" ] || return 0
  _cv=$(signed_version "$QUAY@$_cd" crunchtools/sdlc) \
    || die "$QUAY:$MAJOR ($_cd) carries an ambiguous sdlc signature"
  _cl=$(label_version "$QUAY@$_cd")
  [ -n "$_cv" ] && [ "$_cv" = "$_cl" ] \
    || die "$QUAY:$MAJOR ($_cd) carries annotation '$_cv' and label '$_cl'; they must agree"
  printf '%s %s' "$_cd" "$_cv"
}
# Copies a digest onto a tag without rewrapping it, and reads it back.
retag() {
  skopeo copy --authfile "$AUTH" --all --preserve-digests "docker://$1@$2" "docker://$1:$3"
  [ "$(tag_digest "$1:$3")" = "$2" ] || die "$1:$3 does not resolve to $2 after the copy"
  say "moved $1:$3 to $2"
}

# --------------------------------------------------------------------- modes

# The workflow's fetch is `git init` plus a fetch by URL, so the checkout has
# no origin. The constitution validator names the repo from origin and falls
# back to the directory, which `sdlc ci` mounts as /repo: without this, sdlc's
# `sdlc_exempt` entry never matches and XII judges it as a fleet repo (#105).
name_origin() {
  git -C "$here" remote get-url origin > /dev/null 2>&1 \
    || git -C "$here" remote add origin \
         "${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-crunchtools/sdlc}"
}

mode_test() {
  [ "${GITHUB_EVENT_NAME:-}" = pull_request ] || die "test runs on pull_request only"
  name_origin
  _sha=$(git -C "$here" rev-parse HEAD)
  build "$here" "localhost/sdlc:$_sha" "localhost/sdlc-bootstrap:$_sha"
  test_all "localhost/sdlc:$_sha" "localhost/sdlc-bootstrap:$_sha"
}

mode_check() {
  case ${GITHUB_EVENT_NAME:-} in push|schedule|workflow_dispatch) ;;
    *) die "check never runs on ${GITHUB_EVENT_NAME:-no event}: a PR's code runs only in test" ;; esac
  name_origin
  _sha=$(git -C "$here" rev-parse HEAD)
  build "$here" "localhost/sdlc:$_sha" "localhost/sdlc-bootstrap:$_sha"
  test_all "localhost/sdlc:$_sha" "localhost/sdlc-bootstrap:$_sha"
  [ "$GITHUB_EVENT_NAME" != schedule ] || keepalive
  # Not `output result "$(plan)"`: a plan that died would write an empty
  # result, which every consumer reads as "no release".
  _plan=$(plan) || exit 1
  output result "$_plan"
}

mode_image() {
  case $MODE:${GITHUB_EVENT_NAME:-} in release:push|weekly:schedule|bootstrap:workflow_dispatch) ;;
    *) die "image MODE=$MODE does not go with event ${GITHUB_EVENT_NAME:-none}" ;; esac
  login
  if [ "$MODE" = release ]; then
    # check's plan, not write's verdict: write tags only once this build and
    # its contract test have passed (a1667), and sign signs only what write
    # then released. A candidate is pushed as gated-<sha> and nothing more.
    _v=$(printf '%s' "${SDLC_CHECK:-}" | jq -r '.release.version // empty' 2> /dev/null) || _v=
    semver "$_v" || die "check planned no release X.Y.Z ('$_v'); nothing to build"
    [ "$(tree_version "$here")" = "$_v" ] || die "the tree is $(tree_version "$here"), check planned $_v"
    _src=$here
  elif [ "$MODE" = bootstrap ]; then
    load_keys
    cosign_image
    bootstrap_ok
    _v=$VERSION
    _src=$here
  else
    load_keys
    cosign_image
    _cur=$(current)
    [ -n "$_cur" ] || die "$QUAY:$MAJOR does not exist; there is no release to rebuild"
    _v=${_cur#* }
    # From the commit :<major>'s signed image says it was built from: never the
    # default branch, never the newest tag, and never the vX.Y.Z tag itself,
    # which anyone with contents: write (the Ashigaru App included) could move.
    # After a rollback the weekly run rebuilds the rolled-back version.
    _was=$(revision_of "$QUAY@${_cur%% *}")
    git -C "$here" fetch -q --depth=1 "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY" "$_was"
    git -C "$here" worktree add -q --detach "$work/src" "$_was"
    _src=$work/src
    [ "$(tree_version "$_src")" = "$_v" ] || die "$_was's tree is not version $_v"
  fi
  [ "$(major "$_v")" = "$MAJOR" ] || die "$_v is not of major $MAJOR, which boot/Containerfile follows"
  _rev=$(git -C "$_src" rev-parse HEAD)
  build "$_src" "localhost/sdlc:$_rev" "localhost/sdlc-bootstrap:$_rev"
  # The contract test of the tree being built, against what was built.
  "$_src/tests/contract.sh" "localhost/sdlc:$_rev"
  podman push -q --authfile "$AUTH" --digestfile "$work/sdlc.digest" \
    "localhost/sdlc:$_rev" "docker://$QUAY:gated-$_rev"
  _d=$(cat "$work/sdlc.digest")
  digest_ok "$_d" || die "the push reported no digest"
  # sdlc-bootstrap only on a release: the fleet runs it by a digest a fleet edit
  # names, so rebuilding it weekly would publish digests nobody runs.
  _b=
  if [ "$MODE" != weekly ]; then
    podman push -q --authfile "$AUTH" --digestfile "$work/boot.digest" \
      "localhost/sdlc-bootstrap:$_rev" "docker://$BOOT:gated-$_rev"
    _b=$(cat "$work/boot.digest")
    digest_ok "$_b" || die "the sdlc-bootstrap push reported no digest"
  fi
  output result "$(jq -cn --arg d "$_d" --arg v "$_v" --arg b "$_b" '{digest: $d, version: $v, boot: $b}')"
}

# Signs $1@$2 for repo annotation $3 at version $4, unless sdlc's key already
# did (a re-run). A signature with another version is a refusal, not a skip.
sign_one() {
  _have=$(signed_version "$1@$2" "$3") \
    || die "$1@$2 carries an ambiguous sdlc signature; refusing to add another"
  if [ -z "$_have" ]; then
    cosign_key sign --yes --tlog-upload=false --key env://SDLC_KEY \
      -a "repo=$3" -a "version=$4" "$1@$2"
    _have=$(signed_version "$1@$2" "$3") || die "$1@$2 is ambiguous after signing"
  fi
  [ "$_have" = "$4" ] || die "$1@$2 is signed as '$_have', not $4"
  say "signed $1@$2 as $4"
}

mode_sign() {
  case $MODE:${EVENT:-} in release:push|weekly:schedule|rollback:workflow_dispatch|bootstrap:workflow_dispatch) ;;
    *) die "sign MODE=$MODE does not go with event ${EVENT:-none}" ;; esac
  [ -n "${SDLC_KEY:-}" ] || die "no SDLC_KEY: it lives in the sdlc-signing environment only"
  cosign_image
  load_keys
  login
  # The private key must be the other half of a key sdlc-bootstrap trusts.
  cosign_key public-key --key env://SDLC_KEY > "$work/secret.pub"
  _match=
  for _k in "$work/keys/"*.pub; do cmp -s "$work/secret.pub" "$_k" && _match=1; done
  [ -n "$_match" ] || die "SDLC_KEY is not the private half of any key in boot/keys"

  if [ "$MODE" = rollback ]; then
    [ "${APPROVED:-}" = success ] || die "rollback needs the sdlc-approval job to have succeeded (${APPROVED:-none})"
    semver "${VERSION:-}" || die "VERSION '${VERSION:-}' is not X.Y.Z"
    [ "$(major "$VERSION")" = "$MAJOR" ] || die "$VERSION is not of major $MAJOR"
    [ "$(vcmp "$VERSION" "$MINIMUM")" -ge 0 ] || die "$VERSION is below sdlc-bootstrap's minimum $MINIMUM"
    digest_ok "${DIGEST:-}" || die "DIGEST '${DIGEST:-}' is not sha256:<64 hex>"
    _sv=$(signed_version "$QUAY@$DIGEST" crunchtools/sdlc) \
      || die "$QUAY@$DIGEST carries an ambiguous sdlc signature"
    [ "$_sv" = "$VERSION" ] || die "$QUAY@$DIGEST is not signed by sdlc's key as $VERSION ('$_sv')"
    [ "$(label_version "$QUAY@$DIGEST")" = "$VERSION" ] || die "$QUAY@$DIGEST's version label is not $VERSION"
    # Nothing is built or signed: only :<major> moves.
    retag "$QUAY" "$DIGEST" "$MAJOR"
    return
  fi

  # image's output is trusted for one thing, which digest it built.
  _d=$(printf '%s' "${BUILT:-}" | jq -r '.digest // empty' 2> /dev/null) || _d=
  digest_ok "$_d" || die "image's output names no sha256 digest: ${BUILT:-}"
  _v=$(label_version "$QUAY@$_d")
  semver "$_v" || die "$QUAY@$_d's version label '$_v' is not X.Y.Z"
  [ "$(major "$_v")" = "$MAJOR" ] || die "$_v is not of major $MAJOR"
  _cur=$(current)
  if [ "$MODE" = bootstrap ]; then
    bootstrap_ok
    [ "$_v" = "$VERSION" ] || die "the image is $_v, the bootstrap is of $VERSION"
  fi
  if [ "$MODE" != weekly ]; then
    [ "$(tree_version "$here")" = "$_v" ] || die "the release merge is $(tree_version "$here"), the image is $_v"
    [ -z "$_cur" ] || [ "$(vcmp "$_v" "${_cur#* }")" -gt 0 ] \
      || die "$_v is not newer than ${_cur#* }, which $QUAY:$MAJOR carries"
  else
    [ -n "$_cur" ] || die "$QUAY:$MAJOR does not exist; a weekly run rebuilds only what it carries"
    [ "$_v" = "${_cur#* }" ] || die "the rebuild is $_v, $QUAY:$MAJOR carries ${_cur#* }; weekly neither undoes a rollback nor releases"
    # The same version is not enough: it must be the same commit, or a moved
    # tag would have Monday's run sign other code under this version.
    _was=$(revision_of "$QUAY@${_cur%% *}")
    _now=$(revision_of "$QUAY@$_d")
    [ "$_now" = "$_was" ] || die "the rebuild is of $_now, $QUAY:$MAJOR was built from $_was; refusing"
  fi
  if [ "$MODE" = release ]; then
    # write verified the merge and tagged it after image passed; a skipped or
    # failed write signs nothing, and its version must be the image's.
    [ "${SDLC_WRITE:-}" = success ] || die "write did not succeed (${SDLC_WRITE:-none}); nothing to sign"
    _rv=$(printf '%s' "${SDLC_RELEASE:-}" | jq -r '.version // empty' 2> /dev/null) || _rv=
    [ "$_rv" = "$_v" ] || die "write released '$_rv', the image is $_v"
  fi
  # sdlc-bootstrap is checked before anything is signed, so a refusal signs nothing.
  _b=
  [ "$MODE" = weekly ] || _b=$(printf '%s' "$BUILT" | jq -r '.boot // empty')
  if [ -n "$_b" ]; then
    digest_ok "$_b" || die "image's output names a malformed sdlc-bootstrap digest: $_b"
    [ "$(label_version "$BOOT@$_b")" = "$_v" ] || die "$BOOT@$_b is not labelled $_v"
    _bs=$(signed_version "$BOOT@$_b" crunchtools/sdlc-bootstrap) \
      || die "$BOOT@$_b carries an ambiguous sdlc signature"
    [ -z "$_bs" ] || [ "$_bs" = "$_v" ] || die "$BOOT@$_b is signed as '$_bs', not $_v"
  fi
  sign_one "$QUAY" "$_d" crunchtools/sdlc "$_v"
  [ -z "$_b" ] || sign_one "$BOOT" "$_b" crunchtools/sdlc-bootstrap "$_v"

  # Read again, inside the sdlc-sign concurrency group, immediately before the
  # move: a weekly run queued behind a release finds :<major> moved and stops.
  # On its own line: inside `[ "$(current)" = ... ]` a die in current() would
  # only end the substitution, and on a first release "" = "" would retag.
  _again=$(current)
  [ "$_again" = "$_cur" ] || die "$QUAY:$MAJOR moved while this run was signing; refusing"
  retag "$QUAY" "$_d" "$_v"
  retag "$QUAY" "$_d" "$MAJOR"
  if [ "$MODE" != weekly ]; then
    retag "$QUAY" "$_d" latest
    [ -z "$_b" ] || retag "$BOOT" "$_b" "$_v"
  fi
}

MODE=${MODE:-}
MAJOR=$(boot_arg SDLC_MAJOR)
MINIMUM=$(boot_arg SDLC_MIN_VERSION)
case $MAJOR in ''|*[!0-9]*) cannot "boot/Containerfile names no ARG SDLC_MAJOR" ;; esac
semver "$MINIMUM" || cannot "boot/Containerfile names no ARG SDLC_MIN_VERSION=X.Y.Z"

case ${GITHUB_JOB:-}:$MODE in
  test:test)   mode_test ;;
  check:check) mode_check ;;
  image:release|image:weekly|image:bootstrap) mode_image ;;
  sign:release|sign:weekly|sign:rollback|sign:bootstrap) mode_sign ;;
  *) cannot "no step for job '${GITHUB_JOB:-}' in MODE '$MODE'" ;;
esac
