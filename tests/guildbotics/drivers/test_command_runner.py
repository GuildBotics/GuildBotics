from pathlib import Path
from types import SimpleNamespace

import pytest

from guildbotics.commands.models import CommandOutcome, CommandSpec
from guildbotics.drivers.command_runner import CommandRunner
from guildbotics.intelligences.brains.cli_agent import PromptInfo
from guildbotics.utils.fileio import load_markdown_with_frontmatter


class DummyCommand:
    last_cwd = None

    def __init__(self, context, spec, cwd):
        DummyCommand.last_cwd = cwd
        self.options = SimpleNamespace(output_key=spec.name)

    async def run(self):
        return CommandOutcome(result="ok", text_output="ok")


class DummyContext:
    def __init__(self):
        self.shared_state = {}
        self.pipe = ""
        self.invoker = None

    def set_invoker(self, invoker):
        self.invoker = invoker

    def update(self, key, value, text_value):
        self.shared_state[key] = value
        self.pipe = text_value


def _main_spec():
    return CommandSpec(
        name="main",
        base_dir=Path("."),
        command_class=DummyCommand,
        path=Path("main.md"),
        cwd=Path("/workspace"),
    )


@pytest.mark.asyncio
async def test_invoke_passes_top_level_cwd_to_spec_factory(monkeypatch):
    monkeypatch.setattr(CommandRunner, "_prepare_main_spec", lambda self: _main_spec())
    ctx = DummyContext()
    runner = CommandRunner(ctx, "main", [])

    captured = {}

    def fake_build(anchor, entry):
        captured["entry"] = entry
        return _main_spec()

    async def fake_run_with_children(spec):
        return CommandOutcome(result="ok", text_output="ok")

    runner._spec_factory.build_from_entry = fake_build
    runner._run_with_children = fake_run_with_children

    await runner._invoke("child", cwd=Path("/memory"), foo="bar")

    assert captured["entry"]["cwd"] == Path("/memory")
    assert captured["entry"]["params"] == {"foo": "bar"}


@pytest.mark.asyncio
async def test_invoke_delegates_completion_managed_turns_to_the_host(monkeypatch):
    from guildbotics.drivers import agent_turn

    monkeypatch.setattr(CommandRunner, "_prepare_main_spec", lambda self: _main_spec())
    ctx = DummyContext()
    runner = CommandRunner(ctx, "main", [])
    captured = {}

    async def fake_run_agent_turn(*, invoke, execution_context):
        captured["execution_context"] = execution_context
        return await invoke(
            {**execution_context, "attempt": 2}, {"previous_attempt_evidence": "[]"}
        )

    def fake_build(anchor, entry):
        captured["entry"] = entry
        return _main_spec()

    async def fake_run_with_children(spec):
        return CommandOutcome(result="completed", text_output="completed")

    monkeypatch.setattr(agent_turn, "run_agent_turn", fake_run_agent_turn)
    runner._spec_factory.build_from_entry = fake_build
    runner._run_with_children = fake_run_with_children

    result = await runner._invoke(
        "child",
        cwd=Path("/memory"),
        agent_execution_context={
            "run_id": "run-1",
            "work_kind": "ticket",
            "max_completion_attempts": 3,
        },
    )

    assert result == "completed"
    assert captured["execution_context"]["run_id"] == "run-1"
    assert captured["entry"]["params"]["agent_execution_context"]["attempt"] == 2
    # The host's per-attempt prompt parameters reach the command.
    assert captured["entry"]["params"]["previous_attempt_evidence"] == "[]"
    assert captured["entry"]["cwd"] == Path("/memory")


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_the_command_is_the_span_its_turns_share_an_environment_in(
    monkeypatch, fails
):
    """Every AI CLI turn of the run, its subcommands' included, shares one
    environment, and it is discarded when the run ends, however it ends."""
    from guildbotics.intelligences.agent_runtime import environment

    closed: list[object] = []
    seen: list[object] = []

    class Shared:
        def __init__(self, access) -> None:
            self.access = access

        async def close(self) -> None:
            closed.append(self)

    monkeypatch.setattr(environment, "_SharedEnvironment", Shared)

    class Turning(DummyCommand):
        @staticmethod
        def populate_spec(*_):
            pass

        async def run(self):
            seen.append(environment._COMMAND.get())
            if fails:
                raise RuntimeError("the command failed")
            return await super().run()

    spec = _main_spec()
    spec.command_class = Turning
    spec.children = [
        CommandSpec(
            name="child", base_dir=Path("."), command_class=Turning, cwd=Path("/")
        )
    ]
    monkeypatch.setattr(CommandRunner, "_prepare_main_spec", lambda self: spec)
    runner = CommandRunner(DummyContext(), "main", [])
    runner._spec_factory.build_from_entry = lambda anchor, entry: entry

    if fails:
        with pytest.raises(RuntimeError):
            await runner.run()
    else:
        await runner.run()

    assert seen and all(isinstance(shared, Shared) for shared in seen)
    assert len(set(map(id, seen))) == 1
    assert closed == seen[:1]
    assert environment._COMMAND.get() is None


@pytest.mark.asyncio
async def test_run_uses_spec_cwd_not_runner_cwd(monkeypatch):
    monkeypatch.setattr(CommandRunner, "_prepare_main_spec", lambda self: _main_spec())
    ctx = DummyContext()
    runner = CommandRunner(ctx, "main", [], cwd=Path("/workspace"))
    spec = CommandSpec(
        name="child",
        base_dir=Path("."),
        command_class=DummyCommand,
        cwd=Path("/memory"),
    )

    await runner._run(spec)

    assert DummyCommand.last_cwd == Path("/memory")


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "ja"])
async def test_ask_passes_message_member_and_working_tree_to_brain(
    tmp_path, monkeypatch, language
):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path / "config"))
    working_tree = tmp_path / "working tree"
    working_tree.mkdir()
    edited_file = working_tree / "uncommitted.txt"
    edited_file.write_text("local edit", encoding="utf-8")
    ctx = DummyContext()
    ctx.person = SimpleNamespace(person_id="alice")
    ctx.team = SimpleNamespace(
        project=SimpleNamespace(get_language_code=lambda: language)
    )
    request = "Review the current edits. 日本語・`$()`\nDo not publish."
    ctx.pipe = request

    def get_brain(name, config, class_resolver):
        metadata = load_markdown_with_frontmatter(Path(name))
        assert metadata["brain"] == "agent"

        async def run(*, message, session_state, cwd):
            assert message == request
            assert cwd == working_tree
            assert (cwd / edited_file.name).read_text(encoding="utf-8") == "local edit"
            prompt = PromptInfo(None, metadata["body"]).to_prompt(
                message, session_state, metadata["template_engine"]
            )
            assert "guildbotics member context --person alice" in prompt
            assert "{{" not in prompt
            return "Review completed: local edit inspected."

        return SimpleNamespace(run=run, response_class=None)

    ctx.get_brain = get_brain
    outcome = await CommandRunner(ctx, "ask", [], cwd=working_tree).run()

    result = "Review completed: local edit inspected."
    assert outcome.result == outcome.text_output == result
    assert ctx.shared_state["ask"] == result
    assert edited_file.read_text(encoding="utf-8") == "local edit"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["manual", "scheduled", "routine"])
async def test_ticket_workflow_runs_only_through_its_selector(monkeypatch, source):
    from guildbotics.drivers import command_runner, ticket_selector
    from guildbotics.runtime.workflow_invocation import (
        TICKET_WORKFLOW_COMMAND,
        WORKFLOW_INVOCATION_KEY,
    )

    invocation = object()
    selected: list[str] = []

    class FakeRunner:
        command_name = TICKET_WORKFLOW_COMMAND

        def __init__(self, context):
            self.context = context

        async def run(self):
            # The workflow finds the ticket the host selected.
            assert self.context.shared_state[WORKFLOW_INVOCATION_KEY] is invocation
            return CommandOutcome(result="worked", text_output="worked")

    class FakeSelector:
        def __init__(self, context, *, source):
            selected.append(source)

        async def run_next(self, person, run_workflow):
            return await run_workflow(invocation)

    monkeypatch.setattr(ticket_selector, "TicketSelector", FakeSelector)
    context = DummyContext()
    context.person = SimpleNamespace(person_id="aiko")

    outcome = await command_runner.run_main_command(FakeRunner(context), source=source)

    assert outcome.text_output == "worked"
    assert selected == [source]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "output"),
    [(None, ""), ("Rate limited until 10:00.", "Rate limited until 10:00.")],
    ids=["no-ticket", "rate-limited"],
)
async def test_ticket_workflow_without_a_run_outputs_what_the_selector_said(
    monkeypatch, reply, output
):
    from guildbotics.drivers import command_runner, ticket_selector
    from guildbotics.runtime.workflow_invocation import TICKET_WORKFLOW_COMMAND

    class FakeRunner:
        command_name = TICKET_WORKFLOW_COMMAND

        def __init__(self, context):
            self.context = context

        async def run(self):
            raise AssertionError("no ticket, no workflow")

    class IdleSelector:
        def __init__(self, context, *, source):
            pass

        async def run_next(self, person, run_workflow):
            return reply

    monkeypatch.setattr(ticket_selector, "TicketSelector", IdleSelector)
    context = DummyContext()
    context.person = SimpleNamespace(person_id="aiko")

    outcome = await command_runner.run_main_command(
        FakeRunner(context), source="manual"
    )

    assert outcome.text_output == output
