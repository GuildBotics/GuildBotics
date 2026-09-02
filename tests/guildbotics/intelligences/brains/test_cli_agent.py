import logging

import pytest

from guildbotics.intelligences.brains import cli_agent
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT


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
    monkeypatch.setitem(
        cli_agent.person_cli_agent_mapping,
        "p1",
        {"default": cli_agent.ExecutableInfo(adapter="claude", **kwargs)},
    )
    return turns


def _read_only_state(tmp_path) -> dict:
    """Session state for a turn that takes no lease and touches no real home."""
    return {
        "agent_execution_context": {
            "run_id": "run-1",
            "work_kind": "manual",
            "workspace_data_root": str(tmp_path),
            "read_only": True,
        }
    }


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
    cli_agent.person_cli_agent_mapping.clear()
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"default": mapping_value},
    )

    resolved = cli_agent.get_cli_agent_mapping("aiko")

    assert resolved["default"].adapter == adapter
    cli_agent.person_cli_agent_mapping.clear()


def test_cli_agent_mapping_rejects_a_tool_outside_the_catalog(monkeypatch) -> None:
    """A mapping no adapter can run fails at load, naming the slot."""
    cli_agent.person_cli_agent_mapping.clear()
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"default": "cli_agents/mytool/default.yml"},
    )

    with pytest.raises(ValueError, match=r"slot 'default'.*mytool"):
        cli_agent.get_cli_agent_mapping("aiko")

    assert "aiko" not in cli_agent.person_cli_agent_mapping


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
    with pytest.raises(cli_agent.CliAgentExecutionError, match="bad option"):
        await brain.run("hello", cwd=tmp_path, session_state=_read_only_state(tmp_path))


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
    monkeypatch, tmp_path
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
    recorded: list[dict] = []
    monkeypatch.setattr(
        cli_agent,
        "record_correlated_event",
        lambda **kwargs: recorded.append(kwargs),
    )

    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())
    with pytest.raises(cli_agent.CliAgentExecutionError) as excinfo:
        await brain.run("hello", cwd=tmp_path, session_state=_read_only_state(tmp_path))

    assert excinfo.value.category == "authentication"
    assert recorded[0]["event_type"] == "credential.failed"
    assert recorded[0]["payload"] == {
        "provider": "cli_agent",
        "cli_agent": "claude",
        "person_id": "p1",
        "code": "authentication",
    }
    assert recorded[0]["attributes"]["credential.provider"] == "cli_agent"
    assert recorded[0]["attributes"]["credential.cli_agent"] == "claude"


def test_normalize_retry_after_handles_composite_relative_duration():
    retry_after_at = cli_agent.normalize_cli_agent_retry_after("Resets in 2h30m15s")

    assert retry_after_at


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
async def test_cli_agent_records_request_response_and_span(monkeypatch, tmp_path):
    io_records: list[tuple[str, dict]] = []
    span_records: list[dict] = []
    multibyte_stderr = "あ" * 2731
    monkeypatch.delenv("GUILDBOTICS_TRANSCRIPT_DETAIL", raising=False)
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
    monkeypatch.setattr(
        cli_agent,
        "record_correlated_io",
        lambda *, io_type, payload: io_records.append((io_type, payload)),
    )
    monkeypatch.setattr(
        cli_agent,
        "record_span_summary",
        lambda **kwargs: span_records.append(kwargs),
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

    assert [record[0] for record in io_records] == [
        "cli_agent.request",
        "cli_agent.response",
    ]
    assert io_records[0][1]["person_id"] == "p1"
    assert io_records[0][1]["brain"] == "functions/handle_chat_event"
    assert "Reply as Alice." in io_records[0][1]["prompt"]
    assert io_records[1][1]["stdout"] == "done"
    assert io_records[1][1]["stderr"] != multibyte_stderr
    assert io_records[1][1]["stderr_truncated"] is True
    assert span_records[0]["status"] == "finished"
    # The span names what the turn really ran with, and keeps the slot name on
    # an attribute so traces stay searchable by slot.
    assert span_records[0]["model"] == "claude-sonnet-5"
    assert span_records[0]["effort"] == "high"
    assert span_records[0]["attributes"] == {
        "agent.kind": "cli_agent",
        "agent.slot": "default",
    }


@pytest.mark.asyncio
async def test_an_unknown_model_stays_empty_in_the_span(monkeypatch, tmp_path, caplog):
    """The slot name lives on ``agent.slot``; it must not pose as the model."""
    span_records: list[dict] = []
    _native_brain(
        monkeypatch,
        cli_agent.CliAgentExecutionResult(stdout="done", stderr="", returncode=0),
    )
    monkeypatch.setattr(
        cli_agent,
        "record_span_summary",
        lambda **kwargs: span_records.append(kwargs),
    )

    brain = cli_agent.CliAgentBrain("p1", "x", logger=logging.getLogger("test"))
    with caplog.at_level(logging.INFO, logger="test"):
        await brain.run("hello", cwd=tmp_path, session_state=_read_only_state(tmp_path))

    assert span_records[0]["model"] == ""
    assert span_records[0]["effort"] == ""
    assert span_records[0]["attributes"]["agent.slot"] == "default"
    # The log line states only what is known, so an unknown model is absent
    # rather than reported as the slot name.
    assert "cli_agent 'default' finished:" in caplog.text
    assert "model=" not in caplog.text


@pytest.mark.asyncio
async def test_a_failed_turn_records_a_failed_span_without_effective_values(
    monkeypatch, tmp_path
):
    span_records: list[dict] = []

    async def failing_turn(self, *, input, **_kwargs):
        raise RuntimeError("provider is unreachable")

    monkeypatch.setattr(cli_agent.CliAgentBrain, "_execute_native_turn", failing_turn)
    monkeypatch.setitem(
        cli_agent.person_cli_agent_mapping,
        "p1",
        {"default": cli_agent.ExecutableInfo(adapter="claude")},
    )
    monkeypatch.setattr(
        cli_agent,
        "record_span_summary",
        lambda **kwargs: span_records.append(kwargs),
    )

    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())
    with pytest.raises(RuntimeError):
        await brain.run("hello", cwd=tmp_path, session_state=_read_only_state(tmp_path))

    assert span_records[0]["status"] == "failed"
    assert span_records[0]["model"] == ""
    assert span_records[0]["effort"] == ""
    assert span_records[0]["attributes"]["agent.slot"] == "default"


@pytest.mark.asyncio
async def test_execution_details_carry_the_effective_model_and_effort(
    monkeypatch, tmp_path
):
    span_records: list[dict] = []
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
    monkeypatch.setattr(
        cli_agent,
        "record_span_summary",
        lambda **kwargs: span_records.append(kwargs),
    )

    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())
    result = await brain.run_with_execution_details(
        "hello", cwd=tmp_path, session_state=_read_only_state(tmp_path)
    )

    assert (result.model, result.effort) == ("gpt-5-codex", "low")
    assert span_records[0]["model"] == "gpt-5-codex"
    assert span_records[0]["effort"] == "low"


@pytest.mark.asyncio
async def test_asking_response_omits_log_reference_when_output_dir_unset(
    monkeypatch, tmp_path
):
    from guildbotics.intelligences.common import AgentResponse

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
    assert output.message == "need input"
    assert "See:" not in output.message


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
async def test_read_only_native_turn_takes_no_person_execution_lease(
    monkeypatch, tmp_path
) -> None:
    from guildbotics.runtime.person_lease import PersonExecutionLease

    captured: dict = {}

    async def fake_execute_native_turn(self, *, input, configured, context, **_kwargs):
        captured["context"] = context
        return cli_agent.CliAgentExecutionResult(
            stdout="answer", stderr="", returncode=0
        )

    monkeypatch.setattr(
        cli_agent.CliAgentBrain, "_execute_native_turn", fake_execute_native_turn
    )
    workspace_root = tmp_path / "workspace"
    isolated_cwd = tmp_path / "data" / "workspaces" / "p1"
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(workspace_root))
    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())
    brain.executable_info = cli_agent.ExecutableInfo(adapter="claude-stream-json")

    # A routine already owns this member. A read-only assistant turn has to stay
    # usable anyway: that is exactly when its logs are worth asking about.
    lease = PersonExecutionLease("p1", tmp_path)
    lease.acquire(source="routine", command="ticket", work_id="work-1")
    try:
        result = await brain._execute(
            input="why did it fail?",
            cwd=isolated_cwd,
            kwargs={
                "session_state": {
                    "agent_execution_context": {
                        "run_id": "run-9",
                        "work_kind": "troubleshooting",
                        "workspace_data_root": str(tmp_path),
                        "read_only": True,
                    }
                }
            },
            effort=cli_agent.EffortDecision(),
        )
    finally:
        lease.release()

    assert not result.error_category
    assert result.stdout == "answer"
    assert captured["context"].read_only is True
    assert captured["context"].lease_id == ""
    assert captured["context"].cwd == isolated_cwd
    assert captured["context"].workspace_root == workspace_root


@pytest.mark.asyncio
async def test_a_default_effort_turn_states_no_settings(monkeypatch, tmp_path) -> None:
    """`default` cancels the frontmatter but still imposes nothing downstream.

    Stating the level here would give the turn a non-empty fingerprint, which
    rotates a session-scoped provider's session -- the opposite of the "keep the
    session's current settings" meaning `default` carries.
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
                    "workspace_data_root": str(tmp_path),
                    "read_only": True,
                },
            }
        },
        effort=brain._resolve_provider_effort({"session_state": {"effort": "default"}}),
    )

    context = captured["context"]
    assert context.effort == ""
    assert context.provider_options == {}
    assert captured["context"].delegation_id == ""


@pytest.mark.asyncio
async def test_an_unmapped_effort_level_is_not_claimed_as_the_turns_effort(
    monkeypatch, tmp_path
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
                    "workspace_data_root": str(tmp_path),
                    "read_only": True,
                },
            }
        },
        effort=brain._resolve_provider_effort({"session_state": {}}),
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
    cli_agent.person_cli_agent_mapping.clear()
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"default": "cli_agents/codex/default.yml"},
    )

    resolved = cli_agent.get_cli_agent_mapping("aiko")

    assert resolved["default"].adapter == "codex"
    assert resolved["default"].effort == {"high": {"effort": "high"}}
    cli_agent.person_cli_agent_mapping.clear()


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
    cli_agent.person_cli_agent_mapping.clear()
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
    cli_agent.person_cli_agent_mapping.clear()


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
    cli_agent.person_cli_agent_mapping.clear()
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
    cli_agent.person_cli_agent_mapping.clear()


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
    monkeypatch.setitem(
        cli_agent.person_cli_agent_mapping,
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
    monkeypatch.setitem(
        cli_agent.person_cli_agent_mapping,
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
    monkeypatch, tmp_path
) -> None:
    io_records: list[tuple[str, dict]] = []
    _native_brain(
        monkeypatch,
        cli_agent.CliAgentExecutionResult(stdout="done", stderr="", returncode=0),
        effort={"high": {"token": "secret-value"}},
    )
    monkeypatch.setattr(
        cli_agent,
        "record_correlated_io",
        lambda *, io_type, payload: io_records.append((io_type, payload)),
    )

    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger(), effort="high")
    await brain.run("hello", cwd=tmp_path, session_state=_read_only_state(tmp_path))

    effort_payload = io_records[0][1]["effort"]
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
    cli_agent.person_cli_agent_mapping.clear()
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"default": "cli_agents/codex/default.yml"},
    )
    try:
        brain = cli_agent.CliAgentBrain("aiko", "x", logger=_stub_logger())
        models = {
            requested: brain._resolve_provider_effort(
                {"session_state": {"effort": requested} if requested else {}}
            ).model
            for requested in ("high", "default", "")
        }
    finally:
        cli_agent.person_cli_agent_mapping.clear()

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
    cli_agent.person_cli_agent_mapping.clear()
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"default": "cli_agents/codex/default.yml"},
    )
    try:
        brain = cli_agent.CliAgentBrain("aiko", "x", logger=_stub_logger())
        unmapped = brain._resolve_provider_effort({"session_state": {"effort": "high"}})
        mapped = brain._resolve_provider_effort({"session_state": {"effort": "low"}})
    finally:
        cli_agent.person_cli_agent_mapping.clear()

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


def test_a_slot_inherits_the_tools_network_block(monkeypatch, tmp_path) -> None:
    _write_definition(
        tmp_path,
        "cli_agents/codex/default.yml",
        "network:\n  command:\n    mode: allowlist\n    allowed_domains: [registry.npmjs.org]\n"
        "    allow_local_network: false\n  web:\n    mode: deny\n    allowed_domains: []\n",
    )
    _write_definition(tmp_path, "cli_agents/codex/writer.yml", "effort: {}\n")
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    cli_agent.person_cli_agent_mapping.clear()
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"writer": "cli_agents/codex/writer.yml"},
    )

    resolved = cli_agent.get_cli_agent_mapping("aiko")

    assert resolved["writer"].network.command.mode == "allowlist"
    assert resolved["writer"].network.command.allowed_domains == ["registry.npmjs.org"]
    assert resolved["writer"].network.web.mode == "deny"
    cli_agent.person_cli_agent_mapping.clear()


def test_a_definition_without_a_network_block_is_closed(monkeypatch, tmp_path) -> None:
    _write_definition(tmp_path, "cli_agents/codex/default.yml", "effort: {}\n")
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    cli_agent.person_cli_agent_mapping.clear()
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"default": "cli_agents/codex/default.yml"},
    )

    resolved = cli_agent.get_cli_agent_mapping("aiko")

    assert resolved["default"].network == cli_agent.NetworkPolicy()
    cli_agent.person_cli_agent_mapping.clear()


def test_a_partial_network_block_names_the_slot_it_came_from(
    monkeypatch, tmp_path
) -> None:
    """A slot states the whole block or none of it; a half block is a mistake."""
    _write_definition(
        tmp_path,
        "cli_agents/codex/default.yml",
        "network:\n  command:\n    mode: unrestricted\n    allowed_domains: []\n"
        "    allow_local_network: false\n",
    )
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    cli_agent.person_cli_agent_mapping.clear()
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"default": "cli_agents/codex/default.yml"},
    )

    with pytest.raises(ValueError, match="AI CLI tool 'default'"):
        cli_agent.get_cli_agent_mapping("aiko")
    cli_agent.person_cli_agent_mapping.clear()
