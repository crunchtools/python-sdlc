#!/bin/sh
# Mirror one upstream parent image into quay.io/crunchtools/<name> and sign it
# with the fleet key, so the image job's verify_parent trusts it. That check
# trusts a quay.io/crunchtools/<name> parent only when the fleet key signed it
# with the annotation repo=crunchtools/<name> (image_owner); every other
# registry outside Red Hat's is refused. Fleet repos built FROM a parent
# nobody signs (quay.io/podman/stable) build FROM the mirror instead.
#
# The copy is `skopeo copy --all --preserve-digests`, so the mirror's digest is
# upstream's, every platform included, and a Containerfile moves from
# upstream@<digest> to quay.io/crunchtools/<name>@<the same digest>. A rerun is
# safe: an already signed digest is left as it is.
#
# Refresh: Renovate cannot run this (it holds no key). When a repo wants a newer
# parent, run this for the new upstream digest, then move that repo's FROM.
#
# Run as root on Exedore, secrets on stdin exactly as local-release.sh takes
# them (it reads COSIGN_PRIVATE_KEY, COSIGN_PASSWORD, QUAY_USERNAME and
# QUAY_PASSWORD, and ignores the rest):
#   ssh gloval 'sh -s' < scripts/local-release-secrets.sh \
#     | ssh exedore '<sdlc checkout>/scripts/mirror-parent.sh \
#         quay.io/podman/stable@sha256:<64 hex> podman-stable v5.8.9'
#
# Usage: <secrets.json scripts/mirror-parent.sh <upstream@sha256:digest> <name> <tag>
set -eu

usage='usage: <secrets.json scripts/mirror-parent.sh <upstream@sha256:digest> <name> <tag>'
die() { printf 'mirror-parent: %s\n' "$*" >&2; exit 1; }
upstream=${1:?$usage}
name=${2:?$usage}
tag=${3:?$usage}
digest=${upstream##*@}
case $upstream in *@sha256:*) ;; *) die "the upstream must be pinned by digest: $upstream" ;; esac
hex=${digest#sha256:}
case $hex in *[!0-9a-f]*) die "not a sha256 digest: $digest" ;; esac
[ "${#hex}" -eq 64 ] || die "not a sha256 digest: $digest"
printf '%s' "$name" | grep -Eqx '[a-z0-9]+([._-][a-z0-9]+)*' || die "not an image name: $name"
printf '%s' "$tag" | grep -Eqx '[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}' || die "not a tag: $tag"
mirror=quay.io/crunchtools/$name

here=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
COSIGN_IMAGE=$(sed -n 's/.*COSIGN_IMAGE: "\(ghcr\.io\/sigstore\/cosign\/cosign@sha256:[0-9a-f]\{64\}\)".*/\1/p' \
  "$here/.github/workflows/ci.yml" | head -1)
[ -n "$COSIGN_IMAGE" ] || die ".github/workflows/ci.yml pins no cosign digest"

secrets=$(cat)
for n in COSIGN_PRIVATE_KEY COSIGN_PASSWORD QUAY_USERNAME QUAY_PASSWORD; do
  value=$(printf '%s' "$secrets" | jq -er --arg n "$n" '.[$n] // empty') || die "$n is missing from stdin"
  export "$n=$value"
done
unset secrets value

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
sed -n "/<<'FLEET_KEY'\$/,/^FLEET_KEY\$/p" "$here/bin/sdlc-bootstrap" | sed '1d;$d' > "$work/fleet.pub"
[ -s "$work/fleet.pub" ] || die "bin/sdlc-bootstrap carries no fleet key"
printf '%s\n' "$QUAY_PASSWORD" \
  | skopeo login -q --authfile "$work/config.json" -u "$QUAY_USERNAME" --password-stdin quay.io >/dev/null

skopeo copy -q --all --preserve-digests --dest-authfile "$work/config.json" \
  "docker://$upstream" "docker://$mirror:$tag"
got=$(skopeo inspect --raw --authfile "$work/config.json" "docker://$mirror@$digest" | sha256sum | cut -c1-64)
[ "sha256:$got" = "$digest" ] || die "$mirror@$digest did not keep upstream's digest (got sha256:$got)"

cosign() {
  podman run --rm --log-driver=none --pull=missing --security-opt label=disable --user 0:0 \
    -v "$work:/w:ro" -e DOCKER_CONFIG=/w -e COSIGN_PRIVATE_KEY -e COSIGN_PASSWORD \
    -- "$COSIGN_IMAGE" "$@"
}
cosign public-key --key env://COSIGN_PRIVATE_KEY > "$work/secret.pub"
cmp -s "$work/secret.pub" "$work/fleet.pub" \
  || die "COSIGN_PRIVATE_KEY is not the private half of the key bin/sdlc-bootstrap trusts"
verify() {
  cosign verify --insecure-ignore-tlog=true --key /w/fleet.pub -a "repo=crunchtools/$name" \
    "$mirror@$digest" > /dev/null 2>&1
}
if verify; then
  echo "$mirror@$digest is already signed"
else
  cosign sign --yes --tlog-upload=false --key env://COSIGN_PRIVATE_KEY \
    -a "repo=crunchtools/$name" -a "upstream=${upstream%@*}" "$mirror@$digest" > /dev/null
  verify || die "$mirror@$digest does not verify after signing"
fi
echo "mirrored $upstream to $mirror:$tag, signed by the fleet key"
