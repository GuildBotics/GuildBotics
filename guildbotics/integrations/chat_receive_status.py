"""Device-local receiver heartbeat shared with member command processes."""

import hashlib
import json
import time
from pathlib import Path
from typing import Literal

from guildbotics.utils.fileio import atomic_write_text, get_workspace_local_path

MAX_HEARTBEAT_AGE_SECONDS = 15.0
HEARTBEAT_INTERVAL_SECONDS = 5.0
ReceiveState = Literal["ready", "catching_up", "unavailable"]


class ChatReceiveStatus:
    def __init__(self, workspace_root: Path | None = None) -> None:
        self.root = get_workspace_local_path(
            "chat-receivers", workspace_root=workspace_root
        )

        self._saved: dict[Path, tuple[ReceiveState, float]] = {}

    def _path(self, service: str, person_id: str, channel_id: str) -> Path:
        key = json.dumps([service, person_id, channel_id]).encode()
        return self.root / (hashlib.sha256(key).hexdigest() + ".json")

    def save(
        self, service: str, person_id: str, channel_id: str, *, state: ReceiveState
    ) -> None:
        path = self._path(service, person_id, channel_id)
        now = time.monotonic()
        previous = self._saved.get(path)
        if (
            previous
            and previous[0] == state
            and now - previous[1] < HEARTBEAT_INTERVAL_SECONDS
        ):
            return
        atomic_write_text(path, json.dumps({"state": state, "checked_at": time.time()}))
        self._saved[path] = (state, now)

    def state(self, service: str, person_id: str, channel_id: str) -> ReceiveState:
        try:
            status = json.loads(self._path(service, person_id, channel_id).read_text())
            age = time.time() - float(status["checked_at"])
            state = status["state"]
            if 0 <= age <= MAX_HEARTBEAT_AGE_SECONDS and state in {
                "ready",
                "catching_up",
                "unavailable",
            }:
                return state
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return "unavailable"

    def available(self, service: str, person_id: str, channel_id: str) -> bool:
        return self.state(service, person_id, channel_id) == "ready"
