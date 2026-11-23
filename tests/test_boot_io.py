#!/usr/bin/env python3
"""The boot gate's real I/O: stdio JSON-RPC, timeouts and the HTTP client, on real processes.

`test_job_image.py` fakes `Tools`; this suite does not. It starts local `python3` processes
as MCP servers (stdio and HTTP) and runs `Tools.converse`, `converse_on`, `await_reply`,
`Tools.run` and `_boot.HTTP_CLIENT` against them. No podman, no mocks of the code under test.

Run: tests/test_boot_io.py   (needs python3 only)
"""

from __future__ import annotations

import http.server
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdlc_jobs import _boot
from sdlc_jobs._core import TIMED_OUT, Tools, await_reply, converse_on, text_of

PY = sys.executable
PODMAN_FAILED = 125  # what the image job reads as podman's own failure, not a verdict
SOON = 4  # seconds: far under any real timeout, far over a 1 s wait
SLOW = 8
RETRIES = 2  # 503s before the fake server comes up
MIN_WAIT = 0.9
INIT_MSG = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
NOTE_MSG = {"jsonrpc": "2.0", "method": "notifications/initialized"}
LIST_MSG = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
INIT_OK = {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "demo", "version": "1"}}}
TOOLS_OK = {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "t"}]}}

ECHO_SERVER = r"""
import json, sys
print("starting up, not json", flush=True)
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    print("a log line mid-stream", flush=True)
    print(json.dumps({"jsonrpc": "2.0", "id": "str-id", "result": "ignored"}), flush=True)
    result = {"serverInfo": {"name": "fake"}} if m["method"] == "initialize" else {"tools": [{}]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": result}), flush=True)
"""
SILENT_SERVER = "import time; time.sleep(30)"
DYING_SERVER = "import sys; sys.stderr.write('boom'); sys.exit(125)"


def test_converse_all_replies() -> None:
    replies, why, code = Tools().converse(
        [PY, "-c", ECHO_SERVER], [INIT_MSG, NOTE_MSG, LIST_MSG], timeout=5
    )
    assert why == "", why
    assert sorted(replies) == [1, 2], replies  # the "str-id" reply and log lines are skipped
    assert replies[1]["result"]["serverInfo"]["name"] == "fake"
    assert replies[2]["result"]["tools"] == [{}]
    assert code is None, code  # still running when it was killed


def test_converse_notification_does_not_wait() -> None:
    start = time.monotonic()
    replies, why, code = Tools().converse([PY, "-c", SILENT_SERVER], [NOTE_MSG], timeout=5)
    assert (replies, why) == ({}, ""), (replies, why)
    assert code is None
    assert time.monotonic() - start < SOON


def test_converse_silent_server_times_out() -> None:
    start = time.monotonic()
    replies, why, code = Tools().converse([PY, "-c", SILENT_SERVER], [INIT_MSG], timeout=1)
    took = time.monotonic() - start
    assert replies == {}
    assert "no reply to initialize within 1s" in why, why
    assert code is None, code
    assert MIN_WAIT <= took < SOON, took


def test_converse_early_exit_reports_stderr_and_code() -> None:
    replies, why, code = Tools().converse([PY, "-c", DYING_SERVER], [INIT_MSG], timeout=5)
    assert replies == {}
    assert "exited" in why and "boom" in why, why
    assert code == PODMAN_FAILED, code


def test_converse_broken_pipe() -> None:
    # The server closes stdin and lingers. A message bigger than the pipe buffer cannot be
    # written whether the child closes before or during the write: it ends in EPIPE.
    script = (
        "import os, sys, time; sys.stderr.write('early\\n'); sys.stderr.flush();"
        " os.close(0); time.sleep(1)"
    )
    big = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"pad": "x" * 1_000_000}}
    replies, why, _code = Tools().converse([PY, "-c", script], [big], timeout=3)
    assert replies == {}
    assert why.startswith("the server exited before initialize"), why
    assert "early" in why, why


def test_converse_on_directly() -> None:
    with tempfile.TemporaryFile(mode="w+") as errlog:
        proc = subprocess.Popen(
            [PY, "-c", ECHO_SERVER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errlog,
            text=True,
        )
        try:
            replies, why = converse_on(proc, [INIT_MSG, NOTE_MSG, LIST_MSG], 5, errlog)
        finally:
            proc.kill()
            proc.wait()
    assert why == "" and sorted(replies) == [1, 2], (why, replies)


def feed(*items: str | None) -> queue.Queue[str | None]:
    q: queue.Queue[str | None] = queue.Queue()
    for item in items:
        q.put(item)
    return q


def test_await_reply_after_log_line() -> None:
    replies: dict[int, dict] = {}
    q = feed("just a log\n", json.dumps(INIT_OK) + "\n")
    why = await_reply(q, INIT_MSG, replies, time.monotonic() + 5, 5)
    assert why == "" and replies[1] == INIT_OK, (why, replies)


def test_await_reply_eof() -> None:
    why = await_reply(feed("log\n", None), INIT_MSG, {}, time.monotonic() + 5, 5)
    assert why == "the server exited without answering initialize", why


def test_await_reply_deadline() -> None:
    start = time.monotonic()
    why = await_reply(feed(), INIT_MSG, {}, time.monotonic() + 0.3, 7)
    assert why == "no reply to initialize within 7s", why
    assert time.monotonic() - start < SOON


def test_run_timeout() -> None:
    start = time.monotonic()
    proc = Tools().run([PY, "-c", "import time; time.sleep(5)"], timeout=1)
    assert proc.returncode == TIMED_OUT, proc.returncode
    assert "timed out" in proc.stderr
    assert time.monotonic() - start < SOON


def test_run_timeout_keeps_what_the_process_said() -> None:
    script = (
        "import sys, time; print('partial out', flush=True); "
        "sys.stderr.write('partial err'); sys.stderr.flush(); time.sleep(5)"
    )
    proc = Tools().run([PY, "-c", script], timeout=1)
    assert proc.returncode == TIMED_OUT
    assert proc.stdout == "partial out\n", repr(proc.stdout)
    assert proc.stderr == "partial err\ntimed out after 1s", repr(proc.stderr)


def test_text_of_every_shape_timeout_expired_keeps() -> None:
    assert text_of(b"caf\xc3\xa9") == "caf\u00e9"
    assert text_of(b"\xff") == "\ufffd"
    assert text_of("already text") == "already text"
    assert text_of(None) == ""


class Server:
    """A real HTTP server on 127.0.0.1:0 that records what it saw and answers by script."""

    def __init__(self, behave: Callable[[Server, dict, str | None], tuple[int, dict | None, dict]]):
        self.seen: list[tuple[str, str | None]] = []
        self.hits = 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                sid = self.headers.get("Mcp-Session-Id")
                outer.seen.append((body.get("method", ""), sid))
                outer.hits += 1
                status, payload, headers = behave(outer, body, sid)
                data = b"" if payload is None else json.dumps(payload).encode()
                if headers.get("Content-Type") == "text/event-stream":
                    data = b"event: message\ndata: " + data + b"\n\n"
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args: object) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]


@contextmanager
def serving(
    behave: Callable[[Server, dict, str | None], tuple[int, dict | None, dict]],
) -> Iterator[Server]:
    server = Server(behave)
    thread = threading.Thread(target=server.httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()


def client(port: int, timeout: int = 10) -> tuple[subprocess.CompletedProcess[str], dict]:
    proc = subprocess.run(
        [
            PY,
            "-I",
            "-c",
            _boot.HTTP_CLIENT,
            str(port),
            "/mcp",
            str(timeout),
            json.dumps(_boot.initialize()),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    return proc, json.loads(proc.stdout.strip().splitlines()[-1])


def plain(_srv: Server, body: dict, _sid: str | None) -> tuple[int, dict | None, dict]:
    json_type = {"Content-Type": "application/json"}
    if "id" not in body:
        return 202, None, {}
    reply = INIT_OK if body["method"] == "initialize" else TOOLS_OK
    return 200, reply, json_type


def test_http_plain_json() -> None:
    with serving(plain) as srv:
        _proc, got = client(srv.port)
    assert got["init"] == INIT_OK and got["tools"] == TOOLS_OK, got
    assert [m for m, _ in srv.seen] == ["initialize", "notifications/initialized", "tools/list"]
    assert _boot.judge_tools(got["init"], got["tools"]) == ""


def test_http_event_stream_and_session_id() -> None:
    def sse(_srv: Server, body: dict, _sid: str | None) -> tuple[int, dict | None, dict]:
        if "id" not in body:
            return 202, None, {"Mcp-Session-Id": "sess-42"}
        reply = INIT_OK if body["method"] == "initialize" else TOOLS_OK
        return 200, reply, {"Content-Type": "text/event-stream", "Mcp-Session-Id": "sess-42"}

    with serving(sse) as srv:
        _proc, got = client(srv.port)
    assert got["init"] == INIT_OK and got["tools"] == TOOLS_OK, got
    assert srv.seen[0] == ("initialize", None), srv.seen
    assert srv.seen[1:] == [("notifications/initialized", "sess-42"), ("tools/list", "sess-42")]
    assert _boot.judge_tools(got["init"], got["tools"]) == ""


def test_http_retries_503_until_ready() -> None:
    def flaky(srv: Server, body: dict, sid: str | None) -> tuple[int, dict | None, dict]:
        if srv.hits <= RETRIES:
            return 503, {"error": "starting"}, {"Content-Type": "application/json"}
        return plain(srv, body, sid)

    with serving(flaky) as srv:
        _proc, got = client(srv.port, timeout=20)
    assert got["init"] == INIT_OK and got["tools"] == TOOLS_OK, got
    assert srv.seen[0][0] == srv.seen[1][0] == srv.seen[2][0] == "initialize", srv.seen


def test_http_nothing_listening() -> None:
    with serving(plain) as srv:
        port = srv.port
    start = time.monotonic()
    _proc, got = client(port, timeout=1)
    assert "never answered initialize" in got["error"], got
    assert set(got) == {"error"}
    assert time.monotonic() - start < SLOW


def test_http_tools_list_500() -> None:
    def broken(srv: Server, body: dict, sid: str | None) -> tuple[int, dict | None, dict]:
        if body.get("method") == "tools/list":
            return 500, {"error": "kaboom"}, {"Content-Type": "application/json"}
        return plain(srv, body, sid)

    with serving(broken) as srv:
        _proc, got = client(srv.port)
    assert got["error"].startswith("after initialize: "), got
    assert "HTTP 500" in got["error"], got


def test_judge_tools_findings() -> None:
    assert _boot.judge_tools(INIT_OK, {"result": {"tools": []}}) != ""
    assert "no tools" in _boot.judge_tools(INIT_OK, {"result": {"tools": []}})
    assert _boot.judge_tools(INIT_OK, None) != ""
    assert "initialize failed" in _boot.judge_tools(None, TOOLS_OK)


def main() -> int:
    tests: list[Callable[[], None]] = [v for k, v in globals().items() if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"ok   {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
