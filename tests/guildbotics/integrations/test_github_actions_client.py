import pytest

from guildbotics.integrations.github.actions_client import (
    GitHubActionsClient,
    GitHubActionsClientError,
)


class FakeResponse:
    status_code = 200

    def __init__(self, *, payload=None, content=b"", location=""):
        self._payload = payload
        self.content = content
        self.headers = {"location": location} if location else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


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
        await client.artifact_archive("owner", "repo", 9, 3)


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

    async def download(url, max_bytes):
        downloads.append((url, max_bytes))
        return b"zip"

    client = GitHubActionsClient(api, download=download)

    result = await client.artifact_archive("owner", "repo", 9, 1024)

    assert result == b"zip"
    assert downloads == [("https://signed.example.test/artifact", 1024)]
