#!/bin/sh
# The docs stack end to end, through the real launcher and a real podman: a
# Markdown-only repo's `sdlc ci` runs markdownlint-cli2 and lychee in the gate
# container, read-only and offline. Then each way a docs repo can be wrong must
# fail the check that owns it, and a repo's own markdownlint config must replace
# the fleet defaults. Planned argv is unit-tested in test_plan.py; this is the
# part only the real tools can prove.
#
# Usage: tests/test_docs_stack.sh <image-ref>

set -eu

IMAGE=${1:?usage: tests/test_docs_stack.sh <image-ref>}
HERE=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
FAILED=0
ok()  { printf 'ok   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

commit() {             # name
  git -C "$WORK/$1" add -A
  git -C "$WORK/$1" -c user.email=t@t -c user.name=t commit -qm fixture
}

fixture() {            # name: a clean two-page docs repo, linked both ways
  mkdir -p "$WORK/$1/docs"
  printf '# Project\n\n## Usage\n\nRead [the guide](docs/guide.md#setup).\n' > "$WORK/$1/README.md"
  printf '# Guide\n\n## Setup\n\nBack to the [usage notes](../README.md#usage).\n' > "$WORK/$1/docs/guide.md"
  # The fixture has no constitution manifest to validate.
  printf '[tool.sdlc.push]\nskip = ["constitution"]\n' > "$WORK/$1/sdlc.toml"
  git -C "$WORK/$1" init -q
  commit "$1"
}

sdlc_in() {            # name command: run the launcher in a fixture, output to $WORK/<name>.out
  rc=0
  (cd "$WORK/$1" && SDLC_IMAGE=$IMAGE "$HERE/bin/sdlc" "$2") > "$WORK/$1.out" 2>&1 || rc=$?
}

show() { tail -15 "$WORK/$1.out" | sed 's/^/       /'; }

fixture clean
sdlc_in clean ci
if [ "$rc" -eq 0 ] \
   && grep -q 'markdown-lint passed' "$WORK/clean.out" \
   && grep -q 'link-check passed' "$WORK/clean.out"; then
  ok "sdlc ci passes a clean docs repo, anchors and all (exit 0)"
else
  bad "sdlc ci on a clean docs repo exited $rc"
  show clean
fi
# A docs repo is not a Python repo and must not be handed Python's checks.
if ! grep -q 'ruff\|mypy\|pytest' "$WORK/clean.out"; then
  ok "a docs repo is not planned any Python check"
else
  bad "a docs repo ran a Python check"
  show clean
fi

# A link to a page that is not there: link-check's finding, and ONLY its.
fixture brokenlink
printf '\nAlso [the reference](docs/reference.md).\n' >> "$WORK/brokenlink/README.md"
commit brokenlink
sdlc_in brokenlink link-check
if [ "$rc" -eq 1 ] && grep -q 'docs/reference.md' "$WORK/brokenlink.out"; then
  ok "a link to a missing file fails link-check (exit 1, names the file)"
else
  bad "link-check on a missing file exited $rc, want 1"
  show brokenlink
fi
sdlc_in brokenlink markdown-lint
if [ "$rc" -eq 0 ]; then
  ok "...and the same repo is still clean for markdown-lint"
else
  bad "markdown-lint on a repo with only a broken link exited $rc, want 0"
  show brokenlink
fi

# The half of a link a rename does not announce: the #fragment.
fixture brokenanchor
printf '\nSee [setup](docs/guide.md#installation).\n' >> "$WORK/brokenanchor/README.md"
commit brokenanchor
sdlc_in brokenanchor link-check
if [ "$rc" -eq 1 ] && grep -q 'installation' "$WORK/brokenanchor.out"; then
  ok "a #fragment that no heading makes fails link-check (exit 1)"
else
  bad "link-check on a missing #fragment exited $rc, want 1"
  show brokenanchor
fi

# Offline means offline: a remote link is not a finding and is not fetched.
fixture remote
printf '\nSee [upstream](https://example.invalid/nothing).\n' >> "$WORK/remote/README.md"
commit remote
sdlc_in remote link-check
if [ "$rc" -eq 0 ]; then
  ok "a remote link is not checked by default (offline, exit 0)"
else
  bad "link-check on a remote link exited $rc, want 0 offline"
  show remote
fi

# A correctness error, not taste: a heading that skips a level (MD001) and a
# reversed link (MD011). Both are what the fleet defaults still judge.
fixture badmd
printf '# Project\n\n### Skipped a level\n' > "$WORK/badmd/README.md"
commit badmd
sdlc_in badmd markdown-lint
if [ "$rc" -eq 1 ] && grep -q 'MD001' "$WORK/badmd.out"; then
  ok "a skipped heading level fails markdown-lint (exit 1, MD001)"
else
  bad "markdown-lint on a skipped heading exited $rc, want 1"
  show badmd
fi
fixture reversed
printf '# Project\n\n(the guide)[docs/guide.md]\n' > "$WORK/reversed/README.md"
commit reversed
sdlc_in reversed markdown-lint
if [ "$rc" -eq 1 ] && grep -q 'MD011' "$WORK/reversed.out"; then
  ok "a reversed link fails markdown-lint (exit 1, MD011)"
else
  bad "markdown-lint on a reversed link exited $rc, want 1"
  show reversed
fi

# Taste is not judged by default: a 200-column line, inline HTML and a missing
# first-line heading are what the fleet writes on purpose.
fixture taste
{
  printf 'Intro paragraph that is wrapped by nobody and runs well past eighty columns, because a line length is a taste: ';
  printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n\n';
  printf '<details><summary>More</summary>inline html</details>\n';
} > "$WORK/taste/README.md"
commit taste
sdlc_in taste markdown-lint
if [ "$rc" -eq 0 ]; then
  ok "long lines, inline HTML and no first heading pass the fleet defaults (exit 0)"
else
  bad "markdown-lint on taste-only findings exited $rc, want 0"
  show taste
fi

# A repo's own config replaces the defaults entirely: this one turns line
# length back on, so the same long line that passed above now fails.
cp "$WORK/taste/README.md" "$WORK/ownconfig.README.md"
fixture ownconfig
cp "$WORK/ownconfig.README.md" "$WORK/ownconfig/README.md"
printf 'default: true\nMD013:\n  line_length: 80\n' > "$WORK/ownconfig/.markdownlint.yaml"
commit ownconfig
sdlc_in ownconfig markdown-lint
if [ "$rc" -eq 1 ] && grep -q 'MD013' "$WORK/ownconfig.out"; then
  ok "a repo's .markdownlint.yaml replaces the fleet defaults (MD013 fails, exit 1)"
else
  bad "markdown-lint with a .markdownlint.yaml exited $rc, want 1 on MD013"
  show ownconfig
fi

# A .gitignore'd tree is not the repo's own docs, and neither is node_modules.
fixture ignored
mkdir -p "$WORK/ignored/node_modules/pkg" "$WORK/ignored/build"
printf '### broken\n\n[x](nowhere.md)\n' > "$WORK/ignored/node_modules/pkg/README.md"
printf '### broken\n\n[x](nowhere.md)\n' > "$WORK/ignored/build/out.md"
printf 'build/\n' > "$WORK/ignored/.gitignore"
commit ignored
sdlc_in ignored ci
if [ "$rc" -eq 0 ] \
   && grep -q 'markdown-lint passed' "$WORK/ignored.out" \
   && grep -q 'link-check passed' "$WORK/ignored.out"; then
  ok "node_modules and .gitignore'd Markdown are not judged (exit 0)"
else
  bad "sdlc ci on a repo with ignored broken Markdown exited $rc, want 0"
  show ignored
fi

printf '\n'
[ "$FAILED" -eq 0 ] && { printf 'docs stack PASSED\n'; exit 0; }
printf 'docs stack FAILED: %s check(s)\n' "$FAILED"; exit 1
