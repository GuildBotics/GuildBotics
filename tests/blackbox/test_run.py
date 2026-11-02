"""Bundled commands through CLI -> server -> a real microVM -> member broker."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from tests.blackbox.environment import EnvironmentSource
from tests.blackbox.harness import PERSON, running_stack
from tests.blackbox.test_code_host import create_pr, create_ticket

pytestmark = pytest.mark.real_device("GUILDBOTICS_E2E_ENVIRONMENT_FROM")


@pytest.fixture(scope="session")
def device_environment():
    # Session setup precedes the suite's per-test HOME redirection. Do not
    # inherit any of the device's credentials or member configuration.
    return EnvironmentSource(
        Path(os.environ["GUILDBOTICS_E2E_ENVIRONMENT_FROM"]).resolve(), Path.home()
    )


@pytest.fixture
def stack(device_environment):
    # Windows cannot mount a workspace under the OS temporary directory.
    root = device_environment.home / ".guildbotics-blackbox"
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="run-", dir=root) as directory:
        with running_stack(Path(directory), environment=device_environment) as stack:
            status = json.loads(stack.cli("environment", "status", "--format", "json"))
            assert status["refusal"] == "", status["refusal"]
            assert status["snapshot"]["state"] == "ready"
            yield stack


def test_bundled_inspections_run_in_the_environment(stack, repository):
    url = create_ticket(stack, "隔離環境から読むチケット")
    pr_url = create_pr(stack, "--issue-url", url)
    stack.member("ticket", "comment", "--url", url, content="broker 越しの確認。")
    output = stack.cli(
        "run",
        "--person",
        PERSON,
        "repository/issue_inspect",
        f"url={url}",
        timeout=180,
    )
    result = json.loads(output)
    assert result["url"] == url
    assert result["title"] == "隔離環境から読むチケット"
    assert result["body"] == "作成時の本文。"
    assert [comment["body"] for comment in result["comments"]] == [
        "broker 越しの確認。"
    ]
    assert result["attachments"] == []
    assert [pr["url"] for pr in result["linked_pull_request_candidates"]] == [pr_url]

    inspected = json.loads(
        stack.cli(
            "run",
            "--person",
            PERSON,
            "repository/pr_inspect",
            f"url={pr_url}",
            "include_comments=true",
            "include_diff=true",
            timeout=180,
        )
    )
    assert inspected["head_sha"] == repository
    assert inspected["conversation_comments"] == []
    assert inspected["review_threads"] == []
    assert inspected["diff_complete"] is True
    assert [file["path"] for file in inspected["files"]] == ["README.md"]
    assert "+feature" in inspected["files"][0]["patch"]

    stack.provider.finish_checks(repository)
    checked = json.loads(
        stack.cli(
            "run",
            "--person",
            PERSON,
            "repository/pr_checks",
            f"url={pr_url}",
            timeout=180,
        )
    )
    assert checked["head_sha"] == checked["current_head_sha"] == repository
    assert checked["readiness"] == "ready"
    assert checked["completion_blockers"] == []
