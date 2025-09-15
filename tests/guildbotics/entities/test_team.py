from unittest.mock import MagicMock, patch

import pytest

from guildbotics.entities.task import Task
from guildbotics.entities.team import (
    Person,
    Project,
    Repository,
    Role,
    Service,
    TaskSchedule,
    Team,
)

# -----------------------------
# Project tests
# -----------------------------


def test_project_get_default_repository_prefers_flagged_default():
    repos = [
        Repository(name="alpha", description="A", is_default=False),
        Repository(name="beta", description="B", is_default=True),
        Repository(name="gamma", description="C", is_default=False),
    ]
    project = Project(repositories=repos)

    repo = project.get_default_repository()

    assert repo.name == "beta"


def test_project_get_default_repository_falls_back_to_first_when_no_default():
    repos = [
        Repository(name="alpha"),
        Repository(name="beta"),
    ]
    project = Project(name="p", repositories=repos)

    repo = project.get_default_repository()

    assert repo.name == "alpha"


def test_project_get_default_repository_raises_when_empty():
    project = Project(name="p", repositories=[])
    with pytest.raises(ValueError):
        _ = project.get_default_repository()


def test_project_get_available_services_and_names():
    project = Project(
        name="p",
        services={
            Service.FILE_STORAGE.value: {"name": "S3"},
            Service.TICKET_MANAGER.value: {"name": "Jira"},
            # Intentionally leave CODE_HOSTING_SERVICE missing
        },
    )

    available = project.get_available_services()

    assert Service.FILE_STORAGE in available
    assert Service.TICKET_MANAGER in available
    assert Service.CODE_HOSTING_SERVICE not in available
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


def test_project_get_language_name_non_empty():
    with patch("guildbotics.entities.team.Language") as mock_language:
        mock_lang = MagicMock()
        mock_lang.language = "en"
        mock_lang.display_name.return_value = "English"
        mock_language.get.return_value = mock_lang

        project = Project(name="p", language="en")
        name = project.get_language_name()
        assert isinstance(name, str) and name.strip() != ""
        assert name == "English"
        mock_lang.display_name.assert_called_once_with("en")


# -----------------------------
# Person tests
# -----------------------------


def make_task(title: str = "t") -> Task:
    return Task(title=title, description="d")


def test_person_get_scheduled_tasks_expands_all_schedules():
    task = make_task("demo")
    schedules = ["0 9 ? ? ?", "15 10 ? ? ?"]
    person = Person(
        person_id="u1",
        name="Alice",
        task_schedules=[TaskSchedule(task=task, schedules=schedules)],
    )

    scheduled = person.get_scheduled_tasks()

    assert len(scheduled) == 2
    assert all(s.task.title == "demo" for s in scheduled)
    assert sorted(s.schedule for s in scheduled) == sorted(schedules)


def test_person_get_role_resolution(monkeypatch):
    # Prepare predefined roles
    predefined = {"mentor": Role(id="mentor", summary="m", description="d")}
    monkeypatch.setattr(Person, "DEFINED_ROLES", predefined, raising=False)

    person = Person(
        person_id="u1",
        name="Alice",
        roles={"dev": Role(id="dev", summary="s", description="d")},
    )

    # Existing role from person
    assert person.get_role("dev").id == "dev"
    # Missing role resolved from predefined
    assert person.get_role("mentor").id == "mentor"
    # None -> defaults to professional
    assert person.get_role(None).id == "professional"


def test_person_get_role_descriptions_filters_when_ids_provided():
    person = Person(
        person_id="u1",
        name="Alice",
        roles={
            "dev": Role(id="dev", summary="s", description="Developer"),
            "qa": Role(id="qa", summary="s", description="QA"),
        },
    )

    # All when None
    all_desc = person.get_role_descriptions()
    assert all_desc == {"dev": "Developer", "qa": "QA"}

    # Filtered when list provided
    filtered = person.get_role_descriptions(["qa"])  # only QA
    assert filtered == {"qa": "QA"}


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


# -----------------------------
# Team tests
# -----------------------------


def test_team_get_role_members_and_available_ids():
    project = Project(name="p", description={})
    alice = Person(
        person_id="u1",
        name="Alice",
        roles={
            "dev": Role(id="dev", summary="", description=""),
            "reviewer": Role(id="reviewer", summary="", description=""),
        },
    )
    bob = Person(
        person_id="u2",
        name="Bob",
        roles={"dev": Role(id="dev", summary="", description="")},
    )
    team = Team(project=project, members=[alice, bob])

    role_members = team.get_role_members()
    assert set(role_members.keys()) == {"dev", "reviewer"}
    assert set(p.person_id for p in role_members["dev"]) == {"u1", "u2"}
    assert [p.person_id for p in role_members["reviewer"]] == ["u1"]

    available_ids = team.get_available_role_ids()
    assert set(available_ids) == {"dev", "reviewer"}
