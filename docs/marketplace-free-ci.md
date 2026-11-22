# One workflow file, no marketplace actions

Design for a1051. Nothing here is implemented: it is the target that
`convert_repo.py` and `cutover.py` will be changed to write, and the list of
`sdlc` internals that have to exist first.

## The file: 68 lines, six jobs, every repo

Every repo carries exactly this `.github/workflows/ci.yml`, byte for byte,
whatever kind of repo it is. It is 68 lines without comments, and it contains
no `uses:` of any kind: no `actions/checkout`, no reusable-workflow call and no
third-party action. Every job runs the sdlc image and the one command
`sdlc event`. That command reads `GITHUB_EVENT_NAME`, the event payload and
`GITHUB_JOB`, and runs whatever that trust level does for that event in this
repo.

```yaml
# One file, every repo, copied unchanged. A job is a trust level, not a task:
# `sdlc event` reads the event and GITHUB_JOB and runs what applies.
name: CI
on:
  push: { branches: [main] }
  pull_request:
  pull_request_target: { types: [opened, synchronize, reopened, closed] }  # zizmor: ignore[dangerous-triggers]
  pull_request_review_comment:
  issues: { types: [opened, labeled, reopened] }
  workflow_run: { workflows: [CI], types: [completed] }  # zizmor: ignore[dangerous-triggers]
  release: { types: [published] }
  repository_dispatch:
  schedule: [{ cron: "17 6 * * 1" }]
  workflow_dispatch: { inputs: { version: { type: string } } }
permissions: {}
jobs:
  check:  # read-only, no secrets: safe on a fork's pull request
    name: sdlc / check (${{ github.event_name }})  # required as "(pull_request)"
    if: contains(fromJSON('["push","pull_request","release","schedule","repository_dispatch","workflow_dispatch"]'), github.event_name)
    runs-on: ${{ github.event_name == 'pull_request' && (github.event.pull_request.head.repo.full_name == github.repository && vars.CACHED_RUNNER_LABEL || 'ubuntu-latest') || vars.UNCACHED_RUNNER_LABEL || 'ubuntu-latest' }}
    container: { image: "${{ vars.SDLC_REF || 'quay.io/crunchtools/sdlc:latest' }}", options: --privileged }
    permissions: { contents: read, security-events: write }
    outputs: { result: "${{ steps.e.outputs.result }}" }
    env: { GITHUB_TOKEN: "${{ github.token }}" }
    steps: &event [{ id: e, run: sdlc event }]
  ai:  # model keys, read-only token, no App key; hands a patch or verdict on
    if: github.event_name == 'issues' || github.event_name == 'pull_request_target' && github.event.action != 'closed'
    runs-on: &runner ${{ vars.UNCACHED_RUNNER_LABEL || 'ubuntu-latest' }}
    container: &sdlc { image: "${{ vars.SDLC_REF }}" }
    permissions: { contents: read, issues: read, pull-requests: read }
    outputs: { result: "${{ steps.e.outputs.result }}" }
    env: { GITHUB_TOKEN: "${{ github.token }}", CLAUDE_CODE_OAUTH_TOKEN: "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}", OPENROUTER_API_KEY: "${{ secrets.OPENROUTER_API_KEY }}" }
    steps: *event
  write:  # the App key, no model: comments, labels, PRs, pushes, release PR and tag, retriage
    needs: [check, ai]
    if: >-
      always() && !cancelled() && !contains(fromJSON('["push","pull_request","repository_dispatch"]'), github.event_name)
      && (github.event_name != 'workflow_run' || github.event.workflow_run.event == 'pull_request_review_comment')
    runs-on: *runner
    container: *sdlc
    permissions: { actions: write }
    env: { ASHIGARU_CLIENT_ID: "${{ vars.ASHIGARU_CLIENT_ID }}", ASHIGARU_APP_KEY: "${{ secrets.ASHIGARU_APP_KEY }}", SDLC_IN: "${{ toJSON(needs) }}", VERSION: "${{ inputs.version }}" }
    steps: *event
  gate:  # required: workflow guard and every finding answered; base definition, no secrets
    name: sdlc / gate (${{ github.event_name }})  # required as "(pull_request_target)"
    needs: write
    if: always() && github.event_name == 'pull_request_target' && github.event.action != 'closed'
    runs-on: *runner
    container: *sdlc
    permissions: { contents: read, pull-requests: write }
    env: { GITHUB_TOKEN: "${{ github.token }}" }
    steps: *event
  image:  # builds and gates the candidate, pushes it by digest; privileged, registry creds
    needs: check
    if: fromJSON(needs.check.outputs.result || '{}').image && github.event_name != 'pull_request' && !inputs.version
    runs-on: *runner
    container: { image: "${{ vars.SDLC_REF }}", options: --privileged }
    permissions: { contents: read }
    outputs: { result: "${{ steps.e.outputs.result }}" }
    env: { GITHUB_TOKEN: "${{ github.token }}", QUAY_USERNAME: "${{ secrets.QUAY_USERNAME }}", QUAY_PASSWORD: "${{ secrets.QUAY_PASSWORD }}" }
    steps: *event
  release:  # signs and promotes the digest, publishes to PyPI by OIDC; never builds
    needs: [check, image]
    if: always() && !cancelled() && (needs.image.result == 'success' || github.event_name == 'release')
    runs-on: ubuntu-latest
    environment: cosign-signing
    container: *sdlc
    permissions: { contents: read, id-token: write }
    env: { SDLC_IN: "${{ toJSON(needs) }}", QUAY_USERNAME: "${{ secrets.QUAY_USERNAME }}", QUAY_PASSWORD: "${{ secrets.QUAY_PASSWORD }}", COSIGN_PRIVATE_KEY: "${{ secrets.COSIGN_PRIVATE_KEY }}", COSIGN_PASSWORD: "${{ secrets.COSIGN_PASSWORD }}" }
    steps: *event
```

**Why 68 lines and not 20.** GitHub makes a separate job only where the
permissions, the secrets, the OIDC grant or the runner differ. The table below
shows six such trust levels. Each job needs its `if`, `runs-on`, `container`,
`permissions`, `env` and `steps` lines, which comes to about 7 lines a job, and
`on:` needs 11 more. So 68 lines is close to the floor for six trust levels.
Merging any two jobs would put a secret or a write grant somewhere the design
says it must not be. Every version and every task is already out of the file:
the image digest is `vars.SDLC_REF`, and the routing is `sdlc event`. YAML
anchors (`&event`, `&sdlc`, `&runner`) save the repeats. GitHub accepts anchors
but not merge keys (`<<`), and it rejects unknown top-level keys. That is why
the anchors are defined inside the first job that uses them, and why `env:` is
written out in each job.

**The image-only repo (ubi10-core) runs the same file.** Its `check` reports
`image: true` and `publish: false`, so `image` and `release` build, sign and
promote, and nothing is sent to PyPI. A Python library reports the reverse. A
docs repo reports neither, and those two jobs are skipped without starting. A
`parent-image-updated` dispatch arrives as `repository_dispatch`, which `check`
routes to a rebuild. Nothing about a repo's kind lives in the file, so
`convert_repo.py` never edits it and the fleet cannot drift.

**Bottom line.** Of the 70 org repos, 69 have workflows today. Every one of
them carries at least one third-party pin (`actions/checkout`, at least), and
most carry 7 to 9 workflow files. Under this design all 69 carry one file and
no `uses:`. That depends on three things:
- the hard cases below are accepted as designed;
- mcp-atlassian deletes the two upstream workflows its fork carries;
- spike #103 passes.

The constitution holds the policy and the sdlc image holds every tool and
version. Renovate bumps those versions in ordinary files in this repo
(`Containerfile`, `uv.lock`, `package-lock.json`), so it never needs the
GitHub App **Workflows** permission.

## Jobs are trust levels

| Job | Check name | Holds | Runs on | Never holds |
|---|---|---|---|---|
| `check` | `sdlc / check (pull_request)` (required) | read token, `security-events` | push, pull_request (forks too), release, schedule, dispatch | any secret |
| `ai` | (not a check anyone requires) | Claude and OpenRouter keys, read token | issues; `pull_request_target` (not closed) | an App key, a write token |
| `write` | | Ashigaru App key (sdlc mints the token), `actions: write` | after `check`/`ai`: `pull_request_target`, issues, release-PR dispatch, schedule, the reply doorbell (`workflow_run` of a review-comment run) | a model |
| `gate` | `sdlc / gate (pull_request_target)` (required) | `pull-requests: write` | `pull_request_target` (not closed), after `write` | any secret, the PR's code |
| `image` | | quay credentials, `--privileged` | after a green `check` that reports an image, on anything but a pull request | the cosign key |
| `release` | | cosign key (`cosign-signing` environment), `id-token: write` | after `image`, or on `release` | a build: it promotes a digest someone else built |

The splits that are forced:
- **`ai` / `write`:** this is the security split, "the credential that can
  write never shares a machine with the LLM" (crunchtools/ashigaru,
  `.github/workflows/code.yml`). `ai` passes its result (a verdict, review
  comments, or a patch, gzip and base64) to `write` as a job output, which is
  capped at 1 MB.
- **`gate`:** it must come from the base definition under
  `pull_request_target` and hold no secret, so it can be required.
- **`image` / `release`:** they keep the signing key off the machine that runs
  a Containerfile's `RUN` lines.
- **`check`:** it is the only job a fork's pull request can run.

## What `sdlc event` runs

This is sdlc's routing table, not the file's. It is listed by job, then event.

| Job | Event | Runs | Was |
|---|---|---|---|
| check | push, pull_request | `ci` (CodeQL SARIF uploaded on the default branch), `gourmand`, `constitution` with zizmor (SARIF uploaded) | ci.yml, constitution.yml, security.yml CodeQL |
| check | schedule, push to default | `audit`; findings go out as output for `write` to raise the alert issue | dependency-audit.yml |
| check | release, schedule, dispatch, repository_dispatch | plans the image build and publish (`result.image`, `result.publish`) | container.yml `resolve` |
| ai | pull_request_target | Gatehouse review of the diff (read through the API, never checked out). On an `ashigaru/` branch in this repo it also runs Ashigaru `fix`, which answers this review's findings and the open ones from earlier rounds, up to five rounds | review.yml, fix.yml |
| ai | issues | Ashigaru `triage`, then `code` when the verdict is ready-to-code, in the same job | triage.yml, code.yml |
| write | pull_request_target | posts the review; pushes the fix and its answers; opens nothing | review.yml / fix.yml publish |
| write | pull_request_target closed, merged `ashigaru/release-*` from this repo | tags the merge and publishes the GitHub Release | release-tag.yml |
| write | issues | labels, and opens the `ashigaru/issue-N` pull request | triage.yml apply, code.yml publish |
| write | workflow_dispatch with `version` | opens the release pull request from `chore/release-X.Y.Z` | release.yml open |
| write | workflow_run of a review-comment run | re-runs `gate` in that PR's `pull_request_target` run | gatehouse-retriage.yml |
| write | schedule | keeps the audit alert issue | dependency-audit.yml |
| gate | pull_request_target | workflow guard (it fails, and comments, if a fork touches `.github/workflows/` or `.sdlc/`), then Gatehouse triage | gatehouse.yml guard, triage.yml |
| image | not a PR | `podman build`, the container gates, push to `gated-<sha>` by digest | container-release build, gates, mark |
| release | after image, or release | sign, attest, move tags, write nothing else; and on `release`, `uv build` + `uv publish` by OIDC | container-release promote, publish.yml |

Merging review, triage, code and fix into one `ai` job is safe because every
one of them already held the same thing: a model key and a read-only token.

## Required checks: from five to two

| Today (org rulesets) | Proposed |
|---|---|
| `sdlc ci`, `Code Quality (Gourmand) / Code Quality (Gourmand)`, `Constitution / Validate constitution` | `sdlc / check (pull_request)` |
| `Protect workflows`, `Gatehouse triage / Gatehouse triage` | `sdlc / gate (pull_request_target)` |

**The event is part of the name, and that is load-bearing.** A job that its
`if:` skips still reports a check, and a skipped check counts as passing. In
one file every run of every event carries every job, so a `pull_request` run
would post a skipped `sdlc / gate` on the same head commit, and a
`pull_request_target` run a skipped `sdlc / check`. Either would satisfy a
required check that never ran. With the event in `name:`, the skipped `gate`
in a `pull_request` run is `sdlc / gate (pull_request)`, which nothing
requires. `jobs.<id>.name` may read the `github` context
([contexts](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts)).
If GitHub does not expand the expression for a skipped job, the literal text
still differs from the required name, so the guarantee holds either way. Only
a `pull_request` run can produce the one required name, and only a
`pull_request_target` run the other. Within its own event, neither job can
reach a no-op: `check` always runs `sdlc ci`, and `gate` always runs the guard
and triage. Exedore 1 is checking how GitHub chooses between check runs of the
same name, and the spike (#103) confirms the rest.

The ruleset edit is made at cutover, in the same sitting as the wave. Until a
repo has converted, its old check names keep it mergeable. Required checks are
per name, so both sets can be required while the fleet is half moved, as long
as each list is in its own ruleset, targeted at the repos that produce it.
Running two rulesets during the wave is the safe order. The non-required checks
that go away are `CodeQL Analysis`, `Dependency CVE Scan`, the container jobs,
Ashigaru's jobs and `Dependabot`.

One thing that is true today and stays true: a fork's `pull_request` run
executes the fork's copy of `ci.yml`, so it could add a job named
`sdlc / gate (pull_request_target)`. The fork-run approval setting ("Require approval for all
outside collaborators") is what stops a fork's workflow from running at all
until a maintainer has read it. Check that it is on org-wide. This design adds
no new exposure.

**One more cutover step.** PyPI's trusted publisher names the workflow file
and the environment. Each of the 22 PyPI projects has to be re-pointed from
`publish.yml` to `ci.yml`, environment `cosign-signing`, before its first
release after cutover. Otherwise that upload is refused.

## What GitHub will not let us do this way

Each item names what breaks, the evidence, and what the design does instead.

**1. Artifacts cannot be made or read from a `run:` step.** The runner gives
`ACTIONS_RUNTIME_TOKEN` and `ACTIONS_RESULTS_URL` to JavaScript actions only:
`NodeScriptActionHandler.cs` sets both, `ScriptHandler.cs` (the `run:` handler)
sets only `ACTIONS_ID_TOKEN_REQUEST_URL` and `_TOKEN`
([runner source](https://github.com/actions/runner/tree/main/src/Runner.Worker/Handlers)).
The REST API lists, downloads and deletes artifacts and has no upload
([docs](https://docs.github.com/en/rest/actions/artifacts)). *Instead:* job
outputs for small data, which are capped at 1 MB per job and 50 MB per run
([workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)),
the registry for images, and merged jobs where no trust boundary separates
them. The case-by-case list is under "The hard cases".

**2. No cache.** The cache REST API has no create
([docs](https://docs.github.com/en/rest/actions/cache)), and the cache service
uses the same runtime token. *Instead:* nothing to cache, because the tools are
in the image.

**3. Pages cannot deploy from a `run:` step.**
`POST /repos/{owner}/{repo}/pages/deployments` requires an `artifact_id` or
`artifact_url` ([docs](https://docs.github.com/en/rest/pages/pages)), which
only an artifact upload produces (item 1). *Instead:* branch publishing:
"You can configure your GitHub Pages site to publish when changes are pushed
to a specific branch"
([docs](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)).

**4. GitHub cannot verify a signature on a `container:` image.** The `container:`
keys are `image`, `credentials`, `env`, `ports`, `volumes` and `options`, and
none of them is a signature policy
([docs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/run-jobs-in-a-container)).
*Instead:* name the image by digest. `jobs.<id>.container` may read `vars`
([contexts](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts)),
so `SDLC_REF` is a signed digest that only promote writes. See "Signature
verification" below.

**5. `workflow_run` is the only way to run privileged work after a fork's
reply.** "The workflow started by the `workflow_run` event is able to access
secrets and write tokens, even if the previous workflow was not"
([events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run)).
A fork's `pull_request_review_comment` run is read-only, and its result does not
count toward the ruleset (gatehouse#50, quoted in `retriage.yml`). In one file,
`workflow_run` has to name the file's own workflow, `CI`. The docs neither allow
nor forbid that. They cap chains: "You can't use `workflow_run` to chain
together more than three levels of workflows". The cost is real: every
completed CI run starts one more CI run in which every job is skipped, up to
that cap. `write`'s `if` makes those runs start no job, so the cost is run
records, not runner time. **This needs a spike before the template ships**
(#103). If GitHub refuses the self-reference, the doorbell becomes a second
file, `retriage.yml`, with one job that runs `sdlc event` and still no
`uses:`. That is the one place the one-file rule might bend.

**6. Who runs an event's workflow.**
- `pull_request_target` runs "in the context of the default branch of the base
  repository", so its jobs are the base's definitions. That is why `ai`,
  `write` and `gate` run there, and never under `pull_request`.
- `pull_request` and `pull_request_review_comment` run the merge commit's copy
  of `ci.yml` ([events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)).
  So a fork can edit the jobs in its own run, as it can today. `gate` refuses
  the change, and only `check` runs under `pull_request`, holding nothing.

Nothing changes here. Moving into one file does not move the boundary.

**7. Ashigaru `fix` no longer needs `workflow_run`.** Today it listens for
Gatehouse's completion. In one file it runs inside `ai`, right after the
review, in the same `pull_request_target` run. It runs on `ashigaru/` branches
in this repo only, and answers that review's findings and the open ones left
from earlier rounds. That is one trigger fewer, and the bound of five rounds
stays in sdlc.

**8. Fork pull requests may not see `vars`.** GitHub's docs do not say whether
an org variable reaches a fork's `pull_request` run, and a community thread
says it does not. `check`, the only job a fork's `pull_request` runs, falls back to
`quay.io/crunchtools/sdlc:latest`, so a fork's run still starts, on the
unverified tag. That run holds no secret and no write token, so the fallback
costs a verification and grants nothing. The spike checks it.

**9. Check names.** A required check is matched by name, and a plain job's
check takes the job's `name:`. The constitution records that a reusable
workflow's checks are named `<caller job> / <called job>` instead (#22,
section "Gate wiring"), which is why today's names carry a slash. The
proposal replaces all five with two, `sdlc / check (pull_request)` and
`sdlc / gate (pull_request_target)`, at
cutover, with the event in each name. See "Required checks" above.

**10. CodeQL licence.** The CodeQL CLI may "generate CodeQL databases for or
during automated analysis, CI, or CD" for an Open Source Codebase hosted on
GitHub.com. It may not be used "in connection with any codebase that is not an
Open Source Codebase (e.g., code in a private repo in GitHub)" without Advanced
Security ([LICENSE](https://github.com/github/codeql-cli-binaries/blob/main/LICENSE.md)).
The same terms bind `codeql-action`, so moving off it changes nothing.
But `sdlc` runs CodeQL in every Python repo today, private ones included, and
a private repo without Advanced Security is outside the licence. `sdlc ci`
should skip CodeQL there (it can read `github.event.repository.private`). That
is worth fixing whatever this design becomes.

**11. PyPI trusted publishing works from a `run:` step.** `ScriptHandler.cs`
sets the OIDC request variables for `run:` steps, and uv's docs publish with a
plain `run: uv publish` under `id-token: write`
([uv](https://docs.astral.sh/uv/guides/integration/github/)). Inside a
`container:` job the runner passes the step environment through. That is
inferred from source and not yet run.

## Every action in use, and its replacement

The `sdlc <step>` names below are internal steps that `sdlc event` runs. They
are not commands any workflow file names.

The inventory covers the sdlc templates, the constitution, ashigaru and
gatehouse workflows, and every other repo in the org (fresh clones of the
default branches, 2026-11-22). Counts are repos / `uses:` lines.

| Action | Where | Replacement | In the image today? |
|---|---|---|---|
| `actions/checkout` | 68 / 221: every repo | sdlc fetches: `git init`, then `git fetch --depth=1` of the one commit the event names, authenticated with `GITHUB_TOKEN` by a per-command `http.extraheader`, so nothing persists. The commit is sdlc's choice per event, never the caller's. | git: yes. The fetch logic: no |
| `astral-sh/setup-uv` | 25 / 48: publish, ci, constitution audit, ashigaru publish | uv is baked in (`UV_VERSION`) | yes |
| `actions/setup-python` | constitution: 5 | the image's Python 3.14 and its constitution venv | yes |
| `actions/setup-node` | acquacotta, constitution, rotv | the image's node 24 | yes |
| `github/codeql-action/init`, `analyze` | 24 / 48: `security.yml` | `sdlc ci` already runs CodeQL (Python bundle). Add `codeql github upload-results` for the Security tab, on push to the default branch. A repo that wants `security-extended` says so in `[tool.sdlc]`. | CLI: yes. Upload: no |
| `github/codeql-action/upload-sarif` | constitution (zizmor), rotv, trentina | `codeql github upload-results`, which takes any SARIF | yes (the CLI) |
| `pypa/gh-action-pypi-publish` | 22: `publish.yml` | `sdlc publish pypi`: `uv build`, then `uv publish --trusted-publishing always` | uv: yes. Command: no |
| `actions/create-github-app-token` | 7 / 18: sdlc release templates, ashigaru, constitution fleet jobs, gatehouse | sdlc mints the token itself: an RS256 JWT from the App key, then `POST /app/installations/{id}/access_tokens` with the repositories and permissions the command needs. It masks the token (`::add-mask::`) and revokes it (`DELETE /installation/token`) when the command exits, which is what the action's post step does. | no: needs `cryptography` (or `PyJWT`) in its own venv |
| `actions/upload-artifact`, `download-artifact` | 6 / 48 | see "Artifacts between jobs" | n/a |
| `anthropics/claude-code-action/base-action` | ashigaru: 6 | the `claude` CLI (`@anthropic-ai/claude-code`, exact version, npm), run by `sdlc ashigaru *` with the same `--model`, `--max-turns`, `--allowedTools`, `--json-schema` and settings the action is given | no |
| `anthropics/claude-code-action` | mcp-atlassian `claude.yml` (inherited from upstream) | delete; Ashigaru is the fleet's agent | n/a |
| `dependabot/fetch-metadata` | 2: automerge | delete. Renovate's own automerge replaces it, and from constitution v1.60.0 Dependabot fails validation anyway | n/a |
| `docker/login-action`, `setup-buildx-action`, `build-push-action` | trentina `l2-train`, rotv `tests` | `podman login` / `podman build` / `podman push` in `sdlc image` or a repo-defined command | no: podman, buildah, skopeo and cosign have to come in |
| `actions/upload-pages-artifact`, `deploy-pages` | packages `publish.yml` | `sdlc pages`: push the built site to a `gh-pages` branch, Pages source set to "Deploy from a branch" | no |
| `actions/stale` | mcp-atlassian (upstream) | delete | n/a |
| `OWASP/cve-lite-cli` | rotv | grype and `npm audit`, both already in `sdlc ci` | yes |

`container:` keys today: ashigaru `ci.yml` (`fedora:45`, a py3.15 canary),
gatehouse `gourmand.yml`, and the RPM/deb matrices in claude-agent, petit and
packages (`almalinux:8`, `almalinux:9`, `debian:12`). Under this design each
becomes an sdlc command that runs that distro with podman inside the job, and
the list of distros lives in the repo's `sdlc.toml` or `pyproject.toml`. That
is an ordinary file, so Renovate can bump an image pinned there without the
Workflows permission.

### What the image has to gain

Every item below is an ordinary pinned version in this repo's `Containerfile`,
`uv.lock` or `package-lock.json`, so Renovate bumps it with Contents
permission alone. `scripts/update_tools.py` and `update-tools.yml` are retired
in favour of Renovate `customManagers` on the `ARG` lines.

- **podman, buildah, skopeo**: Hummingbird RPMs, for `sdlc image`, the RPM
  matrices and `podman login`.
- **cosign v2.6.5** (copied from its digest-pinned image), and **syft**: for
  `sdlc image promote`. They do what `scripts/sign-digest.sh` and
  `scripts/attest-digest.sh` already do in sdlc's own `container.yml`.
- **zizmor**: today it is installed by hash in the constitution's
  `requirements/zizmor.txt`.
- **claude**: the CLI from npm, at an exact version, under its own prefix as
  markdownlint-cli2 is.
- **cryptography** (or PyJWT): in an isolated venv, for App tokens.
- **gh** is not needed: sdlc calls the REST API itself, as `release_publish.py`
  and `update_tools.py` already do.

## The hard cases

**Artifacts between jobs.** These go away by design, not by workaround. Today's
hand-offs, and what each becomes:

- **Ashigaru agent → publish** (`change.patch` plus `result.json`; KBs of
  text). This becomes a job output: gzip, then base64, then
  `$GITHUB_OUTPUT`. The output is capped at 1 MB per job. A model's change that
  is still over 1 MB compressed is refused as too large to review, which it
  is. The split between the two jobs stays.
- **container-release build → gates** (`image.tar`, 100 MB–1 GB). This only
  happens on the path where `pushed == false`, a pull request with no registry
  credentials. With Dependabot gone, that is a fork's pull request alone. In
  the new file, no pull request reaches `image` at all. `check` builds and
  gates the image in place (the container stack of `sdlc ci`), holding no
  credential, so there is nothing to hand on. Outside pull requests, `image`
  pushes the candidate to quay by digest, and `needs.image.outputs` carries
  that digest to `release`.
- **RPM build → publish** (ashigaru, claude-agent, petit; a few MB per
  distro). The split exists to keep `contents: write` off the job that runs
  `build.sh`. On a pull request, the build job holds nothing and publishes
  nothing. On a `release` event the code being built is the merged,
  Gatehouse-reviewed tag, so build and upload run in one job with
  `contents: write`, and sdlc uploads the release assets through the REST API.
- **trentina `l2-train`** (a model file). This becomes one job, or a push to
  ghcr as an OCI artifact, chosen when that workflow is converted. It is
  trentina-specific and is not part of the fleet template.
- **Uploads with no consumer** (constitution's audit JSON, trentina's
  benchmark, demo and eval reports, rhel10-bootc-server's raw disk). These are
  for people. They go to the job summary (`$GITHUB_STEP_SUMMARY`), the alert
  issue, or a release asset. rhel10-bootc-server's disk image (GBs) is the one
  that needs an OCI artifact on quay.

**actions/cache.** No repo uses it. The tools are in the image, so nothing is
downloaded to cache. `uv sync` fetches a repo's own dependencies on each run.
That is the cost today too, and the cached self-hosted runner keeps its layer
cache.

**Build attestations.** No repo uses `actions/attest*`. Images are attested with
cosign and the fleet key (`attest-digest.sh`: SPDX SBOM and SLSA v1
provenance), and that moves into `sdlc image promote` as it is. GitHub's
attestation store is not used, and nothing is lost.

**GitHub Pages.** Only `crunchtools/packages` deploys Pages.
`actions/deploy-pages` needs an artifact made by `upload-pages-artifact`, and
neither can run from a `run:` step. The repo moves its Pages source to
"Deploy from a branch", and `sdlc pages` pushes the built tree to `gh-pages`
with the job's token (`contents: write`, on release or push to the default
branch only). That is a one-time setting change on that repo.

**`container:` jobs on gha-runner.** They already work: the runner points
`/var/run/docker.sock` at its rootless podman API (gha-runner README, "Job
containers and services"). The two things that are new:
1. **Jobs that need a container engine inside the job container**
   (`check` and `image`). These carry `options: --privileged`. On GitHub-hosted runners that
   is Docker's privileged mode on a throwaway VM. On gha-runner it is rootless
   podman, privileged only within the runner user's namespace, and inside it
   the same nested-podman setup the runner image already proves. This needs a
   spike on both runner kinds before the template ships. The fallback is to
   run those jobs on the host with the existing launcher.
2. **Per-check sandboxing.** Today the launcher runs each check in its own
   container with `--network=none --cap-drop=all`. In a job container the
   checks are processes. sdlc keeps `pytest` offline with
   `unshare --net` where the job is privileged, and otherwise says plainly in
   the log that the check ran with the network on. This is a real loss of
   isolation on GitHub-hosted runners. It is acceptable because the job
   container is itself a throwaway, but it is a loss.

**Signature verification moves from pull time to promote time.** The bootstrap
checks the cosign signature before it runs anything. A `container:` image is
pulled by the runner, and nothing verifies it. So CI names the image by
**digest**. The org variable `SDLC_REF` is
`quay.io/crunchtools/sdlc@sha256:…`, and only sdlc's promote job writes it,
after it signs that digest. The runner then pulls exactly the bytes that were
verified. Rollback is setting the variable back. A repo holds itself back with
a repo variable of the same name, which GitHub gives precedence. Writing an
org variable needs an App with organization **Variables: write**, which
belongs to the CRUNCHTOOLS_CI app rather than a person. Laptops keep the
bootstrap, `:latest` and verification. Promote moves `:latest` and `SDLC_REF`
together, so a green `sdlc push` still predicts a green `sdlc ci`.

## What stays as `uses:`

Nothing, in any repo. The four kinds that exist today all go:

- **Third-party actions**: replaced as in the table above.
- **Calls to crunchtools reusable workflows** (`validate.yml@v1`,
  `container-release.yml@v1`, `gourmand.yml@v0`, `review.yml@v0`,
  `router.yml@v5` and the rest): their logic becomes sdlc subcommands. While
  they still exist, XV rule 5 settles the original question: a crunchtools
  reusable workflow is called at its moving major tag (`@v1`), never by SHA.
  A SHA would be one more pin for Renovate to move with the Workflows
  permission, which is the thing this design removes. After the move the
  reusables are deprecated, kept at their tags for any straggler, and deleted
  a release later.
- **`docker://` steps**: none exist.
- **Composite actions**: none exist, and none are needed.

The one thing left in a workflow that selects code is `${{ vars.SDLC_REF }}`.
It is a variable, which only an org or repo admin (or promote's App) can set,
and no pull request can change it.

## Per-repo files that can go

Counts are across the 70 repos.

| File | Repos | Goes because |
|---|---|---|
| `.github/workflows/*` except `ci.yml` | 69 | everything is in `ci.yml` |
| `.github/dependabot.yml` | 69 | Renovate's org-inherited config (XV rule 2); fails validation from v1.60.0 |
| `.github/workflows/dependabot-automerge.yml` | 68 | Renovate automerge |
| `renovate.json` | 0 | already none: the org-inherited config makes it optional |
| `.gemini/config.yaml`, `.gemini/styleguide.md` | 35 | Gemini Code Assist's config. Remove if that app is no longer installed: Gatehouse is the reviewer. (Check the org's installed apps first.) |
| `.github/ISSUE_TEMPLATE/` | 22 | org-wide defaults from `crunchtools/.github` (GitHub's community health files), kept only where a repo differs |
| `.sdlc/bin/sdlc` | 69 | CI no longer runs it. It stays for the hooks until the `sdlc` RPM puts the same bootstrap on every seat's PATH. Then the hooks call `sdlc`, and the file goes |

These stay, because they are the repo's own data and not policy:
`.pre-commit-config.yaml` (it wires the hooks),
`.specify/memory/constitution.md` (the manifest), `gourmand.toml` (49 repos, only 7 alike) and
`.gatehouse-ignore` (30 repos), which are each repo's policy and never the
image's, `gourmand-exceptions.toml`,
`gatehouse-findings.toml`, `.sdlc/codeql-baseline.json`, and the
`[tool.sdlc]` / `sdlc.toml` config.

## Changes to convert_repo.py and cutover.py

Prose only. Neither is changed by this PR.

**convert_repo.py** stops replacing workflows one at a time and writes the
single file:

- **Writing the file.** It writes `examples/ci.yml`, the 68-line file
  above, unchanged. There is nothing to template: the repo's kind, the
  default branch and Ashigaru enrolment (`vars.ASHIGARU_ENABLED`) are all read
  at run time by `sdlc event`.
- **Moving what used to be in the workflows.** What the old files said per
  repo moves into `[tool.sdlc]`: a `publish = "pypi"` where a PyPI publish
  existed, and an image's downstream dispatch list. These are ordinary
  config, so they are not fleet policy.
- **Deleting the old files.** It deletes every workflow whose every job maps
  to a row of the "What `sdlc event` runs" table. The test is the existing
  `is_covered_workflow`, widened from "the CI stub" to that table.
- **Refusing what it cannot cover.** A workflow it does not cover is refused
  with exit 2 and named. trentina's `benchmark-decoys`, `l2-train`,
  `collect-wild`, `demo` and `mutation-advisory`, petit's `fingerprints`, and
  the RPM `packages.yml` workflows become repo-defined commands
  (`[tool.sdlc.commands]`, run as `sdlc run <name>`) by hand, one repo at a
  time.
- **No branch substitution.** `templated()` goes: nothing in the new file
  names the default branch, since sdlc reads it from the event.
- **Settings it cannot make.** It sets no variables, no rulesets, no PyPI
  trusted publishers and no repo settings. The Pages source
  for `packages` is listed for a human.

**cutover.py** sheds most of its work, because the things it moved no longer
exist in the repo:

- **What goes.** `move_constitution` loses its workflow pins, since there are
  no calls (it still moves `Inherits:` and the hook `rev:`). `sign()` goes:
  the `release` job always signs, and the cosign secrets are named in the
  file. `add_release_flow`, `open_release`, `tagged_release`,
  `write_template` and `recopy_caller` go: release and Ashigaru run inside
  `write`, so there is one tag path by construction and `tag_paths` has
  nothing to count. `add_schedule` goes, because the template carries the
  weekly cron.
- **`drop_dependabot`** stays.
- **New work.** It deletes `.gemini/` where the org no longer has the app. It moves the repo from the old required-checks ruleset to the
  one that requires `sdlc / check (pull_request)` and
  `sdlc / gate (pull_request_target)` (by repo targeting, in
  the same run that merges the pull request). It names the PyPI project whose
  trusted publisher must be re-pointed. A repo it cannot move is a refusal,
  never a pull request, because a required check that never reports blocks
  every merge silently.
- **`pins_left` becomes "any `uses:` at all".** The new constitution release
  makes the same check in the validator: `check_third_party_pins` widens to
  refuse every `uses:` for a repo inheriting that release. That is a
  constitution change, filed separately: XV rules 2 and 5 are rewritten to
  "no `uses:`; versions live in the sdlc image".

**Order.** The order is the same one every move in this repo follows.
1. sdlc grows `sdlc event` and the tools it needs, and releases.
2. Promote starts writing `SDLC_REF`.
3. The privileged-container spike passes on both runner kinds.
4. Three canary repos convert: mcp-wordpress, ubi10-core and acquacotta.
5. The fleet wave runs.
6. The reusables are deprecated a release later.
