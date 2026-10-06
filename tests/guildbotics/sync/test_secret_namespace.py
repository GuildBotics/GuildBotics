"""A device's keychain namespace survives joining a hub and synchronizing.

Both replace ``config/secrets.yml`` with another device's version. The
namespace is this device's own, so a value entered here and not yet sent stays
readable afterwards.
"""

from __future__ import annotations

from pathlib import Path

from guildbotics.sync import enrollment
from guildbotics.utils.secret_store import KeyringSecretStore, SecretKeyStatus
from guildbotics.utils.workspace_sync_port import ChangeSet
from guildbotics.workspace.identity import new_uuid7
from tests.guildbotics.sync.conftest import Device

SECRETS_INDEX = "config/secrets.yml"


def _store(root: Path) -> KeyringSecretStore:
    return KeyringSecretStore(root / ".guildbotics" / "config")


def _workspace(root: Path) -> Path:
    (root / ".guildbotics" / "state").mkdir(parents=True)
    return root


def _announce_index(device: Device) -> None:
    device.manager.shared_state_changed(
        ChangeSet(change_id=new_uuid7(), operation="update", paths=(SECRETS_INDEX,))
    )


def test_an_unsent_value_is_still_readable_after_joining(
    fake_keyring, tmp_path: Path, hub: Path
) -> None:
    registered = _workspace(tmp_path / "mac")
    _store(registered).set("MAC_TOKEN", "from-mac")
    enrollment.enroll(str(hub), registered)
    joining = _workspace(tmp_path / "windows")
    store = _store(joining)
    store.set("WINDOWS_TOKEN", "typed-here")

    result = enrollment.enroll(str(hub), joining, record_rejection=lambda **_: None)

    assert SECRETS_INDEX in result.adopted
    assert store.get("WINDOWS_TOKEN") == "typed-here"
    assert store.key_state("WINDOWS_TOKEN").status is SecretKeyStatus.PENDING_SEND
    assert store.key_state("MAC_TOKEN").status is SecretKeyStatus.MISSING


def test_an_unsent_value_is_still_readable_after_synchronizing(
    fake_keyring, first: Device, second: Device
) -> None:
    _store(first.root).set("MAC_TOKEN", "from-mac")
    _announce_index(first)
    first.manager.synchronize()
    store = _store(second.root)
    store.set("WINDOWS_TOKEN", "typed-here")
    _announce_index(second)

    second.manager.synchronize()

    assert second.read(SECRETS_INDEX) == first.read(SECRETS_INDEX)
    assert store.get("WINDOWS_TOKEN") == "typed-here"
    assert store.key_state("WINDOWS_TOKEN").status is SecretKeyStatus.PENDING_SEND
    assert store.key_state("MAC_TOKEN").status is SecretKeyStatus.MISSING
