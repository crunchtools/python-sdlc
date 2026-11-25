#!/bin/sh
# bin/sdlc-bootstrap against a fake podman: nothing from the image runs before
# a trusted key's signature on its digest is verified (sdlc's own for a
# release, sdlc's or the fleet key for main or a digest), and everything after
# runs by that digest (#81). The real cosign and quay.io are what
# container.yml's promote step proves; this pins the order and the exits.
#
# Usage: tests/test_bootstrap.sh

set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
BOOT=$PWD/bin/sdlc-bootstrap
FAILED=0
ok()  { printf 'ok   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }

printf '== bin/sdlc-bootstrap\n\n'

W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
mkdir "$W/bin"
D=sha256:$(printf 'a%.0s' $(seq 64))
FLEET=quay.io/crunchtools/sdlc

# The fake podman. Its behaviour is files in $W: `pull-fails`, `absent`,
# `unsigned` (by either key), `unsigned-sdlc` and `unsigned-fleet` (by that
# key), and `digest` (what inspect answers). The launcher it prints
# records the environment and arguments it ran with.
cat > "$W/bin/podman" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >> "$W/log"
case " \$* " in
  *" pull "*)         [ ! -e "$W/pull-fails" ] ;;
  *" image exists "*) [ ! -e "$W/absent" ] ;;
  *" image inspect "*)
    [ ! -e "$W/inspect-fails" ] || { echo 'Error: inspecting object: boom' >&2; exit 125; }
    cat "$W/digest" ;;
  *" verify "*)
    [ ! -e "$W/unsigned" ] || { echo 'Error: no signatures found' >&2; exit 10; }
    case " \$* " in *" --key /sdlc.pub "*) _k=sdlc ;; *) _k=fleet ;; esac
    [ ! -e "$W/unsigned-\$_k" ] || { echo "Error: no \$_k signature found" >&2; exit 10; } ;;
  *" launcher "*)
    printf 'printf "%%s|%%s|%%s\\\\n" "\$SDLC_IMAGE" "\$SDLC_VERIFIED_FROM" "\$*" > "$W/ran"\n' ;;
  *) exit 125 ;;
esac
EOF
chmod +x "$W/bin/podman"

reset() {
  rm -rf "$W/log" "$W/ran" "$W/cache" "$W/pull-fails" "$W/absent" "$W/unsigned" "$W/inspect-fails" \
    "$W/unsigned-sdlc" "$W/unsigned-fleet"
  printf '%s\n' "$D" > "$W/digest"
}
boot() {             # args...: run the bootstrap, set RC and OUT
  RC=0
  OUT=$(env -u CI -u GITHUB_ACTIONS PATH="$W/bin:$PATH" XDG_CACHE_HOME="$W/cache" \
          sh "$BOOT" "$@" 2>&1) || RC=$?
}
SDLC_ID=$(sed -n 's/^SDLC_KEY_ID=//p' "$BOOT")
FLEET_ID=$(sed -n 's/^FLEET_KEY_ID=//p' "$BOOT")
verifies() { grep ' verify ' "$W/log" | sed 's/.* --key \/\([a-z]*\)\.pub .*/\1/' | tr '\n' ' '; }

reset
boot versions
if [ "$RC" -eq 0 ] && [ "$(cat "$W/ran" 2>/dev/null)" = "$FLEET@$D|$FLEET:latest|versions" ]; then
  ok "a signed :latest runs its launcher by digest, told the name it came from"
else
  bad "signed :latest: exit $RC, ran '$(cat "$W/ran" 2>/dev/null)'"; printf '%s\n' "$OUT"
fi
if grep -q -- "verify --key /sdlc.pub --insecure-ignore-tlog=true $FLEET@$D\$" "$W/log" \
   && [ "$(verifies)" = "sdlc " ] \
   && grep -q -- "--pull=never -- $FLEET@$D launcher $FLEET@$D\$" "$W/log"; then
  ok "cosign verifies the digest with sdlc's key alone, and the launcher comes from that digest, never pulled"
else
  bad "the verify or launcher argv is not the digest"; cat "$W/log"
fi
verify_at=$(grep -n ' verify ' "$W/log" | cut -d: -f1)
launch_at=$(grep -n ' launcher ' "$W/log" | cut -d: -f1)
if [ -n "$verify_at" ] && [ -n "$launch_at" ] && [ "$verify_at" -lt "$launch_at" ]; then
  ok "the signature is verified before anything from the image runs"
else
  bad "verify at line '$verify_at', launcher at line '$launch_at'"
fi
if [ -e "$W/cache/sdlc/verified/$SDLC_ID/${D#sha256:}" ] && [ ! -d "$W/cache/sdlc/verified/$FLEET_ID" ]; then
  ok "the verified digest is remembered under sdlc's key"
else
  bad "no cache marker for the verified digest"
fi

rm -f "$W/log" "$W/ran"
touch "$W/pull-fails"
boot versions
if [ "$RC" -eq 0 ] && ! grep -q ' verify ' "$W/log" && [ -e "$W/ran" ]; then
  ok "offline, a digest verified before runs without asking the registry again"
else
  bad "offline rerun of a verified digest: exit $RC"; cat "$W/log"
fi

# CI remembers nothing: a self-hosted runner outlives the jobs it runs.
reset
for _ in 1 2; do
  RC=0
  OUT=$(PATH="$W/bin:$PATH" XDG_CACHE_HOME="$W/cache" CI=true sh "$BOOT" versions 2>&1) || RC=$?
done
if [ "$RC" -eq 0 ] && [ "$(grep -c ' verify ' "$W/log")" -eq 2 ] && [ ! -d "$W/cache/sdlc" ]; then
  ok "in CI every run verifies, and nothing is remembered"
else
  bad "CI: exit $RC, $(grep -c ' verify ' "$W/log") verify call(s)"; cat "$W/log"
fi

reset
touch "$W/unsigned"
boot versions
if [ "$RC" -eq 2 ] && [ ! -e "$W/ran" ] && ! grep -q ' launcher ' "$W/log" \
   && printf '%s' "$OUT" | grep -q "GATE CANNOT RUN: .* could not be verified as signed by sdlc's key\$" \
   && printf '%s' "$OUT" | grep -q 'no signatures found' \
   && [ ! -d "$W/cache/sdlc/verified" ]; then
  ok "an unsigned image is a 2, says why, runs nothing and is not remembered"
else
  bad "unsigned: exit $RC, ran '$(cat "$W/ran" 2>/dev/null)'"; printf '%s\n' "$OUT"
fi

# Which key vouches: a release only by sdlc's, main and a digest by either.
reset
touch "$W/unsigned-sdlc"
boot versions
if [ "$RC" -eq 2 ] && [ ! -e "$W/ran" ] && [ "$(verifies)" = "sdlc " ] \
   && printf '%s' "$OUT" | grep -q "could not be verified as signed by sdlc's key\$" \
   && [ ! -d "$W/cache/sdlc/verified" ]; then
  ok ":latest signed by the fleet key alone is a 2, and the fleet key is never asked"
else
  bad "fleet-signed :latest: exit $RC, verified with '$(verifies)'"; printf '%s\n' "$OUT"
fi

for v in 0.15.7 0.15.1; do
  reset
  [ "$v" = 0.15.7 ] || touch "$W/unsigned-sdlc"
  RC=0
  OUT=$(env -u CI -u GITHUB_ACTIONS PATH="$W/bin:$PATH" XDG_CACHE_HOME="$W/cache" \
          SDLC_IMAGE=$v sh "$BOOT" versions 2>&1) || RC=$?
  case $v:$RC in
    0.15.7:0|0.15.1:2)
      if [ "$(verifies)" = "sdlc " ]; then ok "SDLC_IMAGE=$v is a release: sdlc's key alone (exit $RC)"
      else bad "SDLC_IMAGE=$v verified with '$(verifies)'"; fi ;;
    *) bad "SDLC_IMAGE=$v: exit $RC"; printf '%s\n' "$OUT" ;;
  esac
done

reset
touch "$W/unsigned-sdlc"
RC=0
OUT=$(env -u CI -u GITHUB_ACTIONS PATH="$W/bin:$PATH" XDG_CACHE_HOME="$W/cache" \
        SDLC_IMAGE=main sh "$BOOT" versions 2>&1) || RC=$?
if [ "$RC" -eq 0 ] && [ "$(verifies)" = "sdlc fleet " ] \
   && [ "$(cat "$W/ran" 2>/dev/null)" = "$FLEET@$D|$FLEET:main|versions" ] \
   && [ -e "$W/cache/sdlc/verified/$FLEET_ID/${D#sha256:}" ] && [ ! -d "$W/cache/sdlc/verified/$SDLC_ID" ]; then
  ok "SDLC_IMAGE=main signed by the fleet key runs, after sdlc's key, remembered under the fleet key"
else
  bad "fleet-signed main: exit $RC, verified with '$(verifies)'"; printf '%s\n' "$OUT"
fi

# A digest the fleet key vouched for, remembered, vouches for main only.
rm -f "$W/log" "$W/ran"
boot versions
if [ "$RC" -eq 2 ] && [ "$(verifies)" = "sdlc " ] && [ ! -e "$W/ran" ]; then
  ok "a digest remembered under the fleet key is not a release: :latest asks sdlc's key again"
else
  bad ":latest after a fleet-key marker: exit $RC, verified with '$(verifies)'"; printf '%s\n' "$OUT"
fi

reset
RC=0
OUT=$(env -u CI -u GITHUB_ACTIONS PATH="$W/bin:$PATH" XDG_CACHE_HOME="$W/cache" \
        SDLC_IMAGE=main sh "$BOOT" versions 2>&1) || RC=$?
if [ "$RC" -eq 0 ] && [ "$(verifies)" = "sdlc " ]; then
  ok "SDLC_IMAGE=main signed by sdlc's key never asks the fleet key"
else
  bad "sdlc-signed main: exit $RC, verified with '$(verifies)'"
fi

reset
touch "$W/unsigned-sdlc" "$W/unsigned-fleet"
RC=0
OUT=$(env -u CI -u GITHUB_ACTIONS PATH="$W/bin:$PATH" XDG_CACHE_HOME="$W/cache" \
        SDLC_IMAGE=main sh "$BOOT" versions 2>&1) || RC=$?
if [ "$RC" -eq 2 ] && [ ! -e "$W/ran" ] && [ "$(verifies)" = "sdlc fleet " ] \
   && printf '%s' "$OUT" | grep -q "signed by sdlc's key or the crunchtools fleet key\$" \
   && printf '%s' "$OUT" | grep -q 'no sdlc signature found' \
   && printf '%s' "$OUT" | grep -q 'no fleet signature found' \
   && [ ! -d "$W/cache/sdlc/verified" ]; then
  ok "main signed by neither key is a 2 with both keys' refusals, runs nothing, remembers nothing"
else
  bad "unsigned main: exit $RC, verified with '$(verifies)'"; printf '%s\n' "$OUT"
fi

reset
touch "$W/pull-fails" "$W/absent"
boot versions
if [ "$RC" -eq 2 ] && printf '%s' "$OUT" | grep -q 'could not be pulled'; then
  ok "an image that cannot be pulled and is not here is a 2"
else
  bad "unpullable: exit $RC"; printf '%s\n' "$OUT"
fi

reset
touch "$W/pull-fails"
boot versions
if [ "$RC" -eq 0 ] && grep -q ' verify ' "$W/log"; then
  ok "offline, the local copy of a tag is verified and run"
else
  bad "offline local copy: exit $RC"; cat "$W/log"
fi

for d in '' '<none>' 'sha256:abc' "sha256:$(printf 'G%.0s' $(seq 64))"; do
  reset
  printf '%s\n' "$d" > "$W/digest"
  boot versions
  if [ "$RC" -eq 2 ] && ! grep -q ' verify \| launcher ' "$W/log"; then
    ok "inspect answering '$d' is a 2 before anything runs"
  else
    bad "inspect answering '$d': exit $RC"; cat "$W/log"
  fi
done

reset
touch "$W/inspect-fails"
boot versions
if [ "$RC" -eq 2 ] && printf '%s' "$OUT" | grep -q 'could not be inspected: Error: inspecting object: boom' \
   && ! printf '%s' "$OUT" | grep -q 'no sha256 digest' && ! grep -q ' verify \| launcher ' "$W/log"; then
  ok "a failed inspect reports its own error, not a missing digest"
else
  bad "failed inspect: exit $RC"; printf '%s\n' "$OUT"
fi

reset
E=sha256:$(printf 'b%.0s' $(seq 64))
printf 'unused\n' > "$W/digest"
RC=0
OUT=$(env -u CI -u GITHUB_ACTIONS PATH="$W/bin:$PATH" XDG_CACHE_HOME="$W/cache" \
        SDLC_IMAGE="@$E" sh "$BOOT" versions 2>&1) || RC=$?
if [ "$RC" -eq 0 ] && ! grep -q ' pull \| inspect ' "$W/log" \
   && grep -q -- "verify .* $FLEET@$E\$" "$W/log"; then
  ok "SDLC_IMAGE=@sha256 verifies that digest, with no pull when it is here"
else
  bad "digest ref: exit $RC"; cat "$W/log"
fi
reset
touch "$W/unsigned-sdlc"
RC=0
OUT=$(env -u CI -u GITHUB_ACTIONS PATH="$W/bin:$PATH" XDG_CACHE_HOME="$W/cache" \
        SDLC_IMAGE="@$E" sh "$BOOT" versions 2>&1) || RC=$?
if [ "$RC" -eq 0 ] && [ "$(verifies)" = "sdlc fleet " ] && [ -e "$W/ran" ]; then
  ok "SDLC_IMAGE=@sha256 signed by the fleet key alone runs (a release up to 0.15.1)"
else
  bad "fleet-signed digest: exit $RC, verified with '$(verifies)'"; printf '%s\n' "$OUT"
fi

# A digest that is not here yet is pulled, then verified like any other; one
# that cannot be pulled is a 2 before anything is verified or run.
reset
touch "$W/absent"
RC=0
OUT=$(env -u CI -u GITHUB_ACTIONS PATH="$W/bin:$PATH" XDG_CACHE_HOME="$W/cache" \
        SDLC_IMAGE="@$E" sh "$BOOT" versions 2>&1) || RC=$?
pull_at=$(grep -n " pull -q -- $FLEET@$E\$" "$W/log" | cut -d: -f1)
verify_at=$(grep -n " verify .* $FLEET@$E\$" "$W/log" | cut -d: -f1)
if [ "$RC" -eq 0 ] && [ -n "$pull_at" ] && [ -n "$verify_at" ] && [ "$pull_at" -lt "$verify_at" ] \
   && [ -e "$W/ran" ]; then
  ok "SDLC_IMAGE=@sha256 that is not here is pulled, then verified, then run"
else
  bad "absent digest: exit $RC"; cat "$W/log"
fi
reset
touch "$W/absent" "$W/pull-fails"
RC=0
OUT=$(env -u CI -u GITHUB_ACTIONS PATH="$W/bin:$PATH" XDG_CACHE_HOME="$W/cache" \
        SDLC_IMAGE="@$E" sh "$BOOT" versions 2>&1) || RC=$?
if [ "$RC" -eq 2 ] && printf '%s' "$OUT" | grep -q 'could not be pulled' \
   && ! grep -q ' verify \| launcher ' "$W/log"; then
  ok "SDLC_IMAGE=@sha256 that cannot be pulled is a 2, nothing verified or run"
else
  bad "unpullable digest: exit $RC"; cat "$W/log"
fi

reset
RC=0
OUT=$(PATH="$W/bin:$PATH" SDLC_IMAGE=docker.io/evil/sdlc:latest sh "$BOOT" versions 2>&1) || RC=$?
if [ "$RC" -eq 2 ] && [ ! -e "$W/log" ]; then
  ok "an image from another repository is refused before podman is asked"
else
  bad "foreign SDLC_IMAGE: exit $RC"
fi

# Each key ID is its inlined key's sha256, so a new key cannot reuse old
# markers, and the sdlc key inlined is boot/keys/sdlc.pub, byte for byte.
inlined() { sed -n "/<<'$1'\$/,/^$1\$/p" "$BOOT" | sed '1d;$d'; }
for k in SDLC FLEET; do
  key_sha=$(inlined "${k}_KEY" | sha256sum | cut -c1-16)
  if [ -n "$(inlined "${k}_KEY")" ] && [ "$key_sha" = "$(sed -n "s/^${k}_KEY_ID=//p" "$BOOT")" ]; then
    ok "${k}_KEY_ID is the inlined key's sha256 ($key_sha)"
  else
    bad "${k}_KEY_ID is not the inlined key's sha256 (want $key_sha)"
  fi
done
if inlined SDLC_KEY | cmp -s - boot/keys/sdlc.pub; then
  ok "the inlined sdlc key is boot/keys/sdlc.pub"
else
  bad "the inlined sdlc key is not boot/keys/sdlc.pub"
fi
if [ "$(grep -c '^-----BEGIN PUBLIC KEY-----$' "$BOOT")" -eq 2 ]; then
  ok "the launcher inlines exactly two keys"
else
  bad "the launcher inlines $(grep -c '^-----BEGIN PUBLIC KEY-----$' "$BOOT") keys"
fi

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf 'bootstrap test PASSED\n'
  exit 0
fi
printf 'bootstrap test FAILED: %s check(s)\n' "$FAILED"
exit 1
