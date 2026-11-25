#!/bin/sh
# Break glass: release the sdlc tree checked out here from a host, without the
# 40-minute release pipeline. It runs self.sh's own image and sign steps, so
# every check they make still holds: the version must be newer than :<major>,
# the key must be the other half of one boot/keys trusts, the contract test
# runs against the image, and :<major> moves only if nobody moved it meanwhile.
#
# Between the image and sign steps it tags vX.Y.Z as `write` tags a release
# merge: the Ashigaru App's token, minted from its key inside the image just
# built, runs Ashigaru's release_publish (sdlc_jobs/_hotfix.py). Tag ruleset
# 27841014 lets only the App create v*.*.* tags; nobody's own push can.
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

secrets=$(cat)
for name in SDLC_KEY SDLC_KEY_PASSWORD SDLC_QUAY_USERNAME SDLC_QUAY_PASSWORD ASHIGARU_CLIENT_ID ASHIGARU_APP_KEY; do
  value=$(printf '%s' "$secrets" | jq -er --arg n "$name" '.[$n] // empty') \
    || { echo "hotfix: $name is missing from stdin" >&2; exit 1; }
  export "$name=$value"
done
unset secrets value

version=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$here/pyproject.toml" | head -1)
sha=$(git -C "$here" rev-parse HEAD)
out=$(mktemp)
trap 'rm -f "$out"' EXIT

export COSIGN_IMAGE=ghcr.io/sigstore/cosign/cosign@sha256:ad281047f85c5e1fc6ffbc30c2b55be3b07b4032bef715a12122ce5829619aca
export GITHUB_SERVER_URL=https://github.com GITHUB_REPOSITORY=crunchtools/sdlc

GITHUB_JOB=image MODE=release GITHUB_EVENT_NAME=push SDLC_WRITE=success SDLC_RELEASE=$version \
  GITHUB_OUTPUT=$out SDLC_KEY='' SDLC_KEY_PASSWORD='' "$here/scripts/self.sh"
built=$(sed -n 's/^result=//p' "$out")
[ -n "$built" ] || { echo "hotfix: the image step named no digest" >&2; exit 1; }

# The tag before sign, as `write` tags before image and sign run. Only the
# App's two names reach the container. A re-run finds its own tag and goes on.
podman run --rm -e ASHIGARU_CLIENT_ID -e ASHIGARU_APP_KEY \
  --entrypoint /opt/constitution-venv/bin/python3 "localhost/sdlc:$sha" \
  -I -m sdlc_jobs._hotfix "$GITHUB_REPOSITORY" "$version" "$sha"

GITHUB_JOB=sign MODE=release EVENT=push BUILT=$built "$here/scripts/self.sh"
echo "hotfix: $version is quay.io/crunchtools/sdlc:${version%%.*}"
