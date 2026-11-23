# sdlc-bootstrap trust keys

sdlc's own cosign public key goes here as `*.pub`; two during a rotation. It
is baked into the image, and it is the only thing boot trusts.

This is not the fleet key in `bin/sdlc-bootstrap`. The private half lives only
in sdlc's `sdlc-signing` environment as `SDLC_COSIGN_KEY`, so a fleet repo's
signing key can never produce something boot will run.

`sdlc.pub` is the current key (ECDSA P-256, generated 2026-11-23; SHA-256 of
its DER form 165390667128b84dc1110188e59246158f597ccecfe543d770e21617b4d18d2e).
The private half and its password are in Scott's Bitwarden Secrets Manager.
With no `*.pub` here, boot refuses everything (exit 2).
