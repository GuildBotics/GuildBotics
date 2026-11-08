"""Shared command machinery contracts retained from the frozen host tests."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from guildbotics.commands.errors import CommandError
from guildbotics.commands.models import CommandOutcome, CommandSpec
from guildbotics.commands.runner import CommandRunner
from guildbotics.commands.spec_factory import CommandSpecFactory
from guildbotics.guest.cli_agent import PromptInfo


class DummyCommand:
    last_cwd = None

    def __init__(self, context, spec, cwd):
        DummyCommand.last_cwd = cwd
        self.options = SimpleNamespace(output_key=spec.name)

    async def run(self):
        return CommandOutcome(result="ok", text_output="ok")


class DummyContext:
    def __init__(self):
        self.shared_state = {}
        self.pipe = ""
        self.invoker = None
        self.person = SimpleNamespace(person_id="aiko")

    def set_invoker(self, invoker):
        self.invoker = invoker

    def update(self, key, value, text_value):
        self.shared_state[key] = value
        self.pipe = text_value


def _main_spec():
    return CommandSpec(
        name="main",
        base_dir=Path("."),
        command_class=DummyCommand,
        path=Path("main.md"),
        cwd=Path("/workspace"),
    )


def _runner(context=None, spec=None, **kwargs):
    """The machinery for ``spec`` (``main`` by default), working in
    ``/workspace`` where it may work anywhere."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            CommandSpecFactory,
            "prepare_main_spec",
            lambda self, *_: spec or _main_spec(),
        )
        return CommandRunner(
            context or DummyContext(),
            "main",
            [],
            Path("/workspace"),
            path=Path("main.md"),
            mounts={"/": True},
            **kwargs,
        )


@pytest.mark.asyncio
async def test_invoke_passes_top_level_cwd_to_spec_factory(monkeypatch):
    runner = _runner()

    captured = {}

    def fake_build(anchor, entry, index):
        captured["entry"] = entry
        return _main_spec()

    async def fake_run_with_children(spec):
        return CommandOutcome(result="ok", text_output="ok")

    runner._spec_factory.build_from_entry = fake_build
    runner._run_with_children = fake_run_with_children

    await runner._invoke("child", cwd=Path("/memory"), foo="bar")

    assert captured["entry"]["cwd"] == Path("/memory")
    assert captured["entry"]["params"] == {"foo": "bar"}


@pytest.mark.asyncio
async def test_invoke_drives_completion_managed_turns_with_the_host_ledger(monkeypatch):
    from guildbotics.commands import runner as runner_module

    ledger = object()
    runner = _runner(ledger=ledger)
    captured = {}

    async def fake_run_agent_turn(*, invoke, execution_context, ledger):
        captured["execution_context"] = execution_context
        captured["ledger"] = ledger
        return await invoke(
            {**execution_context, "attempt": 2}, {"previous_attempt_evidence": "[]"}
        )

    def fake_build(anchor, entry, index):
        captured["entry"] = entry
        return _main_spec()

    async def fake_run_with_children(spec):
        return CommandOutcome(result="completed", text_output="completed")

    monkeypatch.setattr(runner_module, "run_agent_turn", fake_run_agent_turn)
    runner._spec_factory.build_from_entry = fake_build
    runner._run_with_children = fake_run_with_children

    result = await runner._invoke(
        "child",
        cwd=Path("/memory"),
        agent_execution_context={
            "run_id": "run-1",
            "work_kind": "ticket",
            "max_completion_attempts": 3,
        },
    )

    assert result == "completed"
    assert captured["execution_context"]["run_id"] == "run-1"
    assert captured["ledger"] is ledger
    assert captured["entry"]["params"]["agent_execution_context"]["attempt"] == 2
    # The host's per-attempt prompt parameters reach the command.
    assert captured["entry"]["params"]["previous_attempt_evidence"] == "[]"
    assert captured["entry"]["cwd"] == Path("/memory")


@pytest.mark.asyncio
async def test_invoke_refuses_completion_managed_turns_without_a_ledger(monkeypatch):
    runner = _runner()

    async def fail_run_with_children(spec):
        raise AssertionError("the turn must not start")

    runner._run_with_children = fail_run_with_children

    with pytest.raises(CommandError, match="run ledger"):
        await runner._invoke(
            "child",
            agent_execution_context={
                "run_id": "run-1",
                "work_kind": "ticket",
                "max_completion_attempts": 3,
            },
        )


@pytest.mark.asyncio
async def test_run_uses_spec_cwd_not_runner_cwd(monkeypatch):
    runner = _runner()
    spec = CommandSpec(
        name="child",
        base_dir=Path("."),
        command_class=DummyCommand,
        cwd=Path("/memory"),
    )

    await runner._run(spec)

    assert DummyCommand.last_cwd == Path("/memory")


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "ja"])
async def test_ask_passes_message_member_and_working_tree_to_brain(
    tmp_path, monkeypatch, language
):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path / "config"))
    working_tree = tmp_path / "working tree"
    working_tree.mkdir()
    edited_file = working_tree / "uncommitted.txt"
    edited_file.write_text("local edit", encoding="utf-8")
    ctx = DummyContext()
    ctx.person = SimpleNamespace(person_id="alice")
    ctx.team = SimpleNamespace(
        project=SimpleNamespace(get_language_code=lambda: language)
    )
    request = "Review the current edits. 日本語・`$()`\nDo not publish."
    ctx.pipe = request

    def get_brain(name, config, class_resolver):
        assert name == "ask"
        metadata = config
        assert metadata["brain"] == "agent"

        async def run(*, message, session_state, cwd):
            assert message == request
            assert cwd == working_tree
            assert (cwd / edited_file.name).read_text(encoding="utf-8") == "local edit"
            prompt = PromptInfo(None, metadata["body"]).to_prompt(
                message, session_state, metadata["template_engine"]
            )
            assert "guildbotics member context --person alice" in prompt
            assert "{{" not in prompt
            return "Review completed: local edit inspected."

        return SimpleNamespace(run=run, response_class=None)

    ctx.get_brain = get_brain
    template = (
        Path(__file__).parents[3]
        / "guildbotics/templates/commands"
        / f"ask.{language}.md"
    )
    outcome = await CommandRunner(
        ctx, "ask", [], working_tree, path=template, mounts={"/": True}
    ).run()

    result = "Review completed: local edit inspected."
    assert outcome.result == outcome.text_output == result
    assert ctx.shared_state["ask"] == result
    assert edited_file.read_text(encoding="utf-8") == "local edit"


@pytest.mark.usefixtures("symlinks")
def test_a_subcommand_works_in_the_real_path_of_a_link_from_the_home(tmp_path):
    # Inside the isolated environment, a granted directory under the home is a
    # link to where it is mounted; the subcommand and its turns work there.
    mounted = (tmp_path / "host" / "Documents" / "GuildBotics").resolve()
    mounted.mkdir(parents=True)
    home = tmp_path / "guest-home"
    (home / "Documents").mkdir(parents=True)
    (home / "Documents" / "GuildBotics").symlink_to(mounted)
    (home / "elsewhere").symlink_to(tmp_path)
    factory = CommandSpecFactory(DummyContext(), {mounted.as_posix(): True})

    for cwd in (home / "Documents" / "GuildBotics", Path("Documents/GuildBotics")):
        assert factory._resolve_cwd(cwd, home) == mounted
    with pytest.raises(CommandError):
        factory._resolve_cwd(home / "elsewhere", home)
