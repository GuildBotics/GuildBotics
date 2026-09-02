from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest
from acp_fake_peer import (
    DEFAULT_OPTIONS,
    AcpPeerBase,
    install,
    session_update,
    text_chunk,
)

from guildbotics.capabilities.task_runs import RUN_ENV, TASK_RUN_ENV
from guildbotics.intelligences.agent_runtime import copilot as copilot_module
from guildbotics.intelligences.agent_runtime.copilot import CopilotAcpAdapter
from guildbotics.intelligences.agent_runtime.models import (
    SETTINGS_SCOPE_TURN,
    AgentEvent,
    AgentEventKind,
    AgentExecutionContext,
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
    ConversationKey,
    ConversationRecord,
    ResumePolicy,
)
from guildbotics.runtime.person_lease import (
    DELEGATION_ID_ENV,
    LEASE_ID_ENV,
    LEASE_PERSON_ENV,
    LEASE_RUN_ENV,
)

FIXTURES = Path(__file__).parent / "fixtures"
INITIALIZE = json.loads((FIXTURES / "copilot_initialize_1_0_77.json").read_text())
SESSION = json.loads((FIXTURES / "copilot_session_1_0_77.json").read_text())


def _initialize(**overrides: Any) -> dict[str, Any]:
    payload = copy.deepcopy(INITIALIZE)
    payload.update(overrides)
    return payload


class _Peer(AcpPeerBase):
    """A fake ``copilot --acp`` peer speaking ACP v1 over line JSON-RPC.

    It mirrors GitHub Copilot CLI 1.0.77: a session response that carries the
    config options, ``session/set_config_option`` answering with the whole
    updated option list, and an unknown option id answered with an empty result
    rather than an error.
    """

    def __init__(
        self,
        *,
        initialize: dict[str, Any] | None = None,
        updates: list[dict[str, Any]] | None = None,
        stop_reason: str = "end_turn",
        replay: list[dict[str, Any]] | None = None,
        load_error: dict[str, Any] | None = None,
        authenticate_error: dict[str, Any] | None = None,
        authenticate_hangs: bool = False,
        prompt_error: dict[str, Any] | None = None,
        set_option_error: dict[str, Any] | None = None,
        known_options: set[str] | None = None,
        current_config: dict[str, str] | None = None,
        permission_request: dict[str, Any] | None = None,
        permission_options: list[dict[str, Any]] | None = None,
        prompt_delay: float = 0.0,
    ) -> None:
        super().__init__()
        self.initialize = initialize if initialize is not None else _initialize()
        self.updates = updates if updates is not None else [text_chunk("hello world")]
        self.stop_reason = stop_reason
        self.replay = replay or []
        self.load_error = load_error
        self.authenticate_error = authenticate_error
        self.authenticate_hangs = authenticate_hangs
        self.prompt_error = prompt_error
        self.set_option_error = set_option_error
        self.known_options = (
            known_options
            if known_options is not None
            else {"mode", "model", "reasoning_effort", "allow_all"}
        )
        self.permission_request = permission_request
        self.permission_options = (
            permission_options if permission_options is not None else DEFAULT_OPTIONS
        )
        self.prompt_delay = prompt_delay
        self.config = copy.deepcopy(SESSION)
        for option in self.config["configOptions"]:
            if option["id"] in (current_config or {}):
                option["currentValue"] = current_config[option["id"]]

    @property
    def current(self) -> dict[str, str]:
        return {
            str(option["id"]): str(option["currentValue"])
            for option in self.config["configOptions"]
        }

    def handle(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params", {})
        if method is None or request_id is None:
            return
        if method == "initialize":
            self.send_result(request_id, self.initialize)
        elif method == "authenticate":
            if self.authenticate_hangs:
                return
            if self.authenticate_error:
                self.send_error(request_id, self.authenticate_error)
            else:
                self.send_result(request_id, {})
        elif method == "session/new":
            self.send_result(request_id, {"sessionId": self.SESSION_ID, **self.config})
        elif method == "session/load":
            if self.load_error:
                self.send_error(request_id, self.load_error)
                return
            for update in self.replay:
                self.feed(session_update(self.SESSION_ID, update))
            self.send_result(request_id, dict(self.config))
        elif method == "session/set_config_option":
            self._set_config_option(request_id, params)
        elif method == "session/prompt":
            self._prompt(request_id)

    def _set_config_option(self, request_id: Any, params: dict[str, Any]) -> None:
        if self.set_option_error:
            self.send_error(request_id, self.set_option_error)
            return
        config_id = str(params.get("configId", ""))
        if config_id not in self.known_options:
            # Copilot 1.0.77 acknowledges an unknown option id with an empty
            # result instead of reporting an error.
            self.send_result(request_id, None)
            return
        for option in self.config["configOptions"]:
            if option["id"] == config_id:
                option["currentValue"] = params.get("value")
        self.send_result(request_id, dict(self.config))
        self.feed(
            session_update(
                self.SESSION_ID,
                {
                    "sessionUpdate": "config_option_update",
                    "configOptions": self.config["configOptions"],
                },
            )
        )

    def _prompt(self, request_id: Any) -> None:
        if self.prompt_error:
            self.send_error(request_id, self.prompt_error)
            return
        self._emit_commands()
        if self.prompt_delay:
            # A real turn answers session/prompt only when the work is done.
            asyncio.get_running_loop().create_task(self._answer_later(request_id))
            return
        if self.permission_request is not None:
            self.feed(
                {
                    "jsonrpc": "2.0",
                    # Copilot numbers its reverse requests from zero.
                    "id": 0,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": self.SESSION_ID,
                        "toolCall": self.permission_request,
                        "options": self.permission_options,
                    },
                }
            )
        for update in self.updates:
            self.feed(session_update(self.SESSION_ID, update))
        self.send_result(request_id, {"stopReason": self.stop_reason})

    async def _answer_later(self, request_id: Any) -> None:
        await asyncio.sleep(self.prompt_delay)
        for update in self.updates:
            self.feed(session_update(self.SESSION_ID, update))
        self.send_result(request_id, {"stopReason": self.stop_reason})

    def _emit_commands(self) -> None:
        # Copilot pushes its slash-command list twice, once the first prompt of
        # the process starts rather than when the session is created.
        for _ in range(2):
            self.feed(
                session_update(
                    self.SESSION_ID,
                    {
                        "sessionUpdate": "available_commands_update",
                        "availableCommands": [{"name": "compact"}],
                    },
                )
            )

    def set_options(self) -> list[dict[str, Any]]:
        return [
            message["params"] for message in self.all_sent("session/set_config_option")
        ]


def _context(tmp_path: Path, **overrides: Any) -> AgentExecutionContext:
    key = ConversationKey("aiko", "copilot", "ticket", "issue-364")
    return AgentExecutionContext(
        person_id="aiko",
        run_id="run-1",
        cwd=tmp_path,
        workspace_root=tmp_path,
        workspace_data_root=tmp_path,
        conversation_key=key,
        resume_policy=ResumePolicy.AUTO,
        **overrides,
    )


async def _run(
    adapter: CopilotAcpAdapter,
    tmp_path: Path,
    conversation: ConversationRecord | None = None,
    **context_overrides: Any,
) -> tuple[Any, list[AgentEvent]]:
    context = _context(tmp_path, **context_overrides)
    record = conversation or ConversationRecord(key=context.conversation_key)
    events: list[AgentEvent] = []
    result = await adapter.run_turn("do the thing", context, record, events.append)
    await adapter.close()
    return result, events


def _named(events: list[AgentEvent], kind: AgentEventKind, name: str) -> AgentEvent:
    return next(event for event in events if event.kind is kind and event.name == name)


# --- launch and handshake ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_session_streams_chunks_and_reports_the_session_id(
    monkeypatch, tmp_path
) -> None:
    ambient_execution = (
        RUN_ENV,
        TASK_RUN_ENV,
        "GUILDBOTICS_WORKSPACE_ROOT",
        LEASE_ID_ENV,
        DELEGATION_ID_ENV,
        LEASE_PERSON_ENV,
        LEASE_RUN_ENV,
    )
    for key in ambient_execution:
        monkeypatch.setenv(key, "stale-parent-value")
    peer = _Peer(updates=[text_chunk("hello "), text_chunk("world")])
    launched = install(monkeypatch, peer)

    result, events = await _run(CopilotAcpAdapter(), tmp_path)

    assert result.output == "hello world"
    assert result.provider_session_id == _Peer.SESSION_ID
    assert result.provider_turn_id == ""
    assert result.finish_reason == "completed"
    launch_argv = launched[0][0]
    assert launch_argv[:4] == (
        "copilot",
        "--acp",
        "--no-auto-update",
        "--no-remote-export",
    )
    assert launch_argv[4] == "--allow-tool"
    assert launch_argv[5].startswith("guildbotics-member-")
    assert launch_argv[5].endswith("(guildbotics_member)")
    assert all(key not in launched[0][1]["env"] for key in ambient_execution)
    assert peer.methods() == [
        "initialize",
        "authenticate",
        "session/new",
        "session/set_config_option",
        "session/prompt",
    ]
    started = _named(events, AgentEventKind.PROCESS, "started")
    assert started.details == {
        "agent_version": "1.0.77",
        "protocol_version": 1,
        "load_session": True,
        "resume_session": False,
        "auth_method": "copilot-login",
    }
    server = peer.sent("session/new")["params"]["mcpServers"][0]
    assert server["type"] == "http"
    assert server["name"].startswith("guildbotics-member-")
    assert server["url"] == "http://127.0.0.1:43123/mcp"
    assert server["headers"][0]["value"].startswith("Bearer ")
    prompt = peer.sent("session/prompt")["params"]["prompt"][0]["text"]
    assert "never run those commands" in prompt
    assert prompt.endswith("\n\ndo the thing")


@pytest.mark.asyncio
async def test_a_peer_that_does_not_speak_v1_is_rejected(monkeypatch, tmp_path) -> None:
    peer = _Peer(initialize=_initialize(protocolVersion=2))
    install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as error:
        await _run(CopilotAcpAdapter(), tmp_path)

    assert error.value.category is AgentRuntimeErrorCategory.UNSUPPORTED_VERSION
    assert error.value.details == {"protocol_version": 2}


@pytest.mark.asyncio
async def test_a_peer_that_cannot_reload_a_session_is_rejected(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        initialize=_initialize(
            agentCapabilities={"loadSession": False, "sessionCapabilities": {}}
        )
    )
    install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as error:
        await _run(CopilotAcpAdapter(), tmp_path)

    assert error.value.category is AgentRuntimeErrorCategory.UNSUPPORTED_VERSION
    assert "cannot resume an exact session" in str(error.value)


# --- authentication ----------------------------------------------------------


@pytest.mark.asyncio
async def test_an_install_without_the_login_method_is_an_authentication_error(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(initialize=_initialize(authMethods=[{"id": "other"}]))
    install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as error:
        await _run(CopilotAcpAdapter(), tmp_path)

    assert error.value.category is AgentRuntimeErrorCategory.AUTHENTICATION
    assert "Run 'copilot login' as this user." in str(error.value)
    assert error.value.details == {"advertised_methods": ["other"]}
    assert "session/new" not in peer.methods()


@pytest.mark.asyncio
async def test_a_rejected_authenticate_is_an_authentication_error(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(authenticate_error={"code": -32001, "message": "not logged in"})
    install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as error:
        await _run(CopilotAcpAdapter(), tmp_path)

    assert error.value.category is AgentRuntimeErrorCategory.AUTHENTICATION
    assert error.value.details["auth_method"] == "copilot-login"


@pytest.mark.asyncio
async def test_a_pending_terminal_login_is_an_authentication_error(
    monkeypatch, tmp_path
) -> None:
    """A headless run must not wait on the sign-in Copilot expects in a terminal."""
    peer = _Peer(authenticate_hangs=True)
    install(monkeypatch, peer)
    monkeypatch.setattr(copilot_module, "_AUTH_TIMEOUT", 0.05)

    with pytest.raises(AgentRuntimeError) as error:
        await _run(CopilotAcpAdapter(), tmp_path)

    assert error.value.category is AgentRuntimeErrorCategory.AUTHENTICATION
    assert "Run 'copilot login' as this user." in str(error.value)


# --- session configuration ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_turns_settings_are_applied_as_session_config_options(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer()
    install(monkeypatch, peer)

    _result, events = await _run(
        CopilotAcpAdapter(),
        tmp_path,
        provider_options={"model": "gpt-5-mini", "reasoning_effort": "low"},
    )

    assert peer.set_options() == [
        {
            "sessionId": _Peer.SESSION_ID,
            "configId": "model",
            "value": "gpt-5-mini",
        },
        {
            "sessionId": _Peer.SESSION_ID,
            "configId": "reasoning_effort",
            "value": "low",
        },
        {"sessionId": _Peer.SESSION_ID, "configId": "allow_all", "value": "on"},
    ]
    settings = _named(events, AgentEventKind.PROCESS, "settings")
    assert settings.provider_session_id == _Peer.SESSION_ID
    assert settings.details == {
        "model": "gpt-5-mini",
        "reasoning_effort": "low",
        "allow_all": "on",
        "requested": {
            "model": "gpt-5-mini",
            "reasoning_effort": "low",
            "allow_all": "on",
        },
        "rejected": [],
    }


@pytest.mark.asyncio
async def test_a_setting_the_session_already_has_is_not_re_sent(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer()
    install(monkeypatch, peer)

    await _run(
        CopilotAcpAdapter(),
        tmp_path,
        # The fixture session already runs gpt-5.4 at medium effort.
        provider_options={"model": "gpt-5.4", "reasoning_effort": "medium"},
    )

    assert [params["configId"] for params in peer.set_options()] == ["allow_all"]


@pytest.mark.asyncio
async def test_an_effective_value_is_reported_not_the_requested_one(
    monkeypatch, tmp_path
) -> None:
    """An option Copilot silently ignores must not be reported as applied."""
    peer = _Peer(known_options={"model", "allow_all"})
    install(monkeypatch, peer)

    _result, events = await _run(
        CopilotAcpAdapter(),
        tmp_path,
        provider_options={"model": "gpt-5-mini", "reasoning_effort": "xhigh"},
    )

    settings = _named(events, AgentEventKind.PROCESS, "settings")
    assert settings.details["model"] == "gpt-5-mini"
    assert settings.details["reasoning_effort"] == "medium"
    assert settings.details["rejected"] == ["reasoning_effort"]
    # The turn result has to agree: the session kept its own effort.
    assert (_result.model, _result.effort) == ("gpt-5-mini", "medium")


@pytest.mark.asyncio
async def test_the_terminal_result_carries_the_confirmed_session_settings(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer()
    install(monkeypatch, peer)

    result, _events = await _run(
        CopilotAcpAdapter(),
        tmp_path,
        provider_options={"model": "gpt-5-mini", "reasoning_effort": "low"},
    )

    assert (result.model, result.effort) == ("gpt-5-mini", "low")


@pytest.mark.asyncio
async def test_a_read_only_turn_makes_copilot_ask_before_acting(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer()
    install(monkeypatch, peer)

    _result, events = await _run(CopilotAcpAdapter(), tmp_path, read_only=True)

    assert peer.current["allow_all"] == "off"
    policy = _named(events, AgentEventKind.APPROVAL, "policy")
    assert policy.approval == "never"
    assert policy.details == {
        "allowed_paths": "workspace",
        "allow_all": "off",
        "read_only": True,
    }


@pytest.mark.asyncio
async def test_a_read_only_turn_stops_when_its_approval_policy_is_not_confirmed(
    monkeypatch, tmp_path
) -> None:
    """An unconfirmed `allow_all: off` is a refusal to run, not a warning.

    Copilot acknowledges an option id it does not know without applying it, so a
    session left on `on` by an earlier turn would act without ever asking.
    """
    peer = _Peer(
        known_options={"model", "reasoning_effort"},
        current_config={"allow_all": "on"},
    )
    install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as error:
        await _run(CopilotAcpAdapter(), tmp_path, read_only=True)

    assert error.value.category is AgentRuntimeErrorCategory.PROTOCOL
    assert error.value.rotate_session is True
    assert error.value.details == {"requested": "off", "effective": "on"}
    assert "session/prompt" not in peer.methods()


@pytest.mark.asyncio
async def test_a_normal_turn_survives_an_approval_policy_it_could_not_set(
    monkeypatch, tmp_path
) -> None:
    """The other direction only costs confirmations, which are declined anyway."""
    peer = _Peer(known_options={"model", "reasoning_effort"})
    install(monkeypatch, peer)

    _result, events = await _run(CopilotAcpAdapter(), tmp_path)

    assert _named(events, AgentEventKind.PROCESS, "settings").details["rejected"] == [
        "allow_all"
    ]


@pytest.mark.asyncio
async def test_a_refused_config_option_is_reported_as_a_protocol_error(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(set_option_error={"code": -32002, "message": "Resource not found"})
    install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as error:
        await _run(CopilotAcpAdapter(), tmp_path)

    assert error.value.category is AgentRuntimeErrorCategory.PROTOCOL
    assert error.value.rotate_session is True


def test_only_the_settings_the_adapter_can_impose_count_as_a_change(tmp_path) -> None:
    adapter = CopilotAcpAdapter()

    applied = adapter.applied_settings(
        _context(
            tmp_path,
            provider_options={
                "model": "gpt-5-mini",
                "reasoning_effort": "high",
                "verbosity": "loud",
                "reasoning_effort_blank": "",
            },
        )
    )

    assert applied == {"model": "gpt-5-mini", "reasoning_effort": "high"}
    # Config options can be re-sent on a live session, so a change never costs
    # a fresh one.
    assert adapter.settings_scope == SETTINGS_SCOPE_TURN


# --- exact reload ------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_existing_session_is_reloaded_and_its_replay_absorbed(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        replay=[
            {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": "the first prompt"},
            },
            text_chunk("the first answer"),
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "call_1",
                "kind": "edit",
                "status": "completed",
            },
        ],
        updates=[text_chunk("the second answer")],
    )
    install(monkeypatch, peer)
    conversation = ConversationRecord(
        key=ConversationKey("aiko", "copilot", "ticket", "issue-364"),
        provider_session_id=_Peer.SESSION_ID,
    )

    result, events = await _run(CopilotAcpAdapter(), tmp_path, conversation)

    assert result.output == "the second answer"
    assert peer.sent("session/load")["params"]["sessionId"] == _Peer.SESSION_ID
    rehydrated = _named(events, AgentEventKind.TURN, "history_rehydrated")
    assert rehydrated.details == {
        "replayed_updates": 3,
        "session_method": "session/load",
    }
    # None of the replayed history may reach this turn's transcript.
    assert not [event for event in events if "first" in event.message]


@pytest.mark.asyncio
async def test_a_session_copilot_no_longer_has_is_rotated(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        load_error={
            "code": -32002,
            "message": "Resource not found: Session ... not found",
            "data": {"uri": "Session ... not found"},
        }
    )
    install(monkeypatch, peer)
    conversation = ConversationRecord(
        key=ConversationKey("aiko", "copilot", "ticket", "issue-364"),
        provider_session_id=_Peer.SESSION_ID,
    )

    with pytest.raises(AgentRuntimeError) as error:
        await _run(CopilotAcpAdapter(), tmp_path, conversation)

    assert error.value.category is AgentRuntimeErrorCategory.SESSION_UNAVAILABLE
    assert error.value.rotate_session is True
    assert error.value.details["session_method"] == "session/load"


@pytest.mark.asyncio
async def test_a_session_this_process_still_holds_is_never_reloaded(
    monkeypatch, tmp_path
) -> None:
    """Copilot answers a second load with "already loaded", and it is right to.

    The conversation never left the process, so there is nothing to rehydrate.
    """
    peer = _Peer(updates=[text_chunk("first")])
    install(monkeypatch, peer)
    adapter = CopilotAcpAdapter()
    context = _context(tmp_path, provider_options={"reasoning_effort": "high"})
    conversation = ConversationRecord(key=context.conversation_key)
    events: list[AgentEvent] = []

    first = await adapter.run_turn("one", context, conversation, events.append)
    conversation.provider_session_id = first.provider_session_id
    peer.updates = [text_chunk("second")]
    second_events: list[AgentEvent] = []
    second = await adapter.run_turn("two", context, conversation, second_events.append)
    await adapter.close()

    assert second.output == "second"
    assert "session/load" not in peer.methods()
    assert not [event for event in second_events if event.name == "history_rehydrated"]
    # Settings are still imposed on the turn, from no assumed current state.
    assert (
        _named(second_events, AgentEventKind.PROCESS, "settings").details[
            "reasoning_effort"
        ]
        == "high"
    )


@pytest.mark.asyncio
async def test_an_unchanged_launch_command_keeps_the_running_process(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(updates=[text_chunk("first")])
    launched = install(monkeypatch, peer)
    adapter = CopilotAcpAdapter()
    conversation = ConversationRecord(
        key=ConversationKey("aiko", "copilot", "ticket", "issue-364")
    )

    await adapter.run_turn("one", _context(tmp_path), conversation, lambda _e: None)
    peer.updates = [text_chunk("second")]
    await adapter.run_turn("two", _context(tmp_path), conversation, lambda _e: None)
    await adapter.close()

    assert len(launched) == 1


@pytest.mark.asyncio
async def test_settings_are_re_applied_to_a_reloaded_session(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(updates=[text_chunk("done")])
    install(monkeypatch, peer)
    conversation = ConversationRecord(
        key=ConversationKey("aiko", "copilot", "ticket", "issue-364"),
        provider_session_id=_Peer.SESSION_ID,
    )

    await _run(
        CopilotAcpAdapter(),
        tmp_path,
        conversation,
        provider_options={"reasoning_effort": "high"},
    )

    assert peer.current["reasoning_effort"] == "high"


# --- turn decoding -----------------------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_is_recorded_but_never_becomes_the_answer(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        updates=[
            {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "let me think"},
            },
            text_chunk("the answer"),
        ]
    )
    install(monkeypatch, peer)

    result, events = await _run(CopilotAcpAdapter(), tmp_path)

    assert result.output == "the answer"
    assert (
        _named(events, AgentEventKind.ASSISTANT, "thinking").message == "let me think"
    )
    assert _named(events, AgentEventKind.ASSISTANT, "completed").message == "the answer"


@pytest.mark.asyncio
async def test_the_slash_command_list_copilot_pushes_is_not_a_transcript_entry(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer()
    install(monkeypatch, peer)

    _result, events = await _run(CopilotAcpAdapter(), tmp_path)

    assert not [event for event in events if event.name == "unknown_update"]
    assert not [event for event in events if event.name == "protocol_extensions"]


@pytest.mark.asyncio
async def test_a_file_editing_tool_call_is_reported_as_a_file_change(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        updates=[
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "call_1",
                "title": "Creating hello.txt",
                "kind": "edit",
                "status": "pending",
                "rawInput": {"path": "/ws/hello.txt", "file_text": "HELLO"},
                "locations": [{"path": "/ws/hello.txt"}],
            },
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call_1",
                "status": "completed",
            },
            text_chunk("DONE"),
        ]
    )
    install(monkeypatch, peer)

    _result, events = await _run(CopilotAcpAdapter(), tmp_path)

    started = _named(events, AgentEventKind.FILE_CHANGE, "started")
    assert started.item_id == "call_1"
    assert started.path == "/ws/hello.txt"
    assert started.details == {
        "tool_kind": "edit",
        "status": "pending",
        "paths": ["/ws/hello.txt"],
    }
    # Only `toolCallId` is required on an update, so the kind must be remembered.
    updated = _named(events, AgentEventKind.FILE_CHANGE, "updated")
    assert updated.details == {"tool_kind": "edit", "status": "completed"}


@pytest.mark.asyncio
async def test_context_usage_is_normalized_to_the_shared_keys(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        updates=[
            {"sessionUpdate": "usage_update", "used": 1200, "size": 128000},
            text_chunk("ok"),
        ]
    )
    install(monkeypatch, peer)

    result, events = await _run(CopilotAcpAdapter(), tmp_path)

    assert _named(events, AgentEventKind.USAGE, "context").usage == {
        "context_used_tokens": 1200,
        "context_size_tokens": 128000,
    }
    assert result.usage == {
        "context_used_tokens": 1200,
        "context_size_tokens": 128000,
    }


@pytest.mark.asyncio
async def test_a_permission_request_is_declined_with_the_agents_own_option_id(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        permission_request={"toolCallId": "call_1", "title": "Create file"},
        updates=[text_chunk("refused")],
    )
    install(monkeypatch, peer)

    _result, events = await _run(CopilotAcpAdapter(), tmp_path, read_only=True)

    answer = next(
        message
        for message in peer.messages
        if message.get("id") == 0 and "result" in message
    )
    assert answer["result"] == {
        "outcome": {"outcome": "selected", "optionId": "opt-r7"}
    }
    decision = _named(events, AgentEventKind.APPROVAL, "decision")
    assert decision.approval == "decline"
    assert decision.details == {"tool_call": "Create file", "outcome": "selected"}


@pytest.mark.asyncio
async def test_a_request_copilot_should_never_send_is_refused(
    monkeypatch, tmp_path
) -> None:
    """No client capability was declared, so nothing may be answered for real."""
    peer = _Peer()
    install(monkeypatch, peer)
    adapter = CopilotAcpAdapter()
    adapter._transport._process = peer  # type: ignore[assignment]

    await adapter._handle_agent_request("fs/read_text_file", 7, {})

    refusal = next(message for message in peer.messages if message.get("id") == 7)
    assert refusal["error"]["code"] == -32601


# --- failures ----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_reason", "category"),
    [
        ("cancelled", AgentRuntimeErrorCategory.CANCELLED),
        ("max_tokens", AgentRuntimeErrorCategory.PROCESS),
        ("refusal", AgentRuntimeErrorCategory.PROCESS),
        ("something_new", AgentRuntimeErrorCategory.PROTOCOL),
    ],
)
async def test_a_stop_reason_maps_to_its_category(
    monkeypatch, tmp_path, stop_reason: str, category: AgentRuntimeErrorCategory
) -> None:
    peer = _Peer(stop_reason=stop_reason)
    install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as error:
        await _run(CopilotAcpAdapter(), tmp_path)

    assert error.value.category is category
    assert str(error.value).startswith("Copilot ")
    assert error.value.details == {"stop_reason": stop_reason}


@pytest.mark.asyncio
async def test_a_finished_turn_without_an_answer_is_a_protocol_error(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(updates=[])
    install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as error:
        await _run(CopilotAcpAdapter(), tmp_path)

    assert error.value.category is AgentRuntimeErrorCategory.PROTOCOL
    assert error.value.rotate_session is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_payload", "category"),
    [
        (
            {"code": -32000, "data": {"code": "user_weekly_rate_limited"}},
            AgentRuntimeErrorCategory.RATE_LIMITED,
        ),
        (
            {"code": -32000, "data": {"type": "too_many_requests"}},
            AgentRuntimeErrorCategory.RATE_LIMITED,
        ),
        (
            {"code": -32000, "data": {"code": "unauthorized"}},
            AgentRuntimeErrorCategory.AUTHENTICATION,
        ),
        (
            {"code": -32601, "message": "Method not found"},
            AgentRuntimeErrorCategory.UNSUPPORTED_VERSION,
        ),
        (
            {"code": -32602, "message": "Session ... not found"},
            AgentRuntimeErrorCategory.PROTOCOL,
        ),
    ],
)
async def test_an_rpc_error_is_classified_by_its_code(
    monkeypatch, tmp_path, error_payload: dict[str, Any], category
) -> None:
    peer = _Peer(prompt_error=error_payload)
    install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as error:
        await _run(CopilotAcpAdapter(), tmp_path)

    assert error.value.category is category


@pytest.mark.asyncio
async def test_a_rate_limit_reset_hint_is_carried_through(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(
        prompt_error={
            "code": -32000,
            "data": {
                "code": "user_weekly_rate_limited",
                "resetAt": "2026-08-10T00:00:00Z",
            },
        }
    )
    install(monkeypatch, peer)

    with pytest.raises(AgentRuntimeError) as error:
        await _run(CopilotAcpAdapter(), tmp_path)

    assert error.value.details["retry_after_at"] == "2026-08-10T00:00:00Z"


@pytest.mark.asyncio
async def test_a_stalled_turn_is_bounded_by_the_turn_deadline(
    monkeypatch, tmp_path
) -> None:
    peer = _Peer(prompt_delay=5.0)
    install(monkeypatch, peer)
    terminated: list[Any] = []

    async def terminate(process: Any) -> None:
        terminated.append(process)
        process.returncode = -15

    monkeypatch.setattr(
        "guildbotics.intelligences.agent_runtime.acp.terminate_process_tree",
        terminate,
    )
    adapter = CopilotAcpAdapter(timeout=0.05)

    with pytest.raises(AgentRuntimeError) as error:
        await _run(adapter, tmp_path)

    assert error.value.category is AgentRuntimeErrorCategory.PROCESS
    assert error.value.rotate_session is True
    assert terminated == [peer]
    assert peer.sent("session/cancel")["params"] == {"sessionId": _Peer.SESSION_ID}
