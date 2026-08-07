from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from guildbotics.intelligences.agent_runtime import usage as usage_module
from guildbotics.intelligences.agent_runtime.usage import (
    CLI_AGENT_USAGE_READERS,
    CliAgentUsageError,
    parse_codex_rate_limits,
    parse_grok_billing,
    read_codex_usage,
    read_grok_usage,
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


_GROK_BILLING = {
    "config": {
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-08-07T07:37:18.756767+00:00",
            "end": "2026-08-14T07:37:18.756767+00:00",
        },
        "onDemandCap": {"val": 0},
        "onDemandUsed": {"val": 0},
        "prepaidBalance": {"val": 0},
        "isUnifiedBillingUser": True,
        "billingPeriodStart": "2026-08-07T07:37:18.756767+00:00",
        "billingPeriodEnd": "2026-08-14T07:37:18.756767+00:00",
    },
    "subscription_tier": "SuperGrok Lite",
}


def test_parse_grok_billing_reads_percentless_subscription_window() -> None:
    snapshot = parse_grok_billing(
        _GROK_BILLING, {"authenticated": True, "meta": {"gate": None}}
    )

    assert snapshot.agent == "grok"
    assert not snapshot.limit_reached
    assert len(snapshot.windows) == 1
    window = snapshot.windows[0]
    assert window.window == "subscription"
    assert window.used_percent is None
    assert window.resets_at == "2026-08-14T07:37:18.756767+00:00"
    assert window.window_minutes == 10_080
    assert snapshot.checked_at


def test_parse_grok_billing_reports_on_demand_credit_percent() -> None:
    billing = {
        "config": {
            **_GROK_BILLING["config"],
            "onDemandCap": {"val": 200},
            "onDemandUsed": {"val": 51},
        }
    }
    snapshot = parse_grok_billing(billing, {})

    on_demand = snapshot.windows[1]
    assert on_demand.window == "on_demand"
    assert on_demand.used_percent == 25.5
    assert not snapshot.limit_reached


def test_parse_grok_billing_marks_limit_on_gate_or_exhausted_credits() -> None:
    gated = parse_grok_billing(
        _GROK_BILLING,
        {"authenticated": True, "meta": {"gate": {"reason": "usage_limit"}}},
    )
    assert gated.limit_reached

    exhausted = parse_grok_billing(
        {
            "config": {
                **_GROK_BILLING["config"],
                "onDemandCap": {"val": 100},
                "onDemandUsed": {"val": 100},
            }
        },
        {},
    )
    assert exhausted.limit_reached


def test_parse_grok_billing_tolerates_empty_and_malformed_input() -> None:
    for billing, subscription in ((None, None), ({}, {}), ({"config": "bad"}, "bad")):
        snapshot = parse_grok_billing(billing, subscription)
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
async def test_read_codex_usage_raises_on_rpc_error(
    monkeypatch, fake_terminate
) -> None:
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


class _GrokProcess:
    """Fake ``grok agent stdio`` speaking just enough ACP for the probe."""

    def __init__(self, auth_error: Any = None):
        self.stdout = asyncio.StreamReader()
        self.stdin = _Writer(self)
        self.returncode: int | None = None
        self.messages: list[dict[str, Any]] = []
        self.auth_error = auth_error

    def handle(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        if "method" not in message or "id" not in message:
            return
        request_id = message["id"]
        method = message["method"]
        if method == "initialize":
            # Grok interleaves private notifications with responses; the
            # probe must skip them.
            self._feed(
                {"jsonrpc": "2.0", "method": "_x.ai/settings/update", "params": {}}
            )
            self._feed(
                {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": 1}}
            )
        elif method == "authenticate":
            if self.auth_error is not None:
                self._feed(
                    {"jsonrpc": "2.0", "id": request_id, "error": self.auth_error}
                )
            else:
                self._feed({"jsonrpc": "2.0", "id": request_id, "result": {}})
        elif method == "_x.ai/billing":
            self._feed({"jsonrpc": "2.0", "id": request_id, "result": _GROK_BILLING})
        elif method == "_x.ai/auth/check_subscription":
            self._feed(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"authenticated": True, "meta": {"gate": None}},
                }
            )

    def _feed(self, message: dict[str, Any]) -> None:
        self.stdout.feed_data(json.dumps(message).encode() + b"\n")


@pytest.mark.asyncio
async def test_read_grok_usage_probes_agent_stdio(monkeypatch, fake_terminate) -> None:
    process = _GrokProcess()

    async def create_process(*args, **_kwargs):
        assert args == ("grok-bin", "--no-auto-update", "agent", "stdio")
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    snapshot = await read_grok_usage("grok-bin")

    assert [message.get("method") for message in process.messages] == [
        "initialize",
        "authenticate",
        "_x.ai/billing",
        "_x.ai/auth/check_subscription",
    ]
    assert process.messages[1]["params"] == {"methodId": "cached_token"}
    assert snapshot.agent == "grok"
    assert snapshot.windows[0].used_percent is None
    assert snapshot.windows[0].resets_at == "2026-08-14T07:37:18.756767+00:00"
    assert not snapshot.limit_reached
    assert fake_terminate == [process]


@pytest.mark.asyncio
async def test_read_grok_usage_raises_without_saved_login(
    monkeypatch, fake_terminate
) -> None:
    process = _GrokProcess(auth_error={"code": -32000, "message": "not logged in"})

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(CliAgentUsageError):
        await read_grok_usage("grok-bin")
    assert fake_terminate == [process]


def test_usage_reader_registry_covers_codex_and_grok() -> None:
    assert CLI_AGENT_USAGE_READERS == {
        "codex": read_codex_usage,
        "grok": read_grok_usage,
    }
