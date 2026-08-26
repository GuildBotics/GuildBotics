"""The framing that carries secret values between a device and its hub.

Secret values travel as the standard input and standard output of one SSH
command and nowhere else: no relay file, no Git object, no temporary file. That
leaves one problem to solve here -- how several values share one stream without
any of them being altered on the way.

Each entry is a JSON header line followed by exactly the bytes it declares. The
header names the key and the generations; the value is never in it, so anything
that reads, logs, or reports a header is safe by construction. The value is
carried as raw bytes with a length rather than as text, because the stream
crosses a Windows machine at one end or the other and universal newline
translation would silently rewrite a value that ends in a line break.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import IO, Any

#: The largest single value the framing accepts. It bounds one entry, not the
#: stream: the reader is handed everything the other side wrote, because the
#: other side is an SSH session that has already authenticated. What the bound
#: is for is a stream that is not a secret stream at all -- one entry claiming
#: a length nothing could satisfy is refused before its value is looked for.
#: Real values are bounded far below it by the OS keychains themselves.
MAX_VALUE_BYTES = 1 * 1024 * 1024
#: The largest header line accepted, for the same reason.
MAX_HEADER_BYTES = 8 * 1024


class SecretStreamError(ValueError):
    """Raised when a secret stream is not shaped the way framing requires.

    The message names the framing problem and never the bytes that caused it:
    an exception message is exactly the kind of place a value must not reach.
    """


@dataclass(frozen=True)
class SecretEntry:
    """One key on the wire.

    Attributes:
        key (str): The logical key name.
        value (bytes): The secret value, or empty when the header carries an
            error instead.
        header (dict[str, Any]): The non-secret fields sent alongside it --
            generations on the way to the hub, results on the way back.
    """

    key: str
    value: bytes = b""
    header: dict[str, Any] = field(default_factory=dict)


def write_entry(
    stream: IO[bytes], key: str, header: Mapping[str, Any], value: bytes = b""
) -> None:
    """Write one framed entry to a binary stream."""
    payload: dict[str, Any] = {**header, "key": key, "length": len(value)}
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(line) > MAX_HEADER_BYTES:
        raise SecretStreamError("a secret stream header is too long")
    if len(value) > MAX_VALUE_BYTES:
        raise SecretStreamError(f"the value for {key} is too large to transfer")
    stream.write(line + b"\n" + value)


def write_entries(stream: IO[bytes], entries: list[SecretEntry]) -> None:
    """Write a whole stream of framed entries and flush it."""
    for entry in entries:
        write_entry(stream, entry.key, entry.header, entry.value)
    stream.flush()


def read_entries(data: bytes) -> Iterator[SecretEntry]:
    """Read framed entries out of a complete stream.

    Args:
        data (bytes): Everything the other side wrote.

    Yields:
        SecretEntry: One entry per frame, in the order they were written.

    Raises:
        SecretStreamError: When the stream is not framed as written above.
    """
    offset = 0
    while offset < len(data):
        end = data.find(b"\n", offset, offset + MAX_HEADER_BYTES + 1)
        if end < 0:
            raise SecretStreamError("a secret stream entry has no header")
        header = _header(data[offset:end])
        length = header.pop("length")
        key = header.pop("key")
        start = end + 1
        if start + length > len(data):
            raise SecretStreamError(f"the stream ended inside the value for {key}")
        yield SecretEntry(key=key, value=data[start : start + length], header=header)
        offset = start + length


def _header(line: bytes) -> dict[str, Any]:
    """Parse one header line into its fields, or refuse the stream."""
    try:
        header = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        # The text is deliberately dropped: a stream that is not framed the way
        # this module writes it may well be a value that arrived unframed.
        raise SecretStreamError("a secret stream header is not readable") from exc
    if not isinstance(header, dict):
        raise SecretStreamError("a secret stream header is not an object")
    key = header.get("key")
    length = header.get("length")
    if not isinstance(key, str) or not key:
        raise SecretStreamError("a secret stream entry names no key")
    if isinstance(length, bool) or not isinstance(length, int) or length < 0:
        raise SecretStreamError(f"the entry for {key} declares no value length")
    if length > MAX_VALUE_BYTES:
        raise SecretStreamError(f"the entry for {key} declares too large a value")
    return header
