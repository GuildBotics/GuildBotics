"""The framing that carries secret values, and what it refuses.

The value has to arrive byte for byte -- a token with a trailing newline is a
different token -- and nothing that goes wrong may quote it back.
"""

from __future__ import annotations

import io

import pytest

from guildbotics.hub.secret_stream import (
    MAX_VALUE_BYTES,
    SecretEntry,
    SecretStreamError,
    read_entries,
    write_entries,
)


def _round_trip(entries: list[SecretEntry]) -> list[SecretEntry]:
    stream = io.BytesIO()
    write_entries(stream, entries)
    return list(read_entries(stream.getvalue()))


def test_values_survive_framing_exactly():
    entries = [
        SecretEntry(key="A_TOKEN", value=b"ghp-plain", header={"candidate": 2}),
        # Newlines and a trailing carriage return are what a naive text stream
        # rewrites; a value that reads back differently is a broken credential.
        SecretEntry(key="B_KEY", value=b"-----BEGIN----\r\nline\n\n", header={}),
        SecretEntry(key="C_KEY", value="鍵\n".encode(), header={}),
    ]

    read = _round_trip(entries)

    assert [entry.key for entry in read] == ["A_TOKEN", "B_KEY", "C_KEY"]
    assert [entry.value for entry in read] == [entry.value for entry in entries]
    assert read[0].header == {"candidate": 2}


def test_an_empty_value_is_still_an_entry():
    """The hub answers "no value for this key" as a framed entry with none."""
    read = _round_trip([SecretEntry(key="A_TOKEN", header={"error": "missing"})])

    assert read[0].value == b""
    assert read[0].header == {"error": "missing"}


def test_unframed_input_is_refused_without_quoting_it():
    secret = "ghp-000111222"

    with pytest.raises(SecretStreamError) as error:
        list(read_entries(secret.encode()))

    assert secret not in str(error.value)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"key": "A", "length": 5}\nabc',
        b'{"key": "", "length": 0}\n',
        b'{"length": 0}\n',
        b'{"key": "A"}\n',
        b'{"key": "A", "length": -1}\n',
        b'["not", "an", "object"]\n',
    ],
)
def test_malformed_frames_are_refused(payload: bytes):
    with pytest.raises(SecretStreamError):
        list(read_entries(payload))


def test_an_implausible_length_is_refused_before_anything_is_read():
    header = b'{"key": "A", "length": %d}\n' % (MAX_VALUE_BYTES + 1)

    with pytest.raises(SecretStreamError):
        list(read_entries(header))
