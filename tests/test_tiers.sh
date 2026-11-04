#!/bin/sh
# `.pre-commit-hooks.yaml` is the single authority for tier membership, and
# `bin/sdlc` parses it with awk to answer `sdlc commit` and `sdlc push`.
#
# This test is what makes that one authority rather than two. The parse is a
# small awk program over YAML, which is exactly the kind of thing that keeps
# working while quietly reading the wrong thing -- and if it ever returns an
# empty or partial list, `sdlc push` runs fewer gates than the hooks do and
# nothing says so. gates.sh's mypy hook drifted from its own script's resource
# budget the moment that budget stopped being a constant; this is the guard
# against the same shape of bug here.
#
# Usage: tests/test_tiers.sh

set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
FAILED=0
ok()  { printf 'ok   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }

printf '== tier membership\n\n'

HOOKS=.pre-commit-hooks.yaml

# The expected answer, derived from the file a DIFFERENT way than bin/sdlc
# derives it. Two independent readings that agree is the point; reusing the same
# awk would only prove it is self-consistent.
expected_for() {
  python3 - "$1" <<'PY' 2>/dev/null || return 1
import re, sys
want = sys.argv[1]
text = open(".pre-commit-hooks.yaml", encoding="utf-8").read()
for block in re.split(r"^- id:", text, flags=re.M)[1:]:
    m = re.search(r"^  entry: bin/sdlc (\S+)", block, re.M)
    s = re.search(r"^  stages: \[(.*)\]", block, re.M)
    if m and s and want in [x.strip() for x in s.group(1).split(",")]:
        print(m.group(1))
PY
}

# What bin/sdlc itself will act on.
actual_for() { sh bin/sdlc tiers | awk -v w="$1:" '$0==w {f=1; next} /^[a-z-]+:$/ {f=0} f {print $1}'; }

for stage in pre-commit pre-push; do
  if ! exp=$(expected_for "$stage"); then
    bad "cannot compute the expected $stage list (python3 unavailable in CI?)"
    continue
  fi
  act=$(actual_for "$stage")
  if [ "$(printf '%s' "$exp" | sort)" = "$(printf '%s' "$act" | sort)" ]; then
    ok "$stage: $(printf '%s' "$act" | tr '\n' ' ')"
  else
    bad "$stage disagrees"
    printf '     hooks file says: %s\n' "$(printf '%s' "$exp" | tr '\n' ' ')"
    printf '     bin/sdlc says:   %s\n' "$(printf '%s' "$act" | tr '\n' ' ')"
  fi
  [ -n "$act" ] || bad "$stage parsed to an EMPTY list; the tier would run nothing"
done

# Every hook must name a target the dispatcher knows, or the hook fails at the
# worst possible moment -- on somebody else's commit. Compared as sets, by
# asking bin/sdlc itself rather than pattern-matching its source: it exits 2 on
# an unknown target, and `tiers` needs no container.
DISPATCHABLE=$(sh bin/sdlc tiers | sed -n 's/^  \([a-z-]*\)$/\1/p')
grep -oE '^  entry: bin/sdlc [a-z-]+' "$HOOKS" | awk '{print $NF}' | while read -r target; do
  if printf '%s\n' "$DISPATCHABLE" | grep -qx "$target"; then
    continue
  fi
  printf 'FAIL hook target %s is not in any tier bin/sdlc reports\n' "$target"
  exit 1
done || FAILED=$((FAILED + 1))
ok "every hook target appears in a tier bin/sdlc reports"

# The tier rule itself, asserted rather than left to prose: nothing that needs
# the network or costs money may sit in the commit tier, because the promise is
# that commits are instant and work offline.
for target in $(expected_for pre-commit); do
  case "$target" in
    ruff-check|ruff-format|hooks-installed) ;;
    *) bad "'$target' is in the commit tier; only sub-second offline targets belong there" ;;
  esac
done
ok "the commit tier holds only sub-second offline targets"

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf 'tier membership PASSED\n'; exit 0
fi
printf 'tier membership FAILED: %s check(s)\n' "$FAILED"; exit 1
