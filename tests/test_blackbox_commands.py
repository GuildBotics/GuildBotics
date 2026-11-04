"""The blackbox launcher accepts an explicitly configured CLI."""

from pathlib import Path
from types import SimpleNamespace
from tests.blackbox.harness import Stack


def test_cli_uses_the_configured_program_and_arguments(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "GUILDBOTICS_BLACKBOX_CLI", '"/tmp/space dir/guildbotics" --profile test'
    )
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="answer", stderr="")

    monkeypatch.setattr("tests.blackbox.harness.subprocess.run", run)
    stack = Stack(tmp_path, {"PATH": "/bin"}, None, None, tmp_path / "server.log")
    assert stack.cli("member", "context") == "answer"
    assert calls[0][0] == [
        "/tmp/space dir/guildbotics",
        "--profile",
        "test",
        "member",
        "context",
    ]
    assert calls[0][1]["env"] == {"PATH": "/bin"}
