"""The jobs of the fleet's one ci.yml, one module per trust level.

`sdlc event` reads GITHUB_EVENT_NAME, the event payload and GITHUB_JOB, and
runs `sdlc_jobs.<job>` through `_core.run_job`. A job exposes either

    def run(event_name: str, event: dict, env: Mapping[str, str],
            tools: Tools) -> int      # check, image, promote, pypi
    def main(event_name: str, event: dict, env: Mapping[str, str]) -> int
                                      # ai, write, gate

and returns 0 clean, 1 found or 2 could not run. `sdlc`'s 3 (deferred to CI)
is not a job's to return: a job is CI, so `run_job` makes it a 2. A module
whose name starts with an underscore is a helper, never a job.

A job module holds only what its job's credentials allow. `ai` holds model keys
and a read token and never an App key; `write` holds the App key and never runs
a model; `gate` holds neither. docs/marketplace-free-ci.md is the design.
"""
