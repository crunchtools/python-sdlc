"""The `ai` job: Gatehouse's review and Ashigaru's agents, with model keys and a read token.

It holds CLAUDE_CODE_OAUTH_TOKEN, OPENROUTER_API_KEY and a GITHUB_TOKEN that
can only read. It never holds an App key and never writes to GitHub: every
review, label, comment, pull request and push it decides on is handed to
`write` as one result, in the four outputs p0..p3 (see _handoff.py).

  pull_request_target  Gatehouse reviews the head, as gatehouse's review.yml
                       did, with what it would post recorded instead of
                       posted. On an Ashigaru branch of this repository,
                       Ashigaru's fix round follows in the same job, over the
                       findings left open and the ones this review just made.
  issues               Ashigaru triages a new issue (or one labelled
                       ready-for-triage) and, when triage says it starts now
                       or a maintainer labels it ready-to-code, writes the
                       change as a patch.

Ashigaru runs where CLAUDE_CODE_OAUTH_TOKEN reaches this job. The provisioner
copies it into a repository's `ai` environment only where Ashigaru is
enrolled, so that copy is the switch the ASHIGARU_ENABLED variable was: the
frozen ci.yml hands this job no variable to read.
"""

from __future__ import annotations

import importlib.util
import json
import secrets
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from http import HTTPStatus
from pathlib import Path
from types import ModuleType
from typing import Any

from sdlc_jobs import _core as core
from sdlc_jobs import _findings, constitution
from sdlc_jobs import _tools as tools
from sdlc_jobs._github import (
    APP_LOGIN,
    APP_SLUG,
    CANNOT_RUN,
    DIFF,
    FOUND,
    OK,
    Api,
    ApiError,
    CannotRunError,
    GitHub,
    contract,
    default_branch,
    repo_of,
    set_output,
)
from sdlc_jobs._handoff import NAMES, TooLargeError, encode

RAW = "application/vnd.github.raw"
REQUIRED_LOW_AGENTS = ",".join(_findings.REQUIRED_LOW_AGENTS)
COMPLETE = "<!-- gatehouse review complete -->"
FAILED = "<!-- gatehouse review failed -->"
RELEASE_BRANCH = "ashigaru/release-"
AGENT_BRANCH = "ashigaru/"
# Every agent Ashigaru runs gets the same deny rule its workflows gave it: the
# model may not read this process's environment, which holds its own token.
# Every tool that reads files is denied both trees: Grep over /proc/*/environ
# would read this job's own environment (the model keys, the read token and
# the GITHUB_OUTPUT path) as surely as Read would.
DENIED = [f"{tool}(//{tree}/**)" for tree in ("proc", "sys") for tool in ("Read", "Grep", "Glob")]
SETTINGS = json.dumps({"permissions": {"deny": DENIED}}, separators=(",", ":"))
READ_ONLY_TOOLS = "Read,Glob,Grep"
EDIT_TOOLS = "Read,Edit,Write,Glob,Grep"
# The model runs in a sibling container, not as this job's child: this job is
# root and holds the socket, and a bare Write in --allowedTools reaches any path,
# so a model steered by a diff or an issue comment could rewrite a binary this
# job runs next. The sibling has no socket, none of this job's environment but
# the model's own token, a read-only root, the scratch dir read-only, and the
# checkout writable but for its .git, which `collect` runs git in.
MODEL_TMP = "/tmp:rw,exec,size=1g,mode=1777"  # noqa: S108 -- inside the sibling


@dataclass(frozen=True)
class Agent:
    """One of Ashigaru's agents as its workflow ran it: schema, model, turns, tools."""

    schema: str
    model: str
    turns: int
    tools: str


TRIAGE = Agent("triage", "sonnet", 40, READ_ONLY_TOOLS)
CODE = Agent("code", "sonnet", 80, EDIT_TOOLS)
FIX = Agent("fix", "sonnet", 80, EDIT_TOOLS)  # the round's model replaces sonnet
MAX_ROUNDS, ESCALATE_AT = 5, 3
SHA_LEN = 40
# The shim's capture file missing or malformed. A named tuple, not PEP 758's
# bare list, which Gourmand's parser cannot read.
UNREADABLE_CAPTURE = (OSError, ValueError, KeyError, TypeError)
MODEL_SECONDS = 3600  # one agent or one review, as the workflows' job timeouts allowed
# The client's own limit: past MODEL_SECONDS podman stops the sibling itself.
# Killing only the client would leave it running with the token, so the
# sibling is removed by name whatever the run's outcome.
MODEL_GRACE = 60
AUTO_CATEGORIES = {"bug", "documentation", "performance"}
MIN_CONFIDENCE = 0.7


@contract
def main(event_name: str, event: dict[str, Any], env: Mapping[str, str]) -> int:
    """`sdlc event`'s entry for the `ai` job.

    Reviews or runs Ashigaru for `event`, writes the result to p0..p3, and
    returns 0, or 2 when the review or an agent could not run (the result is
    still handed on), or 1 when the result was over the cap.
    """
    if event_name not in ("pull_request_target", "issues"):
        print(f"ai: nothing runs on {event_name}")
        return OK
    repo = repo_of(event, env)
    api = GitHub(env.get("GITHUB_TOKEN", ""))
    # Under RUNNER_TEMP, a mount of this container, so the model's sibling can mount it.
    with tempfile.TemporaryDirectory(
        prefix="sdlc-ai-", dir=env.get("RUNNER_TEMP") or None
    ) as scratch:
        site = tools.Site(repo, env, tools.run, Path(scratch))
        work = Path(scratch)
        code, result = run(
            event_name, event, Job(site, api, lambda: core.host_mount(core.Tools(), env, work))
        )
    return hand_on(env, result, code)


def run(event_name: str, event: Mapping[str, Any], job: Job) -> tuple[int, dict[str, Any]]:
    """The result to hand on and this job's exit, for one event. No output is written here."""
    if event_name == "pull_request_target":
        return job.pull_request(event["pull_request"], str(event.get("action", "")))
    return job.issue(event)


def hand_on(env: Mapping[str, str], result: Mapping[str, Any], code: int) -> int:
    """Write p0..p3. A result over the cap fails the job, and `write` is told the size."""
    try:
        parts = encode(result)
    except TooLargeError as err:
        print(f"::error::ai: {err}. Nothing is truncated: a person applies this one.")
        parts = encode(
            {k: result[k] for k in ("v", "kind", "number") if k in result} | {"too_large": err.size}
        )
        code = max(code, FOUND)
    for name, part in zip(NAMES, parts, strict=True):
        set_output(env, name, part)
    return code


class Job:
    """One run of `ai`: the repository, the read-only API, the runner and a scratch dir."""

    def __init__(self, site: tools.Site, api: Api, mount: Callable[[], tuple[str, str]]) -> None:
        """`mount` gives the scratch dir's host path and this job's image, asked once."""
        self.site, self.api, self.mount = site, api, mount
        self.mounted: tuple[str, str] | None = None
        self.env, self.repo, self.runner, self.work = site.env, site.repo, site.runner, site.work
        self.read_token = site.env.get("GITHUB_TOKEN", "")

    # -- pull requests -----------------------------------------------------------------------

    def pull_request(self, pr: Mapping[str, Any], action: str) -> tuple[int, dict[str, Any]]:
        """Review the head, then run Ashigaru's fix round on an Ashigaru branch here."""
        number, head = int(pr["number"]), str(pr["head"]["sha"])
        result: dict[str, Any] = {"v": 1, "kind": "pr", "number": number, "head_sha": head}
        code, result["review"] = self.review(pr, action)
        if self.fixable(pr) and self.env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            # A fix round that cannot run must not lose the review it follows:
            # the review is still handed on and posted, the job still fails.
            try:
                fix_code, result["fix"] = self.fix(pr, result["review"]["posts"])
            except CannotRunError as err:
                print(f"::error::ai: Ashigaru's fix round could not run: {err}")
                fix_code, result["fix"] = CANNOT_RUN, {"action": "failed", "error": str(err)[-500:]}
            code = max(code, fix_code)
        return code, result

    def fixable(self, pr: Mapping[str, Any]) -> bool:
        """Ashigaru's own branch in this repository, and not a release: fix.yml's test."""
        ref = str(pr["head"]["ref"])
        return (
            ref.startswith(AGENT_BRANCH)
            and not ref.startswith(RELEASE_BRANCH)
            and (pr["head"].get("repo") or {}).get("full_name") == self.repo
        )

    def review(self, pr: Mapping[str, Any], action: str) -> tuple[int, dict[str, Any]]:
        """review.yml, step for step: its skips, its failed review, then the CLI."""
        number, head = int(pr["number"]), str(pr["head"]["sha"])
        if self.release_pr(pr):
            print("ai: an Ashigaru release PR; not reviewed.")
            return OK, {"status": "skipped", "reason": "release", "posts": []}
        if not self.env.get("OPENROUTER_API_KEY"):
            return self.failed(head, "no OPENROUTER_API_KEY reached this run.")
        skip = self.same_patch(number, str(pr["base"]["sha"]), head)
        if skip is not None:
            return OK, {"status": "skipped", "reason": "same-patch", "posts": [skip]}
        try:
            diff = self.diff(number)
        except CannotRunError as err:
            return self.failed(head, str(err))
        return self.gatehouse(pr, action, diff)

    def release_pr(self, pr: Mapping[str, Any]) -> bool:
        """Ashigaru's release PR: an ashigaru/release- branch in this repo, opened by the App."""
        return (
            str(pr["head"]["ref"]).startswith(RELEASE_BRANCH)
            and (pr["head"].get("repo") or {}).get("full_name") == self.repo
            and (pr.get("user") or {}).get("login") == APP_LOGIN
        )

    @staticmethod
    def failed(head: str, reason: str) -> tuple[int, dict[str, Any]]:
        """The review that says none ran, so `gate` holds instead of passing on silence."""
        print(f"::error::Gatehouse could not review this PR: {reason}")
        body = f"Gatehouse could not review this PR: {reason}\n\n{FAILED}"
        post = {"event": "COMMENT", "commit_id": head, "body": body}
        return CANNOT_RUN, {"status": "failed", "reason": reason, "posts": [post]}

    def same_patch(self, number: int, base: str, head: str) -> dict[str, Any] | None:
        """review.yml's third skip: a head whose patch a complete review already saw.

        Every lookup that fails falls through to a review, as it did there.
        """
        try:
            reviews = self.api.pages(f"/repos/{self.repo}/pulls/{number}/reviews")
            seen = earlier_heads(reviews)
            if not seen:
                return None
            mine = self.patch_id(base, head)
            for sha, url in seen:
                if mine and self.patch_id(base, sha) == mine:
                    compare = self.api.request("GET", f"/repos/{self.repo}/compare/{head}...{sha}")
                    carried = " ".join(c["sha"] for c in compare.get("commits", []))
                    body = (
                        f"Gatehouse skipped this review: the patch is unchanged since {sha[:7]}, "
                        f"already reviewed ({url}). Its findings and their answers carry forward."
                        f"\n\n{COMPLETE}\n<!-- gatehouse carried {carried} -->"
                    )
                    return {"event": "COMMENT", "commit_id": head, "body": body}
        except (ApiError, KeyError, TypeError) as err:
            return self.no_skip(err)
        return None

    @staticmethod
    def no_skip(err: Exception) -> None:
        """review.yml's rule: a skip that cannot be proven is a review, said once."""
        print(f"::notice::ai: could not check for an unchanged patch ({err}); reviewing.")

    def patch_id(self, base: str, sha: str) -> str:
        """`git patch-id --verbatim` of base...sha, or "" when it cannot be had."""
        diff = self.api.request("GET", f"/repos/{self.repo}/compare/{base}...{sha}", accept=DIFF)
        done = self.runner(
            ["git", "patch-id", "--verbatim"], env=tools.child_env(self.env), stdin=str(diff)
        )
        return done.stdout.split(" ", 1)[0].strip() if done.returncode == 0 else ""

    def diff(self, number: int) -> str:
        """The PR's net diff, or rebuilt from its file list past the diff endpoint's 300 files.

        Never the patch media type: that is one patch per commit, so a file one
        commit adds and a later one removes, and every intermediate line, look
        like part of the PR, and GitHub refuses the whole review when a comment
        is anchored to one ("Line could not be resolved", gatehouse#173).
        """
        try:
            return str(self.api.request("GET", f"/repos/{self.repo}/pulls/{number}", accept=DIFF))
        except ApiError as err:
            return self.rebuilt_diff(number, err)

    def rebuilt_diff(self, number: int, why: ApiError) -> str:
        """The diff from the PR's file list, when the diff endpoint refused (#75: 300 files)."""
        print(f"::warning::could not fetch the PR diff ({why}); rebuilding it from its files.")
        files = self.api.pages(f"/repos/{self.repo}/pulls/{number}/files")
        changed = int(
            self.api.request("GET", f"/repos/{self.repo}/pulls/{number}")["changed_files"]
        )
        if len(files) < changed:
            raise CannotRunError(f"it changes {changed} files and the API lists only {len(files)}.")
        unpatched = sum(1 for f in files if f.get("patch") is None)
        if unpatched:
            print(f"::warning::{unpatched} file(s) carry no patch and were not reviewed.")
        return rebuild_diff(files)

    def gatehouse(
        self, pr: Mapping[str, Any], action: str, diff: str
    ) -> tuple[int, dict[str, Any]]:
        """Run the gatehouse CLI through the shim and return what it would have posted."""
        number, head = int(pr["number"]), str(pr["head"]["sha"])
        capture = self.work / "gatehouse-posts.json"
        args = ["--stdin", "--comment", "--required-low-agents", REQUIRED_LOW_AGENTS, "--advisory"]
        if action == "synchronize":
            args.append("--incremental")
        try:
            context = self.constitution_context(str(pr["base"]["sha"]))
        except CannotRunError as err:
            return self.failed(head, f"the constitution to review against is unknown: {err}")
        if context is not None:
            args += ["--constitution", str(context)]
        env = tools.child_env(
            self.env,
            OPENROUTER_API_KEY=self.env["OPENROUTER_API_KEY"],
            GITHUB_TOKEN=self.read_token,
            GITHUB_REPOSITORY=self.repo,
            GITHUB_REF=f"refs/pull/{number}/merge",
            GATEHOUSE_CONTEXT_REPO=self.repo,
            GATEHOUSE_CONTEXT_REF=str(pr["base"]["sha"]),
            GATEHOUSE_HEAD_SHA=head,
            GATEHOUSE_HEAD_REPO=str((pr["head"].get("repo") or {}).get("full_name", "")),
            **{k: self.env[k] for k in ("GITHUB_STEP_SUMMARY",) if k in self.env},
        )
        done = self.runner(
            [tools.GATEHOUSE_PYTHON, "-I", str(tools.SHIM), str(capture), *args],
            env=env,
            stdin=diff,
            timeout=MODEL_SECONDS,
        )
        print(done.stdout, end="")
        print(done.stderr, end="", file=sys.stderr)
        try:
            posts = [p["payload"] for p in json.loads(capture.read_text(encoding="utf-8"))]
        except UNREADABLE_CAPTURE:
            return self.failed(head, f"the reviewer could not be run (exit {done.returncode}).")
        if done.returncode not in (0, 1):
            # Gatehouse's own 2: no review happened, and it says so in a post
            # carrying the failed marker. With no such post, one is made here.
            if not any(FAILED in str(p.get("body", "")) for p in posts):
                return self.failed(head, f"the reviewer exited {done.returncode}.")
            return CANNOT_RUN, {"status": "failed", "exit": done.returncode, "posts": posts}
        return OK, {"status": "reviewed", "exit": done.returncode, "posts": posts}

    def constitution_context(self, base: str) -> Path | None:
        """What a repo on constitution v2 is reviewed against; None for a v1 repo.

        Both halves come from the trusted base, never the PR: the constitution's
        text is the image's own, and the README section is read at the base SHA,
        so a pull request cannot rewrite the rules it is judged by. A v1 repo
        keeps Gatehouse's own discovery of its manifest at the same base.
        """
        manifest = f"/repos/{self.repo}/contents/{constitution.V1_MANIFEST}?ref={base}"
        try:
            self.api.request("GET", manifest, accept=RAW)
        except ApiError as err:
            if err.status != HTTPStatus.NOT_FOUND:
                raise
        else:
            return None
        try:
            readme = str(
                self.api.request(
                    "GET", f"/repos/{self.repo}/contents/README.md?ref={base}", accept=RAW
                )
            )
        except ApiError as err:
            if err.status != HTTPStatus.NOT_FOUND:
                raise
            readme = ""
        out = self.work / "constitution-context.md"
        try:
            out.write_text(constitution.context(readme, constitution.TEXT), encoding="utf-8")
        except constitution.CannotRunError as err:
            raise CannotRunError(str(err)) from err
        return out

    # -- Ashigaru's fix round ----------------------------------------------------------------

    def fix(self, pr: Mapping[str, Any], posts: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        """fix.yml's agent job: findings.py, then the model over the PR branch.

        findings.py reads the findings already posted. This run's review is
        not posted yet -- `write` posts it -- so its inline comments join the
        list with negative ids, which `write` swaps for the real ones once
        the review exists.
        """
        branch = str(pr["head"]["ref"])
        out = self.work / "findings.json"
        done, outputs = tools.ashigaru(
            self.site,
            "findings.py",
            [
                "--repo",
                self.repo,
                "--branch",
                branch,
                "--reviewer",
                APP_LOGIN,
                "--bot",
                APP_SLUG,
                "--max-rounds",
                str(MAX_ROUNDS),
                "--out",
                str(out),
            ],
            env=self.gh_env(GITHUB_RUN_ID=self.env.get("GITHUB_RUN_ID", "")),
        )
        tools.checked(done, "findings.py")
        if not out.exists():
            return OK, {"action": "none", "reason": outputs.get("action", "none")}
        collected = json.loads(out.read_text(encoding="utf-8"))
        collected["findings"] = list(collected.get("findings") or []) + pending_findings(posts)
        findings_mod = ashigaru_module("findings")
        used = int(collected.get("round", 1)) - 1
        action = findings_mod.plan(
            collected["findings"], collected.get("failed_checks") or [], used, MAX_ROUNDS
        )
        result: dict[str, Any] = {"action": action, "branch": branch, "findings": collected}
        if action != "fix":
            return OK, result
        model = findings_mod.model_for(used + 1, "sonnet", "opus", ESCALATE_AT)
        out.write_text(json.dumps(collected), encoding="utf-8")
        checkout = tools.clone(
            self.site,
            str(pr["head"]["sha"]),
            self.work / "pr",
            branch=branch,
        )
        prompt = self.prompt("fix", ["--repo", self.repo, "--findings", str(out)], checkout)
        answer = self.model(prompt, replace(FIX, model=model), checkout)
        result |= {"model": model, "result": answer, "patch": self.collect(checkout)}
        return OK, result

    # -- issues ------------------------------------------------------------------------------

    def issue(self, event: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """router.yml for issues: triage on `opened` or ready-for-triage, code on ready-to-code."""
        number = int(event["issue"]["number"])
        result: dict[str, Any] = {"v": 1, "kind": "issue", "number": number}
        stage = issue_stage(event)
        if stage is None or not self.env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            why = "Ashigaru is not enrolled here" if stage else "nothing for Ashigaru"
            print(f"ai: issue #{number}: {why}")
            return OK, result | {"skip": why}
        allowed, trust = self.guard(event, stage)
        result["trust"] = trust
        if not allowed:
            return OK, result | {"skip": f"guard refused {stage}"}
        checkout = tools.clone(self.site, default_branch(event), self.work / "repo")
        if stage == "triage":
            queue = "fresh" if event.get("action") == "opened" else "backlog"
            verdict = self.on_issue(TRIAGE, number, checkout)
            result["triage"] = {"verdict": verdict, "queue": queue}
            if not starts_now(verdict, trust, queue):
                return OK, result
            allowed, _ = self.guard(event, "code")
            if not allowed:
                return OK, result
        result["code"] = self.code(number, checkout, default_branch(event))
        return OK, result

    def guard(self, event: Mapping[str, Any], stage: str) -> tuple[bool, str]:
        """Ashigaru's guard.py: who may reach an agent, from the API's write access."""
        path = self.work / "event.json"
        path.write_text(json.dumps(event), encoding="utf-8")
        done, outputs = tools.ashigaru(
            self.site,
            "guard.py",
            ["--stage", stage],
            env=self.gh_env(
                GITHUB_EVENT_PATH=str(path), GITHUB_EVENT_NAME="issues", GITHUB_REPOSITORY=self.repo
            ),
        )
        tools.checked(done, "guard.py")
        return outputs.get("allowed") == "true", outputs.get("trust", "none")

    def on_issue(self, agent: Agent, number: int, checkout: Path) -> dict[str, Any]:
        """One of the issue agents (triage or code) on issue `number`: its structured answer.

        The prompt carries the issue, its comments and up to five of its images,
        as prompt.py renders them; the agent works in `checkout`.
        """
        attachments = self.work / "attachments"
        attachments.mkdir(exist_ok=True)
        prompt = self.prompt(
            agent.schema,
            ["--repo", self.repo, "--issue", str(number), "--attachments", str(attachments)],
            checkout,
        )
        return self.model(prompt, agent, checkout)

    def code(self, number: int, checkout: Path, base: str) -> dict[str, Any]:
        """code.yml's agent job, less its pre-commit retries (see the PR: those run repo code)."""
        answer = self.on_issue(CODE, number, checkout)
        return {"result": answer, "patch": self.collect(checkout), "base": base}

    # -- shared ------------------------------------------------------------------------------

    def gh_env(self, **extra: str) -> dict[str, str]:
        """A child's environment with the read token as gh's, and nothing else of the job's."""
        return tools.child_env(self.env, GH_TOKEN=self.read_token, **extra)

    def prompt(self, kind: str, args: list[str], cwd: Path) -> str:
        """Render one of Ashigaru's prompts with prompt.py, reading with the read token."""
        out = self.work / f"{kind}-prompt.md"
        done, _ = tools.ashigaru(
            self.site, "prompt.py", [kind, *args, "--out", str(out)], env=self.gh_env(), cwd=cwd
        )
        tools.checked(done, f"prompt.py {kind}")
        if not out.exists():
            raise CannotRunError(f"prompt.py {kind} wrote no prompt")
        return out.read_text(encoding="utf-8")

    def model(self, prompt: str, agent: Agent, cwd: Path) -> dict[str, Any]:
        """One claude run, as claude-code-action's base-action ran it, holding only its token.

        The prompt goes in on stdin: an issue or a diff can be longer than one
        argument may be. The answer is the run's structured output.
        """
        shape = json.loads(
            (tools.ASHIGARU_SCRIPTS.parent / "schemas" / f"{agent.schema}.json").read_text()
        )
        name = f"sdlc-ai-model-{secrets.token_hex(8)}"
        argv = [
            *self.sibling(cwd, name),
            "-p",
            "--model",
            agent.model,
            "--max-turns",
            str(agent.turns),
            "--allowedTools",
            agent.tools,
            "--json-schema",
            json.dumps(shape, separators=(",", ":")),
            "--settings",
            SETTINGS,
            "--output-format",
            "json",
        ]
        env = tools.child_env(self.env, CLAUDE_CODE_OAUTH_TOKEN=self.env["CLAUDE_CODE_OAUTH_TOKEN"])
        try:
            done = self.runner(
                argv, env=env, cwd=cwd, stdin=prompt, timeout=MODEL_SECONDS + MODEL_GRACE
            )
        finally:
            self.remove(name)
        if done.returncode != 0:
            raise CannotRunError(
                f"claude ({agent.schema}) exited {done.returncode}: {done.stderr[-400:]}"
            )
        return structured(done.stdout, agent.schema)

    def remove(self, name: str) -> None:
        """`podman rm -f` of the model's sibling; gone already is fine, a failure is said."""
        argv = [*core.podman(self.env), "rm", "-f", "--ignore", name]
        done = self.runner(argv, env=tools.child_env(self.env), timeout=tools.GIT_SECONDS)
        if done.returncode != 0:
            print(f"::warning::could not remove {name}: {(done.stderr or done.stdout).strip()}")

    def sibling(self, cwd: Path, name: str) -> list[str]:
        """podman's argv up to claude's own: the model's container (see MODEL_TMP)."""
        if self.mounted is None:
            self.mounted = self.mount()
        host, image = self.mounted
        if "@sha256:" not in image:
            raise CannotRunError(f"this job's image is not pinned by digest: {image!r}")

        def on_host(path: Path) -> str:
            return str(Path(host) / path.relative_to(self.work))

        git = cwd / ".git"
        return [
            *core.podman(self.env),
            "run",
            "--rm",
            "--name",
            name,
            "--timeout",
            str(MODEL_SECONDS),
            "-i",
            "--pull=never",
            "--user",
            "0",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--security-opt=label=disable",
            "--read-only",
            "--tmpfs",
            MODEL_TMP,
            "-v",
            f"{host}:{self.work}:ro",
            "-v",
            f"{on_host(cwd)}:{cwd}:rw",
            "-v",
            f"{on_host(git)}:{git}:ro",
            "-w",
            str(cwd),
            "-e",
            "HOME=/tmp",
            "-e",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "--entrypoint",
            "claude",
            image,
        ]

    def collect(self, checkout: Path) -> str:
        """What the model changed, as code.yml collected it: staged, binary-safe."""
        env = tools.child_env(self.env)
        tools.checked(self.runner(["git", "add", "-A"], env=env, cwd=checkout), "git add")
        done = self.runner(["git", "diff", "--cached", "--binary"], env=env, cwd=checkout)
        return tools.checked(done, "git diff").stdout


def earlier_heads(reviews: list[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """The newest five heads with a complete Gatehouse review by the App, newest first."""
    heads: list[tuple[str, str]] = []
    for review in reversed(reviews):
        body = str(review.get("body") or "")
        sha = str(review.get("commit_id") or "")
        if (
            (review.get("user") or {}).get("login") == APP_LOGIN
            and body.startswith("Gatehouse")
            and COMPLETE in body
            and FAILED not in body
            and len(sha) == SHA_LEN
            and all(c in "0123456789abcdef" for c in sha)
            and sha not in (h[0] for h in heads)
        ):
            heads.append((sha, str(review.get("html_url") or "")))
    return heads[:5]


def rebuild_diff(files: list[Mapping[str, Any]]) -> str:
    """A unified diff from the PR's file list: review.yml's jq, line for line."""
    lines: list[str] = []
    for f in files:
        new, status = str(f["filename"]), f.get("status")
        old = str(f.get("previous_filename") or new)
        lines.append(f"diff --git a/{old} b/{new}")
        if status == "added":
            lines.append("new file mode 100644")
        elif status == "removed":
            lines.append("deleted file mode 100644")
        elif status == "renamed":
            lines += [f"rename from {old}", f"rename to {new}"]
        if f.get("patch") is not None:
            lines.append("--- /dev/null" if status == "added" else f"--- a/{old}")
            lines.append("+++ /dev/null" if status == "removed" else f"+++ b/{new}")
            lines.append(str(f["patch"]))
    return "\n".join(lines) + "\n"


def pending_findings(posts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """This run's inline findings, in findings.py's shape, numbered -1, -2, ... in post order."""
    found: list[dict[str, Any]] = []
    for post in posts:
        for comment in post.get("comments") or []:
            found.append(
                {
                    "id": -(len(found) + 1),
                    "path": comment.get("path"),
                    "line": comment.get("line"),
                    "body": comment.get("body", ""),
                    "origin": "reviewer",
                    "kind": "inline",
                    "author": APP_LOGIN,
                }
            )
    return found


def issue_stage(event: Mapping[str, Any]) -> str | None:
    """`triage` for a new issue or ready-for-triage, `code` for ready-to-code, else None."""
    action, label = event.get("action"), (event.get("label") or {}).get("name")
    if action == "opened" or (action == "labeled" and label == "ready-for-triage"):
        return "triage"
    if action == "labeled" and label == "ready-to-code":
        return "code"
    return None


def starts_now(verdict: Mapping[str, Any], trust: str, queue: str) -> bool:
    """triage_apply.py's own decision, called rather than restated, so ai and write agree."""
    apply = ashigaru_module("triage_apply")
    state, _ = apply.settle(apply.decide(dict(verdict), AUTO_CATEGORIES, MIN_CONFIDENCE), trust)
    return bool(apply.starts_now(state, queue))


def structured(stdout: str, schema: str) -> dict[str, Any]:
    """The structured output of `claude -p --output-format json --json-schema`."""
    try:
        printed = json.loads(stdout)
    except json.JSONDecodeError as err:
        raise CannotRunError(f"claude ({schema}) printed no JSON: {err}") from err
    answer = printed.get("structured_output") if isinstance(printed, dict) else None
    if answer is None and isinstance(printed, dict):
        try:
            answer = json.loads(str(printed.get("result", "")))
        except json.JSONDecodeError:
            answer = None
    if not isinstance(answer, dict):
        raise CannotRunError(f"claude ({schema}) returned no structured output")
    return answer


def ashigaru_module(name: str) -> ModuleType:
    """One of Ashigaru's scripts, imported by path under a name of its own.

    Cached as `ashigaru_scripts.<name>`, so nothing else that happens to be
    called `findings` is ever reused. The scripts import each other flat
    (`from common import ...`), so their directory goes on sys.path for the
    import alone.
    """
    key = f"ashigaru_scripts.{name}"
    if key in sys.modules:
        return sys.modules[key]
    folder = str(tools.ASHIGARU_SCRIPTS)
    spec = importlib.util.spec_from_file_location(key, tools.ASHIGARU_SCRIPTS / f"{name}.py")
    if spec is None or spec.loader is None:
        raise CannotRunError(f"Ashigaru's {name}.py is not in {folder}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, folder)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(folder)
    sys.modules[key] = module
    return module
