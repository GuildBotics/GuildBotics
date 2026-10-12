"""GitHub Checks and Actions API client used by the code host."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from tempfile import TemporaryFile
from typing import IO, Any

import httpx

from guildbotics.integrations.github.async_client import GitHubResponseError
from guildbotics.integrations.github.github_utils import paginated_items
from guildbotics.runtime.port import ServiceError
from guildbotics.utils.i18n_tool import t

_PERMISSION = "Actions / Checks / Commit statuses"

Download = Callable[[str, int], AbstractAsyncContextManager[IO[bytes]]]
DownloadTail = Callable[[str, int], Awaitable[tuple[bytes, bool]]]


class GitHubActionsClient:
    """Read CI state and bounded downloadable content from GitHub."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        download: Download | None = None,
        download_tail: DownloadTail | None = None,
    ) -> None:
        self._client = client
        self._download = download or _download_without_credentials
        self._download_tail = download_tail or _download_tail_without_credentials

    async def check_runs(self, repository: str, sha: str) -> list[dict[str, Any]]:
        return await self._paginated_items(
            f"/repos/{repository}/commits/{sha}/check-runs", "check_runs"
        )

    async def commit_statuses(self, repository: str, sha: str) -> list[dict[str, Any]]:
        return await self._paginated_items(
            f"/repos/{repository}/commits/{sha}/status", "statuses"
        )

    async def workflow_runs(self, repository: str, sha: str) -> list[dict[str, Any]]:
        return await self._paginated_items(
            f"/repos/{repository}/actions/runs",
            "workflow_runs",
            extra_params={"head_sha": sha},
        )

    async def jobs(self, repository: str, run_id: int) -> list[dict[str, Any]]:
        return await self._paginated_items(
            f"/repos/{repository}/actions/runs/{run_id}/jobs", "jobs"
        )

    async def job_log_tail(
        self, repository: str, job_id: int, tail_bytes: int
    ) -> tuple[bytes, bool]:
        location, content = await self._redirect_or_body(
            f"/repos/{repository}/actions/jobs/{job_id}/logs"
        )
        if location:
            return await self._download_tail(location, tail_bytes)
        return content[-tail_bytes:], len(content) > tail_bytes

    async def artifacts(
        self,
        repository: str,
        *,
        name: str | None = None,
        run_id: int | None = None,
    ) -> list[dict[str, Any]]:
        endpoint = f"/repos/{repository}/actions/artifacts"
        if run_id is not None:
            endpoint = f"/repos/{repository}/actions/runs/{run_id}/artifacts"
        extra_params = {"name": name} if name else None
        return await self._paginated_items(
            endpoint, "artifacts", extra_params=extra_params
        )

    @asynccontextmanager
    async def artifact_archive(
        self, repository: str, artifact_id: int, max_bytes: int
    ) -> AsyncIterator[IO[bytes]]:
        location, content = await self._redirect_or_body(
            f"/repos/{repository}/actions/artifacts/{artifact_id}/zip"
        )
        if location:
            async with self._download(location, max_bytes) as archive:
                yield archive
            return
        if len(content) > max_bytes:
            raise ServiceError(f"GitHub download exceeds the {max_bytes} byte limit.")
        with TemporaryFile() as archive:
            archive.write(content)
            archive.seek(0)
            yield archive

    async def _redirect_or_body(self, endpoint: str) -> tuple[str, bytes]:
        """Read an endpoint that answers with either a redirect or the content.

        GitHub serves what it stores elsewhere -- job logs, artifact archives --
        by redirecting to a short-lived signed URL, so a redirect is this
        endpoint's ordinary answer rather than a failure.

        Returns:
            tuple[str, bytes]: The URL to fetch without credentials, or an
                empty string and the content the endpoint answered with.
        """
        response = await self._get(endpoint)
        if response.has_redirect_location:
            return str(response.headers["location"]), b""
        return "", response.content

    async def _paginated_items(
        self,
        endpoint: str,
        key: str,
        *,
        extra_params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        def parse_page(response: Any) -> list[dict[str, Any]]:
            payload = response.json()
            value = payload.get(key, []) if isinstance(payload, dict) else None
            if not isinstance(value, list) or not all(
                isinstance(item, dict) for item in value
            ):
                raise ServiceError(t("integrations.github.response.response"))
            return value

        return [
            item
            async for item in paginated_items(
                self._get, endpoint, parse_page, params=extra_params
            )
        ]

    async def _get(self, endpoint: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.get(endpoint, **kwargs)
        except GitHubResponseError as exc:
            raise exc.needing(_PERMISSION) from None


@asynccontextmanager
async def _download_without_credentials(
    url: str, max_bytes: int
) -> AsyncIterator[IO[bytes]]:
    """Download a short-lived GitHub URL without forwarding GitHub credentials."""
    with TemporaryFile() as archive:
        total = 0
        async with _anonymous(url) as response:
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > max_bytes:
                raise ServiceError(
                    f"GitHub download exceeds the {max_bytes} byte limit."
                )
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ServiceError(
                        f"GitHub download exceeds the {max_bytes} byte limit."
                    )
                archive.write(chunk)
        archive.seek(0)
        yield archive


async def _download_tail_without_credentials(
    url: str, tail_bytes: int
) -> tuple[bytes, bool]:
    """Keep only the tail while streaming a potentially large job log."""
    content = bytearray()
    total = 0
    async with _anonymous(url) as response:
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            content.extend(chunk)
            if len(content) > tail_bytes:
                del content[:-tail_bytes]
    return bytes(content), total > len(content)


@asynccontextmanager
async def _anonymous(url: str) -> AsyncIterator[httpx.Response]:
    """The answer to a GET of ``url``, sent with no credential.

    Raises:
        ServiceError: If it could not be completed or is an error.
    """
    try:
        async with (
            httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client,
            client.stream("GET", url) as response,
        ):
            if response.is_error:
                raise GitHubResponseError.of(response)
            yield response
    except httpx.RequestError:
        raise ServiceError(t("integrations.github.response.transport")) from None
