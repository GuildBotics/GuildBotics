"""Directory-relative Windows opens; reparse points are never traversed.

NtCreateFile's RootDirectory anchors each child to an already opened handle.
FILE_OPEN_REPARSE_POINT opens the reparse object itself; its attributes are
checked before another component is opened or created beneath it.
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import IO, Any

from guildbotics.utils.safe_paths import UnsafePathError, t


class _ReparsePoint(Exception):
    pass


# ctypes exposes these APIs only on Windows.
_WINDOWS: Any = ctypes


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.USHORT),
        ("maximum", wintypes.USHORT),
        ("buffer", wintypes.LPWSTR),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.ULONG),
        ("root", wintypes.HANDLE),
        ("name", ctypes.POINTER(_UnicodeString)),
        ("attributes", wintypes.ULONG),
        ("security", wintypes.LPVOID),
        ("quality", wintypes.LPVOID),
    ]


class _IoStatus(ctypes.Structure):
    _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]


class _FileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("creation", wintypes.FILETIME),
        ("access", wintypes.FILETIME),
        ("write", wintypes.FILETIME),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


class _FileIdInformation(ctypes.Structure):
    _fields_ = [("volume", ctypes.c_ulonglong), ("identifier", ctypes.c_ubyte * 16)]


def inspect_windows_path(
    path: Path,
    create: bool,
    directory: bool,
    consume: Callable[[int], None] | None = None,
    *,
    open_file: bool = False,
    stable: bool = False,
    link_as_missing: bool = False,
) -> tuple[tuple[tuple[int, int], ...], tuple[str, ...]]:
    """Inspect/create one child at a time and return its ancestry identities."""
    kernel = _WINDOWS.WinDLL("kernel32", use_last_error=True)
    native = _WINDOWS.WinDLL("ntdll")
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FileInformation),
    ]
    kernel.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    native.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatus),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
    ]
    native.NtCreateFile.restype = wintypes.LONG
    native.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
    native.RtlNtStatusToDosError.restype = wintypes.ULONG
    handles: list[int] = []
    identities: list[tuple[int, int]] = []

    def identity(handle: int, require_directory: bool) -> tuple[int, int]:
        info = _FileInformation()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise _WINDOWS.WinError(_WINDOWS.get_last_error())
        if info.attributes & 0x400:
            raise _ReparsePoint
        if require_directory and not info.attributes & 0x10:
            raise NotADirectoryError(str(path))
        file_id = _FileIdInformation()
        if not kernel.GetFileInformationByHandleEx(
            handle, 18, ctypes.byref(file_id), ctypes.sizeof(file_id)
        ):
            raise _WINDOWS.WinError(_WINDOWS.get_last_error())
        inode = int.from_bytes(bytes(file_id.identifier), "little")
        if not inode:
            raise OSError("The filesystem supplies no object identity")
        return file_id.volume, inode

    def child(
        parent: int,
        name: str,
        make: bool,
        require_directory: bool,
        leaf_file: bool = False,
    ) -> int:
        buffer = ctypes.create_unicode_buffer(name)
        length = len(name.encode("utf-16-le"))
        text = _UnicodeString(length, length + 2, ctypes.cast(buffer, wintypes.LPWSTR))
        attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes),
            parent,
            ctypes.pointer(text),
            0x40,
            None,
            None,
        )
        handle = wintypes.HANDLE()
        status = _IoStatus()
        result = native.NtCreateFile(
            ctypes.byref(handle),
            0x100083
            if leaf_file
            else 0x100081
            if consume and not require_directory
            else 0x100080,
            ctypes.byref(attributes),
            ctypes.byref(status),
            None,
            (0x80 if leaf_file else 0x10) if make else 0,
            3 if stable else 7,
            3 if make else 1,
            0x200020 | (1 if require_directory else 0x40 if leaf_file else 0),
            None,
            0,
        )
        if result < 0:
            raise _WINDOWS.WinError(native.RtlNtStatusToDosError(result))
        assert handle.value is not None
        return handle.value

    try:
        root = kernel.CreateFileW(
            "\\\\?\\" + path.anchor,
            0x100080,
            3 if stable else 7,
            None,
            3,
            0x02200000,
            None,
        )
        if root == ctypes.c_void_p(-1).value:
            raise _WINDOWS.WinError(_WINDOWS.get_last_error())
        handles.append(root)
        identities.append(identity(root, True))
        parent = root
        parts = path.parts[1:]
        for index, name in enumerate(parts):
            require_directory = directory or index < len(parts) - 1
            leaf_file = open_file and index == len(parts) - 1
            try:
                handle = child(parent, name, leaf_file, require_directory, leaf_file)
            except FileNotFoundError:
                if not create:
                    return tuple(identities), parts[index:]
                handle = child(parent, name, True, True)
            handles.append(handle)
            try:
                identities.append(identity(handle, require_directory))
            except _ReparsePoint:
                if link_as_missing:
                    return tuple(identities), parts[index:]
                raise UnsafePathError(t("safe_paths.link", path=path)) from None
            parent = handle
        if consume is not None:
            consume(parent)
        return tuple(identities), ()
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


def consume_windows_file(path: Path, consume_chunk: Callable[[bytes], None]) -> None:
    """Stream through the same no-follow leaf handle."""
    kernel = _WINDOWS.WinDLL("kernel32", use_last_error=True)
    kernel.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]

    def consume(handle: int) -> None:
        buffer = ctypes.create_string_buffer(65536)
        count = wintypes.DWORD()
        while True:
            if not kernel.ReadFile(
                handle, buffer, len(buffer), ctypes.byref(count), None
            ):
                raise _WINDOWS.WinError(_WINDOWS.get_last_error())
            if not count.value:
                return
            consume_chunk(buffer.raw[: count.value])

    _, missing = inspect_windows_path(path, False, False, consume)
    if missing:
        raise FileNotFoundError(str(path))


def open_windows_file(path: Path) -> IO[str]:
    """Return a Python file for the regular leaf opened through NT parents."""
    import msvcrt

    windows_io: Any = msvcrt

    kernel = _WINDOWS.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    descriptors: list[int] = []

    def consume(handle: int) -> None:
        duplicate = wintypes.HANDLE()
        process = kernel.GetCurrentProcess()
        if not kernel.DuplicateHandle(
            process, handle, process, ctypes.byref(duplicate), 0, False, 2
        ):
            raise _WINDOWS.WinError(_WINDOWS.get_last_error())
        assert duplicate.value is not None
        descriptors.append(windows_io.open_osfhandle(duplicate.value, os.O_RDWR))

    _, absent = inspect_windows_path(path, False, False, consume, open_file=True)
    if absent:
        raise FileNotFoundError(str(path))
    return os.fdopen(descriptors[0], "r+", encoding="utf-8")
