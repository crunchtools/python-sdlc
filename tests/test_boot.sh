#!/bin/sh
# boot/sdlc against a fake docker and cosign: what it runs and how, and that
# every way the image can fail to be trustworthy is a 2 before `docker run`.
#
# Usage: tests/test_boot.sh

set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
BOOT=$PWD/boot/sdlc
FAILED=0
ok()  { printf 'ok   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }

printf '== boot/sdlc\n\n'

W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
mkdir "$W/bin"
# boot/sdlc's policy root is a literal, not an override, so the test runs a
# copy with that one line pointed at the scratch dir.
sed "s|^ROOT=/etc/sdlc-boot\$|ROOT=$W/keys|" "$BOOT" > "$W/sdlc"
[ "$(grep -c "^ROOT=$W/keys\$" "$W/sdlc")" -eq 1 ] || { echo "boot/sdlc has no ROOT=/etc/sdlc-boot line"; exit 1; }
BOOT=$W/sdlc
HEX=$(printf 'a%.0s' $(seq 64))
REPO=quay.io/crunchtools/sdlc

# The fake docker logs its argv, one line per call, and is steered by files in
# $W: `pull-fails`, `repodigests` (what inspect prints for RepoDigests),
# `label` (the version label), `no-self` (HOSTNAME is not a container) and
# `inner-rc` (what `docker run` exits with).
cat > "$W/bin/docker" <<EOF2
#!/bin/sh
printf '%s\n' "\$*" >> "$W/log"
case "\$1" in
  pull) [ ! -e "$W/pull-fails" ] ;;
  image) case "\$*" in
           *RepoDigests*) cat "$W/repodigests" ;;
           *) cat "$W/label" ;;
         esac ;;
  inspect) [ ! -e "$W/no-self" ] && echo selfid123 ;;
  run) exit "\$(cat "$W/inner-rc")" ;;
  *) exit 99 ;;
esac
EOF2

# The fake cosign verifies only with a key whose file name is in `accept`, and
# then prints the payload cosign prints: one object per signature, the
# annotations under .optional. `versions` lists the annotations to put in it.
cat > "$W/bin/cosign" <<EOF2
#!/bin/sh
printf '%s\n' "\$*" >> "$W/log"
[ "\$1" = verify ] || exit 99
[ "\$2" = --key ] || exit 99
_k=\$(basename "\$3")
grep -qx "\$_k" "$W/accept" 2> /dev/null || { echo "no matching signatures" >&2; exit 1; }
_out='['
_sep=
for _v in \$(cat "$W/versions"); do
  _out="\$_out\$_sep{\"optional\":{\"repo\":\"crunchtools/sdlc\",\"version\":\"\$_v\"}}"
  _sep=,
done
printf '%s]\n' "\$_out"
EOF2
chmod +x "$W/bin/docker" "$W/bin/cosign"

reset() {
  rm -rf "$W/log" "$W/pull-fails" "$W/no-self" "$W/keys"
  mkdir -p "$W/keys/keys"
  : > "$W/keys/keys/a.pub"
  printf '0\n' > "$W/keys/major"
  printf '0.12.0\n' > "$W/keys/minimum"
  printf '%s@sha256:%s\n' "$REPO" "$HEX" > "$W/repodigests"
  printf '0.12.1\n' > "$W/label"
  printf '0.12.1\n' > "$W/versions"
  printf 'a.pub\n' > "$W/accept"
  printf '0\n' > "$W/inner-rc"
}
run() {              # args...: set RC and OUT
  RC=0
  OUT=$(env PATH="$W/bin:$PATH" HOSTNAME="${HN-abc123}" \
          SDLC_TEST_PEM='-----BEGIN KEY-----
line two
-----END KEY-----' GITHUB_OUTPUT=/tmp/out \
          sh "$BOOT" "$@" 2>&1) || RC=$?
}
ran() { grep -q '^run ' "$W/log" 2> /dev/null; }
refused() {          # description [why]: exit 2, said so (with why), nothing run
  if [ "$RC" -eq 2 ] && printf '%s' "$OUT" | grep -q "^sdlc-boot: GATE CANNOT RUN: .*${2-}" && ! ran; then
    ok "$1"
  else
    bad "$1: exit $RC, run=$(ran && echo yes || echo no)"; printf '%s\n' "$OUT"
  fi
}

reset
run event
RUNLINE=$(grep '^run ' "$W/log" || true)
if [ "$RC" -eq 0 ] && [ "$(grep -c '^run ' "$W/log")" -eq 1 ] \
   && printf '%s' "$RUNLINE" | grep -q -- '--volumes-from selfid123' \
   && printf '%s' "$RUNLINE" | grep -q -- '--pull=never' \
   && printf '%s' "$RUNLINE" | grep -q -- " $REPO@sha256:$HEX event\$" \
   && grep -q -- "^pull -q $REPO:0\$" "$W/log" \
   && grep -q -- "^verify --key $W/keys/keys/a.pub --insecure-ignore-tlog=true -a repo=crunchtools/sdlc $REPO@sha256:$HEX\$" "$W/log"; then
  ok "a verified image runs by digest, from this container's volumes, with the args"
else
  bad "happy path: exit $RC"; cat "$W/log"; printf '%s\n' "$OUT"
fi
if printf '%s' "$RUNLINE" | grep -q -- ' -e SDLC_TEST_PEM ' \
   && printf '%s' "$RUNLINE" | grep -q -- ' -e GITHUB_OUTPUT ' \
   && ! printf '%s' "$RUNLINE" | grep -q 'BEGIN KEY\|line two'; then
  ok "the environment goes by name, so a multi-line value never reaches argv"
else
  bad "env by name"; printf '%s\n' "$RUNLINE"
fi
if ! printf '%s' "$RUNLINE" | grep -q -- ' -e \(PATH\|HOSTNAME\|PWD\|SHLVL\|_\|SDLC_BOOT_[A-Z]*\) '; then
  ok "PATH, HOSTNAME and boot's own settings are not passed on"
else
  bad "a boot variable leaked"; printf '%s\n' "$RUNLINE"
fi

reset
rm -f "$W/keys/keys/a.pub"
run event
refused "no public key is a refusal" "no sdlc public key"

reset
: > "$W/accept"
run event
refused "an image no key verifies is refused" "could not be verified"

reset
: > "$W/keys/keys/b.pub"
printf 'b.pub\n' > "$W/accept"
run event
if [ "$RC" -eq 0 ] && ran; then
  ok "a second key verifies (rotation)"
else
  bad "rotation: exit $RC"; printf '%s\n' "$OUT"
fi

reset
printf '0.12.2\n' > "$W/label"
run event
refused "a label that differs from the signed version is refused" "version label"

reset
printf '0.12.1 0.12.2\n' > "$W/versions"
run event
refused "two different signed versions are refused" "single version"

reset
: > "$W/versions"
run event
refused "no signed version is refused"

reset
printf '0.11.9\n' > "$W/label"; printf '0.11.9\n' > "$W/versions"
run event
refused "a version below the minimum is refused" "below the minimum"

reset
printf '0.9.0\n' > "$W/label"; printf '0.9.0\n' > "$W/versions"
run event
refused "0.9.0 is below 0.12.0 by number, not by string" "below the minimum"

reset
printf '0.12.0\n' > "$W/label"; printf '0.12.0\n' > "$W/versions"
run event
if [ "$RC" -eq 0 ] && ran; then
  ok "a version equal to the minimum runs"
else
  bad "equal to minimum: exit $RC"; printf '%s\n' "$OUT"
fi

reset
printf '1.0.0\n' > "$W/label"; printf '1.0.0\n' > "$W/versions"
run event
refused "a version in another major is refused" "not in major"

for v in 0.12 0.12.0-rc1 0.12.1.1 v0.12.1 0.012.1 0.12.x ''; do
  reset
  printf '%s\n' "$v" > "$W/label"; printf '%s\n' "$v" > "$W/versions"
  run event
  refused "version '$v' is not strict SemVer"
done

for d in '' "$REPO@sha256:abc" "$REPO@sha256:$(printf 'G%.0s' $(seq 64))" \
         "other.io/x@sha256:$HEX" "$REPO@sha256:$(printf 'a%.0s' $(seq 65))"; do
  reset
  printf '%s\n' "$d" > "$W/repodigests"
  run event
  refused "RepoDigest '$d' is refused"
done

reset
printf 'other.io/x@sha256:%s\n%s@sha256:%s\n' "$HEX" "$REPO" "$HEX" > "$W/repodigests"
run event
if [ "$RC" -eq 0 ] && ran; then
  ok "the digest is picked from the line that names the repository"
else
  bad "digest pick: exit $RC"; printf '%s\n' "$OUT"
fi

reset
touch "$W/pull-fails"
run event
refused "a pull that fails is refused" "could not be pulled"

reset
HN='' run event
refused "an empty HOSTNAME is refused" "HOSTNAME"

reset
touch "$W/no-self"
run event
refused "a HOSTNAME that is not a container is refused"

for bad_policy in "major=x" "major=01" "major=" "minimum=0.12" "minimum=0.12.0-rc1" \
                  "minimum=1.0.0" "minimum=00.1.0"; do
  reset
  printf '%s\n' "${bad_policy#*=}" > "$W/keys/${bad_policy%%=*}"
  run event
  refused "baked policy $bad_policy is refused"
done
for gone in major minimum; do
  reset
  rm "$W/keys/$gone"
  run event
  refused "a missing baked $gone is refused" "$gone is missing"
done

# The environment cannot loosen the policy: the old override names, set to a
# lower minimum and another key directory, change nothing.
reset
printf '0.9.0\n' > "$W/label"
printf '0.9.0\n' > "$W/versions"
mkdir -p "$W/other/keys"
RC=0
OUT=$(env PATH="$W/bin:$PATH" HOSTNAME=abc SDLC_BOOT_ROOT="$W/other" SDLC_BOOT_MIN=0.1.0 \
        SDLC_BOOT_MAJOR=0 sh "$BOOT" event 2>&1) || RC=$?
refused "SDLC_BOOT_* in the environment does not lower the minimum" "below the minimum 0.12.0"

reset
printf '1\n' > "$W/inner-rc"
run event
if [ "$RC" -eq 1 ]; then
  ok "the inner command's 1 is boot's 1"
else
  bad "inner exit 1 became $RC"
fi

# docker's own failures mean no check ran, so they are boot's 2, never a verdict.
for rc in 125 126 127; do
  reset
  printf '%s\n' "$rc" > "$W/inner-rc"
  run event
  if [ "$RC" -eq 2 ]; then
    ok "docker run exiting $rc is boot's 2"
  else
    bad "docker run exiting $rc became $RC"
  fi
done

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf 'boot test PASSED\n'
  exit 0
fi
printf 'boot test FAILED: %s check(s)\n' "$FAILED"
exit 1
