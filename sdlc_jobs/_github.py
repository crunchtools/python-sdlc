"""The GitHub REST API, the job outputs and the exit-code contract, for every job.

No `gh` and no third-party HTTP client: urllib, as `release_publish.py` and
`update_tools.py` already do. Every call goes through `Api`, so a test hands a
job a fake and nothing touches real GitHub.
"""

from __future__ import annotations

import functools
import json
import re
import secrets
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Protocol

API_HOST = "api.github.com"
JSON = "application/vnd.github+json"
DIFF = "application/vnd.github.diff"
HTTP_TIMEOUT_SECONDS = 30

# The exit-code contract, named as `sdlc` names it.
OK, FOUND, CANNOT_RUN = 0, 1, 2

# The Ashigaru App's account as the API names it. `write` posts Gatehouse's
# review with the App's token, so this is also the reviewer `gate` trusts: a
# review by any other account, github-actions[bot] included, is not a review.
# A pull request's own `pull_request` run holds a GITHUB_TOKEN that can post
# as github-actions[bot]; it cannot post as the App.
APP_LOGIN = "ashigaru-crunchtools[bot]"
APP_SLUG = "ashigaru-crunchtools"

_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


class CannotRunError(Exception):
    """Anything that is not the job's own verdict. Becomes exit 2."""


class ApiError(CannotRunError):
    """GitHub answered with an error status, or not at all."""

    def __init__(self, method: str, path: str, status: int, message: str) -> None:
        super().__init__(f"{method} {path}: HTTP {status}: {message[:300]}")
        self.status = status


class Api(Protocol):
    """What a job needs of GitHub. `GitHub` is the real one; tests pass a fake."""

    def request(self, method: str, path: str, body: Any = None, *, accept: str = JSON) -> Any: ...

    def pages(self, path: str, key: str | None = None) -> list[Any]: ...


class GitHub:
    """One token's view of the REST API, on one host.

    Every request goes to that host. A pagination link is followed by its path
    and query alone, so a `Link` header can never send the token elsewhere.
    """

    def __init__(
        self,
        token: str,
        *,
        host: str = API_HOST,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self._token = token
        self._host = host
        self._open = opener

    def _call(self, method: str, path: str, body: Any, accept: str) -> tuple[Any, str]:
        payload = None if body is None else json.dumps(body).encode()
        headers = {
            "Accept": accept,
            "User-Agent": "crunchtools-sdlc",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if payload is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"https://{self._host}{path}", data=payload, method=method, headers=headers
        )
        try:
            with self._open(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                link = resp.headers.get("Link", "") or ""
        except urllib.error.HTTPError as err:
            raise ApiError(method, path, err.code, err.read().decode(errors="replace")) from err
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise ApiError(method, path, 0, str(err)) from err
        if accept != JSON:
            return text, link
        return (json.loads(text) if text.strip() else None), link

    def request(self, method: str, path: str, body: Any = None, *, accept: str = JSON) -> Any:
        """One call. `accept` other than JSON returns the body as text."""
        if not path.startswith("/"):
            raise CannotRunError(f"an API path must start with /: {path[:80]!r}")
        return self._call(method, path, body, accept)[0]

    def pages(self, path: str, key: str | None = None) -> list[Any]:
        """Every item of a list endpoint, following `Link: rel=next`.

        `key` names the list inside an object response (`workflow_runs`,
        `jobs`); without it the response must be the list itself.
        """
        sep = "&" if "?" in path else "?"
        url: str | None = f"{path}{sep}per_page=100"
        items: list[Any] = []
        while url:
            found, link = self._call("GET", url, None, JSON)
            page = found[key] if key else found
            if not isinstance(page, list):
                raise ApiError("GET", path, 0, "expected a list")
            items.extend(page)
            following = _NEXT.search(link)
            url = None
            if following:
                parts = urllib.parse.urlsplit(following.group(1))
                url = f"{parts.path}?{parts.query}" if parts.query else parts.path
        return items


def mask(value: str) -> None:
    """Tell the runner to redact `value` from every log line after this one."""
    if value:
        print(f"::add-mask::{value}", flush=True)


def set_output(env: Mapping[str, str], name: str, value: str) -> None:
    """Append one job output to $GITHUB_OUTPUT, multi-line safe.

    Without GITHUB_OUTPUT (a run outside Actions) it is printed instead, so a
    job still says what it would have handed on.
    """
    path = env.get("GITHUB_OUTPUT", "")
    if not path:
        print(f"output {name}={value[:200]}")
        return
    delim = f"SDLC_{secrets.token_hex(8)}"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}<<{delim}\n{value}\n{delim}\n")


def repo_of(event: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """owner/name, from the runner's GITHUB_REPOSITORY or else the payload."""
    repo = env.get("GITHUB_REPOSITORY") or (event.get("repository") or {}).get("full_name", "")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or "") or ".." in repo:
        raise CannotRunError(f"no repository to act on (got {repo!r})")
    return repo


def contract(fn: Callable[..., int]) -> Callable[..., int]:
    """Keep a job's main inside 0/1/2: a refusal or a crash is a 2, said out loud.

    The message names the job (`AI CANNOT RUN`, `WRITE CANNOT RUN`, ...) from
    the module the main lives in.

    A job that cannot run must never look like one that ran and passed, and a
    traceback is not a verdict.
    """

    job = fn.__module__.rsplit(".", 1)[-1].upper()

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> int:
        """The job's main, its exit kept inside the contract."""
        try:
            return fn(*args, **kwargs)
        except CannotRunError as err:
            print(f"::error::{job} CANNOT RUN: {err}", file=sys.stderr)
            return CANNOT_RUN
        except Exception as err:
            traceback.print_exc()
            print(f"::error::{job} CANNOT RUN: {type(err).__name__}: {err}", file=sys.stderr)
            return CANNOT_RUN

    return wrapper
