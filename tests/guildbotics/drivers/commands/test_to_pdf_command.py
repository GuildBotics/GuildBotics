from __future__ import annotations

import base64
from pathlib import Path

import pytest

from guildbotics.drivers.commands.models import CommandSpec
from guildbotics.drivers.commands.to_pdf_command import ToPdfCommand
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
    base = Context.get_default(loader_factory, integration_factory, brain_factory, message)
    return base.clone_for(members[0])


def _make_spec(tmp_path: Path, params: dict | None = None, config: dict | None = None) -> CommandSpec:
    return CommandSpec(
        name="inline_to_pdf",
        base_dir=tmp_path,
        command_class=ToPdfCommand,
        path=None,
        params=params or {},
        args=[],
        stdin_override=None,
        cwd=tmp_path,
        command_index=0,
        config=config or {},
    )


@pytest.mark.asyncio
async def test_to_pdf_generates_pdf_from_markdown_message(tmp_path: Path):
    ctx = _make_context("# PDF Title")
    spec = _make_spec(tmp_path)
    command = ToPdfCommand(ctx, spec, tmp_path)

    outcome = await command.run()

    assert isinstance(outcome.result, (bytes, bytearray))
    assert outcome.result.startswith(b"%PDF")
    decoded = base64.b64decode(outcome.text_output)
    assert decoded == outcome.result


@pytest.mark.asyncio
async def test_to_pdf_writes_output_file_when_requested(tmp_path: Path):
    ctx = _make_context("Hello PDF")
    output_path = Path("out/report.pdf")
    spec = _make_spec(tmp_path, params={"output": str(output_path)})
    command = ToPdfCommand(ctx, spec, tmp_path)

    outcome = await command.run()

    resolved = (tmp_path / output_path).resolve()
    assert resolved.exists()
    assert outcome.result.startswith(b"%PDF")
    assert outcome.text_output == str(resolved)
    assert resolved.read_bytes() == outcome.result


@pytest.mark.asyncio
async def test_to_pdf_accepts_html_input(tmp_path: Path):
    html_path = tmp_path / "snippet.html"
    html_path.write_text("<div><h1>HTML</h1><p>content</p></div>", encoding="utf-8")

    ctx = _make_context("should not be used")
    spec = _make_spec(tmp_path, params={"input": str(html_path)})
    command = ToPdfCommand(ctx, spec, tmp_path)

    outcome = await command.run()

    assert outcome.result.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_to_pdf_handles_full_html_document(tmp_path: Path):
    html_path = tmp_path / "document.html"
    html_path.write_text(
        "<!DOCTYPE html><html><head><title>Doc</title></head>"
        "<body><h1>Title</h1><p>body</p></body></html>",
        encoding="utf-8",
    )

    ctx = _make_context("should not be used")
    spec = _make_spec(tmp_path, params={"input": str(html_path)})
    command = ToPdfCommand(ctx, spec, tmp_path)

    outcome = await command.run()

    assert outcome.result.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_to_pdf_accepts_string_inline_syntax(tmp_path: Path):
    css_path = tmp_path / "custom.css"
    css_path.write_text(".markdown-body { color: blue; }", encoding="utf-8")
    output_path = Path("inline/output.pdf")
    ctx = _make_context("# Inline Syntax")
    spec = _make_spec(
        tmp_path,
        params={},
        config={"to_pdf": f"{output_path} css={css_path.name}"},
    )
    command = ToPdfCommand(ctx, spec, tmp_path)

    outcome = await command.run()

    resolved = (tmp_path / output_path).resolve()
    assert resolved.exists()
    assert outcome.text_output == str(resolved)
    assert outcome.result.startswith(b"%PDF")
