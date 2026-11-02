"""Config concurrency and service lifecycle, as seen by CLI and settings screens."""

import pytest

from tests.blackbox.harness import PERSON


@pytest.mark.parametrize("kind", ["project", "member"])
def test_settings_round_trip_and_stale_write(stack, kind):
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
