# One workflow file, no marketplace actions

Design for a1051. Nothing here is implemented: it is the target that
`convert_repo.py` and `cutover.py` will be changed to write, and the list of
`sdlc` internals that have to exist first.

## The fleet file: 103 lines, eight jobs

Every fleet repo carries exactly this `.github/workflows/ci.yml`, byte for
byte, whatever kind of repo it is. The one exception is crunchtools/sdlc
itself, below. The file is 103 lines without comments. It contains no `uses:`
of any kind: no `actions/checkout`, no reusable-workflow call and no
third-party action. Every job runs one image, the sdlc **bootstrap**, by a
literal digest, and one command, `sdlc event`. That command reads
`GITHUB_EVENT_NAME`, the event payload and `GITHUB_JOB`, and runs whatever
that trust level does for that event in this repo.

```yaml
# One file, every repo, copied unchanged. A job is a trust level; `sdlc event`
# routes inside it. The only version here is the bootstrap's digest: it resolves
# quay.io/crunchtools/sdlc:<major>, verifies it against sdlc's own key and a
# minimum version, and runs it through the runner's container socket. Every
# secret is an org secret: a fork's PR gets none, pull_request_target runs the
# default branch's copy of this file, and only Prime's credential may change a
# workflow. Every job that holds one runs on our uncached runners, or GitHub's
# for a fork.
name: CI
on:
  push:
  pull_request:
  pull_request_target: { types: [opened, synchronize, reopened] }  # zizmor: ignore[dangerous-triggers]
  pull_request_review_comment: { types: [created, edited, deleted] }
  issues: { types: [opened, labeled, reopened] }
  schedule: [{ cron: "17 6 * * 1" }, { cron: "47 */6 * * *" }]  # weekly: audit, rebuild, keepalive; 6-hourly: parent image moved?
  workflow_dispatch: { inputs: { version: { type: string } } }
permissions: {}
jobs:
  check:  # no secret, read token: the one level a fork's PR runs; our uncached runners otherwise
    name: sdlc / check (${{ github.event_name }})  # required as "(pull_request)"
    if: >-
      contains(fromJSON('["pull_request","schedule"]'), github.event_name)
      || contains(fromJSON('["push","workflow_dispatch"]'), github.event_name) && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)
    runs-on: ${{ (github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository) && vars.UNCACHED_RUNNER_LABEL || 'ubuntu-latest' }}
    container: &boot { image: "quay.io/crunchtools/sdlc-bootstrap@sha256:5f25b7968ff9f14e0fa363ea3c25e0df4f6159abf43549653fff74eb674de060", volumes: ["/var/run/docker.sock:/var/run/docker.sock"] }
    permissions: { contents: read, security-events: write, actions: write }  # actions: the 60-day schedule keepalive
    outputs: { result: "${{ steps.e.outputs.result }}" }
    env: { GITHUB_TOKEN: "${{ github.token }}" }
    steps: &event [{ id: e, run: sdlc event }]
  ai:  # model keys and a read token, never an App key; hands its result on in four chunks of at most 112,500 bytes
    if: github.event_name == 'issues' || github.event_name == 'pull_request_target'
    runs-on: &secret ${{ (github.event_name == 'issues' && contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), github.event.issue.author_association) || github.event_name != 'issues' && (github.event_name != 'pull_request_target' || github.event.pull_request.head.repo.full_name == github.repository)) && vars.UNCACHED_RUNNER_LABEL == 'crunchtools-uncached' && 'crunchtools-uncached' || 'ubuntu-latest' }}
    container: *boot
    permissions: { contents: read, issues: read, pull-requests: read }
    outputs: { p0: "${{ steps.e.outputs.p0 }}", p1: "${{ steps.e.outputs.p1 }}", p2: "${{ steps.e.outputs.p2 }}", p3: "${{ steps.e.outputs.p3 }}" }
    env: { GITHUB_TOKEN: "${{ github.token }}", CLAUDE_CODE_OAUTH_TOKEN: "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}", OPENROUTER_API_KEY: "${{ secrets.OPENROUTER_API_KEY }}" }
    steps: *event
  image:  # verifies the parent itself, builds FROM @digest through the socket, gates, pushes; a PR's to candidates by OIDC, a merge adopts it
    name: sdlc / image (${{ github.event_name }})
    needs: check
    if: >-
      needs.check.result == 'success' && (github.event_name == 'pull_request' && fromJSON(needs.check.outputs.result || '{}').candidate
      || github.event_name != 'pull_request' && fromJSON(needs.check.outputs.result || '{}').image)
    runs-on: *secret
    container: *boot
    permissions: { contents: read, actions: read, checks: read, id-token: write }  # id-token: a PR's candidate by Quay robot federation, no secret; actions, checks: a merge reads the PR's record of it
    outputs: { result: "${{ steps.e.outputs.result }}" }
    env: { GITHUB_TOKEN: "${{ github.token }}", QUAY_USERNAME: "${{ github.event_name != 'pull_request' && secrets.QUAY_USERNAME || '' }}", QUAY_PASSWORD: "${{ github.event_name != 'pull_request' && secrets.QUAY_PASSWORD || '' }}",
           RHSM_ACTIVATION_KEY: "${{ github.event_name != 'pull_request' && secrets.RHSM_ACTIVATION_KEY || '' }}", RHSM_ORG_ID: "${{ github.event_name != 'pull_request' && secrets.RHSM_ORG_ID || '' }}",
           REGISTRY_REDHAT_IO_USER: "${{ github.event_name != 'pull_request' && secrets.REGISTRY_REDHAT_IO_USER || '' }}", REGISTRY_REDHAT_IO_PASSWORD: "${{ github.event_name != 'pull_request' && secrets.REGISTRY_REDHAT_IO_PASSWORD || '' }}" }
    steps: *event
  write:  # the Ashigaru App key, no model: verifies and tags a release merge; comments, labels, PRs, leased pushes
    needs: [check, ai]
    if: >-
      always() && !cancelled() && !inputs.digest
      && (github.event_name == 'pull_request_target' || github.event_name == 'issues'
          || needs.check.result == 'success' && (github.event_name == 'workflow_dispatch' || fromJSON(needs.check.outputs.result || '{}').write))
    concurrency: { group: "write-${{ github.event.pull_request.number || github.event.issue.number || github.ref }}", queue: max }
    runs-on: *secret
    container: *boot
    permissions: {}
    outputs: { release: "${{ steps.e.outputs.release }}" }
    env: { ASHIGARU_CLIENT_ID: "${{ vars.ASHIGARU_CLIENT_ID }}", ASHIGARU_APP_KEY: "${{ secrets.ASHIGARU_APP_KEY }}", VERSION: "${{ inputs.version }}", DIGEST: "${{ inputs.digest }}", SDLC_CHECK: "${{ needs.check.outputs.result }}",
           SDLC_AI: "${{ needs.ai.result }}", SDLC_AI_0: "${{ needs.ai.outputs.p0 }}", SDLC_AI_1: "${{ needs.ai.outputs.p1 }}", SDLC_AI_2: "${{ needs.ai.outputs.p2 }}", SDLC_AI_3: "${{ needs.ai.outputs.p3 }}" }
    steps: *event
  promote:  # the cosign key: signs, attests and moves tags on a digest it did not build; idempotent; version only from write
    needs: [check, image, write]
    if: always() && !cancelled() && needs.image.result == 'success' && github.event_name != 'pull_request'
    concurrency: { group: "promote-${{ github.repository }}", queue: max }
    runs-on: *secret
    container: *boot
    permissions: { contents: read }
    env: { GITHUB_TOKEN: "${{ github.token }}", SDLC_CHECK: "${{ needs.check.outputs.result }}", SDLC_IMAGE_RESULT: "${{ needs.image.outputs.result }}", SDLC_WRITE: "${{ needs.write.result }}", SDLC_RELEASE: "${{ needs.write.outputs.release }}",
           QUAY_USERNAME: "${{ secrets.QUAY_USERNAME }}", QUAY_PASSWORD: "${{ secrets.QUAY_PASSWORD }}", COSIGN_PRIVATE_KEY: "${{ secrets.COSIGN_PRIVATE_KEY }}", COSIGN_PASSWORD: "${{ secrets.COSIGN_PASSWORD }}" }
    steps: *event
  pypi:  # OIDC only; re-derives the release from the API itself and must agree with write
    needs: [check, write]
    if: always() && !cancelled() && needs.check.result == 'success' && github.event_name == 'push' && fromJSON(needs.check.outputs.result || '{}').release.publish == 'pypi'
    runs-on: *secret
    environment: pypi
    container: *boot
    permissions: { contents: read, id-token: write }
    env: { GITHUB_TOKEN: "${{ github.token }}", SDLC_WRITE: "${{ needs.write.result }}", SDLC_RELEASE: "${{ needs.write.outputs.release }}" }
    steps: *event
  gate:  # required: guard, review done on this head, every finding answered; no secret. A collaborator's reply re-runs it where it counts
    name: sdlc / gate (${{ github.event_name }})  # required as "(pull_request_target)"
    needs: [ai, write]
    if: >-
      always() && (github.event_name == 'pull_request_target'
      || github.event_name == 'pull_request_review_comment' && contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), github.event.comment.author_association))
    concurrency: { group: "gate-${{ github.event.pull_request.number }}-${{ github.event_name }}" }
    runs-on: ${{ vars.UNCACHED_RUNNER_LABEL == 'crunchtools-uncached' && 'crunchtools-uncached' || 'ubuntu-latest' }}  # no repo code runs here, a fork's PR included
    container: *boot
    permissions: { contents: read, pull-requests: write, actions: write }
    env: { GITHUB_TOKEN: "${{ github.token }}", SDLC_AI: "${{ needs.ai.result }}", SDLC_WRITE: "${{ needs.write.result }}" }
    steps: *event
  renovate:  # the Renovate App key alone, no GITHUB_TOKEN; the constitution only: the weekly run for real, a bare dispatch dry
    if: >-
      github.repository == 'crunchtools/constitution'
      && (github.event.schedule == '17 6 * * 1'
          || github.event_name == 'workflow_dispatch' && !inputs.version && github.ref == format('refs/heads/{0}', github.event.repository.default_branch))
    concurrency: { group: renovate, queue: max }
    timeout-minutes: 55  # the minted token lives an hour
    runs-on: *secret
    environment: renovate
    container: *boot
    permissions: {}
    env: { RENOVATE_APP_KEY: "${{ secrets.RENOVATE_APP_KEY }}" }
    steps: *event
```

**Why 103 lines and not 20.** GitHub makes a separate job only where the
permissions, the secrets, the environment or the runner differ. Seven levels
differ, and each job needs its `if`, `runs-on`, `environment`, `container`,
`permissions`, `env` and `steps` lines. Merging any two would put a secret, a
write grant or a signing key somewhere it must not be. Everything else is
already out of the file:
- **Versions:** the only one is the bootstrap's digest.
- **Tasks:** all routing is `sdlc event`.
- **Repo kind:** nothing here depends on it.

YAML anchors (`&boot`, `&event`) save the repeats. GitHub accepts anchors but
not merge keys (`<<`), and rejects unknown top-level keys.

**An image-only repo (ubi10-core) runs the same file.** Its `check` reports
`image: true` on a default-branch push, on the weekly rebuild, and on any
6-hourly run that finds its parent image's digest has moved. Then `image` and
`promote` run. A Python library's `check` reports `release.publish: "pypi"` on
the push that merges its release PR, so `pypi` runs then. A docs repo reports
neither, and those jobs never start. Nothing about a repo's kind lives in the
file, so `convert_repo.py` never edits it and the fleet cannot drift.

**Downstream rebuilds poll; nobody dispatches.** A child image rebuilds when
its 6-hourly `check` sees its `FROM` digest change. So the
`parent-image-updated` dispatch, its trigger and the App token it needed are
all gone. The cost is latency: up to six hours per level of the image tree,
where a dispatch took minutes.
- **`check`'s plan is only a plan.** It decides whether `image` runs, and
  carries no digest into it.
- **`image` resolves and verifies the parent itself.**
  - A crunchtools parent is checked with the fleet key and
    `-a repo=<parent repo>`.
  - A `registry.redhat.io` or `registry.access.redhat.com` parent is checked
    with Red Hat's release key, as gha-runner's `policy.json` already does.
  - A `quay.io/hummingbird/<name>` parent is unsigned, being a mirror, so
    its digest is checked at `registry.access.redhat.com/hi/<name>` with the
    same key.
  - The build then runs `FROM` that verified `@sha256:` digest, whatever tag
    the Containerfile names.
- **Quiet repos stay scheduled.** GitHub disables a public repo's schedule
  after 60 days without activity, and a quiet image repo would then stop
  rebuilding. On the weekly run, `check` re-enables its own workflow through
  the API (`PUT /actions/workflows/ci.yml/enable`). That resets the 60-day
  clock, and it is why `check` carries `actions: write`. Its nested repo code
  never sees that token.

**Bottom line.**
- 69 of the 70 public org repos have workflows today, and every one of them
  carries at least one third-party pin.
- **65 of the 69 end with this one file and no `uses:` at all.**
- The other four carry this file too, but each keeps its package release
  workflow. These are ashigaru (`rpm.yml`), claude-agent and petit
  (`packages.yml`), and crunchtools/packages (`publish.yml`, which builds the
  package repository and deploys Pages). They are out of scope and keep their
  current path.
- mcp-atlassian has to delete the two upstream workflows its fork carries.
- Private repos are a separate question, under "Private repos and the org's
  plan".

## The trust root: sdlc-bootstrap, `:<major>` and a minimum version

The fleet file names `quay.io/crunchtools/sdlc-bootstrap@sha256:5f25b7968ff9f14e0fa363ea3c25e0df4f6159abf43549653fff74eb674de060`. That is a
literal, so only a reviewed commit to `ci.yml` can change it; no variable can.
`sdlc-bootstrap` is `bin/sdlc-bootstrap` grown into an image. It holds cosign, the
**sdlc public key**, the major version it follows and a **minimum version**,
all baked in. In every job it does five things:
1. It resolves `quay.io/crunchtools/sdlc:<major>` to a digest.
2. It verifies that digest:
   `cosign verify --key sdlc.pub -a repo=crunchtools/sdlc` (`--insecure-ignore-tlog`, as today).
3. It reads the `version` annotation and the image's `version` label. It
   refuses unless they agree and are at or above the baked minimum.
4. It runs `sdlc event` from that digest, as a sibling container on the
   runner's socket.
5. That container runs repo code only in nested containers that hold no
   GitHub token, no App key and never the socket.

Any failure is a 2, "GATE CANNOT RUN". An sdlc release needs no `ci.yml` edit:
sdlc's `sign` moves `:<major>`, and the next job runs it.

**sdlc's key is not the fleet key.** Its private half lives only in sdlc's
`sdlc-signing` environment, so a fleet repo's signing key can never produce
something `sdlc-bootstrap` will run. The fleet key keeps signing the fleet's images.

**Accepted risk.** Write access to `quay.io/crunchtools/sdlc` is sdlc's own
quay robot, held only in sdlc's `registry` and `sdlc-signing` environments,
plus the quay org's admins. The fleet's QUAY_* robot has no write there.
Anyone with that access can
point `:<major>` at any **older signed** sdlc of the same major that is at or
above the boot minimum, and the fleet will run it. They cannot make it run
anything sdlc's key did not sign. The recovery is to raise the minimum, which
means a new `sdlc-bootstrap` digest in `ci.yml`. That is a fleet edit only Josui
Prime makes. A signed pointer with expiry and a deny list was designed to
close this window, and it was dropped: two review rounds of blockers in that
machinery cost more than the window it closed.

**Rollback** is the same mechanism used on purpose. An approved job in sdlc
retags `:<major>` to an older digest that sdlc already signed (below).
Break-glass is that same rollback; there is no other mode and no deny.

**Key rotation.**
1. **Planned.**
   - Ship an `sdlc-bootstrap` that trusts both public keys (fleet edit 1).
   - Sign every later release with the new key only.
   - Ship an `sdlc-bootstrap` with the new key alone and a minimum at the first
     new-key release (fleet edit 2).
   - Destroy the old private key.
2. **Compromise.** Ship an `sdlc-bootstrap` with a new key and a minimum above
   anything the old key signed. That is one emergency fleet edit, and until it
   merges the fleet is exposed.

Laptops keep `.sdlc/bin/sdlc`, which does the same resolve-and-verify against
the same key.

## sdlc's own workflow: 98 lines, the stated exception

crunchtools/sdlc builds the image that `sdlc-bootstrap` runs, so the jobs that
build and sign it cannot run from `sdlc-bootstrap`. It is the one repo whose
`ci.yml` differs from the fleet file:

```yaml
# crunchtools/sdlc's own ci.yml: the trust root, and the one exception to
# "every repo, byte for byte". A pull request's sdlc code runs only in `test`,
# which holds no write permission, no secret and no socket of ours. Everything
# that holds a token, a socket or a key runs reviewed default-branch code or a
# released sdlc. ai, write and gate are the fleet's jobs, unchanged. sign never
# executes a release image: cosign runs from the digest-pinned image below, and
# the release is data. Inputs reach scripts only through env:.
name: CI
on:
  push:
  pull_request:
  pull_request_target: { types: [opened, synchronize, reopened] }  # zizmor: ignore[dangerous-triggers]
  pull_request_review_comment: { types: [created, edited, deleted] }
  issues: { types: [opened, labeled, reopened] }
  schedule: [{ cron: "17 6 * * 1" }]  # weekly: rebuild what :<major> carries, audit, keepalive
  workflow_dispatch:
    inputs:
      version: { type: string, default: "" }  # alone: open the release PR; with digest: roll :<major> back to it
      digest: { type: string, default: "" }
permissions: {}
env: { COSIGN_IMAGE: "ghcr.io/sigstore/cosign/cosign@sha256:ad281047f85c5e1fc6ffbc30c2b55be3b07b4032bef715a12122ce5829619aca" }  # v2.6.5
jobs:
  test:  # the PR's own sdlc code, and nothing to steal: read-only contents, no secret, GitHub's machine
    name: sdlc / check (${{ github.event_name }})  # required as "(pull_request)"
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    permissions: { contents: read }
    env: { MODE: test }
    steps:  # sdlc is public: the fetch needs no token and persists none; origin names the repo for the validator's exemptions
      - &fetch { run: 'git init -q . && git remote add origin "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY" && git fetch -q --depth=1 origin "$GITHUB_SHA" && git checkout -q FETCH_HEAD' }
      - run: scripts/self.sh  # podman build, then `sdlc ci` and the contract test from the built digest
  check:  # default-branch code only: builds it, runs `sdlc ci` from it, plans, uploads SARIF, keeps the schedule alive
    name: sdlc / plan (${{ github.event_name }})  # never the required name: test reports that
    if: >-
      github.event_name == 'schedule'
      || contains(fromJSON('["push","workflow_dispatch"]'), github.event_name) && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)
    runs-on: ${{ vars.UNCACHED_RUNNER_LABEL || 'ubuntu-latest' }}
    permissions: { contents: read, security-events: write, actions: write }
    outputs: { result: "${{ steps.s.outputs.result }}" }
    env: { GITHUB_TOKEN: "${{ github.token }}", MODE: check }
    steps: [*fetch, { id: s, run: scripts/self.sh }]
  ai:  # model keys and a read token, never an App key; hands its result on in four chunks of at most 112,500 bytes
    if: github.event_name == 'issues' || github.event_name == 'pull_request_target'
    runs-on: &secret ${{ (github.event_name == 'issues' && contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), github.event.issue.author_association) || github.event_name != 'issues' && (github.event_name != 'pull_request_target' || github.event.pull_request.head.repo.full_name == github.repository)) && vars.UNCACHED_RUNNER_LABEL == 'crunchtools-uncached' && 'crunchtools-uncached' || 'ubuntu-latest' }}
    container: &boot { image: "quay.io/crunchtools/sdlc-bootstrap@sha256:5f25b7968ff9f14e0fa363ea3c25e0df4f6159abf43549653fff74eb674de060", volumes: ["/var/run/docker.sock:/var/run/docker.sock"] }
    permissions: { contents: read, issues: read, pull-requests: read }
    outputs: { p0: "${{ steps.e.outputs.p0 }}", p1: "${{ steps.e.outputs.p1 }}", p2: "${{ steps.e.outputs.p2 }}", p3: "${{ steps.e.outputs.p3 }}" }
    env: { GITHUB_TOKEN: "${{ github.token }}", CLAUDE_CODE_OAUTH_TOKEN: "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}", OPENROUTER_API_KEY: "${{ secrets.OPENROUTER_API_KEY }}" }
    steps: &event [{ id: e, run: sdlc event }]
  write:  # the Ashigaru App key, no model: verifies and tags a release merge; comments, labels, PRs, leased pushes
    needs: [check, ai]
    if: >-
      always() && !cancelled() && !inputs.digest
      && (github.event_name == 'pull_request_target' || github.event_name == 'issues'
          || needs.check.result == 'success' && (github.event_name == 'workflow_dispatch' || fromJSON(needs.check.outputs.result || '{}').write))
    concurrency: { group: "write-${{ github.event.pull_request.number || github.event.issue.number || github.ref }}", queue: max }
    runs-on: *secret
    container: *boot
    permissions: {}
    outputs: { release: "${{ steps.e.outputs.release }}" }
    env: { ASHIGARU_CLIENT_ID: "${{ vars.ASHIGARU_CLIENT_ID }}", ASHIGARU_APP_KEY: "${{ secrets.ASHIGARU_APP_KEY }}", VERSION: "${{ inputs.version }}", DIGEST: "${{ inputs.digest }}", SDLC_CHECK: "${{ needs.check.outputs.result }}",
           SDLC_AI: "${{ needs.ai.result }}", SDLC_AI_0: "${{ needs.ai.outputs.p0 }}", SDLC_AI_1: "${{ needs.ai.outputs.p1 }}", SDLC_AI_2: "${{ needs.ai.outputs.p2 }}", SDLC_AI_3: "${{ needs.ai.outputs.p3 }}" }
    steps: *event
  gate:  # required: guard, review done on this head, every finding answered; no secret. A collaborator's reply re-runs it where it counts
    name: sdlc / gate (${{ github.event_name }})  # required as "(pull_request_target)"
    needs: [ai, write]
    if: >-
      always() && (github.event_name == 'pull_request_target'
      || github.event_name == 'pull_request_review_comment' && contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), github.event.comment.author_association))
    concurrency: { group: "gate-${{ github.event.pull_request.number }}-${{ github.event_name }}" }
    runs-on: ${{ vars.UNCACHED_RUNNER_LABEL == 'crunchtools-uncached' && 'crunchtools-uncached' || 'ubuntu-latest' }}  # no repo code runs here, a fork's PR included
    container: *boot
    permissions: { contents: read, pull-requests: write, actions: write }
    env: { GITHUB_TOKEN: "${{ github.token }}", SDLC_AI: "${{ needs.ai.result }}", SDLC_WRITE: "${{ needs.write.result }}" }
    steps: *event
  image:  # a release build only: a verified release merge, or the version :<major> carries, weekly
    needs: [check, write]
    if: always() && !cancelled() && needs.check.result == 'success' && (fromJSON(needs.check.outputs.result || '{}').release || github.event_name == 'schedule')
    runs-on: ubuntu-latest
    permissions: { contents: read }
    outputs: { result: "${{ steps.s.outputs.result }}" }
    env: { GITHUB_TOKEN: "${{ github.token }}", MODE: "${{ github.event_name == 'schedule' && 'weekly' || 'release' }}", SDLC_WRITE: "${{ needs.write.result }}",
           SDLC_RELEASE: "${{ needs.write.outputs.release }}", SDLC_QUAY_USERNAME: "${{ secrets.SDLC_QUAY_USERNAME }}", SDLC_QUAY_PASSWORD: "${{ secrets.SDLC_QUAY_PASSWORD }}" }
    steps: [*fetch, { id: s, run: scripts/self.sh }]
  approve:  # no secret; sdlc-approval's reviewers (Josui Prime, Scott; prevent_self_review) are the approval
    if: github.event_name == 'workflow_dispatch' && inputs.digest != '' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)
    runs-on: ubuntu-latest
    environment: sdlc-approval
    permissions: {}
    steps: [{ run: "true" }]
  sign:  # the only job with the sdlc key: cosign and validation on the host; release images are data
    needs: [image, approve]
    if: always() && !cancelled() && github.ref == format('refs/heads/{0}', github.event.repository.default_branch) && (needs.image.result == 'success' || needs.approve.result == 'success')
    concurrency: { group: sdlc-sign, queue: max }
    runs-on: ubuntu-latest
    environment: sdlc-signing
    permissions: { contents: read }
    env: { MODE: "${{ github.event_name == 'workflow_dispatch' && 'rollback' || github.event_name == 'schedule' && 'weekly' || 'release' }}", EVENT: "${{ github.event_name }}",
           BUILT: "${{ needs.image.outputs.result }}", APPROVED: "${{ needs.approve.result }}", VERSION: "${{ inputs.version }}", DIGEST: "${{ inputs.digest }}",
           SDLC_KEY: "${{ secrets.SDLC_COSIGN_KEY }}", SDLC_KEY_PASSWORD: "${{ secrets.SDLC_COSIGN_PASSWORD }}",
           SDLC_QUAY_USERNAME: "${{ secrets.SDLC_QUAY_USERNAME }}", SDLC_QUAY_PASSWORD: "${{ secrets.SDLC_QUAY_PASSWORD }}" }
    steps: [*fetch, { run: scripts/self.sh }]
```

**It is longer than the 54-line draft because it is now converted like every
other repo.** It has the fleet's `ai`, `write` and `gate`, byte for byte,
running from `sdlc-bootstrap`. So sdlc gets Gatehouse review, Ashigaru and the
required `sdlc / gate (pull_request_target)`. Only `test`, `check`, `image`,
`approve` and `sign` are its own.

**The fleet invariant, applied to sdlc's own pull requests: code a PR wrote
never runs where a token with write, a secret or a socket of ours is.** In the
fleet that holds because PR code only ever runs nested, under the reviewed
orchestrator. In sdlc the PR's code *is* the orchestrator, so sdlc runs it in
a job that has nothing to take:
- **`test`** is the required `sdlc / check (pull_request)`. It runs only on
  `pull_request`, on GitHub's `ubuntu-latest`, with `permissions: { contents:
  read }`, no `environment`, no secret and no `container:` socket mount. It
  fetches the PR's merge commit with `git` (sdlc is public: no token, nothing
  persisted), builds that commit's image with podman, and runs `sdlc ci` and
  the contract test from it by digest. Whatever the PR's code does, it does
  it on a throwaway VM with a read-only token.
- **`check`** never runs on `pull_request`. It runs on a default-branch push,
  the schedule and a dispatch **from the default branch** (a dispatch can name
  any ref, so its `if:` checks `github.ref`; `approve` and `sign` carry the same
  test), so the code it builds and runs is reviewed, merged code. It holds the `security-events` and `actions` writes, uploads
  SARIF, keeps the schedule alive, and plans, as the fleet's `check` does.
- **`ai`, `write` and `gate`** run `sdlc-bootstrap`, which runs a **released**,
  signed sdlc, never the PR's tree.
- **`image`** builds **only releases**, so its `if:` keys on the plan's
  `release`, not on the `image` key the fleet's `image` job reads: sdlc's plan
  never reports `image`, because nothing but a release is ever built to quay.
  On a push, that means the merge of a
  release PR that `write` has verified with the full test (under "Releases run
  from the merge"). The version comes from `write`'s output, which is read
  from the tree. On the weekly schedule it means **the version `:<major>`
  carries now**, read from that image's `version` annotation and label (which
  must agree), and rebuilt from the commit that image's
  `org.opencontainers.image.revision` label names: never from the default
  branch, never from the newest tag, and never from the `vX.Y.Z` tag itself,
  which anyone with `contents: write` could move. `sign` refuses a rebuild
  whose revision is not the one `:<major>` carries. After a rollback, the weekly run
  rebuilds the rolled-back version. It pushes a candidate by digest. A
  non-release push builds nothing to quay, so no intermediate digest ever
  exists to be signed.
- **`sign`** is the only job with the sdlc key, in `sdlc-signing` (default
  branch only, no reviewers). It runs on the host: validation in the reviewed
  commit's `scripts/self.sh`, and cosign from the digest-pinned
  `COSIGN_IMAGE` named in the file. **It never executes the image it signs.**
  The release is data, a digest to sign and retag. It re-validates
  everything itself, starting from `MODE`, which is passed explicitly and
  checked against `EVENT`.

  **What `sign` takes from `image`, and what it checks itself.** It trusts
  `image`'s output for one thing: which digest was built. That is reviewed
  default-branch code in a job holding only the registry robot. Everything
  else it re-validates before signing:
  - the digest is a well-formed `sha256:` in `quay.io/crunchtools/sdlc`;
  - the image at that digest carries a `version` annotation and label that
    agree, are strict SemVer, and are of the current major;
  - that version matches what `MODE` allows (below);
  - **`:<major>` still carries the version it expects**, read again
    immediately before the retag, inside the `sdlc-sign` concurrency group.
    A weekly run queued behind a release therefore finds `:<major>` on a
    newer version and refuses, rather than landing late and moving it back.

  **`:<major>` moves only forward on a release, or anywhere on an approved
  rollback.** The modes:
  - **`release`:** a verified release-merge push. The version is `write`'s
    output, and it must be newer than the one `:<major>` carries. It signs the
    built digest with `-a repo=crunchtools/sdlc -a version=X.Y.Z` and moves
    `X.Y.Z`, `<major>` and `latest`.
  - **`weekly`:** a rebuild of the version `:<major>` carries. The built
    version must equal it, and so must the built revision label. It re-signs and moves `X.Y.Z` and `:<major>` to the
    rebuilt digest, the version unchanged. It refuses any other version,
    older or newer, so it can neither undo a rollback nor stand in for a
    release.
  - **`rollback`:** a dispatch with `version` and `digest`. It needs `approve`
    to have succeeded, `VERSION` to be strict SemVer of the current major at
    or above the boot minimum, and `DIGEST` to already carry sdlc's signature
    with exactly that version. Then it retags `:<major>` to `DIGEST`. Nothing
    is built or signed. `write` does not run on that dispatch: its `if:`
    carries `!inputs.digest`, and `sdlc event` also exits doing nothing when
    `DIGEST` is set, so a rollback cannot open a release PR for `VERSION`.
  - **`bootstrap`:** once, the first release. `write` runs in sdlc-bootstrap,
    which verifies `:<major>`, so no release exists for it to verify until
    one is made without it. A dispatch with `version` and `bootstrap: true`
    needs `approve` to have succeeded, no `:<major>` at all, the tree at
    `VERSION`, and tag `vVERSION` (pushed by the old release path) at the
    dispatched commit. `image` then builds and pushes both candidates, and
    `sign` signs and tags them as a release would. Once `:<major>` exists it
    refuses forever.
- **`approve`** runs in `sdlc-approval`. That environment holds no secret,
  admits the default branch only, and has required reviewers Josui Prime and
  Scott with `prevent_self_review` on. sdlc is public, so reviewers are
  available on any plan.

**What became of sdlc's other workflows**

| Workflow today | Fate |
|---|---|
| `ashigaru.yml` | deleted; its jobs are `ai` and `write` |
| `gatehouse.yml`, `gatehouse-retriage.yml` | deleted; `ai`, `write`, `gate` |
| `gourmand.yml`, `constitution.yml` | deleted; `check` runs both through `sdlc ci` |
| `container.yml` (build, verify-digest, promote) | deleted; `check`, `image`, `sign` |
| `release.yml` (open and tag with the App) | deleted; `write` opens, verifies and tags, and `sign` publishes |
| `update-tools.yml` | deleted; Renovate updates the `Containerfile` pins |
| `dependabot-automerge.yml` | deleted with Dependabot |

sdlc is the one repo with environments: `registry`, `sdlc-signing` and
`sdlc-approval`. Its `ai` and `write` read the org secrets like any repo's.
Its `registry` and `sdlc-signing` hold **sdlc's own quay robot**
(`SDLC_QUAY_USERNAME`, `SDLC_QUAY_PASSWORD`), the only credential that can
write `quay.io/crunchtools/sdlc` and `sdlc-bootstrap`. The fleet's `QUAY_*`
robot cannot, so no fleet repo can push or retag the trust root. It moves to
the new ruleset like any converted repo.

## Releases run from the merge, and every job checks what it needs

1. **Open.** `workflow_dispatch` with `version` makes `write` open
   `ashigaru/release-X.Y.Z` from `chore/release-X.Y.Z`. This is unchanged.
2. **Publish.** The push to the default branch that merges that PR carries the
   release.

   **The plan.** `check` notices the merge and reports
   `release: {version, publish}`. That is a plan, not an authority: it decides
   which jobs are **needed**.

   **Verification.** `write` re-derives the release from the API, and refuses
   unless every one of these holds:
   - `GET /repos/{repo}/commits/{github.sha}/pulls` returns exactly one pull
     request, and it is merged;
   - its `merge_commit_sha` equals `github.sha`;
   - its author is the Ashigaru App;
   - its base is the default branch;
   - its head repo is this repo;
   - its head ref is `ashigaru/release-X.Y.Z`;
   - that `X.Y.Z` equals the version in the merged tree (`pyproject.toml` or
     the repo's version file, and the newest CHANGELOG heading).

   Only then does `write` tag `vX.Y.Z` on that commit with the App token,
   publish the GitHub Release, and output `release`.

   **Publishing.**
   - `promote` takes the version **only from `write`'s output**, never from
     `check`, and moves `X.Y.Z` with `latest`.
   - `pypi` re-derives the release from the API itself with the same tests,
     using its read token, and refuses unless it agrees with `write`'s output.
     Then it publishes by OIDC.

**"Needed" comes from the plan, and a skipped needed job is a failure.** A job
whose `if:` is false is `skipped`, and GitHub lets a dependent job read that as
benign. sdlc does not. Each job receives the results of the jobs it needs
(`SDLC_AI`, `SDLC_WRITE`) together with `check`'s plan. A job that acts on
the plan also requires `needs.check.result == 'success'` in its `if:`. A
`check` that failed may have printed a plan before failing, and nothing acts
on it: `image`, `write` (outside the triage events) and `pypi` all carry that
test, and `promote` follows `image`. If the plan says a job was needed and
its result is anything but `success` (`skipped`, `failure`, `cancelled`), the
dependent job fails. Three cases:
- `promote` on a release merge fails if `write` did not succeed.
- `pypi` fails if `write` did not succeed.
- `gate` fails unless `ai` and `write` both succeeded for this head.

The `always()` in those `if:`s exists to make that judgement **run**, never to
let it pass.

Nothing runs from a tag, and nothing triggers on `release` or a closed pull
request. The App-only tag ruleset stays as it is today.

## Jobs are trust levels

| Job | Check name | Secrets (org secrets) | Runs on | Never holds |
|---|---|---|---|---|
| `check` | `sdlc / check (pull_request)` (required) | none; read token, `security-events`, the socket | pull_request (forks on `ubuntu-latest`), default-branch push, schedule, dispatch; our **uncached** runners otherwise | any secret |
| `ai` | | Claude and OpenRouter keys | issues, `pull_request_target`; our **uncached** runners (a fork's `pull_request_target`, or an outsider's issue, on `ubuntu-latest`) | an App key, a write token |
| `image` | | quay, RHSM, registry.redhat.io | when `check` plans an image; our **uncached** runners | the cosign key |
| `write` | | Ashigaru App key; queued | `pull_request_target`, issues, a `version` dispatch; otherwise only when a successful `check` plans `write` (a release merge, the weekly audit); never a rollback dispatch; our **uncached** runners (a fork's `pull_request_target`, or an outsider's issue, on `ubuntu-latest`) | a model |
| `promote` | | cosign key, quay; queued | after `image`, and after `write` on a release; our **uncached** runners | an App key, a build |
| `pypi` | | none: OIDC, environment `pypi` for the publisher | a release merge that publishes to PyPI; our **uncached** runners | anything else |
| `gate` | `sdlc / gate (pull_request_target)` (required) | none; `pull-requests: write`, `actions: write` | `pull_request_target`; a collaborator's review comment; `ubuntu-latest` | any secret |
| `renovate` | | `renovate`: Renovate App key; no `GITHUB_TOKEN`; queued | crunchtools/constitution only: the weekly schedule, a bare dispatch from the default branch; our **uncached** runners | a token of this repo's, a build |

This table is the fleet file. In crunchtools/sdlc alone, the required
`sdlc / check (pull_request)` is `test`: `pull_request` only,
`contents: read`, no socket and no secret. sdlc's `check` is named
`sdlc / plan`, holds the socket and `security-events`, and never runs on
`pull_request` (under "sdlc's own workflow").

**Runners.** Every job that holds a secret (`ai`, `image`, `write`,
`promote`, `pypi`, `renovate`) runs on Exedore's **uncached** runner class,
`crunchtools-uncached` (Scott, 2026-11-23). Its container storage is scratch,
emptied before and after every job, and the only thing it shares between jobs
is the warm image store, mounted read-only: a job can run an image from it and
cannot add, change or remove one. Only the warm timer writes it. The job
still checks what it runs: `sdlc-bootstrap` by its literal digest, sdlc against
sdlc's key, and `image`'s parent against the fleet key or Red Hat's. So the
store saves pulls and is trusted for nothing. A secret job
therefore starts from the same state as a GitHub VM, on a machine we run.
- **The label is a literal, and the variable is only a switch.** The six
  jobs share one `runs-on` (`&secret`) that yields `crunchtools-uncached` only
  when `vars.UNCACHED_RUNNER_LABEL` is exactly that, and `ubuntu-latest`
  otherwise. Clearing the org variable sends them back to GitHub with no
  commit. A repo variable can only do the same: it cannot name
  `crunchtools-cached`, or any other label, for a job that holds a secret.
- **A fork's `pull_request_target` stays on `ubuntu-latest`.** `ai` reads the
  fork's diff and `write` acts on it, so both route a fork, or a deleted one
  (no head repo), to GitHub. A same-repo pull request and every
  default-branch event go to Exedore.
- **A stranger's issue stays on `ubuntu-latest`** (Josui Prime, a1100). An
  `issues` event goes to Exedore only when the issue's `author_association`
  is `OWNER`, `MEMBER` or `COLLABORATOR`; anyone else's runs on GitHub.
- **Today the barrier is the route, not a hook.** The gha-runner fork-reject
  hook is prerequisite 5 and does not exist yet. Until it does, what keeps a
  fork off Exedore is this `runs-on` and the org's "Require approval for all
  external contributors" setting (prerequisite 11).
- **`check`**, which holds nothing, keeps its own rule: our uncached runners,
  a fork's PR on `ubuntu-latest`. **`gate`** stays on `ubuntu-latest`.
- **Never the cached class.** Its persistent layer storage lets one same-repo
  PR's build poison a later job (gha-runner `deploy/exedore/README.md`), so
  no job here uses `CACHED_RUNNER_LABEL`.
- **sdlc's own** `image`, `approve` and `sign` run where its other jobs do:
  Exedore while the variable is visible to it, GitHub's machines otherwise.
  sdlc is public, so today that is GitHub's.

`renovate` (a1099) shares that runner. It walks every repo in the org once a
week, within 55 minutes. Its key is read once, used only to mint tokens, and
dropped. Renovate's package managers do run dependency code (a Python lock
update builds an sdist, which runs its `setup.py`), and Renovate hands that
code its token on purpose: as `GIT_CONFIG_VALUE_n` to uv, poetry, pdm and
gomod, and in the clone's origin URL. No option turns that off (sdlc#121), so
the job lists the repos itself with a metadata-only token and gives each repo
a nested container of its own, from the job's own image, holding a token
minted for that repo alone: no socket, no `--volumes-from`, every capability
dropped, a read-only root and an anonymous work volume. Renovate in it is
started by an execute-only node, so it is not dumpable and nothing it runs can
read its `/proc/<pid>/environ` or ptrace it; the contract test proves that
against the built image. Each container has a unique name and podman's own
time limit, and it is removed, then its token revoked, on every path, a
cancel included.

**Private repos and the org's plan.** The plan could not be verified from this
seat: the org's `plan` field needs an admin token. Two facts point to GitHub
Free:
- constitution 1.48.0 records that the org's hosted minutes ran out with a
  $0 limit;
- every private repo runs on our runners for that reason.

On Free, GitHub's docs say "environment secrets are only available in public
repositories". Deployment branch policies in private repos need Pro or Team,
and required reviewers in private repos need Enterprise. So on Free a private
repo can hold none of this design's secrets in an environment, and it has no
hosted minutes to run them on. The private repos named in this repo and the
constitution are openbao, rhel10-bootc-workstation and josui-skills; the full
list needs an admin listing (`type=private`). The proposals, in order:
1. **Move the org to GitHub Team.** That brings environment secrets and branch
   policies to private repos and 3,000 hosted minutes, with a spending limit
   above $0. Everything here then holds for private repos unchanged. This is a
   cost decision for Scott.
2. **Until then, private repos stay out of the wave.** They keep their current
   workflows and secrets, on the uncached runner as constitution 1.48.0 has
   them, and `cutover.py` refuses a private repo.
3. Where a private repo needs no privacy, making it public moves it into the
   wave.

**Every secret is an org secret, keyed once, and no fleet repo has an
environment to set up** (Scott, 2026-11-23: nothing per repo, because every
container build will be signed). What keeps a secret on the right code:
- a fork's `pull_request` gets no secret at all, org secrets included;
- `pull_request_target`, `issues`, `schedule` and default-branch `push` run
  the default branch's copy of `ci.yml`;
- every other run (a pushed branch, a branch dispatch, a same-repo PR's
  review-comment run) runs a copy a writer could have changed, and changing a
  workflow needs the workflows permission. Only Prime's credential holds it:
  the Ashigaru App and the seats' tokens do not, so no writer but Prime can
  stage a branch workflow that reads a secret.

sdlc alone keeps environments (`registry`, `sdlc-signing`, `sdlc-approval`):
its robot and key must stay out of the fleet's reach, and it is one repo.

**The splits are forced.**
- **`ai` / `write`:** "the credential that can write never shares a machine
  with the LLM" (crunchtools/ashigaru `.github/workflows/code.yml`).
- **`image` / `promote`:** the signing key never shares a job with a
  Containerfile build.
- **`write` / `promote`:** no App key in the signing job. With downstream
  dispatch gone, nothing needs one there.
- **`gate`:** it must come from the base definition and hold no secret, so it
  can be required.
- **`pypi`:** a job has one environment.

**Ordering and races.**
- `promote` and `write` use `queue: max`, so a second run waits and nothing is
  dropped.
- Promote is idempotent: an unchanged digest signs nothing new and moves no
  tag, and a tag never moves backwards.
- Before `write` pushes to a PR branch, it checks that the branch head is
  still the event's `pull_request.head.sha`. It then pushes with
  `--force-with-lease=<branch>:<sha>`.

**The model's container holds the Claude token.** It has to, because the
model calls the API with it. This predates the design: `claude-code-action`
puts it in the same process's environment today, which is why Ashigaru denies
`Read(//proc/**)`. That container never holds a GitHub write token, an App key
or the socket.

**What `write` will not push.** The App applies `ai`'s patch only through a
path filter. A patch touching any of these fails `write`, and the change goes
to a person:
- **Every repo:**
  - `.github/workflows/`, which the App cannot write anyway;
  - `.sdlc/`;
  - `gatehouse-findings.toml`;
  - `gourmand-exceptions.toml`;
  - `sdlc.toml`, and the `[tool.sdlc]` tables in `pyproject.toml`.
- **crunchtools/sdlc's trust-root files:**
  - `bin/sdlc-bootstrap`;
  - the `sdlc-bootstrap` Containerfile;
  - `scripts/self.sh`;
  - the public keys;
  - `sdlc`, the in-image entrypoint;
  - `bin/sdlc`, the launcher;
  - `Containerfile`;
  - `.github/workflows/ci.yml`, sdlc's own.

Those files decide what the gates accept or what the fleet trusts, so a model
must never edit its own judge. Exedore 4 is reworking the filter in
ashigaru#254, which should take this list.

**What crosses between jobs.** `ai` gzips and base64-encodes its result, then
splits it into four outputs of at most 112,500 bytes each, 450,000 bytes in
all. That stays under Linux's 128 KiB `MAX_ARG_STRLEN` per environment
variable. `write` reads the four as `SDLC_AI_0..3`. Spike #103 confirmed the
limit: a step given the whole 450 KB as one variable fails to exec, so the
split stays at four chunks.
- **A result over 450,000 bytes encoded** fails `ai` loudly; it is never
  truncated. The admin override is a person: `ai`'s failure labels the issue
  or PR `needs-human`, with the size, and an admin applies the change by hand.
  Nothing raises the cap per run.
- **An empty or missing result**, when the plan needed one, fails `write`.

## What `sdlc event` runs

This is sdlc's routing table, not the file's. It is listed by job, then event.
Tool and category names are kept, so the Security tab's history carries over:
CodeQL uploads as `/language:python`, and zizmor as `zizmor`.

| Job | Event | Runs | Was |
|---|---|---|---|
| check | pull_request, push | `ci` with CodeQL (SARIF uploaded on the default branch), `gourmand`, `constitution` with zizmor (SARIF uploaded); an image repo also builds and gates the PR's Containerfile in a nested container, nothing pushed | ci.yml, constitution.yml, security.yml CodeQL, container-release on a PR |
| check | push | plans `image`, and `release` on the merge of an App-authored `ashigaru/release-X.Y.Z` PR | container.yml `resolve` |
| check | schedule `17 6 * * 1` | `audit`, plans the weekly rebuild | dependency-audit.yml, weekly rebuild |
| check | schedule `47 */6 * * *` | plans a rebuild only when the parent image's digest moved | repository_dispatch `parent-image-updated` |
| ai | pull_request_target | Gatehouse review of the head SHA's diff, read through the API; on an `ashigaru/` branch in this repo also Ashigaru `fix`, five rounds at most | review.yml, fix.yml agent |
| ai | issues | Ashigaru `triage`, then `code` when the verdict is ready-to-code | triage.yml, code.yml agent |
| image | push, schedule | builds through the socket, runs the container gates, pushes `gated-<sha>` by digest | container-release build, gates, mark |
| write | release merge push | verifies the release from the API, tags `vX.Y.Z`, publishes the GitHub Release, outputs `release` | release-tag.yml |
| write | pull_request_target | posts the review; pushes the fix commit (leased) and its answers | review.yml, fix.yml publish |
| write | issues | labels; opens the `ashigaru/issue-N` PR | triage.yml apply, code.yml publish |
| write | workflow_dispatch with `version`, no `digest` | opens the release PR; a dispatch with `digest` is sdlc's rollback, and `write` does not run | release.yml open |
| write | schedule, when `check` plans it | keeps the audit alert issue; only when the weekly audit's plan sets `write`, so the 6-hourly polls never mint the App key | dependency-audit.yml |
| promote | after image | signs, attests, moves `latest`, and `X.Y.Z` from `write`'s output | container-release promote |
| pypi | release merge push | re-verifies, `uv build` nested, `uv publish` by OIDC | publish.yml |
| gate | pull_request_target | fails unless `ai` and `write` succeeded; workflow guard; triage, which needs a review of the head SHA | gatehouse.yml guard, triage.yml |
| gate | collaborator review comment | re-runs `gate` in the PR's `pull_request_target` run with `GITHUB_TOKEN` | gatehouse-retriage.yml |
| renovate | schedule `17 6 * * 1`, constitution only | `sdlc_jobs/renovate.py`: runs Renovate one repo at a time, each in a nested container of this image with a token minted for that repo alone (no socket, no key), with the constitution's `renovate/self-hosted.json` at this commit as `RENOVATE_CONFIG` | the constitution's renovate.yml |
| renovate | workflow_dispatch without `version`, constitution only | the same as a dry run: a read-only token, the weekly window ignored; a dispatch with `version` is refused | renovate.yml's dispatch |

## Required checks: from five to two

| Today (org rulesets) | Proposed |
|---|---|
| `sdlc ci`, `Code Quality (Gourmand) / Code Quality (Gourmand)`, `Constitution / Validate constitution` | `sdlc / check (pull_request)` |
| `Protect workflows`, `Gatehouse triage / Gatehouse triage` | `sdlc / gate (pull_request_target)` |

Both are required with **GitHub Actions** pinned as the source app.

**`check` is assured only because `gate` refuses workflow edits.** A
`pull_request` run executes the PR's copy of `ci.yml`. A fork's `check` would
be worthless alone. It counts because `gate` runs from the base definition and
fails any fork PR that touches `.github/workflows/` or `.sdlc/`. The two checks
are one control.

**No skipped job can carry a required name.** A skipped job still reports a
check, and a skipped check counts as passing. With the event in `name:`, the
skipped `gate` in a `pull_request` run is `sdlc / gate (pull_request)`, and
nothing requires that. Spike #103 (a1058) settled how: a skipped job's check
carries its `name:` literally, `sdlc / gate (${{ github.event_name }})`
unevaluated, and that differs from every required name. Within its own event,
neither required job can no-op.

**The gate stays on `pull_request_target`**, as Josui Prime agreed: a
`pull_request` run would let a fork edit the guard out of its own run. A fork's
`pull_request` run could add a job literally named
`sdlc / gate (pull_request_target)`, as it could name one `Protect workflows`
today. GitHub's fork-run approval setting and the gha-runner hook below stop
it.

**The 50 re-run cap, and who can spend it.** GitHub allows at most 50 re-runs
of one workflow run. The doorbell fires only for a review comment whose
`author_association` is `OWNER`, `MEMBER` or `COLLABORATOR`. Bots, the App and
outsiders never re-run `gate`, so an outsider cannot burn the cap with
comments. Past 50 collaborator replies on one head, re-triage waits for the
next push, which starts a new run.

## Cutover prerequisites

Each must hold before the first canary converts, unless it says otherwise.

1. **The org event policy for `pull_request_target` (a1071, Josui Prime).**
   GitHub blocks `pull_request_target` in public repos from 2026-12-02 unless
   the organization's Actions event policy allows it. It must allow it for
   `.github/workflows/ci.yml` in every repo. Without that, `ai`, `write` and
   the required `gate` never run.
2. **No environments, no per-repo secrets.** Every fleet secret stays an org
   secret: CLAUDE_CODE_OAUTH_TOKEN, OPENROUTER_API_KEY, ASHIGARU_APP_KEY,
   QUAY_*, RHSM_*, REGISTRY_REDHAT_IO_* and COSIGN_*; ASHIGARU_CLIENT_ID stays
   a variable. The control is the workflows permission, held by Prime's
   credential alone. In sdlc alone, `registry` and `sdlc-signing` hold **sdlc's
   own quay robot** (SDLC_QUAY_*), with write on `quay.io/crunchtools/sdlc`
   and `sdlc-bootstrap` only; the fleet robot has none there.
3. *(Withdrawn: org secrets stay visible to the converted repos.)*
4. **sdlc's trust root, converted first.** In this order:
   - sdlc's own `ci.yml` and `scripts/self.sh`;
   - `sdlc-signing` holding the sdlc key;
   - `sdlc-approval`, with its reviewers and `prevent_self_review`;
   - the first release signed with the sdlc key, and `:<major>` on it;
   - `sdlc-bootstrap`, built with the key, the major and the minimum version, with
     its digest recorded;
   - sdlc's own ruleset entry.
5. **gha-runner rejects fork jobs** (separate gha-runner work). A job-started
   hook (`ACTIONS_RUNNER_HOOK_JOB_STARTED`) exits non-zero when the payload's
   `pull_request.head.repo.full_name` differs from `repository.full_name`.
   Future work: gha-runner has no such hook today. The secret jobs' `runs-on`
   already sends a fork's `pull_request_target` to `ubuntu-latest`, and the
   org's fork-approval setting holds fork runs, so the hook will be the
   backstop, not the route.
6. **Ashigaru: the wider patch filter** (ashigaru#254, which applies today),
   with the files listed under "What `write` will not push".
7. **Gatehouse: triage ignores App-authored answers** (a separate gatehouse
   item, which applies today). An answer counts only when a person wrote it.
8. **PyPI trusted publishers** (below).
9. **The org's plan, or private repos held out**, as above. `cutover.py` and
   `fleet_wave.py` refuse any repo whose API record says `private: true`,
   unconditionally, unless `--include-private` is passed. A test pins that:
   a private repo in the listing, without the flag, is refused and named.
10. **Spike #103** passes on both runner kinds.
11. **"Require approval for all external contributors"** (Josui Prime, an
    org setting, at cutover). Fork pull requests then wait for a
    maintainer's approval before any workflow run, `test`'s and `check`'s
    included. Defence in depth: neither holds anything a fork could use.
12. **A branch ruleset on `ashigaru/release-*`** (Josui Prime, at cutover)
    restricting creation and updates to the Ashigaru App. Today
    `merged_release` in ashigaru's `scripts/release_publish.py` (lines
    23–34) checks the PR's author, its merge, the `ashigaru/release-`
    prefix, the head repo and the base. It does **not** check who created
    or pushed the branch, so a collaborator could push a commit to a
    release branch the App opened, and an App-authored PR would carry it.
    `write`'s seven tests inherit that gap. The ruleset closes it, and
    `cutover.py --check` asserts it exists.

## PyPI: re-registering 22 trusted publishers

This is a human step, for Scott. Each publisher must change from
`publish.yml` with no environment to `ci.yml` with environment `pypi`.

1. **Before the wave.** Nothing to create: GitHub creates `pypi` the first
   time a job names it. `cutover.py` lists the projects with their PyPI URLs.
2. **Before each repo's merge**, at
   pypi.org/manage/project/<name>/settings/publishing:
   - add the new publisher (owner `crunchtools`, the repo, workflow `ci.yml`,
     environment `pypi`);
   - **remove the old `publish.yml` publisher.**

   Between those steps and the merge, no workflow can publish. The old file is
   still there but no longer trusted, and the new one is not merged yet. A
   release is not cut in that window.
3. **The check.** PyPI does not publish a project's trusted publishers, and
   has no API for them, so the check is a ledger. `cutover.py` refuses to
   merge a PyPI repo until its row in the wave ledger records both steps as
   done, with who did them.

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
- `issues` and `schedule` run the default branch's
  copy, and so does `push` to the default branch.
- `release` events and tag pushes would run the tagged commit's copy
  (`GITHUB_REF` is `refs/tags/<tag>`). That is why this file subscribes to
  no `release` event and runs no job on a tag push: release work runs from
  the push that merges the release PR (see "Releases run from the merge").
- `pull_request` and `pull_request_review_comment` run the merge commit's copy.
  So a fork can edit the jobs in its own run, as it can today. `gate` refuses
  the change, `check` holds nothing, and `gate`'s review-comment path holds no
  secret.
- `workflow_dispatch` runs the copy on whatever ref it is dispatched against.
- **An `if:` cannot keep a secret on the right ref.** A writer can dispatch the
  workflow against their own branch, or push a branch whose copy of `ci.yml`
  adds triggers, and either way the `if:` that runs is the one they wrote. So
  the control is who may change a workflow at all: the workflows permission,
  held by Prime's credential alone (see "Every secret is an org secret").

Nothing changes here. Moving into one file does not move the boundary.

**7. Ashigaru `fix` no longer needs `workflow_run`.** Today it listens for
Gatehouse's completion. In one file it runs inside `ai`, right after the
review, in the same `pull_request_target` run. It runs on `ashigaru/` branches
in this repo only, and answers that review's findings and the open ones left
from earlier rounds. That is one trigger fewer, and the bound of five rounds
stays in sdlc.

**8. No variable chooses code.** A repo variable can be written by anyone
with write access to the repo, and it overrides the org's. So the image is a
literal digest in the file, never `vars.*`. `check`, which holds nothing,
takes its runner label from a variable (`UNCACHED_RUNNER_LABEL`). The secret
jobs read the same variable only as a switch: they run on the literal
`crunchtools-uncached` when it equals that, and on `ubuntu-latest` otherwise,
so it can turn Exedore off for them and never point them anywhere else.

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
| `docker/login-action`, `setup-buildx-action`, `build-push-action` | trentina `l2-train`, rotv `tests` | `docker login` / `docker build` / `docker push` through the socket in `sdlc image`, or a repo-defined command | no: the docker CLI, skopeo and cosign have to come in |
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

- **The docker CLI and skopeo**: for driving the runner's socket (builds,
  nested runs, `docker login`) and for copying by digest. There is no engine
  inside the image: the socket is the engine. Not the podman remote client:
  spike #103 found `podman --remote` fails against the Docker 29 socket on
  GitHub-hosted runners, while the docker CLI drives both that and
  gha-runner's podman API.
- **cosign v2.6.5** (copied from its digest-pinned image), and **syft**: for
  `sdlc image promote`. They do what `scripts/sign-digest.sh` and
  `scripts/attest-digest.sh` already do in sdlc's own `container.yml`.
- **zizmor**: today it is installed by hash in the constitution's
  `requirements/zizmor.txt`.
- **claude**: the CLI from npm, at an exact version, under its own prefix as
  markdownlint-cli2 is.
- **cryptography** (or PyJWT): in an isolated venv, for App tokens.
- **The bootstrap** is a second, separate image (`sdlc-bootstrap`), built from
  this repo. It holds cosign, sdlc's own public key (two during a rotation),
  the major it follows, the minimum version and the docker CLI, and
  nothing else. It changes rarely, and each
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
- **RPM build → publish** (ashigaru `rpm.yml`, claude-agent and petit
  `packages.yml`; a few MB per distro, attached as release assets). **Out of
  scope for this design.** These keep their current workflows and their
  artifact hand-off for now. Moving them needs a way to get files from a
  no-token build job to a release, and nothing here provides one. They are
  the one remaining source of `uses:` in those three repos.
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
That is the cost today too. No layer cache carries between jobs: PR checks
run on the uncached runners, whose storage is emptied every job.

**Build attestations.** No repo uses `actions/attest*`. Images are attested with
cosign and the fleet key (`attest-digest.sh`: SPDX SBOM and SLSA v1
provenance), and that moves into `sdlc image promote` as it is. GitHub's
attestation store is not used, and nothing is lost.

**GitHub Pages.** Only `crunchtools/packages` deploys Pages, and its
`publish.yml` is out of scope with the other package release workflows. What
follows is the path for when it moves.
`actions/deploy-pages` needs an artifact made by `upload-pages-artifact`, and
neither can run from a `run:` step. The repo moves its Pages source to
"Deploy from a branch", and `sdlc pages` pushes the built tree to `gh-pages`
from `write`, with the App token, on a push to the default branch only. That is a one-time setting change on that repo.

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
secret, and never the socket itself. One exception to `--network=none`: on
an Exedore runner a Containerfile's `RUN` steps cannot have it. The runner's
rootless podman builds with chroot isolation, which cannot take
`--network=none` (spike #103), so a `RUN` step there has the runner's
network. It still gets no socket, token or secret. The model runs nested too, holding the
Claude token it needs to call the API and nothing else: not the socket, not a
GitHub write token, not an App key. That is today's `run_in_image`, moved
from the host into a job container.

Two things are new and go in spike #103:
1. **Path mapping.** A container started through the socket mounts paths on
   the host, not on the job container. sdlc finds the host path of
   `$GITHUB_WORKSPACE` by inspecting its own container through the socket, and
   it mounts only that.
2. **Holding the socket is holding the runner.** On GitHub-hosted runners the
   Docker socket is root on the VM; on an uncached Exedore runner it is that
   runner's rootless podman, in a container and a store thrown away after the
   job. A job with a secret also has that socket.
   It runs only the verified sdlc image, but that image does more than
   orchestrate: `image` builds the repo's Containerfile through the socket,
   beside the registry secrets. A `RUN` step cannot reach the socket or the
   secrets (the build gets neither), but the Containerfile is still repo
   code executing on the same VM. That is safe only because `image` runs on
   a default-branch push or the schedule, so the Containerfile it builds is
   reviewed, merged code. A PR's Containerfile is built only by `check`,
   which holds no secret. The socket itself is never passed down to repo
   code.

**No image, or several.** A repo whose root Containerfile is not a published
image says `[tool.sdlc.image] publish = false`: no image job, no candidate, and
the container gates stay in `check`. A repo with more images lists each one:

```toml
[[tool.sdlc.images]]
name = "rotv-base"                 # quay.io/crunchtools/rotv-base
containerfile = "Containerfile.base"
# context = ".", and any [tool.sdlc.image] setting, for this image alone
```

The same `image` job builds them in dependency order, read from their FROM
lines (build-args included), and an image FROM a sibling builds on the digest
this run just pushed. Each is gated (build, scan, its own boot gate) and
pushed only when it passed; `promote` re-validates and signs each. Which names
a repo may publish is `EXTRA_IMAGES` in `sdlc_jobs/_core.py`, reviewed in sdlc:
a prefix rule would let `foo` claim the image of a repo `foo-bar`, and the
registry credentials are the org's.

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

- **Writing the file.** It writes `examples/ci.yml`, the 90-line fleet file
  above, unchanged. There is nothing to template: the repo's kind, the
  default branch and Ashigaru enrolment (`vars.ASHIGARU_ENABLED`) are all read
  at run time by `sdlc event`.
- **Moving what used to be in the workflows.** What the old files said per
  repo moves into `[tool.sdlc]`: a `publish = "pypi"` where a PyPI publish
  existed. An image's downstream dispatch list is dropped, since children poll.
  These are ordinary config, so they are not fleet policy.
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
- **Tests that pin the file's guards.** `tests/test_workflows.py` parses
  `examples/ci.yml` and sdlc's own `ci.yml`, and asserts:
  - `write`'s `if:` contains `!inputs.digest`, so a rollback dispatch never
    runs `write`; and `sdlc event`, given `DIGEST` in `write`'s env, exits 0
    having done nothing (a unit test on the dispatcher, the second guard);
  - `write`'s `if:` outside the triage events, `image`'s and `pypi`'s all
    contain `needs.check.result == 'success'`;
  - `image`'s env maps `REGISTRY_REDHAT_IO_USER` and
    `REGISTRY_REDHAT_IO_PASSWORD`;
  - sdlc's `test` is the only job whose `if:` admits `pull_request` and it
    has `permissions: { contents: read }`, no `environment` and no
    `container`, and sdlc's `check` does not admit `pull_request`;
  - `ai`, `write` and `gate` are byte for byte the same in both files.
- **New work.** It deletes `.gemini/` where the org no longer has the app.
  It drops the repo from each moved org secret's selected-repository list in
  its merge run. It refuses a private repo unless `--include-private` is passed.
- **How the conversion PR merges.** It merges by Josui Prime's admin bypass of
  the old ruleset. The PR deletes the very workflows that produce the old
  required checks, so on that PR they never report, and nothing else could
  merge it. The new `ci.yml` is already running on it, and its two checks are
  read before the bypass is used. Immediately after, in the same run, the
  idempotent ruleset move puts the repo on the new ruleset.
- **The ruleset move is idempotent and can be re-run.** It does not move a
  repo from one list to another. It computes both rulesets' target lists from
  the state of the fleet: a repo is converted when its default branch carries
  the new `ci.yml` byte for byte. It then `PUT`s both lists whole. Running it
  twice, or after a crash between the merge and the move, gives the same
  rulesets. A repo the old list drops is in the new one, or the run refuses
  before writing anything, because a required check that never reports
  blocks every merge silently.
- **It refuses while the repo is busy.**
  - Any open pull request not authored by a bot blocks the conversion: a
    person's open work is never rebased under them.
  - Any open old-style release PR (an `ashigaru/release-*` opened by the old
    release flow) blocks it, because the two flows must never both tag.
  - Ashigaru's and Renovate's open PRs do not block. After the merge,
    `cutover.py` brings each one up to the new default branch
    (`PUT /pulls/{n}/update-branch`), so their next run uses the new file.
- **PyPI.** It refuses a PyPI repo until the wave ledger records the new
  publisher added and the old one removed, both before the merge (see
  "PyPI").
- **`pins_left` becomes "any `uses:` at all".** The new constitution release
  makes the same check in the validator: `check_third_party_pins` widens to
  refuse every `uses:` for a repo inheriting that release. That is a
  constitution change, filed separately: XV rules 2 and 5 are rewritten to
  "no `uses:`; versions live in the sdlc image".

**Order.** The order is the same one every move in this repo follows.
1. sdlc grows `sdlc event` and the tools it needs, and releases. No
   reusable workflow is released or changed first: the unconverted repos keep
   calling them, unchanged, until the wave is done.
2. The cutover prerequisites above are met, in their order: the org event
   policy; sdlc's trust root; the gha-runner
   hook; the Ashigaru and Gatehouse items; the plan decision for private
   repos; the spike. The PyPI publishers change per repo, just before each
   merge.
3. Three canary repos convert: mcp-wordpress, ubi10-core and acquacotta.
4. The fleet wave runs, repo by repo, each one moving to the new ruleset in
   its merge run.
5. When the whole wave has converted, every selected-repository list is
   empty and `cutover.py --check` is clean. Then the org copies are deleted.
6. The reusables are deprecated a release later.
