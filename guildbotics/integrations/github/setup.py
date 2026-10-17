"""GitHub's setup: the items of its services, a member's GitHub account, the
GitHub App registration, and the Projects board the setup screens prepare.

The App registration is a semi-automatic browser round trip: the screen
starts a registration, the user's browser posts the app manifest to
github.com and clicks "Create GitHub App", GitHub redirects back to the local
API with a one-time code, and the code is converted into credentials here.
The screen then polls the registration until the user has installed the app.
Registrations are held in memory only. The PEM never leaves this process
except into the OS secret store: the member save names the registration,
takes the key and the app's IDs from it, and discards it once saved.
"""

from __future__ import annotations

import html
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, computed_field, model_validator

from guildbotics.entities.team import Person, Project, Service, Team
from guildbotics.integrations.github import app_manifest
from guildbotics.integrations.github.github_utils import GitHubAppAuth
from guildbotics.integrations.github.ticket_service import GitHubTicketService
from guildbotics.integrations.provider import (
    AppCredential,
    HostCredential,
    MemberAccount,
    Operation,
    Page,
    ProviderSetup,
    SavedMember,
    SetupError,
    SetupItems,
    html_page,
    message_page,
    page_path,
)
from guildbotics.utils.i18n_tool import t

_NAME = "github"
_API = "https://api.github.com"
_TIMEOUT_SECONDS = 15.0
_PROJECT_URL = re.compile(
    r"^https://github\.com/(?:orgs|users)/([^/\s]+)/projects/([0-9]+)/?$"
)
_APP_URL = re.compile(r"^https://github\.com/organizations/[^/]+/settings/apps/([^/]+)")
_APP_NAME_MAX_LENGTH = 34
_REGISTRATION_TTL_SECONDS = 30 * 60

# -- the items of the services ------------------------------------------------


class LaneMap(SetupItems):
    """The workflow lanes as the board's Status options name them.

    Blank names are the board's defaults. A working lane the board does not
    have only keeps tickets from moving on start, which the diagnostics
    report; ``ready`` and ``done`` must differ.
    """

    ready: str = GitHubTicketService.DEFAULT_LANE_MAP[GitHubTicketService.LANE_READY]
    working: str = GitHubTicketService.DEFAULT_LANE_MAP[
        GitHubTicketService.LANE_WORKING
    ]
    done: str = GitHubTicketService.DEFAULT_LANE_MAP[GitHubTicketService.LANE_DONE]

    @model_validator(mode="after")
    def _defaults(self) -> LaneMap:
        defaults = LaneMap.model_fields
        for lane, spec in defaults.items():
            setattr(self, lane, getattr(self, lane) or spec.default)
        if self.ready == self.done:
            raise ValueError("ready and done lanes must be different")
        return self


class CodeHosting(SetupItems):
    """The owner whose repositories the members write to."""

    owner: str = Field(min_length=1)
    api_base_url: str | None = None


class Board(SetupItems):
    """A Projects board, named by its URL; the owner and the number are the
    URL's."""

    url: str
    owner: str = ""
    project_id: str = ""
    lane_map: LaneMap = Field(default_factory=LaneMap)
    api_base_url: str | None = None

    @model_validator(mode="after")
    def _identity(self) -> Board:
        match = _PROJECT_URL.fullmatch(self.url)
        if match is None:
            raise ValueError(
                "url must be a GitHub Project URL: "
                "https://github.com/orgs/<owner>/projects/<number>"
            )
        self.owner, self.project_id = match.groups()
        return self


# -- a member's account -------------------------------------------------------

AccountType = Literal["", "human", "machine_user", "github_apps"]


class AccountView(BaseModel):
    """A member's stored GitHub account, as the member screen shows it."""

    account_type: AccountType = ""
    username: str = ""
    installation_id: int | None = None
    app_id: int | None = None
    has_private_key: bool = False
    has_access_token: bool = False


class GitHubAccount(MemberAccount):
    """A member's GitHub account: none (``""``), a human's user, a machine
    user with a token, or a GitHub App."""

    account_type: AccountType = ""
    username: str = ""
    installation_id: int | None = None
    app_id: int | None = None
    #: A key file the save reads; the stored key is its content.
    private_key_path: Path | None = None
    #: A finished registration whose IDs and key the save stores, whatever
    #: the form says of them.
    app_registration_id: str = ""
    access_token: str = ""

    def stored(
        self, person_id: str, human: bool
    ) -> tuple[dict[str, str], dict[str, str]]:
        if human:
            return {
                "github_account_type": GitHubAppAuth.HUMAN,
                "github_username": self.username,
            }, {}
        app_id, installation_id, key = self.app_id, self.installation_id, ""
        if self.app_registration_id:
            registration = _registrations.claim(self.app_registration_id, person_id)
            app_id, installation_id = registration.app_id, registration.installation_id
            key = registration.pem
        info = {
            "github_account_type": self.account_type,
            "github_username": self.username,
            "github_app_id": "" if app_id is None else str(app_id),
            "github_installation_id": (
                "" if installation_id is None else str(installation_id)
            ),
        }
        values = {
            "GITHUB_ACCESS_TOKEN": self.access_token,
            "GITHUB_PRIVATE_KEY": key or self._key_file(),
        }
        return (
            {key: value for key, value in info.items() if value},
            {key: value for key, value in values.items() if value},
        )

    def saved(self) -> None:
        _registrations.discard(self.app_registration_id)

    @classmethod
    def credential(cls, member: SavedMember) -> HostCredential | None:
        """A machine user's token, or a GitHub App; a human hands over none."""
        kind = member.account_info.get("github_account_type")
        if kind == GitHubAppAuth.MACHINE_USER:
            token = member.get_secret("GITHUB_ACCESS_TOKEN")
            return HostCredential(token=token) if token else None
        key = member.get_secret("GITHUB_PRIVATE_KEY")
        if kind != GitHubAppAuth.GITHUB_APPS or not key:
            return None
        return HostCredential(
            app=AppCredential(
                app_id=member.account_info.get("github_app_id", ""),
                installation_id=member.account_info.get("github_installation_id", ""),
                private_key=key,
            )
        )

    @classmethod
    def view(
        cls, account_info: Mapping[str, Any], has_secret: Callable[[str], bool]
    ) -> AccountView:
        return AccountView.model_validate(
            {
                "account_type": str(account_info.get("github_account_type", "")),
                "username": str(account_info.get("github_username", "")),
                "installation_id": _number(account_info.get("github_installation_id")),
                "app_id": _number(account_info.get("github_app_id")),
                "has_private_key": has_secret("GITHUB_PRIVATE_KEY"),
                "has_access_token": has_secret("GITHUB_ACCESS_TOKEN"),
            }
        )

    def _key_file(self) -> str:
        if not self.private_key_path:
            return ""
        try:
            return self.private_key_path.expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SetupError(
                "github_private_key_unreadable",
                "GitHub App private key file could not be read.",
            ) from exc


def _number(value: object) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


# -- user resolution and the avatar -------------------------------------------


class UserReference(BaseModel):
    """Whom the member screen names: a user by name or email, or a GitHub App
    by its settings URL."""

    account_type: Literal["human", "machine_user", "github_apps"]
    identity: str = Field(min_length=1)


class ResolvedUser(BaseModel):
    person_id: str
    username: str
    user_id: int
    git_email: str


async def _resolve_user(
    request: UserReference, team: Callable[[], Team]
) -> ResolvedUser:
    if request.account_type == GitHubAppAuth.GITHUB_APPS:
        match = _APP_URL.match(request.identity)
        if match is None:
            raise SetupError("invalid_github_apps_url", "Invalid GitHub Apps URL.")
        return await _user(match.group(1), app=True)
    return await _user(request.identity.split("@", maxsplit=1)[0], app=False)


async def _user(name: str, *, app: bool) -> ResolvedUser:
    username = f"{name}[bot]" if app else name
    data = await _get_user(username)
    user_id = int(data.get("id") or 0)
    return ResolvedUser(
        person_id=name,
        username=username,
        user_id=user_id,
        git_email=f"{user_id}+{username}@users.noreply.github.com",
    )


async def _get_user(username: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{_API}/users/{quote(username)}")
        response.raise_for_status()
        return dict(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        raise SetupError("invalid_github_username", "Invalid GitHub username.") from exc


async def _avatar_url(person: Person, team: Team) -> str:
    username = str(person.account_info.get("github_username", ""))
    if not username:
        raise SetupError(
            "avatar_account_missing", key="integrations.github.setup.username_missing"
        )
    url = str((await _get_user(username)).get("avatar_url") or "")
    if not url:
        raise ValueError(f"GitHub user '{username}' has no avatar URL.")
    return url


# -- the Projects board -------------------------------------------------------


class BoardTarget(BaseModel):
    """The board the form names, which may not be saved yet."""

    url: str = ""


class StatusOptions(BaseModel):
    """The board's Status options; ``available`` is False when they cannot be
    read, and the form falls back to typing lane names."""

    available: bool
    statuses: list[str] = Field(default_factory=list)


class AgentFieldOption(BaseModel):
    """An ``Agent`` field option, keyed by a member's agent token."""

    name: str
    description: str = ""


class AgentFieldState(BaseModel):
    """The board's ``Agent`` field: the members registered as options, and
    the configured non-human members not yet registered (what ensuring the
    field adds). ``available`` is False when it cannot be read."""

    available: bool
    exists: bool = False
    options: list[AgentFieldOption] = Field(default_factory=list)
    missing: list[AgentFieldOption] = Field(default_factory=list)


async def _with_board(
    target: BoardTarget,
    team: Callable[[], Team],
    action: Callable[[GitHubTicketService], Awaitable[Any]],
) -> Any:
    """Run *action* against the board the form names, with the saved members'
    credentials, trying each until one succeeds; ``None`` when the URL does
    not name a board or every attempt fails."""
    try:
        board = Board(url=target.url)
        saved = team()
    except Exception:
        return None
    named = Team(
        project=Project(
            name=saved.project.name or "setup",
            services={
                Service.TICKET_SERVICE.value: board.model_dump(exclude_none=True)
            },
        ),
        members=saved.members,
    )
    logger = logging.getLogger(__name__)
    for member in [m for m in saved.members if m.is_active] or saved.members:
        # Constructing raises for a member without a GitHub username, which
        # is skipped so that a later credentialed member is still tried.
        tickets: GitHubTicketService | None = None
        try:
            tickets = GitHubTicketService(logger, member, named)
            return await action(tickets)
        except Exception:
            continue
        finally:
            if tickets is not None:
                await tickets.aclose()
    return None


async def _status_options(
    target: BoardTarget, team: Callable[[], Team]
) -> StatusOptions:
    statuses = await _with_board(target, team, lambda board: board.get_statuses())
    return StatusOptions(available=statuses is not None, statuses=statuses or [])


def _agent_field(
    action: Callable[[GitHubTicketService], Awaitable[dict[str, Any]]],
) -> Callable[[BoardTarget, Callable[[], Team]], Awaitable[AgentFieldState]]:
    async def run(target: BoardTarget, team: Callable[[], Team]) -> AgentFieldState:
        state = await _with_board(target, team, action)
        if state is None:
            return AgentFieldState(available=False)
        return AgentFieldState(available=True, **state)

    return run


# -- the GitHub App registration ----------------------------------------------


class AppRegistrationStart(BaseModel):
    app_name: str = Field(min_length=1)
    #: The member whose save may store this app's key: the one being edited,
    #: or the ID of the member being added.
    person_id: str = Field(min_length=1)
    organization: str = ""
    #: This local API as the user's browser reaches it; GitHub redirects the
    #: manifest callback there.
    callback_base_url: str = Field(min_length=1)


class AppRegistrationRef(BaseModel):
    state: str = Field(min_length=1)


class AppRegistrationStatus(BaseModel):
    """A registration as the screen follows it."""

    state: str
    status: Literal["pending", "converted", "installed"] = "pending"
    app_name: str
    person_id: str
    slug: str = ""
    app_id: int | None = None
    html_url: str = ""
    username: str = ""
    git_email: str = ""
    installation_id: int | None = None
    installation_check_error: str = ""
    #: Where the screen sends the user's browser to post the manifest.
    start_url: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def installation_page_url(self) -> str:
        return app_manifest.app_installation_page_url(self.slug) if self.slug else ""


class _Registration(AppRegistrationStatus):
    organization: str
    callback_url: str
    created_at: float = Field(default_factory=time.time)
    pem: str = ""

    def status_view(self) -> AppRegistrationStatus:
        return AppRegistrationStatus.model_validate(
            self.model_dump(include=set(AppRegistrationStatus.model_fields))
        )


class AppRegistrations:
    """The registrations in flight, and the manifest flow."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._registrations: dict[str, _Registration] = {}
        self._transport = transport

    async def start(
        self, request: AppRegistrationStart, team: Callable[[], Team]
    ) -> AppRegistrationStatus:
        name = request.app_name.strip()
        if not name or len(name) > _APP_NAME_MAX_LENGTH:
            raise SetupError(
                "invalid_github_app_name", "GitHub App name must be 1-34 characters."
            )
        self._purge_expired()
        base = request.callback_base_url.rstrip("/")
        registration = _Registration(
            state=secrets.token_urlsafe(32),
            app_name=name,
            person_id=request.person_id,
            organization=request.organization.strip(),
            callback_url=f"{base}{page_path(_NAME, 'app-callback')}",
        )
        registration.start_url = (
            f"{base}{page_path(_NAME, 'app-manifest')}?state={registration.state}"
        )
        self._registrations[registration.state] = registration
        return registration.status_view()

    async def status(
        self, request: AppRegistrationRef, team: Callable[[], Team]
    ) -> AppRegistrationStatus:
        """Detect the app's installation and capture its installation ID."""
        registration = self._get(request.state, status_code=404)
        if registration.status == "converted" and registration.app_id is not None:
            try:
                installations = await app_manifest.list_app_installations(
                    str(registration.app_id),
                    registration.pem.encode(),
                    transport=self._transport,
                )
            except (httpx.HTTPError, ValueError) as exc:
                # The screen polls this; a transient GitHub error must not
                # abort the flow, so it is shown on the registration.
                registration.installation_check_error = str(exc)
            else:
                registration.installation_check_error = ""
                if installations:
                    registration.installation_id = max(
                        installation.installation_id for installation in installations
                    )
                    registration.status = "installed"
        return registration.status_view()

    async def manifest_page(self, query: Mapping[str, str]) -> Page:
        """A page that posts the manifest form to github.com at once."""
        try:
            registration = self._get(query.get("state", ""))
        except SetupError as exc:
            return _error_page(exc.message)
        url = app_manifest.manifest_submission_url(registration.organization)
        manifest = app_manifest.build_app_manifest(
            registration.app_name, registration.callback_url
        )
        return html_page(
            f"<p>{html.escape(t('integrations.github.app_registration.redirecting'))}</p>"
            f'<form id="manifest-form" action="{html.escape(url)}'
            f'?state={html.escape(registration.state)}" method="post">'
            f'<input type="hidden" name="manifest" '
            f'value="{html.escape(json.dumps(manifest))}">'
            f'<button type="submit">{html.escape(t("integrations.github.app_registration.submit"))}</button>'
            "</form>"
            '<script>document.getElementById("manifest-form").submit();</script>'
        )

    async def callback_page(self, query: Mapping[str, str]) -> Page:
        """Convert the callback's one-time code into credentials, then send
        the browser on to install the app."""
        code = query.get("code", "")
        if not code:
            return _error_page(t("integrations.github.app_registration.missing_code"))
        try:
            registration = self._get(query.get("state", ""))
            if registration.status == "pending":
                # A reload replays the callback; the one-time code cannot be
                # converted twice, so the stored result is kept.
                conversion = await app_manifest.convert_manifest_code(
                    code, transport=self._transport
                )
                registration.slug = conversion.slug
                registration.app_id = conversion.app_id
                registration.html_url = conversion.html_url
                registration.pem = conversion.pem
                registration.username = f"{conversion.slug}[bot]"
                registration.git_email = await self._bot_email(conversion.slug)
                registration.status = "converted"
        except SetupError as exc:
            return _error_page(exc.message)
        except httpx.HTTPError as exc:
            logging.getLogger(__name__).exception(
                "GitHub App manifest conversion failed"
            )
            return _error_page(str(exc))
        return Page(redirect=registration.installation_page_url)

    def claim(self, state: str, person_id: str) -> _Registration:
        """The installed registration a save of ``person_id`` names.

        It stays held, so a save that fails after this can be retried with
        the same registration; it is discarded once the save succeeded.

        Raises:
            SetupError: The registration is missing, expired or already
                saved, is not installed yet, or was started for another member.
        """
        registration = self._get(state)
        if registration.status != "installed":
            raise SetupError(
                "github_app_registration_incomplete",
                "GitHub App registration has not been installed yet.",
            )
        if registration.person_id != person_id:
            raise SetupError(
                "github_app_registration_member_mismatch",
                "GitHub App registration was started for another member.",
            )
        return registration

    def discard(self, state: str) -> None:
        self._registrations.pop(state, None)

    def _get(self, state: str, *, status_code: int = 400) -> _Registration:
        self._purge_expired()
        registration = self._registrations.get(state)
        if registration is None:
            raise SetupError(
                "github_app_registration_not_found",
                "GitHub App registration was not found or has expired.",
                status_code=status_code,
            )
        return registration

    async def _bot_email(self, slug: str) -> str:
        # The bot user usually exists right after the app is created; if the
        # lookup fails the field stays empty and the screen's ordinary
        # resolve action fills it in later.
        try:
            return (await _user(slug, app=True)).git_email
        except SetupError:
            return ""

    def _purge_expired(self) -> None:
        deadline = time.time() - _REGISTRATION_TTL_SECONDS
        self._registrations = {
            state: registration
            for state, registration in self._registrations.items()
            if registration.created_at >= deadline
        }


def _error_page(message: str) -> Page:
    """A page telling the user to retry from the app."""
    return message_page(
        t("integrations.github.app_registration.error_title"),
        message,
        t("integrations.github.app_registration.error_hint"),
        status_code=400,
    )


_registrations = AppRegistrations()

SETUP = ProviderSetup(
    title="GitHub",
    services={
        Service.CODE_HOSTING_SERVICE: CodeHosting,
        Service.TICKET_SERVICE: Board,
    },
    member=GitHubAccount,
    avatar_url=_avatar_url,
    operations={
        "resolve-user": Operation(UserReference, ResolvedUser, _resolve_user),
        "status-options": Operation(BoardTarget, StatusOptions, _status_options),
        "agent-field": Operation(
            BoardTarget,
            AgentFieldState,
            _agent_field(lambda board: board.get_agent_field_state()),
        ),
        "agent-field/ensure": Operation(
            BoardTarget,
            AgentFieldState,
            _agent_field(lambda board: board.sync_agent_field()),
        ),
        "app-registrations": Operation(
            AppRegistrationStart, AppRegistrationStatus, _registrations.start
        ),
        "app-registration": Operation(
            AppRegistrationRef, AppRegistrationStatus, _registrations.status
        ),
    },
    pages={
        "app-manifest": _registrations.manifest_page,
        "app-callback": _registrations.callback_page,
    },
)
