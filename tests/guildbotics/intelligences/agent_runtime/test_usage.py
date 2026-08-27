from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from datetime import UTC, datetime

from guildbotics.intelligences.agent_runtime import usage as usage_module
from guildbotics.intelligences.agent_runtime.usage import (
    CLI_AGENT_USAGE_READERS,
    CliAgentUsageError,
    parse_claude_usage,
    parse_codex_rate_limits,
    parse_grok_billing,
    read_claude_usage,
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
    assert primary.label == ""
    assert snapshot.windows[1].window_minutes == 10_080
    assert snapshot.windows[1].label == ""
    assert snapshot.checked_at


def test_parse_codex_rate_limits_reads_snake_case_flat_shape() -> None:
    snapshot = parse_codex_rate_limits(
        {"rate_limits": {"primary": {"used_percent": 100, "resets_at": 2_000_000_000}}}
    )

    assert snapshot.limit_reached
    assert snapshot.windows[0].used_percent == 100
    assert snapshot.windows[0].window_minutes is None


def test_parse_codex_rate_limits_labels_extra_buckets() -> None:
    # Codex App Server reports a separate weekly quota (gpt-reserve) alongside
    # the main 5h/1w windows. Same duration is not a duplicate; the extra
    # bucket's limitName must survive so the UI can tell the two 1w meters
    # apart. Extra buckets may arrive first in the payload; the main windows
    # still lead the snapshot so the compact meters stay 5h, 1w, then extras.
    snapshot = parse_codex_rate_limits(
        {
            "rateLimitsByLimitId": {
                "base_model_inference": {
                    "limitId": "base_model_inference",
                    "limitName": "gpt-reserve",
                    "primary": {
                        "usedPercent": 0,
                        "windowDurationMins": 10_080,
                        "resetsAt": 1_788_427_524,
                    },
                    "secondary": None,
                },
                "codex": {
                    "limitId": "codex",
                    "limitName": None,
                    "primary": {
                        "usedPercent": 0,
                        "windowDurationMins": 300,
                        "resetsAt": 1_787_840_724,
                    },
                    "secondary": {
                        "usedPercent": 0,
                        "windowDurationMins": 10_080,
                        "resetsAt": 1_788_427_524,
                    },
                },
            }
        }
    )

    assert [
        (window.window, window.window_minutes, window.label)
        for window in snapshot.windows
    ] == [
        ("primary", 300, ""),
        ("secondary", 10_080, ""),
        ("primary", 10_080, "gpt-reserve"),
    ]


def test_parse_codex_rate_limits_uses_limit_id_when_name_missing() -> None:
    snapshot = parse_codex_rate_limits(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"usedPercent": 10, "windowDurationMins": 300},
                },
                "base_model_inference": {
                    "limitName": None,
                    "primary": {"usedPercent": 20, "windowDurationMins": 10_080},
                },
            }
        }
    )

    assert [window.label for window in snapshot.windows] == [
        "",
        "base_model_inference",
    ]


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


# Verbatim shape of the `claude -p /usage` result text on 2.1.224; the trailing
# contribution section must not produce windows.
_CLAUDE_USAGE_TEXT = """\
You are currently using your subscription to power your Claude Code usage

Current session: 24% used · resets Aug 8 at 11:10am (Asia/Tokyo)
Current week (all models): 56% used · resets Aug 8 at 10am (Asia/Tokyo)
Current week (Fable): 59% used · resets Aug 8 at 10am (Asia/Tokyo)

What's contributing to your limits usage?
Last 24h · 313 requests · 7 sessions
  51% of your usage was at >150k context
"""

_CLAUDE_NOW = datetime(2026, 8, 7, 22, 0, 0, tzinfo=UTC)


def test_parse_claude_usage_reads_session_and_weekly_windows() -> None:
    snapshot = parse_claude_usage(_CLAUDE_USAGE_TEXT, now=_CLAUDE_NOW)

    assert snapshot.agent == "claude"
    assert not snapshot.limit_reached
    assert [
        (window.window, window.used_percent, window.label, window.detail)
        for window in snapshot.windows
    ] == [
        ("session", 24.0, "", False),
        ("week", 56.0, "", False),
        ("current_week_fable", 59.0, "Fable", True),
    ]
    session, week, fable = snapshot.windows
    assert session.resets_at == "2026-08-08T11:10:00+09:00"
    assert session.window_minutes == 300
    assert week.resets_at == "2026-08-08T10:00:00+09:00"
    assert week.window_minutes == 10_080
    assert fable.window_minutes == 10_080
    assert snapshot.checked_at


def test_parse_claude_usage_marks_limit_at_full_window() -> None:
    snapshot = parse_claude_usage(
        "Current session: 100% used · resets Aug 8 at 11:10am (Asia/Tokyo)",
        now=_CLAUDE_NOW,
    )
    assert snapshot.limit_reached


def test_parse_claude_usage_drops_unusable_reset_times() -> None:
    # A missing timezone or an unknown one makes the instant ambiguous, and a
    # malformed phrase must not survive as a bogus timestamp.
    for reset in ("Aug 8 at 11:10am", "Aug 8 at 11:10am (Mars/Olympus)", "tomorrow"):
        snapshot = parse_claude_usage(
            f"Current session: 10% used · resets {reset}", now=_CLAUDE_NOW
        )
        assert snapshot.windows[0].resets_at == ""
        assert snapshot.windows[0].used_percent == 10.0


def test_parse_claude_usage_rolls_reset_into_next_year() -> None:
    snapshot = parse_claude_usage(
        "Current week (all models): 12% used · resets Jan 2 at 12am (UTC)",
        now=datetime(2026, 12, 30, 12, 0, 0, tzinfo=UTC),
    )
    assert snapshot.windows[0].resets_at == "2027-01-02T00:00:00+00:00"


def test_parse_claude_usage_tolerates_empty_and_malformed_input() -> None:
    for raw in ("", "no usage here", None, {"unexpected": "shape"}):
        snapshot = parse_claude_usage(raw, now=_CLAUDE_NOW)
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


class _ClaudeProcess:
    """Fake ``claude -p /usage`` returning one JSON document on stdout."""

    def __init__(self, payload: Any):
        self.payload = payload
        self.returncode: int | None = None

    async def communicate(self) -> tuple[bytes, bytes]:
        self.returncode = 0
        raw = (
            self.payload
            if isinstance(self.payload, bytes)
            else json.dumps(self.payload).encode()
        )
        return raw, b""


@pytest.mark.asyncio
async def test_read_claude_usage_probes_print_mode(monkeypatch, fake_terminate) -> None:
    process = _ClaudeProcess(
        {"is_error": False, "num_turns": 0, "result": _CLAUDE_USAGE_TEXT}
    )

    async def create_process(*args, **_kwargs):
        assert args == (
            "claude-bin",
            "-p",
            "/usage",
            "--output-format",
            "json",
            "--no-session-persistence",
        )
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    snapshot = await read_claude_usage("claude-bin")

    assert snapshot.agent == "claude"
    assert snapshot.windows[0].used_percent == 24.0
    assert [window.detail for window in snapshot.windows] == [False, False, True]
    assert fake_terminate == [process]


@pytest.mark.asyncio
async def test_read_claude_usage_raises_on_error_or_empty_panel(
    monkeypatch, fake_terminate
) -> None:
    # An error result, a panel without usage lines (e.g. API-key auth), and
    # non-JSON output must all surface as CliAgentUsageError.
    for payload in (
        {"is_error": True, "result": "Not available"},
        {"is_error": False, "result": "No usage panel"},
        b"claude exploded",
    ):
        process = _ClaudeProcess(payload)

        async def create_process(*_args, **_kwargs):
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

        with pytest.raises(CliAgentUsageError):
            await read_claude_usage("claude-bin")
        assert fake_terminate[-1] is process


def test_usage_reader_registry_covers_supported_tools() -> None:
    assert CLI_AGENT_USAGE_READERS == {
        "claude": read_claude_usage,
        "codex": read_codex_usage,
        "grok": read_grok_usage,
    }
