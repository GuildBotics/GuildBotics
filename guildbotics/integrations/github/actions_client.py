"""GitHub Checks and Actions API client used by member capabilities."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from http import HTTPStatus
from tempfile import TemporaryFile
from typing import IO, Any

import httpx

GITHUB_PAGE_SIZE = 100
_PERMISSION_GUIDANCE = (
    " Check that the token or GitHub App has Actions, Checks, and Commit statuses "
    "read permissions. For a GitHub App, also approve the permission update for "
    "the installation."
)


class GitHubActionsClientError(RuntimeError):
    """A GitHub Checks or Actions request failed."""


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

    async def check_runs(self, owner: str, repo: str, sha: str) -> list[dict[str, Any]]:
        return await self._paginated_items(
            f"/repos/{owner}/{repo}/commits/{sha}/check-runs", "check_runs"
        )

    async def commit_statuses(
        self, owner: str, repo: str, sha: str
    ) -> list[dict[str, Any]]:
        endpoint = f"/repos/{owner}/{repo}/commits/{sha}/status"
        statuses: list[dict[str, Any]] = []
        page = 1
        while True:
            response = await self._get(
                endpoint, params={"per_page": GITHUB_PAGE_SIZE, "page": page}
            )
            _raise_for_status(response, permission_guidance=True)
            payload = response.json()
            if not isinstance(payload, dict):
                raise GitHubActionsClientError(
                    "Unexpected GitHub commit status response."
                )
            page_items = _dict_items(payload, "statuses")
            statuses.extend(page_items)
            if len(page_items) < GITHUB_PAGE_SIZE:
                return statuses
            page += 1

    async def workflow_runs(
        self, owner: str, repo: str, sha: str
    ) -> list[dict[str, Any]]:
        return await self._paginated_items(
            f"/repos/{owner}/{repo}/actions/runs",
            "workflow_runs",
            extra_params={"head_sha": sha},
        )

    async def jobs(self, owner: str, repo: str, run_id: int) -> list[dict[str, Any]]:
        return await self._paginated_items(
            f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs", "jobs"
        )

    async def job_log_tail(
        self, owner: str, repo: str, job_id: int, tail_bytes: int
    ) -> tuple[bytes, bool]:
        response = await self._get(f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs")
        _raise_for_status(response, permission_guidance=True)
        location = response.headers.get("location", "")
        if location:
            return await self._download_tail(location, tail_bytes)
        content = response.content
        return content[-tail_bytes:], len(content) > tail_bytes

    async def artifacts(
        self,
        owner: str,
        repo: str,
        *,
        name: str | None = None,
        run_id: int | None = None,
    ) -> list[dict[str, Any]]:
        endpoint = f"/repos/{owner}/{repo}/actions/artifacts"
        if run_id is not None:
            endpoint = f"/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts"
        extra_params = {"name": name} if name else None
        return await self._paginated_items(
            endpoint, "artifacts", extra_params=extra_params
        )

    @asynccontextmanager
    async def artifact_archive(
        self, owner: str, repo: str, artifact_id: int, max_bytes: int
    ) -> AsyncIterator[IO[bytes]]:
        async with self._download_endpoint(
            f"/repos/{owner}/{repo}/actions/artifacts/{artifact_id}/zip", max_bytes
        ) as archive:
            yield archive

    @asynccontextmanager
    async def _download_endpoint(
        self, endpoint: str, max_bytes: int
    ) -> AsyncIterator[IO[bytes]]:
        response = await self._get(endpoint)
        _raise_for_status(response, permission_guidance=True)
        location = response.headers.get("location", "")
        if location:
            async with self._download(location, max_bytes) as archive:
                yield archive
            return
        content = response.content
        if len(content) > max_bytes:
            raise GitHubActionsClientError(
                f"GitHub download exceeds the {max_bytes} byte limit."
            )
        with TemporaryFile() as archive:
            archive.write(content)
            archive.seek(0)
            yield archive

    async def _paginated_items(
        self,
        endpoint: str,
        key: str,
        *,
        extra_params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            params = {
                "per_page": GITHUB_PAGE_SIZE,
                "page": page,
                **(extra_params or {}),
            }
            response = await self._get(endpoint, params=params)
            _raise_for_status(response, permission_guidance=True)
            payload = response.json()
            if not isinstance(payload, dict):
                raise GitHubActionsClientError(
                    f"Unexpected GitHub response for '{key}'."
                )
            page_items = _dict_items(payload, key)
            items.extend(page_items)
            if len(page_items) < GITHUB_PAGE_SIZE:
                return items
            page += 1

    async def _get(self, endpoint: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.get(endpoint, **kwargs)
        except httpx.HTTPError as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", "")
            raise GitHubActionsClientError(
                f"GitHub API request failed with status {status_code}."
            ) from exc


@asynccontextmanager
async def _download_without_credentials(
    url: str, max_bytes: int
) -> AsyncIterator[IO[bytes]]:
    """Download a short-lived GitHub URL without forwarding GitHub credentials."""
    with TemporaryFile() as archive:
        total = 0
        async with (
            httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client,
            client.stream("GET", url) as response,
        ):
            _raise_for_status(response)
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > max_bytes:
                raise GitHubActionsClientError(
                    f"GitHub download exceeds the {max_bytes} byte limit."
                )
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise GitHubActionsClientError(
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
    async with (
        httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client,
        client.stream("GET", url) as response,
    ):
        _raise_for_status(response)
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            content.extend(chunk)
            if len(content) > tail_bytes:
                del content[:-tail_bytes]
    return bytes(content), total > len(content)


def _dict_items(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise GitHubActionsClientError(f"Unexpected GitHub response for '{key}'.")
    return value


def _raise_for_status(response: Any, *, permission_guidance: bool = False) -> None:
    try:
        response.raise_for_status()
    except Exception as exc:
        status_code = getattr(response, "status_code", "")
        guidance = (
            _PERMISSION_GUIDANCE
            if permission_guidance
            and status_code in {HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND}
            else ""
        )
        raise GitHubActionsClientError(
            f"GitHub API request failed with status {status_code}.{guidance}"
        ) from exc
