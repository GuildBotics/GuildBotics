"""The member CLI run inside the host process, as the member broker runs it."""

from __future__ import annotations

import importlib
import os
import re
import threading
import typing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import click
import pytest

from guildbotics.capabilities.member_reference import capability_reference_text
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.runtime.member_invocation import (
    ChatSubject,
    MemberInvocation,
    Work,
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


def test_help_run_in_process_matches_the_cli(monkeypatch) -> None:
    """The member group runs without the root CLI whose settings it would
    otherwise inherit, such as showing each option's default."""
    from click.testing import CliRunner

    from guildbotics.cli import main

    monkeypatch.setattr(main, "callback", None)
    arguments = ["repository", "read", "--help"]
    through_cli = CliRunner().invoke(
        main, ["member", *arguments], prog_name="guildbotics"
    )

    exit_code, stdout, _stderr = _run(arguments)

    assert exit_code == 0
    assert "[default: json]" in stdout
    # CliRunner forces its own width, so the text wraps differently; compare
    # what the settings decide instead.
    defaults = re.compile(r"\[default: [^\]]+\]")
    assert defaults.findall(" ".join(stdout.split())) == defaults.findall(
        " ".join(through_cli.output.split())
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--dest", "a-file"], "is a file"),
        (["--dest", "missing-dir"], None),
    ],
)
def test_path_checks_read_from_the_commands_directory(
    monkeypatch, tmp_path, arguments, message
) -> None:
    """Path options keep their checks, made against the command's directory
    rather than the process's."""
    (tmp_path / "a-file").write_text("", encoding="utf-8")
    seen: list[Path] = []

    async def github(person, operation):
        return operation(
            SimpleNamespace(
                artifact_download=lambda _u, _n, d: seen.append(d) or {"dest": str(d)}
            )
        )

    monkeypatch.setattr(member_module, "_github", github)
    command = ["github", "run", "artifact", "download", "--person", "aiko"]
    command += ["--url", "https://example.test/pr/1", "--name", "logs", *arguments]

    exit_code, _stdout, stderr = _run(
        command, MemberInvocation(task_run_id="run-1"), cwd=tmp_path
    )

    if message:
        assert exit_code == 2
        assert message in stderr
        assert seen == []
    else:
        assert (exit_code, seen) == (0, [tmp_path / "missing-dir"])


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


_SUMMARY = ["--content-stdin"]
_CHAT = Work.of_chat(ChatSubject("slack", "C1", "100.1", "E1", "U_BOT"))
_TICKET = Work.of_ticket("https://github.com/owner/repo/issues/1")
#: The invocation of a run of each kind of workflow work, as its host makes it.
_RUNS = {
    "chat": MemberInvocation(run_id="run-1", work=_CHAT),
    "ticket": MemberInvocation(task_run_id="run-1", work=_TICKET),
}
#: The commands that write a run's record, with the kind of run they act on.
_RUN_RECORD_WRITES = [
    (["chat", "noop", "--person", "aiko", *_SUMMARY], "chat"),
    (
        ["chat", "complete", "--person", "aiko", "--status", "blocked", *_SUMMARY],
        "chat",
    ),
    (
        ["task", "complete", "--person", "aiko", "--status", "blocked", *_SUMMARY],
        "ticket",
    ),
]


@pytest.mark.parametrize(("arguments", "kind"), _RUN_RECORD_WRITES)
@pytest.mark.parametrize(
    ("lease_person", "runs"),
    [(None, False), ("yuki", False), ("aiko", True)],
)
def test_a_run_record_is_written_only_under_the_turns_lease(
    monkeypatch, tmp_path, arguments, kind, lease_person, runs
) -> None:
    """A read-only command's turn holds no lease, so the guard refuses every
    write of it, these that record the run's outcome among them."""
    monkeypatch.setattr(
        member_module,
        "_resolve",
        lambda person: (None, SimpleNamespace(person_id=person)),
    )
    monkeypatch.setattr(member_module, "prepare_commit_and_push_once", lambda: None)
    lease = PersonExecutionLease(lease_person, tmp_path) if lease_person else None

    exit_code, stdout, stderr = _run(
        arguments,
        replace(_RUNS[kind], lease=lease),
        cwd=tmp_path,
        stdin="Nothing to do.",
    )

    if runs:
        assert (exit_code, stderr) == (0, "")
        [record] = RunStore().records()
        assert record.run_id == "run-1"
        # The subject is the run's work, which no argument names.
        if record.result is None:
            assert record.provider_evidence[-1]["payload"]["event_id"] == "E1"
        else:
            assert record.result.subject_id == (
                "slack:C1:100.1:E1" if kind == "chat" else _TICKET.identity
            )
    else:
        assert (exit_code, stdout) == (1, "")
        assert t("cli.member.lease.invalid_delegation") in " ".join(stderr.split())
        assert list(RunStore().records()) == []


@pytest.mark.parametrize(
    ("arguments", "kind"),
    [
        *_RUN_RECORD_WRITES,
        (["chat", "updates", "--person", "aiko"], "chat"),
        (["task", "status", "--person", "aiko"], "ticket"),
    ],
)
def test_a_run_command_acts_only_on_its_invocations_run_and_work(
    tmp_path, arguments, kind
) -> None:
    """The run and its subject are the invocation's, never ones the command
    names, so a call whose invocation carries no run doing the command's kind
    of work is refused."""
    other = "ticket" if kind == "chat" else "chat"
    run = _RUNS[kind]

    for invocation in (
        MemberInvocation(),
        _RUNS[other],
        replace(run, work=None),
        replace(run, work=_RUNS[other].work),
        replace(run, work=Work("manual", "run-1")),
    ):
        exit_code, stdout, stderr = _run(
            arguments,
            invocation,
            cwd=tmp_path,
            stdin="Nothing to do.",
        )

        assert (exit_code, stdout) == (1, "")
        assert t("cli.member.run.required", kind=kind) in " ".join(stderr.split())
    assert list(RunStore().records()) == []


def _leaf_commands(group):
    for command in group.commands.values():
        if isinstance(command, click.Group):
            yield from _leaf_commands(command)
        else:
            yield command


def test_every_member_command_returns_its_work_to_the_member_guard() -> None:
    """What a command does is the work its callback returns, which runs only
    once the guard admits it: a callback acting itself would act unguarded.
    ``help`` only prints the reference."""
    unguarded = [
        command.name
        for command in _leaf_commands(member_module.member)
        if command.name != "help"
        and (
            not isinstance(command, member_module._MemberCommand)
            or typing.get_type_hints(command.callback).get("return")
            != member_module._CommandWork
        )
    ]

    assert unguarded == []


def test_no_member_command_names_its_run() -> None:
    """The run is the invocation's, so no command takes one."""
    assert [
        command.name
        for command in _leaf_commands(member_module.member)
        if any(param.name == "run_id" for param in command.params)
    ] == []


def test_a_command_of_an_environment_has_the_host_read_none_of_its_files(
    monkeypatch, tmp_path
) -> None:
    """What the environment can write it can swap for a link to any of the
    host's files between a check and the read, so the host reads none of
    it, whether or not it is there: the content comes on standard input."""
    _record_git_commit(monkeypatch)
    (tmp_path / "message.txt").write_text("the host's own", encoding="utf-8")
    base = ["git", "commit", "--person", "aiko", "--repo-path", "repo"]
    of_an_environment = MemberInvocation(task_run_id="run-1", guest=SimpleNamespace())

    refused = [
        _run([*base, "--content-file", name], of_an_environment, cwd=tmp_path)
        for name in ("message.txt", str(tmp_path / "message.txt"), "missing.txt")
    ]
    from_stdin = _run(
        [*base, "--content-stdin"], of_an_environment, cwd=tmp_path, stdin="given"
    )

    for exit_code, stdout, stderr in refused:
        assert (exit_code, stdout) == (2, "")
        assert t("cli.member.content.file_in_environment") in " ".join(stderr.split())
        assert "the host's own" not in stderr
    assert member_module.json.loads(from_stdin[1])["message"] == "given"


def test_a_command_of_an_environment_names_paths_the_host_does_not_look_at(
    monkeypatch, tmp_path
) -> None:
    """Checking one on the host would look at what the environment can
    write; what it names goes on as a name, for the environment to use."""
    (tmp_path / "a-file").write_text("", encoding="utf-8")
    seen: list[Path] = []

    async def github(person, operation):
        return operation(
            SimpleNamespace(artifact_download=lambda _u, _n, d: seen.append(d) or {})
        )

    monkeypatch.setattr(member_module, "_github", github)
    looked: list[str] = []
    for name in ("stat", "lstat"):
        original = getattr(os, name)

        def look(path, *args, original=original, **kwargs):
            if str(path).startswith(str(tmp_path)):
                looked.append(str(path))
            return original(path, *args, **kwargs)

        monkeypatch.setattr(os, name, look)

    exit_code, _stdout, stderr = _run(
        ["github", "run", "artifact", "download", "--person", "aiko"]
        + ["--url", "https://example.test/pr/1", "--name", "logs"]
        + ["--dest", "a-file"],
        MemberInvocation(task_run_id="run-1", guest=SimpleNamespace()),
        cwd=tmp_path,
    )

    assert (exit_code, stderr, seen) == (0, "", [tmp_path / "a-file"])
    assert looked == []


def test_outside_a_turn_a_command_opens_what_it_is_named(monkeypatch, tmp_path) -> None:
    _record_git_commit(monkeypatch)
    (tmp_path / "message.txt").write_text("the user's own", encoding="utf-8")

    exit_code, stdout, _stderr = _run(
        ["git", "commit", "--person", "aiko", "--repo-path", "repo"]
        + ["--content-file", str(tmp_path / "message.txt")],
        MemberInvocation(task_run_id="run-1"),
        cwd=tmp_path / "elsewhere",
    )

    assert exit_code == 0
    assert member_module.json.loads(stdout)["message"] == "the user's own"


@pytest.mark.parametrize("invocation", ["task run", "chat run", "no run"])
def test_a_command_of_an_environment_runs_no_git_on_the_host(
    monkeypatch, tmp_path, invocation
) -> None:
    """The current mode runs git on the host in a repository the command's
    environment can write, whatever run the command belongs to, if any."""
    monkeypatch.setattr(member_module, "_member_command_needs_lease", lambda: False)
    guest = SimpleNamespace()
    of_an_environment = {
        "task run": MemberInvocation(task_run_id="run-1", guest=guest),
        "chat run": MemberInvocation(run_id="run-1", guest=guest),
        "no run": MemberInvocation(guest=guest),
    }[invocation]

    exit_code, _stdout, stderr = _run(
        ["git", "commit", "--person", "aiko", "--repo-path", "."]
        + ["--content-stdin", "--workspace-mode", "current"],
        of_an_environment,
        cwd=tmp_path,
        stdin="message",
    )

    assert exit_code == 1
    assert "only for interactive use" in stderr
