import logging
from datetime import timedelta

import pytest

from guildbotics.intelligences.agent_runtime.host_client import (
    TURN_WORKING_DIRECTORY,
    IoEntry,
    SummaryEntry,
)
from guildbotics.intelligences.brains import cli_agent
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT
from tests.guildbotics.intelligences.agent_runtime.window_doubles import (
    WindowDouble,
    enter_command,
)
from tests.guildbotics.slot_mappings import use_cli_agent_slots


@pytest.fixture(autouse=True)
def window(monkeypatch, tmp_path) -> WindowDouble:
    """Every turn here runs inside a command's environment, whose window to
    the host keeps what the turn records."""
    double = WindowDouble(tmp_path)
    enter_command(monkeypatch, double, person_id="p1")
    return double


def _io(window: WindowDouble) -> list[IoEntry]:
    return [entry for entry in window.entries if isinstance(entry, IoEntry)]


def _spans(window: WindowDouble) -> list[SummaryEntry]:
    return [entry for entry in window.entries if isinstance(entry, SummaryEntry)]


def _test_logger():
    return type(
        "L",
        (),
        {
            "debug": lambda *args, **kwargs: None,
            "info": lambda *args, **kwargs: None,
            "warning": lambda *args, **kwargs: None,
            "error": lambda *args, **kwargs: None,
        },
    )()


_stub_logger = _test_logger


def _native_brain(monkeypatch, result: cli_agent.CliAgentExecutionResult, **kwargs):
    """A brain on the native path whose provider turn returns ``result``.

    The turn itself belongs to the adapters (and is covered by their own tests);
    what is exercised here is everything the brain does around it.
    """
    turns: list[str] = []

    async def fake_execute_native_turn(self, *, input, **_kwargs):
        turns.append(input)
        return result

    monkeypatch.setattr(
        cli_agent.CliAgentBrain, "_execute_native_turn", fake_execute_native_turn
    )
    use_cli_agent_slots(
        monkeypatch,
        "p1",
        {"default": cli_agent.ExecutableInfo(adapter="claude", **kwargs)},
    )
    return turns


def _read_only_state(tmp_path) -> dict:
    """Session state for a turn of the command's own run."""
    return {"agent_execution_context": {"run_id": "command-run", "work_kind": "manual"}}


@pytest.mark.parametrize(
    ("mapping_value", "adapter"),
    [
        ("cli_agents/codex/default.yml", "codex"),
        ("cli_agents/codex/reviewer.yml", "codex"),
        ("cli_agents/claude/default.yml", "claude"),
    ],
)
def test_cli_agent_mapping_selects_the_tools_adapter(
    monkeypatch, mapping_value, adapter
) -> None:
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"default": mapping_value},
    )

    resolved = cli_agent.get_cli_agent_mapping("aiko")

    assert resolved["default"].adapter == adapter


def test_cli_agent_mapping_rejects_a_tool_outside_the_catalog(monkeypatch) -> None:
    """A mapping no adapter can run fails at load, naming the slot."""
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"default": "cli_agents/mytool/default.yml"},
    )

    with pytest.raises(ValueError, match=r"slot 'default'.*mytool"):
        cli_agent.get_cli_agent_mapping("aiko")


@pytest.mark.asyncio
async def test_cli_agent_run_returns_the_provider_output(monkeypatch, tmp_path):
    _native_brain(
        monkeypatch,
        cli_agent.CliAgentExecutionResult(stdout="done", stderr="", returncode=0),
    )

    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())
    output = await brain.run(
        "hello", cwd=tmp_path, session_state=_read_only_state(tmp_path)
    )

    assert output == "done"


@pytest.mark.asyncio
async def test_cli_agent_execution_details_include_stderr_and_returncode(
    monkeypatch, tmp_path
):
    _native_brain(
        monkeypatch,
        cli_agent.CliAgentExecutionResult(
            stdout="", stderr="login required", returncode=2
        ),
    )

    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())
    result = await brain.run_with_execution_details(
        "hello", cwd=tmp_path, session_state=_read_only_state(tmp_path)
    )

    assert result.stdout == ""
    assert result.stderr == "login required"
    assert result.returncode == 2


@pytest.mark.asyncio
async def test_cli_agent_run_raises_when_the_tool_fails(monkeypatch, tmp_path):
    _native_brain(
        monkeypatch,
        cli_agent.CliAgentExecutionResult(stdout="", stderr="bad option", returncode=2),
    )

    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())
    with pytest.raises(cli_agent.CliAgentExecutionError) as excinfo:
        await brain.run("hello", cwd=tmp_path, session_state=_read_only_state(tmp_path))

    # The brain never sees the tool's process, so the message must not dress
    # the reason up as an exit code: a device that refused the turn before any
    # process started reaches here through the same path.
    assert str(excinfo.value) == "AI CLI tool 'default' failed: bad option"


@pytest.mark.asyncio
async def test_cli_agent_run_raises_when_response_is_empty(monkeypatch, tmp_path):
    _native_brain(
        monkeypatch,
        cli_agent.CliAgentExecutionResult(
            stdout="", stderr="usage error", returncode=0
        ),
    )

    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())
    with pytest.raises(cli_agent.CliAgentExecutionError, match="produced no response"):
        await brain.run("hello", cwd=tmp_path, session_state=_read_only_state(tmp_path))


@pytest.mark.asyncio
async def test_cli_agent_run_raises_rate_limit_error_carrying_retry_after(
    monkeypatch, tmp_path
):
    _native_brain(
        monkeypatch,
        cli_agent.CliAgentExecutionResult(
            stdout="",
            stderr="rate limited",
            returncode=1,
            error_category="rate_limited",
            error_details={
                "cli_agent": "claude",
                "retry_after_at": "2026-07-08T11:44:00+09:00",
            },
        ),
    )

    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())
    with pytest.raises(cli_agent.CliAgentExecutionError) as excinfo:
        await brain.run("hello", cwd=tmp_path, session_state=_read_only_state(tmp_path))

    assert excinfo.value.category == "rate_limited"
    assert excinfo.value.details["retry_after_at"] == "2026-07-08T11:44:00+09:00"


@pytest.mark.asyncio
async def test_cli_agent_authentication_failure_records_credential_failure(
    monkeypatch, tmp_path, window
):
    _native_brain(
        monkeypatch,
        cli_agent.CliAgentExecutionResult(
            stdout="",
            stderr="not logged in",
            returncode=1,
            error_category="authentication",
            error_details={"cli_agent": "claude"},
        ),
    )

    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())
    with pytest.raises(cli_agent.CliAgentExecutionError) as excinfo:
        await brain.run("hello", cwd=tmp_path, session_state=_read_only_state(tmp_path))

    assert excinfo.value.category == "authentication"
    assert [(entry.tool, entry.failed) for entry in window.credentials()] == [
        ("claude", True)
    ]


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("Resets in 2h30m15s", 9015),
        ("Resets in 25h57m34s", 93454),
        ("Resets in 1h23m", 4980),
        ("Resets in 2m15s", 135),
        ("Resets in 2H 30M 15S", 9015),
        ("Please wait 1 hour 2 minutes 3 seconds", 3723),
        ("Please wait 30 seconds", 30),
        ("Resets in 0s", 0),
        ("Resets in 2months", None),
        ("2h30m15s", None),
    ],
)
def test_relative_retry_duration(text, seconds):
    expected = timedelta(seconds=seconds) if seconds is not None else None
    assert cli_agent._parse_relative_retry_delta(text) == expected


def test_normalize_retry_after_handles_date_text():
    retry_after_at = cli_agent.normalize_cli_agent_retry_after(
        "reset on July 8, 2026 at 11:44 AM",
        "Asia/Tokyo",
    )

    assert retry_after_at == "2026-07-08T11:44:00+09:00"


def test_normalize_native_retry_after_handles_provider_text():
    details = {
        "retry_after_text": "resets 12:50pm (Asia/Tokyo)",
        "retry_after_timezone": "Asia/Tokyo",
    }

    cli_agent._normalize_native_retry_after(details)

    assert details["retry_after_at"].endswith("T12:50:00+09:00")


@pytest.mark.asyncio
async def test_cli_agent_records_request_response_and_span(
    monkeypatch, tmp_path, window
):
    multibyte_stderr = "あ" * 2731
    _native_brain(
        monkeypatch,
        cli_agent.CliAgentExecutionResult(
            stdout="done",
            stderr=multibyte_stderr,
            returncode=0,
            model="claude-sonnet-5",
            effort="high",
        ),
    )

    brain = cli_agent.CliAgentBrain(
        "p1",
        "functions/handle_chat_event",
        logger=_test_logger(),
        description="Reply as {{ context.person.name }}.",
        template_engine="jinja2",
    )
    state = _read_only_state(tmp_path)
    state["context"] = type("C", (), {"person": type("P", (), {"name": "Alice"})()})()
    await brain.run("hello", cwd=tmp_path, session_state=state)

    request, response = _io(window)
    assert (request.io_type, response.io_type) == (
        "cli_agent.request",
        "cli_agent.response",
    )
    assert request.payload["person_id"] == "p1"
    assert request.payload["brain"] == "functions/handle_chat_event"
    assert "Reply as Alice." in request.payload["prompt"]
    assert response.payload["stdout"] == "done"
    # The whole stderr goes to the host, which keeps what it records of it.
    assert response.payload["stderr"] == multibyte_stderr
    (span,) = _spans(window)
    assert span.status == "finished"
    # The span names what the turn really ran with, and keeps the slot name
    # apart so traces stay searchable by slot.
    assert (span.model, span.effort, span.slot) == (
        "claude-sonnet-5",
        "high",
        "default",
    )
    # Each record is sent under the turn's span.
    assert request.span == response.span == span.span is not None


@pytest.mark.asyncio
async def test_an_unknown_model_stays_empty_in_the_span(monkeypatch, tmp_path, window):
    """The slot name lives on ``agent.slot``; it must not pose as the model."""
    _native_brain(
        monkeypatch,
        cli_agent.CliAgentExecutionResult(stdout="done", stderr="", returncode=0),
    )

    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())
    await brain.run("hello", cwd=tmp_path, session_state=_read_only_state(tmp_path))

    (span,) = _spans(window)
    assert (span.model, span.effort, span.slot) == ("", "", "default")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["finished", "failed", "exception"])
@pytest.mark.parametrize(
    ("parameters", "levels", "configured", "expected"),
    [
        ({"model": "slot-model"}, {}, {}, "slot-model"),
        (
            {"model": "slot-model"},
            {"high": {"model": "effort-model"}},
            {},
            "effort-model",
        ),
        ({}, {}, {"model": "context-model"}, "context-model"),
        ({}, {}, {}, ""),
    ],
)
async def test_span_records_the_model_selection_sent_to_the_turn(
    monkeypatch, tmp_path, window, parameters, levels, configured, expected, outcome
):
    _native_brain(
        monkeypatch,
        cli_agent.CliAgentExecutionResult(stdout="done", stderr="", returncode=0),
        parameters=parameters,
        effort=levels,
    )
    requested = []

    async def execute(self, *, context, **_kwargs):
        requested.append(context.model)
        if outcome == "exception":
            raise RuntimeError("provider unavailable")
        return cli_agent.CliAgentExecutionResult(
            stdout="done",
            stderr="",
            returncode=int(outcome == "failed"),
            model="actual-model",
        )

    monkeypatch.setattr(cli_agent.CliAgentBrain, "_execute_native_turn", execute)
    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger(), effort="high")
    call = brain.run_with_execution_details(
        "hello", cwd=tmp_path, session_state={"agent_execution_context": configured}
    )
    if outcome == "exception":
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await call
    else:
        await call

    (span,) = _spans(window)
    assert requested == [expected]
    assert span.model_specified is bool(expected)
    assert span.tool == "claude"
    assert span.model == ("" if outcome == "exception" else "actual-model")
    assert span.status == ("finished" if outcome == "finished" else "failed")


@pytest.mark.asyncio
async def test_a_record_the_host_refuses_does_not_undo_the_turn(
    monkeypatch, tmp_path, window, caplog
):
    """The turn's work is what it did: a record the host refuses (one too
    large to take, say) is logged, and the turn's result stands."""
    from guildbotics.intelligences.agent_runtime.host_client import HostCallError

    _native_brain(
        monkeypatch,
        cli_agent.CliAgentExecutionResult(stdout="done", stderr="", returncode=0),
    )

    def refuse(name, **arguments):
        raise HostCallError("refused", "The call is too large.")

    monkeypatch.setattr(window, "call", refuse)
    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())

    with caplog.at_level(logging.WARNING, logger="guildbotics"):
        result = await brain.run_with_execution_details(
            "hello", cwd=tmp_path, session_state=_read_only_state(tmp_path)
        )

    assert (result.returncode, result.stdout) == (0, "done")
    assert "The call is too large." in caplog.text


@pytest.mark.asyncio
async def test_a_failed_turn_records_a_failed_span_without_effective_values(
    monkeypatch, tmp_path, window
):
    async def failing_turn(self, *, input, **_kwargs):
        raise RuntimeError("provider is unreachable")

    monkeypatch.setattr(cli_agent.CliAgentBrain, "_execute_native_turn", failing_turn)
    use_cli_agent_slots(
        monkeypatch,
        "p1",
        {"default": cli_agent.ExecutableInfo(adapter="claude")},
    )

    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())
    with pytest.raises(RuntimeError):
        await brain.run("hello", cwd=tmp_path, session_state=_read_only_state(tmp_path))

    (span,) = _spans(window)
    assert (span.status, span.model, span.effort, span.slot) == (
        "failed",
        "",
        "",
        "default",
    )


@pytest.mark.asyncio
async def test_execution_details_carry_the_effective_model_and_effort(
    monkeypatch, tmp_path, window
):
    _native_brain(
        monkeypatch,
        cli_agent.CliAgentExecutionResult(
            stdout="done",
            stderr="",
            returncode=0,
            model="gpt-5-codex",
            effort="low",
        ),
    )

    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())
    result = await brain.run_with_execution_details(
        "hello", cwd=tmp_path, session_state=_read_only_state(tmp_path)
    )

    assert (result.model, result.effort) == ("gpt-5-codex", "low")
    (span,) = _spans(window)
    assert (span.model, span.effort) == ("gpt-5-codex", "low")


@pytest.mark.asyncio
@pytest.mark.parametrize("trace_id", ["", "trace-7"])
async def test_an_asking_response_points_at_the_commands_trace(
    monkeypatch, tmp_path, window, trace_id
):
    """A member who asks back leaves the command's trace for the reader to
    look into, when the command has one."""
    from guildbotics.intelligences.common import AgentResponse
    from guildbotics.utils.i18n_tool import t

    enter_command(monkeypatch, window, person_id="p1", trace_id=trace_id)
    _native_brain(
        monkeypatch,
        cli_agent.CliAgentExecutionResult(
            stdout='{"status": "asking", "message": "need input"}',
            stderr="",
            returncode=0,
        ),
    )

    brain = cli_agent.CliAgentBrain(
        "p1", "x", logger=_test_logger(), response_class=AgentResponse
    )
    output = await brain.run(
        "hello", cwd=tmp_path, session_state=_read_only_state(tmp_path)
    )

    assert isinstance(output, AgentResponse)
    assert output.status == AgentResponse.ASKING
    reference = (
        "\n\n" + t("intelligences.cli_agent.trace_reference", trace_id=trace_id)
        if trace_id
        else ""
    )
    assert output.message == f"need input{reference}"


@pytest.mark.parametrize(
    ("current", "persisted", "expected"),
    [
        ("101.2", "101.1", "newer"),
        ("101.1", "101.1", "equal"),
        ("100.9", "101.1", "older"),
        ("2", "10", "older"),
        ("same-text", "same-text", "equal"),
        ("text-a", "text-b", "unknown"),
        ("", "101.1", "unknown"),
        ("101.1", "", "unknown"),
        ("", "", "unknown"),
    ],
)
def test_cursor_relation_orders_numeric_and_rejects_unorderable(
    current, persisted, expected
):
    assert cli_agent._cursor_relation(current, persisted) == expected


@pytest.mark.asyncio
async def test_a_default_effort_turn_states_no_settings(
    monkeypatch, tmp_path, window
) -> None:
    """`default` cancels the frontmatter but still imposes nothing downstream.

    Stating the level here would impose its mapped settings, despite `default`
    asking for no effort overlay on this turn.
    """
    captured: dict = {}

    async def fake_execute_native_turn(self, *, input, configured, context, **_kwargs):
        captured["context"] = context
        return cli_agent.CliAgentExecutionResult(
            stdout="answer", stderr="", returncode=0
        )

    monkeypatch.setattr(
        cli_agent.CliAgentBrain, "_execute_native_turn", fake_execute_native_turn
    )
    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger(), effort="high")
    brain.executable_info = cli_agent.ExecutableInfo(
        adapter="claude-stream-json",
        effort={"high": {"model": "big-model"}},
    )

    await brain._execute(
        input="hello",
        cwd=tmp_path,
        kwargs={
            "session_state": {
                "effort": "default",
                "agent_execution_context": {
                    "run_id": "run-9",
                    "work_kind": "troubleshooting",
                },
            }
        },
        effort=brain._resolve_provider_effort({"session_state": {"effort": "default"}}),
        records=cli_agent._TurnRecords(window),
        model="",
    )

    context = captured["context"]
    assert context.effort == ""
    assert context.provider_options == {}


@pytest.mark.asyncio
async def test_an_unmapped_effort_level_is_not_claimed_as_the_turns_effort(
    monkeypatch, tmp_path, window
) -> None:
    """A level with an empty overlay imposed nothing of its own.

    The baseline settings still apply, but attributing them to the level would
    let the turn report an effort it never translated into provider settings.
    """
    captured: dict = {}

    async def fake_execute_native_turn(self, *, input, configured, context, **_kwargs):
        captured["context"] = context
        return cli_agent.CliAgentExecutionResult(
            stdout="answer", stderr="", returncode=0
        )

    monkeypatch.setattr(
        cli_agent.CliAgentBrain, "_execute_native_turn", fake_execute_native_turn
    )
    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger(), effort="high")
    brain.executable_info = cli_agent.ExecutableInfo(
        adapter="claude-stream-json",
        parameters={"model": "base-model"},
    )

    await brain._execute(
        input="hello",
        cwd=tmp_path,
        kwargs={
            "session_state": {
                "agent_execution_context": {
                    "run_id": "run-9",
                    "work_kind": "troubleshooting",
                },
            }
        },
        effort=brain._resolve_provider_effort({"session_state": {}}),
        records=cli_agent._TurnRecords(window),
        model="base-model",
    )

    context = captured["context"]
    assert context.effort == ""
    assert context.provider_options == {"model": "base-model"}


# --------------------------------------------------------------------------- #
# Effort: mapping discovery and the settings a turn hands the adapter
# --------------------------------------------------------------------------- #


def _write_definition(root, relative: str, body: str):
    path = root / "intelligences" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_a_tool_reads_its_own_definition(monkeypatch, tmp_path) -> None:
    _write_definition(
        tmp_path, "cli_agents/codex/default.yml", "effort:\n  high:\n    effort: high\n"
    )
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"default": "cli_agents/codex/default.yml"},
    )

    resolved = cli_agent.get_cli_agent_mapping("aiko")

    assert resolved["default"].adapter == "codex"
    assert resolved["default"].effort == {"high": {"effort": "high"}}


def test_two_slots_on_one_tool_keep_their_own_settings(monkeypatch, tmp_path) -> None:
    """The reason slots exist: two features on one tool, configured apart.

    Before definitions were per-slot, both slots read the same tool file and
    could not differ at all.
    """
    _write_definition(
        tmp_path,
        "cli_agents/codex/default.yml",
        "effort:\n  high:\n    model: strong\n",
    )
    _write_definition(
        tmp_path,
        "cli_agents/codex/reviewer.yml",
        "effort:\n  high:\n    model: cheap\n",
    )
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {
            "default": "cli_agents/codex/default.yml",
            "reviewer": "cli_agents/codex/reviewer.yml",
        },
    )

    resolved = cli_agent.get_cli_agent_mapping("aiko")

    assert resolved["default"].effort == {"high": {"model": "strong"}}
    assert resolved["reviewer"].effort == {"high": {"model": "cheap"}}
    # Both still run on the same adapter.
    assert {info.adapter for info in resolved.values()} == {"codex"}


def test_a_slot_inherits_the_keys_it_does_not_state(monkeypatch, tmp_path) -> None:
    _write_definition(
        tmp_path,
        "cli_agents/codex/default.yml",
        "parameters:\n  model: steady\neffort:\n  high:\n    effort: high\n",
    )
    _write_definition(
        tmp_path,
        "cli_agents/codex/writer.yml",
        "effort:\n  high:\n    effort: max\n",
    )
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"writer": "cli_agents/codex/writer.yml"},
    )

    resolved = cli_agent.get_cli_agent_mapping("aiko")

    # Its own effort wins; the baseline parameters come from the tool's default.
    assert resolved["writer"].effort == {"high": {"effort": "max"}}
    assert resolved["writer"].parameters == {"model": "steady"}
    assert resolved["writer"].adapter == "codex"


@pytest.mark.asyncio
async def test_runtime_effort_reaches_the_adapter_settings(monkeypatch, tmp_path):
    """`guildbotics run <command> effort=high` arrives via session_state."""
    captured: dict = {}

    async def fake_execute_native_turn(self, *, input, configured, context, **_kwargs):
        captured["context"] = context
        return cli_agent.CliAgentExecutionResult(stdout="done", stderr="", returncode=0)

    monkeypatch.setattr(
        cli_agent.CliAgentBrain, "_execute_native_turn", fake_execute_native_turn
    )
    use_cli_agent_slots(
        monkeypatch,
        "p1",
        {
            "default": cli_agent.ExecutableInfo(
                adapter="claude",
                effort={"high": {"model": "big-model", "verbosity": "high"}},
            )
        },
    )

    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())
    state = _read_only_state(tmp_path)
    state["effort"] = "high"
    await brain.run("hello", cwd=tmp_path, session_state=state)

    assert captured["context"].model == "big-model"
    assert captured["context"].effort == "high"
    assert captured["context"].provider_options == {
        "model": "big-model",
        "verbosity": "high",
    }


@pytest.mark.asyncio
async def test_frontmatter_effort_reaches_the_adapter_settings(monkeypatch, tmp_path):
    captured: dict = {}

    async def fake_execute_native_turn(self, *, input, configured, context, **_kwargs):
        captured["context"] = context
        return cli_agent.CliAgentExecutionResult(stdout="done", stderr="", returncode=0)

    monkeypatch.setattr(
        cli_agent.CliAgentBrain, "_execute_native_turn", fake_execute_native_turn
    )
    use_cli_agent_slots(
        monkeypatch,
        "p1",
        {
            "default": cli_agent.ExecutableInfo(
                adapter="claude", effort={"high": {"model": "big-model"}}
            )
        },
    )

    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger(), effort="high")
    await brain.run("hello", cwd=tmp_path, session_state=_read_only_state(tmp_path))

    assert captured["context"].model == "big-model"


@pytest.mark.asyncio
async def test_request_diagnostics_record_effort_keys_not_values(
    monkeypatch, tmp_path, window
) -> None:
    _native_brain(
        monkeypatch,
        cli_agent.CliAgentExecutionResult(stdout="done", stderr="", returncode=0),
        effort={"high": {"token": "secret-value"}},
    )

    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger(), effort="high")
    await brain.run("hello", cwd=tmp_path, session_state=_read_only_state(tmp_path))

    effort_payload = _io(window)[0].payload["effort"]
    assert effort_payload["resolved"] == "high"
    assert effort_payload["applied_keys"] == ["token"]
    assert "secret-value" not in str(effort_payload)


def test_a_tools_own_settings_apply_whatever_effort_was_asked_for(
    monkeypatch, tmp_path
) -> None:
    """`default` is the common case, so a model must not depend on a level.

    Without a baseline the only place to name a model was inside an effort
    level, which left every `default` turn -- every ordinary chat reply --
    unable to state one at all.
    """
    _write_definition(
        tmp_path,
        "cli_agents/codex/default.yml",
        "parameters:\n  model: steady\neffort:\n  high:\n    model: stronger\n",
    )
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"default": "cli_agents/codex/default.yml"},
    )
    brain = cli_agent.CliAgentBrain("aiko", "x", logger=_stub_logger())
    models = {
        requested: brain._resolve_provider_effort(
            {"session_state": {"effort": requested} if requested else {}}
        ).model
        for requested in ("high", "default", "")
    }

    assert models == {"high": "stronger", "default": "steady", "": "steady"}


def test_diagnostics_reflect_the_level_not_the_baseline(monkeypatch, tmp_path) -> None:
    """A baseline must not disguise an unmapped level as a supported one.

    The tool still runs with its standing settings, but the *effort decision*
    contributed nothing — diagnostics have to say so, exactly as the LLM API
    path does, or an ignored `high` would look applied.
    """
    _write_definition(
        tmp_path,
        "cli_agents/codex/default.yml",
        "parameters:\n  model: steady\neffort:\n  low:\n    effort: low\n",
    )
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"default": "cli_agents/codex/default.yml"},
    )
    brain = cli_agent.CliAgentBrain("aiko", "x", logger=_stub_logger())
    unmapped = brain._resolve_provider_effort({"session_state": {"effort": "high"}})
    mapped = brain._resolve_provider_effort({"session_state": {"effort": "low"}})

    unmapped_payload = unmapped.diagnostics()
    assert unmapped_payload["unsupported"] is True
    assert unmapped_payload["applied_keys"] == []
    # The tool itself still runs on its standing settings.
    assert unmapped.provider_options == {"model": "steady"}
    assert unmapped_payload["model"] == "steady"

    mapped_payload = mapped.diagnostics()
    assert mapped_payload["unsupported"] is False
    # Only the level's own contribution counts as applied, not the baseline.
    assert mapped_payload["applied_keys"] == ["effort"]


def test_a_tool_definition_network_block_is_ignored(monkeypatch, tmp_path) -> None:
    _write_definition(
        tmp_path,
        "cli_agents/codex/default.yml",
        "network:\n  mode: allowlist\n  allowed_domains: [registry.npmjs.org]\n"
        "  allow_local_network: false\n",
    )
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"writer": "cli_agents/codex/default.yml"},
    )

    resolved = cli_agent.get_cli_agent_mapping("aiko")

    assert resolved["writer"] == cli_agent.ExecutableInfo(adapter="codex")


@pytest.mark.asyncio
async def test_a_turn_the_provider_answered_records_the_credential_as_verified(
    monkeypatch, tmp_path, window
):
    """The counterpart of the refusal: the same tool, so the credential alert
    the refusal opened closes on the next successful turn."""
    _native_brain(
        monkeypatch,
        cli_agent.CliAgentExecutionResult(stdout="done", stderr="", returncode=0),
    )

    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())
    await brain.run("hello", cwd=tmp_path, session_state=_read_only_state(tmp_path))

    assert [(entry.tool, entry.failed) for entry in window.credentials()] == [
        ("claude", False)
    ]


@pytest.mark.asyncio
async def test_the_workflow_path_records_the_credential_outcome_too(
    monkeypatch, tmp_path, window
):
    """`run_with_execution_details` leaves the judgement to the workflow but
    still records what the turn proved, so a Slack or ticket turn opens and
    closes the credential alert like a command does."""
    _native_brain(
        monkeypatch,
        cli_agent.CliAgentExecutionResult(
            stdout="",
            stderr="not logged in",
            returncode=1,
            error_category="authentication",
        ),
    )
    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())
    await brain.run_with_execution_details(
        "hello", cwd=tmp_path, session_state=_read_only_state(tmp_path)
    )
    assert [entry.failed for entry in window.credentials()] == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "category,returncode,expected",
    [
        ("authentication", 1, [True]),
        ("network", 1, []),
        ("rate_limited", 1, []),
        ("", 2, []),
        ("", 0, [False]),
    ],
)
async def test_only_what_a_turn_proves_of_its_login_is_recorded(
    monkeypatch, tmp_path, window, category, returncode, expected
):
    """A refused login fails the tool's login, an answer verifies it, and any
    other failure says nothing about it."""
    _native_brain(
        monkeypatch,
        cli_agent.CliAgentExecutionResult(
            stdout="done", stderr="", returncode=returncode, error_category=category
        ),
    )
    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())

    await brain.run_with_execution_details(
        "hello", cwd=tmp_path, session_state=_read_only_state(tmp_path)
    )

    assert [entry.failed for entry in window.credentials()] == expected


@pytest.mark.asyncio
async def test_a_turn_says_where_it_works_for_the_host_to_record_its_confinement(
    tmp_path, monkeypatch, window
):
    """The turn's start names where it works; what the environment confines
    it to there is the host's to record, whatever provider runs it."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from guildbotics.intelligences.agent_runtime.models import (
        AgentTerminalResult,
        ConversationKey,
    )

    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    adapter = SimpleNamespace(
        run_turn=AsyncMock(
            return_value=AgentTerminalResult(
                output="answer", events=(), provider_session_id="s"
            )
        ),
        close=AsyncMock(),
    )
    monkeypatch.setattr(cli_agent, "create_native_adapter", lambda _name: adapter)
    brain = cli_agent.CliAgentBrain("aiko", "troubleshoot", _test_logger())
    context = cli_agent.AgentExecutionContext(
        person_id="aiko",
        run_id="turn",
        cwd=tmp_path,
        conversation_key=ConversationKey("aiko", "grok", "troubleshooting", "c1"),
    )

    records = cli_agent._TurnRecords(window)
    await brain._execute_native_turn(
        input="why?",
        configured={},
        context=context,
        adapter_name="grok",
        records=records,
    )
    await records.flush()

    started = next(event for event in window.events() if event.name == "started")
    assert started.details[TURN_WORKING_DIRECTORY] == str(tmp_path)
    assert "requested_policy" not in started.details


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", ["", "log in again"])
@pytest.mark.parametrize("tool_says", ["answer", "failure", "crash"])
async def test_a_turn_whose_lent_login_was_refused_fails_as_authentication(
    tmp_path, monkeypatch, window, refused, tool_says
):
    """The tool meets a refused login only in what the gateway answers, and
    reports it as it likes -- as an answer, or as some other failure, or
    not at all. The
    turn is an authentication failure all the same, and what the tool said
    goes along with it."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from guildbotics.intelligences.agent_runtime.models import (
        AgentRuntimeError,
        AgentRuntimeErrorCategory,
        AgentTerminalResult,
        ConversationKey,
    )

    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    use_cli_agent_slots(
        monkeypatch,
        "judge",
        {"default": cli_agent.ExecutableInfo(adapter="copilot")},
    )

    async def run_turn(prompt, context, conversation, emit):
        context.login.refusal = lambda: refused  # As the environment tells it.
        if tool_says == "failure":
            raise AgentRuntimeError(
                AgentRuntimeErrorCategory.PROCESS, "error sending request"
            )
        if tool_says == "crash":
            raise TimeoutError("no answer")
        return AgentTerminalResult(
            output="Error: Execution failed", events=(), provider_session_id="s"
        )

    adapter = SimpleNamespace(run_turn=run_turn, close=AsyncMock())

    monkeypatch.setattr(cli_agent, "create_native_adapter", lambda _name: adapter)
    brain = cli_agent.CliAgentBrain("judge", "chat_decision", _test_logger())
    context = cli_agent.AgentExecutionContext(
        person_id="judge",
        run_id="turn",
        cwd=tmp_path,
        conversation_key=ConversationKey("judge", "copilot", "manual", "turn"),
    )

    turn = brain._execute_native_turn(
        input="work",
        configured={},
        context=context,
        adapter_name="copilot",
        records=cli_agent._TurnRecords(window),
    )
    if tool_says == "crash" and not refused:
        with pytest.raises(TimeoutError):
            await turn
        return
    result = await turn

    said = {
        "answer": "Error: Execution failed",
        "failure": "error sending",
        "crash": "no answer",
    }[tool_says]
    if refused:
        assert result.error_category == "authentication"
        assert result.stderr.startswith("log in again")
        assert said in result.stderr
    elif tool_says == "answer":
        assert result.error_category == "" and result.stdout == said
    else:
        assert result.error_category == "process"
