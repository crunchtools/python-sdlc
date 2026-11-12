# python-sdlc Constitution

> **Version:** 0.1.0
> **Ratified:** 2026-11-04
> **Amended:** 2026-11-04
> **Status:** Active
> **Inherits:** [crunchtools/constitution](https://github.com/crunchtools/constitution) v1.41.0
> **Profile:** Container Image

This is a manifest. It carries only what is unique to this repo; the fleet rules
and the Container Image profile are inherited, not restated.

## What this image promises

One image and one script holding every Python SDLC check for the fleet, so that
a consuming repo pins **one** `rev:` and gets a fixed set of tool versions.

Three properties follow from that, and all three are load-bearing:

1. **Every tool is pinned to an exact version.** Not `:latest`, not a major tag.
   The failure this prevents was observed in the repo this image replaces:
   `gates.sh` pinned `gatehouse:latest`, podman's default pull policy is
   `missing`, so a cached tag was never refreshed and the local gate ran a
   two-week-old reviewer while CI pulled fresh. A floating pin in this image is
   a floating pin in 35 repos.

2. **`sdlc versions` reports what is installed beside what was pinned.** An
   image tag and the binary inside it can disagree — the Gourmand image tagged
   `1.0.0` ships a binary reporting `0.16.5`. A consuming repo pins the tag, so
   the gap has to be visible or the tool is not really pinned.

3. **The image carries tools, never project policy.** Rulesets stay in the
   repo being checked. Baking one here means one repo's rule change breaks the
   other thirty-four.

## The exit code contract

```
0   the check ran and found nothing
1   the check ran and found something
2   the check COULD NOT RUN
```

**2 is never to be worked around**, and it is the reason this repo exists rather
than a pile of `podman run` lines in each consumer. Specifically:

- Anything that is not the tool's own verdict is a 2. Podman's 125, 126 and 127
  mean the container never started, so they are a 2 and not a 1. The script this
  replaces mapped them to 1 through `|| fail`, reporting "found something" for
  "could not invoke".
- A missing or unwritable cache, an out-of-date lockfile, an absent config path
  and an unreachable origin are each a 2. None of them may report 0.
- `mypy` and `pytest` resolve from the consumer's `uv.lock`. When either is
  absent from it, the answer is 2 — never a pass for a check that did not run.
- A later 1 must not mask an earlier 2 when a tier runs several targets.

## The two seams, and why they are seams

**mypy and pytest are pinned by the consumer, not by this image.** They load the
project's own plugins inside their own interpreter, so they must come from the
project environment. This is a deliberate hole in the one-pin promise and is
documented as such in `README.md` rather than papered over.

**The constitution validator is cloned at the consumer's tag, never baked.** The
fleet spans `Inherits:` v1.0.0 to v1.29.0; a baked copy would judge most repos by
rules they have not adopted. Two details that are not optional: the clone must
bring `constitution.md` and `profiles/requirements.toml`, because the validator
resolves them from its own directory and silently skips most of its checks when
they are absent; and `--pinned` is correct only because the clone is made at the
tag the repo itself names.

## Running as root is the correct choice here

The container runs as its own root and mounts the repo at `/repo`, which is
Gourmand's and Gatehouse's existing convention across this fleet.

Under rootless podman the container's root maps to the invoking user, so a file
`ruff --fix` writes into the worktree is owned by the developer and the shared
cache volume is writable, with no `--user` and no `--userns=keep-id`. Running as
an image-internal UID instead maps to a subuid the developer does not own, which
breaks the cache and leaves un-ownable files in the repo.

One deliberate difference from those two images: the mount is `z`, not `Z`. A
tier runs several containers over one tree, and `Z` relabels it exclusively,
taking it out from under whichever started first.

## Contract test

The Container Image profile requires a contract test against the pushed digest.
For an image whose entire purpose is "these executables exist at these
versions", that test is the central one, not a formality. It asserts three
things:

- every tool EXECUTES, not merely that it is present -- a `COPY --from` can land
  a binary that exists and dies on exec, which is how a renamed Gatehouse venv
  was caught;
- every version `sdlc versions` reports is the version pinned at build time;
- every target `sdlc list` advertises is dispatchable and reports a verdict or a
  reason, and the offline targets a fixture can exercise actually RUN against
  one -- clean code passing and broken code failing.

What it does not claim: that every target runs. `mypy`, `pytest` and
`pip-audit` need a network, `gatehouse` an API key, `constitution` a clone,
`codeql` minutes, and `gourmand` audits repository hygiene rather than a
fixture's code. Those are verified against real repositories, and the exclusions
are named in the test rather than left as a gap in a claim.
