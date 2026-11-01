from __future__ import annotations

import threading
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from guildbotics.drivers.task_scheduler import (
    DEFAULT_CONSECUTIVE_ERROR_LIMIT,
    DEFAULT_ROUTINE_INTERVAL_MINUTES,
)
from guildbotics.utils.fileio import (
    get_workspace_local_path,
    load_yaml_file,
    save_yaml_file,
)

#: The service's settings for this device, under the workspace's ``local/``:
#: the service runs on this machine, so how it runs is a property of the
#: machine, like whether the machine sleeps while the service waits.
SETTINGS_FILE = "service.yml"
#: Orders the changes the screens make: each reads what is saved and writes
#: it back with its own change.
_WRITE_LOCK = threading.Lock()

RoutineIntervalMinutes = Annotated[int, Field(ge=1, le=1440)]
ConsecutiveErrorLimit = Annotated[int, Field(ge=1, le=20)]


class ServiceSettings(BaseModel):
    """How the service runs, whichever screen or command starts it: what it
    runs (scheduled commands, patrol, chat events; at least one), how often it
    patrols, and after how many failures in a row a worker stops."""

    model_config = ConfigDict(extra="forbid")

    keep_awake: bool = False
    scheduled: bool = True
    routine: bool = True
    event_queue: bool = True
    routine_interval_minutes: RoutineIntervalMinutes = DEFAULT_ROUTINE_INTERVAL_MINUTES
    max_consecutive_errors: ConsecutiveErrorLimit = DEFAULT_CONSECUTIVE_ERROR_LIMIT

    @model_validator(mode="after")
    def _require_a_source(self) -> ServiceSettings:
        if not (self.scheduled or self.routine or self.event_queue):
            raise ValueError("at least one runtime source must be enabled")
        return self


class ServiceSettingsChange(BaseModel):
    """What one screen changed; the settings it leaves out stay as saved, so
    screens changing different settings do not undo each other."""

    model_config = ConfigDict(extra="forbid")

    keep_awake: bool | None = None
    scheduled: bool | None = None
    routine: bool | None = None
    event_queue: bool | None = None
    routine_interval_minutes: RoutineIntervalMinutes | None = None
    max_consecutive_errors: ConsecutiveErrorLimit | None = None


class ServiceSettingsError(ValueError):
    """The settings file exists but does not hold service settings."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def service_settings_path(workspace: Path | None = None) -> Path:
    return get_workspace_local_path(SETTINGS_FILE, workspace_root=workspace)


def read_service_settings(workspace: Path | None = None) -> ServiceSettings:
    """Read this device's service settings; the defaults until one is saved.

    Raises:
        ServiceSettingsError: If the file cannot be read as service settings.
    """
    path = service_settings_path(workspace)
    try:
        return ServiceSettings.model_validate(load_yaml_file(path))
    except FileNotFoundError:
        return ServiceSettings()
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
        raise ServiceSettingsError(path, str(exc)) from exc


def write_service_settings(settings: ServiceSettings) -> None:
    """Save this device's service settings for every later start."""
    path = service_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    save_yaml_file(path, settings.model_dump())


def update_service_settings(change: ServiceSettingsChange) -> ServiceSettings:
    """Save one screen's change over this device's saved service settings.

    Raises:
        ServiceSettingsError: If the saved file cannot be read as settings.
        ValidationError: If the change would leave nothing to run.
    """
    with _WRITE_LOCK:
        settings = ServiceSettings.model_validate(
            read_service_settings().model_dump() | change.model_dump(exclude_none=True)
        )
        write_service_settings(settings)
        return settings
