"""Provider translation and substitution at every repository-read entry point."""

import ast
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import guildbotics
from guildbotics.capabilities.member_repository import read_repository
from guildbotics.editions.simple.simple_integration_factory import (
    SimpleIntegrationFactory,
)
from guildbotics.entities.team import Person, Project, Service, Team
from guildbotics.integrations.code_hosting_service import (
    CodeHostingService,
    DependencyAlert,
    RepositoryReadError,
    RepositoryReadPage,
)
from guildbotics.integrations.github import code_hosting_service as github
from guildbotics.integrations.window import WindowCodeHostingService


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
                "ticket_manager": {"name": "github"},
            },
        ),
    )


@pytest.mark.parametrize("name", ["", "gitlab", "unsupported"])
def test_unsupported_hosting_never_uses_ticket_provider(name):
    with pytest.raises(RepositoryReadError):
        SimpleIntegrationFactory().create_code_hosting_service(
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
        result = httpx.AsyncClient(
            base_url=base_url, transport=httpx.MockTransport(respond)
        )
        clients.append(result)
        return result

    monkeypatch.setattr(
        "guildbotics.integrations.github.pull_requests.create_github_client", client
    )
    service = SimpleIntegrationFactory().create_code_hosting_service(
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
    assert page.model_dump(exclude={"target"}) == {
        "continuation": None,
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
    detail = await service.read("dependency_alerts", "org/repo", identifier="42")
    assert detail == page
    await service.aclose()
    assert clients[0].is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameters",
    [
        {"state": "fixed"},
        {"state": "auto_dismissed"},
        {"per_page": 1},
        {"page_size": True},
        {"page_size": 101},
        {"method": "PATCH"},
        {"headers": {}},
        {"after": "cursor"},
        [],
    ],
)
async def test_native_or_unapproved_parameters_are_not_public(adapter, parameters):
    service, _, requests, clients, _ = adapter
    with pytest.raises(RepositoryReadError):
        await service.read("dependency_alerts", "org/repo", parameters=parameters)
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
    with pytest.raises(RepositoryReadError):
        await service.read("dependency_alerts", "org/repo")
    await service.aclose()


class OtherHostingService(CodeHostingService):
    def __init__(self, fail=False):
        self.closed = False
        self.fail = fail

    async def read(
        self, resource, repo, *, identifier="", parameters=None, continuation=""
    ):
        assert resource == "dependency_alerts"
        assert repo == "group/subgroup/repo"
        assert identifier == "alert:opaque"
        if self.fail:
            raise RepositoryReadError("denied")
        return RepositoryReadPage(items=[DependencyAlert(id=identifier, state="open")])

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_capability_and_window_use_configured_service_and_close_it(fail):
    service = OtherHostingService(fail)
    calls = []

    def create(logger, person, configured_team):
        calls.append(
            (
                person.person_id,
                configured_team.project.get_service_name(Service.CODE_HOSTING_SERVICE),
            )
        )
        return service

    context = SimpleNamespace(
        logger=logging.getLogger(),
        person=Person(person_id="aiko", name="Aiko"),
        team=team("other"),
        integration_factory=SimpleNamespace(create_code_hosting_service=create),
    )

    class Window:
        async def acall(self, name, *, arguments, stdin):
            assert name == "member" and arguments[:2] == ["repository", "read"]
            assert arguments[arguments.index("--person") + 1] == "aiko"
            result = await read_repository(
                context,
                "dependency_alerts",
                "group/subgroup/repo",
                identifier="alert:opaque",
                parameters={},
                continuation="",
            )
            return {"exit_code": 0, "stdout": json.dumps(result), "stderr": ""}

    proxy = WindowCodeHostingService(Window(), "aiko")
    if fail:
        with pytest.raises(RepositoryReadError, match="denied"):
            await proxy.read(
                "dependency_alerts", "group/subgroup/repo", identifier="alert:opaque"
            )
    else:
        result = await proxy.read(
            "dependency_alerts", "group/subgroup/repo", identifier="alert:opaque"
        )
        assert result.items[0].id == "alert:opaque"
    assert calls == [("aiko", "other")]
    assert service.closed


def test_repository_consumers_cannot_import_provider_modules():
    root = Path(guildbotics.__file__).parent
    consumers = {
        "integrations/code_hosting_service.py",
        "integrations/window.py",
        "runtime/context.py",
        "runtime/integration_factory.py",
        "capabilities/member_repository.py",
        *(
            str(path.relative_to(root))
            for path in (root / "templates/commands/repository").glob("*.py")
        ),
    }
    violations = []
    for name in consumers:
        for node in ast.walk(ast.parse((root / name).read_text(encoding="utf-8"))):
            modules = (
                [f"{node.module}.{entry.name}" for entry in node.names]
                if isinstance(node, ast.ImportFrom)
                else [entry.name for entry in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            for module in modules:
                if (
                    module.startswith("guildbotics.integrations.")
                    and (root / "integrations" / module.split(".")[2]).is_dir()
                ):
                    violations.append((name, node.lineno, module))
    assert not violations


def test_cli_reads_another_provider_without_github_or_numeric_ids(monkeypatch):
    from guildbotics.cli import member as cli
    from guildbotics.runtime.member_invocation import MemberInvocation

    service = OtherHostingService()
    person = Person(person_id="aiko", name="Aiko")
    context = SimpleNamespace(
        logger=logging.getLogger(),
        person=person,
        team=team("other"),
        integration_factory=SimpleNamespace(
            create_code_hosting_service=lambda *_: service
        ),
    )
    monkeypatch.setattr(cli, "resolve_member_context", lambda _: (context, person))
    code, stdout, stderr = cli.run_in_process(
        [
            "repository",
            "read",
            "--person",
            "aiko",
            "--resource",
            "dependency_alerts",
            "--repo",
            "group/subgroup/repo",
            "--identifier",
            "alert:opaque",
        ],
        MemberInvocation(),
        cwd=Path.cwd(),
        stdin="",
    )
    assert (code, stderr) == (0, "")
    assert json.loads(stdout)["items"][0]["id"] == "alert:opaque"
    assert service.closed


@pytest.mark.asyncio
async def test_normalized_output_limit_applies_to_every_provider(monkeypatch):
    from guildbotics.capabilities import member_repository

    service = OtherHostingService()
    context = SimpleNamespace(
        logger=logging.getLogger(),
        person=Person(person_id="aiko", name="Aiko"),
        team=team("other"),
        integration_factory=SimpleNamespace(
            create_code_hosting_service=lambda *_: service
        ),
    )
    monkeypatch.setattr(member_repository, "MAX_PAGE_BYTES", 10)
    with pytest.raises(RepositoryReadError):
        await read_repository(
            context,
            "dependency_alerts",
            "group/subgroup/repo",
            identifier="alert:opaque",
            parameters={},
            continuation="",
        )
    assert service.closed
