"""An https server in a urllib handler chain: each URL answers from a table, and
every request it sees is kept, so a suite can drive a redirect through the real
redirect handler without the network:

    urllib.request.build_opener(SafeRedirect, Site({...})).open
"""

from __future__ import annotations

import email
import io
import urllib.request
import urllib.response


class Answer(urllib.response.addinfourl):
    """A response as AbstractHTTPHandler leaves it: `msg` is what HTTPErrorProcessor reads."""

    msg = "answered"


class Site(urllib.request.BaseHandler):
    """https_open answered from `pages`: url -> (status, body, Location or "")."""

    handler_order = 100  # ahead of the real HTTPSHandler, which never runs

    def __init__(self, pages: dict[str, tuple[int, bytes, str]]) -> None:
        self.pages = pages
        self.seen: list[urllib.request.Request] = []

    def https_open(self, req: urllib.request.Request) -> Answer:
        self.seen.append(req)
        status, body, location = self.pages[req.full_url]
        hdrs = email.message_from_string(f"Location: {location}\n\n" if location else "\n")
        return Answer(io.BytesIO(body), hdrs, req.full_url, status)

    def http_open(self, req: urllib.request.Request) -> None:
        raise AssertionError(f"opened plain http: {req.full_url}")
