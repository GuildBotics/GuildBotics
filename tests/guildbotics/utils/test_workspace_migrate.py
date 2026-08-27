import json
from pathlib import Path

import pytest

from guildbotics.utils.fileio import load_yaml_dict
from guildbotics.utils.secret_store import KeyringSecretStore
from guildbotics.utils.workspace_migrate import migrate_workspace
from guildbotics.utils.workspace_state import read_active_workspace


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_migrate_copies_data_without_touching_source(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    source = tmp_path / "repo"
    (source / ".git").mkdir(parents=True)
    _write(source / "src" / "app.py", "print('hi')\n")
    _write(
        source / ".guildbotics" / "config" / "team" / "project.yml", "language: en\n"
    )
    _write(
        source / ".guildbotics" / "data" / "documents" / "team" / "doc" / "body.md",
        "hello\n",
    )
    _write(
        source
        / ".guildbotics"
        / "data"
        / "chat_state"
        / "slack"
        / "aiko"
        / "threads"
        / "C1"
        / "1.json",
        '{"thread_topic": "t", "messages": [{"message_ts": "1", "text": "hi"}]}\n',
    )
    _write(source / ".guildbotics" / "data" / "run" / "diagnostics.jsonl", "{}\n")
    dest = tmp_path / "workspace"

    result = migrate_workspace(source, dest)

    assert result.destination == dest.resolve()
    assert (source / "src" / "app.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert (source / ".guildbotics" / "config" / "team" / "project.yml").exists()
    assert (dest / ".guildbotics" / "config" / "team" / "project.yml").read_text(
        encoding="utf-8"
    ) == "language: en\n"
    assert (
        dest / ".guildbotics" / "state" / "documents" / "team" / "doc" / "body.md"
    ).exists()
    control = (
        dest
        / ".guildbotics"
        / "state"
        / "chat_state"
        / "slack"
        / "aiko"
        / "threads"
        / "C1"
        / "1.json"
    )
    cache = (
        dest
        / ".guildbotics"
        / "local"
        / "chat-cache"
        / "slack"
        / "aiko"
        / "threads"
        / "C1"
        / "1.json"
    )
    assert "messages" not in control.read_text(encoding="utf-8")
    assert "hi" in cache.read_text(encoding="utf-8")
    assert (dest / ".guildbotics" / "local" / "run" / "diagnostics.jsonl").exists()
    active = read_active_workspace()
    assert active is not None
    assert active.workspace == dest.resolve()


def test_migrate_refuses_to_overwrite_destination(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    source = tmp_path / "repo"
    dest = tmp_path / "workspace"
    _write(source / ".guildbotics" / "config" / "a.yml", "a: 1\n")
    _write(dest / ".guildbotics" / "config" / "a.yml", "a: 2\n")

    with pytest.raises(FileExistsError):
        migrate_workspace(source, dest)


def test_migrate_converts_old_secrets_index_to_generation_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    source = tmp_path / "repo"
    _write(
        source / ".guildbotics" / "config" / "secrets.yml",
        "backend: keyring\nstore_id: abc123\nkeys:\n- OPENAI_API_KEY\n"
        "- AIKO_GITHUB_ACCESS_TOKEN\n",
    )
    dest = tmp_path / "workspace"

    migrate_workspace(source, dest)

    store = KeyringSecretStore(dest / ".guildbotics" / "config")
    assert sorted(store.keys()) == ["AIKO_GITHUB_ACCESS_TOKEN", "OPENAI_API_KEY"]
    # Migrated keys start unshared: no hub has been given these values yet.
    assert store.shared_generation("OPENAI_API_KEY") == 0
    # This device already holds the keychain values, so its device-local
    # generation record must exist after migration.
    assert store.local_generation("OPENAI_API_KEY") == 0
    assert store.local_generation("AIKO_GITHUB_ACCESS_TOKEN") == 0
    assert (dest / ".guildbotics" / "local" / "secrets.json").is_file()
    index = load_yaml_dict(dest / ".guildbotics" / "config" / "secrets.yml")
    # The keychain namespace must survive so existing values stay readable.
    assert index["store_id"] == "abc123"
    assert "backend" not in index


def test_migrate_extracts_domain_events_into_shared_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    source = tmp_path / "repo"
    _write(
        source / ".guildbotics" / "config" / "team" / "project.yml", "language: en\n"
    )
    diagnostics = "\n".join(
        [
            json.dumps(
                {
                    "kind": "event",
                    "type": "github.push",
                    "timestamp": "2026-07-10T01:00:00Z",
                    "trace_id": "trace-1",
                    "person_id": "aiko",
                    "payload": {"ref": "refs/heads/main"},
                }
            ),
            json.dumps(
                {
                    "kind": "event",
                    "type": "session.pointer",
                    "timestamp": "2026-07-10T01:00:01Z",
                }
            ),
            json.dumps({"kind": "log", "timestamp": "2026-07-10T01:00:02Z"}),
            "not-json",
        ]
    )
    _write(source / ".guildbotics" / "data" / "run" / "diagnostics.jsonl", diagnostics)
    dest = tmp_path / "workspace"

    migrate_workspace(source, dest)

    event_files = list((dest / ".guildbotics" / "state" / "events").rglob("*.json"))
    assert len(event_files) == 1
    event = json.loads(event_files[0].read_text(encoding="utf-8"))
    assert event["kind"] == "github.push"
    assert event["member_id"] == "aiko"
    # The device-local diagnostics copy is still carried over unchanged.
    assert (dest / ".guildbotics" / "local" / "run" / "diagnostics.jsonl").exists()


def test_migrate_reports_notice_for_leftover_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    source = tmp_path / "repo"
    _write(source / ".guildbotics" / "config" / "a.yml", "a: 1\n")
    _write(source / ".env", "OPENAI_API_KEY=sk-old\n")

    result = migrate_workspace(source, tmp_path / "workspace")

    assert len(result.notices) == 1
    assert "guildbotics secrets import" in result.notices[0]


def test_migrate_keeps_local_generations_of_new_format_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    source = tmp_path / "repo"
    _write(
        source / ".guildbotics" / "config" / "secrets.yml",
        "store_id: abc123\nkeys:\n  OPENAI_API_KEY:\n    generation: 3\n"
        "    updated_at: 2026-08-01T00:00:00Z\n",
    )
    _write(
        source / ".guildbotics" / "local" / "secrets.json",
        json.dumps(
            {
                "schema_version": 1,
                "keys": {"OPENAI_API_KEY": {"generation": 1, "pending_send": False}},
            }
        ),
    )
    dest = tmp_path / "workspace"

    migrate_workspace(source, dest)

    store = KeyringSecretStore(dest / ".guildbotics" / "config")
    # The device's real holdings survive: it holds generation 1, not the
    # shared index's newer generation 3.
    assert store.shared_generation("OPENAI_API_KEY") == 3
    assert store.local_generation("OPENAI_API_KEY") == 1


def test_migrate_scrubs_pending_events_to_minimal_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    source = tmp_path / "repo"
    _write(
        source
        / ".guildbotics"
        / "data"
        / "chat_state"
        / "slack"
        / "aiko"
        / "pending_events"
        / "C1.json",
        json.dumps(
            {
                "events": [
                    {
                        "event_id": "C1:100.1",
                        "channel_id": "C1",
                        "message_ts": "100.1",
                        "thread_ts": "100.1",
                        "author_id": "U1",
                        "text": "<@U_BOT> please check",
                        "mentions": ["U_BOT"],
                        "chat_participation": "strict",
                        "metadata": {
                            "event_type": "guildbotics.workflow_status",
                            "event_payload": {
                                "reason": "failed",
                                "raw_provider_payload": {"blocks": ["huge"]},
                            },
                            "client_msg_id": "abc",
                        },
                    }
                ]
            }
        ),
    )
    dest = tmp_path / "workspace"

    migrate_workspace(source, dest)

    pending_file = (
        dest
        / ".guildbotics"
        / "state"
        / "chat_state"
        / "slack"
        / "aiko"
        / "pending_events"
        / "C1.json"
    )
    text = pending_file.read_text(encoding="utf-8")
    # The resumable minimal payload survives; stray provider metadata does
    # not, even when nested inside the workflow-status envelope.
    assert "please check" in text
    assert '"reason": "failed"' in text
    assert "raw_provider_payload" not in text
    assert "client_msg_id" not in text


def test_migrate_upgrades_dedicated_root_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    _write(
        workspace / ".guildbotics" / "config" / "team" / "project.yml", "language: en\n"
    )
    _write(
        workspace / ".guildbotics" / "config" / "secrets.yml",
        "backend: keyring\nstore_id: abc123\nkeys:\n- OPENAI_API_KEY\n",
    )
    _write(
        workspace / ".guildbotics" / "data" / "documents" / "team" / "doc" / "body.md",
        "hello\n",
    )
    # The new layout is already partially in use: merging must still bring
    # over the old documents without touching the new one.
    _write(
        workspace / ".guildbotics" / "state" / "documents" / "team" / "new" / "body.md",
        "recent\n",
    )

    result = migrate_workspace(workspace, workspace)

    guild = workspace / ".guildbotics"
    # data/ is reclassified into the new layout and left untouched itself.
    assert (guild / "state" / "documents" / "team" / "doc" / "body.md").exists()
    assert (guild / "state" / "documents" / "team" / "new" / "body.md").read_text(
        encoding="utf-8"
    ) == "recent\n"
    assert (guild / "data" / "documents" / "team" / "doc" / "body.md").exists()
    # The old secrets index is converted and this device keeps its holdings.
    store = KeyringSecretStore(guild / "config")
    assert store.keys() == ["OPENAI_API_KEY"]
    assert store.local_generation("OPENAI_API_KEY") == 0
    assert result.destination == workspace.resolve()
