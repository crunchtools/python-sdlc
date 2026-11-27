# a1796 proof bundle (Dolza)

Proves the new `assets` and `pages` jobs on real builds, real signing and real distro installs. Only GitHub is faked (API, upload, download, the Pages build request).

## Run

Needs podman, git, python3, bash, curl and Docker Hub, quay, registry.suse.com, registry.access.redhat.com, github.com.

    git clone https://github.com/crunchtools/sdlc && cd sdlc
    git am /path/to/a1796.patch          # git format-patch output, on branch feat/a1796-release-assets
    /path/to/dolza/run.sh "$PWD" [workdir]

Expected final line: `DOLZA PROOF PASSED`. On failure the work dir (default `/tmp/a1796.XXXXXX`) is kept; logs are in `logs/`, `assets.log`, `pages.log`, `contract.log`.

## What each step proves

1. `podman build` of the sdlc image, then `tests/contract.sh` (run from the checkout, it calls `tests/test_apparmor.sh` by relative path): gnupg2, rpm-sign, createrepo_c, rpmkeys install and execute on the Hummingbird base.
2. A podman API socket, the job's `CONTAINER_HOST`.
3. Clones ashigaru and petit `main`, appends the Adoption list's `[[tool.sdlc.assets]]`, commits locally.
4a. `drive.py assets`, inside the image: `assets.run` per repo with a faked release and merged release PR. A real `podman build --target`, create and cp through the socket. Checks: ashigaru yields noarch and source RPMs for fc43, fc45, el10; petit yields .rpm and .deb; every name carries the version; SHA256SUMS matches. Uploaded bytes land in `pool/<repo>/`.
4b. `drive.py pages`, inside the image with `--network=none` and no socket: throwaway GPG key, `pages.run("workflow_dispatch")` pushing to a local bare repo, `git clone -b gh-pages` to `site/`. Checks: every RPM on the site passes `rpmkeys --checksig` against a temp rpmdb holding only the site key (the unsigned pool copy is the negative control); `gpgv` on InRelease, Release.gpg, repomd.xml.asc (a tampered Release is the negative control); no SRPM on the site; generated `.repo`/`.sources` are as the install step assumes.
5. The site is served by `python3 -m http.server` (container `a1796-site`, alias `site`) on network `a1796net`. publish.yml's 13 targets install petit with signature checking on (gpgcheck=1, repo_gpgcheck=1, Signed-By), the installed RPM must show a signature, and `petit --version` and `--hash` run. Then ashigaru's fc43 noarch RPM is `dnf install`ed on fedora:43.

## Assumptions and TODOs

- **KEY_ALGO defaults to rsa4096, not ed25519.** The production crunchtools.asc is RSA-4096, and UBI 8's rpm 4.14 cannot verify ed25519 signatures. Try `KEY_ALGO=ed25519 ./run.sh ...` to see which targets break; that is a question for the real key, not a defect of the job.
- **Site URL.** `pages.settings` requires https, so the site is built for `https://packages.invalid`. The install step does not rewrite the generated files; it writes its own `.repo` and `.sources` for `http://site:8080`, after drive.py asserts the generated ones are the same text apart from the host.
- **zypper** uses `--gpg-auto-import-keys` as publish.yml does: it imports the repo's gpgkey and still verifies with it. There is no `--no-gpg-checks`.
- **ashigaru's version is dynamic (VERSION file)** and `verify_release.tree_version` demands a static `[project].version`. On unmodified main `assets.run` therefore refuses (test 7). drive.py detects this, prints a `FINDING`, and in the throwaway clone only replaces `dynamic = ["version"]` with `version = "<VERSION>"` so the rest can be proven. assets.py is not touched. Adopting ashigaru needs that change in the repo (or verify_release reading VERSION); the run's final summary repeats the finding. Version 6.1.0 also matches CHANGELOG, so test 7's second half holds.
- Ashigaru's stage is `build` and its dir is `/out` (as in the design). It contains `ashigaru-<v>.tar.gz` (not matched by `*.rpm`), the noarch RPMs (`ashigaru`, `ashigaru-desk`) and SRPMs. Expected dist tags: `.fc43`, `.fc45`, `.el10`; if CentOS Stream 10 tags differently the assertion names the files found.
- Petit: the stage is `packages` (FROM scratch), holding the RPM and DEB at `/`, so `path = "/"`. `pkg-ubi8` builds `TARGET=ubi8 PKG_KIND=rpm`, but the `packages` stage builds both `rpm` and `deb`. The design says "pin the workflow-only roots": `assets.pinned` checks every non-scratch FROM that the build uses, with the check's build-args; if it refuses petit's Containerfile, that is a real finding to carry back, not a proof bug.
- ashigaru is not in packages' products today; the proof's site lists it. Its RPM install on fedora:43 may need dependencies; if `dnf install` fails the script falls back to `rpmkeys --checksig` and reports `PASS*` with the log path. TODO: decide whether a `PASS*` is acceptable; it is not silent.
- The ashigaru install is asked for by name-version-release because fc43, fc45 and el10 builds share one repo.
- Not covered: claude-agent (not built here), `pages.deploy` and the real Pages build, the `pages-signing` environment, assets re-run/replace behaviour (proven by the local harness, not repeated here, to save three builds).
- Untested here (no mirror access on the authoring seat): everything that runs. Only `bash -n` and `py_compile` passed. Expect first-run fixes; each step prints a header so a failure is easy to place.
- drive.py runs with the image's `/opt/constitution-venv/bin/python3`; `sdlc_jobs` is importable there from `/usr/local/lib/sdlc` (the image's .pth, asserted by contract.sh).
