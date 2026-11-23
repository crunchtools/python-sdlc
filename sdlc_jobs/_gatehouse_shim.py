"""Run the gatehouse CLI unchanged, but keep what it would post instead of posting it.

`ai` holds the model key and a read token; `write` holds the App key and runs
no model. Gatehouse reviews and posts in one call, so this runs its real CLI,
under its own interpreter (/app/venv/bin/python -I), with the one function
every post goes through, gatehouse.github._post_review, replaced by a recorder.
Everything else -- the agents, the ledger, incremental reviews, the answered
threads, the failed-review posts -- is gatehouse's own code, reading with the
read token. `write` then posts each recorded review with the App's token.

The argument after the script names the JSON file the posts are written to;
the rest are gatehouse's own. The exit status is the CLI's.
"""

from __future__ import annotations

import inspect
import json
import sys
from typing import Any

import gatehouse.github
from gatehouse.cli import main

_posts: list[dict[str, Any]] = []


async def _record(repo: str, pr_number: int, token: str, payload: dict[str, Any]) -> bool:
    """gatehouse.github._post_review's signature, recording instead of posting."""
    del token  # `write` posts with its own
    _posts.append({"repo": repo, "pr": pr_number, "payload": payload})
    return True


if __name__ == "__main__":
    capture = sys.argv.pop(1)
    # The seam is gatehouse's private function. If a release renames it or
    # makes it synchronous, replacing it would post for real or not at all, so
    # the shim refuses (2: the review could not run) rather than guess.
    if not inspect.iscoroutinefunction(getattr(gatehouse.github, "_post_review", None)):
        print("gatehouse.github._post_review is gone or not async; not reviewing.", file=sys.stderr)
        sys.exit(2)
    # Looked up as a module global at call time, so replacing it here reaches
    # every caller inside gatehouse.github.
    gatehouse.github._post_review = _record
    code = 2
    try:
        main()
        code = 0
    except SystemExit as stop:
        code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 2)
    finally:
        with open(capture, "w", encoding="utf-8") as handle:
            json.dump(_posts, handle)
    sys.exit(code)
