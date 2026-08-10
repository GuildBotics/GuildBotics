"""Platform keychain adapters used by workspace secret storage."""

from __future__ import annotations

import contextlib
import sys
from typing import Any, Protocol


class SecretStoreError(Exception):
    """Base error raised when an OS keychain operation fails."""


class SecretValueTooLargeError(SecretStoreError):
    """A secret cannot fit in the selected OS keychain."""

    def __init__(self, key: str, size: int, limit: int):
        super().__init__(f"{key} requires {size} bytes; the limit is {limit} bytes")
        self.key = key
        self.size = size
        self.limit = limit


class Keychain(Protocol):
    """Minimal password-store operations required by ``KeyringSecretStore``."""

    def validate_password(self, username: str, password: str) -> None:
        """Validate a value before any write in a batch."""

    def get_password(self, service: str, username: str) -> str | None:
        """Return one password, or ``None`` when it is absent."""

    def set_password(self, service: str, username: str, password: str) -> None:
        """Store one password."""

    def delete_password(self, service: str, username: str) -> None:
        """Delete one password, doing nothing when it is absent."""


class PythonKeyringAdapter:
    """Adapter for the standard Python keyring backends used off Windows."""

    def __init__(self, backend: Any):
        self._backend = backend

    def validate_password(self, username: str, password: str) -> None:
        del username, password

    def get_password(self, service: str, username: str) -> str | None:
        return self._backend.get_password(service, username)

    def set_password(self, service: str, username: str, password: str) -> None:
        self._backend.set_password(service, username, password)

    def delete_password(self, service: str, username: str) -> None:
        from keyring.errors import PasswordDeleteError

        with contextlib.suppress(PasswordDeleteError):
            self._backend.delete_password(service, username)


def system_keychain() -> Keychain:
    """Return the adapter for the active OS keychain backend."""
    import keyring

    backend = keyring.get_keyring()
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring

        if isinstance(backend, WinVaultKeyring):
            from guildbotics.utils.windows_credentials import (
                WindowsCredentialManager,
            )

            return WindowsCredentialManager()
    return PythonKeyringAdapter(backend)
