import json
from pathlib import Path

import pytest

from guildbotics.entities.message import Message
from guildbotics.intelligences import functions as f
from guildbotics.intelligences.common import (
    AgentResponse,
    DecisionResponse,
    MessageResponse,
)
from guildbotics.utils.fileio import load_markdown_with_frontmatter


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


def test_handle_github_ticket_prompt_uses_member_capability_contract():
    prompt = load_markdown_with_frontmatter(
        Path("guildbotics/templates/commands/functions/handle_github_ticket.en.md")
    )

    assert prompt["response_class"] == "guildbotics.intelligences.common.AgentResponse"
    assert "guildbotics member" in prompt["body"]
    assert "member task complete" in prompt["body"]
    assert "GitHubTicketAgentResult" not in prompt["body"]
    assert "capabilities section is the source of truth" in prompt["body"]
    assert "Use `{ticket_url}` as this run's memory source key" in prompt["body"]
    # The workflow no longer injects issue content; the agent inspects it.
    assert "{issue_title}" not in prompt["body"]
    assert "{issue_description}" not in prompt["body"]
    # PR-review safety: the agent runs the workflow-provided prepare command,
    # which carries --pr-url for PR review.
    assert "{prepare_command}" in prompt["body"]
    assert "--pr-url" in prompt["body"]
    assert "communication style" in prompt["body"]
    assert "neutral workflow execution summary" in prompt["body"]
    assert "guildbotics_execution_mode=workflow" in prompt["body"]
    assert "isolated member workspace" in prompt["body"]
    assert "--workspace-mode current" in prompt["body"]


def test_guildbotics_skill_uses_member_persona_without_decorating_control_data():
    skill = load_markdown_with_frontmatter(Path("skills/guildbotics/SKILL.md"))

    assert skill["name"] == "guildbotics"
    assert "member context" in skill["body"]
    assert "persona" in skill["body"]
    assert "communication style" in skill["body"]
    assert "capabilities` section returned by `member context`" in skill["body"]
    assert "source-vs-current-state handling" in skill["body"]
    assert "conversational outputs" in skill["body"]
    assert "issue titles/bodies, PR titles/bodies, commit messages" in skill["body"]
    assert "workflow `AgentResponse.message`" in skill["body"]
    assert "Active Member Session Rules" in skill["body"]
    assert (
        "active GuildBotics member for the rest of the conversation/session"
        in skill["body"]
    )
    assert "Workspace Rules" in skill["body"]
    assert "guildbotics_execution_mode=workflow" in skill["body"]
    assert "shared pair-programming workspace" in skill["body"]
    assert "--workspace-mode current" in skill["body"]
    assert "Do not run `member git prepare`" in skill["body"]
    assert (
        "After commit, push, PR creation, and final GitHub comment/reaction are done"
        in skill["body"]
    )
    assert "When a PR was created or updated, record durable context" in skill["body"]
    assert "When a PR was updated, record durable context" in skill["body"]


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
async def test_talk_as_builds_session_state(monkeypatch, fake_context):
    ctx = fake_context

    async def fake_get_content(context, name, message, params=None, cwd=None):
        return MessageResponse(content=" result ", author="ai", author_type="Assistant")

    monkeypatch.setattr(f, "get_content", fake_get_content)

    txt = await f.talk_as(
        ctx, topic="t", context_location="here", conversation_history=[]
    )
    assert txt == "result"
