# sdlc

Every CrunchTools Python check in one container image, run the same way on a
laptop and in CI.

Constitution XII (v1.30.0) requires it for every Python repo — any repo with a
`pyproject.toml` — except `gourmand` and this one, which build what the image
contains. `gatehouse` runs it too, as of gatehouse#134 (constitution#138).

## What's different here

- **This image enforces the constitution.** Its v2 checks
  (`sdlc-constitution check`) and the text Gatehouse reviews against ship
  inside it, so a rule change reaches every repo as one sdlc release. A repo
  still carrying `.specify/memory/constitution.md` is judged by v1 until it
  converts.
- **Exit codes are a contract:** 0 found nothing, 1 found something, 2 could
  not run. Anything that is not a tool's own verdict (a container that never
  started, a missing cache, an absent lock entry) is 2, never 0, and a later 1
  never masks an earlier 2.
- **Tools are pinned here; policy is not.** Rulesets, thresholds and
  exceptions stay in the repo being checked. mypy and pytest are the
  deliberate exception: they come from the consumer's `uv.lock`, because they
  load its plugins.
- **The container runs as its own root** under rootless podman, so files it
  writes belong to the developer. Mounts are `z`, not `Z`, because one tier
  runs several containers over one tree.
- **The contract test is the central test:** the pushed digest holds every
  promised executable at its pinned version.

The reasons behind each of these are in [docs/design.md](docs/design.md).

## Three commands, three config blocks

| Command | Runs when | Config block in `pyproject.toml` |
|---|---|---|
| `sdlc commit` | the pre-commit hook | `[tool.sdlc.commit]` |
| `sdlc push` | the pre-push hook | `[tool.sdlc.push]` |
| `sdlc ci` | the CI job named `sdlc ci` | `[tool.sdlc.ci]` |

- **commit** takes seconds, works offline and costs nothing: formatting, lint
  fixes, and Gourmand's structural review. Everything here is deterministic —
  the same tree gives the same answer, with no model and no network — so there
  is no reason to find it at push, where each rejection costs a six-to-eight
  minute cycle.
- **push** renders every verdict, once, over `origin/<default-branch>...HEAD`.
  It never builds a container — that is minutes, and a push you wait minutes
  for is a push people stop making.
- **ci** runs push's checks except Gourmand and Gatehouse, which the fleet's
  ci.yml runs in their own steps (Gourmand in `check`, after `sdlc ci`;
  Gatehouse in `ai` and `gate`), plus the repo's own checks, such as building
  its image.

Because the laptop and CI run the same image and the same commands, a green
push predicts a green CI.

## Working a change

In the order that costs least:

```
sdlc commit && sdlc push          every verdict, before a CI cycle
sdlc pytest -- tests/x.py::test_y -n0    reproduce one failure, just that one
jq . .git/sdlc/last-push.json     what the last run cost, per check
```

Two things mislead people here. The Python running your tests is the image's,
not your machine's, so a result from a host `pytest` says nothing about this
gate — and bytecode your machine compiled is kept out of the container
deliberately, so it cannot say anything either. And on a review finding, CI
triage is the authority, not the local record: push predicts CI, it does not
replace it.

## Wiring a repo

A repo carries one file, `.sdlc/bin/sdlc` (this repo's `bin/sdlc-bootstrap`),
and pins nothing. The launcher ships inside `quay.io/crunchtools/sdlc`; the
bootstrap asks the image for it (`sdlc launcher <ref>`, which writes that ref
into it) and runs it. So the launcher, the image and every tool inside it float
together on `:latest`, for the laptop and CI both, and a release reaches every
repo without a pull request.

Before it runs anything from the image, the bootstrap checks that the digest
the tag resolves to is signed by a key it carries inline, using cosign v2
from a digest-pinned image. Then it runs the launcher and every check by that
digest. An unsigned image is a 2, GATE CANNOT RUN. A release (`:latest`,
`X.Y.Z`) must be signed by sdlc's own key (`boot/keys/sdlc.pub`), which has
signed every release since 0.15.2. `:main` and an `@sha256` digest may carry
either sdlc's key or the crunchtools fleet key, which signs `:main` and signed
the releases up to 0.15.1; run one of those by its digest. A digest verified
once is remembered in `${XDG_CACHE_HOME:-~/.cache}/sdlc/verified/`, so an
offline seat keeps working. CI always verifies. Promote also attests an SPDX
SBOM and SLSA v1 provenance to the digest with the fleet key; `cosign
verify-attestation --insecure-ignore-tlog --type spdxjson` (or
`slsaprovenance1`) reads them.

`.pre-commit-config.yaml` is
[`examples/pre-commit-floating.yaml`](examples/pre-commit-floating.yaml): two
`language: system` hooks, `.sdlc/bin/sdlc commit` and `.sdlc/bin/sdlc push`.
CI is [`examples/ci.yml`](examples/ci.yml), the fleet file: copied into
every repo byte for byte, no `uses:` of any kind, nine jobs that are nine
trust levels, each running `sdlc event` in the digest-pinned `sdlc-bootstrap`
image (design: `docs/marketplace-free-ci.md`). Its two required checks are
`sdlc / check (pull_request)` and `sdlc / gate (pull_request_target)`.
`tests/test_workflows.py` pins its guards. Its `boot` job runs a repo's
`boot-command` and `contract-test` shell (and `free-disk`) on the pushed digest
with no secret, so `promote` signs nothing whose boot proof failed; `image`
pushes `gated-<sha>` before `boot` judges it, so for a repo that sets those keys
the registry can hold an ungated digest. The shell runs in a sibling container of
the job's own image, started through the runner's socket: the workspace is at its
runner path, `TMPDIR` is under it, and the socket and the runner's network are
there, so a `-v` source, `mktemp` and `127.0.0.1` mean what they did under
container-release. Through the socket the shell can exec into the job's own
container and read its `GITHUB_TOKEN` (`contents: read`) and
`ACTIONS_RUNTIME_TOKEN`, which is why the job holds no secret and runs only where
the secret jobs run: each crunchtools-uncached runner is an ephemeral VM with its
own engine, running one job. Every runner's socket is its host podman's, a
GitHub-hosted one included, so the shell's container goes through podman-remote.

`scripts/convert_repo.py <checkout>` converts a repo and prints the diff;
`--apply` writes it. It floats the hooks onto `.sdlc/bin/sdlc`, writes the
fleet file as `.github/workflows/ci.yml`, and deletes every other workflow --
but only when every job in every one of them is a row of what `sdlc event`
runs (the `sdlc ci` job, the constitution, Gourmand, Gatehouse, Ashigaru and
release callers, `container-release.yml`, CodeQL, a PyPI publish, Dependabot
automerge). Anything else refuses the whole repo with exit 2, naming the file
and its jobs, to be moved to `[tool.sdlc.commands]` by hand. What the old
files said per repo moves into `[tool.sdlc]`: `publish = "pypi"`, and a
`container-release.yml` call's inputs as `[tool.sdlc.image]`. It also writes
`skip-stacks` and `[tool.sdlc.ci] skip` where the image cannot check a stack
yet or the Containerfile needs build secrets, as before.

**What floating trusts.** Whoever can move `quay.io/crunchtools/sdlc:latest`
now decides the host-side code every seat and runner runs, and that code is no
longer read in git at a pinned rev before it runs. Only this repo's release
workflow, after the contract test, moves the tag, and never backwards. Signing
the image and verifying it on pull (a `policy.json` sigstore entry for
`quay.io/crunchtools/sdlc`) would close the rest, and is not done yet.

**An `sdlc` RPM, later.** It installs the same bootstrap as `/usr/bin/sdlc` on
hosts and seats, so a person can type `sdlc tiers` anywhere. Nothing in a repo
changes when it lands: the hooks keep starting the repo's own copy, which is
identical, and the RPM never needs a release of its own, because the launcher
it fetches floats with the image.

The rev-pinned form still works, and is what a repo has until it converts:

```yaml
default_install_hook_types: [pre-commit, pre-push]

repos:
  - repo: https://github.com/crunchtools/sdlc
    rev: v0.8.0
    hooks:
      - id: sdlc-commit
      - id: sdlc-push
```

with a CI step that clones this repo at that `rev:` and runs
`$RUNNER_TEMP/sdlc/bin/sdlc ci`. That `rev:` pins the launcher, the image and
every tool inside it.

### A project in a subdirectory

If the repo's Python is not at the root -- a JavaScript repo with a Python
service beside it, say -- name the directory in the ROOT `pyproject.toml`:

```toml
[tool.sdlc]
project = "image-server"
```

Only the uv-driven checks move: mypy, pytest, pip-audit and the sync they
share. The constitution validator, Gourmand, Gatehouse, CodeQL and
`ruff check .` stay repo-wide, and ruff's own config is hierarchical, so a
nested `pyproject.toml` still applies to the files under it.

## Stacks

One image serves every kind of repo. A **stack** is the set of checks that
only mean something for one kind of repo, and which stacks a repo has is read
off its files:

| File at the root | Stack | Status |
|---|---|---|
| `pyproject.toml` | python | checked |
| `Containerfile` or `Dockerfile` | container | checked |
| `package.json` | node | checked |
| a Containerfile whose `FROM` is a bootc image | bootc | checked |
| none of these | docs | checked |

Gatehouse takes its settings from `~/.config/mcp-env/gatehouse.env` (or
`GATEHOUSE_ENV`), passed as `--env-file`. When that file sets
`OPENROUTER_API_KEY_FILE` to an absolute path, the file is mounted read-only at
the same path, so the key stays a file and never enters the container's
environment.

Gourmand, `secret-scan`, `workflow-lint`, Gatehouse, the constitution validator and
`hooks-installed` belong to no stack and run everywhere. A repo can add or drop a stack, and nothing more:

```toml
[tool.sdlc]
stacks = ["container"]
skip-stacks = ["node"]
```

**A Python repo that names neither key runs the python stack alone**, exactly
as sdlc always has, whatever else is in it. Turning the container stack
on under 36 pinned repos is its own decision, not a side effect of a bump.
`sdlc stacks` shows what was detected and what runs.

A stack this image names but cannot check yet is refused with exit 2, not left
out: a node-only repo judged by the constitution alone would go green on checks
that never ran. `skip-stacks` is how a repo says it accepts that.

A repo with no `pyproject.toml` writes the same `[tool.sdlc]` tables in
`sdlc.toml`. A repo with one configures sdlc there, and `sdlc.toml` beside it is
refused.

### The container stack

| Check | Command | What it does |
|---|---|---|
| `containerfile-lint` | push, ci | hadolint over the Containerfile. Without a `.hadolint.yaml` the threshold is `error` and DL3007 (floating base tags) DL3041 (unpinned dnf) and SC1072 (raised on an exec-form `RUN ["python3", "-c", …]`) are ignored, as fleet conventions; warnings such as DL3002 are printed and do not fail. A repo's `.hadolint.yaml` replaces all of that. A Containerfile hadolint cannot parse (a `RUN` heredoc) is printed as ADVISORY and SKIPPED, not failed; `container-build` still builds it |
| `container-build` | ci | `podman build`, at the tag the next two read |
| `container-smoke` | ci | starts the built image detached, with no network and no capabilities, and reads its state from podman: still running after `seconds`, or exited 0, passes; any other exit, the OOM killer, or an entrypoint that cannot start (126/127) is a finding. **Advisory by default**: a finding is printed as `FOUND SOMETHING, ADVISORY` and does not fail the command; `gate = true` makes it gate. Exit 0 passes even when podman reports OOMKilled, which nested rootless podman does for any container; OOMKilled counts only beside a non-zero exit. Podman failing to create the container is still a 2. An image whose PID 1 is systemd (`/sbin/init`, `systemd`) is SKIPPED with its reason: systemd needs privileges the run withholds |
| `container-scan` | ci | grype over the built image's filesystem, mounted read-only by podman; fails on a fixable Critical by default, and the repo's `.grype.yaml` applies |

Build, smoke and scan are one lane, beside everything else.

### The node stack

A root `package.json` turns it on. It has no commit-tier check: an install is not seconds, offline or free.

| Check | Command | What it does |
|---|---|---|
| `npm-lint` | push, ci | `npm run lint` in each listed package that defines a `lint` script. If none does, exit 2 (add one, or skip `npm-lint`): a check that ran nothing does not pass |
| `npm-test` | push, ci | `npm run test` in each listed package that defines a `test` script, with `CI=true` set so watch-mode runners exit. Words after `--` go to the script. None defined is exit 2 |
| `npm-audit` | push, ci | `npm audit --json` per package, judged at `level` (default `high`). A finding prints the package, severity, advisory and fix. A registry failure is retried, then exit 2, never a pass |

The repo is mounted read-only, so the scripts run in a workspace: a copy of the tracked and not-ignored files with its own `node_modules`, refreshed on every run. The workspace is a podman volume of the repo's own, `sdlc-node-<owner>-<repo>-<hash>` from the origin remote, and a node step mounts that and not the shared cache volume, so one repo's install scripts or tests cannot reach another repo's tree. Each check is an `npm-install` step with the network, then the check: `npm-lint` and `npm-test` with no network, `npm-audit` with it. `npm ci` runs on every run: the last run executed repo code with the workspace writable, so its `node_modules` is never trusted, and npm's cache, checked against the lockfile's hashes, makes the reinstall a local copy. A package without a `package-lock.json` is exit 2: the gate will not test whatever is newest today. Lifecycle scripts run during `npm ci`, as they do in CI. Tests run at `/node/tree`, not `/repo`, and see no `.git`.

A `package.json` only in a subdirectory is still the node stack: with no `packages` set, the checks exit 2 and name the directories, rather than the repo falling through to docs.

For a monorepo, `packages` (default `["."]`) lists the package directories, each with its own lockfile and each installed. Set it in each check's table. A root script that fans out (`npm run lint --prefix frontend`) needs those packages installed, so list them instead of the root:

```toml
[tool.sdlc.push.npm-lint]
packages = ["frontend", "backend"]
```

### The bootc stack

A Containerfile whose `FROM` names a bootc image is a host, not an app, so it
gets `containerfile-lint`, `container-build` and `container-scan` as above, no
`container-smoke` (its PID 1 is systemd), and one check of its own:

| Check | Command | What it does |
|---|---|---|
| `bootc-lint` | ci | `bootc container lint` inside the built image: no network, never pulls, and under the launcher's `--cap-drop=all`, which it does not need. Lint failures exit 1. Warnings pass unless `fatal_warnings = true`; a stock bootc base warns on its own |

It is in the build's lane, after `container-build`.

### The docs stack

A repo with none of the files above is judged as documentation. Both checks
read every `.md` file outside `node_modules`, `vendor` and `.git`, honour
`.gitignore`, and run on the read-only tree. Neither fixes anything, so neither
runs at commit.

| Check | Command | What it does |
|---|---|---|
| `markdown-lint` | push, ci | markdownlint-cli2. Without a markdownlint config the fleet defaults judge correctness and not taste: line length, inline HTML, bare URLs, blank lines around blocks, first-line heading, duplicate headings and the like are off; a skipped heading level, a reversed link, a broken `#fragment` or reference link, or a table row with the wrong cell count fail. A repo's own `.markdownlint.{json,jsonc,yaml,yml,cjs,mjs}` or `.markdownlint-cli2.{jsonc,yaml,cjs,mjs}` replaces all of that. |
| `link-check` | push, ci | lychee, offline: relative links, root-relative links (`/docs/x.md`, against the repo root) and `#fragments` must resolve. Remote links are not fetched, so the verdict needs no network. A `lychee.toml` is merged over those defaults key by key, and `.lycheeignore` applies |

## What a repo gets with no config at all

| Command | Runs |
|---|---|
| `commit` | ruff-check, ruff-format, gourmand, hooks-installed |
| `push` | lint, mypy, pytest, gourmand, secret-scan, workflow-lint, gatehouse, semgrep, pip-audit, constitution, codeql |
| `ci` | lint, mypy, pytest, secret-scan, workflow-lint, semgrep, pip-audit, constitution, codeql |

`ruff-check` and `ruff-format` fix what they find, so they pass on anything
fixable. `lint` is the same two tools as a verdict — `ruff check --no-fix` and
`ruff format --check`, changing nothing — and it is the only ruff that push
and ci run.

`secret-scan` refuses a change that adds a plaintext secret (constitution
XVII): a vendor credential shape (`ghp_`, `github_pat_`, `sk-ant-`, `AKIA`, ...)
or a secret-named key given a literal. It reads only the lines added since the
branch left the default branch; in CI, since the pull request's base or the
push's `before`. A named literal in a test or doc file only warns, and a line
carrying `gatehouse: allow-secret` is a fixture and is skipped.

`workflow-lint` runs zizmor, offline, over the repo's GitHub Actions workflows
and actions; a repo with none is clean. It is advisory: findings are printed
and marked ADVISORY and do not fail, until the repo sets `gate = true` in its
table. The repo's `zizmor.yml` and `# zizmor: ignore[...]` comments apply.

`sdlc tiers` prints what each command runs in the repo you are in, and which
lane each check runs in.

## Lanes

A command's checks run in lanes. Steps of one lane run in sequence; lanes run
at the same time, so a command takes about as long as its longest chain rather
than the sum of everything in it.

The image decides the lanes, by three rules:

- An `image-build` check and every `pytest-in-image` or `command` check that
  runs the image it tags are **one lane** — the build has to finish before the
  tests start. The build is planned first whatever order `checks` lists them
  in; every other check keeps its place.
- Gatehouse is the `review` lane, always. It is the one check that does not
  read the worktree: its input is the `origin/<default-branch>...HEAD` diff,
  which `bin/sdlc` computes on the host and pipes in. So it can run beside
  anything, including a command that rewrites the tree.
- Everything else is the `main` lane, in the order `tiers` prints. That is
  where `uv sync` happens, and one lane keeps two syncs out of one venv.

A command that rewrites the tree — anything running `ruff-check`,
`ruff-format` or `gourmand` — gets no lanes beside it except that reviewer,
because a build context reading those files while they change is judging a tree
that no longer exists. That makes `sdlc commit` sequential by definition.

So `sdlc push` has two lanes: everything that computes, and the review. `sdlc
ci` has one unless the repo builds an image, because CI gets Gatehouse from its
own workflow.

The CPU budget is for the whole gate, not for each container: it is divided
between the lanes that run containers, floor of one CPU each. A lane whose
every step is a question `bin/sdlc` answers itself — the reviewer, which is one
API call and then waiting — takes a single CPU and is **left out of that
division**, so putting it beside the others costs them nothing. Two lanes are
no busier on your laptop than one was.

The budget itself is half the machine, so a gate cannot take the desktop away
from the person running it — except where there is no such person. In CI, or
where a cpuset, a CPU affinity or a cgroup quota already narrows the machine,
the whole allowance is used: something has already decided this process's
share, and halving it again double-counts the same protection. Halving CI is
what made lanes slower than no lanes on a 4-vCPU runner; see the v0.3.2
changelog entry.

Each lane's output is buffered and printed whole, in the order `tiers` lists
the checks, so two lanes never cut into each other's log. Each block ends with
how long its check took.

With one exception, which is the shape `sdlc push` has: when exactly one lane
runs containers and the rest do not, that lane runs in the **foreground** and
streams to your terminal as it always did, and the others are printed after it.
Buffering the lane that is doing the work would mean watching a silent terminal
for the length of the test suite, which reads like a hang.

## What the last run found

Every command leaves a record, inside the git directory rather than in the
worktree:

| Path | What is in it |
|---|---|
| `<git-dir>/sdlc/last-<command>.json` | each check, its exit, its seconds, its log |
| `<git-dir>/sdlc/logs-<command>/` | what each buffered check printed |

```
$ jq -r '.checks[] | "\(.seconds)s \(.check) exit \(.exit)"' .git/sdlc/last-push.json
```

A check's `exit` is its status: 0 passed, 1 found something, 2 could not run,
3 deferred to CI, 4 SKIPPED -- reported only by `container-smoke`, for a
systemd PID 1 it cannot boot, and `containerfile-lint`, for a Containerfile
hadolint cannot parse -- and 5 an ADVISORY finding from an ungated
`container-smoke`. A 4 or a 5 never decides the command's own exit.

The checks appear in the order they **finished**, which with lanes is not the
order they were listed in. A check that streamed to your terminal has no log
file: the terminal had it.

The record is written, never read. Nothing in this tool consults it to decide
anything, because a verdict comes from running the check — if a file could
stand in for one, a stale or hand-edited file would be a way past a gate.
Failing to write it never fails a gate either; a repo with nowhere to write
simply has no record.

If no check finishes for `SDLC_LANE_STALL` seconds (default 3600) the printer
stops waiting on the order, says so, waits for every lane to stop and then
prints what is left. That guards against a lane killed outright — SIGKILL, the
OOM killer, a cancelled CI job — which runs no cleanup and so never reports at
all, leaving the printer waiting for a report that is never coming. One check
honestly taking longer than the threshold reaches it too; raise the variable if
that is your repo. Either way only the order is given up, never the verdict:
every lane's real exit status is still collected.

## Configuring it

One rule for all three blocks:

- `checks` **adds** to that command's defaults; `skip` removes from them.
  What push skips, ci skips too.
- **`[tool.sdlc.ci] skip` may not name a push verdict** — lint, mypy, pytest,
  semgrep, pip-audit, constitution or codeql. Constitution XII says CI runs
  push's checks except Gourmand and Gatehouse, and this tool decides which two
  those are. Skipping a verdict in ci alone leaves it on pre-push, which is a
  developer's machine and one `--no-verify`, while the required `sdlc ci`
  reports green. If a check should not run, take it out of
  `[tool.sdlc.push]` — then the choice is in one place. A check the repo added
  itself may be skipped in ci freely.
- A check that needs settings gets its own table:
  `[tool.sdlc.<command>.<check>]`.
- A check that is not built in must say what `kind` it is.
- Check names are unique, and `sdlc <name>` runs any one of them — the inner
  loop.

Paths are not set here. Each tool reads its own config: `[tool.ruff]` excludes,
`[tool.mypy] files`, `[tool.pytest.ini_options] testpaths`.

A typical repo writes nothing. Trentina writes this:

```toml
[tool.sdlc.ci]
checks = ["image", "image-tests"]

[tool.sdlc.ci.image]
kind = "image-build"
build_args = { VERSION = "@project.version" }
labels = { "org.opencontainers.image.revision" = "@git.sha" }

[tool.sdlc.ci.image-tests]
kind = "pytest-in-image"
tests = ["tests/image/"]
```

### Built-in checks and their settings

| Check | Settings |
|---|---|
| `pytest` | `env` (table), `args` (default `["-q", "-rfE"]`) |
| `semgrep` | `config` (default `.semgrep`), `tests` (default `.semgrep/run-tests.sh`), `paths` (default `["."]`) |
| `codeql` | `config` (default `.github/codeql/codeql-config.yml`), `baseline` (default `.sdlc/codeql-baseline.json`) |
| `constitution` | `manifest` (default `.specify/memory/constitution.md`) |
| `ruff-check`, `ruff-format`, `lint`, `mypy`, `gourmand`, `secret-scan`, `gatehouse`, `pip-audit`, `hooks-installed` | none |
| `containerfile-lint` | `containerfiles` (default: the root Containerfile or Dockerfile and every `[[tool.sdlc.images]]` one), `threshold` |
| `container-build` | the `image-build` kind's settings; `containerfile` defaults to the root one |
| `container-smoke` | `gate` (default `false`: findings are advisory), `seconds` (default 10), `args`, `env`, `network` (default `false`), `image` |
| `workflow-lint` | `gate` (default `false`: findings are advisory) |
| `container-scan` | `fail_on` (default `critical`), `only_fixed` (default `true`), `image` |
| `npm-lint`, `npm-test` | `packages` (default `["."]`) |
| `npm-audit` | `packages`, `level` (`info`, `low`, `moderate`, `high`, `critical`; default `high`), `omit_dev` (default `false`: devDependencies are audited too) |
| `bootc-lint` | `fatal_warnings` (default `false`), `skip` (lint names, see `bootc container lint --list`), `image` |
| `markdown-lint` | `globs` (default `["**/*.md"]`) |
| `link-check` | `globs` (default `["."]`, the repo as a directory), `external` (default `false`: `true` also fetches remote links, over the network; a timeout or a 429 is accepted, any other bad answer is a finding, and `.lycheeignore` is how a flaky host is excused) |

### Kinds, for a repo's own checks

**`image-build`** — `podman build`, leaving the image at a known tag for the
next check and for a CI publish step.

| Setting | Type | Default |
|---|---|---|
| `containerfile` | path in the repo | `"Containerfile"` |
| `context` | path in the repo | `"."` |
| `build_args` | table | none |
| `labels` | table | none |
| `tag` | string | `"localhost/<repo>:sdlc"` |

**`pytest-in-image`** — runs tests inside a built image, locked down the way
production runs it: no network, a read-only root, no capabilities. pytest is
installed into the cache volume at the versions the repo's `uv.lock` pins, not
added to the image.

| Setting | Type | Default |
|---|---|---|
| `tests` | list of paths | **required** |
| `mounts` | list of paths in the repo, mounted read-only | `["tests"]` |
| `env` | table | none |
| `args` | list | `["-q", "-rfE"]` |
| `image` | string | `"localhost/<repo>:sdlc"` |
| `network` | boolean | `false` |

**`command`** — runs one program in a nested container, with the repo mounted
read-only at `/repo`, a tmpfs `/tmp`, no capabilities and no network. `argv` is
a list of words, never a shell string: `argv[0]` is the entrypoint and the rest
are its arguments, exactly as written. A repo's own script runs by naming it
(`["bash", "tests/lint.sh"]`), so the code is a file a reviewer sees. The
environment starts with `CI=true`, `HOME=/tmp`, `PYTHONPYCACHEPREFIX=/tmp/pycache`
and git's `safe.directory=*`; `env` adds to it or replaces those. An `image` that
an `image-build` check in the same command tags runs after that build, in its
lane, which is how a test gets a runtime the gate image does not carry (another
Python, a browser).

| Setting | Type | Default |
|---|---|---|
| `argv` | list of strings | **required** |
| `image` | string | the gate's own image (node, npm, bash, gawk, git, python 3.14) |
| `workdir` | path in the repo | `"."` |
| `env` | table | none |
| `network` | boolean | `false` |

```toml
[tool.sdlc.ci]
checks = ["migrations", "py311"]

[tool.sdlc.ci.migrations]
kind = "command"
argv = ["node", "--test", "tests/migrations/*.test.mjs"]

[tool.sdlc.ci.py311]
kind = "command"
image = "ghcr.io/astral-sh/uv:0.13.7-python3.11-trixie-slim"
argv = ["uv", "run", "--locked", "--all-extras", "pytest", "-q", "-p", "no:cacheprovider"]
env = { UV_PROJECT_ENVIRONMENT = "/tmp/venv", UV_PYTHON_DOWNLOADS = "never" }
network = true
```

Any exit other than 0 fails the check; 1 is a finding and anything higher is
reported as could-not-run, which fails it too.

**`command` with `systemd = true`** — the same command against a running
service, for a test that needs the image's units up (a database, the API). The
launcher starts `image` detached with systemd as PID 1, execs `ready` every two
seconds until it exits 0 or `timeout` is up, then execs each `setup` argv in
order and `argv` last, in the one container, and removes it however that ended.
The first step that does not exit 0 ends the check, judged as `argv` is; a
service that never becomes ready is a finding, with systemd's failed units and
the container log printed. Every step is an argv list as above, so a loop or a
pipe is a script in the repo that a step names. The steps run in `workdir` with
`env`; the repo is read-only at `/repo`, and there is no network but the
container's loopback unless `network = true`.

systemd gets back exactly the capabilities it needs to run units as their own
users — `SETUID`, `SETGID`, `SETPCAP`, `CHOWN`, `FOWNER`, `DAC_OVERRIDE` and
`KILL` — plus `NET_BIND_SERVICE` for a unit that listens below 1024, and keeps
`no-new-privileges` except on an AppArmor host (#181). They are the launcher's to grant, not the config's. The
`setup` steps and `argv` together get 30 minutes once the service is ready; a
step still running then is a finding and the container is removed. On Actions,
workflow commands are stopped from boot until the container is gone. A
cancelled gate removes the container on its way out, and conmon kills a service
still running after an hour, so a gate killed outright leaks nothing for long.

| Setting | Type | Default |
|---|---|---|
| `systemd` | boolean | `false` |
| `image` | string | **required** with `systemd`: the gate's own image does not boot systemd |
| `setup` | list of argv lists | none |
| `ready` | list of strings | `["systemctl", "is-system-running"]` |
| `timeout` | seconds, 1-1800 | `180` |

```toml
[tool.sdlc.ci]
checks = ["backend", "app-image"]   # the build is planned first either way

[tool.sdlc.ci.app-image]
kind = "image-build"
tag = "localhost/app:test"

[tool.sdlc.ci.backend]
kind = "command"
systemd = true
image = "localhost/app:test"
ready = ["curl", "-sf", "-o", "/dev/null", "http://localhost:8080/health"]
setup = [
  ["psql", "-U", "postgres", "-d", "app", "-f", "/repo/tests/fixtures/seed.sql"],
  ["systemctl", "restart", "app"],
]
argv = ["npm", "--prefix", "/app", "test"]
```

Paths must stay inside the repository; `..` or an absolute path is refused.
Values in `build_args`, `labels` and `env` may be `@project.version`,
`@git.sha` or `@now`.

There is no shell kind. `command` takes an argv list and nothing parses it; a
repo that needs something these cannot express gets a new kind here, so the
config stays something a person can read.

## Running main before a release

Every push to main publishes its tested image as `quay.io/crunchtools/sdlc:main`,
the same name git and Gourmand use: what has merged and not yet been released.
It is the digest the contract test ran against at `:<sha>`, read back after the
copy, and it never moves back onto an older commit. Only a release moves `latest` and the version tags, and no
pull request run touches a registry at all. The release is the gate.

`quay.io/crunchtools/sdlc` is the image. `quay.io/crunchtools/python-sdlc` is a
transitional mirror carrying the same signed digest, retired once the fleet has
moved.

A floating repo opts its CI into `:main` with the repository variable
`SDLC_IMAGE=main` -- a setting only an admin can change, rather than a one-line
edit a pull request could slip in. It is not a wall: a pull request from a
branch can still edit `ci.yml` or `.sdlc/bin/sdlc` themselves, as it always
could edit its workflows; an org ruleset's required workflow is what would
close that. To run one on your machine, first matching wins:

| How | Value |
|---|---|
| `SDLC_IMAGE=main git push` | a bare tag of `quay.io/crunchtools/sdlc` (`main`, or a release such as `0.8.0`), or a full image reference |
| `[tool.sdlc] image = "main"` | a bare tag only, written on its own line under `[tool.sdlc]` |

Every command on anything but the pinned release prints which image it is and
what chose it. A registry tag is pulled when newer, so `:main` is the current
`:main`, not whatever was cached.

**CI never honours `[tool.sdlc] image`.** Under `CI` or `GITHUB_ACTIONS`, and
for `sdlc ci` anywhere, the key is refused with exit 2. A pull request must not
choose the image that judges it, and only the launcher can enforce that: the
planner runs inside whichever image was chosen.

**Nothing built on a laptop or a seat is ever pushed.** To try a change to
this image before it merges, build it locally (`podman build -t
localhost/sdlc:dev .`) and run it with
`SDLC_IMAGE=localhost/sdlc:dev` through this clone's `bin/sdlc`. A
converted repo's `.sdlc/bin/sdlc` will not take it: the bootstrap runs what it
pulls, so it pulls only `quay.io/crunchtools/sdlc` at `main`, `latest`, a
release `X.Y.Z` or `@sha256:<digest>`, and refuses anything else with exit 2.
`sdlc` has no push step, and only this repo's GitHub Actions on main publish to
quay.io.

## When :latest is broken

A floating repo runs whatever `:latest` is, so a bad release reaches every repo
at once. Until a fixed release ships:

- **A local commit or push:** `git commit --no-verify` / `git push --no-verify`.
  CI still judges the pull request.
- **CI, fleet-wide:** set the organization variable `SDLC_IMAGE` to the last
  good release, `X.Y.Z`. Every floating repo's CI picks it up on its next run,
  with no pull request; a repository variable of the same name does the same
  for one repo. Clear it once the fix is on `:latest`.
- **A seat:** `SDLC_IMAGE=X.Y.Z git push`.

The fallback needs a release that carries the launcher. The bootstrap asks the
image for it, so a tag older than the first release with `sdlc launcher` cannot
be pinned this way: it exits 2.
Rolling back works only once two releases carry `launcher` -- the broken one
and a good one before it.

## Gatehouse and the remote

Gatehouse sends the diff to an external model, so in every tier, and when run by
hand as `sdlc gatehouse`, it runs only when the remote is a crunchtools repo on
GitHub. The accepted forms, each with an optional `.git` and one trailing
slash, the owner `crunchtools` in any case, and the repo one `[A-Za-z0-9._-]`
segment (not `.` or `..`):

- `https://github.com/crunchtools/<repo>`
- `ssh://git@github.com/crunchtools/<repo>`
- `git@github.com:crunchtools/<repo>`
- `http(s)://trentina[:port]/proxy/git/github/crunchtools/<repo>`, and the same
  on `trentina.crunchtools.com`, the gateway's own names

Scheme and host are lower-cased and compared exactly; https and ssh may carry
one `user[:pass]@` and a digits-only port. A URL containing `#`, `?`, `%`, a
backslash, whitespace or a control character is rejected. Any other remote, or
none found, prints one `GATEHOUSE SKIPPED` line naming the remote (userinfo
masked) and exits 0: a policy verdict, not a failure and not a 2. The remote is
`PRE_COMMIT_REMOTE_URL` from pre-commit, else the branch's upstream remote,
else `origin`.

`SDLC_GATEHOUSE_ANY_REMOTE=1` (exactly `1`) overrides the guard for a deliberate
hand run against another remote, and prints `GATEHOUSE GUARD OVERRIDDEN`. No
hook sets it; do not set it in one.

This is a cooperative client-side guard against accidents, not a security
boundary. The scp and ssh forms trust the literal host `github.com`, which
`~/.ssh/config` can redirect, and the remote's name says nothing about where the
server really is.

## Skipping a check

`SDLC_SKIP=gatehouse git push` skips one check, once, for a false positive. The
finding still has to be answered on the pull request.

## `sdlc event`, in CI

Each job of the fleet's one `ci.yml` (`examples/ci.yml`, designed in
`docs/marketplace-free-ci.md`) runs `sdlc event` in the `sdlc-bootstrap` container.
It routes on `GITHUB_JOB` to `sdlc_jobs/<job>.py` -- `check`, `image`,
`promote`, `pypi`, `write` -- and passes the job `GITHUB_EVENT_NAME`, the payload
at `GITHUB_EVENT_PATH` and the environment. A job with no module is a 2 that
names it, and `write` with `DIGEST` set (sdlc's own rollback dispatch) exits 0
having done nothing. It is not for a laptop: outside a workflow it is a 2.

## Exit codes

```
0   ran, found nothing
1   ran, found something
2   COULD NOT RUN
```

`2` is never treated as a pass. A gate that cannot run and reports success is
worse than no gate.

## CodeQL fails only on new findings

`codeql` fails on any finding not in `.sdlc/codeql-baseline.json`. The baseline
is the findings someone has looked at and accepted, committed where a reviewer
sees it. `sdlc codeql-baseline` writes it from the current findings. See
`docs/codeql.md`.

## What is in the image

| Tool | Pinned by |
|---|---|
| ruff, semgrep, pip-audit | this image |
| Gourmand, Gatehouse | this image, copied from their official images |
| CodeQL | this image (the Python-only bundle, 110 MB rather than 1.75 GB) |
| hadolint, grype, lychee | this image, copied from their official images |
| markdownlint-cli2 | this image, from npm at an exact version |
| constitution validator | the repo — cloned at the release its own manifest names |
| mypy, pytest | the repo's `uv.lock`, because they load the project's own plugins |

`sdlc versions` prints what the image carries.

## How a new Gourmand or Gatehouse arrives

Their releases trigger a rebuild here, which is released as a new sdlc
version and promoted to `sdlc:latest`. Every converted repo floats on that tag,
so the laptop then runs the reviewer CI runs, with no pull request per repo. A
repo still pinned by `rev:` moves only when it converts or bumps by hand.

## Host contract

A developer machine needs `git`, `podman`, `pre-commit` and `gh`. `bin/sdlc` is
POSIX shell, runs only `git` and `podman`, and refuses any step that is not a
`podman build` or `podman run`. `tests/test_host_contract.sh` proves it with a
restricted `PATH`, and proves the lane executor there too: that the lanes
really overlap, that a `2` in one beats a `1` in another whichever finishes
first, that a failure in one lane does not stop the other, and that no two
lanes' output interleaves.

## Licence

AGPL-3.0-or-later. The CodeQL bundle is redistributed under GitHub's CodeQL CLI
terms — see `docs/codeql.md`.
