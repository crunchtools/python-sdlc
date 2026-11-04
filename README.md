# python-sdlc

One container image and one script for every CrunchTools Python SDLC check.

A consuming repo's whole gate configuration is one block:

```yaml
# .pre-commit-config.yaml
default_install_hook_types: [pre-commit, pre-merge-commit, pre-push]

repos:
  - repo: https://github.com/crunchtools/python-sdlc
    rev: v0.1.0
    hooks:
      - id: sdlc-ruff-check
      - id: sdlc-ruff-format
      - id: sdlc-hooks-installed
      - id: sdlc-mypy
      - id: sdlc-pytest
      - id: sdlc-gourmand
      - id: sdlc-gatehouse
      - id: sdlc-semgrep
      - id: sdlc-pip-audit
      - id: sdlc-constitution
      - id: sdlc-codeql
```

That `rev:` pins the launcher and the image together, and the image pins every
tool inside it. One pin per repo.

## Why

Across 69 CrunchTools checkouts (35 Python), the tools were right and the
versions were not:

| | |
|---|---|
| ruff `rev:` in pre-commit | **9 distinct versions**, v0.8.6 → v0.17.1 |
| ruff `line-length` | 5 values: 88, 100, 110, 120, unset |
| mypy | **15 of 35 Python repos had none at all** |
| `pip-audit` | 17 repos ran it as `… \|\| true`, so the scan could not fail |
| Trivy action | `@v0.36.0`, a SHA, and `@master` in 6 places |

And a concrete one, found while building this: `trentina/scripts/gates.sh` pins
`gatehouse:latest`, podman's default pull policy is `missing`, so a cached tag
is never refreshed. That gate had been running a **two-week-old reviewer** while
CI pulled fresh. Local and CI were different reviewers on the same diff, which
is the failure mode this image exists to remove.

## The tier rule

Constitution XII. Not lint-versus-tests — **what rewrites your code in under a
second** versus **what renders a verdict**.

| Tier | Targets | Properties |
|---|---|---|
| `pre-commit` | `ruff-check --fix`, `ruff-format`, `hooks-installed` | sub-second, offline, free, auto-fixing |
| `pre-push` | `mypy`, `pytest`, `gourmand`, `gatehouse`, `semgrep`, `pip-audit`, `constitution`, `codeql` | one run per push, over `origin/main...HEAD` |

The push tier is **the same set and the same diff CI reviews**. That is the
whole mechanism behind things sailing through CI: if the push tier is green, CI
is green, because they are the same checks on the same bytes.

Commits are therefore instant and work offline. `mypy` is on the push side
despite feeling like linting: it needs the project's locked dependencies
installed to see type stubs, so it cannot be sub-second.

## Inner loop

Run one target, as often as you like, while fixing it:

```bash
sdlc mypy
sdlc semgrep
sdlc pytest
sdlc list        # what targets exist
sdlc tiers       # which tier each one runs in
sdlc versions    # the exact tool versions this image carries
```

`sdlc` is `bin/sdlc` from this repo; pre-commit clones it for you, or add it to
`PATH` for direct use.

## Exit codes

```
0   ran, found nothing
1   ran, found something
2   COULD NOT RUN
```

**2 is never to be worked around.** A gate that cannot run and reports success
is worse than no gate, because the pipeline goes green and nobody looks again.
Constitution XII was written after a repo's test runner "skipped three gates
with a warning and printed the other results as if that were a pass".

This is also where the script deliberately differs from the `gates.sh` it
replaces: that mapped podman's own 125–127 onto exit 1, reporting "found
something" for "could not invoke the container".

## Per-repo configuration

The image carries tools; the repo carries policy. `pyproject.toml`'s
`[tool.ruff]`, `[tool.mypy]`, `[tool.pytest]`, plus `.semgrep/`,
`.github/codeql/`, `gourmand*.toml` and `.gatehouse-ignore` are read from the
repo as they always were. Baking a ruleset into the image would mean one repo's
rule change breaking 35.

One optional table covers what a fleet-wide script cannot guess:

```toml
[tool.sdlc]
lint_paths = ["src", "tests", "corpora"]   # default: ["src", "tests"]
type_paths = ["src"]
scan_paths = ["src"]
semgrep_config = ".semgrep"
semgrep_tests = ".semgrep/run-tests.sh"
codeql_config = ".github/codeql/codeql-config.yml"
pytest_args = ["-q", "-rfE"]

[tool.sdlc.test_env]
GEMINI_API_KEY = "test_key_for_ci"
```

Set each value to what your CI already does, so a local verdict is the CI
verdict.

## What is in the image

| Tool | Pinned by |
|---|---|
| ruff, semgrep, pip-audit | this image (`uv tool install`, one venv each) |
| Gourmand, Gatehouse | this image (`COPY --from` their official images) |
| CodeQL | this image (the **Python-only** bundle, 110 MB; the full one is 1.75 GB) |
| constitution validator | the repo being checked — cloned at the tag its own manifest names |
| **mypy, pytest** | **the repo's `uv.lock`** |

That last row is the one honest seam. mypy runs plugins and pytest runs the
project's plugins *inside their own interpreter*, so they have to come from the
project's environment, not from here. That is still one pin per repo — the
lockfile, which is where constitution XV already says a Python dependency's
version belongs — but it is not this image's pin, and `sdlc` exits **2** rather
than reporting a pass if either is missing from the lock.

The constitution validator is cloned rather than baked for a different reason:
the fleet spans `Inherits:` v1.0.0 to v1.29.0, so a baked copy would judge most
repos by rules they have not adopted.

`sdlc versions` prints what is installed next to what was pinned, because the
two answer different questions. They need not match: `crunchtools/gourmand:1.0.0`
is that packaging repo's own first release and the binary inside it is upstream
`mattdm/gourmand` v0.16.5. Both are correct. The tag is what CI pins; the
self-reported version is which upstream you are actually running, and a tag
alone does not tell you that.

## Trivy is not here

Deliberately, and it is CI-only. It is **not** redundant with `pip-audit`:
pip-audit reads the Python lock, Trivy reads the OS packages in the image layers
(glibc, openssl, zlib), and nothing else in the pipeline looks at those. CI is
where the built image exists, so that is where it runs.

## How the repo gets into the container

Gourmand and Gatehouse have been doing this across the fleet for a year, so
this copies their convention rather than inventing one:

```bash
podman run --rm -v "$PWD:/repo:ro,Z" -w /repo quay.io/crunchtools/python-sdlc:v0.1.0 mypy
```

The container runs as its own root. Under rootless podman that maps to the
invoking user, so a file `ruff --fix` writes is owned by the developer and the
shared cache volume is writable — with no `--user` and no `--userns=keep-id`.

One deliberate difference from those two: `z`, not `Z`. A tier runs several
containers over one tree and `Z` relabels it *exclusively*, taking it out from
under whichever started first.

## Host contract

Constitution XII: a developer machine has `git`, `podman`, `pre-commit` and
`gh`, plus `~/.config/mcp-env/gatehouse.env`, and nothing else may be assumed.
`bin/sdlc` is POSIX shell and runs only `git` and `podman`; everything else is
inside the image. `tests/test_host_contract.sh` asserts that, rather than
relying on the validator not looking.

## Licence

AGPL-3.0-or-later. The CodeQL bundle is redistributed under GitHub's own terms
for the CodeQL CLI, which permit use for analysing repositories — see
`docs/codeql.md`.
