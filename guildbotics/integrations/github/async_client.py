import httpx


async def raise_for_status_with_text(response: httpx.Response):
    if response.is_error:
        await response.aread()
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


def get_async_client(base_url: str, auth: httpx.Auth) -> httpx.AsyncClient:
    """
    Create and return an async HTTP client with the specified base URL and headers.

    Args:
        base_url (str): The base URL for the client.
        auth (httpx.Auth): Authentication class to use for the client.

    Returns:
        httpx.AsyncClient: An instance of AsyncClient configured with the provided base URL and headers.
    """
    return httpx.AsyncClient(
        base_url=base_url,
        auth=auth,
        timeout=10.0,
        event_hooks={
            "response": [raise_for_status_with_text],
        },
    )
