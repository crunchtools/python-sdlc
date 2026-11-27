# Changelog

All notable changes to this project are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `EXTRA_IMAGES` gives crunchtools/trentina `trentina-l2-model`, the L2 model
  image a training round publishes through `ci.yml` (crunchtools/trentina#954,
  a1793).

### Fixed

- The lane memory budget now keeps a quarter of MemTotal (never under 512 MB)
  for the kernel, podman and the page cache, as it already did for a cgroup
  ceiling (a1809, crunchtools/gha-runner#71). A 16G runner VM gave four lanes
  3.9G each: every `--memory` held, but together they filled the VM, and
  trentina#955's check thrashed at its ceiling for 38 minutes. Those four lanes
  now take turns; three still run side by side. A 32G laptop's four lanes get
  5.8G each instead of 8G.
- Constitution R4 reads a Deprecated bullet's wrapped continuation lines, so an
  end release named on a later line no longer fails (#269). It also takes
  "renamed in X" and "retired in X" as naming the end release, and no longer
  reads a continuation line that opens with `--` or `**bold**` as a new bullet.
  claude-supervisor's 2.0.0 CHANGELOG, which hit the first two, now passes.

### Removed

- `.github/workflows/container.yml` (a1790). `ci.yml`'s `test`, `image` and
  `sign` already build, test, sign and publish every release; `scripts/self.sh`
  now also runs `tests/test_mirror_parent.sh` and shellchecks `mirror-parent.sh`,
  the one test only container.yml ran. `:main` and the `python-sdlc` mirror,
  which only container.yml moved, stay at 0.15.8 and are no longer published.

### Changed

- `bin/sdlc-bootstrap` refuses `SDLC_IMAGE=main` with exit 2 and says why:
  `:main` stopped at 0.15.8, and running it would gate on a frozen image that
  still verifies under the fleet key. Use `latest`, a release, a digest, or a
  local build through a clone's `bin/sdlc`. `examples/pre-commit-floating.yaml`
  no longer suggests it; re-copy it. `scripts/provenance.py` now trusts a build
  record from `ci.yml`, and the README says sdlc's own releases are signed but
  not attested since 0.15.9 (#266).

### Documentation

- `docs/marketplace-free-ci.md` records spike #103's open items re-run on
  crunchtools/ci-spike (a1792): review-comment re-triage, the `pypi`
  environment's default-branch policy, a tag push starting no job, and an App
  release end to end, each with its run.

### Security

- The fleet `ci.yml` (and sdlc's own) give the `ai` job `CLAUDE_CODE_OAUTH_TOKEN`
  only for this repository's own pull requests and for issues opened by an
  owner, member or collaborator (#215, a1650). A fork's `pull_request_target`
  or an outsider's issue now runs triage with no Claude token, so text an
  outsider wrote never reaches a model that holds one. Re-copy `examples/ci.yml`.

## [0.18.0] - 2026-11-26

### Added

- `scripts/mirror-parent.sh` mirrors one upstream parent, pinned by digest,
  into `quay.io/crunchtools/<name>:<tag>` with `skopeo copy --all
  --preserve-digests` and signs it with the fleet key and
  `repo=crunchtools/<name>`, the signature the image job's `verify_parent`
  trusts (a1730). A repo built FROM `quay.io/podman/stable`, which nobody
  signs, was refused; it now builds FROM
  `quay.io/crunchtools/podman-stable` at the same digest. It runs on Exedore
  with the secrets `local-release-secrets.sh` already pipes.

### Changed

- cosign and grype come from Hummingbird (`registry.access.redhat.com/hi/cosign`
  and `hi/grype`, pinned by index digest), not ghcr.io and docker.io: seats and
  builds pull only from quay.io, Red Hat's registries and Fedora's (a1751).
  grype moves 0.121.0 to 0.122.1, with the same findings, table columns and
  exit codes on the same rootfs. cosign moves 2.6.5 to 3.1.6 everywhere sdlc
  runs it: the image, the bootstrap's verify, `COSIGN_IMAGE` in `ci.yml`, the
  examples and `hotfix.sh`.
- Every `cosign sign` and `cosign attest` (promote, `sign-digest.sh`,
  `attest-digest.sh`) passes `--use-signing-config=false
  --new-bundle-format=false`. v3 otherwise writes a protobuf bundle as an OCI
  referrer, which podman and bootc cannot read, and it refuses
  `--tlog-upload=false` outright. With the flags, v3 writes the same `.sig` and
  `.att` tags v2 did. v2 and v3 verify each other's signatures and
  attestations, and podman's `sigstoreSigned` policy accepts them. cosign
  2.6.5 accepts the flags, so container.yml's downloaded v2 binary keeps
  working.
- ShellCheck runs from `quay.io/crunchtools/shellcheck`, v0.11.0 built from
  upstream source and fleet-signed, not `docker.io/koalaman/shellcheck`: the
  pre-commit hook, `scripts/self.sh` and `container.yml` (a1751).

## [0.17.1] - 2026-11-26

### Fixed

- The `pypi` job no longer refuses every build: `uv build` writes a
  `.gitignore` into `dist/`, and the check that each file is named for the
  release counted it. That one name is now skipped and never uploaded; any
  other stray file still refuses (a1745).

## [0.17.0] - 2026-11-26

### Added

- `scripts/local-release.sh` runs ci.yml's `pypi` job after promote when the
  release publishes to PyPI, so fleet releases reach PyPI again while org
  Actions are off. `local-release-secrets.sh` emits `PYPI_API_TOKEN` from
  `kv/gloval/crunchtools`; the script hands it to the pypi job alone, as
  `UV_PUBLISH_TOKEN`, and stops before image when a PyPI release has no token.
  `--pypi <owner/repo> <sha>` publishes a release already tagged that missed
  PyPI, reading every page of the repo's tags for it. A PyPI failure after
  promote says the release is out and prints that retry line (a1738).
- The `pypi` job publishes with an API token in `UV_PUBLISH_TOKEN` when no
  OIDC request variables are present. The token is kept out of every other
  step's environment, the nested build included, and off argv; OIDC stays the
  CI path, and a job holding both refuses (a1738).

### Fixed

- `tests/test_job_pypi.py` ran its `safe.directory` test only under pytest: it
  was defined after the script's `main()` call.
- `tests/test_local_release.py` runs the script end to end against faked host
  tools (`tests/local_release_stub.py`, under `SDLC_LOCAL_RELEASE_ROOT`) and
  asserts what each job was handed. It was regexes over the source with no
  `test_` function, so pytest collected nothing and moved or deleted lines
  passed.
- The image installs `libzstd` with `zstd`. Hummingbird's zstd 1.6.0 does not
  require the libzstd it links against, the base keeps libzstd 1.5.7, and
  `unzstd` failed on an undefined `ZSTD_isDeterministicBuild` while unpacking
  the CodeQL bundle, failing the build (a1745).

### Security

- `pypi` uploads with `uv --no-config publish --publish-url
  https://upload.pypi.org/legacy/`, on the OIDC path too. uv ran in the
  checkout, so a committed `uv.toml` or `[tool.uv] publish-url`, or one the
  nested build wrote into the read-write workspace, chose where the files and
  the credential went. The token also leaves `os.environ` before the first
  step, so the shared helpers' podman calls do not inherit it (a1738).

## [0.16.9] - 2026-11-26

### Fixed

- `promote` reads cosign attachments up to 512 MiB. cosign uploads an
  attestation of any size but refuses to read one over 128 MiB by default, so
  rhel10-bootc-workstation 0.3.0's 185 MB SBOM attestation failed promote's own
  verify-attestation and nothing was tagged in the registry (a1654).

## [0.16.8] - 2026-11-26

### Fixed

- **`boot` pulls the gated digest under a policy of its own (a1688).** A host
  whose policy.json requires a fleet signature for every quay.io/crunchtools
  image refuses the engine's pull of `gated-<sha>`, which promote signs only
  after boot: "Source image rejected: A signature was required" on Exedore's
  rhel10-bootc-* releases, and on every repo once rhel10-bootc-server 1.8.x
  lands. podman-remote cannot hand the engine a policy, so skopeo, in the job,
  copies `ref@digest` under a policy that rejects everything but that one
  digest, keeping the manifest so the digest is unchanged, and the engine loads
  the archive as `ref:gated-<sha>`. A tag, another digest or another repository
  is refused by that policy; nothing is signed before promote, and the host's
  policy is untouched. local-release.sh needs no change: its service only
  loads the archive.

## [0.16.7] - 2026-11-26

### Fixed

- **local-release.sh starts its podman service with no secret in its
  environment (a1679).** The service inherited every secret the script exports,
  and a build secret whose id names a variable in the service's environment
  reaches the RUN empty: ubi10-httpd-perl-mariadb's
  `--mount=type=secret,id=RHSM_ACTIVATION_KEY` read nothing, and
  subscription-manager said "Must specify an activation key". The lowercase
  ids (`activation_key`) never collided. CI was not affected: a runner's podman
  holds no secret.
- `check` on a push treats a 403 from listing the merged pull request's
  Actions runs as no evidence the pull request passed, and runs every gate,
  with one line saying why. `local-release.sh`'s App token cannot list runs on
  a private repository, where the Ashigaru App holds no Actions permission, so
  rhel10-bootc-workstation's local release stopped there with a 2 (a1654). A
  401, a 404 or any other failure is still a 2.

- **A local release's image earns SLSA provenance (a1683).** image left an
  empty label off the build, so a build by scripts/local-release.sh, which has
  no workflow ref, inherited its parent's `com.crunchtools.build.builder` and
  `.workflow`: rt 1.0.5 and ubi10-httpd-perl-mariadb carried ubi10-httpd's
  `ci.yml@refs/heads/main`, and promote's `local_provenance`, which wants both
  empty, attested none. image now writes every label, empty ones included,
  overriding what the parent stamped. No condition in promote changed.

## [0.16.6] - 2026-11-25

### Changed

- **A release is tagged only after its gates pass (a1667).** The fleet's
  `write` needs `image` and `boot`, and on a release its `if:` wants each to
  have succeeded or been skipped with none planned; `write` also refuses in
  code when the `SDLC_IMAGE` or `SDLC_BOOT` it is handed says otherwise (a
  fleet file older than this maps neither, and stays unenforced, as promote
  treats `SDLC_BOOT`). A release whose image or boot fails leaves no `vX.Y.Z`
  and no GitHub Release behind, as rhel10-bootc-server v1.8.0 did. Fleet repos
  take it by re-copying `examples/ci.yml`.
- `scripts/local-release.sh` runs check, image, boot, write, promote: `write`
  after the gates, handed their results.
- sdlc's own release: `image` builds the version `check` planned without
  waiting on `write`, `write` waits on `image`, and `sign` signs only what
  `write` then released (`SDLC_WRITE` success, `SDLC_RELEASE` at the image's
  version). `scripts/hotfix.sh` hands self.sh the same inputs.
- `containerfile-lint`: a Containerfile hadolint cannot parse is a finding
  (exit 1) in the PR check, as it already failed the image job, instead of
  ADVISORY and SKIPPED there. A PR no longer passes a lint its release fails.

### Fixed

- `bootc-lint` gets `DAC_OVERRIDE` back after `--cap-drop=all`, on a
  `--read-only` root, and nothing else. `bootc container lint` walks the whole
  image as root, and a RHEL bootc image with setroubleshoot ships
  `/var/lib/setroubleshoot` 0700 to another user, so the gate stopped at
  "Permission denied (os error 13)" on an image whose in-build lint, with
  podman's default capabilities, passed (rhel10-bootc-server 1.8.1, a1654).
  Not `DAC_READ_SEARCH`: it is what `open_by_handle_at` needs, the "shocker"
  escape, which is why it is outside the default set. The image job takes
  `--cap-add` only for bootc-lint, only `DAC_OVERRIDE`, and only beside
  `--read-only`.
- sdlc's own release plan carries `image: true`, so `write`'s own refusal
  holds back a release whose image was skipped, not only ci.yml's `if:` (a1667)
- S2 parses a Renovate config as JSON5 and judges the keys it sets and the
  presets it extends, so a locked name in a description, a comment or
  `ignorePresets` is no longer an override. The locked keys and presets come
  from the constitution's `fleet.toml`, not a list in sdlc. A preset from
  outside Renovate's built-ins and crunchtools/constitution (`github>…`,
  `local>…`, a URL, an npm package) is reported as unjudgeable. The image adds
  json5 0.17.3 (sdlc#203, a1411)
- The image carries one constitution pin, at constitution main, with
  `constitution.md` and `fleet.toml`; `audit_alert` is vendored as
  `sdlc_jobs/_audit_alert.py`, since the constitution deleted it (a1411)
- S1 counts a `--require-hashes` install as from the lock only when the flag is
  the install's own and its requirements file is one the repo commits, copied
  in and not named by any later shell command except as an install's `-r`
  (so `>>`, `sed -i`, `cp` and `curl -o` over it count as generated). A copy
  into `.`, the WORKDIR or a known directory keeps the file's name. Commands
  now split on `||` and `|` too, with inline comments dropped (a `#` inside
  quotes is text), and an exec-form `COPY ["src", "dest"]` is read as JSON
  (sdlc#203, a1411)

## [0.16.5] - 2026-11-25

### Added

- A local release's image is attested with SLSA v1 provenance, the predicate
  CI's promote writes, signed with the fleet key. Its builder is
  `scripts/local-release.sh` at the sdlc commit it ran from, never a workflow
  run. Promote writes it only when the image's run label carries that run's
  attempt and no run id (a1649).

### Fixed

- boot finds the runner's socket when its container took it by `--volumes-from`,
  as the bootstrap starts it: podman lists those mounts only under the source
  container, so every boot job on podman refused with "the runner's socket is
  not mounted" (ashigaru 6.0.1, a1416)
- `scripts/local-release.sh` holds a per-project lock for the whole run, so two
  runs of one repo no longer remove each other's images in cleanup (a1649)

## [0.16.4] - 2026-11-25

### Fixed

- scripts/local-release.sh runs its podman service in a private mount namespace
  that hides a RHEL host's entitlement, so subscription-manager in a build's RHSM
  step registers instead of refusing in container mode (a1551, #233)
- write verifies a release whose version is dynamic: with `version` in
  `[project].dynamic`, the version is read from `[tool.hatch.version].path` at
  the merge (ashigaru's `VERSION`), a relative path inside the repository holding
  an X.Y.Z, instead of refusing it as None (a1416)
- promote reads a repo's own `version` label before the OCI version its base
  sets, so the rebuild of a tagged release (hermes 1.2.0 on hummingbird/python
  3.13.18) moves its version tag (a1551)

## [0.16.3] - 2026-11-25

### Added

- `workflow-lint`, a check in push and ci for every repo: zizmor, offline, over
  its GitHub Actions workflows and actions (RT#1662). Advisory until a repo sets
  `gate = true` in its table, because most of the fleet's workflows have high
  findings today.
- `renovate.json`: Renovate moves markdownlint-cli2 and the final-stage copies
  of the Gourmand, Gatehouse and lychee ARGs, beside the first copies and the
  sdlc-bootstrap base digest its dockerfile manager already moves, under the
  fleet's 7-day release-age cooldown.

### Removed

- `update-tools.yml` and `scripts/update_tools.py`, the last user of the
  crunchtools-ci App here (RT#1669): Renovate moves those pins now.

### Fixed

- image no longer pins a digest into a line that continues a RUN: a
  `python -c "...` line starting `from x import y` matched the FROM rewrite
  (hermes 1.2.0) (a1551)
- The image carries Ashigaru at 044cdff: publish arms auto-merge only on a base
  branch that requires checks (ashigaru#257), and the staged guard refuses
  .python-version, uv.toml, `[tool.uv]` and mode changes (ashigaru#263).
- `write` verifies a release against package.json's version when pyproject.toml
  has no `[project]` table (rotv keeps only `[tool.sdlc]` there), instead of
  refusing every release of a Node repo as "at None".
- `container-smoke` reports "killed for memory" only for exit 137. Nested
  rootless podman sets OOMKilled on an image that simply exits 1, so a crashing
  image was blamed on memory instead of its own exit code, and self.sh failed.
- This repo's pre-commit passes again: Gourmand's CH012 hits in
  scripts/convert_repo.py and sdlc_jobs/promote.py are path-scoped exceptions
  in gourmand-exceptions.toml, with what each pairs and why it stays.
- The v2 constitution checks close three gaps from #203. K11 catches a vendor
  token (`ghp_`, `github_pat_`, `sk-ant-`, AWS keys, ...) or a private key's
  PEM header in a SKILL.md whatever it is assigned to, using
  `sdlc-secret-scan`'s shapes. S1 requires git to track `uv.lock`, not just
  find it in the checkout. K1 flags a server that turns its HTTP timeout off,
  or uses `requests`/`urllib.request` without ever setting one.
- renovate, pypi, ai, relock and boot ask the engine about AppArmor, as image
  has since #217, and leave no-new-privileges off their nested containers
  where it reports it (#181); `--cap-drop=all` stays. They passed it
  unconditionally, so on an AppArmor runner a process in those containers
  could not signal its own child (a1555).

- sdlc-bootstrap (`boot/sdlc`) resolves `:<major>` in the registry, in the same
  `cosign verify` that checks it, and runs the digest from the verified
  payload. It used to read the digest back from the first `RepoDigests` entry
  after a pull. On a host that had built the release, that entry was the
  build's own manifest digest, never pushed and never signed, so it refused a
  correctly signed `:0` (a1566). Release order was already right: the
  signature lands before any tag moves, and `tests/test_self.sh` now pins that
  order.
- `bin/sdlc-bootstrap`, the launcher a converted repo runs, resolves a tag in
  the registry with the pinned cosign's `triangulate --type=digest`, pulls
  that digest when it is missing, and verifies and runs it (a1569). It read
  the digest from the local copy, so a seat that pulled a tag while it pointed
  at an unsigned digest kept refusing the signed one after every later pull,
  until someone removed the image. Offline, the local copy still stands.

## [0.16.2] - 2026-11-25

### Added

- scripts/local-release.sh: release a fleet repo's default-branch commit from
  Exedore with Actions off, by running the fleet file's check, image, boot and
  promote jobs through the pinned sdlc-bootstrap on a podman service of its
  own, then verifying the pushed digest against the fleet key. Secrets arrive
  on stdin from scripts/local-release-secrets.sh (OpenBao on Gloval) (a1551)

### Fixed

- promote leaves `latest` alone for a newer version only when that version is a
  release of this repo: a `latest` labelled with its base's version (Hummingbird
  python's 3.14.x) no longer holds `latest` back from every release (a1551)

## [0.16.1] - 2026-11-25

### Security

- `secret-scan`, a check in push and ci for every repo, restores the
  plaintext-secret refusal the fleet lost when ci.yml replaced gatehouse's
  `gourmand.yml` (#213): `bin/sdlc-secret-scan` is that workflow's `Refuse
  plaintext secrets` step, rules and `gatehouse: allow-secret` opt-out
  unchanged, and its 90 cases plus planted `ghp_`/`github_pat_` tokens. The
  check job fetches the pull request's base, or the push's `before`, and hands
  it in as `SDLC_DIFF_BASE`.

## [0.16.0] - 2026-11-25

### Removed

- sdlc's own `ashigaru.yml`, `gatehouse.yml`, `gatehouse-retriage.yml`, `gourmand.yml` and `bootstrap.yml`: `ci.yml` replaced them (#212). `container.yml` and `update-tools.yml` stay for now.
- The constitution v1 readers: `sdlc-validate` (`validate`), the launcher's host clone of the pinned constitution (`host_constitution`), the v1 branch of the `constitution` check, and `scripts/cutover.py` and `scripts/fleet_wave.py` now the fleet cutover is done. A repo still carrying `.specify/memory/constitution.md` gets exit 2 from `sdlc-constitution check` (#214).

### Fixed

- `bin/sdlc-bootstrap`: `main` tries the fleet key before sdlc's, each key's verification errors are labelled with the key, and the report no longer needs host `sed`.
- `pytest-in-image` no longer passes its own `no-new-privileges` (a1493). On an
  AppArmor host bin/sdlc drops that option, because it stacks the container
  profile and a process cannot signal its own child (#181), but the check named
  it in its own argv, so it reached podman anyway: claude-agent's tmux tests
  failed every `terminate()` with EPERM under 0.15.9. The launcher alone now
  adds `--cap-drop=all` and the option, where it holds.
- The `image` job's gates (`container-smoke` and every planned `podman run`) and
  its boot gate's containers no longer pass `no-new-privileges` unconditionally
  (a1552). The job drives the runner's engine over the socket, so bin/sdlc's
  AppArmor rule (#181) never saw them: it now asks the engine, as bin/sdlc does,
  and leaves the option off where AppArmor is enforced.

## [0.15.9] - 2026-11-25

### Fixed

- `gourmand` keeps its cache outside a tree it cannot write. 0.15.8 runs
  Gourmand in the `check` job (#182) as the image's non-root user on a
  checkout it cannot write, so every check failed with "Permission denied"
  before a single rule ran.

## [0.15.8] - 2026-11-25

### Fixed

- `docs/design.md` principle 3 now matches constitution 2.0.0 G3: the image
  carries the tools and the v2 rule checks, and a rule change is an sdlc
  release (#200).

### Security

- `ai` runs the model in a sibling container instead of as its own child
  (a1133): no socket, none of the job's environment but the model's token, a
  read-only root, and the checkout writable but for its `.git`. A bare `Write`
  reaches any path, and the job is root on the socket, so a model steered by a
  diff or a comment could have rewritten a binary the job runs next.
  docs/socket-audit.md has the audit of every job.

### Added

- `sdlc-constitution check` makes four more rule checks, ported from the v1
  validator (RT #1666). S1: a Containerfile that installs Python dependencies
  does so with `--no-deps`, from a file it exported from the committed
  `uv.lock` (or a hash-pinned one), or by `uv sync --frozen`. S2: a repo's own
  Renovate config does not change the fleet's release-age cooldown. K1 (kind
  `mcp-server`): a server that makes HTTP requests reads the body in bounded
  pieces against a named ceiling, and a test rejects an oversized body. K11
  (kind `claude-skill`): no `SKILL.md` assigns a credential literal. Each
  finding cites its principle; the rest of K1, S2 and K11 stays with review.
  A file committed as a symlink, or reached through one, is never read.
- tests/test_workflows.py pins the bootstrap's launch line in the fleet file
  and in sdlc's own (or the file it is given): the socket's mode and owner,
  `--user 0` and `label=disable`, the mounts, the exact `--env` list, no other
  option, and the bootstrap by digest with `event` (#178)
- `[tool.sdlc.image] pr-build-paths`: globs of repo paths that make a pull
  request's `check` test-build the image, added to the default inputs (a
  Containerfile, an ignore file, a lockfile). An image that copies `deps/` or
  `rootfs/` in now gets its container gates on a pull request that changes only
  those (#186). Patterns are anchored at the repo root (`**` crosses
  directories); an absolute path, `..`, or more than 50 is refused with exit 2.
- A credential-free `boot` job in the fleet file, between `image` and `promote`
  (#118). It runs a repo's `boot-command` (with `boot = "command"`) and
  `contract-test` as `bash -euo pipefail -c` with `IMAGE=<ref>@<digest>`, and
  `free-disk` on a GitHub-hosted runner, against the digest `image` pushed. It
  holds `contents: read` and `GITHUB_TOKEN` alone and refuses to start with a
  secret in its environment. The shell runs in a sibling container of the job's
  own image, started through the runner's socket and removed after each shell
  (timeout included): the workspace is mounted at its runner path as the working
  directory, `TMPDIR` and `HOME` are under it, and the socket and the runner's
  network are there, so a `-v` source, `mktemp` and `127.0.0.1` mean what they
  did under container-release. Its environment is a short list of the run's
  variables and what is set for it, never the job's: no `GITHUB_TOKEN`, OIDC or
  `ACTIONS_*`. The pull and the container go through podman-remote on the
  runner's host podman socket, as every job now does. It never runs on a pull
  request, since `image` does not. A non-zero exit or `boot-timeout` fails it, and `promote` needs it when
  `check`'s plan says `boot` and the fleet file maps `SDLC_BOOT`, so nothing is
  signed or tagged on a failed boot proof. `image` pushes `gated-<sha>` before
  `boot` judges it, so for a repo that sets these keys the registry can hold an
  ungated digest. The three keys are no longer "not enforced", and
  `tests/test_job_boot.py` and `tests/test_workflows.py` pin it. `boot-command`
  under a `boot` other than `command` is no longer a 2: it prints a warning and
  is not run; `boot = "command"` without `boot-command` is still a 2.
- `sdlc sarif`: the ci checks that write SARIF (CodeQL, semgrep), run with an
  output directory the CI job mounts (`SDLC_HOST_SARIF`, `/sarif` inside). The
  fleet ci.yml's `check` runs it on every default-branch push and on the weekly
  schedule, and uploads what it wrote to code scanning: the Security tab updates
  again, and the weekly run rescans CodeQL and semgrep as security.yml did (its
  verdict is the weekly run's). `sdlc ci`, which runs the repo's own code, never
  gets the mount, and no token reaches a nested container. Under `sdlc sarif`
  the semgrep rule tests (repo shell) do not run, and the run gets no cache
  volume: CodeQL builds its database and compiles its queries under `/tmp`. The
  planner lists the checks it ran, and a planned check that wrote no SARIF is a
  scan that did not complete. On the weekly run that, or an upload code scanning
  refused, is the job's 2. On a push, a scan that did not complete is an
  `::error::`, not the job's 2, so a report cannot hold a release the gates
  passed, and a refused upload is a warning. A repo where code scanning is not
  offered at all (a private one without it answers 403) only warns, on either.

### Rollout

1. Release the sdlc image. Under the old fleet file nothing changes: there is no
   `boot` job and no `SDLC_BOOT`, so `promote` does not require boot and the three
   keys stay unenforced, as in 0.15. `boot-command` under a non-command profile
   warns.
2. Roll the new `examples/ci.yml` (with the `boot` job) to each repo. From then
   `boot` is required for a repo that sets the keys.
3. After every repo is on the new fleet file, `boot-command` under a non-command
   profile can become a 2.

The reverse order is safe too: a repo on the new fleet file whose image is still
0.15 gets no `boot` from `check`'s plan, so the `boot` job is skipped and
`promote`, which admits a skipped `boot` when the plan did not ask for it, passes.
Until step 2 the image job says the three keys are not enforced.

- `tests/test_apparmor.sh`, run by the contract test: rootless, and rootful
  through `sudo -n`, a gate container kills its own child. On an AppArmor host,
  systemd also boots and stops a unit, and the log says whether
  no-new-privileges still blocks the kill. `tests/test_host_contract.sh` pins
  the gate, smoke, systemd and host-run invocations with AppArmor on and off.

### Changed

- sdlc itself is on constitution v2: `[tool.sdlc] repo-kinds = ["cli-tool",
  "container-image"]` in pyproject.toml, `.specify/` deleted, and its rationale
  moved to docs/design.md. `.github/workflows/constitution.yml` (the v1
  validator) goes with it; `sdlc / check` runs the v2 check in `sdlc ci` (#198)

- Where podman reports AppArmor, gate containers drop no-new-privileges and keep
  the default AppArmor profile enforced. Before, they ran AppArmor-unconfined
  (0.15.4). The cause, measured on Ubuntu 26.04.1 with rootful podman 5.7.0 and
  crun 1.21: under no-new-privileges, AppArmor stacks the container profile onto
  crun's (`containers-default-0.66.0//&crun`). The default profile does not name
  that label as a signal peer, so a process could not kill its own child and
  systemd as PID 1 never left `initializing`. Without no-new-privileges the
  profile is applied as is, and both work. Rootless podman reports no AppArmor,
  so it is unaffected; SELinux hosts keep no-new-privileges (#181).

### Fixed

- `scripts/hotfix.sh` tags vX.Y.Z through the Ashigaru App, as `write` tags a
  release merge (sdlc#191, a1383). Tag ruleset 27841014 lets only the App
  create v*.*.* tags, so 0.15.2 through 0.15.7 shipped untagged. Between the
  image and sign steps, hotfix.sh signs the App's JWT with openssl and mints
  `write`'s TAG token for sdlc alone; only that token, on stdin, reaches the
  image just built, where `sdlc_jobs/_hotfix.py` runs Ashigaru's
  `release_publish`. The token is revoked on exit. A tag already at the commit
  is a re-run, one at another commit a refusal. The stdin JSON now also
  carries `ASHIGARU_CLIENT_ID` and `ASHIGARU_APP_KEY`, and the host needs
  openssl and curl.

- `write` no longer runs Ashigaru's relock in its own process (#195). uv
  executes the interpreter a branch's .python-version names, so `uv lock
  --no-build` ran branch content beside the runner's socket and
  ASHIGARU_APP_KEY. The scripts' PATH now starts with sdlc's `uv`, which locks
  in a sibling on the job's image by digest: no socket, no secret, all
  capabilities dropped, CPU, memory and process limits, a read-only root, its
  own network, the checkout read-only and copied without .git. Only uv.lock
  comes back, and only when it parses as a uv lock. Before anything runs it
  refuses a staged .python-version, uv.toml, pyproject `[tool.uv]` or mode
  change, and any command but `lock --no-build`. write's scratch dir moves
  under RUNNER_TEMP so the sibling can mount it.

- Gourmand runs in the fleet again (#177). The cutover deleted every repo's
  `gourmand.yml`, and `sdlc ci` leaves Gourmand out (constitution XII), so
  nothing ran it. The `check` job now runs the launcher's `gourmand` after
  `sdlc ci` wherever it runs the gates, and the worse of the two exits is the
  job's: a Gourmand finding fails `sdlc / check`, as `Code Quality (Gourmand)`
  did. A repo that skips `gourmand` in `[tool.sdlc.push]` is not reviewed here
  either. No ci.yml changes.
- `bin/sdlc-bootstrap` trusts sdlc's own release key (`boot/keys/sdlc.pub`,
  inlined) for quay.io/crunchtools/sdlc, so local hooks verify `:latest` again:
  every release since 0.15.2 is signed by that key alone, and the launcher
  refused them all (#185). A release (`latest`, `X.Y.Z`) is accepted on sdlc's
  key only, because every fleet repo's promote job holds the fleet key; `main`,
  which container.yml still signs with the fleet key, and an `@sha256` digest
  are accepted on either. 0.15.1 and older are fleet-signed, so they run by
  digest, not by tag. `scripts/sign-digest.sh` and `scripts/attest-digest.sh`
  read the fleet key out of the launcher by name.
- A release PR carries the version in uv.lock too (#187). The `write` job's
  dispatch copied `chore/release-X.Y.Z` as prepared, so a branch that moved
  pyproject.toml and not uv.lock (gatehouse 0.34.0) failed `uv sync --locked`.
  It now commits uv.lock's root package at the version onto
  `ashigaru/release-X.Y.Z`, as the App, before opening the PR. The root is the
  entry locked from the checkout (`source = { editable = "." }` or
  `{ virtual = "." }`), never matched by name, so `gatehouse-crunchtools` in
  repo `gatehouse` is found. A root with a dynamic version, which uv locks
  with no version line, is left alone.

## [0.15.7] - 2026-11-25

### Fixed

- The ai job reviews a pull request's net diff, not its per-commit patches. In
  the patch form, a file one commit adds and a later one removes, and every
  intermediate line number, look like part of the PR; Gatehouse anchored
  findings there and GitHub refused the whole review ("Line could not be
  resolved", gatehouse#173), so the write job failed and the gate held the PR.
- `bin/sdlc-bootstrap` reports a failed `podman image inspect` with podman's own
  error, instead of "has no sha256 digest to verify". It still fails closed.

## [0.15.6] - 2026-11-25

### Fixed

- The image installs nodejs24-full-i18n. Hummingbird's node ships without full
  ICU data, and without it `new Intl.Segmenter()` segfaults node, which crashed
  vitest workers in the node stack's npm-test. The build now proves Segmenter works.

## [0.15.5] - 2026-11-25

### Fixed

- The gate container marks /repo safe for git. On a CI runner the checkout belongs to
  the runner's user, not the container's, so a test that runs git failed with
  "dubious ownership" (exit 128).

## [0.15.4] - 2026-11-25

### Fixed

- Gate containers run AppArmor-unconfined where podman reports AppArmor. Ubuntu
  26.04 labels crun's processes `//&crun`, and the default container profile then
  refuses signals between a container's own processes, so a test that kills a
  child it spawned failed with EPERM.

## [0.15.3] - 2026-11-25

### Changed

- One image build per release. A merge to the default branch builds no image unless
  it is a release merge; the weekly and parent rebuilds run only when a release tag
  names the commit, so unreleased code is never published. A pull request runs the
  container gates (a throwaway build, never pushed) only when it changes a
  Containerfile, a `.containerignore` or a lockfile; other pull requests skip them.
- The fleet file runs sdlc on the runner's own podman: no `container:` job, no
  Docker. Each job starts the runner's rootful podman socket and runs sdlc-bootstrap
  with the job's work directory mounted at the same path, so GitHub's runners and
  Exedore's run the same engine the same way.

### Removed

- Build-once candidates: `quay.io/crunchtools/candidates`, the OIDC-federated robot,
  the pull request's image job and the merge's adoption of its digest. quay.io offers
  no robot federation to this org, so no candidate was ever pushed.

## [0.15.2] - 2026-11-25

### Fixed

- The fleet file's gates run on runners whose engine is podman behind a Docker
  API (the Exedore runner VMs). boot's sibling of the job container lists no
  mounts of its own there, only `HostConfig.VolumesFrom`, so the workspace is
  now mapped through the job container's mounts; and podman records the tag,
  not the digest, as the image name, so the launcher's image is pinned again
  from `ImageDigest`. Before, every `sdlc / check` there stopped with "the
  workspace ... is not a mount of this container" or "not pinned by digest".

### Added

- `scripts/hotfix.sh`: release a pushed, version-bumped tree from a host in
  minutes, by running `self.sh`'s own image and sign steps with sdlc's key and
  robot read from stdin. Every check of the pipeline's release still applies.

## [0.15.1] - 2026-11-25

### Fixed

- `sdlc event`, `check`, `image` and `pypi` no longer fail with git's "dubious
  ownership" error when the runner's mounted workspace is owned by another uid:
  every git call sdlc makes there (fetch, clone, `rev-parse`, and the launcher's
  git) marks that one tree as a `safe.directory` through the environment, never
  `*` (#157).
- `image` builds, smoke-tests and scans an image whose checks `sdlc plan ci`
  leaves out: an RHSM build (skipped in ci, where there are no subscription
  secrets) or a python repo naming no container stack. It plans
  `container-build`, `container-smoke` and `container-scan` by name (for a bootc
  image, `container-build`, `container-scan` and `bootc-lint`), which ignores ci's
  skip and stacks, instead of failing with "plans no container-build".

## [0.15.0] - 2026-11-24

### Fixed

- `sdlc push` no longer dies with "sdlc-constitution is not a check this image
  runs itself": the image's entrypoint is `sdlc`, so the gatehouse step now
  runs `sdlc-constitution` as the entrypoint instead of as an argument to it.

### Added

- `[tool.sdlc.image] publish = false`: a repo whose root Containerfile is a
  reference image publishes nothing. `check` plans no image, no candidate and
  no parent poll for it, and the container gates still run in `sdlc ci`.
- More than one image per repo, with no change to ci.yml: each
  `[[tool.sdlc.images]]` entry (`name`, `containerfile`, optional `context`,
  and the `[tool.sdlc.image]` settings for that image) is built, gated, pushed
  and promoted by the same `image` and `promote` jobs. An image built FROM a
  sibling is built after it, on the digest this run pushed; the weekly and
  parent-poll rebuilds rebuild every image, so a moved base cascades. A name
  must be one `EXTRA_IMAGES` in `sdlc_jobs/_core.py` gives that repo
  (rotv-base, rotv-dev, images-rotv; acquacotta-base; trentina-demo):
  reviewed here, never by the repo that benefits.
- A candidate is keyed by the image it publishes as,
  `candidates:<image>-<tree>`, one annotation per image; a repo with one image
  keeps the key it had. An image FROM a sibling the merge rebuilt (its
  candidate was gone) is rebuilt on it too, never adopted on the old parent.
- `containerfile-lint` lints every `[[tool.sdlc.images]]` Containerfile with
  the root one, and `sdlc` accepts the `images` key.
- The `command` kind: a repo's own test as an argv list, run in a nested
  container with the repo read-only at `/repo`, no capabilities and no network
  unless `network = true`. It runs in the gate image by default, or in any
  `image`, including one an `image-build` check in the same command tags, which
  it then follows in that build's lane. It is how a repo's extra workflows
  (node test files, a lint script, pytest on another Python, browser tests)
  move into `[tool.sdlc.ci]` and run the same under `sdlc ci` locally and in CI.
  Never a shell string: `argv[0]` is the entrypoint and the rest its arguments.
- `command` with `systemd = true`: the test runs against the image booted with
  systemd as PID 1. The launcher polls a `ready` argv for up to `timeout`
  seconds, execs each `setup` argv and then `argv` in order, and removes the
  container however that ended (conmon's `--timeout` and `--rm` catch a gate
  killed outright). systemd gets back `SETUID`, `SETGID`, `SETPCAP`, `CHOWN`,
  `FOWNER`, `DAC_OVERRIDE`, `KILL` and `NET_BIND_SERVICE`, keeps
  `no-new-privileges`, and has no network unless asked; on Actions, workflow
  commands stay stopped while it runs. A hung probe ends at `timeout`, and the
  setup steps and the test share a 30-minute budget, past which the step is a
  finding and the container is removed. A cancelled gate (INT, TERM, HUP)
  removes the container before it exits. Each container's name carries its
  lane and a random suffix, so two lanes starting in the same second cannot
  share a name and remove each other's container. The plan carries it as a new `host-systemd` mode whose run and exec
  options `bin/sdlc` checks word by word. This is what lets a repo whose
  backend tests need its database and API up drop its own test workflow.

### Fixed

- `[tool.sdlc.image] build-args` now choose the parent that is verified and
  pinned, as they choose it in the build: rotv's `BASE_IMAGE` was verified and
  pinned as the Containerfile's default, not as rotv-base.
- A parent another repo publishes as an extra image (rotv-base, say) is
  verified as signed by that repo, which is how promote signs it.
- sdlc's next ci.yml (`examples/sdlc-ci.yml`) runs its `test` job on the
  uncached runner class, as the live one does, never the cached class.
- A candidate annotation that is not a JSON object is reported in the log
  before it is ignored, rather than silently.
- `bin/sdlc` no longer expands glob patterns on the host. A plan's argv words
  were split unquoted, so a repo's `"/home/*/.ssh/*"` was matched against the
  runner's own filesystem before podman saw it. Pathname expansion is now off
  for the whole script.
- A plain `host` step (an image build, `command` without systemd, a smoke
  run) now stops workflow commands on Actions while it runs, as a host-systemd
  step already did. Repo code's output could set an output or add a path.
- An INT, TERM or HUP now ends `bin/sdlc` with 2 after its cleanup, instead of
  running the cleanup and carrying on with the next check.
- A `pytest-in-image` or `command` listed before the `image-build` that tags
  its image ran first, in the shared lane, against an image that did not exist
  yet (podman exit 125). The plan now puts each build ahead of the checks that
  run its image; every other check keeps the order `checks` gives it.
- `scripts/convert_repo.py` no longer writes container-release's
  `default-branch` into `[tool.sdlc.image]`, which the image job refuses. Every
  job reads the default branch from the event; a converted repo that already
  has the key should delete it.

## [0.14.1] - 2026-11-24

### Fixed

- Only a public repo builds a candidate: `quay.io/crunchtools/candidates` is
  public, so a private repo's pull request builds at merge, as before.

## [0.14.0] - 2026-11-24

### Added

- `sdlc-constitution check` enforces R4 on the newest release in
  CHANGELOG.md: a `### Removed` entry needs a breaking bump (MAJOR, or MINOR
  before 1.0), and every `### Deprecated` entry says which later
  release removes it ("removed in 2.0.0"); a bare "since 1.2.0" does not count. Older releases are history and are not judged. Whether a removal names
  its replacement stays a review question.

- Build once, promote on merge (RT #1668). A same-repo pull request's `image`
  job builds, gates and pushes the release image to
  `quay.io/crunchtools/candidates:<repo>-<tree>` with a GitHub OIDC token
  exchanged for the `crunchtools+candidates` Quay robot; it gets no secret.
  The pull request's `check` leaves the container gates to it. On the merge,
  `check` skips gates its pull request already passed on the same tree, and
  `image` copies the digest GitHub recorded for the pull request's image
  job instead of building; `promote` accepts it by its tree label. No candidate, a fork, or an RHSM build: the
  merge builds as before.

### Fixed

- `sdlc / gate` counts an answer to a Gatehouse finding only from an owner,
  member or collaborator (constitution C3). A PR author outside the org could
  clear a finding by replying `not a bug: ...` to it.

## [0.13.0] - 2026-11-24

### Added

- Constitution 2.0.0 (RT #1666). A repo without a
  `.specify/memory/constitution.md` is judged by `sdlc-constitution check`
  against the 2.0.0 text baked into the image; it names what it is with
  `[tool.sdlc] repo-kinds`, and every finding cites a principle ID. A repo
  that keeps its manifest gets the v1 validator, unchanged. Gatehouse reviews
  a v2 repo against the image's text plus the README's "What's different
  here" section. `sdlc-constitution coverage` lists how each principle is
  checked: by rule, by review, or not yet.

### Changed

- Public repos run on GitHub's machines again; private repos run on Exedore.
  The runner variables are scoped to private repos, and every `runs-on` keeps
  its `ubuntu-latest` fallback. Measured on the same sdlc check, GitHub's
  runner took 14 minutes and an Exedore VM more than 17. GitHub's minutes are
  free for public repos, and a fork's code stays off our network. sdlc's own
  `image`, `approve` and `sign` follow the same rule instead of a hardcoded
  `ubuntu-latest`.

### Fixed

- `sdlc push`'s Gatehouse verdict is CI's. Gatehouse exits non-zero only on a
  finding every sample blocked on, but the fleet's `gate` holds a pull request
  for every CRITICAL, HIGH and MEDIUM finding it posts, and for Bug Hunter's
  and Security Scan's LOWs. A push could pass locally and fail CI on the same
  finding. `t_gatehouse` now reads `--consensus-json` and fails on those, so
  each is fixed or judged in gatehouse-findings.toml before CI runs. The rule
  lives once, in `sdlc_jobs/_findings.py`: `ai` passes its agents to
  Gatehouse, `gate` judges posted findings by it, and `sdlc push` judges
  local ones by it, so the three cannot drift apart.

## [0.12.4] - 2026-11-24

### Fixed

- `self.sh` read Quay's answer for a deleted tag ("Tag 0 was deleted or has
  expired") as a registry error, so the bootstrap refused to run after `:0`
  was deleted. It is an absent tag, like `manifest unknown`.

### Changed

- sdlc releases only through its own `ci.yml`: a prepared
  `chore/release-X.Y.Z` branch, a dispatch with `version`, the App's release
  PR, then `image` and `sign`, which moves `:0`. `release.yml` is deleted;
  `container.yml` stays until the constitution validator knows sdlc's own
  release path, and still moves `X.Y.Z` and `latest`, which the fleet does
  not read. 0.12.2 and 0.12.3 went out through them, so
  they were signed with the fleet key and `:0`, which sdlc-bootstrap
  resolves, stayed at 0.12.1. This is the first release the fleet receives
  since 0.12.1, with every fix listed under 0.12.3. It goes out by the
  one-time bootstrap dispatch, because 0.12.1's `write` refuses the App's
  release merge.

## [0.12.3] - 2026-11-24

### Fixed

- `sdlc event`'s check job works against a runner's container socket
  (RT #1664). Job containers drive the runner's engine through the mounted
  socket, and four things broke on a real runner:
  - The image had no `podman` that speaks to a socket. `podman` is now a shim
    to `podman-remote --url $CONTAINER_HOST`; it drops the local-only
    `--remote` and `--events-backend` flags.
  - Host paths were wrong. The launcher's cache now lives under
    `RUNNER_TEMP` and is passed to nested containers by its host path
    (`SDLC_HOST_CACHE`), the same way the workspace is.
  - The job container found itself by `HOSTNAME`, which `docker exec` does
    not export and a runner may set to its own name. It now reads its id from
    the engine's `/etc/hostname` mount in `/proc/self/mountinfo`.
  - There was no git tree, because the fleet file has no `actions/checkout`.
    The check job fetches the event's commit at depth 1 with the job token
    passed as a per-command header, so nothing is written to `.git/config`.
  - `gawk` is in the image for the launcher's scripts.

  Proven on Exedore's gha-runner VMs with an sdlc-dev build.
- Every fleet job ran end to end in a sandbox repo (crunchtools/sdlc-dev):
  pull request, finding reply, issue triage and auto-code, push, and release.
  That surfaced four more bugs:
  - `gate`, `release` and `verify_release` trusted `ashigaru-crunchtools[bot]`.
    The App's slug is `ashigaru-crunchtools-com`, so `gate` refused every
    Gatehouse review the App posted: "no review of it was posted".
  - `image` and `pypi` never fetched the commit, so `image` found "no
    Containerfile". `fetch_tree` moved to `_core` and runs first in `check`,
    `image` and `pypi`.
  - A streamed `podman build` returns no captured output, and `show()` raised
    `TypeError` adding the two `None`s after a successful build.
  - The converter copied container-release's `cascade` into
    `[tool.sdlc.image]`, which `image` refuses. It's dropped now: every
    default-branch push already moves `latest`.

### Changed

- The fleet file's `gate` job runs on `crunchtools-uncached` when the org's
  runner label says so, like the other trusted jobs. It runs no repository
  code, so a fork's PR is included.

## [0.12.2] - 2026-11-23

### Changed

- sdlc-bootstrap base core-runtime:2-builder sha256:3d625fad86d7 -> sha256:3f42474b6433.

### Changed

- sdlc's own CI fetch adds an `origin` remote before fetching. Without one
  the constitution validator names the repo after its mount point, `/repo`,
  so sdlc's own exemptions (`sdlc_exempt`, `container_release_custom`) never
  applied and `sdlc ci` failed the constitution check.
- `convert_repo.py` takes the fleet's two Gatehouse variants as the template:
  the guard on `ubuntu-latest`, and triage handed `review_result`. A publish
  job may set up Python with `uv python install`.
- The fleet file, the doc and sdlc's own file pin
  `quay.io/crunchtools/sdlc-bootstrap@sha256:5f25b796…de060`, sdlc-bootstrap
  0.12.1, signed with sdlc's key by the bootstrap run. `cutover.py` and
  `fleet_wave.py --apply` no longer refuse.
- sdlc's own `.github/workflows/ci.yml` is installed, beside the old workflows
  until the rulesets move. `test` runs on the cached runner for a same-repo PR
  and on GitHub's for a fork. The bootstrap wiring is pinned in
  `tests/test_workflows.py` (#128).
- Every fleet secret stays an org secret: the fleet file names no `ai`,
  `ashigaru`, `registry` or `cosign-signing` environment, so no repo needs
  any setup to build, sign or review. The control against a branch workflow
  reading a secret is the workflows permission, held by Prime's credential
  alone. `cutover.py` no longer takes repos off the org secrets' lists, and
  `--check` no longer looks for moved secrets. sdlc alone keeps its
  `registry`, `sdlc-signing` and `sdlc-approval`.
- `cutover.py` and `fleet_wave.py` list the org's repos with a GraphQL query for
  name, privacy, archived and default branch alone. The REST listing carries
  each repo's description, free text a gateway route may withhold. The same
  goes for the merge's check of the required runs: GraphQL, for id, name,
  conclusion and app only, which also needs no Checks permission (fine-grained
  tokens cannot hold it).
- `cutover.py` and `fleet_wave.py` take `--api-url`, so their GitHub calls can
  go through a gateway route that adds its own credential.

## [0.12.1] - 2026-11-23

### Fixed

- `scripts/self.sh` makes the registry login it mounts for cosign readable by
  cosign's nonroot user. `podman login` writes it 0600, so every cosign call
  failed: the 0.12.0 bootstrap's sign job could not sign, and a verify read as
  "not signed". The login sits in mktemp's 0700 directory, so nothing else on
  the host can reach it.

## [0.12.0] - 2026-11-23

### Added

- `.github/workflows/bootstrap.yml`, the one-time dispatch that cuts this
  release under sdlc's own key (MODE=bootstrap). The PR that installs sdlc's
  own ci.yml deletes it.
- sdlc-bootstrap now trusts sdlc's own cosign key (`boot/keys/sdlc.pub`); the private half lives only in the `sdlc-signing` environment.
- sdlc-boot is renamed sdlc-bootstrap: the image is
  `quay.io/crunchtools/sdlc-bootstrap`, and its signature annotation
  `repo=crunchtools/sdlc-bootstrap`.
- `scripts/self.sh` gains MODE=bootstrap: sdlc's first release, once, by an
  approved dispatch with `version` and `bootstrap: true`, of the `vX.Y.Z` tag
  at the dispatched commit, while no `:<major>` exists. It breaks the loop in
  which the release path's `write` runs in sdlc-bootstrap, which needs a
  release to verify.

- The fleet's self-hosted Renovate runs from this image (a1099). `renovate` is
  installed by `npm ci --ignore-scripts` from a committed lockfile,
  `npm/renovate/`, at `RENOVATE_VERSION` (44.221.0, the version the
  constitution validates its config with; the build fails if they differ), and
  `sdlc versions` and the contract test report it. The fleet file gains an eighth job, `renovate`: in
  crunchtools/constitution only, the weekly cron is the real run and a bare
  dispatch from the default branch a dry run. It holds the Renovate App key from
  the `renovate` environment and nothing else, no `GITHUB_TOKEN` included, and
  runs on the secret jobs' guarded runner (`*secret`, a1100), for at most 55
  minutes, inside the minted token's hour.
  `sdlc_jobs/renovate.py` reads the key and drops it from its environment,
  mints the App's org-wide token in-process (`_app_token.org_token`), clones
  the constitution at the run's sha for `renovate/self-hosted.json`, and runs
  Renovate in a nested container from its own image by digest: no socket, no
  `--volumes-from`, every capability dropped, a read-only root, and only the
  RENOVATE_* variables, never the key. The container has a unique name and
  podman's own 50-minute limit; SIGTERM and SIGINT raise, so on every path,
  a cancel included, it is removed and the token revoked. It writes the step
  summary the old workflow did. The constitution's `renovate.yml` is retired.
- The `image` job enforces container-release's boot gate from
  `[tool.sdlc.image]` before it pushes (#110, a1108). It runs `boot` = `mcp`
  (the stdio handshake and tools/list), `mcp-http` (the same over streamable HTTP,
  from a client in the server's network namespace), `systemd`, `healthcheck` and
  `none`, and honours `boot-env`, `boot-cmd`, `boot-units`, `boot-port`,
  `boot-path`, `boot-timeout` and `boot-none-reason`. A failed boot pushes
  nothing, and podman's own failure (exit 125) is a 2, not a verdict. Every boot
  container drops all capabilities and runs with no-new-privileges and no
  network, as the other gates do; systemd gets podman's default capabilities
  back by name. `boot-run-args` may only hold `--env`, `--tmpfs`, `--read-only`,
  `--user`, `--workdir`, `--entrypoint`, `--shm-size` and a read-only or overlay
  volume from the checkout, without relabelling, whose path is walked on disk
  and refused if any part is a symlink. The job holds the registry secrets and
  the runner's socket. `build-args` reach the build as `--build-arg`.
- `boot = "command"`, `boot-command`, `contract-test` and `free-disk` are still
  accepted and printed as not enforced. They are repo shell or the host's disk,
  and they need a job of their own that holds no secret (#118).

### Security

- Dependency code that Renovate's package managers run can no longer reach a
  token for the whole org (sdlc#121, a1142). Renovate hands its token to that
  code on purpose, as `GIT_CONFIG_VALUE_n` for uv, poetry, pdm and gomod and in
  each clone's origin URL, and no option turns it off; and the code could read
  `/proc/<renovate>/environ` as the same uid. So `renovate` now lists the
  installation's repos itself, with a metadata-only token that never enters a
  container, matching `autodiscoverFilter`'s plain owner/globs as Renovate
  would (anything else is refused), and runs each repo in a container of its
  own with a token minted for that repo alone and revoked when it is removed.
  A repo that fails no longer hides the rest: each is run and the failures are
  named. Renovate is started by `/usr/local/libexec/renovate-node`, node at
  mode 0111, so the kernel marks it not dumpable: its children cannot read its
  environ or mem, or ptrace it. The contract test proves that against the
  built image, with plain node as the control.

### Fixed

- `scripts/cutover.py` raises no CodeQL `py/clear-text-logging-sensitive-data` (a1140).
  `MOVED_SECRETS` is now `MOVED_TO_ENVIRONMENTS`: the query's name heuristic read
  `*_SECRETS` as secret values, so every problem line naming a moved secret was
  flagged. Only the names are printed, never a value.
- The `image` job trusts a `quay.io/hummingbird/<name>` parent (a1128). That
  registry is Hummingbird's unsigned mirror, so the job verifies the same
  digest at `registry.access.redhat.com/hi/<name>` with Red Hat's release key
  and builds from the mirror by that digest. A mirror digest Red Hat has not
  signed, `quay.io/hummingbird-community` and any other registry still refuse.
  A tag-only `FROM` must name the same digest at `hi/<name>:<tag>` as on the
  mirror, so a stale mirror cannot roll a build back to an older signed digest.
- The Red Hat verification policy now rejects by default, so only the listed
  Red Hat registries ever pass it.
- The `image` job resolves a `name:tag@sha256:` parent by its digest alone.
  skopeo refuses a reference with both, so every tag-and-digest `FROM` was a 2.

### Changed

- The fleet file's secret jobs (`ai`, `image`, `write`, `promote`, `pypi`) run on
  Exedore's uncached class, `crunchtools-uncached`, instead of `ubuntu-latest`
  (a1100): scratch storage emptied every job, the warm image store read-only.
  One shared `runs-on` takes that label only when `vars.UNCACHED_RUNNER_LABEL`
  is exactly it, so the variable is a kill switch that can never name the
  cached class. A fork's `pull_request_target` (or a deleted fork's) and an
  issue from anyone but an owner, member or collaborator stay on
  `ubuntu-latest`. The
  design doc's runner rule says so, and `tests/test_workflows.py` pins the routes.
- `docs/marketplace-free-ci.md` takes spike #103's findings (a1118): a skipped
  job's check carries its `name:` literally, so no skipped job matches a
  required name; the image and `sdlc-boot` drive the socket with the docker
  CLI, since `podman --remote` fails against the Docker 29 socket; an Exedore
  build's `RUN` steps cannot be `--network=none` under chroot isolation; and a
  450 KB variable cannot exec, so `ai`'s result stays four chunks.
- `examples/ci.yml` is the fleet file of `docs/marketplace-free-ci.md` (a1051):
  one workflow, no `uses:`, seven trust-level jobs each running `sdlc event` in
  `sdlc-boot`. `tests/test_workflows.py` pins its guards (`!inputs.digest` on
  `write`, `check`'s success before acting on its plan, the registry.redhat.io
  secrets on `image`, sdlc's own `test` and shared jobs).
- `scripts/convert_repo.py` writes that file and deletes every other workflow
  whose every job `sdlc event` runs; any other workflow refuses the repo (exit 2),
  named, for `[tool.sdlc.commands]` by hand. A PyPI publish becomes
  `publish = "pypi"`, a `container-release.yml` call's inputs `[tool.sdlc.image]`.
  Runner lifting, step floating and mixed workflows are gone.
- `scripts/cutover.py` sheds signing, the weekly schedule, the release flow and
  the Ashigaru caller; it moves `Inherits:` and the hook `rev:`, drops Dependabot,
  removes `.gemini/` on request, and reports any `uses:` left.
- `scripts/fleet_wave.py` refuses private repos without `--include-private`, opens
  the pull request without auto-merge, and drops `--constitution-ref` and `--weekly`.
- `check`, `approve` and `sign` (and the fleet template's `check`) run on a
  `workflow_dispatch` from the default branch only.
- Inherits constitution v1.60.0. The pre-commit Gourmand and Gatehouse hooks float
  (`:main`, `:latest`) with `--pull=newer`, and the Dependabot config and its
  automerge workflow are gone (Renovate, XV). With `self.sh` naming the fetch's
  origin, `sdlc ci` on sdlc itself passes the constitution check (#105).

### Added

- `sdlc event`: the one command each job of the fleet's single `ci.yml` runs. It
  routes on `GITHUB_JOB` to `sdlc_jobs/<job>.py`; a job with no module is a 2
  naming it, and `write` with `DIGEST` set (sdlc's rollback dispatch) does nothing.
- The `check`, `image`, `promote` and `pypi` jobs (docs/marketplace-free-ci.md):
  `check` gates through the launcher pinned to its own image and writes the plan;
  `image` verifies the parent, builds FROM its digest, gates and pushes
  `gated-<sha>`; `promote` signs, attests and moves tags without ever moving a
  version backwards; `pypi` re-verifies the release and publishes by OIDC.
- The `image` job reads `[tool.sdlc.image]` (#107's contract): `image` may only
  restate the repo's own quay.io/crunchtools/<repo>, `rhsm` and
  `redhat-registry` make their secrets required, `format` overrides docker/oci. `containerfile`/`context` other
  than the root are refused for `[tool.sdlc.ci.container-build]`; the boot gate's
  inputs are accepted and printed as not enforced yet (#110).
- `sdlc pip-audit -- --report` prints pip-audit's JSON report on one
  `SDLC-AUDIT-REPORT` line. The weekly `check` hands it on as `audit`, clean or
  not, so `write`'s `audit_alert` can also close the issue; `release` is
  `{version, publish}`.
- `bin/sdlc` honours `SDLC_HOST_REPO`, the repo's path on the machine podman runs
  on, for a job container that drives the runner's socket.
- `cutover.py --merge REPO --pr N`: refuses a busy repo (a person's open PR, an old
  `ashigaru/release-*`), an unrecorded PyPI publisher swap (`--pypi-ledger`), a
  private repo, org secrets not scoped to selected repositories, or red new checks;
  then merges by admin bypass, moves the rulesets idempotently, drops the repo from
  each moved org secret and updates bot PRs. `--move-rulesets` and `--check`
  (moved secrets still readable, rulesets out of step, no `ashigaru/release-*` guard).
- `sdlc_jobs/`: the `ai`, `write` and `gate` jobs of the one-file ci.yml
  (docs/marketplace-free-ci.md), each a `main(event_name, event, env)` that
  `sdlc event` calls. `ai` runs Gatehouse's review (its own CLI, with what it
  would post recorded rather than posted) and Ashigaru's triage, code and fix
  agents with model keys and a read token, and hands the result to `write` in
  four outputs of at most 112,500 bytes. `write` mints an Ashigaru App token
  per step and posts the review, applies the fix round, triage and issue PR
  through Ashigaru's own scripts, opens the release PR, verifies and tags a
  release merge, and keeps the audit alert issue; it exits at once on a
  dispatch carrying `DIGEST`. `gate` is the required check: the fork workflow
  guard, `ai` and `write` succeeded, and a port of Gatehouse's triage that
  counts only the App's review; a collaborator's reply re-runs it in the
  `pull_request_target` run.
- The image half of the marketplace-free CI (a1088, docs/marketplace-free-ci.md).
  The image gains podman-remote, skopeo, the docker CLI, cosign 2.6.5, syft 1.57.0,
  zizmor 1.32.0, the claude CLI 2.1.323 (auto-updater off), gh 2.104.0, and PyJWT
  2.15.1 with cryptography 50.0.2 in the interpreter `sdlc` runs under, each an ARG
  pin with a label and a contract test. For `sdlc event`'s handlers it also carries
  Ashigaru 5.4.0 (its release commit, tag pending)'s scripts, prompts and schemas at `/opt/ashigaru`, the constitution
  v1.60.0's scripts at `/opt/constitution/scripts`, and `sdlc_jobs` on the path.
- `sdlc-boot` (`boot/`): the fleet's trust root. It resolves
  `quay.io/crunchtools/sdlc:<major>`, verifies it against sdlc's own key and a baked
  minimum version, and runs it by digest beside the job container. Every refusal
  is a 2. Built and smoke-tested by container.yml; nothing publishes it yet.
- `scripts/self.sh`: the test, check, image and sign steps of sdlc's own new
  ci.yml, which waits in `examples/sdlc-ci.yml` until the cutover. sign runs cosign
  from a digest-pinned image and never runs the image it signs. The weekly
  rebuild fetches the commit `:<major>`'s revision label names, never the movable
  tag, and sign refuses a rebuild of any other revision. sdlc-boot's base is pinned
  by digest (moved by `scripts/update_tools.py`), its policy is baked as files that
  no job env can override, and Ashigaru and the constitution are fetched by commit.

### Fixed

- The CodeQL bundle is checksum-verified before it is extracted (sdlc#111,
  a1112). The build fetched the full bundle's checksum for the Python bundle,
  ignored a failed fetch and never compared it; the Python bundle's sha256 is
  now pinned as `CODEQL_SHA256` beside `CODEQL_BUNDLE` and checked with
  `sha256sum -c`.

### Fixed

- `[tool.sdlc] image` as a table no longer fails config: a string stays the
  seat's launcher tag, now deprecated for `SDLC_IMAGE`.
- The jobs read `sdlc.toml`'s `[tool.sdlc]` tables; they read its top level.
- A gate's `--network=host` / `--net=host` is refused like the two-word form.
- `containerfile-lint` exit 4 (hadolint could not parse) is a 2, not a pass.
- `[tool.sdlc] publish = "pypi"` no longer fails config as a stray key.
- `promote` sends `GITHUB_TOKEN` and refuses any tag-API status but 200/404;
  it requires the image's revision labels and `gated-<sha>` to match the digest.
- `image` takes the digest from `podman push --digestfile`, checks gate options
  against an allowlist (`--opt=value` included), and verifies and pins every
  registry FROM, builder stages too: an untrusted builder is now a 2.
- Every job names the events it runs on and refuses the rest; an unexpected
  exception in a job is a 2, never a 1.
- `check` caps the audit report under GitHub's output limit (only vulnerable
  dependencies when large); `pypi` reads `[project].name` before the build.

## [0.11.1] - 2026-11-23

### Changed

- gatehouse 0.31.0 -> 0.33.0.
- markdownlint-cli2 0.24.0 -> 0.24.2.

### Added

- The node stack: a repo with a root `package.json` gets `npm-lint`, `npm-test` and
  `npm-audit` in push and ci. They run the repo's own `lint` and `test` scripts and
  `npm audit` in a workspace volume of the repo's own (`sdlc-node-<owner>-<repo>-<hash>`,
  never the shared cache; a fresh `npm ci` every run), lint and test
  with no network, with `packages` for monorepos and `level`/`omit_dev` for the
  audit. A `package.json` only in a subdirectory is node, not docs.
- The bootc stack. `bootc-lint` runs `bootc container lint` inside the image
  `container-build` built, with no network and the launcher's dropped
  capabilities (`fatal_warnings`, `skip`, `image` settings); a bootc image is
  never smoke-run. `tests/test_bootc_stack.sh` proves it against a real bootc base.
  Detection reads the final stage's base, with `ARG` defaults resolved.
- The docs stack: `markdown-lint` (markdownlint-cli2, fleet defaults that judge
  correctness and not taste unless the repo has its own config) and `link-check`
  (lychee, offline by default; `external = true` adds remote links), both in
  push and ci for a repo with nothing else to be. The image carries
  markdownlint-cli2 0.24.0 and lychee 0.24.2.
  `scripts/update_tools.py` moves both pins, lychee's digest with its version.
- `sdlc gatehouse` mounts the file named by `OPENROUTER_API_KEY_FILE` in
  `gatehouse.env` read-only at the same path, so an OpenRouter key delivered
  as a file reaches Gatehouse as a file and never as an environment value
  (a968). A relative path, one holding `:` or `,`, or a missing file stops the
  gate before it runs. Without the variable nothing changes. Needs Gatehouse
  0.9.0 or later in the image; it ships 0.31.0.
- Promote attests a syft SPDX SBOM and SLSA v1 provenance to the digest it
  signed, with the fleet key, before any tag moves, as constitution's
  `container-release.yml` does for every other image (a997). The build step
  records its run, workflow and commit in `com.crunchtools.build.*` labels,
  and `scripts/provenance.py` writes the predicate from them, so a tag run
  that promotes without building names the run that built the digest. A
  digest built before this, or by any ref but main or a `v*` tag, gets the
  SBOM and no provenance. `scripts/attest-digest.sh` scans once and attests in
  both `sdlc` and the `python-sdlc` mirror, since an attestation stays in the
  repository it was made in. As constitution#209 does, syft, skopeo and the
  predicate writer run in a step without the key, and only the `cosign attest`
  step holds it. Verify with `cosign verify-attestation --insecure-ignore-tlog
  --type spdxjson` (or `slsaprovenance1`) and the fleet key.
- The fleet cutover (sdlc#88, a982): `scripts/fleet_wave.py` now opens one
  pull request per consumer repo carrying every change at once, so each repo's
  CI runs once. After `convert_repo.py` floats the gates, the new
  `scripts/cutover.py` moves every exact `crunchtools/constitution` workflow
  pin, `Inherits:` and the constitution hook rev to the target release; drops
  `.github/dependabot.yml` and its auto-merge caller with the target
  constitution's own `fleet-bump.py` code (from constitution v1.60.0 the
  validator fails a repo that has them); renames a bare `python-sdlc` left in a
  workflow, hooks file or sdlc config (`downstream:`, a dispatch loop); adds
  `sign: true` and the two cosign secrets to every `container-release.yml`
  call; adds a weekly rebuild to the repos `--weekly` names (#88's four); and
  writes `examples/release.yml`, the Ashigaru release flow from
  rhel10-bootc-workstation, where a repo publishes an image and has no release
  flow. The versions are arguments (`--constitution-dir`, `--constitution`,
  `--constitution-ref`, default `v1`, the major tag, as fleet-bump.py writes it; `--sdlc`, `--unbuilt-stacks`). Each converted repo is
  judged by the target validator, and one it fails is reported `invalid` and
  not pushed. `--out DIR` writes each repo's diff and a `report.md`: what each
  repo got, and what it needs by hand. The branch is `chore/fleet-cutover`;
  open pull requests from `chore/sdlc-floating` or `chore/renovate` skip a
  repo. The listing includes private repos the token can see, and `sdlc` and
  `constitution` are skipped as upstream.
- The fleet cutover re-copies the Ashigaru caller (a1024): a repo whose
  `.github/workflows/ashigaru.yml` calls triage, code and fix one by one, so
  Dependabot bumps it one pin at a time, gets crunchtools/ashigaru's
  `examples/ashigaru.yml`, read at the `v5` tag and never main, in the same
  pull request. Its `router.yml@v5` call carries the release-tag job, so a
  router repo's `release.yml` becomes the new `examples/release-open.yml`:
  the workflow_dispatch that has the app open the release pull request, and no
  tag job. sdlc's full template is swapped for it, another `release.yml`
  loses its tag job and `pull_request_target`, a workflow that is only a tag
  job is removed, and a repo still left with two tag paths is hand work. A
  router caller already at `v5`, a repo with no `ashigaru.yml`, and the
  ashigaru repo itself are left alone.
  `report.md` also counts the constitution workflow calls, in any workflow,
  left at neither the target ref nor a major tag after the cutover, and each
  is hand work. `report.md` names the ashigaru
  commit the caller came from, the repos that got it, and the open Dependabot
  pull requests that bump one Ashigaru pin, to close at cutover.
- `scripts/convert_repo.py --unbuilt-stacks LIST`: the stacks the image has no
  checks for yet, none by default now that all three are checked (this
  repo's fleet_wave.py likewise). A `skip-stacks` entry for one
  of those three that is no longer in the list is dropped, with the note the
  converter wrote above it when the list empties.

### Fixed

- A stray node_modules no longer survives into the next node check. The
  workspace sync kept every node_modules in the copied tree, but `npm ci`
  resets only each listed package's own, so one an earlier run left elsewhere
  survived both, and Node's resolution walks up into it. The sync now removes
  every node_modules except those directly under a listed package; a
  symlinked directory is still never walked into. (#100)
- `convert_repo.py` no longer carries a `runs-on` that reads `matrix`,
  `inputs`, `needs` or `strategy` into examples/ci.yml, whose `sdlc` job has
  none of them; and every time ci.yml falls back to `ubuntu-latest` from a
  runner of the repo's own, it says why in a `runner:` note that `fleet_wave.py`
  files as hand work in report.md and the PR body (a1019, #97).

- `scripts/convert_repo.py` renamed `quay.io/crunchtools/python-sdlc`, and
  refused a clone of sdlc, only when no period followed the name, so a
  reference ending a sentence was missed. A period is now a boundary unless a
  name character follows it.
- `sdlc ci` no longer gets CodeQL OOM-killed on agent seats (a989). Rootless
  podman inside a seat drops `--memory`, so every lane was "given" 8g in a 4g
  cgroup and CodeQL sized its heap from that beside an image build. The
  launcher now reads the host's real ceiling (cgroup `memory.max` from leaf to
  root, keeping a quarter for the agent, or MemTotal), defaults
  `SDLC_MEMORY` to it when it is under 8g, divides it between CPU lanes as it
  does the CPUs, and runs the lanes in turn when a share would be under
  3072m. A machine with room for 8g per lane is unchanged. `SDLC_MEMORY`
  now also refuses a size with the unit in the middle, like `8g8m`.
- `scripts/convert_repo.py` replaced a covered `sdlc ci` job's `runs-on` with
  `examples/ci.yml`'s `ubuntu-latest`, moving 54 fleet repos off the cached
  and uncached self-hosted runners. The stub now keeps the job's own runner
  and the comment above it (a1003).
- `git commit -a` no longer loses its blobs when something prunes the clone
  while the pre-commit hook runs (#94). Until the commit is made, `commit -a`
  records its new blobs only in `.git/index.lock`, which gc cannot see, so a
  `git gc --prune=now` from another process deleted them and the commit failed
  with "invalid object ... Error building trees". While a hook's gate runs,
  `bin/sdlc` now pins the hook's index tree at `refs/sdlc/hook-index` and
  removes the pin when the gate ends.

- `sdlc push` on this repo runs every lane instead of reporting mypy, pytest,
  semgrep and pip-audit as could-not-run (a1008). The lock was refreshed (it
  still pinned 0.6.0, so `uv sync --locked` failed) and gained a `dev` group of
  mypy, pytest and PyYAML; `[tool.mypy]` checks `sdlc`, `validate` and
  `scripts/` with `scripts_are_modules`, and `tests/test_suites.py` runs each
  script-style suite as its own process. `.semgrep/` holds two rules with
  fixtures: no shell between config and a tool, and no `t_*` check returning a
  pass from an `except` arm. `convert_repo.py` refuses a `- repo:` line with no
  repo instead of crashing on it.

### Changed

- The repo is `crunchtools/sdlc` and the image `quay.io/crunchtools/sdlc`
  (a974). `container.yml` pushes and signs `sdlc` first, and copies the same
  digest, with the same tags and its own signature, to
  `quay.io/crunchtools/python-sdlc`. That copy is a transitional mirror and
  goes away after the fleet wave. A clone's `bin/sdlc` runs
  `quay.io/crunchtools/sdlc:$IMAGE_TAG`, and its constitution cache moves to
  `~/.cache/sdlc/constitution/`. The project is `sdlc` in `pyproject.toml`.
  `bin/sdlc-bootstrap` is unchanged byte for byte, so its pinned sha256 stands.
- `scripts/convert_repo.py` renames what still says python-sdlc in a
  consumer: `quay.io/crunchtools/python-sdlc` and
  `uses: crunchtools/python-sdlc/.github/workflows/...@` become `sdlc`, in
  workflows, `[tool.sdlc]` config and the hooks file. It accepts the pinned
  hooks, and the pinned `sdlc ci` step, under either repo name. It refuses a
  kept workflow that clones sdlc by rev under either name, over https or ssh,
  where before it caught only an https clone of python-sdlc. A repo that already floats gets the rename and
  the current bootstrap, so `scripts/fleet_wave.py` can run the wave over the
  whole fleet. The wave skips this repo under either name, and its commit and
  pull request text says what the rename changes.
- `constitution.yml` calls `validate.yml@v1`, the constitution's floating
  major tag, as `examples/constitution.yml` does, in place of `@v1.41.0`.
  v1.60.0 lists `sdlc` beside `python-sdlc` in the fleet lists, which the
  rename had dropped this repo from.

## [0.11.0] - 2026-11-22

### Security

- `bin/sdlc-bootstrap` verifies the image before it runs anything from it
  (#82). It resolves the tag to a digest and checks that digest's cosign
  signature against the fleet key, which it carries inline. cosign v2.6.5 runs
  from a digest-pinned image, so the host still needs only podman. The
  launcher and every check then run by that digest. An unsigned or
  wrongly-signed image is exit 2, GATE CANNOT RUN. A verified digest is
  remembered in `${XDG_CACHE_HOME:-~/.cache}/sdlc/verified/`, so an offline
  seat keeps working. The bootstrap's sha256 changes, so the constitution's
  `sdlc_bootstrap_sha256` needs the new hash, and converted repos need the
  new `.sdlc/bin/sdlc`.
- `container.yml`'s promote job signs the tested digest in both
  `quay.io/crunchtools/python-sdlc` and `quay.io/crunchtools/sdlc` before any
  tag moves. It uses the fleet key from the `cosign-signing` environment and
  checks it against the key the bootstrap trusts (`scripts/sign-digest.sh`,
  tested by `tests/test_sign_digest.sh`). Release tags from before
  this change are unsigned, and the new bootstrap refuses them.

### Fixed

- `release.yml` tags with the `ashigaru-crunchtools` App instead of
  `crunchtools-ci` (a965). The new cosign release-tag ruleset lets only the
  Ashigaru App create `v*.*.*` tags, so the next release tag would have been
  refused. The token is scoped to this repository and `contents: write`.
- Gatehouse runs, in every tier and by hand as `sdlc gatehouse`, only when the
  remote is a crunchtools repo on GitHub, because it sends the diff to an
  external model and the diff can be a Red Hat repo's. Any other remote, or
  none found, skips it with one `GATEHOUSE SKIPPED` line -- exit 0, a policy
  verdict, not a 2. The remote is `PRE_COMMIT_REMOTE_URL`, else the branch's
  upstream remote, else `origin`. The URL is parsed strictly (see the README):
  `#`, `?`, `%`, a backslash, whitespace or control characters reject it, and
  scheme and host are compared exactly, the owner case-insensitively, and the repo against an allowlist. The printed URL has its userinfo
  masked and non-printable bytes replaced.
- `SDLC_GATEHOUSE_ANY_REMOTE=1` (exactly `1`) overrides the guard for a
  deliberate hand run against another remote and prints
  `GATEHOUSE GUARD OVERRIDDEN`. No hook sets it.

### Changed

- Gatehouse triage is passed `review_result: ${{ needs.review.result }}`, so a PR
  whose review job died before posting is held (gatehouse v0.32.0).
- The launcher's not-the-release banner asks about the name the bootstrap
  resolved (`SDLC_VERIFIED_FROM`), not the digest it runs by, so `:latest`
  does not call itself unpinned.

## [0.10.4] - 2026-11-19

### Changed

- `sdlc mypy` runs mypy with `--show-traceback`, so a mypy crash (its
  INTERNAL ERROR, exit 2) leaves the traceback naming its cause in the CI log
  rather than only COULD NOT RUN. The flag changes nothing unless mypy crashes
  and does not key mypy's cache (#75).

## [0.10.3] - 2026-11-18

### Changed

- `examples/ci.yml`'s `sdlc ci` job times out after 30 minutes rather than
  GitHub's 6 hours, so a hung job cannot hold a runner; `convert_repo.py` still
  treats a job carrying `timeout-minutes` as one the stub covers.

## [0.10.2] - 2026-11-18

### Changed

- gourmand 1.0.0 -> 1.1.0.

## [0.10.1] - 2026-11-18

### Changed

- gatehouse 0.30.0 -> 0.31.0.

### Changed

- `examples/gatehouse.yml`: the `guard` job ("Protect workflows") runs on
  `vars.UNCACHED_RUNNER_LABEL` in private repos, so they no longer depend on
  GitHub Actions billing. Public repos stay on `ubuntu-latest`.

## [0.10.0] - 2026-11-18

### Fixed

- **pip-audit reports what happened on the network, not a verdict it never
  reached.** pip-audit 2.9.0 exits 1 for a PyPI read timeout, which `sdlc ci`
  printed as a failed gate on the busy self-hosted runners; and an egress path
  that answers 404 makes it skip every package and exit 0, which read as a
  pass. pip-audit now runs with `--timeout 60` (was 15), retries a network
  failure twice with 10s then 30s backoff, and reports one that outlasts the
  retries as exit 2, COULD NOT RUN. A package that uv.lock takes from PyPI and
  pip-audit calls "not found" counts as a failed lookup, not as unauditable.
  So is a run that skipped every registry package installed in the venv,
  which covers a mirror whose packages are not from PyPI at all. A real
  vulnerability is still a 1, at once, with no retry: pip-audit's
  `Found N known vulnerabilities` line is checked first, so a finding beside
  a skipped package or uv's own network noise is never retried into a 2.
  pip-audit is now pinned to the image's own version (`--with pip-audit==2.9.0`)
  instead of whatever PyPI's newest is. `tests/test_pip_audit_resilience.py`
  covers each case.

## [0.9.1] - 2026-11-17

### Changed

- gatehouse 0.29.1 -> 0.30.0.

### Fixed

- **`convert_repo.py` floats a shared-file `sdlc ci` step to exactly
  `run: .sdlc/bin/sdlc ci`** (a227). It used to keep `set -eu` above the
  launcher in a `run: |` block, which constitution v1.46.0's exact-run check
  fails (gatehouse needed a hand fix). A block an earlier run left that way is
  rewritten too, a pinned step with more in its block is refused, and the
  final verify reads the run as the validator does. The pinned form now
  accepts a quoted launcher path, `"$RUNNER_TEMP/sdlc/bin/sdlc" ci`, which
  made the fleet wave refuse gatehouse.

## [0.9.0] - 2026-11-17

### Added

- **No `rev:` to pin.** The image now carries its own launcher
  (`/usr/share/sdlc/launcher`) and prints it with `sdlc launcher <ref>`, the
  ref written into it. `bin/sdlc-bootstrap` -- committed in a repo as
  `.sdlc/bin/sdlc` -- pulls `quay.io/crunchtools/sdlc:${SDLC_IMAGE:-latest}`
  when newer, asks it for the launcher and runs it, so a repo's hooks and CI
  float with the image and a release needs no bump pull requests. An image
  that cannot print its launcher is a 2. `SDLC_IMAGE` there may name only
  `quay.io/crunchtools/sdlc` -- `main`, `latest`, `X.Y.Z` or `@sha256:<64
  hex>` -- and anything else is refused with exit 2 before podman runs. `examples/pre-commit-floating.yaml`
  and `examples/ci.yml` are the hook and CI forms; the CI job keeps the name
  `sdlc ci`, and a repo opts its CI into `:main` with the repository variable
  `SDLC_IMAGE`.
- README "When :latest is broken": `--no-verify` locally, the organization
  variable `SDLC_IMAGE` set to the last good release for CI, and why that
  works only once two releases carry `launcher`.
- `scripts/convert_repo.py`: converts one checkout to the floating form, from
  either the rev-pinned python-sdlc hooks or the standalone Gourmand and
  Gatehouse hooks. Deterministic; prints the diff and writes nothing without
  `--apply`; keeps every workflow the stubs do not cover; never touches
  `.specify/`; refuses with exit 2 a shape it would have to guess at. Every
  file it would write is parsed back and held to the floating wiring before
  anything is written, and a second run is a no-op.
- `scripts/fleet_wave.py`: runs the converter across the org, the last
  fleet-wide PR wave. A dry run by default: each public, non-archived repo (or
  each named in `--repos FILE`) is shallow-cloned at the default branch the API
  names, converted and verified -- a second converter run must be a no-op, the
  bootstrap committed executable, nothing outside the converter's files
  changed -- and the plan goes to a TSV ledger. Preflight skips a repo whose
  `SDLC_IMAGE` the bootstrap would refuse or that has an open
  `chore/python-sdlc` PR. `--apply` pushes `chore/sdlc-floating` (reused, one
  per repo), opens or updates one PR with a fixed body, and queues a squash
  auto-merge, through the REST and GraphQL APIs with `GH_TOKEN` from the
  environment, never argv. Sequential; a re-run skips what the ledger says is
  done.

- `container-smoke` SKIPS an image whose PID 1 is systemd (`/sbin/init`,
  `/usr/sbin/init`, `systemd`, as in ubi-init and bootc images): systemd needs
  privileges the smoke run withholds on purpose. The run says why and prints
  `SKIPPED, not judged`, never `passed`; the build and the scan still run. The
  check's record carries exit 4. Only the smoke run can skip: a 4 from a tool
  in the image is still a finding.

- `scripts/convert_repo.py` writes the config a converted repo needs to run
  on its first try: `skip-stacks` for node, bootc or docs, which this image
  cannot check yet, and `[tool.sdlc.ci] skip` for `container-build`,
  `container-smoke` and `container-scan` when the Containerfile needs build
  secrets (`--mount=type=secret`, `subscription-manager`, RHSM). A Python repo
  naming no stacks is untouched; an existing key that disagrees is refused.

### Changed

- **`container-smoke` is advisory by default.** It runs and prints what it
  found as `FOUND SOMETHING, ADVISORY: not gating` (record exit 5), and the
  command does not fail. `gate = true` in `[tool.sdlc.ci.container-smoke]`
  makes a finding gate; a smoke run that could not run is still a 2.
- `containerfile-lint` also ignores SC1072, which hadolint raises on an
  exec-form `RUN ["python3", "-c", …]`, and a Containerfile hadolint cannot
  parse (a `RUN` heredoc) is printed as ADVISORY and SKIPPED, not failed.
- `examples/gatehouse.yml` is gatehouse at 4ae89d0 (gatehouse#150): the guard
  also refuses a fork's change to `.sdlc/`. The converter brings an older guard
  up to date.
- `containerfile-lint` without a `.hadolint.yaml` now fails at `error`, not
  `warning`, and ignores DL3007 (floating base tags) and DL3041 (unpinned dnf
  packages), which the fleet does on purpose. Warnings such as DL3002 are
  printed and do not fail. A repo's `.hadolint.yaml` replaces these defaults.

### Removed

- `bump-consumers` (and `scripts/bump_consumers.py`): repos float on
  `sdlc:latest`, so a release opens no bump pull requests. A repo still pinned
  by `rev:` moves when it converts.

### Fixed

- `container-smoke` passes an exit 0 whatever podman says of OOMKilled: nested
  rootless podman reports it true even for `true`. OOMKilled counts only
  beside a non-zero exit.
- `container-smoke` removes the smoke container with `-v`, so an image that
  declares a `VOLUME` no longer leaks an anonymous volume on every run. A fleet
  measurement filled a seat's disk with 13 GB of them.

## [0.8.0] - 2026-11-17

### Added

- **Stacks.** One image for every kind of repo: the planner detects which
  stacks a repo has from its files -- `pyproject.toml` is python, a root
  `Containerfile` or `Dockerfile` is container, `package.json` is node, a
  `bootc` base image is bootc, nothing is docs -- and runs each stack's checks
  plus the ones every repo gets (Gourmand, Gatehouse, the constitution
  validator, the hook check). `[tool.sdlc] stacks` adds a stack and
  `skip-stacks` drops one; neither can name a command. `sdlc stacks` shows what
  was detected and what runs. node, bootc and docs are named but not yet
  checked, so a repo that has one is refused (exit 2) until it skips it, rather
  than going green on checks that never ran.
- **A Python repo that names no stacks runs exactly what it ran before**, with
  or without a Containerfile. Proved by planning every public consumer's
  `commit`, `push`, `ci` and `tiers` with v0.7.4 and with this image and
  diffing the output.
- **The container stack**: `containerfile-lint` (hadolint 2.14.0, in push and
  ci) and, in ci only, `container-build`, `container-smoke` (the built image
  starts detached with no network and no capabilities, and podman's report of
  its state decides: still running after `seconds`, default 10, or exited 0,
  passes; an entrypoint that cannot start fails) and `container-scan` (grype 0.121.0 over the
  built image's filesystem, failing on a fixable Critical by default; the
  repo's `.grype.yaml` applies).
- `sdlc.toml`: the same `[tool.sdlc]` tables, for a repo with no
  `pyproject.toml`. Refused beside one.
- **Main before a release, as `:main`.** Every push to main publishes its
  tested image as `quay.io/crunchtools/sdlc:main`: the digest the contract
  test ran against, read back after the copy, never moved onto an older
  commit. No pull request run touches a registry. On a developer's machine,
  `SDLC_IMAGE=main` (or any tag or image reference) or
  `[tool.sdlc] image = "<tag>"` runs it; CI and `sdlc ci` refuse the repo key
  (exit 2). Every run on anything but the pinned release prints which image
  it is and what chose it, and a registry tag is pulled when newer. With
  neither set, nothing changes.
- The image is also published as `quay.io/crunchtools/sdlc`: the same digest
  copied, never rebuilt, with the same tags as `python-sdlc`.

### Security

- **A repo's config cannot inject podman options.** Every image and tag a
  check uses -- `container-build`'s tag, smoke's and scan's image, and the
  `image-build` and `pytest-in-image` kinds (the last two since they were
  added) -- must be an image reference: nothing starting with `-`, no comma,
  no whitespace. Otherwise the planner exits 2. The smoke and scan runs put
  the image after `--`, and `bin/sdlc` checks that word again on the host. A
  repo path that begins with `-` is refused the same way.
- The promote guard fails closed: it treats only "manifest unknown" as an
  absent tag, and any other failure to read a tag's revision stops the job.

### Fixed

- `bin/sdlc`'s `run_host` kept its exit status in `_rc`, which `run_serial`
  also uses, so a host step could leak its own status. It has its own name now.
- The hadolint, grype and shellcheck images are pinned by digest, and every
  `actions/*` step in `container.yml`, `release.yml` and `update-tools.yml`
  by commit SHA.

## [0.7.5] - 2026-11-17

### Changed

- gatehouse 0.29.0 -> 0.29.1.

## [0.7.4] - 2026-11-17

### Changed

- gatehouse 0.28.0 -> 0.29.0.

### Fixed

- `release.yml` no longer mistakes a failed `gh release view` (auth, network,
  rate limit) for an unreleased version: only `release not found` counts, and
  any other failure stops the job. The CHANGELOG heading check is a literal
  match, so a version's dots no longer match any character. The decision now
  lives in `scripts/release-version.sh`, tested by
  `tests/test_release_version.py`.

## [0.7.3] - 2026-11-17

### Changed

- gatehouse 0.27.0 -> 0.28.0.

## [0.7.2] - 2026-11-17

### Changed

- gatehouse 0.26.0 -> 0.27.0.

### Fixed

- `sdlc ci` and `sdlc tiers` now name the push verdicts a repo took out of
  ci with `[tool.sdlc.push] skip`, as one line:
  `sdlc ci: not run, skipped in [tool.sdlc.push]: lint, codeql` (#50).
  Before, a skipped verdict was simply absent, so a green ci could not be
  told from one that opted out.
- This repo's own gatehouse pre-commit hook now pins
  `quay.io/crunchtools/gatehouse:0.26.0` (was 0.23.0, missed by the image
  bumps). 0.23.0 predates `GATEHOUSE_LLM_BASE_URL` (0.24.0), so it ignored the
  seat's Trentina proxy and sent the proxy token straight to OpenRouter: 401.

## [0.7.1] - 2026-11-17

### Changed

- gatehouse 0.25.2 -> 0.26.0.

## [0.7.0] - 2026-11-17

### Changed

- **`sdlc push` and `sdlc ci` now run `lint`**: `ruff check --no-fix .` and
  `ruff format --check .`, read-only, first in push (#48). Ruff lived only in
  the commit tier, where it runs with `--fix` and passes anything fixable, so
  `sdlc ci` judged no lint at all and a `--no-verify` commit reached a green
  required check unlinted. **This can turn a consumer's CI red** on its next
  bump: a tree that ruff would change now fails. Measured against the 36
  consuming repos' default branches with this image's ruff 0.17.1: 35 are
  clean and data-collector has one fixable I001. Fix with `sdlc commit`, or
  opt out with `[tool.sdlc.push] skip = ["lint"]`, which removes it from ci
  as well; `[tool.sdlc.ci] skip` may not name it.
- README: `gatehouse` is no longer listed as exempt from python-sdlc; it runs
  it as of gatehouse#134 (constitution#138).

### Fixed

- `sdlc-validate` now says "cannot read the constitution checkout" when the
  checkout directory cannot be listed (e.g. an SELinux label), instead of
  reporting `validate-constitution.py` as missing.

## [0.6.3] - 2026-11-11

### Changed

- gatehouse 0.25.1 -> 0.25.2.

### Fixed

- **The constitution check could not run on an SELinux host with a cold
  cache.** The launcher mounted the `ln -sn` link to the host's checkout, and
  podman's `z` relabels the path it is given, so the link was relabelled and
  the directory behind it kept `cache_home_t`. The container was refused it and
  reported "missing validate-constitution.py". It now mounts the resolved
  directory. Found converting mcp-atlassian (RT#1552).

## [0.6.2] - 2026-11-10

### Changed

- gatehouse 0.25.0 -> 0.25.1.

## [0.6.1] - 2026-11-09

### Changed

- gatehouse 0.24.1 -> 0.25.0.

## [0.6.0] - 2026-11-08

### Added

- **Exit 3: could not run, deferred to CI** (constitution 1.35.0, XII). When
  CodeQL itself breaks at the push tier, outside CI, in a repo whose workflow
  RUNS `bin/sdlc ci`, the push goes through with a loud `DEFERRED TO CI` and the
  record shows exit 3. Before this, a broken CodeQL blocked every push and
  `--no-verify`, which skips every gate, was the only way past it. `sdlc ci`,
  `sdlc codeql` and a config error are still 2. A tool's own exit 3 is mapped to
  2, so only the gate can defer, and it is granted per check -- today only
  CodeQL. The text `sdlc ci` alone does not unlock it: a comment, a YAML `name:`
  and an echoed string are mentions, not runs, and trentina has two of those.
  The question is answered from the PARSED workflow: an unconditional
  `steps[].run` invoking a path ending in `bin/sdlc` with `ci` as its argument,
  in a workflow whose `push`, `pull_request`, `pull_request_target` or
  `merge_group` trigger is unfiltered or reaches the default branch under
  GitHub's own glob rules. A job name,
  a step name, an `env:` value, a comment and an echoed string are mentions;
  a job or step behind an `if:` may be skipped on this commit; `on: push`
  filtered to another branch judges nothing; a file that does not parse grants
  nothing (#33, constitution#99).

### Changed

- Inherits constitution v1.35.0, the release that amended XII to allow exit 3.

### Fixed

- **CodeQL's evaluator gets the memory the gate budgets.** CodeQL ignored the
  container and gave itself a ~1.2-1.5 GB heap, and analyze died on
  josui-skills with status 70. `--ram` is now three quarters of the smallest of
  `SDLC_MEMORY`, the cgroup limit and MemTotal: 6144 MB at the default 8g
  (#33).
- **A half-built CodeQL database no longer needs a person to delete it.** Runs
  on one project take a lock and wait for each other instead of deleting each
  other's database mid-run. The previous database is removed, read-only parts
  included, and checked to be gone before `create` runs (#33).
- `run_plan` read run_serial's per-step `_rc` as its own result, because shell
  variables are global. It had no visible effect while every non-zero step
  failed the run, but it turned a deferred push's 0 into a 3.

## [0.5.3] - 2026-11-08

### Changed

- gatehouse 0.24.0 -> 0.24.1.

## [0.5.2] - 2026-11-07

### Changed

- gatehouse 0.23.0 -> 0.24.0.

### Fixed

- **A gourmand sidecar file with no `gourmand.toml` beside it stops the gate
  instead of being ignored.** gourmand resolves
  `gourmand-exceptions.toml`, `gourmand-patterns.toml` and
  `gourmand-audit-ledger.toml` relative to `gourmand.toml`. With no
  `gourmand.toml` the sidecar is found on disk, never parsed, and nothing is
  said about it, so the gate goes red listing violations that were already
  justified — which reads exactly like a wrong justification, so the natural
  response is to rewrite the justification, repeatedly. It cost an evening on
  this repository before the cause was found, and 26 repositories in the fleet
  carry an exceptions file. `gourmand` now exits 2 naming the file it found and
  the one it wanted, because a check that cannot do what it was configured to do
  must not report as though it did (#25).

## [0.5.1] - 2026-11-06

### Fixed

- **The constitution gate no longer depends on a warm cache.** `validate`
  clones crunchtools/constitution from inside the container, which on a gateway
  seat goes through a CONNECT proxy that refuses github.com -- the route to
  GitHub there is a git `insteadOf` in the host's git config, which the
  container never reads. A seat whose cache volume already held the pinned tag
  passed; an empty volume, or a repo moving to a tag nobody had fetched, was a
  gate that could not run. `bin/sdlc` now makes that clone with the host's git,
  once per tag under `~/.cache/python-sdlc/constitution/`, and mounts it
  read-only where `validate` already looks. The container still gets no git
  config or credentials. If the host clone fails, nothing changes: `validate`
  tries its own and reports a 2 as before.

## [0.5.0] - 2026-11-05

### Changed

- **`[tool.sdlc.ci] skip` may no longer name a push verdict.** Constitution XII
  says CI runs push's checks except Gourmand and Gatehouse. Those two are
  `CI_EXCLUDES`, decided by this tool. A repo that skipped any of the rest in
  the `ci` block alone kept the check on pre-push -- a developer's machine, one
  `--no-verify` from gone -- while the required `sdlc ci` reported green.

  This was not hypothetical. 24 of the 33 repos converted under RT#1552 carried
  `[tool.sdlc.ci] skip = ["codeql"]`, each with a comment saying the CodeQL
  Action covered it. Checked against the rulesets: the Action's check was
  required in 1 of them, advisory in 21, and in 2 (`ashigaru`, `rotv`) it never
  ran on a pull request at all -- `ashigaru` has no CodeQL workflow, and
  `rotv`'s `upload-sarif` step is gated on `github.event_name != 'pull_request'`.
  So in 23 repos the only place CodeQL could block was the pre-push hook.

  `resolve()` now refuses the config, with the names it objects to. Because
  `sdlc_config()` validates every block whichever command runs, a repo that
  adds the skip back finds out on its next `sdlc commit`, not in CI -- which
  matters, because in CI the skip is exactly what would have made it read
  green. A check the repo added itself may still be skipped in `ci` freely, and
  a check that should not run anywhere belongs out of `[tool.sdlc.push]`, where
  the decision is visible in one place.
## [0.4.0] - 2026-11-04

Fewer push cycles, and a gate you can see into. Every change here comes from
one piece of evidence: crunchtools/trentina#706 took five pushes, four of them
rejected, about 28 minutes of gate time, and not one of those four rejections
was something the developer could have seen before pushing.

### Added

- **Gourmand runs at commit, not only at push.** Its findings are
  deterministic -- the same tree gives the same answer, with no model and no
  network -- so paying for them at push, where each rejection costs a
  six-to-eight minute cycle and a re-review of the whole diff, was paying the
  wrong price. Measured in full mode, warm: 1.7s on this repo, 3.2s on
  trentina. Full rather than `--diff-source`, because full mode also sees
  duplication across files, which is most of what it is for. It runs after both
  ruff steps so it judges the formatted tree, and it stays in push as well: a
  commit can be made with `--no-verify`, and push is the tier that renders the
  verdicts.

  Semgrep deliberately does not join it. It takes 27s, and `semgrep` exits 2 on
  a repo with no `.semgrep`, so as a default it would break the commit hook
  across the fleet. A repo that wants it there writes
  `[tool.sdlc.commit] checks = ["semgrep"]`.

- **`sdlc <check> -- <args>`.** `sdlc pytest tests/test_x.py` used to run the
  whole suite: the dispatcher read `"$1"` and dropped everything after it
  without a word, so the first thing anyone reaches for after a rejected push
  -- rerun just that test -- silently did the most expensive possible thing
  instead.

  ```
  sdlc pytest -- tests/test_x.py::test_y -n0
  ```

  The arguments are appended after the config's `args` and after
  `-n $SDLC_JOBS`, so the caller's flag wins: `-- -n0` turns off the
  parallelism the gate just added, which is how you read a traceback. An
  argument that is not behind `--` is an exit 2 rather than a guess, and so is
  `--` on a tier, which is several checks. They travel by name through the
  environment and land in an argv list, never through the plan and never
  through a shell.

- **A record of what the last run cost.**

  | Path | What is in it |
  |---|---|
  | `<git-dir>/sdlc/last-<command>.json` | each check, its exit, its seconds, its log |
  | `<git-dir>/sdlc/logs-<command>/` | what each buffered check printed |

  The serial path -- which is the path every `sdlc push` took -- printed no
  durations whatsoever, so nothing about the slowest tier in this tool could be
  tuned by anyone who had not sat watching it with a stopwatch. It now prints
  the same per-check footer the lane path always had. The record is also what
  an agent needs after a rejected push: which check, how long, where its output
  went.

  In the git directory rather than a committed `.sdlc/`, because it is one
  machine's last run, and asked of `git rev-parse --git-path` because in a
  linked worktree `.git` is a file. It is **written, never read**: a verdict
  comes from running the check, and a file that could stand in for one would be
  a way past a gate. Failing to write it never fails a gate.

- **A recipe in `--help` and the README** for the order that costs least, plus
  the two things that reliably mislead people here: that the Python running the
  tests is the image's and not the machine's, and that CI triage rather than
  the local record is the authority on a review finding.

### Changed

- **The review runs beside the rest.** Gatehouse is the one check that does not
  read the worktree -- its input is the `origin/<default>...HEAD` diff,
  committed objects that `bin/sdlc` computes on the host and pipes in on stdin
  -- so the rule that sends a tree-rewriting command down a single lane does
  not apply to it. It is also the only check whose time is not CPU: one API
  call and then waiting, measured at 35s on a 365-line diff, previously spent
  with the CPUs idle. `sdlc push` therefore has two lanes now; `sdlc ci` still
  has one, because CI gets Gatehouse from its own workflow.

  What makes it free is a change to the budget: a lane whose every step is a
  question `bin/sdlc` answers itself takes one CPU and is **left out of the
  division**, so the main lane keeps the whole budget it had. Dividing by the
  lane count would have handed half the machine to a lane that wants no CPU at
  all, which is precisely how lanes were measured as a regression on a 4-vCPU
  runner in 0.3.2.

  And the working lane keeps the terminal. Buffering every lane is right when
  both are computing and wrong here -- the buffered lane would be the whole of
  a push, and three minutes of silence reads like a hang -- so when exactly one
  lane runs containers, that lane streams as it always did and the others are
  printed whole after it.

- **A release builds the commit once.** `container.yml` fired on a push to main
  *and* on a `v*` tag, and `release.yml` tags the commit that just merged, so
  both events built the same sha and both pushed `:<sha>`; the second push
  replaced the first's manifest. That cost v0.3.5 its image -- a git tag and a
  GitHub release with no `0.3.5` behind them, which for a fleet pinning by
  exact release is an exit 2 and nothing checked. A tag event now builds
  nothing: it resolves the digest the main-branch run already pushed, **runs
  the contract test against it again**, and promotes that.

  The re-verification is not ceremony. `:<sha>` is pushed before it is
  verified, so a main run whose contract test failed leaves a pushed but
  untested digest on the registry; promoting that on the strength of the push
  alone would publish an image no job ever tested. The skip is per step rather
  than per job, because a job-level `if:` would skip every job that `needs:` it
  and the fix would have looked right while promoting nothing.

- **`z`, not `Z`, in this repo's own hooks**, for the reason `bin/sdlc` has
  carried since it was written: `Z` relabels the worktree with a category pair
  private to one container, taking the tree out from under every other
  container that mounts it. Two local hooks did it anyway, and the result was
  that a plain `-v "$PWD:/mnt:ro"` -- the shellcheck step -- could not read the
  repo at all. The local Gatehouse hook also pinned 0.22.0 while the image
  bakes 0.23.0: the drift this repo exists to delete, in its own config.

### Fixed

- **A test run on the laptop no longer decides the gate's verdict.** The gate
  mounts the worktree at `/repo`, so the container and the laptop share the
  tree's `__pycache__`. Bytecode carries the path it was compiled from, and
  pytest's assertion rewriter reads its cached code back as marshalled --
  `_read_pyc` returns `marshal.load(fp)` as it stands, because the point of
  that cache is the rewritten code. So a module compiled on the host kept
  `/home/<user>/.../x.py` in `co_filename` while `__file__` was
  `/repo/.../x.py`, and any test comparing the two failed in the gate and
  nowhere else. That was four of the five pushes on trentina#706.

  `PYTHONPYCACHEPREFIX=/tmp/pycache`, in **both** containers that mount the
  tree: `run_in_image` and the `pytest-in-image` argv. With it set, the
  container neither reads the tree's bytecode nor writes any back into it --
  the second direction matters because the commit tier mounts the tree
  read-write. A plain `import` never showed this, because the stdlib's loader
  calls `_imp._fix_co_filename`; the regression test pins both halves of that.

### Security

- **The gate's own containers run with no capabilities.** A gate runs the
  repo's own code -- conftest, mypy plugins, semgrep rules, a Containerfile's
  `RUN` lines -- so the container it runs in is a boundary, not a convenience.
  `pytest-in-image` has been locked down since it was written; the gate's own
  containers, which run far more repo-supplied code, had none of it.
  `--cap-drop=all --security-opt=no-new-privileges` on both. Not
  `--network none`, which `uv sync`, `pip-audit` and Gatehouse need, and not
  `--read-only`, because rewriting the tree is the point of the commit tier.
  Normalizing `/opt/codeql`'s ownership and modes is part of this, not
  housekeeping. The bundle extracts 25 precompiled `.qlx` files at mode 0600
  owned by uid 1001, and the container's root had been reading them purely on
  `CAP_DAC_OVERRIDE`; without it `codeql database analyze` recompiled all 55
  queries and then died with an internal error -- 355 seconds and an exit 2
  instead of a verdict. Found by running trentina's whole `sdlc push` against
  the image, which is the only place it shows: a bare `podman run` still holds
  the capability, so a tool broken this way passes every hand-test.
  `tests/contract.sh` now asserts that no file under `/opt` needs that
  capability.

### Not done, and why

- **Incremental local review** (#18's 0.5.0) is out. It trades the second look
  at unchanged code -- which on trentina#706 found two real crashes -- for
  speed; it depends on two unshipped changes in `crunchtools/gatehouse`; and
  with the review now running beside the rest, it only saves wall time if the
  review is the longest lane, which nobody has measured. The durations added
  here are how that gets decided.
- **`--consensus-json` from the local Gatehouse run** is out. Gatehouse has the
  flag, but the container mounts the worktree read-only, so collecting the file
  would mean a new writable bind mount inside the git directory -- a real cost
  against a convenience the terminal output already provides.

## [0.3.7] - 2026-11-04

### Added

- **`[tool.sdlc] project`, for a repo whose Python lives in a subdirectory.**
  The launcher resolved every check against the mount root, so a repo holding a
  Python project one level down could not be gated at all: `need_project` saw
  the root `pyproject.toml`, `uv sync` ran where there was no lockfile, and the
  gate exited 2 while the real code sat untouched below.

  `project = "image-server"` in the ROOT `pyproject.toml` moves the uv-driven
  checks -- mypy, pytest, pip-audit and the sync they share -- into that
  directory. Everything repo-wide stays at the root: the constitution
  validator, Gourmand, Gatehouse, CodeQL and `ruff check .`, whose config is
  hierarchical and already picks up a nested `pyproject.toml` for files under
  it. The venv cache key includes the subdirectory, so two projects in one repo
  do not fight over one environment.

  It lives in the root pyproject and nowhere else, because the pre-commit hook,
  the pre-push hook and the CI job all read that file and must agree; an env
  var or a CI argument would have to be told to three entry points separately.
  Validated at config time -- relative, inside the repo, and holding a
  `pyproject.toml` -- so a bad value is caught on the next commit rather than
  in CI. Default `"."`, so nothing changes for the 26 repos already converted.

  Prompted by crunchtools/rotv: a Node.js repo whose `image-server/` is a
  deployed Python service with 19 modules, `strict = true` on paper and six
  test files that no workflow had ever run.

## [0.3.6] - 2026-11-04

### Fixed

- **Two builds of the same commit raced and one of them lost its image.**
  `container.yml`'s concurrency group was keyed on `github.run_id`, which is
  unique per run -- so every run sat in a group of its own and the group
  controlled nothing. A merge to main and the tag that `release.yml` then
  creates are two pushes of the SAME commit, so both built it and both pushed
  the same `:<sha>` tag; the second replaced the first's manifest, and the
  loser's "Verify the pushed digest" step failed with `manifest unknown` on a
  digest that no longer existed.

  v0.3.4 won that race. **v0.3.5 lost it**: the main run moved `latest`, the tag
  run failed, and the `0.3.5` tag was never created -- a GitHub release and a
  git tag with no image behind them, which is the one state a fleet pinning this
  image cannot survive. Nothing had pinned it yet only because the promote job
  that opens the bump pull requests is in the run that failed.

  The group is keyed on `github.sha` now, so the two serialize and the second
  verifies the digest it just pushed. v0.3.5 is superseded by this release
  rather than backfilled.

### Added

- **jq, and a stated boundary for the shell test toolbox.** Claude Code hands a
  hook its event as JSON on stdin, hooks are shell scripts, and jq is the
  smallest correct way to read JSON from bash. So a repo that ships such a hook
  has unit tests that run the script as a subprocess -- and those failed here
  with `jq: command not found`, which is not a verdict about the code. Two
  repos hit it within a day of the fleet conversion (crunchtools/josui's
  entrypoint tests; 4 of crunchtools/claude-agent's 103 shell-hook tests), and
  both declare `Requires: jq` in their own package manifests.

  The alternative was to run those tests inside each repo's own image. That is
  right for a per-repo runtime dependency and wrong here: claude-agent's
  reference Containerfile installs the PUBLISHED package, so building it in the
  gate would test the last release rather than the pull request.

  The boundary now lives as a comment above the `dnf install` where it is read:
  beyond the checks, this image carries bash, coreutils, git, curl, python3 and
  jq, and nothing else. A test that reaches for socat, nc, node or a repo's own
  runtime is an integration test and belongs in `pytest-in-image` against that
  repo's image, or in a new kind. `tests/contract.sh` now asserts jq, curl and
  bash execute, so the toolbox is a tested property rather than an accident of
  the base image.

## [0.3.5] - 2026-11-04

### Changed

- gatehouse 0.22.0 -> 0.23.0.

## [0.3.4] - 2026-11-04

### Fixed

- **`bump-consumers` says what it did.** The job printed nothing when it had
  nothing to bump and nothing when it could not open a pull request, so the two
  looked identical from the outside. That is how RT#1553 survived two releases:
  the `crunchtools-ci` App had no `pull_requests: write`, every `gh pr create`
  was refused, and the job still read as green and quiet. It now always ends
  with a count -- how many repos pin python-sdlc, how many were bumped, how
  many were already there, how many failed -- and says so loudly if no repo
  pins it at all.

## [0.3.3] - 2026-11-04

### Fixed

- **CodeQL keeps its compiled queries between runs.** Its cache defaulted to
  `~/.codeql`, which is `/tmp` in the image and discarded with the container,
  so every run compiled the whole query suite again. `--common-caches` now
  points at `/cache/codeql-common` on the cache volume. Measured on trentina
  (same image, same limits as the hook): analysis 132s cold, 46s warm; the
  whole check 166s to 92s. Compiling had been 1m32s to 3m13s of every push.

## [0.3.2] - 2026-11-04

### Fixed

- **Lanes were a 59% REGRESSION on a GitHub-hosted runner, and this is the fix.**
  v0.3.0 divided the CPU budget between the lanes, which was right -- the budget
  is for the whole gate. What was wrong is the budget. `budget()` halves the
  machine so a gate cannot take the desktop away from the person running it, and
  it did that on CI too, where there is no desktop. On a 4-vCPU runner that left
  a budget of 2, two lanes at 1 CPU each, and trentina's `sdlc ci` went from
  15m27s to 24m38s: both lanes are CPU-bound, so each took about twice as long,
  and the image chain alone (1457s) came out longer than the entire sequential
  job had been (927s). No amount of overlap wins from there.

  The budget is no longer halved in two cases that have nobody at a keyboard to
  protect: `CI` is set, or a cpuset, a CPU affinity or a cgroup quota already
  narrows the machine -- which is someone having already decided this process's
  share, so halving it again double-counts the same protection. A developer's
  laptop is unchanged: still half, and still divided between the lanes.

  Measured on trentina's `sdlc ci` step, GitHub-hosted 4-vCPU. Those runners
  are fresh VMs with no layer cache, so the three are directly comparable:

  | | step | vs sequential |
  |---|---|---|
  | v0.2.2 sequential, 2 CPUs | 927s (15m27s) | -- |
  | v0.3.0 lanes, 1 CPU each | 1478s (24m38s) | +59% |
  | v0.3.2 lanes, 2 CPUs each | 704s (11m44s) | -24% |

  Per check, the same three: image build 500s / 1103s / 535s, in-image tests
  181s / 354s / 155s, and the `main` lane 216s / 416s / 254s. `main` ends up
  38s worse than it was sequentially, because the two lanes now jointly ask for
  all four vCPUs where the sequential job asked for two -- and that is the
  trade working as intended, since `main` was never the critical path.

  On a self-hosted runner that keeps its layer cache, absolute step times swing
  40s between runs of the same commit, so the saving there is quoted from
  inside a single run: 169s of work across two lanes against a 101s longest
  lane, about 65s, repeatable across three runs.

- The cap detection compared the allowance against `nproc`, which a cpuset has
  already narrowed, so it could never see one. It compares against the installed
  CPU count now.

## [0.3.1] - 2026-11-04

### Fixed

- **A check with more than one step ended with two footers, the first reading
  "passed in 0s".** The per-check footer v0.3.0 added was written per STEP, and
  `pytest-in-image` is two: install the lock's pytest into the cache volume,
  then run the tests in the image. trentina's first lane run printed
  `== image-tests passed in 0s` and then `== image-tests passed in 56s`, so the
  one number people now read timings from was wrong for exactly the check that
  takes the longest. There is one footer per check now, carrying the time all
  of its steps took, and `tests/test_host_contract.sh` holds a two-step check.

## [0.3.0] - 2026-11-04

### Changed

- **A command's checks run in lanes, at the same time.** `sdlc ci` used to run
  every check one after another, so trentina's image build -- which depends on
  none of mypy, pytest, semgrep or pip-audit -- waited for all four. The job
  took the sum of its checks instead of the length of its longest chain:
  5m09s on the self-hosted runner, 16m05s on a GitHub-hosted one, of which the
  image chain was 2m34s and 11m21s.

  Each plan line now carries a lane as its second field,
  `check TAB lane TAB mode TAB payload`. Steps of one lane run in sequence, in
  plan order; lanes run at the same time. The image assigns them, by two rules
  that name no repo:

  - an `image-build` check and every `pytest-in-image` check that runs the
    image it tags are one lane, because the build has to finish before the
    tests start and two builds writing one tag must not race;
  - everything else is the `main` lane, in plan order, which is where `uv sync`
    happens -- mypy, pytest and pip-audit each sync the project venv in the
    shared cache volume, and one lane makes those sequential. uv does lock the
    target environment, but a gate should not bet its venv on a lock it did not
    take.

  And one refusal: a command that rewrites the tree gets no lanes at all. ruff
  rewrites your source and gourmand writes its cache, and a build context or an
  in-image mount reading those files while they change is a check judging a
  tree that no longer exists. `sdlc commit` is ruff, so commit stays
  sequential, which is what "seconds, offline, free" wanted anyway.

  A repo with no image-build check gets one lane, and one lane is the
  launcher's sequential path: the same run, the same output, as before. `sdlc
  push` has no image chain, so push is unchanged too.

- **The CPU budget is for the whole gate, not for each container.** It is
  divided between the lanes, floor of one CPU each, so two lanes take the same
  share of the machine that one lane took when the checks were sequential.
  `sdlc ci` on a laptop is no busier than it was.

- **Each lane's output is buffered and printed whole, in plan order**, as each
  check finishes -- a log that cuts two lanes into each other is a log nobody
  can read. Buffering collapses the timestamps people were reading the
  `== <step>` markers for, so every block now ends with `== <check> passed in
  Ns`, or with how long it ran before it failed.

- `sdlc tiers` prints each check's lane when a command has more than one.

### Added

- `SDLC_LANE_STALL` (default 3600 seconds): how long the printer waits with no
  check finishing before it stops waiting on the order. What it measures is
  silence, and two things cause it -- a lane killed outright (SIGKILL, the OOM
  killer, a cancelled CI job), which runs no cleanup and so never reports at
  all, or one check that honestly takes longer than the threshold. The first is
  the one worth guarding, because without the guard the printer polls for a
  report that is never coming. Tripping it is cosmetic: the launcher says so,
  waits for every lane to stop and prints what is left, possibly out of plan
  order, and every lane's real exit status is still collected.

### Fixed

- Two lanes starting on a machine with no cache volume could both try to create
  it, and the one that lost the race called it a gate that could not run.
- A `__pycache__/*.pyc` was committed, and nothing ignored it. Running
  `python3 sdlc` or the unit tests left a modified tracked file behind.

## [0.2.2] - 2026-11-04

### Fixed

- **Every command hung inside a container.** The CPU budget walks the cgroup
  tree up to `/sys/fs/cgroup` with `dirname`, and stopped only on an exact
  match. Inside a container with its own cgroup namespace -- podman's default,
  and the self-hosted GitHub runner -- `/proc/self/cgroup` says `0::/`, so the
  walk started at `/sys/fs/cgroup/`, passed it, reached `/` and dirname'd `/`
  forever, before printing a line. trentina's first `sdlc ci` on that runner sat
  for 30 minutes. The walk now ends at the root or anywhere outside it.
- **A container's CPU quota was never read.** The walk skipped the namespace
  root, which is exactly where a container's own `cpu.max` lives; it now reads
  it, so a run in a 4-CPU container budgets 2 rather than half the host.
- `tests/contract.sh` runs the launcher inside a container and fails if it does
  not finish in 20 seconds.

## [0.2.1] - 2026-11-04

### Fixed

- **A passing Gatehouse review failed the push whenever there was a diff to
  review.** `run_in_image` never reset its exit-status variable, and shell
  function variables are global, so `git diff --quiet` -- which exits 1
  whenever there IS a diff -- left a 1 that the container's success never
  overwrote. Every push with a branch to review printed "No issues found" and
  then failed. Present since v0.1.0; found on trentina's first push through
  v0.2.0. `tests/test_host_contract.sh` now holds it.

## [0.2.0] - 2026-11-04

### Changed

- **Three commands, three config blocks.** `sdlc commit` (the pre-commit hook),
  `sdlc push` (the pre-push hook) and `sdlc ci` (the CI job) are configured by
  `[tool.sdlc.commit]`, `[tool.sdlc.push]` and `[tool.sdlc.ci]`. Each runs its
  defaults; a block's `checks` adds and `skip` removes; a check's settings live
  in its own `[tool.sdlc.<command>.<check>]` table. `sdlc tiers` prints what
  each command runs. Constitution XII v1.30.0 requires this shape.
- **ci = push minus the two reviewers, plus the repo's own checks.** CI runs
  Gourmand and Gatehouse through their own workflows; running them again here
  would pay twice and make the reviewer blocking.
- **The hooks file exports two hooks**, `sdlc-commit` and `sdlc-push`, instead
  of eleven. Which checks run is decided by the config alone, so there is no
  second list to keep in sync.
- **Paths come from each tool's own config.** `ruff check .`, `ruff format .`
  and bare `mypy` read `[tool.ruff]` and `[tool.mypy] files`. `lint_paths`,
  `type_paths`, `scan_paths`, `test_env`, `pytest_args`, `semgrep_config`,
  `codeql_config` and `codeql_gate` are gone; a stray key in `[tool.sdlc]` is
  refused rather than ignored.
- **CodeQL fails on new findings.** Any finding not in
  `.sdlc/codeql-baseline.json` fails; `sdlc codeql-baseline` writes the file.
  It used to report and pass, which made it a push-tier check that could not
  fail.

### Added

- **Kinds for a repo's own checks:** `image-build` and `pytest-in-image`. The
  second runs tests inside the built image with no network, a read-only root
  and no capabilities, with pytest installed into the cache volume at the
  versions the repo's `uv.lock` pins. There is no shell kind.
- `SDLC_SKIP=<check>[,<check>]` skips a check once. pre-commit's own `SKIP=`
  cannot reach inside a single hook.
- `bin/sdlc` refuses any planned host step that is not `podman build` or
  `podman run`, so the host contract is held by the launcher, not trusted to a
  repo's config. A host step is an argv split on the ASCII unit separator and
  never `eval`ed, and the plan refuses any value with a tab, a newline or that
  separator in it, so a config value cannot forge a second plan line or be read
  as shell. `tests/test_host_contract.sh` proves both.
- `tests/test_plan.py`; it and `tests/test_codeql_globs.py`, which CI never ran
  before, now run in the built image on every build.
- **A new Gourmand or Gatehouse reaches every Python repo on its own.** Their
  releases trigger `update-tools.yml`, which moves the pin and bumps this
  image's patch version in a pull request that merges itself when green;
  `release.yml` tags the merged version; and after the tag's image is promoted,
  `container.yml` opens an auto-merging bump PR in every repo that pins
  python-sdlc (`scripts/bump_consumers.py`). Rebuilding on a tool release, which
  is what v0.1.0 did, changed nothing: the Containerfile pins exact versions.

### Removed

- Trivy, from this image's own build. pip-audit covers dependencies; fleet
  image scanning moves to a scheduled job (RT #1551).
- `tests/test_tiers.sh` and `examples/pre-commit.yaml`; the README holds the
  one block a repo copies.
- The contract test at this repo's own pre-push, which built the image: a push
  never waits on a container build. CI runs it against the pushed digest.

## [0.1.0] - 2026-11-04

First release. One container image and one script holding every CrunchTools
Python SDLC check, so a consuming repo pins one `rev:` instead of seven tool
versions.

### Added

- `quay.io/crunchtools/python-sdlc`, on `quay.io/hummingbird/python:3.14-builder`,
  carrying ruff 0.17.1, Semgrep 1.177.0, pip-audit 2.9.0, Gourmand 1.0.0,
  Gatehouse 0.21.0, CodeQL 2.27.4 (the **Python-only** bundle, 110 MB against
  the full bundle's 1.75 GB) and uv 0.13.7. Every tool pinned to an exact
  version; the image is 1.36 GB.
- `sdlc`, the in-image runner, with ten targets: `ruff-check`, `ruff-format`,
  `mypy`, `pytest`, `gourmand`, `gatehouse`, `semgrep`, `pip-audit`,
  `constitution`, `codeql`.
- `bin/sdlc`, the host launcher. POSIX shell running only `git` and `podman`,
  per the host contract.
- `.pre-commit-hooks.yaml`, so a consuming repo's gate configuration is one
  `repo:` block. It is also the single authority for tier membership, which
  `bin/sdlc` parses and `tests/test_tiers.sh` holds it to.
- The tier split: the commit tier is `ruff-check --fix`, `ruff-format` and
  `hooks-installed` — sub-second, offline, free — and everything that renders a
  verdict runs once at pre-push over `origin/main...HEAD`, the same set and the
  same diff CI reviews.
- The exit-code contract: 0 clean, 1 found something, **2 could not run**.
  Podman's 125–127, an unwritable cache, an out-of-date lockfile, a missing
  config path and an unreachable origin are all 2. A later 1 never masks an
  earlier 2.
- `[tool.sdlc]` in the consumer's `pyproject.toml`: the one declared seam, for
  the things a fleet-wide script cannot guess (`lint_paths`, `type_paths`,
  `scan_paths`, `semgrep_config`, `semgrep_tests`, `codeql_config`,
  `pytest_args`, `test_env`, `codeql_gate`).
- `tests/contract.sh`, which runs against the **pushed digest** on every
  trigger. For an image whose purpose is "these executables exist at these
  versions", this is the central test, not a formality.
- `tests/test_host_contract.sh`, which runs every target with a `PATH` holding
  only git, podman, pre-commit, gh and POSIX utilities, plus a recording podman
  shim. Earns the host-contract exemption rather than relying on the
  constitution validator not inspecting `language: script` hooks.
- `sdlc versions`, reporting what is installed beside what was pinned. The two
  columns answer different questions and need not match: a wrapper image carries
  its own version (`crunchtools/gourmand:1.0.0` is that packaging repo's first
  release) around an upstream build (`mattdm/gourmand` v0.16.5). The tag is what
  CI pins; the self-reported version is which upstream actually runs.

### Verified against trentina

Every target produces the verdict CI produces on the same commit (`c32a1dc9`):
ruff, `mypy` (233 files), `pytest` (9464 passed, 51 skipped), Gourmand, Semgrep
(8 rules over 233 files), pip-audit, the constitution validator at the v1.29.0
tag trentina itself pins, and CodeQL (55 queries including trentina's own
`trentina-queries` pack).

### Notes on three deliberate decisions

- **mypy and pytest resolve from the consumer's `uv.lock`, not from this
  image.** They load the project's own plugins inside their own interpreter, so
  they cannot come from a tool venv here. That is a real hole in the one-pin
  promise and is documented rather than papered over; `sdlc` exits 2 instead of
  reporting a pass when either is missing from the lock.
- **The constitution validator is cloned at the consumer's own `Inherits:`
  tag**, never baked. The fleet spans v1.0.0 to v1.29.0, so a baked copy would
  judge most repos by rules they have not adopted — and the clone must bring
  `constitution.md` and `profiles/requirements.toml`, because the validator
  resolves them from its own directory and silently skips most of its checks
  when they are absent.
- **CodeQL reports and does not block by default** (`codeql_gate = false`),
  because that is what CI does: the Action uploads SARIF and its check passes
  with findings, which become alerts to triage. trentina's green commit carries
  19. A local gate stricter than CI is a gate people learn to skip. Gating on a
  committed baseline, so only new findings block, is the next piece of work on
  this target.

### Why this exists

Across 69 CrunchTools checkouts (35 Python) the tools were right and the
versions were not: ruff pinned at **9 distinct versions** from v0.8.6 to
v0.17.1, five different `line-length` values, **15 of 35 Python repos with no
mypy at all**, 17 running `pip-audit … || true` so the scan could not fail, and
the Trivy action pinned three different ways including `@master` in six places.

And one concrete bug found while building this: `trentina/scripts/gates.sh`
pinned `gatehouse:latest`, podman's default pull policy is `missing`, so a
cached tag was never refreshed — that gate had been running a two-week-old
reviewer while CI pulled fresh. Local and CI were different reviewers on the
same diff.

[Unreleased]: https://github.com/crunchtools/sdlc/compare/v0.11.0...HEAD
[0.11.0]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.11.0
[0.10.4]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.10.4
[0.10.3]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.10.3
[0.10.2]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.10.2
[0.10.1]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.10.1
[0.10.0]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.10.0
[0.9.1]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.9.1
[0.9.0]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.9.0
[0.8.0]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.8.0
[0.7.5]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.7.5
[0.7.4]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.7.4
[0.7.3]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.7.3
[0.7.2]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.7.2
[0.7.1]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.7.1
[0.7.0]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.7.0
[0.6.3]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.6.3
[0.6.2]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.6.2
[0.6.1]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.6.1
[0.6.0]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.6.0
[0.5.3]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.5.3
[0.5.2]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.5.2
[0.5.1]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.5.1
[0.5.0]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.5.0
[0.4.0]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.4.0
[0.3.7]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.3.7
[0.3.6]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.3.6
[0.3.5]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.3.5
[0.3.4]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.3.4
[0.3.3]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.3.3
[0.3.2]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.3.2
[0.3.1]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.3.1
[0.3.0]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.3.0
[0.2.2]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.2.2
[0.2.1]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.2.1
[0.2.0]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.2.0
[0.1.0]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.1.0
