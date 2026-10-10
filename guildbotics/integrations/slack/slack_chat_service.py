from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from logging import Logger
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import httpx

from guildbotics.entities.team import Person
from guildbotics.integrations.slack.auth_errors import (
    is_slack_auth_error,
    slack_api_error,
)
from guildbotics.integrations.slack.message_events import (
    MENTION_PATTERN,
    chat_event,
    mentioned_user_ids,
    time_ts,
    ts_time,
)
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.runtime.chat_service import (
    ChatCredentialsError,
    ChatEventPage,
    ChatIdentity,
    ChatMessageRef,
    ChatPostResult,
    ChatService,
    ChatServiceError,
    ChatThreadNotFoundError,
    CredentialCheck,
    SemanticReaction,
)

_EPHEMERAL_PARTICIPANT_LABEL_RE = re.compile(r"^(?:user|agent)_\d+$", re.IGNORECASE)
_PARTICIPANT_LABEL_MENTION_RE = re.compile(r"@([A-Za-z0-9_-]+)")
_SLACK_REACTION_MAP: dict[SemanticReaction, str] = {
    "ack": "white_check_mark",
    "agree": "thumbsup",
    "celebrate": "tada",
    "support": "heart",
}
HTTP_UNAUTHORIZED = 401
DEFAULT_BASE_URL = "https://slack.com/api"
#: The digits after the ``p`` of a permalink's last path segment that are the
#: microseconds of the message's timestamp.
_PERMALINK_FRACTION_DIGITS = 6
#: ``archives``, the channel, and the message, at the end of a permalink's path.
_PERMALINK_PARTS = 3


def _record_slack_auth_failure(code: str) -> None:
    record_correlated_event(
        event_type="credential.failed",
        default_source="slack",
        attributes={
            "credential.provider": "slack",
            "error.category": "authentication",
        },
        payload={"provider": "slack", "code": code},
    )


class SlackApiError(ChatServiceError):
    """Raised when Slack returns ok=false for a Web API method."""

    def __init__(
        self, method: str, error: str, needed: str = "", provided: str = ""
    ) -> None:
        self.method = method
        self.error = error
        # Slack returns these alongside ``missing_scope``. Without them the
        # message only says a scope is missing, never which one, which is the
        # single fact needed to fix the app's configuration.
        self.needed = needed
        self.provided = provided
        message = f"Slack API '{method}' failed: {error}"
        if needed:
            message += f" (needed: {needed}"
            message += f", provided: {provided})" if provided else ")"
        super().__init__(message)


class SlackChatService(ChatService):
    """Slack Web API-backed chat service (MVP subset)."""

    def __init__(
        self,
        logger: Logger,
        *,
        token: str | None = None,
        token_name: str = "the Slack bot token",
        app_token: str = "",
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        record_credential_events: bool = True,
    ) -> None:
        self._logger = logger
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._token = token or ""
        #: Where the bot token is configured, for when it is not.
        self._token_name = token_name
        self._app_token = app_token
        self._client = client
        self._transport = transport
        # Setup-time checks try credentials the user is still editing, so a
        # rejection there is an answer to a question rather than a runtime
        # credential failure worth alerting the whole desktop about.
        self._record_credential_events = record_credential_events
        self._owns_client = client is None
        self._channel_name_cache: dict[str, str] = {}

    async def get_bot_identity(self) -> ChatIdentity:
        payload = await self._post_form("auth.test", {})
        return ChatIdentity(
            user_id=str(payload.get("user_id", "")),
            display_name=str(payload.get("user", "")),
            workspace=str(payload.get("team", "")),
        )

    async def check_credentials(self) -> list[CredentialCheck]:
        """Try the bot token with ``auth.test`` and the app-level token with
        ``apps.connections.open``, the call the event listener makes; a failure
        names the Slack error code, never a value."""
        return [
            await _check("bot_token", self._token, self.get_bot_identity),
            await _check(
                "app_token",
                self._app_token,
                lambda: probe_app_token(
                    self._app_token, self._base_url, transport=self._transport
                ),
            ),
        ]

    def self_user_id(self, person: Person) -> str:
        return str(person.account_info.get("slack_user_id", "")).strip()

    def parse_message_url(self, url: str) -> ChatMessageRef:
        """The message of a Slack permalink: ``.../archives/<channel>/p<ts>``,
        with ``?thread_ts=<ts>`` for a reply."""
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")
        if len(parts) < _PERMALINK_PARTS or parts[-3] != "archives":
            raise ChatServiceError("Slack message URL must be an /archives/... URL.")
        digits = parts[-1].removeprefix("p")
        if not digits.isdigit() or len(digits) <= _PERMALINK_FRACTION_DIGITS:
            raise ChatServiceError("Slack message URL contains an invalid message id.")
        message_id = (
            f"{digits[:-_PERMALINK_FRACTION_DIGITS]}."
            f"{digits[-_PERMALINK_FRACTION_DIGITS:]}"
        )
        thread = parse_qs(parsed.query).get("thread_ts", [message_id])[0]
        return ChatMessageRef(
            channel_id=parts[-2], message_id=message_id, thread_id=thread
        )

    def mentioned_user_ids(self, text: str) -> list[str]:
        return mentioned_user_ids(text)

    async def list_channel_events(
        self,
        channel_id: str,
        *,
        cursor: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> ChatEventPage:
        form: dict[str, str] = {
            "channel": channel_id,
            "limit": str(limit),
            "include_all_metadata": "true",
            # Both bounds are inclusive, as the port's are.
            "inclusive": "true",
        }
        if cursor:
            form["cursor"] = cursor
        if since:
            form["oldest"] = time_ts(since)
        if until:
            form["latest"] = time_ts(until)
        return _page(channel_id, await self._post_form("conversations.history", form))

    async def list_thread_events(
        self,
        channel_id: str,
        *,
        thread_id: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> ChatEventPage:
        form: dict[str, str] = {
            "channel": channel_id,
            "ts": thread_id,
            "limit": str(limit),
            "include_all_metadata": "true",
        }
        if cursor:
            form["cursor"] = cursor
        try:
            payload = await self._post_form("conversations.replies", form)
        except SlackApiError as exc:
            if exc.error == "thread_not_found":
                raise ChatThreadNotFoundError(str(exc)) from exc
            raise
        return _page(channel_id, payload)

    async def probe_read_scopes(self) -> None:
        """Make the narrowest scoped read call, to prove scopes were granted.

        ``auth.test`` needs no scope at all, so it cannot tell a fully scoped
        token from one that was issued before the app's scopes were added.
        This asks for a single conversation instead, which fails with
        ``missing_scope`` when the token lacks the channel read scopes.
        """
        await self._post_form(
            "conversations.list",
            {
                "limit": "1",
                "exclude_archived": "true",
                "types": "public_channel,private_channel",
            },
        )

    async def resolve_channel_id(self, channel_name: str) -> str | None:
        name = channel_name.strip().lstrip("#")
        if not name:
            return None
        cached = self._channel_name_cache.get(name)
        if cached:
            return cached

        cursor: str | None = None
        while True:
            form: dict[str, str] = {
                "limit": "1000",
                "exclude_archived": "true",
                "types": "public_channel,private_channel",
            }
            if cursor:
                form["cursor"] = cursor
            payload = await self._post_form("conversations.list", form)
            channels = payload.get("channels", [])
            for item in channels:
                if not isinstance(item, dict):
                    continue
                cid = _str_or_none(item.get("id"))
                if not cid:
                    continue
                raw_name = str(item.get("name", "") or "")
                normalized = str(item.get("name_normalized", "") or "")
                if name in (raw_name, normalized):
                    self._channel_name_cache[name] = cid
                    return cid
            metadata = payload.get("response_metadata", {})
            next_cursor = None
            if isinstance(metadata, dict):
                next_cursor = _str_or_none(metadata.get("next_cursor"))
            if not next_cursor:
                return None
            cursor = next_cursor

    async def post_message(
        self,
        channel_id: str,
        text: str,
        *,
        thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ChatPostResult:
        form: dict[str, str] = {"channel": channel_id, "text": text}
        if thread_id:
            form["thread_ts"] = thread_id
        if metadata:
            form["metadata"] = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        payload = await self._post_form("chat.postMessage", form)
        ts = str(payload.get("ts", ""))
        return ChatPostResult(
            channel_id=channel_id,
            message_id=ts,
            thread_id=thread_id or ts,
            occurred_at=ts_time(ts),
        )

    async def add_reaction(
        self, channel_id: str, message_id: str, reaction: str
    ) -> None:
        if reaction not in _SLACK_REACTION_MAP:
            raise ChatServiceError(
                f"Unsupported semantic reaction for Slack: {reaction}"
            )
        reaction_name = _SLACK_REACTION_MAP[cast(SemanticReaction, reaction)]
        try:
            await self._post_form(
                "reactions.add",
                {"channel": channel_id, "timestamp": message_id, "name": reaction_name},
            )
        except SlackApiError as exc:
            # Repeating the same reaction after a crash between API success and
            # evidence persistence must recover, not fail or add another action.
            if exc.error != "already_reacted":
                raise

    def normalize_participant_text(
        self, text: str, participant_labels: dict[str, str]
    ) -> str:
        def repl(match: re.Match[str]) -> str:
            user_id = match.group(1)
            label = participant_labels.get(user_id, "participant")
            return f"@{label}"

        return MENTION_PATTERN.sub(repl, text or "")

    def render_participant_text(
        self, text: str, participant_labels: dict[str, str]
    ) -> str:
        ephemeral_labels = {
            label.casefold()
            for label in participant_labels.values()
            if label and _EPHEMERAL_PARTICIPANT_LABEL_RE.match(label)
        }
        label_to_user_id = {
            label.casefold(): user_id
            for user_id, label in participant_labels.items()
            if user_id and label and not _EPHEMERAL_PARTICIPANT_LABEL_RE.match(label)
        }

        def repl(match: re.Match[str]) -> str:
            label = match.group(1).strip().casefold()
            if label in ephemeral_labels:
                return match.group(1)
            user_id = label_to_user_id.get(label)
            if not user_id:
                return match.group(0)
            return f"<@{user_id}>"

        return _PARTICIPANT_LABEL_MENTION_RE.sub(repl, text or "")

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _post_form(self, method: str, form: dict[str, str]) -> dict[str, Any]:
        client = self._get_client()
        response = await client.post(f"{self._base_url}/{method}", data=form)
        if response.status_code == HTTP_UNAUTHORIZED and self._record_credential_events:
            _record_slack_auth_failure("unauthorized")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"Slack API '{method}' returned non-object JSON.")
        if not payload.get("ok", False):
            error = str(payload.get("error", "unknown_error") or "unknown_error")
            if is_slack_auth_error(error) and self._record_credential_events:
                _record_slack_auth_failure(error)
            raise SlackApiError(
                method,
                error,
                needed=str(payload.get("needed", "") or ""),
                provided=str(payload.get("provided", "") or ""),
            )
        return payload

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            if not self._token:
                raise ChatCredentialsError(
                    f"Slack bot token is not configured: set {self._token_name}."
                )
            headers = {}
            headers["Authorization"] = f"Bearer {self._token}"
            self._client = httpx.AsyncClient(
                timeout=10.0, transport=self._transport, headers=headers
            )
            self._owns_client = True
        return self._client


def _page(channel_id: str, payload: dict[str, Any]) -> ChatEventPage:
    events = [
        event
        for item in payload.get("messages", [])
        if isinstance(item, dict)
        and (event := chat_event(channel_id, item)) is not None
    ]
    metadata = payload.get("response_metadata", {})
    cursor = (
        _str_or_none(metadata.get("next_cursor"))
        if isinstance(metadata, dict)
        else None
    )
    return ChatEventPage(events=events, cursor=cursor)


async def _check(
    name: str, token: str, probe: Callable[[], Awaitable[object]]
) -> CredentialCheck:
    if not token:
        return CredentialCheck(name=name, status="unconfigured")
    try:
        await probe()
    except Exception as exc:
        # Whatever answered, the credential did not work: say so, not fail.
        return CredentialCheck(
            name=name, status="failed", error=str(exc) or type(exc).__name__
        )
    return CredentialCheck(name=name, status="ok")


async def probe_app_token(
    app_token: str,
    base_url: str | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Validate an app-level token via ``apps.connections.open``.

    Raises:
        ChatServiceError: Carrying only the Slack error code (``invalid_auth``,
            say), which is no secret.
    """
    url = (base_url or DEFAULT_BASE_URL).rstrip("/") + "/apps.connections.open"
    async with httpx.AsyncClient(
        timeout=10.0,
        transport=transport,
        headers={"Authorization": f"Bearer {app_token}"},
    ) as client:
        response = await client.post(url)
        response.raise_for_status()
        payload = response.json()
    if error := slack_api_error(payload):
        raise ChatServiceError(error)


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if s else None
