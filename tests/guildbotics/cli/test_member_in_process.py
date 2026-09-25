"""The member CLI run inside the host process, as the member broker runs it."""

from __future__ import annotations

import importlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from guildbotics.capabilities.member_reference import capability_reference_text
from guildbotics.runtime.member_invocation import (
    MemberInvocation,
    current_member_invocation,
)
from guildbotics.runtime.person_lease import PersonExecutionLease
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.shared_write_lock import SharedWriteBusyError

#: The module, not the ``member`` group ``guildbotics.cli`` re-exports by that name.
member_module = importlib.import_module("guildbotics.cli.member")


def _run(arguments, invocation=MemberInvocation(), *, cwd=Path("."), stdin=""):
    return member_module.run_in_process(arguments, invocation, cwd=cwd, stdin=stdin)


def test_output_goes_to_the_command_and_not_the_process(capsys) -> None:
    assert _run(["help"]) == (0, capability_reference_text() + "\n", "")
    assert capsys.readouterr() == ("", "")


def test_help_of_a_command_goes_to_the_command(capsys) -> None:
    exit_code, stdout, stderr = _run(["git", "commit", "--help"])

    assert exit_code == 0
    assert stdout.startswith("Usage: guildbotics member git commit [OPTIONS]")
    assert stderr == ""
    assert capsys.readouterr() == ("", "")


def test_a_usage_error_is_reported_the_way_the_cli_reports_it(capsys) -> None:
    exit_code, stdout, stderr = _run(["git", "commit", "--bogus"])

    assert exit_code == 2
    assert stdout == ""
    assert stderr.startswith("Usage: guildbotics member git commit [OPTIONS]")
    assert stderr.endswith("Error: No such option '--bogus'.\n")
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            SharedWriteBusyError("busy"),
            lambda: f"Error: {t('cli.shared.write_busy')}\n",
        ),
        (RuntimeError("boom"), lambda: "RuntimeError: boom\n"),
    ],
)
def test_a_failing_command_reports_like_the_cli(monkeypatch, error, expected) -> None:
    def fail() -> str:
        raise error

    monkeypatch.setattr(member_module, "capability_reference_text", fail)

    exit_code, stdout, stderr = _run(["help"])

    assert (exit_code, stdout) == (1, "")
    assert stderr.endswith(expected())


def test_the_command_does_not_select_the_workspace_again(monkeypatch) -> None:
    """The host process already runs in the workspace; re-applying it would
    rewrite the process's environment under every other command."""

    def apply(_workspace):
        raise AssertionError("the host's workspace was applied again")

    monkeypatch.setattr(member_module, "apply_workspace_option", apply)

    assert _run(["help"])[0] == 0


def _record_git_commit(monkeypatch) -> None:
    async def git_commit(person, repo_path, message, workspace_mode):
        return {
            "repo_path": str(repo_path),
            "message": message,
            "cwd": str(member_module._call().cwd),
        }

    monkeypatch.setattr(member_module, "_git_commit", git_commit)
    monkeypatch.setattr(member_module, "_member_command_needs_lease", lambda: False)


def test_relative_paths_and_content_are_the_commands_own(monkeypatch, tmp_path) -> None:
    _record_git_commit(monkeypatch)
    (tmp_path / "message.txt").write_text("from a file", encoding="utf-8")
    base = ["git", "commit", "--person", "aiko", "--repo-path", "repo"]

    from_file = _run(
        [*base, "--content-file", "message.txt"],
        MemberInvocation(task_run_id="run-1"),
        cwd=tmp_path,
    )
    from_stdin = _run(
        [*base, "--content-stdin"],
        MemberInvocation(task_run_id="run-1"),
        cwd=tmp_path,
        stdin="from stdin",
    )

    for (exit_code, stdout, _stderr), message in (
        (from_file, "from a file"),
        (from_stdin, "from stdin"),
    ):
        assert exit_code == 0
        assert member_module.json.loads(stdout) == {
            "repo_path": str(tmp_path / "repo"),
            "message": message,
            "cwd": str(tmp_path),
        }


def test_markdown_output_goes_to_the_command(monkeypatch, tmp_path, capsys) -> None:
    _record_git_commit(monkeypatch)

    exit_code, stdout, _stderr = _run(
        [
            *["git", "commit", "--person", "aiko", "--repo-path", "repo"],
            *["--content-stdin", "--format", "markdown"],
        ],
        MemberInvocation(task_run_id="run-1"),
        cwd=tmp_path,
        stdin="message",
    )

    assert exit_code == 0
    assert stdout.startswith(f"- **repo_path**: {tmp_path / 'repo'}\n")
    assert capsys.readouterr() == ("", "")


def test_commands_running_at_once_keep_their_own_invocation_and_output(
    monkeypatch,
) -> None:
    both_running = threading.Barrier(2, timeout=5)

    def reference() -> str:
        both_running.wait()
        return current_member_invocation().run_id

    monkeypatch.setattr(member_module, "capability_reference_text", reference)
    results: dict[str, tuple[int, str, str]] = {}

    def run(run_id: str) -> None:
        results[run_id] = _run(["help"], MemberInvocation(run_id=run_id))

    threads = [threading.Thread(target=run, args=(run_id,)) for run_id in "ab"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert results == {"a": (0, "a\n", ""), "b": (0, "b\n", "")}


@pytest.mark.parametrize(
    ("lease_person", "runs"),
    [(None, False), ("yuki", False), ("aiko", True)],
)
def test_a_workflow_command_writes_only_under_its_turns_lease(
    monkeypatch, tmp_path, lease_person, runs
) -> None:
    _record_git_commit(monkeypatch)
    monkeypatch.setattr(
        member_module,
        "_resolve",
        lambda person: (None, SimpleNamespace(person_id=person)),
    )
    monkeypatch.setattr(member_module, "_member_command_needs_lease", lambda: True)
    monkeypatch.setattr(member_module, "prepare_commit_and_push_once", lambda: None)
    lease = PersonExecutionLease(lease_person, tmp_path) if lease_person else None

    exit_code, stdout, stderr = _run(
        ["git", "commit", "--person", "aiko", "--repo-path", "repo", "--content-stdin"],
        MemberInvocation(task_run_id="run-1", lease=lease),
        cwd=tmp_path,
        stdin="message",
    )

    if runs:
        assert (exit_code, stderr) == (0, "")
    else:
        assert (exit_code, stdout) == (1, "")
        assert t("cli.member.lease.invalid_delegation") in stderr
