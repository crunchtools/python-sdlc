# sdlc-boot trust keys

sdlc's own cosign public key goes here as `*.pub`; two during a rotation. It
is baked into the image, and it is the only thing boot trusts.

This is not the fleet key in `bin/sdlc-bootstrap`. The private half lives only
in sdlc's `sdlc-signing` environment as `SDLC_COSIGN_KEY`, so a fleet repo's
signing key can never produce something boot will run.

No key is committed yet. Until one is, boot refuses everything (exit 2).
