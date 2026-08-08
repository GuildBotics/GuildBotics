"""Tests for the tracking issue lifecycle shared by the dependency digests."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest
import tracking_issue

PREFIX = "<!-- example-digest"


class FakeGh:
    """Record gh invocations and answer the reads the reconciler performs."""

    def __init__(self, issues: list[dict[str, Any]] | None = None) -> None:
        self.issues = issues or []
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args: list[str], stdin: str | None = None) -> str:
        self.calls.append((list(args), stdin))
        if list(args[:2]) == ["issue", "list"]:
            return json.dumps(self.issues)
        if list(args[:2]) == ["issue", "create"]:
            return "https://github.com/o/r/issues/9\n"
        return ""

    def subcommands(self) -> list[str]:
        """Return the gh subcommand of every recorded call."""
        return [" ".join(args[:2]) for args, _ in self.calls]

    def stdin_for(self, subcommand: str) -> str:
        """Return the body piped to the single call for the given subcommand."""
        bodies = [
            stdin
            for args, stdin in self.calls
            if " ".join(args[:2]) == subcommand and stdin is not None
        ]
        assert len(bodies) == 1, f"expected one {subcommand} call, got {len(bodies)}"
        return bodies[0]


@pytest.fixture
def fake_gh(monkeypatch: pytest.MonkeyPatch):
    """Install a FakeGh factory that replaces tracking_issue.gh."""

    def install(issues: list[dict[str, Any]] | None = None) -> FakeGh:
        gh = FakeGh(issues)
        monkeypatch.setattr(tracking_issue, "gh", gh)
        return gh

    return install


def reconcile(**overrides: Any) -> int:
    """Call reconcile with defaults that individual tests override."""
    kwargs: dict[str, Any] = {
        "prefix": PREFIX,
        "title": "Example digest",
        "body": "body text",
        "item_ids": ["a", "b"],
        "close_comment": "closing",
    }
    kwargs.update(overrides)
    return tracking_issue.reconcile("o/r", **kwargs)


def test_gh_returns_stdout_when_the_command_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="[]\n", stderr="")

    monkeypatch.setattr(tracking_issue.subprocess, "run", fake_run)

    assert tracking_issue.gh(["api", "/repos/o/r"]) == "[]\n"


def test_gh_failure_reports_the_command_and_the_stderr_of_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gh puts API errors such as a missing permission only on stderr."""

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 1, stdout="", stderr="HTTP 403: Resource not accessible\n"
        )

    monkeypatch.setattr(tracking_issue.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as error:
        tracking_issue.gh(["api", "/repos/o/r/dependabot/alerts"])

    message = str(error.value)
    assert "gh api /repos/o/r/dependabot/alerts" in message
    assert "HTTP 403: Resource not accessible" in message


def test_gh_failure_without_stderr_still_reports_the_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 2, stdout="", stderr="")

    monkeypatch.setattr(tracking_issue.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="status 2: \\(no stderr\\)"):
        tracking_issue.gh(["issue", "list"])


def test_marker_round_trips_through_the_rendered_body() -> None:
    marker = tracking_issue.build_marker(PREFIX, ["npm:react", "pip:agno"])

    assert tracking_issue.extract_item_ids(marker, PREFIX) == {"npm:react", "pip:agno"}


def test_extract_item_ids_returns_empty_without_a_usable_marker() -> None:
    assert tracking_issue.extract_item_ids("hand written body", PREFIX) == set()
    assert tracking_issue.extract_item_ids(f"{PREFIX} ids= -->", PREFIX) == set()


def test_find_issue_matches_only_the_body_carrying_the_marker(fake_gh) -> None:
    marked = {"number": 3, "body": f"{PREFIX} ids=a -->\n\nbody"}
    gh = fake_gh(
        [
            {"number": 1, "body": "unrelated issue"},
            {"number": 2, "body": None},
            marked,
        ]
    )

    assert tracking_issue.find_issue("o/r", PREFIX) == marked
    assert gh.subcommands() == ["issue list"]


def test_no_items_and_no_issue_writes_nothing(fake_gh) -> None:
    gh = fake_gh([])

    assert reconcile(item_ids=[]) == 0
    assert gh.subcommands() == ["issue list"]


def test_no_items_closes_the_existing_issue(fake_gh) -> None:
    gh = fake_gh([{"number": 7, "body": f"{PREFIX} ids=a -->"}])

    assert reconcile(item_ids=[]) == 0
    assert gh.subcommands() == ["issue list", "issue close"]
    close_args = gh.calls[1][0]
    assert "7" in close_args
    assert close_args[close_args.index("--reason") + 1] == "completed"
    assert close_args[close_args.index("--comment") + 1] == "closing"


def test_items_without_an_existing_issue_creates_one_with_the_marker(fake_gh) -> None:
    gh = fake_gh([])

    assert reconcile() == 0
    assert gh.subcommands() == ["issue list", "issue create"]
    body = gh.stdin_for("issue create")
    assert body.startswith(f"{PREFIX} ids=a,b -->")
    assert body.endswith("body text")
    create_args = gh.calls[1][0]
    assert create_args[create_args.index("--title") + 1] == "Example digest"


def test_already_reported_items_update_the_body_without_commenting(fake_gh) -> None:
    gh = fake_gh([{"number": 7, "body": f"{PREFIX} ids=a,b -->\n\nold body"}])

    assert reconcile(on_new_items=lambda ids: "should not be posted") == 0
    assert gh.subcommands() == ["issue list", "issue edit"]
    assert gh.stdin_for("issue edit").endswith("body text")


def test_newly_appeared_items_also_post_a_comment(fake_gh) -> None:
    gh = fake_gh([{"number": 7, "body": f"{PREFIX} ids=a -->\n\nold body"}])
    seen: list[set[str]] = []

    def on_new_items(ids: set[str]) -> str:
        seen.append(ids)
        return "b is new"

    assert reconcile(on_new_items=on_new_items) == 0
    assert gh.subcommands() == ["issue list", "issue edit", "issue comment"]
    assert seen == [{"b"}]
    assert gh.stdin_for("issue comment") == "b is new"


def test_new_items_without_a_renderer_do_not_comment(fake_gh) -> None:
    gh = fake_gh([{"number": 7, "body": f"{PREFIX} ids=a -->"}])

    assert reconcile(on_new_items=None) == 0
    assert gh.subcommands() == ["issue list", "issue edit"]


@pytest.mark.parametrize(
    "issues",
    [
        pytest.param([], id="would-create"),
        pytest.param([{"number": 7, "body": f"{PREFIX} ids=a -->"}], id="would-update"),
    ],
)
def test_dry_run_performs_no_writes(fake_gh, issues: list[dict[str, Any]]) -> None:
    gh = fake_gh(issues)

    assert reconcile(dry_run=True) == 0
    assert gh.subcommands() == ["issue list"]


def test_dry_run_does_not_close_an_issue(fake_gh) -> None:
    gh = fake_gh([{"number": 7, "body": f"{PREFIX} ids=a -->"}])

    assert reconcile(item_ids=[], dry_run=True) == 0
    assert gh.subcommands() == ["issue list"]
