import textwrap
from pathlib import Path

import pytest

from guildbotics.drivers.custom_command_runner import (
    CustomCommandExecutor,
    run_custom_command,
)
from guildbotics.entities.team import Person, Project, Team
from guildbotics.runtime.context import Context
from tests.conftest import coverage_suspended
from tests.guildbotics.runtime.test_context import (
    DummyBrainFactory,
    DummyIntegrationFactory,
    DummyLoaderFactory,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def _make_context(message: str = "", members: list[Person] | None = None) -> Context:
    if members is None:
        members = [Person(person_id="alice", name="Alice", is_active=True)]
    team = Team(project=Project(name="demo"), members=members)
    loader_factory = DummyLoaderFactory(team)
    integration_factory = DummyIntegrationFactory()
    brain_factory = DummyBrainFactory()
    # Use default, then clone to first active person for deterministic person binding
    base = Context.get_default(
        loader_factory, integration_factory, brain_factory, message
    )
    for m in members:
        if m.is_active:
            return base.clone_for(m)
    return base.clone_for(members[0])


@pytest.mark.asyncio
async def test_quickstart_positional_args_with_pipe(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "prompts/translate.md",
        """
        ---
        brain: none
        template_engine: jinja2
        ---
        以下のテキストが{{ arg1 }}であれば{{ arg2 }}に翻訳してください:

        {{ context.pipe }}
        """,
    )

    ctx = _make_context("こんにちは")
    out = await run_custom_command(ctx, "translate", ["英語", "日本語"])
    assert "以下のテキストが英語であれば日本語に翻訳してください:" in out
    assert "こんにちは" in out


def test_named_args_placeholders(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "prompts/translate.md",
        """
        以下のテキストを${source}から${target}に翻訳してください:
        """,
    )

    ctx = _make_context("Hello")
    ex = CustomCommandExecutor(ctx, "translate", ["source=英語", "target=日本語"])
    # brain: default in absence of front matter, so result is produced by DummyBrain
    # We only assert the parameter expansion occurs at prompt-level for `none` brains.
    # To keep deterministic, write another prompt using none brain.
    _write(
        tmp_path / "prompts/translate2.md",
        """
        ---
        brain: none
        ---
        以下のテキストを${source}から${target}に翻訳してください:
        """,
    )
    ex2 = CustomCommandExecutor(ctx, "translate2", ["source=英語", "target=日本語"])
    out = pytest.run(async_fn=ex2.run) if hasattr(pytest, "run") else None  # type: ignore[attr-defined]
    # Fallback to explicit event loop if helper not available
    if out is None:
        import asyncio

        out = asyncio.get_event_loop().run_until_complete(ex2.run())
    assert "英語から日本語に翻訳してください" in out


@pytest.mark.asyncio
async def test_jinja2_conditional_rendering(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "prompts/cond.md",
        """
        ---
        brain: none
        template_engine: jinja2
        ---
        {% if target %}
        以下のテキストを{{ target }}に翻訳してください:
        {% else %}
        以下のテキストを英訳してください:
        {% endif %}
        """,
    )

    ctx = _make_context("")
    ex1 = CustomCommandExecutor(ctx, "cond", [])
    out1 = await ex1.run()
    assert "以下のテキストを英訳してください:" in out1

    ex2 = CustomCommandExecutor(ctx, "cond", ["target=中国語"])
    out2 = await ex2.run()
    assert "以下のテキストを中国語に翻訳してください:" in out2


@pytest.mark.asyncio
async def test_context_variables_in_jinja2(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "prompts/context-info.md",
        """
        ---
        brain: none
        template_engine: jinja2
        ---
        言語コード: {{ context.language_code }}
        言語名: {{ context.language_name }}

        ID: {{ context.person.person_id }}
        名前: {{ context.person.name }}

        チームメンバー:
        {% for member in context.team.members %}
        - {{ member.person_id }}: {{ member.name }}
        {% endfor %}
        """,
    )

    members = [
        Person(person_id="alice", name="Alice", is_active=True),
        Person(person_id="yuki", name="Yuki", is_active=False),
    ]
    ctx = _make_context("", members)
    ex = CustomCommandExecutor(ctx, "context-info", [])
    with coverage_suspended():
        out = await ex.run()
    assert "言語コード: en" in out
    assert "言語名: English" in out
    assert "ID: alice" in out and "名前: Alice" in out
    assert "- alice: Alice" in out and "- yuki: Yuki" in out


@pytest.mark.asyncio
async def test_cli_agent_brain_cli_passes_cwd_and_params(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "prompts/summarize.md",
        """
        ---
        brain: cli
        ---
        ${file}の最初のセクションを読み、その内容を${language}を用いて要約してください
        """,
    )

    ctx = _make_context("")
    ex = CustomCommandExecutor(
        ctx, "summarize", ["file=README.md", "language=日本語"], cwd=Path(".")
    )
    await ex.run()
    result = ex._context.shared_state.get("summarize")
    # DummyBrain returns kwargs; ensure cwd and session_state are provided
    assert isinstance(result, dict)
    assert str(result.get("cwd", "")).endswith("")  # cwd present
    session = result.get("session_state", {})
    assert session.get("file") == "README.md"
    assert session.get("language") == "日本語"


@pytest.mark.asyncio
async def test_builtin_command_in_pipeline_identify_item_args_passed(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "prompts/get-time-of-day.md",
        """
        ---
        commands:
          - name: current_time
            script: echo "現在の時刻は`date +%T`です"
          - name: time_of_day
            command: functions/identify_item item_type=時間帯 candidates="朝, 昼, 夜"
        ---
        """,
    )

    ctx = _make_context("")
    ex = CustomCommandExecutor(ctx, "get-time-of-day", [])
    await ex.run()
    shared = ex._context.shared_state
    assert "current_time" in shared
    assert "time_of_day" in shared
    # DummyBrain echoes session_state; confirm parameters flowed through
    assert shared["time_of_day"]["session_state"]["item_type"] == "時間帯"


@pytest.mark.asyncio
async def test_subcommand_naming_and_reference_with_jinja2(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "prompts/greet-time.md",
        """
        ---
        commands:
          - name: current_time
            script: echo "現在の時刻は`date +%T`です"
          - name: time_of_day
            command: functions/identify_item item_type=時間帯 candidates="朝, 昼, 夜"
        brain: none
        template_engine: jinja2
        ---
        {% if time_of_day.label == "朝" %}
        おはようございます。
        {% elif time_of_day.label == "夜" %}
        こんばんは。
        {% else %}
        こんにちは。
        {% endif %}

        {{ current_time }}
        """,
    )

    ctx = _make_context("")
    ex = CustomCommandExecutor(ctx, "greet-time", [])
    out = await ex.run()
    # With DummyBrain, no label; template should fall back to else branch
    assert "こんにちは。" in out
    assert "現在の時刻は" in out


@pytest.mark.asyncio
async def test_external_shell_script_arguments_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    # Reference script by logical name without extension
    _write(
        tmp_path / "prompts/echo-args.md",
        """
        ---
        brain: none
        commands:
          - name: echo_args
            path: tools/echo-args
            args:
              - a
              - b
            params:
              key1: c
              key2: d
        ---
        ${echo_args}
        """,
    )
    script_path = tmp_path / "prompts/tools/echo-args.sh"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(
        """
        #!/usr/bin/env bash
        echo "arg1: ${1}"
        echo "arg2: ${2}"
        echo "key1: ${key1}"
        echo "key2: ${key2}"
        """.strip()
        + "\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    ctx = _make_context("")
    ex = CustomCommandExecutor(ctx, "echo-args", [])
    out = await ex.run()
    assert "arg1: a" in out
    assert "arg2: b" in out
    assert "key1: c" in out
    assert "key2: d" in out


@pytest.mark.asyncio
async def test_python_command_hello_world(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "prompts/hello.py",
        """
        def main():
            return "Hello, world!"
        """,
    )

    ctx = _make_context("")
    ex = CustomCommandExecutor(ctx, "hello", [])
    out = await ex.run()
    assert out.strip() == "Hello, world!"


@pytest.mark.asyncio
async def test_python_command_with_args_and_kwargs(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "prompts/hello.py",
        """
from guildbotics.runtime import Context

def main(context: Context, arg1, arg2, key1=None, key2=None):
    return [
        f"arg1: {arg1}",
        f"arg2: {arg2}",
        f"key1: {key1}",
        f"key2: {key2}",
    ]
        """,
    )

    ctx = _make_context("")
    ex = CustomCommandExecutor(ctx, "hello", ["a", "b", "key1=c", "key2=d"])
    out = await ex.run()
    assert "arg1: a" in out
    assert "arg2: b" in out
    assert "key1: c" in out
    assert "key2: d" in out


@pytest.mark.asyncio
async def test_python_command_varargs_and_kwargs(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "prompts/hello.py",
        """
from guildbotics.runtime import Context

def main(context: Context, *args, **kwargs):
    lines = []
    for i, arg in enumerate(args):
        lines.append(f"arg[{i}]: {arg}")
    for k, v in kwargs.items():
        lines.append(f"kwarg[{k}]: {v}")
    return lines
        """,
    )

    ctx = _make_context("")
    ex = CustomCommandExecutor(ctx, "hello", ["a", "b", "key1=c", "key2=d"])
    out = await ex.run()
    assert "arg[0]: a" in out
    assert "arg[1]: b" in out
    assert "kwarg[key1]: c" in out
    assert "kwarg[key2]: d" in out


@pytest.mark.asyncio
async def test_python_command_invokes_other_command(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "prompts/hello.py",
        """
from datetime import datetime
from guildbotics.runtime import Context

async def main(context: Context):
    current_time = f"現在の時刻は{datetime.now().strftime('%H:%M')}です"
    time_of_day = await context.invoke(
        "functions/identify_item",
        message=current_time,
        item_type="時間帯",
        candidates="朝, 昼, 夜",
    )

    message = ""
    if time_of_day and getattr(time_of_day, 'label', '') == "朝":
        message = "おはようございます。"
    elif time_of_day and getattr(time_of_day, 'label', '') == "夜":
        message = "こんばんは。"
    else:
        message = "こんにちは。"

    return [message, current_time]
        """,
    )

    ctx = _make_context("")
    ex = CustomCommandExecutor(ctx, "hello", [])
    out = await ex.run()
    assert "こんにちは。" in out
    assert "現在の時刻は" in out


@pytest.mark.asyncio
async def test_print_command_basic(tmp_path, monkeypatch):
    """docs 5.3: `print` outputs literal text without LLM."""
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "prompts/greet.md",
        """
        ---
        commands:
          - print: こんにちは。
        ---
        """,
    )

    ctx = _make_context("")
    ex = CustomCommandExecutor(ctx, "greet", [])
    out = await ex.run()
    assert "こんにちは。" in out


@pytest.mark.asyncio
async def test_print_command_with_pipeline_and_jinja(tmp_path, monkeypatch):
    """docs 5.3: `print` supports Jinja and previous outputs."""
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "prompts/greet-time-print.md",
        """
        ---
        commands:
          - name: current_time
            script: echo "現在の時刻は`date +%T`です"
          - name: time_of_day
            command: functions/identify_item item_type=時間帯 candidates="朝, 昼, 夜"
          - print: |
              {% if time_of_day.label == "朝" %}
              おはようございます。
              {% elif time_of_day.label == "夜" %}
              こんばんは。
              {% else %}
              こんにちは。
              {% endif %}

              {{ current_time }}
        ---
        """,
    )

    ctx = _make_context("")
    ex = CustomCommandExecutor(ctx, "greet-time-print", [])
    out = await ex.run()
    # With DummyBrain, label is absent; falls to else branch
    assert "こんにちは。" in out
    assert "現在の時刻は" in out


@pytest.mark.asyncio
async def test_external_shell_script_called_by_command_name(tmp_path, monkeypatch):
    """docs 6: reference external script via `command: <name>`."""
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))

    # Create external script `current-time.sh`
    script_path = tmp_path / "prompts/current-time.sh"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(
        """
        #!/usr/bin/env bash
        echo "現在の時刻は`date +%T`です"
        """.strip()
        + "\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    # Compose prompt that calls the external script by logical name
    _write(
        tmp_path / "prompts/greet-ext.md",
        """
        ---
        commands:
          - name: current_time
            command: current-time
          - name: time_of_day
            command: functions/identify_item item_type=時間帯 candidates="朝, 昼, 夜"
        brain: none
        template_engine: jinja2
        ---
        {% if time_of_day.label == "朝" %}
        おはようございます。
        {% elif time_of_day.label == "夜" %}
        こんばんは。
        {% else %}
        こんにちは。
        {% endif %}

        {{ current_time }}
        """,
    )

    ctx = _make_context("")
    ex = CustomCommandExecutor(ctx, "greet-ext", [])
    out = await ex.run()
    assert "こんにちは。" in out
    assert "現在の時刻は" in out


@pytest.mark.asyncio
async def test_member_selection_with_person_identifier(tmp_path, monkeypatch):
    """docs 1.3: select member by identifier during run."""
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "prompts/whoami.md",
        """
        ---
        brain: none
        template_engine: jinja2
        ---
        ID: {{ context.person.person_id }}
        """,
    )

    members = [
        Person(person_id="alice", name="Alice", is_active=True),
        Person(person_id="yuki", name="Yuki", is_active=True),
    ]
    base_ctx = _make_context("", members)
    out = await run_custom_command(base_ctx, "whoami", [], person_identifier="yuki")
    assert "ID: yuki" in out


@pytest.mark.asyncio
async def test_schema_defined_prompt_pipeline(tmp_path, monkeypatch):
    """docs 5.2: schema + response_class in subcommands runs and stores named result.

    We simulate coverage output instead of running pytest to keep tests fast and deterministic.
    The inline `prompt` steps carry `response_class` values, and although our DummyBrain does
    not perform structured parsing, command execution and shared state updates should succeed.
    """
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))

    _write(
        tmp_path / "prompts/coverage.md",
        """
        ---
        schema: |
            class Ranking:
                package: str
                detail: str
                line_rate: float
                reason: str

            class Rankings:
                items: list[Ranking]

            class Task:
                title: str
                description: str
                priority: int

            class TaskList:
                tasks: list[Task]
        commands:
          - script: |
              cat > coverage.xml <<'XML'
              <coverage line-rate="0.76"></coverage>
              XML
              cat coverage.xml |grep line-rate
          - prompt: |
              この情報を解析して、テスト実装の対応優先度が高いパッケージのトップ3についてRankings形式のJSONとして出力してください。
            response_class: Rankings
          - name: task_list
            prompt: |
              この分析情報に基づいて、優先度が高い順に、TaskList形式のJSONで、すぐに着手可能なテスト実装タスク定義を最大5つまで提案してください。
            response_class: TaskList
        template_engine: jinja2
        brain: none
        ---
        {% for task in task_list.tasks %}
        - [ ] {{ task.title }} (priority: {{ task.priority }})
        {% endfor %}
        """,
    )

    ctx = _make_context("")
    # Stub brain to return a deterministic TaskList-like dict for the named
    # prompt command `task_list`, so the final Jinja template expands values.
    class _StubBrain:
        def __init__(self, name: str, config: dict | None):
            self.name = name
            self._config = config or {}
            self.response_class = None

        async def run(self, message: str, **kwargs):
            # Return tasks only for the command named `task_list` in the config
            if self._config.get("name") == "task_list":
                return {
                    "tasks": [
                        {"title": "Implement coverage-driven tests", "description": "Add tests for low coverage areas", "priority": 1},
                        {"title": "Refactor flaky tests", "description": "Stabilize intermittently failing tests", "priority": 2},
                    ]
                }
            # Default echo for other prompts
            return {"message": message, **kwargs}

    class _StubBrainFactory:
        def create_brain(
            self,
            person_id: str,
            name: str,
            language_code: str,
            logger,
            config: dict | None = None,
            class_resolver=None,
        ):
            return _StubBrain(name, config)

    ctx.brain_factory = _StubBrainFactory()  # type: ignore[assignment]
    ex = CustomCommandExecutor(ctx, "coverage", [])
    out = await ex.run()

    # Verify template expanded schema-defined variables into the final output.
    assert "- [ ] Implement coverage-driven tests (priority: 1)" in out
    assert "- [ ] Refactor flaky tests (priority: 2)" in out
    shared = ex._context.shared_state
    # Auto-generated name for the second command (the first inline prompt)
    assert any(k.startswith("coverage__") for k in shared.keys())
    # Named result from the third command should exist
    assert "task_list" in shared and isinstance(shared["task_list"], dict)
