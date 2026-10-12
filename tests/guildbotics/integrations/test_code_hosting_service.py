"""Provider translation and substitution at every repository-read entry point."""

import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest

from guildbotics.capabilities import member_repository
from guildbotics.entities.team import Person, Project, Service, Team
from guildbotics.guest.window import WindowCodeHostingService
from guildbotics.integrations.factory import ServiceIntegrationFactory
from guildbotics.integrations.github import async_client, github_utils
from guildbotics.runtime.code_hosting_service import CodeHostingService
from guildbotics.runtime.port import ServiceError
from guildbotics.runtime.resources import (
    RESOURCES_BY_SUBJECT,
    DependencyAlert,
    ReadPage,
)
from tests.guildbotics.member_services import MemberServices


def team(name="github"):
    return Team(
        members=[],
        project=Project(
            name="demo",
            services={
                "code_hosting_service": {
                    "name": name,
                    "api_base_url": "https://hosting.test/api/v3/",
                },
                "ticket_service": {"name": "github"},
            },
        ),
    )


@pytest.mark.parametrize("name", ["", "gitlab", "unsupported"])
def test_unsupported_hosting_never_uses_ticket_provider(name):
    with pytest.raises(ServiceError):
        ServiceIntegrationFactory().create_code_hosting_service(
            logging.getLogger(), Person(person_id="aiko", name="Aiko"), team(name)
        )


@pytest.fixture
def adapter(monkeypatch):
    requests, clients, identities = [], [], []
    body = [
        {
            "number": 42,
            "state": "fixed",
            "html_url": "https://hosting.test/alert/42",
            "dependency": {
                "package": {"name": "demo", "ecosystem": "pip"},
                "manifest_path": "uv.lock",
            },
            "security_advisory": {
                "identifiers": [
                    {"type": "GHSA", "value": "GHSA-example"},
                    {"type": "CVE", "value": "CVE-example"},
                ],
                "summary": "summary",
                "description": "description",
            },
            "security_vulnerability": {
                "severity": "high",
                "vulnerable_version_range": "< 2",
                "first_patched_version": {"identifier": "2"},
            },
            "created_at": "2026-01-01",
            "updated_at": "2026-09-30",
        }
    ]

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200, json=body[0] if request.url.path.endswith("/42") else body
        )

    async def client(person, base_url, owner):
        identities.append((person.person_id, base_url, owner))
        result = async_client.get_async_client(
            base_url, github_utils.GitHubTokenAuth("token"), owner
        )
        result._transport = httpx.MockTransport(respond)
        result._mounts = {}
        clients.append(result)
        return result

    monkeypatch.setattr(
        "guildbotics.integrations.github.api.create_github_client", client
    )
    service = ServiceIntegrationFactory().create_code_hosting_service(
        logging.getLogger(), Person(person_id="aiko", name="Aiko"), team()
    )
    return service, body, requests, clients, identities


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "native"),
    [
        ("open", "open"),
        ("resolved", "fixed"),
        ("dismissed", "dismissed,auto_dismissed"),
    ],
)
async def test_provider_maps_conditions_and_all_result_fields(adapter, state, native):
    service, _, requests, clients, identities = adapter
    page = await service.read(
        "dependency_alerts", "org/repo", parameters={"state": state, "page_size": 1}
    )
    assert page.model_dump() == {
        "continuation": None,
        "target": None,
        "items": [
            {
                "id": "42",
                "state": "resolved",
                "url": "https://hosting.test/alert/42",
                "package": "demo",
                "ecosystem": "pip",
                "manifest_path": "uv.lock",
                "severity": "high",
                "identifiers": [
                    {"type": "GHSA", "value": "GHSA-example"},
                    {"type": "CVE", "value": "CVE-example"},
                ],
                "summary": "summary",
                "description": "description",
                "affected_versions": "< 2",
                "patched_version": "2",
                "created_at": "2026-01-01",
                "updated_at": "2026-09-30",
            }
        ],
    }
    assert dict(requests[0].url.params) == {"state": native, "per_page": "1"}
    assert requests[0].url.path == "/api/v3/repos/org/repo/dependabot/alerts"
    assert identities == [("aiko", "https://hosting.test/api/v3", "")]
    detail = await service.read(
        "dependency_alerts", "org/repo", parameters={"id": "42"}
    )
    assert detail == page
    assert requests[1].url.path == "/api/v3/repos/org/repo/dependabot/alerts/42"
    await service.aclose()
    assert clients[0].is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subject", "parameters"),
    [
        ("org/repo", {"state": "fixed"}),
        ("org/repo", {"state": "auto_dismissed"}),
        ("org/repo", {"per_page": 1}),
        ("org/repo", {"page_size": True}),
        ("org/repo", {"page_size": 101}),
        ("org/repo", {"method": "PATCH"}),
        ("org/repo", {"headers": {}}),
        ("org/repo", {"after": "cursor"}),
        ("org/repo", {"id": "42", "state": "open"}),
        ("org/repo", {"id": "alert:opaque"}),
        ("org/repo", ["open"]),
        # GitHub's repositories are an owner and a name.
        ("group/sub/repo", {}),
        ("https://github.com/org/repo", {}),
    ],
)
async def test_native_or_unapproved_parameters_are_not_public(
    adapter, subject, parameters
):
    service, _, requests, clients, _ = adapter
    with pytest.raises(ServiceError):
        await service.read("dependency_alerts", subject, parameters=parameters)
    assert requests == clients == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"number": None},
        {"number": True},
        {"state": "unexpected"},
        {"dependency": "bad"},
        {"security_advisory": {"identifiers": ["bad"]}},
        {"security_vulnerability": {"first_patched_version": "bad"}},
    ],
)
async def test_malformed_provider_fields_fail_instead_of_empty_success(adapter, change):
    service, body, _, _, _ = adapter
    body[0].update(change)
    with pytest.raises(ServiceError):
        await service.read("dependency_alerts", "org/repo")
    await service.aclose()


class OtherHostingService(CodeHostingService):
    """A code host of another provider: a repository three names deep and
    alerts named by opaque ids."""

    def __init__(self, fail=False):
        self.closed = False
        self.fail = fail

    async def read(self, resource, subject, *, parameters=None, continuation=""):
        assert resource == "dependency_alerts"
        assert subject == "group/subgroup/repo"
        assert parameters == {"id": "alert:opaque"}
        if self.fail:
            raise ServiceError("denied")
        return ReadPage.of(
            resource, [DependencyAlert(id=parameters["id"], state="open")]
        )

    async def aclose(self):
        self.closed = True


def _read_through_the_cli(monkeypatch, service: CodeHostingService, configured: Team):
    """A host client whose ``member`` call runs the member CLI in process, its
    member resolved to ``service``."""
    from guildbotics.cli import member as cli
    from guildbotics.runtime.member_invocation import MemberInvocation

    person = Person(person_id="aiko", name="Aiko")
    context = MemberServices(person, configured, service)
    monkeypatch.setattr(cli, "resolve_member_context", lambda _: (context, person))

    class Window:
        def __init__(self):
            self.sent: list[list[str]] = []

        async def acall(self, name, *, arguments, stdin):
            assert name == "member"
            self.sent.append(arguments)
            # The broker runs a member command on a worker thread of its own.
            code, stdout, stderr = await asyncio.to_thread(
                cli.run_in_process,
                arguments,
                MemberInvocation(),
                cwd=Path.cwd(),
                stdin=stdin,
            )
            return {"exit_code": code, "stdout": stdout, "stderr": stderr}

    return Window()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_a_command_reads_another_provider_through_the_member_cli(
    monkeypatch, fail
):
    """A command's environment reads the configured code host through
    ``repo-info read``, with the provider's own subject and ids."""
    service = OtherHostingService(fail)
    configured = team("other")
    assert configured.project.get_service_name(Service.CODE_HOSTING_SERVICE) == "other"
    window = _read_through_the_cli(monkeypatch, service, configured)
    proxy = WindowCodeHostingService(window, "aiko")

    if fail:
        with pytest.raises(ServiceError, match="^denied$"):
            await proxy.read(
                "dependency_alerts",
                "group/subgroup/repo",
                parameters={"id": "alert:opaque"},
            )
    else:
        result = await proxy.read(
            "dependency_alerts",
            "group/subgroup/repo",
            parameters={"id": "alert:opaque"},
        )
        assert result.items[0]["id"] == "alert:opaque"
    [arguments] = window.sent
    assert arguments[:4] == ["repo-info", "read", "--person", "aiko"]
    assert arguments[arguments.index("--subject") + 1] == "group/subgroup/repo"
    assert service.closed


@pytest.mark.asyncio
async def test_normalized_output_limit_applies_to_every_provider(monkeypatch):
    service = OtherHostingService()
    context = MemberServices(
        Person(person_id="aiko", name="Aiko"), team("other"), service
    )
    monkeypatch.setattr(member_repository, "MAX_PAGE_BYTES", 10)
    with pytest.raises(ServiceError):
        await member_repository.read_resource(
            context,
            "dependency_alerts",
            "group/subgroup/repo",
            parameters={"id": "alert:opaque"},
            continuation="",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource", "group"),
    [
        (resource, group)
        for subject, group in (
            ("ticket", "ticket"),
            ("pull_request", "pr"),
            ("repository", "repo-info"),
        )
        for resource in RESOURCES_BY_SUBJECT[subject]
    ],
)
async def test_a_command_reads_each_resource_through_its_subjects_member_command(
    resource, group
):
    """A command's environment reads a ticket's resources with ``ticket read``,
    a pull request's with ``pr read``, and the repository's with
    ``repo-info read``."""
    sent: list[list[str]] = []

    class Window:
        async def acall(self, name, *, arguments, stdin):
            sent.append(arguments)
            return {"exit_code": 0, "stdout": json.dumps({"items": []}), "stderr": ""}

    await WindowCodeHostingService(Window(), "aiko").read(
        resource, "https://example.com/item/1", parameters={"page_size": 5}
    )

    [arguments] = sent
    assert arguments[:4] == [group, "read", "--person", "aiko"]
    options = dict(zip(arguments[4::2], arguments[5::2], strict=True))
    assert options == {
        "--resource": resource,
        "--subject": "https://example.com/item/1",
        "--params": '{"page_size": 5}',
        "--continuation": "",
        "--format": "json",
    }


@pytest.mark.asyncio
async def test_a_command_cannot_read_a_resource_no_code_host_has():
    class Window:
        async def acall(self, name, *, arguments, stdin):
            raise AssertionError("nothing is asked of the host")

    with pytest.raises(ServiceError):
        await WindowCodeHostingService(Window(), "aiko").read("secrets", "owner/repo")
