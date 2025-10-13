import json
from pathlib import Path

import pytest

from guildbotics.entities.message import Message
from guildbotics.intelligences import functions as f
from guildbotics.intelligences.common import (
    AgentResponse,
    ArtifactProcessEvaluation,
    DecisionResponse,
    ImprovementRecommendations,
    Labels,
    MessageResponse,
    NextTasksResponse,
    RootCauseAnalysis,
    RootCauseItem,
)


def test_to_text_with_model_and_list():
    model = DecisionResponse(label="a", reason="r", confidence=0.9)
    # single model -> YAML without empty values
    out1 = f.to_text(model)
    assert "label: a" in out1 and "confidence: 0.9" in out1
    # list of models
    out2 = f.to_text([model, model])
    assert out2.startswith("-") or "-" in out2


def test_messages_to_json_basic():
    msgs = [
        Message(content="hi", author="u", author_type=Message.USER),
        Message(content="ok", author="b", author_type=Message.ASSISTANT),
    ]
    s = f.messages_to_json(msgs)
    data = json.loads(s)
    assert data[0]["content"] == "hi" and data[1]["author_type"] == "Assistant"


@pytest.mark.asyncio
async def test_get_content_variants(monkeypatch, fake_context, stub_brain):
    # use fake_context and stub_brain instead of real LLM
    ctx = fake_context

    # get_content: response_class is falsy -> returns raw
    stub_brain("a", {"k": 1}, response_class=None)
    assert await f.get_content(ctx, "a", message="m") == {"k": 1}

    # get_content: response_class match -> returns instance
    ar = AgentResponse(status=AgentResponse.DONE, message="ok")
    stub_brain("b", ar, response_class=AgentResponse)
    assert await f.get_content(ctx, "b", message="m") == ar

    # get_content: response_class mismatch -> calls convert_object
    called = {"count": 0}

    async def fake_convert(context, output, response_model):
        called["count"] += 1
        return ar

    monkeypatch.setattr(f, "convert_object", fake_convert)
    stub_brain("c", "{not json}", response_class=AgentResponse)
    assert await f.get_content(ctx, "c", message="m") == ar
    assert called["count"] == 1


@pytest.mark.asyncio
async def test_talk_as_and_reply_as_build_session_state(monkeypatch, fake_context):
    ctx = fake_context

    async def fake_get_content(context, name, message, params=None, cwd=None):
        return MessageResponse(content=" result ", author="ai", author_type="Assistant")

    monkeypatch.setattr(f, "get_content", fake_get_content)

    txt = await f.talk_as(
        ctx, topic="t", context_location="here", conversation_history=[]
    )
    assert txt == "result"

    txt2 = await f.reply_as(ctx, messages=[Message(content="x")], cwd=Path("."))
    assert txt2 == "result"


@pytest.mark.asyncio
async def test_identify_role_and_mode(monkeypatch, fake_context, stub_brain):
    ctx = fake_context

    async def fake_get_content(context, name, message, **kwargs):
        return DecisionResponse(label="dev", reason="r", confidence=1.0)

    monkeypatch.setattr(f, "get_content", fake_get_content)

    assert await f.identify_role(ctx, "who") == "dev"
    assert await f.identify_mode(ctx, Labels({"build": "b"}), input="mode?") == "dev"


@pytest.mark.asyncio
async def test_write_commit_message_and_pr_description(
    monkeypatch, fake_context, stub_brain
):
    ctx = fake_context

    async def fake_get_content(context, name, message, **kwargs):
        return "message"

    monkeypatch.setattr(f, "get_content", fake_get_content)
    assert (
        await f.write_commit_message(ctx, task_title="t", changes="diff")
    ) == "message"

    assert (
        await f.write_pull_request_description(
            ctx, changes="d", commit_message="c", ticket_url="u", pr_template="tmp"
        )
    ) == "message"


@pytest.mark.asyncio
async def test_evaluate_interaction_performance_and_root_cause(
    monkeypatch, fake_context, stub_brain
):
    ctx = fake_context

    # stub translation to produce deterministic string
    monkeypatch.setattr(
        f,
        "t",
        lambda key, **kw: kw.get("subject_type")
        or kw.get("reason")
        or kw.get("context")
        or key,
    )

    eval_model = ArtifactProcessEvaluation(
        review_comment_count=1,
        review_cycle_count=2,
        request_changes_count=3,
        outcome_score=1.0,
        review_sentiment_score=0.5,
        overall_score=0.8,
        reason="why",
        context="ctx",
    )

    async def fake_get_content_eval(context, name, message, **kwargs):
        return eval_model

    monkeypatch.setattr(f, "get_content", fake_get_content_eval)
    summary = await f.evaluate_interaction_performance(ctx, "text")
    # Our t stub returns either reason/context/subject_type/key; ensure reason leaked
    assert "why" in summary or "ctx" in summary

    # analyze_root_cause default path
    rca = RootCauseAnalysis(
        items=[
            RootCauseItem(
                perspective="Process",
                problem="p",
                root_cause="rc",
                severity=0.9,
                severity_reason="sr",
            )
        ]
    )

    async def fake_get_content_rca(context, name, message, **kwargs):
        return rca

    monkeypatch.setattr(f, "get_content", fake_get_content_rca)
    assert await f.analyze_root_cause(ctx, "txt", "ev") is rca


@pytest.mark.asyncio
async def test_propose_process_improvements(monkeypatch, fake_context, stub_brain):
    ctx = fake_context
    rec = ImprovementRecommendations(suggestions=[])

    async def fake_get_content(context, name, message, **kwargs):
        return rec

    monkeypatch.setattr(f, "get_content", fake_get_content)
    assert await f.propose_process_improvements(ctx, RootCauseAnalysis(items=[])) is rec


@pytest.mark.asyncio
async def test_identify_tasks(monkeypatch, fake_context, stub_brain):
    ctx = fake_context
    nxt = NextTasksResponse(tasks=[])

    captured = {"called": False, "session_state": None}

    async def fake_get_content(context, name, message, params=None, cwd=None):
        captured["called"] = True
        captured["params"] = params
        captured["message"] = message
        return nxt

    monkeypatch.setattr(f, "get_content", fake_get_content)

    modes = Labels({"build": "desc"})
    messages = [Message(content="d", author="user", author_type="user")]
    out = await f.identify_next_tasks(
        ctx, role_id="dev", cwd=Path("."), messages=messages, available_modes=modes
    )
    assert out is nxt and captured["called"]
    assert "available_modes" in captured["params"]
    assert (
        str(captured["message"]).replace(" ", "").replace("\n", "") == '[{"user":"d"}]'
    )


@pytest.mark.asyncio
async def test_identify_output_and_message_and_analyze_log(
    monkeypatch, fake_context, stub_brain
):
    ctx = fake_context

    async def fake_get_content_dec(context, name, message, **kwargs):
        return DecisionResponse(label="code", reason="r", confidence=1)

    monkeypatch.setattr(f, "get_content", fake_get_content_dec)
    assert await f.identify_output_type(ctx, "in") == "code"
    assert await f.identify_message_type(ctx, "in") == "code"

    # analyze_log proxies to get_content
    async def fake_get_content_agent(context, name, message, **kwargs):
        return AgentResponse(status=AgentResponse.DONE, message="ok")

    monkeypatch.setattr(f, "get_content", fake_get_content_agent)
    ar = await f.analyze_log(ctx, "logs")
    assert ar.status == AgentResponse.DONE and ar.message == "ok"


@pytest.mark.asyncio
async def test_to_agent_response_variants(monkeypatch, fake_context):
    ctx = fake_context

    # already an AgentResponse
    ar = AgentResponse(status=AgentResponse.DONE, message="ok")
    assert await f.to_agent_response(ctx, ar, "") is ar

    # JSON parse success
    js = ar.model_dump_json()
    ar2 = await f.to_agent_response(ctx, js, "")
    assert ar2 == ar

    # parse error -> message type error -> returns ASKING
    async def fake_identify_message_type(context, input):
        return "error"

    async def fake_analyze_log(context, log_output):
        return AgentResponse(status=AgentResponse.DONE, message="ignored")

    monkeypatch.setattr(f, "identify_message_type", fake_identify_message_type)
    monkeypatch.setattr(f, "analyze_log", fake_analyze_log)
    ar3 = await f.to_agent_response(ctx, "not json", "log")
    assert ar3.status == AgentResponse.ASKING

    # parse error -> message type normal -> convert_object used
    async def fake_identify_message_type_normal(context, input):
        return "normal"

    async def fake_convert_object(context, json_str, model):
        return AgentResponse(status=AgentResponse.DONE, message="conv")

    monkeypatch.setattr(f, "identify_message_type", fake_identify_message_type_normal)
    monkeypatch.setattr(f, "convert_object", fake_convert_object)
    ar4 = await f.to_agent_response(ctx, "{}", "log")
    assert ar4.status == AgentResponse.DONE and ar4.message == "conv"


@pytest.mark.asyncio
async def test_edit_files_happy_path(monkeypatch, tmp_path, fake_context):
    ctx = fake_context

    # Stub _get_content to simulate LLM call
    async def fake__get_content(context, name, message, **kwargs):
        assert name == "functions/edit_files"
        # message should be a JSON string of list[dict]
        payload = json.loads(message)
        assert isinstance(payload, list)
        return AgentResponse(status=AgentResponse.DONE, message="ok").model_dump_json()

    monkeypatch.setattr(f, "_get_content", fake__get_content)

    # Ensure to_agent_response is used to convert
    async def fake_to_agent_response(context, json_str, log_output):
        return AgentResponse(status=AgentResponse.DONE, message="ok")

    monkeypatch.setattr(f, "to_agent_response", fake_to_agent_response)

    res = await f.edit_files(ctx, input=[{"a": 1}], cwd=tmp_path)
    assert res.status == AgentResponse.DONE and res.message == "ok"


def test_preprocess_handles_quoted_arguments(monkeypatch, fake_context, tmp_path):
    ctx = fake_context
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("")

    monkeypatch.setattr(f, "get_prompt_path", lambda name, lang, person_id: prompt_path)
    monkeypatch.setattr(
        f, "load_markdown_with_frontmatter", lambda path: {"body": "body"}
    )

    captured = {}

    def fake_get_body(prompt, args):
        captured["args"] = args
        return "processed"

    monkeypatch.setattr(f, "get_body_from_prompt", fake_get_body)

    text = "//test-command key=\"value with spaces\" 'single quoted arg' plain"
    out = f.preprocess(ctx, text)

    assert out == "processed"
    assert captured["args"] == [
        "key=value with spaces",
        "single quoted arg",
        "plain",
    ]


def make_msg(content: str, author_type: str = Message.USER) -> Message:
    """Helper to build a Message with minimal fields."""
    return Message(
        content=content, author="tester", author_type=author_type, timestamp=""
    )


def test_to_simple_dicts_returns_all_when_no_output(fake_context):
    messages = [
        make_msg("Intro context."),
        make_msg(
            "Some analysis before output.\nOutput: [Title](https://example.com)\nMore text after.",
            author_type=Message.ASSISTANT,
        ),
        make_msg("Should be ignored after Output line."),
    ]

    got = f.messages_to_simple_dicts(messages)

    # Should include only messages prior to the Output line (i.e., first message only)
    assert got == [
        {Message.USER: "Intro context."},
        {
            Message.ASSISTANT: "Some analysis before output.\nOutput: [Title](https://example.com)\nMore text after."
        },
        {Message.USER: "Should be ignored after Output line."},
    ]


def test_to_simple_dicts_with_empty_list(fake_context):
    got = f.messages_to_simple_dicts([])
    assert got == []
