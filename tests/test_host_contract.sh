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

# The fake podman appends every call to one cumulative log, which the argv
# assertions at the end of this file read whole. A block that wants only its own
# calls marks the position and reads from there, rather than truncating it.
LOG_AT=0
log_mark()  { LOG_AT=$(wc -l < "$LOG" 2>/dev/null || printf 0); }
log_since() {
  tail -n "+$((LOG_AT + 1))" "$LOG" > "$RUN/podman.log" 2>/dev/null \
    || : > "$RUN/podman.log"
}

printf '== host contract: bin/sdlc\n\n'

case "$(head -1 bin/sdlc)" in
  '#!/bin/sh') ok "bin/sdlc is #!/bin/sh" ;;
  *)           bad "shebang is not #!/bin/sh: $(head -1 bin/sdlc)" ;;
esac

SHIM=$(mktemp -d)
LOG="$SHIM/podman.log"
RUN=$(mktemp -d)
trap 'rm -rf "$SHIM" "$RUN"' EXIT 0

# The four tools the contract allows. git is real: bin/sdlc genuinely asks git
# where the repo root and the hooks are, and that is allowed.
for prog in git pre-commit gh; do
  if full=$(command -v "$prog" 2>/dev/null); then ln -s "$full" "$SHIM/$prog"; fi
done

# POSIX utilities. The rule is about language toolchains and linters, not about
# rebuilding coreutils -- every machine with a shell has these.
#
# Only what bin/sdlc ITSELF runs belongs here. Every entry is one tool this test
# stops checking for, and checking for them is the whole job. What this file's
# own helpers use is irrelevant: they run with the real PATH, not the shim's.
POSIX_UTILS="sh awk sed tr grep basename dirname nproc cat mktemp rm ln head id
             cut sleep date wc mkdir"
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
  *" plan lanes-two-step "*)
    printf 'solo${TAB}one${TAB}image-ro${TAB}quiet-ok\n'
    printf 'pair${TAB}two${TAB}image-ro${TAB}step-one\n'
    printf 'pair${TAB}two${TAB}image-ro${TAB}step-two\n' ;;
  *" plan lanes-stall "*)
    printf 'f${TAB}one${TAB}image-ro${TAB}quiet-ok\n'
    printf 's${TAB}two${TAB}image-ro${TAB}very-slow-rc-one\n' ;;
  *" plan lanes-review "*)
    printf 'x${TAB}main${TAB}image-ro${TAB}tick-x\n'
    printf 'rev${TAB}review${TAB}local${TAB}gatehouse\n' ;;
  *" plan lanes-budget "*)
    printf 'p${TAB}one${TAB}host${TAB}podman${US}build${US}-t${US}localhost/x:sdlc${US}.\n'
    printf 'q${TAB}two${TAB}image-ro${TAB}quiet-ok\n' ;;
  *" plan "*)
    if [ -f "$SHIM/defer-last" ]; then printf 'codeql${TAB}main${TAB}image-ro${TAB}rc-three\n'; exit 0; fi
    if [ -f "$SHIM/defer" ]; then printf 'codeql${TAB}main${TAB}image-ro${TAB}rc-three\n'; fi
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
  rc-three)    printf 'RAN rc-three\n'; exit 3 ;;
  slow-rc-one) printf 'RAN slow-rc-one\n'; sleep 2; exit 1 ;;
  very-slow-rc-one) printf 'RAN very-slow-rc-one\n'; sleep 6; printf 'LAST LINE\n'; exit 1 ;;
  step-one) printf 'RAN step-one\n'; sleep 1 ;;
  step-two) printf 'RAN step-two\n'; sleep 1 ;;
  slow-rc-two) printf 'RAN slow-rc-two\n'; sleep 2; exit 2 ;;
  gatehouse)
    if [ -f "$SHIM/review-fails" ]; then printf 'REVIEW FOUND SOMETHING\n'; exit 1; fi
    printf 'REVIEW CLEAN\n' ;;
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

printf '\n== a check deferred to CI\n\n'

# 3 is the image saying "could not run, and CI judges this commit". A push lets
# it through, loudly, so --no-verify is not the only door past a broken
# CodeQL; every other command must still treat it as a 2 (python-sdlc#33).
touch "$SHIM/defer"
log_mark
PATH="$SHIM" sh bin/sdlc push > "$RUN/out" 2>&1 && rc=0 || rc=$?
log_since
if [ "$rc" -eq 0 ] && grep -q 'codeql COULD NOT RUN -- DEFERRED TO CI' "$RUN/out" \
   && grep -q ' mypy$' "$RUN/podman.log"; then
  ok "a push lets a deferred check through, says so, and runs the rest"
else
  bad "a deferred check on push exited $rc or said nothing"
fi
if grep -q 'SDLC_MEMORY=' "$RUN/podman.log" && grep -q -- '-e SDLC_TIER' "$RUN/podman.log"; then
  ok "the image is told the memory budget and the tier"
else
  bad "the image was not given SDLC_MEMORY and SDLC_TIER"
fi
for target in ci codeql; do
  PATH="$SHIM" sh bin/sdlc "$target" > "$RUN/out" 2>&1 && rc=0 || rc=$?
  if [ "$rc" -eq 2 ] && ! grep -q 'DEFERRED' "$RUN/out"; then
    ok "$target treats a deferral as could-not-run (exit 2)"
  else
    bad "$target let a deferral through (exit $rc)"
  fi
done
rm -f "$SHIM/defer"
# Alone, so the deferral is the LAST status the executor saw.
touch "$SHIM/defer-last"
PATH="$SHIM" sh bin/sdlc push > "$RUN/out" 2>&1 && rc=0 || rc=$?
if [ "$rc" -eq 0 ]; then
  ok "a push whose only check deferred exits 0"
else
  bad "a push whose only check deferred exited $rc"
fi
rm -f "$SHIM/defer-last"

printf '\n== the CPU budget\n\n'

# Halving the machine protects the person at the keyboard. Two cases have no
# such person, and halving them throws away half a machine -- which is what
# made lanes a REGRESSION on a 4-vCPU GitHub runner: budget 2, two lanes at 1
# each, and trentina's `sdlc ci` went from 15m27s to 24m38s.
BUDGET="$RUN/budget.sh"
# shellcheck disable=SC2016
#   Deliberate throughout this block: the sed patterns match literal `$_best`
#   in bin/sdlc, and the echoed line must reach the generated script as source
#   rather than be expanded here.
{
  echo 'set -eu'
  echo 'die() { echo "$*" >&2; exit 2; }'
  sed -n '/^_cpus_in_list()/,/^}$/p' bin/sdlc
  sed -n '/^_cgroup_cpu_ceiling()/,/^  printf .%s. "$_best"$/p' bin/sdlc
  echo '}'
  sed -n '/^budget()/,/^}$/p' bin/sdlc
  echo 'printf "%s" "$(budget)"'
} > "$BUDGET"

machine=$(nproc --all 2>/dev/null || nproc)
half=$((machine / 2))
[ "$half" -lt 1 ] && half=1

got=$(env -u CI sh "$BUDGET")
if [ "$got" = "$half" ]; then
  ok "a developer machine still gets half of it ($got of $machine)"
else
  bad "an unconstrained machine budgeted $got, want $half of $machine"
fi

got=$(CI=true sh "$BUDGET")
if [ "$got" = "$machine" ]; then
  ok "CI gets the whole machine, having no desktop to protect ($got)"
else
  bad "CI budgeted $got, want all $machine"
fi

# A cpuset is the case the first version of this missed: `nproc` is already
# narrowed by it, so comparing the allowance against nproc found nothing.
if command -v taskset >/dev/null 2>&1 && [ "$machine" -ge 2 ]; then
  got=$(env -u CI taskset -c 0-1 sh "$BUDGET")
  if [ "$got" = 2 ]; then
    ok "a cpuset is someone else's decision, so it is taken whole ($got)"
  else
    bad "a 2-CPU cpuset budgeted $got, want 2"
  fi
else
  printf 'skip taskset unavailable or too few CPUs\n'
fi

printf '\n== the lanes\n\n'

# Steps of one lane run in sequence; lanes run at the same time. What has to
# hold is not the speed -- it is that the answer and the log do not depend on
# which lane happened to finish first.


lanes() {              # target [stall-seconds] -> exit status, output in $RUN/out
  rm -f "$SHIM/must-not-run"
  _rc=0
  PATH="$SHIM" SDLC_CPUS=4 SDLC_LANE_STALL=${2:-3600} \
    sh bin/sdlc "$1" > "$RUN/out" 2>&1 || _rc=$?
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
if ! grep -q 'no check has finished for' "$RUN/out"; then
  ok "a healthy run never claims nothing is finishing"
else
  bad "a healthy run claimed no check had finished"
fi

# A check with MORE THAN ONE step gets ONE footer, carrying the time all of its
# steps took. `pytest-in-image` is two steps -- install the lock's pytest into
# the cache volume, then run the tests in the image -- and v0.3.0 gave it two
# footers, the first reading "passed in 0s", because the footer was written per
# step. trentina's first lane run is what showed it.
lanes lanes-two-step && rc=0 || rc=$?
footers=$(grep -c '^== pair passed in' "$RUN/out" || true)
if [ "$footers" = 1 ]; then
  ok "a two-step check gets exactly one footer"
else
  bad "a two-step check printed $footers footers, want 1"
  grep '^== ' "$RUN/out" | sed 's/^/       /'
fi
# Both steps took a second, so the one footer must report their sum, not the
# last step alone -- and never 0s.
if grep -qE '^== pair passed in [2-9][0-9]*s' "$RUN/out"; then
  ok "the footer reports the time every step took"
else
  bad "the footer does not sum the check's steps"
  grep '^== pair' "$RUN/out" | sed 's/^/       /'
fi
if grep -q 'RAN step-one' "$RUN/out" && grep -q 'RAN step-two' "$RUN/out"; then
  ok "both steps of the check ran"
else
  bad "a step of the two-step check did not run"
fi

# Nothing finishing for STALL_MAX seconds. The case worth guarding is a lane
# killed outright, which runs no EXIT trap and so never reports at all, leaving
# the printer polling for a marker that is never coming. At the threshold it
# gives up the ORDER -- and must still reap every lane for its real status, and
# still print the late block WHOLE rather than reading a file another process
# is writing. Reached here with a one-second threshold and a check that takes
# six, which is the OTHER way in and the cheaper one to arrange.
lanes lanes-stall 1 && rc=0 || rc=$?
if grep -q 'no check has finished for 1s' "$RUN/out"; then
  ok "the printer gives up the order and says what it measured"
else
  bad "the stall threshold was never reached"
fi
if [ "$rc" -eq 1 ]; then
  ok "a lane's status is still collected after the order is given up"
else
  bad "giving up on the order lost the lane's status (exit $rc)"
fi
if grep -q '^== f passed in [0-9]*s' "$RUN/out"; then
  ok "the block that did finish in time is still printed"
else
  bad "the finished block was dropped when the order was given up"
fi
# LAST LINE is written six seconds in, after the printer gave up at one. It can
# only be here if the block was printed after the lane was reaped.
if grep -q '^LAST LINE$' "$RUN/out" && grep -q 'sdlc: s FAILED after' "$RUN/out"; then
  ok "the late block is printed whole, after its lane was reaped"
else
  bad "the late block was read while its lane was still writing it"
  sed 's/^/       /' "$RUN/out"
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
log_mark
lanes lanes-budget || true
log_since
if grep -q -- '--cpus 2 ' "$RUN/podman.log" \
   && grep -q -- '--cpu-quota 200000' "$RUN/podman.log"; then
  ok "a budget of 4 is split 2 and 2 between two lanes"
else
  bad "the lanes did not divide the CPU budget"
  grep -o -- '--cpus [0-9]*\|--cpu-quota [0-9]*' "$RUN/podman.log" | sort -u \
    | sed 's/^/       /'
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
if [ "$rc" -eq 0 ]; then
  ok "a passing review after a non-empty diff reports success"
else
  bad "a passing review after a non-empty diff exited $rc"
fi

printf '\n== a lane that is not on a CPU\n\n'

# A reviewer waiting on an API is not competing for the machine, so it runs
# BESIDE the lane that is -- and the lane that is keeps the whole budget and
# keeps the terminal. Buffering the working lane too, as run_lanes does, would
# mean a push shows nothing for the length of the test suite.
review() {             # [fails] -> exit status, output in $RUN/out
  rm -f "$SHIM/review-fails"
  [ "${1:-}" = fails ] && touch "$SHIM/review-fails"
  # Not truncated: the argv assertions at the end of this file read the whole
  # log, so this run's lines are taken from where it was instead.
  log_mark
  _rc=0
  PATH="$GITSHIM" GATEHOUSE_ENV="$ENVFILE" SDLC_CPUS=4 \
    sh bin/sdlc lanes-review > "$RUN/out" 2>&1 || _rc=$?
  log_since
  rm -f "$SHIM/review-fails"
  return "$_rc"
}

review && rc=0 || rc=$?
if [ "$rc" -eq 0 ]; then
  ok "two lanes, one of them off the CPU, pass together"
else
  bad "a passing split run exited $rc"
  sed 's/^/       /' "$RUN/out"
fi

# The working lane streamed: its output is there, and the buffered lane's block
# comes after all of it rather than interleaved into it.
fg_end=$(grep -n 'END tick-x' "$RUN/out" | cut -d: -f1)
rev_at=$(grep -n 'REVIEW CLEAN' "$RUN/out" | cut -d: -f1)
if [ -n "$fg_end" ] && [ -n "$rev_at" ] && [ "$rev_at" -gt "$fg_end" ]; then
  ok "the lane off the CPU is printed whole, after the one that streamed"
else
  bad "the two lanes' output was not separated (fg=$fg_end review=$rev_at)"
  sed 's/^/       /' "$RUN/out"
fi

# Said up front, before the streaming starts, so nobody wonders what else is
# running. The check's name comes from the plan: no check is named in bin/sdlc.
if grep -q 'beside the rest: rev' "$RUN/out"; then
  ok "the run says what is going on beside it, and names it from the plan"
else
  bad "the split run did not say what was running beside the main lane"
fi

# The regression guard. Dividing the budget by the LANE count would have given
# the working lane 2 of 4 to make room for a lane that wants no CPU at all --
# which is how lanes were measured as a regression in the first place.
if grep -q -- '--cpus 4 ' "$RUN/podman.log" && grep -q -- '--cpus 1 ' "$RUN/podman.log"; then
  ok "the working lane kept the whole budget; the other took one CPU"
else
  bad "the budget was divided with a lane that runs no container"
  grep -o -- '--cpus [0-9]*' "$RUN/podman.log" | sort -u | sed 's/^/       /'
fi

# And the verdict is the worst of the two, whichever lane it came from.
review fails && rc=0 || rc=$?
if [ "$rc" -eq 1 ]; then
  ok "a finding in the lane off the CPU fails the command"
else
  bad "a failing review in a split run exited $rc, want 1"
  sed 's/^/       /' "$RUN/out"
fi

rm -rf "$GITSHIM"

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

printf '\n== arguments for one check\n\n'

# `sdlc pytest tests/x.py` used to run the whole suite: the dispatcher read
# "$1" and dropped the rest without a word. Silently doing the most expensive
# possible thing is worse than refusing, so an argument that is not behind `--`
# is now an exit 2, and `--` on a tier is too.
args_case() {          # expect-rc target [args...]
  _want=$1; shift
  _what="$*"
  _got=0
  PATH="$SHIM" sh bin/sdlc "$@" >/dev/null 2>&1 || _got=$?
  if [ "$_got" -eq "$_want" ]; then
    ok "'$_what' exits $_got"
  else
    bad "'$_what' exits $_got, want $_want"
  fi
}

args_case 2 mypy tests/x.py
args_case 2 mypy --
args_case 2 push -- -x
args_case 2 commit -- -x
args_case 0 mypy -- -q

# Handed over in the ENVIRONMENT, not built into the argv: podman is told the
# variable's name and the kernel carries the value, so no word of it is ever a
# word of a command line here.
log_mark
PATH="$SHIM" sh bin/sdlc mypy -- --tb=short -n0 >/dev/null 2>&1 || true
log_since
if grep -q -- '-e SDLC_ARGS' "$RUN/podman.log" && ! grep -q -- '--tb=short' "$RUN/podman.log"
then
  ok "the check's arguments reach the container by name, not as argv words"
else
  bad "SDLC_ARGS was not passed, or its value leaked into the podman argv"
  grep -o -- '-e SDLC_[A-Z]*\|--tb=short' "$RUN/podman.log" | sort -u | sed 's/^/       /'
fi

# A tier clears it rather than refusing it, so an exported one cannot reach a
# tier's pytest by the back door.
log_mark
PATH="$SHIM" SDLC_ARGS="--lf" sh bin/sdlc push >/dev/null 2>&1 || true
log_since
if ! grep -q -- '-e SDLC_ARGS' "$RUN/podman.log"; then
  ok "an inherited SDLC_ARGS does not reach a tier"
else
  bad "a tier forwarded SDLC_ARGS from the environment"
fi

printf '\n== what the containers are given\n\n'

# The worktree is the developer's, so its __pycache__ is the laptop's. Every
# container that mounts it must keep its bytecode somewhere else, or a module
# compiled on the host decides a verdict in here -- four of the five pushes on
# trentina#706. tests/test_pycache_prefix.py tests the property; this asserts
# the env var reaches every such container.
mounts=$(grep -c ':/repo:' "$LOG" 2>/dev/null || true)
prefixed=$(grep ':/repo:' "$LOG" 2>/dev/null | grep -c 'PYTHONPYCACHEPREFIX=/tmp/pycache' || true)
if [ "${mounts:-0}" -gt 0 ] && [ "$prefixed" = "$mounts" ]; then
  ok "all $mounts container(s) mounting the worktree got their own bytecode cache"
else
  bad "$prefixed of ${mounts:-0} containers mounting the worktree set PYTHONPYCACHEPREFIX"
fi

# A gate runs the repo's own code, so its containers get no capabilities and no
# way to acquire one. Asserted over every `run` the launcher itself builds --
# `podman volume` lines and a repo's own host steps are not it.
runs=$(grep -c 'run --rm' "$LOG" 2>/dev/null || true)
locked=$(grep 'run --rm' "$LOG" 2>/dev/null \
  | grep 'cap-drop=all' | grep -c 'no-new-privileges' || true)
if [ "${runs:-0}" -gt 0 ] && [ "$locked" = "$runs" ]; then
  ok "all $runs gate container(s) ran with no capabilities"
else
  bad "$locked of ${runs:-0} gate containers dropped capabilities"
fi

# The constitution is cloned by the HOST's git and published with `ln -sn`, so
# runs that meet on a cold cache never delete each other's clone. Three run at
# once against a git whose clone is slow and offline; what must be left is one
# checkout, one link to it, and no loser's copy.
CLONESHIM=$(mktemp -d)
CLONECACHE=$(mktemp -d)
for prog in $POSIX_UTILS podman pre-commit gh; do
  [ -e "$SHIM/$prog" ] && ln -s "$(readlink -f "$SHIM/$prog")" "$CLONESHIM/$prog"
done
REALGIT=$(command -v git)
cat > "$CLONESHIM/git" <<EOF
#!/bin/sh
case "\$1" in
  clone) for dest; do :; done; sleep 1; : > "\$dest/constitution.md" ;;
  *) exec "$REALGIT" "\$@" ;;
esac
EOF
chmod +x "$CLONESHIM/git"
# The check's own verdict depends on the checkout (CI has no pre-push hook), so
# each concurrent run must match one that had a cache to itself.
_solo=$(mktemp -d)
PATH="$CLONESHIM" XDG_CACHE_HOME="$_solo" sh bin/sdlc hooks-installed >/dev/null 2>&1 \
  && _want=0 || _want=$?
rm -rf "$_solo"
_pids=
for _ in 1 2 3; do
  PATH="$CLONESHIM" XDG_CACHE_HOME="$CLONECACHE" sh bin/sdlc hooks-installed >/dev/null 2>&1 &
  _pids="$_pids $!"
done
_odd=0
for _pid in $_pids; do
  wait "$_pid" && _got=0 || _got=$?
  [ "$_got" = "$_want" ] || _odd=$((_odd + 1))
done
if [ "$_odd" = 0 ]; then
  ok "three runs racing one cold cache each exit $_want, as a run alone does"
else
  bad "$_odd of 3 runs racing one cold cache did not exit $_want"
fi
_root="$CLONECACHE/python-sdlc/constitution"
_links=$(find "$_root" -mindepth 1 -maxdepth 1 -type l | wc -l | tr -d ' ')
_dirs=$(find "$_root" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')
_nested=$(find "$_root" -mindepth 2 -name '.v*' | wc -l | tr -d ' ')
if [ "$_links" = 1 ] && [ "$_dirs" = 1 ] && [ "$_nested" = 0 ] \
   && [ -f "$(find "$_root" -mindepth 1 -maxdepth 1 -type l)/constitution.md" ]; then
  ok "three runs on a cold cache leave one constitution checkout and one link"
else
  bad "concurrent constitution clones left $_links link(s), $_dirs dir(s), $_nested nested"
fi
rm -rf "$CLONESHIM" "$CLONECACHE"

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf 'host contract PASSED\n'; exit 0
fi
printf 'host contract FAILED: %s check(s)\n' "$FAILED"; exit 1
