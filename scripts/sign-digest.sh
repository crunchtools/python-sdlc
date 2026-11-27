#!/bin/sh
# Sign one tested digest in each named repository with the fleet key, unless
# it is already signed, and prove each signature verifies (#81). container.yml's
# promote used it; since that was retired (a1790) sdlc_jobs/promote.py does this
# for the fleet, and nothing calls this script.
#
# The public key is the one bin/sdlc-bootstrap trusts, read out of it, and the
# private key must be its other half: the key that signs and the key every
# repo's gate checks cannot drift apart. A signature is an attachment in the
# repository it was made in, and skopeo copy does not carry it across, so
# each repository is signed on its own.
#
# Needs cosign (v2.6.5, or v3 in the legacy format the flags below keep) on PATH, logged in to the registry, and COSIGN_PRIVATE_KEY
# (with COSIGN_PASSWORD) in the environment.
#
# Usage: scripts/sign-digest.sh <sha256:digest> <registry/repo>...
set -eu

usage='usage: scripts/sign-digest.sh <sha256:digest> <registry/repo>...'
digest=${1:?$usage}
shift
[ "$#" -gt 0 ] || { echo "$usage" >&2; exit 1; }
not_digest() { echo "the digest to sign is not a sha256 digest: $digest" >&2; exit 1; }
hex=${digest#sha256:}
case $digest in sha256:*) ;; *) not_digest ;; esac
case $hex in *[!0-9a-f]*) not_digest ;; esac
[ "${#hex}" -eq 64 ] || not_digest
[ -n "${COSIGN_PRIVATE_KEY:-}" ] || { echo "no COSIGN_PRIVATE_KEY in the cosign-signing environment" >&2; exit 1; }

here=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
pub=$work/fleet.pub
# The fleet key by name: bin/sdlc-bootstrap also carries sdlc's own key.
sed -n "/<<'FLEET_KEY'\$/,/^FLEET_KEY\$/p" "$here/bin/sdlc-bootstrap" | sed '1d;$d' > "$pub"
[ -s "$pub" ] || { echo "bin/sdlc-bootstrap carries no fleet key" >&2; exit 1; }
cosign public-key --key env://COSIGN_PRIVATE_KEY > "$work/secret.pub"
cmp -s "$work/secret.pub" "$pub" \
  || { echo "COSIGN_PRIVATE_KEY is not the private half of the key bin/sdlc-bootstrap trusts" >&2; exit 1; }

for repo in "$@"; do
  if cosign verify --insecure-ignore-tlog --key "$pub" "$repo@$digest" > /dev/null 2>&1; then
    echo "$repo@$digest is already signed"
  else
    cosign sign --yes --tlog-upload=false --use-signing-config=false --new-bundle-format=false \
      --key env://COSIGN_PRIVATE_KEY "$repo@$digest"
  fi
  cosign verify --insecure-ignore-tlog --key "$pub" "$repo@$digest" > /dev/null
  echo "signed $repo@$digest"
done
