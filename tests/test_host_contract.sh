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
# The digest every pulled tag resolves to, which the bootstrap then verifies
# and runs by.
DIG64=feedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedface
TAB=$(printf '\t')
US=$(printf '\037')
cat > "$SHIM/podman" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >> "$LOG"
for _last in "\$@"; do :; done
# container-smoke: a detached run records which case it is, and inspect
# answers that case's state as: Running ExitCode OOMKilled.
# The image's own entrypoint and command, for the systemd PID 1 check: an
# image named systemd-ep has ENTRYPOINT /sbin/init, one named systemd-cmd only
# CMD /usr/lib/systemd/systemd, and any other image neither.
case " \$* " in
  *" image inspect "*)
    case "\$*:\$_last" in
      *.Digest*) printf 'sha256:%s\n' "$DIG64" ;;
      *Entrypoint*:*systemd-ep*) printf '/sbin/init\n' ;;
      *Cmd*:*systemd-cmd*)       printf '/usr/lib/systemd/systemd\n--system\n' ;;
    esac
    exit 0 ;;
esac
case " \$* " in
  *" inspect "*)
    _st=\$(cat "$SHIM/upcase" 2>/dev/null)
    case "\$_st" in
      go-up-stays)    _full='true 0 false' ;;
      go-up-exits-0)  _full='false 0 false' ;;
      go-up-dies-255) _full='false 255 false' ;;
      go-up-dies-3)   _full='false 3 false' ;;
      go-up-oom)      _full='false 137 true' ;;
      go-up-exit0-oom) _full='false 0 true' ;;
      *) exit 125 ;;
    esac
    case " \$* " in
      *ExitCode*) printf '%s\n' "\$_full" ;;
      *) printf '%s\n' "\${_full%% *}" ;;
    esac
    exit 0 ;;
  *" logs "*) printf 'CONTAINER LOG\n'; exit 0 ;;
  *" rm -f "*) exit 0 ;;
esac
# sdlc launcher REF: print this repo's launcher with the ref written in, as
# the image does -- or fail, or print nothing, as $SHIM/launchcase says.
case " \$* " in
  *" launcher "*)
    case "\$(cat "$SHIM/launchcase" 2>/dev/null)" in
      fail)  exit 125 ;;
      empty) exit 0 ;;
    esac
    sed "s|^LAUNCHED_FROM=\$|LAUNCHED_FROM='\$_last'|" "$PWD/bin/sdlc"
    exit 0 ;;
esac
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
  *" plan gate-word "*)
    printf 'gate-word${TAB}main${TAB}host${TAB}podman${US}run${US}--rm${US}--${US}@sdlc-image${US}scan-rootfs\n' ;;
  *" plan inject-up "*)
    printf 'inject-up${TAB}main${TAB}host-up${TAB}5${US}podman${US}run${US}--${US}--privileged${US}-v${US}/:/host\n' ;;
  *" plan inject-run "*)
    printf 'inject-run${TAB}main${TAB}host${TAB}podman${US}run${US}--rm${US}--${US}-x\n' ;;
  *" plan inject-comma "*)
    printf 'inject-comma${TAB}main${TAB}host${TAB}podman${US}run${US}--rm${US}--${US}a,b\n' ;;
  *" plan inject-nodd "*)
    printf 'inject-nodd${TAB}main${TAB}host-up${TAB}5${US}podman${US}run${US}--privileged${US}img\n' ;;
  *" plan up-systemd-ep "*)
    printf 'up-systemd-ep${TAB}main${TAB}host-up${TAB}1${US}podman${US}run${US}--${US}localhost/systemd-ep:sdlc\n' ;;
  *" plan up-systemd-cmd "*)
    printf 'up-systemd-cmd${TAB}main${TAB}host-up${TAB}1${US}podman${US}run${US}--${US}localhost/systemd-cmd:sdlc\n' ;;
  *" plan up-systemd-args "*)
    printf 'up-systemd-args${TAB}main${TAB}host-up${TAB}1${US}podman${US}run${US}--${US}localhost/systemd-cmd:sdlc${US}go-up-stays\n' ;;
  *" plan lanes-skip "*)
    printf 'b${TAB}one${TAB}image-ro${TAB}quiet-ok\n'
    printf 'up-systemd-ep${TAB}two${TAB}host-up${TAB}1${US}podman${US}run${US}--${US}localhost/systemd-ep:sdlc\n' ;;
  *" plan up-adv-dies "*)
    printf 'up-adv-dies${TAB}main${TAB}host-up-advisory${TAB}1${US}podman${US}run${US}--${US}localhost/x:sdlc${US}go-up-dies-255\n' ;;
  *" plan up-adv-podman "*)
    printf 'up-adv-podman${TAB}main${TAB}host-up-advisory${TAB}1${US}podman${US}run${US}--${US}localhost/x:sdlc${US}go-up-podman\n' ;;
  *" plan lanes-adv "*)
    printf 'b${TAB}one${TAB}image-ro${TAB}quiet-ok\n'
    printf 'up-adv-dies${TAB}two${TAB}host-up-advisory${TAB}1${US}podman${US}run${US}--${US}localhost/x:sdlc${US}go-up-dies-255\n' ;;
  *" plan lint-unparsed "*)
    printf 'containerfile-lint${TAB}main${TAB}image-ro${TAB}containerfile-lint\n' ;;
  *" plan rc-five "*)
    printf 'rc-five${TAB}main${TAB}image-ro${TAB}exit-five\n' ;;
  *" plan rc-four "*)
    printf 'rc-four${TAB}main${TAB}image-ro${TAB}exit-four\n' ;;
  *" plan up-"*)
    _secs=1; _what=\$_last
    case "\$_last" in up-build) _what=build ;; up-noseconds) _secs=soon ;; esac
    if [ "\$_what" = build ]; then
      printf '%s${TAB}main${TAB}host-up${TAB}1${US}podman${US}build${US}-t${US}x${US}.\n' "\$_last"
    else
      printf '%s${TAB}main${TAB}host-up${TAB}%s${US}podman${US}run${US}--${US}localhost/x:sdlc${US}go-%s\n' \
        "\$_last" "\$_secs" "\$_last"
    fi ;;
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
  go-up-*)
    printf '%s\n' "\$_last" > "$SHIM/upcase"
    case "\$_last" in go-up-podman) exit 125 ;; go-up-127) exit 127 ;; esac ;;
  rc-one)      printf 'RAN rc-one\n'; exit 1 ;;
  rc-two)      printf 'RAN rc-two\n'; exit 2 ;;
  rc-three)    printf 'RAN rc-three\n'; exit 3 ;;
  exit-four)   printf 'RAN exit-four\n'; exit 4 ;;
  exit-five)   printf 'RAN exit-five\n'; exit 5 ;;
  containerfile-lint) printf 'ADVISORY: hadolint could not parse\n'; exit 4 ;;
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

printf '\n== a run given time to stay up\n\n'

# container-smoke's step: `host-up`, seconds and then a podman run. The
# container starts detached and its STATE decides: still running, or exited 0,
# passes; any other exit, the OOM killer, or an entrypoint that cannot start
# (126/127) is a finding; podman failing to create it (125) is a 2. A 255 is
# just another exit -- no clock is consulted.
for case_ in up-stays:0 up-exits-0:0 up-exit0-oom:0 up-dies-255:1 up-dies-3:1 up-oom:1 up-127:1 \
             up-podman:2 up-build:2 up-noseconds:2; do
  name=${case_%:*}; want=${case_#*:}
  log_mark
  PATH="$SHIM" sh bin/sdlc "$name" > "$RUN/out" 2>&1 && rc=0 || rc=$?
  log_since
  if [ "$rc" -eq "$want" ]; then
    ok "host-up $name exits $want"
  else
    bad "host-up $name exited $rc, want $want: $(tail -3 "$RUN/out")"
  fi
done
log_mark
PATH="$SHIM" sh bin/sdlc up-stays >/dev/null 2>&1 || true
log_since
if grep -q -- 'run -d --name sdlc-smoke-[0-9-]* --log-driver=k8s-file --cap-drop=all --security-opt=no-new-privileges .* -- localhost/x:sdlc go-up-stays$' "$RUN/podman.log" \
   && grep -q -- '^rm -f -v sdlc-smoke-' "$RUN/podman.log"; then
  ok "host-up runs detached and locked down, and removes the container and its volumes"
else
  bad "host-up's run or cleanup is wrong: $(grep -E 'up-stays|rm -f' "$RUN/podman.log" | tail -2)"
fi

# A systemd PID 1 cannot boot under this run's lockdown, so container-smoke
# says SKIPPED -- never "passed" -- and starts nothing. Not for an image whose
# CMD a run argument replaces, and a 4 from anything else is still a finding.
for case_ in up-systemd-ep up-systemd-cmd; do
  log_mark
  rc=0
  PATH="$SHIM" sh bin/sdlc "$case_" > "$RUN/out" 2>&1 || rc=$?
  log_since
  if [ "$rc" -eq 0 ] && grep -q "SKIPPED: the image starts .* as PID 1, and systemd PID 1 needs privileges smoke cannot grant" "$RUN/out" \
     && grep -q "== $case_ SKIPPED, not judged" "$RUN/out" && ! grep -q 'passed in' "$RUN/out" \
     && ! grep -q ' run -d ' "$RUN/podman.log"; then
    ok "$case_: a systemd PID 1 is SKIPPED with its reason, never run and never a pass"
  else
    bad "$case_ exited $rc: $(tail -3 "$RUN/out")"
  fi
done
log_mark
rc=0
PATH="$SHIM" sh bin/sdlc up-systemd-args > "$RUN/out" 2>&1 || rc=$?
log_since
if [ "$rc" -eq 0 ] && grep -q ' run -d ' "$RUN/podman.log" && ! grep -q SKIPPED "$RUN/out"; then
  ok "a run argument that replaces a systemd CMD is run and judged"
else
  bad "up-systemd-args exited $rc: $(tail -2 "$RUN/out")"
fi
rc=0
PATH="$SHIM" SDLC_CPUS=4 sh bin/sdlc lanes-skip > "$RUN/out" 2>&1 || rc=$?
if [ "$rc" -eq 0 ] && grep -q '== up-systemd-ep SKIPPED, not judged' "$RUN/out" \
   && grep -q '== b passed' "$RUN/out"; then
  ok "in a lane, the skip is printed as SKIPPED and the run still exits 0"
else
  bad "lanes-skip exited $rc: $(tail -4 "$RUN/out")"
fi
rc=0
PATH="$SHIM" sh bin/sdlc rc-four > "$RUN/out" 2>&1 || rc=$?
if [ "$rc" -eq 1 ] && grep -q 'rc-four FAILED' "$RUN/out" && ! grep -q SKIPPED "$RUN/out"; then
  ok "a 4 from a check in the image is a finding, not a skip"
else
  bad "rc-four exited $rc: $(tail -2 "$RUN/out")"
fi

# container-smoke is advisory unless a repo gates it: a finding is printed as
# FOUND SOMETHING, ADVISORY -- never "passed" -- and does not fail the command.
# Podman failing to create the container is still a 2.
rc=0
PATH="$SHIM" sh bin/sdlc up-adv-dies > "$RUN/out" 2>&1 || rc=$?
if [ "$rc" -eq 0 ] && grep -q '== up-adv-dies FOUND SOMETHING, ADVISORY: not gating' "$RUN/out" \
   && grep -q 'gate = true in \[tool.sdlc.ci.up-adv-dies\]' "$RUN/out" && ! grep -q 'passed in' "$RUN/out"; then
  ok "an advisory smoke finding is printed as ADVISORY and the command exits 0"
else
  bad "up-adv-dies exited $rc: $(tail -3 "$RUN/out")"
fi
rc=0
PATH="$SHIM" sh bin/sdlc up-adv-podman > "$RUN/out" 2>&1 || rc=$?
if [ "$rc" -eq 2 ]; then
  ok "an advisory smoke run podman could not create is still a 2"
else
  bad "up-adv-podman exited $rc, want 2"
fi
rc=0
PATH="$SHIM" SDLC_CPUS=4 sh bin/sdlc lanes-adv > "$RUN/out" 2>&1 || rc=$?
if [ "$rc" -eq 0 ] && grep -q '== up-adv-dies FOUND SOMETHING, ADVISORY' "$RUN/out" \
   && grep -q '== b passed' "$RUN/out"; then
  ok "in a lane, an advisory finding is printed as ADVISORY and the run exits 0"
else
  bad "lanes-adv exited $rc: $(tail -4 "$RUN/out")"
fi
rc=0
PATH="$SHIM" sh bin/sdlc rc-five > "$RUN/out" 2>&1 || rc=$?
if [ "$rc" -eq 1 ] && ! grep -q ADVISORY "$RUN/out"; then
  ok "a 5 from a check in the image is a finding, not advisory"
else
  bad "rc-five exited $rc: $(tail -2 "$RUN/out")"
fi
rc=0
PATH="$SHIM" sh bin/sdlc lint-unparsed > "$RUN/out" 2>&1 || rc=$?
if [ "$rc" -eq 0 ] && grep -q '== containerfile-lint SKIPPED, not judged' "$RUN/out"; then
  ok "containerfile-lint may report SKIPPED when hadolint cannot parse"
else
  bad "lint-unparsed exited $rc: $(tail -2 "$RUN/out")"
fi

# Every smoke cleanup, whatever the case, takes the anonymous volumes with it.
if grep -q -- '^rm -f -v sdlc-smoke-' "$LOG" && ! grep -q -- '^rm -f sdlc-smoke-' "$LOG"; then
  ok "every smoke container is removed with -v"
else
  bad "a smoke cleanup left its volumes: $(grep -- '^rm -f sdlc-smoke-' "$LOG" | head -1)"
fi

# A repo's config must never reach the host's podman as an option. The planner
# refuses such an image; the launcher refuses it again, here, on the host.
for case_ in inject-up inject-run inject-comma inject-nodd; do
  log_mark
  rc=0
  PATH="$SHIM" sh bin/sdlc "$case_" > "$RUN/out" 2>&1 || rc=$?
  log_since
  if [ "$rc" -eq 2 ] && ! grep -q -- '--privileged\|a,b\| -x$' "$RUN/podman.log"; then
    ok "$case_: an image word that is not an image is refused (exit 2) and never run"
  else
    bad "$case_ exited $rc: $(grep -E 'privileged|a,b| -x$' "$RUN/podman.log" | tail -1)"
  fi
done

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
  *"remote get-url"*) echo https://github.com/crunchtools/x ;;
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

printf '\n== an image other than the pinned release\n\n'

# The inner loop runs a pull request's image. With no override, nothing about
# the argv may change: no pull flag, the pinned image. With one, the image is
# named on every run and a floating tag is pulled when newer.
pinned="quay.io/crunchtools/python-sdlc:$(tr -d ' \t\n\r' < IMAGE_TAG)"
last_run() { grep ' run --rm' "$RUN/podman.log" | tail -1; }

log_mark
PATH="$SHIM" sh bin/sdlc tiers > "$RUN/out" 2>&1 || true
log_since
if last_run | grep -q -- " $pinned tiers\$" && ! last_run | grep -q -- '--pull' \
   && ! grep -q 'NOT THE PINNED RELEASE' "$RUN/out"; then
  ok "no override: the pinned image, no pull flag, no banner"
else
  bad "no override changed the run: $(last_run)"
fi

log_mark
SDLC_IMAGE=main PATH="$SHIM" sh bin/sdlc tiers > "$RUN/out" 2>&1 || true
log_since
if last_run | grep -q -- '--pull=newer .* quay.io/crunchtools/sdlc:main tiers$' \
   && grep -q 'NOT THE PINNED RELEASE' "$RUN/out" && grep -q 'set by SDLC_IMAGE' "$RUN/out"; then
  ok "SDLC_IMAGE=main runs quay.io/crunchtools/sdlc:main, pulled when newer, and says so"
else
  bad "SDLC_IMAGE=main: $(last_run) / $(head -3 "$RUN/out")"
fi

log_mark
SDLC_IMAGE=localhost/python-sdlc:dev PATH="$SHIM" sh bin/sdlc tiers > "$RUN/out" 2>&1 || true
log_since
if last_run | grep -q -- ' localhost/python-sdlc:dev tiers$' && ! last_run | grep -q -- '--pull'; then
  ok "a local image is run as built, never pulled"
else
  bad "SDLC_IMAGE=localhost/...: $(last_run)"
fi

rc=0
SDLC_IMAGE='main;touch' PATH="$SHIM" sh bin/sdlc tiers > "$RUN/out" 2>&1 || rc=$?
if [ "$rc" -eq 2 ]; then
  ok "a bare SDLC_IMAGE that is not a tag is refused (exit 2)"
else
  bad "SDLC_IMAGE='main;touch' exited $rc"
fi

CREPO=$(mktemp -d)
git -C "$CREPO" init -q
printf '[project]\nname = "x"\n\n[tool.sdlc]\nimage = "0.8.0"\n' > "$CREPO/pyproject.toml"
log_mark
(cd "$CREPO" && CI='' GITHUB_ACTIONS='' PATH="$SHIM" sh "$OLDPWD/bin/sdlc" tiers) > "$RUN/out" 2>&1 || true
log_since
if last_run | grep -q -- 'SDLC_CONFIG_IMAGE .*quay.io/crunchtools/sdlc:0.8.0 tiers$' \
   && grep -q 'set by \[tool.sdlc\] image in pyproject.toml' "$RUN/out"; then
  ok "[tool.sdlc] image = \"0.8.0\" runs that tag and tells the image what it read"
else
  bad "[tool.sdlc] image: $(last_run) / $(head -4 "$RUN/out")"
fi
log_mark
(cd "$CREPO" && CI='' GITHUB_ACTIONS='' SDLC_IMAGE=main PATH="$SHIM" sh "$OLDPWD/bin/sdlc" tiers) > "$RUN/out" 2>&1 || true
log_since
if last_run | grep -q -- 'quay.io/crunchtools/sdlc:main tiers$'; then
  ok "SDLC_IMAGE beats [tool.sdlc] image"
else
  bad "SDLC_IMAGE did not beat the repo's image: $(last_run)"
fi
# The image is the judge, so a pull request must not pick it: CI and `sdlc ci`
# refuse the repo's key outright. The planner cannot hold this line, because
# it runs inside whichever image was chosen.
for how in 'CI=true' 'GITHUB_ACTIONS=true' 'ci'; do
  rc=0
  case "$how" in
    ci) (cd "$CREPO" && CI='' GITHUB_ACTIONS='' PATH="$SHIM" sh "$OLDPWD/bin/sdlc" ci) > "$RUN/out" 2>&1 || rc=$? ;;
    *)  (cd "$CREPO" && CI='' GITHUB_ACTIONS='' env "$how" PATH="$SHIM" sh "$OLDPWD/bin/sdlc" tiers) > "$RUN/out" 2>&1 || rc=$? ;;
  esac
  if [ "$rc" -eq 2 ] && grep -q 'cannot choose the image that judges it' "$RUN/out"; then
    ok "[tool.sdlc] image is refused under $how (exit 2)"
  else
    bad "[tool.sdlc] image under $how exited $rc"
  fi
done
printf '[tool.sdlc]\nimage = "../../evil"\n' > "$CREPO/sdlc.toml"
rm "$CREPO/pyproject.toml"
rc=0
(cd "$CREPO" && CI='' GITHUB_ACTIONS='' PATH="$SHIM" sh "$OLDPWD/bin/sdlc" tiers) > "$RUN/out" 2>&1 || rc=$?
if [ "$rc" -eq 2 ] && grep -q 'must be a tag such as' "$RUN/out"; then
  ok "a repo's image that is not a plain tag is refused (exit 2)"
else
  bad "[tool.sdlc] image = ../../evil exited $rc: $(tail -2 "$RUN/out")"
fi
rm -rf "$CREPO"

printf '\n== the floating launcher, from the image (bin/sdlc-bootstrap)\n\n'

# A repo with no `rev:` runs the launcher the image prints. The bootstrap is the
# whole of what such a repo carries, so it is held to the same host contract:
# this PATH has podman and POSIX utilities and nothing else.
# It verifies each digest's signature before it runs anything from it, and
# runs by that digest (tests/test_bootstrap.sh); this fake podman says every
# digest is signed. The cache of verified digests is this test's own.
boot() { PATH="$SHIM" XDG_CACHE_HOME="$RUN/cache" sh "$PWD/bin/sdlc-bootstrap" "$@"; }
PINNED=quay.io/crunchtools/sdlc@sha256:$DIG64

log_mark
boot tiers > "$RUN/out" 2>&1 || true
log_since
if grep -q -- " verify --key /fleet.pub --insecure-ignore-tlog=true $PINNED\$" "$RUN/podman.log" \
   && grep -q -- "--security-opt=no-new-privileges --pull=never -- $PINNED launcher $PINNED\$" "$RUN/podman.log" \
   && last_run | grep -q -- " $PINNED tiers\$" && ! last_run | grep -q -- '--pull' \
   && ! grep -q 'NOT THE PINNED RELEASE' "$RUN/out"; then
  ok "no SDLC_IMAGE: :latest is verified and runs by its digest, no banner"
else
  bad "bootstrap on :latest: $(last_run) / $(head -3 "$RUN/out")"
fi

log_mark
SDLC_IMAGE=main boot tiers > "$RUN/out" 2>&1 || true
log_since
if grep -q -- "-- $PINNED launcher $PINNED\$" "$RUN/podman.log" \
   && last_run | grep -q -- " $PINNED tiers\$" \
   && grep -q 'NOT THE PINNED RELEASE' "$RUN/out" \
   && grep -q 'quay.io/crunchtools/sdlc:main$' "$RUN/out"; then
  ok "SDLC_IMAGE=main: main's own launcher runs main by digest, and says it is not the release"
else
  bad "bootstrap on main: $(last_run) / $(head -3 "$RUN/out")"
fi

# What the bootstrap runs is what it pulls, so it pulls from one repository:
# quay.io/crunchtools/sdlc at main, latest, a release or a digest.
D64=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
for good in "0.9.0:$PINNED" \
            "@sha256:$D64:quay.io/crunchtools/sdlc@sha256:$D64" \
            "quay.io/crunchtools/sdlc:main:$PINNED" \
            "quay.io/crunchtools/sdlc@sha256:$D64:quay.io/crunchtools/sdlc@sha256:$D64"; do
  case "$good" in
    @sha256:*|quay.io/crunchtools/sdlc@*) value=${good%:quay.io/*}; want=${good#"$value":} ;;
    *) value=${good%%:quay.io/*}; want=${good#"$value":} ;;
  esac
  case "$good" in quay.io/crunchtools/sdlc:main:*) value=quay.io/crunchtools/sdlc:main; want=$PINNED ;; esac
  log_mark
  SDLC_IMAGE=$value boot tiers > "$RUN/out" 2>&1 || true
  log_since
  if grep -q -- "-- $want launcher $want\$" "$RUN/podman.log" && last_run | grep -q -- " $want tiers\$"; then
    ok "SDLC_IMAGE=$value pulls and runs $want"
  else
    bad "SDLC_IMAGE=$value: $(last_run) / $(tail -1 "$RUN/out")"
  fi
done
for bad_ in localhost/sdlc:dev quay.io/evil/sdlc:latest quay.io/crunchtools/sdlcx:latest \
            1.2 v1.2.3 1.2.3.4 1..3 @sha256:abc "@sha256:$(printf '%s' "$D64" | tr a G)" \
            'main;id' '--privileged' quay.io/crunchtools/sdlc \
            quay.io/crunchtools/sdlc1.2.3 quay.io/crunchtools/sdlcmain; do
  log_mark
  rc=0
  SDLC_IMAGE=$bad_ boot tiers > "$RUN/out" 2>&1 || rc=$?
  log_since
  if [ "$rc" -eq 2 ] && grep -q 'is not main, latest, X.Y.Z or @sha256' "$RUN/out" \
     && ! [ -s "$RUN/podman.log" ]; then
    ok "SDLC_IMAGE='$bad_' is refused (exit 2) before podman runs"
  else
    bad "SDLC_IMAGE='$bad_' exited $rc"
  fi
done

# Nothing printed must never become nothing run and exit 0.
for case_ in fail empty; do
  printf '%s\n' "$case_" > "$SHIM/launchcase"
  rc=0
  boot tiers > "$RUN/out" 2>&1 || rc=$?
  if [ "$rc" -eq 2 ]; then
    ok "an image that cannot print its launcher ($case_) is a 2"
  else
    bad "launcher $case_: bootstrap exited $rc"
  fi
done
rm -f "$SHIM/launchcase"

# The launcher's verdict is the bootstrap's: a 1 stays a 1.
rc=0
rm -f "$SHIM/must-not-run"
boot lanes-keeps-going > "$RUN/out" 2>&1 || rc=$?
if [ "$rc" -eq 1 ] && grep -q 'RAN rc-one' "$RUN/out"; then
  ok "the launcher's exit code comes back through the bootstrap"
else
  bad "a failing check through the bootstrap exited $rc"
fi

# Floating, the launcher needs no clone of python-sdlc: no IMAGE_TAG beside it.
FLOAT=$(mktemp -d)
sed "s|^LAUNCHED_FROM=\$|LAUNCHED_FROM='quay.io/crunchtools/sdlc:latest'|" bin/sdlc > "$FLOAT/sdlc"
log_mark
PATH="$SHIM" sh "$FLOAT/sdlc" tiers > "$RUN/out" 2>&1 || true
log_since
if last_run | grep -q -- ' quay.io/crunchtools/sdlc:latest tiers$'; then
  ok "a printed launcher runs with no IMAGE_TAG and no clone"
else
  bad "printed launcher alone: $(last_run) / $(head -2 "$RUN/out")"
fi
rm -rf "$FLOAT"

# A converted repo carries the bootstrap as .sdlc/bin/sdlc, and its hooks and
# CI job start that file -- a script in the repo, which is what XII's host
# contract lets a `system` hook run -- and nothing else.
if grep -q '^        entry: \.sdlc/bin/sdlc commit$' examples/pre-commit-floating.yaml \
   && grep -q '^        entry: \.sdlc/bin/sdlc push$' examples/pre-commit-floating.yaml \
   && grep -q '^        run: \.sdlc/bin/sdlc ci$' examples/ci.yml; then
  ok "the floating hooks and the CI stub run .sdlc/bin/sdlc"
else
  bad "examples/ do not run .sdlc/bin/sdlc as the hooks and the CI job"
fi

# container-scan runs the gate's own image as a host step, and only the
# launcher knows which image that is: the plan names it `@sdlc-image`.
log_mark
SDLC_IMAGE=main PATH="$SHIM" sh bin/sdlc gate-word >/dev/null 2>&1 || true
log_since
if grep -q -- '--cap-drop=all .* quay.io/crunchtools/sdlc:main scan-rootfs$' "$RUN/podman.log" \
   && ! grep -q '@sdlc-image' "$RUN/podman.log"; then
  ok "a host step's @sdlc-image is the image this run is on"
else
  bad "@sdlc-image was not replaced: $(grep scan-rootfs "$RUN/podman.log" | tail -1)"
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
# The mount must name the checkout itself. `z` relabels the path podman is
# given, so a symlink source relabels only the link and SELinux refuses the
# directory behind it.
log_mark
PATH="$CLONESHIM" XDG_CACHE_HOME="$CLONECACHE" sh bin/sdlc mypy >/dev/null 2>&1 || :
log_since
_mnt=$(tr ' ' '\n' < "$RUN/podman.log" | grep ':/cache/constitution/' | head -1)
_mnt=${_mnt%%:*}
if [ -n "$_mnt" ] && [ -d "$_mnt" ] && [ ! -L "$_mnt" ]; then
  ok "the constitution mount names the checkout, not the link"
else
  bad "the constitution mount source is '${_mnt:-absent}', not a resolved directory"
fi
rm -rf "$CLONESHIM" "$CLONECACHE"

printf '\n== gatehouse: only a crunchtools GitHub push is reviewed\n\n'

# Gatehouse sends the diff to an external model, so at the push tier it runs
# only when the remote is a crunchtools repo on GitHub. The functions are lifted
# out of bin/sdlc and run with a shell function standing in for git, which is
# how the remote lookup is pinned without a remote.
GH_FUNCS="$RUN/gh-funcs.sh"
sed -n '/^gatehouse_remote_url()/,/^t_gatehouse()/p' bin/sdlc | sed '$d' > "$GH_FUNCS"

matches() {            # URL -> 0 when it is a crunchtools GitHub repo
  sh -c '. "$1"; is_crunchtools_github "$2"' sh "$GH_FUNCS" "$1"
}
for url in \
  https://github.com/crunchtools/python-sdlc \
  https://github.com/crunchtools/python-sdlc.git \
  https://github.com/CrunchTools/python-sdlc \
  https://user:token@github.com/crunchtools/python-sdlc \
  git@github.com:crunchtools/python-sdlc \
  git@github.com:crunchtools/python-sdlc.git \
  ssh://git@github.com/crunchtools/python-sdlc \
  http://trentina:8019/proxy/git/github/crunchtools/developer \
  https://trentina.crunchtools.com/proxy/git/github/crunchtools/josui \
  HTTPS://GITHUB.COM/crunchtools/x \
  https://github.com/crunchtools/x/ \
  https://github.com/crunchtools/x.git/ \
  https://Trentina.CrunchTools.com/proxy/git/github/crunchtools/x
do
  if matches "$url"; then ok "crunchtools: $url"; else bad "not matched, should be: $url"; fi
done
for url in \
  git@gitlab.cee.redhat.com:foo/bar \
  https://gitlab.com/redhat/hummingbird/containers.git \
  ssh://breetai/~/Projects/MarketLens \
  https://github.com/someoneelse/x \
  https://github.com/crunchtools-fork/x \
  https://github.com/crunchtools/x/y \
  https://github.com/crunchtools \
  https://github.com@evil.example/crunchtools/x \
  http://trentina:8019/proxy/git/gitlab/crunchtools/x \
  https://attacker.example/proxy/git/github/crunchtools/x \
  http://trentina.evil.example:8019/proxy/git/github/crunchtools/x \
  https://trentina.crunchtools.com/x/proxy/git/github/crunchtools/y \
  git@github.com:someoneelse/crunchtools \
  'https://evil.example#@github.com/crunchtools/x' \
  'http://evil.example?@trentina/proxy/git/github/crunchtools/x' \
  'http://trentina:8019/proxy/git/github/crunchtools/..%2fother%2fx' \
  https://github.com/crunchtools/.. \
  https://github.com/crunchtools/. \
  'https://github.com/crunchtools/x%2fy' \
  https://github.com:evil/crunchtools/x \
  'https://trentina:80x/proxy/git/github/crunchtools/x' \
  https://user@evil.example@github.com/crunchtools/x \
  'https://github.com/crunchtools/x\y' \
  'https://github.com/crunchtools/x y' \
  https://github.com/crunchtools/x// \
  https://github.com/crunchtools/.git \
  git@evil.example:crunchtools/x \
  git@github.com:/crunchtools/x \
  http://github.com/crunchtools/x
do
  if matches "$url"; then bad "matched, should not be: $url"; else ok "not crunchtools: $url"; fi
done
NL='
'
for url in "https://github.com/crunchtools/x${NL}" "https://evil.example/${NL}https://github.com/crunchtools/x" \
           "https://github.com/crunchtools/x$(printf '\t')"
do
  if matches "$url"; then bad "matched with a newline or tab in it"; else ok "not crunchtools: URL with an embedded newline or tab"; fi
done

# The decision, with git faked: $GH_UPSTREAM is the branch's remote, and a
# remote named in $GH_REMOTES resolves to its URL.
decide() {             # [TIER] -> exit status; output in $RUN/gh-out
  SDLC_TIER=${1:-} sh -c '
    . "$1"; ME=sdlc
    git() {
      case "$*" in
        "symbolic-ref --quiet --short HEAD") echo topic ;;
        "config --get branch.topic.remote") [ -z "$GH_UPSTREAM" ] || echo "$GH_UPSTREAM" ;;
        "remote get-url --push origin") [ -z "$GH_ORIGIN" ] || echo "$GH_ORIGIN" ;;
        "remote get-url --push fork")   [ -z "$GH_FORK" ] || echo "$GH_FORK" ;;
        *) return 1 ;;
      esac
    }
    gatehouse_skipped
  ' sh "$GH_FUNCS" > "$RUN/gh-out" 2>&1
}

unset PRE_COMMIT_REMOTE_URL PRE_COMMIT_REMOTE_NAME GH_UPSTREAM GH_ORIGIN GH_FORK
export GH_UPSTREAM GH_ORIGIN GH_FORK

PRE_COMMIT_REMOTE_URL=https://github.com/crunchtools/x decide push && rc=0 || rc=$?
if [ "$rc" -ne 0 ]; then ok "a crunchtools remote is reviewed at the push tier"; else bad "crunchtools push was skipped"; fi

PRE_COMMIT_REMOTE_URL=https://u:pw@gitlab.com/redhat/x.git decide push && rc=0 || rc=$?
if [ "$rc" -eq 0 ] && grep -q 'GATEHOUSE SKIPPED' "$RUN/gh-out" \
   && grep -q 'https://\*\*\*@gitlab.com/redhat/x.git' "$RUN/gh-out" && ! grep -q 'pw' "$RUN/gh-out"; then
  ok "another remote is skipped, loudly, with its credentials stripped"
else
  bad "a non-crunchtools push was not skipped loudly (rc=$rc)"; sed 's/^/       /' "$RUN/gh-out"
fi

# Every tier is guarded, a hand run included.
unset SDLC_GATEHOUSE_ANY_REMOTE
GH_ORIGIN=git@gitlab.cee.redhat.com:foo/bar decide && rc=0 || rc=$?
if [ "$rc" -eq 0 ] && grep -q 'GATEHOUSE SKIPPED' "$RUN/gh-out"; then
  ok "a hand run against a non-crunchtools remote skips, loudly"
else
  bad "a hand run was not skipped (rc=$rc)"
fi
GH_ORIGIN=https://github.com/crunchtools/x decide && rc=0 || rc=$?
if [ "$rc" -ne 0 ]; then ok "a hand run against crunchtools runs"; else bad "a hand run against crunchtools was skipped"; fi
for tier in commit pre-commit gatehouse; do
  GH_ORIGIN=git@gitlab.com:a/b decide "$tier" && rc=0 || rc=$?
  if [ "$rc" -eq 0 ]; then ok "tier $tier is guarded too"; else bad "tier $tier was not guarded"; fi
done

PRE_COMMIT_REMOTE_URL=https://github.com/crunchtools/x GH_ORIGIN=git@gitlab.com:a/b decide && rc=0 || rc=$?
if [ "$rc" -ne 0 ]; then ok "PRE_COMMIT_REMOTE_URL wins over origin (crunchtools)"; else bad "origin beat PRE_COMMIT_REMOTE_URL"; fi
PRE_COMMIT_REMOTE_URL=git@gitlab.com:a/b GH_ORIGIN=https://github.com/crunchtools/x decide && rc=0 || rc=$?
if [ "$rc" -eq 0 ]; then ok "PRE_COMMIT_REMOTE_URL wins over origin (other)"; else bad "origin beat PRE_COMMIT_REMOTE_URL"; fi

# The override: exactly 1, loud, never anything else.
SDLC_GATEHOUSE_ANY_REMOTE=1 GH_ORIGIN=git@gitlab.com:a/b decide && rc=0 || rc=$?
if [ "$rc" -ne 0 ] && grep -q 'GATEHOUSE GUARD OVERRIDDEN' "$RUN/gh-out"; then
  ok "SDLC_GATEHOUSE_ANY_REMOTE=1 runs it, loudly"
else
  bad "the override did not apply loudly (rc=$rc)"
fi
for v in 0 true yes 11 ' 1' ''; do
  SDLC_GATEHOUSE_ANY_REMOTE=$v GH_ORIGIN=git@gitlab.com:a/b decide && rc=0 || rc=$?
  if [ "$rc" -eq 0 ]; then ok "SDLC_GATEHOUSE_ANY_REMOTE='$v' is not the override"; else bad "override accepted '$v'"; fi
done

# By hand: no pre-commit variables, so the upstream remote, then origin.
GH_UPSTREAM=fork GH_FORK=git@github.com:crunchtools/x decide push && rc=0 || rc=$?
if [ "$rc" -ne 0 ]; then ok "no URL given: the branch's upstream remote decides"; else bad "upstream remote was not used"; fi

GH_UPSTREAM=fork GH_FORK=git@gitlab.com:a/b GH_ORIGIN=https://github.com/crunchtools/x decide push && rc=0 || rc=$?
if [ "$rc" -eq 0 ]; then ok "the upstream remote wins over origin"; else bad "origin was used over the upstream"; fi

GH_ORIGIN=https://github.com/crunchtools/x decide push && rc=0 || rc=$?
if [ "$rc" -ne 0 ]; then ok "no upstream: origin decides"; else bad "origin was not used"; fi

PRE_COMMIT_REMOTE_NAME=fork GH_FORK=https://gitlab.com/a/b decide push && rc=0 || rc=$?
if [ "$rc" -eq 0 ]; then ok "pre-commit's remote name is looked up when its URL is absent"; else bad "remote name ignored"; fi

decide push && rc=0 || rc=$?
if [ "$rc" -eq 0 ] && grep -q 'GATEHOUSE SKIPPED: no remote URL' "$RUN/gh-out"; then
  ok "no URL anywhere is a loud skip, not a failure"
else
  bad "no remote found did not skip loudly (rc=$rc)"; sed 's/^/       /' "$RUN/gh-out"
fi

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf 'host contract PASSED\n'; exit 0
fi
printf 'host contract FAILED: %s check(s)\n' "$FAILED"; exit 1
