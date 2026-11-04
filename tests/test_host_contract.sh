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
POSIX_UTILS="sh awk sed tr grep basename dirname nproc cat mktemp rm ln head id
             cut sleep date wc"
for prog in $POSIX_UTILS; do
  if full=$(command -v "$prog" 2>/dev/null); then ln -sf "$full" "$SHIM/$prog"; fi
done

# A fake podman that records what it was asked to do and succeeds. Every target
# that needs a container must reach THIS and nothing else. Asked for a plan, it
# answers with one step of each mode, so the launcher's whole executor runs.
#
# A plan line is `check TAB lane TAB mode TAB payload`. The plans whose names
# begin `lanes-` exercise the parallel executor: their steps print markers,
# sleep and exit on command, so a test can pin what the launcher does with a
# lane that fails, a lane that is slow, and two lanes writing one terminal.
TAB=$(printf '\t')
US=$(printf '\037')
cat > "$SHIM/podman" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >> "$LOG"
for _last in "\$@"; do :; done
case " \$* " in
  *" plan gatehouse-direct "*)
    printf 'gatehouse${TAB}main${TAB}local${TAB}gatehouse\n' ;;
  *" plan evil "*)
    printf 'evil${TAB}main${TAB}host${TAB}sh${US}-c${US}touch $SHIM/pwned\n' ;;
  *" plan subst "*)
    printf 'subst${TAB}main${TAB}host${TAB}podman${US}run${US}\\\$(touch $SHIM/pwned)\n' ;;
  *" plan lanes-interleave "*)
    printf 'a1${TAB}one${TAB}image-ro${TAB}tick-a1\n'
    printf 'a2${TAB}one${TAB}image-ro${TAB}tick-a2\n'
    printf 'b1${TAB}two${TAB}image-ro${TAB}tick-b1\n'
    printf 'b2${TAB}two${TAB}image-ro${TAB}tick-b2\n' ;;
  *" plan lanes-two-first "*)
    printf 'x${TAB}one${TAB}image-ro${TAB}rc-two\n'
    printf 'y${TAB}two${TAB}image-ro${TAB}slow-rc-one\n' ;;
  *" plan lanes-one-first "*)
    printf 'x${TAB}one${TAB}image-ro${TAB}rc-one\n'
    printf 'y${TAB}two${TAB}image-ro${TAB}slow-rc-two\n' ;;
  *" plan lanes-keeps-going "*)
    printf 'x${TAB}one${TAB}image-ro${TAB}rc-one\n'
    printf 'x${TAB}one${TAB}image-ro${TAB}must-not-run\n'
    printf 'w${TAB}one${TAB}image-ro${TAB}after-the-failure\n'
    printf 'y${TAB}two${TAB}image-ro${TAB}other-lane-first\n'
    printf 'z${TAB}two${TAB}image-ro${TAB}other-lane-second\n' ;;
  *" plan lanes-budget "*)
    printf 'p${TAB}one${TAB}host${TAB}podman${US}build${US}-t${US}localhost/x:sdlc${US}.\n'
    printf 'q${TAB}two${TAB}image-ro${TAB}quiet-ok\n' ;;
  *" plan "*)
    printf 'mypy${TAB}main${TAB}image-ro${TAB}mypy\n'
    printf 'ruff-check${TAB}main${TAB}image-rw${TAB}ruff-check\n'
    printf 'image${TAB}main${TAB}host${TAB}podman${US}build${US}-t${US}localhost/x:sdlc${US}.\n'
    printf 'image-tests${TAB}main${TAB}host${TAB}podman${US}run${US}--rm${US}localhost/x:sdlc${US}-m${US}pytest\n' ;;
esac
case "\$_last" in
  tick-*)
    printf 'START %s\n' "\$_last" >> "$SHIM/race.log"
    printf 'BEGIN %s\n' "\$_last"; sleep 1; printf 'END %s\n' "\$_last"
    printf 'DONE %s\n' "\$_last" >> "$SHIM/race.log" ;;
  rc-one)      printf 'RAN rc-one\n'; exit 1 ;;
  rc-two)      printf 'RAN rc-two\n'; exit 2 ;;
  slow-rc-one) printf 'RAN slow-rc-one\n'; sleep 2; exit 1 ;;
  slow-rc-two) printf 'RAN slow-rc-two\n'; sleep 2; exit 2 ;;
  must-not-run) printf 'RAN must-not-run\n'; touch "$SHIM/must-not-run" ;;
  after-the-failure|other-lane-first|other-lane-second)
    printf 'RAN %s\n' "\$_last" ;;
esac
exit 0
EOF
chmod +x "$SHIM/podman"

# Each target run with only the above on PATH. If bin/sdlc reaches for python3,
# uv, jq or a real tool binary, the shell cannot find it and the target fails.
for target in list versions tiers commit push ci mypy ruff-check image; do
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

# The launcher, not the config, holds the host contract: a plan step that is
# not a podman command must be refused, whatever a repo's config asked for.
PATH="$SHIM" sh bin/sdlc evil >/dev/null 2>&1 && rc=0 || rc=$?
if [ "$rc" -eq 2 ] && [ ! -e "$SHIM/pwned" ]; then
  ok "a host step that is not podman is refused (exit 2) and never runs"
else
  bad "a non-podman host step was not refused (exit $rc)"
fi

# And a podman argument that LOOKS like a command substitution is passed to
# podman as a literal word, never run by a shell.
PATH="$SHIM" sh bin/sdlc subst >/dev/null 2>&1 || true
if [ ! -e "$SHIM/pwned" ]; then
  ok "a host step's words are never evaluated as shell"
else
  bad "a host step's word was evaluated as shell"
fi

printf '\n== the lanes\n\n'

# Steps of one lane run in sequence; lanes run at the same time. What has to
# hold is not the speed -- it is that the answer and the log do not depend on
# which lane happened to finish first.

RUN=$(mktemp -d)
trap 'rm -rf "$SHIM" "$RUN"' EXIT 0

lanes() {              # target -> exit status, output in $RUN/out
  rm -f "$SHIM/must-not-run"
  _rc=0
  PATH="$SHIM" SDLC_CPUS=4 sh bin/sdlc "$1" > "$RUN/out" 2>&1 || _rc=$?
  return "$_rc"
}

# A single-lane plan takes the sequential path: no lane banner, and the log is
# the one a repo without an image chain has always had.
lanes ci && rc=0 || rc=$?
if ! grep -q 'lanes at the same time' "$RUN/out"; then
  ok "a single-lane plan does not go through the lane executor"
else
  bad "a single-lane plan was split into lanes"
fi

# Two lanes, four checks, each step printing BEGIN and END a second apart. Run
# unbuffered the two lanes would alternate; what must come out is each check's
# block whole, in PLAN order -- a1 a2 b1 b2 -- however the lanes ran.
rm -f "$SHIM/race.log"
lanes lanes-interleave && rc=0 || rc=$?

# First: the lanes really did overlap, so the ordering assertion below is
# testing the buffering and not a sequential run that could not interleave if
# it tried. Each step records when it started and finished in one shared file;
# both lanes' first steps must have started before either had finished.
if [ "$(head -2 "$SHIM/race.log" | grep -c '^START ')" -eq 2 ]; then
  ok "both lanes were running at the same time"
else
  bad "the lanes ran one after another: $(tr '\n' ' ' < "$SHIM/race.log")"
fi

got=$(grep -E '^(BEGIN|END) ' "$RUN/out" | tr '\n' ' ')
want="BEGIN tick-a1 END tick-a1 BEGIN tick-a2 END tick-a2 BEGIN tick-b1 END tick-b1 BEGIN tick-b2 END tick-b2 "
if [ "$got" = "$want" ]; then
  ok "each check's block is printed whole, in plan order, never interleaved"
else
  bad "lane output interleaved or reordered: $got"
fi
if [ "$rc" -eq 0 ]; then
  ok "two passing lanes exit 0"
else
  bad "two passing lanes exited $rc"
fi
# And the blocks must carry a duration, because buffering collapses the
# timestamps people were reading the `== <step>` markers for.
if grep -q '^== a1 passed in [0-9]*s' "$RUN/out"; then
  ok "a block ends with how long its check took"
else
  bad "a block does not report its check's duration"
fi

# A 2 anywhere wins over a 1, in either finishing order. Fast-2/slow-1 and
# fast-1/slow-2 are two different code paths through the aggregation, and the
# second is the one a naive "last writer wins" gets wrong.
lanes lanes-two-first && rc=0 || rc=$?
if [ "$rc" -eq 2 ]; then
  ok "a 2 that finishes first beats a 1 that finishes last"
else
  bad "fast 2 + slow 1 exited $rc, should be 2"
fi
lanes lanes-one-first && rc=0 || rc=$?
if [ "$rc" -eq 2 ]; then
  ok "a 2 that finishes last beats a 1 that finished first"
else
  bad "fast 1 + slow 2 exited $rc, should be 2"
fi

# A failed step ends its OWN check. The next check in that lane still runs, and
# so does everything in every other lane -- that rule is older than the lanes
# and the lanes must not quietly narrow it.
lanes lanes-keeps-going && rc=0 || rc=$?
if [ "$rc" -eq 1 ]; then
  ok "a lane that finds something exits 1 when nothing could not run"
else
  bad "a found-something run exited $rc, should be 1"
fi
if [ -e "$SHIM/must-not-run" ]; then
  bad "a failed check's remaining steps still ran"
else
  ok "a failed step ends its own check"
fi
for marker in after-the-failure other-lane-first other-lane-second; do
  if grep -q "RAN $marker" "$RUN/out"; then
    ok "$marker ran despite the failure in lane one"
  else
    bad "$marker was skipped because another check failed"
  fi
done

# The CPU budget is for the whole gate, not for each container: two lanes out
# of a budget of 4 get 2 each, so a laptop running `sdlc ci` is no worse off
# than it was when the checks were sequential.
: > "$LOG"
lanes lanes-budget || true
if grep -q -- '--cpus 2 ' "$LOG" && grep -q -- '--cpu-quota 200000' "$LOG"; then
  ok "a budget of 4 is split 2 and 2 between two lanes"
else
  bad "the lanes did not divide the CPU budget"
  grep -o -- '--cpus [0-9]*\|--cpu-quota [0-9]*' "$LOG" | sort -u | sed 's/^/       /'
fi

# A container that succeeds must report success, whatever the caller's last
# exit status was. Gatehouse runs right after `git diff --quiet`, which exits 1
# whenever there IS a diff to review; that 1 leaked through a shared variable
# and failed every push with a branch to review, after printing "No issues
# found". Checked here with a git shim that has a diff and a podman that passes.
GITSHIM=$(mktemp -d)
for prog in pre-commit gh; do
  [ -e "$SHIM/$prog" ] && ln -s "$SHIM/$prog" "$GITSHIM/$prog"
done
for prog in $POSIX_UTILS podman; do
  [ -e "$SHIM/$prog" ] && ln -s "$(readlink -f "$SHIM/$prog")" "$GITSHIM/$prog"
done
cat > "$GITSHIM/git" <<EOF
#!/bin/sh
case "\$*" in
  *--show-toplevel*) pwd ;;
  *symbolic-ref*) echo refs/remotes/origin/main ;;
  *"diff --quiet"*) exit 1 ;;
  *diff*) echo "+a change" ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$GITSHIM/git"
ENVFILE="$GITSHIM/gatehouse.env"
: > "$ENVFILE"
PATH="$GITSHIM" GATEHOUSE_ENV="$ENVFILE" sh bin/sdlc gatehouse-direct >/dev/null 2>&1 && rc=0 || rc=$?
rm -rf "$GITSHIM"
if [ "$rc" -eq 0 ]; then
  ok "a passing review after a non-empty diff reports success"
else
  bad "a passing review after a non-empty diff exited $rc"
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
