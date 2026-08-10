from __future__ import annotations

import pytest

from guildbotics.utils.keychain import (
    SecretStoreError,
    SecretValueTooLargeError,
)
from guildbotics.utils.windows_credentials import (
    CREDENTIAL_BLOB_LIMIT,
    WindowsCredentialManager,
)


class FakeCredentialApi:
    def __init__(self):
        self.values: dict[str, bytes] = {}
        self.usernames: dict[str, str] = {}

    def read(self, target: str) -> bytes | None:
        return self.values.get(target)

    def write(self, target: str, username: str, blob: bytes) -> None:
        self.values[target] = blob
        self.usernames[target] = username

    def delete(self, target: str) -> None:
        self.values.pop(target, None)
        self.usernames.pop(target, None)


def test_roundtrips_large_ascii_pem_as_utf8():
    api = FakeCredentialApi()
    manager = WindowsCredentialManager(api)
    pem = "x" * 1679

    manager.set_password("GuildBotics/store", "ALICE_GITHUB_PRIVATE_KEY", pem)

    target = "GuildBotics/store/utf8/ALICE_GITHUB_PRIVATE_KEY"
    assert api.values[target] == pem.encode("utf-8")
    assert len(api.values[target]) == 1679
    assert manager.get_password("GuildBotics/store", "ALICE_GITHUB_PRIVATE_KEY") == pem


def test_utf8_size_limit_is_measured_in_encoded_bytes():
    api = FakeCredentialApi()
    manager = WindowsCredentialManager(api)
    value = "あ" * 854

    with pytest.raises(SecretValueTooLargeError) as raised:
        manager.set_password("GuildBotics/store", "TOO_LARGE", value)

    assert raised.value.key == "TOO_LARGE"
    assert raised.value.size == 2562
    assert raised.value.limit == CREDENTIAL_BLOB_LIMIT
    assert api.values == {}


def test_limit_boundary_is_accepted():
    api = FakeCredentialApi()
    manager = WindowsCredentialManager(api)
    value = "x" * CREDENTIAL_BLOB_LIMIT

    manager.set_password("GuildBotics/store", "AT_LIMIT", value)

    assert manager.get_password("GuildBotics/store", "AT_LIMIT") == value


def test_invalid_utf8_blob_raises_store_error_without_blob_contents():
    api = FakeCredentialApi()
    manager = WindowsCredentialManager(api)
    target = "GuildBotics/store/utf8/BROKEN"
    api.values[target] = b"\xffsecret"

    with pytest.raises(SecretStoreError) as raised:
        manager.get_password("GuildBotics/store", "BROKEN")

    assert "BROKEN" in str(raised.value)
    assert "secret" not in str(raised.value)


def test_delete_missing_credential_is_a_noop():
    manager = WindowsCredentialManager(FakeCredentialApi())

    manager.delete_password("GuildBotics/store", "MISSING")
