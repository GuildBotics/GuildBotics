import json
from pathlib import Path

import pytest

from guildbotics.capabilities import member_memory
from guildbotics.capabilities.member_memory import (
    MemberMemoryError,
    MemberMemoryService,
)
from guildbotics.capabilities.task_runs import RUN_ENV
from guildbotics.entities.team import Person
from guildbotics.utils.fileio import GUILDBOTICS_DATA_DIR, load_yaml_file

EXPECTED_DIGEST_N = 3


@pytest.fixture
def person() -> Person:
    return Person(person_id="aiko", name="Aiko", person_type="human")


@pytest.fixture(autouse=True)
def data_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "data"
    monkeypatch.setenv(GUILDBOTICS_DATA_DIR, str(root))
    monkeypatch.delenv(RUN_ENV, raising=False)
    return root


def test_record_recall_get_and_digest_redact_secret(
    monkeypatch: pytest.MonkeyPatch, person: Person, data_root: Path
) -> None:
    monkeypatch.setenv("AIKO_GITHUB_ACCESS_TOKEN", "secret-token-value")
    service = MemberMemoryService(person)

    result = service.record(
        scope="personal",
        title="Retry pitfall",
        summary="Refresh tokens before retrying.",
        keywords=["retry", "リトライ"],
        source=[{"type": "ticket", "url": "https://example.test/issues/1"}],
        body="Do not store secret-token-value. Retry after refresh.",
    )

    doc_id = result["doc_id"]
    assert result["path"] == f"documents/personal/aiko/{doc_id}"
    meta = load_yaml_file(
        data_root / "documents" / "personal" / "aiko" / doc_id / "meta.yml"
    )
    assert isinstance(meta, dict)
    assert meta["title"] == "Retry pitfall"
    assert meta["source"][0]["url"] == "https://example.test/issues/1"
    assert (data_root / "documents" / "personal" / "aiko" / "recent.txt").read_text(
        encoding="utf-8"
    ).splitlines() == [doc_id]

    by_body = service.recall(queries=["リトライ", "retry"])["results"]
    assert [item["doc_id"] for item in by_body] == [doc_id]
    by_source = service.recall(
        queries=["https://example.test/issues/1"], meta_only=True
    )["results"]
    assert [item["doc_id"] for item in by_source] == [doc_id]

    full = service.get(doc_id=doc_id)
    assert full["body"] == "Do not store ***. Retry after refresh."
    assert full["assets"] == []
    assert service.load_digest(limit=1)[0]["doc_id"] == doc_id


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
