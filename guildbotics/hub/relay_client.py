"""Device-side client for the Hub's owner and live relay commands."""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import IO, Any

from guildbotics.hub import connection, host, relay


class HubRelayClientError(RuntimeError):
    """Raised when a relay command cannot be started or decoded."""


@dataclass(frozen=True)
class ServiceOwnerResult:
    owner: relay.ServiceOwner | None
    claimed: bool = False


class HubRelayClient:
    """Use local file operations or the same SSH command over a remote Hub."""

    def __init__(
        self,
        location: connection.HubLocation,
        workspace_id: str,
        device_id: str,
        publisher_id: str,
    ) -> None:
        self.location = location
        self.workspace_id = host.require_workspace_id(workspace_id)
        self.device_id = _require_uuid(device_id, "device_id")
        self.publisher_id = _require_uuid(publisher_id, "publisher_id")
        self._publisher: _Publisher | None = None
        self._publisher_lock = threading.Lock()
        self._watch_process: subprocess.Popen[str] | None = None
        self._watch_lock = threading.Lock()

    def owner_get(self) -> relay.ServiceOwner | None:
        payload = self._owner_command(["owner", "get", self.workspace_id])
        return _decode_owner(payload)

    def owner_claim(self) -> ServiceOwnerResult:
        payload = self._owner_command(
            ["owner", "claim", self.workspace_id, self.device_id]
        )
        return ServiceOwnerResult(
            owner=_decode_owner(payload), claimed=bool(payload.get("claimed"))
        )

    def owner_transfer(self, device_id: str) -> relay.ServiceOwner:
        target = _require_uuid(device_id, "device_id")
        payload = self._owner_command(["owner", "transfer", self.workspace_id, target])
        owner = _decode_owner(payload)
        if owner is None:
            raise HubRelayClientError("The Hub did not return a service owner.")
        return owner

    def publish_line(self, line: str) -> None:
        """Publish one line, reopening the SSH stream after a disconnect."""
        with self._publisher_lock:
            if self.location.is_local:
                relay.publish_live_line(
                    self.workspace_id,
                    self.device_id,
                    self.publisher_id,
                    line,
                )
                return
            publisher = self._publisher or self._open_publisher()
            try:
                publisher.write(line)
            except (BrokenPipeError, OSError):
                publisher.close()
                publisher = self._open_publisher()
                publisher.write(line)
            self._publisher = publisher

    def close(self) -> None:
        with self._publisher_lock:
            if self._publisher is not None:
                self._publisher.close()
                self._publisher = None
        with self._watch_lock:
            process = self._watch_process
            self._watch_process = None
        if process is not None:
            _terminate(process)

    def watch(
        self,
        callback: Callable[[str], None],
        stop_event: threading.Event,
        *,
        reconnect_delay: float = 1.0,
    ) -> None:
        """Forward watch lines and reconnect while the owning process lives."""
        if self.location.is_local:
            relay.watch_live(
                self.workspace_id,
                output=_CallbackWriter(callback),
                stop_event=stop_event,
            )
            return
        while not stop_event.is_set():
            try:
                process = self._open_watch_process()
            except OSError:
                stop_event.wait(reconnect_delay)
                continue
            with self._watch_lock:
                self._watch_process = process
            try:
                if stop_event.is_set():
                    return
                stdout = process.stdout
                if stdout is None:
                    raise HubRelayClientError("The Hub watch has no stdout stream.")
                for line in stdout:
                    if stop_event.is_set():
                        break
                    if line.rstrip("\r\n"):
                        callback(line.rstrip("\r\n"))
            except (OSError, ValueError):
                pass
            finally:
                with self._watch_lock:
                    if self._watch_process is process:
                        self._watch_process = None
                _terminate(process)
            stop_event.wait(reconnect_delay)

    def _owner_command(self, arguments: list[str]) -> dict[str, Any]:
        if self.location.is_local:
            command = arguments[0:2]
            if command == ["owner", "get"]:
                owner = relay.read_service_owner(self.workspace_id)
                return {"owner": _owner_dict(owner)}
            if command == ["owner", "claim"]:
                owner, claimed = relay.claim_service_owner(
                    self.workspace_id, self.device_id
                )
                return {"owner": _owner_dict(owner), "claimed": claimed}
            if command == ["owner", "transfer"]:
                owner = relay.transfer_service_owner(self.workspace_id, arguments[-1])
                return {"owner": _owner_dict(owner)}
            raise HubRelayClientError(f"Unsupported local owner command: {arguments}")
        endpoint = self.location.endpoint
        if endpoint is None:  # pragma: no cover - guarded by the local branch
            raise HubRelayClientError("A local Hub has no remote owner command.")
        try:
            output = connection.run_hub_command(
                endpoint, [*arguments, "--format", "json"]
            )
            payload = json.loads(output)
        except (AttributeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise HubRelayClientError("The Hub returned invalid owner data.") from exc
        if not isinstance(payload, dict):
            raise HubRelayClientError("The Hub returned invalid owner data.")
        return payload

    def _open_publisher(self) -> _Publisher:
        process = subprocess.Popen(
            self._ssh_command(
                [
                    "live",
                    "publish",
                    self.workspace_id,
                    self.device_id,
                    self.publisher_id,
                ]
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
        if process.stdin is None:
            _terminate(process)
            raise HubRelayClientError("The Hub publish has no stdin stream.")
        return _Publisher(process, process.stdin)

    def _open_watch_process(self) -> subprocess.Popen[str]:
        return subprocess.Popen(
            self._ssh_command(["live", "watch", self.workspace_id]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

    def _ssh_command(self, arguments: list[str]) -> list[str]:
        endpoint = self.location.endpoint
        if endpoint is None:  # pragma: no cover - guarded by local branches
            raise HubRelayClientError("A local Hub has no SSH endpoint.")
        return connection.hub_ssh_command(endpoint, arguments)


@dataclass
class _Publisher:
    process: subprocess.Popen[str]
    stream: IO[str]

    def write(self, line: str) -> None:
        self.stream.write(line.rstrip("\r\n") + "\n")
        self.stream.flush()

    def close(self) -> None:
        with suppress(OSError):
            self.stream.close()
        try:
            self.process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            _terminate(self.process)


class _CallbackWriter:
    def __init__(self, callback: Callable[[str], None]) -> None:
        self._callback = callback

    def write(self, value: str) -> int:
        for line in value.splitlines():
            if line:
                self._callback(line)
        return len(value)

    def flush(self) -> None:
        return None


def _decode_owner(payload: dict[str, Any]) -> relay.ServiceOwner | None:
    value = payload.get("owner")
    if value is None:
        return None
    try:
        return relay.ServiceOwner.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise HubRelayClientError("The Hub returned invalid owner data.") from exc


def _owner_dict(owner: relay.ServiceOwner | None) -> dict[str, Any] | None:
    return None if owner is None else owner.model_dump()


def _require_uuid(value: str, label: str) -> str:
    try:
        return host.require_uuid(value, label)
    except host.InvalidWorkspaceIdError as exc:
        raise HubRelayClientError(str(exc)) from exc


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2.0)
