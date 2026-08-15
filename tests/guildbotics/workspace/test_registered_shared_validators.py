"""The semantic checks each owning module registers for its shared files.

The boundary itself only guarantees that a shared file is inside the shared
roots, bounded, decodable, and well-formed. Everything below is a rule its
owner adds on top, and the same rule runs twice: before the file is sent, and
when another device receives it.
"""

from __future__ import annotations

import importlib
import json

import pytest
import yaml

# Registration happens on import, so the owning modules must be loaded.
import guildbotics.capabilities.member_memory  # noqa: F401
import guildbotics.capabilities.task_runs  # noqa: F401
import guildbotics.integrations.file_chat_state_store  # noqa: F401
import guildbotics.utils.secret_store  # noqa: F401
from guildbotics.integrations.file_chat_state_store import PENDING_EVENT_FIELDS
from guildbotics.utils import shared_file_validators
from guildbotics.utils.shared_file_validators import (
    OWNED_SHARED_PREFIXES,
    SharedFileInvalidError,
    find_shared_validator,
)
from guildbotics.workspace.validation import validate_shared_file

PENDING = "state/chat_state/slack/aiko/pending_events/C1.json"
OWNED = "state/chat_state/slack/aiko/channels/C1.json"
META = "state/documents/team/abc123/meta.yml"
RUN = "state/task-runs/run-1.jsonl"
SECRETS = "config/secrets.yml"


def _meta(**overrides: object) -> bytes:
    meta = {
        "title": "同期の設計",
        "summary": "Git 同期の判断基準",
        "keywords": ["sync"],
        "source": [],
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-02T00:00:00Z",
        "created_by": "yuki",
        "updated_by": "yuki",
        "pinned": False,
        "kind": "note",
    }
    meta.update(overrides)
    return yaml.dump(meta, allow_unicode=True).encode("utf-8")


def _pending(**overrides: object) -> bytes:
    event: dict[str, object] = {field: None for field in PENDING_EVENT_FIELDS}
    event.update(
        {
            "event_id": "C1:100.1",
            "channel_id": "C1",
            "message_ts": "100.1",
            "thread_ts": "100.1",
            "text": "hello",
            "mentions": ["U2"],
        }
    )
    event.update(overrides)
    return json.dumps({"events": [event]}).encode("utf-8")


# -- Owners that were not loaded ----------------------------------------------


def test_every_declared_owner_registers_the_kind_it_claims() -> None:
    """The declaration and the registration are written in different modules,
    so the boundary can only refuse a missing owner if the two agree."""
    for prefix, owner in OWNED_SHARED_PREFIXES.items():
        importlib.import_module(owner)
        assert find_shared_validator(f"{prefix}/x") is not None, prefix


def test_an_owned_kind_is_refused_when_its_owner_is_not_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registration happens on import, so import order would otherwise decide
    how strictly a shared file is checked. A missing owner is a refusal, never
    a quiet fallback to a syntax check."""
    monkeypatch.setattr(shared_file_validators, "_validators", {})

    with pytest.raises(SharedFileInvalidError, match="file_chat_state_store"):
        validate_shared_file(OWNED, b'{"cursor": "1"}')


def test_a_kind_with_no_owner_is_still_held_to_the_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shared_file_validators, "_validators", {})

    validate_shared_file("config/intelligences/model_mapping.yml", b"default: gpt\n")
    with pytest.raises(SharedFileInvalidError, match="not valid YAML"):
        validate_shared_file(
            "config/intelligences/model_mapping.yml", b"default: [unclosed\n"
        )


# -- Conversation control state ----------------------------------------------


def test_conversation_state_accepts_the_normalized_pending_event() -> None:
    validate_shared_file(PENDING, _pending())


def test_conversation_state_refuses_a_raw_provider_payload() -> None:
    """§27.5 allows only the normalized minimum, so an event carrying the
    provider payload it came from is refused rather than trimmed."""
    with pytest.raises(SharedFileInvalidError, match="raw_event"):
        validate_shared_file(PENDING, _pending(raw_event={"blocks": []}))


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"event_id": None}, "no event_id"),
        ({"message_ts": ""}, "no message_ts"),
        ({"thread_ts": None}, "no thread_ts"),
        ({"mentions": "U2"}, "non-list mentions"),
    ],
)
def test_conversation_state_refuses_an_event_the_loader_would_skip(
    overrides: dict[str, object], expected: str
) -> None:
    """``load_pending_events`` drops an item missing an id or a timestamp in
    silence. A device taking over the service would lose the work, so the
    boundary refuses the file instead of letting it arrive and be ignored."""
    with pytest.raises(SharedFileInvalidError, match=expected):
        validate_shared_file(PENDING, _pending(**overrides))


def test_conversation_state_refuses_an_empty_event() -> None:
    with pytest.raises(SharedFileInvalidError, match="no event_id"):
        validate_shared_file(PENDING, b'{"events": [{}]}')


def test_conversation_state_refuses_a_file_that_is_not_an_object() -> None:
    with pytest.raises(SharedFileInvalidError, match="not a JSON object"):
        validate_shared_file(
            "state/chat_state/slack/aiko/channels/C1.json", b"[1, 2, 3]"
        )


def test_conversation_state_leaves_other_files_to_the_boundary() -> None:
    validate_shared_file("state/chat_state/slack/aiko/channels/C1.json", b'{"ts": "1"}')


# -- Memory documents ---------------------------------------------------------


def test_memory_metadata_accepts_a_document_with_extra_fields() -> None:
    """``--set`` puts caller-chosen fields in metadata, so extras are allowed;
    what recall needs is what must be there."""
    validate_shared_file(META, _meta(review_round=2))


def test_memory_metadata_accepts_a_document_recorded_without_a_summary() -> None:
    """``--summary`` is optional and recall reads a missing one as empty, so
    requiring it would hold back a document the user recorded correctly."""
    validate_shared_file(META, _meta(summary=""))


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"title": ""}, "has no title"),
        ({"kind": "policy-ish"}, "unknown kind"),
        ({"updated_at": "yesterday"}, "unparsable updated_at"),
        ({"keywords": "sync"}, "non-list keywords"),
    ],
)
def test_memory_metadata_refuses_what_recall_cannot_use(
    overrides: dict[str, object], expected: str
) -> None:
    with pytest.raises(SharedFileInvalidError, match=expected):
        validate_shared_file(META, _meta(**overrides))


def test_memory_bodies_and_journals_stay_free_form() -> None:
    validate_shared_file("state/documents/team/abc123/body.md", "# 見出し\n".encode())
    validate_shared_file(
        "state/documents/memory-audit.jsonl", b'{"action": "record"}\n'
    )


# -- Task runs ----------------------------------------------------------------


def test_a_run_journal_accepts_its_own_record_kinds() -> None:
    validate_shared_file(
        RUN,
        b'{"kind": "evidence", "evidence_type": "pr_create"}\n'
        b'{"kind": "complete", "status": "done"}\n',
    )


def test_a_run_journal_refuses_a_record_of_an_unknown_kind() -> None:
    with pytest.raises(SharedFileInvalidError, match="unknown record kind"):
        validate_shared_file(RUN, b'{"kind": "console_output", "text": "..."}\n')


# -- The secret key index -----------------------------------------------------


def test_the_secret_index_accepts_key_names_and_generations() -> None:
    validate_shared_file(
        SECRETS,
        yaml.dump(
            {
                "store_id": "abc",
                "keys": {"GITHUB_TOKEN": {"generation": 2, "updated_at": "2026-08-01"}},
            }
        ).encode("utf-8"),
    )


def test_the_secret_index_refuses_anything_that_could_be_a_value() -> None:
    """Secrets stay out of shared state because the index has no slot for a
    value at all, not because values are recognized and stripped."""
    with pytest.raises(SharedFileInvalidError, match="records value for GITHUB_TOKEN"):
        validate_shared_file(
            SECRETS,
            yaml.dump(
                {
                    "store_id": "abc",
                    "keys": {"GITHUB_TOKEN": {"generation": 1, "value": "ghp-x"}},
                }
            ).encode("utf-8"),
        )


def test_the_secret_index_refuses_a_key_stored_as_a_bare_value() -> None:
    with pytest.raises(SharedFileInvalidError, match="stores a value for GITHUB_TOKEN"):
        validate_shared_file(
            SECRETS,
            yaml.dump({"store_id": "abc", "keys": {"GITHUB_TOKEN": "ghp-x"}}).encode(
                "utf-8"
            ),
        )


def test_the_secret_index_refuses_an_unknown_top_level_section() -> None:
    with pytest.raises(SharedFileInvalidError, match="carries values"):
        validate_shared_file(
            SECRETS,
            yaml.dump({"store_id": "abc", "keys": {}, "values": {}}).encode("utf-8"),
        )


# -- What the storage layers really write -------------------------------------


def test_a_pending_event_written_by_the_store_validates(tmp_path) -> None:
    """The allowed-field list is only worth having if it tracks the writer, so
    the check runs against a file the store itself produced."""
    from guildbotics.integrations.chat_service import ChatEvent
    from guildbotics.integrations.file_chat_state_store import (
        FileConversationStateStore,
    )

    store = FileConversationStateStore(base_dir=tmp_path)
    store.upsert_pending_event(
        "slack",
        "aiko",
        "C1",
        ChatEvent(
            event_id="C1:100.1",
            channel_id="C1",
            message_ts="100.1",
            thread_ts="100.1",
            author_id="U1",
            text="hello",
            mentions=["U2"],
        ),
        "social",
    )

    written = tmp_path / "slack" / "aiko" / "pending_events" / "C1.json"
    validate_shared_file(PENDING, written.read_bytes())


def test_a_memory_document_written_by_the_service_validates(
    tmp_path, monkeypatch
) -> None:
    from guildbotics.capabilities.member_memory import MemberMemoryService
    from guildbotics.entities.team import Person

    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))
    service = MemberMemoryService(Person(person_id="yuki", name="Yuki Nakamura"))
    result = service.record(
        scope="team",
        title="同期の設計",
        body="first-committer-wins にする",
        summary="Git 同期の判断基準",
        keywords=["sync", "同期"],
    )

    written = (
        tmp_path / ".guildbotics" / "state" / "documents" / "team" / result["doc_id"]
    )
    validate_shared_file(META, (written / "meta.yml").read_bytes())


def test_a_secret_index_written_by_the_store_validates(
    tmp_path, monkeypatch, fake_keyring
) -> None:
    from guildbotics.utils.secret_store import KeyringSecretStore

    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))
    config_dir = tmp_path / ".guildbotics" / "config"
    KeyringSecretStore(config_dir).set("GITHUB_TOKEN", "ghp-live-secret-12345")

    data = (config_dir / "secrets.yml").read_bytes()
    validate_shared_file(SECRETS, data)
    assert b"ghp-live-secret-12345" not in data
