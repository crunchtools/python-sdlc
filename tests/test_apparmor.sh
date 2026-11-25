#!/bin/sh
# A gate container can signal its own processes, on every podman this machine
# has: rootless, and rootful through `sudo -n` where that works (the fleet's
# runners hand the gate root's podman socket). crunchtools/sdlc#181.
#
# On an AppArmor host, no-new-privileges makes AppArmor stack the container
# profile onto crun's (`containers-default-<v>//&crun`) and a process cannot
# kill its own child, so bin/sdlc drops no-new-privileges there and keeps the
# profile enforced. tests/test_host_contract.sh pins that bin/sdlc passes these
# options; this test runs them for real. On an AppArmor host the systemd
# container must also boot and stop a unit, and the old setting is tried once
# so the log says whether the kernel still stacks (when it stops, #181's
# workaround can go).
#
# Usage: tests/test_apparmor.sh
set -u
cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)" || exit 2

IMG=docker.io/library/alpine:3.22@sha256:9db2934886712d877c932e07249c75a59e6ca27adfbccc05261987a17bef2d63
SDIMG=registry.access.redhat.com/ubi10/ubi-init@sha256:1938686cb8d7a2a69440d55f5e7b9b5efb61eea460a654879e69abf54b5b9faa
SYSTEMD_CAPS=$(sed -n 's/^SYSTEMD_CAPS=//p' bin/sdlc)
# bin/sdlc's own rule for no-new-privileges, from `NNP=` to the blank line after
# it, so changing the rule there changes what runs here.
NNP_RULE=$(sed -n '/^NNP=/,/^$/p' bin/sdlc)
T="timeout -k 5 30"
FAILED=0
ok()  { printf 'ok   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }
cannot() { printf 'test_apparmor: CANNOT RUN: %s\n' "$*" >&2; exit 2; }

printf '== gate containers signal their own processes\n\n'
[ -n "$SYSTEMD_CAPS" ] || cannot "no SYSTEMD_CAPS= line in bin/sdlc"
case "$NNP_RULE" in
  *podman\ info*) ;;
  *) cannot "no NNP= rule asking podman info in bin/sdlc" ;;
esac

# Prints KILL_OK or KILL_FAIL <why>: a shell kills the child it started.
kills_child() {
  # The script is the container's, so its $p is meant for its shell.
  # shellcheck disable=SC2086,SC2016
  $T $PODMAN --events-backend=none run --rm --log-driver=none --network=none "$@" "$IMG" sh -c '
    sleep 30 & p=$!
    if kill "$p" 2>/tmp/e; then echo KILL_OK; else echo "KILL_FAIL $(cat /tmp/e)"; fi' 2>&1 | tail -1
}

# Prints the system state and a stopped unit's state: systemd as PID 1 boots
# and stops a unit, which it does by signalling the unit's process.
systemd_stops() {
  _c=sdlc-apparmor-$$
  # shellcheck disable=SC2086
  $T $PODMAN --events-backend=none run -d --rm --name "$_c" --systemd=always \
    --network=none "$@" "$SDIMG" >/dev/null 2>"/tmp/sdlc-apparmor-$$.err" \
    || { echo "no-start: $(tail -1 "/tmp/sdlc-apparmor-$$.err")"; rm -f "/tmp/sdlc-apparmor-$$.err"; return; }
  rm -f "/tmp/sdlc-apparmor-$$.err"
  _st=
  for _ in $(seq 1 30); do
    # shellcheck disable=SC2086
    _st=$($T $PODMAN exec "$_c" systemctl is-system-running 2>/dev/null) || :
    case "$_st" in running|degraded) break ;; esac
    sleep 1
  done
  # shellcheck disable=SC2086
  $T $PODMAN exec "$_c" systemd-run --no-block --unit=sdlc-probe sleep 300 >/dev/null 2>&1
  sleep 2
  # shellcheck disable=SC2086
  $T $PODMAN exec "$_c" systemctl stop --no-block sdlc-probe >/dev/null 2>&1
  _after=
  for _ in $(seq 1 15); do
    # shellcheck disable=SC2086
    _after=$($T $PODMAN exec "$_c" systemctl show -p ActiveState --value sdlc-probe 2>/dev/null)
    [ "$_after" = inactive ] && break
    sleep 1
  done
  # shellcheck disable=SC2086
  _res=$($T $PODMAN exec "$_c" systemctl show -p Result --value sdlc-probe 2>/dev/null)
  # shellcheck disable=SC2086
  $T $PODMAN rm -f -t 0 "$_c" >/dev/null 2>&1 || :
  echo "$_st $_after (result ${_res:-unknown})"
}

# One podman, by the label it is reported under.
check() {
  _who=$1
  # shellcheck disable=SC2086
  $T $PODMAN pull -q "$IMG" >/dev/null 2>&1 || cannot "$_who podman could not pull $IMG"
  # shellcheck disable=SC2086
  _aa=$($T $PODMAN info --format '{{.Host.Security.AppArmorEnabled}}' 2>/dev/null)
  # bin/sdlc's rule, run against this podman: its `podman` is the one checked.
  # `command` reaches the real binary, never this function; the rule calls it
  # through eval, and sudo below runs the binary, not the function.
  # shellcheck disable=SC2086,SC2032,SC2329
  podman() { command $PODMAN "$@"; }
  NNP=
  eval "$NNP_RULE"
  unset -f podman
  _r=$(kills_child --cap-drop=all ${NNP:+"$NNP"})
  case "$_r" in
    KILL_OK) ok "$_who (AppArmor=${_aa:-unknown}): a gate container kills its own child" ;;
    *)       bad "$_who (AppArmor=${_aa:-unknown}): a gate container could not kill its own child: $_r" ;;
  esac
  [ "$_aa" = true ] || return 0
  # shellcheck disable=SC2086
  $T $PODMAN pull -q "$SDIMG" >/dev/null 2>&1 || cannot "$_who podman could not pull $SDIMG"
  _r=$(systemd_stops --cap-drop=all --cap-add="$SYSTEMD_CAPS")
  case "$_r" in
    "running inactive "*|"degraded inactive "*) ok "$_who: systemd boots ($_r) and stops a unit" ;;
    *) bad "$_who: systemd as PID 1 did not boot and stop a unit: $_r" ;;
  esac
  _r=$(kills_child --cap-drop=all --security-opt=no-new-privileges)
  case "$_r" in
    KILL_OK) printf 'note %s\n' "$_who: no-new-privileges no longer blocks the kill; #181's workaround can go" ;;
    *)       printf 'note %s\n' "$_who: with no-new-privileges the kill still fails ($_r), so dropping it is still needed" ;;
  esac
}

PODMAN=podman
check rootless
# shellcheck disable=SC2033
if sudo -n podman version >/dev/null 2>&1; then
  PODMAN="sudo -n podman"
  check rootful
else
  printf 'note rootful: no passwordless sudo here, only rootless podman checked\n'
fi

printf '\n'
[ "$FAILED" -eq 0 ] || { printf '%s check(s) failed\n' "$FAILED"; exit 1; }
printf 'all passed\n'
