import httpx

from guildbotics.observability.diagnostics_events import record_correlated_event

HTTP_UNAUTHORIZED = 401


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


def get_async_client(base_url: str, auth: httpx.Auth) -> httpx.AsyncClient:
    """
    Create and return an async HTTP client with the specified base URL and headers.

    Args:
        base_url (str): The base URL for the client.
        auth (httpx.Auth): Authentication class to use for the client.

    Returns:
        httpx.AsyncClient: An instance of AsyncClient configured with the provided base URL and headers.
    """

    async def response_hook(response: httpx.Response) -> None:
        await raise_for_status_with_text(
            response,
            handle_unauthorized=not bool(getattr(auth, "handles_unauthorized", False)),
            person_id=str(getattr(auth, "person_id", "")),
        )

    return httpx.AsyncClient(
        base_url=base_url,
        auth=auth,
        timeout=10.0,
        event_hooks={
            "response": [response_hook],
        },
    )
