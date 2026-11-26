#!/bin/sh
# scripts/sign-digest.sh against a fake cosign: what promote signs, that it
# refuses a wrong key or a bad digest before signing anything, and that a
# failed signature or verification fails the step.
#
# Usage: tests/test_sign_digest.sh

set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
SIGN=$PWD/scripts/sign-digest.sh
FAILED=0
ok()  { printf 'ok   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }

printf '== scripts/sign-digest.sh\n\n'

W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
mkdir "$W/bin"
D=sha256:$(printf 'c%.0s' $(seq 64))
A=quay.io/crunchtools/sdlc
B=quay.io/crunchtools/python-sdlc
sed -n "/<<'FLEET_KEY'\$/,/^FLEET_KEY\$/p" bin/sdlc-bootstrap | sed '1d;$d' > "$W/fleet.pub"

# The fake cosign. `public-key` prints $W/secret.pub; a reference is signed
# when $W/signed/<repo> exists; `sign-fails` and `verify-fails` break those.
cat > "$W/bin/cosign" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >> "$W/log"
for _last in "\$@"; do :; done
_repo=\$(printf '%s' "\${_last%@*}" | tr / _)
case "\$1" in
  public-key) cat "$W/secret.pub" ;;
  sign)   [ ! -e "$W/sign-fails" ] && mkdir -p "$W/signed" && : > "$W/signed/\$_repo" ;;
  verify) [ ! -e "$W/verify-fails" ] && [ -e "$W/signed/\$_repo" ] ;;
  *) exit 99 ;;
esac
EOF
chmod +x "$W/bin/cosign"

reset() {
  rm -rf "$W/log" "$W/signed" "$W/sign-fails" "$W/verify-fails"
  cp "$W/fleet.pub" "$W/secret.pub"
}
run() {              # args...: set RC and OUT
  RC=0
  OUT=$(PATH="$W/bin:$PATH" COSIGN_PRIVATE_KEY=k sh "$SIGN" "$@" 2>&1) || RC=$?
}

reset
run "$D" "$A" "$B"
if [ "$RC" -eq 0 ] && [ "$(grep -c '^sign ' "$W/log")" -eq 2 ] \
   && grep -q -- "^sign --yes --tlog-upload=false --use-signing-config=false --new-bundle-format=false --key env://COSIGN_PRIVATE_KEY $A@$D\$" "$W/log" \
   && grep -q -- "^sign --yes --tlog-upload=false --use-signing-config=false --new-bundle-format=false --key env://COSIGN_PRIVATE_KEY $B@$D\$" "$W/log"; then
  ok "an unsigned digest is signed by digest in both repositories"
else
  bad "signing both: exit $RC"; cat "$W/log"; printf '%s\n' "$OUT"
fi
if sed -n '$p' "$W/log" | grep -q -- "^verify --insecure-ignore-tlog --key .*/fleet.pub $B@$D\$" \
   && grep -A1 "^sign .* $A@$D\$" "$W/log" | grep -q "^verify .* $A@$D\$"; then
  ok "each signature is verified after it is made"
else
  bad "no verify after signing"; cat "$W/log"
fi

rm -f "$W/log"
run "$D" "$A" "$B"
if [ "$RC" -eq 0 ] && ! grep -q '^sign ' "$W/log" && printf '%s' "$OUT" | grep -q 'already signed' \
   && [ "$(grep -c -- "^verify --insecure-ignore-tlog --key .*/fleet.pub $A@$D\$" "$W/log")" -eq 2 ] \
   && [ "$(grep -c -- "^verify --insecure-ignore-tlog --key .*/fleet.pub $B@$D\$" "$W/log")" -eq 2 ]; then
  ok "an already-signed digest is verified in both repositories and not signed again"
else
  bad "already signed: exit $RC"; cat "$W/log"
fi

reset
printf 'not the fleet key\n' > "$W/secret.pub"
run "$D" "$A"
if [ "$RC" -eq 1 ] && ! grep -q '^sign \|^verify ' "$W/log" \
   && printf '%s' "$OUT" | grep -q 'not the private half'; then
  ok "a private key that is not the bootstrap's is refused before anything is signed"
else
  bad "wrong key: exit $RC"; cat "$W/log"
fi

for d in '' sha256:abc "sha256:$(printf 'G%.0s' $(seq 64))" "md5:$(printf 'c%.0s' $(seq 64))" "$A@$D"; do
  reset
  run "$d" "$A"
  if [ "$RC" -ne 0 ] && [ ! -e "$W/log" ]; then
    ok "digest '$d' is refused before cosign runs"
  else
    bad "digest '$d': exit $RC"
  fi
done

reset
RC=0
OUT=$(PATH="$W/bin:$PATH" COSIGN_PRIVATE_KEY='' sh "$SIGN" "$D" "$A" 2>&1) || RC=$?
if [ "$RC" -eq 1 ] && [ ! -e "$W/log" ]; then
  ok "no private key is a failure, not a skip"
else
  bad "no private key: exit $RC"
fi

reset
touch "$W/sign-fails"
run "$D" "$A"
if [ "$RC" -ne 0 ] && ! printf '%s' "$OUT" | grep -q '^signed '; then
  ok "a signature that cannot be made fails the step"
else
  bad "failed sign: exit $RC"
fi

reset
mkdir -p "$W/signed"
touch "$W/verify-fails"
run "$D" "$A"
if [ "$RC" -ne 0 ] && ! printf '%s' "$OUT" | grep -q '^signed '; then
  ok "a signature that does not verify fails the step"
else
  bad "failed verify: exit $RC"
fi

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf 'sign-digest test PASSED\n'
  exit 0
fi
printf 'sign-digest test FAILED: %s check(s)\n' "$FAILED"
exit 1
