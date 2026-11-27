#!/usr/bin/env bash
# a1796 proof bundle for Dolza. Usage: run.sh <path-to-sdlc-checkout> [workdir]
# See README.md. Final line on success: DOLZA PROOF PASSED
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SDLC=${1:?usage: run.sh <sdlc checkout at feat/a1796-release-assets> [workdir]}
SDLC=$(cd "$SDLC" && pwd)
# A short path: the podman socket path must fit sun_path (108 bytes).
WORK=${2:-$(mktemp -d /tmp/a1796.XXXXXX)}
mkdir -p "$WORK"; WORK=$(cd "$WORK" && pwd)
IMAGE=localhost/sdlc:a1796
NET=a1796net
SITE_CTR=a1796-site
SOCK=$WORK/podman.sock
PY=/opt/constitution-venv/bin/python3
SVC_PID=""

hdr() { printf '\n########## %s ##########\n' "$*"; }
die() { printf 'FATAL: %s\n' "$*" >&2; exit 1; }

cleanup() {
  rc=$?
  podman rm -f "$SITE_CTR" >/dev/null 2>&1 || true
  podman network rm -f "$NET" >/dev/null 2>&1 || true
  if [ -n "$SVC_PID" ]; then kill "$SVC_PID" 2>/dev/null || true; fi
  [ "$rc" -eq 0 ] || printf '\nDOLZA PROOF FAILED (rc=%s); work dir kept: %s\n' "$rc" "$WORK" >&2
  exit "$rc"
}
trap cleanup EXIT

for t in podman git python3 bash curl; do command -v "$t" >/dev/null || die "$t not found"; done
[ -f "$SDLC/sdlc_jobs/assets.py" ] || die "$SDLC has no sdlc_jobs/assets.py: wrong branch?"
printf 'sdlc: %s @ %s\nwork: %s\n' "$SDLC" "$(git -C "$SDLC" rev-parse --short HEAD 2>/dev/null || echo '?')" "$WORK"

# ---------------------------------------------------------------- 1
hdr "1. build the sdlc image and run its contract test"
podman build -t "$IMAGE" "$SDLC"
# contract.sh calls tests/test_apparmor.sh by relative path: run it from the checkout.
( cd "$SDLC" && tests/contract.sh "$IMAGE" ) 2>&1 | tee "$WORK/contract.log"
grep -q 'contract test PASSED' "$WORK/contract.log" || die "contract test did not pass"
for probe in gpg rpmsign createrepo_c rpmkeys; do
  grep -q "^ok   $probe executes" "$WORK/contract.log" || die "contract.sh did not report '$probe executes'"
done
echo "gnupg2, rpm-sign, createrepo_c (and rpmkeys) install and execute on the Hummingbird base"

# ---------------------------------------------------------------- 2
hdr "2. podman API socket for the job's CONTAINER_HOST"
rm -f "$SOCK"
podman system service --time=0 "unix://$SOCK" >"$WORK/service.log" 2>&1 &
SVC_PID=$!
for _ in $(seq 1 30); do [ -S "$SOCK" ] && break; sleep 1; done
[ -S "$SOCK" ] || die "podman socket never appeared (see $WORK/service.log)"
echo "socket: $SOCK"

# ---------------------------------------------------------------- 3
hdr "3. clone ashigaru and petit, add the adoption's [[tool.sdlc.assets]]"
for r in ashigaru petit; do
  rm -rf "${WORK:?}/$r"
  git clone --quiet "https://github.com/crunchtools/$r" "$WORK/$r"
  git -C "$WORK/$r" log -1 --format="$r main: %h %s"
done
{
  echo
  for d in fc43 fc45 el10; do
    printf '[[tool.sdlc.assets]]\ncheck = "rpm-%s"\ntarget = "build"\npath = "/out"\nfiles = ["*.rpm"]\n\n' "$d"
  done
} >> "$WORK/ashigaru/pyproject.toml"
# petit: packaging/Containerfile's final stage `packages` is FROM scratch and holds
# the .rpm and .deb at /. pkg-ubi8 is the one check the design names.
{
  printf '\n[[tool.sdlc.assets]]\ncheck = "pkg-ubi8"\ntarget = "packages"\npath = "/"\nfiles = ["*.rpm", "*.deb"]\n'
} >> "$WORK/petit/pyproject.toml"
grep -q '^\[tool.sdlc.ci.rpm-fc43\]' "$WORK/ashigaru/pyproject.toml" || die "ashigaru has no rpm-fc43 check: tree changed, see README TODO"
grep -q '^\[tool.sdlc.ci.pkg-ubi8\]' "$WORK/petit/pyproject.toml" || die "petit has no pkg-ubi8 check: tree changed, see README TODO"
grep -q 'AS build$' "$WORK/ashigaru/packaging/rpm.Containerfile" || die "ashigaru stage 'build' is gone: see README TODO"
grep -q 'AS packages$' "$WORK/petit/packaging/Containerfile" || die "petit stage 'packages' is gone: see README TODO"
for r in ashigaru petit; do
  git -C "$WORK/$r" -c user.name=a1796-proof -c user.email=proof@example.invalid \
    commit -qam "proof: adopt [[tool.sdlc.assets]]"
done

# ---------------------------------------------------------------- 4a
hdr "4a. assets phase, inside the sdlc image (real build through the socket)"
cp "$HERE/drive.py" "$WORK/drive.py"
inimage() { # inimage <extra podman args...> -- <drive.py args...>
  local extra=()
  while [ "$1" != "--" ]; do extra+=("$1"); shift; done; shift
  podman run --rm --security-opt label=disable \
    -v "$WORK:$WORK" -w "$WORK" ${extra[@]+"${extra[@]}"} \
    --entrypoint "$PY" "$IMAGE" "$WORK/drive.py" "$@"
}
# $WORK is mounted, so the socket and both clones are at the same path inside.
inimage -- assets "$WORK" "unix://$SOCK" 2>&1 | tee "$WORK/assets.log"
[ "${PIPESTATUS[0]}" -eq 0 ] || die "assets phase failed (see $WORK/assets.log)"
ls "$WORK/pool/ashigaru" "$WORK/pool/petit"

# ---------------------------------------------------------------- 4b
hdr "4b. pages phase, inside the sdlc image, no network, no socket"
inimage --network=none -- pages "$WORK" 2>&1 | tee "$WORK/pages.log"
[ "${PIPESTATUS[0]}" -eq 0 ] || die "pages phase failed (see $WORK/pages.log)"
[ -d "$WORK/site/rpm" ] && [ -d "$WORK/site/deb" ] || die "no site tree"

# ---------------------------------------------------------------- 5
hdr "5. serve the site; install from it on 13 targets with signature checking ON"
podman network exists "$NET" || podman network create "$NET" >/dev/null
podman rm -f "$SITE_CTR" >/dev/null 2>&1 || true
podman run -d --name "$SITE_CTR" --network "$NET" --network-alias site \
  --security-opt label=disable -v "$WORK/site:/srv:ro" \
  docker.io/library/python:3-slim python3 -m http.server 8080 --directory /srv >/dev/null
for _ in $(seq 1 30); do
  podman run --rm --network "$NET" docker.io/library/python:3-slim \
    python3 -c 'import urllib.request as u; u.urlopen("http://site:8080/crunchtools.asc")' \
    >/dev/null 2>&1 && break
  sleep 1
done
podman run --rm --network "$NET" docker.io/library/python:3-slim \
  python3 -c 'import urllib.request as u; print(len(u.urlopen("http://site:8080/rpm/crunchtools.repo").read()), "bytes of crunchtools.repo served")'

mkdir -p "$WORK/proof" "$WORK/logs"
cat > "$WORK/proof/install.sh" <<'INSTALL'
#!/bin/sh
# Inside a distro container on a1796net. $1 = dnf|zypper|apt. Signature checking ON:
# gpgcheck=1 and repo_gpgcheck=1 (rpm), Signed-By (apt). No --nogpgcheck, no
# --allow-unsigned, no trusted=yes. zypper's --gpg-auto-import-keys is what
# publish.yml uses: it imports the repo's gpgkey and still verifies against it.
set -eu
kind=$1
S=http://site:8080
write_repo() {
  printf '[crunchtools]\nname=crunchtools\nbaseurl=%s/rpm\nenabled=1\ngpgcheck=1\nrepo_gpgcheck=1\ngpgkey=%s/crunchtools.asc\n' "$S" "$S" > "$1"
}
case "$kind" in
dnf)
  write_repo /etc/yum.repos.d/crunchtools.repo
  dnf install -y petit
  ;;
zypper)
  write_repo /tmp/crunchtools.repo
  zypper --non-interactive --gpg-auto-import-keys addrepo /tmp/crunchtools.repo
  zypper --non-interactive --gpg-auto-import-keys refresh
  zypper --non-interactive --gpg-auto-import-keys install petit
  ;;
apt)
  apt-get update
  apt-get install -y ca-certificates curl
  install -d /etc/apt/keyrings
  curl -fsSL -o /etc/apt/keyrings/crunchtools.asc "$S/crunchtools.asc"
  printf 'Types: deb\nURIs: %s/deb/\nSuites: ./\nSigned-By: /etc/apt/keyrings/crunchtools.asc\n' "$S" \
    > /etc/apt/sources.list.d/crunchtools.sources
  apt-get update
  apt-get install -y petit
  apt-cache policy petit | grep -q 'site:8080' || { echo "petit did not come from the site"; exit 1; }
  ;;
*) echo "bad kind $kind"; exit 2 ;;
esac
case "$kind" in
dnf|zypper)
  sig=$(rpm -qi petit | grep -E '^Signature' || true)
  echo "installed petit: $sig"
  case "$sig" in ''|*'(none)'*) echo "the installed petit carries no signature"; exit 1 ;; esac
  ;;
esac
petit --version
printf '%s\n' \
  'Sep 23 10:00:01 host sshd[101]: Accepted publickey for alice from 10.0.0.1 port 5001 ssh2' \
  'Sep 23 10:00:02 host sshd[102]: Accepted publickey for bob from 10.0.0.2 port 5002 ssh2' \
  'Sep 23 10:00:03 host CRON[103]: (root) CMD (run-parts /etc/cron.hourly)' > /tmp/sample.log
petit --hash /tmp/sample.log | tee /tmp/hash.out
grep -q 'Accepted publickey' /tmp/hash.out
echo "TARGET-OK"
INSTALL

cat > "$WORK/proof/ashigaru.sh" <<'ASH'
#!/bin/sh
# fedora:43. $1 = the site's fc43 noarch ashigaru file name (no .rpm). The fc45 and el10
# builds sit in the same repo with the same name and version, so the fc43 one is asked
# for by its full name-version-release.
set -eu
nevra=$1
S=http://site:8080
printf '[crunchtools]\nname=crunchtools\nbaseurl=%s/rpm\nenabled=1\ngpgcheck=1\nrepo_gpgcheck=1\ngpgkey=%s/crunchtools.asc\n' "$S" "$S" > /etc/yum.repos.d/crunchtools.repo
if dnf install -y "$nevra"; then
  rpm -qi ashigaru | grep -E '^(Name|Version|Release|Signature)'
  sig=$(rpm -qi ashigaru | grep -E '^Signature' || true)
  case "$sig" in ''|*'(none)'*) echo "installed ashigaru carries no signature"; exit 1 ;; esac
  ashigaru --help >/dev/null 2>&1 && echo "ashigaru --help runs" || echo "NOTE: ashigaru --help exited non-zero (informational)"
  echo "ASHIGARU-INSTALLED"
else
  echo "NOTE: dnf install of $nevra failed (dependencies?). Falling back to signature check only."
  dnf install -y --setopt=install_weak_deps=False curl >/dev/null 2>&1 || true
  curl -fsSL -o /tmp/a.rpm "$S/rpm/$nevra.rpm"
  rpmkeys --import "$S/crunchtools.asc"
  out=$(rpmkeys --checksig /tmp/a.rpm); echo "$out"
  case "$out" in *'signatures OK'*) echo "ASHIGARU-SIGONLY" ;; *) exit 1 ;; esac
fi
ASH

fail=0
declare -a ROWS=()
# publish.yml's smoke matrix, 13 targets (claude-agent is not built here).
# Names are fully qualified so podman never prompts for a registry.
run_full() { # run_full <fully qualified image> <kind>
  local image=$1 kind=$2 name log
  name=$(printf '%s' "$image" | tr '/:' '__'); log="$WORK/logs/$name.log"
  printf '  -> %-52s %-6s ' "$image" "$kind"
  if timeout 1200 podman run --rm --network "$NET" --security-opt label=disable \
       -v "$WORK/proof:/proof:ro" "$image" sh /proof/install.sh "$kind" >"$log" 2>&1 \
     && grep -q '^TARGET-OK$' "$log"; then
    echo PASS; ROWS+=("PASS  $image ($kind)")
  else
    echo FAIL; ROWS+=("FAIL  $image ($kind)  log: $log"); fail=$((fail + 1))
    tail -n 25 "$log" | sed 's/^/        | /'
  fi
}
run_full registry.access.redhat.com/ubi8/ubi dnf
run_full registry.access.redhat.com/ubi9/ubi dnf
run_full registry.access.redhat.com/ubi10/ubi dnf
run_full docker.io/library/almalinux:9 dnf
run_full quay.io/centos/centos:stream10 dnf
run_full docker.io/library/fedora:latest dnf
run_full docker.io/library/amazonlinux:2023 dnf
run_full registry.suse.com/bci/bci-base:15.7 zypper
run_full registry.suse.com/bci/bci-base:16.0 zypper
run_full docker.io/library/debian:12 apt
run_full docker.io/library/debian:13 apt
run_full docker.io/library/ubuntu:24.04 apt
run_full docker.io/library/ubuntu:26.04 apt

hdr "5b. ashigaru's fc43 RPM from the same repo on fedora:43"
A_FILE=$(cd "$WORK/site/rpm" && ls ashigaru-*.fc43.noarch.rpm | grep -v '^ashigaru-desk' | head -n1 || true)
[ -n "$A_FILE" ] || die "no ashigaru fc43 noarch RPM on the site"
A_NEVRA=${A_FILE%.noarch.rpm}.noarch
log="$WORK/logs/ashigaru-fedora43.log"
printf '  -> fedora:43 %s ' "$A_NEVRA"
if timeout 1200 podman run --rm --network "$NET" --security-opt label=disable \
     -v "$WORK/proof:/proof:ro" docker.io/library/fedora:43 sh /proof/ashigaru.sh "$A_NEVRA" >"$log" 2>&1; then
  if grep -q '^ASHIGARU-INSTALLED$' "$log"; then
    echo PASS; ROWS+=("PASS  fedora:43 ashigaru dnf install (signature checked)")
  elif grep -q '^ASHIGARU-SIGONLY$' "$log"; then
    echo "PASS (signature only)"
    ROWS+=("PASS* fedora:43 ashigaru: rpmkeys --checksig only; dnf install failed, see $log")
  else
    echo FAIL; ROWS+=("FAIL  fedora:43 ashigaru  log: $log"); fail=$((fail + 1))
  fi
else
  echo FAIL; ROWS+=("FAIL  fedora:43 ashigaru  log: $log"); fail=$((fail + 1))
  tail -n 25 "$log" | sed 's/^/        | /'
fi

hdr "summary"
printf '%s\n' "${ROWS[@]}"
if [ -s "$WORK/findings.txt" ]; then
  printf '\nFINDINGS to carry back (from drive.py):\n'; sed 's/^/  - /' "$WORK/findings.txt"
fi
[ "$fail" -eq 0 ] || die "$fail install target(s) failed; logs in $WORK/logs"
echo
echo "DOLZA PROOF PASSED"
