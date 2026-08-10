"""UTF-8 Windows Credential Manager adapter.

Python keyring writes strings as UTF-16 on Windows, halving the effective
2,560-byte CredentialBlob capacity for ASCII-heavy values such as PEM keys.
GuildBotics uses its own target namespace and stores the opaque blob as UTF-8.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any, Protocol

from guildbotics.utils.keychain import (
    SecretStoreError,
    SecretValueTooLargeError,
)

CREDENTIAL_BLOB_LIMIT = 5 * 512
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_ENTERPRISE = 3
_ERROR_NOT_FOUND = 1168


def _windows_library(name: str) -> Any:
    return vars(ctypes)["WinDLL"](name, use_last_error=True)


def _windows_last_error() -> int:
    return int(vars(ctypes)["get_last_error"]())


def _windows_error(error: int) -> OSError:
    return vars(ctypes)["WinError"](error)


class _CredentialApi(Protocol):
    def read(self, target: str) -> bytes | None:
        """Read an opaque credential blob."""

    def write(self, target: str, username: str, blob: bytes) -> None:
        """Write an opaque credential blob."""

    def delete(self, target: str) -> None:
        """Delete a credential when present."""


class _CredentialAttribute(ctypes.Structure):
    _fields_ = [
        ("Keyword", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("ValueSize", wintypes.DWORD),
        ("Value", ctypes.POINTER(wintypes.BYTE)),
    ]


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(wintypes.BYTE)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.POINTER(_CredentialAttribute)),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class _WindowsCredentialApi:
    def __init__(self) -> None:
        library = _windows_library("Advapi32.dll")
        self._write = library.CredWriteW
        self._write.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
        self._write.restype = wintypes.BOOL
        self._read = library.CredReadW
        self._read.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_Credential)),
        ]
        self._read.restype = wintypes.BOOL
        self._delete = library.CredDeleteW
        self._delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._delete.restype = wintypes.BOOL
        self._free = library.CredFree
        self._free.argtypes = [ctypes.c_void_p]
        self._free.restype = None

    def read(self, target: str) -> bytes | None:
        credential = ctypes.POINTER(_Credential)()
        if not self._read(
            target,
            _CRED_TYPE_GENERIC,
            0,
            ctypes.byref(credential),
        ):
            error = _windows_last_error()
            if error == _ERROR_NOT_FOUND:
                return None
            raise _windows_error(error)
        try:
            value = credential.contents
            if not value.CredentialBlobSize:
                return b""
            return ctypes.string_at(
                value.CredentialBlob,
                value.CredentialBlobSize,
            )
        finally:
            self._free(credential)

    def write(self, target: str, username: str, blob: bytes) -> None:
        buffer = (wintypes.BYTE * len(blob)).from_buffer_copy(blob) if blob else None
        credential = _Credential(
            Type=_CRED_TYPE_GENERIC,
            TargetName=target,
            Comment="Stored by GuildBotics",
            CredentialBlobSize=len(blob),
            CredentialBlob=(
                ctypes.cast(buffer, ctypes.POINTER(wintypes.BYTE))
                if buffer is not None
                else None
            ),
            Persist=_CRED_PERSIST_ENTERPRISE,
            UserName=username,
        )
        if not self._write(ctypes.byref(credential), 0):
            raise _windows_error(_windows_last_error())

    def delete(self, target: str) -> None:
        if self._delete(target, _CRED_TYPE_GENERIC, 0):
            return
        error = _windows_last_error()
        if error != _ERROR_NOT_FOUND:
            raise _windows_error(error)


class WindowsCredentialManager:
    """Store GuildBotics secrets as UTF-8 Credential Manager blobs."""

    def __init__(self, api: _CredentialApi | None = None):
        self._api = api or _WindowsCredentialApi()

    def validate_password(self, username: str, password: str) -> None:
        size = len(password.encode("utf-8"))
        if size > CREDENTIAL_BLOB_LIMIT:
            raise SecretValueTooLargeError(username, size, CREDENTIAL_BLOB_LIMIT)

    def get_password(self, service: str, username: str) -> str | None:
        try:
            blob = self._api.read(self._target(service, username))
            return None if blob is None else blob.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretStoreError(f"credential {username} is not valid UTF-8") from exc
        except OSError as exc:
            raise SecretStoreError(str(exc)) from exc

    def set_password(self, service: str, username: str, password: str) -> None:
        self.validate_password(username, password)
        try:
            self._api.write(
                self._target(service, username),
                username,
                password.encode("utf-8"),
            )
        except OSError as exc:
            raise SecretStoreError(str(exc)) from exc

    def delete_password(self, service: str, username: str) -> None:
        try:
            self._api.delete(self._target(service, username))
        except OSError as exc:
            raise SecretStoreError(str(exc)) from exc

    @staticmethod
    def _target(service: str, username: str) -> str:
        return f"{service}/utf8/{username}"
