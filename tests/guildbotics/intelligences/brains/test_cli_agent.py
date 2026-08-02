import json
import os

import pytest

from guildbotics.intelligences.brains import cli_agent


class StubProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"done",
        stderr: bytes = b"",
        returncode: int = 0,
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self.stdout, self.stderr


@pytest.mark.parametrize(
    ("mapping_value", "adapter"),
    [
        ("cli_agents/codex/default.yml", "codex"),
        ("cli_agents/codex/reviewer.yml", "codex"),
        ("cli_agents/claude/default.yml", "claude"),
    ],
)
def test_cli_agent_mapping_selects_native_adapter_without_script_file(
    monkeypatch, tmp_path, mapping_value, adapter
) -> None:
    cli_agent.person_cli_agent_mapping.clear()
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"default": mapping_value},
    )

    resolved = cli_agent.get_cli_agent_mapping("aiko")

    assert resolved["default"].adapter == adapter
    assert resolved["default"].script == ""
    cli_agent.person_cli_agent_mapping.clear()


@pytest.mark.asyncio
async def test_cli_agent_run_passes_cwd_without_mutating_mapping(monkeypatch, tmp_path):
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(script="echo test", env={"A": "1"})
    }

    captured = {}

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        captured["script"] = script
        captured["cwd"] = cwd
        captured["env"] = env
        return StubProcess()

    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )

    try:
        brain = cli_agent.CliAgentBrain(
            "p1",
            "x",
            logger=type(
                "L",
                (),
                {
                    "debug": lambda *a, **k: None,
                    "info": lambda *a, **k: None,
                    "error": lambda *a, **k: None,
                },
            )(),
        )
        output = await brain.run("hello", cwd=tmp_path)
        assert output == "done"
        assert captured["cwd"] == str(tmp_path)
        assert "PROMPT_FILE" in captured["env"]
        assert not hasattr(brain.executable_info, "cwd")
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)


@pytest.mark.asyncio
async def test_cli_agent_run_inherits_environment_and_overlays_config(
    monkeypatch, tmp_path
):
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(
            script="echo test",
            env={
                "CONFIG_ONLY": "configured",
                "GUILDBOTICS_PARENT_ENV": "overridden",
            },
        )
    }

    captured = {}

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        captured["env"] = env
        return StubProcess()

    monkeypatch.setenv("GUILDBOTICS_PARENT_ENV", "parent")
    monkeypatch.setenv("GUILDBOTICS_PARENT_ONLY", "inherited")
    monkeypatch.setenv("GH_TOKEN", "host-gh-token")
    monkeypatch.setenv("GITHUB_TOKEN", "host-github-token")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")
    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )

    try:
        brain = cli_agent.CliAgentBrain(
            "p1",
            "x",
            logger=type(
                "L",
                (),
                {
                    "debug": lambda *a, **k: None,
                    "info": lambda *a, **k: None,
                    "error": lambda *a, **k: None,
                },
            )(),
        )
        await brain.run("hello", cwd=tmp_path)
        assert captured["env"]["GUILDBOTICS_PARENT_ONLY"] == "inherited"
        assert captured["env"]["GUILDBOTICS_PARENT_ENV"] == "overridden"
        assert captured["env"]["CONFIG_ONLY"] == "configured"
        assert "PROMPT_FILE" in captured["env"]
        assert "GH_TOKEN" not in captured["env"]
        assert "GITHUB_TOKEN" not in captured["env"]
        assert "SSH_AUTH_SOCK" not in captured["env"]
        assert captured["env"]["GH_CONFIG_DIR"]
        assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert captured["env"]["GIT_CONFIG_GLOBAL"]
        assert "IdentityFile=/dev/null" in captured["env"]["GIT_SSH_COMMAND"]
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)


@pytest.mark.asyncio
async def test_cli_agent_run_applies_execution_context_env(monkeypatch, tmp_path):
    # The agent execution context is scoped to this single subprocess and
    # survives credential isolation, without touching the process environment.
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(script="echo test", env={})
    }

    captured = {}

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        captured["env"] = env
        return StubProcess()

    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )

    try:
        brain = cli_agent.CliAgentBrain(
            "p1",
            "x",
            logger=type(
                "L",
                (),
                {
                    "debug": lambda *a, **k: None,
                    "info": lambda *a, **k: None,
                    "error": lambda *a, **k: None,
                },
            )(),
        )
        await brain.run(
            "hello",
            cwd=tmp_path,
            session_state={"agent_execution_context": {"run_id": "run-123"}},
        )
        assert captured["env"]["GUILDBOTICS_TASK_RUN_ID"] == "run-123"
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)
    assert "GUILDBOTICS_TASK_RUN_ID" not in os.environ


@pytest.mark.asyncio
async def test_fresh_one_shot_chat_receives_full_context_each_invocation(
    monkeypatch, tmp_path
) -> None:
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(script="echo test", env={})
    }
    prompts: list[str] = []

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        prompts.append(cli_agent.Path(env["PROMPT_FILE"]).read_text(encoding="utf-8"))
        return StubProcess()

    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )
    state = {
        "agent_execution_context": {
            "run_id": "run-1",
            "workspace_data_root": str(tmp_path),
            "work_kind": "chat",
            "work_identity": "slack:bot:C1:100.1",
            "context_cursor": "101.1",
            "attempt": 2,
            "rebuild_context_complete": True,
            "rebuild_context": json.dumps(
                [
                    {"timestamp": "100.1", "content": "older"},
                    {"timestamp": "101.1", "content": "latest"},
                ]
            ),
            "continuation_input": "continue-only",
        }
    }
    try:
        brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())
        await brain.run("latest-turn", cwd=tmp_path, session_state=state)
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

    assert 'mode="full"' in prompts[0]
    assert "older" in prompts[0]
    assert 'latest"' not in prompts[0]
    assert "latest-turn" in prompts[0]


@pytest.mark.asyncio
async def test_dispatch_scoped_one_shot_retry_uses_exact_session_continuation(
    monkeypatch, tmp_path
) -> None:
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(
            script="echo test", env={}, conversation_scope="dispatch"
        )
    }
    prompts: list[str] = []

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        prompts.append(cli_agent.Path(env["PROMPT_FILE"]).read_text(encoding="utf-8"))
        return StubProcess()

    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )
    conversation_file = tmp_path / "task-runs" / "run-1.agy-conversation"
    conversation_file.parent.mkdir(parents=True)
    conversation_file.write_text("exact-session-id", encoding="utf-8")
    state = {
        "agent_execution_context": {
            "run_id": "run-1",
            "workspace_data_root": str(tmp_path),
            "work_kind": "chat",
            "work_identity": "slack:bot:C1:100.1",
            "context_cursor": "101.1",
            "attempt": 2,
            "rebuild_context_complete": True,
            "rebuild_context": json.dumps([{"timestamp": "100.1", "content": "older"}]),
            "continuation_input": "continue-only",
        }
    }
    try:
        brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())
        await brain.run("duplicate-latest", cwd=tmp_path, session_state=state)
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

    assert 'mode="continuation"' in prompts[0]
    assert "continue-only" in prompts[0]
    assert "older" not in prompts[0]
    assert "duplicate-latest" not in prompts[0]


def _test_logger():
    return type(
        "L",
        (),
        {
            "debug": lambda *args, **kwargs: None,
            "info": lambda *args, **kwargs: None,
            "error": lambda *args, **kwargs: None,
        },
    )()


@pytest.mark.asyncio
async def test_one_shot_agent_inherits_verified_member_delegation(
    monkeypatch, tmp_path
) -> None:
    from guildbotics.runtime.person_lease import (
        DELEGATION_ID_ENV,
        LEASE_ID_ENV,
        LEASE_RUN_ENV,
        PersonExecutionLease,
    )

    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(script="echo test", env={})
    }
    captured = {}

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        captured["env"] = env
        return StubProcess()

    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )
    lease = PersonExecutionLease("p1", tmp_path)
    lease.acquire(source="routine", command="ticket", work_id="work-1")
    try:
        brain = cli_agent.CliAgentBrain(
            "p1",
            "x",
            logger=type(
                "L",
                (),
                {
                    "debug": lambda *a, **k: None,
                    "info": lambda *a, **k: None,
                    "error": lambda *a, **k: None,
                },
            )(),
        )
        await brain.run(
            "hello",
            cwd=tmp_path,
            session_state={"agent_execution_context": {"run_id": "run-123"}},
        )
    finally:
        lease.release()
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

    assert captured["env"][LEASE_RUN_ENV] == "run-123"
    assert captured["env"][LEASE_ID_ENV]
    assert captured["env"][DELEGATION_ID_ENV]


@pytest.mark.asyncio
async def test_cli_agent_run_propagates_cwd_workspace_environment(
    monkeypatch, tmp_path
):
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(script="echo test", env={})
    }
    project = tmp_path / "project"
    member_workspace = tmp_path / "member-workspace"
    config_dir = project / ".guildbotics" / "config"
    config_dir.mkdir(parents=True)
    env_file = project / ".env"
    env_file.write_text("DEMO=1\n", encoding="utf-8")
    member_workspace.mkdir()

    captured = {}

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        captured["cwd"] = cwd
        captured["env"] = env
        return StubProcess()

    monkeypatch.chdir(project)
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    monkeypatch.delenv("GUILDBOTICS_ENV_FILE", raising=False)
    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )

    try:
        brain = cli_agent.CliAgentBrain(
            "p1",
            "x",
            logger=type(
                "L",
                (),
                {
                    "debug": lambda *a, **k: None,
                    "info": lambda *a, **k: None,
                    "error": lambda *a, **k: None,
                },
            )(),
        )
        await brain.run("hello", cwd=member_workspace)
        assert captured["cwd"] == str(member_workspace)
        assert captured["env"]["GUILDBOTICS_CONFIG_DIR"] == str(config_dir.resolve())
        assert captured["env"]["GUILDBOTICS_ENV_FILE"] == str(env_file.resolve())
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)
    assert "GUILDBOTICS_CONFIG_DIR" not in os.environ
    assert "GUILDBOTICS_ENV_FILE" not in os.environ


@pytest.mark.asyncio
async def test_cli_agent_execution_details_include_stderr_and_returncode(
    monkeypatch, tmp_path
):
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(script="echo test", env={})
    }

    returncode = 2

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        return StubProcess(stdout=b"", stderr=b"login required", returncode=returncode)

    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )

    try:
        brain = cli_agent.CliAgentBrain(
            "p1",
            "x",
            logger=type(
                "L",
                (),
                {
                    "debug": lambda *a, **k: None,
                    "info": lambda *a, **k: None,
                    "error": lambda *a, **k: None,
                },
            )(),
        )
        result = await brain.run_with_execution_details("hello", cwd=tmp_path)
        assert result.stdout == ""
        assert result.stderr == "login required"
        assert result.returncode == returncode
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)


@pytest.mark.asyncio
async def test_cli_agent_run_raises_when_script_fails(monkeypatch, tmp_path):
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(script="echo test", env={})
    }

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        return StubProcess(stdout=b"", stderr=b"bad option", returncode=2)

    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )

    try:
        brain = cli_agent.CliAgentBrain(
            "p1",
            "x",
            logger=type(
                "L",
                (),
                {
                    "debug": lambda *a, **k: None,
                    "info": lambda *a, **k: None,
                    "error": lambda *a, **k: None,
                },
            )(),
        )
        with pytest.raises(RuntimeError, match="bad option"):
            await brain.run("hello", cwd=tmp_path)
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)


@pytest.mark.asyncio
async def test_cli_agent_run_raises_rate_limit_error_from_marker(monkeypatch, tmp_path):
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(script="echo test", env={})
    }
    marker = (
        'GUILDBOTICS_CLI_AGENT_ERROR_JSON: {"category":"rate_limited",'
        '"retry_after_at":"2026-07-04T11:44:00+09:00",'
        '"retry_after_text":"11:44 AM"}'
    )

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        return StubProcess(stdout=b"", stderr=marker.encode(), returncode=75)

    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )

    try:
        brain = cli_agent.CliAgentBrain(
            "p1",
            "x",
            logger=type(
                "L",
                (),
                {
                    "debug": lambda *a, **k: None,
                    "info": lambda *a, **k: None,
                    "warning": lambda *a, **k: None,
                    "error": lambda *a, **k: None,
                },
            )(),
        )
        with pytest.raises(cli_agent.CliAgentExecutionError) as excinfo:
            await brain.run("hello", cwd=tmp_path)
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

    assert excinfo.value.category == "rate_limited"
    assert excinfo.value.details["retry_after_at"] == "2026-07-04T11:44:00+09:00"
    assert excinfo.value.details["retry_after_text"] == "11:44 AM"


@pytest.mark.asyncio
async def test_cli_agent_authentication_marker_records_credential_failure(
    monkeypatch, tmp_path
):
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(script="echo test", env={})
    }
    marker = 'GUILDBOTICS_CLI_AGENT_ERROR_JSON: {"category":"authentication"}'
    recorded = []

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        return StubProcess(stdout=b"", stderr=marker.encode(), returncode=77)

    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )
    monkeypatch.setattr(
        cli_agent,
        "record_correlated_event",
        lambda **kwargs: recorded.append(kwargs),
    )

    try:
        brain = cli_agent.CliAgentBrain(
            "p1",
            "x",
            logger=type(
                "L",
                (),
                {
                    "debug": lambda *a, **k: None,
                    "info": lambda *a, **k: None,
                    "warning": lambda *a, **k: None,
                    "error": lambda *a, **k: None,
                },
            )(),
        )
        with pytest.raises(cli_agent.CliAgentExecutionError) as excinfo:
            await brain.run("hello", cwd=tmp_path)
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

    assert excinfo.value.category == "authentication"
    assert recorded[0]["event_type"] == "credential.failed"
    assert recorded[0]["payload"] == {
        "provider": "cli_agent",
        "cli_agent": "default",
        "person_id": "p1",
        "code": "authentication",
    }
    assert recorded[0]["attributes"]["credential.provider"] == "cli_agent"
    assert recorded[0]["attributes"]["credential.cli_agent"] == "default"


def test_normalize_retry_after_handles_composite_relative_duration():
    retry_after_at = cli_agent.normalize_cli_agent_retry_after("Resets in 2h30m15s")

    assert retry_after_at


def test_normalize_retry_after_handles_date_text():
    retry_after_at = cli_agent.normalize_cli_agent_retry_after(
        "reset on July 8, 2026 at 11:44 AM",
        "Asia/Tokyo",
    )

    assert retry_after_at == "2026-07-08T11:44:00+09:00"


def test_normalize_native_retry_after_handles_provider_text():
    details = {
        "retry_after_text": "resets 12:50pm (Asia/Tokyo)",
        "retry_after_timezone": "Asia/Tokyo",
    }

    cli_agent._normalize_native_retry_after(details)

    assert details["retry_after_at"].endswith("T12:50:00+09:00")


@pytest.mark.asyncio
async def test_cli_agent_marker_normalizes_retry_after_text(monkeypatch, tmp_path):
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(script="echo test", env={})
    }
    marker = (
        'GUILDBOTICS_CLI_AGENT_ERROR_JSON: {"category":"rate_limited",'
        '"retry_after_text":"reset on July 8, 2026 at 11:44 AM",'
        '"retry_after_timezone":"Asia/Tokyo"}'
    )

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        return StubProcess(stdout=b"", stderr=marker.encode(), returncode=75)

    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )

    try:
        brain = cli_agent.CliAgentBrain(
            "p1",
            "x",
            logger=type(
                "L",
                (),
                {
                    "debug": lambda *a, **k: None,
                    "info": lambda *a, **k: None,
                    "warning": lambda *a, **k: None,
                    "error": lambda *a, **k: None,
                },
            )(),
        )
        with pytest.raises(cli_agent.CliAgentExecutionError) as excinfo:
            await brain.run("hello", cwd=tmp_path)
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

    assert excinfo.value.details["retry_after_at"] == "2026-07-08T11:44:00+09:00"


@pytest.mark.asyncio
async def test_cli_agent_broken_error_marker_remains_regular_failure(
    monkeypatch, tmp_path
):
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(script="echo test", env={})
    }

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        return StubProcess(
            stdout=b"",
            stderr=b"GUILDBOTICS_CLI_AGENT_ERROR_JSON: {bad",
            returncode=75,
        )

    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )

    try:
        brain = cli_agent.CliAgentBrain(
            "p1",
            "x",
            logger=type(
                "L",
                (),
                {
                    "debug": lambda *a, **k: None,
                    "info": lambda *a, **k: None,
                    "warning": lambda *a, **k: None,
                    "error": lambda *a, **k: None,
                },
            )(),
        )
        with pytest.raises(RuntimeError, match="exited with code 75"):
            await brain.run("hello", cwd=tmp_path)
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)


@pytest.mark.asyncio
async def test_cli_agent_run_raises_when_response_is_empty(monkeypatch, tmp_path):
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(script="echo test", env={})
    }

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        return StubProcess(stdout=b"", stderr=b"usage error", returncode=0)

    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )

    try:
        brain = cli_agent.CliAgentBrain(
            "p1",
            "x",
            logger=type(
                "L",
                (),
                {
                    "debug": lambda *a, **k: None,
                    "info": lambda *a, **k: None,
                    "error": lambda *a, **k: None,
                },
            )(),
        )
        with pytest.raises(RuntimeError, match="produced no response"):
            await brain.run("hello", cwd=tmp_path)
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)


@pytest.mark.asyncio
async def test_cli_agent_records_request_response_and_span(monkeypatch, tmp_path):
    original = cli_agent.person_cli_agent_mapping.copy()
    io_records: list[tuple[str, dict]] = []
    span_records: list[dict] = []
    multibyte_stderr = "あ" * 2731
    monkeypatch.delenv("GUILDBOTICS_TRANSCRIPT_DETAIL", raising=False)
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(script="echo test", env={})
    }

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        return StubProcess(
            stdout=b"done", stderr=multibyte_stderr.encode(), returncode=0
        )

    monkeypatch.setattr(
        cli_agent,
        "record_correlated_io",
        lambda *, io_type, payload: io_records.append((io_type, payload)),
    )
    monkeypatch.setattr(
        cli_agent,
        "record_span_summary",
        lambda **kwargs: span_records.append(kwargs),
    )
    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )

    try:
        brain = cli_agent.CliAgentBrain(
            "p1",
            "functions/handle_chat_event",
            logger=type(
                "L",
                (),
                {
                    "debug": lambda *a, **k: None,
                    "info": lambda *a, **k: None,
                    "error": lambda *a, **k: None,
                },
            )(),
            description="Reply as {{ context.person.name }}.",
            template_engine="jinja2",
        )
        await brain.run(
            "hello",
            cwd=tmp_path,
            session_state={
                "context": type(
                    "C", (), {"person": type("P", (), {"name": "Alice"})()}
                )()
            },
        )
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

    assert [record[0] for record in io_records] == [
        "cli_agent.request",
        "cli_agent.response",
    ]
    assert io_records[0][1]["person_id"] == "p1"
    assert io_records[0][1]["brain"] == "functions/handle_chat_event"
    assert "Reply as Alice." in io_records[0][1]["prompt"]
    assert io_records[1][1]["stdout"] == "done"
    assert io_records[1][1]["stderr"] != multibyte_stderr
    assert io_records[1][1]["stderr_truncated"] is True
    assert span_records[0]["status"] == "finished"


@pytest.mark.asyncio
async def test_asking_response_omits_log_reference_when_output_dir_unset(
    monkeypatch, tmp_path
):
    from guildbotics.intelligences.common import AgentResponse

    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(script="echo test", env={})
    }

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        return StubProcess(
            stdout=b'{"status": "asking", "message": "need input"}', returncode=0
        )

    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )

    try:
        brain = cli_agent.CliAgentBrain(
            "p1",
            "x",
            logger=type(
                "L",
                (),
                {
                    "debug": lambda *a, **k: None,
                    "info": lambda *a, **k: None,
                    "error": lambda *a, **k: None,
                },
            )(),
            response_class=AgentResponse,
        )
        output = await brain.run("hello", cwd=tmp_path)
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

    assert isinstance(output, AgentResponse)
    assert output.status == AgentResponse.ASKING
    assert output.message == "need input"
    assert "See:" not in output.message


class _BlockingProcess:
    """Fake subprocess whose communicate() blocks until the task is cancelled."""

    def __init__(self) -> None:
        self.returncode = None
        self.pid = 0
        self.killed = False
        self.started = __import__("asyncio").Event()

    async def communicate(self):
        self.started.set()
        await __import__("asyncio").sleep(30)
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def terminate(self) -> None:
        self.kill()

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


@pytest.mark.asyncio
async def test_cli_agent_kills_subprocess_on_cancellation(monkeypatch, tmp_path):
    import asyncio

    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(script="sleep 30", env={})
    }

    proc = _BlockingProcess()

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        assert _kwargs["start_new_session"] is True
        return proc

    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )

    try:
        brain = cli_agent.CliAgentBrain(
            "p1",
            "x",
            logger=type(
                "L",
                (),
                {
                    "debug": lambda *a, **k: None,
                    "info": lambda *a, **k: None,
                    "error": lambda *a, **k: None,
                },
            )(),
        )
        task = asyncio.create_task(
            brain.run_with_execution_details("hello", cwd=tmp_path)
        )
        await asyncio.wait_for(proc.started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The independently grouped in-flight agent subprocess must be terminated
        # and reaped, not left running behind its wrapper shell.
        assert proc.killed is True
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)


@pytest.mark.parametrize(
    ("current", "persisted", "expected"),
    [
        ("101.2", "101.1", "newer"),
        ("101.1", "101.1", "equal"),
        ("100.9", "101.1", "older"),
        ("2", "10", "older"),
        ("same-text", "same-text", "equal"),
        ("text-a", "text-b", "unknown"),
        ("", "101.1", "unknown"),
        ("101.1", "", "unknown"),
        ("", "", "unknown"),
    ],
)
def test_cursor_relation_orders_numeric_and_rejects_unorderable(
    current, persisted, expected
):
    assert cli_agent._cursor_relation(current, persisted) == expected


@pytest.mark.asyncio
async def test_read_only_native_turn_takes_no_person_execution_lease(
    monkeypatch, tmp_path
) -> None:
    from guildbotics.runtime.person_lease import PersonExecutionLease

    captured: dict = {}

    async def fake_execute_native_turn(self, *, input, configured, context, **_kwargs):
        captured["context"] = context
        return cli_agent.CliAgentExecutionResult(
            stdout="answer", stderr="", returncode=0
        )

    monkeypatch.setattr(
        cli_agent.CliAgentBrain, "_execute_native_turn", fake_execute_native_turn
    )
    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger())
    brain.executable_info = cli_agent.ExecutableInfo(
        script="", env={}, adapter="claude-stream-json"
    )

    # A routine already owns this member. A read-only assistant turn has to stay
    # usable anyway: that is exactly when its logs are worth asking about.
    lease = PersonExecutionLease("p1", tmp_path)
    lease.acquire(source="routine", command="ticket", work_id="work-1")
    try:
        result = await brain._execute_native(
            input="why did it fail?",
            cwd=tmp_path,
            kwargs={
                "session_state": {
                    "agent_execution_context": {
                        "run_id": "run-9",
                        "work_kind": "troubleshooting",
                        "workspace_data_root": str(tmp_path),
                        "read_only": True,
                    }
                }
            },
        )
    finally:
        lease.release()

    assert not result.error_category
    assert result.stdout == "answer"
    assert captured["context"].read_only is True
    assert captured["context"].lease_id == ""


@pytest.mark.asyncio
async def test_a_default_effort_turn_states_no_settings(monkeypatch, tmp_path) -> None:
    """`default` cancels the frontmatter but still imposes nothing downstream.

    Stating the level here would give the turn a non-empty fingerprint, which
    rotates a session-scoped provider's session -- the opposite of the "keep the
    session's current settings" meaning `default` carries.
    """
    captured: dict = {}

    async def fake_execute_native_turn(self, *, input, configured, context, **_kwargs):
        captured["context"] = context
        return cli_agent.CliAgentExecutionResult(
            stdout="answer", stderr="", returncode=0
        )

    monkeypatch.setattr(
        cli_agent.CliAgentBrain, "_execute_native_turn", fake_execute_native_turn
    )
    brain = cli_agent.CliAgentBrain("p1", "x", logger=_test_logger(), effort="high")
    brain.executable_info = cli_agent.ExecutableInfo(
        script="",
        env={},
        adapter="claude-stream-json",
        effort={"high": {"model": "big-model"}},
    )

    await brain._execute_native(
        input="hello",
        cwd=tmp_path,
        kwargs={
            "session_state": {
                "effort": "default",
                "agent_execution_context": {
                    "run_id": "run-9",
                    "work_kind": "troubleshooting",
                    "workspace_data_root": str(tmp_path),
                    "read_only": True,
                },
            }
        },
        effort=brain._resolve_provider_effort({"session_state": {"effort": "default"}}),
    )

    context = captured["context"]
    assert context.effort == ""
    assert context.provider_options == {}
    assert captured["context"].delegation_id == ""


# --------------------------------------------------------------------------- #
# Effort: mapping discovery, provider options and the one-shot env contract
# --------------------------------------------------------------------------- #


def _stub_logger():
    return type(
        "L",
        (),
        {
            "debug": lambda *a, **k: None,
            "info": lambda *a, **k: None,
            "warning": lambda *a, **k: None,
            "error": lambda *a, **k: None,
        },
    )()


def _write_definition(root, relative: str, body: str):
    path = root / "intelligences" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_a_native_tool_reads_its_own_definition(monkeypatch, tmp_path) -> None:
    _write_definition(
        tmp_path, "cli_agents/codex/default.yml", "effort:\n  high:\n    effort: high\n"
    )
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    cli_agent.person_cli_agent_mapping.clear()
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"default": "cli_agents/codex/default.yml"},
    )

    resolved = cli_agent.get_cli_agent_mapping("aiko")

    assert resolved["default"].adapter == "codex"
    assert resolved["default"].effort == {"high": {"effort": "high"}}
    cli_agent.person_cli_agent_mapping.clear()


def test_two_slots_on_one_tool_keep_their_own_settings(monkeypatch, tmp_path) -> None:
    """The reason slots exist: two features on one tool, configured apart.

    Before definitions were per-slot, both slots read the same tool file and
    could not differ at all.
    """
    _write_definition(
        tmp_path,
        "cli_agents/codex/default.yml",
        "effort:\n  high:\n    model: strong\n",
    )
    _write_definition(
        tmp_path,
        "cli_agents/codex/reviewer.yml",
        "effort:\n  high:\n    model: cheap\n",
    )
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    cli_agent.person_cli_agent_mapping.clear()
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {
            "default": "cli_agents/codex/default.yml",
            "reviewer": "cli_agents/codex/reviewer.yml",
        },
    )

    resolved = cli_agent.get_cli_agent_mapping("aiko")

    assert resolved["default"].effort == {"high": {"model": "strong"}}
    assert resolved["reviewer"].effort == {"high": {"model": "cheap"}}
    # Both still run on the same adapter.
    assert {info.adapter for info in resolved.values()} == {"codex"}
    cli_agent.person_cli_agent_mapping.clear()


def test_a_slot_inherits_the_keys_it_does_not_state(monkeypatch, tmp_path) -> None:
    _write_definition(
        tmp_path,
        "cli_agents/copilot/default.yml",
        "script: run-copilot\nenv:\n  A: '1'\neffort:\n  high:\n    effort: high\n",
    )
    _write_definition(
        tmp_path, "cli_agents/copilot/writer.yml", "effort:\n  high:\n    effort: max\n"
    )
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    cli_agent.person_cli_agent_mapping.clear()
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"writer": "cli_agents/copilot/writer.yml"},
    )

    resolved = cli_agent.get_cli_agent_mapping("aiko")

    # Its own effort wins; the script and env come from the tool's default.
    assert resolved["writer"].effort == {"high": {"effort": "max"}}
    assert resolved["writer"].script == "run-copilot"
    assert resolved["writer"].env == {"A": "1"}
    assert resolved["writer"].agent_name == "copilot"
    cli_agent.person_cli_agent_mapping.clear()


@pytest.mark.parametrize(
    ("effort_level", "expected"),
    [
        (
            "high",
            {
                "GUILDBOTICS_CLI_AGENT_EFFORT": "high",
                "GUILDBOTICS_CLI_AGENT_MODEL": "big-model",
                "GUILDBOTICS_CLI_AGENT_EFFORT_OPTIONS": '{"verbosity": "high"}',
            },
        ),
        # `default` and unspecified impose nothing, so the script keeps its own
        # defaults instead of having to recognize a no-op value.
        ("default", {}),
        ("", {}),
    ],
)
def test_one_shot_effort_environment_contract(effort_level, expected) -> None:
    decision = cli_agent.EffortDecision(
        resolved=cli_agent.ResolvedEffort(
            requested=effort_level, resolved=effort_level, source="runtime"
        ),
        provider_options=(
            {"model": "big-model", "verbosity": "high"}
            if effort_level == "high"
            else {}
        ),
    )
    assert decision.script_environment() == expected


def test_one_shot_effort_environment_omits_model_when_unmapped() -> None:
    decision = cli_agent.EffortDecision(
        resolved=cli_agent.ResolvedEffort(
            requested="low", resolved="low", source="runtime"
        ),
        provider_options={"reasoning": "minimal"},
    )
    assert decision.script_environment() == {
        "GUILDBOTICS_CLI_AGENT_EFFORT": "low",
        "GUILDBOTICS_CLI_AGENT_EFFORT_OPTIONS": '{"reasoning": "minimal"}',
    }


@pytest.mark.asyncio
async def test_runtime_effort_reaches_a_one_shot_script_environment(
    monkeypatch, tmp_path
) -> None:
    """`guildbotics run <command> effort=high` arrives via session_state."""
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(
            script="echo test",
            env={},
            effort={"high": {"model": "big-model", "verbosity": "high"}},
        )
    }
    captured: dict = {}

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        captured["env"] = env
        return StubProcess()

    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )
    try:
        brain = cli_agent.CliAgentBrain("p1", "x", logger=_stub_logger())
        await brain.run("hello", cwd=tmp_path, session_state={"effort": "high"})
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

    assert captured["env"]["GUILDBOTICS_CLI_AGENT_EFFORT"] == "high"
    assert captured["env"]["GUILDBOTICS_CLI_AGENT_MODEL"] == "big-model"
    assert (
        captured["env"]["GUILDBOTICS_CLI_AGENT_EFFORT_OPTIONS"]
        == '{"verbosity": "high"}'
    )
    assert "GUILDBOTICS_CLI_AGENT_EFFORT" not in os.environ


@pytest.mark.asyncio
async def test_frontmatter_effort_reaches_a_one_shot_script_environment(
    monkeypatch, tmp_path
) -> None:
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(
            script="echo test", env={}, effort={"high": {"model": "big-model"}}
        )
    }

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        fake_create_subprocess_shell.env = env
        return StubProcess()

    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )
    try:
        brain = cli_agent.CliAgentBrain("p1", "x", logger=_stub_logger(), effort="high")
        await brain.run("hello", cwd=tmp_path, session_state={})
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

    assert (
        fake_create_subprocess_shell.env["GUILDBOTICS_CLI_AGENT_MODEL"] == "big-model"
    )


@pytest.mark.asyncio
async def test_request_diagnostics_record_effort_keys_not_values(
    monkeypatch, tmp_path
) -> None:
    original = cli_agent.person_cli_agent_mapping.copy()
    io_records: list[tuple[str, dict]] = []
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(
            script="echo test", env={}, effort={"high": {"token": "secret-value"}}
        )
    }

    async def fake_create_subprocess_shell(*_args, **_kwargs):
        return StubProcess()

    monkeypatch.setattr(
        cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )
    monkeypatch.setattr(
        cli_agent,
        "record_correlated_io",
        lambda *, io_type, payload: io_records.append((io_type, payload)),
    )
    try:
        brain = cli_agent.CliAgentBrain("p1", "x", logger=_stub_logger(), effort="high")
        await brain.run("hello", cwd=tmp_path, session_state={})
    finally:
        cli_agent.person_cli_agent_mapping.clear()
        cli_agent.person_cli_agent_mapping.update(original)

    effort_payload = io_records[0][1]["effort"]
    assert effort_payload["resolved"] == "high"
    assert effort_payload["applied_keys"] == ["token"]
    assert "secret-value" not in str(effort_payload)


def test_a_mapping_effort_value_overrides_the_neutral_level() -> None:
    """A tool with a richer vocabulary than low/high must be drivable.

    Copilot accepts `xhigh`, Antigravity accepts `medium`; passing only the
    neutral label would put those out of reach.
    """
    decision = cli_agent.EffortDecision(
        resolved=cli_agent.ResolvedEffort(
            requested="high", resolved="high", source="runtime"
        ),
        provider_options={"effort": "xhigh", "model": "gpt-5.4"},
    )

    environment = decision.script_environment()

    assert environment["GUILDBOTICS_CLI_AGENT_EFFORT"] == "xhigh"
    assert environment["GUILDBOTICS_CLI_AGENT_MODEL"] == "gpt-5.4"
    # `effort` and `model` have names of their own, so neither is duplicated
    # into the catch-all JSON.
    assert "GUILDBOTICS_CLI_AGENT_EFFORT_OPTIONS" not in environment


def test_without_a_mapping_the_neutral_level_is_passed_through() -> None:
    """Every shipped tool accepts low/high, so a mapping stays optional."""
    decision = cli_agent.EffortDecision(
        resolved=cli_agent.ResolvedEffort(
            requested="low", resolved="low", source="runtime"
        )
    )

    assert decision.script_environment() == {"GUILDBOTICS_CLI_AGENT_EFFORT": "low"}


def test_a_tools_own_settings_apply_whatever_effort_was_asked_for(
    monkeypatch, tmp_path
) -> None:
    """`default` is the common case, so a model must not depend on a level.

    Without a baseline the only place to name a model was inside an effort
    level, which left every `default` turn -- every ordinary chat reply --
    unable to state one at all.
    """
    _write_definition(
        tmp_path,
        "cli_agents/codex/default.yml",
        "parameters:\n  model: steady\neffort:\n  high:\n    model: stronger\n",
    )
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    cli_agent.person_cli_agent_mapping.clear()
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"default": "cli_agents/codex/default.yml"},
    )
    try:
        brain = cli_agent.CliAgentBrain("aiko", "x", logger=_stub_logger())
        models = {
            requested: brain._resolve_provider_effort(
                {"session_state": {"effort": requested} if requested else {}}
            ).model
            for requested in ("high", "default", "")
        }
    finally:
        cli_agent.person_cli_agent_mapping.clear()

    assert models == {"high": "stronger", "default": "steady", "": "steady"}


def test_diagnostics_reflect_the_level_not_the_baseline(monkeypatch, tmp_path) -> None:
    """A baseline must not disguise an unmapped level as a supported one.

    The tool still runs with its standing settings, but the *effort decision*
    contributed nothing — diagnostics have to say so, exactly as the LLM API
    path does, or an ignored `high` would look applied.
    """
    _write_definition(
        tmp_path,
        "cli_agents/codex/default.yml",
        "parameters:\n  model: steady\neffort:\n  low:\n    effort: low\n",
    )
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    cli_agent.person_cli_agent_mapping.clear()
    monkeypatch.setattr(
        cli_agent,
        "load_person_slot_mapping",
        lambda *_args: {"default": "cli_agents/codex/default.yml"},
    )
    try:
        brain = cli_agent.CliAgentBrain("aiko", "x", logger=_stub_logger())
        unmapped = brain._resolve_provider_effort({"session_state": {"effort": "high"}})
        mapped = brain._resolve_provider_effort({"session_state": {"effort": "low"}})
    finally:
        cli_agent.person_cli_agent_mapping.clear()

    unmapped_payload = unmapped.diagnostics()
    assert unmapped_payload["unsupported"] is True
    assert unmapped_payload["applied_keys"] == []
    # The tool itself still runs on its standing settings.
    assert unmapped.provider_options == {"model": "steady"}
    assert unmapped_payload["model"] == "steady"

    mapped_payload = mapped.diagnostics()
    assert mapped_payload["unsupported"] is False
    # Only the level's own contribution counts as applied, not the baseline.
    assert mapped_payload["applied_keys"] == ["effort"]


def test_a_baseline_model_reaches_a_one_shot_script_without_an_effort(
    monkeypatch, tmp_path
) -> None:
    decision = cli_agent.EffortDecision(
        resolved=cli_agent.ResolvedEffort(), provider_options={"model": "steady"}
    )

    environment = decision.script_environment()

    assert environment == {"GUILDBOTICS_CLI_AGENT_MODEL": "steady"}
    # No level was stated, so the script keeps its own effort default.
    assert "GUILDBOTICS_CLI_AGENT_EFFORT" not in environment


def test_a_tools_own_effort_applies_on_a_default_turn() -> None:
    """A `default` request is not a reason to withhold a standing setting.

    `default` means the caller asks for no particular effort; it does not mean
    the slot has no configured one. Dropping it here would leave the baseline
    working for `model` but silently ignored for `effort`.
    """
    baseline_only = cli_agent.EffortDecision(
        resolved=cli_agent.ResolvedEffort(
            requested="default", resolved="default", source="runtime"
        ),
        provider_options={"effort": "high", "model": "steady"},
    )

    assert baseline_only.script_environment() == {
        "GUILDBOTICS_CLI_AGENT_EFFORT": "high",
        "GUILDBOTICS_CLI_AGENT_MODEL": "steady",
    }


def test_a_default_turn_with_nothing_configured_stays_silent() -> None:
    """Without a standing setting, the tool keeps its own preference."""
    decision = cli_agent.EffortDecision(
        resolved=cli_agent.ResolvedEffort(
            requested="default", resolved="default", source="runtime"
        )
    )

    assert decision.script_environment() == {}
