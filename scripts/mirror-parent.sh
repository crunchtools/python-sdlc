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
# parent, run this for the new upstream digest and tag. A FROM written as
# quay.io/crunchtools/<name>:<tag>@<digest> lets Renovate's dockerfile manager
# propose the move once the newer tag is mirrored; it watches the mirror, not
# upstream.
#
# Run as root on Exedore, secrets on stdin exactly as local-release.sh takes
# them (it reads COSIGN_PRIVATE_KEY, COSIGN_PASSWORD, QUAY_USERNAME and
# QUAY_PASSWORD, and ignores the rest):
#   ssh gloval 'sh -s' < scripts/local-release-secrets.sh \
#     | ssh exedore '<sdlc checkout>/scripts/mirror-parent.sh \
#         quay.io/podman/stable@sha256:<64 hex> podman-stable v5.8.9-immutable'
#
# The names it may write are MIRRORS, below, reviewed here like EXTRA_IMAGES:
# a fleet repo's image is that repo's to sign, so a name that is an
# EXTRA_IMAGES entry or a crunchtools repository is refused even if listed,
# and a tag that already names another digest is never moved.
#
# Usage: <secrets.json scripts/mirror-parent.sh <upstream@sha256:digest> <name> <tag>
set -eu

usage='usage: <secrets.json scripts/mirror-parent.sh <upstream@sha256:digest> <name> <tag>'
die() { printf 'mirror-parent: %s\n' "$*" >&2; exit 1; }
# The mirrors this script may write. Adding one is a reviewed change here.
MIRRORS='podman-stable'
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
case " $MIRRORS " in *" $name "*) ;; *) die "$name is not one of the mirrors this script may write: $MIRRORS" ;; esac
extra=$(sed -n '/^EXTRA_IMAGES/,/^}/p' "$here/sdlc_jobs/_core.py")
[ -n "$extra" ] || die "sdlc_jobs/_core.py has no EXTRA_IMAGES to check $name against"
! printf '%s\n' "$extra" | grep -Eq "\"(crunchtools/)?$name\"" \
  || die "$name is a fleet repo's image (EXTRA_IMAGES): it is that repo's to sign"
code=$(curl -sS -o /dev/null -w '%{http_code}' "https://api.github.com/repos/crunchtools/$name") || code=error
[ "$code" = 404 ] \
  || die "crunchtools/$name is a repository, or GitHub could not say (HTTP $code): its image is that repo's to sign"

# ci.yml's cosign pin, in either form it has had.
COSIGN_IMAGE=$(sed -n 's/.*COSIGN_IMAGE: "\(\(ghcr\.io\/sigstore\/cosign\/cosign\|registry\.access\.redhat\.com\/hi\/cosign\)@sha256:[0-9a-f]\{64\}\)".*/\1/p' \
  "$here/.github/workflows/ci.yml" | head -1)
[ -n "$COSIGN_IMAGE" ] || die ".github/workflows/ci.yml pins no cosign digest"

secrets=$(cat)
field() { printf '%s' "$secrets" | jq -er --arg n "$1" '.[$n] // empty' || die "$1 is missing from stdin"; }
COSIGN_PRIVATE_KEY=$(field COSIGN_PRIVATE_KEY)
COSIGN_PASSWORD=$(field COSIGN_PASSWORD)
export COSIGN_PRIVATE_KEY COSIGN_PASSWORD
# The robot's login is never exported: skopeo login reads it from argv and stdin, and
# every child after that, cosign's container included, has only the authfile.
quay_user=$(field QUAY_USERNAME)
quay_pass=$(field QUAY_PASSWORD)
unset secrets

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
sed -n "/<<'FLEET_KEY'\$/,/^FLEET_KEY\$/p" "$here/bin/sdlc-bootstrap" | sed '1d;$d' > "$work/fleet.pub"
[ -s "$work/fleet.pub" ] || die "bin/sdlc-bootstrap carries no fleet key"

cosign() {
  podman run --rm --log-driver=none --pull=missing --security-opt label=disable --user 0:0 \
    -v "$work:/w:ro" -e DOCKER_CONFIG=/w -e COSIGN_PRIVATE_KEY -e COSIGN_PASSWORD \
    -- "$COSIGN_IMAGE" "$@"
}
cosign public-key --key env://COSIGN_PRIVATE_KEY > "$work/secret.pub" \
  || die "cosign could not read COSIGN_PRIVATE_KEY"
cmp -s "$work/secret.pub" "$work/fleet.pub" \
  || die "COSIGN_PRIVATE_KEY is not the private half of the key bin/sdlc-bootstrap trusts"

printf '%s\n' "$quay_pass" \
  | skopeo login --authfile "$work/config.json" -u "$quay_user" --password-stdin quay.io > /dev/null \
  || die "the quay login failed"
unset quay_user quay_pass

# A tag is never moved: one already naming another digest is refused.
if skopeo inspect --raw --authfile "$work/config.json" "docker://$mirror:$tag" \
     > "$work/tagged" 2> "$work/err"; then
  now=sha256:$(sha256sum < "$work/tagged" | cut -c1-64)
  [ "$now" = "$digest" ] || die "$mirror:$tag already names $now, not $digest: refused"
elif ! grep -q 'manifest unknown' "$work/err"; then
  die "could not read $mirror:$tag: $(tail -n 1 "$work/err")"
fi

skopeo copy -q --all --preserve-digests --dest-authfile "$work/config.json" \
  "docker://$upstream" "docker://$mirror:$tag" || die "the copy to $mirror:$tag failed"
skopeo inspect --raw --authfile "$work/config.json" "docker://$mirror@$digest" > "$work/copied" \
  || die "could not read back $mirror@$digest"
got=sha256:$(sha256sum < "$work/copied" | cut -c1-64)
[ "$got" = "$digest" ] || die "$mirror@$digest did not keep upstream's digest (got $got)"

verify() {
  cosign verify --insecure-ignore-tlog=true --key /w/fleet.pub -a "repo=crunchtools/$name" \
    "$mirror@$digest" > /dev/null 2>&1
}
if verify; then
  echo "$mirror@$digest is already signed"
else
  # cosign v3 needs both flags for the legacy .sig signature verify_parent reads; v2 takes them.
  cosign sign --yes --tlog-upload=false --use-signing-config=false --new-bundle-format=false \
    --key env://COSIGN_PRIVATE_KEY -a "repo=crunchtools/$name" -a "upstream=${upstream%@*}" \
    "$mirror@$digest" > /dev/null || die "cosign could not sign $mirror@$digest"
  verify || die "$mirror@$digest does not verify after signing"
fi
echo "mirrored $upstream to $mirror:$tag, signed by the fleet key"
