from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from guildbotics.intelligences.agent_runtime import usage as usage_module
from guildbotics.intelligences.agent_runtime.usage import (
    CliAgentUsageError,
    parse_codex_rate_limits,
    read_codex_usage,
)


def test_parse_codex_rate_limits_reads_camel_case_buckets() -> None:
    # The duration field name has changed across codex versions; one window
    # uses the current name and the other a legacy alias.
    snapshot = parse_codex_rate_limits(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {
                        "usedPercent": 42.5,
                        "resetsAt": 2_000_000_000,
                        "windowDurationMins": 300,
                    },
                    "secondary": {
                        "usedPercent": 78,
                        "resetsAt": 2_000_500_000,
                        "windowMinutes": 10_080,
                    },
                }
            }
        }
    )

    assert snapshot.agent == "codex"
    assert not snapshot.limit_reached
    assert [window.window for window in snapshot.windows] == ["primary", "secondary"]
    primary = snapshot.windows[0]
    assert primary.used_percent == 42.5
    assert primary.resets_at == "2033-05-18T03:33:20+00:00"
    assert primary.window_minutes == 300
    assert snapshot.windows[1].window_minutes == 10_080
    assert snapshot.checked_at


def test_parse_codex_rate_limits_reads_snake_case_flat_shape() -> None:
    snapshot = parse_codex_rate_limits(
        {"rate_limits": {"primary": {"used_percent": 100, "resets_at": 2_000_000_000}}}
    )

    assert snapshot.limit_reached
    assert snapshot.windows[0].used_percent == 100
    assert snapshot.windows[0].window_minutes is None


def test_parse_codex_rate_limits_honors_reached_type_flag() -> None:
    snapshot = parse_codex_rate_limits(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "rateLimitReachedType": "primary",
                    "primary": {"usedPercent": 55},
                }
            }
        }
    )

    assert snapshot.limit_reached


def test_parse_codex_rate_limits_drops_unparseable_reset_values() -> None:
    # Anything that is neither epoch seconds nor an ISO timestamp must not
    # leak to the UI as a bogus date string.
    bad = parse_codex_rate_limits(
        {"rate_limits": {"primary": {"used_percent": 10, "resets_at": "bad"}}}
    )
    assert bad.windows[0].resets_at == ""

    iso = parse_codex_rate_limits(
        {
            "rate_limits": {
                "primary": {
                    "used_percent": 10,
                    "resets_at": "2026-07-18T05:00:00+00:00",
                }
            }
        }
    )
    assert iso.windows[0].resets_at == "2026-07-18T05:00:00+00:00"


def test_parse_codex_rate_limits_tolerates_empty_and_malformed_input() -> None:
    for raw in ({}, {"rate_limits": {}}, {"rateLimitsByLimitId": {"x": "bad"}}, None):
        snapshot = parse_codex_rate_limits(raw)
        assert snapshot.windows == []
        assert not snapshot.limit_reached


class _Writer:
    def __init__(self, process: "_Process") -> None:
        self.process = process

    def write(self, data: bytes) -> None:
        for line in data.splitlines():
            self.process.handle(json.loads(line))

    async def drain(self) -> None:
        return None


class _Process:
    """Fake ``codex app-server`` speaking just enough JSONL RPC for the probe."""

    def __init__(self, rate_limits: dict[str, Any] | None = None, error: Any = None):
        self.stdout = asyncio.StreamReader()
        self.stdin = _Writer(self)
        self.returncode: int | None = None
        self.messages: list[dict[str, Any]] = []
        self.rate_limits = rate_limits if rate_limits is not None else {}
        self.error = error

    def handle(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        if "method" not in message or "id" not in message:
            return
        request_id = message["id"]
        if message["method"] == "initialize":
            self._feed({"jsonrpc": "2.0", "id": request_id, "result": {}})
            self._feed({"jsonrpc": "2.0", "method": "loginStatus", "params": {}})
        elif message["method"] == "account/rateLimits/read":
            if self.error is not None:
                self._feed({"jsonrpc": "2.0", "id": request_id, "error": self.error})
            else:
                self._feed(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"rateLimits": self.rate_limits},
                    }
                )

    def _feed(self, message: dict[str, Any]) -> None:
        self.stdout.feed_data(json.dumps(message).encode() + b"\n")


@pytest.fixture
def fake_terminate(monkeypatch) -> list[Any]:
    terminated: list[Any] = []

    async def terminate(process, **_kwargs) -> None:
        terminated.append(process)
        process.returncode = 0

    monkeypatch.setattr(usage_module, "terminate_process_tree", terminate)
    return terminated


@pytest.mark.asyncio
async def test_read_codex_usage_probes_app_server(monkeypatch, fake_terminate) -> None:
    process = _Process(
        rate_limits={"primary": {"usedPercent": 12.0, "resetsAt": 2_000_000_000}}
    )

    async def create_process(*args, **_kwargs):
        assert args[:2] == ("codex-bin", "app-server")
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    snapshot = await read_codex_usage("codex-bin")

    assert [message.get("method") for message in process.messages] == [
        "initialize",
        "initialized",
        "account/rateLimits/read",
    ]
    assert snapshot.windows[0].used_percent == 12.0
    assert not snapshot.limit_reached
    assert fake_terminate == [process]


@pytest.mark.asyncio
async def test_read_codex_usage_raises_on_rpc_error(monkeypatch, fake_terminate) -> None:
    process = _Process(error={"code": -32601, "message": "method not found"})

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(CliAgentUsageError):
        await read_codex_usage("codex-bin")
    assert fake_terminate == [process]


@pytest.mark.asyncio
async def test_read_codex_usage_raises_when_stream_closes(
    monkeypatch, fake_terminate
) -> None:
    process = _Process()
    process.stdout.feed_eof()
    process.handle = lambda _message: None  # type: ignore[method-assign]

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(CliAgentUsageError):
        await read_codex_usage("codex-bin")


@pytest.mark.asyncio
async def test_read_codex_usage_raises_when_start_fails(monkeypatch) -> None:
    async def create_process(*_args, **_kwargs):
        raise OSError("missing binary")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(CliAgentUsageError):
        await read_codex_usage("codex-bin")
