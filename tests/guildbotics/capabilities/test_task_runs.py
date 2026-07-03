import pytest

from guildbotics.capabilities.task_runs import RunStore, TaskRunError, TaskRunStore


def test_task_run_complete_requires_write_evidence(tmp_path):
    store = TaskRunStore(tmp_path)
    store.append("run-1", {"kind": "note", "message": "started"})

    with pytest.raises(TaskRunError, match="write evidence"):
        store.complete(
            "run-1",
            "done",
            "summary",
            "https://github.com/owner/repo/issues/1",
            "aiko",
        )


def test_task_run_complete_and_status(tmp_path):
    store = TaskRunStore(tmp_path)
    store.append_evidence("run-1", "pr_create", {"pr_url": "https://example.test/pr"})

    status = store.complete(
        "run-1",
        "done",
        "summary",
        "https://github.com/owner/repo/issues/1",
        "aiko",
    )

    assert status.to_dict() == {
        "run_id": "run-1",
        "completed": True,
        "status": "done",
        "summary": "summary",
        "subject_type": "ticket",
        "subject_id": "https://github.com/owner/repo/issues/1",
        "subject_url": "https://github.com/owner/repo/issues/1",
        "person_id": "aiko",
        "evidence_count": 1,
        "evidence_types": ["pr_create"],
        "completed_at": status.completed_at,
    }
    assert store.status("run-1").completed is True


def test_task_run_asking_requires_comment_evidence(tmp_path):
    store = TaskRunStore(tmp_path)
    store.append_evidence("run-1", "git_publish", {"commit_sha": "abc"})

    with pytest.raises(TaskRunError, match="write evidence"):
        store.complete(
            "run-1",
            "asking",
            "summary",
            "https://github.com/owner/repo/issues/1",
            "aiko",
        )


def test_task_run_done_with_code_publish_requires_pr_create(tmp_path):
    store = TaskRunStore(tmp_path)
    store.append_evidence("run-1", "git_publish", {"commit_sha": "abc"})

    with pytest.raises(TaskRunError, match="write evidence"):
        store.complete(
            "run-1",
            "done",
            "summary",
            "https://github.com/owner/repo/issues/1",
            "aiko",
        )

    store.append_evidence("run-1", "pr_create", {"pr_url": "https://example.test/pr"})

    status = store.complete(
        "run-1",
        "done",
        "summary",
        "https://github.com/owner/repo/issues/1",
        "aiko",
    )

    assert status.evidence_types == ["git_publish", "pr_create"]


def test_task_run_redacts_secret_like_payload_keys(tmp_path):
    store = TaskRunStore(tmp_path)
    store.append_evidence("run-1", "issue_comment", {"access_token": "secret"})
    text = (tmp_path / "run-1.jsonl").read_text(encoding="utf-8")

    assert "secret" not in text
    assert "***" in text


def test_chat_run_done_accepts_noop_evidence(tmp_path):
    store = RunStore(tmp_path)
    store.append_evidence("run-1", "chat_noop", {"reason": "not relevant"})

    status = store.complete_run(
        "run-1",
        "done",
        "No response needed.",
        subject_type="chat",
        subject_id="slack:C1:100.1:E1",
        person_id="aiko",
    )

    assert status.subject_type == "chat"
    assert status.subject_id == "slack:C1:100.1:E1"
    assert status.evidence_types == ["chat_noop"]


def test_summaries_by_subject_keys_on_subject_and_person(tmp_path):
    store = RunStore(tmp_path)
    for run_id, person, summary in [
        ("run-a", "aiko", "aiko の回答"),
        ("run-y", "yuki", "yuki の回答"),
    ]:
        store.append_evidence(run_id, "chat_reply", {"text": "answered"})
        store.complete_run(
            run_id,
            "done",
            summary,
            subject_type="chat",
            subject_id="slack:C1:100.1:E1",
            person_id=person,
        )

    # Same subject, two members: each keeps its own summary (no cross-member).
    assert store.summaries_by_subject() == {
        ("slack:C1:100.1:E1", "aiko"): "aiko の回答",
        ("slack:C1:100.1:E1", "yuki"): "yuki の回答",
    }


def test_summaries_by_subject_ignores_incomplete_runs(tmp_path):
    store = RunStore(tmp_path)

    assert store.summaries_by_subject() == {}

    store.append_evidence("run-2", "chat_reply", {"text": "in progress"})
    assert store.summaries_by_subject() == {}


def test_chat_run_asking_requires_post_evidence(tmp_path):
    store = RunStore(tmp_path)
    store.append_evidence("run-1", "chat_reaction", {"reaction": "ack"})

    with pytest.raises(TaskRunError, match="write evidence"):
        store.complete_run(
            "run-1",
            "asking",
            "Asked a follow-up.",
            subject_type="chat",
            subject_id="slack:C1:100.1:E1",
            person_id="aiko",
        )

    store.append_evidence("run-1", "chat_reply", {"text": "Could you clarify?"})

    status = store.complete_run(
        "run-1",
        "asking",
        "Asked a follow-up.",
        subject_type="chat",
        subject_id="slack:C1:100.1:E1",
        person_id="aiko",
    )

    assert status.status == "asking"


def test_chat_run_blocked_requires_summary_but_no_evidence(tmp_path):
    store = RunStore(tmp_path)

    with pytest.raises(TaskRunError, match="write evidence"):
        store.complete_run(
            "run-1",
            "blocked",
            "",
            subject_type="chat",
            subject_id="slack:C1:100.1:E1",
            person_id="aiko",
        )

    status = store.complete_run(
        "run-1",
        "blocked",
        "Slack credential is unavailable.",
        subject_type="chat",
        subject_id="slack:C1:100.1:E1",
        person_id="aiko",
    )

    assert status.status == "blocked"
    assert status.evidence_types == []
