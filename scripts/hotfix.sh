#!/bin/sh
# Break glass: release the sdlc tree checked out here from a host, without the
# 40-minute release pipeline. It runs self.sh's own image and sign steps, so
# every check they make still holds: the version must be newer than :<major>,
# the key must be the other half of one boot/keys trusts, the contract test
# runs against the image, and :<major> moves only if nobody moved it meanwhile.
#
# Between the image and sign steps it tags vX.Y.Z as `write` tags a release
# merge, with the Ashigaru App's token: tag ruleset 27841014 lets only the App
# create v*.*.* tags. The App's key never leaves this script, which signs the
# App's JWT with openssl and mints `write`'s TAG token (sdlc_jobs/write.py),
# for this repo alone. Only that token, on stdin, reaches the image just built,
# where sdlc_jobs/_hotfix.py runs Ashigaru's release_publish; it is revoked on
# exit.
#
# The secrets arrive on stdin as one JSON object, never on argv or disk:
#   {"SDLC_KEY": ..., "SDLC_KEY_PASSWORD": ..., "SDLC_QUAY_USERNAME": ..., "SDLC_QUAY_PASSWORD": ...,
#    "ASHIGARU_CLIENT_ID": ..., "ASHIGARU_APP_KEY": ...}
# The tree must be a commit pushed to crunchtools/sdlc (the revision label
# names it, and the weekly rebuild fetches it), with its version already bumped.
#
# Usage: <secrets.json scripts/hotfix.sh
set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
[ -z "$(git -C "$here" status --porcelain)" ] || { echo "hotfix: the tree is dirty" >&2; exit 1; }

for tool in jq openssl curl podman; do
  command -v "$tool" > /dev/null || { echo "hotfix: needs $tool" >&2; exit 1; }
done

secrets=$(cat)
field() {
  printf '%s' "$secrets" | jq -er --arg n "$1" '.[$n] // empty' \
    || { echo "hotfix: $1 is missing from stdin" >&2; exit 1; }
}
for name in SDLC_KEY SDLC_KEY_PASSWORD SDLC_QUAY_USERNAME SDLC_QUAY_PASSWORD; do
  value=$(field "$name")
  export "$name=$value"
done
# The App's two stay unexported: neither self.sh nor any container sees them.
app_id=$(field ASHIGARU_CLIENT_ID)
app_key=$(field ASHIGARU_APP_KEY)
unset secrets value

version=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$here/pyproject.toml" | head -1)
sha=$(git -C "$here" rev-parse HEAD)
work=$(mktemp -d)
token=
API=https://api.github.com
# The token is revoked however the run ends; it expires within the hour anyway.
trap '[ -z "$token" ] || printf "Authorization: Bearer %s\n" "$token" \
  | curl -fsS -o /dev/null -X DELETE -H @- "$API/installation/token" \
  || echo "hotfix: could not revoke the App token" >&2; rm -rf "$work"' EXIT

# GitHub's REST API with the bearer token $1, on curl's stdin and never argv.
api() {
  _bearer=$1; shift
  printf 'Authorization: Bearer %s\nAccept: application/vnd.github+json\n' "$_bearer" \
    | curl -fsS -H @- "$@"
}
b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
# write's TAG token (sdlc_jobs/write.py TAG), for crunchtools/sdlc alone, as
# sdlc_jobs/_app_token.py mints it: a JWT backdated a minute that lives nine.
TAG_BODY='{"repositories":["sdlc"],"permissions":{"contents":"write","pull_requests":"read"}}'
# Every step is checked: `token=$(mint) || ...` runs it with set -e off.
mint() {
  _now=$(date +%s)
  _head=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
  _claims=$(jq -cn --arg iss "$app_id" --argjson t "$_now" '{iat: ($t - 60), exp: ($t + 540), iss: $iss}' | b64url)
  printf '%s.%s' "$_head" "$_claims" > "$work/jwt"
  printf '%s\n' "$app_key" | openssl dgst -sha256 -sign /dev/stdin -out "$work/sig" "$work/jwt" || return 1
  _jwt=$_head.$_claims.$(b64url < "$work/sig")
  _inst=$(api "$_jwt" "$API/repos/$GITHUB_REPOSITORY/installation" | jq -er .id) || return 1
  api "$_jwt" -X POST -d "$TAG_BODY" "$API/app/installations/$_inst/access_tokens" | jq -er .token
}

export COSIGN_IMAGE=ghcr.io/sigstore/cosign/cosign@sha256:ad281047f85c5e1fc6ffbc30c2b55be3b07b4032bef715a12122ce5829619aca
export GITHUB_SERVER_URL=https://github.com GITHUB_REPOSITORY=crunchtools/sdlc

GITHUB_JOB=image MODE=release GITHUB_EVENT_NAME=push SDLC_WRITE=success SDLC_RELEASE=$version \
  GITHUB_OUTPUT=$work/out SDLC_KEY='' SDLC_KEY_PASSWORD='' "$here/scripts/self.sh"
built=$(sed -n 's/^result=//p' "$work/out")
[ -n "$built" ] || { echo "hotfix: the image step named no digest" >&2; exit 1; }

# The tag before sign, as `write` tags before image and sign run. A re-run
# finds its own tag and goes on.
token=$(mint) || { echo "hotfix: could not mint the App's TAG token" >&2; exit 1; }
unset app_key
printf '%s' "$token" | podman run --rm -i \
  --entrypoint /opt/constitution-venv/bin/python3 "localhost/sdlc:$sha" \
  -I -m sdlc_jobs._hotfix "$GITHUB_REPOSITORY" "$version" "$sha"

GITHUB_JOB=sign MODE=release EVENT=push BUILT=$built "$here/scripts/self.sh"
echo "hotfix: $version is quay.io/crunchtools/sdlc:${version%%.*}"
