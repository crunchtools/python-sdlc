#!/usr/bin/env python3
"""What every job in sdlc_jobs shares: the handoff, the GitHub client, App tokens, tool plumbing.

  - the handoff is gzip+base64 in exactly four parts of at most 112,500 bytes,
    and a result over the cap is a TooLargeError, never a truncation
  - GitHub.request/pages speak the REST API through an injected opener: one
    host, bearer auth, paths that start with "/" (an absolute URL is refused),
    Link pagination by path and query on that same host, HTTP errors as
    ApiError, text on request; by default through OPENER, whose redirects keep
    the token on its own host and never go to plain http (#278)
  - an App token is scoped to one repository and exactly the named
    permissions, masked at once, and revoked on exit even when the body raises
  - a child process sees the allowlisted environment plus what it is handed,
    and a git token travels in the environment, never on argv

Nothing here touches the network; the only processes it starts are the
interpreter running itself and a command that does not exist.

Run: python3 tests/test_event_common.py
"""

from __future__ import annotations

import base64
import contextlib
import email.message
import gzip
import io
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

import fake_https
import jwt
import sdlc_root
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fake_github import FakeGitHub, FakeRunner, carries_credential, check, finish

from sdlc_jobs import _handoff, _tools
from sdlc_jobs._app_token import app_jwt, app_token
from sdlc_jobs._github import (
    CANNOT_RUN,
    DIFF,
    OPENER,
    ApiError,
    CannotRunError,
    GitHub,
    SafeRedirect,
    contract,
    mask,
    repo_of,
    set_output,
)
from sdlc_jobs._tools import Done, Site


def captured(fn: Any, *args: Any, **kw: Any) -> tuple[Any, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        value = fn(*args, **kw)
    return value, out.getvalue(), err.getvalue()


def raises(kind: type[BaseException], fn: Any, *args: Any) -> BaseException | None:
    try:
        fn(*args)
    except kind as err:
        return err
    return None


# --- _handoff ---------------------------------------------------------------

small = {"v": 1, "kind": "pr", "number": 7, "review": {"posts": [{"body": "x" * 50}]}}
parts = _handoff.encode(small)
check("handoff: round trip", _handoff.decode(parts) == small)
check("handoff: exactly four parts", len(parts) == 4)
check("handoff: a small result fills p0 only", parts[0] != "" and parts[1:] == ["", "", ""])
check("handoff: names and env names", _handoff.NAMES == ("p0", "p1", "p2", "p3"))
check("handoff: env names", _handoff.ENV_NAMES[3] == "SDLC_AI_3" and len(_handoff.ENV_NAMES) == 4)
check("handoff: encoding is deterministic", _handoff.encode(small) == parts)

# 100 KB of random bytes, base64'd, does not compress: the text spans parts.
noise = base64.b64encode(os.urandom(100_000)).decode()
big = {"v": 1, "blob": noise}
spread = _handoff.encode(big)
check("handoff: a big result still has four parts", len(spread) == 4)
check(
    "handoff: every part is within the cap",
    all(len(p) <= _handoff.CHUNK for p in spread),
    str([len(p) for p in spread]),
)
check("handoff: a big result spans more than one part", spread[1] != "")
check("handoff: a big result round-trips", _handoff.decode(spread) == big)
check("handoff: cap is 112,500 x 4", (_handoff.CHUNK, _handoff.PARTS) == (112_500, 4))

incompressible = {"blob": base64.b64encode(os.urandom(400_000)).decode()}
err = raises(_handoff.TooLargeError, _handoff.encode, incompressible)
check("handoff: incompressible payload over the cap raises", err is not None)
size = getattr(err, "size", 0)
check("handoff: TooLargeError carries the size", size > _handoff.CHUNK * _handoff.PARTS)
check("handoff: TooLargeError is a ValueError", isinstance(err, ValueError))
check("handoff: TooLargeError message names the size", f"{size:,}" in str(err))

check(
    "handoff: empty parts are a ValueError", raises(ValueError, _handoff.decode, ["", "", "", ""])
)
check("handoff: no parts at all is a ValueError", raises(ValueError, _handoff.decode, []))
check(
    "handoff: not base64 is a ValueError",
    raises(ValueError, _handoff.decode, ["!!! no", "", "", ""]),
)
check(
    "handoff: base64 of non-gzip is a ValueError",
    raises(ValueError, _handoff.decode, [base64.b64encode(b"plain text").decode()]),
)
whole = "".join(parts)
check(
    "handoff: a truncated gzip is a ValueError", raises(ValueError, _handoff.decode, [whole[:-12]])
)
not_json = base64.b64encode(gzip.compress(b"not json at all")).decode()
check("handoff: gzip of non-JSON is a ValueError", raises(ValueError, _handoff.decode, [not_json]))
for label, doc in (("a list", b"[1, 2]"), ("a number", b"7"), ("null", b"null")):
    enc = base64.b64encode(gzip.compress(doc)).decode()
    err = raises(ValueError, _handoff.decode, [enc])
    check(f"handoff: {label} is not an object", err is not None and "not an object" in str(err))
env = dict(zip(_handoff.ENV_NAMES, parts, strict=True))
check("handoff: from_env decodes SDLC_AI_0..3", _handoff.from_env(env) == small)
check("handoff: from_env with nothing is a ValueError", raises(ValueError, _handoff.from_env, {}))

# --- _github: the client ----------------------------------------------------


class Resp:
    def __init__(self, body: str, link: str = "") -> None:
        self._body, self.headers = body, {"Link": link} if link else {}

    def __enter__(self) -> Resp:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body.encode()


class Opener:
    """urllib.request.urlopen, answered from a queue and remembered."""

    def __init__(self, *answers: Any) -> None:
        self.answers, self.requests = list(answers), []

    def __call__(self, req: Any, timeout: float | None = None) -> Resp:
        del timeout
        self.requests.append(req)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


opener = Opener(Resp('{"id": 5}'))
got = GitHub("tok-1", opener=opener).request("GET", "/repos/o/r")
req = opener.requests[0]
check("github: request returns parsed JSON", got == {"id": 5})
check("github: url is the host + path", req.full_url == "https://api.github.com/repos/o/r")
check("github: Authorization is Bearer", req.get_header("Authorization") == "Bearer tok-1")
check("github: Accept is the JSON type", req.get_header("Accept") == "application/vnd.github+json")
check(
    "github: a GET has no body or content type",
    req.data is None and not req.has_header("Content-type"),
)

opener = Opener(Resp('{"ok": true}'))
GitHub("t", opener=opener).request("POST", "/x", {"a": 1})
req = opener.requests[0]
check("github: POST sends the JSON body", json.loads(req.data) == {"a": 1} and req.method == "POST")
check("github: POST has a JSON content type", req.get_header("Content-type") == "application/json")

opener = Opener(Resp("{}"))
GitHub("", opener=opener).request("GET", "/x")
check("github: no token sends no Authorization", not opener.requests[0].has_header("Authorization"))

opener = Opener(Resp("{}"))
GitHub("t", host="ghe.example", opener=opener).request("GET", "/x")
check(
    "github: the host is configurable and the only one used",
    opener.requests[0].full_url == "https://ghe.example/x",
)

for refused_path in (
    "https://api.github.com/repos/o/r/next?page=2",
    "https://evil.example/x",
    "repos/o/r",
    "",
):
    opener = Opener(Resp("{}"))
    err = raises(CannotRunError, GitHub("t", opener=opener).request, "GET", refused_path)
    check(
        f"github: a path of {refused_path!r} is refused before any request",
        type(err) is CannotRunError and "must start with /" in str(err) and opener.requests == [],
    )

check(
    "github: an empty body is None",
    GitHub("t", opener=Opener(Resp("  \n"))).request("DELETE", "/x") is None,
)

opener = Opener(Resp("diff --git a/x b/x\n+1\n"))
text = GitHub("t", opener=opener).request("GET", "/x", accept=DIFF)
check("github: a non-JSON accept returns text", text == "diff --git a/x b/x\n+1\n")
check("github: and sends that Accept", opener.requests[0].get_header("Accept") == DIFF)

opener = Opener(
    Resp('["a", "b"]', '<https://api.github.com/l?per_page=100&page=2>; rel="next"'), Resp('["c"]')
)
items = GitHub("t", opener=opener).pages("/l")
check("github: pages follows Link rel=next", items == ["a", "b", "c"])
check(
    "github: pages asks for 100 per page", opener.requests[0].full_url.endswith("/l?per_page=100")
)
check(
    "github: pages follows the Link by path and query",
    opener.requests[1].full_url == "https://api.github.com/l?per_page=100&page=2",
)
check("github: pages stops without a next link", len(opener.requests) == 2)

opener = Opener(
    Resp("[1]", '<https://evil.example/steal?page=2>; rel="next"'),
    Resp("[2]", '<https://evil.example/steal?page=3>; rel="next"'),
    Resp("[3]"),
)
items = GitHub("tok-secret", opener=opener).pages("/l")
check("github: a Link to another host is still followed", items == [1, 2, 3])
check(
    "github: and is requested on api.github.com, path and query only",
    [r.full_url for r in opener.requests]
    == [
        "https://api.github.com/l?per_page=100",
        "https://api.github.com/steal?page=2",
        "https://api.github.com/steal?page=3",
    ],
)
check(
    "github: so the token never leaves for the Link's host",
    all("evil.example" not in r.full_url for r in opener.requests)
    and all(r.get_header("Authorization") == "Bearer tok-secret" for r in opener.requests),
)

check("github: the default opener is OPENER", GitHub("t")._open == OPENER.open)
check(
    "github: OPENER redirects through SafeRedirect",
    any(isinstance(h, SafeRedirect) for h in OPENER.handlers),
)
for where, kept in (("https://objects.example/x", False), ("https://api.github.com/y", True)):
    site = fake_https.Site(
        {"https://api.github.com/x": (302, b"", where), where: (200, b'{"id": 7}', "")}
    )
    client = GitHub("tok-r", opener=urllib.request.build_opener(SafeRedirect, site).open)
    got = client.request("GET", "/x")
    check(f"github: a 302 to {where} is followed", got == {"id": 7})
    check(
        f"github: and {'keeps' if kept else 'drops'} Authorization there",
        site.seen[-1].has_header("Authorization") == kept,
    )
site = fake_https.Site({"https://api.github.com/x": (302, b"{}", "http://api.github.com/x")})
plain = GitHub("t", opener=urllib.request.build_opener(SafeRedirect, site).open)
err = raises(ApiError, plain.request, "GET", "/x")
check(
    "github: a 302 to plain http is an ApiError of the 302, never opened",
    isinstance(err, ApiError) and err.status == 302 and len(site.seen) == 1,
)

opener = Opener(Resp("[1]", '<https://evil.example/s>; rel="next"'), Resp("[2]"))
GitHub("t", host="ghe.example", opener=opener).pages("/l")
check(
    "github: a Link is followed on the client's own host",
    opener.requests[1].full_url == "https://ghe.example/s",
)

link = (
    '<https://api.github.com/l?page=1>; rel="prev", <https://api.github.com/l?page=3>; rel="next"'
)
opener = Opener(Resp("[1]", link), Resp("[2]"))
check(
    "github: next is picked out among several rels",
    GitHub("t", opener=opener).pages("/l") == [1, 2],
)

opener = Opener(Resp('{"jobs": [1, 2]}'))
check(
    "github: pages with a key reads the list inside",
    GitHub("t", opener=opener).pages("/l", "jobs") == [1, 2],
)
opener = Opener(Resp("[]"))
GitHub("t", opener=opener).pages("/l?state=open")
check(
    "github: pages joins an existing query with &",
    "/l?state=open&per_page=100" in opener.requests[0].full_url,
)
err = raises(ApiError, GitHub("t", opener=Opener(Resp('{"a": 1}'))).pages, "/l")
check("github: pages of an object without a key is an ApiError", err is not None)

http = urllib.error.HTTPError(
    "u", 422, "Unprocessable", email.message.Message(), io.BytesIO(b"bad things")
)
err = raises(ApiError, GitHub("t", opener=Opener(http)).request, "POST", "/repos/o/r/x")
check("github: HTTPError becomes ApiError with the status", getattr(err, "status", 0) == 422)
check(
    "github: ApiError names method, path and body",
    "POST /repos/o/r/x" in str(err) and "bad things" in str(err),
)
check("github: ApiError is a CannotRunError", isinstance(err, CannotRunError))
err = raises(
    ApiError, GitHub("t", opener=Opener(urllib.error.URLError("no route"))).request, "GET", "/x"
)
check("github: URLError is an ApiError with status 0", getattr(err, "status", -1) == 0)
err = raises(ApiError, GitHub("t", opener=Opener(TimeoutError("slow"))).request, "GET", "/x")
check("github: a timeout is an ApiError with status 0", getattr(err, "status", -1) == 0)
http = urllib.error.HTTPError("u", 404, "nf", email.message.Message(), io.BytesIO(b"gone"))
err = raises(ApiError, GitHub("t", opener=Opener(http)).pages, "/l")
check("github: pages surfaces HTTP errors too", getattr(err, "status", 0) == 404)

# --- _github: outputs, repo_of, mask, contract ------------------------------

with tempfile.TemporaryDirectory() as scratch:
    path = Path(scratch) / "out"
    out_env = {"GITHUB_OUTPUT": str(path)}
    set_output(out_env, "one", "plain")
    set_output(out_env, "two", "line a\nline b\nk=v")
    set_output(out_env, "three", "")
    read = _tools.read_outputs(path.read_text())
    check("outputs: single line round-trips", read.get("one") == "plain")
    check("outputs: multi-line with = round-trips", read.get("two") == "line a\nline b\nk=v")
    check("outputs: an empty value is kept", read.get("three") == "")
    check("outputs: written as heredoc blocks", "one<<SDLC_" in path.read_text())
    set_output(out_env, "one", "again")
    check(
        "outputs: appends, the later one wins",
        _tools.read_outputs(path.read_text())["one"] == "again",
    )

_, out, _ = captured(set_output, {}, "name", "v" * 500)
check(
    "outputs: without GITHUB_OUTPUT it prints, truncated", out == "output name=" + "v" * 200 + "\n"
)

_, out, _ = captured(mask, "s3cret")
check("mask: prints the add-mask command", out == "::add-mask::s3cret\n")
_, out, _ = captured(mask, "")
check("mask: an empty value prints nothing", out == "")

check("repo_of: from GITHUB_REPOSITORY", repo_of({}, {"GITHUB_REPOSITORY": "o/r"}) == "o/r")
check(
    "repo_of: from the payload",
    repo_of({"repository": {"full_name": "a.b/c-d_e"}}, {}) == "a.b/c-d_e",
)
check(
    "repo_of: the runner's value wins over the payload",
    repo_of({"repository": {"full_name": "x/y"}}, {"GITHUB_REPOSITORY": "o/r"}) == "o/r",
)
for junk in ("", "norepo", "a/b/c", "a b/c", "o/r;rm", "o/r\n", "/r", "o/"):
    got_err = raises(CannotRunError, repo_of, {"repository": {"full_name": junk}}, {})
    check(f"repo_of: rejects {junk!r}", got_err is not None)
check("repo_of: no repository anywhere", raises(CannotRunError, repo_of, {}, {}) is not None)


@contract
def returns(code: int) -> int:
    return code


@contract
def refuses() -> int:
    raise CannotRunError("no key")


@contract
def crashes() -> int:
    raise KeyError("boom")


check("contract: passes 0 through", captured(returns, 0)[0] == 0)
check("contract: passes 1 through", captured(returns, 1)[0] == 1)
check("contract: passes 2 through", captured(returns, 2)[0] == 2)
code, _, err_text = captured(refuses)
check(
    "contract: CannotRunError is exit 2, said out loud",
    code == CANNOT_RUN and "__MAIN__ CANNOT RUN: no key" in err_text,  # named for its module
)
code, _, err_text = captured(crashes)
check(
    "contract: any exception is exit 2 with a traceback",
    code == CANNOT_RUN and "KeyError" in err_text and "Traceback" in err_text,
)
check("contract: keeps the function's name", returns.__name__ == "returns")

# --- _app_token -------------------------------------------------------------

REPO = "crunchtools/demo"
PERMS = {"contents": "write", "issues": "write"}
APP_ENV = {"ASHIGARU_CLIENT_ID": "Iv1.abc", "ASHIGARU_APP_KEY": "PEM-KEY"}


class Rig:
    """A fake GitHub per bearer token, so the JWT's calls and the install token's are apart."""

    def __init__(self, revoke: Any = None) -> None:
        self.app = FakeGitHub(
            {
                ("GET", f"/repos/{REPO}/installation"): {"id": 42},
                ("POST", "/app/installations/42/access_tokens"): {"token": "ghs_installtok"},
            }
        )
        self.install = FakeGitHub({("DELETE", "/installation/token"): revoke})
        self.tokens: list[str] = []
        self.signed: list[tuple[str, str, float]] = []

    def api_for(self, token: str) -> FakeGitHub:
        self.tokens.append(token)
        return self.install if token == "ghs_installtok" else self.app

    def signer(self, client_id: str, key: str, now: float) -> str:
        self.signed.append((client_id, key, now))
        return "the.jwt"


for missing, label in (
    ({}, "both"),
    ({"ASHIGARU_CLIENT_ID": "x"}, "the key"),
    ({"ASHIGARU_APP_KEY": "k"}, "the client id"),
):
    rig = Rig()
    try:
        with app_token(missing, REPO, PERMS, api_for=rig.api_for, signer=rig.signer):
            pass
        refused = False
    except CannotRunError as refusal:
        refused = "ASHIGARU_CLIENT_ID" in str(refusal)
    check(f"app_token: missing {label} is CannotRunError", refused)
    check(f"app_token: missing {label} calls nothing", rig.tokens == [] and rig.signed == [])
rig = Rig()
try:
    with app_token(
        {"ASHIGARU_CLIENT_ID": "", "ASHIGARU_APP_KEY": ""}, REPO, PERMS, api_for=rig.api_for
    ):
        pass
    refused = False
except CannotRunError:
    refused = True
check("app_token: empty strings count as missing", refused)

rig = Rig()
seen_inside: dict[str, Any] = {}


def body_of_the_step() -> None:
    with (
        mock.patch.object(time, "time", return_value=1234.5),
        app_token(APP_ENV, REPO, PERMS, api_for=rig.api_for, signer=rig.signer) as tok,
    ):
        seen_inside["token"] = tok
        seen_inside["deleted_yet"] = bool(rig.install.made("DELETE"))


_, out, _ = captured(body_of_the_step)
check("app_token: yields the minted token", seen_inside["token"] == "ghs_installtok")
check(
    "app_token: signs with the client id, key and the time",
    rig.signed == [("Iv1.abc", "PEM-KEY", 1234.5)],
)
signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
signing_pem = signing_key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
claims = jwt.decode(
    app_jwt("Iv1.abc", signing_pem, 2000.9),
    signing_key.public_key(),
    algorithms=["RS256"],
    options={"verify_exp": False, "verify_iat": False},
)
check(
    "app_jwt: RS256 claims, backdated a minute, expiring inside ten",
    claims == {"iat": 2000 - 60, "exp": 2000 + 540, "iss": "Iv1.abc"},
    str(claims),
)
check("app_token: the JWT is the first api's token", rig.tokens[0] == "the.jwt")
check(
    "app_token: asks for the repo's installation",
    rig.app.made("GET", f"/repos/{REPO}/installation") != [],
)
minted = rig.app.made("POST", "/app/installations/42/access_tokens")
check("app_token: POSTs to the installation's access_tokens", len(minted) == 1)
check(
    "app_token: scoped to the repo name alone and exactly the permissions",
    minted[0][2] == {"repositories": ["demo"], "permissions": PERMS},
    str(minted[0][2]),
)
check("app_token: masks the token", "::add-mask::ghs_installtok" in out)
check("app_token: does not mask the JWT or the key", "the.jwt" not in out and "PEM-KEY" not in out)
check("app_token: not revoked while in use", seen_inside["deleted_yet"] is False)
check(
    "app_token: revoked on exit with the install token",
    len(rig.install.made("DELETE", "/installation/token")) == 1,
)
check("app_token: the revoke used the install token", rig.tokens[-1] == "ghs_installtok")

rig = Rig()


def raising_body() -> None:
    with app_token(APP_ENV, REPO, PERMS, api_for=rig.api_for, signer=rig.signer):
        raise RuntimeError("step failed")


try:
    captured(raising_body)
    propagated = False
except RuntimeError as boom:
    propagated = "step failed" in str(boom)
check("app_token: the body's exception propagates", propagated)
check("app_token: revoked even when the body raises", len(rig.install.made("DELETE")) == 1)

rig = Rig(revoke=ApiError("DELETE", "/installation/token", 500, "down"))


def revoke_fails() -> str:
    with app_token(APP_ENV, REPO, PERMS, api_for=rig.api_for, signer=rig.signer) as tok:
        return tok


value, _, err_text = captured(revoke_fails)
check("app_token: a failing revoke does not raise", value == "ghs_installtok")
check("app_token: a failing revoke warns", "::warning::could not revoke the App token" in err_text)

# --- _tools: child_env, git_auth, clone, read_outputs, ashigaru -------------

SECRETS = {
    "ASHIGARU_APP_KEY": "PEM-KEY",
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok",
    "OPENROUTER_API_KEY": "or-key",
    "GITHUB_TOKEN": "ghs-read",
    "GH_TOKEN": "gh-tok",
    "ASHIGARU_CLIENT_ID": "cid",
}
JOB_ENV = {
    **SECRETS,
    "PATH": "/p",
    "HOME": "/h",
    "LANG": "C",
    "TZ": "UTC",
    "RANDOM_VAR": "x",
    "GITHUB_EVENT_PATH": "/e",
}
cenv = _tools.child_env(JOB_ENV)
check(
    "child_env: passes the allowlisted names",
    cenv == {"PATH": "/p", "HOME": "/h", "LANG": "C", "TZ": "UTC"},
)
for name in (
    "ASHIGARU_APP_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENROUTER_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
):
    check(f"child_env: {name} does not leak", name not in cenv)
check("child_env: nothing else leaks", "RANDOM_VAR" not in cenv and "GITHUB_EVENT_PATH" not in cenv)
cenv = _tools.child_env(JOB_ENV, GH_TOKEN="given", EXTRA="e")
check("child_env: extras are added exactly", cenv["GH_TOKEN"] == "given" and cenv["EXTRA"] == "e")
check(
    "child_env: and only the extras",
    "GITHUB_TOKEN" not in cenv and "OPENROUTER_API_KEY" not in cenv,
)
bare = _tools.child_env({})
check(
    "child_env: PATH and HOME get defaults",
    bare["PATH"] and bare["HOME"] and set(bare) == {"PATH", "HOME"},
)
check("child_env: the default PATH is os.defpath", bare["PATH"] == os.defpath)
check(
    "statuses: a timeout is 124 and a command that cannot start is 127",
    (_tools.TIMED_OUT, _tools.NOT_STARTED) == (124, 127),
)
missing_cmd = _tools.run(["no-such-command-for-sdlc-tests"], env=bare)
check(
    "run: a command that cannot start is a status, not a raise",
    missing_cmd.returncode == _tools.NOT_STARTED and "no-such-command" in missing_cmd.stderr,
)
sleeper = _tools.run(
    [sys.executable, "-I", "-c", "import time; time.sleep(30)"], env=bare, timeout=0.5
)
check(
    "run: a child that runs out of time is a status, not a raise",
    sleeper.returncode == _tools.TIMED_OUT,
)
echoed = _tools.run([sys.executable, "-I", "-c", "print('hi')"], env=bare)
check(
    "run: a child's status and output are returned",
    (echoed.returncode, echoed.stdout) == (0, "hi\n"),
)

auth = _tools.git_auth("tok-123")
check("git_auth: one config entry", auth["GIT_CONFIG_COUNT"] == "1")
check(
    "git_auth: an extraheader for github.com",
    auth["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader",
)
value = auth["GIT_CONFIG_VALUE_0"]
check("git_auth: AUTHORIZATION: basic", value.startswith("AUTHORIZATION: basic "))
check(
    "git_auth: the credential is base64 x-access-token:<token>",
    base64.b64decode(value.removeprefix("AUTHORIZATION: basic ")).decode()
    == "x-access-token:tok-123",
)
check(
    "git_auth: the token is in no other value",
    all("tok-123" not in v for k, v in auth.items() if k != "GIT_CONFIG_VALUE_0"),
)
check("git_auth: never prompts", auth["GIT_TERMINAL_PROMPT"] == "0")

with tempfile.TemporaryDirectory() as tree:
    for tok, count in (("tok-123", "2"), ("", "1")):
        genv = _tools.git_env(Path(tree), tok)
        last = int(count) - 1
        check(
            f"git_env: only the tree is a safe.directory (token={bool(tok)})",
            genv["GIT_CONFIG_COUNT"] == count
            and genv[f"GIT_CONFIG_KEY_{last}"] == "safe.directory"
            and genv[f"GIT_CONFIG_VALUE_{last}"] == str(Path(tree).resolve()),
        )
    check("git_env: never safe.directory=*", "*" not in _tools.git_env(Path(tree)).values())
    base = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.x", "GIT_CONFIG_VALUE_0": "y"}
    merged = {**base, **_tools.git_env(Path(tree), "tok-123", base)}
    check(
        "git_env: numbered after the base's entries, which survive the merge",
        merged["GIT_CONFIG_COUNT"] == "3"
        and (merged["GIT_CONFIG_KEY_0"], merged["GIT_CONFIG_VALUE_0"]) == ("core.x", "y")
        and merged["GIT_CONFIG_KEY_1"].endswith(".extraheader")
        and merged["GIT_CONFIG_KEY_2"] == "safe.directory",
    )
    check(
        "carries_credential: header names match in any case",
        carries_credential({"GIT_CONFIG_KEY_0": "x", "GIT_CONFIG_VALUE_0": "authorization: b"})
        and carries_credential({"GIT_CONFIG_KEY_0": "http.ExtraHeader", "GIT_CONFIG_VALUE_0": ""})
        and not carries_credential(_tools.git_env(Path(tree))),
    )

runner = FakeRunner()
with tempfile.TemporaryDirectory() as scratch:
    site = Site(REPO, JOB_ENV, runner, Path(scratch))
    into = Path(scratch) / "nested" / "co"
    got_path = _tools.clone(site, "abc123", into, token="app-tok")
    check("clone: returns the directory and creates it", got_path == into and into.is_dir())
    argvs = [c[0] for c in runner.calls]
    check(
        "clone: init, remote add, fetch, checkout in order",
        [a[1] for a in argvs] == ["init", "remote", "fetch", "checkout"],
        str(argvs),
    )
    check(
        "clone: remote is the https url",
        argvs[1] == ["git", "remote", "add", "origin", f"https://github.com/{REPO}.git"],
    )
    check(
        "clone: fetch is blobless, of the ref",
        argvs[2] == ["git", "fetch", "-q", "--filter=blob:none", "origin", "abc123"],
    )
    check(
        "clone: detached checkout of FETCH_HEAD without a branch",
        argvs[3] == ["git", "checkout", "-q", "--detach", "FETCH_HEAD"],
    )
    check("clone: every step runs in the directory", all(c[2] == into for c in runner.calls))
    check("clone: the token is on no argv", all("app-tok" not in " ".join(a) for a in argvs))
    check(
        "clone: the token is in the env, as the extraheader only",
        all(
            "app-tok" not in v
            for c in runner.calls
            for k, v in c[1].items()
            if k != "GIT_CONFIG_VALUE_0"
        )
        and all(
            base64.b64encode(b"x-access-token:app-tok").decode() in c[1]["GIT_CONFIG_VALUE_0"]
            for c in runner.calls
        ),
    )
    check(
        "clone: no job credential reaches git",
        all(not set(SECRETS) & set(c[1]) for c in runner.calls),
    )

    runner = FakeRunner()
    site = Site(REPO, JOB_ENV, runner, Path(scratch))
    _tools.clone(site, "sha", Path(scratch) / "b", branch="feat/x")
    argvs = [c[0] for c in runner.calls]
    check(
        "clone: a branch checks out with -B",
        argvs[3] == ["git", "checkout", "-q", "-B", "feat/x", "FETCH_HEAD"],
    )
    check(
        "clone: a branch also sets the remote-tracking ref",
        argvs[4] == ["git", "update-ref", "refs/remotes/origin/feat/x", "FETCH_HEAD"],
    )
    check(
        "clone: no token, no extraheader",
        all(
            not carries_credential(c[1]) and c[1]["GIT_TERMINAL_PROMPT"] == "0"
            for c in runner.calls
        ),
    )

    runner = FakeRunner([(["fetch"], Done(128, "", "fatal: could not read from remote"))])
    site = Site(REPO, JOB_ENV, runner, Path(scratch))
    err = raises(CannotRunError, _tools.clone, site, "sha", Path(scratch) / "c")
    check(
        "clone: a failing step is CannotRunError, naming it",
        err is not None and "git fetch" in str(err) and "128" in str(err),
    )
    check("clone: and stops there", runner.ran("checkout") == [])

check("checked: passes a zero exit", _tools.checked(Done(0, "o", ""), "x").stdout == "o")
err = raises(CannotRunError, _tools.checked, Done(3, "", "bad"), "thing")
check(
    "checked: a non-zero exit is CannotRunError",
    err is not None and "thing failed (exit 3): bad" in str(err),
)

blocks = "a<<EOF1\nline1\nline2\nEOF1\nb=simple\nc=has=equals\nd<<X\n\nX\n"
check(
    "read_outputs: parses both forms",
    _tools.read_outputs(blocks) == {"a": "line1\nline2", "b": "simple", "c": "has=equals", "d": ""},
    str(_tools.read_outputs(blocks)),
)
check("read_outputs: empty text", _tools.read_outputs("") == {})
check(
    "read_outputs: junk lines are ignored",
    _tools.read_outputs("not an output\n=x\n1bad=2\nok=1\n") == {"ok": "1"},
)
check(
    "read_outputs: a heredoc body that looks like name=value stays in the block",
    _tools.read_outputs("blk<<D\nx=1\nD\n") == {"blk": "x=1"},
)

fixture_scripts = sdlc_root.ROOT / "ashigaru-scripts"
scripts_patch = mock.patch.object(_tools, "ASHIGARU_SCRIPTS", fixture_scripts)
scripts_patch.start()
seen: dict[str, Any] = {}


def script_answer(
    args: list[str], env: dict[str, str], cwd: Path | None, stdin: str | None
) -> Done:
    del stdin
    seen.update(args=args, env=env, cwd=cwd)
    Path(env["GITHUB_OUTPUT"]).write_text("action=fix\nmsg<<D\nmulti\nline\nD\n")
    seen["path_existed"] = Path(env["GITHUB_OUTPUT"]).exists()
    return Done(0, "script said hi", "")


runner = FakeRunner([(["findings.py"], script_answer)])
site = Site(REPO, JOB_ENV, runner, sdlc_root.ROOT)
(done, outputs), printed, _ = captured(
    _tools.ashigaru,
    site,
    "findings.py",
    ["--repo", REPO],
    env={"PATH": "/p", "GH_TOKEN": "t"},
    cwd=sdlc_root.ROOT,
)
check(
    "ashigaru: runs python3 on the script path with the args",
    seen["args"] == ["python3", str(fixture_scripts / "findings.py"), "--repo", REPO],
)
check(
    "ashigaru: sets GITHUB_OUTPUT on top of the given env",
    seen["env"]["GH_TOKEN"] == "t" and seen["env"]["GITHUB_OUTPUT"].endswith(".out"),
)
check("ashigaru: passes cwd", seen["cwd"] == sdlc_root.ROOT)
check(
    "ashigaru: returns the outputs the script wrote",
    outputs == {"action": "fix", "msg": "multi\nline"},
    str(outputs),
)
check("ashigaru: returns the Done", done.returncode == 0 and done.stdout == "script said hi")
check("ashigaru: prints the script's stdout", printed == "script said hi\n")
check(
    "ashigaru: removes the outputs file",
    seen["path_existed"] and not Path(seen["env"]["GITHUB_OUTPUT"]).exists(),
)

runner = FakeRunner([(["x.py"], Done(1, "", "bad"))])
site = Site(REPO, JOB_ENV, runner, sdlc_root.ROOT)
(done, outputs), printed, _ = captured(_tools.ashigaru, site, "x.py", [], env={})
check(
    "ashigaru: a script that writes no outputs gives {}",
    outputs == {} and done.returncode == 1 and printed == "",
)

runner = FakeRunner([(["y.py"], Done(0, "no newline", ""))])
site = Site(REPO, JOB_ENV, runner, sdlc_root.ROOT)
captured_out = captured(_tools.ashigaru, site, "y.py", [], env={})[1]
check("ashigaru: a trailing newline is added to stdout", captured_out == "no newline\n")
scripts_patch.stop()

# --- the Gatehouse shim, executed ---------------------------------------------
# A stand-in `gatehouse` package whose CLI posts twice through
# gatehouse.github._post_review and exits with the code its argv asks for.

with tempfile.TemporaryDirectory(prefix="shim-") as shim_dir:
    package = Path(shim_dir) / "gatehouse"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "github.py").write_text(
        "async def _post_review(repo, pr_number, token, payload):\n"
        "    raise AssertionError('the real post must never run')\n"
        "async def post_both():\n"
        "    await _post_review('o/r', 5, 'tok', {'body': 'one'})\n"
        "    return await _post_review('o/r', 5, 'tok', {'body': 'two'})\n"
    )
    (package / "cli.py").write_text(
        "import asyncio, sys\n"
        "from gatehouse import github\n"
        "def main():\n"
        "    assert asyncio.run(github.post_both()) is True\n"
        "    sys.exit(int(sys.argv[-1]))\n"
    )
    for wanted in (0, 2):
        capture = Path(shim_dir) / f"posts-{wanted}.json"
        done = _tools.run(
            [sys.executable, str(_tools.SHIM), str(capture), "--stdin", str(wanted)],
            env={**_tools.child_env({}), "PYTHONPATH": shim_dir},
        )
        recorded = json.loads(capture.read_text()) if capture.exists() else None
        check(
            f"shim: exit {wanted} is the CLI's, and both posts are recorded, not sent",
            done.returncode == wanted
            and recorded
            == [
                {"repo": "o/r", "pr": 5, "payload": {"body": "one"}},
                {"repo": "o/r", "pr": 5, "payload": {"body": "two"}},
            ],
            done.stdout + done.stderr,
        )

# The seam gone or made synchronous: the shim refuses rather than guess.
with tempfile.TemporaryDirectory(prefix="shim-sync-") as sync_dir:
    package = Path(sync_dir) / "gatehouse"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "github.py").write_text(
        "def _post_review(repo, pr_number, token, payload):\n    return True\n"
    )
    (package / "cli.py").write_text("def main():\n    raise AssertionError('ran')\n")
    capture = Path(sync_dir) / "posts.json"
    done = _tools.run(
        [sys.executable, str(_tools.SHIM), str(capture), "--stdin"],
        env={**_tools.child_env({}), "PYTHONPATH": sync_dir},
    )
    check(
        "shim: a synchronous _post_review is refused with 2, gatehouse never runs",
        done.returncode == 2 and not capture.exists() and "not async" in done.stderr,
        done.stdout + done.stderr,
    )

if __name__ == "__main__":
    sys.exit(finish())
