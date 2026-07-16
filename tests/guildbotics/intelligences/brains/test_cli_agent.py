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
    [("codex", "codex"), ("codex-cli.yml", "codex"), ("claude", "claude")],
)
def test_cli_agent_mapping_selects_native_adapter_without_script_file(
    monkeypatch, tmp_path, mapping_value, adapter
) -> None:
    mapping = tmp_path / "cli_agent_mapping.yml"
    mapping.write_text(f"default: {mapping_value}\n", encoding="utf-8")
    cli_agent.person_cli_agent_mapping.clear()
    monkeypatch.setattr(cli_agent, "get_person_config_path", lambda *_args: mapping)

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
async def test_cli_agent_run_applies_session_state_env_overlay(monkeypatch, tmp_path):
    # cli_agent_env in session_state is scoped to this single subprocess and
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
            session_state={"cli_agent_env": {"GUILDBOTICS_TASK_RUN_ID": "run-123"}},
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
            session_state={"cli_agent_env": {"GUILDBOTICS_TASK_RUN_ID": "run-123"}},
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
async def test_cli_agent_prompt_trace_records_request_and_response(
    monkeypatch, tmp_path
):
    original = cli_agent.person_cli_agent_mapping.copy()
    trace_path = tmp_path / "prompt_trace.jsonl"
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(script="echo test", env={})
    }

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None, **_kwargs
    ):
        return StubProcess(stdout=b"done", stderr=b"debug output", returncode=0)

    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE", "1")
    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE_PATH", str(trace_path))
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

    events = [
        json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events] == [
        "cli_agent.request",
        "cli_agent.response",
    ]
    assert events[0]["person_id"] == "p1"
    assert events[0]["brain"] == "functions/handle_chat_event"
    assert "Reply as Alice." in events[0]["prompt"]
    assert events[1]["stdout"] == "done"
    assert events[1]["stderr"] == "debug output"


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

    # LOG_OUTPUT_DIR unset -> no per-call log file is created, so the ASKING
    # message must not gain a misleading empty "See:" reference.
    monkeypatch.setattr(cli_agent, "get_log_output_dir", lambda *a, **k: None)
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
