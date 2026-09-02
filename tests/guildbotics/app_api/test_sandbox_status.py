from __future__ import annotations

from pathlib import Path

import pytest

from guildbotics.app_api import sandbox_status as module
from guildbotics.app_api.sandbox_status import (
    evaluate_grant,
    network_support,
    sandbox_problems,
    sandbox_status,
)
from guildbotics.intelligences.brains.cli_agent import ExecutableInfo
from guildbotics.intelligences.sandbox import (
    DocumentGrant,
    LocalGrants,
    LocalPathGrant,
    SharedGrants,
    parse_network_policy,
)


def _network(command: str = "deny", web: str = "deny", **extra):
    def route(mode: str) -> dict:
        return {
            "mode": mode,
            "allowed_domains": ["example.com"] if mode == "allowlist" else [],
        }

    return parse_network_policy(
        {"command": {**route(command), **extra}, "web": route(web)}, where="test"
    )


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(module, "get_cli_agent_search_path", lambda: "")
    monkeypatch.setattr(module, "load_shared_grants", lambda: SharedGrants())
    monkeypatch.setattr(module, "load_local_grants", lambda: LocalGrants())
    return home


def test_network_support_reports_this_device_and_any_os() -> None:
    support = network_support("darwin")

    assert support["codex"].command_modes == ["allowlist", "deny", "unrestricted"]
    assert support["codex"].contract_applied is True
    assert support["grok"].command_modes == ["unrestricted"]
    assert support["grok"].command_modes_anywhere == ["deny", "unrestricted"]
    assert support["antigravity"].grant_accesses == ["read_write"]
    assert support["claude"].contract_applied is False


def test_status_resolves_the_grants_once_and_names_what_each_slot_cannot_get(
    monkeypatch, home: Path
) -> None:
    uv = _executable(home / ".local/bin/uv")
    (home / "tools").mkdir()
    (home / ".ssh").mkdir()
    monkeypatch.setattr(module, "get_cli_agent_search_path", lambda: str(uv.parent))
    monkeypatch.setattr(
        module,
        "load_shared_grants",
        lambda: SharedGrants(
            documents=[
                DocumentGrant(path="tools", access="read"),
                DocumentGrant(path="Projects/out", access="read_write"),
            ],
        ),
    )
    monkeypatch.setattr(
        module, "load_local_grants", lambda: LocalGrants(deny=[".local/share/x"])
    )
    mappings = {
        "aiko": {
            "default": ExecutableInfo(
                adapter="codex", network=_network("deny", allow_local_network=True)
            )
        },
        "kenji": {"default": ExecutableInfo(adapter="grok", network=_network())},
    }
    monkeypatch.setattr(
        module, "get_cli_agent_mapping", lambda person: mappings[person]
    )

    status = sandbox_status(["aiko", "kenji"], platform="darwin")

    # A preview creates nothing: the missing document directory is shown absent.
    assert [(g.path, g.present) for g in status.access.documents] == [
        ("$HOME/tools", True),
        ("$HOME/Projects/out", False),
    ]
    assert not (home / "Projects/out").exists()
    assert [(t.path, t.sources) for t in status.access.trees] == [
        ("$HOME/.local", ["$HOME/.local/bin"])
    ]
    assert [(d.path, d.builtin) for d in status.access.denied] == [
        ("$HOME/.ssh", True),
        ("$HOME/.local/share/x", False),
    ]
    aiko = status.members[0].slots[0]
    assert aiko.tool == "codex"
    assert aiko.contract_applied is True
    assert [(p.setting, p.reason) for p in aiko.problems] == [
        (
            "network",
            "Codex cannot open the local network separately under command network mode 'deny'.",
        )
    ]
    kenji = status.members[1].slots[0]
    # Grok has not adopted the contract, so nothing is claimed or refused.
    assert kenji.contract_applied is False
    assert kenji.problems == []

    assert sandbox_problems(["aiko", "kenji"]) == [
        ("aiko", "default", "network", aiko.problems[0].reason)
    ]


def test_a_grant_a_tool_cannot_hold_is_a_problem_for_that_slot(
    monkeypatch, home: Path
) -> None:
    (home / "docs").mkdir()
    monkeypatch.setattr(
        module,
        "load_shared_grants",
        lambda: SharedGrants(documents=[DocumentGrant(path="docs", access="read")]),
    )
    # Pretend Antigravity adopted the contract, so its grant claims are checked.
    monkeypatch.setattr(
        module,
        "CLI_AGENTS",
        tuple(
            agent.model_copy(
                update={
                    "network": agent.network.model_copy(
                        update={"contract_applied": True}
                    )
                }
            )
            if agent.name == "antigravity"
            else agent
            for agent in module.CLI_AGENTS
        ),
    )
    monkeypatch.setattr(
        module,
        "get_cli_agent_mapping",
        lambda _person: {
            "default": ExecutableInfo(
                adapter="antigravity", network=_network("unrestricted")
            )
        },
    )

    slot = sandbox_status(["x"], platform="darwin").members[0].slots[0]

    assert [(p.setting, p.reason) for p in slot.problems] == [
        ("grants", "Antigravity cannot grant 'read' access to a directory.")
    ]


def test_an_unresolvable_local_path_is_reported_on_every_applied_slot(
    monkeypatch, home: Path
) -> None:
    monkeypatch.setattr(
        module,
        "load_local_grants",
        lambda: LocalGrants(paths=[LocalPathGrant(path="/opt/nowhere", access="read")]),
    )
    monkeypatch.setattr(
        module,
        "get_cli_agent_mapping",
        lambda _person: {
            "default": ExecutableInfo(adapter="codex", network=_network())
        },
    )

    status = sandbox_status(["aiko"], platform="darwin")

    # The row is shown absent, and every applied slot names it as the reason.
    assert status.access.problem == ""
    assert [(g.path, g.present) for g in status.access.paths] == [
        ("/opt/nowhere", False)
    ]
    assert [(p.setting, p.reason) for p in status.members[0].slots[0].problems] == [
        ("grants", "local path '/opt/nowhere' does not exist on this device")
    ]


def test_an_excluded_tree_is_reported_with_its_source_and_reason(
    monkeypatch, home: Path
) -> None:
    secret = _executable(home / ".ssh/bin/leak")
    monkeypatch.setattr(module, "get_cli_agent_search_path", lambda: str(secret.parent))

    status = sandbox_status(["aiko"], platform="darwin")

    assert status.access.trees == []
    # `bin` and its package collapse to `~/.ssh` itself, which stays closed.
    assert [(t.path, t.source, t.reason) for t in status.access.excluded] == [
        ("$HOME/.ssh", "$HOME/.ssh/bin", "it is under ~/.ssh")
    ]


@pytest.mark.parametrize(
    ("scope", "path", "access", "valid", "present", "reason_part", "sensitive"),
    [
        ("document", "Documents", "read", True, True, "", ""),
        ("document", "Projects/new", "read_write", True, False, "", ""),
        ("document", "/opt/x", "read", False, False, "relative to the home", ""),
        ("document", "..", "read", False, False, "must name a directory", ""),
        ("document", ".ssh", "read", True, True, "", "~/.ssh"),
        ("local", "/opt/nowhere", "read", False, False, "does not exist", ""),
        ("local", ".cache/uv", "read_write", True, True, "", ""),
        ("local", ".codex", "read", True, True, "", "~/.codex"),
        ("deny", "/opt/homebrew/etc", "", True, True, "", ""),
        ("deny", ".local/share/some-app", "", True, True, "", ""),
        ("deny", "..", "", False, False, "must name a directory", ""),
    ],
)
def test_a_typed_grant_is_judged_before_it_is_saved(
    home: Path,
    scope,
    path: str,
    access: str,
    valid: bool,
    present: bool,
    reason_part: str,
    sensitive: str,
) -> None:
    for name in ("Documents", ".ssh", ".cache/uv", ".codex"):
        (home / name).mkdir(parents=True)

    evaluation = evaluate_grant(scope, path, access or "read")

    assert evaluation.valid is valid
    assert evaluation.present is present
    assert reason_part in evaluation.reason
    assert evaluation.sensitive == sensitive
    assert not (home / "Projects/new").exists()


def test_a_deny_that_would_close_the_home_is_refused(home: Path) -> None:
    refused = evaluate_grant("deny", str(home))

    assert refused.valid is False
    assert "whole home directory" in refused.reason


def test_the_sandbox_endpoints_answer_from_this_device(
    monkeypatch, tmp_path: Path
) -> None:
    from fastapi.testclient import TestClient

    from guildbotics.app_api import api as api_module
    from guildbotics.app_api.api import create_app
    from guildbotics.app_api.events import EventBus
    from guildbotics.app_api.models import GrantEvaluation, SandboxStatusResponse
    from guildbotics.app_api.runtime import AppRuntime

    runtime = AppRuntime(EventBus())
    monkeypatch.setattr(
        runtime,
        "get_sandbox_status",
        lambda: SandboxStatusResponse(
            platform="darwin", working_directory="<workspace>"
        ),
    )
    monkeypatch.setattr(
        api_module,
        "evaluate_grant",
        lambda scope, path, access="read": GrantEvaluation(
            scope=scope, path=path, access=access, valid=True
        ),
    )
    headers = {"X-GuildBotics-Session-Token": "secret"}

    with TestClient(create_app(session_token="secret", runtime=runtime)) as client:
        status = client.get("/intelligences/sandbox", headers=headers)
        evaluation = client.get(
            "/intelligences/grant-evaluation",
            params={"scope": "deny", "path": "/opt/x"},
            headers=headers,
        )
        bad_scope = client.get(
            "/intelligences/grant-evaluation",
            params={"scope": "other", "path": "x"},
            headers=headers,
        )
        unauthorized = client.get("/intelligences/sandbox")

    assert status.json()["platform"] == "darwin"
    assert evaluation.json()["scope"] == "deny"
    assert bad_scope.status_code == 422
    assert unauthorized.status_code == 401
