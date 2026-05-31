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

    async def fake_create_subprocess_shell(script, cwd=None, env=None, stdout=None, stderr=None):
        captured["script"] = script
        captured["cwd"] = cwd
        captured["env"] = env
        return StubProcess()

    monkeypatch.setattr(cli_agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell)

    try:
        brain = cli_agent.CliAgentBrain("p1", "x", logger=type("L", (), {"debug": lambda *a, **k: None, "info": lambda *a, **k: None, "error": lambda *a, **k: None})())
        output = await brain.run("hello", cwd=tmp_path)
        assert output == "done"
        assert captured["cwd"] == str(tmp_path)
        assert "PROMPT_FILE" in captured["env"]
        assert not hasattr(brain.executable_info, "cwd")
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
