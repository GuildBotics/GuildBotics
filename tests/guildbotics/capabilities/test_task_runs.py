import pytest

from guildbotics.capabilities.task_runs import TaskRunError, TaskRunStore


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
