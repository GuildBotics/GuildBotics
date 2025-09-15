import datetime as dt
import time
from typing import ClassVar, Optional

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from guildbotics.entities.message import Message
from guildbotics.entities.team import Person
from guildbotics.integrations.github.async_client import get_async_client


class GitHubTokenAuth(httpx.Auth):

    requires_request_body = False
    requires_response_body = False

    def __init__(self, token: str):
        self.token = token

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"token {self.token}"
        request.headers.setdefault("Accept", "application/vnd.github.v3+json")
        yield request


class GitHubAppAuth(httpx.Auth):

    HUMAN: ClassVar[str] = "human"
    MACHINE_USER: ClassVar[str] = "machine_user"
    GITHUB_APPS: ClassVar[str] = "github_apps"
    PROXY_AGENT: ClassVar[str] = "proxy_agent"

    requires_request_body = True
    requires_response_body = True

    def __init__(self, app_id: str, installation_id: str, private_key_path: str):
        self.app_id = app_id
        self.installation_id = installation_id
        self.private_key_path = private_key_path
        self._token: Optional[str] = None
        self._expires_at: Optional[dt.datetime] = None
        self._leeway = dt.timedelta(seconds=120)

        with open(self.private_key_path, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
            if not isinstance(key, RSAPrivateKey):
                raise TypeError("Private key must be an RSA private key")
            self._private_key = key

    def _need_refresh(self) -> bool:
        now = dt.datetime.now(dt.timezone.utc)
        return (
            self._token is None
            or self._expires_at is None
            or now >= (self._expires_at - self._leeway)
        )

    def _build_refresh_request(self, request: httpx.Request) -> httpx.Request:
        refresh_url = request.url.copy_with(
            path=f"/app/installations/{self.installation_id}/access_tokens",
            query=None,
        )
        now = int(time.time()) - 60
        payload = {"iat": now, "exp": now + 10 * 60, "iss": self.app_id}
        jwt_token = jwt.encode(payload, self._private_key, algorithm="RS256")
        ua = request.headers.get("user-agent", "GuildBotics/1.0")
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": ua,
        }
        return httpx.Request("POST", refresh_url, headers=headers)

    def _update_token_from_response(self, resp: httpx.Response) -> None:
        data = resp.json()
        self._token = data["token"]
        exp = data["expires_at"].replace("Z", "+00:00")
        self._expires_at = dt.datetime.fromisoformat(exp)

    def auth_flow(self, request: httpx.Request):
        if self._need_refresh():
            refresh_req = self._build_refresh_request(request)
            refresh_resp = yield refresh_req
            self._update_token_from_response(refresh_resp)

        request.headers["Authorization"] = f"token {self._token}"
        request.headers.setdefault("Accept", "application/vnd.github.v3+json")
        request.headers.setdefault("X-GitHub-Api-Version", "2022-11-28")

        try:
            response = yield request
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                refresh_req = self._build_refresh_request(request)
                refresh_resp = yield refresh_req
                self._update_token_from_response(refresh_resp)
                request.headers["Authorization"] = f"token {self._token}"
                yield request
            else:
                raise
        return


async def create_github_client(person: Person, base_url: str) -> httpx.AsyncClient:
    """
    Create an authenticated GitHub API client.
    Args:
        person (Person): The person object with GitHub credentials.
        base_url (str): The base URL for the GitHub API.
    Returns:
        AsyncClient: An authenticated httpx.AsyncClient.
    """
    auth: Optional[httpx.Auth] = None

    if person.person_type == GitHubAppAuth.GITHUB_APPS:
        # Use GitHub App authentication with auto-refresh
        app_id = person.get_secret("github_app_id")
        installation_id = person.get_secret("github_installation_id")
        private_key_path = person.get_secret("github_private_key_path")
        auth = GitHubAppAuth(
            app_id=app_id,
            installation_id=installation_id,
            private_key_path=private_key_path,
        )
    else:
        # Use personal access token
        auth = GitHubTokenAuth(person.get_secret("github_access_token"))

    client = get_async_client(base_url=base_url, auth=auth)
    client.headers.update(
        {
            "Accept": "application/vnd.github.v3+json",
        }
    )
    return client


def get_signature_line(content: str) -> str:
    """
    Get the signature for a given content.

    Args:
        content (str): The content to check.

    Returns:
        str: The signature.
    """
    lines = content.splitlines()
    return lines[-1] if lines else ""


def get_github_username(person: Person, strict: bool = False) -> str:
    """
    Get the GitHub username of the person.

    Returns:
        str: The GitHub username.
    """
    if strict:
        return person.account_info["github_username"]
    return person.account_info.get("github_username", "")


def get_person_name(members: list[Person], username: str, comment_body: str) -> str:
    """
    Get the person name associated with a GitHub username.

    Args:
        username (str): The GitHub username.
        comment_body (str): The body of the comment.

    Returns:
        str: The person name.
    """
    signature = get_signature_line(comment_body)
    if signature.startswith("⚙"):
        person_id = signature[1:].strip()
        for member in members:
            if member.person_id == person_id:
                return member.name

    for member in members:
        if get_github_username(member) == username:
            return member.name
    return ""


def is_proxy_agent(person: Person) -> bool:
    """
    Check if the current person is a proxy agent.

    Returns:
        bool: True if the person is a proxy agent, False otherwise.
    """
    return person.person_type == "proxy_agent"


def get_proxy_agent_signature(person: Person) -> str:
    """
    Get the signature for the proxy agent.

    Returns:
        str: The proxy agent's signature.
    """
    return f"⚙{person.person_id}"


def get_author_type(person: Person, username: str, content: str) -> str:
    """
    Get the author type (user or assistant) for a given username.

    Args:
        username (str): The username to check.
        content (str): The content of the message.

    Returns:
        str: The author type.
    """
    if username == get_github_username(person, strict=True):
        return Message.ASSISTANT
    if is_proxy_agent(person):
        if get_signature_line(content) == get_proxy_agent_signature(person):
            return Message.ASSISTANT
    return Message.USER
