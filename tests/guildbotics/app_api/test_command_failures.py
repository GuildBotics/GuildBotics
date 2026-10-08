"""Command failures reach Desktop through the guest reply and host reconstruction.

Run the real command entry in process; replace only the VM and external I/O.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import requests
from fastapi.testclient import TestClient

from guildbotics.app_api.api import TOKEN_HEADER, create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.drivers import command_runner
from guildbotics.editions.simple.simple_brain_factory import (
    BrainConfig,
    person_brain_mapping,
)
from guildbotics.intelligences.agent_runtime.host_client import (
    CommandFacts,
    CommandReply,
    CommandRequest,
    HostCallError,
)
from guildbotics.intelligences.agent_runtime.host_window import _inference_failed
from guildbotics.intelligences.brains import inference as inference_module
from guildbotics.intelligences.brains.agno_agent import (
    AgnoAgentDefaultBrain,
    ModelConfig,
    person_model_mapping,
)
from guildbotics.intelligences.brains.jev import JevBrain
from guildbotics.runtime import command_entry
from guildbotics.utils.i18n_tool import set_language, t

_WINDOW_FAILURES = {
    "timeout": "The host call timed out.",
    "large": "The call's result is too large.",
    "host": "Host operation failed.",
}


@pytest.fixture
def command_api(tmp_path, monkeypatch, language):
    config = tmp_path / ".guildbotics/config"
    member = config / "team/members/aiko"
    member.mkdir(parents=True)
    (member / "person.yml").write_text(
        "person_id: aiko\nname: Aiko\nis_active: true\n", encoding="utf-8"
    )
    (config / "team/project.yml").write_text(
        f"name: demo\nlanguage: {language}\n", encoding="utf-8"
    )
    commands = config / "commands"
    commands.mkdir()
    (commands / "echo.py").write_text(
        "def main():\n    return 'hello'\n", encoding="utf-8"
    )
    for prefix in ("", "async "):
        name = "custom_async" if prefix else "custom"
        (commands / f"{name}.py").write_text(
            f"{prefix}def main(context, *, required):\n"
            "    raise AssertionError('must not enter main')\n",
            encoding="utf-8",
        )
    (commands / "defect.py").write_text(
        "def main():\n    raise TypeError('private implementation detail')\n",
        encoding="utf-8",
    )
    (commands / "duplicate.py").write_text(
        "def main(context, first, **kwargs):\n"
        "    raise AssertionError('must not enter main')\n",
        encoding="utf-8",
    )
    (commands / "python_brain.py").write_text(
        "async def main(context, brain='default'):\n"
        "    model = context.get_brain('probe', {'brain': brain, 'body': 'Answer.'}, None)\n"
        '    return await model.run(\'{"state": {}, "questions": {}}\')\n',
        encoding="utf-8",
    )
    (commands / "agno.md").write_text("Answer hello.\n", encoding="utf-8")
    (commands / "jev.md").write_text(
        '---\nbrain: jev\n---\n{"state": {}, "questions": {}}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(config))
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    set_language(language)
    monkeypatch.setitem(
        person_brain_mapping,
        "aiko",
        {
            "default": BrainConfig(type=AgnoAgentDefaultBrain),
            "jev": BrainConfig(type=JevBrain),
        },
    )
    monkeypatch.setitem(
        person_model_mapping,
        "aiko",
        {"default": ModelConfig(name="test", model_class="test.Model")},
    )
    state = SimpleNamespace(failure="", replies=[])

    class Window:
        async def acall(self, name, **kwargs):
            if name in {"agno", "jev"}:
                if state.failure in _WINDOW_FAILURES:
                    raise HostCallError("failed", _WINDOW_FAILURES[state.failure])
                error = requests.HTTPError(
                    "private key sk-secret", response=SimpleNamespace(status_code=429)
                )
                raise _inference_failed(error)
            assert name == "member"
            operation = kwargs["arguments"][1]
            if operation == state.failure:
                return {"exit_code": 1, "stdout": "", "stderr": "Error: chat denied"}
            assert operation == "resolve-channel"
            return {"exit_code": 0, "stdout": json.dumps({"channel_id": "C1"})}

    window = Window()
    monkeypatch.setattr(inference_module, "command_window", lambda: window)

    async def in_process(command):
        reply = await command_entry.run(
            CommandRequest(
                path=str(command.path),
                name=command.command_name,
                args=command.args,
                cwd=str(command.cwd),
                pipe=command.context.pipe,
            ),
            CommandFacts(
                person_id="aiko",
                run_id="run",
                work_kind="manual",
                work_identity="run",
                trace_id="trace",
                access=command.access,
                mounts={tmp_path.as_posix(): True},
            ),
            window,
        )
        state.replies.append(reply)
        return command_runner._outcome(
            command, CommandReply.model_validate_json(reply.model_dump_json())
        )

    monkeypatch.setattr(command_runner, "run_in_environment", in_process)
    runtime = AppRuntime(EventBus())
    state.client = TestClient(create_app(session_token="test", runtime=runtime))
    state.cwd = str(tmp_path)
    return state


@pytest.mark.parametrize("language", ["en", "ja"])
@pytest.mark.parametrize(
    ("command", "args", "failure", "reason"),
    [
        ("repository/security_alerts", [], "", "repo"),
        ("custom", [], "", "required"),
        ("custom_async", [], "", "required"),
        ("duplicate", ["one", "first=two"], "", "multiple values for argument 'first'"),
        ("agno", [], "", "inference"),
        ("jev", [], "", "inference"),
        *[
            ("python_brain", [f"brain={brain}"], "", "inference")
            for brain in ("default", "jev")
        ],
        *[
            ("python_brain", [f"brain={brain}"], failure, message)
            for brain in ("default", "jev")
            for failure, message in _WINDOW_FAILURES.items()
        ],
        (
            "workflows/chat_post_command",
            ["service=discord", "channel_id=C1", "command=print"],
            "",
            "unsupported",
        ),
        *[
            (
                "workflows/chat_post_command",
                ["channel_name=general", "command=echo"],
                operation,
                "chat denied",
            )
            for operation in ("resolve-channel", "post")
        ],
        *[
            ("examples/reports/tools/fetch_ai_news", [], failure, "network")
            for failure in ("ConnectionError", "Timeout", "HTTPError")
        ],
    ],
)
def test_anticipated_failure_reaches_desktop(
    command_api, monkeypatch, command, args, failure, reason
):
    command_api.failure = failure

    def fetch(*_args, **_kwargs):
        error = getattr(requests, failure)("request failed")
        if failure == "HTTPError":
            return SimpleNamespace(raise_for_status=lambda: _raise(error))
        raise error

    monkeypatch.setattr(requests, "get", fetch)
    response = command_api.client.post(
        "/commands/run",
        headers={TOKEN_HEADER: "test"},
        json={
            "command": command,
            "args": args,
            "person": "aiko",
            "cwd": command_api.cwd,
            "message": '{"state": {}, "questions": {}}' if command == "jev" else "",
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "command_error"
    message = response.json()["message"]
    if reason == "inference":
        assert (
            t(
                "intelligences.inference.failed_with_status",
                error_type="HTTPError",
                status=429,
            )
            in message
        )
    elif reason == "unsupported":
        assert message == t(
            "commands.workflows.chat_post_command.unsupported_service",
            service="discord",
        )
    elif reason == "network":
        assert message == t(
            "commands.examples.reports.fetch_ai_news.network", error_type=failure
        )
    elif reason in {"repo", "required"}:
        assert message.startswith(
            t("commands.python.arguments", command=command, reason="")
        )
        assert reason in message
    else:
        assert reason in message
    assert "sk-secret" not in response.text
    assert command_api.replies[-1].failure.command


def _raise(error):
    raise error


@pytest.mark.parametrize("language", ["en"])
def test_python_body_type_error_remains_a_defect(command_api):
    response = command_api.client.post(
        "/commands/run",
        headers={TOKEN_HEADER: "test"},
        json={"command": "defect", "person": "aiko", "cwd": command_api.cwd},
    )
    assert response.status_code == 500
    assert "private implementation detail" not in response.text
    assert not command_api.replies[-1].failure.command
