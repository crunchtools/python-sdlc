"""The jobs of the fleet's one ci.yml, one module per trust level.

`sdlc event` reads GITHUB_EVENT_NAME, the event payload and GITHUB_JOB, and
calls `sdlc_jobs.<job>.main(event_name, event, env)`. Each main returns the
exit-code contract `sdlc` itself keeps:

    0   the job ran and found nothing to stop for
    1   the job ran and found something (a failed review, an unanswered finding)
    2   the job COULD NOT RUN
    3   could not run, and CI will judge the same commit (never returned here)

A job module holds only what its job's credentials allow. `ai` holds model keys
and a read token and never an App key; `write` holds the App key and never runs
a model; `gate` holds neither. docs/marketplace-free-ci.md is the design.
"""
