#!/bin/sh
# Attest a syft SPDX SBOM and SLSA v1 provenance to one signed digest in each
# named repository with the fleet key, unless it already carries them, and
# prove each attestation verifies. Used by container.yml's promote job after
# scripts/sign-digest.sh and before any tag moves (a997), as constitution's
# container-release.yml does for every other image (#203, #204).
#
# The SBOM is scanned and the predicate written ONCE, from the first
# repository, and the same bytes attested in each: cosign v2 keeps an
# attestation as a .att object in the repository it was made in, and skopeo
# copy does not carry it across, so each repository is attested on its own,
# as it is signed. A digest whose labels record no build run (built before
# a997) gets the SBOM and no provenance, with a warning, rather than a
# predicate naming the wrong builder (scripts/provenance.py).
#
# The key checks are sign-digest.sh's: the public key is the one
# bin/sdlc-bootstrap trusts, and the private key must be its other half.
#
# Needs cosign v2, syft, skopeo and python3 on PATH, logged in to the
# registry, and COSIGN_PRIVATE_KEY (with COSIGN_PASSWORD) in the environment.
#
# Usage: scripts/attest-digest.sh <sha256:digest> <registry/repo>...
set -eu

usage='usage: scripts/attest-digest.sh <sha256:digest> <registry/repo>...'
digest=${1:?$usage}
shift
[ "$#" -gt 0 ] || { echo "$usage" >&2; exit 1; }
not_digest() { echo "the digest to attest is not a sha256 digest: $digest" >&2; exit 1; }
hex=${digest#sha256:}
case $digest in sha256:*) ;; *) not_digest ;; esac
case $hex in *[!0-9a-f]*) not_digest ;; esac
[ "${#hex}" -eq 64 ] || not_digest
[ -n "${COSIGN_PRIVATE_KEY:-}" ] || { echo "no COSIGN_PRIVATE_KEY in the cosign-signing environment" >&2; exit 1; }

here=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
pub=$work/fleet.pub
sed -n '/^-----BEGIN PUBLIC KEY-----$/,/^-----END PUBLIC KEY-----$/p' \
  "$here/bin/sdlc-bootstrap" > "$pub"
[ -s "$pub" ] || { echo "bin/sdlc-bootstrap carries no public key" >&2; exit 1; }
cosign public-key --key env://COSIGN_PRIVATE_KEY > "$work/secret.pub"
cmp -s "$work/secret.pub" "$pub" \
  || { echo "COSIGN_PRIVATE_KEY is not the private half of the key bin/sdlc-bootstrap trusts" >&2; exit 1; }

first=$1
sbom=$work/sbom.spdx.json
prov=$work/provenance.json
attested() { cosign verify-attestation --insecure-ignore-tlog --type "$1" --key "$pub" "$2@$digest" > /dev/null 2>&1; }

# The predicates, made only if some repository still lacks one. Provenance is
# read off the image's own labels, never this run's (scripts/provenance.py).
need_sbom='' need_prov=''
for repo in "$@"; do
  attested spdxjson "$repo" || need_sbom=1
  attested slsaprovenance1 "$repo" || need_prov=1
done
if [ -n "$need_sbom" ]; then
  SYFT_CHECK_FOR_APP_UPDATE=false syft scan "registry:$first@$digest" -o "spdx-json=$sbom"
  [ -s "$sbom" ] || { echo "syft wrote no SBOM for $first@$digest" >&2; exit 1; }
fi
if [ -n "$need_prov" ]; then
  skopeo inspect "docker://$first@$digest" > "$work/inspect.json"
  python3 -I "$here/scripts/provenance.py" --out "$prov" < "$work/inspect.json"
fi

for repo in "$@"; do
  if attested spdxjson "$repo"; then
    echo "$repo@$digest already carries an SBOM"
  else
    cosign attest --yes --tlog-upload=false --type spdxjson \
      --predicate "$sbom" --key env://COSIGN_PRIVATE_KEY "$repo@$digest"
  fi
  cosign verify-attestation --insecure-ignore-tlog --type spdxjson --key "$pub" "$repo@$digest" > /dev/null
  echo "attested the SBOM of $repo@$digest"

  if attested slsaprovenance1 "$repo"; then
    echo "$repo@$digest already carries provenance"
  elif [ -e "$prov" ]; then
    cosign attest --yes --tlog-upload=false --type slsaprovenance1 \
      --predicate "$prov" --key env://COSIGN_PRIVATE_KEY "$repo@$digest"
  else
    continue
  fi
  cosign verify-attestation --insecure-ignore-tlog --type slsaprovenance1 --key "$pub" "$repo@$digest" > /dev/null
  echo "attested the provenance of $repo@$digest"
done
