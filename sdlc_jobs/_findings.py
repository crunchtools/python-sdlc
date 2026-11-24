"""Which Gatehouse findings a pull request must answer: the one definition.

The `ai` job tells Gatehouse which agents' LOWs to post, the `gate` job holds a
pull request for each posted finding this names with no answer, and `sdlc push`
fails on the same findings before anything is pushed. All three read it here,
so the local verdict and CI's cannot drift apart.
"""

from __future__ import annotations

# The agents whose LOW findings must be answered, by the name Gatehouse prints
# in a review comment and the slug its `--consensus-json` records.
REQUIRED_LOW_AGENTS = {"Bug Hunter": "bugs", "Security Scan": "security"}


def requires_answer(severity: str, agent: str) -> bool:
    """CRITICAL, HIGH, MEDIUM always; LOW only from REQUIRED_LOW_AGENTS (name or slug)."""
    if severity.lower() != "low":
        return True
    return agent in REQUIRED_LOW_AGENTS or agent in REQUIRED_LOW_AGENTS.values()
