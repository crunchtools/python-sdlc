#!/bin/sh
# scripts/attest-digest.sh against a fake cosign, syft and skopeo: what promote
# attests (an SBOM and SLSA provenance, per repository), that it scans and
# inspects only when something is missing, that it refuses a wrong key or a bad
# digest before attesting anything, that a failed attestation, verification or
# scan fails the step, and that only the attest step holds the key and only the
# keyless write step parses the image (constitution#209).
#
# Usage: tests/test_attest_digest.sh

set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
ATTEST=$PWD/scripts/attest-digest.sh
FAILED=0
ok()  { printf 'ok   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }

printf '== scripts/attest-digest.sh\n\n'

W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
mkdir "$W/bin"
D=sha256:$(printf 'c%.0s' $(seq 64))
A=quay.io/crunchtools/sdlc
B=quay.io/crunchtools/python-sdlc
SHA=$(printf 'a%.0s' $(seq 40))
sed -n "/<<'FLEET_KEY'\$/,/^FLEET_KEY\$/p" bin/sdlc-bootstrap | sed '1d;$d' > "$W/fleet.pub"

# The fake cosign. `public-key` prints $W/secret.pub; a reference carries a
# type of attestation when $W/att/<repo>_<type> exists; every attest also logs
# the type, repo and checksum of its predicate to $W/preds. `attest-fails` and
# `verify-fails` break those.
cat > "$W/bin/cosign" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >> "$W/log"
_cmd=\$1
_type= _pred=
while [ "\$#" -gt 0 ]; do
  case "\$1" in
    --type) _type=\$2; shift ;;
    --predicate) _pred=\$2; shift ;;
  esac
  _last=\$1
  shift
done
_repo=\$(printf '%s' "\${_last%@*}" | tr / _)
case "\$_cmd" in
  public-key) cat "$W/secret.pub" ;;
  attest)
    [ ! -e "$W/attest-fails" ] || exit 1
    printf '%s %s %s\n' "\$_type" "\$_repo" "\$(cksum < "\$_pred")" >> "$W/preds"
    mkdir -p "$W/att" && : > "$W/att/\${_repo}_\$_type" ;;
  verify-attestation) [ ! -e "$W/verify-fails" ] && [ -e "$W/att/\${_repo}_\$_type" ] ;;
  *) exit 99 ;;
esac
EOF

# The fake syft logs its arguments and writes a non-empty SBOM to the file
# named after `spdx-json=`; `syft-fails` breaks it, and `syft-empty` makes it
# succeed without writing one.
cat > "$W/bin/syft" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >> "$W/syft.log"
[ ! -e "$W/syft-fails" ] || exit 1
[ ! -e "$W/syft-empty" ] || exit 0
for _a in "\$@"; do
  case "\$_a" in spdx-json=*) printf '{"spdxVersion":"SPDX-2.3"}\n' > "\${_a#spdx-json=}" ;; esac
done
EOF

# The fake skopeo prints the labels in $W/labels.json as `skopeo inspect` would.
cat > "$W/bin/skopeo" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >> "$W/skopeo.log"
[ "\$1" = inspect ] || exit 99
printf '{"Labels": %s}\n' "\$(cat "$W/labels.json")"
EOF
chmod +x "$W/bin/cosign" "$W/bin/syft" "$W/bin/skopeo"

good_labels() {
  cat > "$W/labels.json" <<EOF
{
  "com.crunchtools.build.run": "https://github.com/crunchtools/sdlc/actions/runs/123/attempts/1",
  "com.crunchtools.build.builder": "crunchtools/sdlc/.github/workflows/ci.yml@refs/heads/main",
  "com.crunchtools.build.workflow": "crunchtools/sdlc/.github/workflows/ci.yml@refs/heads/main",
  "com.crunchtools.build.commit": "$SHA",
  "org.opencontainers.image.revision": "$SHA"
}
EOF
}
reset() {
  rm -rf "$W/log" "$W/att" "$W/out" "$W/preds" "$W/syft.log" "$W/skopeo.log" \
    "$W/attest-fails" "$W/verify-fails" "$W/syft-fails" "$W/syft-empty"
  cp "$W/fleet.pub" "$W/secret.pub"
  good_labels
}
write() {            # digest repo...: the keyless step alone; set RC and OUT
  RC=0
  OUT=$(PATH="$W/bin:$PATH" GITHUB_REPOSITORY=crunchtools/sdlc \
    sh "$ATTEST" write "$W/out" "$@" 2>&1) || RC=$?
}
attest() {           # digest repo...: the key step alone; set RC and OUT
  RC=0
  OUT=$(PATH="$W/bin:$PATH" COSIGN_PRIVATE_KEY=k sh "$ATTEST" attest "$W/out" "$@" 2>&1) || RC=$?
}
run() {              # digest repo...: both steps, as promote runs them; set RC and OUT
  write "$@"
  [ "$RC" -ne 0 ] || { _w=$OUT; attest "$@"; OUT="$_w
$OUT"; }
}
lines() { grep -c -- "$1" "$2" 2>/dev/null || true; }   # count matches, 0 if the file is absent

reset
run "$D" "$A" "$B"
if [ "$RC" -eq 0 ] && [ "$(lines '^attest ' "$W/log")" -eq 4 ]; then
  ok "a fresh digest gets both attestations in both repositories"
else
  bad "attesting both: exit $RC"; cat "$W/log"; printf '%s\n' "$OUT"
fi
ALL=1
for r in "$A" "$B"; do
  for t in spdxjson slsaprovenance1; do
    grep -q -- "^attest --yes --tlog-upload=false --use-signing-config=false --new-bundle-format=false --type $t --predicate .* --key env://COSIGN_PRIVATE_KEY $r@$D\$" "$W/log" || ALL=0
    # the verify that follows the attest is the line right after it
    grep -A1 -- "^attest .* --type $t .* $r@$D\$" "$W/log" \
      | grep -q -- "^verify-attestation --insecure-ignore-tlog --type $t --key .*/fleet.pub $r@$D\$" || ALL=0
  done
done
if [ "$ALL" -eq 1 ]; then
  ok "each attestation is made by digest and verified right after"
else
  bad "attest or verify missing"; cat "$W/log"
fi
if [ "$(lines '' "$W/syft.log")" -eq 1 ] && grep -q -- " registry:$A@$D " "$W/syft.log" \
   && grep -q -- "-o spdx-json=" "$W/syft.log" \
   && [ "$(lines '' "$W/skopeo.log")" -eq 1 ] && grep -q -- "^inspect docker://$A@$D\$" "$W/skopeo.log"; then
  ok "the image is scanned and inspected once, from the first repository"
else
  bad "syft or skopeo calls"; cat "$W/syft.log" "$W/skopeo.log"
fi
SBOMS=$(grep '^spdxjson ' "$W/preds" | cut -d' ' -f3- | sort -u | wc -l)
PROVS=$(grep '^slsaprovenance1 ' "$W/preds" | cut -d' ' -f3- | sort -u | wc -l)
if [ "$SBOMS" -eq 1 ] && [ "$PROVS" -eq 1 ] && [ "$(lines '' "$W/preds")" -eq 4 ]; then
  ok "the same SBOM and the same provenance are attested in both repositories"
else
  bad "predicates differ between repositories"; cat "$W/preds"
fi

rm -f "$W/log" "$W/syft.log" "$W/skopeo.log" "$W/preds"
run "$D" "$A" "$B"
if [ "$RC" -eq 0 ] && ! grep -q '^attest ' "$W/log" \
   && [ ! -e "$W/syft.log" ] && [ ! -e "$W/skopeo.log" ] \
   && printf '%s' "$OUT" | grep -q 'already carries an SBOM' \
   && printf '%s' "$OUT" | grep -q 'already carries provenance'; then
  ok "an already-attested digest is not scanned, inspected or attested again"
else
  bad "already attested: exit $RC"; cat "$W/log"; printf '%s\n' "$OUT"
fi
VER=1   # three each: the up-front check, the one that skips the attest, the proof
for r in "$A" "$B"; do
  for t in spdxjson slsaprovenance1; do
    [ "$(lines "^verify-attestation --insecure-ignore-tlog --type $t --key .*/fleet.pub $r@$D\$" "$W/log")" -eq 3 ] || VER=0
  done
done
if [ "$VER" -eq 1 ]; then
  ok "an already-attested digest is still verified in both repositories"
else
  bad "verifies missing"; cat "$W/log"
fi

reset
mkdir -p "$W/att"
AR=$(printf '%s' "$A" | tr / _)
: > "$W/att/${AR}_spdxjson"
: > "$W/att/${AR}_slsaprovenance1"
run "$D" "$A" "$B"
if [ "$RC" -eq 0 ] && [ "$(lines '' "$W/syft.log")" -eq 1 ] && [ "$(lines '' "$W/skopeo.log")" -eq 1 ] \
   && [ "$(lines '^attest ' "$W/log")" -eq 2 ] \
   && ! grep -q -- "^attest .* $A@$D\$" "$W/log" \
   && grep -q -- "^attest .* --type spdxjson .* $B@$D\$" "$W/log" \
   && grep -q -- "^attest .* --type slsaprovenance1 .* $B@$D\$" "$W/log"; then
  ok "a digest attested in one repository is scanned once and attested only in the other"
else
  bad "partial: exit $RC"; cat "$W/log"
fi

reset
printf '{}\n' > "$W/labels.json"
run "$D" "$A" "$B"
if [ "$RC" -eq 0 ] && [ "$(lines '^attest ' "$W/log")" -eq 2 ] \
   && ! grep -q -- '^attest .*--type slsaprovenance1' "$W/log" \
   && grep -q -- "^attest .* --type spdxjson .* $A@$D\$" "$W/log" \
   && grep -q -- "^attest .* --type spdxjson .* $B@$D\$" "$W/log" \
   && printf '%s' "$OUT" | grep -q '::warning::.*no provenance attested'; then
  ok "labels with no build record get the SBOM, no provenance, and a warning"
else
  bad "no build record: exit $RC"; cat "$W/log"; printf '%s\n' "$OUT"
fi

reset
printf 'not the fleet key\n' > "$W/secret.pub"
run "$D" "$A"
if [ "$RC" -eq 1 ] && ! grep -q '^attest ' "$W/log" \
   && printf '%s' "$OUT" | grep -q 'not the private half'; then
  ok "a private key that is not the bootstrap's is refused before anything is attested"
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
OUT=$(PATH="$W/bin:$PATH" COSIGN_PRIVATE_KEY='' \
  sh "$ATTEST" attest "$W/out" "$D" "$A" 2>&1) || RC=$?
if [ "$RC" -eq 1 ] && [ ! -e "$W/log" ]; then
  ok "no private key is a failure, not a skip"
else
  bad "no private key: exit $RC"
fi

reset
touch "$W/attest-fails"
run "$D" "$A"
if [ "$RC" -ne 0 ] && ! printf '%s' "$OUT" | grep -q '^attested the '; then
  ok "an attestation that cannot be made fails the step"
else
  bad "failed attest: exit $RC"
fi

reset
touch "$W/verify-fails"
run "$D" "$A"
if [ "$RC" -ne 0 ] && ! printf '%s' "$OUT" | grep -q '^attested the '; then
  ok "an attestation that does not verify fails the step"
else
  bad "failed verify: exit $RC"
fi

reset
touch "$W/syft-fails"
run "$D" "$A"
if [ "$RC" -ne 0 ] && ! grep -q '^attest ' "$W/log" && ! printf '%s' "$OUT" | grep -q '^attested the '; then
  ok "a scan that fails fails the step before anything is attested"
else
  bad "failed syft: exit $RC"
fi

reset
touch "$W/syft-empty"
run "$D" "$A"
if [ "$RC" -ne 0 ] && ! grep -q '^attest ' "$W/log" && printf '%s' "$OUT" | grep -q 'syft wrote no SBOM'; then
  ok "a scan that writes no SBOM fails the step before anything is attested"
else
  bad "empty syft: exit $RC"
fi

# Each predicate is made only when some repository lacks that one type.
reset
mkdir -p "$W/att"
for r in "$A" "$B"; do : > "$W/att/$(printf '%s' "$r" | tr / _)_slsaprovenance1"; done
run "$D" "$A" "$B"
if [ "$RC" -eq 0 ] && [ "$(lines '' "$W/syft.log")" -eq 1 ] && [ ! -e "$W/skopeo.log" ] \
   && [ "$(lines '^attest .* --type spdxjson ' "$W/log")" -eq 2 ] \
   && [ "$(lines '^attest .* --type slsaprovenance1 ' "$W/log")" -eq 0 ]; then
  ok "only the SBOM missing: scanned once, not inspected, only the SBOM attested"
else
  bad "SBOM only: exit $RC"; cat "$W/log"
fi

reset
mkdir -p "$W/att"
for r in "$A" "$B"; do : > "$W/att/$(printf '%s' "$r" | tr / _)_spdxjson"; done
run "$D" "$A" "$B"
if [ "$RC" -eq 0 ] && [ ! -e "$W/syft.log" ] && [ "$(lines '' "$W/skopeo.log")" -eq 1 ] \
   && [ "$(lines '^attest .* --type slsaprovenance1 ' "$W/log")" -eq 2 ] \
   && [ "$(lines '^attest .* --type spdxjson ' "$W/log")" -eq 0 ]; then
  ok "only the provenance missing: inspected once, not scanned, only the provenance attested"
else
  bad "provenance only: exit $RC"; cat "$W/log"
fi

# The key boundary (constitution#209): write never runs with the key, and
# attest never runs anything that parses the image.
for v in COSIGN_PRIVATE_KEY COSIGN_PASSWORD; do
  reset
  RC=0
  OUT=$(env "$v=k" PATH="$W/bin:$PATH" GITHUB_REPOSITORY=crunchtools/sdlc \
    sh "$ATTEST" write "$W/out" "$D" "$A" 2>&1) || RC=$?
  if [ "$RC" -eq 1 ] && [ ! -e "$W/log" ] && [ ! -e "$W/syft.log" ] && [ ! -e "$W/skopeo.log" ] \
     && printf '%s' "$OUT" | grep -q 'must run without'; then
    ok "write refuses to run with $v set, before anything parses the image"
  else
    bad "write with $v: exit $RC"; printf '%s\n' "$OUT"
  fi
done

reset
write "$D" "$A" "$B"
rm -f "$W/syft.log" "$W/skopeo.log"
attest "$D" "$A" "$B"
if [ "$RC" -eq 0 ] && [ ! -e "$W/syft.log" ] && [ ! -e "$W/skopeo.log" ] \
   && [ "$(lines '^attest ' "$W/log")" -eq 4 ]; then
  ok "attest runs only cosign: no scan and no inspect with the key in the environment"
else
  bad "attest parsed the image: exit $RC"; cat "$W/syft.log" "$W/skopeo.log" 2>/dev/null
fi

reset
attest "$D" "$A"
if [ "$RC" -ne 0 ] && ! grep -q '^attest ' "$W/log" && printf '%s' "$OUT" | grep -q 'write step left none'; then
  ok "attest with no SBOM written fails rather than attesting nothing"
else
  bad "attest without write: exit $RC"; printf '%s\n' "$OUT"
fi

reset
RC=0
OUT=$(PATH="$W/bin:$PATH" sh "$ATTEST" sign "$W/out" "$D" "$A" 2>&1) || RC=$?
if [ "$RC" -eq 1 ] && [ ! -e "$W/log" ]; then
  ok "an unknown step is refused"
else
  bad "unknown step: exit $RC"
fi

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf 'attest-digest test PASSED\n'
  exit 0
fi
printf 'attest-digest test FAILED: %s check(s)\n' "$FAILED"
exit 1
