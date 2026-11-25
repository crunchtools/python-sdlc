#!/bin/sh
# Gloval's half of scripts/local-release.sh: read the fleet release secrets out
# of OpenBao with the gloval-host AppRole and write them as ONE JSON object on
# stdout, for a pipe into local-release.sh on Exedore. Never to a terminal,
# never to disk, never on argv: the role and secret ids go to jq by --rawfile,
# the OpenBao token to curl by a header on stdin.
#
# kv/gloval/github is the mirror of the org secrets ci.yml's image and promote
# jobs hold: the fleet cosign key, RHSM and registry.redhat.io, and the Ashigaru
# App key local-release.sh mints its read token from. The quay robot is
# kv/gloval/crunchtools-sdlc-quay's (crunchtools+sdlc).
#
# Usage (from a seat that reaches both hosts):
#   ssh gloval 'sh -s' < scripts/local-release-secrets.sh \
#     | ssh exedore '<sdlc checkout>/scripts/local-release.sh <owner/repo> <sha>'
set -eu

[ ! -t 1 ] || { echo "local-release-secrets: stdout is a terminal; pipe it into local-release.sh" >&2; exit 1; }

# shellcheck source=/dev/null
. /srv/openbao.crunchtools.com/config/openbao-tools-env
creds=$OPENBAO_CREDDIR/gloval-host
token=$(jq -n --rawfile r "$creds.role_id" --rawfile s "$creds.secret_id" \
    '{role_id:($r|sub("\\s+$";"")), secret_id:($s|sub("\\s+$";""))}' \
  | curl -fsS --cacert "$OPENBAO_CA" -X PUT --data-binary @- "$OPENBAO_ADDR/v1/auth/approle/login" \
  | jq -er .auth.client_token) \
  || { echo "local-release-secrets: the gloval-host AppRole login failed" >&2; exit 1; }

# One read of a KV path's data; the token goes to curl as a header on stdin.
read_kv() {
  printf 'X-Vault-Token: %s\n' "$token" \
    | curl -fsS --cacert "$OPENBAO_CA" -H @- "$OPENBAO_ADDR/v1/$OPENBAO_KVMOUNT/data/$OPENBAO_PREFIX/$1" \
    | jq -ce .data.data \
    || { echo "local-release-secrets: $OPENBAO_PREFIX/$1 could not be read" >&2; exit 1; }
}
# The quay robot is crunchtools-sdlc-quay's: github's QUAY_* is a stale login.
github=$(read_kv github)
quay=$(read_kv crunchtools-sdlc-quay)
printf '%s\n%s\n' "$github" "$quay" | jq -cse '.[0] as $g | .[1] as $q
  | $g | {COSIGN_PRIVATE_KEY, COSIGN_PASSWORD, ASHIGARU_APP_KEY,
          RHSM_ACTIVATION_KEY, RHSM_ORG_ID, REGISTRY_REDHAT_IO_USER, REGISTRY_REDHAT_IO_PASSWORD}
  + {QUAY_USERNAME: $q.USERNAME, QUAY_PASSWORD: $q.PASSWORD}'
