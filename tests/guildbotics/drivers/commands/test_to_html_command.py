from __future__ import annotations

from pathlib import Path

import pytest

from guildbotics.commands.models import CommandSpec
from guildbotics.commands.to_html_command import ToHtmlCommand
from guildbotics.entities.team import Person, Project, Team
from guildbotics.runtime.context import Context
from tests.guildbotics.runtime.test_context import (
    DummyBrainFactory,
    DummyIntegrationFactory,
    DummyLoaderFactory,
)


def _make_context(message: str = "") -> Context:
    members = [Person(person_id="alice", name="Alice", is_active=True)]
    team = Team(project=Project(name="demo"), members=members)
    loader_factory = DummyLoaderFactory(team)
    integration_factory = DummyIntegrationFactory()
    brain_factory = DummyBrainFactory()
    base = Context.get_default(
        loader_factory, integration_factory, brain_factory, message
    )
    return base.clone_for(members[0])


def _make_spec(
    tmp_path: Path, params: dict | None = None, config: dict | None = None
) -> CommandSpec:
    return CommandSpec(
        name="inline_to_html",
        base_dir=tmp_path,
        command_class=ToHtmlCommand,
        path=None,
        params=params or {},
        args=[],
        stdin_override=None,
        cwd=tmp_path,
        command_index=0,
        config=config or {},
    )


@pytest.mark.asyncio
async def test_to_html_uses_message_when_markdown_missing(tmp_path: Path):
    ctx = _make_context("Hello world")
    spec = _make_spec(tmp_path, params={}, config={"to_html": None})
    command = ToHtmlCommand(ctx, spec, tmp_path)

    outcome = await command.run()

    assert "<p>Hello world</p>" in outcome.result
    assert outcome.result == outcome.text_output
    assert "<style>" in outcome.result


@pytest.mark.asyncio
async def test_to_html_applies_custom_css_parameter(tmp_path: Path):
    css_path = tmp_path / "custom.css"
    css_path.write_text(".markdown-body { color: red; }", encoding="utf-8")

    ctx = _make_context("# Heading")
    params = {"css": str(css_path)}
    spec = _make_spec(tmp_path, params=params)
    command = ToHtmlCommand(ctx, spec, tmp_path)

    outcome = await command.run()

    assert "<h1>Heading</h1>" in outcome.result
    assert ".markdown-body { color: red; }" in outcome.result


@pytest.mark.asyncio
async def test_to_html_writes_output_file_when_requested(tmp_path: Path):
    ctx = _make_context("sample")
    output_relative = Path("output/result.html")
    params = {"output": str(output_relative)}
    spec = _make_spec(tmp_path, params=params)
    command = ToHtmlCommand(ctx, spec, tmp_path)

    outcome = await command.run()

    output_path = (tmp_path / output_relative).resolve()
    assert output_path.exists()
    written = output_path.read_text(encoding="utf-8")
    assert written == outcome.result
    assert "<p>sample</p>" in written


@pytest.mark.asyncio
async def test_to_html_reads_input_file_when_provided(tmp_path: Path):
    input_path = tmp_path / "input.md"
    input_path.write_text("# From file", encoding="utf-8")

    ctx = _make_context("should not use message")
    params = {"input": str(input_path)}
    spec = _make_spec(tmp_path, params=params)
    command = ToHtmlCommand(ctx, spec, tmp_path)

    outcome = await command.run()

    assert "<h1>From file</h1>" in outcome.result


@pytest.mark.asyncio
async def test_to_html_accepts_string_inline_syntax(tmp_path: Path):
    css_path = tmp_path / "inline.css"
    css_path.write_text(".markdown-body { font-weight: 700; }", encoding="utf-8")
    ctx = _make_context("# Title")
    output_relative = Path("inline/output.html")
    spec = _make_spec(
        tmp_path,
        params={},
        config={"to_html": f"{output_relative} css={css_path.name}"},
    )
    command = ToHtmlCommand(ctx, spec, tmp_path)

    outcome = await command.run()

    output_path = (tmp_path / output_relative).resolve()
    assert output_path.exists()
    written = output_path.read_text(encoding="utf-8")
    assert "<h1>Title</h1>" in written
    assert ".markdown-body { font-weight: 700; }" in written
