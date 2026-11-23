"""One module per trust level of the fleet's ci.yml, run by `sdlc event`.

Each `sdlc_jobs/<job>.py` exposes

    def run(event_name: str, event: dict, env: Mapping[str, str],
            tools: Tools) -> int

and returns 0 clean, 1 found or 2 could not run. `sdlc`'s 3 (deferred to CI)
is not a job's to return: a job is CI, so `_core.run_job` makes it a 2. A module
whose name starts with an underscore is a helper, never a job.
"""
