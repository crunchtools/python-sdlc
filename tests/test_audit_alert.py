#!/usr/bin/env python3
"""The CVE alert lifecycle in sdlc_jobs/_audit_alert.py.

Vendored with the module from crunchtools/constitution at 7afa748^ (its
tests/test_audit_alert.py), moved from pytest to this repo's script style.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdlc_jobs import _audit_alert as alert

CLEAN = {"dependencies": [{"name": "httpx", "version": "0.28.1", "vulns": []}]}
DIRTY = {
    "dependencies": [
        {"name": "httpx", "version": "0.28.1", "vulns": []},
        {"name": "jinja2", "version": "3.1.4", "vulns": [{"id": "GHSA-x"}]},
    ]
}


def issue(number: int, title: str = alert.TITLE, login: str = "app/github-actions") -> dict:
    return {"number": number, "title": title, "author": {"login": login}}


def raises(exc: type[BaseException], fn: Callable[[], Any]) -> bool:
    try:
        fn()
    except exc:
        return True
    return False


def test_a_dict_report_is_counted() -> None:
    """Twelve old copies iterated the report as a list and always read zero."""
    assert [dep["name"] for dep in alert.vulnerable(DIRTY)] == ["jinja2"]
    assert alert.vulnerable(CLEAN) == []


def test_a_malformed_report_raises() -> None:
    assert raises(KeyError, lambda: alert.vulnerable({"vulns": []}))


def test_only_this_workflows_alerts_are_managed() -> None:
    """A person's issue, or another bot's, is never edited or closed."""
    mine = issue(96, "Security Alert: CVEs found in dependencies (2026-10-26)")
    found = alert.alerts([issue(5, login="smccarty"), issue(7, "Bump jinja2"), mine, issue(3)])
    assert [i["number"] for i in found] == [3, 96]


def test_the_apps_own_issues_are_managed_once_author_is_set() -> None:
    with mock.patch.object(alert, "AUTHOR", "app/ashigaru-crunchtools-com"):
        found = alert.alerts(
            [issue(4, login="app/ashigaru-crunchtools-com"), issue(5, login="bob")]
        )
    assert [i["number"] for i in found] == [4]


def test_pull_request_fails_on_a_vulnerability() -> None:
    assert alert.plan("pull_request", alert.vulnerable(DIRTY), [issue(1)]) == [("fail", None)]
    assert alert.plan("pull_request", [], [issue(1)]) == []


def test_clean_scan_closes_every_open_alert() -> None:
    assert alert.plan("push", [], [issue(3), issue(96)]) == [("close", 3), ("close", 96)]


def test_vulnerable_scan_creates_one_alert() -> None:
    for event in ("schedule", "push", "workflow_dispatch"):
        assert alert.plan(event, alert.vulnerable(DIRTY), []) == [("create", None)], event


def test_vulnerable_scan_rewrites_the_oldest_and_closes_duplicates() -> None:
    """The weekly copies opened a new dated issue every Monday."""
    found = alert.vulnerable(DIRTY)
    assert alert.plan("schedule", found, [issue(3), issue(9)]) == [
        ("edit", 3),
        ("close-duplicate", 9),
    ]


def test_other_events_do_nothing() -> None:
    assert alert.plan("merge_group", alert.vulnerable(DIRTY), []) == []


def test_body_links_the_run_and_lists_each_advisory() -> None:
    found = [
        {"name": "jinja2", "version": "3.1.4", "vulns": [{"id": "A", "fix_versions": ["3.1.5"]}]}
    ]
    text = alert.body(found, "https://github.com/x/y/actions/runs/1")
    assert "(https://github.com/x/y/actions/runs/1)" in text
    assert "| jinja2 | 3.1.4 | A | 3.1.5 |" in text
    # The fleet's dependency PRs come from Renovate; Dependabot is retired.
    assert "Renovate" in text and "Dependabot" not in text, text


def recorded(action: str, number: int | None) -> list[tuple[str, ...]]:
    made: list[tuple[str, ...]] = []
    with mock.patch.object(alert, "gh", lambda *args: made.append(args) or ""):
        alert.apply("o/r", action, number, "BODY", "RUN")
    return made


def test_create_makes_missing_labels_then_the_issue() -> None:
    calls = recorded("create", None)
    assert [c[:3] for c in calls[:2]] == [
        ("label", "create", "security"),
        ("label", "create", "dependencies"),
    ]
    assert calls[2] == (
        "issue",
        "create",
        "--repo",
        "o/r",
        "--title",
        alert.TITLE,
        "--label",
        "security,dependencies",
        "--body",
        "BODY",
    )


def test_edit_rewrites_title_and_body() -> None:
    assert recorded("edit", 3) == [
        ("issue", "edit", "3", "--repo", "o/r", "--title", alert.TITLE, "--body", "BODY")
    ]


def test_closes_carry_their_reason_and_comment() -> None:
    for action, reason, says in (
        ("close", "completed", "(RUN) found no vulnerable"),
        ("close-duplicate", "not planned", "Superseded"),
    ):
        ((*head, comment),) = recorded(action, 9)
        assert head == ["issue", "close", "9", "--repo", "o/r", "--reason", reason, "--comment"]
        assert says in comment


def test_an_existing_label_is_left_alone() -> None:
    tried: list[tuple[str, ...]] = []

    def fail(*args: str) -> str:
        tried.append(args)
        raise RuntimeError("gh label create: label with name already exists")

    with mock.patch.object(alert, "gh", fail):
        assert alert.ensure_label("o/r", "security") is None
    assert tried == [("label", "create", "security", "--repo", "o/r")]


def test_any_other_label_failure_raises() -> None:
    def fail(*_args: str) -> str:
        raise RuntimeError("gh label create: HTTP 403")

    with mock.patch.object(alert, "gh", fail):
        assert raises(RuntimeError, lambda: alert.ensure_label("o/r", "security"))


def main() -> int:
    failed = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as error:
                print(f"FAIL {name} -- {error!r}")
                failed.append(name)
    print(f"\n{len(failed)} failed: {', '.join(failed)}" if failed else "\nall passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
