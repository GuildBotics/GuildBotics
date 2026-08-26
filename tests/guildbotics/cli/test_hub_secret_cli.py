"""The hub's secret commands, driven the way a device drives them.

These commands are a wire protocol: a device pipes framed entries into them
over SSH and reads framed entries back, so what is asserted here is the bytes
on that pipe and what never appears in them.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from guildbotics.cli.hub import hub
from guildbotics.hub import host, secret_stream

WORKSPACE_ID = "0198ab00-0000-7000-8000-000000000001"


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CliRunner:
    root = tmp_path / "machine" / ".guildbotics"
    monkeypatch.setattr(host, "get_machine_root", lambda: root)
    return CliRunner()


@pytest.fixture
def hosted(runner: CliRunner, fake_keyring) -> CliRunner:
    del fake_keyring
    host.create_hub()
    host.create_workspace_repository(WORKSPACE_ID)
    return runner


def _framed(entries: list[secret_stream.SecretEntry]) -> bytes:
    payload = io.BytesIO()
    secret_stream.write_entries(payload, entries)
    return payload.getvalue()


def _offer(key: str, value: str, base: int, candidate: int, sender: str = "mac"):
    return secret_stream.SecretEntry(
        key=key,
        value=value.encode("utf-8"),
        header={
            "base_generation": base,
            "candidate_generation": candidate,
            "sender": sender,
        },
    )


def _receive(runner: CliRunner, entries: list[secret_stream.SecretEntry]) -> dict:
    result = runner.invoke(
        hub, ["secret", "receive", WORKSPACE_ID], input=_framed(entries)
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout_bytes.decode("utf-8"))


def _send(runner: CliRunner, *keys: str) -> list[secret_stream.SecretEntry]:
    arguments = ["secret", "send", WORKSPACE_ID]
    for key in keys:
        arguments += ["--key", key]
    result = runner.invoke(hub, arguments)
    assert result.exit_code == 0, result.output
    return list(secret_stream.read_entries(result.stdout_bytes))


def test_values_go_in_and_come_back_unchanged(hosted: CliRunner) -> None:
    summary = _receive(
        hosted,
        [
            _offer("A_TOKEN", "ghp-first", 0, 1),
            # A value ending in a line break is where a text pipe would differ.
            _offer("B_TOKEN", "xoxb-second\n", 0, 1),
        ],
    )

    assert summary["results"] == [
        {"key": "A_TOKEN", "status": "stored", "generation": 1},
        {"key": "B_TOKEN", "status": "stored", "generation": 1},
    ]
    assert [
        (entry.key, entry.value) for entry in _send(hosted, "A_TOKEN", "B_TOKEN")
    ] == [
        ("A_TOKEN", b"ghp-first"),
        ("B_TOKEN", b"xoxb-second\n"),
    ]


def test_the_answer_to_a_send_carries_no_value(hosted: CliRunner) -> None:
    summary = _receive(hosted, [_offer("A_TOKEN", "ghp-first", 0, 1)])

    assert "ghp-first" not in json.dumps(summary)


def test_a_losing_race_is_reported_rather_than_stored(hosted: CliRunner) -> None:
    _receive(hosted, [_offer("A_TOKEN", "from-mac", 0, 1, sender="mac")])

    summary = _receive(
        hosted, [_offer("A_TOKEN", "from-windows", 0, 1, sender="windows")]
    )

    assert summary["results"] == [{"key": "A_TOKEN", "status": "conflict"}]
    assert _send(hosted, "A_TOKEN")[0].value == b"from-mac"


def test_a_key_the_hub_does_not_hold_comes_back_as_an_empty_entry(
    hosted: CliRunner,
) -> None:
    entries = _send(hosted, "NEVER_SENT")

    assert [(entry.key, entry.value, entry.header) for entry in entries] == [
        ("NEVER_SENT", b"", {"error": "missing"})
    ]


def test_the_generation_list_names_no_value(hosted: CliRunner) -> None:
    _receive(hosted, [_offer("A_TOKEN", "ghp-first", 0, 1)])

    result = hosted.invoke(hub, ["secret", "list", WORKSPACE_ID])

    payload = json.loads(result.stdout_bytes.decode("utf-8"))
    assert payload["keys"] == {"A_TOKEN": 1}
    assert payload["secret_store"] == {"available": True, "locked": False}
    assert "ghp-first" not in result.stdout_bytes.decode("utf-8")


def test_input_that_is_not_framed_is_refused(hosted: CliRunner) -> None:
    result = hosted.invoke(hub, ["secret", "receive", WORKSPACE_ID], input=b"ghp-loose")

    assert result.exit_code != 0
    assert "ghp-loose" not in result.output
