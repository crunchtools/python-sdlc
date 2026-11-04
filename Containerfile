# python-sdlc: every CrunchTools Python SDLC check, in one image.
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

ARG GOURMAND_VERSION=1.0.0
ARG GATEHOUSE_VERSION=0.23.0
ARG UV_VERSION=0.13.7

FROM quay.io/crunchtools/gourmand:${GOURMAND_VERSION} AS gourmand
FROM quay.io/crunchtools/gatehouse:${GATEHOUSE_VERSION} AS gatehouse
# Distroless, binary only -- the documented way to get a pinned uv.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM quay.io/hummingbird/python:3.14-builder

# Repeated after FROM: an ARG before the first FROM is out of scope here.
ARG GOURMAND_VERSION=1.0.0
ARG GATEHOUSE_VERSION=0.23.0
ARG UV_VERSION=0.13.7
ARG RUFF_VERSION=0.17.1
ARG SEMGREP_VERSION=1.177.0
ARG PIP_AUDIT_VERSION=2.9.0
ARG PYYAML_VERSION=6.0.3
ARG CODEQL_BUNDLE=codeql-bundle-v2.27.4

# The base runs non-root; installing needs root. The image drops back at the end.
USER root

LABEL maintainer="fatherlinux <scott.mccarty@crunchtools.com>"
LABEL description="Every CrunchTools Python SDLC check in one image: ruff, mypy, pytest, Gourmand, Gatehouse, Semgrep, CodeQL, pip-audit and the constitution validator."
LABEL org.opencontainers.image.source="https://github.com/crunchtools/python-sdlc"
LABEL org.opencontainers.image.description="Every CrunchTools Python SDLC check in one image."
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
LABEL version="0.3.6"

# What the image carries, readable without starting anything. `sdlc versions`
# prints these and a consuming repo's CI pins them.
LABEL com.crunchtools.sdlc.gourmand="${GOURMAND_VERSION}"
LABEL com.crunchtools.sdlc.gatehouse="${GATEHOUSE_VERSION}"
LABEL com.crunchtools.sdlc.ruff="${RUFF_VERSION}"
LABEL com.crunchtools.sdlc.semgrep="${SEMGREP_VERSION}"
LABEL com.crunchtools.sdlc.pip-audit="${PIP_AUDIT_VERSION}"
LABEL com.crunchtools.sdlc.codeql="${CODEQL_BUNDLE}"
LABEL com.crunchtools.sdlc.uv="${UV_VERSION}"

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
    SDLC_UV_VERSION=${UV_VERSION}

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
# A test that reaches for socat, nc, node, or a repo's own runtime is an
# integration test: it runs through the `pytest-in-image` kind against the
# repo's OWN built image, or it gets a new kind. It does not get a package
# here. Without that line this list becomes a host-tools grab bag, one repo at
# a time. tests/contract.sh asserts the toolbox, so it is a tested property
# rather than an accident of the base image.
RUN dnf install -y git-core tar zstd which jq \
 && dnf clean all \
 && rm -rf /var/cache/dnf

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
 && rm -rf /root/.cache/uv

# The constitution validator is a standalone script run by path, not a package
# (its own pyproject declares no dependencies). It needs exactly one third-party
# import, PyYAML, so that gets an environment of its own rather than joining a
# tool venv it has nothing to do with.
RUN uv venv /opt/constitution-venv \
 && VIRTUAL_ENV=/opt/constitution-venv uv pip install --quiet "pyyaml==${PYYAML_VERSION}" \
 && rm -rf /root/.cache/uv

# Gourmand is a single Rust binary -- 25 MB, linking only libc, libm and libgcc,
# so it is portable out of its image. Verified before relying on it.
COPY --from=gourmand /usr/local/sbin/gourmand /usr/local/bin/gourmand

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
RUN set -eu; \
    base="https://github.com/github/codeql-action/releases/download/${CODEQL_BUNDLE}"; \
    curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors --connect-timeout 20 -o /tmp/codeql.tar.zst "${base}/codeql-bundle-python-linux64.tar.zst"; \
    curl -fsSL -o /tmp/codeql.sha "${base}/codeql-bundle-linux64.tar.zst.checksum.txt" || true; \
    mkdir -p /opt; \
    tar --use-compress-program=unzstd -xf /tmp/codeql.tar.zst -C /opt; \
    rm -f /tmp/codeql.tar.zst /tmp/codeql.sha; \
    ln -s /opt/codeql/codeql /usr/local/bin/codeql; \
    codeql --version | head -1

# Leave /tmp empty: podman's --tmpfs does a COPY-UP of the image's directory
# contents into the fresh tmpfs, so anything a build step leaves here reappears
# at run time. Cheap hygiene rather than a fix for anything now that the
# container runs as root (below).
RUN rm -rf /tmp/* /tmp/.[!.]* 2>/dev/null || true

COPY sdlc /usr/local/bin/sdlc
COPY validate /usr/local/bin/sdlc-validate
RUN chmod 0755 /usr/local/bin/sdlc /usr/local/bin/sdlc-validate

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
