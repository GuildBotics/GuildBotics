"""End-to-end unit tests for the command runner and command specs.

These tests exercise the real ``CommandRunner`` / ``CommandSpecFactory`` /
``Context`` collaboration using on-disk command files under ``tmp_path`` and
``GUILDBOTICS_CONFIG_DIR``. They focus on behaviours that the existing
``test_command_runner.py`` (which uses doubles) does not cover:

- ``Context.pipe`` and ``shared_state`` update ordering across a multi-command
  chain (children run before the parent; pipe reflects the last command).
- parent-command behaviour when a child command fails (error propagates,
  parent body never executes).
- YAML command ``commands:`` being empty / invalid / nested.
- Markdown command frontmatter options (``brain: none`` disables the brain and
  renders placeholders) and nested ``commands:``.
- Python command async vs sync ``main`` and context/positional/keyword binding.
- Shell command cwd / env / stderr / non-zero exit.
- inline ``print`` / ``to_html`` / ``to_pdf`` behaviour within a chain.
- person-specific command vs common command fallback.

External I/O is kept hermetic: shell commands use trivial real ``bash``
snippets, and ``to_pdf`` stubs ``weasyprint.HTML`` so the test does not depend
on native libraries.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import os
import signal
import sys
from pathlib import Path
from typing import Any

import pytest

from guildbotics.commands.errors import (
    CommandError,
    PersonNotFoundError,
    PersonSelectionRequiredError,
)
from guildbotics.commands.metadata import CommandAccess
from guildbotics.drivers.command_runner import run_command
from guildbotics.entities.team import Person, Project, Team
from guildbotics.runtime.context import Context
from guildbotics.runtime.person_lease import PersonExecutionLease
from guildbotics.utils import child_process
from tests.guildbotics.command_environment_doubles import machinery
from tests.guildbotics.runtime.test_context import (
    DummyBrainFactory,
    DummyIntegrationFactory,
    DummyLoaderFactory,
)

# --- Fixtures / helpers ----------------------------------------------------


def _make_team(members: list[Person] | None = None, language: str = "en") -> Team:
    if members is None:
        members = [Person(person_id="alice", name="Alice", is_active=True)]
    return Team(project=Project(name="demo", language=language), members=members)


def _make_context(message: str = "", team: Team | None = None) -> Context:
    team = team or _make_team()
    loader_factory = DummyLoaderFactory(team)
    base = Context.get_default(
        loader_factory, DummyIntegrationFactory(), DummyBrainFactory(), message
    )
    active = next((m for m in team.members if m.is_active), team.members[0])
    return base.clone_for(active)


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide an isolated primary config dir with a commands/ subdir."""
    root = tmp_path / "config"
    (root / "commands").mkdir(parents=True)
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(root))
    return root


async def _run_main(
    config_dir: Path,
    name: str,
    args: list[str] | None = None,
    message: str = "",
    language: str = "en",
) -> Context:
    """Run a named command resolved from config_dir and return its context."""
    ctx = _make_context(message, _make_team(language=language))
    runner = machinery(ctx, name, args or [], config_dir)
    await runner.run()
    return ctx


# --- pipe / shared_state ordering across a chain ---------------------------


@pytest.mark.asyncio
async def test_chain_runs_children_before_parent_and_orders_pipe(config_dir: Path):
    """Children run first; pipe ends with the parent output; shared_state keeps all."""
    commands = config_dir / "commands"
    # Parent yaml referencing two python children, then a print parent step.
    (commands / "first.py").write_text(
        "def main():\n    return 'first-output'\n", encoding="utf-8"
    )
    (commands / "second.py").write_text(
        "def main():\n    return 'second-output'\n", encoding="utf-8"
    )
    (commands / "chain.md").write_text(
        "---\n"
        "brain: none\n"
        "commands:\n"
        "  - first\n"
        "  - second\n"
        "---\n"
        "parent saw: {first} & {second}\n",
        encoding="utf-8",
    )

    ctx = await _run_main(config_dir, "chain")

    # Children executed before parent and recorded under their own output keys.
    assert ctx.shared_state["first"] == "first-output"
    assert ctx.shared_state["second"] == "second-output"
    # Parent (markdown brain=none) rendered placeholders from shared_state and
    # is the last to write the pipe.
    assert ctx.shared_state["chain"] == "parent saw: first-output & second-output"
    assert ctx.pipe == "parent saw: first-output & second-output"


@pytest.mark.asyncio
@pytest.mark.parametrize("command_name", ["child", "parent"])
async def test_unnamed_output_uses_child_local_index(
    config_dir: Path, command_name: str
) -> None:
    commands = config_dir / "commands"
    (commands / "child.md").write_text(
        "---\nbrain: none\ncommands:\n  - print: inner-output\n---\n"
        "child saw {child__1}\n",
        encoding="utf-8",
    )
    (commands / "parent.md").write_text(
        "---\nbrain: none\ncommands:\n  - print: earlier\n"
        "  - path: child.md\n---\nparent saw {child}\n",
        encoding="utf-8",
    )

    ctx = await _run_main(config_dir, command_name)

    assert ctx.shared_state["child__1"] == "inner-output"
    assert ctx.shared_state["child"] == "child saw inner-output"
    assert list(ctx.shared_state) == (
        ["child__1", "child"]
        if command_name == "child"
        else ["parent__1", "child__1", "child", "parent"]
    )
    assert ctx.pipe == (
        "child saw inner-output"
        if command_name == "child"
        else "parent saw child saw inner-output"
    )


@pytest.mark.asyncio
async def test_unnamed_output_uses_source_name_under_aliases(config_dir: Path) -> None:
    commands = config_dir / "commands"
    (commands / "child.md").write_text(
        "---\nbrain: none\ncommands:\n  - print: '{{ label }}'\n---\n"
        "child saw {child__1}\n",
        encoding="utf-8",
    )
    (commands / "parent.md").write_text(
        "---\nbrain: none\ncommands:\n"
        "  - name: first\n    path: child.md\n    params: {label: one}\n"
        "  - name: second\n    path: child.md\n    params: {label: two}\n---\n"
        "parent saw {first} and {second}\n",
        encoding="utf-8",
    )

    ctx = await _run_main(config_dir, "parent")

    assert ctx.shared_state["child__1"] == "two"
    assert ctx.shared_state["first"] == "child saw one"
    assert ctx.shared_state["second"] == "child saw two"
    assert "first__1" not in ctx.shared_state
    assert "second__1" not in ctx.shared_state
    assert ctx.pipe == "parent saw child saw one and child saw two"


@pytest.mark.asyncio
@pytest.mark.parametrize("command_name", ["sub/child", "parent"])
async def test_unnamed_output_uses_source_basename_in_subdirectory(
    config_dir: Path, command_name: str
) -> None:
    commands = config_dir / "commands"
    (commands / "sub").mkdir()
    (commands / "sub/child.md").write_text(
        "---\nbrain: none\ncommands:\n  - print: inner-output\n---\n"
        "child saw {child__1}\n",
        encoding="utf-8",
    )
    (commands / "parent.md").write_text(
        "---\nbrain: none\ncommands:\n  - path: sub/child.md\n---\n"
        "parent saw {child}\n",
        encoding="utf-8",
    )

    ctx = await _run_main(config_dir, command_name)

    assert ctx.shared_state["child__1"] == "inner-output"
    assert "sub/child__1" not in ctx.shared_state
    assert ctx.pipe == (
        "child saw inner-output"
        if command_name == "sub/child"
        else "parent saw child saw inner-output"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "ja"])
async def test_unnamed_output_uses_logical_name_for_localized_source(
    config_dir: Path, language: str
) -> None:
    commands = config_dir / "commands"
    (commands / f"child.{language}.md").write_text(
        "---\nbrain: none\ncommands:\n  - print: inner-output\n---\n"
        "child saw {child__1}\n",
        encoding="utf-8",
    )
    (commands / "parent.md").write_text(
        "---\nbrain: none\ncommands:\n"
        f"  - name: alias\n    path: child.{language}.md\n---\n"
        "parent saw {alias}\n",
        encoding="utf-8",
    )

    ctx = await _run_main(config_dir, "parent", language=language)

    assert ctx.shared_state["child__1"] == "inner-output"
    assert ctx.shared_state["alias"] == "child saw inner-output"


@pytest.mark.asyncio
async def test_unnamed_output_index_is_stable_across_repeated_runs(
    config_dir: Path,
) -> None:
    (config_dir / "commands/child.md").write_text(
        "---\nbrain: none\ncommands:\n  - print: inner-output\n---\n"
        "child saw {child__1}\n",
        encoding="utf-8",
    )
    ctx = _make_context()
    runner = machinery(ctx, "child", [], config_dir)

    await runner.run()
    await runner.run()

    assert list(ctx.shared_state) == ["child__1", "child"]
    assert ctx.shared_state["child"] == "child saw inner-output"


@pytest.mark.asyncio
@pytest.mark.usefixtures("shell_commands")
async def test_pipe_flows_as_stdin_into_each_command(config_dir: Path):
    """pipe is fed as stdin/message; each command can transform and forward it."""
    commands = config_dir / "commands"
    # A shell child uppercases stdin, the parent shell appends a marker.
    (commands / "upper.sh").write_text(
        "#!/usr/bin/env bash\ntr '[:lower:]' '[:upper:]'\n", encoding="utf-8"
    )
    (commands / "pipe_parent.yml").write_text(
        "commands:\n  - upper\n",
        encoding="utf-8",
    )

    ctx = await _run_main(config_dir, "pipe_parent", message="hello")

    # Child shell read the initial pipe ("hello") from stdin and uppercased it.
    assert ctx.shared_state["upper"].strip() == "HELLO"
    # Yaml parent returns None, so the pipe stays at the child's output.
    assert ctx.pipe.strip() == "HELLO"


# --- child command failure -> parent never runs ----------------------------


@pytest.mark.asyncio
async def test_child_failure_propagates_and_parent_not_executed(config_dir: Path):
    """A failing child raises CommandError and the parent body never runs."""
    commands = config_dir / "commands"
    (commands / "boom.py").write_text(
        "def main():\n    raise ValueError('child exploded')\n", encoding="utf-8"
    )
    (commands / "outer.md").write_text(
        "---\nbrain: none\ncommands:\n  - boom\n---\nPARENT-MARKER\n",
        encoding="utf-8",
    )

    ctx = _make_context("seed")
    runner = machinery(ctx, "outer", [], config_dir)

    with pytest.raises(ValueError, match="child exploded"):
        await runner.run()

    # Parent markdown body never produced output; pipe untouched by the parent.
    assert "outer" not in ctx.shared_state
    assert ctx.pipe == "seed"


# --- YAML command commands: empty / invalid / nested -----------------------


@pytest.mark.asyncio
async def test_yaml_empty_commands_is_noop(config_dir: Path):
    """An empty commands: list produces no children and leaves pipe unchanged."""
    commands = config_dir / "commands"
    (commands / "empty.yml").write_text("commands: []\n", encoding="utf-8")

    ctx = await _run_main(config_dir, "empty", message="unchanged")

    assert ctx.shared_state == {}
    assert ctx.pipe == "unchanged"


@pytest.mark.asyncio
async def test_yaml_single_mapping_command_executes(config_dir: Path):
    """A single mapping may be declared without wrapping it in a list."""
    commands = config_dir / "commands"
    (commands / "single.yml").write_text(
        "commands:\n  name: greeting\n  print: hello\n", encoding="utf-8"
    )

    ctx = await _run_main(config_dir, "single")

    assert ctx.shared_state["greeting"] == "hello"


@pytest.mark.asyncio
async def test_yaml_invalid_command_entry_raises(config_dir: Path):
    """A non-string / non-mapping command entry raises a CommandError."""
    commands = config_dir / "commands"
    (commands / "bad.yml").write_text("commands:\n  - [1, 2, 3]\n", encoding="utf-8")

    ctx = _make_context()
    runner = machinery(ctx, "bad", [], config_dir)

    with pytest.raises(CommandError, match="must be a mapping or string"):
        await runner.run()


@pytest.mark.asyncio
async def test_yaml_nested_commands_execute_depth_first(config_dir: Path):
    """Nested yaml commands run their grandchildren before children."""
    commands = config_dir / "commands"
    (commands / "leaf.py").write_text(
        "def main():\n    return 'leaf'\n", encoding="utf-8"
    )
    (commands / "inner.yml").write_text("commands:\n  - leaf\n", encoding="utf-8")
    (commands / "root.yml").write_text("commands:\n  - inner\n", encoding="utf-8")

    ctx = await _run_main(config_dir, "root", message="x")

    # Grandchild python ran and recorded output; yaml wrappers return None.
    assert ctx.shared_state["leaf"] == "leaf"
    assert ctx.pipe == "leaf"


# --- Markdown frontmatter options ------------------------------------------


@pytest.mark.asyncio
async def test_markdown_brain_disabled_renders_placeholders(config_dir: Path):
    """brain: none renders the body with default placeholder substitution."""
    commands = config_dir / "commands"
    (commands / "render.md").write_text(
        "---\nbrain: none\n---\nName is $name\n", encoding="utf-8"
    )

    ctx = _make_context("ignored-pipe")
    ctx.shared_state["name"] = "Ada"
    runner = machinery(ctx, "render", [], config_dir)
    await runner.run()

    assert ctx.shared_state["render"] == "Name is Ada"
    assert ctx.pipe == "Name is Ada"


@pytest.mark.asyncio
async def test_markdown_declared_arguments_apply_defaults(config_dir: Path):
    commands = config_dir / "commands"
    (commands / "summarize.md").write_text(
        "---\n"
        "brain: none\n"
        "args:\n"
        "  file:\n"
        "    required: true\n"
        "  language:\n"
        "    default: English\n"
        "---\n"
        "Summarize ${file} using ${language}.\n",
        encoding="utf-8",
    )

    ctx = await _run_main(config_dir, "summarize", ["file=README.md"])

    assert ctx.pipe == "Summarize README.md using English."


@pytest.mark.asyncio
async def test_markdown_declared_arguments_reject_missing_required_value(
    config_dir: Path,
):
    commands = config_dir / "commands"
    (commands / "summarize.md").write_text(
        "---\nargs:\n  file:\n    required: true\n---\nRead ${file}.\n",
        encoding="utf-8",
    )

    with pytest.raises(CommandError, match="Missing required command arguments: file"):
        await _run_main(config_dir, "summarize")


@pytest.mark.asyncio
async def test_markdown_empty_body_returns_none(config_dir: Path):
    """A markdown command with no body produces no outcome and no pipe change."""
    commands = config_dir / "commands"
    (commands / "blank.md").write_text("---\nbrain: none\n---\n", encoding="utf-8")

    ctx = await _run_main(config_dir, "blank", message="keep")

    assert "blank" not in ctx.shared_state
    assert ctx.pipe == "keep"


# --- Python command async vs sync, context/positional/keyword binding ------


@pytest.mark.asyncio
async def test_python_sync_main_returns_value(config_dir: Path):
    commands = config_dir / "commands"
    (commands / "sync_cmd.py").write_text(
        "def main():\n    return 'sync-result'\n", encoding="utf-8"
    )

    ctx = await _run_main(config_dir, "sync_cmd")

    assert ctx.shared_state["sync_cmd"] == "sync-result"
    assert ctx.pipe == "sync-result"


@pytest.mark.asyncio
async def test_python_async_main_is_awaited(config_dir: Path):
    commands = config_dir / "commands"
    (commands / "async_cmd.py").write_text(
        "import asyncio\n\n"
        "async def main():\n"
        "    await asyncio.sleep(0)\n"
        "    return 'async-result'\n",
        encoding="utf-8",
    )

    ctx = await _run_main(config_dir, "async_cmd")

    # An awaited coroutine result, not a coroutine object.
    assert ctx.shared_state["async_cmd"] == "async-result"
    assert ctx.pipe == "async-result"


@pytest.mark.asyncio
async def test_python_main_receives_context_positional_and_keyword(config_dir: Path):
    commands = config_dir / "commands"
    (commands / "bound.py").write_text(
        "def main(context, first, second, *, flag):\n"
        "    return f'{context.person.person_id}:{first}:{second}:{flag}'\n",
        encoding="utf-8",
    )

    ctx = await _run_main(
        config_dir, "bound", args=["pos1", "pos2", "flag=on"], message=""
    )

    assert ctx.shared_state["bound"] == "alice:pos1:pos2:on"


# --- Shell command cwd / env / stderr / non-zero exit ----------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("shell_commands")
async def test_shell_runs_in_spec_cwd(config_dir: Path, tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir()
    # The directory is read back through a relative path rather than through
    # ``pwd``: a shell may spell the same directory its own way -- Git for
    # Windows answers ``/c/...`` -- while a relative read either finds the
    # marker or does not.
    (work / "marker.txt").write_text("in-the-spec-cwd\n", encoding="utf-8")
    commands = config_dir / "commands"
    (commands / "pwd.yml").write_text(
        f"commands:\n  - command: print_pwd\n    cwd: {work}\n", encoding="utf-8"
    )
    (commands / "print_pwd.sh").write_text(
        "#!/usr/bin/env bash\ncat marker.txt\n", encoding="utf-8"
    )

    ctx = await _run_main(config_dir, "pwd")

    assert ctx.shared_state["print_pwd"].strip() == "in-the-spec-cwd"


@pytest.mark.asyncio
@pytest.mark.usefixtures("shell_commands")
async def test_relative_spec_cwd_is_relative_to_the_calling_command(
    config_dir: Path, tmp_path: Path
):
    """A relative ``cwd:`` names a directory under the command's own working
    directory, never under the process's."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "marker.txt").write_text("under-the-command-cwd\n", encoding="utf-8")
    commands = config_dir / "commands"
    (commands / "pwd.yml").write_text(
        "commands:\n  - command: print_pwd\n    cwd: work\n", encoding="utf-8"
    )
    (commands / "print_pwd.sh").write_text(
        "#!/usr/bin/env bash\ncat marker.txt\n", encoding="utf-8"
    )
    ctx = _make_context()

    await machinery(ctx, "pwd", [], tmp_path).run()

    assert ctx.shared_state["print_pwd"].strip() == "under-the-command-cwd"


@pytest.mark.asyncio
@pytest.mark.usefixtures("shell_commands")
async def test_shell_receives_params_as_env(config_dir: Path):
    commands = config_dir / "commands"
    (commands / "env_echo.sh").write_text(
        '#!/usr/bin/env bash\necho "$GREETING"\n', encoding="utf-8"
    )
    (commands / "env_parent.yml").write_text(
        "commands:\n  - command: env_echo\n    params:\n      GREETING: from-env\n",
        encoding="utf-8",
    )

    ctx = await _run_main(config_dir, "env_parent")

    assert ctx.shared_state["env_echo"].strip() == "from-env"


@pytest.mark.asyncio
@pytest.mark.usefixtures("shell_commands")
async def test_unresolved_placeholder_never_reads_host_environment(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    """A ``$NAME`` absent from shared_state and params stays the text ``NAME``."""
    monkeypatch.setenv("HOST_ONLY_VALUE", "from-host")
    commands = config_dir / "commands"
    (commands / "show.md").write_text(
        "---\nbrain: none\n---\nparam=${value}\n", encoding="utf-8"
    )
    (commands / "echo_arg.sh").write_text(
        '#!/usr/bin/env bash\necho "arg=$1"\n', encoding="utf-8"
    )
    (commands / "leak.yml").write_text(
        "commands:\n"
        "  - name: show\n"
        "    params:\n"
        "      value: $HOST_ONLY_VALUE\n"
        "  - name: echo_arg\n"
        "    args:\n"
        "      - ${HOST_ONLY_VALUE}\n",
        encoding="utf-8",
    )

    ctx = await _run_main(config_dir, "leak")

    assert ctx.shared_state["show"] == "param=HOST_ONLY_VALUE"
    assert ctx.shared_state["echo_arg"].strip() == "arg=HOST_ONLY_VALUE"


@pytest.mark.asyncio
@pytest.mark.usefixtures("shell_commands")
async def test_shell_nonzero_exit_includes_stderr(config_dir: Path):
    commands = config_dir / "commands"
    (commands / "fail.sh").write_text(
        "#!/usr/bin/env bash\necho 'oops on stderr' >&2\nexit 3\n", encoding="utf-8"
    )

    ctx = _make_context()
    runner = machinery(ctx, "fail", [], config_dir)

    with pytest.raises(CommandError) as excinfo:
        await runner.run()

    message = str(excinfo.value)
    assert "exit code 3" in message
    assert "oops on stderr" in message


@pytest.mark.asyncio
@pytest.mark.usefixtures("shell_commands")
@pytest.mark.parametrize(
    ("source", "said"),
    [
        # Every file mounted from a Windows host is executable.
        ("echo by-bash\n", "by-bash"),
        ('#!/usr/bin/env python3\nprint("by-its-shebang")\n', "by-its-shebang"),
    ],
    ids=["no-shebang", "shebang"],
)
async def test_executable_script_runs_itself_only_with_a_shebang(
    config_dir: Path, source: str, said: str
):
    script = config_dir / "commands" / "exec.sh"
    script.write_text(source, encoding="utf-8")
    script.chmod(0o755)

    ctx = await _run_main(config_dir, "exec")

    assert ctx.pipe.strip() == said


@pytest.mark.asyncio
@pytest.mark.usefixtures("shell_commands")
async def test_shell_ends_with_the_script_not_what_it_left_running(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    """A script that leaves a process running, holding its output open, ends
    the command when it ends itself, with what it said before."""
    monkeypatch.setattr(child_process, "_PIPES_OUTLIVE_SECONDS", 0.05)
    (config_dir / "commands" / "leave.sh").write_text(
        '#!/usr/bin/env bash\nsleep 60 &\necho "left $!"\n', encoding="utf-8"
    )

    ctx = _make_context()
    await asyncio.wait_for(machinery(ctx, "leave", [], config_dir).run(), 10)

    said, left = ctx.pipe.split()
    os.kill(int(left), signal.SIGTERM)
    assert said == "left"


# --- inline print / to_html / to_pdf inside a chain ------------------------


@pytest.mark.asyncio
async def test_inline_print_renders_with_jinja2_in_chain(config_dir: Path):
    """An inline print step renders shared_state via jinja2 and updates pipe."""
    commands = config_dir / "commands"
    (commands / "seed.py").write_text(
        "def main():\n    return 'world'\n", encoding="utf-8"
    )
    (commands / "printer.yml").write_text(
        "commands:\n  - seed\n  - name: greeting\n    print: 'Hello {{ seed }}!'\n",
        encoding="utf-8",
    )

    ctx = await _run_main(config_dir, "printer")

    assert ctx.shared_state["seed"] == "world"
    assert ctx.shared_state["greeting"] == "Hello world!"
    # The inline print is the final child, so it owns the pipe.
    assert ctx.pipe == "Hello world!"


@pytest.mark.asyncio
async def test_inline_to_html_in_chain(config_dir: Path):
    commands = config_dir / "commands"
    (commands / "body.py").write_text(
        "def main():\n    return '# Heading'\n", encoding="utf-8"
    )
    (commands / "html_parent.yml").write_text(
        "commands:\n  - body\n  - name: rendered\n    to_html: {}\n",
        encoding="utf-8",
    )

    ctx = await _run_main(config_dir, "html_parent")

    rendered = ctx.shared_state["rendered"]
    assert "<h1>Heading</h1>" in rendered
    assert ctx.pipe == rendered


@pytest.mark.asyncio
async def test_inline_to_pdf_in_chain(config_dir: Path, monkeypatch):
    """to_pdf within a chain produces PDF bytes; weasyprint is stubbed."""
    fake_weasyprint = _install_fake_weasyprint(monkeypatch)

    commands = config_dir / "commands"
    (commands / "doc.py").write_text(
        "def main():\n    return '# Title'\n", encoding="utf-8"
    )
    (commands / "pdf_parent.yml").write_text(
        "commands:\n  - doc\n  - name: pdf\n    to_pdf: {}\n",
        encoding="utf-8",
    )

    ctx = await _run_main(config_dir, "pdf_parent")

    # result is the raw PDF bytes; pipe (text_output) is the base64 encoding.
    assert ctx.shared_state["pdf"] == b"%PDF-FAKE"
    assert ctx.pipe == base64.b64encode(b"%PDF-FAKE").decode("ascii")
    # The HTML passed to weasyprint contained the rendered markdown heading.
    assert "<h1>Title</h1>" in fake_weasyprint["last_html"]


def _install_fake_weasyprint(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Install a fake weasyprint module so to_pdf needs no native libs."""
    import types

    captured: dict[str, Any] = {}

    class _FakeHTML:
        def __init__(self, string: str = "", base_url: str | None = None) -> None:
            captured["last_html"] = string

        def write_pdf(self) -> bytes:
            return b"%PDF-FAKE"

    module = types.ModuleType("weasyprint")
    module.HTML = _FakeHTML  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "weasyprint", module)
    return captured


# --- person-specific command vs common command fallback --------------------


@pytest.mark.asyncio
async def test_person_specific_command_overrides_common(config_dir: Path):
    """A member-scoped command takes precedence over the common command."""
    commands = config_dir / "commands"
    member_commands = config_dir / "team" / "members" / "alice" / "commands"
    member_commands.mkdir(parents=True)

    (commands / "greet.py").write_text(
        "def main():\n    return 'common-greeting'\n", encoding="utf-8"
    )
    (member_commands / "greet.py").write_text(
        "def main():\n    return 'alice-greeting'\n", encoding="utf-8"
    )

    ctx = await _run_main(config_dir, "greet")

    assert ctx.shared_state["greet"] == "alice-greeting"


@pytest.mark.asyncio
async def test_falls_back_to_common_command_when_no_person_specific(config_dir: Path):
    """Without a member-scoped file, the common command resolves."""
    commands = config_dir / "commands"
    (commands / "greet.py").write_text(
        "def main():\n    return 'common-greeting'\n", encoding="utf-8"
    )

    ctx = await _run_main(config_dir, "greet")

    assert ctx.shared_state["greet"] == "common-greeting"


# --- run_command person resolution -----------------------------------------


@pytest.fixture(autouse=True)
def _in_process(commands_in_process) -> None:
    """The commands the host starts run in this process."""


@pytest.mark.asyncio
async def test_run_command_requires_person_without_candidates(config_dir: Path):
    """Without a member that can execute commands, selection is required."""
    commands = config_dir / "commands"
    (commands / "noop.py").write_text("def main():\n    return 'x'\n", encoding="utf-8")
    team = _make_team(
        [
            Person(person_id="alice", name="Alice Anderson", is_active=False),
            Person(person_id="bob", name="bob", is_active=False),
        ]
    )
    ctx = _make_context(team=team)

    with pytest.raises(PersonSelectionRequiredError) as excinfo:
        await run_command(ctx, "noop", [], person_identifier=None, cwd=config_dir)

    # Sorted labels: a distinct display name gets a "id (name)" label, while a
    # name matching the id (case-insensitively) is shown as the bare id.
    assert excinfo.value.available == ["alice (Alice Anderson)", "bob"]


@pytest.mark.asyncio
async def test_run_command_unknown_person_raises(config_dir: Path):
    commands = config_dir / "commands"
    (commands / "noop.py").write_text("def main():\n    return 'x'\n", encoding="utf-8")
    ctx = _make_context()

    with pytest.raises(PersonNotFoundError) as excinfo:
        await run_command(ctx, "noop", [], person_identifier="ghost", cwd=config_dir)

    assert excinfo.value.identifier == "ghost"


@pytest.mark.asyncio
async def test_run_command_selects_single_active_member(config_dir: Path):
    commands = config_dir / "commands"
    (commands / "whoami.py").write_text(
        "def main(context):\n    return context.person.person_id\n", encoding="utf-8"
    )
    ctx = _make_context()

    outcome = await run_command(
        ctx, "whoami", [], person_identifier=None, cwd=config_dir
    )

    assert outcome.text_output == "alice"


# --- declared access ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_read_only_command_runs_while_its_member_is_busy(config_dir: Path):
    """Only a command that declares itself read-only goes without the lease."""
    commands = config_dir / "commands"
    (commands / "look.py").write_text(
        'COMMAND_METADATA = {"read_only": True}\n\n'
        "def main():\n    return 'looked'\n",
        encoding="utf-8",
    )
    (commands / "change.py").write_text(
        "def main():\n    return 'changed'\n", encoding="utf-8"
    )
    # Another run holds the member's lease, in a context of its own.
    other_run = contextvars.Context()
    busy = PersonExecutionLease("alice")
    other_run.run(
        busy.acquire, source="routine", command="workflows/ticket", work_id="other"
    )
    try:
        outcome = await run_command(
            _make_context(), "look", [], person_identifier=None, cwd=config_dir
        )
        with pytest.raises(CommandError):
            await run_command(
                _make_context(), "change", [], person_identifier=None, cwd=config_dir
            )
    finally:
        other_run.run(busy.release)

    assert outcome.text_output == "looked"


@pytest.mark.asyncio
async def test_an_invalid_access_declaration_refuses_the_command(config_dir: Path):
    (config_dir / "commands" / "odd.md").write_text(
        "---\nbrain: none\ninspects: [secrets]\n---\nhello\n", encoding="utf-8"
    )

    with pytest.raises(CommandError, match="inspects"):
        await run_command(
            _make_context(), "odd", [], person_identifier=None, cwd=config_dir
        )
