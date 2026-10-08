"""Why each workspace key was last refused, read from this device's records."""

from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.events import EventBus
from guildbotics.app_api.inference_failures import latest_inference_failures
from guildbotics.observability.diagnostics_store import DiagnosticsStore


def _call(
    store: DiagnosticsStore,
    status: str,
    timestamp: str,
    *,
    service: str = "llm",
    provider: str = "openai",
    category: str = "",
) -> None:
    store.record(
        {
            "kind": "event",
            "type": f"span.{status}",
            "timestamp": timestamp,
            "attributes": {
                "credential.provider": service,
                **({"llm.provider": provider} if provider else {}),
                **(
                    {
                        "error.category": category,
                        "error.status_code": 429,
                        "error.response": f"{category} answer",
                    }
                    if category
                    else {}
                ),
            },
            "payload": {},
        }
    )


def test_each_keys_latest_call_decides_its_state(tmp_path) -> None:
    store = DiagnosticsStore(tmp_path / "diagnostics.jsonl", memory_limit=1)
    _call(store, "failed", "2026-10-08T10:00:00Z", category="credit")
    # A later call of the same key that succeeds clears it.
    _call(store, "finished", "2026-10-08T10:05:00Z")
    _call(
        store, "failed", "2026-10-08T10:00:00Z", provider="anthropic", category="credit"
    )
    # Arrival order and timezone spelling do not decide recency.
    _call(
        store,
        "failed",
        "2026-10-08T19:10:00+09:00",
        provider="anthropic",
        category="rate_limit",
    )
    _call(
        store,
        "finished",
        "2026-10-08T19:00:00+09:00",
        provider="anthropic",
    )
    _call(
        store,
        "failed",
        "2026-10-08T10:00:00Z",
        service="jev",
        provider="",
        category="authentication",
    )
    # A call given up on says nothing of the key.
    _call(store, "failed", "2026-10-08T11:00:00Z", service="jev", provider="")
    # Nor does a span of anything else.
    store.record(
        {
            "kind": "event",
            "type": "span.failed",
            "timestamp": "2026-10-08T12:00:00Z",
            "attributes": {"agent.kind": "cli_agent", "error.category": "credit"},
        }
    )

    failures = latest_inference_failures(store)

    assert failures.model_dump() == {
        "llm": {
            "anthropic": {
                "category": "rate_limit",
                "timestamp": "2026-10-08T19:10:00+09:00",
                "status_code": 429,
                "response": "rate_limit answer",
            }
        },
        "jev": {
            "category": "authentication",
            "timestamp": "2026-10-08T10:00:00Z",
            "status_code": 429,
            "response": "authentication answer",
        },
    }


def test_no_store_or_no_calls(tmp_path) -> None:
    assert latest_inference_failures(None).model_dump() == {"llm": {}, "jev": None}
    empty = DiagnosticsStore(tmp_path / "empty.jsonl")
    assert latest_inference_failures(empty).model_dump() == {"llm": {}, "jev": None}


def test_api_reads_the_device_records_and_requires_token(tmp_path) -> None:
    path = tmp_path / "diagnostics.jsonl"
    _call(DiagnosticsStore(path), "failed", "2026-10-08T10:00:00Z", category="credit")
    store = DiagnosticsStore(path)
    app = create_app(
        session_token="secret", event_bus=EventBus(store=store), diagnostics_store=store
    )
    with TestClient(app) as client:
        url = "/intelligences/inference-failures"
        assert client.get(url).status_code == 401
        response = client.get(url, headers={"X-GuildBotics-Session-Token": "secret"})

    assert response.status_code == 200
    assert response.json() == {
        "llm": {
            "openai": {
                "category": "credit",
                "timestamp": "2026-10-08T10:00:00Z",
                "status_code": 429,
                "response": "credit answer",
            }
        },
        "jev": None,
    }
