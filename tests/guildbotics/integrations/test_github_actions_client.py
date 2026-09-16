from contextlib import asynccontextmanager
from io import BytesIO

import pytest

from guildbotics.integrations.github.actions_client import (
    GitHubActionsClient,
    GitHubActionsClientError,
    _raise_for_status,
)


HTTP_FOUND = 302


class FakeResponse:
    """An answer shaped the way httpx reports GitHub's.

    A redirect is a real ``302``, and ``raise_for_status`` raises for every
    status that is not a success -- redirects included, as httpx does. Modelling
    a redirect as a ``200`` would hide a caller that checks the status before
    reading where it was sent.
    """

    status_code = 200

    def __init__(self, *, payload=None, content=b"", location="", status_code=None):
        self._payload = payload
        self.content = content
        self.headers = {"location": location} if location else {}
        if status_code is None:
            status_code = HTTP_FOUND if location else 200
        self.status_code = status_code

    @property
    def has_redirect_location(self):
        return "location" in self.headers and self.status_code != 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not 200 <= self.status_code < 300:
            raise RuntimeError(f"status {self.status_code}")


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.gets = []

    async def get(self, endpoint, **kwargs):
        self.gets.append((endpoint, kwargs))
        return self.responses[endpoint]


@pytest.mark.asyncio
async def test_job_log_follows_redirect_without_reusing_api_client():
    api = FakeClient(
        {
            "/repos/owner/repo/actions/jobs/9/logs": FakeResponse(
                location="https://signed.example.test/job-log"
            )
        }
    )
    downloads = []

    async def download_tail(url, tail_bytes):
        downloads.append((url, tail_bytes))
        return b"tail", True

    client = GitHubActionsClient(api, download_tail=download_tail)

    result = await client.job_log_tail("owner", "repo", 9, 100)

    assert result == (b"tail", True)
    assert downloads == [("https://signed.example.test/job-log", 100)]
    assert len(api.gets) == 1


@pytest.mark.asyncio
async def test_artifact_archive_applies_limit_to_direct_response():
    api = FakeClient(
        {
            "/repos/owner/repo/actions/artifacts/9/zip": FakeResponse(
                content=b"too large"
            )
        }
    )
    client = GitHubActionsClient(api)

    with pytest.raises(GitHubActionsClientError, match="exceeds the 3 byte limit"):
        async with client.artifact_archive("owner", "repo", 9, 3):
            pass


@pytest.mark.asyncio
async def test_artifact_archive_passes_limit_to_signed_download():
    api = FakeClient(
        {
            "/repos/owner/repo/actions/artifacts/9/zip": FakeResponse(
                location="https://signed.example.test/artifact"
            )
        }
    )
    downloads = []

    @asynccontextmanager
    async def download(url, max_bytes):
        downloads.append((url, max_bytes))
        yield BytesIO(b"zip")

    client = GitHubActionsClient(api, download=download)

    async with client.artifact_archive("owner", "repo", 9, 1024) as result:
        assert result.read() == b"zip"
    assert downloads == [("https://signed.example.test/artifact", 1024)]


@pytest.mark.parametrize(
    ("endpoint", "call"),
    [
        (
            "/repos/owner/repo/actions/jobs/9/logs",
            lambda client: client.job_log_tail("owner", "repo", 9, 100),
        ),
        (
            "/repos/owner/repo/actions/artifacts/9/zip",
            lambda client: _enter(client.artifact_archive("owner", "repo", 9, 1024)),
        ),
    ],
    ids=["job log", "artifact archive"],
)
@pytest.mark.asyncio
async def test_an_endpoint_that_redirects_still_reports_a_refusal(endpoint, call):
    """Ruling out the redirect first must not swallow the statuses that fail."""
    api = FakeClient({endpoint: FakeResponse(status_code=403)})
    client = GitHubActionsClient(api)

    with pytest.raises(GitHubActionsClientError) as error:
        await call(client)

    assert "token or GitHub App" in str(error.value)


async def _enter(manager):
    async with manager:
        pass


def test_api_permission_error_distinguishes_token_and_app_setup():
    with pytest.raises(GitHubActionsClientError) as error:
        _raise_for_status(FakeResponse(status_code=403), permission_guidance=True)

    assert "token or GitHub App" in str(error.value)
    assert "For a GitHub App" in str(error.value)


def test_signed_download_error_does_not_report_api_permission_guidance():
    with pytest.raises(GitHubActionsClientError) as error:
        _raise_for_status(FakeResponse(status_code=403))

    assert "token or GitHub App" not in str(error.value)
