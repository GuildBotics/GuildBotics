import json

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


@pytest.mark.asyncio
async def test_cli_agent_run_passes_cwd_without_mutating_mapping(monkeypatch, tmp_path):
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(script="echo test", env={"A": "1"})
    }

    captured = {}

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None
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
        script, cwd=None, env=None, stdout=None, stderr=None
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
        script, cwd=None, env=None, stdout=None, stderr=None
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
        script, cwd=None, env=None, stdout=None, stderr=None
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
async def test_cli_agent_run_raises_when_response_is_empty(monkeypatch, tmp_path):
    original = cli_agent.person_cli_agent_mapping.copy()
    cli_agent.person_cli_agent_mapping.clear()
    cli_agent.person_cli_agent_mapping["p1"] = {
        "default": cli_agent.ExecutableInfo(script="echo test", env={})
    }

    async def fake_create_subprocess_shell(
        script, cwd=None, env=None, stdout=None, stderr=None
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
        script, cwd=None, env=None, stdout=None, stderr=None
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
            "workflows/chat/chat_reply_actionable",
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
    assert events[0]["brain"] == "workflows/chat/chat_reply_actionable"
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
        script, cwd=None, env=None, stdout=None, stderr=None
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
