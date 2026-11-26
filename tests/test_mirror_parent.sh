#!/bin/sh
# scripts/mirror-parent.sh against fake skopeo, podman (cosign's container) and
# curl, in a copy of the tree whose ci.yml and MIRRORS each case sets: which
# cosign pins it takes, how it signs, and that it refuses a name or a tag it
# must not write before anything is copied or signed.
#
# Usage: tests/test_mirror_parent.sh

set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
FAILED=0
ok()  { printf 'ok   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }

printf '== scripts/mirror-parent.sh\n\n'

W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
T=$W/tree
mkdir -p "$W/bin" "$T/scripts" "$T/bin" "$T/sdlc_jobs" "$T/.github/workflows"
cp bin/sdlc-bootstrap "$T/bin/"
cp sdlc_jobs/_core.py "$T/sdlc_jobs/"
sed -n "/<<'FLEET_KEY'\$/,/^FLEET_KEY\$/p" bin/sdlc-bootstrap | sed '1d;$d' > "$W/fleet.pub"
printf '{"manifests":[]}\n' > "$W/up"
D=sha256:$(sha256sum < "$W/up" | cut -c1-64)
UP=quay.io/podman/stable@$D
GHCR=ghcr.io/sigstore/cosign/cosign@sha256:$(printf 'a%.0s' $(seq 64))
HI=registry.access.redhat.com/hi/cosign@sha256:$(printf 'b%.0s' $(seq 64))
SECRETS='{"COSIGN_PRIVATE_KEY":"k","COSIGN_PASSWORD":"p","QUAY_USERNAME":"u","QUAY_PASSWORD":"q"}'

# Every fake logs its argv, and LEAK when a QUAY_* reaches its environment. The
# registry is $W/reg/<reference with / : @ as _>.
cat > "$W/bin/skopeo" <<EOF
#!/bin/sh
printf 'skopeo %s\n' "\$*" >> "$W/log"
env | grep -q '^QUAY_' && echo LEAK >> "$W/log"
for _last in "\$@"; do :; done
key() { printf '%s' "\${1#docker://}" | tr '/:@' '___'; }
case "\$1" in
  login) [ ! -e "$W/login-fails" ] ;;
  inspect)
    if [ -e "$W/reg/\$(key "\$_last")" ]; then
      [ ! -e "$W/readback-fails" ] || { echo 'reading manifest: connection reset' >&2; exit 1; }
      cat "$W/reg/\$(key "\$_last")"
    elif [ -e "$W/inspect-error" ]; then cat "$W/inspect-error" >&2; exit 1
    else echo 'reading manifest: manifest unknown' >&2; exit 1; fi ;;
  copy)
    mkdir -p "$W/reg"; : > "$W/copied"
    cp "$W/up" "$W/reg/\$(key "\$_last")"
    cp "$W/up" "$W/reg/\$(key "quay.io/crunchtools/podman-stable@$D")" ;;
  *) exit 99 ;;
esac
EOF
cat > "$W/bin/podman" <<EOF
#!/bin/sh
printf 'podman %s\n' "\$*" >> "$W/log"
env | grep -q '^QUAY_' && echo LEAK >> "$W/log"
while [ "\$1" != -- ]; do shift; done
shift 2
case "\$1" in
  public-key) cat "$W/secret.pub" ;;
  verify) [ -e "$W/signed" ] ;;
  sign) [ ! -e "$W/sign-fails" ] && : > "$W/signed" ;;
  *) exit 99 ;;
esac
EOF
cat > "$W/bin/curl" <<EOF
#!/bin/sh
printf 'curl %s\n' "\$*" >> "$W/log"
env | grep -q '^QUAY_' && echo LEAK >> "$W/log"
cat "$W/gh-code"
EOF
chmod +x "$W/bin/skopeo" "$W/bin/podman" "$W/bin/curl"

# reset <cosign pin line> [MIRRORS]: a fresh registry, the fleet key, GitHub's 404.
reset() {
  rm -rf "$W/log" "$W/reg" "$W/signed" "$W/copied" "$W/sign-fails" "$W/login-fails" \
    "$W/inspect-error" "$W/readback-fails"
  cp "$W/fleet.pub" "$W/secret.pub"
  printf 404 > "$W/gh-code"
  printf 'env: { COSIGN_IMAGE: "%s" }  # pin\n' "$1" > "$T/.github/workflows/ci.yml"
  sed "s/^MIRRORS=.*/MIRRORS='${2:-podman-stable}'/" scripts/mirror-parent.sh > "$T/scripts/mirror-parent.sh"
}
run() {              # args...: set RC and OUT
  RC=0
  OUT=$(printf '%s' "$SECRETS" | PATH="$W/bin:$PATH" sh "$T/scripts/mirror-parent.sh" "$@" 2>&1) || RC=$?
}
untouched() { [ ! -e "$W/copied" ] && ! grep -q 'cosign.* sign ' "$W/log" 2>/dev/null; }

# 1. Both forms of ci.yml's cosign pin.
for pin in "$GHCR" "$HI"; do
  reset "$pin"
  run "$UP" podman-stable v5.8.9-immutable
  if [ "$RC" -eq 0 ] && grep -q -- "-- $pin sign " "$W/log"; then
    ok "the cosign pin $pin is run"
  else
    bad "pin $pin: exit $RC"; printf '%s\n' "$OUT"; cat "$W/log" 2>/dev/null || :
  fi
done
reset "docker.io/someone/cosign@sha256:$(printf 'c%.0s' $(seq 64))"
run "$UP" podman-stable v1
if [ "$RC" -eq 1 ] && printf '%s' "$OUT" | grep -q 'pins no cosign digest' && untouched; then
  ok "a cosign pin from anywhere else is refused"
else
  bad "foreign pin: exit $RC"
fi

# 2. The signature: legacy format, the annotation verify_parent requires, verified after.
reset "$HI"
run "$UP" podman-stable v5.8.9-immutable
if [ "$RC" -eq 0 ] \
   && grep -q -- "sign --yes --tlog-upload=false --use-signing-config=false --new-bundle-format=false --key env://COSIGN_PRIVATE_KEY -a repo=crunchtools/podman-stable -a upstream=quay.io/podman/stable quay.io/crunchtools/podman-stable@$D\$" "$W/log" \
   && grep -A1 -- ' sign ' "$W/log" | grep -q -- "verify .* -a repo=crunchtools/podman-stable quay.io/crunchtools/podman-stable@$D\$"; then
  ok "the digest is signed in the legacy format with repo=crunchtools/<name>, then verified"
else
  bad "signature: exit $RC"; cat "$W/log" 2>/dev/null || :
fi
if ! grep -q LEAK "$W/log"; then
  ok "no child sees QUAY_*"
else
  bad "QUAY_* reached a child"
fi
rm -f "$W/log"
run "$UP" podman-stable v5.8.9-immutable
if [ "$RC" -eq 0 ] && ! grep -q ' sign ' "$W/log" && printf '%s' "$OUT" | grep -q 'already signed'; then
  ok "a rerun on the same digest and tag signs nothing again"
else
  bad "rerun: exit $RC"; cat "$W/log" 2>/dev/null || :
fi

# 3. Names and tags it must not write.
for case in "sdlc|podman-stable|not one of the mirrors" \
            "rotv-base|rotv-base podman-stable|EXTRA_IMAGES" \
            "acquacotta|acquacotta podman-stable|EXTRA_IMAGES"; do
  n=${case%%|*}; rest=${case#*|}; m=${rest%%|*}; why=${rest#*|}
  reset "$HI" "$m"
  run "$UP" "$n" 0
  if [ "$RC" -eq 1 ] && printf '%s' "$OUT" | grep -q "$why" && [ ! -e "$W/log" ]; then
    ok "the name $n is refused ($why) before anything runs"
  else
    bad "name $n: exit $RC"; printf '%s\n' "$OUT"
  fi
done
for code in 200 301 000 403; do
  reset "$HI" "gatehouse podman-stable"
  printf '%s' "$code" > "$W/gh-code"
  run "$UP" gatehouse 0
  if [ "$RC" -eq 1 ] && printf '%s' "$OUT" | grep -q "HTTP $code" && untouched \
     && ! grep -q '^skopeo\|^podman' "$W/log"; then
    ok "GitHub answering $code for crunchtools/<name> is refused"
  else
    bad "GitHub $code: exit $RC"; printf '%s\n' "$OUT"
  fi
done

reset "$HI"
mkdir -p "$W/reg"
printf 'another manifest\n' > "$W/reg/quay.io_crunchtools_podman-stable_v5.8.9"
run "$UP" podman-stable v5.8.9
if [ "$RC" -eq 1 ] && printf '%s' "$OUT" | grep -q "already names sha256:.*refused" && untouched; then
  ok "a tag that names another digest is never moved"
else
  bad "moved tag: exit $RC"; printf '%s\n' "$OUT"
fi
reset "$HI"
printf 'reading manifest: unauthorized: access denied\n' > "$W/inspect-error"
run "$UP" podman-stable v5.8.9
if [ "$RC" -eq 1 ] && printf '%s' "$OUT" | grep -q 'could not read .*unauthorized' && untouched; then
  ok "a tag it cannot read is refused, not taken for a new one"
else
  bad "unreadable tag: exit $RC"; printf '%s\n' "$OUT"
fi

# 4. The key is checked before anything is copied.
reset "$HI"
printf 'not the fleet key\n' > "$W/secret.pub"
run "$UP" podman-stable v1
if [ "$RC" -eq 1 ] && printf '%s' "$OUT" | grep -q 'not the private half' && untouched \
   && ! grep -q '^skopeo' "$W/log"; then
  ok "a key that is not the fleet key is refused before the login and the copy"
else
  bad "wrong key: exit $RC"; cat "$W/log" 2>/dev/null || :
fi

# 5. A failed read-back says so, not a digest mismatch.
reset "$HI"
: > "$W/readback-fails"
run "$UP" podman-stable v1
if [ "$RC" -eq 1 ] && printf '%s' "$OUT" | grep -q 'could not read back' \
   && ! printf '%s' "$OUT" | grep -q 'did not keep'; then
  ok "a read-back that fails is reported as one"
else
  bad "read-back: exit $RC"; printf '%s\n' "$OUT"
fi

for up in "quay.io/podman/stable:v1" "quay.io/podman/stable@sha256:abc"; do
  reset "$HI"
  run "$up" podman-stable v1
  if [ "$RC" -ne 0 ] && [ ! -e "$W/log" ]; then
    ok "upstream '$up' is refused before anything runs"
  else
    bad "upstream '$up': exit $RC"
  fi
done

printf '\n'
[ "$FAILED" -eq 0 ] || { printf '%d failed\n' "$FAILED"; exit 1; }
printf 'all passed\n'
