"""State-based system health alerts derived from structured diagnostics."""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from guildbotics.app_api.models import (
    RuntimeStatus,
    SystemAlert,
    SystemAlertAction,
    SystemAlertCode,
    SystemAlertSeverity,
    SystemAlertsResponse,
)
from guildbotics.observability.diagnostics_store import (
    DiagnosticsCursor,
    DiagnosticsStore,
)

_DIAGNOSTIC_CREDENTIAL_CODES = {
    "github": {"github_access", "github_project_access"},
    "slack": {
        "slack_access",
        "slack_app_token",
        "slack_app_token_invalid",
        "slack_bot_auth",
    },
    "cli_agent": {"cli_agent_brain"},
    "llm": {"llm_api_key", "llm_live_call"},
}
_VERIFY_CREDENTIAL_CODES = {"llm_api_key": "llm"}
_CREDENTIAL_ALERT_CODES: dict[str, SystemAlertCode] = {
    "github": "credential_github",
    "slack": "credential_slack",
    "cli_agent": "credential_cli_agent",
    "llm": "credential_llm",
}
_IGNORED_COMMAND_FAILURES = {
    "cancelled",
    "person_not_found",
    "person_selection_required",
}
_RELEVANT_EVENT_TYPES = frozenset(
    {
        "command.failed",
        "command.finished",
        "credential.failed",
        "diagnostics.completed",
        "scheduler.failed",
        "scheduler.running",
        "scheduler.worker_failed",
        "verify.completed",
        "workflow.rate_limited",
    }
)
_STATE_VERSION = 3
_ALERT_ID_PARTS = 3


class SystemAlertService:
    """Fold diagnostics and current runtime state into unresolved alerts."""

    def __init__(self, store: DiagnosticsStore | None) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._state_path: Path | None = None
        self._cursor: DiagnosticsCursor | None = None
        self._alerts: dict[str, SystemAlert] = {}
        self._dismissed: set[str] = set()

    def list_alerts(self, runtime: RuntimeStatus) -> SystemAlertsResponse:
        with self._lock:
            self._refresh_state()
            changed = False
            if self._store is not None:
                records, cursor = self._store.records_after(
                    self._cursor, includes=_is_relevant_record
                )
                for record in records:
                    self._apply_record(self._alerts, record)
                changed = cursor != self._cursor
                self._cursor = cursor
            changed = self._reconcile_runtime_dismissals(runtime) or changed
            if changed:
                self._save_state()
            alerts = {
                key: alert.model_copy(deep=True) for key, alert in self._alerts.items()
            }
            dismissed = set(self._dismissed)
        self._apply_runtime(alerts, runtime, dismissed)
        ordered = sorted(
            alerts.values(),
            key=lambda alert: (alert.severity != "critical", alert.opened_at, alert.id),
        )
        return SystemAlertsResponse(alerts=ordered)

    def dismiss(self, alert_id: str) -> None:
        """Dismiss the current occurrence; a later occurrence reopens it."""
        with self._lock:
            self._refresh_state()
            self._alerts.pop(alert_id, None)
            self._dismissed.add(alert_id)
            self._save_state()

    def _refresh_state(self) -> None:
        path = (
            self._store.path.with_name("system-alerts.json")
            if self._store is not None
            else None
        )
        if path == self._state_path:
            return
        self._state_path = path
        self._cursor = None
        self._alerts = {}
        self._dismissed = set()
        if path is None:
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            version = payload.get("version")
            if version == _STATE_VERSION:
                self._cursor = DiagnosticsCursor.from_dict(payload.get("cursor"))
            elif self._store is not None:
                # Preserve existing alert/dismissal state without replaying rows
                # already represented by the previous hash-based state format.
                self._cursor = self._store.current_cursor()
            self._alerts = {
                alert.id: alert
                for raw in payload.get("alerts", [])
                if isinstance(raw, dict)
                for alert in [SystemAlert.model_validate(raw)]
            }
            dismissed = payload.get("dismissed", [])
            if isinstance(dismissed, list):
                self._dismissed = {str(item) for item in dismissed}
            if version != _STATE_VERSION:
                self._save_state()
        except (OSError, ValueError, TypeError):
            self._cursor = None
            self._alerts = {}
            self._dismissed = set()

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        payload = {
            "version": _STATE_VERSION,
            "cursor": self._cursor.to_dict() if self._cursor is not None else None,
            "alerts": [alert.model_dump() for alert in self._alerts.values()],
            "dismissed": sorted(self._dismissed),
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            return

    def _apply_record(
        self, alerts: dict[str, SystemAlert], record: dict[str, Any]
    ) -> None:
        event_type = str(record.get("type") or "")
        if event_type == "diagnostics.completed":
            self._apply_diagnostic_snapshot(alerts, record)
        elif event_type == "verify.completed":
            self._apply_verify_snapshot(alerts, record)
        elif event_type == "workflow.rate_limited":
            self._open_execution_alert(alerts, record, code="rate_limited")
        elif event_type == "command.failed":
            if str(_payload(record).get("code") or "") not in _IGNORED_COMMAND_FAILURES:
                self._open_execution_alert(alerts, record, code="command_failed")
        elif event_type == "command.finished":
            self._resolve_execution_alerts(alerts, record)
        elif event_type == "credential.failed":
            self._open_credential_alert(alerts, record)
        elif event_type == "scheduler.worker_failed":
            person_id = str(record.get("person_id") or "")
            self._open(
                alerts,
                key=f"runtime:worker:{person_id}",
                code="worker_stopped",
                severity="critical",
                record=record,
                person_id=person_id,
                actions=["service", "diagnostics"],
            )
        elif event_type == "scheduler.failed":
            self._open(
                alerts,
                key="runtime:scheduler",
                code="scheduler_failed",
                severity="critical",
                record=record,
                actions=["service", "diagnostics"],
            )
        elif event_type == "scheduler.running":
            for key in list(alerts):
                if key == "runtime:scheduler" or key.startswith("runtime:worker:"):
                    alerts.pop(key, None)
                    self._dismissed.discard(key)

    def _apply_diagnostic_snapshot(
        self, alerts: dict[str, SystemAlert], record: dict[str, Any]
    ) -> None:
        checks = _checks(record)
        payload = _payload(record)
        if not str(payload.get("scope_person_id") or ""):
            self._resolve_removed_credentials(alerts, payload, checks)
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for check in checks:
            section = str(check.get("section") or "")
            code = str(check.get("code") or "")
            if code not in _DIAGNOSTIC_CREDENTIAL_CODES.get(section, set()):
                continue
            person_id = "" if section == "llm" else str(check.get("person_id") or "")
            grouped[(section, person_id)].append(check)
        self._apply_credential_groups(alerts, record, grouped)

    def _resolve_removed_credentials(
        self,
        alerts: dict[str, SystemAlert],
        payload: dict[str, Any],
        checks: list[dict[str, Any]],
    ) -> None:
        if "active_members" in payload:
            active_members = {
                str(item) for item in payload.get("active_members", []) if str(item)
            }
            for key in list(alerts):
                parts = key.split(":", 2)
                if (
                    len(parts) == _ALERT_ID_PARTS
                    and parts[0] == "credential"
                    and parts[2]
                    and parts[2] not in active_members
                ):
                    alerts.pop(key, None)
        check_codes = {str(check.get("code") or "") for check in checks}
        if "github_not_configured" in check_codes:
            _remove_alert_prefix(alerts, "credential:github:")
        if "slack_not_configured" in check_codes:
            _remove_alert_prefix(alerts, "credential:slack:")

    def _apply_verify_snapshot(
        self, alerts: dict[str, SystemAlert], record: dict[str, Any]
    ) -> None:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for check in _checks(record):
            section = _VERIFY_CREDENTIAL_CODES.get(str(check.get("code") or ""))
            if section is None:
                continue
            context = check.get("context")
            person_id = (
                str(context.get("person_id") or "") if isinstance(context, dict) else ""
            )
            grouped[(section, person_id)].append(check)
        self._apply_credential_groups(alerts, record, grouped)
        if not grouped:
            alerts.pop("credential:llm:", None)

    def _apply_credential_groups(
        self,
        alerts: dict[str, SystemAlert],
        record: dict[str, Any],
        grouped: dict[tuple[str, str], list[dict[str, Any]]],
    ) -> None:
        for (section, person_id), checks in grouped.items():
            key = f"credential:{section}:{person_id}"
            if any(check.get("status") == "error" for check in checks):
                self._open(
                    alerts,
                    key=key,
                    code=_CREDENTIAL_ALERT_CODES[section],
                    severity="critical",
                    record=record,
                    person_id=person_id,
                    actions=["diagnostics", "setup"],
                )
            elif any(check.get("status") == "ok" for check in checks):
                alerts.pop(key, None)

    def _open_credential_alert(
        self, alerts: dict[str, SystemAlert], record: dict[str, Any]
    ) -> None:
        provider = str(_payload(record).get("provider") or "")
        if provider not in _CREDENTIAL_ALERT_CODES:
            return
        person_id = str(record.get("person_id") or "")
        trace_id = str(record.get("trace_id") or "")
        self._remove_execution_alerts_for_cause(alerts, person_id, trace_id)
        actions: list[SystemAlertAction] = ["diagnostics", "setup"]
        if trace_id:
            actions.append("trace")
        self._open(
            alerts,
            key=f"credential:{provider}:{person_id}",
            code=_CREDENTIAL_ALERT_CODES[provider],
            severity="critical",
            record=record,
            person_id=person_id,
            trace_id=trace_id,
            actions=actions,
        )

    def _open_execution_alert(
        self,
        alerts: dict[str, SystemAlert],
        record: dict[str, Any],
        *,
        code: SystemAlertCode,
    ) -> None:
        person_id = str(record.get("person_id") or "")
        command = str(record.get("command") or _payload(record).get("command") or "")
        key = f"{code}:{person_id}:{command}"
        if self._has_credential_alert_for_cause(alerts, person_id, record):
            return
        if code == "command_failed":
            if f"rate_limited:{person_id}:{command}" in alerts:
                return
        elif code == "rate_limited":
            alerts.pop(f"command_failed:{person_id}:{command}", None)
        self._open(
            alerts,
            key=key,
            code=code,
            severity="warning",
            record=record,
            person_id=person_id,
            command=command,
            trace_id=str(record.get("trace_id") or ""),
            actions=["trace"] if record.get("trace_id") else ["diagnostics"],
        )

    def _resolve_execution_alerts(
        self, alerts: dict[str, SystemAlert], record: dict[str, Any]
    ) -> None:
        person_id = str(record.get("person_id") or "")
        trace_id = str(record.get("trace_id") or "")
        prefixes = (f"command_failed:{person_id}:", f"rate_limited:{person_id}:")
        for key, alert in list(alerts.items()):
            if not key.startswith(prefixes):
                continue
            if alert.code == "rate_limited" and trace_id and alert.trace_id == trace_id:
                continue
            alerts.pop(key, None)

    def _has_credential_alert_for_cause(
        self,
        alerts: dict[str, SystemAlert],
        person_id: str,
        record: dict[str, Any],
    ) -> bool:
        trace_id = str(record.get("trace_id") or "")
        return any(
            key.startswith("credential:")
            and alert.person_id == person_id
            and bool(trace_id)
            and alert.trace_id == trace_id
            for key, alert in alerts.items()
        )

    def _remove_execution_alerts_for_cause(
        self, alerts: dict[str, SystemAlert], person_id: str, trace_id: str
    ) -> None:
        if not trace_id:
            return
        for key, alert in list(alerts.items()):
            if (
                key.startswith(("command_failed:", "rate_limited:"))
                and alert.person_id == person_id
                and alert.trace_id == trace_id
            ):
                alerts.pop(key, None)

    def _apply_runtime(
        self,
        alerts: dict[str, SystemAlert],
        runtime: RuntimeStatus,
        dismissed: set[str],
    ) -> None:
        scheduler = runtime.scheduler
        if scheduler.state == "failed":
            self._open_runtime(
                alerts,
                dismissed,
                key="runtime:scheduler",
                code="scheduler_failed",
                timestamp=scheduler.stopped_at or scheduler.started_at or "",
            )
        for person_id in runtime.events.events_auth_failed_persons:
            self._open_runtime(
                alerts,
                dismissed,
                key=f"credential:slack:{person_id}",
                code="credential_slack",
                timestamp=runtime.events.started_at or "",
                person_id=person_id,
                actions=["diagnostics", "setup"],
            )

    def _reconcile_runtime_dismissals(self, runtime: RuntimeStatus) -> bool:
        before = set(self._dismissed)
        if runtime.scheduler.state != "failed":
            self._dismissed.discard("runtime:scheduler")
        auth_failed = set(runtime.events.events_auth_failed_persons)
        for key in list(self._dismissed):
            if (
                key.startswith("credential:slack:")
                and key.rsplit(":", 1)[-1] not in auth_failed
            ):
                self._dismissed.discard(key)
        return before != self._dismissed

    def _open_runtime(
        self,
        alerts: dict[str, SystemAlert],
        dismissed: set[str],
        *,
        key: str,
        code: SystemAlertCode,
        timestamp: str,
        person_id: str = "",
        actions: list[SystemAlertAction] | None = None,
    ) -> None:
        if key in alerts or key in dismissed:
            return
        alerts[key] = SystemAlert(
            id=key,
            code=code,
            severity="critical",
            opened_at=timestamp,
            updated_at=timestamp,
            person_id=person_id,
            actions=actions or ["service", "diagnostics"],
        )

    def _open(
        self,
        alerts: dict[str, SystemAlert],
        *,
        key: str,
        code: SystemAlertCode,
        severity: SystemAlertSeverity,
        record: dict[str, Any],
        person_id: str = "",
        command: str = "",
        trace_id: str = "",
        actions: list[SystemAlertAction],
    ) -> None:
        self._dismissed.discard(key)
        timestamp = str(record.get("timestamp") or "")
        current = alerts.get(key)
        if current is None:
            alerts[key] = SystemAlert(
                id=key,
                code=code,
                severity=severity,
                opened_at=timestamp,
                updated_at=timestamp,
                person_id=person_id,
                command=command,
                trace_id=trace_id,
                actions=actions,
            )
            return
        alerts[key] = current.model_copy(
            update={
                "updated_at": timestamp,
                "occurrence_count": current.occurrence_count + 1,
                "trace_id": trace_id or current.trace_id,
            }
        )


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def _checks(record: dict[str, Any]) -> list[dict[str, Any]]:
    checks = _payload(record).get("checks")
    if not isinstance(checks, list):
        return []
    return [check for check in checks if isinstance(check, dict)]


def _is_relevant_record(record: dict[str, Any]) -> bool:
    return record.get("kind") == "event" and record.get("type") in _RELEVANT_EVENT_TYPES


def _remove_alert_prefix(alerts: dict[str, SystemAlert], prefix: str) -> None:
    for key in list(alerts):
        if key.startswith(prefix):
            alerts.pop(key, None)
