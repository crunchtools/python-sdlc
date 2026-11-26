# sdlc: every CrunchTools Python SDLC check, in one image.
#
# The rule this image exists to serve (constitution XII): a language-specific
# SDLC ships as ONE fully baked image. The standalone gourmand and gatehouse
# images stay as they are for the repos that are not Python.
#
# Why baked rather than launched: a tool pinned by this image's release is a
# tool whose version a consuming repo pins ONCE, with its `rev:`. The failure
# this prevents is live, not theoretical -- `trentina/scripts/gates.sh` pins
# `gatehouse:latest`, podman's default pull policy is `missing`, so a cached
# tag is never refreshed and that gate ran a two-week-old reviewer while CI
# pulled fresh. Local and CI were different reviewers on the same diff.
#
# Every tool below is pinned to an exact version for that reason. `sdlc
# versions` reports them and they are OCI labels, so a consuming repo's CI can
# pin what the image actually carries instead of guessing.

ARG GOURMAND_VERSION=1.1.0
ARG GATEHOUSE_VERSION=0.33.0
ARG UV_VERSION=0.13.7
ARG HADOLINT_VERSION=2.14.0
ARG GRYPE_VERSION=0.121.0
ARG LYCHEE_VERSION=0.24.2
ARG DOCKER_CLI_VERSION=29.9.3
ARG COSIGN_VERSION=2.6.5
ARG SYFT_VERSION=1.57.0

FROM quay.io/crunchtools/gourmand:${GOURMAND_VERSION} AS gourmand
FROM quay.io/crunchtools/gatehouse:${GATEHOUSE_VERSION} AS gatehouse
# Distroless, binary only -- the documented way to get a pinned uv.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv
# The container stack's two tools, each a static binary in a scratch image.
# Pinned by digest as well as tag: the digest is what is fetched, the tag says
# which release it is, and the contract test asserts the two agree.
FROM ghcr.io/hadolint/hadolint:v${HADOLINT_VERSION}@sha256:27086352fd5e1907ea2b934eb1023f217c5ae087992eb59fde121dce9c9ff21e AS hadolint
FROM docker.io/anchore/grype:v${GRYPE_VERSION}@sha256:621d56d1d68106b787022c81546f7e0a36202a25d1239f0c20195fab8c16f7ea AS grype
# The docs stack's link checker, a static Rust binary, the same way.
FROM docker.io/lycheeverse/lychee:${LYCHEE_VERSION}@sha256:e2d19e57cf6ab037026f20b8e449a1f30d9d7f81eef4194763aab2eab20bd28d AS lychee
# The runner's socket is driven, never an engine run, so the image carries
# clients. The docker CLI is here because on a GitHub-hosted runner the mounted
# socket is dockerd, whose API podman-remote cannot drive (it speaks libpod
# endpoints); the docker CLI speaks dockerd and podman's compat API alike.
FROM docker.io/library/docker:${DOCKER_CLI_VERSION}-cli@sha256:5a7af0dcb0e46c803efe9b45371dd21cb500e5b4453250394ac6f06d49b6e6f1 AS docker
# cosign is v2 on purpose: v3 stores signatures where podman and bootc cannot
# read them (see bin/sdlc-bootstrap). syft is for `sdlc image promote`, which
# does what scripts/attest-digest.sh does.
FROM ghcr.io/sigstore/cosign/cosign:v${COSIGN_VERSION}@sha256:ad281047f85c5e1fc6ffbc30c2b55be3b07b4032bef715a12122ce5829619aca AS cosign
FROM docker.io/anchore/syft:v${SYFT_VERSION}@sha256:3c70e6d5979364cefdc22b51617a78fd264574d3bd6b4f84a351204147025e2a AS syft

FROM quay.io/hummingbird/python:3.14-builder

# Repeated after FROM: an ARG before the first FROM is out of scope here.
ARG GOURMAND_VERSION=1.1.0
ARG GATEHOUSE_VERSION=0.33.0
ARG UV_VERSION=0.13.7
ARG HADOLINT_VERSION=2.14.0
ARG GRYPE_VERSION=0.121.0
ARG LYCHEE_VERSION=0.24.2
ARG RUFF_VERSION=0.17.1
ARG SEMGREP_VERSION=1.177.0
ARG PIP_AUDIT_VERSION=2.9.0
ARG PYYAML_VERSION=6.0.3
ARG JSON5_VERSION=0.17.3
ARG CODEQL_BUNDLE=codeql-bundle-v2.27.4
# The release publishes a checksum file for the full bundle only, none for the
# Python bundle installed below, so its sha256 is pinned here. Bump it with
# CODEQL_BUNDLE: the release API's asset `digest` for
# codeql-bundle-python-linux64.tar.zst is the value.
ARG CODEQL_SHA256=d862cb11aff035988f178703b69751be072f2aa7e76616f16bb725a207b50b7d
# The node stack's runtime. Pinned to the MAJOR, the way the base pins Python
# to 3.14: Hummingbird ships one nodejs24 at a time and drops the old build, so
# an exact RPM version would fail the weekly rebuild that exists to take errata.
ARG NODE_MAJOR=24
ARG MARKDOWNLINT_CLI2_VERSION=0.24.2
ARG DOCKER_CLI_VERSION=29.9.3
ARG COSIGN_VERSION=2.6.5
ARG SYFT_VERSION=1.57.0
ARG ZIZMOR_VERSION=1.32.0
ARG CLAUDE_CODE_VERSION=2.1.323
# The fleet's self-hosted Renovate (the `renovate` job, a1099). Kept equal by
# hand to the renovate@ the constitution's ci.yml validates its config with,
# and to npm/renovate's lockfile, which the build checks.
ARG RENOVATE_VERSION=44.221.0
ARG CRYPTOGRAPHY_VERSION=50.0.2
ARG PYJWT_VERSION=2.15.1
# gh, because every Ashigaru script talks to GitHub through it (its
# scripts/common.py `gh()`); there is no Hummingbird RPM, so the release tarball
# by exact version and checksum, as CodeQL is fetched.
ARG GH_VERSION=2.104.0
ARG GH_SHA256=98cb101ad87dbbd3f6b2f2bfcbd43197ccd07643140121d8a9cfaa91eadbc24e
# What sdlc_jobs runs: Ashigaru's scripts, prompts and schemas, and the
# constitution's text and fleet data, each at a release and fetched by its
# commit, never by a tag that can move. The text is what v2 repos are judged
# against: `sdlc-constitution context` hands it to Gatehouse; fleet.toml holds
# what S2 judges Renovate configs by (renovate_locked, renovate_locked_presets).
# Ashigaru is v5.4.0, tag pending: write depends on
# 5.4.0's staged_refusal patch filter.
ARG ASHIGARU_VERSION=5.4.0
ARG ASHIGARU_COMMIT=044cdff812619be9d2e424ab4118084db56cb05e
ARG CONSTITUTION_VERSION=2.0.0
ARG CONSTITUTION_COMMIT=e4b6fa17cd7a84834e7b0d12b96a3b34d0612878

# The base runs non-root; installing needs root. The image drops back at the end.
USER root

LABEL maintainer="fatherlinux <scott.mccarty@crunchtools.com>"
LABEL description="Every CrunchTools SDLC check in one image: ruff, mypy, pytest, Gourmand, Gatehouse, Semgrep, CodeQL, pip-audit, hadolint, grype, node and npm, markdownlint, lychee and the constitution validator, plus the clients to drive a runner's socket (podman-remote, docker, skopeo, cosign, syft), zizmor, the claude CLI and Renovate."
LABEL org.opencontainers.image.source="https://github.com/crunchtools/sdlc"
LABEL org.opencontainers.image.description="Every CrunchTools SDLC check in one image, for Python, container, node, bootc and docs repos, with the clients a runner job needs."
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
LABEL version="0.16.7"
LABEL org.opencontainers.image.version="0.16.7"

# What the image carries, readable without starting anything. `sdlc versions`
# prints these and a consuming repo's CI pins them.
LABEL com.crunchtools.sdlc.gourmand="${GOURMAND_VERSION}"
LABEL com.crunchtools.sdlc.gatehouse="${GATEHOUSE_VERSION}"
LABEL com.crunchtools.sdlc.ruff="${RUFF_VERSION}"
LABEL com.crunchtools.sdlc.semgrep="${SEMGREP_VERSION}"
LABEL com.crunchtools.sdlc.pip-audit="${PIP_AUDIT_VERSION}"
LABEL com.crunchtools.sdlc.codeql="${CODEQL_BUNDLE}"
LABEL com.crunchtools.sdlc.uv="${UV_VERSION}"
LABEL com.crunchtools.sdlc.hadolint="${HADOLINT_VERSION}"
LABEL com.crunchtools.sdlc.grype="${GRYPE_VERSION}"
LABEL com.crunchtools.sdlc.node="${NODE_MAJOR}"
LABEL com.crunchtools.sdlc.markdownlint-cli2="${MARKDOWNLINT_CLI2_VERSION}"
LABEL com.crunchtools.sdlc.lychee="${LYCHEE_VERSION}"
LABEL com.crunchtools.sdlc.docker="${DOCKER_CLI_VERSION}"
LABEL com.crunchtools.sdlc.cosign="${COSIGN_VERSION}"
LABEL com.crunchtools.sdlc.syft="${SYFT_VERSION}"
LABEL com.crunchtools.sdlc.zizmor="${ZIZMOR_VERSION}"
LABEL com.crunchtools.sdlc.claude-code="${CLAUDE_CODE_VERSION}"
LABEL com.crunchtools.sdlc.renovate="${RENOVATE_VERSION}"
LABEL com.crunchtools.sdlc.cryptography="${CRYPTOGRAPHY_VERSION}"
LABEL com.crunchtools.sdlc.pyjwt="${PYJWT_VERSION}"
LABEL com.crunchtools.sdlc.gh="${GH_VERSION}"
LABEL com.crunchtools.sdlc.ashigaru="${ASHIGARU_VERSION}"
LABEL com.crunchtools.sdlc.constitution="${CONSTITUTION_VERSION}"

# The commit and build time. NOT optional, and the reason is a trap trentina hit
# in #689: its promote job refuses a release unless it can read
# `org.opencontainers.image.revision` off the published image, and the
# Containerfile set `version` but never the revision -- so the first release
# after the publish path changed would have refused. Here the promote job uses
# it to refuse moving `latest` BACKWARDS onto an older commit, which matters
# because 35 repos pin this image.
ARG REVISION=unknown
ARG CREATED=unknown
LABEL org.opencontainers.image.revision="${REVISION}"
LABEL org.opencontainers.image.created="${CREATED}"

# The same values the labels carry, visible to the running script so `sdlc
# versions` reports what is actually installed rather than a second list
# somebody has to keep in step.
ENV SDLC_GOURMAND_VERSION=${GOURMAND_VERSION} \
    SDLC_GATEHOUSE_VERSION=${GATEHOUSE_VERSION} \
    SDLC_RUFF_VERSION=${RUFF_VERSION} \
    SDLC_SEMGREP_VERSION=${SEMGREP_VERSION} \
    SDLC_PIP_AUDIT_VERSION=${PIP_AUDIT_VERSION} \
    SDLC_CODEQL_BUNDLE=${CODEQL_BUNDLE} \
    SDLC_UV_VERSION=${UV_VERSION} \
    SDLC_HADOLINT_VERSION=${HADOLINT_VERSION} \
    SDLC_GRYPE_VERSION=${GRYPE_VERSION} \
    SDLC_NODE_VERSION=${NODE_MAJOR} \
    SDLC_MARKDOWNLINT_CLI2_VERSION=${MARKDOWNLINT_CLI2_VERSION} \
    SDLC_LYCHEE_VERSION=${LYCHEE_VERSION} \
    SDLC_DOCKER_VERSION=${DOCKER_CLI_VERSION} \
    SDLC_COSIGN_VERSION=${COSIGN_VERSION} \
    SDLC_SYFT_VERSION=${SYFT_VERSION} \
    SDLC_ZIZMOR_VERSION=${ZIZMOR_VERSION} \
    SDLC_CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION} \
    SDLC_RENOVATE_VERSION=${RENOVATE_VERSION} \
    SDLC_CRYPTOGRAPHY_VERSION=${CRYPTOGRAPHY_VERSION} \
    SDLC_PYJWT_VERSION=${PYJWT_VERSION} \
    SDLC_GH_VERSION=${GH_VERSION} \
    SDLC_ASHIGARU_VERSION=${ASHIGARU_VERSION} \
    SDLC_CONSTITUTION_VERSION=${CONSTITUTION_VERSION} \
    DISABLE_AUTOUPDATER=1

# git-core, not git: no perl, no tk, and nothing here runs `git send-email`.
# tar and zstd are for the CodeQL bundle below.
#
# `which` is not optional despite looking it: CodeQL's own
# python/tools/autobuild.sh runs `which python3` to locate an interpreter, and
# Hummingbird does not ship it. Without it `codeql database create` fails with
# "Could not find a valid Python distribution" on an image that obviously has
# one.
#
# THE SHELL TEST TOOLBOX, and its boundary. Beyond the checks themselves this
# image carries exactly: bash, coreutils, git, curl, python3 and jq. Nothing
# else. jq is here because Claude Code hands hooks their event as JSON on
# stdin, hooks are shell scripts, and jq is the smallest correct way to read
# JSON from bash -- so a repo that ships such a hook has unit tests that run
# the script as a subprocess, and two of them (crunchtools/josui,
# crunchtools/claude-agent) declare `Requires: jq` in their own packages. That
# is a shared protocol, not one repo's quirk.
#
# podman-remote and skopeo are not toolbox members either: they are for
# driving the runner's socket (builds, nested runs, `podman login`) and for
# copying images by digest. They float with the Hummingbird repo like git-core
# (podman-remote 6.x, skopeo 1.25 today), so the weekly rebuild takes errata
# and there is no ARG for them. There is no engine in this image: the socket
# is the engine. A shell test does not get to reach for them.
#
# Node is here for the node stack (and the docs stack's markdownlint), not for
# the toolbox: a shell test that reaches for it is still the case below.
#
# A test that reaches for socat, nc, or a repo's own runtime is an
# integration test: it runs through the `pytest-in-image` kind against the
# repo's OWN built image, or it gets a new kind. It does not get a package
# here. Without that line this list becomes a host-tools grab bag, one repo at
# a time. tests/contract.sh asserts the toolbox, so it is a tested property
# rather than an accident of the base image.
RUN dnf install -y git-core tar zstd which jq gawk podman-remote skopeo \
 && dnf clean all \
 && rm -rf /var/cache/dnf

# Node and npm are their own step, with --no-best, because Hummingbird publishes
# them as separate RPMs that do not always land together: when this was written
# the newest nodejs24-npm required nodejs24 build 0.1 while the repo's newest
# nodejs24 was build 1, and `dnf install` of both refused outright, failing the
# weekly rebuild. --no-best lets dnf take the newest PAIR that installs. It is
# not applied to the line above, where it could also skip a missing toolbox
# package, and the version checks below fail the build if either binary is not
# there afterwards.
#
# nodejs24-full-i18n is the ICU data Hummingbird's node ships without: lacking
# it, `new Intl.Segmenter()` segfaults node outright, which took down rotv's
# vitest workers. The last check below proves the build has it.
RUN dnf install -y --no-best "nodejs${NODE_MAJOR}" "nodejs${NODE_MAJOR}-npm" "nodejs${NODE_MAJOR}-full-i18n" \
 && dnf clean all \
 && rm -rf /var/cache/dnf \
 && node --version | grep -q "^v${NODE_MAJOR}\." \
 && npm --version \
 && node -e 'process.exit([...new Intl.Segmenter().segment("a\u{1F600}b")].length === 3 ? 0 : 1)'

COPY --from=uv /uv /uvx /usr/local/bin/

# One isolated environment per tool. These have conflicting dependency trees --
# semgrep and pip-audit disagree on click and rich -- so a single shared venv
# resolves to the intersection and silently installs older tools than asked for.
# `uv tool install` keeps each one's resolution to itself.
ENV UV_TOOL_DIR=/opt/tools \
    UV_TOOL_BIN_DIR=/usr/local/bin \
    UV_PYTHON_DOWNLOADS=never
RUN uv tool install "ruff==${RUFF_VERSION}" \
 && uv tool install "semgrep==${SEMGREP_VERSION}" \
 && uv tool install "pip-audit==${PIP_AUDIT_VERSION}" \
 && uv tool install "zizmor==${ZIZMOR_VERSION}" \
 && rm -rf /root/.cache/uv

# The interpreter `sdlc` and the constitution validator run under, kept apart
# from the tool venvs it has nothing to do with. PyYAML for both; json5 for the
# S2 check's Renovate configs; PyJWT with
# cryptography because sdlc_jobs signs the Ashigaru App's RS256 JWT inside the
# `sdlc` process, so it has to be importable here and cannot live in a venv of
# its own. cryptography is pinned beside it so the extra cannot float it.
RUN uv venv /opt/constitution-venv \
 && VIRTUAL_ENV=/opt/constitution-venv uv pip install --quiet "pyyaml==${PYYAML_VERSION}" \
      "json5==${JSON5_VERSION}" "pyjwt[crypto]==${PYJWT_VERSION}" "cryptography==${CRYPTOGRAPHY_VERSION}" \
 && rm -rf /root/.cache/uv

# Gourmand is a single Rust binary -- 25 MB, linking only libc, libm and libgcc,
# so it is portable out of its image. Verified before relying on it.
COPY --from=gourmand /usr/local/sbin/gourmand /usr/local/bin/gourmand

# The container stack. Both are static Go/Haskell binaries with no runtime, so
# they copy out of their scratch images as Gourmand does. hadolint lints a
# Containerfile; grype scans a built image's filesystem, which the launcher
# mounts read-only with `podman run --mount type=image`, so neither needs a
# container engine in here.
COPY --from=hadolint /bin/hadolint /usr/local/bin/hadolint
COPY --from=grype /grype /usr/local/bin/grype

# The docs stack. markdownlint-cli2 is installed from npm at an EXACT version
# into a prefix of its own, so it shares nothing with a repo's node_modules and
# the node stack's tools can move independently; --omit=dev --no-audit --no-fund
# keep the layer to what runs, and the npm cache is removed because /root/.npm
# would otherwise ship in the image. Its transitive dependencies resolve at
# build time (the package ships no lockfile to npm); the weekly rebuild takes
# their patch releases, the same trade the dnf installs make.
#
# lychee is a Rust binary that links only libc, libm and libgcc (rustls, no
# OpenSSL), so like Gourmand it is portable out of its image onto this glibc
# base; the contract test runs it here rather than trusting that.
RUN npm install -g --prefix /opt/markdownlint-cli2 --omit=dev --no-audit --no-fund \
        "markdownlint-cli2@${MARKDOWNLINT_CLI2_VERSION}" \
 && ln -s /opt/markdownlint-cli2/bin/markdownlint-cli2 /usr/local/bin/markdownlint-cli2 \
 && rm -rf /root/.npm \
 && markdownlint-cli2 --help | head -1
COPY --from=lychee /usr/local/bin/lychee /usr/local/bin/lychee

# The runner-socket and supply-chain clients, each a static binary copied from
# its digest-pinned image, as hadolint and grype are. Contract-tested for
# running here rather than trusted.
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
COPY bin/podman-shim /usr/local/bin/podman
COPY --from=cosign /ko-app/cosign /usr/local/bin/cosign
COPY --from=syft /syft /usr/local/bin/syft

# The claude CLI, from npm at an exact version, in a prefix of its own exactly
# as markdownlint-cli2 is. DISABLE_AUTOUPDATER (in ENV above) is not optional:
# an image must not update itself, its version is the pin.
RUN npm install -g --prefix /opt/claude-code --omit=dev --no-audit --no-fund \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && ln -s /opt/claude-code/bin/claude /usr/local/bin/claude \
 && rm -rf /root/.npm \
 && claude --version

# Renovate, in a prefix of its own, run only by the `renovate` job
# (sdlc_jobs/renovate.py), which starts it in a nested container. Unlike the
# tools above it installs from a committed lockfile, npm/renovate/, so every
# one of its ~600 packages is the one reviewed, by integrity hash; the build
# fails if the lockfile's renovate is not RENOVATE_VERSION. --ignore-scripts:
# no package runs code at install. That leaves re2's native module unbuilt, on
# purpose. Renovate then uses JavaScript's RegExp, and re2 only guards against
# a catastrophic regex in a config, and every config this job reads is the
# fleet's own. renovate-config-validator comes with it. 347 MB.
#
# /usr/local/libexec/renovate-node is node at mode 0111, execute-only, and the
# job starts Renovate with it (sdlc#121). The kernel marks a process that execs
# a file it cannot read as not dumpable, so the package managers Renovate runs,
# and the dependency code they run, same uid or not, cannot read its
# /proc/<pid>/environ or mem, or ptrace it: the minted token stays in Renovate.
# Outside /opt on purpose, where every file must stay readable (contract.sh).
COPY npm/renovate/package.json npm/renovate/package-lock.json /opt/renovate/
RUN npm ci --prefix /opt/renovate --omit=dev --ignore-scripts --no-audit --no-fund \
 && got=$(node -p 'require("/opt/renovate/node_modules/renovate/package.json").version') \
 && { [ "$got" = "${RENOVATE_VERSION}" ] || { \
      echo "npm/renovate's lockfile has renovate $got, not RENOVATE_VERSION ${RENOVATE_VERSION}" >&2; exit 1; }; } \
 && ln -s /opt/renovate/node_modules/.bin/renovate /usr/local/bin/renovate \
 && ln -s /opt/renovate/node_modules/.bin/renovate-config-validator /usr/local/bin/renovate-config-validator \
 && install -o 0 -g 0 -m 0111 /usr/bin/node /usr/local/libexec/renovate-node \
 && rm -rf /root/.npm \
 && renovate --version

# Gatehouse is a Python app in a self-contained venv. Copying it works ONLY
# because that image and this one are both Hummingbird with python3.14 at the
# same path -- its pyvenv.cfg records `home = /usr/sbin`. A different base would
# need this installed from source instead. Checked, not assumed.
#
# And it keeps gatehouse's OWN path, /app/venv. Moving it to a tidier
# /opt/gatehouse-venv breaks it: the console script's shebang is the absolute
# `#!/app/venv/bin/python3` that pip wrote at install time, so a renamed venv
# leaves a binary that is present, executable, and dies on exec. The contract
# test caught exactly that.
COPY --from=gatehouse /app/venv /app/venv
RUN ln -s /app/venv/bin/gatehouse /usr/local/bin/gatehouse

# CodeQL, Python only. The full multi-language bundle is 1.75 GB; this one is
# 110 MB, which is the whole reason CodeQL can be in a pre-push gate at all.
# Fetched by exact release and checksum-verified: a tool that decides whether
# code ships is not downloaded from a moving target.
#
# The ownership and modes are NORMALIZED after extraction, and that is load
# bearing rather than tidiness. The bundle carries 25 precompiled `.qlx` files
# under `.codeql/precompiled/` at mode 0600, owned by the uid that built it
# (1001). The container runs as root, so root read them only by holding
# CAP_DAC_OVERRIDE -- and the gate now drops every capability. Without the
# chmod, `codeql database analyze` cannot read its own precompiled queries,
# recompiles all 55 of them, and then dies with an internal StringPool error:
#
#   WARNING: Not using precompiled UseofInput.qlx: ...
#     java.nio.file.AccessDeniedException: /opt/codeql/.../*.bytes.qlx
#   Oops! A fatal internal error occurred.
#   codeql database analyze failed (exit 100)
#
# Found by running trentina's `sdlc push` against this image: 355 seconds and
# an exit 2 instead of a verdict. `a+rX` rather than `a+rx`, so execute is
# added to directories and to files that already had it, and nothing else.
RUN set -eu; \
    base="https://github.com/github/codeql-action/releases/download/${CODEQL_BUNDLE}"; \
    curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors --connect-timeout 20 -o /tmp/codeql.tar.zst "${base}/codeql-bundle-python-linux64.tar.zst"; \
    echo "${CODEQL_SHA256}  /tmp/codeql.tar.zst" | sha256sum -c -; \
    mkdir -p /opt; \
    tar --use-compress-program=unzstd -xf /tmp/codeql.tar.zst -C /opt; \
    rm -f /tmp/codeql.tar.zst; \
    chown -R root:root /opt/codeql; \
    chmod -R a+rX /opt/codeql; \
    ln -s /opt/codeql/codeql /usr/local/bin/codeql; \
    codeql --version | head -1

# gh, checksum-verified before it is unpacked. Unpacked by Python's tarfile
# because the base has no gzip, and gzip would be one more toolbox member.
RUN set -eu; \
    curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors -o /tmp/gh.tgz \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz"; \
    echo "${GH_SHA256}  /tmp/gh.tgz" | sha256sum -c -; \
    python3 -I -c 'import sys, tarfile; t = tarfile.open(sys.argv[1]); f = t.extractfile(sys.argv[2]); open("/usr/local/bin/gh", "wb").write(f.read())' \
      /tmp/gh.tgz "gh_${GH_VERSION}_linux_amd64/bin/gh"; \
    chmod 0755 /usr/local/bin/gh; \
    rm -f /tmp/gh.tgz; \
    gh --version | head -1

# Ashigaru's scripts, prompts and schemas, and the constitution's text and fleet
# data, at the commits above: the paths sdlc_jobs/_tools.py names. Each must say it is
# the version its ARG names (Ashigaru's VERSION file, the constitution's
# `> **Version:**` line), or the build fails. Data the jobs run, not packages,
# so one shallow fetch of the commit and nothing of its history.
RUN set -eu; \
    git init -q /tmp/ashigaru; \
    git -C /tmp/ashigaru fetch -q --depth 1 https://github.com/crunchtools/ashigaru "${ASHIGARU_COMMIT}"; \
    git -C /tmp/ashigaru -c advice.detachedHead=false checkout -q FETCH_HEAD; \
    got=$(cat /tmp/ashigaru/VERSION); \
    [ "$got" = "${ASHIGARU_VERSION}" ] || { \
      echo "ASHIGARU_COMMIT ${ASHIGARU_COMMIT} is version $got, not ASHIGARU_VERSION ${ASHIGARU_VERSION}" >&2; exit 1; }; \
    mkdir -p /opt/ashigaru; \
    cp -r /tmp/ashigaru/scripts /tmp/ashigaru/prompts /tmp/ashigaru/schemas /opt/ashigaru/; \
    git init -q /tmp/constitution; \
    git -C /tmp/constitution fetch -q --depth 1 https://github.com/crunchtools/constitution "${CONSTITUTION_COMMIT}"; \
    git -C /tmp/constitution -c advice.detachedHead=false checkout -q FETCH_HEAD; \
    got=$(sed -n 's/^> \*\*Version:\*\* //p' /tmp/constitution/constitution.md | head -1); \
    [ "$got" = "${CONSTITUTION_VERSION}" ] || { \
      echo "CONSTITUTION_COMMIT ${CONSTITUTION_COMMIT} is version '$got', not CONSTITUTION_VERSION ${CONSTITUTION_VERSION}" >&2; exit 1; }; \
    mkdir -p /opt/constitution; \
    cp /tmp/constitution/constitution.md /tmp/constitution/fleet.toml /opt/constitution/; \
    rm -rf /tmp/ashigaru /tmp/constitution

# sdlc_jobs, the ai/write/gate handlers `sdlc event` imports, on the
# interpreter's path through a .pth file. A COPY whose only source is a glob
# that matches nothing fails, so the package is staged beside a file every
# tree has: that lets this build on a tree that does not carry it yet. The
# package is flat, so the glob's copy-the-contents rule loses nothing.
COPY pyproject.toml sdlc_job[s] /tmp/sdlc-lib/
RUN set -eu; \
    rm /tmp/sdlc-lib/pyproject.toml; \
    mkdir -p /usr/local/lib/sdlc; \
    if [ -e /tmp/sdlc-lib/__init__.py ]; then mv /tmp/sdlc-lib /usr/local/lib/sdlc/sdlc_jobs; fi; \
    rm -rf /tmp/sdlc-lib; \
    site=$(/opt/constitution-venv/bin/python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))'); \
    echo /usr/local/lib/sdlc > "$site/sdlc.pth"

# Leave /tmp empty: podman's --tmpfs does a COPY-UP of the image's directory
# contents into the fresh tmpfs, so anything a build step leaves here reappears
# at run time. Cheap hygiene rather than a fix for anything now that the
# container runs as root (below).
RUN rm -rf /tmp/* /tmp/.[!.]* 2>/dev/null || true

COPY sdlc /usr/local/bin/sdlc
COPY bin/sdlc-constitution /usr/local/bin/sdlc-constitution
COPY bin/sdlc-secret-scan /usr/local/bin/sdlc-secret-scan
RUN chmod 0755 /usr/local/bin/sdlc /usr/local/bin/sdlc-constitution /usr/local/bin/sdlc-secret-scan

# The host launcher rides in the image it starts, so a repo with no `rev:` gets
# it with `sdlc launcher <ref>` (bin/sdlc-bootstrap) and it floats with the tag.
# Data here, not a program: it runs on the host, never in this container.
COPY bin/sdlc /usr/share/sdlc/launcher
COPY bin/sdlc-bootstrap /usr/share/sdlc/bootstrap

# Stays root, and WORKDIR /repo, because that is what Gourmand and Gatehouse do
# and those two have been getting code into a container for analysis across this
# fleet for a year. Copying their convention rather than inventing one:
#
#   podman run --rm -v $(pwd):/repo:ro,Z -w /repo quay.io/crunchtools/gatehouse
#
# Root here is not a privilege grab, it is what makes rootless podman correct.
# Rootless maps the container's root to the INVOKING USER, so a file `ruff --fix`
# writes into the mounted worktree is owned by the developer and the shared cache
# volume is writable, with no --user and no --userns=keep-id. Running as an
# image-internal UID instead maps to a subuid the developer does not own, which
# breaks the cache and leaves un-ownable files in the repo. Both were observed
# before this was corrected.
WORKDIR /repo

ENTRYPOINT ["/usr/local/bin/sdlc"]
CMD ["list"]
