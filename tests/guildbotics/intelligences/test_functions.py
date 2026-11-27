import json
import logging
from pathlib import Path

import pytest

from guildbotics.entities.message import Message
from guildbotics.entities.team import Person, Role
from guildbotics.intelligences import functions as f
from guildbotics.intelligences.common import (
    AgentResponse,
    DecisionResponse,
    MessageResponse,
)
from guildbotics.utils.fileio import load_markdown_with_frontmatter
from guildbotics.utils.import_utils import ClassResolver


class FakeProject:
    """
    Fake project for testing, returns English language code and name.
    """

    def get_language_code(self) -> str:
        return "en"

    def get_language_name(self) -> str:
        return "English"


class FakeContext:
    """
    Fake context that holds team, person, task, logger, and a brain registry.
    """

    def __init__(self):
        # Minimal team and project for intelligences.functions
        self.team = type("T", (), {"project": FakeProject()})()
        # Create a person with "dev" and "pm" roles and account_info
        self.person = Person(
            person_id="p1",
            name="Tester",
            roles={
                "dev": Role(id="dev", summary="Developer", description="Writes code"),
                "pm": Role(id="pm", summary="PM", description="Manages project"),
            },
        )
        # Add account_info to person
        self.person.account_info = {
            "git_user": "Test User",
            "git_email": "test@example.com",
        }
        self.logger = logging.getLogger("test.context")
        # Registry for fake brains
        self._brains: dict[str, FakeBrain] = {}

    def get_brain(
        self, name: str, config: dict | None, class_resolver: ClassResolver | None
    ):
        return self._brains[name]


class FakeBrain:
    """
    Fake brain that returns a preset result and optional response_class.
    """

    def __init__(self, result, response_class=None):
        self._result = result
        self.response_class = response_class

    async def run(self, **kwargs):
        return self._result


@pytest.fixture
def fake_context() -> FakeContext:
    """
    Provides a FakeContext for tests.
    """
    return FakeContext()


@pytest.fixture
def stub_brain(fake_context):
    """
    Provides a helper to register a FakeBrain in the fake_context.

    Usage:
        stub_brain(name: str, result, response_class=None)
    """

    def _stub(name: str, result, response_class=None):
        fake_context._brains[name] = FakeBrain(result, response_class)

    return _stub


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


def test_handle_ticket_prompt_keeps_only_trigger_specific_contract():
    prompt = load_markdown_with_frontmatter(
        Path("guildbotics/templates/commands/functions/handle_ticket.en.md")
    )

    body = prompt["body"]
    assert prompt["response_class"] == "guildbotics.intelligences.common.AgentResponse"
    # The shared workflow envelope arrives via injection, never restated inline.
    assert "{workflow_contract}" in body
    assert "member task complete" in body
    assert "Use `{ticket_url}` as this run's memory source key" in body
    # The workflow no longer injects issue content; the agent inspects it.
    assert "{issue_title}" not in body
    assert "{issue_description}" not in body
    # PR-review safety: the agent runs the workflow-provided prepare command,
    # which carries --pr-url for PR review.
    assert "{prepare_command}" in body
    assert "--pr-url" in body
    assert "include_diff=true" in body
    assert "commentable_lines" in body
    assert "pr review-comment" in body
    assert "path" in body
    assert "line" in body
    assert "side" in body
    assert "reply_target_id" in body
    assert "repository/pr_checks" in body
    assert "required_checks=passed" in body
    # Envelope and shared-procedure content lives in the injected contract and
    # the member capability reference only.
    assert "--workspace-mode" not in body
    assert "guildbotics member context" not in body
    assert "secret" not in body.lower()


def test_handle_chat_event_prompt_keeps_only_trigger_specific_contract():
    prompt = load_markdown_with_frontmatter(
        Path("guildbotics/templates/commands/functions/handle_chat_event.en.md")
    )

    body = prompt["body"]
    assert prompt["response_class"] == "guildbotics.intelligences.common.AgentResponse"
    assert "{workflow_contract}" in body
    assert "member chat complete" in body
    assert "chat inspect thread" in body
    assert "reply / reaction-only / no-op / asking / blocked" in body
    # Chat-originated code work needs no pre-existing ticket: prepare runs from
    # a repository and branch, and ticket/PR anchors are only for explicit
    # references.
    assert "--repo <repository> --branch <branch>" in body
    assert "no ticket has to be created first" in body
    # Envelope and shared-procedure content lives in the injected contract and
    # the member capability reference only.
    assert "--workspace-mode" not in body
    assert "guildbotics member context" not in body
    assert "secret" not in body.lower()


def test_guildbotics_skill_keeps_only_interactive_envelope():
    skill = load_markdown_with_frontmatter(Path("skills/guildbotics/SKILL.md"))

    body = skill["body"]
    assert skill["name"] == "guildbotics"
    # Interactive envelope: member context first, active member session, shared
    # workspace with --workspace-mode current, interactive DOD, marker guardrail.
    assert "member context" in body
    assert "persona" in body
    assert "Active Member Session Rules" in body
    assert "active GuildBotics member for the rest of the conversation/session" in body
    assert "communication_style.interactive_replies" in body
    # The active member voice must persist across the whole interactive session,
    # including intermediate progress updates (not only the final reply) — this
    # is interactive-only and must stay in the skill envelope.
    assert "progress updates" in body
    assert "not neutral task summaries" in body
    assert "shared pair-programming workspace" in body
    assert "--workspace-mode current" in body
    assert "Do not run `member git prepare`" in body
    assert "Definition of Done" in body
    assert "standard work procedure" in body
    assert "required_checks=passed" in body
    assert "guildbotics_execution_mode=workflow" in body
    # Shared procedure, memory contracts, and command details live in the member
    # capability reference; the skill no longer restates per-domain flows.
    assert "GitHub Issue Flow" not in body
    assert "PR Review Flow" not in body
    assert "Slack Chat Flow" not in body
    assert "--pr <pr_url>" not in body


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
