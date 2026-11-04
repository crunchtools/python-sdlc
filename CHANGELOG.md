# Changelog

All notable changes to this project are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/crunchtools/python-sdlc/compare/v0.3.2...HEAD
[0.3.2]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.3.2
[0.3.1]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.3.1
[0.3.0]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.3.0
[0.2.2]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.2.2
[0.2.1]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.2.1
[0.2.0]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.2.0
[0.1.0]: https://github.com/crunchtools/python-sdlc/releases/tag/v0.1.0
