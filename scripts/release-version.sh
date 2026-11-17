#!/usr/bin/env bash
# Print the tag release.yml should cut, or nothing when there is none.
#
# Reads the version from pyproject.toml in the current directory. Prints
# vX.Y.Z when that version has no release yet and CHANGELOG.md has a
# "## [X.Y.Z]" heading, prints nothing when it is already released, and exits
# non-zero for everything else: no version, no heading, or a gh failure other
# than "release not found". An auth or network error must stop the release,
# not read as "unreleased".
set -euo pipefail

v=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -1)
[ -n "$v" ] || { echo "pyproject.toml names no version" >&2; exit 1; }

if out=$(gh release view "v$v" 2>&1); then
  echo "v$v is already released" >&2
  exit 0
fi
if ! printf '%s\n' "$out" | grep -q 'release not found'; then
  printf 'gh release view v%s failed:\n%s\n' "$v" "$out" >&2
  exit 1
fi

# A literal match, not a regex: the heading is exactly "## [X.Y.Z]", alone or
# followed by a space, so 0.7.3 matches neither "## [0x7x3]" nor "## [0.7.30]".
if awk -v h="## [$v]" 'index($0, h) == 1 &&
    (length($0) == length(h) || substr($0, length(h) + 1, 1) == " ") {f = 1}
    END {exit !f}' CHANGELOG.md; then
  echo "v$v"
else
  echo "v$v has no CHANGELOG heading; not releasing" >&2
  exit 1
fi
