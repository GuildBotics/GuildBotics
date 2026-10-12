from collections.abc import Callable
from contextlib import asynccontextmanager
from io import BytesIO

import httpx
import pytest

from guildbotics.integrations.github import async_client, github_utils
from guildbotics.integrations.github.actions_client import GitHubActionsClient
from guildbotics.runtime.port import ServiceError
from guildbotics.utils.i18n_tool import t

Respond = Callable[[httpx.Request], httpx.Response]


def _member_client(respond: Respond, seen: list[httpx.Request]) -> httpx.AsyncClient:
    """A member's GitHub client, gate and response hook included, whose
    transport answers with ``respond``."""

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond(request)

    client = async_client.get_async_client(
        "https://api.github.com", github_utils.GitHubTokenAuth("token"), "owner"
    )
    client._transport = httpx.MockTransport(record)
    client._mounts = {}
    return client


def _pages(key: str) -> Respond:
    """A collection of 101 items under ``key``, 100 to a page."""

    def respond(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        ids = range(100) if page == 1 else [100]
        return httpx.Response(200, json={key: [{"id": index} for index in ids]})

    return respond


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read", "path", "key"),
    [
        ("check_runs", "/repos/owner/repo/commits/abc/check-runs", "check_runs"),
        ("commit_statuses", "/repos/owner/repo/commits/abc/status", "statuses"),
    ],
)
async def test_action_collections_and_commit_statuses_read_every_page(read, path, key):
    seen: list[httpx.Request] = []
    async with _member_client(_pages(key), seen) as api:
        items = await getattr(GitHubActionsClient(api), read)("owner/repo", "abc")

    assert [item["id"] for item in items] == list(range(101))
    assert [(r.url.path, dict(r.url.params)) for r in seen] == [
        (path, {"per_page": "100", "page": "1"}),
        (path, {"per_page": "100", "page": "2"}),
    ]


@pytest.mark.asyncio
async def test_a_collection_of_another_shape_is_an_invalid_response():
    seen: list[httpx.Request] = []
    respond = lambda _: httpx.Response(200, json={"jobs": "not a list"})  # noqa: E731
    async with _member_client(respond, seen) as api:
        with pytest.raises(ServiceError) as raised:
            await GitHubActionsClient(api).jobs("owner/repo", 7)

    assert str(raised.value) == t("integrations.github.response.response")


@pytest.mark.asyncio
async def test_job_log_follows_redirect_without_reusing_api_client():
    seen: list[httpx.Request] = []
    downloads = []

    async def download_tail(url, tail_bytes):
        downloads.append((url, tail_bytes))
        return b"tail", True

    respond = lambda _: httpx.Response(  # noqa: E731
        302, headers={"location": "https://signed.example.test/job-log"}
    )
    async with _member_client(respond, seen) as api:
        client = GitHubActionsClient(api, download_tail=download_tail)
        result = await client.job_log_tail("owner/repo", 9, 100)

    assert result == (b"tail", True)
    assert downloads == [("https://signed.example.test/job-log", 100)]
    assert [request.url.path for request in seen] == [
        "/repos/owner/repo/actions/jobs/9/logs"
    ]


@pytest.mark.asyncio
async def test_a_job_log_answered_directly_keeps_its_tail():
    seen: list[httpx.Request] = []
    respond = lambda _: httpx.Response(200, content=b"0123456789")  # noqa: E731
    async with _member_client(respond, seen) as api:
        client = GitHubActionsClient(api)
        assert await client.job_log_tail("owner/repo", 9, 4) == (b"6789", True)
        assert await client.job_log_tail("owner/repo", 9, 40) == (b"0123456789", False)


@pytest.mark.asyncio
async def test_artifact_archive_applies_limit_to_direct_response():
    seen: list[httpx.Request] = []
    respond = lambda _: httpx.Response(200, content=b"too large")  # noqa: E731
    async with _member_client(respond, seen) as api:
        client = GitHubActionsClient(api)
        with pytest.raises(ServiceError, match="exceeds the 3 byte limit"):
            async with client.artifact_archive("owner/repo", 9, 3):
                pass


@pytest.mark.asyncio
async def test_artifact_archive_passes_limit_to_signed_download():
    seen: list[httpx.Request] = []
    downloads = []

    @asynccontextmanager
    async def download(url, max_bytes):
        downloads.append((url, max_bytes))
        yield BytesIO(b"zip")

    respond = lambda _: httpx.Response(  # noqa: E731
        302, headers={"location": "https://signed.example.test/artifact"}
    )
    async with _member_client(respond, seen) as api:
        client = GitHubActionsClient(api, download=download)
        async with client.artifact_archive("owner/repo", 9, 1024) as result:
            assert result.read() == b"zip"

    assert downloads == [("https://signed.example.test/artifact", 1024)]


async def _enter(manager):
    async with manager:
        pass


@pytest.mark.parametrize(
    "call",
    [
        lambda client: client.job_log_tail("owner/repo", 9, 100),
        lambda client: _enter(client.artifact_archive("owner/repo", 9, 1024)),
        lambda client: client.workflow_runs("owner/repo", "abc"),
    ],
    ids=["job log", "artifact archive", "workflow runs"],
)
@pytest.mark.asyncio
async def test_a_refusal_names_the_permission_the_read_needs(call):
    """Ruling out the redirect first must not swallow the statuses that fail."""
    seen: list[httpx.Request] = []
    respond = lambda _: httpx.Response(403, text="SECRET-BODY")  # noqa: E731
    async with _member_client(respond, seen) as api:
        with pytest.raises(async_client.GitHubResponseError) as raised:
            await call(GitHubActionsClient(api))

    assert raised.value.status == 403
    assert "Actions / Checks / Commit statuses" in str(raised.value)
    assert "SECRET-BODY" not in str(raised.value)
