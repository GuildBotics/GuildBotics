from unittest.mock import MagicMock, patch

import pytest

from guildbotics.entities.team import (
    CommandSchedule,
    Person,
    Project,
    Service,
)

# -----------------------------
# Project tests
# -----------------------------


def test_project_service_names():
    project = Project(
        name="p",
        services={
            Service.TICKET_SERVICE.value: {"name": "Jira"},
            # Intentionally leave CODE_HOSTING_SERVICE missing
        },
    )

    # Name is lower-cased by implementation
    assert project.get_service_name(Service.TICKET_SERVICE) == "jira"


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


def test_person_retains_schedule_configuration_for_the_host():
    person = Person(
        person_id="u1",
        name="Alice",
        task_schedules=[
            CommandSchedule(command="demo", schedules=["0 9 * * *", "15 10 * * *"])
        ],
    )
    assert person.model_dump()["task_schedules"] == [
        {"command": "demo", "schedules": ["0 9 * * *", "15 10 * * *"]}
    ]
