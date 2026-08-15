"""The hub commands, which a joining device also runs over SSH.

The JSON form is a wire format: a device on another machine parses it to learn
which workspaces a hub holds, so its shape is asserted rather than sampled.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from guildbotics.cli.hub import hub
from guildbotics.hub import host

WORKSPACE_ID = "0198ab00-0000-7000-8000-000000000001"


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CliRunner:
    root = tmp_path / "machine" / ".guildbotics"
    monkeypatch.setattr(host, "get_machine_root", lambda: root)
    return CliRunner()


def _json(runner: CliRunner, *arguments: str) -> dict:
    result = runner.invoke(hub, [*arguments, "--format", "json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_a_machine_reports_that_it_hosts_no_hub(runner: CliRunner) -> None:
    assert _json(runner, "status")["hosted"] is False


def test_creating_a_hub_reports_the_address_to_share(runner: CliRunner) -> None:
    payload = _json(runner, "create")

    assert payload["hosted"] is True
    assert payload["ssh_endpoint"]
    assert payload["workspaces"] == []


def test_a_workspace_repository_is_created_for_a_joining_device(
    runner: CliRunner,
) -> None:
    _json(runner, "create")

    payload = _json(runner, "workspace", "create", WORKSPACE_ID)

    assert payload["workspace_id"] == WORKSPACE_ID
    assert Path(payload["repository"]).is_dir()


def test_the_workspace_list_is_what_a_joining_device_reads(runner: CliRunner) -> None:
    _json(runner, "create")
    _json(runner, "workspace", "create", WORKSPACE_ID)

    assert _json(runner, "workspace", "list") == {"workspaces": [WORKSPACE_ID]}


def test_a_machine_that_is_not_a_hub_refuses_to_hold_a_workspace(
    runner: CliRunner,
) -> None:
    result = runner.invoke(hub, ["workspace", "create", WORKSPACE_ID])

    assert result.exit_code != 0
    assert "not a hub" in result.output


def test_an_identifier_that_is_not_a_workspace_is_refused(runner: CliRunner) -> None:
    """The argument arrives from another machine's command line."""
    _json(runner, "create")

    result = runner.invoke(hub, ["workspace", "create", "../../escape"])

    assert result.exit_code != 0


def test_the_readable_form_names_every_field(runner: CliRunner) -> None:
    result = runner.invoke(hub, ["create"])

    assert result.exit_code == 0, result.output
    assert "SSH endpoint:" in result.output
    assert "Workspaces: (none)" in result.output
