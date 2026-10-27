import importlib
import json
import logging
import os
from contextlib import ExitStack
from datetime import UTC, datetime
from types import SimpleNamespace

import click
import httpx
import pytest
from click.testing import CliRunner

from guildbotics.capabilities import member_activity_events
from guildbotics.capabilities.member_memory_audit import MemoryAuditStore
from guildbotics.capabilities.member_reference import command_summaries
from guildbotics.capabilities.task_runs import TaskRunStore
from guildbotics.drivers.execution import ExecutionCoordinator, WorkRejectedError
from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.factory import ServiceIntegrationFactory
from guildbotics.integrations.github import async_client
from guildbotics.integrations.local.identity import username
from guildbotics.integrations.local import chat
from guildbotics.observability.activity_event_store import ActivityEventStore
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.observability.diagnostics_store import DiagnosticsStore
from guildbotics.observability.interactive_sessions import (
    InteractiveSessionStore,
    InteractiveTraceSession,
)
from guildbotics.runtime.member_invocation import (
    ChatSubject,
    MemberInvocation,
    Work,
    member_invocation_scope,
)
from guildbotics.runtime.code_hosting_service import TicketLink
from guildbotics.runtime.person_lease import PersonExecutionLease
from guildbotics.runtime.port import ServiceError
from guildbotics.sync.local_repository import LocalSyncRepository
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.workspace_state import (
    GUILDBOTICS_CONFIG_DIR,
    write_active_workspace,
)
from tests.guildbotics.local_chat import at, chat_event, chat_team, lines, say

member_module = importlib.import_module("guildbotics.cli.member")


def test_member_command_reports_pending_when_one_shot_lock_is_busy(monkeypatch, capsys):
    monkeypatch.setattr(member_module, "_member_command_writes", lambda: True)
    calls = []

    class PreparedSync:
        def commit_and_push_once(self, *, timeout):
            calls.append("sync")
            raise member_module.SyncRepositoryBusyError("sync.lock is busy")

    def prepare():
        calls.append("prepare")
        return PreparedSync()

    monkeypatch.setattr(member_module, "prepare_commit_and_push_once", prepare)

    async def command_result():
        calls.append("command")
        return {"doc_id": "doc-1"}

    result = member_module._run(command_result(), output_format="json")

    assert result == {"doc_id": "doc-1", "sync": "pending"}
    assert calls == ["prepare", "command", "sync"]
    assert json.loads(capsys.readouterr().out) == result


@pytest.mark.parametrize("invalid_identity", ["device", "workspace"])
def test_member_write_validates_sync_identity_before_running_command(
    tmp_path, invalid_identity
):
    repository = LocalSyncRepository(tmp_path)
    repository.initialize()
    repository.set_remote(str(tmp_path / "hub.git"))
    ignore = tmp_path / ".guildbotics" / ".gitignore"
    ignore.write_text("stale rules\n", encoding="utf-8")
    if invalid_identity == "device":
        identity = tmp_path / "home" / ".guildbotics" / "data" / "device.json"
    else:
        identity = tmp_path / ".guildbotics" / "state" / "workspace.json"
    identity.parent.mkdir(parents=True, exist_ok=True)
    identity.write_text('{"device_id": "not a uuid"}', encoding="utf-8")
    marker = tmp_path / "command-ran"

    @click.command()
    def write_command():
        async def write():
            marker.write_text("changed", encoding="utf-8")
            return {"written": True}

        member_module._run(write(), output_format="json")

    result = CliRunner().invoke(write_command)

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert (
        result.output
        == "Error: "
        + member_module.t("cli.member.sync.invalid_identity", path=identity)
        + "\n"
    )
    assert not marker.exists()
    assert ignore.read_text(encoding="utf-8") == "stale rules\n"


def test_member_write_without_sync_runs_without_a_one_shot_result(capsys):
    async def command_result():
        return {"written": True}

    result = member_module._run(command_result(), output_format="json")

    assert result == {"written": True}
    assert json.loads(capsys.readouterr().out) == result


def test_read_only_member_command_skips_one_shot_sync(monkeypatch):
    def prepare():
        pytest.fail("read-only commands must not prepare one-shot sync")

    monkeypatch.setattr(member_module, "prepare_commit_and_push_once", prepare)

    @click.command()
    @member_module._read_only_member_command
    def read_command():
        async def read():
            return {"read": True}

        member_module._run(read(), output_format="json")

    result = CliRunner().invoke(read_command)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"read": True}


@pytest.mark.parametrize("source", ["routine", "scheduled", "manual", "event_queue"])
def test_interactive_write_and_sync_run_while_the_members_clone_is_in_use(
    monkeypatch, source
):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    monkeypatch.setattr(
        member_module,
        "resolve_member_context",
        lambda _: (FakeContext(person), person),
    )
    calls = []

    class PreparedSync:
        def commit_and_push_once(self, *, timeout):
            calls.append("sync")
            return SimpleNamespace(failure=None)

    def prepare():
        calls.append("prepare")
        return PreparedSync()

    monkeypatch.setattr(member_module, "prepare_commit_and_push_once", prepare)
    coordinator = ExecutionCoordinator()
    with coordinator.track_work(source=source, person_id="aiko", command="workflow"):
        result = CliRunner().invoke(
            member_module.member,
            [
                "memory",
                "record",
                "--person",
                "aiko",
                "--scope",
                "personal",
                "--title",
                "Concurrent note",
                "--content-stdin",
            ],
            input="Saved while the workflow uses the clone.\n",
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["doc_id"]
        # The interactive command neither releases nor replaces the run's lease.
        with pytest.raises(WorkRejectedError) as excinfo:
            with coordinator.track_work(
                source="manual", person_id="aiko", command="another workflow"
            ):
                pass
        assert excinfo.value.reason == "lease_unavailable"

    assert calls == ["prepare", "sync"]


@pytest.fixture
def bind_invocation():
    """Bind a member invocation for the rest of the test, as the broker does,
    releasing the lease it carries when the test ends."""
    with ExitStack() as stack:

        def bind(**fields) -> None:
            # A workflow run's invocation carries its work, as its host
            # settled it: a chat run's event, or a task run's ticket.
            if fields.get("run_id"):
                fields.setdefault("work", CHAT_WORK)
            elif fields.get("task_run_id"):
                fields.setdefault("work", TICKET_WORK)
            if fields.get("lease") is not None:
                stack.callback(fields["lease"].release)
            stack.enter_context(member_invocation_scope(MemberInvocation(**fields)))

        yield bind


CHAT_WORK = Work.of_chat(ChatSubject("slack", "C1", "100.1", "E1", "U_BOT"))
TICKET_WORK = Work.of_ticket("https://github.com/owner/repo/issues/1")


def _bind_workflow(bind_invocation, person_id="aiko", **run_ids):
    """Run as a workflow turn of ``person_id`` that holds the person's lease."""
    lease = PersonExecutionLease(person_id)
    lease.acquire(source="routine", command="test", work_id="work-1")
    bind_invocation(lease=lease, **run_ids)
    return lease


class FakeContext:
    def __init__(self, person):
        self.person = person
        self.team = Team(project=Project(name="demo"), members=[person])
        self.logger = None
        #: The capabilities' service of the command (``_fake_capabilities``).
        self.service = None
        self.tickets = None
        self.closed = False

    def clone_for(self, person):
        return FakeContext(person)

    def get_code_hosting_service(self):
        """The configured code host, as a real context makes it."""
        if self.service is None:
            self.service = ServiceIntegrationFactory().create_code_hosting_service(
                logging.getLogger(__name__), self.person, self.team
            )
        return self.service

    def get_ticket_service(self):
        """The configured ticket service, as a real context makes it."""
        if self.tickets is None:
            self.tickets = ServiceIntegrationFactory().create_ticket_service(
                logging.getLogger(__name__), self.person, self.team
            )
        return self.tickets

    async def aclose(self):
        self.closed = True
        for service in (self.service, self.tickets):
            if service is not None and hasattr(service, "aclose"):
                await service.aclose()


#: The code-host and ticket capabilities the member CLI runs.
_CODE_HOST_CAPABILITIES = (
    "ticket_comment",
    "ticket_create",
    "ticket_update",
    "ticket_reaction",
    "pr_create",
    "pr_update",
    "pr_comment",
    "pr_review",
    "pr_review_comment",
    "pr_reply",
    "pr_reaction",
    "artifact_download",
    "task_completion_readiness",
    "await_checks",
)


def _fake_capabilities(monkeypatch, service_type):
    """Run the CLI's code-host capabilities on ``service_type``: each command
    is given one, which closing the command's context closes."""

    def capability(name):
        async def run(context, *args, **kwargs):
            context.service = service_type(context.person, context.team)
            return await getattr(context.service, name)(*args, **kwargs)

        return run

    for name in _CODE_HOST_CAPABILITIES:
        monkeypatch.setattr(member_module.member_repository, name, capability(name))

    async def member_context(context, check_credentials=False):
        context.service = service_type(context.person, context.team)
        return await context.service.context(check_credentials=check_credentials)

    monkeypatch.setattr(member_module, "member_context", member_context)


def _use_real_member_resolution(monkeypatch, *members):
    """Run member commands through the real person resolution for a fake team."""
    from guildbotics.drivers import context as context_module

    team = Team(project=Project(name="demo"), members=list(members))
    base_context = FakeContext(members[0])
    base_context.team = team

    def create_context(message: str = ""):
        return base_context

    monkeypatch.setattr(context_module, "create_context", create_context)


def _files_containing(root, needle):
    """Return files under root whose text contains needle."""
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and needle in path.read_text(errors="ignore")
    ]


def _domain_event_records(*fields):
    """Return recorded events without the member CLI's own command lifecycle.

    The interactive session attributes carried by every member CLI event depend
    on the host environment, so they are dropped as well.
    """
    return [
        {field: _domain_event_field(record, field) for field in fields}
        for record in ActivityEventStore().records_between(
            datetime(1970, 1, 1, tzinfo=UTC), datetime(9999, 1, 1, tzinfo=UTC)
        )
        if not record["type"].startswith("member.command.")
    ]


def _domain_event_field(record, field):
    value = record[field]
    if field != "attributes":
        return value
    return {
        key: item for key, item in value.items() if not key.startswith("interactive.")
    }


@pytest.fixture(autouse=True)
def _clear_workspace_markers(monkeypatch):
    monkeypatch.delenv("CWD_ONLY_MARKER", raising=False)
    monkeypatch.delenv("WORKSPACE_MARKER", raising=False)


def test_member_context_outputs_no_secret(monkeypatch):
    person = Person(
        person_id="aiko",
        name="Aiko",
        person_type="agent",
        profile={"bio": "developer"},
        account_info={"github_username": "aiko-gh"},
    )

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    # Seed a real per-person secret value; context output must never include it.
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "super-secret-sentinel-value")
    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    runner = CliRunner()

    result = runner.invoke(
        member_module.member, ["context", "--person", "aiko", "--format", "json"]
    )

    assert result.exit_code == 0
    assert '"person_id": "aiko"' in result.output
    assert '"credential_status": "unchecked"' in result.output
    assert "super-secret-sentinel-value" not in result.output


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["context", "--person", "hana"], id="context"),
        pytest.param(
            [
                "git",
                "prepare",
                "--person",
                "hana",
                "--repo",
                "owner/repo",
                "--branch",
                "feature",
            ],
            id="git",
        ),
        pytest.param(
            [
                "ticket",
                "comment",
                "--content-stdin",
                "--person",
                "hana",
                "--url",
                "https://github.com/owner/repo/issues/1",
            ],
            id="ticket",
        ),
        pytest.param(["chat", "identity", "--person", "hana"], id="chat"),
        pytest.param(
            ["memory", "recall", "--person", "hana", "--query", "note"], id="memory"
        ),
    ],
)
def test_member_commands_reject_human_member(monkeypatch, argv):
    _use_real_member_resolution(
        monkeypatch,
        Person(person_id="hana", name="Hana", person_type="human"),
    )

    result = CliRunner().invoke(member_module.member, argv, input="Body")

    assert result.exit_code != 0
    assert "Human member 'hana' cannot be used as an AI execution subject" in (
        result.output
    )


def test_member_memory_record_rejects_human_member_without_writing(
    monkeypatch, tmp_path
):
    _use_real_member_resolution(
        monkeypatch,
        Person(person_id="hana", name="Hana", person_type="human"),
    )

    result = CliRunner().invoke(
        member_module.member,
        [
            "memory",
            "record",
            "--person",
            "hana",
            "--scope",
            "personal",
            "--title",
            "Should not be stored",
            "--content-stdin",
        ],
        input="body\n",
    )

    assert result.exit_code != 0
    assert "cannot be used as an AI execution subject" in result.output
    assert _files_containing(tmp_path, "Should not be stored") == []


def test_member_help_prints_capability_reference():
    runner = CliRunner()

    result = runner.invoke(member_module.member, ["help"])

    assert result.exit_code == 0
    assert "guildbotics member git commit" in result.output
    assert "guildbotics member chat reply" in result.output
    assert "guildbotics member memory recall" in result.output
    assert "### Rules" in result.output
    assert "#### Handling review feedback" in result.output
    assert "#### レビュー指摘への対応" in result.output


def test_member_command_write_classification_uses_callback_metadata() -> None:
    @click.command(name="context")
    def unmarked_command() -> None:
        """A write-capable command whose name resembles a read-only command."""

    @click.command(name="write-looking-command")
    @member_module._read_only_member_command
    def read_only_command() -> None:
        """A read-only command whose name does not imply its access mode."""

    with click.Context(unmarked_command):
        assert member_module._member_command_writes() is True
    with click.Context(read_only_command):
        assert member_module._member_command_writes() is False


_READ_GROUPS = {"ticket": "ticket", "pull_request": "pr", "repository": "repo-info"}


def test_ci_inspection_commands_are_read_only() -> None:
    commands = member_module.member.commands

    for command in (
        *(commands[group].commands["read"] for group in _READ_GROUPS.values()),
        commands["pr"].commands["artifact"].commands["download"],
        commands["ticket"].commands["attachment"].commands["download"],
    ):
        with click.Context(command):
            assert member_module._member_command_writes() is False


def test_inspection_cli_leaves_are_removed():
    member = member_module.member
    assert "inspect" not in member.commands["ticket"].commands
    assert not {"inspect", "checks"} & member.commands["pr"].commands.keys()


@pytest.mark.parametrize(("subject", "group"), _READ_GROUPS.items())
def test_each_read_reads_only_its_subjects_resources(subject, group):
    """``ticket read`` reads a ticket, ``pr read`` a pull request, and
    ``repo-info read`` the repository: every resource is read in one place."""
    from guildbotics.runtime.resources import RESOURCES_BY_SUBJECT

    read = member_module.member.commands[group].commands["read"]
    [resource] = [option for option in read.params if option.name == "resource"]
    assert set(resource.type.choices) == set(RESOURCES_BY_SUBJECT[subject])
    elsewhere = next(
        name
        for other, resources in RESOURCES_BY_SUBJECT.items()
        if other != subject
        for name in resources
    )
    result = CliRunner().invoke(
        member_module.member,
        [
            group,
            "read",
            "--person",
            "aiko",
            "--resource",
            elsewhere,
            "--subject",
            "o/r",
        ],
    )
    assert result.exit_code != 0
    assert "Invalid value for '--resource'" in result.output


def _command_paths(group: click.Group, prefix: tuple[str, ...] = ()):
    for name, command in group.commands.items():
        yield (*prefix, name)
        if isinstance(command, click.Group):
            yield from _command_paths(command, (*prefix, name))


def test_member_commands_are_named_by_kind_not_provider():
    """Agents call the member CLI by what they act on (ticket, pr, repo-info,
    chat), whichever provider the workspace configures for it."""
    from guildbotics.integrations.factory import PROVIDERS

    names = {name for path in _command_paths(member_module.member) for name in path}
    assert not names & PROVIDERS.keys()
    assert {"ticket", "pr", "repo-info", "chat"} <= member_module.member.commands.keys()
    assert "repository" not in member_module.member.commands


def test_pr_read_records_host_target_in_workflow_trace(monkeypatch, bind_invocation):
    from guildbotics.runtime.resources import ReadPage, WorkTarget

    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    bind_invocation(run_id="run-1", trace_id="trace-parent")
    url = "https://github.com/owner/repo/pull/544"
    target = {
        "kind": "pull_request",
        "url": url,
        "title": "Host observed title",
        "label": "#544",
    }
    reads = []

    class Service:
        async def read(self, *args, **kwargs):
            reads.append((args, kwargs))
            return ReadPage(
                items=[{"title": target["title"]}],
                target=WorkTarget.model_validate(target),
            )

        async def aclose(self):
            pass

    context = FakeContext(person)
    context.service = Service()
    monkeypatch.setattr(member_module, "_resolve", lambda _: (context, person))
    result = CliRunner().invoke(
        member_module.member,
        [
            "pr",
            "read",
            "--person",
            "aiko",
            "--resource",
            "pull_requests",
            "--subject",
            url,
            "--params",
            '{"page_size": 5}',
            "--continuation",
            "next",
        ],
    )
    assert result.exit_code == 0, result.output
    # The subject is read as the item's URL, with the conditions and
    # continuation as given.
    assert reads == [
        (
            ("pull_requests", url),
            {"parameters": {"page_size": 5}, "continuation": "next"},
        )
    ]
    summary = DiagnosticsStore().get_summary("trace-parent")
    assert summary["title"] == target["title"]
    records = DiagnosticsStore().get_records("trace-parent")
    assert [record["type"] for record in records] == ["target.recorded"]
    [(action, attributes)] = _work_target_records()
    assert action == "inspected"
    assert (attributes["target.label"], attributes["target.url"]) == ("#544", url)
    assert _domain_event_records("type") == []


def test_workflow_member_write_rejects_missing_delegation(
    monkeypatch, bind_invocation
) -> None:
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    bind_invocation(run_id="run-1")
    monkeypatch.setattr(
        member_module,
        "resolve_member_context",
        lambda _identifier: (FakeContext(person), person),
    )

    result = CliRunner().invoke(
        member_module.member,
        ["chat", "noop", "--person", "aiko", "--content-stdin"],
        input="Nothing to do.",
    )

    assert result.exit_code != 0
    assert member_module.t("cli.member.lease.invalid_delegation") in result.output


def test_member_memory_record_and_recall_cli(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    runner = CliRunner()

    record = runner.invoke(
        member_module.member,
        [
            "memory",
            "record",
            "--person",
            "aiko",
            "--scope",
            "personal",
            "--title",
            "Retry note",
            "--summary",
            "Retry summary",
            "--keyword",
            "retry",
            "--thread",
            "https://chat.test/t/1",
            "--ticket",
            " https://example.test/issues/1 ",
            "--ticket",
            "  ",
            "--pr",
            "https://example.test/pull/2",
            "--channel",
            "https://chat.test/c/1",
            "--content-stdin",
        ],
        input="Retry after refresh.\n",
    )

    assert record.exit_code == 0
    doc_id = json.loads(record.output)["doc_id"]

    recall = runner.invoke(
        member_module.member,
        [
            "memory",
            "recall",
            "--person",
            "aiko",
            "--query",
            "https://example.test/issues/1",
            "--meta-only",
        ],
    )

    assert recall.exit_code == 0
    assert json.loads(recall.output)["results"][0]["doc_id"] == doc_id

    def get(*flags: str):
        return runner.invoke(
            member_module.member,
            ["memory", "get", "--person", "aiko", "--id", doc_id, *flags],
        )

    team, either = get("--team"), get()

    assert team.exit_code != 0
    assert f"Memory document not found: {doc_id}" in team.output
    assert either.exit_code == 0
    assert json.loads(either.output)["meta"]["source"] == [
        {"type": "ticket", "url": "https://example.test/issues/1"},
        {"type": "pr", "url": "https://example.test/pull/2"},
        {"type": "channel", "url": "https://chat.test/c/1"},
        {"type": "thread", "url": "https://chat.test/t/1"},
    ]


def test_member_memory_update_reads_stdin_only_when_requested(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    runner = CliRunner()
    record = runner.invoke(
        member_module.member,
        [
            "memory",
            "record",
            "--person",
            "aiko",
            "--title",
            "Original title",
            "--content-stdin",
        ],
        input="Original body\n",
    )
    doc_id = json.loads(record.output)["doc_id"]

    metadata_only = runner.invoke(
        member_module.member,
        [
            "memory",
            "update",
            "--person",
            "aiko",
            "--id",
            doc_id,
            "--title",
            "Updated title",
        ],
        input="This must not be consumed.",
    )
    body_update = runner.invoke(
        member_module.member,
        [
            "memory",
            "update",
            "--person",
            "aiko",
            "--id",
            doc_id,
            "--content-stdin",
        ],
        input="Updated body\n",
    )
    fetched = runner.invoke(
        member_module.member,
        [
            "memory",
            "get",
            "--person",
            "aiko",
            "--id",
            doc_id,
        ],
    )

    assert record.exit_code == 0
    assert metadata_only.exit_code == 0
    assert body_update.exit_code == 0
    assert fetched.exit_code == 0
    payload = json.loads(fetched.output)
    assert payload["title"] == "Updated title"
    assert payload["body"] == "Updated body\n"


def test_interactive_session_record_keeps_the_targets_its_commands_named(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        member_module.InteractiveTraceStore, "touch", lambda self, session: None
    )
    session = InteractiveTraceSession(
        trace_id="trace-target",
        person_id="aiko",
        workspace=str(tmp_path),
        host="codex",
        thread_key="thread-1",
        started_at="2026-07-01T10:00:00+00:00",
        last_seen_at="2026-07-01T10:00:00+00:00",
        expires_at="2026-07-01T10:30:00+00:00",
    )

    async def inspect_pr():
        record_correlated_event(
            event_type="target.recorded",
            payload={},
            attributes={
                "target.action": "inspected",
                "target.kind": "pull_request",
                "target.title": "利用枠の表示",
                "interactive.host": "codex",
            },
        )
        return {}

    async def failing():
        raise RuntimeError("boom")

    member_module._run_interactive(inspect_pr(), session, "member pr read")
    with pytest.raises(RuntimeError):
        member_module._run_interactive(failing(), session, "member git push")

    record = json.loads(
        (tmp_path / ".guildbotics/state/sessions/trace-target.json").read_text(
            encoding="utf-8"
        )
    )
    assert record["command"] == "member pr read"
    assert record["status"] == "failed"
    # Only the targets are shared: the session's host attributes describe
    # this machine.
    assert record["attributes"] == {
        "target.action": "inspected",
        "target.kind": "pull_request",
        "target.title": "利用枠の表示",
    }


def test_interrupted_interactive_command_records_its_end(monkeypatch):
    # Ctrl-C ends the command. ``KeyboardInterrupt`` is not an ``Exception``,
    # so a boundary that only caught ``Exception`` left
    # ``member.command.started`` as the session's last record and the run read
    # as still going.
    recorded: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        member_module,
        "_record_member_command_event",
        lambda event_type, command, payload=None: recorded.append(
            (event_type, command, payload or {})
        ),
    )
    monkeypatch.setattr(
        member_module.InteractiveTraceStore, "touch", lambda self, session: None
    )
    session = InteractiveTraceSession(
        trace_id="trace-1",
        person_id="aiko",
        workspace="workspace-1",
        host="codex",
        thread_key="thread-1",
        started_at="",
        last_seen_at="",
        expires_at="",
    )

    async def interrupted():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        member_module._run_interactive(interrupted(), session, "member memory recall")

    assert [event_type for event_type, _command, _payload in recorded] == [
        "member.command.started",
        "member.command.failed",
    ]
    assert recorded[-1][2] == {"error_type": "KeyboardInterrupt", "code": "cancelled"}


def test_member_cli_reuses_trace_for_interactive_session(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-1")
    person = Person(person_id="aiko", name="Aiko", person_type="agent")

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    runner = CliRunner()

    record = runner.invoke(
        member_module.member,
        [
            "--workspace",
            str(tmp_path),
            "memory",
            "record",
            "--person",
            "aiko",
            "--scope",
            "personal",
            "--title",
            "Trace note",
            "--summary",
            "Trace summary",
            "--keyword",
            "trace",
            "--content-stdin",
        ],
        input="Trace body.\n",
    )
    assert record.exit_code == 0

    recall = runner.invoke(
        member_module.member,
        [
            "--workspace",
            str(tmp_path),
            "memory",
            "recall",
            "--person",
            "aiko",
            "--query",
            "trace",
            "--meta-only",
        ],
    )
    assert recall.exit_code == 0

    traces = DiagnosticsStore().list_traces(source="interactive")
    assert len(traces) == 1
    trace_id = traces[0]["trace_id"]
    records = DiagnosticsStore().get_records(trace_id)
    events = [item for item in records if item.get("kind") == "event"]
    assert [item["type"] for item in events] == [
        "member.command.started",
        "member.command.finished",
        "member.command.started",
        "member.command.finished",
    ]
    assert {item["command"] for item in events} == {
        "member memory recall",
        "member memory record",
    }
    memory_events = MemoryAuditStore().list_events(trace_id=trace_id)
    assert {event["type"] for event in memory_events} == {
        "memory.recall",
        "memory.record",
    }
    # The command boundary stays local; the session itself is one shared
    # record, rewritten by each command, that other devices read instead.
    assert [record["type"] for record in _domain_event_records("type")] == []
    sessions = InteractiveSessionStore().list_between(
        datetime(1970, 1, 1, tzinfo=UTC), datetime(9999, 1, 1, tzinfo=UTC)
    )
    assert [item["trace_id"] for item in sessions] == [trace_id]
    assert sessions[0]["command"] == "member memory record"
    assert sessions[0]["status"] == "success"
    assert sessions[0]["person_id"] == "aiko"
    assert sessions[0]["attributes"] == {}


def test_member_context_markdown_renders_capabilities_section(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")

    def fake_resolve_member_context(identifier):
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def context(self, check_credentials=False):
            return {
                "person_id": "aiko",
                "capabilities": "### Repository\n- `guildbotics member pr create ...`",
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    _fake_capabilities(monkeypatch, FakeService)
    runner = CliRunner()

    result = runner.invoke(member_module.member, ["context", "--person", "aiko"])

    assert result.exit_code == 0
    assert "## Member Capabilities" in result.output
    assert "guildbotics member pr create" in result.output


def test_member_context_markdown_highlights_communication_style(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def context(self, check_credentials=False):
            assert check_credentials is False
            return {
                "person_id": "aiko",
                "communication_style": {
                    "active_member_instruction": "Treat Aiko as active.",
                    "interactive_replies": "Reply as Aiko.",
                    "comments": "Comment as Aiko.",
                    "neutral_documents": "Use neutral documents.",
                    "machine_outputs": "Keep JSON factual.",
                },
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    _fake_capabilities(monkeypatch, FakeService)
    runner = CliRunner()

    result = runner.invoke(member_module.member, ["context", "--person", "aiko"])

    assert result.exit_code == 0
    assert "## Communication Style" in result.output
    assert "Treat Aiko as active." in result.output
    assert "Keep JSON factual." in result.output


def test_markdown_renders_failed_logs_as_multiline_code_blocks():
    rendered = member_module._to_markdown(
        {
            "rollup": "failure",
            "failed_logs": [
                {
                    "name": "test",
                    "conclusion": "failure",
                    "run_id": 9,
                    "log": "first line\nsecond line\n",
                }
            ],
        }
    )

    assert "## Failed job logs" in rendered
    assert "### test (failure)" in rendered
    assert "```text\nfirst line\nsecond line\n```" in rendered
    assert "first line\\nsecond line" not in rendered


def test_member_context_uses_active_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv(GUILDBOTICS_WORKSPACE_ROOT, raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_active_workspace(workspace)

    person = Person(person_id="aiko", name="Aiko", person_type="agent")

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        assert os.environ[GUILDBOTICS_CONFIG_DIR] == str(
            workspace.resolve() / ".guildbotics" / "config"
        )
        return FakeContext(person), person

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    runner = CliRunner()

    result = runner.invoke(
        member_module.member, ["context", "--person", "aiko", "--format", "json"]
    )

    assert result.exit_code == 0


def test_member_context_workspace_option_overrides_active_workspace(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    active_workspace = tmp_path / "active"
    active_workspace.mkdir()
    explicit_workspace = tmp_path / "explicit"
    explicit_workspace.mkdir()
    write_active_workspace(active_workspace)

    person = Person(person_id="aiko", name="Aiko", person_type="agent")

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        assert os.environ[GUILDBOTICS_CONFIG_DIR] == str(
            explicit_workspace.resolve() / ".guildbotics" / "config"
        )
        return FakeContext(person), person

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "--workspace",
            str(explicit_workspace),
            "context",
            "--person",
            "aiko",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0


def test_member_workspace_without_env_does_not_load_cwd_env(monkeypatch, tmp_path):
    monkeypatch.delenv("CWD_ONLY_MARKER", raising=False)
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / ".env").write_text("CWD_ONLY_MARKER=leaked\n", encoding="utf-8")
    monkeypatch.chdir(caller)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    person = Person(person_id="aiko", name="Aiko", person_type="agent")

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def context(self, check_credentials=False):
            return {"person_id": "aiko"}

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    _fake_capabilities(monkeypatch, FakeService)

    result = CliRunner().invoke(
        member_module.member,
        [
            "--workspace",
            str(workspace),
            "context",
            "--person",
            "aiko",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert "CWD_ONLY_MARKER" not in os.environ


def test_member_active_workspace_without_env_does_not_load_cwd_env(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("CWD_ONLY_MARKER", raising=False)
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / ".env").write_text("CWD_ONLY_MARKER=leaked\n", encoding="utf-8")
    monkeypatch.chdir(caller)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_active_workspace(workspace)
    person = Person(person_id="aiko", name="Aiko", person_type="agent")

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def context(self, check_credentials=False):
            return {"person_id": "aiko"}

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    _fake_capabilities(monkeypatch, FakeService)

    result = CliRunner().invoke(
        member_module.member, ["context", "--person", "aiko", "--format", "json"]
    )

    assert result.exit_code == 0
    assert "CWD_ONLY_MARKER" not in os.environ


def test_member_context_check_credentials_fail_closed(monkeypatch):
    """A credential the service does not accept fails the check with the
    service's own words, which name no secret value and are shown as they
    are."""
    person = Person(
        person_id="aiko",
        name="Aiko",
        person_type="agent",
        account_info={"github_username": "aiko-gh"},
    )

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def context(self, check_credentials=False):
            assert check_credentials is True
            raise ServiceError("Set AIKO_GITHUB_ACCESS_TOKEN for the member.")

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    _fake_capabilities(monkeypatch, FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        ["context", "--person", "aiko", "--check-credentials", "--format", "json"],
    )

    assert result.exit_code != 0
    assert result.output == "Error: Set AIKO_GITHUB_ACCESS_TOKEN for the member.\n"


def test_member_write_command_requires_content_source():
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "ticket",
            "comment",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/issues/1",
        ],
    )

    assert result.exit_code != 0
    assert "--content-stdin or --content-file" in result.output


CONTENT_COMMANDS = (
    "memory record --person aiko --title Title",
    "chat post --person aiko --channel-id C1",
    "chat reply --person aiko --channel-id C1 --thread-id 100.1",
    "chat noop --person aiko",
    "chat complete --person aiko --status done",
    "git commit --person aiko --repo-path .",
    "git publish --person aiko --repo-path .",
    "ticket comment --person aiko --url https://github.com/owner/repo/issues/1",
    "ticket create --person aiko --tracker owner/repo --title Title",
    "pr create --person aiko --repo owner/repo --head feature --title Title",
    "pr comment --person aiko --url https://github.com/owner/repo/pull/1",
    "pr review --person aiko --url https://github.com/owner/repo/pull/1 "
    "--event comment --head abc123",
    "pr review-comment --person aiko --url https://github.com/owner/repo/pull/1 "
    "--head abc123 --path file.py --line 1",
    "pr reply --person aiko --url https://github.com/owner/repo/pull/1 "
    "--reply-target-id 1",
    "task complete --person aiko --status done",
)


@pytest.mark.parametrize("command", CONTENT_COMMANDS)
def test_required_content_commands_reject_empty_stdin(command, bind_invocation):
    if command.startswith("task "):
        _bind_workflow(bind_invocation, task_run_id="run-1")
    else:
        _bind_workflow(bind_invocation, run_id="run-1", task_run_id="run-1")
    result = CliRunner().invoke(
        member_module.member,
        [*command.split(), "--content-stdin"],
        input="",
    )

    assert result.exit_code != 0
    assert "must not be empty" in result.output


@pytest.mark.parametrize(
    "command",
    [
        "memory record",
        "memory update",
        "chat post",
        "chat reply",
        "chat noop",
        "chat complete",
        "git commit",
        "git publish",
        "ticket comment",
        "ticket create",
        "ticket update",
        "pr create",
        "pr update",
        "pr comment",
        "pr review-comment",
        "pr reply",
        "task complete",
    ],
)
def test_content_command_help_exposes_shared_content_sources(command):
    result = CliRunner().invoke(member_module.member, [*command.split(), "--help"])

    assert result.exit_code == 0
    assert "--content-stdin" in result.output
    assert "--content-file" in result.output
    for removed in (
        "--title-file",
        "--body-file",
        "--body-stdin",
        "--message-file",
        "--message-stdin",
        "--reason-file",
        "--summary-file",
    ):
        assert removed not in result.output


def test_required_content_option_reads_utf8_file(tmp_path):
    content_file = tmp_path / "message.txt"
    content_file.write_text("日本語\n$ `value`\n", encoding="utf-8")

    @click.command()
    @member_module._required_content_stdin_option
    def command():
        click.echo(member_module._read_stdin("body"), nl=False)

    result = CliRunner().invoke(
        command,
        ["--content-file", str(content_file)],
    )

    assert result.exit_code == 0
    assert result.output == "日本語\n$ `value`\n"


def test_content_sources_are_mutually_exclusive(tmp_path):
    content_file = tmp_path / "message.txt"
    content_file.write_text("body", encoding="utf-8")

    @click.command()
    @member_module._required_content_stdin_option
    def command():
        pass

    result = CliRunner().invoke(
        command,
        ["--content-stdin", "--content-file", str(content_file)],
        input="stdin",
    )

    assert result.exit_code != 0
    assert "exactly one" in result.output


def test_content_file_must_exist(tmp_path):
    @click.command()
    @member_module._required_content_stdin_option
    def command():
        pass

    result = CliRunner().invoke(
        command,
        ["--content-file", str(tmp_path / "missing.txt")],
    )

    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_content_file_must_be_utf8(tmp_path):
    content_file = tmp_path / "message.txt"
    content_file.write_bytes(b"\xff\xfe")

    @click.command()
    @member_module._required_content_stdin_option
    def command():
        pass

    result = CliRunner().invoke(
        command,
        ["--content-file", str(content_file)],
    )

    assert result.exit_code != 0
    assert "valid UTF-8" in result.output


def test_content_file_read_failure_is_reported(monkeypatch, tmp_path):
    content_file = tmp_path / "message.txt"
    content_file.write_text("body", encoding="utf-8")
    real_read_text = member_module.Path.read_text

    def read_text(path, *args, **kwargs):
        if path == content_file:
            raise OSError("read failed")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(member_module.Path, "read_text", read_text)

    @click.command()
    @member_module._required_content_stdin_option
    def command():
        pass

    result = CliRunner().invoke(
        command,
        ["--content-file", str(content_file)],
    )

    assert result.exit_code != 0
    assert "Could not read content file" in result.output


@pytest.mark.parametrize(
    "command",
    [
        "memory record --person aiko",
        "ticket create --person aiko --tracker owner/repo",
        "pr create --person aiko --repo owner/repo --head feature",
    ],
)
@pytest.mark.parametrize(
    ("title", "error"),
    [("", "title must not be empty"), ("First line\nSecond line", "newlines")],
)
def test_write_command_titles_reject_empty_or_multiline_values(command, title, error):
    result = CliRunner().invoke(
        member_module.member,
        [*command.split(), "--title", title, "--content-stdin"],
        input="Body\n",
    )

    assert result.exit_code != 0
    assert error in result.output


def test_member_issue_commands_pass_content_stdin(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = []
    activity_calls = []

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def ticket_comment(self, ticket_url, body):
            calls.append(("comment", ticket_url, body))
            return {
                "id": "issuecomment-1",
                "url": f"{ticket_url}#issuecomment-1",
                "target": _ticket_target(ticket_url, "#1"),
            }

        async def ticket_create(
            self, tracker, title, body, add_to_board, labels, human_approved
        ):
            calls.append(
                ("create", tracker, title, body, add_to_board, labels, human_approved)
            )
            return {
                "labels": list(labels),
                "target": _ticket_target(
                    "https://github.com/owner/repo/issues/2", "#2", title
                ),
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    _fake_capabilities(monkeypatch, FakeService)
    monkeypatch.setattr(
        member_module,
        "record_member_issue_comment_event",
        lambda member, payload: activity_calls.append(("comment", member, payload)),
    )
    monkeypatch.setattr(
        member_module,
        "record_member_issue_create_event",
        lambda member, payload: activity_calls.append(("create", member, payload)),
    )
    runner = CliRunner()

    comment = runner.invoke(
        member_module.member,
        [
            "ticket",
            "comment",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/issues/1",
            "--content-stdin",
        ],
        input="Comment body\n",
    )
    create = runner.invoke(
        member_module.member,
        [
            "ticket",
            "create",
            "--person",
            "aiko",
            "--tracker",
            "owner/repo",
            "--title",
            "Issue title",
            "--label",
            "priority: high",
            "--no-add-to-board",
            "--human-approved",
            "--content-stdin",
        ],
        input="Issue body\n",
    )

    assert comment.exit_code == 0
    assert create.exit_code == 0
    assert calls == [
        (
            "comment",
            "https://github.com/owner/repo/issues/1",
            "Comment body\n",
        ),
        (
            "create",
            "owner/repo",
            "Issue title",
            "Issue body\n",
            False,
            ("priority: high",),
            True,
        ),
    ]
    assert [call[0] for call in activity_calls] == ["comment", "create"]
    assert all(call[1] is person for call in activity_calls)
    assert activity_calls[0][2]["target"]["url"] == (
        "https://github.com/owner/repo/issues/1"
    )
    assert activity_calls[1][2]["target"]["title"] == "Issue title"


def test_member_issue_api_failures_do_not_record_activity(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    activity_calls = []

    monkeypatch.setattr(
        member_module,
        "resolve_member_context",
        lambda _identifier: (FakeContext(person), person),
    )

    class FakeService:
        def __init__(self, *_args):
            pass

        async def ticket_comment(self, _ticket_url, _body):
            raise ServiceError("GitHub API failed")

        async def ticket_create(
            self, _tracker, _title, _body, _add_to_board, _labels, _human_approved
        ):
            raise ServiceError("GitHub API failed")

        async def aclose(self):
            pass

    _fake_capabilities(monkeypatch, FakeService)
    monkeypatch.setattr(
        member_module,
        "record_member_issue_comment_event",
        lambda *_args: activity_calls.append("comment"),
    )
    monkeypatch.setattr(
        member_module,
        "record_member_issue_create_event",
        lambda *_args: activity_calls.append("create"),
    )
    runner = CliRunner()

    comment = runner.invoke(
        member_module.member,
        [
            "ticket",
            "comment",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/issues/1",
            "--content-stdin",
        ],
        input="Comment body\n",
    )
    create = runner.invoke(
        member_module.member,
        [
            "ticket",
            "create",
            "--person",
            "aiko",
            "--tracker",
            "owner/repo",
            "--title",
            "Issue title",
            "--human-approved",
            "--content-stdin",
        ],
        input="Issue body\n",
    )

    assert comment.exit_code != 0
    assert "GitHub API failed" in comment.output
    assert create.exit_code != 0
    assert "GitHub API failed" in create.output
    assert activity_calls == []


def test_member_write_outside_the_configured_owner_is_never_sent(monkeypatch):
    """#679: a public repository of another owner took an issue in the
    member's name, because the token alone does not stop that."""
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    context = FakeContext(person)
    context.team = Team(
        project=Project(
            name="demo",
            services={
                "code_hosting_service": {"name": "GitHub", "owner": "acme"},
                "ticket_service": {
                    "name": "GitHub",
                    "owner": "acme",
                    "project_id": "1",
                    "url": "https://github.com/orgs/acme/projects/1",
                },
            },
        ),
        members=[person],
    )
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "token")
    monkeypatch.setattr(
        member_module, "resolve_member_context", lambda _identifier: (context, person)
    )
    sent = []
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        async_client.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(
            **kwargs,
            transport=httpx.MockTransport(
                lambda request: sent.append(request) or httpx.Response(201, json={})
            ),
        ),
    )

    result = CliRunner().invoke(
        member_module.member,
        [
            "ticket",
            "create",
            "--person",
            "aiko",
            "--tracker",
            "other-owner/demo",
            "--title",
            "Issue title",
            "--human-approved",
            "--no-add-to-board",
            "--content-stdin",
        ],
        input="Issue body\n",
    )

    assert result.exit_code != 0
    assert "refused POST /repos/other-owner/demo/issues" in result.output
    assert sent == []


def test_member_git_publish_current_mode_uses_current_workspace_service(
    monkeypatch, tmp_path
):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    calls = {}

    context = FakeContext(person)

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return context, person

    class FakeResult:
        def to_dict(self):
            return {
                "repo_path": str(repo_path),
                "branch": "main",
                "commit_sha": "abc",
                "pushed": True,
                "has_changes": True,
                "status": "published",
            }

    class FakeService:
        def __init__(self, *_args):
            pass

        async def publish_current_workspace(self, repo_path, message, cwd):
            calls["repo_path"] = repo_path
            calls["message"] = message
            calls["cwd"] = cwd
            return FakeResult()

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitWorkspaceService", FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "git",
            "publish",
            "--person",
            "aiko",
            "--repo-path",
            str(repo_path),
            "--content-stdin",
            "--workspace-mode",
            "current",
        ],
        input="publish\n",
    )

    assert result.exit_code == 0
    assert calls["repo_path"] == repo_path
    assert calls["message"] == "publish\n"
    assert calls["cwd"].is_absolute()
    assert context.closed
    assert '"status": "published"' in result.output


def test_member_git_commit_current_mode_uses_current_workspace_service(
    monkeypatch, tmp_path
):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    calls = {}

    context = FakeContext(person)

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return context, person

    class FakeResult:
        def to_dict(self):
            return {
                "repo_path": str(repo_path),
                "branch": "main",
                "commit_sha": "abc",
                "has_changes": True,
                "status": "committed",
            }

    class FakeService:
        def __init__(self, *_args):
            pass

        async def commit(self, repo_path, message, workspace_mode, cwd):
            calls["repo_path"] = repo_path
            calls["message"] = message
            calls["workspace_mode"] = workspace_mode
            calls["cwd"] = cwd
            return FakeResult()

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitWorkspaceService", FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "git",
            "commit",
            "--person",
            "aiko",
            "--repo-path",
            str(repo_path),
            "--content-stdin",
            "--workspace-mode",
            "current",
        ],
        input="commit\n",
    )

    assert result.exit_code == 0
    assert calls["repo_path"] == repo_path
    assert calls["message"] == "commit\n"
    assert calls["workspace_mode"] == "current"
    assert calls["cwd"].is_absolute()
    assert context.closed
    assert '"status": "committed"' in result.output


def test_member_git_commit_reads_message_from_stdin(monkeypatch, tmp_path):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeResult:
        def to_dict(self):
            return {
                "repo_path": str(repo_path),
                "branch": "main",
                "commit_sha": "abc",
                "has_changes": True,
                "status": "committed",
            }

    class FakeService:
        def __init__(self, *_args):
            pass

        async def commit(self, repo_path, message, workspace_mode, cwd):
            calls["message"] = message
            return FakeResult()

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitWorkspaceService", FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "git",
            "commit",
            "--person",
            "aiko",
            "--repo-path",
            str(repo_path),
            "--content-stdin",
            "--workspace-mode",
            "current",
        ],
        input="日本語のコミットメッセージ\n",
    )

    assert result.exit_code == 0
    assert calls["message"] == "日本語のコミットメッセージ\n"


def test_member_git_prepare_rejects_missing_anchor():
    runner = CliRunner()

    result = runner.invoke(member_module.member, ["git", "prepare", "--person", "aiko"])

    assert result.exit_code != 0
    assert "Provide --issue-url, --pr-url, or --repo with --branch." in result.output


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        (
            ["--repo", "acme/widget", "--pr-url", "https://github.com/acme/w/pull/1"],
            "--repo cannot be combined with --pr-url.",
        ),
        (
            [
                "--repo",
                "acme/widget",
                "--branch",
                "chat/fix-typo",
                "--issue-url",
                "https://github.com/acme/widget/issues/1",
            ],
            "--branch cannot be combined with --issue-url.",
        ),
        (["--repo", "acme/widget"], "--repo requires --branch or --issue-url."),
    ],
    ids=["repo-and-pr", "branch-and-ticket", "repo-alone"],
)
def test_member_git_prepare_rejects_an_anchor_it_cannot_take(arguments, error):
    result = CliRunner().invoke(
        member_module.member, ["git", "prepare", "--person", "aiko", *arguments]
    )

    assert result.exit_code != 0
    assert error in result.output


def test_member_git_prepare_names_the_repository_of_a_ticket_with_repo(monkeypatch):
    """A ticket that belongs to no repository is prepared in the one --repo
    names: the two are passed on together."""
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    context = FakeContext(person)
    monkeypatch.setattr(
        member_module, "resolve_member_context", lambda _identifier: (context, person)
    )
    calls = []

    class FakeService:
        def __init__(self, *_args):
            pass

        async def prepare(self, **anchor):
            calls.append(anchor)
            return {"repository": anchor["repo"], "branch": "ticket/OPS-7"}

    monkeypatch.setattr(member_module, "_git_service", lambda *_: FakeService())
    ticket_url = "https://tickets.example.com/browse/OPS-7"

    result = CliRunner().invoke(
        member_module.member,
        [
            "git",
            "prepare",
            "--person",
            "aiko",
            "--issue-url",
            ticket_url,
            "--repo",
            "acme/widget",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {"issue_url": ticket_url, "pr_url": None, "repo": "acme/widget", "branch": None}
    ]
    assert context.closed


def test_member_git_prepare_rejects_branch_without_repo():
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        ["git", "prepare", "--person", "aiko", "--branch", "chat/fix-typo"],
    )

    assert result.exit_code != 0
    assert "--branch requires --repo." in result.output


def test_member_git_commit_requires_content_source(tmp_path):
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "git",
            "commit",
            "--person",
            "aiko",
            "--repo-path",
            str(tmp_path),
        ],
    )

    assert result.exit_code != 0
    assert "--content-stdin or --content-file" in result.output


def test_member_git_commit_rejects_removed_message_options(tmp_path):
    runner = CliRunner()
    removed_option = "--message-file"

    result = runner.invoke(
        member_module.member,
        [
            "git",
            "commit",
            "--person",
            "aiko",
            "--repo-path",
            str(tmp_path),
            removed_option,
            "message.txt",
        ],
    )

    assert result.exit_code != 0
    assert f"No such option '{removed_option}'" in result.output


def test_member_git_push_current_mode_uses_current_workspace_service(
    monkeypatch, tmp_path
):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    calls = {}

    context = FakeContext(person)

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return context, person

    class FakeResult:
        def to_dict(self):
            return {
                "repo_path": str(repo_path),
                "branch": "main",
                "pushed": True,
                "status": "pushed",
                "commits": [
                    {
                        "id": "abc1234",
                        "message": "Improve activity",
                        "url": "https://github.com/owner/repo/commit/abc1234",
                    }
                ],
            }

    class FakeService:
        def __init__(self, *_args):
            pass

        async def push(self, repo_path, workspace_mode, cwd):
            calls["repo_path"] = repo_path
            calls["workspace_mode"] = workspace_mode
            calls["cwd"] = cwd
            return FakeResult()

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitWorkspaceService", FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "git",
            "push",
            "--person",
            "aiko",
            "--repo-path",
            str(repo_path),
            "--workspace-mode",
            "current",
        ],
    )

    assert result.exit_code == 0
    assert calls["repo_path"] == repo_path
    assert calls["workspace_mode"] == "current"
    assert calls["cwd"].is_absolute()
    assert context.closed
    assert '"status": "pushed"' in result.output
    assert _domain_event_records("type", "person_id", "payload") == [
        {
            "type": "repository.push",
            "person_id": "aiko",
            "payload": {
                "action": "push",
                "ref": "refs/heads/main",
                "commits": [
                    {
                        "id": "abc1234",
                        "message": "Improve activity",
                        "url": "https://github.com/owner/repo/commit/abc1234",
                    }
                ],
            },
        }
    ]


def test_member_git_push_rejected_by_remote_exits_non_zero(monkeypatch, tmp_path):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    def fake_resolve_member_context(identifier):
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def push(self, repo_path, workspace_mode, cwd):
            raise member_module.MemberCapabilityError(
                "Failed to push 'ticket/371' to origin: "
                "[rejected] (fetch first, non-fast-forward)"
            )

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitWorkspaceService", FakeService)

    result = CliRunner().invoke(
        member_module.member,
        ["git", "push", "--person", "aiko", "--repo-path", str(repo_path)],
    )

    assert result.exit_code != 0
    assert "Failed to push 'ticket/371' to origin" in result.output
    assert "non-fast-forward" in result.output
    assert '"status": "pushed"' not in result.output
    # A refused push is not activity: no push event is recorded for it.
    assert _domain_event_records("type") == []


def test_member_git_publish_current_mode_rejects_workflow_task_run(
    monkeypatch, bind_invocation, tmp_path
):
    lease = _bind_workflow(bind_invocation, task_run_id="run-1")

    runner = CliRunner()
    result = runner.invoke(
        member_module.member,
        [
            "git",
            "publish",
            "--person",
            "aiko",
            "--repo-path",
            str(tmp_path),
            "--content-stdin",
            "--workspace-mode",
            "current",
        ],
        input="publish\n",
    )
    lease.release()

    assert result.exit_code != 0
    assert "only for interactive use" in result.output


def test_member_pr_create_passes_base(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def pr_create(
            self, repo, head, base, title, body, issue_urls, draft, closes_issue=False
        ):
            calls.update(
                {
                    "repo": repo,
                    "head": head,
                    "base": base,
                    "title": title,
                    "body": body,
                    "issue_urls": issue_urls,
                    "draft": draft,
                    "closes_issue": closes_issue,
                }
            )
            return {
                "created": True,
                "draft": False,
                "head": head,
                "base": base,
                "target": _pr_target(
                    "https://github.com/owner/repo/pull/1", "#1", title
                ),
            }

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    _fake_capabilities(monkeypatch, FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "pr",
            "create",
            "--person",
            "aiko",
            "--repo",
            "owner/repo",
            "--head",
            "feature",
            "--base",
            "ticket-driven-workflow",
            "--title",
            "PR title",
            "--content-stdin",
            "--issue-url",
            "https://github.com/owner/repo/issues/42",
            "--draft",
            "false",
        ],
        input="PR body\n",
    )

    assert result.exit_code == 0
    assert calls == {
        "repo": "owner/repo",
        "head": "feature",
        "base": "ticket-driven-workflow",
        "title": "PR title",
        "body": "PR body\n",
        "issue_urls": ("https://github.com/owner/repo/issues/42",),
        "draft": "false",
        "closes_issue": False,
        "closed": True,
    }
    assert '"base": "ticket-driven-workflow"' in result.output
    assert _domain_event_records("type", "person_id", "payload", "attributes") == [
        {
            "type": "pull_request.item",
            "person_id": "aiko",
            "payload": {
                "action": "opened",
                "pull_request": {
                    "label": "#1",
                    "title": "PR title",
                    "url": "https://github.com/owner/repo/pull/1",
                    "merged": False,
                },
            },
            "attributes": {
                "target.action": "opened",
                "target.kind": "pull_request",
                "target.label": "#1",
                "target.url": "https://github.com/owner/repo/pull/1",
                "target.title": "PR title",
            },
        }
    ]


def test_member_pr_create_reads_content_from_stdin(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def pr_create(
            self, repo, head, base, title, body, issue_urls, draft, closes_issue=False
        ):
            calls.update(
                {
                    "repo": repo,
                    "head": head,
                    "base": base,
                    "title": title,
                    "body": body,
                    "issue_urls": issue_urls,
                    "draft": draft,
                    "closes_issue": closes_issue,
                }
            )
            return {
                "created": True,
                "draft": False,
                "head": head,
                "base": base,
                "target": _pr_target(
                    "https://github.com/owner/repo/pull/1", "#1", title
                ),
            }

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    _fake_capabilities(monkeypatch, FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "pr",
            "create",
            "--person",
            "aiko",
            "--repo",
            "owner/repo",
            "--head",
            "feature",
            "--base",
            "ticket-driven-workflow",
            "--title",
            "PR title",
            "--content-stdin",
        ],
        input="## Summary\n\nPR body\n",
    )

    assert result.exit_code == 0
    assert calls == {
        "repo": "owner/repo",
        "head": "feature",
        "base": "ticket-driven-workflow",
        "title": "PR title",
        "body": "## Summary\n\nPR body\n",
        "issue_urls": (),
        "draft": "false",
        "closes_issue": False,
        "closed": True,
    }
    assert '"base": "ticket-driven-workflow"' in result.output


def test_member_pr_create_passes_closes_issue(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def pr_create(
            self, repo, head, base, title, body, issue_urls, draft, closes_issue=False
        ):
            calls["closes_issue"] = closes_issue
            calls["issue_urls"] = issue_urls
            return {
                "created": True,
                "draft": False,
                "head": head,
                "base": base,
                "target": _pr_target(
                    "https://github.com/owner/repo/pull/1", "#1", title
                ),
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    _fake_capabilities(monkeypatch, FakeService)

    result = CliRunner().invoke(
        member_module.member,
        [
            "pr",
            "create",
            "--person",
            "aiko",
            "--repo",
            "owner/repo",
            "--head",
            "feature",
            "--title",
            "PR title",
            "--content-stdin",
            "--issue-url",
            "https://github.com/owner/repo/issues/42",
            "--issue-url",
            "https://github.com/owner/repo/issues/43",
            "--closes-issue",
        ],
        input="PR body\n",
    )

    assert result.exit_code == 0
    assert calls == {
        "closes_issue": True,
        "issue_urls": (
            "https://github.com/owner/repo/issues/42",
            "https://github.com/owner/repo/issues/43",
        ),
    }


def test_member_pr_create_rejects_closes_issue_without_issue_url():
    result = CliRunner().invoke(
        member_module.member,
        [
            "pr",
            "create",
            "--person",
            "aiko",
            "--repo",
            "owner/repo",
            "--head",
            "feature",
            "--title",
            "PR title",
            "--content-stdin",
            "--closes-issue",
        ],
        input="PR body\n",
    )

    assert result.exit_code != 0
    assert "--closes-issue requires --issue-url." in result.output


def test_member_pr_create_rejects_an_empty_issue_url():
    result = CliRunner().invoke(
        member_module.member,
        [
            "pr",
            "create",
            "--person",
            "aiko",
            "--repo",
            "owner/repo",
            "--head",
            "feature",
            "--title",
            "PR title",
            "--content-stdin",
            "--issue-url",
            "https://github.com/owner/repo/issues/42",
            "--issue-url",
            " ",
        ],
        input="PR body\n",
    )

    assert result.exit_code != 0
    assert "--issue-url must not be empty." in result.output


def test_member_pr_create_rejects_unknown_draft_value():
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "pr",
            "create",
            "--person",
            "aiko",
            "--repo",
            "owner/repo",
            "--head",
            "feature",
            "--title",
            "PR title",
            "--content-stdin",
            "--draft",
            "auto",
        ],
        input="PR body\n",
    )

    assert result.exit_code != 0
    assert "'auto' is not one of 'true', 'false'" in result.output


def test_member_pr_create_rejects_multiline_title():
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "pr",
            "create",
            "--person",
            "aiko",
            "--repo",
            "owner/repo",
            "--head",
            "feature",
            "--title",
            "PR\nTitle",
            "--content-stdin",
        ],
        input="PR body\n",
    )

    assert result.exit_code != 0
    assert "title must not contain newlines" in result.output


def test_member_pr_create_rejects_missing_content_source():
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "pr",
            "create",
            "--person",
            "aiko",
            "--repo",
            "owner/repo",
            "--head",
            "feature",
            "--title",
            "PR title",
        ],
    )

    assert result.exit_code != 0
    assert "--content-stdin or --content-file" in result.output


def test_member_pr_update_rejects_update_without_any_change():
    result = CliRunner().invoke(
        member_module.member,
        [
            "pr",
            "update",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/pull/7",
        ],
        input="This must not be read.",
    )

    assert result.exit_code != 0
    assert (
        "pr update needs --content-stdin/--content-file, --title, --issue-url, or --drop-issue-links."
        in result.output
    )


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (["--issue-url", ""], "--issue-url must not be empty."),
        (["--closes-issue"], "--closes-issue requires --issue-url."),
        (
            ["--issue-url", "https://tickets.test/A", "--drop-issue-links"],
            "cannot be combined",
        ),
    ],
)
def test_member_pr_update_refuses_ambiguous_or_invalid_links(options, message):
    result = CliRunner().invoke(
        member_module.member,
        [
            "pr",
            "update",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/pull/7",
            *options,
        ],
    )
    assert result.exit_code != 0 and message in result.output


@pytest.mark.parametrize(
    ("options", "links", "content"),
    [
        ([], None, True),
        (["--drop-issue-links"], [], True),
        (["--drop-issue-links"], [], False),
        (
            ["--issue-url", "https://tickets.test/A"],
            [TicketLink("https://tickets.test/A", False)],
            False,
        ),
        (
            ["--issue-url", "https://tickets.test/A", "--refs-issue"],
            [TicketLink("https://tickets.test/A", False)],
            True,
        ),
        (
            [
                "--issue-url",
                "https://tickets.test/A",
                "--issue-url",
                "https://tickets.test/B",
                "--closes-issue",
            ],
            [
                TicketLink("https://tickets.test/A", True),
                TicketLink("https://tickets.test/B", True),
            ],
            False,
        ),
    ],
)
def test_member_pr_update_reads_entire_stdin_and_closes_service(
    monkeypatch, options, links, content
):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def pr_update(self, pr_url, body, title, *, tickets):
            calls.update(
                {
                    "pr_url": pr_url,
                    "body": body,
                    "title": title,
                    "tickets": tickets,
                }
            )
            return {"body": body, "target": _pr_target(pr_url)}

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    _fake_capabilities(monkeypatch, FakeService)

    result = CliRunner().invoke(
        member_module.member,
        [
            "pr",
            "update",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/pull/7",
            *(["--content-stdin"] if content else []),
            *options,
        ],
        input="## Summary\n\nUpdated body\n",
    )

    assert result.exit_code == 0
    assert calls == {
        "pr_url": "https://github.com/owner/repo/pull/7",
        "body": "## Summary\n\nUpdated body\n" if content else None,
        "title": None,
        "tickets": links,
        "closed": True,
    }


@pytest.mark.parametrize("content", ["", "\n", " \t\n"])
def test_member_pr_update_normalizes_blank_stdin_and_records_evidence(
    monkeypatch, bind_invocation, content
):
    lease = _bind_workflow(bind_invocation, run_id="run-1")
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def pr_update(self, pr_url, body, title, *, tickets):
            calls.update(
                {
                    "pr_url": pr_url,
                    "body": body,
                    "title": title,
                    "tickets": tickets,
                }
            )
            return {"body": body, "target": _pr_target(pr_url)}

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    _fake_capabilities(monkeypatch, FakeService)

    result = CliRunner().invoke(
        member_module.member,
        [
            "pr",
            "update",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/pull/7",
            "--content-stdin",
        ],
        input=content,
    )
    lease.release()

    assert result.exit_code == 0
    assert calls == {
        "pr_url": "https://github.com/owner/repo/pull/7",
        "body": "",
        "title": None,
        "tickets": None,
        "closed": True,
    }
    assert json.loads(result.output)["body"] == ""
    assert TaskRunStore().evidence("run-1")[0]["evidence_type"] == "pr_update"


def test_member_issue_update_rejects_update_without_any_change():
    result = CliRunner().invoke(
        member_module.member,
        [
            "ticket",
            "update",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/issues/42",
        ],
        input="This must not be read.",
    )

    assert result.exit_code != 0
    assert "ticket update needs --content-stdin" in result.output


@pytest.mark.parametrize(
    ("command", "url_option"),
    [
        (["ticket", "update"], "https://github.com/owner/repo/issues/42"),
        (["pr", "update"], "https://github.com/owner/repo/pull/7"),
    ],
)
def test_member_update_rejects_an_empty_title(command, url_option):
    result = CliRunner().invoke(
        member_module.member,
        [*command, "--person", "aiko", "--url", url_option, "--title", "   "],
    )

    assert result.exit_code != 0
    assert "title must not be empty." in result.output


@pytest.mark.parametrize(
    ("options", "relations"),
    [
        (
            [
                "--parent",
                "https://github.com/owner/repo/issues/1",
                "--add-blocked-by",
                "https://github.com/owner/repo/issues/2",
                "--add-blocked-by",
                "https://github.com/owner/repo/issues/3",
                "--remove-blocked-by",
                "https://github.com/owner/repo/issues/4",
            ],
            {
                "parent": "https://github.com/owner/repo/issues/1",
                "add_blocked_by": (
                    "https://github.com/owner/repo/issues/2",
                    "https://github.com/owner/repo/issues/3",
                ),
                "remove_blocked_by": ("https://github.com/owner/repo/issues/4",),
            },
        ),
        (
            ["--parent", ""],
            {"parent": "", "add_blocked_by": (), "remove_blocked_by": ()},
        ),
    ],
)
def test_member_issue_update_passes_the_relations(monkeypatch, options, relations):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = {}

    class FakeService:
        def __init__(self, *_args):
            pass

        async def ticket_update(self, ticket_url, **kwargs):
            calls.update(kwargs)
            return {
                "body": "",
                "state": "open",
                "state_changed": False,
                "labels": [],
                "target": _ticket_target(ticket_url, "#42", "Child"),
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module,
        "resolve_member_context",
        lambda identifier: (FakeContext(person), person),
    )
    _fake_capabilities(monkeypatch, FakeService)

    result = CliRunner().invoke(
        member_module.member,
        [
            "ticket",
            "update",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/issues/42",
            *options,
        ],
    )

    assert result.exit_code == 0, result.output
    assert {key: calls[key] for key in relations} == relations


def test_member_issue_update_rejects_state_reason_without_close():
    result = CliRunner().invoke(
        member_module.member,
        [
            "ticket",
            "update",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/issues/42",
            "--state",
            "open",
            "--state-reason",
            "completed",
            "--human-approved",
        ],
    )

    assert result.exit_code != 0
    assert "--state-reason requires --state closed." in result.output


def test_member_issue_edit_on_a_closed_issue_records_no_close_activity(
    monkeypatch,
):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    recorded = []

    class FakeService:
        def __init__(self, *_args):
            pass

        async def ticket_update(self, ticket_url, **_kwargs):
            # An already-closed ticket reports state "closed" for any edit;
            # the service marks the state as unchanged.
            return {
                "body": "Updated body",
                "state": "closed",
                "state_changed": False,
                "labels": [],
                "target": _ticket_target(ticket_url, "#42", "Capability gap"),
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module,
        "resolve_member_context",
        lambda identifier: (FakeContext(person), person),
    )
    _fake_capabilities(monkeypatch, FakeService)
    monkeypatch.setattr(
        member_activity_events,
        "record_correlated_event",
        lambda **kwargs: recorded.append(kwargs),
    )

    result = CliRunner().invoke(
        member_module.member,
        [
            "ticket",
            "update",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/issues/42",
            "--content-stdin",
        ],
        input="Updated body",
    )

    assert result.exit_code == 0
    # The ticket is the command's work target, and nothing more: no close.
    assert [event["event_type"] for event in recorded] == ["target.recorded"]


def test_member_issue_close_passes_approval_and_records_activity(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = {}
    activity_calls = []

    class FakeService:
        def __init__(self, *_args):
            pass

        async def ticket_update(self, ticket_url, **kwargs):
            calls.update({"ticket_url": ticket_url, **kwargs})
            return {
                "body": "Body",
                "state": "closed",
                "state_changed": True,
                "labels": ["cli"],
                "target": _ticket_target(ticket_url, "#42", "Capability gap"),
            }

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module,
        "resolve_member_context",
        lambda identifier: (FakeContext(person), person),
    )
    _fake_capabilities(monkeypatch, FakeService)
    monkeypatch.setattr(
        member_module,
        "record_member_issue_close_event",
        lambda member, payload: activity_calls.append((member, payload)),
    )

    result = CliRunner().invoke(
        member_module.member,
        [
            "ticket",
            "update",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/issues/42",
            "--state",
            "closed",
            "--state-reason",
            "completed",
            "--add-label",
            "cli",
            "--human-approved",
        ],
    )

    assert result.exit_code == 0
    assert calls == {
        "ticket_url": "https://github.com/owner/repo/issues/42",
        "body": None,
        "title": None,
        "add_labels": ("cli",),
        "remove_labels": (),
        "state": "closed",
        "state_reason": "completed",
        "human_approved": True,
        "parent": None,
        "add_blocked_by": (),
        "remove_blocked_by": (),
        "closed": True,
    }
    assert activity_calls[0][0] is person
    assert activity_calls[0][1]["state"] == "closed"
    assert activity_calls[0][1]["state_changed"] is True


def test_member_issue_update_reads_entire_stdin_and_closes_service(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def ticket_update(self, ticket_url, **kwargs):
            calls.update({"ticket_url": ticket_url, **kwargs})
            return {
                "body": kwargs["body"],
                "state": "open",
                "state_changed": False,
                "labels": [],
                "target": _ticket_target(ticket_url, "#42"),
            }

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    _fake_capabilities(monkeypatch, FakeService)

    result = CliRunner().invoke(
        member_module.member,
        [
            "ticket",
            "update",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/issues/42",
            "--content-stdin",
        ],
        input="## Summary\n\nUpdated body\n",
    )

    assert result.exit_code == 0
    assert calls == {
        "ticket_url": "https://github.com/owner/repo/issues/42",
        "body": "## Summary\n\nUpdated body\n",
        "title": None,
        "add_labels": (),
        "remove_labels": (),
        "state": None,
        "state_reason": None,
        "human_approved": False,
        "parent": None,
        "add_blocked_by": (),
        "remove_blocked_by": (),
        "closed": True,
    }


@pytest.mark.parametrize("content", ["", "\n", " \t\n"])
def test_member_issue_update_normalizes_blank_stdin_and_records_evidence(
    monkeypatch, bind_invocation, content
):
    lease = _bind_workflow(bind_invocation, run_id="run-1")
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def ticket_update(self, ticket_url, **kwargs):
            calls.update({"ticket_url": ticket_url, **kwargs})
            return {
                "body": kwargs["body"],
                "state": "open",
                "state_changed": False,
                "labels": [],
                "target": _ticket_target(ticket_url, "#42"),
            }

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    _fake_capabilities(monkeypatch, FakeService)

    result = CliRunner().invoke(
        member_module.member,
        [
            "ticket",
            "update",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/issues/42",
            "--content-stdin",
        ],
        input=content,
    )
    lease.release()

    assert result.exit_code == 0
    assert calls == {
        "ticket_url": "https://github.com/owner/repo/issues/42",
        "body": "",
        "title": None,
        "add_labels": (),
        "remove_labels": (),
        "state": None,
        "state_reason": None,
        "human_approved": False,
        "parent": None,
        "add_blocked_by": (),
        "remove_blocked_by": (),
        "closed": True,
    }
    assert json.loads(result.output)["body"] == ""
    assert TaskRunStore().evidence("run-1")[0]["evidence_type"] == "issue_update"


def test_member_pr_review_reads_stdin_and_records_evidence(
    monkeypatch, bind_invocation
):
    lease = _bind_workflow(bind_invocation, run_id="run-1")
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def pr_review(self, pr_url, body, verdict, head):
            calls.update(
                {"pr_url": pr_url, "body": body, "verdict": verdict, "head": head}
            )
            return {
                "verdict": verdict,
                "commit": head,
                "url": "https://github.com/owner/repo/pull/7#pullrequestreview-555",
                "target": _pr_target(pr_url),
            }

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    _fake_capabilities(monkeypatch, FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "pr",
            "review",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/pull/7",
            "--event",
            "request_changes",
            "--head",
            "abc123",
            "--content-stdin",
        ],
        input="Nothing blocks this.\n",
    )
    lease.release()

    assert result.exit_code == 0, result.output
    assert calls == {
        "pr_url": "https://github.com/owner/repo/pull/7",
        "body": "Nothing blocks this.\n",
        "verdict": "request_changes",
        "head": "abc123",
        "closed": True,
    }
    payload = json.loads(result.output)
    assert (payload["verdict"], payload["commit"]) == ("request_changes", "abc123")
    assert TaskRunStore().evidence("run-1")[0]["evidence_type"] == "pr_review"


def test_member_pr_review_rejects_unknown_event():
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "pr",
            "review",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/pull/7",
            "--event",
            "lgtm",
            "--head",
            "abc123",
            "--content-stdin",
        ],
        input="x\n",
    )

    assert result.exit_code != 0
    assert "Invalid value for '--event'" in result.output


@pytest.mark.parametrize("command", ["review", "review-comment"])
def test_member_pr_review_needs_the_head_the_member_read(command):
    """A review is submitted on the head the member read, so it names it."""
    location = ["--path", "a.py", "--line", "1"] if command == "review-comment" else []
    event = ["--event", "comment"] if command == "review" else []
    result = CliRunner().invoke(
        member_module.member,
        [
            "pr",
            command,
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/pull/7",
            *event,
            *location,
            "--content-stdin",
        ],
        input="x\n",
    )

    assert result.exit_code != 0
    assert "Missing option '--head'" in result.output


def test_member_pr_review_comment_reads_stdin_and_records_evidence(
    monkeypatch, bind_invocation
):
    lease = _bind_workflow(bind_invocation, run_id="run-1")
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def pr_review_comment(
            self, pr_url, body, head, file_path, line, side, start_line, start_side
        ):
            calls.update(
                {
                    "pr_url": pr_url,
                    "body": body,
                    "head": head,
                    "file_path": file_path,
                    "line": line,
                    "side": side,
                    "start_line": start_line,
                    "start_side": start_side,
                }
            )
            return {
                "id": "discussion_r123",
                "url": "https://github.com/owner/repo/pull/7#discussion_r123",
                "created_at": "2026-01-01T00:00:00Z",
                "path": file_path,
                "line": line,
                "side": side,
                "target": _pr_target(pr_url),
            }

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    _fake_capabilities(monkeypatch, FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "pr",
            "review-comment",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/pull/7",
            "--head",
            "abc123",
            "--path",
            "guildbotics/example.py",
            "--line",
            "12",
            "--side",
            "RIGHT",
            "--start-line",
            "10",
            "--start-side",
            "RIGHT",
            "--content-stdin",
        ],
        input="Please simplify this branch.\n",
    )
    lease.release()

    assert result.exit_code == 0
    assert calls == {
        "pr_url": "https://github.com/owner/repo/pull/7",
        "body": "Please simplify this branch.\n",
        "head": "abc123",
        "file_path": "guildbotics/example.py",
        "line": 12,
        "side": "RIGHT",
        "start_line": 10,
        "start_side": "RIGHT",
        "closed": True,
    }
    payload = json.loads(result.output)
    assert payload["id"] == "discussion_r123"
    assert TaskRunStore().evidence("run-1")[0]["evidence_type"] == ("pr_review_comment")


def test_member_pr_review_comment_rejects_partial_range():
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "pr",
            "review-comment",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/pull/7",
            "--head",
            "abc123",
            "--path",
            "guildbotics/example.py",
            "--line",
            "12",
            "--start-line",
            "10",
            "--content-stdin",
        ],
        input="Please simplify this branch.\n",
    )

    assert result.exit_code != 0
    assert "--start-line and --start-side must be provided together" in result.output


_ISSUE = "https://github.com/owner/repo/issues/42"
_PULL = "https://github.com/owner/repo/pull/7"


@pytest.mark.parametrize(
    ("group", "url", "capability"),
    [("ticket", _ISSUE, "ticket_reaction"), ("pr", _PULL, "pr_reaction")],
)
@pytest.mark.parametrize("reaction", ["ack", "agree", "celebrate", "support"])
def test_member_reaction_add_names_its_comment_and_records_evidence(
    monkeypatch, bind_invocation, group, url, capability, reaction
):
    """A reaction names the comment (or review) by the id the item's read
    gave it, and is one of the four meanings every service carries."""
    lease = _bind_workflow(bind_invocation, run_id="run-1")
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = []

    class FakeService:
        def __init__(self, *_args):
            pass

        async def react(self, *args):
            calls.append((capability, *args))
            return {"reaction": args[2], "id": args[1]}

        ticket_reaction = pr_reaction = react

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module,
        "resolve_member_context",
        lambda _identifier: (FakeContext(person), person),
    )
    _fake_capabilities(monkeypatch, FakeService)

    result = CliRunner().invoke(
        member_module.member,
        [
            group,
            "reaction",
            "add",
            "--url",
            url,
            "--person",
            "aiko",
            "--comment-id",
            "pullrequestreview-55",
            "--reaction",
            reaction,
        ],
    )
    lease.release()

    assert result.exit_code == 0, result.output
    assert calls == [(capability, url, "pullrequestreview-55", reaction)]
    assert json.loads(result.output) == {
        "reaction": reaction,
        "id": "pullrequestreview-55",
    }
    assert TaskRunStore().evidence("run-1")[0]["evidence_type"] == "reaction_add"


@pytest.mark.parametrize(("group", "url"), [("ticket", _ISSUE), ("pr", _PULL)])
def test_member_reaction_add_takes_neither_a_target_nor_an_emoji(group, url):
    base = [group, "reaction", "add", "--person", "aiko", "--url", url]
    with_target = CliRunner().invoke(
        member_module.member,
        [*base, "--target", "review", "--comment-id", "55", "--reaction", "ack"],
    )
    emoji = CliRunner().invoke(
        member_module.member,
        [*base, "--comment-id", "55", "--reaction", "eyes"],
    )

    assert with_target.exit_code != 0
    assert "No such option '--target'" in with_target.output
    assert emoji.exit_code != 0
    assert "'eyes' is not one of 'ack', 'agree', 'celebrate', 'support'" in (
        emoji.output
    )


def test_member_task_status_cli(monkeypatch, bind_invocation, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    bind_invocation(task_run_id="run-1")
    store = TaskRunStore()
    store.append_evidence("run-1", "issue_comment", {"id": "issuecomment-1"})
    store.complete(
        "run-1",
        "done",
        "summary",
        Work.of_ticket("https://github.com/owner/repo/issues/1"),
        "aiko",
    )
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        ["task", "status", "--person", "aiko"],
    )

    assert result.exit_code == 0
    assert '"completed": true' in result.output
    assert '"evidence_types": ["issue_comment"]' in result.output


def test_member_interactive_trace_leaves_missing_context_to_the_command(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-1")
    missing = FileNotFoundError("team/project.yml")

    def missing_context(_identifier):
        raise missing

    class UnexpectedInteractiveTraceStore:
        def start_or_touch(self, **_fields):
            raise AssertionError("no session starts for an unresolvable member")

    monkeypatch.setattr(member_module, "resolve_member_context", missing_context)
    monkeypatch.setattr(
        member_module, "InteractiveTraceStore", UnexpectedInteractiveTraceStore
    )

    result = CliRunner().invoke(
        member_module.member,
        ["memory", "recall", "--person", "aiko", "--query", "x"],
    )

    assert result.exception is missing


def test_member_task_status_skips_interactive_trace_under_workflow(
    monkeypatch, bind_invocation, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-1")
    bind_invocation(task_run_id="run-1")

    def unexpected_context(_identifier):
        raise AssertionError("workflow command should not resolve interactive context")

    monkeypatch.setattr(member_module, "resolve_member_context", unexpected_context)
    store = TaskRunStore()
    store.append_evidence("run-1", "issue_comment", {"id": "issuecomment-1"})
    store.complete(
        "run-1",
        "done",
        "summary",
        Work.of_ticket("https://github.com/owner/repo/issues/1"),
        "aiko",
    )
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        ["task", "status", "--person", "aiko"],
    )

    assert result.exit_code == 0
    assert DiagnosticsStore().list_traces(source="interactive") == []


def test_member_interactive_trace_uses_resolved_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls: dict[str, str] = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeInteractiveTraceStore:
        def start_or_touch(self, *, person_id, workspace, host, thread_key):
            calls.update(
                {
                    "person_id": person_id,
                    "workspace": workspace,
                    "host": host,
                    "thread_key": thread_key,
                }
            )
            return member_module.InteractiveTraceSession(
                trace_id="trace-1",
                person_id=person_id,
                workspace=workspace,
                host=host,
                thread_key=thread_key,
                started_at="2026-01-01T00:00:00+00:00",
                last_seen_at="2026-01-01T00:00:00+00:00",
                expires_at="2026-01-01T00:30:00+00:00",
            )

        def touch(self, session):
            return session

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(
        member_module, "InteractiveTraceStore", FakeInteractiveTraceStore
    )
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "--workspace",
            str(workspace),
            "memory",
            "recall",
            "--person",
            "aiko",
            "--query",
            "x",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["workspace"] == str(workspace.resolve())


@pytest.fixture
def local_chat(monkeypatch, tmp_path):
    """Aiko, whose chat is the local one, with channel ``C1``."""
    from guildbotics.integrations.local.chat import LocalChatService

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    person = Person(person_id="aiko", name="Aiko")
    context = FakeContext(person)
    context.team = chat_team(person)
    context.get_chat_service = lambda: LocalChatService(person)
    monkeypatch.setattr(
        member_module, "resolve_member_context", lambda _person: (context, person)
    )
    say("C1", "question", message_id="t1", occurred_at=at(100))
    say("C1", "detail", message_id="r1", thread_id="t1", occurred_at=at(101))
    return "C1"


def _chat(*args: str, content: str | None = None):
    return CliRunner().invoke(
        member_module.member,
        ["chat", *args, "--person", "aiko"]
        + (["--content-stdin"] if content is not None else []),
        input=content,
    )


def _replies(channel_id: str) -> list[tuple[str, str]]:
    return [
        (line["text"], line["thread_id"])
        for line in lines(channel_id)
        if line["author"] == username("aiko") and "reaction" not in line
    ]


def test_member_chat_reply_reads_body_and_records_evidence(local_chat, bind_invocation):
    # The run id is injected by the workflow via env, not a CLI flag; the write
    # command records its evidence under the env-provided run id.
    from guildbotics.capabilities.task_runs import RunStore
    from guildbotics.integrations.chat_receive_status import ChatReceiveStatus

    lease = _bind_workflow(bind_invocation, run_id="run-1")
    # The chat run has been handed its batch and has read the updates since.
    RunStore().append_evidence("run-1", "chat_batch", {"event_ids": ["E1"]})
    RunStore().append_evidence("run-1", "chat_updates", {"event_ids": []})
    ChatReceiveStatus().save("slack", "aiko", "C1", state="ready")

    result = _chat(
        "reply",
        "--channel-id",
        local_chat,
        "--thread-id",
        "t1",
        content="了解しました。",
    )
    lease.release()

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["text"] == "了解しました。"
    assert payload["thread_id"] == "t1"
    assert _replies(local_chat) == [("了解しました。", "t1")]
    assert TaskRunStore().evidence("run-1")[-1]["evidence_type"] == "chat_reply"


def test_member_chat_reply_to_a_message_url_answers_its_thread(local_chat):
    result = _chat(
        "reply",
        "--message-url",
        chat.message_url(local_chat, "r1", "t1"),
        content="了解しました。",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert (payload["channel_id"], payload["thread_id"]) == (local_chat, "t1")
    assert _replies(local_chat) == [("了解しました。", "t1")]


def test_member_chat_reply_needs_a_thread(local_chat):
    result = _chat("reply", "--channel-id", local_chat, content="hi")

    assert result.exit_code != 0
    assert "message id or message URL is required" in result.output
    assert _replies(local_chat) == []


def test_member_chat_inspect_thread_accepts_message_url(local_chat):
    result = _chat(
        "inspect",
        "thread",
        "--message-url",
        chat.message_url(local_chat, "r1", "t1"),
        "--limit",
        "20",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["channel_id"] == local_chat
    assert payload["thread_id"] == "t1"
    assert [m["text"] for m in payload["messages"]] == ["question", "detail"]


@pytest.mark.parametrize(
    ("since", "until"),
    [
        (at(90).isoformat(), at(100).isoformat()),
        # Without an offset, a time is the local one.
        (
            at(90).astimezone().replace(tzinfo=None).isoformat(),
            at(100).astimezone().replace(tzinfo=None).isoformat(),
        ),
    ],
)
def test_member_chat_inspect_channel_reads_a_window_by_channel_name(
    local_chat, since, until
):
    say("general", "topic", message_id="g1", occurred_at=at(100))
    say("general", "later", message_id="g2", occurred_at=at(101))

    result = _chat(
        "inspect",
        "channel",
        "--channel-name",
        "general",
        "--since",
        since,
        "--until",
        until,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["channel_name"] == "general"
    assert datetime.fromisoformat(payload["since"]) == at(90)
    assert [m["text"] for m in payload["messages"]] == ["topic"]


def test_member_chat_inspect_channel_refuses_what_is_no_time(local_chat):
    result = _chat("inspect", "channel", "--channel-id", local_chat, "--since", "100.0")

    assert result.exit_code != 0
    assert "not an ISO 8601 time" in result.output


def test_member_chat_rejects_empty_content():
    result = _chat("reply", "--channel-id", "C1", "--thread-id", "100.1", content="")

    assert result.exit_code != 0
    assert "message body must not be empty" in result.output


def test_member_chat_reaction_accepts_message_url(local_chat):
    result = _chat(
        "reaction",
        "add",
        "--message-url",
        chat.message_url(local_chat, "r1", "t1"),
        "--reaction",
        "ack",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["channel_id"] == local_chat
    assert payload["message_id"] == "r1"
    assert [line for line in lines(local_chat) if "reaction" in line] == [
        {"reaction": "ack", "author": username("aiko"), "message_id": "r1"}
    ]


def test_member_chat_needs_a_chat_service(monkeypatch):
    person = Person(person_id="aiko", name="Aiko")
    context = FakeContext(person)
    context.get_chat_service = lambda: pytest.fail("no chat service to make")
    monkeypatch.setattr(
        member_module, "resolve_member_context", lambda _person: (context, person)
    )

    result = _chat("identity")

    assert result.exit_code != 0
    assert (
        t("integrations.resources.unsupported", service="chat_service", name="")
        in result.output
    )


def test_member_chat_noop_and_complete(tmp_path, monkeypatch, bind_invocation):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _bind_workflow(bind_invocation, run_id="run-1")
    person = Person(person_id="aiko", name="Aiko")

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    runner = CliRunner()

    noop = runner.invoke(
        member_module.member,
        [
            "chat",
            "noop",
            "--person",
            "aiko",
            "--content-stdin",
        ],
        input="Not relevant.",
    )
    complete = runner.invoke(
        member_module.member,
        [
            "chat",
            "complete",
            "--person",
            "aiko",
            "--status",
            "done",
            "--content-stdin",
        ],
        input="No response needed.",
    )

    assert noop.exit_code == 0
    assert complete.exit_code == 0
    payload = json.loads(complete.output)
    assert payload["subject_id"] == "slack:C1:100.1:E1"
    assert payload["evidence_types"] == ["chat_noop"]


def test_member_task_complete_reads_summary_from_stdin(
    monkeypatch, bind_invocation, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _bind_workflow(bind_invocation, task_run_id="run-1")
    person = Person(person_id="aiko", name="Aiko")

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def task_completion_readiness(self, _ticket_url, _evidence):
            return []

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    _fake_capabilities(monkeypatch, FakeService)
    TaskRunStore().append_evidence("run-1", "issue_comment", {"id": "issuecomment-1"})
    result = CliRunner().invoke(
        member_module.member,
        [
            "task",
            "complete",
            "--person",
            "aiko",
            "--status",
            "done",
            "--content-stdin",
        ],
        input="Completed with verification.\n",
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["summary"] == "Completed with verification.\n"
    assert payload["pr_readiness"] == []


def test_member_task_complete_rejects_blocked_pr_readiness(
    monkeypatch, bind_invocation, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _bind_workflow(bind_invocation, task_run_id="run-1")
    person = Person(person_id="aiko", name="Aiko")
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def task_completion_readiness(self, work, evidence):
            calls.update({"work": work, "evidence": evidence})
            raise member_module.MemberCapabilityError(
                "Task cannot be completed because PR readiness is blocked. "
                "https://github.com/owner/repo/pull/7: CI checks are still pending."
            )

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    _fake_capabilities(monkeypatch, FakeService)
    store = TaskRunStore()
    store.append_evidence(
        "run-1",
        "pr_create",
        {"target": _pr_target("https://github.com/owner/repo/pull/7")},
    )

    result = CliRunner().invoke(
        member_module.member,
        [
            "task",
            "complete",
            "--person",
            "aiko",
            "--status",
            "done",
            "--content-stdin",
        ],
        input="Completed.\n",
    )

    assert result.exit_code != 0
    assert "PR readiness is blocked" in result.output
    assert "CI checks are still pending" in result.output
    assert calls["work"] == TICKET_WORK
    assert calls["evidence"][0]["evidence_type"] == "pr_create"
    assert calls["closed"] is True
    with pytest.raises(member_module.TaskRunError, match="not completed"):
        store.status("run-1")


def test_member_task_complete_returns_revalidated_pr_readiness(
    monkeypatch, bind_invocation, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _bind_workflow(bind_invocation, task_run_id="run-1")
    person = Person(person_id="aiko", name="Aiko")
    readiness = {
        "target": _pr_target("https://github.com/owner/repo/pull/7"),
        "head_sha": "abc123",
        "readiness": "ready",
        "completion_blockers": [],
    }
    checked: list[str] = []

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def task_completion_readiness(self, work, _evidence):
            checked.append(work)
            return [readiness]

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    _fake_capabilities(monkeypatch, FakeService)
    TaskRunStore().append_evidence(
        "run-1",
        "pr_create",
        {"target": _pr_target("https://github.com/owner/repo/pull/7")},
    )

    result = CliRunner().invoke(
        member_module.member,
        [
            "task",
            "complete",
            "--person",
            "aiko",
            "--status",
            "done",
            "--content-stdin",
        ],
        input="Completed.\n",
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["pr_readiness"] == [readiness]
    # The ticket is the run's work, which no argument names.
    assert checked == [TICKET_WORK]
    assert payload["subject_url"] == TICKET_WORK.identity


@pytest.mark.parametrize(
    ("run_ids", "work", "subject"),
    [
        ({"task_run_id": "run-1"}, TICKET_WORK, TICKET_WORK.identity),
        ({"run_id": "run-1"}, CHAT_WORK, "slack:C1:100.1:E1"),
        ({"task_run_id": "run-1", "work": Work("manual", "run-1")}, None, "run-1"),
    ],
)
def test_member_checks_await_ends_any_run_to_await_its_pending_checks(
    monkeypatch, bind_invocation, tmp_path, run_ids, work, subject
):
    """A ticket's, a chat thread's, or a command's run ends without a wait in
    the turn, the same way: what the capability told the PRs is the run's
    evidence, and the run is over, not failed, about its own work's subject."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _bind_workflow(bind_invocation, **run_ids)
    work = work or run_ids["work"]
    person = Person(person_id="aiko", name="Aiko")
    waiting = {
        "target": _pr_target("https://github.com/owner/repo/pull/7"),
        "readiness": "blocked",
    }
    ready = {
        "target": _pr_target("https://github.com/owner/repo/pull/8"),
        "readiness": "ready",
    }
    calls: list[tuple[Work, str]] = []

    class FakeService:
        def __init__(self, *_args):
            pass

        async def await_checks(self, awaited_work, run_id, runs):
            calls.append((awaited_work, run_id))
            runs.append_evidence(
                run_id, "checks_awaited", {"target": waiting["target"]}
            )
            return [waiting, ready]

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module,
        "resolve_member_context",
        lambda _identifier: (FakeContext(person), person),
    )
    _fake_capabilities(monkeypatch, FakeService)
    TaskRunStore().append("run-1", {"kind": "note", "message": "started"})

    result = CliRunner().invoke(
        member_module.member,
        ["checks", "await", "--person", "aiko", "--content-stdin"],
        input="Waiting for CI.\n",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert calls == [(work, "run-1")]
    assert (payload["status"], payload["evidence_types"]) == (
        "awaiting_checks",
        ["checks_awaited"],
    )
    assert payload["pr_readiness"] == [waiting, ready]
    [record] = TaskRunStore().records()
    assert record.status == "succeeded"
    assert record.result is not None
    assert (record.result.status, record.result.subject_id) == (
        "awaiting_checks",
        subject,
    )


def test_member_checks_await_is_refused_outside_a_run():
    result = CliRunner().invoke(
        member_module.member,
        ["checks", "await", "--person", "aiko", "--content-stdin"],
        input="Waiting.\n",
    )

    assert result.exit_code == 1
    assert t("cli.member.run.required_any") in " ".join(result.output.split())


@pytest.mark.parametrize("group", ["task", "chat"])
def test_a_run_completion_cannot_await_checks(group):
    """Only ``checks await`` ends a run to await checks."""
    result = CliRunner().invoke(
        member_module.member,
        [group, "complete", "--person", "aiko", "--status", "awaiting_checks"]
        + ["--content-stdin"],
        input="Waiting.\n",
    )

    assert result.exit_code == 2
    assert "'awaiting_checks' is not one of" in result.output


def test_member_cli_help_stays_in_sync_with_capability_catalog():
    """Every member command documents itself (own docstring or catalog summary),
    and the capability catalog and the CLI leaf commands stay in exact sync:
    a leaf absent from the catalog would be missing from ``member help`` /
    ``member context`` even when its own docstring satisfies ``--help``."""
    leaves: dict[str, click.Command] = {}

    def collect(group: click.Group, path: tuple[str, ...] = ()) -> None:
        for name, command in group.commands.items():
            if isinstance(command, click.Group):
                collect(command, path + (name,))
            else:
                leaves[" ".join(path + (name,))] = command

    collect(member_module.member)

    assert sorted(path for path, cmd in leaves.items() if not cmd.help) == []
    assert sorted(leaves) == sorted(command_summaries())


def test_chat_updates_reads_queue_without_constructing_chat_service(
    monkeypatch, bind_invocation
):
    from guildbotics.capabilities.task_runs import RunStore
    from guildbotics.integrations.chat_receive_status import ChatReceiveStatus
    from guildbotics.integrations.file_chat_state_store import (
        FileConversationStateStore,
    )
    from guildbotics.runtime.chat_service import ChatEvent

    lease = _bind_workflow(bind_invocation, run_id="run-1")
    person = Person(person_id="aiko", name="Aiko")
    context = FakeContext(person)

    async def close():
        pass

    context.aclose = close

    def no_chat_api():
        pytest.fail("Queue updates must not construct a Slack client")

    context.get_chat_service = no_chat_api
    monkeypatch.setattr(
        member_module, "resolve_member_context", lambda _person: (context, person)
    )
    RunStore().append_evidence(
        "run-1",
        "chat_batch",
        {
            "person_id": "aiko",
            "service": "slack",
            "channel_id": "C1",
            "thread_id": "100.1",
            "self_user_id": "U_BOT",
            "event_ids": ["E1"],
        },
    )
    ChatReceiveStatus().save("slack", "aiko", "C1", state="ready")
    store = FileConversationStateStore()
    store.upsert_pending_event(
        "slack",
        "aiko",
        "C1",
        chat_event(
            event_id="E3",
            channel_id="C1",
            message_id="103.1",
            thread_id="100.1",
            author_id="U_USER",
            text="訂正です",
        ),
    )
    try:
        result = CliRunner().invoke(
            member_module.member,
            ["chat", "updates", "--person", "aiko"],
        )
    finally:
        lease.release()
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "new_messages"
    assert payload["messages"][0]["text"] == "訂正です"
    assert len(store.load_pending_events("slack", "aiko", "C1")) == 1
    assert RunStore().evidence("run-1")[-1]["evidence_type"] == "chat_updates"


def _fake_github_service(monkeypatch, person, **results):
    """Install a GitHub capability fake whose methods return ``results``."""
    monkeypatch.setattr(
        member_module,
        "resolve_member_context",
        lambda _id: (FakeContext(person), person),
    )

    class FakeService:
        def __init__(self, *_args):
            pass

        async def aclose(self):
            pass

    for name, result in results.items():

        async def method(self, *_args, _result=result, **_kwargs):
            return _result

        setattr(FakeService, name, method)
    _fake_capabilities(monkeypatch, FakeService)


def _ticket_target(url, label, title=""):
    return {"kind": "ticket", "url": url, "title": title, "label": label}


def _pr_target(url, label="#7", title=""):
    return {"kind": "pull_request", "url": url, "title": title, "label": label}


def _work_target_records():
    return [
        (record["attributes"].get("target.action", ""), record["attributes"])
        for record in DiagnosticsStore().records_between(includes=lambda _: True)
        if record.get("type") == "target.recorded"
    ]


@pytest.mark.parametrize(
    ("arguments", "read_only"),
    [
        (
            [
                "pr",
                "comment",
                "--url",
                "https://github.com/owner/repo/pull/544",
                "--content-stdin",
            ],
            False,
        ),
    ],
)
def test_member_code_hosting_commands_declare_their_work_target(
    monkeypatch, arguments, read_only
):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    target = _pr_target(
        "https://github.com/owner/repo/pull/544", "#544", "Show the work target"
    )
    _fake_github_service(
        monkeypatch,
        person,
        pr_comment={"id": "issuecomment-1", "target": target},
    )

    result = CliRunner().invoke(
        member_module.member,
        [*arguments, "--person", "aiko", "--format", "json"],
        input="Looks good.\n",
    )

    assert result.exit_code == 0, result.output
    [(action, attributes)] = _work_target_records()
    assert action == ("inspected" if read_only else "")
    assert attributes["target.title"] == "Show the work target"
    assert attributes["target.label"] == "#544"
    # A read is diagnostics only: it never becomes shared activity.
    assert _domain_event_records("type") == []
