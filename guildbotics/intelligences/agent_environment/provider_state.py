"""What a provider keeps between turns on this device, and its login.

The environment is discarded after every turn, so a provider's sessions live
outside it, in a store this device keeps per provider
(``~/.guildbotics/data/agent_environment/<provider>/``) and every member
shares. Only the entries the provider's provision names are ever kept from a
turn: the sessions, and the account files that hold no credential. The rest
of the provider's directory -- its settings, skills, plugins, and everything
else it reads its instructions and its tools from -- is the snapshot's, so
what an agent changes there is gone with the turn. A persisted directory is
bound from the store, and a turn writes into it as it goes; so is a persisted
file, once it exists.

A provider's login is in no turn's microVM, nor in a plain file on this
device. Logging in is the one interactive step: the provider's own login
command runs inside an environment with its state root in memory, nothing
else of the device, and all egress open, because the addresses an OAuth
exchange visits are the provider's business and change with its versions.
What it leaves is taken out while the environment still runs and sealed
(:mod:`.credential_vault`). A turn is lent the login through a gateway
outside its microVM (:mod:`.auth_gateway`) and holds only a stand-in. What
must hold the real login -- the refresh, and the tool's own ``/usage`` --
runs in an environment of its own with the login in memory, no workspace,
and the provider's domains only, one at a time on this device, and the
refreshed login is sealed before that environment is stopped.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import secrets
import shlex
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from logging import getLogger
from pathlib import Path
from typing import Any

from guildbotics.intelligences.agent_environment.auth_gateway import (
    CredentialUnavailableError,
)
from guildbotics.intelligences.agent_environment.credential_vault import (
    CredentialVaultError,
    HeldVaultLock,
    VaultState,
    held_vault_lock,
    seal,
    unseal,
    vault_problem,
)
from guildbotics.intelligences.agent_environment.runtime import (
    AgentEnvironment,
    AgentEnvironmentError,
)
from guildbotics.intelligences.agent_environment.spec import (
    AgentEnvironmentSpec,
    EnvironmentMount,
    EnvironmentNetwork,
    guest_home,
)
from guildbotics.intelligences.agent_environment.toolchain import (
    ToolchainDeclaration,
    upstream_nameservers,
)
from guildbotics.intelligences.cli_agents import CliAgentInfo, CredentialBroker
from guildbotics.utils.fileio import (
    atomic_write_bytes,
    atomic_write_text,
    get_machine_state_path,
)
from guildbotics.utils.i18n_tool import t

#: This device's per-provider stores, and the cache turns keep between them.
STATE_ROOT = ("agent_environment",)
CACHE_DIR = "cache"
#: Under a provider's directory: the store its read-only turns keep.
READ_ONLY_STORE = "read-only"
#: Longest line the login command may print.
_LOGIN_LINE_LIMIT = 1 << 16
#: Where a brokered tool's login is sealed, beside its store.
SEALED_LOGIN = "login.sealed"
#: The one account a tool has on a device; part of the sealed record's name.
_ACCOUNT = "default"
#: A lent token this close to its expiry is refreshed before it is sent.
_REFRESH_MARGIN_SECONDS = 5 * 60
#: The shortest value of a login taken for one a credential could be.
_SHORTEST_CREDENTIAL = 8
#: How long a refresh or a probe may keep the login to itself, and how long
#: another one waits for it.
_LOGIN_HOLD_SECONDS = 90.0
_LOGIN_WAIT_SECONDS = 3 * _LOGIN_HOLD_SECONDS
#: How far ahead a stand-in claims to expire: never, as far as a turn goes.
_STAND_IN_LIFETIME_SECONDS = 365 * 24 * 60 * 60
_LOGGER = getLogger(__name__)


def provider_state_dir(tool: CliAgentInfo) -> Path:
    """The provider's store on this device: its state root, as it is under home."""
    return get_machine_state_path(*STATE_ROOT, tool.name, tool.provision.state_root)


def cache_dir() -> Path:
    """The ``~/.cache`` turns share, so what one turn fetched the next has."""
    return get_machine_state_path(*STATE_ROOT, CACHE_DIR)


def _inside(root: Path, entry: str) -> Path | None:
    """``entry`` under ``root``, or None when it resolves to a place outside.

    The store is bound by what its names resolve to on the device: the
    runtime binds a mount's resolved path. A link in it was spelled for the
    guest's file system by whatever wrote it -- a turn under a prompt's
    direction, or an earlier GuildBotics' login -- and one that leads out of
    the store would bind any place on the device into the environment. So
    it is treated as absent, wherever on the way it stands.
    """
    path = root / entry
    return path if path.resolve().is_relative_to(root.resolve()) else None


def credential_state(tool: CliAgentInfo) -> VaultState:
    """What of the provider's login this device holds.

    The sealed login is read the way a turn would read it, so a locked
    keychain, a record that no longer opens, or one that is no longer a
    login is reported, not found out at the next turn.
    """
    broker = tool.provision.credential_broker
    if broker is None:
        return "missing"
    try:
        _access(broker, _unsealed_login(tool), tool)
    except CredentialVaultError as exc:
        return exc.state
    return "saved"


def has_credentials(tool: CliAgentInfo) -> bool:
    """Whether this device holds a sealed login of the provider that opens."""
    return credential_state(tool) == "saved"


def login_command(name: str, *, platform: str | None = None) -> str:
    """The terminal login instruction shared by status, alerts, and Desktop.

    Windows installers put the CLI on PATH. Unix Desktop installs it under
    home; quote its absolute path so spaces and shell metacharacters survive.
    """
    if (platform or sys.platform) == "win32":
        return f"guildbotics environment login {name}"
    return shlex.join(
        [
            str(Path.home() / ".guildbotics/bin/guildbotics"),
            "environment",
            "login",
            name,
        ]
    )


def sealed_login_path(tool: CliAgentInfo) -> Path:
    """Where a brokered tool's login is sealed on this device."""
    return get_machine_state_path(*STATE_ROOT, tool.name, SEALED_LOGIN)


def authentication_failed(tool: CliAgentInfo) -> bool:
    """Whether the last known authentication outcome on this device failed."""
    return _failure_path(tool).is_file()


def record_authentication_outcome(tool: CliAgentInfo, *, failed: bool) -> None:
    """Keep only the latest outcome, shared by all members on this device.

    This marker is outside the provider's mounted store and contains no
    credentials or provider output. Unknown outcomes must not call this.
    """
    path = _failure_path(tool)
    if failed:
        atomic_write_text(path, "authentication failed\n")
    else:
        path.unlink(missing_ok=True)


def _failure_path(tool: CliAgentInfo) -> Path:
    return get_machine_state_path(*STATE_ROOT, tool.name, "authentication-failed")


def read_only_state_dir(tool: CliAgentInfo) -> Path:
    """The store read-only turns of the provider keep apart from every other."""
    return get_machine_state_path(
        *STATE_ROOT, tool.name, READ_ONLY_STORE, tool.provision.state_root
    )


def bind_state(
    tool: CliAgentInfo, home: Path | None = None, *, read_only: bool = False
) -> tuple[EnvironmentMount, ...]:
    """What a turn of ``tool`` binds of this device's store.

    A persisted directory is created in the store, so a first turn can fill
    it, and bound at its place under the state root. A persisted file is
    bound only once it exists.

    A read-only turn is bound a store of its own and no cache: its sessions,
    its provider's settings, and what it fetched would otherwise be read,
    resumed, or run by the next turn that may change things. The account
    files it needs are copied in afresh from the provider's store, so what
    one read-only turn changes in them no turn reads.
    """
    provision = tool.provision
    shared = provider_state_dir(tool)
    store = read_only_state_dir(tool) if read_only else shared
    root = f"{guest_home(home)}/{provision.state_root}"
    mounts = []
    for entry in provision.persisted:
        host = _inside(store, entry.rstrip("/"))
        if host is None:
            continue
        if entry.endswith("/"):
            host.mkdir(parents=True, exist_ok=True, mode=0o700)
        elif read_only:
            account = _inside(shared, entry)
            if account is None or not account.is_file():
                host.unlink(missing_ok=True)
                continue
            host.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write_bytes(host, account.read_bytes())
        elif not host.is_file():
            continue
        mounts.append(EnvironmentMount(f"{root}/{entry.rstrip('/')}", host, False))
    if not read_only:
        cache = cache_dir()
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        mounts.append(EnvironmentMount(f"{guest_home(home)}/.cache", cache, False))
    return tuple(mounts)


def login_spec(
    tool: CliAgentInfo, declaration: ToolchainDeclaration, home: Path | None = None
) -> AgentEnvironmentSpec:
    """The environment the provider's login command runs in: its state root
    in memory, where :func:`login` takes the login out of."""
    return _state_root_spec(
        tool,
        EnvironmentNetwork(
            unrestricted=True,
            domains=(),
            host_ports=(),
            local_network=False,
            nameservers=upstream_nameservers(declaration.dns),
        ),
        home,
    )


def _state_root_spec(
    tool: CliAgentInfo, network: EnvironmentNetwork, home: Path | None = None
) -> AgentEnvironmentSpec:
    """An environment of the tool's state root alone, in memory, and nothing
    else of the device."""
    guest = guest_home(home)
    return AgentEnvironmentSpec(
        cwd=guest,
        home=guest,
        mounts=(EnvironmentMount(f"{guest}/{tool.provision.state_root}", None, False),),
        network=network,
        env=tool.provision.environment(guest),
    )


async def login(
    tool: CliAgentInfo,
    declaration: ToolchainDeclaration,
    *,
    snapshot: Path,
    read_line: Callable[[], str | None],
    write: Callable[[str], None],
    home: Path | None = None,
) -> int:
    """Run the provider's login command inside the environment, interactively.

    The command gets a terminal: a tool that must ask before it stores its
    credentials in a file (Copilot, where there is no keychain) only asks on
    one.

    Args:
        tool: The provider to log in to; it must be provisioned.
        declaration: Where the environment's DNS gateway forwards to.
        snapshot: The snapshot to boot from.
        read_line: Blocks for one line the user typed, or None at the end
            of their input; it is called from a worker thread.
        write: Receives what the login command prints, as it comes; a
            prompt waits without a newline, so this is not line by line.
        home: The host home directory, the guest's too.

    Returns:
        The login command's exit code.
    """
    environment = await AgentEnvironment.start(
        login_spec(tool, declaration, home),
        snapshot=str(snapshot),
        memory_mib=declaration.resources.memory_mib,
        cpus=declaration.resources.cpus,
    )
    try:
        process = await environment.run(
            *tool.provision.login, limit=_LOGIN_LINE_LIMIT, tty=True
        )

        async def pump(reader: asyncio.StreamReader) -> None:
            while chunk := await reader.read(_LOGIN_LINE_LIMIT):
                write(chunk.decode(errors="replace"))

        async def feed() -> None:
            while (line := await asyncio.to_thread(read_line)) is not None:
                if process.returncode is not None:
                    return
                process.stdin.write(line.encode())
                await process.stdin.drain()
            process.stdin.close()

        feeding = asyncio.create_task(feed())
        try:
            await asyncio.gather(pump(process.stdout), pump(process.stderr))
            code = await process.wait()
            if code == 0:
                await _keep_login(tool, environment)
                record_authentication_outcome(tool, failed=False)
            return code
        finally:
            feeding.cancel()
    finally:
        await environment.close()


async def _keep_login(tool: CliAgentInfo, environment: AgentEnvironment) -> None:
    """Take a brokered login out of the login's environment and seal it.

    The rest of what the login left under the state root is kept only as far
    as the tool's persisted files name it (Claude Code's account file). Both
    are written under the login's hold, as a refresh's are.

    Raises:
        AgentEnvironmentError: When the login is not an account login that
            can be kept, or cannot be sealed.
    """
    provision = tool.provision
    broker = provision.credential_broker
    assert broker is not None
    root = f"{environment.spec.home}/{provision.state_root}"
    auth = await environment.read_file(f"{root}/{provision.auth}")
    files = {provision.auth: auth} if auth is not None else {}
    try:
        _account_login(broker, files, provision.auth)
    except ValueError as exc:
        raise AgentEnvironmentError(
            t(
                "intelligences.agent_environment.tool.login_not_an_account",
                tool=tool.label,
            )
        ) from exc
    kept = {
        name: data
        for name in provision.persisted
        if not name.endswith("/")
        and (data := await environment.read_file(f"{root}/{name}")) is not None
    }
    # Under the login's hold: a refresh still running on the login this one
    # replaces must not seal over it afterwards.
    try:
        hold = await _hold_login(tool)
    except CredentialUnavailableError as exc:
        raise AgentEnvironmentError(str(exc)) from exc
    try:
        _seal_login(tool, files)
        store = provider_state_dir(tool)
        for name, data in kept.items():
            if (target := _inside(store, name)) is not None:
                atomic_write_bytes(target, data)
    except CredentialVaultError as exc:
        raise AgentEnvironmentError(_vault_refusal(tool, exc)) from exc
    finally:
        hold.release()


@dataclass(frozen=True, slots=True)
class LoginEnvironment:
    """What an environment that holds a login boots from on this device."""

    snapshot: Path
    memory_mib: int
    cpus: int
    nameservers: tuple[str, ...]


class LentLogin:
    """A brokered login lent to one turn: the token its gateway sends.

    The token is read from the sealed login once, kept in memory for the
    turn, and refreshed when it is about to expire or the provider refuses
    it. Turns lend the same login side by side; only the refresh is one at
    a time on this device (:func:`refresh_login`), so none of them waits for
    another turn to end. A refresh that did not give the turn a token the
    provider takes is not tried again in the same turn.
    """

    def __init__(self, tool: CliAgentInfo, where: LoginEnvironment) -> None:
        self._tool = tool
        self._where = where
        self._lock = asyncio.Lock()
        self._failure: CredentialUnavailableError | None = None
        self._refreshed = False
        self.files = _unsealed_login(tool)
        self._token, self._expires = _access(self._broker, self.files, tool)
        #: What the turn holds for the access token, and its gateway takes.
        self.stand_in = _minted(self._broker, self._login)

    @property
    def _broker(self) -> CredentialBroker:
        broker = self._tool.provision.credential_broker
        assert broker is not None
        return broker

    @property
    def _login(self) -> Any:
        return _parsed(self.files[self._tool.provision.auth])

    def stand_in_files(self) -> dict[str, bytes]:
        """The login files a turn holds, built from the named fields only:
        the stand-in for the access token, an expiry the turn never reaches,
        and the non-secret fields the catalog names -- never a refresh token
        (an empty one at most), nor any other credential the file holds.
        Nothing for a tool that takes its stand-in from a command or a
        variable (:meth:`stand_in_environment`)."""
        broker = self._broker
        if broker.stand_in_command_env or broker.stand_in_env:
            return {}
        login = self._login
        held: dict[str, Any] = {}
        _put(held, _resolved(login, broker.access_token), self.stand_in)
        if broker.stand_in_claims_from:
            _put(held, _resolved(login, broker.stand_in_claims_from), self.stand_in)
        if broker.empty_refresh_token:
            _put(held, _resolved(login, broker.refresh_token), "")
        if broker.expires_format != "jwt":  # A JWT stand-in carries its own.
            _put(
                held,
                _resolved(login, broker.expires_at),
                _expiry(broker, time.time() + _STAND_IN_LIFETIME_SECONDS),
            )
        for path in broker.turn_fields:
            if (value := _at(login, path)) is not None:
                _put(held, _resolved(login, path), value)
        return {self._tool.provision.auth: json.dumps(held).encode()}

    def stand_in_environment(self) -> dict[str, str]:
        """The variables a turn is given its stand-in in: the one a tool takes
        it from, or the command that prints it with a lifetime the turn never
        reaches, and any it is given in as well."""
        broker = self._broker
        told = dict.fromkeys(broker.stand_in_also_env, self.stand_in)
        if broker.stand_in_env:
            told[broker.stand_in_env] = self.stand_in
        if broker.stand_in_command_env:
            printed = json.dumps(
                {
                    "access_token": self.stand_in,
                    "expires_in": _STAND_IN_LIFETIME_SECONDS,
                }
            )
            told[broker.stand_in_command_env] = f"echo '{printed}'"
        return told

    def refusal(self) -> str:
        """Why the login could not be given to the turn, or nothing."""
        return str(self._failure) if self._failure is not None else ""

    async def access_token(self, refused: str | None) -> str:
        """The token to send; a refresh first, when it is due.

        Raises:
            CredentialUnavailableError: When the login cannot be read or
                refreshed; the message says what to do.
        """
        async with self._lock:
            # A login the turn could not use is not lent again: the turn fails
            # for it, whatever the tool makes of what is answered after.
            if self._failure is not None:
                raise self._failure
            stale = self._expires - time.time() < _REFRESH_MARGIN_SECONDS
            if (refused is None and not stale) or (
                refused is not None and refused != self._token
            ):
                return self._token
            if refused is not None and (self._refreshed or not self._broker.refresh):
                # The provider refuses the login it has just refreshed, or one
                # that never expires, which is refused once it is revoked.
                self._failure = CredentialUnavailableError(
                    _refresh_failed(self._tool)
                    if self._refreshed
                    else _refused(self._tool)
                )
                raise self._failure
            try:
                self.files = await refresh_login(self._tool, self._where, self._token)
            except CredentialUnavailableError as exc:
                self._failure = exc
                raise
            self._refreshed = True
            self._token, self._expires = _access(self._broker, self.files, self._tool)
            return self._token


async def refresh_login(
    tool: CliAgentInfo, where: LoginEnvironment, stale: str
) -> dict[str, bytes]:
    """Refresh the sealed login whose access token is ``stale``, and seal it.

    The tool refreshes it itself, in an environment of its own that holds
    the login in memory and is told it has expired. What it left there is
    taken back while the environment still runs, even when the refresh is
    cancelled or times out: once the tool has refreshed, the refresh token
    it replaced is spent. A login another turn or process refreshed
    meanwhile is taken as it is.

    Raises:
        CredentialUnavailableError: When the login cannot be refreshed.
    """
    broker = tool.provision.credential_broker
    assert broker is not None
    hold = await _hold_login(tool)
    try:
        files = _unsealed_login(tool)
        token, expires = _access(broker, files, tool)
        if token != stale and expires - time.time() >= _REFRESH_MARGIN_SECONDS:
            return files
        auth = tool.provision.auth
        expired = {auth: _expired(broker, files[auth])}
        # What the tool is handed, which for a JWT is not the token sealed: a
        # login left as it was handed over was not refreshed.
        handed, _expires = _account_login(broker, expired, auth)
        refreshed: list[dict[str, bytes]] = []

        async def take_back(environment: AgentEnvironment) -> None:
            if (login := await _taken_back(tool, environment, handed)) is not None:
                refreshed.append(login)

        environment = await _boot_login_environment(
            tool, where, expired, before_stop=take_back
        )
        try:
            process = await environment.run(*broker.refresh, limit=_LOGIN_LINE_LIMIT)
            try:
                await asyncio.wait_for(process.communicate(), _LOGIN_HOLD_SECONDS)
            except TimeoutError:
                await process.kill()
        finally:
            await environment.close()
        if not refreshed:
            raise CredentialUnavailableError(_refresh_failed(tool))
        return refreshed[0]
    except CredentialVaultError as exc:
        raise CredentialUnavailableError(_vault_refusal(tool, exc)) from exc
    except AgentEnvironmentError as exc:
        _LOGGER.warning("%s login refresh did not run: %s", tool.label, exc)
        raise CredentialUnavailableError(_refresh_failed(tool)) from exc
    finally:
        hold.release()


async def start_login_environment(
    tool: CliAgentInfo, where: LoginEnvironment
) -> AgentEnvironment:
    """An environment holding the brokered login, for asking the tool about
    its account (its usage), with nothing of the user's in it.

    It keeps the login to itself until it is closed, and a login the tool
    refreshed meanwhile is sealed before the environment is stopped.

    Raises:
        CredentialUnavailableError: When the login cannot be read.
    """
    broker = tool.provision.credential_broker
    assert broker is not None
    hold = await _hold_login(tool)
    try:
        files = _unsealed_login(tool)
        token, _expires = _access(broker, files, tool)

        async def take_back(environment: AgentEnvironment) -> None:
            # A failure is recorded as the login's; the usage read stands.
            with suppress(CredentialUnavailableError):
                await _taken_back(tool, environment, token)

        return await _boot_login_environment(
            tool, where, files, before_stop=take_back, on_close=hold.release
        )
    except CredentialVaultError as exc:
        hold.release()
        raise CredentialUnavailableError(_vault_refusal(tool, exc)) from exc
    except BaseException:
        hold.release()
        raise


async def _hold_login(tool: CliAgentInfo) -> HeldVaultLock:
    """Hold the sealed login against every other use of it on this device.

    Every write of the login -- a login, a refresh, a probe that refreshed --
    is made holding this, so none is sealed over another it did not see.

    Raises:
        CredentialUnavailableError: When it cannot be held; the message says
            what to do.
    """
    try:
        return await held_vault_lock(
            sealed_login_path(tool), timeout=_LOGIN_WAIT_SECONDS
        )
    except CredentialVaultError as exc:
        raise CredentialUnavailableError(_vault_refusal(tool, exc)) from exc


async def _boot_login_environment(
    tool: CliAgentInfo,
    where: LoginEnvironment,
    files: Mapping[str, bytes],
    *,
    before_stop: Callable[[AgentEnvironment], Awaitable[None]] | None = None,
    on_close: Callable[[], None] | None = None,
) -> AgentEnvironment:
    """Boot an environment with ``files`` in memory at the tool's state root.

    It mounts nothing of the device and reaches the provider's domains only.
    """
    spec = _state_root_spec(
        tool,
        EnvironmentNetwork(
            unrestricted=False,
            domains=tool.provision.api_domains,
            host_ports=(),
            local_network=False,
            nameservers=where.nameservers,
        ),
    )
    environment = await AgentEnvironment.start(
        spec,
        snapshot=str(where.snapshot),
        memory_mib=where.memory_mib,
        cpus=where.cpus,
        before_stop=before_stop,
        on_close=on_close,
    )
    try:
        root = f"{spec.home}/{tool.provision.state_root}"
        for name, data in files.items():
            await environment.write_file(f"{root}/{name}", data)
    except BaseException:
        await environment.close()
        raise
    return environment


async def _taken_back(
    tool: CliAgentInfo, environment: AgentEnvironment, given: str
) -> dict[str, bytes] | None:
    """Seal the login the tool left in ``environment`` if it refreshed it.

    Returns:
        The refreshed login, or None when the tool kept the token ``given``.

    Raises:
        CredentialUnavailableError: When the tool may have refreshed the login
            but it cannot be kept -- it cannot be read back, is no longer a
            login, or cannot be sealed. The refresh token it replaced may be
            spent, so the login is marked as failed rather than kept.
    """
    provision = tool.provision
    broker = provision.credential_broker
    assert broker is not None
    try:
        data = await environment.read_file(
            f"{environment.spec.home}/{provision.state_root}/{provision.auth}"
        )
        files = {provision.auth: data} if data is not None else {}
        token, _expires = _account_login(broker, files, provision.auth)
        if token == given:
            return None
        _seal_login(tool, files)
    except (AgentEnvironmentError, ValueError, CredentialVaultError) as exc:
        record_authentication_outcome(tool, failed=True)
        raise CredentialUnavailableError(_refresh_failed(tool)) from exc
    return files


def _label(tool: CliAgentInfo, broker: CredentialBroker) -> str:
    return f"{tool.name}:{_ACCOUNT}:{broker.format}"


def _seal_login(tool: CliAgentInfo, files: Mapping[str, bytes]) -> None:
    broker = tool.provision.credential_broker
    assert broker is not None
    seal(sealed_login_path(tool), _label(tool, broker), files)


def _unsealed_login(tool: CliAgentInfo) -> dict[str, bytes]:
    """The sealed login, or the reason a turn cannot have it.

    Raises:
        CredentialVaultError: When nothing is sealed, or it cannot be read.
    """
    broker = tool.provision.credential_broker
    assert broker is not None
    files = unseal(sealed_login_path(tool), _label(tool, broker))
    if files is None or tool.provision.auth not in files:
        raise CredentialVaultError("missing")
    return files


def masked(tool: CliAgentInfo, text: str) -> str:
    """``text``, from where the tool's login is held -- the tool, or its
    provider through it -- with the login masked in it: every value of the
    login but the fields a turn is lent (``turn_fields``), as a stand-in file
    is built, so a credential a later version of the tool keeps is masked too,
    and each part of one a JWT's dots divide. A value too short to be a
    credential is left, as it could only be words of the text. The login is
    the one sealed now: a token it replaced meanwhile is no longer one. When
    it cannot be read to mask the text by, nothing of the text is kept."""
    try:
        files = _unsealed_login(tool)
    except CredentialVaultError:
        return "(withheld: the login to mask it by could not be read)"
    broker = tool.provision.credential_broker
    assert broker is not None
    for value in sorted(_credentials(broker, files, tool.provision.auth), key=len)[
        ::-1
    ]:
        text = text.replace(value, "*" * len(value))
    return text


def _credentials(
    broker: CredentialBroker, files: Mapping[str, bytes], auth: str
) -> set[str]:
    """Every value of the login's files that a credential could be."""
    found: set[str] = set()
    for name, data in files.items():
        try:
            document = _parsed(data)
        except ValueError:  # Not JSON, or not text: it is a value whole.
            found.add(data.decode(errors="replace"))
            continue
        lent = (
            [_at(document, path) for path in broker.turn_fields] if name == auth else []
        )
        found |= _strings(document) - _strings(lent)
    found |= {part for value in found for part in value.split(".")}
    return {value for value in found if len(value) >= _SHORTEST_CREDENTIAL}


def _strings(node: Any) -> set[str]:
    """The strings in a JSON document, however deep."""
    if isinstance(node, dict):
        node = list(node.values())
    if isinstance(node, list):
        return set().union(*map(_strings, node))
    return {node} if isinstance(node, str) else set()


def _access(
    broker: CredentialBroker, files: Mapping[str, bytes], tool: CliAgentInfo
) -> tuple[str, float]:
    """The sealed login's access token and when it expires (epoch seconds)."""
    try:
        return _account_login(broker, files, tool.provision.auth)
    except ValueError as exc:
        raise CredentialVaultError("corrupt", str(exc)) from exc


def _account_login(
    broker: CredentialBroker, files: Mapping[str, bytes], auth: str
) -> tuple[str, float]:
    """The access token and expiry of an account login the tool can refresh.

    Raises:
        ValueError: When ``files`` hold no such login.
    """
    if auth not in files:
        raise ValueError("no credentials file")
    document = _parsed(files[auth])
    token = _at(document, broker.access_token)
    if not isinstance(token, str) or not token:
        raise ValueError("no access token")
    if broker.refresh_token:
        refresh = _at(document, broker.refresh_token)
        if not isinstance(refresh, str) or not refresh:
            raise ValueError("no refresh token")
    if not broker.expires_at:  # A login that lasts until it is revoked.
        return token, math.inf
    return token, _expires(broker, _at(document, broker.expires_at))


def _parsed(data: bytes) -> Any:
    """A credentials file as JSON, after the lines of comments a tool may
    write above it (Copilot's)."""
    lines = data.decode().splitlines()
    return json.loads("\n".join(x for x in lines if not x.lstrip().startswith("//")))


def _expired(broker: CredentialBroker, data: bytes) -> bytes:
    """The credentials file ``data`` as the tool reads a login that has
    expired, so that it refreshes it; everything else of it as it was."""
    document = _parsed(data)
    path = _resolved(document, broker.expires_at)
    _put(document, path, _expiry(broker, 0, _at(document, path)))
    return json.dumps(document).encode()


def _expires(broker: CredentialBroker, value: Any) -> float:
    """``value`` of the file's expiry field, in seconds since the epoch.

    Raises:
        ValueError: When it is not spelled the way the tool spells it.
    """
    if broker.expires_format == "rfc3339":
        moment = datetime.fromisoformat(value) if isinstance(value, str) else None
        if moment is not None and moment.tzinfo is not None:
            return moment.timestamp()
    elif broker.expires_format == "jwt":
        claim = _claims(value).get("exp")
        if isinstance(claim, int | float) and not isinstance(claim, bool):
            return claim
    elif isinstance(value, int | float) and not isinstance(value, bool):
        return value / 1000
    raise ValueError("no expiry")


def _expiry(broker: CredentialBroker, seconds: float, token: Any = None) -> Any:
    """``seconds`` since the epoch as the file's expiry field spells it: for
    a JWT, ``token`` claiming it, as the tool reads it (unverified)."""
    if broker.expires_format == "rfc3339":
        return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")
    if broker.expires_format == "jwt":
        header, _claimed, signature = str(token).split(".")
        claims = {**_claims(token), "exp": int(seconds)}
        return f"{header}.{_segment(claims)}.{signature}"
    return int(seconds * 1000)


def _minted(broker: CredentialBroker, login: Any) -> str:
    """A turn's stand-in: a secret of its own, shaped as the tool reads its
    access token -- for a JWT, one that claims only what the catalog names
    of the account, and an expiry the turn never reaches."""
    secret = secrets.token_urlsafe(32)
    if broker.expires_format != "jwt":
        return broker.stand_in_prefix + secret
    account = _claims(_at(login, broker.stand_in_claims_from))
    claims: dict[str, Any] = {}
    for path in broker.stand_in_claims:
        if (value := _at(account, path)) is not None:
            _put(claims, path, value)
    claims["exp"] = int(time.time() + _STAND_IN_LIFETIME_SECONDS)
    return f"{_segment({'alg': 'none', 'typ': 'JWT'})}.{_segment(claims)}.{secret}"


def _claims(token: Any) -> dict[str, Any]:
    """The claims of the JWT ``token``, unverified, or none when it is not one."""
    if not isinstance(token, str):
        return {}
    try:
        _header, payload, _signature = token.split(".")
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
    except ValueError:
        return {}
    return claims if isinstance(claims, dict) else {}


def _segment(value: Any) -> str:
    """``value`` as a JWT segment: base64url JSON, unpadded."""
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def _resolved(document: Any, path: tuple[str, ...]) -> tuple[str, ...]:
    """``path`` with each ``*`` replaced by the one key the object there has.

    Raises:
        ValueError: When a ``*`` stands where there is not exactly one key.
    """
    resolved: list[str] = []
    for step in path:
        key = step
        if step == "*":
            if not isinstance(document, dict) or len(document) != 1:
                raise ValueError(f"'{'.'.join(path)}' is not in the credentials file")
            key = next(iter(document))
        resolved.append(key)
        document = document.get(key) if isinstance(document, dict) else None
    return tuple(resolved)


def _put(document: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    for key in path[:-1]:
        document = document.setdefault(key, {})
    document[path[-1]] = value


def _at(document: Any, path: tuple[str, ...]) -> Any:
    """The value at ``path``, or None where there is none."""
    try:
        resolved = _resolved(document, path)
    except ValueError:
        return None
    for key in resolved:
        if not isinstance(document, dict):
            return None
        document = document.get(key)
    return document


def _refused(tool: CliAgentInfo) -> str:
    return t(
        "intelligences.agent_environment.tool.login_refused",
        tool=tool.label,
        command=login_command(tool.name),
    )


def _refresh_failed(tool: CliAgentInfo) -> str:
    return t(
        "intelligences.agent_environment.tool.refresh_failed",
        tool=tool.label,
        command=login_command(tool.name),
    )


def _vault_refusal(tool: CliAgentInfo, error: CredentialVaultError) -> str:
    return vault_problem(error.state, tool=tool.label, command=login_command(tool.name))
