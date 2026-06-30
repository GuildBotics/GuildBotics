import textwrap
from pathlib import Path

import click
import pytest

import guildbotics.cli as cli_module
from guildbotics.cli import _parse_command_spec
from guildbotics.drivers.command_runner import (
    CommandRunner,
    PersonExecutionNotAllowedError,
    PersonNotFoundError,
    PersonSelectionRequiredError,
    _resolve_person,
    run_command,
)
from guildbotics.entities.team import Person, Project, Team
from guildbotics.intelligences.functions import to_text
from guildbotics.runtime.context import Context
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
    assert "_resolve_person" in command_runner.__all__


def test_parse_command_spec_with_person():
    name, person = _parse_command_spec("translate@yuki")
    assert name == "translate"
    assert person == "yuki"


def test_parse_command_spec_without_person():
    name, person = _parse_command_spec(" summarize ")
    assert name == "summarize"
    assert person is None


def test_resolve_person_with_explicit_identifier():
    members = [
        Person(person_id="yuki", name="Yuki", is_active=True),
        Person(person_id="kato", name="Kato", is_active=False),
    ]
    person = _resolve_person(members, "Kato")
    assert person.person_id == "kato"


def test_resolve_person_defaults_to_single_active():
    members = [Person(person_id="yuki", name="Yuki", is_active=True)]
    person = _resolve_person(members, None)
    assert person.person_id == "yuki"


def test_resolve_person_requires_identifier_when_ambiguous():
    members = [
        Person(person_id="yuki", name="Yuki", is_active=True),
        Person(person_id="akira", name="Akira", is_active=True),
    ]
    with pytest.raises(PersonSelectionRequiredError):
        _resolve_person(members, None)


def test_resolve_person_raises_when_unknown():
    members = [Person(person_id="yuki", name="Yuki", is_active=True)]
    with pytest.raises(PersonNotFoundError):
        _resolve_person(members, "akira")


class RecordingBrain:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []
        self.response_class = None

    async def run(self, message: str, **_: object) -> str:
        self.calls.append(message)
        return f"{self.name}:{message}"


def _make_team(person: Person) -> Team:
    project = Project(name="demo")
    return Team(project=project, members=[person])


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def _get_context(message: str = "") -> Context:
    person = Person(person_id="alice", name="Alice", is_active=True)
    team = _make_team(person)
    loader_factory = DummyLoaderFactory(team)
    integration_factory = DummyIntegrationFactory()
    brain_factory = DummyBrainFactory()
    return Context.get_default(
        loader_factory, integration_factory, brain_factory, message
    ).clone_for(person)


def _context_for_person(person: Person, message: str = "") -> Context:
    return Context.get_default(
        DummyLoaderFactory(_make_team(person)),
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
