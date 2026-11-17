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
    # Windows cannot mount a workspace under the OS temporary directory, and
    # the run directory is the server's HOME, under which the runtime's unix
    # sockets must stay within macOS's 103 bytes: keep the path short.
    root = device_environment.home / ".gbb"
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="", dir=root) as directory:
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


def test_member_git_runs_the_clone_inside_the_command(stack, checkout):
    source = r"""
import json
from pathlib import Path
import subprocess

from guildbotics.guest.host_client import command_window

async def main():
    client = command_window()
    assert client is not None
    async def git(*args, stdin=""):
        result = await client.acall(
            "member", arguments=["git", *args, "--person", "blackbox", "--format", "json"],
            stdin=stdin,
        )
        assert result["exit_code"] == 0, result["stderr"]
        return json.loads(result["stdout"])

    prepared = await git("prepare", "--repo", "acme/demo", "--branch", "probe")
    clone = Path(prepared["repo_path"])
    assert (clone / "README.md").read_text() == "base\n"
    hook = clone / ".git/hooks/pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        '#!/bin/sh\n'
        'test -d /opt/guildbotics/code/guildbotics || exit 1\n'
        'echo inside > "$(git rev-parse --git-dir)/real-device-hook"\n'
    )
    hook.chmod(0o755)
    (clone / "probe").write_text("isolated commit")
    subprocess.run(["git", "add", "probe"], cwd=clone, check=True, timeout=30)
    published = await git("publish", "--repo-path", str(clone), "--content-stdin", stdin="probe")
    assert published["pushed"] is True
    assert (clone / ".git/real-device-hook").read_text().strip() == "inside"
    assert subprocess.check_output(
        ["git", "rev-parse", "refs/remotes/origin/probe"], cwd=clone, text=True, timeout=30,
    ).strip() == published["commit_sha"]
    return published["commit_sha"]
"""
    (stack.workspace / ".guildbotics/config/commands/probe.py").write_text(
        source, encoding="utf-8"
    )
    head = stack.cli("run", "probe", "--person", PERSON, timeout=180).strip()
    assert checkout.git("ls-remote", "origin", "refs/heads/probe").split()[0] == head
    checkout.git("fetch", "origin", "probe")
    assert (
        checkout.git("show", "-s", "--format=%ae", head) == "blackbox@guildbotics.local"
    )
    assert checkout.git("show", f"{head}:probe") == "isolated commit"
