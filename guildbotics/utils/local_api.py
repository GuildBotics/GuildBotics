"""Device-local discovery record for a running Desktop API."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from guildbotics.utils.fileio import atomic_write_text, get_machine_state_path


class LocalApiEndpoint(BaseModel):
    """A private endpoint; the token must never appear in logs or argv."""

    model_config = ConfigDict(strict=True)

    port: int = Field(gt=0, le=65535)
    token: str = Field(min_length=1, repr=False)
    pid: int = Field(gt=0)
    service_instance_id: str = Field(min_length=1)
    workspace: Path | None

    def publish(self) -> None:
        # atomic_write_text uses a private (0600) temporary file and rename.
        atomic_write_text(endpoint_path(), self.model_dump_json())

    def discard(self) -> None:
        current = read_endpoint()
        if current and current.service_instance_id == self.service_instance_id:
            endpoint_path().unlink(missing_ok=True)


def endpoint_path() -> Path:
    return get_machine_state_path("run", "app-api.json")


def read_endpoint() -> LocalApiEndpoint | None:
    try:
        return LocalApiEndpoint.model_validate_json(endpoint_path().read_bytes())
    except (OSError, ValidationError):
        return None
