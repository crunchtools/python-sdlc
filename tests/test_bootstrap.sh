#!/bin/sh
# bin/sdlc-bootstrap against a fake podman: nothing from the image runs before
# the fleet key's signature on its digest is verified, and everything after
# runs by that digest (python-sdlc#81). The real cosign and quay.io are what
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
# `unsigned`, and `digest` (what inspect answers). The launcher it prints
# records the environment and arguments it ran with.
cat > "$W/bin/podman" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >> "$W/log"
case " \$* " in
  *" pull "*)         [ ! -e "$W/pull-fails" ] ;;
  *" image exists "*) [ ! -e "$W/absent" ] ;;
  *" image inspect "*) cat "$W/digest" ;;
  *" verify "*)       [ ! -e "$W/unsigned" ] || { echo 'Error: no signatures found' >&2; exit 10; } ;;
  *" launcher "*)
    printf 'printf "%%s|%%s|%%s\\\\n" "\$SDLC_IMAGE" "\$SDLC_VERIFIED_FROM" "\$*" > "$W/ran"\n' ;;
  *) exit 125 ;;
esac
EOF
chmod +x "$W/bin/podman"

reset() {
  rm -rf "$W/log" "$W/ran" "$W/cache" "$W/pull-fails" "$W/absent" "$W/unsigned"
  printf '%s\n' "$D" > "$W/digest"
}
boot() {             # args...: run the bootstrap, set RC and OUT
  RC=0
  OUT=$(env -u CI -u GITHUB_ACTIONS PATH="$W/bin:$PATH" XDG_CACHE_HOME="$W/cache" \
          sh "$BOOT" "$@" 2>&1) || RC=$?
}

reset
boot versions
if [ "$RC" -eq 0 ] && [ "$(cat "$W/ran" 2>/dev/null)" = "$FLEET@$D|$FLEET:latest|versions" ]; then
  ok "a signed :latest runs its launcher by digest, told the name it came from"
else
  bad "signed :latest: exit $RC, ran '$(cat "$W/ran" 2>/dev/null)'"; printf '%s\n' "$OUT"
fi
if grep -q -- "verify --key /fleet.pub --insecure-ignore-tlog=true $FLEET@$D\$" "$W/log" \
   && grep -q -- "--pull=never -- $FLEET@$D launcher $FLEET@$D\$" "$W/log"; then
  ok "cosign verifies the digest, and the launcher comes from that digest, never pulled"
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
if [ -e "$W/cache/sdlc/verified/$(sed -n 's/^KEY_ID=//p' "$BOOT")/${D#sha256:}" ]; then
  ok "the verified digest is remembered under the key"
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
   && printf '%s' "$OUT" | grep -q 'GATE CANNOT RUN: .* could not be verified as signed by the crunchtools fleet key' \
   && printf '%s' "$OUT" | grep -q 'no signatures found' \
   && [ ! -d "$W/cache/sdlc/verified" ]; then
  ok "an unsigned image is a 2, says why, runs nothing and is not remembered"
else
  bad "unsigned: exit $RC, ran '$(cat "$W/ran" 2>/dev/null)'"; printf '%s\n' "$OUT"
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

# KEY_ID is the inlined key's sha256, so a new key cannot reuse old markers.
key_sha=$(sed -n '/^-----BEGIN PUBLIC KEY-----$/,/^-----END PUBLIC KEY-----$/p' "$BOOT" \
            | sha256sum | cut -c1-16)
if [ "$key_sha" = "$(sed -n 's/^KEY_ID=//p' "$BOOT")" ]; then
  ok "KEY_ID is the inlined key's sha256 ($key_sha)"
else
  bad "KEY_ID is not the inlined key's sha256 (want $key_sha)"
fi

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf 'bootstrap test PASSED\n'
  exit 0
fi
printf 'bootstrap test FAILED: %s check(s)\n' "$FAILED"
exit 1
