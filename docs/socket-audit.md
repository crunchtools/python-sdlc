# Where repo content meets the socket or a secret

Audit for a1133, against examples/ci.yml at the bootstrap digest
`sha256:5f25b796…`. Every job runs `sdlc-bootstrap event` as root
(`--user 0`, `label=disable`) on the runner's rootful podman socket, mounted
at /var/run/docker.sock. boot/sdlc then runs the sdlc image with
`--volumes-from` itself and every env name forwarded (boot/sdlc:140-154), so
the sdlc process, and every child it starts, holds the socket and that job's
secrets. Whoever holds the socket can `podman inspect` the job container and
read its Config.Env, so the line that matters is whether untrusted content
runs **in-process** (a child of that container) or **nested** (a sibling
container started without the socket and without the job's env).

## Jobs

| Job | Untrusted content it reads or runs | Socket | Secrets in the job | Where the content runs | Verdict |
|---|---|---|---|---|---|
| check | PR head: gates, linters, uv/npm/go, plan, review | yes | read token (`actions: write`, `security-events: write`) | nested: `run_in_image`, no socket, only `SDLC_*`/`HOME`/`GIT_CONFIG_*` (bin/sdlc:474-492). In-process it only parses TOML and the Containerfile as text | mitigated |
| ai | PR diff and head, issue text and comments, model output | yes | CLAUDE_CODE_OAUTH_TOKEN, OPENROUTER_API_KEY, read token | **was in-process** ⚠: `claude -p` a child of the job, root, with a bare `Write` that reaches any path (docs: a bare tool name pre-approves writes outside the working directory). A model steered by the diff or a comment could rewrite `git` or `/opt/ashigaru/scripts/*.py`, which this job runs next with the socket. **Now nested**, see Fixes | fixed |
| image | the default branch's Containerfile and tree | yes | quay, RHSM, registry.redhat.io | `podman build` through the socket (a sibling: no socket, no volume, no host network); gates and boot nested with `--network none` (image.py:743-770, _boot.py:75-91); in-process only hadolint, `sdlc plan`, skopeo on text | mitigated ⚠ see below |
| write | PR head clone, the `ai` patch and result | yes | ASHIGARU_APP_KEY; children get a minted token | in-process, but nothing from the repo executes: fresh `git init` (no repo `.git/config`), `git apply`, `uv lock --no-build` (ashigaru relock.py), pinned `uvx ruff==` (tidy.py); `SDLC_AI_*` is `json.loads`ed and passed as argv, never to a shell | mitigated |
| promote | none: registry bytes only (skopeo, cosign, syft) | yes, unused | quay, cosign key (handed only to `cosign sign`/`attest`) | in-process, on an image `image` built and gated | mitigated |
| pypi | the default branch's tree | yes | OIDC request vars, read token | build nested (pypi.py:64-96); `uv publish` in-process in the tree the build wrote | mitigated ⚠ see below |
| gate | none: changed-file list and reviews by API, a fork's PR included | yes | read token (`pull-requests: write`) | reads no repo content | none |
| renovate | every repo, its package managers and their dependency code | yes (parent) | RENOVATE_APP_KEY, popped before anything runs (renovate.py:138-141) | nested: no socket, read-only root, only a per-repo minted RENOVATE_TOKEN (renovate.py:363-396) | mitigated |

⚠ marks a cell where untrusted content and the socket or a secret coexist.
The remaining two are bounded rather than open:

- **image** passes the RHSM activation key and org to every build as
  `--secret` files, whether or not `[tool.sdlc.image] rhsm` is set
  (image.py:681-694). A `RUN --mount=type=secret` can read it. Only
  default-branch content builds there (check's `if` limits `push` to the
  default branch), so it is reviewed code. The comment there makes it
  deliberate, because Containerfiles in the fleet use both secret ids.
  Gating it on `rhsm = true` would be least privilege, but it would break any
  repo that mounts the secret without setting the key. Left as is.
- **pypi** runs `uv publish` in a tree the nested build could rewrite.
  It is the repo's own reviewed release, and `dist_files` checks names only.
  Low.

## Runners: one job, one engine

crunchtools/gha-runner, deploy/exedore. Each `gha-runner@N` slot boots a VM
from a JIT config minted for that one VM (gha-runner-vm-mint), so it takes
exactly one job. The VM's runner powers it off after the job
(vm/rootfs/usr/lib/systemd/system/gha-runner.service:15-16), and the qcow2
overlay is deleted on the way in and on the way out and recreated from the
untouched base (gha-runner-vm:57-75). podman.socket is inside the VM
(Containerfile:84, gha-runner.service:13). The only thing the VM shares with
the host is the `images` virtiofs, read-only on both ends and holding image
layers only (gha-runner-vm:25-39, var-lib-shared-images.mount). Networking is
qemu user mode behind an nft fence that rejects RFC1918 and loopback for the
VM uids (gha-runner-egress.nft:19-24), and podman listens on no TCP port.
So one job cannot inspect another job's container: whoever holds a socket
reaches only their own job's secrets, plus root in a VM that is thrown away.

Two things the repo cannot confirm:

- The legacy container runners `gha-runner-uncached@` and `gha-runner-cached@`
  are still in deploy/exedore, carrying the `crunchtools-uncached` label
  (gha-runner-uncached.env:8). The README says the VMs replaced them. If an
  instance is still enabled, its rootless engine is per instance, not per job.
  The scratch area is wiped between jobs, but that is a weaker boundary than a
  VM. Check with `systemctl list-units 'gha-runner-*@*'` on Exedore.
- The repo describes no gloval pool. Its deploy files were removed and the
  README says gloval's runners were retired on 2026-11-18. Exedore is the only
  pool.

## Fixes

- **ai: the model runs in a sibling container** (sdlc_jobs/ai.py `Job.sibling`).
  The flags follow Renovate's: `--cap-drop=all`, `no-new-privileges`,
  `--read-only`, a private `/tmp`, this job's own image by digest with
  `--pull=never`, and only `CLAUDE_CODE_OAUTH_TOKEN` by name. The mounts are
  the scratch dir read-only, the checkout read-write, and the checkout's
  `.git` read-only, so nothing the model writes can change what the job's
  later `git add`/`git diff` run. The scratch dir moves under RUNNER_TEMP so
  that the sibling can mount it.
- **No change needed to the frozen files.** The `ai` job already forwards
  RUNNER_* and already holds the socket, which is all the sibling needs.
