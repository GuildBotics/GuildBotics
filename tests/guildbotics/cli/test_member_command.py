import importlib
import json
import os

import pytest
from click.testing import CliRunner

from guildbotics.capabilities.member_memory_audit import MemoryAuditStore
from guildbotics.capabilities.task_runs import TaskRunStore
from guildbotics.entities.team import Person, Project, Team
from guildbotics.observability.diagnostics_store import DiagnosticsStore
from guildbotics.utils.env_loader import GUILDBOTICS_ENV_FILE
from guildbotics.utils.fileio import GUILDBOTICS_DATA_DIR
from guildbotics.utils.workspace_state import (
    GUILDBOTICS_CONFIG_DIR,
    write_active_workspace,
)

member_module = importlib.import_module("guildbotics.cli.member")


class FakeContext:
    def __init__(self, person):
        self.person = person
        self.team = Team(project=Project(name="demo"), members=[person])
        self.logger = None


@pytest.fixture(autouse=True)
def _isolate_member_data_root(monkeypatch, tmp_path):
    monkeypatch.setenv(GUILDBOTICS_DATA_DIR, str(tmp_path / "data"))
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


def test_member_context_rejects_human_member(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="human")

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    runner = CliRunner()

    result = runner.invoke(
        member_module.member, ["context", "--person", "aiko", "--format", "json"]
    )

    assert result.exit_code != 0
    assert "cannot be used as an AI execution subject" in result.output


def test_member_help_prints_capability_reference():
    runner = CliRunner()

    result = runner.invoke(member_module.member, ["help"])

    assert result.exit_code == 0
    assert "guildbotics member git commit" in result.output
    assert "guildbotics member chat reply" in result.output
    assert "guildbotics member memory recall" in result.output
    assert "### Rules" in result.output


def test_member_memory_record_and_recall_cli(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="human")

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
            "--body-stdin",
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


def test_member_cli_reuses_trace_for_interactive_session(monkeypatch):
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
            "--body-stdin",
        ],
        input="Trace body.\n",
    )
    assert record.exit_code == 0

    recall = runner.invoke(
        member_module.member,
        [
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
    assert [item["type"] for item in records] == [
        "member.command.started",
        "member.command.finished",
        "member.command.started",
        "member.command.finished",
    ]
    assert {item["command"] for item in records} == {
        "member memory recall",
        "member memory record",
    }
    memory_events = MemoryAuditStore().list_events(trace_id=trace_id)
    assert {event["type"] for event in memory_events} == {
        "memory.recall",
        "memory.record",
    }


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


def test_member_context_uses_active_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(GUILDBOTICS_CONFIG_DIR, raising=False)
    monkeypatch.delenv(GUILDBOTICS_ENV_FILE, raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("WORKSPACE_MARKER=loaded\n", encoding="utf-8")
    write_active_workspace(workspace)

    person = Person(person_id="aiko", name="Aiko", person_type="agent")

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        assert os.environ[GUILDBOTICS_CONFIG_DIR] == str(
            workspace.resolve() / ".guildbotics" / "config"
        )
        assert os.environ[GUILDBOTICS_ENV_FILE] == str(workspace.resolve() / ".env")
        assert os.environ["WORKSPACE_MARKER"] == "loaded"
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
    monkeypatch.delenv(GUILDBOTICS_CONFIG_DIR, raising=False)
    monkeypatch.delenv(GUILDBOTICS_ENV_FILE, raising=False)
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
    monkeypatch.delenv(GUILDBOTICS_CONFIG_DIR, raising=False)
    monkeypatch.delenv(GUILDBOTICS_ENV_FILE, raising=False)
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
    assert GUILDBOTICS_ENV_FILE not in os.environ


def test_member_active_workspace_without_env_does_not_load_cwd_env(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(GUILDBOTICS_CONFIG_DIR, raising=False)
    monkeypatch.delenv(GUILDBOTICS_ENV_FILE, raising=False)
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
    assert GUILDBOTICS_ENV_FILE not in os.environ


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


def test_member_write_command_requires_existing_body_file(tmp_path):
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
            "--body-file",
            str(tmp_path / "missing.md"),
        ],
    )

    assert result.exit_code != 0
    assert "body-file does not exist" in result.output


def test_member_git_publish_current_mode_uses_current_workspace_service(
    monkeypatch, tmp_path
):
    person = Person(person_id="aiko", name="Aiko", person_type="human")
    message_file = tmp_path / "message.txt"
    message_file.write_text("publish\n", encoding="utf-8")
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
            "--message-file",
            str(message_file),
            "--workspace-mode",
            "current",
        ],
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
    person = Person(person_id="aiko", name="Aiko", person_type="human")
    message_file = tmp_path / "message.txt"
    message_file.write_text("commit\n", encoding="utf-8")
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
            "--message-file",
            str(message_file),
            "--workspace-mode",
            "current",
        ],
    )

    assert result.exit_code == 0
    assert calls["repo_path"] == repo_path
    assert calls["message"] == "commit\n"
    assert calls["workspace_mode"] == "current"
    assert calls["cwd"].is_absolute()
    assert calls["closed"] is True
    assert '"status": "committed"' in result.output


def test_member_git_commit_reads_message_from_stdin(monkeypatch, tmp_path):
    person = Person(person_id="aiko", name="Aiko", person_type="human")
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
            "--message-stdin",
            "--workspace-mode",
            "current",
        ],
        input="日本語のコミットメッセージ\n",
    )

    assert result.exit_code == 0
    assert calls["message"] == "日本語のコミットメッセージ\n"


def test_member_git_prepare_rejects_missing_anchor():
    runner = CliRunner()

    result = runner.invoke(
        member_module.member, ["git", "prepare", "--person", "aiko"]
    )

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


def test_member_git_commit_rejects_missing_message_source(tmp_path):
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
    assert "Either --message-file or --message-stdin is required" in result.output


def test_member_git_commit_rejects_multiple_message_sources(tmp_path):
    runner = CliRunner()
    message_file = tmp_path / "message.txt"
    message_file.write_text("message\n", encoding="utf-8")

    result = runner.invoke(
        member_module.member,
        [
            "git",
            "commit",
            "--person",
            "aiko",
            "--repo-path",
            str(tmp_path),
            "--message-file",
            str(message_file),
            "--message-stdin",
        ],
        input="message\n",
    )

    assert result.exit_code != 0
    assert "Use either --message-file or --message-stdin" in result.output


def test_member_git_push_current_mode_uses_current_workspace_service(
    monkeypatch, tmp_path
):
    person = Person(person_id="aiko", name="Aiko", person_type="human")
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
    events = DiagnosticsStore().records_between(includes=lambda _timestamp: True)
    assert [
        {
            "type": event["type"],
            "person_id": event["person_id"],
            "payload": event["payload"],
        }
        for event in events
    ] == [
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


def test_member_git_publish_current_mode_rejects_workflow_task_run(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("GUILDBOTICS_TASK_RUN_ID", "run-1")
    message_file = tmp_path / "message.txt"
    message_file.write_text("publish\n", encoding="utf-8")

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
            "--message-file",
            str(message_file),
            "--workspace-mode",
            "current",
        ],
    )

    assert result.exit_code != 0
    assert "only for interactive use" in result.output


def test_member_github_pr_inspect_passes_include_diff(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="human")
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
    assert payload["files"][0]["commentable_lines"] == [
        {"line": 12, "side": "RIGHT"}
    ]


def test_member_github_pr_create_passes_base(monkeypatch, tmp_path):
    person = Person(person_id="aiko", name="Aiko", person_type="human")
    title_file = tmp_path / "title.txt"
    body_file = tmp_path / "body.md"
    title_file.write_text("PR title\n", encoding="utf-8")
    body_file.write_text("PR body\n", encoding="utf-8")
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def pr_create(self, repo, head, base, title, body, issue_url, draft):
            calls.update(
                {
                    "repo": repo,
                    "head": head,
                    "base": base,
                    "title": title,
                    "body": body,
                    "issue_url": issue_url,
                    "draft": draft,
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
            "--title-file",
            str(title_file),
            "--body-file",
            str(body_file),
            "--issue-url",
            "https://github.com/owner/repo/issues/42",
            "--draft",
            "false",
        ],
    )

    assert result.exit_code == 0
    assert calls == {
        "repo": "owner/repo",
        "head": "feature",
        "base": "ticket-driven-workflow",
        "title": "PR title\n",
        "body": "PR body\n",
        "issue_url": "https://github.com/owner/repo/issues/42",
        "draft": "false",
        "closed": True,
    }
    assert '"base": "ticket-driven-workflow"' in result.output
    events = DiagnosticsStore().records_between(includes=lambda _timestamp: True)
    assert [
        {
            "type": event["type"],
            "person_id": event["person_id"],
            "payload": event["payload"],
            "attributes": event["attributes"],
        }
        for event in events
    ] == [
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
                "github.number": 1,
                "github.repo": "owner/repo",
                "github.url": "https://github.com/owner/repo/pull/1",
            },
        }
    ]


def test_member_github_pr_create_reads_content_from_stdin(monkeypatch):
    person = Person(person_id="aiko", name="Aiko", person_type="human")
    calls = {}

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    class FakeService:
        def __init__(self, *_args):
            pass

        async def pr_create(self, repo, head, base, title, body, issue_url, draft):
            calls.update(
                {
                    "repo": repo,
                    "head": head,
                    "base": base,
                    "title": title,
                    "body": body,
                    "issue_url": issue_url,
                    "draft": draft,
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
            "--content-stdin",
        ],
        input="PR title\n\n## Summary\n\nPR body\n",
    )

    assert result.exit_code == 0
    assert calls == {
        "repo": "owner/repo",
        "head": "feature",
        "base": "ticket-driven-workflow",
        "title": "PR title",
        "body": "## Summary\n\nPR body\n",
        "issue_url": "",
        "draft": "auto",
        "closed": True,
    }
    assert '"base": "ticket-driven-workflow"' in result.output


def test_member_github_pr_create_rejects_mixed_content_sources(tmp_path):
    title_file = tmp_path / "title.txt"
    title_file.write_text("PR title\n", encoding="utf-8")
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
            "--title-file",
            str(title_file),
            "--content-stdin",
        ],
        input="PR title\n\nPR body\n",
    )

    assert result.exit_code != 0
    assert "Use either --content-stdin or both --title-file and --body-file" in (
        result.output
    )


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
        ],
    )

    assert result.exit_code != 0
    assert "Either --content-stdin or both --title-file and --body-file" in (
        result.output
    )


def test_member_github_pr_review_comment_reads_body_file_and_records_evidence(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("GUILDBOTICS_RUN_ID", "run-1")
    person = Person(person_id="aiko", name="Aiko", person_type="human")
    body_file = tmp_path / "comment.md"
    body_file.write_text("Please simplify this branch.\n", encoding="utf-8")
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
            "--body-file",
            str(body_file),
        ],
    )

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
    assert TaskRunStore().evidence("run-1")[0]["evidence_type"] == (
        "pr_review_comment"
    )


def test_member_github_pr_review_comment_rejects_partial_range(tmp_path):
    body_file = tmp_path / "comment.md"
    body_file.write_text("Please simplify this branch.\n", encoding="utf-8")
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
            "--body-file",
            str(body_file),
        ],
    )

    assert result.exit_code != 0
    assert "--start-line and --start-side must be provided together" in result.output


def test_member_task_status_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
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
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-1")
    monkeypatch.setenv("GUILDBOTICS_TASK_RUN_ID", "run-1")

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


def test_member_chat_reply_reads_body_file_and_records_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    # The run id is injected by the workflow via env, not a CLI flag; the write
    # command records its evidence under the env-provided run id.
    monkeypatch.setenv("GUILDBOTICS_RUN_ID", "run-1")
    person = Person(person_id="aiko", name="Aiko")
    context = FakeContext(person)
    context.get_chat_service = lambda: object()
    body_file = tmp_path / "reply.md"
    body_file.write_text("了解しました。", encoding="utf-8")

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
            "--body-file",
            str(body_file),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["text"] == "了解しました。"
    assert TaskRunStore().evidence("run-1")[0]["evidence_type"] == "chat_reply"


def test_member_chat_reply_accepts_channel_name_and_message_url(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    person = Person(person_id="aiko", name="Aiko")
    context = FakeContext(person)
    context.get_chat_service = lambda: object()
    body_file = tmp_path / "reply.md"
    body_file.write_text("了解しました。", encoding="utf-8")

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
            "--body-file",
            str(body_file),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["channel_id"] == "C1"
    assert payload["channel_name"] == "general"
    assert payload["thread_ts"] == "1000000000.000001"


def test_member_chat_inspect_thread_accepts_message_url(monkeypatch, tmp_path):
    expected_limit = 20
    monkeypatch.setenv("HOME", str(tmp_path))
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


def test_member_chat_body_file_and_stdin_are_exclusive(tmp_path):
    body_file = tmp_path / "reply.md"
    body_file.write_text("hello", encoding="utf-8")
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
            "--body-file",
            str(body_file),
            "--body-stdin",
        ],
        input="hello",
    )

    assert result.exit_code != 0
    assert "not both" in result.output


def test_member_chat_reaction_accepts_message_url(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
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
    person = Person(person_id="aiko", name="Aiko")

    def fake_resolve_member_context(identifier):
        assert identifier == "aiko"
        return FakeContext(person), person

    monkeypatch.setattr(
        member_module, "resolve_member_context", fake_resolve_member_context
    )
    reason_file = tmp_path / "reason.md"
    reason_file.write_text("Not relevant.", encoding="utf-8")
    summary_file = tmp_path / "summary.md"
    summary_file.write_text("No response needed.", encoding="utf-8")
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
            "--reason-file",
            str(reason_file),
        ],
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
            "--summary-file",
            str(summary_file),
        ],
    )

    assert noop.exit_code == 0
    assert complete.exit_code == 0
    payload = json.loads(complete.output)
    assert payload["subject_id"] == "slack:C1:100.1:E1"
    assert payload["evidence_types"] == ["chat_noop"]
