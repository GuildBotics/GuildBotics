"""A brokered login is sealed on this device, and nothing falls back to plain."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from keyring.errors import KeyringError, KeyringLocked

from guildbotics.intelligences.agent_environment import credential_vault
from guildbotics.intelligences.agent_environment.credential_vault import (
    CredentialVaultError,
    held_vault_lock,
    seal,
    unseal,
    vault_problem,
)
from guildbotics.utils.i18n_tool import t


def vault_state(path: Path, label: str) -> str:
    """What is sealed at ``path``: its files opened, or why not."""
    try:
        return "saved" if unseal(path, label) is not None else "missing"
    except CredentialVaultError as exc:
        return exc.state


LOGIN = {".credentials.json": b'{"accessToken": "SYNTHETIC-SECRET-459"}'}
LABEL = "claude:default:claude-oauth"


@pytest.fixture
def record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(
        credential_vault,
        "get_machine_state_path",
        lambda *parts: tmp_path.joinpath("data", *parts),
    )
    return tmp_path / "data/agent_environment/claude/login.sealed"


class _Keychain:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def get_password(self, service: str, username: str) -> str | None:
        raise self.error

    def set_password(self, service: str, username: str, password: str) -> None:
        raise self.error


def test_a_sealed_login_opens_as_itself_and_is_never_plain(record: Path) -> None:
    seal(record, LABEL, LOGIN)

    assert b"SYNTHETIC-SECRET-459" not in record.read_bytes()
    assert unseal(record, LABEL) == LOGIN
    assert vault_state(record, LABEL) == "saved"
    assert not list(record.parent.glob("*.tmp"))


def test_nothing_sealed_is_missing(record: Path) -> None:
    assert unseal(record, LABEL) is None
    assert vault_state(record, LABEL) == "missing"


def test_a_record_opened_as_another_tool_or_shape_is_corrupt(record: Path) -> None:
    """What a record holds is part of it: it never opens as something else."""
    seal(record, LABEL, LOGIN)

    for label in ("codex:default:claude-oauth", "claude:default:other-format"):
        with pytest.raises(CredentialVaultError) as refused:
            unseal(record, label)
        assert refused.value.state == "corrupt"


def test_a_tampered_record_is_corrupt(record: Path) -> None:
    seal(record, LABEL, LOGIN)
    record.write_bytes(
        record.read_bytes().replace(b'"ciphertext": "', b'"ciphertext": "AA')
    )

    assert vault_state(record, LABEL) == "corrupt"
    record.write_text("not json")
    assert vault_state(record, LABEL) == "corrupt"


def test_a_lost_device_key_makes_the_login_corrupt_not_missing(record: Path) -> None:
    """Logging in again is the way back; the record is not read as absent."""
    import keyring

    seal(record, LABEL, LOGIN)
    keyring.delete_password("GuildBotics", "agent-environment-credentials")

    assert vault_state(record, LABEL) == "corrupt"


def test_one_device_key_opens_every_tools_record(record: Path, tmp_path: Path) -> None:
    other = tmp_path / "data/agent_environment/codex/login.sealed"
    seal(record, LABEL, LOGIN)
    seal(other, "codex:default:codex", LOGIN)

    assert unseal(record, LABEL) == LOGIN
    assert unseal(other, "codex:default:codex") == LOGIN


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (KeyringLocked("locked"), "locked"),
        (KeyringError("no backend"), "unavailable"),
    ],
)
def test_a_keychain_that_refuses_stops_the_login_with_its_reason(
    record: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, state: str
) -> None:
    seal(record, LABEL, LOGIN)
    working = credential_vault.system_keychain
    monkeypatch.setattr(credential_vault, "system_keychain", lambda: _Keychain(error))

    assert vault_state(record, LABEL) == state
    with pytest.raises(CredentialVaultError) as refused:
        seal(record, LABEL, {".credentials.json": b"other"})
    assert refused.value.state == state

    monkeypatch.setattr(credential_vault, "system_keychain", working)
    assert unseal(record, LABEL) == LOGIN


def test_a_record_that_cannot_be_written_is_unavailable(
    record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(path: Path, data: bytes) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(credential_vault, "atomic_write_bytes", full)

    with pytest.raises(CredentialVaultError) as refused:
        seal(record, LABEL, LOGIN)
    assert refused.value.state == "unavailable"
    assert not record.exists()


@pytest.mark.parametrize("state", ["missing", "locked", "unavailable", "corrupt"])
def test_every_state_but_saved_says_what_to_do(state: str) -> None:
    problem = vault_problem(state, tool="Claude Code", command="gb login claude")  # type: ignore[arg-type]

    assert problem == t(
        f"intelligences.agent_environment.tool.credentials_{state}",
        tool="Claude Code",
        command="gb login claude",
    )
    assert problem and vault_problem("saved", tool="x", command="y") == ""


@pytest.mark.asyncio
async def test_one_holder_at_a_time_and_a_release_lets_the_next_in(
    record: Path,
) -> None:
    record.parent.mkdir(parents=True)
    first = await held_vault_lock(record, timeout=1.0)

    with pytest.raises(CredentialVaultError) as waited:
        await held_vault_lock(record, timeout=0.2)
    assert waited.value.state == "unavailable"

    second = asyncio.create_task(held_vault_lock(record, timeout=5.0))
    await asyncio.sleep(0.2)
    assert not second.done()
    first.release()
    first.release()
    (await second).release()


@pytest.mark.asyncio
async def test_threads_of_this_process_exclude_each_other_before_the_os_lock(
    record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scheduler workers run their own event loops on their own threads; the
    process's mutex keeps them apart even where the OS lock does not."""
    # An OS lock that takes nothing, and so has nothing to give back.
    monkeypatch.setattr(credential_vault, "lock_file_nonblocking", lambda handle: None)
    monkeypatch.setattr(credential_vault, "unlock_file", lambda handle: None)
    record.parent.mkdir(parents=True)
    first = await held_vault_lock(record, timeout=1.0)

    def other_thread() -> None:
        asyncio.run(held_vault_lock(record, timeout=0.2))

    with pytest.raises(CredentialVaultError) as waited:
        await asyncio.to_thread(other_thread)
    assert waited.value.state == "unavailable"

    first.release()
    second = await asyncio.to_thread(
        lambda: asyncio.run(held_vault_lock(record, timeout=1.0))
    )
    second.release()
