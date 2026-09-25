"""A brokered login: sealed on the device, lent to a turn as a stand-in,
refreshed and asked about only where it is held in memory."""

from __future__ import annotations

import ast
import asyncio
import base64
import datetime as dt
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from guildbotics.intelligences.agent_environment import (
    credential_vault,
    provider_state,
)
from guildbotics.intelligences.agent_environment.auth_gateway import (
    CredentialUnavailableError,
)
from guildbotics.intelligences.agent_environment.credential_vault import (
    CredentialVaultError,
)
from guildbotics.intelligences.agent_environment.provider_state import (
    LentLogin,
    LoginEnvironment,
    credential_state,
    login,
    provider_state_dir,
    refresh_login,
    sealed_login_path,
    start_login_environment,
)
from guildbotics.intelligences.agent_environment.runtime import AgentEnvironmentError
from guildbotics.intelligences.agent_environment.toolchain import parse_toolchain
from guildbotics.intelligences.cli_agents import CliAgentInfo, cli_agent_info
from guildbotics.utils.i18n_tool import t

CLAUDE = cli_agent_info("claude")
AUTH = CLAUDE.provision.auth
WHERE = LoginEnvironment(Path("/snap"), 1024, 1, ("10.0.0.53",))
DECLARATION = parse_toolchain({"dns": {"nameservers": ["10.0.0.53"]}}, where="t")


def _login(
    token: str = "REAL-459", refresh: str = "REFRESH-459", expires_in: float = 3600
) -> bytes:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": token,
                "refreshToken": refresh,
                "expiresAt": int((time.time() + expires_in) * 1000),
                "scopes": ["user:inference", "user:profile"],
                "subscriptionType": "max",
                # A credential the file gains in some later version.
                "idToken": "ID-SECRET-459",
            },
            "mcpOAuth": {"server": {"accessToken": "MCP-SECRET-459"}},
        }
    ).encode()


def _oauth(data: bytes) -> dict[str, Any]:
    return json.loads(data)["claudeAiOauth"]


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for module in (provider_state, credential_vault):
        monkeypatch.setattr(
            module,
            "get_machine_state_path",
            lambda *parts: tmp_path.joinpath("data", *parts),
        )
    _Environment.reset()
    monkeypatch.setattr(provider_state, "AgentEnvironment", _Environment)
    return tmp_path


class _Process:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.stdin = self

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""

    async def wait(self) -> int:
        return self.returncode

    async def kill(self) -> None:
        pass


class _Environment:
    """A microVM whose guest files are a dict; ``act`` is what the tool does
    to them when it runs."""

    instances: list[_Environment] = []
    act: Callable[[dict[str, bytes], str], None] = staticmethod(
        lambda files, root: None
    )

    def __init__(self, spec: Any, before_stop: Any, on_close: Any) -> None:
        self.spec = spec
        self.files: dict[str, bytes] = {}
        self.commands: list[tuple[str, ...]] = []
        self.closed = False
        self._before_stop = before_stop
        self._on_close = on_close

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.act = staticmethod(lambda files, root: None)

    @classmethod
    async def start(
        cls,
        spec: Any,
        *,
        snapshot: str,
        memory_mib: int,
        cpus: int,
        before_stop: Any = None,
        on_close: Any = None,
    ) -> _Environment:
        environment = cls(spec, before_stop, on_close)
        cls.instances.append(environment)
        return environment

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data

    async def read_file(self, path: str) -> bytes | None:
        return self.files.get(path)

    async def run(self, *command: str, limit: int, tty: bool = False) -> _Process:
        self.commands.append(command)
        # The tool's state root: the one thing the login's environment mounts.
        _Environment.act(self.files, self.spec.mounts[0].guest)
        return _Process()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self._before_stop is not None:
                await self._before_stop(self)
        finally:
            if self._on_close is not None:
                self._on_close()


def _refreshes_to(token: str) -> Callable[[dict[str, bytes], str], None]:
    def act(files: dict[str, bytes], root: str) -> None:
        files[f"{root}/{AUTH}"] = _login(token, refresh=f"{token}-REFRESH")

    return act


def _sealed() -> dict[str, Any]:
    return _oauth(provider_state._unsealed_login(CLAUDE)[AUTH])


# --- the state ------------------------------------------------------------------


def test_a_brokered_login_is_saved_only_when_it_is_sealed(machine: Path) -> None:
    """A plain credentials file an earlier GuildBotics kept is not read."""
    assert credential_state(CLAUDE) == "missing"
    legacy = provider_state_dir(CLAUDE) / AUTH
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(_login())
    assert credential_state(CLAUDE) == "missing"

    provider_state._seal_login(CLAUDE, {AUTH: _login()})

    assert credential_state(CLAUDE) == "saved"
    assert b"REAL-459" not in sealed_login_path(CLAUDE).read_bytes()


def test_a_brokered_login_that_does_not_open_is_reported(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_state._seal_login(CLAUDE, {AUTH: _login()})
    sealed_login_path(CLAUDE).write_text("{}")

    assert credential_state(CLAUDE) == "corrupt"


# --- lending ----------------------------------------------------------------------


def test_a_turn_holds_a_stand_in_that_can_neither_refresh_nor_expire(
    machine: Path,
) -> None:
    provider_state._seal_login(CLAUDE, {AUTH: _login()})
    lent = LentLogin(CLAUDE, WHERE)

    ((name, data),) = lent.stand_in_files().items()

    held = _oauth(data)
    assert name == AUTH
    assert held["accessToken"] == lent.stand_in
    assert lent.stand_in.startswith("guildbotics-stand-in-")
    assert lent.stand_in != LentLogin(CLAUDE, WHERE).stand_in  # The turn's own.
    assert "refreshToken" not in held
    assert held["expiresAt"] / 1000 > time.time() + 300 * 24 * 60 * 60
    assert held["scopes"] == ["user:inference", "user:profile"]
    assert held["subscriptionType"] == "max"
    # Built from the named fields: no credential the file holds, known or
    # not, reaches the turn.
    assert json.loads(data) == {"claudeAiOauth": held}
    assert set(held) == {"accessToken", "expiresAt", "scopes", "subscriptionType"}
    for secret in (b"REAL-459", b"REFRESH-459", b"ID-SECRET-459", b"MCP-SECRET-459"):
        assert secret not in data


def test_a_login_that_cannot_be_lent_says_why(machine: Path) -> None:
    with pytest.raises(CredentialVaultError) as missing:
        LentLogin(CLAUDE, WHERE)
    assert missing.value.state == "missing"


@pytest.mark.asyncio
async def test_a_fresh_token_is_sent_as_it_is(machine: Path) -> None:
    provider_state._seal_login(CLAUDE, {AUTH: _login()})
    lent = LentLogin(CLAUDE, WHERE)

    assert await lent.access_token(None) == "REAL-459"
    assert await lent.access_token("OLDER-459") == "REAL-459"
    assert _Environment.instances == []


@pytest.mark.asyncio
async def test_a_token_about_to_expire_is_refreshed_where_the_login_is(
    machine: Path,
) -> None:
    provider_state._seal_login(CLAUDE, {AUTH: _login(expires_in=60)})
    _Environment.act = staticmethod(_refreshes_to("NEW-459"))
    lent = LentLogin(CLAUDE, WHERE)

    assert await lent.access_token(None) == "NEW-459"

    (environment,) = _Environment.instances
    root = f"{environment.spec.home}/.claude"
    # The login's own environment: its state root in memory, nothing of the
    # device, the provider's domains only.
    assert [(m.guest, m.host, m.readonly) for m in environment.spec.mounts] == [
        (root, None, False)
    ]
    assert not environment.spec.network.unrestricted
    assert environment.spec.network.domains == CLAUDE.provision.api_domains
    assert environment.spec.network.host_ports == ()
    assert environment.commands == [CLAUDE.provision.credential_broker.refresh]
    assert environment.closed
    assert _sealed()["accessToken"] == "NEW-459"
    assert _sealed()["refreshToken"] == "NEW-459-REFRESH"
    assert await lent.access_token(None) == "NEW-459"
    assert len(_Environment.instances) == 1


@pytest.mark.asyncio
async def test_the_refresh_is_told_the_login_expired(machine: Path) -> None:
    provider_state._seal_login(CLAUDE, {AUTH: _login()})
    given: list[dict[str, Any]] = []

    def act(files: dict[str, bytes], root: str) -> None:
        given.append(_oauth(files[f"{root}/{AUTH}"]))
        _refreshes_to("NEW-459")(files, root)

    _Environment.act = staticmethod(act)

    await refresh_login(CLAUDE, WHERE, "REAL-459")

    ((held),) = given
    assert (held["accessToken"], held["refreshToken"], held["expiresAt"]) == (
        "REAL-459",
        "REFRESH-459",
        0,
    )


@pytest.mark.asyncio
async def test_a_login_refreshed_by_another_turn_is_taken_as_it_is(
    machine: Path,
) -> None:
    provider_state._seal_login(CLAUDE, {AUTH: _login()})
    lent = LentLogin(CLAUDE, WHERE)
    provider_state._seal_login(CLAUDE, {AUTH: _login("OTHER-459")})

    assert await lent.access_token("REAL-459") == "OTHER-459"
    assert _Environment.instances == []


@pytest.mark.asyncio
async def test_turns_refreshing_at_once_refresh_once(machine: Path) -> None:
    provider_state._seal_login(CLAUDE, {AUTH: _login(expires_in=60)})
    _Environment.act = staticmethod(_refreshes_to("NEW-459"))
    turns = [LentLogin(CLAUDE, WHERE) for _ in range(3)]

    tokens = await asyncio.gather(*(turn.access_token(None) for turn in turns))

    assert tokens == ["NEW-459"] * 3
    assert len(_Environment.instances) == 1


@pytest.mark.asyncio
async def test_a_refresh_the_tool_did_not_make_keeps_the_login_and_says_why(
    machine: Path,
) -> None:
    provider_state._seal_login(CLAUDE, {AUTH: _login()})

    with pytest.raises(CredentialUnavailableError) as refused:
        await refresh_login(CLAUDE, WHERE, "REAL-459")

    assert "login claude" in str(refused.value)
    assert _sealed()["accessToken"] == "REAL-459"
    assert _Environment.instances[0].closed
    assert not provider_state.authentication_failed(CLAUDE)


@pytest.mark.asyncio
async def test_a_refresh_that_cannot_be_sealed_is_a_failed_login(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool spent the refresh token it replaced: the old login is not
    kept as if it still worked."""
    provider_state._seal_login(CLAUDE, {AUTH: _login()})
    _Environment.act = staticmethod(_refreshes_to("NEW-459"))

    def full(tool: Any, files: Any) -> None:
        raise CredentialVaultError("unavailable", "No space left on device")

    monkeypatch.setattr(provider_state, "_seal_login", full)

    with pytest.raises(CredentialUnavailableError):
        await refresh_login(CLAUDE, WHERE, "REAL-459")

    assert provider_state.authentication_failed(CLAUDE)


@pytest.mark.asyncio
async def test_a_refresh_waits_for_whoever_holds_the_login(machine: Path) -> None:
    provider_state._seal_login(CLAUDE, {AUTH: _login()})
    _Environment.act = staticmethod(_refreshes_to("NEW-459"))
    holding = await start_login_environment(CLAUDE, WHERE)

    refreshing = asyncio.create_task(refresh_login(CLAUDE, WHERE, "REAL-459"))
    await asyncio.sleep(0.3)
    assert not refreshing.done()

    await holding.close()
    files = await refreshing

    assert _oauth(files[AUTH])["accessToken"] == "NEW-459"


@pytest.mark.asyncio
async def test_a_refresh_cancelled_after_the_tool_refreshed_still_keeps_it(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh token the tool replaced is spent: a turn that ends while
    the refresh is still running must not leave the old one sealed."""
    provider_state._seal_login(CLAUDE, {AUTH: _login()})
    _Environment.act = staticmethod(_refreshes_to("NEW-459"))
    ran = asyncio.Event()

    async def communicate(self: _Process) -> tuple[bytes, bytes]:
        ran.set()
        await asyncio.Event().wait()
        return b"", b""

    monkeypatch.setattr(_Process, "communicate", communicate)
    refreshing = asyncio.create_task(refresh_login(CLAUDE, WHERE, "REAL-459"))
    await ran.wait()

    refreshing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await refreshing

    assert _sealed()["refreshToken"] == "NEW-459-REFRESH"
    assert _Environment.instances[0].closed


@pytest.mark.asyncio
async def test_a_login_that_cannot_be_read_back_after_the_refresh_is_failed(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_state._seal_login(CLAUDE, {AUTH: _login()})

    async def gone(self: _Environment, path: str) -> bytes | None:
        raise AgentEnvironmentError("agent unreachable")

    monkeypatch.setattr(_Environment, "read_file", gone)

    with pytest.raises(CredentialUnavailableError):
        await refresh_login(CLAUDE, WHERE, "REAL-459")

    assert provider_state.authentication_failed(CLAUDE)


@pytest.mark.asyncio
async def test_a_turn_does_not_refresh_again_after_a_refresh_did_not_help(
    machine: Path,
) -> None:
    """One refresh environment per turn at most: a login the provider still
    refuses, or one the tool did not refresh, is not tried again."""
    provider_state._seal_login(CLAUDE, {AUTH: _login()})
    _Environment.act = staticmethod(_refreshes_to("NEW-459"))
    refused_again = LentLogin(CLAUDE, WHERE)

    assert await refused_again.access_token("REAL-459") == "NEW-459"
    for _ in range(2):
        with pytest.raises(CredentialUnavailableError):
            await refused_again.access_token("NEW-459")
    assert len(_Environment.instances) == 1

    _Environment.reset()
    provider_state._seal_login(CLAUDE, {AUTH: _login()})
    unrefreshed = LentLogin(CLAUDE, WHERE)
    for refused in ("REAL-459", "REAL-459", None):
        with pytest.raises(CredentialUnavailableError):
            await unrefreshed.access_token(refused)
    assert len(_Environment.instances) == 1


@pytest.mark.asyncio
async def test_a_refresh_that_cannot_hold_the_login_says_why(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway answers a login it cannot use; anything else would be a
    server error with no way back offered."""
    provider_state._seal_login(CLAUDE, {AUTH: _login()})
    monkeypatch.setattr(provider_state, "_LOGIN_WAIT_SECONDS", 0.2)
    holding = await start_login_environment(CLAUDE, WHERE)
    try:
        with pytest.raises(CredentialUnavailableError) as refused:
            await refresh_login(CLAUDE, WHERE, "REAL-459")
    finally:
        await holding.close()

    assert str(refused.value) == t(
        "intelligences.agent_environment.tool.credentials_unavailable",
        tool=CLAUDE.label,
        command=provider_state.login_command("claude"),
    )
    assert len(_Environment.instances) == 1


@pytest.mark.asyncio
async def test_a_login_made_while_a_refresh_runs_is_the_one_kept(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh still running on the replaced login seals first, and the
    new login after it -- never the other way round."""
    provider_state._seal_login(CLAUDE, {AUTH: _login("OLD-459")})

    def act(files: dict[str, bytes], root: str) -> None:
        if f"{root}/{AUTH}" in files:  # The refresh, holding the old login.
            files[f"{root}/{AUTH}"] = _login("OLD-REFRESHED-459")
        else:  # The login, starting from nothing.
            files[f"{root}/{AUTH}"] = _login("RELOGIN-459")

    _Environment.act = staticmethod(act)
    refreshing_ran = asyncio.Event()
    finish_refresh = asyncio.Event()

    async def communicate(self: _Process) -> tuple[bytes, bytes]:
        refreshing_ran.set()
        await finish_refresh.wait()
        return b"", b""

    monkeypatch.setattr(_Process, "communicate", communicate)
    refreshing = asyncio.create_task(refresh_login(CLAUDE, WHERE, "OLD-459"))
    await refreshing_ran.wait()
    logging_in = asyncio.create_task(
        login(
            CLAUDE,
            DECLARATION,
            snapshot=machine / "snap",
            read_line=lambda: None,
            write=lambda _: None,
        )
    )
    await asyncio.sleep(0.3)
    assert not logging_in.done()

    finish_refresh.set()
    await refreshing
    assert await logging_in == 0

    assert _sealed()["accessToken"] == "RELOGIN-459"


# --- asking the tool about its account ---------------------------------------------


@pytest.mark.asyncio
async def test_a_probe_holds_the_login_in_memory_and_seals_what_it_refreshed(
    machine: Path,
) -> None:
    provider_state._seal_login(CLAUDE, {AUTH: _login()})
    _Environment.act = staticmethod(_refreshes_to("NEW-459"))

    environment = await start_login_environment(CLAUDE, WHERE)
    root = f"{environment.spec.home}/.claude"
    assert _oauth(environment.files[f"{root}/{AUTH}"])["accessToken"] == "REAL-459"
    await environment.run("claude", "-p", "/usage", limit=1)
    assert _sealed()["accessToken"] == "REAL-459"  # not before it is taken back

    await environment.close()

    assert _sealed()["accessToken"] == "NEW-459"


@pytest.mark.asyncio
async def test_a_probe_that_changed_nothing_seals_nothing(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_state._seal_login(CLAUDE, {AUTH: _login()})
    sealed: list[Any] = []
    monkeypatch.setattr(provider_state, "_seal_login", lambda *a: sealed.append(a))

    environment = await start_login_environment(CLAUDE, WHERE)
    await environment.close()

    assert sealed == []


@pytest.mark.asyncio
async def test_a_probe_without_a_login_says_why(machine: Path) -> None:
    with pytest.raises(CredentialUnavailableError) as refused:
        await start_login_environment(CLAUDE, WHERE)

    assert str(refused.value) == t(
        "intelligences.agent_environment.tool.credentials_missing",
        tool=CLAUDE.label,
        command=provider_state.login_command("claude"),
    )
    # The login is not left held.
    provider_state._seal_login(CLAUDE, {AUTH: _login()})
    await (await start_login_environment(CLAUDE, WHERE)).close()


# --- logging in -------------------------------------------------------------------


def _log_in(machine: Path, act: Callable[[dict[str, bytes], str], None]) -> int:
    _Environment.act = staticmethod(act)
    return asyncio.run(
        login(
            CLAUDE,
            DECLARATION,
            snapshot=machine / "snap",
            read_line=lambda: None,
            write=lambda _: None,
        )
    )


def test_a_brokered_login_is_sealed_and_only_its_account_file_kept(
    machine: Path,
) -> None:
    store = provider_state_dir(CLAUDE)
    provider_state.record_authentication_outcome(CLAUDE, failed=True)

    def logs_in(files: dict[str, bytes], root: str) -> None:
        files[f"{root}/{AUTH}"] = _login()
        files[f"{root}/.claude.json"] = b'{"oauthAccount": {"emailAddress": "a@b"}}'

    assert _log_in(machine, logs_in) == 0

    assert _sealed()["accessToken"] == "REAL-459"
    assert not (store / AUTH).exists()
    assert (store / ".claude.json").read_bytes() == (
        b'{"oauthAccount": {"emailAddress": "a@b"}}'
    )
    assert not any(
        b"REAL-459" in p.read_bytes() for p in store.rglob("*") if p.is_file()
    )
    assert credential_state(CLAUDE) == "saved"
    assert not provider_state.authentication_failed(CLAUDE)


def test_a_login_that_is_not_an_account_login_is_not_kept(machine: Path) -> None:
    def api_key(files: dict[str, bytes], root: str) -> None:
        files[f"{root}/.claude.json"] = b'{"primaryApiKey": "sk-ant-api"}'

    with pytest.raises(AgentEnvironmentError) as refused:
        _log_in(machine, api_key)

    assert str(refused.value) == t(
        "intelligences.agent_environment.tool.login_not_an_account", tool=CLAUDE.label
    )
    assert credential_state(CLAUDE) == "missing"
    assert not (provider_state_dir(CLAUDE) / ".claude.json").exists()


# --- a login named for its account, lent through a command (Grok Build) -----------

GROK = cli_agent_info("grok")
GROK_AUTH = GROK.provision.auth


def _grok_login(token: str = "REAL-GROK-459", expires_in: float = 3600) -> bytes:
    expires = dt.datetime.fromtimestamp(time.time() + expires_in, dt.UTC)
    return json.dumps(
        {
            "https://auth.x.ai::00000000-0000-0000-0000-000000000459": {
                "key": token,
                "auth_mode": "oidc",
                "refresh_token": f"{token}-REFRESH",
                # Nanoseconds, as Grok Build writes them.
                "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%S.%f") + "123Z",
                "oidc_issuer": "https://auth.x.ai",
            }
        }
    ).encode()


def _grok_entry(data: bytes) -> dict[str, Any]:
    (entry,) = json.loads(data).values()
    return entry


@pytest.mark.asyncio
async def test_a_login_named_for_its_account_is_lent_through_a_command(
    machine: Path,
) -> None:
    provider_state._seal_login(GROK, {GROK_AUTH: _grok_login()})
    lent = LentLogin(GROK, WHERE)

    assert await lent.access_token(None) == "REAL-GROK-459"
    assert lent.stand_in_files() == {}
    ((variable, command),) = lent.stand_in_environment().items()
    assert variable == "GROK_AUTH_PROVIDER_COMMAND"
    printed = json.loads(command.removeprefix("echo '").removesuffix("'"))
    assert printed["access_token"] == lent.stand_in
    assert printed["expires_in"] > 300 * 24 * 60 * 60
    assert "REAL-GROK-459" not in command
    assert _Environment.instances == []  # Fresh: nothing was refreshed.


@pytest.mark.asyncio
async def test_a_login_named_for_its_account_is_refreshed_and_kept_whole(
    machine: Path,
) -> None:
    """The refresh is told the RFC 3339 expiry has passed, and what the tool
    writes back under its account's name is what is sealed."""
    provider_state._seal_login(GROK, {GROK_AUTH: _grok_login(expires_in=60)})
    given: list[dict[str, Any]] = []

    def act(files: dict[str, bytes], root: str) -> None:
        path = f"{root}/{GROK_AUTH}"
        given.append(_grok_entry(files[path]))
        files[path] = _grok_login("NEW-GROK-459")

    _Environment.act = staticmethod(act)
    lent = LentLogin(GROK, WHERE)

    assert await lent.access_token(None) == "NEW-GROK-459"

    (held,) = given
    assert held["expires_at"] == "1970-01-01T00:00:00Z"
    assert held["refresh_token"] == "REAL-GROK-459-REFRESH"
    (environment,) = _Environment.instances
    assert environment.commands == [GROK.provision.credential_broker.refresh]
    sealed = _grok_entry(provider_state._unsealed_login(GROK)[GROK_AUTH])
    assert (sealed["key"], sealed["refresh_token"]) == (
        "NEW-GROK-459",
        "NEW-GROK-459-REFRESH",
    )


def test_a_login_file_with_more_than_one_account_is_not_a_login(
    machine: Path,
) -> None:
    """Two names are not the one account a tool keeps. The bytes are written
    here, not taken from ``_grok_login``: that helper reads the clock, and a
    value the guard cannot follow back to one call would have to be reported.
    """
    entry = {
        "key": "REAL-GROK-459",
        "auth_mode": "oidc",
        "refresh_token": "REAL-GROK-459-REFRESH",
        "expires_at": "2026-09-01T00:00:00.000000123Z",
        "oidc_issuer": "https://auth.x.ai",
    }
    two = json.dumps({"a": entry, "b": entry}).encode()
    provider_state._seal_login(GROK, {GROK_AUTH: two})

    # The status says so, in the words a turn is refused with.
    assert credential_state(GROK) == "corrupt"
    with pytest.raises(CredentialVaultError) as refused:
        LentLogin(GROK, WHERE)
    assert refused.value.state == "corrupt"


# --- a login read from its JWTs (Codex) ------------------------------------------

CODEX = cli_agent_info("codex")
CODEX_AUTH = CODEX.provision.auth
_ACCOUNT_CLAIM = "https://api.openai.com/auth"


def _jwt(claims: dict[str, Any]) -> str:
    def segment(value: Any) -> str:
        encoded = base64.urlsafe_b64encode(json.dumps(value).encode())
        return encoded.rstrip(b"=").decode()

    return f"{segment({'alg': 'RS256', 'kid': 'k'})}.{segment(claims)}.U0lHTkVE"


def _claims_of(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _codex_login(token: str = "REAL-CODEX-459", expires_in: float = 3600) -> bytes:
    account = {
        "chatgpt_plan_type": "pro",
        "chatgpt_account_id": "account-459",
        "chatgpt_user_id": "USER-SECRET-459",
    }
    exp = int(time.time() + expires_in)
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": _jwt(
                    {
                        "email": "a@example.com",
                        "exp": exp,
                        "sid": "ID-SECRET-459",
                        _ACCOUNT_CLAIM: account,
                    }
                ),
                "access_token": _jwt({"exp": exp, "jti": token}),
                "refresh_token": f"{token}-REFRESH",
                "account_id": "account-459",
            },
            "last_refresh": "2026-09-01T00:00:00Z",
        }
    ).encode()


def _codex_tokens(data: bytes) -> dict[str, Any]:
    return json.loads(data)["tokens"]


def test_a_login_read_from_its_jwts_is_lent_one_that_claims_the_account_only(
    machine: Path,
) -> None:
    sealed = _codex_login()
    provider_state._seal_login(CODEX, {CODEX_AUTH: sealed})
    lent = LentLogin(CODEX, WHERE)

    ((name, data),) = lent.stand_in_files().items()

    held = json.loads(data)
    assert name == CODEX_AUTH
    # The stand-in stands for both tokens; the refresh token is there, empty.
    assert held == {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": lent.stand_in,
            "id_token": lent.stand_in,
            "refresh_token": "",
            "account_id": "account-459",
        },
        "last_refresh": "2026-09-01T00:00:00Z",
    }
    # The connected apps take theirs from a variable, the login from the file.
    assert lent.stand_in_environment() == {"CODEX_CONNECTORS_TOKEN": lent.stand_in}
    header, _claims, secret = lent.stand_in.split(".")
    assert json.loads(base64.urlsafe_b64decode(header + "==")) == {
        "alg": "none",
        "typ": "JWT",
    }
    claimed = _claims_of(lent.stand_in)
    assert claimed.pop("exp") > time.time() + 300 * 24 * 60 * 60
    assert claimed == {
        "email": "a@example.com",
        _ACCOUNT_CLAIM: {
            "chatgpt_plan_type": "pro",
            "chatgpt_account_id": "account-459",
        },
    }
    real = _codex_tokens(sealed)
    for secret_value in (
        real["access_token"],
        real["id_token"],
        real["refresh_token"],
        "USER-SECRET-459",
        "ID-SECRET-459",
    ):
        assert secret_value.encode() not in data
        assert secret_value not in json.dumps(claimed)
    # The turn's own secret, where the signature goes.
    assert secret != LentLogin(CODEX, WHERE).stand_in.split(".")[2]


@pytest.mark.asyncio
async def test_a_login_read_from_its_jwts_expires_when_its_access_token_does(
    machine: Path,
) -> None:
    """The refresh is handed the access token claiming an expiry that has
    passed -- everything else of the token and the login as it was."""
    sealed = _codex_login(expires_in=60)
    provider_state._seal_login(CODEX, {CODEX_AUTH: sealed})
    given: list[dict[str, Any]] = []

    def act(files: dict[str, bytes], root: str) -> None:
        path = f"{root}/{CODEX_AUTH}"
        given.append(_codex_tokens(files[path]))
        files[path] = _codex_login("NEW-CODEX-459", expires_in=864000)

    _Environment.act = staticmethod(act)
    lent = LentLogin(CODEX, WHERE)
    stale = _codex_tokens(sealed)

    token = await lent.access_token(None)

    assert _claims_of(token)["jti"] == "NEW-CODEX-459"
    (held,) = given
    header, _claims, signature = held["access_token"].split(".")
    assert (header, signature) == tuple(stale["access_token"].split(".")[::2])
    assert _claims_of(held["access_token"])["exp"] == 0
    assert _claims_of(held["access_token"])["jti"] == "REAL-CODEX-459"
    assert {k: v for k, v in held.items() if k != "access_token"} == {
        k: v for k, v in stale.items() if k != "access_token"
    }
    (environment,) = _Environment.instances
    assert environment.commands == [("codex", "debug", "models")]
    assert _codex_tokens(provider_state._unsealed_login(CODEX)[CODEX_AUTH])[
        "refresh_token"
    ] == ("NEW-CODEX-459-REFRESH")
    assert await lent.access_token(None) == token
    assert len(_Environment.instances) == 1


@pytest.mark.parametrize(
    "access_token",
    [
        "opaque",
        _jwt({"sub": "no expiry"}),
        _jwt({"exp": 4102444800}).replace(".", ".!!!!", 1),
    ],
    ids=["opaque", "no-expiry", "not-base64"],
)
def test_a_login_whose_access_token_claims_no_expiry_is_not_a_login(
    machine: Path, access_token: str
) -> None:
    login = json.loads(_codex_login())
    login["tokens"]["access_token"] = access_token
    provider_state._seal_login(CODEX, {CODEX_AUTH: json.dumps(login).encode()})

    assert credential_state(CODEX) == "corrupt"


# --- a Google login (Antigravity) ------------------------------------------------

ANTIGRAVITY = cli_agent_info("antigravity")
ANTIGRAVITY_AUTH = ANTIGRAVITY.provision.auth


def _google_login(token: str = "REAL-AGY-459", expires_in: float = 3600) -> bytes:
    expires = dt.datetime.fromtimestamp(time.time() + expires_in, dt.UTC)
    return json.dumps(
        {
            "token": {
                "access_token": token,
                "token_type": "Bearer",
                "refresh_token": f"{token}-REFRESH",
                # Nanoseconds, as Antigravity writes them.
                "expiry": expires.strftime("%Y-%m-%dT%H:%M:%S.%f") + "123Z",
            },
            "auth_method": "consumer",
            "id_token": "ID-SECRET-459",
        }
    ).encode()


def test_a_google_login_is_lent_without_its_refresh_or_id_token(machine: Path) -> None:
    provider_state._seal_login(ANTIGRAVITY, {ANTIGRAVITY_AUTH: _google_login()})
    lent = LentLogin(ANTIGRAVITY, WHERE)

    ((name, data),) = lent.stand_in_files().items()

    held = json.loads(data)
    assert name == ANTIGRAVITY_AUTH
    expiry = dt.datetime.fromisoformat(held["token"].pop("expiry"))
    assert expiry.timestamp() > time.time() + 300 * 24 * 60 * 60
    assert held == {
        "token": {"access_token": lent.stand_in, "token_type": "Bearer"},
        "auth_method": "consumer",
    }
    for secret in (b"REAL-AGY-459", b"ID-SECRET-459"):
        assert secret not in data


@pytest.mark.asyncio
async def test_a_google_login_is_refreshed_reading_its_usage(machine: Path) -> None:
    provider_state._seal_login(
        ANTIGRAVITY, {ANTIGRAVITY_AUTH: _google_login(expires_in=60)}
    )
    given: list[dict[str, Any]] = []

    def act(files: dict[str, bytes], root: str) -> None:
        path = f"{root}/{ANTIGRAVITY_AUTH}"
        given.append(json.loads(files[path]))
        files[path] = _google_login("NEW-AGY-459")

    _Environment.act = staticmethod(act)
    lent = LentLogin(ANTIGRAVITY, WHERE)

    assert await lent.access_token(None) == "NEW-AGY-459"

    (held,) = given
    assert held["token"]["expiry"] == "1970-01-01T00:00:00Z"
    assert held["token"]["refresh_token"] == "REAL-AGY-459-REFRESH"
    (environment,) = _Environment.instances
    assert environment.commands == [("agy", "-p", "/usage", "--output-format", "json")]


# --- a login that neither expires nor refreshes (GitHub Copilot) ----------------

COPILOT = cli_agent_info("copilot")
COPILOT_AUTH = COPILOT.provision.auth


def _github_login(token: str = "gho_REAL459") -> bytes:
    """What `copilot login` leaves: comments above a JSON document."""
    document = {
        "authTokens": {"https://github.com:synthetic459": {"token": token}},
        "lastLoggedInUser": {"host": "https://github.com", "login": "synthetic459"},
        "firstLaunchAt": "2026-09-23T00:00:00.000Z",
    }
    return (
        "// User settings belong in settings.json.\n"
        "// This file is managed automatically.\n" + json.dumps(document, indent=2)
    ).encode()


@pytest.mark.asyncio
async def test_a_login_that_never_expires_is_lent_in_a_variable_of_its_shape(
    machine: Path,
) -> None:
    provider_state._seal_login(COPILOT, {COPILOT_AUTH: _github_login()})
    assert credential_state(COPILOT) == "saved"
    lent = LentLogin(COPILOT, WHERE)

    assert await lent.access_token(None) == "gho_REAL459"
    assert lent.stand_in_files() == {}
    assert lent.stand_in_environment() == {"COPILOT_GITHUB_TOKEN": lent.stand_in}
    # Copilot takes only a token that looks like GitHub's.
    assert lent.stand_in.startswith("gho_") and "REAL459" not in lent.stand_in
    assert _Environment.instances == []


@pytest.mark.asyncio
async def test_a_login_that_never_expires_is_logged_in_again_once_refused(
    machine: Path,
) -> None:
    """There is nothing to refresh it with: a refused one is revoked."""
    provider_state._seal_login(COPILOT, {COPILOT_AUTH: _github_login()})
    lent = LentLogin(COPILOT, WHERE)

    for token in ("gho_REAL459", "gho_REAL459", None):
        with pytest.raises(CredentialUnavailableError) as refused:
            await lent.access_token(token)

    assert str(refused.value) == t(
        "intelligences.agent_environment.tool.login_refused",
        tool=COPILOT.label,
        command=provider_state.login_command("copilot"),
    )
    assert _Environment.instances == []


@pytest.mark.parametrize(
    "data",
    [
        b'  // indented\n{"a": {"//b": "kept"}}',
        b'{"a": {"//b": "kept"}}',
    ],
    ids=["indented-comment", "no-comment"],
)
def test_a_credentials_file_is_read_past_its_lines_of_comments(data: bytes) -> None:
    """Only a whole line of comment goes; a key that begins alike stays."""
    assert provider_state._parsed(data) == {"a": {"//b": "kept"}}


@pytest.mark.asyncio
async def test_a_login_read_from_its_jwts_the_tool_did_not_refresh_is_kept(
    machine: Path,
) -> None:
    """The login handed to the refresh claims to have expired, so it is no
    longer the token sealed; left as it was handed, it was not refreshed,
    and nothing of it is sealed over the login."""
    sealed = _codex_login(expires_in=60)
    provider_state._seal_login(CODEX, {CODEX_AUTH: sealed})
    lent = LentLogin(CODEX, WHERE)

    with pytest.raises(CredentialUnavailableError):
        await lent.access_token(None)

    assert provider_state._unsealed_login(CODEX)[CODEX_AUTH] == sealed
    assert not provider_state.authentication_failed(CODEX)


def _values(data: bytes) -> list[str]:
    """Every string value of a login file, in order."""
    document = provider_state._parsed(data)
    found: list[str] = []
    pending: list[Any] = [document]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
        elif isinstance(node, str):
            found.append(node)
    return found


def _assert_the_login_is_told_masked(
    tool: CliAgentInfo, login: bytes, kept: set[str]
) -> None:
    """Every value of the login is masked but those a turn is lent anyway,
    and those too short to be a credential; a credential the file gains in a
    later version of the tool is masked too.

    The login is built in the test and passed in. A parameter or a
    ``parametrize`` argument is evaluated where this function cannot see the
    call, so the guard would have to report it instead of checking it.
    """
    said = " | ".join(_values(login))

    told = provider_state.masked(tool, f"failed: {said}")

    assert told.startswith("failed: ") and len(told) == len(f"failed: {said}")
    for value in _values(login):
        if value in kept or len(value) < 8:
            assert value in told, value
        else:
            assert value not in told, value


def test_what_comes_from_a_claude_login_is_told_with_it_masked(machine: Path) -> None:
    login = _login()
    provider_state._seal_login(CLAUDE, {AUTH: login})
    _assert_the_login_is_told_masked(
        CLAUDE, login, {"user:inference", "user:profile", "max"}
    )


def test_what_comes_from_a_codex_login_is_told_with_it_masked(machine: Path) -> None:
    login = _codex_login()
    provider_state._seal_login(CODEX, {CODEX_AUTH: login})
    _assert_the_login_is_told_masked(
        CODEX, login, {"chatgpt", "account-459", "2026-09-01T00:00:00Z"}
    )


def test_what_comes_from_a_grok_login_is_told_with_it_masked(machine: Path) -> None:
    login = _grok_login()
    provider_state._seal_login(GROK, {GROK_AUTH: login})
    _assert_the_login_is_told_masked(GROK, login, {"oidc"})


def test_what_comes_from_an_antigravity_login_is_told_with_it_masked(
    machine: Path,
) -> None:
    login = _google_login()
    provider_state._seal_login(ANTIGRAVITY, {ANTIGRAVITY_AUTH: login})
    _assert_the_login_is_told_masked(ANTIGRAVITY, login, {"Bearer", "consumer"})


def test_what_comes_from_a_copilot_login_is_told_with_it_masked(machine: Path) -> None:
    login = _github_login()
    provider_state._seal_login(COPILOT, {COPILOT_AUTH: login})
    _assert_the_login_is_told_masked(COPILOT, login, set())


def test_a_part_of_a_jwt_is_masked_on_its_own(machine: Path) -> None:
    """A tool may print a token's claims without the rest of it."""
    sealed = _codex_login()
    provider_state._seal_login(CODEX, {CODEX_AUTH: sealed})
    token = _codex_tokens(sealed)["access_token"]
    _header, claims, _signature = token.split(".")

    assert claims not in provider_state.masked(CODEX, f"claims: {claims}")


def test_what_cannot_be_masked_is_not_told(machine: Path) -> None:
    """Without the login to mask it by, nothing of what was said is kept."""
    assert "REAL-459" not in provider_state.masked(CLAUDE, "failed: REAL-459")


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_clock_read(node: ast.AST) -> bool:
    """True for the attribute ``time.time`` itself.

    That call is what makes two builds differ across a second. The attribute
    is the whole definition; a callee in another module is not one of these.
    """
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "time"
        and node.attr == "time"
    )


def _reads_the_clock(function: ast.AST) -> bool:
    """True when the function itself calls ``time.time``.

    Nested functions count, so a builder that hides the call still joins the
    population.
    """
    return any(_is_clock_read(node) for node in ast.walk(function))


def _clock_reading_helpers(tree: ast.AST) -> frozenset[str]:
    """Module-level helpers whose bytes depend on ``time.time``.

    The population is the clock read, not a provider name. ``_github_login``
    stays out because it does not read the clock. Test functions stay out
    because a ``time.time`` in an assertion is not a login being built.
    """
    return frozenset(
        node.name
        for node in getattr(tree, "body", [])
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and not node.name.startswith("test_")
        and _reads_the_clock(node)
    )


def _is_login_call(node: ast.AST, helpers: frozenset[str]) -> bool:
    return isinstance(node, ast.Call) and _call_name(node.func) in helpers


def _local_nodes(function: ast.AST):
    """Nodes of one function, excluding anything nested in another function."""
    pending = list(getattr(function, "body", []))
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        yield node
        pending.extend(ast.iter_child_nodes(node))


def _login_in(expression: ast.AST, helpers: frozenset[str]) -> ast.Call | None:
    calls = [node for node in ast.walk(expression) if _is_login_call(node, helpers)]
    if len(calls) == 1:
        return calls[0]
    return None


def _bind_login_names(
    target: ast.expr,
    value: ast.expr,
    bound: dict[str, ast.Call],
    helpers: frozenset[str],
) -> None:
    """Bind names to the one login call that initializes them.

    A bare name uses the whole value. A tuple pairs each element with the
    value in the same position, so ``sealed, other = helper(), helper()``
    keeps both calls. A name this does not bind is not a sealed call. The
    guard reports that, unless the sealed bytes are not a clock read.
    """
    if isinstance(target, ast.Name):
        call = _login_in(value, helpers)
        if call is not None:
            bound[target.id] = call
        return
    if not isinstance(target, ast.Tuple | ast.List):
        return
    if not isinstance(value, ast.Tuple | ast.List):
        return
    if len(target.elts) != len(value.elts):
        return
    if any(isinstance(elt, ast.Starred) for elt in (*target.elts, *value.elts)):
        return
    for elt, piece in zip(target.elts, value.elts, strict=True):
        _bind_login_names(elt, piece, bound, helpers)


def _login_bound_here(
    function: ast.AST, helpers: frozenset[str]
) -> dict[str, ast.Call]:
    """Names assigned in this function from an expression with one login call.

    The walk is ``_local_nodes``, the same one that finds ``_seal_login`` and
    comparisons. Stopping at the function's top-level statements left a name
    assigned inside ``if`` / ``for`` / ``with`` unresolved, and the function
    then dropped out of the population.
    """
    bound: dict[str, ast.Call] = {}
    assignments = [
        node
        for node in _local_nodes(function)
        if isinstance(node, ast.Assign | ast.AnnAssign)
    ]
    for statement in sorted(
        assignments, key=lambda node: (node.lineno, node.col_offset)
    ):
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                _bind_login_names(target, statement.value, bound, helpers)
        elif statement.value is not None:
            _bind_login_names(statement.target, statement.value, bound, helpers)
    return bound


def _login_call(
    node: ast.AST, bound: dict[str, ast.Call], helpers: frozenset[str]
) -> ast.Call | None:
    if _is_login_call(node, helpers):
        return node
    if isinstance(node, ast.Name):
        return bound.get(node.id)
    return None


def _calls_under(
    expression: ast.AST, bound: dict[str, ast.Call], helpers: frozenset[str]
) -> set[int]:
    return {
        id(call)
        for node in ast.walk(expression)
        if (call := _login_call(node, bound, helpers)) is not None
    }


def _sealed_logins(
    function: ast.AST, bound: dict[str, ast.Call], helpers: frozenset[str]
) -> set[int]:
    sealed: set[int] = set()
    for node in _local_nodes(function):
        if isinstance(node, ast.Call) and _call_name(node.func) == "_seal_login":
            sealed.update(_calls_under(node, bound, helpers))
    return sealed


def _expected_logins(
    function: ast.AST, bound: dict[str, ast.Call], helpers: frozenset[str]
) -> set[int]:
    """Logins a test treats as the value it sealed: token reads and compares.

    ``_codex_tokens`` is named because the compared value is a piece of the
    JWT, so the login call is not itself in the comparison. Other providers
    compare a name bound to the call, which the comparison walk already sees.
    """
    expected: set[int] = set()
    for node in _local_nodes(function):
        if isinstance(node, ast.Call) and _call_name(node.func) == "_codex_tokens":
            for arg in node.args:
                expected.update(_calls_under(arg, bound, helpers))
        elif isinstance(node, ast.Compare):
            expected.update(_calls_under(node.left, bound, helpers))
            for comparator in node.comparators:
                expected.update(_calls_under(comparator, bound, helpers))
    return expected


def _module_names(tree: ast.AST) -> frozenset[str]:
    """Imports, classes, and functions on the module, plus builtins.

    An assignment is not one of these. Its value is followed, and a name
    this walk cannot follow is ``unknown``. A parameter is neither.
    """
    import builtins

    names = set(dir(builtins))
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add("*" if alias.name == "*" else alias.asname or alias.name)
    return frozenset(names)


def _module_functions(tree: ast.AST) -> frozenset[str]:
    """Functions defined on the module body, not inside another function.

    A call of one of these that is not a clock-reading helper is a call this
    walk can see does not read the clock. A parameter, a local name, or a
    free name is not that function.
    """
    return frozenset(
        node.name
        for node in getattr(tree, "body", [])
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    )


def _parameter_names(function: ast.AST) -> frozenset[str]:
    args = getattr(function, "args", None)
    if args is None:
        return frozenset()
    found = [arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    if args.vararg is not None:
        found.append(args.vararg.arg)
    if args.kwarg is not None:
        found.append(args.kwarg.arg)
    return frozenset(found)


def _simple_assignments(function: ast.AST) -> dict[str, ast.expr]:
    """Last simple assignment of each name. A tuple target is not one call."""
    assigned: dict[str, ast.expr] = {}
    statements = [
        node
        for node in _local_nodes(function)
        if isinstance(node, ast.Assign | ast.AnnAssign)
    ]
    for statement in sorted(
        statements, key=lambda node: (node.lineno, node.col_offset)
    ):
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    assigned[target.id] = statement.value
        elif isinstance(statement.target, ast.Name) and statement.value is not None:
            assigned[statement.target.id] = statement.value
    return assigned


def _sealed_byte_expressions(function: ast.AST) -> list[ast.expr | None]:
    """The file bytes passed to each ``_seal_login``, or None when unseen.

    ``None`` is a call whose mapping this walk cannot see. That is the same
    as a login it cannot name: the function is reported.
    """
    found: list[ast.expr | None] = []
    for node in _local_nodes(function):
        if not isinstance(node, ast.Call) or _call_name(node.func) != "_seal_login":
            continue
        mapping: ast.expr | None = node.args[1] if len(node.args) >= 2 else None
        for keyword in node.keywords:
            if keyword.arg == "files":
                mapping = keyword.value
        if not isinstance(mapping, ast.Dict):
            found.append(mapping)
            continue
        values = [value for value in mapping.values if value is not None]
        found.extend(values or [None])
    return found


def _combine_origins(origins: list[str]) -> str:
    if "clock" in origins:
        return "clock"
    if "unknown" in origins:
        return "unknown"
    return "plain"


def _byte_origin(
    node: ast.AST,
    *,
    assigned: dict[str, ast.expr],
    parameters: frozenset[str],
    module_names: frozenset[str],
    module_assigned: dict[str, ast.expr],
    module_functions: frozenset[str],
    helpers: frozenset[str],
    visiting: set[str],
) -> str:
    """Where sealed bytes come from: a clock read, plainly not, or unnamed.

    ``plain`` is the only answer that leaves a call out of the population,
    and only when this walk has seen that the bytes are not a clock read: a
    literal, a module import or function used as a value, or a call of a
    module function that does not read the clock. A direct ``time.time()``
    is that clock read, in the expression or through a simple assignment.
    A module assignment is followed the same way as one in the function. A
    parameter, a free name, a name this walk cannot follow, or a call through
    anything other than such a function is ``unknown`` and is reported.
    Following one more shape (a decorator, a fixture, a tuple unpack) is the
    path this refuses to grow.

    An attribute callee's body is not in this file, so it is not followed.
    The call stays ``plain`` when it is not ``time.time()`` and its arguments
    and receiver are. That is the boundary, not a claim that the callee
    ignores the clock.
    """

    def origin(
        child: ast.AST,
        *,
        assigned: dict[str, ast.expr] = assigned,
        parameters: frozenset[str] = parameters,
    ) -> str:
        return _byte_origin(
            child,
            assigned=assigned,
            parameters=parameters,
            module_names=module_names,
            module_assigned=module_assigned,
            module_functions=module_functions,
            helpers=helpers,
            visiting=visiting,
        )

    if isinstance(node, ast.Constant):
        return "plain"
    if isinstance(node, ast.Name):
        if node.id in visiting:
            return "unknown"
        if node.id in parameters:
            return "unknown"
        if node.id in assigned or node.id in module_assigned:
            visiting.add(node.id)
            try:
                if node.id in assigned:
                    return origin(assigned[node.id])
                # A module assignment is not inside this function. Names in
                # it are module names, not this function's parameters.
                return origin(
                    module_assigned[node.id],
                    assigned={},
                    parameters=frozenset(),
                )
            finally:
                visiting.discard(node.id)
        if node.id in module_names:
            return "plain"
        return "unknown"
    if isinstance(
        node,
        ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp | ast.Lambda,
    ):
        return "unknown"
    if isinstance(node, ast.Call):
        if _is_login_call(node, helpers) or _is_clock_read(node.func):
            return "clock"
        # A Name callee counts only when it is a module function this walk
        # already classified. A parameter, a local, or a free name is not.
        if not isinstance(node.func, ast.Name | ast.Attribute):
            return "unknown"
        if isinstance(node.func, ast.Name) and (
            node.func.id in assigned
            or node.func.id in parameters
            or node.func.id not in module_functions
        ):
            return "unknown"
        parts = [origin(arg) for arg in node.args]
        parts.extend(origin(keyword.value) for keyword in node.keywords)
        if isinstance(node.func, ast.Attribute):
            parts.append(origin(node.func.value))
        return _combine_origins(parts)
    children = [
        child for child in ast.iter_child_nodes(node) if isinstance(child, ast.expr)
    ]
    if not children:
        return "plain" if isinstance(node, ast.expr) else "unknown"
    return _combine_origins([origin(child) for child in children])


def _logins_the_guard_reports(tree: ast.AST) -> list[str]:
    """Tests that seal one clock read and compare another, or seal unnamed bytes.

    A ``_seal_login`` whose bytes are not a clock-reading helper, and can be
    seen to be so, is outside the population (``_github_login``). Anything
    else that does not name the sealed call is reported. Skipping it is how
    a login built in ``parametrize`` or a fixture used to pass unexamined.
    """
    helpers = _clock_reading_helpers(tree)
    module_names = _module_names(tree)
    module_assigned = _simple_assignments(tree)
    module_functions = _module_functions(tree)
    apart: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        bound = _login_bound_here(node, helpers)
        sealed = _sealed_logins(node, bound, helpers)
        assigned = _simple_assignments(node)
        parameters = _parameter_names(node)
        unnamed = False
        for expression in _sealed_byte_expressions(node):
            if expression is not None and _calls_under(expression, bound, helpers):
                continue
            origin = (
                "unknown"
                if expression is None
                else _byte_origin(
                    expression,
                    assigned=assigned,
                    parameters=parameters,
                    module_names=module_names,
                    module_assigned=module_assigned,
                    module_functions=module_functions,
                    helpers=helpers,
                    visiting=set(),
                )
            )
            if origin != "plain":
                unnamed = True
        stray = _expected_logins(node, bound, helpers) - sealed
        if unnamed or (sealed and stray):
            apart.append(f"{node.name}:{node.lineno}")
    return sorted(apart)


def test_a_login_is_compared_with_the_one_that_was_sealed() -> None:
    """A helper that reads ``time.time`` can fall on either side of a second.

    The bytes that are sealed and the bytes a test treats as what was sealed
    have to be the same call. A second call used as the expected value is the
    flake, and the same shape on the ``not in`` side is the check that passes
    without looking at the login that was sealed. Which helpers those are is
    derived from the clock read. An empty derivation would pass every test
    without looking, so it fails here. A ``_seal_login`` whose call cannot be
    named fails too, unless the bytes are visibly not that clock read.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    helpers = _clock_reading_helpers(tree)
    assert helpers, "clock-reading helpers were not derived; the guard would pass open"
    apart = _logins_the_guard_reports(tree)
    assert not apart, apart


def test_a_login_the_guard_cannot_name_is_reported() -> None:
    """The population is not grown by reading decorators, fixtures, or unpacks.

    A parameter (what ``parametrize`` and a fixture pass in), a call through
    a parameter or a free name, a tuple unpack of a clock read, a module
    assignment of a clock read, and bytes written out with ``time.time`` are
    reported by name. A helper that does not read the clock, and bytes that
    are not a login, are not, including when that helper or those bytes are
    named at module level. An attribute call whose arguments do not read the
    clock is one of those: its body is not in this file.
    """
    sample = "\n".join(
        (
            "import json",
            "import time",
            "",
            "def _login():",
            "    return time.time()",
            "",
            "def _github_login():",
            "    return b'gho'",
            "",
            "_MODULE_LOGIN = _login()",
            "_MODULE_GITHUB = _github_login()",
            "_MODULE_LITERAL = b'written'",
            "",
            "def test_direct():",
            "    provider_state._seal_login(CLAUDE, {AUTH: _login()})",
            "",
            "def test_reused():",
            "    sealed = _login()",
            "    provider_state._seal_login(CLAUDE, {AUTH: sealed})",
            "",
            "def test_second_call():",
            "    sealed = _login()",
            "    provider_state._seal_login(CLAUDE, {AUTH: sealed})",
            "    _codex_tokens(_login())",
            "",
            "def test_github():",
            "    provider_state._seal_login(COPILOT, {AUTH: _github_login()})",
            "",
            "def test_github_reused():",
            "    sealed = _github_login()",
            "    provider_state._seal_login(COPILOT, {AUTH: sealed})",
            "",
            "def test_module_name():",
            "    provider_state._seal_login(CODEX, {AUTH: _MODULE_LOGIN})",
            "",
            "def test_module_github():",
            "    provider_state._seal_login(COPILOT, {AUTH: _MODULE_GITHUB})",
            "",
            "def test_module_literal():",
            "    provider_state._seal_login(GROK, {AUTH: _MODULE_LITERAL})",
            "",
            "def test_parameter(login):",
            "    provider_state._seal_login(CODEX, {AUTH: login})",
            "",
            "def test_parameter_call(make_login):",
            "    provider_state._seal_login(CODEX, {AUTH: make_login()})",
            "",
            "def test_free_name():",
            "    provider_state._seal_login(CODEX, {AUTH: build_a_login()})",
            "",
            "def test_tuple_unpack():",
            "    (entry,) = json.loads(_login()).values()",
            "    two = json.dumps({'a': entry, 'b': entry}).encode()",
            "    provider_state._seal_login(GROK, {AUTH: two})",
            "",
            "def test_literal():",
            "    two = json.dumps({'a': {}, 'b': {}}).encode()",
            "    provider_state._seal_login(GROK, {AUTH: two})",
            "",
            "def test_written_clock():",
            "    blob = json.dumps({'expires_at': time.time() + 3600}).encode()",
            "    provider_state._seal_login(CLAUDE, {AUTH: blob})",
            "",
            "def test_named_clock():",
            "    now = time.time()",
            "    blob = json.dumps({'expires_at': now + 3600}).encode()",
            "    provider_state._seal_login(CLAUDE, {AUTH: blob})",
            "",
        )
    )
    reported = {
        item.split(":", 1)[0] for item in _logins_the_guard_reports(ast.parse(sample))
    }
    assert reported == {
        "test_second_call",
        "test_parameter",
        "test_parameter_call",
        "test_free_name",
        "test_tuple_unpack",
        "test_module_name",
        "test_written_clock",
        "test_named_clock",
    }
