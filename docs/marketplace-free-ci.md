# One workflow file, no marketplace actions

Design for a1051. Nothing here is implemented: it is the target that
`convert_repo.py` and `cutover.py` will be changed to write, and the list of
`sdlc` subcommands that have to exist first.

**The rule.** Every repo has exactly one workflow file,
`.github/workflows/ci.yml`. Every job in it runs the sdlc image and one
`sdlc <command>`. It contains no `uses:` at all: no `actions/checkout`, no
reusable-workflow call and no third-party action. The constitution holds the
policy ("use Renovate", "use the sdlc image", "sign releases"). The sdlc image
holds every tool and every version. Renovate bumps those versions in ordinary
files in this repo (`Containerfile`, `uv.lock`, `package-lock.json`), so it
never needs the GitHub App **Workflows** permission. There is no Dependabot for
actions and no fleet bump.

**Bottom line.** Of the 70 org repos, 69 have workflows today, and every one of
them carries at least one third-party pin (`actions/checkout`, at least). Under
this design all 69 end with zero `uses:` lines of any kind. That depends on two
things: the hard cases below being accepted as designed, and mcp-atlassian
dropping the two upstream workflows its fork carries. The only versions left in
a workflow file are none. Which image runs is the org variable `SDLC_REF`, and
a repo variable overrides it.

## The draft ci.yml

### A typical repo: mcp-wordpress

This one file replaces nine: `ci.yml`, `constitution.yml`, `security.yml`,
`gatehouse.yml`, `gatehouse-retriage.yml`, `container.yml`, `publish.yml`,
`ashigaru.yml` and `dependabot-automerge.yml`.

```yaml
# The repo's whole CI. Every job runs quay.io/crunchtools/sdlc, by digest
# (vars.SDLC_REF, written by sdlc's promote job after it signs), and one sdlc
# command. sdlc fetches the code itself, deciding per event what is safe to
# fetch: never a fork's head under pull_request_target. Copy unchanged;
# convert_repo.py drops the jobs a repo has no use for.
name: CI

on:
  push:
    branches: [main]
  pull_request:
  pull_request_target:  # zizmor: ignore[dangerous-triggers] -- nothing checks out the PR
    types: [opened, synchronize, reopened, closed]
  pull_request_review_comment:
    types: [created, edited, deleted]
  issues:
    types: [opened, labeled, reopened]
  issue_comment:
    types: [created]
  workflow_run:  # zizmor: ignore[dangerous-triggers] -- the reply doorbell, see below
    workflows: [CI]
    types: [completed]
  release:
    types: [published]
  schedule:
    - cron: "17 6 * * 1"
  workflow_dispatch:
    inputs:
      version:
        description: "Release X.Y.Z, prepared on chore/release-X.Y.Z (empty: rebuild)"
        type: string

permissions: {}

jobs:
  # ── Required checks. The names are the org ruleset's, unchanged. ─────────
  # The first job defines the two anchors every other job aliases. GitHub
  # takes YAML anchors (since 2025-09) but not merge keys (<<), and it rejects
  # unknown top-level keys, so they cannot live in an x-sdlc: block.
  ci:
    name: sdlc ci
    if: github.event_name == 'push' || github.event_name == 'pull_request'
    runs-on: &runner >-
      ${{ github.event_name == 'pull_request'
          && github.event.pull_request.head.repo.full_name == github.repository
          && (vars.CACHED_RUNNER_LABEL || 'ubuntu-latest')
          || github.event_name != 'pull_request'
          && github.event_name != 'pull_request_target'
          && (vars.UNCACHED_RUNNER_LABEL || 'ubuntu-latest')
          || 'ubuntu-latest' }}
    container: &sdlc
      image: ${{ vars.SDLC_REF || 'quay.io/crunchtools/sdlc:latest' }}
    permissions: { contents: read, security-events: write }
    env: { GITHUB_TOKEN: "${{ github.token }}" }
    steps: [{ run: sdlc ci }]

  gourmand:
    name: Code Quality (Gourmand) / Code Quality (Gourmand)
    if: github.event_name == 'push' || github.event_name == 'pull_request'
    runs-on: *runner
    container: *sdlc
    permissions: { contents: read }
    env: { GITHUB_TOKEN: "${{ github.token }}" }
    steps: [{ run: sdlc gourmand }]

  constitution:
    name: Constitution / Validate constitution
    if: github.event_name == 'push' || github.event_name == 'pull_request'
    runs-on: *runner
    container: *sdlc
    permissions: { contents: read, security-events: write }
    env: { GITHUB_TOKEN: "${{ github.token }}" }
    steps: [{ run: sdlc constitution --zizmor }]

  guard:
    name: Protect workflows
    if: >-
      github.event_name == 'pull_request_target' && github.event.action != 'closed'
      || github.event_name == 'pull_request_review_comment'
    runs-on: *runner
    container: *sdlc
    permissions: { contents: read, pull-requests: write }
    env: { GITHUB_TOKEN: "${{ github.token }}" }
    steps: [{ run: sdlc guard }]

  # ── Gatehouse. The review is advisory; triage is required. ───────────────
  review:
    name: Gatehouse review / Gatehouse Code Review
    if: github.event_name == 'pull_request_target' && github.event.action != 'closed'
    runs-on: *runner
    container: *sdlc
    permissions: { contents: read, pull-requests: write }
    env:
      GITHUB_TOKEN: ${{ github.token }}
      OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
    steps: [{ run: sdlc review }]

  triage:
    name: Gatehouse triage / Gatehouse triage
    needs: review
    if: >-
      always() && (github.event_name == 'pull_request_target' && github.event.action != 'closed'
      || github.event_name == 'pull_request_review_comment')
    runs-on: *runner
    container: *sdlc
    permissions: { pull-requests: read }
    env: { GITHUB_TOKEN: "${{ github.token }}" }
    steps: [{ run: sdlc triage }]

  retriage:
    name: Re-run Gatehouse triage
    if: >-
      github.event_name == 'workflow_run'
      && github.event.workflow_run.event == 'pull_request_review_comment'
    runs-on: *runner
    container: *sdlc
    permissions: { actions: write, pull-requests: read }
    env: { GITHUB_TOKEN: "${{ github.token }}" }
    steps: [{ run: sdlc retriage }]

  # ── Security ──────────────────────────────────────────────────────────────
  audit:
    name: Dependency CVE Scan
    if: github.event_name == 'schedule' || github.event_name == 'push'
    runs-on: *runner
    container: *sdlc
    permissions: { contents: read, issues: write }
    env: { GITHUB_TOKEN: "${{ github.token }}" }
    steps: [{ run: sdlc audit --alert }]

  # ── Container image: build, gate, then sign and promote ──────────────────
  image-build:
    name: image / build
    if: >-
      contains(fromJSON('["push","pull_request","schedule","workflow_dispatch"]'), github.event_name)
      && !inputs.version
    runs-on: *runner
    container: { image: "${{ vars.SDLC_REF || 'quay.io/crunchtools/sdlc:latest' }}", options: --privileged }
    permissions: { contents: read }
    outputs: { ref: "${{ steps.build.outputs.ref }}" }
    env:
      GITHUB_TOKEN: ${{ github.token }}
      QUAY_USERNAME: ${{ secrets.QUAY_USERNAME }}
      QUAY_PASSWORD: ${{ secrets.QUAY_PASSWORD }}
    steps: [{ id: build, run: sdlc image build }]

  image-gates:
    name: image / gates
    needs: image-build
    runs-on: *runner
    container: { image: "${{ vars.SDLC_REF || 'quay.io/crunchtools/sdlc:latest' }}", options: --privileged }
    permissions: { contents: read }
    env:
      GITHUB_TOKEN: ${{ github.token }}
      SDLC_CANDIDATE: ${{ needs.image-build.outputs.ref }}
    steps: [{ run: sdlc image gates }]

  image-promote:
    name: image / promote
    needs: image-gates
    if: github.event_name != 'pull_request'
    runs-on: ${{ vars.UNCACHED_RUNNER_LABEL || 'ubuntu-latest' }}
    environment: cosign-signing
    container: { image: "${{ vars.SDLC_REF || 'quay.io/crunchtools/sdlc:latest' }}", options: --privileged }
    permissions: { contents: read }
    env:
      GITHUB_TOKEN: ${{ github.token }}
      SDLC_CANDIDATE: ${{ needs.image-build.outputs.ref }}
      QUAY_USERNAME: ${{ secrets.QUAY_USERNAME }}
      QUAY_PASSWORD: ${{ secrets.QUAY_PASSWORD }}
      COSIGN_PRIVATE_KEY: ${{ secrets.COSIGN_PRIVATE_KEY }}
      COSIGN_PASSWORD: ${{ secrets.COSIGN_PASSWORD }}
    steps: [{ run: sdlc image promote }]

  # ── Release: Ashigaru's app opens the PR and tags the merge ──────────────
  release-open:
    name: release / open
    if: github.event_name == 'workflow_dispatch' && inputs.version
    runs-on: *runner
    container: *sdlc
    permissions: {}
    env:
      ASHIGARU_CLIENT_ID: ${{ vars.ASHIGARU_CLIENT_ID }}
      ASHIGARU_APP_KEY: ${{ secrets.ASHIGARU_APP_KEY }}
      VERSION: ${{ inputs.version }}
    steps: [{ run: sdlc release open }]

  release-tag:
    name: release / tag
    if: >-
      github.event_name == 'pull_request_target' && github.event.action == 'closed'
      && github.event.pull_request.merged
      && github.event.pull_request.head.repo.full_name == github.repository
      && startsWith(github.event.pull_request.head.ref, 'ashigaru/release-')
    runs-on: *runner
    container: *sdlc
    permissions: {}
    env:
      ASHIGARU_CLIENT_ID: ${{ vars.ASHIGARU_CLIENT_ID }}
      ASHIGARU_APP_KEY: ${{ secrets.ASHIGARU_APP_KEY }}
    steps: [{ run: sdlc release tag }]

  pypi:
    name: publish / pypi
    if: github.event_name == 'release'
    runs-on: ubuntu-latest  # PyPI's trusted publisher names the GitHub-hosted environment
    container: *sdlc
    permissions: { contents: read, id-token: write }
    env: { GITHUB_TOKEN: "${{ github.token }}" }
    steps: [{ run: sdlc publish pypi }]

  # ── Ashigaru: each model job is read-only; each app-key job runs no model ─
  ashigaru-triage:
    name: ashigaru / triage
    if: github.event_name == 'issues' && vars.ASHIGARU_ENABLED == 'true'
    concurrency: { group: "ashigaru-issue-${{ github.event.issue.number }}" }
    runs-on: *runner
    container: *sdlc
    permissions: { contents: read, issues: read }
    outputs: { verdict: "${{ steps.ai.outputs.verdict }}" }
    env:
      GITHUB_TOKEN: ${{ github.token }}
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
    steps: [{ id: ai, run: sdlc ashigaru triage }]

  ashigaru-code:
    name: ashigaru / code
    needs: ashigaru-triage
    if: needs.ashigaru-triage.outputs.verdict == 'ready-to-code'
    runs-on: *runner
    container: *sdlc
    permissions: { contents: read, issues: read }
    outputs: { patch: "${{ steps.ai.outputs.patch }}" }
    env:
      GITHUB_TOKEN: ${{ github.token }}
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
    steps: [{ id: ai, run: sdlc ashigaru code }]

  ashigaru-fix:
    name: ashigaru / fix
    needs: triage
    if: >-
      always() && needs.triage.result == 'failure'
      && github.event_name == 'pull_request_target'
      && startsWith(github.event.pull_request.head.ref, 'ashigaru/')
      && github.event.pull_request.head.repo.full_name == github.repository
    runs-on: *runner
    container: *sdlc
    permissions: { contents: read, pull-requests: read }
    outputs: { patch: "${{ steps.ai.outputs.patch }}" }
    env:
      GITHUB_TOKEN: ${{ github.token }}
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
    steps: [{ id: ai, run: sdlc ashigaru fix }]

  ashigaru-publish:
    name: ashigaru / publish
    needs: [ashigaru-triage, ashigaru-code, ashigaru-fix]
    if: >-
      always() && !cancelled()
      && (needs.ashigaru-triage.result == 'success' || needs.ashigaru-fix.result == 'success')
    runs-on: *runner
    container: *sdlc
    permissions: {}
    env:
      ASHIGARU_CLIENT_ID: ${{ vars.ASHIGARU_CLIENT_ID }}
      ASHIGARU_APP_KEY: ${{ secrets.ASHIGARU_APP_KEY }}
      VERDICT: ${{ needs.ashigaru-triage.outputs.verdict }}
      CODE_PATCH: ${{ needs.ashigaru-code.outputs.patch }}
      FIX_PATCH: ${{ needs.ashigaru-fix.outputs.patch }}
    steps: [{ run: sdlc ashigaru publish }]
```

### An image-only repo: ubi10-core

The same file with the Python-only jobs (`audit`, `pypi`) dropped and one
trigger added: a parent image's promote dispatches `parent-image-updated`.
`convert_repo.py` makes exactly these two edits, and nothing else differs.

```yaml
on:
  # ...every trigger above, plus:
  repository_dispatch:
    types: [parent-image-updated]
```

```yaml
  image-build:
    name: image / build
    if: >-
      contains(fromJSON('["push","pull_request","schedule","workflow_dispatch","repository_dispatch"]'),
               github.event_name)
      && !inputs.version
    # ...as above
```

`sdlc ci` already knows from the repo's files which stacks it has (container,
bootc, docs), so the job is the same line in both repos.

## Event → job → command

| Event | Job (check name) | Command | Holds |
|---|---|---|---|
| push, pull_request | `sdlc ci` | `sdlc ci` (and on push to the default branch, uploads CodeQL SARIF) | read token, security-events |
| push, pull_request | `Code Quality (Gourmand) / Code Quality (Gourmand)` | `sdlc gourmand` | read token |
| push, pull_request | `Constitution / Validate constitution` | `sdlc constitution --zizmor` | read token, security-events |
| pull_request_target, pull_request_review_comment | `Protect workflows` | `sdlc guard` | pull-requests: write |
| pull_request_target | `Gatehouse review / Gatehouse Code Review` | `sdlc review` | OPENROUTER_API_KEY |
| pull_request_target, pull_request_review_comment | `Gatehouse triage / Gatehouse triage` | `sdlc triage` | read token |
| workflow_run (CI, from a reply) | `Re-run Gatehouse triage` | `sdlc retriage` | actions: write |
| push, schedule | `Dependency CVE Scan` | `sdlc audit --alert` | issues: write |
| push, pull_request, schedule, dispatch, repository_dispatch | `image / build` | `sdlc image build` | quay push to `gated-*` (none on a fork PR) |
| (needs build) | `image / gates` | `sdlc image gates` | nothing |
| (needs gates), not a PR | `image / promote` | `sdlc image promote` | cosign key, quay, in `cosign-signing` |
| workflow_dispatch with `version` | `release / open` | `sdlc release open` | Ashigaru app key |
| pull_request_target closed, merged `ashigaru/release-*` | `release / tag` | `sdlc release tag` | Ashigaru app key |
| release published | `publish / pypi` | `sdlc publish pypi` | OIDC id-token |
| issues | `ashigaru / triage` → `ashigaru / code` | `sdlc ashigaru triage`, `sdlc ashigaru code` | Claude token, read-only GitHub token |
| pull_request_target on `ashigaru/*` with triage failed | `ashigaru / fix` | `sdlc ashigaru fix` | Claude token, read-only GitHub token |
| (needs the model jobs) | `ashigaru / publish` | `sdlc ashigaru publish` | Ashigaru app key, no model |
| issue_comment | none yet | (Ashigaru's superset; no job acts on it today) | |

**Required checks keep their names.** A plain job's check is named by the job's
`name:` alone, and a reusable workflow's is `<caller job> / <called job>`. So
the four required checks in the org ruleset (`Protect workflows`,
`Gatehouse triage / Gatehouse triage`,
`Code Quality (Gourmand) / Code Quality (Gourmand)`,
`Constitution / Validate constitution`), and the `sdlc ci` check in "Python SDLC
gates", are reproduced by naming the jobs exactly that, slash included. No
ruleset changes. Dropping the slash names is a separate, optional ruleset edit
made after the fleet has moved.

**Checks that do change name**, none of them required: `CodeQL Analysis` goes
away (CodeQL already runs in `sdlc ci`, and its SARIF is uploaded from there).
The container-release jobs (`container / build`, `gates`, `mark gated`,
`promote`) become `image / build|gates|promote`. The Ashigaru router's job
names become `ashigaru / *`. `Dependabot` (automerge) goes with Dependabot.

**The security split is unchanged.** Every job that runs a model (`review`,
`ashigaru / triage|code|fix`) holds a read-only `GITHUB_TOKEN` and the model's
own key, never an App key. Every job that holds an App key (`release / *`,
`ashigaru / publish`) runs no model. They are separate jobs and so separate
runners, exactly as crunchtools/ashigaru's `.github/workflows/code.yml`'s "the credential that can write never shares a
machine with the LLM" requires. The patch crosses from one to the other as a
job output, not an artifact (see below).

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
that cap. **This needs a spike before the template ships.** If GitHub refuses
the self-reference, or the doubled run count is not acceptable, the doorbell
becomes a second file, `retriage.yml`, with one job (`sdlc retriage`) and
still no `uses:`. That is the one place the one-file rule might bend.

**6. Who runs an event's workflow.**
- `pull_request_target` runs "in the context of the default branch of the base
  repository", so its jobs are the base's definitions. That is why `guard`,
  `triage` and `release / tag` are safe there.
- `pull_request` and `pull_request_review_comment` run the merge commit's copy
  of `ci.yml` ([events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)).
  So a fork can edit the jobs in its own run, as it can today. `guard` refuses
  the change, and the required checks that matter come from
  `pull_request_target`.

Nothing changes here. Moving into one file does not move the boundary.

**7. Ashigaru `fix` no longer needs `workflow_run`.** Today it listens for
Gatehouse's completion. In one file it is a job that `needs: triage` in the
same `pull_request_target` run, gated on `ashigaru/` branches in this repo.
That is one trigger fewer, and the bound of five rounds stays in sdlc.

**8. Fork pull requests may not see `vars`.** GitHub's docs do not say whether
an org variable reaches a fork's `pull_request` run, and a community thread
says it does not. The image line falls back to
`quay.io/crunchtools/sdlc:latest`, so a fork's run still starts, on the
unverified tag. That run holds no secret and no write token, so the fallback
costs a verification and grants nothing. The spike checks it.

**9. Check names.** A required check is matched by name. The constitution
already records that nesting a reusable workflow renames its checks to
`<caller job> / <called job>` (#22, section "Gate wiring"). The one-file design
keeps every required name by spelling it out in `name:`. The canary repos
confirm this before the fleet moves.

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
  credentials. With Dependabot gone, that is a fork's pull request alone. Fork
  pull requests get build and gates in one job (`sdlc image build --gate`),
  which holds no credential, so there is no trust boundary to keep. Same-repo
  runs already pass the image through quay by digest, and `needs.*.outputs`
  carries the digest.
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
   (`image / *`, RPM matrices, and `sdlc ci` in a repo with a container
   stack). These carry `options: --privileged`. On GitHub-hosted runners that
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

- **Writing the file.** It reads `examples/ci.yml`, the mcp-wordpress file
  above, and deletes the jobs the repo has no use for. `audit` and `pypi` stay
  only with a `pyproject.toml` and a PyPI publish today. `image-*` stays only
  with a Containerfile and a `container-release.yml` call. `release-*` stays
  only for an image or PyPI publisher. `ashigaru-*` stays only if the repo is
  enrolled. It adds `repository_dispatch` where the old `container.yml` had
  it. Edits are by line, never by a YAML writer, and are read back as now.
- **Deleting the old files.** It deletes every workflow whose every job the
  new file covers. The test is the existing `is_covered_workflow`, widened
  from "the CI stub" to the job table above.
- **Refusing what it cannot cover.** A workflow it does not cover is refused
  with exit 2 and named. trentina's `benchmark-decoys`, `l2-train`,
  `collect-wild`, `demo` and `mutation-advisory`, petit's `fingerprints`, and
  the RPM `packages.yml` workflows become repo-defined commands
  (`[tool.sdlc.commands]`, run as `sdlc run <name>`) by hand, one repo at a
  time.
- **No branch substitution.** `templated()` goes: nothing in the new file
  names the default branch, since sdlc reads it from the event.
- **Settings it cannot make.** It writes the gha-runner `runs-on` expression
  as the anchor. It sets no variables and no repo settings. The Pages source
  for `packages` is listed for a human.

**cutover.py** sheds most of its work, because the things it moved no longer
exist in the repo:

- **What goes.** `move_constitution` loses its workflow pins, since there are
  no calls (it still moves `Inherits:` and the hook `rev:`). `sign()` goes:
  `sdlc image promote` always signs, and the cosign secrets are named in the
  template. `add_release_flow`, `open_release`, `tagged_release`,
  `write_template` and `recopy_caller` go: release and Ashigaru are jobs in
  `ci.yml`, so there is one tag path by construction and `tag_paths` has
  nothing to count. `add_schedule` goes, because the template carries the
  weekly cron.
- **`drop_dependabot`** stays.
- **New work.** It deletes `.gemini/` where the org no longer has the app. Before pushing, it checks that the repo's ruleset-required checks
  are all produced by the new `ci.yml`, by name. A missing one is a refusal,
  never a pull request, because a required check that never reports blocks
  every merge silently.
- **`pins_left` becomes "any `uses:` at all".** The new constitution release
  makes the same check in the validator: `check_third_party_pins` widens to
  refuse every `uses:` for a repo inheriting that release. That is a
  constitution change, filed separately: XV rules 2 and 5 are rewritten to
  "no `uses:`; versions live in the sdlc image".

**Order.** The order is the same one every move in this repo follows.
1. sdlc grows the subcommands and tools, and releases.
2. Promote starts writing `SDLC_REF`.
3. The privileged-container spike passes on both runner kinds.
4. Three canary repos convert: mcp-wordpress, ubi10-core and acquacotta.
5. The fleet wave runs.
6. The reusables are deprecated a release later.
