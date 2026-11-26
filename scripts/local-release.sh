#!/bin/sh
# Release one fleet repo's commit from a host, without GitHub Actions: the
# fleet ci.yml's check, image, boot, write, promote and pypi jobs, run in that order by the
# same pinned sdlc-bootstrap the workflow runs, on this host's podman socket,
# with the environment the workflow would give each. Nothing here re-implements
# a job: check gates the commit (`sdlc ci`), image verifies every parent and
# builds FROM its digest, gates, and pushes gated-<sha>, boot runs the repo's
# boot proof, and promote signs and attests with the fleet key and moves
# `latest`, plus `X.Y.Z` when the commit is the one tag vX.Y.Z names. Last, the
# pushed digest is verified here against the fleet public key, independently of
# promote's own check.
#
# Run as root on Exedore (podman, a socket, the disk). The secrets arrive on
# stdin as one JSON object, never on argv or disk, and each job is handed only
# the ones its ci.yml job holds, by name:
#   {"COSIGN_PRIVATE_KEY", "COSIGN_PASSWORD", "QUAY_USERNAME", "QUAY_PASSWORD",
#    "ASHIGARU_APP_KEY", and optionally "RHSM_ACTIVATION_KEY", "RHSM_ORG_ID",
#    "REGISTRY_REDHAT_IO_USER", "REGISTRY_REDHAT_IO_PASSWORD", "PYPI_API_TOKEN"}
# scripts/local-release-secrets.sh writes exactly that from OpenBao on Gloval.
#
# The jobs' GITHUB_TOKEN is an Ashigaru App token minted here for this one
# repository with contents, metadata and pull requests read, what ci.yml's
# github.token holds, and revoked on exit. The commit must be on the default
# branch, as it must be for ci.yml's image and promote. The event is the push
# of that commit, as CI saw it: when it merges an App-authored release pull
# request, check plans the release, and once image and boot have passed,
# ci.yml's `write` job runs here too, with the App key, to verify the merge,
# tag vX.Y.Z and publish the GitHub Release (only the App may create v* tags);
# a failed gate tags nothing. promote then moves X.Y.Z with
# `latest`. Any other commit moves `latest` alone. Like CI, check runs no
# gates when the merged pull request's check passed on the same tree; image's
# container gates always run. The image records no Actions run, so its SLSA
# provenance names this script, at the sdlc commit it ran from, as the builder
# (promote's local_provenance), never a workflow run that did not happen.
#
# Last, when check's plan says the release publishes to PyPI, ci.yml's `pypi`
# job runs. No Actions run means no OIDC token, so it publishes with the PyPI
# API token from stdin, PYPI_API_TOKEN, handed to that job alone as
# UV_PUBLISH_TOKEN; the job keeps it out of its nested build. A run whose plan
# publishes to PyPI and whose stdin holds no token stops before image.
#
# --pypi publishes a release already tagged that never reached PyPI: the commit
# is the one tag vX.Y.Z names, and only the pypi job runs, which re-derives the
# release from the API as it does after write. A re-run is safe: files already
# on PyPI with the same hash are skipped.
#
# Usage: <secrets.json scripts/local-release.sh [--pypi] <owner/repo> <40-hex commit>
set -eu

usage='usage: <secrets.json scripts/local-release.sh [--pypi] <owner/repo> <40-hex commit>'
mode=release
[ "${1:-}" != --pypi ] || { mode=pypi; shift; }
repo=${1:?$usage}
sha=${2:?$usage}
die() { printf 'local-release: %s\n' "$*" >&2; exit 1; }
say() { printf 'local-release: %s\n' "$*"; }
case $repo in */*/*|/*|*/|*[!A-Za-z0-9._/-]*) die "$repo is not owner/name" ;; */*) ;; *) die "$repo is not owner/name" ;; esac
case $sha in *[!0-9a-f]*) die "$sha is not a 40-hex commit" ;; esac
[ "${#sha}" -eq 40 ] || die "$sha is not a 40-hex commit"
[ "$(id -u)" -eq 0 ] || die "run as root: the jobs drive the host's podman socket"
for tool in podman skopeo jq curl openssl unshare flock git; do
  command -v "$tool" > /dev/null || die "$tool is not on PATH"
done

project=${repo#*/}
here=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
# One run of a project at a time: cleanup removes the images this commit labels
# and every localhost/<project> image, which a second run of it is still using.
# By project, not owner/name, because the launcher names its image by project.
# Taken before the cleanup trap, so a run that waits here removes nothing.
mkdir -p /run/lock/sdlc-local-release
exec 8> "/run/lock/sdlc-local-release/$project.lock"
flock -n 8 || { say "waiting for the other run of $project"; flock 8; }
# The builder promote's provenance names: this script at the commit it ran from.
rev=$(git -C "$here" rev-parse HEAD 2> /dev/null) || die "$here is not a git checkout of sdlc"
[ -z "$(git -C "$here" status --porcelain -- scripts sdlc_jobs bin examples)" ] \
  || die "$here has uncommitted changes: the provenance would name a commit that did not run"
SDLC_LOCAL_BUILDER=https://github.com/crunchtools/sdlc/blob/$rev/scripts/local-release.sh
# The fleet file's own pins, read out of it so this loop cannot drift from CI.
bootstrap=$(grep -o 'quay\.io/crunchtools/sdlc-bootstrap@sha256:[0-9a-f]\{64\}' "$here/examples/ci.yml" | head -1)
[ -n "$bootstrap" ] || die "examples/ci.yml pins no sdlc-bootstrap digest"
COSIGN_IMAGE=$(sed -n 's/.*COSIGN_IMAGE: "\(ghcr\.io\/sigstore\/cosign\/cosign@sha256:[0-9a-f]\{64\}\)".*/\1/p' \
  "$here/.github/workflows/ci.yml" | head -1)
[ -n "$COSIGN_IMAGE" ] || die ".github/workflows/ci.yml pins no cosign digest"
# Ashigaru's client id is public (any comment it makes names it); the key is not.
ASHIGARU_CLIENT_ID=Iv23li5NqBVUWVPf9ONu
API=https://api.github.com

secrets=$(cat)
for name in COSIGN_PRIVATE_KEY COSIGN_PASSWORD QUAY_USERNAME QUAY_PASSWORD ASHIGARU_APP_KEY; do
  value=$(printf '%s' "$secrets" | jq -er --arg n "$name" '.[$n] // empty') \
    || die "$name is missing from stdin"
  export "$name=$value"
done
for name in RHSM_ACTIVATION_KEY RHSM_ORG_ID REGISTRY_REDHAT_IO_USER REGISTRY_REDHAT_IO_PASSWORD; do
  value=$(printf '%s' "$secrets" | jq -r --arg n "$name" '.[$n] // empty')
  [ -z "$value" ] || export "$name=$value"
done
# Not exported: it reaches the pypi job alone, as UV_PUBLISH_TOKEN, and only then.
pypi_token=$(printf '%s' "$secrets" | jq -r '.PYPI_API_TOKEN // empty')
[ "$mode" != pypi ] || [ -n "$pypi_token" ] || die "PYPI_API_TOKEN is missing from stdin"
unset secrets value

umask 022
work=$(mktemp -d /var/tmp/sdlc-local.XXXXXX)
# World-readable, as a runner workspace is: the gates run in containers of their
# own, and the tree, the event and the outputs hold no secret.
chmod 0755 "$work"
GITHUB_TOKEN='' service=''
# The jobs' images live in a podman storage of their own on Exedore's big onsite
# disk, not root's graphroot on /var: a release build is tens of GB. One storage
# and one runroot for every run, so overlapping runs share them under podman's
# own locks. Root's own podman is left as it is.
store=/var/mnt/onsite/sdlc-release
[ -d "${store%/*}" ] || die "${store%/*} is not mounted: the release storage lives there"
mkdir -p "$store/storage" "$store/tmp" /run/sdlc-release/runroot
pod() { podman --root "$store/storage" --runroot /run/sdlc-release/runroot "$@"; }
cleanup() {
  [ -z "$service" ] || kill "$service" 2> /dev/null || :
  [ -z "$GITHUB_TOKEN" ] || ghapi "$GITHUB_TOKEN" DELETE /installation/token > /dev/null 2>&1 \
    || echo "local-release: could not revoke the App token; it expires within the hour" >&2
  # Root-owned files the jobs left; the workspace holds no secret.
  rm -rf "$work"
  # The images this run built, which a runner VM would take with it; not
  # forced, so one a run beside this one still uses stays.
  for _id in $(pod images -q --filter "label=org.opencontainers.image.revision=$sha" 2> /dev/null | sort -u) \
    $(pod images --format '{{.Repository}}:{{.Tag}}' 2> /dev/null | grep "^localhost/${project}[:-]"); do
    pod rmi "$_id" > /dev/null 2>&1 || :
  done
}
trap cleanup EXIT
trap 'exit 2' HUP INT TERM

# One GitHub API call; the bearer goes to curl as config on stdin, never argv.
ghapi() {
  _tok=$1 _method=$2 _path=$3
  shift 3
  printf 'header = "Authorization: Bearer %s"\n' "$_tok" \
    | curl -fsS -K - -X "$_method" -H 'Accept: application/vnd.github+json' \
        -H 'X-GitHub-Api-Version: 2022-11-28' "$@" "$API$_path"
}
b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

# ------------------------------------------------------------------ the read token

now=$(date +%s)
printf '%s.%s' "$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)" \
  "$(printf '{"iat":%s,"exp":%s,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "$ASHIGARU_CLIENT_ID" | b64url)" \
  > "$work/jwt"
jwt=$(cat "$work/jwt").$(printf '%s\n' "$ASHIGARU_APP_KEY" \
  | openssl dgst -sha256 -binary -sign /dev/stdin "$work/jwt" | b64url)
installation=$(ghapi "$jwt" GET "/repos/$repo/installation" | jq -er .id) \
  || die "the Ashigaru App is not installed on $repo"
jq -n --arg r "${repo#*/}" '{repositories: [$r], permissions: {contents: "read", metadata: "read", pull_requests: "read"}}' \
  > "$work/token.json"
GITHUB_TOKEN=$(ghapi "$jwt" POST "/app/installations/$installation/access_tokens" \
  --data-binary "@$work/token.json" | jq -er .token) || die "the App would not mint a read token"
unset jwt
export GITHUB_TOKEN

default=$(ghapi "$GITHUB_TOKEN" GET "/repos/$repo" | jq -er .default_branch) || die "$repo could not be read"
# On the default branch, as ci.yml's image and promote require: the commit is
# the branch head or behind it.
status=$(ghapi "$GITHUB_TOKEN" GET "/repos/$repo/compare/$sha...$default" | jq -er .status) \
  || die "$sha is not a commit of $repo"
case $status in identical|ahead) ;; *) die "$sha is not on $repo's $default ($status)" ;; esac

# ------------------------------------------------------------------ the jobs

# The jobs' socket: a podman service of this run's own, every container it
# starts unlabelled. The bootstrap starts its sibling through the socket with
# no label=disable, and on this SELinux host, unlike a runner VM, a labelled
# container cannot read the workspace or reach the socket. The host's own
# podman.socket and its configuration are left as they are.
sock=$work/podman.sock
printf '[containers]\nlabel = false\n' > "$work/containers.conf"
# No host entitlement: on a RHEL host podman's mounts.conf puts the host's
# entitlement in every container's /run/secrets, and subscription-manager then
# refuses to register in a build's RHSM step ("operating in container mode").
# A runner VM mounts none. The service runs in a private mount namespace with an
# empty directory over that source, so only its containers lose it;
# --default-mounts-file is not honored by a build through the API.
mkdir "$work/no-secrets"
# podman itself, exec'd by env, unshare and sh: $! must be the service, for cleanup to stop it.
# The service holds none of this run's secrets, as a runner's podman holds none:
# a build secret whose id names a variable in the service's environment reaches
# the RUN empty, whatever its src= holds, so with RHSM_ACTIVATION_KEY and
# RHSM_ORG_ID exported a RUN --mount=type=secret,id=RHSM_* read nothing and
# subscription-manager said "Must specify an activation key" (a1679).
# shellcheck disable=SC2016 # $1 and $@ are the inner sh's
CONTAINERS_CONF_OVERRIDE=$work/containers.conf TMPDIR=$store/tmp \
  env -u COSIGN_PRIVATE_KEY -u COSIGN_PASSWORD -u QUAY_USERNAME -u QUAY_PASSWORD \
    -u ASHIGARU_APP_KEY -u RHSM_ACTIVATION_KEY -u RHSM_ORG_ID -u REGISTRY_REDHAT_IO_USER \
    -u REGISTRY_REDHAT_IO_PASSWORD -u GITHUB_TOKEN -u PYPI_API_TOKEN -u UV_PUBLISH_TOKEN \
  unshare -m --propagation private sh -c \
    'mount --bind "$1" /usr/share/rhel/secrets 2> /dev/null || [ ! -d /usr/share/rhel/secrets ] || exit 1; shift; exec "$@"' \
    sh "$work/no-secrets" podman --root "$store/storage" --runroot /run/sdlc-release/runroot \
      system service --time=0 "unix://$sock" 8>&- &
service=$!
tries=0
until [ -S "$sock" ]; do
  tries=$((tries + 1))
  [ "$tries" -le 50 ] || die "the podman service did not start"
  sleep 0.2
done
jq -n --arg r "$repo" --arg d "$default" --arg s "$sha" \
  '{repository: {full_name: $r, default_branch: $d}, ref: ("refs/heads/" + $d), after: $s, head_commit: {id: $s}}' \
  > "$work/event.json"

# No Actions run exists, so none is named; the attempt alone is set, unique,
# because image names its cache volume by it and runs here can overlap, and
# promote's local provenance holds the image's run label to this attempt.
attempt=$(date +%s)$$
# The bootstrap verifies the FIRST of sdlc:<major>'s RepoDigests. An image
# re-pushed under a new manifest (a hotfix's unsigned push, then its signed
# one) keeps every digest it was pulled by, the old, unsigned one first, so
# the bootstrap refuses it after every pull. When the host's copy names more
# than the digest the tag has now, it is removed and pulled fresh; not
# forced, so a run beside this one keeps its image. The bootstrap still
# verifies what it gets.
current=$(skopeo inspect --format '{{.Digest}}' docker://quay.io/crunchtools/sdlc:0) \
  || die "quay.io/crunchtools/sdlc:0 could not be inspected"
held() {
  pod image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' quay.io/crunchtools/sdlc:0 2> /dev/null \
    | grep '^quay\.io/crunchtools/sdlc@' || :
}
# One run at a time: two runs untagging and pulling at once undo each other.
exec 9> /run/lock/sdlc-local-release-sdlc0.lock
flock 9
if [ "$(held)" != "quay.io/crunchtools/sdlc@$current" ]; then
  # A new image: untagging the old one (which a container may still hold) is enough.
  pod untag quay.io/crunchtools/sdlc:0 2> /dev/null || :
  pod pull -q quay.io/crunchtools/sdlc:0 > /dev/null || die "quay.io/crunchtools/sdlc:0 could not be pulled"
  # The same image under a new manifest still lists the old digest: only removing it clears that.
  if [ "$(held)" != "quay.io/crunchtools/sdlc@$current" ]; then
    pod rmi quay.io/crunchtools/sdlc:0 > /dev/null \
      || die "the host's sdlc:0 names a stale digest and is in use; rerun when the other run ends"
    pod pull -q quay.io/crunchtools/sdlc:0 > /dev/null || die "quay.io/crunchtools/sdlc:0 could not be pulled"
  fi
fi
flock -u 9
# One ci.yml job: the fleet file's launch line, with that job's secrets by name.
# Its log has the runner's ::add-mask:: lines removed: no runner reads them here.
job() {
  _job=$1
  shift
  # Named for the repo, as a runner's workspace is: the launcher names the
  # image it builds (localhost/<dir>:sdlc) after this directory.
  mkdir -p "$work/$_job/$project" "$work/$_job/tmp"
  : > "$work/$_job/output"
  say "job $_job"
  set +e
  ( podman --remote --url "unix://$sock" run --rm --user 0 --security-opt label=disable \
      -v "$sock:/var/run/docker.sock" -v "$work:$work" -w "$work/$_job/$project" \
      -e CI=true -e GITHUB_ACTIONS=true -e RUNNER_OS=Linux -e RUNNER_ARCH=X64 \
      -e RUNNER_ENVIRONMENT=self-hosted -e "RUNNER_TEMP=$work/$_job/tmp" \
      -e "GITHUB_JOB=$_job" -e GITHUB_EVENT_NAME=push \
      -e "GITHUB_EVENT_PATH=$work/event.json" -e "GITHUB_OUTPUT=$work/$_job/output" \
      -e "GITHUB_WORKSPACE=$work/$_job/$project" -e "GITHUB_REPOSITORY=$repo" \
      -e "GITHUB_REPOSITORY_OWNER=${repo%%/*}" -e "GITHUB_SHA=$sha" \
      -e "GITHUB_REF=refs/heads/$default" -e "GITHUB_REF_NAME=$default" -e GITHUB_REF_TYPE=branch \
      -e "GITHUB_RUN_ATTEMPT=$attempt" -e GITHUB_SERVER_URL=https://github.com -e "GITHUB_API_URL=$API" \
      -e GITHUB_TOKEN "$@" -- "$bootstrap" event 2>&1
    echo "rc=$?" > "$work/$_job/rc" ) | { grep -v '^::add-mask::' || :; }
  set -e
  _rc=$(sed -n 's/^rc=//p' "$work/$_job/rc")
  [ "$_rc" = 0 ] || die "job $_job exited ${_rc:-?}"
}
# A job output from $GITHUB_OUTPUT, as `name<<EOF_x ... EOF_x` writes it.
output() {
  awk -v n="$2" '
    !inside && index($0, n "<<") == 1 { d = substr($0, length(n) + 3); inside = 1; v = ""; next }
    inside && $0 == d { inside = 0; out = v; next }
    inside { v = (v == "" ? $0 : v "\n" $0) }
    END { printf "%s", out }' "$work/$1/output"
}

# ci.yml's pypi job, last: the token by name and nothing else of this run's
# secrets, with write's result and release for the job's own refusal.
pypi() {
  export UV_PUBLISH_TOKEN="$pypi_token"
  SDLC_WRITE=success SDLC_RELEASE=$1 job pypi -e UV_PUBLISH_TOKEN -e SDLC_WRITE -e SDLC_RELEASE
  unset UV_PUBLISH_TOKEN
  say "published $repo $(printf '%s' "$1" | jq -r .version) to PyPI"
}

if [ "$mode" = pypi ]; then
  # Only the App may create v* tags, so a tag is write's: its version, for this commit.
  version=$(ghapi "$GITHUB_TOKEN" GET "/repos/$repo/tags?per_page=100" | jq -er --arg s "$sha" \
    '[.[] | select(.commit.sha == $s) | .name | select(test("^v[0-9]+\\.[0-9]+\\.[0-9]+$"))]
     | if length == 1 then .[0][1:] else error("tags") end') \
    || die "$sha is not the commit of exactly one vX.Y.Z tag of $repo"
  pypi "$(jq -cn --arg v "$version" --arg s "$sha" '{version: $v, sha: $s}')"
  exit 0
fi

job check
plan=$(output check result)
printf '%s' "$plan" | jq -e 'type == "object"' > /dev/null || die "check wrote no plan"
# Before image, so a release that cannot reach PyPI is never tagged.
publish=$(printf '%s' "$plan" | jq -r '.release.publish // empty')
[ "$publish" != pypi ] || [ -n "$pypi_token" ] \
  || die "the release publishes to PyPI and PYPI_API_TOKEN is missing from stdin"

registry='-e QUAY_USERNAME -e QUAY_PASSWORD'
for name in RHSM_ACTIVATION_KEY RHSM_ORG_ID REGISTRY_REDHAT_IO_USER REGISTRY_REDHAT_IO_PASSWORD; do
  eval "[ -z \"\${$name:-}\" ]" || registry="$registry -e $name"
done
# shellcheck disable=SC2086 # names only, never values
job image $registry
built=$(output image result)
printf '%s' "$built" | jq -e 'type == "object"' > /dev/null || die "image named no digest"

boot=skipped
if printf '%s' "$plan" | jq -e '.boot' > /dev/null; then
  SDLC_IMAGE_RESULT=$built job boot -e SDLC_IMAGE_RESULT
  boot=success
fi

# write, on a release merge, only now that image and boot have passed: a
# failed gate has already ended the run, so no vX.Y.Z or GitHub Release ever
# names an image that does not exist (a1667). The App key and nothing else, as
# in ci.yml, with the gates' results for write's own refusal.
release='' write=skipped
if printf '%s' "$plan" | jq -e '.release' > /dev/null; then
  ASHIGARU_CLIENT_ID=$ASHIGARU_CLIENT_ID SDLC_CHECK=$plan SDLC_IMAGE=success SDLC_BOOT=$boot \
    job write -e ASHIGARU_CLIENT_ID -e ASHIGARU_APP_KEY -e SDLC_CHECK -e SDLC_IMAGE -e SDLC_BOOT
  release=$(output write release) write=success
  printf '%s' "$release" | jq -e '.version' > /dev/null || die "write released nothing"
  say "tagged v$(printf '%s' "$release" | jq -r .version) and published its GitHub Release"
fi
unset ASHIGARU_APP_KEY

SDLC_CHECK=$plan SDLC_IMAGE_RESULT=$built SDLC_BOOT=$boot SDLC_WRITE=$write SDLC_RELEASE=$release \
  SDLC_LOCAL_BUILDER=$SDLC_LOCAL_BUILDER \
  job promote -e SDLC_CHECK -e SDLC_IMAGE_RESULT -e SDLC_BOOT -e SDLC_WRITE -e SDLC_RELEASE \
    -e SDLC_LOCAL_BUILDER \
    -e QUAY_USERNAME -e QUAY_PASSWORD -e COSIGN_PRIVATE_KEY -e COSIGN_PASSWORD

# ------------------------------------------------------------------ verify

sed -n "/<<'FLEET_KEY'\$/,/^FLEET_KEY\$/p" "$here/bin/sdlc-bootstrap" | sed '1d;$d' > "$work/fleet.pub"
[ -s "$work/fleet.pub" ] || die "bin/sdlc-bootstrap carries no fleet key"
printf '%s' "$built" | jq -r 'if .images then .images[] else . end | "\(.ref)@\(.digest)"' \
  | while read -r pinned; do
      pod run --rm --log-driver=none --pull=missing --security-opt label=disable \
        -v "$work/fleet.pub:/fleet.pub:ro" -- "$COSIGN_IMAGE" \
        verify --key /fleet.pub --insecure-ignore-tlog=true "$pinned" > /dev/null 2>&1 \
        || die "$pinned does not verify against the fleet key"
      say "released $pinned (signed by the fleet key)"
    done

[ "$publish" != pypi ] || pypi "$release"
