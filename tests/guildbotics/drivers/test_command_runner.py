from pathlib import Path
from types import SimpleNamespace

import pytest

from guildbotics.commands.errors import CommandError
from guildbotics.commands.models import CommandOutcome, CommandSpec
from guildbotics.commands.runner import CommandRunner
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
        self.person = SimpleNamespace(person_id="aiko")

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


def _runner_for(spec):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(CommandRunner, "_prepare_main_spec", lambda self: spec)
        runner = CommandRunner(DummyContext(), "main", [])
    runner._spec_factory.build_from_entry = lambda anchor, entry: entry
    return runner


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
async def test_invoke_drives_completion_managed_turns_with_the_host_ledger(monkeypatch):
    from guildbotics.commands import runner as runner_module

    monkeypatch.setattr(CommandRunner, "_prepare_main_spec", lambda self: _main_spec())
    ctx = DummyContext()
    ledger = object()
    runner = CommandRunner(ctx, "main", [], ledger=ledger)
    captured = {}

    async def fake_run_agent_turn(*, invoke, execution_context, ledger):
        captured["execution_context"] = execution_context
        captured["ledger"] = ledger
        return await invoke(
            {**execution_context, "attempt": 2}, {"previous_attempt_evidence": "[]"}
        )

    def fake_build(anchor, entry):
        captured["entry"] = entry
        return _main_spec()

    async def fake_run_with_children(spec):
        return CommandOutcome(result="completed", text_output="completed")

    monkeypatch.setattr(runner_module, "run_agent_turn", fake_run_agent_turn)
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
    assert captured["ledger"] is ledger
    assert captured["entry"]["params"]["agent_execution_context"]["attempt"] == 2
    # The host's per-attempt prompt parameters reach the command.
    assert captured["entry"]["params"]["previous_attempt_evidence"] == "[]"
    assert captured["entry"]["cwd"] == Path("/memory")


@pytest.mark.asyncio
async def test_invoke_refuses_completion_managed_turns_without_a_ledger(monkeypatch):
    monkeypatch.setattr(CommandRunner, "_prepare_main_spec", lambda self: _main_spec())
    runner = CommandRunner(DummyContext(), "main", [])

    async def fail_run_with_children(spec):
        raise AssertionError("the turn must not start")

    runner._run_with_children = fail_run_with_children

    with pytest.raises(CommandError, match="run ledger"):
        await runner._invoke(
            "child",
            agent_execution_context={
                "run_id": "run-1",
                "work_kind": "ticket",
                "max_completion_attempts": 3,
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_the_command_is_the_span_its_turns_share_an_environment_in(
    monkeypatch, fails
):
    """Every AI CLI turn of the run, its subcommands' included, shares one
    environment, and it is discarded when the run ends, however it ends."""
    from guildbotics.drivers.command_runner import run_in_environment
    from guildbotics.intelligences.agent_runtime import environment

    closed: list[object] = []
    seen: list[object] = []

    class Shared:
        def __init__(self, access, contract, tools) -> None:
            self.access = access
            self.tools = tools

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
            await run_in_environment(runner)
    else:
        await run_in_environment(runner)

    assert seen and all(isinstance(shared, Shared) for shared in seen)
    assert len(set(map(id, seen))) == 1
    assert closed == seen[:1]
    assert environment._COMMAND.get() is None


@pytest.mark.asyncio
async def test_the_machinery_opens_no_environment_of_its_own():
    """The host that starts a run opens its environment; the runner alone
    runs the command outside any."""
    from guildbotics.intelligences.agent_runtime import environment

    seen: list[object] = []

    class Turning(DummyCommand):
        @staticmethod
        def populate_spec(*_):
            pass

        async def run(self):
            seen.append(environment._COMMAND.get())
            return await super().run()

    spec = _main_spec()
    spec.command_class = Turning
    await _runner_for(spec).run()

    assert seen == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize("inner_read_only", [False, True])
async def test_a_command_started_inside_another_shares_its_environment(
    monkeypatch, inner_read_only
):
    """A host entry that starts a command inside a running one shares the
    running one's environment when it declares the same access, and is
    refused when it declares other access."""
    from guildbotics.commands.errors import CommandError
    from guildbotics.commands.metadata import CommandAccess
    from guildbotics.drivers.command_runner import run_in_environment
    from guildbotics.intelligences.agent_runtime import environment

    closed: list[object] = []
    seen: list[object] = []

    class Shared:
        def __init__(self, access, contract, tools) -> None:
            self.access = access
            self.tools = tools

        async def close(self) -> None:
            closed.append(self)

    monkeypatch.setattr(environment, "_SharedEnvironment", Shared)

    class Inner(DummyCommand):
        @staticmethod
        def populate_spec(*_):
            pass

        async def run(self):
            seen.append(environment._COMMAND.get())
            return await super().run()

    class Outer(Inner):
        async def run(self):
            seen.append(environment._COMMAND.get())
            inner_spec = _main_spec()
            inner_spec.command_class = Inner
            inner = _runner_for(inner_spec)
            inner.access = CommandAccess(read_only=inner_read_only)
            await run_in_environment(inner)
            return await DummyCommand.run(self)

    spec = _main_spec()
    spec.command_class = Outer

    if inner_read_only:
        with pytest.raises(CommandError):
            await run_in_environment(_runner_for(spec))
        assert len(seen) == 1
    else:
        await run_in_environment(_runner_for(spec))
        assert len(seen) == 2 and seen[0] is seen[1]
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
    from guildbotics.commands.metadata import CommandAccess
    from guildbotics.drivers import command_runner, ticket_selector
    from guildbotics.intelligences.agent_runtime import environment
    from guildbotics.runtime.workflow_invocation import (
        TICKET_WORKFLOW_COMMAND,
        WORKFLOW_INVOCATION_KEY,
    )

    invocation = object()
    selected: list[str] = []

    class FakeRunner:
        command_name = TICKET_WORKFLOW_COMMAND
        access = CommandAccess()

        def __init__(self, context):
            self.context = context

        async def run(self):
            # The workflow finds the ticket the host selected, and runs in the
            # environment its turns share.
            assert self.context.shared_state[WORKFLOW_INVOCATION_KEY] is invocation
            assert environment._COMMAND.get() is not None
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


@pytest.mark.asyncio
async def test_the_environment_is_shaped_for_every_tool_the_member_is_configured_with(
    monkeypatch,
):
    """Which tool a turn uses is decided while the command runs, so the
    environment is started able to run each of the member's slots."""
    from guildbotics.drivers.command_runner import run_in_environment
    from guildbotics.intelligences.agent_runtime import environment
    from guildbotics.intelligences.brains import cli_agent

    monkeypatch.setitem(
        cli_agent.person_cli_agent_mapping,
        "aiko",
        {
            "default": cli_agent.ExecutableInfo(adapter="claude"),
            "review": cli_agent.ExecutableInfo(adapter="codex"),
            "again": cli_agent.ExecutableInfo(adapter="claude"),
        },
    )
    seen: list[frozenset[str]] = []

    class Turning(DummyCommand):
        @staticmethod
        def populate_spec(*_):
            pass

        async def run(self):
            seen.append(environment.running_command().tools)
            return await super().run()

    spec = _main_spec()
    spec.command_class = Turning
    await run_in_environment(_runner_for(spec))

    assert seen == [frozenset({"claude", "codex"})]


def _unreadable(name: str):
    def fail(*_):
        raise PermissionError(13, "Permission denied", name)

    return fail


def _invalid(error: type[Exception]):
    def fail(*_):
        raise error("the setting is invalid")

    return fail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("loader", "failure", "message"),
    [
        ("load_toolchain", "toolchain", "the setting is invalid"),
        ("load_shared_grants", "grants", "the setting is invalid"),
        ("resolve_access", "permission", None),
    ],
)
async def test_invalid_contract_settings_fail_the_command_when_it_starts(
    monkeypatch, tmp_path, loader, failure, message
):
    """The settings the contract is read from are read once, when the command
    starts: an error in them fails every command, one that runs no AI CLI
    turn included, before anything of it runs."""
    from guildbotics.commands.errors import CommandError
    from guildbotics.drivers.command_runner import run_in_environment
    from guildbotics.intelligences.agent_environment.contract import (
        AccessContractError,
    )
    from guildbotics.intelligences.agent_environment.status import (
        filesystem_permission_problem,
    )
    from guildbotics.intelligences.agent_environment.toolchain import ToolchainError
    from guildbotics.intelligences.agent_runtime import environment

    unreadable = tmp_path / "Documents" / "shared"
    monkeypatch.setattr(
        environment,
        loader,
        {
            "toolchain": _invalid(ToolchainError),
            "grants": _invalid(AccessContractError),
            "permission": _unreadable(str(unreadable)),
        }[failure],
    )
    ran: list[str] = []

    class Plain(DummyCommand):
        @staticmethod
        def populate_spec(*_):
            pass

        async def run(self):
            ran.append("ran")
            return await super().run()

    spec = _main_spec()
    spec.command_class = Plain

    with pytest.raises(CommandError) as refused:
        await run_in_environment(_runner_for(spec))

    assert str(refused.value) == (message or filesystem_permission_problem(unreadable))
    assert ran == []
    assert environment._COMMAND.get() is None


@pytest.mark.asyncio
async def test_invalid_ai_cli_tool_settings_fail_the_command_when_it_starts(
    monkeypatch,
):
    from guildbotics.commands.errors import CommandError
    from guildbotics.drivers import command_runner

    def invalid(person_id):
        raise ValueError(f"AI CLI tool slot 'default' of {person_id} is invalid")

    monkeypatch.setattr(command_runner, "get_cli_agent_mapping", invalid)
    ran: list[str] = []

    class Plain(DummyCommand):
        @staticmethod
        def populate_spec(*_):
            pass

        async def run(self):
            ran.append("ran")
            return await super().run()

    spec = _main_spec()
    spec.command_class = Plain

    with pytest.raises(CommandError, match="slot 'default' of aiko is invalid"):
        await command_runner.run_in_environment(_runner_for(spec))
    assert ran == []


def test_host_ledger_needs_a_workspace_only_when_a_turn_uses_it(monkeypatch):
    # Every command gets a ledger, so building one must not require the
    # workspace that only a completion-managed turn reads.
    from guildbotics.drivers.command_runner import HostRunLedger
    from guildbotics.utils.fileio import WorkspaceNotConfiguredError

    monkeypatch.delenv("GUILDBOTICS_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)

    ledger = HostRunLedger()

    with pytest.raises(WorkspaceNotConfiguredError):
        ledger.require_completion("run-1")
