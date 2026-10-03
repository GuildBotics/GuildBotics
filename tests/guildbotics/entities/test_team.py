from unittest.mock import MagicMock, patch

import pytest

from guildbotics.entities.team import (
    CommandSchedule,
    Person,
    Project,
    Service,
)

EXPECTED_SCHEDULE_COUNT = 2

# -----------------------------
# Project tests
# -----------------------------


def test_project_service_names_and_availability():
    project = Project(
        name="p",
        services={
            Service.FILE_STORAGE.value: {"name": "S3"},
            Service.TICKET_MANAGER.value: {"name": "Jira"},
            # Intentionally leave CODE_HOSTING_SERVICE missing
        },
    )

    assert project.is_available_service(Service.FILE_STORAGE)
    assert project.is_available_service(Service.TICKET_MANAGER)
    assert not project.is_available_service(Service.CODE_HOSTING_SERVICE)
    # Name is lower-cased by implementation
    assert project.get_service_name(Service.FILE_STORAGE) == "s3"


@pytest.mark.parametrize(
    "tag,expected_code",
    [
        ("en", "en"),
        ("ja", "ja"),
        ("bogus-lang-tag", "en"),  # falls back
        ("", "en"),  # empty -> default
    ],
)
def test_project_get_language_code(tag: str, expected_code: str):
    project = Project(name="p", language=tag)
    assert project.get_language_code() == expected_code


@pytest.mark.parametrize(
    ("tag", "expected_name"),
    [
        ("en", "English"),
        ("ja", "日本語"),
    ],
)
def test_project_get_language_name_for_known_setup_languages(tag, expected_name):
    project = Project(name="p", language=tag)

    assert project.get_language_name() == expected_name


def test_project_get_language_name_falls_back_to_langcodes_for_other_languages():
    with patch("guildbotics.entities.team.Language") as mock_language:
        mock_lang = MagicMock()
        mock_lang.language = "fr"
        mock_lang.display_name.return_value = "French"
        mock_language.get.return_value = mock_lang

        project = Project(name="p", language="fr")
        name = project.get_language_name()
        assert isinstance(name, str) and name.strip() != ""
        assert name == "French"
        mock_lang.display_name.assert_called_once_with("fr")


def test_project_accepts_description():
    project = Project(name="p", description="Project context for agents.")

    assert project.description == "Project context for agents."


# -----------------------------
# Person tests
# -----------------------------


def test_person_get_scheduled_tasks_expands_all_schedules():
    schedules = ["0 9 ? ? ?", "15 10 ? ? ?"]
    person = Person(
        person_id="u1",
        name="Alice",
        task_schedules=[CommandSchedule(command="demo", schedules=schedules)],
    )

    scheduled = person.get_scheduled_commands()

    assert len(scheduled) == EXPECTED_SCHEDULE_COUNT
    assert all(s.command == "demo" for s in scheduled)
    assert sorted(s.schedule for s in scheduled) == sorted(schedules)


def test_person_secret_helpers(monkeypatch):
    person = Person(person_id="u2", name="Bob")
    key = "token"
    env_key = f"{person.person_id.upper()}_{key.upper()}"

    # Not set
    assert person.has_secret(key) is False
    with pytest.raises(KeyError):
        _ = person.get_secret(key)

    # Set and read
    monkeypatch.setenv(env_key, "secret-value")
    assert person.has_secret(key) is True
    assert person.get_secret(key) == "secret-value"
