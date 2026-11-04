# CodeQL in python-sdlc

## Why it fits at all

The full CodeQL bundle is **1.75 GB**. The Python-only one,
`codeql-bundle-python-linux64.tar.zst`, is **110 MB**. We analyse one language,
so the image takes the small one — and that single fact is what makes a local
CodeQL gate affordable instead of a 2 GB download per developer.

## Why it is at pre-push, not pre-commit

It builds a database. On trentina that is 747 modules in ~42 s, then ~55 query
evaluations, for a few minutes end to end. That is not a linter and cannot run
on every commit.

## Why findings do not block by default

`[tool.sdlc] codeql_gate` defaults to **false**, and this is parity with CI
rather than laxness.

GitHub's CodeQL Action uploads SARIF to code scanning. The `CodeQL Analysis`
check **passes whether or not there are findings** — each finding becomes an
alert in the Security tab, where it is triaged and often dismissed. Measured on
trentina's `main` at `c32a1dc9`, a commit whose CodeQL check is green: **19
findings**, including `py/clear-text-logging-sensitive-data` and trentina's own
`trentina/tool-arg-to-log`.

So a local gate that blocked on any finding would be strictly stricter than CI.
On trentina it would block every push until all 19 were fixed or suppressed,
which inverts the promise this whole image is built on — that a green push tier
means a green CI. A gate nobody can pass is a gate everybody skips, and
`SKIP=sdlc-codeql` becomes muscle memory.

**The honest gap:** without GitHub's dismissal state, the local run cannot tell
a known, accepted finding from a new one. So it reports rather than judges.

**The fix, which is the next piece of work here:** a committed baseline of
accepted findings, so only *new* ones block. That is how a repo gets a CodeQL
gate with teeth without first clearing a backlog. Until then,
`codeql_gate = true` is available for a repo whose findings are already at zero.

## The config file

`.github/codeql/codeql-config.yml` is the **Action's** format, not the CLI's.
Two things were tried and both failed on CodeQL 2.27.4:

- `database create --codescanning-config=…` — not a flag; `create --help` does
  not list it.
- `database analyze --codescanning-config=…` — rejected outright with
  `Unknown option`, despite `analyze --help` *describing* the flag ("if a Code
  Scanning configuration file was specified at database creation time using
  --codescanning-config"). That text documents the Action's internal flow.

So `sdlc` reads the config itself and applies it in two parts:

- **`queries:`** become arguments to `analyze`. A bare suite name is the
  Action's shorthand for this language's suite, so `security-extended` maps to
  `python-security-extended.qls`; an entry starting with `.` is a path to a
  local query pack and is passed through. trentina's config yields both, and its
  `trentina-queries` pack does run locally — `ToolArgToLog` and
  `ToolArgToHttpClientWithoutEgressCheck` both appear in the evaluation list.
- **`paths-ignore:`** filters the SARIF after the fact.

That second part matters more than it looks. trentina's `paths-ignore` excludes
`tests/fixtures/boundary-review`, which holds **deliberately vulnerable code**
used to grade a reviewer in its boundary-review eval. Analyse without honouring
it and the local gate reports a pile of findings CI does not. On the verified
run, 2 results were correctly suppressed this way.

## One base-image dependency worth knowing

CodeQL's own `python/tools/autobuild.sh` runs `which python3` to find an
interpreter. Hummingbird does not ship `which`, so without it `database create`
fails with "Could not find a valid Python distribution" on an image that
ships Python 3.14. The Containerfile installs `which` for exactly this reason.

## Licence

The CodeQL CLI and its query packs are redistributed here under GitHub's
[CodeQL CLI terms](https://github.com/github/codeql-cli-binaries/blob/main/LICENSE.md),
which permit use for analysing software. python-sdlc uses it to analyse
CrunchTools repositories, which is within those terms. python-sdlc's own code is
AGPL-3.0-or-later; the bundled CodeQL is not, and keeps its own licence.
