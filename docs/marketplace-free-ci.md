# One workflow file, no marketplace actions

Design for a1051. Nothing here is implemented: it is the target that
`convert_repo.py` and `cutover.py` will be changed to write, and the list of
`sdlc` internals that have to exist first.

## The file: 89 lines, seven jobs, every repo

Every repo carries exactly this `.github/workflows/ci.yml`, byte for byte,
whatever kind of repo it is. It is 89 lines without comments, and it contains
no `uses:` of any kind: no `actions/checkout`, no reusable-workflow call and no
third-party action. Every job runs one image, the sdlc **bootstrap**, by a
literal digest, and one command, `sdlc event`. That command reads
`GITHUB_EVENT_NAME`, the event payload and `GITHUB_JOB`, and runs whatever
that trust level does for that event in this repo.

```yaml
# One file, every repo, copied unchanged. A job is a trust level; `sdlc event`
# routes inside it. The only version here is the bootstrap's digest: it follows
# sdlc's signed channel pointer, verifies the release against sdlc's own key,
# and runs it through the runner's container socket. Every secret lives in an
# environment that admits the default branch and v*.*.* tags only.
name: CI
on:
  push:
  pull_request:
  pull_request_target: { types: [opened, synchronize, reopened, closed] }  # zizmor: ignore[dangerous-triggers]
  pull_request_review_comment: { types: [created, edited, deleted] }
  issues: { types: [opened, labeled, reopened] }
  release: { types: [published] }
  repository_dispatch: { types: [parent-image-updated] }
  schedule: [{ cron: "17 6 * * 1" }]
  workflow_dispatch: { inputs: { version: { type: string } } }
permissions: {}
jobs:
  check:  # no secret, read token: the one level a fork's PR runs, and the only one allowed self-hosted
    name: sdlc / check (${{ github.event_name }})  # required as "(pull_request)"
    if: >-
      contains(fromJSON('["pull_request","release","schedule","repository_dispatch","workflow_dispatch"]'), github.event_name)
      || github.event_name == 'push' && github.ref_name == github.event.repository.default_branch
    runs-on: ${{ github.event_name == 'pull_request' && (github.event.pull_request.head.repo.full_name == github.repository && vars.CACHED_RUNNER_LABEL || 'ubuntu-latest') || vars.UNCACHED_RUNNER_LABEL || 'ubuntu-latest' }}
    container: &boot { image: "quay.io/crunchtools/sdlc-boot@sha256:<digest>", volumes: ["/var/run/docker.sock:/var/run/docker.sock"] }
    permissions: { contents: read, security-events: write }
    outputs: { result: "${{ steps.e.outputs.result }}" }
    env: { GITHUB_TOKEN: "${{ github.token }}" }
    steps: &event [{ id: e, run: sdlc event }]
  ai:  # model keys and a read token, never an App key; hands its result on in four chunks of at most 112,500 bytes
    if: github.event_name == 'issues' || github.event_name == 'pull_request_target' && github.event.action != 'closed'
    runs-on: &hosted ${{ github.event.repository.private && 'crunchtools-uncached' || 'ubuntu-latest' }}
    environment: ai
    container: *boot
    permissions: { contents: read, issues: read, pull-requests: read }
    outputs: { p0: "${{ steps.e.outputs.p0 }}", p1: "${{ steps.e.outputs.p1 }}", p2: "${{ steps.e.outputs.p2 }}", p3: "${{ steps.e.outputs.p3 }}" }
    env: { GITHUB_TOKEN: "${{ github.token }}", CLAUDE_CODE_OAUTH_TOKEN: "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}", OPENROUTER_API_KEY: "${{ secrets.OPENROUTER_API_KEY }}" }
    steps: *event
  image:  # builds the Containerfile through the socket, gates it, pushes gated-<sha> by digest
    needs: check
    if: >-
      fromJSON(needs.check.outputs.result || '{}').image
      && (contains(fromJSON('["push","release","schedule","repository_dispatch"]'), github.event_name) || github.event_name == 'workflow_dispatch' && !inputs.version)
    runs-on: *hosted
    environment: registry
    container: *boot
    permissions: { contents: read }
    outputs: { result: "${{ steps.e.outputs.result }}" }
    env: { GITHUB_TOKEN: "${{ github.token }}", SDLC_CHECK: "${{ needs.check.outputs.result }}", QUAY_USERNAME: "${{ secrets.QUAY_USERNAME }}", QUAY_PASSWORD: "${{ secrets.QUAY_PASSWORD }}",
           RHSM_ACTIVATION_KEY: "${{ secrets.RHSM_ACTIVATION_KEY }}", RHSM_ORG_ID: "${{ secrets.RHSM_ORG_ID }}" }
    steps: *event
  promote:  # the cosign key: signs, attests and moves tags on a digest it did not build
    needs: image
    if: needs.image.result == 'success' && contains(fromJSON('["push","release","schedule","repository_dispatch","workflow_dispatch"]'), github.event_name)
    concurrency: { group: "promote-${{ github.repository }}" }
    runs-on: *hosted
    environment: cosign-signing
    container: *boot
    permissions: { contents: read }
    outputs: { result: "${{ steps.e.outputs.result }}" }
    env: { SDLC_IMAGE_RESULT: "${{ needs.image.outputs.result }}", QUAY_USERNAME: "${{ secrets.QUAY_USERNAME }}", QUAY_PASSWORD: "${{ secrets.QUAY_PASSWORD }}", COSIGN_PRIVATE_KEY: "${{ secrets.COSIGN_PRIVATE_KEY }}", COSIGN_PASSWORD: "${{ secrets.COSIGN_PASSWORD }}" }
    steps: *event
  write:  # the Ashigaru App key, no model: comments, labels, PRs, pushes, release PR and tag, downstream dispatch
    needs: [check, ai, promote]
    if: >-
      always() && !cancelled()
      && (contains(fromJSON('["pull_request_target","issues","workflow_dispatch","schedule"]'), github.event_name) || needs.promote.result == 'success')
    concurrency: { group: "write-${{ github.event.pull_request.number || github.event.issue.number || github.ref }}" }
    runs-on: *hosted
    environment: ashigaru
    container: *boot
    permissions: {}
    env: { ASHIGARU_CLIENT_ID: "${{ vars.ASHIGARU_CLIENT_ID }}", ASHIGARU_APP_KEY: "${{ secrets.ASHIGARU_APP_KEY }}", VERSION: "${{ inputs.version }}", SDLC_CHECK: "${{ needs.check.outputs.result }}", SDLC_PROMOTED: "${{ needs.promote.outputs.result }}",
           SDLC_AI_0: "${{ needs.ai.outputs.p0 }}", SDLC_AI_1: "${{ needs.ai.outputs.p1 }}", SDLC_AI_2: "${{ needs.ai.outputs.p2 }}", SDLC_AI_3: "${{ needs.ai.outputs.p3 }}" }
    steps: *event
  gate:  # required: workflow guard and every finding answered; no secret. On a reply, re-runs itself where it counts
    name: sdlc / gate (${{ github.event_name }})  # required as "(pull_request_target)"
    needs: write
    if: always() && (github.event_name == 'pull_request_target' && github.event.action != 'closed' || github.event_name == 'pull_request_review_comment')
    concurrency: { group: "gate-${{ github.event.pull_request.number }}-${{ github.event_name }}" }
    runs-on: *hosted
    container: *boot
    permissions: { contents: read, pull-requests: write, actions: write }
    env: { GITHUB_TOKEN: "${{ github.token }}" }
    steps: *event
  pypi:  # OIDC only, in the pypi environment (v*.*.* tags)
    needs: check
    if: github.event_name == 'release' && fromJSON(needs.check.outputs.result || '{}').publish == 'pypi'
    runs-on: ubuntu-latest
    environment: pypi
    container: *boot
    permissions: { contents: read, id-token: write }
    env: { GITHUB_TOKEN: "${{ github.token }}" }
    steps: *event
```

**Why 89 lines and not 20.** GitHub makes a separate job only where the
permissions, the secrets, the environment or the runner differ. Seven levels
differ (see below), and each job needs its `if`, `runs-on`, `environment`,
`container`, `permissions`, `env` and `steps` lines. Merging any two jobs would
put a secret, a write grant or a signing key somewhere the design says it must
not be. Everything else is already out of the file:
- **Versions:** the only one is the bootstrap's digest.
- **Tasks:** all routing is `sdlc event`.
- **Repo kind:** nothing here depends on it.

YAML anchors (`&boot`, `&event`, `&hosted`) save the repeats. GitHub accepts
anchors but not merge keys (`<<`), and it rejects unknown top-level keys, so
the anchors are defined in the first job that uses them, and `env:` is written
out in each job.

**The image-only repo (ubi10-core) runs the same file.** Its `check` reports
`image: true`, so `image`, `promote`, and then `write` (for the downstream
dispatch) run. A Python library reports `publish: "pypi"`, so `pypi` runs on
its releases. A docs repo reports neither, and those jobs never start. A parent
image's promote sends `parent-image-updated`, which `check` routes to a
rebuild. Nothing about a repo's kind lives in the file, so `convert_repo.py`
never edits it and the fleet cannot drift.

**Bottom line.** Of the 70 org repos, 69 have workflows today. Every one of
them carries at least one third-party pin, and most carry 7 to 9 workflow
files. Under this design all 69 carry this one file and no `uses:`. Three
conditions:
- mcp-atlassian deletes the two upstream workflows its fork carries;
- spike #103 passes;
- the cutover prerequisites below are in place.

The constitution holds the policy, and the sdlc image holds every tool and
version. Renovate bumps those versions in ordinary files in this repo, so it
never needs the GitHub App **Workflows** permission. The bootstrap digest is
the one exception. It is edited fleet-wide by Josui Prime, and only when the
bootstrap itself changes.

## The trust root: sdlc's own key and a signed channel

The file names `quay.io/crunchtools/sdlc-boot@sha256:<digest>`. That is a
literal, so only a reviewed commit to `ci.yml` can change it. No variable can
change it: anyone with write access can set a repo variable, and a repo
variable overrides the org's.

**sdlc's release key.** sdlc signs its own releases with its **own** cosign
key. That key is not the fleet key, and its private half lives only in sdlc's
`cosign-signing` environment. A fleet repo's signing key therefore cannot
produce something the bootstrap will run. The fleet key keeps signing the
fleet's images, as it does today. Each sdlc release is signed by digest with
two annotations:

    cosign sign --key sdlc.key -a repo=crunchtools/sdlc -a version=X.Y.Z quay.io/crunchtools/sdlc@sha256:…

**The channel pointer.** On each release, promote also publishes a small JSON
document, signed with the same key (`cosign sign-blob`, the signature carried
with it):

    {"repo": "crunchtools/sdlc", "serial": 412, "issued": "2026-11-22T23:40:00Z",
     "expires": "2026-12-06T23:40:00Z", "current": {"version": "X.Y.Z", "digest": "sha256:…"},
     "deny": ["sha256:…", "…"]}

It is stored as an OCI artifact, `quay.io/crunchtools/sdlc-channel:stable`. The
location is mutable, but the content is not trusted until it verifies. The
bootstrap never resolves a mutable image tag.

**What the bootstrap does, in every job:**
1. It fetches the pointer and verifies it against the baked sdlc key, and
   checks `repo`. It refuses a pointer whose `serial` is below the baked
   floor, or whose `expires` has passed. A replayed old pointer, from before a
   revocation, fails on one or the other.
2. It checks that `current.digest` is not in `deny` and that
   `current.version` is at or above the baked minimum version.
3. It verifies the image at that digest:
   `cosign verify --key sdlc.pub -a repo=crunchtools/sdlc -a version=<current.version>`.
   It also checks that the image's `version` label agrees.
4. It runs `sdlc event` from that digest, as a sibling container on the
   runner's socket. Repo code then runs only in nested containers that sdlc
   starts. Those hold no GitHub token, no App key and never the socket.

Any failure is a 2, "GATE CANNOT RUN". Nothing falls back to a tag.

**Release, rollback and revocation need no fleet edit.**
- **A release** is a new pointer.
- **A rollback** is a new pointer whose `current` is an earlier release.
- **A revocation** is a new pointer that adds the bad digest to `deny`.

Each new pointer has a higher `serial`. sdlc's weekly rebuild re-issues the
pointer, so `expires` (two weeks) never lapses in normal running. A lapse
fails closed: every gate stops with a 2 until a fresh pointer is published.

**Key rotation.**
1. **Planned.** Generate the new key in sdlc's `cosign-signing` environment.
   Ship a bootstrap that trusts **both** public keys (fleet edit 1). Sign the
   next release and pointer with the new key only, and raise the serial
   floor. Then ship a bootstrap with the new key alone (fleet edit 2), and
   destroy the old private key. Between the two edits, either key verifies,
   and nothing is ever unverifiable.
2. **Compromise.** The deny list cannot help, because whoever holds the key
   can sign a pointer without it. Ship a bootstrap with only a new key and a
   serial floor above anything the old key signed. That is one emergency
   fleet edit, and until it merges a repo is exposed. Pointer expiry limits
   how long a **stolen old pointer** stays usable. It does not limit what a
   thief can sign fresh. This is the one case that needs a fleet edit, and it
   is why the key never leaves one environment of one repo.

Laptops keep `.sdlc/bin/sdlc`, which moves to the same pointer and key.

## Jobs are trust levels

| Job | Check name | Environment and secrets | Runs on | Never holds |
|---|---|---|---|---|
| `check` | `sdlc / check (pull_request)` (required) | none; read token, `security-events`, the socket | pull_request (forks too), push to the default branch, release, schedule, dispatch; may self-host | any secret |
| `ai` | | `ai`: Claude and OpenRouter keys | issues; `pull_request_target` not closed | an App key, a write token |
| `image` | | `registry`: quay, RHSM and registry.redhat.io credentials | after `check` reports an image: push, release, schedule, parent-image-updated, rebuild dispatch | the cosign key |
| `promote` | | `cosign-signing`: cosign key, quay; one at a time per repo | after a green `image`, same events | an App key, a build |
| `write` | | `ashigaru`: Ashigaru App key (sdlc mints and revokes the token) | `pull_request_target`, issues, release dispatch, schedule; and after a green `promote`, for the downstream dispatch | a model |
| `gate` | `sdlc / gate (pull_request_target)` (required) | none; `pull-requests: write`, `actions: write` | `pull_request_target` not closed, after `write`; review comments, as the doorbell | any secret |
| `pypi` | | `pypi`: OIDC only | `release`, when `check` reports `publish: pypi` | anything else |

**The model's container does hold the Claude token.** It has to, because the
model calls the API with it. This predates the design:
`claude-code-action` puts the same token in the same process's environment
today, which is why Ashigaru denies `Read(//proc/**)`. What the model's
container never holds is a GitHub write token, an App key or the socket.

**Every secret lives in an environment.** That includes quay, RHSM and
registry.redhat.io, which are org or repo secrets today. `ai`, `ashigaru`,
`registry` and `cosign-signing` admit the default branch and `v*.*.*` tags.
`pypi` admits `v*.*.*` alone. A job that runs from any other ref gets empty
secrets, whatever its `if:` says. That matters because an `if:` comes from
whichever copy of `ci.yml` runs: a branch dispatch, a pushed branch, or a
review-comment run all bring their own. Each tag rule is exactly `v*.*.*`,
the same pattern as the tag ruleset that lets only the Ashigaru App create
tags, so a tag-ref job runs a commit that the App tagged.

**Runners.** Only `check`, which holds no secret, reads runner labels from
variables and may run self-hosted: cached for a same-repo pull request,
uncached otherwise. A fork's pull request always gets `ubuntu-latest`. Every
job that holds a secret names its runner literally: `ubuntu-latest`, or
`crunchtools-uncached` for a private repo (the label the uncached runners
register). A variable cannot
move a secret-holding job onto another machine.

**Allowlists are in `if:`, as well as in the environments.** Secrets reach a
job before `sdlc event` decides anything. So each job's `if:` names the
events it may run on, and `sdlc event` picks the task within them. The `if:`
keeps the intended run from starting where it should not. The environment
stops a rewritten one.

**The splits are forced.**
- **`ai` / `write`:** "the credential that can write never shares a machine
  with the LLM" (crunchtools/ashigaru `.github/workflows/code.yml`).
- **`image` / `promote`:** the signing key never shares a job with a
  Containerfile build.
- **`promote` / `write`:** no App key goes into `cosign-signing`. So the
  downstream `parent-image-updated` dispatch, which today runs inside
  promote, moves to `write` (`needs: promote`), where the App key already
  lives.
- **`gate`:** it must come from the base definition and hold no secret, so it
  can be required.
- **`pypi`:** a job has one environment.

**What crosses between jobs.** `ai` passes its result (a verdict, review
comments, a patch) to `write`. sdlc gzips and base64-encodes it, then splits it
into four outputs of at most 112,500 bytes each, 450,000 bytes in all. That
stays under Linux's 128 KiB limit on one environment variable
(`MAX_ARG_STRLEN`, which counts the name too), and `write` reads the four
outputs as `SDLC_AI_0..3`. A result over 450,000 bytes encoded **fails `ai`
loudly** as too large to review. It is never truncated. An empty or missing
result when `ai` succeeded is an error in `write`, never "nothing to do".
Spike #103 sends a result of exactly the cap and one byte over it. `check`
passes a small JSON plan, and `image` and `promote` pass a digest and a list
of tags.

## What `sdlc event` runs

This is sdlc's routing table, not the file's. It is listed by job, then event.
Tool and category names are kept, so the Security tab's history carries over:
CodeQL uploads as `/language:python`, and zizmor as `zizmor`.

| Job | Event | Runs | Was |
|---|---|---|---|
| check | pull_request, push | `ci` with CodeQL (SARIF uploaded on the default branch), `gourmand`, `constitution` with zizmor (SARIF uploaded). A repo with an image also builds and gates the PR's Containerfile in a nested container, with no credential and nothing pushed | ci.yml, constitution.yml, security.yml CodeQL, container-release build on a PR |
| check | schedule, push | `audit`; findings go out in `result` for `write` | dependency-audit.yml |
| check | release, schedule, dispatch, parent-image-updated | plans `image` and `publish` | container.yml `resolve` |
| ai | pull_request_target | Gatehouse review of the diff, read through the API and never checked out. On an `ashigaru/` branch in this repo it also runs Ashigaru `fix` over this review's findings and the open ones from earlier rounds, five rounds at most | review.yml, fix.yml agent |
| ai | issues | Ashigaru `triage`, then `code` when the verdict is ready-to-code | triage.yml classify, code.yml agent |
| image | push, release, schedule, dispatch, parent-image-updated | builds through the socket, runs the container gates, pushes `gated-<sha>` by digest | container-release build, gates, mark |
| promote | after image | signs, attests, moves tags, never backwards | container-release promote |
| write | after promote | sends `parent-image-updated` to the downstream repos | container-release promote's dispatch |
| write | pull_request_target | posts the review; pushes the fix commit and its finding answers | review.yml, fix.yml publish |
| write | pull_request_target closed, merged `ashigaru/release-*` from this repo | tags the merge and publishes the GitHub Release | release-tag.yml |
| write | issues | labels; opens the `ashigaru/issue-N` pull request | triage.yml apply, code.yml publish |
| write | workflow_dispatch with `version` | opens the release pull request from `chore/release-X.Y.Z` | release.yml open |
| write | schedule | keeps the audit alert issue | dependency-audit.yml |
| gate | pull_request_target | the workflow guard: fails, and comments, when a fork touches `.github/workflows/` or `.sdlc/`. Then Gatehouse triage | gatehouse.yml guard, triage.yml |
| gate | pull_request_review_comment | retriage: re-runs `gate` in this PR's latest `pull_request_target` run with `GITHUB_TOKEN` (`POST /actions/jobs/{id}/rerun`). Its own check is `sdlc / gate (pull_request_review_comment)`, which nothing requires | gatehouse-retriage.yml |
| pypi | release | `uv build` in a nested container, then `uv publish` by OIDC from the job | publish.yml |

## Required checks: from five to two

| Today (org rulesets) | Proposed |
|---|---|
| `sdlc ci`, `Code Quality (Gourmand) / Code Quality (Gourmand)`, `Constitution / Validate constitution` | `sdlc / check (pull_request)` |
| `Protect workflows`, `Gatehouse triage / Gatehouse triage` | `sdlc / gate (pull_request_target)` |

The ruleset requires both names with **GitHub Actions** pinned as the source
app.

**`check` is assured only because `gate` refuses workflow edits.** A
`pull_request` run executes the PR's copy of `ci.yml`, so whatever
`sdlc / check (pull_request)` reports is what that copy says. For a fork's PR
that would be worthless on its own. It counts because
`sdlc / gate (pull_request_target)` runs from the base definition and fails
any fork PR that touches `.github/workflows/` or `.sdlc/`. The two checks are
one control, and neither should be required without the other. (A same-repo
branch can edit its own `check` too, but its author already has write access
and the PR goes through review.)

**No skipped or no-op job can carry a required name.** A job that its `if:`
skips still reports a check, and a skipped check counts as passing. In one
file every run carries every job. So, without the event in the name, a
`pull_request` run would post a skipped `sdlc / gate` on the PR's head commit,
and a `pull_request_target` run would post a skipped `sdlc / check`. With the
event in `name:` (`jobs.<id>.name` may read the `github` context,
[contexts](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts)),
those skipped checks are `sdlc / gate (pull_request)` and
`sdlc / check (pull_request_target)`, which nothing requires. GitHub might
show a skipped job's name unevaluated. Then it shows the literal
`(${{ github.event_name }})`, which still differs from the required name.
Spike #103 checks which happens. Within its own event, neither required job
can reach a no-op: `check` always runs `sdlc ci`, and `gate` always runs the
guard and triage.

**Where I differ from "required names only from `pull_request` and `push`":**
the gate has to stay on `pull_request_target`. A `pull_request` run executes
the PR's copy of `ci.yml`, so a fork could edit the guard out of the very run
that judges it. `pull_request_target` runs "in the context of the default
branch of the base repository", and that is the whole reason `Protect
workflows` and Gatehouse triage are required from there today.

A fork's `pull_request` run could still add a job literally named
`sdlc / gate (pull_request_target)`. That is true today with
`Protect workflows`, and this design adds nothing to it. The org's fork-run
approval setting stops it on GitHub's side. On ours, the gha-runner hook
below refuses any fork job outright.

**The 50 re-run cap.** GitHub allows at most 50 re-runs of one workflow run.
Retriage re-runs `gate` once per reply, so a PR with more than 50 replies on
one head commit stops being re-triaged until its next push, which starts a new
run. Ashigaru's five-round bound keeps it far below that cap.

**Cutover order for the rulesets.** A new ruleset, targeting converted repos
by name, requires the two new checks. The old one keeps the five for repos not
yet converted. `cutover.py` moves each repo from the old target list to the
new one in the run that merges its pull request. Once the wave is done, the
old ruleset is deleted.

## Cutover prerequisites

Each of these must hold before the first canary repo converts.

1. **Secrets in environments, with a check that fails while any copy is
   left.**
   - The environments are `ai` (CLAUDE_CODE_OAUTH_TOKEN, OPENROUTER_API_KEY),
     `ashigaru` (ASHIGARU_APP_KEY; ASHIGARU_CLIENT_ID stays a variable),
     `registry` (QUAY_*, RHSM_*, REGISTRY_REDHAT_IO_*), `cosign-signing`
     (COSIGN_*, plus its own copy of QUAY_*) and `pypi` (no secret).
   - `cutover.py --check` is modelled on `provision-cosign-env.py`'s
     `stale()` and `org_stale()` (constitution `scripts/`, lines 306–326). For
     every moved name it reads both the repo's `actions/secrets` and the
     `actions/organization-secrets` the repo can see, plus the org's own list.
     It fails while any org-level or repo-level copy of a moved secret still
     exists. A refused read is an error, never "none found".
   - Deleting the org copies is a cutover step, done once all repos have the
     environment copies, and the check stays red until it is done.
2. **Environment rules equal to the tag ruleset.** The provisioner sets each
   environment's deployment policies: the default branch by name plus `v*.*.*`
   for `ai`, `ashigaru`, `registry` and `cosign-signing`, and `v*.*.*` alone
   for `pypi`. It **asserts** that every tag policy is exactly the pattern of
   the App-only tag ruleset (`TAG = "v*.*.*"` in `provision-cosign-env.py`),
   so the two cannot drift. It refuses while any existing `v*.*.*` tag names a
   commit off the default branch, as `stray_tags()` already does.
3. **sdlc's own key and channel.** sdlc's `cosign-signing` environment holds
   the new sdlc key, promote signs with annotations and publishes the
   pointer, and `sdlc-boot` is built with the key, serial floor and minimum
   version, its digest recorded.
4. **gha-runner rejects fork jobs** (separate gha-runner work). This is a job-started hook
   (`ACTIONS_RUNNER_HOOK_JOB_STARTED`) that reads the event payload and exits
   non-zero when `pull_request.head.repo.full_name` differs from
   `repository.full_name`. Then no fork's job runs on a self-hosted runner,
   whatever a `runs-on` expression or a fork's copy of the file says.
5. **PyPI trusted publishers** re-registered (below).
6. **Spike #103** passes on both runner kinds.

## PyPI: re-registering 22 trusted publishers

This is a human step, for Scott. A PyPI trusted publisher names the
repository, the workflow file and, optionally, the environment. Today's name
`publish.yml` with no environment, and after cutover each must name `ci.yml`
with environment `pypi`.

1. **Before the wave.** The provisioner creates the `pypi` environment in each
   of the 22 repos, with the single tag policy `v*.*.*`. `cutover.py` lists
   the 22 projects with their PyPI URLs.
2. **On PyPI, per project, before that repo converts.** At
   pypi.org/manage/project/<name>/settings/publishing, add a **new** trusted
   publisher: owner `crunchtools`, the repo, workflow `ci.yml`, environment
   `pypi`.
3. **When the repo's conversion PR merges, in the same sitting**, remove the
   old `publish.yml` publisher. After the merge `publish.yml` no longer
   exists. A stale publisher that names it would accept an upload from any
   future file of that name, with no environment gate.
4. **The check.** The wave report marks each repo whose PyPI project still
   lists a `publish.yml` publisher after its merge. Scott clears them from
   that list.

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
*Instead:* the file names a literal digest, and that bootstrap verifies the
real image itself. See "The trust root" above.

**5. A fork's reply cannot re-run triage.** A `pull_request_review_comment`
run uses the PR's copy of the workflow. For a fork's PR it gets a read-only
token and no secrets ([events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)),
and its checks do not count toward the ruleset (gatehouse#50, quoted in
`retriage.yml`). Today `gatehouse-retriage.yml` listens on `workflow_run`,
which "is able to access secrets and write tokens, even if the previous
workflow was not". The new file has no `workflow_run`. Instead, on a review
comment, the `gate` job re-runs `gate` inside the PR's `pull_request_target`
run. It does this with `GITHUB_TOKEN` and `actions: write`, and it holds no
secret.

Because that run executes the PR's copy of `ci.yml`, **no job holding a secret
may run on this event**. A same-repo branch could otherwise edit the file and
then comment, and so get the Ashigaru App key, which can tag past the cosign
ruleset. That is why `write` does not list this event. What the PR's copy can
reach here is `GITHUB_TOKEN`, and the author of a same-repo branch already has
that much access.

On a fork's PR the token is read-only, so the re-run fails. A fork
contributor's reply is then re-triaged at their next push, or when a
maintainer re-runs `gate`. That is a real regression, kept because forks are
rare here. Spike #103 confirms that a re-run started this way updates the
check the ruleset reads, as a re-run of the same job did in gatehouse#50.

**6. Which copy of `ci.yml` an event runs.**
- `pull_request_target` runs "in the context of the default branch of the base
  repository" ([events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)),
  so its jobs are the default branch's definitions. That is why `ai`, `write`
  and `gate` run there, and never under `pull_request`.
- `issues`, `schedule` and `repository_dispatch` run the default branch's
  copy, and so does `push` to the default branch.
- **`release` runs the tagged commit's copy.** Its `GITHUB_REF` is
  `refs/tags/<tag>`. So do pushes of tags, which this file does not
  subscribe to. Every `v*.*.*` tag is created by the Ashigaru App alone, on a
  merge commit of the default branch (the tag ruleset and `stray_tags()`).
  So a tag's copy of the file has been through review. That is why
  `cosign-signing`, `registry`, `ashigaru` and `ai` admit `v*.*.*`, and why
  `pypi` admits nothing else.
- `pull_request` and `pull_request_review_comment` run the merge commit's copy.
  So a fork can edit the jobs in its own run, as it can today. `gate` refuses
  the change, `check` holds nothing, and `gate`'s review-comment path holds no
  secret.
- `workflow_dispatch` runs the copy on whatever ref it is dispatched against.
- **An `if:` cannot keep a secret on the right ref.** A writer can dispatch the
  workflow against their own branch, or push a branch whose copy of `ci.yml`
  adds triggers, and either way the `if:` that runs is the one they wrote. So
  the environments' deployment rules are the control. Today the App key, the
  Claude, OpenRouter and quay secrets are org or repo secrets, which any
  branch's workflow can read. Moving them closes that gap for the current
  workflows too. The cost is cosmetic: the environment shows as a deployment
  in each PR's timeline.

Nothing changes here. Moving into one file does not move the boundary.

**7. Ashigaru `fix` no longer needs `workflow_run`.** Today it listens for
Gatehouse's completion. In one file it runs inside `ai`, right after the
review, in the same `pull_request_target` run. It runs on `ashigaru/` branches
in this repo only, and answers that review's findings and the open ones left
from earlier rounds. That is one trigger fewer, and the bound of five rounds
stays in sdlc.

**8. No variable chooses code.** A repo variable can be written by anyone
with write access to the repo, and it overrides the org's. So the image is a
literal digest in the file, never `vars.*`. The runner labels still come from
variables, as they do today, but only `check`, which holds nothing, reads the
self-hosted ones. The other jobs read `UNCACHED_RUNNER_LABEL` only for a
private repo, where there is no GitHub-hosted alternative.

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

- **The podman remote client and skopeo**: for driving the runner's socket
  (builds, nested runs, `podman login`) and for copying by digest. There is
  no engine inside the image: the socket is the engine.
- **cosign v2.6.5** (copied from its digest-pinned image), and **syft**: for
  `sdlc image promote`. They do what `scripts/sign-digest.sh` and
  `scripts/attest-digest.sh` already do in sdlc's own `container.yml`.
- **zizmor**: today it is installed by hash in the constitution's
  `requirements/zizmor.txt`.
- **claude**: the CLI from npm, at an exact version, under its own prefix as
  markdownlint-cli2 is.
- **cryptography** (or PyJWT): in an isolated venv, for App tokens.
- **The bootstrap** is a second, separate image (`sdlc-boot`), built from
  this repo. It holds cosign, sdlc's own public key (two during a rotation),
  the pointer's serial floor, the minimum version and a podman remote client,
  and nothing else. It changes rarely, and each
  change is a fleet edit of one digest.
- **gh** is not needed: sdlc calls the REST API itself, as `release_publish.py`
  and `update_tools.py` already do.

## The hard cases

**Artifacts between jobs.** These go away by design, not by workaround. Today's
hand-offs, and what each becomes:

- **Ashigaru agent → publish** (`change.patch` plus `result.json`; KBs of
  text). This becomes four job outputs of at most 112,500 bytes each (see "What
  crosses between jobs"). The split between the two jobs stays.
- **container-release build → gates** (`image.tar`, 100 MB–1 GB). This only
  happens on the path where `pushed == false`, a pull request with no registry
  credentials. With Dependabot gone, that is a fork's pull request alone. In
  the new file, no pull request reaches `image` at all. `check` builds and
  gates the image in place (the container stack of `sdlc ci`), holding no
  credential, so there is nothing to hand on. Outside pull requests, `image`
  pushes the candidate to quay by digest, and `needs.image.outputs` carries
  that digest to `promote`.
- **RPM build → publish** (ashigaru, claude-agent, petit; a few MB per
  distro). The split exists to keep `contents: write` off the job that runs
  `build.sh`. On a pull request, the build job holds nothing and publishes
  nothing. On a `release` event the code being built is the merged,
  Gatehouse-reviewed tag. The build runs in a nested container that holds no
  token, and then `write` uploads the RPMs as release assets through the REST
  API. Repo code and the write token never share a container.
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

**`container:` jobs and the socket.** Every job mounts the runner's container
socket, and nothing runs `--privileged`.
- **On gha-runner** this already works: the runner points
  `/var/run/docker.sock` at its rootless podman API (gha-runner README, "Job
  containers and services").
- **On GitHub-hosted runners** it is Docker's socket, on a VM thrown away
  after the job.

The bootstrap and `sdlc event` drive the socket. Repo code (tests, a
Containerfile build) runs only in the nested containers they start:
`--cap-drop=all`, `--network=none` where the check allows, no token, no
secret, and never the socket itself. The model runs nested too, holding the
Claude token it needs to call the API and nothing else: not the socket, not a
GitHub write token, not an App key. That is today's `run_in_image`, moved
from the host into a job container.

Two things are new and go in spike #103:
1. **Path mapping.** A container started through the socket mounts paths on
   the host, not on the job container. sdlc finds the host path of
   `$GITHUB_WORKSPACE` by inspecting its own container through the socket, and
   it mounts only that.
2. **Holding the socket is holding the runner.** On GitHub-hosted runners the
   Docker socket is root on the VM. A job with a secret also has that socket,
   but it runs no repo code beside it, only the verified sdlc image. The
   socket is never passed down to repo code.

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

The one thing left in a workflow that selects code is the bootstrap's digest.
It is a literal, so only a reviewed commit can change it, and it is the only
version in the file.

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
  the `promote` job always signs, and the cosign secrets are named in the
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
2. The cutover prerequisites above are met, in their order: environments and
   the `--check`, the environment rules, sdlc's key and channel with
   `sdlc-boot`, the gha-runner hook, PyPI publishers, the spike.
3. Org copies of the moved secrets are deleted, and `--check` goes green.
4. Three canary repos convert: mcp-wordpress, ubi10-core and acquacotta.
5. The fleet wave runs.
6. The reusables are deprecated a release later.
