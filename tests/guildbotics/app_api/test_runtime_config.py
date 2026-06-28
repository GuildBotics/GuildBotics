"""Direct unit tests for the config / workspace / team / prompt-trace /
cli-agent-detection behaviors of :class:`guildbotics.app_api.runtime.AppRuntime`.

These exercise finer branch granularity than the API-level tests in
``test_api.py``: each method is driven directly with ``tmp_path`` and
``monkeypatch`` so that env, cwd, HOME, and scheduler interactions are isolated
and never touch the real home directory or network.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.models import (
    CliAgentInfo,
    ProjectStatusOptionsRequest,
    PromptTraceUpdateRequest,
    RuntimeDebugUpdateRequest,
)
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.entities import Person, Project, Team
from guildbotics.utils.env_loader import GUILDBOTICS_ENV_FILE
from guildbotics.utils.fileio import GUILDBOTICS_DATA_DIR
from guildbotics.utils.workspace_state import (
    GUILDBOTICS_CONFIG_DIR,
    active_workspace_file,
)

HTTP_BAD_REQUEST = 400
TRACE_EVENT_TOTAL = 5
TRACE_EVENT_LIMIT = 2


class _FakeContext:
    def __init__(self, members: list[Person]) -> None:
        self.team = Team(project=Project(name="demo"), members=members)

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_fetch_status_options_incomplete_identity_is_unavailable() -> None:
    runtime = AppRuntime(EventBus())
    result = await runtime.fetch_project_status_options(
        ProjectStatusOptionsRequest(owner="", project_id="", github_project_url="")
    )
    assert result.available is False
    assert result.statuses == []


@pytest.mark.asyncio
async def test_fetch_status_options_reads_live_with_member_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = Person(
        person_id="alice",
        name="Alice",
        is_active=True,
        account_info={"github_username": "alice"},
    )
    monkeypatch.setattr(
        AppRuntime, "_get_context", lambda self, message="": _FakeContext([member])
    )

    class _FakeTicketManager:
        def __init__(self, logger: object, person: Person, team: Team) -> None:
            self.client = None
            self.person = person

        async def get_statuses(self) -> list[str]:
            return ["Todo", "In Progress", "Done"]

    monkeypatch.setattr(
        "guildbotics.app_api.runtime.GitHubTicketManager", _FakeTicketManager
    )

    runtime = AppRuntime(EventBus())
    result = await runtime.fetch_project_status_options(
        ProjectStatusOptionsRequest(
            owner="acme",
            project_id="9",
            github_project_url="https://github.com/orgs/acme/projects/9",
        )
    )
    assert result.available is True
    assert result.statuses == ["Todo", "In Progress", "Done"]


def _agent_request() -> ProjectStatusOptionsRequest:
    return ProjectStatusOptionsRequest(
        owner="acme",
        project_id="9",
        github_project_url="https://github.com/orgs/acme/projects/9",
    )


def _patch_agent_ticket_manager(
    monkeypatch: pytest.MonkeyPatch, *, state: dict, record: dict | None = None
) -> None:
    member = Person(
        person_id="alice",
        name="Alice",
        is_active=True,
        account_info={"github_username": "alice"},
    )
    monkeypatch.setattr(
        AppRuntime, "_get_context", lambda self, message="": _FakeContext([member])
    )

    class _FakeTicketManager:
        def __init__(self, logger: object, person: Person, team: Team) -> None:
            self.client = None

        async def get_agent_field_state(self) -> dict:
            return state

        async def sync_agent_field(self) -> dict:
            if record is not None:
                record["synced"] = True
            return state

    monkeypatch.setattr(
        "guildbotics.app_api.runtime.GitHubTicketManager", _FakeTicketManager
    )


@pytest.mark.asyncio
async def test_fetch_agent_field_state_incomplete_identity_is_unavailable() -> None:
    runtime = AppRuntime(EventBus())
    result = await runtime.fetch_agent_field_state(
        ProjectStatusOptionsRequest(owner="", project_id="", github_project_url="")
    )
    assert result.available is False
    assert result.exists is False
    assert result.options == []
    assert result.missing == []


@pytest.mark.asyncio
async def test_fetch_agent_field_state_maps_live_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_agent_ticket_manager(
        monkeypatch,
        state={
            "exists": True,
            "options": [{"name": "⚙bot1", "description": "Bot One"}],
            "missing": [{"name": "⚙bot2", "description": "Bot Two"}],
        },
    )
    runtime = AppRuntime(EventBus())
    result = await runtime.fetch_agent_field_state(_agent_request())

    assert result.available is True
    assert result.exists is True
    assert [o.name for o in result.options] == ["⚙bot1"]
    assert result.missing[0].description == "Bot Two"


@pytest.mark.asyncio
async def test_agent_field_skips_member_without_github_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An active member without a GitHub username makes GitHubTicketManager.__init__
    # raise; the helper must skip it and try the next credentialed member instead
    # of surfacing a 500.
    bad = Person(person_id="bot", name="Bot", is_active=True, account_info={})
    good = Person(
        person_id="alice",
        name="Alice",
        is_active=True,
        account_info={"github_username": "alice"},
    )
    monkeypatch.setattr(
        AppRuntime, "_get_context", lambda self, message="": _FakeContext([bad, good])
    )

    class _FakeTicketManager:
        def __init__(self, logger: object, person: Person, team: Team) -> None:
            self.client = None
            if not person.account_info.get("github_username"):
                raise ValueError("github username required")

        async def get_agent_field_state(self) -> dict:
            return {"exists": True, "options": [], "missing": []}

    monkeypatch.setattr(
        "guildbotics.app_api.runtime.GitHubTicketManager", _FakeTicketManager
    )

    runtime = AppRuntime(EventBus())
    result = await runtime.fetch_agent_field_state(_agent_request())

    assert result.available is True
    assert result.exists is True


@pytest.mark.asyncio
async def test_ensure_agent_field_syncs_and_returns_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record: dict = {}
    _patch_agent_ticket_manager(
        monkeypatch,
        state={
            "exists": True,
            "options": [{"name": "⚙bot1", "description": "Bot One"}],
            "missing": [],
        },
        record=record,
    )
    runtime = AppRuntime(EventBus())
    result = await runtime.ensure_agent_field(_agent_request())

    assert record.get("synced") is True
    assert result.available is True
    assert result.missing == []


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point cwd and HOME at the tmp tree and clear config overrides."""
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    monkeypatch.delenv(GUILDBOTICS_DATA_DIR, raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def _restore_cwd() -> object:
    """Restore the working directory even if a test calls ``os.chdir``."""
    original = Path.cwd()
    yield
    os.chdir(original)


def _write_project(config_dir: Path, body: str = "language: en\n") -> Path:
    project_file = config_dir / "team" / "project.yml"
    project_file.parent.mkdir(parents=True, exist_ok=True)
    project_file.write_text(body)
    return project_file


# --- get_config_status() ----------------------------------------------------


def test_config_status_reports_workspace_when_workspace_config_present(
    isolated_home: Path,
) -> None:
    _write_project(isolated_home / ".guildbotics" / "config")

    status = AppRuntime(EventBus()).get_config_status()

    assert status.cwd == isolated_home
    assert status.config_dir == isolated_home / ".guildbotics" / "config"
    assert status.project_file_exists is True


def test_config_status_uses_custom_config_dir_env(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_config = isolated_home / "custom-config"
    _write_project(custom_config)
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(custom_config))

    status = AppRuntime(EventBus()).get_config_status()

    assert status.config_dir == custom_config
    assert status.project_file_exists is True


def test_config_status_reports_missing_when_no_project_file(
    isolated_home: Path,
) -> None:
    status = AppRuntime(EventBus()).get_config_status()

    assert status.project_file_exists is False
    assert status.env_file == isolated_home / ".env"
    assert status.env_file_exists is False


def test_config_status_detects_existing_env_file(isolated_home: Path) -> None:
    (isolated_home / ".env").write_text("OPENAI_API_KEY=value\n")

    status = AppRuntime(EventBus()).get_config_status()

    assert status.env_file_exists is True


# --- set_workspace() --------------------------------------------------------


def test_set_workspace_raises_for_missing_path(isolated_home: Path) -> None:
    runtime = AppRuntime(EventBus())
    missing = isolated_home / "does-not-exist"

    with pytest.raises(AppApiError) as exc_info:
        runtime.set_workspace(missing)

    assert exc_info.value.code == "workspace_not_found"
    assert exc_info.value.status_code == HTTP_BAD_REQUEST
    assert exc_info.value.context == {"workspace_dir": str(missing.resolve())}


def test_set_workspace_raises_for_file_path(isolated_home: Path) -> None:
    runtime = AppRuntime(EventBus())
    file_path = isolated_home / "workspace-file"
    file_path.write_text("not a dir")

    with pytest.raises(AppApiError) as exc_info:
        runtime.set_workspace(file_path)

    assert exc_info.value.code == "workspace_not_directory"
    assert exc_info.value.status_code == HTTP_BAD_REQUEST


def test_set_workspace_stops_scheduler_changes_cwd_and_loads_env(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = AppRuntime(EventBus())
    stop_calls: list[bool] = []
    monkeypatch.setattr(
        runtime, "stop_scheduler", lambda: stop_calls.append(True) or None
    )
    monkeypatch.delenv("WORKSPACE_MARKER", raising=False)

    workspace = isolated_home / "workspace"
    workspace.mkdir()
    _write_project(workspace / ".guildbotics" / "config")
    (workspace / ".env").write_text("WORKSPACE_MARKER=loaded\n")

    status = runtime.set_workspace(workspace)

    assert stop_calls == [True]
    assert Path.cwd() == workspace.resolve()
    assert os.environ["WORKSPACE_MARKER"] == "loaded"
    assert os.environ[GUILDBOTICS_CONFIG_DIR] == str(
        workspace.resolve() / ".guildbotics" / "config"
    )
    assert os.environ[GUILDBOTICS_ENV_FILE] == str(workspace.resolve() / ".env")
    assert os.environ[GUILDBOTICS_DATA_DIR] == str(
        workspace.resolve() / ".guildbotics" / "data"
    )
    assert active_workspace_file().exists()
    assert status.cwd == workspace.resolve()
    assert status.machine_state_dir == isolated_home / "home/.guildbotics/data"
    assert status.workspace_data_dir == workspace.resolve() / ".guildbotics" / "data"
    assert status.storage_dir == status.workspace_data_dir
    assert status.config_dir == workspace.resolve() / ".guildbotics" / "config"
    assert status.env_file_exists is True


def test_set_workspace_env_does_not_override_home_state_root(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_home = os.environ["HOME"]
    original_userprofile = str(isolated_home / "userprofile")
    original_homedrive = "C:"
    original_homepath = "\\Users\\Original"
    monkeypatch.setenv("USERPROFILE", original_userprofile)
    monkeypatch.setenv("HOMEDRIVE", original_homedrive)
    monkeypatch.setenv("HOMEPATH", original_homepath)
    runtime = AppRuntime(EventBus())
    monkeypatch.setattr(runtime, "stop_scheduler", lambda: None)

    workspace = isolated_home / "workspace"
    workspace.mkdir()
    _write_project(workspace / ".guildbotics" / "config")
    (workspace / ".env").write_text(
        "\n".join(
            [
                "HOME=workspace-home",
                "USERPROFILE=workspace-userprofile",
                "HOMEDRIVE=Z:",
                "HOMEPATH=\\Users\\Workspace",
                "WORKSPACE_MARKER=loaded",
            ]
        ),
        encoding="utf-8",
    )

    status = runtime.set_workspace(workspace)

    assert os.environ["HOME"] == original_home
    assert os.environ["USERPROFILE"] == original_userprofile
    assert os.environ["HOMEDRIVE"] == original_homedrive
    assert os.environ["HOMEPATH"] == original_homepath
    assert os.environ["WORKSPACE_MARKER"] == "loaded"
    assert status.machine_state_dir == isolated_home / "home/.guildbotics/data"
    assert active_workspace_file() == (
        isolated_home / "home/.guildbotics/data/active-workspace.json"
    )


def test_set_workspace_prefers_env_data_dir_over_inherited(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inherited_data = isolated_home / "inherited-data"
    monkeypatch.setenv(GUILDBOTICS_DATA_DIR, str(inherited_data))
    runtime = AppRuntime(EventBus())
    monkeypatch.setattr(runtime, "stop_scheduler", lambda: None)

    workspace = isolated_home / "workspace"
    workspace.mkdir()
    _write_project(workspace / ".guildbotics" / "config")
    (workspace / ".env").write_text(
        "GUILDBOTICS_DATA_DIR=workspace-data\n",
        encoding="utf-8",
    )

    status = runtime.set_workspace(workspace)

    expected = (workspace / "workspace-data").resolve(strict=False)
    assert os.environ[GUILDBOTICS_DATA_DIR] == str(expected)
    assert status.workspace_data_dir == expected
    assert status.machine_state_dir == isolated_home / "home/.guildbotics/data"
    assert active_workspace_file() == (
        isolated_home / "home/.guildbotics/data/active-workspace.json"
    )


def test_set_workspace_uses_initial_inherited_data_dir_across_switches(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inherited_data = isolated_home / "inherited-data"
    monkeypatch.setenv(GUILDBOTICS_DATA_DIR, str(inherited_data))
    runtime = AppRuntime(EventBus())
    monkeypatch.setattr(runtime, "stop_scheduler", lambda: None)

    workspace_a = isolated_home / "workspace-a"
    workspace_a.mkdir()
    _write_project(workspace_a / ".guildbotics" / "config")
    (workspace_a / ".env").write_text(
        "GUILDBOTICS_DATA_DIR=workspace-a-data\n",
        encoding="utf-8",
    )

    workspace_b = isolated_home / "workspace-b"
    workspace_b.mkdir()
    _write_project(workspace_b / ".guildbotics" / "config")

    runtime.set_workspace(workspace_a)
    status_b = runtime.set_workspace(workspace_b)

    assert status_b.workspace_data_dir == inherited_data.resolve(strict=False)
    assert os.environ[GUILDBOTICS_DATA_DIR] == str(inherited_data.resolve(strict=False))


def test_get_context_does_not_reapply_workspace_data_root(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = AppRuntime(EventBus())
    monkeypatch.setattr(runtime, "stop_scheduler", lambda: None)
    workspace = isolated_home / "workspace"
    workspace.mkdir()
    _write_project(workspace / ".guildbotics" / "config")
    env_file = workspace / ".env"
    env_file.write_text(
        "GUILDBOTICS_DATA_DIR=data-a\nWORKSPACE_MARKER=a\n",
        encoding="utf-8",
    )
    runtime.set_workspace(workspace)
    data_root = os.environ[GUILDBOTICS_DATA_DIR]
    env_file.write_text(
        "GUILDBOTICS_DATA_DIR=data-b\nWORKSPACE_MARKER=b\n",
        encoding="utf-8",
    )

    class _FakeEdition:
        def get_context(self, message: str = "") -> _FakeContext:
            return _FakeContext([])

    monkeypatch.setattr(
        "guildbotics.app_api.runtime.get_edition", lambda: _FakeEdition()
    )

    runtime._get_context()

    assert os.environ[GUILDBOTICS_DATA_DIR] == data_root
    assert os.environ["WORKSPACE_MARKER"] == "b"


def test_diagnostics_store_switches_with_workspace_data_root(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from guildbotics.app_api.diagnostics_store import DiagnosticsStore

    store = DiagnosticsStore()
    runtime = AppRuntime(EventBus(store=store))
    monkeypatch.setattr(runtime, "stop_scheduler", lambda: None)

    workspace_a = isolated_home / "workspace-a"
    workspace_a.mkdir()
    _write_project(workspace_a / ".guildbotics" / "config")

    workspace_b = isolated_home / "workspace-b"
    workspace_b.mkdir()
    _write_project(workspace_b / ".guildbotics" / "config")

    runtime.set_workspace(workspace_a)
    store.record({"kind": "event", "type": "a", "trace_id": "trace-a"})
    runtime.set_workspace(workspace_b)

    assert store.list_traces() == []
    assert (
        store.path == workspace_b.resolve() / ".guildbotics/data/run/diagnostics.jsonl"
    )


def test_set_workspace_clears_stale_dotenv_keys_from_previous_workspace(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = AppRuntime(EventBus())
    monkeypatch.setattr(runtime, "stop_scheduler", lambda: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("WORKSPACE_MARKER", raising=False)

    workspace_a = isolated_home / "workspace-a"
    workspace_a.mkdir()
    _write_project(workspace_a / ".guildbotics" / "config")
    (workspace_a / ".env").write_text("OPENAI_API_KEY=secret-a\nWORKSPACE_MARKER=a\n")

    workspace_b = isolated_home / "workspace-b"
    workspace_b.mkdir()
    _write_project(workspace_b / ".guildbotics" / "config")
    (workspace_b / ".env").write_text("WORKSPACE_MARKER=b\n")

    runtime.set_workspace(workspace_a)
    assert os.environ["OPENAI_API_KEY"] == "secret-a"

    runtime.set_workspace(workspace_b)
    # Workspace B does not define OPENAI_API_KEY, so the credential injected by
    # workspace A must not leak into workspace B.
    assert "OPENAI_API_KEY" not in os.environ
    assert os.environ["WORKSPACE_MARKER"] == "b"


def test_set_workspace_clears_dotenv_keys_when_new_env_missing(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = AppRuntime(EventBus())
    monkeypatch.setattr(runtime, "stop_scheduler", lambda: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    workspace_a = isolated_home / "workspace-a"
    workspace_a.mkdir()
    _write_project(workspace_a / ".guildbotics" / "config")
    (workspace_a / ".env").write_text("OPENAI_API_KEY=secret-a\n")

    workspace_b = isolated_home / "workspace-b"
    workspace_b.mkdir()
    _write_project(workspace_b / ".guildbotics" / "config")

    runtime.set_workspace(workspace_a)
    assert os.environ["OPENAI_API_KEY"] == "secret-a"

    runtime.set_workspace(workspace_b)
    assert "OPENAI_API_KEY" not in os.environ


# --- get_team_summary() -----------------------------------------------------


class _ProjectStub:
    name = "GuildBotics"

    def get_language_code(self) -> str:
        return "ja"

    def get_language_name(self) -> str:
        return "日本語"


class _MemberStub:
    def __init__(
        self, person_id: str, name: str, is_active: bool, roles: dict[str, object]
    ) -> None:
        self.person_id = person_id
        self.name = name
        self.is_active = is_active
        self.roles = roles


def _context_with_members(members: list[_MemberStub]) -> object:
    team = type("TeamStub", (), {"project": _ProjectStub(), "members": members})()
    return type("ContextStub", (), {"team": team})()


def test_team_summary_reports_project_language_and_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = AppRuntime(EventBus())
    context = _context_with_members([])
    monkeypatch.setattr(runtime, "_get_context", lambda message="": context)

    summary = runtime.get_team_summary()

    assert summary.project.name == "GuildBotics"
    assert summary.project.language_code == "ja"
    assert summary.project.language_name == "日本語"
    assert summary.members == []


def test_team_summary_sorts_member_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = AppRuntime(EventBus())
    member = _MemberStub(
        "alice",
        "Alice",
        True,
        {"reviewer": {}, "architect": {}, "developer": {}},
    )
    context = _context_with_members([member])
    monkeypatch.setattr(runtime, "_get_context", lambda message="": context)

    summary = runtime.get_team_summary()

    assert [member.roles for member in summary.members] == [
        ["architect", "developer", "reviewer"]
    ]


def test_team_summary_includes_inactive_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = AppRuntime(EventBus())
    members = [
        _MemberStub("active", "Active", True, {"architect": {}}),
        _MemberStub("inactive", "Inactive", False, {}),
    ]
    context = _context_with_members(members)
    monkeypatch.setattr(runtime, "_get_context", lambda message="": context)

    summary = runtime.get_team_summary()

    by_id = {member.person_id: member for member in summary.members}
    assert by_id["active"].is_active is True
    assert by_id["inactive"].is_active is False
    assert by_id["inactive"].roles == []


# --- prompt trace -----------------------------------------------------------


def test_prompt_trace_status_reflects_limit_and_read_path(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE", "1")
    monkeypatch.delenv("GUILDBOTICS_PROMPT_TRACE_PATH", raising=False)
    output_trace = isolated_home / "output.jsonl"
    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE_PATH", str(output_trace))
    read_trace = isolated_home / "history.jsonl"
    read_trace.write_text(
        "\n".join(
            f'{{"event":"llm.request","timestamp":"t{index}",'
            f'"person_id":"p{index}","message":"m{index}"}}'
            for index in range(5)
        )
        + "\n"
    )
    runtime = AppRuntime(EventBus())

    status = runtime.get_prompt_trace_status(limit=2, read_path=str(read_trace))

    assert status.enabled is True
    assert status.trace_file == read_trace
    assert status.output_trace_file == output_trace
    assert status.trace_file_exists is True
    assert status.event_count == TRACE_EVENT_TOTAL
    assert len(status.events) == TRACE_EVENT_LIMIT
    # The reader returns the most recent ``limit`` events, newest first.
    assert [event.person_id for event in status.events] == ["p4", "p3"]


def test_prompt_trace_status_uses_output_path_when_read_path_blank(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_trace = isolated_home / "output.jsonl"
    output_trace.write_text(
        '{"event":"llm.request","timestamp":"t","person_id":"p","message":"m"}\n'
    )
    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE", "0")
    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE_PATH", str(output_trace))
    runtime = AppRuntime(EventBus())

    status = runtime.get_prompt_trace_status(read_path="   ")

    assert status.enabled is False
    assert status.trace_file == output_trace
    assert status.event_count == 1


def test_update_prompt_trace_writes_env_and_environ(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GUILDBOTICS_PROMPT_TRACE", raising=False)
    monkeypatch.delenv("GUILDBOTICS_PROMPT_TRACE_PATH", raising=False)
    trace_path = isolated_home / "trace.jsonl"
    runtime = AppRuntime(EventBus())

    status = runtime.update_prompt_trace(
        PromptTraceUpdateRequest(enabled=True, trace_path=f"  {trace_path}  ")
    )

    assert os.environ["GUILDBOTICS_PROMPT_TRACE"] == "1"
    assert os.environ["GUILDBOTICS_PROMPT_TRACE_PATH"] == str(trace_path)
    env_text = (isolated_home / ".env").read_text()
    assert "GUILDBOTICS_PROMPT_TRACE=1" in env_text
    assert f"GUILDBOTICS_PROMPT_TRACE_PATH={trace_path}" in env_text
    assert status.enabled is True
    assert status.trace_file == trace_path


def test_update_prompt_trace_empty_path_removes_env_key(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE_PATH", "/old/trace.jsonl")
    (isolated_home / ".env").write_text(
        "GUILDBOTICS_PROMPT_TRACE=1\nGUILDBOTICS_PROMPT_TRACE_PATH=/old/trace.jsonl\n"
    )
    runtime = AppRuntime(EventBus())

    runtime.update_prompt_trace(PromptTraceUpdateRequest(enabled=False, trace_path=""))

    assert "GUILDBOTICS_PROMPT_TRACE_PATH" not in os.environ
    env_text = (isolated_home / ".env").read_text()
    assert "GUILDBOTICS_PROMPT_TRACE=0" in env_text
    assert "GUILDBOTICS_PROMPT_TRACE_PATH" not in env_text


def test_update_prompt_trace_preserves_existing_keys_and_order(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GUILDBOTICS_PROMPT_TRACE", raising=False)
    (isolated_home / ".env").write_text(
        "\n".join(
            [
                "# leading comment",
                "OPENAI_API_KEY=first",
                "GUILDBOTICS_PROMPT_TRACE=0",
                "EXTRA=keep",
            ]
        )
        + "\n"
    )
    trace_path = isolated_home / "trace.jsonl"
    runtime = AppRuntime(EventBus())

    runtime.update_prompt_trace(
        PromptTraceUpdateRequest(enabled=True, trace_path=str(trace_path))
    )

    lines = (isolated_home / ".env").read_text().splitlines()
    keys = [line.split("=", 1)[0] for line in lines if "=" in line]
    # Existing keys retain their original relative order; the toggled key is
    # updated in place and the new path key is appended after them.
    assert keys == [
        "OPENAI_API_KEY",
        "GUILDBOTICS_PROMPT_TRACE",
        "EXTRA",
        "GUILDBOTICS_PROMPT_TRACE_PATH",
    ]
    env_map = dict(line.split("=", 1) for line in lines if "=" in line)
    assert env_map["OPENAI_API_KEY"] == "first"
    assert env_map["GUILDBOTICS_PROMPT_TRACE"] == "1"
    assert env_map["EXTRA"] == "keep"
    assert env_map["GUILDBOTICS_PROMPT_TRACE_PATH"] == str(trace_path)


# --- runtime debug ----------------------------------------------------------


def test_runtime_debug_status_reads_env_file(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("AGNO_DEBUG", raising=False)
    (isolated_home / ".env").write_text("LOG_LEVEL=DEBUG\nAGNO_DEBUG=false\n")
    runtime = AppRuntime(EventBus())

    status = runtime.get_runtime_debug_status()

    assert status.enabled is True
    assert status.log_level == "DEBUG"
    assert status.agno_debug is False
    assert status.env_file == isolated_home / ".env"
    assert status.env_file_exists is True


def test_update_runtime_debug_writes_env_environ_and_logger_level(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("AGNO_DEBUG", raising=False)
    (isolated_home / ".env").write_text("OPENAI_API_KEY=keep\nLOG_LEVEL=INFO\n")
    runtime = AppRuntime(EventBus())
    logger = logging.getLogger("guildbotics")
    original_level = logger.level
    handler = logging.StreamHandler()
    logger.addHandler(handler)

    try:
        status = runtime.update_runtime_debug(RuntimeDebugUpdateRequest(enabled=True))

        assert os.environ["LOG_LEVEL"] == "DEBUG"
        assert os.environ["AGNO_DEBUG"] == "true"
        assert logger.level == logging.DEBUG
        assert handler.level == logging.DEBUG
        env_map = dict(
            line.split("=", 1)
            for line in (isolated_home / ".env").read_text().splitlines()
            if "=" in line
        )
        assert env_map == {
            "OPENAI_API_KEY": "keep",
            "LOG_LEVEL": "DEBUG",
            "AGNO_DEBUG": "true",
        }
        assert status.enabled is True
        assert status.log_level == "DEBUG"
        assert status.agno_debug is True
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)


def test_update_runtime_debug_disables_both_debug_flags(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("AGNO_DEBUG", "true")
    (isolated_home / ".env").write_text("LOG_LEVEL=DEBUG\nAGNO_DEBUG=true\n")
    runtime = AppRuntime(EventBus())

    status = runtime.update_runtime_debug(RuntimeDebugUpdateRequest(enabled=False))

    assert os.environ["LOG_LEVEL"] == "INFO"
    assert os.environ["AGNO_DEBUG"] == "false"
    env_text = (isolated_home / ".env").read_text()
    assert "LOG_LEVEL=INFO" in env_text
    assert "AGNO_DEBUG=false" in env_text
    assert status.enabled is False
    assert status.log_level == "INFO"
    assert status.agno_debug is False


# --- detect_cli_agents() ----------------------------------------------------


def _cli_infos(*items: tuple[str, str, int, str]) -> list[CliAgentInfo]:
    return [
        CliAgentInfo(name=name, label=label, order=order, executable=executable)
        for name, label, order, executable in items
    ]


def test_detect_cli_agents_resolves_executable_and_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "guildbotics.app_api.runtime.discover_cli_agents",
        lambda _config_dir: _cli_infos(
            ("codex", "OpenAI Codex CLI", 10, "codex"),
            ("antigravity", "Antigravity CLI", 20, "agy"),
        ),
    )

    def _resolve_path(executable: str) -> str:
        return f"/usr/local/bin/{executable}" if executable == "codex" else ""

    monkeypatch.setattr(
        "guildbotics.app_api.runtime.resolve_cli_agent_path", _resolve_path
    )
    runtime = AppRuntime(EventBus())

    agents = {agent.name: agent for agent in runtime.detect_cli_agents().agents}

    assert agents["codex"].label == "OpenAI Codex CLI"
    assert agents["codex"].executable == "codex"
    assert agents["codex"].detected is True
    assert agents["codex"].path == "/usr/local/bin/codex"
    # The agent name and binary differ for antigravity (agy); detection uses the
    # declared executable.
    assert agents["antigravity"].executable == "agy"
    assert agents["antigravity"].detected is False
    assert agents["antigravity"].path == ""


def test_detect_cli_agents_marks_undetected_when_executable_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "guildbotics.app_api.runtime.discover_cli_agents",
        lambda _config_dir: _cli_infos(("codex", "OpenAI Codex CLI", 10, "codex")),
    )
    monkeypatch.setattr(
        "guildbotics.app_api.runtime.resolve_cli_agent_path", lambda _executable: ""
    )
    runtime = AppRuntime(EventBus())

    response = runtime.detect_cli_agents()

    assert all(agent.detected is False for agent in response.agents)
    assert all(agent.path == "" for agent in response.agents)


def test_detect_cli_agents_returns_empty_for_empty_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "guildbotics.app_api.runtime.discover_cli_agents", lambda _config_dir: []
    )
    runtime = AppRuntime(EventBus())

    assert runtime.detect_cli_agents().agents == []
