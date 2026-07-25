import textwrap
from pathlib import Path

import click
import pytest

import guildbotics.cli as cli_module
from guildbotics.cli import _parse_command_spec
from guildbotics.commands.errors import CommandError
from guildbotics.drivers.command_runner import (
    CommandRunner,
    PersonExecutionNotAllowedError,
    PersonNotFoundError,
    PersonSelectionRequiredError,
    run_command,
)
from guildbotics.entities.team import Person, Project, Team
from guildbotics.intelligences.functions import to_text
from guildbotics.runtime.context import Context
from guildbotics.runtime.member_context import resolve_person
from tests.guildbotics.runtime.test_context import (
    DummyBrainFactory,
    DummyIntegrationFactory,
    DummyLoaderFactory,
)


def test_command_runner_public_exports():
    import guildbotics.drivers.command_runner as command_runner

    assert "PersonNotFoundError" in command_runner.__all__
    assert "PersonExecutionNotAllowedError" in command_runner.__all__
    assert "PersonSelectionRequiredError" in command_runner.__all__


def test_parse_command_spec_with_person():
    name, person = _parse_command_spec("translate@yuki")
    assert name == "translate"
    assert person == "yuki"


def test_parse_command_spec_without_person():
    name, person = _parse_command_spec(" summarize ")
    assert name == "summarize"
    assert person is None


def _team(*members: Person, default_person_id: str = "") -> Team:
    return Team(
        project=Project(name="demo", default_person_id=default_person_id),
        members=list(members),
    )


def test_resolve_person_with_explicit_identifier():
    team = _team(
        Person(person_id="yuki", name="Yuki", is_active=True),
        Person(person_id="kato", name="Kato", is_active=False),
    )
    person = resolve_person(team, "Kato", allow_default=True)
    assert person.person_id == "kato"


def test_resolve_person_defaults_to_single_active():
    team = _team(Person(person_id="yuki", name="Yuki", is_active=True))
    person = resolve_person(team, None, allow_default=True)
    assert person.person_id == "yuki"


def test_resolve_person_defaults_to_first_candidate_without_configuration():
    team = _team(
        Person(person_id="yuki", name="Yuki", is_active=True),
        Person(person_id="akira", name="Akira", is_active=True),
        Person(person_id="aiko", name="Aiko", is_active=False),
        Person(person_id="ai", name="Ai", is_active=True, person_type="human"),
    )
    person = resolve_person(team, None, allow_default=True)
    assert person.person_id == "akira"


def test_resolve_person_uses_configured_default():
    team = _team(
        Person(person_id="yuki", name="Yuki", is_active=True),
        Person(person_id="akira", name="Akira", is_active=True),
        default_person_id="akira",
    )
    person = resolve_person(team, None, allow_default=True)
    assert person.person_id == "akira"


def test_resolve_person_prefers_explicit_identifier_over_default():
    team = _team(
        Person(person_id="yuki", name="Yuki", is_active=True),
        Person(person_id="akira", name="Akira", is_active=True),
        default_person_id="akira",
    )
    person = resolve_person(team, "yuki", allow_default=True)
    assert person.person_id == "yuki"


def test_resolve_person_reports_stale_default_as_unknown_person():
    team = _team(
        Person(person_id="yuki", name="Yuki", is_active=True),
        Person(person_id="akira", name="Akira", is_active=True),
        default_person_id="removed",
    )
    with pytest.raises(PersonNotFoundError) as excinfo:
        resolve_person(team, None, allow_default=True)
    assert excinfo.value.identifier == "removed"


def test_resolve_person_requires_identifier_without_any_candidate():
    team = _team(
        Person(person_id="yuki", name="Yuki", is_active=False),
        Person(person_id="ai", name="Ai", is_active=True, person_type="human"),
    )
    with pytest.raises(PersonSelectionRequiredError):
        resolve_person(team, None, allow_default=True)


def test_resolve_person_ignores_default_when_not_allowed():
    team = _team(
        Person(person_id="yuki", name="Yuki", is_active=True),
        default_person_id="yuki",
    )
    with pytest.raises(PersonSelectionRequiredError):
        resolve_person(team, None)


def test_resolve_person_raises_when_unknown():
    team = _team(Person(person_id="yuki", name="Yuki", is_active=True))
    with pytest.raises(PersonNotFoundError):
        resolve_person(team, "akira", allow_default=True)


class RecordingBrain:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []
        self.response_class = None

    async def run(self, message: str, **_: object) -> str:
        self.calls.append(message)
        return f"{self.name}:{message}"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def _get_context(message: str = "") -> Context:
    person = Person(person_id="alice", name="Alice", is_active=True)
    return _context_for_team(_team(person), message).clone_for(person)


def _context_for_person(person: Person, message: str = "") -> Context:
    return _context_for_team(_team(person), message)


def _context_for_team(team: Team, message: str = "") -> Context:
    return Context.get_default(
        DummyLoaderFactory(team),
        DummyIntegrationFactory(),
        DummyBrainFactory(),
        message,
    )


@pytest.mark.asyncio
async def test_run_custom_command_returns_brain_output(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "commands/solo.md",
        """
        ---
        brain: none
        template_engine: jinja2
        ---
        Greetings {{ arg1 }}
        {{ context.pipe }}
        """,
    )

    result = await run_command(_get_context("stdin text"), "solo", ["world"])
    assert result == "Greetings world\nstdin text"


@pytest.mark.asyncio
async def test_run_command_runs_as_configured_default_person(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "commands/whoami.md",
        """
        ---
        brain: none
        template_engine: jinja2
        ---
        {{ context.person.person_id }}
        """,
    )
    team = _team(
        Person(person_id="yuki", name="Yuki", is_active=True),
        Person(person_id="akira", name="Akira", is_active=True),
        default_person_id="akira",
    )

    assert await run_command(_context_for_team(team), "whoami", []) == "akira"


@pytest.mark.asyncio
async def test_run_command_releases_person_lease_when_discovery_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    context = _get_context()

    with pytest.raises(CommandError):
        await run_command(context, "missing", [])

    _write(tmp_path / "commands/solo.md", "---\nbrain: none\n---\ndone")
    assert await run_command(context, "solo", []) == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command_spec", "person_option"),
    [("solo", "aiko"), ("solo@aiko", None)],
)
async def test_cli_run_rejects_human_member_without_traceback(
    monkeypatch, command_spec: str, person_option: str | None
):
    human = Person(
        person_id="aiko",
        name="Aiko",
        is_active=False,
        person_type="human",
    )
    context = _context_for_person(human)

    class FakeEdition:
        def get_context(self, message: str = "") -> Context:
            assert message == ""
            return context

    monkeypatch.setattr(cli_module, "get_edition", lambda: FakeEdition())

    with pytest.raises(click.ClickException) as exc_info:
        await cli_module._run_custom_command(command_spec, (), person_option, "")

    assert "cannot be used as an AI execution subject" in str(exc_info.value)


@pytest.mark.asyncio
async def test_run_custom_command_rejects_human_member(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "commands/solo.md",
        """
        ---
        brain: none
        ---
        Greetings
        """,
    )
    human = Person(
        person_id="aiko",
        name="Aiko",
        is_active=False,
        person_type="human",
    )
    context = _context_for_person(human)

    with pytest.raises(PersonExecutionNotAllowedError):
        await run_command(context, "solo", [], person_identifier="aiko")


@pytest.mark.asyncio
async def test_executor_runs_markdown_with_subcommands(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "commands/pipeline.md",
        """
        ---
        brain: none
        commands:
          - name: first_payload
            path: first.md
          - name: python_payload
            path: tools/python_step.py
            params:
              foo: bar
        ---
        Main start for {{1}}
        """,
    )
    _write(
        tmp_path / "commands/first.md",
        """
        ---
        brain: default
        ---
        First step
        """,
    )
    _write(
        tmp_path / "commands/tools/python_step.py",
        """
        from guildbotics.runtime import Context


        async def main(context: Context, foo: str):
            return {"pipe": context.pipe, "foo": foo}
        """,
    )

    context = _get_context("initial")
    executor = CommandRunner(context, "pipeline", ["ARG"])
    result = await executor.run()

    runner = executor._context
    assert runner.shared_state["pipeline"].startswith("Main start for ARG")
    assert "first_payload" in runner.shared_state
    assert runner.shared_state["python_payload"] == {
        "pipe": to_text(runner.shared_state["first_payload"]),
        "foo": "bar",
    }
    assert runner.pipe == result


@pytest.mark.asyncio
async def test_executor_runs_shell_command(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "commands/shell_driver.md",
        """
        ---
        brain: none
        commands:
          - name: shell_output
            path: tools/echo.sh
            params:
              foo: bar
            args:
            - alpha
            - beta
        ---
        Shell body {{1}}
        """,
    )

    script_path = tmp_path / "commands/tools/echo.sh"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(
        """
        #!/usr/bin/env bash
        set -euo pipefail

        echo "args:$*"
        echo "stdin:$(cat)"
        echo "FOO=${foo:-missing}"
        """.strip()
        + "\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    context = _get_context("initial")
    executor = CommandRunner(context, "shell_driver", ["ARG"])
    result = await executor.run()

    runner = executor._context
    shell_output = runner.shared_state["shell_output"]

    assert "args:alpha beta" in shell_output
    assert "FOO=bar" in shell_output
    assert "Shell body ARG" in result


@pytest.mark.asyncio
async def test_python_command_can_invoke_subcommand(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "commands/driver.py",
        """
        from guildbotics.runtime import Context

        async def main(context: Context):
            await context.invoke("invoked_md", "value")
            return {
                "invoked": context.shared_state.get("invoked_md"),
                "stdin": context.pipe,
            }
        """,
    )
    _write(
        tmp_path / "commands/invoked_md.md",
        """
        ---
        brain: none
        ---
        Placeholder {{1}}
        """,
    )

    context = _get_context()
    executor = CommandRunner(context, "driver", [])
    await executor.run()

    shared = executor._context.shared_state
    assert shared["invoked_md"].startswith("Placeholder value")
    assert shared["driver"]["invoked"] == shared["invoked_md"]
    assert shared["driver"]["stdin"] == shared["invoked_md"]
