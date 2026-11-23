"""GitHub App installation tokens, minted in-process: Ashigaru's and Renovate's.

What actions/create-github-app-token did, without it: an RS256 JWT from the
App's key, the repository's installation, then a token scoped to this one
repository and to only the permissions the step needs. The token is masked the
moment it exists and revoked when the step is done, which is what that
action's post step did.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

import jwt

from sdlc_jobs._github import Api, ApiError, CannotRunError, GitHub, mask

# A JWT may live ten minutes. It is backdated a minute for clock drift and
# expires a minute early, as GitHub's own guidance has it.
JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 540


def app_jwt(client_id: str, key_pem: str, now: float) -> str:
    """The App's own JWT. PyJWT (with cryptography) signs it."""
    issued = int(now)
    claims = {
        "iat": issued - JWT_BACKDATE_SECONDS,
        "exp": issued + JWT_LIFETIME_SECONDS,
        "iss": client_id,
    }
    return jwt.encode(claims, key_pem, algorithm="RS256")


@contextmanager
def app_token(
    env: Mapping[str, str],
    repo: str,
    permissions: Mapping[str, str],
    *,
    api_for: Callable[[str], Api] = GitHub,
    signer: Callable[[str, str, float], str] = app_jwt,
) -> Iterator[str]:
    """An Ashigaru token for `repo` alone, holding `permissions` alone, revoked on exit."""
    client_id = env.get("ASHIGARU_CLIENT_ID", "")
    key = env.get("ASHIGARU_APP_KEY", "")
    if not client_id or not key:
        raise CannotRunError(
            "ASHIGARU_CLIENT_ID and ASHIGARU_APP_KEY must both reach `write` "
            "(the `ashigaru` environment admits the default branch only)"
        )
    app = api_for(signer(client_id, key, time.time()))
    installation = app.request("GET", f"/repos/{repo}/installation")["id"]
    body = {"repositories": [repo.split("/", 1)[1]], "permissions": dict(permissions)}
    with minted(app, installation, body, api_for) as token:
        yield token


@contextmanager
def org_token(
    app: Api, org: str, permissions: Mapping[str, str], *, api_for: Callable[[str], Api] = GitHub
) -> Iterator[tuple[str, str]]:
    """(token, the App's slug): every repo of `org`'s installation, holding
    `permissions` alone, revoked on exit. `app` is the API as the App itself
    (`api_for(app_jwt(...))`), for an App whose job is the whole org."""
    slug = app.request("GET", "/app")["slug"]
    installation = app.request("GET", f"/orgs/{org}/installation")["id"]
    with minted(app, installation, {"permissions": dict(permissions)}, api_for) as token:
        yield token, slug


@contextmanager
def minted(
    app: Api, installation: int, body: Mapping[str, object], api_for: Callable[[str], Api]
) -> Iterator[str]:
    """An installation token made from `body`, masked at once and revoked on exit."""
    token = app.request("POST", f"/app/installations/{installation}/access_tokens", dict(body))[
        "token"
    ]
    mask(token)
    try:
        yield token
    finally:
        try:
            api_for(token).request("DELETE", "/installation/token")
        except ApiError as err:
            # It expires within the hour anyway; a failed revoke is worth a
            # warning, never a failed job after the work is done.
            print(f"::warning::could not revoke the App token: {err}", file=sys.stderr)
