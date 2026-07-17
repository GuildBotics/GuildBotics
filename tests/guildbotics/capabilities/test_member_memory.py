import json
from pathlib import Path

import pytest

from guildbotics.capabilities import member_memory
from guildbotics.capabilities.member_memory import (
    MemberMemoryError,
    MemberMemoryService,
)
from guildbotics.capabilities.member_memory_audit import MemoryAuditStore
from guildbotics.capabilities.task_runs import RUN_ENV, TASK_RUN_ENV
from guildbotics.entities.team import Person
from guildbotics.observability import trace_scope
from guildbotics.utils.fileio import (
    GUILDBOTICS_DATA_DIR,
    load_yaml_file,
    save_yaml_file,
)

EXPECTED_DIGEST_N = 3


@pytest.fixture
def person() -> Person:
    return Person(person_id="aiko", name="Aiko", person_type="human")


@pytest.fixture(autouse=True)
def data_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "data"
    monkeypatch.setenv(GUILDBOTICS_DATA_DIR, str(root))
    monkeypatch.delenv(RUN_ENV, raising=False)
    monkeypatch.delenv(TASK_RUN_ENV, raising=False)
    return root


def test_record_recall_get_and_digest_redact_secret(
    monkeypatch: pytest.MonkeyPatch, person: Person, data_root: Path
) -> None:
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "secret-token-value")
    service = MemberMemoryService(person)

    result = service.record(
        scope="personal",
        title="Retry secret-token-value pitfall",
        summary="Refresh secret-token-value before retrying.",
        keywords=["retry", "リトライ", "secret-token-value"],
        source=[
            {
                "type": "ticket",
                "url": "https://example.test/issues/secret-token-value",
            }
        ],
        body="Do not store secret-token-value. Retry after refresh.",
    )

    doc_id = result["doc_id"]
    assert result["path"] == f"documents/personal/aiko/{doc_id}"
    meta = load_yaml_file(
        data_root / "documents" / "personal" / "aiko" / doc_id / "meta.yml"
    )
    assert isinstance(meta, dict)
    assert meta["title"] == "Retry *** pitfall"
    assert meta["summary"] == "Refresh *** before retrying."
    assert meta["keywords"] == ["retry", "リトライ", "***"]
    assert meta["source"][0]["url"] == "https://example.test/issues/***"
    assert (data_root / "documents" / "personal" / "aiko" / "recent.txt").read_text(
        encoding="utf-8"
    ).splitlines() == [doc_id]

    by_body = service.recall(queries=["リトライ", "retry"])["results"]
    assert [item["doc_id"] for item in by_body] == [doc_id]
    by_source = service.recall(
        queries=["https://example.test/issues/***"], meta_only=True
    )["results"]
    assert [item["doc_id"] for item in by_source] == [doc_id]

    full = service.get(doc_id=doc_id)
    assert full["body"] == "Do not store ***. Retry after refresh."
    assert full["assets"] == []
    assert service.load_digest(limit=1)[0]["doc_id"] == doc_id


def test_record_rejects_set_for_note(person: Person) -> None:
    service = MemberMemoryService(person)

    with pytest.raises(MemberMemoryError, match="--set"):
        service.record(
            scope="personal",
            title="Note",
            body="body",
            params={"digest_n": EXPECTED_DIGEST_N},
        )


def test_team_memory_tracks_creator_and_updater(
    monkeypatch: pytest.MonkeyPatch, person: Person, data_root: Path
) -> None:
    timestamps = iter(["2026-07-14T01:00:00Z", "2026-07-14T02:00:00Z"])
    monkeypatch.setattr(member_memory, "_now", lambda: next(timestamps))
    recorded = MemberMemoryService(person).record(
        scope="team", title="Shared context", body="Initial context"
    )

    updater = Person(person_id="yuki", name="Yuki", person_type="human")
    MemberMemoryService(updater).update(
        doc_id=recorded["doc_id"], scope="team", body="Updated context"
    )

    meta = load_yaml_file(
        data_root / "documents" / "team" / recorded["doc_id"] / "meta.yml"
    )
    assert isinstance(meta, dict)
    assert meta["created_at"] == "2026-07-14T01:00:00Z"
    assert meta["created_by"] == "aiko"
    assert meta["updated_at"] == "2026-07-14T02:00:00Z"
    assert meta["updated_by"] == "yuki"


def test_updating_legacy_memory_does_not_fabricate_creator(
    person: Person, data_root: Path
) -> None:
    recorded = MemberMemoryService(person).record(
        scope="team", title="Legacy context", body="Initial context"
    )
    meta_path = data_root / "documents" / "team" / recorded["doc_id"] / "meta.yml"
    legacy_meta = load_yaml_file(meta_path)
    assert isinstance(legacy_meta, dict)
    legacy_meta.pop("created_by")
    legacy_meta.pop("updated_by")
    save_yaml_file(meta_path, legacy_meta)

    updater = Person(person_id="yuki", name="Yuki", person_type="human")
    MemberMemoryService(updater).update(
        doc_id=recorded["doc_id"], scope="team", body="Updated context"
    )

    updated_meta = load_yaml_file(meta_path)
    assert isinstance(updated_meta, dict)
    assert "created_by" not in updated_meta
    assert updated_meta["updated_by"] == "yuki"


def test_recall_strips_blank_queries(person: Person) -> None:
    service = MemberMemoryService(person)
    first = service.record(scope="personal", title="First", body="alpha")
    second = service.record(scope="personal", title="Second", body="beta")

    results = service.recall(queries=["   ", " alpha "])["results"]

    assert [item["doc_id"] for item in results] == [first["doc_id"]]
    assert second["doc_id"] not in [item["doc_id"] for item in results]


def test_recall_uses_rg_for_meta_only(
    monkeypatch: pytest.MonkeyPatch, person: Person
) -> None:
    service = MemberMemoryService(person)
    result = service.record(
        scope="personal",
        title="Source note",
        source=[{"type": "ticket", "url": "https://example.test/issues/1"}],
        body="body text",
    )
    doc_id = result["doc_id"]
    meta_path = service.root / "personal" / "aiko" / doc_id / "meta.yml"
    captured: dict[str, list[str]] = {}

    def fake_run(command: list[str], **_kwargs: object) -> object:
        captured["command"] = command
        stdout = json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": str(meta_path)},
                    "lines": {"text": "url: https://example.test/issues/1\n"},
                },
            }
        )
        return member_memory.subprocess.CompletedProcess(
            command,
            0,
            stdout=stdout,
            stderr="",
        )

    monkeypatch.setattr(member_memory.shutil, "which", lambda _name: "/usr/bin/rg")
    monkeypatch.setattr(member_memory.subprocess, "run", fake_run)

    recall = service.recall(
        queries=["https://example.test/issues/1"],
        meta_only=True,
    )

    assert recall["results"][0]["doc_id"] == doc_id
    assert f"**/{member_memory.META_FILE}" in captured["command"]
    assert f"**/{member_memory.BODY_FILE}" not in captured["command"]
    assert "--fixed-strings" in captured["command"]
    assert "--ignore-case" in captured["command"]


def test_recall_warns_when_rg_is_missing(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    person: Person,
) -> None:
    service = MemberMemoryService(person)
    result = service.record(scope="personal", title="Fallback", body="fallback body")

    monkeypatch.setattr(member_memory.shutil, "which", lambda _name: None)

    with caplog.at_level("WARNING", logger=member_memory.__name__):
        recall = service.recall(queries=["fallback"])

    assert recall["results"][0]["doc_id"] == result["doc_id"]
    assert "ripgrep executable 'rg' was not found" in caplog.text


def test_update_touch_archive_and_promote_manage_recency(person: Person) -> None:
    service = MemberMemoryService(person)
    first = service.record(scope="personal", title="First", body="alpha")
    second = service.record(scope="personal", title="Second", body="beta")
    first_id = first["doc_id"]
    second_id = second["doc_id"]

    service.update(
        doc_id=first_id,
        body="alpha fixed",
        add_keywords=["fixed"],
        summary="Updated.",
    )
    assert service.get(doc_id=first_id)["body"] == "alpha fixed"
    assert [item["doc_id"] for item in service.load_digest(limit=2)] == [
        first_id,
        second_id,
    ]

    service.touch(doc_id=second_id)
    assert [item["doc_id"] for item in service.load_digest(limit=2)] == [
        second_id,
        first_id,
    ]

    promoted = service.promote(doc_id=second_id)
    assert promoted["path"] == f"documents/team/{second_id}"
    assert service.get(doc_id=second_id)["path"] == f"documents/team/{second_id}"

    archived = service.archive(doc_id=first_id)
    assert archived["path"] == f"documents/personal/aiko/archived/{first_id}"
    assert service.recall(queries=["alpha"])["results"] == []


def test_memory_mutations_write_audit_events(
    monkeypatch: pytest.MonkeyPatch, person: Person, data_root: Path
) -> None:
    monkeypatch.setenv(RUN_ENV, "run-1")
    monkeypatch.setenv(TASK_RUN_ENV, "task-1")
    service = MemberMemoryService(person)

    with trace_scope(
        "manual",
        trace_id="trace-1",
        person_id="aiko",
        command="workflows/demo",
    ):
        recorded = service.record(
            scope="personal",
            title="Audit note",
            summary="Audit summary",
            source=[{"type": "ticket", "url": "https://example.test/issues/1"}],
            body="body",
        )
        service.touch(doc_id=recorded["doc_id"])
        service.update(doc_id=recorded["doc_id"], summary="Updated summary")

    audit_path = data_root / "documents" / "memory_events.jsonl"
    events = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]

    assert [event["type"] for event in events] == [
        "memory.record",
        "memory.touch",
        "memory.update",
    ]
    assert events[0]["person_id"] == "aiko"
    assert events[0]["trace_id"] == "trace-1"
    assert events[0]["source"] == "manual"
    assert events[0]["command"] == "workflows/demo"
    assert events[0]["attributes"]["memory.doc_id"] == recorded["doc_id"]
    assert events[0]["attributes"]["memory.scope"] == "personal"
    assert events[0]["attributes"]["run_id"] == "run-1"
    assert events[0]["attributes"]["task_run_id"] == "task-1"
    assert events[0]["timestamp"].endswith("Z")
    assert events[0]["payload"]["source"] == [
        {"type": "ticket", "url": "https://example.test/issues/1"}
    ]
    assert events[2]["payload"]["changed_fields"] == ["summary"]


def test_memory_recall_and_get_write_audit_events(
    monkeypatch: pytest.MonkeyPatch, person: Person, data_root: Path
) -> None:
    monkeypatch.setattr(member_memory.shutil, "which", lambda _name: None)
    service = MemberMemoryService(person)

    recorded = service.record(
        scope="personal",
        title="Searchable note",
        summary="Useful memory search summary.",
        body="Searchable body",
    )
    recall = service.recall(queries=[" Searchable ", " "], limit=5)
    full = service.get(doc_id=recorded["doc_id"])

    assert [item["doc_id"] for item in recall["results"]] == [recorded["doc_id"]]
    assert full["doc_id"] == recorded["doc_id"]

    audit_path = data_root / "documents" / "memory_events.jsonl"
    events = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]

    assert [event["type"] for event in events] == [
        "memory.record",
        "memory.recall",
        "memory.get",
    ]
    assert events[1]["payload"]["query_keywords"] == ["Searchable"]
    assert events[1]["payload"]["result_count"] == 1
    assert isinstance(events[1]["payload"]["duration_ms"], float)
    assert events[1]["attributes"]["memory.result_count"] == 1
    assert "Searchable" in events[1]["message"]
    assert events[2]["attributes"]["memory.doc_id"] == recorded["doc_id"]


def test_memory_audit_filters_timestamps_by_instant(tmp_path: Path) -> None:
    store = MemoryAuditStore(tmp_path / "memory_events.jsonl")
    store.record(
        {
            "timestamp": "2026-06-21T01:00:00Z",
            "message": "first",
            "attributes": {},
            "payload": {},
        }
    )
    store.record(
        {
            "timestamp": "2026-06-21T10:30:00+09:00",
            "message": "second",
            "attributes": {},
            "payload": {},
        }
    )

    assert [
        event["message"]
        for event in store.list_events(since="2026-06-21T09:30:00+09:00")
    ] == ["second", "first"]
    assert [
        event["message"]
        for event in store.list_events(until="2026-06-21T10:00:00+09:00")
    ] == ["first"]


def test_memory_audit_rewrites_to_bounded_newest_rows(tmp_path: Path) -> None:
    path = tmp_path / "memory_events.jsonl"
    store = MemoryAuditStore(path, max_file_bytes=240)
    for index in range(8):
        store.record(
            {
                "timestamp": f"2026-06-21T01:00:0{index}Z",
                "message": f"event-{index}-" + "x" * 40,
                "attributes": {},
                "payload": {},
            }
        )

    events = store.list_events()

    assert path.stat().st_size <= 240
    assert events[0]["message"].startswith("event-7-")
    assert all(json.loads(line) for line in path.read_text().splitlines())


def test_memory_audit_compacts_oversized_newest_record(tmp_path: Path) -> None:
    path = tmp_path / "memory_events.jsonl"
    store = MemoryAuditStore(path, max_file_bytes=512)
    store.record(
        {
            "kind": "memory",
            "type": "memory.record",
            "timestamp": "2026-06-21T01:00:00Z",
            "trace_id": "trace-1",
            "message": "oversized",
            "attributes": {
                "memory.action": "record",
                "memory.doc_id": "doc-1",
            },
            "payload": {"summary": "x" * 2048},
        }
    )

    events = store.list_events()

    assert path.stat().st_size <= 512
    assert len(events) == 1
    assert events[0]["trace_id"] == "trace-1"
    assert events[0]["payload"]["truncated"] is True
    assert events[0]["payload"]["original_size_bytes"] > 512


def test_memory_audit_normalizes_document_path(
    monkeypatch: pytest.MonkeyPatch, person: Person
) -> None:
    captured: list[dict[str, object]] = []

    def fake_append_memory_event(**kwargs: object) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(member_memory, "append_memory_event", fake_append_memory_event)
    monkeypatch.setattr(
        member_memory,
        "_document_path",
        lambda _doc: "documents\\personal\\aiko\\doc-1",
    )
    service = MemberMemoryService(person)

    service.record(scope="personal", title="Path note", body="body")

    assert captured[0]["path"] == "documents/personal/aiko/doc-1"


def test_team_memory_uses_member_recent_file_only(
    person: Person, data_root: Path
) -> None:
    service = MemberMemoryService(person)

    personal = service.record(scope="personal", title="Personal", body="personal")
    team = service.record(scope="team", title="Team", body="team")

    recent = (data_root / "documents" / "personal" / "aiko" / "recent.txt").read_text(
        encoding="utf-8"
    )
    assert recent.splitlines() == [team["doc_id"], personal["doc_id"]]
    assert not (data_root / "documents" / "team" / "recent.txt").exists()
    assert [item["path"] for item in service.load_digest(limit=2)] == [
        team["path"],
        personal["path"],
    ]


def test_baseline_policy_defines_memory_scope(person: Person) -> None:
    body = MemberMemoryService(person).load_pinned()[0]["body"]

    assert "PRs, issues, or team-shared Slack threads in team memory" in body
    assert "personal memory only for member-specific knowledge" in body


def test_policy_memory_requires_approval_and_controls_context(
    monkeypatch: pytest.MonkeyPatch, person: Person
) -> None:
    service = MemberMemoryService(person)

    with pytest.raises(MemberMemoryError, match="policy-approved"):
        service.record(
            scope="team",
            kind="policy",
            title="Recording policy",
            body="Policy body",
        )

    policy = service.record(
        scope="team",
        kind="policy",
        title="Recording policy",
        body="Policy body",
        policy_approved=True,
        params={"digest_n": EXPECTED_DIGEST_N},
    )

    assert policy["path"].startswith("documents/team/")
    assert service.load_policy_params().digest_n == EXPECTED_DIGEST_N
    pinned = service.load_pinned()
    assert pinned[0]["doc_id"] == "baseline-policy"
    assert any(item["body"] == "Policy body" for item in pinned)

    policy_body = service.root / "team" / str(policy["doc_id"]) / "body.md"
    policy_body.write_text("---\ndigest_n: 99\n---\nPolicy body", encoding="utf-8")
    assert service.load_policy_params().digest_n == EXPECTED_DIGEST_N

    service.update(
        doc_id=policy["doc_id"],
        scope="team",
        policy_approved=True,
        params={"digest_n": EXPECTED_DIGEST_N + 1},
    )
    assert service.load_policy_params().digest_n == EXPECTED_DIGEST_N + 1

    monkeypatch.setenv(RUN_ENV, "run-1")
    with pytest.raises(MemberMemoryError, match="autonomous"):
        service.update(
            doc_id=policy["doc_id"],
            scope="team",
            body="New policy",
            policy_approved=True,
        )
