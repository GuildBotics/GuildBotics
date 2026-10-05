import httpx

from guildbotics.integrations.github.repository_scope import check_request
from guildbotics.observability.diagnostics_events import record_correlated_event

HTTP_UNAUTHORIZED = 401


class ResponseTooLarge(httpx.RequestError):
    """A decoded response exceeded the request's byte limit."""


async def _read_bounded(response: httpx.Response, limit: int) -> None:
    content = bytearray()
    async for chunk in response.aiter_bytes():
        if len(content) + len(chunk) > limit:
            raise ResponseTooLarge(
                "Response exceeds byte limit", request=response.request
            )
        content.extend(chunk)
    # Populate HTTPX's read cache so auth refresh and error hooks can read the
    # same bounded body. Auth's requires_response_body runs after this hook.
    response._content = bytes(content)


async def raise_for_status_with_text(
    response: httpx.Response,
    *,
    handle_unauthorized: bool = True,
    person_id: str = "",
):
    if response.is_error:
        await response.aread()
        if response.status_code == HTTP_UNAUTHORIZED and not handle_unauthorized:
            return response
        if response.status_code == HTTP_UNAUTHORIZED:
            record_github_auth_failure(person_id=person_id)
        message = (
            f"HTTP {response.status_code} Error for {response.url}\n"
            f"Response text: {response.text}"
        )
        raise httpx.HTTPStatusError(
            message,
            request=response.request,
            response=response,
        )
    return response


def record_github_auth_failure(
    *, person_id: str = "", code: str = "unauthorized"
) -> None:
    record_correlated_event(
        event_type="credential.failed",
        default_source="github",
        person_id=person_id,
        attributes={
            "credential.provider": "github",
            "error.category": "authentication",
        },
        payload={"provider": "github", "code": code},
    )


def get_async_client(
    base_url: str,
    auth: httpx.Auth,
    owner: str,
) -> httpx.AsyncClient:
    """
    Create and return an async HTTP client with the specified base URL and headers.

    Args:
        base_url (str): The base URL for the client.
        auth (httpx.Auth): Authentication class to use for the client.
        owner (str): The configured owner, the only one whose repositories
            the client writes to.

    Returns:
        httpx.AsyncClient: An instance of AsyncClient configured with the provided base URL and headers.
    """
    base = httpx.URL(base_url)

    async def request_hook(request: httpx.Request) -> None:
        await check_request(owner, base, request, client)

    async def response_hook(response: httpx.Response) -> None:
        max_response_bytes = response.request.extensions.get("max_response_bytes")
        if max_response_bytes is not None:
            await _read_bounded(response, max_response_bytes)
        await raise_for_status_with_text(
            response,
            handle_unauthorized=not bool(getattr(auth, "handles_unauthorized", False)),
            person_id=str(getattr(auth, "person_id", "")),
        )

    client = httpx.AsyncClient(
        base_url=base_url,
        auth=auth,
        timeout=10.0,
        event_hooks={
            "request": [request_hook],
            "response": [response_hook],
        },
    )
    return client
