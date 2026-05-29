import json
import os
from pathlib import Path

import guildbotics.utils.cognee_memory_backend as cognee_memory_backend
from guildbotics.entities.team import Person, Project, Team
from guildbotics.utils.cognee_memory_backend import (
    CogneeMemoryBackend,
    DefaultCogneeAdapter,
    FakeMemoryBackend,
    configure_cognee_environment_from_guildbotics_keys,
    dataset_name_for_person,
    memory_data_id,
)
from guildbotics.utils.memory_backend import (
    FileMemoryBackend,
    MemoryContext,
    MemoryForgetRequest,
    MemoryForgetResult,
    MemoryItem,
    MemoryQuery,
    MemoryUpdate,
    MemoryWriteResult,
    write_memory_context_trace,
    write_memory_forget_trace,
    write_memory_recall_final_trace,
    write_memory_recall_raw_trace,
    write_memory_recall_trace,
    write_memory_remember_decision_trace,
    write_memory_remember_trace,
)


def _backend(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    person = Person(person_id="alice", name="Alice")
    team = Team(project=Project(name="GuildBotics", language="ja"), members=[person])
    return FileMemoryBackend(person, team)


class MockCogneeAdapter:
    def __init__(self) -> None:
        self.remember_calls = []
        self.recall_calls = []
        self.forget_calls = []
        self.results = []

    def remember(self, **kwargs):
        self.remember_calls.append(kwargs)
        return {
            "status": "completed",
            "pipeline_run_id": "run-1",
            "dataset_name": kwargs["dataset_name"],
        }

    def recall(self, **kwargs):
        self.recall_calls.append(kwargs)
        return self.results

    def forget(self, **kwargs):
        self.forget_calls.append(kwargs)
        return {
            "status": "success",
            "data_id": str(kwargs["data_id"]),
            "dataset_id": "dataset-1",
        }


def test_file_memory_backend_remembers_and_recalls_topic(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)

    result = backend.remember(
        MemoryUpdate(
            should_update=True,
            topic_id="onboarding",
            title="Onboarding",
            summary="Initial onboarding flow decisions.",
            memory="# Onboarding\n\n## Decisions\n- Keep the first step short.",
            source={
                "type": "slack_thread",
                "service": "slack",
                "channel": "C1",
                "thread_ts": "100.1",
            },
            scope={"person_id": "alice"},
        )
    )

    context = backend.recall(
        MemoryQuery(
            person_id="alice",
            thread_topic="Initial onboarding flow",
            latest_focus="",
            transcript="",
            source={
                "type": "slack_thread",
                "service": "slack",
                "channel": "C1",
                "thread_ts": "100.1",
            },
            scope={"person_id": "alice"},
        )
    )

    assert result.changed is True
    assert result.backend == "file"
    assert result.reference
    assert context.backend == "file"
    assert context.person_id == "alice"
    assert context.query["thread_topic"] == "Initial onboarding flow"
    assert context.items[0].id == "onboarding"
    assert context.items[0].path == "topics/onboarding/memory.md"
    assert context.items[0].score == 1.0
    assert context.items[0].source["thread_ts"] == "100.1"
    assert context.items[0].scope == {"person_id": "alice"}
    assert "Keep the first step short." in context.items[0].content


def test_file_memory_backend_forget_hides_topic_from_recall(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)
    backend.remember(
        MemoryUpdate(
            should_update=True,
            topic_id="onboarding",
            title="Onboarding",
            summary="Initial onboarding flow decisions.",
            memory="# Onboarding\n\n## Decisions\n- Keep the first step short.",
            scope={"person_id": "alice"},
        )
    )

    result = backend.forget(
        MemoryForgetRequest(
            person_id="alice",
            item_id="onboarding",
            reason="User explicitly cancelled this memory.",
            scope={"person_id": "alice"},
        )
    )
    context = backend.recall(
        MemoryQuery(
            person_id="alice",
            thread_topic="Initial onboarding flow",
            latest_focus="",
            transcript="",
        )
    )

    assert result.changed is True
    assert context.items == []


def test_file_memory_backend_recall_skips_expired_temporary_topic(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)
    backend.remember(
        MemoryUpdate(
            should_update=True,
            topic_id="demo-cta",
            title="Demo CTA",
            summary="Temporary demo CTA.",
            memory="# Demo CTA\n\n## Decisions\n- Make the CTA louder today.",
            scope={"person_id": "alice"},
            retention={
                "status": "temporary",
                "expires_at": "2000-01-01T00:00:00+00:00",
                "reason": "Demo-only adjustment.",
            },
        )
    )

    context = backend.recall(
        MemoryQuery(
            person_id="alice",
            thread_topic="Demo CTA",
            latest_focus="",
            transcript="",
        )
    )

    assert context.items == []


def test_file_memory_backend_recall_skips_paths_outside_repo(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)
    repo_path = backend.repo.get_repo_path()
    outside_path = tmp_path / "secret.txt"
    outside_path.write_text("do not leak", encoding="utf-8")
    (repo_path / "memory_index.yml").write_text(
        f"""
topics:
  traversal:
    title: Traversal
    summary: topic
    path: ../../secret.txt
  absolute:
    title: Absolute
    summary: topic
    path: {outside_path}
""",
        encoding="utf-8",
    )

    context = backend.recall(
        MemoryQuery(
            person_id="alice",
            thread_topic="topic",
            latest_focus="",
            transcript="",
        )
    )

    assert context.items == []


def test_memory_trace_writes_jsonl_only_when_enabled(tmp_path, monkeypatch):
    trace_path = tmp_path / "trace.jsonl"
    context = MemoryContext(
        backend="fake",
        person_id="alice",
        query={"thread_topic": "FocusFlow"},
        items=[
            MemoryItem(
                id="focusflow-onboarding-plan",
                title="FocusFlow Onboarding Plan",
                summary="FocusFlow onboarding decisions",
                path="topics/focusflow-onboarding-plan/memory.md",
                content="# FocusFlow",
                score=0.82,
                match_reason="Related to prior decisions.",
                source={
                    "type": "slack_thread",
                    "service": "slack",
                    "channel": "random",
                    "thread_ts": "1720000000.000100",
                },
                scope={"person_id": "alice"},
            )
        ],
    )

    monkeypatch.delenv("GUILDBOTICS_MEMORY_TRACE", raising=False)
    monkeypatch.setenv("GUILDBOTICS_MEMORY_TRACE_PATH", str(trace_path))
    write_memory_recall_trace(context)
    assert not trace_path.exists()

    monkeypatch.setenv("GUILDBOTICS_MEMORY_TRACE", "1")
    write_memory_recall_trace(context)
    write_memory_remember_trace(
        MemoryWriteResult(
            changed=True,
            backend="fake",
            reference="fake-ref",
            person_id="alice",
            item_id="focusflow-onboarding-plan",
            title="FocusFlow Onboarding Plan",
            source=context.items[0].source,
            scope=context.items[0].scope,
            retention={"kind": "current_fact", "status": "active"},
        )
    )
    write_memory_forget_trace(
        MemoryForgetResult(
            changed=True,
            backend="fake",
            person_id="alice",
            item_id="focusflow-onboarding-plan",
        )
    )

    events = [
        json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events] == [
        "memory.recall",
        "memory.remember",
        "memory.forget",
    ]
    assert events[0]["status"] == "ok"
    assert events[0]["error"] == {}
    assert events[0]["hits"][0]["id"] == "focusflow-onboarding-plan"
    assert events[1]["status"] == "ok"
    assert events[1]["error"] == {}
    assert events[1]["result"]["changed"] is True
    assert events[1]["item"]["retention"] == {
        "kind": "current_fact",
        "status": "active",
    }
    assert events[2]["item"]["id"] == "focusflow-onboarding-plan"


def test_memory_diagnostic_trace_events(tmp_path, monkeypatch):
    trace_path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("GUILDBOTICS_MEMORY_TRACE", "1")
    monkeypatch.setenv("GUILDBOTICS_MEMORY_TRACE_PATH", str(trace_path))

    write_memory_recall_raw_trace(
        {
            "backend": "cognee",
            "person_id": "alice",
            "raw_count": 1,
            "raw_results": [{"index": 0, "score": 0.0}],
        }
    )
    write_memory_recall_final_trace(
        {
            "backend": "cognee",
            "person_id": "alice",
            "candidates": [{"id": "x", "decision": "dropped"}],
            "hits": [],
        }
    )
    write_memory_remember_decision_trace(
        {
            "backend": "cognee",
            "person_id": "alice",
            "proposal": {"topic_id": "x"},
            "gate": {"label": "suppress"},
            "final": {"changed": False},
        }
    )
    write_memory_context_trace(
        event="memory.context.prompted",
        backend="cognee",
        person_id="alice",
        consumer="reply_generation",
        query={"thread_topic": "Lunch"},
        items=[
            MemoryItem(
                id="focusflow-onboarding",
                title="FocusFlow Onboarding",
                summary="FocusFlow onboarding policy",
                path="cognee://guildbotics:person:alice/focusflow-onboarding",
                content="# FocusFlow Onboarding",
                score=0.0,
                match_reason="Returned by Cognee recall.",
                source={"type": "slack_thread"},
                scope={"person_id": "alice"},
            )
        ],
        extra={
            "reply_text_excerpt": "今日は蕎麦がよさそうです。",
            "note": "supplied, not necessarily used",
        },
    )

    events = [
        json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]

    assert [event["event"] for event in events] == [
        "memory.recall.raw",
        "memory.recall.final",
        "memory.remember.decision",
        "memory.context.prompted",
    ]
    assert events[0]["raw_count"] == 1
    assert events[1]["candidates"][0]["decision"] == "dropped"
    assert events[2]["gate"]["label"] == "suppress"
    assert events[3]["consumer"] == "reply_generation"
    assert events[3]["memory_context"]["item_ids"] == ["focusflow-onboarding"]
    assert "not necessarily used" in events[3]["note"]


def test_cognee_memory_backend_uses_person_dataset_and_normalizes_results():
    adapter = MockCogneeAdapter()
    adapter.results = [
        {
            "search_result": "\n".join(
                [
                    "guildbotics_memory_id: focusflow-onboarding-plan",
                    "guildbotics_title: FocusFlow Onboarding Plan",
                    "guildbotics_summary: FocusFlow onboarding decisions",
                    'guildbotics_source: {"service": "slack", "thread_ts": "172.1", "type": "slack_thread"}',
                    'guildbotics_scope: {"person_id": "aiko"}',
                    'guildbotics_metadata: {"backend_item_id": "node-1"}',
                    "",
                    "# FocusFlow Onboarding Plan",
                ]
            ),
            "score": 0.82,
            "dataset_id": "dataset-1",
            "dataset_name": "guildbotics:person:aiko",
        }
    ]
    backend = CogneeMemoryBackend("aiko", adapter=adapter)

    context = backend.recall(
        MemoryQuery(
            person_id="aiko",
            thread_topic="FocusFlow onboarding",
            latest_focus="Step 2 tasks",
            transcript="さっき決めたFocusFlowのオンボーディング方針",
            scope={"person_id": "aiko"},
        )
    )

    assert adapter.recall_calls[0]["dataset_name"] == "guildbotics:person:aiko"
    assert context.backend == "cognee"
    assert context.query["dataset"] == "guildbotics:person:aiko"
    assert context.items[0].id == "focusflow-onboarding-plan"
    assert context.items[0].source["thread_ts"] == "172.1"
    assert context.items[0].scope == {"person_id": "aiko"}
    assert context.items[0].metadata["dataset_id"] == "dataset-1"


def test_cognee_memory_backend_keeps_zero_score_results_with_content():
    adapter = MockCogneeAdapter()
    adapter.results = [
        {
            "search_result": "\n".join(
                [
                    "guildbotics_memory_id: focusflow-onboarding-plan",
                    "guildbotics_title: FocusFlow Onboarding Plan",
                    "",
                    "# FocusFlow Onboarding Plan",
                ]
            ),
            "score": 0.0,
        }
    ]
    backend = CogneeMemoryBackend("aiko", adapter=adapter)

    context = backend.recall(
        MemoryQuery(
            person_id="aiko",
            thread_topic="今日のランチの選択肢を検討する",
            latest_focus="ランチの提案を最優先で出すべき。",
            transcript="今日のランチ何にしようかな。",
        )
    )

    assert context.items[0].id == "focusflow-onboarding-plan"
    assert context.items[0].score == 0.0


def test_cognee_memory_backend_keeps_latest_duplicate_memory_id():
    adapter = MockCogneeAdapter()
    adapter.results = [
        {
            "search_result": "\n".join(
                [
                    "guildbotics_memory_id: focusflow-onboarding-plan",
                    "guildbotics_title: FocusFlow Onboarding Plan",
                    "guildbotics_updated_at: 2026-04-29T10:00:00+09:00",
                    "",
                    "# FocusFlow Onboarding Plan",
                    "- 通知初期値は弱め。",
                ]
            ),
            "score": 0.9,
        },
        {
            "search_result": "\n".join(
                [
                    "guildbotics_memory_id: focusflow-onboarding-plan",
                    "guildbotics_title: FocusFlow Onboarding Plan",
                    "guildbotics_updated_at: 2026-04-30T10:00:00+09:00",
                    "",
                    "# FocusFlow Onboarding Plan",
                    "- 通知初期値はオフ。",
                ]
            ),
            "score": 0.8,
        },
    ]
    backend = CogneeMemoryBackend("aiko", adapter=adapter)

    context = backend.recall(
        MemoryQuery(
            person_id="aiko",
            thread_topic="FocusFlow Onboarding Plan",
            latest_focus="通知初期値",
            transcript="",
        )
    )

    assert len(context.items) == 1
    assert "通知初期値はオフ" in context.items[0].content


def test_cognee_memory_backend_prefers_structured_header_when_content_is_mixed():
    adapter = MockCogneeAdapter()
    adapter.results = [
        {
            "search_result": "\n".join(
                [
                    "Nodes:",
                    "Node: guildbotics_memory_id: focusflow-onboarding-policy guildbotics_title: FocusFlow onboarding policy",
                    "__node_content_start__",
                    "guildbotics_memory_id: focusflow-onboarding-policy",
                    "guildbotics_title: FocusFlow onboarding policy",
                    'guildbotics_retention: {"status":"active","kind":"current_fact"}',
                    '{"guildbotics_memory":{"memory_id":"focusflow-onboarding-policy-transition-1","title":"FocusFlow onboarding policy Change","summary":"Transition summary","source":{"type":"slack_thread"},"scope":{"person_id":"aiko"},"metadata":{"reason":"transition"},"retention":{"status":"active","kind":"transition","subject_item_id":"focusflow-onboarding-policy","effective_at":"2026-05-01T21:03:51+09:00"},"updated_at":"2026-05-01T21:03:51+09:00"}}',
                    "# FocusFlow onboarding policy Change",
                    "## Current Memory Excerpt",
                    "guildbotics_memory_id: focusflow-onboarding-policy",
                    "guildbotics_title: FocusFlow onboarding policy",
                    'guildbotics_retention: {"status":"active","kind":"current_fact"}',
                ]
            ),
            "score": 0.8,
        }
    ]
    backend = CogneeMemoryBackend("aiko", adapter=adapter)

    context = backend.recall(
        MemoryQuery(
            person_id="aiko",
            thread_topic="FocusFlow onboarding policy update",
            latest_focus="transition handling",
            transcript="",
        )
    )

    assert len(context.items) == 1
    assert context.items[0].id == "focusflow-onboarding-policy-transition-1"
    assert context.items[0].title == "FocusFlow onboarding policy Change"
    assert context.items[0].retention["kind"] == "transition"
    assert context.items[0].retention["subject_item_id"] == "focusflow-onboarding-policy"


def test_cognee_memory_backend_handles_multi_node_single_result_and_picks_latest():
    adapter = MockCogneeAdapter()
    adapter.results = [
        {
            "search_result": "\n".join(
                [
                    "Nodes:",
                    "Node: guildbotics_memory_id: focusflow-cta guildbotics_title: FocusFlow CTA",
                    "__node_content_start__",
                    "guildbotics_memory_id: focusflow-cta",
                    "guildbotics_title: FocusFlow CTA",
                    "guildbotics_summary: FocusFlow の CTA 検討: 候補「まずは3つだけ整える」が挙がっており、メインCTAは未決。",
                    "guildbotics_updated_at: 2026-05-29T20:21:20+09:00",
                    'guildbotics_retention: {"status":"active","kind":"current_fact"}',
                    "",
                    "# FocusFlow CTA",
                    "- 候補は「まずは3つだけ整える」。",
                    "__node_content_start__",
                    "guildbotics_memory_id: focusflow-cta",
                    "guildbotics_title: FocusFlow CTA",
                    "guildbotics_summary: メインCTAは「今日の集中プランを作る」に決定。",
                    "guildbotics_updated_at: 2026-05-29T20:22:47+09:00",
                    'guildbotics_retention: {"status":"active","kind":"current_fact"}',
                    "",
                    "# FocusFlow CTA",
                    "- メインCTAは「今日の集中プランを作る」。",
                ]
            ),
            "score": 0.8,
        }
    ]
    backend = CogneeMemoryBackend("yuki", adapter=adapter)

    context = backend.recall(
        MemoryQuery(
            person_id="yuki",
            thread_topic="FocusFlow CTA",
            latest_focus="メインCTAの最終決定",
            transcript="",
        )
    )

    assert len(context.items) == 1
    assert context.items[0].id == "focusflow-cta"
    assert "今日の集中プランを作る" in context.items[0].content
    assert context.items[0].metadata["updated_at"] == "2026-05-29T20:22:47+09:00"


def test_cognee_memory_backend_drops_non_memory_noise_nodes():
    adapter = MockCogneeAdapter()
    adapter.results = [
        {
            "search_result": "\n".join(
                [
                    "Nodes:",
                    "__node_content_start__",
                    "guildbotics_memory_id: focusflow-cta",
                    "guildbotics_title: FocusFlow CTA",
                    "guildbotics_summary: Current CTA",
                    "guildbotics_updated_at: 2026-05-29T20:58:11+09:00",
                    'guildbotics_retention: {"status":"active","kind":"current_fact"}',
                    "",
                    "# FocusFlow CTA",
                    "- メインCTAは今日の集中プランを作る。",
                    "__node_content_start__",
                    "random graph edge --[relationship_name: contains]--> focusflow cta",
                    "__node_content_start__",
                    "guildbotics_memory_id: focusflow-cta-transition-1780055800-644339 guildbotics_title: FocusFlow CTA Change guildbotics_summary: ... --[relationship_name: contains]--> focusflow cta",
                ]
            ),
            "score": 0.8,
        }
    ]
    backend = CogneeMemoryBackend("yuki", adapter=adapter)

    context = backend.recall(
        MemoryQuery(
            person_id="yuki",
            thread_topic="FocusFlow CTA",
            latest_focus="CTA方針",
            transcript="",
        )
    )

    assert len(context.items) == 1
    assert context.items[0].id == "focusflow-cta"
    assert context.items[0].title == "FocusFlow CTA"


def test_cognee_memory_backend_forgets_expired_latest_temporary_item():
    adapter = MockCogneeAdapter()
    adapter.results = [
        {
            "search_result": "\n".join(
                [
                    "guildbotics_memory_id: demo-cta",
                    "guildbotics_title: Demo CTA",
                    "guildbotics_summary: Temporary CTA treatment",
                    "guildbotics_updated_at: 2026-04-30T10:00:00+09:00",
                    'guildbotics_retention: {"status": "temporary", "expires_at": "2000-01-01T00:00:00+00:00"}',
                    "",
                    "# Demo CTA",
                    "- Make the CTA louder for today's demo.",
                ]
            ),
            "score": 0.9,
        }
    ]
    backend = CogneeMemoryBackend("aiko", adapter=adapter)

    context = backend.recall(
        MemoryQuery(
            person_id="aiko",
            thread_topic="Demo CTA",
            latest_focus="",
            transcript="",
        )
    )

    assert context.items == []
    assert adapter.forget_calls[0]["dataset_name"] == "guildbotics:person:aiko"
    assert adapter.forget_calls[0]["data_id"] == memory_data_id(
        "guildbotics:person:aiko", "demo-cta"
    )


def test_cognee_memory_backend_does_not_forget_old_expired_duplicate_when_latest_active():
    adapter = MockCogneeAdapter()
    adapter.results = [
        {
            "search_result": "\n".join(
                [
                    "guildbotics_memory_id: focusflow-cta",
                    "guildbotics_title: FocusFlow CTA",
                    "guildbotics_updated_at: 2026-04-30T10:00:00+09:00",
                    'guildbotics_retention: {"status": "temporary", "expires_at": "2000-01-01T00:00:00+00:00"}',
                    "",
                    "# FocusFlow CTA",
                    "- Demo-only CTA.",
                ]
            ),
            "score": 0.9,
        },
        {
            "search_result": "\n".join(
                [
                    "guildbotics_memory_id: focusflow-cta",
                    "guildbotics_title: FocusFlow CTA",
                    "guildbotics_updated_at: 2026-05-01T10:00:00+09:00",
                    "",
                    "# FocusFlow CTA",
                    "- Normal CTA is current.",
                ]
            ),
            "score": 0.8,
        },
    ]
    backend = CogneeMemoryBackend("aiko", adapter=adapter)

    context = backend.recall(
        MemoryQuery(
            person_id="aiko",
            thread_topic="FocusFlow CTA",
            latest_focus="",
            transcript="",
        )
    )

    assert len(context.items) == 1
    assert "Normal CTA is current" in context.items[0].content
    assert adapter.forget_calls == []


def test_cognee_memory_backend_keeps_transition_and_current_for_memory_update_consumer():
    adapter = MockCogneeAdapter()
    adapter.results = [
        {
            "search_result": "\n".join(
                [
                    "guildbotics_memory_id: focusflow-onboarding-policy-transition-1",
                    "guildbotics_title: FocusFlow onboarding policy Change",
                    'guildbotics_retention: {"status":"active","kind":"transition","subject_item_id":"focusflow-onboarding-policy"}',
                    "",
                    "# Transition",
                ]
            ),
            "score": 0.9,
        },
        {
            "search_result": "\n".join(
                [
                    "guildbotics_memory_id: focusflow-onboarding-policy",
                    "guildbotics_title: FocusFlow onboarding policy",
                    'guildbotics_retention: {"status":"active","kind":"current_fact"}',
                    "",
                    "# Current",
                ]
            ),
            "score": 0.8,
        },
    ]
    backend = CogneeMemoryBackend("aiko", adapter=adapter)

    context = backend.recall(
        MemoryQuery(
            person_id="aiko",
            thread_topic="FocusFlow onboarding",
            latest_focus="update",
            transcript="",
            metadata={"consumer": "memory_update"},
        )
    )

    assert len(context.items) == 2
    assert context.items[0].id == "focusflow-onboarding-policy-transition-1"
    assert context.items[1].id == "focusflow-onboarding-policy"


def test_cognee_memory_backend_remember_passes_normalized_document_to_adapter():
    adapter = MockCogneeAdapter()
    backend = CogneeMemoryBackend("yuki", adapter=adapter)

    result = backend.remember(
        MemoryUpdate(
            should_update=True,
            topic_id="focusflow-onboarding-experience",
            title="FocusFlow Onboarding Experience",
            summary="UX guidance for onboarding",
            memory="# FocusFlow Onboarding Experience\n\n## Decisions\n- Keep it light.",
            source={
                "type": "slack_thread",
                "service": "slack",
                "channel": "random",
                "thread_ts": "172.1",
            },
            scope={"person_id": "yuki"},
            metadata={"reason": "durable UX guidance"},
        )
    )

    call = adapter.remember_calls[0]
    assert call["dataset_name"] == "guildbotics:person:yuki"
    assert call["item_id"] == "focusflow-onboarding-experience"
    assert call["data_id"] == memory_data_id(
        "guildbotics:person:yuki", "focusflow-onboarding-experience"
    )
    assert "guildbotics_memory_id: focusflow-onboarding-experience" in call["content"]
    assert '"person_id": "yuki"' in call["content"]
    assert result.backend == "cognee"
    assert result.changed is True
    assert result.reference == "run-1"
    assert result.metadata["dataset"] == "guildbotics:person:yuki"


def test_cognee_memory_backend_marks_errored_remember_as_failed():
    adapter = MockCogneeAdapter()
    adapter.remember = lambda **kwargs: {"status": "errored", "error": "boom"}
    backend = CogneeMemoryBackend("yuki", adapter=adapter)

    result = backend.remember(
        MemoryUpdate(
            should_update=True,
            topic_id="focusflow-onboarding-experience",
            title="FocusFlow Onboarding Experience",
            summary="UX guidance for onboarding",
            memory="# FocusFlow Onboarding Experience",
            scope={"person_id": "yuki"},
        )
    )

    assert result.changed is False
    assert result.status == "failed"
    assert result.error == {"type": "CogneeRememberError", "message": "boom"}
    assert result.reference == ""
    assert result.metadata["raw_status"] == "errored"


def test_cognee_memory_backend_forget_uses_stable_data_id():
    adapter = MockCogneeAdapter()
    backend = CogneeMemoryBackend("yuki", adapter=adapter)

    result = backend.forget(
        MemoryForgetRequest(
            person_id="yuki",
            item_id="focusflow-onboarding-experience",
            reason="Replaced by a newer memory.",
            scope={"person_id": "yuki"},
        )
    )

    call = adapter.forget_calls[0]
    assert call["dataset_name"] == "guildbotics:person:yuki"
    assert call["data_id"] == memory_data_id(
        "guildbotics:person:yuki", "focusflow-onboarding-experience"
    )
    assert result.changed is True
    assert result.backend == "cognee"
    assert result.metadata["data_id"] == str(call["data_id"])


def test_cognee_async_runner_reuses_single_event_loop():
    async def running_loop_id():
        import asyncio

        return id(asyncio.get_running_loop())

    first_loop_id = cognee_memory_backend._run_async(running_loop_id())
    second_loop_id = cognee_memory_backend._run_async(running_loop_id())

    assert first_loop_id == second_loop_id


def test_default_cognee_adapter_recall_returns_empty_when_dataset_is_missing(
    monkeypatch,
):
    checked_datasets = []

    async def dataset_missing(dataset_name: str) -> bool:
        checked_datasets.append(dataset_name)
        return False

    monkeypatch.setattr(
        cognee_memory_backend,
        "_cognee_dataset_exists",
        dataset_missing,
    )

    adapter = DefaultCogneeAdapter.__new__(DefaultCogneeAdapter)

    assert (
        adapter.recall(
            query_text="FocusFlow onboarding",
            dataset_name="guildbotics:person:aiko",
            top_k=5,
        )
        == []
    )
    assert checked_datasets == ["guildbotics:person:aiko"]


def test_fake_memory_backend_uses_same_memory_context_contract():
    backend = FakeMemoryBackend("aiko")
    backend.remember(
        MemoryUpdate(
            should_update=True,
            topic_id="focusflow-onboarding-plan",
            title="FocusFlow Onboarding Plan",
            summary="FocusFlow onboarding decisions",
            memory="# FocusFlow Onboarding Plan",
            source={"type": "slack_thread"},
            scope={"person_id": "aiko"},
        )
    )

    context = backend.recall(
        MemoryQuery(
            person_id="aiko",
            thread_topic="FocusFlow Onboarding Plan",
            latest_focus="",
            transcript="",
            scope={"person_id": "aiko"},
        )
    )

    assert context.backend == "fake"
    assert context.query["dataset"] == "guildbotics:person:aiko"
    assert context.items[0].id == "focusflow-onboarding-plan"

    result = backend.forget(
        MemoryForgetRequest(
            person_id="aiko",
            item_id="focusflow-onboarding-plan",
            reason="User asked to forget.",
            scope={"person_id": "aiko"},
        )
    )
    context = backend.recall(
        MemoryQuery(
            person_id="aiko",
            thread_topic="FocusFlow Onboarding Plan",
            latest_focus="",
            transcript="",
            scope={"person_id": "aiko"},
        )
    )

    assert result.changed is True
    assert context.items == []


def test_fake_memory_backend_recall_skips_expired_temporary_memory():
    backend = FakeMemoryBackend("aiko")
    backend.remember(
        MemoryUpdate(
            should_update=True,
            topic_id="demo-cta",
            title="Demo CTA",
            summary="Temporary demo CTA",
            memory="# Demo CTA",
            scope={"person_id": "aiko"},
            retention={
                "status": "temporary",
                "expires_at": "2000-01-01T00:00:00+00:00",
            },
        )
    )

    context = backend.recall(
        MemoryQuery(
            person_id="aiko",
            thread_topic="Demo CTA",
            latest_focus="",
            transcript="",
            scope={"person_id": "aiko"},
        )
    )

    assert context.items == []


def test_dataset_name_for_person_is_stable():
    assert dataset_name_for_person("aiko") == "guildbotics:person:aiko"
    assert dataset_name_for_person("Yuki UX") == "guildbotics:person:Yuki-UX"


def test_cognee_environment_uses_openai_key_without_extra_llm_api_key(monkeypatch):
    for key in [
        "LLM_API_KEY",
        "LLM_PROVIDER",
        "EMBEDDING_API_KEY",
        "EMBEDDING_PROVIDER",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "ANTHROPIC_API_KEY",
    ]:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.lower(), raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    configure_cognee_environment_from_guildbotics_keys()

    assert os.environ["LLM_API_KEY"] == "openai-key"
    assert os.environ["LLM_PROVIDER"] == "openai"
    assert os.environ["EMBEDDING_API_KEY"] == "openai-key"
    assert os.environ["EMBEDDING_PROVIDER"] == "openai"


def test_cognee_environment_creates_default_system_directories(tmp_path, monkeypatch):
    for key in [
        "SYSTEM_ROOT_DIRECTORY",
        "COGNEE_LOGS_DIR",
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "ANTHROPIC_API_KEY",
    ]:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.lower(), raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    configure_cognee_environment_from_guildbotics_keys()

    assert (tmp_path / ".cognee" / "system" / "databases").is_dir()
    assert (tmp_path / ".cognee" / "logs").is_dir()


def test_cognee_environment_uses_google_key_for_llm_and_embeddings(monkeypatch):
    for key in [
        "LLM_API_KEY",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "EMBEDDING_API_KEY",
        "EMBEDDING_PROVIDER",
        "EMBEDDING_MODEL",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "ANTHROPIC_API_KEY",
    ]:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.lower(), raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")

    configure_cognee_environment_from_guildbotics_keys()

    assert os.environ["LLM_API_KEY"] == "google-key"
    assert os.environ["LLM_PROVIDER"] == "gemini"
    assert os.environ["LLM_MODEL"] == "gemini/gemini-flash-latest"
    assert os.environ["EMBEDDING_API_KEY"] == "google-key"
    assert os.environ["EMBEDDING_PROVIDER"] == "gemini"
    assert os.environ["EMBEDDING_MODEL"] == "gemini/gemini-embedding-001"


def test_cognee_environment_keeps_explicit_llm_api_key(monkeypatch):
    for key in ["LLM_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"]:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.lower(), raising=False)
    monkeypatch.setenv("LLM_API_KEY", "explicit-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    configure_cognee_environment_from_guildbotics_keys()

    assert os.environ["LLM_API_KEY"] == "explicit-key"
