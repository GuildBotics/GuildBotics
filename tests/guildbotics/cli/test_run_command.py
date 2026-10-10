import inspect
import json
import sys
import textwrap
from pathlib import Path

import click
import httpx
import pytest
from click.testing import CliRunner

import guildbotics.cli.run as run_module
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.cli import main
from guildbotics.cli.member import run_in_process
from guildbotics.cli.run import _parse_command_spec
from guildbotics.commands.errors import (
    PersonNotFoundError,
    PersonSelectionRequiredError,
)
from guildbotics.commands.models import CommandOutcome
from guildbotics.drivers.command_runner import prepare_command, run_main_command
from guildbotics.drivers.member_context import resolve_person
from guildbotics.entities.team import Person, Project, Team
from guildbotics.environment import member_broker
from guildbotics.intelligences.functions import to_text
from guildbotics.runtime.context import Context
from guildbotics.runtime.member_invocation import Work
from guildbotics.runtime.person_lease import PersonExecutionLease
from guildbotics.runtime.workflow_invocation import (
    WORKFLOW_INVOCATION_KEY,
    WorkflowInvocation,
)
from guildbotics.utils import local_api
from guildbotics.utils.correlation import current_trace, set_attributes
from guildbotics.utils.local_api import (
    PROOF_PATH,
    TOKEN_HEADER,
    LocalApiEndpoint,
    local_api_proof,
)
from guildbotics.utils.safe_paths import normalize_host_path
from tests.guildbotics.command_environment_doubles import machinery
from tests.guildbotics.runtime.configured_team import make_context
from tests.guildbotics.runtime.test_context import (
    DummyBrainFactory,
    DummyIntegrationFactory,
)

#: The commands the host starts run in this process.
pytestmark = pytest.mark.usefixtures("configured_team", "commands_in_process")


def test_parse_command_spec_with_person():
    name, person = _parse_command_spec("translate@yuki")
    assert name == "translate"
    assert person == "yuki"


def test_parse_command_spec_without_person():
    name, person = _parse_command_spec(" summarize ")
    assert name == "summarize"
    assert person is None


@pytest.fixture
def desktop_route(tmp_path, monkeypatch):
    monkeypatch.setattr(local_api, "endpoint_path", lambda: tmp_path / "app-api.json")
    monkeypatch.setattr(run_module, "selected_workspace", lambda: tmp_path)
    endpoint = LocalApiEndpoint(
        port=8765,
        token="test-token",
        service_instance_id="instance",
        workspace=tmp_path,
    )
    endpoint.publish()
    calls = []

    async def local(*args):
        calls.append(("local", args))
        click.echo("local output")

    monkeypatch.setattr(run_module, "_run_custom_command", local)
    client_type = httpx.Client

    def respond(handler):
        def request(req):
            if req.url.path == PROOF_PATH:
                assert TOKEN_HEADER not in req.headers
                nonce = json.loads(req.content)["nonce"]
                proof = local_api_proof("test-token", nonce, "instance")
                return httpx.Response(200, json={"proof": proof})
            calls.append((req.url.path, req))
            assert req.headers[TOKEN_HEADER] == "test-token"
            return handler(req)

        monkeypatch.setattr(
            local_api.httpx,
            "Client",
            lambda **kwargs: client_type(
                **kwargs, transport=httpx.MockTransport(request)
            ),
        )

    def healthy(req):
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "service_instance_id": "instance",
                "workspace": str(tmp_path),
            },
        )

    return endpoint, calls, respond, healthy


@pytest.mark.parametrize(
    "person_args", [["ask@alice"], ["ask@bob", "--person", "alice"]]
)
def test_cli_delegates_stdin_args_person_and_absolute_cwd(
    desktop_route, tmp_path, person_args
):
    endpoint, calls, respond, healthy = desktop_route

    def handler(req):
        if req.url.path == "/health":
            return healthy(req)
        assert json.loads(req.content) == {
            "command": "ask",
            "args": ["topic=review", "value"],
            "person": "alice",
            "message": "日本語\nreview",
            "cwd": str(tmp_path),
            "expected_workspace": str(tmp_path),
        }
        assert all(value is None for value in req.extensions["timeout"].values())
        return httpx.Response(200, json={"trace_id": "run", "output": "review output"})

    respond(handler)
    result = CliRunner().invoke(
        main,
        ["run", *person_args, "--cwd", str(tmp_path), "topic=review", "value"],
        input="日本語\nreview",
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == "review output\n"
    assert [path for path, _ in calls] == ["/health", "/commands/run"]


@pytest.mark.parametrize(
    "state",
    [
        "missing",
        "workspace",
        "health_error",
        "connection",
        "malformed",
    ],
)
def test_cli_runs_locally_only_when_no_matching_desktop(
    desktop_route, monkeypatch, state
):
    endpoint, calls, respond, healthy = desktop_route
    if state == "missing":
        local_api.endpoint_path().unlink()

    def handler(req):
        if state == "connection":
            raise httpx.ConnectError("not listening")
        if state == "health_error":
            return httpx.Response(401)
        if state == "malformed":
            return httpx.Response(200, json=[])
        payload = healthy(req).json()
        payload["workspace"] = "other"
        return httpx.Response(200, json=payload)

    respond(handler)
    result = CliRunner().invoke(main, ["run", "ask"], input="review")
    assert result.exit_code == 0, result.output
    assert result.stdout == "local output\n"
    assert calls[-1][0] == "local"
    # Locally, too, the command works where it was started.
    assert calls[-1][1][-1] == Path.cwd()
    assert not any(path == "/commands/run" for path, _ in calls)


@pytest.mark.parametrize(
    "person_args", [["ask@alice"], ["ask@bob", "--person", "alice"]]
)
def test_cli_runs_locally_as_the_named_member_in_cwd_after_loading_env(
    tmp_path, monkeypatch, person_args
):
    """Without a matching Desktop the command runs in this process, with the
    secrets published first and without overriding what is already set, and
    with the member CLI installed for the command's member broker."""
    monkeypatch.setattr(member_broker, "_member_cli", None)
    monkeypatch.setattr(run_module, "selected_workspace", lambda: tmp_path)
    monkeypatch.setattr(run_module, "run_on_desktop", lambda *args: None)
    context = _get_context()
    events: list[tuple] = []

    def create_context(message: str = "") -> Context:
        events.append(("context", message))
        return context

    def prepare(base, command_name, command_args, person_identifier, cwd):
        events.append(
            ("prepare", base, command_name, tuple(command_args), person_identifier, cwd)
        )
        return prepare_command(base, command_name, command_args, person_identifier, cwd)

    async def run(command, *, source):
        events.append(("run", command.command_name, source))
        assert member_broker.member_cli() is run_in_process
        return CommandOutcome(result=None, text_output="local output")

    monkeypatch.setattr(run_module, "create_context", create_context)
    monkeypatch.setattr(
        run_module,
        "load_guildbotics_env",
        lambda **kwargs: events.append(("env", kwargs)),
    )
    monkeypatch.setattr(run_module, "prepare_command", prepare)
    monkeypatch.setattr(run_module, "run_main_command", run)

    result = CliRunner().invoke(
        main,
        ["run", *person_args, "--cwd", str(tmp_path), "topic=review"],
        input="review",
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "local output\n"
    assert events == [
        ("context", "review"),
        ("env", {"override": False}),
        (
            "prepare",
            context,
            "ask",
            ("topic=review",),
            "alice",
            normalize_host_path(tmp_path),
        ),
        ("run", "ask", "manual"),
    ]


@pytest.mark.parametrize(
    "failure", ["command_already_running", "work_rejected", "connection"]
)
def test_cli_never_runs_locally_after_post(desktop_route, failure):
    endpoint, calls, respond, healthy = desktop_route

    def handler(req):
        if req.url.path == "/health":
            return healthy(req)
        if failure == "connection":
            raise httpx.ReadError("connection lost after sending")
        return httpx.Response(
            409,
            json={
                "code": failure,
                "message": "実行中のため開始できません",
                "context": {},
            },
        )

    respond(handler)
    result = CliRunner().invoke(main, ["run", "ask"], input="review")
    assert result.exit_code != 0
    assert (
        "実行中のため開始できません" in result.stderr
        if failure != "connection"
        else "connection lost" in result.stderr
    )
    assert not any(path == "local" for path, _ in calls)


def test_cli_reports_plain_http_error_without_retry(desktop_route):
    endpoint, calls, respond, healthy = desktop_route
    respond(
        lambda req: (
            healthy(req)
            if req.url.path == "/health"
            else httpx.Response(500, text="Internal Server Error")
        )
    )
    result = CliRunner().invoke(main, ["run", "ask"], input="review")
    assert result.exit_code == 1
    assert "HTTP 500: Internal Server Error" in result.stderr
    assert not any(path == "local" for path, _ in calls)


def test_cli_preserves_default_cwd_and_empty_desktop_output(desktop_route):
    endpoint, calls, respond, healthy = desktop_route

    def handler(req):
        if req.url.path == "/health":
            return healthy(req)
        assert json.loads(req.content)["cwd"] == str(Path.cwd())
        return httpx.Response(200, json={"trace_id": "run", "output": ""})

    respond(handler)
    result = CliRunner().invoke(main, ["run", "ask"], input="review")
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert not any(path == "local" for path, _ in calls)


def _team(*members: Person, default_person_id: str = "") -> Team:
    return Team(
        project=Project(name="demo", default_person_id=default_person_id),
        members=list(members),
    )


def test_resolve_person_with_explicit_identifier():
    team = _team(
        Person(person_id="yuki", name="Yuki", is_active=True),
        Person(person_id="kato", name="Kato", is_active=False),
    )
    person = resolve_person(team, "Kato", allow_default=True)
    assert person.person_id == "kato"


def test_resolve_person_defaults_to_single_active():
    team = _team(Person(person_id="yuki", name="Yuki", is_active=True))
    person = resolve_person(team, None, allow_default=True)
    assert person.person_id == "yuki"


def test_resolve_person_defaults_to_first_candidate_without_configuration():
    team = _team(
        Person(person_id="yuki", name="Yuki", is_active=True),
        Person(person_id="akira", name="Akira", is_active=True),
        Person(person_id="aiko", name="Aiko", is_active=False),
        Person(person_id="ai", name="Ai", is_active=True, person_type="human"),
    )
    person = resolve_person(team, None, allow_default=True)
    assert person.person_id == "akira"


def test_resolve_person_uses_configured_default():
    team = _team(
        Person(person_id="yuki", name="Yuki", is_active=True),
        Person(person_id="akira", name="Akira", is_active=True),
        default_person_id="akira",
    )
    person = resolve_person(team, None, allow_default=True)
    assert person.person_id == "akira"


def test_resolve_person_prefers_explicit_identifier_over_default():
    team = _team(
        Person(person_id="yuki", name="Yuki", is_active=True),
        Person(person_id="akira", name="Akira", is_active=True),
        default_person_id="akira",
    )
    person = resolve_person(team, "yuki", allow_default=True)
    assert person.person_id == "yuki"


def test_resolve_person_reports_stale_default_as_unknown_person():
    team = _team(
        Person(person_id="yuki", name="Yuki", is_active=True),
        Person(person_id="akira", name="Akira", is_active=True),
        default_person_id="removed",
    )
    with pytest.raises(PersonNotFoundError) as excinfo:
        resolve_person(team, None, allow_default=True)
    assert excinfo.value.identifier == "removed"


def test_resolve_person_requires_identifier_without_any_candidate():
    team = _team(
        Person(person_id="yuki", name="Yuki", is_active=False),
        Person(person_id="ai", name="Ai", is_active=True, person_type="human"),
    )
    with pytest.raises(PersonSelectionRequiredError):
        resolve_person(team, None, allow_default=True)


def test_resolve_person_ignores_default_when_not_allowed():
    team = _team(
        Person(person_id="yuki", name="Yuki", is_active=True),
        default_person_id="yuki",
    )
    with pytest.raises(PersonSelectionRequiredError):
        resolve_person(team, None)


def test_resolve_person_raises_when_unknown():
    team = _team(Person(person_id="yuki", name="Yuki", is_active=True))
    with pytest.raises(PersonNotFoundError):
        resolve_person(team, "akira", allow_default=True)


class RecordingBrain:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []
        self.response_class = None

    async def run(self, message: str, **_: object) -> str:
        self.calls.append(message)
        return f"{self.name}:{message}"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def _get_context(message: str = "") -> Context:
    person = Person(person_id="alice", name="Alice", is_active=True)
    return _context_for_team(_team(person), message).clone_for(person)


def _context_for_person(person: Person, message: str = "") -> Context:
    return _context_for_team(_team(person), message)


def _context_for_team(team: Team, message: str = "") -> Context:
    return make_context(team, DummyIntegrationFactory(), DummyBrainFactory(), message)


async def _run_locally(
    monkeypatch, context: Context, command_spec: str, cwd: Path, *args: str
) -> None:
    """Run ``command_spec`` the way ``guildbotics run`` does without a Desktop."""

    def create_context(message: str = "") -> Context:
        return context

    monkeypatch.setattr(run_module, "create_context", create_context)
    await run_module._run_custom_command(command_spec, args, None, context.pipe, cwd)


@pytest.mark.asyncio
async def test_run_custom_command_returns_brain_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "commands/solo.md",
        """
        ---
        brain: none
        template_engine: jinja2
        ---
        Greetings {{ arg1 }}
        {{ context.pipe }}
        """,
    )

    await _run_locally(
        monkeypatch, _get_context("stdin text"), "solo", tmp_path, "world"
    )

    assert capsys.readouterr().out == "Greetings world\nstdin text\n"


@pytest.mark.asyncio
async def test_run_custom_command_runs_as_configured_default_person(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "commands/whoami.md",
        """
        ---
        brain: none
        template_engine: jinja2
        ---
        {{ context.person.person_id }}
        """,
    )
    team = _team(
        Person(person_id="yuki", name="Yuki", is_active=True),
        Person(person_id="akira", name="Akira", is_active=True),
        default_person_id="akira",
    )

    await _run_locally(monkeypatch, _context_for_team(team), "whoami", tmp_path)

    assert capsys.readouterr().out == "akira\n"


@pytest.mark.asyncio
async def test_a_command_that_cannot_be_resolved_holds_no_lease(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    context = _get_context()

    with pytest.raises(click.ClickException):
        await _run_locally(monkeypatch, context, "missing", tmp_path)
    assert list(RunStore().records()) == []

    _write(tmp_path / "commands/solo.md", "---\nbrain: none\n---\ndone")
    await _run_locally(monkeypatch, context, "solo", tmp_path)
    assert capsys.readouterr().out == "done\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(("read_only", "runs"), [(False, False), (True, True)])
async def test_a_writing_command_is_refused_while_another_run_holds_the_member(
    tmp_path, monkeypatch, capsys, read_only: bool, runs: bool
):
    """The member's lease is the same one the Desktop and the scheduler take,
    so a command that can change things waits its turn; one that declares
    itself read-only changes nothing and runs alongside."""
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "commands/solo.md",
        f"---\nbrain: none\nread_only: {str(read_only).lower()}\n---\ndone",
    )
    holder = PersonExecutionLease("alice")
    holder.acquire(source="scheduled", command="workflows/other", work_id="other")
    try:
        if runs:
            await _run_locally(monkeypatch, _get_context(), "solo", tmp_path)
        else:
            # The refusal names the run that holds the member.
            with pytest.raises(click.ClickException, match="workflows/other"):
                await _run_locally(monkeypatch, _get_context(), "solo", tmp_path)
    finally:
        holder.release()

    assert (capsys.readouterr().out == "done\n") is runs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "status", "boundary"),
    [
        ("done", "succeeded", "command.finished"),
        ("{{ undefined_filter | nope }}", "failed", "command.failed"),
    ],
)
async def test_a_local_run_records_the_run_its_trace_opens(
    tmp_path, monkeypatch, body: str, status: str, boundary: str
):
    """The run is recorded as the Desktop's manual run is: the task run and
    both ends of the command, all under one trace whose id is the run's."""
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "commands/solo.md",
        f"---\nbrain: none\ntemplate_engine: jinja2\n---\n{body}",
    )
    recorded: list[tuple[str, str]] = []

    def _record(*, event_type: str, **_: object) -> None:
        trace = current_trace()
        recorded.append((event_type, trace.trace_id if trace is not None else ""))

    monkeypatch.setattr("guildbotics.drivers.utils.record_correlated_event", _record)

    if status == "succeeded":
        await _run_locally(monkeypatch, _get_context(), "solo", tmp_path)
    else:
        with pytest.raises(click.ClickException):
            await _run_locally(monkeypatch, _get_context(), "solo", tmp_path)

    [record] = RunStore().records()
    assert recorded == [
        ("command.started", record.run_id),
        (boundary, record.run_id),
    ]
    assert record.status == status
    assert record.source == "manual"
    assert record.execution_mode == "user_initiated"
    assert record.member_id == "alice"
    assert record.work_kind == "solo"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command_spec", "person_option"),
    [("solo", "aiko"), ("solo@aiko", None)],
)
async def test_cli_run_rejects_human_member_without_traceback(
    tmp_path, monkeypatch, command_spec: str, person_option: str | None
):
    human = Person(
        person_id="aiko",
        name="Aiko",
        is_active=False,
        person_type="human",
    )
    context = _context_for_person(human)

    def create_context(message: str = "") -> Context:
        assert message == ""
        return context

    monkeypatch.setattr(run_module, "create_context", create_context)

    with pytest.raises(click.ClickException) as exc_info:
        await run_module._run_custom_command(
            command_spec, (), person_option, "", tmp_path
        )

    assert "cannot be used as an AI execution subject" in str(exc_info.value)


@pytest.mark.asyncio
async def test_a_local_runs_record_keeps_what_its_trace_learned(tmp_path, monkeypatch):
    """The trace stays open around the whole task run, so what the run's
    trace learns while it runs -- the ticket the selector took -- is on the
    run's record when it finishes."""
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(tmp_path / "commands/solo.md", "---\nbrain: none\n---\ndone")

    async def run(command, *, source):
        set_attributes(**{"github.title": "ログイン修正"})
        return CommandOutcome(result=None, text_output="")

    monkeypatch.setattr(run_module, "run_main_command", run)

    await _run_locally(monkeypatch, _get_context(), "solo", tmp_path)

    [record] = RunStore().records()
    assert record.attributes["github.title"] == "ログイン修正"


@pytest.mark.asyncio
async def test_executor_runs_markdown_with_subcommands(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "commands/pipeline.md",
        """
        ---
        brain: none
        commands:
          - name: first_payload
            path: first.md
          - name: python_payload
            path: tools/python_step.py
            params:
              foo: bar
        ---
        Main start for {{1}}
        """,
    )
    _write(
        tmp_path / "commands/first.md",
        """
        ---
        brain: default
        ---
        First step
        """,
    )
    _write(
        tmp_path / "commands/tools/python_step.py",
        """
        from guildbotics.runtime import Context


        async def main(context: Context, foo: str):
            return {"pipe": context.pipe, "foo": foo}
        """,
    )

    context = _get_context("initial")
    executor = machinery(context, "pipeline", ["ARG"], tmp_path)
    result = (await executor.run()).text_output

    runner = executor.context
    assert runner.shared_state["pipeline"].startswith("Main start for ARG")
    assert "first_payload" in runner.shared_state
    assert runner.shared_state["python_payload"] == {
        "pipe": to_text(runner.shared_state["first_payload"]),
        "foo": "bar",
    }
    assert runner.pipe == result


@pytest.mark.asyncio
@pytest.mark.usefixtures("shell_commands")
async def test_executor_runs_shell_command(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "commands/shell_driver.md",
        """
        ---
        brain: none
        commands:
          - name: shell_output
            path: tools/echo.sh
            params:
              foo: bar
            args:
            - alpha
            - beta
        ---
        Shell body {{1}}
        """,
    )

    script_path = tmp_path / "commands/tools/echo.sh"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(
        """
        #!/usr/bin/env bash
        set -euo pipefail

        echo "args:$*"
        echo "stdin:$(cat)"
        echo "FOO=${foo:-missing}"
        """.strip()
        + "\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    context = _get_context("initial")
    executor = machinery(context, "shell_driver", ["ARG"], tmp_path)
    result = (await executor.run()).text_output

    runner = executor.context
    shell_output = runner.shared_state["shell_output"]

    assert "args:alpha beta" in shell_output
    assert "FOO=bar" in shell_output
    assert "Shell body ARG" in result


@pytest.mark.asyncio
async def test_python_command_can_invoke_subcommand(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "commands/driver.py",
        """
        from guildbotics.runtime import Context

        async def main(context: Context):
            await context.invoke("invoked_md", "value")
            return {
                "invoked": context.shared_state.get("invoked_md"),
                "stdin": context.pipe,
            }
        """,
    )
    _write(
        tmp_path / "commands/invoked_md.md",
        """
        ---
        brain: none
        ---
        Placeholder {{1}}
        """,
    )

    context = _get_context()
    executor = machinery(context, "driver", [], tmp_path)
    await executor.run()

    shared = executor.context.shared_state
    assert shared["invoked_md"].startswith("Placeholder value")
    assert shared["driver"]["invoked"] == shared["invoked_md"]
    assert shared["driver"]["stdin"] == shared["invoked_md"]


@pytest.mark.asyncio
async def test_a_ticket_run_drives_its_turn_until_the_host_record_completes(
    tmp_path, monkeypatch
):
    """A completion-managed turn records to the run the host started the
    command for, and does its work: the command names neither."""
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "commands/driver.py",
        """
        from guildbotics.runtime import Context

        async def main(context: Context):
            return await context.invoke(
                "functions/answer",
                agent_execution_context={"max_completion_attempts": 3},
            )
        """,
    )
    # The member completes the run on its second attempt, in the workspace's
    # own run record, which the host ledger reads.
    _write(
        tmp_path / "commands/functions/answer.py",
        """
        from guildbotics.capabilities.task_runs import RunStore

        async def main(context, agent_execution_context):
            attempt = agent_execution_context["attempt"]
            if attempt == 2:
                store = RunStore()
                store.append_evidence(
                    "run-1", "issue_comment", {"url": "https://example.test/1"}
                )
                store.complete(
                    "run-1", "done", "done", "https://example.test/1", "alice"
                )
            return f"attempt-{attempt}"
        """,
    )
    command = prepare_command(_get_context(), "driver", [], None, tmp_path)
    command.context.shared_state[WORKFLOW_INVOCATION_KEY] = WorkflowInvocation(
        "driver",
        "alice",
        "manual",
        "ticket",
        run_id="run-1",
        work=Work.of_ticket("https://example.test/1"),
    )

    outcome = await run_main_command(command, source="manual")

    assert outcome.text_output == "attempt-2"


@pytest.mark.asyncio
async def test_a_command_of_no_workflow_run_drives_no_completion(tmp_path, monkeypatch):
    """A manual command does its own run's manual work, which no completion
    record finishes, so it cannot drive a completion-managed turn."""
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / "commands/driver.py",
        """
        from guildbotics.runtime import Context

        async def main(context: Context):
            return await context.invoke(
                "functions/answer",
                agent_execution_context={"max_completion_attempts": 3},
            )
        """,
    )
    _write(
        tmp_path / "commands/functions/answer.py",
        """
        def main():
            raise AssertionError("no turn runs")
        """,
    )

    with pytest.raises(click.ClickException, match="work_kind"):
        await _run_locally(monkeypatch, _get_context(), "driver", tmp_path)


@pytest.mark.asyncio
async def test_python_command_leaves_no_bytecode_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    # A process started with PYTHONDONTWRITEBYTECODE would hide a regression.
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    _write(
        tmp_path / "commands/functions/cached.py",
        """
        def main():
            return "done"
        """,
    )

    executor = machinery(_get_context(), "functions/cached", [], tmp_path)
    result = (await executor.run()).text_output

    assert result == "done"
    assert not (tmp_path / "commands/functions/__pycache__").exists()
    assert list((tmp_path / "commands").rglob("*.pyc")) == []


@pytest.mark.asyncio
async def test_python_command_named_like_stdlib_keeps_stdlib_import(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    # Restores the real module at teardown, so a regression fails only here
    # instead of breaking every later test in the same worker.
    monkeypatch.setitem(sys.modules, "inspect", inspect)
    _write(
        tmp_path / "commands/inspect.py",
        """
        def main():
            return __name__
        """,
    )

    executor = machinery(_get_context(), "inspect", [], tmp_path)
    outcome = await executor.run()

    assert outcome.result != "inspect"
    assert sys.modules["inspect"] is inspect


@pytest.mark.asyncio
async def test_python_commands_sharing_a_stem_get_distinct_modules(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    for directory in ("alpha", "beta"):
        _write(
            tmp_path / f"commands/{directory}/tool.py",
            """
            def main():
                return __name__
            """,
        )

    names = [
        (
            await machinery(_get_context(), f"{directory}/tool", [], tmp_path).run()
        ).result
        for directory in ("alpha", "beta")
    ]

    assert names[0] != names[1]


@pytest.mark.asyncio
async def test_python_command_supports_dataclass_with_postponed_annotations(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))
    # dataclasses resolves string annotations through sys.modules, so this
    # pins that the command module stays registered there.
    _write(
        tmp_path / "commands/record.py",
        """
        from __future__ import annotations

        from dataclasses import dataclass

        @dataclass
        class Item:
            name: str

        def main():
            return Item("ok").name
        """,
    )

    executor = machinery(_get_context(), "record", [], tmp_path)

    assert (await executor.run()).text_output == "ok"
