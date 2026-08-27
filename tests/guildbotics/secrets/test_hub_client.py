"""The device's half of the wire, checked against the hub's own commands.

The remote client and the hub CLI are two ends of one protocol, so they are
tested together: the client's arguments are handed to the real commands, and
what those write is what the client parses back.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from guildbotics.cli.hub import hub
from guildbotics.hub import host, secret_stream
from guildbotics.hub.connection import HubEndpoint
from guildbotics.secrets.hub_client import (
    LocalHubSecretClient,
    RemoteHubSecretClient,
    SecretOffer,
)

WORKSPACE_ID = "0198ab00-0000-7000-8000-000000000001"


@pytest.fixture
def hub_machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_keyring):
    del fake_keyring
    root = tmp_path / "machine" / ".guildbotics"
    monkeypatch.setattr(host, "get_machine_root", lambda: root)
    host.create_hub()
    host.create_workspace_repository(WORKSPACE_ID)


@pytest.fixture
def remote(hub_machine, monkeypatch: pytest.MonkeyPatch) -> RemoteHubSecretClient:
    """A remote client whose SSH call runs the hub's own commands here.

    The hub machine really is another machine, but the only thing that differs
    is which process the arguments reach. Running them in this one is what lets
    the two halves of the protocol be checked against each other at all.
    """
    del hub_machine
    runner = CliRunner()

    def run(endpoint: HubEndpoint, arguments: list[str], payload: bytes = b"") -> bytes:
        del endpoint
        assert arguments[:1] == ["secret"]
        result = runner.invoke(hub, arguments, input=io.BytesIO(payload))
        assert result.exit_code == 0, result.output
        return result.stdout_bytes

    monkeypatch.setattr("guildbotics.secrets.hub_client.run_hub_stream", run)
    return RemoteHubSecretClient(HubEndpoint(host="hub.local"), WORKSPACE_ID)


def test_a_value_crosses_the_wire_unchanged(remote: RemoteHubSecretClient) -> None:
    sent = remote.send(
        [SecretOffer(key="A_TOKEN", candidate=1, value="ghp-\n\r first")]
    )

    assert [(result.key, result.status, result.generation) for result in sent] == [
        ("A_TOKEN", "stored", 1)
    ]
    fetched = remote.fetch(["A_TOKEN"])
    assert [(result.key, result.status, result.value) for result in fetched] == [
        ("A_TOKEN", "sent", "ghp-\n\r first")
    ]


def test_the_index_reports_generations_and_the_hub_store(
    remote: RemoteHubSecretClient,
) -> None:
    remote.send([SecretOffer(key="A_TOKEN", candidate=1, value="ghp")])

    index = remote.index()

    assert index.generations == {"A_TOKEN": 1}
    assert (index.available, index.locked) == (True, False)


def test_a_race_lost_at_the_hub_is_reported_to_the_device(
    remote: RemoteHubSecretClient,
) -> None:
    remote.send([SecretOffer(key="A_TOKEN", candidate=1, value="mine")])

    sent = remote.send([SecretOffer(key="A_TOKEN", candidate=1, value="theirs")])

    assert [(result.key, result.status) for result in sent] == [("A_TOKEN", "conflict")]


def test_a_key_the_hub_lacks_is_reported_rather_than_guessed(
    remote: RemoteHubSecretClient,
) -> None:
    assert [(r.key, r.status, r.value) for r in remote.fetch(["NEVER_SENT"])] == [
        ("NEVER_SENT", "missing", None)
    ]


def test_a_key_name_a_shell_would_read_never_reaches_the_command_line(
    remote: RemoteHubSecretClient,
) -> None:
    """OpenSSH hands the far side one command line for a shell to split, so a
    name carrying whitespace or a metacharacter is refused before it is
    written rather than when the hub receives it."""
    refused = remote.fetch(["A_TOKEN; rm -rf ~", "with space", "A_TOKEN"])

    assert [(result.key, result.status) for result in refused[:2]] == [
        ("A_TOKEN; rm -rf ~", "invalid"),
        ("with space", "invalid"),
    ]
    assert refused[2].key == "A_TOKEN"


def test_one_unusable_name_does_not_cost_the_batch(
    remote: RemoteHubSecretClient,
) -> None:
    sent = remote.send(
        [
            SecretOffer(key="bad name", candidate=1, value="x"),
            SecretOffer(key="A_TOKEN", candidate=1, value="ghp"),
        ]
    )

    assert {result.key: result.status for result in sent} == {
        "bad name": "invalid",
        "A_TOKEN": "stored",
    }


def _answering(monkeypatch: pytest.MonkeyPatch, output: bytes) -> RemoteHubSecretClient:
    """A client whose "hub" answers with exactly ``output``.

    The real hub's boundary refuses these answers, so producing them takes a
    corrupt or hand-made far side -- which is exactly what the client's own
    check exists for.
    """

    def run(endpoint: HubEndpoint, arguments: list[str], payload: bytes = b"") -> bytes:
        del endpoint, arguments, payload
        return output

    monkeypatch.setattr("guildbotics.secrets.hub_client.run_hub_stream", run)
    return RemoteHubSecretClient(HubEndpoint(host="hub.local"), WORKSPACE_ID)


def test_a_generation_no_hub_could_hold_is_not_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Published generations start at 1. A fetched value labelled 0 or a
    negative number would put a number into the workspace's records that no
    legitimate send can produce, so it is refused as invalid instead."""
    stream = io.BytesIO()
    secret_stream.write_entries(
        stream,
        [
            secret_stream.SecretEntry(
                key="A_TOKEN", value=b"ghp", header={"generation": -1}
            ),
            secret_stream.SecretEntry(
                key="B_TOKEN", value=b"xoxb", header={"generation": 0}
            ),
        ],
    )
    client = _answering(monkeypatch, stream.getvalue())

    fetched = client.fetch(["A_TOKEN", "B_TOKEN"])

    assert [(r.key, r.status, r.value) for r in fetched] == [
        ("A_TOKEN", "invalid", None),
        ("B_TOKEN", "invalid", None),
    ]


def test_the_index_drops_generations_no_hub_could_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        {
            "workspace_id": WORKSPACE_ID,
            "keys": {"A_TOKEN": -1, "B_TOKEN": 0, "C_TOKEN": 2},
            "secret_store": {"available": True, "locked": False},
        }
    ).encode("utf-8")
    client = _answering(monkeypatch, payload)

    assert client.index().generations == {"C_TOKEN": 2}


def test_a_hub_on_this_machine_answers_the_same_way(hub_machine) -> None:
    """A device that hosts its own hub takes the same path with no SSH at all."""
    del hub_machine
    client = LocalHubSecretClient(WORKSPACE_ID)

    client.send([SecretOffer(key="A_TOKEN", candidate=1, value="ghp-first")])

    assert client.index().generations == {"A_TOKEN": 1}
    assert [(r.key, r.status, r.value) for r in client.fetch(["A_TOKEN"])] == [
        ("A_TOKEN", "sent", "ghp-first")
    ]
