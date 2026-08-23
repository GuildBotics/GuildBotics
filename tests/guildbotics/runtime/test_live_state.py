from __future__ import annotations

import json

from guildbotics.runtime.live_state import LIVE_LINE_LIMIT, LiveStatePublisher


def test_live_publisher_keeps_one_snapshot_and_bounds_strings() -> None:
    lines: list[str] = []
    publisher = LiveStatePublisher(
        "workspace",
        "device",
        lines.append,
        publisher_id="publisher",
        clock=lambda: "2026-08-23T12:00:00+00:00",
    )

    publisher.started("work", "run", "member", "x" * 200)
    publisher.progressed(
        "work",
        {
            "label_key": "label",
            "label_fallback": "fallback",
            "message": "message" * 1000,
            "message_params": {"secret": "value" * 1000},
        },
    )

    payload = json.loads(lines[-1])
    assert len(lines[-1].encode("utf-8")) <= LIVE_LINE_LIMIT
    assert payload["works"][0]["workflow_name"] == "x" * 120
    assert len(payload["works"][0]["presentation"]["message"]) == 120

    publisher.finished("work")
    assert json.loads(lines[-1])["works"] == []


def test_live_publish_failure_does_not_escape_execution() -> None:
    calls = 0

    def fail(_: str) -> None:
        nonlocal calls
        calls += 1
        raise OSError("Hub unavailable")

    publisher = LiveStatePublisher(
        "workspace", "device", fail, publisher_id="publisher"
    )

    publisher.started("work", None, "member", "workflow")

    assert calls == 1
    assert isinstance(publisher.last_error, OSError)
