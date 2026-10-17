"""GitHub's setup: the items of its services, a member's GitHub account, the
App registration round trip, and the operations the setup screens call."""

import html
import json
import re
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.github import setup as github_setup
from guildbotics.integrations.github.setup import (
    AccountView,
    AppRegistrationRef,
    AppRegistrations,
    AppRegistrationStart,
    Board,
    BoardTarget,
    CodeHosting,
    GitHubAccount,
    LaneMap,
    UserReference,
)
from guildbotics.integrations.provider import SetupError
from guildbotics.setup.setup_service import (
    PersonSetupInput,
    SimplePersonSetupService,
    settle_accounts,
)
from guildbotics.utils.fileio import load_yaml_file
from guildbotics.utils.person_id import IDENTITY_FILE, identity_members_dir
from guildbotics.utils.secret_store import identity_secret_store

BASE_URL = "http://127.0.0.1:8765"
APP_ID = 1978826
BOT_USER_ID = 233270845
LATEST_INSTALLATION = 86632391
INSTALLATION_PAGE = "https://github.com/apps/my-bot/installations/new"
BOT_EMAIL = f"{BOT_USER_ID}+my-bot[bot]@users.noreply.github.com"


@pytest.fixture(scope="module")
def pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


class GitHubApi:
    """api.github.com as the setup reaches it, with the requests it was sent."""

    def __init__(self, pem: str) -> None:
        self.pem = pem
        self.requests: list[httpx.Request] = []
        self.installations: Callable[[], httpx.Response] = lambda: httpx.Response(
            200,
            json=[
                {"id": 11, "account": {"login": "a"}},
                {"id": LATEST_INSTALLATION, "account": {"login": "b"}},
            ],
        )
        self.users: dict[str, dict] = {
            "my-bot[bot]": {"id": BOT_USER_ID},
            "alice": {"id": 7, "avatar_url": "https://avatars.example/alice"},
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/app-manifests/tmp-code/conversions":
            return httpx.Response(
                201,
                json={
                    "id": APP_ID,
                    "slug": "my-bot",
                    "html_url": "https://github.com/apps/my-bot",
                    "pem": self.pem,
                },
            )
        if path == "/app/installations":
            return self.installations()
        if path.startswith("/users/"):
            user = self.users.get(path.removeprefix("/users/"))
            return httpx.Response(200, json=user) if user else httpx.Response(404)
        return httpx.Response(404)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


@pytest.fixture
def api(pem: str, monkeypatch: pytest.MonkeyPatch) -> GitHubApi:
    """Route every client the setup opens, including the user lookups that
    take no transport, to the fake API."""
    fake = GitHubApi(pem)
    transport = httpx.MockTransport(fake)
    real_client = httpx.AsyncClient

    class Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs) -> None:
            kwargs["transport"] = kwargs.get("transport") or transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(github_setup.httpx, "AsyncClient", Client)
    return fake


@pytest.fixture
def registrations(api: GitHubApi, monkeypatch: pytest.MonkeyPatch) -> AppRegistrations:
    """The registrations a member save claims from."""
    held = AppRegistrations(transport=httpx.MockTransport(api))
    monkeypatch.setattr(github_setup, "_registrations", held)
    return held


def _no_team() -> Team:
    raise AssertionError("the operation does not read the saved team")


async def _start(registrations: AppRegistrations, organization: str = ""):
    return await registrations.start(
        AppRegistrationStart(
            app_name="  my-bot  ",
            person_id="alice",
            organization=organization,
            callback_base_url=f"{BASE_URL}/",
        ),
        _no_team,
    )


async def _installed(registrations: AppRegistrations) -> str:
    started = await _start(registrations)
    page = await registrations.callback_page(
        {"code": "tmp-code", "state": started.state}
    )
    assert page.redirect == INSTALLATION_PAGE
    status = await registrations.status(
        AppRegistrationRef(state=started.state), _no_team
    )
    assert status.status == "installed"
    return started.state


# --------------------------------------------------------------------------- #
# the items of the services
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url, owner, number",
    [
        ("https://github.com/orgs/GuildBotics/projects/1", "GuildBotics", "1"),
        ("https://github.com/users/alice/projects/42/", "alice", "42"),
    ],
)
def test_a_board_is_named_by_its_url(url: str, owner: str, number: str) -> None:
    board = Board(url=f"  {url}  ")

    assert board.model_dump(exclude_none=True) == {
        "url": url,
        "owner": owner,
        "project_id": number,
        "lane_map": {"ready": "Todo", "working": "In Progress", "done": "Done"},
    }


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://github.com/GuildBotics/GuildBotics",
        "https://github.com/orgs/GuildBotics/projects/x",
        "https://example.com/orgs/GuildBotics/projects/1",
    ],
)
def test_a_board_url_must_name_a_project(url: str) -> None:
    with pytest.raises(ValidationError):
        Board(url=url)


def test_a_board_derives_owner_and_number_over_what_it_is_given() -> None:
    board = Board(
        url="https://github.com/orgs/GuildBotics/projects/1",
        owner="someone-else",
        project_id="9",
    )

    assert (board.owner, board.project_id) == ("GuildBotics", "1")


def test_blank_lanes_are_the_boards_defaults() -> None:
    assert LaneMap(ready=" ", working="", done="Shipped").model_dump() == {
        "ready": "Todo",
        "working": "In Progress",
        "done": "Shipped",
    }


@pytest.mark.parametrize(
    "lanes",
    [{"ready": "Same", "done": "Same"}, {"ready": "Done"}, {"lane": "x"}],
)
def test_lanes_that_cannot_move_a_ticket_are_refused(lanes: dict) -> None:
    with pytest.raises(ValidationError):
        LaneMap(**lanes)


def test_code_hosting_names_the_owner_it_writes_to() -> None:
    assert CodeHosting(owner="GuildBotics").model_dump(exclude_none=True) == {
        "owner": "GuildBotics"
    }
    for items in (
        {},
        {"owner": ""},
        {"owner": "  "},
        {"owner": "a", "repo_base_url": "b"},
    ):
        with pytest.raises(ValidationError):
            CodeHosting(**items)


# --------------------------------------------------------------------------- #
# a member's account
# --------------------------------------------------------------------------- #


def test_a_human_stores_only_a_username() -> None:
    account = GitHubAccount(
        account_type="machine_user", username="alice", access_token="ghp-x", app_id=7
    )

    assert account.stored("alice", True) == (
        {"github_account_type": "human", "github_username": "alice"},
        {},
    )


def test_an_agent_stores_the_values_it_names_and_its_token() -> None:
    account = GitHubAccount(
        account_type="machine_user", username="alice-bot", access_token="ghp-x"
    )

    assert account.stored("alice", False) == (
        {"github_account_type": "machine_user", "github_username": "alice-bot"},
        {"GITHUB_ACCESS_TOKEN": "ghp-x"},
    )
    assert GitHubAccount().stored("alice", False) == ({}, {})


def test_an_app_stores_its_ids_and_the_key_files_content(tmp_path: Path) -> None:
    key_file = tmp_path / "app.pem"
    key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nkey\n", encoding="utf-8")
    account = GitHubAccount(
        account_type="github_apps",
        username="my-bot[bot]",
        app_id=7,
        installation_id=42,
        private_key_path=key_file,
    )

    assert account.stored("alice", False) == (
        {
            "github_account_type": "github_apps",
            "github_username": "my-bot[bot]",
            "github_app_id": "7",
            "github_installation_id": "42",
        },
        {"GITHUB_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----\nkey\n"},
    )


@pytest.mark.parametrize("content", [None, b"\xff\xfe"])
def test_an_unreadable_key_file_is_refused(tmp_path: Path, content) -> None:
    key_file = tmp_path / "app.pem"
    if content is not None:
        key_file.write_bytes(content)

    with pytest.raises(SetupError) as error:
        GitHubAccount(account_type="github_apps", private_key_path=key_file).stored(
            "alice", False
        )

    assert error.value.code == "github_private_key_unreadable"


def test_the_view_shows_the_stored_values_and_which_secrets_are_held() -> None:
    held = {"GITHUB_PRIVATE_KEY"}

    view = GitHubAccount.view(
        {
            "github_account_type": "github_apps",
            "github_username": "my-bot[bot]",
            "github_app_id": "7",
            "github_installation_id": "42",
            "git_email": "ignored@example.com",
        },
        held.__contains__,
    )

    assert view == AccountView(
        account_type="github_apps",
        username="my-bot[bot]",
        installation_id=42,
        app_id=7,
        has_private_key=True,
        has_access_token=False,
    )


def test_the_view_shows_ids_that_are_not_numbers_as_unset() -> None:
    view = GitHubAccount.view(
        {"github_app_id": "abc", "github_installation_id": ""}, lambda name: False
    )

    assert (view.app_id, view.installation_id, view.account_type) == (None, None, "")


# --------------------------------------------------------------------------- #
# the App registration
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["   ", "x" * 35])
async def test_start_refuses_an_invalid_app_name(name: str) -> None:
    with pytest.raises(SetupError) as error:
        await AppRegistrations().start(
            AppRegistrationStart(
                app_name=name, person_id="alice", callback_base_url=BASE_URL
            ),
            _no_team,
        )

    assert error.value.code == "invalid_github_app_name"


@pytest.mark.asyncio
async def test_start_sends_the_browser_to_the_manifest_page() -> None:
    started = await _start(AppRegistrations())

    assert started.status == "pending"
    assert started.app_name == "my-bot"
    assert started.person_id == "alice"
    assert started.start_url == (
        f"{BASE_URL}/providers/github/pages/app-manifest?state={started.state}"
    )
    assert started.installation_page_url == ""


@pytest.mark.asyncio
async def test_the_manifest_page_posts_the_manifest_to_github() -> None:
    registrations = AppRegistrations()
    started = await _start(registrations, organization="acme")

    page = await registrations.manifest_page({"state": started.state})

    assert page.status_code == 200
    action = re.search(r'action="([^"]+)"', page.html)
    assert action is not None
    assert html.unescape(action.group(1)) == (
        f"https://github.com/organizations/acme/settings/apps/new?state={started.state}"
    )
    value = re.search(r'name="manifest" value="([^"]+)"', page.html)
    assert value is not None
    manifest = json.loads(html.unescape(value.group(1)))
    assert manifest["name"] == "my-bot"
    assert manifest["redirect_url"] == f"{BASE_URL}/providers/github/pages/app-callback"


@pytest.mark.asyncio
async def test_pages_of_an_unknown_registration_say_so() -> None:
    registrations = AppRegistrations()

    manifest = await registrations.manifest_page({"state": "missing"})
    callback = await registrations.callback_page({"code": "c", "state": "missing"})
    no_code = await registrations.callback_page({"state": "missing"})

    for page in (manifest, callback, no_code):
        assert page.status_code == 400
        assert page.redirect == ""
    assert "GitHub App registration was not found or has expired." in manifest.html


@pytest.mark.asyncio
async def test_status_of_an_unknown_registration_is_not_found() -> None:
    with pytest.raises(SetupError) as error:
        await AppRegistrations().status(AppRegistrationRef(state="missing"), _no_team)

    assert error.value.code == "github_app_registration_not_found"
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_the_callback_converts_the_code_once_and_keeps_the_key_private(
    registrations: AppRegistrations, api: GitHubApi
) -> None:
    started = await _start(registrations)

    page = await registrations.callback_page(
        {"code": "tmp-code", "state": started.state}
    )
    # A reload replays the callback with a code GitHub no longer accepts.
    again = await registrations.callback_page(
        {"code": "tmp-code", "state": started.state}
    )

    assert page.redirect == again.redirect == INSTALLATION_PAGE
    assert api.paths().count("/app-manifests/tmp-code/conversions") == 1
    status = await registrations.status(
        AppRegistrationRef(state=started.state), _no_team
    )
    assert status.app_id == APP_ID
    assert status.slug == "my-bot"
    assert status.username == "my-bot[bot]"
    assert status.git_email == BOT_EMAIL
    assert api.pem not in status.model_dump_json()


@pytest.mark.asyncio
async def test_the_callback_leaves_the_email_blank_when_the_bot_is_not_found(
    registrations: AppRegistrations, api: GitHubApi
) -> None:
    del api.users["my-bot[bot]"]
    started = await _start(registrations)

    await registrations.callback_page({"code": "tmp-code", "state": started.state})

    status = await registrations.status(
        AppRegistrationRef(state=started.state), _no_team
    )
    assert status.username == "my-bot[bot]"
    assert status.git_email == ""


@pytest.mark.asyncio
async def test_a_failed_conversion_is_shown_on_the_page(
    registrations: AppRegistrations,
) -> None:
    started = await _start(registrations)

    page = await registrations.callback_page({"code": "bad", "state": started.state})

    assert page.status_code == 400
    assert page.redirect == ""


@pytest.mark.asyncio
async def test_status_finds_the_latest_installation(
    registrations: AppRegistrations,
) -> None:
    state = await _installed(registrations)

    status = await registrations.status(AppRegistrationRef(state=state), _no_team)

    assert status.status == "installed"
    assert status.installation_id == LATEST_INSTALLATION
    assert status.installation_page_url == INSTALLATION_PAGE


@pytest.mark.asyncio
async def test_status_waits_for_the_conversion(
    registrations: AppRegistrations, api: GitHubApi
) -> None:
    started = await _start(registrations)

    status = await registrations.status(
        AppRegistrationRef(state=started.state), _no_team
    )

    assert status.status == "pending"
    assert status.installation_id is None
    assert api.requests == []


@pytest.mark.asyncio
async def test_status_shows_a_transient_error_and_keeps_polling(
    registrations: AppRegistrations, api: GitHubApi
) -> None:
    started = await _start(registrations)
    await registrations.callback_page({"code": "tmp-code", "state": started.state})
    api.installations = lambda: httpx.Response(502, text="bad gateway")

    failed = await registrations.status(
        AppRegistrationRef(state=started.state), _no_team
    )
    api.installations = lambda: httpx.Response(200, json=[{"id": 5}])
    recovered = await registrations.status(
        AppRegistrationRef(state=started.state), _no_team
    )

    assert failed.status == "converted"
    assert "502" in failed.installation_check_error
    assert recovered.status == "installed"
    assert recovered.installation_check_error == ""
    assert recovered.installation_id == 5


# --------------------------------------------------------------------------- #
# a save claims the registration, and discards it once saved
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_save_stores_the_registrations_ids_and_key_until_saved(
    registrations: AppRegistrations, api: GitHubApi
) -> None:
    state = await _installed(registrations)
    account = GitHubAccount(
        account_type="github_apps",
        username="my-bot[bot]",
        app_id=1,
        installation_id=2,
        app_registration_id=state,
    )

    stored = account.stored("alice", False)
    await account.settled(False)
    # A save refused after the claim retries with the same registration.
    assert account.stored("alice", False) == stored
    assert stored == (
        {
            "github_account_type": "github_apps",
            "github_username": "my-bot[bot]",
            "github_app_id": str(APP_ID),
            "github_installation_id": str(LATEST_INSTALLATION),
        },
        {"GITHUB_PRIVATE_KEY": api.pem},
    )

    await account.settled(True)

    with pytest.raises(SetupError) as error:
        account.stored("alice", False)
    assert error.value.code == "github_app_registration_not_found"


@pytest.mark.asyncio
async def test_a_member_save_keeps_the_key_only_in_the_secret_store(
    registrations: AppRegistrations, api: GitHubApi, tmp_path: Path
) -> None:
    config_dir = tmp_path / ".guildbotics/config"
    state = await _installed(registrations)

    member = PersonSetupInput(
        config_dir=config_dir,
        person_type="machine_user",
        person_id="alice",
        person_name="Alice",
        is_active=True,
        accounts={
            "github": {
                "account_type": "github_apps",
                "username": "my-bot[bot]",
                "app_registration_id": state,
            }
        },
    )

    SimplePersonSetupService().save_person(member)
    await settle_accounts(member, saved=True)

    person_file = identity_members_dir() / "alice" / IDENTITY_FILE
    account_info = load_yaml_file(person_file)["account_info"]
    assert account_info["github_app_id"] == str(APP_ID)
    assert account_info["github_installation_id"] == str(LATEST_INSTALLATION)
    assert api.pem not in person_file.read_text(encoding="utf-8")
    assert identity_secret_store().get("ALICE_GITHUB_PRIVATE_KEY") == api.pem
    with pytest.raises(SetupError):
        await registrations.status(AppRegistrationRef(state=state), _no_team)


@pytest.mark.asyncio
async def test_a_save_refuses_a_registration_that_is_not_installed(
    registrations: AppRegistrations,
) -> None:
    started = await _start(registrations)
    account = GitHubAccount(app_registration_id=started.state)

    with pytest.raises(SetupError) as pending:
        account.stored("alice", False)
    await registrations.callback_page({"code": "tmp-code", "state": started.state})
    with pytest.raises(SetupError) as converted:
        account.stored("alice", False)

    assert (
        pending.value.code
        == converted.value.code
        == ("github_app_registration_incomplete")
    )


@pytest.mark.asyncio
async def test_a_save_refuses_a_registration_started_for_another_member(
    registrations: AppRegistrations,
) -> None:
    state = await _installed(registrations)

    with pytest.raises(SetupError) as error:
        GitHubAccount(app_registration_id=state).stored("bob", False)

    assert error.value.code == "github_app_registration_member_mismatch"


@pytest.mark.asyncio
async def test_a_save_refuses_an_expired_registration(
    registrations: AppRegistrations, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = await _installed(registrations)
    later = time.time() + 30 * 60 + 1
    monkeypatch.setattr(github_setup.time, "time", lambda: later)

    with pytest.raises(SetupError) as error:
        GitHubAccount(app_registration_id=state).stored("alice", False)

    assert error.value.code == "github_app_registration_not_found"


# --------------------------------------------------------------------------- #
# operations: user resolution and the board
# --------------------------------------------------------------------------- #


def _operation(name: str):
    return github_setup.SETUP.operations[name].run


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account_type, identity, person_id, username, user_id",
    [
        ("machine_user", "alice@example.com", "alice", "alice", 7),
        ("human", "alice", "alice", "alice", 7),
        (
            "github_apps",
            "https://github.com/organizations/acme/settings/apps/my-bot",
            "my-bot",
            "my-bot[bot]",
            BOT_USER_ID,
        ),
    ],
)
async def test_resolve_user_names_the_github_user(
    api: GitHubApi,
    account_type: str,
    identity: str,
    person_id: str,
    username: str,
    user_id: int,
) -> None:
    resolved = await _operation("resolve-user")(
        UserReference(account_type=account_type, identity=identity), _no_team
    )

    assert resolved.model_dump() == {
        "person_id": person_id,
        "username": username,
        "user_id": user_id,
        "git_email": f"{user_id}+{username}@users.noreply.github.com",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account_type, identity, code",
    [
        ("github_apps", "https://github.com/apps/my-bot", "invalid_github_apps_url"),
        ("machine_user", "nobody", "invalid_github_username"),
    ],
)
async def test_resolve_user_refuses_what_names_no_user(
    api: GitHubApi, account_type: str, identity: str, code: str
) -> None:
    with pytest.raises(SetupError) as error:
        await _operation("resolve-user")(
            UserReference(account_type=account_type, identity=identity), _no_team
        )

    assert error.value.code == code


def _team(*members: Person) -> Team:
    return Team(project=Project(name="test"), members=list(members))


def _unreadable_team() -> Team:
    raise OSError("the saved team cannot be read")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["status-options", "agent-field"])
@pytest.mark.parametrize(
    "url, team",
    [
        pytest.param("https://github.com/acme", _no_team, id="not-a-board"),
        pytest.param(
            "https://github.com/orgs/acme/projects/1",
            _unreadable_team,
            id="no-saved-team",
        ),
        pytest.param(
            "https://github.com/orgs/acme/projects/1",
            lambda: _team(Person(person_id="alice", name="Alice", is_active=True)),
            id="no-member-reaches-it",
        ),
    ],
)
async def test_a_board_that_cannot_be_read_is_unavailable(
    api: GitHubApi, operation: str, url: str, team: Callable[[], Team]
) -> None:
    result = await _operation(operation)(BoardTarget(url=url), team)

    assert result.available is False
    assert api.requests == []


# --------------------------------------------------------------------------- #
# the avatar
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_avatar_is_the_github_users(api: GitHubApi) -> None:
    person = Person(
        person_id="alice", name="Alice", account_info={"github_username": "alice"}
    )

    assert (
        await github_setup.SETUP.avatar_url(person, _team(person))
        == "https://avatars.example/alice"
    )


@pytest.mark.asyncio
async def test_the_avatar_needs_a_github_username(api: GitHubApi) -> None:
    person = Person(person_id="alice", name="Alice")

    with pytest.raises(SetupError) as error:
        await github_setup.SETUP.avatar_url(person, _team(person))

    assert error.value.code == "avatar_account_missing"
    assert error.value.localized("ja") == (
        "このメンバーには GitHub ユーザー名が設定されていません。"
    )
    assert api.requests == []
