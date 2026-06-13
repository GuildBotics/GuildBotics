import importlib
import os

from click.testing import CliRunner

from guildbotics.capabilities.task_runs import TaskRunStore
from guildbotics.entities.team import Person, Project, Team
from guildbotics.utils.env_loader import GUILDBOTICS_ENV_FILE
from guildbotics.utils.workspace_state import (
    GUILDBOTICS_CONFIG_DIR,
    write_active_workspace,
)

member_module = importlib.import_module("guildbotics.cli.member")


class FakeContext:
    def __init__(self, person):
        self.person = person
        self.team = Team(project=Project(name="demo"), members=[person])


def test_member_context_outputs_no_secret(monkeypatch):
    person = Person(
        person_id="aiko",
        name="Aiko",
        person_type="human",
        profile={"bio": "developer"},
        account_info={"github_username": "aiko-gh"},
    )

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

    assert result.exit_code == 0
    assert '"person_id": "aiko"' in result.output
    assert '"credential_status": "unchecked"' in result.output
    assert "token" not in result.output.lower()


def test_member_context_uses_active_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(GUILDBOTICS_CONFIG_DIR, raising=False)
    monkeypatch.delenv(GUILDBOTICS_ENV_FILE, raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("WORKSPACE_MARKER=loaded\n", encoding="utf-8")
    write_active_workspace(workspace)

    person = Person(person_id="aiko", name="Aiko", person_type="human")

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

    person = Person(person_id="aiko", name="Aiko", person_type="human")

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


def test_member_context_check_credentials_fail_closed(monkeypatch):
    person = Person(
        person_id="aiko",
        name="Aiko",
        person_type="human",
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
