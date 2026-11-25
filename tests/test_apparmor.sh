#!/bin/sh
# PROBE (a1379, temporary): what lets a gate container kill its own child on an
# AppArmor host. Prints evidence for each candidate setting; never fails.
set -u
IMG=docker.io/library/alpine:3.22@sha256:9db2934886712d877c932e07249c75a59e6ca27adfbccc05261987a17bef2d63
say() { printf 'apparmor-probe: %s\n' "$*"; }

say "== host"
uname -a
grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release 2>/dev/null
podman version --format '{{.Client.Version}}' 2>&1 | sed 's/^/podman /'
crun --version 2>&1 | head -2
command -v runc >/dev/null && runc --version 2>&1 | head -1
say "rootless=$(podman info --format '{{.Host.Security.Rootless}}' 2>&1) apparmor=$(podman info --format '{{.Host.Security.AppArmorEnabled}}' 2>&1) runtime=$(podman info --format '{{.Host.OCIRuntime.Name}} {{.Host.OCIRuntime.Path}}' 2>&1)"
say "host label: $(cat /proc/self/attr/current 2>&1) / $(cat /proc/self/attr/apparmor/current 2>&1)"
ls /etc/apparmor.d 2>/dev/null | grep -i -E 'crun|runc|podman|contain|buildah|conmon' | sed 's/^/apparmor.d: /'
for f in /etc/apparmor.d/crun /etc/apparmor.d/podman /etc/apparmor.d/runc; do
  [ -r "$f" ] && { say "--- $f"; grep -v '^\s*#' "$f" | grep -v '^\s*$'; }
done
if sudo -n true 2>/dev/null; then
  sudo -n cat /sys/kernel/security/apparmor/profiles 2>/dev/null \
    | grep -i -E 'crun|runc|podman|contain|conmon' | sed 's/^/loaded: /'
fi

if [ "$(podman info --format '{{.Host.Security.AppArmorEnabled}}' 2>/dev/null)" != true ]; then
  say "no AppArmor here; candidates still run for comparison"
fi
podman pull -q "$IMG" >/dev/null 2>&1 || say "pull failed"

probe() {
  _name=$1; shift
  _out=$(podman --events-backend=none run --rm --log-driver=none "$@" "$IMG" sh -c '
    printf "label=%s " "$(cat /proc/self/attr/current 2>/dev/null)"
    printf "nnp=%s " "$(grep NoNewPrivs /proc/self/status | tr -dc 0-9)"
    sleep 30 & p=$!
    if kill "$p" 2>/tmp/e; then echo KILL_OK; else echo "KILL_FAIL $(cat /tmp/e)"; fi
    wait "$p" 2>/dev/null; :' 2>&1)
  say "[$_name] $_out"
}
BASE="--cap-drop=all --security-opt=no-new-privileges"
# shellcheck disable=SC2086
{
probe "base (cap-drop, nnp)"            $BASE
probe "base + apparmor=unconfined"      $BASE --security-opt apparmor=unconfined
probe "cap-drop only (no nnp)"          --cap-drop=all
probe "nothing"
probe "base + label=disable"            $BASE --security-opt label=disable
command -v runc >/dev/null && probe "base + runtime runc" $BASE --runtime runc
probe "base + systemd-ish caps"         --cap-drop=all --cap-add=KILL --security-opt=no-new-privileges
}
if sudo -n true 2>/dev/null; then
  sudo -n dmesg 2>/dev/null | grep -i 'apparmor="DENIED"' | tail -8 | sed 's/^/dmesg: /'
  sudo -n journalctl -k --no-pager 2>/dev/null | grep -i 'apparmor="DENIED"' | tail -8 | sed 's/^/kern: /'
fi
say "PROBE DONE (failing on purpose so the log can be read)"; exit 1
