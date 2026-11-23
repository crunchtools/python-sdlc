"""One module per trust level of the fleet's ci.yml, run by `sdlc event`.

Each `sdlc_jobs/<job>.py` exposes

    def main(event_name: str, event: dict, env: Mapping[str, str]) -> int

and returns the exit contract of `sdlc`: 0 clean, 1 found, 2 could not run,
3 could not run and CI judges the same commit. A module whose name starts with
an underscore is a helper, never a job.
"""
