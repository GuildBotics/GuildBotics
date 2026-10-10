"""The shared execution-to-live-state contract used by runtime publishers."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from guildbotics.utils.timestamps import utc_now_iso
from guildbotics.utils.workspace_sync_port import SHARED_RECORD_SCHEMA_VERSION

LIVE_LINE_LIMIT = 4096
LIVE_STRING_LIMIT = 120


class LiveStateTooLargeError(ValueError):
    """Raised when a live snapshot cannot fit the relay line contract."""


class LivePresentation(BaseModel):
    """Provider-neutral trace presentation shared by local and live views."""

    model_config = ConfigDict(extra="forbid")

    label_key: str = ""
    label_fallback: str = ""
    message_key: str = ""
    message: str = ""
    message_params: dict[str, Any] = Field(default_factory=dict)
    tone: str = "neutral"
    effort: str = ""


class LiveWork(BaseModel):
    """One currently running workflow or command."""

    model_config = ConfigDict(extra="forbid")

    work_id: str
    run_id: str | None = None
    member_id: str
    workflow_name: str
    presentation: LivePresentation | None = None
    retry_at: str | None = None


class LiveState(BaseModel):
    """A complete process heartbeat sent to the Hub relay."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SHARED_RECORD_SCHEMA_VERSION
    workspace_id: str
    device_id: str
    publisher_id: str
    observed_at: str
    works: list[LiveWork] = Field(default_factory=list)


class LiveStatePort(Protocol):
    """Lifecycle hooks shared by scheduler, event listener, and commands."""

    def started(
        self, work_id: str, run_id: str | None, member_id: str, workflow_name: str
    ) -> None: ...

    def progressed(
        self,
        work_id: str,
        presentation: Mapping[str, Any] | LivePresentation,
        retry_at: str | None = None,
    ) -> None: ...

    def finished(self, work_id: str) -> None: ...


class LiveStatePublisher:
    """Publish lifecycle snapshots without making live relay failure fatal."""

    def __init__(
        self,
        workspace_id: str,
        device_id: str,
        publish_line: Callable[[str], None],
        *,
        publisher_id: str | None = None,
        clock: Callable[[], str] = utc_now_iso,
    ) -> None:
        self.workspace_id = workspace_id
        self.device_id = device_id
        self.publisher_id = publisher_id or str(uuid4())
        self._publish_line = publish_line
        self._clock = clock
        self._works: dict[str, LiveWork] = {}
        self._lock = threading.RLock()
        self.last_error: Exception | None = None

    def started(
        self, work_id: str, run_id: str | None, member_id: str, workflow_name: str
    ) -> None:
        with self._lock:
            self._works[work_id] = LiveWork(
                work_id=_bounded(work_id),
                run_id=_bounded_or_none(run_id),
                member_id=_bounded(member_id),
                workflow_name=_bounded(workflow_name),
            )
        self.heartbeat()

    def progressed(
        self,
        work_id: str,
        presentation: Mapping[str, Any] | LivePresentation,
        retry_at: str | None = None,
    ) -> None:
        with self._lock:
            work = self._works.get(work_id)
            if work is None:
                return
            raw = (
                presentation.model_dump(mode="python")
                if isinstance(presentation, LivePresentation)
                else dict(presentation)
            )
            value = LivePresentation.model_validate(_bound_strings(raw))
            self._works[work_id] = work.model_copy(
                update={
                    "presentation": value,
                    "retry_at": _bounded_or_none(retry_at),
                }
            )
        self.heartbeat()

    def finished(self, work_id: str) -> None:
        with self._lock:
            self._works.pop(work_id, None)
        self.heartbeat()

    def heartbeat(self) -> LiveState:
        """Publish a snapshot, including an idle heartbeat when no work exists."""
        with self._lock:
            state = LiveState(
                workspace_id=_bounded(self.workspace_id),
                device_id=_bounded(self.device_id),
                publisher_id=_bounded(self.publisher_id),
                observed_at=self._clock(),
                works=list(self._works.values()),
            )
        try:
            line = _serialize(state)
            self._publish_line(line)
            self.last_error = None
        except Exception as exc:  # live availability must not fail execution
            self.last_error = exc
        return state

    def snapshot(self) -> LiveState:
        with self._lock:
            return LiveState(
                workspace_id=self.workspace_id,
                device_id=self.device_id,
                publisher_id=self.publisher_id,
                observed_at=self._clock(),
                works=list(self._works.values()),
            )


def _serialize(state: LiveState) -> str:
    payload = state.model_dump(mode="json")
    line = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    if len(line.encode("utf-8")) <= LIVE_LINE_LIMIT:
        return line
    compact = state.model_copy(
        update={
            "works": [
                work.model_copy(update={"presentation": None, "retry_at": None})
                for work in state.works
            ]
        }
    )
    while True:
        line = json.dumps(
            compact.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(line.encode("utf-8")) <= LIVE_LINE_LIMIT or not compact.works:
            break
        compact = compact.model_copy(update={"works": compact.works[:-1]})
    if len(line.encode("utf-8")) > LIVE_LINE_LIMIT:
        raise LiveStateTooLargeError(
            f"Live state exceeds the {LIVE_LINE_LIMIT}-byte line limit."
        )
    return line


def _bound_strings(value: Any) -> Any:
    if isinstance(value, str):
        return value[:LIVE_STRING_LIMIT]
    if isinstance(value, Mapping):
        return {str(key): _bound_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_bound_strings(item) for item in value]
    if isinstance(value, tuple):
        return [_bound_strings(item) for item in value]
    return value


def _bounded(value: str) -> str:
    return value[:LIVE_STRING_LIMIT]


def _bounded_or_none(value: str | None) -> str | None:
    return None if value is None else _bounded(value)
