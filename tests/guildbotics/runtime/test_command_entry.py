"""The command execution machinery's entry inside a command's environment.

Run here in the test's own process, or as the process the host starts: it
runs the file the host resolved, as the member the command's facts name,
with the member's services through the command's window, and says how the
command ended without recording anything itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from guildbotics.commands.metadata import CommandAccess
from guildbotics.integrations.window import WindowChatService
from guildbotics.intelligences.agent_runtime.host_client import (
    COMMAND_ENV,
    HOST_TOKEN_ENV,
    HOST_URL_ENV,
    CommandFacts,
    CommandReply,
    CommandRequest,
    HostClient,
)
from guildbotics.runtime import command_entry
from guildbotics.utils.fileio import GUILDBOTICS_CONFIG_DIR

_PROBE = """
from guildbotics.runtime.workflow_invocation import WORKFLOW_INVOCATION_KEY

def main(context, name):
    services = type(context.get_chat_service()).__name__
    invocation = context.shared_state[WORKFLOW_INVOCATION_KEY]
    return {
        "person": context.person.person_id,
        "pipe": context.pipe,
        "name": name,
        "services": services,
        "payload": invocation.payload,
    }
"""


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace of one member, aiko, as the environment mounts it."""
    config = tmp_path / "config"
    (config / "team" / "members" / "aiko").mkdir(parents=True)
    (config / "team" / "project.yml").write_text(
        "name: demo\nlanguage: en\n", encoding="utf-8"
    )
    (config / "team" / "members" / "aiko" / "person.yml").write_text(
        "person_id: aiko\nname: Aiko\nis_active: true\n", encoding="utf-8"
    )
    (config / "commands").mkdir()
    monkeypatch.setenv(GUILDBOTICS_CONFIG_DIR, str(config))
    return config


def _facts(tmp_path: Path) -> CommandFacts:
    return CommandFacts(
        person_id="aiko",
        run_id="run-1",
        work_kind="",
        trace_id="trace-1",
        access=CommandAccess(),
        mounts={tmp_path.as_posix(): True},
    )


@pytest.mark.asyncio
async def test_child_resolves_inside_existing_environment_and_uses_its_member(
    config_dir, tmp_path
):
    path = config_dir / "commands" / "probe.py"
    path.write_text(_PROBE, encoding="utf-8")
    facts = _facts(tmp_path)
    from dataclasses import replace

    facts = replace(facts, access=CommandAccess(read_only=True))
    reply = await command_entry.run(
        _request(tmp_path / "not-the-child.py", tmp_path, wants_result=True),
        facts,
        HostClient("http://window.test/host", "token"),
        child=True,
    )
    assert reply.failure is None
    assert reply.result["person"] == "aiko"
    assert reply.result["services"] == "WindowChatService"
    assert facts.access.read_only


@pytest.mark.asyncio
async def test_child_rejects_cwd_outside_main_mounts(config_dir, tmp_path):
    reply = await command_entry.run(
        _request(config_dir / "commands" / "probe.py", tmp_path.parent),
        _facts(tmp_path),
        HostClient("http://window.test/host", "token"),
        child=True,
    )
    from guildbotics.utils.i18n_tool import t

    assert reply.failure.command
    assert reply.failure.message == t(
        "intelligences.agent_environment.runtime.outside_mounts",
        path=tmp_path.parent.as_posix(),
    )


def test_child_cli_requires_existing_environment(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["command_entry", "repository/issue_inspect"])
    monkeypatch.delenv(HOST_URL_ENV, raising=False)
    monkeypatch.delenv(HOST_TOKEN_ENV, raising=False)
    with pytest.raises(SystemExit, match="running GuildBotics command"):
        command_entry.main()


def _request(path: Path, cwd: Path, **fields) -> CommandRequest:
    return CommandRequest(
        path=str(path),
        name="probe",
        args=["name=Aiko"],
        cwd=cwd.as_posix(),
        **{
            "pipe": "the input",
            "invocation": {
                "command": "probe",
                "person_id": "aiko",
                "source": "manual",
                "trigger_type": "generic",
                "payload": {"k": "v"},
            },
            **fields,
        },
    )


@pytest.mark.asyncio
async def test_the_entry_runs_the_file_the_host_resolved(tmp_path, config_dir):
    """Not a command it would resolve by the name: the file the host read,
    for the member and with the input and workflow run the host names, and
    the member's services through the command's window."""
    elsewhere = tmp_path / "read by the host" / "probe.py"
    elsewhere.parent.mkdir()
    elsewhere.write_text(_PROBE, encoding="utf-8")
    window = HostClient("http://window.test/host", "token")

    reply = await command_entry.run(
        _request(elsewhere, tmp_path, wants_result=True), _facts(tmp_path), window
    )
    unread = await command_entry.run(
        _request(elsewhere, tmp_path), _facts(tmp_path), window
    )

    assert reply.failure is None
    assert reply.result == {
        "person": "aiko",
        "pipe": "the input",
        "name": "Aiko",
        "services": WindowChatService.__name__,
        "payload": {"k": "v"},
    }
    # A result nobody reads does not cross.
    assert unread.result is None
    assert unread.text_output == reply.text_output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "command", "error_type", "cli_agent"),
    [
        (
            "from guildbotics.commands.errors import CommandError\n"
            "def main():\n    raise CommandError('refused')\n",
            True,
            "CommandError",
            "",
        ),
        ("def main():\n    raise ValueError('broken')\n", False, "ValueError", ""),
        (
            "from guildbotics.commands.errors import CommandError\n"
            "from guildbotics.intelligences.brains.cli_agent import (\n"
            "    CliAgentExecutionError, CliAgentExecutionResult,\n"
            ")\n"
            "def main():\n"
            "    result = CliAgentExecutionResult(\n"
            "        stdout='', stderr='slow down', returncode=1,\n"
            "        error_category='rate_limited',\n"
            "        error_details={'retry_after_at': '2026-09-27T10:00:00+09:00'},\n"
            "    )\n"
            "    try:\n"
            "        raise CliAgentExecutionError(cli_agent='codex', result=result)\n"
            "    except CliAgentExecutionError as exc:\n"
            "        raise CommandError('the turn failed') from exc\n",
            True,
            "CommandError",
            "codex",
        ),
    ],
)
async def test_a_failure_is_said_the_way_the_host_rebuilds_it(
    tmp_path, config_dir, body, command, error_type, cli_agent
):
    probe = config_dir / "commands" / "probe.py"
    probe.write_text(body, encoding="utf-8")

    reply = await command_entry.run(
        _request(probe, tmp_path),
        _facts(tmp_path),
        HostClient("http://window.test/host", "token"),
    )

    failure = reply.failure
    assert failure is not None
    assert (failure.command, failure.type, failure.cli_agent) == (
        command,
        error_type,
        cli_agent,
    )
    if cli_agent:
        assert failure.cli_agent_result is not None
        assert failure.cli_agent_result["error_category"] == "rate_limited"
        assert failure.cli_agent_result["error_details"] == {
            "retry_after_at": "2026-09-27T10:00:00+09:00"
        }


def test_the_entry_is_the_process_the_host_starts(tmp_path, config_dir):
    """As the host starts it: the command's facts in its environment, what to
    run on standard input, how it ended as the one line of standard output,
    and its log -- each line led by its level, and what the command prints --
    on standard error."""
    probe = config_dir / "commands" / "probe.py"
    probe.write_text(
        "import logging\n"
        "def main(context):\n"
        "    logging.getLogger('guildbotics').warning('careful')\n"
        "    print('printed')\n"
        "    return context.pipe.upper()\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        GUILDBOTICS_CONFIG_DIR: str(config_dir),
        HOST_URL_ENV: "http://window.test/host",
        HOST_TOKEN_ENV: "token",
        COMMAND_ENV: _facts(tmp_path).dump(),
    }

    finished = subprocess.run(
        [sys.executable, "-m", "guildbotics.runtime.command_entry"],
        input=_request(probe, tmp_path).model_dump_json(),
        capture_output=True,
        text=True,
        env=environment,
        cwd=tmp_path,
        check=True,
        timeout=60,
    )

    (line,) = finished.stdout.splitlines()
    reply = CommandReply.model_validate(json.loads(line))
    assert reply.failure is None
    assert reply.text_output == "THE INPUT"
    assert {"WARNING careful", "printed"} <= set(finished.stderr.splitlines())
