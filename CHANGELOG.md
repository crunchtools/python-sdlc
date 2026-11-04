# Changelog

All notable changes to this project are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
- `sdlc versions`, reporting what is installed beside what was pinned.

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

[Unreleased]: https://github.com/crunchtools/python-sdlc/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.1.0
