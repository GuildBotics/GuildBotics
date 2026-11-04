"""The guest sends inference through the leased endpoint and records on host."""

import logging
import pytest
from guildbotics.guest.resolved import ResolvedBrainFactory
from guildbotics.intelligences.agent_runtime.wire import ResolvedGuest


def data():
    return ResolvedGuest(
        language="ja",
        project={},
        member={"person_id": "aiko", "name": "Aiko"},
        commands={},
        brains={
            "default": {
                "kind": "model",
                "slot": "main",
                "definition": {
                    "name": "models/openai/default.yml",
                    "model_class": "agno.models.openai.OpenAIChat",
                    "parameters": {"id": "fixture"},
                },
            }
        },
    )


def test_resolved_brain_uses_the_definition_without_loading_a_mapping(monkeypatch):
    from guildbotics.intelligences.brains import agno_agent

    monkeypatch.setattr(
        agno_agent,
        "get_model_mapping",
        lambda *_: pytest.fail("guest read a model mapping"),
    )
    brain = ResolvedBrainFactory(data()).create_brain(
        "aiko", "fixture", "ja", logging.getLogger(), config={"body": "instruction"}
    )
    assert brain.model_config.parameters == {"id": "fixture"}
    assert brain.model_slot == "main"
    with pytest.raises(ValueError, match="active member"):
        ResolvedBrainFactory(data()).create_brain(
            "someone",
            "fixture",
            "ja",
            logging.getLogger(),
            config={"body": "instruction"},
        )


@pytest.mark.asyncio
async def test_inference_runs_in_guest_through_the_gateway_and_records_on_host(
    monkeypatch,
):
    from types import SimpleNamespace
    import agno.agent
    from agno.run.base import RunStatus
    from guildbotics.guest import inference as module
    from guildbotics.intelligences.brains.inference import AgnoCall
    from guildbotics.intelligences.effort import ResolvedEffort

    captured = {}
    recorded = []

    class Model:
        def __init__(self, **parameters):
            captured.update(parameters)

    class Agent:
        def __init__(self, **kwargs):
            pass

        async def arun(self, message):
            assert message == "question"
            return SimpleNamespace(
                status=RunStatus.completed, content={"answer": 2}, metrics=None
            )

    class Window:
        async def acall(self, name, **arguments):
            assert name == "acquire_inference"
            assert arguments == {"model_name": "models/openai/default.yml"}

        async def record(self, entries):
            recorded.extend(entries)

    monkeypatch.setattr(module, "load_class", lambda _: Model)
    monkeypatch.setattr(agno.agent, "Agent", Agent)
    resolved = data()
    resolved.inference_urls = {"openai": "http://127.0.0.1:1234/v1"}
    resolved.inference_keys = {"openai": "fixture-placeholder"}
    answer = await module.GuestInference(resolved, Window()).agno(
        "aiko",
        AgnoCall(
            brain="reply",
            slot="main",
            effort=ResolvedEffort(),
            description="instruction",
            message="question",
        ),
    )
    assert answer.content == {"answer": 2}
    assert captured["api_key"] == "fixture-placeholder"
    assert captured["base_url"] == resolved.inference_urls["openai"]
    assert [entry.type for entry in recorded] == ["io", "io", "summary"]
    assert [entry.io_type for entry in recorded if entry.type == "io"] == [
        "llm.request",
        "llm.response",
    ]


def test_member_clone_path_uses_only_the_resolved_file(tmp_path, monkeypatch):
    import json
    from guildbotics.utils.fileio import RESOLVED_GUEST_ENV, get_member_clone_path

    path = tmp_path / "resolved.json"
    clone = tmp_path / "host-chosen-clone"
    path.write_text(
        json.dumps(
            {"member": {"person_id": "aiko"}, "clone_paths": {"aiko": str(clone)}}
        )
    )
    monkeypatch.setenv(RESOLVED_GUEST_ENV, str(path))
    monkeypatch.delenv("GUILDBOTICS_WORKSPACE_ROOT", raising=False)
    assert get_member_clone_path("aiko") == clone
    with pytest.raises(ValueError, match="resolved member"):
        get_member_clone_path("someone")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,model_class,prefix,expected_path,header",
    [
        (
            "openai",
            "agno.models.openai.OpenAIChat",
            "/v1",
            "/v1/chat/completions",
            "authorization",
        ),
        ("anthropic", "agno.models.anthropic.Claude", "", "/v1/messages", "x-api-key"),
        (
            "gemini",
            "agno.models.google.Gemini",
            "",
            "/v1beta/models/fixture:generateContent",
            "x-goog-api-key",
        ),
    ],
)
async def test_actual_provider_sdk_preserves_gateway_paths_and_auth_headers(
    monkeypatch, provider, model_class, prefix, expected_path, header
):
    import asyncio
    import json
    from guildbotics.guest.inference import GuestInference
    from guildbotics.intelligences.brains.inference import AgnoCall
    from guildbotics.intelligences.effort import ResolvedEffort

    requests = []
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    answers = {
        "openai": {
            "id": "chatcmpl-fixture",
            "object": "chat.completion",
            "created": 1,
            "model": "fixture",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "answer"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
        "anthropic": {
            "id": "msg_fixture",
            "type": "message",
            "role": "assistant",
            "model": "fixture",
            "content": [{"type": "text", "text": "answer"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        "gemini": {
            "candidates": [
                {
                    "content": {"parts": [{"text": "answer"}], "role": "model"},
                    "finishReason": "STOP",
                    "index": 0,
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 1,
                "candidatesTokenCount": 1,
                "totalTokenCount": 2,
            },
            "modelVersion": "fixture",
        },
    }

    async def respond(reader, writer):
        raw = await reader.readuntil(b"\r\n\r\n")
        lines = raw.decode().split("\r\n")
        headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
        headers = {k.lower(): v.strip() for k, v in headers.items()}
        size = int(headers.get("content-length", "0"))
        if size:
            await reader.readexactly(size)
        requests.append((lines[0].split()[1], headers))
        body = json.dumps(answers[provider]).encode()
        writer.write(
            f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    resolved = data()
    resolved.brains["default"].definition["model_class"] = model_class
    resolved.inference_urls = {provider: f"http://127.0.0.1:{port}{prefix}"}
    resolved.inference_keys = {provider: "fixture-placeholder"}

    class Window:
        async def acall(self, *args, **kwargs):
            return None

        async def record(self, entries):
            return None

    try:
        answer = await GuestInference(resolved, Window()).agno(
            "aiko",
            AgnoCall(
                brain="reply",
                slot="main",
                effort=ResolvedEffort(),
                description="instruction",
                message="question",
            ),
        )
        assert answer.content == "answer"
    finally:
        server.close()
        await server.wait_closed()
    assert len(requests) == 1
    path, headers = requests[0]
    assert path.split("?")[0] == expected_path
    assert (
        headers[header]
        == ("Bearer " if provider == "openai" else "") + "fixture-placeholder"
    )
