from __future__ import annotations

import base64
import builtins
import os
import sys
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest

from guildbotics.commands.errors import CommandError
from guildbotics.commands.models import CommandSpec
from guildbotics.commands.to_pdf_command import (
    ToPdfCommand,
    _homebrew_library_path,
)
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


@pytest.mark.parametrize("error", [ModuleNotFoundError, OSError])
@pytest.mark.asyncio
async def test_to_pdf_raises_command_error_when_weasyprint_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
):
    """Packaged sidecars exclude WeasyPrint; surface a friendly CommandError."""

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "weasyprint":
            raise error("weasyprint unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    ctx = _make_context("# PDF Title")
    spec = _make_spec(tmp_path)
    command = ToPdfCommand(ctx, spec, tmp_path)

    with pytest.raises(CommandError, match="WeasyPrint native dependencies"):
        await command.run()


def _use_homebrew_prefix(
    monkeypatch: pytest.MonkeyPatch, prefix: Path, platform: str = "darwin"
) -> None:
    """Point the Homebrew lookup at a temporary prefix on a chosen platform.

    Earlier tests in this module import WeasyPrint, so the module is dropped
    from ``sys.modules`` to exercise the first-import path the override exists
    for.
    """
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("HOMEBREW_PREFIX", str(prefix))
    monkeypatch.delenv("DYLD_LIBRARY_PATH", raising=False)
    monkeypatch.delitem(sys.modules, "weasyprint", raising=False)


def test_homebrew_library_path_prepends_library_dir_on_macos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """macOS does not search Homebrew's prefix, so to_pdf has to add it."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    _use_homebrew_prefix(monkeypatch, tmp_path)

    with _homebrew_library_path():
        assert os.environ["DYLD_LIBRARY_PATH"] == str(lib_dir)

    assert "DYLD_LIBRARY_PATH" not in os.environ


def test_homebrew_library_path_keeps_an_explicit_setting_ahead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An explicitly configured search path keeps priority and is restored."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    _use_homebrew_prefix(monkeypatch, tmp_path)
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/custom/lib")

    with _homebrew_library_path():
        assert os.environ["DYLD_LIBRARY_PATH"] == f"/custom/lib{os.pathsep}{lib_dir}"

    assert os.environ["DYLD_LIBRARY_PATH"] == "/custom/lib"


@pytest.mark.parametrize(
    ("platform", "create_lib_dir"),
    [("linux", True), ("darwin", False)],
    ids=["other-platform", "no-homebrew-prefix"],
)
def test_homebrew_library_path_is_a_no_op_when_not_applicable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    create_lib_dir: bool,
):
    """Only macOS with a Homebrew prefix present needs the override."""
    if create_lib_dir:
        (tmp_path / "lib").mkdir()
    _use_homebrew_prefix(monkeypatch, tmp_path, platform=platform)

    with _homebrew_library_path():
        assert "DYLD_LIBRARY_PATH" not in os.environ

    assert "DYLD_LIBRARY_PATH" not in os.environ


def test_homebrew_library_path_is_a_no_op_once_weasyprint_is_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """After the first import the libraries are resolved, so leave the env alone."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    _use_homebrew_prefix(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "weasyprint", ModuleType("weasyprint"))

    with _homebrew_library_path():
        assert "DYLD_LIBRARY_PATH" not in os.environ

    assert "DYLD_LIBRARY_PATH" not in os.environ


def test_homebrew_library_path_does_not_interleave_across_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The task scheduler runs one worker thread per member.

    Two conversions entering the override around the same time must not
    overlap: an unsynchronized save/restore lets the second thread capture the
    first thread's override as its own "previous" value and put the Homebrew
    path back after both have finished, leaking it into child processes.
    """
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    _use_homebrew_prefix(monkeypatch, tmp_path)

    steps: list[str] = []
    first_is_inside = threading.Event()
    second_is_entering = threading.Event()

    def enter_first() -> None:
        with _homebrew_library_path():
            steps.append("first-enter")
            first_is_inside.set()
            assert second_is_entering.wait(timeout=5)
            # Give an unsynchronized implementation time to slip in before the
            # override is restored.
            time.sleep(0.05)
            steps.append("first-exit")

    def enter_second() -> None:
        assert first_is_inside.wait(timeout=5)
        second_is_entering.set()
        with _homebrew_library_path():
            steps.append("second-enter")
            steps.append("second-exit")

    threads = [
        threading.Thread(target=enter_first),
        threading.Thread(target=enter_second),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert [thread.is_alive() for thread in threads] == [False, False]
    assert steps == ["first-enter", "first-exit", "second-enter", "second-exit"]
    assert "DYLD_LIBRARY_PATH" not in os.environ


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
