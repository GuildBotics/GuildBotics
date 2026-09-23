"""Redacted provider-neutral event recording."""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

from guildbotics.intelligences.agent_runtime.models import (
    AgentEvent,
    AgentEventKind,
    AgentExecutionContext,
    ConversationRecord,
)
from guildbotics.intelligences.cli_agents import cli_agent_info
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.observability.session_transcripts import should_record_agent_event

MAX_MESSAGE = 8_192
MAX_NETWORK_CANDIDATES = 32
_MAX_PORT = 65_535
_SENSITIVE_PARTS = ("token", "secret", "password", "credential", "authorization")
_INLINE_SECRET = re.compile(
    r"(?i)(?P<label>(?:--)?(?:access[-_]?token|api[-_]?key|token|password|secret|authorization))"
    r"(?P<separator>\s*(?:=|:)\s*|\s+)"
    r"(?P<value>(?:bearer\s+)?[^\s,;]+)"
)
_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s<>()\"']+")
_HOST_PORT = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"(?P<host>(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}):(?P<port>[1-9][0-9]{0,4})(?![0-9])"
)
_IP_PORT = re.compile(
    r"(?<![A-Za-z0-9:.])"
    r"(?:\[(?P<ipv6>[0-9A-Fa-f:.]+)\]|"
    r"(?P<ipv4>[0-9]{1,3}(?:\.[0-9]{1,3}){3}))"
    r":(?P<port>[1-9][0-9]{0,4})(?![0-9])"
)
_FAILED_STATUSES = frozenset({"error", "failed"})


def record_agent_event(
    event: AgentEvent,
    context: AgentExecutionContext,
    conversation: ConversationRecord,
) -> None:
    if not should_record_agent_event(event.kind.value, event.name):
        return
    payload: dict[str, Any] = {
        "name": event.name,
        "message": _redact_text(event.message or _default_message(event)),
        "command": _redact_text(event.command),
        "path": event.path[:MAX_MESSAGE],
        "approval": event.approval,
        "usage": dict(event.usage),
        "details": _redact(event.details),
    }
    record_correlated_event(
        event_type=f"agent_runtime.{event.kind.value}",
        default_source="agent_runtime",
        person_id=context.person_id,
        attributes={
            "agent.adapter": conversation.key.adapter,
            "agent.run_id": context.run_id,
            "agent.conversation_id": conversation.key.stable_id,
            "agent.conversation_generation": conversation.generation,
            "agent.provider_session_id": event.provider_session_id,
            "agent.provider_turn_id": event.provider_turn_id,
            "agent.context_cursor": context.context_cursor,
            "agent.lease_id": context.lease_id,
        },
        payload=payload,
    )
    evidence = _network_evidence(event)
    if evidence is not None:
        record_network_egress_candidates(
            text=_event_text(event),
            context=context,
            adapter_name=conversation.key.adapter,
            evidence=evidence,
        )


def record_network_egress_candidates(
    *,
    text: str,
    context: AgentExecutionContext,
    adapter_name: str,
    evidence: str,
) -> None:
    """Record destination-shaped clues from evidence a restricted turn emitted."""
    policy = context.contract.network
    if policy.mode == "unrestricted" or not text:
        return
    try:
        provider_domains = cli_agent_info(adapter_name).provision.turn_domains
    except ValueError:
        provider_domains = ()
    allowed_domains = (
        *provider_domains,
        *(policy.allowed_domains if policy.mode == "allowlist" else ()),
    )
    candidates = _destination_candidates(
        text[:MAX_MESSAGE],
        allowed_domains=allowed_domains,
        allow_local_network=policy.allow_local_network,
    )
    if not candidates:
        return
    record_correlated_event(
        event_type="agent_environment.network_egress_candidate",
        default_source="agent_runtime",
        person_id=context.person_id,
        attributes={
            "agent.adapter": adapter_name,
            "agent.run_id": context.run_id,
            "agent.context_cursor": context.context_cursor,
            "agent.lease_id": context.lease_id,
        },
        payload={
            "name": "network_egress_candidate",
            "message": (
                "A restricted turn emitted destination-shaped evidence that was "
                "not matched to a known allowed destination."
            ),
            "evidence": evidence,
            "candidates": candidates,
        },
    )


def _network_evidence(event: AgentEvent) -> str | None:
    if event.kind is AgentEventKind.FAILED:
        return "failed_event"
    if event.kind is AgentEventKind.COMMAND and _structured_failure(event.details):
        return "command_result"
    if event.kind is AgentEventKind.TOOL and _structured_failure(event.details):
        return "tool_failure"
    return None


def _structured_failure(details: dict[str, Any]) -> bool:
    return (
        details.get("is_error") is True
        or str(details.get("status") or "").lower() in _FAILED_STATUSES
    )


def _event_text(event: AgentEvent) -> str:
    values = [event.message, event.command]
    values.extend(
        str(event.details[key])
        for key in ("stderr", "log_tail", "output")
        if event.details.get(key)
    )
    return "\n".join(value for value in values if value)[:MAX_MESSAGE]


def _destination_candidates(
    text: str,
    *,
    allowed_domains: tuple[str, ...],
    allow_local_network: bool,
) -> list[dict[str, Any]]:
    found: dict[tuple[str, int | None], dict[str, Any]] = {}

    def add(host: str, port: int | None, kind: str) -> None:
        normalized, ip = _normalize_host(host)
        if (
            not normalized
            or (kind == "ip" and ip is None)
            or _destination_allowed(
                normalized,
                ip,
                allowed_domains=allowed_domains,
                allow_local_network=allow_local_network,
            )
        ):
            return
        key = (normalized, port)
        if port is None and any(existing[0] == normalized for existing in found):
            return
        if port is not None:
            found.pop((normalized, None), None)
        candidate = {
            "destination": normalized,
            "kind": "ip" if ip is not None else kind,
            **({"port": port} if port is not None else {}),
        }
        found[key] = candidate

    for match in _URL.finditer(text):
        try:
            parsed = urlsplit(match.group(0).rstrip(".,;"))
            add(parsed.hostname or "", parsed.port, "domain")
        except ValueError:
            continue
    for match in _HOST_PORT.finditer(text):
        port = int(match.group("port"))
        if port <= _MAX_PORT:
            add(match.group("host"), port, "domain")
    for match in _IP_PORT.finditer(text):
        port = int(match.group("port"))
        if port <= _MAX_PORT:
            add(match.group("ipv6") or match.group("ipv4"), port, "ip")
    return list(found.values())[:MAX_NETWORK_CANDIDATES]


def _normalize_host(
    host: str,
) -> tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address | None]:
    normalized = host.strip("[]").rstrip(".").lower()
    try:
        return normalized, ipaddress.ip_address(normalized)
    except ValueError:
        try:
            return normalized.encode("idna").decode("ascii"), None
        except UnicodeError:
            return "", None


def _destination_allowed(
    host: str,
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None,
    *,
    allowed_domains: tuple[str, ...],
    allow_local_network: bool,
) -> bool:
    if ip is not None:
        return allow_local_network and (
            ip.is_private or ip.is_loopback or ip.is_link_local
        )
    for rule in allowed_domains:
        normalized = rule.lower().rstrip(".")
        if normalized.startswith("*."):
            suffix = normalized[2:]
            if host == suffix or host.endswith(f".{suffix}"):
                return True
        elif host == normalized:
            return True
    return False


def _default_message(event: AgentEvent) -> str:
    """Build a human-readable message from the provider-neutral contract fields.

    Every AgentEvent record should carry a message so consumers can render it
    without provider-specific payload knowledge. Only uniform fields are used:
    ``command``, ``approval``, and the cross-adapter usage token keys.
    """
    if event.command:
        return event.command
    if event.approval:
        return event.approval
    parts = [
        f"{label} {event.usage[key]:,}"
        for key, label in (("input_tokens", "input"), ("output_tokens", "output"))
        if key in event.usage
    ]
    return f"{' · '.join(parts)} tokens" if parts else ""


def _redact(value: Any, *, key: str = "") -> Any:
    if any(part in key.lower() for part in _SENSITIVE_PARTS):
        return "***"
    if isinstance(value, dict):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, key=key) for item in value[:100]]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    return _redact_text(str(value))


def _redact_text(value: str) -> str:
    bounded = value[:MAX_MESSAGE]
    return _INLINE_SECRET.sub(
        lambda match: f"{match.group('label')}{match.group('separator')}***",
        bounded,
    )
