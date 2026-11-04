#!/bin/sh
# Earn the host-contract exemption rather than rely on the validator not looking.
#
# Constitution XII's host contract: a developer machine has git, podman,
# pre-commit and gh, and a repo MUST NOT assume anything else. Its machine check
# (`check_host_contract` in validate-constitution.py) inspects only `system` and
# `unsupported` pre-commit entries, and its own docstring says a `language:
# script` hook's internals are never inspected -- "the rule for runners is
# prose".
#
# So nothing upstream would catch `bin/sdlc` growing a dependency on python3,
# jq, yq or a non-POSIX awk. Leaning on that gap would be laundering the rule
# instead of keeping it.
#
# This test is BEHAVIOURAL, not a grep over the source. A first attempt grepped
# for tool names and reported six false positives, because the dispatcher
# necessarily mentions `pytest`, `semgrep`, `codeql` and the rest -- as
# ARGUMENTS it passes to the container, not as host commands. Reading intent out
# of shell source does not work; running it with a restricted PATH does.
#
# Usage: tests/test_host_contract.sh

set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
FAILED=0
ok()  { printf 'ok   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }

printf '== host contract: bin/sdlc\n\n'

case "$(head -1 bin/sdlc)" in
  '#!/bin/sh') ok "bin/sdlc is #!/bin/sh" ;;
  *)           bad "shebang is not #!/bin/sh: $(head -1 bin/sdlc)" ;;
esac

SHIM=$(mktemp -d)
LOG="$SHIM/podman.log"
trap 'rm -rf "$SHIM"' EXIT 0

# The four tools the contract allows. git is real: bin/sdlc genuinely asks git
# where the repo root and the hooks are, and that is allowed.
for prog in git pre-commit gh; do
  if full=$(command -v "$prog" 2>/dev/null); then ln -s "$full" "$SHIM/$prog"; fi
done

# POSIX utilities. The rule is about language toolchains and linters, not about
# rebuilding coreutils -- every machine with a shell has these.
for prog in sh awk sed tr grep basename dirname nproc cat mktemp rm ln head id; do
  if full=$(command -v "$prog" 2>/dev/null); then ln -sf "$full" "$SHIM/$prog"; fi
done

# A fake podman that records what it was asked to do and succeeds. Every target
# that needs a container must reach THIS and nothing else.
cat > "$SHIM/podman" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >> "$LOG"
exit 0
EOF
chmod +x "$SHIM/podman"

# Each target run with only the above on PATH. If bin/sdlc reaches for python3,
# uv, jq or a real tool binary, the shell cannot find it and the target fails.
for target in list versions tiers mypy pytest gourmand semgrep pip-audit constitution codeql ruff-check ruff-format; do
  if PATH="$SHIM" sh bin/sdlc "$target" >/dev/null 2>&1; then
    ok "$target needs only the contract's tools"
  else
    bad "$target fails with only git, podman, pre-commit and gh on PATH"
  fi
done

# hooks-installed is host-only and asks git about this repo's hooks. It may
# legitimately report a finding (exit 1) -- what matters is that it did not die
# for want of a program, which is exit 2.
PATH="$SHIM" sh bin/sdlc hooks-installed >/dev/null 2>&1 && rc=0 || rc=$?
if [ "$rc" -ne 2 ]; then
  ok "hooks-installed needs only the contract's tools (exit $rc)"
else
  bad "hooks-installed could not run with a restricted PATH"
fi

# The targets that run a container must actually have tried to. Without this the
# loop above would also pass if every target silently did nothing.
if [ -s "$LOG" ]; then
  ok "container targets invoked podman ($(wc -l < "$LOG" | tr -d ' ') calls)"
else
  bad "no target invoked podman; the test above proved nothing"
fi

# And they must have invoked podman ONLY -- a second shim would have been
# created if anything else were reached, but assert the image name appears so we
# know it was a real run and not just `podman volume`.
if grep -q 'python-sdlc\|run --rm' "$LOG" 2>/dev/null; then
  ok "podman was asked to run the image"
else
  bad "podman was called but never asked to run the image"
fi

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf 'host contract PASSED\n'; exit 0
fi
printf 'host contract FAILED: %s check(s)\n' "$FAILED"; exit 1
