#!/bin/sh
# Print the revision label of a published image tag, or nothing when the tag
# does not exist yet. Used by container.yml's promote guard, which refuses to
# move a floating tag onto a commit the tag already contains.
#
# Only "manifest unknown" means absent. Any other failure to read the tag --
# auth, network, a registry error -- exits non-zero: a guard that cannot read
# what it guards would otherwise wave the move through.
#
# Usage: scripts/revision-of.sh <registry/repo:tag>
set -eu

ref=${1:?usage: scripts/revision-of.sh <registry/repo:tag>}
err=$(mktemp)
trap 'rm -f "$err"' EXIT

if out=$(skopeo inspect --format '{{ index .Labels "org.opencontainers.image.revision" }}' \
           "docker://$ref" 2>"$err"); then
  printf '%s\n' "$out"
  exit 0
fi
if grep -qi 'manifest unknown' "$err"; then
  exit 0
fi
printf 'cannot read %s to guard it:\n' "$ref" >&2
cat "$err" >&2
exit 1
