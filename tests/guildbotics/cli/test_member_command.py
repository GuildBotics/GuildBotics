import importlib
import json
import os
from contextlib import ExitStack
from datetime import UTC, datetime

import click
import pytest
from click.testing import CliRunner

from guildbotics.capabilities import member_activity_events
from guildbotics.capabilities.member_memory_audit import MemoryAuditStore
from guildbotics.capabilities.member_reference import command_summaries
from guildbotics.capabilities.task_runs import TaskRunStore
from guildbotics.entities.team import Person, Project, Team
from guildbotics.observability.activity_event_store import ActivityEventStore
from guildbotics.observability.diagnostics_store import DiagnosticsStore
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.runtime.member_invocation import (
    MemberInvocation,
    member_invocation_scope,
)
from guildbotics.runtime.person_lease import PersonExecutionLease
from guildbotics.observability.interactive_sessions import (
    InteractiveSessionStore,
    InteractiveTraceSession,
)
from guildbotics.sync.local_repository import LocalSyncRepository
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT
from guildbotics.utils.workspace_state import (
    GUILDBOTICS_CONFIG_DIR,
    write_active_workspace,
)

member_module = importlib.import_module("guildbotics.cli.member")


def test_member_command_reports_pending_when_one_shot_lock_is_busy(monkeypatch, capsys):
    monkeypatch.setattr(member_module, "_member_command_needs_lease", lambda: True)
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


@pytest.fixture
def bind_invocation():
    """Bind a member invocation for the rest of the test, as the broker does."""
    with ExitStack() as stack:

        def bind(**fields) -> None:
            stack.enter_context(member_invocation_scope(MemberInvocation(**fields)))

        yield bind


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

    def clone_for(self, person):
        return FakeContext(person)


def _use_real_member_resolution(monkeypatch, *members):
    """Run member commands through the real person resolution for a fake team."""
    from guildbotics.runtime import member_context as member_context_module

    team = Team(project=Project(name="demo"), members=list(members))
    base_context = FakeContext(members[0])
    base_context.team = team

    class FakeEdition:
        def get_context(self):
            return base_context

    monkeypatch.setattr(member_context_module, "get_edition", lambda: FakeEdition())


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
def _isolate_member_data_root(monkeypatch, tmp_path):
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
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
                "agent",
                "conversation",
                "reset",
                "--person",
                "hana",
                "--adapter",
                "codex",
                "--work-kind",
                "ticket",
                "--work-identity",
                "issue-1",
            ],
            id="agent",
        ),
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
                "github",
                "issue",
                "inspect",
                "--person",
                "hana",
                "--url",
                "https://github.com/owner/repo/issues/1",
            ],
            id="github",
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

    result = CliRunner().invoke(member_module.member, argv)

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
    assert "guildbotics member agent conversation reset" in result.output
    assert "### Rules" in result.output


def test_member_command_lease_classification_uses_callback_metadata() -> None:
    @click.command(name="context")
    def unmarked_command() -> None:
        """A write-capable command whose name resembles a read-only command."""

    @click.command(name="write-looking-command")
    @member_module._read_only_member_command
    def read_only_command() -> None:
        """A read-only command whose name does not imply its access mode."""

    with click.Context(unmarked_command):
        assert member_module._member_command_needs_lease() is True
    with click.Context(read_only_command):
        assert member_module._member_command_needs_lease() is False


def test_ci_inspection_commands_are_read_only() -> None:
    github = member_module.member.commands["github"]
    pr = github.commands["pr"]
    run = github.commands["run"]
    artifact = run.commands["artifact"]

    for command in (pr.commands["checks"], artifact.commands["download"]):
        with click.Context(command):
            assert member_module._member_command_needs_lease() is False


def test_member_agent_conversation_reset_rotates_exact_session(monkeypatch, tmp_path):
    from guildbotics.intelligences.agent_runtime.models import (
        ConversationKey,
        ResumePolicy,
    )
    from guildbotics.intelligences.agent_runtime.store import ConversationStore

    person = Person(person_id="aiko", name="Aiko", person_type="agent")

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    data_root = tmp_path / "data"
    monkeypatch.setattr(member_module, "get_workspace_root", lambda: data_root)
    key = ConversationKey("aiko", "codex", "ticket", "issue-300")
    store = ConversationStore(data_root)
    record = store.resolve(key, ResumePolicy.AUTO)
    record.provider_session_id = "thread-1"
    store.save(record)

    result = CliRunner().invoke(
        member_module.member,
        [
            "--workspace",
            str(tmp_path),
            "agent",
            "conversation",
            "reset",
            "--person",
            "aiko",
            "--adapter",
            "codex",
            "--work-kind",
            "ticket",
            "--work-identity",
            "issue-300",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["reset"] is True
    assert payload["generation"] == 1
    persisted = store.load(key)
    assert persisted is not None
    assert persisted.provider_session_id == ""
    assert persisted.rotation_reason == "reset"


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
        [
            "agent",
            "conversation",
            "reset",
            "--person",
            "aiko",
            "--adapter",
            "codex",
            "--work-kind",
            "manual",
            "--work-identity",
            "forged",
        ],
    )

    assert result.exit_code != 0
    assert "execution lease delegation is invalid" in result.output


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
            "--ticket",
            "https://example.test/issues/1",
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
            event_type="github.work_target",
            payload={},
            attributes={
                "github.action": "inspected",
                "github.kind": "pull_request",
                "github.title": "利用枠の表示",
                "interactive.host": "codex",
            },
        )
        return {}

    async def failing():
        raise RuntimeError("boom")

    member_module._run_interactive(inspect_pr(), session, "member github pr inspect")
    with pytest.raises(RuntimeError):
        member_module._run_interactive(failing(), session, "member git push")

    record = json.loads(
        (tmp_path / ".guildbotics/state/sessions/trace-target.json").read_text(
            encoding="utf-8"
        )
    )
    assert record["command"] == "member github pr inspect"
    assert record["status"] == "failed"
    # Only the targets are shared: the session's host attributes describe
    # this machine.
    assert record["attributes"] == {
        "github.action": "inspected",
        "github.kind": "pull_request",
        "github.title": "利用枠の表示",
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
                "capabilities": "### GitHub\n- `guildbotics member github pr create ...`",
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)
    runner = CliRunner()

    result = runner.invoke(member_module.member, ["context", "--person", "aiko"])

    assert result.exit_code == 0
    assert "## Member Capabilities" in result.output
    assert "guildbotics member github pr create" in result.output


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
                    "github_comments": "Comment as Aiko.",
                    "neutral_documents": "Use neutral documents.",
                    "machine_outputs": "Keep JSON factual.",
                },
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)
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
    monkeypatch.delenv(GUILDBOTICS_CONFIG_DIR, raising=False)
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
    monkeypatch.delenv(GUILDBOTICS_CONFIG_DIR, raising=False)
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
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv(GUILDBOTICS_CONFIG_DIR, raising=False)
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
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)

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
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv(GUILDBOTICS_CONFIG_DIR, raising=False)
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
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)

    result = CliRunner().invoke(
        member_module.member, ["context", "--person", "aiko", "--format", "json"]
    )

    assert result.exit_code == 0
    assert "CWD_ONLY_MARKER" not in os.environ


def test_member_context_check_credentials_fail_closed(monkeypatch):
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
            raise KeyError("AIKO_GITHUB_ACCESS_TOKEN")

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        ["context", "--person", "aiko", "--check-credentials", "--format", "json"],
    )

    assert result.exit_code != 0
    assert "Member credential could not be resolved" in result.output
    assert "GITHUB_ACCESS_TOKEN" not in result.output


def test_member_write_command_requires_content_source():
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "github",
            "issue",
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
    "chat reply --person aiko --channel-id C1 --thread-ts 100.1",
    "chat noop --person aiko --run-id run-1 --channel-id C1 "
    "--thread-ts 100.1 --event-id E1",
    "chat complete --person aiko --run-id run-1 --channel-id C1 "
    "--thread-ts 100.1 --event-id E1 --status done",
    "git commit --person aiko --repo-path .",
    "git publish --person aiko --repo-path .",
    "github issue comment --person aiko --url https://github.com/owner/repo/issues/1",
    "github issue create --person aiko --repo owner/repo --title Title",
    "github pr create --person aiko --repo owner/repo --head feature --title Title",
    "github pr comment --person aiko --url https://github.com/owner/repo/pull/1",
    "github pr review-comment --person aiko "
    "--url https://github.com/owner/repo/pull/1 --path file.py --line 1",
    "github pr reply --person aiko --url https://github.com/owner/repo/pull/1 "
    "--reply-target-id 1",
    "task complete --person aiko --run-id run-1 "
    "--ticket-url https://github.com/owner/repo/issues/1 --status done",
)


@pytest.mark.parametrize("command", CONTENT_COMMANDS)
def test_required_content_commands_reject_empty_stdin(command):
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
        "github issue comment",
        "github issue create",
        "github issue update",
        "github pr create",
        "github pr update",
        "github pr comment",
        "github pr review-comment",
        "github pr reply",
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

    missing = tmp_path / "missing.txt"
    result = CliRunner().invoke(command, ["--content-file", str(missing)])

    assert result.exit_code != 0
    prefix = member_module.t(
        "cli.member.content.file_read_failed", path=missing, error=""
    )
    assert prefix in result.output


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
        "github issue create --person aiko --repo owner/repo",
        "github pr create --person aiko --repo owner/repo --head feature",
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


def test_member_github_issue_commands_pass_content_stdin(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = []
    activity_calls = []

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def issue_comment(self, issue_url, body):
            calls.append(("comment", issue_url, body))
            return {
                "comment_id": 1,
                "comment_url": f"{issue_url}#issuecomment-1",
                "issue_number": 1,
                "repo": "owner/repo",
                "issue_url": issue_url,
            }

        async def issue_create(
            self, repo, title, body, add_to_project, labels, human_approved
        ):
            calls.append(
                ("create", repo, title, body, add_to_project, labels, human_approved)
            )
            return {
                "issue_number": 2,
                "issue_title": title,
                "repo": repo,
                "issue_url": "https://github.com/owner/repo/issues/2",
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)
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
            "github",
            "issue",
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
            "github",
            "issue",
            "create",
            "--person",
            "aiko",
            "--repo",
            "owner/repo",
            "--title",
            "Issue title",
            "--label",
            "priority: high",
            "--no-add-to-project",
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
            ["priority: high"],
            True,
        ),
    ]
    assert [call[0] for call in activity_calls] == ["comment", "create"]
    assert all(call[1] is person for call in activity_calls)
    assert activity_calls[0][2]["issue_url"] == "https://github.com/owner/repo/issues/1"
    assert activity_calls[1][2]["issue_title"] == "Issue title"


def test_member_github_issue_api_failures_do_not_record_activity(monkeypatch):
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

        async def issue_comment(self, _issue_url, _body):
            raise member_module.MemberCapabilityError("GitHub API failed")

        async def issue_create(
            self, _repo, _title, _body, _add_to_project, _labels, _human_approved
        ):
            raise member_module.MemberCapabilityError("GitHub API failed")

        async def aclose(self):
            pass

    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)
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
            "github",
            "issue",
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
            "github",
            "issue",
            "create",
            "--person",
            "aiko",
            "--repo",
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


def test_member_git_publish_current_mode_uses_current_workspace_service(
    monkeypatch, tmp_path
):
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

        async def aclose(self):
            calls["closed"] = True

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
    assert calls["closed"] is True
    assert '"status": "published"' in result.output


def test_member_git_commit_current_mode_uses_current_workspace_service(
    monkeypatch, tmp_path
):
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
            calls["repo_path"] = repo_path
            calls["message"] = message
            calls["workspace_mode"] = workspace_mode
            calls["cwd"] = cwd
            return FakeResult()

        async def aclose(self):
            calls["closed"] = True

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
    assert calls["closed"] is True
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


def test_member_git_prepare_rejects_repo_combined_with_url():
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "git",
            "prepare",
            "--person",
            "aiko",
            "--repo",
            "acme/widget",
            "--branch",
            "chat/fix-typo",
            "--issue-url",
            "https://github.com/acme/widget/issues/1",
        ],
    )

    assert result.exit_code != 0
    assert "--repo cannot be combined with --issue-url or --pr-url." in result.output


def test_member_git_prepare_rejects_repo_without_branch():
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        ["git", "prepare", "--person", "aiko", "--repo", "acme/widget"],
    )

    assert result.exit_code != 0
    assert "--repo requires --branch." in result.output


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

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

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

        async def aclose(self):
            calls["closed"] = True

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
    assert calls["closed"] is True
    assert '"status": "pushed"' in result.output
    assert _domain_event_records("type", "person_id", "payload") == [
        {
            "type": "github.push",
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


def test_member_github_pr_inspect_passes_include_diff(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def pr_inspect(self, pr_url, include_comments, include_diff):
            calls.update(
                {
                    "pr_url": pr_url,
                    "include_comments": include_comments,
                    "include_diff": include_diff,
                }
            )
            return {
                "repo": "owner/repo",
                "number": 7,
                "files": [
                    {
                        "path": "guildbotics/example.py",
                        "commentable_lines": [{"line": 12, "side": "RIGHT"}],
                    }
                ],
            }

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "github",
            "pr",
            "inspect",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/pull/7",
            "--include-comments",
            "--include-diff",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert calls == {
        "pr_url": "https://github.com/owner/repo/pull/7",
        "include_comments": True,
        "include_diff": True,
        "closed": True,
    }
    payload = json.loads(result.output)
    assert payload["files"][0]["commentable_lines"] == [{"line": 12, "side": "RIGHT"}]


def test_member_github_pr_create_passes_base(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def pr_create(
            self, repo, head, base, title, body, issue_url, draft, closes_issue=False
        ):
            calls.update(
                {
                    "repo": repo,
                    "head": head,
                    "base": base,
                    "title": title,
                    "body": body,
                    "issue_url": issue_url,
                    "draft": draft,
                    "closes_issue": closes_issue,
                }
            )
            return {
                "pr_number": 1,
                "pr_url": "https://github.com/owner/repo/pull/1",
                "created": True,
                "draft": False,
                "head": head,
                "base": base,
            }

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "github",
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
        "issue_url": "https://github.com/owner/repo/issues/42",
        "draft": "false",
        "closes_issue": False,
        "closed": True,
    }
    assert '"base": "ticket-driven-workflow"' in result.output
    assert _domain_event_records("type", "person_id", "payload", "attributes") == [
        {
            "type": "github.pull_request",
            "person_id": "aiko",
            "payload": {
                "action": "opened",
                "pull_request": {
                    "number": 1,
                    "title": "PR title",
                    "html_url": "https://github.com/owner/repo/pull/1",
                    "merged": False,
                },
            },
            "attributes": {
                "github.action": "opened",
                "github.kind": "pull_request",
                "github.number": "1",
                "github.repo": "owner/repo",
                "github.url": "https://github.com/owner/repo/pull/1",
            },
        }
    ]


def test_member_github_pr_create_reads_content_from_stdin(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def pr_create(
            self, repo, head, base, title, body, issue_url, draft, closes_issue=False
        ):
            calls.update(
                {
                    "repo": repo,
                    "head": head,
                    "base": base,
                    "title": title,
                    "body": body,
                    "issue_url": issue_url,
                    "draft": draft,
                    "closes_issue": closes_issue,
                }
            )
            return {
                "pr_number": 1,
                "pr_url": "https://github.com/owner/repo/pull/1",
                "created": True,
                "draft": False,
                "head": head,
                "base": base,
            }

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "github",
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
        "issue_url": "",
        "draft": "false",
        "closes_issue": False,
        "closed": True,
    }
    assert '"base": "ticket-driven-workflow"' in result.output


def test_member_github_pr_create_passes_closes_issue(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def pr_create(
            self, repo, head, base, title, body, issue_url, draft, closes_issue=False
        ):
            calls["closes_issue"] = closes_issue
            calls["issue_url"] = issue_url
            return {
                "pr_number": 1,
                "pr_url": "https://github.com/owner/repo/pull/1",
                "created": True,
                "draft": False,
                "head": head,
                "base": base,
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)

    result = CliRunner().invoke(
        member_module.member,
        [
            "github",
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
            "--closes-issue",
        ],
        input="PR body\n",
    )

    assert result.exit_code == 0
    assert calls == {
        "closes_issue": True,
        "issue_url": "https://github.com/owner/repo/issues/42",
    }


def test_member_github_pr_create_rejects_closes_issue_without_issue_url():
    result = CliRunner().invoke(
        member_module.member,
        [
            "github",
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


def test_member_github_pr_create_rejects_unknown_draft_value():
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "github",
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


def test_member_github_pr_create_rejects_multiline_title():
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "github",
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


def test_member_github_pr_create_rejects_missing_content_source():
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "github",
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


def test_member_github_pr_update_rejects_update_without_any_change():
    result = CliRunner().invoke(
        member_module.member,
        [
            "github",
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
    assert "pr update needs --content-stdin/--content-file or --title." in result.output


def test_member_github_pr_update_reads_entire_stdin_and_closes_service(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def pr_update(self, pr_url, body, title):
            calls.update({"pr_url": pr_url, "body": body, "title": title})
            return {"pr_number": 7, "pr_url": pr_url, "body": body}

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)

    result = CliRunner().invoke(
        member_module.member,
        [
            "github",
            "pr",
            "update",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/pull/7",
            "--content-stdin",
        ],
        input="## Summary\n\nUpdated body\n",
    )

    assert result.exit_code == 0
    assert calls == {
        "pr_url": "https://github.com/owner/repo/pull/7",
        "body": "## Summary\n\nUpdated body\n",
        "title": None,
        "closed": True,
    }


@pytest.mark.parametrize("content", ["", "\n", " \t\n"])
def test_member_github_pr_update_normalizes_blank_stdin_and_records_evidence(
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

        async def pr_update(self, pr_url, body, title):
            calls.update({"pr_url": pr_url, "body": body, "title": title})
            return {
                "pr_number": 7,
                "pr_url": pr_url,
                "body": body,
            }

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)

    result = CliRunner().invoke(
        member_module.member,
        [
            "github",
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
        "closed": True,
    }
    assert json.loads(result.output)["body"] == ""
    assert TaskRunStore().evidence("run-1")[0]["evidence_type"] == "pr_update"


def test_member_github_issue_update_rejects_update_without_any_change():
    result = CliRunner().invoke(
        member_module.member,
        [
            "github",
            "issue",
            "update",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/issues/42",
        ],
        input="This must not be read.",
    )

    assert result.exit_code != 0
    assert "issue update needs --content-stdin" in result.output


@pytest.mark.parametrize(
    ("command", "url_option"),
    [
        (["github", "issue", "update"], "https://github.com/owner/repo/issues/42"),
        (["github", "pr", "update"], "https://github.com/owner/repo/pull/7"),
    ],
)
def test_member_github_update_rejects_an_empty_title(command, url_option):
    result = CliRunner().invoke(
        member_module.member,
        [*command, "--person", "aiko", "--url", url_option, "--title", "   "],
    )

    assert result.exit_code != 0
    assert "title must not be empty." in result.output


def test_member_github_issue_update_rejects_state_reason_without_close():
    result = CliRunner().invoke(
        member_module.member,
        [
            "github",
            "issue",
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


def test_member_github_issue_edit_on_a_closed_issue_records_no_close_activity(
    monkeypatch,
):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    recorded = []

    class FakeService:
        def __init__(self, *_args):
            pass

        async def issue_update(self, issue_url, **_kwargs):
            # An already-closed issue reports state "closed" for any edit; the
            # capability marks the state as unchanged.
            return {
                "issue_number": 42,
                "issue_url": issue_url,
                "repo": "owner/repo",
                "title": "Capability gap",
                "state": "closed",
                "state_changed": False,
                "labels": [],
                "body": "Updated body",
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module,
        "resolve_member_context",
        lambda identifier: (FakeContext(person), person),
    )
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)
    monkeypatch.setattr(
        member_activity_events,
        "record_correlated_event",
        lambda **kwargs: recorded.append(kwargs),
    )

    result = CliRunner().invoke(
        member_module.member,
        [
            "github",
            "issue",
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
    assert recorded == []


def test_member_github_issue_close_passes_approval_and_records_activity(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = {}
    activity_calls = []

    class FakeService:
        def __init__(self, *_args):
            pass

        async def issue_update(self, issue_url, **kwargs):
            calls.update({"issue_url": issue_url, **kwargs})
            return {
                "issue_number": 42,
                "issue_url": issue_url,
                "repo": "owner/repo",
                "title": "Capability gap",
                "state": "closed",
                "state_changed": True,
                "labels": ["cli"],
                "body": "Body",
            }

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module,
        "resolve_member_context",
        lambda identifier: (FakeContext(person), person),
    )
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)
    monkeypatch.setattr(
        member_module,
        "record_member_issue_close_event",
        lambda member, payload: activity_calls.append((member, payload)),
    )

    result = CliRunner().invoke(
        member_module.member,
        [
            "github",
            "issue",
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
        "issue_url": "https://github.com/owner/repo/issues/42",
        "body": None,
        "title": None,
        "add_labels": ["cli"],
        "remove_labels": [],
        "state": "closed",
        "state_reason": "completed",
        "human_approved": True,
        "closed": True,
    }
    assert activity_calls[0][0] is person
    assert activity_calls[0][1]["state"] == "closed"
    assert activity_calls[0][1]["state_changed"] is True


def test_member_github_issue_update_reads_entire_stdin_and_closes_service(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def issue_update(self, issue_url, **kwargs):
            calls.update({"issue_url": issue_url, **kwargs})
            return {
                "issue_number": 42,
                "issue_url": issue_url,
                "body": kwargs["body"],
                "state": "open",
            }

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)

    result = CliRunner().invoke(
        member_module.member,
        [
            "github",
            "issue",
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
        "issue_url": "https://github.com/owner/repo/issues/42",
        "body": "## Summary\n\nUpdated body\n",
        "title": None,
        "add_labels": [],
        "remove_labels": [],
        "state": None,
        "state_reason": None,
        "human_approved": False,
        "closed": True,
    }


@pytest.mark.parametrize("content", ["", "\n", " \t\n"])
def test_member_github_issue_update_normalizes_blank_stdin_and_records_evidence(
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

        async def issue_update(self, issue_url, **kwargs):
            calls.update({"issue_url": issue_url, **kwargs})
            return {
                "issue_number": 42,
                "issue_url": issue_url,
                "body": kwargs["body"],
                "state": "open",
            }

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)

    result = CliRunner().invoke(
        member_module.member,
        [
            "github",
            "issue",
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
        "issue_url": "https://github.com/owner/repo/issues/42",
        "body": "",
        "title": None,
        "add_labels": [],
        "remove_labels": [],
        "state": None,
        "state_reason": None,
        "human_approved": False,
        "closed": True,
    }
    assert json.loads(result.output)["body"] == ""
    assert TaskRunStore().evidence("run-1")[0]["evidence_type"] == "issue_update"


def test_member_github_pr_review_reads_stdin_and_records_evidence(
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

        async def pr_review(self, pr_url, body, event):
            calls.update({"pr_url": pr_url, "body": body, "event": event})
            return {
                "review_id": 555,
                "html_url": "https://github.com/owner/repo/pull/7#pullrequestreview-555",
                "state": "APPROVED",
                "commit_id": "abc123",
                "submitted_at": "2026-01-01T00:00:00Z",
            }

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "github",
            "pr",
            "review",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/pull/7",
            "--event",
            "approve",
            "--content-stdin",
        ],
        input="Nothing blocks this.\n",
    )
    lease.release()

    assert result.exit_code == 0
    assert calls == {
        "pr_url": "https://github.com/owner/repo/pull/7",
        "body": "Nothing blocks this.\n",
        "event": "approve",
        "closed": True,
    }
    payload = json.loads(result.output)
    assert payload["review_id"] == 555
    assert TaskRunStore().evidence("run-1")[0]["evidence_type"] == "pr_review"


def test_member_github_pr_review_rejects_unknown_event():
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "github",
            "pr",
            "review",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/pull/7",
            "--event",
            "lgtm",
            "--content-stdin",
        ],
        input="x\n",
    )

    assert result.exit_code != 0
    assert "Invalid value for '--event'" in result.output


def test_member_github_pr_review_comment_reads_stdin_and_records_evidence(
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
            self, pr_url, body, file_path, line, side, start_line, start_side
        ):
            calls.update(
                {
                    "pr_url": pr_url,
                    "body": body,
                    "file_path": file_path,
                    "line": line,
                    "side": side,
                    "start_line": start_line,
                    "start_side": start_side,
                }
            )
            return {
                "review_comment_id": 123,
                "html_url": "https://github.com/owner/repo/pull/7#discussion_r123",
                "created_at": "2026-01-01T00:00:00Z",
            }

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "github",
            "pr",
            "review-comment",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/pull/7",
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
        "file_path": "guildbotics/example.py",
        "line": 12,
        "side": "RIGHT",
        "start_line": 10,
        "start_side": "RIGHT",
        "closed": True,
    }
    payload = json.loads(result.output)
    assert payload["review_comment_id"] == 123
    assert TaskRunStore().evidence("run-1")[0]["evidence_type"] == ("pr_review_comment")


def test_member_github_pr_review_comment_rejects_partial_range():
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "github",
            "pr",
            "review-comment",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/pull/7",
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


def test_member_task_status_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    store = TaskRunStore()
    store.append_evidence("run-1", "issue_comment", {"comment_id": 1})
    store.complete(
        "run-1",
        "done",
        "summary",
        "https://github.com/owner/repo/issues/1",
        "aiko",
    )
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        ["task", "status", "--person", "aiko", "--run-id", "run-1"],
    )

    assert result.exit_code == 0
    assert '"completed": true' in result.output
    assert '"evidence_types": ["issue_comment"]' in result.output


def test_member_task_status_ignores_missing_context_for_interactive_trace(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-1")

    def missing_context(_identifier):
        raise FileNotFoundError("team/project.yml")

    monkeypatch.setattr(member_module, "resolve_member_context", missing_context)
    store = TaskRunStore()
    store.append_evidence("run-1", "issue_comment", {"comment_id": 1})
    store.complete(
        "run-1",
        "done",
        "summary",
        "https://github.com/owner/repo/issues/1",
        "aiko",
    )
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        ["task", "status", "--person", "aiko", "--run-id", "run-1"],
    )

    assert result.exit_code == 0
    assert '"completed": true' in result.output


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
    store.append_evidence("run-1", "issue_comment", {"comment_id": 1})
    store.complete(
        "run-1",
        "done",
        "summary",
        "https://github.com/owner/repo/issues/1",
        "aiko",
    )
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        ["task", "status", "--person", "aiko", "--run-id", "run-1"],
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
    store = TaskRunStore(workspace / ".guildbotics" / "state" / "task-runs")
    store.append_evidence("run-1", "issue_comment", {"comment_id": 1})
    store.complete(
        "run-1",
        "done",
        "summary",
        "https://github.com/owner/repo/issues/1",
        "aiko",
    )
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "--workspace",
            str(workspace),
            "task",
            "status",
            "--person",
            "aiko",
            "--run-id",
            "run-1",
        ],
    )

    assert result.exit_code == 0
    assert calls["workspace"] == str(workspace.resolve())


def test_member_chat_reply_reads_body_file_and_records_evidence(
    monkeypatch, bind_invocation, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # The run id is injected by the workflow via env, not a CLI flag; the write
    # command records its evidence under the env-provided run id.
    lease = _bind_workflow(bind_invocation, run_id="run-1")
    person = Person(person_id="aiko", name="Aiko")
    context = FakeContext(person)
    context.get_chat_service = lambda: object()

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return context, person

    class FakeService:
        def __init__(self, *_args, **_kwargs):
            pass

        async def reply(self, *, channel_id, channel_name, thread_ts, body):
            return {
                "service": "slack",
                "channel_id": channel_id,
                "channel_name": channel_name,
                "message_ts": "200.1",
                "thread_ts": thread_ts,
                "text": body,
                "posted": True,
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberChatCapabilityService", FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "chat",
            "reply",
            "--person",
            "aiko",
            "--channel-id",
            "C1",
            "--thread-ts",
            "100.1",
            "--content-stdin",
        ],
        input="了解しました。",
    )
    lease.release()

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["text"] == "了解しました。"
    assert TaskRunStore().evidence("run-1")[0]["evidence_type"] == "chat_reply"


def test_member_chat_reply_accepts_channel_name_and_message_url(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    person = Person(person_id="aiko", name="Aiko")
    context = FakeContext(person)
    context.get_chat_service = lambda: object()

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return context, person

    class FakeService:
        def __init__(self, *_args, **_kwargs):
            pass

        async def reply(self, *, channel_id, channel_name, thread_ts, body):
            return {
                "service": "slack",
                "channel_id": channel_id,
                "channel_name": channel_name,
                "message_ts": "200.1",
                "thread_ts": thread_ts,
                "text": body,
                "posted": True,
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberChatCapabilityService", FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "chat",
            "reply",
            "--person",
            "aiko",
            "--channel-name",
            "general",
            "--message-url",
            "https://example.slack.com/archives/C1/p1000000000000001",
            "--content-stdin",
        ],
        input="了解しました。",
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["channel_id"] == "C1"
    assert payload["channel_name"] == "general"
    assert payload["thread_ts"] == "1000000000.000001"


def test_member_chat_inspect_thread_accepts_message_url(monkeypatch, tmp_path):
    expected_limit = 20
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    person = Person(person_id="aiko", name="Aiko")
    context = FakeContext(person)
    context.get_chat_service = lambda: object()

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return context, person

    class FakeService:
        def __init__(self, *_args, **_kwargs):
            pass

        async def inspect_thread(self, *, channel_id, channel_name, thread_ts, limit):
            return {
                "service": "slack",
                "mode": "thread",
                "channel_id": channel_id,
                "channel_name": channel_name or "",
                "thread_ts": thread_ts,
                "next_cursor": "",
                "messages": [{"message_ts": "100.1", "text": "question"}],
                "limit": limit,
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberChatCapabilityService", FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "chat",
            "inspect",
            "thread",
            "--person",
            "aiko",
            "--message-url",
            "https://example.slack.com/archives/C1/p1000000000000002"
            "?thread_ts=1000000000.000001&cid=C1",
            "--limit",
            str(expected_limit),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["channel_id"] == "C1"
    assert payload["thread_ts"] == "1000000000.000001"
    assert payload["limit"] == expected_limit


def test_member_chat_inspect_channel_accepts_channel_name(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    person = Person(person_id="aiko", name="Aiko")
    context = FakeContext(person)
    context.get_chat_service = lambda: object()

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return context, person

    class FakeService:
        def __init__(self, *_args, **_kwargs):
            pass

        async def inspect_channel(
            self, *, channel_id, channel_name, oldest_ts, latest_ts, limit
        ):
            return {
                "service": "slack",
                "mode": "channel",
                "channel_id": channel_id or "C_GENERAL",
                "channel_name": channel_name,
                "oldest_ts": oldest_ts,
                "latest_ts": latest_ts,
                "next_cursor": "",
                "messages": [{"message_ts": "100.1", "text": "topic"}],
                "limit": limit,
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberChatCapabilityService", FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "chat",
            "inspect",
            "channel",
            "--person",
            "aiko",
            "--channel-name",
            "general",
            "--oldest-ts",
            "100.0",
            "--latest-ts",
            "200.0",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["channel_name"] == "general"
    assert payload["oldest_ts"] == "100.0"
    assert payload["latest_ts"] == "200.0"


def test_member_chat_rejects_empty_content():
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "chat",
            "reply",
            "--person",
            "aiko",
            "--channel-id",
            "C1",
            "--thread-ts",
            "100.1",
            "--content-stdin",
        ],
        input="",
    )

    assert result.exit_code != 0
    assert "message body must not be empty" in result.output


def test_member_chat_reaction_accepts_message_url(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    person = Person(person_id="aiko", name="Aiko")
    context = FakeContext(person)
    context.get_chat_service = lambda: object()

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return context, person

    class FakeService:
        def __init__(self, *_args, **_kwargs):
            pass

        async def add_reaction(self, *, channel_id, channel_name, message_ts, reaction):
            return {
                "service": "slack",
                "channel_id": channel_id,
                "channel_name": channel_name,
                "message_ts": message_ts,
                "reaction": reaction,
                "reacted": True,
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberChatCapabilityService", FakeService)
    runner = CliRunner()

    result = runner.invoke(
        member_module.member,
        [
            "chat",
            "reaction",
            "add",
            "--person",
            "aiko",
            "--message-url",
            "https://example.slack.com/archives/C1/p1000000000000002"
            "?thread_ts=1000000000.000001&cid=C1",
            "--reaction",
            "ack",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["channel_id"] == "C1"
    assert payload["message_ts"] == "1000000000.000002"
    assert payload["reaction"] == "ack"


def test_member_chat_noop_and_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
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
            "--run-id",
            "run-1",
            "--channel-id",
            "C1",
            "--thread-ts",
            "100.1",
            "--event-id",
            "E1",
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
            "--run-id",
            "run-1",
            "--channel-id",
            "C1",
            "--thread-ts",
            "100.1",
            "--event-id",
            "E1",
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


def test_member_task_complete_reads_summary_from_stdin(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
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
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)
    TaskRunStore().append_evidence("run-1", "issue_comment", {"comment_id": 1})
    result = CliRunner().invoke(
        member_module.member,
        [
            "task",
            "complete",
            "--person",
            "aiko",
            "--run-id",
            "run-1",
            "--ticket-url",
            "https://github.com/owner/repo/issues/1",
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


def test_member_task_complete_rejects_blocked_pr_readiness(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    person = Person(person_id="aiko", name="Aiko")
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def task_completion_readiness(self, ticket_url, evidence):
            calls.update({"ticket_url": ticket_url, "evidence": evidence})
            raise member_module.MemberCapabilityError(
                "Task cannot be completed because PR readiness is blocked. "
                "https://github.com/owner/repo/pull/7: CI checks are still pending."
            )

        async def aclose(self):
            calls["closed"] = True

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)
    store = TaskRunStore()
    store.append_evidence(
        "run-1",
        "pr_create",
        {"pr_url": "https://github.com/owner/repo/pull/7"},
    )

    result = CliRunner().invoke(
        member_module.member,
        [
            "task",
            "complete",
            "--person",
            "aiko",
            "--run-id",
            "run-1",
            "--ticket-url",
            "https://github.com/owner/repo/issues/1",
            "--status",
            "done",
            "--content-stdin",
        ],
        input="Completed.\n",
    )

    assert result.exit_code != 0
    assert "PR readiness is blocked" in result.output
    assert "CI checks are still pending" in result.output
    assert calls["ticket_url"] == "https://github.com/owner/repo/issues/1"
    assert calls["evidence"][0]["evidence_type"] == "pr_create"
    assert calls["closed"] is True
    with pytest.raises(member_module.TaskRunError, match="not completed"):
        store.status("run-1")


def test_member_task_complete_returns_revalidated_pr_readiness(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    person = Person(person_id="aiko", name="Aiko")
    readiness = {
        "pr_url": "https://github.com/owner/repo/pull/7",
        "head_sha": "abc123",
        "readiness": "ready",
        "completion_blockers": [],
    }

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def task_completion_readiness(self, _ticket_url, _evidence):
            return [readiness]

        async def aclose(self):
            pass

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)
    TaskRunStore().append_evidence(
        "run-1",
        "pr_create",
        {"pr_url": "https://github.com/owner/repo/pull/7"},
    )

    result = CliRunner().invoke(
        member_module.member,
        [
            "task",
            "complete",
            "--person",
            "aiko",
            "--run-id",
            "run-1",
            "--ticket-url",
            "https://github.com/owner/repo/issues/1",
            "--status",
            "done",
            "--content-stdin",
        ],
        input="Completed.\n",
    )

    assert result.exit_code == 0
    assert json.loads(result.output)["pr_readiness"] == [readiness]


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
    from guildbotics.integrations.chat_service import ChatEvent
    from guildbotics.integrations.file_chat_state_store import (
        FileConversationStateStore,
    )

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
            "thread_ts": "100.1",
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
        ChatEvent(
            event_id="E3",
            channel_id="C1",
            message_ts="103.1",
            thread_ts="100.1",
            author_id="U_USER",
            text="訂正です",
        ),
    )
    try:
        result = CliRunner().invoke(
            member_module.member,
            ["chat", "updates", "--person", "aiko", "--run-id", "run-1"],
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
    monkeypatch.setattr(member_module, "MemberGitHubCapabilityService", FakeService)


def _work_target_records():
    return [
        (record["attributes"].get("github.action", ""), record["attributes"])
        for record in DiagnosticsStore().records_between(includes=lambda _: True)
        if record.get("type") == "github.work_target"
    ]


@pytest.mark.parametrize(
    ("arguments", "read_only"),
    [
        (["pr", "inspect", "--url", "https://github.com/owner/repo/pull/544"], True),
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
def test_member_github_commands_declare_their_work_target(
    monkeypatch, arguments, read_only
):
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    target = {
        "kind": "pull_request",
        "repo": "owner/repo",
        "number": 544,
        "title": "Show the work target",
        "html_url": "https://github.com/owner/repo/pull/544",
    }
    _fake_github_service(
        monkeypatch,
        person,
        pr_inspect={"repo": "owner/repo", "number": 544, "target": target},
        pr_comment={"comment_id": 1, "target": target},
    )

    result = CliRunner().invoke(
        member_module.member,
        ["github", *arguments, "--person", "aiko", "--format", "json"],
        input="Looks good.\n",
    )

    assert result.exit_code == 0, result.output
    [(action, attributes)] = _work_target_records()
    assert action == ("inspected" if read_only else "")
    assert attributes["github.title"] == "Show the work target"
    assert attributes["github.number"] == "544"
    # A read is diagnostics only: it never becomes shared activity.
    assert _domain_event_records("type") == []


def test_workflow_member_command_records_into_the_turns_trace(
    monkeypatch, bind_invocation
):
    # The broker hands the workflow's trace over; the command's records then
    # belong to that execution instead of to a trace of their own.
    person = Person(person_id="aiko", name="Aiko", person_type="agent")
    bind_invocation(run_id="run-1", trace_id="trace-parent")
    _fake_github_service(
        monkeypatch,
        person,
        pr_inspect={
            "target": {
                "kind": "pull_request",
                "repo": "owner/repo",
                "number": 544,
                "title": "Show the work target",
                "html_url": "https://github.com/owner/repo/pull/544",
            }
        },
    )

    result = CliRunner().invoke(
        member_module.member,
        [
            "github",
            "pr",
            "inspect",
            "--person",
            "aiko",
            "--url",
            "https://github.com/owner/repo/pull/544",
        ],
    )

    assert result.exit_code == 0, result.output
    summary = DiagnosticsStore().get_summary("trace-parent")
    assert summary is not None
    assert summary["title"] == "Show the work target"
    assert summary["person_id"] == "aiko"
    # The turn's owner records the boundary; the member command must not.
    assert [
        item["type"] for item in DiagnosticsStore().get_records("trace-parent")
    ] == ["github.work_target"]
