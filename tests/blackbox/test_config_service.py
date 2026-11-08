"""Config concurrency and service lifecycle, as seen by CLI and settings screens."""

import pytest

from tests.blackbox.harness import PERSON


def test_setup_lists_saved_members_before_project_creation(server_stack):
    project = server_stack.workspace / ".guildbotics/config/team/project.yml"
    project.unlink()
    response = server_stack.api.get("team")
    assert response.status_code == 200
    assert [member["person_id"] for member in response.json()["members"]] == [PERSON]
    assert not project.exists()


def test_config_status_reports_and_recovers_corrupt_registry(server_stack):
    registry = server_stack.workspace.parent / ".guildbotics/data/workspaces.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text("{", encoding="utf-8")
    status = server_stack.api.get("config/status")
    assert status.status_code == 200
    assert "workspace registry" in status.json()["workspace_problem"]
    assert registry.read_text() == "{"
    registry.write_text("[]", encoding="utf-8")
    assert server_stack.api.get("config/status").json()["workspace_problem"] == ""


@pytest.mark.parametrize("kind", ["project", "member"])
def test_settings_round_trip_and_stale_write(server_stack, kind):
    stack = server_stack
    path = "config/project" if kind == "project" else f"config/members/{PERSON}"
    response = stack.api.get(path)
    assert response.status_code == 200
    before = response.json()
    if kind == "project":
        payload = {
            key: before[key]
            for key in (
                "language",
                "description",
                "llm_api_type",
                "cli_agent",
                "services",
            )
        }
        field, value = "description", "黒箱で更新したプロジェクト。"
    else:
        payload = {
            key: before[key]
            for key in (
                "person_id",
                "person_name",
                "person_type",
                "is_active",
                "git_email",
                "roles",
            )
        }
        field, value = "speaking_style", "短く具体的に説明する。"
    payload.update(
        config_dir=stack.config_dir,
        expected_revisions=before["revisions"],
        **{field: value},
    )
    saved = stack.api.put(path, json=payload)
    assert saved.status_code == 200, saved.text
    revisions = saved.json()["revisions"]
    assert revisions and revisions != before["revisions"]
    reread = stack.api.get(path).json()
    assert reread[field] == value
    assert reread["revisions"] == revisions

    payload[field] = "古い画面からの上書き。"
    stale = stack.api.put(path, json=payload)
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "config_changed"
    unchanged = stack.api.get(path).json()
    assert unchanged[field] == value
    assert unchanged["revisions"] == revisions


def test_service_start_and_stop(stack):
    def status():
        response = stack.api.get("scheduler/status")
        assert response.status_code == 200
        return response.json()

    assert status()["scheduler"]["running"] is False
    stack.cli("service", "start")
    assert status()["scheduler"]["running"] is True
    stack.cli("service", "stop")
    stopped = status()
    assert stopped["scheduler"]["running"] is False
    assert stopped["events"]["running"] is False
    assert stopped["has_active_work"] is False
    assert stack.api.get("health").status_code == 200


def test_command_requirements_use_project_language_and_keep_runtime_refusal(
    server_stack,
):
    import yaml

    project = server_stack.workspace / ".guildbotics/config/team/project.yml"
    data = yaml.safe_load(project.read_text(encoding="utf-8"))
    data["language"] = "ja"
    project.write_text(yaml.safe_dump(data), encoding="utf-8")
    command = server_stack.workspace / ".guildbotics/config/commands/refusal.md"
    command.parent.mkdir(parents=True, exist_ok=True)
    command.write_text(
        "---\nname: Refusal\nbrain: none\n---\nNo external work.\n", encoding="utf-8"
    )
    response = server_stack.api.get("commands/options")
    assert response.status_code == 200
    requirements = [
        item
        for command in response.json()["options"]
        for item in command["requirements"]
        if item["kind"] == "environment"
    ]
    assert requirements
    assert all(item["satisfied"] is False for item in requirements)
    assert all("microsandbox ランタイム" in item["message"] for item in requirements)
    assert all(
        str(server_stack.workspace.parent / ".guildbotics/data/msb") in item["message"]
        for item in requirements
    )
