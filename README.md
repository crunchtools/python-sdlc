# python-sdlc

Every CrunchTools Python check in one container image, run the same way on a
laptop and in CI.

Constitution XII (v1.30.0) requires it for every Python repo — any repo with a
`pyproject.toml` — except `gourmand`, `gatehouse` and this one, which build what
the image contains.

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
- **ci** runs push's checks except Gourmand and Gatehouse, which CI already runs
  through their own workflows, plus the repo's own checks, such as building its
  image.

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

`.pre-commit-config.yaml`, the whole file:

```yaml
default_install_hook_types: [pre-commit, pre-push]

repos:
  - repo: https://github.com/crunchtools/python-sdlc
    rev: v0.3.4
    hooks:
      - id: sdlc-commit
      - id: sdlc-push
```

CI, one job, which reads the release from that same `rev:`:

```yaml
  sdlc:
    name: sdlc ci
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - name: sdlc ci
        run: |
          rev=$(awk '/repo: https:\/\/github.com\/crunchtools\/python-sdlc/ {f=1; next}
                     f && /rev:/ {print $2; exit}' .pre-commit-config.yaml)
          git clone -q --depth 1 --branch "$rev" https://github.com/crunchtools/python-sdlc "$RUNNER_TEMP/sdlc"
          $RUNNER_TEMP/sdlc/bin/sdlc ci
```

That `rev:` pins the launcher, the image and every tool inside it, for the
laptop and CI both. One pin.

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

## What a repo gets with no config at all

| Command | Runs |
|---|---|
| `commit` | ruff-check, ruff-format, gourmand, hooks-installed |
| `push` | mypy, pytest, gourmand, gatehouse, semgrep, pip-audit, constitution, codeql |
| `ci` | mypy, pytest, semgrep, pip-audit, constitution, codeql |

`sdlc tiers` prints what each command runs in the repo you are in, and which
lane each check runs in.

## Lanes

A command's checks run in lanes. Steps of one lane run in sequence; lanes run
at the same time, so a command takes about as long as its longest chain rather
than the sum of everything in it.

The image decides the lanes, by three rules:

- An `image-build` check and every `pytest-in-image` check that runs the image
  it tags are **one lane** — the build has to finish before the tests start.
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
| `ruff-check`, `ruff-format`, `mypy`, `gourmand`, `gatehouse`, `pip-audit`, `hooks-installed` | none |

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

Paths must stay inside the repository; `..` or an absolute path is refused.
Values in `build_args`, `labels` and `env` may be `@project.version`,
`@git.sha` or `@now`.

There is no shell kind. A repo that needs something these cannot express gets a
new kind here, so the config stays something a person can read.

## Skipping a check

`SDLC_SKIP=gatehouse git push` skips one check, once, for a false positive. The
finding still has to be answered on the pull request.

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
| constitution validator | the repo — cloned at the release its own manifest names |
| mypy, pytest | the repo's `uv.lock`, because they load the project's own plugins |

`sdlc versions` prints what the image carries.

## How a new Gourmand or Gatehouse arrives

Their releases trigger a rebuild here, which is released as a new python-sdlc
version, which opens a version-bump pull request in each Python repo. The laptop
then runs the reviewer CI runs.

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
