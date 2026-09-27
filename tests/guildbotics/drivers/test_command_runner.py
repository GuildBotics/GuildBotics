from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from guildbotics.commands.errors import CommandError, CommandFailedError
from guildbotics.commands.metadata import CommandAccess
from guildbotics.commands.models import CommandOutcome, CommandSpec
from guildbotics.commands.runner import CommandRunner
from guildbotics.commands.spec_factory import CommandSpecFactory
from guildbotics.drivers import command_runner
from guildbotics.drivers.command_runner import PreparedCommand
from guildbotics.intelligences.agent_runtime.host_client import (
    CommandFailure,
    CommandReply,
)
from guildbotics.intelligences.brains.cli_agent import (
    CliAgentExecutionError,
    PromptInfo,
)
from guildbotics.utils.fileio import load_markdown_with_frontmatter
from tests.guildbotics.command_environment_doubles import machinery


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


def _runner(context=None, spec=None, **kwargs):
    """The machinery for ``spec`` (``main`` by default), working in
    ``/workspace`` where it may work anywhere."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            CommandSpecFactory,
            "prepare_main_spec",
            lambda self, *_: spec or _main_spec(),
        )
        return CommandRunner(
            context or DummyContext(),
            "main",
            [],
            Path("/workspace"),
            path=Path("main.md"),
            mounts={"/": True},
            **kwargs,
        )


def _prepared(
    context=None,
    *,
    path=Path("/workspace/.guildbotics/config/commands/main.md"),
    **kwargs,
):
    """The host's reading of ``main`` for aiko, working in ``/workspace``."""
    context = context or DummyContext()
    return PreparedCommand(
        context,
        kwargs.pop("command_name", "main"),
        ["x=1"],
        Path("/workspace"),
        path,
        kwargs.pop("access", CommandAccess()),
        **kwargs,
    )


def _environment(monkeypatch, *replies):
    """The environment every command the host runs is booted in: what it was
    booted for, what it was asked to run, and whether it was discarded. It
    answers with ``replies``, in turn; one that is an exception is raised."""
    booted = SimpleNamespace(opened=[], requests=[], closed=0)
    answers = list(replies) or [CommandReply(text_output="ok")]

    @asynccontextmanager
    async def command_environment(access, tools, **where):
        booted.opened.append({"access": access, "tools": tools, **where})

        class Running:
            async def execute(self, request):
                booted.requests.append(request)
                answer = answers.pop(0)
                if isinstance(answer, BaseException):
                    raise answer
                return answer

        try:
            yield Running()
        finally:
            booted.closed += 1

    monkeypatch.setattr(command_runner, "command_environment", command_environment)
    return booted


@pytest.mark.asyncio
async def test_invoke_passes_top_level_cwd_to_spec_factory(monkeypatch):
    runner = _runner()

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

    ledger = object()
    runner = _runner(ledger=ledger)
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
    runner = _runner()

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
@pytest.mark.parametrize(
    ("path", "spelled"),
    [
        (
            Path("/workspace/.guildbotics/config/commands/main.md"),
            "/workspace/.guildbotics/config/commands/main.md",
        ),
        (
            Path(command_runner.__file__).parents[1] / "templates/commands/ask.md",
            "/opt/guildbotics/code/guildbotics/templates/commands/ask.md",
        ),
    ],
)
@pytest.mark.parametrize("fails", [False, True])
async def test_the_command_runs_in_the_environment_booted_for_it(
    monkeypatch, path, spelled, fails
):
    """One environment per run, booted for where the command works and
    discarded however the run ends; it runs the very file the host read, as
    it spells it, with the command's input and workflow run."""
    from guildbotics.runtime.workflow_invocation import (
        WORKFLOW_INVOCATION_KEY,
        WorkflowInvocation,
    )
    from guildbotics.utils.fileio import get_member_clone_path, get_workspace_root

    booted = _environment(
        monkeypatch,
        RuntimeError("the environment failed") if fails else CommandReply(),
    )
    command = _prepared(path=path)
    command.context.pipe = "the input"
    invocation = WorkflowInvocation("main", "aiko", "routine", "generic", {"k": "v"})
    command.context.shared_state[WORKFLOW_INVOCATION_KEY] = invocation

    if fails:
        with pytest.raises(RuntimeError):
            await command_runner.run_in_environment(command)
    else:
        await command_runner.run_in_environment(command)

    workspace_root = get_workspace_root()
    ((opened,), (request,)) = booted.opened, booted.requests
    assert {key: opened[key] for key in ("cwd", "workspace_root", "clone")} == {
        "cwd": Path("/workspace"),
        "workspace_root": workspace_root,
        "clone": get_member_clone_path("aiko", workspace_root),
    }
    assert (request.path, request.name, request.args, request.cwd) == (
        spelled,
        "main",
        ["x=1"],
        "/workspace",
    )
    assert (request.pipe, request.invocation) == (
        "the input",
        {
            "command": "main",
            "person_id": "aiko",
            "source": "routine",
            "trigger_type": "generic",
            "payload": {"k": "v"},
            "idempotency_key": "",
        },
    )
    assert request.wants_result is False
    assert booted.closed == 1


@pytest.mark.asyncio
async def test_run_uses_spec_cwd_not_runner_cwd(monkeypatch):
    runner = _runner()
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
    outcome = await machinery(ctx, "ask", [], working_tree).run()

    result = "Review completed: local edit inspected."
    assert outcome.result == outcome.text_output == result
    assert ctx.shared_state["ask"] == result
    assert edited_file.read_text(encoding="utf-8") == "local edit"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["manual", "scheduled", "routine"])
async def test_ticket_workflow_runs_only_through_its_selector(monkeypatch, source):
    from guildbotics.drivers import ticket_selector
    from guildbotics.runtime.workflow_invocation import (
        TICKET_WORKFLOW_COMMAND,
        WORKFLOW_INVOCATION_KEY,
        WorkflowInvocation,
    )

    invocation = WorkflowInvocation(
        TICKET_WORKFLOW_COMMAND, "aiko", source, "ticket", {"run_id": "run-1"}
    )
    selected: list[str] = []

    async def run_in_environment(command):
        # The workflow finds the ticket the host selected.
        assert command.context.shared_state[WORKFLOW_INVOCATION_KEY] is invocation
        return CommandOutcome(result=None, text_output="worked")

    class FakeSelector:
        def __init__(self, context, *, source):
            selected.append(source)

        async def run_next(self, person, run_workflow):
            return await run_workflow(invocation)

    monkeypatch.setattr(ticket_selector, "TicketSelector", FakeSelector)
    monkeypatch.setattr(command_runner, "run_in_environment", run_in_environment)

    outcome = await command_runner.run_main_command(
        _prepared(command_name=TICKET_WORKFLOW_COMMAND), source=source
    )

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
    from guildbotics.drivers import ticket_selector
    from guildbotics.runtime.workflow_invocation import TICKET_WORKFLOW_COMMAND

    class IdleSelector:
        def __init__(self, context, *, source):
            pass

        async def run_next(self, person, run_workflow):
            return reply

    monkeypatch.setattr(ticket_selector, "TicketSelector", IdleSelector)
    booted = _environment(monkeypatch)

    outcome = await command_runner.run_main_command(
        _prepared(command_name=TICKET_WORKFLOW_COMMAND), source="manual"
    )
    assert booted.opened == []

    assert outcome.text_output == output


@pytest.mark.asyncio
async def test_the_environment_is_shaped_for_every_tool_the_member_is_configured_with(
    monkeypatch,
):
    """Which tool a turn uses is decided while the command runs, so the
    environment is started able to run each of the member's slots, and held
    to what the command declares."""
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
    booted = _environment(monkeypatch)
    declared = CommandAccess(read_only=True, inspects=frozenset({"diagnostics"}))

    await command_runner.run_in_environment(_prepared(access=declared))

    ((opened,),) = [booted.opened]
    assert opened["tools"] == frozenset({"claude", "codex"})
    assert opened["access"] == declared


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
    booted: list[object] = []
    monkeypatch.setattr(environment, "_start", lambda *args, **_: booted.append(args))

    with pytest.raises(CommandError) as refused:
        await run_in_environment(_prepared())

    assert str(refused.value) == (message or filesystem_permission_problem(unreadable))
    assert booted == []
    assert environment._COMMAND.get() is None


@pytest.mark.asyncio
async def test_invalid_ai_cli_tool_settings_fail_the_command_when_it_starts(
    monkeypatch,
):
    def invalid(person_id):
        raise ValueError(f"AI CLI tool slot 'default' of {person_id} is invalid")

    monkeypatch.setattr(command_runner, "get_cli_agent_mapping", invalid)
    booted = _environment(monkeypatch)

    with pytest.raises(CommandError, match="slot 'default' of aiko is invalid"):
        await command_runner.run_in_environment(_prepared())
    assert booted.opened == []


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trigger", "payload", "traced", "run_id", "work_kind"),
    [
        ("ticket", {"run_id": "ticket-run"}, True, "ticket-run", "ticket"),
        ("chat", {"run_id": "chat-run"}, True, "chat-run", "chat"),
        ("scheduled", {"run_id": "ignored"}, True, "trace", ""),
        (None, {}, True, "trace", ""),
        (None, {}, False, None, ""),
    ],
)
async def test_the_run_is_granted_for_its_member_and_run(
    monkeypatch, trigger, payload, traced, run_id, work_kind
):
    """What its microVM asks of the host is answered for the member it runs
    as and the run it records to: its workflow run's, or else one of its own
    -- its trace's, or a fresh one outside any."""
    from guildbotics.drivers.command_runner import run_in_environment
    from guildbotics.intelligences.agent_runtime.host_window import HostWindow
    from guildbotics.observability import trace_scope
    from guildbotics.runtime.workflow_invocation import (
        WORKFLOW_INVOCATION_KEY,
        WorkflowInvocation,
    )

    booted = _environment(monkeypatch)
    command = _prepared()
    if trigger is not None:
        command.context.shared_state[WORKFLOW_INVOCATION_KEY] = WorkflowInvocation(
            "workflows/x", "aiko", "routine", trigger, payload
        )
    if traced:
        with trace_scope("scheduler", trace_id="trace"):
            await run_in_environment(command)
    else:
        await run_in_environment(command)

    [grant] = [opened["host"] for opened in booted.opened]
    assert isinstance(grant, HostWindow)
    assert (grant._person_id, grant._work_kind) == ("aiko", work_kind)
    assert grant._run_id == run_id if run_id else len(grant._run_id) == 32


@pytest.mark.asyncio
async def test_the_grant_ends_with_its_command_however_it_ended(monkeypatch):
    """What the command's microVM was granted ends with the command -- a
    turn it left open among it -- whether the command said how it ended or
    its environment failed."""
    from guildbotics.intelligences.agent_runtime.host_window import HostWindow

    closed = []

    async def close(self):
        closed.append(self)

    monkeypatch.setattr(HostWindow, "close", close)
    booted = _environment(
        monkeypatch, CommandReply(text_output="ok"), CommandError("broken")
    )

    await command_runner.run_in_environment(_prepared())
    with pytest.raises(CommandError, match="broken"):
        await command_runner.run_in_environment(_prepared())

    assert closed == [opened["host"] for opened in booted.opened]
    assert len(closed) == 2


@pytest.mark.asyncio
async def test_a_result_crosses_only_as_the_type_the_caller_reads_it_as(
    monkeypatch,
):
    """What the environment returns is the command's to say, so the host
    reads it only as what it asked for: a caller that reads no result gets
    none, and a result that is not of its type fails the command."""
    from guildbotics.intelligences.troubleshooting import TroubleshootingResult

    answer = {"message": "The token expired.", "trace_ids": ["abc"]}
    booted = _environment(
        monkeypatch,
        CommandReply(result=answer, text_output="out"),
        CommandReply(result=answer, text_output="out"),
        CommandReply(result={"message": 3}, text_output="out"),
    )

    unread = await command_runner.run_in_environment(_prepared())
    read = await command_runner.run_in_environment(
        _prepared(result_type=TroubleshootingResult)
    )
    with pytest.raises(CommandError, match="did not return a TroubleshootingResult"):
        await command_runner.run_in_environment(
            _prepared(result_type=TroubleshootingResult)
        )

    assert unread == CommandOutcome(result=None, text_output="out")
    assert read.result == TroubleshootingResult(**answer)
    assert [request.wants_result for request in booted.requests] == [
        False,
        True,
        True,
    ]


_REFUSED = {
    "stdout": "",
    "stderr": "slow down",
    "returncode": 1,
    "error_category": "rate_limited",
    "error_details": {"retry_after_at": "2026-09-27T10:00:00+09:00"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "raised", "error_type", "code"),
    [
        (
            CommandFailure(command=True, type="CommandError", message="m"),
            CommandError,
            "CommandError",
            "",
        ),
        (
            CommandFailure(command=False, type="ValueError", message="m"),
            CommandFailedError,
            "ValueError",
            "",
        ),
        (
            CommandFailure(
                command=True,
                type="CommandError",
                message="m",
                cli_agent="codex",
                cli_agent_message="limited",
                cli_agent_result=_REFUSED,
            ),
            CommandError,
            "CommandError",
            "",
        ),
        (
            CommandFailure(
                command=False,
                type="CliAgentExecutionError",
                message="limited",
                cli_agent="codex",
                cli_agent_message="limited",
                cli_agent_result={**_REFUSED, "error_category": "authentication"},
            ),
            CliAgentExecutionError,
            "CliAgentExecutionError",
            "cli_agent_authentication",
        ),
        # A tool's failure that is not one as the host reads it is no cause.
        (
            CommandFailure(
                command=False,
                type="CliAgentExecutionError",
                message="limited",
                cli_agent="codex",
                cli_agent_result={**_REFUSED, "returncode": "failed"},
            ),
            CommandFailedError,
            "CliAgentExecutionError",
            "",
        ),
        (
            CommandFailure(
                command=True,
                type="CommandError",
                message="m",
                cli_agent="codex",
                cli_agent_result={**_REFUSED, "error_details": "slow down"},
            ),
            CommandError,
            "CommandError",
            "",
        ),
    ],
)
async def test_a_failure_in_the_environment_is_rebuilt_as_the_host_knows_it(
    monkeypatch, failure, raised, error_type, code
):
    """A command's own failure is a failed command; anything else is named by
    what it raised there; and the AI CLI tool's failure it came from reaches
    the host whole, so a rate limit or a refused login is told apart."""
    from guildbotics.capabilities.command_failures import command_failure_payload
    from guildbotics.capabilities.workflow_rate_limits import (
        workflow_rate_limit_from_exception,
    )

    _environment(monkeypatch, CommandReply(failure=failure))

    with pytest.raises(raised) as failed:
        await command_runner.run_in_environment(_prepared())

    assert type(failed.value) is raised
    assert command_failure_payload(failed.value) == {
        "error_type": error_type,
        "code": code,
    }
    limit = workflow_rate_limit_from_exception(failed.value)
    if failure.cli_agent_result == _REFUSED:
        assert limit is not None
        assert limit.retry_after_at == "2026-09-27T10:00:00+09:00"
    else:
        assert limit is None


@pytest.mark.parametrize(
    ("cwd", "resolved"),
    [
        ("/elsewhere", "/elsewhere"),
        ("../outside", "/outside"),
        ("/workspace/../etc", "/etc"),
        ("secret/deeper", "/workspace/secret/deeper"),
    ],
)
def test_a_subcommand_works_only_where_the_environment_lets_one_work(cwd, resolved):
    """A subcommand's working directory outside what the environment lets a
    command work in -- beyond what it mounted, or under a corner it covers --
    holds nothing of the host: it is refused, not run in the microVM's own."""
    from guildbotics.utils.i18n_tool import t

    factory = CommandSpecFactory(
        DummyContext(), {"/workspace": True, "/workspace/secret": False}
    )

    with pytest.raises(CommandError) as refused:
        factory._resolve_cwd(cwd, Path("/workspace"))

    assert str(refused.value) == t(
        "intelligences.agent_environment.runtime.outside_mounts",
        path=Path(resolved),
    )
    assert factory._resolve_cwd("child", Path("/workspace")) == Path("/workspace/child")
