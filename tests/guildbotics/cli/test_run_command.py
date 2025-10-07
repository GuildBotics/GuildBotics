import logging
import textwrap
from pathlib import Path

import pytest
from pydantic import BaseModel

from guildbotics.cli import _parse_command_spec
from guildbotics.drivers.custom_command_runner import (
    CustomCommandExecutor,
    PersonNotFoundError,
    PersonSelectionRequiredError,
    _resolve_person,
    _stringify_output,
    run_custom_command,
)
from guildbotics.entities.team import Person, Project, Team


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


class _SampleModel(BaseModel):
    value: str


def test_stringify_output_handles_model_and_primitives():
    model_output = _SampleModel(value="ok")
    assert "value: ok" in _stringify_output(model_output)
    assert _stringify_output({"a": 1}) == "a: 1"
    assert _stringify_output(["foo", "bar"]) == "foo\nbar"


class RecordingBrain:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []
        self.response_class = None

    async def run(self, message: str, **_: object) -> str:
        self.calls.append(message)
        return f"{self.name}:{message}"


class StubContext:
    def __init__(self, team: Team, person: Person, store: dict | None = None) -> None:
        self.team = team
        self.person = person
        self._brain_store = store if store is not None else {}
        self.brains = self._brain_store
        self.logger = logging.getLogger("test")

    def clone_for(self, person: Person) -> "StubContext":
        return StubContext(self.team, person, self._brain_store)

    def get_brain(self, name: str) -> RecordingBrain:
        key = (self.person.person_id, name)
        brain = self._brain_store.get(key)
        if brain is None:
            brain = RecordingBrain(name)
            self._brain_store[key] = brain
        return brain


def _make_team(person: Person) -> Team:
    project = Project(name="demo")
    return Team(project=project, members=[person])


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_run_custom_command_returns_brain_output(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "prompts/solo.md",
        """
        ---
        brain: default
        ---
        Greetings {{1}}
        """,
    )

    person = Person(person_id="alice", name="Alice", is_active=True)
    team = _make_team(person)
    base_context = StubContext(team, person)

    result = await run_custom_command(base_context, "solo", ["world"], "stdin text")

    solo_path = str(tmp_path / "prompts/solo.md")
    assert result == f"{solo_path}:Greetings world\n\nstdin text"
    brain = base_context.brains[(person.person_id, solo_path)]
    assert brain.calls == ["Greetings world\n\nstdin text"]


@pytest.mark.asyncio
async def test_executor_runs_markdown_with_subcommands(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "prompts/pipeline.md",
        """
        ---
        brain: default
        commands:
          - name: first_step
            path: first.md
            output_key: first_payload
          - name: python_step
            path: tools/python_step.py
            output_key: python_payload
            params:
              foo: bar
        ---
        Main start for {{1}}
        """,
    )
    _write(
        tmp_path / "prompts/first.md",
        """
        ---
        brain: default
        ---
        First step
        """,
    )
    _write(
        tmp_path / "prompts/tools/python_step.py",
        """
        from guildbotics.drivers.custom_command_runner import RunnerContext


        async def main(context: RunnerContext, foo: str):
            return {"stdin": context.stdin_text, "foo": foo}
        """,
    )

    person = Person(person_id="alice", name="Alice", is_active=True)
    team = _make_team(person)
    context = StubContext(team, person)

    executor = CustomCommandExecutor(
        context.clone_for(person), "pipeline", ["ARG"], "initial"
    )
    result = await executor.run()

    runner = executor._runner_context
    pipeline_path = str(tmp_path / "prompts/pipeline.md")
    assert runner.shared_state["pipeline"].startswith(
        f"{pipeline_path}:Main start for ARG"
    )
    assert "first_payload" in runner.shared_state
    assert runner.shared_state["python_payload"] == {
        "stdin": runner.shared_state["first_payload"],
        "foo": "bar",
    }
    assert runner.prompt_output == result
    assert runner.message == result


@pytest.mark.asyncio
async def test_executor_runs_shell_command(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "prompts/shell_driver.md",
        """
        ---
        brain: default
        commands:
          - name: echo_shell
            path: tools/echo.sh
            output_key: shell_output
            params:
              foo: bar
              args:
                - alpha
                - beta
        ---
        Shell body {{1}}
        """,
    )

    script_path = tmp_path / "prompts/tools/echo.sh"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(
        """
        #!/usr/bin/env bash
        set -euo pipefail

        echo "args:$*"
        echo "stdin:$(cat)"
        echo "FOO=${GUILDBOTICS_PARAM_FOO:-missing}"
        """.strip()
        + "\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    person = Person(person_id="alice", name="Alice", is_active=True)
    team = _make_team(person)
    context = StubContext(team, person)

    executor = CustomCommandExecutor(
        context.clone_for(person), "shell_driver", ["ARG"], "initial"
    )
    result = await executor.run()

    runner = executor._runner_context
    shell_output = runner.shared_state["shell_output"]

    assert "args:alpha beta" in shell_output
    assert "FOO=bar" in shell_output
    assert "Shell body ARG" in shell_output
    assert result == shell_output


@pytest.mark.asyncio
async def test_python_command_can_invoke_subcommand(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "prompts/driver.py",
        """
        from guildbotics.drivers.custom_command_runner import RunnerContext


        async def main(context: RunnerContext):
            await context.invoke("invoked_md", args=["value"])
            return {
                "invoked": context.shared_state.get("invoked_md"),
                "stdin": context.stdin_text,
            }
        """,
    )
    _write(
        tmp_path / "prompts/invoked_md.md",
        """
        ---
        brain: default
        ---
        Placeholder {{1}}
        """,
    )

    person = Person(person_id="alice", name="Alice", is_active=True)
    team = _make_team(person)
    context = StubContext(team, person)

    executor = CustomCommandExecutor(context.clone_for(person), "driver", [], "")
    await executor.run()

    shared = executor._runner_context.shared_state
    invoked_path = str(tmp_path / "prompts/invoked_md.md")
    assert shared["invoked_md"].startswith(f"{invoked_path}:Placeholder value")
    assert shared["driver"]["invoked"] == shared["invoked_md"]
    assert shared["driver"]["stdin"] == shared["invoked_md"]
