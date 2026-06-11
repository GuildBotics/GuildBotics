import pytest

from guildbotics.entities import Person, Project, Team
from guildbotics.integrations.github.github_code_hosting_service import (
    GitHubCodeHostingService,
)


def _service(api_base_url: str | None = None) -> GitHubCodeHostingService:
    config: dict[str, str] = {"name": "GitHub", "owner": "acme"}
    if api_base_url:
        config["api_base_url"] = api_base_url
    project = Project(name="demo", services={"code_hosting_service": config})
    person = Person(
        person_id="alice", name="Alice", account_info={"github_username": "alice"}
    )
    team = Team(project=project, members=[person])
    return GitHubCodeHostingService(person, team, repository="repo")


def test_web_base_url_defaults_to_github_com() -> None:
    assert _service()._web_base_url() == "https://github.com"


def test_web_base_url_derives_enterprise_host_from_api_base_url() -> None:
    # GitHub Enterprise API host -> web/clone host stay on the same instance.
    assert (
        _service("https://ghe.example.com/api/v3")._web_base_url()
        == "https://ghe.example.com"
    )


@pytest.mark.asyncio
async def test_get_repository_url_uses_github_com_by_default() -> None:
    assert await _service().get_repository_url() == "https://github.com/acme/repo.git"


@pytest.mark.asyncio
async def test_get_repository_url_follows_enterprise_api_host() -> None:
    service = _service("https://ghe.example.com/api/v3")
    assert await service.get_repository_url() == "https://ghe.example.com/acme/repo.git"


def test_constructor_requires_a_repository() -> None:
    project = Project(
        name="demo",
        services={"code_hosting_service": {"name": "GitHub", "owner": "acme"}},
    )
    person = Person(
        person_id="alice", name="Alice", account_info={"github_username": "alice"}
    )
    team = Team(project=project, members=[person])
    with pytest.raises(ValueError):
        GitHubCodeHostingService(person, team, repository=None)
