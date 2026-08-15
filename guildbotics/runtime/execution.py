"""Execution placement for local and remote command runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class ExecutionPlacement:
    """Where a workflow or command should run.

    A missing target always resolves to ``local``. Remote placement is reserved
    for a later RemoteJob implementation.
    """

    kind: Literal["local", "remote"]
    device_id: str | None = None

    @classmethod
    def local(cls) -> ExecutionPlacement:
        return cls(kind="local")

    @classmethod
    def remote(cls, device_id: str) -> ExecutionPlacement:
        device = device_id.strip()
        if not device:
            raise ValueError("remote placement requires a device_id")
        return cls(kind="remote", device_id=device)


def resolve_execution_placement(target_device: str | None = None) -> ExecutionPlacement:
    """Resolve placement. An omitted target is always local."""
    if target_device and target_device.strip():
        return ExecutionPlacement.remote(target_device)
    return ExecutionPlacement.local()
